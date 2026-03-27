#!/bin/bash
set -euo pipefail

# Backup artifacts from SD card to NAS disk on the same Pi
# Run via cron on the NAS Pi itself
# Usage: backup-artifacts.sh
#
# Policy: HD is append-only retention — no --delete.
# Files removed from SD are preserved on HD indefinitely.

SOURCE="/home/magnus/mimir/"
DEST="/mnt/timemachine/backups/mimir/"
LOG="/home/magnus/mimir-server/backup.log"

# Verify HD is mounted before writing
if ! mountpoint -q /mnt/timemachine; then
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) ERROR: /mnt/timemachine not mounted — skipping backup" >> "$LOG"
  exit 1
fi

mkdir -p "$DEST"
rsync -a "$SOURCE" "$DEST"
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) Backup complete" >> "$LOG"
