"""Tests for opt-in semantic retrieval state (synthetic fixtures, no network)."""
from __future__ import annotations

import json
import math
import os
import shutil
import stat
import tempfile
import unittest
import zlib
from unittest import mock

from tools.artifact_search import runtime_vectors as rv
from tools.artifact_search.embedding_client import EmbedderUnavailable
from tools.artifact_search.runtime_vectors import (SemanticConfig, VectorError, VectorStore, abstention,
                                                   build_step, identity_tag, load_vectors, rank, transient)
from tools.artifact_search.vectors import EmbeddingIdentity, document_segments

REVISION = 'a' * 40 + ':' + 'b' * 64
OTHER_REVISION = 'c' * 40 + ':' + 'd' * 64
IDENTITY = EmbeddingIdentity(REVISION, 256)
IDENTITY_768 = EmbeddingIdentity(REVISION, 768)
FAKE_AWS_KEY = 'AKIA' + 'A1B2C3D4E5F6G7H8'


class FakeEmbedder:
    """Deterministic keyword-hashed embedder with scripted failures."""

    def __init__(self, identity=IDENTITY, fail_on_call=None, error=None, wrong_count=False, wrong_dimension=False):
        self.identity = identity
        self.calls = []
        self.fail_on_call = fail_on_call
        self.error = error or EmbedderUnavailable('embedding_http_error', status=503)
        self.wrong_count = wrong_count
        self.wrong_dimension = wrong_dimension

    def embed(self, inputs):
        self.calls.append(list(inputs))
        if self.fail_on_call == len(self.calls):
            raise self.error
        dimensions = self.identity.dimensions + (1 if self.wrong_dimension else 0)
        vectors = []
        for text in inputs:
            vector = [0.0] * dimensions
            for word in text.lower().split():
                vector[zlib.crc32(word.encode()) % dimensions] += 1.0
            if not any(vector):
                vector[0] = 1.0
            vectors.append(vector)
        return vectors[:-1] if self.wrong_count else vectors

    @property
    def inputs(self):
        return [item for call in self.calls for item in call]


def unit(number, text=None, title=None, path=None, identifier=None):
    return {'id': identifier if identifier is not None else number, 'artifact_id': 'artifact-%d' % number,
            'path': path or 'docs/file-%d.md' % number, 'sha256': '%064x' % number, 'kind': 'section',
            'number': number, 'anchor': 'a%d' % number, 'title': title or 'Title %d' % number,
            'text': text if text is not None else 'distinct body text number %d' % number}


class StateDirTestCase(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix='mimir-rv-test-')
        os.chmod(self.state, 0o700)
        self.addCleanup(shutil.rmtree, self.state, ignore_errors=True)
        self.stores = []

    def open_store(self, identity=IDENTITY, create=True):
        store = VectorStore(self.state, identity, create=create)
        self.stores.append(store)
        self.addCleanup(store.close)
        return store


