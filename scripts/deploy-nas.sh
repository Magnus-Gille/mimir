#!/bin/bash
set -euo pipefail

# Deploy Mímir to NAS Pi
# Usage: ./scripts/deploy-nas.sh [hostname]

NAS_HOST="${1:-${MIMIR_NAS_HOST:-}}"
[ -n "$NAS_HOST" ] || { echo "ERROR: pass a deployment host or set MIMIR_NAS_HOST." >&2; exit 1; }
DEPLOY_USER="${MIMIR_DEPLOY_USER:-mimir}"
[[ "$DEPLOY_USER" =~ ^[a-z_][a-z0-9_-]*$ ]] || {
  echo "ERROR: MIMIR_DEPLOY_USER must be a Linux account name." >&2
  exit 1
}
REMOTE="$DEPLOY_USER@$NAS_HOST"
REMOTE_DIR="/home/$DEPLOY_USER/mimir-server"
REMOTE_ROOT="/home/$DEPLOY_USER/mimir"

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
if ! ssh "$REMOTE" "set -a; . '$REMOTE_DIR/.env'; test -n \"\${MIMIR_API_KEY:-}\" && test -n \"\${MIMIR_ROOT_DIR:-}\" && { test -z \"\${MIMIR_SHARE_SECRET:-}\" || test -n \"\${MIMIR_BASE_URL:-}\"; }"; then
  echo "ERROR: $REMOTE_DIR/.env must define non-empty MIMIR_API_KEY and MIMIR_ROOT_DIR; MIMIR_BASE_URL is also required when MIMIR_SHARE_SECRET is set" >&2
  exit 1
fi
echo "  required variables present (values not displayed)"

# Reporter configuration is optional, but a half-configured pair can never
# authenticate and is therefore a deployment error. Inspect only presence and
# return a bounded state word so neither secret can reach local output.
HEIMDALL_STATE=$(ssh "$REMOTE" "set -a; . '$REMOTE_DIR/.env'; if [ -n \"\${HEIMDALL_HUB_URL:-}\" ] && [ -n \"\${HEIMDALL_FLEET_TOKEN:-}\" ]; then printf configured; elif [ -z \"\${HEIMDALL_HUB_URL:-}\" ] && [ -z \"\${HEIMDALL_FLEET_TOKEN:-}\" ]; then printf disabled; else printf partial; fi")
case "$HEIMDALL_STATE" in
  configured)
    echo "  Heimdall reporting configured (values not displayed)"
    ;;
  disabled)
    echo "WARNING: Heimdall reporting is disabled; add both HEIMDALL_HUB_URL and HEIMDALL_FLEET_TOKEN to $REMOTE_DIR/.env to enable it." >&2
    ;;
  partial)
    echo "ERROR: $REMOTE_DIR/.env must define both HEIMDALL_HUB_URL and HEIMDALL_FLEET_TOKEN, or neither." >&2
    exit 1
    ;;
  *)
    echo "ERROR: Could not determine Heimdall reporter configuration state on $REMOTE." >&2
    exit 1
    ;;
esac
ssh "$REMOTE" "chmod 600 '$REMOTE_DIR/.env'"

PREVIOUS_COMMIT=$(ssh "$REMOTE" "if [ -f '$REMOTE_DIR/.deployed-commit' ]; then head -n 1 '$REMOTE_DIR/.deployed-commit'; else printf unknown; fi")
case "$PREVIOUS_COMMIT" in
  *[!0-9a-f]*|'') ROLLBACK_TARGET="<known-good-commit>";;
  *) if [ "${#PREVIOUS_COMMIT}" -eq 40 ]; then ROLLBACK_TARGET="$PREVIOUS_COMMIT"; else ROLLBACK_TARGET="<known-good-commit>"; fi;;
esac

