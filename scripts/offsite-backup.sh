#!/bin/bash
set -euo pipefail

# Mímir offsite backup — encrypted push of the artifact archive to cloud (OneDrive)
# through an rclone *crypt* remote. Runs on the NAS Pi via a systemd timer (daily).
#
# ┌─ REFERENCE IMPLEMENTATION of the Grimnir offsite-backup pattern (mimir#9) ─┐
# │ munin-memory#172 and brokkr#1 copy-adapt this script. The shared contract: │
# │   • Encrypted: rclone crypt — file CONTENTS and NAMES never leave in clear. │
# │   • Mirror `current/` + N-day history via --backup-dir → `archive/<date>/`. │
# │   • Non-destructive: deletions are MOVED to archive, pruned after N days.   │
# │   • --max-delete guard: abort a run that would delete an implausible count. │
# │   • Heartbeat stamp + Heimdall status panel so a silent failure is visible. │
# │   • Fail-loud: any error exits non-zero AND pushes a `fail` panel.          │
# └────────────────────────────────────────────────────────────────────────────┘
#
# Prereqs (see docs/offsite-backup.md for the one-time setup):
#   • rclone installed on the Pi.
#   • An rclone crypt remote (default name `mimir-crypt`) wrapping a OneDrive remote.
#   • RCLONE_CONFIG chmod 600, owned by magnus — it holds the OneDrive OAuth token
#     AND the (obscured) crypt password. NEVER commit it to the repo.
#   • The crypt password + salt backed up independently (lose them = the offsite
#     copy is permanently unreadable). See the doc's "Key custody" section.
#
# Usage:
#   ./offsite-backup.sh              run the backup
#   ./offsite-backup.sh --dry-run    show what would change, touch nothing

# ---- Config (override via environment / EnvironmentFile) ----
SERVICE="${MIMIR_OFFSITE_SERVICE:-mimir}"                 # Heimdall service id
SOURCE="${MIMIR_OFFSITE_ROOT:-/home/magnus/mimir}"        # directory to back up
REMOTE="${MIMIR_OFFSITE_REMOTE:-mimir-crypt}"             # rclone crypt remote name
RETENTION_DAYS="${MIMIR_OFFSITE_RETENTION_DAYS:-30}"      # archive prune horizon
MAX_DELETE="${MIMIR_OFFSITE_MAX_DELETE:-1000}"            # abort if a run deletes > this
STAMP="${MIMIR_OFFSITE_STAMP:-$HOME/mimir-server/offsite.stamp}"
LOG="${MIMIR_OFFSITE_LOG:-$HOME/mimir-server/offsite-backup.log}"
RCLONE="${RCLONE_BIN:-rclone}"
PANEL="${MIMIR_OFFSITE_PANEL:-offsite}"                   # Heimdall panel id

DRY_RUN=""
if [ "${1:-}" = "--dry-run" ] || [ "${MIMIR_OFFSITE_DRYRUN:-}" = "1" ]; then
  DRY_RUN="--dry-run"
fi

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { echo "$(ts) $*" | tee -a "$LOG" >&2; }

# Push a Heimdall status panel. Optional (no-op if the hub env vars are unset).
# Never logs the token; curl output is discarded.
push_panel() {
  local state="$1" message="$2"
  [ -n "${HEIMDALL_HUB_URL:-}" ] && [ -n "${HEIMDALL_FLEET_TOKEN:-}" ] || return 0
  curl -fsS --max-time 5 -X POST "$HEIMDALL_HUB_URL" \
    -H "Authorization: Bearer ${HEIMDALL_FLEET_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{\"service\":\"${SERVICE}\",\"panel\":\"${PANEL}\",\"kind\":\"status\",\"label\":\"Offsite backup\",\"state\":\"${state}\",\"message\":\"${message}\"}" \
    >/dev/null 2>&1 || true
}

# Fail loud: report the failing exit code to the log + Heimdall, then propagate it.
on_err() {
  local rc=$?
  trap - ERR
  log "ERROR: offsite backup failed (exit ${rc})"
  push_panel fail "backup failed (exit ${rc}) — see ${LOG}"
  exit "${rc}"
}
trap on_err ERR

mkdir -p "$(dirname "$LOG")"

# ---- Preflight ----
command -v "$RCLONE" >/dev/null 2>&1 || { log "ERROR: rclone not found (RCLONE_BIN=$RCLONE)"; exit 1; }
[ -d "$SOURCE" ] || { log "ERROR: source dir missing: $SOURCE"; exit 1; }

# Warn (don't fail) if the rclone config is group/world-readable — it holds secrets.
CONF="${RCLONE_CONFIG:-$HOME/.config/rclone/rclone.conf}"
if [ -f "$CONF" ]; then
  PERM=$(stat -c '%a' "$CONF" 2>/dev/null || stat -f '%Lp' "$CONF" 2>/dev/null || echo "")
  case "$PERM" in ""|600|400) ;; *) log "WARN: $CONF is mode $PERM — should be 600 (holds OAuth token + crypt password)";; esac
fi

# Force an auth/connectivity check against the crypt remote before we start.
"$RCLONE" lsd "${REMOTE}:" >/dev/null 2>>"$LOG" || { log "ERROR: cannot reach remote '${REMOTE}:' — check rclone config / network"; exit 1; }

DEST="${REMOTE}:current"
ARCHIVE="${REMOTE}:archive/$(date -u +%F)"

log "starting offsite backup ${DRY_RUN:+(dry-run) }${SOURCE} → ${DEST} (archive: ${ARCHIVE})"

# Mirror the current state. Overwritten/deleted files are MOVED into the dated
# archive (never destroyed). --max-delete aborts the run if the change set is
# implausibly large (e.g. the source was accidentally wiped) — even though the
# archive would still hold them, we'd rather stop and be noticed.
# shellcheck disable=SC2086
"$RCLONE" sync "${SOURCE}/" "$DEST" \
  --backup-dir "$ARCHIVE" \
  --max-delete "$MAX_DELETE" \
  --transfers 4 --checkers 8 \
  --log-file "$LOG" --log-level INFO \
  --stats 0 $DRY_RUN

if [ -n "$DRY_RUN" ]; then
  log "dry-run complete — no changes made, stamp/prune/panel skipped"
  exit 0
fi

# Prune archive entries older than the retention horizon. A prune failure must
# NOT fail the backup (the mirror already succeeded), so it's best-effort.
if "$RCLONE" delete "${REMOTE}:archive" --min-age "${RETENTION_DAYS}d" --log-file "$LOG" --log-level INFO; then
  "$RCLONE" rmdirs "${REMOTE}:archive" --leave-root --log-file "$LOG" --log-level INFO \
    || log "WARN: archive rmdirs cleanup failed — harmless, will retry next run"
else
  log "WARN: archive prune (>${RETENTION_DAYS}d) failed — will retry next run"
fi

# Heartbeat + success panel.
date +%s > "$STAMP"
COUNT=$(find "$SOURCE" -type f | wc -l | tr -d ' ')
log "offsite backup complete: ${COUNT} files mirrored to ${DEST}"
push_panel pass "${COUNT} files, $(ts)"
