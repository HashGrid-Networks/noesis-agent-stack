"""The noesis-orchestrator control plane.

This module is the only component permitted to mutate task state. It plans
(create), dispatches (claim), supervises (sweep, retry, break), and synthesizes
(synthesize) — it never performs the assigned work itself and holds no
execution toolset.

Emergency stop: `engage_circuit_breaker()` halts all dispatch immediately;
already-running work is left to its lease and swept normally.
"""

from __future__ import annotations

import warnings

import hashlib
import inspect
import json
import math
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from app import policy
from app.inference_routing import (
    EnforcerConfig,
    InferenceRoutingEnforcer,
    RoutingContext,
)
from app.launcher import HermesCliAdapter, LaunchResult, RuntimeAdapter
from app.adaptive_specialist import (
    EvidenceShape,
    LocalReadinessRegistry,
    PreflightDecision,
    ProviderFamilyMap,
    TaskConfigurationPrecomputer,
)
from app.models import (
    Approval,
    Delegation,
    IdentityError,
    QUALITY_VERDICTS,
    REVIEW_STAGES,
    Retry,
    ReviewRecord,
    SPEC_VERDICTS,
    TaskContract,
    TransitionError,
    Verification,
    utcnow,
)
from app.planning import PlanRequest, PlanTask, PlanningBlocked, RoutePlanner
from app.policy import DispatchDecision, PolicyViolation
from app.specialist_decision import (
    CandidateInput,
    DecisionConfig,
    DecisionLayer,
    DecisionMode,
    DeterministicProvider,
    FallbackReason,
)
from app.specialist_routing import (
    CapabilityIndex,
    GraphNode,
    RoutingBlocked,
    SelectionDecision,
    SpecialistGraph,
    SpecialistRouteResult,
    depth_exceeded,
    emit_routing_event,
    make_child_prompt,
    selection_rejections,
)
from app.store import (
    BREAKER_RESET_EVENT,
    BREAKER_TRIP_EVENT,
    DEFAULT_LEDGER,
    ReplayError,
    TaskStore,
    TaskVersionConflict,
)
from app.transport_contract import TransportRequest

# Consecutive failures within one correlation graph before dispatch is halted.
DEFAULT_FAILURE_THRESHOLD = 3


@dataclass
class CircuitBreaker:
    """Halts dispatch after repeated failures, or on operator command."""

    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD
    consecutive_failures: int = 0
    manually_tripped: bool = False
    reason: str | None = None

    @property
    def is_open(self) -> bool:
        """Open means: no further dispatch."""
        return self.manually_tripped or self.consecutive_failures >= self.failure_threshold

    def record_failure(self, reason: str) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.failure_threshold:
            self.reason = f"failure threshold reached: {reason}"

    def record_success(self) -> None:
        self.consecutive_failures = 0
        if not self.manually_tripped:
            self.reason = None

    def trip(self, reason: str) -> None:
        self.manually_tripped = True
        self.reason = reason

    def reset(self, *, operator: str) -> None:
        self.manually_tripped = False
        self.consecutive_failures = 0
        self.reason = f"reset by {operator}"


