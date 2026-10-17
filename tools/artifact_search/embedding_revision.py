"""Detect and explicitly update embedding gateway revision drift.

``semantic-check`` reads the serving runtime's ``/health`` over loopback and
compares the gateway half of its pinned ``MIMIR_SEARCH_EMBEDDING_REVISION`` with
the gateway's live ``/portal/me``. It reports ``semantic.error``, the vector
state and the comparison as one Heimdall status panel, or as a loud log when no
hub is configured.

``repin-embedding`` updates only the gateway half of the pin in ``runtime.env``.
It verifies the installed model GGUF against the pinned model SHA-256 and
refuses a model change. Without ``--apply`` it is a dry run. ``--apply`` also
needs ``--expect-gateway`` set to the revision the operator saw, so a new
revision is never trusted automatically.

Neither command prints credentials, route URLs or response bodies.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from .embedding_client import EmbedderUnavailable, M5Embedder, _NoRedirect
from . import runtime_vectors
from .vectors import EmbeddingIdentity

ACTIONS = ('semantic-check', 'repin-embedding')
PIN_KEY = 'MIMIR_SEARCH_EMBEDDING_REVISION'
ROUTE_KEYS = ('M5_OPENAI_BASE_URL', 'M5_API_KEY')
MAX_ENV_BYTES = 65536
MAX_HEALTH_BYTES = 65536
GATEWAY_TIMEOUT = 8
CHECK_SECONDS = 15
PUSH_TIMEOUT = 4
_GATEWAY = re.compile(r'[0-9a-f]{40}')
# systemd accepts whitespace around "=", multiline quotes and backslash escapes.
# Only the single-line subset below is parsed; anything else is refused so the
# command can never rewrite a different assignment from the one systemd loads.
_ASSIGNMENT = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)=(.*)')
# Physical lines are LF (optionally CRLF); other separators or controls are refused.
_LINE = re.compile(r'[^\n]*\n|[^\n]+\Z')
_CONTROL = re.compile('[\x00-\x08\x0b-\x1f\x7f\x85\u2028\u2029]')
_VALUE = re.compile(r'[^"\'\\]*|"[^"\\]*"|\'[^\'\\]*\'')
_PIN_LINE = re.compile(r'([ \t]*' + PIN_KEY + r'=[ \t]*)(["\']?)([0-9a-f]{40}):([0-9a-f]{64})\2([ \t]*)')
_CODE = re.compile(r'[a-z_0-9]{1,64}')
_RANK = {'pass': 0, 'warn': 1, 'fail': 2}


class RevisionError(ValueError):
    """A bounded, non-sensitive refusal code."""


def _printable(value, low=1, high=4096):
    return isinstance(value, str) and low <= len(value) <= high and all(33 <= ord(c) <= 126 for c in value)


def _gateway_probe(revision, base_url, api_key, dimensions=runtime_vectors.RUNTIME_DIMENSIONS[0]):
    """Live gateway revision via the reader's own key; refuses a non-private route."""
    try:
        embedder = M5Embedder(EmbeddingIdentity(revision, dimensions), base_url, api_key,
                              timeout=GATEWAY_TIMEOUT, deadline=time.monotonic() + CHECK_SECONDS)
    except (ValueError, TypeError):
        raise RevisionError('embedding_route_unavailable') from None
    with runtime_vectors.wall_clock_limit(embedder.deadline):
        return embedder.gateway_revision()


# --- runtime.env ---------------------------------------------------------

