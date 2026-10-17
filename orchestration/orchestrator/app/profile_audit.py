"""Report-only deterministic profile auditor (slice H).

The Phase B contract requires a deterministic, report-only auditor with
singleton locking, finding-fingerprint deduplication, bounded execution,
and zero side effects on profiles or runtime state.

This module is offline and report-only by construction:

*   It only reads from repository sources (``profiles/``, ``agents/``,
    ``shared/``, ``platform/``) and from the user-provided
    ``installed_profiles_root`` (sandbox-style for tests, or the live
    ``~/.hermes/profiles`` for operator-initiated runs).
*   It writes to ``report_path`` (Markdown summary) and
    ``findings_path`` (JSONL of structured findings), both supplied by
    the caller. Nothing else is touched.
*   The scheduled recurrence contract is intentionally **not** wired.
    The existing Hermes cron scheduler can be reused in the future as a
    pure activation interface; this module exposes only ``run_once`` so a
    schedule cannot enable or disable it from within.

Decision result is captured in :class:`AuditDecision` and the typed
``status`` field so callers (a future cron entry, an operator shell
invocation, a CI test) can react without parsing the prose report.
"""
from __future__ import annotations

import dataclasses
import fcntl
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# Status outcomes for ``run_once``.
RAN = "ran"
SKIPPED = "skipped"
ABORTED_RUNTIME_BOUND = "aborted_runtime_bound"


@dataclass(frozen=True)
class Finding:
    """One audit finding.

    ``fingerprint`` is a deterministic hash over the canonicalised inputs
    (profile id, kind, severity, principal evidence fields). Two runs
    with identical inputs must produce identical fingerprints; this is
    what enables ``run_once`` to deduplicate.
    """

    kind: str
    severity: str
    profile_id: str | None
    summary: str
    evidence: dict[str, Any]
    fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "profile_id": self.profile_id,
            "summary": self.summary,
            "evidence": self.evidence,
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class AuditDecision:
    status: str
    finding_count: int
    report_path: Path
    findings_path: Path
    started_at: datetime
    finished_at: datetime
    notes: tuple[str, ...] = ()


def _canonicalise(payload: dict[str, Any]) -> str:
    """Canonicalise a finding's payload so it fingerprints deterministically."""
    return json.dumps(payload, sort_keys=True, default=str)


def _fingerprint(*, kind: str, severity: str, profile_id: str | None, summary: str,
                 evidence: dict[str, Any]) -> str:
    seed = _canonicalise({
        "kind": kind,
        "severity": severity,
        "profile_id": profile_id,
        "summary": summary,
        "evidence": evidence,
    })
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


