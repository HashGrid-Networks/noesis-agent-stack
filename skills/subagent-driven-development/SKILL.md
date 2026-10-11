---
name: subagent-driven-development
description: "Approved implementation plan to fresh implementer, ordered spec/quality reviews, final integration review, and verified delivery."
version: 0.1.0
metadata:
  noesis:
    workflow_entrypoint: orchestration/orchestrator/app/subagent_development.py
    owner_profile: noesis-orchestrator
    implementer_profile: noesis-forge
    spec_reviewer_profile: noesis-sentinel
    quality_reviewer_profile: noesis-sentinel
    integration_reviewer_profile: noesis-skeptic
---

# Subagent-Driven Development

## Purpose

Run an approved implementation plan through bounded, fresh implementation
contexts and independent review gates:

`approved plan -> fresh implementer -> spec review -> remediation if needed -> spec PASS -> quality review -> task completed -> final integration review -> verified delivery`.

This skill is executable through the orchestrator workflow controller at
`orchestration/orchestrator/app/subagent_development.py`. The skill is not a
standalone permission grant and does not authorize push, merge, publish,
deployment, production mutation, credential changes, or irreversible actions.

## Triggers

Use when an implementation plan has already been approved and must be executed
with durable task state, fresh coding contexts, ordered review gates, and final
integration verification.

## Prerequisites

- Approved plan text and approval reference.
- Baseline repository revision.
- Task acceptance criteria and expected changed paths.
- Verification commands or explicit non-code verification method.
- Backend readiness for an authorized coding session adapter (`codex` or
  `claude-code`) when the coding specialist elects external execution.

## Role mapping

| Responsibility | Existing profile | Boundary |
|---|---|---|
| Supervisor/controller | `noesis-orchestrator` | owns durable ledger, dependencies, gates, budgets, synthesis; does not implement |
| Coding specialist | `noesis-forge` | owns bounded implementation task and supervised implementation session |
| Spec reviewer | `noesis-sentinel` | checks original task requirements and scope; no mutation |
| Quality reviewer | `noesis-sentinel` in a fresh review session | checks correctness, tests, security, maintainability; no mutation |
| Integration reviewer | `noesis-skeptic` | checks assembled result and regressions after task gates pass |

A reviewer profile may serve multiple review roles only through separate fresh
sessions. The implementer and independent review must never share a session.

## State machine

Task states:

```text
pending -> ready -> implementing -> spec_review -> quality_review -> completed
                     ^                 |              |
                     |                 v              v
                  remediation <- REQUEST_CHANGES <- REQUEST_CHANGES
```

Supporting terminal/exception states: `blocked`, `failed`, `cancelling`,
`cancelled`, `awaiting_approval`, `awaiting_clarification`.

Transition rules enforced by the controller:

- `ready` requires satisfied dependencies.
- `implementing` requires backend readiness, workspace conflict check, and a
  fresh implementation context.
- implementation completion records artifacts and candidate revision.
- `quality_review` requires `spec` review `PASS` on the same candidate revision.
- `completed` requires quality `APPROVED` on the same candidate revision.
- Critical/important findings block approval and route to remediation.
- Any candidate revision change invalidates prior reviews for the old revision.
- Final integration review requires every task to be completed.

## Implementation contract

Every implementation session receives:

- Root plan ID and parent task ID.
- Owning coder profile (`noesis-forge`) and selected backend.
- Objective, non-goals, acceptance criteria, expected paths.
- Baseline revision and verification commands.
- Data classification and approval reference.
- Stop conditions for questions, missing approval, policy violation, or timeout.
- TDD instruction when behavior is testable.

Provide only task-relevant context. Do not include secrets, unrestricted
supervisor instructions, or authority to reinterpret the parent goal.

## Review templates

Spec review verdicts: `PASS | REQUEST_CHANGES | BLOCKED`.

Spec review checks:

- Every original requirement is represented in the candidate.
- Acceptance criteria have evidence.
- Expected interfaces and paths match the contract.
- No unauthorized scope expansion.

Quality review verdicts: `APPROVED | REQUEST_CHANGES | BLOCKED`.

Quality review checks:

- Correctness, edge cases, maintainability, project conventions.
- Security and approval boundaries.
- Test quality and regression risk.
- Dependency or operational implications.

Findings must include severity, location, rationale, and required remediation.

## Recovery and escalation

- Missing backend/auth/policy approval blocks before launch.
- Failed review sends the task to bounded remediation and requires re-review.
- Exhausted retry budget escalates to the supervisor/operator.
- Cancellation terminates managed execution, records cleanup status, and
  preserves redacted evidence.
- Restart replays the ledger and must not duplicate an active implementation.

## Related skills and references

Compatible local skills verified in this repository/runtime:

- `test-driven-development` for RED/GREEN behavior where applicable.
- `systematic-debugging` for root-cause remediation.
- `ulw-work` for disjoint lane planning and final integration evidence.

The requested reference files `references/context-budget-discipline.md` and
`references/gates-taxonomy.md` were not present in this checkout during
integration. Their contents are therefore not incorporated or claimed here.

## Runtime evidence

Preferred invocation is through `SDDWorkflow` in
`orchestration/orchestrator/app/subagent_development.py`. Tests exercise this
entrypoint with mocked session adapters and durable ledger replay.
