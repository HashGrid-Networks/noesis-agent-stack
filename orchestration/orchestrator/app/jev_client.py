"""Jev advisory client + deterministic baseline.

The real client speaks to TypeSafe's ``POST /v1/systemone``; the mock is
what the bounded slice actually uses. Both implement the same
``JevClient`` protocol so a single import swap turns the slice on or off
without touching the retrieval pipeline.

Authority: this client is *advisory only*. It never authorizes, accepts,
spends, or mutates authoritative state. The retrieval pipeline enforces
hard limits and falls back to the deterministic baseline on any
malformed/timeout/outage signal.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol


class JevClientError(RuntimeError):
    """Base error for the Jev client family."""


class JevTimeoutError(JevClientError):
    """Jev call exceeded the enforced latency budget."""


class JevMalformedError(JevClientError):
    """Jev returned output that did not validate against the typed schema."""


@dataclass(frozen=True)
class JevDecision:
    """A single typed-decision result from Jev.

    All fields are advisory. ``probabilities`` and ``confidence`` are model
    signals, not proof of correctness or permission.
    """

    question: str
    choice: str | None
    score: float | None
    boolean: bool | None
    probabilities: dict[str, float] = field(default_factory=dict)
    confidence: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class JevResponse:
    decisions: tuple[JevDecision, ...]
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int


class JevClient(Protocol):
    """Protocol every Jev client (real or mock) implements."""

    def call(
        self,
        *,
        state: str,
        questions: dict[str, dict[str, Any]],
        timeout_s: float,
        max_input_tokens: int,
    ) -> JevResponse: ...


# ---------------------------------------------------------------------------
# Real client (not invoked in the bounded slice)
# ---------------------------------------------------------------------------


class HttpJevClient:
    """Thin HTTP wrapper for ``POST /v1/systemone``.

    Activated only when ``TYPESAFE_API_KEY`` is set. The bounded slice never
    instantiates this class; tests import :class:`MockJevClient` instead.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str = "jev-latest",
    ) -> None:
        self.api_key = api_key or os.environ.get("TYPESAFE_API_KEY")
        if not self.api_key:
            raise JevClientError("TYPESAFE_API_KEY not set")
        self.base_url = (base_url or os.environ.get("TYPESAFE_API_BASE")
                         or "https://api.typesafe.ai").rstrip("/")
        self.model = model

    def call(
        self,
        *,
        state: str,
        questions: dict[str, dict[str, Any]],
        timeout_s: float,
        max_input_tokens: int,
    ) -> JevResponse:
        body = json.dumps({
            "state": state,
            "model": self.model,
            "questions": questions,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/v1/systemone",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError) as exc:
            raise JevTimeoutError(str(exc)) from exc
        latency_ms = int((time.monotonic() - started) * 1000)
        return _parse_response(payload, self.model, latency_ms)


# ---------------------------------------------------------------------------
# Mock client (what the bounded slice and tests use)
# ---------------------------------------------------------------------------


class MockJevClient:
    """Offline mock that scripts responses per ``scripted_responses`` queue.

    Each call pops the next scripted response (a dict) and returns it parsed
    as a :class:`JevResponse`. When the queue is empty the default response
    is used. The mock also records every call so tests can assert on
    questions asked, state shipped, and budgets enforced.
    """

    def __init__(self, default_response: dict[str, Any] | None = None) -> None:
        self.default_response = default_response or {
            "model": "mock-jev-0",
            "answers": {
                "route": {
                    "type": "choice",
                    "choice": "none",
                    "probabilities": {"none": 1.0},
                    "confidence": 0.5,
                }
            },
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
        self.scripted_responses: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []

    def script(self, response: dict[str, Any]) -> None:
        self.scripted_responses.append(response)

    def call(
        self,
        *,
        state: str,
        questions: dict[str, dict[str, Any]],
        timeout_s: float,
        max_input_tokens: int,
    ) -> JevResponse:
        self.calls.append({
            "state": state,
            "questions": questions,
            "timeout_s": timeout_s,
            "max_input_tokens": max_input_tokens,
        })
        payload = (self.scripted_responses.pop(0)
                   if self.scripted_responses else self.default_response)
        if "error" in payload:
            kind = payload["error"]
            if kind == "timeout":
                raise JevTimeoutError("simulated timeout")
            if kind == "malformed":
                raise JevMalformedError("simulated malformed output")
            raise JevClientError(f"simulated error: {kind}")
        return _parse_response(payload, payload.get("model", "mock-jev-0"),
                               latency_ms=0)


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _parse_response(payload: dict[str, Any], model: str,
                    latency_ms: int) -> JevResponse:
    if not isinstance(payload, dict):
        raise JevMalformedError("response is not a JSON object")
    answers = payload.get("answers")
    if not isinstance(answers, dict):
        raise JevMalformedError("'answers' missing or not an object")
    decisions: list[JevDecision] = []
    for name, ans in answers.items():
        if not isinstance(ans, dict):
            raise JevMalformedError(f"answer '{name}' is not an object")
        decisions.append(JevDecision(
            question=name,
            choice=ans.get("choice") if ans.get("type") == "choice" else None,
            score=ans.get("score") if ans.get("type") == "score" else None,
            boolean=ans.get("probability") if ans.get("type") == "boolean" else None,
            probabilities=dict(ans.get("probabilities") or {}),
            confidence=ans.get("confidence"),
            raw=ans,
        ))
    usage = payload.get("usage") or {}
    return JevResponse(
        decisions=tuple(decisions),
        model=str(payload.get("model", model)),
        input_tokens=int(usage.get("input_tokens", 0) or 0),
        output_tokens=int(usage.get("output_tokens", 0) or 0),
        latency_ms=latency_ms,
    )
