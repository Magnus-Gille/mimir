from __future__ import annotations

import sqlite3
import unittest

from tools.artifact_search.vectors import (
    EmbeddingIdentity,
    cache_key,
    cosine,
    create_schema,
    document_segments,
    get_cached,
    normalize,
    put_cached,
    query_input,
    reciprocal_rank_fusion,
)


class VectorPrimitiveTests(unittest.TestCase):
    def test_identity_and_content_keys_are_stable_and_path_independent(self) -> None:
        identity = EmbeddingIdentity("rev-1")
        key = cache_key(identity, "Report", "hello", "pipeline-1")
        self.assertEqual(key, cache_key(identity, "Report", "hello", "pipeline-1"))
        self.assertNotEqual(key, cache_key(EmbeddingIdentity("rev-2"), "Report", "hello", "pipeline-1"))
        self.assertNotEqual(key, cache_key(identity, "Report", "hello!", "pipeline-1"))
        self.assertNotEqual(key, cache_key(identity, "Report", "hello", "pipeline-2"))

    def test_identity_rejects_unbounded_or_unsupported_values(self) -> None:
        with self.assertRaises(ValueError):
            EmbeddingIdentity(" " * 2)
        with self.assertRaises(ValueError):
            EmbeddingIdentity("x" * 129)
        with self.assertRaises(ValueError):
            EmbeddingIdentity("rev", dimensions=2)
        with self.assertRaises(ValueError):
            EmbeddingIdentity("rev", model="other")

    def test_segmentation_preserves_unicode_body_and_bounds_title(self) -> None:
        title = "å" * 700
        body = "A🙂\nB漢字\x00C"
        segments = document_segments(title, body, max_chars=3)
        prefix, first_body = segments[0].split(" | text: ", 1)
        self.assertEqual(prefix, "title: " + "å" * 512)
        self.assertEqual("".join(segment.split(" | text: ", 1)[1] for segment in segments), body)
        self.assertTrue(all(first_body for _ in [0]))
        self.assertEqual(document_segments("", "", max_chars=3), [])

    def test_query_and_normalization_contract(self) -> None:
        self.assertEqual(query_input("find 🙂"), "task: search result | query: find 🙂")
        self.assertEqual(normalize([3, 4], 2), [0.6, 0.8])
        self.assertAlmostEqual(cosine([3, 4], [0, 5]), 0.8)
        for vector in ([1], [True, 0], [float("nan"), 0], [0, 0]):
            with self.subTest(vector=vector):
                with self.assertRaises((TypeError, ValueError)):
                    normalize(vector, 2)
        with self.assertRaises(ValueError):
            cosine([1, 0], [1])

    def test_normalization_scales_large_and_tiny_finite_values(self) -> None:
        large = normalize([1e308, 1e308, 1e308, 1e308], 4)
        self.assertTrue(all(value == 0.5 for value in large))
        self.assertAlmostEqual(cosine([1e308] * 4, [1e308] * 4), 1.0)

        tiny = normalize([1e-320, 2e-320], 2)
        self.assertTrue(all(value != 0.0 for value in tiny))
        self.assertAlmostEqual(tiny[0], 1 / (5**0.5), places=12)
        self.assertAlmostEqual(tiny[1], 2 / (5**0.5), places=12)

    def test_cache_round_trip_and_corruption_miss(self) -> None:
        conn = sqlite3.connect(":memory:")
        create_schema(conn)
        vectors = [[3, 4], [0, 2]]
        put_cached(conn, "key", vectors, 2)
        self.assertEqual(get_cached(conn, "key", 2), [[0.6, 0.8], [0.0, 1.0]])
        self.assertIsNone(get_cached(conn, "key", 3))
        with self.assertRaises(ValueError):
            put_cached(conn, "empty", [], 2)
        conn.execute(
            "INSERT INTO artifact_vectors(key, dimensions, vectors) VALUES (?, ?, ?)",
            ("empty", 2, "[]"),
        )
        self.assertIsNone(get_cached(conn, "empty", 2))
        conn.execute(
            "UPDATE artifact_vectors SET vectors = ? WHERE key = ?",
            ("[[1, 1]]", "key"),
        )
        self.assertIsNone(get_cached(conn, "key", 2))

    def test_rrf_deduplicates_each_ranked_list(self) -> None:
        scores = reciprocal_rank_fusion([10, 10, 20], [20, 30, 20], constant=1)
        self.assertEqual(set(scores), {10, 20, 30})
        self.assertAlmostEqual(scores[10], 1 / 2)
        self.assertAlmostEqual(scores[20], 1 / 4 + 1 / 2)
        self.assertAlmostEqual(scores[30], 1 / 3)


if __name__ == "__main__":
    unittest.main()
