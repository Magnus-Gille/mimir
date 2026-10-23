#!/bin/bash
set -euo pipefail
export LC_ALL=C

# Mímir offsite backup — encrypted push of the artifact archive to cloud storage
# through an rclone *crypt* remote. Runs on the NAS Pi via a systemd timer (daily).
#
# ┌─ REFERENCE IMPLEMENTATION of the Grimnir offsite-backup pattern (mimir#9) ─┐
# │ munin-memory#172 and brokkr#1 copy-adapt this script. The shared contract: │
# │   • Encrypted: rclone crypt — file CONTENTS and NAMES never leave in clear, │
# │     and the script fails closed if the remote is not a verified crypt.      │
# │   • Mirror `current/` + N-day history via --backup-dir → tagged run dirs.   │
# │   • Non-destructive: deletions are MOVED to a per-run archive dir; whole    │
# │     archive dirs older than N days are pruned by their NAME (not mtime).     │
# │   • Preflight delete-count gate + --max-delete: abort an implausible wipe.   │
# │   • Heartbeat stamp + Heimdall status panel so a silent failure is visible.  │
# │   • Fail-loud: every expected failure pushes a `fail` panel and exits ≠0.    │
# └────────────────────────────────────────────────────────────────────────────┘
#
# Runs on Linux (GNU date). Prereqs / setup / key custody: docs/offsite-backup.md.
#
# Usage:
#   ./offsite-backup.sh              run the backup
#   ./offsite-backup.sh --dry-run    show what would change, touch nothing

# ---- Config (override via environment / EnvironmentFile) ----
SERVICE="${MIMIR_OFFSITE_SERVICE:-mimir}"                 # Heimdall service id
PANEL="${MIMIR_OFFSITE_PANEL:-offsite}"                   # Heimdall panel id
SOURCE="${MIMIR_OFFSITE_ROOT:-$HOME/mimir}"              # directory to back up
REMOTE="${MIMIR_OFFSITE_REMOTE:-mimir-crypt}"             # rclone crypt remote NAME (no ':' / path)
RETENTION_DAYS="${MIMIR_OFFSITE_RETENTION_DAYS:-30}"      # archive prune horizon (days)
MAX_DELETE="${MIMIR_OFFSITE_MAX_DELETE:-1000}"            # abort if a run would remove ≥ this many files
MAX_DELETE_PCT="${MIMIR_OFFSITE_MAX_DELETE_PCT:-25}"      # ...or more than this % of current/
STATE_DIR="${MIMIR_OFFSITE_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/mimir}"
PRIVATE_STATE_DIR="$STATE_DIR/offsite-private"
STAMP="${MIMIR_OFFSITE_STAMP:-$PRIVATE_STATE_DIR/offsite.stamp}"
LOG="${MIMIR_OFFSITE_LOG:-$PRIVATE_STATE_DIR/offsite-backup.log}"
RCLONE="${RCLONE_BIN:-rclone}"
# no-cloud markers (classification policy v1.1, decision D). The file travels
# with the archive; when it is absent the run proceeds with zero exclusions
# unless MIMIR_OFFSITE_CLASSIFICATION_REQUIRED=1.
CLASSIFICATION="${MIMIR_OFFSITE_CLASSIFICATION:-$SOURCE/classification.yaml}"
CLASSIFICATION_REQUIRED="${MIMIR_OFFSITE_CLASSIFICATION_REQUIRED:-0}"
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" 2>/dev/null && pwd) || SCRIPT_DIR=""

# Git internals are transient workspace state, not Mimir artifacts. Deep Codex
# checkpoint refs can exceed provider-specific encrypted path limits.
RCLONE_FILTERS=(--exclude '**/.git/**')

DRY_RUN=""
if [ "${1:-}" = "--dry-run" ] || [ "${MIMIR_OFFSITE_DRYRUN:-}" = "1" ]; then
  DRY_RUN="--dry-run"
