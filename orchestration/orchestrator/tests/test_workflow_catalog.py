"""Tests for the proposed workflow catalog layer and the advisory agency registry adapter.

Covers design validation checks V1-V6 (catalog -> contract mapping, capability
resolution, advisory privilege boundaries, reviewer-gate eligibility,
dependency enforcement, workspace conflicts) plus idempotent emission.
All tests run offline against tmp_path ledgers and the committed catalog.
"""

from __future__ import annotations

import pytest

from app import registry
from app.agency_registry import CURATION_PATH, load_agency_registry
from app.control_plane import Orchestrator
from app.models import Verification
from app.policy import PolicyViolation
from app.workflow_catalog import (
    CATALOG_PATH,
    admit_workflow,
    load_catalog,
    validate_workflow,
)


# ------------------------------------------------------------- fixtures --


@pytest.fixture()
def orch(tmp_path):
    return Orchestrator(tmp_path / "tasks.jsonl")


def _verification(reviewer: str | None = None) -> Verification:
    return Verification(
        method="reviewer_signoff" if reviewer else "human_inspection",
        evidence_required=True,
    )


# ------------------------------------------------- registry adapter (B1) --


class TestAgencyRegistryAdapter:
    def test_adapter_registry_matches_curation_roster(self):
        """Every curated agency profile is registry-visible under agency-<slug>."""
        curated = load_agency_registry()
        assert len(curated) == 18
        for name, record in curated.items():
            assert name.startswith("agency-")
            assert record.source == "agency-curation"
            assert record.activation_state == "staged"
            assert record.status == "curated"

    def test_agency_profiles_declare_only_converter_capabilities(self):
        """Capabilities come from curation.yaml's declared_capabilities — never invented."""
        curated = load_agency_registry()
        declared = set()
        for record in curated.values():
            declared |= set(record.capabilities)
        # The converter emits exactly this set into every generated agent.yaml.
        assert declared == {"advisory", "review", "analysis"}

    def test_agency_profiles_have_no_terminal_authority(self):
        curated = load_agency_registry()
        for record in curated.values():
            assert not record.has_terminal
            assert record.tier == "advisory"
            assert not record.is_reviewer_only
            assert not record.is_supervisor

    def test_agency_profiles_visible_through_canonical_registry(self):
        profiles, _aliases = registry.load_registry()
        agency = {n: p for n, p in profiles.items() if p.source == "agency-curation"}
        assert len(agency) == 18
        assert "agency-security-architect" in agency

    def test_adapter_fails_closed_without_declared_capabilities(self, monkeypatch):
        import app.agency_registry as ar

        class _FakePath:
            def read_text(self, *a, **k):
                return "profile_prefix: agency\nagents: []\n"

        monkeypatch.setattr(ar, "CURATION_PATH", _FakePath())
        with pytest.raises(ValueError, match="declared_capabilities"):
            ar.load_agency_registry()


# ----------------------------------------------------------- catalog load --


class TestCatalogLoading:
    def test_six_workflows_present(self):
        catalog = load_catalog()
        ids = {wf.workflow_id for wf in catalog.workflows}
        assert ids == {
            "wf-arch-security-decision",
            "wf-repo-archaeology",
            "wf-infra-reliability",
            "wf-evidence-synthesis",
            "wf-product-discovery",
            "wf-telecom-edge-feasibility",
        }

    def test_all_workflows_validate_clean(self):
        catalog = load_catalog()
        for wf in catalog.workflows:
            issues = validate_workflow(catalog, wf.workflow_id)
            assert issues == [], f"{wf.workflow_id}: {issues}"

    def test_catalog_dag_is_acyclic(self):
        catalog = load_catalog()
        for wf in catalog.workflows:
            order = wf.topological_order()
            assert len(order) == len(wf.nodes)

    def test_shared_controls_present(self):
        catalog = load_catalog()
        assert catalog.shared_controls["max_tasks_per_workflow"] >= 1
        assert catalog.shared_controls["live_inference_default"] is False


