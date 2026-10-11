#!/usr/bin/env python3
"""Validate Honcho memory profiles in noesis-agent-stack."""
from __future__ import annotations

import json
from pathlib import Path
import sys

try:
    import yaml
except Exception as exc:  # pragma: no cover
    raise SystemExit(f'PyYAML is required: {exc}')

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / 'honcho'
PROFILES = MODULE / 'profiles'
REQUIRED = {
    'apiVersion': 'noesis.ai/v1alpha1',
    'kind': 'AgentMemoryProfile',
}

expected = {
    'noesis-orchestrator', 'noesis-steward', 'noesis-cartographer', 'noesis-forge',
    'noesis-sentinel', 'noesis-scribe', 'noesis-signal', 'noesis-substrate',
    'noesis-tracer', 'noesis-ledger', 'noesis-grid', 'noesis-quill',
    'noesis-advocate', 'noesis-herald', 'noesis-architect', 'noesis-skeptic',
}

errors = []
seen = set()

for path in sorted(PROFILES.glob('*.yaml')):
    doc = yaml.safe_load(path.read_text())
    if not isinstance(doc, dict):
        errors.append(f'{path}: not a mapping')
        continue
    name = doc.get('metadata', {}).get('name')
    if name:
        seen.add(name)
    for key, value in REQUIRED.items():
        if doc.get(key) != value:
            errors.append(f'{path}: {key} != {value!r}')
    ident = doc.get('spec', {}).get('identity', {})
    honcho = doc.get('spec', {}).get('honcho', {})
    for key in ('agent_id', 'peer_id', 'peer_type', 'role', 'peer_card'):
        if key not in ident:
            errors.append(f'{path}: missing identity.{key}')
    for key in ('workspace_id', 'cloud_api', 'peer', 'sessions', 'context', 'reasoning', 'dreaming', 'search', 'chat', 'files', 'mcp', 'security'):
        if key not in honcho:
            errors.append(f'{path}: missing honcho.{key}')

missing = expected - seen
extra = seen - expected
if missing:
    errors.append(f'missing profiles: {sorted(missing)}')
if extra:
    errors.append(f'unexpected profiles: {sorted(extra)}')

if errors:
    print(json.dumps({'ok': False, 'errors': errors}, indent=2))
    raise SystemExit(1)

print(json.dumps({'ok': True, 'profiles': sorted(seen)}, indent=2))
