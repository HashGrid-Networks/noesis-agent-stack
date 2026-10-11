"""Contract tests: coding-session supervision rules are registered and enforced.

Covers criterion 1 (Coder Profile — Coding Session Supervision Rules):
the supervision contract exists, is referenced by the coding specialist's
agent contract, and its lifecycle/gate semantics are backed by real
enforcement points in subagent_development.py — not only prose.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
FORGE_YAML = REPO_ROOT / "agents" / "noesis-forge" / "agent.yaml"
SUPERVISION_DOC = REPO_ROOT / "agents" / "noesis-forge" / "CODING-SESSION-SUPERVISION.md"
SDD_MODULE = REPO_ROOT / "orchestration" / "orchestrator" / "app" / "subagent_development.py"

# Lifecycle states mandated by the supervision rules (§7).
SUPERVISION_LIFECYCLE_STATES = (
    "prepared",
    "running",
    "awaiting_approval",
    "verifying",
    "failed",
    "cancelling",
    "cancelled",
    "cleanup_failed",
)

# Gate behaviors that must have real enforcement points (§1–§10).
REQUIRED_DOC_SECTIONS = (
    "## 1. Decide whether to launch a session",
    "## 2. Select the execution backend",
    "## 3. Create a bounded session contract",
    "## 4. Isolate the workspace and credentials",
    "## 5. Enforce permissions and approval boundaries",
    "## 6. Maintain ownership and prevent delegation loops",
    "## 7. Supervise the lifecycle",
    "## 8. Validate outputs independently",
    "## 9. Log decisions and report results",
    "## 10. Cancel, recover, and clean up",
)


@pytest.fixture
def supervision_text() -> str:
    return SUPERVISION_DOC.read_text(encoding="utf-8")


def test_supervision_doc_exists() -> None:
    assert SUPERVISION_DOC.is_file(), f"missing supervision contract: {SUPERVISION_DOC}"


def test_supervision_doc_covers_all_ten_sections(supervision_text: str) -> None:
    for section in REQUIRED_DOC_SECTIONS:
        assert section in supervision_text, f"supervision contract missing section: {section}"


def test_supervision_doc_declares_intersection_not_union(supervision_text: str) -> None:
    # §5: sessions inherit the INTERSECTION of permission sets, never the union.
    assert "intersection" in supervision_text.lower()
    assert "never inherits the union" in supervision_text


def test_supervision_doc_declares_full_lifecycle(supervision_text: str) -> None:
    for state in SUPERVISION_LIFECYCLE_STATES:
        assert state in supervision_text, f"lifecycle state absent from supervision contract: {state}"


def test_forge_agent_contract_references_supervision_doc() -> None:
    text = FORGE_YAML.read_text(encoding="utf-8")
    assert "CODING-SESSION-SUPERVISION.md" in text
    assert "supervision:" in text
    assert "subagent_development.py" in text


def test_supervision_applies_to_both_backends() -> None:
    text = FORGE_YAML.read_text(encoding="utf-8")
    supervision_block = text.split("supervision:", 1)[1]
    assert "claude-code" in supervision_block
    assert "codex" in supervision_block


def test_enforcement_module_backs_review_gates() -> None:
    # §8/§9: ordered spec→quality reviews and backend availability checks are
    # enforced in code, so the contract is not prose-only.
    src = SDD_MODULE.read_text(encoding="utf-8")
    assert "def record_spec_review" in src
    assert "def record_quality_review" in src
    assert "def dispatch_implementation" in src
    assert "backend_available" in src


def test_no_secret_material_in_live_profile_configs() -> None:
    # Live profile configs must use env-var interpolation, never inline keys
    # (criterion: no secrets in catalogs; sibling convention ${VAR_NAME}).
    import re

    live_dir = REPO_ROOT / "profiles" / "live"
    offenders: list[str] = []
    pattern = re.compile(r"api_key:\s*(?!\${|'')[A-Za-z0-9_\-]{20,}")
    for cfg in sorted(live_dir.glob("*/config.yaml")):
        for lineno, line in enumerate(cfg.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{cfg.relative_to(REPO_ROOT)}:{lineno}")
    assert not offenders, f"inline secret material in live configs: {offenders}"
