"""Opt-in semantic retrieval for the snapshot runtime; lexical stays the default.

Vectors are derived, disposable state.  They are cached by content under one
private SQLite file per embedding identity in the runtime state directory and
bound to a snapshot only through an explicit, complete coverage record.  This
module never decides source freshness: units come from the caller's complete
snapshot index and every returned original is still verified by the runtime.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import sqlite3
import stat
import threading
import time

from .core import PIPELINE_VERSION, has_secret
from .embedding_client import EmbedderUnavailable, payload_bytes
from .vectors import (DOCUMENT_PREFIX, NORMALIZATION_VERSION, QUERY_PREFIX, SEGMENTATION_VERSION,
                      EmbeddingIdentity, cache_key, cosine, create_schema, document_segments,
                      get_cached, normalize, put_cached, query_input, reciprocal_rank_fusion)

MODES = ('lexical', 'hybrid', 'vector')
RUNTIME_DIMENSIONS = (256, 768)
CACHE_SCHEMA = 'runtime-vectors-1'
MAX_SEMANTIC_UNITS = 5_000
MAX_CACHE_PAGES = 32_768  # 128 MiB: vectors can never exhaust the 512 MiB state budget.
MAX_STEP_BATCHES = 8
# The serve loop kills an index child after 90 s and that cancels the lexical job,
# so a vector step must finish well inside it.  Gateway calls share one absolute
# deadline (requests, retries and Retry-After sleeps): no new unit after 15 s and
# nothing at all after 60 s from the start of the step.
STEP_SECONDS = 15
HARD_STEP_SECONDS = 60
BUILD_TIMEOUT = 8
QUERY_TIMEOUT = 3
QUERY_SECONDS = 4
# All vector caches together: a quota separate from (and never counted in) the
# runtime's 512 MiB lexical state budget.
MAX_VECTOR_BYTES = MAX_CACHE_PAGES * 4096
MIN_VECTOR_GROWTH = 1024 * 1024
VECTOR_STATES = frozenset({'pending', 'complete', 'unavailable', 'failed'})
BREAKER_SECONDS = 30
MIN_ZSCORE_UNITS = 8
MIN_RETRY_SECONDS = 5
MAX_RETRY_SECONDS = 300
_REVISION = re.compile(r'[0-9a-f]{40}:[0-9a-f]{64}')
_CODE = re.compile(r'[a-z_0-9]{1,64}')
# Gateway outages and throttling recover by themselves; auth, revision drift and
# malformed responses need an operator and stay failed until an explicit retry.
_TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}


class VectorError(ValueError):
    """A bounded, non-sensitive semantic-state error code."""


def _flag(env, name):
    return env.get(name, '').strip()


def _threshold(value, name, low, high):
    if value == '':
        return None
    try:
        number = float(value)
    except ValueError:
        raise VectorError('invalid_' + name) from None
    if not math.isfinite(number) or not low <= number <= high:
        raise VectorError('invalid_' + name)
    return number


@dataclass(frozen=True)
class SemanticConfig:
    """Operator configuration. Any invalid value disables semantic retrieval."""

    enabled: bool = False
    default_mode: str = 'lexical'
    identity: EmbeddingIdentity | None = None
    min_cosine: float | None = None
    min_zscore: float | None = None
    error: str | None = None

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        try:
            flag = _flag(env, 'MIMIR_SEARCH_SEMANTIC') or 'off'
            if flag not in {'off', 'on'}:
                raise VectorError('invalid_semantic_flag')
            default_mode = _flag(env, 'MIMIR_SEARCH_DEFAULT_MODE') or 'lexical'
            if default_mode not in MODES:
                raise VectorError('invalid_default_mode')
            if flag == 'off':
                if default_mode != 'lexical':
                    raise VectorError('semantic_default_requires_enabled')
                return cls()
            revision = _flag(env, 'MIMIR_SEARCH_EMBEDDING_REVISION')
            if not _REVISION.fullmatch(revision):
                raise VectorError('invalid_embedding_revision')
            raw_dimensions = _flag(env, 'MIMIR_SEARCH_EMBEDDING_DIMENSIONS') or '256'
            if raw_dimensions not in {str(value) for value in RUNTIME_DIMENSIONS}:
                raise VectorError('invalid_embedding_dimensions')
            min_cosine = _threshold(_flag(env, 'MIMIR_SEARCH_ABSTAIN_MIN_COSINE'), 'abstain_min_cosine', -1.0, 1.0)
            min_zscore = _threshold(_flag(env, 'MIMIR_SEARCH_ABSTAIN_MIN_ZSCORE'), 'abstain_min_zscore', 0.0, 100.0)
            # A semantic default must never return the nearest neighbour of an
            # absent topic as if it were an answer: require an abstention policy.
            if default_mode != 'lexical' and min_cosine is None and min_zscore is None:
                raise VectorError('semantic_default_requires_abstention')
            return cls(True, default_mode, EmbeddingIdentity(revision, int(raw_dimensions)),
                       min_cosine, min_zscore)
        except VectorError as error:
            return cls(error=str(error))

    @property
    def tag(self):
        return identity_tag(self.identity) if self.identity is not None else None

    def public(self):
        """Bounded, credential-free health description."""
        output = {'enabled': self.enabled, 'default_mode': self.default_mode, 'error': self.error,
                  'abstention': {'min_cosine': self.min_cosine, 'min_zscore': self.min_zscore}}
        if self.identity is not None:
            output.update(model=self.identity.model, dimensions=self.identity.dimensions,
                          revision=self.identity.revision, cache=self.tag)
        return output


def identity_tag(identity):
    if not isinstance(identity, EmbeddingIdentity):
        raise TypeError('identity must be an EmbeddingIdentity')
    payload = json.dumps({'cache': CACHE_SCHEMA, 'model': identity.model, 'revision': identity.revision,
                          'dimensions': identity.dimensions, 'document_prefix': DOCUMENT_PREFIX,
                          'query_prefix': QUERY_PREFIX, 'normalization': NORMALIZATION_VERSION,
                          'segmentation': SEGMENTATION_VERSION, 'pipeline': PIPELINE_VERSION},
                         sort_keys=True, separators=(',', ':')).encode()
    return '%dd-%s' % (identity.dimensions, hashlib.sha256(payload).hexdigest()[:16])


def valid_state(value):
    """Total check of a persisted ``vectors`` job block over any JSON value."""
    try:
        if not isinstance(value, dict) or not isinstance(value.get('status'), str):
            return False
        if value['status'] not in VECTOR_STATES:
            return False
        failures = value.get('failures')
        if 'failures' in value and (type(failures) is not int or failures < 1):
            return False
        if value['status'] == 'unavailable':
            retry = value.get('retry_after')
            if type(retry) not in (int, float) or not math.isfinite(float(retry)) or failures is None:
                return False
        return True
    except (TypeError, ValueError, OverflowError):
        return False


@contextlib.contextmanager
def wall_clock_limit(deadline):
    """Absolute wall-clock bound for one blocking model call.

    Socket timeouts apply per read, so a gateway that trickles bytes can outlive
    any per-call timeout.  In the main thread (the step child and the serve
    loop) a real-time timer interrupts the call at ``deadline``.  Elsewhere the
    embedder's own deadline is the only bound.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise EmbedderUnavailable('embedding_deadline_exceeded')
    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, 'setitimer'):
        yield
        return

    def expire(_signum, _frame):
        raise EmbedderUnavailable('embedding_deadline_exceeded')

    previous = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, remaining)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class Watched:
    """Embedder wrapper that applies :func:`wall_clock_limit` to every call."""

    def __init__(self, embedder, deadline):
        self.embedder = embedder
        self.identity = embedder.identity
        self.deadline = deadline

    def embed(self, inputs):
        with wall_clock_limit(self.deadline):
            return self.embedder.embed(inputs)


