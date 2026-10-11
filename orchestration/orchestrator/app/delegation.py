"""Durable orchestrator-specialist handoff protocol and delegated-task state.

Control-plane mechanism for auditable delegation:

    validated task contract -> explicit specialist assignment ->
    supervised execution -> evidence-bearing result -> required reviews ->
    orchestrator acceptance

State is persisted as append-only JSONL snapshots keyed by task id; the latest
snapshot per task is reconstructed by replay. Every state-changing request is
bound to an assignment epoch (monotonic fencing), an expected task version
(optimistic concurrency), and an idempotency key so duplicate delivery never
duplicates work. Backend success (run finished) is kept distinct from task
acceptance (orchestrator explicitly accepts).

Envelope authentication (optional, opt-in): when a store is constructed with
an HmacEnvelopeAuthenticator, every HandoffEnvelope entering process() must
carry a valid HMAC-SHA256 signature over the canonical envelope content. A
sender_id claim inside a payload is not proof of identity — the signature
binds sender, recipient, task ancestry, and payload before any authorization
or state check runs. Key material is injected by the caller (never stored in
the ledger); without an authenticator the store preserves its previous
unauthenticated behavior for in-process/test use. Replay of a signed ASSIGN
is neutralized by idempotency keys and version/epoch fencing, not by the
signature alone; runtime process fencing remains a launcher-side concern.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import lease_lifecycle

extract_from_delegation = lease_lifecycle.extract_from_delegation
lease_renew = lease_lifecycle.lease_renew

PROTOCOL_VERSION = "noesis.delegated-task/v1"
ORCHESTRATOR_ID = "noesis-orchestrator"
IMPLEMENTER_ID = "noesis-forge"


class MessageType(StrEnum):
    ASSIGN = "ASSIGN"
    ACK = "ACK"
    STARTED = "STARTED"
    PROGRESS = "PROGRESS"
    HEARTBEAT = "HEARTBEAT"
    CLARIFICATION_REQUEST = "CLARIFICATION_REQUEST"
    APPROVAL_REQUEST = "APPROVAL_REQUEST"
    REPLAN_REQUEST = "REPLAN_REQUEST"
    RESULT = "RESULT"
    FAILURE = "FAILURE"
    CANCELLED = "CANCELLED"
    REMEDIATE = "REMEDIATE"
    CANCEL = "CANCEL"
    ACCEPT_RESULT = "ACCEPT_RESULT"
    REJECT_RESULT = "REJECT_RESULT"


class TaskState(StrEnum):
    pending = "pending"
    ready = "ready"
    assigned = "assigned"
    running = "running"
    verifying = "verifying"
    spec_review = "spec_review"
    quality_review = "quality_review"
    completed = "completed"
    blocked = "blocked"
    cancelled = "cancelled"
    failed = "failed"


@dataclass(frozen=True)
class Lease:
    holder: str
    epoch: int
    issued_at: str
    expires_at: str
    revoked: bool = False
    attempt_epoch: int | None = None
    execution_released_at: str | None = None

    @property
    def is_active(self) -> bool:
        return not self.revoked and self.expires_at > _utcnow()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lease_expiry(seconds: int = 1800) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _optional_attempt_epoch(data: dict[str, Any], *, field: str = "attempt_epoch") -> int | None:
    if field not in data:
        return None
    value = data[field]
    if value is None:
        return None
    if type(value) is not int:
        raise ActivationError(f"{field} must be an integer, got {type(value).__name__}")
    if value < 0:
        raise ActivationError(f"{field} must be non-negative")
    return value


def _optional_released_at(data: dict[str, Any], *, field: str = "execution_released_at") -> str | None:
    if field not in data:
        return None
    value = data[field]
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ActivationError(f"{field} must be an ISO timestamp string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ActivationError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ActivationError(f"{field} must be timezone-aware")
    return value


def _replace_lease(lease: Lease, **changes: Any) -> Lease:
    payload = asdict(lease)
    payload.update(changes)
    return Lease(**payload)


def _require_worker_epoch(
    *,
    current_epoch: int | None,
    expected: int | None,
    released: str | None,
) -> None:
    """Fail closed when persisted epoch authority exists and the worker omits or mismatches it.

    Legacy records with neither attempt_epoch nor execution_released_at keep
    absence as absence and do not invent a fence here.
    """
    if current_epoch is None and released is None:
        if expected is not None:
            raise ActivationError("stale_attempt_report: attempt_epoch mismatch")
        return
    if expected is None:
        raise ActivationError("stale_attempt_report: attempt_epoch is required")
    if current_epoch is None:
        raise ActivationError("stale_attempt_report: legacy record has no epoch authority")
    if current_epoch != expected:
        raise ActivationError("stale_attempt_report: attempt_epoch mismatch")


@dataclass
class Review:
    stage: str
    verdict: str
    reviewer: str
    candidate_revision: str
    findings: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class HandoffEnvelope:
    protocol_version: str
    message_id: str
    message_type: MessageType
    sender_id: str
    recipient_id: str
    root_task_id: str
    task_id: str
    attempt_id: str
    contract_revision: str
    assignment_epoch: int
    correlation_id: str
    idempotency_key: str | None = None
    expected_task_version: int | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    # Criterion 4 §3 requires a timestamp on every envelope. Signed: tampering
    # with it invalidates the signature. Defaults to issue time (UTC ISO-8601).
    timestamp: str = field(default_factory=_utcnow)
    # HMAC-SHA256 over canonical_envelope_bytes(), hex-encoded. Required iff
    # the receiving store was constructed with an authenticator.
    signature: str | None = None


def canonical_envelope_bytes(msg: HandoffEnvelope) -> bytes:
    """Deterministic serialization of every signed envelope field.

    Covers header fields AND payload, so any tampering with sender identity,
    task ancestry, epoch, issue time, or contract content invalidates the
    signature.
    """
    signed = {
        "protocol_version": msg.protocol_version,
        "message_id": msg.message_id,
        "message_type": str(msg.message_type),
        "sender_id": msg.sender_id,
        "recipient_id": msg.recipient_id,
        "root_task_id": msg.root_task_id,
        "task_id": msg.task_id,
        "attempt_id": msg.attempt_id,
        "contract_revision": msg.contract_revision,
        "assignment_epoch": msg.assignment_epoch,
        "correlation_id": msg.correlation_id,
        "idempotency_key": msg.idempotency_key,
        "expected_task_version": msg.expected_task_version,
        "timestamp": msg.timestamp,
        "payload": msg.payload,
    }
    return json.dumps(signed, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass
class DelegatedTask:
    task_id: str
    contract_revision: str
    root_task_id: str
    correlation_id: str
    assignee_profile: str
    required_capability: str
    intent: str
    acceptance_criteria: list[str]
    contract: dict[str, Any] = field(default_factory=dict)
    state: TaskState = TaskState.assigned
    version: int = 0
    assignment_epoch: int = 1
    candidate_revision: str | None = None
    lease: Lease | None = None
    reviews: list[Review] = field(default_factory=list)
    accepted: bool = False
    last_event: str = "assigned"
    idempotency_key: str | None = None
    budget_reserved: float = 0.0
    budget_consumed: float = 0.0
    created_at: str = field(default_factory=_utcnow)
    updated_at: str = field(default_factory=_utcnow)


class NoSuchTask(RuntimeError):
    pass


class ActivationError(RuntimeError):
    """A state change was refused by an authorization, version, or gate rule."""


class BudgetExhausted(ActivationError):
    """A reservation would exceed the root task's shared budget cap."""


