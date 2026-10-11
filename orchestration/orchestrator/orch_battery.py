"""Orchestrator control-plane battery: lifecycle, gates, negatives, replay.

Runs against a THROWAWAY tmp ledger (never the production ledger). Exits 0
when every check passes. Stdout: results table.
"""
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.control_plane import Orchestrator  # noqa: E402
from app.models import Verification  # noqa: E402

results = []


def check(name, fn):
    try:
        detail = fn()
        results.append((name, "PASS", str(detail)))
    except Exception as e:  # noqa: BLE001
        results.append((name, "FAIL", f"{type(e).__name__}: {e}"))


LEDGER = Path(tempfile.mkdtemp(prefix="orch-battery-")) / "tasks.jsonl"
orch = Orchestrator(str(LEDGER))


def full_lifecycle():
    t = orch.propose(
        title="battery-happy-path",
        intent="synthetic verification task",
        assignee_profile="noesis-signal",
        required_capability="cross_validate",
        acceptance_criteria=["three sources cited"],
        verification=Verification(method="reviewer_signoff", evidence_required=True),
        idempotency_key="battery:happy:1",
        risk_tier="r0",
        timeout_s=900,
        max_attempts=2,
    )
    orch.enqueue(t.task_id)
    orch.claim(t.task_id, claimed_by="noesis-signal")
    orch.start(t.task_id)
    orch.submit_handoff(t.task_id, {
        "summary": "done",
        "artifacts": [{"path": "workspace/research/brief.md", "checksum": None, "kind": "brief"}],
        "verification_result": {"passed": True, "evidence": "3 sources"},
        "unmet_criteria": [],
    })
    orch.succeed(t.task_id)
    return orch.store.get(t.task_id).state


def r3_requires_approval_manifest():
    t = orch.propose(
        title="battery-r3",
        intent="high-risk bounded op",
        assignee_profile="noesis-signal",
        required_capability="cross_validate",
        acceptance_criteria=["ok"],
        verification=Verification(method="operator_signoff"),
        idempotency_key="battery:r3:1",
        risk_tier="r3",
        timeout_s=900,
    )
    if not (t.approval and t.approval.required):
        raise AssertionError("r3 task did not require approval")
    d0 = orch.dispatchable(t.task_id)
    if d0.allowed:
        raise AssertionError("r3 dispatchable before any approval")
    try:
        orch.approve(t.task_id, operator="elvis")  # missing approval_manifest_id
    except Exception as e:  # noqa: BLE001
        blocked = str(e)
    else:
        raise AssertionError("r3 approved without manifest — GATE BYPASSED")
    orch.approve(t.task_id, operator="elvis", approval_manifest_id="manifest-battery-001")
    orch.enqueue(t.task_id)
    d1 = orch.dispatchable(t.task_id)
    if not d1.allowed:
        raise AssertionError(f"r3 with manifest not dispatchable: {d1.reason}")
    return f"pending-gate ok; approve-without-manifest refused ({blocked[:60]}); with manifest dispatchable"


def unknown_assignee_refused():
    try:
        orch.propose(
            title="battery-bad-assignee",
            intent="x",
            assignee_profile="not-a-real-profile",
            required_capability="cross_validate",
            acceptance_criteria=["x"],
            verification=Verification(method="reviewer_signoff"),
            idempotency_key="battery:badassignee:1",
            risk_tier="r0",
            timeout_s=60,
        )
    except Exception as e:  # noqa: BLE001
        return f"refused: {e}"
    raise AssertionError("unknown assignee accepted")


def orchestrator_cannot_execute():
    try:
        orch.propose(
            title="battery-self-assign",
            intent="x",
            assignee_profile="noesis-orchestrator",
            required_capability="cross_validate",
            acceptance_criteria=["x"],
            verification=Verification(method="reviewer_signoff"),
            idempotency_key="battery:selfassign:1",
            risk_tier="r0",
            timeout_s=60,
        )
    except Exception as e:  # noqa: BLE001
        return f"refused: {e}"
    raise AssertionError("orchestrator assigned its own work")


def undeclared_capability_refused():
    try:
        orch.propose(
            title="battery-bad-capability",
            intent="x",
            assignee_profile="noesis-signal",
            required_capability="delete_production",
            acceptance_criteria=["x"],
            verification=Verification(method="reviewer_signoff"),
            idempotency_key="battery:badcap:1",
            risk_tier="r0",
            timeout_s=60,
        )
    except Exception as e:  # noqa: BLE001
        return f"refused: {e}"
    raise AssertionError("undeclared capability accepted")


