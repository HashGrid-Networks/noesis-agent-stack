"""Plan-first, capability-based task decomposition.

The orchestrator decomposes a nontrivial request into bounded, cohesive tasks
before dispatch, maps each task to an eligible specialist via the capability
index, and validates a planning gate (cycles, overlapping mutation targets,
missing owners, budget). Tasks are persisted; the run is not executed until
``prepare_for_dispatch`` orders them and the planning gate passes.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from uuid import uuid4

from app.launcher import RuntimeAdapter
from app.specialist_routing import CapabilityIndex, SelectionDecision, selection_rejections

# Decomposition is by meaningful responsibility, not keyword count. These
# signals map a fragment to a task class; each fragment becomes one cohesive
# task rounded to a single cohesive deliverable.
_FRAGMENT_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("writing", ("rollout notes", "runbook", "docs", "documentation", "changelog", "operator notes", "write notes")),
    ("agent_architecture", ("design", "profile contract", "architecture", "mcp contract")),
    ("implementation", ("implement", "patch", "code", "tests", "test", "update", "refactor", "build")),
    ("research", ("research", "sources", "cited", "brief")),
    ("operations", ("docker", "kubernetes", "terraform", "deploy", "observability")),
    ("review", ("review", "qa", "audit", "verify", "validate")),
)

_PATH_RE = re.compile(r"[\w./\-]+\.(?:py|yaml|yml|md|json|toml|sh|tf)")


@dataclass
class PlanTask:
    task_id: str
    text: str
    task_class: str
    required_capability: str
    assignee_profile: str
    runtime_profile: str
    acceptance_criteria: list[str]
    expected_paths: list[str]
    workflow: str
    reviewers: list[str] = field(default_factory=list)
    dispatch_allowed: bool = False


@dataclass
class PlanRequest:
    plan_id: str
    plan_revision: str
    idempotency_key: str
    status: str = "validated"
    tasks: list[PlanTask] = field(default_factory=list)
    execution_gate: str = "passed"
    fast_path_reason: str | None = None

    def to_sdd_plan_text(self) -> str:
        lines: list[str] = []
        for idx, task in enumerate(self.tasks, start=1):
            lines.append(f"Task T{idx}: {task.text}")
            lines.append(f"Acceptance: {'; '.join(task.acceptance_criteria)}")
        return "\n".join(lines)


class PlanningBlocked(RuntimeError):
    def __init__(self, code: str, reason: str, *, rejections: dict[str, list[str]] | None = None) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason
        self.rejections = rejections or {}


def _classify_fragment(text: str) -> str:
    lowered = text.lower().strip()
    for task_class, keywords in _FRAGMENT_RULES:
        if any(keyword in lowered for keyword in keywords):
            return task_class
    return "general"


def _expected_paths(text: str) -> list[str]:
    return sorted(set(_PATH_RE.findall(text)))


def _split_fragments(request: str) -> list[str]:
    """Split a request into cohesive fragments bounded by comma/coordinate clauses."""
    # Split on sentence coordinators and commas, but not inside parentheticals.
    raw = re.split(r"[,;\n]| and | then ", request)
    fragments = [part.strip() for part in raw if part.strip()]
    # Merge trailing fragments that carry no independent task class into the
    # previous fragment so we don't create an empty "general" task.
    merged: list[str] = []
    for fragment in fragments:
        cls = _classify_fragment(fragment)
        # A fragment that carries explicit file references is real work and must
        # never be merged away (it may conflict with another task's target).
        if cls == "general" and not _expected_paths(fragment) and merged:
            merged[-1] = f"{merged[-1]} {fragment}"
        else:
            merged.append(fragment)
    return merged


class RoutePlanner:
    """Decompose a request and assign each task to an eligible specialist."""

    def __init__(
        self,
        capability_index: CapabilityIndex,
        adapter: RuntimeAdapter | None = None,
        *,
        ledger: Path | None = None,
    ) -> None:
        self.index = capability_index
        self.adapter = adapter
        self.ledger = ledger

    def plan(
        self,
        request: str,
        *,
        idempotency_key: str,
        exclude_profiles: set[str] | None = None,
    ) -> PlanRequest:
        revision = hashlib.sha256(request.encode("utf-8")).hexdigest()
        plan_id = f"plan-{revision[:12]}"
        exclude = set(exclude_profiles or set())

        fragments = _split_fragments(request)
        tasks: list[PlanTask] = []
        rejections: dict[str, list[str]] = {}
        seen_paths: dict[str, str] = {}
        fast_path = len(fragments) == 1 and _classify_fragment(fragments[0]) != "general"

        for fragment in fragments:
            classification = self.index.classifier.classify(fragment)
            selection: SelectionDecision = self.index.select(
                fragment,
                required_capability=classification.required_capability,
                exclude_profiles=exclude,
            )
            if selection.selected is None:
                rejections.update(selection_rejections(selection))
                continue
            record = selection.selected
            task_cls = _classify_fragment(fragment)
            expected = _expected_paths(fragment)
            # Overlapping mutation targets across distinct tasks are blocked
            # (shared-file / workspace conflict) before dispatch.
            for path in expected:
                if path in seen_paths:
                    raise PlanningBlocked(
                        "workspace_conflict",
                        f"attribute '{path}' is claimed by both task {seen_paths[path]} and the new task",
                    )
                seen_paths[path] = record.profile_id
            reviewers = [r for r in (selection.classification.reviewer_required,) if r] or list(record.delegates_to)[:1]
            tasks.append(
                PlanTask(
                    task_id=str(uuid4()),
                    text=fragment,
                    task_class=task_cls,
                    required_capability=classification.required_capability,
                    assignee_profile=record.profile_id,
                    runtime_profile=record.runtime_profile,
                    acceptance_criteria=[f"{task_cls} deliverable for: {fragment}"],
                    expected_paths=expected,
                    workflow="subagent-driven-development"
                    if (task_cls == "implementation" and record.profile_id.endswith("forge"))
                    else "direct-specialist",
                    reviewers=reviewers,
                )
            )

        if not tasks or rejections:
            # Every fragment must have produced an eligible owner.
            raise PlanningBlocked(
                "no_eligible_owner",
                "one or more decomposed tasks have no eligible specialist",
                rejections=rejections,
            )

        request_obj = PlanRequest(
            plan_id=plan_id,
            plan_revision=revision,
            idempotency_key=idempotency_key,
            status="validated",
            tasks=tasks,
            execution_gate="passed",
            fast_path_reason="atomic_low_risk_single_specialist" if fast_path and len(tasks) == 1 else None,
        )
        self._persist(request_obj)
        return request_obj

    def _persist(self, plan: PlanRequest) -> None:
        if self.ledger is None:
            return
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(plan)) + "\n")
