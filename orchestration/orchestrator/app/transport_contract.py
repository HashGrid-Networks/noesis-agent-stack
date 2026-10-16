"""Transport-neutral delegation observations over authoritative task records.

This module does not own lifecycle state. It projects the existing TaskContract
and LaunchResult records into a protocol-neutral boundary; TaskStore and the
control plane remain the only task/event authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from app.models import TaskContract


class TransportContractError(ValueError):
    """A transport request or observation failed closed."""


@dataclass(frozen=True)
class TransportRequest:
    transport: str
    task_id: str
    correlation_id: str
    idempotency_key: str
    attempt: int
    assignment_epoch: int
    attempt_epoch: int | None
    contract_revision: str | None
    baseline: str | None
    assignee_profile: str
    worker_profile: str
    required_capability: str
    prompt: str
    cwd: str | None
    dry_run: bool
    risk_tier: str
    approval_state: str
    approval_manifest_id: str | None
    acceptance_criteria: tuple[str, ...]
    request_limit: int | None
    requests_used: int
    budget_tokens: int | None
    tokens_used: int | None
    lease_expires_at: str | None
    execution_revoked: bool
    irreversible_operations: tuple[str, ...]

    @classmethod
    def from_task(
        cls,
        task: TaskContract,
        *,
        prompt: str,
        cwd: Path | str | None,
        dry_run: bool,
        expected_worker_profile: str | None = None,
        transport: str = "native",
    ) -> "TransportRequest":
        runtime_profile = task.delegation.runtime_profile
        worker_profile = runtime_profile or expected_worker_profile or task.assignee_profile
        if expected_worker_profile is not None and worker_profile != expected_worker_profile:
            raise TransportContractError("worker_profile_mismatch")
        if not isinstance(transport, str) or not transport:
            raise TransportContractError("transport_required")
        if not isinstance(prompt, str) or not prompt:
            raise TransportContractError("prompt_required")
        if task.delegation.execution_revoked:
            raise TransportContractError("execution_revoked")
        if not dry_run and task.claim is None:
            raise TransportContractError("claim_required")
        approval = task.approval
        claim = task.claim or {}
        lease_expires = claim.get("lease_expires_at")
        return cls(
            transport=transport,
            task_id=str(task.task_id),
            correlation_id=str(task.correlation_id),
            idempotency_key=task.idempotency_key,
            attempt=task.delegation.attempt,
            assignment_epoch=task.delegation.assignment_epoch,
            attempt_epoch=task.delegation.attempt_epoch,
            contract_revision=task.delegation.contract_revision,
            baseline=task.delegation.baseline,
            assignee_profile=task.assignee_profile,
            worker_profile=worker_profile,
            required_capability=task.required_capability,
            prompt=prompt,
            cwd=str(cwd) if cwd is not None else None,
            dry_run=dry_run,
            risk_tier=task.risk_tier,
            approval_state=approval.state if approval else "not_required",
            approval_manifest_id=approval.approval_manifest_id if approval else None,
            acceptance_criteria=tuple(task.acceptance_criteria),
            request_limit=task.delegation.request_limit,
            requests_used=task.delegation.requests_used,
            budget_tokens=task.delegation.budget_tokens,
            tokens_used=task.delegation.tokens_used,
            lease_expires_at=lease_expires.isoformat() if lease_expires is not None else None,
            execution_revoked=task.delegation.execution_revoked,
            irreversible_operations=tuple(task.irreversible_operations),
        )

    @property
    def dispatch_key(self) -> str:
        epoch = "unknown" if self.attempt_epoch is None else str(self.attempt_epoch)
        return f"{self.task_id}:{self.assignment_epoch}:{self.attempt}:{epoch}"


@dataclass(frozen=True)
class CapabilityObservation:
    transport: str
    worker_id: str
    advertised: tuple[str, ...]
    supported: tuple[str, ...]
    assumptions_verified: bool
    unverified_assumptions: tuple[str, ...] = ()

    def supports(self, capability: str) -> bool:
        return capability in self.supported


@dataclass(frozen=True)
class ProgressObservation:
    sequence: int
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    partial: bool = True

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or self.sequence < 1:
            raise TransportContractError("invalid_progress_sequence")


@dataclass(frozen=True)
class CancellationObservation:
    requested: bool = False
    acknowledged: bool | None = None
    process_stopped: bool | None = None
    authority_revoked: bool = False
    cleanup_state: str | None = None


@dataclass(frozen=True)
class TransportResult:
    status: str
    session_id: str | None = None
    capabilities: CapabilityObservation | None = None
    progress: tuple[ProgressObservation, ...] = ()
    artifacts: tuple[str, ...] = ()
    candidate_digest: str | None = None
    blocker_code: str | None = None
    error: str | None = None
    cancellation: CancellationObservation = field(default_factory=CancellationObservation)
    usage_tokens: int | None = None
    usage_cost: float | None = None
    verification_pending: bool = False
    accepted: bool = False
    raw_result: Any | None = field(default=None, compare=False, repr=False)


class SyntheticAcpPeer(Protocol):
    def initialize(self, requested: tuple[str, ...]) -> dict[str, Any]: ...

    def new_session(self, correlation_id: str) -> dict[str, Any]: ...

    def prompt(self, session_id: str, text: str) -> list[dict[str, Any]]: ...

    def cancel(self, session_id: str) -> dict[str, Any]: ...


_ACP_REQUIRED = ("session/new", "session/prompt", "session/cancel")
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "uncertain"})


def _blocked(code: str, *, session_id: str | None = None, error: str | None = None) -> TransportResult:
    return TransportResult(status="blocked", session_id=session_id, blocker_code=code, error=error)


def run_synthetic_acp_session(
    request: TransportRequest,
    peer: SyntheticAcpPeer,
    *,
    cancel_before_prompt: bool = False,
    reservations: dict[str, str] | None = None,
    reconcile_after_restart: bool = False,
) -> TransportResult:
    """Exercise the contract against an injected, non-operational ACP-like peer.

    No network, process, credential, tool, or model operation exists here. The
    caller-provided reservation map is a fixture seam, not a second task store.
    Restart recovery deliberately stops at an explicit reconciliation result.
    """
    if request.transport != "acp":
        return _blocked("transport_mismatch")
    key = request.dispatch_key
    if reservations is not None and key in reservations:
        code = "reconciliation_required" if reconcile_after_restart else "duplicate_dispatch"
        return TransportResult(status="uncertain", blocker_code=code)
    if reservations is not None:
        reservations[key] = "reserved"

    init = peer.initialize(_ACP_REQUIRED)
    capabilities = init.get("capabilities") if isinstance(init, dict) else None
    if not isinstance(capabilities, list) or any(cap not in capabilities for cap in _ACP_REQUIRED):
        return _blocked("unsupported_capabilities")
    capability_observation = CapabilityObservation(
        transport="acp",
        worker_id=request.worker_profile,
        advertised=tuple(str(item) for item in capabilities),
        supported=tuple(str(item) for item in capabilities),
        assumptions_verified=False,
        unverified_assumptions=(
            "fixture framing and method names are not verified against installed Hermes",
        ),
    )
    session = peer.new_session(request.correlation_id)
    if not isinstance(session, dict) or session.get("method") != "session/new":
        return _blocked("malformed_session")
    session_id = session.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return _blocked("malformed_session")
    if session.get("correlation_id") != request.correlation_id:
        return _blocked("session_correlation_mismatch", session_id=session_id)

    if cancel_before_prompt:
        response = peer.cancel(session_id)
        acknowledged = bool(isinstance(response, dict) and response.get("acknowledged") is True)
        return TransportResult(
            status="cancelled",
            session_id=session_id,
            cancellation=CancellationObservation(
                requested=True,
                acknowledged=acknowledged,
                process_stopped=None,
                authority_revoked=True,
                cleanup_state="uncertain",
            ),
        )

    try:
        messages = peer.prompt(session_id, request.prompt)
    except TimeoutError as exc:
        return TransportResult(
            status="uncertain",
            session_id=session_id,
            blocker_code="timeout",
            error=str(exc),
            cancellation=CancellationObservation(
                requested=False,
                acknowledged=None,
                process_stopped=None,
                authority_revoked=True,
                cleanup_state="uncertain",
            ),
        )
    if not messages:
        return TransportResult(status="uncertain", session_id=session_id, blocker_code="early_exit")

    progress: list[ProgressObservation] = []
    expected_sequence = 1
    terminal: dict[str, Any] | None = None
    for message in messages:
        if not isinstance(message, dict):
            return _blocked("malformed_message", session_id=session_id)
        method = message.get("method")
        sequence = message.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence != expected_sequence:
            return _blocked("out_of_order_update", session_id=session_id)
        expected_sequence += 1
        if method == "permission/request":
            return TransportResult(
                status="blocked",
                session_id=session_id,
                progress=tuple(progress),
                blocker_code="permission_denied",
                error="transport workers have no authority to expand scope",
            )
        if method == "session/update":
            payload = message.get("payload")
            if not isinstance(payload, dict):
                return _blocked("malformed_message", session_id=session_id)
            progress.append(ProgressObservation(sequence=sequence, kind="update", payload=payload))
            continue
        if method == "session/result":
            terminal = message
            break
        return _blocked("malformed_message", session_id=session_id)

    if terminal is None:
        return TransportResult(
            status="uncertain",
            session_id=session_id,
            progress=tuple(progress),
            blocker_code="missing_terminal_result",
        )
    status = terminal.get("status")
    if status not in _TERMINAL_STATUSES:
        return _blocked("unsupported_terminal_status", session_id=session_id)
    artifacts = terminal.get("artifacts") or []
    if not isinstance(artifacts, list) or not all(isinstance(item, str) for item in artifacts):
        return _blocked("malformed_message", session_id=session_id)
    return TransportResult(
        status=status,
        session_id=session_id,
        capabilities=capability_observation,
        progress=tuple(progress),
        artifacts=tuple(artifacts),
        candidate_digest=terminal.get("candidate_digest"),
        usage_tokens=terminal.get("usage_tokens"),
        usage_cost=terminal.get("usage_cost"),
        verification_pending=bool(terminal.get("candidate_digest")),
        accepted=False,
    )


def normalize_native_result(result: Any) -> TransportResult:
    """Project the existing native LaunchResult without changing its behavior."""
    if getattr(result, "ok", False):
        status = getattr(result, "disposition", None) or "completed"
    elif getattr(result, "blocker_code", None) == "timeout":
        status = "uncertain"
    else:
        status = "failed"
    cleanup = getattr(result, "cleanup_state", None)
    cancellation = CancellationObservation(
        requested=getattr(result, "blocker_code", None) == "timeout",
        acknowledged=None,
        process_stopped=True if cleanup == "terminated" else None,
        authority_revoked=not bool(getattr(result, "ok", False)),
        cleanup_state=cleanup,
    )
    return TransportResult(
        status=status,
        session_id=getattr(result, "session_id", None),
        artifacts=tuple(getattr(result, "artifacts", ()) or ()),
        candidate_digest=getattr(result, "candidate_digest", None),
        blocker_code=getattr(result, "blocker_code", None),
        error=getattr(result, "error", None),
        cancellation=cancellation,
        usage_tokens=getattr(result, "usage_tokens", None),
        usage_cost=None,
        verification_pending=bool(getattr(result, "verification_pending", False)),
        accepted=bool(getattr(result, "accepted", False)),
        raw_result=result,
    )
