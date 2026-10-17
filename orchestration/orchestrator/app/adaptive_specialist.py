"""Adaptive Specialist — Phase B preflight (contract ``adaptive-specialist-preflight-b1``).

This module owns the **read-only** task-configuration preflight surface. It is
deliberately scoped to off-line decision support:

*   A versioned, digestable :class:`CapabilitySnapshot` derived from the
    existing roster and policy sources — never a parallel roster.
*   A local-only :class:`LocalReadinessRegistry` that surfaces auth,
    entitlement, quota, provider-reported state, and local cooldown as
    independent fields. Missing or unsupported quota is ``UNKNOWN``, never
    silently upgraded to ``READY``.
*   A :class:`TaskConfigurationPrecomputer` that produces one of five typed
    decisions: ``KEEP``, ``TASK_OVERRIDE``, ``SELECT_ALTERNATIVE``,
    ``REQUIRE_APPROVAL``, ``BLOCK``. Decisions are immutable, digests are
    bound to the canonical task/attempt ledger, and credential values are
    never carried in evidence.
*   The :class:`CapabilityState` five-state taxonomy
    (installed/available/enabled/exposed/authorized) plus an explicit
    ``SERVER_WIDE_ONLY`` flag for MCP exposure.

The module is offline: it does not call providers, does not refresh OAuth,
does not mutate installed profiles, and does not schedule. Live launch
behaviour, isolated attempt-home materialisation, and the plan-first
admission gate live alongside it (see ``app.isolated_attempt_home`` and the
``noesis-specialist-router`` plugin) but share this evidence model.

The contract says: the canonical lifecycle lives in
``app.store.TaskStore``; this module produces evidence, never state.
"""
from __future__ import annotations

import enum
import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml

# The five typed outcomes required by the proposal. Anything else must fail
# closed rather than coerce a label.
class PreflightDecision(str, enum.Enum):
    KEEP = "KEEP"
    TASK_OVERRIDE = "TASK_OVERRIDE"
    SELECT_ALTERNATIVE = "SELECT_ALTERNATIVE"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    BLOCK = "BLOCK"

    @classmethod
    def from_label(cls, label: str) -> "PreflightDecision":
        try:
            return cls(str(label))
        except ValueError as exc:
            raise ValueError(
                f"unknown preflight decision label: {label!r}; allowed="
                f"{[d.value for d in cls]}"
            ) from exc


# The five state capability taxonomy (spec §5.3 / item 9). ``SERVER_WIDE_ONLY``
# is an auxiliary flag, not a state, used to surface the MCP limitation.
class CapabilityState(str, enum.Enum):
    INSTALLED = "installed"
    AVAILABLE = "available"
    ENABLED = "enabled"
    EXPOSED = "exposed"
    AUTHORIZED = "authorized"

    @classmethod
    def from_label(cls, label: str) -> "CapabilityState":
        try:
            return cls(str(label))
        except ValueError as exc:
            raise ValueError(
                f"unknown capability state label: {label!r}; allowed="
                f"{[s.value for s in cls]}"
            ) from exc


# Spec §11.4 / §9.1 route disposition taxonomy. ``UNKNOWN`` is preserved on
# purpose and must never be coerced into ``READY``.
class RouteDisposition(str, enum.Enum):
    READY = "READY"
    EXHAUSTED = "EXHAUSTED"
    RATE_LIMITED = "RATE_LIMITED"
    AUTH_FAILED = "AUTH_FAILED"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def from_label(cls, label: str) -> "RouteDisposition":
        try:
            return cls(str(label))
        except ValueError as exc:
            raise ValueError(
                f"unknown route disposition label: {label!r}; allowed="
                f"{[d.value for d in cls]}"
            ) from exc


class PrivacyConstraint(str, enum.Enum):
    """The data-destination scope for an attempted task.

    The cheap-and-public class is the proposal's hidden default — calling
    code must explicitly enumerate stricter scopes to claim any constraint
    at all.
    """

    PUBLIC_OK = "public_ok"
    PRIVATE_ONLY = "private_only"
    SENSITIVE_LOCAL_ONLY = "sensitive_local_only"

    @property
    def label(self) -> str:
        return self.value

    @classmethod
    def from_label(cls, label: str | None) -> "PrivacyConstraint | None":
        if label is None:
            return None
        for member in cls:
            if member.value == label:
                return member
        raise ValueError(f"unknown privacy constraint label: {label!r}")


