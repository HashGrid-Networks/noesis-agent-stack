#!/usr/bin/env bash
# =============================================================================
# install-noesis-specialist-router.sh
# -----------------------------------------------------------------------------
# Installs the noesis-specialist-router Hermes plugin into one profile's plugin
# directory. This is an activation helper for canaries; it does not restart
# Hermes, launch child tasks, change credentials, or enable production work.
#
# Usage:
#   ./scripts/install-noesis-specialist-router.sh \
#     [--home DIR] [--profile NAME] [--dry-run] [--yes]
# =============================================================================
set -euo pipefail
umask 077

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PROFILE="noesis-orchestrator"
DRY=0
YES=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --home) HERMES_HOME="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    --yes) YES=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

SELF_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -P)"
REPO_ROOT="$(cd -- "$SELF_DIR/.." >/dev/null 2>&1 && pwd -P)"
PLUGIN_SRC="$REPO_ROOT/integrations/hermes-plugin/noesis-specialist-router"
PLUGIN_NAME="noesis-specialist-router"

if [[ ! -f "$PLUGIN_SRC/plugin.yaml" || ! -f "$PLUGIN_SRC/__init__.py" ]]; then
  echo "ERROR: plugin source missing: $PLUGIN_SRC" >&2
  exit 1
fi

if [[ "$PROFILE" == "default" ]]; then
  PROFILE_ROOT="$HERMES_HOME"
else
  PROFILE_ROOT="$HERMES_HOME/profiles/$PROFILE"
fi
PLUGIN_DST="$PROFILE_ROOT/plugins/$PLUGIN_NAME"
BACKUP_ROOT="$HERMES_HOME/.profile-backups/noesis-specialist-router-$(date +%Y%m%d-%H%M%S)"

log() { printf '%s\n' "$*"; }
run() {
  if [[ "$DRY" -eq 1 ]]; then
    log "  [dry-run] $*"
  else
    "$@"
  fi
}
json_quote() { python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$1"; }

log "Noesis specialist router plugin installer"
log "  Hermes home : $HERMES_HOME"
log "  Profile     : $PROFILE"
log "  Source      : $PLUGIN_SRC"
log "  Target      : $PLUGIN_DST"
log "  Mode        : $([[ $DRY -eq 1 ]] && echo DRY-RUN || echo APPLY)"
log ""

if [[ "$DRY" -eq 0 && "$YES" -ne 1 ]]; then
  echo "ERROR: real install requires --yes (use --dry-run first)." >&2
  exit 2
fi

if [[ "$DRY" -eq 0 && ! -d "$PROFILE_ROOT" ]]; then
  echo "ERROR: profile root does not exist: $PROFILE_ROOT" >&2
  exit 1
fi

if [[ -e "$PLUGIN_DST" ]]; then
  run mkdir -p "$BACKUP_ROOT"
  run cp -a "$PLUGIN_DST" "$BACKUP_ROOT/$PLUGIN_NAME"
fi

if [[ "$DRY" -eq 1 ]]; then
  log "  would install plugin $PLUGIN_NAME into $PLUGIN_DST"
else
  mkdir -p "$PROFILE_ROOT/plugins"
  rm -rf "$PLUGIN_DST"
  cp -a "$PLUGIN_SRC" "$PLUGIN_DST"
  mkdir -p "$HERMES_HOME/logs"
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  plugin_sha="$(find "$PLUGIN_SRC" -type f -print0 | sort -z | xargs -0 shasum -a 256 | shasum -a 256 | awk '{print $1}')"
  printf '{"ts":%s,"script":%s,"home":%s,"mode":%s,"status":%s,"plugin":%s,"plugin_sha256":%s,"profile":%s,"rollback":%s}\n' \
    "$(json_quote "$ts")" \
    "$(json_quote "install-noesis-specialist-router.sh")" \
    "$(json_quote "$HERMES_HOME")" \
    "$(json_quote "install")" \
    "$(json_quote "ok")" \
    "$(json_quote "$PLUGIN_NAME")" \
    "$(json_quote "$plugin_sha")" \
    "$(json_quote "$PROFILE")" \
    "$(json_quote "$BACKUP_ROOT")" >> "$HERMES_HOME/logs/apply-audit.jsonl"
  log "  installed plugin $PLUGIN_NAME"
fi

log ""
log "Next verification command:"
log "  hermes -p $PROFILE chat -q 'Use noesis_specialist_delegate dry_run=true for an architecture task and report requested_profile_id and loaded_profile_id.'"
log ""
log "Rollback:"
log "  rm -rf '$PLUGIN_DST'"
log "  # if backup exists: cp -a '$BACKUP_ROOT/$PLUGIN_NAME' '$PLUGIN_DST'"
