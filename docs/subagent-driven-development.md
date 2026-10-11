# Subagent-driven development workflow

## Purpose

Execute an approved implementation plan through fresh coding contexts, ordered
spec/quality reviews, bounded remediation, and final integration review.

## Entrypoint

Controller: `orchestration/orchestrator/app/subagent_development.py` (`SDDWorkflow`).
Skill: `skills/subagent-driven-development/SKILL.md`.
Schemas:

- `contracts/orchestration/subagent-development-plan.schema.json`
- `contracts/orchestration/subagent-development-review.schema.json`

## Actors

| Role | Profile |
|---|---|
| Supervisor/controller | `noesis-orchestrator` |
| Implementation owner | `noesis-forge` |
| Spec reviewer | `noesis-sentinel` |
| Quality reviewer | `noesis-sentinel` in a fresh review session |
| Integration reviewer | `noesis-skeptic` |

## States

```text
pending -> ready -> implementing -> spec_review -> quality_review -> completed
                     ^                 |              |
                     |                 v              v
                  remediation <- REQUEST_CHANGES <- REQUEST_CHANGES
```

Exception states: `awaiting_clarification`, `awaiting_approval`, `blocked`,
`failed`, `cancelling`, `cancelled`.

## Gates

- Pre-flight: approved plan, baseline revision, dependency readiness, backend
  readiness, workspace conflict check, and policy/approval compatibility.
- Revision: spec review must PASS on the current candidate revision before
  quality review starts; quality must APPROVE the same revision before the task
  completes.
- Escalation: questions, missing authority, repeated failure, exhausted retry
  budget, or disputed requirements pause the task.
- Abort: policy violations, cancellation, or emergency stop terminate managed
  work and preserve redacted evidence.

Any candidate revision change invalidates prior approvals for the old revision.
Critical or important findings block completion until remediated or explicitly
resolved by policy.

## External coding sessions

Claude Code/Codex execution remains behind a supervised session adapter. The
coding specialist (`noesis-forge`) owns scope, isolation, backend readiness,
contract assembly, artifact collection, and independent verification. Backend
fallback is explicit only; no missing backend silently becomes a model API call
or another profile.

## Delivery semantics

Report these states separately: implemented, task-reviewed,
integration-reviewed, verified, committed, merged, deployed. This workflow may
produce verified local artifacts; it does not authorize commit, push, merge,
publish, or deploy.
