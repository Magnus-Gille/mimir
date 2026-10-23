#!/bin/bash
# shellcheck disable=SC2034  # variables are consumed by the sourcing sync script
# Shared helpers for sync-artifacts.sh and sync-artifacts-daemon.sh.
# Sourced, never executed. The caller owns `set -euo pipefail`.

# --- Node resolution -------------------------------------------------------
#
# launchd (and other schedulers) start with a minimal PATH that lacks the
# Homebrew prefixes, so a bare `node` is not found. Resolution order:
#   1. MIMIR_NODE_BIN, which must be an absolute path to an executable file.
#      If it is set but unusable the lookup fails; it never falls through.
#   2. `command -v node` with PATH augmented by MIMIR_NODE_SEARCH_PATH
#      (default /opt/homebrew/bin:/usr/local/bin), appended so an existing
#      PATH entry wins.
#   3. Otherwise fail. The secret scan is never skipped.
SYNC_NODE_SEARCH_PATH="${MIMIR_NODE_SEARCH_PATH:-/opt/homebrew/bin:/usr/local/bin}"
NODE_BIN=""
NODE_DIAGNOSTIC=""

resolve_node_bin() {
  if [ -n "${MIMIR_NODE_BIN:-}" ]; then
    case "$MIMIR_NODE_BIN" in
      /*) ;;
      *) NODE_DIAGNOSTIC="MIMIR_NODE_BIN is not an absolute path"; return 1 ;;
    esac
    if [ -f "$MIMIR_NODE_BIN" ] && [ -x "$MIMIR_NODE_BIN" ]; then
      NODE_BIN="$MIMIR_NODE_BIN"
      return 0
    fi
    NODE_DIAGNOSTIC="MIMIR_NODE_BIN is not an executable file"
    return 1
  fi
  local found
  if found=$(PATH="${PATH:-}:$SYNC_NODE_SEARCH_PATH" command -v node 2>/dev/null) && [ -n "$found" ]; then
    case "$found" in
      /*) NODE_BIN="$found"; return 0 ;;
    esac
  fi
  NODE_DIAGNOSTIC="node was not found on PATH or in $SYNC_NODE_SEARCH_PATH"
  return 1
}

# Resolve both halves of the scanner. Sets SCAN_NODE and SCAN_CLI, or returns
# nonzero with SCANNER_DIAGNOSTIC set. Diagnostics name configuration, never
# archive content.
SCANNER_DIAGNOSTIC=""
SCAN_NODE=""
SCAN_CLI=""
resolve_scanner() {
  local script_dir="$1"
  SCAN_CLI="${MIMIR_SECRET_SCAN_CLI:-$script_dir/../dist/cli/secret-scan.js}"
  if ! resolve_node_bin; then
    SCANNER_DIAGNOSTIC="$NODE_DIAGNOSTIC"
    return 1
  fi
  if [ ! -f "$SCAN_CLI" ]; then
    SCANNER_DIAGNOSTIC="scanner build output is missing at $SCAN_CLI (run npm run build)"
    return 1
  fi
  SCAN_NODE="$NODE_BIN"
}

# Exit code for "the scanner cannot run" (sysexits EX_UNAVAILABLE), distinct
# from the generic 1 used for every other refusal to mirror.
SCANNER_UNAVAILABLE_RC=69

# --- Mirror filters --------------------------------------------------------
#
# One filter list shared by the dry-run delete gate, the real rsync and the
# remote population count, in both sync scripts. Pure caches only: never the
# version-control directory, worktrees, dist/ or build/.
#
# rsync does not delete excluded names on the receiver (no --delete-excluded),
# so a cache directory already mirrored to the NAS is neither transferred nor
# deleted, and is invisible to the delete gate. The remote population count
# prunes the same names so the percentage denominator matches.
#
# Defaults cannot be removed. MIMIR_SYNC_EXTRA_EXCLUDES adds rsync exclude
# patterns (whitespace separated, so a pattern cannot contain whitespace).
SYNC_DEFAULT_EXCLUDES=(
  'node_modules/'
  '__pycache__/'
  '.venv/'
  'venv/'
  '.next/'
  '.pytest_cache/'
  '.mypy_cache/'
  '.DS_Store'
)

MIRROR_EXCLUDES=()
MIRROR_FILTERS=()

build_mirror_filters() {
  local pattern
  MIRROR_EXCLUDES=("${SYNC_DEFAULT_EXCLUDES[@]}")
  for pattern in ${MIMIR_SYNC_EXTRA_EXCLUDES:-}; do
    case "$pattern" in
      -*|+*|'!'*|*[[:cntrl:]]*)
        echo "ERROR: MIMIR_SYNC_EXTRA_EXCLUDES entries must be plain patterns (no leading -, + or !)." >&2
        return 1
        ;;
    esac
    MIRROR_EXCLUDES+=("$pattern")
  done
  # NAS-owned snapshot output is neither replaced nor deleted by laptop sync.
  MIRROR_FILTERS=(--filter='P /artifact-snapshots/***' --exclude='/artifact-snapshots')
  for pattern in "${MIRROR_EXCLUDES[@]}"; do
    MIRROR_FILTERS+=("--exclude=$pattern")
  done
}

quote_remote_sh() {
  # SSH hands this string to a remote shell. Quote configuration values so an
  # owner-overlay value cannot change the command structure.
  printf "'"
  printf '%s' "$1" | sed "s/'/'\\\\''/g"
  printf "'"
}

# Remote shell command that prints one byte per remote entry that the mirror
# could delete: everything under the root except NAS-owned snapshots and the
# excluded names. find's globbing approximates rsync's for these simple
# patterns; a mismatch can only count extra entries (a laxer percentage).
build_remote_population_command() {
  local root="$1" pattern dir_only predicate quoted
  local expr="find '$root' -mindepth 1 -path '$root/artifact-snapshots' -prune -o \\("
  local first=1
  for pattern in "${MIRROR_EXCLUDES[@]}"; do
    dir_only=0
    case "$pattern" in */) dir_only=1; pattern="${pattern%/}" ;; esac
    case "$pattern" in
      /*) quoted=$(quote_remote_sh "$root$pattern"); predicate="-path $quoted" ;;
      */*) quoted=$(quote_remote_sh "*/$pattern"); predicate="-path $quoted" ;;
      *) quoted=$(quote_remote_sh "$pattern"); predicate="-name $quoted" ;;
    esac
    [ "$dir_only" -eq 0 ] || predicate="\\( $predicate -type d \\)"
    if [ "$first" -eq 1 ]; then first=0; else expr="$expr -o"; fi
    expr="$expr $predicate"
  done
  printf '%s \\) -prune -o -printf .' "$expr"
}

