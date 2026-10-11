import pytest

from app.launcher import reset_credential_attempt_budget
from app.control_plane import Orchestrator
from app.models import Verification


@pytest.fixture(autouse=True)
def _credential_budget_isolation():
    """Reset the per-attempt credential budget before every test.

    F5's credential-attempt budget persists across launcher calls inside one
    test (so a second launch of the same attempt is refused), but resets
    between tests. Without this reset, a successful launch in one test would
    close the budget for the shared (provider, model, task_id) key and cause
    later tests that reuse the same key to fail with credential_attempt_
    exhausted instead of the intended failure mode.
    """
    reset_credential_attempt_budget()
    yield
    reset_credential_attempt_budget()


@pytest.fixture
def ledger(tmp_path):
    return tmp_path / "tasks.jsonl"


@pytest.fixture
def orch(ledger):
    return Orchestrator(ledger)


@pytest.fixture
def verification():
    return Verification(method="reviewer_signoff", evidence_required=True)


@pytest.fixture
def passing_handoff():
    return {
        "summary": "Collected and cross-validated three independent sources.",
        "artifacts": [{"path": "workspace/research/brief.md", "checksum": None, "kind": "brief"}],
        "verification_result": {"passed": True, "evidence": "3 sources cited; all labelled."},
        "unmet_criteria": [],
    }


def make_task(orch, **overrides):
    """A baseline valid, low-risk research task."""
    params = dict(
        title="Cross-validate hashprice sources",
        intent="Collect and cross-validate 2026 hashprice sources with explicit labels.",
        assignee_profile="noesis-signal",
        required_capability="cross_validate",
        acceptance_criteria=["At least three independent sources cited"],
        verification=Verification(method="reviewer_signoff", evidence_required=True),
        idempotency_key="noesis-signal:cross_validate:hashprice:2026-09-18",
        risk_tier="r0",
        timeout_s=900,
    )
    params.update(overrides)
    return orch.propose(**params)


def drive_to_running(orch, task, *, claimed_by=None):
    orch.enqueue(task.task_id)
    orch.claim(task.task_id, claimed_by=claimed_by or task.assignee_profile)
    return orch.start(task.task_id)
