"""State machine, dependency gating, handoff, idempotency, and staleness tests.

Covers ORCH-005..ORCH-012 in EVALS.platform.yaml.
"""

from datetime import timedelta
from uuid import uuid4

import pytest

from app.control_plane import Orchestrator
from app.models import TransitionError, utcnow
from app.policy import PolicyViolation
from tests.conftest import drive_to_running, make_task


class TestStateMachine:
    def test_happy_path_transitions(self, orch, passing_handoff):
        task = make_task(orch)
        assert task.state == "proposed"
        orch.enqueue(task.task_id)
        assert orch.store.get(task.task_id).state == "queued"
        orch.claim(task.task_id, claimed_by="noesis-signal")
        assert orch.store.get(task.task_id).state == "claimed"
        orch.start(task.task_id)
        assert orch.store.get(task.task_id).state == "running"
        orch.submit_handoff(task.task_id, passing_handoff)
        final = orch.succeed(task.task_id)
        assert final.state == "succeeded"

    def test_illegal_transition_is_refused(self, orch):
        """A proposed task cannot jump straight to running."""
        task = make_task(orch)
        with pytest.raises(TransitionError):
            task.transition_to("running")

    def test_succeeded_is_terminal(self, orch, passing_handoff):
        task = make_task(orch)
        drive_to_running(orch, task)
        orch.submit_handoff(task.task_id, passing_handoff)
        orch.succeed(task.task_id)
        with pytest.raises(TransitionError):
            orch.store.get(task.task_id).transition_to("running")

    def test_claim_by_wrong_profile_is_rejected(self, orch):
        task = make_task(orch)
        orch.enqueue(task.task_id)
        with pytest.raises(PolicyViolation) as exc:
            orch.claim(task.task_id, claimed_by="noesis-forge")
        assert exc.value.code == "claim_by_wrong_profile"

    def test_cancel_from_any_active_state(self, orch):
        task = make_task(orch)
        drive_to_running(orch, task)
        cancelled = orch.cancel(task.task_id, reason="operator stopped the graph")
        assert cancelled.state == "cancelled"
        assert "operator stopped" in cancelled.terminal_reason


class TestDependencyGating:
    def test_unresolved_dependency_blocks_dispatch(self, orch):
        upstream = make_task(orch)
        downstream = make_task(
            orch,
            title="Summarize validated sources",
            assignee_profile="noesis-quill",
            required_capability="technical_writing",
            idempotency_key="noesis-quill:technical_writing:summary:2026-09-18",
            depends_on=[upstream.task_id],
        )
        orch.enqueue(downstream.task_id)
        decision = orch.dispatchable(downstream.task_id)
        assert decision.allowed is False
        assert "unresolved dependencies" in decision.reason

    def test_dependency_satisfied_unblocks_dispatch(self, orch, passing_handoff):
        upstream = make_task(orch)
        downstream = make_task(
            orch,
            title="Summarize validated sources",
            assignee_profile="noesis-quill",
            required_capability="technical_writing",
            idempotency_key="noesis-quill:technical_writing:summary:2026-09-18",
            depends_on=[upstream.task_id],
        )
        drive_to_running(orch, upstream)
        orch.submit_handoff(upstream.task_id, passing_handoff)
        orch.succeed(upstream.task_id)

        orch.enqueue(downstream.task_id)
        assert orch.dispatchable(downstream.task_id).allowed is True

    def test_missing_dependency_is_rejected(self, orch):
        task = make_task(orch, depends_on=[uuid4()])
        orch.enqueue(task.task_id)
        decision = orch.dispatchable(task.task_id)
        assert decision.allowed is False
        assert "does not exist" in decision.reason


