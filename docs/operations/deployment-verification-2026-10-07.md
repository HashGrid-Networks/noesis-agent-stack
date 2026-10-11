# Deployment Verification — 2026-10-07

Supervisor: Noesis Hermes-Core · Operator: Elvis Nuno
Repository: `github.com/enuno/noesis-agent-stack` @ `291eb3e17878179b084aa9e58caf7813b403a006` (main)
Host: macOS 27.2 arm64 · Python 3.11.13 · Node v24.10.0 · Hermes v0.21.5+8076.g85db7c3 · OpenClaw 2026.6.8 (844f405)

Pre-deploy backup: `~/.hermes/backups/pre-deploy-20261007-082639/`
Post-verify backup: `~/.hermes/backups/post-verify-20261007-090921/`
Restore test: isolated extract to `/tmp/noesis-restore-test-*` — 16,141 files, 60/60 configs parse, pre-change state intact. Live data untouched.

## Verdict

**Implemented control plane: deployed and verified.** Inference, QA lane,
Claude Code bridge, and OpenClaw-worker E2E remain unverified-by-design
(lack live credentials / bridge implementation / authorized inference budget).

## Component status matrix

| Component | Status | Evidence |
|---|---|---|
| Broker (FastAPI, :8787 loopback) | deployed + verified | 35/35 tests; 13/13 live battery (`workspace/ops/broker-battery-20261007.txt`); launchd `ai.noesis.broker` |
| Orchestrator control plane | deployed + verified | 104/104 tests (incl. 3 new supervisor tests); 9/9 control-plane battery; launchd `ai.noesis.orchestrator` |
| Supervisor sweep daemon | deployed + verified | `orchestration/orchestrator/supervisor.py`; JSONL sweeps clean every 60s; no execution authority |
| Hermes gateway (multiplex) | deployed | `ai.hermes.gateway`; PHOTON creds stripped from non-default profiles; explicit `platforms.photon.enabled: false` per profile |
| OpenClaw gateway (:18789 loopback) | repaired + verified | crash-loop root-caused (state DB schema 15 vs supported 1) → DB quarantined, mode=local; `ai.openclaw.gateway` healthy, restart-durable |
| Profiles (35 dirs) | applied; lane audit clean | 5 mismatches fixed (advocate, architect, herald, quill, skeptic); background apply job + audit re-run confirm **DRIFTED roster profiles: 0** |
| Inference routing | partially verified | `validate_inference_routing.py` green; default lane `kimi-coding/kimi-k2.7-code` approved+credentialed; anthropic lane **credentialed** (pool-backed, 4 entries in `~/.hermes/auth.json` credential_pool, presence-only confirmed 2026-10-07); live per-lane calls deferred (budget unspent) |
| ROUTING-SMOKE (18 rows) | blocked (narrowed) | credentials now confirmed for anthropic + kimi lanes; sole remaining blocker is router plugin installation (built + offline-validated, not installed) |
| Research / Subconscious workers | contract-only | broker schema + evals; no live worker loops in repo; E2E deferred with inference authorization |
| QA lane | negative-verified | reviewer-only profiles refused execution by policy (e2e hop 4) |
| Claude Code bridge | not implemented | no bridge code in repo at this commit |
| Treasury / Content / Ops workers | not implemented | Phase 3–7 roadmap; not required for control-plane deployment |

## Batteries (all against the live deployment)

- Broker live battery: **13/13** — health, workers registry, valid submit → 202,
  fetch, idempotency replay → 409, malformed → 400, unknown worker rejected,
  scope violation → 422, cancel → 200, event trail, unknown job → 404,
  malformed id → 422, artifacts shape.
- Orchestrator battery: **9/9** — full lifecycle, r3 approval-manifest gate,
  unknown assignee refused, supervisor-cannot-execute, undeclared capability
  refused, succeed-without-handoff refused, idempotent propose, emergency stop
  durable across process replay, synthesis with evidence.
- E2E demo: **5/5 hops** — research hop → Main approval gate (blocked before
  approval, dispatchable after) → coder hop with dependency edge → QA
  separation negative (refused) → synthesis operator brief.
  Synthetic fixtures, real contracts, throwaway ledger.

## Security posture

- Broker and OpenClaw bound to loopback only (`lsof` verified: 127.0.0.1:8787,
  127.0.0.1/[::1]:18789).
- PHOTON bot token confined to default profile scope; stripped from 19 profile
  caches; env-pass auto-enablement neutralized via explicit disable blocks.
- Secrets presence-only throughout; `auth.json` backup mode 600.
- No firewall/DNS/proxy changes. No financial actions. No public exposure.
- Emergency stop: `emergency_stop()` blocks dispatch, persists across restarts,
  `resume()` clears; proven in battery.

## Restart durability

`launchctl kickstart -k` exercised on all three launchd services:
- `ai.openclaw.gateway` — new PID, `/health` 200, logs clean.
- `ai.noesis.broker` — fresh uptime, 4/4 workers healthy.
- `ai.noesis.orchestrator` — resumes sweeping; ledger replay correct.
- Host reboot survival: **unverified** (not authorized).

## Known blockers / follow-ups

1. ROUTING-SMOKE — **sole blocker: router plugin install** (built + offline-validated).
   Anthropic + kimi lanes both credentialed (pool-backed, presence-only verified).
   Running the 18 rows consumes the approved ~30-call inference budget.
2. OpenClaw-worker E2E — needs authorized inference budget (0 of ~30 consumed).
3. ~~Anthropic-lane decision for 5 profiles~~ — **resolved 2026-10-07**: roster
   approves anthropic for those profiles, fleet aligned (0 drift), lane credentialed.
4. r3 approval manifest binding is string-presence only (not store-validated) — hardening candidate.
5. Quarantined OpenClaw DB (`~/.openclaw/state/openclaw.sqlite.quarantined-20261007`) — upgrade OpenClaw or discard.
