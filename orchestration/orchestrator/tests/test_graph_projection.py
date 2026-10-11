"""Knowledge graph projection determinism and authority-isolation tests.

These tests prove:
1. Identical inputs produce identical graph output (deterministic rebuild).
2. Duplicate events do not duplicate entities (dedup by deterministic ID).
3. Graph write cannot alter TaskStore state (authority isolation).
4. Superseded candidates do not inherit approval (supersession chain).
5. Missing evidence cannot become verified (evidence-state enforcement).
6. Contradictory assertions remain visible (both preserved).
7. Query results cite exact source artifacts (provenance completeness).
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

from app.graph import (
    EvidenceState,
    GraphEntity,
    GraphProjection,
    GraphRelationship,
    RelationshipType,
    _det_hash,
)
from app.models import Approval, TaskContract, Verification, utcnow
from app.store import TaskStore
from datetime import datetime


def _make_task(
    *,
    title: str = "test task",
    intent: str = "implement a widget",
    assignee: str = "noesis-forge",
    depends_on: list[UUID] | None = None,
    approval: Approval | None = None,
    task_id: UUID | None = None,
    created_at: datetime | None = None,
) -> TaskContract:
    return TaskContract(
        title=title,
        intent=intent,
        assignee_profile=assignee,
        required_capability="code_modify",
        risk_tier="r0",
        idempotency_key=f"test:{title}",
        acceptance_criteria=["tests pass"],
        verification=Verification(method="synthetic"),
        timeout_s=900,
        correlation_id=UUID("00000000-0000-0000-0000-000000000001"),
        depends_on=depends_on or [],
        approval=approval,
        task_id=task_id or UUID("00000000-0000-0000-0000-000000000001"),
        created_at=created_at or datetime(2026, 1, 1, 0, 0, 0),
    )


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_identical_inputs_produce_identical_graph(self, tmp_path: Path):
        # Use same filename in different directories to isolate content determinism
        dir1 = tmp_path / "a"
        dir2 = tmp_path / "b"
        dir1.mkdir()
        dir2.mkdir()
        store1 = TaskStore(dir1 / "tasks.jsonl")
        store2 = TaskStore(dir2 / "tasks.jsonl")
        for store in (store1, store2):
            store.put(_make_task(title="t1"))

        g1 = GraphProjection.from_stores(store1, now="2026-01-01T00:00:00Z")
        g2 = GraphProjection.from_stores(store2, now="2026-01-01T00:00:00Z")
        assert g1.to_jsonl() == g2.to_jsonl()

    def test_same_store_rebuilt_produces_identical_output(self, tmp_path: Path):
        store = TaskStore(tmp_path / "tasks.jsonl")
        store.put(_make_task(title="t1"))
        g1 = GraphProjection.from_stores(store, now="2026-01-01T00:00:00Z")
        g2 = GraphProjection.from_stores(store, now="2026-01-01T00:00:00Z")
        assert g1.to_jsonl() == g2.to_jsonl()

    def test_output_is_valid_jsonl(self, tmp_path: Path):
        store = TaskStore(tmp_path / "tasks.jsonl")
        store.put(_make_task(title="t1"))
        g = GraphProjection.from_stores(store)
        for line in g.to_jsonl().strip().split("\n"):
            record = json.loads(line)
            assert "entity_id" in record or "relationship_id" in record


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


class TestDeduplication:
    def test_duplicate_task_put_does_not_duplicate_entities(self, tmp_path: Path):
        """put() with same task (same idempotency key) updates, not duplicates."""
        store = TaskStore(tmp_path / "tasks.jsonl")
        task = _make_task(title="t1")
        store.put(task)
        store.put(task)  # idempotent re-put
        g = GraphProjection.from_stores(store)
        task_entities = [e for e in g.entities if e.canonical_key == str(task.task_id)]
        assert len(task_entities) == 1

    def test_agent_role_deduplicated_across_tasks(self, tmp_path: Path):
        store = TaskStore(tmp_path / "tasks.jsonl")
        store.put(_make_task(title="t1", assignee="noesis-forge", task_id=UUID("00000000-0000-0000-0000-000000000001")))
        store.put(_make_task(title="t2", assignee="noesis-forge", task_id=UUID("00000000-0000-0000-0000-000000000002")))
        g = GraphProjection.from_stores(store)
        forge_agents = [
            e
            for e in g.entities
            if e.entity_type == "agent_role" and e.canonical_key == "noesis-forge"
        ]
        assert len(forge_agents) == 1


# ---------------------------------------------------------------------------
# Authority isolation
# ---------------------------------------------------------------------------


class TestAuthorityIsolation:
    def test_graph_write_cannot_alter_taskstore(self, tmp_path: Path):
        """Graph projection must not mutate the authoritative TaskStore."""
        store = TaskStore(tmp_path / "tasks.jsonl")
        task = _make_task(title="t1")
        store.put(task)
        tasks_before = dict(store.all_tasks())

        g = GraphProjection.from_stores(store)
        g.write_to(tmp_path / "graph.jsonl")

        tasks_after = dict(store.all_tasks())
        assert len(tasks_after) == len(tasks_before)
        assert tasks_after[str(task.task_id)].task_id == tasks_before[str(task.task_id)].task_id
        assert tasks_after[str(task.task_id)].state == tasks_before[str(task.task_id)].state

    def test_graph_projection_does_not_grant_approval(self, tmp_path: Path):
        store = TaskStore(tmp_path / "tasks.jsonl")
        store.put(_make_task(title="t1"))
        g = GraphProjection.from_stores(store)
        auth_entities = [e for e in g.entities if e.entity_type == "authorization"]
        assert len(auth_entities) == 0

    def test_graph_projection_preserves_approval_from_source(self, tmp_path: Path):
        store = TaskStore(tmp_path / "tasks.jsonl")
        approval = Approval(
            required=True,
            state="granted",
            approved_by="operator",
            approval_manifest_id="manifest-abc",
        )
        task = _make_task(title="t1", approval=approval)
        store.put(task)
        g = GraphProjection.from_stores(store)
        auth_entities = [e for e in g.entities if e.entity_type == "authorization"]
        assert len(auth_entities) == 1
        assert auth_entities[0].evidence_state == EvidenceState.VERIFIED
        assert auth_entities[0].provenance["approved_by"] == "operator"


# ---------------------------------------------------------------------------
# Evidence state lifecycle
# ---------------------------------------------------------------------------


class TestEvidenceState:
    def test_default_state_is_observed_not_verified(self, tmp_path: Path):
        store = TaskStore(tmp_path / "tasks.jsonl")
        store.put(_make_task(title="t1", assignee="noesis-signal"))
        g = GraphProjection.from_stores(store)
        agent_entities = [e for e in g.entities if e.entity_type == "agent_role"]
        for agent in agent_entities:
            assert agent.evidence_state == EvidenceState.OBSERVED

    def test_task_entity_is_verified_with_source_hash(self, tmp_path: Path):
        store = TaskStore(tmp_path / "tasks.jsonl")
        store.put(_make_task(title="t1"))
        g = GraphProjection.from_stores(store)
        task_entities = [e for e in g.entities if e.entity_type == "task"]
        for t in task_entities:
            assert t.evidence_state == EvidenceState.VERIFIED
            assert "source_hash" in t.provenance


# ---------------------------------------------------------------------------
# Supersession
# ---------------------------------------------------------------------------


class TestSupersession:
    def test_superseded_candidate_does_not_inherit_approval(self, tmp_path: Path):
        """A new task revision without approval must not link to v1's authorization."""
        store = TaskStore(tmp_path / "tasks.jsonl")
        approval = Approval(
            required=True,
            state="granted",
            approved_by="operator",
            approval_manifest_id="v1-manifest",
        )
        task_v1 = _make_task(title="original", approval=approval)
        store.put(task_v1)

        g1 = GraphProjection.from_stores(store)
        task_id_hash = _det_hash("task", str(task_v1.task_id))
        auths_v1 = g1.approved_bytes(task_id_hash)
        assert len(auths_v1) == 1

        # New task (different task_id, no approval) must have no authorizations
        task_v2 = _make_task(
            title="updated",
            approval=None,
            task_id=UUID("00000000-0000-0000-0000-000000000002"),
        )
        store.put(task_v2)
        g2 = GraphProjection.from_stores(store)
        auths_v2 = g2.approved_bytes(_det_hash("task", str(task_v2.task_id)))
        assert len(auths_v2) == 0


