#!/bin/bash
set -euo pipefail

# Manual sync — same logic as daemon but with verbose output
#
# Usage: ./scripts/sync-artifacts.sh [nas-host]

NAS="${1:-${MIMIR_NAS:-magnus@${MIMIR_NAS_HOST:-nas}}}"
LOCAL="$HOME/mimir/"
REMOTE="$NAS:/home/magnus/mimir/"
INBOX="$NAS:/home/magnus/mimir-inbox/"
REMOTE_ROOT="/home/magnus/mimir"
MAX_DELETE="${MIMIR_SYNC_MAX_DELETE:-1000}"
MAX_DELETE_PCT="${MIMIR_SYNC_MAX_DELETE_PCT:-20}"

case "$MAX_DELETE" in ''|*[!0-9]*) echo "ERROR: MIMIR_SYNC_MAX_DELETE must be a positive integer." >&2; exit 1;; esac
[ "$MAX_DELETE" -ge 1 ] || { echo "ERROR: MIMIR_SYNC_MAX_DELETE must be at least 1." >&2; exit 1; }
case "$MAX_DELETE_PCT" in ''|*[!0-9]*) echo "ERROR: MIMIR_SYNC_MAX_DELETE_PCT must be an integer from 0 to 100." >&2; exit 1;; esac
[ "$MAX_DELETE_PCT" -le 100 ] || { echo "ERROR: MIMIR_SYNC_MAX_DELETE_PCT must be at most 100." >&2; exit 1; }

# Step 1: Import new files from inbox
echo "==> Importing from inbox..."
if ! IMPORTED_RAW=$(rsync -a --ignore-existing --remove-source-files --out-format='%n' "$INBOX" "$LOCAL"); then
  echo "ERROR: inbox import failed; refusing to scan or mirror partial results." >&2
  exit 1
fi
IMPORTED=$(printf '%s\n' "$IMPORTED_RAW" | sed '/\/$/d')

# Step 1.5: Secret-scan newly imported files before they can reach the NAS's
# Bearer-servable tree. Hits are quarantined out of $LOCAL and alerted —
# see src/secret-scan.ts and mimir#13.
if [ -n "$IMPORTED" ]; then
  echo "==> Scanning imported files for secrets..."
  if ! printf '%s\n' "$IMPORTED" | node "$(dirname "$0")/../dist/cli/secret-scan.js" --stdin "$LOCAL"; then
    echo "ERROR: secret scan failed; refusing to mirror unverified imports." >&2
    exit 1
  fi
else
  echo "==> No new files imported — skipping secret scan."
fi

# Step 2: Mirror laptop → NAS (laptop is source of truth)
# Safety: abort if --delete would remove >20% of remote files
echo "==> Pushing laptop → NAS (with --delete)..."
DRY_OUTPUT=$(rsync -an --delete "$LOCAL" "$REMOTE" 2>/dev/null)
DELETES=$(printf '%s\n' "$DRY_OUTPUT" | awk '/^deleting / { n++ } END { print n + 0 }')
if [ "$DELETES" -gt 0 ]; then
  # Count the actual remote population, not rsync's changed-line output. A
  # delete mixed with many additions must not dilute the safety percentage.
  REMOTE_POPULATION=$(ssh -o ConnectTimeout=5 -o BatchMode=yes "$NAS" "find '$REMOTE_ROOT' -mindepth 1 -printf .")
  REMOTE_TOTAL=${#REMOTE_POPULATION}
  if [ "$REMOTE_TOTAL" -gt 0 ]; then
    PCT=$(( DELETES * 100 / REMOTE_TOTAL ))
  else
    PCT=100
  fi
  if [ "$DELETES" -ge "$MAX_DELETE" ] || [ "$PCT" -gt "$MAX_DELETE_PCT" ]; then
    echo "ERROR: --delete would remove $DELETES/$REMOTE_TOTAL remote entries ($PCT%; max $MAX_DELETE entries or $MAX_DELETE_PCT%). Aborting." >&2
    echo "Run with caution: rsync -av --delete $LOCAL $REMOTE" >&2
    exit 1
  fi
fi
rsync -av --delete --max-delete="$MAX_DELETE" "$LOCAL" "$REMOTE"

echo "==> Sync complete."
