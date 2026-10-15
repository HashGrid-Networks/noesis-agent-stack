# Noesis Delegation — Standing Goal and Final Deliverable

**Repository:** `git@github.com:enuno/noesis-agent-stack.git`
**Workspace branch at amendment:** `docs/status-reconcile-2026-10-12`
**Workspace revision at amendment:** `cc0f3001c7ee0ce9fda58be21f282e8b2b57ebe8`
**Document version:** v3.0 (protocol-adaptable delegation amendment, 2026-10-15)
**Verification baseline:** **444 passed / 0 failed** for the current bounded
A2/A3/B1 offline candidate; the earlier 430-test pre-slice baseline and the
360/342-test checkpoints below are retained as historical chronology.
**Status:** Native specialist routing is implemented and locally tested. The
installed-runtime specialist canary remains **BLOCKED**. Runtime restoration was
verified at the historical rollback checkpoint; a later unrelated
`codebase_memory` configuration drift is recorded separately and was neither
adopted nor reverted. ACP and A2A are approved architecture/planning directions
only—not approved implementations or verified deployments. Production release
and deployment approval remain held.

> **Historical-record boundary.** Sections 1–8 preserve the 2026-10-11
> checkpoint chronology and its then-current completion/test claims. They are
> evidence history, not statements that the amended v3.0 goal is currently
> complete or deployed. Section 9 and the final Acceptance table are the current
> planning baseline and disposition.

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
| 5 | ~~HARDENING-1 (file-integrity attestation) mode decision~~ **DONE** (2026-10-11) | none — operator chose opt-in via `expected_candidate_digest`; preserves v3 standalone behavior; fails closed on mismatch |
| 6 | Governed-loops/graph Phases 3–4 | Authorization to proceed beyond P1/P2 |
| 7 | Runtime fencing / mTLS | Infrastructure change, approval-gated |
| 8 | Planner qualification review (NOESIS-LOOP-GRAPH-V1) | Operator hand-carry of `workspace/loop-graph-slice/sentinel-request.md`, then `skeptic-request.md` after PASS. Reproducible snapshot digest `a4519bd6556d3f98030928349b336c0bc46aeb2846e3ad23dec85358a85e8b19` per `workspace/loop-graph-slice/SNAPSHOT-DIGEST-RECIPE.md`; legacy `f2127745…` is historical only. |
| 9 | Lease-lifecycle implementation | Approval of design `workspace/loop-graph-slice/LEASE-LIFECYCLE-DESIGN.md` + brief `workspace/loop-graph-slice/LEASE-LIFECYCLE-APPROVAL-BRIEF.md` (queued second) |
| 10 | ~~HARDENING-1 implementation route decision~~ **DONE** (2026-10-11) | none — operator chose option C: hand-carry in operator's own `noesis-forge` context. Copy-ready handoff staged at `workspace/a6-v3-prototype-install/HARDENING-1-FORGE-IMPLEMENTATION-HANDOFF.md` (operator-mediated, no implementation dispatch from this profile). |
| 11 | Planner snapshot digest reproducibility | None for the implementation; legacy `f2127745…` was non-reproducible (7 candidate recipes tried, none matched). New canonical recipe in `workspace/loop-graph-slice/SNAPSHOT-DIGEST-RECIPE.md`; reproducible digest `a4519bd6…`; per-file SHAs unchanged. Reviewer recipes updated. |
| 12 | Lease-lifecycle implementation approval | One-line operator decision on `LEASE-LIFECYCLE-APPROVAL-BRIEF.md` (changes authority semantics, additive fields, review-gated, no new provider/budget) |
| 13 | LLM-wiki reference integration | None required; wiki used as reference for loop engineering, knowledge graph, evidence/provenance, retrieval, bounded evaluation. Gap map at `workspace/a6-v3-prototype-install/LLM-WIKI-GAP-MAP.md`. Production-release backlog sequenced (10 items). Graph remains a derived projection; no new authority store introduced. |

---

## 9. Governed, protocol-adaptable delegation amendment

### 9.1 Amended standing goal

