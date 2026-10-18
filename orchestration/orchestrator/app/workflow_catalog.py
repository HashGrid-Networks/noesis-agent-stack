"""Proposed workflow catalog loader (noesis-workflow-catalog/v1).

Workflow definitions live in ``orchestration/orchestrator/workflows/catalog.yaml``.
This is a PROPOSED specification layer, not a repository-native format: every
node is emitted as native ``TaskContract`` kwargs and admitted through the
normal ``Orchestrator.propose`` / ``policy.validate_admission`` path. No
parallel scheduler, ledger, approval service, or registry is introduced — the
existing control plane remains the single authority.

Guarantees enforced at load/validate time (fail fast, before any ledger write):

  * DAG acyclicity; ``depends_on`` references resolve inside the workflow
  * assignee is registry-visible; ``required_capability`` is declared by that
    profile (never inferred)
  * advisory (agency-curation) nodes are r0-only and flagged advisory
  * ``reviewer_profile`` is reviewer-only (mirrors policy)
  * timeout/retry bounds from the published contract schema
  * workspace uniqueness: two nodes sharing a workspace must be ordered by a
    dependency path in one direction
  * secret preflight: intent/acceptance text must not carry secret-like tokens
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from app import _yaml_compat as yaml
from app import registry
from app.models import TaskContract, Verification

REPO_ROOT = Path(__file__).resolve().parents[3]
CATALOG_PATH = REPO_ROOT / "orchestration" / "orchestrator" / "workflows" / "catalog.yaml"

RISK_TIERS = ("r0", "r1", "r2", "r3")
TIMEOUT_MIN_S = 60
TIMEOUT_MAX_S = 7200
MAX_ATTEMPTS_MAX = 5

_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key\s*[:=]|secret\s*[:=]|password\s*[:=]|bearer\s+[a-z0-9._-]{8,}|sk-[a-z0-9]{16,})"
)


@dataclass
class CatalogNode:
    node_id: str
    assignee_profile: str
    required_capability: str
    intent: str
    acceptance_criteria: list[str]
    risk_tier: str = "r0"
    reviewer_profile: str | None = None
    depends_on: list[str] = field(default_factory=list)
    advisory: bool = False
    timeout_s: int = 900
    max_attempts: int = 1
    workspace: str | None = None


@dataclass
class CatalogWorkflow:
    workflow_id: str
    description: str
    nodes: list[CatalogNode]

    def node_ids(self) -> set[str]:
        return {n.node_id for n in self.nodes}

    def get_node(self, node_id: str) -> CatalogNode | None:
        for n in self.nodes:
            if n.node_id == node_id:
                return n
        return None

    def topological_order(self) -> list[str]:
        """Kahn's algorithm; raises ValueError on a cycle."""
        edges = {n.node_id: list(n.depends_on) for n in self.nodes}
        indegree = {nid: 0 for nid in edges}
        for nid, deps in edges.items():
            for d in deps:
                if d not in indegree:
                    raise ValueError(f"unknown dependency '{d}' from '{nid}'")
                indegree[nid] += 1
        queue = [nid for nid, deg in indegree.items() if deg == 0]
        order: list[str] = []
        while queue:
            nid = queue.pop(0)
            order.append(nid)
            for other, deps in edges.items():
                if nid in deps:
                    indegree[other] -= 1
                    if indegree[other] == 0:
                        queue.append(other)
        if len(order) != len(edges):
            raise ValueError("workflow graph contains a dependency cycle")
        return order

    def reaches(self, src: str, dst: str) -> bool:
        """True when dst is reachable from src via depends_on edges."""
        edges = {n.node_id: list(n.depends_on) for n in self.nodes}
        seen: set[str] = set()
        stack = [src]
        while stack:
            cur = stack.pop()
            if cur == dst:
                return True
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(edges.get(cur, []))
        return False


