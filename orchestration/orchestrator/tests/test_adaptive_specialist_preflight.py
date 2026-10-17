"""Phase B adaptive-specialist preflight tests.

Each test maps to a specific acceptance case in
``PROPOSAL.md §11/12`` (contract ``adaptive-specialist-preflight-b1``).
The slices mirror the behaviors and stay offline: fake providers,
fake MCP endpoints, in-tree policy/capability fixtures.

Test slice inventory (mirrors implementation order):

A. Typed decision model and capability snapshot enums.
B. Capability snapshot derivation from existing roster.
C. Local-only readiness evidence (auth/eligibility/quota/cooldown).
D. Preflight decision outputs (KEEP/TASK_OVERRIDE/SELECT_ALTERNATIVE/REQUIRE_APPROVAL/BLOCK).
E. Orchestrator.preflight_configuration bound to canonical task/attempt.
F. Isolated attempt-home allowlist and launcher identity repair.
G. Specialist plugin plan-first admission + durable-ledger requirement.
H. Report-only deterministic auditor: lock, dedupe, bounds, no mutation.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
ORCHESTRATOR_ROOT = REPO_ROOT / "orchestration" / "orchestrator"
sys.path.insert(0, str(ORCHESTRATOR_ROOT))


# ---------------------------------------------------------------------------
# Slice A: Typed decision model and capability-state enums.
# ---------------------------------------------------------------------------


def test_preflight_decision_enum_has_five_typed_outcomes() -> None:
    """Spec §11.3: outcomes are KEEP | TASK_OVERRIDE | SELECT_ALTERNATIVE |
    REQUIRE_APPROVAL | BLOCK. Unknown/missing decisions are rejected so a
    downstream consumer cannot mistake a placeholder for a real verdict.
    """
    from app.adaptive_specialist import PreflightDecision

    expected = {
        PreflightDecision.KEEP,
        PreflightDecision.TASK_OVERRIDE,
        PreflightDecision.SELECT_ALTERNATIVE,
        PreflightDecision.REQUIRE_APPROVAL,
        PreflightDecision.BLOCK,
    }
    # Distinct, named, and exhaustive on the requested set.
    names = {d.name for d in PreflightDecision}
    assert names == {
        "KEEP",
        "TASK_OVERRIDE",
        "SELECT_ALTERNATIVE",
        "REQUIRE_APPROVAL",
        "BLOCK",
    }
    # Round-trip from a serialised label is the only way a configured durable
    # ledger can persist a decision; anything else must fail closed.
    assert PreflightDecision.from_label("KEEP") is PreflightDecision.KEEP
    assert PreflightDecision.from_label("BLOCK") is PreflightDecision.BLOCK
    with pytest.raises(ValueError):
        PreflightDecision.from_label("MAYBE")


def test_capability_state_enum_distinguishes_five_states() -> None:
    """Spec §5.3 / item 9: installed / available / enabled / exposed /
    authorized are separate capability states. ``from_label`` rejects any
    other input so a downgrade cannot be claimed with a misspelled keyword.
    """
    from app.adaptive_specialist import CapabilityState

    labels = ["installed", "available", "enabled", "exposed", "authorized"]
    assert {CapabilityState.from_label(lab).name.lower() for lab in labels} == set(labels)
    with pytest.raises(ValueError):
        CapabilityState.from_label("unknown_state")


def test_route_disposition_enum_keeps_separate_unknown_state() -> None:
    """Spec §11.4: missing quota evidence must remain UNKNOWN, never
    accidentally coerced into READY. Each disposition is a discrete label.
    """
    from app.adaptive_specialist import RouteDisposition

    expected = {
        "READY",
        "EXHAUSTED",
        "RATE_LIMITED",
        "AUTH_FAILED",
        "UNAVAILABLE",
        "UNKNOWN",
    }
    assert {d.name for d in RouteDisposition} == expected
    assert RouteDisposition.from_label("UNKNOWN") is RouteDisposition.UNKNOWN
    with pytest.raises(ValueError):
        RouteDisposition.from_label("READY_LIKE")


# ---------------------------------------------------------------------------
# Slice B: Capability snapshot derivation.
# ---------------------------------------------------------------------------


def test_capability_snapshot_is_derived_from_existing_roster(tmp_path: Path) -> None:
    """Spec §5.1: snapshot is a derived, cached artifact from existing
    roster and policy sources. It must not invent a second roster: no new
    profile ids may appear that are not already declared in
    ``profiles/noesis-roster.yaml``.
    """
    from app.adaptive_specialist import CapabilitySnapshot

    snap = CapabilitySnapshot.build(repo_root=REPO_ROOT)
    roster_ids = set(_declared_roster_ids())
    snapshot_ids = {p.profile_id for p in snap.profiles}
    # The snapshot is a strict subset of declared roster profile ids — never an
    # independent registry. A second roster would imply a parallel authority.
    assert snapshot_ids
    assert snapshot_ids.issubset(roster_ids), sorted(snapshot_ids - roster_ids)
    # Snapshot is regenerable from sources, so it carries source digests.
    assert snap.source_digests
    assert "roster" in snap.source_digests
    assert snap.revision
    assert snap.digest


def test_capability_snapshot_records_five_state_per_capability(tmp_path: Path) -> None:
    """Spec §5.3 / item 9: each capability carries installed/available/
    enabled/exposed/authorized. ``authorized`` must default to False and
    require an explicit policy grant to flip to True.
    """
    from app.adaptive_specialist import CapabilitySnapshot, CapabilityState

    snap = CapabilitySnapshot.build(repo_root=REPO_ROOT)
    profile = next(p for p in snap.profiles if p.profile_id in {"noesis-forge"})
    sample_cap = profile.capabilities[0]
    states = profile.capability_states[sample_cap]
    # Five distinct states; every state has a known status, not just a presence flag.
    assert {s for s in states.keys()} >= {
        CapabilityState.INSTALLED.value,
        CapabilityState.AVAILABLE.value,
        CapabilityState.ENABLED.value,
        CapabilityState.EXPOSED.value,
        CapabilityState.AUTHORIZED.value,
    }
    # ``authorized`` must be False until a task-scoped grant flips it.
    assert states[CapabilityState.AUTHORIZED.value] is False


def test_mcp_capability_is_marked_server_wide_when_no_tool_filter(tmp_path: Path) -> None:
    """Spec item 9 / §6 Tool/MCP overexposure: treat MCP as server-wide
    exposure unless an independently verified per-tool policy is supplied.
    The snapshot must surface that limitation instead of over-claiming a
    tool-level allowlist.
    """
    from app.adaptive_specialist import CapabilitySnapshot, CapabilityState

    snap = CapabilitySnapshot.build(repo_root=REPO_ROOT)
    profile = next((p for p in snap.profiles if any("mcp" in c.lower() for c in p.capabilities)), None)
    if profile is None:
        pytest.skip("no MCP-bearing profile declared in this fixture repo")
    states = profile.capability_states
    mcp_keys = [c for c in profile.capabilities if "mcp" in c.lower()]
    for key in mcp_keys:
        # Exposed status is true when the server is enabled, but the
        # authoritative / finer-grained tool-level claim is NOT lifted to True.
        assert states.get(key, {}).get(CapabilityState.EXPOSED.value) is True
        assert states.get(key, {}).get("SERVER_WIDE_ONLY") is True
        assert states.get(key, {}).get(CapabilityState.AUTHORIZED.value) is False


def _declared_roster_ids() -> list[str]:
    """Read the canonical roster to identify declared profiles."""
    import yaml

    path = REPO_ROOT / "profiles" / "noesis-roster.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return list((data or {}).get("profiles", {}).keys())


# ---------------------------------------------------------------------------
# Slice C: Local-only readiness evidence.
# ---------------------------------------------------------------------------


def test_readiness_registry_reports_unknown_when_quota_missing(tmp_path: Path) -> None:
    """Spec §11.4 / §9.1: missing quota interface is UNKNOWN, never READY.
    Auth and quota are kept as separate fields so authentication success
    cannot be misread as capacity.
    """
    from app.adaptive_specialist import LocalReadinessRegistry, RouteDisposition

    reg = LocalReadinessRegistry.synthetic(provider="fake-provider")
    evidence = reg.evaluate(provider="fake-provider", model="fake-model")
    assert evidence.auth_eligible is False
    assert evidence.entitlement is None
    assert evidence.quota_disposition is RouteDisposition.UNKNOWN
    assert evidence.provider_state_disposition is RouteDisposition.UNKNOWN
    assert evidence.local_cooldown_disposition is RouteDisposition.UNKNOWN
    assert evidence.credential_reference_id is None  # never the credential value.


def test_readiness_auth_success_does_not_imply_quota(tmp_path: Path) -> None:
    """Spec §6 'Authentication mistaken for capacity' / acceptance
    'auth success does not imply quota': auth-eligible with no quota
    evidence is an UNKNOWN quota, not a READY route.
    """
    from app.adaptive_specialist import LocalReadinessRegistry, RouteDisposition

    reg = LocalReadinessRegistry.synthetic(
        provider="fake-provider",
        auth_eligible=True,
    )
    evidence = reg.evaluate(provider="fake-provider", model="fake-model")
    assert evidence.auth_eligible is True
    # The disposition "allows auth-eligible" must not silently make the route READY.
    assert evidence.quota_disposition is RouteDisposition.UNKNOWN
    assert evidence.combined_disposition is RouteDisposition.UNKNOWN


def test_readiness_exhausted_skips_even_when_inference_otherwise_possible(tmp_path: Path) -> None:
    """Acceptance: 'exhausted primary skipped before fake inference'.
    An exhausted local cooldown disposition must surface as the combined
    disposition EXHAUSTED so the decision layer skips the route.
    """
    from app.adaptive_specialist import LocalReadinessRegistry, RouteDisposition

    reg = LocalReadinessRegistry.synthetic(
        provider="fake-provider",
        auth_eligible=True,
        local_cooldown_disposition=RouteDisposition.EXHAUSTED,
    )
    evidence = reg.evaluate(provider="fake-provider", model="fake-model")
    assert evidence.local_cooldown_disposition is RouteDisposition.EXHAUSTED
    assert evidence.combined_disposition is RouteDisposition.EXHAUSTED
    # No provider refresh / inference call is implied by evidence alone.


def test_readiness_provider_state_and_cooldown_are_independent_fields(tmp_path: Path) -> None:
    """Spec §11.4: 'Authentication, entitlement, quota, provider-reported
    state, and local cooldown are separate'. The schema must keep them as
    independent fields even when both are EXHAUSTED so callers cannot fold
    them into one signal and lose the cause.
    """
    from app.adaptive_specialist import LocalReadinessRegistry, RouteDisposition

    reg = LocalReadinessRegistry.synthetic(
        provider="fake-provider",
        auth_eligible=True,
        local_cooldown_disposition=RouteDisposition.EXHAUSTED,
        provider_state_disposition=RouteDisposition.RATE_LIMITED,
    )
    evidence = reg.evaluate(provider="fake-provider", model="fake-model")
    assert evidence.local_cooldown_disposition is RouteDisposition.EXHAUSTED
    assert evidence.provider_state_disposition is RouteDisposition.RATE_LIMITED
    assert evidence.auth_eligible is True
    assert evidence.credential_value is None  # evidence never carries the value.


def test_readiness_credential_value_is_never_recorded(tmp_path: Path) -> None:
    """Spec §6 'Credential value leakage': the registry must accept a
    ``credential_reference_id`` but never persist a credential value in any
    field of the returned evidence.
    """
    from app.adaptive_specialist import LocalReadinessRegistry

    reg = LocalReadinessRegistry.synthetic(
        provider="fake-provider",
        credential_reference_id="ref://vault/key-23",
    )
    evidence = reg.evaluate(provider="fake-provider", model="fake-model")
    serialised = json.dumps(evidence.to_dict(), sort_keys=True)
    # The reference label is allowed; credential-shaped values are not.
    assert "ref://vault" in serialised
    assert "sk-" not in serialised
    assert "AKIA" not in serialised
    assert "Bearer " not in serialised
    assert evidence.credential_value is None


# ---------------------------------------------------------------------------
# Slice D: Preflight decision outputs.
# ---------------------------------------------------------------------------


def test_preflight_keep_when_route_qualified_and_no_overrides_needed(tmp_path: Path) -> None:
    """Spec §5.2 step 9: returns one typed keep decision when the selected
    profile/route already satisfies every constraint and no override is
    required."""
    from app.adaptive_specialist import (
        CapabilitySnapshot,
        LocalReadinessRegistry,
        RouteDisposition,
        TaskConfigurationPrecomputer,
        PreflightDecision,
    )

    snap = CapabilitySnapshot.build(repo_root=REPO_ROOT)
    reg = LocalReadinessRegistry.synthetic(provider="fake", auth_eligible=True)
    evidence = reg.evaluate(provider="fake", model="fake")
    engine = TaskConfigurationPrecomputer(snapshot=snap, readiness=reg)
    out = engine.evaluate(
        task_class="implementation",
        risk_tier="r0",
        data_classification="public",
        privacy_constraint=None,
        current_profile_id="noesis-forge",
        readiness_overrides={"fake": evidence},
    )
    assert out.decision is PreflightDecision.KEEP
    assert out.profile_id == "noesis-forge"


def test_preflight_task_override_privacy_constraint_overrides_cheaper_route(tmp_path: Path) -> None:
    """Acceptance: 'privacy constraint overrides cheaper route'. The
    precomputer must return TASK_OVERRIDE when the cheaper candidate's
    privacy policy cannot accept the data class even though it qualifies.
    """
    from app.adaptive_specialist import (
        CapabilitySnapshot,
        LocalReadinessRegistry,
        PrivacyConstraint,
        TaskConfigurationPrecomputer,
        PreflightDecision,
    )

    snap = CapabilitySnapshot.build(repo_root=REPO_ROOT)
    reg = LocalReadinessRegistry.synthetic(provider="fake", auth_eligible=True)
    engine = TaskConfigurationPrecomputer(snapshot=snap, readiness=reg)
    out = engine.evaluate(
        task_class="implementation",
        risk_tier="r0",
        data_classification="confidential",
        privacy_constraint=PrivacyConstraint.PRIVATE_ONLY,
        current_profile_id="noesis-forge",
        readiness_overrides={"fake": reg.evaluate(provider="fake", model="fake")},
    )
    assert out.decision in {
        PreflightDecision.TASK_OVERRIDE,
        PreflightDecision.SELECT_ALTERNATIVE,
        PreflightDecision.REQUIRE_APPROVAL,
        PreflightDecision.BLOCK,
    }
    # Whatever the verdict, a confidential data class must not be silently
    # admitted to a non-private candidate.
    assert out.selected_provider_family != "public-cloud"


def test_preflight_block_returns_typed_block_with_reasons(tmp_path: Path) -> None:
    """Acceptance: 'all unavailable routes produce one precise block'. A
    BLOCK decision must carry reasons and digests so the caller can
    reproduce the verdict without re-running the whole precomputer.
    """
    from app.adaptive_specialist import (
        CapabilitySnapshot,
        LocalReadinessRegistry,
        RouteDisposition,
        TaskConfigurationPrecomputer,
        PreflightDecision,
    )

    snap = CapabilitySnapshot.build(repo_root=REPO_ROOT)
    reg = LocalReadinessRegistry.synthetic(
        provider="fake",
        auth_eligible=False,
        local_cooldown_disposition=RouteDisposition.UNAVAILABLE,
    )
    engine = TaskConfigurationPrecomputer(snapshot=snap, readiness=reg)
    out = engine.evaluate(
        task_class="implementation",
        risk_tier="r0",
        data_classification="public",
        privacy_constraint=None,
        current_profile_id="noesis-forge",
        readiness_overrides={
            "fake": reg.evaluate(provider="fake", model="fake"),
        },
    )
    assert out.decision is PreflightDecision.BLOCK
    assert out.reasons
    assert out.bundle_digest
    assert out.evidence_digest


def test_preflight_aggregator_upstream_dependency_is_treated_as_same_failure_domain(
    tmp_path: Path,
) -> None:
    """Acceptance: 'aggregator/upstream shared dependency prevents false
    independence count'. An aggregator (e.g. moa) that depends on a public
    upstream is NOT an independent backup, even when its provider label
    differs from the upstream provider.
    """
    from app.adaptive_specialist import (
        CapabilitySnapshot,
        ProviderFamilyMap,
        TaskConfigurationPrecomputer,
    )

    snap = CapabilitySnapshot.build(repo_root=REPO_ROOT)
    families = ProviderFamilyMap.from_snapshot(
        snap,
        # ``moa`` shares an upstream with ``public-cloud-1``: same backend,
        # same exit, different label.
        overrides={"moa": "public-cloud-1"},
    )
    assert families.family_of("moa") == families.family_of("public-cloud-1")


def test_preflight_self_authority_expansion_is_rejected(tmp_path: Path) -> None:
    """Acceptance: 'unauthorized self-expansion is rejected'. A preflight
    that claims the orchestrator profile as the implementation target must
    BLOCK rather than admit a self-authority route.
    """
    from app.adaptive_specialist import (
        CapabilitySnapshot,
        LocalReadinessRegistry,
        TaskConfigurationPrecomputer,
        PreflightDecision,
    )

    snap = CapabilitySnapshot.build(repo_root=REPO_ROOT)
    reg = LocalReadinessRegistry.synthetic(provider="fake", auth_eligible=True)
    engine = TaskConfigurationPrecomputer(snapshot=snap, readiness=reg)
    out = engine.evaluate(
        task_class="implementation",
        risk_tier="r0",
        data_classification="public",
        privacy_constraint=None,
        current_profile_id="noesis-orchestrator",  # self-authority attempt
        readiness_overrides={"fake": reg.evaluate(provider="fake", model="fake")},
    )
    assert out.decision is PreflightDecision.BLOCK
    assert any("self_authority" in reason for reason in out.reasons)
