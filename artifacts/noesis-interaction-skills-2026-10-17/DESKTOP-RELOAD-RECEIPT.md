# Desktop reload receipt

Date: 2026-10-17
Authorization: operator out-of-band message `reload approved`.
Scope: reload the desktop host so its serve processes re-read dotenv. Not a dry-run, not a canary, not inference.

## Why this host

In-session `noesis_specialist_delegate` reads `NOESIS_AGENT_STACK_ROOT` from the process environment. Gateway PID 43698 and desktop serve PIDs 54916 (`--profile default serve`) and 55713 (`--profile coder serve`, parent of this session) all had `root_present=no`. A gateway restart does not refresh that desktop session.

## Env writes before reload

`NOESIS_AGENT_STACK_ROOT=/Users/elvis/projects/noesis-agent-stack`

| File | Action |
| --- | --- |
| `~/.hermes/.env` | already_correct |
| `~/.hermes/profiles/coder/.env` | appended; backup `coder/.env.pre-reload-2026-10-17` |
| `~/.hermes/profiles/default/.env` | appended; backup `default/.env.pre-reload-2026-10-17` |
| `~/.hermes/profiles/noesis-orchestrator/.env` | appended; backup `noesis-orchestrator/.env.pre-reload-2026-10-17` |

`INFERENCE_ROUTING_MODE` was not set. Enforce mode remains a separate approval.

## Pre-reload process identity

- Desktop app PID 54139, launchd label `application.com.nousresearch.hermes.1482279369.1482281178`
- Gateway PID 43698, label `ai.hermes.gateway` — not restarted by this action
- Default serve PID 54916
- Coder serve PID 55713

## Reload command

`launchctl kickstart -k "gui/$(id -u)/application.com.nousresearch.hermes.1482279369.1482281178"`

Result: FAILED — `Could not kickstart service ... 102: Operation not supported on socket`.
App-instance launchd jobs do not support kickstart. Proceeding with quit+relaunch via a
detached watchdog (osascript quit, wait, `hermes desktop --skip-build` only after exit).

## Verify after the app returns

1. Desktop PID changed from 54139.
2. New serve process environment has `NOESIS_AGENT_STACK_ROOT` present (do not print the value).
3. Do not treat the PID change as a live profile-load proof. Dry-run and canary stay unauthorized.

## Post-reload verification (2026-10-17 21:38 MDT)

- Relaunch observed: desktop PID 61549 at 16:40:57; serve 62611 (default) / 62997 (coder) at 16:41 — all after the 16:24:28 `.env` writes. Attribution unverified (no `/tmp/hermes-desktop-relaunch.log` found).
- `ps eww` on the serve PIDs shows no `NOESIS_AGENT_STACK_ROOT` — expected false negative: ps shows exec-time env, the dotenv loader injects post-exec.
- Decisive check: this session's slash worker (spawned after serve bootstrap) inherits `NOESIS_AGENT_STACK_ROOT`, `NOESIS_TASK_LEDGER`, `NOESIS_TASK_EVENTS` → live session environment correct.
- Installed plugin `~/.hermes/profiles/noesis-orchestrator/plugins/noesis-specialist-router/__init__.py` byte-identical to committed `7578750` version.
- `noesis_specialist_delegate` visible in-session with the epoch-forwarding schema (`dry_run`, `expected_epoch`, `timeout_s`, `max_attempts`, `reviewer_profile`).
- Open operator ruling: profile `.env` binds `NOESIS_TASK_LEDGER` to the plugin-scoped `noesis-specialist-plugin-ledger.jsonl`, not canonical `workspace/orchestrator/tasks.jsonl`.
- Ladder status: reload ✅ → dry-run (needs explicit approval) → enforce-mode canary `KIMI_CODE / kimi-k2.7-code` (separate approval).
