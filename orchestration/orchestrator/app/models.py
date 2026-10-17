"""Task contract model and the orchestrator state machine.

States (exactly the supervised lifecycle):

    proposed -> approved -> queued -> claimed -> running
             -> awaiting_review | blocked | failed | succeeded | cancelled

Transitions are validated here and nowhere else. Assignees report outcomes;
they never write a state directly.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

SCHEMA_VERSION = "1.0"

TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "proposed": frozenset({"approved", "cancelled", "blocked"}),
    "approved": frozenset({"queued", "cancelled", "blocked"}),
    "queued": frozenset({"claimed", "blocked", "cancelled", "failed"}),
    "claimed": frozenset({"running", "blocked", "cancelled", "failed"}),
    "running": frozenset(
        {"awaiting_review", "succeeded", "failed", "blocked", "cancelled"}
    ),
    "awaiting_review": frozenset({"succeeded", "failed", "blocked", "cancelled"}),
    "blocked": frozenset({"queued", "cancelled", "failed"}),
    "failed": frozenset({"queued", "cancelled"}),  # retry re-queues
    "succeeded": frozenset(),
    "cancelled": frozenset(),
}

RISK_TIERS = ("r0", "r1", "r2", "r3")

IRREVERSIBLE_OPERATIONS = frozenset(
    {
        "production_mutation",
        "financial_operation",
        "wallet_signing",
        "credential_rotation",
        "dns_or_firewall_change",
        "destructive_data_operation",
        "external_publication",
    }
)


class TransitionError(RuntimeError):
    """Raised when a state transition is not permitted."""


class IdentityError(RuntimeError):
    """Raised when authoritative execution identity is missing or mismatched.

    ``contract_revision``/``baseline`` are derived, never caller-supplied.
    A derivation that disagrees with an already-persisted value means the
    task's immutable definition (or the running control-plane code) changed
    underneath an in-flight execution — a stale/mismatched identity that must
    block, never silently pass or be overwritten.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


_BASELINE_CACHE: str | None = None


