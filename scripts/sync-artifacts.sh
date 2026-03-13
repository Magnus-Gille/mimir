#!/bin/bash
set -euo pipefail

# Sync mgc/ folder from laptop to NAS Pi artifacts directory
# Usage: ./scripts/sync-artifacts.sh [hostname]

NAS_HOST="${1:-100.99.119.52}"
DEPLOY_USER="${DEPLOY_USER:-magnus}"
SOURCE="${MGC_DIR:-$HOME/mgc/}"
DEST="$DEPLOY_USER@$NAS_HOST:/home/$DEPLOY_USER/artifacts/mgc/"

echo "==> Syncing $SOURCE to $DEST..."
rsync -av \
  --exclude='.git/' \
  --exclude='.DS_Store' \
  --exclude='node_modules/' \
  --exclude='.playwright-mcp/' \
  --exclude='tools/' \
  --exclude='files/' \
  --exclude='.pytest_cache/' \
  --exclude='test-results/' \
  "$SOURCE" "$DEST"

echo "==> Sync complete."
echo "Files synced: $(rsync -avn --exclude='.git/' --exclude='.DS_Store' --exclude='node_modules/' "$SOURCE" "$DEST" 2>/dev/null | grep -c '^[^>]' || echo 'unknown')"
