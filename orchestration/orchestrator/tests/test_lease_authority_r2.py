"""r2 Sentinel lease-authority regressions (must fail on 60ff6377)."""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.control_plane import Orchestrator, PolicyViolation
from app.delegation import (
    ActivationError,
    ORCHESTRATOR_ID,
    DelegationStore,
)

from tests.test_lease_lifecycle import (
    _FakeAdapter,
    _dispatched_coding,
    _handoff,
    _reviews_to_accept,
    _running_task,
)


@pytest.fixture
def store(tmp_path: Path) -> DelegationStore:
    return DelegationStore(tmp_path / "delegated-tasks.jsonl")


def test_worker_submit_omitting_epoch_after_restart_is_refused(store: DelegationStore) -> None:
    _running_task(store, "t-omit-epoch")
    first = store.submit_verifying("t-omit-epoch", by="noesis-forge", expected_attempt_epoch=1, candidate_revision="cand-1")
    released = first.lease.execution_released_at if first.lease is not None else None
    rebuilt = DelegationStore(store.ledger_path)
    with pytest.raises(ActivationError, match="stale_attempt|required"):
        rebuilt.submit_verifying("t-omit-epoch", by="noesis-forge", candidate_revision="cand-late")
    again = rebuilt.get("t-omit-epoch")
    assert again is not None and again.lease is not None
    assert again.candidate_revision == "cand-1"
    assert again.lease.attempt_epoch == 1
    assert again.lease.execution_released_at == released


def test_worker_different_revision_with_current_epoch_cannot_mint_attempt(
    store: DelegationStore,
) -> None:
    _running_task(store, "t-no-mint")
    first = store.submit_verifying("t-no-mint", by="noesis-forge", expected_attempt_epoch=1, candidate_revision="cand-1")
    epoch = first.lease.attempt_epoch if first.lease is not None else None
    with pytest.raises(ActivationError, match="stale_attempt|released"):
        store.submit_verifying(
            "t-no-mint",
            by="noesis-forge",
            candidate_revision="cand-2",
            expected_attempt_epoch=epoch,
        )
    again = store.get("t-no-mint")
    assert again is not None and again.lease is not None
    assert again.candidate_revision == "cand-1"
    assert again.lease.attempt_epoch == epoch


def test_only_orchestrator_grant_new_attempt_increments_epoch(store: DelegationStore) -> None:
    _running_task(store, "t-grant")
    first = store.submit_verifying("t-grant", by="noesis-forge", expected_attempt_epoch=1, candidate_revision="cand-1")
    assert first.lease is not None
    first_epoch = first.lease.attempt_epoch
    _reviews_to_accept(store, "t-grant", "cand-1")
    current = store.get("t-grant")
    assert current is not None
    granted = store.grant_new_attempt(
        "t-grant",
        by=ORCHESTRATOR_ID,
        expected_task_version=current.version,
        intended_assignee="noesis-forge",
        intended_candidate_revision="cand-2",
    )
    assert granted.lease is not None
    assert granted.lease.attempt_epoch == (first_epoch or 0) + 1
    assert granted.lease.execution_released_at is None
    assert granted.reviews == []
    with pytest.raises(ActivationError, match="orchestrator"):
        store.grant_new_attempt(
            "t-grant",
            by="noesis-forge",
            expected_task_version=granted.version,
        )
    second = store.submit_verifying(
        "t-grant",
        by="noesis-forge",
        candidate_revision="cand-2",
        expected_attempt_epoch=granted.lease.attempt_epoch,
    )
    assert second.lease is not None
    assert second.lease.attempt_epoch == granted.lease.attempt_epoch
    assert second.candidate_revision == "cand-2"


def test_review_non_pass_then_start_running_cannot_clear_release(store: DelegationStore) -> None:
    _running_task(store, "t-revive")
    submitted = store.submit_verifying("t-revive", by="noesis-forge", expected_attempt_epoch=1, candidate_revision="cand-1")
    released = submitted.lease.execution_released_at if submitted.lease is not None else None
    assert released is not None
    store.record_review(
        "t-revive",
        stage="spec",
        verdict="FAIL",
        reviewer="noesis-sentinel",
        candidate_revision="cand-1",
        findings=[],
    )
    with pytest.raises(ActivationError, match="stale_attempt|released"):
        store.start_running("t-revive", by="noesis-forge")
    again = store.get("t-revive")
    assert again is not None and again.lease is not None
    assert again.lease.execution_released_at == released
    assert again.lease.attempt_epoch == 1
    with pytest.raises(ActivationError, match="stale_attempt|released"):
        store.consume_budget("t-revive", 0.0, by="noesis-forge", expected_attempt_epoch=1)


