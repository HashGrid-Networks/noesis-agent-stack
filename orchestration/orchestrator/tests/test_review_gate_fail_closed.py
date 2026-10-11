"""Regressions for BLOCKED review verdicts failing closed.

These tests intentionally exercise both the pure delegation predicate and the
real control-plane acceptance seams so a BLOCKED verdict can never be coerced
into success for any required review stage.
"""
from __future__ import annotations

import pytest

from app.control_plane import Orchestrator, PolicyViolation
from app.models import Delegation, ReviewRecord, Verification


def _handoff() -> dict:
    return {
        "summary": "candidate produced",
        "artifacts": [{"path": "workspace/orchestrator/bootstrap-repair-evidence/out.txt"}],
        "verification_result": {"passed": True, "evidence": "offline regression evidence"},
        "unmet_criteria": [],
    }


def _running_review_task(orch: Orchestrator, *, idem: str = "review-gate"):
    task = orch.propose(
        title="Review-gated repair",
        intent="Implement a bounded local repair with independent review gates.",
        assignee_profile="noesis-forge",
        required_capability="code_modify",
        acceptance_criteria=["review gates are positive only"],
        verification=Verification(method="automated_test"),
        idempotency_key=idem,
        risk_tier="r1",
        reviewer_profile="noesis-sentinel",
        timeout_s=900,
    )
    return orch.mark_dispatched(task.task_id, runtime_profile="noesis-forge")


def _candidate_ready(orch: Orchestrator, *, idem: str = "review-gate"):
    task = _running_review_task(orch, idem=idem)
    orch.submit_handoff(task.task_id, _handoff())
    orch.submit_candidate(task.task_id, candidate_revision="rev-1", candidate_digest="sha256:rev-1")
    return orch.store.get(task.task_id)


@pytest.mark.parametrize(
    ("stage", "verdict", "expected"),
    [
        ("spec", "PASS", True),
        ("quality", "APPROVED", True),
        ("quality", "PASS", False),
        ("spec", "APPROVED", False),
        ("spec", "REQUEST_CHANGES", False),
        ("quality", "REQUEST_CHANGES", False),
        ("spec", "BLOCKED", False),
        ("quality", "BLOCKED", False),
        ("spec", "", False),
        ("quality", "UNKNOWN", False),
        ("integration", "APPROVED", False),
    ],
)
def test_required_review_satisfied_is_positive_stage_specific(stage, verdict, expected):
    delegation = Delegation(candidate_revision="rev-1")
    delegation.reviews.append(
        ReviewRecord(
            stage=stage,
            verdict=verdict,
            reviewer="noesis-sentinel",
            candidate_revision="rev-1",
        )
    )

    assert delegation.required_review_satisfied(stage) is expected


@pytest.mark.parametrize("bad_verdict", [None, [], {}, "UNKNOWN", ""])
def test_malformed_or_unknown_verdicts_never_satisfy_predicate(bad_verdict):
    delegation = Delegation(candidate_revision="rev-1")
    delegation.reviews.append(
        ReviewRecord(
            stage="spec",
            verdict=bad_verdict,  # type: ignore[arg-type]
            reviewer="noesis-sentinel",
            candidate_revision="rev-1",
        )
    )

    assert delegation.required_review_satisfied("spec") is False


def test_quality_cannot_start_after_spec_blocked(orch):
    task = _candidate_ready(orch)
    orch.record_review(
        task.task_id,
        stage="spec",
        verdict="BLOCKED",
        reviewer="noesis-sentinel",
        candidate_revision="rev-1",
    )

    with pytest.raises(PolicyViolation, match="spec_required"):
        orch.record_review(
            task.task_id,
            stage="quality",
            verdict="APPROVED",
            reviewer="noesis-skeptic",
            candidate_revision="rev-1",
        )


def test_acceptance_cannot_complete_with_spec_blocked(orch):
    task = _candidate_ready(orch, idem="review-gate:spec-blocked")
    orch.record_review(
        task.task_id,
        stage="spec",
        verdict="BLOCKED",
        reviewer="noesis-sentinel",
        candidate_revision="rev-1",
    )

    with pytest.raises(PolicyViolation, match="spec_review_required"):
        orch.accept(task.task_id)


def test_acceptance_cannot_complete_with_quality_blocked(orch):
    task = _candidate_ready(orch, idem="review-gate:quality-blocked")
    orch.record_review(
        task.task_id,
        stage="spec",
        verdict="PASS",
        reviewer="noesis-sentinel",
        candidate_revision="rev-1",
    )
    orch.record_review(
        task.task_id,
        stage="quality",
        verdict="BLOCKED",
        reviewer="noesis-skeptic",
        candidate_revision="rev-1",
    )

    with pytest.raises(PolicyViolation, match="quality_review_required"):
        orch.accept(task.task_id)


