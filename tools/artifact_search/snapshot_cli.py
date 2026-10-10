"""JSON-only source adapter CLI. Credentials come only from explicit environment."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .snapshot import (LocalSnapshotSource, SnapshotError, acquire_snapshot,
                       create_snapshot, diff_manifests)
from .snapshot_http import HttpSnapshotSource


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('create', help='Capture a complete selected local corpus')
    create.add_argument('--root', type=Path, required=True)
    create.add_argument('--store', type=Path, required=True)
    create.add_argument('--source-id', required=True, help='Stable owner-assigned UUID for this corpus scope')
    create.add_argument('--previous', type=Path, help='Previously accepted content-addressed manifest.json')
    create.add_argument('--allow-empty', action='store_true', help='Explicitly allow complete empty corpus')
    for name in ('acquire-local', 'acquire-http'):
        command = commands.add_parser(name, help='Verify every object before accepting a pinned snapshot')
        command.add_argument('--store', type=Path, required=True)
        command.add_argument('--source-id', required=True)
        command.add_argument('--snapshot-id', required=True, help='Expected manifest byte SHA-256 from trusted publisher')
        if name == 'acquire-local':
            command.add_argument('--bundle', type=Path, required=True)
    delta = commands.add_parser('diff', help='Compare two pinned complete snapshots without changing state')
    delta.add_argument('--before', type=Path, required=True)
    delta.add_argument('--after', type=Path, required=True)
    delta.add_argument('--source-id', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'create':
            result = create_snapshot(args.root, args.store, args.source_id, args.previous, args.allow_empty)
        elif args.command == 'diff':
            old = LocalSnapshotSource(args.before.parent, args.before.parent.name, args.source_id)
            new = LocalSnapshotSource(args.after.parent, args.after.parent.name, args.source_id)
            result = diff_manifests(old.manifest, new.manifest)
        else:
            source = (LocalSnapshotSource(args.bundle, args.snapshot_id, args.source_id)
                      if args.command == 'acquire-local'
                      else HttpSnapshotSource.from_env(args.snapshot_id, args.source_id))
            result = acquire_snapshot(source, args.store)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError) as error:
        diagnostic = {'error': str(error) if isinstance(error, SnapshotError) else type(error).__name__}
        if isinstance(error, SnapshotError) and error.artifact is not None:
            diagnostic['artifact'] = error.artifact
        print(json.dumps(diagnostic), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
