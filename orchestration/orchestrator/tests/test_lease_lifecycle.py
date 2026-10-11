"""Bounded lease-lifecycle tests (LEASE-LIFECYCLE-DESIGN.md, 2026-10-11)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.delegation import (
    ORCHESTRATOR_ID,
    PROTOCOL_VERSION,
    DelegationStore,
    HandoffEnvelope,
    MessageType,
    extract_from_delegation as delegation_extract,
    lease_renew as delegation_lease_renew,
)
from app.lease_lifecycle import (
    LeaseState,
    LeaseTransitionError,
    extract_from_delegation,
    grant_lease,
    lease_renew,
    load_attempt_epoch,
    release_lease,
    start_running,
)


def _assign(store: DelegationStore, task_id: str) -> None:
    store.process(
        HandoffEnvelope(
            protocol_version=PROTOCOL_VERSION,
            message_id=f"msg-assign-{task_id}",
            message_type=MessageType.ASSIGN,
            sender_id=ORCHESTRATOR_ID,
            recipient_id="noesis-forge",
            root_task_id="root-1",
            task_id=task_id,
            attempt_id=f"attempt-{task_id}",
            contract_revision="rev-1",
            assignment_epoch=1,
            correlation_id="corr-1",
            idempotency_key=f"idem-{task_id}",
            payload={
                "required_capability": "code_modify",
                "intent": "implement validate_port",
                "acceptance_criteria": ["tests pass"],
            },
        )
    )


def _running_task(store: DelegationStore, task_id: str):
    _assign(store, task_id)
    store.mark_ready(task_id, by=ORCHESTRATOR_ID)
    return store.start_running(task_id, by="noesis-forge")


@pytest.fixture
def store(tmp_path: Path) -> DelegationStore:
    return DelegationStore(tmp_path / "delegated-tasks.jsonl")


def test_extract_from_delegation_preserves_existing_behavior(store: DelegationStore) -> None:
    task = _running_task(store, "t-extract")
    assert task.lease is not None
    assert task.lease.is_active

    lease = extract_from_delegation(task)

    assert lease.holder == task.lease.holder
    assert lease.issued_at.isoformat() == task.lease.issued_at
    assert lease.expires_at.isoformat() == task.lease.expires_at
    assert lease.revoked is task.lease.revoked
    assert lease.is_active is task.lease.is_active
    assert lease.state is LeaseState.held
    assert lease.attempt_epoch == 1
    assert lease.task_id == task.task_id

    # DelegationStore path is unchanged: still running with an active lease.
    again = store.get("t-extract")
    assert again is not None
    assert again.state.value == "running"
    assert again.lease is not None and again.lease.is_active


def test_execution_released_at_is_set_on_release(store: DelegationStore) -> None:
    task = _running_task(store, "t-release")
    lease = extract_from_delegation(task)
    released = release_lease(lease)

    assert released.state is LeaseState.released
    assert released.execution_released_at is not None
    assert released.is_active is False
    # One-way: cannot return to granted.
    with pytest.raises(LeaseTransitionError, match="fail-closed"):
        grant_lease(released)


def test_monotonic_attempt_epoch_increments_on_start_running_and_rejects_stale_renew(
    store: DelegationStore,
) -> None:
    task = _running_task(store, "t-epoch")
    lease = extract_from_delegation(task)
    assert lease.attempt_epoch == 1

    second = start_running(lease, store=store)
    assert second.attempt_epoch == 2
    assert second.state is LeaseState.held
    assert load_attempt_epoch(store, "t-epoch") == 2

    third = start_running(second, store=store)
    assert third.attempt_epoch == 3
    assert load_attempt_epoch(store, "t-epoch") == 3

    records = [
        json.loads(line)
        for line in store.ledger_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    epochs = [r["attempt_epoch"] for r in records if "attempt_epoch" in r]
    assert epochs == [2, 3]

    rebuilt = DelegationStore(store.ledger_path)
    assert rebuilt.get("t-epoch") is not None
    assert rebuilt.get("t-epoch").lease.is_active  # type: ignore[union-attr]

    with pytest.raises(LeaseTransitionError, match="attempt_epoch mismatch"):
        lease_renew(third, 60, expected_attempt_epoch=2)


def test_lease_renew_fails_closed_on_attempt_epoch_mismatch(store: DelegationStore) -> None:
    lease = extract_from_delegation(_running_task(store, "t-mismatch"))
    with pytest.raises(LeaseTransitionError, match="attempt_epoch mismatch"):
        lease_renew(lease, 30, expected_attempt_epoch=lease.attempt_epoch + 1)
    renewed = lease_renew(lease, 3600, expected_attempt_epoch=lease.attempt_epoch)
    assert renewed.state is LeaseState.held
    assert renewed.attempt_epoch == lease.attempt_epoch
    assert renewed.expires_at > lease.expires_at


def test_lease_renew_fails_closed_if_not_in_held_state(store: DelegationStore) -> None:
    held = extract_from_delegation(_running_task(store, "t-not-held"))
    released = release_lease(held)
    with pytest.raises(LeaseTransitionError, match="not held"):
        lease_renew(released, 30, expected_attempt_epoch=released.attempt_epoch)

    requested = extract_from_delegation(store.process(
        HandoffEnvelope(
            protocol_version=PROTOCOL_VERSION,
            message_id="msg-assign-t-req",
            message_type=MessageType.ASSIGN,
            sender_id=ORCHESTRATOR_ID,
            recipient_id="noesis-forge",
            root_task_id="root-1",
            task_id="t-req",
            attempt_id="attempt-t-req",
            contract_revision="rev-1",
            assignment_epoch=1,
            correlation_id="corr-1",
            idempotency_key="idem-t-req",
            payload={"required_capability": "code_modify", "intent": "x", "acceptance_criteria": []},
        )
    ))
    assert requested.state is LeaseState.requested
    with pytest.raises(LeaseTransitionError, match="not held"):
        lease_renew(requested, 30, expected_attempt_epoch=0)


def test_additive_delegation_import_reexports_lifecycle_api() -> None:
    assert delegation_extract is extract_from_delegation
    assert delegation_lease_renew is lease_renew
