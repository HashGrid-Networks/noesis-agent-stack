"""Policy lane names must map to Hermes CLI provider ids before launch.

Live canary finding (2026-10-18): the launcher received the policy lane name
``KIMI_CODE`` verbatim as ``hermes --provider`` and the CLI refused with
``Unknown provider 'kimi_code'``. The mapping from lane to provider id now
lives in ``platform/inference-routing.yaml`` (``hermes_provider``) and is
resolved by the control plane before ``_invoke_launch``.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "orchestration" / "orchestrator"))


def test_hermes_provider_for_lane_maps_kimi_code_from_real_policy() -> None:
    from app.inference_routing import EnforcerConfig, InferenceRoutingEnforcer

    enforcer = InferenceRoutingEnforcer(EnforcerConfig.from_env())
    assert enforcer.hermes_provider_for_lane("KIMI_CODE") == "kimi-coding"


def test_hermes_provider_for_lane_unknown_lane_returns_none() -> None:
    from app.inference_routing import EnforcerConfig, InferenceRoutingEnforcer

    enforcer = InferenceRoutingEnforcer(EnforcerConfig.from_env())
    assert enforcer.hermes_provider_for_lane("not-a-lane") is None
    assert enforcer.hermes_provider_for_lane(None) is None


def test_launch_maps_approved_lane_to_hermes_provider(tmp_path: Path) -> None:
    """The provider handed to ``_invoke_launch`` is the mapped provider id,
    not the policy lane name; non-lane ids pass through unchanged."""
    from app.control_plane import Orchestrator
    from app.launcher import LaunchResult
    from app.models import Verification
    from app.specialist_routing import CapabilityIndex

    class _FakeRuntimeAdapter:
        def profile_installed(self, profile_id: str) -> bool:
            return profile_id == "noesis-forge"

    class _LaneEnforcer:
        def hermes_provider_for_lane(self, lane: str | None) -> str | None:
            return "kimi-coding" if lane == "KIMI_CODE" else None

    adapter = _FakeRuntimeAdapter()
    orch = Orchestrator(
        tmp_path / "tasks.jsonl",
        capability_index=CapabilityIndex.from_repo(adapter=adapter),
        runtime_adapter=adapter,
    )
    orch.inference_enforcer = _LaneEnforcer()  # type: ignore[assignment]

    captured: dict = {}

    def _capture(**kwargs):
        captured.update(kwargs)
        return LaunchResult(
            ok=True,
            requested_profile=kwargs["profile_id"],
            loaded_profile=kwargs["profile_id"],
            command=["hermes", "-p", kwargs["profile_id"], "-z", "prompt"],
            attestation=None,
            provider=kwargs.get("provider"),
            model=kwargs.get("model"),
        )

    orch._invoke_launch = _capture  # type: ignore[method-assign]

    result = orch.route_and_launch(
        title="lane mapping",
        intent="Implement a deterministic input-validation function with tests",
        acceptance_criteria=["approved lane mapped before launch"],
        verification=Verification(method="synthetic"),
        idempotency_key="lane-mapping-kimi-code",
        required_capability="code_modify",
        dry_run=False,
        approved_provider="KIMI_CODE",
        approved_model="kimi-k2.7-code",
        workspace=str(tmp_path),
    )

    assert result.launch is not None
    assert captured["provider"] == "kimi-coding", captured
    assert captured["model"] == "kimi-k2.7-code"


def test_launch_persists_bounded_captured_stdout_for_accept_audit(tmp_path: Path) -> None:
    """The ACCEPT-phase audit trail must carry the child's handoff text, not
    only its digest (canary 2026-10-18 finding)."""
    import json

    from app.control_plane import Orchestrator, _bounded_stdout_evidence
    from app.launcher import LaunchResult
    from app.models import Verification
    from app.specialist_routing import CapabilityIndex

    class _FakeRuntimeAdapter:
        def profile_installed(self, profile_id: str) -> bool:
            return profile_id == "noesis-forge"

    adapter = _FakeRuntimeAdapter()
    ledger = tmp_path / "tasks.jsonl"
    orch = Orchestrator(
        ledger,
        capability_index=CapabilityIndex.from_repo(adapter=adapter),
        runtime_adapter=adapter,
    )

    def _capture(**kwargs):
        return LaunchResult(
            ok=True,
            requested_profile=kwargs["profile_id"],
            loaded_profile=kwargs["profile_id"],
            command=["hermes", "-p", kwargs["profile_id"], "-z", "prompt"],
            attestation=None,
            provider=kwargs.get("provider"),
            model=kwargs.get("model"),
            stdout_digest="digest-1",
            captured_stdout='{"handoff": "implemented validator", "detail": "x" * 10}',
        )

    orch._invoke_launch = _capture  # type: ignore[method-assign]

    orch.route_and_launch(
        title="stdout evidence",
        intent="Implement a deterministic input-validation function with tests",
        acceptance_criteria=["captured stdout persisted"],
        verification=Verification(method="synthetic"),
        idempotency_key="stdout-evidence",
        required_capability="code_modify",
        dry_run=False,
        approved_provider="anthropic",
        approved_model="claude-sonnet-5",
        workspace=str(tmp_path),
    )

    persisted = [
        json.loads(line)
        for line in ledger.read_text(encoding="utf-8").splitlines()
        if json.loads(line).get("event") == "launch_handle_persisted"
    ]
    assert persisted, "launch_handle_persisted record missing"
    reservation = persisted[-1]["task"]["delegation"]["launch_reservation"]
    assert reservation["stdout_digest"] == "digest-1"
    assert '"handoff": "implemented validator"' in (reservation["captured_stdout"] or "")


def test_bounded_stdout_evidence_caps_and_marks_truncation() -> None:
    from app.control_plane import _bounded_stdout_evidence

    assert _bounded_stdout_evidence(None) is None
    assert _bounded_stdout_evidence("") is None
    short = "handoff"
    assert _bounded_stdout_evidence(short) == short
    long = "x" * 20_000
    capped = _bounded_stdout_evidence(long)
    assert capped is not None
    assert len(capped) < 20_000
    assert "[truncated" in capped
    assert capped.endswith("chars]")


def test_launch_passes_through_non_lane_provider_ids(tmp_path: Path) -> None:
    """Provider ids that are not policy lanes keep their raw value."""
    from app.control_plane import Orchestrator
    from app.launcher import LaunchResult
    from app.models import Verification
    from app.specialist_routing import CapabilityIndex

    class _FakeRuntimeAdapter:
        def profile_installed(self, profile_id: str) -> bool:
            return profile_id == "noesis-forge"

    class _LaneEnforcer:
        def hermes_provider_for_lane(self, lane: str | None) -> str | None:
            return "kimi-coding" if lane == "KIMI_CODE" else None

    adapter = _FakeRuntimeAdapter()
    orch = Orchestrator(
        tmp_path / "tasks.jsonl",
        capability_index=CapabilityIndex.from_repo(adapter=adapter),
        runtime_adapter=adapter,
    )
    orch.inference_enforcer = _LaneEnforcer()  # type: ignore[assignment]

    captured: dict = {}

    def _capture(**kwargs):
        captured.update(kwargs)
        return LaunchResult(
            ok=True,
            requested_profile=kwargs["profile_id"],
            loaded_profile=kwargs["profile_id"],
            command=["hermes", "-p", kwargs["profile_id"], "-z", "prompt"],
            attestation=None,
            provider=kwargs.get("provider"),
            model=kwargs.get("model"),
        )

    orch._invoke_launch = _capture  # type: ignore[method-assign]

    orch.route_and_launch(
        title="passthrough",
        intent="Implement a deterministic input-validation function with tests",
        acceptance_criteria=["raw provider id preserved"],
        verification=Verification(method="synthetic"),
        idempotency_key="lane-mapping-passthrough",
        required_capability="code_modify",
        dry_run=False,
        approved_provider="anthropic",
        approved_model="claude-sonnet-5",
        workspace=str(tmp_path),
    )

    assert captured["provider"] == "anthropic", captured