def compute_baseline() -> str:
    """Authoritative control-plane baseline: a manifest digest, never a placeholder.

    Sha256 over the bytes of the schema/state-authoritative orchestrator
    modules (this file plus its sibling store/control_plane/launcher
    modules), in a fixed order. No git dependency (tests run without a
    checkout), no caller input, no unchecked default. Computed once per
    process and cached, mirroring ``HermesCliAdapter._manifest_sha256``.
    """
    global _BASELINE_CACHE
    if _BASELINE_CACHE is not None:
        return _BASELINE_CACHE
    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for name in ("models.py", "store.py", "control_plane.py", "launcher.py"):
        path = root / name
        if path.is_file():
            digest.update(name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    _BASELINE_CACHE = digest.hexdigest()
    return _BASELINE_CACHE


def reset_baseline_cache() -> None:
    """Test-only: force the next ``compute_baseline()`` to recompute."""
    global _BASELINE_CACHE
    _BASELINE_CACHE = None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value else None


@dataclass
class Verification:
    method: str
    evidence_required: bool = True
    command: str | None = None


@dataclass
class Retry:
    max_attempts: int = 1
    attempts: int = 0
    backoff_s: int = 30

    @property
    def exhausted(self) -> bool:
        return self.attempts >= self.max_attempts


@dataclass
class Approval:
    required: bool
    state: str = "not_required"
    approved_by: str | None = None
    approved_at: datetime | None = None
    approval_manifest_id: str | None = None


# Review stages in the authoritative lifecycle. Spec compliance must pass for a
# candidate before quality review may begin; integration is plan/root-level.
REVIEW_STAGES = frozenset({"spec", "quality", "integration"})
SPEC_VERDICTS = frozenset({"PASS", "REQUEST_CHANGES", "BLOCKED"})
QUALITY_VERDICTS = frozenset({"APPROVED", "REQUEST_CHANGES", "BLOCKED"})
# Positive success only. Failure verdicts are never mapped toward PASS/APPROVED.
_POSITIVE_REVIEW_VERDICT = {
    "spec": "PASS",
    "quality": "APPROVED",
}


@dataclass
class ReviewRecord:
    """A review verdict bound to one candidate revision (never a stale diff)."""

    stage: str
    verdict: str
    reviewer: str
    candidate_revision: str
    findings: list[dict[str, Any]] = field(default_factory=list)
    at: datetime = field(default_factory=utcnow)


@dataclass
class Delegation:
    """Authoritative delegation/assignment record lived on the task contract.

    This is the extension of the TaskStore contract with delegation behavior;
    a task that has been explicitly assigned to a specialist carries this. It
    records assignment epoch (monotonic fencing), active attempt, resolved
    runtime profile, the current candidate revision/digest, revision-bound
    reviews, and the explicit orchestrator acceptance. Budget accounting is
    minimal and honest: unknown token usage is tracked as ``None``, never as 0.
    """

    assignment_epoch: int = 0
    attempt: int = 0
    runtime_profile: str | None = None
    candidate_revision: str | None = None
    candidate_digest: str | None = None
    granted_candidate_revision: str | None = None
    granted_candidate_digest: str | None = None
    contract_revision: str | None = None
    baseline: str | None = None
    reviews: list[ReviewRecord] = field(default_factory=list)
    accepted_by: str | None = None
    accepted_at: datetime | None = None
    request_limit: int | None = None
    requests_used: int = 0
    budget_tokens: int | None = None
    tokens_used: int | None = None  # None = unknown, tracked explicitly, never 0
    approved_provider: str | None = None
    approved_model: str | None = None
    launch_reservation: dict[str, Any] | None = None
    # Execution-ownership fencing. None = unknown/absent on old records;
    # never invent authority on replay.
    attempt_epoch: int | None = None
    execution_released_at: datetime | None = None
    execution_revoked: bool = False

    def current_reviews(self, stage: str) -> list[ReviewRecord]:
        rev = self.candidate_revision
        return [r for r in self.reviews if r.stage == stage and r.candidate_revision == rev]

    def current_verdicts(self) -> dict[str, str]:
        """Best current candidate's verdicts keyed by stage (later review wins)."""
        out: dict[str, str] = {}
        rev = self.candidate_revision
        if not rev:
            return out
        for r in self.reviews:
            if r.candidate_revision == rev:
                out[r.stage] = r.verdict
        return out

    def required_review_satisfied(self, stage: str) -> bool:
        """Satisfied only by the explicit positive verdict for that stage.

        Spec requires PASS. Quality requires APPROVED. Missing, unknown,
        malformed, wrong-stage, BLOCKED, REQUEST_CHANGES, and unsupported
        stages fail closed. A failure verdict is never coerced into success.
        """
        expected = _POSITIVE_REVIEW_VERDICT.get(stage)
        if expected is None:
            return False
        verdict = self.current_verdicts().get(stage)
        if not isinstance(verdict, str) or not verdict.strip():
            return False
        return verdict == expected

    def budget_exceeded(self) -> bool:
        if self.request_limit is not None and self.requests_used > self.request_limit:
            return True
        return False



@dataclass
class TaskContract:
    """Durable unit of supervised cross-profile work."""

    title: str
    intent: str
    assignee_profile: str
    required_capability: str
    risk_tier: str
    idempotency_key: str
    acceptance_criteria: list[str]
    verification: Verification
    timeout_s: int
    correlation_id: UUID
    retry: Retry = field(default_factory=Retry)
    approval: Approval | None = None
    reviewer_profile: str | None = None
    depends_on: list[UUID] = field(default_factory=list)
    irreversible_operations: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    parent_task_id: UUID | None = None
    traceparent: str | None = None

    task_id: UUID = field(default_factory=uuid4)
    state: str = "proposed"
    handoff: dict[str, Any] | None = None
    claim: dict[str, Any] | None = None
    created_at: datetime = field(default_factory=utcnow)
    created_by: str = "noesis-orchestrator"
    updated_at: datetime | None = None
    terminal_reason: str | None = None
    delegation: Delegation = field(default_factory=Delegation)
    # Compare-and-swap fence for TaskStore.put. Absent on legacy records (0).
    task_version: int = 0
    # Phase B (contract ``adaptive-specialist-preflight-b1``): immutable
    # preflight decision/evidence bound to the canonical task record. ``None``
    # means no preflight decision has been recorded yet — never default to
    # a fabricated "KEEP".
    #
    # Contract addendum (remediation B1, finding Skeptic §3.B / important):
    # the shape is keyed per ``attempt_epoch`` so that:
    #
    #   * each ``preflight_configuration`` call APPENDS one entry under the
    #     current ``task.delegation.attempt_epoch`` key (or ``0`` when no
    #     attempt has been granted yet), rather than overwriting the
    #     evidence;
    #   * ``submit_candidate`` revalidates evidence against the current
    #     ``attempt_epoch`` and rejects stale evidence with a typed block;
    #   * operators can replay the full decision history for a task across
    #     attempts without ever conflating per-attempt evidence.
    #
    # ``None`` ⇒ no preflight has ever run for this task. ``{}`` is not a
    # valid value (a never-preflighted task carries ``None``).
    preflight_evidence: dict[int, list[dict[str, Any]]] | None = None

    def __post_init__(self) -> None:
        if self.risk_tier not in RISK_TIERS:
            raise ValueError(f"Unknown risk tier: {self.risk_tier}")
        unknown = set(self.irreversible_operations) - IRREVERSIBLE_OPERATIONS
        if unknown:
            raise ValueError(f"Unknown irreversible operations: {sorted(unknown)}")
        if not self.acceptance_criteria:
            raise ValueError("acceptance_criteria must not be empty")
        if self.approval is None:
            self.approval = Approval(required=False, state="not_required")

    # ---------------------------------------------------------------- state --

    def can_transition_to(self, new_state: str) -> bool:
        return new_state in ALLOWED_TRANSITIONS.get(self.state, frozenset())

    def transition_to(self, new_state: str, *, reason: str | None = None) -> None:
        if new_state == self.state:
            raise TransitionError(f"Task is already in state '{new_state}'")
        if not self.can_transition_to(new_state):
            raise TransitionError(
                f"Illegal transition {self.state} -> {new_state} "
                f"(allowed: {sorted(ALLOWED_TRANSITIONS.get(self.state, []))})"
            )
        self.state = new_state
        self.updated_at = utcnow()
        if new_state in TERMINAL_STATES or new_state == "blocked":
            self.terminal_reason = reason
        # A re-queued task releases its lease so a fresh claim is required.
        if new_state == "queued":
            self.claim = None

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    # ---------------------------------------------------------------- lease --

    def apply_claim(self, claimed_by: str) -> None:
        now = utcnow()
        self.claim = {
            "claimed_by": claimed_by,
            "claimed_at": now,
            "lease_expires_at": now + timedelta(seconds=self.timeout_s),
        }
        # Execution identity is authoritative from this moment: every claim
        # path (claim(), mark_dispatched(), reserve_launch()) funnels through
        # apply_claim, so identity is derived here once, not asserted by a
        # caller and not left missing for any task that reaches "claimed".
        self.ensure_execution_identity()

    def lease_expired(self, *, now: datetime | None = None) -> bool:
        """True when a claimed/running task has outlived its timeout lease."""
        if not self.claim:
            return False
        now = now or utcnow()
        return now > self.claim["lease_expires_at"]

    # ----------------------------------------------------- execution identity --

    def compute_contract_revision(self) -> str:
        """Authoritative identity hash of this contract's immutable definition.

        Derived purely from fields that define what this task IS (never from
        caller-supplied assertions, never from mutable lifecycle state), so
        the same contract always derives the same revision and a tampered or
        divergent definition always derives a different one.
        """
        payload = {
            "task_id": str(self.task_id),
            "correlation_id": str(self.correlation_id),
            "idempotency_key": self.idempotency_key,
            "title": self.title,
            "intent": self.intent,
            "assignee_profile": self.assignee_profile,
            "required_capability": self.required_capability,
            "risk_tier": self.risk_tier,
            "acceptance_criteria": list(self.acceptance_criteria),
        }
        canonical = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def ensure_execution_identity(self) -> None:
        """Populate or verify authoritative ``contract_revision``/``baseline``.

        Values are always derived here, never accepted from a caller. A task
        entering execution for the first time gets its identity set. A task
        that already carries an identity (a retry/re-claim within the same
        process) must re-derive the SAME value; a derivation that disagrees
        with the persisted value is a stale/mismatched identity and blocks
        (raises ``IdentityError``) rather than silently overwriting or
        passing through.
        """
        revision = self.compute_contract_revision()
        if self.delegation.contract_revision is None:
            self.delegation.contract_revision = revision
        elif self.delegation.contract_revision != revision:
            raise IdentityError(
                "contract_revision_mismatch",
                f"persisted contract_revision '{self.delegation.contract_revision}' "
                f"!= derived '{revision}' for task {self.task_id}",
            )
        baseline = compute_baseline()
        if not baseline:
            raise IdentityError(
                "baseline_unavailable",
                "control-plane baseline manifest digest could not be derived",
            )
        if self.delegation.baseline is None:
            self.delegation.baseline = baseline
        elif self.delegation.baseline != baseline:
            raise IdentityError(
                "baseline_mismatch",
                f"persisted baseline '{self.delegation.baseline}' != derived "
                f"'{baseline}' for task {self.task_id}",
            )

    # ------------------------------------------------------------ serialize --

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["schema_version"] = SCHEMA_VERSION
        data["task_id"] = str(self.task_id)
        data["correlation_id"] = str(self.correlation_id)
        data["parent_task_id"] = str(self.parent_task_id) if self.parent_task_id else None
        data["depends_on"] = [str(d) for d in self.depends_on]
        data["created_at"] = _iso(self.created_at)
        data["updated_at"] = _iso(self.updated_at)
        data["preflight_evidence"] = self.preflight_evidence
        approval = data["approval"] or {}
        if approval:
            approval["approved_at"] = _iso(self.approval.approved_at if self.approval else None)
        if self.claim:
            data["claim"] = {
                "claimed_by": self.claim["claimed_by"],
                "claimed_at": _iso(self.claim["claimed_at"]),
                "lease_expires_at": _iso(self.claim["lease_expires_at"]),
            }
        data["delegation"] = {
            "assignment_epoch": self.delegation.assignment_epoch,
            "attempt": self.delegation.attempt,
            "runtime_profile": self.delegation.runtime_profile,
            "candidate_revision": self.delegation.candidate_revision,
            "candidate_digest": self.delegation.candidate_digest,
            "granted_candidate_revision": self.delegation.granted_candidate_revision,
            "granted_candidate_digest": self.delegation.granted_candidate_digest,
            "contract_revision": self.delegation.contract_revision,
            "baseline": self.delegation.baseline,
            "accepted_by": self.delegation.accepted_by,
            "accepted_at": _iso(self.delegation.accepted_at),
            "request_limit": self.delegation.request_limit,
            "requests_used": self.delegation.requests_used,
            "budget_tokens": self.delegation.budget_tokens,
            "tokens_used": self.delegation.tokens_used,
            "approved_provider": self.delegation.approved_provider,
            "approved_model": self.delegation.approved_model,
            "launch_reservation": self.delegation.launch_reservation,
            "attempt_epoch": self.delegation.attempt_epoch,
            "execution_released_at": _iso(self.delegation.execution_released_at),
            "execution_revoked": self.delegation.execution_revoked,
            "reviews": [
                {
                    "stage": r.stage,
                    "verdict": r.verdict,
                    "reviewer": r.reviewer,
                    "candidate_revision": r.candidate_revision,
                    "findings": list(r.findings),
                    "at": _iso(r.at),
                }
                for r in self.delegation.reviews
            ],
        }
        return data
