#!/bin/bash
set -euo pipefail
export LC_ALL=C

# Mímir offline copy — encrypted copy of the no-cloud folders to a local offline
# medium (for example an external SSD), with verification and a receipt.
#
# Data-classification policy v1.1, decision D: a folder marked `no-cloud: true`
# in <archive root>/classification.yaml is excluded from the cloud backup
# (offsite-backup.sh) and must instead be kept on an offline medium that is
# encrypted at rest, verified, receipted and then unmounted. This script is that
# path. It is EXPLICITLY invoked by the owner with the medium attached; it is
# never run by a timer or any other automation.
#
# What it does, in order (every step fails closed; nothing is written before the
# preflight has passed):
#   1. Load the markers through the shared parser in no-cloud-filter.sh. A
#      missing or invalid classification file fails: an explicit run needs
#      markers. Zero markers is a no-op that never touches the medium.
#   2. Preflight: the medium is mounted, and the rclone remote is a crypt remote
#      with filename encryption whose `remote =` path is a directory on that
#      medium. A `name:` style remote, or any path outside the mount, is
#      rejected so the copy can never land in the cloud.
#   3. Count source files per marker. A marker that matches nothing usually means
#      a spelling mismatch (rclone filters are case and normalization
#      sensitive), so it fails unless --allow-unmatched is given.
#   4. `rclone sync` the marked folders into <remote>:current. Replaced and
#      deleted files move to <remote>:history/<UTC id>; nothing is ever pruned.
#   5. `rclone cryptcheck --one-way` verifies the encrypted copy.
#   6. Write a receipt to <state>/offline-private/receipts/ (mode 600) and an
#      encrypted copy of it to <remote>:receipts/.
#   7. Unmount the medium unless --keep-mounted.
#
# Usage:
#   ./offline-copy-no-cloud.sh [--keep-mounted] [--allow-unmatched]
#
# Environment:
#   MIMIR_OFFLINE_MOUNT           (required) absolute path of the mounted medium
#   MIMIR_OFFLINE_REMOTE          rclone crypt remote NAME (default mimir-offline-crypt)
#   MIMIR_OFFLINE_ROOT            archive root (default $HOME/mimir)
#   MIMIR_OFFLINE_CLASSIFICATION  default $MIMIR_OFFLINE_ROOT/classification.yaml
#   MIMIR_OFFLINE_STATE_DIR       default ${XDG_STATE_HOME:-$HOME/.local/state}/mimir
#   RCLONE_BIN                    default rclone
#
# Output: content-blind event lines on stderr, `<UTC> stage=<s> state=<s>
# exit=<n>[ detail]`. Folder names, the mount path, the remote name and all rclone
# output are discarded; details are counts or fixed reason tokens. An event's
# exit is the failing command's status; the script itself exits 1 on failure,
# 3 when only the final unmount failed (copy and receipt are already complete),
# and 64 on a usage error.

KEEP_MOUNTED=0
ALLOW_UNMATCHED=0
for arg in "$@"; do
  case "$arg" in
    --keep-mounted) KEEP_MOUNTED=1 ;;
    --allow-unmatched) ALLOW_UNMATCHED=1 ;;
    *)
      printf '%s\n' 'usage: offline-copy-no-cloud.sh [--keep-mounted] [--allow-unmatched]' >&2
      exit 64
      ;;
  esac
done

MOUNT="${MIMIR_OFFLINE_MOUNT:-}"
REMOTE="${MIMIR_OFFLINE_REMOTE-mimir-offline-crypt}"
ROOT="${MIMIR_OFFLINE_ROOT:-$HOME/mimir}"
CLASSIFICATION="${MIMIR_OFFLINE_CLASSIFICATION:-$ROOT/classification.yaml}"
STATE_DIR="${MIMIR_OFFLINE_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/mimir}"
RCLONE="${RCLONE_BIN:-rclone}"
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" 2>/dev/null && pwd) || SCRIPT_DIR=""

