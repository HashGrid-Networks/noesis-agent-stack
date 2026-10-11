"""Offline proof that an isolated empty fallback chain, not a runner flag or a pin, gates launch.

The transport is synthetic. It does not call a provider. A live Hermes child is not started.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.launcher import (
    FallbackDenied,
    HermesCliAdapter,
    authorize_inference_request,
    isolated_fallback_chain,
    write_isolated_fallback_session,
)


class _Completed:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "", pid: int = 4242) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.pid = pid


class SyntheticTransport:
    """Records the process boundary. Provider requests are counted only after authorization."""

    supports_fallback_deny = False

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.spawn_count = 0
        self.provider_requests: list[tuple[str, str]] = []

    def __call__(self, cmd, **kwargs):
        env = dict(kwargs.get("env") or {})
        self.calls.append({"cmd": list(cmd), "env": env})
        if "profile" in cmd and "show" in cmd:
            profile = cmd[cmd.index("-p") + 1]
            return _Completed(0, f"Profile: {profile}\nPath: /offline/profiles/{profile}\n")
        home = Path(env["HERMES_HOME"])
        profile = cmd[cmd.index("-p") + 1]
        config_text = (home / "profiles" / profile / "config.yaml").read_text(encoding="utf-8")
        chain = isolated_fallback_chain(config_text)
        provider = cmd[cmd.index("--provider") + 1]
        model = cmd[cmd.index("-m") + 1]
        authorize_inference_request(
            approved_provider=env["NOESIS_APPROVED_PROVIDER"],
            approved_model=env["NOESIS_APPROVED_MODEL"],
            requested_provider=provider,
            requested_model=model,
            fallback_chain=chain,
            attempts=len(self.provider_requests),
            blocked=bool(self.provider_requests),
        )
        self.spawn_count += 1
        self.provider_requests.append((provider, model))
        # Permit a second launch of the same attempt later for regressions.
        self._launch_attempt_opened = True
        return _Completed(0, json.dumps({"provider": provider, "model": model, "text": "verification pending"}))

    def close(self) -> None:
        """Placeholder: the adapter real path closes the budget; the synthetic
        runner returns a verification_pending LaunchResult and never runs a
        second launch for the same attempt."""

        return


def _launch(tmp_path: Path, runner: SyntheticTransport, **overrides):
    profiles = tmp_path / "profiles" / "noesis-forge"
    profiles.mkdir(parents=True)
    (profiles / "SOUL.md").write_text("name: noesis-forge\n", encoding="utf-8")
    adapter = HermesCliAdapter(
        hermes_bin="hermes",
        profiles_root=profiles.parent,
        process_runner=runner,
    )
    kwargs = dict(
        profile_id="noesis-forge",
        prompt="implement the repair",
        task_id="task-fallback",
        cwd=str(tmp_path),
        dry_run=False,
        provider="approved-provider",
        model="approved-model",
        timeout_s=30,
        remaining_lease_s=30,
        workspace=tmp_path / "workspace",
        fallback_policy="deny",
    )
    kwargs.update(overrides)
    (tmp_path / "workspace").mkdir(exist_ok=True)
    return adapter.launch(**kwargs)


def test_permitted_primary_starts_only_under_proven_empty_chain(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_PROVIDER", "ambient-provider")
    monkeypatch.setenv("HERMES_INFERENCE_MODEL", "ambient-model")
    runner = SyntheticTransport()
    result = _launch(tmp_path, runner)

    assert result.ok is True
    assert result.accepted is False
    assert runner.spawn_count == 1
    spawned = [call for call in runner.calls if "--provider" in call["cmd"]]
    assert spawned
    env = spawned[-1]["env"]
    assert env.get("HERMES_PROVIDER") is None
    assert env.get("HERMES_INFERENCE_MODEL") is None
    home = Path(env["HERMES_HOME"])
    assert "workspace" in home.parts
    config_path = home / "profiles" / "noesis-forge" / "config.yaml"
    text = config_path.read_text(encoding="utf-8")
    assert "fallback_providers: []" in text
    assert "fallback_model:" not in text
    assert "approved-provider" in text
    assert "approved-model" in text
    assert home != Path.home() / ".hermes"
    assert runner.provider_requests == [("approved-provider", "approved-model")]


def test_unauthorized_fallback_is_refused_before_a_provider_request(tmp_path):
    requests: list[tuple[str, str]] = []

    def send(provider: str, model: str, chain: list) -> None:
        authorize_inference_request(
            approved_provider="approved-provider",
            approved_model="approved-model",
            requested_provider=provider,
            requested_model=model,
            fallback_chain=chain,
        )
        requests.append((provider, model))

    with pytest.raises(FallbackDenied) as denied:
        send("other-provider", "other-model", [])
    assert denied.value.code == "unauthorized_fallback"
    assert requests == []

    with pytest.raises(FallbackDenied) as chained:
        send("approved-provider", "approved-model", [{"provider": "other-provider", "model": "other-model"}])
    assert chained.value.code == "fallback_unenforced"
    assert requests == []

    class SwitchingTransport(SyntheticTransport):
        def __call__(self, cmd, **kwargs):
            if "profile" not in cmd or "show" not in cmd:
                cmd = list(cmd)
                cmd[cmd.index("--provider") + 1] = "other-provider"
            return super().__call__(cmd, **kwargs)

    runner = SwitchingTransport()
    with pytest.raises(FallbackDenied):
        _launch(tmp_path, runner)
    assert runner.provider_requests == []


def test_billing_failure_does_not_cycle_providers():
    requests: list[tuple[str, str]] = []
    state = {"attempts": 0, "blocked": False}

    def send(provider: str, model: str) -> None:
        authorize_inference_request(
            approved_provider="approved-provider",
            approved_model="approved-model",
            requested_provider=provider,
            requested_model=model,
            fallback_chain=[],
            attempts=state["attempts"],
            blocked=state["blocked"],
        )
        requests.append((provider, model))
        state["attempts"] = 1
        state["blocked"] = True
        raise FallbackDenied("billing_or_quota", "synthetic billing failure")

    with pytest.raises(FallbackDenied) as first:
        send("approved-provider", "approved-model")
    assert first.value.code == "billing_or_quota"
    assert requests == [("approved-provider", "approved-model")]

    with pytest.raises(FallbackDenied) as second:
        send("other-provider", "other-model")
    assert second.value.code == "provider_attempt_exhausted"
    assert requests == [("approved-provider", "approved-model")]


def test_child_and_reviewer_sessions_keep_the_empty_chain(tmp_path):
    import sys

    sys.path.insert(0, "/Users/elvis/.hermes/hermes-agent")
    from hermes_cli.fallback_config import get_fallback_chain, scoped_fallback_chain

    inherited = [{"provider": "other-provider", "model": "other-model"}]
    for profile_id in ("noesis-forge", "noesis-sentinel"):
        root = write_isolated_fallback_session(
            tmp_path / profile_id,
            profile_id,
            "approved-provider",
            "approved-model",
        )
        text = (root / "profiles" / profile_id / "config.yaml").read_text(encoding="utf-8")
        assert isolated_fallback_chain(text) == []
        assert get_fallback_chain({"fallback_providers": [], "model": {"provider": "approved-provider", "default": "approved-model"}}) == []
        assert scoped_fallback_chain(inherited, [], pinned=False, owner=profile_id) is None
        assert "HERMES_PROVIDER" not in text
        assert root.is_relative_to(tmp_path)
