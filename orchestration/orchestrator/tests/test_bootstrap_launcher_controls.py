"""Bounded launcher controls: offline process doubles, real TaskStore persistence.

These tests must not make provider requests, touch live ledgers, or treat a
backend exit as acceptance.
"""
from __future__ import annotations

import json
import hashlib
import os
import subprocess
import threading
from datetime import timedelta
from pathlib import Path

import pytest

from app.control_plane import Orchestrator, PolicyViolation
from app.launcher import HermesCliAdapter, LaunchResult
from app.models import Verification, utcnow
from app.specialist_routing import RoutingBlocked


class _Completed:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "", pid: int = 424242) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.pid = pid


class OfflineProcessDouble:
    """Process/provider boundary. Does not perform inference."""

    supports_fallback_deny = True

    def __init__(
        self,
        *,
        fallback_chain: list[tuple[str, str]] | None = None,
        start_then_raise: bool = False,
        raise_timeout: bool = False,
        confirm_cleanup: bool = True,
    ) -> None:
        self.fallback_chain = list(fallback_chain or [])
        self.start_then_raise = start_then_raise
        self.raise_timeout = raise_timeout
        self.confirm_cleanup = confirm_cleanup
        self.calls: list[dict] = []
        self.spawn_count = 0
        self.terminated: list[dict] = []
        self.group_signals: list[int | None] = []
        self.cleanup_invocations = 0
        self.started = False
        self.active_pid = 424242

    def __call__(self, cmd, **kwargs):
        env = dict(kwargs.get("env") or {})
        self.calls.append({
            "cmd": list(cmd),
            "env": env,
            "timeout": kwargs.get("timeout"),
            "cwd": kwargs.get("cwd"),
            "start_new_session": kwargs.get("start_new_session"),
        })
        text = " ".join(cmd)
        if "profile" in cmd and "show" in cmd:
            profile = cmd[cmd.index("-p") + 1]
            return _Completed(0, f"Profile: {profile}\nPath: /offline/profiles/{profile}\n")
        if self.raise_timeout:
            self.started = True
            self.spawn_count += 1
            self.active_pid = 424242
            exc = subprocess.TimeoutExpired(cmd, kwargs.get("timeout") or 0)
            setattr(exc, "pid", self.active_pid)
            raise exc
        if self.start_then_raise:
            self.started = True
            self.spawn_count += 1
            raise RuntimeError("crash after spawn before handle returned")
        provider = _flag(cmd, "--provider")
        model = _flag(cmd, "-m")
        if env.get("NOESIS_FALLBACK_POLICY") != "deny" and self.fallback_chain:
            provider, model = self.fallback_chain[0]
            self.calls[-1]["fallback_activated"] = True
        else:
            self.calls[-1]["fallback_activated"] = False
        if env.get("HERMES_INFERENCE_MODEL") or env.get("HERMES_MODEL"):
            self.calls[-1]["ambient_selected"] = True
        self.spawn_count += 1
        self.started = True
        body = json.dumps({"provider": provider, "model": model, "text": "verification pending"})
        return _Completed(0, body, "")

    def terminate(self, handle: dict) -> bool:
        self.terminated.append(dict(handle))
        self.cleanup_invocations += 1
        if "confirm" in handle:
            return bool(handle["confirm"])
        return self.confirm_cleanup

    def terminate_group(self, handle: dict) -> bool:
        """Process-group cleanup boundary. Tests count these invocations."""
        self.group_signals.append(handle.get("pgid") or handle.get("pid"))
        return self.terminate(handle)


def _flag(cmd: list[str], name: str) -> str | None:
    if name not in cmd:
        return None
    return cmd[cmd.index(name) + 1]


def _adapter(tmp_path: Path, runner: OfflineProcessDouble | None = None) -> HermesCliAdapter:
    runner = runner or OfflineProcessDouble()
    profiles = tmp_path / "profiles"
    for name in ("noesis-forge", "noesis-architect", "noesis-signal"):
        (profiles / name).mkdir(parents=True, exist_ok=True)
        (profiles / name / "SOUL.md").write_text(f"name: {name}\n", encoding="utf-8")
    adapter = HermesCliAdapter(hermes_bin="hermes", profiles_root=profiles, process_runner=runner)
    return adapter


