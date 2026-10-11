"""Envelope authentication tests for durable delegation.

Covers Subgoal 4 §1/§11 ("Authenticate the transport and bind the sender to
the assigned role and task — a sender_id inside a payload is not proof of
identity") and the "unauthorized sender and forged payload identity"
failure-injection requirement. Exercises the real DelegationStore and ledger
persistence; signatures are HMAC-SHA256 over the canonical envelope content.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from app.delegation import (
    ActivationError,
    AuthenticationError,
    DelegationStore,
    HandoffEnvelope,
    HmacEnvelopeAuthenticator,
    MessageType,
)

_ORCH_KEY = b"orch-test-key-0123456789abcdef"
_WRONG_KEY = b"wrong-test-key-0123456789abcdef"


def _auth() -> HmacEnvelopeAuthenticator:
    return HmacEnvelopeAuthenticator(key=_ORCH_KEY)


def _store(tmp_path: Path, *, authenticated: bool = True) -> DelegationStore:
    if authenticated:
        return DelegationStore(tmp_path / "delegated-tasks.jsonl", authenticator=_auth())
    return DelegationStore(tmp_path / "delegated-tasks.jsonl")


def _assign(task_id: str, *, root: str | None = None) -> HandoffEnvelope:
    return HandoffEnvelope(
        protocol_version="noesis.delegated-task/v1",
        message_id=str(uuid4()),
        message_type=MessageType.ASSIGN,
        sender_id="noesis-orchestrator",
        recipient_id="noesis-forge",
        root_task_id=root or task_id,
        task_id=task_id,
        attempt_id=f"{task_id}/a1",
        contract_revision="c-rev-1",
        assignment_epoch=1,
        correlation_id=root or task_id,
        idempotency_key=f"idem-{task_id}",
        expected_task_version=0,
        payload={
            "intent": "bounded work",
            "acceptance_criteria": ["done"],
            "required_capability": "implement",
        },
    )


class TestSignedLifecycle:
    def test_signed_assign_accepted_and_task_created(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        msg = _auth().apply(_assign("t-auth-1"))
        task = store.process(msg)
        assert task.task_id == "t-auth-1"
        assert task.assignee_profile == "noesis-forge"
        assert store.get("t-auth-1") is not None

    def test_full_lifecycle_under_authentication(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        auth = _auth()
        task = store.process(auth.apply(_assign("t-auth-2")))
        task = store.mark_ready(task.task_id, by="noesis-orchestrator")
        task = store.start_running(task.task_id, by="noesis-forge", expected_attempt_epoch=1)
        task = store.submit_verifying(
            task.task_id, by="noesis-forge", expected_attempt_epoch=1, candidate_revision="rev-a"
        )
        task = store.record_review(
            task.task_id,
            stage="spec",
            verdict="PASS",
            reviewer="noesis-skeptic",
            candidate_revision="rev-a",
            findings=[],
        )
        task = store.record_review(
            task.task_id,
            stage="quality",
            verdict="APPROVED",
            reviewer="noesis-sentinel",
            candidate_revision="rev-a",
            findings=[],
        )
        task = store.accept(task.task_id, by="noesis-orchestrator")
        assert task.state == "completed"
        assert task.accepted


class TestRefusals:
    def test_unsigned_message_refused_when_authenticator_configured(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        with pytest.raises(AuthenticationError):
            store.process(_assign("t-auth-3"))  # no signature
        assert store.get("t-auth-3") is None
        # Nothing was persisted: the ledger has no task record.
        ledger = tmp_path / "delegated-tasks.jsonl"
        assert not ledger.exists() or ledger.read_text(encoding="utf-8").strip() == ""

    def test_empty_signature_string_refused(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        msg = replace(_auth().apply(_assign("t-auth-4")), signature="")
        with pytest.raises(AuthenticationError):
            store.process(msg)
        assert store.get("t-auth-4") is None

    def test_wrong_key_refused(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        msg = HmacEnvelopeAuthenticator(key=_WRONG_KEY).apply(_assign("t-auth-5"))
        with pytest.raises(AuthenticationError):
            store.process(msg)
        assert store.get("t-auth-5") is None

    def test_tampered_sender_after_signing_refused(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        signed = _auth().apply(_assign("t-auth-6"))
        forged = replace(signed, sender_id="noesis-forge")  # claim orchestrator role
        with pytest.raises(AuthenticationError):
            store.process(forged)
        assert store.get("t-auth-6") is None

    def test_tampered_recipient_after_signing_refused(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        signed = _auth().apply(_assign("t-auth-7"))
        forged = replace(signed, recipient_id="noesis-substrate")
        with pytest.raises(AuthenticationError):
            store.process(forged)
        assert store.get("t-auth-7") is None

    def test_tampered_payload_after_signing_refused(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        signed = _auth().apply(_assign("t-auth-8"))
        forged = replace(
            signed, payload={**signed.payload, "acceptance_criteria": ["exfiltrate"]}
        )
        with pytest.raises(AuthenticationError):
            store.process(forged)
        assert store.get("t-auth-8") is None

    def test_tampered_epoch_after_signing_refused(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        signed = _auth().apply(_assign("t-auth-9"))
        forged = replace(signed, assignment_epoch=99)
        with pytest.raises(AuthenticationError):
            store.process(forged)
        assert store.get("t-auth-9") is None

    def test_tampered_message_id_after_signing_refused(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        signed = _auth().apply(_assign("t-auth-10"))
        forged = replace(signed, message_id=str(uuid4()))
        with pytest.raises(AuthenticationError):
            store.process(forged)
        assert store.get("t-auth-10") is None

    def test_signature_reused_from_different_message_refused(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        donor = _auth().apply(_assign("t-donor"))
        transplant = replace(_assign("t-auth-11"), signature=donor.signature)
        with pytest.raises(AuthenticationError):
            store.process(transplant)
        assert store.get("t-auth-11") is None

    def test_tampered_timestamp_after_signing_refused(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        signed = _auth().apply(_assign("t-auth-15"))
        forged = replace(signed, timestamp="2020-01-01T00:00:00+00:00")
        with pytest.raises(AuthenticationError):
            store.process(forged)
        assert store.get("t-auth-15") is None

    def test_timestamp_populated_by_default_and_signed(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        msg = _assign("t-auth-16")
        assert msg.timestamp  # criterion 4 §3: every envelope carries issue time
        signed = _auth().apply(msg)
        task = store.process(signed)
        assert task.task_id == "t-auth-16"

    def test_authentication_error_is_activation_error(self) -> None:
        assert issubclass(AuthenticationError, ActivationError)


class TestMessageIdBinding:
    def test_idempotent_redelivery_same_signed_message_returns_existing(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        signed = _auth().apply(_assign("t-bind-1"))
        first = store.process(signed)
        second = store.process(signed)  # identical bytes: idempotent, not refused
        assert second.task_id == first.task_id
        assert store.get("t-bind-1") is not None

    def test_message_id_reuse_with_conflicting_content_refused(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        # Bind "reused-id" to an authentic ASSIGN.
        original = _auth().apply(replace(_assign("t-bind-2"), message_id="reused-id"))
        store.process(original)
        # Same message_id, different content, validly signed: a protocol-level
        # message_id reuse attack. The binding refuses it even though the
        # signature itself is valid.
        attacker = replace(_assign("t-bind-4"), message_id="reused-id")
        attacker = replace(
            attacker, payload={**attacker.payload, "intent": "reinterpreted goal"}
        )
        attacker = _auth().apply(attacker)
        with pytest.raises(AuthenticationError, match="reused with conflicting content"):
            store.process(attacker)
        assert store.get("t-bind-4") is None

    def test_binding_only_records_verified_messages(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        msg = _assign("t-bind-5")  # unsigned; refused before binding
        with pytest.raises(AuthenticationError):
            store.process(msg)
        # The refused message_id remains unbound: a later validly signed
        # message under the same id is not falsely accused of reuse.
        later = replace(_assign("t-bind-6"), message_id=msg.message_id)
        later = _auth().apply(later)
        task = store.process(later)
        assert task.task_id == "t-bind-6"


class TestCompatibilityAndRecovery:
    def test_store_without_authenticator_preserves_legacy_behavior(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path, authenticated=False)
        task = store.process(_assign("t-auth-12"))  # unsigned, accepted as before
        assert task.task_id == "t-auth-12"

    def test_signed_message_accepted_by_reloaded_authenticated_store(
        self, tmp_path: Path
    ) -> None:
        ledger = tmp_path / "delegated-tasks.jsonl"
        store = DelegationStore(ledger, authenticator=_auth())
        store.process(_auth().apply(_assign("t-auth-13")))
        reloaded = DelegationStore(ledger, authenticator=_auth())
        assert reloaded.get("t-auth-13") is not None
        # New signed messages verify against the reloaded store too.
        task = reloaded.process(_auth().apply(_assign("t-auth-14")))
        assert task.task_id == "t-auth-14"
