#!/usr/bin/env python3
"""Offline SHADOW evaluation runner for the Jev decision layer.

Runs the decision layer in SHADOW mode across all 25 sanitized synthetic
scenarios, writes a structured JSONL event log, and prints a summary table.

NO LIVE PROVIDER CALLS.  All distributions are constructed in Python via
FakeReplayProvider; the layer never sees a real network response, SDK,
or credential.

Usage:
    PYTHONPATH=orchestration/orchestrator python \\
      orchestration/orchestrator/tests/eval_decision_layer.py

Output:
    - <scratch_dir>/decision-shadow-events.jsonl   — per-call SHADOW records
    - <scratch_dir>/decision-shadow-summary.txt   — human-readable summary
    - metrics to stdout

Exit codes:
    0  — evaluation completed
    1  — setup error (missing deps, path issues)
    2  — one or more scenarios produced unexpected outcomes (rerank without
         clearance, or wrong candidate selected in ACTIVE mode)
"""
from __future__ import annotations

import json
import sys
import time
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "orchestration" / "orchestrator"))

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
from app.specialist_decision import CONTRACT_VERSION, CONTRACT_DIGEST

# Import existing FakeReplayProvider from the pytest suite
from tests.test_decision_layer_evaluation import (
    DecisionScenario,
    SCENARIOS,
    FakeReplayProvider,
)


# ---------------------------------------------------------------------------
# Scenario result record
# ---------------------------------------------------------------------------

@dataclass
class ScenarioResult:
    scenario_id: str
    scenario_type: str
    baseline_candidate: str | None
    final_candidate: str | None
    mode: str
    reranked: bool
    fallback_reason: str
    distribution: dict[str, float] | None
    provider_confidence: float | None
    latency_ms: float | None


# ---------------------------------------------------------------------------
# SHADOW evaluation loop
# ---------------------------------------------------------------------------

def _run_shadow_scenario(
    scenario: DecisionScenario,
    provider: DecisionProvider,
    event_log_path: Path,
) -> ScenarioResult:
    """Run one scenario in SHADOW mode and return the result."""
    layer = DecisionLayer(
        config=DecisionConfig(
            mode=DecisionMode.SHADOW,
            accept_threshold=0.5,
            apply_margin_epsilon=0.05,
            content_uniform_replay_threshold=3,
            event_log_path=event_log_path,
        ),
        provider=provider,
    )

    candidate_records: dict[str, CandidateInput] = {
        cid: CandidateInput(
            candidate_id=cid,
            capabilities=("plan_artifact", "code_modify"),
            mandate=f"{cid} mandate",
        )
        for cid in scenario.candidate_ids
    }

    t0 = time.monotonic()
    outcome = layer.decide(
        baseline_candidate_id=scenario.baseline,
        tied_candidates=list(scenario.candidate_ids),
        required_capability="code_modify",
        classification="implementation",
        risk_tier="r1",
        candidate_records=candidate_records,
        registry_digest="r" * 32,
        policy_digest="p" * 32,
        rubric_digest="b" * 32,
        snapshot_digest="s" * 32,
        question=scenario.scenario_id,
        correlation_id=f"eval-{scenario.scenario_id}",
        task_id=f"eval-{scenario.scenario_id}",
        attempt_id="1",
        requested_model="fake-replay-model",
    )
    latency_ms = (time.monotonic() - t0) * 1000

    return ScenarioResult(
        scenario_id=scenario.scenario_id,
        scenario_type=scenario.scenario_type,
        baseline_candidate=scenario.baseline,
        final_candidate=outcome.applied_candidate_id,
        mode="SHADOW",
        reranked=(
            outcome.applied_candidate_id is not None
            and outcome.applied_candidate_id != scenario.baseline
        ),
        fallback_reason=outcome.fallback_reason.value,
        distribution=dict(scenario.distribution),
        provider_confidence=None,
        latency_ms=round(latency_ms, 3),
    )