def _read_private(path):
    """Bytes and stat of an owner-only regular file, never following a symlink."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0))
    except OSError as error:
        if os.path.islink(path):
            raise RevisionError('private_env_file_required') from None
        raise RevisionError('env_file_unreadable') from error
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RevisionError('private_env_file_required')
        chunks, size = [], 0
        while size <= MAX_ENV_BYTES:
            chunk = os.read(fd, MAX_ENV_BYTES + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if size > MAX_ENV_BYTES:
            raise RevisionError('env_file_too_large')
        return b''.join(chunks), info
    finally:
        os.close(fd)


def parse_env(data):
    """systemd ``EnvironmentFile`` assignments plus the pin's line index.

    Duplicate pin or route keys are refused instead of guessing which one a
    reader would load.
    """
    try:
        text = data.decode('utf-8')
    except UnicodeError:
        raise RevisionError('invalid_env_file') from None
    if _CONTROL.search(text.replace('\r\n', '\n')):
        raise RevisionError('unsupported_env_syntax')
    lines = _LINE.findall(text)
    values, seen, pin = {}, set(), None
    for number, line in enumerate(lines):
        body = line.rstrip('\r\n').strip(' \t')
        if not body or body[0] in '#;':
            continue
        match = _ASSIGNMENT.fullmatch(body)
        if match is None or not _VALUE.fullmatch(match.group(2).strip(' \t')):
            raise RevisionError('unsupported_env_syntax')
        key, value = match.group(1), match.group(2).strip(' \t')
        if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
            value = value[1:-1]
        if key in seen and key in (PIN_KEY, *ROUTE_KEYS):
            raise RevisionError('ambiguous_env_file')
        seen.add(key)
        values[key] = value
        if key == PIN_KEY:
            pin = number
    if pin is None:
        raise RevisionError('revision_pin_missing')
    match = _PIN_LINE.fullmatch(lines[pin].rstrip('\r\n'))
    if match is None:
        raise RevisionError('invalid_revision_pin')
    return lines, values, pin, match


def file_sha256(path):
    """SHA-256 of the installed model file (model stores may link to a blob)."""
    try:
        with open(path, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise RevisionError('model_file_required')
            digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
            return digest.hexdigest()
    except OSError as error:
        raise RevisionError('model_file_unreadable') from error


def _backup(env_file, data, backup_dir, now):
    """Write an exclusive owner-only copy of the current bytes; never overwrite."""
    directory = Path(backup_dir) if backup_dir is not None else env_file.parent
    info = os.lstat(directory)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise RevisionError('private_backup_directory_required')
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(now()))
    path = directory / ('%s.pre-repin-%s' % (env_file.name, stamp))
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise RevisionError('backup_exists') from None
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    return path


def _replace(env_file, original, info, data):
    """Atomically replace ``env_file`` unless it changed since it was read."""
    fd, temporary = tempfile.mkstemp(dir=env_file.parent, prefix='.' + env_file.name + '.', suffix='.tmp')
    try:
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        current, now = _read_private(env_file)
        if current != original or (now.st_dev, now.st_ino) != (info.st_dev, info.st_ino):
            raise RevisionError('env_file_changed')
        os.replace(temporary, env_file)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    directory = os.open(env_file.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


@contextlib.contextmanager
def _locked(env_file):
    """Serialize re-pins with a stable lock held from the read to the replace."""
    path = env_file.parent / ('.%s.repin.lock' % env_file.name)
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0), 0o600)
    except OSError:
        raise RevisionError('env_lock_unavailable') from None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RevisionError('repin_in_progress') from None
        yield
    finally:
        os.close(fd)


def repin(env_file, model_file, *, apply=False, expect=None, backup_dir=None, now=time.time):
    """Dry-run or apply a gateway re-pin. Returns a credential-free summary.

    Applying holds a lock through read, backup and replace, so concurrent
    re-pins cannot interleave. A hand edit that ignores the lock is still
    detected up to the final comparison before the atomic replace.
    """
    env_file = Path(env_file)
    if apply and not (isinstance(expect, str) and _GATEWAY.fullmatch(expect)):
        raise RevisionError('expect_gateway_required')
    if not apply:
        return _repin(env_file, model_file, False, expect, backup_dir, now)
    with _locked(env_file):
        return _repin(env_file, model_file, True, expect, backup_dir, now)


def _repin(env_file, model_file, apply, expect, backup_dir, now):
    data, info = _read_private(env_file)
    lines, values, pin, match = parse_env(data)
    old_gateway, model = match.group(3), match.group(4)
    # The model checksum stays operator-verified: refuse before any network call.
    if file_sha256(model_file) != model:
        raise RevisionError('model_sha256_mismatch')
    try:
        live = _gateway_probe(old_gateway + ':' + model, values.get('M5_OPENAI_BASE_URL', ''),
                              values.get('M5_API_KEY', ''))
    except EmbedderUnavailable as error:
        raise RevisionError(runtime_vectors.error_code(error, 'embedding_unavailable')) from None
    result = {'old_revision': old_gateway + ':' + model, 'new_revision': live + ':' + model,
              'model_sha256_verified': True, 'changed': live != old_gateway, 'applied': False,
              'dry_run': not apply}
    if not apply:
        return result
    if live != expect:
        raise RevisionError('gateway_revision_unexpected')
    if not result['changed']:
        return result
    ending = lines[pin][len(lines[pin].rstrip('\r\n')):]
    lines[pin] = (match.group(1) + match.group(2) + live + ':' + model + match.group(2) +
                  match.group(5) + ending)
    backup = _backup(env_file, data, backup_dir, now)
    _replace(env_file, data, info, ''.join(lines).encode('utf-8'))
    result.update(applied=True, backup=str(backup), restart_required=True,
                  next_step='systemctl --user restart mimir-search.service')
    return result


# --- semantic-check ------------------------------------------------------

def _health(env, port):
    key = env.get('MIMIR_SEARCH_API_KEY', '')
    if not _printable(key, 16):
        raise RevisionError('search_credential_unavailable')
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    request = Request('http://127.0.0.1:%d/health' % port, headers={'Authorization': 'Bearer ' + key})
    try:
        with opener.open(request, timeout=5) as response:
            raw = response.read(MAX_HEALTH_BYTES + 1)
    except HTTPError as error:
        # A not-ready runtime answers 503 with the same health body.
        with error:
            if error.code != 503:
                raise RevisionError('runtime_health_unavailable') from None
            try:
                raw = error.read(MAX_HEALTH_BYTES + 1)
            except (OSError, HTTPException, ValueError):
                raise RevisionError('runtime_health_unavailable') from None
    except (URLError, OSError, HTTPException, ValueError):
        # Malformed responses carry raw response text: never surface it.
        raise RevisionError('runtime_health_unavailable') from None
    try:
        if len(raw) > MAX_HEALTH_BYTES:
            raise ValueError('oversized')
        health = json.loads(raw)
    except (ValueError, UnicodeError):
        raise RevisionError('runtime_health_unavailable') from None
    if not isinstance(health, dict) or not isinstance(health.get('semantic'), dict):
        raise RevisionError('runtime_health_unavailable')
    return health['semantic']


def _code(value, default):
    return value if isinstance(value, str) and _CODE.fullmatch(value) else default


def semantic_check(env, *, port=8791):
    """Classify the serving runtime's semantic state; no side effects besides reads."""
    findings = []

    def add(state, code):
        findings.append((state, code))

    try:
        semantic = _health(env, port)
    except RevisionError as error:
        add('fail', str(error))
        semantic = None
    vectors = None
    if semantic is None:
        pass
    elif semantic.get('error') is not None:
        add('fail', _code(semantic['error'], 'semantic_config_invalid'))
    elif semantic.get('enabled') is not True:
        add('pass', 'semantic_disabled')
    else:
        revision = semantic.get('revision')
        try:
            if not (isinstance(revision, str) and runtime_vectors._REVISION.fullmatch(revision)):
                raise RevisionError('semantic_config_invalid')
            live = _gateway_probe(revision, env.get('M5_OPENAI_BASE_URL', ''), env.get('M5_API_KEY', ''))
        except RevisionError as error:
            add('fail', str(error))
        except EmbedderUnavailable as error:
            if runtime_vectors.transient(error):
                add('warn', 'embedding_unavailable')
            else:
                add('fail', runtime_vectors.error_code(error, 'embedding_unavailable'))
        else:
            if live == revision.split(':')[0]:
                add('pass', 'embedding_gateway_revision_current')
            else:
                add('fail', 'embedding_gateway_revision_mismatch')
        vectors = semantic.get('vectors')
        if vectors is None:
            add('warn', 'vectors_not_started')
        elif not runtime_vectors.valid_state(vectors):
            add('fail', 'vectors_state_invalid')
        elif vectors.get('identity') != semantic.get('cache'):
            add('warn', 'vectors_identity_stale')
        elif vectors['status'] == 'complete':
            add('pass', 'vectors_complete')
        elif vectors['status'] == 'pending':
            add('pass', 'vectors_building')
        else:
            add('warn' if vectors['status'] == 'unavailable' else 'fail',
                _code(vectors.get('error'), 'vectors_' + vectors['status']))
    state = max((s for s, _ in findings), key=_RANK.__getitem__)
    codes = [code for _, code in findings]
    message = ', '.join(codes)
    if isinstance(vectors, dict) and type(vectors.get('covered')) is int and type(vectors.get('units')) is int:
        message += '; vectors %d/%d' % (vectors['covered'], vectors['units'])
    if 'embedding_gateway_revision_mismatch' in codes:
        message += '; re-pin with ops repin-embedding'
    return {'state': state, 'codes': codes, 'message': message}