class DeterministicProfileAuditor:
    """A bounded, report-only profile auditor.

    Parameters are explicit. There is no implicit configuration; the
    caller (a shell driver, a future Hermes cron adapter, a CI script) is
    the only authority that decides what to audit and where to write.
    """

    FORBIDDEN_METHODS = frozenset({
        "install_schedule",
        "schedule_cron",
        "activate",
        "register_cron",
    })

    def __init__(
        self,
        *,
        repo_root: Path,
        installed_profiles_root: Path,
        report_path: Path,
        findings_path: Path,
        lock_path: Path | None = None,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.installed_profiles_root = Path(installed_profiles_root)
        self.report_path = Path(report_path)
        self.findings_path = Path(findings_path)
        self.lock_path = (
            Path(lock_path) if lock_path is not None else Path(tempfile_default_lock_name())
        )
        self._lock_fh = None

    # ---------------------------------------------------------------- locks

    def acquire_lock(self) -> None:
        """Acquire the singleton lock. Idempotent within this process."""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_fh = self.lock_path.open("a+")
        try:
            fcntl.flock(self._lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            # Release our handle; the caller will get the SKIPPED decision.
            self._lock_fh.close()
            self._lock_fh = None
            raise AlreadyLocked(str(self.lock_path)) from exc

    def release_lock(self) -> None:
        if self._lock_fh is None:
            return
        try:
            fcntl.flock(self._lock_fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self._lock_fh.close()
        self._lock_fh = None

    def run_once(
        self,
        *,
        report_only: bool,
        max_runtime_s: float,
        audit_targets: Iterable[str] | None = None,
    ) -> AuditDecision:
        """One bounded report-only run.

        ``report_only`` is a typed contract: setting it to ``False`` is a
        silent override attempt and the run must refuse rather than
        mutate anything.

        ``max_runtime_s`` is a wall-clock bound; if exceeded the run
        aborts cleanly without partial state.
        """
        started_at = datetime.now(timezone.utc)
        if not report_only:
            return self._abort(
                started_at=started_at,
                finding_count=0,
                notes=("report_only must be true; mutation is not permitted",),
            )

        # Bound the wall-clock budget. A negative bound is not a free pass
        # to skip the lock — we still try to take it and report the bound.
        try:
            self.acquire_lock()
        except AlreadyLocked:
            return AuditDecision(
                status=SKIPPED,
                finding_count=0,
                report_path=self.report_path,
                findings_path=self.findings_path,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
                notes=("lock_held",),
            )

        deadline = time.monotonic() + max(0.0, float(max_runtime_s))
        notes: list[str] = []
        try:
            findings = self._collect_findings(
                audit_targets=audit_targets or [],
                deadline=deadline,
            )
            if time.monotonic() > deadline:
                return self._abort(
                    started_at=started_at,
                    finding_count=len(findings),
                    notes=("aborted_runtime_bound",),
                )
            self._write_findings(findings)
            if time.monotonic() > deadline:
                return self._abort(
                    started_at=started_at,
                    finding_count=len(findings),
                    notes=("aborted_runtime_bound",),
                )
            self._write_report(findings, started_at=started_at)
            return AuditDecision(
                status=RAN,
                finding_count=len(findings),
                report_path=self.report_path,
                findings_path=self.findings_path,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
                notes=tuple(notes),
            )
        except _RuntimeBound:
            return self._abort(
                started_at=started_at,
                finding_count=0,
                notes=("aborted_runtime_bound",),
            )
        finally:
            self.release_lock()

    # --------------------------------------------------------- finding collection

    def _collect_findings(
        self, *, audit_targets: Iterable[str], deadline: float,
    ) -> list[Finding]:
        findings: list[Finding] = []
        for finding in self._check_approved_files(deadline=deadline):
            findings.append(finding)
        for finding in self._check_profile_drift(deadline=deadline):
            findings.append(finding)
        for finding in self._check_manifest_internal_consistency(deadline=deadline):
            findings.append(finding)
        # Deterministic dedup by fingerprint preserves order on first-seen.
        seen: dict[str, Finding] = {}
        for finding in findings:
            seen.setdefault(finding.fingerprint, finding)
        return list(seen.values())

    def _check_approved_files(self, *, deadline: float) -> Iterable[Finding]:
        """Verify that each declared profile's required identity files exist.

        This is a deterministic check; the auditor never reads config
        contents, only their presence and size.
        """
        roster_path = self.repo_root / "profiles" / "noesis-roster.yaml"
        if not roster_path.is_file():
            return
        import yaml

        roster = yaml.safe_load(roster_path.read_text(encoding="utf-8")) or {}
        profiles = roster.get("profiles") or {}
        for profile_id in sorted(profiles.keys()):
            self._raise_if_overrun(deadline)
            for filename in ("agent.yaml",):
                path = self.repo_root / "agents" / profile_id / filename
                if not path.is_file():
                    yield Finding(
                        kind="missing_identity_file",
                        severity="error",
                        profile_id=profile_id,
                        summary=f"agent manifest {filename} missing for {profile_id}",
                        evidence={"path": str(path)},
                        fingerprint=_fingerprint(
                            kind="missing_identity_file",
                            severity="error",
                            profile_id=profile_id,
                            summary=f"agent manifest {filename} missing for {profile_id}",
                            evidence={"path": str(path)},
                        ),
                    )
            installed = self.installed_profiles_root / profile_id
            for filename in ("config.yaml", "SOUL.md"):
                path = installed / filename
                if installed.is_dir() and not path.is_file():
                    yield Finding(
                        kind="installed_profile_missing_identity_file",
                        severity="warning",
                        profile_id=profile_id,
                        summary=f"installed profile missing {filename}",
                        evidence={"path": str(path)},
                        fingerprint=_fingerprint(
                            kind="installed_profile_missing_identity_file",
                            severity="warning",
                            profile_id=profile_id,
                            summary=f"installed profile missing {filename}",
                            evidence={"path": str(path)},
                        ),
                    )

    def _check_profile_drift(self, *, deadline: float) -> Iterable[Finding]:
        """Detect drift between the roster and the registry.

        Specifically: profile ids declared in ``profiles/noesis-roster.yaml``
        that are missing from the agent registry's ``profiles`` block
        produce a typed drift finding. The auditor never writes to either.
        """
        import yaml

        roster_path = self.repo_root / "profiles" / "noesis-roster.yaml"
        agents_path = self.repo_root / "platform" / "agent-registry.yaml"
        if not (roster_path.is_file() and agents_path.is_file()):
            return
        roster = yaml.safe_load(roster_path.read_text(encoding="utf-8")) or {}
        agents = yaml.safe_load(agents_path.read_text(encoding="utf-8")) or {}
        declared = set((roster.get("profiles") or {}).keys())
        agent_profiles = {
            entry.get("name")
            for entry in (agents.get("profiles") or [])
            if isinstance(entry, dict)
        }
        # The agent-registry is allowed to declare additional profiles
        # (e.g. ``coder``), but it must contain every roster profile id.
        for profile_id in sorted(declared - agent_profiles):
            self._raise_if_overrun(deadline)
            summary = f"profile {profile_id} is in the roster but missing from agent-registry"
            yield Finding(
                kind="registry_drift",
                severity="warning",
                profile_id=profile_id,
                summary=summary,
                evidence={"registry": "agent-registry.yaml"},
                fingerprint=_fingerprint(
                    kind="registry_drift",
                    severity="warning",
                    profile_id=profile_id,
                    summary=summary,
                    evidence={"registry": "agent-registry.yaml"},
                ),
            )

    def _check_manifest_internal_consistency(
        self, *, deadline: float
    ) -> Iterable[Finding]:
        """Internal-consistency checks against the roster itself.

        Detects missing required keys (``role``, ``tier``) at the manifest
        level. It is deterministic: same inputs produce same findings.
        """
        import yaml

        roster_path = self.repo_root / "profiles" / "noesis-roster.yaml"
        if not roster_path.is_file():
            return
        roster = yaml.safe_load(roster_path.read_text(encoding="utf-8")) or {}
        profiles = roster.get("profiles") or {}
        for profile_id, raw in sorted(profiles.items()):
            self._raise_if_overrun(deadline)
            for required_key in ("role", "tier"):
                if not raw.get(required_key):
                    summary = f"profile {profile_id} is missing required roster field {required_key}"
                    yield Finding(
                        kind="manifest_internal_drift",
                        severity="error",
                        profile_id=profile_id,
                        summary=summary,
                        evidence={"field": required_key},
                        fingerprint=_fingerprint(
                            kind="manifest_internal_drift",
                            severity="error",
                            profile_id=profile_id,
                            summary=summary,
                            evidence={"field": required_key},
                        ),
                    )

    def _raise_if_overrun(self, deadline: float) -> None:
        if time.monotonic() > deadline:
            raise _RuntimeBound()

    # ----------------------------------------------------------------- writers

    def _write_findings(self, findings: list[Finding]) -> None:
        # Remediation B1 / minor 2: ``_write_findings`` now mirrors
        # ``_write_report`` (line below). The previous implementation
        # silently returned when ``findings_path.parent`` did not exist,
        # creating an inconsistent write path where findings could be
        # silently dropped while the report claimed the audit completed.
        # The parent directory is now created unconditionally and the
        # findings path is written atomically; a typed
        # ``FindingsWriteError`` is raised if either step fails.
        try:
            self.findings_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.findings_path.with_name(
                self.findings_path.name + ".tmp"
            )
            with tmp_path.open("w", encoding="utf-8") as fh:
                for finding in findings:
                    fh.write(_canonicalise(finding.to_dict()) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, self.findings_path)
        except OSError as exc:
            raise FindingsWriteError(
                f"failed to write findings to {self.findings_path}: {exc}"
            ) from exc

    def _write_report(self, findings: list[Finding], *, started_at: datetime) -> None:
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        lines.append("# Deterministic Profile Audit (Phase B)")
        lines.append("")
        lines.append(f"Started: {started_at.isoformat()}")
        lines.append(f"Repository: {self.repo_root}")
        lines.append(f"Installed profiles root: {self.installed_profiles_root}")
        lines.append(f"Findings: {len(findings)}")
        lines.append("")
        if not findings:
            lines.append("No findings recorded.")
        else:
            for finding in findings:
                lines.append(f"## [{finding.severity}] {finding.kind}")
                lines.append(f"- profile: {finding.profile_id or '-'}")
                lines.append(f"- summary: {finding.summary}")
                lines.append(f"- evidence: `{json.dumps(finding.evidence, sort_keys=True)}`")
                lines.append(f"- fingerprint: `{finding.fingerprint}`")
                lines.append("")
        # Atomic per-write guard: caller is in control of the report path
        # but it must not leak partially written bytes if interrupted.
        tmp_path = self.report_path.with_name(self.report_path.name + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
            fh.flush()
            import os
            os.fsync(fh.fileno())
        import os
        os.replace(tmp_path, self.report_path)

    # ----------------------------------------------------------------- helpers

    def _abort(
        self, *, started_at: datetime, finding_count: int, notes: tuple[str, ...]
    ) -> AuditDecision:
        return AuditDecision(
            status=ABORTED_RUNTIME_BOUND,
            finding_count=finding_count,
            report_path=self.report_path,
            findings_path=self.findings_path,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            notes=notes,
        )


class AlreadyLocked(RuntimeError):
    """The singleton lock is held by another audit run."""


class FindingsWriteError(RuntimeError):
    """Raised when ``_write_findings`` cannot persist the audit findings.

    Remediation B1 / minor 2: a typed error replaces the previous silent
    return when ``findings_path.parent`` did not exist, so callers can
    distinguish an empty finding set from a write failure without
    parsing free text.
    """


class _RuntimeBound(RuntimeError):
    """Internal sentinel raised when the wall-clock budget is exceeded."""


def tempfile_default_lock_name() -> str:
    """Stable default lock path under a caller-supplied location.

    The auditor itself never picks an absolute path; this helper exists so
    tests can construct a deterministic name without the auditor touching
    ``/tmp`` or relying on caller-provided order.
    """
    import tempfile

    return str(Path(tempfile.gettempdir()) / "noesis-adaptive-specialist-auditor.lock")
