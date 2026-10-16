"""Baseline-vs-Jev-mock evaluation harness.

Runs the same evidence questions twice: once with the deterministic
baseline, once with the mocked Jev backend, and reports a small table
of recall, false-sufficiency, and provenance-accuracy outcomes. No live
network. No fixture mutation. Output is plain text.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from app.evidence_retrieval import EvidenceQuery, EvidenceRetriever, StopReason
from app.graph import EvidenceState
from app.jev_client import MockJevClient

from tests.fixtures.evidence_fixtures import build_graph, build_no_authorization_graph


@dataclass
class EvalRow:
    scenario: str
    baseline_stop: str
    jev_stop: str
    mandatory_recall_baseline: float
    mandatory_recall_jev: float
    eligible_returned_baseline: int
    eligible_returned_jev: int
    false_sufficiency: bool
    provenance_accuracy: float
    baseline_latency_ms: float
    jev_latency_ms: float


def _mandatory_ids(records) -> set[str]:
    return {r.entity_id for r in records if r.record_kind == "mandatory"}


def _provenance_accuracy(records) -> float:
    if not records:
        return 1.0
    ok = sum(1 for r in records
             if r.source_artifact and r.source_hash and r.evidence_state)
    return ok / len(records)


def _evaluate(scenario_name: str, projection, query: EvidenceQuery,
              jev_script: dict | None,
              expected_mandatory: set[str]) -> EvalRow:
    # Baseline
    base = EvidenceRetriever(projection)
    t0 = time.monotonic()
    base_decision = base.retrieve(query)
    base_ms = (time.monotonic() - t0) * 1000

    # Jev-assisted
    jev = MockJevClient()
    if jev_script is not None:
        jev.script(jev_script)
    jev_retriever = EvidenceRetriever(projection, jev_client=jev)
    t0 = time.monotonic()
    jev_decision = jev_retriever.retrieve(query)
    jev_ms = (time.monotonic() - t0) * 1000

    base_mand = _mandatory_ids(base_decision.records) & expected_mandatory
    jev_mand = _mandatory_ids(jev_decision.records) & expected_mandatory
    base_recall = len(base_mand) / len(expected_mandatory) if expected_mandatory else 1.0
    jev_recall = len(jev_mand) / len(expected_mandatory) if expected_mandatory else 1.0

    # False sufficiency = jev said "stop" while missing mandatory evidence.
    false_suff = (jev_recall < 1.0
                  and jev_decision.stop_reason in (
                      StopReason.JEV_STOP,
                      StopReason.EXACT_IDENTITY_COMPLETE))

    return EvalRow(
        scenario=scenario_name,
        baseline_stop=base_decision.stop_reason.value,
        jev_stop=jev_decision.stop_reason.value,
        mandatory_recall_baseline=base_recall,
        mandatory_recall_jev=jev_recall,
        eligible_returned_baseline=sum(1 for r in base_decision.records
                                       if r.record_kind == "eligible"),
        eligible_returned_jev=sum(1 for r in jev_decision.records
                                  if r.record_kind == "eligible"),
        false_sufficiency=false_suff,
        provenance_accuracy=_provenance_accuracy(jev_decision.records),
        baseline_latency_ms=base_ms,
        jev_latency_ms=jev_ms,
    )


def main() -> None:
    projection = build_graph()
    query = EvidenceQuery(
        task_id="task-1", candidate_revision="cand-A",
        operator_session_scope=("default",),
    )
    jev_stop = {
        "model": "mock-jev-0",
        "answers": {
            "route": {"type": "choice", "choice": "none",
                      "probabilities": {"none": 0.9}, "confidence": 0.9},
            "rank": {"type": "score", "score": 0.0, "confidence": 0.9},
        },
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    jev_expand = {
        "model": "mock-jev-0",
        "answers": {
            "route": {"type": "choice", "choice": "what_blocks",
                      "probabilities": {"what_blocks": 1.0}, "confidence": 0.9},
            "rank": {"type": "score", "score": 1.0, "confidence": 0.9},
        },
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    jev_malformed = {"error": "malformed"}
    jev_timeout = {"error": "timeout"}

    rows = [
        _evaluate("happy_stop", projection, query, jev_stop, {
            "cand-A", "review-spec-pass", "review-quality-approved",
            "finding-blocker", "auth-ok",
        }),
        _evaluate("happy_expand", projection, query, jev_expand, {
            "cand-A", "review-spec-pass", "review-quality-approved",
            "finding-blocker", "auth-ok",
        }),
        _evaluate("malformed", projection, query, jev_malformed, {
            "cand-A", "review-spec-pass", "review-quality-approved",
            "finding-blocker", "auth-ok",
        }),
        _evaluate("timeout", projection, query, jev_timeout, {
            "cand-A", "review-spec-pass", "review-quality-approved",
            "finding-blocker", "auth-ok",
        }),
    ]

    # Missing-mandatory scenario on a different graph.
    no_auth = build_no_authorization_graph()
    q2 = EvidenceQuery(task_id="task-2", candidate_revision="cand-B",
                       operator_session_scope=("default",))
    rows.append(_evaluate("no_authorization", no_auth, q2, jev_stop, {
        "cand-B", "review-spec-pass-B",
    }))

    # Render a small table.
    headers = [
        "scenario", "baseline_stop", "jev_stop",
        "recall_base", "recall_jev",
        "elig_base", "elig_jev",
        "false_suff", "prov_acc",
        "lat_base_ms", "lat_jev_ms",
    ]
    print("\t".join(headers))
    for r in rows:
        print("\t".join([
            r.scenario, r.baseline_stop, r.jev_stop,
            f"{r.mandatory_recall_baseline:.2f}",
            f"{r.mandatory_recall_jev:.2f}",
            str(r.eligible_returned_baseline),
            str(r.eligible_returned_jev),
            str(r.false_sufficiency),
            f"{r.provenance_accuracy:.2f}",
            f"{r.baseline_latency_ms:.2f}",
            f"{r.jev_latency_ms:.2f}",
        ]))


if __name__ == "__main__":
    main()
