# Subgoal 4 checkpoint verification

Date: 2026-10-17 21:44 MDT
Checkout: `/Users/elvis/projects/noesis-agent-stack` main @ `de54097`
Method: re-run, not trusted from report. Read-only plus test execution. No commits.

## Results

- `orchestration/orchestrator/app/delegation.py` (38,367 B): `MessageType` (15 types),
  `TaskState`, `Lease`, `HandoffEnvelope`, `canonical_envelope_bytes`,
  `HmacEnvelopeAuthenticator`, `DelegationStore` — all present.
- `contracts/orchestration/delegated-task.schema.json` present.
- Delegation tests: **61 passed, 0 failed** in 1.26s across:
  - test_delegated_task_state.py
  - test_delegation_authentication.py
  - test_delegation_budget.py
  - test_delegation_failure_injection.py
  - test_delegation_subgoal4_coverage.py
  - test_plan_first_delegation.py
- Reported checkpoint claimed 8 tests; actual coverage is 61 — superset confirmed.

## State machine reconciliation vs drafted protocol

Equivalent: core lifecycle pending→ready→assigned→running→verifying→
spec_review→quality_review→completed; epoch-checked leases; HMAC envelopes;
retry-as-new-task preserving prior attempt; idempotency; budget + launch
reservations (`control_plane.py:479-727`).

Implemented differently (not gaps):
- No `remediation` state: rejection returns task to `running`, owner reconciles
  (`delegation.py:705`).
- No CLARIFICATION_*/APPROVAL_* envelope types: approval gating is
  orchestrator-side (`control_plane.py:2352` `awaiting_approval` list).
- No `cancelling` intermediate: `cancelled` terminal with cleanup evidence.

Expected noise: one DeprecationWarning — a backward-compat test deliberately
exercises legacy `route_and_launch` (test_delegation_subgoal4_coverage.py:269).

## Router–delegation connection

Plugin → `control_plane.preflight_then_route` → durable `task.delegation.*`
records. Epoch mismatch refused at `control_plane.py:569`. Plugin never
touches `DelegationStore` directly — the ledger cannot be bypassed from the
tool surface. Live admission still unverified (dry-run pending approval).

## Remaining live milestones (unchanged)

1. Operator ruling: plugin-scoped ledger vs canonical `tasks.jsonl` binding.
2. Approved dry-run of `noesis_specialist_delegate` (synthetic).
3. Approved canary `KIMI_CODE / kimi-k2.7-code` + inline `INFERENCE_ROUTING_MODE=enforce`.

## Ledger ruling (2026-10-17, operator approved all 3) — RESOLVED: keep plugin-scoped

Evidence against rebinding to canonical `tasks.jsonl`:
- `store.py:302`: replay folds **any** record containing a `task` key into
  orchestrator state. Plugin records are full TaskContract-shaped, so they
  would not poison replay (all `_rehydrate` keys present) — but every
  synthetic dry-run task would permanently fold into canonical orchestrator
  state on every replay, and `task_proposed` is a canonical event name
  (`store.py:496`), so synthetic records would be indistinguishable from real
  admitted tasks. Separation is load-bearing, not accidental.
- Ruling: keep `NOESIS_TASK_LEDGER` → `noesis-specialist-plugin-ledger.jsonl`.
  Reconciliation, when needed, is an explicit export — never shared append.

## Dry-run canary (2026-10-18) — PASSED

In-session `noesis_specialist_delegate`, post-reload:

- First attempt refused `no_configured_ledger`: plugin tool host does not
  inherit the dotenv env (verified worker env does not cover the plugin host).
  Unblocked by passing `ledger_path`/`events_path` explicitly — the plugin's
  own documented escape hatch.
- Attempts with review-flavored intent refused `no_eligible_specialist`:
  classifier mapped "verify…" to task_class `review`, which requires
  sentinel/skeptic, which lack `code_modify` → correct fail-closed behavior.
- Representative implementation payload:

```json
{"task_id": "53911d90-db0e-45a8-8809-81e388b9ad63", "task_class": "implementation",
 "selected_agent": "noesis-forge", "requested_profile_id": "noesis-forge",
 "loaded_profile_id": "noesis-forge", "resolved_launch_target": "noesis-forge",
 "fallback_reason": null, "model_provider": "nous",
 "model_id": "deepseek/deepseek-v4-flash",
 "attestation": {"requested_profile": "noesis-forge", "loaded_profile": "noesis-forge",
  "profile_path": "/Users/elvis/.hermes/profiles/noesis-forge",
  "manifest_sha256": "aef7828be54b18b5808eae4c5c366bf4c156c30b8230d6454c25d2177efa2366"}}
```

Requested == loaded == resolved, no fallback — the routing/identity path is
proven at the control-plane level. Lane/model for this run: agent default
(nous/deepseek-v4-flash); the approved KIMI_CODE lane is exercised by the
live canary below, not the synthetic dry-run.

## Live canary (2026-10-18) — r1 FAILED, defect fixed, r2 RUNNING

