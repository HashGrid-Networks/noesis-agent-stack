"""Roster-backed profile registry for the noesis-orchestrator control plane.

Canonical identities come from profiles/noesis-roster.yaml (profile existence,
tier, toolsets) joined with agents/<profile>/agent.yaml (declared capabilities
and guardrails). Advisory agency identities come from agency/curation.yaml —
the sole roster source for the third-party fleet — via app/agency_registry.py
(source "agency-curation", tier "advisory", staged, no terminal authority).
A small set of execution lanes from platform/agent-registry.yaml
is additionally admissible, but only when explicitly listed in that file's
`orchestration_lanes` allowlist AND marked `enabled: true`. Registration never
implicitly activates a lane, and unknown names are rejected at dispatch time.

Identity states exposed on ProfileRecord:
  - roster profiles:   "activated" (wave 1, applied) or "staged" (waves 2–3)
  - registry lanes:    "activated" (allowlisted + enabled) — anything else is
                       not visible to admission at all
Authorization for a specific task is still decided by policy.validate_assignment;
registration/activation alone grants nothing.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from pathlib import Path

from app import _yaml_compat as yaml
from app import agency_registry

REPO_ROOT = Path(__file__).resolve().parents[3]
ROSTER_PATH = REPO_ROOT / "profiles" / "noesis-roster.yaml"
AGENTS_DIR = REPO_ROOT / "agents"
AGENT_REGISTRY_PATH = REPO_ROOT / "platform" / "agent-registry.yaml"

# Tiers that may never be handed executing work by the orchestrator.
REVIEWER_ONLY_TIERS = {"reviewer-only"}

# Profiles that supervise rather than execute. The orchestrator must not assign
# execution work to itself or to another supervisor.
SUPERVISOR_TIERS = {"supervisor"}

# Registry lane roles treated as supervising (cannot be assigned execution).
SUPERVISOR_ROLES = {"supervisor"}


@dataclass(frozen=True)
class ProfileRecord:
    """A roster profile or registry lane joined with its declared capabilities."""

    name: str
    display_name: str
    role: str
    tier: str
    toolsets: tuple[str, ...] = ()
    capabilities: frozenset[str] = field(default_factory=frozenset)
    status: str = "applied"
    activation_state: str = "activated"  # registered | staged | activated
    source: str = "roster"               # roster | agent-registry

    @property
    def is_reviewer_only(self) -> bool:
        return self.tier in REVIEWER_ONLY_TIERS

    @property
    def is_supervisor(self) -> bool:
        return self.tier in SUPERVISOR_TIERS

    @property
    def can_execute(self) -> bool:
        """True when this profile may be assigned executing work."""
        return not (self.is_reviewer_only or self.is_supervisor)

    @property
    def has_terminal(self) -> bool:
        return "terminal" in self.toolsets or "code_execution" in self.toolsets


def _load_capabilities(profile_name: str) -> frozenset[str]:
    """Read declared capabilities from agents/<profile>/agent.yaml, if present."""
    agent_file = AGENTS_DIR / profile_name / "agent.yaml"
    if not agent_file.is_file():
        return frozenset()
    data = yaml.safe_load(agent_file.read_text(encoding="utf-8")) or {}
    caps = (data.get("agent") or {}).get("capabilities") or []
    return frozenset(str(c) for c in caps)


@functools.lru_cache(maxsize=1)
def load_registry() -> tuple[dict[str, ProfileRecord], dict[str, str]]:
    """Load and cache roster + registry lanes. Returns (profiles, aliases).

    Cache is process-local; tests may clear it. Fails closed (raises) on
    identity conflicts: lane/roster name collisions, alias shadowing, or
    aliases that do not resolve to exactly one canonical identity.
    """
    raw = yaml.safe_load(ROSTER_PATH.read_text(encoding="utf-8")) or {}
    profiles_raw = raw.get("profiles") or {}
    registry: dict[str, ProfileRecord] = {}
    for name, spec in profiles_raw.items():
        spec = spec or {}
        wave = spec.get("wave", 1)
        registry[name] = ProfileRecord(
            name=name,
            display_name=str(spec.get("name", name)),
            role=str(spec.get("role", "")),
            tier=str(spec.get("tier", "")),
            toolsets=tuple(spec.get("toolsets") or ()),
            capabilities=_load_capabilities(name),
            status=str(spec.get("status", "applied")),
            activation_state="activated" if wave == 1 else "staged",
            source="roster",
        )

    reg_raw = yaml.safe_load(AGENT_REGISTRY_PATH.read_text(encoding="utf-8")) or {}
    lanes = list(reg_raw.get("orchestration_lanes") or [])
    agents_raw = reg_raw.get("agents") or {}
    for lane in lanes:
        spec = agents_raw.get(lane)
        if spec is None:
            raise ValueError(
                f"orchestration lane '{lane}' has no entry in platform/agent-registry.yaml"
            )
        if lane in registry:
            raise ValueError(
                f"identity conflict: '{lane}' is defined in both the roster and "
                f"agent-registry orchestration_lanes"
            )
        role = str(spec.get("role", ""))
        registry[lane] = ProfileRecord(
            name=lane,
            display_name=str(spec.get("agent_id", lane)),
            role=role,
            tier="supervisor" if role in SUPERVISOR_ROLES else "worker",
            capabilities=frozenset(
                str(c) for c in (spec.get("capabilities") or ())
            ).union(_load_capabilities(lane)),
            status="enabled" if spec.get("enabled") else "disabled",
            activation_state="activated" if spec.get("enabled") else "registered",
            source="agent-registry",
        )

    for name, record in agency_registry.load_agency_registry().items():
        if name in registry:
            raise ValueError(
                f"identity conflict: '{name}' is defined in both the roster and "
                f"agency/curation.yaml"
            )
        registry[name] = record

    aliases: dict[str, str] = {}
    for alias, target in (reg_raw.get("aliases") or {}).items():
        if alias in registry:
            raise ValueError(f"alias '{alias}' shadows an existing identity")
        if target not in registry:
            raise ValueError(f"alias '{alias}' resolves to unknown identity '{target}'")
        aliases[str(alias)] = str(target)

    return registry, aliases


def _profiles() -> dict[str, ProfileRecord]:
    return load_registry()[0]


def _aliases() -> dict[str, str]:
    return load_registry()[1]


def reset_registry_cache() -> None:
    load_registry.cache_clear()


def get_profile(name: str) -> ProfileRecord | None:
    profiles = _profiles()
    record = profiles.get(name)
    if record is None and name in _aliases():
        record = profiles.get(_aliases()[name])
    return record


def profile_exists(name: str) -> bool:
    return get_profile(name) is not None


def list_profiles() -> list[ProfileRecord]:
    return list(_profiles().values())


def executor_profiles() -> list[ProfileRecord]:
    return [p for p in _profiles().values() if p.can_execute]
