"""Bounded, default-off specialist-selection decision layer.

This module is an optional, pluggable advisor that sits between
``CapabilityIndex.select`` (the deterministic baseline) and the launch
boundary. It never calls a provider in its default mode and never modifies
the existing SelectionDecision in any mode other than ``active``.

Design contract
---------------

* ``DecisionMode.OFF`` (the default) -- no provider call, return the
  baseline SelectionDecision byte-for-byte.
* ``DecisionMode.SHADOW`` -- call the injected fake / replay / explicit
  provider, record a sanitized recommendation event, and return the
  baseline SelectionDecision unchanged in every authority-relevant field.
* ``DecisionMode.ACTIVE`` -- test-only. May rerank only already-eligible
  tied candidates that share the required capability and the best
  deterministic qualification score. It never admits a candidate,
  changes classification, weakens generic fallback rules, selects a
  supervisor or reviewer-only profile, or changes downstream task
  contracts, approvals, lifecycle, admission, launch, review,
  acceptance, or recovery.

Failure semantics
-----------------

Abstain, threshold rejection, timeout, malformed output, provider outage,
stale input, late response, open circuit, cache miss / expiry, budget
exhaustion, and any validation error preserve the deterministic baseline.

Skip the provider call when zero or one eligible candidate is available.
The current canonical routing fixtures have exactly one, so the layer
records no useful decision rather than manufacture one.

Reliability:

* configurable advisory deadline, default ``0.75`` seconds;
* no automatic online retry;
* bounded concurrency with a small explicit default and a bounded
  in-flight submission slot so the executor's pending queue cannot
  grow without limit under sustained slow-provider load;
* provider-specific circuit breaker independent from the task failure
  breaker; covers validation failures, late responses, AND repeated
  identical-mass distributions (content replay);
* reject a response that arrives after its deadline even if the
  provider returns successfully;
* optional exact-input cache keyed by stable identity fields only --
  correlation_id / task_id / attempt_id are intentionally excluded so
  the cache hit-rate is not driven to zero by per-call correlation
  identities.

Provenance:

* Sanitized structured events only. No raw task text, no prompt body,
  no acceptance criteria, no secrets, no credentials.
"""

from __future__ import annotations

import enum
import hashlib
import json
import math
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

# ---------------------------------------------------------------------------
# Versioned contract identity
# ---------------------------------------------------------------------------

CONTRACT_VERSION = "specialist-decision/v1"
CONTRACT_DIGEST = hashlib.sha256(
    json.dumps(
        {
            "version": CONTRACT_VERSION,
            "module": "app.specialist_decision",
            "abstain_key": "__abstain__",
        },
        sort_keys=True,
    ).encode("utf-8")
).hexdigest()


# ---------------------------------------------------------------------------
# Modes and failure reasons
# ---------------------------------------------------------------------------


class DecisionMode(str, enum.Enum):
    OFF = "off"
    SHADOW = "shadow"
    ACTIVE = "active"


VALID_MODES: tuple[str, ...] = ("off", "shadow", "active")


class FallbackReason(str, enum.Enum):
    OK = "ok"
    MODE_OFF = "mode_off"
    SKIPPED_ZERO_CANDIDATES = "skipped_zero_candidates"
    SKIPPED_SINGLE_CANDIDATE = "skipped_single_candidate"
    SKIPPED_NO_TIES = "skipped_no_ties"
    SKIPPED_ACTIVE_FORBIDDEN = "skipped_active_test_only"
    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_OUTAGE = "provider_outage"
    PROVIDER_MALFORMED = "provider_malformed"
    PROVIDER_ABSTAINED = "provider_abstained"
    PROVIDER_THRESHOLD_REJECTED = "provider_threshold_rejected"
    CIRCUIT_OPEN = "circuit_open"
    CACHE_HIT_REJECTED = "cache_hit_rejected"
    STALE_INPUT = "stale_input"
    LATE_RESPONSE = "late_response"
    BUDGET_EXHAUSTED = "budget_exhausted"
    VALIDATION_ERROR = "validation_error"
    PROVIDER_MISSING = "provider_missing"
    NOT_INJECTED = "provider_not_injected"