class SemanticConfigTests(unittest.TestCase):
    def test_default_is_off_and_lexical(self):
        config = SemanticConfig.from_env({})
        self.assertFalse(config.enabled)
        self.assertEqual(config.default_mode, 'lexical')
        self.assertIsNone(config.error)
        self.assertIsNone(config.identity)
        self.assertIsNone(config.tag)

    def test_enabled_with_valid_revision_and_dimensions(self):
        for dimensions in (256, 768):
            with self.subTest(dimensions=dimensions):
                config = SemanticConfig.from_env({'MIMIR_SEARCH_SEMANTIC': 'on',
                                                  'MIMIR_SEARCH_EMBEDDING_REVISION': REVISION,
                                                  'MIMIR_SEARCH_EMBEDDING_DIMENSIONS': str(dimensions)})
                self.assertTrue(config.enabled)
                self.assertIsNone(config.error)
                self.assertEqual(config.identity, EmbeddingIdentity(REVISION, dimensions))
                self.assertEqual(config.default_mode, 'lexical')

    def test_dimensions_default_to_256(self):
        config = SemanticConfig.from_env({'MIMIR_SEARCH_SEMANTIC': 'on', 'MIMIR_SEARCH_EMBEDDING_REVISION': REVISION})
        self.assertEqual(config.identity.dimensions, 256)

    def assert_disabled(self, env, error):
        config = SemanticConfig.from_env(env)
        self.assertEqual(config.error, error)
        self.assertFalse(config.enabled)
        self.assertEqual(config.default_mode, 'lexical')
        self.assertIsNone(config.identity)

    def test_invalid_values_disable_semantic_retrieval(self):
        on = {'MIMIR_SEARCH_SEMANTIC': 'on', 'MIMIR_SEARCH_EMBEDDING_REVISION': REVISION}
        self.assert_disabled({'MIMIR_SEARCH_SEMANTIC': 'yes'}, 'invalid_semantic_flag')
        self.assert_disabled({'MIMIR_SEARCH_SEMANTIC': 'off', 'MIMIR_SEARCH_DEFAULT_MODE': 'fuzzy'}, 'invalid_default_mode')
        self.assert_disabled({'MIMIR_SEARCH_SEMANTIC': 'on'}, 'invalid_embedding_revision')
        for revision in ('a' * 40, 'A' * 40 + ':' + 'b' * 64, 'a' * 40 + ':' + 'b' * 63, 'a' * 39 + ':' + 'b' * 64):
            self.assert_disabled({'MIMIR_SEARCH_SEMANTIC': 'on', 'MIMIR_SEARCH_EMBEDDING_REVISION': revision},
                                 'invalid_embedding_revision')
        for dimensions in ('512', '128', 'abc'):
            self.assert_disabled({**on, 'MIMIR_SEARCH_EMBEDDING_DIMENSIONS': dimensions}, 'invalid_embedding_dimensions')

    def test_non_lexical_default_requires_semantic_on(self):
        for mode in ('hybrid', 'vector'):
            self.assert_disabled({'MIMIR_SEARCH_DEFAULT_MODE': mode}, 'semantic_default_requires_enabled')

    def test_non_lexical_default_requires_abstention_threshold(self):
        base = {'MIMIR_SEARCH_SEMANTIC': 'on', 'MIMIR_SEARCH_EMBEDDING_REVISION': REVISION,
                'MIMIR_SEARCH_DEFAULT_MODE': 'hybrid'}
        self.assert_disabled(base, 'semantic_default_requires_abstention')
        for key, value in (('MIMIR_SEARCH_ABSTAIN_MIN_COSINE', '0.3'), ('MIMIR_SEARCH_ABSTAIN_MIN_ZSCORE', '2')):
            config = SemanticConfig.from_env({**base, key: value})
            self.assertIsNone(config.error)
            self.assertTrue(config.enabled)
            self.assertEqual(config.default_mode, 'hybrid')

    def test_thresholds_are_range_and_finiteness_checked(self):
        on = {'MIMIR_SEARCH_SEMANTIC': 'on', 'MIMIR_SEARCH_EMBEDDING_REVISION': REVISION}
        for name, values in (('MIMIR_SEARCH_ABSTAIN_MIN_COSINE', ('1.01', '-1.01', 'nan', 'inf', '-inf', 'x')),
                             ('MIMIR_SEARCH_ABSTAIN_MIN_ZSCORE', ('-0.1', '100.5', 'nan', 'inf', 'x'))):
            for value in values:
                with self.subTest(name=name, value=value):
                    self.assert_disabled({**on, name: value}, 'invalid_' + name.replace('MIMIR_SEARCH_', '').lower())
        config = SemanticConfig.from_env({**on, 'MIMIR_SEARCH_ABSTAIN_MIN_COSINE': '-1',
                                          'MIMIR_SEARCH_ABSTAIN_MIN_ZSCORE': '100'})
        self.assertEqual((config.min_cosine, config.min_zscore), (-1.0, 100.0))

    def test_public_description_has_no_credential_and_includes_cache_tag(self):
        env = {'MIMIR_SEARCH_SEMANTIC': 'on', 'MIMIR_SEARCH_EMBEDDING_REVISION': REVISION,
               'M5_API_KEY': 'super-secret-key-value', 'M5_OPENAI_BASE_URL': 'http://10.0.0.1/v1'}
        config = SemanticConfig.from_env(env)
        public = config.public()
        self.assertEqual(public['cache'], identity_tag(IDENTITY))
        self.assertEqual(public['dimensions'], 256)
        encoded = json.dumps(public)
        self.assertNotIn('super-secret-key-value', encoded)
        self.assertNotIn('10.0.0.1', encoded)
        self.assertNotIn('cache', SemanticConfig.from_env({}).public())