umask 077

ts() { date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || printf unknown; }

event() {
  local stage="$1" state="$2" exit_code="$3" detail="${4:-}"
  printf '%s stage=%s state=%s exit=%s%s\n' "$(ts)" "$stage" "$state" "$exit_code" "${detail:+ ${detail}}" >&2 || true
}

fail_event() {
  event "$1" fail "${2:-1}" "${3:-}"
  exit 1
}

stat_value() {
  local format="$1" path="$2" value
  if value=$(stat -c "$format" -- "$path" 2>/dev/null); then
    printf '%s' "$value"
    return 0
  fi
  if value=$(stat -f "$format" "$path" 2>/dev/null); then
    printf '%s' "$value"
    return 0
  fi
  return 1
}

# Print PATH without trailing slashes; fail unless it is absolute, not "/", and
# free of empty, "." and ".." segments (a lexical prefix check is then sound).
normalize_absolute() {
  local p="$1"
  [[ "$p" = /* ]] || return 1
  while [ "${#p}" -gt 1 ] && [ "${p%/}" != "$p" ]; do p="${p%/}"; done
  [ "$p" != / ] || return 1
  case "$p/" in
    *//*|*/./*|*/../*) return 1 ;;
  esac
  printf '%s' "$p"
}

# ---- Configuration sanity (no filesystem or medium access yet) ----
[ -n "$MOUNT" ] || fail_event preflight 1 reason=mount_unset
case "$REMOTE" in
  ''|*:*|*/*) fail_event preflight 1 reason=remote_name ;;
esac
[ -n "$SCRIPT_DIR" ] || fail_event preflight 1
[ -d "$ROOT" ] || fail_event preflight 1 reason=root

# ---- Classification ----
# shellcheck source-path=SCRIPTDIR source=no-cloud-filter.sh
if ! . "$SCRIPT_DIR/no-cloud-filter.sh" 2>/dev/null; then
  fail_event classification 1 reason=library
fi
if ! no_cloud_load "$CLASSIFICATION"; then
  fail_event classification 1 "$(no_cloud_failure_detail)"
fi
[ "$NO_CLOUD_SOURCE" = file ] || fail_event classification 1 reason=absent
if ! no_cloud_patterns; then
  fail_event classification 1 "reason=${NO_CLOUD_ERROR:-unicode}"
fi
MARKERS=${#NO_CLOUD_PATHS[@]}
if [ "$MARKERS" -eq 0 ]; then
  # Nothing to keep offline: leave the medium, receipts and mounts untouched.
  event complete pass 0 'no_cloud_markers=0'
  exit 0
fi

# ---- Preflight (read-only until the copy starts) ----
command -v "$RCLONE" >/dev/null 2>&1 || fail_event preflight 1 reason=rclone

MOUNT_NORM=$(normalize_absolute "$MOUNT") || fail_event medium 1 reason=mount_invalid
[ -d "$MOUNT_NORM" ] || fail_event medium 1 reason=mount_invalid
MOUNT_REAL=$(cd -- "$MOUNT_NORM" 2>/dev/null && pwd -P) || fail_event medium 1 reason=mount_invalid

if command -v mountpoint >/dev/null 2>&1; then
  mountpoint -q "$MOUNT_NORM" >/dev/null 2>&1 || fail_event medium 1 reason=not_mounted
else
  mount_dev=$(stat_value %d "$MOUNT_NORM") || fail_event medium 1 reason=not_mounted
  parent_dev=$(stat_value %d "$MOUNT_NORM/..") || fail_event medium 1 reason=not_mounted
  [ "$mount_dev" != "$parent_dev" ] || fail_event medium 1 reason=not_mounted
fi

CONF_SHOW=$("$RCLONE" config show "$REMOTE" 2>/dev/null) || CONF_SHOW=""
RTYPE=$(printf '%s\n' "$CONF_SHOW" | awk -F' = ' '/^type =/{print $2; exit}')
FENC=$(printf '%s\n' "$CONF_SHOW" | awk -F' = ' '/^filename_encryption =/{print $2; exit}')
RPATH=$(printf '%s\n' "$CONF_SHOW" | awk 'index($0, "remote = ") == 1 {print substr($0, 10); exit}')
[ "$RTYPE" = crypt ] || fail_event preflight 1 reason=not_crypt
[ "$FENC" != off ] || fail_event preflight 1 reason=filename_encryption_off
[ -n "$RPATH" ] || fail_event preflight 1 reason=remote_path

# The crypt remote must wrap a directory on the medium. A `name:` remote, a
# relative path, or a path elsewhere would send the data somewhere else.
RPATH_NORM=$(normalize_absolute "$RPATH") || fail_event medium 1 reason=remote_outside_medium
if [ "$RPATH_NORM" != "$MOUNT_NORM" ] && [[ "$RPATH_NORM" != "$MOUNT_NORM"/* ]]; then
  fail_event medium 1 reason=remote_outside_medium
fi
# Resolve symlinks through the deepest existing ancestor: the vault directory may
# not exist yet, but an existing link must not lead off the medium.
probe="$RPATH_NORM"
while [ ! -d "$probe" ]; do
  probe="${probe%/*}"
  [ -n "$probe" ] || fail_event medium 1 reason=remote_outside_medium
done
probe_real=$(cd -- "$probe" 2>/dev/null && pwd -P) || fail_event medium 1 reason=remote_outside_medium
if [ "$probe_real" != "$MOUNT_REAL" ] && [[ "$probe_real" != "$MOUNT_REAL"/* ]]; then
  fail_event medium 1 reason=remote_outside_medium
fi

# Private receipt directories, created before copying so a state problem cannot
# leave a finished copy without its receipt.
PRIVATE_DIR="$STATE_DIR/offline-private"
RECEIPT_DIR="$PRIVATE_DIR/receipts"
ensure_private_dir() {
  local dir="$1"
  if [ -L "$dir" ]; then
    return 1
  elif [ -e "$dir" ]; then
    [ -d "$dir" ] || return 1
    [ "$(stat_value %u "$dir" 2>/dev/null || printf x)" = "$EUID" ] || return 1
  else
    mkdir -p -- "$(dirname -- "$dir")" 2>/dev/null && mkdir -- "$dir" 2>/dev/null || return 1
  fi
  chmod 700 "$dir" 2>/dev/null || return 1
  [ "$(stat_value %u "$dir" 2>/dev/null || printf x)" = "$EUID" ]
}
ensure_private_dir "$PRIVATE_DIR" || fail_event preflight 1 reason=state_dir
ensure_private_dir "$RECEIPT_DIR" || fail_event preflight 1 reason=state_dir

# ---- Filters: only the marked folders, never git internals ----
FILTERS=(--filter '- **/.git/**')
for pattern in "${NO_CLOUD_PATTERNS[@]}"; do
  FILTERS+=(--filter "+ $pattern")
done
FILTERS+=(--filter '- **')

count_files() {
  local count
  count=$("$RCLONE" lsf -R --files-only "$ROOT" "$@" 2>/dev/null | awk 'END { print NR }') || return 1
  case "$count" in ''|*[!0-9]*) return 1 ;; esac
  printf '%s' "$count"
}

# ---- Per-marker presence ----
# macOS matches names case and normalization insensitively; rclone filters do
# not, so a marker with no files is most likely a spelling mismatch.
UNMATCHED=0
for ((i = 0; i < ${#NO_CLOUD_PATTERNS[@]}; i += 2)); do
  marker_count=$(count_files \
    --filter '- **/.git/**' \
    --filter "+ ${NO_CLOUD_PATTERNS[i]}" \
    --filter "+ ${NO_CLOUD_PATTERNS[i + 1]}" \
    --filter '- **') || fail_event classification 1 reason=list
  [ "$marker_count" -gt 0 ] || UNMATCHED=$((UNMATCHED + 1))
done
FILES=$(count_files "${FILTERS[@]}") || fail_event classification 1 reason=list
if [ "$UNMATCHED" -gt 0 ] && [ "$ALLOW_UNMATCHED" != 1 ]; then
  fail_event classification 1 "reason=unmatched no_cloud_unmatched=${UNMATCHED}"
fi
event classification pass 0 "no_cloud_markers=${MARKERS} no_cloud_unmatched=${UNMATCHED} files=${FILES}"

# ---- Copy ----
utc_id() { date -u +%Y%m%dT%H%M%SZ; }
HISTORY_ID=$(utc_id) || fail_event preflight 1
attempt=0
while [ -e "$RECEIPT_DIR/offline-copy-${HISTORY_ID}.receipt" ]; do
  # A second run inside the same second would share the history directory.
  attempt=$((attempt + 1))
  [ "$attempt" -le 5 ] || fail_event preflight 1 reason=receipt_exists
  sleep 1
  HISTORY_ID=$(utc_id) || fail_event preflight 1
done

rc=0
"$RCLONE" sync "$ROOT/" "$REMOTE:current" \
  --backup-dir "$REMOTE:history/$HISTORY_ID" \
  "${FILTERS[@]}" >/dev/null 2>&1 || rc=$?
[ "$rc" -eq 0 ] || fail_event copy "$rc"
event copy pass 0

# ---- Verify ----
rc=0
"$RCLONE" cryptcheck "$ROOT/" "$REMOTE:current" --one-way \
  "${FILTERS[@]}" >/dev/null 2>&1 || rc=$?
[ "$rc" -eq 0 ] || fail_event verify "$rc"
event verify pass 0

# ---- Receipt ----
RECEIPT_NAME="offline-copy-${HISTORY_ID}.receipt"
RECEIPT="$RECEIPT_DIR/$RECEIPT_NAME"
receipt_failed() { [ -z "${receipt_tmp:-}" ] || rm -f -- "$receipt_tmp"; fail_event receipt 1; }
receipt_tmp=$(mktemp "$RECEIPT_DIR/.receipt.XXXXXX" 2>/dev/null) || receipt_failed
{
  printf 'receipt=mimir-offline-copy\n'
  printf 'version=1\n'
  printf 'completed_at=%s\n' "$(ts)"
  printf 'no_cloud_markers=%s\n' "$MARKERS"
  printf 'no_cloud_unmatched=%s\n' "$UNMATCHED"
  printf 'files=%s\n' "$FILES"
  printf 'copy=pass\n'
  printf 'verify=cryptcheck-one-way-pass\n'
  printf 'history=%s\n' "$HISTORY_ID"
} > "$receipt_tmp" 2>/dev/null || receipt_failed
chmod 600 "$receipt_tmp" 2>/dev/null || receipt_failed
mv -f -- "$receipt_tmp" "$RECEIPT" 2>/dev/null || receipt_failed
receipt_tmp=""
"$RCLONE" copyto "$RECEIPT" "$REMOTE:receipts/$RECEIPT_NAME" >/dev/null 2>&1 || fail_event receipt 1
event receipt pass 0

# ---- Unmount ----
if [ "$KEEP_MOUNTED" = 1 ]; then
  event complete pass 0 "files=${FILES} no_cloud_markers=${MARKERS} kept_mounted=1"
  exit 0
fi
unmount_ok=0
if [ "$(uname -s 2>/dev/null || printf unknown)" = Darwin ]; then
  diskutil unmount "$MOUNT_NORM" >/dev/null 2>&1 && unmount_ok=1
else
  umount "$MOUNT_NORM" >/dev/null 2>&1 && unmount_ok=1
fi
if [ "$unmount_ok" != 1 ]; then
  # The copy and receipt are complete; only the detach needs the owner.
  event unmount warning 1
  exit 3
fi
event complete pass 0 "files=${FILES} no_cloud_markers=${MARKERS}"
