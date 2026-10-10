"""Deterministic tests for selected-corpus publication and handoff refresh."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.artifact_search import operations
from tools.artifact_search.snapshot import canonical, digest


SOURCE_ID = "44444444-4444-4444-8444-444444444444"
ORIGIN = "https://files.example.test"
PUBLISH_PREFIX = "artifact-snapshots/pilot"
PATHS = ["docs/a.html", "docs/nested/b.md", "docs/nested/c.txt", "slides/deck.html"]
CONTENTS = {
    "docs/a.html": b"<html><body>Selected document alpha</body></html>",
    "docs/nested/b.md": b"# Selected document beta\n",
    "docs/nested/c.txt": b"Selected document gamma\n",
    "slides/deck.html": b"<html><body>Selected slide deck delta</body></html>",
}


class _Response:
    status = 200

    def __init__(self, body):
        self.body = body
        self.headers = {"Content-Encoding": "identity"}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, limit):
        return self.body[:limit]


class _Opener:
    def __init__(self, body):
        self.body = body
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        return _Response(self.body)


class _FakeSource:
    def __init__(self, artifacts, objects):
        self.manifest = {"artifacts": artifacts, "excluded": []}
        self.objects = objects

    def read_object(self, item):
        return self.objects[item["path"]]


class OperationsTestCase(unittest.TestCase):
    def setUp(self):
        self._new_fixture()

    def _new_fixture(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "archive"
        self.root.mkdir(mode=0o700)
        self._write_selected_files()
        self.store = self.root / PUBLISH_PREFIX
        self.store.mkdir(parents=True, mode=0o700)
        self.config = self._config()

    def _write_selected_files(self):
        for relative, data in CONTENTS.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (self.root / "extra-original.txt").write_text("outside the selected set\n")

    def _config(self, **overrides):
        info = self.root.lstat()
        value = {
            "schema": "mimir-search-operations-v1",
            "origin": ORIGIN,
            "source_id": SOURCE_ID,
            "paths": list(PATHS),
            "publish_prefix": PUBLISH_PREFIX,
            "handoff": str(self.store / "handoff.json"),
            "archive_root": str(self.root),
            "root_device": info.st_dev,
            "root_inode": info.st_ino,
            "publish_store": str(self.store),
        }
        value.update(overrides)
        return value

    def _install_prior_publish(self):
        result = operations.publish(self.config)
        handoff = Path(self.config["handoff"])
        return result, handoff, handoff.read_bytes()

    @staticmethod
    def _private_write(path, value):
        path.write_bytes(canonical(value))
        os.chmod(path, 0o600)

    def _refresh_inputs(self, *, observed=None, artifacts=None, objects=None, handoff_overrides=None):
        now = int(time.time())
        observed = now - 30 if observed is None else observed
        value = operations.binding(self.config, "a" * 64, observed)
        # Use a real, canonical digest-bound snapshot id so load_handoff can
        # validate the handoff before operations checks the configured scope.
        if value["snapshot_id"] == "a" * 64:
            value["snapshot_id"] = digest(canonical({"synthetic": "remote handoff"}))
            value["bundle"] = ORIGIN + "/files/" + PUBLISH_PREFIX + "/" + value["snapshot_id"]
        if handoff_overrides:
            value.update(handoff_overrides)
        body = canonical(value)
        artifacts = self._refresh_artifacts() if artifacts is None else artifacts
        objects = self._refresh_objects() if objects is None else objects
        source = _FakeSource(artifacts, objects)
        opener = _Opener(body)
        return value, source, opener

    def _refresh_artifacts(self):
        artifacts = []
        for index, (path, data) in enumerate(CONTENTS.items()):
            sha = digest(data)
            artifacts.append({
                "path": path, "size": len(data), "mtime_ns": index + 1,
                "sha256": sha, "object": "objects/" + sha,
            })
        return artifacts

    def _refresh_objects(self):
        return dict(CONTENTS)

    @contextmanager
    def _mock_refresh(self, opener, source):
        with patch.dict(os.environ, {
            "MIMIR_API_KEY": "synthetic-refresh-bearer",
            "MIMIR_CF_ACCESS_CLIENT_ID": "synthetic-refresh-access-id",
            "MIMIR_CF_ACCESS_CLIENT_SECRET": "synthetic-refresh-access-secret",
        }), patch("tools.artifact_search.operations.build_opener", return_value=opener), \
             patch("tools.artifact_search.operations.source_for", return_value=source):
            yield

    def test_config_rejects_unsafe_prefix_origin_and_unsorted_paths(self):
        path = self.base / "operations.json"
        cases = (
            ("publish_prefix", "../escape"),
            ("publish_prefix", "has space"),
            ("origin", "http://files.example.test"),
            ("paths", [PATHS[1], PATHS[0], *PATHS[2:]]),
        )
        for key, invalid in cases:
            with self.subTest(key=key, invalid=invalid):
                value = self._config(**{key: invalid})
                self._private_write(path, value)
                with self.assertRaises(ValueError):
                    operations.config(path, "publish")

    def test_publish_includes_exact_selected_set_and_nested_html(self):
        result = operations.publish(self.config)
        handoff = json.loads(Path(self.config["handoff"]).read_bytes())
        snapshot_dir = self.store / result["snapshot_id"]
        manifest = json.loads((snapshot_dir / "manifest.json").read_bytes())
        published_paths = [item["path"] for item in manifest["artifacts"]]

        self.assertEqual(published_paths, PATHS)
        self.assertIn("docs/a.html", published_paths)
        self.assertIn("slides/deck.html", published_paths)
        self.assertNotIn("extra-original.txt", published_paths)
        self.assertEqual(manifest["excluded"], [])
        self.assertEqual(handoff["snapshot_id"], result["snapshot_id"])

    def test_unchanged_publish_reuses_snapshot_and_renews_handoff_after_rereads(self):
        first, handoff_path, _ = self._install_prior_publish()
        previous = json.loads(handoff_path.read_bytes())
        read_paths = []
        original_read = operations._read

        def observe(root, relative, *args):
            read_paths.append(relative)
            return original_read(root, relative, *args)

        with patch("tools.artifact_search.operations.time.time", side_effect=[previous["observed_at"] + 10,
                                                                               previous["observed_at"] + 11]), \
             patch("tools.artifact_search.operations._read", side_effect=observe):
            second = operations.publish(self.config)

        updated = json.loads(handoff_path.read_bytes())
        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(updated["snapshot_id"], previous["snapshot_id"])
        self.assertGreater(updated["observed_at"], previous["observed_at"])
        self.assertEqual(read_paths, PATHS + PATHS)

    def test_cumulative_selected_size_limit_preserves_existing_handoff(self):
        _result, handoff_path, before = self._install_prior_publish()
        snapshot_dirs_before = {
            path.name for path in self.store.iterdir()
            if path.is_dir() and not path.name.startswith(".")
        }
        # The first selected object is under this limit; adding the second
        # selected object exceeds it without constructing a large fixture.
        small_limit = len(CONTENTS[PATHS[0]]) + len(CONTENTS[PATHS[1]]) - 1
        with patch("tools.artifact_search.operations.MAX_TOTAL_BYTES", small_limit):
            with self.assertRaisesRegex(ValueError, "selected_corpus_too_large"):
                operations.publish(self.config)

        snapshot_dirs_after = {
            path.name for path in self.store.iterdir()
            if path.is_dir() and not path.name.startswith(".")
        }
        self.assertEqual(handoff_path.read_bytes(), before)
        self.assertEqual(snapshot_dirs_after, snapshot_dirs_before)

    def test_publish_source_failures_preserve_existing_handoff(self):
        failures = ("missing", "symlink", "secret", "changed_during_reread")
        for failure in failures:
            with self.subTest(failure=failure):
                # Each subtest needs a new initial publication and clean input.
                self._new_fixture()
                _result, handoff, before = self._install_prior_publish()
                selected = self.root / PATHS[0]
                if failure == "missing":
                    selected.unlink()
                elif failure == "symlink":
                    selected.unlink()
                    selected.symlink_to(self.base / "outside.txt")
                elif failure == "secret":
                    selected.write_text("api_key='synthetic-test-secret-value'\n")
                else:
                    real_read = operations._read
                    calls = 0

                    def changed(root, relative, *args):
                        nonlocal calls
                        data, info = real_read(root, relative, *args)
                        if relative == PATHS[0]:
                            calls += 1
                            if calls == 2:
                                return b"changed between observations", info
                        return data, info

                    with patch("tools.artifact_search.operations._read", side_effect=changed):
                        with self.assertRaises(ValueError):
                            operations.publish(self.config)
                    self.assertEqual(handoff.read_bytes(), before)
                    continue

                with self.assertRaises((ValueError, OSError)):
                    operations.publish(self.config)
                self.assertEqual(handoff.read_bytes(), before)

    def test_publish_rejects_replaced_source_root(self):
        _result, handoff, before = self._install_prior_publish()
        old_root = self.base / "old-archive"
        self.root.rename(old_root)
        self.root.mkdir(mode=0o700)
        with self.assertRaisesRegex(ValueError, "source_root_replaced"):
            operations.publish(self.config)
        self.assertEqual((old_root / PUBLISH_PREFIX / "handoff.json").read_bytes(), before)

    def test_unchanged_snapshot_can_renew_near_storage_limit_but_new_snapshot_cannot(self):
        first, handoff, before = self._install_prior_publish()
        stored = sum(p.lstat().st_size for p in self.store.rglob('*') if p.is_file())
        with patch('tools.artifact_search.operations.STORE_LIMIT', stored + 10):
            second = operations.publish(self.config)
            self.assertEqual(first['snapshot_id'], second['snapshot_id'])
            valid = handoff.read_bytes()
            (self.root / PATHS[0]).write_text('changed source bytes')
            with self.assertRaisesRegex(ValueError, 'publisher_store_limit'):
                operations.publish(self.config)
            self.assertEqual(handoff.read_bytes(), valid)

    def test_refresh_rejects_invalid_remote_observations_without_overwriting_target(self):
        cases = ("wrong_scope", "backwards", "expired", "extra_artifact", "corrupt_object")
        for failure in cases:
            with self.subTest(failure=failure):
                target = Path(self.config["handoff"])
                now = int(time.time())
                previous = operations.binding(self.config, "d" * 64, now - 20)
                # Make the existing target a current, scope-correct handoff.
                previous["snapshot_id"] = digest(canonical({"prior": "synthetic"}))
                previous["bundle"] = ORIGIN + "/files/" + PUBLISH_PREFIX + "/" + previous["snapshot_id"]
                self._private_write(target, previous)
                before = target.read_bytes()

                overrides = {}
                observed = now - 5
                artifacts = self._refresh_artifacts()
                objects = self._refresh_objects()
                if failure == "wrong_scope":
                    pin = digest(canonical({"synthetic": "remote handoff"}))
                    overrides["bundle"] = ORIGIN + "/files/other-prefix/" + pin
                elif failure == "backwards":
                    observed = now - 40
                elif failure == "expired":
                    observed = now - operations.LEASE_SECONDS
                elif failure == "extra_artifact":
                    extra = b"unselected synthetic extra"
                    sha = digest(extra)
                    artifacts = [*artifacts, {
                        "path": "docs/unselected.txt", "size": len(extra), "mtime_ns": 999,
                        "sha256": sha, "object": "objects/" + sha,
                    }]
                    artifacts.sort(key=lambda item: item["path"])
                    objects["docs/unselected.txt"] = extra
                elif failure == "corrupt_object":
                    objects[PATHS[0]] = b"tampered synthetic bytes"

                _remote, source, opener = self._refresh_inputs(
                    observed=observed, artifacts=artifacts, objects=objects,
                    handoff_overrides=overrides,
                )
                with self._mock_refresh(opener, source), \
                     patch("tools.artifact_search.operations.time.time", return_value=now):
                    with self.assertRaises((ValueError, OSError)):
                        operations.refresh(self.config)
                self.assertEqual(target.read_bytes(), before)

    def test_refresh_installs_private_handoff_after_verified_remote_snapshot(self):
        target = Path(self.config["handoff"])
        if target.exists():
            target.unlink()
        now = int(time.time())
        _remote, source, opener = self._refresh_inputs(observed=now - 5)
        with self._mock_refresh(opener, source), patch(
            "tools.artifact_search.operations.time.time", return_value=now
        ):
            result = operations.refresh(self.config)

        installed = json.loads(target.read_bytes())
        self.assertEqual(result["snapshot_id"], installed["snapshot_id"])
        self.assertEqual(installed["source_id"], SOURCE_ID)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(opener.requests), 1)


if __name__ == "__main__":
    unittest.main()
