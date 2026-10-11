"""Contract tests for the pinned local client and loopback SSH payload."""

from __future__ import annotations

from contextlib import contextmanager
import http.client
import json
import os
from pathlib import Path
import base64
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from tools.artifact_search import client, client_remote
from tools.artifact_search import agent_tool, client_cli
from tools.artifact_search.test_runtime import (
    API_KEY,
    ORIGIN,
    SOURCE_ID,
    _Clock,
)
from tools.artifact_search import test_runtime


RELEASE = "a" * 40
PACKAGE_SHA256 = "b" * 64
SYNTHETIC_KEY = "synthetic-search-key-for-tests"


def _profile(**overrides):
    value = {
        "schema": "mimir-search-client-v1",
        "ssh_host": "search-host",
        "source_id": SOURCE_ID,
        "mimir_origin": ORIGIN,
        "release": RELEASE,
        "package_sha256": PACKAGE_SHA256,
    }
    value.update(overrides)
    return value


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(data)
    os.chmod(path, 0o600)


@contextmanager
def _serve(runtime, api_key=SYNTHETIC_KEY):
    server = ThreadingHTTPServer(("127.0.0.1", 0), client_remote_handler(runtime, api_key))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def client_remote_handler(runtime, api_key):
    # Import the real runtime handler here to keep test setup explicit.
    from tools.artifact_search.runtime import handler

    return handler(runtime, api_key)


class ClientInputAndReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.profile_path = self.root / "client.json"
        _write_private(self.profile_path, json.dumps(_profile()).encode())

    def tearDown(self):
        self.tempdir.cleanup()

    def test_load_profile_accepts_private_pinned_profile(self):
        self.assertEqual(client.load_profile(self.profile_path), _profile())

    def test_load_profile_rejects_bad_schema_fields_origin_and_permissions(self):
        cases = (
            {"schema": "wrong"},
            {"unexpected": True},
            {"ssh_host": "-option"},
            {"mimir_origin": "http://files.example.test"},
            {"mimir_origin": "https://user@files.example.test"},
            {"release": "A" * 40},
            {"package_sha256": "c" * 63},
        )
        for override in cases:
            with self.subTest(override=override):
                _write_private(self.profile_path, json.dumps(_profile(**override)).encode())
                with self.assertRaises(client.ClientError) as raised:
                    client.load_profile(self.profile_path)
                self.assertEqual(raised.exception.code, "invalid_profile")
        _write_private(self.profile_path, json.dumps(_profile()).encode())
        os.chmod(self.profile_path, 0o644)
        with self.assertRaises(client.ClientError) as raised:
            client.load_profile(self.profile_path)
        self.assertEqual(raised.exception.code, "invalid_profile")

    def test_validate_receipt_checks_freshness_pins_and_result_provenance(self):
        profile = _profile()
        now = int(time.time())
        receipt = _receipt(profile, now)
        self.assertIs(client.validate_receipt(receipt, profile, 5, now=now), receipt)

        cases = (
            ({**receipt, "source_id": "11111111-1111-4111-8111-111111111111"}, now, "invalid_response"),
            ({**receipt, "release": "c" * 40}, now, "invalid_response"),
            ({**receipt, "package_sha256": "c" * 64}, now, "invalid_response"),
            ({**receipt, "verified_at": now - 31}, now, "stale_response"),
            ({**receipt, "expires_at": now}, now, "stale_response"),
            ({**receipt, "extra": True}, now, "invalid_response"),
            ({**receipt, "results": [{**receipt["results"][0], "sha256": "z" * 64}]}, now, "invalid_response"),
            ({**receipt, "results": [{**receipt["results"][0], "mimir_url": ORIGIN + "/files/elsewhere"}]}, now, "invalid_response"),
            ({**receipt, "results": [{**receipt["results"][0], "path": "../outside.txt"}]}, now, "invalid_response"),
            ({**receipt, "results": [{**receipt["results"][0], "title": "\ud800"}]}, now, "invalid_response"),
        )
        for value, observed_now, code in cases:
            with self.subTest(code=code, value=value):
                with self.assertRaises(client.ClientError) as raised:
                    client.validate_receipt(value, profile, 5, now=observed_now)
                self.assertEqual(raised.exception.code, code)

    def test_validate_receipt_rejects_top_overflow_and_nonfinite_numbers(self):
        profile = _profile()
        now = int(time.time())
        receipt = _receipt(profile, now)
        with self.assertRaises(client.ClientError):
            client.validate_receipt({**receipt, "results": [*receipt["results"], *receipt["results"]]}, profile, 1, now=now)
        with self.assertRaises(client.ClientError):
            client.validate_receipt({**receipt, "verified_at": float("nan")}, profile, 5, now=now)