Evolve the Hermes-centered Noesis orchestration engine so one admitted Noesis
task attempt can execute through one of three explicitly selected adapters:

1. the existing qualified native/local Hermes profile path;
2. an **Agent Client Protocol (ACP)** client path to a compatible,
   session-based worker; or
3. an **Agent2Agent (A2A)** client path to an approved, independently deployed
   agent service.

The protocol is never the orchestration authority. Noesis continues to own
specialist selection, task-contract validation, admission, authorization,
approval, budget reservation, durable task/attempt identity, lease and
revocation, recovery, provenance, independent verification, and acceptance.
Workers report outcomes; they do not approve themselves, expand their scope,
renew authority, declare independent QA complete, or mark work accepted.

```text
Human / NoesisPraxis user-facing intake
    ↓
Hermes-centered Noesis orchestration
    - specialist selection
    - task-contract validation
    - admission and authorization
    - budget reservation
    - durable attempt identity
    ↓
Noesis-owned delegation adapter boundary
    ├─ native/local adapter → qualified Hermes profile
    ├─ ACP client adapter   → compatible session-based worker
    └─ A2A client adapter   → approved independent agent service
    ↓
normalized progress, artifacts, failures, cancellation, and terminal state
    ↓
Noesis verification, independent QA, and explicit acceptance
```

Supervision, research, reflection, implementation, and independent QA remain
separate responsibilities. OpenClaw and coding harnesses remain bounded workers.
No adapter may assume that workers are stateless, ephemeral, or interchangeable.
No parallel task store, approval ledger, scheduler, or delegation service is
introduced.

### 9.2 Architecture decision record: ADR-PDA-001

**Title:** Keep authority in Noesis; treat native, ACP, and A2A as execution
adapters

**Status:** Accepted for architecture and implementation planning; execution
phases are separately gated

**Date:** 2026-10-15

**Context.** Noesis already has durable task contracts, append-only state,
assignment/attempt fencing, leases, approvals, bounded retry, launch
reservations, quarantine, provenance, review, and explicit acceptance. Its
current runtime boundary is shaped around a local `HermesCliAdapter`. Installed
Hermes also contains (a) an inbound ACP server/session implementation, (b) a
Copilot-specific outbound ACP client shim, and (c) inbound and outbound A2A
implementation. These protocol implementations are not automatically integrated
with the Noesis task lifecycle and are not evidence of an active deployment.

**Decision.** Introduce one transport-neutral, Noesis-owned adapter contract
behind the existing dispatch/admission path. Extend existing `TaskContract`,
`Delegation`, launch reservation, handoff, and event records only where a
protocol-neutral value is missing. Every adapter must normalize protocol state
into the existing Noesis task/attempt identity and lifecycle. Native/local
remains the regression baseline. ACP is implemented first, for one explicitly
selected local specialist over a bounded stdio process. A2A is deferred until a
named remote service creates a requirement that native/local or ACP cannot
satisfy.

**Protocol definitions.** ACP here means **Agent Client Protocol**, not IBM's
former Agent Communication Protocol. It is a client-to-worker session protocol.
A2A is the interface to independently deployed agent services. Discovery is not
authorization, and A2A is not required for local profiles.

**Consequences.**

- Noesis task, attempt, approval, budget, lease, review, and acceptance records
  remain authoritative.
- Protocol session/task/context identifiers are correlations, not identities or
  grants of authority.
- Adapter failure never silently changes worker, profile, provider, model,
  transport, endpoint, or approval context.
- No delivery semantic is assumed: the adapter must not claim at-most-once,
  at-least-once, or exactly-once behavior without protocol- and worker-specific
  evidence. Idempotency, duplicate suppression, reconciliation, and manual
  disposition are required before retrying an ambiguous attempt.
- `cancel_requested`, `cancel_acknowledged`, `execution_stopped`, and
  `authority_revoked` are distinct facts. Transport cancellation is not proof
  of process termination.
- Passing mock protocol tests proves only contract behavior, not installed or
  production qualification.

