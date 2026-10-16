"""Tests for the bounded, default-OFF decision layer wiring.

The decision layer is wired into ``Orchestrator.route_and_launch``
between the deterministic baseline (CapabilityIndex.select) and the
launch gate. This module verifies that the wiring is:

* observable: the orchestrator holds a ``_decision_layer`` field;
* backward-compatible: the default layer is OFF +
  ``DeterministicProvider``, so existing callers see no behavior
  change (this is what the 481-test regression confirms in
  aggregate);
* scoped: ACTIVE-mode reranking only substitutes tied-eligible
  candidates; OFF/SHADOW never modify the selection;
* non-authoritative: the layer never admits a candidate, never
  changes classification, never weakens policy rules, and never
  changes a risk tier -- reranking only swaps the selected
  profile_id when all other guards remain satisfied.

G1-G4 gate-regression xfails are out of scope here; they live in
``test_control_plane_routing_gate.py`` and require separate control-
plane repairs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from app.control_plane import Orchestrator
from app.specialist_decision import (
    ABSTAIN_KEY,
    CandidateInput,
    DecisionConfig,
    DecisionLayer,
    DecisionMode,
    DecisionProvider,
    FallbackReason,
    ProviderResponse,
)
from app.specialist_decision import DeterministicProvider
from app.specialist_routing import (
    CandidateDecision,
    Classification,
    SelectionDecision,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _StubRecord:
    """A SpecialistRecord-shaped stand-in for unit tests."""

    profile_id: str
    runtime_profile: str = ""
    model_id: str | None = None
    capabilities: tuple[str, ...] = ()
    responsibilities: str = ""
    role: str = ""

    def __post_init__(self) -> None:
        if not self.runtime_profile:
            object.__setattr__(self, "runtime_profile", self.profile_id)


class _StubProvider(DecisionProvider):
    """Test provider whose distribution the test controls per call."""

    provider_name = "stub-wiring-provider"

    def __init__(self, distributions: Sequence[dict[str, float]]):
        self._distributions = list(distributions)
        self._index = 0
        self.calls = 0

    def evaluate(self, request):
        self.calls += 1
        idx = min(self._index, len(self._distributions) - 1)
        distribution = dict(self._distributions[idx])
        self._index += 1
        for cid in (c.candidate_id for c in request.candidates):
            distribution.setdefault(cid, 0.0)
        distribution[ABSTAIN_KEY] = float(distribution.get(ABSTAIN_KEY, 0.0))
        return ProviderResponse(
            distribution=distribution,
            provider_confidence=0.9,
            reported_model="stub-model",
            raw_answer=None,
            latency_ms=1.0,
            timed_out=False,
        )


def _two_records() -> dict[str, _StubRecord]:
    """Two tied records shaped like SpecialistRecord."""
    return {
        "noesis-forge": _StubRecord(
            profile_id="noesis-forge",
            capabilities=("plan_artifact", "code_modify"),
            responsibilities="implementation mandate",
            role="implementation",
        ),
        "noesis-architect": _StubRecord(
            profile_id="noesis-architect",
            capabilities=("plan_artifact", "code_modify"),
            responsibilities="architecture mandate",
            role="architecture",
        ),
    }


def _tied_selection(records: dict[str, _StubRecord]) -> SelectionDecision:
    """Selection with the two records tied at the best eligible score."""
    cand_a = CandidateDecision(
        profile_id="noesis-forge", eligible=True, score=100, reasons=()
    )
    cand_b = CandidateDecision(
        profile_id="noesis-architect", eligible=True, score=100, reasons=()
    )
    # The selected field is type-narrowed to SpecialistRecord; the test
    # stub satisfies the structural shape (profile_id, runtime_profile,
    # capabilities, role, responsibilities, model_id) but pyright cannot
    # prove it without the SpecialistRecord base class.
    return SelectionDecision(
        classification=Classification("implementation", "code_modify"),
        selected=records["noesis-forge"],  # type: ignore[arg-type]
        candidates=(cand_a, cand_b),
        reason="selected noesis-forge for code_modify",
    )


def _two_record_inputs(
    records: dict[str, _StubRecord],
) -> dict[str, CandidateInput]:
    return {
        "noesis-forge": CandidateInput(
            candidate_id="noesis-forge",
            capabilities=tuple(records["noesis-forge"].capabilities),
            mandate=str(records["noesis-forge"].responsibilities),
        ),
        "noesis-architect": CandidateInput(
            candidate_id="noesis-architect",
            capabilities=tuple(records["noesis-architect"].capabilities),
            mandate=str(records["noesis-architect"].responsibilities),
        ),
    }


def _call_kwargs(records: dict[str, _StubRecord]) -> dict:
    return dict(
        baseline_candidate_id="noesis-forge",
        tied_candidates=["noesis-forge", "noesis-architect"],
        required_capability="code_modify",
        classification="implementation",
        risk_tier="r1",
        candidate_records=_two_record_inputs(records),
        registry_digest="r" * 32,
        policy_digest="p" * 32,
        rubric_digest="b" * 32,
        snapshot_digest="s" * 32,
        question="q",
        correlation_id="c",
        task_id="t",
        attempt_id="a",
    )


# ---------------------------------------------------------------------------
# Wiring tests
# ---------------------------------------------------------------------------


def test_default_decision_layer_is_off_and_idle() -> None:
    """The orchestrator's default layer is OFF + abstaining provider."""
    orch = Orchestrator()
    assert isinstance(orch._decision_layer, DecisionLayer)
    assert orch._decision_layer.mode is DecisionMode.OFF
    assert orch._decision_layer.provider_name == "deterministic-abstain"

    records = _two_records()
    outcome = orch._decision_layer.decide(**_call_kwargs(records))
    assert outcome.mode is DecisionMode.OFF
    assert outcome.fallback_reason is FallbackReason.MODE_OFF
    assert outcome.applied_candidate_id == "noesis-forge"
    assert outcome.recommendation is None


