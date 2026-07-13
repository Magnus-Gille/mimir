#!/bin/bash
set -euo pipefail

# Deploy Mímir to NAS Pi
# Usage: ./scripts/deploy-nas.sh [hostname]

NAS_HOST="${1:-${MIMIR_NAS_HOST:-nas}}"
DEPLOY_USER="${DEPLOY_USER:-magnus}"
REMOTE="$DEPLOY_USER@$NAS_HOST"
REMOTE_DIR="/home/$DEPLOY_USER/mimir-server"

WORKTREE_STATUS=$(git status --porcelain --untracked-files=normal)
if [ -n "$WORKTREE_STATUS" ]; then
  echo "ERROR: Refusing to deploy a dirty worktree; commit or stash every change first." >&2
  exit 1
fi
DEPLOY_COMMIT=$(git rev-parse HEAD)
case "$DEPLOY_COMMIT" in
  *[!0-9a-f]*|'') echo "ERROR: Could not determine an exact Git deployment commit." >&2; exit 1;;
esac
[ "${#DEPLOY_COMMIT}" -eq 40 ] || { echo "ERROR: Git deployment commit must be a full 40-character SHA." >&2; exit 1; }

echo "==> Validating required remote environment..."
if ! ssh "$REMOTE" "test -f '$REMOTE_DIR/.env'"; then
  echo "ERROR: No .env file found at $REMOTE_DIR/.env" >&2
  exit 1
fi
ssh "$REMOTE" "chmod 600 '$REMOTE_DIR/.env'"
if ! ssh "$REMOTE" "set -a; . '$REMOTE_DIR/.env'; test -n \"\${MIMIR_API_KEY:-}\" && test -n \"\${HEIMDALL_HUB_URL:-}\" && test -n \"\${HEIMDALL_FLEET_TOKEN:-}\""; then
  echo "ERROR: $REMOTE_DIR/.env must define non-empty MIMIR_API_KEY, HEIMDALL_HUB_URL, and HEIMDALL_FLEET_TOKEN" >&2
  exit 1
fi
echo "  required variables present (values not displayed)"

PREVIOUS_COMMIT=$(ssh "$REMOTE" "if [ -f '$REMOTE_DIR/.deployed-commit' ]; then head -n 1 '$REMOTE_DIR/.deployed-commit'; else printf unknown; fi")
case "$PREVIOUS_COMMIT" in
  *[!0-9a-f]*|'') ROLLBACK_TARGET="<known-good-commit>";;
  *) if [ "${#PREVIOUS_COMMIT}" -eq 40 ]; then ROLLBACK_TARGET="$PREVIOUS_COMMIT"; else ROLLBACK_TARGET="<known-good-commit>"; fi;;
esac

deployment_failed() {
  local rc=$?
  if [ "$MARKER_INVALIDATED" -eq 1 ]; then
    echo "ERROR: Deployment of $DEPLOY_COMMIT did not complete; the remote acceptance marker was cleared and not recreated." >&2
  else
    echo "ERROR: Deployment of $DEPLOY_COMMIT did not complete before remote artifact mutation; the previous acceptance marker remains valid." >&2
  fi
  echo "Rollback: check out $ROLLBACK_TARGET in a clean worktree and run ./scripts/deploy-nas.sh $NAS_HOST" >&2
  exit "$rc"
}
MARKER_INVALIDATED=0
trap deployment_failed ERR

echo "==> Building locally..."
npm run build

# The marker describes the currently accepted artifact, not merely the most
# recent successful deploy. Invalidate it before the first remote code-tree
# mutation so any interrupted deploy is visibly unaccepted.
echo "==> Invalidating previous deployment acceptance..."
ssh "$REMOTE" "rm -f '$REMOTE_DIR/.deployed-commit'"
MARKER_INVALIDATED=1

# Old deploys could accidentally copy a worktree's .git file. Remove either a
# file or directory before transfer, and exclude the basename form below so
# rsync protects both checkout shapes.
echo "==> Removing remote Git metadata..."
ssh "$REMOTE" "rm -rf '$REMOTE_DIR/.git'"

echo "==> Syncing to $REMOTE:$REMOTE_DIR..."
rsync -av --delete \
  --exclude='node_modules/' \
  --exclude='.git' \
  --exclude='.env' \
  --exclude='.deployed-commit' \
  --exclude='tests/' \
  --exclude='.DS_Store' \
  ./ "$REMOTE:$REMOTE_DIR/"

echo "==> Installing dependencies on NAS Pi..."
ssh "$REMOTE" "cd '$REMOTE_DIR' && npm ci --omit=dev"

echo "==> Refreshing systemd units..."
ssh "$REMOTE" "sudo install -m 0644 '$REMOTE_DIR/mimir.service' '$REMOTE_DIR/mimir-offsite.service' '$REMOTE_DIR/mimir-offsite.timer' /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable mimir && if sudo systemctl is-enabled --quiet mimir-offsite.timer; then sudo systemctl restart mimir-offsite.timer; fi"

echo "==> Checking artifacts directory..."
ssh "$REMOTE" "mkdir -p /home/$DEPLOY_USER/mimir && echo '  /home/$DEPLOY_USER/mimir exists'"

echo "==> Restarting service..."
ssh "$REMOTE" "set -eu; sudo systemctl restart mimir; set -a; . '$REMOTE_DIR/.env'; set +a; port=\${MIMIR_PORT:-3031}; healthy=0; for attempt in 1 2 3 4 5; do if curl -fsS --max-time 3 \"http://127.0.0.1:\${port}/health\" >/dev/null; then healthy=1; break; fi; sleep 1; done; if [ \"\$healthy\" -ne 1 ]; then sudo systemctl status mimir --no-pager || true; exit 1; fi"

echo "==> Recording accepted deployment commit..."
ssh "$REMOTE" "set -eu; marker='$REMOTE_DIR/.deployed-commit'; tmp='$REMOTE_DIR/.deployed-commit.tmp.\$\$'; trap 'rm -f \"\$tmp\"' EXIT; printf '%s\\n' '$DEPLOY_COMMIT' > \"\$tmp\"; chmod 644 \"\$tmp\"; mv -f \"\$tmp\" \"\$marker\"; trap - EXIT"

trap - ERR

echo ""
echo "Deploy complete!"
echo "Accepted commit: $DEPLOY_COMMIT"
echo "Health check: ssh $REMOTE curl http://127.0.0.1:3031/health"
echo "Rollback: check out $ROLLBACK_TARGET in a clean worktree and run ./scripts/deploy-nas.sh $NAS_HOST"
