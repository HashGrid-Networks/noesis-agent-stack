"""Operator CLI. Scheduling and live indexes are out of scope for every command."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .engine import (
    IngestConfig,
    discover,
    host_timezone_name,
    propose_schedule,
    query_current,
    run_ingestion,
    status_document,
)


def _config(args: argparse.Namespace) -> IngestConfig:
    return IngestConfig(
        projects_root=Path(args.projects_root),
        state_dir=Path(args.state_dir),
        dry_run=getattr(args, "dry_run", False),
        max_runtime_s=getattr(args, "max_runtime_s", 900.0),
        host_tz=getattr(args, "host_tz", None) or None,
    )


def _add_roots(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--projects-root", default=str(Path.home() / "projects"))
    parser.add_argument(
        "--state-dir",
        default=str(Path.home() / ".local" / "share" / "noesis" / "codebase-state"),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="codebase_state")
    sub = parser.add_subparsers(dest="cmd", required=True)

    discover_p = sub.add_parser("discover", help="list matching directories; write nothing")
    _add_roots(discover_p)

    run_p = sub.add_parser("run", help="collect and publish a local manifest")
    _add_roots(run_p)
    run_p.add_argument("--dry-run", action="store_true")
    run_p.add_argument("--max-runtime-s", type=float, default=900.0)

    status_p = sub.add_parser("status", help="show local state; does not contact a scheduler")
    _add_roots(status_p)

    query_p = sub.add_parser("query", help="substring search of the current local snapshot")
    _add_roots(query_p)
    query_p.add_argument("text")

    propose = sub.add_parser("propose-schedule", help="print a crontab proposal; do not install it")
    propose.add_argument("--host-tz", default="")
    propose.add_argument("--hour", type=int, default=2)
    propose.add_argument("--minute", type=int, default=30)

    args = parser.parse_args(argv)
    if args.cmd == "discover":
        rows = discover(_config(args))
        json.dump({"matches": rows, "schedule_installed": False}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    if args.cmd == "run":
        result = run_ingestion(_config(args))
        json.dump(result.report, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0 if result.report.get("status") in {"success", "partial", "skipped"} else 1
    if args.cmd == "status":
        json.dump(status_document(Path(args.state_dir)), sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    if args.cmd == "query":
        json.dump(query_current(Path(args.state_dir), args.text), sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    if args.cmd == "propose-schedule":
        host = args.host_tz or host_timezone_name()
        payload = propose_schedule(host_tz=host, hour=args.hour, minute=args.minute)
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0 if payload.get("timezone_match") else 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
