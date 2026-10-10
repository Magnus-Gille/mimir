from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from tools.artifact_search.heldout import (
    HeldoutError,
    run_heldout,
    validate_evaluation,
    validate_split,
)
from tools.artifact_search.snapshot import create_snapshot


SOURCE_ID = "11111111-1111-4111-8111-111111111111"
AUTHOR = "label-author"
REVIEWER = "independent-reviewer"
WHEN = "2026-10-10T00:00:00Z"


def canonical(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def locator(path: str, digest: str, *, kind: str = "page", number: int = 1) -> dict:
    return {"path": path, "kind": kind, "number": number, "sha256": digest}


def case(case_id: str, query: str, case_type: str, expected: list[dict]) -> dict:
    return {
        "id": case_id,
        "query": query,
        "type": case_type,
        "expected": expected,
        "provenance": {"source_task": "synthetic-test", "evidence": "fixture evidence"},
    }


def labels(snapshot_id: str, cases: list[dict], *, status: str = "approved", reviewer: str | None = REVIEWER) -> dict:
    return {
        "schema": "mimir-artifact-evaluation-v1",
        "corpus_snapshot_id": snapshot_id,
        "corpus_source_id": SOURCE_ID,
        "author": AUTHOR,
        "reviewer": reviewer,
        "status": status,
        "created_at": WHEN,
        "frozen_at": WHEN if status == "approved" else None,
        "cases": cases,
    }


class HeldoutTests(unittest.TestCase):
    def _snapshot(self) -> tuple[tempfile.TemporaryDirectory, Path, str, dict[str, str]]:
        workspace = tempfile.TemporaryDirectory()
        root = Path(workspace.name) / "corpus"
        store = Path(workspace.name) / "snapshots"
        root.mkdir()
        (root / "answer.txt").write_text("answer", encoding="utf-8")
        (root / "distractor.txt").write_text("distractor", encoding="utf-8")
        result = create_snapshot(root, store, SOURCE_ID)
        manifest_path = Path(result["manifest_path"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        hashes = {item["path"]: item["sha256"] for item in manifest["artifacts"]}
        return workspace, manifest_path, result["snapshot_id"], hashes

    def _write(self, path: Path, value: dict) -> str:
        data = canonical(value)
        path.write_bytes(data)
        return hashlib.sha256(data).hexdigest()

    def test_draft_validation_and_synthetic_duplicate_distractor_miss(self) -> None:
        workspace, manifest_path, snapshot_id, hashes = self._snapshot()
        self.addCleanup(workspace.cleanup)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        draft = labels(
            snapshot_id,
            [
                case("positive", "where answer", "positive", [locator("answer.txt", hashes["answer.txt"])]),
                case("distractor", "where answer", "distractor", []),
                case("miss", "absent topic", "miss", []),
            ],
            status="draft",
            reviewer=None,
        )
        normalized = validate_evaluation(draft, snapshot_manifest=manifest)
        self.assertEqual([item["id"] for item in normalized["cases"]], ["positive", "distractor", "miss"])

    def test_validation_rejects_duplicate_ids_empty_questions_unknown_types_and_bad_labels(self) -> None:
        workspace, manifest_path, snapshot_id, hashes = self._snapshot()
        self.addCleanup(workspace.cleanup)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        valid = case("same", "question", "miss", [])
        invalid_cases = [
            [valid, dict(valid)],
            [case("empty", "   ", "miss", [])],
            [case("unknown", "question", "other", [])],
            [case("miss-answer", "question", "miss", [locator("answer.txt", hashes["answer.txt"])] )],
            [case("positive-empty", "question", "positive", [])],
            [case("bad-locator", "question", "positive", [locator("answer.txt", "not-a-hash")])],
            [case("bad-kind", "question", "positive", [locator("answer.txt", hashes["answer.txt"], kind="chapter")])],
        ]
        for cases in invalid_cases:
            with self.subTest(cases=cases), self.assertRaises(HeldoutError):
                validate_evaluation(labels(snapshot_id, cases, status="draft", reviewer=None), snapshot_manifest=manifest)

    def test_approval_digest_and_snapshot_mismatches_fail_closed(self) -> None:
        workspace, manifest_path, snapshot_id, hashes = self._snapshot()
        self.addCleanup(workspace.cleanup)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        positive = case("heldout", "where answer", "positive", [locator("answer.txt", hashes["answer.txt"])])
        draft = labels(snapshot_id, [positive], status="draft", reviewer=None)
        with self.assertRaisesRegex(HeldoutError, "approval_required"):
            validate_evaluation(draft, snapshot_manifest=manifest, require_approved=True)
        same_reviewer = labels(snapshot_id, [positive], reviewer=AUTHOR)
        with self.assertRaisesRegex(HeldoutError, "reviewer_not_independent"):
            validate_evaluation(same_reviewer, snapshot_manifest=manifest, require_approved=True)
        wrong_hash = locator("answer.txt", "0" * 64)
        with self.assertRaisesRegex(HeldoutError, "expected_snapshot_mismatch"):
            validate_evaluation(labels(snapshot_id, [case("heldout", "where answer", "positive", [wrong_hash])]), snapshot_manifest=manifest)

    def test_run_report_is_digest_pinned_and_contains_no_query_or_source_text(self) -> None:
        workspace, manifest_path, snapshot_id, hashes = self._snapshot()
        self.addCleanup(workspace.cleanup)
        heldout_path = Path(workspace.name) / "heldout.json"
        calibration_path = Path(workspace.name) / "calibration.json"
        heldout = labels(
            snapshot_id,
            [
                case("positive-case", "private answer question", "positive", [locator("answer.txt", hashes["answer.txt"])]),
                case("distractor-case", "private distractor question", "distractor", []),
                case("miss-case", "private missing question", "miss", []),
            ],
        )
        calibration = labels(snapshot_id, [case("calibration-case", "calibration", "miss", [])], status="draft", reviewer=None)
        heldout_sha = self._write(heldout_path, heldout)
        self._write(calibration_path, calibration)

        def search(_db: Path, query: str, _top: int) -> dict:
            if query == "private answer question":
                return {"results": [{"path": "answer.txt", "locator": {"kind": "page", "number": 1}}], "mode": "lexical"}
            return {"results": [{"path": "distractor.txt", "locator": {"kind": "page", "number": 1}}], "mode": "lexical"} if "distractor" in query else {"results": [], "mode": "lexical"}

        report = run_heldout(
            heldout_path,
            manifest_path,
            Path(workspace.name) / "index.db",
            calibration_path=calibration_path,
            expected_label_sha256=heldout_sha,
            search_fn=search,
            top=1,
        )
        self.assertEqual(report["label_sha256"], heldout_sha)
        self.assertEqual(report["snapshot_id"], snapshot_id)
        self.assertEqual(report["mode"], "lexical")
        self.assertEqual(report["hit_at_k"], 1.0)
        self.assertEqual(report["mean_reciprocal_rank"], 1.0)
        self.assertEqual(report["negative_pass_rate"], 1.0)
        self.assertEqual([item["case_id"] for item in report["results"]], ["positive-case", "distractor-case", "miss-case"])
        rendered = repr(report)
        for secret_text in ("private answer question", "answer.txt", "distractor.txt"):
            self.assertNotIn(secret_text, rendered)
        with self.assertRaisesRegex(HeldoutError, "label_digest_mismatch"):
            run_heldout(
                heldout_path,
                manifest_path,
                Path(workspace.name) / "index.db",
                calibration_path=calibration_path,
                expected_label_sha256="0" * 64,
                search_fn=search,
            )

    def test_split_rejects_same_hash_and_case_overlap(self) -> None:
        workspace, _manifest_path, snapshot_id, _hashes = self._snapshot()
        self.addCleanup(workspace.cleanup)
        first = Path(workspace.name) / "first.json"
        second = Path(workspace.name) / "second.json"
        first_value = labels(snapshot_id, [case("overlap", "q", "miss", [])], status="draft", reviewer=None)
        second_value = labels(snapshot_id, [case("overlap", "q2", "miss", [])], status="draft", reviewer=None)
        self._write(first, first_value)
        self._write(second, second_value)
        with self.assertRaisesRegex(HeldoutError, "calibration_heldout_case_overlap"):
            validate_split(first, second)
        self._write(second, first_value)
        with self.assertRaisesRegex(HeldoutError, "calibration_heldout_same_hash"):
            validate_split(first, second)


if __name__ == "__main__":
    unittest.main()
