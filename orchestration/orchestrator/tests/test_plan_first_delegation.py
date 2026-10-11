"""Plan-first, capability-based decomposition tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.control_plane import Orchestrator
from app.planning import PlanningBlocked
from app.specialist_dispatch_cli import _DeclaredProfilesAdapter

ELIGIBLE = [
    "noesis-architect",
    "noesis-forge",
    "noesis-sentinel",
    "noesis-skeptic",
    "noesis-signal",
    "noesis-substrate",
    "noesis-quill",
    "noesis-cartographer",
]


def _orch(tmp_path: Path, installed: list[str] | None = None) -> Orchestrator:
    adapter = _DeclaredProfilesAdapter(installed if installed is not None else ELIGIBLE)
    return Orchestrator(
        ledger_path=tmp_path / "tasks.jsonl",
        runtime_adapter=adapter,
        capability_index=None,
        routing_event_log_path=tmp_path / "routing-events.jsonl",
    )


def test_nontrivial_request_is_decomposed_and_assigned_before_dispatch(tmp_path: Path) -> None:
    orch = _orch(tmp_path)

    plan = orch.plan_request(
        "Design the Hermes profile contract, implement the routing patch with tests, "
        "and write the operator rollout notes.",
        idempotency_key="plan:mixed-routing-work",
        risk_tier="r1",
    )

    assert plan.status == "validated"
    assert [task.task_class for task in plan.tasks] == ["agent_architecture", "implementation", "writing"]
    assert [task.assignee_profile for task in plan.tasks] == ["noesis-architect", "noesis-forge", "noesis-quill"]
    assert all(task.acceptance_criteria for task in plan.tasks)
    assert all(task.dispatch_allowed is False for task in plan.tasks)
    assert plan.execution_gate == "passed"
    assert plan.fast_path_reason is None


def test_atomic_request_uses_documented_fast_path(tmp_path: Path) -> None:
    orch = _orch(tmp_path)

    plan = orch.plan_request(
        "Write the operator rollout notes for specialist routing.",
        idempotency_key="plan:atomic-docs",
        risk_tier="r0",
    )

    assert len(plan.tasks) == 1
    assert plan.tasks[0].assignee_profile == "noesis-quill"
    assert plan.fast_path_reason == "atomic_low_risk_single_specialist"
    assert plan.status == "validated"


def test_planning_gate_blocks_missing_specialist_without_generic_fallback(tmp_path: Path) -> None:
    orch = _orch(tmp_path, installed=["noesis-forge", "noesis-sentinel"])

    with pytest.raises(PlanningBlocked) as excinfo:
        orch.plan_request(
            "Design the Hermes profile contract for a new specialist.",
            idempotency_key="plan:missing-architect",
            risk_tier="r0",
        )

    assert excinfo.value.code == "no_eligible_owner"
    assert "noesis-architect" in excinfo.value.rejections
    assert "profile_not_installed" in excinfo.value.rejections["noesis-architect"]


def test_planning_gate_blocks_overlapping_mutation_targets(tmp_path: Path) -> None:
    orch = _orch(tmp_path)

    with pytest.raises(PlanningBlocked) as excinfo:
        orch.plan_request(
            "Implement the routing patch in orchestration/orchestrator/app/control_plane.py "
            "and update the Python tests for orchestration/orchestrator/app/control_plane.py.",
            idempotency_key="plan:overlap",
            risk_tier="r1",
        )

    assert excinfo.value.code == "workspace_conflict"


def test_coding_plan_exports_subagent_development_contracts(tmp_path: Path) -> None:
    orch = _orch(tmp_path)

    plan = orch.plan_request(
        "Implement the specialist routing patch with tests.",
        idempotency_key="plan:coding-sdd",
        risk_tier="r1",
    )

    assert plan.tasks[0].assignee_profile == "noesis-forge"
    assert plan.tasks[0].workflow == "subagent-driven-development"
    assert plan.to_sdd_plan_text().startswith("Task T1:")
    assert "Acceptance:" in plan.to_sdd_plan_text()
