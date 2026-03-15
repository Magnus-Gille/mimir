#!/bin/bash
# Daemon wrapper for sync-artifacts — called by launchd
# Adds timestamps, checks NAS reachability, and exits cleanly on failure

NAS_HOST="100.99.119.52"
DEPLOY_USER="magnus"
SOURCE="$HOME/mimir/"
DEST="$DEPLOY_USER@$NAS_HOST:/home/$DEPLOY_USER/mimir/"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Sync triggered"

# Check if NAS is reachable (2s timeout)
if ! ssh -o ConnectTimeout=2 -o BatchMode=yes "$DEPLOY_USER@$NAS_HOST" true 2>/dev/null; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] NAS not reachable at $NAS_HOST — skipping"
    exit 0
fi

rsync -a --delete "$SOURCE" "$DEST"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Sync complete"
