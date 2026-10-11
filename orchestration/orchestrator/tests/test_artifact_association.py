"""Artifact-association repair regressions (RED before implementation).

Offline process double; no provider requests, no live ledgers. These tests pin
the following operator-specified guarantees:

- ``candidate_digest`` is the verified artifact digest (sha256 of the collected
  candidate artifact bytes), never sha256 of captured stdout.
- Captured stdout keeps its own execution-evidence identity (``stdout_digest``).
- Nonzero exit and timeout preserve available partial artifact references and
  captured output, and record collection failure explicitly.
- ``LaunchResult`` binds task_id/attempt_id/contract_revision/assignment_epoch/
  baseline to the attempt.
- No collected candidate means ``candidate_digest is None`` (never fabricated).
- Partial evidence never yields accepted/verification_pending=True.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.launcher import HermesCliAdapter


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _Completed:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.pid = 424242


class ArtifactDouble:
    """Process/provider boundary: emits a chosen stdout body, exits, or times out."""

    supports_fallback_deny = True

    def __init__(
        self,
        *,
        body: str = "",
        returncode: int = 0,
        raise_timeout: bool = False,
        partial_stdout: str | None = None,
    ) -> None:
        self.body = body
        self.returncode = returncode
        self.raise_timeout = raise_timeout
        self.partial_stdout = partial_stdout
        self.calls: list[dict] = []
        self.spawn_count = 0
        self.cleanup_invocations = 0
        self.group_signals: list[int | None] = []
        self.active_pid = 424242

    def __call__(self, cmd, **kwargs):
        self.calls.append({"cmd": list(cmd), "timeout": kwargs.get("timeout")})
        if "profile" in cmd and "show" in cmd:
            profile = cmd[cmd.index("-p") + 1]
            return _Completed(0, f"Profile: {profile}\nPath: /offline/profiles/{profile}\n")
        if self.raise_timeout:
            import subprocess

            self.spawn_count += 1
            exc = subprocess.TimeoutExpired(cmd, kwargs.get("timeout") or 0)
            setattr(exc, "pid", self.active_pid)
            if self.partial_stdout is not None:
                setattr(exc, "stdout", self.partial_stdout)
            raise exc
        self.spawn_count += 1
        return _Completed(self.returncode, self.body or "", "")

    def terminate_group(self, handle: dict) -> bool:
        self.group_signals.append(handle.get("pgid") or handle.get("pid"))
        self.cleanup_invocations += 1
        return True


def _adapter(tmp_path: Path, runner: ArtifactDouble | None = None) -> HermesCliAdapter:
    runner = runner or ArtifactDouble()
    profiles = tmp_path / "profiles"
    (profiles / "noesis-forge").mkdir(parents=True, exist_ok=True)
    (profiles / "noesis-forge" / "SOUL.md").write_text("name: noesis-forge\n", encoding="utf-8")
    return HermesCliAdapter(hermes_bin="hermes", profiles_root=profiles, process_runner=runner)


def _launch(adapter, *, workspace, task_id="task-1", **extra):
    return adapter.launch(
        profile_id="noesis-forge",
        prompt="implement the repair",
        task_id=task_id,
        dry_run=False,
        provider="approved-provider",
        model="approved-model",
        timeout_s=30,
        remaining_lease_s=30,
        workspace=workspace,
        fallback_policy="deny",
        **extra,
    )


# ---------------------------------------------------------------------------
# Candidate identity is distinct from stdout identity
# ---------------------------------------------------------------------------

def test_candidate_digest_is_verified_artifact_digest_not_stdout(tmp_path):
    (tmp_path / "candidate.diff").write_text("hello candidate", encoding="utf-8")
    artifact_digest = _sha256_text("hello candidate")
    body = json.dumps({"artifacts": ["candidate.diff"], "digest": "sha256:" + artifact_digest})
    runner = ArtifactDouble(body=body)
    adapter = _adapter(tmp_path, runner)
    result = _launch(adapter, workspace=tmp_path)

    stdout_digest = _sha256_text(body)
    assert result.ok is True
    assert result.candidate_digest == artifact_digest
    assert result.candidate_digest != stdout_digest
    assert result.stdout_digest == stdout_digest
    assert result.artifacts == ("candidate.diff",)


def test_no_collected_candidate_means_no_candidate_digest(tmp_path):
    runner = ArtifactDouble(body="plain stdout, no artifact payload")
    adapter = _adapter(tmp_path, runner)
    result = _launch(adapter, workspace=tmp_path)
    assert result.ok is True
    assert result.candidate_digest is None
    assert result.verification_pending is False
    assert result.accepted is False
    assert result.artifacts == ()


def test_claimed_digest_mismatch_fails_explicitly(tmp_path):
    (tmp_path / "candidate.diff").write_text("hello candidate", encoding="utf-8")
    body = json.dumps({"artifacts": ["candidate.diff"], "digest": "sha256:" + "0" * 64})
    runner = ArtifactDouble(body=body)
    adapter = _adapter(tmp_path, runner)
    result = _launch(adapter, workspace=tmp_path)
    assert result.ok is False
    assert result.blocker_code == "malformed_evidence"
    assert result.candidate_digest is None


# ---------------------------------------------------------------------------
# Nonzero exit preserves partial evidence
# ---------------------------------------------------------------------------

def test_nonzero_exit_preserves_artifacts_and_stdout(tmp_path):
    (tmp_path / "candidate.diff").write_text("partial candidate", encoding="utf-8")
    artifact_digest = _sha256_text("partial candidate")
    body = json.dumps({"artifacts": ["candidate.diff"], "digest": "sha256:" + artifact_digest})
    runner = ArtifactDouble(body=body, returncode=1)
    adapter = _adapter(tmp_path, runner)
    result = _launch(adapter, workspace=tmp_path)

    assert result.ok is False
    assert result.blocker_code == "child_failed"
    assert result.disposition == "nonzero_exit"
    assert result.artifacts == ("candidate.diff",)
    assert result.candidate_digest == artifact_digest
    assert result.stdout_digest == _sha256_text(body)
    assert result.captured_stdout == body
    assert result.accepted is False
    assert result.verification_pending is False


# ---------------------------------------------------------------------------
# Timeout preserves partial evidence and records cleanup certainty
# ---------------------------------------------------------------------------

def test_timeout_preserves_partial_artifact_refs_and_stdout(tmp_path):
    (tmp_path / "partial.txt").write_text("partial bytes", encoding="utf-8")
    artifact_digest = _sha256_text("partial bytes")
    partial_stdout = json.dumps({"artifacts": ["partial.txt"], "digest": "sha256:" + artifact_digest})
    runner = ArtifactDouble(raise_timeout=True, partial_stdout=partial_stdout)
    adapter = _adapter(tmp_path, runner)
    result = _launch(adapter, workspace=tmp_path, task_id="task-timeout")

    assert result.ok is False
    assert result.blocker_code == "timeout"
    assert result.cleanup_state == "terminated"
    assert result.workspace_reusable is True
    assert result.artifacts == ("partial.txt",)
    assert result.candidate_digest == artifact_digest
    assert result.stdout_digest == _sha256_text(partial_stdout)
    assert result.captured_stdout == partial_stdout
    assert result.accepted is False
    assert result.verification_pending is False
    assert runner.cleanup_invocations == 1


# ---------------------------------------------------------------------------
# Task/attempt binding
# ---------------------------------------------------------------------------

def test_launch_result_binds_task_and_attempt_identifiers(tmp_path):
    (tmp_path / "candidate.diff").write_text("x", encoding="utf-8")
    body = json.dumps({"artifacts": ["candidate.diff"], "digest": "sha256:" + _sha256_text("x")})
    runner = ArtifactDouble(body=body)
    adapter = _adapter(tmp_path, runner)
    result = _launch(
        adapter,
        workspace=tmp_path,
        task_id="task-7",
        attempt_id="attempt-3",
        contract_revision="rev-9",
        assignment_epoch=5,
        baseline="291eb3e17878179b084aa9e58caf7813b403a006",
    )
    assert result.task_id == "task-7"
    assert result.attempt_id == "attempt-3"
    assert result.contract_revision == "rev-9"
    assert result.assignment_epoch == 5
    assert result.baseline == "291eb3e17878179b084aa9e58caf7813b403a006"


def test_distinct_tasks_bind_distinct_identifiers_not_cross_assigned(tmp_path):
    (tmp_path / "candidate.diff").write_text("x", encoding="utf-8")
    body = json.dumps({"artifacts": ["candidate.diff"], "digest": "sha256:" + _sha256_text("x")})
    adapter = _adapter(tmp_path, ArtifactDouble(body=body))
    result_a = _launch(adapter, workspace=tmp_path, task_id="task-A", attempt_id="attempt-1", assignment_epoch=1)
    result_b = _launch(adapter, workspace=tmp_path, task_id="task-B", attempt_id="attempt-2", assignment_epoch=2)
    assert result_a.task_id == "task-A"
    assert result_b.task_id == "task-B"
    assert (result_a.task_id, result_a.attempt_id, result_a.assignment_epoch) != (
        result_b.task_id,
        result_b.attempt_id,
        result_b.assignment_epoch,
    )


# ---------------------------------------------------------------------------
# Existing safety gates stay enforced (fallback deny, lease/approved pair)
# ---------------------------------------------------------------------------

def test_fallback_deny_and_missing_pair_still_block(tmp_path):
    adapter = _adapter(tmp_path, ArtifactDouble())
    # Missing approved pair blocks before spawn.
    blocked = adapter.launch(
        profile_id="noesis-forge",
        prompt="implement",
        task_id="task-1",
        dry_run=False,
        timeout_s=30,
        workspace=tmp_path,
    )
    assert blocked.ok is False
    assert blocked.blocker_code == "missing_approved_pair"

    # Non-deny fallback policy blocks before spawn.
    denied = adapter.launch(
        profile_id="noesis-forge",
        prompt="implement",
        task_id="task-1",
        dry_run=False,
        provider="approved-provider",
        model="approved-model",
        timeout_s=30,
        remaining_lease_s=30,
        workspace=tmp_path,
        fallback_policy="allow",
    )
    assert denied.ok is False
    assert denied.blocker_code == "fallback_unenforced"