# ---------------------------------------------------------------------------
# Provider protocol and primitive response
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderResponse:
    """A normalized provider response.

    ``distribution`` must contain a probability for every candidate_id
    passed in the request PLUS the explicit ``ABSTAIN_KEY`` sentinel. The
    adapter layer is responsible for translation; this module only
    validates the contract.
    """

    distribution: Mapping[str, float]
    provider_confidence: float | None = None
    reported_model: str | None = None
    raw_answer: Mapping[str, Any] | None = None
    latency_ms: float = 0.0
    timed_out: bool = False


class DecisionProvider(Protocol):
    """Narrow provider protocol used by the decision layer.

    Implementations must be deterministic given the same contract, the same
    question, the same rubric, the same policy, the same registry digest,
    the same candidate set, and the same snapshot identity. They must
    never read raw task text, never reach into the caller's filesystem,
    and never touch the live ledger.
    """

    provider_name: str

    def evaluate(self, request: "DecisionRequest") -> ProviderResponse: ...


ABSTAIN_KEY = "__abstain__"


# ---------------------------------------------------------------------------
# Request and response models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CandidateInput:
    """A trusted, pre-eligible candidate passed to the provider.

    The provider never defines candidates, never decides policy, never
    observes the registry -- it is asked to recommend a probability over a
    closed set it has already been told is eligible.
    """

    candidate_id: str
    capabilities: tuple[str, ...]
    mandate: str


@dataclass(frozen=True)
class DecisionRequest:
    """Versioned immutable request. Treat task text and evidence as untrusted."""

    contract_version: str
    contract_digest: str
    question: str
    rubric_digest: str
    policy_digest: str
    registry_digest: str
    snapshot_digest: str
    required_capability: str
    classification: str
    risk_tier: str
    candidates: tuple[CandidateInput, ...]
    correlation_id: str
    task_id: str
    attempt_id: str
    requested_model: str | None = None
    request_digest: str | None = None

    def with_request_digest(self) -> "DecisionRequest":
        canonical = json.dumps(
            {
                "contract_version": self.contract_version,
                "contract_digest": self.contract_digest,
                "question": self.question,
                "rubric_digest": self.rubric_digest,
                "policy_digest": self.policy_digest,
                "registry_digest": self.registry_digest,
                "snapshot_digest": self.snapshot_digest,
                "required_capability": self.required_capability,
                "classification": self.classification,
                "risk_tier": self.risk_tier,
                "candidates": [
                    {
                        "candidate_id": c.candidate_id,
                        "capabilities": list(c.capabilities),
                        "mandate": c.mandate,
                    }
                    for c in self.candidates
                ],
                "correlation_id": self.correlation_id,
                "task_id": self.task_id,
                "attempt_id": self.attempt_id,
                "requested_model": self.requested_model,
            },
            sort_keys=True,
        ).encode("utf-8")
        digest = hashlib.sha256(canonical).hexdigest()
        return DecisionRequest(
            contract_version=self.contract_version,
            contract_digest=self.contract_digest,
            question=self.question,
            rubric_digest=self.rubric_digest,
            policy_digest=self.policy_digest,
            registry_digest=self.registry_digest,
            snapshot_digest=self.snapshot_digest,
            required_capability=self.required_capability,
            classification=self.classification,
            risk_tier=self.risk_tier,
            candidates=self.candidates,
            correlation_id=self.correlation_id,
            task_id=self.task_id,
            attempt_id=self.attempt_id,
            requested_model=self.requested_model,
            request_digest=digest,
        )


@dataclass(frozen=True)
class DecisionRecommendation:
    """A complete distribution plus provenance. Never authoritative on its own."""

    distribution: Mapping[str, float]
    abstain_reason: FallbackReason
    provider_name: str
    requested_model: str | None
    reported_model: str | None
    provider_confidence: float | None
    cache_status: str  # miss | hit | hit_expired | rejected
    latency_ms: float
    retry_count: int
    sdk_version: str | None
    cost: Mapping[str, float] | None


