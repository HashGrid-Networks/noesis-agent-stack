"""Hermes plugin exposing the Noesis explicit specialist dispatch path."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "orchestration" / "orchestrator" / "app").is_dir():
            return parent
    configured = Path(str(__import__("os").environ.get("NOESIS_AGENT_STACK_ROOT", ""))).expanduser()
    if configured and (configured / "orchestration" / "orchestrator" / "app").is_dir():
        return configured
    raise RuntimeError("NOESIS_AGENT_STACK_ROOT must point to noesis-agent-stack")


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


DELEGATE_DESCRIPTION = (
    "Route a bounded Noesis task through the explicit specialist dispatch path. "
    "This selects an eligible specialist, launches with an explicit Hermes profile, "
    "and verifies requested-vs-loaded profile identity. It does not use parent-"
    "inheriting delegate_task fallback."
)

DELEGATE_SCHEMA = {
    "name": "noesis_specialist_delegate",
    "description": DELEGATE_DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "intent": {"type": "string"},
            "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
            "verification": {"type": "object"},
            "idempotency_key": {"type": "string"},
            "required_capability": {"type": "string"},
            "risk_tier": {"type": "string"},
            "dry_run": {"type": "boolean"},
            "allow_generic_fallback": {"type": "boolean"},
        },
        "required": ["title", "intent", "idempotency_key"],
    },
}


def _delegate_payload(args: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": str(args["title"]),
        "intent": str(args["intent"]),
        "acceptance_criteria": [str(item) for item in args.get("acceptance_criteria", [])],
        "verification": args.get("verification") or {"method": "manual"},
        "idempotency_key": str(args["idempotency_key"]),
        "required_capability": args.get("required_capability"),
        "risk_tier": str(args.get("risk_tier", "r0")),
        "dry_run": bool(args.get("dry_run", True)),
        "allow_generic_fallback": bool(args.get("allow_generic_fallback", False)),
        "approved_provider": args.get("approved_provider"),
        "approved_model": args.get("approved_model"),
        "workspace": args.get("workspace"),
        "ledger_path": args.get("ledger_path"),
        "events_path": args.get("events_path"),
    }


def register(ctx):
    def delegate(args: dict[str, Any], **kwargs) -> str:
        del kwargs
        try:
            repo = _repo_root()
            sys.path.insert(0, str(repo / "orchestration" / "orchestrator"))
            from app.specialist_dispatch_cli import _blocked_payload, _make_orchestrator, _success_payload
            from app.specialist_routing import RoutingBlocked

            payload = _delegate_payload(args)
            ledger = payload.get("ledger_path") or __import__("os").environ.get("NOESIS_TASK_LEDGER")
            events = payload.get("events_path") or __import__("os").environ.get("NOESIS_TASK_EVENTS")
            if not ledger:
                import tempfile
                ledger = str(Path(tempfile.mkdtemp(prefix="noesis-plugin-ledger-")) / "tasks.jsonl")
            ledger_path = Path(str(ledger))
            events_path = Path(str(events)) if events else ledger_path.with_name("noesis-specialist-plugin-events.jsonl")
            orch = _make_orchestrator(payload, ledger_path, events_path)
            try:
                result = orch.route_and_launch(
                    title=payload["title"],
                    intent=payload["intent"],
                    acceptance_criteria=payload["acceptance_criteria"],
                    verification=__import__("app.models", fromlist=["Verification"]).Verification(
                        method=str(payload["verification"].get("method", "manual")),
                        command=payload["verification"].get("command"),
                    ),
                    idempotency_key=payload["idempotency_key"],
                    required_capability=payload.get("required_capability"),
                    risk_tier=payload["risk_tier"],
                    dry_run=payload["dry_run"],
                    allow_generic_fallback=payload["allow_generic_fallback"],
                    approved_provider=payload.get("approved_provider"),
                    approved_model=payload.get("approved_model"),
                    workspace=payload.get("workspace"),
                )
            except RoutingBlocked as exc:
                return _json(_blocked_payload(exc))
            return _json(_success_payload(result))
        except Exception as exc:  # pragma: no cover - runtime boundary
            return _json({"success": False, "code": "router_unavailable", "reason": str(exc)})

    ctx.register_tool(
        name="noesis_specialist_delegate",
        toolset="noesis_specialist_router",
        schema=DELEGATE_SCHEMA,
        handler=delegate,
        description=DELEGATE_DESCRIPTION,
    )
