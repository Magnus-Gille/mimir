#!/bin/bash
set -euo pipefail

# Upgrade the installed Mimir search reader runtime on the reader node to one
# exact, merged commit, and move the Mac client pin in the same rollout.
#
# Usage: ./scripts/deploy-search-reader.sh --release <40-hex-sha> [options]
#   --dry-run              preflight, build twice, render, print the plan; no mutation
#   --rollback             restore the pre-deploy state recorded for --release
#   --no-auto-rollback     on failure stop and leave the system as-is (marker absent)
#   --host ALIAS           ssh alias of the reader node (default m5)
#   --client-pin PATH      Mac client pin (default ~/.config/mimir-search/client.json)
#   --acceptance-config P  approved acceptance pins (default ~/.config/mimir-search/deploy-acceptance.json)
#   --state-dir PATH       local deploy journal root (default ~/.local/state/mimir-search-deploy)
#
# Exit codes: 0 deployed/no-op/rolled back, 2 usage, 3 preflight failed (target
# unchanged), 10 failed and automatically rolled back, 11 failed with unknown or
# unrestored state (marker absent; journal path printed).
#
# The contract lives in the Grimnir repository (docs/plans/mimir-search-deploy-entrypoint.md)
# and the registry entry `mimir-search-reader`; keep the two in lockstep. Every
# host interaction goes through the hooks below so the flow is testable with fakes:
#   MIMIR_DEPLOY_SSH             ssh command words (default: ssh -o BatchMode=yes -o ConnectTimeout=10)
#   MIMIR_DEPLOY_SCP             scp command words (default: scp -q -o BatchMode=yes -o ConnectTimeout=10)
#   MIMIR_DEPLOY_PYTHON          local python3 for the packager, renderer and helpers
#   MIMIR_DEPLOY_CLIENT          client launcher (default ~/.local/bin/mimir-search); run as
#                                `<launcher> --tool --config <pin>` with the request JSON on stdin
#   MIMIR_DEPLOY_REMOTE_PYTHON   interpreter for the remote probes (default /usr/bin/python3)
#   MIMIR_DEPLOY_REMOTE_USER     expected remote account (default magnus)
#   MIMIR_DEPLOY_EXPECTED_HOME   expected remote $HOME (default /home/magnus)
#   MIMIR_DEPLOY_WAIT_SECONDS    bounded wait for unit state changes (default 60)
#   MIMIR_DEPLOY_WARN_RETRY_SECONDS  pause before the single semantic-check warn retry (default 30)
# No credential is ever read here: the API key is read only by the remote probe
# process from runtime.env and never reaches argv, output, the journal or ssh env.
# xtrace must stay off so no expansion can echo a value.

export PYTHONDONTWRITEBYTECODE=1
export GIT_OPTIONAL_LOCKS=0
umask 077

UNIT_NAMES=(
  mimir-search.service
  mimir-search-refresh.service
  mimir-search-refresh.timer
  mimir-search-semantic-check.service
  mimir-search-semantic-check.timer
)
UNITS_WORDS="${UNIT_NAMES[*]}"
RO_OPS=" identity installed package-state config-check marker-state disk-free baseline probe-receipts probe-401 probe-listeners semantic-read "

SSH_WORDS="${MIMIR_DEPLOY_SSH:-ssh -o BatchMode=yes -o ConnectTimeout=10}"
SCP_WORDS="${MIMIR_DEPLOY_SCP:-scp -q -o BatchMode=yes -o ConnectTimeout=10}"
PY="${MIMIR_DEPLOY_PYTHON:-python3}"
CLIENT_WORDS="${MIMIR_DEPLOY_CLIENT:-${HOME:-/nonexistent}/.local/bin/mimir-search}"
RPY="${MIMIR_DEPLOY_REMOTE_PYTHON:-/usr/bin/python3}"
EXPECTED_USER="${MIMIR_DEPLOY_REMOTE_USER:-magnus}"
EXPECTED_HOME="${MIMIR_DEPLOY_EXPECTED_HOME:-/home/magnus}"
WAIT_SECONDS="${MIMIR_DEPLOY_WAIT_SECONDS:-60}"
WARN_RETRY_SECONDS="${MIMIR_DEPLOY_WARN_RETRY_SECONDS:-30}"
read -r -a SSH_CMD <<<"$SSH_WORDS"
read -r -a SCP_CMD <<<"$SCP_WORDS"
read -r -a CLIENT_CMD <<<"$CLIENT_WORDS"

RELEASE=""
DRY_RUN=0
ROLLBACK=0
AUTO_ROLLBACK=1
HOST="m5"
CLIENT_PIN=""
ACCEPT_CFG=""
STATE_DIR=""

TMP=""
JOURNAL=""
JOURNAL_ACTIVE=0
PHASE="init"
MUTATED=0
IN_ROLLBACK=0
COMMITTED=0
LOCAL_LOCK=""
REMOTE_LOCK_TOKEN=""
REMOTE_LOCK_HELD=0
R_OUT=""
R_RC=0
REMOTE_HOME=""
PKG_SHA=""
PREV_RELEASE=""
PREV_PKG_SHA=""
PREV_MARKER=""
LIVE_RELEASE=""
LIVE_MARKER=""
PIN_REL=""
PIN_PKG=""
PIN_SHA_A6=""
PIN_PREV_SHA=""
PIN_WRITTEN_SHA=""
SOURCE_ID=""
SNAPSHOT_ID=""
RESUME=0

# ---------------------------------------------------------------- output ----

log() { printf '%s\n' "$*"; }
err() { printf 'ERROR: %s\n' "$*" >&2; }
now_utc() { date -u +%Y-%m-%dT%H:%M:%SZ; }

journal() {
  if [ "$JOURNAL_ACTIVE" = 1 ]; then
    printf '%s %s %s\n' "$(now_utc)" "$PHASE" "$*" >>"$JOURNAL/phase.log"
  fi
}

jset() {
  # jset key value: replace or append one validated token in previous.env
  local key=$1 value=$2 file tmp
  [ "$JOURNAL_ACTIVE" = 1 ] || return 0
  case "$value" in *[!A-Za-z0-9._:-]*) err "internal: unsafe journal value for $key"; exit 11;; esac
  file=$JOURNAL/previous.env
  tmp=$file.tmp.$$
  { if [ -f "$file" ]; then grep -v "^$key=" "$file" || true; fi; printf '%s=%s\n' "$key" "$value"; } >"$tmp"
  mv -f "$tmp" "$file"
}

jget() {
  [ -f "$JOURNAL/previous.env" ] || return 0
  sed -n "s/^$1=//p" "$JOURNAL/previous.env" | head -n 1
}

usage_error() {
  err "$1"
  printf 'Usage: %s --release <40-hex-sha> [--dry-run|--rollback] [--no-auto-rollback] [--host ALIAS] [--client-pin PATH] [--acceptance-config PATH] [--state-dir PATH]\n' "$0" >&2
  exit 2
}

fail_pre() {
  err "$*"
  journal "preflight failed: $*"
  exit 3
}

is_hex() { # is_hex <value> <length>
  [ "${#1}" -eq "$2" ] && case "$1" in *[!0-9a-f]*|'') return 1;; esac
}

canonical_path() {
  case "$1" in
    /*) ;;
    *) return 1;;
  esac
  case "$1" in
    *[!A-Za-z0-9._@+/-]*|*//*|*/./*|*/../*|*/.|*/..) return 1;;
  esac
  [ "$1" = "/" ] || case "$1" in */) return 1;; esac
  return 0
}

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  else
    shasum -a 256 "$1" | cut -d' ' -f1
  fi
}

private_file() { # regular file, not a symlink, mode 0600, owned by the caller
  [ -f "$1" ] && [ ! -L "$1" ] &&
    [ -n "$(find "$1" -maxdepth 0 -type f -perm 0600 -user "$(id -un)" 2>/dev/null)" ]
}

safe_ancestors() { # no group/world-writable, non-sticky ancestor directory
  local d
  d=$(dirname "$1")
  while :; do
    if [ -n "$(find "$d" -maxdepth 0 ! -perm -1000 \( -perm -0020 -o -perm -0002 \) 2>/dev/null)" ]; then
      return 1
    fi
    [ "$d" = "/" ] && return 0
    d=$(dirname "$d")
  done
}

kv() { # kv <key>: first value of "key=value" in $R_OUT
  sed -n "s/^$1=//p" <<<"$R_OUT" | head -n 1
}

# ------------------------------------------------------ local python helper ----

read -r -d '' PY_HELPER <<'PYEOF' || true
import hashlib, json, os, re, sys, tempfile, uuid

def fail(code):
    print(code)
    sys.exit(1)

def unique(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError('duplicate')
        out[key] = value
    return out

def load(path):
    if os.path.islink(path) or not os.path.isfile(path):
        fail('not_regular_file')
    try:
        with open(path, 'rb') as stream:
            data = stream.read(1 << 20)
        return data, json.loads(data, object_pairs_hook=unique)
    except (OSError, ValueError):
        fail('invalid_json')

def atomic_write(dest, data):
    directory = os.path.dirname(dest) or '.'
    fd, tmp = tempfile.mkstemp(dir=directory, prefix='.' + os.path.basename(dest) + '.new.')
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    dfd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)

def pin_fields(obj):
    if not isinstance(obj, dict):
        fail('pin_not_object')
    rel, pkg = obj.get('release'), obj.get('package_sha256')
    if not (isinstance(rel, str) and re.fullmatch('[0-9a-f]{40}', rel)):
        fail('pin_release_invalid')
    if not (isinstance(pkg, str) and re.fullmatch('[0-9a-f]{64}', pkg)):
        fail('pin_package_invalid')
    return rel, pkg

def text(value, limit=2000):
    return (isinstance(value, str) and value.strip() != '' and len(value) <= limit
            and all(ord(c) >= 32 and ord(c) != 127 for c in value))

cmd = sys.argv[1]
if cmd == 'pin-read':
    _, obj = load(sys.argv[2])
    print(*pin_fields(obj))
