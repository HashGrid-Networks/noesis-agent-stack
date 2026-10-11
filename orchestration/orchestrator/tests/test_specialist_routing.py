"""Specialist-first dispatch regression tests.

These tests exercise the orchestrator entrypoint, not just a standalone helper:
classification, profile selection, policy admission, launcher target resolution,
launcher-side profile attestation, and blocked results all pass through
``Orchestrator.route_and_launch``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.control_plane import Orchestrator
from app.inference_routing import EnforcerConfig, InferenceRoutingEnforcer, REPO_ROOT
from app.launcher import LaunchAttestation, LaunchResult, RuntimeAdapter
from app.models import Verification
from app.specialist_routing import CapabilityIndex, RoutingBlocked


class FakeHermesAdapter(RuntimeAdapter):
    def __init__(self, *, installed: set[str] | None = None, loaded_as: str | None = None, fail: bool = False) -> None:
        self.installed = installed or set()
        self.loaded_as = loaded_as
        self.fail = fail
        self.calls: list[dict] = []

    def profile_installed(self, profile_id: str) -> bool:
        return profile_id in self.installed

    def launch(self, *, profile_id: str, prompt: str, task_id: str, cwd: str | None = None, dry_run: bool = True) -> LaunchResult:
        self.calls.append({"profile_id": profile_id, "prompt": prompt, "task_id": task_id, "cwd": cwd, "dry_run": dry_run})
        if self.fail:
            return LaunchResult(
                ok=False,
                requested_profile=profile_id,
                loaded_profile=None,
                command=["hermes", "-p", profile_id, "profile", "show", profile_id],
                attestation=None,
                error="simulated launch failure",
            )
        loaded = self.loaded_as or profile_id
        return LaunchResult(
            ok=True,
            requested_profile=profile_id,
            loaded_profile=loaded,
            command=["hermes", "-p", profile_id, "profile", "show", profile_id],
            attestation=LaunchAttestation(
                requested_profile=profile_id,
                loaded_profile=loaded,
                runtime="hermes",
                profile_path=f"/fake/profiles/{loaded}",
                manifest_sha256="deadbeef",
            ),
            error=None,
        )


ELIGIBLE = {
    "noesis-steward",
    "noesis-cartographer",
    "noesis-forge",
    "noesis-scribe",
    "noesis-signal",
    "noesis-substrate",
    "noesis-tracer",
    "noesis-ledger",
    "noesis-grid",
    "noesis-quill",
    "noesis-advocate",
    "noesis-herald",
    "noesis-architect",
    "noesis-sentinel",
}


def orch_with_fake(tmp_path: Path, installed: set[str] | None = None, *, enforcer: InferenceRoutingEnforcer | None = None, **adapter_kwargs) -> tuple[Orchestrator, FakeHermesAdapter]:
    adapter = FakeHermesAdapter(installed=ELIGIBLE if installed is None else installed, **adapter_kwargs)
    index = CapabilityIndex.from_repo(adapter=adapter)
    orch = Orchestrator(
        tmp_path / "tasks.jsonl",
        capability_index=index,
        runtime_adapter=adapter,
        inference_enforcer=enforcer,
    )
    return orch, adapter


def enforce_enforcer(tmp_path: Path) -> InferenceRoutingEnforcer:
    cfg = EnforcerConfig(
        mode="enforce",
        fail_closed=True,
        policy_path=REPO_ROOT / "platform/inference-routing.yaml",
        reconciliation_path=REPO_ROOT / "platform/runtime-reconciliation.yaml",
        models_path=REPO_ROOT / "shared/models.yaml",
        aliases_path=REPO_ROOT / "shared/model-aliases.yaml",
        provider_policy_path=REPO_ROOT / "shared/provider-policies.yaml",
        risk_tiers_path=REPO_ROOT / "platform/risk-tiers.yaml",
        decision_log_path=tmp_path / "routing-decisions.jsonl",
        reload_seconds=60,
        allowlist=(),
        emergency_disable=False,
    )
    enforcer = InferenceRoutingEnforcer(cfg)
    enforcer.set_profile_overrides(
        "noesis-signal",
        {
            "observed_runtime": {"provider_lane": "KIMI_CODE", "model_alias_or_id": "kimi-k2.7-code"},
            "repo_declared": {"provider_lane": "KIMI_CODE", "model_alias_or_id": "kimi-k2.7-code"},
            "resolution": {"state": "aligned", "required_gates": []},
        },
    )
    return enforcer


@pytest.mark.parametrize(
    ("intent", "expected"),
    [
        ("Design an MCP/Hermes profile contract for a new memory agent", "noesis-architect"),
        ("Research market pricing and return a cited brief", "noesis-signal"),
        ("Implement the approved Python patch and tests", "noesis-forge"),
        ("Implement a deterministic input-validation function with tests", "noesis-forge"),
        ("Review the patch independently for security and correctness", "noesis-sentinel"),
        ("Diagnose Docker/Kubernetes deployment health and observability", "noesis-substrate"),
        ("Write the operator runbook and changelog", "noesis-quill"),
        ("Analyze this CSV and produce charts", "noesis-grid"),
        ("Draft external customer comms but do not send", "noesis-herald"),
        ("Prepare legal advocacy research support with citations", "noesis-advocate"),
        ("Investigate wallet protocol and on-chain evidence", "noesis-ledger"),
        ("Build a public-source OSINT timeline with provenance", "noesis-tracer"),
        ("Create a dependency map and phased plan", "noesis-cartographer"),
        ("triage priorities and produce a status digest", "noesis-steward"),
        ("archive the knowledge base with citation preservation", "noesis-scribe"),
    ],
)
def test_representative_tasks_launch_intended_specialist(tmp_path: Path, intent: str, expected: str) -> None:
    orch, adapter = orch_with_fake(tmp_path)

    result = orch.route_and_launch(
        title=f"route {expected}",
        intent=intent,
        acceptance_criteria=["bounded output returned"],
        verification=Verification(method="synthetic"),
        idempotency_key=f"route:{expected}",
        dry_run=True,
    )

    assert result.blocked is False
    assert result.task is not None
    assert result.task.assignee_profile == expected
    assert result.launch is not None and result.launch.ok is True
    assert result.launch.requested_profile == expected
    assert result.launch.loaded_profile == expected
    assert adapter.calls[-1]["profile_id"] == expected


def test_input_validation_phrase_is_implementation_not_review(tmp_path: Path) -> None:
    """Regression for the Subgoal 5 rehearsal finding.

    A dry-run through the real CLI entrypoint classified 'Implement a
    deterministic port-range validation function with tests' as review /
    code_review and selected noesis-sentinel. 'validation' is a coding-domain
    term (input/form/schema validation); the review keyword rule no longer
    claims it, so implementation intents reach noesis-forge.
    """
    from app.specialist_routing import TaskClassifier

    cls = TaskClassifier().classify(
        "Implement a deterministic port-range validation function with tests."
    )
    assert cls.task_class == "implementation"
    # Review intent is still captured by the remaining keywords.
    review_cls = TaskClassifier().classify(
        "Independently review this patch for security and correctness"
    )
    assert review_cls.task_class == "review"


def test_architecture_does_not_fall_back_to_default_or_coder(tmp_path: Path) -> None:
    orch, _ = orch_with_fake(tmp_path)
    result = orch.route_and_launch(
        title="architecture route",
        intent="Design a multi-agent routing architecture and prompt contract",
        acceptance_criteria=["design produced"],
        verification=Verification(method="synthetic"),
        idempotency_key="route:architecture:not-coder",
        dry_run=True,
    )
    assert result.task is not None
    assert result.task.assignee_profile == "noesis-architect"
    assert result.task.assignee_profile not in {"default", "coder", "noesis-orchestrator"}


def test_every_policy_eligible_specialist_has_a_routing_fixture(tmp_path: Path) -> None:
    orch, _ = orch_with_fake(tmp_path)
    coverage = orch.routing_coverage()
    missing = [row["profile_id"] for row in coverage if row["policy_eligible"] and not row["has_fixture"]]
    assert missing == []


def test_ineligible_specialists_report_reasons_instead_of_disappearing(tmp_path: Path) -> None:
    adapter = FakeHermesAdapter(installed={"noesis-signal"})
    index = CapabilityIndex.from_repo(adapter=adapter)
    row = index.record("noesis-forge")
    assert row is not None
    assert row.installed is False
    assert row.launchable is False
    assert "profile_not_installed" in row.eligibility_reasons


def test_missing_unknown_or_unauthorized_profile_fails_explicitly(tmp_path: Path) -> None:
    orch, _ = orch_with_fake(tmp_path, installed=set())
    with pytest.raises(RoutingBlocked) as exc:
        orch.route_and_launch(
            title="unknown route",
            intent="Implement the approved Python patch and tests",
            acceptance_criteria=["patch returned"],
            verification=Verification(method="synthetic"),
            idempotency_key="route:missing-profile",
            dry_run=True,
        )
    assert exc.value.code == "no_eligible_specialist"
    assert "profile_not_installed" in str(exc.value.rejections)


def test_launch_failure_does_not_inherit_parent_default_or_coder(tmp_path: Path) -> None:
    orch, _ = orch_with_fake(tmp_path, fail=True)
    with pytest.raises(RoutingBlocked) as exc:
        orch.route_and_launch(
            title="launch fails",
            intent="Research the evidence and produce a cited brief",
            acceptance_criteria=["brief returned"],
            verification=Verification(method="synthetic"),
            idempotency_key="route:launch-fails",
            dry_run=True,
        )
    assert exc.value.code == "launch_failed"
    assert "simulated launch failure" in exc.value.reason


def test_loaded_profile_mismatch_is_blocked(tmp_path: Path) -> None:
    orch, _ = orch_with_fake(tmp_path, loaded_as="noesis-orchestrator")
    with pytest.raises(RoutingBlocked) as exc:
        orch.route_and_launch(
            title="identity mismatch",
            intent="Implement the approved Python patch and tests",
            acceptance_criteria=["patch returned"],
            verification=Verification(method="synthetic"),
            idempotency_key="route:identity-mismatch",
            dry_run=True,
        )
    assert exc.value.code == "loaded_profile_mismatch"
    assert "noesis-orchestrator" in exc.value.reason


def test_self_delegation_and_cycles_are_blocked(tmp_path: Path) -> None:
    orch, _ = orch_with_fake(tmp_path)
    with pytest.raises(RoutingBlocked) as exc:
        orch.route_and_launch(
            title="self clone",
            intent="Create another noesis-orchestrator to run this same goal",
            acceptance_criteria=["must not clone"],
            verification=Verification(method="synthetic"),
            idempotency_key="route:self-clone",
            dry_run=True,
            max_depth=0,
            parent_task_id="00000000-0000-0000-0000-000000000000",
        )
    assert exc.value.code in {"depth_exceeded", "self_delegation_blocked", "no_eligible_specialist"}


def test_model_policy_gate_still_applies_after_profile_selection(tmp_path: Path) -> None:
    orch, _ = orch_with_fake(tmp_path, enforcer=enforce_enforcer(tmp_path))
    result = orch.route_and_launch(
        title="privacy denied later",
        intent="Research the evidence and produce a cited brief",
        acceptance_criteria=["brief returned"],
        verification=Verification(method="synthetic"),
        idempotency_key="route:policy-gate",
        dry_run=True,
        inference_routing_context={"data_classification": "secret"},
    )
    assert result.task is not None
    assert result.launch is not None
    orch.enqueue(result.task.task_id)
    decision = orch.dispatchable(result.task.task_id)
    assert decision.allowed is False
    assert "data_classification_denied" in decision.reason


def test_mixed_domain_work_produces_bounded_specialist_graph(tmp_path: Path) -> None:
    orch, _ = orch_with_fake(tmp_path)
    graph = orch.plan_specialist_graph(
        root_title="route mixed domain",
        intents=[
            "Research the sources for a cited brief",
            "Implement the approved Python patch and tests",
            "Review the implementation independently",
        ],
        max_fanout=3,
    )
    assert [node.selected_profile for node in graph.nodes] == ["noesis-signal", "noesis-forge", "noesis-sentinel"]
    assert graph.bounded is True


def test_generic_fallback_requires_explicit_policy_allowance(tmp_path: Path) -> None:
    orch, _ = orch_with_fake(tmp_path, installed={"coder"})
    with pytest.raises(RoutingBlocked):
        orch.route_and_launch(
            title="generic blocked",
            intent="Implement a tiny code patch",
            acceptance_criteria=["patch returned"],
            verification=Verification(method="synthetic"),
            idempotency_key="route:generic-blocked",
            dry_run=True,
        )

    result = orch.route_and_launch(
        title="generic allowed",
        intent="Implement a tiny code patch",
        acceptance_criteria=["patch returned"],
        verification=Verification(method="synthetic"),
        idempotency_key="route:generic-allowed",
        dry_run=True,
        allow_generic_fallback=True,
    )
    assert result.task is not None
    assert result.task.assignee_profile == "coder"
    assert result.fallback_reason is not None
