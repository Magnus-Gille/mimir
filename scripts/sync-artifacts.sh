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

# Step 2: Mirror laptop → NAS
echo "==> Pushing laptop → NAS (with --delete)..."
rsync -av --delete "$LOCAL" "$REMOTE"

echo "==> Sync complete."
