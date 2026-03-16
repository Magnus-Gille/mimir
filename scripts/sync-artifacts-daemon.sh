#!/bin/bash
# Sync ~/mimir/ between laptop and NAS — called by launchd every 30 min
#
# Policy:
#   1. Import new files from NAS inbox → laptop (no overwrites, no deletions)
#   2. Mirror laptop → NAS (laptop is authoritative)
#
# Hugin tasks write outputs to ~/mimir-inbox/ on the NAS.
# The inbox is a staging area — files are removed after successful import.

NAS="magnus@100.99.119.52"
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
rsync -a --ignore-existing --remove-source-files "$INBOX" "$LOCAL"

# Step 2: Mirror laptop → NAS (authoritative, with --delete)
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Pushing laptop → NAS..."
rsync -a --delete "$LOCAL" "$REMOTE"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Sync complete"
