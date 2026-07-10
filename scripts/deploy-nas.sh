#!/bin/bash
set -euo pipefail

# Deploy Mímir to NAS Pi
# Usage: ./scripts/deploy-nas.sh [hostname]

NAS_HOST="${1:-${MIMIR_NAS_HOST:-nas}}"
DEPLOY_USER="${DEPLOY_USER:-magnus}"
REMOTE="$DEPLOY_USER@$NAS_HOST"
REMOTE_DIR="/home/$DEPLOY_USER/mimir-server"

echo "==> Validating required remote environment..."
if ! ssh "$REMOTE" "test -f '$REMOTE_DIR/.env'"; then
  echo "ERROR: No .env file found at $REMOTE_DIR/.env" >&2
  exit 1
fi
if ! ssh "$REMOTE" "set -a; . '$REMOTE_DIR/.env'; test -n \"\${MIMIR_API_KEY:-}\" && test -n \"\${HEIMDALL_HUB_URL:-}\" && test -n \"\${HEIMDALL_FLEET_TOKEN:-}\""; then
  echo "ERROR: $REMOTE_DIR/.env must define non-empty MIMIR_API_KEY, HEIMDALL_HUB_URL, and HEIMDALL_FLEET_TOKEN" >&2
  exit 1
fi
echo "  required variables present (values not displayed)"

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

echo "==> Checking artifacts directory..."
ssh "$REMOTE" "mkdir -p /home/$DEPLOY_USER/mimir && echo '  /home/$DEPLOY_USER/mimir exists'"

echo "==> Restarting service..."
ssh "$REMOTE" "sudo systemctl restart mimir && sleep 2 && sudo systemctl status mimir --no-pager"

echo ""
echo "Deploy complete!"
echo "Health check: curl http://$NAS_HOST:3031/health"