fi

# Panel identity is a fixed deployment label, never a path or free-form text.
# Sanitize it before any bootstrap failure can attempt a panel update.
case "$SERVICE" in ''|*[!A-Za-z0-9_.-]*) SERVICE=mimir;; esac
case "$PANEL" in ''|*[!A-Za-z0-9_.-]*) PANEL=offsite;; esac

umask 077

# Private state is established before the ERR trap or any logging is enabled.
# Bootstrap failures must never echo a configured path (or recurse through a
# logger whose destination is the thing that failed).
send_panel() {
  local state="$1" message="$2" token="${HEIMDALL_FLEET_TOKEN:-}"
  [ -n "${HEIMDALL_HUB_URL:-}" ] && [ -n "$token" ] || return 0
  case "$token" in
    *$'\r'*|*$'\n'*)
      printf '%s\n' 'WARN: refusing Heimdall panel push because the fleet token contains CR/LF' >&2
      return 0
      ;;
  esac
  printf 'Authorization: Bearer %s\n' "$token" |
    curl -fsS --max-time 5 -X POST "$HEIMDALL_HUB_URL" \
      -H @- -H "Content-Type: application/json" \
      -d "{\"service\":\"${SERVICE}\",\"panel\":\"${PANEL}\",\"kind\":\"status\",\"label\":\"Offsite backup\",\"state\":\"${state}\",\"message\":\"${message}\"}" \
      >/dev/null 2>&1 || true
}

bootstrap_panel() {
  send_panel fail 'stage=bootstrap state=fail exit=1'
}

