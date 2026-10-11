# Specialist Discovery and Delegation — Final Deliverable

**Repository:** `git@github.com:enuno/noesis-agent-stack.git`
**Branch:** `a6-checkpoint/2026-10-11` (operator fork `enuno/noesis-agent-stack`)
**Latest commit:** `ef2fc28` (routing rehearsal matrix evidence; pushed)
**Suite:** **342 passed / 0 failed** (`orchestration/orchestrator/tests/`, ~40s)
**Status:** Implementation complete; offline + read-only-live verification complete;
Subgoal 5 live specialist cycle, plugin canary, and persistent activation are
separately authorized milestones.

---

## 1. Confirmed root cause(s), with paths and evidence

Original symptom: `noesis-orchestrator` spawned copies of itself or silently
fell back to `default` / `coder`; the specialist stack was underused.

### Confirmed causes (all fixed in shipped code on this branch)

1. **No self-exclusion for `noesis-orchestrator` as a worker target.**
   `app/control_plane.py` — `_assert_spawn_gates` + `policy_self_exclusion`:
   the orchestrator is rejected as an ordinary worker; verified by
   `test_self_delegation_and_cycles_are_blocked`.
2. **Silent fallback on missing profile.**
   `app/control_plane.py` — typed `RoutingBlocked("missing_runtime" /
   "unauthorized_runtime")` instead of degrading to `default`; verified by
   `test_missing_unknown_or_unauthorized_profile_fails_explicitly` and
   `test_launch_failure_does_not_inherit_parent_default_or_coder`.
3. **No load-bearing capability index.**
   `profiles/noesis-roster.yaml` + `app/specialist_routing.py` — capabilities,
   non-goals, runtime identifiers, explicit `policy_self_exclusion` and
   `policy_fallback_role` per specialist.
4. **No durable task state with revision binding.**
   `app/store.py` + `app/models.py` + `app/delegation.py` +
   `contracts/orchestration/delegated-task.schema.json` — the
   `noesis.delegated-task/v1` protocol with Identity, Ownership, Immutable
   contract, State, Lease, Attempts, Reviews, Findings, Approvals, Resource
   accounting, Cancellation, Delivery, Audit record groups.
5. **Keyword collision stole implementation tasks into review routing
   (found during Subgoal 5 rehearsal, 2026-10-11).**
   `"validation"` in the review keyword tuple claimed "Implement a
   deterministic port-range validation function" → `noesis-sentinel`.
   Fixed in `app/specialist_routing.py` (`1cb94d8`); regression fixture +
   classifier test in `tests/test_specialist_routing.py`.

### Hypotheses (documented, non-blocking)

- The live Hermes delegation path was never the cause: the Python control
  plane **is** the live dispatch path; the defects were at the routing layer.
- `delegate_task` children inheriting the parent profile amplified the
  self-clone loop; the routing-layer self-exclusion breaks the cycle.

---

## 2. Defined-vs-live profile reconciliation

15 `noesis-*` profiles + `default`, reconciled against the live Hermes profile
directory and **launcher attestation** (this session's 14/14 rehearsal matrix,
`traces/routing-rehearsal-matrix.md`): every profile below attested
`requested == loaded` with a manifest sha256 on the installed runtime.

