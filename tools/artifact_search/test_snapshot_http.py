"""Acceptance tests for authenticated HTTP snapshot acquisition.

The server in this module is a small NAS-shaped fixture: it exposes an
immutable bundle beneath ``/files/<bundle>/<manifest-sha>`` and requires the
fixture bearer token for every request.  It deliberately records requests so
the tests can assert that rejected manifests do not cause object fetches and
that redirects do not forward credentials.
"""

from __future__ import annotations

import copy
import json
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest

from tools.artifact_search.snapshot import (
    LocalSnapshotSource,
    SnapshotError,
    acquire_snapshot,
    canonical,
    create_snapshot,
    digest,
)
from tools.artifact_search.snapshot_http import HttpSnapshotSource


SOURCE_ID = "11111111-1111-4111-8111-111111111111"
FIXTURE_TOKEN = "fixture-bearer"


class _BundleHandler(BaseHTTPRequestHandler):
    server: "_BundleServer"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _send(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self.server.requests.append((self.path, self.headers.get("Authorization")))
        if self.headers.get("Authorization") != "Bearer " + self.server.expected_token:
            self._send(self.server.auth_failure_status, b"unauthorized")
            return
        route = self.server.routes.get(self.path)
        if route is None:
            self._send(404, b"missing")
            return
        status, body, headers = route
        self._send(status, body, headers)


class _BundleServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(
        self,
        routes: dict[str, tuple[int, bytes, dict[str, str]]],
        *,
        expected_token: str = FIXTURE_TOKEN,
        auth_failure_status: int = 401,
    ) -> None:
        super().__init__(("127.0.0.1", 0), _BundleHandler)
        self.routes = routes
        self.expected_token = expected_token
        self.auth_failure_status = auth_failure_status
        self.requests: list[tuple[str, str | None]] = []


@contextmanager
def _running_server(
    routes: dict[str, tuple[int, bytes, dict[str, str]]],
    *,
    expected_token: str = FIXTURE_TOKEN,
    auth_failure_status: int = 401,
):
    server = _BundleServer(
        routes,
        expected_token=expected_token,
        auth_failure_status=auth_failure_status,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _NoNetworkOpener:
    def __init__(self) -> None:
        self.calls = 0

    def open(self, *_args: object, **_kwargs: object):
        self.calls += 1
        raise AssertionError("invalid input attempted network access")


class HttpSnapshotContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tempdir.name)
        self.root = self.workspace / "archive"
        self.published = self.workspace / "published"
        self.root.mkdir()
        self.published.mkdir(mode=0o700)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _bundle(self) -> tuple[dict, bytes, str, LocalSnapshotSource]:
        (self.root / "docs").mkdir()
        (self.root / "docs" / "alpha.txt").write_bytes(b"alpha\n")
        (self.root / "docs" / "beta.md").write_bytes("beta ä\n".encode("utf-8"))
        result = create_snapshot(self.root, self.published, SOURCE_ID)
        manifest_path = Path(result["manifest_path"])
        raw = manifest_path.read_bytes()
        manifest = json.loads(raw.decode("utf-8"))
        source = LocalSnapshotSource(manifest_path.parent, result["snapshot_id"], SOURCE_ID)
        return manifest, raw, result["snapshot_id"], source

    @staticmethod
    def _routes_for_bundle(
        manifest: dict,
        raw: bytes,
        snapshot_id: str,
        source: LocalSnapshotSource,
        *,
        prefix: str = "synthetic",
    ) -> tuple[dict[str, tuple[int, bytes, dict[str, str]]], str]:
        base_path = f"/files/{prefix}/{snapshot_id}"
        routes: dict[str, tuple[int, bytes, dict[str, str]]] = {
            base_path + "/manifest.json": (200, raw, {}),
        }
        for item in manifest["artifacts"]:
            routes[base_path + "/" + item["object"]] = (200, source.read_object(item), {})
        return routes, base_path

    def _assert_unpublished(self, store: Path) -> None:
        self.assertFalse(store.exists() and any(store.rglob("manifest.json")))

    def test_authenticated_http_acquisition_matches_local_bundle_bytes(self) -> None:
        manifest, raw, snapshot_id, local = self._bundle()
        routes, base_path = self._routes_for_bundle(manifest, raw, snapshot_id, local)
        acquired_store = self.workspace / "http-acquired"

        with _running_server(routes) as (server, host):
            http = HttpSnapshotSource(host + base_path, snapshot_id, SOURCE_ID, FIXTURE_TOKEN)
            result = acquire_snapshot(http, acquired_store)

            self.assertEqual(http.manifest_bytes, local.manifest_bytes)
            self.assertEqual(result["snapshot_id"], snapshot_id)
            for item in manifest["artifacts"]:
                self.assertEqual(http.read_object(item), local.read_object(item))
            self.assertEqual(
                (acquired_store / snapshot_id / "manifest.json").read_bytes(),
                raw,
            )
            for path, authorization in server.requests:
                self.assertTrue(path.endswith("manifest.json") or "/objects/" in path)
                self.assertEqual(authorization, "Bearer " + FIXTURE_TOKEN)

    def test_authentication_failures_are_reported_for_401_and_403(self) -> None:
        manifest, raw, snapshot_id, local = self._bundle()
        routes, base_path = self._routes_for_bundle(manifest, raw, snapshot_id, local)

        for status in (401, 403):
            with self.subTest(status=status):
                with _running_server(routes, auth_failure_status=status) as (server, host):
                    with self.assertRaisesRegex(SnapshotError, f"source_http_{status}"):
                        HttpSnapshotSource(host + base_path, snapshot_id, SOURCE_ID, "wrong-token")
                    self.assertEqual(len(server.requests), 1)
                    self.assertEqual(server.requests[0][0], base_path + "/manifest.json")

    def test_loopback_outage_is_fail_closed(self) -> None:
        manifest, raw, snapshot_id, local = self._bundle()
        routes, base_path = self._routes_for_bundle(manifest, raw, snapshot_id, local)
        server = _BundleServer(routes)
        host = f"http://127.0.0.1:{server.server_port}"
        server.server_close()

        with self.assertRaisesRegex(SnapshotError, "source_unavailable"):
            HttpSnapshotSource(host + base_path, snapshot_id, SOURCE_ID, FIXTURE_TOKEN)

    def test_missing_and_truncated_manifests_are_rejected_before_objects(self) -> None:
        manifest, raw, snapshot_id, local = self._bundle()
        routes, base_path = self._routes_for_bundle(manifest, raw, snapshot_id, local)
        routes.pop(base_path + "/manifest.json")
        with _running_server(routes) as (server, host):
            with self.assertRaisesRegex(SnapshotError, "source_http_404"):
                HttpSnapshotSource(host + base_path, snapshot_id, SOURCE_ID, FIXTURE_TOKEN)
            self.assertEqual([path for path, _auth in server.requests], [base_path + "/manifest.json"])

        truncated = raw[:-10]
        truncated_pin = digest(truncated)
        truncated_base = f"/files/synthetic/{truncated_pin}"
        with _running_server({truncated_base + "/manifest.json": (200, truncated, {})}) as (
            server,
            host,
        ):
            with self.assertRaisesRegex(SnapshotError, "invalid_manifest"):
                HttpSnapshotSource(host + truncated_base, truncated_pin, SOURCE_ID, FIXTURE_TOKEN)
            self.assertEqual([path for path, _auth in server.requests], [truncated_base + "/manifest.json"])

    def test_changed_or_missing_object_rejects_whole_snapshot_without_manifest(self) -> None:
        manifest, raw, snapshot_id, local = self._bundle()
        base_routes, base_path = self._routes_for_bundle(manifest, raw, snapshot_id, local)
        target = manifest["artifacts"][-1]

        for failure, route in (
            ("changed", (200, b"tampered-object", {})),
            ("missing", None),
        ):
            with self.subTest(failure=failure):
                routes = dict(base_routes)
                object_path = base_path + "/" + target["object"]
                if route is None:
                    routes.pop(object_path)
                else:
                    routes[object_path] = route
                acquired_store = self.workspace / f"failed-{failure}"
                with _running_server(routes) as (_server, host):
                    http = HttpSnapshotSource(host + base_path, snapshot_id, SOURCE_ID, FIXTURE_TOKEN)
                    with self.assertRaises(SnapshotError):
                        acquire_snapshot(http, acquired_store)
                self._assert_unpublished(acquired_store)

    def test_redirect_is_rejected_without_following_or_forwarding_bearer(self) -> None:
        manifest, raw, snapshot_id, local = self._bundle()
        routes, base_path = self._routes_for_bundle(manifest, raw, snapshot_id, local)
        manifest_path = base_path + "/manifest.json"
        routes[manifest_path] = (302, b"", {"Location": "/redirect-target"})
        routes["/redirect-target"] = (200, raw, {})

        with _running_server(routes) as (server, host):
            with self.assertRaisesRegex(SnapshotError, "source_redirect_rejected"):
                HttpSnapshotSource(host + base_path, snapshot_id, SOURCE_ID, FIXTURE_TOKEN)
            self.assertEqual(server.requests, [(manifest_path, "Bearer " + FIXTURE_TOKEN)])

    def test_manifest_path_tamper_with_new_pin_fails_before_object_fetch(self) -> None:
        manifest, _raw, _snapshot_id, local = self._bundle()
        tampered = copy.deepcopy(manifest)
        tampered["artifacts"][0]["object"] = "objects/../escaped-object"
        tampered_raw = canonical(tampered)
        tampered_pin = digest(tampered_raw)
        base_path = f"/files/synthetic/{tampered_pin}"
        routes = {base_path + "/manifest.json": (200, tampered_raw, {})}

        with _running_server(routes) as (server, host):
            with self.assertRaisesRegex(SnapshotError, "invalid_artifact"):
                HttpSnapshotSource(host + base_path, tampered_pin, SOURCE_ID, FIXTURE_TOKEN)
            self.assertEqual(server.requests, [(base_path + "/manifest.json", "Bearer " + FIXTURE_TOKEN)])

    def test_wrong_manifest_pin_rejects_without_object_fetch(self) -> None:
        _manifest, raw, _snapshot_id, _local = self._bundle()
        wrong_pin = "0" * 64
        base_path = f"/files/synthetic/{wrong_pin}"
        routes = {base_path + "/manifest.json": (200, raw, {})}

        with _running_server(routes) as (server, host):
            with self.assertRaisesRegex(SnapshotError, "manifest_hash_mismatch"):
                HttpSnapshotSource(host + base_path, wrong_pin, SOURCE_ID, FIXTURE_TOKEN)
            self.assertEqual(server.requests, [(base_path + "/manifest.json", "Bearer " + FIXTURE_TOKEN)])

    def test_fixed_url_and_token_validation_rejects_without_network(self) -> None:
        pin = "a" * 64
        valid_source = f"https://nas.example.invalid/files/synthetic/{pin}"
        invalid_urls = (
            f"http://nas.example.invalid/files/synthetic/{pin}",
            f"http://192.168.1.20/files/synthetic/{pin}",
            f"https://nas.example.invalid/files/synthetic/not-{pin}",
            f"https://nas.example.invalid/files//{pin}",
            f"https://nas.example.invalid/files/synthetic/{pin}?download=1",
            f"https://user:pass@nas.example.invalid/files/synthetic/{pin}",
            f"https://@nas.example.invalid/files/synthetic/{pin}",
            "file:///tmp/files/synthetic/" + pin,
        )
        for url in invalid_urls:
            with self.subTest(url=url):
                opener = _NoNetworkOpener()
                with self.assertRaises(SnapshotError):
                    HttpSnapshotSource(url, pin, SOURCE_ID, FIXTURE_TOKEN, opener=opener)
                self.assertEqual(opener.calls, 0)

        for token in ("", " ", "bad\nvalue", "å"):
            with self.subTest(token=repr(token)):
                opener = _NoNetworkOpener()
                with self.assertRaisesRegex(SnapshotError, "invalid_source_credential"):
                    HttpSnapshotSource(valid_source, pin, SOURCE_ID, token, opener=opener)
                self.assertEqual(opener.calls, 0)


if __name__ == "__main__":
    unittest.main()
