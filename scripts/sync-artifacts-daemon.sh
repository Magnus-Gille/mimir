#!/bin/bash
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
[ -n "$NAS" ] || { echo "ERROR: set MIMIR_NAS_HOST or MIMIR_NAS=user@host." >&2; exit 1; }
LOCAL_ROOT="${MIMIR_LOCAL_ROOT:-$HOME/mimir}"
REMOTE_ROOT="${MIMIR_REMOTE_ROOT:-/home/mimir/mimir}"
REMOTE_INBOX="${MIMIR_REMOTE_INBOX:-/home/mimir/mimir-inbox}"
SYNC_STAMP="${MIMIR_REMOTE_SYNC_STAMP:-/home/mimir/mimir-sync.stamp}"
LOCAL="$LOCAL_ROOT/"
REMOTE="$NAS:$REMOTE_ROOT/"
INBOX="$NAS:$REMOTE_INBOX/"
STATE_DIR="${MIMIR_SYNC_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/mimir}"
PENDING="$STATE_DIR/import-pending"
QUARANTINE="${MIMIR_QUARANTINE_DIR:-${LOCAL%/}-quarantine}"
MAX_DELETE="${MIMIR_SYNC_MAX_DELETE:-1000}"
MAX_DELETE_PCT="${MIMIR_SYNC_MAX_DELETE_PCT:-20}"

case "$MAX_DELETE" in ''|*[!0-9]*) echo "ERROR: MIMIR_SYNC_MAX_DELETE must be a positive integer." >&2; exit 1;; esac
[ "$MAX_DELETE" -ge 1 ] || { echo "ERROR: MIMIR_SYNC_MAX_DELETE must be at least 1." >&2; exit 1; }
case "$MAX_DELETE_PCT" in ''|*[!0-9]*) echo "ERROR: MIMIR_SYNC_MAX_DELETE_PCT must be an integer from 0 to 100." >&2; exit 1;; esac
[ "$MAX_DELETE_PCT" -le 100 ] || { echo "ERROR: MIMIR_SYNC_MAX_DELETE_PCT must be at most 100." >&2; exit 1; }

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
if ! rsync -a --ignore-existing --remove-source-files "$INBOX" "$PENDING/"; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Import FAILED; pending files remain staged at $PENDING; refusing to scan or mirror."
  exit 1
fi

PENDING_FILES=$(cd "$PENDING" && find . ! -type d -print | sed 's#^\./##')

# Step 1.5: Secret-scan every pending file, including files left by an older
# failed invocation. Hits are quarantined outside both staging and $LOCAL.
if [ -n "$PENDING_FILES" ]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Scanning pending imports for secrets..."
  if ! printf '%s\n' "$PENDING_FILES" | node "$(dirname "$0")/../dist/cli/secret-scan.js" --stdin "$PENDING" "$QUARANTINE"; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Secret scan FAILED; pending files remain staged at $PENDING; refusing to mirror."
    exit 1
  fi
fi

# Step 1.6: Promote only verified content. --ignore-existing prevents an
# inbox path from overwriting a local artifact; --remove-source-files makes
# successful promotion durable. Any collision or partial promotion remains in
# staging and blocks the mirror until an operator resolves it without data loss.
if [ -n "$(find "$PENDING" -mindepth 1 -print -quit)" ]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Promoting verified imports..."
  if ! rsync -a --ignore-existing --remove-source-files "$PENDING/" "$LOCAL"; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Promotion FAILED; pending files remain staged at $PENDING; refusing to mirror."
    exit 1
  fi
  find "$PENDING" -mindepth 1 -depth -type d -empty -delete
  if [ -n "$(find "$PENDING" -mindepth 1 -print -quit)" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Promotion BLOCKED by local path collisions; resolve $PENDING before syncing."
    exit 1
  fi
fi
rmdir "$PENDING"

# Step 2: Mirror laptop → NAS (laptop is source of truth)
# Safety: abort if --delete would remove >20% of remote files
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Pushing laptop → NAS..."
DRY_OUTPUT=$(rsync -an --delete "$LOCAL" "$REMOTE" 2>/dev/null)
DELETES=$(printf '%s\n' "$DRY_OUTPUT" | awk '/^deleting / { n++ } END { print n + 0 }')
if [ "$DELETES" -gt 0 ]; then
  REMOTE_POPULATION=$(ssh -o ConnectTimeout=5 -o BatchMode=yes "$NAS" "find '$REMOTE_ROOT' -mindepth 1 -printf .")
  REMOTE_TOTAL=${#REMOTE_POPULATION}
  if [ "$REMOTE_TOTAL" -gt 0 ]; then
    PCT=$(( DELETES * 100 / REMOTE_TOTAL ))
  else
    PCT=100
  fi
  if [ "$DELETES" -ge "$MAX_DELETE" ] || [ "$PCT" -gt "$MAX_DELETE_PCT" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ABORT: --delete would remove $DELETES/$REMOTE_TOTAL remote entries ($PCT%; max $MAX_DELETE entries or $MAX_DELETE_PCT%)."
    exit 1
  fi
fi
if rsync -a --delete --max-delete="$MAX_DELETE" "$LOCAL" "$REMOTE"; then
  # Heartbeat for Heimdall's sync-freshness probe. Written OUTSIDE the mirrored
  # tree so the --delete above can't remove it. Records when the sync last ran
  # successfully (every 30 min) rather than newest-content age — which avoids
  # false "Backup stale" criticals when no new files have been created lately.
  ssh -o ConnectTimeout=5 -o BatchMode=yes "$NAS" "date +%s > '$SYNC_STAMP'" 2>/dev/null || true
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Sync complete"
else
  RC=$?
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Push FAILED (rsync exit $RC)"
  exit "$RC"
fi
