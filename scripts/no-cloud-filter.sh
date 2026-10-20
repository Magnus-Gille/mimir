#!/bin/bash
# shellcheck shell=bash
#
# no-cloud markers from the archive's classification file (data-classification
# policy v1.1, decision D). A folder whose contract forbids third-party storage,
# even encrypted, is marked in <archive root>/classification.yaml:
#
#   paths:
#     mgc/customers/<client>/: {level: client-restricted, no-cloud: true}
#
# Sourced by offsite-backup.sh, offsite-purge-no-cloud.sh and
# offline-copy-no-cloud.sh. Executed directly, it prints rclone filter rules for
# operator use in a private terminal (they name client folders):
#
#   no-cloud-filter.sh --exclude FILE   "- " rules: everything except no-cloud
#   no-cloud-filter.sh --include FILE   "+ " rules then "- **": only no-cloud
#
# Exit status: 0 parsed or absent, 2 invalid, 3 unreadable, 4 a non-ASCII
# marker that cannot be normalized (python3 missing, or not UTF-8), 64 usage.
# Diagnostics are content-blind: counts, a reason and a line number, never a path.
#
# The parser is deliberately strict and accepts exactly this schema, so a typo
# such as `no_cloud: true` fails closed instead of uploading the folder:
#   - blank lines and full-line `#` comments anywhere;
#   - top-level `default: <level>` (at most once) and `paths:` (exactly once);
#   - below `paths:`, one entry per line, all at the same space indentation:
#       <path>: <level>
#       <path>: {level: <level>, no-cloud: true|false}
#     each optionally followed by ` # comment`;
#   - <path> is an unquoted relative path without `.`/`..`/empty segments;
#     a no-cloud path must name a folder (trailing `/`); duplicates are invalid;
#   - <level> is a lowercase token ([a-z][a-z0-9-]*).
# Anything else (tabs, CR, quotes, block mappings, lists, unknown keys or
# top-level keys, YAML anchors) is invalid. No YAML library is needed.