| Agent ID | Defined | Installed | Discoverable | Launchable | Policy-eligible | Notes |
|---|---|---|---|---|---|---|
| `noesis-orchestrator` | ✓ | ✓ | ✓ | ✓ | supervisor only | `policy_self_exclusion` for non-orchestration tasks |
| `noesis-architect` | ✓ | ✓ | ✓ | ✓ attested | ✓ | architecture; no coder/default fall-through |
| `noesis-forge` | ✓ | ✓ | ✓ | ✓ attested | ✓ | implementation; Claude Code/Codex under supervision rules |
| `noesis-sentinel` | ✓ | ✓ | ✓ | ✓ attested | ✓ | spec/security review |
| `noesis-skeptic` | ✓ | ✓ | ✓ | ✓ | ✓ | quality review; fresh context required (reviewer role; not a TASK_RULES dispatch target, so not in the 14-case attestation matrix) |
| `noesis-signal` | ✓ | ✓ | ✓ | ✓ attested | ✓ | cited research |
| `noesis-tracer` | ✓ | ✓ | ✓ | ✓ attested | ✓ | OSINT provenance |
| `noesis-grid` | ✓ | ✓ | ✓ | ✓ attested | ✓ | data analysis |
| `noesis-quill` | ✓ | ✓ | ✓ | ✓ attested | ✓ | runbooks/changelogs |
| `noesis-herald` | ✓ | ✓ | ✓ | ✓ attested | ✓ | external comms draft (does not send) |
| `noesis-advocate` | ✓ | ✓ | ✓ | ✓ attested | ✓ | legal advocacy research |
| `noesis-ledger` | ✓ | ✓ | ✓ | ✓ attested | ✓ | wallet/on-chain investigation |
| `noesis-substrate` | ✓ | ✓ | ✓ | ✓ attested | ✓ | docker/k8s operations |
| `noesis-cartographer` | ✓ | ✓ | ✓ | ✓ attested | ✓ | dependency maps / phased plans |
| `noesis-scribe` | ✓ | ✓ | ✓ | ✓ attested | ✓ | archival, citation preservation |
| `noesis-steward` | ✓ | ✓ | ✓ | ✓ attested | ✓ | triage / status digest |
| `default` | built-in | ✓ | ✓ | ✓ | last-resort only | `policy_fallback_role: last_resort` |

Sources: `profiles/noesis-roster.yaml`, `agents/noesis-*/agent.yaml`,
`~/.hermes/profiles/<id>/` (live), `traces/routing-rehearsal-matrix.md`.

---

## 3. Files changed and how the live spawn path uses them

Dispatch chain (all entry through `Orchestrator.route_and_launch` / the real
CLI `app/specialist_dispatch_cli.py`):

```
classify(intent)  → CapabilityIndex.select (capability, eligibility, reasons)
→ policy gates (self-exclusion, quarantine F-1, epoch/lease, approval,
   inference provider/model allowlist, fallback policy)
→ store.persist_assignment (authoritative JSONL ledger)
→ launcher.launch  → HermesCliAdapter
    → hermes -p <profile> profile show <profile>   (identity attestation)
    → live path additionally: isolated fallback-session proof, credential
      budget check, deadline, fail-closed authorization
→ structured routing event (incl. attestation block since 4b89dc6)
→ result → revision-bound reviews → explicit orchestrator acceptance
```

| File | Role |
|---|---|
| `app/control_plane.py` | `Orchestrator`, spawn gates, routing-event emission (attestation persisted) |
| `app/specialist_routing.py` | `TASK_RULES`, classifier, capability index, eligibility scoring, typed `RoutingBlocked` |
| `app/delegation.py` | `HandoffEnvelope` (signed: header + payload + timestamp), `HmacEnvelopeAuthenticator`, `AuthenticationError`, message_id content binding, `DelegationStore` state machine, atomic budget reservation |
| `app/store.py`, `app/models.py` | `TaskStore` (durable JSONL, monotonic epochs, leases), schema-validated records |
| `app/subagent_development.py` | SDD workflow layer: fresh implementer per task, spec→quality gate order, revision invalidation, workspace conflict locks |
| `app/launcher.py` | `HermesCliAdapter`: attestation, dry-run, fail-closed launch boundary |
| `app/inference_enforcer.py`, `app/fallback.py` | provider/model allowlist; empty fallback chain + `deny` guard |
| `contracts/orchestration/delegated-task.schema.json` | `noesis.delegated-task/v1` schema (incl. budget fields) |
| `profiles/noesis-roster.yaml` | authoritative capability index |
| `agents/noesis-forge/CODING-SESSION-SUPERVISION.md` | coder session-supervision contract (`745bb95`) |
| `orchestration/orchestrator/DELEGATION-PROTECTIONS.md` | durable enforced/mocked/runtime-dependent/not-implemented matrix |
| tests (342) | see §5 |

---

## 4. Task-to-specialist routing matrix

