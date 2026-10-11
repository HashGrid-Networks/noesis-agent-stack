"""Delivery-loop planner tests (addendum A / NOESIS-LOOP-GRAPH-V1).

All scenarios run against a real DelegationStore on a sandbox ledger with
synthetic records. The planner must stay strictly read-only: every test that
plans asserts the authoritative ledger is untouched.
"""
from __future__ import annotations

import json
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.delegation import (
    ORCHESTRATOR_ID,
    PROTOCOL_VERSION,
    ActivationError,
    DelegationStore,
    HandoffEnvelope,
    Lease,
    MessageType,
    Review,
    TaskState,
)
from app.delivery_planner import DeliveryPlanner, PlannerDisposition


def _assign(store: DelegationStore, task_id: str, *, payload: dict | None = None) -> None:
    msg = HandoffEnvelope(
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
            **(payload or {}),
        },
    )
    store.process(msg)


@pytest.fixture
def store(tmp_path: Path) -> DelegationStore:
    return DelegationStore(tmp_path / "delegated-tasks.jsonl")


@pytest.fixture
def planner(store: DelegationStore) -> DeliveryPlanner:
    return DeliveryPlanner(store)


def _to_verifying(store: DelegationStore, task_id: str, candidate: str = "cand-1") -> None:
    store.mark_ready(task_id, by=ORCHESTRATOR_ID)
    store.start_running(task_id, by="noesis-forge")
    store.submit_verifying(task_id, by="noesis-forge", expected_attempt_epoch=1, candidate_revision=candidate)


def _accept_ready(store: DelegationStore, task_id: str, candidate: str = "cand-1") -> None:
    _to_verifying(store, task_id, candidate)
    store.record_review(
        task_id, stage="spec", verdict="PASS", reviewer="noesis-sentinel",
        candidate_revision=candidate, findings=[],
    )
    store.record_review(
        task_id, stage="quality", verdict="APPROVED", reviewer="noesis-skeptic",
        candidate_revision=candidate, findings=[],
    )


def _snapshot(store: DelegationStore) -> tuple[bytes, dict[str, int]]:
    ledger = store.ledger_path.read_bytes() if store.ledger_path.exists() else b""
    versions = {tid: t.version for tid, t in store._tasks.items()}
    return ledger, versions


# --------------------------------------------------------------------------
# Determinism / rebuild
# --------------------------------------------------------------------------

def test_deterministic_rebuild_and_duplicate_ingestion(tmp_path, planner, store):
    for tid in ("t1", "t2", "t3"):
        _assign(store, tid)
    _to_verifying(store, "t1")
    before = {tid: planner.plan(tid) for tid in ("t1", "t2", "t3")}

    # Rebuild a fresh store from the same ledger (replay dedups by version).
    rebuilt = DelegationStore(store.ledger_path)
    planner2 = DeliveryPlanner(rebuilt)
    for tid in ("t1", "t2", "t3"):
        again = planner2.plan(tid)
        assert again == before[tid]
    assert len(rebuilt._all_records()) == 3


# --------------------------------------------------------------------------
# Scope mismatch / candidate changes do not inherit review
# --------------------------------------------------------------------------

def test_candidate_change_does_not_inherit_review(planner, store):
    _assign(store, "t1")
    _accept_ready(store, "t1", candidate="cand-1")
    # Remediation requires an orchestrator grant; old reviews must not apply.
    current = store.get("t1")
    assert current is not None
    granted = store.grant_new_attempt(
        "t1",
        by=ORCHESTRATOR_ID,
        expected_task_version=current.version,
        intended_candidate_revision="cand-2",
    )
    assert granted.lease is not None
    store.submit_verifying(
        store.get("t1").task_id,
        by="noesis-forge",
        expected_attempt_epoch=granted.lease.attempt_epoch,
        candidate_revision="cand-2",
    )
    decision = planner.plan("t1")
    assert decision.disposition is PlannerDisposition.next_action_proposal
    assert decision.proposed_action == "dispatch_spec_review"

    # An externally-ingested row for the superseded candidate is cited as a
    # scope mismatch and still does not apply.
    task = store.get("t1")
    injected = replace(task)
    injected.reviews = task.reviews + [
        Review("quality", "APPROVED", "noesis-skeptic", "cand-1", [])
    ]
    injected.version = task.version + 1
    store._tasks["t1"] = injected
    store._append(injected)
    decision = planner.plan("t1")
    stale = [r for r in decision.source_references if r.detail.get("scope_mismatch")]
    assert len(stale) == 1
    assert stale[0].detail["review_candidate"] == "cand-1"
    assert decision.proposed_action == "dispatch_spec_review"