**Rejected alternatives.** A second protocol-specific scheduler or ledger;
letting a worker or Agent Card authorize itself; making every profile an A2A
service; deploying a registry for one known endpoint; treating Hermes's inbound
ACP server as proof of a general outbound Noesis ACP client; using ACP/A2A to
bypass the blocked native canary.

### 9.3 Transport-neutral delegation contract

Map these values onto the current Noesis records before adding new storage:

| Concern | Existing authoritative surface | Minimal adapter-neutral addition or rule |
|---|---|---|
| Task and admitted attempt | `TaskContract.task_id`; `Delegation.attempt`, `attempt_epoch`, `assignment_epoch`, `contract_revision`, `baseline`; launch reservation | Add an immutable `adapter_kind` and an adapter correlation object only after admission. |
| Worker identity | `assignee_profile`, `runtime_profile`, launch attestation and profile manifest digest | Bind expected worker/profile identity plus adapter-specific attestation; mismatch quarantines the attempt. |
| Capability and scope | `required_capability`, `intent`, acceptance criteria, risk tier, labels | Add a normalized capability snapshot and allowed operations; protocol-advertised capabilities cannot expand it. |
| Inputs and disclosure | Structured prompt/handoff and workspace binding | Add input references, sensitivity class, allowed destination, and disclosure restrictions; do not embed secrets. |
| Approval and authority | `Approval`, risk policy, approved route, lease/revocation/quarantine | Permission requests are evaluated against the existing grant; absent or ambiguous authority denies or pauses. |
| Bounds | timeout/lease, retry limits, request/token budgets, approved provider/model, fallback denial | Add adapter request/concurrency/resource ceilings and remote cost reconciliation where applicable. |
| Progress and artifacts | `noesis.delegated-task/v1` messages, handoff, `LaunchResult.artifacts`, digests | Normalize progress, partial artifacts, source session/task ID, integrity digest, and collection error. |
| Cancellation and failure | cancellation lifecycle, process-group cleanup, quarantine, `cleanup_state`, `workspace_reusable` | Record request, acknowledgement, observed stop proof, authority revocation, and `uncertain` outcome separately. |
| Verification and acceptance | revision-bound spec/quality reviews and explicit orchestrator acceptance | Adapter-reported success can create a candidate only; it cannot set acceptance. |

The normalized adapter result must preserve at least: task ID, attempt ID,
assignment/attempt epoch, adapter kind, expected and observed worker identity,
protocol version, protocol session/task/context ID, progress sequence, artifact
references and digests, usage/cost when known, cancellation observations,
terminal disposition, and uncertainty reason. Unknown usage remains unknown,
never zero.

### 9.4 Repository-grounded current state and gap table

Confidence labels: **verified** means read directly from the named repository or
installed source during this amendment; **tested** means backed by the recorded
local test/run evidence; **unverified** means configuration or code exists but no
qualifying deployment evidence was observed.

