"""Read-only codebase-state collection and local publication.

No model calls. No git hooks. No scheduler installation.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .exclusions import (
    contains_secret,
    is_key_excerpt,
    looks_binary,
    path_excluded,
    redact_text,
    remote_identity,
    sanitize_remote,
    symlink_escapes,
)

SCHEMA = "noesis.codebase-state/v1"
REQUESTED_TZ = "America/Denver"
GIT_BIN = "/usr/bin/git"
DANGEROUS_CONFIG_RE = re.compile(
    r"(?im)^\s*(\[alias\]|alias\.|core\.hookspath|diff\.external|diff\.[^.\s]+\.textconv|filter\.|\[filter[\s\]]|clean\s*=|smudge\s*=)"
)


class LockHeld(RuntimeError):
    pass


@dataclass
class IngestConfig:
    projects_root: Path
    state_dir: Path
    name_prefix: str = "noesis-"
    requested_tz: str = REQUESTED_TZ
    host_tz: str | None = None
    max_runtime_s: float = 900.0
    max_file_bytes: int = 262_144
    max_files_per_repo: int = 20_000
    max_total_bytes: int = 256 * 1024 * 1024
    max_excerpt_bytes: int = 4_096
    max_todo_lines: int = 50
    retention_snapshots: int = 14
    dry_run: bool = False
    now: datetime | None = None
    git_bin: str = GIT_BIN


@dataclass
class RunResult:
    report: dict[str, Any]
    wrote_state: bool = False


def utc_now(config: IngestConfig) -> datetime:
    if config.now is not None:
        if config.now.tzinfo is None:
            return config.now.replace(tzinfo=timezone.utc)
        return config.now.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def host_timezone_name() -> str:
    link = Path("/etc/localtime")
    try:
        target = os.path.realpath(link)
    except OSError:
        return "unknown"
    marker = "/zoneinfo/"
    if marker in target:
        return target.split(marker, 1)[1]
    return "unknown"


def classify_local_time(tz_name: str, year: int, month: int, day: int, hour: int, minute: int) -> str:
    """Return exists, skipped, or repeated for a civil time in tz_name."""
    tz = ZoneInfo(tz_name)
    start = datetime(year, month, day, tzinfo=timezone.utc) - timedelta(hours=14)
    hits = 0
    for offset in range(0, 28 * 60):
        local = (start + timedelta(minutes=offset)).astimezone(tz)
        if (local.year, local.month, local.day, local.hour, local.minute) == (
            year,
            month,
            day,
            hour,
            minute,
        ):
            hits += 1
    if hits == 0:
        return "skipped"
    if hits > 1:
        return "repeated"
    return "exists"


def dst_notes(tz_name: str, hour: int, minute: int, year: int = 2026) -> dict[str, Any]:
    """Document America/Denver transition behavior for a civil time.

    2026-03-08 is the US spring-forward Sunday. 2026-11-01 is the fall-back Sunday.
    These dates are the US DST rules, not a cron-daemon observation.
    """
    spring = classify_local_time(tz_name, year, 3, 8, hour, minute)
    fall = classify_local_time(tz_name, year, 11, 1, hour, minute)
    return {
        "timezone": tz_name,
        "hour": hour,
        "minute": minute,
        "spring_forward_date": f"{year}-03-08",
        "spring_forward": spring,
        "fall_back_date": f"{year}-11-01",
        "fall_back": fall,
        "note": (
            "BSD/vixie cron on macOS evaluates the host timezone. A skipped civil time "
            "does not run that morning. A repeated civil time may run once or twice "
            "depending on the cron implementation; this collector does not claim which."
        ),
    }


def propose_schedule(
    host_tz: str,
    requested_tz: str = REQUESTED_TZ,
    hour: int = 2,
    minute: int = 30,
) -> dict[str, Any]:
    """Return a reviewable crontab proposal. Does not install it."""
    requested = dst_notes(requested_tz, hour, minute)
    recommended = dst_notes(requested_tz, 3, 30)
    blocked = host_tz != requested_tz
    line = None
    if not blocked:
        line = (
            f"{minute} {hour} * * * PATH=/usr/bin:/bin HOME=/Users/elvis "
            "/bin/bash /Users/elvis/projects/noesis-agent-stack/orchestration/"
            "codebase_state/run_ingest.sh run "
            "--state-dir /Users/elvis/.local/share/noesis/codebase-state "
            ">> /Users/elvis/.local/log/noesis-codebase-state.log 2>&1"
        )
    return {
        "schema": SCHEMA,
        "installed": False,
        "activation": "not_performed",
        "scheduler": "user_crontab",
        "scheduler_reason": (
            "The host already uses user crontab for wiki-sync.sh. Hermes cron is a "
            "different scheduler: observed jobs interpret cron fields in the host "
            "offset, and the Hermes skill documents a 3-minute hard interrupt plus "
            "a default model turn. This job must stay deterministic and local, so "
            "user crontab is the proposed mechanism. It is not installed."
        ),
        "hermes_cron_not_used_because": [
            "3-minute hard interrupt documented in hermes-agent background-systems reference",
            "default cron jobs invoke the main model; source must not be sent to an external model",
            "an existing Hermes job (05d68f5af3c5) is unrelated; creating another would be a second scheduler",
        ],
        "execution_user": "elvis",
        "host_timezone": host_tz,
        "requested_timezone": requested_tz,
        "timezone_match": not blocked,
        "blocked_reason": (
            None
            if not blocked
            else "host timezone differs from America/Denver; refusing an unqualified crontab line"
        ),
        "requested_schedule": requested,
        "recommended_schedule": recommended,
        "requested_crontab_line": line,
        "recommended_hour": 3,
        "recommended_reason": (
            "02:30 America/Denver does not exist on spring-forward morning "
            f"({requested['spring_forward']}). 03:30 exists on both 2026 transition days "
            f"(spring={recommended['spring_forward']}, fall={recommended['fall_back']})."
        ),
    }


def _git_dir(path: Path) -> Path | None:
    git = path / ".git"
    if git.is_dir():
        return git
    if git.is_file():
        text = git.read_text(encoding="utf-8", errors="replace").strip()
        if text.startswith("gitdir:"):
            raw = text.split(":", 1)[1].strip()
            resolved = Path(raw)
            if not resolved.is_absolute():
                resolved = (path / resolved).resolve()
            return resolved
    return None


def _read_head(git_dir: Path) -> tuple[str, str]:
    """Return (branch_or_detached, sha_or_empty) from files only."""
    head_path = git_dir / "HEAD"
    if not head_path.is_file():
        return ("unknown", "")
    head = head_path.read_text(encoding="utf-8", errors="replace").strip()
    if head.startswith("ref:"):
        ref = head.split(":", 1)[1].strip()
        branch = ref.removeprefix("refs/heads/")
        ref_file = git_dir / ref
        if ref_file.is_file():
            return (branch, ref_file.read_text(encoding="utf-8", errors="replace").strip())
        packed = git_dir / "packed-refs"
        if packed.is_file():
            prefix = f"{ref} "
            # packed-refs lines are "<sha> <ref>"
            for line in packed.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("#") or line.startswith("^"):
                    continue
                parts = line.split()
                if len(parts) == 2 and parts[1] == ref:
                    return (branch, parts[0])
        return (branch, "")
    if re.fullmatch(r"[0-9a-fA-F]{40}", head):
        return ("DETACHED", head)
    return ("unknown", "")


def _local_config_dangerous(git_dir: Path) -> str | None:
    config = git_dir / "config"
    if not config.is_file():
        return None
    text = config.read_text(encoding="utf-8", errors="replace")
    match = DANGEROUS_CONFIG_RE.search(text)
    if match:
        return match.group(1).strip()
    return None


def _git_env() -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
        "GIT_EXTERNAL_DIFF": "",
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
    }


def _git(
    config: IngestConfig,
    repo: Path,
    git_dir: Path,
    args: list[str],
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    git_bin = config.git_bin
    if not Path(git_bin).exists():
        git_bin = shutil.which("git") or git_bin
    cmd = [
        git_bin,
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=",
        "-c",
        "diff.external=",
        "-c",
        "alias.rev-parse=",
        "-c",
        "alias.ls-tree=",
        "-c",
        "alias.ls-files=",
        "-c",
        "alias.diff-index=",
        "-c",
        "alias.check-ignore=",
        "-c",
        "alias.status=",
        "-c",
        "alias.diff=",
        "--git-dir",
        str(git_dir),
        "--work-tree",
        str(repo),
        *args,
    ]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=30,
        input=input_text,
        env=_git_env(),
        check=False,
    )


def _origin_url(config: IngestConfig, repo: Path, git_dir: Path) -> str:
    config_file = git_dir / "config"
    if config_file.is_file():
        text = config_file.read_text(encoding="utf-8", errors="replace")
        # Prefer a direct parse so a malicious alias cannot run.
        match = re.search(r'(?im)^\s*url\s*=\s*(\S+)\s*$', text)
        # Take the first remote url under [remote "origin"] if present.
        origin = re.search(
            r'(?is)\[remote\s+"origin"\](.*?)(?:\n\[|\Z)',
            text,
        )
        if origin:
            url_match = re.search(r"(?im)^\s*url\s*=\s*(\S+)\s*$", origin.group(1))
            if url_match:
                return sanitize_remote(url_match.group(1))
        if match:
            return sanitize_remote(match.group(1))
    return ""


def discover(config: IngestConfig) -> list[dict[str, Any]]:
    root = config.projects_root
    if not root.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for child in sorted(root.iterdir(), key=lambda p: p.name):
        if not child.is_dir() or not child.name.startswith(config.name_prefix):
            continue
        if child.is_symlink():
            records.append(
                {
                    "path": str(child),
                    "name": child.name,
                    "kind": "symlink_match",
                    "ingestable": False,
                    "reason": "top-level match is a symlink; not followed",
                }
            )
            continue
        git_dir = _git_dir(child)
        if git_dir is None:
            records.append(
                {
                    "path": str(child),
                    "name": child.name,
                    "kind": "non_repository",
                    "ingestable": False,
                    "reason": "no .git directory or gitdir file",
                }
            )
            continue
        branch, sha = _read_head(git_dir)
        kind = "linked_worktree" if (child / ".git").is_file() else "primary_checkout"
        worktree_of = ""
        if kind == "linked_worktree":
            # .../repo/.git/worktrees/<name>
            parts = git_dir.parts
            if "worktrees" in parts:
                idx = parts.index("worktrees")
                worktree_of = str(Path(*parts[: idx - 1])) if idx >= 1 else ""
        remote = _origin_url(config, child, git_dir)
        records.append(
            {
                "path": str(child),
                "name": child.name,
                "kind": kind,
                "ingestable": True,
                "git_dir": str(git_dir),
                "branch": branch,
                "head_sha": sha,
                "detached": branch == "DETACHED",
                "worktree_of": worktree_of,
                "remote_sanitized": remote,
                "remote_identity": remote_identity(remote) or f"local/{child.name}",
            }
        )
    return records


def _ignored(config: IngestConfig, repo: Path, git_dir: Path, rels: list[str]) -> set[str]:
    """Return untracked paths git itself would ignore.

    Do not pass --no-index. That flag reports tracked files as ignored when a
    pattern matches, which drops the committed tree from the snapshot.
    """
    if not rels:
        return set()
    ignored: set[str] = set()
    for start in range(0, len(rels), 400):
        chunk = rels[start : start + 400]
        payload = "\0".join(chunk) + "\0"
        proc = _git(
            config,
            repo,
            git_dir,
            ["check-ignore", "-z", "--stdin"],
            input_text=payload,
        )
        if proc.returncode not in (0, 1):
            continue
        ignored.update(item for item in proc.stdout.split("\0") if item)
    return ignored


def _ls_tree(config: IngestConfig, repo: Path, git_dir: Path) -> dict[str, str] | None:
    proc = _git(config, repo, git_dir, ["ls-tree", "-r", "-z", "HEAD"])
    if proc.returncode != 0:
        return None
    blobs: dict[str, str] = {}
    for item in proc.stdout.split("\0"):
        if not item or "\t" not in item:
            continue
        meta, path = item.split("\t", 1)
        parts = meta.split()
        if len(parts) < 3:
            continue
        mode, _typ, oid = parts[0], parts[1], parts[2]
        if mode == "160000":
            blobs[path] = f"submodule:{oid}"
            continue
        if mode.startswith("120"):
            blobs[path] = f"symlink:{oid}"
            continue
        blobs[path] = oid
    return blobs


def _index_blobs(config: IngestConfig, repo: Path, git_dir: Path) -> dict[str, str] | None:
    proc = _git(config, repo, git_dir, ["ls-files", "-s", "-z"])
    if proc.returncode != 0:
        return None
    blobs: dict[str, str] = {}
    for item in proc.stdout.split("\0"):
        if not item or "\t" not in item:
            continue
        meta, path = item.split("\t", 1)
        parts = meta.split()
        if len(parts) >= 2:
            blobs[path] = parts[1]
    return blobs


def _git_blob_sha1(data: bytes) -> str:
    header = f"blob {len(data)}\0".encode("ascii")
    return hashlib.sha1(header + data).hexdigest()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def collect_repo(config: IngestConfig, record: dict[str, Any], deadline: datetime) -> dict[str, Any]:
    repo = Path(record["path"])
    git_dir = Path(record["git_dir"])
    collected_at = utc_now(config).isoformat()
    head_before = _read_head(git_dir)
    result: dict[str, Any] = {
        "path": str(repo),
        "name": record["name"],
        "kind": record["kind"],
        "remote_identity": record["remote_identity"],
        "remote_sanitized": record["remote_sanitized"],
        "branch": head_before[0],
        "head_sha": head_before[1],
        "detached": head_before[0] == "DETACHED",
        "worktree_of": record.get("worktree_of", ""),
        "collected_at": collected_at,
        "deployment_status": "unverified",
        "status": "success",
        "incomplete_reason": "",
        "git_exec": "used",
        "files": {},
        "excluded": [],
        "todos": [],
        "model_calls": 0,
        "bytes_hashed": 0,
        "changed_during_collection": False,
    }
    dangerous = _local_config_dangerous(git_dir)
    if dangerous:
        result["git_exec"] = "suppressed"
        result["status"] = "incomplete"
        result["incomplete_reason"] = f"untrusted_git_config:{dangerous}"
        return result
    if datetime.now(timezone.utc) > deadline:
        result["status"] = "skipped_budget"
        result["incomplete_reason"] = "runtime_budget"
        return result
    committed = _ls_tree(config, repo, git_dir)
    index = _index_blobs(config, repo, git_dir)
    if committed is None or index is None:
        result["status"] = "incomplete"
        result["incomplete_reason"] = "git_read_failed"
        result["git_exec"] = "failed"
        return result

    candidates: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(repo, followlinks=False):
        current = Path(dirpath)
        kept: list[str] = []
        for dirname in dirnames:
            child = current / dirname
            if child.is_symlink():
                rel = child.relative_to(repo)
                reason = "symlink_dir_not_followed"
                if symlink_escapes(repo, child):
                    reason = "symlink_escapes_root"
                result["excluded"].append({"path": rel.as_posix(), "reason": reason})
                continue
            if dirname in {".git"} or dirname.startswith(".git"):
                continue
            rel_dir = child.relative_to(repo)
            why = path_excluded(rel_dir / "placeholder")
            if why and why.startswith("excluded_dir:"):
                result["excluded"].append({"path": rel_dir.as_posix() + "/", "reason": why})
                continue
            kept.append(dirname)
        dirnames[:] = kept
        for filename in filenames:
            candidates.append(current / filename)

    rels = []
    for path in candidates:
        try:
            rels.append(path.relative_to(repo).as_posix())
        except ValueError:
            continue
    ignored = _ignored(config, repo, git_dir, rels)
    ignored -= set(committed) | set(index)

    files: dict[str, Any] = {}
    total = 0
    for path in candidates:
        if datetime.now(timezone.utc) > deadline:
            result["status"] = "incomplete"
            result["incomplete_reason"] = "runtime_budget"
            break
        if len(files) >= config.max_files_per_repo or total >= config.max_total_bytes:
            result["status"] = "incomplete"
            result["incomplete_reason"] = "byte_or_file_budget"
            break
        rel = path.relative_to(repo)
        rel_s = rel.as_posix()
        if path.is_symlink():
            reason = "symlink_escapes_root" if symlink_escapes(repo, path) else "symlink_not_followed"
            result["excluded"].append({"path": rel_s, "reason": reason})
            continue
        why = path_excluded(rel)
        if why:
            result["excluded"].append({"path": rel_s, "reason": why})
            continue
        if rel_s in ignored:
            result["excluded"].append({"path": rel_s, "reason": "gitignore"})
            continue
        if committed.get(rel_s, "").startswith("submodule:"):
            result["excluded"].append({"path": rel_s, "reason": "submodule_not_traversed"})
            continue
        try:
            st = path.stat()
        except OSError as exc:
            result["excluded"].append({"path": rel_s, "reason": f"unreadable:{exc.__class__.__name__}"})
            continue
        if st.st_size > config.max_file_bytes:
            result["excluded"].append({"path": rel_s, "reason": "oversize"})
            continue
        try:
            data = path.read_bytes()
        except OSError as exc:
            result["excluded"].append({"path": rel_s, "reason": f"unreadable:{exc.__class__.__name__}"})
            continue
        try:
            st2 = path.stat()
        except OSError:
            result["changed_during_collection"] = True
            result["status"] = "incomplete"
            result["incomplete_reason"] = "changed_during_collection"
            continue
        if st2.st_size != st.st_size or st2.st_mtime_ns != st.st_mtime_ns:
            result["changed_during_collection"] = True
            result["status"] = "incomplete"
            result["incomplete_reason"] = "changed_during_collection"
            continue
        if looks_binary(data[:8192]):
            result["excluded"].append({"path": rel_s, "reason": "binary"})
            continue
        text = data.decode("utf-8", errors="replace")
        if contains_secret(text):
            result["excluded"].append({"path": rel_s, "reason": "secret_content"})
            continue
        digest = _sha256(data)
        blob = _git_blob_sha1(data)
        head_blob = committed.get(rel_s)
        index_blob = index.get(rel_s)
        if head_blob and head_blob.startswith("symlink:"):
            result["excluded"].append({"path": rel_s, "reason": "committed_symlink"})
            continue
        if head_blob is None and index_blob is None:
            wt = "untracked"
        elif head_blob == blob and (index_blob in (None, blob)):
            wt = "committed_clean"
        elif index_blob and index_blob != head_blob and index_blob == blob:
            wt = "staged"
        elif index_blob and index_blob != head_blob and blob != index_blob:
            wt = "staged_and_unstaged"
        else:
            wt = "unstaged"
        line_count = text.count("\n") + (0 if text.endswith("\n") or text == "" else 1)
        excerpt = ""
        if is_key_excerpt(rel):
            excerpt = redact_text(text[: config.max_excerpt_bytes])
            if contains_secret(excerpt):
                excerpt = ""
                result["excluded"].append({"path": rel_s, "reason": "excerpt_withheld_secret"})
        files[rel_s] = {
            "sha256": digest,
            "git_blob": blob,
            "head_blob": head_blob or "",
            "status": wt,
            "bytes": len(data),
            "line_count": line_count,
            "excerpt": excerpt,
            "collected_at": collected_at,
            "head_sha": head_before[1],
        }
        total += len(data)
        if len(result["todos"]) < config.max_todo_lines:
            for n, line in enumerate(text.splitlines(), start=1):
                if re.search(r"\b(TODO|FIXME)\b", line):
                    result["todos"].append(
                        {"path": rel_s, "line": n, "text": redact_text(line.strip())[:240]}
                    )
                    if len(result["todos"]) >= config.max_todo_lines:
                        break
    # Deleted eligible paths: committed, not excluded by name, absent on disk.
    present = set(files)
    if result["status"] == "success":
        for rel_s, oid in committed.items():
            if rel_s in present or oid.startswith("submodule:") or oid.startswith("symlink:"):
                continue
            rel = Path(rel_s)
            why = path_excluded(rel)
            if why:
                continue
            disk = repo / rel
            if disk.exists() or disk.is_symlink():
                continue
            files[rel_s] = {
                "sha256": "",
                "git_blob": "",
                "head_blob": oid,
                "status": "deleted",
                "bytes": 0,
                "line_count": 0,
                "excerpt": "",
                "collected_at": collected_at,
                "head_sha": head_before[1],
            }
    _infer_renames(files)
    head_after = _read_head(git_dir)
    if head_after != head_before:
        result["changed_during_collection"] = True
        result["status"] = "incomplete"
        result["incomplete_reason"] = "head_changed_during_collection"
    result["files"] = files
    result["bytes_hashed"] = total
    result["head_sha_after"] = head_after[1]
    return result


def _infer_renames(files: dict[str, Any]) -> None:
    deleted = [path for path, meta in files.items() if meta["status"] == "deleted" and meta.get("head_blob")]
    added = [
        path
        for path, meta in files.items()
        if meta["status"] in {"untracked", "staged"} and meta.get("git_blob")
    ]
    used: set[str] = set()
    for old in deleted:
        old_blob = files[old]["head_blob"]
        for new in added:
            if new in used:
                continue
            if files[new]["git_blob"] == old_blob:
                files[old]["status"] = "renamed_from"
                files[old]["renamed_to"] = new
                files[new]["status"] = "renamed_to"
                files[new]["renamed_from"] = old
                used.add(new)
                break


def summarize_repo(repo: dict[str, Any]) -> str:
    files = repo.get("files") or {}
    top_dirs = sorted({path.split("/", 1)[0] for path in files if "/" in path and files[path]["status"] != "deleted"})
    readme = ""
    for name in ("README.md", "README.rst", "README"):
        meta = files.get(name)
        if meta and meta.get("excerpt"):
            readme = meta["excerpt"].splitlines()[0][:240]
            break
    counts: dict[str, int] = {}
    for meta in files.values():
        counts[meta["status"]] = counts.get(meta["status"], 0) + 1
    lines = [
        f"# {repo.get('name', 'repository')}",
        "",
        f"- schema: {SCHEMA}",
        f"- fact: path `{repo.get('path', '')}`",
        f"- fact: kind `{repo.get('kind', '')}` branch `{repo.get('branch', '')}` head `{repo.get('head_sha', '')}`",
        f"- fact: remote_identity `{repo.get('remote_identity', '')}`",
        f"- fact: collected_at `{repo.get('collected_at', '')}`",
        "- fact: deployment_status `unverified` (no runtime probe was authorized)",
        f"- fact: scan_status `{repo.get('status', '')}` {repo.get('incomplete_reason', '')}".rstrip(),
        f"- observed_top_level_dirs: {', '.join(top_dirs) if top_dirs else '(none in eligible files)'}",
        f"- observed_file_status_counts: {json.dumps(counts, sort_keys=True)}",
    ]
    if readme:
        lines.append(f"- documentation_claim: README first line: {readme}")
    else:
        lines.append("- known_unknown: no eligible README excerpt")
    if repo.get("todos"):
        lines.append("- observed_todos:")
        for item in repo["todos"][:10]:
            lines.append(f"  - `{item['path']}:{item['line']}` {item['text']}")
    lines.append("- inference: cross-repository dependency edges are not asserted by this collector")
    lines.append("- model_calls: 0")
    lines.append("")
    lines.append("Every material line above is either a collector fact or an explicitly labeled claim.")
    return "\n".join(lines) + "\n"


def _restrict(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _restrict(path.parent, 0o700)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    _restrict(path, 0o600)


def _snapshot_unchanged(previous: dict[str, Any] | None, repos: list[dict[str, Any]]) -> bool:
    if not previous:
        return False
    prev_files = previous.get("file_index") or {}
    new_index = _file_index(repos)
    return prev_files == new_index


def _file_index(repos: list[dict[str, Any]]) -> dict[str, Any]:
    index: dict[str, Any] = {}
    for repo in repos:
        if repo.get("status") != "success":
            continue
        index[repo["path"]] = {
            "head_sha": repo.get("head_sha") or "",
            "files": {
                path: f"{meta.get('sha256', '')}|{meta.get('status', '')}"
                for path, meta in (repo.get("files") or {}).items()
            },
        }
    return index


def _publish(config: IngestConfig, report: dict[str, Any], repos: list[dict[str, Any]]) -> dict[str, Any]:
    state = config.state_dir
    state.mkdir(parents=True, exist_ok=True)
    _restrict(state, 0o700)
    if state.resolve().is_relative_to(config.projects_root.resolve()):
        raise RuntimeError("state_dir must not live inside the scanned projects root")
    checkpoint_path = state / "checkpoint.json"
    previous = _load_json(checkpoint_path)
    # Preserve last success for repos that did not complete.
    published_repos = []
    prev_by_key = {}
    if previous:
        for repo in previous.get("repos") or []:
            prev_by_key[repo.get("path")] = repo
    for repo in repos:
        if repo.get("status") == "success":
            published_repos.append(repo)
        elif repo.get("path") in prev_by_key and repo.get("status") != "success":
            carried = dict(prev_by_key[repo["path"]])
            carried["carried_forward"] = True
            carried["this_run_status"] = repo.get("status")
            published_repos.append(carried)
            repo["preserved_previous"] = True
        else:
            published_repos.append(repo)
    scan_repos = [r for r in repos if r.get("kind") in {"primary_checkout", "linked_worktree"}]
    all_scans_ok = bool(scan_repos) and all(r.get("status") == "success" for r in scan_repos)
    success_index = _file_index([r for r in repos if r.get("status") == "success"])
    unchanged = bool(previous) and all_scans_ok and (previous.get("file_index") or {}) == success_index
    tombstones = _tombstones(previous, repos) if all_scans_ok else []
    stamp = utc_now(config).strftime("%Y%m%dT%H%M%SZ")
    if not scan_repos:
        report["ingestion"] = "no_repository_content"
        report["snapshot_id"] = (previous or {}).get("snapshot_id")
        report["checkpoint_advanced"] = False
        report["model_calls"] = 0
        report["published_as_current"] = False
        _atomic_write(
            state / "last-run.json",
            json.dumps(report, indent=2, sort_keys=True).encode("utf-8") + b"\n",
        )
        return report
    if not all_scans_ok:
        report["ingestion"] = "withheld_partial"
        report["snapshot_id"] = (previous or {}).get("snapshot_id")
        report["checkpoint_advanced"] = False
        report["checkpoint_note"] = "partial or empty scan; current pointer and checkpoint left unchanged"
        report["model_calls"] = 0
        report["published_as_current"] = False
        _atomic_write(
            state / "last-run.json",
            json.dumps(report, indent=2, sort_keys=True).encode("utf-8") + b"\n",
        )
        return report
    if unchanged:
        report["ingestion"] = "unchanged"
        report["snapshot_id"] = (previous or {}).get("snapshot_id")
        report["duplicate_chunks"] = 0
        report["model_calls"] = 0
        report["published_as_current"] = False
        report["checkpoint_advanced"] = False
        _atomic_write(
            state / "last-run.json",
            json.dumps(report, indent=2, sort_keys=True).encode("utf-8") + b"\n",
        )
        return report
    snapshot = state / "snapshots" / stamp
    snapshot.mkdir(parents=True, exist_ok=True)
    _restrict(snapshot, 0o700)
    manifest = {
        "schema": SCHEMA,
        "snapshot_id": stamp,
        "repos": published_repos,
        "tombstones": tombstones,
        "model_calls": 0,
    }
    _atomic_write(snapshot / "manifest.json", json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n")
    summary_dir = snapshot / "summaries"
    summary_dir.mkdir(exist_ok=True)
    _restrict(summary_dir, 0o700)
    for repo in published_repos:
        if repo.get("kind") not in {"primary_checkout", "linked_worktree"}:
            continue
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", repo.get("name") or "repo")
        text = summarize_repo(repo)
        _atomic_write(summary_dir / f"{name}.md", text.encode("utf-8"))
    _atomic_write(snapshot / "report.json", json.dumps(report, indent=2, sort_keys=True).encode("utf-8") + b"\n")
    pointer = {
        "schema": SCHEMA,
        "snapshot_id": stamp,
        "path": str(snapshot),
        "freshness": "complete",
    }
    _atomic_write(state / "current.json", json.dumps(pointer, indent=2, sort_keys=True).encode("utf-8") + b"\n")
    checkpoint = {
        "schema": SCHEMA,
        "snapshot_id": stamp,
        "file_index": success_index,
        "repos": [
            {
                "path": r.get("path"),
                "head_sha": r.get("head_sha"),
                "status": r.get("status"),
                "files": r.get("files"),
                "kind": r.get("kind"),
                "name": r.get("name"),
                "remote_identity": r.get("remote_identity"),
                "branch": r.get("branch"),
                "collected_at": r.get("collected_at"),
            }
            for r in published_repos
            if r.get("status") == "success" or r.get("carried_forward")
        ],
    }
    # Advance checkpoint only after current.json is in place.
    if (state / "current.json").is_file():
        advance = all(r.get("status") == "success" for r in scan_repos) if scan_repos else True
        if advance:
            _atomic_write(checkpoint_path, json.dumps(checkpoint, indent=2, sort_keys=True).encode("utf-8") + b"\n")
            report["checkpoint_advanced"] = True
        else:
            report["checkpoint_advanced"] = False
            report["checkpoint_note"] = "partial scan; checkpoint not advanced"
    _rotate_snapshots(state, config.retention_snapshots, stamp)
    report["ingestion"] = "updated"
    report["snapshot_id"] = stamp
    report["model_calls"] = 0
    report["published_as_current"] = True
    report["tombstones"] = tombstones
    return report


def _tombstones(previous: dict[str, Any] | None, repos: list[dict[str, Any]]) -> list[dict[str, str]]:
    if not previous:
        return []
    stones: list[dict[str, str]] = []
    prev_repos = {r.get("path"): r for r in previous.get("repos") or []}
    for repo in repos:
        if repo.get("status") != "success":
            continue
        old = prev_repos.get(repo.get("path")) or {}
        old_files = (old.get("files") or {}) if isinstance(old, dict) else {}
        new_files = repo.get("files") or {}
        for path, meta in old_files.items():
            if path not in new_files and meta.get("status") != "deleted":
                stones.append(
                    {
                        "repo": repo.get("path", ""),
                        "path": path,
                        "sha256": meta.get("sha256", ""),
                        "reason": "absent_after_complete_scan",
                    }
                )
        for path, meta in new_files.items():
            if meta.get("status") in {"deleted", "renamed_from"}:
                stones.append(
                    {
                        "repo": repo.get("path", ""),
                        "path": path,
                        "sha256": meta.get("sha256", ""),
                        "reason": meta.get("status", "deleted"),
                    }
                )
    return stones


def _rotate_snapshots(state: Path, keep: int, current_id: str) -> None:
    root = state / "snapshots"
    if not root.is_dir():
        return
    names = sorted(p.name for p in root.iterdir() if p.is_dir())
    extra = names[:-keep] if keep > 0 else names
    for name in extra:
        if name == current_id:
            continue
        shutil.rmtree(root / name, ignore_errors=True)


class _Lock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = None

    def __enter__(self) -> "_Lock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _restrict(self.path.parent, 0o700)
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(self.fd)
            self.fd = None
            raise LockHeld(str(self.path)) from exc
        return self

    def __exit__(self, *_: object) -> None:
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


def run_ingestion(config: IngestConfig) -> RunResult:
    started = utc_now(config)
    deadline = datetime.now(timezone.utc) + timedelta(seconds=config.max_runtime_s)
    host_tz = config.host_tz or host_timezone_name()
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "started_at": started.isoformat(),
        "host_timezone": host_tz,
        "requested_timezone": config.requested_tz,
        "dry_run": config.dry_run,
        "model_calls": 0,
        "external_model_calls": 0,
        "semantic_retrieval": "blocked",
        "semantic_retrieval_reason": "no verified local codebase ingestion backend was bound; qmd and wiki ingest-status are markdown tools, not this contract",
        "deployment_status": "unverified",
        "schedule_installed": False,
        "limits": {
            "max_runtime_s": config.max_runtime_s,
            "max_file_bytes": config.max_file_bytes,
            "max_files_per_repo": config.max_files_per_repo,
            "max_total_bytes": config.max_total_bytes,
        },
    }
    if config.max_runtime_s <= 0:
        report["status"] = "skipped"
        report["reason"] = "runtime_budget"
        report["discovery"] = []
        report["finished_at"] = utc_now(config).isoformat()
        return RunResult(report=report, wrote_state=False)
    discovery = discover(config)
    report["discovery"] = discovery
    if not discovery:
        report["status"] = "success"
        report["reason"] = "empty_match_set"
        report["repos"] = []
        report["finished_at"] = utc_now(config).isoformat()
        if not config.dry_run:
            _publish(config, report, [])
            return RunResult(report=report, wrote_state=True)
        return RunResult(report=report, wrote_state=False)

    lock_path = config.state_dir / "run.lock"
    try:
        lock_cm = _Lock(lock_path) if not config.dry_run else _NullLock()
        with lock_cm:
            repos: list[dict[str, Any]] = []
            for record in discovery:
                if not record.get("ingestable"):
                    repos.append(
                        {
                            "path": record["path"],
                            "name": record["name"],
                            "kind": record["kind"],
                            "status": record["kind"],
                            "ingestable": False,
                            "files": {},
                            "model_calls": 0,
                            "deployment_status": "unverified",
                        }
                    )
                    continue
                if datetime.now(timezone.utc) > deadline:
                    skipped = dict(record)
                    skipped["status"] = "skipped_budget"
                    skipped["files"] = {}
                    skipped["model_calls"] = 0
                    repos.append(skipped)
                    continue
                try:
                    repos.append(collect_repo(config, record, deadline))
                except OSError as exc:
                    failed = dict(record)
                    failed["status"] = "unavailable"
                    failed["incomplete_reason"] = exc.__class__.__name__
                    failed["files"] = {}
                    failed["model_calls"] = 0
                    failed["deployment_status"] = "unverified"
                    repos.append(failed)
            statuses = [r.get("status") for r in repos if r.get("ingestable", True) and r.get("kind") in {"primary_checkout", "linked_worktree"}]
            if not statuses:
                overall = "success"
            elif all(s == "success" for s in statuses):
                overall = "success"
            elif any(s == "success" for s in statuses):
                overall = "partial"
            elif all(s == "unavailable" for s in statuses):
                overall = "partial"
            else:
                preserved = (config.state_dir / "checkpoint.json").is_file()
                if all(s in {"incomplete", "failed"} for s in statuses) and preserved:
                    overall = "partial"
                elif all(s in {"incomplete", "failed"} for s in statuses):
                    overall = "failed"
                else:
                    overall = "partial"
            if any(s == "skipped_budget" for s in statuses) and not any(s == "success" for s in statuses):
                overall = "partial"
            report["status"] = overall
            report["repos"] = [
                {k: v for k, v in repo.items() if k != "files"}
                | {"file_count": len(repo.get("files") or {}), "statuses": _status_counts(repo)}
                for repo in repos
            ]
            report["finished_at"] = utc_now(config).isoformat()
            report["model_calls"] = 0
            if config.dry_run:
                report["ingestion"] = "dry_run"
                return RunResult(report=report, wrote_state=False)
            _publish(config, report, repos)
            return RunResult(report=report, wrote_state=True)
    except LockHeld:
        report["status"] = "skipped"
        report["reason"] = "lock_held"
        report["finished_at"] = utc_now(config).isoformat()
        return RunResult(report=report, wrote_state=False)


def _status_counts(repo: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for meta in (repo.get("files") or {}).values():
        counts[meta["status"]] = counts.get(meta["status"], 0) + 1
    return counts


class _NullLock:
    def __enter__(self) -> "_NullLock":
        return self

    def __exit__(self, *_: object) -> None:
        return None


def current_snapshot(state_dir: Path) -> dict[str, Any] | None:
    pointer = _load_json(state_dir / "current.json")
    if not pointer:
        return None
    snap = Path(pointer["path"])
    manifest = _load_json(snap / "manifest.json")
    return {"pointer": pointer, "manifest": manifest, "snapshot": str(snap)}


def query_current(state_dir: Path, text: str, limit: int = 20) -> list[dict[str, Any]]:
    """Substring retrieval over current summaries and path index. Not semantic."""
    current = current_snapshot(state_dir)
    if not current or not current.get("manifest"):
        return []
    needle = text.casefold()
    hits: list[dict[str, Any]] = []
    snap = Path(current["snapshot"])
    summary_dir = snap / "summaries"
    if summary_dir.is_dir():
        for path in sorted(summary_dir.glob("*.md")):
            body = path.read_text(encoding="utf-8", errors="replace")
            if needle in body.casefold():
                hits.append(
                    {
                        "kind": "summary",
                        "summary_path": str(path),
                        "excerpt": _line_hit(body, needle),
                        "provenance": "current_summary",
                    }
                )
    for repo in current["manifest"].get("repos") or []:
        for rel, meta in (repo.get("files") or {}).items():
            if meta.get("status") in {"deleted", "renamed_from"}:
                continue
            hay = " ".join(
                [
                    rel,
                    meta.get("excerpt") or "",
                    repo.get("path") or "",
                    repo.get("name") or "",
                ]
            ).casefold()
            if needle in hay:
                hits.append(
                    {
                        "kind": "file",
                        "repo_path": repo.get("path"),
                        "relative_path": rel,
                        "sha256": meta.get("sha256"),
                        "head_sha": meta.get("head_sha") or repo.get("head_sha"),
                        "worktree_status": meta.get("status"),
                        "collected_at": meta.get("collected_at"),
                        "snapshot_id": current["pointer"].get("snapshot_id"),
                        "provenance": "current_manifest",
                    }
                )
            if len(hits) >= limit:
                return hits
    return hits[:limit]


def _line_hit(body: str, needle: str) -> str:
    for line in body.splitlines():
        if needle in line.casefold():
            return line[:400]
    return ""


def status_document(state_dir: Path) -> dict[str, Any]:
    pointer = _load_json(state_dir / "current.json")
    checkpoint = _load_json(state_dir / "checkpoint.json")
    last = _load_json(state_dir / "last-run.json")
    return {
        "schema": SCHEMA,
        "state_dir": str(state_dir),
        "current": pointer,
        "checkpoint_snapshot": None if not checkpoint else checkpoint.get("snapshot_id"),
        "last_run_status": None if not last else last.get("status"),
        "schedule_installed": False,
        "semantic_retrieval": "blocked",
    }
