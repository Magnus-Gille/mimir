from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.artifact_search.evaluate import compare, evaluate
from tools.artifact_search.vectors import EmbeddingIdentity


class FakeEmbedder:
    def __init__(self, identity: EmbeddingIdentity):
        self.identity = identity
        self.calls: list[list[str]] = []

    def embed(self, inputs: list[str]) -> list[list[float]]:
        self.calls.append(list(inputs))
        values = []
        for item in inputs:
            vector = [0.0] * self.identity.dimensions
            vector[0 if "orchard" in item else 1 if "glacier" in item else 2] = 1.0
            values.append(vector)
        return values


class CompareTests(unittest.TestCase):
    def test_evaluate_accepts_search_function_and_reports_actual_mode(self) -> None:
        cases = [{"label": "q", "query": "orchard", "expected": []}]

        def search(_db, _query, _top):
            return {"results": [], "mode": "lexical_fallback"}

        report = evaluate(Path("index.db"), cases, search_fn=search)
        self.assertEqual(report["results"][0]["mode"], "lexical_fallback")

    def test_compare_shares_query_embeddings_and_reports_quality(self) -> None:
        identity = EmbeddingIdentity("test", dimensions=128)
        embedder = FakeEmbedder(identity)
        cases = [
            {"label": "orchard", "query": "orchard", "expected": [{"path": "orchard.txt", "kind": "page", "number": 1}]},
            {"label": "glacier", "query": "glacier", "expected": [{"path": "glacier.txt", "kind": "page", "number": 1}]},
            {"label": "negative", "query": "unknown", "expected": []},
        ]

        def lexical(_db, query, _top):
            result = [] if query == "unknown" else [{"path": query + ".txt", "locator": {"kind": "page", "number": 1}}]
            return {"results": result, "mode": "lexical"}

        def semantic_search(_db, query, memo, received_identity, *, top, mode, min_score):
            self.assertIs(received_identity, identity)
            vector = memo.embed([query_input(query)])[0]
            del vector
            if query == "unknown":
                result = []
            else:
                result = [{"path": query + ".txt", "locator": {"kind": "page", "number": 1}}]
            actual_mode = "lexical_fallback" if mode == "hybrid" and query == "unknown" else mode
            return {"results": result[:top], "mode": actual_mode}

        from tools.artifact_search.vectors import query_input

        semantic_module = types.ModuleType("tools.artifact_search.semantic")
        semantic_module.semantic_search = semantic_search
        with patch.dict(sys.modules, {"tools.artifact_search.core": types.SimpleNamespace(search=lexical), "tools.artifact_search.semantic": semantic_module}):
            report = compare(Path("index.db"), cases, embedder, identity, top=1, min_score=0.25)

        self.assertEqual(report["lexical"]["hit_at_k"], 1.0)
        self.assertEqual(report["vector"]["hit_at_k"], 1.0)
        self.assertEqual(report["hybrid"]["hit_at_k"], 1.0)
        self.assertEqual(report["query_embedding_calls"], 3)
        self.assertTrue(report["shared_query_embeddings"])
        self.assertEqual(report["cosine_floor"], 0.25)
        self.assertEqual(report["hybrid"]["results"][-1]["mode"], "lexical_fallback")
        self.assertEqual(len(embedder.calls), 3)

    def test_vector_failure_is_propagated(self) -> None:
        identity = EmbeddingIdentity("test", dimensions=128)
        semantic_module = types.ModuleType("tools.artifact_search.semantic")

        def failing_search(*_args, **_kwargs):
            raise RuntimeError("vector unavailable")

        semantic_module.semantic_search = failing_search
        core_module = types.SimpleNamespace(search=lambda *_args: {"results": []})
        cases = [{"label": "q", "query": "orchard", "expected": []}]
        with patch.dict(sys.modules, {"tools.artifact_search.core": core_module, "tools.artifact_search.semantic": semantic_module}), self.assertRaises(RuntimeError):
            compare(Path("index.db"), cases, FakeEmbedder(identity), identity)


if __name__ == "__main__":
    unittest.main()