| Area | Intended architecture | Implemented repository behavior | Installed runtime capability | Verified deployment / gap |
|---|---|---|---|---|
| Noesis authority | One control plane owns lifecycle and acceptance | **Verified:** `control_plane.py`, `models.py`, `store.py`, `delegation.py`, and policy code implement append-only state, approval/dependency gates, assignment and attempt fencing, leases, bounded retry, quarantine, structured handoff, revision-bound review, and explicit acceptance. The A2 types in `transport_contract.py` are projections only and add no store, scheduler, ledger, or event authority. | The Python control plane and local profile resolver exist. | **Tested:** 444 orchestrator tests pass on the current offline candidate. No persistent schema changed. Independent review remains pending. |
| Native/local execution | Explicit selected profile through a governed adapter | **Implemented and tested offline:** `RuntimeAdapter`/`HermesCliAdapter` retain the existing launch behavior; `launch_request()` maps the transport-neutral request onto that path. Existing profile attestation, fallback denial, provider/model and lease binding, spawn authority checks, cleanup uncertainty, artifact collection, review, and acceptance tests remain green. | **Verified historical source/runtime:** installed Hermes v0.22.1, source revision `297552a9d646119871391d202509822cd86330df`; direct zero-inference profile attestation succeeded earlier. This slice did not invoke it. | **BLOCKED for model-facing use:** toolset exposure and installed plugin/source drift remain unresolved. The credential root cause also remains unresolved: route-specific absence, exhaustion, cooldown, expiry, provider/model mismatch, or profile/store mismatch has not been distinguished, and the credential-pool log is not proof of the generic startup error. Restoration was verified only at the historical rollback checkpoint; later unrelated `codebase_memory` config drift remains separately recorded. No retry is authorized. |
| ACP worker/server side | A selected worker exposes session semantics | No Noesis ACP adapter exists. `noesis-ansible/roles/hermes_acp` describes a service, config, registry registration, and validation. | **Verified source capability:** installed `acp_adapter/session.py` persists and restores Hermes ACP sessions and binds cwd; Hermes documents `hermes acp`. This is worker/server-side capability, not a Noesis outbound client. | **Unverified:** `noesispraxis_enable_hermes_acp` defaults false; role installation uses `latest`, ignores material errors, assumes a Linux binary/service and HTTP health around a stdio adapter. No active service was proven. |
| ACP client side | General client maps session lifecycle to one Noesis attempt | No such adapter exists in `noesis-agent-stack`. | **Verified source capability, limited:** `agent/copilot_acp_client.py` is an outbound stdio client specifically for `copilot --acp`; it sends ACP protocol version 1 initialization, creates a new session, submits a prompt, consumes updates, bounds the whole session by a timeout, and terminates the subprocess. It currently cancels permission requests, has no durable Noesis correlation, no session continuation in this path, no explicit protocol cancel exchange, and may continue on the session default if requested model selection fails. | Not qualified as a general ACP adapter and not suitable unchanged for the Noesis authority boundary. Exact protocol-version and worker compatibility remain to be verified. |
| A2A outbound | Approved remote service behind Noesis adapter | No Noesis A2A adapter exists. Registry roles and historical plans exist in `noesis-ansible`. | **Verified source capability:** installed `plugins/platforms/a2a/tools.py` can discover Agent Cards and send A2A v1.0 `SendMessage` calls to configured peers; it also accepts direct URLs and contains model-facing fan-out helpers. | **Unverified and currently too permissive for Noesis:** discovery/call paths are not bound to Noesis admission, allowlists, attempt identity, budgets, QA, or acceptance. No approved remote target was identified. |
| A2A inbound | Optional service interface, separately governed | No Noesis control-plane integration. | **Verified source capability:** installed `plugins/platforms/a2a/adapter.py` is an inbound HTTP server with Agent Card, authenticated/trusted-peer checks, task/status/cancel methods, and gateway-session routing. | No active/exposed service was verified. Inbound support does not prove the required outbound Noesis adapter. |
| Registries and deployment | Reproducible, pinned, fail-closed deployment only when justified | `noesis-ansible` has ACP/A2A registry roles, playbooks, endpoints, rollback hooks, and component toggles. | Files and defaults exist; broad `noesispraxis_enable_acp/a2a` flags are true in several inventories while the Hermes ACP role flag is false by default. | **Unverified:** registry entries and toggles are plans/configuration, not deployment proof. Several validations/install steps use `ignore_errors`; registry deployment is not required for the first known ACP endpoint or one future A2A endpoint. |
| NoesisPraxis relationship | Human-facing intake may lead into the Noesis control plane; authority stays with orchestrator | **Verified:** `noesis-praxis` is not an entry in `profiles/noesis-roster.yaml` or `platform/runtime-reconciliation.yaml`. The current roster consolidates Hermes-Core into `noesis-orchestrator`, mapped historically to the default profile. `agents/noesis-praxis/` is retained as a council/aeon skill/runbook and legacy identity; `noesis-ansible` also uses NoesisPraxis as stack/host naming. | Some generated/live profile material still carries the NoesisPraxis persona and remote host name. | No evidence establishes a separate deployed NoesisPraxis orchestration authority. In this target architecture, “Human / NoesisPraxis” is an intake/persona boundary, not a new scheduler, profile requirement, or service grant. |
| Evidence levels | Mocks, processes, installation, live calls, and distributed operation are distinct | **Implemented and tested:** the synthetic in-memory `noesis-fake-acp/v1` fixture covers capability negotiation, correlated session creation, prompt submission, ordered updates/results, permission denial, malformed/unsupported messages, early exit, timeout, cancellation observations, partial/uncertain output, duplicate suppression, and explicit restart reconciliation. It has no model, credential, network, subprocess, tool, or operational capability. | Protocol-related source is installed but was not exercised by this slice. | 14 new contract tests pass; this is mocked contract evidence only. Fixture framing, exact method names, continuation, permission/cancellation acknowledgement semantics, and general Hermes outbound ACP compatibility remain unverified. |

