# Delegation protections — enforced, mocked, runtime-dependent, not implemented

Durable limitation ledger and protection matrix for the orchestrator–specialist
handoff protocol (`app/delegation.py`, `noesis.delegated-task/v1`). This is the
Subgoal 4 §1 deliverable: which protections are real, where they live, and
where honest limits remain. Last verified against the suite at **334 passed /
0 failed** (`test_delegation_*.py`, `test_subagent_driven_development.py`,
`test_delegation_subgoal4_coverage.py`).

## Classification matrix

| Protection | Class | Enforcement point | Evidence |
|---|---|---|---|
| Profile/role authorization on every transition (only orchestrator assigns/accepts/cancels; only assignee starts/submits/consumes) | **Production code** | `DelegationStore.process` + per-method `by=` checks | `test_delegation_failure_injection.py` |
| Reviewer independence — assignee (any profile) can never review its own work | **Production code** | `record_review`: `reviewer == task.assignee_profile or reviewer == IMPLEMENTER_ID` → refuse | self-review regression tests (noesis-substrate assignee) |
| Optimistic concurrency — `expected_task_version` mismatch refuses the write | **Production code** | `_check_version` on every mutating method | version-conflict tests |
| Assignment-epoch fencing — stale epoch refuses updates | **Production code** | `process` epoch comparison; `start_running` binds lease epoch | stale-epoch tests |
| Idempotent delivery — duplicate ASSIGN never duplicates work; duplicate RESULT/ACCEPT are no-ops | **Production code** | `idempotency_key` map; duplicate-delivery short-circuits | dedup + restart tests |
| Lease expiry — expired/revoked lease blocks start; expiry mid-run marks `blocked`, never silent success | **Production code** | `Lease.is_active`; `expire_lease` | lease tests |
| Spec-before-quality ordering; quality requires spec PASS on the **current** candidate | **Production code** | `record_review` stage gate | review-gate tests |
| Revision binding — candidate change drops reviews bound to older revisions | **Production code** | `submit_verifying` / `record_review` filter | revision-invalidation tests |
| Critical/important findings block a passing verdict | **Production code** | `record_review` severity check | critical-finding tests |
| Explicit acceptance — backend success ≠ completion; only orchestrator `accept` from `verifying` with active lease + both reviews | **Production code** | `accept` gate | acceptance tests |
| Cancellation — lease revoked, durable, idempotent; post-cancel mutations rejected | **Production code** | `cancel` + state checks | cancellation tests |
| Shared-budget reservation — atomic check-and-reserve under `threading.Lock`; exhaustion raises `BudgetExhausted` and persists nothing; consumption ≤ reservation | **Production code** | `_check_budget`, `reserve_budget`, `consume_budget` | `test_delegation_budget.py` (incl. 6-thread race) |
| **Envelope authentication** — HMAC-SHA256 over canonical envelope content; signature binds sender/recipient/ancestry/epoch/payload; checked before any authorization or state change; refusal persists nothing | **Production code** (opt-in) | `HmacEnvelopeAuthenticator`, `DelegationStore(authenticator=...)`, first check in `process` | `test_delegation_authentication.py` (14 tests) |
| **message_id content binding** — reuse of a message id with conflicting content refused (even when validly signed); identical redelivery stays idempotent; only verified messages bind | **Production code** (opt-in, in-process) | `DelegationStore._msg_binding` in `process` | `TestMessageIdBinding` (3 tests) |
| Crash-window recovery — 7 restarts across every lifecycle transition; version monotonic 0→6; exactly one completion | **Production code** | append-only replay in `__post_init__` | crash-window matrix tests |
| Controller restart preserves accepted state, open findings, reservations | **Production code** | ledger replay | reload tests |
| Out-of-order / wrong-assignee / forged-sender messages refused | **Production code** | role + binding + version checks | failure-injection tests |
| Launcher identity attestation (loaded-profile proof) | **Runtime-dependent** | router plugin host + launcher provenance | plugin host tests (mocked host); live canary **authorization-gated** |
| Transport authentication | **Now production code at envelope layer** (opt-in HMAC); wire-level mTLS/channel binding remains **runtime-dependent** | `HmacEnvelopeAuthenticator` | authentication tests |
| Runtime process fencing (kill-by-epoch enforcement in the OS/runtime) | **Not implemented** — compensated, not claimed | none; lease is advisory | see limitations below |
| Exactly-once external execution | **Not implemented** — at-least-once + idempotent reconciliation is the design | none claimed | adapter contract |

## Envelope authentication (new)

- `HandoffEnvelope.signature` is an HMAC-SHA256 hex digest over
  `canonical_envelope_bytes(msg)`: protocol version, message id/type, sender,
  recipient, root/task/attempt ids, contract revision, assignment epoch,
  correlation id, idempotency key, expected version, **and the full payload**.
  Any tampering with identity, ancestry, epoch, or contract content
  invalidates the signature.
- Verification is the **first** operation in `process()` — before protocol,
  role authorization, or state checks — so an unauthenticated envelope is
  refused without persisting anything (`AuthenticationError`, an
  `ActivationError` subclass so existing catch-paths treat it as a refusal,
  not a crash).
- Opt-in: `DelegationStore(ledger_path, authenticator=HmacEnvelopeAuthenticator(key))`.
  Without an authenticator the store keeps its prior in-process behavior
  (tests, trusted local callers). **Key material is injected, never stored in
  the ledger, and never committed to the repo.**
- `apply(msg)` signs a frozen envelope and returns the signed copy.

### Honest limits of envelope authentication

1. **Replay**: a validly signed message could be replayed. Defenses, in
   layers: (a) an in-process `message_id -> content digest` binding refuses
   reuse of a message id with conflicting content (even validly signed); the
   window re-opens on restart, so (b) for ASSIGN the durable defense remains
   idempotency keys + task-existence/epoch checks. Other message types enter
   through authenticated transport in production wiring; the store itself
   keeps no durable replay cache.
2. **Shared key**: HMAC is symmetric. Sender and receiver share key material;
   this proves "came from a holder of the key," not "came from profile X" at
   the cryptographic level. Role binding comes from the envelope fields, which
   the signature protects from forgery. Per-profile asymmetric identity
   (e.g., ed25519 per sender) is a possible future upgrade.
3. **Scope**: authentication covers the `process()` envelope entrypoint. The
   typed transition methods (`mark_ready`, `start_running`, …) remain an
   in-process trusted surface — same trust boundary as before, now explicitly
   documented rather than implied.

## Lease/fencing limitations and compensating controls

- The lease is **advisory persistence fencing**: it gates store transitions
  and marks expired execution `blocked`. It does not kill a running process.
  Compensating controls: expiry → `blocked` (never silent success); conflicting
  retry requires cancellation/reconciliation; uncertain termination quarantines
  the workspace and blocks reuse (`drain_and_rollback_requirements`).
- Real runtime fencing (launcher refuses to act for a stale epoch, OS-level
  process-group kill) requires launcher integration and is **runtime-dependent**
  — not claimed as implemented.

## What remains for runtime integration

| Item | Why gated |
|---|---|
| Launcher-side epoch fencing + identity attestation | requires live launcher plugin + canary (authorization-gated) |
| Wire-level transport security (mTLS / channel binding) | infrastructure change, approval-gated |
| Per-profile asymmetric envelope signatures | design choice; shared-key HMAC is the documented interim |
| Subgoal 5 live specialist cycle; Subgoal 4 live canary | explicit operator authorization required |
