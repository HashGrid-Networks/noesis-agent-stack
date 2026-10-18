"""Final spawn boundary must bind the isolated home to source identity bytes.

``hermes profile show`` returning the requested name is not proof that the
child process loaded specialist instructions. ``write_isolated_fallback_session``
plants ``SOUL.md`` as ``name: <id>`` and ``launch`` then sets ``HERMES_HOME``
to that tree. These tests use a synthetic process runner and do not start Hermes.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.launcher import HermesCliAdapter
from app.specialist_dispatch_cli import main


class _Completed:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "", pid: int = 4242) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.pid = pid


class _Runner:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, cmd, **kwargs):
        env = dict(kwargs.get("env") or {})
        self.calls.append({"cmd": list(cmd), "env": env})
        if "profile" in cmd and "show" in cmd:
            profile = cmd[cmd.index("-p") + 1]
            return _Completed(0, f"Profile: {profile}\nPath: /offline/profiles/{profile}\n")
        return _Completed(0, "{\"text\": \"verification pending\"}")


SPECIALIST_SOUL = (
    "role: noesis-forge\n"
    "non-goals: do not orchestrate and do not inherit the parent identity\n"
)


def _launch(tmp_path: Path, runner: _Runner):
    profiles = tmp_path / "profiles" / "noesis-forge"
    profiles.mkdir(parents=True)
    (profiles / "SOUL.md").write_text(SPECIALIST_SOUL, encoding="utf-8")
    (profiles / "config.yaml").write_text(
        "model:\n  provider: kimi-coding\n  default: kimi-k2.7-code\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = HermesCliAdapter(
        hermes_bin="hermes",
        profiles_root=profiles.parent,
        process_runner=runner,
    )
    return adapter.launch(
        profile_id="noesis-forge",
        prompt="implement the bounded repair",
        task_id="task-identity",
        cwd=str(tmp_path),
        dry_run=False,
        provider="kimi-coding",
        model="kimi-k2.7-code",
        timeout_s=30,
        remaining_lease_s=30,
        workspace=workspace,
        fallback_policy="deny",
    )


def test_inference_spawn_carries_source_soul_not_name_only_placeholder(tmp_path: Path) -> None:
    runner = _Runner()
    result = _launch(tmp_path, runner)
    inference = [call for call in runner.calls if "-z" in call["cmd"]]
    assert inference, "identity binding must happen before a refused launch is the only outcome"
    home = Path(inference[-1]["env"]["HERMES_HOME"])
    isolated_soul = (home / "profiles" / "noesis-forge" / "SOUL.md").read_text(encoding="utf-8")
    assert isolated_soul == SPECIALIST_SOUL
    assert result.attestation is not None
    assert result.attestation.profile_path == str(home / "profiles" / "noesis-forge")
    assert result.attestation.loaded_profile == "noesis-forge"
    assert "name: noesis-forge\n" != isolated_soul


def test_cli_entrypoint_binds_source_soul_before_inference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The live CLI must reach HermesCliAdapter.launch and carry source identity."""
    profiles = tmp_path / "profiles"
    forge = profiles / "noesis-forge"
    forge.mkdir(parents=True)
    (forge / "SOUL.md").write_text(SPECIALIST_SOUL, encoding="utf-8")
    runner = _Runner()
    real_init = HermesCliAdapter.__init__

    def _init(self, *args, **kwargs):
        kwargs["profiles_root"] = profiles
        kwargs["process_runner"] = runner
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(HermesCliAdapter, "__init__", _init)
    monkeypatch.setenv("INFERENCE_ROUTING_MODE", "enforce")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    payload = {
        "title": "bounded implementation",
        "intent": "Implement a deterministic input validation function",
        "idempotency_key": "cli:spawn-identity",
        "acceptance_criteria": ["source soul bound before inference"],
        "verification": {"method": "manual"},
        "dry_run": False,
        "approved_provider": "KIMI_CODE",
        "approved_model": "kimi-k2.7-code",
        "risk_tier": "r0",
        "expected_epoch": 0,
        "workspace": str(workspace),
        "timeout_s": 30,
        "required_capability": "code_modify",
    }
    path = tmp_path / "task.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    code = main(
        [
            "--input",
            str(path),
            "--ledger",
            str(tmp_path / "ledger.jsonl"),
            "--events",
            str(tmp_path / "events.jsonl"),
        ]
    )
    captured = json.loads(capsys.readouterr().out)
    inference = [call for call in runner.calls if "-z" in call["cmd"]]
    assert inference, captured
    assert code == 0, captured
    home = Path(inference[-1]["env"]["HERMES_HOME"])
    isolated = home / "profiles" / "noesis-forge" / "SOUL.md"
    assert isolated.read_text(encoding="utf-8") == SPECIALIST_SOUL
