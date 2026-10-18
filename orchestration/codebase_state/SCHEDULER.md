# Scheduler proposal — not installed

Observed on 2026-10-18 13:30 MDT.

## Identity

- Proposed mechanism: the existing user crontab, not a new scheduler.
- Execution user: `elvis` (the crontab owner already running `wiki-sync.sh`).
- Host timezone: `America/Denver` (`/etc/localtime` -> `.../zoneinfo/America/Denver`, `date +%Z` = `MDT`).
- Requested timezone: `America/Denver`. These match, so an unqualified crontab line is in the requested zone. It would be refused if they diverged.
- Hermes cron is a different scheduler. The orchestrator profile already has job `05d68f5af3c5` (`30 7 * * *`, deliver local, next run offset `-06:00`). It is not a codebase ingest. Hermes cron was not modified.
- Launchd labels observed and not used: `ai.noesis.orchestrator`, `ai.noesis.broker`, `ai.hermes.gateway`.

## Why not Hermes cron as the primary runner

The Hermes skill reference documents a 3-minute hard interrupt per cron run and a default model turn. This job must stay deterministic and must not send source to an external model. A `no_agent` Hermes job was not created. User crontab is the mechanism already used for a local nightly script.

`wiki-sync.sh` is a different job. Its body, read 2026-10-18, commits and pushes `/Users/elvis/.openclaw/wiki`. It is not a codebase-state ingest, and this collector must not copy that pattern: no add, commit, or push. Do not replace that crontab line.

## Daylight saving

Requested civil time is 02:30 America/Denver.

Verified with `zoneinfo`, not by installing cron:

- 2026-03-08 02:30 America/Denver does not exist (spring forward). A 02:30 crontab line is skipped that morning.
- 2026-11-01 02:30 exists once. The repeated hour that morning is 01:00–01:59, not 02:30.
- 2026-03-08 03:30 exists. 2026-11-01 03:30 exists once.

This collector does not claim whether macOS cron would run a repeated civil time once or twice. 02:30 is not the repeated hour.

Recommended alternative, pending approval: 03:30 America/Denver, which exists on both 2026 transition days. The requested 02:30 line is what `propose-schedule` prints. Neither line is installed.

## Proposed line (not installed)

```cron
30 2 * * * PATH=/usr/bin:/bin HOME=/Users/elvis /bin/bash /Users/elvis/projects/noesis-agent-stack/orchestration/codebase_state/run_ingest.sh run --state-dir /Users/elvis/.local/share/noesis/codebase-state >> /Users/elvis/.local/log/noesis-codebase-state.log 2>&1
```

Ansible hold: `noesis-ansible/roles/noesis_codebase_state/` still defaults `noesis_codebase_state_activate` to false. No playbook references the role. It was not applied.

## Activation record (2026-10-18)

Operator approval was the message `I approve`. That was applied as approval of the requested 02:30 America/Denver schedule, local substring retrieval, and the proposed retention (14 snapshots, 1 MiB × 5 logs). Semantic indexing was not approved because no backend was named.

The user crontab was not modified. `crontab -l` still contains only the wiki-sync line. `crontab -` timed out after 20 seconds, so that write was abandoned.

The live schedule is launchd, which this host already uses for Noesis agents:

- Label: `ai.noesis.codebase-state`
- Plist: `/Users/elvis/Library/LaunchAgents/ai.noesis.codebase-state.plist`
- Interval: Hour 2, Minute 30, system timezone America/Denver
- Verified with `launchctl print gui/501/ai.noesis.codebase-state`: `state = not running`, `runs = 0`
- `RunAtLoad` is false. The controlled ingest was not a launchd run.

Disable:

```bash
launchctl bootout gui/$(id -u)/ai.noesis.codebase-state
rm /Users/elvis/Library/LaunchAgents/ai.noesis.codebase-state.plist
```

## Environment

Minimal `PATH=/usr/bin:/bin`, explicit `HOME`, absolute bash and python, state directory outside `~/projects`. No credentials in the crontab line. The collector does not call a model.

## Notification

No new notification integration. Failure and partial runs are recorded in `last-run.json` and, after an approved install, in the log path above. An approved delivery channel was not identified for this job. Hermes cron `deliver: local` on the unrelated history job is not reused.

## Rollback

The user crontab was not changed. To remove the nightly job:

```bash
launchctl bootout gui/$(id -u)/ai.noesis.codebase-state
rm /Users/elvis/Library/LaunchAgents/ai.noesis.codebase-state.plist
rm -rf /Users/elvis/.local/share/noesis/codebase-state
```

Hermes cron was not modified. No semantic index was written.