Source anchors for this table include
`orchestration/orchestrator/app/{control_plane,models,store,delegation,launcher}.py`,
`profiles/noesis-roster.yaml`, `platform/runtime-reconciliation.yaml`,
`agents/noesis-praxis/SOUL.md`, the `noesis-ansible` ACP/A2A roles and
inventories, installed Hermes `agent/copilot_acp_client.py`,
`acp_adapter/session.py`, `plugins/platforms/a2a/{tools,adapter,protocol}.py`, and
the matching Hermes ACP/A2A documentation at
<https://hermes-agent.nousresearch.com/docs/user-guide/features/acp>,
<https://hermes-agent.nousresearch.com/docs/developer-guide/acp-internals>, and
<https://hermes-agent.nousresearch.com/docs/user-guide/messaging/a2a>.

### 9.5 Adapter capability and failure requirements

Before an adapter can qualify, document and test its exact support for:

- identity/profile attestation and working-directory/environment binding;
- initialization and capability/version negotiation;
- session/task creation, continuation, reconnect, and persistent state;
- prompt/message submission, progress ordering, results, and partial artifacts;
- approval-bound permission handling;
- deadline, request, concurrency, resource, token, and cost ceilings;
- cancellation request, acknowledgement, observed execution stop, and authority
  revocation as separate states;
- restart safety, idempotency, duplicate suppression, ambiguous-outcome
  reconciliation, and retry eligibility;
- artifact provenance and integrity; and
- final independent verification and explicit Noesis acceptance.

No adapter may claim exactly-once execution. An uncertain remote or local
outcome blocks automatic redispatch until reconciled. A transport cancellation
acknowledgement is not proof that a subprocess, model request, or remote task has
stopped. Authority can be revoked even when stop is unconfirmed; the attempt then
remains quarantined/uncertain.

### 9.6 Bounded phased backlog