bootstrap_fail() {
  trap - ERR
  bootstrap_panel
  printf '%s\n' 'ERROR: offsite backup private state unavailable' >&2
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

stat_mode() {
  local path="$1" value
  if value=$(stat -c '%a' -- "$path" 2>/dev/null); then
    printf '%s' "$value"
    return 0
  fi
  if value=$(stat -f '%Lp' "$path" 2>/dev/null); then
    printf '%s' "$value"
    return 0
  fi
  return 1
}

remove_darwin_acl() {
  [ "$(uname -s 2>/dev/null || printf unknown)" = "Darwin" ] || return 0
  chmod -N "$1" 2>/dev/null || return 1
}

tighten_dir() {
  chmod 700 "$1" 2>/dev/null || return 1
  remove_darwin_acl "$1"
}

tighten_file() {
  chmod 600 "$1" 2>/dev/null || return 1
  remove_darwin_acl "$1"
}

path_is_present() {
  [ -e "$1" ] || [ -L "$1" ]
}

# Create only missing components, with private permissions. Existing ancestors
# are never chmodded: they may be shared system or home directories. The
# dedicated offsite child is tightened in place; custom LOG/STAMP parents must
# already be private or be newly created.
ensure_private_parent() {
  local path="$1" dedicated="$2" current=/ component parent_path parent_uid parent_mode mode_num created_final=0
  local -a components

  [[ "$path" = /* && "$path" != / && "$path" != *'//'* ]] || bootstrap_fail
  IFS='/' read -r -a components <<< "${path#/}"
  for component in "${components[@]}"; do
    case "$component" in
      ''|.|..) bootstrap_fail ;;
    esac
    parent_path="$current"
    current="${current%/}/$component"
    if [ -L "$current" ]; then
      # macOS exposes /var through a system symlink. The directly used final
      # parent is still rejected when it is a symlink; trusted ancestors may
      # be traversed for portability.
      [ "$current" != "$path" ] || bootstrap_fail
    elif path_is_present "$current"; then
      [ -d "$current" ] || bootstrap_fail
    else
      parent_uid=$(stat_value '%u' "$parent_path" 2>/dev/null) || bootstrap_fail
      parent_mode=$(stat_mode "$parent_path" 2>/dev/null) || bootstrap_fail
      mode_num=$((0$parent_mode))
      # Do not create a private child below a group/world-writable parent.
      if (( (mode_num & 0022) != 0 )) && [ "$parent_uid" != "$EUID" ]; then
        bootstrap_fail
      fi
      mkdir "$current" 2>/dev/null || bootstrap_fail
      tighten_dir "$current" || bootstrap_fail
      created_final=1
    fi
  done

  parent_uid=$(stat_value '%u' "$path" 2>/dev/null) || bootstrap_fail
  [ "$parent_uid" = "$EUID" ] || bootstrap_fail
  if [ "$dedicated" = 1 ] || [ "$created_final" = 1 ]; then
    tighten_dir "$path" || bootstrap_fail
  else
    parent_mode=$(stat_mode "$path" 2>/dev/null) || bootstrap_fail
    [ "$parent_mode" = 700 ] || bootstrap_fail
    # macOS access ACLs can grant other users access despite mode 0700.
    # This directly configured, owned private parent is safe to tighten;
    # existing shared ancestors remain untouched.
    tighten_dir "$path" || bootstrap_fail
  fi
}

prepare_private_file() {
  local path="$1" create="${2:-1}" file_uid file_links
  if [ -L "$path" ]; then
    bootstrap_fail
  fi
  if path_is_present "$path"; then
    [ -f "$path" ] || bootstrap_fail
    file_uid=$(stat_value '%u' "$path" 2>/dev/null) || bootstrap_fail
    file_links=$(stat_value '%h' "$path" 2>/dev/null || stat_value '%l' "$path" 2>/dev/null) || bootstrap_fail
    [ "$file_uid" = "$EUID" ] || bootstrap_fail
    [ "$file_links" = 1 ] || bootstrap_fail
  else
    [ "$create" = 1 ] || return 0
    # umask 077 plus an explicit chmod establishes the mode before the first
    # byte is written. The private parent removes the practical race window.
    (set -C; : > "$path") 2>/dev/null || bootstrap_fail
  fi
  tighten_file "$path" || bootstrap_fail
}

LOG_PARENT=$(dirname "$LOG")
STAMP_PARENT=$(dirname "$STAMP")
ensure_private_parent "$PRIVATE_STATE_DIR" 1
ensure_private_parent "$LOG_PARENT" 0
ensure_private_parent "$STAMP_PARENT" 0
[ "$LOG" != "$STAMP" ] || bootstrap_fail
prepare_private_file "$LOG"
prepare_private_file "$STAMP" 0
if ! { exec 9>>"$LOG"; } 2>/dev/null; then
  bootstrap_fail
fi

ts() { date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || printf unknown; }

log_event() {
  local stage="$1" state="$2" exit_code="$3" detail="${4:-}"
  local line
  line="$(ts) stage=${stage} state=${state} exit=${exit_code}${detail:+ ${detail}}"
  printf '%s\n' "$line" >&9 2>/dev/null || true
  printf '%s\n' "$line" >&2 || true
}

# Push a Heimdall status panel. Optional (no-op if the hub env vars are unset).
# Never logs the token; curl output is discarded.
push_panel() {
  local state="$1" stage="$2" exit_code="$3" detail="${4:-}"
  local message="stage=${stage} state=${state} exit=${exit_code}${detail:+ ${detail}}"
  send_panel "$state" "$message"
}

fail_event() {
  local stage="$1" exit_code="${2:-1}" detail="${3:-}"
  trap - ERR
  log_event "$stage" fail "$exit_code" "$detail"
  push_panel fail "$stage" "$exit_code" "$detail"
  exit "$exit_code"
}

# Unexpected command failure after preflight (e.g. rclone sync aborts).
on_err() {
  local rc=$?
  trap - ERR
  log_event runtime fail "$rc"
  push_panel fail runtime "$rc"
  exit "${rc}"
}
trap on_err ERR

# ---- Preflight ----
case "$RETENTION_DAYS" in ''|*[!0-9]*) fail_event preflight 1;; esac
[ "$RETENTION_DAYS" -ge 1 ] || fail_event preflight 1
case "$MAX_DELETE" in ''|*[!0-9]*) fail_event preflight 1;; esac
case "$MAX_DELETE_PCT" in ''|*[!0-9]*) fail_event preflight 1;; esac
[ "$MAX_DELETE_PCT" -le 100 ] || fail_event preflight 1
command -v "$RCLONE" >/dev/null 2>&1 || fail_event preflight 1
[ -d "$SOURCE" ] || fail_event preflight 1
case "$REMOTE" in *:*|*/*) fail_event preflight 1;; esac
case "$CLASSIFICATION_REQUIRED" in 0|1) ;; *) fail_event preflight 1;; esac
# shellcheck source=scripts/no-cloud-filter.sh
. "$SCRIPT_DIR/no-cloud-filter.sh" 2>/dev/null || fail_event preflight 1

# ---- no-cloud exclusions (before any remote call) ----
# Folders whose contract forbids third-party storage never reach the remote.
# The same rules filter the sync and both sides of the delete gate, so marking
# a folder neither uploads it nor counts its earlier copies as deletions; those
# copies stay in current/ and the archive until the explicit, owner-approved
# offsite-purge-no-cloud.sh removes them. Only counts are reported: marked
# paths name clients. An unreadable or invalid file aborts the run.
# rclone options from the environment could re-include or follow into them.
no_cloud_rclone_env_check filters || fail_event preflight 1 "reason=${NO_CLOUD_ERROR}"
NO_CLOUD_FINGERPRINT=$(no_cloud_fingerprint "$CLASSIFICATION")
if ! no_cloud_load "$CLASSIFICATION"; then
  fail_event classification 1 "$(no_cloud_failure_detail)"
fi
if [ "$NO_CLOUD_SOURCE" = absent ] && [ "$CLASSIFICATION_REQUIRED" = 1 ]; then
  fail_event classification 1 "reason=absent"
fi
# While folders are marked, the classification file names them: keep it off
# the cloud too when it lives in the archive.
NO_CLOUD_MANIFEST=$(no_cloud_manifest_path "$SOURCE" "$CLASSIFICATION")
no_cloud_patterns "$NO_CLOUD_MANIFEST" || fail_event classification 1 "$(no_cloud_failure_detail)"
NO_CLOUD_UNMATCHED=0
for no_cloud_path in ${NO_CLOUD_PATHS[@]+"${NO_CLOUD_PATHS[@]}"}; do
  path_is_present "$SOURCE/${no_cloud_path%/}" || NO_CLOUD_UNMATCHED=$((NO_CLOUD_UNMATCHED + 1))
done
for no_cloud_pattern in ${NO_CLOUD_PATTERNS[@]+"${NO_CLOUD_PATTERNS[@]}"}; do
  RCLONE_FILTERS+=(--exclude "$no_cloud_pattern")
done
NO_CLOUD_MARKERS=${#NO_CLOUD_PATHS[@]}
log_event classification pass 0 "no_cloud_markers=${NO_CLOUD_MARKERS} source=${NO_CLOUD_SOURCE} no_cloud_unmatched=${NO_CLOUD_UNMATCHED}"

# Warn (don't fail) if the rclone config is group/world-readable — it holds secrets.
CONF="${RCLONE_CONFIG:-$HOME/.config/rclone/rclone.conf}"
if [ -f "$CONF" ]; then
  PERM=$(stat_mode "$CONF" 2>/dev/null || true)
  case "$PERM" in ""|600|400) ;; *) log_event config warning 0 "mode=${PERM}";; esac