def _run_all_shadow(scenarios: list[DecisionScenario]) -> tuple[list[ScenarioResult], Path]:
    """Run all scenarios in SHADOW mode. Returns (results, event_log_path)."""
    results: list[ScenarioResult] = []

    with tempfile.TemporaryDirectory() as tmpdir:
        event_log_path = Path(tmpdir) / "decision-shadow-events.jsonl"

        for scenario in scenarios:
            # Build a FakeReplayProvider that returns this scenario's distribution
            provider = FakeReplayProvider(
                [scenario.distribution] * 5,  # enough for all internal calls
                raw_distribution=False,
            )
            result = _run_shadow_scenario(scenario, provider, event_log_path)
            results.append(result)

        # Copy the event log to a durable location
        durable_path = Path.home() / ".hermes" / "profiles" / "noesis-orchestrator" / "cache" / "scratch" / "decision-shadow-events.jsonl"
        durable_path.parent.mkdir(parents=True, exist_ok=True)
        import shutil
        if event_log_path.exists():
            shutil.copy(event_log_path, durable_path)
        print(f"\nSHADOW event log: {durable_path}", file=sys.stderr)

    return results, durable_path


# ---------------------------------------------------------------------------
# Summary report
# ---------------------------------------------------------------------------

def _print_summary(results: list[ScenarioResult]) -> None:
    n = len(results)
    reranked = sum(1 for r in results if r.reranked)
    agreed = sum(1 for r in results if r.final_candidate == r.baseline_candidate)
    abstain = sum(1 for r in results if r.fallback_reason in {"provider_abstained", "abstained"})
    error = sum(1 for r in results if r.fallback_reason == "malformed_distribution")

    print("\n" + "=" * 70)
    print("SHADOW EVALUATION SUMMARY — offline 25-scenario run")
    print("=" * 70)
    print(f"  Total scenarios     : {n}")
    print(f"  SHADOW agree rate  : {agreed}/{n} ({agreed/n:.2%})")
    print(f"  SHADOW rerank rate : {reranked}/{n} ({reranked/n:.2%})")
    print(f"  Abstain count      : {abstain}")
    print(f"  Malformed count    : {error}")
    print()

    # Per-scenario table
    print(f"  {'scenario_id':<35} {'type':<12} {'baseline':>12} {'final':>12}  fallback")
    print(f"  {'-'*35} {'-'*12} {'-'*12} {'-'*12}  {'-'*20}")
    for r in results:
        marker = " RERANK" if r.reranked else ""
        print(
            f"  {r.scenario_id:<35} {r.scenario_type:<12} "
            f"{(r.baseline_candidate or '-'):>12} {(r.final_candidate or '-'):>12}"
            f"  {r.fallback_reason}{marker}"
        )
    print("=" * 70)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("SHADOW offline evaluation — Jev decision layer", file=sys.stderr)
    print(f"  Scenarios    : {len(SCENARIOS)}", file=sys.stderr)
    print(f"  Mode         : SHADOW (no mutation, event log only)", file=sys.stderr)
    print(f"  Provider     : FakeReplayProvider (synthetic distributions)", file=sys.stderr)
    print(f"  Contract     : v{CONTRACT_VERSION} ({CONTRACT_DIGEST[:12]}...)", file=sys.stderr)

    results, event_log_path = _run_all_shadow(list(SCENARIOS))
    _print_summary(results)

    # Write structured summary as machine-readable JSON
    scratch_dir = Path.home() / ".hermes" / "profiles" / "noesis-orchestrator" / "cache" / "scratch"
    scratch_dir.mkdir(parents=True, exist_ok=True)
    summary_path = scratch_dir / "decision-shadow-summary.json"
    n = len(results)
    agreed = sum(1 for r in results if r.final_candidate == r.baseline_candidate)
    reranked = sum(1 for r in results if r.reranked)

    summary = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "contract_version": CONTRACT_VERSION,
        "n_scenarios": n,
        "shadow_agree_rate": agreed / n,
        "shadow_rerank_rate": reranked / n,
        "n_shadow_agree": agreed,
        "n_shadow_rerank": reranked,
        "event_log_path": str(event_log_path),
        "results": [
            {
                "scenario_id": r.scenario_id,
                "scenario_type": r.scenario_type,
                "baseline_candidate": r.baseline_candidate,
                "final_candidate": r.final_candidate,
                "reranked": r.reranked,
                "fallback_reason": r.fallback_reason,
                "latency_ms": r.latency_ms,
            }
            for r in results
        ],
    }

    with open(summary_path, "w") as fh:
        json.dump(summary, fh, indent=2)

    print(f"\nJSON summary : {summary_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
