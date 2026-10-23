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
# FILE is taken to sit at the archive root: while any folder is marked, the
# rules also cover FILE itself, because it names the marked folders.
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
  # stderr goes first so a failed open of the file cannot print its path.
  output=$(LC_ALL=C awk "$NO_CLOUD_AWK" 2>/dev/null < "$file") || status=$?
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
# the archive mixes both. Each character cluster (a base character with the marks
# and jamo that compose with it) whose literal, NFC and NFD forms differ becomes
# an rclone alternation such as `{ö,ö}`, keeping the literal spelling, so a
# marker matches every spelling. Only the standard library is used, and only
# when a marked path is non-ASCII.
# shellcheck disable=SC2016
NO_CLOUD_UNICODE_PY='
import sys, unicodedata
NFC = lambda s: unicodedata.normalize("NFC", s)
NFD = lambda s: unicodedata.normalize("NFD", s)
META = set(",{}[]*?\\")
for raw in sys.stdin.buffer.read().split(b"\n"):
    if not raw:
        continue
    clusters = []
    for ch in raw.decode("utf-8", "strict"):
        if clusters and (unicodedata.combining(ch) or NFC(clusters[-1] + ch) != NFC(clusters[-1]) + NFC(ch)):
            clusters[-1] += ch
        else:
            clusters.append(ch)
    out = []
    for cluster in clusters:
        forms = []
        for form in (cluster, NFC(cluster), NFD(cluster)):
            if form not in forms:
                forms.append(form)
        if len(forms) == 1:
            out.append(cluster)
            continue
        if any(c in META for form in forms for c in form):
            sys.exit(3)
        out.append("{" + ",".join(forms) + "}")
    sys.stdout.buffer.write("".join(out).encode("utf-8") + b"\n")
'

