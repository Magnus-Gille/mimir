#!/bin/bash
set -euo pipefail
umask 077

# Backup artifacts from SD card to NAS disk on the same Pi
# Run via cron on the NAS Pi itself
# Usage: backup-artifacts.sh
#
# Policy: HD is append-only retention — no --delete.
# Files removed from SD are preserved on HD indefinitely.

SOURCE="${MIMIR_BACKUP_SOURCE:-$HOME/mimir/}"
DEST="${MIMIR_BACKUP_DEST:-/mnt/backup/mimir/}"
BACKUP_MOUNT="${MIMIR_BACKUP_MOUNT:-/mnt/backup}"
STATE_DIR="${MIMIR_BACKUP_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/mimir}"
LOG="${MIMIR_BACKUP_LOG:-$STATE_DIR/backup.log}"
FRESHNESS_DIR="${MIMIR_FRESHNESS_DIR:-/var/lib/mimir/heimdall-freshness}"
FRESHNESS_PUBLISHER="${MIMIR_FRESHNESS_PUBLISHER:-$(dirname "$0")/publish-freshness.sh}"

publish_freshness() {
  # Deployment does not create privileged state. Until the explicit root
  # installer runs, retain historical backup behavior and expose no surface.
  [ -d "$FRESHNESS_DIR" ] || return 0
  MIMIR_FRESHNESS_DIR="$FRESHNESS_DIR" "$FRESHNESS_PUBLISHER" backup "$1"
}

# Runtime logs must survive code deployments and remain private.
mkdir -p "$(dirname "$LOG")"

# Verify HD is mounted before writing
if ! mountpoint -q "$BACKUP_MOUNT"; then
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) ERROR: $BACKUP_MOUNT not mounted — skipping backup" >> "$LOG"
  publish_freshness error || true
  exit 1
fi

mkdir -p "$DEST"
if ! rsync -a "$SOURCE" "$DEST"; then
  publish_freshness error || true
  exit 1
fi
publish_freshness success
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) Backup complete" >> "$LOG"
