"""Knowledge graph query interface.

Thin read-only query layer over GraphProjection. All queries return
source-evidence-backed results — never fluent summaries without provenance.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.graph import (
    EntityType,
    EvidenceState,
    GraphEntity,
    GraphProjection,
    GraphRelationship,
    RelationshipType,
)


@dataclass(frozen=True)
class QueryResult:
    """A single query result with full provenance."""

    entity: GraphEntity | None = None
    relationship: GraphRelationship | None = None
    evidence: dict[str, Any] | None = None


class GraphQuery:
    """Read-only query engine over a GraphProjection.

    Usage:
        gq = GraphQuery(projection)
        blockers = gq.what_blocks(candidate_id)
        auths = gq.approved_bytes(candidate_id)
    """

    def __init__(self, projection: GraphProjection) -> None:
        self._projection = projection
        self._entity_index: dict[str, GraphEntity] = {
            e.entity_id: e for e in projection.entities
        }
        self._rel_by_from: dict[str, list[GraphRelationship]] = {}
        self._rel_by_to: dict[str, list[GraphRelationship]] = {}
        for rel in projection.relationships:
            self._rel_by_from.setdefault(rel.from_id, []).append(rel)
            self._rel_by_to.setdefault(rel.to_id, []).append(rel)

    # ------------------------------------------------------------------
    # Thin-slice queries
    # ------------------------------------------------------------------

    def what_blocks(self, entity_id: str) -> list[QueryResult]:
        """Return FINDING entities that block the given entity.

        Traverses blocked_by relationships from the given entity to
        finding entities. Returns empty list if nothing blocks.
        """
        results: list[QueryResult] = []
        for rel in self._rel_by_from.get(entity_id, []):
            if rel.relationship_type == RelationshipType.BLOCKED_BY:
                finding = self._entity_index.get(rel.to_id)
                if finding:
                    results.append(
                        QueryResult(
                            entity=finding,
                            relationship=rel,
                            evidence={
                                "source_artifact": finding.provenance.get(
                                    "source_artifact", ""
                                ),
                                "source_hash": finding.provenance.get("source_hash", ""),
                                "evidence_state": finding.evidence_state.value,
                                "scope": rel.scope,
                            },
                        )
                    )
        return results

    def approved_bytes(self, entity_id: str) -> list[QueryResult]:
        """Return AUTHORIZATION entities bound to the given entity.

        Traverses authorized_by relationships. Returns the exact artifact
        hashes and authorization scopes — not just a boolean.
        """
        results: list[QueryResult] = []
        for rel in self._rel_by_from.get(entity_id, []):
            if rel.relationship_type == RelationshipType.AUTHORIZED_BY:
                auth = self._entity_index.get(rel.to_id)
                if auth:
                    results.append(
                        QueryResult(
                            entity=auth,
                            relationship=rel,
                            evidence={
                                "source_artifact": auth.provenance.get(
                                    "source_artifact", ""
                                ),
                                "approved_by": auth.provenance.get("approved_by", ""),
                                "approval_manifest_id": auth.provenance.get(
                                    "approval_manifest_id", ""
                                ),
                                "evidence_state": auth.evidence_state.value,
                                "scope": rel.scope,
                            },
                        )
                    )
        return results

    def changed_since(
        self, entity_type: EntityType, since: str
    ) -> list[QueryResult]:
        """Return entities of the given type created or updated since a timestamp.

        Useful for "what changed since the last verified environment?"
        """
        results: list[QueryResult] = []
        for entity in self._projection.entities:
            if entity.entity_type == entity_type and entity.created_at >= since:
                results.append(
                    QueryResult(
                        entity=entity,
                        evidence={
                            "source_artifact": entity.provenance.get(
                                "source_artifact", ""
                            ),
                            "created_at": entity.created_at,
                            "evidence_state": entity.evidence_state.value,
                        },
                    )
                )
        return results

    def recurring_findings(
        self, task_class: str, window: int = 10
    ) -> list[QueryResult]:
        """Return findings that appear across multiple tasks in a class.

        Groups findings by canonical_key and returns those appearing ≥2 times.
        Useful for Loop D (improvement) pattern detection.
        """
        # Group findings by reviewer+verdict pattern
        groups: dict[str, list[GraphEntity]] = {}
        for entity in self._projection.entities:
            if entity.entity_type == EntityType.FINDING:
                key = entity.canonical_key.split(":")[0]  # task_class prefix
                if key == task_class:
                    groups.setdefault(key, []).append(entity)

        results: list[QueryResult] = []
        for key, entities in groups.items():
            if len(entities) >= 2:
                for entity in entities[:window]:
                    results.append(
                        QueryResult(
                            entity=entity,
                            evidence={
                                "source_artifact": entity.provenance.get(
                                    "source_artifact", ""
                                ),
                                "recurrence_count": len(entities),
                                "evidence_state": entity.evidence_state.value,
                            },
                        )
                    )
        return results

    def evidence_for(self, relationship_id: str) -> QueryResult | None:
        """Return the full relationship record with provenance for a given ID."""
        for rel in self._projection.relationships:
            if rel.relationship_id == relationship_id:
                return QueryResult(
                    relationship=rel,
                    evidence={
                        "source_artifact": rel.provenance.get("source_artifact", ""),
                        "evidence_ref": rel.evidence_ref,
                        "evidence_state": rel.evidence_state.value,
                        "scope": rel.scope,
                        "from_id": rel.from_id,
                        "to_id": rel.to_id,
                    },
                )
        return None

    # ------------------------------------------------------------------
    # Convenience: entity lookup
    # ------------------------------------------------------------------

    def get_entity(self, entity_id: str) -> GraphEntity | None:
        return self._entity_index.get(entity_id)

    def get_relationships_from(
        self, entity_id: str, rel_type: RelationshipType | None = None
    ) -> list[GraphRelationship]:
        rels = self._rel_by_from.get(entity_id, [])
        if rel_type:
            return [r for r in rels if r.relationship_type == rel_type]
        return rels

    def get_relationships_to(
        self, entity_id: str, rel_type: RelationshipType | None = None
    ) -> list[GraphRelationship]:
        rels = self._rel_by_to.get(entity_id, [])
        if rel_type:
            return [r for r in rels if r.relationship_type == rel_type]
        return rels
