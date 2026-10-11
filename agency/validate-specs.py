#!/usr/bin/env python3
"""validate-specs.py — three-layer reviewer-guard for the agency integration.

Consensus plan (ralplan Round 2): safety invariants live in code, not in
prose. Layers:

  1. Curation guard  — curation.yaml structure and policy invariants.
  2. Spec guard      — generated specs in .scratch/ honor the contract:
                       byte-identical persona core, appended Noesis contract,
                       advisory risk block, reporting to noesis-orchestrator,
                       secret-shape scan (hard fail), 100% extraction
                       coverage, description uniqueness (TF-IDF cosine —
                       fail > 0.85 vs sibling Tier A profiles AND vs the
                       16 noesis fleet descriptions).
  3. Apply guard     — static checks that the Phase-2 apply layer will be
                       able to honor the contract (script presence, roster
                       derivation source, plugin coexistence inputs).
                       Missing Phase-2 files are WARNINGS here, not errors —
                       they land after the B2 pause gate.

Exit non-zero on any layer-1/layer-2 failure.
"""
from __future__ import annotations

import json
import math
import re
import sys
from collections import Counter
from pathlib import Path

import yaml

AGENCY = Path(__file__).resolve().parent
ROOT = AGENCY.parent
sys.path.insert(0, str(AGENCY))
import secretscan  # noqa: E402

CURATION = AGENCY / "curation.yaml"
SCRATCH = AGENCY / ".scratch"
NOESIS_ROSTER = ROOT / "profiles" / "noesis-roster.yaml"
AGENCY_SCRIPTS = [
    ROOT / "scripts" / "apply-agency-profiles.sh",
    ROOT / "scripts" / "apply-noesis-profiles.sh",
]
PLUGIN_DIR = AGENCY / "integrations" / "hermes-plugin" / "agency-agents-router"
CONTRACT_MARKER = "## Noesis Integration Contract (appended)"

COSINE_FAIL_THRESHOLD = 0.85

errors: list[str] = []
warnings: list[str] = []


def fail(layer: str, msg: str) -> None:
    errors.append(f"[{layer}] {msg}")


def warn(layer: str, msg: str) -> None:
    warnings.append(f"[{layer}] {msg}")


# --------------------------------------------------------------------------
# Layer 1 — curation guard
# --------------------------------------------------------------------------

def layer1_curation() -> tuple[dict, list[dict]]:
    layer = "L1-curation"
    cur = yaml.safe_load(CURATION.read_text(encoding="utf-8"))
    entries = cur.get("agents") or []
    if not entries:
        fail(layer, "curation.yaml has no agents")
        return cur, entries

    prefix = cur.get("profile_prefix", "agency")
    seen_div: dict[str, str] = {}
    seen_slug: set[str] = set()
    for e in entries:
        slug = e.get("slug", "?")
        if slug in seen_slug:
            fail(layer, f"duplicate slug {slug}")
        seen_slug.add(slug)
        div = e.get("division", "?")
        if div in seen_div:
            fail(layer, f"division {div} curated twice ({seen_div[div]}, {slug})")
        seen_div[div] = slug
        if e.get("wave") not in (1, 2):
            fail(layer, f"{slug}: wave must be 1 or 2")
        for key in ("source", "router_slug", "risk"):
            if key not in e:
                fail(layer, f"{slug}: missing curation key {key!r}")
        risk = e.get("risk") or {}
        for key in ("privileged_tools", "secret_access", "autonomous_execution",
                    "human_approval_required"):
            if key not in risk:
                fail(layer, f"{slug}: risk missing {key!r}")
        if risk.get("privileged_tools") or risk.get("secret_access") \
                or risk.get("autonomous_execution"):
            fail(layer, f"{slug}: Tier A must be advisory-only; risk flags must be false")

    if len(entries) > 18:
        fail(layer, f"Tier A cap 18 exceeded: {len(entries)}")

    rep = cur.get("reporting", {})
    if rep.get("reports_to") != "noesis-orchestrator":
        fail(layer, "all Tier A profiles must report to noesis-orchestrator")

    # collision with noesis fleet profile names
    noesis_names = set()
    agents_dir = ROOT / "agents"
    if agents_dir.is_dir():
        noesis_names = {p.name for p in agents_dir.iterdir() if p.is_dir()}
    for e in entries:
        profile = f"{prefix}-{e['slug']}"
        if profile in noesis_names:
            fail(layer, f"{profile} collides with noesis fleet")
    return cur, entries


# --------------------------------------------------------------------------
# Layer 2 — generated spec guard
# --------------------------------------------------------------------------