def transient(error):
    """True when an embedding failure should back off rather than fail."""
    if not isinstance(error, EmbedderUnavailable):
        return False
    return (str(error) in {'embedding_transport_error', 'embedding_deadline_exceeded'} or
            error.status in _TRANSIENT_STATUS)


def error_code(error, default):
    code = error.args[0] if error.args else None
    return code if isinstance(code, str) and _CODE.fullmatch(code) else default


def retry_delay(failures):
    return min(MAX_RETRY_SECONDS, MIN_RETRY_SECONDS * 2 ** min(max(failures, 1) - 1, 16))


class VectorStore:
    """Private content-addressed vector cache plus per-snapshot coverage."""

    def __init__(self, state, identity, create=False):
        self.identity = identity
        self.tag = identity_tag(identity)
        self.path = Path(state) / ('vectors-%s.sqlite' % self.tag)
        new = False
        try:
            info = os.lstat(self.path)
        except FileNotFoundError:
            if not create:
                raise VectorError('vectors_missing') from None
            try:
                os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600))
            except OSError:
                raise VectorError('vector_cache_unavailable') from None
            new = True
        else:
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise VectorError('vector_cache_not_private')
        self.connection = None
        try:
            self.connection = sqlite3.connect(self.path, timeout=5.0)
            self.connection.execute('PRAGMA trusted_schema=OFF')
            self.connection.execute('PRAGMA busy_timeout=5000')
            self.connection.execute('PRAGMA max_page_count=%d' % MAX_CACHE_PAGES)
            if new:
                with self.connection:
                    self.connection.execute('CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)')
                    self.connection.execute('CREATE TABLE snapshot_coverage(snapshot_id TEXT PRIMARY KEY, '
                                            'unit_count INTEGER NOT NULL, completed_at INTEGER NOT NULL)')
                    create_schema(self.connection)
                    self.connection.executemany('INSERT INTO metadata(key,value) VALUES (?,?)',
                                                [('schema', CACHE_SCHEMA), ('identity', self.tag)])
            else:
                values = dict(self.connection.execute('SELECT key,value FROM metadata').fetchall())
                if values != {'schema': CACHE_SCHEMA, 'identity': self.tag}:
                    raise VectorError('vector_cache_identity_mismatch')
                check = self.connection.execute('PRAGMA quick_check(1)').fetchone()
                if check is None or check[0] != 'ok':
                    raise VectorError('vector_cache_corrupt')
        except BaseException as error:
            if self.connection is not None:
                self.connection.close()
            if isinstance(error, sqlite3.DatabaseError):
                raise VectorError('vector_cache_corrupt') from None
            if isinstance(error, OSError):
                raise VectorError('vector_cache_unavailable') from None
            raise

    def limit(self, max_bytes):
        """Cap this file below the shared budget; SQLite never shrinks below its size."""
        try:
            page = int(self.connection.execute('PRAGMA page_size').fetchone()[0])
            self.connection.execute('PRAGMA max_page_count=%d' % max(1, min(MAX_CACHE_PAGES, max_bytes // page)))
        except sqlite3.DatabaseError:
            raise VectorError('vector_cache_corrupt') from None

    def get(self, key, segments):
        try:
            vectors = get_cached(self.connection, key, self.identity.dimensions)
        except sqlite3.DatabaseError:
            raise VectorError('vector_cache_corrupt') from None
        return vectors if vectors is not None and len(vectors) == segments else None

    def put(self, key, vectors):
        try:
            with self.connection:
                put_cached(self.connection, key, vectors, self.identity.dimensions)
        except sqlite3.DatabaseError as error:
            raise VectorError('vector_cache_full' if 'full' in str(error).lower() else 'vector_cache_corrupt') from None

    def covered(self, snapshot_id):
        try:
            row = self.connection.execute('SELECT unit_count FROM snapshot_coverage WHERE snapshot_id=?',
                                          (snapshot_id,)).fetchone()
        except sqlite3.DatabaseError:
            raise VectorError('vector_cache_corrupt') from None
        return None if row is None else int(row[0])

    def mark_covered(self, snapshot_id, unit_count, now):
        try:
            with self.connection:
                self.connection.execute('INSERT OR REPLACE INTO snapshot_coverage(snapshot_id,unit_count,completed_at) '
                                        'VALUES (?,?,?)', (snapshot_id, unit_count, int(now)))
        except sqlite3.DatabaseError:
            raise VectorError('vector_cache_corrupt') from None

    def close(self):
        self.connection.close()


def eligible(units):
    """Units that may be sent to the model. Known secrets never leave the host."""
    return [unit for unit in units if not has_secret(unit['title'] + '\n' + unit['text'])]


def _key(identity, unit):
    return cache_key(identity, unit['title'], unit['text'], PIPELINE_VERSION)


def _embed(embedder, inputs, identity):
    if embedder.identity != identity:
        raise VectorError('embedding_identity_mismatch')
    try:
        values = embedder.embed(inputs)
        if not isinstance(values, list) or len(values) != len(inputs):
            raise ValueError('count')
        return [normalize(vector, identity.dimensions) for vector in values]
    except EmbedderUnavailable:
        raise
    except (OSError, RuntimeError, ValueError, TypeError, ArithmeticError):
        raise EmbedderUnavailable('embedding_invalid_response') from None


def build_step(units, store, embedder, snapshot_id, *, deadline, hard_deadline=None,
               max_batches=MAX_STEP_BATCHES, clock=time.monotonic, now=time.time):
    """Embed a bounded slice of missing units; record coverage once all are cached.

    Only complete per-unit vectors are committed.  After ``deadline`` no new unit
    is started, but a unit already in flight is finished so large units still
    make progress.  ``hard_deadline`` stops even that, discarding the partial
    unit.  Returns counts only; never text.
    """
    if hard_deadline is None:
        hard_deadline = deadline + (HARD_STEP_SECONDS - STEP_SECONDS)
    rows = eligible(units)
    if len(rows) > MAX_SEMANTIC_UNITS:
        raise VectorError('semantic_corpus_too_large')
    identity = store.identity
    pending = {}
    for row in rows:
        key = _key(identity, row)
        if key not in pending:
            segments = document_segments(row['title'], row['text'])
            if store.get(key, len(segments)) is None:
                pending[key] = segments
    tasks = [(key, len(segments), segment) for key, segments in pending.items() for segment in segments]
    received = {}
    batches = 0
    while tasks:
        current = clock()
        if current >= hard_deadline or (batches >= max_batches or current >= deadline) and not received:
            break
        batch, inputs = [], []
        while tasks and len(inputs) < 16:
            if len(payload_bytes(inputs + [tasks[0][2]], identity)) > 32768:
                if not inputs:
                    raise VectorError('segment_exceeds_api_limit')
                break
            batch.append(tasks.pop(0))
            inputs.append(batch[-1][2])
        try:
            values = _embed(embedder, inputs, identity)
        except EmbedderUnavailable as error:
            if str(error) != 'embedding_deadline_exceeded':
                raise
            break  # Out of step time: keep committed units, resume next step.
        batches += 1
        for (key, count, _), vector in zip(batch, values):
            received.setdefault(key, []).append(vector)
            if len(received[key]) == count:
                store.put(key, received.pop(key))
                pending.pop(key)
    covered = sum(1 for row in rows if _key(identity, row) not in pending)
    status = 'pending' if pending else 'complete'
    if not pending:
        store.mark_covered(snapshot_id, len(rows), now())
    return {'status': status, 'units': len(rows), 'covered': covered, 'batches': batches}


def load_vectors(units, store, snapshot_id):
    """Return ``(rows, vectors_by_id)`` for a covered snapshot, else ``None``."""
    rows = eligible(units)
    if store.covered(snapshot_id) != len(rows) or len(rows) > MAX_SEMANTIC_UNITS:
        return None
    vectors = {}
    for row in rows:
        found = store.get(_key(store.identity, row), len(document_segments(row['title'], row['text'])))
        if found is None:
            return None
        vectors[row['id']] = found
    return rows, vectors


def embed_query(embedder, query, identity):
    return _embed(embedder, [query_input(query)], identity)[0]


def abstention(scores, min_cosine=None, min_zscore=None):
    """Decide whether a semantic ranking supports any answer at all.

    Raw cosine is not a probability: the nearest neighbour of an absent topic
    still scores.  The optional z-score asks whether the best unit stands out
    from this query's whole-corpus distribution.  Both thresholds must come from
    a separate calibration split; with neither set, nothing is withheld and the
    response says so.
    """
    policy = 'none' if min_cosine is None and min_zscore is None else 'score'
    signal = {'units': len(scores), 'top_cosine': None, 'zscore': None}
    if not scores:
        return {'policy': policy, 'abstained': True, 'reason': 'no_vectors', 'signal': signal}
    top = max(scores)
    signal['top_cosine'] = round(top, 6)
    zscore = None
    if len(scores) >= MIN_ZSCORE_UNITS:
        # fsum keeps identical scores exactly flat; a rounding residue must not
        # look like an outlier, so a negligible spread counts as no spread.
        mean = math.fsum(scores) / len(scores)
        spread = math.sqrt(math.fsum((value - mean) ** 2 for value in scores) / len(scores))
        zscore = (top - mean) / spread if spread > 1e-9 * max(1.0, abs(mean)) else 0.0
        signal['zscore'] = round(zscore, 6)
    reason = None
    if min_cosine is not None and top < min_cosine:
        reason = 'below_cosine_floor'
    elif min_zscore is not None and zscore is None:
        reason = 'too_few_units_for_margin'
    elif min_zscore is not None and zscore < min_zscore:
        reason = 'below_score_margin'
    return {'policy': policy, 'abstained': reason is not None, 'reason': reason, 'signal': signal}


def rank(rows, vectors, query_vector, *, mode, top, lexical_ids, min_cosine=None, min_zscore=None):
    """Return ranked ``(unit_id, score)`` pairs plus the abstention decision."""
    if mode not in {'vector', 'hybrid'}:
        raise VectorError('invalid_mode')
    if isinstance(top, bool) or not isinstance(top, int) or top < 1:
        raise VectorError('invalid_top')
    by_id = {row['id']: row for row in rows}
    similarity = {row['id']: max(cosine(query_vector, vector) for vector in vectors[row['id']]) for row in rows}
    decision = abstention(list(similarity.values()), min_cosine, min_zscore)
    if decision['abstained']:
        return [], similarity, decision
    stable = lambda identifier: (by_id[identifier]['path'], by_id[identifier]['number'], identifier)
    vector_order = sorted(similarity, key=lambda identifier: (-similarity[identifier], stable(identifier)))
    if mode == 'vector':
        return [(identifier, similarity[identifier]) for identifier in vector_order[:top]], similarity, decision
    window = max(top, 5)
    lexical = [identifier for identifier in lexical_ids if identifier in by_id][:window]
    fused = reciprocal_rank_fusion(lexical, vector_order[:window])
    order = sorted(fused, key=lambda identifier: (-fused[identifier], stable(identifier)))
    return [(identifier, fused[identifier]) for identifier in order[:top]], similarity, decision


__all__ = ['MODES', 'SemanticConfig', 'VectorError', 'VectorStore', 'abstention', 'build_step',
           'embed_query', 'identity_tag', 'load_vectors', 'rank', 'transient']
