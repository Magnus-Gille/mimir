"""JSON-only local CLI. No HTTP server, original-file writes, or model calls."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

from .core import index_corpus, search
from .evaluate import evaluate
from .embedding_client import M5Embedder, EmbedderUnavailable
from .vectors import EmbeddingIdentity
from .semantic import build_vectors, semantic_search


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
    finding.add_argument('--mode', choices=('lexical', 'vector', 'hybrid'), default='lexical')
    finding.add_argument('--min-score', type=float, default=0.0, help='Cosine floor, not an answerability guarantee')
    evaluating = commands.add_parser('evaluate', help='Measure a labeled query set without emitting source text')
    evaluating.add_argument('--db', type=Path, required=True)
    evaluating.add_argument('--cases', type=Path, required=True)
    evaluating.add_argument('--top', type=int, default=5)
    embedding = commands.add_parser('embed', help='Explicitly send current allowlisted text to private M5 and cache vectors')
    embedding.add_argument('--db', type=Path, required=True)
    comparing = commands.add_parser('compare', help='Compare lexical/vector/hybrid on one fixed query set')
    comparing.add_argument('--db', type=Path, required=True)
    comparing.add_argument('--cases', type=Path, required=True)
    comparing.add_argument('--top', type=int, default=5)
    comparing.add_argument('--min-score', type=float, default=0.0)
    for command in (finding, embedding, comparing):
        command.add_argument('--embedding-revision', help='Gateway release SHA:model-artifact SHA256; required for M5 calls')
        command.add_argument('--dimensions', type=int, choices=(128, 256, 512, 768), default=256)
    args = parser.parse_args(argv)
    try:
        if args.command == 'index':
            paths = json.loads(args.manifest.read_text(encoding='utf-8'))
            result = index_corpus(args.root, args.db, paths)
        elif args.command == 'search' and args.mode == 'lexical':
            query = args.query if args.query is not None else sys.stdin.read(4097)
            result = search(args.db, query.strip(), args.top, args.base_url)
        elif args.command == 'evaluate':
            cases = json.loads(args.cases.read_text(encoding='utf-8'))
            result = evaluate(args.db, cases, args.top)
        else:
            if not args.embedding_revision:
                raise ValueError('--embedding-revision is required for M5 calls')
            identity = EmbeddingIdentity(args.embedding_revision, args.dimensions)
            embedder = M5Embedder.from_env(identity)
            if args.command == 'embed':
                result = build_vectors(args.db, embedder, identity)
            elif args.command == 'search':
                query = args.query if args.query is not None else sys.stdin.read(4097)
                result = semantic_search(args.db, query.strip(), embedder, identity, args.top, args.base_url, args.mode, args.min_score)
            else:
                from .evaluate import compare
                cases = json.loads(args.cases.read_text(encoding='utf-8'))
                result = compare(args.db, cases, embedder, identity, args.top, args.min_score)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, sqlite3.Error, EmbedderUnavailable) as error:
        # No source bytes, query, secret values, or extractor stderr in diagnostics.
        reason = str(error) if isinstance(error, (ValueError, EmbedderUnavailable)) else type(error).__name__
        diagnostic = {'error': reason}
        if isinstance(error, EmbedderUnavailable):
            diagnostic.update(http_status=error.status, retry_after=error.retry_after)
        print(json.dumps(diagnostic), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