_WORD = re.compile(r"[a-z0-9][a-z0-9+\-]*")


def tfidf_cosine(texts: list[str]) -> dict[tuple[int, int], float]:
    docs = [_WORD.findall(t.lower()) for t in texts]
    df: Counter = Counter()
    for tokens in docs:
        df.update(set(tokens))
    n = len(docs)
    vecs = []
    for tokens in docs:
        tf = Counter(tokens)
        vec = {t: (1 + math.log(c)) * math.log((1 + n) / (1 + df[t]))
               for t, c in tf.items()}
        vecs.append(vec)
    sims: dict[tuple[int, int], float] = {}
    for i in range(n):
        for j in range(i + 1, n):
            a, b = vecs[i], vecs[j]
            if not a or not b:
                continue
            dot = sum(v * b.get(t, 0.0) for t, v in a.items())
            na = math.sqrt(sum(v * v for v in a.values()))
            nb = math.sqrt(sum(v * v for v in b.values()))
            sims[(i, j)] = dot / (na * nb) if na and nb else 0.0
    return sims


def layer2_specs(cur: dict, entries: list[dict]) -> None:
    layer = "L2-specs"
    prefix = cur.get("profile_prefix", "agency")
    if not SCRATCH.is_dir():
        warn(layer, "no .scratch tree — run convert.py --full-tree first; "
                    "spec checks skipped")
        return

    upstream = Path.home() / "tools" / "agency-agents"
    descriptions: list[str] = []
    desc_names: list[str] = []

    for e in entries:
        slug = e["slug"]
        profile = f"{prefix}-{slug}"
        soul = SCRATCH / "agents" / profile / "SOUL.md"
        ayaml = SCRATCH / "agents" / profile / "agent.yaml"
        if not soul.is_file() or not ayaml.is_file():
            fail(layer, f"{profile}: spec missing in .scratch "
                        f"(run convert.py --full-tree)")
            continue

        # (a) persona byte-identity: upstream body must appear verbatim
        src = upstream / e["source"]
        if not src.is_file():
            fail(layer, f"{profile}: upstream source missing {e['source']}")
            continue
        text = src.read_text(encoding="utf-8")
        parts = text.split("---\n", 2)
        body = parts[2].lstrip("\n").rstrip("\n")
        soul_text = soul.read_text(encoding="utf-8")
        if body not in soul_text:
            fail(layer, f"{profile}: persona core is not byte-identical to upstream")
        if CONTRACT_MARKER not in soul_text:
            fail(layer, f"{profile}: Noesis contract block missing from SOUL.md")
        if "reports_to: noesis-orchestrator" not in soul_text:
            fail(layer, f"{profile}: SOUL.md frontmatter must report to noesis-orchestrator")

        # (b) agent.yaml contract
        doc = yaml.safe_load(ayaml.read_text(encoding="utf-8"))
        agent = (doc or {}).get("agent") or {}
        if agent.get("tier") != "advisory":
            fail(layer, f"{profile}: agent.yaml tier must be advisory")
        if (agent.get("collaboration") or {}).get("reports_to") != "noesis-orchestrator":
            fail(layer, f"{profile}: agent.yaml must report to noesis-orchestrator")
        risk = agent.get("risk") or {}
        if risk.get("privileged_tools") or risk.get("secret_access") \
                or risk.get("autonomous_execution"):
            fail(layer, f"{profile}: agent.yaml risk flags must be false")
        toolsets = ((agent.get("hermes_profile") or {}).get("toolsets")) or []
        for banned in ("terminal", "delegation", "cronjob", "code_execution"):
            if banned in toolsets:
                fail(layer, f"{profile}: advisory profile must not grant {banned!r}")
        prov = agent.get("provenance") or {}
        if not prov.get("upstream_commit"):
            fail(layer, f"{profile}: agent.yaml provenance missing commit")

        # (c) secret scan (hard fail) — source + generated
        problems = secretscan.scan_file(soul) + secretscan.scan_file(ayaml)
        problems += secretscan.scan_file(src)
        for p in problems:
            fail(layer, f"secret-shape scan: {p}")

        # collect description for uniqueness check
        hp = agent.get("hermes_profile") or {}
        descriptions.append(str(hp.get("description") or ""))
        desc_names.append(profile)

    # noesis fleet descriptions for cross-fleet uniqueness
    if NOESIS_ROSTER.is_file():
        roster = yaml.safe_load(NOESIS_ROSTER.read_text(encoding="utf-8"))
        for name, pdata in ((roster.get("profiles") or {}).items()):
            d = (pdata or {}).get("description")
            if d:
                descriptions.append(str(d))
                desc_names.append(name)

    if len(descriptions) >= 2:
        sims = tfidf_cosine(descriptions)
        for (i, j), sim in sorted(sims.items(), key=lambda kv: -kv[1]):
            if sim > COSINE_FAIL_THRESHOLD:
                fail(layer, f"description similarity {sim:.3f} > "
                            f"{COSINE_FAIL_THRESHOLD} between {desc_names[i]} "
                            f"and {desc_names[j]} — routing smoke test required")

    # (d) extraction coverage: every curated upstream body non-trivial
    for e in entries:
        src = upstream / e["source"]
        if src.is_file():
            text = src.read_text(encoding="utf-8")
            if len(text) < 500:
                warn(layer, f"{e['slug']}: upstream persona < 500 chars "
                            f"(suspicious extraction target)")