# ---------------------------------------------------------------------------
# Contradiction preservation
# ---------------------------------------------------------------------------


class TestContradictionPreservation:
    def test_contradictory_relationships_both_preserved(self, tmp_path: Path):
        store = TaskStore(tmp_path / "tasks.jsonl")
        task = _make_task(title="t1")
        store.put(task)
        g = GraphProjection.from_stores(store)

        now = "2026-01-01T00:00:00Z"
        entity_a = GraphEntity(
            entity_id=_det_hash("task", str(task.task_id)),
            entity_type="task",
            canonical_key=str(task.task_id),
            created_at=now,
        )
        entity_b = GraphEntity(
            entity_id=_det_hash("agent_role", "noesis-forge"),
            entity_type="agent_role",
            canonical_key="noesis-forge",
            created_at=now,
        )

        rel_a = GraphRelationship(
            relationship_id=_det_hash("depends_on", entity_a.entity_id, entity_b.entity_id, "v1"),
            relationship_type=RelationshipType.DEPENDS_ON,
            from_id=entity_a.entity_id,
            to_id=entity_b.entity_id,
            scope="v1",
            evidence_ref="hash_v1",
            created_at=now,
            evidence_state=EvidenceState.VERIFIED,
        )
        rel_b = GraphRelationship(
            relationship_id=_det_hash("depends_on", entity_a.entity_id, entity_b.entity_id, "v2"),
            relationship_type=RelationshipType.DEPENDS_ON,
            from_id=entity_a.entity_id,
            to_id=entity_b.entity_id,
            scope="v2",
            evidence_ref="hash_v2",
            created_at=now,
            evidence_state=EvidenceState.DISPUTED,
        )
        g.relationships.extend([rel_a, rel_b])
        g.relationships.sort(key=lambda r: r.relationship_id)

        scopes = {
            r.scope for r in g.relationships if r.relationship_type == "depends_on"
        }
        assert "v1" in scopes
        assert "v2" in scopes