class IdentityTagTests(unittest.TestCase):
    def test_tag_is_stable_and_distinguishes_dimensions_and_revisions(self):
        self.assertEqual(identity_tag(IDENTITY), identity_tag(EmbeddingIdentity(REVISION, 256)))
        self.assertNotEqual(identity_tag(IDENTITY), identity_tag(IDENTITY_768))
        self.assertNotEqual(identity_tag(IDENTITY), identity_tag(EmbeddingIdentity(OTHER_REVISION, 256)))
        self.assertTrue(identity_tag(IDENTITY).startswith('256d-'))
        self.assertTrue(identity_tag(IDENTITY_768).startswith('768d-'))

    def test_tag_requires_an_identity(self):
        with self.assertRaises(TypeError):
            identity_tag('256')


class VectorStoreTests(StateDirTestCase):
    def test_missing_store_without_create_is_refused_and_not_created(self):
        with self.assertRaises(VectorError) as caught:
            VectorStore(self.state, IDENTITY, create=False)
        self.assertEqual(str(caught.exception), 'vectors_missing')
        self.assertEqual(os.listdir(self.state), [])

    def test_created_file_is_private(self):
        store = self.open_store()
        self.assertEqual(stat.S_IMODE(os.stat(store.path).st_mode), 0o600)
        self.assertIn(identity_tag(IDENTITY), store.path.name)

    def test_reopen_existing_store_without_create(self):
        self.open_store().close()
        reopened = VectorStore(self.state, IDENTITY, create=False)
        self.addCleanup(reopened.close)
        self.assertIsNone(reopened.covered('snap'))

    def test_group_readable_existing_file_is_refused(self):
        path = self.open_store().path
        self.stores[0].close()
        os.chmod(path, 0o640)
        for create in (False, True):
            with self.assertRaises(VectorError) as caught:
                VectorStore(self.state, IDENTITY, create=create)
            self.assertEqual(str(caught.exception), 'vector_cache_not_private')

    def test_symlink_at_path_is_refused_and_not_followed(self):
        target = os.path.join(self.state, 'target.sqlite')
        link = os.path.join(self.state, 'vectors-%s.sqlite' % identity_tag(IDENTITY))
        os.symlink(target, link)  # dangling link: following it would create the target
        for create in (False, True):
            with self.assertRaises(VectorError) as caught:
                VectorStore(self.state, IDENTITY, create=create)
            self.assertEqual(str(caught.exception), 'vector_cache_not_private')
        self.assertFalse(os.path.exists(target))

    def test_symlink_to_private_regular_file_is_refused(self):
        real = self.open_store()
        real.close()
        os.rename(real.path, os.path.join(self.state, 'real.sqlite'))
        os.symlink(os.path.join(self.state, 'real.sqlite'), real.path)
        with self.assertRaises(VectorError) as caught:
            VectorStore(self.state, IDENTITY, create=False)
        self.assertEqual(str(caught.exception), 'vector_cache_not_private')

    def test_store_contents_for_another_identity_are_refused(self):
        small = self.open_store(IDENTITY)
        small.close()
        wrong = os.path.join(self.state, 'vectors-%s.sqlite' % identity_tag(IDENTITY_768))
        shutil.copyfile(small.path, wrong)
        os.chmod(wrong, 0o600)
        with self.assertRaises(VectorError) as caught:
            VectorStore(self.state, IDENTITY_768, create=False)
        self.assertEqual(str(caught.exception), 'vector_cache_identity_mismatch')

    def test_garbage_bytes_are_reported_corrupt(self):
        path = os.path.join(self.state, 'vectors-%s.sqlite' % identity_tag(IDENTITY))
        fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(bytes(range(256)) * 8)
        with self.assertRaises(VectorError) as caught:
            VectorStore(self.state, IDENTITY, create=False)
        self.assertEqual(str(caught.exception), 'vector_cache_corrupt')

    def test_coverage_is_none_until_marked(self):
        store = self.open_store()
        self.assertIsNone(store.covered('snap'))
        store.mark_covered('snap', 7, 1000.9)
        self.assertEqual(store.covered('snap'), 7)
        self.assertIsNone(store.covered('other'))
        store.mark_covered('snap', 0, 1001)
        self.assertEqual(store.covered('snap'), 0)

    def test_coverage_and_vectors_persist_across_reopen(self):
        store = self.open_store()
        build_step([unit(1)], store, FakeEmbedder(), 'snap', deadline=math.inf)
        store.close()
        reopened = VectorStore(self.state, IDENTITY, create=False)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.covered('snap'), 1)
        self.assertIsNotNone(load_vectors([unit(1)], reopened, 'snap'))


