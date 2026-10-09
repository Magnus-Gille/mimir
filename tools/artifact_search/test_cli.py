"""CLI acceptance tests for the local artifact-search command."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest


WORKTREE = Path(__file__).resolve().parents[2]


class ArtifactSearchCliTests(unittest.TestCase):
    def _run_cli(self, *arguments: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run(
            [sys.executable, "-m", "tools.artifact_search.cli", *arguments],
            cwd=WORKTREE,
            env=environment,
            input=stdin,
            text=True,
            capture_output=True,
            check=False,
        )

    @staticmethod
    def _manifest(workspace: Path, paths: list[str]) -> Path:
        manifest = workspace / "manifest.json"
        manifest.write_text(json.dumps(paths), encoding="utf-8")
        return manifest

    @staticmethod
    def _write_slide(root: Path, name: str, *, text: str = "needle") -> tuple[Path, bytes]:
        data = (
            '<!doctype html><html><body><section class="slide" id="raw-anchor">'
            f"<h1>CLI deck</h1><p>{text}</p></section></body></html>"
        ).encode("utf-8")
        path = root / name
        path.write_bytes(data)
        return path, data

    def _index(self, root: Path, db: Path, workspace: Path, paths: list[str]) -> dict:
        manifest = self._manifest(workspace, paths)
        completed = self._run_cli("index", "--root", str(root), "--db", str(db), "--manifest", str(manifest))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        return json.loads(completed.stdout)

    def test_index_then_search_from_stdin_returns_hash_slide_and_raw_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "archive"
            root.mkdir()
            db = workspace / "state" / "index.sqlite3"
            source, data = self._write_slide(root, "deck.html")

            summary = self._index(root, db, workspace, ["deck.html"])
            self.assertEqual(summary["indexed_files"], 1)
            self.assertEqual(summary["indexed_units"], 1)

            completed = self._run_cli(
                "search",
                "--db",
                str(db),
                "--base-url",
                "https://files.example.test",
                stdin="needle\n",
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["stale_candidates"], 0)
            self.assertEqual(payload["unavailable_candidates"], 0)
            self.assertEqual(len(payload["results"]), 1)
            result = payload["results"][0]
            self.assertEqual(result["path"], "deck.html")
            self.assertEqual(result["sha256"], hashlib.sha256(data).hexdigest())
            self.assertEqual(
                result["locator"],
                {"kind": "slide", "number": 1, "anchor": "raw-anchor"},
            )
            self.assertEqual(result["mimir_url"], "https://files.example.test/files/deck.html#raw-anchor")
            self.assertEqual(source.read_bytes(), data)

    def test_evaluate_emits_metrics_without_query_snippet_or_path_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "archive"
            root.mkdir()
            db = workspace / "state" / "index.sqlite3"
            self._write_slide(root, "deck.html", text="alpha searchable text")
            self._index(root, db, workspace, ["deck.html"])
            cases = workspace / "cases.json"
            cases.write_text(
                json.dumps(
                    [
                        {
                            "label": "positive",
                            "query": "alpha",
                            "expected": [{"path": "deck.html", "kind": "slide", "number": 1}],
                        },
                        {"label": "negative", "query": "absent", "expected": []},
                    ]
                ),
                encoding="utf-8",
            )

            completed = self._run_cli("evaluate", "--db", str(db), "--cases", str(cases))
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["case_count"], 2)
            self.assertEqual(payload["positive_case_count"], 1)
            self.assertEqual(payload["negative_case_count"], 1)
            self.assertEqual(payload["hit_at_k"], 1.0)
            self.assertEqual(payload["case_pass_rate"], 1.0)
            self.assertEqual(payload["negative_pass_rate"], 1.0)
            self.assertEqual([item["label"] for item in payload["results"]], ["positive", "negative"])
            output = completed.stdout.casefold()
            self.assertNotIn("alpha", output)
            self.assertNotIn("query", output)
            self.assertNotIn("snippet", output)
            self.assertNotIn('"path"', output)

    def test_malformed_manifest_is_json_error_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "archive"
            root.mkdir()
            db = workspace / "state" / "index.sqlite3"
            manifest = workspace / "malformed.json"
            manifest.write_text("[\"deck.html\"", encoding="utf-8")

            completed = self._run_cli(
                "index",
                "--root",
                str(root),
                "--db",
                str(db),
                "--manifest",
                str(manifest),
            )
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(completed.stdout, "")
            self.assertNotIn("Traceback", completed.stderr)
            error = json.loads(completed.stderr)
            self.assertIn("error", error)

    def test_image_and_corrupt_pdf_are_excluded_without_source_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "archive"
            root.mkdir()
            db = workspace / "state" / "index.sqlite3"
            image = root / "image.png"
            corrupt_pdf = root / "broken.pdf"
            image.write_bytes(b"synthetic image bytes")
            corrupt_pdf.write_bytes(b"simplebadbytes")
            snapshots = {
                path: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in (image, corrupt_pdf)
            }

            summary = self._index(root, db, workspace, ["image.png", "broken.pdf"])
            excluded = {entry["path"]: entry["reason"] for entry in summary["excluded_files"]}
            self.assertIn("image.png", excluded)
            self.assertIn("broken.pdf", excluded)
            self.assertTrue(excluded["image.png"])
            self.assertTrue(excluded["broken.pdf"])
            for path, (contents, mtime_ns) in snapshots.items():
                self.assertEqual(path.read_bytes(), contents)
                self.assertEqual(path.stat().st_mtime_ns, mtime_ns)

    def test_index_rejects_public_state_directory_and_db_inside_root_without_source_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "archive"
            root.mkdir()
            source, data = self._write_slide(root, "deck.html")
            before = (source.read_bytes(), source.stat().st_mtime_ns)
            manifest = self._manifest(workspace, ["deck.html"])

            public_state = workspace / "public-state"
            public_state.mkdir(mode=0o755)
            public_db = public_state / "index.sqlite3"
            public_result = self._run_cli(
                "index",
                "--root",
                str(root),
                "--db",
                str(public_db),
                "--manifest",
                str(manifest),
            )
            self.assertEqual(public_result.returncode, 1)
            self.assertNotIn("Traceback", public_result.stderr)
            self.assertFalse(public_db.exists())

            inside_db = root / "index.sqlite3"
            inside_result = self._run_cli(
                "index",
                "--root",
                str(root),
                "--db",
                str(inside_db),
                "--manifest",
                str(manifest),
            )
            self.assertEqual(inside_result.returncode, 1)
            self.assertNotIn("Traceback", inside_result.stderr)
            self.assertFalse(inside_db.exists())
            self.assertEqual(source.read_bytes(), data)
            self.assertEqual((source.read_bytes(), source.stat().st_mtime_ns), before)


if __name__ == "__main__":
    unittest.main()