| Phase / item | Dependency | Deliverable | Acceptance criterion | Authorization state |
|---|---|---|---|---|
| A1. Freeze ADR and interface map | This amendment | ADR-PDA-001 plus current-state/gap table | Operator can trace every proposed value to an existing authority surface or a justified additive field. | **Done by documentation amendment.** |
| A2. Specify normalized adapter types | A1 | Typed adapter request, progress event, result, cancellation observation, and capability matrix | No protocol field can mutate approval, lease, review, or acceptance; invalid-state tests pass. | **Implemented in bounded offline candidate; 14 contract tests pass; no persistent schema change; independent review pending.** |
| A3. Native conformance adapter | A2 | Wrap/refine current native path against the normalized types without behavioral change | Existing native suite stays green; explicit profile/route/fallback, lease, quarantine, artifact, and acceptance behavior is unchanged. | **Implemented in bounded offline candidate; focused 93/93 and full 444/444 pass; independent review pending.** |
| B1. ACP compatibility fixture | A2 | Synthetic in-memory ACP-like peer and transcript fixtures with pinned assumptions | Covers initialize/capabilities, session creation, prompt, updates/result, permission request, timeout, cancellation behavior, malformed messages, early exit, and uncertain cleanup without model/network/process use. | **Implemented and tested at mocked-contract level only; installed Hermes assumptions remain unverified; independent review pending.** |
| A4. Offline-slice independent review | A2, A3, B1 exact candidate | Sentinel authority/spec review, then Skeptic quality/security review of identical candidate contents | Sentinel PASS precedes Skeptic; verdicts bind to the exact content manifest; no historical verdict is reused. | **Prepared for operator-mediated review; no authorized in-session review route is available under the no-provider/no-worker limit.** |
| B2. Minimal ACP stdio adapter | B1, A3 | Noesis-owned outbound ACP adapter for one selected Hermes specialist | Explicit worker/profile attestation; one admitted attempt ↔ one ACP session; no fallback; bounded process/request budget; fail-closed permissions; durable correlation; partial evidence retained. | Not authorized. |
| B3. Independent review | B2 | Sentinel authority/spec review, then Skeptic quality/security review of the exact candidate | Sentinel PASS precedes Skeptic; verdicts are candidate-revision-bound; no historical verdict is reused. | Separately gated. |
| C1. Installed-runtime ACP test | B3, reproducible installation plan | Offline installed-process test with no model request | Proves command/profile binding, negotiation, cancellation/cleanup, rollback, and hashes on the installed version. | Separate approval required. |
| C2. Bounded live ACP canary | C1, resolved credentials and exact canary contract | One synthetic task with parent/worker request and cost ceilings | Tool exposure, profile attestation, session correlation, ledger events, artifact digest, usage/cost, stop proof, independent verification, and restoration all pass. | Explicit live/inference/spend approval required. |
| D1. A2A requirement record | Named independent service exists | Target, owner, Agent Card/version, endpoint, auth, disclosure class, capability, operational value, and why native/ACP cannot satisfy it | Operator approves the named service and data boundary before any implementation. | Deferred. |
| D2. Explicit-endpoint A2A adapter | D1, A2A threat/data review | Noesis adapter to one allowlisted endpoint; no registry dependency | Agent Card validated as untrusted metadata; configured identity/destination/auth wins; durable task correlation, status/artifacts/cancel/uncertainty and budgets map into Noesis. | Deferred and separately gated. |
| D3. Discovery/registry | Multiple approved dynamic services and demonstrated operational need | Governed discovery with allowlists and lifecycle ownership | Registry adds measurable value; discovery never grants authorization; deployment and rollback are reproducible. | Not triggered. |

Tests must report five separate evidence levels: (1) mocked protocol contracts,
(2) real local process/session behavior, (3) installed-runtime integration,
(4) live model-facing delegation, and (5) verified distributed operation. A
lower level never implies a higher one.

### 9.7 Smallest proposed ACP implementation slice

**Worker:** `noesis-forge`, because it is an existing bounded implementation
specialist with an explicit profile identity and an independent review path.
This selection is a compatibility target, not authorization to invoke it.

**Transport:** one child stdio process exposing Hermes's ACP worker interface,
with the exact installed command/profile syntax and negotiated protocol version
determined by offline fixture and parser validation before implementation. Do
not infer compatibility from the presence of `acp_adapter/session.py`. Do not
reuse the Copilot-specific client unchanged.

**Slice boundary:**

1. retain existing specialist selection, task proposal, approval, dependency,
   budget, lease, reservation, and fallback-deny gates;
2. select `adapter_kind=acp_stdio` explicitly—never as automatic fallback;
3. spawn only the expected Hermes executable/profile/working directory under a
   process group and remaining lease;
4. negotiate the pinned supported ACP version and required capabilities;
5. create one new ACP session and durably bind its session ID to the existing
   task/attempt/epoch and adapter process identity;
6. submit one synthetic/non-secret prompt and normalize ordered updates,
   terminal result, partial artifacts, and errors;
7. deny permission requests not already covered by the Noesis grant; the first
   slice may fail closed on every interactive permission request rather than
   auto-approve;
8. on timeout/cancel, request protocol cancellation when supported, revoke
   Noesis authority, terminate the process group, verify whether execution
   stopped, retain partial evidence, and mark uncertainty if stop is not proven;
9. produce a candidate only—existing review and acceptance gates remain
   unchanged; and
10. leave native/local behavior and schema replay compatible.

**Deliberately excluded from the first slice:** session continuation across
attempts, remote ACP transport, multiple workers, dynamic worker discovery,
model/provider fallback, automatic permission approval, parallel fan-out,
registry deployment, A2A, production service activation, and live inference.