elif cmd == 'pin-new':
    path, release, package, out = sys.argv[2:6]
    data, obj = load(path)
    pin_fields(obj)
    obj['release'], obj['package_sha256'] = release, package
    new = (json.dumps(obj, indent=2, ensure_ascii=False) + '\n').encode()
    with open(out, 'wb') as stream:
        stream.write(new)
    print(hashlib.sha256(new).hexdigest())
elif cmd == 'atomic-install':
    with open(sys.argv[2], 'rb') as stream:
        atomic_write(sys.argv[3], stream.read())
elif cmd == 'accept-norm':
    path, outdir = sys.argv[2:4]
    _, cfg = load(path)
    if not isinstance(cfg, dict) or set(cfg) != {'schema', 'source_id', 'snapshot_id', 'known_hit', 'no_hit'}:
        fail('acceptance_fields')
    hit, nohit = cfg['known_hit'], cfg['no_hit']
    if cfg['schema'] != 'mimir-search-deploy-acceptance-v1':
        fail('acceptance_schema')
    if not isinstance(hit, dict) or set(hit) != {'query', 'expected_path', 'expected_sha256'}:
        fail('acceptance_known_hit')
    if not isinstance(nohit, dict) or set(nohit) != {'query'}:
        fail('acceptance_no_hit')
    try:
        source_ok = isinstance(cfg['source_id'], str) and str(uuid.UUID(cfg['source_id'])) == cfg['source_id']
    except ValueError:
        source_ok = False
    if not source_ok:
        fail('acceptance_source_id')
    if not (isinstance(cfg['snapshot_id'], str) and re.fullmatch('[0-9a-f]{64}', cfg['snapshot_id'])):
        fail('acceptance_snapshot_id')
    if not (isinstance(hit['expected_sha256'], str) and re.fullmatch('[0-9a-f]{64}', hit['expected_sha256'])):
        fail('acceptance_expected_sha256')
    if not (text(hit['query']) and text(nohit['query']) and text(hit['expected_path'])):
        fail('acceptance_values')
    for name, query in (('hit', hit['query']), ('nohit', nohit['query'])):
        request = (json.dumps({'query': query}, ensure_ascii=False) + '\n').encode()
        if len(request) > 8000:
            fail('acceptance_query_too_long')
        with open(os.path.join(outdir, name + '.request.json'), 'wb') as stream:
            stream.write(request)
    import base64
    with open(os.path.join(outdir, 'hit.query.b64'), 'w') as stream:
        stream.write(base64.b64encode(hit['query'].encode()).decode())
    with open(os.path.join(outdir, 'expected.json'), 'w') as stream:
        json.dump({'path': hit['expected_path'], 'sha256': hit['expected_sha256']}, stream)
    with open(os.path.join(outdir, 'ids.env'), 'w') as stream:
        stream.write('source_id=%s\nsnapshot_id=%s\n' % (cfg['source_id'], cfg['snapshot_id']))
elif cmd == 'split':
    probe, health_out, retrieval_out = sys.argv[2:5]
    _, value = load(probe)
    if not isinstance(value, dict) or value.get('health_status') != 200 or value.get('retrieval_status') != 200:
        fail('probe_status')
    if not isinstance(value.get('health'), dict) or not isinstance(value.get('retrieval'), dict):
        fail('probe_body')
    for target, key in ((health_out, 'health'), (retrieval_out, 'retrieval')):
        with open(target, 'w') as stream:
            json.dump(value[key], stream)
elif cmd == 'client-check':
    mode, envelope, release, package, expected = sys.argv[2:7]
    _, env = load(envelope)
    _, want = load(expected)
    if not isinstance(env, dict) or env.get('schema') != 'mimir-artifact-search-v1':
        fail('envelope_schema')
    evidence = env.get('evidence')
    if not isinstance(evidence, dict):
        fail('envelope_evidence')
    if evidence.get('release') != release or evidence.get('package_sha256') != package:
        fail('provenance_mismatch')
    results = evidence.get('results')
    if not isinstance(results, list):
        fail('results_invalid')
    if mode == 'hit':
        if env.get('status') != 'ok' or not results:
            fail('known_hit_missing')
        top = results[0]
        if (not isinstance(top, dict) or top.get('path') != want['path']
                or top.get('sha256') != want['sha256']
                or top.get('verification') != 'original_sha256_matched'):
            fail('known_hit_mismatch')
    else:
        if env.get('status') != 'no_results' or results or evidence.get('abstained') is not True:
            fail('no_hit_violated')
else:
    fail('unknown_helper')
PYEOF

py_helper() { "$PY" -I -c "$PY_HELPER" "$@"; }

# ------------------------------------------------------------ remote side ----
# Remote scripts travel on stdin to `ssh <host> bash -s`. Interpolated values
# are validated tokens passed as printf-%q assignments, never built into command
# lines. The first line of each script names the operation (`# op: <name>`).

read -r -d '' REMOTE_PRELUDE <<'EOF' || true
RH=$HOME
CFG=$RH/.config/mimir-search
UD=$RH/.config/systemd/user
SHARE=$RH/.local/share/mimir-search
RELROOT=$SHARE/releases
MARKER=$SHARE/.deployed-commit
DSTATE=$RH/.local/state/mimir-search-deploy
LOCKFILE=$RH/.local/state/mimir-search-deploy.lock
ME=$(id -un)
sc() { systemctl --user "$@" </dev/null; }
sha() { if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }
owned_mode() { [ -n "$(find "$3" -maxdepth 0 -type "$2" -perm "$1" -user "$ME" 2>/dev/null)" ]; }
others_writable() { [ -n "$(find "$1" -maxdepth 0 ! -perm -1000 \( -perm -0020 -o -perm -0002 \) 2>/dev/null)" ]; }
read -r -d '' PROBE_PY <<'PYEOF' || true
import base64, json, os, socket, sys, urllib.error, urllib.request

BASE = 'http://127.0.0.1:8791'

def key():
    path = os.path.expanduser('~/.config/mimir-search/runtime.env')
    with open(path) as stream:
        for line in stream:
            name, sep, value = line.strip().partition('=')
            if sep and name == 'MIMIR_SEARCH_API_KEY':
                if len(value) >= 2 and value[0] == value[-1] and value[0] in '\'"':
                    value = value[1:-1]
                return value
    raise SystemExit('credential unavailable')

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)

def call(method, path, body=None, auth=True):
    request = urllib.request.Request(BASE + path, data=body, method=method)
    if auth:
        request.add_header('Authorization', 'Bearer ' + key())
    if body is not None:
        request.add_header('Content-Type', 'application/json')
    try:
        with OPENER.open(request, timeout=20) as response:
            return response.status, response.read(1 << 20)
    except urllib.error.HTTPError as error:
        with error:
            return error.code, error.read(1 << 20)

def parsed(raw):
    try:
        return json.loads(raw)
    except ValueError:
        return None

mode = sys.argv[1]
if mode == 'receipts':
    query = base64.b64decode(sys.argv[2]).decode()
    health_status, health = call('GET', '/health')
    retrieval_status, retrieval = call('POST', '/search', json.dumps({'query': query, 'top': 5}).encode())
    print(json.dumps({'health_status': health_status, 'health': parsed(health),
                      'retrieval_status': retrieval_status, 'retrieval': parsed(retrieval)}))
elif mode == 'unauth':
    status, _ = call('POST', '/search', json.dumps({'query': 'deploy acceptance'}).encode(), auth=False)
    print(status)
elif mode == 'port':
    probe = socket.socket()
    probe.settimeout(2)
    print('open' if probe.connect_ex(('127.0.0.1', 8791)) == 0 else 'closed')
    probe.close()
else:
    raise SystemExit('unknown mode')
PYEOF
probe() { "$RPY" -c "$PROBE_PY" "$@" </dev/null; }
listeners() {
  local cg pids pid
  cg=$(sc show -p ControlGroup --value mimir-search.service 2>/dev/null || true)
  pids=""
  if [ -n "$cg" ] && [ -r "/sys/fs/cgroup$cg/cgroup.procs" ]; then pids=$(cat "/sys/fs/cgroup$cg/cgroup.procs"); fi
  if [ -z "$pids" ]; then pids=$(sc show -p MainPID --value mimir-search.service 2>/dev/null || true); fi
  for pid in $pids; do
    [ "$pid" -gt 0 ] 2>/dev/null || continue
    ss -Hltnp </dev/null | awk -v p="pid=$pid," 'index($0, p) { print $4 }'
  done | sort -u
}
wait_inactive() {
  local i=0 busy n
  while :; do
    busy=0
    for n in "$@"; do
      case "$(sc is-active "$n" 2>/dev/null || true)" in active|activating|deactivating|reloading) busy=1;; esac
    done
    [ "$busy" -eq 0 ] && return 0
    i=$((i + 1))
    [ "$i" -le "$WAIT" ] || return 1
    sleep 1
  done
}
EOF

read -r -d '' OP_IDENTITY <<'EOF' || true
printf 'user=%s\n' "$ME"
printf 'home=%s\n' "$RH"
if [ -n "${XDG_RUNTIME_DIR:-}" ] && sc show --property=Version >/dev/null 2>&1; then echo systemd=yes; else echo systemd=no; fi
printf 'linger=%s\n' "$(loginctl show-user "$ME" -p Linger 2>/dev/null </dev/null | cut -d= -f2)"
if [ -x /usr/bin/python3 ]; then echo python=yes; else echo python=no; fi
if [ -x "$RPY" ]; then echo probe_python=yes; else echo probe_python=no; fi
EOF

read -r -d '' OP_INSTALLED <<'EOF' || true
for n in $UNITS; do
  if [ -f "$UD/$n" ] && [ ! -L "$UD/$n" ]; then printf 'unit %s %s\n' "$n" "$(sha "$UD/$n")"; else printf 'unit %s absent\n' "$n"; fi
