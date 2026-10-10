"""Transport and handoff boundary tests for the snapshot search runtime."""

from __future__ import annotations

from email.message import Message
from pathlib import Path
import os
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from types import SimpleNamespace

from tools.artifact_search import runtime
from tools.artifact_search.runtime import Runtime, RuntimeErrorCode, load_handoff
from tools.artifact_search.snapshot import MAX_BYTES, canonical


SOURCE_ID = "33333333-3333-4333-8333-333333333333"
SNAPSHOT_ID = "b" * 64
ORIGIN = "https://files.example.test"


class _Response:
    def __init__(self, status: int, data: bytes, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self._data = data
        self.headers = Message()
        for name, value in (headers or {}).items():
            self.headers[name] = value

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return self._data


class _Opener:
    def __init__(self, outcome) -> None:
        self.outcome = outcome
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def _reader(outcome, *, last_request=None):
    opener = _Opener(outcome)
    return SimpleNamespace(
        opener=opener,
        api_key="transport-test-key",
        timeout=7,
        last_request=last_request,
    ), opener


def _write_private(path: Path, value: dict) -> None:
    path.write_bytes(canonical(value))
    os.chmod(path, 0o600)


def _handoff(**overrides) -> dict:
    value = {
        "schema": "mimir-search-handoff-v1",
        "source_id": SOURCE_ID,
        "snapshot_id": SNAPSHOT_ID,
        "observed_at": 100,
        "expires_at": 200,
        "kind": "http",
        "bundle": ORIGIN + "/files/" + SNAPSHOT_ID,
        "original": ORIGIN + "/files",
        "mimir_origin": ORIGIN,
        "original_prefix": "",
    }
    value.update(overrides)
    return value


class ReadOriginalTransportTests(unittest.TestCase):
    def test_failures_are_bounded_and_sanitized(self) -> None:
        cases = (
            ("redirect", HTTPError("https://upstream.invalid/secret", 302, "redirect", {}, None)),
            ("http-error", HTTPError("https://upstream.invalid/secret", 503, "upstream secret", {}, None)),
            ("non200", _Response(404, b"private response")),
            ("encoding", _Response(200, b"private response", {"Content-Encoding": "gzip"})),
            ("oversize", _Response(200, b"", {"Content-Length": str(MAX_BYTES + 1)})),
            ("truncated", _Response(200, b"short", {"Content-Length": "99"})),
        )
        for name, outcome in cases:
            with self.subTest(name=name):
                reader, opener = _reader(outcome)
                with self.assertRaisesRegex(RuntimeErrorCode, "^original_unavailable$") as raised:
                    runtime._read_original(reader, ORIGIN + "/files/docs/secret.txt")
                self.assertNotIn("upstream", str(raised.exception))
                self.assertNotIn("private", str(raised.exception))
                self.assertEqual(len(opener.requests), 1)
                request, timeout = opener.requests[0]
                self.assertEqual(timeout, 7)
                self.assertEqual(request.get_header("Authorization"), "Bearer transport-test-key")
                self.assertEqual(request.get_header("Accept-encoding"), "identity")

    def test_read_respects_pacing_between_original_reads(self) -> None:
        reader, _opener = _reader(_Response(200, b"ok", {"Content-Length": "2"}), last_request=time.monotonic())
        with patch("tools.artifact_search.runtime.time.sleep") as sleep:
            self.assertEqual(runtime._read_original(reader, ORIGIN + "/files/docs/ok.txt"), b"ok")
        sleep.assert_called_once()
        self.assertGreater(sleep.call_args.args[0], 0)
        self.assertLessEqual(sleep.call_args.args[0], 1.1)


class HttpHandoffValidationTests(unittest.TestCase):
    def _load(self, value: dict, now: int = 150):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "handoff.json"
            _write_private(path, value)
            return load_handoff(path, now=now)

    def test_http_handoff_rejects_cross_origin_bundle_and_original_mismatch(self) -> None:
        with self.assertRaisesRegex(RuntimeErrorCode, "source_origin_mismatch"):
            self._load(_handoff(bundle="https://other.example/files/" + SNAPSHOT_ID))
        with self.assertRaisesRegex(RuntimeErrorCode, "original_binding_mismatch"):
            self._load(_handoff(original=ORIGIN + "/files/other-prefix"))

    def test_http_handoff_rejects_userinfo_including_empty_username(self) -> None:
        for origin in ("https://@files.example.test", "https://user:password@files.example.test"):
            with self.subTest(origin=origin):
                with self.assertRaisesRegex(RuntimeErrorCode, "invalid_origin"):
                    self._load(_handoff(mimir_origin=origin))

    def test_http_handoff_rejects_original_prefix_traversal(self) -> None:
        for prefix in ("../secret", "archive/..", ".hidden", "/absolute", "archive\\escape"):
            with self.subTest(prefix=prefix):
                with self.assertRaisesRegex(RuntimeErrorCode, "invalid_original_prefix"):
                    self._load(_handoff(original_prefix=prefix))


class RuntimeStateTests(unittest.TestCase):
    def _local_handoff(self, directory: Path) -> Path:
        path = directory / "handoff.json"
        _write_private(
            path,
            dict(
                _handoff(
                    kind="local",
                    bundle=str(directory / "bundle"),
                    original=str(directory / "original"),
                )
            ),
        )
        return path

    def test_health_reports_unavailable_for_corrupt_job_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            state = directory / "state"
            state.mkdir(mode=0o700)
            handoff = self._local_handoff(directory)
            job = state / "job.json"
            job.write_bytes(b'{"status":')
            os.chmod(job, 0o600)
            status = Runtime(state, handoff, clock=lambda: 150).health()
            self.assertEqual(status["status"], "unavailable")
            self.assertEqual(status["error"], "job_state_unreadable")
            self.assertFalse(status["ready"])

    def test_rebuild_moves_database_and_all_sidecars_before_recreation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            state = directory / "state"
            state.mkdir(mode=0o700)
            handoff = self._local_handoff(directory)
            index_name = SNAPSHOT_ID + ".sqlite"
            job = state / "job.json"
            _write_private(job, {"status": "failed", "index": index_name})
            sidecars = ("", "-journal", "-wal", "-shm")
            expected = {}
            for sidecar in sidecars:
                path = state / (index_name + sidecar)
                expected[sidecar] = ("stale-" + (sidecar or "db")).encode()
                path.write_bytes(expected[sidecar])
                os.chmod(path, 0o600)

            runtime_instance = Runtime(state, handoff, clock=lambda: 150)
            runtime_instance.control("rebuild")
            self.assertEqual(runtime_instance.job()["status"], "pending")
            for sidecar in sidecars:
                active = state / (index_name + sidecar)
                self.assertFalse(active.exists(), sidecar)
                saved = [
                    path
                    for path in state.iterdir()
                    if path.name.startswith(index_name + ".saved-")
                    and (path.name.endswith(sidecar) if sidecar else not path.name.endswith(("-journal", "-wal", "-shm")))
                ]
                self.assertEqual(len(saved), 1, sidecar)
                self.assertEqual(saved[0].read_bytes(), expected[sidecar])


if __name__ == "__main__":
    unittest.main()
