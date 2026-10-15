"""Synthetic ACP-like fixture for offline transport contract tests.

This is deliberately not a protocol implementation. It has no subprocess,
network, credential, model, filesystem-write, or tool-execution capability.
The pinned message vocabulary documents assumptions that remain unverified
against the installed Hermes runtime.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any


PINNED_PROTOCOL_ASSUMPTIONS = {
    "protocol": "Agent Client Protocol",
    "fixture_revision": "noesis-fake-acp/v1",
    "messages": (
        "initialize",
        "session/new",
        "session/prompt",
        "session/update",
        "session/result",
        "session/cancel",
        "session/cancelled",
        "permission/request",
    ),
    "installed_hermes_verified": False,
    "unverified": (
        "wire framing and exact method names",
        "installed Hermes outbound-client support",
        "continuation semantics",
        "permission and cancellation acknowledgement semantics",
    ),
}


@dataclass
class FakeAcpFixture:
    """Deterministic in-memory peer that emits a configured message script."""

    script: list[dict[str, Any]]
    capabilities: tuple[str, ...] = ("session/new", "session/prompt", "session/cancel")
    exit_early: bool = False
    time_out: bool = False
    initialized: bool = False
    sessions: dict[str, str] = field(default_factory=dict)
    prompt_count: int = 0
    cancel_requests: list[str] = field(default_factory=list)

    def initialize(self, requested: Iterable[str]) -> dict[str, Any]:
        self.initialized = True
        requested_set = set(requested)
        return {
            "method": "initialize",
            "fixture_revision": PINNED_PROTOCOL_ASSUMPTIONS["fixture_revision"],
            "capabilities": [item for item in self.capabilities if item in requested_set],
        }

    def new_session(self, correlation_id: str) -> dict[str, Any]:
        if not self.initialized:
            raise RuntimeError("fixture_not_initialized")
        session_id = f"fake-session-{len(self.sessions) + 1}"
        self.sessions[session_id] = correlation_id
        return {"method": "session/new", "session_id": session_id, "correlation_id": correlation_id}

    def prompt(self, session_id: str, text: str) -> list[dict[str, Any]]:
        if session_id not in self.sessions:
            raise RuntimeError("unknown_session")
        if not isinstance(text, str) or not text:
            raise ValueError("prompt_required")
        self.prompt_count += 1
        if self.time_out:
            raise TimeoutError("synthetic_timeout")
        if self.exit_early:
            return []
        return [dict(message) for message in self.script]

    def cancel(self, session_id: str) -> dict[str, Any]:
        self.cancel_requests.append(session_id)
        return {"method": "session/cancelled", "session_id": session_id, "acknowledged": True}