class BuildStepTests(StateDirTestCase):
    def build(self, units, embedder=None, store=None, snapshot='snap', **kwargs):
        kwargs.setdefault('deadline', math.inf)
        return build_step(units, store or self.open_store(), embedder or FakeEmbedder(), snapshot, **kwargs)

    def test_small_corpus_completes_and_records_coverage(self):
        store = self.open_store()
        embedder = FakeEmbedder()
        result = self.build([unit(n) for n in range(1, 6)], embedder, store, now=lambda: 42)
        self.assertEqual(result, {'status': 'complete', 'units': 5, 'covered': 5, 'batches': 1})
        self.assertEqual(store.covered('snap'), 5)
        self.assertEqual(len(embedder.inputs), 5)

    def test_empty_corpus_is_complete_with_zero_coverage(self):
        store = self.open_store()
        embedder = FakeEmbedder()
        result = self.build([], embedder, store)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(store.covered('snap'), 0)
        self.assertEqual(embedder.calls, [])

    def test_second_call_embeds_nothing_new(self):
        store = self.open_store()
        units = [unit(n) for n in range(1, 4)]
        self.build(units, FakeEmbedder(), store)
        again = FakeEmbedder()
        result = self.build(units, again, store, snapshot='next')
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['batches'], 0)
        self.assertEqual(again.calls, [])
        self.assertEqual(store.covered('next'), 3)

    def test_identical_title_and_text_are_embedded_once(self):
        embedder = FakeEmbedder()
        store = self.open_store()
        units = [unit(1, text='same', title='Same'), unit(2, text='same', title='Same', path='docs/other.md'),
                 unit(3, text='different', title='Same')]
        result = self.build(units, embedder, store)
        self.assertEqual(len(embedder.inputs), 2)
        self.assertEqual(result['units'], 3)
        self.assertEqual(result['covered'], 3)
        self.assertEqual(store.covered('snap'), 3)

    def test_max_batches_leaves_pending_and_next_call_completes(self):
        store = self.open_store()
        units = [unit(n) for n in range(1, 21)]  # 20 single-segment units, 16 per batch
        first = FakeEmbedder()
        result = self.build(units, first, store, max_batches=1)
        self.assertEqual(result, {'status': 'pending', 'units': 20, 'covered': 16, 'batches': 1})
        self.assertIsNone(store.covered('snap'))
        self.assertEqual(len(first.calls[0]), 16)
        second = FakeEmbedder()
        result = self.build(units, second, store, max_batches=1)
        self.assertEqual(result, {'status': 'complete', 'units': 20, 'covered': 20, 'batches': 1})
        self.assertEqual(len(second.inputs), 4)
        self.assertEqual(store.covered('snap'), 20)

    def test_expired_deadline_does_nothing_when_nothing_is_partial(self):
        store = self.open_store()
        embedder = FakeEmbedder()
        result = self.build([unit(1), unit(2)], embedder, store, deadline=10, clock=lambda: 100)
        self.assertEqual(result, {'status': 'pending', 'units': 2, 'covered': 0, 'batches': 0})
        self.assertEqual(embedder.calls, [])
        self.assertIsNone(store.covered('snap'))

    def multi_segment_corpus(self):
        long_text = ''.join(chr(ord('a') + i) * 4000 for i in range(2)) + 'tail words'
        self.assertEqual(len(document_segments('Long', long_text)), 3)
        return [unit(n) for n in range(1, 15)] + [unit(99, text=long_text, title='Long')]

    def test_multi_segment_unit_is_never_committed_partially(self):
        store = self.open_store()
        units = self.multi_segment_corpus()
        failing = FakeEmbedder(fail_on_call=2)
        with self.assertRaises(EmbedderUnavailable):
            self.build(units, failing, store)
        self.assertEqual([len(call) for call in failing.calls], [16, 1])
        long_unit = units[-1]
        key = rv._key(IDENTITY, long_unit)
        self.assertIsNone(store.get(key, 3))
        self.assertEqual(store.connection.execute('SELECT COUNT(*) FROM artifact_vectors WHERE key=?',
                                                  (key,)).fetchone()[0], 0)
        self.assertEqual(store.connection.execute('SELECT COUNT(*) FROM artifact_vectors').fetchone()[0], 14)
        self.assertIsNone(store.covered('snap'))
        retry = FakeEmbedder()
        result = self.build(units, retry, store)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(len(retry.inputs), 3)  # only the long unit's segments
        self.assertEqual(len(store.get(key, 3)), 3)
        self.assertEqual(store.covered('snap'), 15)

    def test_partial_unit_finishes_past_batch_limit(self):
        store = self.open_store()
        units = self.multi_segment_corpus()
        embedder = FakeEmbedder()
        result = self.build(units, embedder, store, max_batches=1)
        # The batch limit applies only between units: the split unit is completed first.
        self.assertEqual(result['batches'], 2)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual([len(call) for call in embedder.calls], [16, 1])

    def test_hard_deadline_discards_a_partial_unit_instead_of_overrunning(self):
        store = self.open_store()
        units = self.multi_segment_corpus()
        ticks = iter([0.0, 100.0, 100.0, 100.0])
        embedder = FakeEmbedder()
        result = self.build(units, embedder, store, deadline=50.0, hard_deadline=60.0,
                            clock=lambda: next(ticks))
        # One batch ran, then the hard stop fired with the split unit half received.
        self.assertEqual((result['status'], result['batches']), ('pending', 1))
        self.assertIsNone(store.covered('snap'))
        long_unit = next(item for item in units if len(item['text']) > 4000)
        from tools.artifact_search.runtime_vectors import _key
        self.assertIsNone(store.get(_key(IDENTITY, long_unit), 2))
        self.assertIsNone(store.get(_key(IDENTITY, long_unit), 3))
        completed = self.build(units, FakeEmbedder(), store)
        self.assertEqual(completed['status'], 'complete')

    def test_secret_bearing_units_never_reach_the_embedder(self):
        store = self.open_store()
        embedder = FakeEmbedder()
        units = [unit(1), unit(2, text='config ' + FAKE_AWS_KEY + ' end'),
                 unit(3, title='token ' + FAKE_AWS_KEY), unit(4)]
        result = self.build(units, embedder, store)
        self.assertNotIn('AKIA', ''.join(embedder.inputs))
        self.assertEqual(len(embedder.inputs), 2)
        self.assertEqual(result, {'status': 'complete', 'units': 2, 'covered': 2, 'batches': 1})
        self.assertEqual(store.covered('snap'), 2)
        loaded = load_vectors(units, store, 'snap')
        self.assertEqual(sorted(loaded[1]), [1, 4])

    def test_oversized_corpus_is_refused_before_embedding(self):
        embedder = FakeEmbedder()
        store = self.open_store()
        with mock.patch.object(rv, 'MAX_SEMANTIC_UNITS', 3):
            with self.assertRaises(VectorError) as caught:
                self.build([unit(n) for n in range(1, 5)], embedder, store)
            self.assertEqual(str(caught.exception), 'semantic_corpus_too_large')
            self.assertEqual(embedder.calls, [])
            self.assertEqual(self.build([unit(n) for n in range(1, 4)], embedder, store)['status'], 'complete')

    def test_secret_units_do_not_count_toward_the_corpus_limit(self):
        units = [unit(1), unit(2), unit(3, text=FAKE_AWS_KEY), unit(4, text=FAKE_AWS_KEY)]
        with mock.patch.object(rv, 'MAX_SEMANTIC_UNITS', 2):
            self.assertEqual(self.build(units)['status'], 'complete')

    def test_wrong_count_or_dimension_is_an_invalid_response(self):
        for options in ({'wrong_count': True}, {'wrong_dimension': True}):
            with self.subTest(options=options):
                store = self.open_store(create=not os.listdir(self.state))
                with self.assertRaises(EmbedderUnavailable) as caught:
                    self.build([unit(1), unit(2)], FakeEmbedder(**options), store)
                self.assertEqual(str(caught.exception), 'embedding_invalid_response')
                self.assertFalse(transient(caught.exception))
                self.assertIsNone(store.covered('snap'))

    def test_mismatched_embedder_identity_is_refused(self):
        embedder = FakeEmbedder(IDENTITY_768)
        with self.assertRaises(VectorError) as caught:
            self.build([unit(1)], embedder)
        self.assertEqual(str(caught.exception), 'embedding_identity_mismatch')
        self.assertEqual(embedder.calls, [])
        other_revision = FakeEmbedder(EmbeddingIdentity(OTHER_REVISION, 256))
        with self.assertRaises(VectorError):
            self.build([unit(1)], other_revision)

    def test_transport_failure_propagates_with_status(self):
        embedder = FakeEmbedder(fail_on_call=1, error=EmbedderUnavailable('embedding_http_error', status=429, retry_after=7))
        with self.assertRaises(EmbedderUnavailable) as caught:
            self.build([unit(1)], embedder)
        self.assertEqual(caught.exception.status, 429)
        self.assertTrue(transient(caught.exception))


