# NOESIS DEPLOYMENT RUNBOOK (repository-verified)

Companion to `DEPLOYMENT-PROMPT.md`. Every command below was verified against
this repository and the local Hermes installation on 2026-10-11 unless marked
**gap**. Do not substitute guessed commands; gaps are listed in §9.

---

## 1. Repository and release identity

```bash
cd ~/projects/noesis-agent-stack
git remote -v                     # expect: github.com:enuno/noesis-agent-stack
git rev-parse HEAD                # full agent-stack revision
git status --porcelain            # dirty state must be reviewed, never blanket-copied
git branch --show-current         # checkpoint branch, not main, until merge approval
```

Release/candidate identity: sha256 digest over the exact artifact set,
recorded in the qualification manifest (precedent:
`workspace/a6-v3-prototype-install/manifests/composition-digest.txt`).
Approval manifest schema: `platform/approval-manifest.schema.json`.

## 2. Destination state (Hermes runtime, read-only)

```bash
hermes --version                  # verified: prints version + install dir + local HEAD
hermes -p <profile> profile show <profile>   # verified: profile loads, SOUL.md, model lane
python3 scripts/verify_live_config.py      # read-only live-config compliance, never prints secrets
python3 scripts/validate_inference_routing.py   # offline policy validator, repo-local
```

Profile inventory lives in `~/.hermes/profiles/<profile>/`. Installed Hermes
source: `~/.hermes/hermes-agent` (verify `git -C ~/.hermes/hermes-agent rev-parse HEAD`
against the qualified installed revision before any install phase).

## 3. Prerequisites, budgets, secrets

- Test suite: `.venv/bin/python -m pytest orchestration/orchestrator/tests/ -q`
  (last verified: 360 passed / 0 failed, 2026-10-11).
- Inference budgets: per-task `budget_tokens` / `request_limit` on delegation
  contracts (`app/models.py` DelegationBudget); enforcer:
  `app/inference_routing.py`.
- Secrets: Bitwarden SM project references only (`noesis_techne`, `noosphere`).
  Never print, commit, or inline values. Runtime delivery via
  `~/.hermes/scripts/browser_use_key.py` pattern (secrets.command helper).
- Authority isolation: see `workspace/a6-v3-prototype-install/AUTHORITY-ISOLATION-FINDINGS.md`
  — same-uid worker writes remain a known blocker; file-integrity attestation
  (HARDENING-1) is the mandated first hardening step.

## 4. Fleet/profile installation (prototype scope only)

```bash
./scripts/apply-noesis-profiles.sh            # roster-driven (profiles/noesis-roster.yaml)
```

This writes profile directories only. It does not restart Hermes, enable
plugins, or start work. Verification: re-run §2 profile shows.

## 5. Control-plane code deployment

The control plane runs from this repository (venv + `PYTHONPATH=orchestration/orchestrator`).
Prototype installation precedent (A6 v3): isolated file-level install into
`~/.hermes/hermes-agent` recorded in
`workspace/a6-v3-prototype-install/install/INSTALLATION-RECEIPT.{json,md}`
with before-bytes for rollback (§7). Installed-runtime changes require their
own phase authorization; **no restart is implied by any install receipt.**

Runtime provenance verification: launcher attestation (loaded profile +
manifest sha256) is persisted in routing events
(`app/control_plane.py::_emit_launch_event`, test
`test_cli_event_persists_launcher_attestation_for_audit`).

## 6. State and backup locations

- Authoritative ledgers: `workspace/orchestrator/tasks.jsonl`,
  per-cycle ledgers (e.g. `workspace/subgoal5-sandbox/control-plane-ledger.jsonl`).
- Evidence: `workspace/a6-v3-prototype-install/` (inventory, manifests,
  receipts, checkpoint, logs).
- Before any write to the installed runtime: capture before-bytes
  (precedent: `install/rollback-bytes/`).

## 7. Rollback / abort

1. Abort criteria: destination drift vs approved digest, dirty-state
   surprises in target paths, budget overrun, any unapproved phase request.
2. Installed-runtime rollback: restore recorded before-bytes, then re-verify
   installed HEAD and profile shows (§2).
3. Ledger recovery: replay is automatic — constructing
   `app.control_plane.Orchestrator(ledger_path=...)` rebuilds state from the
   JSONL ledger; verify task states after replay.
4. Never roll forward silently: record the abort in the deployment receipt.

## 8. Deployment receipt (machine-readable)

Fields (precedent: `install/INSTALLATION-RECEIPT.json`): target environment,
readiness class, repo + installed revisions, candidate/manifest digests,
changed paths, verification commands + outcomes, limitations, remaining
gates, rollback evidence reference, approval manifest hash.

## 9. Missing automation (gaps — propose, do not disguise as done)

- **G-1: noesis-ansible absent.** No clone exists under `~/projects`;
  no Ansible roles/playbooks verified. All deployment automation today is
  the shell/python scripts in §2/§4. Bounded follow-up: create the
  noesis-ansible repository with inventory + idempotent roles wrapping the
  verified commands above; until then the runbook's manual steps ARE the
  supported path.
- **G-2: No service manager integration.** No launchd/systemd units; the
  fleet runs as user-invoked `hermes` processes. Bounded follow-up: define
  unit templates + readiness probes before any production rollout phase.
- **G-3: No automated drift detection** between repo policy and live
  profiles beyond `verify_live_config.py` (point-in-time). Bounded
  follow-up: scheduled compliance check with alerting.