@dataclass
class Catalog:
    version: str
    status: str
    shared_controls: dict[str, Any]
    workflows: list[CatalogWorkflow]

    def get_workflow(self, workflow_id: str) -> CatalogWorkflow | None:
        for wf in self.workflows:
            if wf.workflow_id == workflow_id:
                return wf
        return None


# ------------------------------------------------------------- loading --


def _node_from_raw(node_id: str, raw: dict[str, Any]) -> CatalogNode:
    return CatalogNode(
        node_id=node_id,
        assignee_profile=str(raw["assignee_profile"]),
        required_capability=str(raw["required_capability"]),
        intent=str(raw["intent"]),
        acceptance_criteria=[str(c) for c in (raw.get("acceptance_criteria") or [])],
        risk_tier=str(raw.get("risk_tier", "r0")),
        reviewer_profile=raw.get("reviewer_profile"),
        depends_on=[str(d) for d in (raw.get("depends_on") or [])],
        advisory=bool(raw.get("advisory", False)),
        timeout_s=int(raw.get("timeout_s", 900)),
        max_attempts=int(raw.get("max_attempts", 1)),
        workspace=raw.get("workspace"),
    )


def load_catalog(path: Path | None = None) -> Catalog:
    catalog_path = path or CATALOG_PATH
    raw = yaml.safe_load(catalog_path.read_text(encoding="utf-8")) or {}
    workflows = [
        CatalogWorkflow(
            workflow_id=str(wf["workflow_id"]),
            description=str(wf.get("description", "")),
            nodes=[
                _node_from_raw(node_id, node_raw)
                for node_id, node_raw in (wf.get("nodes") or {}).items()
            ],
        )
        for wf in (raw.get("workflows") or [])
    ]
    return Catalog(
        version=str(raw.get("catalog", {}).get("version", "0.1.0")),
        status=str(raw.get("catalog", {}).get("status", "designed")),
        shared_controls=dict(raw.get("shared_controls") or {}),
        workflows=workflows,
    )


# ------------------------------------------------------------ validation --


def validate_workflow(catalog: Catalog, workflow_id: str) -> list[str]:
    """Return human-readable issues; an empty list means the workflow is valid."""
    wf = catalog.get_workflow(workflow_id)
    if wf is None:
        return [f"unknown workflow '{workflow_id}'"]

    issues: list[str] = []
    max_tasks = int(catalog.shared_controls.get("max_tasks_per_workflow", 8))
    if len(wf.nodes) > max_tasks:
        issues.append(
            f"workflow has {len(wf.nodes)} nodes; shared limit is {max_tasks}"
        )

    try:
        wf.topological_order()
    except ValueError as exc:
        issues.append(str(exc))
        return issues  # further checks assume a coherent graph

    known = wf.node_ids()
    for node in wf.nodes:
        for dep in node.depends_on:
            if dep not in known:
                issues.append(
                    f"node '{node.node_id}': unknown dependency '{dep}'"
                )

        profile = registry.get_profile(node.assignee_profile)
        if profile is None:
            issues.append(
                f"node '{node.node_id}': assignee '{node.assignee_profile}' "
                f"is not a known profile"
            )
        else:
            if profile.capabilities and node.required_capability not in profile.capabilities:
                issues.append(
                    f"node '{node.node_id}': capability "
                    f"'{node.required_capability}' not declared by "
                    f"'{node.assignee_profile}'"
                )
            if profile.source == "agency-curation":
                if not node.advisory:
                    issues.append(
                        f"node '{node.node_id}': agency profile "
                        f"'{node.assignee_profile}' must be flagged advisory"
                    )
                if node.risk_tier != "r0":
                    issues.append(
                        f"node '{node.node_id}': advisory agency profile "
                        f"'{node.assignee_profile}' is r0-only, got "
                        f"'{node.risk_tier}'"
                    )

        if node.risk_tier not in RISK_TIERS:
            issues.append(
                f"node '{node.node_id}': invalid risk_tier '{node.risk_tier}'"
            )
        if not (TIMEOUT_MIN_S <= node.timeout_s <= TIMEOUT_MAX_S):
            issues.append(
                f"node '{node.node_id}': timeout_s {node.timeout_s} outside "
                f"[{TIMEOUT_MIN_S}, {TIMEOUT_MAX_S}]"
            )
        if not (1 <= node.max_attempts <= MAX_ATTEMPTS_MAX):
            issues.append(
                f"node '{node.node_id}': max_attempts {node.max_attempts} "
                f"outside [1, {MAX_ATTEMPTS_MAX}]"
            )

        if node.reviewer_profile is not None:
            reviewer = registry.get_profile(node.reviewer_profile)
            if reviewer is None:
                issues.append(
                    f"node '{node.node_id}': reviewer "
                    f"'{node.reviewer_profile}' is not a known profile"
                )
            elif not reviewer.is_reviewer_only:
                issues.append(
                    f"node '{node.node_id}': reviewer "
                    f"'{node.reviewer_profile}' is not reviewer-only"
                )

        secret_haystack = node.intent + " " + " ".join(node.acceptance_criteria)
        if _SECRET_RE.search(secret_haystack):
            issues.append(
                f"node '{node.node_id}': intent/acceptance text carries "
                f"secret-like material"
            )

    # workspace conflicts: same workspace requires dependency ordering
    by_workspace: dict[str, list[CatalogNode]] = {}
    for node in wf.nodes:
        if node.workspace:
            by_workspace.setdefault(node.workspace, []).append(node)
    for workspace, nodes in by_workspace.items():
        for i in range(len(nodes)):
            for j in range(i + 1, len(nodes)):
                a, b = nodes[i], nodes[j]
                if not (wf.reaches(a.node_id, b.node_id) or wf.reaches(b.node_id, a.node_id)):
                    issues.append(
                        f"workspace '{workspace}' shared by unordered nodes "
                        f"'{a.node_id}' and '{b.node_id}'"
                    )

    return issues


