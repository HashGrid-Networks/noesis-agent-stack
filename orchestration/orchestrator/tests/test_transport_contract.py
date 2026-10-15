from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from app.control_plane import Orchestrator
from app.launcher import LaunchResult
from app.models import Approval, TaskContract, Verification
from app.transport_contract import (
    CancellationObservation,
    CapabilityObservation,
    ProgressObservation,
    TransportContractError,
    TransportRequest,
    TransportResult,
    normalize_native_result,
    run_synthetic_acp_session,
)
from tests.fixtures.fake_acp import FakeAcpFixture, PINNED_PROTOCOL_ASSUMPTIONS


def _task() -> TaskContract:
    task = TaskContract(
        title="offline transport contract",
        intent="Exercise a synthetic transport without operational side effects.",
        assignee_profile="noesis-forge",
        required_capability="code_modify",
        risk_tier="r1",
        idempotency_key="transport-contract:test:1",
        acceptance_criteria=["ordered observations are retained"],
        verification=Verification(method="automated_test"),
        timeout_s=300,
        correlation_id=UUID("11111111-2222-4333-8444-555555555555"),
        approval=Approval(required=False, state="not_required"),
    )
    task.apply_claim("noesis-forge")
    task.delegation.assignment_epoch = 4
    task.delegation.attempt = 2
    task.delegation.attempt_epoch = 3
    task.delegation.runtime_profile = "noesis-forge"
    task.delegation.request_limit = 1
    task.delegation.budget_tokens = 500
    return task


def _request() -> TransportRequest:
    return TransportRequest.from_task(
        _task(),
        prompt="bounded offline prompt",
        cwd=Path("/tmp/offline-fixture"),
        dry_run=False,
        transport="acp",
    )


def test_request_maps_existing_authority_identity_budget_and_scope() -> None:
    task = _task()
    request = TransportRequest.from_task(task, prompt="p", cwd=Path("/tmp/w"), dry_run=False)
    assert request.task_id == str(task.task_id)
    assert request.transport == "native"
    assert request.correlation_id == str(task.correlation_id)
    assert request.attempt == 2
    assert request.assignment_epoch == 4
    assert request.attempt_epoch == 3
    assert request.worker_profile == task.assignee_profile == task.delegation.runtime_profile
    assert request.required_capability == "code_modify"
    assert request.approval_state == "not_required"
    assert request.request_limit == 1
    assert request.budget_tokens == 500
    assert request.tokens_used is None
    assert request.acceptance_criteria == ("ordered observations are retained",)
    assert request.contract_revision == task.delegation.contract_revision
    assert request.baseline == task.delegation.baseline


def test_request_fails_closed_on_worker_binding_mismatch() -> None:
    task = _task()
    with pytest.raises(TransportContractError, match="worker_profile_mismatch"):
        TransportRequest.from_task(
            task,
            prompt="p",
            cwd=None,
            dry_run=False,
            expected_worker_profile="other-profile",
        )


def test_observation_types_preserve_unknown_usage_and_cancellation_distinctions() -> None:
    capability = CapabilityObservation(
        transport="native",
        worker_id="noesis-forge",
        advertised=("prompt",),
        supported=("prompt",),
        assumptions_verified=True,
    )
    progress = ProgressObservation(sequence=1, kind="update", payload={"text": "partial"}, partial=True)
    cancellation = CancellationObservation(
        requested=True,
        acknowledged=True,
        process_stopped=None,
        authority_revoked=True,
        cleanup_state="uncertain",
    )
    result = TransportResult(
        status="uncertain",
        progress=(progress,),
        cancellation=cancellation,
        usage_tokens=None,
        usage_cost=None,
    )
    assert capability.supports("prompt")
    assert result.usage_tokens is None
    assert result.usage_cost is None
    assert result.cancellation.process_stopped is None
    assert result.progress[0].sequence == 1


def test_fake_acp_initialization_session_correlation_order_and_terminal_result() -> None:
    fixture = FakeAcpFixture(
        script=[
            {"method": "session/update", "sequence": 1, "payload": {"text": "a"}},
            {"method": "session/update", "sequence": 2, "payload": {"text": "b"}},
            {"method": "session/result", "sequence": 3, "status": "completed", "artifacts": []},
        ]
    )
    request = _request()
    result = run_synthetic_acp_session(request, fixture)
    assert result.status == "completed"
    assert [item.sequence for item in result.progress] == [1, 2]
    assert result.session_id == "fake-session-1"
    assert result.capabilities is not None
    assert result.capabilities.supports("session/prompt")
    assert result.capabilities.assumptions_verified is False
    assert fixture.sessions[result.session_id] == request.correlation_id
    assert fixture.prompt_count == 1
    assert result.accepted is False
    assert PINNED_PROTOCOL_ASSUMPTIONS["installed_hermes_verified"] is False


def test_fake_acp_denies_permission_request_without_authority() -> None:
    fixture = FakeAcpFixture(
        script=[
            {"method": "permission/request", "sequence": 1, "permission": "shell"},
            {"method": "session/result", "sequence": 2, "status": "completed"},
        ]
    )
    result = run_synthetic_acp_session(_request(), fixture)
    assert result.status == "blocked"
    assert result.blocker_code == "permission_denied"
    assert result.accepted is False


