"""Provenance-aware knowledge graph projection.

The graph is a DERIVED read-only projection from authoritative durable records
(TaskStore + DelegationStore). It is never authoritative: it cannot grant
approval, reserve budget, mark tasks complete, or override quarantine.

Determinism: identical inputs produce byte-identical graph JSONL output.
Rebuild: ``GraphProjection.from_stores()`` re-reads authoritative stores and
regenerates the entire graph. No incremental state is retained.

Storage: JSONL, one entity or relationship per line, same convention as
TaskStore and DelegationStore. No new service required.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class EntityType(StrEnum):
    REPOSITORY = "repository"
    CANDIDATE = "candidate"
    ARTIFACT = "artifact"
    TASK = "task"
    ATTEMPT = "attempt"
    AGENT_ROLE = "agent_role"
    REQUIREMENT = "requirement"
    TEST_RUN = "test_run"
    FINDING = "finding"
    REVIEW = "review"
    AUTHORIZATION = "authorization"
    ENVIRONMENT = "environment"
    DEPLOYMENT = "deployment"


class RelationshipType(StrEnum):
    DEPENDS_ON = "depends_on"
    IMPLEMENTS = "implements"
    TESTED_BY = "tested_by"
    PRODUCES = "produces"
    DERIVED_FROM = "derived_from"
    REVIEWED_BY = "reviewed_by"
    AUTHORIZED_BY = "authorized_by"
    SUPERSEDES = "supersedes"
    CONTRADICTS = "contradicts"
    BLOCKED_BY = "blocked_by"
    DEPLOYED_AS = "deployed_as"


class EvidenceState(StrEnum):
    PROPOSED = "proposed"
    OBSERVED = "observed"
    VERIFIED = "verified"
    DISPUTED = "disputed"
    SUPERSEDED = "superseded"


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


def _det_hash(*parts: str) -> str:
    """Deterministic content hash for entity/relationship IDs."""
    canonical = "|".join(parts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class GraphEntity:
    entity_id: str
    entity_type: EntityType
    canonical_key: str
    created_at: str
    superseded_by: str | None = None
    evidence_state: EvidenceState = EvidenceState.OBSERVED
    provenance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "entity_type": self.entity_type.value,
            "canonical_key": self.canonical_key,
            "created_at": self.created_at,
            "superseded_by": self.superseded_by,
            "evidence_state": self.evidence_state.value,
            "provenance": self.provenance,
        }


@dataclass(frozen=True)
class GraphRelationship:
    relationship_id: str
    relationship_type: RelationshipType
    from_id: str
    to_id: str
    scope: str
    evidence_ref: str
    created_at: str
    superseded_by: str | None = None
    evidence_state: EvidenceState = EvidenceState.OBSERVED
    provenance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "relationship_id": self.relationship_id,
            "relationship_type": self.relationship_type.value,
            "from_id": self.from_id,
            "to_id": self.to_id,
            "scope": self.scope,
            "evidence_ref": self.evidence_ref,
            "created_at": self.created_at,
            "superseded_by": self.superseded_by,
            "evidence_state": self.evidence_state.value,
            "provenance": self.provenance,
        }


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


@dataclass
class GraphProjection:
    """Deterministic knowledge graph built from authoritative stores.

    Usage:
        projection = GraphProjection.from_stores(task_store, delegation_store)
        projection.write_to(Path("workspace/orchestrator/graph.jsonl"))
    """

    entities: list[GraphEntity] = field(default_factory=list)
    relationships: list[GraphRelationship] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Constructors
    # ------------------------------------------------------------------

    @classmethod
    def from_stores(
        cls,
        task_store: Any,  # TaskStore (duck-typed to avoid import cycle)
        delegation_store: Any | None = None,  # DelegationStore | None
        *,
        now: str | None = None,
    ) -> "GraphProjection":
        """Build a deterministic graph projection from authoritative stores.

        Reads all tasks from TaskStore and all delegated tasks from
        DelegationStore (if provided), then projects entities and
        relationships. The output is a pure function of the input records:
        identical store contents always produce identical output.

        Pass ``now`` to pin the extraction timestamp for reproducible output.
        When ``now`` is None, the current UTC time is used.
        """
        projection = cls()
        if now is None:
            now = datetime.utcnow().isoformat() + "Z"

        # --- Task entities ---
        for task in task_store.all_tasks().values():
            task_id_str = str(task.task_id)
            task_revision = task.compute_contract_revision()
            task_entity = GraphEntity(
                entity_id=_det_hash("task", task_id_str),
                entity_type=EntityType.TASK,
                canonical_key=task_id_str,
                created_at=task.created_at.isoformat() if task.created_at else now,
                evidence_state=EvidenceState.VERIFIED,
                provenance={
                    "source_artifact": Path(task_store.ledger_path).name,
                    "source_hash": task_revision,
                    "extraction_method": "GraphProjection.from_stores",
                    "extracted_by": "noesis-orchestrator",
                    "extracted_at": now,
                    "environment": "control-plane",
                    "sensitivity": "internal",
                },
            )
            projection.entities.append(task_entity)

            # --- Agent role entity (dedup by canonical key) ---
            agent_key = task.assignee_profile
            agent_id = _det_hash("agent_role", agent_key)
            if not any(e.entity_id == agent_id for e in projection.entities):
                projection.entities.append(
                    GraphEntity(
                        entity_id=agent_id,
                        entity_type=EntityType.AGENT_ROLE,
                        canonical_key=agent_key,
                        created_at=now,
                        evidence_state=EvidenceState.OBSERVED,
                        provenance={
                            "source_artifact": Path(task_store.ledger_path).name,
                            "extraction_method": "GraphProjection.from_stores",
                            "extracted_by": "noesis-orchestrator",
                            "extracted_at": now,
                            "environment": "control-plane",
                            "sensitivity": "internal",
                        },
                    )
                )

            # --- Relationship: task depends_on agent_role (assignment) ---
            projection.relationships.append(
                GraphRelationship(
                    relationship_id=_det_hash(
                        "depends_on", task_entity.entity_id, agent_id, "assignment"
                    ),
                    relationship_type=RelationshipType.DEPENDS_ON,
                    from_id=task_entity.entity_id,
                    to_id=agent_id,
                    scope="assignment",
                    evidence_ref=task_revision,
                    created_at=now,
                    evidence_state=EvidenceState.VERIFIED,
                    provenance={
                        "source_artifact": Path(task_store.ledger_path).name,
                        "extraction_method": "GraphProjection.from_stores",
                        "extracted_by": "noesis-orchestrator",
                        "extracted_at": now,
                        "environment": "control-plane",
                        "sensitivity": "internal",
                    },
                )
            )

            # --- Review entities (from task.delegation.reviews) ---
            for review in task.delegation.reviews:
                review_key = f"{task_id_str}:{review.reviewer}:{review.candidate_revision}"
                review_id = _det_hash("review", review_key)
                projection.entities.append(
                    GraphEntity(
                        entity_id=review_id,
                        entity_type=EntityType.REVIEW,
                        canonical_key=review_key,
                        created_at=now,
                        evidence_state=EvidenceState.VERIFIED,
                        provenance={
                            "source_artifact": Path(task_store.ledger_path).name,
                            "extraction_method": "GraphProjection.from_stores",
                            "extracted_by": "noesis-orchestrator",
                            "extracted_at": now,
                            "environment": "control-plane",
                            "sensitivity": "internal",
                            "reviewer": review.reviewer,
                            "verdict": review.verdict,
                            "candidate_revision": review.candidate_revision,
                        },
                    )
                )
                projection.relationships.append(
                    GraphRelationship(
                        relationship_id=_det_hash(
                            "reviewed_by", task_entity.entity_id, review_id, "review"
                        ),
                        relationship_type=RelationshipType.REVIEWED_BY,
                        from_id=task_entity.entity_id,
                        to_id=review_id,
                        scope="review",
                        evidence_ref=review.candidate_revision,
                        created_at=now,
                        evidence_state=EvidenceState.VERIFIED,
                        provenance={
                            "source_artifact": Path(task_store.ledger_path).name,
                            "extraction_method": "GraphProjection.from_stores",
                            "extracted_by": "noesis-orchestrator",
                            "extracted_at": now,
                            "environment": "control-plane",
                            "sensitivity": "internal",
                        },
                    )
                )

            # --- Authorization entity ---
            if task.approval and task.approval.state == "granted":
                auth_key = f"{task_id_str}:{task.approval.approved_by}"
                auth_id = _det_hash("authorization", auth_key)
                projection.entities.append(
                    GraphEntity(
                        entity_id=auth_id,
                        entity_type=EntityType.AUTHORIZATION,
                        canonical_key=auth_key,
                        created_at=now,
                        evidence_state=EvidenceState.VERIFIED,
                        provenance={
                            "source_artifact": Path(task_store.ledger_path).name,
                            "extraction_method": "GraphProjection.from_stores",
                            "extracted_by": "noesis-orchestrator",
                            "extracted_at": now,
                            "environment": "control-plane",
                            "sensitivity": "internal",
                            "approved_by": task.approval.approved_by,
                            "approval_manifest_id": task.approval.approval_manifest_id,
                        },
                    )
                )
                projection.relationships.append(
                    GraphRelationship(
                        relationship_id=_det_hash(
                            "authorized_by", task_entity.entity_id, auth_id, "approval"
                        ),
                        relationship_type=RelationshipType.AUTHORIZED_BY,
                        from_id=task_entity.entity_id,
                        to_id=auth_id,
                        scope="approval",
                        evidence_ref=task_revision,
                        created_at=now,
                        evidence_state=EvidenceState.VERIFIED,
                        provenance={
                            "source_artifact": Path(task_store.ledger_path).name,
                            "extraction_method": "GraphProjection.from_stores",
                            "extracted_by": "noesis-orchestrator",
                            "extracted_at": now,
                            "environment": "control-plane",
                            "sensitivity": "internal",
                        },
                    )
                )

            # --- Dependency relationships ---
            for dep_id in task.depends_on:
                dep_entity_id = _det_hash("task", str(dep_id))
                projection.relationships.append(
                    GraphRelationship(
                        relationship_id=_det_hash(
                            "depends_on", task_entity.entity_id, dep_entity_id, "task"
                        ),
                        relationship_type=RelationshipType.DEPENDS_ON,
                        from_id=task_entity.entity_id,
                        to_id=dep_entity_id,
                        scope="task",
                        evidence_ref=task_revision,
                        created_at=now,
                        evidence_state=EvidenceState.VERIFIED,
                        provenance={
                            "source_artifact": Path(task_store.ledger_path).name,
                            "extraction_method": "GraphProjection.from_stores",
                            "extracted_by": "noesis-orchestrator",
                            "extracted_at": now,
                            "environment": "control-plane",
                            "sensitivity": "internal",
                        },
                    )
                )

        # --- Delegated task entities ---
        if delegation_store:
            for dt in delegation_store._all_records():
                dt_entity = GraphEntity(
                    entity_id=_det_hash("task", dt.task_id),
                    entity_type=EntityType.TASK,
                    canonical_key=dt.task_id,
                    created_at=dt.created_at,
                    evidence_state=EvidenceState.VERIFIED,
                    provenance={
                        "source_artifact": Path(delegation_store.ledger_path).name,
                        "source_hash": dt.contract_revision,
                        "extraction_method": "GraphProjection.from_stores",
                        "extracted_by": "noesis-orchestrator",
                        "extracted_at": now,
                        "environment": "control-plane",
                        "sensitivity": "internal",
                    },
                )
                if not any(e.entity_id == dt_entity.entity_id for e in projection.entities):
                    projection.entities.append(dt_entity)

                # --- Attempt entity ---
                attempt_key = f"{dt.task_id}:{dt.assignment_epoch}"
                attempt_id = _det_hash("attempt", attempt_key)
                projection.entities.append(
                    GraphEntity(
                        entity_id=attempt_id,
                        entity_type=EntityType.ATTEMPT,
                        canonical_key=attempt_key,
                        created_at=dt.created_at,
                        evidence_state=EvidenceState.OBSERVED,
                        provenance={
                            "source_artifact": Path(delegation_store.ledger_path).name,
                            "extraction_method": "GraphProjection.from_stores",
                            "extracted_by": "noesis-orchestrator",
                            "extracted_at": now,
                            "environment": "control-plane",
                            "sensitivity": "internal",
                            "assignment_epoch": dt.assignment_epoch,
                            "state": (
                                dt.state.value if hasattr(dt.state, "value") else str(dt.state)
                            ),
                        },
                    )
                )
                projection.relationships.append(
                    GraphRelationship(
                        relationship_id=_det_hash(
                            "produces", attempt_id, dt_entity.entity_id, "attempt"
                        ),
                        relationship_type=RelationshipType.PRODUCES,
                        from_id=attempt_id,
                        to_id=dt_entity.entity_id,
                        scope="attempt",
                        evidence_ref=dt.contract_revision,
                        created_at=now,
                        evidence_state=EvidenceState.OBSERVED,
                        provenance={
                            "source_artifact": Path(delegation_store.ledger_path).name,
                            "extraction_method": "GraphProjection.from_stores",
                            "extracted_by": "noesis-orchestrator",
                            "extracted_at": now,
                            "environment": "control-plane",
                            "sensitivity": "internal",
                        },
                    )
                )

                # --- Review entities from delegated task ---
                for review in dt.reviews:
                    review_key = f"{dt.task_id}:{review.reviewer}:{review.candidate_revision}"
                    review_id = _det_hash("review", review_key)
                    if not any(e.entity_id == review_id for e in projection.entities):
                        projection.entities.append(
                            GraphEntity(
                                entity_id=review_id,
                                entity_type=EntityType.REVIEW,
                                canonical_key=review_key,
                                created_at=now,
                                evidence_state=EvidenceState.VERIFIED,
                                provenance={
                                    "source_artifact": Path(delegation_store.ledger_path).name,
                                    "extraction_method": "GraphProjection.from_stores",
                                    "extracted_by": "noesis-orchestrator",
                                    "extracted_at": now,
                                    "environment": "control-plane",
                                    "sensitivity": "internal",
                                    "reviewer": review.reviewer,
                                    "verdict": review.verdict,
                                    "candidate_revision": review.candidate_revision,
                                },
                            )
                        )
                    projection.relationships.append(
                        GraphRelationship(
                            relationship_id=_det_hash(
                                "reviewed_by", dt_entity.entity_id, review_id, "review"
                            ),
                            relationship_type=RelationshipType.REVIEWED_BY,
                            from_id=dt_entity.entity_id,
                            to_id=review_id,
                            scope="review",
                            evidence_ref=review.candidate_revision,
                            created_at=now,
                            evidence_state=EvidenceState.VERIFIED,
                            provenance={
                                "source_artifact": Path(delegation_store.ledger_path).name,
                                "extraction_method": "GraphProjection.from_stores",
                                "extracted_by": "noesis-orchestrator",
                                "extracted_at": now,
                                "environment": "control-plane",
                                "sensitivity": "internal",
                            },
                        )
                    )

        # Sort for determinism
        projection.entities.sort(key=lambda e: e.entity_id)
        projection.relationships.sort(key=lambda r: r.relationship_id)
        return projection

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    def to_jsonl(self) -> str:
        """Serialize all entities and relationships as deterministic JSONL."""
        lines: list[str] = []
        for entity in self.entities:
            lines.append(json.dumps(entity.to_dict(), sort_keys=True))
        for rel in self.relationships:
            lines.append(json.dumps(rel.to_dict(), sort_keys=True))
        return "\n".join(lines) + "\n"

    def write_to(self, path: Path | str) -> None:
        """Write the graph projection to a JSONL file."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_jsonl(), encoding="utf-8")

    # ------------------------------------------------------------------
    # Queries (minimal thin-slice set)
    # ------------------------------------------------------------------

    def what_blocks(self, entity_id: str) -> list[GraphEntity]:
        """Return FINDING entities that block the given entity via blocked_by."""
        blocked = set()
        for rel in self.relationships:
            if (
                rel.relationship_type == RelationshipType.BLOCKED_BY
                and rel.from_id == entity_id
            ):
                blocked.add(rel.to_id)
        return [e for e in self.entities if e.entity_id in blocked]

    def approved_bytes(self, entity_id: str) -> list[GraphEntity]:
        """Return AUTHORIZATION entities bound to the given entity."""
        auths = set()
        for rel in self.relationships:
            if (
                rel.relationship_type == RelationshipType.AUTHORIZED_BY
                and rel.from_id == entity_id
            ):
                auths.add(rel.to_id)
        return [e for e in self.entities if e.entity_id in auths]

    def evidence_for(self, relationship_id: str) -> GraphRelationship | None:
        """Return the full relationship record with provenance for a given ID."""
        for rel in self.relationships:
            if rel.relationship_id == relationship_id:
                return rel
        return None
