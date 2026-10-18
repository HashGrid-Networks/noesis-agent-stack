"""Bounded lease-lifecycle tests (LEASE-LIFECYCLE-DESIGN.md, 2026-10-11)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.control_plane import Orchestrator, PolicyViolation
from app.delegation import (
    ActivationError,
    ORCHESTRATOR_ID,
    PROTOCOL_VERSION,
    DelegationStore,
    HandoffEnvelope,
    MessageType,
    TaskState,
    extract_from_delegation as delegation_extract,
    lease_renew as delegation_lease_renew,
)
from app.models import Verification
from app.lease_lifecycle import (
    LeaseState,
    LeaseTransitionError,
    extract_from_delegation,
    grant_lease,
    grant_new_attempt,
    lease_renew,
    load_attempt_epoch,
    release_lease,
    start_running,
)


REPO_ROOT = Path(__file__).resolve().parents[3]
HISTORICAL_LEDGER = REPO_ROOT / "workspace/subgoal5-sandbox/control-plane-ledger.jsonl"

# The historical ledger is a gitignored sandbox artifact (workspace/ is
# gitignored); absent checkouts skip rather than fail.
requires_historical_ledger = pytest.mark.skipif(
    not HISTORICAL_LEDGER.is_file(),
    reason=f"historical sandbox fixture absent (gitignored): {HISTORICAL_LEDGER}",
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
    return store.start_running(task_id, by="noesis-forge", expected_attempt_epoch=1)


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

    second = grant_new_attempt(lease, store=store)
    assert second.attempt_epoch == 2
    assert second.state is LeaseState.held
    assert load_attempt_epoch(store, "t-epoch") == 2

    released = release_lease(second)
    third = grant_new_attempt(released, store=store)
    assert third.attempt_epoch == 3
    assert load_attempt_epoch(store, "t-epoch") == 3

    records = [
        json.loads(line)
        for line in store.ledger_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    epochs = [
        ((r.get("task") or {}).get("lease") or {}).get("attempt_epoch")
        for r in records
        if ((r.get("task") or {}).get("lease") or {}).get("attempt_epoch") in {2, 3}
    ]
    assert epochs[-2:] == [2, 3]

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


def _reviews_to_accept(store: DelegationStore, task_id: str, candidate: str) -> None:
    store.record_review(
        task_id,
        stage="spec",
        verdict="PASS",
        reviewer="noesis-sentinel",
        candidate_revision=candidate,
        findings=[],
    )
    store.record_review(
        task_id,
        stage="quality",
        verdict="APPROVED",
        reviewer="noesis-skeptic",
        candidate_revision=candidate,
        findings=[],
    )


def _force_lease_wall_clock_expiry(store: DelegationStore, task_id: str) -> None:
    task = store.get(task_id)
    assert task is not None and task.lease is not None
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    lease_dict = {
        "holder": task.lease.holder,
        "epoch": task.lease.epoch,
        "issued_at": task.lease.issued_at,
        "expires_at": past,
        "revoked": task.lease.revoked,
        "attempt_epoch": task.lease.attempt_epoch,
        "execution_released_at": task.lease.execution_released_at,
    }
    task.lease = type(task.lease)(**{k: v for k, v in lease_dict.items() if hasattr(task.lease, k) or k in ("attempt_epoch", "execution_released_at")})
    store._tasks[task_id] = task


# ---------------------------------------------------------------------------
# Production-path regressions (Sentinel F1–F5 / E1)
# ---------------------------------------------------------------------------


def test_submit_verifying_releases_execution_and_persists_epoch(store: DelegationStore) -> None:
    task = _running_task(store, "t-release-prod")
    submitted = store.submit_verifying(
        "t-release-prod", by="noesis-forge", expected_attempt_epoch=1, candidate_revision="cand-1"
    )

    assert submitted.state is TaskState.verifying
    assert submitted.lease is not None
    assert submitted.lease.execution_released_at is not None
    assert submitted.lease.attempt_epoch == 1
    assert submitted.lease.execution_released_at is not None

    records = [
        json.loads(line)
        for line in store.ledger_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    last_task = records[-1]["task"]
    assert last_task["lease"]["attempt_epoch"] == 1
    assert last_task["lease"]["execution_released_at"]
    assert "lease_lifecycle" not in records[-1]

    rebuilt = DelegationStore(store.ledger_path)
    again = rebuilt.get("t-release-prod")
    assert again is not None and again.lease is not None
    assert again.lease.attempt_epoch == 1
    assert again.lease.execution_released_at == submitted.lease.execution_released_at


def test_stale_same_epoch_submit_after_release_fails(store: DelegationStore) -> None:
    _running_task(store, "t-stale-submit")
    store.submit_verifying("t-stale-submit", by="noesis-forge", expected_attempt_epoch=1, candidate_revision="cand-1")
    with pytest.raises(ActivationError, match="stale_attempt"):
        store.submit_verifying(
            "t-stale-submit",
            by="noesis-forge",
            candidate_revision="cand-2",
            expected_attempt_epoch=1,
        )


def test_revoked_worker_submit_and_action_fail(store: DelegationStore) -> None:
    _running_task(store, "t-revoked")
    store.submit_verifying("t-revoked", by="noesis-forge", expected_attempt_epoch=1, candidate_revision="cand-1")
    store.expire_lease("t-revoked")
    task = store.get("t-revoked")
    assert task is not None and task.lease is not None and task.lease.revoked
    assert task.state is TaskState.verifying
    with pytest.raises(ActivationError, match="revoked"):
        store.submit_verifying("t-revoked", by="noesis-forge", expected_attempt_epoch=1, candidate_revision="cand-2")
    with pytest.raises(ActivationError, match="revoked"):
        store.consume_budget("t-revoked", 0.0, by="noesis-forge")
    with pytest.raises(ActivationError, match="revoked|not held|released"):
        store.lease_renew("t-revoked", duration=60, expected_attempt_epoch=1, by="noesis-forge")


def test_accept_after_execution_lease_expiry_without_reviving_permission(
    store: DelegationStore,
) -> None:
    _running_task(store, "t-accept-expired")
    store.submit_verifying("t-accept-expired", by="noesis-forge", expected_attempt_epoch=1, candidate_revision="cand-1")
    _reviews_to_accept(store, "t-accept-expired", "cand-1")
    _force_lease_wall_clock_expiry(store, "t-accept-expired")

    accepted = store.accept("t-accept-expired", by=ORCHESTRATOR_ID)
    assert accepted.accepted is True
    assert accepted.state is TaskState.completed

    with pytest.raises(ActivationError, match="stale_attempt|released|not ready"):
        store.start_running("t-accept-expired", by="noesis-forge", expected_attempt_epoch=1)
    with pytest.raises(ActivationError, match="stale_attempt|revoked|released"):
        store.submit_verifying(
            "t-accept-expired",
            by="noesis-forge",
            candidate_revision="cand-late",
            expected_attempt_epoch=1,
        )


def test_accept_rejects_stale_candidate_epoch_reviews_actor_and_version(
    store: DelegationStore,
) -> None:
    _running_task(store, "t-gates")
    store.submit_verifying("t-gates", by="noesis-forge", expected_attempt_epoch=1, candidate_revision="cand-1")
    _reviews_to_accept(store, "t-gates", "cand-1")
    current = store.get("t-gates")
    assert current is not None

    with pytest.raises(ActivationError, match="orchestrator"):
        store.accept("t-gates", by="noesis-forge")
    with pytest.raises(ActivationError, match="task version conflict"):
        store.accept("t-gates", by=ORCHESTRATOR_ID, expected_task_version=current.version - 1)
    with pytest.raises(ActivationError, match="stale_attempt"):
        store.accept("t-gates", by=ORCHESTRATOR_ID, expected_attempt_epoch=99)

    current = store.get("t-gates")
    assert current is not None
    granted = store.grant_new_attempt(
        "t-gates",
        by=ORCHESTRATOR_ID,
        expected_task_version=current.version,
        intended_assignee="noesis-forge",
        intended_candidate_revision="cand-2",
    )
    store.submit_verifying(
        "t-gates",
        by="noesis-forge",
        expected_attempt_epoch=granted.lease.attempt_epoch,
        candidate_revision="cand-2",
    )
    with pytest.raises(ActivationError, match="spec PASS|quality APPROVED"):
        store.accept("t-gates", by=ORCHESTRATOR_ID)


def test_restart_at_review_boundary_preserves_release_and_rejects_stale_worker(
    store: DelegationStore,
) -> None:
    _running_task(store, "t-restart")
    submitted = store.submit_verifying(
        "t-restart", by="noesis-forge", expected_attempt_epoch=1, candidate_revision="cand-1"
    )
    rebuilt = DelegationStore(store.ledger_path)
    again = rebuilt.get("t-restart")
    assert again is not None and again.lease is not None
    assert again.lease.attempt_epoch == submitted.lease.attempt_epoch
    assert again.lease.execution_released_at == submitted.lease.execution_released_at
    with pytest.raises(ActivationError, match="stale_attempt"):
        rebuilt.submit_verifying(
            "t-restart",
            by="noesis-forge",
            candidate_revision="cand-late",
            expected_attempt_epoch=again.lease.attempt_epoch,
        )


def test_new_candidate_increments_epoch_and_invalidates_reviews(
    store: DelegationStore,
) -> None:
    _running_task(store, "t-new-cand")
    first = store.submit_verifying("t-new-cand", by="noesis-forge", expected_attempt_epoch=1, candidate_revision="cand-1")
    first_epoch = first.lease.attempt_epoch if first.lease is not None else None
    _reviews_to_accept(store, "t-new-cand", "cand-1")
    current = store.get("t-new-cand")
    assert current is not None
    granted = store.grant_new_attempt(
        "t-new-cand",
        by=ORCHESTRATOR_ID,
        expected_task_version=current.version,
        intended_candidate_revision="cand-2",
    )
    second = store.submit_verifying(
        "t-new-cand",
        by="noesis-forge",
        expected_attempt_epoch=granted.lease.attempt_epoch,
        candidate_revision="cand-2",
    )
    assert second.lease is not None
    assert first_epoch == 1
    assert second.lease.attempt_epoch == first_epoch + 1
    assert second.reviews == []
    with pytest.raises(ActivationError):
        store.record_review(
            "t-new-cand",
            stage="quality",
            verdict="APPROVED",
            reviewer="noesis-sentinel",
            candidate_revision="cand-2",
            findings=[],
        )


def test_malformed_persisted_epoch_and_release_fail_closed(tmp_path: Path) -> None:
    ledger = tmp_path / "bad.jsonl"
    good = DelegationStore(ledger)
    _running_task(good, "t-malformed")
    good.submit_verifying("t-malformed", by="noesis-forge", expected_attempt_epoch=1, candidate_revision="cand-1")
    line = json.loads(ledger.read_text(encoding="utf-8").splitlines()[-1])
    line["task"]["lease"]["attempt_epoch"] = "not-an-int"
    ledger.write_text(json.dumps(line) + "\n", encoding="utf-8")
    with pytest.raises(ActivationError, match="attempt_epoch"):
        DelegationStore(ledger)

    ledger.write_text("", encoding="utf-8")
    good2 = DelegationStore(ledger)
    _running_task(good2, "t-malformed-ts")
    good2.submit_verifying("t-malformed-ts", by="noesis-forge", expected_attempt_epoch=1, candidate_revision="cand-1")
    line = json.loads(ledger.read_text(encoding="utf-8").splitlines()[-1])
    line["task"]["lease"]["execution_released_at"] = "not-a-timestamp"
    ledger.write_text(json.dumps(line) + "\n", encoding="utf-8")
    with pytest.raises(ActivationError, match="execution_released_at"):
        DelegationStore(ledger)


def test_old_record_absent_fields_remain_unknown_and_keep_legacy_accept_gate(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "legacy.jsonl"
    store = DelegationStore(ledger)
    task = _running_task(store, "t-legacy")
    store.submit_verifying("t-legacy", by="noesis-forge", expected_attempt_epoch=1, candidate_revision="cand-1")
    _reviews_to_accept(store, "t-legacy", "cand-1")
    line = json.loads(ledger.read_text(encoding="utf-8").splitlines()[-1])
    line["task"]["lease"].pop("attempt_epoch", None)
    line["task"]["lease"].pop("execution_released_at", None)
    ledger.write_text(json.dumps({"task": line["task"]}) + "\n", encoding="utf-8")
    rebuilt = DelegationStore(ledger)
    again = rebuilt.get("t-legacy")
    assert again is not None and again.lease is not None
    assert again.lease.attempt_epoch is None
    assert again.lease.execution_released_at is None
    _force_lease_wall_clock_expiry(rebuilt, "t-legacy")
    with pytest.raises(ActivationError, match="lease is inactive"):
        rebuilt.accept("t-legacy", by=ORCHESTRATOR_ID)


def test_concurrent_accept_stale_version_loses(store: DelegationStore) -> None:
    _running_task(store, "t-concurrent")
    store.submit_verifying("t-concurrent", by="noesis-forge", expected_attempt_epoch=1, candidate_revision="cand-1")
    _reviews_to_accept(store, "t-concurrent", "cand-1")
    current = store.get("t-concurrent")
    assert current is not None
    stale_version = current.version
    winner = store.accept("t-concurrent", by=ORCHESTRATOR_ID, expected_task_version=stale_version)
    assert winner.accepted is True
    with pytest.raises(ActivationError, match="task version conflict"):
        store.submit_verifying(
            "t-concurrent",
            by="noesis-forge",
            candidate_revision="cand-race",
            expected_task_version=stale_version,
        )


def test_lease_renew_refused_during_review_and_after_release(store: DelegationStore) -> None:
    _running_task(store, "t-no-renew")
    store.submit_verifying("t-no-renew", by="noesis-forge", expected_attempt_epoch=1, candidate_revision="cand-1")
    with pytest.raises(ActivationError, match="not held|released|review"):
        store.lease_renew("t-no-renew", duration=3600, expected_attempt_epoch=1, by="noesis-forge")


@requires_historical_ledger
def test_historical_cand_subgoal5_v1_remains_unaccepted() -> None:
    path = HISTORICAL_LEDGER
    accepted = 0
    saw_candidate = False
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        record = json.loads(raw)
        task = record.get("task") or {}
        delegation = task.get("delegation") or {}
        if delegation.get("candidate_revision") == "cand-subgoal5-v1":
            saw_candidate = True
            assert delegation.get("accepted_by") in (None, "")
            assert delegation.get("accepted_at") in (None, "")
        if record.get("event") == "task_accepted":
            accepted += 1
    assert saw_candidate
    assert accepted == 0


class _FakeAdapter:
    def __init__(self, loaded_profile: str) -> None:
        self._p = loaded_profile

    def launch(self, *, profile_id, prompt, task_id, cwd=None, dry_run=True):
        return SimpleNamespace(
            ok=True,
            loaded_profile=self._p,
            error=None,
            requested_profile=profile_id,
            dry_run=dry_run,
            verification_pending=False,
            artifacts=(),
            stdout_digest="",
            collection_error=None,
            attempt_id="1",
            contract_revision="rev-1",
            assignment_epoch=1,
            baseline=None,
        )


def _handoff() -> dict:
    return {
        "summary": "Implemented and verified the function with full test coverage.",
        "artifacts": [{"path": "validate_port.py", "checksum": None, "kind": "source"}],
        "verification_result": {"passed": True, "evidence": "pytest -q: 11 passed."},
        "unmet_criteria": [],
    }


def _dispatched_coding(orch: Orchestrator, *, idem: str = "forge:lease:validate_port:1"):
    task = orch.propose(
        title="Implement validate_port",
        intent="Implement a deterministic port-range validation function with tests.",
        assignee_profile="noesis-forge",
        required_capability="code_modify",
        acceptance_criteria=["Return (True, None) in [1,65535] excluding bool"],
        verification=Verification(method="automated_test"),
        idempotency_key=idem,
        risk_tier="r1",
        reviewer_profile="noesis-sentinel",
        timeout_s=900,
    )
    return orch.mark_dispatched(task.task_id, runtime_profile="noesis-forge")


def test_control_plane_submit_releases_and_accept_ignores_claim_lease_expiry(tmp_path: Path) -> None:
    orch = Orchestrator(tmp_path / "tasks.jsonl")
    orch.runtime_adapter = _FakeAdapter("noesis-forge")
    task = _dispatched_coding(orch)
    orch.submit_handoff(task.task_id, _handoff())
    submitted = orch.submit_candidate(task.task_id, candidate_revision="rev-1", candidate_digest="sha256:rev-1", attempt_epoch=1)
    assert submitted.delegation.execution_released_at is not None
    assert submitted.delegation.attempt_epoch == 1

    claim = dict(submitted.claim)
    claim["lease_expires_at"] = submitted.claim["lease_expires_at"] - timedelta(hours=2)
    submitted.claim = claim
    orch.store.put(submitted, event="lease_forced_expired")

    orch.record_review(
        task.task_id, stage="spec", verdict="PASS", reviewer="noesis-sentinel", candidate_revision="rev-1"
    )
    orch.record_review(
        task.task_id, stage="quality", verdict="APPROVED", reviewer="noesis-skeptic", candidate_revision="rev-1"
    )
    with pytest.raises(PolicyViolation, match="stale_attempt"):
        orch.submit_candidate(
            task.task_id,
            candidate_revision="rev-late",
            attempt_epoch=1,
        )
    with pytest.raises(PolicyViolation, match="unauthorized"):
        orch.accept(task.task_id, operator="noesis-forge")
    accepted = orch.accept(task.task_id)
    assert accepted.state == "succeeded"
    assert accepted.delegation.accepted_by == "noesis-orchestrator"

    rebuilt = Orchestrator(tmp_path / "tasks.jsonl")
    again = rebuilt.store.get(task.task_id)
    assert again.delegation.execution_released_at is not None
    assert again.delegation.attempt_epoch == 1


def test_control_plane_new_candidate_bumps_epoch(tmp_path: Path) -> None:
    orch = Orchestrator(tmp_path / "tasks.jsonl")
    orch.runtime_adapter = _FakeAdapter("noesis-forge")
    task = _dispatched_coding(orch, idem="forge:lease:validate_port:2")
    orch.submit_handoff(task.task_id, _handoff())
    first = orch.submit_candidate(task.task_id, candidate_revision="a", attempt_epoch=1)
    first_epoch = first.delegation.attempt_epoch
    orch.record_review(task.task_id, stage="spec", verdict="PASS", reviewer="noesis-sentinel", candidate_revision="a")
    orch.record_review(task.task_id, stage="quality", verdict="APPROVED", reviewer="noesis-skeptic", candidate_revision="a")
    live = orch.store.get(task.task_id)
    assert live is not None
    granted = orch.grant_new_attempt(
        task.task_id,
        expected_task_version=live.task_version,
        intended_assignee="noesis-forge",
        intended_candidate_revision="b",
    )
    second = orch.submit_candidate(
        task.task_id,
        candidate_revision="b",
        attempt_epoch=granted.delegation.attempt_epoch,
    )
    assert first_epoch == 1
    assert second.delegation.attempt_epoch == first_epoch + 1
    assert second.delegation.current_reviews("spec") == []
    with pytest.raises(PolicyViolation, match="spec_review_required"):
        orch.accept(task.task_id)