# no_cloud_escape STRING: NO_CLOUD_ESCAPED is STRING with rclone glob
# metacharacters escaped, so a name cannot widen or narrow a match.
no_cloud_escape() {
  local value="$1" char i
  NO_CLOUD_ESCAPED=""
  for ((i = 0; i < ${#value}; i++)); do
    char="${value:i:1}"
    # shellcheck disable=SC1003
    case "$char" in
      '\'|'*'|'?'|'['|']'|'{'|'}') NO_CLOUD_ESCAPED+="\\${char}" ;;
      *) NO_CLOUD_ESCAPED+="$char" ;;
    esac
  done
}

# no_cloud_patterns [MANIFEST]: fill NO_CLOUD_PATTERNS with pairs of rooted
# rclone globs, a marked folder's contents and a same-named file. MANIFEST is
# the classification file's path relative to the archive root, when it lies
# inside it: while any folder is marked, the manifest names those folders, so
# it is kept off the cloud as one more pair.
# Returns 1 (NO_CLOUD_ERROR=unicode) when a non-ASCII marker cannot be made
# normalization-insensitive: python3 is missing or the path is not UTF-8.
no_cloud_patterns() {
  local manifest="${1:-}" path joined converted count=0 pairs
  NO_CLOUD_PATTERNS=()
  for path in ${NO_CLOUD_PATHS[@]+"${NO_CLOUD_PATHS[@]}"}; do
    no_cloud_escape "${path%/}"
    NO_CLOUD_PATTERNS+=("/${NO_CLOUD_ESCAPED}" "/${NO_CLOUD_ESCAPED}/**")
  done
  [ "${#NO_CLOUD_PATTERNS[@]}" -gt 0 ] || return 0
  if [ -n "$manifest" ]; then
    no_cloud_escape "$manifest"
    NO_CLOUD_PATTERNS+=("/${NO_CLOUD_ESCAPED}" "/${NO_CLOUD_ESCAPED}/**")
  fi
  pairs=$(( ${#NO_CLOUD_PATTERNS[@]} ))
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
  if [ "$count" -ne "$pairs" ]; then
    NO_CLOUD_PATTERNS=()
    NO_CLOUD_ERROR=unicode
    return 1
  fi
}

# no_cloud_manifest_path ROOT FILE: FILE's path relative to the archive ROOT
# when it lies inside it, after resolving symlinks and `.`/`..` on both sides
# (a lexical prefix would let `ROOT/./classification.yaml` escape the rule).
# Prints nothing when FILE is outside ROOT or either cannot be resolved.
no_cloud_manifest_path() {
  local root="$1" file="$2" root_real dir_real base
  base="${file##*/}"
  case "$base" in ''|.|..) return 0 ;; esac
  root_real=$(cd -- "$root" 2>/dev/null && pwd -P) || return 0
  dir_real=$(cd -- "$(dirname -- "$file")" 2>/dev/null && pwd -P) || return 0
  case "$dir_real/" in
    "$root_real"/*) ;;
    *) return 0 ;;
  esac
  if [ "$dir_real" = "$root_real" ]; then
    printf '%s' "$base"
  else
    printf '%s/%s' "${dir_real#"$root_real"/}" "$base"
  fi
}

# no_cloud_fingerprint FILE: a checksum of the classification file (or
# "absent"/"unreadable"), so a run can tell whether it changed under it.
no_cloud_fingerprint() {
  local file="$1"
  if [ ! -e "$file" ] && [ ! -L "$file" ]; then
    printf absent
  elif [ -L "$file" ] || [ ! -f "$file" ]; then
    printf unreadable
  else
    cksum 2>/dev/null < "$file" || printf unreadable
  fi
}

# no_cloud_rclone_env_check strict|filters: rclone also reads its options from
# RCLONE_* environment variables, and those override the config the scripts
# verified (RCLONE_CRYPT_REMOTE, RCLONE_CONFIG_<NAME>_*) or add filter rules
# and link following that defeat the exclusions. `filters` rejects those
# classes; `strict` rejects every RCLONE_* setting except the config location
# and its password (and RCLONE_BIN, the scripts' own). Fails with NO_CLOUD_ERROR=rclone_env, naming nothing.
no_cloud_rclone_env_check() {
  local mode="$1" name
  while IFS= read -r name; do
    case "$name" in
      ''|RCLONE_BIN|RCLONE_CONFIG|RCLONE_CONFIG_PASS|RCLONE_PASSWORD_COMMAND) continue ;;
    esac
    if [ "$mode" = strict ]; then
      NO_CLOUD_ERROR=rclone_env
      return 1
    fi
    case "$name" in
      RCLONE_CONFIG_*|RCLONE_CRYPT_*|RCLONE_LOCAL_*|RCLONE_FILTER*|RCLONE_INCLUDE*|RCLONE_EXCLUDE*|\
      RCLONE_FILES_FROM*|RCLONE_DELETE_EXCLUDED|RCLONE_COPY_LINKS|RCLONE_LINKS|RCLONE_IGNORE_CASE)
        NO_CLOUD_ERROR=rclone_env
        return 1
        ;;
    esac
  done <<EOF
$(env | sed -n 's/^\(RCLONE_[A-Za-z0-9_]*\)=.*/\1/p')
EOF
}

# no_cloud_crypt_check "$(rclone config show NAME)" basic|strict: the remote is a
# crypt remote that encrypts file contents and names. `strict` also requires
# standard filename encryption and directory-name encryption. Unset options take
# rclone's defaults (standard, true, false). NO_CLOUD_ERROR names the reason.
no_cloud_crypt_check() {
  local conf="$1" mode="$2" type fenc denc nodata
  type=$(printf '%s\n' "$conf" | awk -F' = ' '/^type =/{print $2; exit}')
  fenc=$(printf '%s\n' "$conf" | awk -F' = ' '/^filename_encryption =/{print $2; exit}')
  denc=$(printf '%s\n' "$conf" | awk -F' = ' '/^directory_name_encryption =/{print $2; exit}')
  nodata=$(printf '%s\n' "$conf" | awk -F' = ' '/^no_data_encryption =/{print $2; exit}')
  NO_CLOUD_ERROR=""
  if [ "$type" != crypt ]; then
    NO_CLOUD_ERROR=not_crypt
  elif [ "$fenc" = off ]; then
    NO_CLOUD_ERROR=filename_encryption_off
  else
    case "$nodata" in ''|false|False|FALSE|0) ;; *) NO_CLOUD_ERROR=no_data_encryption ;; esac
  fi
  if [ -z "$NO_CLOUD_ERROR" ] && [ "$mode" = strict ]; then
    case "$fenc" in ''|standard) ;; *) NO_CLOUD_ERROR=filename_encryption_not_standard ;; esac
    case "$denc" in ''|true|True|TRUE|1) ;; *) NO_CLOUD_ERROR=directory_name_encryption_off ;; esac
  fi
  [ -z "$NO_CLOUD_ERROR" ]
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
  if ! no_cloud_patterns "$(basename -- "$file")"; then
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