class TestApprovalGate:
    def test_high_risk_task_cannot_be_queued_without_approval(self, orch):
        task = make_task(
            orch,
            assignee_profile="noesis-substrate",
            required_capability="terraform",
            risk_tier="r3",
            irreversible_operations=["production_mutation"],
            idempotency_key="noesis-substrate:terraform:prod:2026-09-18",
        )
        with pytest.raises(PolicyViolation) as exc:
            orch.enqueue(task.task_id)
        assert exc.value.code == "approval_gate_required"

    def test_r3_approval_requires_a_bound_manifest(self, orch):
        task = make_task(
            orch,
            assignee_profile="noesis-substrate",
            required_capability="terraform",
            risk_tier="r3",
            irreversible_operations=["production_mutation"],
            idempotency_key="noesis-substrate:terraform:prod:2026-09-18",
        )
        with pytest.raises(PolicyViolation) as exc:
            orch.approve(task.task_id, operator="elvis")
        assert exc.value.code == "approval_invalid"

    def test_r3_proceeds_with_operator_and_manifest(self, orch):
        task = make_task(
            orch,
            assignee_profile="noesis-substrate",
            required_capability="terraform",
            risk_tier="r3",
            irreversible_operations=["production_mutation"],
            idempotency_key="noesis-substrate:terraform:prod:2026-09-18",
        )
        approved = orch.approve(
            task.task_id, operator="elvis", approval_manifest_id=str(uuid4())
        )
        assert approved.state == "approved"
        assert approved.approval.approved_by == "elvis"
        orch.enqueue(task.task_id)
        assert orch.dispatchable(task.task_id).allowed is True

    def test_denied_approval_cancels_the_task(self, orch):
        task = make_task(
            orch,
            assignee_profile="noesis-forge",
            required_capability="ci_cd",
            risk_tier="r2",
            idempotency_key="noesis-forge:ci_cd:staging:2026-09-18",
        )
        denied = orch.deny(task.task_id, operator="elvis", reason="scope too broad")
        assert denied.state == "cancelled"
        assert "approval denied" in denied.terminal_reason


class TestHandoffEvidence:
    def test_success_without_handoff_is_refused(self, orch):
        task = make_task(orch)
        drive_to_running(orch, task)
        with pytest.raises(PolicyViolation) as exc:
            orch.succeed(task.task_id)
        assert exc.value.code == "handoff_required"

    def test_failed_verification_is_refused(self, orch):
        task = make_task(orch)
        drive_to_running(orch, task)
        orch.submit_handoff(
            task.task_id,
            {
                "summary": "Attempted the cross-validation pass.",
                "artifacts": [],
                "verification_result": {"passed": False, "evidence": "only one source"},
            },
        )
        with pytest.raises(PolicyViolation) as exc:
            orch.succeed(task.task_id)
        assert exc.value.code == "verification_failed"

    def test_empty_evidence_is_refused(self, orch):
        task = make_task(orch)
        drive_to_running(orch, task)
        orch.submit_handoff(
            task.task_id,
            {
                "summary": "Cross-validation complete.",
                "artifacts": [],
                "verification_result": {"passed": True, "evidence": "   "},
            },
        )
        with pytest.raises(PolicyViolation) as exc:
            orch.succeed(task.task_id)
        assert exc.value.code == "evidence_required"

    def test_unmet_acceptance_criteria_is_refused(self, orch):
        task = make_task(orch)
        drive_to_running(orch, task)
        orch.submit_handoff(
            task.task_id,
            {
                "summary": "Partial cross-validation only.",
                "artifacts": [],
                "verification_result": {"passed": True, "evidence": "two sources"},
                "unmet_criteria": ["At least three independent sources cited"],
            },
        )
        with pytest.raises(PolicyViolation) as exc:
            orch.succeed(task.task_id)
        assert exc.value.code == "acceptance_criteria_unmet"

    def test_reviewer_signoff_routes_through_awaiting_review(self, orch, passing_handoff):
        task = make_task(orch, reviewer_profile="noesis-skeptic")
        drive_to_running(orch, task)
        orch.submit_handoff(task.task_id, passing_handoff)
        assert orch.store.get(task.task_id).state == "awaiting_review"
        orch.submit_candidate(task.task_id, candidate_revision="rev-1", candidate_digest="sha256:rev-1", attempt_epoch=1)
        orch.record_review(task.task_id, stage="spec", verdict="PASS",
                           reviewer="noesis-sentinel", candidate_revision="rev-1")
        orch.record_review(task.task_id, stage="quality", verdict="APPROVED",
                           reviewer="noesis-skeptic", candidate_revision="rev-1")
        assert orch.succeed(task.task_id).state == "succeeded"


