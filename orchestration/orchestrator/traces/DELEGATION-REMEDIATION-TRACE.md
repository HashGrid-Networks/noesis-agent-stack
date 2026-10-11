# Delegation remediation trace — provenance

`delegation-remediation-trace.jsonl` (12 events) is **synthetic sandbox output
produced by executing the real `app/delegation.py` implementation** — not a
live run, and not hand-written. It satisfies the Subgoal 4 §12.8 deliverable
("a representative end-to-end event trace, including a retry or remediation
and revision-specific review acceptance") while live logs remain
authorization-gated.

## Regeneration

```bash
cd orchestration/orchestrator
# the generator script is the heredoc in commit history (see ef32a11^..);
# it runs the real DelegationStore against a tempfile.TemporaryDirectory()
# ledger with an HmacEnvelopeAuthenticator and prints one JSON object per line.
```

Properties visible in the trace:

1. **Monotonic fencing**: `version` increments 0→10 across every accepted
   transition; no transition is skipped or overwritten.
2. **Backend success ≠ acceptance**: after `RESULT rev-1` the task sits in
   `verifying` (`accepted: false`); completion occurs only at the explicit
   `ACCEPTED` event by `noesis-orchestrator`.
3. **Remediation is revision-specific**: spec `REQUEST_CHANGES` on rev-1
   returns the task to `running`; the rev-2 candidate invalidates the prior
   review row; both spec PASS and quality APPROVED bind to rev-2 only.
4. **Budget accounting survives the cycle**: `budget_reserved: 2.0` from the
   ASSIGN payload, `budget_consumed: 1.2` recorded by the assignee; both
   survive the ledger reload.
5. **Restart durability**: the `LEDGER RELOAD` event shows a fresh
   `DelegationStore` reconstructing `completed`, `accepted: true`, both
   reviews, and the budget figures from the append-only ledger — with
   `relaunch: false`.

## Label

SYNTHETIC (offline sandbox execution of production code). Live-path traces
(Subgoal 4 §4 canary, Subgoal 5) remain separate authorization-gated
milestones and must not be inferred from this artifact.
