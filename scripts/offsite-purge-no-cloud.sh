#!/bin/bash
set -euo pipefail
export LC_ALL=C

# Mímir no-cloud purge — removes the cloud copies of folders that were marked
# `no-cloud` (data-classification policy v1.1, decision D) after they had been
# uploaded: from current/ and from every archive run-dir of the offsite crypt
# remote. offsite-backup.sh only stops uploading a marked folder; it never
# deletes its earlier copies, so the nightly delete gate is never bypassed.
#
# Owner-only and explicitly invoked: never from a timer, never by an agent.
# Run it on the NAS as the backup user, outside the 03:30 offsite window:
#
#   offsite-purge-no-cloud.sh                plan: count matching objects, delete
#                                            nothing, record a single-use plan
#                                            (valid 24 h) and print its id
#   offsite-purge-no-cloud.sh --execute ID   the owner's approval of that plan:
#                                            recount, delete exactly the planned
#                                            objects, verify none remain, receipt
#
# Execute refuses when the markers, the remote or any per-directory count have
# changed since the plan. Every delete is confined to the marked folders by
# rclone filters and capped by --max-delete at the planned count. Output, plans
# and receipts are content-blind (counts, ids, timestamps): never paths, archive
# names or rclone output. The provider may keep deleted blobs in its recycle
# bin; emptying it is the owner's final step (docs/offsite-backup.md).

SOURCE="${MIMIR_OFFSITE_ROOT:-$HOME/mimir}"
REMOTE="${MIMIR_OFFSITE_REMOTE:-mimir-crypt}"
CLASSIFICATION="${MIMIR_OFFSITE_CLASSIFICATION:-$SOURCE/classification.yaml}"
STATE_DIR="${MIMIR_OFFSITE_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/mimir}"
PURGE_DIR="$STATE_DIR/offsite-private/no-cloud-purge"
RCLONE="${RCLONE_BIN:-rclone}"
PLAN_TTL=86400
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" 2>/dev/null && pwd) || SCRIPT_DIR=""

umask 077

ts() { date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || printf unknown; }

event() {
  local stage="$1" state="$2" exit_code="$3" detail="${4:-}"
  printf '%s stage=%s state=%s exit=%s%s\n' "$(ts)" "$stage" "$state" "$exit_code" "${detail:+ ${detail}}" >&2
}

fail() {
  event "$1" fail 1 "${2:-}"
  exit 1
}

usage() {
  printf '%s\n' 'usage: offsite-purge-no-cloud.sh [--execute PLAN_ID]' >&2
  exit 64
}

MODE=plan
PLAN_ID=""
case "$#" in
  0) ;;
  2) [ "$1" = --execute ] || usage; MODE=execute; PLAN_ID="$2" ;;
  *) usage ;;
esac
if [ "$MODE" = execute ]; then
  case "$PLAN_ID" in ''|*[!0-9a-f]*) fail execute "reason=bad_plan_id" ;; esac
  [ "${#PLAN_ID}" -eq 32 ] || fail execute "reason=bad_plan_id"
fi

