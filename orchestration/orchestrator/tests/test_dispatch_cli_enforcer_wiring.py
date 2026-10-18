"""Live CLI/plugin callers must wire the enforcer and forward assignment epoch.

These tests exercise ``specialist_dispatch_cli.main`` and the repository plugin
handler — the surfaces a live orchestrator actually calls — not only
``_assert_spawn_gates``.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from app.specialist_dispatch_cli import _make_orchestrator, main

ROOT = Path(__file__).resolve().parents[3]
PLUGIN = ROOT / "integrations" / "hermes-plugin" / "noesis-specialist-router" / "__init__.py"


class _FakeContext:
    def __init__(self) -> None:
        self.tools: dict = {}

    def register_tool(self, *, name, toolset, schema, handler, description) -> None:
        self.tools[name] = {"handler": handler, "schema": schema}


def _load_plugin():
    spec = importlib.util.spec_from_file_location("noesis_specialist_router_epoch", PLUGIN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _payload(**overrides) -> dict:
    body = {
        "title": "bounded route",
        "intent": "Implement a deterministic input validation function",
        "idempotency_key": "cli:enforcer-epoch",
        "acceptance_criteria": ["typed refusal before spawn"],
        "verification": {"method": "manual"},
        "dry_run": False,
        "approved_provider": "not-a-lane",
        "approved_model": "not-a-model",
        "risk_tier": "r0",
    }
    body.update(overrides)
    return body


def test_make_orchestrator_wires_env_enforcer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INFERENCE_ROUTING_MODE", "enforce")
    orch = _make_orchestrator({"title": "wire"}, tmp_path / "ledger.jsonl", None)
    assert orch.inference_enforcer is not None
    assert orch.inference_enforcer.config.mode == "enforce"


def test_cli_non_dry_run_without_epoch_is_epoch_required_not_missing_enforcer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("INFERENCE_ROUTING_MODE", "enforce")
    monkeypatch.setattr(
        "app.launcher.HermesCliAdapter.launch",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("spawned")),
    )
    path = tmp_path / "task.json"
    path.write_text(json.dumps(_payload(workspace=str(tmp_path))), encoding="utf-8")
    code = main(["--input", str(path), "--ledger", str(tmp_path / "ledger.jsonl")])
    captured = json.loads(capsys.readouterr().out)
    assert code == 2
    assert captured["code"] == "epoch_required"


def test_cli_forwards_expected_epoch_and_does_not_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("INFERENCE_ROUTING_MODE", "enforce")
    monkeypatch.setattr(
        "app.launcher.HermesCliAdapter.launch",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("spawned")),
    )
    path = tmp_path / "task.json"
    path.write_text(
        json.dumps(_payload(workspace=str(tmp_path), expected_epoch=0, idempotency_key="cli:epoch-0")),
        encoding="utf-8",
    )
    code = main(["--input", str(path), "--ledger", str(tmp_path / "ledger.jsonl")])
    captured = json.loads(capsys.readouterr().out)
    assert code == 2
    assert captured["code"] not in {"enforcer_unavailable", "epoch_required"}


def test_cli_uses_canonical_preflight_entrypoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The live CLI must enter through preflight_then_route, not the legacy launcher."""
    seen: dict[str, object] = {}

    def _preflight(self, **kwargs):
        seen.update(kwargs)
        from app.specialist_routing import RoutingBlocked

        raise RoutingBlocked("preflight_probe", "canonical entrypoint reached")

    def _legacy(self, **kwargs):
        raise AssertionError("route_and_launch must not be the live CLI entrypoint")

    monkeypatch.setattr("app.control_plane.Orchestrator.preflight_then_route", _preflight)
    monkeypatch.setattr("app.control_plane.Orchestrator.route_and_launch", _legacy)
    path = tmp_path / "task.json"
    path.write_text(
        json.dumps(
            _payload(
                workspace=str(tmp_path),
                expected_epoch=0,
                timeout_s=120,
                reviewer_profile="noesis-sentinel",
                idempotency_key="cli:canonical-preflight",
            )
        ),
        encoding="utf-8",
    )
    code = main(["--input", str(path), "--ledger", str(tmp_path / "ledger.jsonl")])
    captured = json.loads(capsys.readouterr().out)
    assert code == 2
    assert captured["code"] == "preflight_probe"
    assert seen.get("expected_epoch") == 0
    assert seen.get("timeout_s") == 120
    assert seen.get("reviewer_profile") == "noesis-sentinel"


def test_plugin_forwards_expected_epoch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INFERENCE_ROUTING_MODE", "enforce")
    monkeypatch.setattr(
        "app.launcher.HermesCliAdapter.launch",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("spawned")),
    )
    module = _load_plugin()
    ctx = _FakeContext()
    module.register(ctx)
    raw = ctx.tools["noesis_specialist_delegate"]["handler"](
        {
            **_payload(idempotency_key="plugin:epoch-0"),
            "expected_epoch": 0,
            "workspace": str(tmp_path),
            "ledger_path": str(tmp_path / "ledger.jsonl"),
            "events_path": str(tmp_path / "events.jsonl"),
        }
    )
    captured = json.loads(raw)
    assert captured["success"] is False
    assert captured["code"] not in {"enforcer_unavailable", "epoch_required", "plugin_admission_blocked"}


def test_plugin_forwards_timeout_and_reviewer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def _preflight(self, **kwargs):
        seen.update(kwargs)
        from app.specialist_routing import RoutingBlocked

        raise RoutingBlocked("preflight_probe", "plugin canonical entrypoint")

    monkeypatch.setattr("app.control_plane.Orchestrator.preflight_then_route", _preflight)
    module = _load_plugin()
    ctx = _FakeContext()
    module.register(ctx)
    raw = ctx.tools["noesis_specialist_delegate"]["handler"](
        {
            **_payload(idempotency_key="plugin:contract-fields"),
            "timeout_s": 120,
            "max_attempts": 1,
            "reviewer_profile": "noesis-sentinel",
            "ledger_path": str(tmp_path / "ledger.jsonl"),
            "events_path": str(tmp_path / "events.jsonl"),
        }
    )
    captured = json.loads(raw)
    assert captured["code"] == "preflight_probe"
    assert seen.get("timeout_s") == 120
    assert seen.get("max_attempts") == 1
    assert seen.get("reviewer_profile") == "noesis-sentinel"