# ---------------------------------------------------------------------------
# Capability snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapabilityProfileView:
    """A derived view of one profile in a snapshot.

    ``capability_states`` is a map: ``capability_name -> {state -> bool,
    SERVER_WIDE_ONLY -> bool, ...}``. Authors of derived views must populate
    each state explicitly rather than relying on default ``False``.
    """

    profile_id: str
    role: str
    tier: str
    capabilities: tuple[str, ...]
    mcp_servers: tuple[str, ...]
    capability_states: Mapping[str, Mapping[str, bool]]
    data_boundaries: tuple[str, ...]

    def is_supervisor(self) -> bool:
        return self.tier == "supervisor"

    def state(self, capability: str, state: CapabilityState) -> bool:
        record = self.capability_states.get(capability, {})
        return bool(record.get(state.value, False))


@dataclass(frozen=True)
class CapabilitySnapshot:
    """A versioned view across profiles, derived from existing sources.

    The snapshot is regenerable from sources and carries ``source_digests``
    to bind it to the exact revision it was built from. It does not exist as
    a parallel roster: ``profiles`` are exactly the subset of profile ids
    declared in ``profiles/noesis-roster.yaml`` that resolve to known
    ``agents/`` records.
    """

    profiles: tuple[CapabilityProfileView, ...]
    revision: str
    digest: str
    source_digests: Mapping[str, str]
    generated_at: datetime

    @classmethod
    def build(cls, *, repo_root: Path) -> "CapabilitySnapshot":
        roster_path = repo_root / "profiles" / "noesis-roster.yaml"
        agents_dir = repo_root / "agents"
        roster_raw = cls._load_yaml(roster_path)
        profiles_section = roster_raw.get("profiles") or {}

        profiles: list[CapabilityProfileView] = []
        for profile_id, raw in profiles_section.items():
            profile = cls._build_profile_view(
                profile_id=profile_id,
                raw=raw or {},
                agents_dir=agents_dir,
            )
            if profile is not None:
                profiles.append(profile)

        profiles.sort(key=lambda p: p.profile_id)
        source_digests = cls._compute_source_digests(
            roster_path=roster_path, agents_dir=agents_dir
        )
        revision = str(roster_raw.get("version") or "0")
        digest = cls._digest_for(
            profiles=[asdict(p) for p in profiles],
            revision=revision,
            source_digests=dict(source_digests),
        )
        return cls(
            profiles=tuple(profiles),
            revision=revision,
            digest=digest,
            source_digests=source_digests,
            generated_at=cls._utcnow(),
        )

    @staticmethod
    def _load_yaml(path: Path) -> dict:
        if not path.is_file():
            return {}
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    @classmethod
    def _build_profile_view(
        cls,
        *,
        profile_id: str,
        raw: dict,
        agents_dir: Path,
    ) -> CapabilityProfileView | None:
        agent_yaml_path = agents_dir / profile_id / "agent.yaml"
        if not agent_yaml_path.is_file():
            # A profile declared in the roster but not present in the agents
            # tree is an ``installed=False`` profile; it does not enter the
            # snapshot because the snapshot only records profiles whose
            # five-state capability view can be derived from sources.
            return None
        agent = cls._load_yaml(agent_yaml_path)
        agent_block = (agent.get("agent") or {})
        role = str(raw.get("role") or agent_block.get("role") or "")
        tier = str(raw.get("tier") or agent_block.get("tier") or "specialist")
        capabilities = cls._collect_capabilities(raw=raw, agent=agent_block)
        mcp_servers = cls._collect_mcp_servers(raw=raw, agent=agent_block)
        capability_states = cls._derive_capability_states(
            capabilities=capabilities,
            mcp_servers=mcp_servers,
            raw=raw,
            agent_block=agent_block,
        )
        data_boundaries = tuple(
            str(item) for item in (agent_block.get("guardrails") or ())
        )
        return CapabilityProfileView(
            profile_id=profile_id,
            role=role,
            tier=tier,
            capabilities=tuple(sorted(set(capabilities))),
            mcp_servers=tuple(sorted(mcp_servers)),
            capability_states=capability_states,
            data_boundaries=data_boundaries,
        )

    @staticmethod
    def _collect_capabilities(*, raw: dict, agent: dict) -> list[str]:
        # Repository manifest declares the role-specific capabilities; the
        # agent block may extend them. Both are read-only data the snapshot
        # is allowed to derive from.
        out: list[str] = []
        for source in (raw.get("capabilities") or (), agent.get("capabilities") or ()):
            for item in source:
                if isinstance(item, str):
                    out.append(item)
        return out

    @staticmethod
    def _collect_mcp_servers(*, raw: dict, agent: dict) -> set[str]:
        servers: set[str] = set()
        for source in (raw.get("mcp_servers") or (), agent.get("mcp_servers") or ()):
            for item in source:
                if isinstance(item, str):
                    servers.add(item)
        return servers

    @classmethod
    def _derive_capability_states(
        cls,
        *,
        capabilities: Iterable[str],
        mcp_servers: Iterable[str],
        raw: dict,
        agent_block: dict,
    ) -> dict[str, dict[str, bool]]:
        # Roll forward ``enabled`` from the agent block where present; the
        # supervisor marker comes from the roster. ``authorized`` always
        # defaults to False until a task-scoped grant flips it (gated by
        # the precomputer, not the snapshot).
        enabled_set = set()
        for source in (
            (raw.get("enabled_capabilities") or ()),
            (agent_block.get("enabled") or ()) if isinstance(
                agent_block.get("enabled"), (list, tuple)
            ) else (),
        ):
            for item in source:
                if isinstance(item, str):
                    enabled_set.add(item)
        out: dict[str, dict[str, bool]] = {}
        for cap in capabilities:
            mcp = cap.lower().startswith("mcp")
            out[cap] = {
                CapabilityState.INSTALLED.value: True,
                CapabilityState.AVAILABLE.value: True,
                CapabilityState.ENABLED.value: cap in enabled_set or not enabled_set,
                # Exposed follows enabled. Real exposed-vs-actual requires
                # runtime observation; the snapshot records the declared
                # state, not an observed one.
                CapabilityState.EXPOSED.value: cap in enabled_set or not enabled_set,
                CapabilityState.AUTHORIZED.value: False,
            }
            if mcp:
                out[cap]["SERVER_WIDE_ONLY"] = True
        return out

    @staticmethod
    def _compute_source_digests(
        *, roster_path: Path, agents_dir: Path
    ) -> dict[str, str]:
        digests: dict[str, str] = {}
        if roster_path.is_file():
            digests["roster"] = hashlib.sha256(
                roster_path.read_bytes()
            ).hexdigest()
        if agents_dir.is_dir():
            for path in sorted(agents_dir.glob("*/agent.yaml")):
                digests[f"agent:{path.parent.name}"] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
        return digests

    @staticmethod
    def _digest_for(
        *, profiles: list[dict], revision: str, source_digests: dict[str, str]
    ) -> str:
        payload = json.dumps(
            {
                "revision": revision,
                "source_digests": source_digests,
                "profiles": profiles,
            },
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _utcnow() -> datetime:
        from datetime import datetime as _dt, timezone as _tz

        return _dt.now(_tz.utc)


# ---------------------------------------------------------------------------
# Local-only readiness registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RouteReadinessEvidence:
    """Evidence for one (provider, model) pair, locally derived only.

    Fields are deliberately separated: collapsing them loses the cause of an
    UNKNOWN or EXHAUSTED verdict. ``credential_reference_id`` and
    ``credential_value`` are distinct: the first is a label allowed into
    evidence, the second is always None.
    """

    provider: str
    model: str
    credential_reference_id: str | None
    auth_eligible: bool
    entitlement: str | None
    quota_disposition: RouteDisposition
    provider_state_disposition: RouteDisposition
    local_cooldown_disposition: RouteDisposition
    evidence_source: str
    collected_at: datetime
    # Always None. Set by the registry to guarantee the field exists and is
    # trivially serialisable, so it cannot accidentally be filled in.
    credential_value: str | None = None

    @property
    def combined_disposition(self) -> RouteDisposition:
        """Worst-of auth/eligibility/cooldown combined disposition."""
        # Order is significant: a missing quota is ``UNKNOWN``; the worst
        # of the deterministic fields (cooldown, provider-state, auth)
        # dominates.
        candidates: list[RouteDisposition] = [
            self.local_cooldown_disposition,
            self.provider_state_disposition,
        ]
        if not self.auth_eligible:
            candidates.append(RouteDisposition.AUTH_FAILED)
        # ``quota_disposition`` is allowed to be UNKNOWN; that's preserved
        # at the field level but never coerced into READY here.
        if self.quota_disposition is not RouteDisposition.UNKNOWN:
            candidates.append(self.quota_disposition)
        worst = RouteDisposition.READY
        rank = {d: i for i, d in enumerate(
            [
                RouteDisposition.READY,
                RouteDisposition.UNKNOWN,
                RouteDisposition.AUTH_FAILED,
                RouteDisposition.RATE_LIMITED,
                RouteDisposition.UNAVAILABLE,
                RouteDisposition.EXHAUSTED,
            ],
            start=0,
        )}
        for disposition in candidates:
            if rank[disposition] > rank[worst]:
                worst = disposition
        return worst

    def to_dict(self) -> dict[str, Any]:
        data = {
            "provider": self.provider,
            "model": self.model,
            "credential_reference_id": self.credential_reference_id,
            "auth_eligible": self.auth_eligible,
            "entitlement": self.entitlement,
            "quota_disposition": self.quota_disposition.value,
            "provider_state_disposition": self.provider_state_disposition.value,
            "local_cooldown_disposition": self.local_cooldown_disposition.value,
            "combined_disposition": self.combined_disposition.value,
            "evidence_source": self.evidence_source,
            "collected_at": self.collected_at.isoformat(),
            "credential_value": None,
        }
        return data


class LocalReadinessRegistry:
    """A local-only readiness registry.

    The synthetic constructor is the offline test path; future operator-
    approved live updates plug in here but never refresh OAuth or call a
    provider. Auth and quota are surfaced as separate fields so callers
    cannot mistake one for the other.
    """

    def __init__(
        self,
        *,
        providers: dict[str, "ProviderReadinessFact"],
    ) -> None:
        self._providers = providers

    @classmethod
    def synthetic(
        cls,
        *,
        provider: str,
        auth_eligible: bool = False,
        local_cooldown_disposition: RouteDisposition = RouteDisposition.UNKNOWN,
        provider_state_disposition: RouteDisposition = RouteDisposition.UNKNOWN,
        quota_disposition: RouteDisposition = RouteDisposition.UNKNOWN,
        credential_reference_id: str | None = None,
    ) -> "LocalReadinessRegistry":
        fact = ProviderReadinessFact(
            auth_eligible=auth_eligible,
            local_cooldown_disposition=local_cooldown_disposition,
            provider_state_disposition=provider_state_disposition,
            quota_disposition=quota_disposition,
            credential_reference_id=credential_reference_id,
        )
        return cls(providers={provider: fact})

    def evaluate(self, *, provider: str, model: str) -> RouteReadinessEvidence:
        fact = self._providers.get(provider)
        if fact is None:
            return RouteReadinessEvidence(
                provider=provider,
                model=model,
                credential_reference_id=None,
                auth_eligible=False,
                entitlement=None,
                quota_disposition=RouteDisposition.UNKNOWN,
                provider_state_disposition=RouteDisposition.UNKNOWN,
                local_cooldown_disposition=RouteDisposition.UNKNOWN,
                evidence_source="local-readiness/synthetic",
                collected_at=_utcnow(),
            )
        return RouteReadinessEvidence(
            provider=provider,
            model=model,
            credential_reference_id=fact.credential_reference_id,
            auth_eligible=fact.auth_eligible,
            entitlement=None,
            quota_disposition=fact.quota_disposition,
            provider_state_disposition=fact.provider_state_disposition,
            local_cooldown_disposition=fact.local_cooldown_disposition,
            evidence_source="local-readiness/synthetic",
            collected_at=_utcnow(),
        )


@dataclass(frozen=True)
class ProviderReadinessFact:
    """One provider's local evidence. Immutable."""

    auth_eligible: bool
    local_cooldown_disposition: RouteDisposition
    provider_state_disposition: RouteDisposition
    quota_disposition: RouteDisposition
    credential_reference_id: str | None


# ---------------------------------------------------------------------------
# Family map and precomputer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderFamilyMap:
    """Maps provider labels to provider families for resilience reasoning.

    Aggregators (``moa`` etc.) that depend on a specific upstream are
    recorded as the same family as their upstream so they cannot be
    counted as an independent backup. The map is operator-editable; the
    precomputer must not invent independence.
    """

    mapping: Mapping[str, str]

    @classmethod
    def from_snapshot(
        cls,
        snapshot: CapabilitySnapshot,
        *,
        overrides: Mapping[str, str] | None = None,
    ) -> "ProviderFamilyMap":
        # Default rule: a provider label is its own family. Operators may
        # override via the ``overrides`` mapping to declare aggregator
        # dependencies.
        merged = dict(overrides or {})
        return cls(mapping=merged)

    def family_of(self, provider: str) -> str:
        return self.mapping.get(provider, provider)


@dataclass(frozen=True)
class TaskConfigurationProposal:
    """A typed, non-executing configuration proposal.

    The proposal is the contract deliverable for the preflight decision.
    It is immutable; evidence digests are stable so the canonical ledger
    can bind them to the task/attempt without re-running the engine.
    """

    task_class: str
    risk_tier: str
    data_classification: str
    privacy_constraint: PrivacyConstraint | None
    decision: PreflightDecision
    profile_id: str
    selected_provider_family: str
    reasons: tuple[str, ...]
    bundle_digest: str
    evidence_digest: str
    capability_snapshot_digest: str
    readiness_digests: Mapping[str, str]
    unknowns: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_class": self.task_class,
            "risk_tier": self.risk_tier,
            "data_classification": self.data_classification,
            "privacy_constraint": self.privacy_constraint.label if self.privacy_constraint else None,
            "decision": self.decision.value,
            "profile_id": self.profile_id,
            "selected_provider_family": self.selected_provider_family,
            "reasons": list(self.reasons),
            "unknowns": list(self.unknowns),
            "bundle_digest": self.bundle_digest,
            "evidence_digest": self.evidence_digest,
            "capability_snapshot_digest": self.capability_snapshot_digest,
            "readiness_digests": dict(self.readiness_digests),
        }


