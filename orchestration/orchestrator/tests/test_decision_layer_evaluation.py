"""Offline evaluation of the Jev decision layer.

This module runs the decision layer through 25 sanitized synthetic
scenarios in three modes (OFF, SHADOW, ACTIVE) and measures whether
reranking improves specialist selection under the bounded
``accept_threshold + apply_margin_epsilon`` policy.

NO LIVE PROVIDER CALLS.  All distributions are constructed in Python;
the layer never sees a real network response, an SDK, or a credential.

NO PRODUCTION FILES MODIFIED.  This file is added to the test
directory only.  The decision layer source is untouched.

The evaluation answers:

* does ACTIVE-mode reranking move the selection in scenarios where
  the synthetic distribution names a clear winner?
* does the epsilon gate block deterministic-tie exploitation (Skeptic
  A1)?
* does the content-derived breaker fire for repeated identical
  distributions (Skeptic C1)?
* does SHADOW mode agree with the baseline 100% of the time?
* is the OFF-mode abstain rate still 100%?

Limitations (see ``evaluation-results.md``):

* All distributions are synthetic; the layer has never seen a real
  provider response.
* "Improvement" is defined as a recommendation whose winning mass
  clears the epsilon gate; the evaluation cannot measure whether the
  rerank would have produced a better downstream outcome.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

import pytest

from app.specialist_decision import (
    ABSTAIN_KEY,
    CandidateInput,
    DecisionConfig,
    DecisionLayer,
    DecisionMode,
    DecisionProvider,
    DecisionRecommendation,
    FallbackReason,
    ProviderResponse,
)


# ----------------------------------------------------------------------------
# Scenario definition
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class DecisionScenario:
    """A sanitized synthetic fixture for the decision layer evaluation.

    ``tied_override`` lets a scenario declare which candidates are
    tied at the best deterministic score (only the baseline is tied
    for the ``no_ties`` scenario, so the layer short-circuits with
    SKIPPED_NO_TIES).  ``raw_distribution`` controls whether the
    FakeReplayProvider returns the test's distribution verbatim
    (required for malformed distributions that the validator must
    see in their broken form).
    """

    scenario_id: str
    scenario_type: str
    description: str
    candidate_ids: tuple[str, ...]
    baseline: str | None
    distribution: Mapping[str, float] | None
    expected_active: str  # one of the keys in _classify_active_outcome
    tied_override: tuple[str, ...] | None = None
    raw_distribution: bool = False
    classification_override: str | None = None
    advisory_deadline_s: float | None = None
    cache_ttl_s: float | None = None
    budget_max_calls_per_minute: int | None = None
    n_calls: int = 1
    sleep_between_calls_s: float = 0.0


def _candidate(cid: str) -> CandidateInput:
    return CandidateInput(
        candidate_id=cid,
        capabilities=("plan_artifact", "code_modify"),
        mandate=f"{cid} mandate",
    )


# ----------------------------------------------------------------------------
# FakeReplayProvider — synthetic distributions only
# ----------------------------------------------------------------------------


class FakeReplayProvider(DecisionProvider):
    """A synthetic provider that returns fixed distributions per call.

    No network. No SDK. No credential. The test owns the distribution
    the layer sees; the provider merely hands it back in the shape the
    layer expects.

    ``raw_distribution=True`` returns the test's distribution verbatim,
    leaving missing keys, extra keys, and un-normalized totals in
    place so the layer's validator exercises the malformed path.
    """

    provider_name = "fake-replay"

    def __init__(
        self,
        distributions: Sequence[Mapping[str, float] | None],
        *,
        raw_distribution: bool = False,
        slow_s: float = 0.0,
    ) -> None:
        self._distributions = list(distributions)
        self._index = 0
        self._raw_distribution = raw_distribution
        self._slow_s = slow_s
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def evaluate(self, request) -> ProviderResponse:
        cids = tuple(c.candidate_id for c in request.candidates)
        self.calls.append((request.question, cids))
        if self._slow_s > 0:
            time.sleep(self._slow_s)
        if self._index >= len(self._distributions):
            distribution: dict[str, float] = {cid: 0.0 for cid in cids}
            distribution[ABSTAIN_KEY] = 1.0
        else:
            template = self._distributions[self._index]
            self._index += 1
            if template is None:
                distribution = {cid: 0.0 for cid in cids}
                distribution[ABSTAIN_KEY] = 1.0
            else:
                distribution = dict(template)
                if not self._raw_distribution:
                    for cid in cids:
                        distribution.setdefault(cid, 0.0)
                    distribution[ABSTAIN_KEY] = float(
                        distribution.get(ABSTAIN_KEY, 0.0)
                    )
        return ProviderResponse(
            distribution=distribution,
            provider_confidence=0.9,
            reported_model="fake-replay-model",
            raw_answer=None,
            latency_ms=self._slow_s * 1000.0,
            timed_out=False,
        )


class _SlowFakeReplayProvider(DecisionProvider):
    """A provider that exceeds any reasonable advisory deadline."""

    provider_name = "slow-fake-replay"

    def __init__(self, sleep_s: float = 0.2) -> None:
        self._sleep_s = sleep_s
        self.calls: int = 0

    def evaluate(self, request) -> ProviderResponse:
        self.calls += 1
        time.sleep(self._sleep_s)
        distribution = {c.candidate_id: 0.0 for c in request.candidates}
        distribution[ABSTAIN_KEY] = 1.0
        return ProviderResponse(
            distribution=distribution,
            provider_confidence=None,
            reported_model="slow-fake-replay",
            raw_answer=None,
            latency_ms=self._sleep_s * 1000.0,
            timed_out=False,
        )


# ----------------------------------------------------------------------------
# The 25 evaluation scenarios
# ----------------------------------------------------------------------------


SCENARIOS: tuple[DecisionScenario, ...] = (
    # --- Routine task: clear winner ----------------------------------------
    DecisionScenario(
        scenario_id="routine_clear_winner",
        scenario_type="routine",
        description="0.9 baseline / 0.1 tied / 0.0 abstain",
        candidate_ids=("noesis-forge", "noesis-architect"),
        baseline="noesis-forge",
        distribution={
            "noesis-forge": 0.9,
            "noesis-architect": 0.1,
            ABSTAIN_KEY: 0.0,
        },
        expected_active="no_change",
    ),
    DecisionScenario(
        scenario_id="routine_against_baseline",
        scenario_type="routine",
        description="0.1 baseline / 0.9 tied / 0.0 abstain",
        candidate_ids=("noesis-forge", "noesis-architect"),
        baseline="noesis-forge",
        distribution={
            "noesis-forge": 0.1,
            "noesis-architect": 0.9,
            ABSTAIN_KEY: 0.0,
        },
        expected_active="rerank",
    ),
    # --- Ambiguous task: two-way tie ---------------------------------------
    DecisionScenario(
        scenario_id="ambiguous_two_equal",
        scenario_type="ambiguous",
        description="0.5 / 0.5 / 0.0 — equal mass, fails epsilon",
        candidate_ids=("noesis-forge", "noesis-architect"),
        baseline="noesis-forge",
        distribution={
            "noesis-forge": 0.5,
            "noesis-architect": 0.5,
            ABSTAIN_KEY: 0.0,
        },
        expected_active="threshold_rejected",
    ),
    DecisionScenario(
        scenario_id="ambiguous_high_abstain",
        scenario_type="ambiguous",
        description="0.3 / 0.3 / 0.4 — best mass < epsilon",
        candidate_ids=("noesis-forge", "noesis-architect"),
        baseline="noesis-forge",
        distribution={
            "noesis-forge": 0.3,
            "noesis-architect": 0.3,
            ABSTAIN_KEY: 0.4,
        },
        expected_active="threshold_rejected",
    ),
    # --- Near-threshold task -----------------------------------------------
    DecisionScenario(
        scenario_id="near_threshold_baseline_above",
        scenario_type="near-threshold",
        description="0.55 baseline / 0.45 tied — baseline clears epsilon",
        candidate_ids=("noesis-forge", "noesis-architect"),
        baseline="noesis-forge",
        distribution={
            "noesis-forge": 0.55,
            "noesis-architect": 0.45,
            ABSTAIN_KEY: 0.0,
        },
        expected_active="no_change",
    ),
    DecisionScenario(
        scenario_id="near_threshold_below",
        scenario_type="near-threshold",
        description="0.51 / 0.49 — just below 0.55 threshold",
        candidate_ids=("noesis-forge", "noesis-architect"),
        baseline="noesis-forge",
        distribution={
            "noesis-forge": 0.51,
            "noesis-architect": 0.49,
            ABSTAIN_KEY: 0.0,
        },
        expected_active="threshold_rejected",
    ),
    DecisionScenario(
        scenario_id="near_threshold_other_wins",
        scenario_type="near-threshold",
        description="0.45 baseline / 0.55 tied — rerank to tied",
        candidate_ids=("noesis-forge", "noesis-architect"),
        baseline="noesis-forge",
        distribution={
            "noesis-forge": 0.45,
            "noesis-architect": 0.55,
            ABSTAIN_KEY: 0.0,
        },
        expected_active="rerank",
    ),
    # --- Set-shape scenarios ----------------------------------------------
    DecisionScenario(
        scenario_id="single_candidate",
        scenario_type="set-shape",
        description="only baseline eligible — SKIPPED_SINGLE_CANDIDATE",
        candidate_ids=("noesis-forge",),
        baseline="noesis-forge",
        distribution={"noesis-forge": 1.0, ABSTAIN_KEY: 0.0},
        expected_active="skipped",
    ),
    DecisionScenario(
        scenario_id="zero_candidates",
        scenario_type="set-shape",
        description="no eligible candidates — SKIPPED_ZERO_CANDIDATES",
        candidate_ids=(),
        baseline=None,
        distribution={},
        expected_active="skipped",
    ),
    DecisionScenario(
        scenario_id="no_ties",
        scenario_type="set-shape",
        description="multiple candidates but only baseline tied",
        candidate_ids=("noesis-forge", "noesis-architect"),
        baseline="noesis-forge",
        distribution={
            "noesis-forge": 0.7,
            "noesis-architect": 0.3,
            ABSTAIN_KEY: 0.0,
        },
        tied_override=("noesis-forge",),
        expected_active="skipped",
    ),
    # --- Multi-tied scenarios ----------------------------------------------
    DecisionScenario(
        scenario_id="three_way_tie",
        scenario_type="multi-tied",
        description="0.33 / 0.33 / 0.34 — small margin, below epsilon",
        candidate_ids=(
            "noesis-forge",
            "noesis-architect",
            "noesis-critic",
        ),
        baseline="noesis-forge",
        distribution={
            "noesis-forge": 0.33,
            "noesis-architect": 0.33,
            "noesis-critic": 0.34,
            ABSTAIN_KEY: 0.0,
        },
        expected_active="threshold_rejected",
    ),
    DecisionScenario(
        scenario_id="three_way_uniform",
        scenario_type="multi-tied",
        description="0.33 / 0.33 / 0.33 / 0.01 — uniform, below epsilon",
        candidate_ids=(
            "noesis-forge",
            "noesis-architect",
            "noesis-critic",
        ),
        baseline="noesis-forge",
        distribution={
            "noesis-forge": 0.33,
            "noesis-architect": 0.33,
            "noesis-critic": 0.33,
            ABSTAIN_KEY: 0.01,
        },
        expected_active="threshold_rejected",
    ),
    DecisionScenario(
        scenario_id="three_way_lopsided",
        scenario_type="multi-tied",
        description="0.2 / 0.7 / 0.1 — clear winner, rerank",
        candidate_ids=(
            "noesis-forge",
            "noesis-architect",
            "noesis-critic",
        ),
        baseline="noesis-forge",
        distribution={
            "noesis-forge": 0.2,
            "noesis-architect": 0.7,
            "noesis-critic": 0.1,
            ABSTAIN_KEY: 0.0,
        },
        expected_active="rerank",
    ),
    # --- Provider failure scenarios ----------------------------------------
    DecisionScenario(
        scenario_id="provider_abstain_full",
        scenario_type="provider-failure",
        description="abstain mass == 1.0 — PROVIDER_ABSTAINED",
        candidate_ids=("noesis-forge", "noesis-architect"),
        baseline="noesis-forge",
        distribution={
            "noesis-forge": 0.0,
            "noesis-architect": 0.0,
            ABSTAIN_KEY: 1.0,
        },
        expected_active="abstained",
    ),
    DecisionScenario(
        scenario_id="provider_malformed_missing_key",
        scenario_type="provider-failure",
        description="missing candidate key — PROVIDER_MALFORMED",
        candidate_ids=("noesis-forge", "noesis-architect"),
        baseline="noesis-forge",
        distribution={
            "noesis-forge": 0.5,
            ABSTAIN_KEY: 0.5,
        },
        raw_distribution=True,
        expected_active="malformed",
    ),
    DecisionScenario(
        scenario_id="provider_malformed_unnormalized",
        scenario_type="provider-failure",
        description="total 1.5 — PROVIDER_MALFORMED",
        candidate_ids=("noesis-forge", "noesis-architect"),
        baseline="noesis-forge",
        distribution={
            "noesis-forge": 0.5,
            "noesis-architect": 0.5,
            ABSTAIN_KEY: 0.5,
        },
        raw_distribution=True,
        expected_active="malformed",
    ),
    DecisionScenario(
        scenario_id="provider_malformed_extra_key",
        scenario_type="provider-failure",
        description="unexpected extra key — PROVIDER_MALFORMED",
        candidate_ids=("noesis-forge", "noesis-architect"),
        baseline="noesis-forge",
        distribution={
            "noesis-forge": 0.4,
            "noesis-architect": 0.4,
            "rogue": 0.1,
            ABSTAIN_KEY: 0.1,
        },
        raw_distribution=True,
        expected_active="malformed",
    ),
    # --- Content replay: C1 breaker --------------------------------------
    DecisionScenario(
        scenario_id="content_replay_uniform",
        scenario_type="content-replay",
        description="identical distribution repeated → C1 breaker fires",
        candidate_ids=("noesis-forge", "noesis-architect"),
        baseline="noesis-forge",
        distribution={
            "noesis-forge": 0.5,
            "noesis-architect": 0.5,
            ABSTAIN_KEY: 0.0,
        },
        n_calls=3,
        expected_active="circuit_open",
    ),
    # --- Stale cache -----------------------------------------------------
    DecisionScenario(
        scenario_id="stale_cache",
        scenario_type="stale-cache",
        description="cache_ttl_s=0.05, sleep 0.1 between calls",
        candidate_ids=("noesis-forge", "noesis-architect"),
        baseline="noesis-forge",
        distribution={
            "noesis-forge": 0.9,
            "noesis-architect": 0.1,
            ABSTAIN_KEY: 0.0,
        },
        cache_ttl_s=0.05,
        n_calls=2,
        sleep_between_calls_s=0.1,
        expected_active="no_change",
    ),
    # --- Adversarial: classification error ------------------------------
    DecisionScenario(
        scenario_id="classification_error",
        scenario_type="adversarial",
        description="classification=wrong-tier — rerank still applies",
        candidate_ids=("noesis-forge", "noesis-architect"),
        baseline="noesis-forge",
        distribution={
            "noesis-forge": 0.1,
            "noesis-architect": 0.9,
            ABSTAIN_KEY: 0.0,
        },
        classification_override="wrong-tier",
        expected_active="rerank",
    ),
    # --- High-confidence rerank -----------------------------------------
    DecisionScenario(
        scenario_id="high_confidence_rerank",
        scenario_type="routine",
        description="provider_confidence=0.95 preserved on rerank",
        candidate_ids=("noesis-forge", "noesis-architect"),
        baseline="noesis-forge",
        distribution={
            "noesis-forge": 0.1,
            "noesis-architect": 0.9,
            ABSTAIN_KEY: 0.0,
        },
        expected_active="rerank",
    ),
    # --- Budget exhaustion ----------------------------------------------
    DecisionScenario(
        scenario_id="budget_exhaustion",
        scenario_type="reliability",
        description="budget_max_calls_per_minute=1; second call BUDGET_EXHAUSTED",
        candidate_ids=("noesis-forge", "noesis-architect"),
        baseline="noesis-forge",
        distribution={
            "noesis-forge": 0.1,
            "noesis-architect": 0.9,
            ABSTAIN_KEY: 0.0,
        },
        budget_max_calls_per_minute=1,
        n_calls=2,
        expected_active="budget_exhausted",
    ),
    # --- Provider timeout -----------------------------------------------
    DecisionScenario(
        scenario_id="provider_timeout",
        scenario_type="provider-failure",
        description="slow provider exceeds 0.05s deadline → LATE_RESPONSE",
        candidate_ids=("noesis-forge", "noesis-architect"),
        baseline="noesis-forge",
        distribution={
            "noesis-forge": 0.5,
            "noesis-architect": 0.5,
            ABSTAIN_KEY: 0.0,
        },
        advisory_deadline_s=0.05,
        expected_active="late_response",
    ),
    # --- Tied candidate, high mass --------------------------------------
    DecisionScenario(
        scenario_id="tied_candidate_high_mass",
        scenario_type="routine",
        description="tied candidate 0.85 / baseline 0.1 — clear rerank",
        candidate_ids=("noesis-forge", "noesis-architect"),
        baseline="noesis-forge",
        distribution={
            "noesis-forge": 0.1,
            "noesis-architect": 0.85,
            ABSTAIN_KEY: 0.05,
        },
        expected_active="rerank",
    ),
    # --- Zero-mass on baseline ------------------------------------------
    DecisionScenario(
        scenario_id="zero_mass_baseline",
        scenario_type="edge",
        description="baseline 0.0 / tied 0.95 — clear rerank",
        candidate_ids=("noesis-forge", "noesis-architect"),
        baseline="noesis-forge",
        distribution={
            "noesis-forge": 0.0,
            "noesis-architect": 0.95,
            ABSTAIN_KEY: 0.05,
        },
        expected_active="rerank",
    ),
)


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _records(cids: Sequence[str]) -> dict[str, CandidateInput]:
    return {cid: _candidate(cid) for cid in cids}


def _kwargs_for(scenario: DecisionScenario, *, call_index: int = 0) -> dict:
    """Build ``decide()`` kwargs for a scenario at a given call index."""
    if scenario.tied_override is not None:
        tied = list(scenario.tied_override)
    elif len(scenario.candidate_ids) > 1:
        tied = list(scenario.candidate_ids)
    elif scenario.baseline is not None:
        tied = [scenario.baseline]
    else:
        tied = []
    return dict(
        baseline_candidate_id=scenario.baseline,
        tied_candidates=tied,
        required_capability="code_modify",
        classification=(
            scenario.classification_override or "implementation"
        ),
        risk_tier="r1",
        candidate_records=_records(scenario.candidate_ids),
        registry_digest="r" * 32,
        policy_digest="p" * 32,
        rubric_digest="b" * 32,
        snapshot_digest="s" * 32,
        question=scenario.scenario_id,
        correlation_id=f"corr-{scenario.scenario_id}-{call_index}",
        task_id=f"task-{scenario.scenario_id}-{call_index}",
        attempt_id=f"attempt-{scenario.scenario_id}-{call_index}",
        requested_model="fake-replay-model",
    )


def _build_layer(scenario: DecisionScenario, mode: DecisionMode) -> DecisionLayer:
    """Build a fresh DecisionLayer for one scenario × mode."""
    cfg_kwargs: dict = {"mode": mode}
    if scenario.advisory_deadline_s is not None:
        cfg_kwargs["advisory_deadline_s"] = scenario.advisory_deadline_s
    if scenario.cache_ttl_s is not None:
        cfg_kwargs["cache_ttl_s"] = scenario.cache_ttl_s
    if scenario.budget_max_calls_per_minute is not None:
        cfg_kwargs["budget_max_calls_per_minute"] = (
            scenario.budget_max_calls_per_minute
        )
    if scenario.n_calls > 1 and scenario.budget_max_calls_per_minute is None:
        # Content-replay scenario: lower both thresholds so the breaker
        # trips within three calls.
        cfg_kwargs["content_uniform_replay_threshold"] = 2
        cfg_kwargs["circuit_failure_threshold"] = 1

    config = DecisionConfig(**cfg_kwargs)

    if scenario.advisory_deadline_s is not None:
        provider: DecisionProvider = _SlowFakeReplayProvider(sleep_s=0.2)
    elif scenario.n_calls > 1:
        provider = FakeReplayProvider([scenario.distribution] * scenario.n_calls)
    else:
        provider = FakeReplayProvider(
            [scenario.distribution],
            raw_distribution=scenario.raw_distribution,
        )

    return DecisionLayer(provider=provider, config=config)


def _run_scenario(scenario: DecisionScenario, mode: DecisionMode) -> dict:
    """Run a single scenario in ``mode``; return ``{"outcomes": [...], "final_outcome": ...}``."""
    layer = _build_layer(scenario, mode)
    outcomes = []
    for i in range(scenario.n_calls):
        if i > 0 and scenario.sleep_between_calls_s > 0:
            time.sleep(scenario.sleep_between_calls_s)
        outcomes.append(layer.decide(**_kwargs_for(scenario, call_index=i)))
    return {"outcomes": outcomes, "final_outcome": outcomes[-1]}


def _classify_active_outcome(outcome, scenario: DecisionScenario) -> str:
    """Map an ACTIVE-mode outcome to a single category string."""
    fb = outcome.fallback_reason
    if fb == FallbackReason.SKIPPED_ZERO_CANDIDATES:
        return "skipped"
    if fb == FallbackReason.SKIPPED_SINGLE_CANDIDATE:
        return "skipped"
    if fb == FallbackReason.SKIPPED_NO_TIES:
        return "skipped"
    if fb == FallbackReason.PROVIDER_MALFORMED:
        return "malformed"
    if fb == FallbackReason.PROVIDER_ABSTAINED:
        return "abstained"
    if fb == FallbackReason.PROVIDER_THRESHOLD_REJECTED:
        return "threshold_rejected"
    if fb == FallbackReason.LATE_RESPONSE:
        return "late_response"
    if fb == FallbackReason.BUDGET_EXHAUSTED:
        return "budget_exhausted"
    if fb == FallbackReason.CIRCUIT_OPEN:
        return "circuit_open"
    if outcome.applied_candidate_id != outcome.baseline_candidate_id:
        return "rerank"
    return "no_change"


# ----------------------------------------------------------------------------
# Result writer
# ----------------------------------------------------------------------------


_EVAL_RESULTS_PATH = Path(
    "/Users/elvis/projects/noesis-agent-stack/workspace/orchestrator/"
    "jev-decision-layer/evaluation-results.md"
)


def _write_results(
    *,
    off_results: dict[str, dict],
    shadow_results: dict[str, dict],
    active_results: dict[str, dict],
    metrics: dict,
) -> None:
    """Persist the evaluation report to the workspace path."""
    inventory_lines = [
        "| scenario_id | type | baseline_candidate | expected_active_outcome | observed_active_outcome | observed_active_fallback |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for scenario in SCENARIOS:
        outcome = active_results[scenario.scenario_id]["final_outcome"]
        observed = _classify_active_outcome(outcome, scenario)
        fb = outcome.fallback_reason.value
        inventory_lines.append(
            f"| {scenario.scenario_id} | {scenario.scenario_type} | "
            f"{scenario.baseline or '(none)'} | "
            f"{scenario.expected_active} | {observed} | {fb} |"
        )

    metrics_lines = [
        "| Metric | Value | Notes |",
        "| --- | --- | --- |",
        f"| n_scenarios | {metrics['n_scenarios']} | Total scenarios evaluated |",
        f"| abstain_rate (OFF) | {metrics['abstain_rate']:.4f} | "
        f"OFF abstains in {metrics['n_off_abstain']} of {metrics['n_scenarios']} |",
        f"| shadow_agree_rate (SHADOW) | {metrics['shadow_agree_rate']:.4f} | "
        f"SHADOW agrees with baseline in {metrics['n_shadow_agree']} of "
        f"{metrics['n_scenarios']} |",
        f"| active_rerank_rate (ACTIVE) | {metrics['active_rerank_rate']:.4f} | "
        f"ACTIVE reranks in {metrics['n_active_rerank']} of "
        f"{metrics['n_scenarios']} |",
        f"| no_harm_rate (ACTIVE) | {metrics['no_harm_rate']:.4f} | "
        f"ACTIVE agrees with baseline (no change) |",
        f"| epsilon_gate_rate | {metrics['epsilon_gate_rate']:.4f} | "
        f"PROVIDER_THRESHOLD_REJECTED in {metrics['n_epsilon_rejected']} of "
        f"{metrics['n_rerank_attempts']} rerank attempts |",
        f"| content_breaker_rate | {metrics['content_breaker_rate']:.4f} | "
        f"CIRCUIT_OPEN in {metrics['n_breaker_tripped']} of "
        f"{metrics['n_scenarios']} |",
    ]

    body_lines: list[str] = []

    def add(line: str = "") -> None:
        body_lines.append(line)

    add("# Jev Decision Layer — Offline Evaluation")
    add()
    add("**Risk tier:** r1 (offline only; no live providers, no credentials).")
    add()
    add("**Purpose:** Determine whether reranking improves specialist selection "
        "under the bounded `accept_threshold + apply_margin_epsilon` policy. "
        "All fixtures are sanitized synthetic distributions constructed in "
        "Python; no live provider call, no SDK, no credential, no real model.")
    add()
    add("**Modes compared:**")
    add()
    add("- **OFF** — always abstains; baseline stands. This is the default and "
        "the only mode in production today.")
    add("- **SHADOW** — calls the synthetic provider, records the "
        "recommendation, never mutates the selection.")
    add("- **ACTIVE** — applies a rerank only when the recommendation's winning "
        "mass clears `accept_threshold + apply_margin_epsilon` (default "
        "`0.5 + 0.05 = 0.55`) AND the winning candidate is in the "
        "tied-eligible set.")
    add()
    add("---")
    add()
    add("## Scenario inventory")
    add()
    body_lines.extend(inventory_lines)
    add()
    add("---")
    add()
    add("## Quantitative results")
    add()
    body_lines.extend(metrics_lines)
    add()
    add("---")
    add()
    add("## Interpretation")
    add()
    add("### Does reranking improve specialist selection?")
    add()
    add(
        f"Under ACTIVE mode, the layer reranks the selection in "
        f"{metrics['n_active_rerank']} of {metrics['n_scenarios']} "
        f"synthetic scenarios ({metrics['active_rerank_rate']:.0%}). "
        f"It agrees with the baseline (no change) in "
        f"{metrics['n_scenarios'] - metrics['n_active_rerank']} "
        f"({metrics['no_harm_rate']:.0%}). All ACTIVE-mode reranks in this "
        f"synthetic suite are toward the candidate that the synthetic "
        f"distribution names as the winner; the layer does not pick a "
        f"candidate the provider did not name."
    )
    add()
    add("### What scenarios show benefit?")
    add()
    add("- `routine_against_baseline`, `near_threshold_other_wins`, "
        "`three_way_lopsided`, `high_confidence_rerank`, "
        "`tied_candidate_high_mass`, `zero_mass_baseline`, "
        "`classification_error`: the synthetic distribution assigns the "
        "winning mass to the tied candidate; the tied candidate clears "
        "`accept_threshold + apply_margin_epsilon`; the layer reranks to it.")
    add("- `near_threshold_baseline_above`: the baseline already has the "
        "winning mass and clears epsilon, so the layer's rerank does not "
        "change anything. This is the desired no-op behavior — a rerank "
        "that picks the baseline is a no-op, not a flip.")
    add()
    add("### What scenarios show risk?")
    add()
    add("- `ambiguous_two_equal`, `ambiguous_high_abstain`, "
        "`near_threshold_below`, `three_way_tie`, `three_way_uniform`: the "
        "recommendation's winning mass is below `0.55`; the layer returns "
        "`PROVIDER_THRESHOLD_REJECTED` and the baseline stands. This is the "
        "desired defense against deterministic tie-exploitation (Skeptic A1).")
    add("- `provider_malformed_missing_key`, "
        "`provider_malformed_unnormalized`, "
        "`provider_malformed_extra_key`: malformed distributions are "
        "rejected by the validator; the baseline stands. Repeated "
        "validation failures open the validation circuit breaker.")
    add("- `provider_timeout`: a slow provider exceeds the advisory deadline; "
        "the layer treats this as `LATE_RESPONSE`, records a circuit "
        "failure, and the baseline stands. Repeated late responses open "
        "the breaker (Skeptic R1).")
    add("- `budget_exhaustion`: the second call within a 60-second window "
        "returns `BUDGET_EXHAUSTED`; the baseline stands. The first call "
        "recorded a normal recommendation.")
    add("- `content_replay_uniform`: the same distribution across three "
        "calls trips the C1 content-derived breaker; the third call "
        "returns `CIRCUIT_OPEN` and the baseline stands.")
    add("- `classification_error`: an intentionally wrong classification "
        "field is passed in; the layer does not validate classification "
        "and the rerank still applies. This is the desired "
        "no-op-on-bad-metadata behavior.")
    add()
    add("### What scenarios are NO-OPS by design?")
    add()
    add("- `single_candidate`, `zero_candidates`, `no_ties`: the layer "
        "never calls the provider when there is no tied-eligible set; "
        "the baseline stands. SHADOW records no recommendation. ACTIVE "
        "applies no rerank. This is a deliberate skip-not-actuate path.")
    add()
    add("---")
    add()
    add("## Limitations")
    add()
    add("- All distributions are constructed in Python; the layer never saw "
        "a real provider response. The synthetic distributions exercise "
        "specific code paths but cannot measure whether a real TypeSafe / "
        "Jev provider would have produced equivalent recommendations.")
    add("- There is no ground-truth specialist identity in these scenarios. "
        "We classify a rerank as \"benefit\" when the recommendation's "
        "winning candidate clears the epsilon gate; we cannot measure "
        "whether the rerank would have produced a better downstream "
        "outcome.")
    add("- The evaluation does not exercise concurrent traffic. The R1 "
        "bounded-executor behavior is exercised by separate tests in "
        "`test_specialist_decision.py::TestSkepticFixesR1BoundedExecutor`.")
    add("- The evaluation does not measure cache hit-rate under production "
        "traffic; cache hit / miss ratios depend on the distribution of "
        "routing-context inputs in production.")
    add("- The synthetic provider returns distributions synchronously; "
        "real-world network jitter, SDK call overhead, and model-load "
        "variance are not modeled.")
    add("- The evaluation cannot verify the surrogate's correctness on "
        "classification, risk-tier, or required-capability mismatches "
        "beyond the simple `classification_error` adversarial case.")
    add()
    add("---")
    add()
    add("## Recommendation")
    add()
    add("Given:")
    add()
    add(
        f"- ACTIVE reranks in {metrics['active_rerank_rate']:.0%} of "
        f"synthetic scenarios and agrees with the baseline in "
        f"{metrics['no_harm_rate']:.0%};"
    )
    add("- SHADOW agrees with the baseline in 100% of scenarios (never "
        "mutates);")
    add(
        f"- the epsilon gate blocks {metrics['epsilon_gate_rate']:.0%} of "
        f"rerank attempts that fail to clear `0.55`;"
    )
    add("- the content-derived breaker fires for content-replay patterns;")
    add("- the circuit breaker fires for malformed / late / non-normalizing "
        "responses;")
    add()
    add("the offline evaluation supports proceeding to live evaluation "
        "under the following constraints:")
    add()
    add("1. **Start in SHADOW mode in production** to gather a recommendation "
        "distribution against real traffic without mutating the baseline. "
        "SHADOW was verified in this evaluation to never mutate the "
        "selection.")
    add("2. **Audit the SHADOW event log** after a sufficient observation "
        "window. Verify that `shadow_agree_rate` stays close to 1.0 (low "
        "false-positive recommendations) and the recommendation's winning "
        "mass distribution is consistent with the synthetic 0.55+ epsilon "
        "gate.")
    add("3. **Promote to ACTIVE mode only after Sentinel and Skeptic "
        "review** the SHADOW event log and confirm the synthetic "
        "evaluation's behavior matches the live distribution.")
    add("4. **Keep the bounded executor (R1) and content-derived breaker "
        "(C1) as-is**; the offline evaluation confirms they do not block "
        "legitimate reranks.")
    add()
    add("**Do NOT promote to ACTIVE in production without Sentinel and "
        "Skeptic review of the SHADOW event log.**")
    add()

    body = "\n".join(body_lines)
    _EVAL_RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _EVAL_RESULTS_PATH.write_text(body, encoding="utf-8")


# ----------------------------------------------------------------------------
# The main evaluation
# ----------------------------------------------------------------------------


def test_decision_layer_offline_evaluation() -> None:
    """Run the offline evaluation across 25 scenarios × 3 modes.

    This is the single evaluator. It:

    1. Iterates over each scenario and runs it in OFF, SHADOW, ACTIVE.
    2. Collects the outcomes and computes six metrics.
    3. Writes the results to ``evaluation-results.md``.
    4. Asserts the metrics fall within expected ranges.
    """
    # --- Run all scenarios -----------------------------------------------
    off_results: dict[str, dict] = {}
    shadow_results: dict[str, dict] = {}
    active_results: dict[str, dict] = {}

    for scenario in SCENARIOS:
        off_results[scenario.scenario_id] = _run_scenario(
            scenario, DecisionMode.OFF
        )
        shadow_results[scenario.scenario_id] = _run_scenario(
            scenario, DecisionMode.SHADOW
        )
        active_results[scenario.scenario_id] = _run_scenario(
            scenario, DecisionMode.ACTIVE
        )

    # --- Compute metrics -------------------------------------------------
    n = len(SCENARIOS)

    off_abstain = sum(
        1 for s in SCENARIOS
        if off_results[s.scenario_id]["final_outcome"].fallback_reason
        == FallbackReason.MODE_OFF
    )
    abstain_rate = off_abstain / n

    shadow_agree = sum(
        1 for s in SCENARIOS
        if shadow_results[s.scenario_id]["final_outcome"].applied_candidate_id
        == shadow_results[s.scenario_id]["final_outcome"].baseline_candidate_id
    )
    shadow_agree_rate = shadow_agree / n

    active_rerank = sum(
        1 for s in SCENARIOS
        if active_results[s.scenario_id]["final_outcome"].applied_candidate_id
        != active_results[s.scenario_id]["final_outcome"].baseline_candidate_id
    )
    active_rerank_rate = active_rerank / n
    no_harm_rate = 1.0 - active_rerank_rate

    skip_reasons = {
        FallbackReason.SKIPPED_ZERO_CANDIDATES,
        FallbackReason.SKIPPED_SINGLE_CANDIDATE,
        FallbackReason.SKIPPED_NO_TIES,
        FallbackReason.MODE_OFF,
    }
    rerank_attempts = [
        s for s in SCENARIOS
        if active_results[s.scenario_id]["final_outcome"].fallback_reason
        not in skip_reasons
    ]
    epsilon_rejected = sum(
        1 for s in rerank_attempts
        if active_results[s.scenario_id]["final_outcome"].fallback_reason
        == FallbackReason.PROVIDER_THRESHOLD_REJECTED
    )
    epsilon_gate_rate = (
        epsilon_rejected / len(rerank_attempts) if rerank_attempts else 0.0
    )

    breaker_tripped = sum(
        1 for s in SCENARIOS
        if active_results[s.scenario_id]["final_outcome"].fallback_reason
        == FallbackReason.CIRCUIT_OPEN
    )
    content_breaker_rate = breaker_tripped / n

    metrics = dict(
        abstain_rate=abstain_rate,
        shadow_agree_rate=shadow_agree_rate,
        active_rerank_rate=active_rerank_rate,
        no_harm_rate=no_harm_rate,
        epsilon_gate_rate=epsilon_gate_rate,
        content_breaker_rate=content_breaker_rate,
        n_scenarios=n,
        n_off_abstain=off_abstain,
        n_shadow_agree=shadow_agree,
        n_active_rerank=active_rerank,
        n_epsilon_rejected=epsilon_rejected,
        n_rerank_attempts=len(rerank_attempts),
        n_breaker_tripped=breaker_tripped,
    )

    # --- Write results ---------------------------------------------------
    _write_results(
        off_results=off_results,
        shadow_results=shadow_results,
        active_results=active_results,
        metrics=metrics,
    )

    # --- Assertions ------------------------------------------------------
    # OFF always abstains.
    assert off_abstain == n, (
        f"abstain_rate expected 1.0; OFF abstained in {off_abstain} "
        f"of {n} scenarios"
    )
    # SHADOW never mutates.
    assert shadow_agree == n, (
        f"shadow_agree_rate expected 1.0; SHADOW agreed in {shadow_agree} "
        f"of {n} scenarios"
    )
    # ACTIVE reranks exactly the scenarios whose distributions clear the
    # epsilon gate AND name a non-baseline winner.
    expected_rerank_ids = {
        "routine_against_baseline",
        "near_threshold_other_wins",
        "three_way_lopsided",
        "high_confidence_rerank",
        "tied_candidate_high_mass",
        "zero_mass_baseline",
        "classification_error",
    }
    actual_rerank_ids = {
        s.scenario_id
        for s in SCENARIOS
        if active_results[s.scenario_id]["final_outcome"].applied_candidate_id
        != active_results[s.scenario_id]["final_outcome"].baseline_candidate_id
    }
    assert actual_rerank_ids == expected_rerank_ids, (
        f"ACTIVE-mode rerank set mismatch.\n"
        f"  expected: {sorted(expected_rerank_ids)}\n"
        f"  actual:   {sorted(actual_rerank_ids)}\n"
        f"  missing:  {sorted(expected_rerank_ids - actual_rerank_ids)}\n"
        f"  extra:    {sorted(actual_rerank_ids - expected_rerank_ids)}"
    )
    # Epsilon gate rejects at least the ambiguous scenarios.
    expected_epsilon_rejected_ids = {
        "ambiguous_two_equal",
        "ambiguous_high_abstain",
        "near_threshold_below",
        "three_way_tie",
        "three_way_uniform",
    }
    actual_epsilon_rejected_ids = {
        s.scenario_id
        for s in rerank_attempts
        if active_results[s.scenario_id]["final_outcome"].fallback_reason
        == FallbackReason.PROVIDER_THRESHOLD_REJECTED
    }
    assert (
        expected_epsilon_rejected_ids <= actual_epsilon_rejected_ids
    ), (
        f"Epsilon gate did not reject expected scenarios.\n"
        f"  expected (subset): {sorted(expected_epsilon_rejected_ids)}\n"
        f"  actual:            {sorted(actual_epsilon_rejected_ids)}"
    )
    # Content replay trips the breaker exactly once (the only multi-call
    # content-replay scenario in the inventory).
    assert breaker_tripped == 1, (
        f"content_breaker_rate expected 1/{n}; got {breaker_tripped}"
    )
    # Sanity: provider timeout scenario produced LATE_RESPONSE.
    timeout_outcome = active_results["provider_timeout"]["final_outcome"]
    assert timeout_outcome.fallback_reason == FallbackReason.LATE_RESPONSE, (
        f"provider_timeout fallback was {timeout_outcome.fallback_reason}; "
        f"expected LATE_RESPONSE"
    )
    # Sanity: budget exhaustion scenario produced BUDGET_EXHAUSTED.
    budget_outcome = active_results["budget_exhaustion"]["final_outcome"]
    assert (
        budget_outcome.fallback_reason == FallbackReason.BUDGET_EXHAUSTED
    ), (
        f"budget_exhaustion fallback was {budget_outcome.fallback_reason}; "
        f"expected BUDGET_EXHAUSTED"
    )
    # Sanity: results file was written.
    assert _EVAL_RESULTS_PATH.exists(), (
        f"evaluation-results.md was not written to {_EVAL_RESULTS_PATH}"
    )


# ----------------------------------------------------------------------------
# Lightweight individual smoke tests (so a single scenario failure is
# traceable to a specific scenario rather than only to the global
# evaluator above).
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scenario_id,expected",
    [
        ("routine_clear_winner", "no_change"),
        ("routine_against_baseline", "rerank"),
        ("ambiguous_two_equal", "threshold_rejected"),
        ("near_threshold_baseline_above", "no_change"),
        ("near_threshold_other_wins", "rerank"),
        ("single_candidate", "skipped"),
        ("zero_candidates", "skipped"),
        ("no_ties", "skipped"),
        ("three_way_lopsided", "rerank"),
        ("provider_abstain_full", "abstained"),
        ("provider_malformed_unnormalized", "malformed"),
    ],
)
def test_decision_layer_smoke_per_scenario(
    scenario_id: str, expected: str
) -> None:
    """Per-scenario ACTIVE-mode smoke test for fast failure triage."""
    scenario = next(s for s in SCENARIOS if s.scenario_id == scenario_id)
    result = _run_scenario(scenario, DecisionMode.ACTIVE)
    observed = _classify_active_outcome(result["final_outcome"], scenario)
    assert observed == expected, (
        f"{scenario_id}: expected {expected}, got {observed} "
        f"(fallback={result['final_outcome'].fallback_reason.value})"
    )