def test_no_candidate_is_blocked_on_evidence(planner, store):
    _assign(store, "t1")
    store.mark_ready("t1", by=ORCHESTRATOR_ID)
    decision = planner.plan("t1")
    assert decision.disposition is PlannerDisposition.blocked_on_evidence
    assert decision.source_references  # source refs exist for the decision


# --------------------------------------------------------------------------
# Active-attempt duplicate prevention
# --------------------------------------------------------------------------

def test_active_attempt_yields_wait_for_existing_attempt(planner, store):
    _assign(store, "t1")
    store.mark_ready("t1", by=ORCHESTRATOR_ID)
    store.start_running("t1", by="noesis-forge")
    decision = planner.plan("t1")
    assert decision.disposition is PlannerDisposition.wait_for_existing_attempt
    assert decision.proposed_action is None
    lease_refs = [r for r in decision.source_references if r.record == "lease"]
    assert lease_refs and lease_refs[0].detail["holder"] == "noesis-forge"


def test_expired_lease_marks_suspect_and_blocks_on_review(planner, store):
    _assign(store, "t1")
    store.mark_ready("t1", by=ORCHESTRATOR_ID)
    store.start_running("t1", by="noesis-forge")
    store.expire_lease("t1")
    task = store.get("t1")
    # Expiry marks the attempt suspect (blocked), never silently successful.
    assert task.state is TaskState.blocked
    decision = planner.plan("t1")
    assert decision.disposition is PlannerDisposition.blocked_on_review


# --------------------------------------------------------------------------
# Review gating
# --------------------------------------------------------------------------

def test_verifying_without_reviews_proposes_spec_review(planner, store):
    _assign(store, "t1")
    _to_verifying(store, "t1")
    decision = planner.plan("t1")
    assert decision.disposition is PlannerDisposition.next_action_proposal
    assert decision.proposed_action == "dispatch_spec_review"


def test_spec_pass_proposes_quality_review(planner, store):
    _assign(store, "t1")
    _to_verifying(store, "t1")
    store.record_review(
        "t1", stage="spec", verdict="PASS", reviewer="noesis-sentinel",
        candidate_revision="cand-1", findings=[],
    )
    decision = planner.plan("t1")
    assert decision.disposition is PlannerDisposition.next_action_proposal
    assert decision.proposed_action == "dispatch_quality_review"


def test_request_changes_blocks_on_review(planner, store):
    _assign(store, "t1")
    _to_verifying(store, "t1")
    store.record_review(
        "t1", stage="spec", verdict="PASS", reviewer="noesis-sentinel",
        candidate_revision="cand-1", findings=[],
    )
    store.record_review(
        "t1", stage="quality", verdict="REQUEST_CHANGES", reviewer="noesis-skeptic",
        candidate_revision="cand-1", findings=[{"severity": "minor", "note": "style"}],
    )
    # Remediation is owned by the assignee while its lease is live: wait, do
    # not dispatch a second attempt.
    assert planner.plan("t1").disposition is PlannerDisposition.wait_for_existing_attempt
    # Lease expiry marks the attempt suspect; the failing verdict gates progress.
    store.expire_lease("t1")
    decision = planner.plan("t1")
    assert decision.disposition is PlannerDisposition.blocked_on_review


def test_blocked_state_is_blocked_on_review(planner, store):
    _assign(store, "t1")
    _to_verifying(store, "t1")
    store.record_review(
        "t1", stage="spec", verdict="BLOCKED", reviewer="noesis-sentinel",
        candidate_revision="cand-1", findings=[],
    )
    decision = planner.plan("t1")
    assert decision.disposition is PlannerDisposition.blocked_on_review


def test_contradicting_reviews_fail_closed(planner, store):
    """Externally-ingested ledger rows may contradict; the planner preserves
    the contradiction and refuses to resolve it by itself."""
    _assign(store, "t1")
    _to_verifying(store, "t1")
    store.record_review(
        "t1", stage="spec", verdict="PASS", reviewer="noesis-sentinel",
        candidate_revision="cand-1", findings=[],
    )
    task = store.get("t1")
    # Inject a contradicting spec row (same stage+candidate, different verdict),
    # as an external projection ingest could.
    contradicted = replace(task)
    contradicted.reviews = task.reviews + [
        Review("spec", "REQUEST_CHANGES", "noesis-sentinel", "cand-1", [])
    ]
    contradicted.version = task.version + 1
    store._tasks["t1"] = contradicted
    store._append(contradicted)

    decision = planner.plan("t1")
    assert decision.disposition is PlannerDisposition.blocked_on_review
    assert "contradicting spec verdicts" in decision.reason
    assert any(r.detail.get("contradicts") for r in decision.source_references)


# --------------------------------------------------------------------------
# Authorization
# --------------------------------------------------------------------------

