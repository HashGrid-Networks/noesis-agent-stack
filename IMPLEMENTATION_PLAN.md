# F1–F5 Launch-Boundary Remediation — Implementation Plan

## Trace the Real Path

```
TaskStore → assignment → launch → child initialization →
effective configuration → inference request → result persistence
```

1. **TaskStore** — append-only JSONL ledger persisting `TaskContract` with `Delegation` (assignment_epoch, attempt, runtime_profile, candidate_revision/candidate_digest, reviews, acceptance, launch_reservation).

2. **Assignment** — `Orchestrator.propose()` creates a `TaskContract`; `policy.validate_admission()` gates admission; `Orchestrator.route_and_launch()` classifies → discovers/filter → selects specialist → policy admission → explicit Hermes `-p` target → profile attestation.

3. **Launch** — `HermesCliAdapter.launch()` with `dry_run=True` verifies profile without inference; `dry_run=False` performs profile attestation, writes isolated fallback session, proves empty fallback chain, then runs the inference command with `--provider`/`-m`/`-z` flags in an isolated `HERMES_HOME`.

4. **Effective configuration** — `write_isolated_fallback_session()` writes a session-level config with `fallback_providers: []` and no `fallback_model`; `fallback_denial_proven()` confirms the chain is truly empty via `isolated_fallback_chain()`.

5. **Inference request** — The actual inference command runs in the isolated environment. `authorize_inference_request()` validates the provider/model pair against the empty fallback chain before any provider request is counted.

6. **Result persistence** — `LaunchResult` binds `task_id/attempt_id/contract_revision/assignment_epoch/baseline`, `candidate_digest` (verified artifact digest, distinct from `stdout_digest`), `verification_pending`, artifacts, disposition, and cleanup state.

---

## F1 — Authoritative Contract and Baseline

**Problem**: Git HEAD value alone does not identify all uncommitted task inputs. The baseline must be preserved separately from the workspace state.

**Changes**:
- Add `contract_revision` field to `Delegation` in `app/models.py` — a versioned hash of the authoritative task inputs (task definition + specialist instructions + config snapshot).
- Add `baseline` field to `Delegation` — explicit baseline identity (e.g., Git reference or snapshot digest), not derived from `HEAD` alone.
- In `TaskStore._rehydrate()`, preserve `contract_revision` and `baseline` from the ledger; if missing for legacy records, represent explicitly (set to `None`) and **block execution** where the contract requires an authoritative baseline.
- At dispatch: load `contract_revision` and `baseline` from the authoritative task/attempt; bind them to the assignment epoch and execution evidence; reject missing, stale, or mismatched values; do not trust caller-supplied replacements.

**Schema impact** (app/models.py Delegation):
```python
@dataclass
class Delegation:
    assignment_epoch: int = 0
    attempt: int = 0
    runtime_profile: str | None = None
    candidate_revision: str | None = None
    candidate_digest: str | None = None
    contract_revision: str | None = None        # NEW
    baseline: str | None = None                  # NEW
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
```

**Rehydration behavior**: `_rehydrate()` must extract `contract_revision` and `baseline` from ledger records. If absent (legacy), the value is `None` — and any code path that requires an authoritative baseline must fail closed (block launch) until the value is set.

---

## F2 — Verification State Consistency

**Problem**: `verification_pending=True` is persisted unconditionally, even when no verified candidate exists.

**Changes**:
- In `LaunchResult`, `verification_pending` must be derived from candidate availability, not set unconditionally:
  - Verified candidate exists → eligible for candidate verification (`verification_pending=True`).
  - No candidate → `verification_pending=False`.
  - Partial output/artifacts alone → execution evidence, not accepted candidate (`verification_pending=False`).
  - Failed/timed-out execution retains evidence but cannot imply success (`verification_pending=False`).
- In `HermesCliAdapter.launch()`, the `verification_pending` flag must be set based on whether a `candidate_digest` was actually collected and verified via `_collect_artifacts()`.
- Before persistence, reject contradictory result fields: e.g., `accepted=True` without `candidate_digest`, or `verification_pending=True` without a validated candidate.
- Preserve explicit orchestrator acceptance and review requirements: `accepted` requires `candidate_digest` present, spec PASS + quality APPROVED reviews for the current candidate, active lease, and budget not exceeded.

**Key code changes** (app/launcher.py):
- Line 618-633 (the non-zero-exit path): Ensure `verification_pending` is `False` when `candidate_digest` is `None`.
- Line 608-633 (the success path): `verification_pending = verified_digest is not None` — this is correct, but must be enforced that `verified_digest` is only set when artifacts with matching digest are collected.
- Remove any code path that sets `verification_pending=True` without a verified candidate.

---

## F3 — Mandatory Pre-Inference Authorization

**Problem**: `authorize_inference_request` is not called by `launch()` before the real child starts.

