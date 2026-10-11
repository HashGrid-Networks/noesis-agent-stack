"""Track A regression: authoritative execution identity, decided deliberately.

Pins the behavior required alongside the schema fix in
contracts/orchestration/task-contract.schema.json:

- A task that has never been claimed (proposed/approved/queued) may
  legitimately carry no contract_revision/baseline (not-yet-assigned,
  never a fabricated value).
- The moment a task is first claimed, contract_revision/baseline are
  derived authoritatively (app.models.TaskContract.ensure_execution_identity)
  — never caller-supplied, never left missing.
- A derivation that disagrees with an already-persisted identity (a stale
  or mismatched contract/baseline) blocks with a typed error; it is never
  silently overwritten and never silently passed through.
- route_and_launch's non-dry-run spawn path refuses to launch when identity
  is absent (defense in depth against any future bypass of apply_claim).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.control_plane import Orchestrator
from app.models import IdentityError
from app.specialist_routing import RoutingBlocked
from tests.conftest import make_task


class FakeAdapter:
    """Launcher double: only replaces the process-spawn boundary."""

    def __init__(self, loaded_profile: str):
        self._p = loaded_profile

    def launch(self, *, profile_id, prompt, task_id, cwd=None, dry_run=True, **_kwargs):
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
            contract_revision=None,
            assignment_epoch=1,
            baseline=None,
            candidate_digest=None,
            disposition=None,
            usage_tokens=None,
            process_id=None,
            provider="approved-provider",
            model="approved-model",
        )


class TestIdentityDeliberatelyMissingBeforeClaim:
    def test_proposed_task_has_no_identity_yet(self, orch):
        task = make_task(orch)
        assert task.state == "proposed"
        assert task.delegation.contract_revision is None
        assert task.delegation.baseline is None

    def test_queued_task_still_has_no_identity(self, orch):
        task = make_task(orch, idempotency_key="identity:queued:1")
        orch.enqueue(task.task_id)
        queued = orch.store.get(task.task_id)
        assert queued.state == "queued"
        assert queued.delegation.contract_revision is None
        assert queued.delegation.baseline is None


class TestIdentityDerivedAuthoritativelyOnClaim:
    def test_claim_derives_non_null_identity(self, orch):
        task = make_task(orch, idempotency_key="identity:claim:1")
        orch.enqueue(task.task_id)
        claimed = orch.claim(task.task_id, claimed_by=task.assignee_profile)
        assert claimed.state == "claimed"
        assert isinstance(claimed.delegation.contract_revision, str) and claimed.delegation.contract_revision
        assert isinstance(claimed.delegation.baseline, str) and claimed.delegation.baseline

    def test_identity_is_derived_not_caller_supplied(self, orch):
        """Two distinct tasks derive distinct contract_revision; nothing in
        the public claim path accepts an identity argument from the caller."""
        t1 = make_task(orch, idempotency_key="identity:derive:1")
        t2 = make_task(orch, idempotency_key="identity:derive:2", title="A distinct task title")
        orch.enqueue(t1.task_id)
        orch.enqueue(t2.task_id)
        c1 = orch.claim(t1.task_id, claimed_by=t1.assignee_profile)
        c2 = orch.claim(t2.task_id, claimed_by=t2.assignee_profile)
        assert c1.delegation.contract_revision != c2.delegation.contract_revision
        # Same control-plane code baseline for both, derived identically.
        assert c1.delegation.baseline == c2.delegation.baseline

    def test_identity_persists_across_restart(self, orch):
        task = make_task(orch, idempotency_key="identity:restart:1")
        orch.enqueue(task.task_id)
        claimed = orch.claim(task.task_id, claimed_by=task.assignee_profile)
        orch2 = Orchestrator(orch.store.ledger_path)
        reloaded = orch2.store.get(task.task_id)
        assert reloaded.delegation.contract_revision == claimed.delegation.contract_revision
        assert reloaded.delegation.baseline == claimed.delegation.baseline

    def test_identity_stable_across_retry_reclaim_same_process(self, orch):
        task = make_task(orch, idempotency_key="identity:retry:1")
        orch.enqueue(task.task_id)
        first = orch.claim(task.task_id, claimed_by=task.assignee_profile)
        first_revision = first.delegation.contract_revision
        first_baseline = first.delegation.baseline
        orch.fail(task.task_id, reason="synthetic failure for retry regression")
        orch.retry(task.task_id)
        second = orch.claim(task.task_id, claimed_by=task.assignee_profile)
        assert second.delegation.contract_revision == first_revision
        assert second.delegation.baseline == first_baseline


class TestStaleOrMismatchedIdentityBlocks:
    def test_mismatched_contract_revision_raises_not_silently_passes(self, orch):
        task = make_task(orch, idempotency_key="identity:mismatch:1")
        orch.enqueue(task.task_id)
        claimed = orch.claim(task.task_id, claimed_by=task.assignee_profile)
        # Simulate a tampered/stale persisted identity disagreeing with what
        # the same contract would derive again.
        claimed.delegation.contract_revision = "deliberately-wrong-revision"
        with pytest.raises(IdentityError) as exc:
            claimed.ensure_execution_identity()
        assert exc.value.code == "contract_revision_mismatch"
        # The bad value is untouched — never silently overwritten.
        assert claimed.delegation.contract_revision == "deliberately-wrong-revision"

    def test_mismatched_baseline_raises_not_silently_passes(self, orch):
        task = make_task(orch, idempotency_key="identity:mismatch:2")
        orch.enqueue(task.task_id)
        claimed = orch.claim(task.task_id, claimed_by=task.assignee_profile)
        claimed.delegation.baseline = "deliberately-wrong-baseline"
        with pytest.raises(IdentityError) as exc:
            claimed.ensure_execution_identity()
        assert exc.value.code == "baseline_mismatch"
        assert claimed.delegation.baseline == "deliberately-wrong-baseline"

    def test_mismatch_does_not_fabricate_a_replacement_value(self, orch):
        """A mismatch must never be "fixed" by writing a fresh value either —
        that would be exactly the fabrication the contract forbids."""
        task = make_task(orch, idempotency_key="identity:mismatch:3")
        orch.enqueue(task.task_id)
        claimed = orch.claim(task.task_id, claimed_by=task.assignee_profile)
        tampered = "not-a-real-revision"
        claimed.delegation.contract_revision = tampered
        for _ in range(2):
            with pytest.raises(IdentityError):
                claimed.ensure_execution_identity()
        assert claimed.delegation.contract_revision == tampered


class TestRouteAndLaunchRefusesMissingIdentity:
    def test_successful_route_and_launch_carries_derived_identity(self, orch):
        """route_and_launch's non-dry-run path only ever reaches the launcher
        with authoritative, persisted identity already attached."""
        orch.runtime_adapter = FakeAdapter("noesis-signal")
        res = orch.route_and_launch(
            title="Research pricing",
            intent="Research market pricing and return a cited brief with at least three sources.",
            acceptance_criteria=["At least three sources cited"],
            verification=__import__("app.models", fromlist=["Verification"]).Verification(
                method="reviewer_signoff"
            ),
            idempotency_key="identity:route:1",
            dry_run=False,
        )
        task = orch.store.get(res.task.task_id)
        assert task.delegation.contract_revision
        assert task.delegation.baseline

    def test_missing_identity_is_a_typed_blocker_not_a_silent_spawn(self, orch, monkeypatch):
        """Defense in depth: if identity were ever absent at the spawn gate,
        route_and_launch refuses with a typed RoutingBlocked, never a silent
        pass-through to the launcher."""
        orch.runtime_adapter = FakeAdapter("noesis-signal")
        original_reserve = orch.store.reserve_launch

        def _reserve_without_identity(task, reservation):
            result_task, created_now = original_reserve(task, reservation)
            # Force the defensive branch: strip identity after the authoritative
            # derivation already happened, simulating a future bypass.
            result_task.delegation.contract_revision = None
            result_task.delegation.baseline = None
            return result_task, created_now

        monkeypatch.setattr(orch.store, "reserve_launch", _reserve_without_identity)
        import app.models as models_mod

        verification = models_mod.Verification(method="reviewer_signoff")
        with pytest.raises(RoutingBlocked) as exc:
            orch.route_and_launch(
                title="Research pricing",
                intent="Research market pricing and return a cited brief with at least three sources.",
                acceptance_criteria=["At least three sources cited"],
                verification=verification,
                idempotency_key="identity:route:missing:1",
                dry_run=False,
            )
        assert exc.value.code == "missing_identity"
