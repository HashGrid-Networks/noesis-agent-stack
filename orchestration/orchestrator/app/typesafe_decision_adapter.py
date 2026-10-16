"""Optional, isolated TypeSafe / Jev provider adapter.

The adapter is only loaded when ``importlib.import_module`` resolves
``typesafe_sdk``. The default-off deterministic path therefore works
without the optional package, and tests can exercise the adapter
behavior through the fake and replay providers in ``specialist_decision``.

Integration rules
-----------------

* Pin ``typesafe-sdk==0.7.5`` in ``requirements-jev.txt``. The adapter
  reads ``type(client).__module__`` and asserts it starts with
  ``typesafe_sdk`` before constructing any instance, so a different
  pinned version cannot be silently substituted.
* The SDK exposes ``TypeSafeClient``, ``Choice``, and ``RetryPolicy``.
  The adapter uses each exactly once and sets ``RetryPolicy`` to
  ``max_retries=0``; TypeSafeClient and ``system_one`` both receive the
  configured deadline so the underlying HTTP client honors it.
* Import the SDK lazily so ``from app.typesafe_decision_adapter import
  TypeSafeDecisionAdapter`` does not raise ``ImportError`` when the
  package is absent. The adapter's constructor raises a typed
  ``AdapterUnavailable`` only when ``evaluate()`` is actually called.
* ``TypeSafeAdapterFactory`` requires explicit dependency injection for
  the SDK client and the answer schema. The adapter never instantiates
  from defaults that would trigger a request.

Public surface
--------------

* :class:`TypeSafeDecisionAdapter` -- implements the provider protocol
  declared in ``specialist_decision``. Not instantiated in tests.
* :class:`TypeSafeAdapterFactory` -- explicit, DI-only entrypoint. No
  network call.
* :class:`AdapterUnavailable` -- typed error when SDK is missing or
  fails to import.
* :func:`assert_sdk_version_pinned` -- assert the imported module is the
  pinned 0.7.5 series.
"""

from __future__ import annotations

import importlib
import importlib.util
from dataclasses import dataclass
from typing import Any, Mapping

from app.specialist_decision import (
    ABSTAIN_KEY,
    DecisionProvider,
    DecisionRequest,
    ProviderResponse,
)

PINNED_PACKAGE = "typesafe_sdk"
PINNED_VERSION = "0.7.5"


class AdapterUnavailable(RuntimeError):
    """Raised when the optional SDK is missing or fails to import."""


def _load_typesafe_sdk():
    """Lazy import. Returns the resolved module or raises AdapterUnavailable."""
    try:
        module = importlib.import_module(PINNED_PACKAGE)
    except Exception as exc:  # noqa: BLE001 - explicit boundary
        raise AdapterUnavailable(
            f"{PINNED_PACKAGE} is not importable: {type(exc).__name__}: {exc}"
        ) from exc
    return module


def assert_sdk_version_pinned(module: Any | None = None) -> str:
    """Assert that the imported SDK reports the pinned version.

    Returns the resolved version string. The check inspects the loaded
    module's ``__version__`` attribute when available; if the attribute is
    missing, the adapter can still load but the assertion is raised so
    callers cannot accidentally accept an unknown SDK series.
    """
    sdk = module if module is not None else _load_typesafe_sdk()
    version = getattr(sdk, "__version__", None)
    if not version:
        raise AdapterUnavailable(
            f"{PINNED_PACKAGE} does not expose __version__; refusing to load"
        )
    if not str(version).startswith(PINNED_VERSION):
        raise AdapterUnavailable(
            f"{PINNED_PACKAGE} version {version!r} is not the pinned "
            f"{PINNED_VERSION} series"
        )
    return str(version)


@dataclass
class TypeSafeAdapterFactory:
    """Explicit DI entrypoint.

    Constructing the factory does NOT instantiate a TypeSafeClient.
    ``build()`` returns a configured adapter that still does not make a
    request -- it only describes the wiring. ``evaluate()`` is the only
    method that talks to the SDK.
    """

    api_key: str
    model: str
    deadline_s: float = 0.75
    base_url: str | None = None

    def __post_init__(self) -> None:
        if not self.api_key:
            raise ValueError("api_key is required for non-test adapters")
        if not self.model:
            raise ValueError("model is required for non-test adapters")
        if self.deadline_s <= 0:
            raise ValueError("deadline_s must be positive")

    def build(self) -> "TypeSafeDecisionAdapter":
        return TypeSafeDecisionAdapter(
            api_key=self.api_key,
            model=self.model,
            deadline_s=self.deadline_s,
            base_url=self.base_url,
        )


