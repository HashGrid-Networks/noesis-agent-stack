# Deployment Verification — noesis-agent-stack

- Date (America/Denver): 2026-10-06
- Operator: Elvis (approvals recorded in-session)
- Branch/SHA: `main` @ `478fd75` (== origin/main; tree clean except untracked `profiles/live/.DS_Store`)
- Host: macOS 27.2, local; Python 3.11.13 via repo-local `uv` venv (`.venv/`); zero live inference spend (offline-only authorization)
- Verdict: **PARTIALLY DEPLOYED**

## Environment / versions

| Item | Observed |
|---|---|
| Hermes runtime | v0.21.5+8076.g85db7c3 at ~/.hermes |
| Repo venv | `.venv/` (uv 0.8.23, CPython 3.11.13) — created this run, approved |
| fastapi / pydantic / pytest | installed in venv only (not system Python) |
| Broker test suite | `pytest orchestration/broker/tests -q` → **28 passed**, 0.10s |
| Orchestrator test suite | `pytest orchestration/orchestrator/tests -q` → **82 passed**, 4.69s |
| Inference-routing validator | `scripts/validate_inference_routing.py` → PASSED gate; drift profile below |
| Honcho profile validator | `scripts/validate-honcho-profiles.py` → `ok: true`, all 16 roster profiles defined repo-side |
| Agency names check | `scripts/check-agency-names.py` → FAIL (missing `~/tools/agency-agents`) |

## Inference-routing reconciliation (repo declaration vs live runtime)

Repo `platform/inference-routing.yaml` validator output: 4 aligned / 13 drift / 1 missing
repo declaration / 4 runtime-not-observed; resolutions: 10 blocked, 12 needs_evidence.
Only `KIMI_CODE` lane is `approved`.

Live `~/.hermes/config.yaml` observed (sanitized):
- `model.default = z-ai/glm-5.2`, `model.provider = nous`
- `fallback_providers = [moa, kimi-coding, openai-codex]`

Conflicts (reported, NOT changed — live config edits are approval-gated):
1. Live default provider `nous` is declared dead in `profiles/noesis-roster.yaml`
   (T12a, 2026-09-16: "nous key dead"); roster migrated core/scribe/grid lanes to kimi.
2. `~/.hermes/profiles/model-profiles.yaml` declares anthropic lanes
   (claude-3-5-haiku, claude-sonnet-4-5, …) with cost guardrails; none of these
   providers appear in live fallback config. `VENICE_PRO`, `XAI_GROK`,
   `CHATGPT_PRO` lanes are `blocked_missing_configuration` per validator.
3. Anthropic lanes in model-profiles.yaml conflict with the repo-approved-lane
   model (only KIMI_CODE approved). Either the live file is stale or the repo
   policy is; operator decision required.

## Broker runtime evidence (live localhost run)

Invocation (per README): `uvicorn app.main:app --host 127.0.0.1 --port 8787`
Run window: 2026-10-06 ~22:47–22:54 MDT. Log: `/tmp/broker-e2e.log`.

| Test | Command (shape) | Result | Status |
|---|---|---|---|
| Health | `GET /v1/health` | `{"status":"ok","workers_healthy":4,"workers_total":4}` | PASS |
| Worker registry | `GET /v1/workers` | 4 workers with read/write scopes enumerated | PASS |
| Valid job submit | `POST /v1/jobs` (research-openclaw, vault-scoped) | HTTP 202, job_id `e14fb662-…` returned | PASS |
| Job status | `GET /v1/jobs/{id}` | 200, state persisted | PASS |
| Idempotency | same `idempotency_key` resubmitted | HTTP 409 `Idempotency key conflict` | PASS |
| Unknown worker | worker=`rogue-agent` | HTTP 400 schema enum rejection | PASS |
| Schema violation | missing required fields | HTTP 400 | PASS |
| Scope violation | write_scope ⊄ read_scope | HTTP 422 `Write scope contains paths not in read scope` | PASS |
| Cancel | `POST /v1/jobs/{id}/cancel` | 200, status=`cancelled`, persisted | PASS |
| Event trail | `GET /v1/jobs/{id}/events` after submit+cancel | **empty array** | GAP |

Broker gaps: (a) submit/cancel produced **no events** in the event stream —
HEARTBEAT.platform.md requires a typed event trail per job; (b) 400 responses
echo the full JSON schema document (verbose error surface, no secrets observed,
but should be trimmed); (c) deprecation warning: FastAPI `regex=` → `pattern=`.