class TestStaleWork:
    def test_expired_lease_cannot_succeed_silently(self, orch, passing_handoff):
        task = make_task(orch, timeout_s=60)
        drive_to_running(orch, task)
        orch.submit_handoff(task.task_id, passing_handoff)
        later = utcnow() + timedelta(seconds=120)
        with pytest.raises(PolicyViolation) as exc:
            orch.succeed(task.task_id, now=later)
        assert exc.value.code == "stale_task_cannot_succeed"

    def test_sweep_fails_and_requeues_stale_work(self, orch):
        task = make_task(orch, timeout_s=60, max_attempts=2)
        drive_to_running(orch, task)
        swept = orch.sweep_timeouts(now=utcnow() + timedelta(seconds=120))
        assert len(swept) == 1
        assert orch.store.get(task.task_id).state == "queued"

    def test_sweep_leaves_exhausted_retries_failed(self, orch):
        task = make_task(orch, timeout_s=60, max_attempts=1)
        drive_to_running(orch, task)
        orch.sweep_timeouts(now=utcnow() + timedelta(seconds=120))
        assert orch.store.get(task.task_id).state == "failed"

    def test_retry_beyond_budget_is_refused(self, orch):
        task = make_task(orch, timeout_s=60, max_attempts=1)
        drive_to_running(orch, task)
        orch.fail(task.task_id, reason="worker error")
        with pytest.raises(PolicyViolation) as exc:
            orch.retry(task.task_id)
        assert exc.value.code == "retry_budget_exhausted"


class TestIdempotencyAndDurability:
    def test_redelivery_does_not_duplicate_work(self, orch):
        first = make_task(orch)
        second = make_task(orch)
        assert first.task_id == second.task_id
        assert len(orch.store.all_tasks()) == 1

    def test_state_survives_restart(self, ledger, passing_handoff):
        orch = Orchestrator(ledger)
        task = make_task(orch)
        drive_to_running(orch, task)
        orch.submit_handoff(task.task_id, passing_handoff)
        orch.succeed(task.task_id)

        reloaded = Orchestrator(ledger)
        restored = reloaded.store.get(task.task_id)
        assert restored is not None
        assert restored.state == "succeeded"
        assert restored.handoff is not None
        assert restored.handoff["verification_result"]["passed"] is True

    def test_redelivery_after_restart_still_dedupes(self, ledger):
        orch = Orchestrator(ledger)
        first = make_task(orch)
        reloaded = Orchestrator(ledger)
        second = make_task(reloaded)
        assert first.task_id == second.task_id
        assert len(reloaded.store.all_tasks()) == 1

    def test_ledger_is_append_only(self, ledger):
        orch = Orchestrator(ledger)
        task = make_task(orch)
        lines_after_propose = ledger.read_text().count("\n")
        orch.enqueue(task.task_id)
        assert ledger.read_text().count("\n") > lines_after_propose


class TestCircuitBreaker:
    def test_breaker_opens_after_threshold(self, ledger):
        orch = Orchestrator(ledger, failure_threshold=2)
        for i in range(2):
            task = make_task(
                orch,
                idempotency_key=f"noesis-signal:cross_validate:probe-{i}:2026-09-18",
                max_attempts=1,
            )
            drive_to_running(orch, task)
            orch.fail(task.task_id, reason="worker error")
        assert orch.breaker.is_open is True

    def test_open_breaker_blocks_dispatch(self, ledger):
        orch = Orchestrator(ledger, failure_threshold=1)
        blocked = make_task(
            orch, idempotency_key="noesis-signal:cross_validate:blocked:2026-09-18"
        )
        orch.enqueue(blocked.task_id)
        orch.emergency_stop(operator="elvis", reason="upstream provider outage")
        decision = orch.dispatchable(blocked.task_id)
        assert decision.allowed is False
        assert "circuit breaker open" in decision.reason

    def test_resume_reopens_dispatch(self, orch):
        task = make_task(orch)
        orch.enqueue(task.task_id)
        orch.emergency_stop(operator="elvis", reason="manual stop")
        orch.resume(operator="elvis")
        assert orch.dispatchable(task.task_id).allowed is True

    def test_success_resets_failure_count(self, ledger, passing_handoff):
        orch = Orchestrator(ledger, failure_threshold=3)
        failing = make_task(
            orch, idempotency_key="noesis-signal:cross_validate:f1:2026-09-18", max_attempts=1
        )
        drive_to_running(orch, failing)
        orch.fail(failing.task_id, reason="transient error")
        assert orch.breaker.consecutive_failures == 1

        ok = make_task(orch, idempotency_key="noesis-signal:cross_validate:ok:2026-09-18")
        drive_to_running(orch, ok)
        orch.submit_handoff(ok.task_id, passing_handoff)
        orch.succeed(ok.task_id)
        assert orch.breaker.consecutive_failures == 0

    def test_escalation_record_requires_human(self, orch):
        task = make_task(orch, max_attempts=1)
        drive_to_running(orch, task)
        orch.fail(task.task_id, reason="worker unreachable")
        record = orch.escalate(task.task_id, reason="retry budget exhausted")
        assert record["requires_human"] is True
        assert record["assignee_profile"] == "noesis-signal"
