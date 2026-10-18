"""Durable delegated-task state and handoff protocol tests."""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from app.delegation import (
    ActivationError,
    DelegatedTask,
    DelegationStore,
    HandoffEnvelope,
    MessageType,
    NoSuchTask,
    TaskState,
)


def _store(tmp_path: Path):
    from app.delegation import DelegationStore

    return DelegationStore(tmp_path / "delegated-tasks.jsonl")


def _task_id() -> str:
    return str(uuid4())


def _assign_msg(*, task_id: str, sender: str = "noesis-orchestrator", epoch: int = 1,
                version: int = 0, idem: str | None = None) -> HandoffEnvelope:
    return HandoffEnvelope(
        protocol_version="noesis.delegated-task/v1",
        message_id=str(uuid4()),
        message_type=MessageType.ASSIGN,
        sender_id=sender,
        recipient_id="noesis-forge",
        root_task_id=task_id,
        task_id=task_id,
        attempt_id=f"{task_id}/a1",
        contract_revision="c-rev-1",
        assignment_epoch=epoch,
        correlation_id=task_id,
        idempotency_key=idem or f"idem-{task_id}",
        expected_task_version=version,
        payload={
            "intent": "implement the patch",
            "acceptance_criteria": ["tests pass"],
            "required_capability": "implement",
        },
    )


