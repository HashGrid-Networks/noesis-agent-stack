"""Persisted-fold TaskStore authority (changed-approach Alternative A)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.control_plane import Orchestrator, PolicyViolation
from app.delegation import (
    ActivationError,
    ORCHESTRATOR_ID,
    DelegationStore,
)
from app.store import TaskStore, TaskVersionConflict

from tests.test_lease_lifecycle import (
    _FakeAdapter,
    _dispatched_coding,
    _handoff,
    _running_task,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
HISTORICAL_LEDGER = REPO_ROOT / "workspace/subgoal5-sandbox/control-plane-ledger.jsonl"
ORCH_PATH = str(REPO_ROOT / "orchestration/orchestrator")
PYTHON = sys.executable

# The historical ledger is a gitignored sandbox artifact (workspace/ is
# gitignored); absent checkouts skip rather than fail.
requires_historical_ledger = pytest.mark.skipif(
    not HISTORICAL_LEDGER.is_file(),
    reason=f"historical sandbox fixture absent (gitignored): {HISTORICAL_LEDGER}",
)

CHILD_PUT = r"""
import sys
import time
from pathlib import Path
from app.store import TaskStore, TaskVersionConflict
ledger, tid, expected, cand, loaded_flag, go_flag = sys.argv[1:7]
store = TaskStore(ledger)
task = store.get(tid)
assert task is not None
task.delegation.candidate_revision = cand
Path(loaded_flag).write_text("loaded", encoding="utf-8")
deadline = time.time() + 10
while not Path(go_flag).exists():
    if time.time() > deadline:
        print("TIMEOUT_GO")
        raise SystemExit(2)
    time.sleep(0.01)
try:
    store.put(task, event="child_put", expected_task_version=int(expected))
    print("COMMITTED")
except TaskVersionConflict as exc:
    print(f"CONFLICT:{exc.expected}:{exc.actual}")