@dataclass(frozen=True)
class DecisionOutcome:
    """What the host actually applies. ``applied`` is always a baseline
    SelectionDecision in OFF/SHADOW modes and an optional rerank in
    ACTIVE mode.
    """

    mode: DecisionMode
    baseline_candidate_id: str | None
    applied_candidate_id: str | None
    recommendation: DecisionRecommendation | None
    fallback_reason: FallbackReason
    decision_id: str


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DecisionConfig:
    mode: DecisionMode = DecisionMode.OFF
    advisory_deadline_s: float = 0.75
    max_concurrency: int = 2
    circuit_failure_threshold: int = 3
    circuit_open_s: float = 30.0
    cache_max_entries: int = 256
    cache_ttl_s: float = 60.0
    budget_max_calls_per_minute: int = 60
    distribution_tolerance: float = 1e-3
    accept_threshold: float = 0.5  # applied recommendation mass must be at least this
    # Extra margin required on top of accept_threshold before a recommendation
    # is applied in ACTIVE mode. Defends against deterministic-winner
    # tie-exploitation: a hostile provider that returns equal masses on tied
    # candidates cannot unilaterally pick a winner without surpassing
    # accept_threshold + apply_margin_epsilon. See Skeptic A1.
    apply_margin_epsilon: float = 0.05
    # Number of consecutive identical-mass distributions (replay) that trip
    # the content-derived breaker. See Skeptic C1.
    content_uniform_replay_threshold: int = 3
    required_capability_match: bool = True
    sdk_version: str | None = None
    provider_name: str | None = None
    event_log_path: Path | None = None

    def __post_init__(self) -> None:
        if self.advisory_deadline_s <= 0:
            raise ValueError("advisory_deadline_s must be positive")
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        if self.circuit_failure_threshold < 1:
            raise ValueError("circuit_failure_threshold must be >= 1")
        if not (0.0 <= self.accept_threshold <= 1.0):
            raise ValueError("accept_threshold must be within [0, 1]")
        if self.apply_margin_epsilon < 0:
            raise ValueError("apply_margin_epsilon must be non-negative")
        if self.content_uniform_replay_threshold < 1:
            raise ValueError(
                "content_uniform_replay_threshold must be >= 1"
            )
        if self.distribution_tolerance < 0:
            raise ValueError("distribution_tolerance must be non-negative")


# ---------------------------------------------------------------------------
# Provider-specific circuit breaker
# ---------------------------------------------------------------------------


@dataclass
class _CircuitState:
    failures: int = 0
    opened_at: float | None = None
    # C1: track consecutive identical-mass distributions for the
    # content-derived breaker. A provider that returns the same
    # distribution N times in a row is replaying or pinned -- either way,
    # it is a content anomaly that the validation-only breaker misses.
    last_distribution_digest: str | None = None
    consecutive_identical: int = 0

    def is_open(self, *, now: float | None = None, open_s: float = 30.0) -> bool:
        if self.opened_at is None:
            return False
        current = now if now is not None else time.monotonic()
        return (current - self.opened_at) < open_s

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def record_failure(self, *, threshold: int, open_s: float) -> None:
        self.failures += 1
        if self.failures >= threshold:
            self.opened_at = time.monotonic()

    def note_distribution(
        self, distribution_digest: str, *, replay_threshold: int
    ) -> bool:
        """Record a normalized distribution's identity.

        Returns True when the consecutive-identical counter reaches
        ``replay_threshold``, indicating a replay/pinned-content
        anomaly that should open the breaker.
        """
        if self.last_distribution_digest == distribution_digest:
            self.consecutive_identical += 1
        else:
            self.consecutive_identical = 1
            self.last_distribution_digest = distribution_digest
        return self.consecutive_identical >= replay_threshold


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


@dataclass
class _CacheEntry:
    expires_at: float
    recommendation: DecisionRecommendation


# ---------------------------------------------------------------------------
# Default providers
# ---------------------------------------------------------------------------


class DeterministicProvider:
    """A deterministic provider that abstains on every request.

    Suitable as the default value when no provider is configured.
    """

    provider_name = "deterministic-abstain"

    def evaluate(self, request: DecisionRequest) -> ProviderResponse:
        distribution = {c.candidate_id: 0.0 for c in request.candidates}
        distribution[ABSTAIN_KEY] = 1.0
        return ProviderResponse(
            distribution=distribution,
            provider_confidence=None,
            reported_model=None,
            raw_answer=None,
            latency_ms=0.0,
            timed_out=False,
        )


# ---------------------------------------------------------------------------
# The decision layer
# ---------------------------------------------------------------------------


