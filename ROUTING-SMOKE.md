# ROUTING-SMOKE.md — Tier A routing smoke test record

> **Purpose**: before expanding Tier A past 1 profile per division, run one
> representative query per division through the lazy router and record which
> profile won. This file is the committed evidence (C3 / Operating rule 1).
>
> **How to run**: with the Tier A fleet live (at minimum wave 1 applied),
> issue each query in a fresh `hermes` session against the router-enabled
> profile and record the winning profile. All 18 rows must have a winner
> before any expansion past 1/division.
>
> **Pass criteria**: (a) every row has a winner; (b) winners are Tier A
> profiles for ≥16/18 divisions; (c) validator description-uniqueness cosine
> < 0.85 at run time; (d) no live profile outside the declared rosters wins.

## Fleet scope

This smoke test validates routing against the **full declared roster**: both
profile families are routing candidates and must not lose to an undeclared
`default`/`coder`/`noesis-orchestrator` fallback unless generic fallback was
explicitly enabled.

- **In-house Noesis fleet (16):** `profiles/noesis-roster.yaml`, applied under
  `~/.hermes/profiles/noesis-*`. See `agents/noesis-*` for `SOUL.md`+`agent.yaml`.
- **Third-party "agency" fleet (18, advisory-only):** generated from
  `agency/curation.yaml` via `agency/convert.py` (not committed; materialized by
  `scripts/apply-agency-profiles.sh` / the Ansible `hermes_profiles` role).
  See `agency/README.md`.
- **Reviewer-only profiles** (`noesis-sentinel`, `noesis-skeptic`) carry no
  execution authority and are excluded from winning an implementation row.

The orchestration engine (`orchestration/orchestrator/`) and
`WORKFLOWS.md` §5 define how these candidates are discovered, policy-checked,
launched, identity-verified, and validated; this file records the smoke
evidence for that routing path.

Date: ____-____-____   Operator: ____________   Fleet: wave __ + noesis
Router plugin: integrations/hermes-plugin (sha256 recorded in apply audit)

| # | Division | Representative query | Winner profile | Notes |
|---|----------|----------------------|----------------|-------|
| 1 | engineering | "Design the module boundaries for a sync engine with offline conflict resolution" | | |
| 2 | research | "Synthesize the last 3 years of CRDT vs OT literature into a decision matrix" | | |
| 3 | security | "Threat-model a public upload endpoint that serves files back to other users" | | |
| 4 | product | "Prioritize the Q3 roadmap for a 2-person team with 400 active users" | | |
| 5 | finance | "Build a rolling 13-week cash forecast from our Stripe + payroll exports" | | |
| 6 | project-management | "Plan the migration of 12 cron jobs into the orchestrator without missing a beat" | | |
| 7 | sales | "Draft the outreach sequence for infra teams at Series A devtool startups" | | |
| 8 | marketing | "Audit our blog for the queries an AI search engine would cite us for" | | |
| 9 | academic | "Check this ANOVA setup for the reviewer-facing methods section" | | |
| 10 | design | "Final-pass review of this settings screen before it ships" | | |
| 11 | game-development | "Balance the economy loop for a 20-minute roguelite run" | | |
| 12 | gis | "Pick a tiling scheme for serving cadastral parcels at city scale" | | |
| 13 | healthcare | "Grade the evidence quality behind this supplement-dosing claim" | | |
| 14 | paid-media | "Audit this ad account for wasted spend and creative fatigue" | | |
| 15 | spatial-computing | "Design the interaction model for a wrist-mounted AR settings panel" | | |
| 16 | specialized | "Map this legacy repo's undocumented module graph before we touch it" | | |
| 17 | support | "Write the runbook for the on-call rotation covering these three alerts" | | |
| 18 | testing | "List the assumptions in this test plan that reality will violate first" | | |

## Outcome

- Rows recorded: ____ / 18
- Tier A winners: ____ / 18
- Cosine guard: ______ (< 0.85 required)
- Verdict: GO / NO-GO   Signature: ____________

**NO-GO →** do not expand past 1/division; file an issue with the losing
rows and revisit after router/plugin adjustments (re-run
`convert.py --full-tree` + `validate-specs.py` before retrying).