done
svc=$UD/mimir-search.service
if [ -f "$svc" ]; then
  printf 'release=%s\n' "$(sed -n 's/^Environment=MIMIR_SEARCH_RELEASE=//p' "$svc" | head -n 1 | tr -cd 'A-Za-z0-9._-')"
  printf 'package_path=%s\n' "$(sed -n 's/^ExecStart=[^ ]* \([^ ]*\) .*/\1/p' "$svc" | head -n 1 | tr -cd 'A-Za-z0-9._/-')"
fi
EOF

read -r -d '' OP_MARKER_STATE <<'EOF' || true
if [ -f "$MARKER" ] && [ ! -L "$MARKER" ]; then
  printf 'marker=%s\n' "$(head -c 64 "$MARKER" | head -n 1 | tr -cd 'A-Za-z0-9._-')"
elif [ -e "$MARKER" ] || [ -L "$MARKER" ]; then
  echo marker=invalid
else
  echo marker=absent
fi
EOF

read -r -d '' OP_PACKAGE_STATE <<'EOF' || true
d=$RELROOT/$REL
p=$d/search.pyz
if [ ! -e "$d" ] && [ ! -L "$d" ]; then echo dir=absent
elif [ -d "$d" ] && [ ! -L "$d" ] && owned_mode 0700 d "$d"; then echo dir=ok
else echo dir=bad; fi
if [ ! -e "$p" ] && [ ! -L "$p" ]; then echo file=absent; echo sha=none
elif [ -f "$p" ] && [ ! -L "$p" ] && owned_mode 0600 f "$p"; then echo file=ok; printf 'sha=%s\n' "$(sha "$p")"
else echo file=bad; echo sha=none; fi
if [ -d "$d" ]; then
  printf 'extra=%s\n' "$(find "$d" -mindepth 1 -maxdepth 1 ! -name search.pyz ! -name '.search.pyz.*' | wc -l | tr -d ' ')"
else
  echo extra=0
fi
EOF

read -r -d '' OP_CONFIG_CHECK <<'EOF' || true
if owned_mode 0600 f "$CFG/runtime.env"; then echo runtime.env=ok; else echo runtime.env=bad; fi
if owned_mode 0600 f "$CFG/reader.json"; then echo reader.json=ok; else echo reader.json=bad; fi
if [ -f "$CFG/handoff.json" ] && [ ! -L "$CFG/handoff.json" ]; then echo handoff.json=ok; else echo handoff.json=bad; fi
if others_writable "$CFG" || others_writable "$RH/.config"; then echo dirs=bad; else echo dirs=ok; fi
EOF

read -r -d '' OP_DISK_FREE <<'EOF' || true
mkdir -p "$RELROOT"
printf 'avail_kb=%s\n' "$(df -Pk "$RELROOT" | awk 'NR == 2 { print $4 }')"
EOF

read -r -d '' OP_BASELINE <<'EOF' || true
for n in $UNITS; do printf 'active %s %s\n' "$n" "$(sc is-active "$n" 2>/dev/null || true)"; done
printf 'semantic=%s\n' "$({ journalctl --user -u mimir-search-semantic-check.service -n 50 --no-pager -o cat 2>/dev/null </dev/null | grep '^{"state"' | tail -n 1; } || true)"
printf 'listeners=%s\n' "$(listeners | tr '\n' ',')"
EOF

read -r -d '' OP_LOCK_ACQUIRE <<'EOF' || true
mkdir -p "$RH/.local/state"
if ( set -C; printf '%s\n' "$TOKEN" >"$LOCKFILE" ) 2>/dev/null; then echo acquired; else echo held; fi
EOF

read -r -d '' OP_LOCK_RELEASE <<'EOF' || true
if [ -f "$LOCKFILE" ] && [ "$(cat "$LOCKFILE")" = "$TOKEN" ]; then rm -f "${LOCKFILE:?}"; fi
echo released
EOF

read -r -d '' OP_STAGE_DIR <<'EOF' || true
mkdir -p "$RELROOT"
if [ ! -e "$RELROOT/$REL" ]; then mkdir -m 0700 "$RELROOT/$REL"; fi
if [ -f "$RELROOT/$REL/search.pyz" ] && [ ! -L "$RELROOT/$REL/search.pyz" ]; then printf 'pkg=%s\n' "$(sha "$RELROOT/$REL/search.pyz")"; else echo pkg=absent; fi
EOF

read -r -d '' OP_STAGE_COMMIT <<'EOF' || true
d=$RELROOT/$REL
chmod 0600 "$d/$TMPNAME"
if [ -e "$d/search.pyz" ]; then
  rm -f "${d:?}/${TMPNAME:?}"
else
  mv "$d/$TMPNAME" "$d/search.pyz"
fi
printf 'pkg=%s\n' "$(sha "$d/search.pyz")"
EOF

read -r -d '' OP_STAGE_UNITS_DIR <<'EOF' || true
mkdir -p "$DSTATE/staging"
mkdir -p -m 0700 "$DSTATE/staging/$STAGE"
echo ready
EOF

read -r -d '' OP_STAGE_UNITS_HASH <<'EOF' || true
for n in $UNITS; do
  if [ -f "$DSTATE/staging/$STAGE/$n" ]; then printf '%s %s\n' "$(sha "$DSTATE/staging/$STAGE/$n")" "$n"; fi
done
EOF

read -r -d '' OP_MARKER_REMOVE <<'EOF' || true
rm -f "${MARKER:?}"
if [ ! -e "$MARKER" ] && [ ! -L "$MARKER" ]; then echo marker-absent; else echo marker-present; fi
EOF

read -r -d '' OP_STOP_UNITS <<'EOF' || true
sc stop mimir-search-refresh.timer
sc stop mimir-search-semantic-check.timer
sc stop mimir-search.service
wait_inactive mimir-search-refresh.timer mimir-search-semantic-check.timer mimir-search.service mimir-search-refresh.service mimir-search-semantic-check.service || { echo "units still active" >&2; exit 1; }
echo stopped
EOF

read -r -d '' OP_INSTALL_UNITS <<'EOF' || true
mkdir -p "$UD"
SRC=$DSTATE/staging/$STAGE
printf '%s\n' "$LINES" | while IFS=' ' read -r want name; do
  [ -n "$name" ] || continue
  case "$name" in
    mimir-search.service|mimir-search-refresh.service|mimir-search-refresh.timer|mimir-search-semantic-check.service|mimir-search-semantic-check.timer) ;;
    *) echo "unexpected unit name" >&2; exit 1;;
  esac
  if [ "$want" = absent ]; then
    sc disable "$name" >/dev/null 2>&1 || true
    rm -f "${UD:?}/${name:?}"
    echo "removed $name"
  else
    [ "$(sha "$SRC/$name")" = "$want" ] || { echo "staged unit hash mismatch" >&2; exit 1; }
    tmp=$UD/$name.new.$$
    cp "$SRC/$name" "$tmp"
    chmod 0644 "$tmp"
    mv -f "$tmp" "$UD/$name"
    [ "$(sha "$UD/$name")" = "$want" ] || { echo "installed unit hash mismatch" >&2; exit 1; }
    echo "installed $name"
  fi
done
sc daemon-reload
EOF

read -r -d '' OP_REFRESH_START <<'EOF' || true
mkdir -p "$DSTATE"
ref=$DSTATE/.handoff.ref.$$
trap 'rm -f "${ref:?}"' EXIT
touch -r "$CFG/handoff.json" "$ref"
sc start mimir-search-refresh.service
res=$(sc show -p Result --value mimir-search-refresh.service 2>/dev/null || true)
[ "$res" = success ] || { echo "refresh result not success" >&2; exit 1; }
[ -n "$(find "$CFG/handoff.json" -newer "$ref" 2>/dev/null)" ] || { echo "handoff.json mtime did not advance" >&2; exit 1; }
echo refreshed
EOF

read -r -d '' OP_ACTIVATE <<'EOF' || true
sc enable mimir-search.service mimir-search-refresh.timer mimir-search-semantic-check.timer >/dev/null 2>&1
sc restart mimir-search.service
sc start mimir-search-refresh.timer mimir-search-semantic-check.timer
i=0
while :; do
  if [ "$(sc is-active mimir-search.service 2>/dev/null || true)" = active ] && [ "$(probe port)" = open ]; then break; fi
  i=$((i + 1))
  [ "$i" -le "$WAIT" ] || { echo "service not answering on loopback" >&2; exit 1; }
  sleep 1
done
echo active
EOF

read -r -d '' OP_PROBE_RECEIPTS <<'EOF' || true
probe receipts "$QB64"
EOF

read -r -d '' OP_PROBE_401 <<'EOF' || true
probe unauth
EOF

read -r -d '' OP_PROBE_LISTENERS <<'EOF' || true
listeners
EOF

read -r -d '' OP_SEMANTIC_RUN <<'EOF' || true
start_rc=0
sc start mimir-search-semantic-check.service >/dev/null 2>&1 || start_rc=$?
line=$({ journalctl --user -u mimir-search-semantic-check.service -n 50 --no-pager -o cat 2>/dev/null </dev/null | grep '^{"state"' | tail -n 1; } || true)
printf 'rc=%s\nresult=%s\n' "$start_rc" "$line"
EOF

read -r -d '' OP_SEMANTIC_READ <<'EOF' || true
line=$({ journalctl --user -u mimir-search-semantic-check.service -n 50 --no-pager -o cat 2>/dev/null </dev/null | grep '^{"state"' | tail -n 1; } || true)
printf 'rc=0\nresult=%s\n' "$line"
EOF

read -r -d '' OP_MARKER_WRITE <<'EOF' || true
mkdir -p "$SHARE"
tmp=$SHARE/.deployed-commit.new.$$
trap 'rm -f "${tmp:?}"' EXIT
printf '%s\n' "$VALUE" >"$tmp"
chmod 0600 "$tmp"
mv -f "$tmp" "$MARKER"
trap - EXIT
printf 'marker=%s\n' "$(head -n 1 "$MARKER")"
printf 'bytes=%s\n' "$(wc -c <"$MARKER" | tr -d ' ')"
EOF

