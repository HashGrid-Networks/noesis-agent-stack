<p align="center">
  <img src="assets/logo/NoesisAgentStack.png" alt="Noesis Agent Stack Logo" width="420"/>
</p>

<p align="center">
  <a href="#"><img src="https://img.shields.io/badge/status-active-brightgreen?style=flat-square" alt="Status: Active"/></a>
  <a href="#"><img src="https://img.shields.io/badge/agents-Hermes%20profiles-3b82f6?style=flat-square" alt="Agents: Hermes profiles"/></a>
  <a href="#"><img src="https://img.shields.io/badge/architecture-supervisor--worker-f97316?style=flat-square" alt="Architecture: Supervisor-Worker"/></a>
  <a href="#"><img src="https://img.shields.io/badge/license-proprietary-lightgrey?style=flat-square" alt="License: Proprietary"/></a>
</p>

---

# Noesis Agent Stack

A production-oriented multi-agent orchestration system built on **Hermes Agent**
profiles. It pairs a durable **task-contract control plane** (the
orchestration engine) with two families of specialized Hermes agent profiles:
the **in-house Noesis fleet** (16 `noesis-*` specialists) and the **third-party
"agency" fleet** (18 advisory Tier A profiles generated from the Agency Agent
Catalog). All specialized work is planned, dispatched, and supervised by the
orchestrator under explicit approval gates; the stack keeps role separation,
durable ledgers, and bounded autonomy at its core.

