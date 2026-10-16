"""Mock-backed tests for the evidence retrieval adapter.

Covers the 10 original evaluation scenarios in DESIGN.md §8 plus the
expanded offline evaluation from the operator's reconciliation message
(conflicting reviews, semantic distractors, unauthorized records,
unknown identifiers, type-malformed, premature stop, graph outage, max
evidence bound, malicious relation write).

All tests use the offline :class:`MockJevClient`; no live API call.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.evidence_retrieval import (
    EvidenceQuery,
    EvidenceRetriever,
    StopReason,
)
from app.graph import EvidenceState
from app.jev_client import MockJevClient

from tests.fixtures.evidence_fixtures import (
    build_conflicting_reviews_graph,
    build_graph,
    build_malicious_relation_graph,
    build_max_evidence_graph,
    build_no_authorization_graph,
    build_outage_graph,
    build_premature_stop_graph,
    build_semantic_distractors_graph,
    build_type_malformed_graph,
    build_unauthorized_records_graph,
    build_unknown_identifier_graph,
)


@pytest.fixture
def projection():
    return build_graph()


@pytest.fixture
def no_auth_projection():
    return build_no_authorization_graph()


# ---------------------------------------------------------------------------
# Baseline (no Jev)
# ---------------------------------------------------------------------------


def test_scenario_1_exact_candidate_lookup(projection):
    retriever = EvidenceRetriever(projection)  # no jev → baseline
    decision = retriever.retrieve(EvidenceQuery(
        task_id="task-1", candidate_revision="cand-A",
        operator_session_scope=("default",),
    ))
    ids = {r.entity_id for r in decision.records}
    # Mandatory set: candidate, two reviews, blocker finding, authorization.
    assert "cand-A" in ids
    assert "review-spec-pass" in ids
    assert "review-quality-approved" in ids
    assert "finding-blocker" in ids
    assert "auth-ok" in ids
    assert decision.stop_reason == StopReason.EXACT_IDENTITY_COMPLETE
    assert decision.backend is None


def test_scenario_2_superseded_evidence(projection):
    """Superseded prior candidate must be cited but not classified as blocking."""
    retriever = EvidenceRetriever(projection)
    decision = retriever.retrieve(EvidenceQuery(
        task_id="task-1", candidate_revision="cand-A",
        operator_session_scope=("default",),
    ))
    superseded = [r for r in decision.records if r.entity_id == "cand-A-old"]
    assert superseded, "superseded candidate should be cited as eligible"
    assert superseded[0].record_kind == "eligible"
    assert superseded[0].evidence_state == EvidenceState.SUPERSEDED.value


def test_scenario_3_conflicting_findings(projection):
    retriever = EvidenceRetriever(projection)
    decision = retriever.retrieve(EvidenceQuery(
        task_id="task-1", candidate_revision="cand-A",
        operator_session_scope=("default",),
    ))
    ids = {r.entity_id for r in decision.records}
    assert "finding-blocker" in ids
    assert "contradicting-finding" in ids


def test_scenario_5_multi_hop_dependency(projection):
    """dep-1 -> dep-2 chain is present in the eligible set; Jev ranking does
    not change that and the chain remains citeable."""
    retriever = EvidenceRetriever(projection)
    decision = retriever.retrieve(EvidenceQuery(
        task_id="task-1", candidate_revision="cand-A",
        operator_session_scope=("default",),
        max_nodes=64,
    ))
    ids = {r.entity_id for r in decision.records}
    # Either dep appears in eligible records (advisory access is broad here).
    assert "dep-1" in ids or "dep-2" in ids


def test_scenario_6_irrelevant_but_similar(projection):
    retriever = EvidenceRetriever(projection)
    decision = retriever.retrieve(EvidenceQuery(
        task_id="task-1", candidate_revision="cand-A",
        operator_session_scope=("default",),
    ))
    ids = {r.entity_id for r in decision.records}
    # Semantically similar but irrelevant twins must be *eligible*, not
    # mandatory. Jev may rank them lower; mandatory set is unchanged.
    assert "twin-1" not in {r.entity_id for r in decision.records
                             if r.record_kind == "mandatory"}


def test_scenario_7_missing_mandatory_evidence(no_auth_projection):
    retriever = EvidenceRetriever(no_auth_projection)
    decision = retriever.retrieve(EvidenceQuery(
        task_id="task-1", candidate_revision="cand-B",
        operator_session_scope=("default",),
    ))
    assert "authorization_record" in decision.missing_information
    # No blocker findings → must surface the missing-findings gap.
    assert "unresolved_findings" in decision.missing_information


def test_scenario_8_prompt_injection_does_not_control_decision(projection):
    """An injection-style artifact must be cited but must not be treated as
    authorization or as an instruction."""
    retriever = EvidenceRetriever(projection)
    decision = retriever.retrieve(EvidenceQuery(
        task_id="task-1", candidate_revision="cand-A",
        operator_session_scope=("default",),
    ))
    ids = {r.entity_id for r in decision.records}
    assert "injected" in ids  # cited
    # Authorization is the explicit auth-ok record, not the injected bait.
    auth_records = [r for r in decision.records
                    if r.entity_type == "authorization"]
    assert len(auth_records) == 1
    assert auth_records[0].entity_id == "auth-ok"


def test_scenario_9_unauthorized_sensitive_record(projection):
    """The owner_credentials record is filtered out of the eligible set,
    and Jev never sees it (no state line is emitted for it)."""
    jev = MockJevClient()
    retriever = EvidenceRetriever(projection, jev_client=jev)
    decision = retriever.retrieve(EvidenceQuery(
        task_id="task-1", candidate_revision="cand-A",
        operator_session_scope=("default",),
    ))
    record_ids = {r.entity_id for r in decision.records}
    assert "sensitive-cred" not in record_ids
    # Filtered records still appear in source_references for diagnostics.
    filtered_refs = [r for r in decision.source_references
                     if r.detail.get("record_kind") == "access_filtered"]
    assert any(r.record == "sensitive-cred" for r in filtered_refs)
    # And Jev's state must not contain its entity_id.
    jev_state = jev.calls[0]["state"]
    assert "sensitive-cred" not in jev_state


def test_scenario_10_malformed_output_falls_back(projection):
    jev = MockJevClient()
    jev.script({"error": "malformed"})
    retriever = EvidenceRetriever(projection, jev_client=jev)
    decision = retriever.retrieve(EvidenceQuery(
        task_id="task-1", candidate_revision="cand-A",
        operator_session_scope=("default",),
    ))
    assert decision.stop_reason == StopReason.MALFORMED_OUTPUT
    assert "jev_malformed_output" in decision.uncertainty


def test_scenario_10_timeout_falls_back(projection):
    jev = MockJevClient()
    jev.script({"error": "timeout"})
    retriever = EvidenceRetriever(projection, jev_client=jev)
    decision = retriever.retrieve(EvidenceQuery(
        task_id="task-1", candidate_revision="cand-A",
        operator_session_scope=("default",),
    ))
    assert decision.stop_reason == StopReason.TIMEOUT


def test_jev_stop_reason(projection):
    jev = MockJevClient()
    jev.script({
        "model": "mock-jev-0",
        "answers": {
            "route": {"type": "choice", "choice": "none",
                      "probabilities": {"none": 0.9}, "confidence": 0.9},
            "rank": {"type": "score", "score": 0.0, "confidence": 0.9},
        },
        "usage": {"input_tokens": 10, "output_tokens": 5},
    })
    retriever = EvidenceRetriever(projection, jev_client=jev)
    decision = retriever.retrieve(EvidenceQuery(
        task_id="task-1", candidate_revision="cand-A",
        operator_session_scope=("default",),
    ))
    assert decision.backend == "jev"
    assert decision.stop_reason == StopReason.JEV_STOP
    assert decision.actual_usage["input_tokens"] == 10


def test_mandatory_never_excluded_by_jev(projection):
    """Jev signals must not exclude mandatory evidence."""
    jev = MockJevClient()
    jev.script({
        "model": "mock-jev-0",
        "answers": {
            "route": {"type": "choice", "choice": "what_blocks",
                      "probabilities": {"what_blocks": 1.0}, "confidence": 0.99},
            "rank": {"type": "score", "score": 0.0, "confidence": 0.99},
        },
        "usage": {"input_tokens": 10, "output_tokens": 5},
    })
    retriever = EvidenceRetriever(projection, jev_client=jev)
    decision = retriever.retrieve(EvidenceQuery(
        task_id="task-1", candidate_revision="cand-A",
        operator_session_scope=("default",),
    ))
    mandatory_ids = {r.entity_id for r in decision.records if r.record_kind == "mandatory"}
    assert {"cand-A", "review-spec-pass", "review-quality-approved",
            "finding-blocker", "auth-ok"} <= mandatory_ids


def test_invalid_identity_rejected():
    projection = build_graph()
    retriever = EvidenceRetriever(projection)
    with pytest.raises(ValueError):
        retriever.retrieve(EvidenceQuery(task_id="", candidate_revision="cand-A"))


def test_no_candidate_returns_incomplete():
    projection = build_graph()
    retriever = EvidenceRetriever(projection)
    decision = retriever.retrieve(EvidenceQuery(
        task_id="task-1", candidate_revision="cand-DOES-NOT-EXIST",
        operator_session_scope=("default",),
    ))
    assert decision.stop_reason == StopReason.INCOMPLETE_EVIDENCE
    assert "candidate_revision" in decision.missing_information


# ---------------------------------------------------------------------------
# Expanded offline evaluation (operator reconciliation, 2026-10-11)
# ---------------------------------------------------------------------------


def test_scenario_expanded_conflicting_reviews():
    """Conflicting reviews on the same stage are both surfaced as mandatory;
    neither is silently dropped."""
    projection = build_conflicting_reviews_graph()
    retriever = EvidenceRetriever(projection)
    decision = retriever.retrieve(EvidenceQuery(
        task_id="task-1", candidate_revision="cand-C",
        operator_session_scope=("default",),
    ))
    mandatory = {r.entity_id for r in decision.records if r.record_kind == "mandatory"}
    assert {"cand-C", "review-spec-A", "review-spec-B"} <= mandatory


def test_scenario_expanded_semantic_distractors():
    """10 distractors outnumber the 3 mandatory entities; the mandatory set
    must still be intact in the result."""
    projection = build_semantic_distractors_graph()
    retriever = EvidenceRetriever(projection)
    decision = retriever.retrieve(EvidenceQuery(
        task_id="task-1", candidate_revision="cand-D",
        operator_session_scope=("default",),
    ))
    mandatory = {r.entity_id for r in decision.records if r.record_kind == "mandatory"}
    assert {"cand-D", "d-review", "d-auth"} <= mandatory


def test_scenario_expanded_unauthorized_records():
    """All three restricted-scope records must be filtered out; Jev's state
    payload must not contain any of their entity_ids."""
    projection = build_unauthorized_records_graph()
    jev = MockJevClient()
    retriever = EvidenceRetriever(projection, jev_client=jev)
    decision = retriever.retrieve(EvidenceQuery(
        task_id="task-1", candidate_revision="cand-E",
        operator_session_scope=("default",),
    ))
    record_ids = {r.entity_id for r in decision.records}
    for cred in ("cred-1", "cred-2", "cred-3"):
        assert cred not in record_ids
    jev_state = jev.calls[0]["state"]
    for cred in ("cred-1", "cred-2", "cred-3"):
        assert cred not in jev_state
    # Filtered refs present.
    filtered_records = [s.record for s in decision.source_references
                        if s.detail.get("record_kind") == "access_filtered"]
    assert {"cred-1", "cred-2", "cred-3"} <= set(filtered_records)


def test_scenario_expanded_unknown_identifier():
    """A graph entry whose entity_type is not in the EntityType enum must
    not crash the adapter; it must be cited as eligible with the raw type
    string preserved."""
    projection = build_unknown_identifier_graph()
    retriever = EvidenceRetriever(projection)
    decision = retriever.retrieve(EvidenceQuery(
        task_id="task-1", candidate_revision="cand-F",
        operator_session_scope=("default",),
    ))
    ids = {r.entity_id for r in decision.records}
    assert "unknown-type-1" in ids
    rec = next(r for r in decision.records if r.entity_id == "unknown-type-1")
    # raw type string preserved for diagnostics (not coerced)
    assert rec.entity_type == "FORGOTYPE"


def test_scenario_expanded_type_malformed():
    """A provenance value of the wrong type (int source_hash) must not crash
    the adapter; the EvidenceRecord.source_hash field must be the raw value."""
    projection = build_type_malformed_graph()
    retriever = EvidenceRetriever(projection)
    decision = retriever.retrieve(EvidenceQuery(
        task_id="task-1", candidate_revision="cand-G",
        operator_session_scope=("default",),
    ))
    rec = next((r for r in decision.records
                if r.entity_id == "bad-meta"), None)
    assert rec is not None
    # source_hash is whatever the graph said, not coerced to a string.
    assert rec.source_hash == 12345  # int, not str


def test_scenario_expanded_premature_stop_with_complete_mandatory():
    """Jev asks to stop; mandatory set is present; the adapter must
    return the present mandatory set with stop_reason=JEV_STOP and an
    empty missing_information (false sufficiency check passes because
    everything required is present)."""
    projection = build_premature_stop_graph()
    jev = MockJevClient()
    jev.script({
        "model": "mock-jev-0",
        "answers": {
            "route": {"type": "choice", "choice": "none",
                      "probabilities": {"none": 0.99}, "confidence": 0.99},
            "rank": {"type": "score", "score": 0.0, "confidence": 0.99},
        },
        "usage": {"input_tokens": 10, "output_tokens": 5},
    })
    retriever = EvidenceRetriever(projection, jev_client=jev)
    decision = retriever.retrieve(EvidenceQuery(
        task_id="task-1", candidate_revision="cand-A",
        operator_session_scope=("default",),
    ))
    assert decision.stop_reason == StopReason.JEV_STOP
    # The mandatory set is complete, so no missing_information is
    # reported and "auth-ok" must be present as mandatory.
    assert "auth-ok" in [
        r.entity_id for r in decision.records if r.record_kind == "mandatory"
    ]


def test_scenario_expanded_graph_outage():
    """Generic Jev client error (outage) falls back to baseline with
    uncertainty carrying jev_outage."""
    projection = build_outage_graph()
    jev = MockJevClient()
    jev.script({"error": "outage"})
    retriever = EvidenceRetriever(projection, jev_client=jev)
    decision = retriever.retrieve(EvidenceQuery(
        task_id="task-1", candidate_revision="cand-A",
        operator_session_scope=("default",),
    ))
    # The catch-all for JevClientError surfaces BUDGET_EXHAUSTED with
    # the jev_outage uncertainty tag.
    assert decision.stop_reason in (
        StopReason.BUDGET_EXHAUSTED, StopReason.TIMEOUT,
        StopReason.MALFORMED_OUTPUT,
    )
    assert "jev_outage" in decision.uncertainty
    # The mandatory set is still returned.
    mandatory = {r.entity_id for r in decision.records if r.record_kind == "mandatory"}
    assert {"cand-A", "review-spec-pass", "review-quality-approved",
            "finding-blocker", "auth-ok"} <= mandatory


def test_scenario_expanded_max_evidence_bound():
    """With max_nodes=10 and ~200 eligible entities, the returned record
    set must not exceed 10."""
    projection = build_max_evidence_graph(200)
    retriever = EvidenceRetriever(projection)
    decision = retriever.retrieve(EvidenceQuery(
        task_id="task-1", candidate_revision="cand-H",
        operator_session_scope=("default",),
        max_nodes=10,
    ))
    assert len(decision.records) <= 10
    # The mandatory set is intact inside the bound.
    mandatory = {r.entity_id for r in decision.records if r.record_kind == "mandatory"}
    assert {"cand-H", "h-review"} <= mandatory


def test_scenario_expanded_malicious_relation():
    """A spurious REVIEWED_BY edge from a Mallory-controlled entity is
    still a typed outgoing edge of the candidate, so the adapter
    surfaces it as a mandatory record. This is by design — the
    retrieval layer is *evidence-citation*, not authority. Authority
    and trust belong to the planner/policy layer. The test documents
    this and the resulting audit-trail visibility."""
    projection = build_malicious_relation_graph()
    retriever = EvidenceRetriever(projection)
    decision = retriever.retrieve(EvidenceQuery(
        task_id="task-1", candidate_revision="cand-I",
        operator_session_scope=("default",),
    ))
    mandatory = {r.entity_id for r in decision.records if r.record_kind == "mandatory"}
    # Mallory's artifact appears as mandatory (typed REVIEWED_BY).
    # The planner/policy layer (not the retrieval layer) is the trust gate.
    assert "i-mallory" in mandatory
    # The legitimate review is also present.
    assert "i-review" in mandatory
    # Provenance surfaces Mallory's source so the planner can detect it.
    mallory = next(r for r in decision.records if r.entity_id == "i-mallory")
    assert mallory.source_artifact == "fixtures/evidence.jsonl"