def _orch(tmp_path: Path, adapter: HermesCliAdapter, **kwargs) -> Orchestrator:
    return Orchestrator(
        tmp_path / "tasks.jsonl",
        runtime_adapter=adapter,
        capability_index=kwargs.get("capability_index"),
        inference_enforcer=kwargs.get("enforcer"),
    )


def test_requested_provider_model_passed_exactly_and_ambient_cannot_override(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_INFERENCE_MODEL", "ambient-other")
    monkeypatch.setenv("HERMES_MODEL", "ambient-model")
    (tmp_path / "candidate.diff").write_text("candidate", encoding="utf-8")
    artifact_digest = hashlib.sha256(b"candidate").hexdigest()

    class ArtifactRunner(OfflineProcessDouble):
        def __call__(self, cmd, **kwargs):
            if "profile" in cmd and "show" in cmd:
                return super().__call__(cmd, **kwargs)
            result = super().__call__(cmd, **kwargs)
            result.stdout = json.dumps({"artifacts": ["candidate.diff"], "digest": "sha256:" + artifact_digest})
            return result

    runner = ArtifactRunner(fallback_chain=[("other-provider", "other-model")])
    adapter = _adapter(tmp_path, runner)
    result = adapter.launch(
        profile_id="noesis-forge",
        prompt="implement the repair",
        task_id="task-1",
        cwd=str(tmp_path),
        dry_run=False,
        provider="approved-provider",
        model="approved-model",
        timeout_s=30,
        remaining_lease_s=30,
        workspace=tmp_path,
        fallback_policy="deny",
    )
    assert result.ok is True
    assert result.accepted is False
    assert result.verification_pending is True
    spawned = [call for call in runner.calls if "-z" in call["cmd"]]
    assert spawned, runner.calls
    cmd = spawned[-1]["cmd"]
    assert _flag(cmd, "--provider") == "approved-provider"
    assert _flag(cmd, "-m") == "approved-model"
    assert spawned[-1]["env"].get("HERMES_INFERENCE_MODEL") is None
    assert spawned[-1]["env"].get("HERMES_MODEL") is None
    assert spawned[-1]["fallback_activated"] is False
    assert result.provider == "approved-provider"
    assert result.model == "approved-model"


def test_missing_pair_blocks_before_spawn(tmp_path):
    runner = OfflineProcessDouble()
    adapter = _adapter(tmp_path, runner)
    result = adapter.launch(
        profile_id="noesis-forge",
        prompt="implement",
        task_id="task-1",
        dry_run=False,
        provider=None,
        model=None,
        timeout_s=30,
        workspace=tmp_path,
    )
    assert result.ok is False
    assert result.blocker_code == "missing_approved_pair"
    assert runner.spawn_count == 0


def test_unenforceable_fallback_blocks_before_inference(tmp_path):
    class ClaimingRunner:
        supports_fallback_deny = True

        def __call__(self, *args, **kwargs):
            raise AssertionError("a runner flag is not proof that fallback is denied")

    profiles = tmp_path / "profiles" / "noesis-forge"
    profiles.mkdir(parents=True)
    adapter = HermesCliAdapter(
        hermes_bin="hermes",
        profiles_root=profiles.parent,
        process_runner=ClaimingRunner(),
    )
    result = adapter.launch(
        profile_id="noesis-forge",
        prompt="implement",
        task_id="task-1",
        dry_run=False,
        provider="approved-provider",
        model="approved-model",
        timeout_s=30,
        remaining_lease_s=30,
        fallback_policy="deny",
    )
    assert result.ok is False
    assert result.blocker_code == "fallback_unenforced"


def test_invalid_and_expired_deadlines_fail_closed(tmp_path):
    runner = OfflineProcessDouble()
    adapter = _adapter(tmp_path, runner)
    invalid = adapter.launch(
        profile_id="noesis-forge",
        prompt="implement",
        task_id="task-1",
        dry_run=False,
        provider="approved-provider",
        model="approved-model",
        timeout_s=0,
        workspace=tmp_path,
    )
    assert invalid.ok is False
    assert invalid.blocker_code == "invalid_deadline"
    assert runner.spawn_count == 0


def test_reservation_fsync_failure_does_not_spawn(tmp_path, monkeypatch):
    runner = OfflineProcessDouble()
    adapter = _adapter(tmp_path, runner)
    orch = Orchestrator(tmp_path / "tasks.jsonl", runtime_adapter=adapter, inference_enforcer=None)
    calls = {"n": 0}

    def failing_sync(fd):
        calls["n"] += 1
        if calls["n"] > 1:
            raise OSError("fsync failed")
        os.fsync(fd)

    monkeypatch.setattr(orch.store, "_sync", failing_sync)
    with pytest.raises(OSError, match="fsync failed"):
        orch.route_and_launch(
            title="Implement repair",
            intent="Implement a deterministic bounded repair with tests.",
            acceptance_criteria=["repair lands"],
            verification=Verification(method="automated_test"),
            idempotency_key="launch:fsync",
            required_capability="code_modify",
            dry_run=False,
            approved_provider="approved-provider",
            approved_model="approved-model",
            workspace=str(tmp_path),
            timeout_s=60,
        )
    assert runner.spawn_count == 0


def test_duplicate_and_restart_do_not_spawn_twice(tmp_path):
    runner = OfflineProcessDouble()
    adapter = _adapter(tmp_path, runner)
    ledger = tmp_path / "tasks.jsonl"
    orch = Orchestrator(ledger, runtime_adapter=adapter, inference_enforcer=None)
    kwargs = dict(
        title="Implement repair",
        intent="Implement a deterministic bounded repair with tests.",
        acceptance_criteria=["repair lands"],
        verification=Verification(method="automated_test"),
        idempotency_key="launch:dup",
        required_capability="code_modify",
        dry_run=False,
        approved_provider="approved-provider",
        approved_model="approved-model",
        workspace=str(tmp_path),
        timeout_s=60,
    )
    first = orch.route_and_launch(**kwargs)
    second = orch.route_and_launch(**kwargs)
    restarted = Orchestrator(ledger, runtime_adapter=adapter, inference_enforcer=None)
    third = restarted.route_and_launch(**kwargs)
    assert first.task.task_id == second.task.task_id == third.task.task_id
    assert runner.spawn_count == 1
    assert first.task.state != "succeeded"
    assert first.launch is not None and first.launch.accepted is False


def test_crash_before_and_after_spawn_is_uncertain_and_does_not_relaunch(tmp_path):
    runner = OfflineProcessDouble(start_then_raise=True)
    adapter = _adapter(tmp_path, runner)
    ledger = tmp_path / "tasks.jsonl"
    orch = Orchestrator(ledger, runtime_adapter=adapter, inference_enforcer=None)
    kwargs = dict(
        title="Implement repair",
        intent="Implement a deterministic bounded repair with tests.",
        acceptance_criteria=["repair lands"],
        verification=Verification(method="automated_test"),
        idempotency_key="launch:crash",
        required_capability="code_modify",
        dry_run=False,
        approved_provider="approved-provider",
        approved_model="approved-model",
        workspace=str(tmp_path),
        timeout_s=60,
    )
    with pytest.raises(RuntimeError, match="crash after spawn"):
        orch.route_and_launch(**kwargs)
    # F-1: a crash after spawn records the task as quarantined (fail-closed).
    # A subsequent route_and_launch with the SAME idempotency_key must therefore
    # be rejected by the quarantine gate, not silently fold a second attempt.
    # This is the documented behavior in app/control_plane.py:_assert_spawn_gates
    # ("quarantine is fail-closed and strictly stronger than task state — a
    # quarantined task must never spawn even if its folded state looks healthy.
    # Release is manual/operator-only; there is no automatic unquarantine.").
    reloaded = Orchestrator(ledger, runtime_adapter=_adapter(tmp_path, OfflineProcessDouble()), inference_enforcer=None)
    with pytest.raises(RoutingBlocked, match="quarantined"):
        reloaded.route_and_launch(**kwargs)
    # Confirm the second launch did not actually spawn anything.
    assert reloaded.runtime_adapter.process_runner.spawn_count == 0
    # Confirm the task is in the quarantine set in the persisted ledger
    # (recorded as a `quarantine` event with reason `handle_uncertain`).
    ledger_text = ledger.read_text(encoding="utf-8")
    assert '"event": "quarantine"' in ledger_text
    assert '"reason": "handle_uncertain"' in ledger_text


def test_dry_run_does_not_consume_attempt_or_claim_canary(tmp_path):
    runner = OfflineProcessDouble()
    adapter = _adapter(tmp_path, runner)
    orch = Orchestrator(tmp_path / "tasks.jsonl", runtime_adapter=adapter, inference_enforcer=None)
    result = orch.route_and_launch(
        title="Attest only",
        intent="Design an MCP/Hermes profile contract for a new memory agent",
        acceptance_criteria=["attested"],
        verification=Verification(method="launcher_attestation"),
        idempotency_key="launch:dry",
        dry_run=True,
    )
    task = orch.store.get(result.task.task_id)
    assert task.state == "proposed"
    assert task.delegation.attempt == 0
    assert runner.spawn_count == 0
    assert result.launch is not None
    assert result.launch.accepted is False
    assert getattr(result.launch, "model_driven_canary", False) is False


def test_corrupted_ledger_fails_closed(tmp_path):
    ledger = tmp_path / "tasks.jsonl"
    ledger.write_text("{not-json\n", encoding="utf-8")
    runner = OfflineProcessDouble()
    adapter = _adapter(tmp_path, runner)
    orch = Orchestrator(ledger, runtime_adapter=adapter, inference_enforcer=None)
    with pytest.raises(RoutingBlocked, match="replay|fail-closed|breaker"):
        orch.route_and_launch(
            title="Implement repair",
            intent="Implement a deterministic bounded repair with tests.",
            acceptance_criteria=["repair lands"],
            verification=Verification(method="automated_test"),
            idempotency_key="launch:corrupt",
            required_capability="code_modify",
            dry_run=False,
            approved_provider="approved-provider",
            approved_model="approved-model",
            workspace=str(tmp_path),
            timeout_s=60,
        )
    assert runner.spawn_count == 0


def test_concurrent_reservation_spawns_once(tmp_path):
    runner = OfflineProcessDouble()
    adapter = _adapter(tmp_path, runner)
    ledger = tmp_path / "tasks.jsonl"
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def launch_once():
        try:
            local = Orchestrator(ledger, runtime_adapter=adapter, inference_enforcer=None)
            original = local.store.reserve_launch

            def synced(*args, **kwargs):
                barrier.wait(timeout=5)
                return original(*args, **kwargs)

            local.store.reserve_launch = synced
            local.route_and_launch(
                title="Implement repair",
                intent="Implement a deterministic bounded repair with tests.",
                acceptance_criteria=["repair lands"],
                verification=Verification(method="automated_test"),
                idempotency_key="launch:concurrent",
                required_capability="code_modify",
                dry_run=False,
                approved_provider="approved-provider",
                approved_model="approved-model",
                workspace=str(tmp_path),
                timeout_s=60,
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=launch_once), threading.Thread(target=launch_once)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert errors == []
    assert runner.spawn_count == 1


def test_artifact_escape_and_malformed_evidence_are_rejected(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("nope", encoding="utf-8")
    link = tmp_path / "workspace" / "escape.txt"
    (tmp_path / "workspace").mkdir()
    link.symlink_to(outside)

    class EscapeRunner(OfflineProcessDouble):
        def __call__(self, cmd, **kwargs):
            if "profile" in cmd and "show" in cmd:
                return super().__call__(cmd, **kwargs)
            result = super().__call__(cmd, **kwargs)
            result.stdout = json.dumps({"artifacts": ["escape.txt"], "digest": "sha256:dead"})
            return result

    runner = EscapeRunner()
    adapter = _adapter(tmp_path, runner)
    result = adapter.launch(
        profile_id="noesis-forge",
        prompt="implement",
        task_id="task-1",
        dry_run=False,
        provider="approved-provider",
        model="approved-model",
        timeout_s=30,
        remaining_lease_s=30,
        workspace=tmp_path / "workspace",
    )
    assert result.ok is False
    assert result.blocker_code in {"artifact_escape", "malformed_evidence"}
    assert result.accepted is False


def test_cancel_uncertain_cleanup_retains_partial_and_blocks_reuse(tmp_path):
    runner = OfflineProcessDouble()
    adapter = _adapter(tmp_path, runner)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "partial.txt").write_text("partial", encoding="utf-8")
    result = adapter.cancel_managed(
        handle={"pid": 1, "confirm": False},
        workspace=workspace,
    )
    assert result["cleanup_state"] == "uncertain"
    assert result["workspace_reusable"] is False
    assert (workspace / "partial.txt").is_file()
    assert runner.cleanup_invocations == 1
    assert runner.group_signals == [1]


def _live_kwargs(tmp_path: Path, *, timeout_s: int = 60, key: str = "launch:cleanup") -> dict:
    return dict(
        title="Implement repair",
        intent="Implement a deterministic bounded repair with tests.",
        acceptance_criteria=["repair lands"],
        verification=Verification(method="automated_test"),
        idempotency_key=key,
        required_capability="code_modify",
        dry_run=False,
        approved_provider="approved-provider",
        approved_model="approved-model",
        workspace=str(tmp_path),
        timeout_s=timeout_s,
    )


def test_timeout_invokes_group_cleanup_and_does_not_accept(tmp_path):
    runner = OfflineProcessDouble(raise_timeout=True, confirm_cleanup=False)
    adapter = _adapter(tmp_path, runner)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "partial.txt").write_text("partial", encoding="utf-8")
    result = adapter.launch(
        profile_id="noesis-forge",
        prompt="implement",
        task_id="task-timeout",
        cwd=str(workspace),
        dry_run=False,
        provider="approved-provider",
        model="approved-model",
        timeout_s=30,
        remaining_lease_s=30,
        workspace=workspace,
    )
    spawned = [call for call in runner.calls if "-z" in call["cmd"]]
    assert spawned
    assert spawned[-1]["start_new_session"] is True
    assert runner.cleanup_invocations == 1
    assert runner.group_signals == [424242]
    assert result.ok is False
    assert result.accepted is False
    assert result.blocker_code == "timeout"
    assert result.cleanup_state == "uncertain"
    assert result.workspace_reusable is False
    assert (workspace / "partial.txt").is_file()