The stack is deployed and operated through its companion IaC and prompt-library
repositories (see [Companion repositories](#companion-repositories)).

## What this repository is

This repository holds the definitions, contracts, policies, schemas, and the
orchestration engine that run the Noesis multi-agent system:

- The **orchestration engine** (`orchestration/`) — the control planes that plan,
  dispatch, supervise, and synthesize cross-profile work (see
  [Orchestration engine](#orchestration-engine)).
- The **in-house Noesis agent profiles** (`agents/noesis-*/`, `profiles/`) — the
  native specialist fleet (see [In-house Noesis fleet](#in-house-noesis-fleet)).
- The **third-party "agency" profiles** (`agency/`, materialized under
  `~/.hermes/profiles/agency-*`) — the advisory Tier A catalog fleet (see
  [Third-party "agency" fleet](#third-party-agency-fleet)).
- Shared policies, schemas, handoff contracts, routing state, and the register of
  deployable profiles.
- The OpenClaw worker definitions and Claude Code bridge retained from the
  original design for the specialized deep-work lanes they still serve.

The core pattern:

- **Noesis Orchestrator** is the long-lived supervisor and routing authority. It
  intakes goals, classifies domain and risk tier, assigns the narrowest-mandate
  specialist, enforces approval gates, and synthesizes results. It holds no
  execution terminal and never performs production, financial, credential, or
  destructive operations itself.
- **Specialist profiles** (both families) consume approved handoffs rather than
  raw external noise, each with an explicit mandate, toolset, and review chain.
- **Reviewers** (Noesis Sentinel, Noesis Skeptic, and reviewer-class agency
  profiles) audit work independently; a finding is not a claim, and a claim is
  not verified knowledge.

## Design principles

- **Supervisor-first control:** Noesis Orchestrator remains the only long-lived
  supervisory and approval authority in the system.
- **Separation of duties:** Orchestrator plans, specialists execute, reviewers
  audit, Scribe archives. No single profile is both implementer and reviewer.
- **Artifacts over chat:** Agents exchange durable files, ledgers, briefs, and
  handoff objects instead of relying on transient chat history as system memory.
- **Least-privilege control plane:** the Orchestrator holds no execution
  authority; irreversible operations are classified `r3`, held at a human
  approval gate, and routed only to a narrowly privileged executor profile.
  See `orchestration/orchestrator/` and `WORKFLOWS.md` §5.
- **Promotion gates:** a finding is not a claim, a claim is not verified
  knowledge, and a signal is not an approved task.
- **Explicit uncertainty:** freshness, contradictions, blocked items, and
  unresolved regressions remain visible rather than being flattened into
  confident summaries.
- **Bounded autonomy:** specialists are advisory or narrowly-privileged; no
  profile makes irreversible external decisions without human approval.

## Orchestration engine

Two control planes live under `orchestration/`:

### noesis-orchestrator — durable task-contract control plane

`orchestration/orchestrator/` is the **planner, dispatcher, supervisor, and
synthesizer** for cross-profile work. It holds **no** `terminal` and **no**
`code_execution` toolset, by roster construction, and never performs production,
financial, wallet, credential, or destructive operations — those are classified
`r3`, held at a human approval gate, and assigned to a narrowly privileged
executor profile.

Key components (see `orchestration/orchestrator/README.md`):

| Path | Purpose |
|---|---|
| `app/models.py` | `TaskContract` and the state machine (`ALLOWED_TRANSITIONS`) |
| `app/registry.py` | Roster-backed profile registry (`profiles/noesis-roster.yaml` + `agents/*/agent.yaml`) |
| `app/policy.py` | Deterministic refusals: assignment, risk, approval, dependency, evidence, staleness |
| `app/store.py` | Append-only JSONL ledger, replayed on start |
| `app/control_plane.py` | `Orchestrator` and `CircuitBreaker` — the only component that mutates contract state |
| `app/delegation.py` | Delegated-task contracts: typed payloads, permitted senders, validation, approval bindings |
| `app/lease_lifecycle.py` | Persisted-fold `TaskStore`: flock-guarded JSONL transaction log, immutable revision copies, compare-and-swap fencing against stale workers |
| `app/delivery_planner.py` | Non-executing delivery-loop planner (authority-scoped approval objects) |
| `app/launcher.py` | Explicit runtime launch with recorded attestation (profile, backend, revision) |
| `app/specialist_dispatch_cli.py` | Normal request entrypoint used for orchestrated dispatch |
| `app/specialist_routing.py` | Classification and eligible-specialist discovery |
| `app/planning.py`, `app/graph.py`, `app/graph_query.py` | Plan/dependency graphs and querying |
| `app/inference_routing.py` | Model/provider lane resolution |
| `app/subagent_development.py` | Reviewer-gated build-development contracts |
| `supervisor.py` | Sweep daemon: replays the ledger, calls `sweep_timeouts()` each interval (default 60 s), fail-closed on faults; runs under `ai.noesis.orchestrator` (launchd) |
| `tests/` | Policy, lifecycle, lease, replay, schema-conformance, and worked-example tests — **407 tests** |

Contracts: schema `contracts/orchestration/task-contract.schema.json`; ledger
`workspace/orchestrator/tasks.jsonl` (append-only, one
`{event, recorded_at, task}` record per line, replayed on start); risk tiers
`platform/risk-tiers.yaml`; R3 approval binding
`platform/approval-manifest.schema.json`.

Task lifecycle:

```
proposed -> approved -> queued -> claimed -> running
         -> awaiting_review | blocked | failed | succeeded | cancelled
```

Full transition table, approval gates, operator procedures, recovery, and
emergency stop are in `WORKFLOWS.md` §5. Run the tests with:

```bash
cd orchestration/orchestrator
~/projects/noesis-agent-stack/.venv/bin/python -m pytest -q   # 407 passed
```

No network, no credentials, and no repository writes: every test uses a
`tmp_path` ledger.

**Timeout semantics (explicit):** wall-clock/claim expiry is not an immediate
submit fence — denial relies on supervisor timeout processing
(`sweep_timeouts`, default 60 s interval). Expire/revoke/cancel fence
immediately; a post-expiry submit can succeed only until the next sweep
observes the expiry, then fails closed. Workers cannot hold TaskStore
authority; production entry is the Orchestrator, so worker-side expiry races
are structurally excluded.

### Broker — OpenClaw worker control plane

`orchestration/broker/` is the sole control plane for managing the OpenClaw
workers (Research, Subconscious) retained for persistent deep-work loops. Hermes
submits typed jobs through this API; the broker validates schemas, enforces
write scopes, tracks state, and emits structured events.

## Agent profiles

The stack routes work across **two families** of Hermes profiles. Both are
registered with the orchestrator and carry explicit roles, toolsets, and review
chains. Reviewer-only profiles (Sentinel, Skeptic) receive no execution
authority.

### In-house Noesis fleet

16 native specialists defined in `profiles/noesis-roster.yaml` (machine-readable
source of truth) and `agents/noesis-*/` (`SOUL.md` + `agent.yaml`), mapped to
Hermes profiles under `~/.hermes/profiles/<name>/`.

| Profile | Role | Tier | Notes |
|---|---|---|---|
| `noesis-orchestrator` | supervisor / router | supervisor | The long-lived control plane; maps to the `default` profile |
| `noesis-steward` | chief-of-staff | primary-operator | Daily triage, prioritization, status |
| `noesis-cartographer` | project-decomposer | primary-operator | Task graphs, dependency mapping, risk, ADRs |
| `noesis-forge` | software-engineer | primary-operator | Code, IaC, CI/CD; reviewed by Noesis Sentinel |
| `noesis-sentinel` | code-reviewer | reviewer-only | Independent code/security review; no execution |
| `noesis-scribe` | knowledge-archivist | primary-operator | KB structure, changelogs, docs |
| `noesis-signal` | research-specialist | specialist | Deep research and multi-source synthesis |
| `noesis-substrate` | infra-engineer | primary-operator | Infra/DevOps/cloud; reviewed by Noesis Sentinel |
| `noesis-tracer` | osint-specialist | specialist | Evidence timelines, labeled fact/allegation/inference |
| `noesis-ledger` | crypto-analyst | specialist | Crypto/mining/DePIN research; never financial advice |
| `noesis-grid` | data-analyst | specialist | Data analysis, spreadsheets, reports |
| `noesis-quill` | technical-writer | specialist | Writing, editing, technical docs |
| `noesis-advocate` | legal-advocacy-support | specialist | Research + drafting support; never legal advice |
| `noesis-herald` | comms-drafter | specialist | Strategic comms drafting; never sends autonomously |
| `noesis-architect` | agent-designer | specialist | Agent/Hermes/MCP/prompt design |
| `noesis-skeptic` | adversarial-reviewer | reviewer-only | Red-team / adversarial QC; no execution |

`agents/` additionally retains the supporting lanes `noesis-clawdev/`,
`noesis-praxis/` (council/aeon orchestration skill + runbook),
`claude-code-worker/`, `main-hermes/`, `research-openclaw/`,
`subconscious-openclaw/`, and `job-application-pipeline/`.

### Third-party "agency" fleet

18 **advisory-only** Tier A profiles generated from the
[Agency Agent Catalog](https://github.com/msitarzewski/agency-agents) (MIT,
pinned in `agency/SOURCE`), curated one per division. See `agency/README.md`.
All carry the shared advisory posture: `reports_to: noesis-orchestrator`, no
privileged tools, no secret access, no autonomous execution; production /
credential / financial / external-communication actions require human approval.

Wave 1 (8): `agency-engineering-software-architect`, `agency-research-synthesist`,
`agency-security-architect`, `agency-product-manager`, `agency-finance-financial-analyst`,
`agency-project-manager-senior`, `agency-sales-deal-strategist`,
`agency-marketing-seo-specialist`.

Wave 2 (10): `agency-academic-statistician`, `agency-design-ui-finish-gate-reviewer`,
`agency-game-designer`, `agency-gis-technical-consultant`,
`agency-healthcare-clinical-evidence-agent`, `agency-paid-media-auditor`,
`agency-xr-interface-architect`, `agency-specialized-codebase-archaeologist`,
`agency-support-infrastructure-maintainer`, `agency-testing-reality-checker`.

Lineage: `agency/curation.yaml` is the sole roster source; `agency/convert.py`
generates specs; `agency/validate-specs.py` is a three-layer reviewer guard;
`scripts/apply-agency-profiles.sh` materializes them; the `hermes_profiles` role
in the companion Ansible repo drives both fleets from one var. Tier A is capped
at 18 — expanding requires a recorded routing smoke test (`ROUTING-SMOKE.md`).

## Routing and delegation

The specialized-routing path is: **task classification → eligible specialist
discovery → profile selection → policy checks → explicit runtime launch →
identity verification → result validation → supervisor synthesis.** Workflows for
cross-profile execution, plan-first delegation, delegated-task handoff, and
reviewer-gated build development are defined in `WORKFLOWS.md`. Routing smoke
evidence and canary status are recorded in `ROUTING-SMOKE.md`.

## Repository layout

```text
.
├── README.md
├── SPEC.md, WORKFLOWS.md, ROUTING-SMOKE.md, HEARTBEAT.platform.md
├── DEPLOYMENT-PROMPT.md, DEPLOYMENT-RUNBOOK.md   # repository-verified operator runbook
├── DEVELOPMENT_PLAN.md, IMPLEMENTATION_PLAN.md, TODO.md
├── profiles/
│   ├── noesis-roster.yaml          # in-house fleet (16), machine-readable
│   ├── model-profiles.yaml
│   └── live/                       # sanitized live profile snapshot (config + SOUL)
├── agents/
│   ├── noesis-*/                   # in-house fleet (SOUL.md + agent.yaml)
│   ├── main-hermes/                # original supervisor design
│   ├── research-openclaw/, subconscious-openclaw/
│   ├── noesis-clawdev/, claude-code-worker/, job-application-pipeline/
│   └── noesis-praxis/              # council/aeon orchestration skill + runbook
│       # agency-*/ profiles are GENERATED (agency/convert.py), not committed:
│       # see agency/README.md — materialized at apply time by
│       # scripts/apply-agency-profiles.sh / the Ansible hermes_profiles role.
├── agency/                         # Agency catalog integration (curation, convert, validate)
├── orchestration/
│   ├── orchestrator/               # durable task-contract control plane (+ supervisor.py sweep daemon)
│   └── broker/                     # OpenClaw worker control plane
├── contracts/                      # schemas, handoffs, task-contract, bridge
├── shared/                         # policies, schemas, models, tools
├── platform/                       # routing, risk tiers, approval manifests
├── workspace/                      # orchestrator ledger, per-profile worktrees
├── apps/                           # careerops-dashboard, noesis-ops-dashboard
├── assets/                         # logos and brand assets
├── catalog/                        # companion catalog notes
├── config/                         # stack.yaml, runtime-inference-routing example env
├── docs/                           # operations evidence, decisions, architecture notes
├── evals/                          # platform evaluations
├── honcho/                         # Honcho memory profile definitions
├── integrations/                   # hermes-plugin integration
├── scripts/                        # apply-agency-profiles, verify-live-config, hooks
├── skills/                         # fleet skill definitions
├── specs/                          # dream-scheduler-mcp, openclaw-dryrun-contract
└── templates/                      # agent-install prompt, hermes-openclaw config template
```

The layout evolves, but the repo preserves strong isolation between the durable
orchestrator ledger, per-profile worktrees, and shared handoff surfaces.

## Companion repositories

The Noesis Agent Stack is deployed and operated alongside three companion
repositories:

- **[NoesisPraxis Ansible Stack](https://github.com/enuno/noesis-ansible)**
  — infrastructure-as-code for deploying, maintaining, and operating the fleet.
  The `hermes_profiles` role drives **both** profile families from a single var
  (`hermes_profiles_fleets`); it also manages the ACP/A2A/MCP registries, agent
  runtimes, observability, and the human-in-the-loop Telegram channel. Agency
  profiles are default-OFF and require explicit enablement
  (`noesispraxis_enable_agency_profiles: true`). It additionally carries the
  reviewed fail-closed `macos_prodqual_loopback` package for the production
  target `noesis-prodqual-macos-arm64-loopback-control-plane/v1`. Local
  reference: `~/projects/noesis-ansible`.

- **[Noesis Prompt Library](https://github.com/HashGrid-Networks/noesis-prompt-library)**
  — the shared prompt library and reference prompt set used by the fleet for
  complex agent goals and deployment prompts (ACP/A2A/MCP registries, Telegram,
  OpenClaw, ClawDev/HermesDev). Renamed from the former `noesis-prompt-cache`,
  which is superseded.

- **[enuno/hermes-agent](https://github.com/enuno/hermes-agent)** — the
  operator-owned Hermes Agent fork consumed by the qualified release: the
  runtime-enforcement HARDENING line and the qualified prodqual adaptation
  branch are published here, and the production target installs the runtime
  payload from an exact pinned commit (never a moving branch).

## Safety model

This system is designed around constrained autonomy.

- The Orchestrator plans, routes, and supervises but holds no execution
  terminal; it can never be the privileged executor for `r3` operations.
- Specialists consume approved artifacts, not raw external inputs or weak
  signals, and each carries an explicit mandate and toolset.
- Reviewer-only profiles (Noesis Sentinel, Noesis Skeptic, and reviewer-class
  agency profiles) audit but never execute; review is independent of
  implementation.
- Agency Tier A profiles are advisory-only and require human approval for
  production / credential / financial / external-communication actions.
- Validation, freshness checks, contradiction tracking, release gates, and
  durable-ledger admission block unsupported promotion.
- The persisted-fold TaskStore fails closed: stale-worker submissions are
  rejected by epoch fencing and revision comparison, and a torn or replayed
  ledger line never grants authority.

## Status

**Implemented and verified (2026-10-11):** the orchestrator control plane
(`orchestration/orchestrator/`) runs **407 passing tests**, including the
persisted-fold TaskStore with orchestrator-granted epochs and lease fencing,
the delivery-loop planner, launcher attestation, correlated lifecycle event
streams, and durable emergency stop. The broker control plane
(`orchestration/broker/`) suite passes. Validators (inference-routing policy,
Honcho profile definitions, agency catalog names against the pinned upstream
checkout, and live-config compliance via `scripts/verify_live_config.py`)
pass. Evidence: `docs/operations/deployment-verification-2026-10-06.md` and
`docs/operations/deployment-verification-2026-10-07.md`.

**Release qualification (2026-10-11):** the production qualification line is
independently reviewed component-by-component (Noesis Sentinel spec review,
then Noesis Skeptic adversarial review, with findings resolved or explicitly
carried): the orchestrator combined tree (planner + persisted-fold lease
lifecycle), the Hermes runtime-enforcement HARDENING line, the qualified
adaptation successor (recipe `hardening-1-trusted-manifest-runtime/v2`), and
the Ansible `macos_prodqual_loopback` package (three-state: offline package
approved-with-comments / automation-as-source pass / deployed not claimed).
Review receipts and digests are recorded in `docs/operations/`. Qualified
revisions are merged or published on the operator-owned repositories; the
production target `noesis-prodqual-macos-arm64-loopback-control-plane/v1` is
adopted and pinned by exact immutable SHA.

**Deployed:** the in-house profile fleet is applied to `~/.hermes`; the agency
Tier A fleet is materialized via the Ansible role (default-OFF). Production
deployment of the qualified release proceeds only through the reviewed
Ansible package, phase-gated (OS identities → authorized apply → offline
validation → separately approved activation and bounded canary), with
supervisor/operator procedures in `DEPLOYMENT-RUNBOOK.md`.

**Governance:** every component carries independent review dispositions;
no receipt approves itself, and no verdict constitutes installation,
activation, canary, merge, or deployment approval. Those gates are operator
decisions, one at a time.

**Verified live (inference):** routing-smoke rows in `ROUTING-SMOKE.md`
remain partially unrecorded pending a bounded live canary; the canary
envelope (profiles, task, provider route, request/turn/duration limits, and
cost ceiling) requires explicit operator approval before any live inference
lane is exercised.

## Roadmap

1. ~~Implement and accept the durable TaskStore admission authority~~ —
   **done (2026-10-11):** persisted-fold `TaskStore` with epoch fencing,
   qualified by independent Sentinel + Skeptic review.
2. Complete the phase-gated production deployment of the qualified release
   per `DEPLOYMENT-RUNBOOK.md` (apply → offline validation → activation).
3. Bounded live canary under an approved envelope (provider route, request /
   turn / duration limits, cost ceiling), then record the live routing-smoke
   rows in `ROUTING-SMOKE.md`.
4. Restart/concurrency acceptance for governed runtime enforcement beyond the
   single-host loopback target.
5. Wider rollout decision — explicitly out of scope until canary success;
   no new hosts, autoscaling, broader credentials, or increased spending is
   implied by any passing phase.
