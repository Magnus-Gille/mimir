"""Acceptance tests for the local semantic artifact-search layer.

The semantic module is intentionally imported inside each test.  That keeps this
contract suite collectible while the implementation is being developed, and
produces a useful red failure when the module has not been added yet.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from tools.artifact_search.core import index_corpus, search


class FakeEmbedder:
    """Deterministic, local-only stand-in for the configured embedding service."""

    dimension = 128

    def __init__(
        self,
        identity,
        *,
        unavailable: bool = False,
        invalid: bool = False,
        invalid_count: bool = False,
        callback=None,
    ):
        self.identity = identity
        self.unavailable = unavailable
        self.invalid = invalid
        self.invalid_count = invalid_count
        self.callback = callback
        self.inputs: list[str] = []

    @staticmethod
    def _topic(text: str) -> str:
        lowered = text.casefold()
        for topic in (
            "exoplanet",
            "quantum",
            "orchard",
            "glacier",
            "strawberry",
            "transit",
        ):
            if topic in lowered:
                return topic
        return "other"

    def embed(self, inputs: list[str]) -> list[list[float]]:
        self.inputs.extend(inputs)
        if self.callback is not None:
            self.callback(inputs)
        if self.unavailable:
            raise RuntimeError("synthetic embedding outage")
        if self.invalid_count:
            return []
        if self.invalid:
            return [[0.0] * (self.dimension - 1) for _ in inputs]
        vectors: list[list[float]] = []
        for value in inputs:
            vector = [0.0] * self.dimension
            digest = hashlib.sha256(self._topic(value).encode("ascii")).digest()
            index = int.from_bytes(digest[:2], "big") % self.dimension
            vector[index] = 1.0
            vectors.append(vector)
        return vectors


class SemanticArtifactSearchTests(unittest.TestCase):
    def test_missing_vectors_hybrid_is_labelled_lexical_without_model_call(self):
        self._write('uncached.html', self._slides((1, 'Orchard', 'orchard strawberries')))
        index_corpus(self.root, self.db, ['uncached.html'])
        _, semantic_search, _ = self._semantic()
        identity = self._identity()
        embedder = FakeEmbedder(identity, unavailable=True)
        result = semantic_search(self.db, 'orchard', embedder, identity, mode='hybrid')
        self.assertEqual(result['mode'], 'lexical_fallback')
        self.assertEqual(result['warning'], 'vectors_missing')
        self.assertEqual(len(result['results']), 1)
        self.assertEqual(embedder.inputs, [])

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        workspace = Path(self.tempdir.name)
        self.root = workspace / "archive"
        self.root.mkdir()
        self.db = workspace / "artifact-search.sqlite3"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    @staticmethod
    def _slides(*slides: tuple[int, str, str]) -> str:
        sections = []
        for number, title, text in slides:
            sections.append(
                f'<section class="slide" id="slide-{number}" '
                f'data-slide-number="{number}">'
                f"<h1>{title}</h1><p>{text}</p></section>"
            )
        return "<!doctype html><html><body>" + "".join(sections) + "</body></html>"

    def _write(self, relative: str, content: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    @staticmethod
    def _identity(revision: str = "fake-revision-1"):
        from tools.artifact_search.vectors import EmbeddingIdentity

        return EmbeddingIdentity(revision=revision, dimensions=128)

    @staticmethod
    def _semantic():
        from tools.artifact_search.semantic import (
            EmbedderUnavailable,
            build_vectors,
            semantic_search,
        )

        return build_vectors, semantic_search, EmbedderUnavailable

    @staticmethod
    def _assert_result_shape(test: unittest.TestCase, payload: dict) -> None:
        expected = {
            "results",
            "stale_candidates",
            "unavailable_candidates",
            "mode",
        }
        if payload["mode"] == "hybrid":
            expected.add("candidate_window")
            test.assertIsInstance(payload["candidate_window"], int)
        test.assertEqual(set(payload), expected)
        test.assertIsInstance(payload["results"], list)
        for result in payload["results"]:
            test.assertEqual(
                set(result),
                {"path", "sha256", "locator", "title", "snippet", "mimir_url", "indexed_at"},
            )
            test.assertEqual(set(result["locator"]), {"kind", "number", "anchor"})

    def test_vector_result_has_location_provenance_and_google_prefixes(self) -> None:
        self._write(
            "science.html",
            self._slides(
                (1, "Quantum", "quantum entanglement and qubits"),
                (2, "Exoplanets", "orbital exoplanet transit observations"),
            ),
        )
        index_corpus(self.root, self.db, ["science.html"])
        build_vectors, semantic_search, _ = self._semantic()
        identity = self._identity()
        embedder = FakeEmbedder(identity)
        summary = build_vectors(self.db, embedder, identity)
        self.assertEqual(summary["embedded_units"], 2)

        payload = semantic_search(
            self.db,
            "find an exoplanet transit",
            embedder,
            identity,
            top=1,
            base_url="https://files.example.test",
        )
        self._assert_result_shape(self, payload)
        self.assertEqual(payload["mode"], "vector")
        self.assertEqual(payload["results"][0]["path"], "science.html")
        self.assertEqual(payload["results"][0]["locator"]["number"], 2)
        self.assertEqual(payload["results"][0]["locator"]["kind"], "slide")
        self.assertIn("science.html#slide-2", payload["results"][0]["mimir_url"])
        self.assertEqual(payload["results"][0]["sha256"], hashlib.sha256((self.root / "science.html").read_bytes()).hexdigest())
        self.assertTrue(any(value.startswith("task: search result | query: ") for value in embedder.inputs))
        self.assertTrue(any(value.startswith("title: ") and " | text: " in value for value in embedder.inputs))

    def test_unchanged_build_reuses_content_based_cache_without_source_mutation(self) -> None:
        first = self._write("one.html", self._slides((1, "Orchard", "orchard strawberries")))
        second = self._write("two.html", self._slides((1, "Glacier", "glacier melt")))
        snapshot = {path: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode) for path in (first, second)}
        index_corpus(self.root, self.db, ["one.html", "two.html"])
        build_vectors, _, _ = self._semantic()
        identity = self._identity()
        first_embedder = FakeEmbedder(identity)
        self.assertEqual(build_vectors(self.db, first_embedder, identity)["embedded_units"], 2)

        second_embedder = FakeEmbedder(identity)
        reused = build_vectors(self.db, second_embedder, identity)
        self.assertEqual(reused["embedded_units"], 0)
        self.assertEqual(reused["cached_units"], 2)
        self.assertEqual(reused["skipped_units"], 0)
        for path, (contents, mtime_ns, mode) in snapshot.items():
            self.assertEqual(path.read_bytes(), contents)
            self.assertEqual(path.stat().st_mtime_ns, mtime_ns)
            self.assertEqual(path.stat().st_mode, mode)

    def test_same_size_same_mtime_change_is_stale_and_reembedded_after_index(self) -> None:
        path = self._write("mutable.html", self._slides((1, "Stable", "orchard token")))
        index_corpus(self.root, self.db, ["mutable.html"])
        build_vectors, semantic_search, _ = self._semantic()
        identity = self._identity()
        embedder = FakeEmbedder(identity)
        build_vectors(self.db, embedder, identity)
        before = path.stat()

        path.write_text(self._slides((1, "Stable", "glacier token")), encoding="utf-8")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = path.stat()
        self.assertEqual(after.st_size, before.st_size)
        self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)

        stale = semantic_search(self.db, "orchard", embedder, identity)
        self._assert_result_shape(self, stale)
        self.assertEqual(stale["results"], [])
        self.assertGreaterEqual(stale["stale_candidates"], 1)

        index_corpus(self.root, self.db, ["mutable.html"])
        rebuilt = build_vectors(self.db, FakeEmbedder(identity), identity)
        self.assertEqual(rebuilt["embedded_units"], 1)
        self.assertEqual(semantic_search(self.db, "orchard", embedder, identity)["results"], [])
        self.assertEqual(len(semantic_search(self.db, "glacier", embedder, identity)["results"]), 1)

    def test_rename_reuses_same_text_embedding_but_updates_reference(self) -> None:
        old = self._write("old-name.html", self._slides((1, "Orchard", "orchard strawberries")))
        index_corpus(self.root, self.db, ["old-name.html"])
        build_vectors, semantic_search, _ = self._semantic()
        identity = self._identity()
        build_vectors(self.db, FakeEmbedder(identity), identity)

        old.rename(self.root / "new-name.html")
        index_corpus(self.root, self.db, ["new-name.html"])
        reused = build_vectors(self.db, FakeEmbedder(identity), identity)
        self.assertEqual(reused["embedded_units"], 0)
        self.assertEqual(reused["cached_units"], 1)
        result = semantic_search(self.db, "orchard", FakeEmbedder(identity), identity)["results"]
        self.assertEqual([entry["path"] for entry in result], ["new-name.html"])

    def test_no_cached_vectors_means_vector_search_has_no_invented_results(self) -> None:
        self._write("uncached.html", self._slides((1, "Orchard", "orchard strawberries")))
        index_corpus(self.root, self.db, ["uncached.html"])
        _, semantic_search, _ = self._semantic()
        identity = self._identity()
        payload = semantic_search(self.db, "orchard", FakeEmbedder(identity), identity)
        self._assert_result_shape(self, payload)
        self.assertEqual(payload["results"], [])

    def test_revision_mismatch_does_not_consume_old_vectors(self) -> None:
        self._write("revision.html", self._slides((1, "Orchard", "orchard strawberries")))
        index_corpus(self.root, self.db, ["revision.html"])
        build_vectors, semantic_search, _ = self._semantic()
        old_identity = self._identity("checkpoint-a")
        build_vectors(self.db, FakeEmbedder(old_identity), old_identity)
        new_identity = self._identity("checkpoint-b")
        payload = semantic_search(self.db, "orchard", FakeEmbedder(new_identity), new_identity)
        self.assertEqual(payload["results"], [])
        rebuilt = build_vectors(self.db, FakeEmbedder(new_identity), new_identity)
        self.assertEqual(rebuilt["embedded_units"], 1)
        self.assertEqual(rebuilt["cached_units"], 0)

    def test_vector_outage_raises_and_hybrid_falls_back_to_lexical(self) -> None:
        self._write("outage.html", self._slides((1, "Orchard", "orchard strawberries")))
        index_corpus(self.root, self.db, ["outage.html"])
        build_vectors, semantic_search, unavailable = self._semantic()
        identity = self._identity()
        healthy = FakeEmbedder(identity)
        build_vectors(self.db, healthy, identity)
        failing = FakeEmbedder(identity, unavailable=True)

        with self.assertRaises(unavailable):
            semantic_search(self.db, "orchard", failing, identity, mode="vector")
        fallback = semantic_search(self.db, "orchard", failing, identity, mode="hybrid")
        self.assertEqual(fallback["mode"], "lexical_fallback")
        self.assertEqual(fallback["warning"], "embedding_unavailable")
        self.assertEqual([entry["path"] for entry in fallback["results"]], ["outage.html"])

    def test_hybrid_mode_returns_fused_results_and_validates_top(self) -> None:
        self._write(
            "hybrid.html",
            self._slides(
                (1, "Orchard", "orchard strawberries"),
                (2, "Quantum", "quantum entanglement"),
            ),
        )
        index_corpus(self.root, self.db, ["hybrid.html"])
        build_vectors, semantic_search, _ = self._semantic()
        identity = self._identity()
        embedder = FakeEmbedder(identity)
        build_vectors(self.db, embedder, identity)
        payload = semantic_search(self.db, "orchard quantum", embedder, identity, mode="hybrid", top=2)
        self._assert_result_shape(self, payload)
        self.assertEqual(payload["mode"], "hybrid")
        self.assertEqual(payload["candidate_window"], 5)
        self.assertEqual(len(payload["results"]), 2)
        self.assertEqual({entry["locator"]["number"] for entry in payload["results"]}, {1, 2})
        for top in (0, 101):
            with self.assertRaises(ValueError):
                semantic_search(self.db, "orchard", embedder, identity, top=top)

    def test_hybrid_bounds_each_channel_before_fusing_tail_overlap(self) -> None:
        class RankedEmbedder(FakeEmbedder):
            @staticmethod
            def _vector(value: str) -> list[float]:
                lowered = value.casefold()
                vector = [0.0] * 128
                if lowered.startswith("task: search result") or "vector-only" in lowered:
                    vector[0] = 1.0
                elif "vector filler" in lowered:
                    vector[0] = 0.98
                    vector[1] = 0.198997
                elif "weak overlap" in lowered:
                    vector[0] = 0.99
                    vector[1] = 0.141067
                else:
                    vector[2] = 1.0
                return vector

            def embed(self, inputs: list[str]) -> list[list[float]]:
                self.inputs.extend(inputs)
                return [self._vector(value) for value in inputs]

        self._write(
            "vector-only.html",
            self._slides((1, "Vector target", "vector-only evidence")),
        )
        self._write(
            "lexical-only.html",
            self._slides((1, "fusiontoken fusiontoken lexical target", "fusiontoken evidence")),
        )
        self._write(
            "lexical-filler.html",
            self._slides((1, "fusiontoken lexical filler", "fusiontoken filler evidence")),
        )
        self._write(
            "vector-filler.html",
            self._slides((1, "Vector filler", "vector filler evidence")),
        )
        for number in range(12):
            self._write(
                f"weak-{number:02d}.html",
                self._slides((1, f"Weak overlap {number:02d}", f"fusiontoken weak overlap {number:02d}")),
            )
        index_corpus(
            self.root,
            self.db,
            ["vector-only.html", "lexical-only.html", "lexical-filler.html", "vector-filler.html"]
            + [f"weak-{number:02d}.html" for number in range(12)],
        )
        build_vectors, semantic_search, _ = self._semantic()
        identity = self._identity()
        embedder = RankedEmbedder(identity)
        build_vectors(self.db, embedder, identity)

        payload = semantic_search(
            self.db,
            "fusiontoken",
            RankedEmbedder(identity),
            identity,
            top=5,
            mode="hybrid",
        )

        paths = [entry["path"] for entry in payload["results"]]
        self.assertIn("vector-only.html", paths)
        self.assertIn("lexical-only.html", paths)
        self.assertEqual(len(paths), 5)
        self.assertEqual(payload["candidate_window"], 5)

    def test_invalid_normalized_response_is_not_cached(self) -> None:
        self._write("invalid.html", self._slides((1, "Orchard", "orchard strawberries")))
        index_corpus(self.root, self.db, ["invalid.html"])
        build_vectors, _, _ = self._semantic()
        identity = self._identity()
        with self.assertRaises((ValueError, RuntimeError)):
            build_vectors(self.db, FakeEmbedder(identity, invalid=True), identity)
        good = build_vectors(self.db, FakeEmbedder(identity), identity)
        self.assertEqual(good["embedded_units"], 1)
        self.assertEqual(good["cached_units"], 0)

    def test_source_reindexed_during_query_cannot_return_old_rowid_hash_or_snippet(self) -> None:
        path = self._write("racing.html", self._slides((1, "Orchard", "orchard token")))
        index_corpus(self.root, self.db, ["racing.html"])
        build_vectors, semantic_search, _ = self._semantic()
        identity = self._identity()
        build_vectors(self.db, FakeEmbedder(identity), identity)
        old_hash = hashlib.sha256(path.read_bytes()).hexdigest()

        def reindex_source(_inputs: list[str]) -> None:
            path.write_text(self._slides((1, "Glacier", "glacier token")), encoding="utf-8")
            index_corpus(self.root, self.db, ["racing.html"])

        payload = semantic_search(
            self.db,
            "orchard",
            FakeEmbedder(identity, callback=reindex_source),
            identity,
        )
        self.assertEqual(payload["results"], [])
        self.assertNotIn(old_hash, str(payload))
        self.assertNotIn("orchard token", str(payload))

    def test_source_removed_during_query_is_ignored(self) -> None:
        removable = self._write("transient.html", self._slides((1, "Orchard", "orchard token")))
        self._write("survivor.html", self._slides((1, "Quantum", "quantum token")))
        index_corpus(self.root, self.db, ["transient.html", "survivor.html"])
        build_vectors, semantic_search, _ = self._semantic()
        identity = self._identity()
        build_vectors(self.db, FakeEmbedder(identity), identity)

        def remove_source(_inputs: list[str]) -> None:
            removable.unlink()

        payload = semantic_search(
            self.db,
            "orchard",
            FakeEmbedder(identity, callback=remove_source),
            identity,
        )
        self.assertEqual(payload["results"], [])
        self.assertGreaterEqual(payload["unavailable_candidates"] + payload["stale_candidates"], 1)

    def test_hybrid_window_skips_stale_fts_rows_before_counting_current_candidates(self) -> None:
        stale = self._write("stale.html", self._slides((1, "Orchard", "orchard stale")))
        valid_paths = []
        for number in range(5):
            relative = f"valid-{number}.html"
            valid_paths.append(relative)
            self._write(relative, self._slides((1, "Orchard", f"orchard valid {number}")))
        index_corpus(self.root, self.db, ["stale.html"] + valid_paths)
        build_vectors, semantic_search, _ = self._semantic()
        identity = self._identity()
        build_vectors(self.db, FakeEmbedder(identity), identity)

        stale.write_text(self._slides((1, "Glacier", "glacier replacement")), encoding="utf-8")
        payload = semantic_search(
            self.db,
            "orchard",
            FakeEmbedder(identity),
            identity,
            top=5,
            mode="hybrid",
            min_score=1.0,
        )

        self.assertEqual(payload["candidate_window"], 5)
        self.assertEqual({entry["path"] for entry in payload["results"]}, set(valid_paths))
        self.assertGreaterEqual(payload["stale_candidates"], 1)

    def test_secret_filtered_chunks_never_reach_embedder(self) -> None:
        synthetic_key = "AKIA" + ("0" * 16)
        self._write("safe.html", self._slides((1, "Safe", "orchard strawberries")))
        self._write(
            "secret.html",
            self._slides((1, "Secret", f"orchard hidden {synthetic_key}")),
        )
        index_corpus(self.root, self.db, ["safe.html", "secret.html"])
        build_vectors, _, _ = self._semantic()
        identity = self._identity()
        embedder = FakeEmbedder(identity)
        summary = build_vectors(self.db, embedder, identity)
        self.assertEqual(summary["embedded_units"], 1)
        self.assertTrue(embedder.inputs)
        serialized = "\n".join(embedder.inputs)
        self.assertNotIn(synthetic_key, serialized)
        self.assertNotIn("secret", serialized.casefold())

    def test_invalid_batch_shape_and_count_commit_no_partial_cache(self) -> None:
        self._write("first.html", self._slides((1, "Orchard", "orchard strawberries")))
        self._write("second.html", self._slides((1, "Glacier", "glacier melt")))
        index_corpus(self.root, self.db, ["first.html", "second.html"])
        build_vectors, _, unavailable = self._semantic()
        identity = self._identity()

        with self.assertRaises(unavailable):
            build_vectors(self.db, FakeEmbedder(identity, invalid=True), identity)
        with self.assertRaises(unavailable):
            build_vectors(self.db, FakeEmbedder(identity, invalid_count=True), identity)

        good = build_vectors(self.db, FakeEmbedder(identity), identity)
        self.assertEqual(good["embedded_units"], 2)
        self.assertEqual(good["cached_units"], 0)


if __name__ == "__main__":
    unittest.main()
