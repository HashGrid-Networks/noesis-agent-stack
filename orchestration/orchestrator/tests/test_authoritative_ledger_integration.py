"""Phase 1: authoritative TaskStore <-> delegation lifecycle integration.

Proves the actual runtime path persists, through ONE authoritative ledger
(tasks.jsonl):

    validated plan -> immutable task contract -> assignment/attempt ->
    lease/budget -> launch -> candidate -> revision-bound reviews ->
    explicit orchestrator acceptance

and that the DelegationStore file is a derived event-log projection the control
plane writes — never a second independent source of truth. Backend success is
never acceptance; crash/restart and duplicate/stale events are handled without
duplicating work. Uses the real Orchestrator/TaskStore persistence (tmp_path)
with a mocked launcher only at the process-spawn boundary.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.control_plane import Orchestrator, PolicyViolation
from app.models import Verification, utcnow

RESEARCH = (
    "Research market pricing and return a cited brief with at least three sources."
)


def _handoff() -> dict:
    return {
        "summary": "Implemented and verified the function with full test coverage.",
        "artifacts": [{"path": "validate_port.py", "checksum": None, "kind": "source"}],
        "verification_result": {"passed": True, "evidence": "pytest -q: 11 passed."},
        "unmet_criteria": [],
    }


class FakeAdapter:
    """Launcher double: only replaces the process-spawn boundary."""

    def __init__(self, loaded_profile: str):
        self._p = loaded_profile

    def launch(self, *, profile_id, prompt, task_id, cwd=None, dry_run=True):
        return SimpleNamespace(
            ok=True,
            loaded_profile=self._p,
            error=None,
            requested_profile=profile_id,
            dry_run=dry_run,
            verification_pending=False,
            artifacts=(),
            stdout_digest="",
            collection_error=None,
            attempt_id="1",
            contract_revision="rev-1",
            assignment_epoch=1,
            baseline=None,
        )


def propose_coding(orch, *, reviewer="noesis-sentinel", idem="forge:implement:validate_port:1"):
    return orch.propose(
        title="Implement validate_port",
        intent="Implement a deterministic port-range validation function with tests.",
        assignee_profile="noesis-forge",
        required_capability="code_modify",
        acceptance_criteria=["Return (True, None) in [1,65535] excluding bool"],
        verification=Verification(method="automated_test"),
        idempotency_key=idem,
        risk_tier="r1",
        reviewer_profile=reviewer,
        timeout_s=900,
    )


def dispatched_coding(orch, **kw):
    task = propose_coding(orch, **kw)
    return orch.mark_dispatched(task.task_id, runtime_profile="noesis-forge")


def review_and_accept(
    orch,
    task,
    *,
    candidate="rev-1",
    spec_verdict="PASS",
    quality_verdict="APPROVED",
    quality_findings=None,
):
    orch.submit_handoff(task.task_id, _handoff())
    orch.submit_candidate(
        task.task_id, candidate_revision=candidate, candidate_digest="sha256:" + candidate
    )
    orch.record_review(
        task.task_id,
        stage="spec",
        verdict=spec_verdict,
        reviewer="noesis-sentinel",
        candidate_revision=candidate,
    )
    orch.record_review(
        task.task_id,
        stage="quality",
        verdict=quality_verdict,
        reviewer="noesis-skeptic",
        candidate_revision=candidate,
        findings=quality_findings or [{"severity": "info"}],
    )
    return orch.accept(task.task_id)


class TestTaskStoreIsAuthoritative:
    def test_router_dispatch_advances_authoritative_lifecycle(self, orch):
        orch = Orchestrator(orch.store.ledger_path)
        orch.runtime_adapter = FakeAdapter("noesis-signal")
        res = orch.route_and_launch(
            title="Research pricing",
            intent=RESEARCH,
            acceptance_criteria=["At least three sources cited"],
            verification=Verification(method="reviewer_signoff"),
            idempotency_key="research:pricing:2026",
            dry_run=False,
        )
        task = orch.store.get(res.task.task_id)
        # The durable contract is advanced to an explicit running assignment.
        assert task.state == "running"
        assert task.delegation.runtime_profile == "noesis-signal"
        assert task.delegation.assignment_epoch >= 1
        assert task.delegation.attempt == 1
        assert task.claim is not None and not task.lease_expired()
        # Backend/launch success is NOT acceptance.
        assert task.state != "succeeded"
        # Reload proves durability across a restart boundary.
        orch2 = Orchestrator(orch.store.ledger_path)
        t2 = orch2.store.get(res.task.task_id)
        assert t2.state == "running"
        assert t2.delegation.runtime_profile == "noesis-signal"

    def test_dry_run_attests_only_and_leaves_proposed(self, orch):
        orch = Orchestrator(orch.store.ledger_path)
        orch.runtime_adapter = FakeAdapter("noesis-signal")
        res = orch.route_and_launch(
            title="Research pricing",
            intent=RESEARCH,
            acceptance_criteria=["At least three sources cited"],
            verification=Verification(method="reviewer_signoff"),
            idempotency_key="research:pricing:dry",
            dry_run=True,
        )
        task = orch.store.get(res.task.task_id)
        assert task.state == "proposed"  # attested, not executed
        assert task.delegation.assignment_epoch == 0

    def test_duplicate_dispatch_does_not_duplicate_contract(self, orch):
        orch = Orchestrator(orch.store.ledger_path)
        orch.runtime_adapter = FakeAdapter("noesis-signal")
        key = "research:pricing:dup"
        r1 = orch.route_and_launch(
            title="Research pricing", intent=RESEARCH,
            acceptance_criteria=["At least three sources cited"],
            verification=Verification(method="reviewer_signoff"),
            idempotency_key=key, dry_run=False,
        )
        r2 = orch.route_and_launch(
            title="Research pricing", intent=RESEARCH,
            acceptance_criteria=["At least three sources cited"],
            verification=Verification(method="reviewer_signoff"),
            idempotency_key=key, dry_run=False,
        )
        assert r1.task.task_id == r2.task.task_id
        matching = [t for t in orch.store.all_tasks().values() if t.idempotency_key == key]
        assert len(matching) == 1  # never a duplicate authoritative contract


class TestDelegationLifecycleOnAuthoritativeLedger:
    def test_review_gated_acceptance_survives_restart(self, orch):
        orch = Orchestrator(orch.store.ledger_path)
        task = dispatched_coding(orch)
        done = review_and_accept(orch, task)
        assert done.state == "succeeded"
        assert done.delegation.accepted_by == "noesis-orchestrator"
        assert len(done.delegation.reviews) == 2
        orch2 = Orchestrator(orch.store.ledger_path)
        t2 = orch2.store.get(task.task_id)
        assert t2.state == "succeeded"
        assert t2.delegation.accepted_by == "noesis-orchestrator"
        assert len(t2.delegation.reviews) == 2

    def test_accept_requires_evidence_then_candidate_then_reviews(self, orch):
        orch = Orchestrator(orch.store.ledger_path)
        task = dispatched_coding(orch)
        with pytest.raises(PolicyViolation, match="evidence_required"):
            orch.accept(task.task_id)
        orch.submit_handoff(task.task_id, _handoff())
        with pytest.raises(PolicyViolation, match="candidate_required"):
            orch.accept(task.task_id)
        orch.submit_candidate(task.task_id, candidate_revision="rev-1")
        with pytest.raises(PolicyViolation, match="spec_review_required"):
            orch.accept(task.task_id)
        orch.record_review(task.task_id, stage="spec", verdict="PASS",
                           reviewer="noesis-sentinel", candidate_revision="rev-1")
        with pytest.raises(PolicyViolation, match="quality_review_required"):
            orch.accept(task.task_id)

    def test_quality_review_cannot_precede_spec_pass(self, orch):
        orch = Orchestrator(orch.store.ledger_path)
        task = dispatched_coding(orch)
        orch.submit_handoff(task.task_id, _handoff())
        orch.submit_candidate(task.task_id, candidate_revision="rev-1")
        with pytest.raises(PolicyViolation, match="spec_required"):
            orch.record_review(task.task_id, stage="quality", verdict="APPROVED",
                               reviewer="noesis-skeptic", candidate_revision="rev-1")

    def test_stale_candidate_review_rejected(self, orch):
        orch = Orchestrator(orch.store.ledger_path)
        task = dispatched_coding(orch)
        orch.submit_handoff(task.task_id, _handoff())
        orch.submit_candidate(task.task_id, candidate_revision="rev-1")
        with pytest.raises(PolicyViolation, match="stale_candidate"):
            orch.record_review(task.task_id, stage="spec", verdict="PASS",
                               reviewer="noesis-sentinel", candidate_revision="rev-2")

    def test_duplicate_review_is_idempotent(self, orch):
        orch = Orchestrator(orch.store.ledger_path)
        task = dispatched_coding(orch)
        orch.submit_handoff(task.task_id, _handoff())
        orch.submit_candidate(task.task_id, candidate_revision="rev-1")
        orch.record_review(task.task_id, stage="spec", verdict="PASS",
                           reviewer="noesis-sentinel", candidate_revision="rev-1")
        orch.record_review(task.task_id, stage="spec", verdict="PASS",
                           reviewer="noesis-sentinel", candidate_revision="rev-1")
        task = orch.store.get(task.task_id)
        assert len(task.delegation.current_reviews("spec")) == 1

    def test_candidate_change_invalidates_prior_reviews(self, orch):
        orch = Orchestrator(orch.store.ledger_path)
        task = dispatched_coding(orch)
        review_and_accept(orch, task, candidate="rev-1")
        assert orch.store.get(task.task_id).state == "succeeded"
        # A materially new contract must go through re-review; a fresh running
        # task pushed to a new candidate cannot carry forward the old verdicts.
        task2 = dispatched_coding(orch, idem="forge:implement:validate_port:2")
        orch.submit_handoff(task2.task_id, _handoff())
        orch.submit_candidate(task2.task_id, candidate_revision="a")
        orch.record_review(task2.task_id, stage="spec", verdict="PASS",
                           reviewer="noesis-sentinel", candidate_revision="a")
        orch.record_review(task2.task_id, stage="quality", verdict="APPROVED",
                           reviewer="noesis-skeptic", candidate_revision="a")
        # Remediation changes the candidate while in awaiting_review.
        orch.submit_candidate(task2.task_id, candidate_revision="b")
        task2 = orch.store.get(task2.task_id)
        assert task2.delegation.candidate_revision == "b"
        assert task2.delegation.current_reviews("spec") == []
        assert task2.delegation.current_reviews("quality") == []
        with pytest.raises(PolicyViolation, match="spec_review_required"):
            orch.accept(task2.task_id)

    def test_open_critical_finding_blocks_acceptance(self, orch):
        orch = Orchestrator(orch.store.ledger_path)
        task = dispatched_coding(orch)
        orch.submit_handoff(task.task_id, _handoff())
        orch.submit_candidate(task.task_id, candidate_revision="rev-1")
        orch.record_review(task.task_id, stage="spec", verdict="PASS",
                           reviewer="noesis-sentinel", candidate_revision="rev-1",
                           findings=[{"severity": "critical", "location": "validate_port.py"}])
        orch.record_review(task.task_id, stage="quality", verdict="APPROVED",
                           reviewer="noesis-skeptic", candidate_revision="rev-1")
        with pytest.raises(PolicyViolation, match="open_findings"):
            orch.accept(task.task_id)

    def test_budget_and_unknown_usage_not_zero(self, orch):
        orch = Orchestrator(orch.store.ledger_path)
        task = propose_coding(orch, idem="forge:budget:1")
        orch.mark_dispatched(task.task_id, runtime_profile="noesis-forge",
                             request_limit=2, budget_tokens=1000)
        task = orch.observe_budget(task.task_id, requests=2, tokens=None)
        assert task.delegation.tokens_used is None  # unknown stays None, never 0
        orch.observe_budget(task.task_id, requests=1)
        with pytest.raises(PolicyViolation, match="budget_exceeded"):
            orch.accept(task.task_id)

    def test_accept_after_claim_lease_expiry_when_execution_released(self, orch):
        orch = Orchestrator(orch.store.ledger_path)
        task = dispatched_coding(orch)
        orch.submit_handoff(task.task_id, _handoff())
        orch.submit_candidate(task.task_id, candidate_revision="rev-1")
        from datetime import timedelta

        live = orch.store.get(task.task_id)
        claim = dict(live.claim)
        claim["lease_expires_at"] = live.claim["lease_expires_at"] - timedelta(hours=2)
        live.claim = claim
        orch.store.put(live, event="lease_forced_expired")
        orch.record_review(task.task_id, stage="spec", verdict="PASS",
                           reviewer="noesis-sentinel", candidate_revision="rev-1")
        orch.record_review(task.task_id, stage="quality", verdict="APPROVED",
                           reviewer="noesis-skeptic", candidate_revision="rev-1")
        done = orch.accept(task.task_id)
        assert done.state == "succeeded"


class TestDelegationProjectionAndPluginPath:
    def test_projection_is_derived_event_log_not_authority(self, orch, tmp_path):
        orch = Orchestrator(orch.store.ledger_path)
        task = dispatched_coding(orch)
        review_and_accept(orch, task)
        proj = orch.emit_delegation_projection(tmp_path / "delegated-tasks.jsonl")
        assert proj and all(r["protocol_version"] == "noesis.delegated-task/v1" for r in proj)
        lines = (tmp_path / "delegated-tasks.jsonl").read_text().splitlines()
        assert len(lines) >= 1
        # Mutating the projection never affects the authoritative task.
        (tmp_path / "delegated-tasks.jsonl").write_text(json.dumps({"state": "tampered"}) + "\n")
        assert orch.store.get(task.task_id).state == "succeeded"

    def test_plugin_dispatch_uses_authoritative_lifecycle(self, tmp_path):
        from app.specialist_dispatch_cli import _make_orchestrator

        ledger = tmp_path / "noesis-specialist-plugin-ledger.jsonl"
        events = tmp_path / "noesis-specialist-plugin-events.jsonl"
        payload = {
            "title": "Research pricing",
            "intent": RESEARCH,
            "acceptance_criteria": ["At least three sources cited"],
            "verification": {"method": "reviewer_signoff"},
            "idempotency_key": "plugin:research:pricing:2026",
            "required_capability": None,
            "risk_tier": "r0",
            "dry_run": False,
            "allow_generic_fallback": False,
        }
        orch = _make_orchestrator(payload, ledger, events)
        orch.runtime_adapter = FakeAdapter("noesis-signal")
        res = orch.route_and_launch(
            title=payload["title"], intent=payload["intent"],
            acceptance_criteria=payload["acceptance_criteria"],
            verification=Verification(method="reviewer_signoff"),
            idempotency_key=payload["idempotency_key"],
            required_capability=payload.get("required_capability"),
            risk_tier=payload["risk_tier"], dry_run=payload["dry_run"],
            allow_generic_fallback=payload["allow_generic_fallback"],
        )
        # The plugin entrypoint writes the same authoritative ledger.
        assert orch.store.ledger_path == ledger
        task = orch.store.get(res.task.task_id)
        assert task.state == "running"
        assert task.delegation.runtime_profile == "noesis-signal"
        # Backend success (launch ok) did not set succeeded.
        assert task.state != "succeeded"
        # Ledger is the durable append-only record used for restart replay.
        assert "task_proposed" in ledger.read_text()
