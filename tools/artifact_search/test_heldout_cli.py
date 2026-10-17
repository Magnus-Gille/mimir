from __future__ import annotations

from contextlib import redirect_stderr
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.artifact_search import heldout_cli
from tools.artifact_search.embedding_client import EmbedderUnavailable
from tools.artifact_search.heldout import HeldoutError
from tools.artifact_search.snapshot import create_snapshot


SOURCE_ID = "11111111-1111-4111-8111-111111111111"
WHEN = "2026-10-10T00:00:00Z"


def _canonical(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


class HeldoutCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        self.root = self.workspace / "corpus"
        self.root.mkdir()
        (self.root / "answer.txt").write_text("synthetic answer", encoding="utf-8")
        snapshot = create_snapshot(self.root, self.workspace / "snapshots", SOURCE_ID)
        self.snapshot_manifest = Path(snapshot["manifest_path"])
        manifest = json.loads(self.snapshot_manifest.read_text(encoding="utf-8"))
        self.snapshot_id = snapshot["snapshot_id"]
        self.answer_sha = next(item["sha256"] for item in manifest["artifacts"] if item["path"] == "answer.txt")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _case(self, case_id: str, query: str, case_type: str, expected: list[dict]) -> dict:
        return {
            "id": case_id,
            "query": query,
            "type": case_type,
            "expected": expected,
            "provenance": {"source_task": "synthetic", "evidence": "fixture"},
        }

    def _write_labels(self, name: str, cases: list[dict], *, status: str = "approved") -> tuple[Path, str]:
        labels = {
            "schema": "mimir-artifact-evaluation-v1",
            "corpus_snapshot_id": self.snapshot_id,
            "corpus_source_id": SOURCE_ID,
            "author": "author",
            "reviewer": "reviewer" if status == "approved" else None,
            "status": status,
            "created_at": WHEN,
            "frozen_at": WHEN if status == "approved" else None,
            "cases": cases,
        }
        path = self.workspace / name
        data = _canonical(labels)
        path.write_bytes(data)
        return path, hashlib.sha256(data).hexdigest()

    def _calibration(self) -> Path:
        path, _ = self._write_labels(
            "calibration.json",
            [self._case("calibration", "calibration query", "miss", [])],
            status="draft",
        )
        return path

    def _args(self, labels: Path, digest: str, *, mode: str = "lexical") -> list[str]:
        return [
            "--labels", str(labels),
            "--snapshot-manifest", str(self.snapshot_manifest),
            "--calibration", str(self._calibration()),
            "--expected-label-sha256", digest,
            "--db", str(self.workspace / "index.sqlite3"),
            "--mode", mode,
        ]

    def test_draft_and_digest_fail_before_model_or_search(self) -> None:
        labels, digest = self._write_labels(
            "draft.json",
            [self._case("heldout", "private query", "miss", [])],
            status="draft",
        )
        with patch.object(heldout_cli.M5Embedder, "from_env") as construct, patch.object(
            heldout_cli, "semantic_search"
        ) as search:
            with self.assertRaisesRegex(HeldoutError, "approval_required"):
                heldout_cli.run_cli(
                    labels,
                    self.snapshot_manifest,
                    self._calibration(),
                    digest,
                    self.workspace / "index.sqlite3",
                    mode="vector",
                    embedding_revision="revision",
                )
            construct.assert_not_called()
            search.assert_not_called()

        approved, approved_digest = self._write_labels(
            "approved.json",
            [self._case("heldout", "private query", "miss", [])],
        )
        with patch.object(heldout_cli.M5Embedder, "from_env") as construct, patch.object(
            heldout_cli, "semantic_search"
        ) as search:
            with self.assertRaisesRegex(HeldoutError, "label_digest_mismatch"):
                heldout_cli.run_cli(
                    approved,
                    self.snapshot_manifest,
                    self._calibration(),
                    "0" * 64,
                    self.workspace / "index.sqlite3",
                    mode="vector",
                    embedding_revision="revision",
                )
            self.assertNotEqual(approved_digest, "0" * 64)
            construct.assert_not_called()
            search.assert_not_called()

    def test_lexical_report_has_requested_and_effective_mode_metadata(self) -> None:
        labels, digest = self._write_labels(
            "heldout.json",
            [
                self._case(
                    "positive",
                    "private query text",
                    "positive",
                    [{"path": "answer.txt", "kind": "page", "number": 1, "sha256": self.answer_sha}],
                ),
                self._case("miss", "different private query", "miss", []),
            ],
        )

        def search(_db: Path, query: str, _top: int) -> dict:
            if query == "private query text":
                return {"results": [{"path": "answer.txt", "sha256": self.answer_sha, "locator": {"kind": "page", "number": 1}}], "mode": "lexical"}
            return {"results": [], "mode": "lexical"}

        with patch.object(heldout_cli.core, "search", side_effect=search):
            report = heldout_cli.run_cli(
                labels,
                self.snapshot_manifest,
                self._calibration(),
                digest,
                self.workspace / "index.sqlite3",
            )
        self.assertEqual(report["requested_mode"], "lexical")
        self.assertEqual(report["effective_modes"], {"positive": "lexical", "miss": "lexical"})
        self.assertEqual(report["results"][0]["pass"], True)
        rendered = repr(report)
        self.assertNotIn("private query text", rendered)
        self.assertNotIn("answer.txt", rendered)

    def test_hybrid_fallback_is_reported_as_effective_lexical_fallback(self) -> None:
        labels, digest = self._write_labels(
            "hybrid.json",
            [self._case("heldout", "private hybrid query", "miss", [])],
        )
        with patch.object(heldout_cli.M5Embedder, "from_env", return_value=object()), patch.object(
            heldout_cli, "semantic_search", return_value={"results": [], "mode": "lexical_fallback"}
        ):
            report = heldout_cli.run_cli(
                labels,
                self.snapshot_manifest,
                self._calibration(),
                digest,
                self.workspace / "index.sqlite3",
                mode="hybrid",
                embedding_revision="revision",
            )
        self.assertEqual(report["requested_mode"], "hybrid")
        self.assertEqual(report["effective_modes"], {"heldout": "lexical_fallback"})
        self.assertEqual(report["results"][0]["mode"], "lexical_fallback")

    def test_runtime_adapter_evaluates_the_served_path_after_preflight(self) -> None:
        draft, draft_digest = self._write_labels(
            "runtime-draft.json", [self._case("heldout", "private query", "miss", [])], status="draft")
        with patch.object(heldout_cli, "_runtime") as construct:
            with self.assertRaisesRegex(HeldoutError, "approval_required"):
                heldout_cli.run_cli(draft, self.snapshot_manifest, self._calibration(), draft_digest, None,
                                    mode="hybrid", runtime_state=self.workspace / "state",
                                    runtime_handoff=self.workspace / "handoff.json")
            construct.assert_not_called()

        labels, digest = self._write_labels(
            "runtime.json",
            [self._case("positive", "private runtime query", "positive",
                        [{"path": "answer.txt", "kind": "page", "number": 1, "sha256": self.answer_sha}]),
             self._case("miss", "absent runtime query", "miss", [])],
        )
        calls = []
        answer_sha = self.answer_sha

        class FakeRuntime:
            def search(self, query, top, mode):
                calls.append((top, mode))
                if query == "private runtime query":
                    return {"mode": "hybrid", "results": [{"path": "answer.txt", "sha256": answer_sha,
                                                           "locator": {"kind": "page", "number": 1}}]}
                return {"mode": "hybrid", "results": [], "abstained": True}

        with patch.object(heldout_cli, "_runtime", return_value=FakeRuntime()) as construct:
            report = heldout_cli.run_cli(labels, self.snapshot_manifest, self._calibration(), digest, None,
                                         mode="hybrid", runtime_state=self.workspace / "state",
                                         runtime_handoff=self.workspace / "handoff.json")
        construct.assert_called_once_with(self.workspace / "state", self.workspace / "handoff.json")
        self.assertEqual(calls, [(5, "hybrid"), (5, "hybrid")])
        self.assertEqual(report["evaluated_path"], "runtime")
        self.assertEqual(report["effective_modes"], {"positive": "hybrid", "miss": "hybrid"})
        self.assertEqual((report["positive_hit_at_k"], report["miss_pass_rate"]), (1.0, 1.0))

    def test_runtime_adapter_takes_model_settings_only_from_service_config(self) -> None:
        labels, digest = self._write_labels("conflict.json", [self._case("heldout", "q", "miss", [])])
        for options in ({"embedding_revision": "revision"}, {"min_score": 0.5}, {"dimensions": 768},
                        {"top": 6}, {"runtime_handoff": None}):
            arguments = dict(mode="vector", runtime_state=self.workspace / "state",
                             runtime_handoff=self.workspace / "handoff.json")
            arguments.update(options)
            with self.subTest(options=options), self.assertRaises(ValueError):
                heldout_cli.run_cli(labels, self.snapshot_manifest, self._calibration(), digest, None, **arguments)
        with self.assertRaises(ValueError):
            heldout_cli.run_cli(labels, self.snapshot_manifest, self._calibration(), digest,
                                self.workspace / "index.sqlite3", runtime_state=self.workspace / "state",
                                runtime_handoff=self.workspace / "handoff.json")

    def test_invalid_floor_and_embedder_error_are_bounded_json(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid_min_score"):
            heldout_cli._validate_min_score(float("nan"))
        labels, digest = self._write_labels(
            "error.json",
            [self._case("heldout", "private error query", "miss", [])],
        )
        args = self._args(labels, digest, mode="hybrid") + ["--embedding-revision", "revision"]
        with patch.object(heldout_cli.M5Embedder, "from_env", return_value=object()), patch.object(
            heldout_cli,
            "semantic_search",
            side_effect=EmbedderUnavailable("private error query", status=503, retry_after=2),
        ):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                self.assertEqual(heldout_cli.main(args), 1)
        diagnostic = json.loads(stderr.getvalue())
        self.assertEqual(diagnostic["error"], "embedding_unavailable")
        self.assertEqual(diagnostic["http_status"], 503)
        self.assertNotIn("private error query", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
