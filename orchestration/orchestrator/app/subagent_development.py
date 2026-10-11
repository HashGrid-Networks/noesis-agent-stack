"""Subagent-driven development workflow controller.

This is a durable, local control-plane workflow for the pattern:
approved implementation plan -> fresh implementation session -> spec review ->
quality review -> final integration review. It deliberately does not execute
Claude Code/Codex itself in tests; execution is behind a small adapter interface
so the supervisor can enforce state, ownership, budgets, and review ordering
without depending on a live external backend.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

IMPLEMENTER_PROFILE = "noesis-forge"
SPEC_REVIEW_PROFILE = "noesis-sentinel"
QUALITY_REVIEW_PROFILE = "noesis-sentinel"
INTEGRATION_REVIEW_PROFILE = "noesis-skeptic"


class ReviewVerdict(StrEnum):
    PASS = "PASS"
    REQUEST_CHANGES = "REQUEST_CHANGES"
    BLOCKED = "BLOCKED"
    APPROVED = "APPROVED"


@dataclass(frozen=True)
class Finding:
    severity: str
    location: str
    rationale: str
    required_remediation: str


@dataclass
class ImplementationAttempt:
    attempt_id: str
    session_id: str
    context_id: str
    backend: str
    owner_profile: str
    state: str
    candidate_revision: str | None = None
    changed_paths: list[str] = field(default_factory=list)
    artifacts: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReviewRecord:
    review_id: str
    review_type: str
    verdict: str
    reviewer_profile: str
    candidate_revision: str
    findings: list[Finding]
    verification: dict[str, Any] = field(default_factory=dict)


@dataclass
class SDDTask:
    task_id: str
    text: str
    acceptance_criteria: list[str]
    non_goals: list[str]
    dependencies: list[str]
    expected_paths: list[str]
    verification_commands: list[str]
    state: str = "pending"
    candidate_revision: str | None = None
    attempts: list[ImplementationAttempt] = field(default_factory=list)
    reviews: list[ReviewRecord] = field(default_factory=list)
    retry_count: int = 0
    max_retries: int = 2
    # State captured when a question pauses the task; restored on resolution.
    resumable_state: str | None = None


@dataclass
class SDDRun:
    plan_id: str
    plan_revision: str
    approval_ref: str
    baseline_revision: str
    tasks: list[SDDTask]
    integration_reviews: list[ReviewRecord] = field(default_factory=list)
    status: str = "prepared"


@dataclass(frozen=True)
class IntegrationResult:
    verified: bool
    review: ReviewRecord


@dataclass(frozen=True)
class SessionLaunchRequest:
    task_id: str
    backend: str
    owner_profile: str
    contract: dict[str, Any]


@dataclass(frozen=True)
class SessionLaunchResult:
    session_id: str
    backend: str
    launcher_confirmed_backend: str
    context_id: str


class SessionAdapter(Protocol):
    def backend_available(self, backend: str) -> bool:
        ...

    def launch(self, request: SessionLaunchRequest) -> SessionLaunchResult:
        ...


class MockCodingSessionAdapter:
    """Deterministic test adapter for supervised coding-session launches."""

    def __init__(self, *, available_backends: dict[str, bool] | None = None) -> None:
        self.available_backends = available_backends or {"codex": True, "claude-code": True}
        self.launches: list[SessionLaunchRequest] = []
        self._counter = 0

    def backend_available(self, backend: str) -> bool:
        return bool(self.available_backends.get(backend, False))

    def launch(self, request: SessionLaunchRequest) -> SessionLaunchResult:
        self._counter += 1
        self.launches.append(request)
        return SessionLaunchResult(
            session_id=f"{request.backend}-session-{self._counter}",
            backend=request.backend,
            launcher_confirmed_backend=request.backend,
            context_id=f"fresh-{request.task_id}-{self._counter}",
        )


class WorkflowBlocked(RuntimeError):
    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


class SDDWorkflow:
    """Durable state machine for subagent-driven development."""

    def __init__(self, ledger_path: Path | str, *, session_adapter: SessionAdapter) -> None:
        self.ledger_path = Path(ledger_path)
        self.session_adapter = session_adapter
        self.runs: dict[str, SDDRun] = {}
        self._replay()

    # ------------------------------------------------------------- ingestion --
    def ingest_approved_plan(self, plan_text: str, *, approval_ref: str, baseline_revision: str) -> SDDRun:
        if not approval_ref:
            raise WorkflowBlocked("approval_required", "approved plan requires an approval reference")
        plan_revision = hashlib.sha256(plan_text.encode("utf-8")).hexdigest()
        plan_id = f"plan-{plan_revision[:12]}"
        if plan_id in self.runs:
            return self.runs[plan_id]
        tasks = _parse_tasks(plan_text)
        if not tasks:
            raise WorkflowBlocked("no_tasks", "approved plan contained no executable tasks")
        run = SDDRun(
            plan_id=plan_id,
            plan_revision=plan_revision,
            approval_ref=approval_ref,
            baseline_revision=baseline_revision,
            tasks=tasks,
        )
        self.runs[plan_id] = run
        self._append("plan_ingested", {"run": _run_to_dict(run)})
        return run

    def task(self, plan_id: str, task_id: str) -> SDDTask:
        run = self._run(plan_id)
        for task in run.tasks:
            if task.task_id == task_id:
                return task
        raise WorkflowBlocked("unknown_task", f"task {task_id} is not in {plan_id}")

    # --------------------------------------------------------------- states --
    def mark_ready(self, plan_id: str, task_id: str) -> SDDTask:
        task = self.task(plan_id, task_id)
        if task.state != "pending":
            return task
        deps = [self.task(plan_id, dep) for dep in task.dependencies]
        if any(dep.state != "completed" for dep in deps):
            raise WorkflowBlocked("dependencies_unsatisfied", "ready requires completed dependencies")
        task.state = "ready"
        self._append("task_ready", {"plan_id": plan_id, "task_id": task_id})
        return task

    def dispatch_implementation(self, plan_id: str, task_id: str, *, backend: str) -> ImplementationAttempt:
        task = self.task(plan_id, task_id)
        if task.state == "implementing":
            raise WorkflowBlocked("already_implementing", f"task {task_id} already has an active implementation attempt")
        if task.state not in {"ready", "remediation"}:
            raise WorkflowBlocked("task_not_ready", f"task {task_id} is {task.state}, not ready/remediation")
        return self._launch_attempt(plan_id, task, backend=backend)

    def dispatch_remediation(self, plan_id: str, task_id: str, *, backend: str) -> ImplementationAttempt:
        task = self.task(plan_id, task_id)
        if task.state != "remediation":
            raise WorkflowBlocked("remediation_not_ready", f"task {task_id} is {task.state}")
        if task.retry_count >= task.max_retries:
            raise WorkflowBlocked("retry_budget_exhausted", f"task {task_id} exhausted remediation budget")
        return self._launch_attempt(plan_id, task, backend=backend)

    def request_clarification(self, plan_id: str, task_id: str, *, question: str) -> SDDTask:
        """Pause the task for an authorized decision-maker (§11: questions pause
        execution instead of triggering assumptions)."""
        task = self.task(plan_id, task_id)
        if task.state not in {"ready", "implementing", "remediation"}:
            raise WorkflowBlocked("clarification_not_allowed", f"task {task_id} is {task.state}")
        task.resumable_state = task.state
        task.state = "awaiting_clarification"
        self._append("clarification_requested", {"plan_id": plan_id, "task_id": task_id, "question": question})
        return task

    def resolve_clarification(self, plan_id: str, task_id: str, *, answer: str, decision_maker: str) -> SDDTask:
        task = self.task(plan_id, task_id)
        if task.state != "awaiting_clarification":
            raise WorkflowBlocked("not_awaiting_clarification", f"task {task_id} is {task.state}")
        task.state = task.resumable_state or "ready"
        task.resumable_state = None
        self._append("clarification_resolved", {
            "plan_id": plan_id,
            "task_id": task_id,
            "answer": answer,
            "decision_maker": decision_maker,
        })
        return task

    def request_cancellation(self, plan_id: str, task_id: str, *, reason: str) -> SDDTask:
        """Begin cancellation. New work is prevented immediately; the task is
        not marked cancelled until termination is confirmed (§10 supervision)."""
        task = self.task(plan_id, task_id)
        if task.state in {"completed", "cancelled"}:
            return task
        task.state = "cancelling"
        for attempt in task.attempts:
            if attempt.state == "running":
                attempt.state = "cancelling"
        self._append("cancellation_requested", {"plan_id": plan_id, "task_id": task_id, "reason": reason})
        return task

    def confirm_cancellation(self, plan_id: str, task_id: str, *, cleanup_status: str) -> SDDTask:
        """Confirm termination/cleanup. Uncertain cleanup stays an explicit
        blocker (state remains cancelling) and prevents workspace reuse."""
        task = self.task(plan_id, task_id)
        if task.state != "cancelling":
            raise WorkflowBlocked("not_cancelling", f"task {task_id} is {task.state}")
        if cleanup_status != "confirmed":
            self._append("cancellation_cleanup_uncertain", {"plan_id": plan_id, "task_id": task_id, "cleanup_status": cleanup_status})
            raise WorkflowBlocked("cleanup_unconfirmed", f"cancellation cleanup is {cleanup_status}")
        for attempt in task.attempts:
            if attempt.state in {"running", "cancelling"}:
                attempt.state = "cancelled"
        task.state = "cancelled"
        self._append("cancellation_confirmed", {"plan_id": plan_id, "task_id": task_id, "cleanup_status": cleanup_status})
        return task

    def _launch_attempt(self, plan_id: str, task: SDDTask, *, backend: str) -> ImplementationAttempt:
        if not self.session_adapter.backend_available(backend):
            raise WorkflowBlocked("backend_unavailable", f"backend {backend} is not installed/authenticated/authorized")
        self._check_workspace_conflict(plan_id, task)
        request = SessionLaunchRequest(
            task_id=task.task_id,
            backend=backend,
            owner_profile=IMPLEMENTER_PROFILE,
            contract=self._implementation_contract(plan_id, task, backend),
        )
        launched = self.session_adapter.launch(request)
        if launched.launcher_confirmed_backend != backend:
            raise WorkflowBlocked("backend_mismatch", f"requested {backend}, launcher confirmed {launched.launcher_confirmed_backend}")
        attempt = ImplementationAttempt(
            attempt_id=str(uuid4()),
            session_id=launched.session_id,
            context_id=launched.context_id,
            backend=backend,
            owner_profile=IMPLEMENTER_PROFILE,
            state="running",
        )
        task.attempts.append(attempt)
        task.state = "implementing"
        self._append("implementation_started", {"plan_id": plan_id, "task_id": task.task_id, "attempt": _attempt_to_dict(attempt)})
        return attempt

    def complete_implementation(
        self,
        plan_id: str,
        task_id: str,
        *,
        candidate_revision: str,
        changed_paths: list[str],
        artifacts: dict[str, Any],
    ) -> SDDTask:
        task = self.task(plan_id, task_id)
        if not task.attempts:
            raise WorkflowBlocked("no_attempt", "implementation completion requires a launched attempt")
        attempt = task.attempts[-1]
        attempt.state = "completed"
        attempt.candidate_revision = candidate_revision
        attempt.changed_paths = list(changed_paths)
        attempt.artifacts = dict(artifacts)
        task.candidate_revision = candidate_revision
        # Any code change after review invalidates previous approvals for that revision.
        task.reviews = [r for r in task.reviews if r.candidate_revision == candidate_revision]
        task.state = "spec_review"
        self._append("implementation_completed", {
            "plan_id": plan_id,
            "task_id": task_id,
            "candidate_revision": candidate_revision,
            "changed_paths": changed_paths,
            "artifacts": artifacts,
        })
        return task

    def record_spec_review(
        self,
        plan_id: str,
        task_id: str,
        *,
        verdict: ReviewVerdict,
        reviewer_profile: str,
        candidate_revision: str,
        findings: list[Finding],
    ) -> ReviewRecord:
        task = self.task(plan_id, task_id)
        if task.state != "spec_review":
            raise WorkflowBlocked("spec_review_not_ready", f"task is {task.state}")
        if candidate_revision != task.candidate_revision:
            raise WorkflowBlocked("candidate_revision_mismatch", "spec review must target current candidate revision")
        if reviewer_profile == IMPLEMENTER_PROFILE:
            raise WorkflowBlocked("reviewer_not_independent", "implementer cannot review its own work")
        # Open critical/important findings block completion even if the
        # reviewer attempted a PASS verdict.
        if verdict == ReviewVerdict.PASS and any(f.severity in {"critical", "important"} for f in findings):
            verdict = ReviewVerdict.REQUEST_CHANGES
        review = ReviewRecord(str(uuid4()), "spec", verdict.value, reviewer_profile, candidate_revision, findings)
        task.reviews.append(review)
        if verdict == ReviewVerdict.PASS:
            task.state = "quality_review"
        elif verdict == ReviewVerdict.REQUEST_CHANGES:
            task.retry_count += 1
            task.state = "remediation"
        else:
            task.state = "blocked"
        self._append("spec_review_recorded", {"plan_id": plan_id, "task_id": task_id, "review": _review_to_dict(review), "state": task.state})
        return review

    def record_quality_review(
        self,
        plan_id: str,
        task_id: str,
        *,
        verdict: ReviewVerdict,
        reviewer_profile: str,
        candidate_revision: str,
        findings: list[Finding],
    ) -> ReviewRecord:
        task = self.task(plan_id, task_id)
        if not any(r.review_type == "spec" and r.verdict == ReviewVerdict.PASS.value and r.candidate_revision == candidate_revision for r in task.reviews):
            raise WorkflowBlocked("spec_review_required", "quality review requires spec PASS on current candidate revision")
        if task.state != "quality_review":
            raise WorkflowBlocked("quality_review_not_ready", f"task is {task.state}")
        if candidate_revision != task.candidate_revision:
            raise WorkflowBlocked("candidate_revision_mismatch", "quality review must target current candidate revision")
        if reviewer_profile == IMPLEMENTER_PROFILE:
            raise WorkflowBlocked("reviewer_not_independent", "implementer cannot approve its own work")
        if any(f.severity in {"critical", "important"} for f in findings):
            verdict = ReviewVerdict.REQUEST_CHANGES
        review = ReviewRecord(str(uuid4()), "quality", verdict.value, reviewer_profile, candidate_revision, findings)
        task.reviews.append(review)
        if verdict == ReviewVerdict.APPROVED:
            task.state = "completed"
        elif verdict == ReviewVerdict.REQUEST_CHANGES:
            task.retry_count += 1
            task.state = "remediation"
        else:
            task.state = "blocked"
        self._append("quality_review_recorded", {"plan_id": plan_id, "task_id": task_id, "review": _review_to_dict(review), "state": task.state})
        return review

    def record_integration_review(
        self,
        plan_id: str,
        *,
        verdict: ReviewVerdict,
        reviewer_profile: str,
        candidate_revision: str,
        verification: dict[str, Any],
        findings: list[Finding],
        affected_task_ids: list[str] | None = None,
    ) -> IntegrationResult:
        run = self._run(plan_id)
        if any(task.state != "completed" for task in run.tasks):
            raise WorkflowBlocked("tasks_not_completed", "integration review requires all task gates completed")
        if reviewer_profile == IMPLEMENTER_PROFILE:
            raise WorkflowBlocked("reviewer_not_independent", "integration reviewer must be independent")
        if any(f.severity in {"critical", "important"} for f in findings) and verdict == ReviewVerdict.APPROVED:
            raise WorkflowBlocked("blocking_findings", "critical/important findings block integration approval")
        review = ReviewRecord(str(uuid4()), "integration", verdict.value, reviewer_profile, candidate_revision, findings, verification)
        run.integration_reviews.append(review)
        if verdict == ReviewVerdict.APPROVED:
            run.status = "verified"
            verified = True
        else:
            run.status = "remediation"
            verified = False
            for tid in affected_task_ids or []:
                self.task(plan_id, tid).state = "remediation"
        self._append("integration_review_recorded", {"plan_id": plan_id, "review": _review_to_dict(review), "status": run.status, "affected_task_ids": affected_task_ids or []})
        return IntegrationResult(verified, review)

    def synthesize(self, plan_id: str) -> dict[str, Any]:
        run = self._run(plan_id)
        return {
            "plan_id": run.plan_id,
            "plan_revision": run.plan_revision,
            "status": run.status,
            "tasks": [{"task_id": t.task_id, "state": t.state, "candidate_revision": t.candidate_revision} for t in run.tasks],
            "integration_reviews": len(run.integration_reviews),
        }

    # --------------------------------------------------------------- helpers --
    def _implementation_contract(self, plan_id: str, task: SDDTask, backend: str) -> dict[str, Any]:
        run = self._run(plan_id)
        return {
            "contract_version": "subagent-driven-development/implementation/v1",
            "root_task_id": run.plan_id,
            "parent_task_id": task.task_id,
            "owning_coder_profile": IMPLEMENTER_PROFILE,
            "backend": backend,
            "objective": task.text,
            "acceptance_criteria": task.acceptance_criteria,
            "non_goals": task.non_goals,
            "baseline_revision": run.baseline_revision,
            "allowed_paths": task.expected_paths,
            "verification_commands": task.verification_commands,
            "data_classification": "internal_redacted",
            "required_approvals": [run.approval_ref],
            "stop_conditions": ["question", "approval_required", "policy_violation", "deadline_exceeded"],
            "tdd": "write/identify failing test before implementation when behavior is testable",
        }

    def _check_workspace_conflict(self, plan_id: str, task: SDDTask) -> None:
        run = self._run(plan_id)
        wanted = set(task.expected_paths)
        for other in run.tasks:
            if other.task_id == task.task_id or other.state != "implementing":
                continue
            if wanted & set(other.expected_paths):
                raise WorkflowBlocked("workspace_conflict", f"{task.task_id} overlaps active writer {other.task_id}")

    def _run(self, plan_id: str) -> SDDRun:
        try:
            return self.runs[plan_id]
        except KeyError as exc:
            raise WorkflowBlocked("unknown_plan", plan_id) from exc

    def _append(self, event: str, payload: dict[str, Any]) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.time(), "event": event, **payload}, sort_keys=True, default=_json_default) + "\n")

    def _replay(self) -> None:
        if not self.ledger_path.is_file():
            return
        for line in self.ledger_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            self._apply_event(event)

    def _apply_event(self, event: dict[str, Any]) -> None:
        name = event.get("event")
        if name == "plan_ingested":
            run = _run_from_dict(event["run"])
            self.runs[run.plan_id] = run
            return
        plan_id = event.get("plan_id")
        if not plan_id or plan_id not in self.runs:
            return
        if name == "task_ready":
            self.task(plan_id, event["task_id"]).state = "ready"
        elif name == "implementation_started":
            task = self.task(plan_id, event["task_id"])
            task.state = "implementing"
            task.attempts.append(_attempt_from_dict(event["attempt"]))
        elif name == "implementation_completed":
            task = self.task(plan_id, event["task_id"])
            if task.attempts:
                attempt = task.attempts[-1]
                attempt.state = "completed"
                attempt.candidate_revision = event["candidate_revision"]
                attempt.changed_paths = list(event.get("changed_paths") or [])
                attempt.artifacts = dict(event.get("artifacts") or {})
            task.candidate_revision = event["candidate_revision"]
            task.reviews = [r for r in task.reviews if r.candidate_revision == task.candidate_revision]
            task.state = "spec_review"
        elif name in {"spec_review_recorded", "quality_review_recorded"}:
            task = self.task(plan_id, event["task_id"])
            review = _review_from_dict(event["review"])
            task.reviews.append(review)
            task.state = event["state"]
        elif name == "integration_review_recorded":
            run = self._run(plan_id)
            run.integration_reviews.append(_review_from_dict(event["review"]))
            run.status = event["status"]
            for tid in event.get("affected_task_ids") or []:
                self.task(plan_id, tid).state = "remediation"
        elif name == "clarification_requested":
            task = self.task(plan_id, event["task_id"])
            task.resumable_state = task.state if task.state != "awaiting_clarification" else task.resumable_state
            task.state = "awaiting_clarification"
        elif name == "clarification_resolved":
            task = self.task(plan_id, event["task_id"])
            task.state = task.resumable_state or "ready"
            task.resumable_state = None
        elif name == "cancellation_requested":
            task = self.task(plan_id, event["task_id"])
            task.state = "cancelling"
            for attempt in task.attempts:
                if attempt.state == "running":
                    attempt.state = "cancelling"
        elif name == "cancellation_confirmed":
            task = self.task(plan_id, event["task_id"])
            for attempt in task.attempts:
                if attempt.state in {"running", "cancelling"}:
                    attempt.state = "cancelled"
            task.state = "cancelled"


def _parse_tasks(plan_text: str) -> list[SDDTask]:
    task_starts = list(re.finditer(r"(?m)^Task\s+(\S+):\s*(.+)$", plan_text))
    tasks: list[SDDTask] = []
    for idx, match in enumerate(task_starts):
        task_id, title = match.group(1).strip(), match.group(2).strip()
        end = task_starts[idx + 1].start() if idx + 1 < len(task_starts) else len(plan_text)
        block = plan_text[match.end():end]
        acceptance = _bullet_section(block, "Acceptance") or [title]
        non_goals = _bullet_section(block, "Non-goals")
        paths = _line_values(block, "Files")
        verify = _line_values(block, "Verify")
        deps = _line_values(block, "Depends")
        tasks.append(SDDTask(task_id, title, acceptance, non_goals, deps, paths, verify))
    return tasks


def _bullet_section(block: str, heading: str) -> list[str]:
    lines = block.splitlines()
    out: list[str] = []
    active = False
    for line in lines:
        stripped = line.strip()
        if stripped.rstrip(":") == heading:
            active = True
            continue
        if active and re.match(r"^[A-Z][A-Za-z -]+:", stripped):
            break
        if active and stripped.startswith("-"):
            out.append(stripped[1:].strip())
    return out


def _line_values(block: str, heading: str) -> list[str]:
    for line in block.splitlines():
        if line.strip().startswith(f"{heading}:"):
            value = line.split(":", 1)[1].strip()
            return [item.strip() for item in value.split(",") if item.strip()]
    return []


def _run_to_dict(run: SDDRun) -> dict[str, Any]:
    data = asdict(run)
    return data


def _run_from_dict(data: dict[str, Any]) -> SDDRun:
    return SDDRun(
        plan_id=data["plan_id"],
        plan_revision=data["plan_revision"],
        approval_ref=data["approval_ref"],
        baseline_revision=data["baseline_revision"],
        tasks=[_task_from_dict(t) for t in data["tasks"]],
        integration_reviews=[_review_from_dict(r) for r in data.get("integration_reviews", [])],
        status=data.get("status", "prepared"),
    )


def _task_from_dict(data: dict[str, Any]) -> SDDTask:
    return SDDTask(
        task_id=data["task_id"],
        text=data["text"],
        acceptance_criteria=list(data.get("acceptance_criteria") or []),
        non_goals=list(data.get("non_goals") or []),
        dependencies=list(data.get("dependencies") or []),
        expected_paths=list(data.get("expected_paths") or []),
        verification_commands=list(data.get("verification_commands") or []),
        state=data.get("state", "pending"),
        candidate_revision=data.get("candidate_revision"),
        attempts=[_attempt_from_dict(a) for a in data.get("attempts", [])],
        reviews=[_review_from_dict(r) for r in data.get("reviews", [])],
        retry_count=int(data.get("retry_count", 0)),
        max_retries=int(data.get("max_retries", 2)),
        resumable_state=data.get("resumable_state"),
    )


def _attempt_to_dict(attempt: ImplementationAttempt) -> dict[str, Any]:
    return asdict(attempt)


def _attempt_from_dict(data: dict[str, Any]) -> ImplementationAttempt:
    return ImplementationAttempt(**data)


def _review_to_dict(review: ReviewRecord) -> dict[str, Any]:
    data = asdict(review)
    data["findings"] = [asdict(f) for f in review.findings]
    return data


def _review_from_dict(data: dict[str, Any]) -> ReviewRecord:
    return ReviewRecord(
        review_id=data["review_id"],
        review_type=data["review_type"],
        verdict=data["verdict"],
        reviewer_profile=data["reviewer_profile"],
        candidate_revision=data["candidate_revision"],
        findings=[Finding(**f) for f in data.get("findings", [])],
        verification=dict(data.get("verification") or {}),
    )


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Finding):
        return asdict(obj)
    if isinstance(obj, StrEnum):
        return obj.value
    raise TypeError(type(obj).__name__)
