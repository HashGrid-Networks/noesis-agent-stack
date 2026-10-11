"""Worked orchestration examples using real roster profiles.

These are executable documentation: each mirrors a scenario in WORKFLOWS.md
§5 (noesis-orchestrator) and proves the control plane behaves as documented.

  1. Parallel analysis and synthesis  — noesis-signal + noesis-tracer -> noesis-quill
  2. Plan-review-execute with approval — noesis-cartographer -> noesis-forge (+ sentinel)
  3. Incident handling                 — noesis-substrate with timeout, retry,
                                          circuit breaker, and escalation
"""

from datetime import timedelta
from uuid import uuid4

from app.control_plane import Orchestrator
from app.models import Verification, utcnow


def _handoff(summary: str, evidence: str) -> dict:
    return {
        "summary": summary,
        "artifacts": [],
        "verification_result": {"passed": True, "evidence": evidence},
        "unmet_criteria": [],
    }


def _run(orch, task, summary="Completed as specified.", evidence="verified"):
    orch.enqueue(task.task_id)
    orch.claim(task.task_id, claimed_by=task.assignee_profile)
    orch.start(task.task_id)
    orch.submit_handoff(task.task_id, _handoff(summary, evidence))
    return orch.succeed(task.task_id)


class TestExampleParallelAnalysisAndSynthesis:
    """Two independent analysts run in parallel; a writer synthesizes both."""

    def test_fan_out_then_synthesize(self, ledger):
        orch = Orchestrator(ledger)
        graph = uuid4()

        research = orch.propose(
            title="Research 2026 hashprice trend",
            intent="Collect and cross-validate hashprice sources for 2026 to date.",
            assignee_profile="noesis-signal",
            required_capability="cross_validate",
            acceptance_criteria=["At least three independent sources", "Claims labelled"],
            verification=Verification(method="reviewer_signoff"),
            idempotency_key="noesis-signal:cross_validate:hashprice:2026-09-18",
            correlation_id=graph,
            risk_tier="r0",
        )
        timeline = orch.propose(
            title="Build public timeline of grid-curtailment events",
            intent="Assemble a sourced timeline of 2026 ERCOT curtailment events.",
            assignee_profile="noesis-tracer",
            required_capability="timeline_construct",
            acceptance_criteria=["Every entry carries a public source"],
            verification=Verification(method="reviewer_signoff"),
            idempotency_key="noesis-tracer:timeline_construct:curtailment:2026-09-18",
            correlation_id=graph,
            risk_tier="r0",
        )

        # Both are dispatchable immediately: no interdependency.
        orch.enqueue(research.task_id)
        orch.enqueue(timeline.task_id)
        assert orch.dispatchable(research.task_id).allowed is True
        assert orch.dispatchable(timeline.task_id).allowed is True

        synthesis = orch.propose(
            title="Synthesize hashprice + curtailment brief",
            intent="Merge the research and timeline outputs into one operator brief.",
            assignee_profile="noesis-quill",
            required_capability="technical_writing",
            acceptance_criteria=["Brief cites both upstream artifacts"],
            verification=Verification(method="human_inspection"),
            idempotency_key="noesis-quill:technical_writing:brief:2026-09-18",
            correlation_id=graph,
            depends_on=[research.task_id, timeline.task_id],
            risk_tier="r0",
        )

        # The synthesizer is gated until BOTH analysts succeed.
        orch.enqueue(synthesis.task_id)
        assert orch.dispatchable(synthesis.task_id).allowed is False

        for task in (research, timeline):
            orch.claim(task.task_id, claimed_by=task.assignee_profile)
            orch.start(task.task_id)
            orch.submit_handoff(task.task_id, _handoff(f"{task.title} done", "sources cited"))
            orch.succeed(task.task_id)

        assert orch.dispatchable(synthesis.task_id).allowed is True
        orch.claim(synthesis.task_id, claimed_by="noesis-quill")
        orch.start(synthesis.task_id)
        orch.submit_handoff(synthesis.task_id, _handoff("Brief written", "both artifacts cited"))
        orch.succeed(synthesis.task_id)

        result = orch.synthesize(graph)
        assert result["complete"] is True
        assert result["task_count"] == 3
        assert len(result["evidence"]) == 3