fi

# Fail CLOSED unless the remote is provably a crypt remote with filename encryption.
# Prevents a misconfigured MIMIR_OFFSITE_REMOTE from uploading plaintext client data.
# Read only the type/encryption fields; never log the config (it contains the key).
# Contents must be encrypted too (no_data_encryption would upload them in clear).
CONF_SHOW=$("$RCLONE" config show "$REMOTE" 2>/dev/null || true)
no_cloud_crypt_check "$CONF_SHOW" basic || fail_event preflight 1

archive_id() {
  local n="$1" alphabet="0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz" out="" remainder
  # A leading tag distinguishes owned archive dirs from every other remote-root
  # directory. Six base-62 epoch digits remain sortable and last for centuries.
  while [ "$n" -gt 0 ]; do
    remainder=$((n % 62))
    out="${alphabet:remainder:1}${out}"
    n=$((n / 62))
  done
  [ "${#out}" -le 6 ] || return 1
  while [ "${#out}" -lt 6 ]; do out="0${out}"; done
  printf 'a%s' "$out"
}

DEST="${REMOTE}:current"
RUN_EPOCH=$(date -u +%s 2>/dev/null) || fail_event preflight 1
ARCHIVE_ID=$(archive_id "$RUN_EPOCH") || fail_event preflight 1
# A tagged seven-character sibling keeps the encrypted archive path no longer
# than the existing `current` component. archive/<timestamp> pushed otherwise
# otherwise-valid provider paths over common encrypted-path limits.
ARCHIVE="${REMOTE}:${ARCHIVE_ID}"