# shellcheck disable=SC2016
NO_CLOUD_AWK='
function reject() { print "ERR " NR; failed = 1; exit 2 }
function is_level(v) { return v ~ /^[a-z][a-z0-9-]*$/ }
function plain_value(s,   p) {
  p = index(s, " #")
  if (p > 0) s = substr(s, 1, p - 1)
  sub(/^ +/, "", s)
  sub(/ +$/, "", s)
  return s
}
function trim(s) {
  sub(/^ +/, "", s)
  sub(/ +$/, "", s)
  return s
}
{
  line = $0
  if (line ~ /[[:cntrl:]]/) reject()
  if (line ~ /^ *$/ || line ~ /^ *#/) next
  if (substr(line, 1, 1) != " ") {
    if (line ~ /^default:( |$)/) {
      if (seen_default++) reject()
      if (!is_level(plain_value(substr(line, 9)))) reject()
      in_paths = 0
      next
    }
    if (line ~ /^paths:( +#.*)? *$/) {
      if (seen_paths++) reject()
      in_paths = 1
      next
    }
    reject()
  }
  if (!in_paths) reject()
  match(line, /^ +/)
  if (indent == 0) indent = RLENGTH
  else if (RLENGTH != indent) reject()
  rest = substr(line, RLENGTH + 1)
  p = index(rest, ": ")
  if (p == 0) reject()
  key = substr(rest, 1, p - 1)
  value = substr(rest, p + 1)
  if (key == "" || index("-?:,[]{}#&*!|>\047\"%@`", substr(key, 1, 1))) reject()
  if (key ~ / $/ || index(key, " #")) reject()
  if (substr(key, 1, 1) == "/" || index(key, "//") || ("/" key "/") ~ /\/\.\.?\//) reject()
  if (key in seen) reject()
  seen[key] = 1
  sub(/^ +/, "", value)
  if (substr(value, 1, 1) == "{") {
    q = index(value, "}")
    if (q == 0) reject()
    after = substr(value, q + 1)
    if (after !~ /^( +#.*)? *$/) reject()
    inner = substr(value, 2, q - 2)
    if (inner ~ /[{}"#]/ || index(inner, "[") || index(inner, "]") || index(inner, "\047")) reject()
    n = split(inner, parts, ",")
    level = ""
    marker = ""
    split("", keys)
    for (i = 1; i <= n; i++) {
      c = index(parts[i], ":")
      if (c == 0) reject()
      k = trim(substr(parts[i], 1, c - 1))
      v = trim(substr(parts[i], c + 1))
      if (k in keys) reject()
      keys[k] = 1
      if (k == "level") {
        if (!is_level(v)) reject()
        level = v
      } else if (k == "no-cloud") {
        if (v != "true" && v != "false") reject()
        marker = v
      } else reject()
    }
    if (level == "") reject()
    if (marker == "true") {
      if (substr(key, length(key), 1) != "/") reject()
      print "P" key
    }
    next
  }
  if (!is_level(plain_value(value))) reject()
}
END {
  if (failed) exit 2
  if (!seen_paths) { print "ERR 0"; exit 2 }
  print "OK"
}
'

# no_cloud_load FILE
#   Returns 0 when FILE was parsed (NO_CLOUD_SOURCE=file) or is absent
#   (NO_CLOUD_SOURCE=absent); NO_CLOUD_PATHS then holds the marked folders.
#   Returns 1 when FILE is present but unusable: NO_CLOUD_ERROR=unreadable
#   (symlink, non-regular, unreadable, read error) or NO_CLOUD_ERROR=invalid
#   with NO_CLOUD_LINE set (0 means the whole file).
no_cloud_load() {
  local file="$1" output="" status=0 record complete=0
  NO_CLOUD_PATHS=()
  NO_CLOUD_SOURCE=""
  NO_CLOUD_ERROR=""
  NO_CLOUD_LINE=""
  if [ ! -e "$file" ] && [ ! -L "$file" ]; then
    NO_CLOUD_SOURCE=absent
    return 0
  fi
  NO_CLOUD_SOURCE="file"
  # The file arrives with the synced archive; a link could point anywhere.
  if [ -L "$file" ] || [ ! -f "$file" ] || [ ! -r "$file" ]; then
    NO_CLOUD_ERROR=unreadable
    return 1
  fi
  output=$(LC_ALL=C awk "$NO_CLOUD_AWK" < "$file" 2>/dev/null) || status=$?
  while IFS= read -r record; do
    case "$record" in
      OK) complete=1 ;;
      P?*) NO_CLOUD_PATHS+=("${record#P}") ;;
      'ERR '*) NO_CLOUD_LINE="${record#ERR }" ;;
    esac
  done <<EOF
$output
EOF
  case "$NO_CLOUD_LINE" in *[!0-9]*) NO_CLOUD_LINE="" ;; esac
  if [ "$status" = 2 ] && [ -n "$NO_CLOUD_LINE" ]; then
    NO_CLOUD_PATHS=()
    NO_CLOUD_ERROR=invalid
    return 1
  fi
  if [ "$status" != 0 ] || [ "$complete" != 1 ]; then
    NO_CLOUD_PATHS=()
    NO_CLOUD_LINE=""
    NO_CLOUD_ERROR=unreadable
    return 1
  fi
  return 0
}

# rclone filters compare names byte for byte, but the same folder name can be
# stored precomposed (NFC, Linux/Windows) or decomposed (NFD, macOS Finder), and
# the archive mixes both. Every non-ASCII character whose forms differ becomes an
# rclone alternation `{nfc,nfd}`, so a marker matches either spelling. Only the
# standard library is used, and only when a marked path is non-ASCII.
# shellcheck disable=SC2016
NO_CLOUD_UNICODE_PY='
import sys, unicodedata
for raw in sys.stdin.buffer.read().split(b"\n"):
    if not raw:
        continue
    text = unicodedata.normalize("NFC", raw.decode("utf-8", "strict"))
    out = []
    for ch in text:
        decomposed = unicodedata.normalize("NFD", ch)
        out.append(ch if decomposed == ch else "{" + ch + "," + decomposed + "}")
    sys.stdout.buffer.write("".join(out).encode("utf-8") + b"\n")
'

# no_cloud_patterns: fill NO_CLOUD_PATTERNS with two rooted rclone globs per
# marked folder, the folder's contents and a same-named file, with glob
# metacharacters escaped so a folder name cannot widen or narrow the match.
# Returns 1 (NO_CLOUD_ERROR=unicode) when a non-ASCII marker cannot be made
# normalization-insensitive: python3 is missing or the path is not UTF-8.
no_cloud_patterns() {
  local path escaped char i joined converted count=0
  NO_CLOUD_PATTERNS=()
  for path in ${NO_CLOUD_PATHS[@]+"${NO_CLOUD_PATHS[@]}"}; do
    escaped=""
    for ((i = 0; i < ${#path}; i++)); do
      char="${path:i:1}"
      # shellcheck disable=SC1003
      case "$char" in
        '\'|'*'|'?'|'['|']'|'{'|'}') escaped+="\\${char}" ;;
        *) escaped+="$char" ;;
      esac
    done
    escaped="/${escaped%/}"
    NO_CLOUD_PATTERNS+=("$escaped" "${escaped}/**")
  done
  [ "${#NO_CLOUD_PATTERNS[@]}" -gt 0 ] || return 0
  joined=$(printf '%s\n' "${NO_CLOUD_PATTERNS[@]}")
  LC_ALL=C grep -q '[^ -~]' <<< "$joined" || return 0
  if ! converted=$(printf '%s\n' "$joined" | "${NO_CLOUD_PYTHON:-python3}" -I -c "$NO_CLOUD_UNICODE_PY" 2>/dev/null); then
    NO_CLOUD_PATTERNS=()
    NO_CLOUD_ERROR=unicode
    return 1
  fi
  NO_CLOUD_PATTERNS=()
  while IFS= read -r path; do
    [ -n "$path" ] || continue
    NO_CLOUD_PATTERNS+=("$path")
    count=$((count + 1))
  done <<EOF
$converted
EOF
  if [ "$count" -ne $(( ${#NO_CLOUD_PATHS[@]} * 2 )) ]; then
    NO_CLOUD_PATTERNS=()
    NO_CLOUD_ERROR=unicode
    return 1
  fi
}

# no_cloud_failure_detail: the content-blind diagnostic for a failed load.
no_cloud_failure_detail() {
  printf 'reason=%s%s' "$NO_CLOUD_ERROR" "${NO_CLOUD_LINE:+ line=${NO_CLOUD_LINE}}"
}

no_cloud_main() {
  set -euo pipefail
  local mode="${1:-}" file="${2:-}" pattern
  case "$mode" in --exclude|--include) ;; *) mode="" ;; esac
  if [ -z "$mode" ] || [ -z "$file" ] || [ "$#" -ne 2 ]; then
    printf '%s\n' 'usage: no-cloud-filter.sh --exclude|--include CLASSIFICATION_FILE' >&2
    return 64
  fi
  if ! no_cloud_load "$file"; then
    printf 'classification=%s%s\n' "$NO_CLOUD_ERROR" "${NO_CLOUD_LINE:+ line=${NO_CLOUD_LINE}}" >&2
    [ "$NO_CLOUD_ERROR" = invalid ] && return 2
    return 3
  fi
  if ! no_cloud_patterns; then
    printf 'classification=%s\n' "$NO_CLOUD_ERROR" >&2
    return 4
  fi
  for pattern in ${NO_CLOUD_PATTERNS[@]+"${NO_CLOUD_PATTERNS[@]}"}; do
    if [ "$mode" = --exclude ]; then
      printf -- '- %s\n' "$pattern"
    else
      printf -- '+ %s\n' "$pattern"
    fi
  done
  if [ "$mode" = --include ]; then
    printf -- '- **\n'
  fi
  printf 'no_cloud_markers=%s source=%s\n' "${#NO_CLOUD_PATHS[@]}" "$NO_CLOUD_SOURCE" >&2
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  no_cloud_main "$@"
fi
