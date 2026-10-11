"""Regression tests for durable emergency-stop / resume behavior.

Defect (deployment-verification-2026-10-06): an operator emergency stop was
held only in memory, so a process restart silently cleared the stop and
reopened dispatch. These tests pin the fixed behavior.
"""

import pytest

from app.control_plane import Orchestrator
from app.models import Verification
from app.policy import PolicyViolation
from app.store import BREAKER_RESET_EVENT, BREAKER_TRIP_EVENT


def _make_task(orch, key, profile="noesis-forge", capability="code_gen"):
    return orch.propose(
        title=f"breaker persistence test {key}",
        intent="bounded synthetic task",
        assignee_profile=profile,
        required_capability=capability,
        acceptance_criteria=["done"],
        verification=Verification(method="manual"),
        idempotency_key=key,
        risk_tier="r1",
    )


def _drive_to_queued(orch, task):
    orch.approve(task.task_id, operator="main-hermes")
    return orch.enqueue(task.task_id)


def test_stop_persists_across_restart(tmp_path):
    ledger = tmp_path / "tasks.jsonl"
    orch = Orchestrator(ledger)
    orch.emergency_stop(operator="elvis", reason="maintenance window")

    orch2 = Orchestrator(ledger)
    assert orch2.breaker.manually_tripped is True
    assert "maintenance window" in (orch2.breaker.reason or "")

    task = _make_task(orch2, "stop-restart-001")
    _drive_to_queued(orch2, task)
    with pytest.raises(PolicyViolation):
        orch2.claim(task.task_id, claimed_by="noesis-forge")


def test_stop_recorded_with_identity_reason_timestamp(tmp_path):
    ledger = tmp_path / "tasks.jsonl"
    orch = Orchestrator(ledger)
    orch.emergency_stop(operator="elvis", reason="incident")
    event = orch.store.latest_breaker_event()
    assert event["event"] == BREAKER_TRIP_EVENT
    assert event["operator"] == "elvis"
    assert event["reason"] == "incident"
    assert event["recorded_at"]
    assert event["schema_version"]


def test_resume_persists_and_restores_dispatch(tmp_path):
    ledger = tmp_path / "tasks.jsonl"
    orch = Orchestrator(ledger)
    orch.emergency_stop(operator="elvis", reason="stop")
    orch.resume(operator="elvis")

    orch2 = Orchestrator(ledger)
    assert orch2.breaker.manually_tripped is False
    task = _make_task(orch2, "resume-restart-001")
    _drive_to_queued(orch2, task)
    assert orch2.claim(task.task_id, claimed_by="noesis-forge").state == "claimed"

    event = orch2.store.latest_breaker_event()
    assert event["event"] == BREAKER_RESET_EVENT
    assert event["operator"] == "elvis"


def test_repeated_stop_resume_last_event_wins(tmp_path):
    ledger = tmp_path / "tasks.jsonl"
    orch = Orchestrator(ledger)
    orch.emergency_stop(operator="elvis", reason="stop-1")
    orch.resume(operator="elvis")
    orch.emergency_stop(operator="elvis", reason="stop-2")

    orch2 = Orchestrator(ledger)
    assert orch2.breaker.manually_tripped is True
    assert "stop-2" in orch2.breaker.reason

    orch2.resume(operator="elvis")
    orch3 = Orchestrator(ledger)
    assert orch3.breaker.manually_tripped is False


def test_malformed_ledger_fails_closed(tmp_path):
    ledger = tmp_path / "tasks.jsonl"
    orch = Orchestrator(ledger)
    _make_task(orch, "pre-corruption-001")
    with ledger.open("a") as fh:
        fh.write('{"event": "circuit_breaker_tripped", "broken": \n')  # truncated JSON

    orch2 = Orchestrator(ledger)
    assert orch2.store.replay_error is not None
    assert orch2.breaker.manually_tripped is True
    assert "fail-closed" in orch2.breaker.reason

    with pytest.raises(PolicyViolation, match="replay"):
        _make_task(orch2, "post-corruption-001")


def test_stop_write_failure_raises_and_fails_closed(tmp_path, monkeypatch):
    ledger = tmp_path / "tasks.jsonl"
    orch = Orchestrator(ledger)

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(orch.store, "append_control_event", boom)
    with pytest.raises(OSError):
        orch.emergency_stop(operator="elvis", reason="should not persist")
    # Not acknowledged as persisted, but dispatch must fail closed.
    assert orch.breaker.manually_tripped is True
    task = _make_task(orch, "write-failure-001")
    _drive_to_queued(orch, task)
    with pytest.raises(PolicyViolation):
        orch.claim(task.task_id, claimed_by="noesis-forge")


def test_stop_requires_operator_identity(tmp_path):
    ledger = tmp_path / "tasks.jsonl"
    orch = Orchestrator(ledger)
    with pytest.raises(ValueError):
        orch.emergency_stop(operator="  ", reason="x")
    with pytest.raises(ValueError):
        orch.resume(operator="")
    assert orch.breaker.manually_tripped is False


def test_stop_blocks_new_dispatch_but_not_running_work(tmp_path):
    ledger = tmp_path / "tasks.jsonl"
    orch = Orchestrator(ledger)
    task = _make_task(orch, "in-flight-001")
    _drive_to_queued(orch, task)
    orch.claim(task.task_id, claimed_by="noesis-forge")
    orch.start(task.task_id)

    orch.emergency_stop(operator="elvis", reason="halt new work")
    # Already-running work completes normally (left to its lease).
    orch.submit_handoff(
        task.task_id,
        handoff={
            "summary": "done",
            "artifacts": ["marker.txt"],
            "verification_result": {"passed": True, "evidence": "checked"},
        },
    )
    assert orch.succeed(task.task_id).state == "succeeded"

    # New dispatch remains blocked.
    task2 = _make_task(orch, "in-flight-002")
    _drive_to_queued(orch, task2)
    with pytest.raises(PolicyViolation):
        orch.claim(task2.task_id, claimed_by="noesis-forge")
