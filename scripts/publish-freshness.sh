#!/bin/bash
set -euo pipefail
umask 027

# Publish the fixed, metadata-only records that Heimdall's restricted storage
# probe may read. The installer owns the directory/group contract; this script
# deliberately never creates it, so a typo cannot widen access.

FRESHNESS_DIR="${MIMIR_FRESHNESS_DIR:-/var/lib/mimir/heimdall-freshness}"
SUBJECT="${1:-}"
RESULT="${2:-}"

case "$SUBJECT" in
  backup|sync) ;;
  *) echo "ERROR: subject must be backup or sync." >&2; exit 64 ;;
esac

case "$RESULT" in
  success) STATE="fresh" ;;
  error) STATE="error" ;;
  *) echo "ERROR: result must be success or error." >&2; exit 64 ;;
esac

[ -d "$FRESHNESS_DIR" ] || {
  echo "ERROR: freshness surface is not installed." >&2
  exit 78
}

target="$FRESHNESS_DIR/$SUBJECT.json"
temporary=$(mktemp "$FRESHNESS_DIR/.${SUBJECT}.XXXXXX")
trap 'rm -f "$temporary"' EXIT

# Do not add a message: it could accidentally disclose a private path, a file
# name, or artifact-derived content. The probe derives stale from observed_at.
printf '{"schema_version":1,"state":"%s","observed_at":"%s"}\n' \
  "$STATE" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$temporary"
chmod 0640 "$temporary"
mv -f "$temporary" "$target"
trap - EXIT