# -------------------------------------------- catalog validation (V1-V6) --


def _catalog_with_node(mutate):
    from app.workflow_catalog import Catalog, CatalogNode, CatalogWorkflow

    base = load_catalog()
    wf = base.workflows[0]
    nodes = [CatalogNode(**{**n.__dict__, "depends_on": list(n.depends_on)}) for n in wf.nodes]
    mutate(nodes)
    broken = CatalogWorkflow(
        workflow_id="wf-broken",
        description="test",
        nodes=nodes,
    )
    return Catalog(
        version=base.version,
        status=base.status,
        shared_controls=base.shared_controls,
        workflows=[broken],
    )


class TestCatalogValidation:
    def test_cycle_detected(self):
        def mutate(nodes):
            nodes[0].depends_on = [nodes[-1].node_id]

        catalog = _catalog_with_node(mutate)
        issues = validate_workflow(catalog, "wf-broken")
        assert any("cycle" in i for i in issues)

    def test_unknown_dependency_detected(self):
        def mutate(nodes):
            nodes[1].depends_on = ["does-not-exist"]

        catalog = _catalog_with_node(mutate)
        issues = validate_workflow(catalog, "wf-broken")
        assert any("unknown dependency" in i for i in issues)

    def test_unbound_capability_detected(self):
        def mutate(nodes):
            nodes[1].required_capability = "threat_modeling"

        catalog = _catalog_with_node(mutate)
        issues = validate_workflow(catalog, "wf-broken")
        assert any("capability" in i for i in issues)

    def test_agency_node_above_r0_detected(self):
        def mutate(nodes):
            for n in nodes:
                if n.advisory:
                    n.risk_tier = "r1"
                    break

        catalog = _catalog_with_node(mutate)
        issues = validate_workflow(catalog, "wf-broken")
        assert any("advisory" in i and "r0" in i for i in issues)

    def test_workspace_conflict_detected(self):
        def mutate(nodes):
            nodes[0].workspace = "shared/dir"
            nodes[1].workspace = "shared/dir"
            # no dependency ordering between 0 and 1 -> conflict
            nodes[1].depends_on = []

        catalog = _catalog_with_node(mutate)
        issues = validate_workflow(catalog, "wf-broken")
        assert any("workspace" in i for i in issues)

    def test_secret_preflight_detected(self):
        def mutate(nodes):
            # constructed dynamically so this source file never carries a
            # secret-shaped literal (the repo scans all orchestrator sources)
            token = "sk-" + "a1b2c3d4e5f60718"
            nodes[1].intent = nodes[1].intent + " api_key=" + token

        catalog = _catalog_with_node(mutate)
        issues = validate_workflow(catalog, "wf-broken")
        assert any("secret" in i for i in issues)


# ------------------------------------------------ admission via orchestrator --


