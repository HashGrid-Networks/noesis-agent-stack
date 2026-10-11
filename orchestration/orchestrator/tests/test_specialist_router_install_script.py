"""Install-script checks for the Noesis specialist router plugin."""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "install-noesis-specialist-router.sh"


def test_specialist_router_install_script_dry_run_targets_current_profile(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes"
    proc = subprocess.run(
        ["bash", str(SCRIPT), "--home", str(hermes_home), "--profile", "noesis-orchestrator", "--dry-run"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert "Mode        : DRY-RUN" in proc.stdout
    assert "Profile     : noesis-orchestrator" in proc.stdout
    assert "noesis-specialist-router" in proc.stdout
    assert "would install plugin" in proc.stdout
    assert not (hermes_home / "profiles" / "noesis-orchestrator" / "plugins").exists()


def test_specialist_router_install_script_installs_plugin_without_enabling_runtime(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes"
    target = hermes_home / "profiles" / "noesis-orchestrator"
    target.mkdir(parents=True)

    proc = subprocess.run(
        ["bash", str(SCRIPT), "--home", str(hermes_home), "--profile", "noesis-orchestrator", "--yes"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    installed = target / "plugins" / "noesis-specialist-router"
    assert (installed / "plugin.yaml").is_file()
    assert (installed / "__init__.py").is_file()
    audit = hermes_home / "logs" / "apply-audit.jsonl"
    assert audit.is_file()
    assert "install-noesis-specialist-router.sh" in audit.read_text(encoding="utf-8")
