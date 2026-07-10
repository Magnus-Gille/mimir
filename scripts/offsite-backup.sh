#!/bin/bash
set -euo pipefail

# Mímir offsite backup — encrypted push of the artifact archive to cloud (OneDrive)
# through an rclone *crypt* remote. Runs on the NAS Pi via a systemd timer (daily).
#
# ┌─ REFERENCE IMPLEMENTATION of the Grimnir offsite-backup pattern (mimir#9) ─┐
# │ munin-memory#172 and brokkr#1 copy-adapt this script. The shared contract: │
# │   • Encrypted: rclone crypt — file CONTENTS and NAMES never leave in clear, │
# │     and the script fails closed if the remote is not a verified crypt.      │
# │   • Mirror `current/` + N-day history via --backup-dir → `archive/<run>/`.  │
# │   • Non-destructive: deletions are MOVED to a per-run archive dir; whole    │
# │     archive dirs older than N days are pruned by their NAME (not mtime).     │
# │   • Preflight delete-count gate + --max-delete: abort an implausible wipe.   │
# │   • Heartbeat stamp + Heimdall status panel so a silent failure is visible.  │
# │   • Fail-loud: every expected failure pushes a `fail` panel and exits ≠0.    │
# └────────────────────────────────────────────────────────────────────────────┘
#
# Runs on Linux (GNU date). Prereqs / setup / key custody: docs/offsite-backup.md.
#
# Usage:
#   ./offsite-backup.sh              run the backup
#   ./offsite-backup.sh --dry-run    show what would change, touch nothing

# ---- Config (override via environment / EnvironmentFile) ----
SERVICE="${MIMIR_OFFSITE_SERVICE:-mimir}"                 # Heimdall service id
PANEL="${MIMIR_OFFSITE_PANEL:-offsite}"                   # Heimdall panel id
SOURCE="${MIMIR_OFFSITE_ROOT:-/home/magnus/mimir}"        # directory to back up
REMOTE="${MIMIR_OFFSITE_REMOTE:-mimir-crypt}"             # rclone crypt remote NAME (no ':' / path)
RETENTION_DAYS="${MIMIR_OFFSITE_RETENTION_DAYS:-30}"      # archive prune horizon (days)
MAX_DELETE="${MIMIR_OFFSITE_MAX_DELETE:-1000}"            # abort if a run would remove ≥ this many files
MAX_DELETE_PCT="${MIMIR_OFFSITE_MAX_DELETE_PCT:-25}"      # ...or more than this % of current/
STATE_DIR="${MIMIR_OFFSITE_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/mimir}"
STAMP="${MIMIR_OFFSITE_STAMP:-$STATE_DIR/offsite.stamp}"
LOG="${MIMIR_OFFSITE_LOG:-$STATE_DIR/offsite-backup.log}"
RCLONE="${RCLONE_BIN:-rclone}"

# Git internals are transient workspace state, not Mimir artifacts. Deep Codex
# checkpoint refs can exceed OneDrive's 400-character encrypted path limit.
RCLONE_FILTERS=(--exclude '**/.git/**')

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

# Expected failure: log, push a fail panel, exit. (ERR trap disabled to avoid
# a double report.) Used for every preflight/validation failure so a silent
# dashboard is impossible — the doc's "any error → fail panel" is literal.
die() {
  trap - ERR
  log "ERROR: $*"
  push_panel fail "$* — see ${LOG}"
  exit 1
}

# Unexpected command failure after preflight (e.g. rclone sync aborts).
on_err() {
  local rc=$?
  trap - ERR
  log "ERROR: offsite backup failed (exit ${rc})"
  push_panel fail "backup failed (exit ${rc}) — see ${LOG}"
  exit "${rc}"
}
trap on_err ERR

mkdir -p "$(dirname "$LOG")" "$(dirname "$STAMP")"