@pytest.mark.parametrize(
    ("script", "blocker"),
    [
        ([{"method": "unknown", "sequence": 1}], "malformed_message"),
        ([{"method": "session/update", "sequence": 2, "payload": {}}], "out_of_order_update"),
        ([{"method": "session/result", "sequence": 1, "status": "mystery"}], "unsupported_terminal_status"),
    ],
)
def test_fake_acp_rejects_malformed_or_unsupported_messages(script, blocker) -> None:
    result = run_synthetic_acp_session(_request(), FakeAcpFixture(script=script))
    assert result.status == "blocked"
    assert result.blocker_code == blocker


def test_fake_acp_early_exit_timeout_and_partial_output_are_uncertain() -> None:
    early = run_synthetic_acp_session(_request(), FakeAcpFixture(script=[], exit_early=True))
    timed = run_synthetic_acp_session(_request(), FakeAcpFixture(script=[], time_out=True))
    partial = run_synthetic_acp_session(
        _request(),
        FakeAcpFixture(script=[{"method": "session/update", "sequence": 1, "payload": {"text": "partial"}}]),
    )
    assert (early.status, early.blocker_code) == ("uncertain", "early_exit")
    assert (timed.status, timed.blocker_code) == ("uncertain", "timeout")
    assert (partial.status, partial.blocker_code) == ("uncertain", "missing_terminal_result")
    assert partial.progress[0].partial is True


def test_fake_acp_cancellation_separates_request_ack_stop_and_revocation() -> None:
    fixture = FakeAcpFixture(script=[])
    result = run_synthetic_acp_session(_request(), fixture, cancel_before_prompt=True)
    assert result.status == "cancelled"
    assert result.cancellation == CancellationObservation(
        requested=True,
        acknowledged=True,
        process_stopped=None,
        authority_revoked=True,
        cleanup_state="uncertain",
    )


def test_control_plane_prefers_transport_request_without_changing_native_result(tmp_path: Path) -> None:
    class RequestAwareAdapter:
        requires_approved_pair = False

        def __init__(self) -> None:
            self.request = None

        def profile_installed(self, profile_id: str) -> bool:
            return True

        def launch_request(self, request: TransportRequest, **runtime_options):
            self.request = request
            assert runtime_options == {}
            return LaunchResult(
                ok=True,
                requested_profile=request.worker_profile,
                loaded_profile=request.worker_profile,
                command=["synthetic-native"],
                attestation=None,
            )

        def launch(self, **kwargs):
            raise AssertionError("legacy launch path should not be used")

    adapter = RequestAwareAdapter()
    orchestrator = Orchestrator(tmp_path / "tasks.jsonl", runtime_adapter=adapter, inference_enforcer=None)
    task = _task()
    result = orchestrator._invoke_launch(
        task=task,
        profile_id=task.assignee_profile,
        prompt="bounded prompt",
        task_id=str(task.task_id),
        cwd=None,
        dry_run=True,
    )
    assert result.ok is True
    assert adapter.request is not None
    assert adapter.request.task_id == str(task.task_id)
    assert adapter.request.worker_profile == task.assignee_profile


def test_native_normalization_preserves_artifacts_attestation_outcome_and_unknown_usage() -> None:
    launch = LaunchResult(
        ok=True,
        requested_profile="noesis-forge",
        loaded_profile="noesis-forge",
        command=["hermes"],
        attestation=None,
        artifacts=("candidate.diff",),
        disposition="exited",
        verification_pending=True,
        accepted=False,
        candidate_digest="abc",
        usage_tokens=None,
    )
    result = normalize_native_result(launch)
    assert result.status == "exited"
    assert result.artifacts == ("candidate.diff",)
    assert result.candidate_digest == "abc"
    assert result.verification_pending is True
    assert result.accepted is False
    assert result.usage_tokens is None
    assert result.usage_cost is None
    assert result.raw_result is launch


def test_native_normalization_keeps_cleanup_uncertain_distinct_from_process_stop() -> None:
    launch = LaunchResult(
        ok=False,
        requested_profile="noesis-forge",
        loaded_profile="noesis-forge",
        command=["hermes"],
        attestation=None,
        blocker_code="timeout",
        cleanup_state="uncertain",
    )
    result = normalize_native_result(launch)
    assert result.status == "uncertain"
    assert result.cancellation.requested is True
    assert result.cancellation.process_stopped is None
    assert result.cancellation.cleanup_state == "uncertain"
    assert result.accepted is False


def test_duplicate_dispatch_is_suppressed_and_restart_reconciliation_is_explicit() -> None:
    request = _request()
    fixture = FakeAcpFixture(
        script=[{"method": "session/result", "sequence": 1, "status": "completed"}]
    )
    reservations: dict[str, str] = {}
    first = run_synthetic_acp_session(request, fixture, reservations=reservations)
    duplicate = run_synthetic_acp_session(request, fixture, reservations=reservations)
    restarted = run_synthetic_acp_session(
        request,
        fixture,
        reservations=dict(reservations),
        reconcile_after_restart=True,
    )
    assert first.status == "completed"
    assert duplicate.blocker_code == "duplicate_dispatch"
    assert restarted.blocker_code == "reconciliation_required"
    assert fixture.prompt_count == 1
