"""CLI acceptance tests for the immutable artifact snapshot contract."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SOURCE_ID = "11111111-1111-4111-8111-111111111111"


class SnapshotCliContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tempdir.name)
        self.root = self.workspace / "archive"
        self.root.mkdir()
        self.published = self.workspace / "published"
        self.published.mkdir(mode=0o700)
        self.repo_root = Path(__file__).resolve().parents[2]

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _write(self, relative: str, contents: bytes | str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(contents, str):
            contents = contents.encode("utf-8")
        path.write_bytes(contents)
        return path

    def _run(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        child_env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")
                     if key in os.environ}
        child_env["PYTHONPATH"] = str(self.repo_root)
        if env is not None:
            child_env.update(env)
        return subprocess.run(
            [sys.executable, "-m", "tools.artifact_search.snapshot_cli", *args],
            cwd=self.repo_root,
            env=child_env,
            text=True,
            capture_output=True,
            check=False,
        )

    def _success_json(self, completed: subprocess.CompletedProcess[str]) -> dict:
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        payload = json.loads(completed.stdout)
        self.assertIsInstance(payload, dict)
        return payload

    def _diagnostic(self, completed: subprocess.CompletedProcess[str]) -> dict:
        self.assertEqual(completed.returncode, 1, completed.stdout)
        self.assertEqual(completed.stdout, "")
        diagnostic = json.loads(completed.stderr)
        self.assertIsInstance(diagnostic, dict)
        self.assertIn("error", diagnostic)
        self.assertTrue(set(diagnostic) <= {"error", "artifact"})
        return diagnostic

    def _assert_unaccepted(self, store: Path) -> None:
        manifests = list(store.rglob("manifest.json")) if store.exists() else []
        markers = list(store.glob("current*")) if store.exists() else []
        self.assertEqual(manifests, [])
        self.assertEqual(markers, [])

    def test_create_then_acquire_local_preserves_exact_snapshot_and_every_object(self) -> None:
        self._write("docs/alpha.txt", "alpha\n")
        self._write("docs/beta.md", "beta ä\n")
        self._write("slides/deck.html", "<h1>synthetic</h1>")

        created = self._success_json(
            self._run(
                "create",
                "--root",
                str(self.root),
                "--store",
                str(self.published),
                "--source-id",
                SOURCE_ID,
            )
        )
        snapshot_id = created["snapshot_id"]
        bundle = Path(created["manifest_path"]).parent
        manifest_bytes = (bundle / "manifest.json").read_bytes()
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        self.assertEqual(bundle.name, snapshot_id)
        self.assertEqual(created["artifacts"], len(manifest["artifacts"]))
        self.assertGreater(created["artifacts"], 0)

        acquired_store = self.workspace / "acquired"
        acquired = self._success_json(
            self._run(
                "acquire-local",
                "--bundle",
                str(bundle),
                "--store",
                str(acquired_store),
                "--source-id",
                SOURCE_ID,
                "--snapshot-id",
                snapshot_id,
            )
        )
        self.assertEqual(acquired["snapshot_id"], snapshot_id)
        self.assertEqual(acquired["artifacts"], len(manifest["artifacts"]))
        acquired_bundle = acquired_store / snapshot_id
        self.assertEqual((acquired_bundle / "manifest.json").read_bytes(), manifest_bytes)
        for artifact in manifest["artifacts"]:
            self.assertEqual(
                (acquired_bundle / artifact["object"]).read_bytes(),
                (bundle / artifact["object"]).read_bytes(),
            )

    def test_diff_after_change_exposes_stable_artifact_identity(self) -> None:
        changed = self._write("changed.txt", "before\n")
        self._write("unchanged.md", "stable\n")
        first = self._success_json(
            self._run(
                "create",
                "--root",
                str(self.root),
                "--store",
                str(self.published),
                "--source-id",
                SOURCE_ID,
            )
        )
        before_manifest = Path(first["manifest_path"])
        before_data = json.loads(before_manifest.read_text(encoding="utf-8"))
        before_item = next(item for item in before_data["artifacts"] if item["path"] == "changed.txt")

        changed.write_text("after\n", encoding="utf-8")
        second = self._success_json(
            self._run(
                "create",
                "--root",
                str(self.root),
                "--store",
                str(self.published),
                "--source-id",
                SOURCE_ID,
                "--previous",
                str(before_manifest),
            )
        )
        after_manifest = Path(second["manifest_path"])
        diff = self._success_json(
            self._run(
                "diff",
                "--before",
                str(before_manifest),
                "--after",
                str(after_manifest),
                "--source-id",
                SOURCE_ID,
            )
        )
        self.assertIn("changed", diff)
        changed_entries = [
            item for item in diff["changed"] if item["after"]["path"] == "changed.txt"
        ]
        self.assertEqual(len(changed_entries), 1)
        entry = changed_entries[0]
        self.assertEqual(entry["before"]["artifact_id"], before_item["artifact_id"])
        self.assertEqual(entry["after"]["artifact_id"], before_item["artifact_id"])
        self.assertNotEqual(entry["before"]["sha256"], entry["after"]["sha256"])

    def test_invalid_missing_and_corrupt_sources_fail_with_json_and_no_acceptance_marker(self) -> None:
        invalid_store = self.workspace / "invalid-store"
        invalid_store.mkdir(mode=0o700)
        invalid = self._run(
            "create",
            "--root",
            str(self.root),
            "--store",
            str(invalid_store),
            "--source-id",
            "not-a-uuid",
        )
        self._diagnostic(invalid)
        self._assert_unaccepted(invalid_store)

        missing_store = self.workspace / "missing-store"
        missing_store.mkdir(mode=0o700)
        missing = self._run(
            "create",
            "--root",
            str(self.workspace / "does-not-exist"),
            "--store",
            str(missing_store),
            "--source-id",
            SOURCE_ID,
        )
        self._diagnostic(missing)
        self._assert_unaccepted(missing_store)

        self._write("source.txt", "source\n")
        created = self._success_json(
            self._run(
                "create",
                "--root",
                str(self.root),
                "--store",
                str(self.published),
                "--source-id",
                SOURCE_ID,
            )
        )
        bundle = Path(created["manifest_path"]).parent
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        corrupt_bundle = self.workspace / "corrupt-bundle"
        shutil.copytree(bundle, corrupt_bundle)
        object_path = corrupt_bundle / manifest["artifacts"][0]["object"]
        object_path.write_bytes(b"tampered synthetic object")
        corrupt_store = self.workspace / "corrupt-store"
        corrupt = self._run(
            "acquire-local",
            "--bundle",
            str(corrupt_bundle),
            "--store",
            str(corrupt_store),
            "--source-id",
            SOURCE_ID,
            "--snapshot-id",
            created["snapshot_id"],
        )
        diagnostic = self._diagnostic(corrupt)
        self.assertIn("error", diagnostic)
        self._assert_unaccepted(corrupt_store)

    def test_http_acquisition_without_explicit_environment_fails_safely_without_headers_or_body(self) -> None:
        store = self.workspace / "http-store"
        store.mkdir(mode=0o700)
        child_env = {"MIMIR_SNAPSHOT_URL": "", "MIMIR_API_KEY": ""}
        result = self._run(
            "acquire-http",
            "--store",
            str(store),
            "--source-id",
            SOURCE_ID,
            "--snapshot-id",
            "0" * 64,
            env=child_env,
        )
        diagnostic = self._diagnostic(result)
        self.assertEqual(diagnostic["error"], "invalid_source_url")
        output = result.stdout + result.stderr
        for forbidden in ("Authorization", "Bearer", "fixture-bearer", "response body"):
            self.assertNotIn(forbidden, output)
        self._assert_unaccepted(store)


if __name__ == "__main__":
    unittest.main()
