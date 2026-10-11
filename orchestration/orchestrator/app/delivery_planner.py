"""Non-executing delivery-loop planner over the authoritative delegation store.

The planner answers two questions for one delegated task:

1. What evidence/review applies to this exact candidate?
2. What action may be *proposed*, and what remains *held*?

It is a pure read-only projection consumer. It never dispatches workers,
reserves budget, revokes quarantine, mutates the ledger, or marks anything
accepted. Every decision carries source references into the authoritative
ledger so the proposal can be revalidated by the real execution authority.

Dispositions (fixed vocabulary; fail closed):

- ``next_action_proposal``    — a concrete next step is proposed (not executed).
- ``wait_for_existing_attempt`` — an active attempt/lease owns the task; do
  not dispatch a duplicate.
- ``blocked_on_evidence``     — no current candidate (or terminal failure)
  prevents any review-based progression.
- ``blocked_on_review``       — open findings, failing/contradicting verdicts,
  or an explicit BLOCKED state gate progression.
- ``blocked_on_authorization`` — required approval is missing; the planner
  cannot and does not create authorization by assertion.
- ``completed_within_scope``  — accepted within the recorded scope; no action.

No-progress detection: comparing the current decision with the previous one
(same disposition, action, candidate, and task version) flags a loop without
the planner ever mutating state to "make progress".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.delegation import DelegatedTask, DelegationStore, TaskState


class PlannerDisposition(StrEnum):
    next_action_proposal = "next_action_proposal"
    wait_for_existing_attempt = "wait_for_existing_attempt"
    blocked_on_evidence = "blocked_on_evidence"
    blocked_on_review = "blocked_on_review"
    blocked_on_authorization = "blocked_on_authorization"
    completed_within_scope = "completed_within_scope"


@dataclass(frozen=True)
class SourceReference:
    """One authoritative-ledger citation behind a planner decision."""

    source_artifact: str
    record: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PlannerDecision:
    task_id: str
    candidate_revision: str | None
    disposition: PlannerDisposition
    proposed_action: str | None
    reason: str
    source_references: tuple[SourceReference, ...] = ()
    no_progress: bool = False
    # The planner proposes; it never authorizes.
    authority_note: str = (
        "proposal only; the recorded execution authority must revalidate "
        "before any action"
    )


_TERMINAL = {TaskState.cancelled, TaskState.failed}


class DeliveryPlanner:
    """Read-only delivery-loop planner bound to a DelegationStore."""

    def __init__(self, store: DelegationStore) -> None:
        self._store = store

    # ------------------------------------------------------------- helpers --
    def _ref(self, task: DelegatedTask, record: str, **detail: Any) -> SourceReference:
        return SourceReference(
            source_artifact=self._store.ledger_path.name,
            record=record,
            detail={"task_id": task.task_id, "task_version": task.version, **detail},
        )

    def has_active_attempt(self, task: DelegatedTask) -> bool:
        """True only while an execution attempt genuinely owns the task.

        A ``running`` state with a live lease means work is in flight —
        dispatching again would duplicate execution. ``verifying`` means the
        candidate is submitted and the execution attempt is over: the lease
        gates execution, not review, so review proposals must flow. An expired
        or revoked lease marks the attempt suspect, never completed.
        """
        if task.state is TaskState.running:
            return task.lease is not None and task.lease.is_active
        return False

    # ---------------------------------------------------------------- plan --
    def plan(
        self,
        task_id: str,
        *,
        previous: PlannerDecision | None = None,
    ) -> PlannerDecision:
        task = self._store.get(task_id)
        if task is None:
            return PlannerDecision(
                task_id=task_id,
                candidate_revision=None,
                disposition=PlannerDisposition.blocked_on_evidence,
                proposed_action=None,
                reason="task not present in the authoritative delegation ledger",
                source_references=(
                    SourceReference(
                        source_artifact=self._store.ledger_path.name,
                        record="missing",
                        detail={"task_id": task_id, "present": False},
                    ),
                ),
            )

        candidate = task.candidate_revision
        refs: list[SourceReference] = [
            self._ref(task, "state", state=task.state.value),
        ]

        # 0. Completed within scope.
        if task.state is TaskState.completed and task.accepted is True:
            refs.append(self._ref(task, "accepted"))
            return self._finish(task, candidate, PlannerDisposition.completed_within_scope,
                                None, "accepted within recorded scope", refs, previous)

        # 1. Terminal non-completion.
        if task.state in _TERMINAL:
            refs.append(self._ref(task, "terminal", state=task.state.value))
            return self._finish(task, candidate, PlannerDisposition.blocked_on_evidence,
                                None, f"terminal state {task.state.value}; no action proposed",
                                refs, previous)

        # 2. Active-attempt duplicate prevention.
        if self.has_active_attempt(task):
            if task.lease is not None:
                refs.append(self._ref(
                    task, "lease",
                    holder=task.lease.holder,
                    epoch=task.lease.epoch,
                    expires_at=task.lease.expires_at,
                    revoked=task.lease.revoked,
                ))
            return self._finish(task, candidate, PlannerDisposition.wait_for_existing_attempt,
                                None,
                                f"active attempt owns the task (state={task.state.value}); "
                                "dispatching again would duplicate execution",
                                refs, previous)

        # 3. Explicit review gate blocked the task.
        if task.state is TaskState.blocked:
            refs.append(self._ref(task, "blocked"))
            return self._finish(task, candidate, PlannerDisposition.blocked_on_review,
                                None, "task is in blocked state; an authorized actor must resolve",
                                refs, previous)

        # 4. No candidate -> no evidence to review.
        if not candidate:
            return self._finish(task, None, PlannerDisposition.blocked_on_evidence,
                                None,
                                "no candidate revision submitted; nothing to review or accept",
                                refs, previous)

        # 4b. Expired/revoked authority cannot produce an action proposal.
        if task.lease is not None and not task.lease.is_active:
            refs.append(self._ref(
                task, "lease",
                holder=task.lease.holder,
                epoch=task.lease.epoch,
                expires_at=task.lease.expires_at,
                revoked=task.lease.revoked,
                inactive=True,
            ))
            return self._finish(
                task, candidate, PlannerDisposition.blocked_on_review,
                None,
                "lease expired or revoked; attempt is suspect; no action proposed",
                refs, previous,
            )

        # 5. Reviews on the current candidate (scope-bound; stale rows listed).
        current = [r for r in task.reviews if r.candidate_revision == candidate]
        stale = [r for r in task.reviews if r.candidate_revision != candidate]
        for r in stale:
            refs.append(self._ref(
                task, f"review:{r.stage}:{r.verdict}",
                scope_mismatch=True,
                review_candidate=r.candidate_revision,
                note="review targets a superseded candidate; it does not apply here",
            ))

        spec = self._latest(current, "spec")
        quality = self._latest(current, "quality")

        # Contradiction preservation: disagreeing verdicts on the same
        # stage+candidate are surfaced and fail closed, never averaged away.
        for stage in ("spec", "quality"):
            verdicts = {r.verdict for r in current if r.stage == stage}
            if len(verdicts) > 1:
                for r in current:
                    if r.stage == stage:
                        refs.append(self._ref(
                            task, f"review:{stage}:{r.verdict}",
                            reviewer=r.reviewer, contradicts=True,
                        ))
                return self._finish(task, candidate, PlannerDisposition.blocked_on_review,
                                    None,
                                    f"contradicting {stage} verdicts on candidate "
                                    f"{candidate}; preserved, fail closed",
                                    refs, previous)

        if spec is None:
            return self._finish(task, candidate, PlannerDisposition.next_action_proposal,
                                "dispatch_spec_review",
                                "candidate submitted; no current spec verdict",
                                refs, previous)
        if spec.verdict != "PASS":
            refs.append(self._ref(task, f"review:spec:{spec.verdict}", reviewer=spec.reviewer))
            return self._finish(task, candidate, PlannerDisposition.blocked_on_review,
                                None,
                                f"spec verdict {spec.verdict} on current candidate; "
                                "remediation and re-review required",
                                refs, previous)
        refs.append(self._ref(task, "review:spec:PASS", reviewer=spec.reviewer))

        if quality is None:
            return self._finish(task, candidate, PlannerDisposition.next_action_proposal,
                                "dispatch_quality_review",
                                "spec PASS on current candidate; quality review not yet recorded",
                                refs, previous)
        if quality.verdict != "APPROVED":
            refs.append(self._ref(task, f"review:quality:{quality.verdict}", reviewer=quality.reviewer))
            return self._finish(task, candidate, PlannerDisposition.blocked_on_review,
                                None,
                                f"quality verdict {quality.verdict} on current candidate",
                                refs, previous)
        refs.append(self._ref(task, "review:quality:APPROVED", reviewer=quality.reviewer))

        # 6. Authorization gate (planner never creates authorization).
        contract = task.contract or {}
        raw_required = contract.get("approval_required")
        raw_grant = contract.get("approval_granted")
        if "approval_required" in contract and not isinstance(raw_required, bool):
            refs.append(self._ref(
                task, "approval:required", malformed=True, value=raw_required,
            ))
            return self._finish(
                task, candidate, PlannerDisposition.blocked_on_authorization,
                None,
                "approval_required is not a boolean; fail closed",
                refs, previous,
            )
        approval_required = raw_required is True
        approval_granted = raw_grant is True
        if approval_required and not approval_granted:
            grant_malformed = raw_grant is not None and not isinstance(raw_grant, bool)
            refs.append(self._ref(
                task, "approval:required", malformed=grant_malformed,
            ))
            if raw_grant is not None:
                refs.append(self._ref(
                    task, "approval:granted", key="approval_granted",
                    value=raw_grant, malformed=grant_malformed,
                ))
            return self._finish(task, candidate, PlannerDisposition.blocked_on_authorization,
                                None,
                                "approval is required by contract and not granted; "
                                "recorded authorization must be revalidated by the "
                                "execution authority",
                                refs, previous)

        # 7. Everything satisfied: propose acceptance; authority stays held.
        return self._finish(task, candidate, PlannerDisposition.next_action_proposal,
                            "orchestrator_accept",
                            "all current-candidate gates satisfied; explicit orchestrator "
                            "acceptance remains the authority-bound step",
                            refs, previous)

    # ------------------------------------------------------------- internal --
    @staticmethod
    def _latest(reviews: list, stage: str):
        rows = [r for r in reviews if r.stage == stage]
        return rows[-1] if rows else None

    def _finish(
        self,
        task: DelegatedTask,
        candidate: str | None,
        disposition: PlannerDisposition,
        proposed_action: str | None,
        reason: str,
        refs: list[SourceReference],
        previous: PlannerDecision | None,
    ) -> PlannerDecision:
        previous_version = None
        if previous is not None:
            for ref in previous.source_references:
                if "task_version" in ref.detail:
                    previous_version = ref.detail["task_version"]
                    break
        no_progress = (
            previous is not None
            and previous.disposition is disposition
            and previous.proposed_action == proposed_action
            and previous.candidate_revision == candidate
            and previous.reason == reason
            and previous_version == task.version
        )
        return PlannerDecision(
            task_id=task.task_id,
            candidate_revision=candidate,
            disposition=disposition,
            proposed_action=proposed_action,
            reason=reason,
            source_references=tuple(refs),
            no_progress=no_progress,
        )