**Changes**:
- In `HermesCliAdapter.launch()` (the `dry_run=False` path), add a mandatory `authorize_inference_request()` call **before** the inference command runs, with the following validation:
  - Validate `task/attempt` binding from the reservation.
  - Validate `profile`, `lane/model`, `data class`.
  - Validate `approval scope` (check `task.delegation.approved_provider`/`approved_model` against the enforcer decision).
  - Validate `lease` (remaining lease must be positive — already done via `_remaining_lease_s()`).
  - Validate `budget` (request budget not exceeded — already done via `budget_exceeded()`).
  - Validate `required execution controls` (fallback policy = "deny", empty fallback chain proven).
- Enforcer initialization failure must fail closed: if `self.inference_enforcer` is `None` and the launch path is inference-bearing, refuse spawn.
- An optional `None` enforcer must not allow execution: add a check that if `inference_enforcer is None` and the launch requires authorization, block with a typed error.
- Protected direct launch calls must not bypass the checks: the `authorize_inference_request()` call must be inside the main launch path, not optional or behind a feature flag.
- Record the actual decision and policy revision in the launch reservation.

**Key code changes** (app/launcher.py):
- After the fallback-denial proof (line 483-490) and before the inference command (line 506), add:
```python
# Mandatory pre-inference authorization
decision = self.inference_enforcer.evaluate(
    self._routing_context_for_task(task)
) if self.inference_enforcer is not None else None
if self.inference_enforcer is None:
    return self._blocked(
        profile_id, [], None, "inference_denied",
        "inference enforcer not configured; refusing launch"
    )
if not decision.allowed:
    raise RoutingBlocked(
        "inference_denied",
        f"{decision.reason_code or 'denied'}: {decision.reason or ''}",
    )
# Record the decision
task.delegation.launch_reservation["policy_decision"] = {
    "revision": getattr(decision, 'revision', 'unknown'),
    "reason_code": decision.reason_code,
    "reason": decision.reason,
}
```
- If `self.inference_enforcer` is `None` at launch time, fail closed instead of proceeding.

---

## F4 — Effective Child Identity and Configuration

**Problem**: Attesting one profile and then executing a different synthetic identity.

**Changes**:
- Before the first inference request, establish the actual child with these attestations:
  - **Canonical profile and effective configuration identity**: The profile attested via `_attest()` must match the `selected.runtime_profile` used for the launch command.
  - **Task/attempt binding**: The `assignment_epoch` and `attempt` from the reservation must match the task's delegation record.
  - **Provider/model resolution**: The `--provider`/`-m` arguments must match the reservation's `approved_provider`/`approved_model`.
  - **Effective fallback chain**: The isolated config written to disk (and re-read) must prove an empty fallback chain — this is already done via `fallback_denial_proven()`.
  - **Relevant permissions and lease**: The remaining lease must be positive; the workspace must be owned by the current user.
  - **Policy decision**: The `inference_enforcer.evaluate()` decision must be recorded and must approve the exact provider/model pair.
- Ensure the attestation applies to the same execution/configuration that will issue the request, not a separate diagnostic process with different settings.
  - The `write_isolated_fallback_session()` config is written and immediately re-read to prove the empty chain — no separate diagnostic process.
  - The `authorize_inference_request()` uses the same `env` variables (`NOESIS_APPROVED_PROVIDER`, `NOESIS_APPROVED_MODEL`) that were set from the reservation.
- Prevent configuration changes between attestation and use where feasible: the isolated session config is written once and used directly; the `HERMES_HOME` is set to the isolated root for the subprocess duration.
- Do not copy production secrets wholesale into temporary homes or log them: the isolated session uses `HERMES_HOME` pointing to the temp root, and ambient inference env vars (`HERMES_INFERENCE_MODEL`, `HERMES_MODEL`, `HERMES_PROVIDER`) are explicitly popped from the environment (see `_env()` method).

**Key code changes** (app/launcher.py):
- The `write_isolated_fallback_session()` → re-read → `fallback_denial_proven()` sequence already ensures the config identity matches the launch.
- Add explicit assertion after config re-read:
```python
# Prove the attested config is the one that will be used
assert config_text == (isolated_root / "profiles" / profile_id / "config.yaml").read_text(encoding="utf-8")
```
- Ensure `authorize_inference_request()` is called with the same `approved_provider`/`approved_model` from the reservation, not freshly supplied values.

---

## F5 — Bounded Credential Attempts

**Problem**: Provider fallback and same-provider credential rotation are distinct; no finite attempt budget enforcement.

**Changes**:
- Implement a finite attempt budget across credential selection/retries:
  - The `attempt` field in `Delegation` tracks the current attempt number.
  - `max_attempts` from the task's `retry` field limits total retries.
  - Each provider cycling iteration increments `attempt`; when `attempts >= max_attempts`, block further attempts.
