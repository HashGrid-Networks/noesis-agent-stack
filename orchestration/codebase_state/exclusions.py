"""Inclusion, exclusion, and redaction rules.

Repository content is untrusted data. These helpers never execute it.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

EXCLUDED_DIR_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "vendor",
        "third_party",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        ".cache",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        "dist",
        "build",
        "target",
        "coverage",
        "htmlcov",
        ".next",
        "out",
        "site-packages",
        ".terraform",
        ".worktrees",
        "Pods",
    }
)

EXCLUDED_SUFFIXES = frozenset(
    {
        ".pem",
        ".key",
        ".p12",
        ".pfx",
        ".sqlite",
        ".sqlite3",
        ".db",
        ".log",
        ".woff",
        ".woff2",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".pdf",
        ".zip",
        ".tar",
        ".gz",
        ".tgz",
        ".bz2",
        ".7z",
        ".whl",
        ".so",
        ".dylib",
        ".dll",
        ".exe",
        ".bin",
        ".gguf",
        ".safetensors",
        ".pt",
        ".pth",
        ".onnx",
        ".parquet",
        ".pyc",
        ".pyo",
    }
)

EXCLUDED_BASENAMES = frozenset(
    {
        ".env",
        ".env.local",
        ".env.production",
        ".env.development",
        "id_rsa",
        "id_ed25519",
        "id_ecdsa",
        "credentials.json",
        "cookies.txt",
        "cookies.sqlite",
        ".netrc",
        ".npmrc",
        ".pypirc",
    }
)

SECRET_NAME_PARTS = ("secret", "credential", "token", "cookie", "password", "passwd")

KEY_EXCERPT_NAMES = frozenset(
    {
        "readme",
        "readme.md",
        "readme.rst",
        "agents.md",
        "claude.md",
        "pyproject.toml",
        "package.json",
        "go.mod",
        "cargo.toml",
        "requirements.txt",
    }
)

_SECRET_RES = (
    re.compile(
        r"(?i)\b(api[_-]?key|secret|token|password|passwd|pwd|authorization)\b\s*[=:]\s*['\"]?[A-Za-z0-9_./+=-]{8,}"
    ),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"https?://[^/\s:@]+:[^@\s/]+@"),
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{12,}\b"),
)

_USERINFO_RE = re.compile(r"(https?://)([^/\s@]+)@")


def sanitize_remote(url: str) -> str:
    """Drop userinfo from a remote URL. Never log the original."""
    if not url:
        return ""
    cleaned = _USERINFO_RE.sub(r"\1", url.strip())
    # scp-style git@host:path has no password; leave it. Reject embedded tokens
    # that arrived as query parameters.
    cleaned = re.sub(r"([?&](?:token|access_token|password|secret)=)[^&\s]+", r"\1REDACTED", cleaned, flags=re.I)
    return cleaned


def remote_identity(url: str) -> str:
    cleaned = sanitize_remote(url)
    cleaned = re.sub(r"^git@", "", cleaned)
    cleaned = re.sub(r"^https?://", "", cleaned)
    cleaned = cleaned.replace(":", "/")
    if cleaned.endswith(".git"):
        cleaned = cleaned[: -len(".git")]
    return cleaned.strip("/").lower()


def is_secret_basename(name: str) -> bool:
    lower = name.lower()
    if lower in EXCLUDED_BASENAMES or lower.startswith(".env"):
        return True
    stem = lower.rsplit(".", 1)[0]
    if any(part in stem for part in SECRET_NAME_PARTS) and not _is_example_name(lower):
        return True
    return False


def _is_example_name(lower: str) -> bool:
    return lower.endswith((".example", ".sample", ".template", ".example.yml", ".example.yaml"))


def path_excluded(rel: Path) -> str | None:
    """Return an exclusion reason, or None if the relative path may be considered."""
    parts = rel.parts
    if not parts:
        return "empty"
    for part in parts[:-1]:
        if part in EXCLUDED_DIR_NAMES or part.startswith(".git"):
            return f"excluded_dir:{part}"
    name = parts[-1]
    lower = name.lower()
    if is_secret_basename(name) and not _is_example_name(lower):
        return "secret_filename"
    if Path(name).suffix.lower() in EXCLUDED_SUFFIXES:
        return "excluded_suffix"
    if lower.endswith(".env") or ".env." in lower:
        return "secret_filename"
    return None


def looks_binary(prefix: bytes) -> bool:
    return b"\0" in prefix


def contains_secret(text: str) -> bool:
    return any(pattern.search(text) for pattern in _SECRET_RES)


def redact_text(text: str) -> str:
    redacted = text
    for pattern in _SECRET_RES:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def is_key_excerpt(rel: Path) -> bool:
    lower = rel.name.lower()
    if lower in KEY_EXCERPT_NAMES:
        return True
    if len(rel.parts) >= 3 and rel.parts[0] == ".github" and rel.parts[1] == "workflows":
        return rel.suffix.lower() in {".yml", ".yaml"}
    return False


def symlink_escapes(root: Path, path: Path) -> bool:
    """True when a symlink's resolved target is outside root. Does not follow chains past the link."""
    if not path.is_symlink():
        return False
    raw = os.readlink(path)
    target = Path(raw)
    if not target.is_absolute():
        target = (path.parent / target).resolve(strict=False)
    else:
        target = target.resolve(strict=False)
    root_resolved = root.resolve(strict=False)
    try:
        target.relative_to(root_resolved)
    except ValueError:
        return True
    return False
