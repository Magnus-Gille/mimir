#!/bin/bash
set -euo pipefail

# Root-owned, reversible installation for Heimdall's least-authority backup
# and sync freshness records. It exposes only two fixed JSON metadata files.

DIR="${MIMIR_FRESHNESS_DIR:-/var/lib/mimir/heimdall-freshness}"
PROBE_GROUP="${MIMIR_FRESHNESS_PROBE_GROUP:-heimdall-storage-probe}"
PUBLISHER_USER="${MIMIR_FRESHNESS_PUBLISHER_USER:-mimir}"
REMOVE=false

usage() {
  echo "Usage: $0 [--remove]" >&2
}

case "${1:-}" in
  "") ;;
  --remove) REMOVE=true ;;
  -h|--help) usage; exit 0 ;;
  *) usage; exit 64 ;;
esac

[ "$(id -u)" -eq 0 ] || { echo "ERROR: run as root." >&2; exit 77; }

if "$REMOVE"; then
  # Remove only the two contract records. rmdir refuses to delete unexpected
  # content, making rollback non-destructive. The group is retained because it
  # may be deliberately shared with another least-authority probe.
  rm -f "$DIR/backup.json" "$DIR/sync.json"
  rmdir "$DIR"
  echo "Removed Heimdall freshness surface; retained group $PROBE_GROUP."
  exit 0
fi

id -u "$PUBLISHER_USER" >/dev/null 2>&1 || {
  echo "ERROR: publisher user does not exist." >&2
  exit 67
}
getent group "$PROBE_GROUP" >/dev/null 2>&1 || groupadd --system "$PROBE_GROUP"

# The setgid bit keeps publisher-created records in the probe group. The probe
# group has r-x on the directory and r-- on records, never write permission.
install -d -o "$PUBLISHER_USER" -g "$PROBE_GROUP" -m 2750 "$DIR"
for record in backup sync; do
  if [ -e "$DIR/$record.json" ]; then
    chown "$PUBLISHER_USER:$PROBE_GROUP" "$DIR/$record.json"
    chmod 0640 "$DIR/$record.json"
  fi
done

echo "Installed Heimdall freshness surface with probe group read-only access."