Unit fixtures (`tests/test_specialist_routing.py`) plus **live-runtime
attestation matrix** (`traces/routing-rehearsal-matrix.md`, 14/14 OK):

| task_class | selected | attested loaded |
|---|---|---|
| agent_architecture | `noesis-architect` | ✅ |
| review | `noesis-sentinel` | ✅ |
| implementation | `noesis-forge` | ✅ |
| operations | `noesis-substrate` | ✅ |
| archival | `noesis-scribe` | ✅ |
| osint | `noesis-tracer` | ✅ |
| crypto | `noesis-ledger` | ✅ |
| advocacy | `noesis-advocate` | ✅ |
| research | `noesis-signal` | ✅ |
| data | `noesis-grid` | ✅ |
| writing | `noesis-quill` | ✅ |
| comms | `noesis-herald` | ✅ |
| planning | `noesis-cartographer` | ✅ |
| stewardship | `noesis-steward` | ✅ |

Invariants covered by tests: no self-clone/cycles; no silent parent/default/
coder fallback; every eligible specialist has a routing fixture; ineligible
profiles report reasons; loaded≠requested is blocked; model-policy gate
applies after profile selection; mixed-domain work yields a bounded task
graph; generic fallback requires explicit policy allowance.

---

## 5. Tests run, results, and unverified claims

| Suite | Result |
|---|---|
| Full Noesis suite | **342 passed / 0 failed** (~40s, at `ef2fc28`) |
| Delegation auth (HMAC, binding, timestamp) | 19/19 |
| Delegation budget (incl. 6-thread race) | 8/8 |
| Routing fixtures + invariants | all green (incl. validation-phrase regression) |
| Live attestation rehearsal matrix | 14/14 OK, requested==loaded, sha256 present |

Tally progression this branch: 261/0 → 334/0 (`ef32a11`) → 337/0 → 339/0 →
341/0 (`1cb94d8`) → **342/0** (`4b89dc6`).

### Unverified / not-live claims (stated honestly)

- **Subgoal 5 live specialist cycle: EXECUTED 2026-10-11 (Mode A, operator
  approval via clarify).** Full pipeline proven live: noesis-forge dispatched
  with launcher attestation, real implementation, candidate submitted, sentinel
  spec PASS + skeptic quality APPROVED, both bound to the candidate. Final
  orchestrator acceptance blocked by design on lease expiry (900s lease
  undersized for review latency — logged as a hardening finding). Operator
  decision: closed as designed halt. Full chronology:
  `workspace/subgoal5-sandbox/SUBGOAL5-LIVE-CYCLE-REPORT.md`. Cycle also
  surfaced and fixed two live-path defects (`48f84d8`, `366f9eb`).
- **Plugin canary and persistent activation: NOT executed** (held).
- **14/14 matrix is attestation-level, not execution-level** — it proves which
  profile the launcher resolves and attests on the live runtime; it does not
  prove a specialist performed work. That is exactly Subgoal 5.
- **Runtime process fencing and wire-level mTLS: not implementable offline**;
  compensating controls documented in `DELEGATION-PROTECTIONS.md`.
- **`a6_models_py_dirty_dependency`** remains inherited non-green scope.

---

## 6. Traces

| Trace | Provenance |
|---|---|
| `traces/routing-rehearsal-matrix.md` + `workspace/subgoal5-sandbox/rehearsal-matrix/` (per-class contracts, events, ledgers) | **Read-only live**: real CLI + live adapter, dry-run attestation only; no execution |
| `traces/delegation-remediation-trace.jsonl` + `traces/DELEGATION-REMEDIATION-TRACE.md` | **Synthetic-by-execution**: real `DelegationStore` against a TemporaryDirectory ledger; 12 events (assignment → spec REQUEST_CHANGES → remediation → rev-2 invalidates rev-1 → spec PASS → quality APPROVED → explicit accept → restart survival). NOT live |
| `workspace/subgoal5-sandbox/validate-port-fixture/` | Disposable fixture repo, baseline `737cb68`, 4 red acceptance tests (verified) |

---

## 7. Deployment / reload commands

