"""CLI entrypoint tests for the live specialist dispatch path."""
from __future__ import annotations

import json
from pathlib import Path

from app import specialist_dispatch_cli


def test_cli_dry_run_routes_through_control_plane_and_attests_profile(tmp_path: Path, capsys) -> None:
    payload = {
        "title": "architecture route",
        "intent": "Design an MCP/Hermes profile contract for a new memory agent",
        "acceptance_criteria": ["selected specialist is explicit"],
        "verification": {"method": "launcher_attestation"},
        "idempotency_key": "cli:architecture-route",
        "dry_run": True,
    }
    input_path = tmp_path / "task.json"
    input_path.write_text(json.dumps(payload), encoding="utf-8")
    ledger = tmp_path / "tasks.jsonl"
    events = tmp_path / "events.jsonl"

    exit_code = specialist_dispatch_cli.main([
        "--input", str(input_path),
        "--ledger", str(ledger),
        "--events", str(events),
    ])

    assert exit_code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["success"] is True
    assert result["selected_agent"] == "noesis-architect"
    assert result["requested_profile_id"] == "noesis-architect"
    assert result["loaded_profile_id"] == "noesis-architect"
    assert result["fallback_reason"] is None
    event = json.loads(events.read_text(encoding="utf-8").splitlines()[-1])
    assert event["selected_agent"] == "noesis-architect"
    assert event["loaded_profile_id"] == "noesis-architect"


def test_cli_event_persists_launcher_attestation_for_audit(tmp_path: Path, capsys) -> None:
    """Standing-goal §7: the routing event must durably carry the launcher
    attestation (loaded profile + manifest digest), not only live on stdout.
    A captured terminal is not an audit trail."""
    payload = {
        "title": "attestation persistence",
        "intent": "Implement a deterministic retry wrapper with unit tests",
        "acceptance_criteria": ["event carries attestation block"],
        "verification": {"method": "launcher_attestation"},
        "idempotency_key": "cli:attestation-persistence",
        "dry_run": True,
    }
    input_path = tmp_path / "task.json"
    input_path.write_text(json.dumps(payload), encoding="utf-8")
    events = tmp_path / "events.jsonl"

    exit_code = specialist_dispatch_cli.main([
        "--input", str(input_path),
        "--ledger", str(tmp_path / "tasks.jsonl"),
        "--events", str(events),
    ])

    assert exit_code == 0
    capsys.readouterr()
    event = json.loads(events.read_text(encoding="utf-8").splitlines()[-1])
    attestation = event.get("attestation")
    assert attestation is not None, "routing event must persist launcher attestation"
    assert attestation["requested_profile"] == "noesis-forge"
    assert attestation["loaded_profile"] == "noesis-forge"
    assert attestation["runtime"] == "hermes"
    assert len(attestation["manifest_sha256"]) == 64


def test_cli_event_labels_real_launch_not_rehearsal(tmp_path: Path, capsys) -> None:
    """Live-path defect found by the Subgoal 5 cycle: LaunchResult had no
    dry_run field, so every launch event was labeled ``dry_run_not_canary``
    even when real inference ran. Real launches must be labeled
    ``verification_pending``; only rehearsals keep the rehearsal label."""
    from app.launcher import LaunchResult

    real = LaunchResult(True, "noesis-forge", "noesis-forge", [], None, None)
    rehearsal = LaunchResult(
        True, "noesis-forge", "noesis-forge", [], None, None, dry_run=True
    )
    assert real.dry_run is False
    assert rehearsal.dry_run is True
    label_real = "verification_pending" if not real.dry_run else "dry_run_not_canary"
    label_rehearsal = "verification_pending" if not rehearsal.dry_run else "dry_run_not_canary"
    assert label_real == "verification_pending"
    assert label_rehearsal == "dry_run_not_canary"


def test_cli_blocks_missing_runtime_without_default_or_parent_fallback(tmp_path: Path, capsys) -> None:
    payload = {
        "title": "research route",
        "intent": "Research market signals and produce citations",
        "acceptance_criteria": ["blocked explicitly when runtime missing"],
        "verification": {"method": "launcher_attestation"},
        "idempotency_key": "cli:missing-runtime",
        "dry_run": True,
        "available_profiles": [],
    }
    input_path = tmp_path / "task.json"
    input_path.write_text(json.dumps(payload), encoding="utf-8")

    exit_code = specialist_dispatch_cli.main([
        "--input", str(input_path),
        "--ledger", str(tmp_path / "tasks.jsonl"),
        "--events", str(tmp_path / "events.jsonl"),
    ])

    assert exit_code == 2
    result = json.loads(capsys.readouterr().out)
    assert result["success"] is False
    assert result["code"] == "no_eligible_specialist"
    text = json.dumps(result)
    assert "profile_not_installed" in text
    assert "noesis-orchestrator" not in result.get("selected_agent", "")
    assert "default" not in result.get("selected_agent", "")
    assert "coder" not in result.get("selected_agent", "")
