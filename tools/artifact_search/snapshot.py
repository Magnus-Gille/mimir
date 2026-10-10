"""Complete, hash-pinned artifact snapshots. No search, model or source writes."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import uuid

from .core import MAX_BYTES, has_secret
from .snapshot_delta import assign_ids, diff_manifests

SCHEMA = 'mimir-artifact-snapshot-v1'
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_ARTIFACTS = 10000
MAX_TOTAL_BYTES = 256 * 1024 * 1024
SUFFIXES = {'.html', '.htm', '.pdf', '.md', '.txt', '.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg'}
POLICY = {'version': 'snapshot-policy-1:known-secrets-1',
          'suffixes': sorted(SUFFIXES), 'max_file_bytes': MAX_BYTES}
REASONS = {'excluded_path', 'unsupported_format', 'known_secret_format'}
HEX = re.compile(r'^[0-9a-f]{64}$')


class SnapshotError(ValueError):
    def __init__(self, reason, artifact=None):
        super().__init__(reason)
        self.artifact = artifact


def canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                       allow_nan=False) + '\n').encode('utf-8')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def _uuid(value):
    if not isinstance(value, str):
        raise SnapshotError('invalid_identity')
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError()
    except ValueError:
        raise SnapshotError('invalid_identity') from None
    return value


def _path(value, protected=True):
    if (not isinstance(value, str) or not value or len(value) > 2048 or
            '\\' in value or any(ord(c) < 32 or ord(c) == 127 for c in value) or
            len(value.split('/')) > 64 or any(p in {'', '.', '..'} for p in value.split('/'))):
        raise SnapshotError('invalid_path')
    try:
        value.encode('utf-8')
    except UnicodeError:
        raise SnapshotError('invalid_path') from None
    if protected and any(p.startswith('.') or p.casefold() in {'secrets', 'credentials', 'node_modules'}
                         for p in value.split('/')):
        raise SnapshotError('excluded_path')
    return value


def load_manifest(data, expected_sha256, source_id):
    """Validate a complete schema and pinned bytes before making any fetch request."""
    _uuid(source_id)
    if not isinstance(expected_sha256, str) or not HEX.fullmatch(expected_sha256):
        raise SnapshotError('invalid_manifest_pin')
    if (not isinstance(data, bytes) or len(data) > MAX_MANIFEST_BYTES or
            digest(data) != expected_sha256):
        raise SnapshotError('manifest_hash_mismatch')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise SnapshotError('duplicate_manifest_key')
            result[key] = value
        return result
    try:
        manifest = json.loads(data, object_pairs_hook=unique)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise SnapshotError('invalid_manifest') from None
    if (not isinstance(manifest, dict) or
            set(manifest) != {'schema', 'source_id', 'complete', 'policy', 'artifacts', 'excluded'} or
            manifest['schema'] != SCHEMA or manifest['complete'] is not True or
            manifest['source_id'] != source_id or manifest['policy'] != POLICY or
            not isinstance(manifest['artifacts'], list) or not isinstance(manifest['excluded'], list) or
            len(manifest['artifacts']) + len(manifest['excluded']) > MAX_ARTIFACTS):
        raise SnapshotError('invalid_manifest_contract')
    paths, identifiers = set(), set()
    total = 0
    for item in manifest['artifacts']:
        if not isinstance(item, dict) or set(item) != {'artifact_id', 'path', 'size', 'mtime_ns', 'sha256', 'object'}:
            raise SnapshotError('invalid_artifact')
        path = _path(item['path'])
        identifier = _uuid(item['artifact_id'])
        sha = item['sha256']
        if (Path(path).suffix.lower() not in SUFFIXES or
                not isinstance(sha, str) or not HEX.fullmatch(sha) or
                item['object'] != 'objects/' + sha or
                type(item['size']) is not int or not 0 <= item['size'] <= MAX_BYTES or
                type(item['mtime_ns']) is not int or item['mtime_ns'] < 0 or
                path in paths or identifier in identifiers):
            raise SnapshotError('invalid_artifact', path)
        paths.add(path)
        identifiers.add(identifier)
        total += item['size']
    for item in manifest['excluded']:
        if not isinstance(item, dict) or set(item) != {'path', 'reason'}:
            raise SnapshotError('invalid_exclusion')
        path = _path(item['path'], protected=False)
        if not isinstance(item['reason'], str) or item['reason'] not in REASONS or path in paths:
            raise SnapshotError('invalid_exclusion', path)
        paths.add(path)
    if (total > MAX_TOTAL_BYTES or
            manifest['artifacts'] != sorted(manifest['artifacts'], key=lambda x: x['path']) or
            manifest['excluded'] != sorted(manifest['excluded'], key=lambda x: x['path']) or
            canonical(manifest) != data):
        raise SnapshotError('noncanonical_or_oversized_manifest')
    return manifest


def _fingerprint(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read(root, relative, limit=MAX_BYTES):
    """Descriptor-relative, no-follow, bounded reads from a trusted root."""
    parts = _path(relative).split('/')
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
                raise SnapshotError('invalid_or_oversized_object', relative)
            data = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
            if len(data) != before.st_size or len(data) > limit or _fingerprint(before) != _fingerprint(after):
                raise SnapshotError('source_changed_during_read', relative)
            return data, after
    except OSError:
        raise SnapshotError('source_read_failed', relative) from None
    finally:
        os.close(directory)


def _inventory(root):
    observations, files, excluded = {}, [], []
    count = 0
    def names(directory):
        # Bound enumeration before sorting, including paths excluded by policy.
        values = []
        with os.scandir(directory) as stream:
            for entry in stream:
                values.append(entry.name)
                if len(values) > MAX_ARTIFACTS:
                    raise SnapshotError('inventory_limit_exceeded')
        return sorted(values)
    def walk(directory, prefix):
        nonlocal count
        observations[prefix] = _fingerprint(os.fstat(directory))
        for name in names(directory):
            count += 1
            if count > MAX_ARTIFACTS:
                raise SnapshotError('inventory_limit_exceeded')
            path = prefix + '/' + name if prefix else name
            _path(path, protected=False)
            if name.startswith('.') or name.casefold() in {'secrets', 'credentials', 'node_modules'}:
                excluded.append({'path': path, 'reason': 'excluded_path'})
                continue
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            observations[path] = _fingerprint(info)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                try:
                    walk(child, path)
                finally:
                    os.close(child)
            elif not stat.S_ISREG(info.st_mode):
                raise SnapshotError('source_not_regular', path)
            elif Path(path).suffix.lower() not in SUFFIXES:
                excluded.append({'path': path, 'reason': 'unsupported_format'})
            elif info.st_size > MAX_BYTES:
                raise SnapshotError('oversized_source', path)
            else:
                files.append(path)
    try:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            walk(directory, '')
        finally:
            os.close(directory)
    except OSError:
        raise SnapshotError('incomplete_inventory') from None
    return observations, files, excluded


class LocalSnapshotSource:
    def __init__(self, snapshot_dir, expected_sha256, source_id):
        self.root = Path(snapshot_dir)
        data, _ = _read(self.root, 'manifest.json', MAX_MANIFEST_BYTES)
        self.manifest_bytes = data
        self.manifest = load_manifest(data, expected_sha256, source_id)
        self.snapshot_id = expected_sha256

    def read_object(self, item):
        return _read(self.root, item['object'])[0]


def _verified_objects(source, manifest=None):
    objects = {}
    for item in (manifest or source.manifest)['artifacts']:
        sha = item['sha256']
        if sha not in objects:
            data = source.read_object(item)
            if not isinstance(data, bytes) or len(data) != item['size'] or digest(data) != sha:
                raise SnapshotError('object_hash_mismatch', item['path'])
            if has_secret(data.decode('utf-8', errors='ignore')):
                raise SnapshotError('known_secret_format', item['path'])
            objects[sha] = data
        elif len(objects[sha]) != item['size']:
            raise SnapshotError('object_size_mismatch', item['path'])
    return objects


def _private_store(store):
    store = Path(store)
    store.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = store.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
        raise SnapshotError('store_must_be_private_directory')
    return store


def _write(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _publish(store, data, objects, manifest, delta):
    """Exclusive files; manifest is the final commit marker, never overwritten."""
    store = _private_store(store)
    snapshot_id = digest(data)
    target = store / snapshot_id
    if target.exists() or target.is_symlink():
        existing = LocalSnapshotSource(target, snapshot_id, manifest['source_id'])
        _verified_objects(existing)
    else:
        stage = Path(tempfile.mkdtemp(prefix='.pending-', dir=store))
        try:
            (stage / 'objects').mkdir(mode=0o700)
            for sha, value in objects.items():
                _write(stage / 'objects' / sha, value)
            _write(stage / 'manifest.json', data)
            _sync_dir(stage / 'objects')
            _sync_dir(stage)
            # Reserve without replace semantics. A collision remains untouched.
            try:
                target.mkdir(mode=0o700)
            except FileExistsError:
                raise SnapshotError('snapshot_collision') from None
            (target / 'objects').mkdir(mode=0o700)
            for sha in objects:
                os.link(stage / 'objects' / sha, target / 'objects' / sha, follow_symlinks=False)
            _sync_dir(target / 'objects')
            os.link(stage / 'manifest.json', target / 'manifest.json', follow_symlinks=False)
            _sync_dir(target)
            _sync_dir(store)
        finally:
            shutil.rmtree(stage)
    return {'snapshot_id': snapshot_id, 'manifest_path': str(target / 'manifest.json'),
            'artifacts': len(manifest['artifacts']), 'excluded': len(manifest['excluded']), 'delta': delta}


def create_snapshot(root, store, source_id, previous=None, allow_empty=False):
    _uuid(source_id)
    root, store = Path(root), Path(store)
    if root.is_symlink():
        raise SnapshotError('source_root_symlink')
    try:
        root = root.resolve(strict=True)
    except OSError:
        raise SnapshotError('source_unavailable') from None
    resolved = store.resolve()
    if root == resolved or root in resolved.parents or resolved in root.parents:
        raise SnapshotError('store_must_be_outside_source')
    old = None
    if previous is not None:
        previous = Path(previous)
        if previous.name != 'manifest.json':
            raise SnapshotError('invalid_previous_snapshot')
        old = LocalSnapshotSource(previous.parent, previous.parent.name, source_id).manifest
    before, paths, excluded = _inventory(root)
    entries, objects, total = [], {}, 0
    for path in paths:
        data, info = _read(root, path)
        if _fingerprint(info) != before[path]:
            raise SnapshotError('source_changed_during_inventory', path)
        if has_secret(data.decode('utf-8', errors='ignore')):
            excluded.append({'path': path, 'reason': 'known_secret_format'})
            continue
        sha = digest(data)
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise SnapshotError('snapshot_byte_limit_exceeded')
        entries.append({'path': path, 'size': len(data), 'mtime_ns': info.st_mtime_ns,
                        'sha256': sha, 'object': 'objects/' + sha})
        objects[sha] = data
    after, _, _ = _inventory(root)
    if before != after:
        raise SnapshotError('source_changed_during_inventory')
    if not entries and not allow_empty:
        raise SnapshotError('empty_snapshot_requires_opt_in')
    manifest = {'schema': SCHEMA, 'complete': True, 'source_id': source_id, 'policy': POLICY,
                'artifacts': assign_ids(source_id, entries, old),
                'excluded': sorted(excluded, key=lambda x: x['path'])}
    data = canonical(manifest)
    load_manifest(data, digest(data), source_id)
    return _publish(store, data, objects, manifest, diff_manifests(old, manifest))


def acquire_snapshot(source, store):
    """All-or-nothing acquisition of a pinned local or HTTP snapshot bundle."""
    if isinstance(source, LocalSnapshotSource):
        root, destination = source.root.resolve(), Path(store).resolve()
        if root == destination or root in destination.parents:
            raise SnapshotError('store_must_be_outside_source')
    manifest = load_manifest(source.manifest_bytes, source.snapshot_id, source.manifest['source_id'])
    objects = _verified_objects(source, manifest)
    return _publish(store, source.manifest_bytes, objects, manifest, diff_manifests(None, manifest))
