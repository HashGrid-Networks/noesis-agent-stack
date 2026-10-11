"""Shared-budget reservation and exhaustion tests for durable delegation.

Covers Subgoal 4 §11 ("shared-budget exhaustion and concurrent reservation
races") and criterion 4 §9 ("reserve parent budgets atomically"; "unknown
usage must not be treated as zero"). Exercises the real DelegationStore and
ledger persistence.
"""
from __future__ import annotations

import threading
from pathlib import Path
from uuid import uuid4

import pytest

from app.delegation import (
    ActivationError,
    BudgetExhausted,
    DelegationStore,
    HandoffEnvelope,
    MessageType,
    ORCHESTRATOR_ID,
)


def _store(tmp_path: Path) -> DelegationStore:
    return DelegationStore(tmp_path / "delegated-tasks.jsonl")


def _assign(
    task_id: str,
    *,
    root: str | None = None,
    idem: str | None = None,
    requirement: float | None = None,
    cap: float | None = None,
) -> HandoffEnvelope:
    payload: dict = {
        "intent": "bounded work",
        "acceptance_criteria": ["done"],
        "required_capability": "implement",
    }
    if requirement is not None:
        payload["budget_requirement"] = requirement
    if cap is not None:
        payload["budget_cap"] = cap
    return HandoffEnvelope(
        protocol_version="noesis.delegated-task/v1",
        message_id=str(uuid4()),
        message_type=MessageType.ASSIGN,
        sender_id="noesis-orchestrator",
        recipient_id="noesis-forge",
        root_task_id=root or task_id,
        task_id=task_id,
        attempt_id=f"{task_id}/a1",
        contract_revision="c-rev-1",
        assignment_epoch=1,
        correlation_id=root or task_id,
        idempotency_key=idem or f"idem-{task_id}",
        expected_task_version=0,
        payload=payload,
    )


def test_assign_reserves_budget_under_cap(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.process(_assign("t1", requirement=4.0, cap=10.0))
    task = store.get("t1")
    assert task is not None
    assert task.budget_reserved == 4.0
    assert task.budget_consumed == 0.0


def test_assign_exhausting_cap_fails_without_persisting(tmp_path: Path) -> None:
    ledger = tmp_path / "delegated-tasks.jsonl"
    store = DelegationStore(ledger)
    with pytest.raises(BudgetExhausted, match="budget_exhausted"):
        store.process(_assign("t1", requirement=11.0, cap=10.0))
    # Nothing was inserted or persisted.
    assert store.get("t1") is None
    reloaded = DelegationStore(ledger)
    assert reloaded.get("t1") is None


def test_shared_root_cap_blocks_sibling_assignment(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.process(_assign("t1", root="root-1", requirement=6.0, cap=10.0))
    with pytest.raises(BudgetExhausted, match="budget_exhausted"):
        store.process(_assign("t2", root="root-1", requirement=6.0, cap=10.0))
    assert store.get("t2") is None
    # A sibling that fits the remaining headroom is admitted.
    store.process(_assign("t3", root="root-1", requirement=4.0, cap=10.0))
    assert store.get("t3") is not None


def test_idempotent_redelivery_does_not_double_reserve(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.process(_assign("t1", root="root-1", idem="idem-x", requirement=6.0, cap=10.0))
    again = store.process(_assign("t1", root="root-1", idem="idem-x", requirement=6.0, cap=10.0))
    assert again.budget_reserved == 6.0
    # Remaining headroom is still 4, not -2: no double reservation.
    store.process(_assign("t2", root="root-1", requirement=4.0, cap=10.0))
    assert store.get("t2") is not None


def test_concurrent_reservations_are_atomic(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.process(_assign("t0", root="root-1", requirement=1.0, cap=10.0))
    results: list[bool] = []
    barrier = threading.Barrier(6)

    def worker(n: int) -> None:
        barrier.wait()
        try:
            store.process(
                _assign(f"t{n}", root="root-1", requirement=3.0, cap=10.0)
            )
            results.append(True)
        except BudgetExhausted:
            results.append(False)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(1, 7)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Cap 10 with 1.0 pre-reserved -> exactly 3 concurrent 3.0 reservations fit.
    assert results.count(True) == 3
    assert results.count(False) == 3
    total = sum(t.budget_reserved for t in store._tasks.values())
    assert total <= 10.0


def test_consume_never_exceeds_reservation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.process(_assign("t1", requirement=6.0))
    store.mark_ready("t1", by=ORCHESTRATOR_ID)
    store.consume_budget("t1", 4.0, by="noesis-forge", expected_attempt_epoch=1)
    task = store.consume_budget("t1", 2.0, by="noesis-forge", expected_attempt_epoch=1)
    assert task.budget_consumed == 6.0
    with pytest.raises(ActivationError, match="budget_overconsumption"):
        store.consume_budget("t1", 0.1, by="noesis-forge", expected_attempt_epoch=1)


def test_consume_requires_assignee(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.process(_assign("t1", requirement=6.0))
    with pytest.raises(ActivationError, match="only the assigned specialist"):
        store.consume_budget("t1", 1.0, by="noesis-grid")
    with pytest.raises(ActivationError, match="only the orchestrator reserves"):
        store.reserve_budget("t1", 1.0, by="noesis-forge")


def test_budget_state_survives_restart(tmp_path: Path) -> None:
    ledger = tmp_path / "delegated-tasks.jsonl"
    store = DelegationStore(ledger)
    store.process(_assign("t1", root="root-1", requirement=6.0, cap=10.0))
    store.mark_ready("t1", by=ORCHESTRATOR_ID)
    store.consume_budget("t1", 2.5, by="noesis-forge", expected_attempt_epoch=1)

    reloaded = DelegationStore(ledger)
    task = reloaded.get("t1")
    assert task is not None
    assert task.budget_reserved == 6.0
    assert task.budget_consumed == 2.5
    # Reservation headroom is still enforced after replay.
    reloaded.process(_assign("t2", root="root-1", requirement=4.0, cap=10.0))
    with pytest.raises(BudgetExhausted):
        reloaded.process(_assign("t3", root="root-1", requirement=1.0, cap=10.0))