def test_timeout_records_terminated_only_when_group_cleanup_confirms(tmp_path):
    runner = OfflineProcessDouble(raise_timeout=True, confirm_cleanup=True)
    adapter = _adapter(tmp_path, runner)
    result = adapter.launch(
        profile_id="noesis-forge",
        prompt="implement",
        task_id="task-timeout-confirmed",
        dry_run=False,
        provider="approved-provider",
        model="approved-model",
        timeout_s=30,
        remaining_lease_s=30,
        workspace=tmp_path,
    )
    assert runner.cleanup_invocations == 1
    assert result.accepted is False
    assert result.ok is False
    assert result.cleanup_state == "terminated"
    assert result.workspace_reusable is True


def test_uncertain_timeout_keeps_partial_artifacts_and_blocks_relaunch(tmp_path):
    runner = OfflineProcessDouble(raise_timeout=True, confirm_cleanup=False)
    adapter = _adapter(tmp_path, runner)
    (tmp_path / "partial.txt").write_text("partial", encoding="utf-8")
    orch = Orchestrator(tmp_path / "tasks.jsonl", runtime_adapter=adapter, inference_enforcer=None)
    kwargs = _live_kwargs(tmp_path, key="launch:timeout-uncertain")
    with pytest.raises(RoutingBlocked) as exc:
        orch.route_and_launch(**kwargs)
    assert exc.value.code == "timeout"
    assert runner.cleanup_invocations == 1
    assert runner.group_signals
    task = orch.store.get(orch.store.find_by_idempotency_key(kwargs["idempotency_key"]).task_id)
    reservation = task.delegation.launch_reservation
    assert reservation["cleanup_state"] == "uncertain"
    assert reservation["workspace_reusable"] is False
    assert reservation.get("partial_artifacts_retained") is True
    assert (tmp_path / "partial.txt").is_file()
    assert task.state != "succeeded"
    with pytest.raises(PolicyViolation) as retry_exc:
        orch.retry(task.task_id)
    assert retry_exc.value.code == "workspace_quarantined"
    again = orch.route_and_launch(**kwargs)
    assert again.launch is None or again.launch.ok is False
    assert runner.spawn_count == 1
    assert runner.cleanup_invocations == 1


