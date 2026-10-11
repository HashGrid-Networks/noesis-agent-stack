"""Policy tests: incompatible assignments and risk gating are rejected.

Covers ORCH-001..ORCH-004 in EVALS.platform.yaml.
"""

import pytest

from app import registry
from app.models import Verification
from app.policy import PolicyViolation
from tests.conftest import make_task


class TestRosterBackedAssignment:
    def test_roster_loads_real_profiles(self):
        assert registry.profile_exists("noesis-signal")
        assert registry.profile_exists("noesis-forge")
        assert registry.profile_exists("noesis-sentinel")

    def test_unknown_profile_is_rejected(self, orch):
        with pytest.raises(PolicyViolation) as exc:
            make_task(orch, assignee_profile="noesis-imaginary")
        assert exc.value.code == "unknown_profile"

    def test_reviewer_only_profile_cannot_be_assigned_work(self, orch):
        """noesis-sentinel is reviewer-only: it reviews, it never executes."""
        with pytest.raises(PolicyViolation) as exc:
            make_task(
                orch,
                assignee_profile="noesis-sentinel",
                required_capability="code_review",
            )
        assert exc.value.code == "reviewer_only_profile_cannot_execute"

    def test_skeptic_is_also_reviewer_only(self, orch):
        with pytest.raises(PolicyViolation) as exc:
            make_task(
                orch,
                assignee_profile="noesis-skeptic",
                required_capability="adversarial_review",
            )
        assert exc.value.code == "reviewer_only_profile_cannot_execute"

    def test_supervisor_profile_cannot_be_assigned_work(self, orch):
        """A supervisor delegates; it must not be handed executing work."""
        with pytest.raises(PolicyViolation) as exc:
            make_task(orch, assignee_profile="noesis-orchestrator", required_capability="route")
        assert exc.value.code == "supervisor_cannot_execute"

    def test_orchestrator_cannot_assign_work_to_itself(self, orch):
        """The control plane is least-privilege: it dispatches, it never executes."""
        with pytest.raises(PolicyViolation) as exc:
            make_task(
                orch,
                assignee_profile="noesis-orchestrator",
                required_capability="dispatch_task",
            )
        assert exc.value.code == "supervisor_cannot_execute"

    def test_orchestrator_holds_no_execution_toolset(self):
        """Least privilege is enforced by the roster, not by prompt text alone."""
        profile = registry.get_profile("noesis-orchestrator")
        assert profile is not None
        assert "terminal" not in profile.toolsets
        assert "code_execution" not in profile.toolsets
        assert profile.has_terminal is False
        assert profile.can_execute is False

    def test_capability_mismatch_is_rejected(self, orch):
        """noesis-quill writes documentation; it cannot be assigned terraform work."""
        with pytest.raises(PolicyViolation) as exc:
            make_task(
                orch,
                assignee_profile="noesis-quill",
                required_capability="terraform",
            )
        assert exc.value.code == "capability_mismatch"

    def test_matching_capability_is_accepted(self, orch):
        task = make_task(
            orch,
            assignee_profile="noesis-quill",
            required_capability="technical_writing",
            idempotency_key="noesis-quill:technical_writing:runbook:2026-09-18",
        )
        assert task.state == "proposed"

    def test_reviewer_must_be_reviewer_only(self, orch):
        with pytest.raises(PolicyViolation) as exc:
            make_task(orch, reviewer_profile="noesis-forge")
        assert exc.value.code == "reviewer_must_be_reviewer_only"


class TestRiskAndIrreversibility:
    def test_irreversible_operation_requires_r3(self, orch):
        with pytest.raises(PolicyViolation) as exc:
            make_task(
                orch,
                assignee_profile="noesis-substrate",
                required_capability="terraform",
                risk_tier="r1",
                irreversible_operations=["production_mutation"],
            )
        assert exc.value.code == "irreversible_requires_r3"

    def test_irreversible_executor_must_hold_terminal_authority(self, orch):
        """noesis-herald drafts comms and has no terminal: it cannot execute r3 work."""
        with pytest.raises(PolicyViolation) as exc:
            make_task(
                orch,
                assignee_profile="noesis-herald",
                required_capability="comms_draft",
                risk_tier="r3",
                irreversible_operations=["external_publication"],
            )
        assert exc.value.code == "executor_lacks_privilege"

    def test_r3_with_privileged_executor_is_admitted_but_gated(self, orch):
        task = make_task(
            orch,
            assignee_profile="noesis-substrate",
            required_capability="terraform",
            risk_tier="r3",
            irreversible_operations=["production_mutation"],
            idempotency_key="noesis-substrate:terraform:prod-apply:2026-09-18",
        )
        assert task.approval.required is True
        assert task.approval.state == "pending"

    def test_r2_forces_an_approval_gate(self, orch):
        task = make_task(
            orch,
            assignee_profile="noesis-forge",
            required_capability="ci_cd",
            risk_tier="r2",
            idempotency_key="noesis-forge:ci_cd:staging:2026-09-18",
        )
        assert task.approval.required is True

    def test_r0_needs_no_approval(self, orch):
        task = make_task(orch)
        assert task.approval.required is False
        assert task.approval.state == "not_required"

    def test_unknown_irreversible_operation_is_rejected(self, orch):
        with pytest.raises(ValueError):
            make_task(orch, risk_tier="r3", irreversible_operations=["launch_missiles"])

    def test_empty_acceptance_criteria_is_rejected(self, orch):
        with pytest.raises(ValueError):
            make_task(orch, acceptance_criteria=[])
