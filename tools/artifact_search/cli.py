"""JSON-only local CLI. No HTTP server, original-file writes, or model calls."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

from .core import index_corpus, search
from .evaluate import evaluate


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    indexing = commands.add_parser('index', help='Synchronize a complete allowlisted corpus manifest')
    indexing.add_argument('--root', type=Path, required=True)
    indexing.add_argument('--db', type=Path, required=True)
    indexing.add_argument('--manifest', type=Path, required=True)
    finding = commands.add_parser('search', help='Return hash-verified source locations')
    finding.add_argument('--db', type=Path, required=True)
    finding.add_argument('--query', help='Query text; omit to read it from stdin')
    finding.add_argument('--top', type=int, default=5)
    finding.add_argument('--base-url', help='Credential-free HTTPS Mimir origin; references only, no HTTP')
    evaluating = commands.add_parser('evaluate', help='Measure a labeled query set without emitting source text')
    evaluating.add_argument('--db', type=Path, required=True)
    evaluating.add_argument('--cases', type=Path, required=True)
    evaluating.add_argument('--top', type=int, default=5)
    args = parser.parse_args(argv)
    try:
        if args.command == 'index':
            paths = json.loads(args.manifest.read_text(encoding='utf-8'))
            result = index_corpus(args.root, args.db, paths)
        elif args.command == 'search':
            query = args.query if args.query is not None else sys.stdin.read(4097)
            result = search(args.db, query.strip(), args.top, args.base_url)
        else:
            cases = json.loads(args.cases.read_text(encoding='utf-8'))
            result = evaluate(args.db, cases, args.top)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, sqlite3.Error) as error:
        # No source bytes, query, secret values, or extractor stderr in diagnostics.
        reason = str(error) if isinstance(error, ValueError) else type(error).__name__
        print(json.dumps({'error': reason}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
