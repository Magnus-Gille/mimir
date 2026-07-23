#!/bin/bash
set -euo pipefail

# Read-only Mimir node/substrate verification hook (ADR-007). It never changes
# service, storage, tunnel, sync, backup, or Heimdall state. Evidence locations
# are supplied by the private owner overlay and are deliberately not defaults.

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

TIMEOUT_SECONDS="${MIMIR_RELOCATION_TIMEOUT_SECONDS:-60}"
case "$TIMEOUT_SECONDS" in ''|*[!0-9]*) echo "ERROR: timeout must be a positive integer" >&2; exit 2;; esac
[ "$TIMEOUT_SECONDS" -gt 0 ] || { echo "ERROR: timeout must be positive" >&2; exit 2; }

require_evidence() {
  local variable="$1" expected="$2"
  local path="${!variable:-}"
  [ -n "$path" ] || { echo "BLOCKED: private overlay did not provide $variable" >&2; return 1; }
  [ -r "$path" ] || { echo "BLOCKED: $variable is not readable" >&2; return 1; }
  grep -Fxq "$expected" "$path" || { echo "BLOCKED: $variable does not contain current $expected evidence" >&2; return 1; }
}

check() {
  local label="$1"; shift
  if timeout "$TIMEOUT_SECONDS" "$@" >/dev/null 2>&1; then
    echo "PASS: $label"
  else
    echo "BLOCKED: $label" >&2
    return 1
  fi
}

# These are status reads only. Names and locations are stable logical identities;
# live endpoints, tunnel identifiers, credentials, and evidence paths remain private.
check "service active" systemctl is-active --quiet mimir.service
check "offsite timer active" systemctl is-active --quiet mimir-offsite.timer
require_evidence MIMIR_RELOCATION_TUNNEL_EVIDENCE "tunnel-v1:connected"
require_evidence MIMIR_RELOCATION_SYNC_EVIDENCE "sync-v1:complete"
require_evidence MIMIR_RELOCATION_T7_EVIDENCE "local-copy-v1:complete"
require_evidence MIMIR_RELOCATION_OFFSITE_EVIDENCE "offsite-v1:complete"
require_evidence MIMIR_RELOCATION_HEIMDALL_EVIDENCE "heimdall-v1:fresh"
require_evidence MIMIR_RELOCATION_RESTORE_EVIDENCE "restore-v1:representative-ok"
require_evidence MIMIR_RELOCATION_DEPLOYMENT_EVIDENCE "deployment-marker-v1:recoverable"
echo "PASS: Mimir $HOOK verification evidence is complete"