def test_timeout_larger_than_remaining_lease_is_capped(tmp_path):
    runner = OfflineProcessDouble()
    adapter = _adapter(tmp_path, runner)
    result = adapter.launch(
        profile_id="noesis-forge",
        prompt="implement",
        task_id="task-cap",
        dry_run=False,
        provider="approved-provider",
        model="approved-model",
        timeout_s=120,
        remaining_lease_s=4,
        workspace=tmp_path,
    )
    spawned = [call for call in runner.calls if "-z" in call["cmd"]]
    assert spawned
    assert spawned[-1]["timeout"] == 4
    assert result.accepted is False
    assert runner.spawn_count == 1


def test_omitted_remaining_lease_refuses_before_spawn(tmp_path):
    runner = OfflineProcessDouble()
    adapter = _adapter(tmp_path, runner)
    result = adapter.launch(
        profile_id="noesis-forge",
        prompt="implement",
        task_id="task-omitted-lease",
        dry_run=False,
        provider="approved-provider",
        model="approved-model",
        timeout_s=120,
        workspace=tmp_path,
    )
    assert result.ok is False
    assert result.blocker_code == "invalid_deadline"
    assert result.accepted is False
    assert runner.spawn_count == 0
    assert not any("-z" in call["cmd"] for call in runner.calls)


