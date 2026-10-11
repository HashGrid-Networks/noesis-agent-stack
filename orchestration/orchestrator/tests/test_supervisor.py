"""Supervisor daemon tests: single-sweep mode, stale-lease sweep, JSONL shape."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from app.control_plane import Orchestrator
from supervisor import main, run_sweep

from .conftest import drive_to_running, make_task


def test_once_on_empty_ledger_exits_zero(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    ledger = tmp_path / "tasks.jsonl"
    rc = main(["--ledger", str(ledger), "--once"])
    assert rc == 0
    lines = [json.loads(l) for l in capsys.readouterr().out.strip().splitlines()]
    assert lines[0]["event"] == "supervisor_start"
    assert lines[-1]["event"] == "supervisor_stop"
    sweep = [l for l in lines if l["event"] == "sweep"]
    assert sweep and sweep[0]["swept"] == 0


def test_sweep_reaps_expired_lease(tmp_path: Path) -> None:
    orch = Orchestrator(tmp_path / "tasks.jsonl")
    task = make_task(orch, timeout_s=0.05, idempotency_key="supervisor-test:expire:1")
    drive_to_running(orch, task)

    time.sleep(0.1)
    summary = run_sweep(orch)
    assert summary["swept"] == 1
    assert str(task.task_id) in summary["swept_task_ids"]
    assert orch.store.all_tasks()[str(task.task_id)].state in ("failed", "queued")


def test_supervisor_reports_breaker_state(tmp_path: Path) -> None:
    orch = Orchestrator(tmp_path / "tasks.jsonl")
    orch.emergency_stop(operator="elvis", reason="test")
    summary = run_sweep(orch)
    assert summary["breaker_open"] is True