r1 (`elvis:canary:2026-10-18:kimi-code`) refused `child_failed`: the launcher
handed the policy lane name to the Hermes CLI as `--provider KIMI_CODE`; the
CLI lowercased for lookup and reported `Unknown provider 'kimi_code'`.
Root cause: no lane→provider-id mapping anywhere in the launch path.

Fix (3 parts, regression tests added):
- `platform/inference-routing.yaml`: `KIMI_CODE` lane gains explicit
  `hermes_provider: kimi-coding`.
- `inference_routing.py`: `InferenceRoutingEnforcer.hermes_provider_for_lane()`
  resolves the binding from the loaded policy (None for unknown lanes).
- `control_plane.py`: both launch sites (legacy `route_and_launch` and
  canonical `route_with_preflight`) map the approved lane before
  `_invoke_launch` and compare the reported provider against the mapped id.

Tests: `tests/test_lane_provider_mapping.py` — 4 tests (real-policy mapping,
unknown-lane None, launch-site mapping, non-lane passthrough). The new tests
caught a real bug (`str(None)` → `'None'` string) before r2.
Gate: 39 passed across wiring + b2 preflight + identity + mapping suites.

r2 (`elvis:canary:2026-10-18:kimi-code:r2`, task `34c16288-41fd-491d-a9c0-2fc6debe82b7`)
— **PASSED** (background PID 99510, exit 0):

- `selected_agent` / `requested` / `loaded` / `resolved`: **noesis-forge** ×4,
  `fallback_reason: null`
- `launch_command`: `hermes -p noesis-forge --provider kimi-coding -m
  kimi-k2.7-code -z <structured contract>` — lane mapping confirmed in the
  actual argv
- `attestation`: loaded_profile `noesis-forge`, isolated home
  `canary-2026-10-18/.noesis-fallback-session/profiles/noesis-forge`,
  manifest sha256 `82fbb104…` (spawn-identity binding from 7578750 verified live)
- reservation: epoch 1, preflight KEEP, approved pair `KIMI_CODE /
  kimi-k2.7-code`, `fallback_policy: deny`
- child exited 0; stdout digest `7ffb975f…` recorded; no workspace artifacts
  collected → `verification_pending: false`, `accepted: false`

Milestone verdict: launch canary **PASSED** — child profile, approved lane,
model, workspace isolation, and identity binding are all proven live.
Result validation is partial: exit-0 + stdout digest only; the child
produced no file artifacts (nothing to verify byte-level), and
`captured_stdout` is not persisted to the ledger (digest only) — a minor
evidence-retention gap for the ACCEPT-phase audit trail.

Residual nits (non-blocking): success payload `model_provider/model_id`
report the agent-registry default (nous/deepseek-v4-flash), not the enforced
launch pair; the launch truth is in `launch_command`.

### Evidence-retention gap — FIXED same day

The launch record persisted only `stdout_digest`, dropping the child's
handoff text. `control_plane.py` now persists `captured_stdout` (launcher
output is already redacted) capped at 16 KiB with an explicit truncation
marker, in both launch sites' `launch_handle_persisted` records. Tests:
`test_launch_persists_bounded_captured_stdout_for_accept_audit`,
`test_bounded_stdout_evidence_caps_and_marks_truncation` — 6 passed in the
mapping file. Full gate: **583 passed, 2 xfailed, 2 xpassed** (72s).

## Coverage audit (2026-10-17 21:52 MDT) — Subgoal 5 §2

Mapped all 61 delegation test names against the §2 checklist. Coverage is
broad: lifecycle, idempotent duplicate ASSIGN/RESULT/ACCEPT, stale
epochs/versions, tamper refusals, concurrent reservations, restart recovery,
lease expiry, cancellation, planning gate, fast path, retry-as-new-task.

Confirmed gap: zero tests exercised the critical/important findings gate
(`delegation.py:680-681`); no `severity` string existed in any delegation
test.

Added to `test_delegated_task_state.py` (uncommitted):
- `test_critical_findings_block_spec_pass` — PASS + critical finding refused,
  state stays verifying, no review row recorded.
- `test_important_findings_block_quality_approval` — APPROVED + important
  finding refused after a valid spec PASS; prior spec row survives.
- `test_minor_findings_do_not_block_passing_verdict` — gate is
  severity-specific; minor findings record and advance to spec_review.

Result: file 8→11 passed; delegation group 61→64 passed, 0 failed.

Open design question (no test pins this): the refusal at `delegation.py:680`
applies to every verdict, including REQUEST_CHANGES/BLOCKED — so structured
critical findings cannot ride a changes-requested review into the remediation
handoff. Drafted protocol carries findings into remediation; current behavior
requires delivering them out-of-band. Operator may rule this intentional
(fail-closed) or request a narrow fix.

Not pursued as gaps (semantic equivalence): contract-change invalidation
(contract is immutable per-task; a change is a new task), launch-before-handle
and result-before-transition crash windows (would need invasive hooks into
`_mutate`; assignment-before-delivery window is already covered).