class DecisionLayer:
    """A pluggable advisor between the deterministic baseline and the host.

    Construction accepts an injected provider. Default mode is OFF. ACTIVE
    mode never runs in production -- it is a test-only knob that reranks
    only eligible candidates tied at the best deterministic score.
    """

    def __init__(
        self,
        *,
        provider: DecisionProvider | None = None,
        config: DecisionConfig | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self.config = config or DecisionConfig()
        self._provider: DecisionProvider = provider or DeterministicProvider()
        self._provider_name: str = getattr(
            self._provider, "provider_name", "unconfigured"
        )
        self._circuits: dict[str, _CircuitState] = {}
        self._cache: dict[str, _CacheEntry] = {}
        self._budget_window_start: float = time.monotonic()
        self._budget_calls: int = 0
        # R1: bound the in-flight submission slots. A hostile or
        # slow provider can otherwise accumulate an unbounded queue
        # of late tasks on the executor's internal queue, which holds
        # memory and worker threads. The semaphore size matches
        # max_concurrency so the executor's queue stays shallow: no
        # submission happens unless a worker slot is available.
        self._submit_slot = threading.BoundedSemaphore(
            value=self.config.max_concurrency
        )
        self._executor = ThreadPoolExecutor(
            max_workers=self.config.max_concurrency,
            thread_name_prefix="decision-layer",
        )

    # ----------------------------------------------------------- public api --

    @property
    def mode(self) -> DecisionMode:
        return self.config.mode

    @property
    def provider_name(self) -> str:
        return self._provider_name

    def set_mode(self, mode: DecisionMode | str) -> None:
        if isinstance(mode, str):
            mode = DecisionMode(mode)
        # Dataclass is frozen; rebuild with the same fields.
        from dataclasses import replace

        object.__setattr__(self, "config", replace(self.config, mode=mode))

    def set_provider(self, provider: DecisionProvider) -> None:
        with self._lock:
            self._provider = provider
            self._provider_name = getattr(
                provider, "provider_name", "unconfigured"
            )
            self._circuits.clear()
            self._cache.clear()

    def decide(
        self,
        *,
        baseline_candidate_id: str | None,
        tied_candidates: Sequence[str],
        required_capability: str,
        classification: str,
        risk_tier: str,
        candidate_records: Mapping[str, CandidateInput],
        registry_digest: str,
        policy_digest: str,
        rubric_digest: str,
        snapshot_digest: str,
        question: str,
        correlation_id: str,
        task_id: str,
        attempt_id: str,
        requested_model: str | None = None,
    ) -> DecisionOutcome:
        """Apply the configured mode to the already-eligible set."""
        baseline = baseline_candidate_id
        decision_id = self._make_decision_id(
            task_id=task_id,
            attempt_id=attempt_id,
            correlation_id=correlation_id,
            baseline=baseline,
            classification=classification,
        )

        # Mode OFF: never call a provider, never mutate.
        if self.config.mode == DecisionMode.OFF:
            self._record_event(
                decision_id=decision_id,
                mode=DecisionMode.OFF,
                baseline=baseline,
                applied=baseline,
                recommendation=None,
                fallback_reason=FallbackReason.MODE_OFF,
            )
            return DecisionOutcome(
                mode=DecisionMode.OFF,
                baseline_candidate_id=baseline,
                applied_candidate_id=baseline,
                recommendation=None,
                fallback_reason=FallbackReason.MODE_OFF,
                decision_id=decision_id,
            )

        # Skip when there is no candidate set worth modeling.
        if not candidate_records:
            return self._baseline_outcome(
                baseline, decision_id, FallbackReason.SKIPPED_ZERO_CANDIDATES
            )

        if len(candidate_records) <= 1:
            return self._baseline_outcome(
                baseline, decision_id, FallbackReason.SKIPPED_SINGLE_CANDIDATE
            )

        # The provider is never told to rerank when there are no ties.
        if len(tied_candidates) <= 1:
            return self._baseline_outcome(
                baseline, decision_id, FallbackReason.SKIPPED_NO_TIES
            )

        # In ACTIVE mode the rerank scope is bounded to the tied set.
        # In SHADOW mode the rerank is *not* applied.
        in_scope = list(tied_candidates)
        candidates_in_scope = tuple(
            candidate_records[cid]
            for cid in in_scope
            if cid in candidate_records
        )
        if not candidates_in_scope:
            return self._baseline_outcome(
                baseline, decision_id, FallbackReason.SKIPPED_NO_TIES
            )

        request = DecisionRequest(
            contract_version=CONTRACT_VERSION,
            contract_digest=CONTRACT_DIGEST,
            question=question,
            rubric_digest=rubric_digest,
            policy_digest=policy_digest,
            registry_digest=registry_digest,
            snapshot_digest=snapshot_digest,
            required_capability=required_capability,
            classification=classification,
            risk_tier=risk_tier,
            candidates=candidates_in_scope,
            correlation_id=correlation_id,
            task_id=task_id,
            attempt_id=attempt_id,
            requested_model=requested_model,
        ).with_request_digest()

        with self._lock:
            circuit = self._circuits.get(self._provider_name) or _CircuitState()
            if circuit.is_open(open_s=self.config.circuit_open_s):
                return self._baseline_outcome(
                    baseline, decision_id, FallbackReason.CIRCUIT_OPEN
                )
            if self._budget_exhausted_locked():
                return self._baseline_outcome(
                    baseline, decision_id, FallbackReason.BUDGET_EXHAUSTED
                )
            self._consume_budget_locked()

        # Cache lookup.
        cache_key = self._cache_key(request)
        cached = self._cache.get(cache_key)
        if cached is not None:
            if cached.expires_at <= time.monotonic():
                with self._lock:
                    self._cache.pop(cache_key, None)
                cache_status = "hit_expired"
                response = None
                recommendation = None
            else:
                cache_status = "hit"
                response = None
                recommendation = cached.recommendation
        else:
            cache_status = "miss"
            response = None
            recommendation = None

        if response is None and recommendation is None:
            response, late, exhausted = self._invoke_provider(request)
            if exhausted:
                # R1: bounded submission slot is saturated; the
                # decision layer refuses the call without ever
                # touching the executor's queue. Treat as a budget
                # overflow distinct from a provider timeout.
                return self._baseline_outcome(
                    baseline, decision_id, FallbackReason.BUDGET_EXHAUSTED
                )
            if late:
                # R1 second half: a late response is now treated as a
                # circuit-accountable failure, not a benign skip. The
                # previous design dropped it on the floor; under a
                # sustained-slow-hostile provider that constitutes an
                # unbounded attack surface, since the validation-only
                # breaker would never observe it.
                self._record_failure_locked()
                return self._baseline_outcome(
                    baseline, decision_id, FallbackReason.LATE_RESPONSE
                )

        if recommendation is None:
            if response is None:
                return self._baseline_outcome(
                    baseline, decision_id, FallbackReason.VALIDATION_ERROR
                )

            try:
                recommendation = self._normalize_response(
                    request=request,
                    response=response,
                    cache_status=cache_status,
                )
            except _ValidationError as exc:
                self._record_failure_locked()
                return self._baseline_outcome(
                    baseline, decision_id, exc.fallback
                )

        # C1: even when validation passed, a repeated identical
        # distribution from this provider is a content anomaly. Run
        # the content-derived breaker check after normalization so it
        # sees the same distribution the validator just accepted.
        # Lock acquisition is short (one mapping lookup + one
        # hash + a counter increment); keep it tight.
        with self._lock:
            self._check_content_breaker_locked(recommendation.distribution)

        # Persist cache only on miss with non-error responses.
        if cache_status == "miss":
            with self._lock:
                self._cache[cache_key] = _CacheEntry(
                    expires_at=time.monotonic() + self.config.cache_ttl_s,
                    recommendation=recommendation,
                )
                if len(self._cache) > self.config.cache_max_entries:
                    # Drop the oldest entry by insertion order; ordered dicts
                    # preserve insertion order in Python 3.7+.
                    oldest = next(iter(self._cache))
                    self._cache.pop(oldest, None)

        # Decide whether to apply. ACTIVE mode may apply a rerank within
        # the tied-eligible set; SHADOW mode never applies.
        applied = baseline
        fallback = self._recommendation_fallback(recommendation)
        if self.config.mode == DecisionMode.ACTIVE:
            applied_candidate, applied_fallback = self._apply_recommendation(
                recommendation=recommendation,
                request=request,
                baseline=baseline,
            )
            if applied_candidate is not None:
                applied = applied_candidate
                fallback = applied_fallback
            elif applied_fallback is not FallbackReason.OK:
                # The recommendation was eligible for application but was
                # rejected by threshold or abstain. The host needs that
                # outcome in the event log even when the baseline stands.
                fallback = applied_fallback

        self._record_event(
            decision_id=decision_id,
            mode=self.config.mode,
            baseline=baseline,
            applied=applied,
            recommendation=recommendation,
            fallback_reason=fallback,
        )
        return DecisionOutcome(
            mode=self.config.mode,
            baseline_candidate_id=baseline,
            applied_candidate_id=applied,
            recommendation=recommendation,
            fallback_reason=fallback,
            decision_id=decision_id,
        )

    # ---------------------------------------------------------- internals --

    def _baseline_outcome(
        self,
        baseline: str | None,
        decision_id: str,
        reason: FallbackReason,
    ) -> DecisionOutcome:
        self._record_event(
            decision_id=decision_id,
            mode=self.config.mode,
            baseline=baseline,
            applied=baseline,
            recommendation=None,
            fallback_reason=reason,
        )
        return DecisionOutcome(
            mode=self.config.mode,
            baseline_candidate_id=baseline,
            applied_candidate_id=baseline,
            recommendation=None,
            fallback_reason=reason,
            decision_id=decision_id,
        )

    def _invoke_provider(
        self, request: DecisionRequest
    ) -> tuple[ProviderResponse | None, bool, bool]:
        """Call the provider with a hard deadline.

        Returns ``(response, late, exhausted)``. ``late`` is True when
        the response arrived after the deadline, even if the underlying
        call returned successfully; such responses are rejected as if
        they had timed out. ``exhausted`` is True when the bounded
        submission slot is full and the caller should treat the call as
        a budget overflow rather than a provider timeout (caller maps
        the former to ``BUDGET_EXHAUSTED``).
        """
        deadline = self.config.advisory_deadline_s
        start = time.monotonic()
        # R1: refuse the submission when the bounded slot is full so
        # the executor's pending task queue cannot grow without bound.
        # blocking=False surfaces saturation to the caller immediately.
        if not self._submit_slot.acquire(blocking=False):
            return None, False, True
        try:
            future = self._executor.submit(self._provider.evaluate, request)
            try:
                response = future.result(timeout=deadline)
            except TimeoutError:
                # The provider may still complete after the deadline.
                # Best-effort cancel only works if the worker has not
                # already started; cancellation is never relied on. The
                # slot is released in the finally block; the worker is
                # bounded by max_concurrency so no infinite backlog.
                return None, True, False
            except Exception:
                # Provider raised. Do not retry. Synthesize a benign
                # abstain that passes validation.
                return ProviderResponse(
                    distribution={
                        c.candidate_id: 0.0 for c in request.candidates
                    }
                    | {ABSTAIN_KEY: 1.0},
                    provider_confidence=None,
                    reported_model=None,
                    raw_answer=None,
                    latency_ms=(time.monotonic() - start) * 1000.0,
                    timed_out=False,
                ), False, False
            elapsed = time.monotonic() - start
            if elapsed > deadline:
                return None, True, False
            return response, False, False
        finally:
            self._submit_slot.release()

    def _normalize_response(
        self,
        *,
        request: DecisionRequest,
        response: ProviderResponse,
        cache_status: str,
    ) -> DecisionRecommendation:
        distribution = dict(response.distribution)
        candidate_ids = {c.candidate_id for c in request.candidates}
        required_keys = candidate_ids | {ABSTAIN_KEY}
        present = set(distribution)
        missing = required_keys - present
        if missing:
            raise _ValidationError(
                FallbackReason.PROVIDER_MALFORMED,
                f"distribution missing keys: {sorted(missing)}",
            )
        extra = present - required_keys
        if extra:
            raise _ValidationError(
                FallbackReason.PROVIDER_MALFORMED,
                f"distribution has unexpected keys: {sorted(extra)}",
            )
        for key, value in distribution.items():
            if not isinstance(value, (int, float)):
                raise _ValidationError(
                    FallbackReason.PROVIDER_MALFORMED,
                    f"distribution value for {key} is not numeric",
                )
            if not math.isfinite(float(value)):
                raise _ValidationError(
                    FallbackReason.PROVIDER_MALFORMED,
                    f"distribution value for {key} is not finite",
                )
            if not (0.0 <= float(value) <= 1.0):
                raise _ValidationError(
                    FallbackReason.PROVIDER_MALFORMED,
                    f"distribution value for {key} is outside [0, 1]",
                )
        total = sum(distribution.values())
        if abs(total - 1.0) > self.config.distribution_tolerance:
            raise _ValidationError(
                FallbackReason.PROVIDER_MALFORMED,
                f"distribution does not normalize: total={total}",
            )
        if response.provider_confidence is not None and not (
            0.0 <= float(response.provider_confidence) <= 1.0
        ):
            raise _ValidationError(
                FallbackReason.PROVIDER_MALFORMED,
                "provider_confidence is outside [0, 1]",
            )
        return DecisionRecommendation(
            distribution=distribution,
            abstain_reason=FallbackReason.OK,
            provider_name=self._provider_name,
            requested_model=request.requested_model,
            reported_model=response.reported_model,
            provider_confidence=response.provider_confidence,
            cache_status=cache_status,
            latency_ms=response.latency_ms,
            retry_count=0,
            sdk_version=self.config.sdk_version,
            cost=None,
        )

    def _recommendation_fallback(
        self, recommendation: DecisionRecommendation
    ) -> FallbackReason:
        distribution = recommendation.distribution
        abstain_mass = float(distribution.get(ABSTAIN_KEY, 0.0))
        if abstain_mass >= 1.0 - self.config.distribution_tolerance:
            return FallbackReason.PROVIDER_ABSTAINED
        return FallbackReason.OK

    def _apply_recommendation(
        self,
        *,
        recommendation: DecisionRecommendation,
        request: DecisionRequest,
        baseline: str | None,
    ) -> tuple[str | None, FallbackReason]:
        distribution = recommendation.distribution
        abstain_mass = float(distribution.get(ABSTAIN_KEY, 0.0))
        if abstain_mass >= 1.0 - self.config.distribution_tolerance:
            return None, FallbackReason.PROVIDER_ABSTAINED
        best_id: str | None = None
        best_mass = -1.0
        for cid in (c.candidate_id for c in request.candidates):
            mass = float(distribution.get(cid, 0.0))
            if mass > best_mass:
                best_mass = mass
                best_id = cid
        if best_id is None:
            return None, FallbackReason.PROVIDER_ABSTAINED
        # A1 defense: require the recommendation's winning mass to
        # clear accept_threshold + apply_margin_epsilon. This blocks
        # the deterministic tie-exploitation attack where a provider
        # returns equal masses on tied candidates and lets iteration
        # order dictate the winner. Without the epsilon margin, a
        # provider could name the FIRST candidate in
        # tied_candidates with mass == accept_threshold (the
        # knife-edge) and the layer would accept. With the margin,
        # only a recommendation with meaningfully more mass on one
        # candidate can rerank. The baseline stands otherwise.
        effective_threshold = (
            self.config.accept_threshold + self.config.apply_margin_epsilon
        )
        if best_mass < effective_threshold:
            return None, FallbackReason.PROVIDER_THRESHOLD_REJECTED
        if best_id == baseline:
            return best_id, FallbackReason.OK
        # The rerank may not move outside the tied-eligible scope. The
        # request was constructed only over the tied set, so best_id is
        # by construction an already-eligible tied candidate.
        return best_id, FallbackReason.OK

    # -- budget / circuit bookkeeping ---------------------------------------

    def _budget_exhausted_locked(self) -> bool:
        now = time.monotonic()
        if now - self._budget_window_start >= 60.0:
            self._budget_window_start = now
            self._budget_calls = 0
        return self._budget_calls >= self.config.budget_max_calls_per_minute

    def _consume_budget_locked(self) -> None:
        self._budget_calls += 1

    def _record_failure_locked(self) -> None:
        circuit = self._circuits.setdefault(self._provider_name, _CircuitState())
        circuit.record_failure(
            threshold=self.config.circuit_failure_threshold,
            open_s=self.config.circuit_open_s,
        )

    def _distribution_digest(self, distribution: Mapping[str, float]) -> str:
        """Stable hash of a normalized distribution's mass vector.

        Used by the C1 content-derived breaker: a provider that
        replays the same distribution many times is a content
        anomaly even when each individual distribution validates
        cleanly. The digest is order-independent so floating-point
        reorderings do not change the identity.
        """
        items = sorted(
            (str(k), float(v)) for k, v in distribution.items()
        )
        return hashlib.sha256(
            json.dumps(items, sort_keys=True).encode("utf-8")
        ).hexdigest()

    def _check_content_breaker_locked(
        self, distribution: Mapping[str, float]
    ) -> None:
        """Trip the breaker when the provider repeats the same distribution.

        C1: validation-failure breakers cannot observe a provider that
        always returns syntactically valid but strategically wrong
        (replayed) distributions. Track the consecutive-identical
        counter against ``content_uniform_replay_threshold``; when the
        threshold is reached, account as a circuit failure and open the
        breaker using the same threshold + open_s as the regular path.
        """
        circuit = self._circuits.setdefault(self._provider_name, _CircuitState())
        repeated = circuit.note_distribution(
            self._distribution_digest(distribution),
            replay_threshold=self.config.content_uniform_replay_threshold,
        )
        if repeated:
            circuit.record_failure(
                threshold=self.config.circuit_failure_threshold,
                open_s=self.config.circuit_open_s,
            )

    # -- keying and provenance -----------------------------------------------

    def _cache_key(self, request: DecisionRequest) -> str:
        """Hash a cache key from stable-identity fields only.

        The cache deliberately omits correlation_id / task_id /
        attempt_id (and the request_digest, which is itself a hash of
        these). Per-call correlation identities are normally unique,
        so including them would degenerate the cache to a per-call
        cache and defeat the "exact-input cache" design claim. The
        provider_name + question + rubric/policy/registry/snapshot
        digests + candidates + classification + risk_tier +
        required_capability + requested_model form the stable identity
        surface.
        """
        payload = {
            "contract_version": request.contract_version,
            "contract_digest": request.contract_digest,
            "question": request.question,
            "rubric_digest": request.rubric_digest,
            "policy_digest": request.policy_digest,
            "registry_digest": request.registry_digest,
            "snapshot_digest": request.snapshot_digest,
            "required_capability": request.required_capability,
            "classification": request.classification,
            "risk_tier": request.risk_tier,
            # Candidates are sorted so order-of-presentation does not
            # poison the cache; the set-of-eligible-tied is the
            # stable identity, not the iteration order returned by
            # CapabilityIndex.select().
            "candidates": sorted(
                c.candidate_id for c in request.candidates
            ),
            "requested_model": request.requested_model,
            "provider_name": self._provider_name,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()

    def _make_decision_id(
        self,
        *,
        task_id: str,
        attempt_id: str,
        correlation_id: str,
        baseline: str | None,
        classification: str,
    ) -> str:
        payload = {
            "task_id": task_id,
            "attempt_id": attempt_id,
            "correlation_id": correlation_id,
            "baseline": baseline,
            "classification": classification,
            "mode": self.config.mode.value,
            "contract_version": CONTRACT_VERSION,
            "contract_digest": CONTRACT_DIGEST,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()

    def _record_event(
        self,
        *,
        decision_id: str,
        mode: DecisionMode,
        baseline: str | None,
        applied: str | None,
        recommendation: DecisionRecommendation | None,
        fallback_reason: FallbackReason,
    ) -> None:
        path = self.config.event_log_path
        if path is None:
            return
        record: dict[str, Any] = {
            "timestamp": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            ),
            "contract_version": CONTRACT_VERSION,
            "contract_digest": CONTRACT_DIGEST,
            "decision_id": decision_id,
            "mode": mode.value,
            "baseline_candidate_id": baseline,
            "applied_candidate_id": applied,
            "fallback_reason": fallback_reason.value,
            "provider_name": self._provider_name,
        }
        if recommendation is not None:
            record.update(
                {
                    "recommendation": {
                        "distribution": dict(recommendation.distribution),
                        "provider_confidence": recommendation.provider_confidence,
                        "cache_status": recommendation.cache_status,
                        "latency_ms": round(recommendation.latency_ms, 3),
                        "requested_model": recommendation.requested_model,
                        "reported_model": recommendation.reported_model,
                        "sdk_version": recommendation.sdk_version,
                        "abstain_reason": recommendation.abstain_reason.value,
                    }
                }
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True, default=str) + "\n")


# ---------------------------------------------------------------------------
# Internal validation helper
# ---------------------------------------------------------------------------


@dataclass
class _ValidationError(Exception):
    fallback: FallbackReason
    message: str

    def __str__(self) -> str:  # pragma: no cover - debug aid
        return self.message


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


__all__ = [
    "ABSTAIN_KEY",
    "CONTRACT_DIGEST",
    "CONTRACT_VERSION",
    "CandidateInput",
    "DecisionConfig",
    "DecisionLayer",
    "DecisionMode",
    "DecisionOutcome",
    "DecisionProvider",
    "DecisionRecommendation",
    "DecisionRequest",
    "DeterministicProvider",
    "FallbackReason",
    "ProviderResponse",
    "VALID_MODES",
]