class LoadVectorsTests(StateDirTestCase):
    def setUp(self):
        super().setUp()
        self.store = self.open_store()
        self.units = [unit(1), unit(2, text='multi ' + 'x' * 4100), unit(3)]

    def test_none_until_covered(self):
        self.assertIsNone(load_vectors(self.units, self.store, 'snap'))
        build_step(self.units, self.store, FakeEmbedder(), 'snap', deadline=math.inf, max_batches=0)
        self.assertIsNone(load_vectors(self.units, self.store, 'snap'))

    def test_returns_rows_and_per_id_vectors_when_covered(self):
        build_step(self.units, self.store, FakeEmbedder(), 'snap', deadline=math.inf)
        rows, vectors = load_vectors(self.units, self.store, 'snap')
        self.assertEqual([row['id'] for row in rows], [1, 2, 3])
        self.assertEqual(sorted(vectors), [1, 2, 3])
        self.assertEqual([len(vectors[i]) for i in (1, 2, 3)], [1, 2, 1])
        self.assertTrue(all(len(vector) == 256 for found in vectors.values() for vector in found))
        self.assertIsNone(load_vectors(self.units, self.store, 'another-snapshot'))

    def test_none_when_coverage_count_differs_from_units(self):
        build_step(self.units, self.store, FakeEmbedder(), 'snap', deadline=math.inf)
        self.assertIsNone(load_vectors(self.units + [unit(4)], self.store, 'snap'))

    def test_none_when_a_cached_row_is_deleted(self):
        build_step(self.units, self.store, FakeEmbedder(), 'snap', deadline=math.inf)
        with self.store.connection:
            self.store.connection.execute('DELETE FROM artifact_vectors WHERE key=?', (rv._key(IDENTITY, self.units[0]),))
        self.assertIsNone(load_vectors(self.units, self.store, 'snap'))


