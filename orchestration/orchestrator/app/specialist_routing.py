"""Specialist discovery, task classification, and profile selection.

This module reconciles the authoritative Noesis roster and agent contracts with
what the runtime can actually launch. It intentionally keeps agent selection
separate from model/provider selection: the orchestrator first chooses a
specialist profile, then the existing inference-routing gate evaluates that
profile's lane/model policy before dispatch.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

import yaml

from app import registry
from app.launcher import HermesCliAdapter, LaunchResult, RuntimeAdapter
from app.models import TaskContract, Verification

REPO_ROOT = Path(__file__).resolve().parents[3]
AGENTS_DIR = REPO_ROOT / "agents"
LIVE_PROFILES_DIR = REPO_ROOT / "profiles" / "live"
GENERIC_PROFILES = {"default", "coder", "claude-code-worker"}
ORDINARY_WORKER_BLOCKLIST = {"noesis-orchestrator", "main-hermes"}


@dataclass(frozen=True)
class Classification:
    task_class: str
    required_capability: str
    reviewer_required: str | None = None


@dataclass(frozen=True)
class SpecialistRecord:
    profile_id: str
    runtime_profile: str
    aliases: tuple[str, ...]
    runtime: str
    launch_mechanism: str
    role: str
    tier: str
    responsibilities: str
    capabilities: tuple[str, ...]
    task_classes: tuple[str, ...]
    toolsets: tuple[str, ...]
    data_boundaries: tuple[str, ...]
    model_provider: str | None
    model_id: str | None
    delegates_to: tuple[str, ...]
    may_delegate: bool
    defined: bool
    installed: bool
    discoverable: bool
    launchable: bool
    policy_eligible: bool
    eligibility_reasons: tuple[str, ...]


@dataclass(frozen=True)
class CandidateDecision:
    profile_id: str
    eligible: bool
    score: int
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class SelectionDecision:
    classification: Classification
    selected: SpecialistRecord | None
    candidates: tuple[CandidateDecision, ...]
    reason: str
    fallback_reason: str | None = None


@dataclass(frozen=True)
class SpecialistRouteResult:
    blocked: bool
    selection: SelectionDecision
    task: TaskContract | None
    launch: LaunchResult | None
    fallback_reason: str | None = None


@dataclass(frozen=True)
class GraphNode:
    intent: str
    classification: Classification
    selected_profile: str


@dataclass(frozen=True)
class SpecialistGraph:
    nodes: tuple[GraphNode, ...]
    bounded: bool
    max_fanout: int


class RoutingBlocked(RuntimeError):
    def __init__(self, code: str, reason: str, *, rejections: dict[str, list[str]] | None = None) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason
        self.rejections = rejections or {}


# Ordered from specific to broad. The first keyword hit determines the default
# task class; capability filtering still decides final eligibility.
TASK_RULES: tuple[tuple[str, tuple[str, ...], str, str, str | None], ...] = (
    ("agent_architecture", ("mcp", "hermes", "agent", "profile", "prompt", "routing architecture"), "profile_spec", "noesis-architect", None),
    ("review", ("review", "qa", "security", "audit", "verify"), "code_review", "noesis-sentinel", None),
    ("implementation", ("implement", "patch", "code", "test", "ci/cd", "pipeline", "python", "javascript", "typescript"), "code_modify", "noesis-forge", "noesis-sentinel"),
    ("operations", ("docker", "kubernetes", "k8s", "terraform", "ansible", "deploy", "observability", "incident", "health"), "observability", "noesis-substrate", "noesis-sentinel"),
    ("archival", ("archive", "record preservation", "knowledge base", "kb_struct", "citation_preserve", "cross_link", "index_maintain"), "provenance_archive", "noesis-scribe", None),
    ("osint", ("osint", "timeline", "provenance", "public-source"), "timeline_construct", "noesis-tracer", "noesis-skeptic"),
    ("crypto", ("wallet", "on-chain", "onchain", "crypto", "protocol", "mining"), "protocol_research", "noesis-ledger", "noesis-skeptic"),
    ("advocacy", ("legal", "advocacy", "filing", "administrative"), "legal_research_support", "noesis-advocate", "noesis-skeptic"),
    ("research", ("research", "sources", "cited", "market", "brief", "evidence"), "cited_brief", "noesis-signal", "noesis-skeptic"),
    ("data", ("csv", "spreadsheet", "chart", "analyze data", "dataset"), "structured_analysis", "noesis-grid", "noesis-skeptic"),
    ("writing", ("write", "runbook", "docs", "documentation", "changelog", "technical writing"), "technical_writing", "noesis-quill", None),
    ("comms", ("comms", "customer", "external", "message", "announce"), "comms_draft", "noesis-herald", "noesis-skeptic"),
    ("planning", ("plan", "dependency", "decompose", "roadmap", "phase"), "plan_artifact", "noesis-cartographer", "noesis-skeptic"),
    ("stewardship", ("triage", "priorities", "status digest", "backlog", "resource allocation"), "priority_triage", "noesis-steward", None),
)

ROUTING_FIXTURES = {
    "noesis-steward": "triage priorities and produce a status digest",
    "noesis-cartographer": "create a dependency map and phased plan",
    "noesis-forge": "implement the approved Python patch and tests",
    "noesis-scribe": "archive the knowledge base with citation preservation",
    "noesis-signal": "research sources and produce a cited brief",
    "noesis-substrate": "diagnose Docker deployment health and observability",
    "noesis-tracer": "build a public-source OSINT timeline with provenance",
    "noesis-ledger": "research a wallet protocol and on-chain evidence",
    "noesis-grid": "analyze this CSV and produce charts",
    "noesis-quill": "write the operator runbook and changelog",
    "noesis-advocate": "prepare legal advocacy research support",
    "noesis-herald": "draft external customer comms but do not send",
    "noesis-architect": "design an MCP/Hermes profile contract",
    "noesis-sentinel": "review the patch independently for security",
    "noesis-skeptic": "perform adversarial review of assumptions",
}


class TaskClassifier:
    def classify(self, intent: str, required_capability: str | None = None) -> Classification:
        text = intent.lower()
        if "noesis-orchestrator" in text and ("clone" in text or "another" in text):
            return Classification("self_orchestration", required_capability or "dispatch_task")
        for task_class, keywords, capability, _preferred, reviewer in TASK_RULES:
            if any(keyword in text for keyword in keywords):
                return Classification(task_class, required_capability or capability, reviewer)
        return Classification("general", required_capability or "plan_artifact")


@dataclass
class CapabilityIndex:
    records: dict[str, SpecialistRecord]
    classifier: TaskClassifier = field(default_factory=TaskClassifier)

    @classmethod
    def from_repo(cls, *, adapter: RuntimeAdapter | None = None) -> "CapabilityIndex":
        adapter = adapter or HermesCliAdapter()
        records: dict[str, SpecialistRecord] = {}
        for profile in registry.list_profiles():
            records[profile.name] = _build_record(profile, adapter)
        return cls(records)

    def record(self, profile_id: str) -> SpecialistRecord | None:
        return self.records.get(profile_id)

    def select(
        self,
        intent: str,
        *,
        required_capability: str | None = None,
        risk_tier: str = "r0",
        allow_generic_fallback: bool = False,
        exclude_profiles: set[str] | None = None,
    ) -> SelectionDecision:
        classification = self.classifier.classify(intent, required_capability)
        exclude_profiles = set(exclude_profiles or set())
        candidates: list[CandidateDecision] = []
        for rec in self.records.values():
            score, reasons = self._score(rec, classification, risk_tier, exclude_profiles, allow_generic_fallback)
            candidates.append(CandidateDecision(rec.profile_id, not reasons, score, tuple(reasons)))
        eligible = [c for c in candidates if c.eligible]
        eligible.sort(key=lambda c: c.score, reverse=True)
        selected = self.records[eligible[0].profile_id] if eligible else None
        if selected is None:
            return SelectionDecision(
                classification,
                None,
                tuple(sorted(candidates, key=lambda c: c.score, reverse=True)),
                "no eligible specialist found",
            )
        fallback_reason = None
        if selected.profile_id in GENERIC_PROFILES:
            fallback_reason = "generic fallback explicitly allowed; no specialist launchable"
        return SelectionDecision(
            classification,
            selected,
            tuple(sorted(candidates, key=lambda c: c.score, reverse=True)),
            f"selected {selected.profile_id} for {classification.task_class}/{classification.required_capability}",
            fallback_reason,
        )

    def _score(
        self,
        rec: SpecialistRecord,
        classification: Classification,
        risk_tier: str,
        exclude_profiles: set[str],
        allow_generic_fallback: bool,
    ) -> tuple[int, list[str]]:
        reasons = list(rec.eligibility_reasons)
        if allow_generic_fallback and rec.profile_id in GENERIC_PROFILES:
            reasons = [reason for reason in reasons if reason != "policy_ineligible"]
        if rec.profile_id in exclude_profiles:
            reasons.append("cycle_or_duplicate_profile")
        if rec.profile_id in ORDINARY_WORKER_BLOCKLIST:
            reasons.append("supervisor_not_ordinary_worker")
        if rec.profile_id in GENERIC_PROFILES and not allow_generic_fallback:
            reasons.append("generic_fallback_not_allowed")
        if not rec.policy_eligible and not (allow_generic_fallback and rec.profile_id in GENERIC_PROFILES):
            reasons.append("policy_ineligible")
        if not rec.launchable:
            reasons.append("not_launchable")
        if classification.task_class == "review":
            if rec.profile_id not in {"noesis-sentinel", "noesis-skeptic"}:
                reasons.append("not_independent_reviewer")
        elif rec.tier == "reviewer-only":
            reasons.append("reviewer_only_not_executor")
        if classification.required_capability not in rec.capabilities:
            if not (
                allow_generic_fallback
                and rec.profile_id == "coder"
                and classification.required_capability == "code_modify"
                and "implement" in rec.capabilities
            ):
                reasons.append(f"capability_mismatch:{classification.required_capability}")
        if risk_tier == "r3" and "terminal" not in rec.toolsets and "code_execution" not in rec.toolsets:
            reasons.append("r3_executor_lacks_terminal")
        score = 0
        if classification.required_capability in rec.capabilities:
            score += 100
        elif allow_generic_fallback and rec.profile_id == "coder" and classification.required_capability == "code_modify" and "implement" in rec.capabilities:
            score += 80
        if classification.task_class in rec.task_classes:
            score += 50
        if rec.profile_id not in GENERIC_PROFILES:
            score += 20
        if rec.profile_id in _preferred_profiles(classification.task_class):
            score += 30
        return score, reasons

    def coverage(self) -> list[dict[str, object]]:
        rows = []
        for rec in sorted(self.records.values(), key=lambda r: r.profile_id):
            rows.append({
                "profile_id": rec.profile_id,
                "defined": rec.defined,
                "installed": rec.installed,
                "discoverable": rec.discoverable,
                "launchable": rec.launchable,
                "policy_eligible": rec.policy_eligible,
                "has_fixture": rec.profile_id in ROUTING_FIXTURES,
                "reasons": list(rec.eligibility_reasons),
            })
        return rows


def _preferred_profiles(task_class: str) -> set[str]:
    return {preferred for cls, _kw, _cap, preferred, _rev in TASK_RULES if cls == task_class}


def _load_agent_yaml(profile_id: str) -> dict:
    path = AGENTS_DIR / profile_id / "agent.yaml"
    if not path.is_file():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _build_record(profile: registry.ProfileRecord, adapter: RuntimeAdapter) -> SpecialistRecord:
    raw = _load_agent_yaml(profile.name)
    agent = raw.get("agent") or {}
    hermes_profile = agent.get("hermes_profile") or {}
    collaboration = agent.get("collaboration") or {}
    runtime = str(agent.get("runtime") or "hermes")
    runtime_profile = str(hermes_profile.get("profile_name") or profile.name)
    installed = adapter.profile_installed(runtime_profile)
    defined = bool(agent) or profile.source in {"roster", "agent-registry"}
    discoverable = defined and bool(agent.get("enabled", True))
    launchable = discoverable and runtime == "hermes" and installed
    reasons: list[str] = []
    if not defined:
        reasons.append("not_defined")
    if not discoverable:
        reasons.append("not_discoverable")
    if runtime != "hermes":
        reasons.append(f"unsupported_runtime:{runtime}")
    if not installed:
        reasons.append("profile_not_installed")
    if profile.is_supervisor:
        reasons.append("supervisor_not_worker")
    # reviewer-only profiles are policy-eligible only for review classes; selection
    # adds that task-specific distinction, so do not mark them globally ineligible.
    policy_eligible = launchable and not profile.is_supervisor and profile.name not in GENERIC_PROFILES
    if not policy_eligible:
        reasons.append("policy_ineligible")
    capabilities = tuple(sorted(set(profile.capabilities) | set(agent.get("capabilities") or ())))
    return SpecialistRecord(
        profile_id=profile.name,
        runtime_profile=runtime_profile,
        aliases=(),
        runtime=runtime,
        launch_mechanism="hermes-cli -p" if runtime == "hermes" else runtime,
        role=profile.role,
        tier=profile.tier,
        responsibilities=str(profile.display_name or profile.role),
        capabilities=capabilities,
        task_classes=tuple(_task_classes_for(profile.name, capabilities)),
        toolsets=profile.toolsets,
        data_boundaries=tuple(str(x) for x in (agent.get("guardrails") or ())),
        model_provider=(hermes_profile.get("model") or {}).get("provider"),
        model_id=(hermes_profile.get("model") or {}).get("default"),
        delegates_to=tuple(collaboration.get("delegates_to") or ()),
        may_delegate=bool(collaboration.get("delegates_to")),
        defined=defined,
        installed=installed,
        discoverable=discoverable,
        launchable=launchable,
        policy_eligible=policy_eligible,
        eligibility_reasons=tuple(dict.fromkeys(reasons)),
    )


def _task_classes_for(profile_id: str, capabilities: tuple[str, ...]) -> list[str]:
    classes = []
    for task_class, _keywords, capability, preferred, _reviewer in TASK_RULES:
        if capability in capabilities or preferred == profile_id:
            classes.append(task_class)
    if profile_id == "noesis-steward":
        classes.append("triage")
    if profile_id == "noesis-scribe":
        classes.append("knowledge")
    if profile_id == "coder":
        classes.append("implementation")
    return classes


def selection_rejections(selection: SelectionDecision) -> dict[str, list[str]]:
    return {c.profile_id: list(c.reasons) for c in selection.candidates if c.reasons}


def make_child_prompt(task: TaskContract, selection: SelectionDecision) -> str:
    return json.dumps(
        {
            "contract_version": "specialist-dispatch/v1",
            "task_id": str(task.task_id),
            "assignee_profile": task.assignee_profile,
            "task_class": selection.classification.task_class,
            "required_capability": task.required_capability,
            "intent": task.intent,
            "acceptance_criteria": task.acceptance_criteria,
            "verification": task.verification.method,
            "risk_tier": task.risk_tier,
            "parent_task_id": str(task.parent_task_id) if task.parent_task_id else None,
            "instructions": "Load only your specialist profile context, do the bounded task, and return structured handoff evidence.",
        },
        sort_keys=True,
    )


def emit_routing_event(path: Path | None, record: dict[str, object]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **record}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, sort_keys=True) + "\n")


def depth_exceeded(parent_task_id: UUID | str | None, max_depth: int) -> bool:
    return parent_task_id is not None and max_depth <= 0