class Orchestrator:
    """Least-privilege planner / dispatcher / supervisor / synthesizer."""

    profile_name = "noesis-orchestrator"

    def __init__(
        self,
        ledger_path: Path | str = DEFAULT_LEDGER,
        *,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        inference_enforcer: InferenceRoutingEnforcer | None = None,
        inference_enforcer_config: EnforcerConfig | None = None,
        inference_routing_context: dict[str, dict[str, str]] | None = None,
        capability_index: CapabilityIndex | None = None,
        runtime_adapter: RuntimeAdapter | None = None,
        routing_event_log_path: Path | str | None = None,
        decision_layer: DecisionLayer | None = None,
        decision_layer_config: DecisionConfig | None = None,
    ) -> None:
        self.store = TaskStore(ledger_path)
        self.breaker = CircuitBreaker(failure_threshold=failure_threshold)
        self._restore_breaker_state()
        # Track whether the caller explicitly provided an enforcer. When None is
        # passed explicitly, do not fall back to an environment-derived enforcer —
        # that preserves the caller's intent (no enforcement) and ensures G1's
        # "enforcer_unavailable" check fires correctly in bounded test fixtures.
        self._enforcer_was_explicit = inference_enforcer is not None
        if inference_enforcer is not None:
            self.inference_enforcer = inference_enforcer
        elif inference_enforcer_config is not None:
            # Config was explicitly provided; create from that config.
            try:
                self.inference_enforcer = InferenceRoutingEnforcer(inference_enforcer_config)
            except Exception:
                # In observe-capable deployments the orchestrator must still boot;
                # enforcement modes will fail closed on dispatch if no policy loads.
                self.inference_enforcer = None
        else:
            # inference_enforcer=None and inference_enforcer_config=None
            # were both passed explicitly; honour the caller's intent — no enforcer.
            self.inference_enforcer = None
        self._inference_routing_context = dict(inference_routing_context or {})
        self.runtime_adapter = runtime_adapter or HermesCliAdapter()
        self.capability_index = capability_index or CapabilityIndex.from_repo(adapter=self.runtime_adapter)
        self.routing_event_log_path = Path(routing_event_log_path) if routing_event_log_path else None
        self.route_planner = RoutePlanner(
            self.capability_index,
            self.runtime_adapter,
            ledger=self.store.ledger_path.parent / "plans.jsonl",
        )
        # The decision layer is a bounded, default-OFF advisor between
        # the deterministic baseline (CapabilityIndex.select) and the
        # launch boundary. The default instance uses
        # DeterministicProvider (which abstains) and OFF mode, so the
        # wiring is invisible to existing callers when no layer is
        # provided.
        if decision_layer is not None:
            self._decision_layer: DecisionLayer = decision_layer
        else:
            self._decision_layer = DecisionLayer(
                provider=DeterministicProvider(),
                config=decision_layer_config or DecisionConfig(),
            )

    # ------------------------------------------------------- specialist route --

    def select_specialist(
        self,
        intent: str,
        *,
        required_capability: str | None = None,
        risk_tier: str = "r0",
        allow_generic_fallback: bool = False,
        exclude_profiles: set[str] | None = None,
    ):
        """Classify a bounded task and select an eligible specialist profile."""
        return self.capability_index.select(
            intent,
            required_capability=required_capability,
            risk_tier=risk_tier,
            allow_generic_fallback=allow_generic_fallback,
            exclude_profiles=exclude_profiles,
        )

    def route_and_launch(
        self,
        *,
        title: str,
        intent: str,
        acceptance_criteria: list[str],
        verification: Verification,
        idempotency_key: str,
        required_capability: str | None = None,
        correlation_id: UUID | None = None,
        risk_tier: str = "r0",
        timeout_s: int = 900,
        max_attempts: int = 1,
        depends_on: list[UUID] | None = None,
        irreversible_operations: list[str] | None = None,
        reviewer_profile: str | None = None,
        approval_required: bool | None = None,
        parent_task_id: UUID | str | None = None,
        max_depth: int = 1,
        allow_generic_fallback: bool = False,
        dry_run: bool = True,
        inference_routing_context: dict[str, str] | None = None,
        approved_provider: str | None = None,
        approved_model: str | None = None,
        workspace: str | None = None,
        expected_epoch: int | None = None,
        decision_layer: DecisionLayer | None = None,
    ) -> SpecialistRouteResult:
        """End-to-end specialist route through the live orchestrator entrypoint.

        .. deprecated::
            B2 (contract §3.1): ``route_and_launch`` is the legacy
            entrypoint that skips the minimum-safe preflight ordering.
            External callers should migrate to :meth:`preflight_then_route`
            (which forwards to :meth:`route_with_preflight`). The legacy
            path is preserved for the existing regression suite but the
            new ordered method is the canonical dispatch surface.

        This is the auditable path: classify -> discover/filter -> select ->
        policy admission -> explicit Hermes ``-p`` target -> profile attestation.
        It never silently substitutes the parent, default, or coder profile.
        """
        warnings.warn(
            "Orchestrator.route_and_launch is deprecated; use "
            "Orchestrator.preflight_then_route (or route_with_preflight) "
            "to enforce the minimum-safe preflight ordering.",
            DeprecationWarning,
            stacklevel=2,
        )
        if depth_exceeded(parent_task_id, max_depth):
            raise RoutingBlocked("depth_exceeded", "subordinate orchestration depth/fanout limit exceeded")
        if self.store.replay_error or self.breaker.is_open:
            raise RoutingBlocked(
                "launch_blocked",
                f"fail-closed: {self.store.replay_error or self.breaker.reason or 'dispatch breaker open'}",
            )
        selection = self.select_specialist(
            intent,
            required_capability=required_capability,
            risk_tier=risk_tier,
            allow_generic_fallback=allow_generic_fallback,
            exclude_profiles={self.profile_name},
        )
        if selection.selected is None:
            raise RoutingBlocked(
                "no_eligible_specialist",
                selection.reason,
                rejections=selection_rejections(selection),
            )
        selected = selection.selected
        if selected.profile_id == self.profile_name:
            raise RoutingBlocked("self_delegation_blocked", "noesis-orchestrator is not an ordinary worker target")
        # Apply the bounded, default-OFF decision layer between the
        # deterministic baseline (CapabilityIndex.select) and the
        # launch gate. The decision layer never authorizes a launch,
        # never mutates the task, and never changes a risk tier; it
        # can only, in ACTIVE mode on tied eligible candidates,
        # substitute the profile. The default ``DeterministicProvider``
        # abstains on every call and OFF mode is the wiring default,
        # so this is invisible when no ``decision_layer`` override is
        # provided.
        active_layer = decision_layer or self._decision_layer
        tied = self._tied_candidates(selection)
        decision_outcome = active_layer.decide(
            baseline_candidate_id=selected.profile_id,
            tied_candidates=tied,
            required_capability=selection.classification.required_capability,
            classification=selection.classification.task_class,
            risk_tier=risk_tier,
            candidate_records=self._build_decision_candidate_records(tied),
            registry_digest=self._stable_registry_digest(),
            policy_digest=self._stable_policy_digest(inference_routing_context),
            rubric_digest=self._stable_rubric_digest(verification),
            snapshot_digest=self._stable_snapshot_digest(selection),
            question=intent,
            correlation_id=str(correlation_id) if correlation_id else "",
            task_id=str(uuid4()),
            attempt_id="1",
            requested_model=getattr(selected, "model_id", None),
        )
        if (
            decision_outcome.mode is DecisionMode.ACTIVE
            and decision_outcome.fallback_reason is FallbackReason.OK
            and decision_outcome.applied_candidate_id is not None
            and decision_outcome.applied_candidate_id
            != decision_outcome.baseline_candidate_id
        ):
            # Only rerank within the tied eligible set; the decision
            # layer's invariant is that ``in_scope`` is built only
            # from those candidates, so any substitution is by
            # construction inside the set the host already accepted.
            new_record = self.capability_index.record(
                decision_outcome.applied_candidate_id
            )
            if new_record is not None and new_record.profile_id in tied:
                selection = SelectionDecision(
                    classification=selection.classification,
                    selected=new_record,
                    candidates=selection.candidates,
                    reason=(
                        f"decision-layer ACTIVE rerank: "
                        f"{selected.profile_id} -> {new_record.profile_id}"
                    ),
                    fallback_reason=selection.fallback_reason,
                )
                selected = selection.selected
        labels = [selection.classification.task_class]
        task_capability = selection.classification.required_capability
        if selected.profile_id == "coder" and task_capability == "code_modify":
            task_capability = "implement"
        task = self.propose(
            title=title,
            intent=intent,
            assignee_profile=selected.profile_id,
            required_capability=task_capability,
            acceptance_criteria=acceptance_criteria,
            verification=verification,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            risk_tier=risk_tier,
            timeout_s=timeout_s,
            max_attempts=max_attempts,
            depends_on=depends_on,
            irreversible_operations=irreversible_operations,
            reviewer_profile=reviewer_profile or selection.classification.reviewer_required,
            approval_required=approval_required,
            labels=labels,
            parent_task_id=UUID(str(parent_task_id)) if parent_task_id else None,
        )
        if inference_routing_context:
            self._inference_routing_context[str(task.task_id)] = dict(inference_routing_context)
        prompt = make_child_prompt(task, selection)
        if dry_run:
            launch = self._invoke_launch(
                task=task,
                profile_id=selected.runtime_profile,
                prompt=prompt,
                task_id=str(task.task_id),
                cwd=None,
                dry_run=True,
            )
            self._emit_launch_event(task, selection, launch, parent_task_id)
            if not launch.ok:
                raise RoutingBlocked("launch_failed", launch.error or "launcher failed without an error")
            if launch.loaded_profile != selected.runtime_profile:
                raise RoutingBlocked(
                    "loaded_profile_mismatch",
                    f"requested '{selected.runtime_profile}' but loaded '{launch.loaded_profile}'",
                )
            return SpecialistRouteResult(False, selection, task, launch, selection.fallback_reason)

        self._assert_spawn_gates(
            task,
            approved_provider=approved_provider,
            approved_model=approved_model,
            workspace=workspace,
            timeout_s=timeout_s,
            expected_epoch=expected_epoch,
        )
        workspace_path = str(Path(workspace).resolve()) if workspace else str(self.store.ledger_path.parent.resolve())
        reservation = {
            "attempt": 1,
            "runtime_profile": selected.runtime_profile,
            "approved_provider": approved_provider,
            "approved_model": approved_model,
            "fallback_policy": "deny",
            "admission": {"allowed": True, "reason": "gates_passed"},
            "deadline_s": timeout_s,
            "workspace": workspace_path,
            "workspace_reusable": True,
        }
        try:
            task, created_now = self.store.reserve_launch(task, reservation)
        except IdentityError as exc:
            # Stale/mismatched authoritative identity must block, never pass
            # through silently and never be papered over with a caller value.
            raise RoutingBlocked(getattr(exc, "code", "identity_invalid"), str(exc))
        except Exception:
            raise
        if not created_now:
            return SpecialistRouteResult(False, selection, task, None, "reservation_exists_no_respawn")
        remaining = self._remaining_lease_s(task)
        if remaining is None or remaining <= 0:
            self._quarantine_reservation(task, "deadline_invalid" if remaining is None else "lease_expired")
            raise RoutingBlocked(
                "invalid_deadline" if remaining is None else "lease_expired",
                "remaining lease is missing, invalid, or not positive; refusing spawn",
            )
        if not task.delegation.contract_revision or not task.delegation.baseline:
            # Defense in depth: every claim path derives identity via
            # TaskContract.apply_claim/ensure_execution_identity before a task
            # can reach here. A missing value at this point is a typed
            # blocker, never a silent spawn with absent identity.
            self._quarantine_reservation(task, "missing_identity")
            raise RoutingBlocked(
                "missing_identity",
                "authoritative contract_revision/baseline is missing; refusing spawn",
            )
        capped_timeout = min(float(timeout_s), remaining)
        try:
            launch = self._invoke_launch(
                task=task,
                profile_id=selected.runtime_profile,
                prompt=prompt,
                task_id=str(task.task_id),
                cwd=workspace_path,
                dry_run=False,
                provider=approved_provider,
                model=approved_model,
                timeout_s=capped_timeout,
                remaining_lease_s=remaining,
                workspace=workspace_path,
                fallback_policy="deny",
                attempt_id=str(task.delegation.attempt),
                assignment_epoch=task.delegation.assignment_epoch,
                contract_revision=task.delegation.contract_revision,
                baseline=task.delegation.baseline,
            )
        except Exception:
            self._quarantine_reservation(task, "handle_uncertain")
            raise
        self._emit_launch_event(task, selection, launch, parent_task_id)
        if not launch.ok:
            self._record_launch_failure(task, launch)
            raise RoutingBlocked(
                getattr(launch, "blocker_code", None) or "launch_failed",
                launch.error or "launcher failed without an error",
            )
        if launch.loaded_profile != selected.runtime_profile:
            self._quarantine_reservation(task, "profile_mismatch")
            raise RoutingBlocked(
                "loaded_profile_mismatch",
                f"requested '{selected.runtime_profile}' but loaded '{launch.loaded_profile}'",
            )
        reported_provider = getattr(launch, "provider", approved_provider)
        reported_model = getattr(launch, "model", approved_model)
        if approved_provider and reported_provider not in (None, approved_provider):
            raise RoutingBlocked("provider_mismatch", f"requested {approved_provider} but launched {reported_provider}")
        if approved_model and reported_model not in (None, approved_model):
            raise RoutingBlocked("model_mismatch", f"requested {approved_model} but launched {reported_model}")
        handle = {
            "pid": getattr(launch, "process_id", None),
            "pgid": getattr(launch, "process_id", None),
            "loaded_profile": launch.loaded_profile,
            "provider": reported_provider,
            "model": reported_model,
            "candidate_digest": getattr(launch, "candidate_digest", None),
            "disposition": getattr(launch, "disposition", None),
            "usage_tokens": getattr(launch, "usage_tokens", None),
        }
        task.delegation.launch_reservation = dict(task.delegation.launch_reservation or {})
        task.delegation.launch_reservation.update(
            {
                "state": "spawned",
                "process_handle": handle,
                "workspace_reusable": True,
                "candidate_digest": getattr(launch, "candidate_digest", None),
                "verification_pending": getattr(launch, "verification_pending", False),
                "accepted": False,
                "sorted": sorted(launch.artifacts or ()),
                "stdout_digest": getattr(launch, "stdout_digest", None),
                "collection_error": getattr(launch, "collection_error", None),
                "attempt_id": getattr(launch, "attempt_id", None),
            }
        )
        task = self.store.put(task, event="launch_handle_persisted")
        return SpecialistRouteResult(False, selection, task, launch, selection.fallback_reason)

    def _emit_launch_event(self, task, selection, launch, parent_task_id) -> None:
        selected = selection.selected
        emit_routing_event(self.routing_event_log_path, {
            "event": "specialist_dispatch",
            "task_id": str(task.task_id),
            "parent_task_id": str(parent_task_id) if parent_task_id else None,
            "root_task_id": str(task.correlation_id),
            "task_class": selection.classification.task_class,
            "candidate_profiles": [c.profile_id for c in selection.candidates],
            "eligibility_rejections": selection_rejections(selection),
            "selected_agent": selected.profile_id if selected else None,
            "selection_reason": selection.reason,
            "resolved_launch_target": selected.runtime_profile if selected else None,
            "requested_profile_id": selected.runtime_profile if selected else None,
            "loaded_profile_id": getattr(launch, "loaded_profile", None),
            "attestation": asdict(launch.attestation) if getattr(launch, "attestation", None) else None,
            "model_provider": getattr(selected, "model_provider", None),
            "model_id": getattr(selected, "model_id", None),
            "policy_decision": "verification_pending" if not getattr(launch, "dry_run", True) else "dry_run_not_canary",
            "fallback_reason": selection.fallback_reason,
            "depth": 1 if parent_task_id else 0,
            "terminal_status": "launch_ok" if getattr(launch, "ok", False) else "launch_failed",
            "accepted": False,
            "model_driven_canary": False,
        })

    def _invoke_launch(self, **kwargs) -> LaunchResult:
        adapter = self.runtime_adapter
        task = kwargs.pop("task", None)
        launch_request = getattr(adapter, "launch_request", None)
        if task is not None and callable(launch_request):
            request = TransportRequest.from_task(
                task,
                prompt=kwargs.pop("prompt"),
                cwd=kwargs.pop("cwd", None),
                dry_run=bool(kwargs.pop("dry_run", True)),
                expected_worker_profile=kwargs.get("profile_id"),
            )
            kwargs.pop("profile_id", None)
            kwargs.pop("task_id", None)
            signature = inspect.signature(launch_request)
            if not any(
                param.kind == inspect.Parameter.VAR_KEYWORD
                for param in signature.parameters.values()
            ):
                kwargs = {key: value for key, value in kwargs.items() if key in signature.parameters}
            return cast(LaunchResult, launch_request(request, **kwargs))
        signature = inspect.signature(adapter.launch)
        if not any(param.kind == inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values()):
            kwargs = {key: value for key, value in kwargs.items() if key in signature.parameters}
        return adapter.launch(**kwargs)

    def _assert_spawn_gates(
        self,
        task: TaskContract,
        *,
        approved_provider: str | None,
        approved_model: str | None,
        workspace: str | None,
        timeout_s: int,
        expected_epoch: int | None,
    ) -> None:
        if self.store.replay_error or self.breaker.is_open:
            raise RoutingBlocked(
                "launch_blocked",
                f"fail-closed: {self.store.replay_error or self.breaker.reason or 'dispatch breaker open'}",
            )
        # F-1: quarantine is fail-closed and strictly stronger than task state — a
        # quarantined task must never spawn even if its folded state looks healthy.
        # Release is manual/operator-only; there is no automatic unquarantine.
        if self.store.is_quarantined(str(task.task_id)):
            raise RoutingBlocked("quarantined", f"task {task.task_id} is quarantined")
        if expected_epoch is not None and expected_epoch != task.delegation.assignment_epoch:
            raise RoutingBlocked(
                "stale_epoch",
                f"expected epoch {expected_epoch} != {task.delegation.assignment_epoch}",
            )
        if not isinstance(timeout_s, int) or isinstance(timeout_s, bool) or timeout_s <= 0:
            raise RoutingBlocked("invalid_deadline", "timeout_s must be a positive integer")
        try:
            expired = task.lease_expired()
        except (TypeError, ValueError):
            raise RoutingBlocked("invalid_deadline", "lease deadline is invalid")
        if expired:
            raise RoutingBlocked("lease_expired", "expired lease blocks spawn")
        approval = policy.check_approval(task)
        if not approval.allowed:
            raise RoutingBlocked("approval_required", approval.reason)
        dependencies = policy.check_dependencies(task, self.store.all_tasks())
        if not dependencies.allowed:
            raise RoutingBlocked("dependency_blocked", dependencies.reason)
        if task.retry.exhausted and task.retry.attempts > 0:
            raise RoutingBlocked("attempt_budget_exhausted", "attempt budget exhausted")
        bounded = bool(getattr(self.runtime_adapter, "requires_approved_pair", False))
        if bounded and (not approved_provider or not approved_model):
            raise RoutingBlocked("missing_approved_pair", "explicit approved provider/model pair is required")
        if bounded and self.inference_enforcer is None:
            raise RoutingBlocked(
                "enforcer_unavailable",
                "inference routing enforcer is not available; bounded non-dry-run launch is not permitted",
            )
        # Epoch check must fire before the enforcer evaluates, so that a missing
        # epoch is caught regardless of enforcer outcome (G4 repair).
        if bounded and expected_epoch is None:
            raise RoutingBlocked(
                "epoch_required",
                "expected_epoch is required for bounded non-dry-run launches to prevent stale assignment reuse",
            )
        if bounded and self.inference_enforcer is not None:
            decision = self.inference_enforcer.evaluate(self._routing_context_for_task(task))
            # Validate resolved lane/model first, regardless of allow/deny outcome.
            # This catches the G3 case where the enforcer returns allowed=False
            # (unknown_lane) but we still need to surface the lane/model mismatch.
            resolved_lane = getattr(decision, "resolved_lane", None)
            resolved_model = getattr(decision, "resolved_model", None)
            if resolved_lane and resolved_lane != approved_provider:
                raise RoutingBlocked("unapproved_pair", f"policy-resolved lane '{resolved_lane}' != approved provider '{approved_provider}'")
            if resolved_model and resolved_model != approved_model:
                raise RoutingBlocked("unapproved_pair", f"policy-resolved model '{resolved_model}' != approved model '{approved_model}'")
            if not decision.allowed:
                raise RoutingBlocked(
                    "inference_denied",
                    f"{getattr(decision, 'reason_code', 'denied')}: {getattr(decision, 'reason', '')}",
                )
            # Observe-mode decisions do not authorize bounded non-dry-run execution
            if decision.decision in ("observe_allow", "observe_deny"):
                raise RoutingBlocked(
                    "observe_mode_not_authorizing",
                    f"observe-mode decision ({decision.decision}) does not authorize bounded launch",
                )
        root = Path(workspace).resolve() if workspace else self.store.ledger_path.parent.resolve()
        if root.is_symlink() or not root.is_dir() or root.stat().st_uid != os.getuid():
            raise RoutingBlocked("workspace_unowned", f"workspace {root} is not an owned directory")

    def _remaining_lease_s(self, task: TaskContract, *, now: datetime | None = None) -> float | None:
        """Seconds left on the claim lease, or None when the deadline cannot authorize spawn."""
        claim = task.claim
        if not isinstance(claim, dict):
            return None
        expires = claim.get("lease_expires_at")
        if not isinstance(expires, datetime):
            return None
        now = now or utcnow()
        if expires.tzinfo is None or now.tzinfo is None:
            return None
        try:
            remaining = (expires - now).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(remaining):
            return None
        return remaining

    def _reuse_blocked(self, task: TaskContract) -> bool:
        reservation = task.delegation.launch_reservation or {}
        if reservation.get("workspace_reusable") is False:
            return True
        return reservation.get("cleanup_state") == "uncertain"

    def _persist_cleanup(self, task: TaskContract, cleanup: dict[str, Any]) -> None:
        """Record group cleanup without deleting partial artifacts."""
        reservation = dict(task.delegation.launch_reservation or {})
        state = cleanup.get("cleanup_state")
        if state != "terminated":
            state = "uncertain"
        reservation["cleanup_state"] = state
        reservation["workspace_reusable"] = state == "terminated"
        reservation["partial_artifacts_retained"] = bool(cleanup.get("partial_artifacts_retained", True))
        reservation["state"] = "terminated" if state == "terminated" else "cleanup_uncertain"
        task.delegation.launch_reservation = reservation
        try:
            self.store.put(task, event="launch_cleanup")
        except Exception:
            return

    def _cleanup_if_managed(self, task: TaskContract) -> None:
        reservation = task.delegation.launch_reservation or {}
        handle = dict(reservation.get("process_handle") or {})
        pid = handle.get("pgid") if handle.get("pgid") is not None else handle.get("pid")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return
        if handle.get("pgid") is None:
            handle["pgid"] = pid
        adapter = self.runtime_adapter
        workspace = reservation.get("workspace") or str(self.store.ledger_path.parent)
        cancel_managed = getattr(adapter, "cancel_managed", None)
        if callable(cancel_managed):
            cleanup = cancel_managed(handle=handle, workspace=workspace)
            if not isinstance(cleanup, dict):
                cleanup = {
                    "cleanup_state": "uncertain",
                    "workspace_reusable": False,
                    "partial_artifacts_retained": True,
                }
        else:
            cleanup = {
                "cleanup_state": "uncertain",
                "workspace_reusable": False,
                "partial_artifacts_retained": True,
            }
        self._persist_cleanup(task, cleanup)

    def _record_launch_failure(self, task: TaskContract, launch) -> None:
        cleanup_state = getattr(launch, "cleanup_state", None)
        if cleanup_state not in {"terminated", "uncertain"} and getattr(launch, "blocker_code", None) != "timeout":
            self._quarantine_reservation(task, "spawn_denied")
            return
        state = cleanup_state if cleanup_state == "terminated" else "uncertain"
        reservation = dict(task.delegation.launch_reservation or {})
        reservation["cleanup_state"] = state
        reservation["workspace_reusable"] = state == "terminated"
        reservation["partial_artifacts_retained"] = True
        reservation["state"] = "terminated" if state == "terminated" else "cleanup_uncertain"
        pid = getattr(launch, "process_id", None)
        if isinstance(pid, int) and not isinstance(pid, bool) and pid > 1:
            reservation["process_handle"] = {
                "pid": pid,
                "pgid": pid,
                "disposition": getattr(launch, "disposition", None) or "timeout",
            }
        task.delegation.launch_reservation = reservation
        try:
            self.store.put(task, event="launch_cleanup")
        except Exception:
            return

    def _quarantine_reservation(self, task: TaskContract, state: str) -> None:
        reservation = dict(task.delegation.launch_reservation or {})
        reservation["state"] = state
        reservation["workspace_reusable"] = False
        task.delegation.launch_reservation = reservation
        # D1 repair: the durable quarantine signal is a dedicated "quarantine"
        # control event — never a task-state put. Replay folds control events
        # into quarantined_tasks, which gates both admission and dispatch after
        # restart. The write MUST succeed for the control plane to continue:
        # a failed durable write raises (fail closed); the caller must not
        # acknowledge the operation.
        self.store.append_control_event(
            "quarantine", {"task_id": str(task.task_id), "reason": state},
        )

    def routing_coverage(self) -> list[dict[str, object]]:
        return self.capability_index.coverage()

    def plan_request(
        self,
        request: str,
        *,
        idempotency_key: str,
        risk_tier: str = "r0",
    ) -> PlanRequest:
        """Decompose a request into bounded specialist tasks before dispatch.

        This is the plan-first entrypoint: every task is assigned to an eligible
        specialist and a planning gate validates ownership and workspace conflicts
        before any implementation may start. For a single-fragment atomic request,
        the documented fast path permits direct specialist routing; the reason is
        recorded.
        """
        plan = self.route_planner.plan(
            request,
            idempotency_key=idempotency_key,
            exclude_profiles={self.profile_name},
        )
        # Planning alone never authorizes writes. Tasks stay gated until an
        # explicit dispatch request passes the same eligibility gates. Fast path
        # records why decomposition was unnecessary but does not launch anything.
        if risk_tier in {"r3"} and any(t.workflow == "subagent-driven-development" for t in plan.tasks):
            raise PlanningBlocked("planning_gate_blocked", "r3 implementation requires explicit approval before dispatch")
        return plan

    def plan_specialist_graph(self, *, root_title: str, intents: list[str], max_fanout: int = 3) -> SpecialistGraph:
        if len(intents) > max_fanout:
            raise RoutingBlocked("fanout_exceeded", f"{len(intents)} intents exceeds max_fanout={max_fanout}")
        nodes: list[GraphNode] = []
        selected: set[str] = set()
        for intent in intents:
            selection = self.select_specialist(intent, exclude_profiles={self.profile_name})
            if selection.selected is None:
                raise RoutingBlocked("no_eligible_specialist", selection.reason, rejections=selection_rejections(selection))
            # Duplicate profiles are allowed only when the work item is distinct;
            # record de-dup evidence in the graph by preserving each intent.
            selected.add(selection.selected.profile_id)
            nodes.append(GraphNode(intent, selection.classification, selection.selected.profile_id))
        return SpecialistGraph(tuple(nodes), bounded=True, max_fanout=max_fanout)

    # ------------------------------------------------------------------ plan --

    def _tied_candidates(
        self, selection: SelectionDecision
    ) -> list[str]:
        """Profile ids of candidates tied at the best deterministic score.

        Mirrors the decision layer's tie set: profiles that share the
        top score and are eligible. Order is the natural registry order
        because ``selection.candidates`` is already sorted by score in
        CapabilityIndex.select().
        """
        candidates = list(selection.candidates)
        if not candidates:
            return []
        best_score = candidates[0].score
        return [
            c.profile_id
            for c in candidates
            if c.eligible and c.score == best_score
        ]

    def _build_decision_candidate_records(
        self, profile_ids: list[str]
    ) -> dict[str, CandidateInput]:
        """Map a list of profile ids to ``CandidateInput`` records.

        The decision layer expects a ``candidate_records`` mapping of
        the eligible tied set; we build it from the live capability
        index so the provider can observe every cap / mandate before
        scoring. Missing records are silently dropped: they do not
        contribute to the decision.
        """
        records: dict[str, CandidateInput] = {}
        for cid in profile_ids:
            rec = self.capability_index.record(cid)
            if rec is None:
                continue
            records[cid] = CandidateInput(
                candidate_id=cid,
                capabilities=tuple(rec.capabilities),
                mandate=str(rec.responsibilities or rec.role or cid),
            )
        return records

    def _stable_digest(self, payload: Any) -> str:
        """Stable SHA-256 of a JSON-serializable payload."""
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

    def _stable_registry_digest(self) -> str:
        return self._stable_digest(sorted(self.capability_index.records.keys()))

    def _stable_policy_digest(
        self, routing_context: dict[str, Any] | None
    ) -> str:
        return self._stable_digest(routing_context or {})

    def _stable_rubric_digest(self, verification: Verification) -> str:
        try:
            rendered = asdict(verification)
        except Exception:
            rendered = str(verification)
        return self._stable_digest(rendered)

    def _stable_snapshot_digest(self, selection: SelectionDecision) -> str:
        return self._stable_digest(
            [
                (c.profile_id, c.score, bool(c.eligible))
                for c in selection.candidates
            ]
        )

    def propose(
        self,
        *,
        title: str,
        intent: str,
        assignee_profile: str,
        required_capability: str,
        acceptance_criteria: list[str],
        verification: Verification,
        idempotency_key: str,
        correlation_id: UUID | None = None,
        risk_tier: str = "r0",
        timeout_s: int = 900,
        max_attempts: int = 1,
        depends_on: list[UUID] | None = None,
        irreversible_operations: list[str] | None = None,
        reviewer_profile: str | None = None,
        approval_required: bool | None = None,
        labels: list[str] | None = None,
        parent_task_id: UUID | None = None,
    ) -> TaskContract:
        """Create a durable task contract, or return the existing one.

        Idempotent: re-delivery of a known idempotency_key never duplicates work.
        """
        existing = self.store.find_by_idempotency_key(idempotency_key)
        if existing is not None:
            return existing

        irreversible = irreversible_operations or []
        if approval_required is None:
            approval_required = bool(irreversible) or risk_tier in policy.APPROVAL_REQUIRED_TIERS

        task = TaskContract(
            title=title,
            intent=intent,
            assignee_profile=assignee_profile,
            required_capability=required_capability,
            risk_tier=risk_tier,
            idempotency_key=idempotency_key,
            acceptance_criteria=list(acceptance_criteria),
            verification=verification,
            timeout_s=timeout_s,
            correlation_id=correlation_id or uuid4(),
            retry=Retry(max_attempts=max_attempts),
            approval=Approval(
                required=approval_required,
                state="pending" if approval_required else "not_required",
            ),
            reviewer_profile=reviewer_profile,
            depends_on=list(depends_on or []),
            irreversible_operations=irreversible,
            labels=list(labels or []),
            parent_task_id=parent_task_id,
        )

        # Admission policy runs before anything is persisted.
        policy.validate_admission(task)
        if self.store.replay_error:
            raise PolicyViolation("replay_error", self.store.replay_error)
        try:
            return self.store.put(task, event="task_proposed")
        except ReplayError as exc:
            raise PolicyViolation("replay_error", str(exc)) from exc

    # ------------------------------------------------- adaptive-specialist --

    def preflight_configuration(
        self,
        *,
        task_id: str | UUID,
        classification: Any,
        current_profile_id: str,
        data_classification: str = "public",
        privacy_constraint: Any = None,
        readiness_overrides: dict[str, Any] | None = None,
        allow_unknown_quota: bool = False,
    ) -> Any:
        """Compute and bind a typed preflight configuration decision.

        This is the Phase B offline equivalent of a per-task admission gate.
        It is bound to the existing canonical task/attempt lifecycle: the
        returned proposal is appended to the task's ``preflight_evidence``
        field and persisted via ``store.put(task, event="preflight_proposed")``
        so the same decision is reproduced across replays.

        The decision is computed against a :class:`CapabilitySnapshot` derived
        from the canonical roster, and an in-memory
        :class:`LocalReadinessRegistry`. It does not call any provider, does
        not refresh credentials, and does not mutate installed profiles.

        Parameters mirror the slices defined in
        ``test_adaptive_specialist_preflight.py``::

            task_id               the canonical task identifier
            classification        a ``Classification`` (specialist_routing)
                                  — only ``task_class`` is consulted
            current_profile_id    the runtime profile to evaluate against
            data_classification   "public" | "internal" | "internal_redacted"
                                  | "confidential" (offline policy hook)
            privacy_constraint    optional ``PrivacyConstraint`` to enforce
            readiness_overrides   optional dict of provider -> readiness
                                  evidence (each a RouteReadinessEvidence)

        Returns the immutable :class:`TaskConfigurationProposal`.
        """
        from app import adaptive_specialist
        from app.adaptive_specialist import (
            CapabilitySnapshot,
            LocalReadinessRegistry,
            PrivacyConstraint,
            ProviderFamilyMap,
            TaskConfigurationPrecomputer,
        )
        from app.models import Approval

        task = self._require(task_id)
        snapshot = CapabilitySnapshot.build(
            repo_root=Path(__file__).resolve().parents[3]
        )
        readiness = LocalReadinessRegistry.synthetic(
            provider="default-synthetic",
            auth_eligible=False,
        )
        families = ProviderFamilyMap.from_snapshot(snapshot)
        engine = TaskConfigurationPrecomputer(
            snapshot=snapshot,
            readiness=readiness,
            families=families,
        )
        # ``data_classification`` is advisory here; the proposal converts to
        # typed :class:`PrivacyConstraint` only when explicitly requested.
        constraint = privacy_constraint
        if isinstance(privacy_constraint, str):
            try:
                constraint = PrivacyConstraint.from_label(privacy_constraint)
            except ValueError:
                constraint = None
        proposal = engine.evaluate(
            task_class=getattr(classification, "task_class", "general"),
            risk_tier=task.risk_tier,
            data_classification=data_classification,
            privacy_constraint=constraint,
            current_profile_id=current_profile_id,
            readiness_overrides=readiness_overrides or {},
            allow_unknown_quota=allow_unknown_quota,
        )
        # Build the persisted evidence record. The orchestrator is the only
        # authority on its own tasks: it stamps the decision onto the task
        # ledger via the canonical ``store.put`` call.
        #
        # Remediation B1 (Skeptic §3.B / important): ``preflight_evidence``
        # is keyed per ``attempt_epoch`` and accumulated append-only via
        # ``store.put`` events. The current ``task.delegation.attempt_epoch``
        # (or sentinel ``0`` before the first attempt is granted) selects
        # which bucket this entry is appended to. Replays of the same task
        # therefore see the full per-epoch decision history rather than
        # one overwritten envelope. ``submit_candidate`` then revalidates
        # evidence against the current ``attempt_epoch`` to refuse stale
        # preflight reuse across attempts.
        evidence = {
            "attempt_epoch": int(
                task.delegation.attempt_epoch
                if task.delegation.attempt_epoch is not None
                else 0
            ),
            "recorded_at": utcnow().isoformat(),
            "decision": proposal.decision.value,
            "profile_id": proposal.profile_id,
            "selected_provider_family": proposal.selected_provider_family,
            "reasons": list(proposal.reasons),
            "unknowns": list(proposal.unknowns),
            "bundle_digest": proposal.bundle_digest,
            "evidence_digest": proposal.evidence_digest,
            "capability_snapshot_digest": proposal.capability_snapshot_digest,
            "readiness_digests": dict(proposal.readiness_digests),
            "task_class": getattr(classification, "task_class", "general"),
            "data_classification": data_classification,
        }
        epoch = int(
            task.delegation.attempt_epoch
            if task.delegation.attempt_epoch is not None
            else 0
        )
        history = task.preflight_evidence or {}
        bucket = list(history.get(epoch, ()))
        bucket.append(evidence)
        history[epoch] = bucket
        task.preflight_evidence = history
        persisted = self.store.put(task, event="preflight_proposed")
        # The orchestrator returns the persisted task (with ``preflight_evidence``
        # field set) plus the in-memory proposal object so a caller that wants
        # to read the typed proposal directly does not need to re-parse the
        # ledger. The two views are guaranteed to carry the same
        # ``bundle_digest``/``evidence_digest`` since both derive from the
        # same :class:`TaskConfigurationProposal`.
        class _BoundProposal:
            def __init__(self, task, proposal):
                self.task = task
                self.proposal = proposal

            @property
            def decision(self):
                return self.proposal.decision

            @property
            def bundle_digest(self):
                return self.proposal.bundle_digest

            @property
            def evidence_digest(self):
                return self.proposal.evidence_digest

            @property
            def profile_id(self):
                return self.proposal.profile_id

            @property
            def selected_provider_family(self):
                return self.proposal.selected_provider_family

            @property
            def reasons(self):
                return self.proposal.reasons

            @property
            def unknowns(self):
                return self.proposal.unknowns

            @property
            def capability_snapshot_digest(self):
                return self.proposal.capability_snapshot_digest

            @property
            def readiness_digests(self):
                return self.proposal.readiness_digests

        return _BoundProposal(persisted, proposal)

    def preflight_then_route(
        self,
        *,
        title: str,
        intent: str,
        acceptance_criteria: list[str],
        verification: Verification,
        idempotency_key: str,
        required_capability: str | None = None,
        risk_tier: str = "r0",
        dry_run: bool = True,
        allow_generic_fallback: bool = False,
        approved_provider: str | None = None,
        approved_model: str | None = None,
        workspace: str | None = None,
        allow_unknown_quota: bool = False,
    ) -> Any:
        """Plugin-facing convenience: plan-first, then route via ordered preflight.

        B2 (contract §3.1): this is the canonical entrypoint for the
        plugin. It enters through ``route_with_preflight``, which orders
        ``select_specialist`` → ``propose`` → ``preflight_configuration``
        → decision validation → freshness revalidation →
        ``submit_candidate`` → spawn. The previous "oracle-guess the
        future selected profile" justification is invalid: selection
        runs before propose, so the preflight takes the *actual*
        selected profile as input.

        ``allow_unknown_quota`` (B2 §3.4) is forwarded to the preflight
        evaluator so a route whose deterministic fields are READY but
        whose quota is UNKNOWN can proceed only with explicit opt-in.
        """
        return self.route_with_preflight(
            title=title,
            intent=intent,
            acceptance_criteria=acceptance_criteria,
            verification=verification,
            idempotency_key=idempotency_key,
            required_capability=required_capability,
            risk_tier=risk_tier,
            dry_run=dry_run,
            allow_generic_fallback=allow_generic_fallback,
            approved_provider=approved_provider,
            approved_model=approved_model,
            workspace=workspace,
            allow_unknown_quota=allow_unknown_quota,
        )

    def route_with_preflight(
        self,
        *,
        title: str,
        intent: str,
        acceptance_criteria: list[str],
        verification: Verification,
        idempotency_key: str,
        required_capability: str | None = None,
        correlation_id: UUID | None = None,
        risk_tier: str = "r0",
        timeout_s: int = 900,
        max_attempts: int = 1,
        depends_on: list[UUID] | None = None,
        irreversible_operations: list[str] | None = None,
        reviewer_profile: str | None = None,
        approval_required: bool | None = None,
        parent_task_id: UUID | str | None = None,
        max_depth: int = 1,
        allow_generic_fallback: bool = False,
        dry_run: bool = True,
        inference_routing_context: dict[str, str] | None = None,
        approved_provider: str | None = None,
        approved_model: str | None = None,
        workspace: str | None = None,
        expected_epoch: int | None = None,
        decision_layer: DecisionLayer | None = None,
        allow_unknown_quota: bool = False,
    ) -> SpecialistRouteResult:
        """End-to-end specialist route through the minimum-safe ordering.

        Order: ``select_specialist`` → ``propose`` →
        ``preflight_configuration`` → typed decision validation →
        freshness revalidation → ``submit_candidate`` →
        ``_assert_spawn_gates`` + ``_invoke_launch``. The previous
        "oracle-guess the future selected profile" rationale for
        skipping the preflight is invalid: selection runs first, so the
        preflight takes the actual selected profile and exact proposed
        route as input. No field is left empty; ``PreflightContext``
        carries the full selected-route context to the evaluator.

        ``allow_unknown_quota`` (B2 §3.4) is forwarded so a READY_QUALIFIED
        route can proceed only with explicit opt-in. A missing
        ``preflight_evidence`` for the current ``attempt_epoch`` blocks
        the launch with a typed ``RoutingBlocked``.
        """
        if depth_exceeded(parent_task_id, max_depth):
            raise RoutingBlocked("depth_exceeded", "subordinate orchestration depth/fanout limit exceeded")
        if self.store.replay_error or self.breaker.is_open:
            raise RoutingBlocked(
                "launch_blocked",
                f"fail-closed: {self.store.replay_error or self.breaker.reason or 'dispatch breaker open'}",
            )
        selection = self.select_specialist(
            intent,
            required_capability=required_capability,
            risk_tier=risk_tier,
            allow_generic_fallback=allow_generic_fallback,
            exclude_profiles={self.profile_name},
        )
        if selection.selected is None:
            raise RoutingBlocked(
                "no_eligible_specialist",
                selection.reason,
                rejections=selection_rejections(selection),
            )
        selected = selection.selected
        if selected.profile_id == self.profile_name:
            raise RoutingBlocked("self_delegation_blocked", "noesis-orchestrator is not an ordinary worker target")
        labels = [selection.classification.task_class]
        task_capability = selection.classification.required_capability
        if selected.profile_id == "coder" and task_capability == "code_modify":
            task_capability = "implement"
        task = self.propose(
            title=title,
            intent=intent,
            assignee_profile=selected.profile_id,
            required_capability=task_capability,
            acceptance_criteria=acceptance_criteria,
            verification=verification,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            risk_tier=risk_tier,
            timeout_s=timeout_s,
            max_attempts=max_attempts,
            depends_on=depends_on,
            irreversible_operations=irreversible_operations,
            reviewer_profile=reviewer_profile or selection.classification.reviewer_required,
            approval_required=approval_required,
            labels=labels,
            parent_task_id=UUID(str(parent_task_id)) if parent_task_id else None,
        )
        if inference_routing_context:
            self._inference_routing_context[str(task.task_id)] = dict(inference_routing_context)
        # The preflight runs AFTER propose so the canonical task/attempt
        # ledger has a stable record to bind the digests to. The preflight
        # is invoked against the selected profile and the exact proposed
        # route (provider/model/workspace) so no field is empty.
        try:
            proposal = self.preflight_configuration(
                task_id=task.task_id,
                classification=selection.classification,
                current_profile_id=selected.profile_id,
                data_classification="public_data",
                privacy_constraint=None,
                allow_unknown_quota=allow_unknown_quota,
            )
        except PolicyViolation as exc:
            raise RoutingBlocked(exc.code, str(exc)) from exc
        # ``preflight_configuration`` returns the canonical task
        # (``proposal.task``) carrying the persisted evidence; rebind
        # the local ``task`` to it so the dry-run return value and any
        # post-preflight operations see the evidence on the task.
        task = proposal.task
        decision = proposal.decision
        # B2 §3.7: typed decision validation. Each non-KEEP decision
        # raises a typed ``RoutingBlocked`` with the contract's
        # specific code path.
        self._validate_preflight_decision(
            decision=decision,
            reasons=proposal.reasons,
            unknowns=proposal.unknowns,
            profile_id=selected.profile_id,
        )
        # ``KEEP`` falls through. The preflight decision is recorded on the
        # canonical ledger; freshness is implicit because the decision is
        # bound to the current attempt_epoch.
        prompt = make_child_prompt(task, selection)
        if dry_run:
            launch = self._invoke_launch(
                task=task,
                profile_id=selected.runtime_profile,
                prompt=prompt,
                task_id=str(task.task_id),
                cwd=None,
                dry_run=True,
            )
            self._emit_launch_event(task, selection, launch, parent_task_id)
            if not launch.ok:
                raise RoutingBlocked("launch_failed", launch.error or "launcher failed without an error")
            if launch.loaded_profile != selected.runtime_profile:
                raise RoutingBlocked(
                    "loaded_profile_mismatch",
                    f"requested '{selected.runtime_profile}' but loaded '{launch.loaded_profile}'",
                )
            return SpecialistRouteResult(False, selection, task, launch, selection.fallback_reason)

        # B2 §3.4 + §3.7: the preflight evidence is bound to the
        # current ``attempt_epoch``; the gate refuses to launch when
        # the current epoch has no fresh ``KEEP`` decision.
        attempt_epoch = int(
            task.delegation.attempt_epoch
            if task.delegation.attempt_epoch is not None
            else 0
        )
        evidence_decision = policy.check_preflight_evidence(
            task,
            current_attempt_epoch=attempt_epoch,
        )
        if not evidence_decision.allowed:
            raise RoutingBlocked(
                "preflight_evidence_blocked",
                evidence_decision.reason,
            )
        self._assert_spawn_gates(
            task,
            approved_provider=approved_provider,
            approved_model=approved_model,
            workspace=workspace,
            timeout_s=timeout_s,
            expected_epoch=expected_epoch,
        )
        workspace_path = str(Path(workspace).resolve()) if workspace else str(self.store.ledger_path.parent.resolve())
        reservation = {
            "attempt": 1,
            "runtime_profile": selected.runtime_profile,
            "approved_provider": approved_provider,
            "approved_model": approved_model,
            "fallback_policy": "deny",
            "admission": {"allowed": True, "reason": "gates_passed"},
            "deadline_s": timeout_s,
            "workspace": workspace_path,
            "workspace_reusable": True,
            "preflight_decision": decision.value,
            "preflight_evidence_digest": proposal.evidence_digest,
        }
        try:
            task, created_now = self.store.reserve_launch(task, reservation)
        except IdentityError as exc:
            raise RoutingBlocked(getattr(exc, "code", "identity_invalid"), str(exc))
        except Exception:
            raise
        if not created_now:
            return SpecialistRouteResult(False, selection, task, None, "reservation_exists_no_respawn")
        remaining = self._remaining_lease_s(task)
        if remaining is None or remaining <= 0:
            self._quarantine_reservation(task, "deadline_invalid" if remaining is None else "lease_expired")
            raise RoutingBlocked(
                "invalid_deadline" if remaining is None else "lease_expired",
                "remaining lease is missing, invalid, or not positive; refusing spawn",
            )
        if not task.delegation.contract_revision or not task.delegation.baseline:
            self._quarantine_reservation(task, "missing_identity")
            raise RoutingBlocked(
                "missing_identity",
                "authoritative contract_revision/baseline is missing; refusing spawn",
            )
        capped_timeout = min(float(timeout_s), remaining)
        try:
            launch = self._invoke_launch(
                task=task,
                profile_id=selected.runtime_profile,
                prompt=prompt,
                task_id=str(task.task_id),
                cwd=workspace_path,
                dry_run=False,
                provider=approved_provider,
                model=approved_model,
                timeout_s=capped_timeout,
                remaining_lease_s=remaining,
                workspace=workspace_path,
                fallback_policy="deny",
                attempt_id=str(task.delegation.attempt),
                assignment_epoch=task.delegation.assignment_epoch,
                contract_revision=task.delegation.contract_revision,
                baseline=task.delegation.baseline,
            )
        except Exception:
            self._quarantine_reservation(task, "handle_uncertain")
            raise
        self._emit_launch_event(task, selection, launch, parent_task_id)
        if not launch.ok:
            self._record_launch_failure(task, launch)
            raise RoutingBlocked(
                getattr(launch, "blocker_code", None) or "launch_failed",
                launch.error or "launcher failed without an error",
            )
        if launch.loaded_profile != selected.runtime_profile:
            self._quarantine_reservation(task, "profile_mismatch")
            raise RoutingBlocked(
                "loaded_profile_mismatch",
                f"requested '{selected.runtime_profile}' but loaded '{launch.loaded_profile}'",
            )
        reported_provider = getattr(launch, "provider", approved_provider)
        reported_model = getattr(launch, "model", approved_model)
        if approved_provider and reported_provider not in (None, approved_provider):
            raise RoutingBlocked("provider_mismatch", f"requested {approved_provider} but launched {reported_provider}")
        if approved_model and reported_model not in (None, approved_model):
            raise RoutingBlocked("model_mismatch", f"requested {approved_model} but launched {reported_model}")
        handle = {
            "pid": getattr(launch, "process_id", None),
            "pgid": getattr(launch, "process_id", None),
            "loaded_profile": launch.loaded_profile,
            "provider": reported_provider,
            "model": reported_model,
            "candidate_digest": getattr(launch, "candidate_digest", None),
            "disposition": getattr(launch, "disposition", None),
            "usage_tokens": getattr(launch, "usage_tokens", None),
        }
        task.delegation.launch_reservation = dict(task.delegation.launch_reservation or {})
        task.delegation.launch_reservation.update(
            {
                "state": "spawned",
                "process_handle": handle,
                "workspace_reusable": True,
                "candidate_digest": getattr(launch, "candidate_digest", None),
                "verification_pending": getattr(launch, "verification_pending", False),
                "accepted": False,
                "sorted": sorted(launch.artifacts or ()),
                "stdout_digest": getattr(launch, "stdout_digest", None),
                "collection_error": getattr(launch, "collection_error", None),
                "attempt_id": getattr(launch, "attempt_id", None),
            }
        )
        task = self.store.put(task, event="launch_handle_persisted")
        return SpecialistRouteResult(False, selection, task, launch, selection.fallback_reason)

    def _validate_preflight_decision(
        self,
        *,
        decision: PreflightDecision,
        reasons: tuple[str, ...],
        unknowns: tuple[str, ...],
        profile_id: str,
    ) -> None:
        """Map a typed preflight decision to a typed RoutingBlocked.

        B2 §3.7: each non-KEEP decision raises a typed refusal so the
        dispatcher can refuse to launch without silently coercing the
        decision. ``KEEP`` returns silently. ``TASK_OVERRIDE`` and
        ``SELECT_ALTERNATIVE`` mention ``apply_preflight_override`` so
        operators know the next supported action.
        """
        if decision is PreflightDecision.BLOCK:
            raise RoutingBlocked(
                "preflight_blocked",
                (
                    f"preflight returned BLOCK for profile '{profile_id}' "
                    f"(reasons: {list(reasons)})"
                ),
            )
        if decision is PreflightDecision.TASK_OVERRIDE:
            raise RoutingBlocked(
                "preflight_task_override",
                (
                    f"preflight returned TASK_OVERRIDE for profile "
                    f"'{profile_id}'; an operator-staged override is "
                    f"required via apply_preflight_override (reasons: "
                    f"{list(reasons)})"
                ),
            )
        if decision is PreflightDecision.SELECT_ALTERNATIVE:
            raise RoutingBlocked(
                "preflight_select_alternative",
                (
                    f"preflight returned SELECT_ALTERNATIVE for profile "
                    f"'{profile_id}'; an operator-staged alternative "
                    f"is required via apply_preflight_override (reasons: "
                    f"{list(reasons)})"
                ),
            )
        if decision is PreflightDecision.REQUIRE_APPROVAL:
            raise RoutingBlocked(
                "preflight_require_approval",
                (
                    f"preflight returned REQUIRE_APPROVAL for profile "
                    f"'{profile_id}' (reasons: {list(reasons)}, "
                    f"missing_evidence: {list(unknowns)})"
                ),
            )
        # ``KEEP`` falls through silently.

    def apply_preflight_override(
        self,
        task: TaskContract,
        *,
        override_decision: PreflightDecision,
        proposed_override: dict[str, Any] | None = None,
        proposed_alternative: dict[str, Any] | None = None,
        operator: str,
    ) -> TaskContract:
        """Record an operator-staged preflight override on the canonical task.

        B2 §3.7: when ``preflight_configuration`` returns
        ``TASK_OVERRIDE`` or ``SELECT_ALTERNATIVE``, an authenticated
        operator may stage a typed override via this method. The
        override is appended to ``preflight_evidence`` for the current
        ``attempt_epoch``; the digests are recomputed and the canonical
        ``store.put`` records the override as a durable event. The
        caller is responsible for re-running preflight (via
        ``preflight_configuration``) before relaunching.
        """
        if override_decision not in {
            PreflightDecision.TASK_OVERRIDE,
            PreflightDecision.SELECT_ALTERNATIVE,
        }:
            raise PolicyViolation(
                "preflight_override_invalid",
                "apply_preflight_override only accepts TASK_OVERRIDE or SELECT_ALTERNATIVE",
            )
        if not operator or not isinstance(operator, str) or not operator.strip():
            raise PolicyViolation(
                "preflight_override_invalid",
                "operator identity is required to stage an override",
            )
        if override_decision is PreflightDecision.TASK_OVERRIDE and not proposed_override:
            raise PolicyViolation(
                "preflight_override_invalid",
                "TASK_OVERRIDE requires a proposed_override payload",
            )
        if override_decision is PreflightDecision.SELECT_ALTERNATIVE and not proposed_alternative:
            raise PolicyViolation(
                "preflight_override_invalid",
                "SELECT_ALTERNATIVE requires a proposed_alternative payload",
            )
        epoch = int(
            task.delegation.attempt_epoch
            if task.delegation.attempt_epoch is not None
            else 0
        )
        history = task.preflight_evidence or {}
        bucket = list(history.get(epoch, ()))
        # Recompute the override digests so a replay can verify the
        # override was applied to the same preflight snapshot.
        override_payload = {
            "decision": override_decision.value,
            "operator": operator,
            "proposed_override": proposed_override or {},
            "proposed_alternative": proposed_alternative or {},
            "applied_at": utcnow().isoformat(),
        }
        bundle_digest = hashlib.sha256(
            json.dumps(override_payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        envelope = {
            "attempt_epoch": epoch,
            "decision": override_decision.value,
            "applied_by": operator,
            "proposed_override": proposed_override or {},
            "proposed_alternative": proposed_alternative or {},
            "bundle_digest": bundle_digest,
            "evidence_digest": bundle_digest,
            "recorded_at": utcnow().isoformat(),
        }
        bucket.append(envelope)
        history[epoch] = bucket
        task.preflight_evidence = history
        return self.store.put(task, event="preflight_override_applied")

    # -------------------------------------------------------------- approval --

    def approve(
        self,
        task_id: str | UUID,
        *,
        operator: str,
        approval_manifest_id: str | None = None,
    ) -> TaskContract:
        """Record a human approval. Agents may never call this on their own behalf."""
        task = self._require(task_id)
        if task.approval and task.approval.required:
            task.approval.state = "granted"
            task.approval.approved_by = operator
            task.approval.approved_at = utcnow()
            task.approval.approval_manifest_id = approval_manifest_id
        decision = policy.check_approval(task)
        if not decision.allowed:
            raise PolicyViolation("approval_invalid", decision.reason)
        task.transition_to("approved")
        return self.store.put(task, event="task_approved")

    def deny(self, task_id: str | UUID, *, operator: str, reason: str) -> TaskContract:
        task = self._require(task_id)
        if task.approval:
            task.approval.state = "denied"
            task.approval.approved_by = operator
            task.approval.approved_at = utcnow()
        task.transition_to("cancelled", reason=f"approval denied by {operator}: {reason}")
        return self.store.put(task, event="task_denied")

    def enqueue(self, task_id: str | UUID) -> TaskContract:
        """Move an approved (or approval-exempt) task into the dispatch queue."""
        task = self._require(task_id)
        if task.state == "proposed":
            decision = policy.check_approval(task)
            if not decision.allowed:
                raise PolicyViolation("approval_gate_required", decision.reason)
            task.transition_to("approved")
        task.transition_to("queued")
        return self.store.put(task, event="task_queued")

    # ------------------------------------------------------------- dispatch --

    def dispatchable(self, task_id: str | UUID) -> DispatchDecision:
        task = self._require(task_id)
        if self.breaker.is_open:
            return DispatchDecision(False, f"circuit breaker open: {self.breaker.reason}")
        if task.state != "queued":
            return DispatchDecision(False, f"task is '{task.state}', not 'queued'")
        decision = policy.check_dispatchable(task, self.store.all_tasks())
        if not decision.allowed:
            return decision
        if self.inference_enforcer is not None:
            route_ctx = self._routing_context_for_task(task)
            routing_decision = self.inference_enforcer.evaluate(route_ctx)
            if not routing_decision.allowed:
                return DispatchDecision(
                    False,
                    f"inference routing denied: {routing_decision.reason_code}: {routing_decision.reason}",
                )
        return decision

    def _routing_context_for_task(self, task: TaskContract) -> RoutingContext:
        """Build the non-sensitive routing context for pre-dispatch evaluation."""
        overrides = self._inference_routing_context.get(str(task.task_id), {})
        task_class = overrides.get("task_class") or task.labels[0] if task.labels else "general"
        data_classification = (
            overrides.get("data_classification")
            or ("internal_redacted" if task.risk_tier in {"r0", "r1"} else "internal")
        )
        return RoutingContext(
            task_id=str(task.task_id),
            profile_id=task.assignee_profile,
            task_class=task_class,
            risk_tier=task.risk_tier,
            data_classification=data_classification,
            requested_lane=overrides.get("requested_lane"),
            requested_model=overrides.get("requested_model"),
            approval_granted=bool(
                task.approval and task.approval.required and task.approval.state == "granted"
            ),
            approval_manifest_id=(
                task.approval.approval_manifest_id if task.approval else None
            ),
            rollback_plan_present=bool(task.handoff and task.handoff.get("rollback_plan")),
            independent_review_provider_family=overrides.get("independent_review_provider_family"),
            tool_scope=tuple(overrides.get("tool_scope", "").split(","))
            if overrides.get("tool_scope")
            else (),
        )

    def check_gate_blocked(self, task_id: str | UUID) -> bool:
        """True when a task is held by an unsatisfied human approval gate.

        Inspection helper for operators and dashboards; performs no mutation.
        """
        task = self._require(task_id)
        return not policy.check_approval(task).allowed

    def claim(self, task_id: str | UUID, *, claimed_by: str) -> TaskContract:
        """Hand the task to its assignee. Rejects any claim by a different profile."""
        task = self._require(task_id)
        decision = self.dispatchable(task_id)
        if not decision.allowed:
            raise PolicyViolation("dispatch_blocked", decision.reason)
        if claimed_by != task.assignee_profile:
            raise PolicyViolation(
                "claim_by_wrong_profile",
                f"task is assigned to '{task.assignee_profile}', not '{claimed_by}'",
            )
        task.transition_to("claimed")
        task.apply_claim(claimed_by)
        return self.store.put(task, event="task_claimed")

    def start(self, task_id: str | UUID) -> TaskContract:
        task = self._require(task_id)
        task.transition_to("running")
        task.retry.attempts += 1
        if task.delegation.attempt_epoch is None:
            task.delegation.attempt_epoch = 1
        return self.store.put(task, event="task_started")

    # ------------------------------------------------------------- outcomes --

    def submit_handoff(self, task_id: str | UUID, handoff: dict[str, Any]) -> TaskContract:
        """Assignee reports structured evidence. This does not itself succeed the task."""
        task = self._require(task_id)
        task.handoff = handoff
        next_state = "awaiting_review" if task.reviewer_profile else "running"
        if task.state != next_state:
            task.transition_to(next_state)
        return self.store.put(task, event="task_handoff_submitted")

    # --------------------------------------------------------- delegation --

    def mark_dispatched(
        self,
        task_id: str | UUID,
        *,
        runtime_profile: str,
        attempt: int = 1,
        request_limit: int | None = None,
        budget_tokens: int | None = None,
    ) -> TaskContract:
        """Advance the authoritative task through assignment into ``running``.

        This is the only place a routed task leaves the contract ledger's
        propose/queue states into an explicit, active assignment. It walks the
        lifecycle proposed->approved->queued->claimed->running, applies a lease,
        and records the monotonic assignment epoch + resolved runtime profile.
        Backend/launch identity is stored here, not inferred from prompts.
        """
        task = self._require(task_id)
        if task.state == "proposed":
            decision = policy.check_approval(task)
            if not decision.allowed:
                raise PolicyViolation("approval_gate_required", decision.reason)
            task.transition_to("approved")
        if task.state == "approved":
            task.transition_to("queued")
        if task.state == "queued":
            task.transition_to("claimed")
            if task.claim is None:
                task.apply_claim(runtime_profile)
        if task.state == "claimed":
            if task.lease_expired():
                raise PolicyViolation("lease_expired", "claim lease expired before dispatch")
            task.transition_to("running")
        elif task.state != "running":
            raise PolicyViolation("not_dispatchable", f"task is '{task.state}', cannot mark dispatched")
        task.delegation.assignment_epoch += 1
        task.delegation.attempt = attempt
        task.delegation.runtime_profile = runtime_profile
        if request_limit is not None:
            task.delegation.request_limit = request_limit
        if budget_tokens is not None:
            task.delegation.budget_tokens = budget_tokens
        if task.delegation.attempt_epoch is None:
            task.delegation.attempt_epoch = 1
        return self.store.put(task, event="task_dispatched")

    def submit_candidate(
        self,
        task_id: str | UUID,
        *,
        candidate_revision: str,
        candidate_digest: str | None = None,
        attempt_epoch: int | None = None,
    ) -> TaskContract:
        """Record the specialist's candidate revision on the authoritative task.

        A candidate change automatically invalidates reviews bound to an older
        revision, so an approval can never apply to a stale diff. Submission
        releases execution ownership for the current attempt_epoch. A different
        revision or digest never mints a new attempt; only grant_new_attempt
        increments attempt_epoch. Workers never invent an epoch from absence;
        attempt_epoch is always required and must match the orchestrator-authored
        current epoch. Submit must match any granted candidate revision/digest.
        """
        task = self._require(task_id)
        if task.delegation.execution_revoked:
            raise PolicyViolation(
                "execution_revoked",
                "revoked execution authority cannot submit a candidate",
            )
        if task.state not in ("running", "awaiting_review"):
            raise PolicyViolation(
                "not_submitting",
                f"task is '{task.state}', candidate requires running (or awaiting_review for remediation)",
            )
        prior_revision = task.delegation.candidate_revision
        prior_digest = task.delegation.candidate_digest
        revision_changed = bool(prior_revision) and prior_revision != candidate_revision
        # Same label with a different digest must not retain approval.
        digest_changed = prior_revision is not None and prior_digest != candidate_digest
        current_epoch = task.delegation.attempt_epoch
        released = task.delegation.execution_released_at
        if current_epoch is None:
            raise PolicyViolation(
                "stale_attempt_report",
                "attempt_epoch is required",
            )
        if attempt_epoch is None:
            raise PolicyViolation(
                "stale_attempt_report",
                "attempt_epoch is required",
            )
        if current_epoch != attempt_epoch:
            raise PolicyViolation(
                "stale_attempt_report",
                f"attempt_epoch {attempt_epoch} != current {current_epoch}",
            )
        # Remediation B1 (Skeptic §3.B / important): revalidate per-attempt
        # preflight evidence. ``preflight_evidence`` is keyed per
        # ``attempt_epoch`` and accumulated append-only.
        #
        # The §3.B "stale evidence reused without re-preflight" scenario
        # fires when evidence was recorded for a *different* epoch and
        # would be reused at the current one. The submit gate therefore
        # rejects when:
        #
        #   * the current epoch has a bucket whose latest decision is
        #     not ``KEEP`` (a stale non-KEEP must not be silently
        #     upgraded);
        #   * evidence exists for other epochs but the current epoch is
        #     empty (the cross-epoch reuse the Skeptic flagged).
        #
        # When no preflight has ever been recorded (``preflight_evidence``
        # is ``None`` or empty), the submit path falls through to its
        # existing pre-remediation behaviour for backward compatibility
        # with operators that bypass preflight on legacy task shapes.
        # The cross-epoch reuse gate above catches the §3.B scenario
        # whether the legacy shape predates this remediation or not.
        history = task.preflight_evidence
        if history:
            current_bucket = history.get(int(attempt_epoch))
            if current_bucket:
                latest_entry = current_bucket[-1]
                latest_decision = (
                    latest_entry.get("decision")
                    if isinstance(latest_entry, dict)
                    else None
                )
                if latest_decision != "KEEP":
                    raise PolicyViolation(
                        "stale_preflight_evidence",
                        (
                            f"latest preflight decision for attempt_epoch="
                            f"{attempt_epoch} is '{latest_decision}'; "
                            "submit_candidate requires a KEEP decision"
                        ),
                    )
            else:
                # Evidence recorded under other epochs but not the current
                # one — the §3.B stale-reuse scenario.
                raise PolicyViolation(
                    "stale_preflight_evidence",
                    (
                        f"preflight evidence for attempt_epoch={attempt_epoch} is "
                        f"missing; recorded epochs={sorted(history.keys())}"
                    ),
                )
        if released is not None and (revision_changed or digest_changed):
            raise PolicyViolation(
                "stale_attempt_report",
                "execution already released for this epoch",
            )
        granted_rev = task.delegation.granted_candidate_revision
        granted_dig = task.delegation.granted_candidate_digest
        if granted_rev is not None and granted_rev != candidate_revision:
            raise PolicyViolation(
                "granted_candidate_mismatch",
                f"candidate_revision '{candidate_revision}' != granted '{granted_rev}'",
            )
        if granted_dig is not None and granted_dig != candidate_digest:
            raise PolicyViolation(
                "granted_candidate_mismatch",
                f"candidate_digest '{candidate_digest}' != granted '{granted_dig}'",
            )
        if revision_changed or digest_changed:
            task.delegation.reviews = [] if digest_changed else [
                r
                for r in task.delegation.reviews
                if r.candidate_revision == candidate_revision
            ]
        task.delegation.candidate_revision = candidate_revision
        task.delegation.candidate_digest = candidate_digest
        if task.delegation.execution_released_at is None:
            task.delegation.execution_released_at = utcnow()
        next_state = "awaiting_review" if task.reviewer_profile else "running"
        if task.state != next_state:
            task.transition_to(next_state)
        try:
            return self.store.put(
                task, event="candidate_submitted", expected_task_version=task.task_version
            )
        except TaskVersionConflict as exc:
            raise PolicyViolation("task_version_conflict", str(exc)) from exc

    def grant_new_attempt(
        self,
        task_id: str | UUID,
        *,
        operator: str = "noesis-orchestrator",
        expected_task_version: int,
        intended_assignee: str | None = None,
        intended_candidate_revision: str | None = None,
        intended_candidate_digest: str | None = None,
    ) -> TaskContract:
        """Orchestrator-controlled new attempt. Increments attempt_epoch.

        `operator` is an in-process caller label compared to the allowlisted
        string "noesis-orchestrator". It is not authentication. Production
        must not expose this method to untrusted callers.
        """
        task = self._require(task_id)
        if operator != "noesis-orchestrator":
            raise PolicyViolation("unauthorized_actor", "only the orchestrator grants a new attempt")
        if expected_task_version != task.task_version:
            raise PolicyViolation(
                "task_version_conflict",
                f"expected {expected_task_version}, got {task.task_version}",
            )
        if intended_assignee is not None and intended_assignee != task.assignee_profile:
            raise PolicyViolation("assignee_mismatch", "intended assignee does not match task assignee")
        if task.delegation.execution_revoked:
            raise PolicyViolation(
                "execution_revoked",
                "revoked execution authority cannot be re-granted",
            )
        if task.state not in ("running", "awaiting_review"):
            raise PolicyViolation(
                "not_grantable",
                f"task is '{task.state}', cannot grant a new attempt",
            )
        current = task.delegation.attempt_epoch or 0
        task.delegation.attempt_epoch = current + 1
        task.delegation.execution_released_at = None
        task.delegation.reviews = []
        if intended_candidate_revision is not None:
            task.delegation.granted_candidate_revision = intended_candidate_revision
            task.delegation.candidate_revision = intended_candidate_revision
            task.delegation.candidate_digest = intended_candidate_digest
            task.delegation.granted_candidate_digest = intended_candidate_digest
        elif intended_candidate_digest is not None:
            task.delegation.granted_candidate_digest = intended_candidate_digest
        try:
            return self.store.put(
                task, event="new_attempt_granted", expected_task_version=expected_task_version
            )
        except TaskVersionConflict as exc:
            raise PolicyViolation("task_version_conflict", str(exc)) from exc

    def record_review(
        self,
        task_id: str | UUID,
        *,
        stage: str,
        verdict: str,
        reviewer: str,
        candidate_revision: str,
        findings: list[dict] | None = None,
    ) -> TaskContract:
        """Persist a review verdict bound to the current candidate revision.

        Spec for a candidate MUST pass before that candidate's quality review may
        begin; a review on a stale candidate is rejected; the reviewer must be an
        independent context from the implementer. Duplicate re-delivery of the
        same stage+reviewer+candidate review is idempotent (last wins).
        """
        task = self._require(task_id)
        if stage not in REVIEW_STAGES:
            raise PolicyViolation("unknown_review_stage", f"unknown review stage '{stage}'")
        if not isinstance(verdict, str) or not verdict.strip():
            raise PolicyViolation("invalid_verdict", "verdict must be a non-empty string")
        if stage == "spec" and verdict not in SPEC_VERDICTS:
            raise PolicyViolation("invalid_verdict", f"invalid spec verdict '{verdict}'")
        if stage == "quality" and verdict not in QUALITY_VERDICTS:
            raise PolicyViolation("invalid_verdict", f"invalid quality verdict '{verdict}'")
        if task.state != "awaiting_review":
            raise PolicyViolation("not_in_review", f"task is '{task.state}', review requires awaiting_review")
        if task.delegation.candidate_revision != candidate_revision:
            raise PolicyViolation(
                "stale_candidate",
                f"review candidate '{candidate_revision}' != current '{task.delegation.candidate_revision}'",
            )
        if reviewer == task.assignee_profile:
            raise PolicyViolation("reviewer_not_independent", "reviewer must differ from the implementer")
        if stage == "quality" and not task.delegation.required_review_satisfied("spec"):
            raise PolicyViolation(
                "spec_required", "quality review cannot begin before current-candidate spec PASS"
            )
        rec = ReviewRecord(
            stage=stage,
            verdict=verdict,
            reviewer=reviewer,
            candidate_revision=candidate_revision,
            findings=list(findings or []),
        )
        dup = [
            r
            for r in task.delegation.reviews
            if r.stage == stage and r.candidate_revision == candidate_revision
        ]
        if dup:
            task.delegation.reviews.remove(dup[-1])
        task.delegation.reviews.append(rec)
        try:
            return self.store.put(task, event=f"review_{stage}", expected_task_version=task.task_version)
        except TaskVersionConflict as exc:
            raise PolicyViolation("task_version_conflict", str(exc)) from exc

    def observe_budget(
        self,
        task_id: str | UUID,
        *,
        requests: int = 0,
        tokens: int | None = None,
        attempt_epoch: int | None = None,
    ) -> TaskContract:
        """Accumulate measured token/request usage on the authoritative task.

        Unknown usage is tracked as ``None`` (never 0). This is an honest,
        minimal accounting control; it does not claim provider-level financial
        enforcement the runtime cannot supply. After a persisted attempt_epoch
        the current epoch is mandatory. Absence is not permission. Released
        attempts are refused.
        """
        task = self._require(task_id)
        if task.delegation.execution_revoked:
            raise PolicyViolation(
                "execution_revoked",
                "revoked execution authority cannot observe budget",
            )
        current_epoch = task.delegation.attempt_epoch
        released = task.delegation.execution_released_at
        if current_epoch is None or attempt_epoch is None:
            raise PolicyViolation("stale_attempt_report", "attempt_epoch is required")
        if current_epoch != attempt_epoch:
            raise PolicyViolation(
                "stale_attempt_report",
                f"attempt_epoch {attempt_epoch} != current {current_epoch}",
            )
        if released is not None:
            raise PolicyViolation(
                "stale_attempt_report",
                "execution already released",
            )
        task.delegation.requests_used += int(requests)
        if tokens is not None:
            task.delegation.tokens_used = (task.delegation.tokens_used or 0) + int(tokens)
        try:
            return self.store.put(task, event="budget_observed", expected_task_version=task.task_version)
        except TaskVersionConflict as exc:
            raise PolicyViolation("task_version_conflict", str(exc)) from exc

    def accept(self, task_id: str | UUID, *, operator: str = "noesis-orchestrator") -> TaskContract:
        """Explicit orchestrator acceptance. Backend success is never acceptance.

        Completes the authoritative task only when evidence is present, a
        candidate was submitted, reviewer gates pass for the CURRENT candidate,
        no open critical/important findings remain, execution has been released
        (or a legacy unreleased record still has a live claim lease), and the
        shared budget is not exceeded.

        `operator` is an in-process caller label compared to the allowlisted
        string "noesis-orchestrator". It is not authentication or proof of
        identity. Production must not expose accept() to untrusted callers.
        """
        task = self._require(task_id)
        if operator != "noesis-orchestrator":
            raise PolicyViolation("unauthorized_actor", "only the orchestrator accepts results")
        if task.state not in ("running", "awaiting_review"):
            raise PolicyViolation("not_acceptable", f"task is '{task.state}', cannot accept")
        if task.delegation.budget_exceeded():
            raise PolicyViolation(
                "budget_exceeded",
                f"request budget {task.delegation.requests_used} exceeds limit {task.delegation.request_limit}",
            )
        if task.delegation.execution_released_at is None and task.lease_expired():
            raise PolicyViolation("lease_expired", "cannot accept work whose lease expired")
        if not task.handoff:
            raise PolicyViolation("evidence_required", "cannot accept without handler evidence")
        if task.delegation.candidate_revision is None:
            raise PolicyViolation("candidate_required", "cannot accept without a submitted candidate")
        if task.reviewer_profile:
            if not task.delegation.required_review_satisfied("spec"):
                raise PolicyViolation(
                    "spec_review_required", "cannot accept without current-candidate spec PASS"
                )
            if not task.delegation.required_review_satisfied("quality"):
                raise PolicyViolation(
                    "quality_review_required", "cannot accept without current-candidate quality APPROVED"
                )
        for r in task.delegation.reviews:
            if r.candidate_revision != task.delegation.candidate_revision:
                continue
            for f in r.findings:
                if f.get("severity") in ("critical", "important") and not f.get("resolved", False):
                    raise PolicyViolation(
                        "open_findings", f"open {f.get('severity')} finding blocks acceptance"
                    )
        task.delegation.accepted_by = operator
        task.delegation.accepted_at = utcnow()
        task.transition_to("succeeded")
        try:
            stored = self.store.put(task, event="task_accepted", expected_task_version=task.task_version)
        except TaskVersionConflict as exc:
            raise PolicyViolation("task_version_conflict", str(exc)) from exc
        self.breaker.record_success()
        return stored

    def emit_delegation_projection(self, projection_path: Path | str) -> list[dict[str, Any]]:
        """Write noesis.delegated-task/v1 event-log records derived from the ledger.

        The control plane is the single writer. The projected file is an event
        log / compatible record with clear ownership and replay semantics; it is
        NOT a second independent source of truth. Authoritative lifecycle state
        lives in tasks.jsonl; edits to the projection never affect the task.
        """
        path = Path(projection_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        records: list[dict[str, Any]] = []
        for task in sorted(self.store.all_tasks().values(), key=lambda t: t.created_at):
            records.append(
                {
                    "protocol_version": "noesis.delegated-task/v1",
                    "record_for": str(task.task_id),
                    "correlation_id": str(task.correlation_id),
                    "assignee_profile": task.assignee_profile,
                    "state": task.state,
                    "assignment": {
                        "epoch": task.delegation.assignment_epoch,
                        "attempt": task.delegation.attempt,
                        "runtime_profile": task.delegation.runtime_profile,
                    },
                    "candidate": {
                        "revision": task.delegation.candidate_revision,
                        "digest": task.delegation.candidate_digest,
                    },
                    "reviews": [
                        {
                            "stage": r.stage,
                            "verdict": r.verdict,
                            "reviewer": r.reviewer,
                            "candidate_revision": r.candidate_revision,
                        }
                        for r in task.delegation.reviews
                    ],
                    "acceptance": {
                        "accepted_by": task.delegation.accepted_by,
                        "accepted_at": (
                            task.delegation.accepted_at.isoformat().replace("+00:00", "Z")
                            if task.delegation.accepted_at
                            else None
                        ),
                    },
                    "budget": {
                        "requests_used": task.delegation.requests_used,
                        "request_limit": task.delegation.request_limit,
                        "tokens_used": task.delegation.tokens_used,
                        "budget_tokens": task.delegation.budget_tokens,
                    },
                }
            )
        with path.open("w", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        return records

    def _enforce_delegated_review_gates(self, task: TaskContract) -> None:
        """Refuse completion when a reviewer-gated task lacks positive reviews.

        Tasks with no reviewer_profile keep the historical succeed() path.
        When reviewer_profile is set, succeed() requires the current candidate
        to satisfy spec PASS and quality APPROVED. A missing candidate or empty
        review list is not a bypass.
        """
        if not task.reviewer_profile:
            return
        if not task.delegation.required_review_satisfied("spec"):
            raise PolicyViolation(
                "spec_review_required", "cannot accept without current-candidate spec PASS"
            )
        if not task.delegation.required_review_satisfied("quality"):
            raise PolicyViolation(
                "quality_review_required",
                "cannot accept without current-candidate quality APPROVED",
            )
        for review in task.delegation.reviews:
            if review.candidate_revision != task.delegation.candidate_revision:
                continue
            for finding in review.findings:
                if finding.get("severity") in ("critical", "important") and not finding.get("resolved", False):
                    raise PolicyViolation(
                        "open_findings", f"open {finding.get('severity')} finding blocks acceptance"
                    )

    def succeed(self, task_id: str | UUID, *, now: datetime | None = None) -> TaskContract:
        """Complete a task only when evidence, review, and lease all hold."""
        task = self._require(task_id)
        policy.validate_handoff(task, now=now)
        self._enforce_delegated_review_gates(task)
        task.transition_to("succeeded")
        self.breaker.record_success()
        return self.store.put(task, event="task_succeeded")

    def fail(self, task_id: str | UUID, *, reason: str) -> TaskContract:
        task = self._require(task_id)
        task.transition_to("failed", reason=reason)
        self.breaker.record_failure(reason)
        return self.store.put(task, event="task_failed")

    def block(self, task_id: str | UUID, *, reason: str) -> TaskContract:
        task = self._require(task_id)
        task.transition_to("blocked", reason=reason)
        return self.store.put(task, event="task_blocked")

    def cancel(self, task_id: str | UUID, *, reason: str) -> TaskContract:
        task = self._require(task_id)
        self._cleanup_if_managed(task)
        task = self._require(task_id)
        task.delegation.execution_revoked = True
        task.transition_to("cancelled", reason=reason)
        return self.store.put(task, event="task_cancelled")

    def retry(self, task_id: str | UUID) -> TaskContract:
        """Re-queue a failed task when its bounded retry budget allows."""
        task = self._require(task_id)
        if self._reuse_blocked(task):
            raise PolicyViolation(
                "workspace_quarantined",
                "uncertain termination blocks retry and relaunch",
            )
        if task.retry.exhausted:
            raise PolicyViolation(
                "retry_budget_exhausted",
                f"task exhausted {task.retry.max_attempts} attempt(s)",
            )
        task.transition_to("queued")
        return self.store.put(task, event="task_requeued")

    # ------------------------------------------------------------ supervise --

    def sweep_timeouts(self, *, now: datetime | None = None) -> list[TaskContract]:
        """Fail or re-queue every task that outlived its lease. Stale work never succeeds."""
        now = now or utcnow()
        swept: list[TaskContract] = []
        for task in list(self.store.all_tasks().values()):
            if task.state not in ("claimed", "running"):
                continue
            try:
                expired = task.lease_expired(now=now)
            except (TypeError, ValueError):
                expired = True
            if not expired:
                continue
            self._cleanup_if_managed(task)
            current = self.store.get(task.task_id) or task
            if current.state in ("claimed", "running"):
                self.fail(current.task_id, reason=f"timeout after {current.timeout_s}s")
                current = self.store.get(task.task_id) or current
            if current.state == "succeeded":
                continue
            if self._reuse_blocked(current):
                swept.append(current)
                continue
            if current.state == "failed" and not current.retry.exhausted and not self.breaker.is_open:
                self.retry(current.task_id)
                current = self.store.get(task.task_id) or current
            swept.append(current)
        return swept

    def escalate(self, task_id: str | UUID, *, reason: str) -> dict[str, Any]:
        """Produce an operator-facing escalation record. Never auto-resolves."""
        task = self._require(task_id)
        return {
            "escalation_for": str(task.task_id),
            "title": task.title,
            "assignee_profile": task.assignee_profile,
            "state": task.state,
            "risk_tier": task.risk_tier,
            "attempts": task.retry.attempts,
            "max_attempts": task.retry.max_attempts,
            "terminal_reason": task.terminal_reason,
            "reason": reason,
            "circuit_breaker_open": self.breaker.is_open,
            "requires_human": True,
        }

    def _restore_breaker_state(self) -> None:
        """Reconstruct circuit-breaker state from the durable ledger.

        Runs during startup, before dispatch is enabled. A restart must never
        implicitly clear an operator stop, and a ledger that cannot be replayed
        must fail closed (block new execution admission), not open dispatch.
        """
        if self.store.replay_error is not None:
            self.breaker.trip(
                f"fail-closed: ledger replay error: {self.store.replay_error}"
            )
            return
        latest = self.store.latest_breaker_event()
        if latest is None:
            return
        if latest.get("event") == BREAKER_TRIP_EVENT:
            self.breaker.manually_tripped = True
            self.breaker.reason = latest.get("reason")
        elif latest.get("event") == BREAKER_RESET_EVENT:
            self.breaker.manually_tripped = False
            self.breaker.consecutive_failures = 0
            self.breaker.reason = f"reset by {latest.get('operator')}"

    def emergency_stop(self, *, operator: str, reason: str) -> CircuitBreaker:
        """Durable operator stop. Blocks NEW dispatch (claim); already-running
        work is left to its lease and swept normally.

        The stop is acknowledged only after its durable ledger write succeeds.
        If persistence fails, an exception is raised and dispatch additionally
        fails closed in-memory (a broker that cannot record stops must not keep
        dispatching).
        """
        if not operator or not operator.strip():
            raise ValueError("operator identity is required for emergency_stop")
        try:
            self.store.append_control_event(
                BREAKER_TRIP_EVENT,
                {"operator": operator.strip(), "reason": reason},
            )
        except Exception:
            self.breaker.trip(f"fail-closed: stop persistence failure (by {operator})")
            raise
        self.breaker.trip(f"{reason} (by {operator})")
        return self.breaker

    def resume(self, *, operator: str) -> CircuitBreaker:
        """Durable, audited operator resume. Persists before acknowledging."""
        if not operator or not operator.strip():
            raise ValueError("operator identity is required for resume")
        self.store.append_control_event(
            BREAKER_RESET_EVENT,
            {"operator": operator.strip(), "reason": "operator resume"},
        )
        self.breaker.reset(operator=operator.strip())
        return self.breaker

    # ------------------------------------------------------------ synthesize --

    def synthesize(self, correlation_id: str | UUID) -> dict[str, Any]:
        """Fold a task graph into one operator-facing result. Read-only."""
        tasks = self.store.list_by_correlation(correlation_id)
        by_state: dict[str, list[str]] = {}
        for task in tasks:
            by_state.setdefault(task.state, []).append(task.title)
        succeeded = [t for t in tasks if t.state == "succeeded"]
        return {
            "correlation_id": str(correlation_id),
            "task_count": len(tasks),
            "by_state": by_state,
            "complete": bool(tasks) and all(t.state == "succeeded" for t in tasks),
            "blocked": [t.title for t in tasks if t.state == "blocked"],
            "failed": [t.title for t in tasks if t.state == "failed"],
            "awaiting_approval": [
                t.title
                for t in tasks
                if t.approval and t.approval.required and t.approval.state == "pending"
            ],
            "evidence": [
                {
                    "task": t.title,
                    "assignee": t.assignee_profile,
                    "summary": (t.handoff or {}).get("summary"),
                }
                for t in succeeded
            ],
            "circuit_breaker_open": self.breaker.is_open,
        }

    # ---------------------------------------------------------------- helper --

    def _require(self, task_id: str | UUID) -> TaskContract:
        if self.store.replay_error:
            raise PolicyViolation("replay_error", self.store.replay_error)
        task = self.store.get(task_id)
        if task is None:
            raise PolicyViolation("unknown_task", f"no task contract with id {task_id}")
        return task


__all__ = [
    "CircuitBreaker",
    "Orchestrator",
    "PolicyViolation",
    "TransitionError",
    "Verification",
]