def test_missing_approval_blocks_on_authorization(planner, store):
    _assign(store, "t1", payload={"approval_required": True})
    _accept_ready(store, "t1")
    decision = planner.plan("t1")
    assert decision.disposition is PlannerDisposition.blocked_on_authorization
    assert decision.proposed_action is None
    assert "revalidate" in decision.authority_note


def test_malformed_approval_data_fails_closed(planner, store):
    # A non-boolean grant value is malformed authority data, never a grant.
    _assign(store, "t1", payload={"approval_required": True, "approval_granted": "pending"})
    _accept_ready(store, "t1")
    decision = planner.plan("t1")
    assert decision.disposition is PlannerDisposition.blocked_on_authorization
    auth_refs = [r for r in decision.source_references if r.record == "approval:required"]
    assert auth_refs[0].detail["malformed"] is True


def test_granted_approval_proposes_accept(planner, store):
    _assign(store, "t1", payload={"approval_required": True, "approval_granted": True})
    _accept_ready(store, "t1")
    decision = planner.plan("t1")
    assert decision.disposition is PlannerDisposition.next_action_proposal
    assert decision.proposed_action == "orchestrator_accept"


# --------------------------------------------------------------------------
# Completion
# --------------------------------------------------------------------------

def test_accepted_task_is_completed_within_scope(planner, store):
    _assign(store, "t1")
    _accept_ready(store, "t1")
    store.accept("t1", by=ORCHESTRATOR_ID)
    decision = planner.plan("t1")
    assert decision.disposition is PlannerDisposition.completed_within_scope
    assert decision.proposed_action is None


# --------------------------------------------------------------------------
# No-progress detection
# --------------------------------------------------------------------------

def test_no_progress_detection(planner, store):
    _assign(store, "t1")
    first = planner.plan("t1")
    second = planner.plan("t1", previous=first)
    assert second.no_progress is True
    # A planner-visible change (candidate submitted) resets the flag.
    store.mark_ready("t1", by=ORCHESTRATOR_ID)
    store.start_running("t1", by="noesis-forge")
    store.submit_verifying("t1", by="noesis-forge", expected_attempt_epoch=1, candidate_revision="cand-1")
    third = planner.plan("t1", previous=second)
    assert third.no_progress is False
    assert third.disposition is PlannerDisposition.next_action_proposal


# --------------------------------------------------------------------------
# Read-only guarantee + source references
# --------------------------------------------------------------------------

def test_planning_never_mutates_authoritative_state(planner, store):
    _assign(store, "t1")
    _accept_ready(store, "t1")
    ledger_before, versions_before = _snapshot(store)
    for _ in range(5):
        for tid in ("t1", "missing"):
            planner.plan(tid)
    ledger_after, versions_after = _snapshot(store)
    assert ledger_after == ledger_before
    assert versions_after == versions_before


def test_every_decision_carries_source_references(planner, store):
    _assign(store, "t1")
    store.mark_ready("t1", by=ORCHESTRATOR_ID)
    store.start_running("t1", by="noesis-forge")
    scenarios = {
        "wait": planner.plan("t1"),
    }
    store.submit_verifying("t1", by="noesis-forge", expected_attempt_epoch=1, candidate_revision="cand-1")
    scenarios["spec_proposal"] = planner.plan("t1")
    store.record_review("t1", stage="spec", verdict="PASS", reviewer="noesis-sentinel",
                        candidate_revision="cand-1", findings=[])
    scenarios["quality_proposal"] = planner.plan("t1")
    store.record_review("t1", stage="quality", verdict="APPROVED", reviewer="noesis-skeptic",
                        candidate_revision="cand-1", findings=[])
    scenarios["accept_proposal"] = planner.plan("t1")
    store.accept("t1", by=ORCHESTRATOR_ID)
    scenarios["completed"] = planner.plan("t1")
    scenarios["missing"] = planner.plan("nope")

    for name, decision in scenarios.items():
        assert decision.source_references, f"{name} decision lacks source references"
        for ref in decision.source_references:
            assert ref.source_artifact == store.ledger_path.name
            assert ref.record


# --------------------------------------------------------------------------
# P1 fail-closed regressions (Sentinel 2026-10-11)
# --------------------------------------------------------------------------

_BLOCKED = {
    PlannerDisposition.blocked_on_evidence,
    PlannerDisposition.blocked_on_review,
    PlannerDisposition.blocked_on_authorization,
}