def test_succeed_cannot_bypass_required_review_gates(orch):
    task = _candidate_ready(orch, idem="review-gate:succeed-bypass")
    orch.record_review(
        task.task_id,
        stage="spec",
        verdict="BLOCKED",
        reviewer="noesis-sentinel",
        candidate_revision="rev-1",
    )

    with pytest.raises(PolicyViolation, match="spec_review_required|review_required"):
        orch.succeed(task.task_id)


def test_succeed_refuses_reviewer_gated_handoff_without_candidate(orch):
    """A reviewer-gated handoff with no candidate and no reviews cannot succeed."""
    task = _running_review_task(orch, idem="review-gate:succeed-no-candidate")
    orch.submit_handoff(task.task_id, _handoff())
    parked = orch.store.get(task.task_id)
    assert parked is not None
    assert parked.state == "awaiting_review"
    assert parked.reviewer_profile == "noesis-sentinel"
    assert parked.delegation.candidate_revision is None
    assert parked.delegation.reviews == []

    with pytest.raises(PolicyViolation, match="spec_review_required") as exc:
        orch.succeed(task.task_id)

    assert exc.value.code == "spec_review_required"
    stored = orch.store.get(task.task_id)
    assert stored is not None
    assert stored.state == "awaiting_review"
    assert stored.state != "succeeded"


def test_same_candidate_label_changed_digest_does_not_retain_approval(orch):
    task = _candidate_ready(orch, idem="review-gate:digest-change")
    orch.record_review(
        task.task_id,
        stage="spec",
        verdict="PASS",
        reviewer="noesis-sentinel",
        candidate_revision="rev-1",
    )
    orch.record_review(
        task.task_id,
        stage="quality",
        verdict="APPROVED",
        reviewer="noesis-skeptic",
        candidate_revision="rev-1",
    )

    live = orch.store.get(task.task_id)
    granted = orch.grant_new_attempt(
        task.task_id,
        expected_task_version=live.task_version,
        intended_candidate_revision="rev-1",
    )
    orch.submit_candidate(
        task.task_id,
        candidate_revision="rev-1",
        candidate_digest="sha256:changed",
        attempt_epoch=granted.delegation.attempt_epoch,
    )

    with pytest.raises(PolicyViolation, match="spec_review_required"):
        orch.accept(task.task_id)


def test_stale_candidate_cannot_pass(orch):
    task = _candidate_ready(orch, idem="review-gate:stale")
    with pytest.raises(PolicyViolation, match="stale_candidate"):
        orch.record_review(
            task.task_id,
            stage="spec",
            verdict="PASS",
            reviewer="noesis-sentinel",
            candidate_revision="rev-stale",
        )


def test_unsupported_stage_fails_closed(orch):
    task = _candidate_ready(orch, idem="review-gate:unsupported")
    assert task is not None
    with pytest.raises(PolicyViolation, match="unknown_review_stage"):
        orch.record_review(
            task.task_id,
            stage="security",
            verdict="APPROVED",
            reviewer="noesis-sentinel",
            candidate_revision="rev-1",
        )


def test_control_plane_rejects_malformed_verdict(orch):
    task = _candidate_ready(orch, idem="review-gate:malformed")
    with pytest.raises(PolicyViolation, match="invalid_verdict"):
        orch.record_review(
            task.task_id,
            stage="spec",
            verdict=["BLOCKED"],  # type: ignore[arg-type]
            reviewer="noesis-sentinel",
            candidate_revision="rev-1",
        )


def test_unresolved_critical_finding_blocks_completion(orch):
    task = _candidate_ready(orch, idem="review-gate:critical")
    orch.record_review(
        task.task_id,
        stage="spec",
        verdict="PASS",
        reviewer="noesis-sentinel",
        candidate_revision="rev-1",
    )
    orch.record_review(
        task.task_id,
        stage="quality",
        verdict="APPROVED",
        reviewer="noesis-skeptic",
        candidate_revision="rev-1",
        findings=[{"severity": "critical", "resolved": False}],
    )
    with pytest.raises(PolicyViolation, match="open_findings"):
        orch.accept(task.task_id)


def test_unresolved_important_finding_blocks_completion(orch):
    task = _candidate_ready(orch, idem="review-gate:finding")
    orch.record_review(
        task.task_id,
        stage="spec",
        verdict="PASS",
        reviewer="noesis-sentinel",
        candidate_revision="rev-1",
    )
    orch.record_review(
        task.task_id,
        stage="quality",
        verdict="APPROVED",
        reviewer="noesis-skeptic",
        candidate_revision="rev-1",
        findings=[{"severity": "important", "resolved": False}],
    )

    with pytest.raises(PolicyViolation, match="open_findings"):
        orch.accept(task.task_id)