read -r -d '' OP_RECEIPT_WRITE <<'EOF' || true
tmp=$SHARE/.deployed-receipt.json.new.$$
trap 'rm -f "${tmp:?}"' EXIT
printf '%s\n' "$BODY" >"$tmp"
chmod 0600 "$tmp"
mv -f "$tmp" "$SHARE/.deployed-receipt.json"
trap - EXIT
echo written
EOF

# rsh <op> <body> [NAME=value ...]: run one remote script; result in R_OUT/R_RC.
rsh() {
  local op=$1 body=$2 assign line script
  shift 2
  if [ "$DRY_RUN" = 1 ]; then
    case "$RO_OPS" in *" $op "*) ;; *) err "internal: mutating remote operation $op refused in --dry-run"; exit 11;; esac
  fi
  script="# op: $op"$'\n'"set -euo pipefail"$'\n'"umask 077"$'\n'
  for assign in "RPY=$RPY" "UNITS=$UNITS_WORDS" "WAIT=$WAIT_SECONDS" "$@"; do
    printf -v line '%s=%q\n' "${assign%%=*}" "${assign#*=}"
    script+=$line
  done
  script+=$REMOTE_PRELUDE$'\n'$body$'\n'
  R_OUT=$("${SSH_CMD[@]}" "$HOST" bash -s <<<"$script") && R_RC=0 || R_RC=$?
  return "$R_RC"
}

# rsh_ro: read-only operations are retried once on a transport error (rc 255).
rsh_ro() {
  rsh "$@" && return 0
  if [ "$R_RC" -eq 255 ]; then
    sleep 1
    rsh "$@" && return 0
  fi
  return "$R_RC"
}

scp_push() { # scp_push <remote-dir> <local-file>...
  local dest=$1
  shift
  if [ "$DRY_RUN" = 1 ]; then err "internal: upload refused in --dry-run"; exit 11; fi
  "${SCP_CMD[@]}" "$@" "$HOST:$dest/"
}

scp_pull() { # scp_pull <remote-file> <local-path>
  "${SCP_CMD[@]}" "$HOST:$1" "$2" || { sleep 1; "${SCP_CMD[@]}" "$HOST:$1" "$2"; }
}

# --------------------------------------------------------------- arguments ----

while [ $# -gt 0 ]; do
  case "$1" in
    --release) [ $# -ge 2 ] || usage_error "--release needs a value"; RELEASE=$2; shift 2;;
    --dry-run) DRY_RUN=1; shift;;
    --rollback) ROLLBACK=1; shift;;
    --no-auto-rollback) AUTO_ROLLBACK=0; shift;;
    --host) [ $# -ge 2 ] || usage_error "--host needs a value"; HOST=$2; shift 2;;
    --client-pin) [ $# -ge 2 ] || usage_error "--client-pin needs a value"; CLIENT_PIN=$2; shift 2;;
    --acceptance-config) [ $# -ge 2 ] || usage_error "--acceptance-config needs a value"; ACCEPT_CFG=$2; shift 2;;
    --state-dir) [ $# -ge 2 ] || usage_error "--state-dir needs a value"; STATE_DIR=$2; shift 2;;
    *) usage_error "unknown argument: $1";;
  esac
done