class AbstentionTests(unittest.TestCase):
    OUTLIER = [0.1] * 7 + [0.9]

    def test_policy_none_never_abstains_with_scores(self):
        for scores in ([-1.0], [0.0] * 3, self.OUTLIER, [0.5] * 20):
            decision = abstention(scores)
            self.assertEqual(decision['policy'], 'none')
            self.assertFalse(decision['abstained'])
            self.assertIsNone(decision['reason'])

    def test_empty_scores_abstain_with_no_vectors(self):
        for kwargs in ({}, {'min_cosine': 0.1}, {'min_zscore': 1.0}):
            decision = abstention([], **kwargs)
            self.assertTrue(decision['abstained'])
            self.assertEqual(decision['reason'], 'no_vectors')
            self.assertEqual(decision['signal'], {'units': 0, 'top_cosine': None, 'zscore': None})
        self.assertEqual(abstention([])['policy'], 'none')
        self.assertEqual(abstention([], min_cosine=0.1)['policy'], 'score')

    def test_cosine_floor(self):
        low = abstention([0.2, 0.4], min_cosine=0.5)
        self.assertTrue(low['abstained'])
        self.assertEqual(low['reason'], 'below_cosine_floor')
        self.assertEqual(low['signal']['top_cosine'], 0.4)
        self.assertFalse(abstention([0.2, 0.5], min_cosine=0.5)['abstained'])

    def test_zscore_margin_passes_clear_outlier(self):
        decision = abstention(self.OUTLIER, min_zscore=2.0)
        self.assertFalse(decision['abstained'])
        self.assertAlmostEqual(decision['signal']['zscore'], 0.7 / math.sqrt(0.07), places=5)
        self.assertEqual(decision['signal']['top_cosine'], 0.9)

    def test_zscore_margin_abstains_on_flat_distribution(self):
        decision = abstention([0.5] * 8, min_zscore=1.0)
        self.assertTrue(decision['abstained'])
        self.assertEqual(decision['reason'], 'below_score_margin')
        self.assertEqual(decision['signal']['zscore'], 0.0)

    def test_zscore_needs_enough_units(self):
        scores = [0.1] * 6 + [0.9]
        self.assertLess(len(scores), rv.MIN_ZSCORE_UNITS)
        decision = abstention(scores, min_zscore=1.0)
        self.assertTrue(decision['abstained'])
        self.assertEqual(decision['reason'], 'too_few_units_for_margin')
        self.assertIsNone(decision['signal']['zscore'])
        self.assertFalse(abstention(scores, min_cosine=0.5)['abstained'])

    def test_cosine_floor_is_evaluated_before_margin(self):
        self.assertEqual(abstention([0.5] * 8, min_cosine=0.9, min_zscore=1.0)['reason'], 'below_cosine_floor')


