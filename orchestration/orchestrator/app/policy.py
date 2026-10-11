"""Policy enforcement for the noesis-orchestrator control plane.

Every rule here is a *refusal*: the orchestrator is a planner, dispatcher,
supervisor, and synthesizer, so its only real power is deciding what must not
proceed. Rules are deterministic and independent of model output.

Enforced:
  1. Assignee must exist in profiles/noesis-roster.yaml.
  2. Assignee must declare the required capability in agents/<name>/agent.yaml.
  3. Reviewer-only and supervisor profiles may never be assigned executing work.
  4. Irreversible operations force r3 and require a narrowly privileged
     executor that actually has terminal authority.
  5. r2+ and every irreversible task require a human approval gate before
     leaving 'proposed'; r3 additionally requires an approval manifest id.
  6. Unresolved dependencies block dispatch.
  7. Structured handoff evidence is mandatory before 'succeeded'.
  8. A task whose lease expired cannot succeed silently.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app import registry
from app.models import TaskContract

# Risk tiers at or above which a human approval gate is mandatory.
APPROVAL_REQUIRED_TIERS = frozenset({"r2", "r3"})


class PolicyViolation(RuntimeError):
    """Raised when a task contract violates a control-plane policy."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class DispatchDecision:
    allowed: bool
    reason: str


# ---------------------------------------------------------------- admission --


def validate_assignment(task: TaskContract) -> None:
    """Reject an assignment that no roster profile can legitimately perform."""
    profile = registry.get_profile(task.assignee_profile)
    if profile is None:
        raise PolicyViolation(
            "unknown_profile",
            f"'{task.assignee_profile}' is not a known roster profile, "
            f"orchestration lane, or alias",
        )

    if profile.source == "agent-registry" and profile.activation_state != "activated":
        raise PolicyViolation(
            "lane_not_activated",
            f"'{profile.name}' is registered but not activated for orchestration",
        )

    if profile.is_reviewer_only:
        if "review" not in task.labels:
            raise PolicyViolation(
                "reviewer_only_profile_cannot_execute",
                f"'{profile.name}' is reviewer-only and cannot be assigned executing work",
            )

    if profile.is_supervisor:
        raise PolicyViolation(
            "supervisor_cannot_execute",
            f"'{profile.name}' is a supervisor profile and must delegate, not execute",
        )

    if profile.capabilities and task.required_capability not in profile.capabilities:
        raise PolicyViolation(
            "capability_mismatch",
            f"'{profile.name}' does not declare capability "
            f"'{task.required_capability}' (declared: {sorted(profile.capabilities)})",
        )

    if task.reviewer_profile is not None:
        reviewer = registry.get_profile(task.reviewer_profile)
        if reviewer is None:
            raise PolicyViolation(
                "unknown_reviewer_profile",
                f"'{task.reviewer_profile}' is not defined in the roster",
            )
        if not reviewer.is_reviewer_only:
            raise PolicyViolation(
                "reviewer_must_be_reviewer_only",
                f"'{reviewer.name}' is not a reviewer-only profile",
            )


def validate_risk(task: TaskContract) -> None:
    """Irreversible work must be r3, approval-gated, and narrowly privileged."""
    if task.irreversible_operations:
        if task.risk_tier != "r3":
            raise PolicyViolation(
                "irreversible_requires_r3",
                f"operations {sorted(task.irreversible_operations)} require risk_tier r3, "
                f"got '{task.risk_tier}'",
            )
        profile = registry.get_profile(task.assignee_profile)
        if profile is not None and not profile.has_terminal:
            raise PolicyViolation(
                "executor_lacks_privilege",
                f"'{profile.name}' has no terminal/code_execution authority and cannot "
                f"be the executor for irreversible operations",
            )

    if task.risk_tier in APPROVAL_REQUIRED_TIERS or task.irreversible_operations:
        if task.approval is None or not task.approval.required:
            raise PolicyViolation(
                "approval_gate_required",
                f"risk tier '{task.risk_tier}' requires approval.required = true",
            )


def validate_admission(task: TaskContract) -> None:
    """Full admission check applied when a task contract is created."""
    validate_assignment(task)
    validate_risk(task)


# ------------------------------------------------------------------ gating --


def check_approval(task: TaskContract) -> DispatchDecision:
    approval = task.approval
    if approval is None or not approval.required:
        return DispatchDecision(True, "no approval required")
    if approval.state == "denied":
        return DispatchDecision(False, "approval denied by operator")
    if approval.state != "granted":
        return DispatchDecision(False, f"awaiting human approval (state={approval.state})")
    if not approval.approved_by:
        return DispatchDecision(False, "approval granted without an operator identity")
    if task.risk_tier == "r3" and not approval.approval_manifest_id:
        return DispatchDecision(
            False, "r3 approval requires a bound approval manifest id"
        )
    return DispatchDecision(True, "approval granted")


def check_dependencies(
    task: TaskContract, all_tasks: dict[str, TaskContract]
) -> DispatchDecision:
    """Unresolved or failed dependencies block dispatch."""
    unresolved: list[str] = []
    for dep_id in task.depends_on:
        dep = all_tasks.get(str(dep_id))
        if dep is None:
            return DispatchDecision(False, f"dependency {dep_id} does not exist")
        if dep.state != "succeeded":
            unresolved.append(f"{dep_id}({dep.state})")
    if unresolved:
        return DispatchDecision(False, f"unresolved dependencies: {', '.join(unresolved)}")
    return DispatchDecision(True, "dependencies satisfied")


def check_dispatchable(
    task: TaskContract, all_tasks: dict[str, TaskContract]
) -> DispatchDecision:
    """Combined gate evaluated before a task may be claimed."""
    approval = check_approval(task)
    if not approval.allowed:
        return approval
    return check_dependencies(task, all_tasks)


# ------------------------------------------------------------- completion --


def validate_handoff(task: TaskContract, *, now: datetime | None = None) -> None:
    """Refuse a silent or unevidenced success."""
    if task.lease_expired(now=now):
        raise PolicyViolation(
            "stale_task_cannot_succeed",
            f"task {task.task_id} exceeded its {task.timeout_s}s lease and must be "
            f"re-queued or failed, not completed",
        )

    handoff = task.handoff
    if not handoff:
        raise PolicyViolation(
            "handoff_required", "structured handoff evidence is required before success"
        )

    for required in ("summary", "artifacts", "verification_result"):
        if required not in handoff:
            raise PolicyViolation(
                "handoff_incomplete", f"handoff is missing required field '{required}'"
            )

    result = handoff["verification_result"]
    if not result.get("passed"):
        raise PolicyViolation(
            "verification_failed", "verification_result.passed is false"
        )
    if task.verification.evidence_required and not str(result.get("evidence", "")).strip():
        raise PolicyViolation(
            "evidence_required", "verification evidence is required but empty"
        )

    if handoff.get("unmet_criteria"):
        raise PolicyViolation(
            "acceptance_criteria_unmet",
            f"unmet acceptance criteria: {handoff['unmet_criteria']}",
        )

    if task.reviewer_profile and task.state != "awaiting_review":
        raise PolicyViolation(
            "review_required",
            f"task requires sign-off by '{task.reviewer_profile}' before success",
        )
