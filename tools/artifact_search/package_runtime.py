"""Build or verify a reproducible standalone zipapp from an explicit Git commit."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import time
import uuid
import zipfile

MODULES = ('__init__.py', 'core.py', 'extract.py', 'snapshot.py', 'snapshot_delta.py',
           'snapshot_http.py', 'runtime_index.py', 'runtime.py', 'operations.py',
           'vectors.py', 'embedding_client.py', 'runtime_vectors.py',
           'embedding_revision.py')
SHA = re.compile(r'^[0-9a-f]{40}$')
BOOT = '''import hashlib, json, os, sys, zipfile
with zipfile.ZipFile(sys.argv[0]) as package:
    release = json.loads(package.read('release.json'))
expected = os.environ.get('MIMIR_SEARCH_RELEASE', '')
if expected != release['release']:
    sys.exit('MIMIR_SEARCH_RELEASE must equal the approved immutable package release')
os.environ['MIMIR_SEARCH_PACKAGE_SHA256'] = hashlib.sha256(open(sys.argv[0], 'rb').read()).hexdigest()
if sys.argv[1:2] == ['ops']:
    del sys.argv[1]
    from tools.artifact_search.operations import main
else:
    from tools.artifact_search.runtime import main
raise SystemExit(main())
'''


def build(repo, revision):
    if not SHA.fullmatch(revision):
        raise ValueError('exact_40_character_release_required')
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.DEVNULL)
    if git('rev-parse', '--verify', revision + '^{commit}').decode().strip() != revision:
        raise ValueError('release_not_commit')
    files = {'tools/__init__.py': b'', '__main__.py': BOOT.encode()}
    for name in MODULES:
        path = 'tools/artifact_search/' + name
        files[path] = git('show', revision + ':' + path)
    files['release.json'] = json.dumps({'schema': 'mimir-search-release-v1', 'release': revision,
        'files': {path: hashlib.sha256(data).hexdigest() for path, data in files.items()}},
        sort_keys=True, separators=(',', ':')).encode() + b'\n'
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED) as archive:
        for path, data in sorted(files.items()):
            info = zipfile.ZipInfo(path, (1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o600 << 16
            archive.writestr(info, data)
    return output.getvalue()


def verify(data, revision, sha256, health=None, *, retrieval=None,
           expected_snapshot=None, expected_source=None):
    if not SHA.fullmatch(revision) or hashlib.sha256(data).hexdigest() != sha256:
        raise ValueError('release_package_mismatch')
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if len(archive.namelist()) != len(set(archive.namelist())):
            raise ValueError('duplicate_package_entries')
        metadata = json.loads(archive.read('release.json'))
        if metadata.get('schema') != 'mimir-search-release-v1' or metadata.get('release') != revision:
            raise ValueError('release_identity_mismatch')
        files = metadata['files']
        if set(archive.namelist()) != set(files) | {'release.json'}:
            raise ValueError('package_inventory_mismatch')
        for path, digest in files.items():
            if hashlib.sha256(archive.read(path)).hexdigest() != digest:
                raise ValueError('package_content_mismatch')
    if health is not None:
        if (not isinstance(health, dict) or not isinstance(retrieval, dict) or
                not isinstance(expected_snapshot, str) or not re.fullmatch('[0-9a-f]{64}', expected_snapshot) or
                not isinstance(expected_source, str) or str(uuid.UUID(expected_source)) != expected_source or
                health.get('ready') is not True):
            raise ValueError('retrieval_acceptance_required')
        for receipt in (health, retrieval):
            if any(receipt.get(key) != expected for key, expected in
                   (('release', revision), ('package_sha256', sha256),
                    ('snapshot_id', expected_snapshot), ('source_id', expected_source))):
                raise ValueError('deployment_not_accepted')
        now = time.time()
        observed, expires, checked = (retrieval.get(key) for key in ('observed_at', 'expires_at', 'verified_at'))
        if (type(observed) is not int or type(expires) is not int or type(checked) not in (int, float) or
                not observed <= checked <= now < expires or not 0 <= now - checked <= 30 or
                not 0 < expires - observed <= 3600 or
                (observed, expires) != (health.get('observed_at'), health.get('expires_at'))):
            raise ValueError('stale_retrieval_receipt')
        hits = retrieval.get('results')
        if (not isinstance(hits, list) or not 1 <= len(hits) <= 5 or
                any(not isinstance(item, dict) or item.get('verification') != 'original_sha256_matched' or
                    not isinstance(item.get('sha256'), str) or not re.fullmatch('[0-9a-f]{64}', item['sha256'])
                    for item in hits)):
            raise ValueError('successful_retrieval_required')
    elif retrieval is not None:
        raise ValueError('health_acceptance_required')
    return {'release': revision, 'sha256': sha256, 'verified': True,
            'deployment_accepted': health is not None}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['build', 'verify'])
    parser.add_argument('--release', required=True)
    parser.add_argument('--package', required=True, type=Path)
    parser.add_argument('--repo', type=Path, default=Path('.'))
    parser.add_argument('--sha256')
    parser.add_argument('--health-json', type=Path)
    parser.add_argument('--retrieval-json', type=Path)
    parser.add_argument('--snapshot-id')
    parser.add_argument('--source-id')
    args = parser.parse_args(argv)
    try:
        if args.action == 'build':
            data = build(args.repo, args.release)
            with args.package.open('xb') as stream:
                stream.write(data)
            sha = hashlib.sha256(data).hexdigest()
        else:
            data, sha = args.package.read_bytes(), args.sha256
        health = json.loads(args.health_json.read_text()) if args.health_json else None
        retrieval = json.loads(args.retrieval_json.read_text()) if args.retrieval_json else None
        print(json.dumps(verify(data, args.release, sha, health, retrieval=retrieval,
                               expected_snapshot=args.snapshot_id, expected_source=args.source_id)))
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError, zipfile.BadZipFile):
        print(json.dumps({'error': 'package_verification_failed'}))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