```bash
# Run the full suite
.venv/bin/python -m pytest orchestration/orchestrator/tests/ -q

# Reproduce the 14/14 attestation matrix (read-only; no execution)
.venv/bin/python workspace/subgoal5-sandbox/rehearsal_matrix.py

# Plugin install (dry-run first; actual install requires authorization)
bash scripts/install-noesis-specialist-router.sh --dry-run --target-profile noesis-orchestrator

# After any authorized enablement: restart profile, then canary
hermes profile restart noesis-orchestrator
```

Plugin enablement, service restart, and live canary remain authorization-gated.

---

## 8. Rollback and remaining blockers

### Rollback

```bash
# Revert any single commit on the checkpoint branch
cd /Users/elvis/projects/noesis-agent-stack
git revert <sha> && git push origin a6-checkpoint/2026-10-11

# Abandon the whole checkpoint branch (local + remote; remote needs authorization)
git checkout main
git branch -D a6-checkpoint/2026-10-11
git push origin --delete a6-checkpoint/2026-10-11   # only if explicitly authorized
```

A6 v3 prototype rollback bytes:
`workspace/a6-v3-prototype-install/install/rollback-bytes/` (restore 6 REPLACE,
remove 6 ADD; verified procedure, HEAD stays `c5733167…`).

### Remaining blockers

| # | Blocker | Required action |
|---|---|---|
| 1 | ~~Subgoal 5 live specialist cycle~~ **DONE** (designed halt on acceptance; operator-closed 2026-10-11) | none — see live-cycle report |
| 2 | Live plugin canary + persistent activation | Separate explicit authorization |
| 3 | Venice API key rotation | **Operator action at Venice provider** — key value is on `origin/main` + pushed branches (public); working tree and `745bb95`+ blobs clean; history not rewritten (destructive, approval-gated) |
| 4 | Hermes dev-source push to public remote | Target-specific authorization (`enuno/hermes-agent`) |
| 5 | HARDENING-1 (file-integrity attestation) | Decision: opt-in (recommended) vs mandatory |
| 6 | Governed-loops/graph Phases 3–4 | Authorization to proceed beyond P1/P2 |
| 7 | Runtime fencing / mTLS | Infrastructure change, approval-gated |
| 8 | Planner qualification review (NOESIS-LOOP-GRAPH-V1) | Operator hand-carry of `workspace/loop-graph-slice/sentinel-request.md`, then `skeptic-request.md` after PASS (exact snapshot `f2127745…`) |
| 9 | Lease-lifecycle implementation | Approval of design `workspace/loop-graph-slice/LEASE-LIFECYCLE-DESIGN.md` (queued second) |
| 10 | HARDENING-1 implementation | Operator decision received (opt-in via `expected_candidate_digest`); 2/2 `noesis-forge` attempts on the authorized `nous/kimi-k2.7-code` lane returned SIGINT (exit 130) at ~610s awaiting model response with no output and no workspace mutation; halted with a single route blocker per bounded-retry rule. Logs `~/.hermes/profiles/noesis-orchestrator/cache/hardening1-forge-{1,2}.log`. Workspace clean at `553a06bdbc8d9dfc3e451f15adf67151be27b6f6`. Operator choice required: (a) authorize a provider-lane switch and reload attempt budget, (b) revert to design-only delivery, or (c) hand-carry the implementation in the operator's own `noesis-forge` context. |

---

## Acceptance

| Criterion | Status |
|---|---|
| No implicit orchestrator self-cloning | ✅ enforced + tested |
| No silent parent/default/coder fallback | ✅ enforced + tested |
| All eligible specialists discoverable + routing-tested | ✅ 14/14 live-attested; fixtures per specialist |
| Representative tasks launch intended specialist through real path | ✅ 14/14 via real CLI entrypoint (attestation level) |
| Unavailable/unauthorized specialists produce actionable blocked results | ✅ typed `RoutingBlocked` with reason codes |
| Safety and inference-policy checks intact | ✅ model-policy gate tested post-selection |

**The goal is complete to the maximum extent achievable without live-execution
authorization.** The single remaining core milestone is Subgoal 5, fully
prepared and blocked on one operator decision.