class RankTests(unittest.TestCase):
    QUERY = [1.0, 0.0, 0.0]

    def corpus(self, count=10):
        """Rows 1..count whose cosine to QUERY strictly decreases with the id."""
        rows = [unit(n) for n in range(1, count + 1)]
        vectors = {n: [[1.0, 0.1 * n, 0.0]] for n in range(1, count + 1)}
        return rows, vectors

    def test_vector_mode_orders_by_cosine_and_limits_top(self):
        rows, vectors = self.corpus()
        results, similarity, decision = rank(rows, vectors, self.QUERY, mode='vector', top=3, lexical_ids=[])
        self.assertEqual([identifier for identifier, _ in results], [1, 2, 3])
        self.assertGreater(results[0][1], results[1][1])
        self.assertEqual(set(similarity), set(range(1, 11)))
        self.assertFalse(decision['abstained'])

    def test_vector_mode_uses_best_segment(self):
        rows = [unit(1), unit(2)]
        vectors = {1: [[0.0, 1.0, 0.0], [0.0, 0.0, 1.0]], 2: [[0.0, 1.0, 0.0], [1.0, 0.0, 0.0]]}
        results, _, _ = rank(rows, vectors, self.QUERY, mode='vector', top=2, lexical_ids=[])
        self.assertEqual(results[0][0], 2)
        self.assertAlmostEqual(results[0][1], 1.0)

    def test_ties_break_by_path_number_then_id(self):
        rows = [unit(1, path='b.md'), unit(2, path='a.md'), unit(3, path='a.md'), unit(4, path='a.md')]
        rows[1]['number'], rows[2]['number'], rows[3]['number'] = 2, 1, 1
        vectors = {row['id']: [[1.0, 0.0, 0.0]] for row in rows}
        results, _, _ = rank(rows, vectors, self.QUERY, mode='vector', top=4, lexical_ids=[])
        self.assertEqual([identifier for identifier, _ in results], [3, 4, 2, 1])

    def test_hybrid_fuses_only_the_window_of_each_channel(self):
        rows, vectors = self.corpus()
        lexical = [10, 9, 8, 7, 1, 6, 5]
        wrapped = mock.Mock(wraps=rv.reciprocal_rank_fusion)
        with mock.patch.object(rv, 'reciprocal_rank_fusion', wrapped):
            results, _, _ = rank(rows, vectors, self.QUERY, mode='hybrid', top=5, lexical_ids=lexical)
        wrapped.assert_called_once_with([10, 9, 8, 7, 1], [1, 2, 3, 4, 5])
        ids = [identifier for identifier, _ in results]
        self.assertEqual(ids[0], 1)  # best in both windows
        self.assertNotIn(6, ids)
        with mock.patch.object(rv, 'reciprocal_rank_fusion', wrapped):
            rank(rows, vectors, self.QUERY, mode='hybrid', top=7, lexical_ids=lexical)
        self.assertEqual(wrapped.call_args.args, ([10, 9, 8, 7, 1, 6, 5], [1, 2, 3, 4, 5, 6, 7]))

    def test_hybrid_window_is_at_least_five_for_small_top(self):
        rows, vectors = self.corpus()
        wrapped = mock.Mock(wraps=rv.reciprocal_rank_fusion)
        with mock.patch.object(rv, 'reciprocal_rank_fusion', wrapped):
            results, _, _ = rank(rows, vectors, self.QUERY, mode='hybrid', top=1, lexical_ids=[10, 9, 8, 7, 6, 5])
        wrapped.assert_called_once_with([10, 9, 8, 7, 6], [1, 2, 3, 4, 5])
        self.assertEqual(len(results), 1)

    def test_hybrid_rewards_agreement_between_channels(self):
        rows, vectors = self.corpus()
        results, _, _ = rank(rows, vectors, self.QUERY, mode='hybrid', top=3, lexical_ids=[3, 2, 1])
        # 1 and 3 are first in one channel and last in the other; 2 is middle in both and scores lower.
        self.assertEqual([identifier for identifier, _ in results], [1, 3, 2])

    def test_lexical_ids_not_in_rows_are_ignored(self):
        rows, vectors = self.corpus(6)
        wrapped = mock.Mock(wraps=rv.reciprocal_rank_fusion)
        with mock.patch.object(rv, 'reciprocal_rank_fusion', wrapped):
            results, _, _ = rank(rows, vectors, self.QUERY, mode='hybrid', top=10, lexical_ids=[99, 4, 98, 2])
        self.assertEqual(wrapped.call_args.args[0], [4, 2])
        self.assertTrue({99, 98}.isdisjoint(identifier for identifier, _ in results))

    def test_abstention_returns_no_results(self):
        rows, vectors = self.corpus()
        for mode in ('vector', 'hybrid'):
            results, similarity, decision = rank(rows, vectors, self.QUERY, mode=mode, top=5,
                                                 lexical_ids=[1, 2], min_cosine=1.0 + 1e-9)
            self.assertEqual(results, [])
            self.assertTrue(decision['abstained'])
            self.assertEqual(decision['reason'], 'below_cosine_floor')
            self.assertEqual(len(similarity), 10)
        results, _, decision = rank([], {}, self.QUERY, mode='vector', top=5, lexical_ids=[])
        self.assertEqual((results, decision['reason']), ([], 'no_vectors'))

    def test_invalid_mode_raises(self):
        rows, vectors = self.corpus(2)
        for mode in ('lexical', 'bogus', ''):
            with self.assertRaises(VectorError) as caught:
                rank(rows, vectors, self.QUERY, mode=mode, top=5, lexical_ids=[])
            self.assertEqual(str(caught.exception), 'invalid_mode')


