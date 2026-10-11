# Routing Rehearsal Matrix — Live-Runtime Attestation Evidence

Standing-goal §6/§8 deliverable: one representative intent per `TASK_RULES` class,
dispatched through the **real** `specialist_dispatch_cli` entrypoint with the **real**
`HermesCliAdapter` against the installed Hermes runtime.

**Provenance**: every case ran with `dry_run: true` and `allow_generic_fallback: false`.
No specialist was spawned and no inference was spent; the only live action is the
read-only attestation command `hermes -p <profile> profile show <profile>`. Results are
attestation-level evidence of which profile the launcher would resolve and load — they
are NOT live execution evidence (that remains the separately gated Subgoal 5 cycle).

Regeneration:

```bash
.venv/bin/python workspace/subgoal5-sandbox/rehearsal_matrix.py
```

| # | task_class | selected | attested loaded | requested==loaded==attested | manifest sha256 | verdict |
|---|-----------|----------|-----------------|------------------------------|-----------------|---------|
| 1 | `agent_architecture` | `noesis-architect` | `noesis-architect` | ✅ | `eac905c5070d…` | OK |
| 2 | `review` | `noesis-sentinel` | `noesis-sentinel` | ✅ | `6949bf649203…` | OK |
| 3 | `implementation` | `noesis-forge` | `noesis-forge` | ✅ | `38bf012993b4…` | OK |
| 4 | `operations` | `noesis-substrate` | `noesis-substrate` | ✅ | `3e34f490225a…` | OK |
| 5 | `archival` | `noesis-scribe` | `noesis-scribe` | ✅ | `9860fc090947…` | OK |
| 6 | `osint` | `noesis-tracer` | `noesis-tracer` | ✅ | `369c686009ab…` | OK |
| 7 | `crypto` | `noesis-ledger` | `noesis-ledger` | ✅ | `954a1ce1628b…` | OK |
| 8 | `advocacy` | `noesis-advocate` | `noesis-advocate` | ✅ | `f54293e2b97e…` | OK |
| 9 | `research` | `noesis-signal` | `noesis-signal` | ✅ | `cbdb6e6fb75b…` | OK |
| 10 | `data` | `noesis-grid` | `noesis-grid` | ✅ | `f453e04c2423…` | OK |
| 11 | `writing` | `noesis-quill` | `noesis-quill` | ✅ | `74871f51913e…` | OK |
| 12 | `comms` | `noesis-herald` | `noesis-herald` | ✅ | `67b60ece5e83…` | OK |
| 13 | `planning` | `noesis-cartographer` | `noesis-cartographer` | ✅ | `bc05406d1775…` | OK |
| 14 | `stewardship` | `noesis-steward` | `noesis-steward` | ✅ | `d58a1edd1743…` | OK |

**Result: 14/14 classes route to their designated
specialist with launcher-attested identity on the live runtime.**

Side evidence captured per case in `workspace/subgoal5-sandbox/rehearsal-matrix/`:

- `task-<class>.json` — input contract (dry_run, no generic fallback).
- `events-<class>.jsonl` — structured routing event incl. eligibility rejections,
  selection reason, model/provider reference, and the persisted attestation block
  (`app/control_plane.py` `_emit_launch_event`, commit `4b89dc6`).
- `ledger-<class>.jsonl` — durable task-proposal ledger record.
