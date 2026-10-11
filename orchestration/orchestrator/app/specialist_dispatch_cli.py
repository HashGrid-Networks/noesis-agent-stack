"""Command-line entrypoint for auditable specialist dispatch.

This module gives live callers a concrete path that does not use Hermes' parent-
inheriting public subagent lifecycle directly. It accepts a bounded task JSON,
selects an eligible specialist via the control plane, launches through an
explicit runtime adapter, verifies requested-vs-loaded profile identity, and
prints a structured result.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from app.control_plane import Orchestrator
from app.launcher import LaunchAttestation, LaunchResult, RuntimeAdapter
from app.models import Verification
from app.specialist_routing import CapabilityIndex, RoutingBlocked, selection_rejections


class _DeclaredProfilesAdapter:
    """Test/sandbox adapter with explicit available profiles.

    The real CLI path uses ``HermesCliAdapter`` through ``Orchestrator``. This
    adapter is only selected when the input JSON includes ``available_profiles``;
    it lets sandbox and regression runs prove missing-runtime behavior without
    depending on the caller's installed Hermes profiles.
    """

    def __init__(self, available_profiles: list[str]) -> None:
        self.available = set(available_profiles)

    def profile_installed(self, profile_id: str) -> bool:
        return profile_id in self.available

    def launch(
        self,
        *,
        profile_id: str,
        prompt: str,
        task_id: str,
        cwd: str | None = None,
        dry_run: bool = True,
    ) -> LaunchResult:
        del prompt, cwd, dry_run
        del task_id
        if profile_id not in self.available:
            return LaunchResult(
                ok=False,
                requested_profile=profile_id,
                loaded_profile=None,
                command=["hermes", "-p", profile_id, "profile", "show", profile_id],
                attestation=None,
                error="profile_not_installed",
            )
        return LaunchResult(
            ok=True,
            requested_profile=profile_id,
            loaded_profile=profile_id,
            command=["hermes", "-p", profile_id, "profile", "show", profile_id],
            attestation=LaunchAttestation(
                requested_profile=profile_id,
                loaded_profile=profile_id,
                runtime="hermes-sandbox",
                profile_path=f"/sandbox/profiles/{profile_id}",
                manifest_sha256="sandbox",
            ),
        )


def _load_payload(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _verification(spec: dict[str, Any] | str | None) -> Verification:
    if isinstance(spec, str):
        return Verification(method=spec)
    if isinstance(spec, dict):
        return Verification(
            method=str(spec.get("method", "manual")),
            command=spec.get("command"),
            evidence_required=bool(spec.get("evidence_required", True)),
        )
    return Verification(method="manual")


def _make_orchestrator(payload: dict[str, Any], ledger: Path, events: Path | None) -> Orchestrator:
    adapter: RuntimeAdapter | None = None
    capability_index: CapabilityIndex | None = None
    if "available_profiles" in payload:
        adapter = _DeclaredProfilesAdapter([str(p) for p in payload.get("available_profiles") or []])
        capability_index = CapabilityIndex.from_repo(adapter=adapter)
    return Orchestrator(
        ledger_path=ledger,
        runtime_adapter=adapter,
        capability_index=capability_index,
        routing_event_log_path=events,
    )


def _success_payload(result) -> dict[str, Any]:
    launch = result.launch
    selected = result.selection.selected
    return {
        "success": True,
        "task_id": str(result.task.task_id),
        "task_class": result.selection.classification.task_class,
        "selected_agent": selected.profile_id if selected else None,
        "selection_reason": result.selection.reason,
        "requested_profile_id": launch.requested_profile if launch else None,
        "loaded_profile_id": launch.loaded_profile if launch else None,
        "resolved_launch_target": selected.runtime_profile if selected else None,
        "fallback_reason": result.fallback_reason,
        "model_provider": selected.model_provider if selected else None,
        "model_id": selected.model_id if selected else None,
        "launch_command": launch.command if launch else None,
        "attestation": asdict(launch.attestation) if launch and launch.attestation else None,
    }


def _blocked_payload(exc: RoutingBlocked) -> dict[str, Any]:
    return {
        "success": False,
        "code": exc.code,
        "reason": exc.reason,
        "selected_agent": "",
        "rejections": exc.rejections,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Route and launch a Noesis specialist task.")
    parser.add_argument("--input", required=True, type=Path, help="JSON task contract input")
    parser.add_argument("--ledger", required=True, type=Path, help="orchestrator JSONL ledger path")
    parser.add_argument("--events", type=Path, help="structured routing event JSONL path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = _load_payload(args.input)
    orch = _make_orchestrator(payload, args.ledger, args.events)
    try:
        result = orch.route_and_launch(
            title=str(payload["title"]),
            intent=str(payload["intent"]),
            acceptance_criteria=[str(item) for item in payload.get("acceptance_criteria", [])],
            verification=_verification(payload.get("verification")),
            idempotency_key=str(payload["idempotency_key"]),
            required_capability=payload.get("required_capability"),
            risk_tier=str(payload.get("risk_tier", "r0")),
            timeout_s=int(payload.get("timeout_s", 900)),
            max_attempts=int(payload.get("max_attempts", 1)),
            reviewer_profile=payload.get("reviewer_profile"),
            approval_required=payload.get("approval_required"),
            parent_task_id=payload.get("parent_task_id"),
            max_depth=int(payload.get("max_depth", 1)),
            allow_generic_fallback=bool(payload.get("allow_generic_fallback", False)),
            dry_run=bool(payload.get("dry_run", True)),
            inference_routing_context=payload.get("inference_routing_context"),
            approved_provider=payload.get("approved_provider"),
            approved_model=payload.get("approved_model"),
            workspace=payload.get("workspace"),
        )
    except RoutingBlocked as exc:
        print(json.dumps(_blocked_payload(exc), ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(_success_payload(result), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
