"""Bounded execution-lease lifecycle, extracted from DelegationStore.

DelegationStore.Lease remains the on-disk source of truth. This module
formalizes the execution-ownership state machine and the monotonic
attempt_epoch used to detect stale-attempt replays. Existing
DelegationStore method bodies are unchanged; callers opt in through
extract_from_delegation / start_running / lease_renew / release_lease.

Design: LEASE-LIFECYCLE-DESIGN.md (inspected 2026-10-11). Bounded
renewal is the 2026-10-11 operator-approved exception to design §6.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any


class LeaseState(StrEnum):
    requested = "requested"
    granted = "granted"
    held = "held"
    released = "released"
    expired = "expired"
    revoked = "revoked"


# Fail-closed table. held -> held is not a transition; start_running while
# already held bumps attempt_epoch in place (one active attempt, new epoch).
ALLOWED_TRANSITIONS: dict[LeaseState, frozenset[LeaseState]] = {
    LeaseState.requested: frozenset({LeaseState.granted, LeaseState.revoked}),
    LeaseState.granted: frozenset({LeaseState.held, LeaseState.expired, LeaseState.revoked}),
    LeaseState.held: frozenset({LeaseState.released, LeaseState.expired, LeaseState.revoked}),
    # released -> held is the explicit new-attempt path (new epoch), never implicit.
    LeaseState.released: frozenset({LeaseState.held, LeaseState.expired, LeaseState.revoked}),
    LeaseState.expired: frozenset({LeaseState.revoked}),
    LeaseState.revoked: frozenset(),
}


class LeaseTransitionError(RuntimeError):
    """A lease-state change was refused (fail-closed)."""


@dataclass(frozen=True)
class Lease:
    holder: str
    issued_at: datetime
    expires_at: datetime
    state: LeaseState = LeaseState.requested
    attempt_epoch: int = 0
    execution_released_at: datetime | None = None
    task_id: str = ""
    revoked: bool = False

    @property
    def is_active(self) -> bool:
        return (
            self.state is LeaseState.held
            and not self.revoked
            and self.expires_at > _now()
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _duration_seconds(duration: int | float | timedelta) -> float:
    if isinstance(duration, timedelta):
        seconds = duration.total_seconds()
    else:
        seconds = float(duration)
    if seconds <= 0:
        raise LeaseTransitionError("renewal duration must be positive")
    return seconds


def _transition(lease: Lease, new_state: LeaseState) -> Lease:
    allowed = ALLOWED_TRANSITIONS[lease.state]
    if new_state not in allowed:
        raise LeaseTransitionError(
            f"fail-closed: {lease.state} -> {new_state} is not permitted"
        )
    released_at = lease.execution_released_at
    if lease.state is LeaseState.held and new_state is not LeaseState.held:
        released_at = released_at or _now()
    return replace(
        lease,
        state=new_state,
        execution_released_at=released_at,
        revoked=True if new_state is LeaseState.revoked else lease.revoked,
    )


def _persist(store: Any, lease: Lease) -> None:
    """Pass attempt_epoch through the DelegationStore JSONL write path.

    Extra keys are ignored by DelegationStore._replay (which reads record['task']).
    Replays are detectable because each start_running appends the epoch.
    """
    if store is None:
        return
    task = store.get(lease.task_id) if lease.task_id else None
    if task is None:
        raise LeaseTransitionError(
            "fail-closed: cannot persist lease without a task on the ledger"
        )
    record = {
        "task": asdict(task),
        "attempt_epoch": lease.attempt_epoch,
        "lease_lifecycle": {
            "task_id": lease.task_id,
            "state": str(lease.state),
            "attempt_epoch": lease.attempt_epoch,
            "holder": lease.holder,
            "issued_at": lease.issued_at.isoformat(),
            "expires_at": lease.expires_at.isoformat(),
            "execution_released_at": (
                None
                if lease.execution_released_at is None
                else lease.execution_released_at.isoformat()
            ),
            "revoked": lease.revoked,
        },
    }
    with store.ledger_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def extract_from_delegation(task: Any) -> Lease:
    """Project a DelegationStore task/lease into the lifecycle Lease.

    Preserves holder, issued_at, expires_at, revoked, and is_active
    equivalence. Does not mutate the store.
    """
    src = getattr(task, "lease", None)
    task_id = str(getattr(task, "task_id", "") or "")
    if src is None:
        now = _now()
        return Lease(
            holder=str(getattr(task, "assignee_profile", "") or ""),
            issued_at=now,
            expires_at=now,
            state=LeaseState.requested,
            attempt_epoch=0,
            task_id=task_id,
        )
    issued_at = _parse_dt(src.issued_at)
    expires_at = _parse_dt(src.expires_at)
    now = _now()
    if src.revoked:
        state = LeaseState.revoked
        released_at = issued_at
        epoch = 0
    elif expires_at <= now:
        state = LeaseState.expired
        released_at = expires_at
        epoch = 0
    else:
        state = LeaseState.held
        released_at = None
        epoch = 1
    return Lease(
        holder=str(src.holder),
        issued_at=issued_at,
        expires_at=expires_at,
        state=state,
        attempt_epoch=epoch,
        execution_released_at=released_at,
        task_id=task_id,
        revoked=bool(src.revoked),
    )


def grant_lease(lease: Lease) -> Lease:
    return _transition(lease, LeaseState.granted)


def start_running(
    lease: Lease,
    *,
    duration: int | float | timedelta = 1800,
    store: Any = None,
) -> Lease:
    """Enter (or re-enter) held and increment attempt_epoch.

    Refuses expired/revoked (I1). A held lease stays held with a new epoch
    so only one active attempt exists (I2). Persists attempt_epoch when a
    DelegationStore is supplied.
    """
    seconds = _duration_seconds(duration)
    now = _now()
    if lease.state is LeaseState.held:
        next_lease = replace(
            lease,
            attempt_epoch=lease.attempt_epoch + 1,
            issued_at=now,
            expires_at=now + timedelta(seconds=seconds),
            execution_released_at=None,
        )
        _persist(store, next_lease)
        return next_lease
    moved = _transition(lease, LeaseState.held)
    next_lease = replace(
        moved,
        attempt_epoch=lease.attempt_epoch + 1,
        issued_at=now,
        expires_at=now + timedelta(seconds=seconds),
        execution_released_at=None,
        revoked=False,
    )
    _persist(store, next_lease)
    return next_lease


def release_lease(lease: Lease, *, store: Any = None) -> Lease:
    """One-way exit from held. Sets execution_released_at."""
    moved = _transition(lease, LeaseState.released)
    _persist(store, moved)
    return moved


def expire_lease(lease: Lease, *, store: Any = None) -> Lease:
    moved = _transition(lease, LeaseState.expired)
    _persist(store, moved)
    return moved


def revoke_lease(lease: Lease, *, store: Any = None) -> Lease:
    moved = _transition(lease, LeaseState.revoked)
    _persist(store, moved)
    return moved


def lease_renew(
    lease: Lease,
    duration: int | float | timedelta,
    expected_attempt_epoch: int,
) -> Lease:
    """Bounded renew. Fail-closed on epoch mismatch or non-held state."""
    if lease.attempt_epoch != expected_attempt_epoch:
        raise LeaseTransitionError(
            f"fail-closed: attempt_epoch mismatch "
            f"(have {lease.attempt_epoch}, expected {expected_attempt_epoch})"
        )
    if lease.state is not LeaseState.held:
        raise LeaseTransitionError(
            f"fail-closed: lease is {lease.state}, not held"
        )
    seconds = _duration_seconds(duration)
    now = _now()
    return replace(lease, expires_at=now + timedelta(seconds=seconds))


def load_attempt_epoch(store: Any, task_id: str) -> int | None:
    """Last written attempt_epoch for task_id, or None if never persisted."""
    path = store.ledger_path
    if not path.exists():
        return None
    last: int | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        epoch = record.get("attempt_epoch")
        life = record.get("lease_lifecycle") or {}
        if epoch is None:
            epoch = life.get("attempt_epoch")
        tid = (record.get("task") or {}).get("task_id") or life.get("task_id")
        if tid == task_id and epoch is not None:
            last = int(epoch)
    return last
