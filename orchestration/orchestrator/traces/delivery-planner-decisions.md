# Delivery-Loop Planner — Example Source-Backed Decisions

NOESIS-LOOP-GRAPH-V1 · deliverable C.3. Provenance: **synthetic fixture**
(workspace/loop-graph-slice, regenerated 2026-10-11), not live records.
Regenerate: run the demo script in `workspace/loop-graph-slice/` (commands in
session log) or any `DeliveryPlanner.plan()` call against a DelegationStore
ledger.

The planner is a read-only projection consumer. Each decision below cites
the authoritative ledger artifact (`delegated-tasks.jsonl`) with the exact
record and task version the decision was derived from. The planner never
dispatches, never grants authorization, and never mutates the ledger
(regression-tested in `tests/test_delivery_planner.py`).

## Q1 — What evidence/review applies to this exact candidate?

Only records bound to the **current candidate revision** apply. Reviews on a
superseded candidate are cited as `scope_mismatch` and never inherited (test:
`test_candidate_change_does_not_inherit_review`). Contradicting verdicts on
the same stage+candidate fail closed (`test_contradicting_reviews_fail_closed`).

## Q2 — What action may be proposed, and what remains held?

Demonstration on three synthetic tasks (assignee `noesis-forge` throughout):

### t1 — attempt in flight

```
disposition : wait_for_existing_attempt
proposed    : (none — dispatching again would duplicate execution)
reason      : active attempt owns the task (state=running)
refs        : state(running, task_version=2)
              lease(holder=noesis-forge, epoch=1)
held        : everything; a live attempt owns the task
```

### t2 — candidate submitted, no reviews yet

```
disposition : next_action_proposal
proposed    : dispatch_spec_review
reason      : candidate submitted; no current spec verdict
refs        : state(verifying, task_version=3)
held        : execution itself; the proposal is advice — the execution
              authority revalidates before any dispatch
```

### t3 — full review pass, authorization required and NOT granted

```
disposition : blocked_on_authorization
proposed    : (none)
reason      : approval is required by contract and not granted; recorded
              authorization must be revalidated by the execution authority
refs        : state(verifying, task_version=5)
              review:spec:PASS (noesis-sentinel, task_version=5)
              review:quality:APPROVED (noesis-skeptic, task_version=5)
              approval:required (malformed=false)
              approval:granted (value=false, malformed=false)
held        : orchestrator_accept — even with both reviews green, the
              planner cannot cross the authorization gate
```

## Fail-closed vocabulary

Fixed dispositions: `next_action_proposal`, `wait_for_existing_attempt`,
`blocked_on_evidence`, `blocked_on_review`, `blocked_on_authorization`,
`completed_within_scope`. Unknown task, missing candidate, malformed
authority data, expired lease, contradicting reviews — all resolve to a
blocked disposition with citations, never to a dispatch proposal.