# Connectivity + ensure destination exists (mkdir is idempotent and proves auth/write).
if [ -z "$DRY_RUN" ]; then
  if ! "$RCLONE" mkdir "$DEST" >/dev/null 2>&1; then
    fail_event connect 1
  fi
else
  if ! "$RCLONE" lsd "${REMOTE}:" >/dev/null 2>&1; then
    log_event connect warning 1
  fi
fi

# ---- Preflight delete-count gate (mirrors sync-artifacts.sh's rsync safety) ----
# Files present in current/ but absent from the source would be MOVED to the archive
# by --backup-dir. That is non-destructive (30-day history), but an implausibly large
# change set (e.g. the source got wiped) should STOP and alert, not silently mirror an
# empty current/ and report success. Skipped on dry-run and on the first run (empty dest).
if [ -z "$DRY_RUN" ]; then
  if ! DEST_LIST=$("$RCLONE" lsf -R --files-only "$DEST" "${RCLONE_FILTERS[@]}" 2>/dev/null | sort); then
    fail_event delete_gate 1
  fi
  if ! SRC_LIST=$("$RCLONE" lsf -R --files-only "$SOURCE" "${RCLONE_FILTERS[@]}" 2>/dev/null | sort); then
    fail_event delete_gate 1
  fi
  DEST_N=$(printf '%s\n' "$DEST_LIST" | grep -c . || true)
  DELETES=$(comm -23 <(printf '%s\n' "$DEST_LIST") <(printf '%s\n' "$SRC_LIST") | grep -c . || true)
  if [ "$DEST_N" -gt 0 ] && [ "$DELETES" -gt 0 ]; then
    PCT=$(( DELETES * 100 / DEST_N ))
    if [ "$DELETES" -ge "$MAX_DELETE" ] || [ "$PCT" -gt "$MAX_DELETE_PCT" ]; then
      fail_event delete_gate 1 "deletes=${DELETES} delete_pct=${PCT}"
    fi
    log_event delete_gate pass 0 "deletes=${DELETES} delete_pct=${PCT}"
  fi
fi

log_event sync running 0 "${DRY_RUN:+dry_run=1}"

