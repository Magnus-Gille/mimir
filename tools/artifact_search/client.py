"""Pinned read-only client for the owner-only Mímir search pilot."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import shlex
import subprocess
import time
from urllib.parse import quote, urlsplit
import uuid

from .client_remote import decode, private_bytes

LIMIT = 131072
ERRORS = {'credential_unavailable', 'unauthorized', 'retrieval_unavailable', 'invalid_response', 'invalid_request'}


class ClientError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(condition):
    if not condition:
        raise ValueError('invalid contract')


def _hex(value, length):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{%d}' % length, value) is not None


def _text(value, limit, *, controls=False):
    if not isinstance(value, str) or len(value) > limit:
        return False
    try:
        value.encode('utf-8', errors='strict')
    except UnicodeError:
        return False
    return controls or all(ord(c) >= 32 and ord(c) != 127 for c in value)


def load_profile(path=None):
    path = Path(path) if path is not None else Path.home() / '.config/mimir-search/client.json'
    try:
        profile = decode(private_bytes(path, 8192))
        _require(isinstance(profile, dict) and set(profile) == {'schema', 'ssh_host', 'source_id', 'mimir_origin', 'original_prefix', 'release', 'package_sha256'})
        _require(profile['schema'] == 'mimir-search-client-v1')
        _require(isinstance(profile['ssh_host'], str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,252}', profile['ssh_host']))
        _require(str(uuid.UUID(profile['source_id'])) == profile['source_id'])
        origin = profile['mimir_origin']
        _require(_text(origin, 2048))
        parsed = urlsplit(origin)
        _require(parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment and parsed.path in ('', '/') and '\\' not in origin)
        _require(_hex(profile['release'], 40) and _hex(profile['package_sha256'], 64))
        prefix = profile['original_prefix']
        _require(_text(prefix, 2048) and '\\' not in prefix)
        _require(not prefix or (not prefix.startswith('/') and str(PurePosixPath(prefix)) == prefix and all(part and not part.startswith('.') for part in prefix.split('/'))))
        return profile
    except (OSError, ValueError, TypeError, AttributeError, UnicodeError, RecursionError):
        raise ClientError('invalid_profile') from None


def validate_receipt(value, profile, top, *, now=None):
    now = time.time() if now is None else now
    try:
        fields = {'mode', 'decision', 'results', 'snapshot_id', 'source_id', 'observed_at', 'expires_at', 'verified_at', 'release', 'package_sha256', 'abstained'}
        _require(isinstance(value, dict) and set(value) == fields)
        _require(value['mode'] == 'lexical' and value['decision'] == 'fts5_bm25')
        _require(all(value[k] == profile[k] for k in ('source_id', 'release', 'package_sha256')))
        _require(_hex(value['snapshot_id'], 64))
        observed, expires, verified = (value[k] for k in ('observed_at', 'expires_at', 'verified_at'))
        _require(type(observed) is int and type(expires) is int and type(verified) in (int, float) and math.isfinite(verified))
        _require(0 <= observed < expires and expires - observed <= 3600)
        if not observed <= verified <= now + 5 or now >= expires or verified >= expires or now - verified > 30:
            raise ClientError('stale_response')
        results = value['results']
        _require(isinstance(results, list) and len(results) <= top and type(value['abstained']) is bool and value['abstained'] == (not results))
        result_fields = {'path', 'artifact_id', 'sha256', 'locator', 'title', 'snippet', 'score', 'mimir_url', 'source_id', 'snapshot_id', 'verification'}
        for result in results:
            _require(isinstance(result, dict) and set(result) == result_fields)
            path = result['path']
            _require(_text(path, 2048) and path and not path.startswith('/') and '\\' not in path)
            _require(str(PurePosixPath(path)) == path and all(part and not part.startswith('.') for part in path.split('/')))
            _require(_text(result['artifact_id'], 128) and result['artifact_id'])
            _require(_hex(result['sha256'], 64))
            _require(result['source_id'] == value['source_id'] and result['snapshot_id'] == value['snapshot_id'])
            _require(result['verification'] == 'original_sha256_matched')
            locator = result['locator']
            _require(isinstance(locator, dict) and set(locator) == {'kind', 'number', 'anchor'})
            _require(locator['kind'] in ('page', 'slide', 'section') and type(locator['number']) is int and locator['number'] > 0)
            _require(locator['anchor'] is None or _text(locator['anchor'], 2048))
            full_path = '/'.join(part for part in (profile['original_prefix'], path) if part)
            expected = profile['mimir_origin'].rstrip('/') + '/files/' + quote(full_path, safe='/')
            if locator['kind'] == 'page':
                expected += '#page=' + str(locator['number'])
            elif locator['anchor']:
                expected += '#' + quote(locator['anchor'], safe='')
            _require(result['mimir_url'] == expected)
            _require(_text(result['title'], 512, controls=True) and _text(result['snippet'], 512, controls=True))
            _require(type(result['score']) in (int, float) and math.isfinite(result['score']))
        return value
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        raise ClientError('invalid_response') from None


def _transport(profile, payload):
    source = Path(__file__).with_name('client_remote.py').read_text(encoding='utf-8')
    command = 'python3 -I -c ' + shlex.quote(source)
    argv = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=10', '-o', 'ClearAllForwardings=yes', '-o', 'ForwardAgent=no', '-o', 'PermitLocalCommand=no', '--', profile['ssh_host'], command]
    environment = {k: os.environ[k] for k in ('PATH', 'HOME', 'USER', 'LOGNAME', 'SSH_AUTH_SOCK', 'LANG') if k in os.environ}
    process = None
    try:
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=environment)
        deadline = time.monotonic() + 45
        chunks = bytearray()
        with selectors.DefaultSelector() as selector:
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE)
            selector.register(process.stdout, selectors.EVENT_READ)
            pending = memoryview(payload)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ClientError('transport_unavailable')
                for key, _ in selector.select(remaining):
                    if key.fileobj is process.stdin:
                        count = os.write(process.stdin.fileno(), pending)
                        pending = pending[count:]
                        if not pending:
                            selector.unregister(process.stdin)
                            process.stdin.close()
                    else:
                        data = os.read(process.stdout.fileno(), 16384)
                        chunks.extend(data)
                        if len(chunks) > LIMIT:
                            raise ClientError('invalid_response')
                        if not data:
                            selector.unregister(process.stdout)
            if process.wait(timeout=max(0.01, deadline - time.monotonic())):
                raise ClientError('transport_unavailable')
        envelope = decode(bytes(chunks))
        _require(isinstance(envelope, dict) and type(envelope.get('ok')) is bool)
        if envelope['ok']:
            _require(set(envelope) == {'ok', 'data'})
            return envelope['data']
        code = envelope.get('error')
        raise ClientError(code if isinstance(code, str) and code in ERRORS else 'invalid_response')
    except (OSError, subprocess.TimeoutExpired):
        raise ClientError('transport_unavailable') from None
    except (ValueError, UnicodeError, RecursionError):
        raise ClientError('invalid_response') from None
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait()
            for stream in (process.stdin, process.stdout):
                if stream and not stream.closed:
                    stream.close()


def search(query, top=5, *, profile_path=None):
    try:
        _require(isinstance(query, str) and query.strip() and len(query) <= 4096 and type(top) is int and 1 <= top <= 5)
        payload = json.dumps({'query': query, 'top': top}, ensure_ascii=False, allow_nan=False).encode('utf-8')
        _require(len(payload) <= 8192)
    except (ValueError, TypeError, UnicodeError):
        raise ClientError('invalid_request') from None
    profile = load_profile(profile_path)
    return validate_receipt(_transport(profile, payload), profile, top)
