"""Advisory agency-fleet registry adapter.

Identity authority is ``agency/curation.yaml`` — the SOLE roster source for the
third-party Agency fleet (see ``agency/README.md``). Generated specs under
``agents/`` are intentionally NOT read (they are never committed), and installed
profiles under ``~/.hermes/profiles/`` are runtime state, not identity
authority.

Every agency profile is registered with:

  tier ``advisory``           — not reviewer-only, not supervisor; eligible for
                                advisory execution tasks only
  source ``agency-curation``  — distinct from roster and agent-registry lanes
  activation_state ``staged`` — curated, not live-exercised (ROUTING-SMOKE.md
                                rows are unrecorded at the design revision)
  toolsets from the curation's ``advisory_toolsets`` — never terminal /
                                code_execution (fail-closed if they are)

Capabilities come exclusively from the curation's ``declared_capabilities``
key — the same set ``agency/convert.py`` emits into every generated
``agent.yaml``. Domain capability strings are never invented here; domain
distinction lives in the task intent and the curated division.

Registration grants no authority: per-task authorization is still decided by
``policy.validate_assignment``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from app import _yaml_compat as yaml

if TYPE_CHECKING:  # deferred at runtime: app.registry imports this module
    from app.registry import ProfileRecord

REPO_ROOT = Path(__file__).resolve().parents[3]
CURATION_PATH = REPO_ROOT / "agency" / "curation.yaml"


def load_agency_registry() -> dict[str, ProfileRecord]:
    """Load curated agency profiles as registry records.

    Returns {profile_name: ProfileRecord}. Fails closed: a curation file
    without an explicit advisory capability or toolset declaration raises
    rather than fabricating a default.
    """
    # Deferred import: app.registry imports this module at its own load time.
    from app.registry import ProfileRecord

    raw = yaml.safe_load(CURATION_PATH.read_text(encoding="utf-8")) or {}
    prefix = str(raw.get("profile_prefix", "agency"))

    declared_capabilities = raw.get("declared_capabilities")
    if not declared_capabilities:
        raise ValueError(
            "agency/curation.yaml is missing 'declared_capabilities'; refusing "
            "to register advisory profiles without an explicit capability set"
        )
    advisory_toolsets = tuple(str(t) for t in (raw.get("advisory_toolsets") or ()))
    if not advisory_toolsets:
        raise ValueError(
            "agency/curation.yaml is missing 'advisory_toolsets'; refusing to "
            "register advisory profiles without an explicit toolset list"
        )
    if "terminal" in advisory_toolsets or "code_execution" in advisory_toolsets:
        raise ValueError(
            "advisory_toolsets must never include terminal/code_execution"
        )

    capabilities = frozenset(str(c) for c in declared_capabilities)
    records: dict[str, ProfileRecord] = {}
    for entry in raw.get("agents") or []:
        slug = str(entry["slug"])
        name = f"{prefix}-{slug}"
        division = str(entry.get("division", ""))
        records[name] = ProfileRecord(
            name=name,
            display_name=name,
            role=f"advisory-{division}" if division else "advisory",
            tier="advisory",
            toolsets=advisory_toolsets,
            capabilities=capabilities,
            status="curated",
            activation_state="staged",
            source="agency-curation",
        )
    return records
