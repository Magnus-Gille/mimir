import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.artifact_search.evaluate import evaluate


class EvaluateTests(unittest.TestCase):
    def _core(self, search):
        module = types.ModuleType("tools.artifact_search.core")
        module.search = search
        return module

    def test_reports_hits_ranks_and_mrr_without_echoing_queries_or_sources(self):
        def search(db, query, top):
            self.assertEqual(db, Path("index.db"))
            self.assertEqual(top, 3)
            if query == "alpha":
                return {
                    "results": [
                        {"path": "wrong.txt", "locator": {"kind": "page", "number": 1}},
                        {"path": "docs/a.pdf", "locator": {"kind": "page", "number": 3}},
                    ],
                    "stale_candidates": 0,
                    "unavailable_candidates": 0,
                }
            return {"results": []}

        cases = [
            {
                "label": "positive",
                "query": "alpha",
                "expected": [{"path": "docs/a.pdf", "kind": "page", "number": 3}],
            },
            {"label": "negative", "query": "no-match", "expected": []},
        ]
        with patch.dict(sys.modules, {"tools.artifact_search.core": self._core(search)}):
            from tools.artifact_search.evaluate import evaluate

            report = evaluate(Path("index.db"), cases, top=3)

        self.assertEqual(report["case_count"], 2)
        self.assertEqual(report["positive_case_count"], 1)
        self.assertEqual(report["hit_at_k"], 1.0)
        self.assertEqual(report["mean_reciprocal_rank"], 0.5)
        self.assertEqual(report["top"], 3)
        self.assertEqual(report["results"][0]["label"], "positive")
        self.assertTrue(report["results"][0]["hit"])
        self.assertEqual(report["results"][0]["rank"], 2)
        self.assertTrue(report["results"][1]["hit"])
        self.assertIsNone(report["results"][1]["rank"])
        self.assertNotIn("query", report)
        self.assertNotIn("expected", report)
        self.assertNotIn("alpha", repr(report))
        self.assertNotIn("docs/a.pdf", repr(report))

    def test_matching_requires_path_kind_and_number(self):
        def search(_db, _query, _top):
            return [{"path": "same.pdf", "locator": {"kind": "page", "number": 2}}]

        cases = [{"label": "wrong", "query": "q", "expected": [{"path": "same.pdf", "kind": "section", "number": 2}]}]
        with patch.dict(sys.modules, {"tools.artifact_search.core": self._core(search)}):
            from tools.artifact_search.evaluate import evaluate

            report = evaluate(Path("index.db"), cases)

        self.assertEqual(report["hit_at_k"], 0.0)
        self.assertEqual(report["mean_reciprocal_rank"], 0.0)
        self.assertFalse(report["results"][0]["hit"])
        self.assertIsNone(report["results"][0]["rank"])
        self.assertEqual(report["positive_case_count"], 1)

    def test_expected_sha256_rejects_stale_and_missing_hit_hashes(self):
        def search(_db, query, _top):
            hit = {"path": "same.pdf", "locator": {"kind": "page", "number": 2}}
            if query == "stale":
                hit["sha256"] = "b" * 64
            return [hit]

        cases = [
            {
                "label": "stale",
                "query": "stale",
                "expected": [{"path": "same.pdf", "kind": "page", "number": 2, "sha256": "a" * 64}],
            },
            {
                "label": "missing",
                "query": "missing",
                "expected": [{"path": "same.pdf", "kind": "page", "number": 2, "sha256": "a" * 64}],
            },
        ]
        report = evaluate(Path("index.db"), cases, search_fn=search)

        self.assertEqual(report["hit_at_k"], 0.0)
        self.assertEqual(report["mean_reciprocal_rank"], 0.0)
        self.assertFalse(report["results"][0]["hit"])
        self.assertFalse(report["results"][1]["hit"])
        self.assertIsNone(report["results"][0]["rank"])
        self.assertIsNone(report["results"][1]["rank"])

    def test_legacy_expected_without_sha256_still_matches(self):
        def search(_db, _query, _top):
            return [{"path": "same.pdf", "locator": {"kind": "page", "number": 2}}]

        report = evaluate(
            Path("index.db"),
            [{"label": "legacy", "query": "q", "expected": [{"path": "same.pdf", "kind": "page", "number": 2}]}],
            search_fn=search,
        )
        self.assertEqual(report["hit_at_k"], 1.0)

    def test_empty_and_invalid_inputs_raise_value_error(self):
        from tools.artifact_search.evaluate import evaluate

        invalid_cases = [
            [],
            [{"label": "missing-query", "expected": []}],
            [{"label": "bad-query", "query": 1, "expected": []}],
            [{"label": "bad-expected", "query": "q", "expected": [{"path": "x", "kind": "page"}]}],
        ]
        for cases in invalid_cases:
            with self.subTest(cases=cases), self.assertRaises(ValueError):
                evaluate(Path("index.db"), cases)
        with self.assertRaises(ValueError):
            evaluate(Path("index.db"), [{"label": "x", "query": "q", "expected": []}], top=101)
        with self.assertRaises(ValueError):
            evaluate(Path("index.db"), [{"label": "x", "query": "q", "expected": []}], top=0)


if __name__ == "__main__":
    unittest.main()
