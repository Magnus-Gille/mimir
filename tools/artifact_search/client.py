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
MODES = ('lexical', 'hybrid', 'vector')
# Runtime fallback reasons, including every SemanticConfig error code.
FALLBACK_REASONS = frozenset({
    'semantic_disabled', 'vectors_incomplete', 'embedding_unavailable', 'embedding_route_unavailable',
    'invalid_semantic_flag', 'invalid_default_mode', 'semantic_default_requires_enabled',
    'invalid_embedding_revision', 'invalid_embedding_dimensions', 'invalid_abstain_min_cosine',
    'invalid_abstain_min_zscore', 'semantic_default_requires_abstention'})
# A configuration error resets the runtime default to lexical, so a semantic
# server default can only fall back for availability reasons.
DEFAULT_FALLBACK_REASONS = frozenset({'vectors_incomplete', 'embedding_unavailable', 'embedding_route_unavailable'})
ABSTENTION_REASONS = (None, 'no_vectors', 'below_cosine_floor', 'too_few_units_for_margin', 'below_score_margin')
MIN_ZSCORE_UNITS = 8


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


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _cosine(value):
    # Unit-vector dot products may exceed 1 by rounding residue only.
    return _number(value) and -1 - 1e-9 <= value <= 1 + 1e-9


def _abstention(value, results):
    _require(isinstance(value, dict) and set(value) == {'policy', 'abstained', 'reason', 'signal'})
    _require(value['policy'] in ('none', 'score') and type(value['abstained']) is bool)
    reason, signal = value['reason'], value['signal']
    _require(reason in ABSTENTION_REASONS and (reason is None) == (not value['abstained']))
    _require(value['abstained'] == (not results))
    _require(value['policy'] == 'score' or reason in (None, 'no_vectors'))
    _require(isinstance(signal, dict) and set(signal) == {'units', 'top_cosine', 'zscore'})
    units, top_cosine, zscore = signal['units'], signal['top_cosine'], signal['zscore']
    _require(type(units) is int and units >= len(results) and (reason == 'no_vectors') == (units == 0))
    _require(top_cosine is None if units == 0 else _cosine(top_cosine))
    _require(zscore is None if units < MIN_ZSCORE_UNITS else _number(zscore) and zscore >= 0)
    # Each threshold reason needs the signal it was computed from.
    _require(reason != 'too_few_units_for_margin' or 0 < units < MIN_ZSCORE_UNITS)
    _require(reason != 'below_score_margin' or zscore is not None)


def _embedding(value):
    _require(isinstance(value, dict) and set(value) == {'model', 'dimensions', 'cache'})
    _require(isinstance(value['model'], str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', value['model']))
    _require(type(value['dimensions']) is int and value['dimensions'] in (256, 768))
    _require(isinstance(value['cache'], str) and re.fullmatch('%dd-[0-9a-f]{16}' % value['dimensions'], value['cache']))


def _shape(value, mode, top):
    """Expected (top-level extras, result extras, score kind) for the served mode.

    An explicit mode opts into the extended receipt, whose fields depend on
    what was served. Without ``mode`` the receipt is exact v1 lexical, unless
    it advertises a semantic server default through ``requested_mode``.
    """
    reasons = FALLBACK_REASONS
    if mode is None:
        if 'requested_mode' not in value:
            _require(value.get('mode') == 'lexical' and value.get('decision') == 'fts5_bm25')
            return set(), set(), None
        # A lexical default is always answered in v1, so only a semantic one
        # may be advertised.
        mode = value['requested_mode']
        _require(isinstance(mode, str) and mode in ('hybrid', 'vector'))
        reasons = DEFAULT_FALLBACK_REASONS
    _require(mode in MODES and value.get('requested_mode') == mode)
    served = value.get('mode')
    if served == 'lexical' and mode == 'lexical' or served == 'lexical_fallback' and mode != 'lexical':
        _require(value.get('decision') == 'fts5_bm25')
        if served == 'lexical':
            return {'requested_mode'}, {'score_kind'}, 'bm25'
        _require(isinstance(value.get('fallback_reason'), str) and value['fallback_reason'] in reasons)
        return {'requested_mode', 'fallback_reason'}, {'score_kind'}, 'bm25'
    _require(served == mode)
    extras = {'requested_mode', 'abstention', 'embedding'}
    if mode == 'vector':
        _require(value.get('decision') == 'cosine_max_segment')
        return extras, {'score_kind', 'cosine'}, 'cosine'
    _require(value.get('decision') == 'rrf60_bounded')
    window = value.get('candidate_window')
    _require(type(window) is int and window == max(top, 5))
    return extras | {'candidate_window'}, {'score_kind', 'cosine'}, 'rrf'


def validate_receipt(value, profile, top, *, mode=None, now=None):
    now = time.time() if now is None else now
    try:
        _require(isinstance(value, dict))
        extras, result_extras, score_kind = _shape(value, mode, top)
        fields = {'mode', 'decision', 'results', 'snapshot_id', 'source_id', 'observed_at', 'expires_at', 'verified_at', 'release', 'package_sha256', 'abstained'}
        _require(set(value) == fields | extras)
        _require(all(value[k] == profile[k] for k in ('source_id', 'release', 'package_sha256')))
        _require(_hex(value['snapshot_id'], 64))
        observed, expires, verified = (value[k] for k in ('observed_at', 'expires_at', 'verified_at'))
        _require(type(observed) is int and type(expires) is int and type(verified) in (int, float) and math.isfinite(verified))
        _require(0 <= observed < expires and expires - observed <= 3600)
        if not observed <= verified <= now + 5 or now >= expires or verified >= expires or now - verified > 30:
            raise ClientError('stale_response')
        results = value['results']
        _require(isinstance(results, list) and len(results) <= top and type(value['abstained']) is bool and value['abstained'] == (not results))
        if 'abstention' in extras:
            _abstention(value['abstention'], results)
            # The runtime refuses a semantic default without an abstention policy.
            _require(mode is not None or value['abstention']['policy'] == 'score')
            _embedding(value['embedding'])
        result_fields = {'path', 'artifact_id', 'sha256', 'locator', 'title', 'snippet', 'score', 'mimir_url', 'source_id', 'snapshot_id', 'verification'} | result_extras
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
            _require(_number(result['score']))
            if score_kind is not None:
                _require(result['score_kind'] == score_kind)
            if score_kind == 'cosine':
                _require(_cosine(result['cosine']) and result['score'] == result['cosine'])
            elif score_kind == 'rrf':
                # Equal-weight RRF60 over two windowed channels: a result gets one
                # or two contributions, each between 1/(60 + window) and 1/61.
                _require(_cosine(result['cosine']) and 1 / (60 + max(top, 5)) <= result['score'] <= 2 / 61)
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


def search(query, top=5, *, mode=None, profile_path=None):
    try:
        _require(isinstance(query, str) and query.strip() and len(query) <= 4096 and type(top) is int and 1 <= top <= 5)
        _require(mode is None or isinstance(mode, str) and mode in MODES)
        request = {'query': query, 'top': top} if mode is None else {'query': query, 'top': top, 'mode': mode}
        payload = json.dumps(request, ensure_ascii=False, allow_nan=False).encode('utf-8')
        _require(len(payload) <= 8192)
    except (ValueError, TypeError, UnicodeError):
        raise ClientError('invalid_request') from None
    profile = load_profile(profile_path)
    return validate_receipt(_transport(profile, payload), profile, top, mode=mode)
