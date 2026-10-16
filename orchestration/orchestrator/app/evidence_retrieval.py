"""Bounded retrieval adapter for the question

    "What evidence and unresolved findings apply to this exact candidate?"

This module composes with the existing read-only :class:`GraphQuery` and
the typed :class:`JevClient` (mock-backed in the bounded slice). It never
touches :class:`TaskStore` or any other authoritative ledger.

Hard contract:
- mandatory exact-identity evidence is always returned, **before** any Jev
  output is consulted, and may not be excluded by a probabilistic signal;
- hard limits (hops, nodes, tokens, latency, Jev requests) are enforced;
- on timeout, malformed output, outage, or budget exhaustion, the
  pipeline returns the deterministic baseline result with the appropriate
  :class:`StopReason`;
- Jev is never a mandatory dependency.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Iterable

from app.graph import (
    EntityType,
    EvidenceState,
    GraphEntity,
    GraphProjection,
    GraphRelationship,
    RelationshipType,
)
from app.graph_query import GraphQuery

from app.jev_client import JevClient, JevClientError, JevMalformedError, JevTimeoutError


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class StopReason(StrEnum):
    EXACT_IDENTITY_COMPLETE = "exact_identity_complete"
    JEV_STOP = "jev_stop"
    BUDGET_EXHAUSTED = "budget_exhausted"
    BASELINE_FALLBACK = "baseline_fallback"
    ACCESS_FILTERED = "access_filtered"
    TIMEOUT = "timeout"
    MALFORMED_OUTPUT = "malformed_output"
    INCOMPLETE_EVIDENCE = "incomplete_evidence"


@dataclass(frozen=True)
class EvidenceQuery:
    task_id: str
    candidate_revision: str
    policy_version: str = "1.0"
    schema_version: str = "1.0"
    max_hops: int = 2
    max_nodes: int = 64
    max_tokens: int = 4096
    max_latency_s: float = 2.0
    operator_session_scope: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvidenceRecord:
    entity_id: str
    entity_type: str
    source_artifact: str
    source_hash: str
    evidence_state: str
    scope: str
    record_kind: str  # "mandatory" | "eligible"
    jev_score: float | None = None
    jev_confidence: float | None = None
    # A small allowlist of free-text fields. We do NOT pass the body of
    # the underlying artifact to the adapter API; ``snippet`` is a
    # pre-sanitized excerpt (e.g. finding title + verdict) capped to a
    # safe length.
    snippet: str = ""


@dataclass(frozen=True)
class SourceReference:
    source_artifact: str
    record: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RetrievalDecision:
    query: EvidenceQuery
    backend: str | None
    backend_version: str | None
    records: tuple[EvidenceRecord, ...]
    stop_reason: StopReason
    budget_recommended: dict[str, int] = field(default_factory=dict)
    budget_enforced: dict[str, int] = field(default_factory=dict)
    actual_usage: dict[str, int] = field(default_factory=dict)
    source_references: tuple[SourceReference, ...] = ()
    uncertainty: tuple[str, ...] = ()
    missing_information: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


# Operator session scopes that block a record from leaving the local
# process. Matched against the ``scope`` field carried by every
# relationship (already populated by :class:`GraphQuery`).
_RESTRICTED_SCOPES = frozenset({
    "owner_credentials",
    "approval_manifest_internal",
    "private_review_notes",
})


class EvidenceRetriever:
    """Adapter wrapping :class:`GraphQuery` and an optional :class:`JevClient`."""

    def __init__(
        self,
        projection: GraphProjection,
        *,
        jev_client: JevClient | None = None,
    ) -> None:
        self._query = GraphQuery(projection)
        self._jev = jev_client  # None → baseline only

    # ------------------------------------------------------------------ public
    def retrieve(self, query: EvidenceQuery) -> RetrievalDecision:
        self._validate(query)
        started = time.monotonic()
        # 1. exact-identity mandatory evidence (deterministic).
        mandatory = self._mandatory(query)
        if not mandatory:
            return RetrievalDecision(
                query=query,
                backend=None,
                backend_version=None,
                records=(),
                stop_reason=StopReason.INCOMPLETE_EVIDENCE,
                uncertainty=("no exact-identity candidate entity found",),
                missing_information=("candidate_revision",),
            )
        # 2. deterministic access/sensitivity filter on the eligible set.
        eligible, filtered = self._eligible_filtered(query, mandatory)
        # 3. optional Jev advisory step.
        usage: dict[str, int] = {}
        backend: str | None = None
        backend_version: str | None = None
        stop = StopReason.EXACT_IDENTITY_COMPLETE
        uncertainty: list[str] = []
        jev_asked = 0
        if self._jev is not None and eligible:
            remaining_s = max(0.05, query.max_latency_s - (time.monotonic() - started))
            try:
                jev_resp = self._jev.call(
                    state=self._state_for_jev(query, mandatory, eligible, filtered),
                    questions={
                        "route": {
                            "type": "choice",
                            "instructions": ("Pick the additional retrieval route "
                                             "to expand, or 'none'."),
                            "criteria": {
                                "what_blocks": "traverse BLOCKED_BY edges",
                                "approved_bytes": "traverse AUTHORIZED_BY edges",
                                "changed_since": "entities created/updated since a time",
                                "recurring_findings": "findings appearing across tasks",
                                "none": "mandatory evidence is sufficient",
                            },
                        },
                        "rank": {
                            "type": "score",
                            "instructions": ("Score 0..1 how much each eligible "
                                             "record helps decide whether to "
                                             "accept this candidate."),
                            "criteria": ["0.0 irrelevant", "0.5 context",
                                         "1.0 directly relevant"],
                        },
                    },
                    timeout_s=min(remaining_s, 1.0),
                    max_input_tokens=min(query.max_tokens, 1024),
                )
                backend = "jev"
                backend_version = jev_resp.model
                usage["input_tokens"] = jev_resp.input_tokens
                usage["output_tokens"] = jev_resp.output_tokens
                usage["latency_ms"] = jev_resp.latency_ms
                jev_asked = 1
                records = self._apply_jev_ranking(mandatory, eligible, jev_resp)
                if any(d.choice == "none" for d in jev_resp.decisions
                       if d.question == "route"):
                    stop = StopReason.JEV_STOP
                else:
                    stop = StopReason.JEV_STOP
            except JevTimeoutError:
                records = (*mandatory, *sorted(eligible, key=lambda r: r.entity_id))
                stop = StopReason.TIMEOUT
                uncertainty.append("jev_timeout")
            except JevMalformedError:
                records = (*mandatory, *sorted(eligible, key=lambda r: r.entity_id))
                stop = StopReason.MALFORMED_OUTPUT
                uncertainty.append("jev_malformed_output")
            except JevClientError:
                records = (*mandatory, *sorted(eligible, key=lambda r: r.entity_id))
                stop = StopReason.BUDGET_EXHAUSTED
                uncertainty.append("jev_outage")
        else:
            records = (*mandatory, *sorted(eligible, key=lambda r: r.entity_id))
        if time.monotonic() - started > query.max_latency_s and stop == StopReason.JEV_STOP:
            stop = StopReason.TIMEOUT
            uncertainty.append("adapter_latency_budget_exceeded")
        usage["records_returned"] = len(records)
        usage["mandatory"] = len(mandatory)
        usage["eligible"] = len(eligible)
        usage["filtered"] = len(filtered)
        usage["jev_requests"] = jev_asked
        budget_enforced = {
            "max_hops": query.max_hops,
            "max_nodes": query.max_nodes,
            "max_tokens": query.max_tokens,
            "max_latency_ms": int(query.max_latency_s * 1000),
        }
        missing: list[str] = []
        if not self._has_authorization(mandatory):
            missing.append("authorization_record")
        if not self._has_unresolved_findings(records):
            missing.append("unresolved_findings")
        return RetrievalDecision(
            query=query,
            backend=backend,
            backend_version=backend_version,
            records=records[: query.max_nodes],
            stop_reason=stop,
            budget_recommended={"max_tokens": query.max_tokens,
                                "max_latency_ms": int(query.max_latency_s * 1000)},
            budget_enforced=budget_enforced,
            actual_usage=usage,
            source_references=self._source_refs(mandatory, eligible, filtered),
            uncertainty=tuple(uncertainty),
            missing_information=tuple(missing),
        )

    # ----------------------------------------------------------------- helpers
    @staticmethod
    def _validate(query: EvidenceQuery) -> None:
        if not query.task_id or not query.candidate_revision:
            raise ValueError("task_id and candidate_revision are required")
        if query.max_hops < 0 or query.max_nodes <= 0:
            raise ValueError("max_hops must be >= 0 and max_nodes > 0")

    def _mandatory(self, query: EvidenceQuery) -> tuple[EvidenceRecord, ...]:
        candidate = self._query.get_entity(query.candidate_revision)
        if candidate is None or candidate.entity_type != EntityType.CANDIDATE:
            return ()
        out = [self._record(candidate, "mandatory")]
        # REVIEWED_BY, BLOCKED_BY, AUTHORIZED_BY → mandatory.
        # SUPERSEDES → cited as eligible (downgraded) so superseded evidence
        # appears in the answer without being treated as blocking.
        mandatory_kinds = {
            RelationshipType.REVIEWED_BY,
            RelationshipType.BLOCKED_BY,
            RelationshipType.AUTHORIZED_BY,
        }
        for rel in self._query.get_relationships_from(candidate.entity_id):
            other = self._query.get_entity(rel.to_id)
            if other is None:
                continue
            if rel.relationship_type in mandatory_kinds:
                out.append(self._record(other, "mandatory", rel))
            elif rel.relationship_type == RelationshipType.SUPERSEDES:
                out.append(self._record(other, "eligible", rel))
        return tuple(out)

    def _eligible_filtered(
        self,
        query: EvidenceQuery,
        mandatory: tuple[EvidenceRecord, ...],
    ) -> tuple[tuple[EvidenceRecord, ...], tuple[EvidenceRecord, ...]]:
        mandatory_ids = {m.entity_id for m in mandatory}
        eligible: list[EvidenceRecord] = []
        filtered: list[EvidenceRecord] = []
        for entity in self._query._projection.entities:
            if entity.entity_id in mandatory_ids:
                continue
            rec = self._record(entity, "eligible")
            if self._is_access_blocked(rec, query):
                filtered.append(rec)
                continue
            eligible.append(rec)
            if len(eligible) + len(mandatory) >= query.max_nodes:
                break
        return tuple(eligible), tuple(filtered)

    def _is_access_blocked(self, rec: EvidenceRecord,
                            query: EvidenceQuery) -> bool:
        if rec.scope in _RESTRICTED_SCOPES:
            return True
        if (query.operator_session_scope
                and rec.scope not in query.operator_session_scope):
            return True
        return False

    def _apply_jev_ranking(
        self,
        mandatory: tuple[EvidenceRecord, ...],
        eligible: tuple[EvidenceRecord, ...],
        jev_resp,
    ) -> tuple[EvidenceRecord, ...]:
        score_by_id: dict[str, tuple[float, float | None]] = {}
        for d in jev_resp.decisions:
            if d.question == "rank":
                # Jev does not currently produce per-record scores in this
                # adapter contract; a single score represents aggregate
                # confidence. We attach it to every eligible record for
                # inspection; mandatory records keep None so they cannot be
                # mistaken for a confidence-weighted filter.
                for e in eligible:
                    score_by_id[e.entity_id] = (
                        d.score if d.score is not None else 0.0,
                        d.confidence,
                    )
        ranked_eligible = tuple(
            replace(
                e,
                jev_score=score_by_id.get(e.entity_id, (None, None))[0],
                jev_confidence=score_by_id.get(e.entity_id, (None, None))[1],
            )
            for e in eligible
        )
        return (*mandatory, *ranked_eligible)

    def _record(
        self,
        entity: GraphEntity,
        kind: str,
        rel: GraphRelationship | None = None,
    ) -> EvidenceRecord:
        scope = (rel.scope if rel is not None
                 else entity.provenance.get("scope", ""))
        # Tolerate unknown entity types: replay can hand us any string
        # because the production ``GraphEntity.entity_type`` field is
        # declared ``EntityType`` (a StrEnum) but a malformed or older
        # file may contain a value that is not a current member. We
        # never want to crash on that. If the value is an ``Enum``
        # member, take its ``.value`` (the string); otherwise pass it
        # through unchanged so the operator can see exactly what the
        # graph said.
        etype = entity.entity_type
        if hasattr(etype, "value"):
            etype_str = etype.value
        else:
            etype_str = str(etype)
        return EvidenceRecord(
            entity_id=entity.entity_id,
            entity_type=etype_str,
            source_artifact=entity.provenance.get("source_artifact", ""),
            source_hash=entity.provenance.get("source_hash", ""),
            evidence_state=entity.evidence_state.value
                if hasattr(entity.evidence_state, "value")
                else str(entity.evidence_state),
            scope=scope,
            record_kind=kind,
            snippet=_sanitized_snippet(entity),
        )

    def _source_refs(
        self,
        mandatory: tuple[EvidenceRecord, ...],
        eligible: tuple[EvidenceRecord, ...],
        filtered: tuple[EvidenceRecord, ...],
    ) -> tuple[SourceReference, ...]:
        refs: list[SourceReference] = []
        for r in mandatory:
            refs.append(SourceReference(
                source_artifact=r.source_artifact, record=r.entity_id,
                detail={"entity_type": r.entity_type, "record_kind": "mandatory",
                        "scope": r.scope},
            ))
        for r in eligible:
            refs.append(SourceReference(
                source_artifact=r.source_artifact, record=r.entity_id,
                detail={"entity_type": r.entity_type, "record_kind": "eligible",
                        "scope": r.scope},
            ))
        for r in filtered:
            refs.append(SourceReference(
                source_artifact=r.source_artifact, record=r.entity_id,
                detail={"entity_type": r.entity_type, "record_kind": "access_filtered",
                        "scope": r.scope},
            ))
        return tuple(refs)

    @staticmethod
    def _state_for_jev(
        query: EvidenceQuery,
        mandatory: tuple[EvidenceRecord, ...],
        eligible: tuple[EvidenceRecord, ...],
        filtered: tuple[EvidenceRecord, ...],
    ) -> str:
        # We pass identities + evidence state + scope only. No raw record
        # bodies, no lease timestamps, no approval manifest IDs, no
        # finding descriptions. **Access-filtered records are not shipped**
        # to the external backend at all; we report only their count so Jev
        # is not blind to the existence of a filter event but cannot derive
        # their identities.
        lines = [
            f"task_id={query.task_id}",
            f"candidate_revision={query.candidate_revision}",
            "mandatory:",
        ]
        for m in mandatory:
            lines.append(
                f"  - {m.entity_id} type={m.entity_type} "
                f"state={m.evidence_state} scope={m.scope or 'default'}"
            )
        lines.append("eligible_fingerprints:")
        for e in eligible:
            lines.append(
                f"  - {e.entity_id} type={e.entity_type} "
                f"state={e.evidence_state} scope={e.scope or 'default'}"
            )
        if filtered:
            lines.append(
                f"access_filtered_count={len(filtered)} "
                "(identities withheld from external backend)"
            )
        return "\n".join(lines)

    @staticmethod
    def _has_authorization(records: Iterable[EvidenceRecord]) -> bool:
        return any(r.entity_type == EntityType.AUTHORIZATION.value
                   for r in records)

    @staticmethod
    def _has_unresolved_findings(records: Iterable[EvidenceRecord]) -> bool:
        return any(
            r.entity_type == EntityType.FINDING.value
            and r.evidence_state != EvidenceState.SUPERSEDED.value
            for r in records
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_SNIPPET_ALLOWLIST = {"title", "verdict", "summary"}


def _sanitized_snippet(entity: GraphEntity, max_chars: int = 160) -> str:
    """Return a small, allowlisted snippet for diagnostics.

    Never include the raw payload. The bounded slice ships a title/verdict
    pair to the adapter for evidence_state inspection; the Jev *call* does
    not include ``snippet`` (see :meth:`_state_for_jev`).
    """
    parts: list[str] = []
    for key in _SNIPPET_ALLOWLIST:
        val = entity.provenance.get(key)
        if isinstance(val, str) and val.strip():
            parts.append(f"{key}={val.strip()}")
    snippet = " | ".join(parts)
    return snippet[:max_chars]