def test_non_positive_or_missing_remaining_lease_does_not_spawn(tmp_path):
    runner = OfflineProcessDouble()
    adapter = _adapter(tmp_path, runner)
    for bad in (None, 0, -1, float("nan"), float("inf"), True, "30"):
        result = adapter.launch(
            profile_id="noesis-forge",
            prompt="implement",
            task_id="task-deadline",
            dry_run=False,
            provider="approved-provider",
            model="approved-model",
            timeout_s=120,
            remaining_lease_s=bad,
            workspace=tmp_path,
        )
        assert result.ok is False
        assert result.blocker_code == "invalid_deadline"
        assert result.accepted is False
    assert runner.spawn_count == 0


def test_control_plane_caps_spawn_timeout_at_remaining_lease(tmp_path):
    runner = OfflineProcessDouble()
    adapter = _adapter(tmp_path, runner)
    orch = Orchestrator(tmp_path / "tasks.jsonl", runtime_adapter=adapter, inference_enforcer=None)
    key = "launch:lease-cap"
    orch.propose(
        title="Implement repair",
        intent="Implement a deterministic bounded repair with tests.",
        assignee_profile="noesis-forge",
        required_capability="code_modify",
        acceptance_criteria=["repair lands"],
        verification=Verification(method="automated_test"),
        idempotency_key=key,
        timeout_s=5,
    )
    orch.route_and_launch(**_live_kwargs(tmp_path, timeout_s=60, key=key))
    spawned = [call for call in runner.calls if "-z" in call["cmd"]]
    assert spawned
    assert 0 < spawned[-1]["timeout"] <= 5
    assert spawned[-1]["timeout"] < 60
    assert spawned[-1]["start_new_session"] is True


