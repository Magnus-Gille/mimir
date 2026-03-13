#!/bin/bash
set -euo pipefail

# Deploy Mímir to NAS Pi
# Usage: ./scripts/deploy-nas.sh [hostname]

NAS_HOST="${1:-100.99.119.52}"
DEPLOY_USER="${DEPLOY_USER:-magnus}"
REMOTE="$DEPLOY_USER@$NAS_HOST"
REMOTE_DIR="/home/$DEPLOY_USER/mimir"

echo "==> Building locally..."
npm run build

echo "==> Syncing to $REMOTE:$REMOTE_DIR..."
rsync -av --delete \
  --exclude='node_modules/' \
  --exclude='.git/' \
  --exclude='.env' \
  --exclude='tests/' \
  --exclude='.DS_Store' \
  ./ "$REMOTE:$REMOTE_DIR/"

echo "==> Installing dependencies on NAS Pi..."
ssh "$REMOTE" "cd $REMOTE_DIR && npm install --omit=dev"

echo "==> Installing systemd service..."
ssh "$REMOTE" "sudo cp $REMOTE_DIR/mimir.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable mimir"

echo "==> Checking for .env file..."
if ssh "$REMOTE" "test -f $REMOTE_DIR/.env"; then
  echo "  .env exists"
else
  echo "  WARNING: No .env file found at $REMOTE_DIR/.env"
  echo "  Create one with: MIMIR_API_KEY=<key>"
  echo "  Generate a key: openssl rand -hex 32"
fi

echo "==> Checking artifacts directory..."
ssh "$REMOTE" "mkdir -p /home/$DEPLOY_USER/artifacts && echo '  /home/$DEPLOY_USER/artifacts exists'"

echo "==> Restarting service..."
ssh "$REMOTE" "sudo systemctl restart mimir && sleep 2 && sudo systemctl status mimir --no-pager"

echo ""
echo "Deploy complete!"
echo "Health check: curl http://$NAS_HOST:3031/health"
