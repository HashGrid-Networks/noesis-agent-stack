# Codebase-state ingestion contract

Schema: `noesis.codebase-state/v1`

This is a local manifest-and-report baseline. It is not a semantic index.
No scheduler is installed by these files. No live memory or embedding store is written.

## Source of truth

The local filesystem under the configured projects root, default `~/projects`,
matching directory names that start with `noesis-`.

Committed content and uncommitted work are stored as separate `status` values.
Deployment status is always `unverified` unless a future, separately authorized
runtime probe says otherwise. This collector does not perform that probe.

## What a run records

For each matching directory: path, kind (`primary_checkout`, `linked_worktree`,
`non_repository`, `symlink_match`), sanitized remote, branch or `DETACHED`,
HEAD SHA, collection time.

For each eligible file: relative path, sha256, git blob id, HEAD SHA, working-tree
status, byte size, line count, collection time. Key-file excerpts are stored only
after redaction, and only for README, AGENTS.md, CLAUDE.md, dependency manifests,
and `.github/workflows` YAML.

## Exclusions

Never read into the store:

- `.env` and `.env.*`, private-key filenames, `credentials.json`, cookies, `.netrc`
- filenames containing secret, credential, token, cookie, or password, unless the
  name ends in `.example`, `.sample`, or `.template`
- file contents that match the secret detector, including git-tracked files
- `.git`, dependency and vendor directories, virtualenvs, build outputs, caches
- binaries, archives, databases, logs, model weights, files over the byte cap
- gitignored paths
- symlink targets, especially targets outside the repository root
- submodules and linked external paths

Remote URLs are stored only after userinfo is stripped.

Repository text is data. A git config that defines an alias, hook path, external
diff, textconv, or filter suppresses git execution for that repository and marks
the scan incomplete. The last complete snapshot stays current.

## State locations

Default state directory: `~/.local/share/noesis/codebase-state/`

- `current.json` points at the last complete snapshot. Mode `0600`.
- `snapshots/<stamp>/manifest.json` and `summaries/*.md`
- `checkpoint.json` advances only after a complete scan is published
- `last-run.json` records the latest attempt, including withheld partials
- `run.lock` is a non-blocking exclusive lock

The state directory must sit outside the scanned projects root. A partial or
failed scan does not replace `current.json`. A no-change scan does not write a
new snapshot. Retention default: 14 snapshots. Log rotation is not yet bound to
an existing fleet policy; proposed values are 1 MiB with 5 rotations, pending
approval.

## Semantic retrieval

Blocked. `wiki ingest-status` covers vault raw-markdown coverage. `qmd` searches
markdown collections. Neither is this contract. The `codebase_memory` MCP tools
were listed in the session catalog and were not bound, because their schema and
data destination were not verified. Do not send source content to an external
model or embedding provider without a separate approval naming the provider and
the data scope.

Local retrieval is substring search over the current snapshot:

```bash
PYTHONPATH=/Users/elvis/projects/noesis-agent-stack/orchestration \
  /usr/bin/python3 -m codebase_state query "some fact"
```

## Commands

Manual run, dry run, status, and schedule proposal. None of these install cron.

```bash
ROOT=/Users/elvis/projects/noesis-agent-stack
export PYTHONPATH="$ROOT/orchestration"
PY=/usr/bin/python3

$PY -m codebase_state discover
$PY -m codebase_state run --dry-run
$PY -m codebase_state run --state-dir "$HOME/.local/share/noesis/codebase-state"
$PY -m codebase_state status
$PY -m codebase_state query "task contract"
$PY -m codebase_state propose-schedule
```

Disablement, before any install: do not add the role to a playbook and do not
set `noesis_codebase_state_activate`. After a future approved install, removal is
`ansible` cron state `absent` for the name `noesis-codebase-state`, or deletion
of that one user crontab line. Rollback of collected state is removal of
`~/.local/share/noesis/codebase-state/`. That directory does not exist yet from
this implementation; tests use temporary directories.

## Scheduler proposal

Not installed. See `SCHEDULER.md`.