class TransientTests(unittest.TestCase):
    def test_transport_error_and_gateway_statuses_are_transient(self):
        self.assertTrue(transient(EmbedderUnavailable('embedding_transport_error')))
        for status in (408, 429, 500, 502, 503, 504):
            with self.subTest(status=status):
                self.assertTrue(transient(EmbedderUnavailable('embedding_http_error', status)))

    def test_operator_actionable_failures_are_not_transient(self):
        for status in (400, 401, 403, 404, 501):
            with self.subTest(status=status):
                self.assertFalse(transient(EmbedderUnavailable('embedding_http_error', status)))
        for reason in ('embedding_gateway_revision_mismatch', 'embedding_invalid_response', 'embedding_invalid_json',
                       'embedding_response_too_large', 'embedding_http_error'):
            with self.subTest(reason=reason):
                self.assertFalse(transient(EmbedderUnavailable(reason)))

    def test_other_exceptions_are_not_transient(self):
        self.assertFalse(transient(RuntimeError('embedding_transport_error')))
        self.assertFalse(transient(VectorError('embedding_identity_mismatch')))


class HelperTests(unittest.TestCase):
    def test_error_code_accepts_only_bounded_codes(self):
        self.assertEqual(rv.error_code(VectorError('vector_cache_full'), 'x'), 'vector_cache_full')
        self.assertEqual(rv.error_code(RuntimeError('Has Spaces and /paths'), 'fallback'), 'fallback')
        self.assertEqual(rv.error_code(RuntimeError(), 'fallback'), 'fallback')
        self.assertEqual(rv.error_code(RuntimeError('a' * 65), 'fallback'), 'fallback')

    def test_retry_delay_is_bounded_exponential(self):
        self.assertEqual([rv.retry_delay(n) for n in (0, 1, 2, 3)], [5, 5, 10, 20])
        self.assertEqual(rv.retry_delay(1000), rv.MAX_RETRY_SECONDS)


if __name__ == '__main__':
    unittest.main()
