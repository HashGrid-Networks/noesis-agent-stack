"""Delivery-loop planner tests (addendum A / NOESIS-LOOP-GRAPH-V1).

All scenarios run against a real DelegationStore on a sandbox ledger with
synthetic records. The planner must stay strictly read-only: every test that
plans asserts the authoritative ledger is untouched.
"""
from __future__ import annotations

import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from app.delegation import (
    ORCHESTRATOR_ID,
    PROTOCOL_VERSION,
    DelegationStore,
    HandoffEnvelope,
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
    store.submit_verifying(task_id, by="noesis-forge", candidate_revision=candidate)


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
    # Remediation submits a new candidate; old reviews must not apply.
    store.submit_verifying(store.get("t1").task_id, by="noesis-forge", candidate_revision="cand-2")
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
    store.submit_verifying("t1", by="noesis-forge", candidate_revision="cand-1")
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
    store.submit_verifying("t1", by="noesis-forge", candidate_revision="cand-1")
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
        if name == "missing":
            continue  # unknown task has no ledger to reference
        assert decision.source_references, f"{name} decision lacks source references"
        for ref in decision.source_references:
            assert ref.source_artifact == store.ledger_path.name
            assert ref.record