# ---- Preflight (no remote call before the classification is loaded) ----
case "$REMOTE" in ''|*:*|*/*) fail preflight ;; esac
command -v "$RCLONE" >/dev/null 2>&1 || fail preflight
# shellcheck source=scripts/no-cloud-filter.sh
. "$SCRIPT_DIR/no-cloud-filter.sh" 2>/dev/null || fail preflight

sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 | awk '{print $1}'
  else
    return 1
  fi
}

random_hex() {
  local value
  value=$(od -An -tx1 -N16 /dev/urandom 2>/dev/null | tr -d ' \n') || return 1
  [ "${#value}" -eq 32 ] || return 1
  printf '%s' "$value"
}

utc_of() {
  date -u -d "@$1" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -r "$1" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || printf unknown
}

ensure_private_dir() {
  local dir="$1" owner
  [ ! -L "$dir" ] || return 1
  if [ ! -e "$dir" ]; then
    mkdir -p "$dir" 2>/dev/null || return 1
  fi
  [ -d "$dir" ] && [ ! -L "$dir" ] || return 1
  owner=$(stat -c %u -- "$dir" 2>/dev/null || stat -f %u "$dir" 2>/dev/null) || return 1
  [ "$owner" = "$EUID" ] || return 1
  chmod 700 "$dir" 2>/dev/null || return 1
  if [ "$(uname -s 2>/dev/null || true)" = Darwin ]; then
    chmod -N "$dir" 2>/dev/null || true
  fi
}

# Write a private file atomically: content on stdin.
write_private() {
  local target="$1" temporary
  temporary=$(mktemp "$PURGE_DIR/.tmp.XXXXXX" 2>/dev/null) || return 1
  if ! { chmod 600 "$temporary" && cat > "$temporary" && mv -f "$temporary" "$target"; } 2>/dev/null; then
    rm -f "$temporary" 2>/dev/null || true
    return 1
  fi
}

plan_value() {
  awk -F= -v key="$1" '$1 == key { print substr($0, length(key) + 2); exit }' "$2" 2>/dev/null
}

no_cloud_load "$CLASSIFICATION" || fail classification "$(no_cloud_failure_detail)"
[ "$NO_CLOUD_SOURCE" = file ] || fail classification "reason=absent"
no_cloud_patterns || fail classification "$(no_cloud_failure_detail)"
MARKERS=${#NO_CLOUD_PATHS[@]}

if [ "$MODE" = plan ] && [ "$MARKERS" -eq 0 ]; then
  event plan pass 0 "no_cloud_markers=0 current_objects=0 archive_objects=0 archive_dirs=0"
  exit 0
fi

# ---- Execute: claim the plan before anything touches the remote ----
if [ "$MODE" = execute ]; then
  { ensure_private_dir "$STATE_DIR/offsite-private" && ensure_private_dir "$PURGE_DIR"; } || fail preflight "reason=state"
  PLAN_FILE="$PURGE_DIR/plan-$PLAN_ID"
  [ -f "$PLAN_FILE" ] && [ ! -L "$PLAN_FILE" ] || fail execute "reason=no_plan"
  # Renaming is the single-use claim: a concurrent or repeated execute finds no plan.
  CLAIMED="$PURGE_DIR/.executing-$PLAN_ID"
  mv "$PLAN_FILE" "$CLAIMED" 2>/dev/null || fail execute "reason=no_plan"
  PLAN_VERSION=$(plan_value version "$CLAIMED")
  PLAN_CREATED=$(plan_value created "$CLAIMED")
  PLAN_NONCE=$(plan_value nonce "$CLAIMED")
  PLAN_DIGEST=$(plan_value digest "$CLAIMED")
  PLAN_OBJECTS=$(plan_value planned_objects "$CLAIMED")
  PLAN_TARGETS=$(plan_value planned_targets "$CLAIMED")
  abandon() { rm -f "$CLAIMED" 2>/dev/null || true; fail execute "$1"; }
  [ "$(plan_value plan "$CLAIMED")" = "$PLAN_ID" ] && [ "$PLAN_VERSION" = 1 ] || abandon "reason=bad_plan"
  case "$PLAN_CREATED$PLAN_OBJECTS$PLAN_TARGETS" in ''|*[!0-9]*) abandon "reason=bad_plan" ;; esac
  [ "${#PLAN_NONCE}" -eq 32 ] && [ "${#PLAN_DIGEST}" -eq 64 ] || abandon "reason=bad_plan"
  NOW=$(date -u +%s 2>/dev/null) || abandon "reason=clock"
  AGE=$((NOW - PLAN_CREATED))
  [ "$AGE" -ge 0 ] && [ "$AGE" -le "$PLAN_TTL" ] || abandon "reason=plan_expired"
  [ "$MARKERS" -gt 0 ] || abandon "reason=plan_mismatch"
fi

# ---- The remote must be the verified crypt remote ----
CONF_SHOW=$("$RCLONE" config show "$REMOTE" 2>/dev/null || true)
RTYPE=$(printf '%s\n' "$CONF_SHOW" | awk -F' = ' '/^type =/{print $2; exit}')
FENC=$(printf '%s\n' "$CONF_SHOW" | awk -F' = ' '/^filename_encryption =/{print $2; exit}')
if [ "$RTYPE" != crypt ] || [ "$FENC" = off ]; then
  [ "$MODE" = plan ] || abandon "reason=remote"
  fail preflight "reason=remote"
fi

FILTERS=()
for pattern in "${NO_CLOUD_PATTERNS[@]}"; do
  FILTERS+=(--filter "+ $pattern")
done
FILTERS+=(--filter "- **")

# Targets: current/, the tagged archive run-dirs, and the legacy archive/<ts>.
list_targets() {
  local root_dirs legacy_dirs dir has_legacy=0
  TARGETS=()
  root_dirs=$("$RCLONE" lsf --dirs-only "${REMOTE}:" 2>/dev/null) || return 1
  while IFS= read -r dir; do
    dir="${dir%/}"
    # `archive` itself also has the shape of a run-dir name.
    if [ "$dir" = archive ]; then
      has_legacy=1
    elif [ "$dir" = current ] || [[ "$dir" =~ ^a[0-9A-Za-z]{6}$ ]]; then
      TARGETS+=("$dir")
    fi
  done <<EOF
$(printf '%s\n' "$root_dirs" | sort)
EOF
  [ "$has_legacy" = 1 ] || return 0
  legacy_dirs=$("$RCLONE" lsf --dirs-only "${REMOTE}:archive" 2>/dev/null) || return 1
  while IFS= read -r dir; do
    dir="${dir%/}"
    [ -n "$dir" ] && TARGETS+=("archive/$dir")
  done <<EOF
$(printf '%s\n' "$legacy_dirs" | sort)
EOF
}

count_target() {
  local listing
  listing=$("$RCLONE" lsf -R --files-only "${REMOTE}:$1" "${FILTERS[@]}" 2>/dev/null) || return 1
  COUNT=$(printf '%s' "$listing" | grep -c . || true)
}

# Count matching objects per target; ENTRIES holds "target<TAB>count" for the
# targets that hold any, which is what the plan approves.
measure() {
  local target
  ENTRIES=()
  CURRENT_OBJECTS=0
  ARCHIVE_OBJECTS=0
  ARCHIVE_DIRS=0
  list_targets || return 1
  for target in ${TARGETS[@]+"${TARGETS[@]}"}; do
    count_target "$target" || return 1
    [ "$COUNT" -gt 0 ] || continue
    ENTRIES+=("${target}"$'\t'"${COUNT}")
    if [ "$target" = current ]; then
      CURRENT_OBJECTS=$COUNT
    else
      ARCHIVE_OBJECTS=$((ARCHIVE_OBJECTS + COUNT))
      ARCHIVE_DIRS=$((ARCHIVE_DIRS + 1))
    fi
  done
  TOTAL=$((CURRENT_OBJECTS + ARCHIVE_OBJECTS))
}

# The random nonce keeps the stored digest from confirming guessed client names.
plan_digest() {
  {
    printf 'mimir-no-cloud-purge v1\n%s\n%s\n' "$1" "$REMOTE"
    printf 'pattern %s\n' "${NO_CLOUD_PATTERNS[@]}"
    printf 'target %s\n' ${ENTRIES[@]+"${ENTRIES[@]}"}
  } | sha256
}

if [ "$MODE" = plan ]; then
  measure || fail plan "reason=list"
  if [ "$TOTAL" -eq 0 ]; then
    event plan pass 0 "no_cloud_markers=${MARKERS} current_objects=0 archive_objects=0 archive_dirs=0"
    exit 0
  fi
  { ensure_private_dir "$STATE_DIR/offsite-private" && ensure_private_dir "$PURGE_DIR"; } || fail preflight "reason=state"
  PLAN_ID=$(random_hex) || fail plan "reason=random"
  NONCE=$(random_hex) || fail plan "reason=random"
  DIGEST=$(plan_digest "$NONCE") || fail plan "reason=digest"
  [ "${#DIGEST}" -eq 64 ] || fail plan "reason=digest"
  CREATED=$(date -u +%s 2>/dev/null) || fail plan "reason=clock"
  write_private "$PURGE_DIR/plan-$PLAN_ID" <<EOF || fail plan "reason=state"
plan=$PLAN_ID
version=1
created=$CREATED
nonce=$NONCE
digest=$DIGEST
no_cloud_markers=$MARKERS
planned_objects=$TOTAL
planned_targets=${#ENTRIES[@]}
EOF
  event plan pass 0 "no_cloud_markers=${MARKERS} current_objects=${CURRENT_OBJECTS} archive_objects=${ARCHIVE_OBJECTS} archive_dirs=${ARCHIVE_DIRS} plan=${PLAN_ID} expires=$(utc_of $((CREATED + PLAN_TTL)))"
  printf '%s\n' "Nothing was deleted. To delete these ${TOTAL} objects from the cloud permanently, the owner runs:"
  printf '  %s --execute %s\n' "$0" "$PLAN_ID"
  exit 0
fi

# ---- Execute the approved plan ----
measure || abandon "reason=list"
DIGEST=$(plan_digest "$PLAN_NONCE") || abandon "reason=digest"
if [ "$DIGEST" != "$PLAN_DIGEST" ] || [ "$TOTAL" != "$PLAN_OBJECTS" ] || [ "${#ENTRIES[@]}" != "$PLAN_TARGETS" ]; then
  abandon "reason=plan_mismatch"
fi

DELETE_FAILURES=0
RMDIRS_WARNINGS=0
for entry in "${ENTRIES[@]}"; do
  target="${entry%%$'\t'*}"
  planned="${entry##*$'\t'}"
  if ! "$RCLONE" delete "${REMOTE}:${target}" "${FILTERS[@]}" --max-delete "$planned" >/dev/null 2>&1; then
    DELETE_FAILURES=$((DELETE_FAILURES + 1))
  fi
  # Encrypted names of the emptied folders go too; filters confine rmdirs.
  if ! "$RCLONE" rmdirs "${REMOTE}:${target}" --leave-root "${FILTERS[@]}" >/dev/null 2>&1; then
    RMDIRS_WARNINGS=$((RMDIRS_WARNINGS + 1))
  fi
done

REMAINING=unknown
if measure; then
  REMAINING=$TOTAL
fi
OUTCOME=fail
if [ "$DELETE_FAILURES" = 0 ] && [ "$REMAINING" = 0 ]; then
  OUTCOME=pass
fi

write_private "$PURGE_DIR/receipt-$PLAN_ID" <<EOF || abandon "reason=receipt"
receipt=mimir-offsite-no-cloud-purge
version=1
plan=$PLAN_ID
planned_at=$(utc_of "$PLAN_CREATED")
executed_at=$(ts)
no_cloud_markers=$MARKERS
planned_objects=$PLAN_OBJECTS
planned_targets=$PLAN_TARGETS
delete_failures=$DELETE_FAILURES
rmdirs_warnings=$RMDIRS_WARNINGS
remaining_objects=$REMAINING
outcome=$OUTCOME
provider_trash=owner_action_required
EOF
rm -f "$CLAIMED" 2>/dev/null || true

DETAIL="planned_objects=${PLAN_OBJECTS} remaining_objects=${REMAINING} receipt=${PLAN_ID}"
if [ "$OUTCOME" = pass ]; then
  event execute pass 0 "$DETAIL"
  printf '%s\n' "Deleted from the remote. Empty the provider's recycle bin to finish (docs/offsite-backup.md)."
  exit 0
fi
fail execute "$DETAIL"
