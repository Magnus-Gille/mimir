#!/bin/bash
# shellcheck disable=SC2034  # state shared with the sourced sync-common.sh
set -euo pipefail

# Manual sync — same logic as daemon but with verbose output
#
# Usage: ./scripts/sync-artifacts.sh [nas-host]

NAS_HOST="${1:-${MIMIR_NAS_HOST:-}}"
NAS="${MIMIR_NAS:-${NAS_HOST:+mimir@$NAS_HOST}}"
LOCAL_ROOT="${MIMIR_LOCAL_ROOT:-$HOME/mimir}"
REMOTE_ROOT="${MIMIR_REMOTE_ROOT:-/home/mimir/mimir}"
REMOTE_INBOX="${MIMIR_REMOTE_INBOX:-/home/mimir/mimir-inbox}"
LOCAL="$LOCAL_ROOT/"
REMOTE="$NAS:$REMOTE_ROOT/"
INBOX="$NAS:$REMOTE_INBOX/"
STATE_DIR="${MIMIR_SYNC_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/mimir}"
PENDING="$STATE_DIR/import-pending"
QUARANTINE="${MIMIR_QUARANTINE_DIR:-${LOCAL%/}-quarantine}"
MAX_DELETE="${MIMIR_SYNC_MAX_DELETE:-1000}"
MAX_DELETE_PCT="${MIMIR_SYNC_MAX_DELETE_PCT:-20}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=sync-common.sh
. "$SCRIPT_DIR/sync-common.sh"

# A refusal to mirror, configuration errors included, pushes a fail-state
# Heimdall panel (class name only) when HEIMDALL_HUB_URL/HEIMDALL_FLEET_TOKEN
# are set.
REFUSAL_CLASS=config_invalid
trap sync_exit_trap EXIT

[ -n "$NAS" ] || { echo "ERROR: pass a sync host, set MIMIR_NAS_HOST, or set MIMIR_NAS=user@host." >&2; exit 1; }
case "$MAX_DELETE" in ''|*[!0-9]*) echo "ERROR: MIMIR_SYNC_MAX_DELETE must be a positive integer." >&2; exit 1;; esac
[ "$MAX_DELETE" -ge 1 ] || { echo "ERROR: MIMIR_SYNC_MAX_DELETE must be at least 1." >&2; exit 1; }
case "$MAX_DELETE_PCT" in ''|*[!0-9]*) echo "ERROR: MIMIR_SYNC_MAX_DELETE_PCT must be an integer from 0 to 100." >&2; exit 1;; esac
[ "$MAX_DELETE_PCT" -le 100 ] || { echo "ERROR: MIMIR_SYNC_MAX_DELETE_PCT must be at most 100." >&2; exit 1; }
MAX_DELETE_PCT=$((10#$MAX_DELETE_PCT))
build_mirror_filters || exit 1
REFUSAL_CLASS=unexpected

# Keep imports outside the mirrored/servable tree until the scanner has
# accepted them. The pending tree itself is the durable recovery record: if
# rsync or the scanner fails, a later invocation must process these paths
# before it can mirror anything.
mkdir -p "$PENDING"
chmod 700 "$STATE_DIR" "$PENDING"

# Step 1: Import new files from inbox into durable staging
echo "==> Importing from inbox..."
REFUSAL_CLASS=import_failed
if ! rsync -a --ignore-existing --remove-source-files "$INBOX" "$PENDING/"; then
  echo "ERROR: inbox import failed; pending files remain staged at $PENDING; refusing to scan or mirror." >&2
  exit 1
fi

# Step 1.5: Secret-scan every pending file, including files left by an older
# failed invocation. Hits are quarantined outside both staging and $LOCAL.
echo "==> Scanning pending imports for secrets..."
if ! resolve_scanner "$SCRIPT_DIR"; then
  REFUSAL_CLASS=scanner_unavailable
  echo "ERROR: secret scanner UNAVAILABLE (class=scanner_unavailable): $SCANNER_DIAGNOSTIC; pending files remain staged at $PENDING; refusing to mirror." >&2
  exit "$SCANNER_UNAVAILABLE_RC"
fi
REFUSAL_CLASS=scan_failed
if ! "$SCAN_NODE" "$SCAN_CLI" "$PENDING" "$QUARANTINE"; then
  echo "ERROR: secret scan failed (class=scan_failed); pending files remain staged at $PENDING; refusing to mirror." >&2
  exit 1
fi

# Step 1.6: Promote only verified content. --ignore-existing prevents an
# inbox path from overwriting a local artifact; --remove-source-files makes
# successful promotion durable. Any collision or partial promotion remains in
# staging and blocks the mirror until an operator resolves it without data loss.
if [ -n "$(find "$PENDING" -mindepth 1 -print -quit)" ]; then
  echo "==> Promoting verified imports..."
  REFUSAL_CLASS=promotion_failed
  if ! rsync -a --ignore-existing --remove-source-files "$PENDING/" "$LOCAL"; then
    echo "ERROR: verified import promotion failed; pending files remain staged at $PENDING; refusing to mirror." >&2
    exit 1
  fi
  find "$PENDING" -mindepth 1 -depth -type d -empty -delete
  if [ -n "$(find "$PENDING" -mindepth 1 -print -quit)" ]; then
    REFUSAL_CLASS=promotion_blocked
    echo "ERROR: pending imports could not be promoted without overwriting local paths; resolve $PENDING before syncing." >&2
    exit 1
  fi
fi
rmdir "$PENDING"

# Step 2: Mirror laptop → NAS (laptop is source of truth)
# Safety: abort if --delete would remove >20% of remote files
echo "==> Pushing laptop → NAS (with --delete)..."
REFUSAL_CLASS=dry_run_failed
DRY_OUTPUT=$(rsync -ani --delete --out-format='%i' "${MIRROR_FILTERS[@]}" "$LOCAL" "$REMOTE")
DELETES=$(printf '%s\n' "$DRY_OUTPUT" | count_dry_run_deletes)
if [ "$DELETES" -gt 0 ]; then
  # Count the actual remote population, not rsync's changed-line output. A
  # delete mixed with many additions must not dilute the safety percentage.
  REFUSAL_CLASS=delete_gate
  REMOTE_POPULATION=$(ssh -o ConnectTimeout=5 -o BatchMode=yes "$NAS" "$(build_remote_population_command "$REMOTE_ROOT")")
  REMOTE_TOTAL=${#REMOTE_POPULATION}
  if [ "$REMOTE_TOTAL" -gt 0 ]; then
    PCT=$(( (DELETES * 100 + REMOTE_TOTAL - 1) / REMOTE_TOTAL ))
  else
    PCT=100
  fi
  if [ "$DELETES" -ge "$MAX_DELETE" ] || [ "$(( DELETES * 100 ))" -gt "$(( MAX_DELETE_PCT * REMOTE_TOTAL ))" ]; then
    echo "ERROR: --delete would remove $DELETES/$REMOTE_TOTAL remote entries ($PCT%; max $MAX_DELETE entries or $MAX_DELETE_PCT%). Aborting." >&2
    echo "Run with caution: rsync -av --delete $LOCAL $REMOTE" >&2
    exit 1
  fi
fi
# NAS-owned snapshot output and the shared cache excludes are neither replaced
# nor deleted by laptop sync (see sync-common.sh).
REFUSAL_CLASS=push_failed
rsync -av --delete --max-delete="$MAX_DELETE" "${MIRROR_FILTERS[@]}" "$LOCAL" "$REMOTE"
MIRROR_COMPLETED=1
heimdall_sync_panel pass "class=ok"

echo "==> Sync complete."