def test_released_observe_budget_consume_renew_start_submit_refused(tmp_path: Path) -> None:
    store = DelegationStore(tmp_path / "delegated-tasks.jsonl")
    _running_task(store, "t-acct")
    store.submit_verifying("t-acct", by="noesis-forge", expected_attempt_epoch=1, candidate_revision="cand-1")
    with pytest.raises(ActivationError, match="stale_attempt|released|required"):
        store.consume_budget("t-acct", 0.0, by="noesis-forge", expected_attempt_epoch=1)
    with pytest.raises(ActivationError, match="stale_attempt|released|not held"):
        store.lease_renew("t-acct", duration=60, expected_attempt_epoch=1, by="noesis-forge")
    with pytest.raises(ActivationError, match="stale_attempt|released"):
        store.start_running("t-acct", by="noesis-forge", expected_attempt_epoch=1)
    with pytest.raises(ActivationError, match="stale_attempt|released|required"):
        store.submit_verifying(
            "t-acct",
            by="noesis-forge",
            candidate_revision="cand-2",
            expected_attempt_epoch=1,
        )

    orch = Orchestrator(tmp_path / "tasks.jsonl")
    orch.runtime_adapter = _FakeAdapter("noesis-forge")
    task = _dispatched_coding(orch, idem="forge:lease:observe:1")
    orch.submit_handoff(task.task_id, _handoff())
    submitted = orch.submit_candidate(task.task_id, candidate_revision="rev-1")
    used = submitted.delegation.requests_used
    with pytest.raises(PolicyViolation, match="stale_attempt|released|required"):
        orch.observe_budget(task.task_id, requests=3)
    with pytest.raises(PolicyViolation, match="stale_attempt|released|required"):
        orch.observe_budget(
            task.task_id, requests=3, attempt_epoch=submitted.delegation.attempt_epoch
        )
    again = orch.store.get(task.task_id)
    assert again is not None
    assert again.delegation.requests_used == used
    with pytest.raises(PolicyViolation, match="stale_attempt|released|required"):
        orch.submit_candidate(task.task_id, candidate_revision="rev-2")


def test_stale_taskstore_snapshot_cannot_overwrite_newer_candidate(tmp_path: Path) -> None:
    orch = Orchestrator(tmp_path / "tasks.jsonl")
    orch.runtime_adapter = _FakeAdapter("noesis-forge")
    task = _dispatched_coding(orch, idem="forge:lease:cas:1")
    orch.submit_handoff(task.task_id, _handoff())
    first = orch.submit_candidate(task.task_id, candidate_revision="v1", candidate_digest="sha256:v1")
    stale = deepcopy(first)
    live = orch.store.get(task.task_id)
    assert live is not None
    live.delegation.candidate_revision = "v2"
    live.delegation.candidate_digest = "sha256:v2"
    live.delegation.reviews = []
    orch.store.put(live, event="candidate_submitted")
    newer = orch.store.get(task.task_id)
    assert newer is not None
    assert newer.delegation.candidate_revision == "v2"
    with pytest.raises(Exception, match="version conflict|task version"):
        orch.store.put(stale, event="stale_restore")
    held = orch.store.get(task.task_id)
    assert held is not None
    assert held.delegation.candidate_revision == "v2"
    assert held.delegation.candidate_digest == "sha256:v2"


