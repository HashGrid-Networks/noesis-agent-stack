"""Failure-injection and integrity tests for the durable delegation store.

These exercise the real DelegationStore and transition logic (no mocks for the
store), injecting duplicate, stale, out-of-order, and unauthorized events to
prove the acceptance criteria: required transitions cannot be bypassed,
duplicate/stale events cannot corrupt authoritative state, reviews bind to the
current candidate, completion requires explicit orchestrator acceptance, and
recovery does not blindly relaunch uncertain execution.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from app.delegation import (
    ActivationError,
    DelegationStore,
    HandoffEnvelope,
    MessageType,
    TaskState,
    ORCHESTRATOR_ID,
)


def _store(tmp_path: Path) -> DelegationStore:
    return DelegationStore(tmp_path / "delegated-tasks.jsonl")


def _task_id() -> str:
    return str(uuid4())


def _assign(*, task_id: str, sender: str = "noesis-orchestrator", epoch: int = 1,
            idem: str | None = None, protocol: str = "noesis.delegated-task/v1") -> HandoffEnvelope:
    return HandoffEnvelope(
        protocol_version=protocol,
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
        expected_task_version=0,
        payload={
            "intent": "implement the patch",
            "acceptance_criteria": ["tests pass"],
            "required_capability": "implement",
        },
    )


def _to_verifying(store: DelegationStore, task_id: str, *, rev: str = "rev-1") -> None:
    store.mark_ready(task_id, by="noesis-orchestrator")
    store.start_running(task_id, by="noesis-forge", expected_attempt_epoch=1)
    store.submit_verifying(task_id, by="noesis-forge", expected_attempt_epoch=1, candidate_revision=rev)


def _to_quality(store: DelegationStore, task_id: str, *, rev: str = "rev-1") -> None:
    _to_verifying(store, task_id, rev=rev)
    store.record_review(task_id, stage="spec", verdict="PASS", reviewer="noesis-sentinel",
                        candidate_revision=rev, findings=[])


# ------------------------------------------------------- duplicate events --
def test_duplicate_accept_is_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _task_id()
    store.process(_assign(task_id=task_id))
    _to_quality(store, task_id)
    store.record_review(task_id, stage="quality", verdict="APPROVED", reviewer="noesis-sentinel",
                        candidate_revision="rev-1", findings=[])
    first = store.accept(task_id, by="noesis-orchestrator")
    second = store.accept(task_id, by="noesis-orchestrator")

    assert first.state is TaskState.completed
    assert second.state is TaskState.completed
    assert second.accepted is True
    # No version corruption from the duplicate.
    assert second.version == first.version


def test_duplicate_result_same_revision_is_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _task_id()
    store.process(_assign(task_id=task_id))
    store.mark_ready(task_id, by="noesis-orchestrator")
    store.start_running(task_id, by="noesis-forge", expected_attempt_epoch=1)
    first = store.submit_verifying(task_id, by="noesis-forge", expected_attempt_epoch=1, candidate_revision="rev-1")
    second = store.submit_verifying(task_id, by="noesis-forge", expected_attempt_epoch=1, candidate_revision="rev-1")

    assert second.state is TaskState.verifying
    assert second.candidate_revision == "rev-1"
    assert second.version == first.version


# ------------------------------------------------- candidate-change binding --
def test_candidate_change_invalidates_prior_reviews(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _task_id()
    store.process(_assign(task_id=task_id))
    _to_quality(store, task_id, rev="rev-1")
    store.record_review(task_id, stage="quality", verdict="APPROVED", reviewer="noesis-sentinel",
                        candidate_revision="rev-1", findings=[])

    # A new candidate revision is submitted after an orchestrator grant.
    current = store.get(task_id)
    granted = store.grant_new_attempt(
        task_id,
        by=ORCHESTRATOR_ID,
        expected_task_version=current.version,
        intended_candidate_revision="rev-2",
    )
    store.submit_verifying(
        task_id,
        by="noesis-forge",
        expected_attempt_epoch=granted.lease.attempt_epoch,
        candidate_revision="rev-2",
    )
    task = store.get(task_id)
    assert all(r.candidate_revision == "rev-2" for r in task.reviews)
    assert task.reviews == []

    # Quality on rev-2 must be refused until a fresh spec PASS on rev-2.
    with pytest.raises(ActivationError):
        store.record_review(task_id, stage="quality", verdict="APPROVED", reviewer="noesis-sentinel",
                            candidate_revision="rev-2", findings=[])


# ------------------------------------------------------- version conflicts --
def test_stale_expected_version_rejects_accept(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _task_id()
    store.process(_assign(task_id=task_id))
    _to_quality(store, task_id)
    store.record_review(task_id, stage="quality", verdict="APPROVED", reviewer="noesis-sentinel",
                        candidate_revision="rev-1", findings=[])
    current = store.get(task_id)

    with pytest.raises(ActivationError):
        store.accept(task_id, by="noesis-orchestrator", expected_task_version=current.version - 1)


def test_stale_expected_version_rejects_start(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _task_id()
    store.process(_assign(task_id=task_id))
    store.mark_ready(task_id, by="noesis-orchestrator")
    current = store.get(task_id)

    with pytest.raises(ActivationError):
        store.start_running(task_id, by="noesis-forge", expected_task_version=current.version - 1, expected_attempt_epoch=1)


# ------------------------------------------------------------- cancellation --
def test_cancel_prevents_new_work(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _task_id()
    store.process(_assign(task_id=task_id))
    store.mark_ready(task_id, by="noesis-orchestrator")
    store.cancel(task_id, by="noesis-orchestrator", reason="superseded")

    assert store.get(task_id).state is TaskState.cancelled
    with pytest.raises(ActivationError):
        store.start_running(task_id, by="noesis-forge", expected_attempt_epoch=1)
    with pytest.raises(ActivationError):
        store.accept(task_id, by="noesis-orchestrator")


def test_cancel_requires_orchestrator(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _task_id()
    store.process(_assign(task_id=task_id))
    with pytest.raises(ActivationError):
        store.cancel(task_id, by="noesis-forge", reason="rogue")


# ------------------------------------------------------- protocol / auth -----
def test_unsupported_protocol_version_fails_explicitly(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ActivationError):
        store.process(_assign(task_id=_task_id(), protocol="noesis.delegated-task/v0"))


def test_assignee_cannot_accept_own_result(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _task_id()
    store.process(_assign(task_id=task_id))
    _to_quality(store, task_id)
    store.record_review(task_id, stage="quality", verdict="APPROVED", reviewer="noesis-sentinel",
                        candidate_revision="rev-1", findings=[])
    with pytest.raises(ActivationError):
        store.accept(task_id, by="noesis-forge")


# ------------------------------------------------------- recovery / restart --
def test_restart_preserves_cancelled_state(tmp_path: Path) -> None:
    ledger = tmp_path / "delegated-tasks.jsonl"
    task_id = _task_id()
    first = DelegationStore(ledger)
    first.process(_assign(task_id=task_id))
    first.mark_ready(task_id, by="noesis-orchestrator")
    first.cancel(task_id, by="noesis-orchestrator", reason="superseded")

    second = DelegationStore(ledger)
    assert second.get(task_id).state is TaskState.cancelled
    # Replaying the same ASSIGN must not relaunch a cancelled task.
    assert second.process(_assign(task_id=task_id)) is not None
    assert second.get(task_id).state is TaskState.cancelled


# ------------------------------------------------- Subgoal 4 §3 crash windows --


def test_assignee_cannot_review_its_own_result_regardless_of_profile(tmp_path: Path) -> None:
    store = _store(tmp_path)
    tid = _task_id()
    assign = _assign(task_id=tid)
    store.process(replace(assign, recipient_id="noesis-substrate"))
    store.mark_ready(tid, by="noesis-orchestrator")
    store.start_running(tid, by="noesis-substrate", expected_attempt_epoch=1)
    store.submit_verifying(tid, by="noesis-substrate", expected_attempt_epoch=1, candidate_revision="rev-1")

    with pytest.raises(ActivationError, match="cannot review its own work"):
        store.record_review(
            tid, stage="spec", verdict="PASS", reviewer="noesis-substrate",
            candidate_revision="rev-1", findings=[],
        )


def test_restart_between_every_transition_preserves_lifecycle(tmp_path: Path) -> None:
    """Crash-window matrix: a controller restart after every single mutation.
    Each window must preserve durable state and never duplicate work."""
    ledger = tmp_path / "delegated-tasks.jsonl"
    tid = _task_id()

    store = DelegationStore(ledger)
    store.process(_assign(task_id=tid))
    store = DelegationStore(ledger)
    assert store.get(tid).state is TaskState.assigned

    store.mark_ready(tid, by="noesis-orchestrator")
    store = DelegationStore(ledger)
    assert store.get(tid).state is TaskState.ready

    store.start_running(tid, by="noesis-forge", expected_attempt_epoch=1)
    store = DelegationStore(ledger)
    assert store.get(tid).state is TaskState.running
    assert store.get(tid).lease is not None and store.get(tid).lease.is_active

    store.submit_verifying(tid, by="noesis-forge", expected_attempt_epoch=1, candidate_revision="rev-1")
    store = DelegationStore(ledger)
    assert store.get(tid).state is TaskState.verifying

    store.record_review(tid, stage="spec", verdict="PASS", reviewer="noesis-sentinel",
                        candidate_revision="rev-1", findings=[])
    store = DelegationStore(ledger)
    assert store.get(tid).state is TaskState.spec_review
    assert len(store.get(tid).reviews) == 1

    store.record_review(tid, stage="quality", verdict="APPROVED", reviewer="noesis-sentinel",
                        candidate_revision="rev-1", findings=[])
    store = DelegationStore(ledger)
    assert store.get(tid).state is TaskState.verifying

    store.accept(tid, by="noesis-orchestrator")
    store = DelegationStore(ledger)
    final = store.get(tid)
    assert final.state is TaskState.completed
    assert final.accepted is True
    # Exactly one task, one accepted completion, monotonic version across
    # the seven restarts — no duplicate work, no state corruption.
    assert final.task_id == tid
    assert final.version == 6  # ready, started, verifying, spec, quality, accepted"


def test_assignment_persisted_before_delivery_does_not_duplicate(tmp_path: Path) -> None:
    ledger = tmp_path / "delegated-tasks.jsonl"
    tid = _task_id()

    store = DelegationStore(ledger)
    store.process(_assign(task_id=tid, idem="idem-delivery"))
    # Crash after persistence, before downstream delivery/launch.
    store = DelegationStore(ledger)
    redelivered = store.process(_assign(task_id=tid, idem="idem-delivery"))
    assert redelivered.task_id == tid
    assert redelivered.state is TaskState.assigned
    # Still exactly one task record; nothing launched.
    store2 = DelegationStore(ledger)
    assert store2.get(tid).state is TaskState.assigned
    assert store2.get(tid).version == 0


def test_stale_start_after_restart_requires_ready_gate(tmp_path: Path) -> None:
    """Message delivered but the ready/ack transition was never persisted:
    a start attempt must fail closed, not skip the gate."""
    ledger = tmp_path / "delegated-tasks.jsonl"
    tid = _task_id()

    store = DelegationStore(ledger)
    store.process(_assign(task_id=tid))
    store = DelegationStore(ledger)

    with pytest.raises(ActivationError, match="not ready"):
        store.start_running(tid, by="noesis-forge", expected_attempt_epoch=1)
    assert store.get(tid).state is TaskState.assigned


def test_wrong_assignee_start_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    tid = _task_id()
    store.process(_assign(task_id=tid))  # recipient noesis-forge
    store.mark_ready(tid, by="noesis-orchestrator")

    with pytest.raises(ActivationError, match="only the assigned specialist"):
        store.start_running(tid, by="noesis-grid", expected_attempt_epoch=1)
    assert store.get(tid).state is TaskState.ready
    assert store.get(tid).lease is not None
    assert store.get(tid).lease.attempt_epoch == 1


def test_cancel_persisted_before_termination_confirmation_blocks_all_work(tmp_path: Path) -> None:
    ledger = tmp_path / "delegated-tasks.jsonl"
    tid = _task_id()

    store = DelegationStore(ledger)
    store.process(_assign(task_id=tid))
    store.mark_ready(tid, by="noesis-orchestrator")
    store.start_running(tid, by="noesis-forge", expected_attempt_epoch=1)
    store.cancel(tid, by="noesis-orchestrator", reason="operator stop")
    # Crash before termination confirmation / downstream cleanup.
    store = DelegationStore(ledger)
    task = store.get(tid)

    assert task.state is TaskState.cancelled
    assert task.lease is not None and not task.lease.is_active  # revoked
    with pytest.raises(ActivationError):
        store.start_running(tid, by="noesis-forge", expected_attempt_epoch=1)
    with pytest.raises(ActivationError):
        store.submit_verifying(tid, by="noesis-forge", expected_attempt_epoch=1, candidate_revision="rev-1")
    with pytest.raises(ActivationError):
        store.record_review(tid, stage="spec", verdict="PASS", reviewer="noesis-sentinel",
                            candidate_revision="rev-1", findings=[])
    with pytest.raises(ActivationError):
        store.accept(tid, by="noesis-orchestrator")


def test_accept_before_required_reviews_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    tid = _task_id()
    store.process(_assign(task_id=tid))
    _to_verifying(store, tid)

    # Out-of-order: backend success alone must never become acceptance.
    with pytest.raises(ActivationError, match="requires spec PASS and quality APPROVED"):
        store.accept(tid, by="noesis-orchestrator")
    assert store.get(tid).state is TaskState.verifying
    assert store.get(tid).accepted is False
