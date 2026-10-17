"""Owner-only snapshot search runtime. Derived state never proves source freshness."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hmac
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import stat
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import quote, urlsplit

from .core import _reference, read_source
from .snapshot import (HEX, MAX_BYTES, LocalSnapshotSource, SnapshotError, _private_store,
                       _read, _sync_dir, _uuid, canonical, digest)
from .snapshot_http import HttpSnapshotSource

LEASE_SCHEMA = 'mimir-search-handoff-v1'
MAX_STATE_BYTES = 512 * 1024 * 1024
MAX_LEASE_SECONDS = 3600
MIN_RETRY_SECONDS = 5
MAX_RETRY_SECONDS = 300
# Upstream outages, throttling and a lapsed lease (renewed by the refresher once
# the source is back) are retried with backoff and never serve meanwhile. Every
# other failure (auth, missing objects, hash or scope mismatches) fails closed.
TRANSIENT_ERRORS = re.compile(r'source_unavailable|source_stale|source_http_(?:408|429|5[0-9]{2})')


class RuntimeErrorCode(ValueError):
    pass


def private_json(path, limit=16384):
    path = Path(path)
    data, info = _read(path.parent, path.name, limit)
    if info.st_mode & 0o077 or info.st_uid != os.getuid():
        raise RuntimeErrorCode('private_owner_file_required')
    def unique(pairs):
        output = {}
        for key, value in pairs:
            if key in output:
                raise RuntimeErrorCode('duplicate_key')
            output[key] = value
        return output
    try:
        return json.loads(data, object_pairs_hook=unique)
    except (UnicodeError, json.JSONDecodeError):
        raise RuntimeErrorCode('invalid_json') from None


def atomic_json(path, value):
    path = Path(path)
    pending = path.with_suffix('.pending')
    # The caller holds the state lock. Never follow an existing pending symlink.
    fd = os.open(pending, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(canonical(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(pending, path)
    _sync_dir(path.parent)


def load_handoff(path, now=None):
    value = private_json(path)
    keys = {'schema', 'source_id', 'snapshot_id', 'observed_at', 'expires_at',
            'kind', 'bundle', 'original', 'mimir_origin', 'original_prefix'}
    if not isinstance(value, dict) or set(value) != keys or value['schema'] != LEASE_SCHEMA:
        raise RuntimeErrorCode('invalid_handoff')
    _uuid(value['source_id'])
    if not isinstance(value['snapshot_id'], str) or not HEX.fullmatch(value['snapshot_id']):
        raise RuntimeErrorCode('invalid_snapshot_id')
    now = time.time() if now is None else now
    start, end = value['observed_at'], value['expires_at']
    if (type(start) is not int or type(end) is not int or
            not 0 < end - start <= MAX_LEASE_SECONDS or not start <= now < end):
        raise RuntimeErrorCode('source_stale')
    if value['kind'] not in {'local', 'http'}:
        raise RuntimeErrorCode('invalid_source_kind')
    if not all(isinstance(value[k], str) and value[k] for k in ('bundle', 'original', 'mimir_origin')):
        raise RuntimeErrorCode('invalid_source_binding')
    origin = value['mimir_origin']
    parsed = urlsplit(origin)
    if parsed.username is not None or parsed.password is not None or any(ord(c) < 33 for c in origin):
        raise RuntimeErrorCode('invalid_origin')
    _reference(origin, 'probe.txt', 'section', 1, None)
    prefix = value['original_prefix']
    if not isinstance(prefix, str) or any(ord(c) < 32 or ord(c) == 127 for c in prefix) or (prefix and (prefix.startswith('/') or prefix.endswith('/') or
            any(p in {'', '.', '..'} or p.startswith('.') for p in prefix.split('/')))) or '\\' in prefix:
        raise RuntimeErrorCode('invalid_original_prefix')
    if value['kind'] == 'local':
        if not all(Path(value[k]).is_absolute() for k in ('bundle', 'original')):
            raise RuntimeErrorCode('absolute_local_binding_required')
    else:
        # Original reads and bundle reads share one configured Mímir origin.
        if value['original'] != origin.rstrip('/') + '/files' + ('/' + quote(prefix, safe='/') if prefix else ''):
            raise RuntimeErrorCode('original_binding_mismatch')
        bundle = urlsplit(value['bundle'])
        if (bundle.scheme, bundle.netloc) != (parsed.scheme, parsed.netloc):
            raise RuntimeErrorCode('source_origin_mismatch')
    return value


def source_for(handoff):
    if handoff['kind'] == 'local':
        return LocalSnapshotSource(handoff['bundle'], handoff['snapshot_id'], handoff['source_id'])
    return HttpSnapshotSource(handoff['bundle'], handoff['snapshot_id'], handoff['source_id'],
                              os.environ.get('MIMIR_API_KEY', ''),
                              access_client_id=os.environ.get('MIMIR_CF_ACCESS_CLIENT_ID', ''),
                              access_client_secret=os.environ.get('MIMIR_CF_ACCESS_CLIENT_SECRET', ''))


class Runtime:
    def __init__(self, state, handoff, clock=time.time):
        self.state = _private_store(Path(state))
        self.handoff = Path(handoff)
        self.clock = clock

    @contextlib.contextmanager
    def locked(self):
        fd = os.open(self.state / 'lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def job(self):
        path = self.state / 'job.json'
        value = private_json(path) if path.exists() else {'status': 'empty'}
        if (not isinstance(value, dict) or value.get('status') not in
                {'empty', 'pending', 'ready', 'failed', 'cancelled'}):
            raise RuntimeErrorCode('invalid_job_state')
        if 'snapshot_id' in value:
            pin = value['snapshot_id']
            if (not isinstance(pin, str) or not HEX.fullmatch(pin) or
                    value.get('index') != pin + '.sqlite' or
                    not isinstance(value.get('handoff'), dict)):
                raise RuntimeErrorCode('invalid_job_state')
            _uuid(value.get('source_id'))
            old = value['handoff']
            if (not {'source_id', 'kind', 'original', 'mimir_origin', 'original_prefix',
                     'observed_at', 'snapshot_id'} <= set(old) or
                    type(old['observed_at']) is not int or old['snapshot_id'] != pin or
                    old['source_id'] != value['source_id']):
                raise RuntimeErrorCode('invalid_job_state')
        elif any(key in value for key in ('index', 'source_id', 'handoff')) or value['status'] == 'ready':
            raise RuntimeErrorCode('invalid_job_state')
        if 'retry_after' in value or 'transient_failures' in value:
            if (value['status'] != 'pending' or type(value.get('retry_after')) not in (int, float) or
                    type(value.get('transient_failures')) is not int or value['transient_failures'] < 1):
                raise RuntimeErrorCode('invalid_job_state')
        return value

    @staticmethod
    def _transient(job):
        return isinstance(job.get('error'), str) and TRANSIENT_ERRORS.fullmatch(job['error']) is not None

    @staticmethod
    def _clear_retry(job):
        job.pop('retry_after', None)
        job.pop('transient_failures', None)

    def save(self, job):
        atomic_json(self.state / 'job.json', job)

    def lease(self, job=None):
        value = load_handoff(self.handoff, self.clock())
        old = (job or self.job()).get('handoff')
        if old and (any(old[k] != value[k] for k in ('source_id', 'kind', 'original', 'mimir_origin', 'original_prefix')) or
                    value['observed_at'] < old['observed_at'] or
                    value['observed_at'] == old['observed_at'] and value != old):
            raise RuntimeErrorCode('handoff_replay_or_scope_change')
        return value

    def index(self, job, create=False):
        from .runtime_index import SnapshotIndex
        return SnapshotIndex(self.state / job['index'], job['snapshot_id'], job['source_id'], create=create)

    def _budget(self):
        total = 0
        for path in self.state.iterdir():
            info = path.lstat()
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
        if total > MAX_STATE_BYTES:
            raise RuntimeErrorCode('state_budget_exceeded')

    def step(self):
        """One artifact per durable step; a crash leaves its transaction retryable."""
        with self.locked():
            job = self.job()
            if job['status'] == 'failed' and self._transient(job):
                # Jobs stranded by older releases on a transient outage resume.
                job['status'] = 'pending'
            if job['status'] in {'failed', 'cancelled'}:
                return job['status']
            if job['status'] == 'pending' and job.get('retry_after', 0) > self.clock():
                return 'pending'
            try:
                self._budget()
                handoff = self.lease(job)
                if job.get('snapshot_id') != handoff['snapshot_id']:
                    job = {'status': 'pending', 'snapshot_id': handoff['snapshot_id'],
                           'source_id': handoff['source_id'], 'handoff': handoff,
                           'index': handoff['snapshot_id'] + '.sqlite', 'error': None}
                    self.save(job)
                elif job['status'] == 'ready':
                    source_for(handoff)  # A renewed lease cannot mask an unavailable source.
                    index = self.index(job)
                    try:
                        if not index.stats()['complete']:
                            raise RuntimeErrorCode('index_incomplete')
                    finally:
                        index.close()
                    job['handoff'] = handoff
                    self._clear_retry(job)
                    job['error'] = None
                    self.save(job)
                    return 'ready'
                job['handoff'] = handoff
                job['status'] = 'pending'
                self.save(job)
                source = source_for(handoff)
                index = self.index(job, create=not (self.state / job['index']).exists())
                try:
                    completed = index.completed()
                    pending = [item for item in source.manifest['artifacts'] if item['artifact_id'] not in completed]
                    if pending:
                        item = pending[0]
                        index.put(item, source.read_object(item))
                    else:
                        index.finish(source.manifest)
                        job['status'] = 'ready'
                    job['counts'] = index.stats()
                finally:
                    index.close()
                # Source renewal or expiry mid-step must not mark a stale job ready.
                current = self.lease(job)
                if current['snapshot_id'] != job['snapshot_id']:
                    raise RuntimeErrorCode('source_changed')
                job['handoff'] = current
                self._clear_retry(job)
                job['error'] = None
                self._budget()
                self.save(job)
                return job['status']
            except (OSError, ValueError, sqlite3.DatabaseError) as error:
                # No exception text, URLs, paths, response bodies or source bytes in diagnostics.
                code = error.args[0] if isinstance(error, (RuntimeErrorCode, SnapshotError)) and error.args else None
                if isinstance(code, str) and TRANSIENT_ERRORS.fullmatch(code):
                    failures = job.get('transient_failures', 0) + 1
                    job['status'], job['error'] = 'pending', code
                    job['transient_failures'] = failures
                    job['retry_after'] = self.clock() + min(MAX_RETRY_SECONDS, MIN_RETRY_SECONDS * 2 ** min(failures - 1, 16))
                    self.save(job)
                    return 'pending'
                self._clear_retry(job)
                job['status'], job['error'] = 'failed', 'index_step_failed'
                if isinstance(code, str) and re.fullmatch('[a-z_0-9]{1,64}', code):
                    job['error'] = code
                self.save(job)
                return 'failed'

    def control(self, action):
        with self.locked():
            job = self.job()
            if action == 'cancel':
                job['status'] = 'cancelled'
            elif action in {'retry', 'rebuild'}:
                if action == 'rebuild' and 'index' in job:
                    suffix = '.saved-' + str(time.time_ns())
                    for sidecar in ('', '-journal', '-wal', '-shm'):
                        old = self.state / (job['index'] + sidecar)
                        if old.exists():
                            os.rename(old, self.state / (job['index'] + suffix + sidecar))
                job['status'], job['error'] = 'pending', None
                self._clear_retry(job)
            else:
                raise RuntimeErrorCode('invalid_action')
            self.save(job)

    def health(self):
        try:
            job = self.job()
        except (OSError, ValueError):
            job = {'status': 'unavailable', 'error': 'job_state_unreadable'}
        output = {k: job[k] for k in ('status', 'snapshot_id', 'source_id', 'counts', 'error') if k in job}
        output['fresh'] = False
        try:
            lease = self.lease(job)
            output['fresh'] = lease['snapshot_id'] == job.get('snapshot_id')
            output['observed_at'], output['expires_at'] = lease['observed_at'], lease['expires_at']
            if job['status'] == 'ready':
                index = self.index(job)
                try:
                    output['counts'] = index.stats()
                    if not output['counts']['complete']:
                        output['status'] = 'failed'
                finally:
                    index.close()
        except (OSError, ValueError, sqlite3.DatabaseError):
            output['status'] = 'unavailable'
        output['ready'] = output['status'] == 'ready' and output['fresh']
        output['readiness_scope'] = 'index_and_handoff; originals verified per retrieval'
        output['release'] = os.environ.get('MIMIR_SEARCH_RELEASE', 'development')
        output['package_sha256'] = os.environ.get('MIMIR_SEARCH_PACKAGE_SHA256')
        return output

    def _verify_originals(self, handoff, results):
        checked = set()
        # Rebind index hits to the pinned manifest, including local fixtures.
        reader = source_for(handoff)
        artifacts = {item['path']: item for item in reader.manifest['artifacts']}
        for item in results:
            artifact = artifacts.get(item['path'])
            if artifact is None or any(artifact[k] != item[k] for k in ('artifact_id', 'sha256')):
                raise RuntimeErrorCode('index_provenance_mismatch')
            if item['path'] in checked:
                continue
            if handoff['kind'] == 'local':
                data, _ = read_source(Path(handoff['original']), item['path'])
            else:
                # Use the same bounded no-proxy/no-redirect transport and pacing.
                original_url = handoff['original'].rstrip('/') + '/' + quote(item['path'], safe='/')
                data = _read_original(reader, original_url)
            if digest(data) != item['sha256']:
                raise RuntimeErrorCode('original_changed')
            checked.add(item['path'])

    def search(self, query, top=5):
        if not isinstance(query, str) or not query.strip() or len(query) > 4096 or type(top) is not int or not 1 <= top <= 5:
            raise RuntimeErrorCode('invalid_query')
        job = self.job()
        handoff = self.lease(job)
        if job['status'] != 'ready' or handoff['snapshot_id'] != job.get('snapshot_id'):
            raise RuntimeErrorCode('index_unavailable')
        index = self.index(job)
        try:
            if not index.stats()['complete']:
                raise RuntimeErrorCode('index_incomplete')
            results = index.search(query, top)
        finally:
            index.close()
        self._verify_originals(handoff, results)
        current = self.lease(job)
        if current['snapshot_id'] != job['snapshot_id'] or self.job()['status'] != 'ready':
            raise RuntimeErrorCode('source_changed')
        for item in results:
            path = '/'.join(part for part in (handoff['original_prefix'], item['path']) if part)
            locator = item['locator']
            item['mimir_url'] = _reference(handoff['mimir_origin'], path, locator['kind'], locator['number'], locator.get('anchor'))
            item['source_id'], item['snapshot_id'] = job['source_id'], job['snapshot_id']
            item['verification'] = 'original_sha256_matched'
        return {'mode': 'lexical', 'decision': 'fts5_bm25', 'results': results,
                'snapshot_id': job['snapshot_id'], 'source_id': job['source_id'],
                'observed_at': current['observed_at'], 'expires_at': current['expires_at'],
                'verified_at': self.clock(),
                'release': os.environ.get('MIMIR_SEARCH_RELEASE', 'development'),
                'package_sha256': os.environ.get('MIMIR_SEARCH_PACKAGE_SHA256'),
                'abstained': not bool(results)}


def _read_original(reader, url):
    # _get's object-only path guard remains intact for snapshot acquisition.
    from urllib.request import Request
    from urllib.error import HTTPError, URLError
    from http.client import HTTPException
    if reader.last_request is not None:
        time.sleep(max(0, 1.1 - (time.monotonic() - reader.last_request)))
    reader.last_request = time.monotonic()
    try:
        request = Request(url, headers=reader.headers)
        with reader.opener.open(request, timeout=reader.timeout) as response:
            if response.status != 200 or response.headers.get('Content-Encoding', 'identity') != 'identity':
                raise RuntimeErrorCode('original_unavailable')
            size = response.headers.get('Content-Length')
            if size is not None and (not size.isdecimal() or int(size) > MAX_BYTES):
                raise RuntimeErrorCode('original_unavailable')
            data = response.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES or size is not None and len(data) != int(size):
                raise RuntimeErrorCode('original_unavailable')
            return data
    except (OSError, HTTPError, URLError, HTTPException):
        raise RuntimeErrorCode('original_unavailable') from None


def handler(runtime, api_key):
    if not isinstance(api_key, str) or not 16 <= len(api_key) <= 4096 or any(ord(c) < 33 or ord(c) > 126 for c in api_key):
        raise RuntimeErrorCode('owner_credential_required')
    expected = ('Bearer ' + api_key).encode('ascii')

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *_args):
            pass  # Neither request paths nor client-supplied text belongs in logs.

        def reply(self, code, value):
            data = canonical(value)
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def authorized(self):
            values = self.headers.get_all('Authorization', [])
            supplied = values[0].encode('utf-8') if len(values) == 1 else b''
            if len(supplied) > 4103 or not hmac.compare_digest(supplied, expected):
                self.reply(401, {'error': 'unauthorized'})
                return False
            if self.headers.get('Host') != '%s:%s' % self.server.server_address:
                self.reply(403, {'error': 'invalid_host'})
                return False
            return True

        def do_GET(self):
            if not self.authorized():
                return
            if self.path not in {'/health', '/index'}:
                self.reply(404, {'error': 'not_found'})
                return
            status = runtime.health()
            self.reply(200 if status['ready'] else 503, status)

        def do_POST(self):
            if not self.authorized():
                return
            if self.path != '/search':
                self.reply(404, {'error': 'not_found'})
                return
            try:
                lengths = self.headers.get_all('Content-Length', [])
                if (len(lengths) != 1 or not lengths[0].isdecimal() or
                        not 0 < int(lengths[0]) <= 8192 or self.headers.get('Transfer-Encoding')):
                    raise RuntimeErrorCode('invalid_request')
                body = json.loads(self.rfile.read(int(lengths[0])))
                if not isinstance(body, dict) or set(body) - {'query', 'top'} or 'query' not in body:
                    raise RuntimeErrorCode('invalid_request')
                query, top = body['query'], body.get('top', 5)
                if (not isinstance(query, str) or not query.strip() or len(query) > 4096 or
                        type(top) is not int or not 1 <= top <= 5):
                    raise RuntimeErrorCode('invalid_request')
            except (ValueError, OSError, UnicodeError):
                self.reply(400, {'error': 'invalid_request'})
                return
            try:
                self.reply(200, runtime.search(body['query'], body.get('top', 5)))
            except (OSError, ValueError, sqlite3.DatabaseError):
                self.reply(503, {'error': 'retrieval_unavailable'})

    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['serve', 'step', 'status', 'cancel', 'retry', 'rebuild'])
    parser.add_argument('--state', required=True, type=Path)
    parser.add_argument('--handoff', required=True, type=Path)
    parser.add_argument('--port', type=int, default=8791)
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        runtime = Runtime(args.state, args.handoff)
        if args.action == 'serve':
            if not 1 <= args.port <= 65535:
                raise RuntimeErrorCode('invalid_port')
            # Single request at a time bounds memory and upstream source reads.
            server = HTTPServer(('127.0.0.1', args.port), handler(runtime, os.environ.get('MIMIR_SEARCH_API_KEY', '')))
            server.timeout = 1
            child = None
            started = 0
            next_step = 0
            def stop(_signal, _frame):
                raise KeyboardInterrupt
            signal.signal(signal.SIGTERM, stop)
            try:
                while True:
                    server.handle_request()
                    if child is not None and child.poll() is None:
                        if time.monotonic() - started > 90:
                            os.killpg(child.pid, signal.SIGKILL)
                            child.wait()
                            runtime.control('cancel')
                        continue
                    if child is not None:
                        if child.returncode != 0 and runtime.job()['status'] != 'failed':
                            runtime.control('cancel')
                        child = None
                        job = runtime.job()
                        delay = 60 if job['status'] == 'ready' else 1.1
                        if 'retry_after' in job:
                            delay = max(delay, min(MAX_RETRY_SECONDS, job['retry_after'] - time.time()))
                        next_step = time.monotonic() + delay
                    job = runtime.job()
                    if ((job['status'] not in {'failed', 'cancelled'} or job['status'] == 'failed' and runtime._transient(job))
                            and time.monotonic() >= next_step):
                        entry = [sys.argv[0]] if sys.argv[0].endswith('.pyz') else ['-m', __package__ + '.runtime']
                        child = subprocess.Popen([sys.executable, *entry, 'step',
                                                  '--state', str(args.state), '--handoff', str(args.handoff)],
                                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                                 stderr=subprocess.DEVNULL, start_new_session=True)
                        started = time.monotonic()
            finally:
                if child is not None and child.poll() is None:
                    os.killpg(child.pid, signal.SIGTERM)
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL)
                        child.wait()
                server.server_close()
        elif args.action == 'step':
            os.nice(10)
            import resource
            resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
            resource.setrlimit(resource.RLIMIT_FSIZE, (128 * 1024 * 1024, 128 * 1024 * 1024))
            if sys.platform.startswith('linux'):
                resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
            return 1 if runtime.step() == 'failed' else 0
        elif args.action == 'status':
            print(json.dumps(runtime.health()))
        else:
            runtime.control(args.action)
        return 0
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, sqlite3.DatabaseError):
        print(json.dumps({'error': 'runtime_operation_failed'}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
