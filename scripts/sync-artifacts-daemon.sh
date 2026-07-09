#!/bin/bash
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

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Sync triggered"

# Check if NAS is reachable (2s timeout)
if ! ssh -o ConnectTimeout=2 -o BatchMode=yes $NAS true 2>/dev/null; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] NAS not reachable — skipping"
    exit 0
fi

# Step 1: Import new files from inbox (skip collisions, remove transferred originals)
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Importing from inbox..."
IMPORTED=$(rsync -a --ignore-existing --remove-source-files --out-format='%n' "$INBOX" "$LOCAL" | grep -v '/$' || true)

# Step 1.5: Secret-scan newly imported files before they can reach the NAS's
# Bearer-servable tree. Hits are quarantined out of $LOCAL and alerted —
# see src/secret-scan.ts and mimir#13.
if [ -n "$IMPORTED" ]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Scanning imported files for secrets..."
  echo "$IMPORTED" | node "$(dirname "$0")/../dist/cli/secret-scan.js" --stdin "$LOCAL"
fi

# Step 2: Mirror laptop → NAS (laptop is source of truth)
# Safety: abort if --delete would remove >20% of remote files
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Pushing laptop → NAS..."
DRY_OUTPUT=$(rsync -an --delete "$LOCAL" "$REMOTE" 2>/dev/null)
TOTAL=$(echo "$DRY_OUTPUT" | grep -c '.' || true)
DELETES=$(echo "$DRY_OUTPUT" | grep -c '^deleting ' || true)
if [ "$TOTAL" -gt 0 ] && [ "$DELETES" -gt 0 ]; then
  PCT=$(( DELETES * 100 / TOTAL ))
  if [ "$PCT" -gt 20 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ABORT: --delete would remove $DELETES files ($PCT%). Skipping sync."
    exit 1
  fi
fi
if rsync -a --delete "$LOCAL" "$REMOTE"; then
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