class AuthenticationError(ActivationError):
    """A message arrived without a valid envelope signature.

    Raised before protocol, authorization, or state checks when the store is
    configured with an authenticator. Nothing is persisted: the message is
    refused, not quarantined into the ledger.
    """


@dataclass(frozen=True)
class HmacEnvelopeAuthenticator:
    """HMAC-SHA256 envelope authentication (shared-key, webhook-style).

    Binds sender_id, recipient_id, task ancestry, epoch, and payload to a
    single signature so a forged sender claim or tampered contract is rejected
    before any authorization decision. Key material is supplied by the caller
    and never written to the ledger; key distribution/rotation is an
    integration-layer concern outside this store.
    """

    key: bytes

    def sign(self, msg: HandoffEnvelope) -> str:
        return hmac.new(self.key, canonical_envelope_bytes(msg), hashlib.sha256).hexdigest()

    def verify(self, msg: HandoffEnvelope) -> bool:
        if not msg.signature:
            return False
        expected = hmac.new(self.key, canonical_envelope_bytes(msg), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, msg.signature)

    def apply(self, msg: HandoffEnvelope) -> HandoffEnvelope:
        """Return a copy of msg carrying its signature (envelope is frozen)."""
        return replace(msg, signature=self.sign(msg))


@dataclass
class DelegationStore:
    ledger_path: Path
    # When set, process() refuses any envelope without a valid HMAC signature
    # before protocol, authorization, or state checks. None preserves the
    # previous unauthenticated behavior (in-process/test use).
    authenticator: HmacEnvelopeAuthenticator | None = None

    def __post_init__(self) -> None:
        self.ledger_path = Path(self.ledger_path)
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self._tasks: dict[str, DelegatedTask] = {}
        self._idem: dict[str, str] = {}
        # message_id -> digest binding, populated only for envelopes that pass
        # signature verification. Per-process (restart re-opens the window);
        # durable replay defense remains idempotency keys + epoch/version
        # fencing, not this cache.
        self._msg_binding: dict[str, str] = {}
        self._lock = threading.Lock()
        self._replay()

    # -------------------------------------------------------------- replay --
    def _replay(self) -> None:
        if not self.ledger_path.exists():
            return
        for line in self.ledger_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            task_id = record["task"]["task_id"]
            task = _task_from_dict(record["task"])
            # Last-write-wins per task; version is a monotonically increasing
            # fencing counter, so we keep the highest version snapshot.
            current = self._tasks.get(task_id)
            if current is None or task.version >= current.version:
                self._tasks[task_id] = task
            if task.idempotency_key:
                self._idem[task.idempotency_key] = task_id

    def _all_records(self) -> list[DelegatedTask]:
        """Latest task per id; used by tests for dedup checks."""
        return sorted(self._tasks.values(), key=lambda t: t.created_at)

    def get(self, task_id: str) -> DelegatedTask | None:
        return self._tasks.get(task_id)

    def _require(self, task_id: str) -> DelegatedTask:
        task = self._tasks.get(task_id)
        if task is None:
            raise NoSuchTask(task_id)
        return task

    def _append(self, task: DelegatedTask) -> None:
        task.updated_at = _utcnow()
        with self.ledger_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"task": asdict(task)}) + "\n")

    def _mutate(self, task: DelegatedTask, *, event: str) -> DelegatedTask:
        task.version += 1
        task.last_event = event
        self._tasks[task.task_id] = task
        self._append(task)
        return task

    def _check_version(self, task: DelegatedTask, expected_task_version: int | None) -> None:
        """Optimistic concurrency: a stale expected version must not overwrite newer state."""
        if expected_task_version is not None and expected_task_version != task.version:
            raise ActivationError(
                f"task version conflict: expected {expected_task_version}, got {task.version}"
            )

    # -------------------------------------------------------------- assign --
    def process(self, msg: HandoffEnvelope) -> DelegatedTask:
        if self.authenticator is not None and not self.authenticator.verify(msg):
            # Authentication precedes everything: an unsigned or forged
            # envelope is refused without touching protocol, authorization,
            # or state, and nothing is persisted.
            raise AuthenticationError(
                f"message {msg.message_id} failed envelope authentication"
            )
        if self.authenticator is not None:
            digest = hashlib.sha256(canonical_envelope_bytes(msg)).hexdigest()
            bound = self._msg_binding.get(msg.message_id)
            if bound is not None and bound != digest:
                raise AuthenticationError(
                    f"message_id {msg.message_id} reused with conflicting content"
                )
            self._msg_binding[msg.message_id] = digest
        if msg.protocol_version != PROTOCOL_VERSION:
            raise ActivationError(f"unsupported protocol {msg.protocol_version}")
        if msg.message_type is not MessageType.ASSIGN:
            raise ActivationError("only ASSIGN messages are accepted at the entrypoint")
        if msg.sender_id != ORCHESTRATOR_ID:
            raise ActivationError(f"unauthorized sender {msg.sender_id}; only {ORCHESTRATOR_ID} may assign")
        if msg.idempotency_key and msg.idempotency_key in self._idem:
            # Duplicate ASSIGN delivery: return the existing task unchanged.
            return self._require(self._idem[msg.idempotency_key])
        existing = self._require(msg.task_id) if msg.task_id in self._tasks else None
        if existing is not None:
            if msg.assignment_epoch < existing.assignment_epoch:
                raise ActivationError(f"stale assignment epoch {msg.assignment_epoch} < {existing.assignment_epoch}")
            if existing.state not in {TaskState.cancelled, TaskState.failed, TaskState.completed}:
                return existing
        if msg.expected_task_version is not None and existing is not None and msg.expected_task_version != existing.version:
            raise ActivationError(
                f"task version conflict: expected {msg.expected_task_version}, got {existing.version}"
            )
        payload = msg.payload or {}
        budget_requirement = float(payload.get("budget_requirement") or 0.0)
        budget_cap = payload.get("budget_cap")
        task = DelegatedTask(
            task_id=msg.task_id,
            contract_revision=msg.contract_revision,
            root_task_id=msg.root_task_id,
            correlation_id=msg.correlation_id,
            assignee_profile=msg.recipient_id,
            required_capability=str(payload.get("required_capability", "")),
            intent=str(payload.get("intent", "")),
            acceptance_criteria=list(payload.get("acceptance_criteria") or []),
            contract=payload,
            state=TaskState.assigned,
            version=0,
            assignment_epoch=msg.assignment_epoch,
            idempotency_key=msg.idempotency_key,
            budget_reserved=budget_requirement,
        )
        # Atomic shared-budget check-and-reserve: the task is inserted and
        # persisted only if the root's aggregate reservation fits the cap.
        # On exhaustion nothing is persisted — the ASSIGN fails explicitly.
        with self._lock:
            self._check_budget(msg.root_task_id, budget_requirement, budget_cap)
            if msg.idempotency_key:
                self._idem[msg.idempotency_key] = task.task_id
            self._tasks[msg.task_id] = task
            self._append(task)
        return task

    # -------------------------------------------------------------- budget --
    def _root_reserved(self, root_task_id: str, *, exclude_task_id: str | None = None) -> float:
        total = 0.0
        for task in self._tasks.values():
            if task.root_task_id != root_task_id or task.task_id == exclude_task_id:
                continue
            total += task.budget_reserved
        return total

    def _check_budget(self, root_task_id: str, amount: float, cap: float | None) -> None:
        if amount <= 0:
            return
        if cap is None:
            return
        projected = self._root_reserved(root_task_id) + amount
        if projected > float(cap):
            raise BudgetExhausted(
                f"budget_exhausted: root {root_task_id} reservation {projected} exceeds cap {float(cap)}"
            )

    def reserve_budget(
        self,
        task_id: str,
        amount: float,
        *,
        by: str,
        cap: float | None = None,
        expected_task_version: int | None = None,
    ) -> DelegatedTask:
        """Reserve additional budget against the task's root. Orchestrator-only;
        atomic with respect to concurrent reservations on the same root."""
        task = self._require(task_id)
        self._check_version(task, expected_task_version)
        if by != ORCHESTRATOR_ID:
            raise ActivationError("only the orchestrator reserves budget")
        if amount <= 0:
            raise ActivationError("reservation amount must be positive")
        with self._lock:
            self._check_budget(task.root_task_id, amount, cap)
            task.budget_reserved += amount
        return self._mutate(task, event="budget_reserved")

    def consume_budget(
        self,
        task_id: str,
        amount: float,
        *,
        by: str,
        expected_task_version: int | None = None,
        expected_attempt_epoch: int | None = None,
    ) -> DelegatedTask:
        """Record measured usage against the task's reservation. Assignee-only;
        consumption can never exceed what was reserved (unknown usage is not
        silently treated as zero — it must be reserved first)."""
        task = self._require(task_id)
        self._check_version(task, expected_task_version)
        if by != task.assignee_profile:
            raise ActivationError("only the assigned specialist consumes budget")
        if task.lease is not None and task.lease.revoked:
            raise ActivationError("execution authority revoked; worker action refused")
        if task.lease is not None:
            _require_worker_epoch(
                current_epoch=task.lease.attempt_epoch,
                expected=expected_attempt_epoch,
                released=task.lease.execution_released_at,
            )
            if task.lease.execution_released_at is not None:
                raise ActivationError("stale_attempt_report: execution already released")
        if amount < 0:
            raise ActivationError("consumption amount must be non-negative")
        with self._lock:
            if task.budget_consumed + amount > task.budget_reserved:
                raise ActivationError(
                    f"budget_overconsumption: consumed {task.budget_consumed + amount} "
                    f"> reserved {task.budget_reserved}; reserve before consuming"
                )
            task.budget_consumed += amount
        return self._mutate(task, event="budget_consumed")

    # ---------------------------------------------------------- lifecycle --
    def mark_ready(self, task_id: str, *, by: str, expected_task_version: int | None = None) -> DelegatedTask:
        task = self._require(task_id)
        self._check_version(task, expected_task_version)
        if by != ORCHESTRATOR_ID:
            raise ActivationError("only the orchestrator marks a task ready")
        if task.state is not TaskState.assigned:
            raise ActivationError(f"task {task_id} is {task.state}, not assigned")
        task.state = TaskState.ready
        return self._mutate(task, event="ready")

    def start_running(
        self,
        task_id: str,
        *,
        by: str,
        expected_task_version: int | None = None,
        expected_attempt_epoch: int | None = None,
    ) -> DelegatedTask:
        task = self._require(task_id)
        self._check_version(task, expected_task_version)
        if by != task.assignee_profile:
            raise ActivationError("only the assigned specialist may start execution")
        if task.lease is not None and task.lease.revoked:
            raise ActivationError("lease expired or revoked; cannot start execution")
        if task.lease is not None and task.lease.execution_released_at is not None:
            raise ActivationError("stale_attempt_report: execution already released")
        if task.state not in {TaskState.ready, TaskState.running}:
            raise ActivationError(f"task {task_id} is {task.state}, not ready")
        if task.lease is not None and task.lease.attempt_epoch is not None:
            _require_worker_epoch(
                current_epoch=task.lease.attempt_epoch,
                expected=expected_attempt_epoch,
                released=task.lease.execution_released_at,
            )
            if not task.lease.is_active:
                raise ActivationError("lease expired or revoked; cannot start execution")
            task.state = TaskState.running
            return self._mutate(task, event="started")
        if task.lease is not None and not task.lease.is_active:
            raise ActivationError("lease expired or revoked; cannot start execution")
        task.lease = Lease(
            holder=by,
            epoch=task.assignment_epoch,
            issued_at=_utcnow(),
            expires_at=_lease_expiry(),
            attempt_epoch=1,
            execution_released_at=None,
        )
        task.state = TaskState.running
        return self._mutate(task, event="started")

    def grant_new_attempt(
        self,
        task_id: str,
        *,
        by: str,
        expected_task_version: int,
        intended_assignee: str | None = None,
        intended_candidate_revision: str | None = None,
    ) -> DelegatedTask:
        """Orchestrator-controlled new attempt. Increments attempt_epoch.

        `by` is an in-process caller label compared to ORCHESTRATOR_ID; it is
        not authentication. Production must not expose this method to
        untrusted callers.
        """
        task = self._require(task_id)
        self._check_version(task, expected_task_version)
        if by != ORCHESTRATOR_ID:
            raise ActivationError("only the orchestrator grants a new attempt")
        if intended_assignee is not None and intended_assignee != task.assignee_profile:
            raise ActivationError("intended assignee does not match task assignee")
        if task.lease is not None and task.lease.revoked:
            raise ActivationError("execution authority revoked; cannot grant a new attempt")
        if task.state not in {
            TaskState.running,
            TaskState.verifying,
            TaskState.spec_review,
            TaskState.quality_review,
        }:
            raise ActivationError(f"task {task_id} is {task.state}; cannot grant a new attempt")
        current_epoch = task.lease.attempt_epoch if task.lease is not None else None
        next_epoch = (current_epoch or 0) + 1
        task.lease = Lease(
            holder=task.assignee_profile,
            epoch=task.assignment_epoch,
            issued_at=_utcnow(),
            expires_at=_lease_expiry(),
            attempt_epoch=next_epoch,
            execution_released_at=None,
            revoked=False,
        )
        task.reviews = []
        if intended_candidate_revision is not None:
            task.candidate_revision = intended_candidate_revision
        task.state = TaskState.running
        return self._mutate(task, event="new_attempt_granted")

    def submit_verifying(
        self,
        task_id: str,
        *,
        by: str,
        candidate_revision: str,
        expected_task_version: int | None = None,
        expected_attempt_epoch: int | None = None,
    ) -> DelegatedTask:
        task = self._require(task_id)
        self._check_version(task, expected_task_version)
        if by != task.assignee_profile:
            raise ActivationError("only the assigned specialist may submit results")
        if task.lease is not None and task.lease.revoked:
            raise ActivationError("execution authority revoked; worker action refused")
        current_epoch = task.lease.attempt_epoch if task.lease is not None else None
        released = task.lease.execution_released_at if task.lease is not None else None
        _require_worker_epoch(
            current_epoch=current_epoch,
            expected=expected_attempt_epoch,
            released=released,
        )
        if released is not None and task.candidate_revision != candidate_revision:
            raise ActivationError("stale_attempt_report: execution already released for this epoch")
        # Duplicate RESULT for the same candidate revision is idempotent.
        if task.state is TaskState.verifying and task.candidate_revision == candidate_revision:
            return task
        # A new candidate revision may be submitted from running (first result),
        # verifying (remediation after quality), or spec_review (remediation
        # after spec review). It invalidates reviews bound to older revisions.
        if task.state not in {TaskState.running, TaskState.verifying, TaskState.spec_review}:
            raise ActivationError(f"task {task_id} is {task.state}, not running/verifying/spec_review")
        if task.candidate_revision != candidate_revision:
            task.reviews = [r for r in task.reviews if r.candidate_revision == candidate_revision]
        next_epoch = current_epoch if current_epoch is not None else 1
        task.candidate_revision = candidate_revision
        task.state = TaskState.verifying
        if task.lease is not None:
            task.lease = _replace_lease(
                task.lease,
                attempt_epoch=next_epoch,
                execution_released_at=released or _utcnow(),
            )
        return self._mutate(task, event="verifying")

    def record_review(
        self,
        task_id: str,
        *,
        stage: str,
        verdict: str,
        reviewer: str,
        candidate_revision: str,
        findings: list[dict[str, Any]],
        expected_task_version: int | None = None,
    ) -> DelegatedTask:
        task = self._require(task_id)
        self._check_version(task, expected_task_version)
        if stage not in {"spec", "quality"}:
            raise ActivationError(f"unknown review stage {stage}")
        # Independence: the assigned specialist — whatever its profile — can
        # never act as its own reviewer (Subgoal 4 §2 authorization).
        if reviewer == task.assignee_profile or reviewer == IMPLEMENTER_ID:
            raise ActivationError("implementation profile cannot review its own work")
        if candidate_revision != task.candidate_revision:
            raise ActivationError("review must target the current candidate revision")
        if any(f.get("severity") in {"critical", "important"} for f in findings):
            raise ActivationError("critical/important findings block a passing verdict")
        if stage == "quality":
            spec_ok = any(
                r.stage == "spec" and r.verdict == "PASS" and r.candidate_revision == candidate_revision
                for r in task.reviews
            )
            if not spec_ok:
                raise ActivationError("quality review requires spec PASS on the current candidate revision")
            if task.state is not TaskState.spec_review:
                raise ActivationError(f"task {task_id} is {task.state}, not spec_review")
        else:  # spec
            if task.state not in {TaskState.verifying, TaskState.spec_review}:
                raise ActivationError(f"task {task_id} is {task.state}, cannot spec-review")

        review = Review(stage, verdict, reviewer, candidate_revision, list(findings))
        # Reviews bind to the candidate revision; rows for an older revision are
        # invalidated by removing non-current rows.
        task.reviews = [r for r in task.reviews if r.candidate_revision == candidate_revision]
        task.reviews.append(review)
        if verdict == "BLOCKED":
            task.state = TaskState.blocked
        elif stage == "spec" and verdict == "PASS":
            task.state = TaskState.spec_review
        elif stage == "spec":
            task.state = TaskState.running  # remediation required; owner reconciles
        elif stage == "quality" and verdict == "APPROVED":
            # Backend + reviews complete; orchestrator acceptance is separate.
            task.state = TaskState.verifying
        elif stage == "quality":
            task.state = TaskState.running
        return self._mutate(task, event=f"review:{stage}:{verdict}")

    def accept(
        self,
        task_id: str,
        *,
        by: str,
        expected_task_version: int | None = None,
        expected_attempt_epoch: int | None = None,
    ) -> DelegatedTask:
        task = self._require(task_id)
        self._check_version(task, expected_task_version)
        if by != ORCHESTRATOR_ID:
            raise ActivationError("only the orchestrator accepts results")
        # Duplicate ACCEPT is idempotent.
        if task.state is TaskState.completed and task.accepted:
            return task
        if task.state is not TaskState.verifying:
            raise ActivationError(f"task {task_id} is {task.state}; only verifying tasks are accepted")
        current_epoch = task.lease.attempt_epoch if task.lease is not None else None
        if expected_attempt_epoch is not None and current_epoch != expected_attempt_epoch:
            raise ActivationError("stale_attempt_report: attempt_epoch mismatch")
        released = task.lease.execution_released_at if task.lease is not None else None
        if not released:
            if not task.lease or not task.lease.is_active:
                raise ActivationError("lease is inactive; abort and reconcile before acceptance")
        spec_ok = any(
            r.stage == "spec"
            and r.verdict == "PASS"
            and r.candidate_revision == task.candidate_revision
            for r in task.reviews
        )
        quality_ok = any(
            r.stage == "quality"
            and r.verdict == "APPROVED"
            and r.candidate_revision == task.candidate_revision
            for r in task.reviews
        )
        if not (spec_ok and quality_ok):
            raise ActivationError("acceptance requires spec PASS and quality APPROVED")
        task.accepted = True
        task.state = TaskState.completed
        return self._mutate(task, event="accepted")

    def cancel(self, task_id: str, *, by: str, reason: str, expected_task_version: int | None = None) -> DelegatedTask:
        task = self._require(task_id)
        self._check_version(task, expected_task_version)
        if by != ORCHESTRATOR_ID:
            raise ActivationError("only the orchestrator may cancel a task")
        if task.state in {TaskState.completed, TaskState.cancelled}:
            return task
        if task.lease is not None:
            lease_dict = asdict(task.lease)
            lease_dict["revoked"] = True
            task.lease = Lease(**{k: v for k, v in lease_dict.items()})
        task.state = TaskState.cancelled
        task.last_event = f"cancelled: {reason}"
        return self._mutate(task, event="cancelled")

    def lease_renew(
        self,
        task_id: str,
        *,
        duration: int | float | timedelta,
        expected_attempt_epoch: int,
        by: str,
        expected_task_version: int | None = None,
    ) -> DelegatedTask:
        """Bounded renew of a still-held execution lease. Never during review."""
        task = self._require(task_id)
        self._check_version(task, expected_task_version)
        if by != task.assignee_profile:
            raise ActivationError("only the assigned specialist may renew an execution lease")
        if task.lease is None:
            raise ActivationError("fail-closed: lease is not held")
        if task.lease.revoked:
            raise ActivationError("execution authority revoked; worker action refused")
        if task.lease.execution_released_at is not None:
            raise ActivationError("stale_attempt_report: execution already released")
        if task.state in {TaskState.verifying, TaskState.spec_review, TaskState.quality_review}:
            raise ActivationError("lease_renew refused during review")
        life = lease_lifecycle.extract_from_delegation(task)
        renewed = lease_lifecycle.lease_renew(life, duration, expected_attempt_epoch)
        expires = renewed.expires_at.isoformat() if isinstance(renewed.expires_at, datetime) else str(renewed.expires_at)
        task.lease = _replace_lease(task.lease, expires_at=expires)
        return self._mutate(task, event="lease_renewed")

    def expire_lease(self, task_id: str) -> DelegatedTask:
        task = self._require(task_id)
        if task.lease is not None:
            lease_dict = asdict(task.lease)
            lease_dict["revoked"] = True
            task.lease = Lease(**{k: v for k, v in lease_dict.items()})
        if task.state is TaskState.running:
            task.state = TaskState.blocked  # suspect; never silent success
            task.last_event = "lease_expired"
            self._tasks[task.task_id] = task
            self._append(task)
        return task