### 9.8 Concrete trigger for A2A work

A2A work begins only when the operator names and approves an independently
deployed agent service that must preserve state or operate outside the local
Hermes/ACP process boundary, and documents a required capability or
interoperability outcome that the native/local and ACP paths cannot meet. The
requirement record must include the service owner, approved endpoint,
Agent Card/protocol version, authenticated identity, capability, data
classification and disclosure policy, expected remote state/lifetime,
cancellation/status/artifact support, resource and cost ceilings, failure
isolation, and operational benefit.

One configured endpoint starts with explicit allowlisted configuration. A
registry/discovery service is triggered only when multiple approved dynamic
services make static endpoint ownership materially inadequate. An Agent Card or
registry entry alone never triggers dispatch or grants authorization.

### 9.9 Authorization boundaries and unresolved blockers

**Authorized after this amendment:** one bounded offline development slice for
A2, A3, and B1, including repository/document edits, synthetic tests, and
operator-mediated Sentinel then Skeptic review of the exact candidate. That
slice is implemented and locally tested; review remains pending.

**Still not authorized:** the real Hermes ACP worker adapter; dependency or
package installation; plugin copy/activation; service start/restart/enablement;
endpoint exposure; credential, provider, model, fallback, route, or
exhaustion-state change; live ACP/A2A/native worker invocation; model inference;
spending; production apply; commit; push; merge; or destructive rollback.

**Unresolved blockers:**

1. the native specialist canary remains **BLOCKED** on installed-runtime
   toolset exposure and installed plugin/source drift. The credential root cause
   remains unresolved: the available evidence does not distinguish route-specific
   absence, exhaustion, cooldown, expiry, provider/model mismatch, or
   profile/store mismatch, and the credential-pool line does not prove the
   generic startup error. ACP/A2A are not remedies for those issues. Restoration
   was verified at the historical rollback checkpoint; the later unrelated
   `codebase_memory` drift remains separately recorded and is neither adopted nor
   reverted here;
2. A2/A3/B1 implementation is locally green but has no Sentinel disposition;
   Skeptic is therefore still gated. The exact candidate review packet must be
   hand-carried by the operator because this slice forbids provider requests and
   real worker invocation, and no substitute route is authorized;
3. the fake fixture pins `noesis-fake-acp/v1` assumptions, but its framing,
   exact method names, continuation behavior, permission/cancellation
   acknowledgement semantics, and the selected worker's capability intersection
   remain unverified against installed Hermes;
4. installed Hermes has inbound ACP capability and a Copilot-specific outbound
   shim, but no verified Noesis-governed general ACP client path;
5. fake permission, cancellation, duplicate, restart, partial-output, timeout,
   and unknown-usage semantics are specified and tested, but continuation,
   process stop proof, durable real-session correlation, retry eligibility, and
   budget/cost reconciliation remain to be verified for the selected real worker;
6. `noesis-ansible` ACP/A2A material is unqualified: versions are not adequately
   pinned, some critical errors are ignored, deployment ownership conflicts and
   lifecycle assumptions need remediation, and no live registry/service was
   proven;
7. no named independent A2A service or compelling interoperability requirement
   exists, so A2A implementation and registry deployment remain deferred;
8. the suspended macOS ARM64 prodqual candidate
   `aee125e4a39fa0b18f270195bedfc2b51439a527` remains unqualified; there is no
   Sentinel receipt, Skeptic was not dispatched, and its gates are not reopened
   by this amendment; and
9. active runtime configuration changed after the specialist-canary rollback
   checkpoint through an unrelated `codebase_memory` addition of unresolved
   provenance; this amendment does not reverse or adopt that change.

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
| One Noesis-owned transport-neutral adapter contract | ✅ implemented as non-authoritative projections; no persistent schema/store change; review pending |
| Native path conformance with no regression | ✅ focused 93/93 and full 444/444 pass; no intended behavior change; review pending |
| ACP mocked protocol contract | ✅ 14/14 synthetic in-memory tests pass; assumptions explicitly unverified against installed Hermes |
| ACP real local process/session behavior | ⏳ not tested; real adapter not implemented or authorized |
| ACP installed-runtime integration | ⛔ separately gated; not tested |
| ACP live model-facing delegation | ⛔ separately gated; not authorized |
| A2A approved independently deployed target | ⏸ deferred; trigger not met |
| Verified distributed ACP/A2A operation | ⏸ not claimed |