deployment_failed() {
  local rc=$?
  case "$MARKER_INVALIDATION_STATE" in
    confirmed)
      echo "ERROR: Deployment of $DEPLOY_COMMIT did not complete; the remote acceptance marker was cleared and not recreated." >&2
      ;;
    unknown)
      echo "ERROR: Deployment of $DEPLOY_COMMIT did not complete; acceptance-marker state is unknown because the invalidation command did not complete. Verify the remote marker before trusting provenance." >&2
      ;;
    not-attempted)
      echo "ERROR: Deployment of $DEPLOY_COMMIT did not complete before marker invalidation; this deploy attempted no remote code-tree mutation." >&2
      ;;
  esac
  echo "Rollback: check out $ROLLBACK_TARGET in a clean worktree and run MIMIR_DEPLOY_USER=$DEPLOY_USER ./scripts/deploy-nas.sh $NAS_HOST" >&2
  exit "$rc"
}
MARKER_INVALIDATION_STATE="not-attempted"
trap deployment_failed ERR

echo "==> Building locally..."
npm run build

# The marker describes the currently accepted artifact, not merely the most
# recent successful deploy. Invalidate it before the first remote code-tree
# mutation so any interrupted deploy is visibly unaccepted.
echo "==> Invalidating previous deployment acceptance..."
MARKER_INVALIDATION_STATE="unknown"
ssh "$REMOTE" "rm -f '$REMOTE_DIR/.deployed-commit'"
MARKER_INVALIDATION_STATE="confirmed"

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
  --exclude='STATUS.md' \
  --exclude='backup.log' \
  --exclude='tests/' \
  --exclude='.DS_Store' \
  ./ "$REMOTE:$REMOTE_DIR/"

echo "==> Installing dependencies on NAS Pi..."
ssh "$REMOTE" "cd '$REMOTE_DIR' && npm ci --omit=dev"

echo "==> Refreshing systemd units..."
ssh "$REMOTE" "set -eu; unit_tmp=\$(mktemp -d /tmp/mimir-units.XXXXXX); trap 'rm -rf \"\$unit_tmp\"' EXIT; for unit in mimir.service mimir-offsite.service mimir-offsite.timer; do sed -e 's|^User=mimir$|User=$DEPLOY_USER|' -e 's|/home/mimir|/home/$DEPLOY_USER|g' '$REMOTE_DIR/'\"\$unit\" > \"\$unit_tmp/\$unit\"; done; sudo install -m 0644 \"\$unit_tmp/mimir.service\" \"\$unit_tmp/mimir-offsite.service\" \"\$unit_tmp/mimir-offsite.timer\" /etc/systemd/system/; sudo systemctl daemon-reload; sudo systemctl enable mimir; if sudo systemctl is-enabled --quiet mimir-offsite.timer; then sudo systemctl restart mimir-offsite.timer; fi"

echo "==> Checking artifacts directory..."
ssh "$REMOTE" "mkdir -p '$REMOTE_ROOT' && echo '  $REMOTE_ROOT exists'"

echo "==> Restarting service..."
ssh "$REMOTE" "set -eu; sudo systemctl restart mimir; set -a; . '$REMOTE_DIR/.env'; set +a; port=\${MIMIR_PORT:-3031}; healthy=0; for attempt in 1 2 3 4 5; do if curl -fsS --max-time 3 \"http://127.0.0.1:\${port}/health\" >/dev/null; then healthy=1; break; fi; sleep 1; done; if [ \"\$healthy\" -ne 1 ]; then sudo systemctl status mimir --no-pager || true; exit 1; fi"

echo "==> Recording accepted deployment commit..."
ssh "$REMOTE" "set -eu; marker='$REMOTE_DIR/.deployed-commit'; tmp='$REMOTE_DIR/.deployed-commit.tmp.\$\$'; trap 'rm -f \"\$tmp\"' EXIT; printf '%s\\n' '$DEPLOY_COMMIT' > \"\$tmp\"; chmod 644 \"\$tmp\"; mv -f \"\$tmp\" \"\$marker\"; trap - EXIT"

trap - ERR

echo ""
echo "Deploy complete!"
echo "Accepted commit: $DEPLOY_COMMIT"
echo "Health check: ssh $REMOTE curl http://127.0.0.1:3031/health"
echo "Rollback: check out $ROLLBACK_TARGET in a clean worktree and run MIMIR_DEPLOY_USER=$DEPLOY_USER ./scripts/deploy-nas.sh $NAS_HOST"