# Mirror the current state. Overwritten/deleted files are MOVED into the per-run
# archive dir (never destroyed). --max-delete is a second-line guard behind the
# preflight gate above.
#
# rclone's log names artifacts, so it is never stored or forwarded. It is
# streamed as JSON straight into an in-memory counter of provider path-length
# rejections (mimir#58). Only per-object error records count, once per distinct
# `object` field; the rejection class is read from the separate `msg` field, so
# summary records (no object) and retries are not counted. rclone's output can
# reach only awk's input: awk prints a single number, and the exit statuses are
# printed by the shell afterwards, so the log cannot forge a status.
# shellcheck disable=SC2016
PATHLEN_AWK='
  # Value of a top-level string field in one compact JSON record, prefixed with
  # "=" when present. In valid JSON a raw `,"` or `{"` only occurs between
  # fields, never inside a string, so the first match is structural.
  function field(s, key,   p, i, c, out) {
    p = index(s, "{\"" key "\":\"")
    if (p == 0) p = index(s, ",\"" key "\":\"")
    if (p == 0) return ""
    out = "="
    for (i = p + length(key) + 5; i <= length(s); i++) {
      c = substr(s, i, 1)
      if (c == "\"") return out
      if (c == "\\") { out = out substr(s, i, 2); i++ } else out = out c
    }
    return ""
  }
  {
    if (tolower(field($0, "level")) != "=error") next
    object = field($0, "object")
    if (object == "" || object == "=") next
    msg = field($0, "msg")
    # Anchored error shapes only: OS errors embed pathnames in msg, so a bare
    # substring match would let a filename choose the class.
    # OneDrive/SharePoint: "Failed to copy: invalidRequest: pathIsTooLong: ...".
    # ENAMETOOLONG: "Failed to copy: open <path>: file name too long".
    if (msg !~ /^=Failed to [a-z ]+: invalidRequest: pathIsTooLong: / &&
        msg !~ /^=Failed to [a-z ]+: (open|mkdir|lstat|stat|rename|remove|chtimes) .*: file name too long$/) next
    if (!(object in seen)) { seen[object] = 1; n++ }
  }
  END { print n + 0 }
'
run_sync() {
  local -a status
  # shellcheck disable=SC2086
  if "$RCLONE" sync "${SOURCE}/" "$DEST" \
    --backup-dir "$ARCHIVE" \
    --max-delete "$MAX_DELETE" \
    --transfers 4 --checkers 8 \
    "${RCLONE_FILTERS[@]}" \
    --use-json-log --stats 0 $DRY_RUN 2>&1 >/dev/null |
    awk "$PATHLEN_AWK" 2>/dev/null; then
    status=(0 0)
  else
    status=("${PIPESTATUS[@]}")
  fi
  printf '\n%s %s\n' "${status[0]:-unknown}" "${status[1]:-unknown}"
}
sync_rc=unknown
counter_rc=unknown
pathlen_rejections=""
if SYNC_RESULT=$(run_sync); then
  pathlen_rejections=$(printf '%s\n' "$SYNC_RESULT" | sed -n '1p')
  read -r sync_rc counter_rc <<< "$(printf '%s\n' "$SYNC_RESULT" | sed -n '$p')" || true
fi
# An unreadable status is a failed sync, never a success.
case "$sync_rc" in ''|*[!0-9]*) fail_event sync 1;; esac
# A failed counter reports nothing rather than a guess.
[ "$counter_rc" = 0 ] || pathlen_rejections=""
case "$pathlen_rejections" in ''|*[!0-9]*) pathlen_rejections=0;; esac
# A marker that arrived with a concurrent archive sync was not applied to this
# traversal, so its folder may have been uploaded: fail loudly (purge if needed).
# Other edits to the file during the run are harmless.
if [ "$(no_cloud_fingerprint "$CLASSIFICATION")" != "$NO_CLOUD_FINGERPRINT" ]; then
  NO_CLOUD_LOADED=$(printf '%s\n' ${NO_CLOUD_PATHS[@]+"${NO_CLOUD_PATHS[@]}"})
  if ! no_cloud_load "$CLASSIFICATION" ||
    [ "$(printf '%s\n' ${NO_CLOUD_PATHS[@]+"${NO_CLOUD_PATHS[@]}"})" != "$NO_CLOUD_LOADED" ]; then
    fail_event classification 1 "reason=changed_during_run"
  fi