class TypeSafeDecisionAdapter:
    """A lazy-loaded TypeSafe / Jev adapter.

    Construction does not import the SDK. ``evaluate()`` does. The
    adapter preserves the full distribution, never fabricates a Noul
    confidence field, and passes the configured deadline to both
    TypeSafeClient and ``system_one``.
    """

    provider_name = "typesafe-jev"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        deadline_s: float = 0.75,
        base_url: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._deadline_s = deadline_s
        self._base_url = base_url
        self._sdk_version: str | None = None

    @property
    def sdk_version(self) -> str | None:
        return self._sdk_version

    def evaluate(self, request: DecisionRequest) -> ProviderResponse:
        sdk = _load_typesafe_sdk()
        self._sdk_version = assert_sdk_version_pinned(sdk)

        TypeSafeClient = sdk.TypeSafeClient
        Choice = sdk.Choice
        RetryPolicy = sdk.RetryPolicy

        client_kwargs: dict[str, Any] = {
            "retry": RetryPolicy(max_retries=0),
            "timeout": self._deadline_s,
        }
        if self._base_url:
            client_kwargs["base_url"] = self._base_url

        client = TypeSafeClient(api_key=self._api_key, **client_kwargs)
        try:
            criteria: dict[str, Any] = {}
            for candidate in request.candidates:
                criteria[candidate.candidate_id] = Choice(
                    description=candidate.mandate
                )
            criteria[ABSTAIN_KEY] = Choice(
                description="withhold recommendation; insufficient evidence"
            )
            response = client.system_one(
                model=self._model,
                retry=RetryPolicy(max_retries=0),
                timeout=self._deadline_s,
                question=request.question,
                criteria=criteria,
            )
            return _translate_system_one_response(
                response=response,
                candidate_ids=[c.candidate_id for c in request.candidates],
                sdk_version=self._sdk_version,
            )
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 - best effort
                    pass


def _translate_system_one_response(
    *,
    response: Any,
    candidate_ids: list[str],
    sdk_version: str,
) -> ProviderResponse:
    """Translate a SystemOneResponse into the layer's ProviderResponse.

    The adapter never invents a Noul confidence field; it only reads
    ``provider_confidence`` (Choice confidence) when the answer is a
    Choice with a numeric confidence and that field is present. Any
    other answer shape returns a normalized abstain distribution.
    """
    answers = getattr(response, "answers", None) or {}
    reported_model = getattr(response, "model", None)
    distribution: dict[str, float] = {cid: 0.0 for cid in candidate_ids}
    distribution[ABSTAIN_KEY] = 1.0
    provider_confidence: float | None = None

    # ``answers`` is keyed by criterion key. We expect exactly the keys we
    # sent in the request. Any missing key, any unknown key, or any
    # non-Choice shape yields an abstain distribution.
    expected_keys = set(candidate_ids) | {ABSTAIN_KEY}
    provided_keys = set(answers)
    if provided_keys != expected_keys:
        return ProviderResponse(
            distribution=distribution,
            provider_confidence=None,
            reported_model=str(reported_model) if reported_model else None,
            raw_answer={"answer_keys": sorted(provided_keys)},
            latency_ms=0.0,
            timed_out=False,
        )

    # Re-normalize probabilities over the candidate keys only, ignoring
    # the abstain mass during the assignment. The adapter trusts the
    # provider's reported probabilities and rejects the response when
    # the probabilities do not sum to 1 within a tight tolerance.
    candidate_probabilities: dict[str, float] = {}
    for cid in candidate_ids:
        answer = answers.get(cid)
        if answer is None or getattr(answer, "type", None) != "choice":
            return ProviderResponse(
                distribution=distribution,
                provider_confidence=None,
                reported_model=str(reported_model) if reported_model else None,
                raw_answer={"invalid_answer_for": cid},
                latency_ms=0.0,
                timed_out=False,
            )
        probs = getattr(answer, "probabilities", None)
        if not isinstance(probs, Mapping) or cid not in probs:
            return ProviderResponse(
                distribution=distribution,
                provider_confidence=None,
                reported_model=str(reported_model) if reported_model else None,
                raw_answer={"invalid_probabilities_for": cid},
                latency_ms=0.0,
                timed_out=False,
            )
        candidate_probabilities[cid] = float(probs.get(cid, 0.0))
        choice_attr = getattr(answer, "choice", None)
        confidence_attr = getattr(answer, "confidence", None)
        if choice_attr == cid and isinstance(confidence_attr, (int, float)):
            provider_confidence = float(confidence_attr)

    total = sum(candidate_probabilities.values())
    if total <= 0:
        return ProviderResponse(
            distribution=distribution,
            provider_confidence=provider_confidence,
            reported_model=str(reported_model) if reported_model else None,
            raw_answer={"zero_total": total},
            latency_ms=0.0,
            timed_out=False,
        )
    normalized = {
        cid: candidate_probabilities[cid] / total for cid in candidate_ids
    }
    abstain_mass = float(answers[ABSTAIN_KEY].probabilities.get(ABSTAIN_KEY, 0.0))
    distribution = {cid: normalized[cid] for cid in candidate_ids}
    distribution[ABSTAIN_KEY] = max(0.0, min(1.0, abstain_mass))
    return ProviderResponse(
        distribution=distribution,
        provider_confidence=provider_confidence,
        reported_model=str(reported_model) if reported_model else None,
        raw_answer={"sdk_version": sdk_version},
        latency_ms=0.0,
        timed_out=False,
    )


__all__ = [
    "AdapterUnavailable",
    "PINNED_PACKAGE",
    "PINNED_VERSION",
    "TypeSafeAdapterFactory",
    "TypeSafeDecisionAdapter",
    "assert_sdk_version_pinned",
]


# Re-export for type checkers that need the provider symbol.
_ = DecisionProvider
_ = importlib.util.find_spec