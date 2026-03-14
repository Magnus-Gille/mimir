#!/bin/bash
set -euo pipefail

# Sync ~/mimir/ archive from laptop to NAS Pi
# Usage: ./scripts/sync-artifacts.sh [hostname]

NAS_HOST="${1:-100.99.119.52}"
DEPLOY_USER="${DEPLOY_USER:-magnus}"
SOURCE="$HOME/mimir/"
DEST="$DEPLOY_USER@$NAS_HOST:/home/$DEPLOY_USER/artifacts/mgc/"

echo "==> Syncing $SOURCE to $DEST..."
rsync -av "$SOURCE" "$DEST"

echo "==> Sync complete."
