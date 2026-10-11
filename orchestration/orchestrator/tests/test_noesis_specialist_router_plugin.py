"""Plugin registration tests for the Noesis specialist router."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PLUGIN = ROOT / "integrations" / "hermes-plugin" / "noesis-specialist-router" / "__init__.py"


class FakeContext:
    def __init__(self) -> None:
        self.tools = {}

    def register_tool(self, *, name, toolset, schema, handler, description):
        self.tools[name] = {
            "toolset": toolset,
            "schema": schema,
            "handler": handler,
            "description": description,
        }


def _load_plugin():
    spec = importlib.util.spec_from_file_location("noesis_specialist_router", PLUGIN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_noesis_specialist_router_plugin_registers_explicit_dispatch_tool() -> None:
    module = _load_plugin()
    ctx = FakeContext()

    module.register(ctx)

    tool = ctx.tools["noesis_specialist_delegate"]
    assert tool["toolset"] == "noesis_specialist_router"
    assert "parent-inheriting delegate_task fallback" in tool["description"]
    assert tool["schema"]["parameters"]["required"] == ["title", "intent", "idempotency_key"]


def test_noesis_specialist_router_plugin_invokes_profile_attested_path(tmp_path: Path) -> None:
    module = _load_plugin()
    ctx = FakeContext()
    module.register(ctx)

    raw = ctx.tools["noesis_specialist_delegate"]["handler"]({
        "title": "plugin architecture route",
        "intent": "Design an MCP/Hermes profile contract for a new memory agent",
        "idempotency_key": "plugin:architecture-route",
        "acceptance_criteria": ["explicit specialist"],
        "verification": {"method": "launcher_attestation"},
        "dry_run": True,
        "ledger_path": str(tmp_path / "plugin-ledger.jsonl"),
        "events_path": str(tmp_path / "plugin-events.jsonl"),
    })

    result = json.loads(raw)
    assert result["success"] is True
    assert result["selected_agent"] == "noesis-architect"
    assert result["requested_profile_id"] == "noesis-architect"
    assert result["loaded_profile_id"] == "noesis-architect"