def succeed_without_handoff_refused():
    t = orch.propose(
        title="battery-nohandoff",
        intent="x",
        assignee_profile="noesis-signal",
        required_capability="cross_validate",
        acceptance_criteria=["x"],
        verification=Verification(method="reviewer_signoff", evidence_required=True),
        idempotency_key="battery:nohandoff:1",
        risk_tier="r0",
        timeout_s=900,
    )
    orch.enqueue(t.task_id)
    orch.claim(t.task_id, claimed_by="noesis-signal")
    orch.start(t.task_id)
    try:
        orch.succeed(t.task_id)
    except Exception as e:  # noqa: BLE001
        return f"refused: {e}"
    raise AssertionError("succeeded without handoff evidence")


def idempotency_no_duplicate():
    kwargs = dict(
        title="battery-idem", intent="x", assignee_profile="noesis-signal",
        required_capability="cross_validate", acceptance_criteria=["x"],
        verification=Verification(method="reviewer_signoff"),
        risk_tier="r0", timeout_s=60,
    )
    before = len(orch.store.all_tasks())
    t1 = orch.propose(idempotency_key="battery:idem:1", **kwargs)
    t2 = orch.propose(idempotency_key="battery:idem:1", **kwargs)
    after = len(orch.store.all_tasks())
    if str(t1.task_id) == str(t2.task_id) and after == before + 1:
        return f"same task returned; ledger grew by exactly 1 ({before}->{after})"
    raise AssertionError(f"duplicate work: t1={t1.task_id} t2={t2.task_id} {before}->{after}")


def emergency_stop_durable_across_replay():
    orch2 = Orchestrator(str(LEDGER))  # replay existing ledger
    t = orch2.propose(
        title="battery-estop", intent="x", assignee_profile="noesis-signal",
        required_capability="cross_validate", acceptance_criteria=["x"],
        verification=Verification(method="reviewer_signoff"),
        idempotency_key="battery:estop:1", risk_tier="r0", timeout_s=900,
    )
    orch2.enqueue(t.task_id)
    orch2.emergency_stop(operator="elvis", reason="battery test")
    d1 = orch2.dispatchable(t.task_id)
    orch3 = Orchestrator(str(LEDGER))  # fresh process replay: breaker must still be open
    d3 = orch3.dispatchable(t.task_id)
    orch3.resume(operator="elvis")
    d4 = orch3.dispatchable(t.task_id)
    ok = (not d1.allowed) and (not d3.allowed) and d4.allowed
    if not ok:
        raise AssertionError(f"estop durability wrong: d1={d1} d3={d3} d4={d4}")
    return f"dispatch blocked under stop (this proc + replayed proc), resumes cleanly"


def synthesis_reports_evidence():
    orch4 = Orchestrator(str(LEDGER))
    happy = orch4.store.find_by_idempotency_key("battery:happy:1")
    syn = orch4.synthesize(happy.correlation_id)
    if not (isinstance(syn, dict) and syn.get("complete") and syn.get("task_count") >= 1 and syn.get("evidence")):
        raise AssertionError(f"synthesis incomplete: {syn}")
    return f"complete={syn['complete']} task_count={syn['task_count']} evidence_items={len(syn['evidence'])}"


check("FULL_LIFECYCLE_HAPPY_PATH", full_lifecycle)
check("R3_REQUIRES_APPROVAL_MANIFEST", r3_requires_approval_manifest)
check("UNKNOWN_ASSIGNEE_REFUSED", unknown_assignee_refused)
check("ORCHESTRATOR_CANNOT_EXECUTE", orchestrator_cannot_execute)
check("UNDECLARED_CAPABILITY_REFUSED", undeclared_capability_refused)
check("SUCCEED_WITHOUT_HANDOFF_REFUSED", succeed_without_handoff_refused)
check("IDEMPOTENCY_NO_DUPLICATE", idempotency_no_duplicate)
check("EMERGENCY_STOP_DURABLE_ACROSS_REPLAY", emergency_stop_durable_across_replay)
check("SYNTHESIS_REPORTS_EVIDENCE", synthesis_reports_evidence)

print(f"{'TEST':42s} {'RESULT':6s} DETAIL")
for name, res, detail in results:
    print(f"{name:42s} {res:6s} {detail[:110]}")
npass = sum(1 for r in results if r[1] == "PASS")
print(f"\nORCHESTRATOR BATTERY: {npass}/{len(results)} passed; ledger={LEDGER}")
sys.exit(0 if npass == len(results) else 1)
