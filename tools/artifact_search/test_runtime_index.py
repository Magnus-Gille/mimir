from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.artifact_search import runtime_index
from tools.artifact_search.extract import Unit


class SnapshotIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tempdir.name)
        self.db = self.workspace / "private" / "index.sqlite3"
        self.snapshot_id = "a" * 64
        self.source_id = "source-test"
        self.index = runtime_index.SnapshotIndex(self.db, self.snapshot_id, self.source_id, create=True)

    def tearDown(self) -> None:
        try:
            self.index.close()
        finally:
            self.tempdir.cleanup()

    @staticmethod
    def item(artifact_id: str, path: str, data: bytes) -> dict:
        return {
            "artifact_id": artifact_id,
            "path": path,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "mtime_ns": 1,
            "object": "objects/" + hashlib.sha256(data).hexdigest(),
        }

    def manifest(self, artifacts: list[dict], excluded: list[dict] | None = None) -> dict:
        return {
            "schema": "mimir-artifact-snapshot-v1",
            "complete": True,
            "source_id": self.source_id,
            "policy": {},
            "artifacts": artifacts,
            "excluded": excluded or [],
        }

    def test_restart_search_and_stable_locator(self) -> None:
        data = b"# Heading\n\nThe stable retrieval phrase is here."
        item = self.item("artifact-1", "docs/readme.md", data)
        self.index.put(item, data)
        self.assertEqual(self.index.stats(), {"file_count": 1, "unit_count": 1, "excluded_count": 0, "complete": False})
        self.index.close()
        self.index = runtime_index.SnapshotIndex(self.db, self.snapshot_id, self.source_id)
        result = self.index.search("stable retrieval phrase", top=1)
        self.assertEqual(result[0]["path"], "docs/readme.md")
        self.assertEqual(result[0]["artifact_id"], "artifact-1")
        self.assertEqual(result[0]["locator"], {"kind": "section", "number": 1, "anchor": None})
        self.assertEqual(set(self.index.completed()), {"artifact-1"})

    def test_put_is_atomic_when_extraction_fails(self) -> None:
        good = b"kept"
        first = self.item("artifact-1", "one.txt", good)
        self.index.put(first, good)
        broken = self.item("artifact-2", "broken.pdf", b"not-a-pdf")
        with self.assertRaisesRegex(ValueError, "extraction_failed"):
            self.index.put(broken, b"not-a-pdf")
        self.assertEqual(self.index.completed(), {"artifact-1"})
        self.assertEqual(self.index.stats()["file_count"], 1)

    def test_pins_and_corruption_are_rejected(self) -> None:
        self.index.close()
        with self.assertRaisesRegex(ValueError, "identity_mismatch"):
            runtime_index.SnapshotIndex(self.db, "b" * 64, self.source_id)
        self.db.write_bytes(b"not sqlite")
        self.db.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "corrupt"):
            runtime_index.SnapshotIndex(self.db, self.snapshot_id, self.source_id)

    def test_raw_and_extracted_secret_filters_are_exclusions(self) -> None:
        raw = b'password="this-is-a-secret-value"'
        raw_item = self.item("secret-raw", "secret.txt", raw)
        self.index.put(raw_item, raw)

        clean = b"safe bytes"
        extracted_item = self.item("secret-extracted", "safe.txt", clean)
        with mock.patch.object(runtime_index, "extract", return_value=[Unit("section", 1, None, "", 'token="hidden-secret-value"')]):
            self.index.put(extracted_item, clean)
        self.assertEqual(self.index.stats()["excluded_count"], 2)
        self.assertEqual(self.index.completed(), {"secret-raw", "secret-extracted"})
        self.assertEqual(self.index.search("hidden-secret-value"), [])

    def test_images_and_empty_text_are_exclusions(self) -> None:
        image = b"\x89PNG\r\n"
        image_item = self.item("image-1", "images/photo.png", image)
        self.index.put(image_item, image)
        empty = self.item("empty-1", "empty.txt", b" \n")
        self.index.put(empty, b" \n")
        self.assertEqual(self.index.stats()["excluded_count"], 2)
        self.assertEqual(self.index.completed(), {"image-1", "empty-1"})

    def test_finish_requires_exact_artifact_and_exclusion_set(self) -> None:
        data = b"indexed content"
        artifact = self.item("artifact-1", "one.txt", data)
        self.index.put(artifact, data)
        manifest = self.manifest([artifact], [{"path": "old.docx", "reason": "unsupported_format"}])
        with self.assertRaisesRegex(ValueError, "manifest_mismatch"):
            self.index.finish(self.manifest([]))
        summary = self.index.finish(manifest)
        self.assertTrue(summary["complete"])
        self.assertEqual(summary["excluded_count"], 1)
        self.assertEqual(self.index.finish(manifest), summary)
        with self.assertRaisesRegex(ValueError, "index_complete"):
            self.index.put(artifact, data)

    def test_finish_accepts_indexed_image_as_artifact_exclusion(self) -> None:
        image = b"image"
        artifact = self.item("image-1", "image.png", image)
        self.index.put(artifact, image)
        summary = self.index.finish(self.manifest([artifact]))
        self.assertEqual(summary["file_count"], 0)
        self.assertEqual(summary["excluded_count"], 1)
        self.assertEqual(self.index.completed(), {"image-1"})

    def test_search_limits_and_fts_injection(self) -> None:
        data = b"alpha beta"
        item = self.item("artifact-1", "a.txt", data)
        self.index.put(item, data)
        self.assertEqual(self.index.search("' OR 1=1 --"), [])
        with self.assertRaisesRegex(ValueError, "invalid_top"):
            self.index.search("alpha", top=21)
        with self.assertRaisesRegex(ValueError, "invalid_query"):
            self.index.search("x" * 4097)
        # The database remains usable after hostile FTS syntax.
        self.assertEqual(self.index.search("alpha")[0]["path"], "a.txt")


if __name__ == "__main__":
    unittest.main()
