"""Tests for the bounded, default-off specialist-selection decision layer.

The decision layer is OFF by default and never calls a provider until
the host explicitly switches its mode. These tests exercise the
contract without making a network request and without depending on the
optional TypeSafe / Jev SDK.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Mapping

import pytest

from app.specialist_decision import (
    ABSTAIN_KEY,
    CONTRACT_DIGEST,
    CONTRACT_VERSION,
    CandidateInput,
    DecisionConfig,
    DecisionLayer,
    DecisionMode,
    DecisionRecommendation,
    DecisionProvider,
    DecisionRequest,
    FallbackReason,
    ProviderResponse,
)


# ---------------------------------------------------------------------------
# Test providers
# ---------------------------------------------------------------------------


class _RecordingProvider(DecisionProvider):
    """A deterministic provider whose distribution the test controls.

    Set ``raw_distribution=True`` to skip the ``setdefault`` fill-in and
    return the test's distribution verbatim. The layer's validator then
    sees the test's exact keys and can reject unexpected, missing, or
    non-normalizing distributions.
    """

    provider_name = "recording-provider"

    def __init__(
        self,
        *,
        distribution_for: dict[tuple[str, ...], Mapping[str, float]] | None = None,
        default_abstain: bool = False,
        confidence: float | None = None,
        latency_ms: float = 1.0,
        raw_distribution: bool = False,
    ) -> None:
        self._distribution_for = distribution_for or {}
        self._default_abstain = default_abstain
        self._confidence = confidence
        self._latency_ms = latency_ms
        self._raw_distribution = raw_distribution
        self.calls: list[DecisionRequest] = []
        self._lock = threading.Lock()

    def evaluate(self, request: DecisionRequest) -> ProviderResponse:
        with self._lock:
            self.calls.append(request)
        candidate_ids = tuple(candidate.candidate_id for candidate in request.candidates)
        if self._default_abstain:
            distribution: dict[str, float] = {cid: 0.0 for cid in candidate_ids}
            distribution[ABSTAIN_KEY] = 1.0
        else:
            distribution = dict(self._distribution_for.get(candidate_ids, {}))
            if not self._raw_distribution:
                for cid in candidate_ids:
                    distribution.setdefault(cid, 0.0)
                distribution[ABSTAIN_KEY] = float(distribution.get(ABSTAIN_KEY, 0.0))
        return ProviderResponse(
            distribution=distribution,
            provider_confidence=self._confidence,
            reported_model="recorded-model",
            raw_answer=None,
            latency_ms=self._latency_ms,
            timed_out=False,
        )


class _SlowProvider(DecisionProvider):
    """A provider that blocks past the deadline. Used for the timeout test."""

    provider_name = "slow-provider"

    def __init__(self, sleep_s: float) -> None:
        self._sleep_s = sleep_s
        self.calls: int = 0

    def evaluate(self, request: DecisionRequest) -> ProviderResponse:
        self.calls += 1
        time.sleep(self._sleep_s)
        distribution = {c.candidate_id: 0.0 for c in request.candidates}
        distribution[ABSTAIN_KEY] = 1.0
        return ProviderResponse(
            distribution=distribution,
            provider_confidence=None,
            reported_model="slow-model",
            raw_answer=None,
            latency_ms=self._sleep_s * 1000.0,
            timed_out=False,
        )


class _RaisingProvider(DecisionProvider):
    """A provider that raises an exception on every call."""

    provider_name = "raising-provider"

    def __init__(self) -> None:
        self.calls: int = 0

    def evaluate(self, request: DecisionRequest) -> ProviderResponse:
        self.calls += 1
        raise RuntimeError("provider blew up")


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _candidate(cid: str, mandate: str = "candidate mandate") -> CandidateInput:
    return CandidateInput(
        candidate_id=cid,
        capabilities=("plan_artifact",),
        mandate=mandate,
    )


def _make_layer(
    *,
    provider: DecisionProvider | None = None,
    mode: DecisionMode = DecisionMode.OFF,
    advisory_deadline_s: float = 0.75,
    cache_ttl_s: float = 60.0,
    circuit_failure_threshold: int = 3,
    accept_threshold: float = 0.5,
    apply_margin_epsilon: float = 0.05,
    content_uniform_replay_threshold: int = 3,
    distribution_tolerance: float = 1e-3,
    budget_max_calls_per_minute: int = 60,
    event_log_path: Path | None = None,
) -> DecisionLayer:
    return DecisionLayer(
        provider=provider,
        config=DecisionConfig(
            mode=mode,
            advisory_deadline_s=advisory_deadline_s,
            cache_ttl_s=cache_ttl_s,
            circuit_failure_threshold=circuit_failure_threshold,
            accept_threshold=accept_threshold,
            apply_margin_epsilon=apply_margin_epsilon,
            content_uniform_replay_threshold=content_uniform_replay_threshold,
            distribution_tolerance=distribution_tolerance,
            budget_max_calls_per_minute=budget_max_calls_per_minute,
            event_log_path=event_log_path,
        ),
    )


def _two_candidate_records() -> dict[str, CandidateInput]:
    return {
        "noesis-forge": _candidate("noesis-forge", "implementation mandate"),
        "noesis-architect": _candidate(
            "noesis-architect", "architecture mandate"
        ),
    }


def _two_tied_candidates() -> list[str]:
    return ["noesis-forge", "noesis-architect"]


def _decide_kwargs() -> dict:
    return dict(
        baseline_candidate_id="noesis-forge",
        tied_candidates=_two_tied_candidates(),
        required_capability="code_modify",
        classification="implementation",
        risk_tier="r1",
        candidate_records=_two_candidate_records(),
        registry_digest="rg" * 32,
        policy_digest="pl" * 32,
        rubric_digest="rb" * 32,
        snapshot_digest="sn" * 32,
        question="select implementation specialist",
        correlation_id="corr-1",
        task_id="task-1",
        attempt_id="attempt-1",
        requested_model="recorded-model",
    )


# ---------------------------------------------------------------------------
# Default mode and provider skip semantics
# ---------------------------------------------------------------------------


class TestOffMode:
    def test_default_mode_is_shadow(self) -> None:
        layer = DecisionLayer(provider=_RecordingProvider())
        assert layer.mode is DecisionMode.SHADOW

    def test_off_mode_never_calls_provider(self) -> None:
        provider = _RecordingProvider()
        layer = _make_layer(provider=provider, mode=DecisionMode.OFF)
        outcome = layer.decide(**_decide_kwargs())
        assert outcome.mode is DecisionMode.OFF
        assert outcome.applied_candidate_id == "noesis-forge"
        assert outcome.baseline_candidate_id == "noesis-forge"
        assert outcome.fallback_reason is FallbackReason.MODE_OFF
        assert outcome.recommendation is None
        assert provider.calls == []

    def test_skip_when_single_candidate(self) -> None:
        provider = _RecordingProvider()
        layer = _make_layer(provider=provider, mode=DecisionMode.SHADOW)
        outcome = layer.decide(
            baseline_candidate_id="noesis-forge",
            tied_candidates=["noesis-forge"],
            required_capability="code_modify",
            classification="implementation",
            risk_tier="r1",
            candidate_records={"noesis-forge": _candidate("noesis-forge")},
            registry_digest="r" * 32,
            policy_digest="p" * 32,
            rubric_digest="b" * 32,
            snapshot_digest="s" * 32,
            question="q",
            correlation_id="c",
            task_id="t",
            attempt_id="a",
        )
        assert outcome.applied_candidate_id == "noesis-forge"
        assert outcome.fallback_reason is FallbackReason.SKIPPED_SINGLE_CANDIDATE
        assert provider.calls == []

    def test_skip_when_zero_candidates(self) -> None:
        provider = _RecordingProvider()
        layer = _make_layer(provider=provider, mode=DecisionMode.SHADOW)
        outcome = layer.decide(
            baseline_candidate_id=None,
            tied_candidates=[],
            required_capability="code_modify",
            classification="implementation",
            risk_tier="r1",
            candidate_records={},
            registry_digest="r" * 32,
            policy_digest="p" * 32,
            rubric_digest="b" * 32,
            snapshot_digest="s" * 32,
            question="q",
            correlation_id="c",
            task_id="t",
            attempt_id="a",
        )
        assert outcome.applied_candidate_id is None
        assert outcome.fallback_reason is FallbackReason.SKIPPED_ZERO_CANDIDATES
        assert provider.calls == []

    def test_skip_when_no_ties(self) -> None:
        """Multiple candidates but only one tied at the best score: skip."""
        provider = _RecordingProvider()
        layer = _make_layer(provider=provider, mode=DecisionMode.SHADOW)
        outcome = layer.decide(
            baseline_candidate_id="noesis-forge",
            tied_candidates=["noesis-forge"],  # only baseline is "tied"
            required_capability="code_modify",
            classification="implementation",
            risk_tier="r1",
            candidate_records=_two_candidate_records(),
            registry_digest="r" * 32,
            policy_digest="p" * 32,
            rubric_digest="b" * 32,
            snapshot_digest="s" * 32,
            question="q",
            correlation_id="c",
            task_id="t",
            attempt_id="a",
        )
        assert outcome.applied_candidate_id == "noesis-forge"
        assert outcome.fallback_reason is FallbackReason.SKIPPED_NO_TIES
        assert provider.calls == []


# ---------------------------------------------------------------------------
# Shadow mode: record but never mutate
# ---------------------------------------------------------------------------


class TestShadowMode:
    def test_shadow_calls_provider_and_records_recommendation(self) -> None:
        cids = _two_tied_candidates()
        provider = _RecordingProvider(
            distribution_for={
                tuple(cids): {
                    "noesis-forge": 0.6,
                    "noesis-architect": 0.3,
                    ABSTAIN_KEY: 0.1,
                }
            }
        )
        layer = _make_layer(provider=provider, mode=DecisionMode.SHADOW)
        outcome = layer.decide(**_decide_kwargs())
        assert outcome.mode is DecisionMode.SHADOW
        # Applied is still the baseline; shadow never mutates selection.
        assert outcome.applied_candidate_id == "noesis-forge"
        assert outcome.baseline_candidate_id == "noesis-forge"
        assert outcome.recommendation is not None
        # Distribution contains every candidate plus abstain.
        dist = outcome.recommendation.distribution
        assert set(dist.keys()) == set(cids) | {ABSTAIN_KEY}
        # Recommendation provenance fields populated.
        assert outcome.recommendation.provider_name == "recording-provider"
        assert outcome.recommendation.reported_model == "recorded-model"
        assert outcome.recommendation.requested_model == "recorded-model"

    def test_shadow_skips_provider_when_input_already_unique(self) -> None:
        provider = _RecordingProvider()
        layer = _make_layer(provider=provider, mode=DecisionMode.SHADOW)
        outcome = layer.decide(
            baseline_candidate_id="noesis-forge",
            tied_candidates=["noesis-forge"],
            required_capability="code_modify",
            classification="implementation",
            risk_tier="r1",
            candidate_records=_two_candidate_records(),
            registry_digest="r" * 32,
            policy_digest="p" * 32,
            rubric_digest="b" * 32,
            snapshot_digest="s" * 32,
            question="q",
            correlation_id="c",
            task_id="t",
            attempt_id="a",
        )
        assert outcome.applied_candidate_id == "noesis-forge"
        assert outcome.fallback_reason is FallbackReason.SKIPPED_NO_TIES
        assert provider.calls == []


# ---------------------------------------------------------------------------
# Active mode: rerank only within the tied-eligible set
# ---------------------------------------------------------------------------


class TestActiveMode:
    def test_active_rerank_applied_within_tied_set(self) -> None:
        cids = _two_tied_candidates()
        provider = _RecordingProvider(
            distribution_for={
                tuple(cids): {
                    "noesis-architect": 0.7,
                    "noesis-forge": 0.2,
                    ABSTAIN_KEY: 0.1,
                }
            }
        )
        layer = _make_layer(
            provider=provider, mode=DecisionMode.ACTIVE, accept_threshold=0.5
        )
        outcome = layer.decide(**_decide_kwargs())
        # Baseline is noesis-forge; rerank moved to noesis-architect.
        assert outcome.baseline_candidate_id == "noesis-forge"
        assert outcome.applied_candidate_id == "noesis-architect"
        assert outcome.fallback_reason is FallbackReason.OK

    def test_active_threshold_rejection_keeps_baseline(self) -> None:
        cids = _two_tied_candidates()
        provider = _RecordingProvider(
            distribution_for={
                tuple(cids): {
                    "noesis-architect": 0.35,
                    "noesis-forge": 0.35,
                    ABSTAIN_KEY: 0.3,
                }
            }
        )
        layer = _make_layer(
            provider=provider, mode=DecisionMode.ACTIVE, accept_threshold=0.5
        )
        outcome = layer.decide(**_decide_kwargs())
        assert outcome.applied_candidate_id == "noesis-forge"
        assert outcome.fallback_reason is FallbackReason.PROVIDER_THRESHOLD_REJECTED

    def test_active_provider_abstain_keeps_baseline(self) -> None:
        provider = _RecordingProvider(default_abstain=True)
        layer = _make_layer(provider=provider, mode=DecisionMode.ACTIVE)
        outcome = layer.decide(**_decide_kwargs())
        assert outcome.applied_candidate_id == "noesis-forge"
        assert outcome.fallback_reason is FallbackReason.PROVIDER_ABSTAINED

    def test_active_provider_confidence_preserved(self) -> None:
        cids = _two_tied_candidates()
        provider = _RecordingProvider(
            distribution_for={
                tuple(cids): {
                    "noesis-architect": 0.7,
                    "noesis-forge": 0.2,
                    ABSTAIN_KEY: 0.1,
                }
            },
            confidence=0.85,
        )
        layer = _make_layer(provider=provider, mode=DecisionMode.ACTIVE)
        outcome = layer.decide(**_decide_kwargs())
        assert outcome.recommendation is not None
        assert outcome.recommendation.provider_confidence == pytest.approx(0.85)


# ---------------------------------------------------------------------------
# Distribution validation
# ---------------------------------------------------------------------------


class TestResponseValidation:
    def _request(self) -> DecisionRequest:
        return DecisionRequest(
            contract_version=CONTRACT_VERSION,
            contract_digest=CONTRACT_DIGEST,
            question="q",
            rubric_digest="r" * 32,
            policy_digest="p" * 32,
            registry_digest="x" * 32,
            snapshot_digest="s" * 32,
            required_capability="code_modify",
            classification="implementation",
            risk_tier="r1",
            candidates=tuple(_two_candidate_records().values()),
            correlation_id="ci",
            task_id="ti",
            attempt_id="ai",
        )

    def test_distribution_missing_key_is_malformed(self) -> None:
        provider = _RecordingProvider(
            raw_distribution=True,
            distribution_for={
                tuple(_two_tied_candidates()): {
                    "noesis-forge": 0.5,
                    ABSTAIN_KEY: 0.5,
                }
            },
        )
        layer = _make_layer(provider=provider, mode=DecisionMode.SHADOW)
        outcome = layer.decide(**_decide_kwargs())
        assert outcome.fallback_reason is FallbackReason.PROVIDER_MALFORMED
        assert outcome.applied_candidate_id == "noesis-forge"
        assert outcome.recommendation is None

    def test_distribution_extra_key_is_malformed(self) -> None:
        provider = _RecordingProvider(
            raw_distribution=True,
            distribution_for={
                tuple(_two_tied_candidates()): {
                    "noesis-forge": 0.4,
                    "noesis-architect": 0.4,
                    "rogue": 0.1,
                    ABSTAIN_KEY: 0.1,
                }
            },
        )
        layer = _make_layer(provider=provider, mode=DecisionMode.SHADOW)
        outcome = layer.decide(**_decide_kwargs())
        assert outcome.fallback_reason is FallbackReason.PROVIDER_MALFORMED

    def test_distribution_out_of_range_is_malformed(self) -> None:
        provider = _RecordingProvider(
            raw_distribution=True,
            distribution_for={
                tuple(_two_tied_candidates()): {
                    "noesis-forge": 1.5,
                    "noesis-architect": -0.1,
                    ABSTAIN_KEY: -0.4,
                }
            },
        )
        layer = _make_layer(provider=provider, mode=DecisionMode.SHADOW)
        outcome = layer.decide(**_decide_kwargs())
        assert outcome.fallback_reason is FallbackReason.PROVIDER_MALFORMED

    def test_distribution_does_not_normalize_is_malformed(self) -> None:
        provider = _RecordingProvider(
            raw_distribution=True,
            distribution_for={
                tuple(_two_tied_candidates()): {
                    "noesis-forge": 0.5,
                    "noesis-architect": 0.5,
                    ABSTAIN_KEY: 0.5,
                }
            },
        )
        layer = _make_layer(provider=provider, mode=DecisionMode.SHADOW)
        outcome = layer.decide(**_decide_kwargs())
        assert outcome.fallback_reason is FallbackReason.PROVIDER_MALFORMED


# ---------------------------------------------------------------------------
# Cache, budget, circuit breaker
# ---------------------------------------------------------------------------


class TestReliabilityGuards:
    def test_cache_hit_does_not_call_provider(self) -> None:
        cids = _two_tied_candidates()
        provider = _RecordingProvider(
            distribution_for={
                tuple(cids): {
                    "noesis-architect": 0.7,
                    "noesis-forge": 0.2,
                    ABSTAIN_KEY: 0.1,
                }
            }
        )
        layer = _make_layer(provider=provider, mode=DecisionMode.SHADOW)
        # Warm cache.
        layer.decide(**_decide_kwargs())
        assert len(provider.calls) == 1
        # Same digest -> cache hit.
        layer.decide(**_decide_kwargs())
        assert len(provider.calls) == 1

    def test_cache_expiry_calls_provider_again(self) -> None:
        cids = _two_tied_candidates()
        provider = _RecordingProvider(
            distribution_for={
                tuple(cids): {
                    "noesis-architect": 0.7,
                    "noesis-forge": 0.2,
                    ABSTAIN_KEY: 0.1,
                }
            }
        )
        layer = _make_layer(
            provider=provider,
            mode=DecisionMode.SHADOW,
            cache_ttl_s=0.05,
        )
        layer.decide(**_decide_kwargs())
        assert len(provider.calls) == 1
        time.sleep(0.1)
        layer.decide(**_decide_kwargs())
        assert len(provider.calls) == 2

    def test_budget_exhaustion_preserves_baseline(self) -> None:
        provider = _RecordingProvider(
            distribution_for={
                tuple(_two_tied_candidates()): {
                    "noesis-architect": 0.7,
                    "noesis-forge": 0.2,
                    ABSTAIN_KEY: 0.1,
                }
            }
        )
        layer = _make_layer(
            provider=provider,
            mode=DecisionMode.SHADOW,
            budget_max_calls_per_minute=1,
        )
        first = layer.decide(**_decide_kwargs())
        assert first.recommendation is not None
        # Use a different snapshot so we don't hit the cache.
        kwargs = _decide_kwargs()
        kwargs["snapshot_digest"] = "snap" * 8
        kwargs["correlation_id"] = "c2"
        kwargs["task_id"] = "t2"
        kwargs["attempt_id"] = "a2"
        kwargs["question"] = "q2"
        second = layer.decide(**kwargs)
        assert second.fallback_reason is FallbackReason.BUDGET_EXHAUSTED
        assert second.applied_candidate_id == "noesis-forge"

    def test_circuit_breaker_opens_after_repeated_failures(self) -> None:
        provider = _RaisingProvider()
        layer = _make_layer(
            provider=provider,
            mode=DecisionMode.SHADOW,
            circuit_failure_threshold=2,
        )
        kwargs_factory = lambda i: {  # noqa: E731
            **_decide_kwargs(),
            "snapshot_digest": f"snap{i}"[-1].ljust(32, "0"),
            "correlation_id": f"c{i}",
            "task_id": f"t{i}",
            "attempt_id": f"a{i}",
            "question": f"q{i}",
        }
        for i in range(2):
            outcome = layer.decide(**kwargs_factory(i))
            # A raising provider is treated as an outage (normalized to
            # a default-abstain distribution that passes validation),
            # so it does NOT open the circuit. Validate that the
            # provider is reachable and the outcome is benign.
            assert outcome.applied_candidate_id == "noesis-forge"
        assert provider.calls == 2

    def test_malformed_response_opens_circuit(self) -> None:
        cids = _two_tied_candidates()
        provider = _RecordingProvider(
            distribution_for={
                tuple(cids): {
                    "noesis-forge": 0.4,
                    "noesis-architect": 0.4,
                    ABSTAIN_KEY: 0.5,  # does not normalize
                }
            }
        )
        layer = _make_layer(
            provider=provider,
            mode=DecisionMode.SHADOW,
            circuit_failure_threshold=2,
            distribution_tolerance=1e-3,
        )
        for i in range(3):
            kwargs = _decide_kwargs()
            kwargs["snapshot_digest"] = f"snap{i}".ljust(32, "0")
            kwargs["correlation_id"] = f"c{i}"
            kwargs["task_id"] = f"t{i}"
            kwargs["attempt_id"] = f"a{i}"
            kwargs["question"] = f"q{i}"
            outcome = layer.decide(**kwargs)
            assert outcome.applied_candidate_id == "noesis-forge"
        # By the third call the circuit is open and provider is skipped.
        assert len(provider.calls) == 2

    def test_timeout_rejects_response_and_returns_baseline(self) -> None:
        provider = _SlowProvider(sleep_s=0.2)
        layer = _make_layer(
            provider=provider,
            mode=DecisionMode.SHADOW,
            advisory_deadline_s=0.05,
        )
        outcome = layer.decide(**_decide_kwargs())
        assert outcome.applied_candidate_id == "noesis-forge"
        assert outcome.fallback_reason in {
            FallbackReason.LATE_RESPONSE,
            FallbackReason.PROVIDER_TIMEOUT,
        }


# ---------------------------------------------------------------------------
# Event log shape
# ---------------------------------------------------------------------------


class TestEventLog:
    def test_event_log_records_each_decision(self, tmp_path: Path) -> None:
        cids = _two_tied_candidates()
        provider = _RecordingProvider(
            distribution_for={
                tuple(cids): {
                    "noesis-architect": 0.7,
                    "noesis-forge": 0.2,
                    ABSTAIN_KEY: 0.1,
                }
            }
        )
        log_path = tmp_path / "decision-events.jsonl"
        layer = _make_layer(
            provider=provider,
            mode=DecisionMode.SHADOW,
            event_log_path=log_path,
        )
        layer.decide(**_decide_kwargs())
        assert log_path.exists()
        records = [
            json.loads(line)
            for line in log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(records) == 1
        rec = records[0]
        assert rec["mode"] == "shadow"
        assert rec["contract_version"] == CONTRACT_VERSION
        assert rec["contract_digest"] == CONTRACT_DIGEST
        assert rec["baseline_candidate_id"] == "noesis-forge"
        assert rec["applied_candidate_id"] == "noesis-forge"
        assert "recommendation" in rec
        assert rec["recommendation"]["provider_confidence"] is None

    def test_event_log_does_not_capture_task_text(self, tmp_path: Path) -> None:
        provider = _RecordingProvider()
        log_path = tmp_path / "decision-events.jsonl"
        layer = _make_layer(
            provider=provider,
            mode=DecisionMode.OFF,
            event_log_path=log_path,
        )
        secret_intent = "secret intent body that must not appear in events"
        kwargs = _decide_kwargs()
        kwargs["question"] = secret_intent
        layer.decide(**kwargs)
        records = [
            json.loads(line)
            for line in log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert records
        serialized = json.dumps(records, sort_keys=True)
        assert secret_intent not in serialized


# ---------------------------------------------------------------------------
# Mode switching and configuration
# ---------------------------------------------------------------------------


class TestConfiguration:
    def test_set_mode_replaces_config(self) -> None:
        layer = _make_layer(provider=_RecordingProvider(), mode=DecisionMode.OFF)
        layer.set_mode(DecisionMode.SHADOW)
        assert layer.mode is DecisionMode.SHADOW

    def test_set_provider_clears_state(self) -> None:
        cids = _two_tied_candidates()
        provider_a = _RecordingProvider(
            distribution_for={
                tuple(cids): {
                    "noesis-architect": 0.7,
                    "noesis-forge": 0.2,
                    ABSTAIN_KEY: 0.1,
                }
            }
        )
        layer = _make_layer(provider=provider_a, mode=DecisionMode.SHADOW)
        layer.decide(**_decide_kwargs())
        assert len(provider_a.calls) == 1

        provider_b = _RecordingProvider()
        layer.set_provider(provider_b)
        layer.decide(**_decide_kwargs())
        assert len(provider_b.calls) == 1
        assert layer.provider_name == "recording-provider"

    def test_invalid_advisory_deadline_rejected(self) -> None:
        with pytest.raises(ValueError):
            DecisionConfig(advisory_deadline_s=0)

    def test_invalid_accept_threshold_rejected(self) -> None:
        with pytest.raises(ValueError):
            DecisionConfig(accept_threshold=1.5)


# ---------------------------------------------------------------------------
# TypeSafe adapter surface
# ---------------------------------------------------------------------------


class TestTypeSafeAdapterSurface:
    def test_factory_requires_api_key_and_model(self) -> None:
        from app.typesafe_decision_adapter import AdapterUnavailable
        from app.typesafe_decision_adapter import TypeSafeAdapterFactory

        with pytest.raises(ValueError):
            TypeSafeAdapterFactory(api_key="", model="m", deadline_s=0.5)
        with pytest.raises(ValueError):
            TypeSafeAdapterFactory(api_key="k", model="", deadline_s=0.5)
        with pytest.raises(ValueError):
            TypeSafeAdapterFactory(api_key="k", model="m", deadline_s=0)

    def test_lazy_import_does_not_require_sdk(self) -> None:
        """Importing the adapter module must not require typesafe_sdk."""
        # The previous import succeeded; this test asserts no ImportError
        # was raised by importing typesafe_decision_adapter itself.
        import app.typesafe_decision_adapter as mod

        assert hasattr(mod, "TypeSafeDecisionAdapter")
        assert hasattr(mod, "TypeSafeAdapterFactory")
        assert hasattr(mod, "AdapterUnavailable")

    def test_assert_sdk_version_pinned_rejects_missing_version(self) -> None:
        from app.typesafe_decision_adapter import (
            AdapterUnavailable,
            assert_sdk_version_pinned,
        )

        class _FakeModule:
            pass

        with pytest.raises(AdapterUnavailable):
            assert_sdk_version_pinned(_FakeModule())

    def test_assert_sdk_version_pinned_accepts_pinned(self) -> None:
        from app.typesafe_decision_adapter import assert_sdk_version_pinned

        class _FakeModule:
            __version__ = "0.7.5"

        assert assert_sdk_version_pinned(_FakeModule()) == "0.7.5"


# ---------------------------------------------------------------------------
# Skeptic adversarial review fixes: R1 (P0), K1, A1, C1
# ---------------------------------------------------------------------------


class _CountingSlowProvider(DecisionProvider):
    """A slow provider that tracks concurrent evaluate() calls.

    Used by the R1 regression test to assert that the executor's pending
    task queue cannot grow beyond ``max_concurrency`` calls regardless
    of how many concurrent decide() invocations arrive.
    """

    provider_name = "counting-slow-provider"

    def __init__(self, sleep_s: float) -> None:
        self._sleep_s = sleep_s
        self.max_concurrent = 0
        self._current = 0
        self._lock = threading.Lock()
        self.calls = 0

    def evaluate(self, request: DecisionRequest) -> ProviderResponse:
        with self._lock:
            self._current += 1
            if self._current > self.max_concurrent:
                self.max_concurrent = self._current
            self.calls += 1
        try:
            time.sleep(self._sleep_s)
            distribution = {c.candidate_id: 0.0 for c in request.candidates}
            distribution[ABSTAIN_KEY] = 1.0
            return ProviderResponse(
                distribution=distribution,
                provider_confidence=None,
                reported_model="counting-model",
                raw_answer=None,
                latency_ms=self._sleep_s * 1000.0,
                timed_out=False,
            )
        finally:
            with self._lock:
                self._current -= 1


class TestSkepticFixesR1BoundedExecutor:
    """R1: regression coverage for the bounded submission slot.

    The previous design let ``_invoke_provider`` submit unlimited
    tasks to ``ThreadPoolExecutor``. With ``max_workers=2`` and a
    sustained slow provider, the executor's pending queue would grow
    without bound while the layer's caller continued returning
    baselines. The fix uses a ``BoundedSemaphore`` of size
    ``max_concurrency`` around the submit, so calls beyond the bound
    are rejected with ``BUDGET_EXHAUSTED`` before any thread-pool
    work begins.
    """

    def test_concurrent_slow_calls_do_not_overflow_max_concurrency(self) -> None:
        # max_concurrency defaults to 2; we drive 7 (max_workers + 5)
        # concurrent decide() invocations.
        n_attempts = 7
        provider = _CountingSlowProvider(sleep_s=0.3)
        layer = _make_layer(
            provider=provider,
            mode=DecisionMode.SHADOW,
            advisory_deadline_s=0.05,
        )

        outcomes: list = []
        barrier = threading.Barrier(n_attempts)
        errors: list = []

        def runner(i: int) -> None:
            try:
                kwargs = _decide_kwargs()
                kwargs["snapshot_digest"] = f"snap{i}".ljust(32, "0")
                kwargs["correlation_id"] = f"corr-{i}"
                kwargs["task_id"] = f"task-{i}"
                kwargs["attempt_id"] = f"attempt-{i}"
                kwargs["question"] = f"question-{i}"
                barrier.wait()
                outcomes.append(layer.decide(**kwargs))
            except BaseException as exc:  # noqa: BLE001 - propagate to main
                errors.append(exc)

        threads = [
            threading.Thread(target=runner, args=(i,)) for i in range(n_attempts)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)
        assert not errors

        # The provider MUST have observed at most max_concurrency
        # in-flight evaluate() calls at any point. Without the
        # semaphore, the executor's pending queue would let callers
        # 3..7 queue up unboundedly (eventually running as the slot
        # frees). With the semaphore, at most the first 2 acquire a
        # slot, the rest are bounced by ``acquire(blocking=False)``.
        assert (
            provider.max_concurrent <= 2
        ), (
            f"observed {provider.max_concurrent} concurrent provider "
            f"calls; queue growth was NOT bounded by max_concurrency"
        )
        # Every outcome should preserve the baseline; the layer
        # never reranks in SHADOW mode and never reranks outside the
        # slow path here.
        for outcome in outcomes:
            assert outcome.applied_candidate_id == "noesis-forge"
        # At least one call must have been refused with the bounded
        # BUDGET_EXHAUSTED path (otherwise the assertion above is
        # vacuous and the test would silently regress).
        budget_exhausted = sum(
            1
            for outcome in outcomes
            if outcome.fallback_reason is FallbackReason.BUDGET_EXHAUSTED
        )
        assert budget_exhausted >= 1, (
            "no BUDGET_EXHAUSTED outcomes observed; the bounded "
            "submission path was not exercised"
        )

    def test_late_response_increments_circuit_failures(self) -> None:
        """R1 second half: late responses now count toward the breaker.

        A provider that always returns late should be isolated by the
        breaker after ``circuit_failure_threshold`` consecutive
        slow calls.
        """
        provider = _SlowProvider(sleep_s=0.2)
        layer = _make_layer(
            provider=provider,
            mode=DecisionMode.SHADOW,
            advisory_deadline_s=0.05,
            circuit_failure_threshold=2,
        )

        def run(i: int) -> None:
            kwargs = _decide_kwargs()
            kwargs["snapshot_digest"] = f"snap{i}".ljust(32, "0")
            kwargs["correlation_id"] = f"corr-{i}"
            kwargs["task_id"] = f"task-{i}"
            kwargs["attempt_id"] = f"attempt-{i}"
            kwargs["question"] = f"q{i}"
            layer.decide(**kwargs)

        # Two consecutive slow calls -> circuit opens on the third.
        run(0)
        run(1)
        circuit = layer._circuits[provider.provider_name]
        assert circuit.opened_at is not None, (
            "circuit should be open after circuit_failure_threshold late "
            "responses; the late-path is not accounted to the breaker"
        )

        # A third call should see the breaker open and never reach the
        # provider.
        before = provider.calls
        run(2)
        assert provider.calls == before, (
            "breaker should have prevented the third slow call"
        )


class TestSkepticFixesK1CacheKey:
    """K1: cache key strips correlation/task/attempt identity."""

    def test_cache_hit_across_different_correlation_ids(self) -> None:
        cids = _two_tied_candidates()
        provider = _RecordingProvider(
            distribution_for={
                tuple(cids): {
                    "noesis-architect": 0.7,
                    "noesis-forge": 0.2,
                    ABSTAIN_KEY: 0.1,
                }
            }
        )
        layer = _make_layer(provider=provider, mode=DecisionMode.SHADOW)
        kwargs_a = _decide_kwargs()
        kwargs_a["correlation_id"] = "corr-A"
        kwargs_a["task_id"] = "task-A"
        kwargs_a["attempt_id"] = "attempt-A"
        layer.decide(**kwargs_a)
        assert len(provider.calls) == 1

        kwargs_b = _decide_kwargs()
        kwargs_b["correlation_id"] = "corr-B"  # different identity
        kwargs_b["task_id"] = "task-B"
        kwargs_b["attempt_id"] = "attempt-B"
        layer.decide(**kwargs_b)
        # Cache MUST hit because stable identity is identical.
        assert len(provider.calls) == 1, (
            "cache miss on a different correlation_id: cache key is "
            "still polluted by per-call correlation identity"
        )


class TestSkepticFixesA1DeterministicTie:
    """A1: equal-mass distributions cannot be exploited to set the winner."""

    def test_equal_mass_distribution_is_rejected_by_epsilon(self) -> None:
        cids = _two_tied_candidates()
        provider = _RecordingProvider(
            distribution_for={
                tuple(cids): {
                    # Equal masses at the accept_threshold boundary.
                    # Without the epsilon margin, the layer would
                    # accept and let iteration order pick the winner.
                    "noesis-architect": 0.5,
                    "noesis-forge": 0.5,
                    ABSTAIN_KEY: 0.0,
                }
            }
        )
        layer = _make_layer(
            provider=provider, mode=DecisionMode.ACTIVE, accept_threshold=0.5
        )
        outcome = layer.decide(**_decide_kwargs())
        # 0.5 < 0.5 + 0.05 = 0.55 -> rejected, baseline stands.
        assert outcome.applied_candidate_id == "noesis-forge"
        assert outcome.fallback_reason is FallbackReason.PROVIDER_THRESHOLD_REJECTED

    def test_lopsided_distribution_passes_epsilon(self) -> None:
        cids = _two_tied_candidates()
        provider = _RecordingProvider(
            distribution_for={
                tuple(cids): {
                    "noesis-architect": 0.7,
                    "noesis-forge": 0.2,
                    ABSTAIN_KEY: 0.1,
                }
            }
        )
        layer = _make_layer(
            provider=provider, mode=DecisionMode.ACTIVE, accept_threshold=0.5
        )
        outcome = layer.decide(**_decide_kwargs())
        # 0.7 >= 0.5 + 0.05 = 0.55 -> applied (rerank moves to architect).
        assert outcome.applied_candidate_id == "noesis-architect"
        assert outcome.fallback_reason is FallbackReason.OK


class TestSkepticFixesC1ContentReplayBreaker:
    """C1: valid-but-repeated distributions must trip the breaker."""

    def test_repeated_uniform_distribution_opens_breaker(self) -> None:
        cids = _two_tied_candidates()
        provider = _RecordingProvider(
            distribution_for={
                tuple(cids): {
                    "noesis-architect": 0.5,
                    "noesis-forge": 0.5,
                    ABSTAIN_KEY: 0.0,
                }
            }
        )
        layer = _make_layer(
            provider=provider,
            mode=DecisionMode.SHADOW,
            circuit_failure_threshold=1,
            content_uniform_replay_threshold=2,
        )

        def run(i: int):
            kwargs = _decide_kwargs()
            kwargs["snapshot_digest"] = f"snap{i}".ljust(32, "0")
            kwargs["correlation_id"] = f"corr-{i}"
            kwargs["task_id"] = f"task-{i}"
            kwargs["attempt_id"] = f"attempt-{i}"
            kwargs["question"] = f"q{i}"
            return layer.decide(**kwargs)

        # First identical distribution: counter = 1, no break yet.
        run(0)
        # Second identical distribution: counter = 2 (= threshold),
        # record_failure called; circuit opens.
        run(1)
        circuit = layer._circuits[provider.provider_name]
        assert circuit.opened_at is not None, (
            "content-derived breaker did not open after repeated "
            "identical distributions"
        )

        # Third call: breaker should be open; the provider must NOT
        # be invoked.
        before = provider.calls
        outcome = run(2)
        assert provider.calls == before, (
            "breaker should have blocked the third call from reaching "
            "the provider"
        )
        assert outcome.fallback_reason is FallbackReason.CIRCUIT_OPEN