fi
if [ "$sync_rc" -ne 0 ]; then
  if [ "$pathlen_rejections" -gt 0 ]; then
    fail_event sync "$sync_rc" "pathlen_rejections=${pathlen_rejections} doc=offsite-backup.md#path-length-budget"
  fi
  fail_event sync "$sync_rc"
fi

if [ -n "$DRY_RUN" ]; then
  log_event complete pass 0 "dry_run=1 no_cloud_markers=${NO_CLOUD_MARKERS}"
  exit 0
fi

# Prune whole archive run-dirs older than the retention horizon, BY NAME (the dir's
# encoded UTC epoch), not by object mtime — sync preserves source mtimes, so an old file
# just moved into the archive must NOT be judged old by its own mtime. Best-effort:
# a prune failure must not fail the backup (the mirror already succeeded).
prune_archive() {
  local cutoff_epoch cutoff_id legacy_cutoff d
  cutoff_epoch=$(date -u -d "${RETENTION_DAYS} days ago" +%s 2>/dev/null || true)
  if [ -z "$cutoff_epoch" ]; then
    log_event prune warning 1
    return 0
  fi
  cutoff_id=$(archive_id "$cutoff_epoch") || {
    log_event prune warning 1
    return 0
  }
  while IFS= read -r d; do
    d="${d%/}"; [ -n "$d" ] || continue
    if [[ "$d" =~ ^a[0-9A-Za-z]{6}$ ]] && [[ "$d" < "$cutoff_id" ]]; then
      if "$RCLONE" purge "${REMOTE}:${d}" >/dev/null 2>&1; then
        log_event prune pass 0
      else
        log_event prune warning 1
      fi
    fi
  done < <("$RCLONE" lsf --dirs-only "${REMOTE}:" 2>/dev/null || true)

  # Backward-compatible cleanup of the former archive/<UTC timestamp> layout.
  legacy_cutoff=$(date -u -d "@${cutoff_epoch}" +%Y-%m-%dT%H%M%SZ 2>/dev/null || true)
  while IFS= read -r d; do
    d="${d%/}"; [ -n "$d" ] || continue
    if [[ "$d" < "$legacy_cutoff" ]]; then
      if "$RCLONE" purge "${REMOTE}:archive/${d}" >/dev/null 2>&1; then
        log_event prune pass 0
      else
        log_event prune warning 1
      fi
    fi
  done < <("$RCLONE" lsf --dirs-only "${REMOTE}:archive" 2>/dev/null || true)
}
prune_archive

# Heartbeat + success panel.
publish_stamp() {
  local temporary stamp_value
  temporary=$(mktemp "$STAMP_PARENT/.offsite-stamp.XXXXXX" 2>/dev/null) || return 1
  if ! tighten_file "$temporary"; then
    rm -f "$temporary" 2>/dev/null || true
    return 1
  fi
  stamp_value=$(date +%s 2>/dev/null) || {
    rm -f "$temporary" 2>/dev/null || true
    return 1
  }
  if ! printf '%s\n' "$stamp_value" > "$temporary" 2>/dev/null; then
    rm -f "$temporary" 2>/dev/null || true
    return 1
  fi
  if ! mv -f "$temporary" "$STAMP" 2>/dev/null; then
    rm -f "$temporary" 2>/dev/null || true
    return 1
  fi
}

publish_stamp || fail_event stamp 1
COUNT=$(find "$SOURCE" -type f ! -path '*/.git/*' 2>/dev/null | wc -l | tr -d ' ')
log_event complete pass 0 "files=${COUNT} no_cloud_markers=${NO_CLOUD_MARKERS}"
push_panel pass complete 0 "files=${COUNT} no_cloud_markers=${NO_CLOUD_MARKERS}"