def _receipt(profile, now, *, results=True):
    snapshot_id = "d" * 64
    items = []
    if results:
        items.append({
            "path": "docs/alpha.txt",
            "artifact_id": "docs/alpha.txt",
            "sha256": "e" * 64,
            "locator": {"kind": "section", "number": 1, "anchor": None},
            "title": "alpha",
            "snippet": "North star retrieval evidence.",
            "score": 1.0,
            "mimir_url": ORIGIN + "/files/docs/alpha.txt",
            "source_id": profile["source_id"],
            "snapshot_id": snapshot_id,
            "verification": "original_sha256_matched",
        })
    return {
        "mode": "lexical",
        "decision": "fts5_bm25",
        "results": items,
        "snapshot_id": snapshot_id,
        "source_id": profile["source_id"],
        "observed_at": now - 5,
        "expires_at": now + 300,
        "verified_at": now,
        "release": profile["release"],
        "package_sha256": profile["package_sha256"],
        "abstained": not bool(items),
    }


class ClientRemoteIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tempdir.name)
        self.fixture = test_runtime.RuntimeContractTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.now = int(time.time())
        self.runtime, self.original, self.snapshot, self.bundle, self.handoff = self.fixture._runtime(
            "client", clock=_Clock(self.now)
        )
        self.fixture._handoff(
            self.handoff,
            self.original,
            self.snapshot,
            self.bundle,
            observed_at=self.now - 10,
            expires_at=self.now + 300,
        )
        self.fixture._complete(self.runtime)
        self.key_path = self.workspace / "runtime.env"
        _write_private(self.key_path, f"MIMIR_SEARCH_API_KEY={SYNTHETIC_KEY}\n".encode())
        self.environment = patch.dict(os.environ, {
            "MIMIR_SEARCH_RELEASE": RELEASE,
            "MIMIR_SEARCH_PACKAGE_SHA256": PACKAGE_SHA256,
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_search_uses_remote_loopback_and_returns_real_verified_hit_and_no_hit(self):
        with _serve(self.runtime) as server:
            with patch.object(client_remote, "KEY_PATH", self.key_path), patch.object(client_remote, "PORT", server.server_port), patch.object(
                client, "_transport", side_effect=lambda _profile, payload: client_remote.execute(json.loads(payload))
            ):
                hit = client.search("north star", profile_path=self._profile_file())
                self.assertEqual(len(hit["results"]), 1)
                self.assertEqual(hit["results"][0]["verification"], "original_sha256_matched")
                self.assertEqual(hit["results"][0]["sha256"], self._original_hash())
                self.assertEqual(hit["release"], RELEASE)
                self.assertEqual(hit["package_sha256"], PACKAGE_SHA256)
                self.assertFalse(client.search("volcano", profile_path=self._profile_file())["results"])

    def test_remote_rejects_wrong_bearer_and_unreachable_server_with_stable_codes(self):
        with _serve(self.runtime, api_key=API_KEY) as server:
            _write_private(self.key_path, b"MIMIR_SEARCH_API_KEY=wrong-synthetic-key\n")
            with patch.object(client_remote, "KEY_PATH", self.key_path), patch.object(client_remote, "PORT", server.server_port):
                with self.assertRaises(client_remote.RemoteError) as raised:
                    client_remote.execute({"query": "north star", "top": 5})
                self.assertEqual(raised.exception.code, "unauthorized")

        with patch.object(client_remote, "KEY_PATH", self.key_path), patch.object(client_remote, "PORT", 1):
            with self.assertRaises(client_remote.RemoteError) as raised:
                client_remote.execute({"query": "north star", "top": 5})
            self.assertEqual(raised.exception.code, "retrieval_unavailable")

    def test_remote_missing_or_insecure_credential_fails_before_request(self):
        missing = self.workspace / "missing.env"
        with patch.object(client_remote, "KEY_PATH", missing):
            with self.assertRaises(client_remote.RemoteError) as raised:
                client_remote.execute({"query": "north star", "top": 5})
        self.assertEqual(raised.exception.code, "credential_unavailable")

        os.chmod(self.key_path, 0o644)
        with patch.object(client_remote, "KEY_PATH", self.key_path):
            with self.assertRaises(client_remote.RemoteError) as raised:
                client_remote.execute({"query": "north star", "top": 5})
        self.assertEqual(raised.exception.code, "credential_unavailable")

    def test_runtime_stale_lease_and_changed_original_fail_closed(self):
        with _serve(self.runtime) as server:
            with patch.object(client_remote, "KEY_PATH", self.key_path), patch.object(client_remote, "PORT", server.server_port):
                self.runtime.clock.value = self.now + 301
                with self.assertRaises(client_remote.RemoteError) as raised:
                    client_remote.execute({"query": "north star", "top": 5})
                self.assertEqual(raised.exception.code, "retrieval_unavailable")

                self.runtime.clock.value = self.now
                target = self.original / "docs" / "alpha.txt"
                target.write_text("Changed original content.\n", encoding="utf-8")
                with self.assertRaises(client_remote.RemoteError) as raised:
                    client_remote.execute({"query": "north star", "top": 5})
                self.assertEqual(raised.exception.code, "retrieval_unavailable")

    def test_remote_entrypoint_never_echoes_synthetic_key_or_raw_failure(self):
        from io import BytesIO, StringIO
        from unittest.mock import patch as mock_patch

        stdout = StringIO()
        with mock_patch.object(client_remote, "KEY_PATH", self.key_path), mock_patch.object(
            client_remote, "PORT", 1
        ), mock_patch("sys.stdin", type("Input", (), {"buffer": BytesIO(b'{"query":"north star","top":5}')})()), mock_patch(
            "sys.stdout", stdout
        ):
            client_remote.main()
        output = stdout.getvalue()
        self.assertNotIn(SYNTHETIC_KEY, output)
        self.assertNotIn("Connection refused", output)
        self.assertEqual(json.loads(output), {"ok": False, "error": "retrieval_unavailable"})

    def test_real_cli_and_agent_wrappers_return_safe_verified_envelopes(self):
        with _serve(self.runtime) as server:
            transport = lambda _profile, payload: client_remote.execute(json.loads(payload))
            with patch.object(client_remote, "KEY_PATH", self.key_path), patch.object(
                client_remote, "PORT", server.server_port
            ), patch.object(client, "_transport", side_effect=transport), patch.object(
                client_cli, "search", side_effect=lambda query, top, *, profile_path=None: client.search(
                    query, top, profile_path=profile_path
                )
            ):
                from io import StringIO
                from unittest.mock import patch as mock_patch

                output = StringIO()
                with mock_patch("sys.stdout", output):
                    status = client_cli.main(["north star", "--config", str(self._profile_file()), "--json"])
                self.assertEqual(status, 0)
                cli_receipt = json.loads(output.getvalue())
                self.assertEqual(cli_receipt["results"][0]["verification"], "original_sha256_matched")
                self.assertNotIn(SYNTHETIC_KEY, output.getvalue())

                result = agent_tool.handle_tool_input(
                    {"query": "north star", "top": 1}, profile_path=self._profile_file()
                )
                self.assertEqual(result["status"], "ok")
                self.assertEqual(result["evidence"]["results"][0]["sha256"], self._original_hash())
                self.assertNotIn(SYNTHETIC_KEY, json.dumps(result))

    def test_cli_and_agent_wrapper_errors_hide_transport_details(self):
        from io import StringIO
        from unittest.mock import patch as mock_patch

        output = StringIO()
        with patch.object(client_cli, "search", side_effect=client.ClientError("transport_unavailable")), mock_patch(
            "sys.stdout", output
        ):
            status = client_cli.main(["north star", "--config", str(self._profile_file()), "--json"])
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(output.getvalue())["error"]["code"], "transport_unavailable")
        self.assertNotIn("Connection refused", output.getvalue())

        with patch.object(agent_tool, "search", side_effect=client.ClientError("transport_unavailable")):
            failure = agent_tool.handle_tool_input({"query": "north star"})
        self.assertEqual(failure["status"], "unavailable")
        self.assertEqual(failure["error"]["code"], "transport_unavailable")

    def test_ssh_transport_sends_bounded_payload_and_accepts_only_bounded_reply(self):
        payload = b'{"query":"north star","top":3}'
        response = b'{"ok":true,"data":{"synthetic":true}}\n'
        audit = self.workspace / "ssh-audit.json"
        self._fake_ssh(audit, response)
        with patch.dict(os.environ, {"PATH": str(self.fake_bin), "HOME": str(self.workspace)}):
            result = client._transport(_profile(), payload)
        self.assertEqual(result, {"synthetic": True})
        record = json.loads(audit.read_text())
        self.assertEqual(record["stdin"], payload.decode())
        argv = record["argv"]
        self.assertEqual(argv[0], "-T")
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("StrictHostKeyChecking=yes", argv)
        self.assertIn("ClearAllForwardings=yes", argv)
        self.assertIn("ForwardAgent=no", argv)
        self.assertEqual(argv[-2], "search-host")
        self.assertNotIn(SYNTHETIC_KEY, " ".join(argv))

        oversized = self.workspace / "oversized-audit.json"
        self._fake_ssh(oversized, b"x" * (client.LIMIT + 1))
        with patch.dict(os.environ, {"PATH": str(self.fake_bin), "HOME": str(self.workspace)}):
            with self.assertRaises(client.ClientError) as raised:
                client._transport(_profile(), payload)
        self.assertEqual(raised.exception.code, "invalid_response")

    def test_ssh_transport_timeout_kills_child_and_returns_stable_error(self):
        audit = self.workspace / "timeout-audit.json"
        self._fake_ssh(audit, b"", sleep=True)
        with patch.dict(os.environ, {"PATH": str(self.fake_bin), "HOME": str(self.workspace)}), patch.object(
            client.time, "monotonic", side_effect=[0.0, 46.0]
        ):
            with self.assertRaises(client.ClientError) as raised:
                client._transport(_profile(), b'{"query":"north star","top":5}')
        self.assertEqual(raised.exception.code, "transport_unavailable")

    def _profile_file(self):
        path = self.workspace / "client.json"
        _write_private(path, json.dumps(_profile()).encode())
        return path

    def _fake_ssh(self, audit: Path, response: bytes, *, sleep=False):
        self.fake_bin = self.workspace / "bin"
        self.fake_bin.mkdir(exist_ok=True)
        encoded = base64.b64encode(response).decode("ascii")
        script = (
            "#!" + os.sys.executable + "\n"
            "import base64, json, os, sys, time\n"
            "payload = sys.stdin.buffer.read()\n"
            f"open({str(audit)!r}, 'w').write(json.dumps({{'argv': sys.argv[1:], 'stdin': payload.decode('utf-8')}}))\n"
            + ("time.sleep(20)\n" if sleep else f"sys.stdout.buffer.write(base64.b64decode({encoded!r}))\n")
        )
        executable = self.fake_bin / "ssh"
        executable.write_text(script, encoding="utf-8")
        executable.chmod(0o700)

    def _original_hash(self):
        import hashlib

        return hashlib.sha256((self.original / "docs" / "alpha.txt").read_bytes()).hexdigest()


if __name__ == "__main__":
    unittest.main()