[ -n "$RELEASE" ] || usage_error "--release is required"
is_hex "$RELEASE" 40 || usage_error "--release must be a full 40-character lowercase hex commit SHA"
[ "$DRY_RUN" = 0 ] || [ "$ROLLBACK" = 0 ] || usage_error "--dry-run cannot be combined with --rollback"
[[ "$HOST" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || usage_error "--host must be an ssh alias"
[ -n "${HOME:-}" ] && canonical_path "$HOME" || usage_error "HOME must be a canonical absolute path"
[ -n "$CLIENT_PIN" ] || CLIENT_PIN=$HOME/.config/mimir-search/client.json
[ -n "$ACCEPT_CFG" ] || ACCEPT_CFG=$HOME/.config/mimir-search/deploy-acceptance.json
[ -n "$STATE_DIR" ] || STATE_DIR=$HOME/.local/state/mimir-search-deploy
for _path in "$CLIENT_PIN" "$ACCEPT_CFG" "$STATE_DIR"; do
  canonical_path "$_path" || usage_error "path is not canonical absolute: $_path"
done
[[ "$WAIT_SECONDS" =~ ^[0-9]{1,4}$ ]] || usage_error "MIMIR_DEPLOY_WAIT_SECONDS must be a number"
[[ "$WARN_RETRY_SECONDS" =~ ^[0-9]{1,4}$ ]] || usage_error "MIMIR_DEPLOY_WARN_RETRY_SECONDS must be a number"
[[ "$EXPECTED_USER" =~ ^[a-z_][a-z0-9_-]*$ ]] || usage_error "MIMIR_DEPLOY_REMOTE_USER must be an account name"
canonical_path "$EXPECTED_HOME" || usage_error "MIMIR_DEPLOY_EXPECTED_HOME must be a canonical absolute path"
canonical_path "$RPY" || usage_error "MIMIR_DEPLOY_REMOTE_PYTHON must be a canonical absolute path"

# ------------------------------------------------------ exits and cleanup ----

release_locks() {
  if [ "$REMOTE_LOCK_HELD" = 1 ]; then
    REMOTE_LOCK_HELD=0
    if ! rsh lock-release "$OP_LOCK_RELEASE" "TOKEN=$REMOTE_LOCK_TOKEN"; then
      printf 'WARNING: could not release the remote deploy lock on %s; it is a file under the reader state directory and may need manual removal.\n' "$HOST" >&2
    fi
  fi
  if [ -n "$LOCAL_LOCK" ]; then
    exec 9>&-
    LOCAL_LOCK=""
  fi
}

cleanup() {
  local rc=$?
  trap - EXIT INT TERM HUP
  release_locks
  case "$TMP" in /*) rm -rf "${TMP:?}";; esac
  exit "$rc"
}

footer_failure() { # footer_failure <marker-state-text>
  [ "$JOURNAL_ACTIVE" != 1 ] || printf 'Journal: %s\n' "$JOURNAL" >&2
  printf 'Marker: %s\n' "$1" >&2
}

# fail_active: a failure after the marker invalidation began.
fail_active() {
  err "$1"
  journal "FAILED: $1"
  if [ "$IN_ROLLBACK" = 1 ]; then
    jset status rollback-failed
    footer_failure "absent (rollback did not complete)"
    exit 11
  fi
  if [ "$AUTO_ROLLBACK" != 1 ]; then
    jset status failed-not-rolled-back
    err "automatic rollback disabled; leaving the system as-is for diagnosis"
    footer_failure "absent"
    exit 11
  fi
  log "==> Failure after mutation began; rolling back automatically"
  if run_rollback; then
    jset status rolled-back
    footer_failure "restored to the previous value ($PREV_MARKER)"
    exit 10
  fi
  jset status rollback-failed
  err "rollback could not be proved; target state is unknown"
  footer_failure "absent (rollback did not complete)"
  exit 11
}

on_signal() {
  trap '' INT TERM HUP
  journal "interrupted by SIG$1"
  err "interrupted by SIG$1"
  if [ "$COMMITTED" = 1 ]; then exit 0; fi
  if [ "$MUTATED" = 1 ]; then fail_active "interrupted by SIG$1"; fi
  exit 3
}

trap cleanup EXIT
trap 'on_signal INT' INT
trap 'on_signal TERM' TERM
trap 'on_signal HUP' HUP

# ------------------------------------------------------ A1 source identity ----

# check_source <network:1|0> [failure-handler]. The handler defaults to fail_pre; the
# final re-check after the switch passes fail_active so a changed tree is handled as
# a post-mutation failure.
check_source() {
  local fail=${2:-fail_pre} top head status tip fetched
  top=$(git rev-parse --show-toplevel 2>/dev/null) || "$fail" "not inside a git worktree"
  head=$(git rev-parse HEAD 2>/dev/null) || "$fail" "cannot resolve HEAD"
  log "Expected source: $(pwd -P) @ $RELEASE"
  log "Actual source: $top @ $head"
  [ "$(pwd -P)" = "$top" ] || "$fail" "run from the worktree root"
  [ "$head" = "$RELEASE" ] || "$fail" "HEAD does not equal --release"
  status=$(git status --porcelain=v1 --untracked-files=normal) || "$fail" "git status failed"
  [ -z "$status" ] || "$fail" "worktree is not clean; commit or remove every change"
  [ "$1" = 1 ] || return 0
  tip=$(git ls-remote origin refs/heads/main 2>/dev/null | awk '{ print $1 }') || tip=""
  is_hex "$tip" 40 || fail_pre "origin/main is unreachable; cannot prove the release is merged"
  # Ancestry is proved in a scratch repository under $TMP so that neither a dry run
  # nor a real run writes FETCH_HEAD, refs or objects into the worktree's .git. The
  # release is present there only if it is part of origin/main's history.
  git init --quiet --bare "$TMP/ancestry.git" || fail_pre "could not create the scratch repository"
  git -C "$TMP/ancestry.git" fetch --quiet --no-tags "$(git config --get remote.origin.url)" refs/heads/main 2>/dev/null ||
    fail_pre "could not fetch origin/main"
  fetched=$(git -C "$TMP/ancestry.git" rev-parse FETCH_HEAD)
  [ "$fetched" = "$tip" ] || fail_pre "origin/main moved while it was being checked; retry"
  git -C "$TMP/ancestry.git" merge-base --is-ancestor "$RELEASE" "$tip" 2>/dev/null ||
    fail_pre "--release is not reachable from origin/main"
}

# ------------------------------------------------------------- acceptance ----

sem_field() { # sem_field <field> from "result=<json>" in $R_OUT
  kv result | sed -n "s/.*\"$1\": *\"\\([a-z]*\\)\".*/\\1/p" | head -n 1
}

semantic_accept() { # semantic_accept <run|read>
  local attempt=1 rc state op body
  if [ "$1" = run ]; then op=semantic-run; body=$OP_SEMANTIC_RUN; else op=semantic-read; body=$OP_SEMANTIC_READ; fi
  while :; do
    rsh_ro "$op" "$body" || { err "semantic check could not be run"; return 1; }
    rc=$(kv rc)
    state=$(sem_field state)
    if [ "$rc" = 0 ] && [ "$state" = pass ]; then return 0; fi
    if [ "$rc" = 0 ] && [ "$state" = warn ] && [ "$attempt" -lt 2 ] && [ "$1" = run ]; then
      attempt=2
      log "  semantic check warned; retrying once"
      sleep "$WARN_RETRY_SECONDS"
      continue
    fi
    err "semantic check did not pass (state=${state:-unknown})"
    return 1
  done
}

units_match_set() { # units_match_set <expected "sha name" file>: compare $R_OUT unit lines
  local live want
  live=$(sed -n 's/^unit \([^ ]*\) \([0-9a-f]\{64\}\)$/\2 \1/p' <<<"$R_OUT" | sort)
  want=$(sort "$1")
  [ "$live" = "$want" ]
}

# accept_server <label> <release> <package-sha> <package-file> <unit-set-file> <mode:full|readonly>
# Section 7 items 1-5. "readonly" reads the last semantic result instead of starting the check.
accept_server() {
  local label=$1 rel=$2 psha=$3 pfile=$4 unitset=$5 mode=$6 dir
  dir=$TMP/receipts-$label
  mkdir -p "$dir"
  log "  [1] package verify with fresh health and retrieval receipts"
  rsh_ro probe-receipts "$OP_PROBE_RECEIPTS" "QB64=$(cat "$TMP/accept/hit.query.b64")" || { err "receipt capture failed"; return 1; }
  printf '%s\n' "$R_OUT" >"$dir/probe.json"
  py_helper split "$dir/probe.json" "$dir/health.json" "$dir/retrieval.json" >/dev/null || { err "health or retrieval receipt was not a success"; return 1; }
  "$PY" -m tools.artifact_search.package_runtime verify --release "$rel" --package "$pfile" --sha256 "$psha" \
    --health-json "$dir/health.json" --retrieval-json "$dir/retrieval.json" \
    --snapshot-id "$SNAPSHOT_ID" --source-id "$SOURCE_ID" >"$dir/verify.out" 2>&1 || { err "package verifier rejected the receipts"; return 1; }
  grep -q '"deployment_accepted": true' "$dir/verify.out" || { err "package verifier did not accept the deployment"; return 1; }
  if [ "$JOURNAL_ACTIVE" = 1 ]; then
    mkdir -p "$JOURNAL/receipts"
    cp "$dir/health.json" "$JOURNAL/receipts/$label-health.json"
    cp "$dir/retrieval.json" "$JOURNAL/receipts/$label-retrieval.json"
    cp "$dir/verify.out" "$JOURNAL/receipts/$label-verify.json"
  fi
  log "  [2] unit and package integrity on the target"
  rsh_ro installed "$OP_INSTALLED" || { err "could not read installed units"; return 1; }
  units_match_set "$unitset" || { err "installed unit bytes differ from the expected set"; return 1; }
  [ "$(kv release)" = "$rel" ] || { err "installed service does not reference the expected release"; return 1; }
  rsh_ro package-state "$OP_PACKAGE_STATE" "REL=$rel" || { err "could not read package state"; return 1; }
  [ "$(kv dir)" = ok ] && [ "$(kv file)" = ok ] && [ "$(kv sha)" = "$psha" ] ||
    { err "release package differs from the expected hash or modes"; return 1; }
  log "  [3] unauthenticated denial"
  rsh_ro probe-401 "$OP_PROBE_401" || { err "unauthenticated probe failed"; return 1; }
  [ "$R_OUT" = 401 ] || { err "unauthenticated request was not answered with 401"; return 1; }
  log "  [4] single loopback listener"
  rsh_ro probe-listeners "$OP_PROBE_LISTENERS" || { err "listener probe failed"; return 1; }
  [ "$R_OUT" = "127.0.0.1:8791" ] || { err "service listeners are not exactly 127.0.0.1:8791"; return 1; }
  log "  [5] semantic check"
  if [ "$mode" = full ]; then semantic_accept run; else semantic_accept read; fi || return 1
  journal "server acceptance ($label) passed"
}

run_client_query() { # run_client_query <hit|nohit> <release> <package-sha>
  local out=$TMP/client-$1.json
  "${CLIENT_CMD[@]}" --tool --config "$CLIENT_PIN" <"$TMP/accept/$1.request.json" >"$out" 2>/dev/null || { err "client query ($1) failed"; return 1; }
  py_helper client-check "$1" "$out" "$2" "$3" "$TMP/accept/expected.json" >"$TMP/client-$1.verdict" 2>&1 ||
    { err "client acceptance ($1) failed: $(head -c 80 "$TMP/client-$1.verdict")"; return 1; }
}

accept_client() { # accept_client <release> <package-sha>
  log "  [6] client known-hit"
  run_client_query hit "$1" "$2" || return 1
  log "  [7] client no-hit"
  run_client_query nohit "$1" "$2" || return 1
  journal "client acceptance passed"
}

# ------------------------------------------------------- journal and locks ----

JOURNAL=$STATE_DIR/$RELEASE

# The local lock is a process-held advisory lock (flock on an inherited descriptor).
# The kernel drops it when this process exits, so there is no stale-lock recovery,
# no pid file race and nothing to remove. perl ships with macOS and Linux and only
# applies flock to descriptor 9; the lock lives on in the open file description.
acquire_local_lock() {
  local lock=$STATE_DIR/deploy.lock rc=0
  command -v perl >/dev/null 2>&1 || fail_pre "perl is required for the local deploy lock"
  mkdir -p "$STATE_DIR"
  chmod 0700 "$STATE_DIR"
  exec 9>>"$lock"
  perl -e 'use Fcntl qw(:flock); open(my $fh, "<&=", 9) or exit 2; flock($fh, LOCK_EX | LOCK_NB) or exit 1;' || rc=$?
  if [ "$rc" -ne 0 ]; then
    exec 9>&-
    if [ "$rc" -eq 1 ]; then fail_pre "another deploy holds $lock"; fi
    fail_pre "could not take the local deploy lock ($lock)"
  fi
  LOCAL_LOCK=$lock
}

activate_journal() {
  mkdir -p "$JOURNAL"
  chmod 0700 "$JOURNAL"
  JOURNAL_ACTIVE=1
  journal "journal active for $RELEASE"
}

acquire_remote_lock() {
  local host_part
  host_part=$(hostname -s 2>/dev/null | tr -cd 'A-Za-z0-9-' || true)
  REMOTE_LOCK_TOKEN="${host_part:-local}.$$.$RANDOM"
  rsh lock-acquire "$OP_LOCK_ACQUIRE" "TOKEN=$REMOTE_LOCK_TOKEN" || fail_pre "could not take the remote deploy lock on $HOST"
  case "$R_OUT" in
    acquired) REMOTE_LOCK_HELD=1;;
    held) fail_pre "the remote deploy lock is held on $HOST (~/.local/state/mimir-search-deploy.lock); another deploy may be running";;
    *) fail_pre "unexpected remote lock state";;
  esac
}

# ------------------------------------------------------------- phase A ----

check_toolchain() {
  local tool
  for tool in "${SSH_CMD[0]}" "${SCP_CMD[0]}" "$PY" git cmp diff find; do
    command -v "$tool" >/dev/null 2>&1 || fail_pre "missing local tool: $tool"
  done
  command -v sha256sum >/dev/null 2>&1 || command -v shasum >/dev/null 2>&1 || fail_pre "missing local tool: sha256sum or shasum"
  command -v "${CLIENT_CMD[0]}" >/dev/null 2>&1 || [ -x "${CLIENT_CMD[0]}" ] || fail_pre "client launcher not found: ${CLIENT_CMD[0]}"
  "$PY" -m tools.artifact_search.package_runtime build --help >/dev/null 2>&1 || fail_pre "package_runtime is not runnable from this worktree"
  "$PY" -m tools.artifact_search.service_units --help >/dev/null 2>&1 || fail_pre "service_units is not runnable from this worktree"
}

check_remote_identity() {
  rsh_ro identity "$OP_IDENTITY" || fail_pre "cannot reach $HOST over ssh"
  [ "$(kv user)" = "$EXPECTED_USER" ] || fail_pre "remote user is not $EXPECTED_USER"
  REMOTE_HOME=$(kv home)
  canonical_path "$REMOTE_HOME" && [ "$REMOTE_HOME" = "$EXPECTED_HOME" ] || fail_pre "remote home is not the registered $EXPECTED_HOME"
  [ "$(kv systemd)" = yes ] || fail_pre "systemctl --user is not reachable (no XDG_RUNTIME_DIR or bus)"
  [ "$(kv linger)" = yes ] || fail_pre "lingering is not enabled for $EXPECTED_USER"
  [ "$(kv python)" = yes ] || fail_pre "/usr/bin/python3 is missing on the reader"
  [ "$(kv probe_python)" = yes ] || fail_pre "the probe interpreter is missing on the reader"
}