class TestAdmissionThroughControlPlane:
    def test_agency_advisory_task_admits(self, orch):
        task = orch.propose(
            title="Threat-model advisory",
            intent="Produce a threat model for the supplied target; advisory only.",
            assignee_profile="agency-security-architect",
            required_capability="analysis",
            acceptance_criteria=["Trust boundaries enumerated"],
            verification=_verification(),
            idempotency_key="test:agency-advisory:1",
        )
        assert task.assignee_profile == "agency-security-architect"

    def test_agency_assignee_unknown_slug_rejected(self, orch):
        with pytest.raises(PolicyViolation) as exc:
            orch.propose(
                title="Bad agency task",
                intent="x" * 20,
                assignee_profile="agency-not-a-profile",
                required_capability="analysis",
                acceptance_criteria=["criterion"],
                verification=_verification(),
                idempotency_key="test:agency-unknown:1",
            )
        assert exc.value.code == "unknown_profile"

    def test_agency_capability_mismatch_rejected(self, orch):
        with pytest.raises(PolicyViolation) as exc:
            orch.propose(
                title="Wrong capability",
                intent="x" * 20,
                assignee_profile="agency-security-architect",
                required_capability="code_modify",
                acceptance_criteria=["criterion"],
                verification=_verification(),
                idempotency_key="test:agency-cap:1",
            )
        assert exc.value.code == "capability_mismatch"

    def test_agency_cannot_be_assigned_r2(self, orch):
        with pytest.raises(PolicyViolation) as exc:
            orch.propose(
                title="Consequential agency task",
                intent="x" * 20,
                assignee_profile="agency-security-architect",
                required_capability="analysis",
                acceptance_criteria=["criterion"],
                verification=_verification(),
                idempotency_key="test:agency-r2:1",
                risk_tier="r2",
                approval_required=True,
            )
        assert exc.value.code == "advisory_cannot_execute_consequential"

    def test_agency_cannot_be_assigned_irreversible(self, orch):
        with pytest.raises(PolicyViolation) as exc:
            orch.propose(
                title="Irreversible agency task",
                intent="x" * 20,
                assignee_profile="agency-security-architect",
                required_capability="analysis",
                acceptance_criteria=["criterion"],
                verification=_verification(),
                idempotency_key="test:agency-irr:1",
                risk_tier="r3",
                approval_required=True,
                irreversible_operations=["production_mutation"],
            )
        assert exc.value.code == "advisory_cannot_execute_consequential"

    def test_agency_cannot_be_reviewer_profile(self, orch):
        with pytest.raises(PolicyViolation) as exc:
            orch.propose(
                title="Agency as reviewer",
                intent="x" * 20,
                assignee_profile="noesis-cartographer",
                required_capability="plan_artifact",
                acceptance_criteria=["criterion"],
                verification=_verification(),
                idempotency_key="test:agency-rev:1",
                reviewer_profile="agency-design-ui-finish-gate-reviewer",
            )
        assert exc.value.code == "reviewer_must_be_reviewer_only"

    def test_supervisor_still_cannot_execute(self, orch):
        with pytest.raises(PolicyViolation) as exc:
            orch.propose(
                title="Supervisor executes",
                intent="x" * 20,
                assignee_profile="noesis-orchestrator",
                required_capability="analysis",
                acceptance_criteria=["criterion"],
                verification=_verification(),
                idempotency_key="test:supervisor:1",
            )
        assert exc.value.code == "supervisor_cannot_execute"


# ------------------------------------------------------- full workflow run --


class TestCatalogEmission:
    def test_admit_workflow_emits_native_contracts(self, orch):
        tasks = admit_workflow(orch, "wf-arch-security-decision")
        assert set(tasks) == {"plan", "sysdes", "threat", "adr", "handoff"}
        correlation_ids = {str(t.correlation_id) for t in tasks.values()}
        assert len(correlation_ids) == 1
        # dependency wiring uses real ledger task ids
        adr = tasks["adr"]
        assert {str(d) for d in adr.depends_on} == {
            str(tasks["sysdes"].task_id),
            str(tasks["threat"].task_id),
        }

    def test_admission_is_idempotent(self, orch):
        first = admit_workflow(orch, "wf-arch-security-decision")
        second = admit_workflow(orch, "wf-arch-security-decision")
        assert {k: str(v.task_id) for k, v in first.items()} == {
            k: str(v.task_id) for k, v in second.items()
        }

    def test_dependency_blocks_dispatch_until_succeeded(self, orch):
        tasks = admit_workflow(orch, "wf-arch-security-decision")
        dependent = tasks["sysdes"]
        orch.enqueue(dependent.task_id)
        decision = orch.dispatchable(dependent.task_id)
        assert not decision.allowed
        assert "unresolved dependencies" in decision.reason
        # root nodes have no dependencies and dispatch cleanly
        root = tasks["plan"]
        orch.enqueue(root.task_id)
        assert orch.dispatchable(root.task_id).allowed