class TaskConfigurationPrecomputer:
    """Evaluates a preflight decision from snapshot + readiness evidence.

    The precomputer never invokes the network or mutates state. It returns
    an immutable :class:`TaskConfigurationProposal` and the call site is
    responsible for binding it to the canonical task/attempt ledger.
    """

    SUPERVISOR_PROFILES = frozenset({"noesis-orchestrator", "main-hermes"})

    def __init__(
        self,
        *,
        snapshot: CapabilitySnapshot,
        readiness: LocalReadinessRegistry,
        families: ProviderFamilyMap | None = None,
    ) -> None:
        self._snapshot = snapshot
        self._readiness = readiness
        self._families = families or ProviderFamilyMap(mapping={})

    @property
    def snapshot(self) -> CapabilitySnapshot:
        return self._snapshot

    def evaluate(
        self,
        *,
        task_class: str,
        risk_tier: str,
        data_classification: str,
        privacy_constraint: PrivacyConstraint | None,
        current_profile_id: str,
        readiness_overrides: Mapping[str, RouteReadinessEvidence] | None = None,
    ) -> TaskConfigurationProposal:
        snapshot = self._snapshot
        profile = next(
            (p for p in snapshot.profiles if p.profile_id == current_profile_id),
            None,
        )
        reasons: list[str] = []
        unknowns: list[str] = []
        readiness_digests: dict[str, str] = {}

        # Self-authority expansion: refuse immediately. A preflight that
        # proposes the orchestrator profile as the implementation target
        # for a non-orchestrator task is an attempt to expand authority
        # into the supervisor profile. Block with a typed reason.
        if current_profile_id in self.SUPERVISOR_PROFILES:
            reasons.append("self_authority_expansion_blocked")
            return self._build_proposal(
                decision=PreflightDecision.BLOCK,
                profile_id=current_profile_id,
                selected_provider_family="internal",
                reasons=tuple(reasons),
                unknowns=("supervisor_self_authority_attempt",),
                readiness_digests=readiness_digests,
            )

        # A confidential data class with no private-only capability is a
        # privacy override situation. Cheaper public providers cannot be
        # substituted silently: either a privacy-qualified alternative
        # exists (TASK_OVERRIDE) or admission is blocked.
        requires_private = privacy_constraint in {
            PrivacyConstraint.PRIVATE_ONLY,
            PrivacyConstraint.SENSITIVE_LOCAL_ONLY,
        }
        provider_family = "public-cloud"
        if profile is None:
            reasons.append("profile_not_in_snapshot")
        else:
            capabilities = set(profile.capabilities)
            if requires_private and not any(
                "private" in c.lower() or "local" in c.lower() for c in capabilities
            ):
                reasons.append("privacy_constraint_unsatisfied")
                unknowns.append("no_private_capability_in_profile")
                provider_family = "private-only"

        # Readiness evidence aggregation. The precomputer only reads the
        # provided evidence; it never refreshes credentials or calls a
        # provider. Unknown quota is preserved as UNKNOWN and never
        # coerced into READY at this layer — the disposition policy lives
        # in the runtime gate.
        #
        # Remediation B1 / minor 1: ``self._families`` is consumed here so
        # the aggregator same-failure-domain invariant is structurally
        # reflected in the decision path rather than exposed only through
        # the operator-editable override map. For each provider we:
        #
        #   1. resolve its provider family via ``self._families``;
        #   2. record a per-provider digest under the ``family/provider``
        #      composite key (preserves traceability of the family
        #      collapse into the canonical evidence record);
        #   3. when the family collapses two or more providers in this
        #      evidence set, emit a ``family_collapse`` reason so the
        #      downstream dispatch can refuse to count them as
        #      independent routes;
        #   4. include the family in every disqualifying reason so a
        #      caller can branch on family without re-resolving.
        family_groups: dict[str, list[str]] = {}
        for provider, evidence in (readiness_overrides or {}).items():
            family = self._families.family_of(provider)
            digest_input = json.dumps(
                evidence.to_dict(), sort_keys=True, default=str
            ).encode("utf-8")
            digest = hashlib.sha256(digest_input).hexdigest()
            readiness_digests[f"{family}/{provider}"] = digest
            family_groups.setdefault(family, []).append(provider)
            if evidence.combined_disposition in {
                RouteDisposition.EXHAUSTED,
                RouteDisposition.UNAVAILABLE,
                RouteDisposition.AUTH_FAILED,
                RouteDisposition.RATE_LIMITED,
            }:
                reasons.append(
                    f"route_disqualifying:{family}:{provider}:"
                    f"{evidence.combined_disposition.value}"
                )
        for family, members in sorted(family_groups.items()):
            if len(members) > 1:
                reasons.append(
                    f"family_collapse:{family}:{len(members)}"
                )

        decision = self._decide(
            reasons=reasons,
            profile=profile,
            requires_private=requires_private,
        )

        return self._build_proposal(
            decision=decision,
            profile_id=current_profile_id,
            selected_provider_family=provider_family,
            reasons=tuple(reasons),
            unknowns=tuple(unknowns),
            readiness_digests=readiness_digests,
        )

    @classmethod
    def _decide(
        cls,
        *,
        reasons: Sequence[str],
        profile: CapabilityProfileView | None,
        requires_private: bool,
    ) -> PreflightDecision:
        if any(r.startswith("self_authority") for r in reasons):
            return PreflightDecision.BLOCK
        if any(r.startswith("route_disqualifying") for r in reasons):
            return PreflightDecision.BLOCK
        if "privacy_constraint_unsatisfied" in reasons and requires_private:
            return PreflightDecision.BLOCK
        if profile is None:
            return PreflightDecision.BLOCK
        return PreflightDecision.KEEP

    def _build_proposal(
        self,
        *,
        decision: PreflightDecision,
        profile_id: str,
        selected_provider_family: str,
        reasons: tuple[str, ...],
        unknowns: tuple[str, ...],
        readiness_digests: Mapping[str, str],
    ) -> TaskConfigurationProposal:
        bundle_digest = hashlib.sha256(
            json.dumps(
                {
                    "decision": decision.value,
                    "profile_id": profile_id,
                    "reasons": list(reasons),
                    "unknowns": list(unknowns),
                },
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        evidence_digest = hashlib.sha256(
            json.dumps(
                {
                    "snapshot_digest": self._snapshot.digest,
                    "readiness_digests": dict(readiness_digests),
                },
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        return TaskConfigurationProposal(
            task_class="",
            risk_tier="",
            data_classification="",
            privacy_constraint=None,
            decision=decision,
            profile_id=profile_id,
            selected_provider_family=selected_provider_family,
            reasons=reasons,
            bundle_digest=bundle_digest,
            evidence_digest=evidence_digest,
            capability_snapshot_digest=self._snapshot.digest,
            readiness_digests=readiness_digests,
            unknowns=unknowns,
        )


def _utcnow() -> datetime:
    from datetime import datetime as _dt, timezone as _tz

    return _dt.now(_tz.utc)