---

---

## Plan-first delegation (noesis-orchestrator)

### Purpose
Decompose every nontrivial agent request into bounded, capability-mapped tasks
before dispatch and validate a planning gate (eligible owner per task, no
overlapping mutation targets) before any implementation starts. Planning does
not launch work; it only produces a validated `PlanRequest` that downstream
workflows (`subagent-driven-development` for coding tasks, direct specialist
routing otherwise) consume.

Implementation: `orchestration/orchestrator/app/planning.py`
Wired entrypoint: `Orchestrator.plan_request(request, idempotency_key=..., risk_tier=...)`
Durable ledger: `<ledger-dir>/plans.jsonl` (append-only, one `PlanRequest` per row)

### Gate rules
- **Owner gate**: every decomposed fragment must select an eligible specialist;
  a fragment with no eligible owner raises `PlanningBlocked("no_eligible_owner", ...)`
  with per-profile rejection reasons (never silent `default`/`coder` fallback).
- **Workspace gate**: two tasks claiming the same expected mutation target raise
  `PlanningBlocked("workspace_conflict", ...)`; this prevents simultaneous writers
  on a shared file.
- **Atomic fast path**: a single-fragment request records
  `fast_path_reason = "atomic_low_risk_single_specialist"` and skips decomposition —
  the reason is recorded, nothing is launched during planning.
- **r3 safety**: an r3 plan whose implementation task requires
  `subagent-driven-development` raises `PlanningBlocked("planning_gate_blocked", ...)`
  because implementation before human approval is refused.

### Roles
- Orchestrator: owns decomposition and the validated plan; never becomes the
  implementer.
- Existing planning/architect profile: may critique or propose the plan, but the
  orchestrator adopts it; no new permanently running planner is added.

---

---

## Delegated-task handoff (noesis-orchestrator)

### Purpose
Provide a durable, versioned orchestrator-to-specialist handoff so live
delegation is auditable and survives interruption: validated contract →
explicit specialist assignment → supervised execution → evidence-bearing
result → revision-bound reviews → orchestrator acceptance. Backend success
(running → verifying) is kept distinct from orchestrator acceptance
(verifying → completed).

Implementation: `orchestration/orchestrator/app/delegation.py`
Protocol: `noesis.delegated-task/v1` (HandoffEnvelope)
Schema: `contracts/orchestration/delegated-task.schema.json`
Ledger: append-only JSONL; latest version per task id wins on replay.

### Guarantees
- **Idempotent delivery**: duplicate ASSIGN (by idempotency key) and replays
  return the existing task unchanged — never duplicate work.
- **Fencing**: monotonic `assignment_epoch` + optimistic `expected_task_version`;
  a stale epoch or a mismatched expected version rejects the state change.
- **Authorization**: only `noesis-orchestrator` may ASSIGN/ready/accept; only the
  assigned specialist may start/verify; the implementer may never review its own work.
- **Lease**: bounded lease (holder/epoch/expiry); expiry marks execution suspect
  (`blocked`), never silent success.
- **Revision-bound reviews**: quality review requires spec PASS on the current
  candidate revision; a candidate change invalidates older review rows; critical
  /important findings block a passing verdict.
- **Distinct acceptance**: a task reaches `completed` only when the orchestrator
  explicitly accepts after spec PASS + quality APPROVED on the current revision.

### Lifecycle
```
assigned → ready → running → verifying → spec_review → quality_review
                                                        → verifying → completed
```
Supporting states: `blocked` (lease expiry, BLOCKED review), `cancelled`, `failed`.

---

# Noesis specialist dispatch smoke

> **Purpose**: verify that Noesis orchestrator dispatch uses the explicit
> specialist-routing entrypoint rather than Hermes parent-inheriting subagent
> launch. This smoke test proves requested-vs-loaded profile identity with
> launcher-side attestation; it does not run inference when `dry_run` is true.

## Sandbox command

Create an input JSON such as:

```json
{
  "title": "sandbox CLI architecture route",
  "intent": "Design an MCP/Hermes profile contract for a new memory agent",
  "acceptance_criteria": [
    "selected specialist is explicit",
    "requested and loaded profile match"
  ],
  "verification": {"method": "launcher_attestation"},
  "idempotency_key": "sandbox:cli-specialist-route-YYYY-MM-DD",
  "dry_run": true
}
```

Run from the repository root:

```sh
PYTHONPATH=orchestration/orchestrator \
uv run python -m app.specialist_dispatch_cli \
  --input workspace/orchestrator/specialist-cli-sandbox-input.json \
  --ledger workspace/orchestrator/specialist-cli-sandbox-ledger.jsonl \
  --events workspace/orchestrator/specialist-cli-sandbox-events.jsonl
```

Hermes plugin entrypoint for live routing:

```text
integrations/hermes-plugin/noesis-specialist-router
```

The plugin exposes `noesis_specialist_delegate`, which invokes the same
`Orchestrator.route_and_launch()` path and refuses to use parent-inheriting
`delegate_task` fallback. Canary installation helper:

```sh
bash scripts/install-noesis-specialist-router.sh \
  --home "$HOME/.hermes" \
  --profile noesis-orchestrator \
  --dry-run

# after reviewing dry-run output and obtaining operator approval:
bash scripts/install-noesis-specialist-router.sh \
  --home "$HOME/.hermes" \
  --profile noesis-orchestrator \
  --yes
```

This only copies the plugin into the target profile's plugin directory and
writes an audit row; it does not restart Hermes, launch non-dry-run work, alter
credentials, push, merge, publish, or deploy.

## Canary blocker — confirmed root cause (2026-10-08)

The fresh-session dry-run canary timed out twice with **zero** `noesis_specialist_delegate`
tool calls. Confirmed cause (not a hypothesis): the plugin registers its tool under
toolset `noesis_specialist_router` (`integrations/hermes-plugin/noesis-specialist-router/__init__.py:107`),
but the target profile's `platform_toolsets.cli` contains only `hermes-cli`
(`/Users/elvis/.hermes/profiles/noesis-orchestrator/config.yaml:206-208`). A tool whose
toolset is not in the profile's CLI toolset list is **not exposed to the model** in a
live CLI session, so the model never sees `noesis_specialist_delegate`. The plugin-host
test passes only because it invokes the handler directly, bypassing model toolset gating.

To expose the tool and unblock the canary, the toolset must be added to the profile's
CLI toolset list (a config change requiring operator authorization):

```sh
hermes -p noesis-orchestrator config set platform_toolsets.cli '[hermes-cli, noesis_specialist_router]'
hermes -p noesis-orchestrator config get platform_toolsets.cli   # verify
hermes -p noesis-orchestrator plugins enable noesis-specialist-router
# re-run the fresh-session dry-run canary, then restore:
hermes -p noesis-orchestrator plugins disable noesis-specialist-router
```

Record prior config + plugin state and back up the config before changing it; restore
both after the canary. This is a config change and is **not** authorized by the
installation helper's write set.

## Pass criteria

- `success` is `true`.
- `selected_agent` is a concrete specialist, not `noesis-orchestrator`,
  `default`, or `coder` unless generic fallback was explicitly enabled.
- `requested_profile_id` equals `loaded_profile_id`.
- `launch_command` contains `hermes -p <selected-profile> profile show
  <selected-profile>`.
- The event log contains `specialist_dispatch` with candidate profiles,
  rejection reasons, selected agent, launch target, model/provider references,
  fallback reason, and terminal status.

## Observed sandbox run

Date: 2026-10-08

```json
{
  "success": true,
  "task_class": "agent_architecture",
  "selected_agent": "noesis-architect",
  "requested_profile_id": "noesis-architect",
  "loaded_profile_id": "noesis-architect",
  "resolved_launch_target": "noesis-architect",
  "fallback_reason": null,
  "model_provider": "nous",
  "model_id": "deepseek/deepseek-v4-flash"
}
```

Attestation used `hermes -p noesis-architect profile show noesis-architect`
and reported profile path `/Users/elvis/.hermes/profiles/noesis-architect`.