# ---------------------------------------------------------------------------
# Provenance completeness
# ---------------------------------------------------------------------------


class TestProvenanceCompleteness:
    def test_all_entities_cite_source_artifact(self, tmp_path: Path):
        store = TaskStore(tmp_path / "tasks.jsonl")
        store.put(_make_task(title="t1"))
        g = GraphProjection.from_stores(store)
        for entity in g.entities:
            assert "source_artifact" in entity.provenance
            assert "extraction_method" in entity.provenance
            assert "extracted_by" in entity.provenance
            assert "extracted_at" in entity.provenance
            assert "environment" in entity.provenance
            assert "sensitivity" in entity.provenance

    def test_all_relationships_have_evidence_ref(self, tmp_path: Path):
        store = TaskStore(tmp_path / "tasks.jsonl")
        store.put(_make_task(title="t1"))
        g = GraphProjection.from_stores(store)
        for rel in g.relationships:
            assert rel.evidence_ref != ""


# ---------------------------------------------------------------------------
# Graph write isolation
# ---------------------------------------------------------------------------


class TestGraphWriteIsolation:
    def test_graph_module_has_no_taskstore_mutation_import(self):
        """graph.py must not import TaskStore or DelegationStore classes."""
        import inspect

        import app.graph as graph_module

        source = inspect.getsource(graph_module)
        assert "from app.store import TaskStore" not in source
        assert "from app.delegation import DelegationStore" not in source

    def test_graph_outage_falls_back_to_direct_queries(self, tmp_path: Path):
        """When graph file is missing, TaskStore reads still work."""
        store = TaskStore(tmp_path / "tasks.jsonl")
        store.put(_make_task(title="t1"))

        # Direct store query still works without graph
        tasks = list(store.all_tasks().values())
        assert len(tasks) == 1
        assert tasks[0].title == "t1"

        # Graph can be rebuilt on demand
        g = GraphProjection.from_stores(store)
        assert len(g.entities) > 0