def test_expired_or_revoked_authority_does_not_propose_action(planner, store):
    """F1: expired/revoked authority never yields an action proposal."""
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()

    _assign(store, "t-verify-expire")
    _to_verifying(store, "t-verify-expire")
    store.expire_lease("t-verify-expire")
    d1a = planner.plan("t-verify-expire")
    assert d1a.disposition in _BLOCKED
    assert d1a.proposed_action is None

    _assign(store, "t-running-cand")
    store.mark_ready("t-running-cand", by=ORCHESTRATOR_ID)
    store.start_running("t-running-cand", by="noesis-forge")
    task = store.get("t-running-cand")
    store._tasks["t-running-cand"] = replace(
        task,
        lease=Lease(
            holder=task.lease.holder,
            epoch=task.lease.epoch,
            issued_at=task.lease.issued_at,
            expires_at=past,
            revoked=False,
        ),
        candidate_revision="cand-1",
    )
    d1c = planner.plan("t-running-cand")
    assert d1c.disposition in _BLOCKED
    assert d1c.proposed_action is None

    _assign(store, "t-verify-revoked")
    _to_verifying(store, "t-verify-revoked")
    task = store.get("t-verify-revoked")
    store._tasks["t-verify-revoked"] = replace(
        task,
        lease=Lease(
            holder=task.lease.holder,
            epoch=task.lease.epoch,
            issued_at=task.lease.issued_at,
            expires_at=task.lease.expires_at,
            revoked=True,
        ),
    )
    d1d = planner.plan("t-verify-revoked")
    assert d1d.disposition in _BLOCKED
    assert d1d.proposed_action is None


def test_non_bool_approval_required_fails_closed(planner, store):
    """F2: non-bool approval_required fails closed, never orchestrator_accept."""
    for i, value in enumerate(("yes", 1, None, {"flag": True}, ["true"]), start=1):
        tid = f"t-req-{i}"
        _assign(store, tid, payload={"approval_required": value, "approval_granted": False})
        _accept_ready(store, tid)
        decision = planner.plan(tid)
        assert decision.disposition is PlannerDisposition.blocked_on_authorization, value
        assert decision.proposed_action is None, value
        assert decision.disposition is not PlannerDisposition.next_action_proposal


def test_replay_rejects_non_bool_accepted_and_revoked(tmp_path):
    """F3: replay must not bool()-coerce malformed accepted/revoked flags."""
    now = datetime.now(timezone.utc).isoformat()
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

    def _row(task_id: str, *, accepted, revoked, state: str = "completed") -> str:
        task = {
            "task_id": task_id,
            "contract_revision": "rev-1",
            "root_task_id": "root-1",
            "correlation_id": "corr-1",
            "assignee_profile": "noesis-forge",
            "required_capability": "code_modify",
            "intent": "x",
            "acceptance_criteria": ["tests pass"],
            "contract": {},
            "state": state,
            "version": 1,
            "assignment_epoch": 1,
            "candidate_revision": "cand-1",
            "lease": {
                "holder": "noesis-forge",
                "epoch": 1,
                "issued_at": now,
                "expires_at": past if state == "running" else now,
                "revoked": revoked,
            },
            "reviews": [],
            "accepted": accepted,
            "last_event": "injected",
            "idempotency_key": f"idem-{task_id}",
            "budget_reserved": 0.0,
            "budget_consumed": 0.0,
            "created_at": now,
            "updated_at": now,
        }
        return json.dumps({"task": task})

    assert bool("false") is True  # the coercion this test forbids

    for task_id, extra in (
        ("accepted_str_false", {"accepted": "false", "revoked": False, "state": "completed"}),
        ("accepted_invalid", {"accepted": "invalid", "revoked": False, "state": "completed"}),
        ("revoked_str_false", {"accepted": False, "revoked": "false", "state": "running"}),
    ):
        ledger = tmp_path / f"{task_id}.jsonl"
        ledger.write_text(
            _row(task_id, accepted=extra["accepted"], revoked=extra["revoked"], state=extra["state"])
            + "\n",
            encoding="utf-8",
        )
        with pytest.raises(ActivationError, match="must be a boolean"):
            DelegationStore(ledger)


def test_no_progress_includes_task_version(planner, store):
    """F4: same output/state but a different task.version is not no-progress."""
    _assign(store, "t-ver")
    first = planner.plan("t-ver")
    task = store.get("t-ver")
    store._tasks["t-ver"] = replace(task, version=task.version + 5)
    second = planner.plan("t-ver", previous=first)
    assert second.no_progress is False
    assert second.disposition is not PlannerDisposition.completed_within_scope


def test_missing_task_is_blocked_with_source_reference(planner, store):
    """F5: a missing task fails closed with a ledger citation (n_refs != 0)."""
    decision = planner.plan("missing")
    assert decision.disposition is PlannerDisposition.blocked_on_evidence
    assert decision.proposed_action is None
    assert len(decision.source_references) >= 1
    assert decision.source_references[0].record == "missing"
    assert decision.source_references[0].detail["present"] is False