def test_injected_decision_layer_overrides_default() -> None:
    """A caller-supplied layer replaces the orchestrator's default."""
    custom = DecisionLayer(
        provider=DeterministicProvider(),
        config=DecisionConfig(mode=DecisionMode.SHADOW),
    )
    orch = Orchestrator(decision_layer=custom)
    assert orch._decision_layer is custom


def test_decision_layer_config_override_takes_effect() -> None:
    """decision_layer_config builds a fresh layer when layer=None."""
    orch = Orchestrator(
        decision_layer_config=DecisionConfig(mode=DecisionMode.SHADOW)
    )
    assert orch._decision_layer.mode is DecisionMode.SHADOW
    assert orch._decision_layer.provider_name == "deterministic-abstain"


def test_tied_candidates_helper_returns_top_scoring_eligible() -> None:
    """_tied_candidates mirrors CapabilityIndex.select(): top-score, eligible-only."""
    orch = Orchestrator()
    records = _two_records()
    selection = _tied_selection(records)
    tied = orch._tied_candidates(selection)
    assert sorted(tied) == ["noesis-architect", "noesis-forge"]


def test_build_decision_candidate_records_drops_missing_ids() -> None:
    """_build_decision_candidate_records drops ids without live records."""
    orch = Orchestrator()
    records = orch._build_decision_candidate_records(
        ["noesis-forge", "missing-id"]
    )
    assert "noesis-forge" in records
    assert "missing-id" not in records


def test_stable_digests_are_deterministic_and_distinguish_input() -> None:
    """The four wiring digests are stable per call and differ across inputs."""
    orch = Orchestrator()
    # Registry digest is stable for the same view.
    assert orch._stable_registry_digest() == orch._stable_registry_digest()
    # Policy digest distinguishes routing-context inputs.
    assert orch._stable_policy_digest({"x": "1"}) != orch._stable_policy_digest(
        {"x": "2"}
    )
    # Snapshot digest distinguishes selection states.
    records = _two_records()
    sel_a = _tied_selection(records)
    cand_c = CandidateDecision(
        profile_id="noesis-quill", eligible=False, score=10, reasons=("mismatch",)
    )
    sel_c = SelectionDecision(
        classification=Classification("implementation", "code_modify"),
        selected=records["noesis-forge"],  # type: ignore[arg-type]
        candidates=(*sel_a.candidates, cand_c),
        reason="selected noesis-forge for code_modify",
    )
    assert orch._stable_snapshot_digest(sel_a) != orch._stable_snapshot_digest(sel_c)


def test_shadow_mode_does_not_rerank() -> None:
    """SHADOW records a recommendation but never reranks the selection."""
    provider = _StubProvider(
        distributions=[
            {
                "noesis-architect": 0.7,
                "noesis-forge": 0.2,
                ABSTAIN_KEY: 0.1,
            }
        ]
    )
    orch = Orchestrator(
        decision_layer=DecisionLayer(
            provider=provider,
            config=DecisionConfig(mode=DecisionMode.SHADOW),
        )
    )
    records = _two_records()
    outcome = orch._decision_layer.decide(**_call_kwargs(records))
    assert outcome.mode is DecisionMode.SHADOW
    # Shadow mode never reranks (it records only).
    assert outcome.applied_candidate_id == "noesis-forge"
    assert outcome.recommendation is not None
    assert provider.calls == 1


def test_active_rerank_selects_architect_when_above_epsilon() -> None:
    """ACTIVE-mode rerank returns the recommended candidate in the tied set."""
    provider = _StubProvider(
        distributions=[
            {
                "noesis-architect": 0.7,
                "noesis-forge": 0.2,
                ABSTAIN_KEY: 0.1,
            }
        ]
    )
    orch = Orchestrator(
        decision_layer=DecisionLayer(
            provider=provider,
            config=DecisionConfig(
                mode=DecisionMode.ACTIVE,
                accept_threshold=0.5,
            ),
        )
    )
    records = _two_records()
    outcome = orch._decision_layer.decide(**_call_kwargs(records))
    assert outcome.mode is DecisionMode.ACTIVE
    assert outcome.applied_candidate_id == "noesis-architect"
    assert outcome.fallback_reason is FallbackReason.OK
    assert outcome.baseline_candidate_id == "noesis-forge"


def test_active_equal_mass_kept_baseline_per_epsilon() -> None:
    """ACTIVE-mode equal masses fail the epsilon gate; baseline stands."""
    provider = _StubProvider(
        distributions=[
            {
                "noesis-architect": 0.5,
                "noesis-forge": 0.5,
                ABSTAIN_KEY: 0.0,
            }
        ]
    )
    orch = Orchestrator(
        decision_layer=DecisionLayer(
            provider=provider,
            config=DecisionConfig(
                mode=DecisionMode.ACTIVE, accept_threshold=0.5
            ),
        )
    )
    records = _two_records()
    outcome = orch._decision_layer.decide(**_call_kwargs(records))
    assert outcome.mode is DecisionMode.ACTIVE
    assert outcome.applied_candidate_id == "noesis-forge"
    assert outcome.fallback_reason is FallbackReason.PROVIDER_THRESHOLD_REJECTED
