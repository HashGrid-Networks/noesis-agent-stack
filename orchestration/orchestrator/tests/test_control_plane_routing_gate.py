"""Integration tests: inference-routing gate wired into Orchestrator.dispatchable."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.control_plane import Orchestrator
from app.inference_routing import EnforcerConfig, InferenceRoutingEnforcer, REPO_ROOT
from app.launcher import LaunchAttestation, LaunchResult, RuntimeAdapter
from app.models import TaskContract, Verification
from app.specialist_routing import CapabilityIndex, RoutingBlocked


def _enforcer(tmp_path: Path, mode: str) -> InferenceRoutingEnforcer:
    cfg = EnforcerConfig(
        mode=mode,
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
    return InferenceRoutingEnforcer(cfg)


def _propose(orch: Orchestrator, **overrides) -> TaskContract:
    base = dict(
        title="integration probe",
        intent="probe routing gate",
        assignee_profile="noesis-signal",
        required_capability="deep_research",
        acceptance_criteria=["probe complete"],
        verification=Verification(method="probe"),
        idempotency_key="probe-1",
        risk_tier="r0",
        labels=["research"],
    )
    base.update(overrides)
    return orch.propose(**base)


def _override_signal(orch: Orchestrator) -> None:
    """Model a reconciled aligned profile so the gate isolates routing policy."""
    orch.inference_enforcer.set_profile_overrides(
        "noesis-signal",
        {
            "observed_runtime": {
                "source": "policy_fixture",
                "provider_lane": "KIMI_CODE",
                "model_alias_or_id": "kimi-k2.7-code",
                "evidence": "integration-test fixture",
            },
            "repo_declared": {
                "provider_lane": "KIMI_CODE",
                "model_alias_or_id": "kimi-k2.7-code",
                "evidence": "integration-test fixture",
            },
            "resolution": {
                "state": "aligned",
                "rationale": "integration fixture",
                "required_gates": [],
            },
        },
    )


class TestRoutingGateInDispatchable:
    def test_enforce_mode_blocks_dispatch_for_blocked_profile(self, tmp_path):
        orch = Orchestrator(tmp_path / "ledger.jsonl", inference_enforcer=_enforcer(tmp_path, "enforce"))
        task = _propose(orch)
        orch.enqueue(task.task_id)
        decision = orch.dispatchable(task.task_id)
        assert decision.allowed is False
        assert "inference routing denied" in decision.reason
        orch.inference_enforcer.clear_profile_overrides()

    def test_enforce_mode_allows_reconciled_baseline_route(self, tmp_path):
        orch = Orchestrator(tmp_path / "ledger.jsonl", inference_enforcer=_enforcer(tmp_path, "enforce"))
        _override_signal(orch)
        task = _propose(orch)
        orch.enqueue(task.task_id)
        decision = orch.dispatchable(task.task_id)
        assert decision.allowed is True
        orch.inference_enforcer.clear_profile_overrides()

    def test_observe_mode_preserves_dispatch_but_records_decision(self, tmp_path):
        enforcer = _enforcer(tmp_path, "observe")
        orch = Orchestrator(tmp_path / "ledger.jsonl", inference_enforcer=enforcer)
        # Blocked profile would be denied under enforce; observe must not block.
        task = _propose(orch)
        orch.enqueue(task.task_id)
        decision = orch.dispatchable(task.task_id)
        assert decision.allowed is True
        log = tmp_path / "routing-decisions.jsonl"
        assert log.exists()
        assert "observe_deny" in log.read_text()
        orch.inference_enforcer.clear_profile_overrides()

    def test_no_enforcer_preserves_existing_dispatch_semantics(self, tmp_path):
        orch = Orchestrator(tmp_path / "ledger.jsonl")
        task = _propose(orch)
        orch.enqueue(task.task_id)
        decision = orch.dispatchable(task.task_id)
        assert decision.allowed is True

    def test_enforce_denies_secret_data_before_dispatch(self, tmp_path):
        orch = Orchestrator(tmp_path / "ledger.jsonl", inference_enforcer=_enforcer(tmp_path, "enforce"))
        _override_signal(orch)
        task = _propose(orch)
        orch.enqueue(task.task_id)
        orch._inference_routing_context[str(task.task_id)] = {
            "data_classification": "secret",
        }
        decision = orch.dispatchable(task.task_id)
        assert decision.allowed is False
        assert "data_classification_denied" in decision.reason
        orch.inference_enforcer.clear_profile_overrides()


# ---------------------------------------------------------------------------
# Gate-probe regression tests
#
# These tests capture the four reproduced defects recorded in
# ``workspace/orchestrator/jev-decision-layer/ORCHESTRATOR-GATE-PROBES.md``.
# They are expected to FAIL against the current control-plane source
# (HEAD ``7c3f6cf2fb971ff594702cedb856328b24f37f2b``) and pass only after
# the bounded repair lands. Active probabilistic recommendation must
# remain disabled until they pass.
#
# The ``xfail(strict=False)`` markers ensure the failures do not block
# the rest of the suite but still surface the defect state. Each test's
# ``reason`` points at the originating gate-probe finding so reviewers
# can map failure to the documented defect.
# ---------------------------------------------------------------------------


class _BoundedAdapter:
    """Test-only runtime adapter that forces the bounded, approved-pair path."""

    requires_approved_pair = True

    def __init__(self) -> None:
        self.launched: list[dict[str, object]] = []
        self._installed = {
            "noesis-signal",
            "noesis-forge",
            "noesis-sentinel",
            "noesis-skeptic",
            "noesis-cartographer",
            "noesis-steward",
        }

    def profile_installed(self, profile_id: str) -> bool:
        return profile_id in self._installed

    def launch(self, **kwargs: object) -> LaunchResult:
        self.launched.append(dict(kwargs))
        return LaunchResult(
            ok=True,
            requested_profile=str(kwargs["profile_id"]),
            loaded_profile=str(kwargs["profile_id"]),
            command=["hermes", "-p", str(kwargs["profile_id"]), "noop"],
            attestation=LaunchAttestation(
                requested_profile=str(kwargs["profile_id"]),
                loaded_profile=str(kwargs["profile_id"]),
                runtime="hermes-test",
                profile_path=f"/tmp/{kwargs['profile_id']}",
                manifest_sha256="0" * 64,
            ),
            provider=str(kwargs.get("provider")),
            model=str(kwargs.get("model")),
        )


def _bounded_orch(tmp_path: Path, *, enforcer) -> Orchestrator:
    adapter = _BoundedAdapter()
    index = CapabilityIndex.from_repo(adapter=adapter)
    return Orchestrator(
        tmp_path / "tasks.jsonl",
        capability_index=index,
        runtime_adapter=adapter,
        inference_enforcer=enforcer,
    )


@pytest.mark.xfail(
    strict=False,
    reason=(
        "Gate defect G1 (missing enforcer fails open). "
        "See workspace/orchestrator/jev-decision-layer/ORCHESTRATOR-GATE-PROBES.md "
        "for the reproduced defect. Repair: require an enforcing-mode "
        "enforcer on the bounded non-dry-run launch path."
    ),
)
def test_gate_regression_missing_enforcer_fails_closed(tmp_path: Path) -> None:
    """A bounded, non-dry-run launch must block when the enforcer is None.

    Today, ``control_plane._assert_spawn_gates`` skips the enforcer
    branch when ``self.inference_enforcer is None`` (``control_plane.py``
    line 479 ``if bounded and self.inference_enforcer is not None``). The
    bounded adapter therefore launches even when policy cannot be
    evaluated. The repair must raise ``RoutingBlocked`` with a typed
    reason such as ``enforcer_unavailable``.
    """
    orch = _bounded_orch(tmp_path, enforcer=None)
    with pytest.raises(RoutingBlocked) as exc:
        orch.route_and_launch(
            title="missing enforcer probe",
            intent="Research sources and produce a cited brief",
            acceptance_criteria=["probe complete"],
            verification=Verification(method="probe"),
            idempotency_key="gate-regression:missing-enforcer",
            approved_provider="KIMI_CODE",
            approved_model="kimi-k2.7-code",
            dry_run=False,
        )
    assert exc.value.code in {"enforcer_unavailable", "inference_denied"}


@pytest.mark.xfail(
    strict=False,
    reason=(
        "Gate defect G2 (observe-deny is consumed as authorization). "
        "See workspace/orchestrator/jev-decision-layer/ORCHESTRATOR-GATE-PROBES.md. "
        "Repair: distinguish observe-mode decisions from enforcing-mode "
        "authorization on the bounded launch path."
    ),
)
def test_gate_regression_observe_deny_does_not_authorize(tmp_path: Path) -> None:
    """An observe-mode deny decision must NOT authorize a non-dry-run launch.

    Today, ``_assert_spawn_gates`` checks only ``decision.allowed``; the
    enforcer returns ``allowed=True`` for observe_deny decisions to keep
    dispatch flowing, so the launch passes. The repair must require an
    enforcing-mode ``allow`` decision for a bounded non-dry-run launch.
    """
    enforcer = _enforcer(tmp_path, "observe")
    orch = _bounded_orch(tmp_path, enforcer=enforcer)
    with pytest.raises(RoutingBlocked) as exc:
        orch.route_and_launch(
            title="observe-deny probe",
            intent="Research sources and produce a cited brief",
            acceptance_criteria=["probe complete"],
            verification=Verification(method="probe"),
            idempotency_key="gate-regression:observe-deny",
            approved_provider="KIMI_CODE",
            approved_model="kimi-k2.7-code",
            dry_run=False,
        )
    assert exc.value.code in {
        "observe_mode_not_authorizing",
        "inference_not_enforcing",
        "inference_denied",
    }


@pytest.mark.xfail(
    strict=False,
    reason=(
        "Gate defect G3 (policy-resolved lane/model not bound to launcher pair). "
        "See workspace/orchestrator/jev-decision-layer/ORCHESTRATOR-GATE-PROBES.md. "
        "Repair: bind the exact policy-resolved lane/model to the launcher "
        "pair by reading ``decision.resolved_lane`` and ``decision.resolved_model``."
    ),
)
def test_gate_regression_resolved_pair_must_match(tmp_path: Path) -> None:
    """Policy-resolved lane/model must equal the approved launcher pair.

    Today, ``_assert_spawn_gates`` reads nonexistent ``decision.provider``
    and ``decision.model`` fields; the binding never fires. The repair
    must read ``decision.resolved_lane`` and ``decision.resolved_model``
    and raise ``RoutingBlocked('unapproved_pair', ...)`` on mismatch.
    """
    enforcer = _enforcer(tmp_path, "enforce")
    orch = _bounded_orch(tmp_path, enforcer=enforcer)
    # Configure the enforcer to resolve a different lane/model than the
    # approved launcher pair. The override's resolution sets
    # state="blocked", which the enforcer returns as a deny decision;
    # the repair must additionally raise on lane/model mismatch with an
    # allow decision. We therefore set the override to aligned but a
    # lane different from the approved pair.
    orch.inference_enforcer.set_profile_overrides(
        "noesis-signal",
        {
            "observed_runtime": {
                "provider_lane": "POLICY_LANE",
                "model_alias_or_id": "policy-model",
            },
            "repo_declared": {
                "provider_lane": "POLICY_LANE",
                "model_alias_or_id": "policy-model",
            },
            "resolution": {
                "state": "aligned",
                "rationale": "test fixture",
                "required_gates": [],
            },
        },
    )
    try:
        with pytest.raises(RoutingBlocked) as exc:
            orch.route_and_launch(
                title="mismatch probe",
                intent="Research sources and produce a cited brief",
                acceptance_criteria=["probe complete"],
                verification=Verification(method="probe"),
                idempotency_key="gate-regression:resolved-mismatch",
                approved_provider="KIMI_CODE",
                approved_model="kimi-k2.7-code",
                dry_run=False,
            )
        assert exc.value.code == "unapproved_pair"
    finally:
        orch.inference_enforcer.clear_profile_overrides()


@pytest.mark.xfail(
    strict=False,
    reason=(
        "Gate defect G4 (current assignment epoch optional at live caller boundary). "
        "See workspace/orchestrator/jev-decision-layer/ORCHESTRATOR-GATE-PROBES.md. "
        "Repair: require the current assignment epoch at the live CLI "
        "and plugin launch path; preserve typed rejection for stale values."
    ),
)
def test_gate_regression_normal_call_requires_current_epoch(tmp_path: Path) -> None:
    """A non-dry-run call without ``expected_epoch`` must be rejected.

    Today, ``_assert_spawn_gates`` only checks the epoch when the caller
    supplies it (``control_plane.py`` line 455). The CLI does not forward
    ``expected_epoch`` (see ``specialist_dispatch_cli.py`` line 147-167),
    so the gate silently accepts any current epoch. The repair must
    raise ``RoutingBlocked('epoch_required', ...)`` when ``expected_epoch``
    is omitted on a non-dry-run launch.
    """
    enforcer = _enforcer(tmp_path, "enforce")
    orch = _bounded_orch(tmp_path, enforcer=enforcer)
    with pytest.raises(RoutingBlocked) as exc:
        orch.route_and_launch(
            title="epoch-required probe",
            intent="Research sources and produce a cited brief",
            acceptance_criteria=["probe complete"],
            verification=Verification(method="probe"),
            idempotency_key="gate-regression:epoch-required",
            approved_provider="KIMI_CODE",
            approved_model="kimi-k2.7-code",
            dry_run=False,
            # Intentionally omit expected_epoch
        )
    assert exc.value.code == "epoch_required"