**Current disposition:** the bounded A2/A3/B1 offline slice is implemented and
locally green at mocked/native-contract evidence level. It is **not independently
accepted**: Sentinel review is pending and Skeptic remains gated on Sentinel PASS
for the identical candidate contents. The real Hermes ACP adapter, installed
process qualification, live delegation, A2A, and production work remain held.
The native specialist-routing canary stays **BLOCKED**; restoration was verified
at the historical rollback checkpoint, and the later unrelated configuration
drift remains separately recorded. No live retry is authorized.

---

## Copy-ready handoffs (staged 2026-10-11, operator-mediated)

All five handoffs are untracked files in `workspace/` (gitignored by design;
operator hand-carries from the local filesystem). Reviewer dispatch and
HARDENING-1 implementation dispatch are NOT performed from this profile.

| # | Artifact | Purpose | Path |
|---|---|---|---|
| A | Sentinel spec-review prompt | Hand-carry to a fresh `noesis-sentinel` context. Verifies the planner against the reproducible snapshot digest `a4519bd6…` and the canonical recipe. | `workspace/loop-graph-slice/sentinel-request.md` |
| B | HARDENING-1 Forge implementation prompt | Copy-paste into a fresh `noesis-forge` session in the operator's own context. Implements file-integrity attestation in the dev-source checkout at `553a06b…`, opt-in via `expected_candidate_digest`, ~50 LoC + ~150 LoC tests. | `workspace/a6-v3-prototype-install/HARDENING-1-FORGE-IMPLEMENTATION-HANDOFF.md` |
| C | Skeptic quality/security prompt | After Sentinel PASS. Hand-carry to a fresh `noesis-skeptic` context. Adversarial lens; verifies scope-binding soundness, citation integrity, fail-closed completeness. | `workspace/loop-graph-slice/skeptic-request.md` |
| D | Lease-lifecycle approval brief | One-line operator decision. Changes authority semantics (additive `execution_released_at` + monotonic `attempt_epoch`), review-gated, no new provider/budget. | `workspace/loop-graph-slice/LEASE-LIFECYCLE-APPROVAL-BRIEF.md` |
| E | LLM-wiki gap map | Source-backed reference for the production release path. 6 critical-path priorities; 10-item sequenced backlog; only exact authorizations needed. | `workspace/a6-v3-prototype-install/LLM-WIKI-GAP-MAP.md` |

Supporting (canonical recipe, used by both reviewer prompts and the operator):

| File | Purpose |
|---|---|
| `workspace/loop-graph-slice/SNAPSHOT-DIGEST-RECIPE.md` | Canonical recipe for the planner snapshot digest (legacy `f2127745…` was non-reproducible; canonical `a4519bd6…` is). |
| `workspace/loop-graph-slice/PLANNER-REVIEW-PACKET.md` | Index of the Sentinel + Skeptic prompts; updated with the reproducible digest. |

**Open material authorizations** (any of which unblocks the next step):
- (a) Approve the lease-lifecycle implementation per the brief.
- (b) Approve a push target for the HARDENING-1 commit (Hermes dev-source
  `origin` is `file:///Users/elvis/.hermes/hermes-agent`; HELD).
- (c) Operator-mediated hand-carry of the Sentinel prompt (and Skeptic
  after PASS) to fresh reviewer contexts.
- (d) Operator-mediated hand-carry of the HARDENING-1 Forge prompt to
  the operator's own `noesis-forge` context.

Held gates (unchanged): plugin canary/activation, service restart, Venice
key rotation, noesis-ansible G-1/G-2/G-3 scoping, Phases 3–4, production
install, merges, public exposure, spending, new provider/lane.