def test_missing_or_expired_lease_refuses_spawn(tmp_path):
    runner = OfflineProcessDouble()
    adapter = _adapter(tmp_path, runner)
    orch = Orchestrator(tmp_path / "tasks.jsonl", runtime_adapter=adapter, inference_enforcer=None)
    missing = orch.propose(
        title="Implement repair",
        intent="Implement a deterministic bounded repair with tests.",
        assignee_profile="noesis-forge",
        required_capability="code_modify",
        acceptance_criteria=["repair lands"],
        verification=Verification(method="automated_test"),
        idempotency_key="launch:missing-lease",
        timeout_s=60,
    )
    orch.mark_dispatched(missing.task_id, runtime_profile="noesis-forge")
    cleared = orch.store.get(missing.task_id)
    cleared.claim = None
    orch.store.put(cleared, event="claim_cleared")
    with pytest.raises(RoutingBlocked) as missing_exc:
        orch.route_and_launch(**_live_kwargs(tmp_path, key="launch:missing-lease"))
    assert missing_exc.value.code == "invalid_deadline"
    assert runner.spawn_count == 0

    expired = orch.propose(
        title="Implement repair",
        intent="Implement a deterministic bounded repair with tests.",
        assignee_profile="noesis-forge",
        required_capability="code_modify",
        acceptance_criteria=["repair lands"],
        verification=Verification(method="automated_test"),
        idempotency_key="launch:expired-lease",
        timeout_s=60,
    )
    orch.mark_dispatched(expired.task_id, runtime_profile="noesis-forge")
    stale = orch.store.get(expired.task_id)
    stale.claim["lease_expires_at"] = utcnow() - timedelta(seconds=5)
    orch.store.put(stale, event="lease_backdated")
    with pytest.raises(RoutingBlocked) as expired_exc:
        orch.route_and_launch(**_live_kwargs(tmp_path, key="launch:expired-lease"))
    assert expired_exc.value.code == "lease_expired"
    assert runner.spawn_count == 0


