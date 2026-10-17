"""YAML compatibility shim: prefer PyYAML, fall back to ruamel.yaml.

The repo declares ``pyyaml>=6.0`` and the test venv provides it, but the
Hermes runtime python (the interpreter that loads the specialist-router
plugin) ships ``ruamel.yaml`` instead of PyYAML. Plugin-loaded code paths
therefore fail with ``ModuleNotFoundError: yaml`` at import time.

Repo code needs exactly two names: ``safe_load`` and ``YAMLError``. This
module exposes both under a stable surface so callers can do::

    from app import _yaml_compat as yaml

    yaml.safe_load(text)
    except yaml.YAMLError:

Both backends are exercised by tests (the PyYAML branch via the test venv,
the ruamel branch via an explicit import-blocking test).
"""

from __future__ import annotations

from typing import Any

BACKEND: str

try:  # PyYAML — declared repo dependency, used by the test venv
    import yaml as _pyyaml

    safe_load = _pyyaml.safe_load
    YAMLError = _pyyaml.YAMLError
    BACKEND = "pyyaml"
except ModuleNotFoundError:  # Hermes runtime — ruamel.yaml only
    from ruamel.yaml import YAML
    from ruamel.yaml.error import YAMLError as _RuamelYAMLError

    _ruamel = YAML(typ="safe")
    YAMLError = _RuamelYAMLError
    BACKEND = "ruamel"

    def safe_load(stream: Any) -> Any:
        return _ruamel.load(stream)
