"""Fail-closed inference-routing enforcement for the orchestrator control plane.

This module loads the merged canonical inference-lane policy and evaluates a
dispatch request before provider/model selection. It never calls a provider,
never reads `~/.hermes`, never mutates routes, and only records structured,
non-sensitive routing decisions.

Modes:
  off                 -- disabled (startup only; requires explicit emergency flag)
  observe             -- record decisions, never block dispatch (default)
  shadow_deny         -- record would-deny, never block dispatch
  enforce_allowlist   -- deny unless explicitly allowlisted
  enforce             -- deny every policy violation

The enforcer is intentionally deterministic and local: all policy inputs are
repository files mounted read-only. Provider selection and adapter invocation
happen only after this gate returns `allow` in an enforcing mode.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app import _yaml_compat as yaml

REPO_ROOT = Path(__file__).resolve().parents[3]

VALID_MODES = ("off", "observe", "shadow_deny", "enforce_allowlist", "enforce")
DEFAULT_MODE = "observe"

ALLOWED_DATA_BY_RISK = {
    "r0": {"public", "internal", "internal_redacted"},
    "r1": {"public", "internal", "internal_redacted"},
    "r2": {"public", "internal", "internal_redacted"},
    "r3": {"public", "internal", "internal_redacted"},
}

# A lane may be used only when its status is exactly one of these.
SELECTABLE_LANE_STATUS = "approved"
NON_SELECTABLE_LANE_STATUSES = {"proposed", "disabled", "blocked_missing_configuration", "blocked"}

# OpenRouter is blocked regardless of requested route.
OPENROUTER_LANE_NAMES = {"OPENROUTER_FALLBACK", "OPENROUTER"}


class InferenceRoutingError(RuntimeError):
    """Base error for inference-routing enforcement failures."""


class PolicyLoadError(InferenceRoutingError):
    """Raised when a policy file cannot be loaded or validated."""


@dataclass(frozen=True)
class RoutingContext:
    """Non-sensitive inputs required to evaluate a routing decision."""

    task_id: str
    profile_id: str
    task_class: str
    risk_tier: str
    data_classification: str
    requested_lane: str | None = None
    requested_model: str | None = None
    approval_granted: bool = False
    approval_manifest_id: str | None = None
    rollback_plan_present: bool = False
    independent_review_provider_family: str | None = None
    tool_scope: tuple[str, ...] = ()


@dataclass(frozen=True)
class RoutingDecision:
    decision: str  # allow | deny | observe_allow | observe_deny
    allowed: bool
    reason_code: str
    reason: str
    resolved_lane: str | None = None
    resolved_model: str | None = None
    policy_version: str | None = None
    policy_sha256: str | None = None
    mode: str = DEFAULT_MODE
    latency_ms: float = 0.0


@dataclass(frozen=True)
class EnforcerConfig:
    mode: str
    fail_closed: bool
    policy_path: Path
    reconciliation_path: Path
    models_path: Path
    aliases_path: Path
    provider_policy_path: Path
    risk_tiers_path: Path
    decision_log_path: Path | None
    reload_seconds: int
    allowlist: tuple[tuple[str, str, str], ...]  # (profile_id, lane, model)
    emergency_disable: bool

    def __post_init__(self) -> None:
        if self.mode not in VALID_MODES:
            raise PolicyLoadError(f"unknown inference routing mode '{self.mode}'")
        if self.mode == "off" and not self.emergency_disable:
            raise PolicyLoadError(
                "mode 'off' requires INFERENCE_ROUTING_EMERGENCY_DISABLE=true at startup"
            )

    @classmethod
    def from_env(cls, repo_root: Path = REPO_ROOT) -> "EnforcerConfig":
        mode = os.environ.get("INFERENCE_ROUTING_MODE", DEFAULT_MODE).strip() or DEFAULT_MODE
        if mode not in VALID_MODES:
            raise PolicyLoadError(f"unknown INFERENCE_ROUTING_MODE '{mode}'")
        emergency_disable = os.environ.get("INFERENCE_ROUTING_EMERGENCY_DISABLE", "").lower() in {
            "1",
            "true",
            "yes",
        }
        if mode == "off" and not emergency_disable:
            raise PolicyLoadError(
                "mode 'off' requires INFERENCE_ROUTING_EMERGENCY_DISABLE=true at startup"
            )
        allowlist_raw = os.environ.get("INFERENCE_ROUTING_ALLOWLIST", "").strip()
        allowlist: list[tuple[str, str, str]] = []
        if allowlist_raw:
            for item in allowlist_raw.split(","):
                parts = [p.strip() for p in item.split(":")]
                if len(parts) != 3 or not all(parts):
                    raise PolicyLoadError(
                        "INFERENCE_ROUTING_ALLOWLIST entries must be profile_id:lane:model"
                    )
                allowlist.append((parts[0], parts[1], parts[2]))
        return cls(
            mode=mode,
            fail_closed=os.environ.get("INFERENCE_ROUTING_FAIL_CLOSED", "true").lower()
            in {"1", "true", "yes"},
            policy_path=repo_root / os.environ.get(
                "INFERENCE_ROUTING_POLICY_PATH", "platform/inference-routing.yaml"
            ),
            reconciliation_path=repo_root / os.environ.get(
                "INFERENCE_ROUTING_RECONCILIATION_PATH",
                "platform/runtime-reconciliation.yaml",
            ),
            models_path=repo_root / os.environ.get(
                "INFERENCE_ROUTING_MODELS_PATH", "shared/models.yaml"
            ),
            aliases_path=repo_root / os.environ.get(
                "INFERENCE_ROUTING_ALIASES_PATH", "shared/model-aliases.yaml"
            ),
            provider_policy_path=repo_root / os.environ.get(
                "INFERENCE_ROUTING_PROVIDER_POLICY_PATH", "shared/provider-policies.yaml"
            ),
            risk_tiers_path=repo_root / os.environ.get(
                "INFERENCE_ROUTING_RISK_TIERS_PATH", "platform/risk-tiers.yaml"
            ),
            decision_log_path=(
                Path(p)
                if (p := os.environ.get("INFERENCE_ROUTING_DECISION_LOG_PATH", "").strip())
                else None
            ),
            reload_seconds=int(os.environ.get("INFERENCE_ROUTING_RELOAD_SECONDS", "60")),
            allowlist=tuple(allowlist),
            emergency_disable=emergency_disable,
        )


@dataclass
class LoadedPolicy:
    policy: dict[str, Any]
    reconciliation: dict[str, Any]
    models: dict[str, Any]
    aliases: dict[str, Any]
    provider_policies: dict[str, Any]
    risk_tiers: dict[str, Any]
    policy_sha256: str
    source_paths: dict[str, str]

    @property
    def version(self) -> str:
        return str(self.policy.get("version", "unversioned"))


class InferenceRoutingEnforcer:
    """Pre-dispatch inference-routing gate.

    The gate is deliberately side-effect free apart from structured decision logs.
    It resolves a candidate lane/model from task context, validates it against the
    loaded policy, and returns a typed decision. In enforce modes, a non-allow
    decision must prevent provider selection and adapter invocation.
    """

    def __init__(self, config: EnforcerConfig | None = None) -> None:
        self.config = config or EnforcerConfig.from_env()
        self._policy: LoadedPolicy | None = None
        self._last_error: str | None = None
        self._profile_overrides: dict[str, dict[str, Any]] = {}
        self.load_policy()

    # ------------------------------------------------------------- lifecycle --

    def _read_yaml(self, path: Path) -> dict[str, Any]:
        if not path.is_file():
            raise PolicyLoadError(f"missing policy file: {path}")
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise PolicyLoadError(f"invalid YAML in policy file {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise PolicyLoadError(f"policy file is not a mapping: {path}")
        return data

    def _validate_policy_shape(self, loaded: LoadedPolicy) -> None:
        policy = loaded.policy
        lanes = policy.get("approved_lanes")
        if not isinstance(lanes, dict) or not lanes:
            raise PolicyLoadError("policy.approved_lanes must be a non-empty mapping")
        for lane_name, lane in lanes.items():
            if not isinstance(lane, dict):
                raise PolicyLoadError(f"lane '{lane_name}' must be a mapping")
            status = lane.get("status")
            if not status:
                raise PolicyLoadError(f"lane '{lane_name}' missing status")
            if status in NON_SELECTABLE_LANE_STATUSES:
                if lane_name in OPENROUTER_LANE_NAMES:
                    continue
            if "max_risk_tier" not in lane:
                raise PolicyLoadError(f"lane '{lane_name}' missing max_risk_tier")
        profiles = loaded.reconciliation.get("profiles")
        if not isinstance(profiles, list) or not profiles:
            raise PolicyLoadError("reconciliation.profiles must be a non-empty list")

    def load_policy(self) -> LoadedPolicy:
        start = time.monotonic()
        try:
            raw_files = {
                "policy": self.config.policy_path,
                "reconciliation": self.config.reconciliation_path,
                "models": self.config.models_path,
                "aliases": self.config.aliases_path,
                "provider_policies": self.config.provider_policy_path,
                "risk_tiers": self.config.risk_tiers_path,
            }
            loaded = {
                name: self._read_yaml(path) for name, path in raw_files.items()
            }
            canonical = json.dumps(loaded["policy"], sort_keys=True, default=str).encode("utf-8")
            policy_sha = hashlib.sha256(canonical).hexdigest()
            new_policy = LoadedPolicy(
                policy=loaded["policy"],
                reconciliation=loaded["reconciliation"],
                models=loaded["models"],
                aliases=loaded["aliases"],
                provider_policies=loaded["provider_policies"],
                risk_tiers=loaded["risk_tiers"],
                policy_sha256=policy_sha,
                source_paths={k: str(v) for k, v in raw_files.items()},
            )
            self._validate_policy_shape(new_policy)
            previous = self._policy
            self._policy = new_policy
            self._last_error = None
            return new_policy
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            if self._policy is None:
                # No validated policy exists: fail closed in every mode.
                raise
            # A validated policy exists. Retain the last known-good policy in
            # observe/shadow modes; in enforce modes the reload error is
            # surfaced and new dispatches are denied via _last_error.
            return self._policy
        finally:
            _ = start  # reserved for future latency metrics

    # --------------------------------------------------------------- helpers --

    def _lane(self, name: str) -> dict[str, Any] | None:
        if self._policy is None:
            return None
        lanes = self._policy.policy.get("approved_lanes") or {}
        lane = lanes.get(name)
        return lane if isinstance(lane, dict) else None

    def _profile_reconciliation(self, profile_id: str) -> dict[str, Any] | None:
        if self._policy is None:
            return None
        for entry in self._policy.reconciliation.get("profiles") or []:
            if isinstance(entry, dict) and entry.get("profile_id") == profile_id:
                merged = deepcopy(entry)
                override = self._profile_overrides.get(profile_id)
                if override:
                    merged.update(override)
                return merged
        return None

    def _risk_rank(self, risk_tier: str) -> int:
        order = {"r0": 0, "r1": 1, "r2": 2, "r3": 3}
        return order.get(risk_tier, 99)

    def _lane_max_risk_rank(self, lane: dict[str, Any]) -> int:
        return self._risk_rank(str(lane.get("max_risk_tier", "r0")))

    def _profile_lanes(self, profile: dict[str, Any]) -> list[str]:
        observed = ((profile.get("observed_runtime") or {}).get("provider_lane"))
        declared = ((profile.get("repo_declared") or {}).get("provider_lane"))
        target = profile.get("target_routing_class")
        classes = (self._policy.policy.get("profile_routing_classes") or {}) if self._policy else {}
        class_def = classes.get(target) or {}
        class_lanes = list(class_def.get("eligible_primary_lanes") or [])
        lanes: list[str] = []
        for candidate in [observed, declared, *class_lanes]:
            if candidate and candidate not in lanes:
                lanes.append(candidate)
        return lanes

    def _resolve_candidate(
        self,
        ctx: RoutingContext,
        profile: dict[str, Any],
    ) -> tuple[str | None, str | None, list[str]]:
        reasons: list[str] = []
        if ctx.requested_lane:
            lane_names = [ctx.requested_lane]
        else:
            lane_names = self._profile_lanes(profile)
        for lane_name in lane_names:
            lane = self._lane(lane_name)
            if lane is None:
                reasons.append(f"unknown_lane:{lane_name}")
                continue
            if lane_name in OPENROUTER_LANE_NAMES:
                reasons.append("openrouter_selected")
                continue
            status = str(lane.get("status", ""))
            if status != SELECTABLE_LANE_STATUS:
                reasons.append(f"lane_not_selectable:{lane_name}:{status}")
                continue
            if ctx.requested_model:
                model = ctx.requested_model
            else:
                model = str(
                    ((profile.get("observed_runtime") or {}).get("model_alias_or_id"))
                    or ((profile.get("repo_declared") or {}).get("model_alias_or_id"))
                    or ""
                ).strip() or None
            if model:
                pinned = set(lane.get("pinned_models") or [])
                aliases = set(lane.get("model_aliases") or [])
                if model not in pinned and model not in aliases:
                    reasons.append(f"model_not_allowed:{lane_name}:{model}")
                    continue
            else:
                reasons.append(f"model_missing:{lane_name}")
                continue
            return lane_name, model, reasons
        return None, None, reasons

    def _data_classification_allowed(self, ctx: RoutingContext) -> bool:
        if ctx.data_classification in {"secret", "restricted"}:
            return False
        if ctx.data_classification in {"confidential", "confidential_redacted"}:
            return False
        return ctx.data_classification in ALLOWED_DATA_BY_RISK.get(ctx.risk_tier, set())

    def _record_decision(
        self,
        *,
        ctx: RoutingContext,
        decision: RoutingDecision,
    ) -> None:
        if not self.config.decision_log_path:
            return
        self.config.decision_log_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "task_hash": hashlib.sha256(ctx.task_id.encode("utf-8")).hexdigest(),
            "profile_id": ctx.profile_id,
            "task_class": ctx.task_class,
            "risk_tier": ctx.risk_tier,
            "data_classification": ctx.data_classification,
            "resolved_lane": decision.resolved_lane,
            "resolved_model": decision.resolved_model,
            "decision": decision.decision,
            "reason_code": decision.reason_code,
            "policy_version": decision.policy_version,
            "policy_sha256": decision.policy_sha256,
            "mode": decision.mode,
            "latency_ms": round(decision.latency_ms, 3),
        }
        with self.config.decision_log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")

    # ------------------------------------------------------------ evaluation --
    def set_profile_overrides(self, profile_id: str, overrides: dict[str, Any]) -> None:
        """Apply test-scoped overrides to a reconciliation profile entry.

        This exists so unit tests and controlled deployments can model a
        reconciled approved profile without editing repository policy files.
        It does not grant runtime authority; the policy still governs lane,
        model, data class, risk tier, and approval gates.
        """
        self._profile_overrides[profile_id] = dict(overrides)

    def clear_profile_overrides(self, profile_id: str | None = None) -> None:
        if profile_id is None:
            self._profile_overrides.clear()
        else:
            self._profile_overrides.pop(profile_id, None)

    def _deny(
        self,
        ctx: RoutingContext,
        reason_code: str,
        reason: str,
        *,
        resolved_lane: str | None = None,
        resolved_model: str | None = None,
        latency_ms: float = 0.0,
    ) -> RoutingDecision:
        mode = self.config.mode
        if mode in {"observe", "shadow_deny", "off"}:
            decision_name = "observe_deny" if mode != "off" else "observe_deny"
            decision = RoutingDecision(
                decision=decision_name,
                allowed=True,  # observe modes never block dispatch
                reason_code=reason_code,
                reason=reason,
                resolved_lane=resolved_lane,
                resolved_model=resolved_model,
                policy_version=self._policy.version if self._policy else None,
                policy_sha256=self._policy.policy_sha256 if self._policy else None,
                mode=mode,
                latency_ms=latency_ms,
            )
        else:
            decision = RoutingDecision(
                decision="deny",
                allowed=False,
                reason_code=reason_code,
                reason=reason,
                resolved_lane=resolved_lane,
                resolved_model=resolved_model,
                policy_version=self._policy.version if self._policy else None,
                policy_sha256=self._policy.policy_sha256 if self._policy else None,
                mode=mode,
                latency_ms=latency_ms,
            )
        self._record_decision(ctx=ctx, decision=decision)
        return decision

    def _allow(
        self,
        ctx: RoutingContext,
        *,
        resolved_lane: str,
        resolved_model: str,
        reason: str,
        latency_ms: float,
    ) -> RoutingDecision:
        mode = self.config.mode
        if mode in {"observe", "shadow_deny", "off"}:
            decision_name = "observe_allow" if mode != "off" else "observe_allow"
        else:
            decision_name = "allow"
        decision = RoutingDecision(
            decision=decision_name,
            allowed=True,
            reason_code="allowed",
            reason=reason,
            resolved_lane=resolved_lane,
            resolved_model=resolved_model,
            policy_version=self._policy.version if self._policy else None,
            policy_sha256=self._policy.policy_sha256 if self._policy else None,
            mode=mode,
            latency_ms=latency_ms,
        )
        self._record_decision(ctx=ctx, decision=decision)
        return decision

    def evaluate(self, ctx: RoutingContext) -> RoutingDecision:
        start = time.monotonic()
        if self.config.mode == "off":
            return RoutingDecision(
                decision="observe_allow",
                allowed=True,
                reason_code="enforcer_disabled",
                reason="inference routing enforcer is disabled",
                mode=self.config.mode,
                latency_ms=(time.monotonic() - start) * 1000.0,
            )
        if self._policy is None:
            return self._deny(
                ctx,
                "policy_unavailable",
                "no validated policy loaded",
                latency_ms=(time.monotonic() - start) * 1000.0,
            )
        if self._last_error and self.config.mode in {"enforce", "enforce_allowlist"}:
            return self._deny(
                ctx,
                "policy_reload_failed",
                f"policy reload failed; failing closed: {self._last_error}",
                latency_ms=(time.monotonic() - start) * 1000.0,
            )
        profile = self._profile_reconciliation(ctx.profile_id)
        if profile is None:
            return self._deny(
                ctx,
                "profile_missing_from_reconciliation",
                f"profile '{ctx.profile_id}' is absent from runtime reconciliation",
                latency_ms=(time.monotonic() - start) * 1000.0,
            )
        resolution = profile.get("resolution") or {}
        state = str(resolution.get("state", ""))
        if state == "blocked":
            return self._deny(
                ctx,
                "reconciliation_blocked",
                f"profile '{ctx.profile_id}' is blocked in runtime reconciliation",
                latency_ms=(time.monotonic() - start) * 1000.0,
            )
        freeze = self._policy.policy.get("interim_operating_mode") or {}
        if freeze.get("mode") == "reconciliation_freeze":
            permitted = freeze.get("permitted_existing_behavior") or {}
            kimi = permitted.get("KIMI_CODE") or {}
            if str(kimi.get("status", "")) != "observed_runtime_baseline":
                return self._deny(
                    ctx,
                    "reconciliation_freeze",
                    "reconciliation freeze does not permit this profile",
                    latency_ms=(time.monotonic() - start) * 1000.0,
                )
        if not self._data_classification_allowed(ctx):
            return self._deny(
                ctx,
                "data_classification_denied",
                f"data classification '{ctx.data_classification}' is not allowed before provider selection",
                latency_ms=(time.monotonic() - start) * 1000.0,
            )
        if ctx.requested_lane and ctx.requested_lane in OPENROUTER_LANE_NAMES:
            return self._deny(
                ctx,
                "openrouter_selected",
                "OpenRouter is blocked before provider selection",
                resolved_lane=ctx.requested_lane,
                latency_ms=(time.monotonic() - start) * 1000.0,
            )
        lane_name, model, reasons = self._resolve_candidate(ctx, profile)
        if lane_name is None or model is None:
            reason_code = reasons[0].split(":", 1)[0] if reasons else "route_unresolvable"
            reason = "; ".join(reasons) if reasons else "no eligible lane/model resolved"
            return self._deny(
                ctx,
                reason_code,
                reason,
                latency_ms=(time.monotonic() - start) * 1000.0,
            )
        freeze = self._policy.policy.get("interim_operating_mode") or {}
        if freeze.get("mode") == "reconciliation_freeze":
            observed_lane = ((profile.get("observed_runtime") or {}).get("provider_lane"))
            if (
                ctx.profile_id != "main-hermes"
                and observed_lane
                and lane_name != observed_lane
            ):
                return self._deny(
                    ctx,
                    "reconciliation_freeze",
                    "reconciliation freeze permits only observed existing behavior",
                    resolved_lane=lane_name,
                    resolved_model=model,
                    latency_ms=(time.monotonic() - start) * 1000.0,
                )
        if lane_name in OPENROUTER_LANE_NAMES:
            return self._deny(
                ctx,
                "openrouter_selected",
                "OpenRouter is blocked before provider selection",
                resolved_lane=lane_name,
                resolved_model=model,
                latency_ms=(time.monotonic() - start) * 1000.0,
            )
        lane = self._lane(lane_name) or {}
        if self._risk_rank(ctx.risk_tier) > self._lane_max_risk_rank(lane):
            return self._deny(
                ctx,
                "risk_tier_exceeds_lane_max",
                f"risk tier '{ctx.risk_tier}' exceeds lane max '{lane.get('max_risk_tier')}'",
                resolved_lane=lane_name,
                resolved_model=model,
                latency_ms=(time.monotonic() - start) * 1000.0,
            )
        if ctx.risk_tier in {"r2", "r3"}:
            if not ctx.independent_review_provider_family:
                return self._deny(
                    ctx,
                    "independent_review_required",
                    f"risk tier '{ctx.risk_tier}' requires independent provider-family review evidence",
                    resolved_lane=lane_name,
                    resolved_model=model,
                    latency_ms=(time.monotonic() - start) * 1000.0,
                )
            requested_family = str(lane.get("provider_family", "")).lower()
            review_family = ctx.independent_review_provider_family.lower()
            if requested_family and review_family and requested_family == review_family:
                return self._deny(
                    ctx,
                    "independent_review_same_provider_family",
                    "independent review must come from a different provider family",
                    resolved_lane=lane_name,
                    resolved_model=model,
                    latency_ms=(time.monotonic() - start) * 1000.0,
                )
        if ctx.risk_tier == "r3":
            if not (ctx.approval_granted and ctx.approval_manifest_id):
                return self._deny(
                    ctx,
                    "r3_approval_required",
                    "r3 requires explicit human approval with an approval manifest",
                    resolved_lane=lane_name,
                    resolved_model=model,
                    latency_ms=(time.monotonic() - start) * 1000.0,
                )
            if not ctx.rollback_plan_present:
                return self._deny(
                    ctx,
                    "r3_rollback_required",
                    "r3 requires rollback metadata before dispatch",
                    resolved_lane=lane_name,
                    resolved_model=model,
                    latency_ms=(time.monotonic() - start) * 1000.0,
                )
        if self.config.mode == "enforce_allowlist":
            allow_key = (ctx.profile_id, lane_name, model)
            if allow_key not in self.config.allowlist:
                return self._deny(
                    ctx,
                    "not_allowlisted",
                    "profile/lane/model tuple is not explicitly allowlisted",
                    resolved_lane=lane_name,
                    resolved_model=model,
                    latency_ms=(time.monotonic() - start) * 1000.0,
                )
        return self._allow(
            ctx,
            resolved_lane=lane_name,
            resolved_model=model,
            reason="route allowed by policy",
            latency_ms=(time.monotonic() - start) * 1000.0,
        )
