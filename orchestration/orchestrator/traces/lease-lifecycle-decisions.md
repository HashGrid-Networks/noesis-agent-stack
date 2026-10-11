# Lease-lifecycle decisions (append-only)

NOESIS-LOOP-GRAPH-V1 · 2026-10-11. Provenance: operator-approved bounded
implementation (`LEASE-LIFECYCLE-DESIGN.md`, `LEASE-LIFECYCLE-APPROVAL-BRIEF.md`,
handoff `lease_lifecycle_implementation_handoff_2026-10-11.md`).
DelegationStore.Lease remains the on-disk source of truth. This file records
each new code path; it is not a review verdict.

## extract_from_delegation

Design: LEASE-LIFECYCLE-DESIGN.md §1 (Delegation-store Lease) and §5
(additive field; no schema break).
Path: `app/lease_lifecycle.py:extract_from_delegation`
Decision: project the existing `delegation.Lease` into the lifecycle Lease
without mutating the store. Active leases map to `held` with `attempt_epoch=1`
(the in-flight start_running). Missing lease maps to `requested` / epoch 0.
Revoked maps to `revoked`; wall-clock-past `expires_at` maps to `expired`.
Holder, issued_at, expires_at, revoked, and `is_active` equivalence are
preserved so extract-from-delegation cannot change existing DelegationStore
behavior.

## grant_lease / ALLOWED_TRANSITIONS

Design: LEASE-LIFECYCLE-DESIGN.md §3 (smallest change consistent with the
existing state machine) and the handoff state sequence
`requested → granted → held → released → expired → revoked`.
Path: `app/lease_lifecycle.py:_transition`, `grant_lease`
Decision: every transition is fail-closed against `ALLOWED_TRANSITIONS`.
Unknown edges raise `LeaseTransitionError`. `revoked` is terminal. Expired
authority cannot return to `held` except that `released → held` is permitted
only as the explicit new-attempt path in `start_running` (new epoch).

## start_running

Design: LEASE-LIFECYCLE-DESIGN.md §3.1–§3.2, invariants I1 and I2.
Path: `app/lease_lifecycle.py:start_running`
Decision: every `start_running` increments the per-task `attempt_epoch`.
A held lease stays held (one active attempt) with a bumped epoch. Granted or
released leases transition to held. Requested / expired / revoked fail closed
(I1: expired authority is never resurrected implicitly). Optional
`store=` writes `attempt_epoch` through the existing JSONL append path as an
extra key so DelegationStore replay (which reads `record["task"]`) is
unchanged and replays are detectable via `load_attempt_epoch`.

## ledger write path (`_persist` / `load_attempt_epoch`)

Design: LEASE-LIFECYCLE-DESIGN.md §5 (additive; replay reconstructs).
Path: `app/lease_lifecycle.py:_persist`, `load_attempt_epoch`
Decision: persist `{task, attempt_epoch, lease_lifecycle}` onto the same
JSONL. Extra keys are ignored by `DelegationStore._replay`. Last-write-wins
epoch per task_id makes a stale-epoch renew detectable against the ledger.

## release_lease / execution_released_at

Design: LEASE-LIFECYCLE-DESIGN.md §3.1 (execution lease released one-way;
`execution_released_at` recorded when leaving held).
Path: `app/lease_lifecycle.py:release_lease`, `_transition`
Decision: any transition out of `held` sets `execution_released_at` if unset.
Release is one-way; `grant_lease` from `released` is refused. Expire and
revoke from held also stamp `execution_released_at` (handoff: set when the
lease transitions out of held).

## expire_lease / revoke_lease

Design: LEASE-LIFECYCLE-DESIGN.md I1, I2; §3.2 stale reports by epoch.
Path: `app/lease_lifecycle.py:expire_lease`, `revoke_lease`
Decision: fail-closed transitions only. Revoke is terminal. Expire cannot
re-enter held; a later attempt must be a new `start_running` from `released`
(not from `expired`/`revoked`).

## lease_renew

Design: LEASE-LIFECYCLE-DESIGN.md §6 rejected “explicit authorized renewal”;
operator 2026-10-11 approval + implementation handoff authorize a *bounded*
`lease_renew(lease, duration, expected_attempt_epoch)` instead.
Path: `app/lease_lifecycle.py:lease_renew`
Decision: renew exists only as this fail-closed helper. Epoch mismatch
refuses (stale-attempt / replay). Non-held state refuses. Positive duration
only. Returns a new frozen Lease; does not write the ledger (renewal is not
an attempt). This is an intentional, documented exception to design §6, not
an implicit resurrection of expired authority (I1 still holds: expired /
revoked cannot renew).

## additive delegation.py import

Design: handoff writable-scope rule; planner-remediation freeze on
`delegation.py` method bodies (`0a60e6f` fail-closed `_require_bool` replay).
Path: `app/delegation.py` import of `lease_lifecycle` + re-export of
`extract_from_delegation` / `lease_renew`
Decision: no existing function body rewritten. Planner F1–F5 behavior is
untouched. Lifecycle callers may import from either module.

## production integration (2026-10-11 remediation)

Design: LEASE-LIFECYCLE-DESIGN.md §3 / I1–I5; Sentinel F1–F5, E1.
Path: `DelegationStore.start_running` / `submit_verifying` / `accept` /
`lease_renew`; `Orchestrator.submit_candidate` / `accept`; `Delegation.attempt_epoch`
and `execution_released_at` via `TaskContract.to_dict` + `TaskStore._rehydrate`.
Decision: persist `attempt_epoch` and `execution_released_at` on the canonical
task/lease snapshot (not an unlocked extra JSONL key). Submit releases
execution one-way and stamps `execution_released_at`. Same-epoch stale
worker submits fail closed. A new candidate without an expected epoch is a
new attempt (epoch++). Acceptance after release does not require wall-clock
lease liveness; old records with absent fields keep the legacy lease gate.
`lease_renew` is version-fenced, persisted via `_mutate`, and refused after
release/revocation or during review. `_require_bool` replay remains.

## r2 authority fence (2026-10-11)

Design: LEASE-LIFECYCLE-DESIGN.md I1–I5; Sentinel r2 R2-F1, R2-F2, R2-N1–N3.
Path: `DelegationStore.grant_new_attempt` / `start_running` / `submit_verifying` /
`consume_budget`; `Orchestrator.grant_new_attempt` / `submit_candidate` /
`observe_budget` / `accept`; `TaskStore.put` compare-and-swap on `task_version`.
Decision: worker actions that depend on persisted execution authority must
present the current `attempt_epoch`; omission fails closed. A different
candidate revision or digest never increments the epoch. The only persisted
transition that mints a new attempt is orchestrator `grant_new_attempt`,
bound to the observed task version and intended assignee/candidate.
`execution_released_at` is one-way: review non-pass and `start_running` cannot
clear it. `TaskStore.put` refuses a stale snapshot over a newer
`task_version`. Timezone-naive `execution_released_at` fails closed on replay.
`operator == "noesis-orchestrator"` remains an in-process caller-label
prerequisite, not authentication. Production must not expose `accept` or
`grant_new_attempt` to untrusted callers.
