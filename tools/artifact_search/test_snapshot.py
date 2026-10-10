"""Acceptance tests for the local immutable artifact snapshot contract.

The snapshot module is imported in ``setUp`` so this file stays a real red
test while the contract implementation is being developed.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import stat
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch


class SnapshotContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tempdir.name)
        self.root = self.workspace / "archive"
        self.store = self.workspace / "snapshot-store"
        self.root.mkdir()
        self.store.mkdir(mode=0o700)
        self.source_id = str(uuid.UUID("11111111-1111-4111-8111-111111111111"))

        # Keep the import lazy: before the implementation exists, every test
        # is reported as a normal failing test rather than a collection error.
        from tools.artifact_search import snapshot

        self.snapshot = snapshot
        self.SnapshotError = snapshot.SnapshotError
        self.create_snapshot = snapshot.create_snapshot
        self.load_manifest = snapshot.load_manifest
        self.LocalSnapshotSource = snapshot.LocalSnapshotSource
        self.acquire_snapshot = snapshot.acquire_snapshot

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _write(self, relative: str, contents: bytes | str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(contents, str):
            contents = contents.encode("utf-8")
        path.write_bytes(contents)
        return path

    def _manifest_path(self, result: dict) -> Path:
        return Path(result["manifest_path"])

    def _manifest(self, result: dict) -> tuple[dict, bytes]:
        path = self._manifest_path(result)
        data = path.read_bytes()
        return json.loads(data.decode("utf-8")), data

    def _assert_manifest_contract(
        self,
        result: dict,
        *,
        source_root: Path | None = None,
        expected_store: Path | None = None,
    ) -> dict:
        manifest, raw = self._manifest(result)
        self.assertEqual(
            set(manifest),
            {"schema", "complete", "source_id", "policy", "artifacts", "excluded"},
        )
        self.assertEqual(manifest["schema"], "mimir-artifact-snapshot-v1")
        self.assertIs(manifest["complete"], True)
        self.assertEqual(manifest["source_id"], self.source_id)
        self.assertIsInstance(manifest["policy"], dict)
        self.assertIsInstance(manifest["artifacts"], list)
        self.assertIsInstance(manifest["excluded"], list)

        manifest_path = self._manifest_path(result)
        self.assertEqual(manifest_path.name, "manifest.json")
        manifest_hash = hashlib.sha256(raw).hexdigest()
        self.assertEqual(manifest_path.parent.name, manifest_hash)
        self.assertEqual(result["snapshot_id"], manifest_hash)
        store = expected_store or self.store
        self.assertEqual(manifest_path.parent.parent, store)
        self.assertEqual(stat.S_IMODE(store.stat().st_mode), 0o700)
        self.assertEqual(raw.decode("utf-8").encode("utf-8"), raw)

        paths: list[str] = []
        artifact_ids: list[str] = []
        for artifact in manifest["artifacts"]:
            self.assertEqual(
                set(artifact),
                {"artifact_id", "path", "size", "mtime_ns", "sha256", "object"},
            )
            self.assertIsInstance(artifact["artifact_id"], str)
            self.assertIsInstance(artifact["path"], str)
            self.assertIsInstance(artifact["size"], int)
            self.assertGreaterEqual(artifact["size"], 0)
            self.assertIsInstance(artifact["mtime_ns"], int)
            self.assertGreaterEqual(artifact["mtime_ns"], 0)
            self.assertRegex(artifact["sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(artifact["object"], "objects/" + artifact["sha256"])
            relative = Path(artifact["path"])
            self.assertFalse(relative.is_absolute())
            self.assertNotIn("..", relative.parts)
            paths.append(artifact["path"])
            artifact_ids.append(artifact["artifact_id"])

            if source_root is not None:
                original = source_root / relative
                self.assertTrue(original.is_file())
                contents = original.read_bytes()
                self.assertEqual(artifact["size"], len(contents))
                self.assertEqual(artifact["sha256"], hashlib.sha256(contents).hexdigest())
                object_path = manifest_path.parent / artifact["object"]
                self.assertTrue(object_path.is_file())
                self.assertEqual(object_path.read_bytes(), contents)

        self.assertEqual(paths, sorted(paths))
        self.assertEqual(len(paths), len(set(paths)))
        self.assertEqual(len(artifact_ids), len(set(artifact_ids)))
        for excluded in manifest["excluded"]:
            self.assertEqual(set(excluded), {"path", "reason"})
            self.assertIsInstance(excluded["path"], str)
            self.assertIsInstance(excluded["reason"], str)
        excluded_paths = [item["path"] for item in manifest["excluded"]]
        self.assertEqual(excluded_paths, sorted(excluded_paths))
        return manifest

    def _assert_failed_without_publication(self, root: Path, store: Path) -> None:
        with self.assertRaises((ValueError, self.SnapshotError)):
            self.create_snapshot(root, store, self.source_id)
        if store.exists():
            self.assertEqual(list(store.rglob("manifest.json")), [])

    @staticmethod
    def _delta_text(result: dict) -> str:
        return json.dumps(result["delta"], sort_keys=True, ensure_ascii=False)

    @staticmethod
    def _delta_is_empty(value: object) -> bool:
        if isinstance(value, dict):
            return all(SnapshotContractTests._delta_is_empty(item) for item in value.values())
        if isinstance(value, (list, tuple, set)):
            return all(SnapshotContractTests._delta_is_empty(item) for item in value)
        return value in (None, False, "", 0)

    def test_repeat_is_deterministic_and_publishes_originals_and_content_objects(self) -> None:
        self._write("docs/readme.md", "alpha ä\n")
        self._write("docs/page.html", "<p>beta</p>")
        self._write("images/picture.png", b"synthetic-png")
        self._write("images/photo.jpg", b"synthetic-jpg")
        self._write("images/photo.jpeg", b"synthetic-jpeg")
        self._write("images/animated.gif", b"synthetic-gif")
        self._write("images/modern.webp", b"synthetic-webp")
        self._write("images/vector.svg", "<svg></svg>")
        self._write(".hidden.md", "hidden")
        self._write("credentials/api.txt", "synthetic credential path")
        self._write("unsupported/archive.zip", b"synthetic archive")
        self._write("secret-note.md", 'AWS_ACCESS_KEY_ID="AKIA0000000000000000"')

        first = self.create_snapshot(self.root, self.store, self.source_id)
        first_manifest = self._assert_manifest_contract(first, source_root=self.root)
        first_raw = self._manifest(first)[1]

        second = self.create_snapshot(
            self.root, self.store, self.source_id, previous=Path(first["manifest_path"])
        )
        second_manifest = self._assert_manifest_contract(second, source_root=self.root)
        self.assertEqual(self._manifest(second)[1], first_raw)
        self.assertEqual(second["snapshot_id"], first["snapshot_id"])
        self.assertEqual(second_manifest["artifacts"], first_manifest["artifacts"])
        self.assertIn("delta", second)
        self.assertTrue(self._delta_is_empty(second["delta"]))

        exclusions = {item["path"]: item["reason"].lower() for item in first_manifest["excluded"]}
        for path in (".hidden.md", "credentials", "unsupported/archive.zip", "secret-note.md"):
            self.assertIn(path, exclusions)
        self.assertTrue(exclusions[".hidden.md"])
        self.assertTrue(exclusions["credentials"])
        self.assertTrue(exclusions["unsupported/archive.zip"])
        self.assertTrue(exclusions["secret-note.md"])

    def test_delta_covers_added_changed_renamed_and_removed_artifacts(self) -> None:
        self._write("same.txt", "same")
        self._write("changed.txt", "before")
        self._write("before.md", "rename me")
        self._write("removed.pdf", b"remove me")
        first = self.create_snapshot(self.root, self.store, self.source_id)
        old_manifest = self._assert_manifest_contract(first, source_root=self.root)

        self._write("changed.txt", "after")
        (self.root / "before.md").rename(self.root / "after.md")
        (self.root / "removed.pdf").unlink()
        self._write("added.html", "new")
        second = self.create_snapshot(
            self.root, self.store, self.source_id, previous=Path(first["manifest_path"])
        )
        new_manifest = self._assert_manifest_contract(second, source_root=self.root)
        new_paths = {item["path"] for item in new_manifest["artifacts"]}
        self.assertEqual(new_paths, {"same.txt", "changed.txt", "after.md", "added.html"})
        old_by_path = {item["path"]: item for item in old_manifest["artifacts"]}
        new_by_path = {item["path"]: item for item in new_manifest["artifacts"]}
        self.assertNotEqual(old_by_path["changed.txt"]["sha256"], new_by_path["changed.txt"]["sha256"])
        self.assertNotEqual(old_by_path["before.md"]["path"], new_by_path["after.md"]["path"])
        delta_text = self._delta_text(second)
        for path in ("added.html", "changed.txt", "before.md", "after.md", "removed.pdf"):
            self.assertIn(path, delta_text)

    def test_empty_source_requires_explicit_allow_empty(self) -> None:
        with self.assertRaises((ValueError, self.SnapshotError)):
            self.create_snapshot(self.root, self.store, self.source_id)
        result = self.create_snapshot(self.root, self.store, self.source_id, allow_empty=True)
        manifest = self._assert_manifest_contract(result, source_root=self.root)
        self.assertEqual(manifest["artifacts"], [])

    def test_missing_inaccessible_symlink_oversized_and_store_inside_source_fail_closed(self) -> None:
        self._assert_failed_without_publication(self.workspace / "missing", self.store / "missing")

        self._write("inaccessible.txt", "not read")
        original_read = self.snapshot._read
        read_failures: list[str] = []

        def inaccessible_read(root: Path, relative: str, *args, **kwargs):
            if Path(root).resolve() == self.root.resolve() and str(relative) == "inaccessible.txt":
                read_failures.append(relative)
                raise self.SnapshotError("synthetic inaccessible source")
            return original_read(root, relative, *args, **kwargs)

        with patch.object(self.snapshot, "_read", inaccessible_read):
            self._assert_failed_without_publication(self.root, self.store / "inaccessible")
        self.assertEqual(read_failures, ["inaccessible.txt"])

        real_root = self.workspace / "real-source"
        real_root.mkdir()
        (real_root / "one.txt").write_text("one", encoding="utf-8")
        symlink_root = self.workspace / "symlink-source"
        symlink_root.symlink_to(real_root, target_is_directory=True)
        self._assert_failed_without_publication(symlink_root, self.store / "symlink")

        oversized_root = self.workspace / "oversized"
        oversized_root.mkdir()
        oversized = oversized_root / "large.txt"
        oversized.write_bytes(b"x" * (20 * 1024 * 1024 + 1))
        self._assert_failed_without_publication(oversized_root, self.store / "oversized")

        inside_store = self.root / "store"
        inside_store.mkdir()
        self._write("inside.txt", "source")
        self._assert_failed_without_publication(self.root, inside_store)

    def test_existing_corrupt_snapshot_object_is_never_overwritten(self) -> None:
        self._write("one.txt", "one")
        first = self.create_snapshot(self.root, self.store, self.source_id)
        manifest = self._assert_manifest_contract(first, source_root=self.root)
        object_path = self._manifest_path(first).parent / manifest["artifacts"][0]["object"]
        object_path.write_bytes(b"synthetic corruption")
        with self.assertRaises((ValueError, self.SnapshotError)):
            self.create_snapshot(
                self.root, self.store, self.source_id, previous=Path(first["manifest_path"])
            )
        self.assertEqual(object_path.read_bytes(), b"synthetic corruption")
        self.assertTrue(self._manifest_path(first).exists())

    def _valid_manifest(self) -> tuple[dict, bytes, str]:
        self._write("valid.txt", "valid")
        result = self.create_snapshot(self.root, self.store, self.source_id)
        manifest, raw = self._manifest(result)
        return manifest, raw, hashlib.sha256(raw).hexdigest()

    def _assert_load_rejects(self, data: bytes, *, expected_hash: str | None = None, source_id: str | None = None) -> None:
        expected_hash = expected_hash or hashlib.sha256(data).hexdigest()
        with self.assertRaises((ValueError, self.SnapshotError)):
            self.load_manifest(data, expected_hash, source_id or self.source_id)

    def test_load_manifest_rejects_truncated_incomplete_malformed_and_wrong_hash_inputs(self) -> None:
        manifest, raw, expected_hash = self._valid_manifest()
        self._assert_load_rejects(raw[:-1], expected_hash=expected_hash)
        self._assert_load_rejects(b"{not-json", expected_hash="0" * 64)

        incomplete = copy.deepcopy(manifest)
        incomplete["complete"] = False
        incomplete_raw = json.dumps(incomplete, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self._assert_load_rejects(incomplete_raw)

        wrong_schema = copy.deepcopy(manifest)
        wrong_schema["schema"] = "other-v1"
        wrong_schema_raw = json.dumps(wrong_schema, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self._assert_load_rejects(wrong_schema_raw)
        self._assert_load_rejects(raw, expected_hash="g" * 64)
        self._assert_load_rejects(raw, expected_hash="0" * 63)
        self._assert_load_rejects(raw, source_id=str(uuid.UUID("22222222-2222-4222-8222-222222222222")))

    def test_load_manifest_rejects_unknown_fields_bad_bounds_duplicate_ids_and_traversal(self) -> None:
        manifest, _raw, _expected_hash = self._valid_manifest()

        cases: list[dict] = []
        unknown = copy.deepcopy(manifest)
        unknown["unexpected"] = True
        cases.append(unknown)
        bad_size = copy.deepcopy(manifest)
        bad_size["artifacts"][0]["size"] = -1
        cases.append(bad_size)
        bad_mtime = copy.deepcopy(manifest)
        bad_mtime["artifacts"][0]["mtime_ns"] = -1
        cases.append(bad_mtime)
        bad_hash = copy.deepcopy(manifest)
        bad_hash["artifacts"][0]["sha256"] = "x" * 64
        cases.append(bad_hash)
        bad_object = copy.deepcopy(manifest)
        bad_object["artifacts"][0]["object"] = "objects/not-the-content-hash"
        cases.append(bad_object)
        duplicate_id = copy.deepcopy(manifest)
        duplicate_id["artifacts"].append(copy.deepcopy(duplicate_id["artifacts"][0]))
        cases.append(duplicate_id)
        duplicate_path = copy.deepcopy(manifest)
        duplicate_path["artifacts"].append(copy.deepcopy(duplicate_path["artifacts"][0]))
        duplicate_path["artifacts"][1]["artifact_id"] = "different-id"
        cases.append(duplicate_path)
        for path in ("../escape.txt", "/absolute.txt", "nested/../../escape.txt"):
            traversal = copy.deepcopy(manifest)
            traversal["artifacts"][0]["path"] = path
            cases.append(traversal)
        bad_excluded = copy.deepcopy(manifest)
        bad_excluded["excluded"].append({"path": "../escape", "reason": "policy"})
        cases.append(bad_excluded)

        for candidate in cases:
            with self.subTest(candidate=candidate):
                data = (json.dumps(candidate, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
                self._assert_load_rejects(data)

    def test_acquisition_never_writes_inside_source_bundle(self):
        self._write("one.txt", "one")
        result = self.create_snapshot(self.root, self.store, self.source_id)
        bundle = self._manifest_path(result).parent
        source = self.LocalSnapshotSource(bundle, result['snapshot_id'], self.source_id)
        with self.assertRaises(self.SnapshotError):
            self.acquire_snapshot(source, bundle / 'derived')
        self.assertFalse((bundle / 'derived').exists())

    def _copy_snapshot_tree(self, result: dict, name: str) -> Path:
        destination = self.workspace / name
        shutil.copytree(self._manifest_path(result).parent, destination)
        return destination

    def test_local_acquisition_validates_all_object_bytes_before_publication(self) -> None:
        self._write("one.txt", "one")
        self._write("two.md", "two")
        source_result = self.create_snapshot(self.root, self.store, self.source_id)
        source_manifest = self._assert_manifest_contract(source_result, source_root=self.root)
        source_dir = self._copy_snapshot_tree(source_result, "published-source")
        source = self.LocalSnapshotSource(source_dir, source_result["snapshot_id"], self.source_id)

        acquired_store = self.workspace / "acquired"
        acquired = self.acquire_snapshot(source, acquired_store)
        self.assertEqual(acquired["snapshot_id"], source_result["snapshot_id"])
        acquired_manifest = self._assert_manifest_contract(acquired, expected_store=acquired_store)
        self.assertEqual(acquired_manifest, source_manifest)
        for artifact in source_manifest["artifacts"]:
            self.assertEqual(
                (self._manifest_path(acquired).parent / artifact["object"]).read_bytes(),
                (source_dir / artifact["object"]).read_bytes(),
            )

        corrupt_source_dir = self._copy_snapshot_tree(source_result, "corrupt-source")
        corrupt_artifact = source_manifest["artifacts"][1]
        (corrupt_source_dir / corrupt_artifact["object"]).write_bytes(b"wrong source bytes")
        corrupt_source = self.LocalSnapshotSource(
            corrupt_source_dir, source_result["snapshot_id"], self.source_id
        )
        failed_store = self.workspace / "failed-acquisition"
        with self.assertRaises((ValueError, self.SnapshotError)):
            self.acquire_snapshot(corrupt_source, failed_store)
        self.assertEqual(list(failed_store.rglob("manifest.json")) if failed_store.exists() else [], [])
        self.assertEqual(list(failed_store.glob("current*")) if failed_store.exists() else [], [])

    def test_local_acquisition_does_not_overwrite_corrupt_target_object(self) -> None:
        self._write("one.txt", "one")
        source_result = self.create_snapshot(self.root, self.store, self.source_id)
        source_manifest = self._assert_manifest_contract(source_result, source_root=self.root)
        source_dir = self._copy_snapshot_tree(source_result, "source-for-target")
        source = self.LocalSnapshotSource(source_dir, source_result["snapshot_id"], self.source_id)
        target_store = self.workspace / "target-store"
        target_store.mkdir(mode=0o700)
        target_object = (
            target_store
            / source_result["snapshot_id"]
            / source_manifest["artifacts"][0]["object"]
        )
        target_object.parent.mkdir(parents=True)
        target_object.write_bytes(b"pre-existing corrupt target")

        with self.assertRaises((ValueError, self.SnapshotError)):
            self.acquire_snapshot(source, target_store)
        self.assertEqual(target_object.read_bytes(), b"pre-existing corrupt target")
        self.assertEqual(list(target_store.rglob("manifest.json")), [])

    def test_local_acquisition_rejects_truncated_incomplete_and_malformed_manifests(self) -> None:
        self._write("one.txt", "one")
        result = self.create_snapshot(self.root, self.store, self.source_id)
        source_dir = self._copy_snapshot_tree(result, "manifest-source")
        manifest_path = source_dir / "manifest.json"
        original = manifest_path.read_bytes()
        for label, data in (
            ("truncated", original[:-1]),
            ("malformed", b"{not-json"),
        ):
            with self.subTest(label=label):
                manifest_path.write_bytes(data)
                destination = self.workspace / ("destination-" + label)
                with self.assertRaises((ValueError, self.SnapshotError)):
                    source = self.LocalSnapshotSource(source_dir, result["snapshot_id"], self.source_id)
                    self.acquire_snapshot(source, destination)
                self.assertEqual(list(destination.rglob("manifest.json")) if destination.exists() else [], [])
                manifest_path.write_bytes(original)

        incomplete = json.loads(original.decode("utf-8"))
        incomplete["complete"] = False
        manifest_path.write_text(json.dumps(incomplete), encoding="utf-8")
        destination = self.workspace / "destination-incomplete"
        with self.assertRaises((ValueError, self.SnapshotError)):
            source = self.LocalSnapshotSource(source_dir, result["snapshot_id"], self.source_id)
            self.acquire_snapshot(source, destination)
        self.assertEqual(list(destination.rglob("manifest.json")) if destination.exists() else [], [])

    def test_source_interruption_never_publishes_a_manifest(self) -> None:
        self._write("first.txt", "first")
        self._write("interrupt.txt", "interrupt")
        original_read = self.snapshot._read
        read_failures: list[str] = []

        def interrupt_read(root: Path, relative: str, *args, **kwargs):
            if Path(root).resolve() == self.root.resolve() and str(relative) == "interrupt.txt":
                read_failures.append(relative)
                raise self.SnapshotError("synthetic source interruption")
            return original_read(root, relative, *args, **kwargs)

        with patch.object(self.snapshot, "_read", interrupt_read):
            with self.assertRaises((ValueError, self.SnapshotError, OSError)):
                self.create_snapshot(self.root, self.store, self.source_id)
        self.assertEqual(read_failures, ["interrupt.txt"])
        self.assertEqual(list(self.store.rglob("manifest.json")), [])
        self.assertEqual(list(self.store.glob("current*")), [])


if __name__ == "__main__":
    unittest.main()