## Orchestrator control-plane evidence (offline, tmp ledger)

Driver: `Orchestrator(tmp/tasks.jsonl)` via app API. All policy refusals raised
`PolicyViolation` as designed.

| Test | Result | Status |
|---|---|---|
| propose → approve → enqueue → claim → start → handoff → succeed | full lifecycle succeeded | PASS |
| Idempotent re-propose (same idempotency_key) | same task_id returned, no duplicate | PASS |
| r3 + `destructive_data_operation`, no approval | `approval_required=True`; claim blocked pre-approval | PASS |
| Unknown irreversible op vocabulary | rejected at task construction (`ValueError`) | PASS |
| Claim by wrong profile (`noesis-scribe` on forge task) | blocked (`claim_by_wrong_profile`) | PASS |
| Capability mismatch (`code_execution` for forge) | blocked at admission (`capability_mismatch`) | PASS |
| Succeed with no handoff | blocked (`handoff_required`) | PASS |
| Handoff missing `verification_result` | blocked (`handoff_incomplete`) | PASS |
| Emergency stop → dispatch | blocked (`dispatch_blocked`) | PASS |
| Ledger replay after restart | task state `succeeded` persisted; 7 ledger events | PASS |
| **E-stop persistence across replay** | **breaker reads `manually_tripped=False` after restart** | **FAIL** |

