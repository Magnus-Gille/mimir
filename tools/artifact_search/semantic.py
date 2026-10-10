"""Opt-in, private semantic retrieval over the hash-verified lexical corpus."""
from __future__ import annotations

import hashlib
import math
from pathlib import Path
import sqlite3

from . import core
from .embedding_client import EmbedderUnavailable, payload_bytes
from .vectors import (cache_key, cosine, create_schema, document_segments, get_cached,
                      normalize, put_cached, query_input, reciprocal_rank_fusion)


def _open(db, writable=False):
    db = Path(db).absolute()
    if db.is_symlink() or db.stat().st_mode & 0o077 or db.parent.is_symlink() or db.parent.stat().st_mode & 0o077:
        raise ValueError('Index and parent must be private and not symlinks')
    conn = sqlite3.connect(db.as_uri() + ('?mode=rw' if writable else '?mode=ro'), uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        if core._metadata(conn, 'schema_version') != '1':
            raise ValueError('Index version mismatch')
        root = Path(core._metadata(conn, 'root'))
        core._outside(root, db)
        return conn, root
    except BaseException:
        conn.close()
        raise


def _current_rows(conn, root):
    counters = dict(stale_candidates=0, unavailable_candidates=0)
    checked = {}
    current = []
    for row in conn.execute('SELECT c.*,f.sha256,f.indexed_at,f.extractor_version FROM chunks c JOIN files f ON f.path=c.path ORDER BY c.path,c.number,c.id'):
        path = row['path']
        if path not in checked:
            try:
                data, _ = core.read_source(root, path)
                checked[path] = 'current' if (hashlib.sha256(data).hexdigest() == row['sha256'] and row['extractor_version'] == core.PIPELINE_VERSION) else 'stale'
            except (OSError, ValueError):
                checked[path] = 'unavailable'
        if checked[path] != 'current':
            counters['stale_candidates' if checked[path] == 'stale' else 'unavailable_candidates'] += 1
        elif not core.has_secret(row['title'] + '\n' + row['text']):
            current.append(row)
    return current, counters


def _embed(embedder, inputs, identity):
    if embedder.identity != identity:
        raise ValueError('Embedding identity mismatch')
    try:
        values = embedder.embed(inputs)
        if not isinstance(values, list) or len(values) != len(inputs):
            raise ValueError('Embedding count mismatch')
        return [normalize(vector, identity.dimensions) for vector in values]
    except EmbedderUnavailable:
        raise
    except (OSError, RuntimeError, ValueError, TypeError) as error:
        raise EmbedderUnavailable('embedding_invalid_or_unavailable') from error


def build_vectors(db: Path, embedder, identity):
    if embedder.identity != identity:
        raise ValueError('Embedding identity mismatch')
    conn, root = _open(db, writable=True)
    summary = dict(embedded_units=0, cached_units=0, skipped_units=0)
    try:
        create_schema(conn)
        conn.commit()
        rows, counters = _current_rows(conn, root)
        summary['skipped_units'] = sum(counters.values())
        tasks = []
        active_keys = set()
        # Coalesce identical content without discarding its distinct original references.
        counts = {}
        for row in rows:
            key = cache_key(identity, row['title'], row['text'], core.PIPELINE_VERSION)
            active_keys.add(key)
            segments = document_segments(row['title'], row['text'])
            cached = get_cached(conn, key, identity.dimensions)
            if cached is not None and len(cached) == len(segments):
                summary['cached_units'] += 1
                continue
            counts[key] = counts.get(key, 0) + 1
            if counts[key] == 1:
                for segment in segments:
                    tasks.append((key, len(segments), segment))
        received = {}
        while tasks:
            batch = []
            inputs = []
            while tasks and len(inputs) < 16:
                candidate = tasks[0]
                if len(payload_bytes(inputs + [candidate[2]], identity)) > 32768:
                    if not inputs:
                        raise ValueError('Document segment exceeds API body limit')
                    break
                batch.append(tasks.pop(0))
                inputs.append(candidate[2])
            values = _embed(embedder, inputs, identity)
            with conn:
                for (key, count, _), vector in zip(batch, values):
                    received.setdefault(key, []).append(vector)
                    if len(received[key]) == count:
                        put_cached(conn, key, received.pop(key), identity.dimensions)
                        summary['embedded_units'] += counts[key]
        # Retention follows all indexed rows, even temporarily unavailable sources.
        # Old identities/obsolete source text are removed only after a completed run.
        retained = {cache_key(identity, row['title'], row['text'], core.PIPELINE_VERSION)
                    for row in conn.execute('SELECT title,text FROM chunks')}
        with conn:
            for entry in conn.execute('SELECT key FROM artifact_vectors').fetchall():
                if entry['key'] not in retained:
                    conn.execute('DELETE FROM artifact_vectors WHERE key=?', (entry['key'],))
        return summary
    finally:
        conn.close()


def semantic_search(db, query, embedder, identity, top=5, base_url=None, mode='vector', min_score=0.0):
    if (isinstance(top, bool) or not isinstance(top, int) or not 1 <= top <= 100 or
            not isinstance(query, str) or not query.strip() or len(query) > 4096):
        raise ValueError('Query must be nonempty and top must be 1..100')
    if mode not in {'vector', 'hybrid'} or not isinstance(min_score, (int, float)) or isinstance(min_score, bool) or not math.isfinite(min_score) or not -1 <= min_score <= 1:
        raise ValueError('Invalid search mode or cosine floor')
    core._reference(base_url, 'probe.txt', 'section', 1, None)
    conn, root = _open(db)
    try:
        rows, counters = _current_rows(conn, root)
        output = dict(results=[], **counters, mode=mode)
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='artifact_vectors'").fetchone()
        candidates = []
        if exists:
            for row in rows:
                key = cache_key(identity, row['title'], row['text'], core.PIPELINE_VERSION)
                vectors = get_cached(conn, key, identity.dimensions)
                if vectors and len(vectors) == len(document_segments(row['title'], row['text'])):
                    candidates.append((row, vectors))
        if mode == 'hybrid' and not candidates:
            return dict(core.search(db, query, top, base_url), mode='lexical_fallback', warning='vectors_missing')
        try:
            query_vector = _embed(embedder, [query_input(query)], identity)[0] if candidates else None
        except EmbedderUnavailable:
            if mode == 'vector':
                raise
            return dict(core.search(db, query, top, base_url), mode='lexical_fallback', warning='embedding_unavailable')
        scores = {}
        for row, vectors in candidates:
            score = max(cosine(query_vector, vector) for vector in vectors)
            if score > min_score:
                scores[row['id']] = score
        by_id = {row['id']: row for row in rows}
        stable = lambda identifier: (by_id[identifier]['path'], by_id[identifier]['number'], identifier)
        vector_order = sorted(scores, key=lambda identifier: (-scores[identifier], stable(identifier)))
        order = vector_order
        if mode == 'hybrid':
            match = core._match_query(query)
            lexical = []
            if match:
                lexical = [row[0] for row in conn.execute('SELECT rowid FROM text_index WHERE text_index MATCH ? ORDER BY bm25(text_index,3.0,1.0),rowid', (match,)) if row[0] in by_id]
            fused = reciprocal_rank_fusion(lexical, vector_order)
            order = sorted(fused, key=lambda identifier: (-fused[identifier], stable(identifier)))
        # Validate bytes again after the network call; a source may change while embedding.
        fresh, _ = _current_rows(conn, root)
        fresh_rows = {row['id']: row for row in fresh}
        for identifier in order:
            old = by_id[identifier]
            latest = fresh_rows.get(identifier)
            # SQLite can reuse an ID during reindex; ID equality is not provenance.
            fields = ('path', 'sha256', 'extractor_version', 'kind', 'number', 'anchor', 'title', 'text')
            if latest is None or any(old[field] != latest[field] for field in fields):
                output['stale_candidates'] += 1
                continue
            row = by_id[identifier]
            output['results'].append(dict(path=row['path'], sha256=row['sha256'],
                locator=dict(kind=row['kind'], number=row['number'], anchor=row['anchor']),
                title=row['title'], snippet=row['text'][:500],
                mimir_url=core._reference(base_url,row['path'],row['kind'],row['number'],row['anchor']),
                indexed_at=row['indexed_at']))
            if len(output['results']) == top:
                break
        return output
    finally:
        conn.close()