def push_panel(env, panel):
    """POST one status panel. ``None`` when unconfigured; never raises or logs secrets."""
    url, token = env.get('HEIMDALL_HUB_URL', ''), env.get('HEIMDALL_FLEET_TOKEN', '')
    if not url and not token:
        return None
    try:
        parts = urlsplit(url) if _printable(url) else None
        if (parts is None or parts.scheme not in {'http', 'https'} or not parts.hostname or
                parts.username or parts.password or parts.fragment or not _printable(token)):
            raise ValueError('invalid hub configuration')
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        request = Request(url, data=json.dumps(panel).encode('utf-8'), method='POST',
                          headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token})
    except ValueError:
        print('[mimir-search] Heimdall push skipped: invalid hub configuration', file=sys.stderr)
        return False
    try:
        with opener.open(request, timeout=PUSH_TIMEOUT) as response:
            return 200 <= response.status < 300
    except HTTPError as error:
        error.close()
        print('[mimir-search] Heimdall push rejected (status=%d)' % error.code, file=sys.stderr)
    except Exception:  # noqa: BLE001 - fixed diagnostic only; the fallback alert must still run.
        print('[mimir-search] Heimdall push failed', file=sys.stderr)
    return False


def main(argv=None):
    parser = argparse.ArgumentParser(prog='search.pyz ops', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='action', required=True)
    check = commands.add_parser('semantic-check', help='alert on semantic/vector state and revision drift')
    check.add_argument('--port', type=int, default=8791)
    pin = commands.add_parser('repin-embedding', help='dry-run or apply a gateway revision re-pin')
    pin.add_argument('--env-file', type=Path, required=True)
    pin.add_argument('--model-file', type=Path, required=True)
    pin.add_argument('--backup-dir', type=Path)
    pin.add_argument('--apply', action='store_true')
    pin.add_argument('--expect-gateway')
    args = parser.parse_args(argv)
    os.umask(0o077)
    if args.action == 'semantic-check':
        if not 1 <= args.port <= 65535:
            print(json.dumps({'error': 'invalid_port'}))
            return 1
        try:
            result = semantic_check(os.environ, port=args.port)
        except Exception:  # noqa: BLE001 - an unexpected failure must still alert, without its text.
            result = {'state': 'fail', 'codes': ['semantic_check_failed'], 'message': 'semantic_check_failed'}
        pushed = push_panel(os.environ, {'service': 'mimir', 'panel': 'search-semantic', 'kind': 'status',
                                         'label': 'Mímir semantic search', 'state': result['state'],
                                         'message': result['message'][:240]})
        print(json.dumps({'state': result['state'], 'codes': result['codes'], 'pushed': pushed}))
        if result['state'] != 'pass' and pushed is not True:
            print('[mimir-search] ALERT semantic retrieval %s: %s' % (result['state'], result['message']),
                  file=sys.stderr)
        return 1 if result['state'] == 'fail' else 0
    try:
        print(json.dumps(repin(args.env_file, args.model_file, apply=args.apply,
                               expect=args.expect_gateway, backup_dir=args.backup_dir)))
        return 0
    except RevisionError as error:
        print(json.dumps({'error': _code(str(error), 'repin_failed')}))
    except Exception as error:  # noqa: BLE001 - bounded: never the exception text.
        print(json.dumps({'error': 'repin_failed', 'error_type': type(error).__name__}))
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