# ---- Preflight ----
command -v "$RCLONE" >/dev/null 2>&1 || die "rclone not found (RCLONE_BIN=$RCLONE)"
[ -d "$SOURCE" ] || die "source dir missing: $SOURCE"
case "$REMOTE" in *:*|*/*) die "MIMIR_OFFSITE_REMOTE must be a remote NAME only, got '$REMOTE'";; esac

# Warn (don't fail) if the rclone config is group/world-readable — it holds secrets.
CONF="${RCLONE_CONFIG:-$HOME/.config/rclone/rclone.conf}"
if [ -f "$CONF" ]; then
  PERM=$(stat -c '%a' "$CONF" 2>/dev/null || stat -f '%Lp' "$CONF" 2>/dev/null || echo "")
  case "$PERM" in ""|600|400) ;; *) log "WARN: $CONF is mode $PERM — should be 600 (holds OAuth token + crypt password)";; esac
fi

# Fail CLOSED unless the remote is provably a crypt remote with filename encryption.
# Prevents a misconfigured MIMIR_OFFSITE_REMOTE from uploading plaintext client data.
# Read only the type/encryption fields; never log the config (it contains the key).
CONF_SHOW=$("$RCLONE" config show "$REMOTE" 2>/dev/null || true)
RTYPE=$(printf '%s\n' "$CONF_SHOW" | awk -F' = ' '/^type =/{print $2; exit}')
FENC=$(printf '%s\n' "$CONF_SHOW" | awk -F' = ' '/^filename_encryption =/{print $2; exit}')
[ "$RTYPE" = "crypt" ] || die "remote '$REMOTE' is type '${RTYPE:-unknown}', not crypt — refusing to upload plaintext"
case "$FENC" in
  off) die "remote '$REMOTE' has filename_encryption=off — refusing (names would leak)";;
esac

base36_7() {
  local n="$1" alphabet="0123456789abcdefghijklmnopqrstuvwxyz" out="" remainder
  while [ "$n" -gt 0 ]; do
    remainder=$((n % 36))
    out="${alphabet:remainder:1}${out}"
    n=$((n / 36))
  done
  while [ "${#out}" -lt 7 ]; do out="0${out}"; done
  printf '%s' "$out"
}

DEST="${REMOTE}:current"
RUN_EPOCH=$(date -u +%s)
ARCHIVE_ID=$(base36_7 "$RUN_EPOCH")
# A single seven-character sibling keeps the encrypted archive path no longer
# than the existing `current` component. archive/<timestamp> pushed otherwise
# valid OneDrive paths over its 400-character limit.
ARCHIVE="${REMOTE}:${ARCHIVE_ID}"

# Connectivity + ensure destination exists (mkdir is idempotent and proves auth/write).
if [ -z "$DRY_RUN" ]; then
  "$RCLONE" mkdir "$DEST" 2>>"$LOG" || die "cannot reach/create ${DEST} — check rclone config / network"
else
  "$RCLONE" lsd "${REMOTE}:" >/dev/null 2>>"$LOG" || log "WARN: dry-run remote check failed (remote may be new/empty)"
fi

# ---- Preflight delete-count gate (mirrors sync-artifacts.sh's rsync safety) ----
# Files present in current/ but absent from the source would be MOVED to the archive
# by --backup-dir. That is non-destructive (30-day history), but an implausibly large
# change set (e.g. the source got wiped) should STOP and alert, not silently mirror an
# empty current/ and report success. Skipped on dry-run and on the first run (empty dest).
if [ -z "$DRY_RUN" ]; then
  DEST_LIST=$("$RCLONE" lsf -R --files-only "$DEST" "${RCLONE_FILTERS[@]}" 2>/dev/null | sort || true)
  SRC_LIST=$("$RCLONE" lsf -R --files-only "$SOURCE" "${RCLONE_FILTERS[@]}" 2>/dev/null | sort || true)
  DEST_N=$(printf '%s\n' "$DEST_LIST" | grep -c . || true)
  DELETES=$(comm -23 <(printf '%s\n' "$DEST_LIST") <(printf '%s\n' "$SRC_LIST") | grep -c . || true)
  if [ "$DEST_N" -gt 0 ] && [ "$DELETES" -gt 0 ]; then
    PCT=$(( DELETES * 100 / DEST_N ))
    if [ "$DELETES" -ge "$MAX_DELETE" ] || [ "$PCT" -gt "$MAX_DELETE_PCT" ]; then
      die "aborting: sync would remove ${DELETES}/${DEST_N} files (${PCT}%) from current/ — over threshold (max ${MAX_DELETE} or ${MAX_DELETE_PCT}%)"
    fi
    log "delete-count gate ok: ${DELETES}/${DEST_N} files (${PCT}%) would move to archive"
  fi
fi

log "starting offsite backup ${DRY_RUN:+(dry-run) }${SOURCE} → ${DEST} (archive: ${ARCHIVE})"

# Mirror the current state. Overwritten/deleted files are MOVED into the per-run
# archive dir (never destroyed). --max-delete is a second-line guard behind the
# preflight gate above.
# shellcheck disable=SC2086
"$RCLONE" sync "${SOURCE}/" "$DEST" \
  --backup-dir "$ARCHIVE" \
  --max-delete "$MAX_DELETE" \
  --transfers 4 --checkers 8 \
  "${RCLONE_FILTERS[@]}" \
  --log-file "$LOG" --log-level INFO \
  --stats 0 $DRY_RUN

if [ -n "$DRY_RUN" ]; then
  log "dry-run complete — no changes made, stamp/prune/panel skipped"
  exit 0
fi

# Prune whole archive run-dirs older than the retention horizon, BY NAME (the dir's
# UTC timestamp), not by object mtime — sync preserves source mtimes, so an old file
# just moved into the archive must NOT be judged old by its own mtime. Best-effort:
# a prune failure must not fail the backup (the mirror already succeeded).
prune_archive() {
  local cutoff_epoch cutoff_id legacy_cutoff d
  cutoff_epoch=$(date -u -d "${RETENTION_DAYS} days ago" +%s 2>/dev/null || true)
  if [ -z "$cutoff_epoch" ]; then
    log "WARN: cannot compute retention cutoff (non-GNU date?) — skipping prune"
    return 0
  fi
  cutoff_id=$(base36_7 "$cutoff_epoch")
  while IFS= read -r d; do
    d="${d%/}"; [ -n "$d" ] || continue
    if [[ "$d" =~ ^[0-9a-z]{7}$ ]] && [[ "$d" < "$cutoff_id" ]]; then
      if "$RCLONE" purge "${REMOTE}:${d}" 2>>"$LOG"; then
        log "pruned archive ${d} (older than ${RETENTION_DAYS}d)"
      else
        log "WARN: purge archive ${d} failed — will retry next run"
      fi
    fi
  done < <("$RCLONE" lsf --dirs-only "${REMOTE}:" 2>/dev/null || true)

  # Backward-compatible cleanup of the former archive/<UTC timestamp> layout.
  legacy_cutoff=$(date -u -d "@${cutoff_epoch}" +%Y-%m-%dT%H%M%SZ 2>/dev/null || true)
  while IFS= read -r d; do
    d="${d%/}"; [ -n "$d" ] || continue
    if [[ "$d" < "$legacy_cutoff" ]]; then
      if "$RCLONE" purge "${REMOTE}:archive/${d}" 2>>"$LOG"; then
        log "pruned archive/${d} (older than ${RETENTION_DAYS}d)"
      else
        log "WARN: purge archive/${d} failed — will retry next run"
      fi
    fi
  done < <("$RCLONE" lsf --dirs-only "${REMOTE}:archive" 2>/dev/null || true)
}
prune_archive

# Heartbeat + success panel.
date +%s > "$STAMP"
COUNT=$(find "$SOURCE" -type f ! -path '*/.git/*' | wc -l | tr -d ' ')
log "offsite backup complete: ${COUNT} files mirrored to ${DEST}"
push_panel pass "${COUNT} files, $(ts)"