"""


def _orch(tmp_path: Path, name: str = "tasks.jsonl") -> Orchestrator:
    orch = Orchestrator(tmp_path / name)
    orch.runtime_adapter = _FakeAdapter("noesis-forge")
    return orch


def _versions_on_disk(ledger: Path, task_id: str) -> list[int]:
    versions: list[int] = []
    for line in ledger.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        task = rec.get("task") or {}
        if str(task.get("task_id")) == str(task_id):
            versions.append(int(task.get("task_version") or 0))
    return versions


def test_two_processes_stale_child_cannot_commit_same_next_version(tmp_path: Path) -> None:
    orch = _orch(tmp_path)
    task = _dispatched_coding(orch, idem="forge:fold:proc:1")
    tid = str(task.task_id)
    expected = int(task.task_version)
    ledger = orch.store.ledger_path
    loaded_flag = tmp_path / "child-loaded"
    go_flag = tmp_path / "parent-go"

    env = dict(os.environ)
    env["PYTHONPATH"] = ORCH_PATH
    proc = subprocess.Popen(
        [
            PYTHON,
            "-c",
            CHILD_PUT,
            str(ledger),
            tid,
            str(expected),
            "child-loser",
            str(loaded_flag),
            str(go_flag),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
    )
    deadline = time.time() + 10
    while not loaded_flag.exists():
        if proc.poll() is not None or time.time() > deadline:
            out, err = proc.communicate(timeout=5)
            raise AssertionError(f"child did not load: {out} {err}")
        time.sleep(0.01)

    winner = orch.store.get(tid)
    assert winner is not None
    winner.delegation.candidate_revision = "parent-winner"
    orch.store.put(winner, event="parent_put", expected_task_version=expected)
    go_flag.write_text("go", encoding="utf-8")
    out, err = proc.communicate(timeout=10)
    assert proc.returncode == 0, out + err
    assert out.strip().startswith("CONFLICT:"), out
    rebuilt = TaskStore(ledger)
    held = rebuilt.get(tid)
    assert held is not None
    assert held.delegation.candidate_revision == "parent-winner"
    versions = _versions_on_disk(ledger, tid)
    assert versions.count(expected + 1) == 1
    assert max(versions) == expected + 1


def test_repeated_two_process_contention_unique_monotonic_versions(tmp_path: Path) -> None:
    for index in range(8):
        test_two_processes_stale_child_cannot_commit_same_next_version(tmp_path / f"run-{index}")


def test_two_taskstore_instances_stale_put_loses(tmp_path: Path) -> None:
    orch = _orch(tmp_path, "inst.jsonl")
    task = _dispatched_coding(orch, idem="forge:fold:inst:1")
    tid = str(task.task_id)
    ledger = orch.store.ledger_path
    a = TaskStore(ledger)
    b = TaskStore(ledger)
    ta = a.get(tid)
    tb = b.get(tid)
    assert ta is not None and tb is not None
    expected = ta.task_version
    ta.delegation.candidate_revision = "a-winner"
    a.put(ta, event="a_put", expected_task_version=expected)
    tb.delegation.candidate_revision = "b-loser"
    with pytest.raises(TaskVersionConflict):
        b.put(tb, event="b_put", expected_task_version=expected)
    held_b = b.get(tid)
    held_a = a.get(tid)
    assert held_b is not None and held_a is not None
    assert held_a.delegation.candidate_revision == "a-winner"
    assert held_b.delegation.candidate_revision == "a-winner"
    versions = _versions_on_disk(ledger, tid)
    assert versions.count(expected + 1) == 1


def test_mutating_get_output_does_not_mutate_memory_or_disk(tmp_path: Path) -> None:
    orch = _orch(tmp_path)
    task = _dispatched_coding(orch, idem="forge:fold:copy:1")
    tid = str(task.task_id)
    before = orch.store.ledger_path.read_bytes()
    got = orch.store.get(tid)
    assert got is not None
    live = orch.store._tasks[tid]
    got.delegation.candidate_revision = "evil"
    got.title = "mutated-title"
    assert live.delegation.candidate_revision != "evil"
    assert live.title != "mutated-title"
    again = orch.store.get(tid)
    assert again is not None
    assert again.delegation.candidate_revision != "evil"
    assert again.title == task.title
    assert orch.store.ledger_path.read_bytes() == before
    assert got is not live
    assert again is not live


def test_failed_cas_cannot_taint_later_successful_write(tmp_path: Path) -> None:
    orch = _orch(tmp_path)
    task = _dispatched_coding(orch, idem="forge:fold:taint:1")
    tid = str(task.task_id)
    current = orch.store.get(tid)
    assert current is not None
    original = current.delegation.candidate_revision
    original_version = current.task_version
    current.delegation.candidate_revision = "evil"
    with pytest.raises(TaskVersionConflict):
        orch.store.put(current, event="taint", expected_task_version=0)
    held = orch.store.get(tid)
    assert held is not None
    assert held.delegation.candidate_revision == original
    assert held.task_version == original_version
    later = orch.store.get(tid)
    assert later is not None
    later.delegation.runtime_profile = "noesis-forge"
    orch.store.put(later, event="later_ok", expected_task_version=later.task_version)
    final = orch.store.get(tid)
    assert final is not None
    assert final.delegation.candidate_revision == original
    assert final.delegation.candidate_revision != "evil"


@requires_historical_ledger
def test_historical_ledger_copy_refuses_worker_actions_without_grant(tmp_path: Path) -> None:
    dest = tmp_path / "historical.jsonl"
    shutil.copyfile(HISTORICAL_LEDGER, dest)
    orch = Orchestrator(dest)
    orch.runtime_adapter = _FakeAdapter("noesis-forge")
    target = None
    for task in orch.store.all_tasks().values():
        if task.delegation.candidate_revision == "cand-subgoal5-v1":
            target = task
            break
    assert target is not None
    assert target.delegation.attempt_epoch is None
    assert target.delegation.execution_released_at is None
    with pytest.raises(PolicyViolation, match="stale_attempt|required|grant"):
        orch.submit_candidate(target.task_id, candidate_revision="cand-invented")
    with pytest.raises(PolicyViolation, match="stale_attempt|required|grant"):
        orch.observe_budget(target.task_id, requests=9)
    held = orch.store.get(target.task_id)
    assert held is not None
    assert held.delegation.candidate_revision == "cand-subgoal5-v1"
    assert held.delegation.attempt_epoch is None
    assert held.delegation.requests_used == target.delegation.requests_used
    assert held.delegation.accepted_by is None

    dstore = DelegationStore(tmp_path / "delegated.jsonl")
    _running_task(dstore, "t-legacy-strip")
    line = json.loads(dstore.ledger_path.read_text(encoding="utf-8").splitlines()[-1])
    line["task"]["lease"].pop("attempt_epoch", None)
    dstore.ledger_path.write_text(json.dumps(line) + "\n", encoding="utf-8")
    legacy = DelegationStore(dstore.ledger_path)
    again = legacy.get("t-legacy-strip")
    assert again is not None and again.lease is not None
    assert again.lease.attempt_epoch is None
    with pytest.raises(ActivationError, match="stale_attempt|required|grant"):
        legacy.start_running("t-legacy-strip", by="noesis-forge")
    with pytest.raises(ActivationError, match="stale_attempt|required|grant"):
        legacy.consume_budget("t-legacy-strip", 0.0, by="noesis-forge")
    with pytest.raises(ActivationError, match="stale_attempt|required|grant"):
        legacy.lease_renew("t-legacy-strip", duration=60, expected_attempt_epoch=1, by="noesis-forge")
    with pytest.raises(ActivationError, match="stale_attempt|required|grant"):
        legacy.submit_verifying("t-legacy-strip", by="noesis-forge", candidate_revision="cand-x")


def test_newly_dispatched_task_has_orchestrator_authored_epoch(tmp_path: Path) -> None:
    orch = _orch(tmp_path)
    task = _dispatched_coding(orch, idem="forge:fold:epoch:1")
    assert task.delegation.attempt_epoch == 1
    rebuilt = Orchestrator(orch.store.ledger_path)
    again = rebuilt.store.get(task.task_id)
    assert again is not None
    assert again.delegation.attempt_epoch == 1
    assert again.delegation.execution_released_at is None


def test_worker_epoch_omission_fails_on_first_submit(tmp_path: Path) -> None:
    orch = _orch(tmp_path)
    task = _dispatched_coding(orch, idem="forge:fold:omit:1")
    orch.submit_handoff(task.task_id, _handoff())
    with pytest.raises(PolicyViolation, match="stale_attempt|required"):
        orch.submit_candidate(task.task_id, candidate_revision="rev-1")
    held = orch.store.get(task.task_id)
    assert held is not None
    assert held.delegation.candidate_revision is None
    submitted = orch.submit_candidate(
        task.task_id, candidate_revision="rev-1", attempt_epoch=1
    )
    assert submitted.delegation.attempt_epoch == 1
    assert submitted.delegation.candidate_revision == "rev-1"


def test_worker_submit_differing_from_granted_revision_or_digest_fails(tmp_path: Path) -> None:
    orch = _orch(tmp_path)
    task = _dispatched_coding(orch, idem="forge:fold:bind:1")
    orch.submit_handoff(task.task_id, _handoff())
    first = orch.submit_candidate(
        task.task_id, candidate_revision="cand-1", candidate_digest="sha256:1", attempt_epoch=1
    )
    live = orch.store.get(task.task_id)
    assert live is not None
    granted = orch.grant_new_attempt(
        task.task_id,
        expected_task_version=live.task_version,
        intended_assignee="noesis-forge",
        intended_candidate_revision="cand-2",
        intended_candidate_digest="sha256:2",
    )
    assert granted.delegation.attempt_epoch == (first.delegation.attempt_epoch or 0) + 1
    with pytest.raises(PolicyViolation, match="granted_candidate|intended_candidate|mismatch"):
        orch.submit_candidate(
            task.task_id,
            candidate_revision="cand-OTHER",
            candidate_digest="sha256:2",
            attempt_epoch=granted.delegation.attempt_epoch,
        )
    with pytest.raises(PolicyViolation, match="granted_candidate|intended_candidate|mismatch"):
        orch.submit_candidate(
            task.task_id,
            candidate_revision="cand-2",
            candidate_digest="sha256:OTHER",
            attempt_epoch=granted.delegation.attempt_epoch,
        )
    held = orch.store.get(task.task_id)
    assert held is not None
    assert held.delegation.candidate_revision != "cand-OTHER"
    ok = orch.submit_candidate(
        task.task_id,
        candidate_revision="cand-2",
        candidate_digest="sha256:2",
        attempt_epoch=granted.delegation.attempt_epoch,
    )
    assert ok.delegation.candidate_revision == "cand-2"
    assert ok.delegation.candidate_digest == "sha256:2"


def test_replay_error_blocks_every_authority_sensitive_operation(tmp_path: Path) -> None:
    orch = _orch(tmp_path, "replay.jsonl")
    task = _dispatched_coding(orch, idem="forge:fold:replay:1")
    orch.submit_handoff(task.task_id, _handoff())
    orch.submit_candidate(
        task.task_id, candidate_revision="rev-1", attempt_epoch=1
    )
    raw_lines = orch.store.ledger_path.read_text(encoding="utf-8").splitlines()
    last = json.loads(raw_lines[-1])
    last["task"]["delegation"]["execution_released_at"] = "2020-01-01T00:00:00"
    orch.store.ledger_path.write_text(
        "\n".join(raw_lines[:-1] + [json.dumps(last)]) + "\n", encoding="utf-8"
    )
    broken = Orchestrator(orch.store.ledger_path)
    broken.runtime_adapter = _FakeAdapter("noesis-forge")
    assert broken.store.replay_error is not None
    tid = task.task_id
    with pytest.raises(PolicyViolation, match="replay"):
        broken.submit_candidate(tid, candidate_revision="rev-2", attempt_epoch=1)
    with pytest.raises(PolicyViolation, match="replay"):
        broken.grant_new_attempt(tid, expected_task_version=0)
    with pytest.raises(PolicyViolation, match="replay"):
        broken.observe_budget(tid, requests=1, attempt_epoch=1)
    with pytest.raises(PolicyViolation, match="replay"):
        broken.record_review(
            tid, stage="spec", verdict="PASS", reviewer="noesis-sentinel", candidate_revision="rev-1"
        )
    with pytest.raises(PolicyViolation, match="replay"):
        broken.accept(tid)
    with pytest.raises(PolicyViolation, match="replay"):
        broken.submit_handoff(tid, _handoff())
    with pytest.raises(PolicyViolation, match="replay"):
        broken.mark_dispatched(tid, runtime_profile="noesis-forge")


def test_append_fsync_failure_leaves_memory_and_disk_unchanged(tmp_path: Path) -> None:
    orch = _orch(tmp_path)
    task = _dispatched_coding(orch, idem="forge:fold:fsync:1")
    tid = str(task.task_id)
    before_disk = orch.store.ledger_path.read_bytes()
    before = orch.store.get(tid)
    assert before is not None
    before_cand = before.delegation.candidate_revision
    before_version = before.task_version

    def boom(_fd: int) -> None:
        raise OSError("simulated fsync failure")

    orch.store._sync = boom  # type: ignore[method-assign]
    working = orch.store.get(tid)
    assert working is not None
    working.delegation.candidate_revision = "should-not-stick"
    with pytest.raises(OSError, match="simulated fsync"):
        orch.store.put(working, event="fsync_fail", expected_task_version=working.task_version)
    assert orch.store.ledger_path.read_bytes() == before_disk
    held = orch.store.get(tid)
    assert held is not None
    assert held.delegation.candidate_revision == before_cand
    assert held.task_version == before_version
    assert orch.store._tasks[tid].delegation.candidate_revision == before_cand


def test_restart_reproduces_exact_winner_version_epoch(tmp_path: Path) -> None:
    orch = _orch(tmp_path)
    task = _dispatched_coding(orch, idem="forge:fold:restart:1")
    orch.submit_handoff(task.task_id, _handoff())
    submitted = orch.submit_candidate(
        task.task_id, candidate_revision="rev-win", candidate_digest="sha256:win", attempt_epoch=1
    )
    rebuilt = Orchestrator(orch.store.ledger_path)
    again = rebuilt.store.get(task.task_id)
    assert again is not None
    assert again.task_version == submitted.task_version
    assert again.delegation.attempt_epoch == submitted.delegation.attempt_epoch
    assert again.delegation.candidate_revision == "rev-win"
    assert again.delegation.candidate_digest == "sha256:win"
    assert again.delegation.execution_released_at == submitted.delegation.execution_released_at


def test_one_way_release_and_review_invalidation_remain_intact(tmp_path: Path) -> None:
    orch = _orch(tmp_path)
    task = _dispatched_coding(orch, idem="forge:fold:oneway:1")
    orch.submit_handoff(task.task_id, _handoff())
    submitted = orch.submit_candidate(
        task.task_id, candidate_revision="rev-1", attempt_epoch=1
    )
    released = submitted.delegation.execution_released_at
    assert released is not None
    orch.record_review(
        task.task_id, stage="spec", verdict="REQUEST_CHANGES", reviewer="noesis-sentinel", candidate_revision="rev-1"
    )
    with pytest.raises(PolicyViolation, match="stale_attempt|released"):
        orch.submit_candidate(task.task_id, candidate_revision="rev-2", attempt_epoch=1)
    with pytest.raises(PolicyViolation, match="stale_attempt|released"):
        orch.observe_budget(task.task_id, requests=1, attempt_epoch=1)
    after_review = orch.store.get(task.task_id)
    assert after_review is not None
    assert after_review.delegation.execution_released_at == released
    live = orch.store.get(task.task_id)
    assert live is not None
    granted = orch.grant_new_attempt(
        task.task_id,
        expected_task_version=live.task_version,
        intended_assignee="noesis-forge",
        intended_candidate_revision="rev-2",
    )
    assert granted.delegation.execution_released_at is None
    assert granted.delegation.reviews == []
    assert granted.delegation.attempt_epoch == 2
    held = orch.store.get(task.task_id)
    assert held is not None
    assert held.delegation.execution_released_at is None