def _require_bool(value: Any, *, field: str) -> bool:
    """Fail closed on non-bool authority flags; never bool()-coerce."""
    if isinstance(value, bool):
        return value
    raise ActivationError(
        f"{field} must be a boolean, got {type(value).__name__}"
    )


def _task_from_dict(data: dict[str, Any]) -> DelegatedTask:
    lease = None
    if data.get("lease"):
        lease_data = data["lease"]
        lease = Lease(
            holder=lease_data["holder"],
            epoch=int(lease_data["epoch"]),
            issued_at=lease_data["issued_at"],
            expires_at=lease_data["expires_at"],
            revoked=_require_bool(lease_data.get("revoked", False), field="lease.revoked"),
            attempt_epoch=_optional_attempt_epoch(lease_data),
            execution_released_at=_optional_released_at(lease_data),
        )
    reviews = [Review(**r) for r in data.get("reviews") or []]
    return DelegatedTask(
        task_id=data["task_id"],
        contract_revision=data["contract_revision"],
        root_task_id=data["root_task_id"],
        correlation_id=data["correlation_id"],
        assignee_profile=data["assignee_profile"],
        required_capability=data["required_capability"],
        intent=data["intent"],
        acceptance_criteria=list(data.get("acceptance_criteria") or []),
        contract=dict(data.get("contract") or {}),
        state=TaskState(data["state"]),
        version=int(data["version"]),
        assignment_epoch=int(data["assignment_epoch"]),
        candidate_revision=data.get("candidate_revision"),
        lease=lease,
        reviews=reviews,
        accepted=_require_bool(data.get("accepted", False), field="accepted"),
        last_event=data.get("last_event", ""),
        idempotency_key=data.get("idempotency_key"),
        budget_reserved=float(data.get("budget_reserved", 0.0)),
        budget_consumed=float(data.get("budget_consumed", 0.0)),
        created_at=data.get("created_at", ""),
        updated_at=data.get("updated_at", ""),
    )


def drain_and_rollback_requirements() -> dict[str, str]:
    """Compatible drain/rollback for the TaskStore lifecycle.

    DelegationStore remains the legacy protocol store for its own tests. It is
    not an independent writer for the authoritative TaskStore lifecycle.
    Projections are derived from TaskStore; a projection failure must not change
    acceptance. This bootstrap does not migrate live ledgers.
    """
    return {
        "authority": "TaskStore tasks.jsonl",
        "legacy_store": "DelegationStore is not a writer for the TaskStore lifecycle",
        "drain": (
            "Stop new admission. Do not automatically relaunch a reserved, "
            "spawn-uncertain, or handle-uncertain attempt. Cancel only after "
            "confirmed process-group termination; uncertain cleanup quarantines "
            "the workspace and blocks reuse."
        ),
        "rollback": (
            "Revert the code change. Do not rewrite tasks.jsonl. Regenerate "
            "projections from TaskStore. Do not migrate or truncate live ledgers."
        ),
        "projection_failure": "A projection write failure must not change TaskStore acceptance.",
    }
