#!/bin/bash
# shellcheck disable=SC2034  # state shared with the sourced sync-common.sh
set -euo pipefail

# Sync ~/mimir/ between laptop and NAS — called by launchd every 30 min
#
# Policy:
#   1. Import new files from NAS inbox → laptop (no overwrites, no deletions)
#   2. Mirror laptop → NAS (laptop is authoritative)
#
# Hugin tasks write outputs to ~/mimir-inbox/ on the NAS.
# The inbox is a staging area — files are removed after successful import.

NAS_HOST="${MIMIR_NAS_HOST:-}"
NAS="${MIMIR_NAS:-${NAS_HOST:+mimir@$NAS_HOST}}"
LOCAL_ROOT="${MIMIR_LOCAL_ROOT:-$HOME/mimir}"
REMOTE_ROOT="${MIMIR_REMOTE_ROOT:-/home/mimir/mimir}"
REMOTE_INBOX="${MIMIR_REMOTE_INBOX:-/home/mimir/mimir-inbox}"
SYNC_STAMP="${MIMIR_REMOTE_SYNC_STAMP:-/home/mimir/mimir-sync.stamp}"
REMOTE_FRESHNESS_DIR="${MIMIR_REMOTE_FRESHNESS_DIR:-/var/lib/mimir/heimdall-freshness}"
REMOTE_FRESHNESS_PUBLISHER="${MIMIR_REMOTE_FRESHNESS_PUBLISHER:-}"
LOCAL="$LOCAL_ROOT/"
REMOTE="$NAS:$REMOTE_ROOT/"
INBOX="$NAS:$REMOTE_INBOX/"
STATE_DIR="${MIMIR_SYNC_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/mimir}"
PENDING="$STATE_DIR/import-pending"
QUARANTINE="${MIMIR_QUARANTINE_DIR:-${LOCAL%/}-quarantine}"
MAX_DELETE="${MIMIR_SYNC_MAX_DELETE:-1000}"
MAX_DELETE_PCT="${MIMIR_SYNC_MAX_DELETE_PCT:-20}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=sync-common.sh
. "$SCRIPT_DIR/sync-common.sh"

publish_sync_freshness() {
  # A deployment-specific publisher path is intentionally required once this
  # optional surface is enabled; no account or code-tree location is assumed.
  # Preserve the old heartbeat until that opt-in is explicitly configured.
  if [ -z "$REMOTE_FRESHNESS_PUBLISHER" ]; then
    [ "$1" = success ] || return 0
    ssh -o ConnectTimeout=5 -o BatchMode=yes "$NAS" "date +%s > '$SYNC_STAMP'" 2>/dev/null || true
    return 0
  fi
  local remote_dir remote_publisher state
  remote_dir=$(quote_remote_sh "$REMOTE_FRESHNESS_DIR")
  remote_publisher=$(quote_remote_sh "$REMOTE_FRESHNESS_PUBLISHER")
  state=$(quote_remote_sh "$1")
  ssh -o ConnectTimeout=5 -o BatchMode=yes "$NAS" \
    "if [ -d $remote_dir ]; then $remote_publisher sync $state; fi"
}

# A stalled sync must not be silent. Every refusal to mirror (anything that
# exits nonzero before the push completes, configuration errors included) pushes
# a fail-state Heimdall panel and moves the NAS freshness record to error; both
# carry only the class name.
on_sync_refusal() {
  # The push-failure branch below already published error for this class.
  [ "$1" != push_failed ] || return 0
  [ -n "$NAS" ] || return 0
  publish_sync_freshness error 2>/dev/null || true
}
REFUSAL_CLASS=config_invalid
trap sync_exit_trap EXIT