# --- Stalled-sync visibility -----------------------------------------------
#
# Content-blind by construction: the panel message is a fixed class name, never
# a path, file name or scanner output. The token is piped to curl on stdin so it
# never appears in argv, and is never logged.
heimdall_sync_panel() {
  local state="$1" message="$2" token="${HEIMDALL_FLEET_TOKEN:-}"
  [ -n "${HEIMDALL_HUB_URL:-}" ] && [ -n "$token" ] || return 0
  command -v curl >/dev/null 2>&1 || return 0
  case "$token" in
    *$'\r'*|*$'\n'*)
      echo "WARN: refusing Heimdall panel push because the fleet token contains CR/LF" >&2
      return 0
      ;;
  esac
  printf 'Authorization: Bearer %s\n' "$token" |
    curl -fsS --max-time 5 -X POST "$HEIMDALL_HUB_URL" \
      -H @- -H "Content-Type: application/json" \
      -d "{\"service\":\"mimir\",\"panel\":\"sync\",\"kind\":\"status\",\"label\":\"Laptop sync\",\"state\":\"${state}\",\"message\":\"${message}\"}" \
      >/dev/null 2>&1 || true
}

# Refusal class for the EXIT trap. Each stage sets it before it can fail; any
# nonzero exit without a completed mirror is reported with that class.
REFUSAL_CLASS="unexpected"
MIRROR_COMPLETED=0

sync_exit_trap() {
  local rc=$?
  trap - EXIT
  if [ "$rc" -ne 0 ] && [ "$MIRROR_COMPLETED" -ne 1 ]; then
    echo "Sync refused to mirror: class=$REFUSAL_CLASS exit=$rc" >&2
    heimdall_sync_panel fail "class=${REFUSAL_CLASS} exit=${rc}"
    if declare -F on_sync_refusal >/dev/null 2>&1; then
      on_sync_refusal "$REFUSAL_CLASS" || true
    fi
  fi
  exit "$rc"
}
