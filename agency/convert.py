#!/usr/bin/env python3
"""convert.py — Tier A spec generator for the Agency catalog integration.

Consensus plan: .omh/plans/ralplan-agency-integration.md (Round 2, 2-tier).

The repo holds ONLY control files (curation.yaml, this converter, vendored
router plugin). Tier A specs are generated ON DEMAND into the gitignored
agency/.scratch/ tree; scripts/apply-agency-profiles.sh (Phase 2) is the
sole path that materializes them into a Hermes home. Ansible invokes the
apply script — no second materializer (ralplan nit N6).

Modes:
  --preview <slug>     print the generated SOUL.md + agent.yaml to stdout
  --promote <slug>     materialize one agent into .scratch/agents/<profile>/
  --full-tree          materialize all curated agents + prefer-live.yaml
                       + roster.yaml into .scratch/
  --check              verify: (a) pinned commit is an ancestor of upstream
                       HEAD; (b) vendored plugin matches a fresh rebuild
                       byte-for-byte (plugin.yaml + data/agents.json);
                       (c) .scratch tree (if present) matches a fresh
                       regeneration byte-for-byte (staleness detection);
                       (d) secret-shape scan over all generated artifacts.

Generated files are deterministic (no wall-clock timestamps) so the
byte-compare gates are stable.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

AGENCY = Path(__file__).resolve().parent
ROOT = AGENCY.parent
sys.path.insert(0, str(AGENCY))
import secretscan  # noqa: E402

CONVERTER_VERSION = "1.0.0"
SOURCE_FILE = AGENCY / "SOURCE"
CURATION = AGENCY / "curation.yaml"
SCRATCH = AGENCY / ".scratch"
PLUGIN_DIR = AGENCY / "integrations" / "hermes-plugin" / "agency-agents-router"
UPSTREAM_BUILDER = Path.home() / "tools" / "agency-agents" / "scripts" / "build-hermes-plugin.py"
UPSTREAM_CHECKOUT = Path.home() / "tools" / "agency-agents"

CONTRACT_MARKER = "## Noesis Integration Contract (appended)"


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------

def parse_source() -> dict[str, str]:
    data: dict[str, str] = {}
    for line in SOURCE_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
    return data


def load_curation() -> dict:
    return yaml.safe_load(CURATION.read_text(encoding="utf-8"))


def parse_upstream_agent(path: Path) -> tuple[dict[str, str], str]:
    """Split an upstream agent file into (frontmatter fields, persona body).

    Body is returned EXACTLY as it appears after the closing frontmatter
    fence (leading newlines stripped, matching upstream's own parser) so the
    persona core is byte-identical.
    """
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError(f"{path}: no frontmatter")
    parts = text.split("---\n", 2)
    if len(parts) < 3:
        raise ValueError(f"{path}: truncated frontmatter")
    fields: dict[str, str] = {}
    for line in parts[1].splitlines():
        if ":" not in line or line.startswith((" ", "\t")):
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip().strip('"').strip("'")
    return fields, parts[2].lstrip("\n")


# --------------------------------------------------------------------------
# generation
# --------------------------------------------------------------------------

def role_label(division: str) -> str:
    return f"{division} advisory specialist"


def contract_block(entry: dict, source: dict, fields: dict[str, str]) -> str:
    commit = source["UPSTREAM_COMMIT"]
    risk = entry["risk"]
    reporting = load_curation().get("reporting", {})
    approvals = "\n".join(f"  - {a}" for a in risk["human_approval_required"])
    return f"""---

{CONTRACT_MARKER} — not part of the upstream persona.

### Provenance
- Upstream: {source['UPSTREAM_URL']} @ `{commit[:12]}`
- Source file: `{entry['source']}` (MIT, (c) 2025 AgentLand Contributors)
- Converter: agency/convert.py v{CONVERTER_VERSION}

### Noesis posture (advisory-only)
- Tier: advisory specialist; reports to `{reporting.get('reports_to', 'noesis-orchestrator')}`
- Privileged tools: {str(risk['privileged_tools']).lower()}
- Secret access: {str(risk['secret_access']).lower()}
- Autonomous execution: {str(risk['autonomous_execution']).lower()}
- Human approval required for:
{approvals}
- Router slug: `{entry['router_slug']}` (prefer-live overlay applies)
"""


def soul_md(entry: dict, source: dict, fields: dict[str, str], body: str) -> str:
    profile = f"{load_curation().get('profile_prefix', 'agency')}-{entry['slug']}"
    commit = source["UPSTREAM_COMMIT"]
    fm = f"""---
name: {profile}
role: advisory-specialist
tier: advisory
persistence: persistent
domain: {entry['division']} (agency catalog)
reports_to: noesis-orchestrator
delegates_to: none
reviewed_by: noesis-skeptic
upstream: {entry['source']} @ {commit[:12]}
---

"""
    return fm + body.rstrip("\n") + "\n" + contract_block(entry, source, fields)


def agent_yaml(entry: dict, source: dict, fields: dict[str, str]) -> str:
    cur = load_curation()
    profile = f"{cur.get('profile_prefix', 'agency')}-{entry['slug']}"
    lane = cur.get("model_lane", {})
    reporting = cur.get("reporting", {})
    risk = entry["risk"]
    approvals = "\n".join(f"    - {a}" for a in risk["human_approval_required"])
    doc = {
        "version": CONVERTER_VERSION,
        "agent": {
            "agent_id": profile,
            "name": fields.get("name", profile),
            "role": role_label(entry["division"]),
            "tier": "advisory",
            "persistence": "persistent",
            "runtime": "hermes",
            "enabled": True,
            "hermes_profile": {
                "profile_name": profile,
                "description": fields.get("description", ""),
                "clone_from": "default",
                "clone_skills": False,
                "model": {
                    "default": lane.get("default", "kimi-k2.7-code"),
                    "provider": lane.get("provider", "kimi-coding"),
                },
                "toolsets": [
                    "memory", "file", "web", "session_search",
                    "skills", "clarify",
                ],
            },
            "risk": {
                "privileged_tools": bool(risk["privileged_tools"]),
                "secret_access": bool(risk["secret_access"]),
                "autonomous_execution": bool(risk["autonomous_execution"]),
                "human_approval_required": risk["human_approval_required"],
            },
            "capabilities": ["advisory", "review", "analysis"],
            "guardrails": [
                "Advisory-only: never executes privileged actions",
                "No secrets or credentials access, ever",
                "No autonomous execution or outbound actions",
                "Human approval required for production, credential,"
                " financial, or external-communication actions",
                "Reports to noesis-orchestrator; does not dispatch sibling agents",
            ],
            "collaboration": {
                "reports_to": reporting.get("reports_to", "noesis-orchestrator"),
                "delegates_to": reporting.get("delegates_to", []),
                "reviewed_by": reporting.get("reviewed_by", ["noesis-skeptic"]),
            },
            "provenance": {
                "upstream_url": source["UPSTREAM_URL"],
                "upstream_commit": source["UPSTREAM_COMMIT"],
                "source_file": entry["source"],
                "license": "MIT (c) 2025 AgentLand Contributors",
                "converter": f"agency/convert.py v{CONVERTER_VERSION}",
            },
            "deployment": {
                "wave": entry["wave"],
                "status": "curated",
            },
        },
    }
    header = (
        f"# agents/{profile}/agent.yaml\n"
        f"# GENERATED by agency/convert.py v{CONVERTER_VERSION} — do not hand-edit.\n"
        f"# Advisory contract for the agency-{entry['slug']} agent.\n"
    )
    return header + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)


def prefer_live_yaml(entries: list[dict]) -> str:
    cur = load_curation()
    prefix = cur.get("profile_prefix", "agency")
    lines = [
        "# prefer-live overlay — GENERATED by agency/convert.py --full-tree.",
        "# Deterministic pre-ranking resolver for the agency-agents-router plugin:",
        "# when a router candidate matches a live Tier A profile, prefer the",
        "# live profile (full Noesis contract) and suppress the router path,",
        "# unless the caller sets `router_exploration`.",
        "strategy: suppress_router_candidate_when_live_profile_matches",
        "unless_flag: router_exploration",
        "live_profiles:",
    ]
    for entry in sorted(entries, key=lambda e: e["slug"]):
        lines.append(f"  - name: {prefix}-{entry['slug']}")
        lines.append(f"    router_slug: {entry['router_slug']}")
    return "\n".join(lines) + "\n"


def roster_yaml(entries: list[dict], source: dict) -> str:
    """Runtime roster consumed by scripts/apply-agency-profiles.sh (Phase 2).

    Derived from curation.yaml + pinned upstream — never hardcoded in the
    apply script (ralplan C3 roster-driven apply).
    """
    cur = load_curation()
    lane = cur.get("model_lane", {})
    doc = {
        "version": CONVERTER_VERSION,
        "generated_from": "agency/curation.yaml",
        "pin": source["UPSTREAM_COMMIT"],
        "model_lane": lane,
        "profiles": [
            {
                "name": f"{cur.get('profile_prefix', 'agency')}-{e['slug']}",
                "slug": e["slug"],
                "division": e["division"],
                "wave": e["wave"],
                "model": lane.get("default"),
                "provider": lane.get("provider"),
            }
            for e in sorted(entries, key=lambda x: (x["wave"], x["slug"]))
        ],
    }
    header = (
        "# roster.yaml — GENERATED by agency/convert.py --full-tree.\n"
        "# Consumed by scripts/apply-agency-profiles.sh; do not hand-edit.\n"
    )
    return header + yaml.safe_dump(doc, sort_keys=False)


def generate(entries: list[dict], source: dict, out_root: Path) -> list[Path]:
    """Materialize specs for entries under out_root; return written paths."""
    cur = load_curation()
    prefix = cur.get("profile_prefix", "agency")
    written: list[Path] = []
    for entry in entries:
        src = UPSTREAM_CHECKOUT / entry["source"]
        fields, body = parse_upstream_agent(src)
        profile = f"{prefix}-{entry['slug']}"
        dest = out_root / "agents" / profile
        dest.mkdir(parents=True, exist_ok=True)
        s_path = dest / "SOUL.md"
        y_path = dest / "agent.yaml"
        s_path.write_text(soul_md(entry, source, fields, body), encoding="utf-8")
        y_path.write_text(agent_yaml(entry, source, fields), encoding="utf-8")
        written += [s_path, y_path]
    integ = out_root / "integrations"
    integ.mkdir(parents=True, exist_ok=True)
    pl_path = integ / "prefer-live.yaml"
    pl_path.write_text(prefer_live_yaml(entries), encoding="utf-8")
    # Canonical copy beside the vendored router (its resolver reads it here).
    # Written only for full-tree materialization, keeping one source of truth.
    if out_root == SCRATCH:
        canonical = AGENCY / "integrations" / "hermes-plugin" / "prefer-live.yaml"
        canonical.write_text(pl_path.read_text(encoding="utf-8"), encoding="utf-8")
        written.append(canonical)
    r_path = out_root / "roster.yaml"
    r_path.write_text(roster_yaml(entries, source), encoding="utf-8")
    written += [pl_path, r_path]
    return written


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------

def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_pin(source: dict) -> list[str]:
    problems: list[str] = []
    commit = source.get("UPSTREAM_COMMIT", "")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        return [f"UPSTREAM_COMMIT malformed: {commit!r}"]
    rc = subprocess.run(
        ["git", "-C", str(UPSTREAM_CHECKOUT), "merge-base", "--is-ancestor",
         commit, "HEAD"], capture_output=True,
    ).returncode
    if rc != 0:
        problems.append(
            f"pin {commit[:12]} is not an ancestor of upstream HEAD "
            f"(checkout moved — run the repin procedure in agency/README.md)"
        )
    return problems


def check_plugin() -> list[str]:
    problems: list[str] = []
    if not UPSTREAM_BUILDER.is_file():
        return [f"upstream builder missing: {UPSTREAM_BUILDER}"]
    with tempfile.TemporaryDirectory() as tmp:
        rc = subprocess.run(
            [sys.executable, str(UPSTREAM_BUILDER), "--out", tmp],
            capture_output=True, text=True,
        )
        if rc.returncode != 0:
            return [f"plugin rebuild failed: {rc.stderr.strip()[:300]}"]
        for rel in ("plugin.yaml", "data/agents.json"):
            fresh = Path(tmp) / "agency-agents-router" / rel
            vendored = PLUGIN_DIR / rel
            if not vendored.is_file():
                problems.append(f"vendored plugin file missing: {rel}")
                continue
            if sha256(fresh) != sha256(vendored):
                problems.append(
                    f"vendored plugin {rel} drifted from fresh rebuild "
                    f"(re-vendor per agency/README.md)"
                )
    return problems


def check_scratch(source: dict, entries: list[dict]) -> list[str]:
    problems: list[str] = []
    if not SCRATCH.is_dir():
        return ["(no .scratch tree — run --full-tree to materialize)"]
    with tempfile.TemporaryDirectory() as tmp:
        generate(entries, source, Path(tmp))
        fresh_files = {p.relative_to(tmp): p for p in Path(tmp).rglob("*") if p.is_file()}
        scratch_files = {p.relative_to(SCRATCH): p for p in SCRATCH.rglob("*") if p.is_file()}
        for rel in sorted(set(fresh_files) | set(scratch_files)):
            if rel not in fresh_files:
                problems.append(f"stale in .scratch (not in curation): {rel}")
            elif rel not in scratch_files:
                problems.append(f"missing in .scratch: {rel}")
            elif sha256(fresh_files[rel]) != sha256(scratch_files[rel]):
                problems.append(f"drifted content in .scratch: {rel}")
    return problems


def check_secrets(entries: list[dict], source: dict) -> list[str]:
    problems: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        written = generate(entries, source, Path(tmp))
        for path in written:
            problems.extend(secretscan.scan_file(path))
    problems.extend(secretscan.scan_file(CURATION))
    return problems


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Tier A spec generator for the Agency catalog integration."
    )
    parser.add_argument("--preview", metavar="SLUG")
    parser.add_argument("--promote", metavar="SLUG")
    parser.add_argument("--full-tree", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    modes = [m for m in (args.preview, args.promote, bool(args.full_tree), args.check) if m]
    if len(modes) != 1:
        parser.error("choose exactly one mode")

    source = parse_source()
    entries = load_curation()["agents"]

    if args.preview or args.promote:
        slug = args.preview or args.promote
        match = [e for e in entries if e["slug"] == slug]
        if not match:
            print(f"ERROR: slug {slug!r} not in curation.yaml", file=sys.stderr)
            return 1
        entry = match[0]
        fields, body = parse_upstream_agent(UPSTREAM_CHECKOUT / entry["source"])
        if args.preview:
            print(soul_md(entry, source, fields, body))
            print("=" * 72)
            print(agent_yaml(entry, source, fields))
            return 0
        with tempfile.TemporaryDirectory() as tmp:
            written = generate([entry], source, Path(tmp))
            for rel in written:
                rel_str = str(rel.relative_to(tmp))
                if rel_str.startswith("agents/"):
                    dest = SCRATCH / rel_str
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(rel, dest)
                    print(f"promoted -> {dest}")
        return 0

    if args.full_tree:
        written = generate(entries, source, SCRATCH)
        print(f"--full-tree: wrote {len(written)} files under {SCRATCH}")
        return 0

    # --check
    problems: list[str] = []
    problems += check_pin(source)
    problems += check_plugin()
    problems += check_scratch(source, entries)
    problems += check_secrets(entries, source)
    if problems:
        for p in problems:
            print(f"CHECK: {p}")
        print(f"\n--check: FAIL ({len(problems)} problem(s))")
        return 1
    print("--check: OK (pin, plugin parity, scratch staleness, secrets)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