- No unbounded revisit of already-rejected credentials within the request:
  - The `authorize_inference_request()` function already rejects if `attempts >= 1` and `blocked=True` (billing/quota failure already closed this attempt).
  - Add tracking so that once a credential pair is rejected (via `FallbackDenied`), the same pair is not retried in the same request.
- Terminal behavior when the budget or eligible pool is exhausted:
  - `FallbackDenied("provider_attempt_exhausted", "billing or quota failure already closed this attempt")` — raised when `attempts >= 1` and `blocked=True`.
  - `FallbackDenied("fallback_unenforced", "fallback chain is not empty")` — raised when fallback chain has entries.
  - `FallbackDenied("unauthorized_fallback", "requested route is not the approved pair")` — raised when provider/model don't match the approved pair.
- Explicit billing/quota error reporting:
  - `FallbackDenied("billing_or_quota", "synthetic billing failure")` — reported as an explicit error.
  - The `launch()` result should carry `blocker_code="billing_quota_failed"` when budget is exhausted.
- Shared deadline/resource limits:
  - The remaining lease caps the effective timeout via `_effective_timeout()`.
  - The attempt budget is checked in `_assert_spawn_gates()`: `if task.retry.exhausted and task.retry.attempts > 0: raise RoutingBlocked("attempt_budget_exhausted", "attempt budget exhausted")`.
- **Do not assume the first billing rejection means every credential is unusable**: the `authorize_inference_request()` only blocks the current attempt; subsequent calls with `attempts=0` could theoretically retry, but the `blocked` flag prevents this within a single request.
- **Do not claim a 300-second process timeout enforces a request-count bound**: the timeout is a subprocess resource limit, not a credential attempt counter.

**Key code changes** (app/launcher.py):
- In `_assert_spawn_gates()`, the retry budget check is already present (line 417-418).
- In `authorize_inference_request()` (line 175-191), the `attempts` parameter is already used to check `attempts >= 1` → `FallbackDenied("provider_attempt_exhausted")`.
- Add credential cycling guard: track rejected provider/model pairs per request and skip them on retry.
- In the launch path, after a `FallbackDenied` error, increment the task's `retry.attempts` and check against `max_attempts` before allowing another launch attempt.

---

## Regression Tests (RED/GREEN)

Write tests proving each F1–F5 guarantee. Use offline fake transports/process fixtures. No provider requests from tests.

**Test categories**:
1. Authoritative identity survives persistence/restart — contract_revision and baseline persist across Orchestrator restarts.
2. Missing/stale/spoofed task revision or baseline blocks launch — if `contract_revision` or `baseline` is `None` and required, launch is blocked.
3. Persisted verification state matches validated candidate availability — `verification_pending=True` only when `candidate_digest` is collected; `False` otherwise.
4. Missing/failed authorization prevents spawn or provider requests at the appropriate boundary — `inference_enforcer=None` blocks launch; `authorize_inference_request()` rejection blocks launch.
5. Actual child identity/config mismatch blocks its first inference — if the loaded profile != selected.runtime_profile, or config doesn't prove empty fallback chain, launch is blocked.
6. Unauthorized fallback cannot issue a request — `authorize_inference_request()` with mismatched provider/model raises `FallbackDenied`.
7. Credential cycling stops at the enforced bound — after `max_attempts` rejections, further attempts are blocked.
8. Success, nonzero exit, timeout, cancellation, and partial artifact behavior remain correct — existing behavior preserved.
9. Existing positive-only review/acceptance gates remain enforced — spec PASS + quality APPROVED still required for acceptance.

**Test files to create/modify**:
- `orchestration/orchestrator/tests/test_contract_revision.py` — F1 tests
- `orchestration/orchestrator/tests/test_verification_consistency.py` — F2 tests
- `orchestration/orchestrator/tests/test_pre_inference_authorization.py` — F3 tests
- `orchestration/orchestrator/tests/test_effective_child_identity.py` — F4 tests
- `orchestration/orchestrator/tests/test_bounded_credential_attempts.py` — F5 tests

---

## Delivery Checklist

- [ ] Implementation plan written and documented
- [ ] F1: `contract_revision` and `baseline` fields added to `Delegation`; rehydration blocks missing values
- [ ] F2: `verification_pending` derived from candidate digest availability, not set unconditionally
- [ ] F3: `authorize_inference_request()` called mandatorily before every inference-bearing launch; enforcer None fails closed
- [ ] F4: Effective child identity/configuration attested before first inference; config changes between attestation and use prevented
- [ ] F5: Finite attempt budget enforced; no unbounded credential revisit; explicit billing/quota errors
- [ ] RED/GREEN tests written for each F1–F5 guarantee
- [ ] `git diff --check` passes (no whitespace errors)
- [ ] `compileall -q` passes on modified modules
- [ ] No installed Hermes-core edits, no provider/ policy/ reconciliation changes, no persistent configuration changes, no dependency installations, no commits, no pushes, no deployments
- [ ] Completion packet marked `not_self_approved`