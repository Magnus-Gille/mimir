"""Acceptance tests for the owner-only snapshot search runtime."""

from __future__ import annotations

from contextlib import contextmanager
import http.client
import json
import os
import sqlite3
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

from tools.artifact_search import runtime as runtime_module
from tools.artifact_search.runtime import Runtime, RuntimeErrorCode, handler, load_handoff
from tools.artifact_search.snapshot import SnapshotError, canonical, create_snapshot, digest


SOURCE_ID = "22222222-2222-4222-8222-222222222222"
API_KEY = "runtime-test-owner-key"
ORIGIN = "https://files.example.test"


class _Clock:
    def __init__(self, value: int, *future: int) -> None:
        self.value = value
        self._future = list(future)

    def __call__(self) -> int:
        if self._future:
            self.value = self._future.pop(0)
        return self.value


class _RecordingRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def health(self) -> dict:
        self.calls.append(("health",))
        return {"status": "ready", "ready": True, "fresh": True}

    def search(self, query: str, top: int) -> dict:
        self.calls.append(("search", query, top))
        return {"mode": "lexical", "results": []}


@contextmanager
def _running_http(runtime, api_key: str = API_KEY):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler(runtime, api_key))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _http_request(server, method: str, path: str, body: bytes | None = None, *, auth: bool = True):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = "Bearer " + API_KEY
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    raw = response.read()
    status = response.status
    connection.close()
    return status, json.loads(raw.decode("utf-8"))


class RuntimeContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _snapshot(self, name: str = "first", *, source_id: str = SOURCE_ID):
        root = self.workspace / (name + "-original")
        store = self.workspace / (name + "-snapshots")
        root.mkdir(mode=0o700)
        store.mkdir(mode=0o700)
        docs = root / "docs"
        docs.mkdir(mode=0o700)
        (docs / "alpha.txt").write_text("North star retrieval evidence.\n", encoding="utf-8")
        (docs / "beta.txt").write_text("South wind archive evidence.\n", encoding="utf-8")
        result = create_snapshot(root, store, source_id)
        return root, result, Path(result["manifest_path"]).parent

    def _handoff(
        self,
        path: Path,
        root: Path,
        result: dict,
        bundle: Path,
        *,
        observed_at: int = 100,
        expires_at: int = 200,
        source_id: str = SOURCE_ID,
    ) -> None:
        value = {
            "schema": "mimir-search-handoff-v1",
            "source_id": source_id,
            "snapshot_id": result["snapshot_id"],
            "observed_at": observed_at,
            "expires_at": expires_at,
            "kind": "local",
            "bundle": str(bundle),
            "original": str(root),
            "mimir_origin": ORIGIN,
            "original_prefix": "",
        }
        path.write_bytes(canonical(value))
        os.chmod(path, 0o600)

    def _runtime(self, name: str = "first", clock=None):
        root, result, bundle = self._snapshot(name)
        state = self.workspace / (name + "-state")
        state.mkdir(mode=0o700)
        handoff = self.workspace / (name + "-handoff.json")
        self._handoff(handoff, root, result, bundle)
        runtime = Runtime(state, handoff, clock=clock or _Clock(150))
        return runtime, root, result, bundle, handoff

    def _complete(self, runtime: Runtime) -> list[str]:
        statuses = []
        for _ in range(8):
            statuses.append(runtime.step())
            if statuses[-1] == "ready":
                break
        self.assertEqual(statuses[-1], "ready")
        return statuses

    def test_one_artifact_steps_persist_and_resume_after_restart(self) -> None:
        runtime, _root, _result, _bundle, handoff = self._runtime()
        self.assertEqual(runtime.step(), "pending")
        first_job = runtime.job()
        self.assertEqual(first_job["status"], "pending")
        self.assertEqual(first_job["counts"]["file_count"] + first_job["counts"]["excluded_count"], 1)

        restarted = Runtime(runtime.state, handoff, clock=_Clock(150))
        statuses = self._complete(restarted)
        self.assertEqual(statuses[:-1], ["pending"])
        self.assertTrue(restarted.health()["ready"])

    def test_cancel_blocks_future_steps(self) -> None:
        runtime, _root, _result, _bundle, _handoff_path = self._runtime()
        runtime.control("cancel")
        self.assertEqual(runtime.job()["status"], "cancelled")
        self.assertEqual(runtime.step(), "cancelled")
        self.assertFalse((runtime.state / "lock").is_symlink())

    def test_retry_resumes_existing_progress(self) -> None:
        runtime, _root, _result, _bundle, _handoff_path = self._runtime()
        self.assertEqual(runtime.step(), "pending")
        runtime.control("retry")
        self.assertEqual(runtime.job()["status"], "pending")
        self._complete(runtime)
        self.assertEqual(runtime.job()["counts"]["file_count"], 2)

    def test_rebuild_recovers_corrupt_and_missing_database(self) -> None:
        for failure in ("corrupt", "missing"):
            with self.subTest(failure=failure):
                runtime, _root, _result, _bundle, _handoff_path = self._runtime(failure)
                self.assertEqual(runtime.step(), "pending")
                index_path = runtime.state / runtime.job()["index"]
                if failure == "corrupt":
                    index_path.write_bytes(b"not a sqlite database")
                    self.assertEqual(runtime.step(), "failed")
                else:
                    index_path.unlink()
                runtime.control("rebuild")
                self.assertEqual(runtime.job()["status"], "pending")
                self._complete(runtime)
                self.assertTrue(runtime.health()["ready"])
                if failure == "corrupt":
                    self.assertTrue(list(runtime.state.glob(index_path.name + ".saved-*")))

    def test_stale_lease_new_snapshot_and_rollback_bindings_block_retrieval(self) -> None:
        clock = _Clock(150)
        runtime, old_root, old_result, old_bundle, handoff = self._runtime(clock=clock)
        self._complete(runtime)

        clock.value = 201
        with self.assertRaisesRegex(RuntimeErrorCode, "source_stale"):
            runtime.search("north star")

        clock.value = 170
        new_root, new_result, new_bundle = self._snapshot("new")
        # New snapshot observation keeps the trusted original scope binding.
        self._handoff(handoff, old_root, new_result, new_bundle, observed_at=160, expires_at=260)
        with self.assertRaisesRegex(RuntimeErrorCode, "index_unavailable"):
            runtime.search("north star")

        self.assertEqual(runtime.step(), "pending")
        self._handoff(handoff, old_root, old_result, old_bundle, observed_at=100, expires_at=200)
        with self.assertRaisesRegex(RuntimeErrorCode, "handoff_replay_or_scope_change"):
            runtime.search("north star")

    def test_provenance_has_exact_location_hash_and_credential_free_url(self) -> None:
        runtime, root, result, _bundle, _handoff_path = self._runtime()
        self._complete(runtime)
        output = runtime.search("north star", top=1)
        self.assertEqual(output["snapshot_id"], result["snapshot_id"])
        self.assertEqual(output["source_id"], SOURCE_ID)
        self.assertEqual(len(output["results"]), 1)
        item = output["results"][0]
        source_bytes = (root / "docs" / "alpha.txt").read_bytes()
        self.assertEqual(item["path"], "docs/alpha.txt")
        self.assertEqual(item["sha256"], digest(source_bytes))
        self.assertEqual(item["locator"], {"kind": "section", "number": 1, "anchor": None})
        self.assertEqual(item["mimir_url"], ORIGIN + "/files/docs/alpha.txt")
        self.assertEqual(item["source_id"], SOURCE_ID)
        self.assertEqual(item["snapshot_id"], result["snapshot_id"])
        self.assertEqual(item["verification"], "original_sha256_matched")
        self.assertNotIn("@", item["mimir_url"])

    def test_index_provenance_must_match_pinned_manifest(self) -> None:
        runtime, _root, _result, _bundle, _handoff_path = self._runtime()
        self._complete(runtime)
        with sqlite3.connect(runtime.state / runtime.job()['index']) as connection:
            connection.execute("UPDATE units SET artifact_id='unrelated-identity'")
        with self.assertRaisesRegex(RuntimeErrorCode, 'index_provenance_mismatch'):
            runtime.search('north star', top=1)

    def test_malformed_job_cannot_redirect_rebuild_or_break_health(self) -> None:
        runtime, _root, _result, _bundle, _handoff = self._runtime()
        self._complete(runtime)
        original = runtime.job()
        for value in ([], {'status': 'ready'}, {**original, 'index': '../outside.sqlite'}):
            runtime.save(value)
            self.assertFalse(runtime.health()['ready'])
            with self.assertRaises(ValueError):
                runtime.control('rebuild')

    def test_missing_or_changed_original_rejects_all_content(self) -> None:
        for failure in ("missing", "changed"):
            with self.subTest(failure=failure):
                runtime, root, _result, _bundle, _handoff_path = self._runtime(failure)
                self._complete(runtime)
                target = root / "docs" / "alpha.txt"
                if failure == "missing":
                    target.unlink()
                else:
                    target.write_text("North star retrieval was changed.\n", encoding="utf-8")
                with self.assertRaises((RuntimeErrorCode, OSError)):
                    runtime.search("north star")

    def test_expiry_during_source_read_fails_closed(self) -> None:
        clock = _Clock(150, 201)
        runtime, root, result, bundle, handoff_path = self._runtime(clock=clock)
        self.assertEqual(runtime.step(), "pending")
        job = runtime.job()
        self.assertEqual((job["status"], job["error"]), ("pending", "source_stale"))
        self.assertFalse(runtime.health()["ready"])
        with self.assertRaises(RuntimeErrorCode):
            runtime.search("north star")
        # Still stale after the backoff: no progress, no readiness.
        clock.value = job["retry_after"]
        self.assertEqual(runtime.step(), "pending")
        self.assertFalse(runtime.health()["ready"])
        # A renewed lease from the refresher lets the job finish.
        clock.value = runtime.job()["retry_after"]
        self._handoff(handoff_path, root, result, bundle, observed_at=clock.value - 1, expires_at=clock.value + 600)
        self._complete(runtime)
        self.assertTrue(runtime.health()["ready"])

    def _flaky_source(self, *codes: str):
        """Make the next source_for calls raise the given codes, then delegate."""
        real = runtime_module.source_for
        pending = list(codes)
        calls = []

        def flaky(handoff):
            calls.append(handoff["snapshot_id"])
            if pending:
                raise SnapshotError(pending.pop(0))
            return real(handoff)

        return mock.patch.object(runtime_module, "source_for", flaky), calls

    def test_transient_source_error_backs_off_and_recovers(self) -> None:
        clock = _Clock(150)
        runtime, _root, _result, _bundle, _handoff_path = self._runtime(clock=clock)
        patch, calls = self._flaky_source("source_http_502")
        with patch:
            self.assertEqual(runtime.step(), "pending")
            job = runtime.job()
            self.assertEqual((job["status"], job["error"]), ("pending", "source_http_502"))
            self.assertGreater(job["retry_after"], 150)
            # Inside the backoff window the source is not contacted again.
            self.assertEqual(runtime.step(), "pending")
            self.assertEqual(len(calls), 1)
            clock.value = job["retry_after"]
            self._complete(runtime)
        job = runtime.job()
        self.assertEqual(job["status"], "ready")
        self.assertNotIn("retry_after", job)
        self.assertNotIn("transient_failures", job)
        self.assertIsNone(job["error"])
        self.assertTrue(runtime.health()["ready"])

    def test_transient_error_on_ready_job_blocks_search_until_recovered(self) -> None:
        clock = _Clock(150)
        runtime, _root, _result, _bundle, _handoff_path = self._runtime(clock=clock)
        self._complete(runtime)
        patch, _calls = self._flaky_source("source_unavailable")
        with patch:
            self.assertEqual(runtime.step(), "pending")
            self.assertFalse(runtime.health()["ready"])
            with self.assertRaises(RuntimeErrorCode):
                runtime.search("north star")
            clock.value = runtime.job()["retry_after"]
            self.assertEqual(runtime.step(), "ready")
        self.assertTrue(runtime.search("north star")["results"])

    def test_repeated_transient_errors_back_off_with_a_cap(self) -> None:
        clock = _Clock(150)
        runtime, root, result, bundle, handoff_path = self._runtime(clock=clock)
        self._handoff(handoff_path, root, result, bundle, observed_at=100, expires_at=3700)
        patch, _calls = self._flaky_source(*["source_http_503"] * 12)
        delays = []
        with patch:
            for _ in range(12):
                self.assertEqual(runtime.step(), "pending")
                job = runtime.job()
                delays.append(job["retry_after"] - clock.value)
                clock.value = job["retry_after"]
        self.assertEqual(delays, sorted(delays))
        self.assertLess(delays[0], delays[-1])
        self.assertLessEqual(max(delays), runtime_module.MAX_RETRY_SECONDS)
        self.assertEqual(runtime.job()["transient_failures"], 12)
        self.assertEqual(runtime.job()["error"], "source_http_503")

    def test_permanent_source_errors_still_fail_closed(self) -> None:
        for code in ("source_http_404", "invalid_source_response", "source_http_401"):
            with self.subTest(code=code):
                runtime, _root, _result, _bundle, _handoff_path = self._runtime(code.replace("_", "-"))
                patch, _calls = self._flaky_source(code)
                with patch:
                    self.assertEqual(runtime.step(), "failed")
                self.assertEqual(runtime.job()["status"], "failed")
                self.assertEqual(runtime.step(), "failed")

    def test_control_actions_on_backed_off_job_keep_state_readable(self) -> None:
        for action in ("cancel", "retry", "rebuild"):
            with self.subTest(action=action):
                runtime, _root, _result, _bundle, _handoff_path = self._runtime("control-" + action)
                patch, _calls = self._flaky_source("source_http_502")
                with patch:
                    self.assertEqual(runtime.step(), "pending")
                runtime.control(action)
                job = runtime.job()
                self.assertNotIn("retry_after", job)
                self.assertNotIn("transient_failures", job)
                self.assertIn("status", runtime.health())
                if action == "cancel":
                    self.assertEqual(runtime.step(), "cancelled")
                    runtime.control("retry")
                self._complete(runtime)

    def test_retry_after_is_bounded_and_finite(self) -> None:
        clock = _Clock(150)
        runtime, root, result, bundle, handoff_path = self._runtime(clock=clock)
        self._handoff(handoff_path, root, result, bundle, observed_at=100, expires_at=3700)
        patch, _calls = self._flaky_source("source_http_502")
        with patch:
            self.assertEqual(runtime.step(), "pending")
        with runtime.locked():
            job = runtime.job()
            job["retry_after"] = clock.value + 10 ** 9
            runtime.save(job)
        # A far-future backoff (clock jump or corruption) waits at most the cap.
        clock.value += runtime_module.MAX_RETRY_SECONDS
        self._complete(runtime)
        for bad in ("Infinity", "NaN", "\"soon\""):
            with self.subTest(bad=bad):
                path = runtime.state / "job.json"
                job = json.loads(path.read_text(encoding="utf-8"))
                job.update(status="pending", transient_failures=1, error="source_http_502")
                text = json.dumps(job).replace('"error"', '"retry_after": %s, "error"' % bad, 1)
                path.write_text(text, encoding="utf-8")
                with self.assertRaises(RuntimeErrorCode):
                    runtime.job()

    def test_legacy_failed_job_with_transient_error_recovers(self) -> None:
        runtime, _root, _result, _bundle, _handoff_path = self._runtime()
        self._complete(runtime)
        with runtime.locked():
            job = runtime.job()
            job["status"], job["error"] = "failed", "source_http_502"
            runtime.save(job)
        self.assertEqual(runtime.step(), "ready")
        self.assertTrue(runtime.health()["ready"])

    def test_corrupt_database_returns_no_content(self) -> None:
        runtime, _root, _result, _bundle, _handoff_path = self._runtime()
        self._complete(runtime)
        index_path = runtime.state / runtime.job()["index"]
        index_path.write_bytes(b"corrupt")
        with self.assertRaises(ValueError):
            runtime.search("north star")

    def test_authenticated_http_get_and_post_succeed(self) -> None:
        runtime, _root, _result, _bundle, _handoff_path = self._runtime()
        self._complete(runtime)
        with _running_http(runtime) as server:
            status, health = _http_request(server, "GET", "/health")
            self.assertEqual(status, 200)
            self.assertTrue(health["ready"])
            status, search = _http_request(
                server, "POST", "/search", json.dumps({"query": "north star", "top": 1}).encode()
            )
            self.assertEqual(status, 200)
            self.assertEqual(search["results"][0]["path"], "docs/alpha.txt")

    def test_unauthorized_health_and_search_do_not_call_runtime(self) -> None:
        runtime = _RecordingRuntime()
        with _running_http(runtime) as server:
            status, _body = _http_request(server, "GET", "/health", auth=False)
            self.assertEqual(status, 401)
            status, _body = _http_request(server, "POST", "/search", b'{"query":"north"}', auth=False)
            self.assertEqual(status, 401)
        self.assertEqual(runtime.calls, [])

    def test_malformed_queries_and_lengths_are_rejected_before_runtime(self) -> None:
        runtime = _RecordingRuntime()
        with _running_http(runtime) as server:
            for body in (
                {"query": "", "top": 1},
                {"query": "north", "top": True},
                {"query": "n" * 4097, "top": 1},
            ):
                with self.subTest(body_keys=list(body)):
                    status, _response = _http_request(
                        server, "POST", "/search", json.dumps(body).encode()
                    )
                    self.assertEqual(status, 400)
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            connection.putrequest("POST", "/search")
            connection.putheader("Authorization", "Bearer " + API_KEY)
            connection.putheader("Content-Length", "not-a-number")
            connection.endheaders()
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 400)
            connection.close()
        self.assertEqual(runtime.calls, [])


class HandoffContractTests(unittest.TestCase):
    def test_handoff_rejects_stale_clock(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "handoff.json"
            value = {
                "schema": "mimir-search-handoff-v1",
                "source_id": SOURCE_ID,
                "snapshot_id": "a" * 64,
                "observed_at": 100,
                "expires_at": 200,
                "kind": "local",
                "bundle": "/tmp/bundle",
                "original": "/tmp/original",
                "mimir_origin": ORIGIN,
                "original_prefix": "",
            }
            path.write_bytes(canonical(value))
            os.chmod(path, 0o600)
            with self.assertRaisesRegex(RuntimeErrorCode, "source_stale"):
                load_handoff(path, now=200)


if __name__ == "__main__":
    unittest.main()