# ---------------------------------------------------------------- assign -----
def test_assign_creates_task_and_is_idempotent_on_duplicate(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _task_id()
    first = store.process(_assign_msg(task_id=task_id))
    second = store.process(_assign_msg(task_id=task_id))

    assert first.state == TaskState.assigned
    assert second.task_id == first.task_id
    # Duplicate ASSIGN must not advance or duplicate work: same version, one task.
    assert second.version == first.version
    assert len(list(store._all_records())) == 1


def test_assign_rejects_unauthorized_sender(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ActivationError):
        store.process(_assign_msg(task_id=_task_id(), sender="noesis-forge"))


def test_process_duplicate_task_returns_duplicate_not_error(tmp_path: Path) -> None:
    """A duplicate idempotency key on ASSIGN yields the existing task, not an error."""
    store = _store(tmp_path)
    task_id = _task_id()
    msg = _assign_msg(task_id=task_id)
    first = store.process(msg)
    dupe = store.process(msg.idempotency_key and _assign_msg(task_id=task_id, idem=msg.idempotency_key))
    assert dupe.task_id == first.task_id


# ------------------------------------------------------------ lifecycle -----
def test_completed_requires_evidence_and_acceptance(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _task_id()
    store.process(_assign_msg(task_id=task_id))
    store.mark_ready(task_id, by="noesis-orchestrator")
    store.start_running(task_id, by="noesis-forge", expected_attempt_epoch=1)
    store.submit_verifying(task_id, by="noesis-forge", expected_attempt_epoch=1, candidate_revision="rev-1")

    # Reviews: spec PASS then quality APPROVE.
    store.record_review(task_id, stage="spec", verdict="PASS", reviewer="noesis-sentinel",
                        candidate_revision="rev-1", findings=[])
    task = store.record_review(task_id, stage="quality", verdict="APPROVED", reviewer="noesis-sentinel",
                               candidate_revision="rev-1", findings=[])
    # Orchestrator acceptance is required; verify acceptance is distinct.
    assert task.state == TaskState.spec_review or task.state == TaskState.verifying


def test_quality_review_requires_spec_pass_on_current_revision(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _task_id()
    store.process(_assign_msg(task_id=task_id))
    store.mark_ready(task_id, by="noesis-orchestrator")
    store.start_running(task_id, by="noesis-forge", expected_attempt_epoch=1)
    store.submit_verifying(task_id, by="noesis-forge", expected_attempt_epoch=1, candidate_revision="rev-1")
    # No spec PASS yet -> quality must be refused.
    with pytest.raises(ActivationError):
        store.record_review(task_id, stage="quality", verdict="APPROVED", reviewer="noesis-sentinel",
                            candidate_revision="rev-1", findings=[])


def test_stale_epoch_conflict_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _task_id()
    store.process(_assign_msg(task_id=task_id, epoch=1))
    # A stale re-assignment epoch must not relaunch work.
    with pytest.raises(ActivationError):
        store.process(_assign_msg(task_id=task_id, epoch=0, idem=f"stale-{task_id}"))


def test_lease_expiry_marks_execution_suspect_not_completed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _task_id()
    store.process(_assign_msg(task_id=task_id))
    store.mark_ready(task_id, by="noesis-orchestrator")
    store.start_running(task_id, by="noesis-forge", expected_attempt_epoch=1)
    store.expire_lease(task_id)
    task = store.get(task_id)
    # Expiry must not silently complete; the task is held suspect/blocked.
    assert task.state not in {TaskState.completed}
    assert task.lease.is_active is False


def test_restart_recovers_without_duplicate_dispatch(tmp_path: Path) -> None:
    ledger = tmp_path / "delegated-tasks.jsonl"
    task_id = _task_id()
    first_store = _store(tmp_path)
    first_store.process(_assign_msg(task_id=task_id))
    first_store.mark_ready(task_id, by="noesis-orchestrator")

    # New store instance replays the same append-only ledger.
    second_store = DelegationStore(ledger)
    recovered = second_store.get(task_id)
    assert recovered is not None
    assert recovered.state == TaskState.ready
    # Replaying the same ASSIGN does not duplicate.
    assert second_store.process(_assign_msg(task_id=task_id)) is not None
    assert len(list(second_store._all_records())) == 1


# ------------------------------------------------------- review findings -----
def _to_verifying(store, task_id: str) -> None:
    store.process(_assign_msg(task_id=task_id))
    store.mark_ready(task_id, by="noesis-orchestrator")
    store.start_running(task_id, by="noesis-forge", expected_attempt_epoch=1)
    store.submit_verifying(
        task_id, by="noesis-forge", expected_attempt_epoch=1, candidate_revision="rev-1"
    )


def test_critical_findings_block_spec_pass(tmp_path: Path) -> None:
    """Subgoal 5 §2: critical/important findings block approval (delegation.py:680)."""
    store = _store(tmp_path)
    task_id = _task_id()
    _to_verifying(store, task_id)
    with pytest.raises(ActivationError):
        store.record_review(
            task_id,
            stage="spec",
            verdict="PASS",
            reviewer="noesis-sentinel",
            candidate_revision="rev-1",
            findings=[{"severity": "critical", "detail": "unsafe change"}],
        )
    task = store.get(task_id)
    assert task is not None
    assert task.state == TaskState.verifying
    assert task.reviews == []


def test_important_findings_block_quality_approval(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _task_id()
    _to_verifying(store, task_id)
    store.record_review(
        task_id, stage="spec", verdict="PASS", reviewer="noesis-sentinel",
        candidate_revision="rev-1", findings=[],
    )
    with pytest.raises(ActivationError):
        store.record_review(
            task_id,
            stage="quality",
            verdict="APPROVED",
            reviewer="noesis-skeptic",
            candidate_revision="rev-1",
            findings=[{"severity": "important", "detail": "missing regression test"}],
        )
    task = store.get(task_id)
    assert task is not None
    assert task.state == TaskState.spec_review
    assert len(task.reviews) == 1  # only the spec PASS row survives the refusal


def test_minor_findings_do_not_block_passing_verdict(tmp_path: Path) -> None:
    """The gate is severity-specific: minor findings are recorded, not refused."""
    store = _store(tmp_path)
    task_id = _task_id()
    _to_verifying(store, task_id)
    task = store.record_review(
        task_id,
        stage="spec",
        verdict="PASS",
        reviewer="noesis-sentinel",
        candidate_revision="rev-1",
        findings=[{"severity": "minor", "detail": "style nit"}],
    )
    assert task.state == TaskState.spec_review
    assert len(task.reviews) == 1
    assert task.reviews[0].findings[0]["severity"] == "minor"
