"""Skill and contract registration checks for subagent-driven development."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_subagent_driven_development_skill_is_discoverable() -> None:
    skill = ROOT / "skills" / "subagent-driven-development" / "SKILL.md"
    text = skill.read_text(encoding="utf-8")
    assert "name: subagent-driven-development" in text
    assert "orchestration/orchestrator/app/subagent_development.py" in text
    assert "noesis-forge" in text
    assert "noesis-sentinel" in text
    assert "noesis-skeptic" in text


def test_subagent_development_contract_schemas_are_valid_json() -> None:
    for name in [
        "subagent-development-plan.schema.json",
        "subagent-development-review.schema.json",
    ]:
        path = ROOT / "contracts" / "orchestration" / name
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert data["type"] == "object"
