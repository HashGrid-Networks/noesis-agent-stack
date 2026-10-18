#!/bin/bash
# Read-only codebase-state collector. Does not install a schedule.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [[ -f "$SCRIPT_DIR/__init__.py" && -d "$SCRIPT_DIR/../.." ]]; then
  ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
else
  ROOT="/Users/elvis/projects/noesis-agent-stack"
fi
export PYTHONPATH="${ROOT}/orchestration${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1
export GIT_CONFIG_GLOBAL="${GIT_CONFIG_GLOBAL:-/dev/null}"
export GIT_CONFIG_NOSYSTEM=1
export GIT_TERMINAL_PROMPT=0

LOG="${NOESIS_CODEBASE_STATE_LOG:-${HOME}/.local/log/noesis-codebase-state.log}"
mkdir -p "$(dirname "$LOG")"
chmod 700 "$(dirname "$LOG")" || true
if [[ -f "$LOG" ]]; then
  size="$(wc -c < "$LOG" | tr -d ' ')"
  if [[ "$size" -gt 1048576 ]]; then
    i=4
    while [[ "$i" -ge 1 ]]; do
      if [[ -f "$LOG.$i" ]]; then
        mv "$LOG.$i" "$LOG.$((i + 1))"
      fi
      i=$((i - 1))
    done
    mv "$LOG" "$LOG.1"
    rm -f "$LOG.6"
  fi
fi
touch "$LOG"
chmod 600 "$LOG" || true
exec >>"$LOG" 2>&1
exec /usr/bin/python3 -m codebase_state "$@"
