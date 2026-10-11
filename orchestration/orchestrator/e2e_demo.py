"""Bounded E2E workflow demo — synthetic fixtures through real contracts.

Chain exercised (per EVALS.platform.yaml e2e_demo):
  Research (noesis-signal) -> evidence/handoff -> Main approval gate ->
  Coder (coder) with dependency edge -> QA separation proof (reviewer-only
  cannot take execution work) -> synthesis = operator brief.

Uses a throwaway ledger. No inference, no external calls, no live workers.
OpenClaw worker E2E is tracked separately (needs inference authorization).
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.control_plane import Orchestrator  # noqa: E402
from app.models import Verification  # noqa: E402

LEDGER = Path(tempfile.mkdtemp(prefix="noesis-e2e-")) / "tasks.jsonl"
orch = Orchestrator(str(LEDGER))
steps = []


def step(name, ok, detail):
    steps.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL':4s}  {name}: {detail}")


# HOP 1 — Research worker task (synthetic fixture input)
research = orch.propose(
    title="e2e: research cited brief",
    intent="synthetic research hop using fixtures only",
    assignee_profile="noesis-signal",
    required_capability="cross_validate",
    acceptance_criteria=["three cited sources", "uncertainty labels"],
    verification=Verification(method="reviewer_signoff", evidence_required=True),
    idempotency_key="e2e:research:1",
    risk_tier="r0",
    timeout_s=900,
)
orch.enqueue(research.task_id)
orch.claim(research.task_id, claimed_by="noesis-signal")
orch.start(research.task_id)
orch.submit_handoff(research.task_id, {
    "summary": "Synthetic brief: fixture findings only",
    "artifacts": [{"path": "workspace/research-vault/brief-e2e.md", "checksum": None, "kind": "brief"}],
    "verification_result": {"passed": True, "evidence": "3 fixture sources cited"},
    "unmet_criteria": [],
})
orch.succeed(research.task_id)
step("research_hop", orch.store.get(research.task_id).state == "succeeded",
     "handoff evidence + artifact recorded")

# HOP 2 — Main approval gate on dependent coder task (r2)
coder = orch.propose(
    title="e2e: coder durable patch",
    intent="implement approved bounded change",
    assignee_profile="coder",
    required_capability="artifact_publish_internal",
    acceptance_criteria=["tests green", "artifact published"],
    verification=Verification(method="ci_artifacts", evidence_required=True),
    idempotency_key="e2e:coder:1",
    correlation_id=research.correlation_id,
    depends_on=[research.task_id],
    risk_tier="r2",
    timeout_s=1800,
)
d_before = orch.dispatchable(coder.task_id)
orch.approve(coder.task_id, operator="elvis")
orch.enqueue(coder.task_id)
d_after = orch.dispatchable(coder.task_id)
step("main_approval_gate", (not d_before.allowed) and d_after.allowed,
     f"blocked before approval ({d_before.reason[:40]}) -> dispatchable after operator approval")

# HOP 3 — dependency edge respected
orch.claim(coder.task_id, claimed_by="coder")
orch.start(coder.task_id)
orch.submit_handoff(coder.task_id, {
    "summary": "Synthetic patch with tests",
    "artifacts": [{"path": "workspace/coder-jobs/patch-e2e.diff", "checksum": None, "kind": "patch"}],
    "verification_result": {"passed": True, "evidence": "ci artifacts"},
    "unmet_criteria": [],
})
orch.succeed(coder.task_id)
step("coder_hop_with_dependency", orch.store.get(coder.task_id).state == "succeeded",
     "dependency on research task satisfied")

# HOP 4 — QA separation: reviewer-only profile cannot take execution work
try:
    orch.propose(
        title="e2e: qa-as-executor (must refuse)",
        intent="execute verification work",
        assignee_profile="noesis-skeptic",
        required_capability="cross_validate",
        acceptance_criteria=["x"],
        verification=Verification(method="reviewer_signoff"),
        idempotency_key="e2e:qa-exec:1",
        risk_tier="r0",
        timeout_s=60,
    )
    step("qa_separation_negative", False, "reviewer-only profile accepted execution work — BROKEN")
except Exception as e:  # noqa: BLE001
    step("qa_separation_negative", True, f"refused: {str(e)[:80]}")

# HOP 5 — synthesis = operator brief
brief = orch.synthesize(research.correlation_id)
ok = (brief["complete"] and brief["task_count"] == 2 and len(brief["evidence"]) == 2
      and not brief["blocked"] and not brief["failed"] and not brief["awaiting_approval"])
step("synthesis_operator_brief", ok,
     f"complete={brief['complete']} tasks={brief['task_count']} evidence={len(brief['evidence'])}")

print(f"\nE2E DEMO: {sum(1 for s in steps if s[1])}/{len(steps)} hops passed; ledger={LEDGER}")
sys.exit(0 if all(s[1] for s in steps) else 1)
