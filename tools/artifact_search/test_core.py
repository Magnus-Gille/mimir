"""Contract tests for the artifact-search core.

The implementation is intentionally not imported through a fallback: running this
module before the core exists should fail red with the missing module error.
"""

from __future__ import annotations

import dataclasses
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from tools.artifact_search.core import index_corpus, search
from tools.artifact_search.extract import Unit


class ArtifactSearchContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tempdir.name)
        self.root = self.workspace / "archive"
        self.root.mkdir()
        self.db = self.workspace / "artifact-search.sqlite3"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _write(self, relative: str, content: str, *, mode: int | None = None) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        if mode is not None:
            path.chmod(mode)
        return path

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

    @staticmethod
    def _result_paths(payload: dict) -> list[str]:
        return [result["path"] for result in payload["results"]]

    @staticmethod
    def _assert_summary_shape(test: unittest.TestCase, summary: dict) -> None:
        test.assertEqual(
            set(summary),
            {
                "indexed_files",
                "indexed_units",
                "unchanged_files",
                "removed_files",
                "excluded_files",
            },
        )
        test.assertIsInstance(summary["indexed_files"], int)
        test.assertIsInstance(summary["indexed_units"], int)
        test.assertIsInstance(summary["unchanged_files"], int)
        test.assertIsInstance(summary["removed_files"], int)
        test.assertIsInstance(summary["excluded_files"], list)
        for excluded in summary["excluded_files"]:
            test.assertEqual(set(excluded), {"path", "reason"})
            test.assertIsInstance(excluded["path"], str)
            test.assertIsInstance(excluded["reason"], str)

    @staticmethod
    def _assert_search_shape(test: unittest.TestCase, payload: dict) -> None:
        test.assertEqual(set(payload), {"results", "stale_candidates", "unavailable_candidates"})
        test.assertIsInstance(payload["results"], list)
        test.assertIsInstance(payload["stale_candidates"], int)
        test.assertIsInstance(payload["unavailable_candidates"], int)
        for result in payload["results"]:
            test.assertEqual(
                set(result),
                {"path", "sha256", "locator", "title", "snippet", "mimir_url", "indexed_at"},
            )
            test.assertEqual(set(result["locator"]), {"kind", "number", "anchor"})

    def test_unit_contract_exposes_the_five_extraction_fields(self) -> None:
        fields = (
            {field.name for field in dataclasses.fields(Unit)}
            if dataclasses.is_dataclass(Unit)
            else set(getattr(Unit, "__annotations__", {}))
        )
        self.assertEqual(
            fields,
            {"kind", "number", "anchor", "title", "text"},
        )

    def test_html_slides_are_ranked_by_numeric_slide_number_for_ties(self) -> None:
        slides = [(number, f"Filler {number}", f"filler-{number}") for number in range(1, 11)]
        slides[1] = (2, "Repeated heading", "shared ranking")
        slides[9] = (10, "Repeated heading", "shared ranking")
        self._write(
            "deck.html",
            self._slides(*slides),
        )

        summary = index_corpus(self.root, self.db, ["deck.html"])
        self._assert_summary_shape(self, summary)
        self.assertEqual(summary["indexed_files"], 1)
        self.assertGreaterEqual(summary["indexed_units"], 2)

        payload = search(self.db, "ranking", top=5, base_url="https://files.example.test")
        self._assert_search_shape(self, payload)
        self.assertEqual(len(payload["results"]), 2)
        self.assertEqual(
            [
                (result["locator"]["kind"], result["locator"]["number"], result["locator"]["anchor"])
                for result in payload["results"]
            ],
            [("slide", 2, "slide-2"), ("slide", 10, "slide-10")],
        )
        for result in payload["results"]:
            self.assertTrue(result["mimir_url"].startswith("https://files.example.test"))
            self.assertIn("deck.html", result["mimir_url"])

    def test_queries_are_parameterized_and_utf8_case_insensitive(self) -> None:
        self._write("notes.html", self._slides((1, "Göteborg", "Malmö ÄLG token")))
        index_corpus(self.root, self.db, ["notes.html"])

        for query in ("' OR 1=1 --", '"quoted query', "MALMÖ", "älg"):
            with self.subTest(query=query):
                payload = search(self.db, query, top=5)
                self._assert_search_shape(self, payload)

        self.assertEqual(self._result_paths(search(self.db, "' OR 1=1 --")), [])
        self.assertEqual(len(search(self.db, "MALMÖ")["results"]), 1)
        self.assertEqual(len(search(self.db, "älg")["results"]), 1)

    def test_same_size_and_mtime_change_is_stale_until_reindexed_and_old_chunks_are_removed(self) -> None:
        path = self._write("mutable.html", self._slides((1, "Alpha", "AlphaToken")))
        index_corpus(self.root, self.db, ["mutable.html"])
        before = path.stat()

        path.write_text(self._slides((1, "Bravo", "BravoToken")), encoding="utf-8")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = path.stat()
        self.assertEqual(after.st_size, before.st_size)
        self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)

        stale = search(self.db, "AlphaToken")
        self._assert_search_shape(self, stale)
        self.assertEqual(stale["results"], [])
        self.assertGreaterEqual(stale["stale_candidates"], 1)

        summary = index_corpus(self.root, self.db, ["mutable.html"])
        self._assert_summary_shape(self, summary)
        self.assertEqual(summary["indexed_files"], 1)
        self.assertEqual(summary["unchanged_files"], 0)
        self.assertEqual(search(self.db, "AlphaToken")["results"], [])
        self.assertEqual(len(search(self.db, "BravoToken")["results"]), 1)

    def test_unchanged_indexing_reports_unchanged_files(self) -> None:
        self._write("same.html", self._slides((1, "Stable", "stable token")))
        first = index_corpus(self.root, self.db, ["same.html"])
        second = index_corpus(self.root, self.db, ["same.html"])
        self._assert_summary_shape(self, first)
        self._assert_summary_shape(self, second)
        self.assertEqual(first["indexed_files"], 1)
        self.assertEqual(second["indexed_files"], 0)
        self.assertEqual(second["unchanged_files"], 1)
        self.assertEqual(second["removed_files"], 0)
        self.assertEqual(second["excluded_files"], [])

    def test_source_removal_and_rename_do_not_leave_old_results(self) -> None:
        old = self._write("old.html", self._slides((1, "Old", "old-only token")))
        index_corpus(self.root, self.db, ["old.html"])
        old.unlink()

        missing_source = search(self.db, "old-only")
        self._assert_search_shape(self, missing_source)
        self.assertEqual(missing_source["results"], [])
        self.assertGreaterEqual(missing_source["unavailable_candidates"], 1)

        removed = index_corpus(self.root, self.db, [])
        self._assert_summary_shape(self, removed)
        self.assertGreaterEqual(removed["removed_files"], 1)
        unavailable = search(self.db, "old-only")
        self._assert_search_shape(self, unavailable)
        self.assertEqual(unavailable["results"], [])

        renamed = self._write("before-rename.html", self._slides((1, "Renamed", "rename-only token")))
        index_corpus(self.root, self.db, ["before-rename.html"])
        renamed.rename(self.root / "after-rename.html")
        summary = index_corpus(self.root, self.db, ["after-rename.html"])
        self._assert_summary_shape(self, summary)
        for query in ("rename-only", "token"):
            with self.subTest(query=query):
                self.assertEqual(self._result_paths(search(self.db, query)), ["after-rename.html"])

    def test_manifest_excludes_symlinks_absolute_parent_hidden_and_secret_paths(self) -> None:
        self._write("safe.html", self._slides((1, "Safe", "safe token")))
        target = self._write("target.html", self._slides((1, "Target", "target token")))
        target_dir = self.root / "target-dir"
        target_dir.mkdir()
        self._write("target-dir/nested.html", self._slides((1, "Nested", "nested token")))
        os.symlink(target, self.root / "linked.html")
        os.symlink(target_dir, self.root / "linked-dir")
        self._write(".hidden.html", self._slides((1, "Hidden", "hidden token")))
        self._write("secrets/manifest.html", self._slides((1, "Secret", "secret path token")))
        outside = self.workspace / "outside.html"
        outside.write_text(self._slides((1, "Outside", "outside token")), encoding="utf-8")

        absolute = str(self.root / "absolute.html")
        self._write("absolute.html", self._slides((1, "Absolute", "absolute token")))
        manifest = [
            "safe.html",
            "linked.html",
            "linked-dir/nested.html",
            absolute,
            "../outside.html",
            ".hidden.html",
            "secrets/manifest.html",
        ]
        summary = index_corpus(self.root, self.db, manifest)
        self._assert_summary_shape(self, summary)
        self.assertEqual(summary["indexed_files"], 1)
        self.assertEqual(self._result_paths(search(self.db, "safe token")), ["safe.html"])
        for query in ("target", "nested", "absolute", "outside", "hidden", "secret"):
            with self.subTest(query=query):
                self.assertEqual(search(self.db, query)["results"], [])
        excluded_paths = {entry["path"] for entry in summary["excluded_files"]}
        for supplied in manifest[1:]:
            self.assertTrue(
                supplied in excluded_paths or any(path.endswith(supplied) for path in excluded_paths),
                f"manifest path was not reported excluded: {supplied!r}",
            )

    def test_secret_in_visible_html_and_raw_hidden_script_is_excluded(self) -> None:
        synthetic_key = "AKIA" + ("0" * 16)
        self._write("safe.html", self._slides((1, "Safe", "ordinary searchable text")))
        self._write(
            "visible-secret.html",
            self._slides((1, "Visible", f"contains synthetic secret {synthetic_key}")),
        )
        self._write(
            "raw-secret.html",
            "<!doctype html><html><body><section class=\"slide\" id=\"slide-1\">"
            "<h1>Raw secret</h1><p>ordinary text</p>"
            f'<script type="application/json" hidden>{{"api_key":"{synthetic_key}"}}</script>'
            "</section></body></html>",
        )

        summary = index_corpus(self.root, self.db, ["safe.html", "visible-secret.html", "raw-secret.html"])
        self._assert_summary_shape(self, summary)
        excluded = {entry["path"] for entry in summary["excluded_files"]}
        self.assertIn("visible-secret.html", excluded)
        self.assertIn("raw-secret.html", excluded)
        self.assertEqual(self._result_paths(search(self.db, "ordinary searchable text")), ["safe.html"])
        serialized = json.dumps(search(self.db, "synthetic secret"), ensure_ascii=False)
        self.assertNotIn(synthetic_key, serialized)

    def test_indexing_outside_archive_preserves_private_original_bytes_and_mtimes(self) -> None:
        self.root.chmod(0o700)
        first = self._write("private/one.html", self._slides((1, "One", "private one")), mode=0o600)
        second = self._write("private/two.html", self._slides((2, "Two", "private two")), mode=0o600)
        snapshot = {
            path: (path.read_bytes(), path.stat().st_mtime_ns, stat.S_IMODE(path.stat().st_mode))
            for path in (first, second)
        }

        index_corpus(self.root, self.db, ["private/one.html", "private/two.html"])
        self.assertFalse(self.db.is_relative_to(self.root))
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        for path, (contents, mtime_ns, mode) in snapshot.items():
            self.assertEqual(path.read_bytes(), contents)
            self.assertEqual(path.stat().st_mtime_ns, mtime_ns)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode)

    def test_existing_database_cannot_be_reused_for_another_root(self) -> None:
        self._write("one.html", self._slides((1, "One", "one token")))
        index_corpus(self.root, self.db, ["one.html"])
        other_root = self.workspace / "other-archive"
        other_root.mkdir()
        (other_root / "two.html").write_text(self._slides((2, "Two", "two token")), encoding="utf-8")

        with self.assertRaises(ValueError):
            index_corpus(other_root, self.db, ["two.html"])

    def test_database_inside_archive_is_rejected(self) -> None:
        self._write("one.html", self._slides((1, "One", "one token")))
        with self.assertRaises(ValueError):
            index_corpus(self.root, self.root / "index.sqlite3", ["one.html"])


if __name__ == "__main__":
    unittest.main()
