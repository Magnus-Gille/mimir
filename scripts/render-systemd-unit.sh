#!/bin/bash
set -euo pipefail

DEPLOY_USER="${1:-}"
UNIT_FILE="${2:-}"

[[ "$DEPLOY_USER" =~ ^[a-z_][a-z0-9_-]*$ ]] || {
  echo "ERROR: deployment user must be a Linux account name." >&2
  exit 1
}
[ -f "$UNIT_FILE" ] || {
  echo "ERROR: systemd unit file does not exist: $UNIT_FILE" >&2
  exit 1
}

sed \
  -e "s|^User=.*$|User=$DEPLOY_USER|" \
  -e "s|/home/[^/]*/|/home/$DEPLOY_USER/|g" \
  "$UNIT_FILE"