[ -n "$NAS" ] || { echo "ERROR: set MIMIR_NAS_HOST or MIMIR_NAS=user@host." >&2; exit 1; }
case "$MAX_DELETE" in ''|*[!0-9]*) echo "ERROR: MIMIR_SYNC_MAX_DELETE must be a positive integer." >&2; exit 1;; esac
[ "$MAX_DELETE" -ge 1 ] || { echo "ERROR: MIMIR_SYNC_MAX_DELETE must be at least 1." >&2; exit 1; }
case "$MAX_DELETE_PCT" in ''|*[!0-9]*) echo "ERROR: MIMIR_SYNC_MAX_DELETE_PCT must be an integer from 0 to 100." >&2; exit 1;; esac
[ "$MAX_DELETE_PCT" -le 100 ] || { echo "ERROR: MIMIR_SYNC_MAX_DELETE_PCT must be at most 100." >&2; exit 1; }
MAX_DELETE_PCT=$((10#$MAX_DELETE_PCT))
build_mirror_filters || exit 1
REFUSAL_CLASS=unexpected

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Sync triggered"

# Check if NAS is reachable (2s timeout)
if ! ssh -o ConnectTimeout=2 -o BatchMode=yes "$NAS" true 2>/dev/null; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] NAS not reachable — skipping"
    exit 0
fi

# Keep imports outside the mirrored/servable tree until the scanner has
# accepted them. The pending tree itself is the durable recovery record: if
# rsync or the scanner fails, a later invocation must process these paths
# before it can mirror anything.
mkdir -p "$PENDING"
chmod 700 "$STATE_DIR" "$PENDING"

# Step 1: Import new files from inbox into durable staging
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Importing from inbox..."
REFUSAL_CLASS=import_failed
if ! rsync -a --ignore-existing --remove-source-files "$INBOX" "$PENDING/"; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Import FAILED; pending files remain staged at $PENDING; refusing to scan or mirror."
  exit 1
fi

# Step 1.5: Secret-scan every pending file, including files left by an older
# failed invocation. Hits are quarantined outside both staging and $LOCAL.
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Scanning pending imports for secrets..."
if ! resolve_scanner "$SCRIPT_DIR"; then
  REFUSAL_CLASS=scanner_unavailable
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Secret scanner UNAVAILABLE (class=scanner_unavailable): $SCANNER_DIAGNOSTIC; pending files remain staged at $PENDING; refusing to mirror."
  exit "$SCANNER_UNAVAILABLE_RC"
fi
REFUSAL_CLASS=scan_failed
if ! "$SCAN_NODE" "$SCAN_CLI" "$PENDING" "$QUARANTINE"; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Secret scan FAILED (class=scan_failed); pending files remain staged at $PENDING; refusing to mirror."
  exit 1
fi

# Step 1.6: Promote only verified content. --ignore-existing prevents an
# inbox path from overwriting a local artifact; --remove-source-files makes
# successful promotion durable. Any collision or partial promotion remains in
# staging and blocks the mirror until an operator resolves it without data loss.
if [ -n "$(find "$PENDING" -mindepth 1 -print -quit)" ]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Promoting verified imports..."
  REFUSAL_CLASS=promotion_failed
  if ! rsync -a --ignore-existing --remove-source-files "$PENDING/" "$LOCAL"; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Promotion FAILED; pending files remain staged at $PENDING; refusing to mirror."
    exit 1
  fi
  find "$PENDING" -mindepth 1 -depth -type d -empty -delete
  if [ -n "$(find "$PENDING" -mindepth 1 -print -quit)" ]; then
    REFUSAL_CLASS=promotion_blocked
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Promotion BLOCKED by local path collisions; resolve $PENDING before syncing."
    exit 1
  fi
fi
rmdir "$PENDING"

# Step 2: Mirror laptop → NAS (laptop is source of truth)
# Safety: abort if --delete would remove >20% of remote files
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Pushing laptop → NAS..."
REFUSAL_CLASS=dry_run_failed
DRY_OUTPUT=$(rsync -ani --delete --out-format='%i' "${MIRROR_FILTERS[@]}" "$LOCAL" "$REMOTE")
DELETES=$(printf '%s\n' "$DRY_OUTPUT" | count_dry_run_deletes)
if [ "$DELETES" -gt 0 ]; then
  REFUSAL_CLASS=delete_gate
  REMOTE_POPULATION=$(ssh -o ConnectTimeout=5 -o BatchMode=yes "$NAS" "$(build_remote_population_command "$REMOTE_ROOT")")
  REMOTE_TOTAL=${#REMOTE_POPULATION}
  if [ "$REMOTE_TOTAL" -gt 0 ]; then
    PCT=$(( (DELETES * 100 + REMOTE_TOTAL - 1) / REMOTE_TOTAL ))
  else
    PCT=100
  fi
  if [ "$DELETES" -ge "$MAX_DELETE" ] || [ "$(( DELETES * 100 ))" -gt "$(( MAX_DELETE_PCT * REMOTE_TOTAL ))" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ABORT: --delete would remove $DELETES/$REMOTE_TOTAL remote entries ($PCT%; max $MAX_DELETE entries or $MAX_DELETE_PCT%)."
    exit 1
  fi
fi
REFUSAL_CLASS=push_failed
if rsync -a --delete --max-delete="$MAX_DELETE" "${MIRROR_FILTERS[@]}" "$LOCAL" "$REMOTE"; then
  # Publishes only a normalized UTC timestamp and state, outside the mirrored
  # tree. A restricted probe cannot traverse the archive or read artifacts.
  if ! publish_sync_freshness success; then
    REFUSAL_CLASS=freshness_publish_failed
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Sync completed but freshness publication FAILED"
    exit 1
  fi
  MIRROR_COMPLETED=1
  heimdall_sync_panel pass "class=ok"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Sync complete"
else
  RC=$?
  publish_sync_freshness error || true
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Push FAILED (rsync exit $RC)"
  exit "$RC"
fi