check_pin() { # sets PIN_REL, PIN_PKG, PIN_SHA_A6
  local values
  private_file "$CLIENT_PIN" || fail_pre "client pin must be a regular, non-symlink file with mode 0600: $CLIENT_PIN"
  safe_ancestors "$CLIENT_PIN" || fail_pre "a parent directory of the client pin is group- or world-writable"
  values=$(py_helper pin-read "$CLIENT_PIN") || fail_pre "client pin is not a valid pin object ($values)"
  PIN_REL=${values%% *}
  PIN_PKG=${values##* }
  PIN_SHA_A6=$(sha256_file "$CLIENT_PIN")
}

check_acceptance_config() {
  local out
  private_file "$ACCEPT_CFG" || fail_pre "acceptance config must be a regular, non-symlink file with mode 0600: $ACCEPT_CFG"
  safe_ancestors "$ACCEPT_CFG" || fail_pre "a parent directory of the acceptance config is group- or world-writable"
  case "$ACCEPT_CFG" in "$(pwd -P)"/*) fail_pre "the acceptance config must live outside the repository";; esac
  mkdir -p "$TMP/accept"
  out=$(py_helper accept-norm "$ACCEPT_CFG" "$TMP/accept") || fail_pre "acceptance config is invalid ($out)"
  SOURCE_ID=$(sed -n 's/^source_id=//p' "$TMP/accept/ids.env")
  SNAPSHOT_ID=$(sed -n 's/^snapshot_id=//p' "$TMP/accept/ids.env")
}

read_installed_state() { # sets LIVE_RELEASE, LIVE_PKG_SHA, LIVE_MARKER; writes $TMP/units-live.sha256
  local n path
  rsh_ro installed "$OP_INSTALLED" || fail_pre "cannot read installed units"
  for n in "${UNIT_NAMES[@]}"; do
    grep -q "^unit $n [0-9a-f]\{64\}\$" <<<"$R_OUT" || fail_pre "$n is not installed on $HOST; this script only upgrades (first install stays manual)"
  done
  sed -n 's/^unit \([^ ]*\) \([0-9a-f]\{64\}\)$/\2 \1/p' <<<"$R_OUT" | sort >"$TMP/units-live.sha256"
  LIVE_RELEASE=$(kv release)
  path=$(kv package_path)
  is_hex "$LIVE_RELEASE" 40 || fail_pre "cannot parse the installed release from mimir-search.service"
  [ "$path" = "$REMOTE_HOME/.local/share/mimir-search/releases/$LIVE_RELEASE/search.pyz" ] ||
    fail_pre "installed package path does not match the installed release"
  rsh_ro package-state "$OP_PACKAGE_STATE" "REL=$LIVE_RELEASE" || fail_pre "cannot read the installed package state"
  [ "$(kv dir)" = ok ] && [ "$(kv file)" = ok ] || fail_pre "installed release package is missing or has wrong modes/owner"
  LIVE_PKG_SHA=$(kv sha)
  is_hex "$LIVE_PKG_SHA" 64 || fail_pre "cannot hash the installed package"
  rsh_ro marker-state "$OP_MARKER_STATE" || fail_pre "cannot read the deployed marker"
  LIVE_MARKER=$(kv marker)
  is_hex "$LIVE_MARKER" 40 || [ "$LIVE_MARKER" = absent ] || fail_pre "the deployed marker is malformed; reconcile it by hand"
}

check_remote_config() {
  rsh_ro config-check "$OP_CONFIG_CHECK" || fail_pre "cannot check the private configuration"
  [ "$(kv runtime.env)" = ok ] || fail_pre "runtime.env is missing or not mode 0600 (contents are never read here)"
  [ "$(kv reader.json)" = ok ] || fail_pre "reader.json is missing or not mode 0600"
  [ "$(kv handoff.json)" = ok ] || fail_pre "handoff.json is missing"
  [ "$(kv dirs)" = ok ] || fail_pre "a configuration parent directory is group- or world-writable"
}

# ------------------------------------------------------------- phase B ----

build_and_render() {
  local n names expected
  mkdir -m 0700 "$TMP/build-1" "$TMP/build-2"
  for n in 1 2; do
    "$PY" -m tools.artifact_search.package_runtime build --release "$RELEASE" --repo "$PWD" \
      --package "$TMP/build-$n/search.pyz" >/dev/null || fail_pre "package build $n failed"
  done
  cmp -s "$TMP/build-1/search.pyz" "$TMP/build-2/search.pyz" || fail_pre "the two package builds are not byte-identical (not reproducible)"
  PKG_SHA=$(sha256_file "$TMP/build-1/search.pyz")
  "$PY" -m tools.artifact_search.package_runtime verify --release "$RELEASE" \
    --package "$TMP/build-1/search.pyz" --sha256 "$PKG_SHA" >/dev/null || fail_pre "package verifier rejected the built package"
  "$PY" -m tools.artifact_search.service_units --role reader --home "$REMOTE_HOME" --release "$RELEASE" \
    --output-directory "$TMP/units" || fail_pre "unit renderer failed"
  names=$(ls -A "$TMP/units" | sort | tr '\n' ' ')
  expected=$(printf '%s\n' "${UNIT_NAMES[@]}" | sort | tr '\n' ' ')
  [ "$names" = "$expected" ] || fail_pre "renderer produced an unexpected file set: $names"
  for n in "${UNIT_NAMES[@]}"; do
    printf '%s %s\n' "$(sha256_file "$TMP/units/$n")" "$n"
  done | sort >"$TMP/units-new.sha256"
}

fetch_installed_units() { # fetch_installed_units <dir>
  local n
  mkdir -p "$1"
  for n in "${UNIT_NAMES[@]}"; do
    scp_pull "$REMOTE_HOME/.config/systemd/user/$n" "$1/$n" || return 1
  done
}

print_unit_diff() {
  local n
  fetch_installed_units "$TMP/installed-units" || fail_pre "could not read the installed units for the diff"
  log "== Unit diff (installed -> new) =="
  for n in "${UNIT_NAMES[@]}"; do
    diff -u -L "installed/$n" -L "new/$n" "$TMP/installed-units/$n" "$TMP/units/$n" || true
  done
}

print_rollback_recipe() {
  cat <<EOF
Rollback recipe (automatic on failure unless --no-auto-rollback; or run
  ./scripts/deploy-search-reader.sh --release $RELEASE --rollback):
  1. stop mimir-search-refresh.timer, mimir-search-semantic-check.timer, mimir-search.service
  2. restore the previous unit files from the journal (hash-checked before and after);
     remove any unit the new render introduced
  3. restore the client pin from the journal only if it still equals what this deploy wrote
  4. daemon-reload; start mimir-search-refresh.service (must succeed); start the service and timers
  5. re-run acceptance against the previous release and package hash
  6. restore the previous marker value (or leave it absent)
  7. new and previous release directories are kept; journal: $JOURNAL
EOF
}

print_plan() {
  cat <<EOF
== Plan ==
Release:            $RELEASE
Target:             $HOST ($REMOTE_HOME)
Previous release:   $PREV_RELEASE (package $PREV_PKG_SHA)
Previous marker:    $PREV_MARKER
Package sha256:     $PKG_SHA (two builds byte-identical, verifier accepted)
Client pin:         release $PIN_REL -> $RELEASE
                    package $PIN_PKG -> $PKG_SHA
Remote commands (ssh $HOST bash -s, scripts on stdin; scp for files):
  C  stage       releases/$RELEASE/search.pyz (0700 dir, 0600 file), staged units, journal backup
  D1 remove      .deployed-commit
  D2 stop        mimir-search-refresh.timer, mimir-search-semantic-check.timer, mimir-search.service
  D3 install     5 user units atomically, systemctl --user daemon-reload
  D4 start       mimir-search-refresh.service (oneshot, must succeed)
  D5 enable/start mimir-search.service and both timers; wait for loopback 8791
  E  accept      verify receipts, unit/package integrity, 401, single listener, semantic check
  F  pin         update only release and package_sha256 in $CLIENT_PIN, then client hit/no-hit
  G  marker      write .deployed-commit last (read-back checked)
EOF
  print_rollback_recipe
}

# --------------------------------------------------- previous-state choice ----

load_journal_previous() {
  PREV_RELEASE=$(jget prev_release)
  PREV_PKG_SHA=$(jget prev_package_sha256)
  PREV_MARKER=$(jget prev_marker)
  PIN_PREV_SHA=$(jget pin_prev_sha256)
  PIN_WRITTEN_SHA=$(jget pin_written_sha256)
  is_hex "$PREV_RELEASE" 40 && is_hex "$PREV_PKG_SHA" 64 || return 1
  is_hex "$PREV_MARKER" 40 || [ "$PREV_MARKER" = absent ] || return 1
  [ -f "$JOURNAL/units-prev.sha256" ] && [ -d "$JOURNAL/units-prev" ] || return 1
}

decide_previous() {
  local jprev jstatus terminal=0
  RESUME=0
  if [ -f "$JOURNAL/previous.env" ]; then
    jprev=$(jget prev_release)
    jstatus=$(jget status)
    case "$jstatus" in deployed|rolled-back) terminal=1;; esac
  else
    jprev=""
    jstatus=""
  fi
  if [ -n "$jprev" ] && [ "$terminal" = 0 ]; then
    # An unfinished deployment's journal is authoritative: its snapshot of the
    # original units, pin and marker is never replaced, even when a partial
    # rollback left a mix of old and new units that already reference jprev.
    [ "$LIVE_RELEASE" = "$RELEASE" ] || [ "$LIVE_RELEASE" = "$jprev" ] ||
      fail_pre "live release $LIVE_RELEASE matches neither $RELEASE nor the journal's previous release; reconcile by hand"
    load_journal_previous || fail_pre "the journal for $RELEASE is incomplete; reconcile by hand"
    RESUME=1
  elif [ "$LIVE_RELEASE" = "$RELEASE" ]; then
    [ -n "$jprev" ] || fail_pre "the reader already runs $RELEASE but no journal records its previous state; reconcile by hand"
    load_journal_previous || fail_pre "the journal for $RELEASE is incomplete; reconcile by hand"
    RESUME=1
  else
    PREV_RELEASE=$LIVE_RELEASE
    PREV_PKG_SHA=$LIVE_PKG_SHA
    PREV_MARKER=$LIVE_MARKER
  fi
}

# ---------------------------------------------------------- C and D steps ----

stage_package() {
  local tmpname=.search.pyz.$$
  rsh stage-dir "$OP_STAGE_DIR" "REL=$RELEASE" || fail_pre "could not create the release directory"
  case "$(kv pkg)" in
    "$PKG_SHA") log "  package already staged with the same hash";;
    absent)
      "${SCP_CMD[@]}" "$TMP/build-1/search.pyz" "$HOST:$REMOTE_HOME/.local/share/mimir-search/releases/$RELEASE/$tmpname" ||
        fail_pre "package upload failed"
      rsh stage-commit "$OP_STAGE_COMMIT" "REL=$RELEASE" "TMPNAME=$tmpname" || fail_pre "package commit failed"
      [ "$(kv pkg)" = "$PKG_SHA" ] || fail_pre "staged package hash differs from the build"
      ;;
    *) fail_pre "releases/$RELEASE/search.pyz already exists with different content; release directories are immutable";;
  esac
}

stage_units() {
  local n files=() staged
  rsh stage-units-dir "$OP_STAGE_UNITS_DIR" "STAGE=$RELEASE" || fail_pre "could not create the unit staging directory"
  for n in "${UNIT_NAMES[@]}"; do files+=("$TMP/units/$n"); done
  scp_push "$REMOTE_HOME/.local/state/mimir-search-deploy/staging/$RELEASE" "${files[@]}" || fail_pre "unit upload failed"
  rsh stage-units-hash "$OP_STAGE_UNITS_HASH" "STAGE=$RELEASE" || fail_pre "could not hash the staged units"
  staged=$(sort <<<"$R_OUT")
  [ "$staged" = "$(sort "$TMP/units-new.sha256")" ] || fail_pre "staged unit hashes differ from the rendered set"
}

backup_previous_state() {
  local n remote_hash local_hash aside
  rsh_ro installed "$OP_INSTALLED" || fail_pre "cannot re-read installed units for the backup"
  if [ -d "$JOURNAL/units-prev" ] || [ -f "$JOURNAL/previous.env" ]; then
    aside=$JOURNAL/prior-$(date +%s)
    mkdir -p "$aside"
    for n in units-prev units-prev.sha256 previous.env client.json.prev units-new.sha256 package.sha256; do
      if [ -e "$JOURNAL/$n" ]; then mv "$JOURNAL/$n" "$aside/$n"; fi
    done
  fi
  mkdir -p "$JOURNAL/units-prev"
  : >"$JOURNAL/units-prev.sha256"
  for n in "${UNIT_NAMES[@]}"; do
    remote_hash=$(sed -n "s/^unit $n //p" <<<"$R_OUT" | head -n 1)
    if [ "$remote_hash" = absent ] || [ -z "$remote_hash" ]; then
      printf 'absent %s\n' "$n" >>"$JOURNAL/units-prev.sha256"
      continue
    fi
    scp_pull "$REMOTE_HOME/.config/systemd/user/$n" "$JOURNAL/units-prev/$n" || fail_pre "could not copy $n into the journal"
    local_hash=$(sha256_file "$JOURNAL/units-prev/$n")
    [ "$local_hash" = "$remote_hash" ] || fail_pre "journal copy of $n differs from the installed unit"
    printf '%s %s\n' "$local_hash" "$n" >>"$JOURNAL/units-prev.sha256"
  done
  rsh_ro marker-state "$OP_MARKER_STATE" || fail_pre "cannot re-read the marker"
  LIVE_MARKER=$(kv marker)
  is_hex "$LIVE_MARKER" 40 || [ "$LIVE_MARKER" = absent ] || fail_pre "the deployed marker is malformed"
  PREV_MARKER=$LIVE_MARKER
  cp "$CLIENT_PIN" "$JOURNAL/client.json.prev"
  [ "$(sha256_file "$JOURNAL/client.json.prev")" = "$PIN_SHA_A6" ] || fail_pre "the client pin changed while it was being backed up"
  cp "$TMP/units-new.sha256" "$JOURNAL/units-new.sha256"
  printf '%s\n' "$PKG_SHA" >"$JOURNAL/package.sha256"
  PIN_PREV_SHA=$PIN_SHA_A6
  PIN_WRITTEN_SHA=""
  jset prev_release "$PREV_RELEASE"
  jset prev_package_sha256 "$PREV_PKG_SHA"
  jset prev_marker "$PREV_MARKER"
  jset pin_prev_sha256 "$PIN_PREV_SHA"
  jset package_sha256 "$PKG_SHA"
  jset host "$HOST"
  jset status prepared
}

remove_marker() { # 0 when the marker is confirmed absent
  if rsh marker-remove "$OP_MARKER_REMOVE" && [ "$R_OUT" = marker-absent ]; then return 0; fi
  if rsh_ro marker-state "$OP_MARKER_STATE" && [ "$(kv marker)" = absent ]; then return 0; fi
  return 1
}

# write_marker <40-hex>: 0 only when the exact marker is confirmed on the target.
# A lost ssh reply is reconciled by reading the marker back, never assumed.
write_marker() {
  if rsh marker-write "$OP_MARKER_WRITE" "VALUE=$1" && [ "$(kv marker)" = "$1" ] && [ "$(kv bytes)" = 41 ]; then
    return 0
  fi
  rsh_ro marker-state "$OP_MARKER_STATE" && [ "$(kv marker)" = "$1" ]
}

# marker_report: describe the marker for the failure footer after write_marker failed.
# A stray value is invalidated where possible; an unreadable state is "unknown".
marker_report() {
  if ! rsh_ro marker-state "$OP_MARKER_STATE"; then printf 'unknown'; return 0; fi
  case "$(kv marker)" in
    absent) printf 'absent';;
    *) if remove_marker; then printf 'absent'; else printf 'unknown'; fi;;
  esac
}

# ---------------------------------------------------------------- rollback ----

restore_pin() {
  local live
  PIN_PREV_SHA=$(jget pin_prev_sha256)
  PIN_WRITTEN_SHA=$(jget pin_written_sha256)
  live=$(sha256_file "$CLIENT_PIN") || { err "cannot read the client pin"; return 1; }
  if [ -n "$PIN_WRITTEN_SHA" ] && [ "$live" = "$PIN_WRITTEN_SHA" ] && [ "$live" != "$PIN_PREV_SHA" ]; then
    [ "$(sha256_file "$JOURNAL/client.json.prev")" = "$PIN_PREV_SHA" ] || { err "journal copy of the client pin is damaged"; return 1; }
    py_helper atomic-install "$JOURNAL/client.json.prev" "$CLIENT_PIN" || { err "could not restore the client pin"; return 1; }
    [ "$(sha256_file "$CLIENT_PIN")" = "$PIN_PREV_SHA" ] || { err "restored client pin does not match the journal"; return 1; }
    journal "client pin restored"
  elif [ -z "$PIN_WRITTEN_SHA" ] || [ "$live" = "$PIN_PREV_SHA" ]; then
    journal "client pin left as found"
  else
    err "the client pin was changed by someone else since this deploy wrote it; not restoring it (conflict)"
    return 1
  fi
}

run_rollback() {
  local n lines prev_set staging
  IN_ROLLBACK=1
  PHASE=R
  jset status rolling-back
  log "==> Rolling back to $PREV_RELEASE (package $PREV_PKG_SHA)"
  [ -f "$JOURNAL/units-prev.sha256" ] || { err "journal has no previous unit set"; return 1; }
  for n in "${UNIT_NAMES[@]}"; do
    lines=$(sed -n "s/^\\([0-9a-f]\\{64\\}\\) $n\$/\\1/p" "$JOURNAL/units-prev.sha256")
    if [ -n "$lines" ] && [ "$(sha256_file "$JOURNAL/units-prev/$n")" != "$lines" ]; then
      err "journal copy of $n no longer matches its recorded hash"
      return 1
    fi
  done
  remove_marker || { err "could not confirm the marker is absent"; return 1; }
  rsh stop-units "$OP_STOP_UNITS" || { err "could not stop the units"; return 1; }
  staging=rollback-$RELEASE
  rsh stage-units-dir "$OP_STAGE_UNITS_DIR" "STAGE=$staging" || { err "could not create the rollback staging directory"; return 1; }
  local files=()
  for n in "${UNIT_NAMES[@]}"; do
    [ -f "$JOURNAL/units-prev/$n" ] && files+=("$JOURNAL/units-prev/$n")
  done
  scp_push "$REMOTE_HOME/.local/state/mimir-search-deploy/staging/$staging" "${files[@]}" || { err "rollback unit upload failed"; return 1; }
  lines=$(cat "$JOURNAL/units-prev.sha256")
  rsh install-units "$OP_INSTALL_UNITS" "STAGE=$staging" "LINES=$lines" || { err "could not restore the previous units"; return 1; }
  restore_pin || return 1
  rsh refresh-start "$OP_REFRESH_START" || { err "refresh failed after restoring the previous units"; return 1; }
  rsh activate "$OP_ACTIVATE" || { err "the previous service did not come back"; return 1; }
  scp_pull "$REMOTE_HOME/.local/share/mimir-search/releases/$PREV_RELEASE/search.pyz" "$TMP/prev.pyz" || { err "cannot fetch the previous package"; return 1; }
  [ "$(sha256_file "$TMP/prev.pyz")" = "$PREV_PKG_SHA" ] || { err "previous package differs from the journal hash"; return 1; }
  prev_set=$TMP/units-prev.set
  sed -n 's/^\([0-9a-f]\{64\}\) \(.*\)$/\1 \2/p' "$JOURNAL/units-prev.sha256" | sort >"$prev_set"
  log "==> Accepting the previous state"
  accept_server rollback "$PREV_RELEASE" "$PREV_PKG_SHA" "$TMP/prev.pyz" "$prev_set" full || return 1
  accept_client "$PREV_RELEASE" "$PREV_PKG_SHA" || return 1
  if [ "$PREV_MARKER" != absent ]; then
    write_marker "$PREV_MARKER" || { err "could not restore the previous marker"; return 1; }
  fi
  jset status rolled-back
  journal "rollback accepted"
  return 0
}

# --------------------------------------------------------------- main flow ----

TMP=$(mktemp -d "${TMPDIR:-/tmp}/mimir-deploy.XXXXXX")
chmod 0700 "$TMP"

PHASE=A1
log "==> A1 source identity"
check_source 1

PHASE=A2
log "==> A2 local toolchain"
check_toolchain

if [ "$ROLLBACK" = 1 ]; then
  [ -f "$JOURNAL/previous.env" ] || fail_pre "no journal for $RELEASE under $STATE_DIR; follow the manual rollback procedure in docs/artifact-search-operations.md"
fi
if [ "$DRY_RUN" = 0 ]; then
  acquire_local_lock
  if [ "$ROLLBACK" = 1 ]; then JOURNAL_ACTIVE=1; else activate_journal; fi
fi

PHASE=A3
log "==> A3 remote reachability and identity"
check_remote_identity
if [ "$DRY_RUN" = 0 ]; then acquire_remote_lock; fi

PHASE=A7
log "==> A7 acceptance config"
check_acceptance_config
PHASE=A6
log "==> A6 client pin"
check_pin

PHASE=A4
log "==> A4/A5 installed state and private configuration"
read_installed_state
check_remote_config

# ---- explicit rollback ------------------------------------------------------
if [ "$ROLLBACK" = 1 ]; then
  load_journal_previous || fail_pre "the journal for $RELEASE is incomplete"
  if [ "$LIVE_RELEASE" != "$RELEASE" ] && [ "$LIVE_RELEASE" != "$PREV_RELEASE" ]; then
    fail_pre "live release $LIVE_RELEASE is neither $RELEASE nor the journal's previous release"
  fi
  rsh_ro package-state "$OP_PACKAGE_STATE" "REL=$PREV_RELEASE" || fail_pre "cannot read the previous release directory"
  [ "$(kv dir)" = ok ] && [ "$(kv file)" = ok ] && [ "$(kv sha)" = "$PREV_PKG_SHA" ] ||
    fail_pre "the previous release directory is missing or its hash differs from the journal"
  MUTATED=1
  print_rollback_recipe
  run_rollback || fail_active "rollback could not be proved"
  jset status rolled-back
  log "Rollback accepted: $PREV_RELEASE (package $PREV_PKG_SHA); marker $PREV_MARKER"
  exit 0
fi

# ---- phase B ----------------------------------------------------------------
PHASE=B
log "==> B build twice, verify, render"
build_and_render

# ---- A8 idempotency probe ---------------------------------------------------
PHASE=A8
if [ "$LIVE_MARKER" = "$RELEASE" ]; then
  log "==> A8 marker equals --release; verifying live state"
  noop_ok=0
  if [ "$LIVE_RELEASE" = "$RELEASE" ] && [ "$PIN_REL" = "$RELEASE" ] && [ "$PIN_PKG" = "$PKG_SHA" ]; then
    if [ "$DRY_RUN" = 1 ]; then probe_mode='readonly'; else probe_mode=full; fi
    if accept_server noop "$RELEASE" "$PKG_SHA" "$TMP/build-1/search.pyz" "$TMP/units-new.sha256" "$probe_mode" &&
      accept_client "$RELEASE" "$PKG_SHA"; then
      noop_ok=1
    fi
  else
    err "drift: live release, pin or package differs from --release"
  fi
  if [ "$noop_ok" = 1 ]; then
    journal "already deployed and verified"
    log "already deployed and verified"
    exit 0
  fi
  log "  live state does not certify the marker; continuing as a normal deploy"
fi

PHASE=A9
decide_previous
if [ "$PIN_REL" != "$PREV_RELEASE" ] || [ "$PIN_PKG" != "$PREV_PKG_SHA" ]; then
  log "NOTE: client pin drift: pin has release $PIN_REL / package $PIN_PKG, previous deploy was $PREV_RELEASE / $PREV_PKG_SHA (rollback restores the pin exactly as found)"
fi
log "==> A9 release directory"
rsh_ro package-state "$OP_PACKAGE_STATE" "REL=$RELEASE" || fail_pre "cannot read the release directory state"
case "$(kv dir)" in
  absent) ;;
  ok)
    [ "$(kv extra)" = 0 ] || fail_pre "releases/$RELEASE contains unexpected entries"
    case "$(kv file)" in
      absent) ;;
      ok) [ "$(kv sha)" = "$PKG_SHA" ] || fail_pre "releases/$RELEASE/search.pyz differs from the build; release directories are immutable";;
      *) fail_pre "releases/$RELEASE/search.pyz has wrong mode or type";;
    esac;;
  *) fail_pre "releases/$RELEASE has wrong mode, owner or type";;
esac

PHASE=A10
log "==> A10 baseline"
rsh_ro baseline "$OP_BASELINE" || log "  baseline could not be read (informational)"
printf '%s\n' "$R_OUT" >"$TMP/baseline.txt"

PHASE=A11
log "==> A11 free space"
rsh_ro disk-free "$OP_DISK_FREE" || fail_pre "cannot read free space"
pkg_bytes=$(wc -c <"$TMP/build-1/search.pyz" | tr -d ' ')
avail_kb=$(kv avail_kb)
[[ "$avail_kb" =~ ^[0-9]+$ ]] || fail_pre "cannot parse free space"
[ $((avail_kb * 1024)) -ge $((pkg_bytes * 2)) ] || fail_pre "less than twice the package size is free on the release filesystem"

print_unit_diff

if [ "$DRY_RUN" = 1 ]; then
  print_plan
  log "Dry run complete: nothing was changed."
  exit 0
fi

if [ -f "$TMP/baseline.txt" ] && [ "$JOURNAL_ACTIVE" = 1 ]; then
  mkdir -p "$JOURNAL/receipts"
  cp "$TMP/baseline.txt" "$JOURNAL/receipts/baseline.txt"
fi
print_plan

# ---- phase C ----------------------------------------------------------------
PHASE=C
log "==> C stage on the target (additive)"
stage_package
stage_units
if [ "$RESUME" = 0 ]; then backup_previous_state; else journal "resuming with the recorded previous state"; fi
journal "staged; previous release $PREV_RELEASE"

# ---- phase D ----------------------------------------------------------------
PHASE=D1
log "==> D1 invalidate the marker"
MUTATED=1
jset status switching
if ! remove_marker; then
  if rsh_ro marker-state "$OP_MARKER_STATE" && [ "$(kv marker)" = "$PREV_MARKER" ] && [ "$PREV_MARKER" != absent ]; then
    MUTATED=0
    jset status prepared
    fail_pre "marker removal failed and the marker is unchanged; nothing was modified"
  fi
  err "the marker removal outcome is indeterminate; marker state is unknown"
  jset status marker-unknown
  footer_failure "unknown"
  exit 11
fi
PHASE=D2
log "==> D2 stop timers and service"
rsh stop-units "$OP_STOP_UNITS" || fail_active "could not stop the units"
PHASE=D3
log "==> D3 install units"
rsh install-units "$OP_INSTALL_UNITS" "STAGE=$RELEASE" "LINES=$(cat "$TMP/units-new.sha256")" || fail_active "unit installation failed"
PHASE=D4
log "==> D4 initial refresh"
rsh refresh-start "$OP_REFRESH_START" || fail_active "initial refresh failed"
PHASE=D5
log "==> D5 start"
rsh activate "$OP_ACTIVATE" || fail_active "the service did not start"

# ---- phase E ----------------------------------------------------------------
PHASE=E
log "==> E server-side acceptance"
accept_server new "$RELEASE" "$PKG_SHA" "$TMP/build-1/search.pyz" "$TMP/units-new.sha256" full || fail_active "server-side acceptance failed"

# ---- phase F ----------------------------------------------------------------
PHASE=F
log "==> F client pin"
[ "$(sha256_file "$CLIENT_PIN")" = "$PIN_SHA_A6" ] || fail_active "the client pin changed during the run; refusing to overwrite it"
new_pin_sha=$(py_helper pin-new "$CLIENT_PIN" "$RELEASE" "$PKG_SHA" "$TMP/client.json.new") || fail_active "could not prepare the new client pin"
jset pin_written_sha256 "$new_pin_sha"
py_helper atomic-install "$TMP/client.json.new" "$CLIENT_PIN" || fail_active "could not write the client pin"
[ "$(sha256_file "$CLIENT_PIN")" = "$new_pin_sha" ] || fail_active "the client pin does not match what was written"
accept_client "$RELEASE" "$PKG_SHA" || fail_active "client acceptance failed"

# ---- phase G ----------------------------------------------------------------
PHASE=G
log "==> G commit the marker"
check_source 0 fail_active
write_marker "$RELEASE" || { sleep 1; write_marker "$RELEASE"; } || {
  marker_state_text=$(marker_report)
  err "accepted but unmarked: $RELEASE passed every check but the marker could not be confirmed (marker: $marker_state_text); re-run the same command to finish (no rollback was performed)"
  jset status accepted-unmarked
  footer_failure "$marker_state_text (accepted but unmarked)"
  exit 11
}
COMMITTED=1
jset status deployed
journal "marker written"
unit_json=""
while read -r unit_hash unit_name; do
  unit_json="$unit_json\"$unit_name\":\"$unit_hash\","
done <"$TMP/units-new.sha256"
receipt_body=$(printf '{"release":"%s","package_sha256":"%s","unit_sha256_set":{%s},"accepted_at_utc":"%s","previous_release":"%s"}' \
  "$RELEASE" "$PKG_SHA" "${unit_json%,}" "$(now_utc)" "$PREV_RELEASE")
rsh receipt-write "$OP_RECEIPT_WRITE" "BODY=$receipt_body" || log "  (optional audit receipt could not be written; the marker is authoritative)"

log ""
log "Deploy complete."
log "Release:          $RELEASE"
log "Package sha256:   $PKG_SHA"
log "Previous release: $PREV_RELEASE"
log "Journal:          $JOURNAL"
log "Marker:           $REMOTE_HOME/.local/share/mimir-search/.deployed-commit"
exit 0
