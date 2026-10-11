"""Hermes PluginManager load tests for the Noesis specialist router plugin."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
PLUGIN_SOURCE = ROOT / "integrations" / "hermes-plugin" / "noesis-specialist-router"
HERMES_SOURCE = Path.home() / ".hermes" / "hermes-agent"


def test_noesis_specialist_router_loads_through_hermes_plugin_manager(tmp_path, monkeypatch) -> None:
    """The plugin must load through Hermes' real plugin host, not only a fake ctx."""
    if not (HERMES_SOURCE / "hermes_cli" / "plugins.py").exists():
        raise AssertionError("Hermes source checkout with plugin host is required for this integration test")

    hermes_home = tmp_path / "hermes-home"
    plugin_dir = hermes_home / "plugins" / "noesis-specialist-router"
    shutil.copytree(PLUGIN_SOURCE, plugin_dir, ignore=shutil.ignore_patterns("__pycache__"))
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": ["noesis-specialist-router"]}}),
        encoding="utf-8",
    )

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("NOESIS_AGENT_STACK_ROOT", str(ROOT))
    monkeypatch.syspath_prepend(str(HERMES_SOURCE))
    monkeypatch.syspath_prepend(str(ROOT / "orchestration" / "orchestrator"))
    candidate_sites = [HERMES_SOURCE / "venv" / "lib" / "python3.11" / "site-packages"]
    candidate_sites.extend(
        sorted((Path.home() / ".hermes" / "installs").glob("**/venv/lib/python*/site-packages"))
    )
    for site_packages in candidate_sites:
        if site_packages.exists() and str(site_packages) not in sys.path:
            sys.path.append(str(site_packages))

    from hermes_cli.plugins import PluginManager
    from tools.registry import registry

    registry.deregister("noesis_specialist_delegate", scope=str(hermes_home))
    manager = PluginManager()
    manager.discover_and_load()

    entry = registry.get_entry("noesis_specialist_delegate", scope=str(hermes_home))
    assert entry is not None
    assert entry.toolset == "noesis_specialist_router"
    assert "explicit specialist dispatch path" in entry.description

    raw = entry.handler(
        {
            "title": "plugin-manager architecture route",
            "intent": "Design the Noesis profile contract for an architecture specialist",
            "idempotency_key": "plugin-manager:architecture-route",
            "acceptance_criteria": ["specialist selected"],
            "verification": {"method": "launcher_attestation"},
            "dry_run": True,
        }
    )
    assert '"success": true' in raw
    assert '"selected_agent": "noesis-architect"' in raw
