#!/bin/bash
set -euo pipefail

# Backup artifacts from SD card to NAS disk on the same Pi
# Run via cron on the NAS Pi itself
# Usage: backup-artifacts.sh

SOURCE="/home/magnus/artifacts/"
DEST="/mnt/timemachine/backups/mimir/"

mkdir -p "$DEST"
rsync -a --delete "$SOURCE" "$DEST"
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) Backup complete" >> /home/magnus/mimir/backup.log
