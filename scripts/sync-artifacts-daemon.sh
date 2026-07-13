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

NAS="${MIMIR_NAS:-magnus@${MIMIR_NAS_HOST:-nas}}"
LOCAL="$HOME/mimir/"
REMOTE="$NAS:/home/magnus/mimir/"
INBOX="$NAS:/home/magnus/mimir-inbox/"
REMOTE_ROOT="/home/magnus/mimir"
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

# Step 1: Import new files from inbox (skip collisions, remove transferred originals)
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Importing from inbox..."
if ! IMPORTED_RAW=$(rsync -a --ignore-existing --remove-source-files --out-format='%n' "$INBOX" "$LOCAL"); then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Import FAILED; refusing to scan or mirror partial results."
  exit 1
fi
IMPORTED=$(printf '%s\n' "$IMPORTED_RAW" | sed '/\/$/d')

# Step 1.5: Secret-scan newly imported files before they can reach the NAS's
# Bearer-servable tree. Hits are quarantined out of $LOCAL and alerted —
# see src/secret-scan.ts and mimir#13.
if [ -n "$IMPORTED" ]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Scanning imported files for secrets..."
  if ! printf '%s\n' "$IMPORTED" | node "$(dirname "$0")/../dist/cli/secret-scan.js" --stdin "$LOCAL"; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Secret scan FAILED; refusing to mirror unverified imports."
    exit 1
  fi
fi

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
  ssh -o ConnectTimeout=5 -o BatchMode=yes "$NAS" "date +%s > /home/magnus/mimir-sync.stamp" 2>/dev/null || true
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Sync complete"
else
  RC=$?
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Push FAILED (rsync exit $RC)"
  exit "$RC"
fi
