"""Durable append-only task store.

Persistence is an append-only JSONL event log (the repository's established
ledger convention, cf. contracts/ledgers/*.schema.json), not chat or in-memory
state. Current task state is a left fold over the log, so the control plane
survives process restarts and every transition remains auditable.
"""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from app.adaptive_specialist import EvidenceShape
from app.models import (
    Approval,
    Delegation,
    Retry,
    ReviewRecord,
    TaskContract,
    Verification,
    utcnow,
)

DEFAULT_LEDGER = Path("workspace/orchestrator/tasks.jsonl")

# Event/schema version for control-plane records written to the ledger.
LEDGER_SCHEMA_VERSION = "1.0"

# Durable control events (no embedded task). Replay folds these to reconstruct
# operator stop/resume state before dispatch is enabled at startup.
BREAKER_TRIP_EVENT = "circuit_breaker_tripped"
BREAKER_RESET_EVENT = "circuit_breaker_reset"


class TaskVersionConflict(RuntimeError):
    """A TaskStore put presented a stale snapshot over a newer task version."""

    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(f"task version conflict: expected {expected}, got {actual}")
        self.expected = expected
        self.actual = actual


class ReplayError(RuntimeError):
    """The persisted fold is malformed; authority-sensitive writes must fail closed."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.replay_error = message


def _coerce_preflight_evidence(
    raw: Any,
) -> tuple["EvidenceShape", dict[int, list[dict[str, Any]]] | None]:
    """Rehydrate ``preflight_evidence`` to the keyed-per-epoch shape.

    Returns a ``(shape, dict | None)`` tuple where ``shape`` is one of:

    *   ``EvidenceShape.VALID`` — a per-epoch shape with one or more
        buckets, each a list of well-formed envelopes.
    *   ``EvidenceShape.LEGACY_ABSENT`` — ``None`` or an empty dict, the
        canonical "no preflight has ever run" signal. Allowed only for
        tasks that were admitted before the B2 contract was merged; new
        governed-dispatch tasks carrying this shape are blocked at the
        dispatcher gate, not here.
    *   ``EvidenceShape.INVALID`` — a present value that is not a
        well-formed per-epoch shape. The dispatcher maps this to a
        typed ``PolicyViolation("invalid_preflight_evidence", ...)``.

    Each envelope is validated: ``attempt_epoch`` keys must be integers
    (the JSON round-trip coerces string keys back to ``int``), the value
    under each key must be a list, and each entry must be a dict. A
    malformed envelope raises ``ValueError`` so the caller can surface a
    typed blocker. ``bundle_digest``/``evidence_digest`` are required
    fields on every envelope; a missing or empty digest fails closed.
    """
    if raw is None:
        return EvidenceShape.LEGACY_ABSENT, None
    if not isinstance(raw, dict):
        return EvidenceShape.INVALID, None
    if not raw:
        # An empty dict is the legacy-absent shape; the canonical
        # ``None`` and the empty-dict form are both treated as
        # legacy-absent at the rehydrate boundary so a pre-B2 record
        # remains discoverable.
        return EvidenceShape.LEGACY_ABSENT, None
    out: dict[int, list[dict[str, Any]]] = {}
    for key, value in raw.items():
        # Modern per-epoch shape: int key (or numeric string from JSON),
        # value is a list of evidence dicts.
        try:
            epoch = int(key)
        except (TypeError, ValueError):
            return EvidenceShape.INVALID, None
        if not isinstance(value, list):
            return EvidenceShape.INVALID, None
        normalised: list[dict[str, Any]] = []
        for entry in value:
            if not isinstance(entry, dict):
                return EvidenceShape.INVALID, None
            # B2 §3.2: the envelope's ``attempt_epoch`` must agree with
            # the bucket key. Mismatches are ambiguous and fail closed.
            envelope_epoch = entry.get("attempt_epoch")
            if envelope_epoch is not None:
                try:
                    envelope_epoch_int = int(envelope_epoch)
                except (TypeError, ValueError):
                    return EvidenceShape.INVALID, None
                if envelope_epoch_int != epoch:
                    return EvidenceShape.INVALID, None
            # B2 §3.2: ``bundle_digest`` and ``evidence_digest`` are
            # required on every envelope. Their absence is not a
            # silent migration opportunity.
            for required_digest in ("bundle_digest", "evidence_digest"):
                digest = entry.get(required_digest)
                if not isinstance(digest, str) or len(digest) < 8:
                    return EvidenceShape.INVALID, None
            normalised.append(entry)
        out[epoch] = normalised
    return EvidenceShape.VALID, out


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _require_dt(value: str | None) -> datetime:
    parsed = _parse_dt(value)
    if parsed is None:
        raise ValueError("expected a timestamp in the task ledger, found none")
    return parsed


def _optional_attempt_epoch(raw: dict[str, Any]) -> int | None:
    if "attempt_epoch" not in raw:
        return None
    value = raw["attempt_epoch"]
    if value is None:
        return None
    if type(value) is not int:
        raise ValueError(f"attempt_epoch must be an integer, got {type(value).__name__}")
    if value < 0:
        raise ValueError("attempt_epoch must be non-negative")
    return value


def _optional_released_at(raw: dict[str, Any]) -> datetime | None:
    if "execution_released_at" not in raw:
        return None
    value = raw["execution_released_at"]
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("execution_released_at must be an ISO timestamp string")
    parsed = _parse_dt(value)
    if parsed is None:
        raise ValueError("execution_released_at must be an ISO timestamp")
    if parsed.tzinfo is None:
        raise ValueError("execution_released_at must be timezone-aware")
    return parsed


def _optional_execution_revoked(raw: dict[str, Any]) -> bool:
    if "execution_revoked" not in raw:
        return False
    value = raw["execution_revoked"]
    if isinstance(value, bool):
        return value
    raise ValueError(f"execution_revoked must be a boolean, got {type(value).__name__}")


class TaskStore:
    """Append-only JSONL task ledger with a replayed in-process index."""

    def __init__(self, ledger_path: Path | str = DEFAULT_LEDGER) -> None:
        self.ledger_path = Path(ledger_path)
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self._tasks: dict[str, TaskContract] = {}
        self._by_idempotency: dict[str, str] = {}
        # Durable operator stop/resume events, in ledger order.
        self.breaker_events: list[dict[str, Any]] = []
        # Durable quarantine set, rebuilt by replay from "quarantine" control events
        # (F-1): a quarantined task stays blocked across restarts until an operator
        # manually reconciles — there is deliberately no automatic unquarantine and
        # no release API here.
        self.quarantined_tasks: set[str] = set()
        # Set when replay hits a malformed/truncated record. The control plane
        # must fail closed (block dispatch) when this is non-None.
        self.replay_error: str | None = None
        self._lock_depth = 0
        self._sync = os.fsync
        if self.ledger_path.exists():
            self.replay()

    # ------------------------------------------------------------ persistence

    def _append(self, event: str, task: TaskContract) -> None:
        record = {"event": event, "recorded_at": utcnow().isoformat(), "task": task.to_dict()}
        line = json.dumps(record, sort_keys=True, default=str) + "\n"
        # Append durably: write then fsync so a crash cannot lose an accepted
        # transition that the caller has already been told about. On write/fsync
        # failure truncate back so disk matches the pre-call fold.
        with self.ledger_path.open("a+", encoding="utf-8") as fh:
            fh.seek(0, os.SEEK_END)
            start = fh.tell()
            try:
                fh.write(line)
                fh.flush()
                self._sync(fh.fileno())
            except Exception:
                fh.seek(start)
                fh.truncate(start)
                fh.flush()
                raise

    def append_control_event(self, event: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Durably record a control-plane event (no embedded task).

        Returns the persisted record. Raises if the durable write fails; the
        caller must NOT acknowledge the operation in that case.
        """
        record = {
            "event": event,
            "recorded_at": utcnow().isoformat(),
            "schema_version": LEDGER_SCHEMA_VERSION,
            **payload,
        }
        quarantine_task_id: str | None = None
        if event == "quarantine":
            candidate = payload.get("task_id")
            if not isinstance(candidate, str) or not candidate:
                raise ValueError(
                    "quarantine control event requires a non-empty string task_id"
                )
            quarantine_task_id = candidate
            # D2 repair: apply the in-memory effect BEFORE the durable write so
            # this process denies work for the task from this moment on — even
            # if the write below fails (the raised error surfaces the failure
            # and the caller must not acknowledge).
            self.quarantined_tasks.add(quarantine_task_id)
        line = json.dumps(record, sort_keys=True, default=str)
        with self.ledger_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            self._sync(fh.fileno())
        # D2 repair: quarantine records are NOT breaker events.
        if quarantine_task_id is None:
            self.breaker_events.append(record)
        return record

    def latest_breaker_event(self) -> dict[str, Any] | None:
        return self.breaker_events[-1] if self.breaker_events else None

    def is_quarantined(self, task_id: str | UUID) -> bool:
        """True when a durable ``quarantine`` control event names this task.

        Quarantine is authoritative across restarts (rebuilt by ``replay``) and is
        strictly stronger than task state: a quarantined task is denied admission and
        dispatch regardless of any later state fold. Release is manual/operator-only.
        """
        return str(task_id) in self.quarantined_tasks

    def replay(self) -> None:
        """Rebuild current state by folding the append-only log.

        A malformed or truncated record stops replay and sets ``replay_error``;
        the control plane is responsible for failing closed (blocking dispatch)
        when that happens. A restart must never silently reopen dispatch.
        Lock-aware: nested callers already holding the exclusive ledger lock
        do not re-acquire it.
        """
        with self._exclusive_lock():
            self._replay_unlocked()

    def _replay_unlocked(self) -> None:
        """Fold persisted JSONL into memory. Caller must hold the exclusive lock."""
        self._tasks.clear()
        self._by_idempotency.clear()
        self.breaker_events.clear()
        self.quarantined_tasks.clear()
        self.replay_error = None
        with self.ledger_path.open("r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    self.replay_error = f"line {lineno}: invalid JSON: {exc}"
                    return
                if "task" in record:
                    try:
                        task = self._rehydrate(record["task"])
                    except (KeyError, ValueError, TypeError) as exc:
                        self.replay_error = f"line {lineno}: malformed task record: {exc}"
                        return
                    self._tasks[str(task.task_id)] = task
                    self._by_idempotency[task.idempotency_key] = str(task.task_id)
                elif record.get("event") in (BREAKER_TRIP_EVENT, BREAKER_RESET_EVENT):
                    self.breaker_events.append(record)
                elif record.get("event") == "quarantine":
                    # F-1 corrected: quarantine control events fold into the durable
                    # quarantine set so a quarantined task stays blocked across
                    # restarts. D3 repair: a malformed quarantine record (missing,
                    # null, empty, or non-string task_id) sets replay_error and
                    # aborts the fold — fail closed. Both admission and dispatch
                    # deny whenever replay_error is set.
                    quarantine_task_id = record.get("task_id")
                    if not isinstance(quarantine_task_id, str) or not quarantine_task_id:
                        self.replay_error = (
                            f"line {lineno}: malformed quarantine record:"
                            " task_id must be a non-empty string"
                        )
                        return
                    self.quarantined_tasks.add(quarantine_task_id)
                # Unknown records are ignored for forward compatibility.

    @staticmethod
    def _rehydrate(data: dict[str, Any]) -> TaskContract:
        approval_raw = data.get("approval") or {}
        claim_raw = data.get("claim")
        task = TaskContract(
            title=data["title"],
            intent=data["intent"],
            assignee_profile=data["assignee_profile"],
            required_capability=data["required_capability"],
            risk_tier=data["risk_tier"],
            idempotency_key=data["idempotency_key"],
            acceptance_criteria=list(data["acceptance_criteria"]),
            verification=Verification(**data["verification"]),
            timeout_s=data["timeout_s"],
            correlation_id=UUID(data["correlation_id"]),
            retry=Retry(**data["retry"]),
            approval=Approval(
                required=approval_raw.get("required", False),
                state=approval_raw.get("state", "not_required"),
                approved_by=approval_raw.get("approved_by"),
                approved_at=_parse_dt(approval_raw.get("approved_at")),
                approval_manifest_id=approval_raw.get("approval_manifest_id"),
            ),
            reviewer_profile=data.get("reviewer_profile"),
            depends_on=[UUID(d) for d in data.get("depends_on", [])],
            irreversible_operations=list(data.get("irreversible_operations", [])),
            labels=list(data.get("labels", [])),
            parent_task_id=UUID(data["parent_task_id"]) if data.get("parent_task_id") else None,
            traceparent=data.get("traceparent"),
            task_id=UUID(data["task_id"]),
        )
        task.state = data["state"]
        task.handoff = data.get("handoff")
        task.created_at = _require_dt(data["created_at"])
        task.created_by = data["created_by"]
        task.updated_at = _parse_dt(data.get("updated_at"))
        task.terminal_reason = data.get("terminal_reason")
        # Phase B / remediation B1: ``preflight_evidence`` is keyed per
        # ``attempt_epoch``. JSON serialises int keys as strings, so we
        # coerce back to int on rehydrate. A legacy single-dict record
        # (no attempt_epoch split) is treated as a flat envelope and
        # recorded under the sentinel epoch ``0`` to keep it discoverable
        # without losing replay fidelity. ``None`` (or a missing key)
        # remains the canonical "no preflight has ever run" signal.
        # B2 (contract §3.2): ``_coerce_preflight_evidence`` returns a
        # typed ``(EvidenceShape, dict | None)`` verdict. ``LEGACY_ABSENT``
        # is preserved as ``None`` so a pre-B2 record remains
        # discoverable; ``INVALID`` raises so a present-but-broken
        # envelope cannot silently be reinterpreted as legacy absence.
        raw_evidence = data.get("preflight_evidence")
        evidence_shape, evidence_dict = _coerce_preflight_evidence(raw_evidence)
        if evidence_shape is EvidenceShape.INVALID:
            raise ReplayError(
                "malformed preflight_evidence (legacy_absent is not a "
                "silent substitute for a present invalid envelope)"
            )
        task.preflight_evidence = evidence_dict
        raw_version = data.get("task_version", 0)
        if raw_version is None:
            raw_version = 0
        if type(raw_version) is not int:
            raise ValueError(f"task_version must be an integer, got {type(raw_version).__name__}")
        if raw_version < 0:
            raise ValueError("task_version must be non-negative")
        task.task_version = raw_version
        if claim_raw:
            task.claim = {
                "claimed_by": claim_raw["claimed_by"],
                "claimed_at": _parse_dt(claim_raw["claimed_at"]),
                "lease_expires_at": _parse_dt(claim_raw["lease_expires_at"]),
            }
        del_raw = data.get("delegation") or {}
        task.delegation = Delegation(
            assignment_epoch=int(del_raw.get("assignment_epoch", 0)),
            attempt=int(del_raw.get("attempt", 0)),
            runtime_profile=del_raw.get("runtime_profile"),
            candidate_revision=del_raw.get("candidate_revision"),
            candidate_digest=del_raw.get("candidate_digest"),
            granted_candidate_revision=del_raw.get("granted_candidate_revision"),
            granted_candidate_digest=del_raw.get("granted_candidate_digest"),
            contract_revision=del_raw.get("contract_revision"),
            baseline=del_raw.get("baseline"),
            accepted_by=del_raw.get("accepted_by"),
            accepted_at=_parse_dt(del_raw.get("accepted_at")),
            request_limit=del_raw.get("request_limit"),
            requests_used=int(del_raw.get("requests_used", 0)),
            budget_tokens=del_raw.get("budget_tokens"),
            tokens_used=del_raw.get("tokens_used"),
            approved_provider=del_raw.get("approved_provider"),
            approved_model=del_raw.get("approved_model"),
            launch_reservation=del_raw.get("launch_reservation"),
            attempt_epoch=_optional_attempt_epoch(del_raw),
            execution_released_at=_optional_released_at(del_raw),
            execution_revoked=_optional_execution_revoked(del_raw),
            reviews=[
                ReviewRecord(
                    stage=rev["stage"],
                    verdict=rev["verdict"],
                    reviewer=rev["reviewer"],
                    candidate_revision=rev["candidate_revision"],
                    findings=list(rev.get("findings", [])),
                    at=_require_dt(rev.get("at")),
                )
                for rev in del_raw.get("reviews", [])
            ],
        )
        return task

    @contextmanager
    def _exclusive_lock(self):
        if self._lock_depth:
            yield
            return
        lock_path = self.ledger_path.with_name(self.ledger_path.name + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = lock_path.open("a+")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        self._lock_depth += 1
        try:
            yield
        finally:
            self._lock_depth -= 1
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            fh.close()

    def reserve_launch(self, task: TaskContract, reservation: dict[str, Any]) -> tuple[TaskContract, bool]:
        """Durably record admission before spawn. Returns (task, created_now).

        A second caller, including after replay, does not create another
        reservation and must not spawn.
        """
        with self._exclusive_lock():
            if self.ledger_path.exists():
                self.replay()
            current = deepcopy(self._tasks.get(str(task.task_id)) or task)
            existing = current.delegation.launch_reservation
            if existing:
                return current, False
            if current.state == "proposed":
                current.transition_to("approved")
            if current.state == "approved":
                current.transition_to("queued")
            if current.state == "queued":
                current.transition_to("claimed")
                if current.claim is None:
                    current.apply_claim(reservation.get("runtime_profile") or current.assignee_profile)
            if current.state == "claimed":
                current.transition_to("running")
            current.delegation.assignment_epoch += 1
            current.delegation.attempt = int(reservation.get("attempt") or 1)
            current.delegation.runtime_profile = reservation.get("runtime_profile")
            current.delegation.approved_provider = reservation.get("approved_provider")
            current.delegation.approved_model = reservation.get("approved_model")
            current.delegation.launch_reservation = dict(reservation)
            current.delegation.launch_reservation["epoch"] = current.delegation.assignment_epoch
            current.delegation.launch_reservation["state"] = "reserved"
            current.delegation.launch_reservation["workspace_reusable"] = True
            return self.put(current, event="launch_reserved"), True

    # ----------------------------------------------------------------- access

    def put(self, task: TaskContract, event: str = "task_updated", *, expected_task_version: int | None = None) -> TaskContract:
        with self._exclusive_lock():
            if self.ledger_path.exists():
                self._replay_unlocked()
            if self.replay_error:
                raise ReplayError(self.replay_error)
            if event == "task_proposed":
                existing_id = self._by_idempotency.get(task.idempotency_key)
                if existing_id is not None:
                    return deepcopy(self._tasks[existing_id])
            tid = str(task.task_id)
            current = self._tasks.get(tid)
            incoming = int(getattr(task, "task_version", 0) or 0)
            try:
                if current is not None:
                    observed = int(getattr(current, "task_version", 0) or 0)
                    if expected_task_version is not None and expected_task_version != observed:
                        raise TaskVersionConflict(expected_task_version, observed)
                    if incoming != observed:
                        raise TaskVersionConflict(incoming, observed)
                    next_version = observed + 1
                else:
                    if expected_task_version is not None and expected_task_version != incoming:
                        raise TaskVersionConflict(expected_task_version, incoming)
                    next_version = incoming + 1
            except TaskVersionConflict:
                raise
            committed = deepcopy(task)
            committed.task_version = next_version
            try:
                self._append(event, committed)
            except Exception:
                if self.ledger_path.exists():
                    self._replay_unlocked()
                raise
            self._tasks[tid] = deepcopy(committed)
            self._by_idempotency[committed.idempotency_key] = tid
            task.task_version = next_version
            return deepcopy(committed)

    def get(self, task_id: str | UUID) -> TaskContract | None:
        task = self._tasks.get(str(task_id))
        return deepcopy(task) if task is not None else None

    def find_by_idempotency_key(self, key: str) -> TaskContract | None:
        task_id = self._by_idempotency.get(key)
        task = self._tasks.get(task_id) if task_id else None
        return deepcopy(task) if task is not None else None

    def all_tasks(self) -> dict[str, TaskContract]:
        return {tid: deepcopy(task) for tid, task in self._tasks.items()}

    def list_by_state(self, state: str) -> list[TaskContract]:
        return [deepcopy(t) for t in self._tasks.values() if t.state == state]

    def list_by_correlation(self, correlation_id: str | UUID) -> list[TaskContract]:
        return [
            deepcopy(t)
            for t in self._tasks.values()
            if str(t.correlation_id) == str(correlation_id)
        ]