# -------------------------------------------------------------- emission --


def admit_workflow(
    orchestrator: Any,
    workflow_id: str,
    *,
    idempotency_scope: str | None = None,
    catalog: Catalog | None = None,
) -> dict[str, TaskContract]:
    """Emit a catalog workflow as native task contracts through Orchestrator.propose.

    Nodes are proposed in topological order so ``depends_on`` resolves to real
    ledger task ids. Emission is idempotent per (scope, node). No dispatch,
    launch, or inference occurs — admission only.
    """
    catalog = catalog or load_catalog()
    wf = catalog.get_workflow(workflow_id)
    if wf is None:
        raise ValueError(f"unknown workflow '{workflow_id}'")
    issues = validate_workflow(catalog, workflow_id)
    if issues:
        raise ValueError(f"workflow '{workflow_id}' is invalid: {issues}")

    scope = idempotency_scope or wf.workflow_id
    correlation_id = uuid4()
    tasks: dict[str, TaskContract] = {}
    for node_id in wf.topological_order():
        node = wf.get_node(node_id)
        assert node is not None
        labels = [f"workflow:{wf.workflow_id}", "catalog:noesis-workflow-catalog/v1"]
        if node.advisory:
            labels.append("advisory")
        task = orchestrator.propose(
            title=f"{wf.workflow_id}:{node.node_id}",
            intent=node.intent,
            assignee_profile=node.assignee_profile,
            required_capability=node.required_capability,
            acceptance_criteria=list(node.acceptance_criteria),
            verification=Verification(
                method="reviewer_signoff" if node.reviewer_profile else "human_inspection",
                evidence_required=True,
            ),
            idempotency_key=f"{scope}:{node.node_id}",
            correlation_id=correlation_id,
            risk_tier=node.risk_tier,
            timeout_s=node.timeout_s,
            max_attempts=node.max_attempts,
            depends_on=[tasks[d].task_id for d in node.depends_on],
            reviewer_profile=node.reviewer_profile,
            labels=labels,
        )
        tasks[node_id] = task
    return tasks
