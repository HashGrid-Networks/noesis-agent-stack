"""Tests for app._yaml_compat — the PyYAML/ruamel compatibility shim.

The test venv ships PyYAML, so the default import exercises that branch.
The ruamel branch is exercised by blocking the ``yaml`` import and
re-importing the shim, which mirrors the Hermes runtime environment
(ruamel.yaml installed, PyYAML absent).
"""

from __future__ import annotations

import builtins
import sys

import pytest

YAML_DOC = """\
roster:
  - id: noesis-forge
    active: true
  - id: noesis-architect
    active: false
"""


def test_shim_prefers_pyyaml_when_available() -> None:
    from app import _yaml_compat

    assert _yaml_compat.BACKEND == "pyyaml"
    data = _yaml_compat.safe_load(YAML_DOC)
    assert data["roster"][0]["id"] == "noesis-forge"
    assert issubclass(_yaml_compat.YAMLError, Exception)


def test_shim_falls_back_to_ruamel(monkeypatch: pytest.MonkeyPatch) -> None:
    ruamel = pytest.importorskip("ruamel.yaml", reason="ruamel.yaml not installed")
    assert ruamel is not None

    real_import = builtins.__import__

    def no_pyyaml(name, *args, **kwargs):
        if name == "yaml":
            raise ModuleNotFoundError("No module named 'yaml'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pyyaml)
    monkeypatch.delitem(sys.modules, "yaml", raising=False)
    monkeypatch.delitem(sys.modules, "app._yaml_compat", raising=False)

    import app

    monkeypatch.delattr(app, "_yaml_compat", raising=False)
    import app._yaml_compat as shim

    assert shim.BACKEND == "ruamel"
    data = shim.safe_load(YAML_DOC)
    assert data["roster"][1]["id"] == "noesis-architect"
    assert data["roster"][1]["active"] is False
    assert issubclass(shim.YAMLError, Exception)


def test_shim_empty_and_invalid_documents() -> None:
    from app import _yaml_compat

    assert _yaml_compat.safe_load("") is None
    with pytest.raises(_yaml_compat.YAMLError):
        _yaml_compat.safe_load("key: [unclosed")
