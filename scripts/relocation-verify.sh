#!/bin/bash
set -euo pipefail

# Thin operator wrapper for the read-only Mimir relocation hooks (ADR-007).
# All verification logic lives in src/cli/relocation-verify.ts; this wrapper
# only refuses mutating hooks up front and locates the built CLI. It never
# changes service, storage, tunnel, sync, backup, or Heimdall state.

HOOK="${1:-}"
case "$HOOK" in
  preflight|verify) ;;
  drain|compensate|rollback)
    echo "ERROR: $HOOK is a mutating component-owned operation; this read-only hook refuses it." >&2
    echo "Use the documented, attempt-bound operator procedure and its compensation recipe." >&2
    exit 2
    ;;
  *)
    echo "Usage: $0 preflight|verify" >&2
    exit 2
    ;;
esac

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
CLI="$SCRIPT_DIR/../dist/cli/relocation-verify.js"
if [ ! -f "$CLI" ]; then
  echo "ERROR: $CLI not found; run 'npm run build' first." >&2
  exit 2
fi
exec node "$CLI" "$HOOK"