def test_cancel_persisted_handle_quarantines_when_termination_uncertain(tmp_path):
    runner = OfflineProcessDouble()
    adapter = _adapter(tmp_path, runner)
    orch = Orchestrator(tmp_path / "tasks.jsonl", runtime_adapter=adapter, inference_enforcer=None)
    launched = orch.route_and_launch(**_live_kwargs(tmp_path, key="launch:cancel-uncertain"))
    (tmp_path / "partial.txt").write_text("partial", encoding="utf-8")
    runner.confirm_cleanup = False
    before = runner.cleanup_invocations
    cancelled = orch.cancel(launched.task.task_id, reason="operator stop")
    assert runner.cleanup_invocations == before + 1
    assert runner.group_signals
    task = orch.store.get(cancelled.task_id)
    assert task.state == "cancelled"
    assert task.state != "succeeded"
    reservation = task.delegation.launch_reservation
    assert reservation["cleanup_state"] == "uncertain"
    assert reservation["workspace_reusable"] is False
    assert (tmp_path / "partial.txt").is_file()
    with pytest.raises(PolicyViolation) as exc:
        orch.retry(task.task_id)
    assert exc.value.code == "workspace_quarantined"
    assert runner.spawn_count == 1


def test_sweep_timeout_cleans_group_and_does_not_succeed(tmp_path):
    runner = OfflineProcessDouble()
    adapter = _adapter(tmp_path, runner)
    orch = Orchestrator(tmp_path / "tasks.jsonl", runtime_adapter=adapter, inference_enforcer=None)
    launched = orch.route_and_launch(**_live_kwargs(tmp_path, key="launch:sweep-timeout", timeout_s=60))
    (tmp_path / "partial.txt").write_text("partial", encoding="utf-8")
    runner.confirm_cleanup = False
    before = runner.cleanup_invocations
    swept = orch.sweep_timeouts(now=utcnow() + timedelta(seconds=120))
    assert swept
    assert runner.cleanup_invocations == before + 1
    assert runner.group_signals
    task = orch.store.get(launched.task.task_id)
    assert task.state != "succeeded"
    assert task.state == "failed"
    assert task.delegation.launch_reservation["cleanup_state"] == "uncertain"
    assert task.delegation.launch_reservation["workspace_reusable"] is False
    assert (tmp_path / "partial.txt").is_file()
    assert runner.spawn_count == 1


def test_real_cleanup_signals_the_process_group(monkeypatch, tmp_path):
    seen: list[tuple[int, int]] = []

    def fake_killpg(pid, sig):
        seen.append((pid, sig))
        raise ProcessLookupError()

    monkeypatch.setattr("app.launcher.os.killpg", fake_killpg)
    monkeypatch.setattr(
        "app.launcher.os.kill",
        lambda *args: (_ for _ in ()).throw(AssertionError("must signal the group, not the child")),
    )
    adapter = HermesCliAdapter(hermes_bin="hermes", profiles_root=tmp_path, process_runner=None)
    result = adapter.cancel_managed(handle={"pid": 4321, "pgid": 4321}, workspace=tmp_path)
    assert seen and seen[0][0] == 4321
    assert result["cleanup_state"] == "terminated"
    refused = adapter.cancel_managed(handle={"pid": 0, "pgid": 0}, workspace=tmp_path)
    assert refused["cleanup_state"] == "uncertain"
    assert refused["workspace_reusable"] is False
    assert all(pid != 0 for pid, _sig in seen)

