"""Explicit selected-corpus publication and trusted HTTPS handoff refresh.

These commands change only the configured private operational paths. They do not
install services, provision credentials, edit original files or prune snapshots.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import Request, ProxyHandler, build_opener

from . import embedding_revision
from .core import has_secret, safe_relative
from .runtime import atomic_json, load_handoff, private_json, source_for
from .snapshot import (SCHEMA, POLICY, MAX_TOTAL_BYTES, LocalSnapshotSource, _fingerprint,
                       _private_store, _publish, _read, _uuid, _verified_objects,
                       assign_ids, canonical, diff_manifests, digest, load_manifest)
from .snapshot_http import _NoRedirect, source_headers

LEASE_SECONDS = 1800
STORE_LIMIT = 512 * 1024 * 1024


def config(path, role):
    value = private_json(path)
    common = {'schema', 'origin', 'source_id', 'paths', 'publish_prefix', 'handoff'}
    publisher = {'archive_root', 'root_device', 'root_inode', 'publish_store'}
    if (not isinstance(value, dict) or set(value) != common | (publisher if role == 'publish' else set()) or
            value.get('schema') != 'mimir-search-operations-v1'):
        raise ValueError('invalid_operations_config')
    _uuid(value['source_id'])
    origin = value['origin']
    if not isinstance(origin, str) or not re.fullmatch(r'https://[A-Za-z0-9.-]+(?::[0-9]+)?', origin):
        raise ValueError('https_origin_required')
    parsed = urlsplit(origin)
    if not parsed.hostname or parsed.port == 0:
        raise ValueError('invalid_origin')
    paths = value['paths']
    if (not isinstance(paths, list) or not 1 <= len(paths) <= 32 or
            not all(isinstance(p, str) for p in paths) or paths != sorted(set(paths))):
        raise ValueError('explicit_sorted_paths_required')
    for item in paths:
        safe_relative(item)
    prefix = value['publish_prefix']
    if not isinstance(prefix, str) or not re.fullmatch(r'artifact-snapshots/[A-Za-z0-9_-]+', prefix):
        raise ValueError('invalid_publish_prefix')
    if any(p == prefix or p.startswith(prefix + '/') for p in paths):
        raise ValueError('source_store_overlap')
    for key in ('handoff', 'archive_root', 'publish_store'):
        if key in value and (not isinstance(value[key], str) or not Path(value[key]).is_absolute()):
            raise ValueError('absolute_paths_required')
    if role == 'publish':
        if any(type(value[k]) is not int or value[k] < 0 for k in ('root_device', 'root_inode')):
            raise ValueError('source_root_identity_required')
        if Path(value['publish_store']) != Path(value['archive_root']) / prefix:
            raise ValueError('store_binding_mismatch')
        if Path(value['handoff']) != Path(value['publish_store']) / 'handoff.json':
            raise ValueError('handoff_binding_mismatch')
    return value


@contextlib.contextmanager
def locked(directory):
    directory = _private_store(directory)
    fd = os.open(directory / '.operations.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def binding(c, snapshot_id, observed):
    return {'schema': 'mimir-search-handoff-v1', 'source_id': c['source_id'],
            'snapshot_id': snapshot_id, 'observed_at': observed,
            'expires_at': observed + LEASE_SECONDS, 'kind': 'http',
            'bundle': c['origin'] + '/files/' + c['publish_prefix'] + '/' + snapshot_id,
            'original': c['origin'] + '/files', 'mimir_origin': c['origin'],
            'original_prefix': ''}


def check_binding(c, handoff):
    wanted = binding(c, handoff['snapshot_id'], handoff['observed_at'])
    if handoff != wanted:
        raise ValueError('handoff_scope_mismatch')


def check_manifest(c, manifest):
    if ([a['path'] for a in manifest['artifacts']] != c['paths'] or manifest['excluded']):
        raise ValueError('selected_corpus_incomplete')


def publish(c):
    root, store = Path(c['archive_root']), Path(c['publish_store'])
    # Refuse a replacement mount/root, including symlink swaps.
    info = root.lstat()
    if root.is_symlink() or (info.st_dev, info.st_ino) != (c['root_device'], c['root_inode']):
        raise ValueError('source_root_replaced')
    with locked(store):
        total_stored = sum(p.lstat().st_size for p in store.rglob('*') if p.is_file())
        if total_stored > STORE_LIMIT:
            raise ValueError('publisher_store_limit')
        observed = int(time.time())
        entries, objects, seen = [], {}, {}
        total_bytes = 0
        for path in c['paths']:
            data, info = _read(root, path)
            total_bytes += len(data)
            if total_bytes > MAX_TOTAL_BYTES:
                raise ValueError('selected_corpus_too_large')
            if has_secret(data.decode('utf-8', errors='ignore')):
                raise ValueError('selected_source_secret_detected')
            sha = digest(data)
            seen[path] = (_fingerprint(info), sha)
            objects[sha] = data
            entries.append({'path': path, 'size': len(data), 'mtime_ns': info.st_mtime_ns,
                            'sha256': sha, 'object': 'objects/' + sha})
        # Re-observe every selected original; never refresh from cached objects.
        for path in c['paths']:
            data, info = _read(root, path)
            if (_fingerprint(info), digest(data)) != seen[path]:
                raise ValueError('source_changed_during_observation')
        current_root = root.lstat()
        if (current_root.st_dev, current_root.st_ino) != (c['root_device'], c['root_inode']):
            raise ValueError('source_root_replaced')
        old = None
        handoff_path = Path(c['handoff'])
        if handoff_path.exists():
            previous = private_json(handoff_path)
            check_binding(c, previous)
            if observed < previous['observed_at']:
                raise ValueError('observation_moved_backwards')
            old = LocalSnapshotSource(store / previous['snapshot_id'], previous['snapshot_id'], c['source_id']).manifest
            check_manifest(c, old)
        manifest = {'schema': SCHEMA, 'source_id': c['source_id'], 'complete': True, 'policy': POLICY,
                    'artifacts': assign_ids(c['source_id'], entries, old), 'excluded': []}
        raw = canonical(manifest)
        load_manifest(raw, digest(raw), c['source_id'])
        if (not (store / digest(raw)).exists() and
                total_stored + sum(len(v) for v in objects.values()) + len(raw) + 4096 > STORE_LIMIT):
            raise ValueError('publisher_store_limit')
        result = _publish(store, raw, objects, manifest, diff_manifests(old, manifest))
        handoff = binding(c, result['snapshot_id'], observed)
        if time.time() >= handoff['expires_at']:
            raise ValueError('observation_expired')
        atomic_json(handoff_path, handoff)
        return {'snapshot_id': result['snapshot_id'], 'source_id': c['source_id'],
                'artifacts': len(entries), 'expires_at': handoff['expires_at']}


def refresh(c):
    target = Path(c['handoff'])
    with locked(target.parent):
        url = c['origin'] + '/files/' + c['publish_prefix'] + '/handoff.json'
        headers = source_headers(url, os.environ.get('MIMIR_API_KEY', ''),
                                 os.environ.get('MIMIR_CF_ACCESS_CLIENT_ID', ''),
                                 os.environ.get('MIMIR_CF_ACCESS_CLIENT_SECRET', ''))
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        with opener.open(Request(url, headers=headers), timeout=15) as response:
            if response.status != 200 or response.headers.get('Content-Encoding', 'identity') != 'identity':
                raise ValueError('handoff_unavailable')
            raw = response.read(16385)
            if len(raw) > 16384:
                raise ValueError('handoff_too_large')
        fd, temporary = tempfile.mkstemp(prefix='handoff-check-', dir=target.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
            handoff = load_handoff(temporary)
            check_binding(c, handoff)
            if target.exists():
                old = private_json(target)
                check_binding(c, old)
                if handoff['observed_at'] < old['observed_at']:
                    raise ValueError('observation_moved_backwards')
            # Bind the remotely fetched observation to the locally approved scope.
            source = source_for(handoff)
            check_manifest(c, source.manifest)
            _verified_objects(source)
            if time.time() >= handoff['expires_at']:
                raise ValueError('observation_expired')
            atomic_json(target, handoff)
            return {'snapshot_id': handoff['snapshot_id'], 'source_id': c['source_id'],
                    'artifacts': len(c['paths']), 'expires_at': handoff['expires_at']}
        finally:
            Path(temporary).unlink()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] and argv[0] in embedding_revision.ACTIONS:
        return embedding_revision.main(argv)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['publish', 'refresh'])
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        c = config(args.config, args.action)
        print(json.dumps(publish(c) if args.action == 'publish' else refresh(c)))
        return 0
    except (OSError, ValueError, KeyError) as error:
        # Never echo remote response bodies, source text, config or credentials.
        print(json.dumps({'error': 'operations_failed', 'error_type': type(error).__name__}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