def test_stale_accept_snapshot_cannot_restore_superseded_candidate(tmp_path: Path) -> None:
    orch = Orchestrator(tmp_path / "tasks.jsonl")
    orch.runtime_adapter = _FakeAdapter("noesis-forge")
    task = _dispatched_coding(orch, idem="forge:lease:accept-race:1")
    orch.submit_handoff(task.task_id, _handoff())
    orch.submit_candidate(task.task_id, candidate_revision="v1", candidate_digest="sha256:v1")
    orch.record_review(
        task.task_id, stage="spec", verdict="PASS", reviewer="noesis-sentinel", candidate_revision="v1"
    )
    orch.record_review(
        task.task_id,
        stage="quality",
        verdict="APPROVED",
        reviewer="noesis-skeptic",
        candidate_revision="v1",
    )
    ready = orch.store.get(task.task_id)
    assert ready is not None
    stale = deepcopy(ready)
    live = orch.store.get(task.task_id)
    assert live is not None
    live.delegation.candidate_revision = "v2"
    live.delegation.candidate_digest = "sha256:v2"
    live.delegation.reviews = []
    orch.store.put(live, event="candidate_submitted")
    with pytest.raises(PolicyViolation, match="spec_review_required"):
        orch.accept(task.task_id)
    stale.delegation.accepted_by = "noesis-orchestrator"
    stale.delegation.accepted_at = datetime.now(timezone.utc)
    stale.state = "succeeded"
    with pytest.raises(Exception, match="version conflict|task version"):
        orch.store.put(stale, event="task_accepted")
    held = orch.store.get(task.task_id)
    assert held is not None
    assert held.delegation.candidate_revision == "v2"
    assert held.delegation.accepted_by is None
    assert held.state != "succeeded"


def test_timezone_naive_execution_released_at_replay_fails_closed(tmp_path: Path) -> None:
    ledger = tmp_path / "naive.jsonl"
    store = DelegationStore(ledger)
    _running_task(store, "t-naive")
    store.submit_verifying("t-naive", by="noesis-forge", expected_attempt_epoch=1, candidate_revision="cand-1")
    line = json.loads(ledger.read_text(encoding="utf-8").splitlines()[-1])
    line["task"]["lease"]["execution_released_at"] = "2020-01-01T00:00:00"
    ledger.write_text(json.dumps(line) + "\n", encoding="utf-8")
    with pytest.raises(ActivationError, match="timezone-aware|execution_released_at"):
        DelegationStore(ledger)

    orch_ledger = tmp_path / "naive-cp.jsonl"
    orch = Orchestrator(orch_ledger)
    orch.runtime_adapter = _FakeAdapter("noesis-forge")
    task = _dispatched_coding(orch, idem="forge:lease:naive:1")
    orch.submit_handoff(task.task_id, _handoff())
    orch.submit_candidate(task.task_id, candidate_revision="rev-1")
    raw_lines = orch_ledger.read_text(encoding="utf-8").splitlines()
    last = json.loads(raw_lines[-1])
    last["task"]["delegation"]["execution_released_at"] = "2020-01-01T00:00:00"
    orch_ledger.write_text("\n".join(raw_lines[:-1] + [json.dumps(last)]) + "\n", encoding="utf-8")
    rebuilt_orch = Orchestrator(orch_ledger)
    assert rebuilt_orch.store.replay_error is not None
    assert "timezone-aware" in rebuilt_orch.store.replay_error or "execution_released_at" in rebuilt_orch.store.replay_error


def test_restart_preserves_grant_epoch_release_and_review_invalidation(
    store: DelegationStore,
) -> None:
    _running_task(store, "t-replay-grant")
    store.submit_verifying("t-replay-grant", by="noesis-forge", expected_attempt_epoch=1, candidate_revision="cand-1")
    _reviews_to_accept(store, "t-replay-grant", "cand-1")
    current = store.get("t-replay-grant")
    assert current is not None
    granted = store.grant_new_attempt(
        "t-replay-grant",
        by=ORCHESTRATOR_ID,
        expected_task_version=current.version,
        intended_assignee="noesis-forge",
        intended_candidate_revision="cand-2",
    )
    rebuilt = DelegationStore(store.ledger_path)
    again = rebuilt.get("t-replay-grant")
    assert again is not None and again.lease is not None and granted.lease is not None
    assert again.lease.attempt_epoch == granted.lease.attempt_epoch
    assert again.lease.execution_released_at is None
    assert again.reviews == []
    submitted = rebuilt.submit_verifying(
        "t-replay-grant",
        by="noesis-forge",
        candidate_revision="cand-2",
        expected_attempt_epoch=again.lease.attempt_epoch,
    )
    assert submitted.lease is not None
    assert submitted.lease.execution_released_at is not None
    rebuilt2 = DelegationStore(store.ledger_path)
    final = rebuilt2.get("t-replay-grant")
    assert final is not None and final.lease is not None
    assert final.lease.attempt_epoch == submitted.lease.attempt_epoch
    assert final.lease.execution_released_at == submitted.lease.execution_released_at
    assert final.candidate_revision == "cand-2"
    assert final.reviews == []
