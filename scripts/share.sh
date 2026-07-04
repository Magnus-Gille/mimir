#!/bin/bash
set -euo pipefail

# Generate a temporary share URL for a file in ~/mimir/.
#
# Usage: ./scripts/share.sh <file-path> [ttl]
#
# Examples:
#   ./scripts/share.sh ~/mimir/presentations/deck.pdf
#   ./scripts/share.sh ~/mimir/presentations/deck.pdf 7d
#   ./scripts/share.sh presentations/deck.pdf 1h
#
# TTL formats: 1h, 6h, 12h, 24h (default), 3d, 7d

NAS="${MIMIR_NAS:-magnus@100.99.119.52}"
LOCAL_ROOT="$HOME/mimir"
REMOTE_DIR="/home/magnus/mimir-server"
TTL="${2:-24h}"

# --- Resolve file path ---

FILE_ARG="${1:?Usage: share.sh <file-path> [ttl]}"

# Support both absolute ~/mimir/... paths and relative paths
if [[ "$FILE_ARG" == "$LOCAL_ROOT"/* ]]; then
  REL_PATH="${FILE_ARG#$LOCAL_ROOT/}"
elif [[ "$FILE_ARG" == "$HOME/mimir/"* ]]; then
  REL_PATH="${FILE_ARG#$HOME/mimir/}"
else
  REL_PATH="$FILE_ARG"
fi

FULL_LOCAL="$LOCAL_ROOT/$REL_PATH"

if [[ ! -f "$FULL_LOCAL" ]]; then
  echo "Error: File not found: $FULL_LOCAL" >&2
  exit 1
fi

# --- Sync the file to Pi ---

# macOS openrsync ignores the /./ --relative marker and replicates the full
# local path on the remote, so sync to the explicit destination path instead.
echo "==> Syncing $REL_PATH to NAS..." >&2
REL_DIR=$(dirname "$REL_PATH")
[[ "$REL_DIR" != "." ]] && ssh "${NAS%%:*}" "mkdir -p '/home/magnus/mimir/$REL_DIR'"
rsync -az "$FULL_LOCAL" "$NAS:/home/magnus/mimir/$REL_PATH"

# --- Generate share URL on Pi ---

echo "==> Generating share link (TTL: $TTL)..." >&2
URL=$(ssh "${NAS%%:*}" "cd $REMOTE_DIR && set -a && source .env && set +a && node dist/cli/share.js '$REL_PATH' '$TTL'")

# --- Output and clipboard ---

echo "$URL"
if command -v pbcopy &>/dev/null; then
  echo -n "$URL" | pbcopy
  echo "==> Copied to clipboard!" >&2
fi
