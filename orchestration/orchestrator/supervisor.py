"""Supervisor sweep daemon for the noesis-orchestrator control plane.

Implements the WORKFLOWS.md §5 `orchestrated-task` scheduled trigger:
"Scheduled: A recurring supervision sweep for timeouts and stale leases".

The supervisor holds no execution authority: it constructs the Orchestrator
(replaying the append-only ledger), periodically calls ``sweep_timeouts()``,
and emits structured JSONL log lines. All state mutations are performed by the
control plane and durably recorded in the ledger; the circuit-breaker state is
restored from the ledger on startup.

Usage:
    python supervisor.py [--ledger PATH] [--interval SECONDS] [--once]

Exit codes: 0 clean shutdown / single sweep; 2 runtime error.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from collections import Counter
from pathlib import Path

from app.control_plane import Orchestrator

DEFAULT_LEDGER = Path(__file__).resolve().parents[2] / "workspace" / "orchestrator" / "tasks.jsonl"


def _log(record: dict) -> None:
    print(json.dumps(record, sort_keys=True), flush=True)


def run_sweep(orch: Orchestrator) -> dict:
    """One supervision sweep; returns a summary record (never raises)."""
    swept = orch.sweep_timeouts()
    states = Counter(t.state for t in orch.store.all_tasks().values())
    return {
        "event": "sweep",
        "swept": len(swept),
        "swept_task_ids": [str(t.task_id) for t in swept],
        "tasks_by_state": dict(states),
        "breaker_open": orch.breaker.is_open,
        "replay_error": orch.store.replay_error,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="noesis-orchestrator supervision sweep daemon")
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER), help="append-only task ledger path")
    parser.add_argument("--interval", type=float, default=60.0, help="seconds between sweeps")
    parser.add_argument("--once", action="store_true", help="run a single sweep and exit")
    args = parser.parse_args(argv)

    orch = Orchestrator(args.ledger)
    _log({"event": "supervisor_start", "ledger": str(args.ledger), "interval_s": args.interval})

    stopping = False

    def _handle_sigterm(signum, frame):  # noqa: ANN001
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)

    try:
        while True:
            summary = run_sweep(orch)
            if summary["swept"] or summary["replay_error"] or summary["breaker_open"]:
                _log(summary)
            else:
                # quiet heartbeat: emit a compact line so launchd logs show liveness
                _log({"event": "sweep", "swept": 0, "tasks": sum(summary["tasks_by_state"].values())})
            if args.once or stopping:
                break
            time.sleep(args.interval)
    except Exception as exc:  # noqa: BLE001 — daemon must fail loudly, not loop silently
        _log({"event": "supervisor_error", "error": str(exc)})
        return 2

    _log({"event": "supervisor_stop"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
