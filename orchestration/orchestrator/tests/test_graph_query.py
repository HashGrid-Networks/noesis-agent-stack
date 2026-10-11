"""Knowledge graph query interface tests.

Proves that queries return source-evidence-backed results with full provenance.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from app.graph import (
    EntityType,
    EvidenceState,
    GraphEntity,
    GraphProjection,
    GraphRelationship,
    RelationshipType,
    _det_hash,
)
from app.graph_query import GraphQuery, QueryResult
from app.models import Approval, TaskContract, Verification
from app.store import TaskStore
from datetime import datetime


def _make_task(
    *,
    title: str = "test task",
    assignee: str = "noesis-forge",
    approval: Approval | None = None,
    task_id: UUID | None = None,
    created_at: datetime | None = None,
) -> TaskContract:
    return TaskContract(
        title=title,
        intent="implement a widget",
        assignee_profile=assignee,
        required_capability="code_modify",
        risk_tier="r0",
        idempotency_key=f"test:{title}",
        acceptance_criteria=["tests pass"],
        verification=Verification(method="synthetic"),
        timeout_s=900,
        correlation_id=UUID("00000000-0000-0000-0000-000000000001"),
        approval=approval,
        task_id=task_id or UUID("00000000-0000-0000-0000-000000000001"),
        created_at=created_at or datetime(2026, 1, 1, 0, 0, 0),
    )


def _build_projection(tmp_path: Path, tasks: list[TaskContract]) -> GraphProjection:
    store = TaskStore(tmp_path / "tasks.jsonl")
    for t in tasks:
        store.put(t)
    return GraphProjection.from_stores(store, now="2026-01-01T00:00:00Z")


# ---------------------------------------------------------------------------
# what_blocks
# ---------------------------------------------------------------------------


class TestWhatBlocks:
    def test_no_blockers_returns_empty(self, tmp_path: Path):
        g = _build_projection(tmp_path, [_make_task(title="t1")])
        gq = GraphQuery(g)
        task_id = _det_hash("task", "00000000-0000-0000-0000-000000000001")
        assert gq.what_blocks(task_id) == []

    def test_blocked_by_finding_returns_result(self, tmp_path: Path):
        g = _build_projection(tmp_path, [_make_task(title="t1")])
        task_id = _det_hash("task", "00000000-0000-0000-0000-000000000001")

        # Inject a finding + blocked_by relationship
        finding = GraphEntity(
            entity_id=_det_hash("finding", "critical-bug"),
            entity_type=EntityType.FINDING,
            canonical_key="critical-bug",
            created_at="2026-01-01T00:00:00Z",
            evidence_state=EvidenceState.VERIFIED,
            provenance={"source_artifact": "review.jsonl", "source_hash": "abc"},
        )
        g.entities.append(finding)
        g.relationships.append(
            GraphRelationship(
                relationship_id=_det_hash("blocked_by", task_id, finding.entity_id, "review"),
                relationship_type=RelationshipType.BLOCKED_BY,
                from_id=task_id,
                to_id=finding.entity_id,
                scope="review",
                evidence_ref="abc",
                created_at="2026-01-01T00:00:00Z",
                evidence_state=EvidenceState.VERIFIED,
                provenance={"source_artifact": "review.jsonl"},
            )
        )

        gq = GraphQuery(g)
        results = gq.what_blocks(task_id)
        assert len(results) == 1
        assert results[0].entity.entity_type == EntityType.FINDING
        assert results[0].evidence["source_artifact"] == "review.jsonl"


# ---------------------------------------------------------------------------
# approved_bytes
# ---------------------------------------------------------------------------


class TestApprovedBytes:
    def test_no_approval_returns_empty(self, tmp_path: Path):
        g = _build_projection(tmp_path, [_make_task(title="t1")])
        gq = GraphQuery(g)
        task_id = _det_hash("task", "00000000-0000-0000-0000-000000000001")
        assert gq.approved_bytes(task_id) == []

    def test_approval_returns_verified_result(self, tmp_path: Path):
        approval = Approval(
            required=True,
            state="granted",
            approved_by="operator",
            approval_manifest_id="manifest-1",
        )
        g = _build_projection(tmp_path, [_make_task(title="t1", approval=approval)])
        gq = GraphQuery(g)
        task_id = _det_hash("task", "00000000-0000-0000-0000-000000000001")
        results = gq.approved_bytes(task_id)
        assert len(results) == 1
        assert results[0].entity.entity_type == EntityType.AUTHORIZATION
        assert results[0].evidence["approved_by"] == "operator"
        assert results[0].evidence["evidence_state"] == "verified"


# ---------------------------------------------------------------------------
# changed_since
# ---------------------------------------------------------------------------


class TestChangedSince:
    def test_filters_by_timestamp(self, tmp_path: Path):
        old_task = _make_task(
            title="old",
            task_id=UUID("00000000-0000-0000-0000-000000000001"),
            created_at=datetime(2025, 1, 1),
        )
        new_task = _make_task(
            title="new",
            task_id=UUID("00000000-0000-0000-0000-000000000002"),
            created_at=datetime(2026, 6, 1),
        )
        g = _build_projection(tmp_path, [old_task, new_task])
        gq = GraphQuery(g)

        results = gq.changed_since(EntityType.TASK, "2026-01-01")
        assert len(results) == 1
        assert results[0].entity.canonical_key == str(new_task.task_id)

    def test_includes_all_after_timestamp(self, tmp_path: Path):
        task = _make_task(title="t1")
        g = _build_projection(tmp_path, [task])
        gq = GraphQuery(g)
        results = gq.changed_since(EntityType.TASK, "2020-01-01")
        assert len(results) >= 1


# ---------------------------------------------------------------------------
# evidence_for
# ---------------------------------------------------------------------------


class TestEvidenceFor:
    def test_returns_relationship_with_provenance(self, tmp_path: Path):
        g = _build_projection(tmp_path, [_make_task(title="t1")])
        gq = GraphQuery(g)
        rel_id = g.relationships[0].relationship_id
        result = gq.evidence_for(rel_id)
        assert result is not None
        assert result.relationship.relationship_id == rel_id
        assert "source_artifact" in result.evidence
        assert "evidence_ref" in result.evidence

    def test_unknown_id_returns_none(self, tmp_path: Path):
        g = _build_projection(tmp_path, [_make_task(title="t1")])
        gq = GraphQuery(g)
        assert gq.evidence_for("nonexistent") is None


# ---------------------------------------------------------------------------
# Entity/relationship lookup
# ---------------------------------------------------------------------------


class TestEntityLookup:
    def test_get_entity_by_id(self, tmp_path: Path):
        g = _build_projection(tmp_path, [_make_task(title="t1")])
        gq = GraphQuery(g)
        task_id = _det_hash("task", "00000000-0000-0000-0000-000000000001")
        entity = gq.get_entity(task_id)
        assert entity is not None
        assert entity.entity_type == EntityType.TASK

    def test_get_relationships_from(self, tmp_path: Path):
        g = _build_projection(tmp_path, [_make_task(title="t1")])
        gq = GraphQuery(g)
        task_id = _det_hash("task", "00000000-0000-0000-0000-000000000001")
        rels = gq.get_relationships_from(task_id, RelationshipType.DEPENDS_ON)
        assert len(rels) >= 1
        assert all(r.relationship_type == RelationshipType.DEPENDS_ON for r in rels)

    def test_get_relationships_to(self, tmp_path: Path):
        g = _build_projection(tmp_path, [_make_task(title="t1")])
        gq = GraphQuery(g)
        agent_id = _det_hash("agent_role", "noesis-forge")
        rels = gq.get_relationships_to(agent_id, RelationshipType.DEPENDS_ON)
        assert len(rels) >= 1


# ---------------------------------------------------------------------------
# Query result provenance
# ---------------------------------------------------------------------------


class TestQueryProvenance:
    def test_all_results_have_source_artifact(self, tmp_path: Path):
        approval = Approval(
            required=True, state="granted", approved_by="op", approval_manifest_id="m1"
        )
        g = _build_projection(tmp_path, [_make_task(title="t1", approval=approval)])
        gq = GraphQuery(g)
        task_id = _det_hash("task", "00000000-0000-0000-0000-000000000001")

        for result in gq.approved_bytes(task_id):
            assert result.evidence["source_artifact"] != ""

        for result in gq.changed_since(EntityType.TASK, "2020-01-01"):
            assert result.evidence["source_artifact"] != ""