class TestExamplePlanReviewExecuteWithApproval:
    """Decompose, review, then execute a staging change behind a human gate."""

    def test_plan_then_gated_execution(self, ledger):
        orch = Orchestrator(ledger)
        graph = uuid4()

        plan = orch.propose(
            title="Decompose the broker deployment change",
            intent="Produce a phased task graph for the staging broker rollout.",
            assignee_profile="noesis-cartographer",
            required_capability="plan_artifact",
            acceptance_criteria=["Phases, dependencies, and owners are explicit"],
            verification=Verification(method="reviewer_signoff"),
            idempotency_key="noesis-cartographer:plan_artifact:broker-rollout:2026-09-18",
            correlation_id=graph,
            risk_tier="r0",
        )
        _run(orch, plan, "Phased plan produced.", "3 phases, owners assigned")

        execute = orch.propose(
            title="Apply the staging CI change",
            intent="Implement the approved staging CI pipeline change per the plan.",
            assignee_profile="noesis-forge",
            required_capability="ci_cd",
            acceptance_criteria=["Pipeline green on staging"],
            verification=Verification(method="automated_test", command="pytest -q"),
            idempotency_key="noesis-forge:ci_cd:staging-rollout:2026-09-18",
            correlation_id=graph,
            depends_on=[plan.task_id],
            reviewer_profile="noesis-sentinel",
            risk_tier="r2",
        )

        # r2 work will not queue without an explicit human approval.
        assert execute.approval is not None and execute.approval.required is True
        assert orch.check_gate_blocked(execute.task_id) is True

        orch.approve(execute.task_id, operator="elvis")
        orch.enqueue(execute.task_id)
        assert orch.dispatchable(execute.task_id).allowed is True

        orch.claim(execute.task_id, claimed_by="noesis-forge")
        orch.start(execute.task_id)
        orch.submit_handoff(execute.task_id, _handoff("Pipeline updated.", "pytest: 45 passed"))

        # Reviewer-gated work parks in awaiting_review before it may succeed.
        parked = orch.store.get(execute.task_id)
        assert parked is not None and parked.state == "awaiting_review"
        orch.submit_candidate(execute.task_id, candidate_revision="rev-1", candidate_digest="sha256:rev-1", attempt_epoch=1)
        orch.record_review(execute.task_id, stage="spec", verdict="PASS",
                           reviewer="noesis-sentinel", candidate_revision="rev-1")
        orch.record_review(execute.task_id, stage="quality", verdict="APPROVED",
                           reviewer="noesis-skeptic", candidate_revision="rev-1")
        assert orch.succeed(execute.task_id).state == "succeeded"

        result = orch.synthesize(graph)
        assert result["complete"] is True


class TestExampleIncidentHandling:
    """A flaky infra task times out, retries, trips the breaker, and escalates."""

    def test_timeout_retry_breaker_escalation(self, ledger):
        orch = Orchestrator(ledger, failure_threshold=2)
        graph = uuid4()

        incident = orch.propose(
            title="Restore degraded broker health check",
            intent="Diagnose and restore the failing broker /v1/health endpoint on staging.",
            assignee_profile="noesis-substrate",
            required_capability="observability",
            acceptance_criteria=["Health endpoint returns ok"],
            verification=Verification(method="command_output", command="curl -sf /v1/health"),
            idempotency_key="noesis-substrate:observability:broker-health:2026-09-18",
            correlation_id=graph,
            risk_tier="r1",
            timeout_s=60,
            max_attempts=2,
        )

        # Attempt 1 hangs past its lease and is swept.
        orch.enqueue(incident.task_id)
        orch.claim(incident.task_id, claimed_by="noesis-substrate")
        orch.start(incident.task_id)
        orch.sweep_timeouts(now=utcnow() + timedelta(seconds=120))
        requeued = orch.store.get(incident.task_id)
        assert requeued is not None and requeued.state == "queued"
        assert orch.breaker.consecutive_failures == 1

        # Attempt 2 also fails; the retry budget is now exhausted.
        orch.claim(incident.task_id, claimed_by="noesis-substrate")
        orch.start(incident.task_id)
        orch.fail(incident.task_id, reason="health endpoint still failing")

        # Two consecutive failures trip the breaker: dispatch halts fleet-wide.
        assert orch.breaker.is_open is True

        followup = orch.propose(
            title="Follow-up remediation attempt",
            intent="Second remediation pass on the broker health check.",
            assignee_profile="noesis-substrate",
            required_capability="observability",
            acceptance_criteria=["Health endpoint returns ok"],
            verification=Verification(method="command_output", command="curl -sf /v1/health"),
            idempotency_key="noesis-substrate:observability:broker-health-2:2026-09-18",
            correlation_id=graph,
            risk_tier="r1",
        )
        orch.enqueue(followup.task_id)
        assert orch.dispatchable(followup.task_id).allowed is False

        # The incident escalates to a human rather than looping.
        record = orch.escalate(incident.task_id, reason="retry budget exhausted")
        assert record["requires_human"] is True
        assert record["circuit_breaker_open"] is True
        assert record["attempts"] == 2

        # Operator inspects, then resumes deliberately.
        orch.resume(operator="elvis")
        assert orch.dispatchable(followup.task_id).allowed is True
