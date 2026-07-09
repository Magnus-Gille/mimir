#!/bin/bash
set -euo pipefail

# Manual sync — same logic as daemon but with verbose output
#
# Usage: ./scripts/sync-artifacts.sh [nas-host]

NAS="${1:-${MIMIR_NAS:-magnus@${MIMIR_NAS_HOST:-nas}}}"
LOCAL="$HOME/mimir/"
REMOTE="$NAS:/home/magnus/mimir/"
INBOX="$NAS:/home/magnus/mimir-inbox/"

# Step 1: Import new files from inbox
echo "==> Importing from inbox..."
IMPORTED=$(rsync -a --ignore-existing --remove-source-files --out-format='%n' "$INBOX" "$LOCAL" | grep -v '/$' || true)

# Step 1.5: Secret-scan newly imported files before they can reach the NAS's
# Bearer-servable tree. Hits are quarantined out of $LOCAL and alerted —
# see src/secret-scan.ts and mimir#13.
if [ -n "$IMPORTED" ]; then
  echo "==> Scanning imported files for secrets..."
  echo "$IMPORTED" | node "$(dirname "$0")/../dist/cli/secret-scan.js" --stdin "$LOCAL"
else
  echo "==> No new files imported — skipping secret scan."
fi

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
