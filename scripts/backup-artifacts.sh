#!/bin/bash
set -euo pipefail

# Backup artifacts from SD card to NAS disk on the same Pi
# Run via cron on the NAS Pi itself
# Usage: backup-artifacts.sh
#
# Policy: HD is append-only retention — no --delete.
# Files removed from SD are preserved on HD indefinitely.

SOURCE="${MIMIR_BACKUP_SOURCE:-$HOME/mimir/}"
DEST="${MIMIR_BACKUP_DEST:-/mnt/backup/mimir/}"
BACKUP_MOUNT="${MIMIR_BACKUP_MOUNT:-/mnt/backup}"
LOG="${MIMIR_BACKUP_LOG:-$HOME/mimir-server/backup.log}"

# Verify HD is mounted before writing
if ! mountpoint -q "$BACKUP_MOUNT"; then
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) ERROR: $BACKUP_MOUNT not mounted — skipping backup" >> "$LOG"
  exit 1
fi

mkdir -p "$DEST"
rsync -a "$SOURCE" "$DEST"
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) Backup complete" >> "$LOG"
