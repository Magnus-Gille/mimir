#!/bin/bash
set -euo pipefail

# Manual sync — same logic as daemon but with verbose output
#
# Usage: ./scripts/sync-artifacts.sh [nas-host]

NAS="${1:-magnus@100.99.119.52}"
LOCAL="$HOME/mimir/"
REMOTE="$NAS:/home/magnus/mimir/"
INBOX="$NAS:/home/magnus/mimir-inbox/"

# Step 1: Import new files from inbox
echo "==> Importing from inbox..."
rsync -av --ignore-existing --remove-source-files "$INBOX" "$LOCAL"

# Step 2: Mirror laptop → NAS (laptop is source of truth)
# Safety: abort if --delete would remove >20% of remote files
echo "==> Pushing laptop → NAS (with --delete)..."
DRY_OUTPUT=$(rsync -an --delete "$LOCAL" "$REMOTE" 2>/dev/null)
TOTAL=$(echo "$DRY_OUTPUT" | grep -c '.' || true)
DELETES=$(echo "$DRY_OUTPUT" | grep -c '^deleting ' || true)
if [ "$TOTAL" -gt 0 ] && [ "$DELETES" -gt 0 ]; then
  PCT=$(( DELETES * 100 / TOTAL ))
  if [ "$PCT" -gt 20 ]; then
    echo "ERROR: --delete would remove $DELETES files ($PCT%% of changes). Aborting." >&2
    echo "Run with caution: rsync -av --delete $LOCAL $REMOTE" >&2
    exit 1
  fi
fi
rsync -av --delete "$LOCAL" "$REMOTE"

echo "==> Sync complete."