Orchestrator gaps: (a) **emergency-stop does not survive process restart** —
manual circuit-breaker trip is not written to the append-only ledger, so a
restarted control plane resumes dispatch despite an operator e-stop; (b)
admission validates profiles **only** against `profiles/noesis-roster.yaml` —
the deployed Hermes profile `coder` (in `platform/agent-registry.yaml` and live
at `~/.hermes/profiles/coder`) is rejected as `unknown_profile`, and
`claude-code-worker` is likewise not roster-visible. Cross-registry unification
needed (registry.py is described as roster+agents/*/agent.yaml but behavior
observed is roster-only for admission).

## Bridge contract evidence (offline schema validation)

`contracts/bridge/task.schema.json` + `result.schema.json` exercised with
jsonschema Draft2020-12:
- Valid task/result payloads: both VALID.
- Negatives (missing `type`, `description` minLength, additionalProperties,
  incomplete result): all correctly REJECTED.
- Live bridge execution (real Claude Code worker inbox/outbox): **NOT TESTED** —
  blocked by offline-only authorization (would invoke a paid/lane executor).

## Security / recovery summary

- r3 gating, approval gates, capability/scope enforcement, idempotency: PASS (evidence above).
- Bounded retries + circuit breaker: covered by 82-test suite (`test_timeout_retry_breaker_escalation`).
- Recovery: ledger replay PASS; **breaker state loss on restart = FAIL**.
- Credential handling: no secrets printed during any test; validators are
  repo-local by design. Live credential verification: NOT TESTED (blocked).
- Worker restart durable-state test: NOT TESTED beyond ledger replay (no live workers authorized).

## Documentation drift (report, do not silently fix)

1. `README.md` "Status" and `TODO.md` Phase 2–6 marked NOT STARTED, but
   `platform/orchestrator.yaml`, `platform/routing.yaml`, `platform/workflows/`,
   orchestrator control plane, and CI workflow `.github/workflows/inference-routing-policy.yml`
   all exist and post-date the docs. TODO/README are stale.
2. `EVALS.platform.yaml` BROKER-001 expects `"state": "queued"`; broker returns
   `"status": "pending"` for a submitted job. Field name/state vocabulary mismatch.
3. `ROUTING-SMOKE.md`: 0/18 rows recorded — no routing smoke evidence exists.

## Approvals obtained

1. Full mission Phases B+C — approved.
2. Repo-local uv venv + dependency install — approved.
3. Live inference spend: **offline-only** — live agent tests (routing smoke,
   bridge execution, live broker worker run) remain BLOCKED.

## Rollback / emergency-stop

- Broker test instance: `kill <uvicorn pid>` (already stopped; no persistent state — in-memory store).
- Venv: `rm -rf /Users/elvis/projects/noesis-agent-stack/.venv` (reproducible via `uv venv .venv && uv pip install -r …/requirements.txt`).
- Orchestrator test ledgers: temp dirs under `$TMPDIR/orch-e2e-*`, disposable.
- Live host changes: **none made**. `~/.hermes` untouched. Git: no commits, no pushes.
- Emergency stop for the orchestrator control plane (when deployed):
  `Orchestrator.emergency_stop(operator=…, reason=…)` — note the persistence
  gap above; verify breaker state after any restart.

## Exact next actions (operator decisions required)

1. Decide live default model/provider: keep nous `z-ai/glm-5.2` (contradicts
   roster T12a) or cut over to kimi per repo-approved lane. Then re-run
   `validate_inference_routing.py` and reconcile `model-profiles.yaml`.
2. Decide whether `coder` + `claude-code-worker` enter the noesis roster or the
   orchestrator admits from `platform/agent-registry.yaml` as well.
3. Approve a fix change-set for: broker event trail on submit/cancel/transition;
   e-stop persistence in orchestrator ledger; error-response trimming.
4. Approve an inference spend cap to unblock: ROUTING-SMOKE 18 rows, bridge
   live run, broker live worker execution.
5. Restore `~/tools/agency-agents` checkout or re-scope `check-agency-names.py`.
6. Update README/TODO status sections to match implemented Phases 1–2 + platform configs.

---

# Remediation Pass — 2026-10-06/07 (America/Denver)

Authorization received: repository remediation + live Noesis host deployment.
Explicitly still blocked: paid inference calls, remote hosts, public exposure,
git commits/pushes. Baseline re-verified: `main` @ `478fd75` (unchanged), zero
tracked modifications at start; untracked = this report + `profiles/live/.DS_Store`.

## Baseline reconciliation corrections

- Roster actually defines **17** profiles (header claims 16): wave 1 = 7
  (core→default, steward, cartographer, forge, sentinel, scribe, signal),
  wave 2 = 5 (substrate, tracer, ledger, grid, quill), wave 3 = 5
  (orchestrator, advocate, herald, architect, skeptic). Earlier "8 applied /
  9 staged" conflated identities: applied = 7 roster identities (6 dirs +
  default-as-core) **plus** the non-roster `coder` profile = 8 profile dirs.
- `~/.hermes/profiles/noesis-orchestrator` does **not** exist (wave 3, correctly
  unapplied). Honcho validator covers 16 of 17 (orchestrator has no honcho
  profile definition; validator still reports ok — its expected set is derived
  from `honcho/profiles/`, not the roster; recorded as a coverage note, not a pass).
- "Clean tree" vs report: report was untracked (never committed); no tracked
  file was modified in the previous pass.

## Defect 1 — Emergency stop lost on restart (Priority 0)

| Field | Value |
|---|---|
| Original finding | Orchestrator breaker trip held in memory only; restart silently reopened dispatch |
| Reproduction | `emergency_stop()` → new `Orchestrator(ledger)` → `manually_tripped=False` (observed previous pass) |
| Root cause | `emergency_stop`/`resume` never wrote to the append-only ledger; startup never reconstructed breaker state |
| Changed files | `orchestration/orchestrator/app/store.py` (durable control events, fail-closed replay, `replay_error`), `orchestration/orchestrator/app/control_plane.py` (`_restore_breaker_state`, durable stop/resume, operator-identity requirement), `orchestration/orchestrator/tests/test_breaker_persistence.py` (new, 8 tests) |
| Regression tests | 8/8 pass: stop→restart blocked; stop→resume→restart dispatch works; last-event-wins; malformed ledger fails closed; stop write-failure raises AND fails closed; operator identity required; running work completes after stop while new dispatch blocked |
| Runtime evidence | 101/101 orchestrator tests; ledger replay verified with breaker events interleaved with task records |
| Remaining limitations | Consecutive-failure counter still not persisted (only manual stop/resume); acceptable per lease semantics |
| Status | **FIXED** |

Stop semantics (documented in code): stop blocks NEW dispatch (`claim`);
`enqueue` remains allowed (queueing is planning); already-running work is left
to its lease and completes normally.

## Defect 2 — Registry admission rejected deployed lanes

| Field | Value |
|---|---|
| Original finding | `coder` (and bridge worker) rejected as `unknown_profile`; admission read only the roster |
| Root cause | Single-source registry; no lane concept from `platform/agent-registry.yaml` |
| Changed files | `orchestration/orchestrator/app/registry.py` (roster+lane merge, alias resolution, fail-closed conflict detection, activation states), `orchestration/orchestrator/app/policy.py` (`lane_not_activated` gate; updated `unknown_profile` message), `platform/agent-registry.yaml` (`orchestration_lanes` allowlist, `aliases`, `claude-code-worker` entry), `orchestration/orchestrator/tests/test_registry_admission.py` (new, 11 tests) |
| Regression tests | 11/11 pass: coder/bridge admitted with declared capabilities; wrong capability rejected; registered-but-not-allowlisted → unknown; disabled lane → not activated; alias resolution; alias shadowing/unknown target → load fails closed; roster-lane name collision fails closed; supervisor-role lane cannot execute; real repo config admits both lanes |
| Runtime evidence | Real-repo-config test drives propose→approve→enqueue→claim for `coder`; 101/101 suite |
| Remaining limitations | Roster staged (wave 2/3) profiles remain admissible by design (tests exercise them); wave gating enforced at profile-application time, not admission |
| Status | **FIXED** |

## Defect 3 — Broker event stream empty

| Field | Value |
|---|---|
| Original finding | `GET /v1/jobs/{id}/events` returned `[]` after submit and cancel |
| Root cause | `main.py` never called `store.append_event`; additionally the `Event` model did not conform to `events.schema.json` (had `message`/`metadata`, lacked required `type`/`payload`) |
| Changed files | `orchestration/broker/app/models.py` (Event conformed to contract), `orchestration/broker/app/main.py` (`_emit` with contract validation + `exclude_none`; events on submitted/cancelling/cancelled/completed/failed; structured errors; bounded rejection audit + `GET /v1/audit/rejections`; FastAPI `regex`→`pattern`), `orchestration/broker/app/store.py` (rejection audit), `orchestration/broker/tests/test_events.py` (new, 8 tests), `orchestration/broker/tests/test_broker.py` (event assertion updated from the defective empty-stream expectation) |
| Regression tests | 35/35 broker tests; events conform to `events.schema.json` (validated in-test); rejection audit sanitized; duplicate submission creates no phantom job or events |
| Runtime evidence | Live localhost run 2026-10-07T00:16Z: ordered stream `broker.job.submitted → cancelling → cancelled` with correlation IDs; 409 duplicate; structured 400 `{"code":"schema_validation_failed",...}`; audit recorded `idempotency_key_conflict` |
| Remaining limitations | **Durability: events are in-memory, per-process.** Stated in code and report — not restart-durable. Durable records remain palace receipts + supervisor ledgers |
| Status | **FIXED** (with honestly-stated durability guarantee) |

## Defect 4 — Verbose 400 responses

| Field | Value |
|---|---|
| Original finding | 400 responses embedded the full JSON Schema document |
| Root cause | Raw exception string passed as `detail` |
| Changed files | `orchestration/broker/app/main.py` (structured `{code, message, correlation_id}`; schema dumps, stack traces, and raw payloads excluded) |
| Runtime evidence | HTTP check: `{"detail":{"code":"schema_validation_failed","message":"job payload failed schema validation: ValidationError","correlation_id":null}}` |
| Status | **FIXED** |

## Defect 5 — EVALS BROKER-001 contract drift

| Field | Value |
|---|---|
| Original finding | Eval expected `state: queued`; broker returns `status: pending` |
| Root cause trace | Authoritative `contracts/broker-api/job.schema.json` defines `status` (enum includes `pending`), no `state` field → implementation contract-conformant; eval text stale. No consumers of the eval text found |
| Changed files | `EVALS.platform.yaml` (BROKER-001 criterion corrected with rationale) |
| Compatibility impact | None — no automated consumers; contract unchanged |
| Status | **FIXED** (eval aligned to contract, not vice versa) |

## Defect 6 — Live config drift + conflated validation layers

| Field | Value |
|---|---|
| Original finding | Validator PASSED while live default ran dead nous route |
| Root cause | `validate_inference_routing.py` is repo-local by design; nothing checked live compliance |
| Changed files | `scripts/verify_live_config.py` (NEW, read-only, sanitized; separates repo-policy / live-compliance / credential-presence / inference-execution layers) |
| Live changes applied | `~/.hermes/config.yaml`: `model.default/provider` → `kimi-k2.7-code`/`kimi-coding` via `hermes config set` (platform-enforced path; direct file edit refused by tool guardrail — correct behavior). `~/.hermes/profiles/noesis-scribe/config.yaml`: `deepseek/deepseek-v4-flash`@nous → `kimi-k2.7-code`@kimi-coding per roster T12a. Backups: `~/.hermes/backups/deployment-20261006-2355/` (mode 700) |
| Verification | `verify_live_config.py` → **PASS** (default route approved + credentialed); `nous` credential pool count = 0 with recorded `last_auth_error` — dead-key claim corroborated |
| Remaining limitations | Per-profile drift surfaced for operator decision: `coder` (anthropic claude-sonnet-5), `live` (nous solar-pro4:free), `noesis-cartographer/sentinel/signal` (openrouter claude-sonnet-5) — anthropic/openrouter lanes explicitly left unresolved pending approval |
| Status | **FIXED** (default route); profile-level lanes pending approval |

## Defect 7 — Missing agency-agents checkout

| Field | Value |
|---|---|
| Original finding | `check-agency-names.py` FAIL: `~/tools/agency-agents` absent |
| Verification of intent | `agency/SOURCE` + `agency/ATTRIBUTION.md`: upstream `https://github.com/msitarzewski/agency-agents`, pinned commit `ad9264e309bd5e5422c04784372d7841b1e5d604` |
| Change applied | Restored checkout at `~/tools/agency-agents`, exact pinned commit (detached HEAD) |
| Verification | `check-agency-names.py` → **OK** |
| Status | **FIXED** |

## Documentation drift

- `README.md` Status: rewritten to distinguish implemented+offline-verified /
  deployed / inference-verified-live / staged (was: "specification and scaffold
  phase").
- `TODO.md` header: status banner added noting phase checklists lag the
  implementation; Phases 3–7 remain scaffolded.

## Post-remediation verification sweep (2026-10-07T00:10–00:18Z)

| Check | Result |
|---|---|
| `pytest orchestration/broker/tests` | 35 passed |
| `pytest orchestration/orchestrator/tests` | 101 passed |
| `scripts/validate_inference_routing.py` | PASSED (repo-policy layer) |
| `scripts/validate-honcho-profiles.py` | ok: true (16 profiles defined) |
| `scripts/check-agency-names.py` | OK (pinned upstream restored) |
| `scripts/verify_live_config.py` | PASS (live-compliance layer) |
| Broker live HTTP (loopback) | submit 202 + event, 409 dup, cancel 200 + 2 events, structured 400, audit trail |
| Broker/orchestrator processes left running | none (test instances stopped) |

## Evidence classification

- **OFFLINE evidence:** all pytest suites, validators, schema validations.
- **Non-billable live host evidence:** loopback broker HTTP run; live config
  reads; `hermes config` writes; agency checkout restore.
- **Inference-tested evidence:** NONE — remains blocked on budget approval.
  No ROUTING-SMOKE rows recorded; no live bridge/worker execution.

---

# Reconciliation Pass — 2026-10-07 ~01:20 MDT (read-only)

Directive: proceed toward narrowly scoped inference-verified deployment;
read-only reconciliation + approval packages only this pass.

## Verified current state
- Git: main @ 291eb3e == origin/main; tree clean; nothing pending.
- On-disk config changes present: ~/.hermes/config.yaml default =
  kimi-coding/kimi-k2.7-code (hermes' own versioned backups
  config.yaml.good.20261006-181416/181417 corroborate change time 18:14 MDT);
  noesis-scribe config = kimi-coding/kimi-k2.7-code. Full diff of config.yaml
  vs .bak = exactly the two model lines (z-ai/glm-5.2@nous → kimi-k2.7-code@kimi-coding).
- Processes: default gateway (PID 2061, `gateway restart`, started 13:48 MDT) +
  serve process (PID 79891, 13:40 MDT) — BOTH predate the 18:14 config change
  → loaded pre-change in-memory config (old nous default route). Coder gateway
  (PID 49528, 09:04 MDT) runs its own gateway on unchanged anthropic config.
  No broker/orchestrator processes (earlier "temporary services stopped" =
  uvicorn test instances only). Desktop app, photon sidecar, omh-menubar =
  platform infra.
- `hermes gateway status`: gateway is STANDALONE serving only `default`; all
  7 wave-1 noesis profile bots are NOT served (coder's own gateway blocks
  multiplex). Router plugin (agency-agents-router) NOT built/installed.

## New discrepancies vs previous report (not previously recorded)
1. Wave-1 fleet is applied-on-disk but not actually served by a gateway;
   ROUTING-SMOKE cannot run as documented (router plugin absent, bots unserved).
2. Photon credential conflicts: default shares PHOTON_PROJECT_ID/SECRET with
   all 7 noesis profiles; multiplexing would park adapters until per-profile
   tokens or profile_routes are configured.
3. Drift count correction: FIVE drifted profiles (report said "4", named 5):
   coder, live, cartographer, sentinel, signal.
4. Scribe config edit has NO .bak (gap; rollback = manual revert to
   deepseek/deepseek-v4-flash@nous).
5. Gateway ambiguity resolved: gateway never restarted; "temporary services"
   referred only to the stopped uvicorn test instances.

## Roster lane policy (profiles/noesis-roster.yaml apply_model)
- kimi lane: core, steward, forge, scribe (+wave2/3 substrate, grid)
- anthropic lane: orchestrator, cartographer, sentinel, signal (+tracer, ledger,
  quill, advocate, herald, architect, skeptic)
- coder/live are non-roster profiles: coder=anthropic (pool 4, credentialed),
  live=nous (pool 0, dead).

## Approval packages prepared in chat (2026-10-07): gateway restart w/ recovery
path; bounded inference plan (retry=per-test); lane decisions (5 profiles);
router plugin build+enable; launchd broker service (draft only). No inference,
restart, activation, install, commit, or push performed this pass.

---

# Corrections + Offline Verification — 2026-10-06 19:40 MDT (19:40 local / 2026-10-07 01:40 UTC)

The previous addendum's header "~01:20 MDT" was WRONG: 01:20 was UTC
(= 19:20 MDT Oct 6). Local time is MDT (UTC-6); the conversation date is
Oct 6 MDT. Original entry preserved above; this is the correction.

## Correction: repository state
After the addendum was written, HEAD = 291eb3e and the working tree holds
exactly ONE modified tracked file: this report
(` M docs/operations/deployment-verification-2026-10-06.md`, uncommitted).
The earlier "tree clean" statement described the state BEFORE the addendum.
Nothing else in the deployment repo changed. (agency/.scratch/ is gitignored
generated output; ~/tools/agency-agents gained the builder's default in-tree
integrations/ output — tooling checkout, not the deployment repo.)

## Correction: "router plugin absent from repo" — REVERSED
The plugin EXISTS at agency/integrations/hermes-plugin/ (vendored). My earlier
claim searched the wrong path. Verified offline 2026-10-07 01:2x-01:4x UTC:
- Build from pinned source (ad9264e3): OK (279 agents)
- convert.py --check: OK (pin parity, plugin byte-parity vs pinned-source
  build, scratch staleness, secret scan) — after materializing the on-demand
  .scratch tree per README operating rules
- check-hermes-plugin.py: PASSED; test-hermes-plugin.py: 6/6 OK
- validate-specs.py: OK (4 warnings: L2 needs .scratch — now built; L3 apply
  --check parity gate is unimplemented/planned)

## Finding: prefer-live overlay has no executable runtime consumer
agency/integrations/hermes-plugin/prefer-live.yaml (strategy
suppress_router_candidate_when_live_profile_matches) is generated and
statically validated, but no code applies it: not in the plugin __init__,
not in apply-agency-profiles.sh, not in generated specs. Offline 18-division
selection via the vendored plugin's real scoring: 2/18 agreement with
curation.yaml (raw token-overlap picks non-curated agents, e.g. engineering
query -> realtime-collaboration-engineer vs curated engineering-software-
architect). As designed today, ROUTING-SMOKE would measure raw routing, not
prefer-live routing. Enforcement mechanism = implementation gap (recorded;
does not block the default-route milestone). Wave-2 divisions (10/18) have
no live profiles (no agency-* profiles under ~/.hermes/profiles).

## Correction: loaded-route + restart-scope claims
- Start times are inference, not proof of loaded route. No supported
  non-inference loaded-route inspection exists (gateway status does not
  report the model; serve API /health = 404, undocumented endpoints not
  probed). Loaded-route verification remains PENDING.
- `hermes gateway restart` (no --all) restarts the default-profile gateway
  service = PID 2061. It does NOT target the desktop serve process PID 79891
  (lock: host-desktop-serve.lock — this session's runtime) nor coder PID
  49528. Earlier statement that restart would terminate this session was
  wrong; residual uncertainty: desktop features that call the gateway
  messaging bus would pause until it returns.

---

# Executed Approvals A + B — 2026-10-06 19:40–19:57 MDT

## Approval A — default gateway restart (PID 2061 scope): EXECUTED
- `hermes gateway restart` (foreground, 120s timeout): old gateway PID 2061
  STOPPED; replacement did not attach before the CLI was reclaimed — gateway
  left not-running (lock free). Recovery per plan: relaunched via
  `hermes gateway run` as tracked background process (session proc_5cd0eafe5941).
- Result: gateway RUNNING as PID 89337, started 2026-10-06 19:53:56 MDT —
  AFTER config mtime 18:14:17 MDT → process read on-disk config at boot
  (kimi route). Status: standalone serving default (multiplex deferred, as
  approved scope). Session serve PID 79891 UNINTERRUPTED; coder PID 49528
  UNTOUCHED. Loaded route = strong inference from boot-after-change; direct
  in-memory route inspection remains unavailable (documented).

## Approval B — one direct KIMI_CODE probe: EXECUTED
- Command: `hermes chat -q "Reply with exactly: noesis-probe-ok" -m kimi-k2.7-code`
- Session: 20261006_195434_bbb544; duration 21s; api_call_count: 1; exit 0.
- Recorded telemetry: model kimi-k2.7-code; billing_provider kimi-coding;
  assistant reply exactly "noesis-probe-ok"; input_tokens 21,781
  (full profile prompt context — the pre-declared ~500-token input estimate
  was wrong for `hermes chat`; corrected here); output_tokens 79;
  actual_cost_usd: None (no provider cost telemetry, as disclosed).
- Establishes: kimi-coding credential functions; approved lane executes
  end-to-end from on-disk config. Consumed subscription quota: 1 call,
  ~21.8K in / 79 out tokens.
- Export artifact (contained all sessions) deleted after evidence extraction;
  canonical record remains in Hermes session store.

---

# Full Profile Fleet Apply — 2026-10-06 ~20:05 MDT (2026-10-07 02:05 UTC)

Operator directive: "apply all noesis profiles and all other agent profiles
contained in the noesis-agent-stack" — explicit authorization for full-fleet
materialization. Model tuning left OFF (zero inference): the scripts verify
lanes with live smoke calls; anthropic-lane verification remains a separate
approval. New profiles inherit the host default route (kimi-k2.7-code@
kimi-coding); roster-mandated anthropic lanes are NOT written — recorded as
pending lane decision, per the standing fence.

## Actions
- Pre-apply backup: ~/.hermes/backups/pre-full-apply-20261006/profiles.tgz
  (full profiles dir; sockets skipped).
- scripts/apply-noesis-profiles.sh --all --no-model-tuning (bash 5.3 required;
  macOS /bin/bash 3.2 lacks mapfile): created=10 refreshed=7 skipped=0.
  New: noesis-substrate, tracer, ledger, grid, quill (wave 2);
  noesis-orchestrator, advocate, herald, architect, skeptic (wave 3).
  NOTE: script exits 1 on success (trailing `[[ ]] && exit 1` returns false
  when the failed array is empty) — cosmetic bug, results verified on disk.
- scripts/apply-agency-profiles.sh --all --yes --no-model-tuning:
  created=18 failed=0 (same cosmetic exit-1).
  All 18 Tier A agency profiles materialized from the validated .scratch tree.

## Verification (post-apply)
- ~/.hermes/profiles: 36 profile dirs (17 noesis-*, 18 agency-*, coder, live)
- validate-honcho-profiles.py: ok; check-agency-names.py: OK;
  validate_inference_routing.py: PASSED; verify_live_config.py: PASS
  (drift list unchanged: the 5 previously identified profiles only; new
  profiles inherit the approved default route).
- Gateway: observed PID changed 89337 -> 25816 without operator action
  (external supervisor restart, post-apply); loaded config post-dates all
  changes. Still STANDALONE serving default only; all 35 other profiles'
  bots unserved (multiplex + photon token strategy remain open decisions).

## Not done (unchanged fences)
- No inference beyond the previously approved single probe; no anthropic or
  openrouter execution; no roster-lane writes to the 12 roster-anthropic
  profiles; no multiplex/photon activation; no launchd install; no commits.
