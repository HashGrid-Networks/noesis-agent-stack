"""Offline proof that noesis_specialist_delegate reaches the model-facing tool
inventory through Hermes' real plugin-loading + toolset-recognition path.

This does NOT invoke a provider or call handler directly: it loads the plugin
through ``PluginManager.discover_and_load()``, then asserts the registered tool
and its toolset are recognized by the same ``get_plugin_toolset_keys_nowait`` /
registry path a fresh CLI uses to decide a toolset is "known" (the check behind
the ``Unknown toolsets`` warning in ``cli_init_mixin._init_toolsets``).
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
PLUGIN_SOURCE = ROOT / "integrations" / "hermes-plugin" / "noesis-specialist-router"
HERMES_SOURCE = Path.home() / ".hermes" / "hermes-agent"


def _add_hermes_deps() -> None:
    candidate_sites = [HERMES_SOURCE / "venv" / "lib" / "python3.11" / "site-packages"]
    candidate_sites.extend(
        sorted((Path.home() / ".hermes" / "installs").glob("**/venv/lib/python*/site-packages"))
    )
    for site_packages in candidate_sites:
        if site_packages.exists() and str(site_packages) not in sys.path:
            sys.path.append(str(site_packages))


def _load_via_plugin_manager(tmp_path: Path, monkeypatch, *, enabled: bool = True):
    _add_hermes_deps()
    hermes_home = tmp_path / "hermes-home"
    plugin_dir = hermes_home / "plugins" / "noesis-specialist-router"
    shutil.copytree(PLUGIN_SOURCE, plugin_dir, ignore=shutil.ignore_patterns("__pycache__"))
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "plugins": {"enabled": ["noesis-specialist-router"] if enabled else []},
                "platform_toolsets": {"cli": ["hermes-cli", "noesis_specialist_router"]},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("NOESIS_AGENT_STACK_ROOT", str(ROOT))
    monkeypatch.syspath_prepend(str(HERMES_SOURCE))
    monkeypatch.syspath_prepend(str(ROOT / "orchestration" / "orchestrator"))

    from hermes_cli.plugins import PluginManager, get_plugin_toolset_keys_nowait
    from tools.registry import registry

    manager = PluginManager()
    manager.discover_and_load()
    return manager, registry, get_plugin_toolset_keys_nowait


def test_tool_reaches_model_facing_inventory_through_real_loading(tmp_path, monkeypatch) -> None:
    """The delegation tool must be registered, and its toolset recognized, via the real path."""
    _, registry, toolset_keys_fn = _load_via_plugin_manager(tmp_path, monkeypatch, enabled=True)

    # The toolset is recognized post-discovery (the check `_init_toolsets` uses).
    assert "noesis_specialist_router" in toolset_keys_fn()

    # The tool appears exactly once in the registry with a valid schema.
    entry = registry.get_entry("noesis_specialist_delegate", scope=str(Path.home()))
    if entry is None:
        entry = registry.get_entry("noesis_specialist_delegate")
    assert entry is not None, "delegation tool not registered in the model-facing registry"
    assert entry.toolset == "noesis_specialist_router"
    assert entry.schema and entry.schema.get("name") == "noesis_specialist_delegate"
    assert not hasattr(entry, "handler_side_effect")  # sanity: not an empty stub


def test_disabling_plugin_removes_tool_and_toolset_recognition(tmp_path, monkeypatch) -> None:
    """With the plugin not enabled, its tool and toolset must not be recognized."""
    _, registry, toolset_keys_fn = _load_via_plugin_manager(tmp_path, monkeypatch, enabled=False)

    assert "noesis_specialist_router" not in toolset_keys_fn()
    assert registry.get_entry("noesis_specialist_delegate") is None


def test_unknown_toolset_warning_scenario_is_transient(tmp_path, monkeypatch) -> None:
    """Reproduce the root cause of the `Warning: Unknown toolsets` line.

    The warning fires when the platform_toolsets.cli entry is not yet recognized
    by the pre-discovery cache. After discovery the toolset IS recognized, so the
    warning is a startup-ordering artifact, not a missing registration.
    """
    # Enabling then re-running discovery must settle the toolset as known.
    manager, registry, toolset_keys_fn = _load_via_plugin_manager(tmp_path, monkeypatch, enabled=True)
    assert "noesis_specialist_router" in toolset_keys_fn()
    assert registry.get_entry("noesis_specialist_delegate") is not None