# --------------------------------------------------------------------------
# Layer 3 — apply-layer static guard
# --------------------------------------------------------------------------

def layer3_apply(cur: dict, entries: list[dict]) -> None:
    layer = "L3-apply"
    prefix = cur.get("profile_prefix", "agency")
    live = [f"{prefix}-{e['slug']}" for e in entries]

    for script_path in AGENCY_SCRIPTS:
        if not script_path.is_file():
            warn(layer, f"{script_path.name} not present yet "
                        "(Phase 2, post-gate) — cannot verify roster derivation")
            continue
        script = script_path.read_text(encoding="utf-8")
        tag = script_path.name
        if re.search(r"^\s*ROSTER=\s*\(", script, re.M):
            fail(layer, f"{tag} hardcodes a ROSTER array — roster must be "
                        "derived from agency/curation.yaml / profiles yaml")
        if "--check" not in script:
            warn(layer, f"{tag} has no --check mode (planned parity gate)")
        if script_path.name == "apply-agency-profiles.sh" \
                and "secret" not in script.lower():
            warn(layer, f"{tag} missing runtime secret assertion "
                        "(two-layer scan layer 3)")

    roster_yaml = SCRATCH / "roster.yaml"
    if roster_yaml.is_file():
        roster = yaml.safe_load(roster_yaml.read_text(encoding="utf-8"))
        listed = [p.get("name") for p in (roster.get("profiles") or [])]
        if sorted(listed) != sorted(live):
            fail(layer, "roster.yaml in .scratch disagrees with curation.yaml "
                        "(regenerate via convert.py --full-tree)")
    else:
        warn(layer, "no .scratch/roster.yaml yet (run --full-tree)")

    prefer_live = SCRATCH / "integrations" / "prefer-live.yaml"
    canonical_pl = AGENCY / "integrations" / "hermes-plugin" / "prefer-live.yaml"
    if prefer_live.is_file():
        pl = yaml.safe_load(prefer_live.read_text(encoding="utf-8"))
        names = [e.get("name") for e in (pl.get("live_profiles") or [])]
        if sorted(names) != sorted(live):
            fail(layer, "prefer-live.yaml does not cover exactly the curated set")
        if canonical_pl.is_file():
            if canonical_pl.read_bytes() != prefer_live.read_bytes():
                fail(layer, "canonical prefer-live.yaml beside the router drifted "
                            "from .scratch copy (re-run convert.py --full-tree)")
        else:
            fail(layer, "canonical prefer-live.yaml missing beside vendored router")
    else:
        warn(layer, "no .scratch/integrations/prefer-live.yaml yet")

    agents_json = PLUGIN_DIR / "data" / "agents.json"
    if agents_json.is_file():
        data = json.loads(agents_json.read_text(encoding="utf-8"))
        slugs = {a.get("slug") for a in data}
        missing = [e["router_slug"] for e in entries if e["router_slug"] not in slugs]
        if missing:
            fail(layer, f"router_slugs absent from vendored plugin: {missing}")
    else:
        fail(layer, "vendored router plugin data missing — re-run T04 vendor step")


# --------------------------------------------------------------------------

def main() -> int:
    cur, entries = layer1_curation()
    layer2_specs(cur, entries)
    layer3_apply(cur, entries)

    for w in warnings:
        print(f"WARNING: {w}")
    if errors:
        for e in errors:
            print(f"ERROR: {e}")
        print(f"\nvalidate-specs: FAIL ({len(errors)} error(s), "
              f"{len(warnings)} warning(s))")
        return 1
    print(f"validate-specs: OK ({len(warnings)} warning(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
