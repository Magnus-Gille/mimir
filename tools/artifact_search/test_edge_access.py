"""Cloudflare Access header propagation for snapshot and original reads."""

from __future__ import annotations

from email.message import Message
import os
from unittest import TestCase
from unittest.mock import patch
from urllib.request import Request

from tools.artifact_search import runtime
from tools.artifact_search.snapshot import SnapshotError
from tools.artifact_search.snapshot_http import HttpSnapshotSource, _NoRedirect


SOURCE_ID = "33333333-3333-4333-8333-333333333333"
SNAPSHOT_ID = "b" * 64
BASE_URL = "https://files.example.test/files/" + SNAPSHOT_ID
API_KEY = "synthetic-bearer-test-key"
ACCESS_ID = "synthetic-access-id"
ACCESS_SECRET = "synthetic-access-secret"


class _Response:
    status = 200

    def __init__(self, data=b""):
        self.data = data
        self.headers = Message()
        self.headers["Content-Length"] = str(len(data))
        self.headers["Content-Encoding"] = "identity"

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _limit):
        return self.data


class _Opener:
    def __init__(self, response=None):
        self.response = response or _Response()
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        return self.response


class EdgeAccessHeaderTests(TestCase):
    def _source(self, opener, *, access_id=ACCESS_ID, access_secret=ACCESS_SECRET,
                base_url=BASE_URL):
        with patch("tools.artifact_search.snapshot_http.load_manifest", return_value={}), \
             patch("tools.artifact_search.snapshot_http.time.sleep"):
            return HttpSnapshotSource(
                base_url, SNAPSHOT_ID, SOURCE_ID, API_KEY, opener=opener,
                access_client_id=access_id, access_client_secret=access_secret,
            )

    def _assert_access_headers(self, request):
        self.assertEqual(request.get_header("Cf-access-client-id"), ACCESS_ID)
        self.assertEqual(request.get_header("Cf-access-client-secret"), ACCESS_SECRET)
        self.assertEqual(request.get_header("Authorization"), "Bearer " + API_KEY)

    def test_manifest_object_and_original_requests_carry_both_credentials(self):
        opener = _Opener()
        source = self._source(opener)
        source.read_object({"path": "docs/example.txt", "object": "objects/" + "c" * 64})
        with patch("tools.artifact_search.runtime.time.sleep"):
            runtime._read_original(source, "https://files.example.test/files/docs/example.txt")

        self.assertEqual(len(opener.requests), 3)
        for request, _timeout in opener.requests:
            with self.subTest(url=request.full_url):
                self._assert_access_headers(request)

    def test_access_credentials_must_be_supplied_as_a_pair_before_open(self):
        for access_id, access_secret in ((ACCESS_ID, ""), ("", ACCESS_SECRET), (ACCESS_ID, None)):
            with self.subTest(access_id=access_id, access_secret=access_secret):
                opener = _Opener()
                with self.assertRaises(SnapshotError):
                    self._source(opener, access_id=access_id, access_secret=access_secret)
                self.assertEqual(opener.requests, [])

    def test_access_credentials_require_printable_nonspace_ascii_up_to_4096_chars(self):
        invalid_ids = ("has space", "has\nlinebreak", "nonascii-å", "x" * 4097)
        for invalid in invalid_ids:
            with self.subTest(access_id=repr(invalid[:24]), length=len(invalid)):
                opener = _Opener()
                with self.assertRaises(SnapshotError):
                    self._source(opener, access_id=invalid)
                self.assertEqual(opener.requests, [])

        invalid_secrets = ("has space", "has\tcontrol", "nonascii-å", "x" * 4097)
        for invalid in invalid_secrets:
            with self.subTest(access_secret=repr(invalid[:24]), length=len(invalid)):
                opener = _Opener()
                with self.assertRaises(SnapshotError):
                    self._source(opener, access_secret=invalid)
                self.assertEqual(opener.requests, [])

        opener = _Opener()
        with patch("tools.artifact_search.snapshot_http.load_manifest", return_value={}):
            HttpSnapshotSource(
                BASE_URL, SNAPSHOT_ID, SOURCE_ID, API_KEY, opener=opener,
                access_client_id="i" * 4096, access_client_secret="s" * 4096,
            )
        self.assertEqual(len(opener.requests), 1)

    def test_credentials_are_keyword_only_and_http_loopback_is_rejected(self):
        opener = _Opener()
        with self.assertRaises(TypeError):
            HttpSnapshotSource(BASE_URL, SNAPSHOT_ID, SOURCE_ID, API_KEY, 15, opener,
                               ACCESS_ID, ACCESS_SECRET)

        opener = _Opener()
        with self.assertRaisesRegex(SnapshotError, "edge_credentials_require_https"):
            self._source(opener, base_url="http://127.0.0.1/files/" + SNAPSHOT_ID)
        self.assertEqual(opener.requests, [])

    def test_default_empty_access_pair_preserves_legacy_bearer_only_requests(self):
        opener = _Opener()
        with patch("tools.artifact_search.snapshot_http.load_manifest", return_value={}):
            source = HttpSnapshotSource(BASE_URL, SNAPSHOT_ID, SOURCE_ID, API_KEY, opener=opener)
        request, _timeout = opener.requests[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer " + API_KEY)
        self.assertIsNone(request.get_header("Cf-access-client-id"))
        self.assertIsNone(request.get_header("Cf-access-client-secret"))
        self.assertEqual(source.api_key, API_KEY)

    def test_from_env_reads_access_credentials(self):
        opener = _Opener()
        with patch.dict(os.environ, {
            "MIMIR_SNAPSHOT_URL": BASE_URL,
            "MIMIR_API_KEY": API_KEY,
            "MIMIR_CF_ACCESS_CLIENT_ID": ACCESS_ID,
            "MIMIR_CF_ACCESS_CLIENT_SECRET": ACCESS_SECRET,
        }), patch("tools.artifact_search.snapshot_http.build_opener", return_value=opener), \
             patch("tools.artifact_search.snapshot_http.load_manifest", return_value={}):
            HttpSnapshotSource.from_env(SNAPSHOT_ID, SOURCE_ID)
        self._assert_access_headers(opener.requests[0][0])

    def test_runtime_source_for_reads_access_credentials(self):
        opener = _Opener()
        handoff = {
            "kind": "http", "bundle": BASE_URL, "snapshot_id": SNAPSHOT_ID,
            "source_id": SOURCE_ID,
        }
        with patch.dict(os.environ, {
            "MIMIR_API_KEY": API_KEY,
            "MIMIR_CF_ACCESS_CLIENT_ID": ACCESS_ID,
            "MIMIR_CF_ACCESS_CLIENT_SECRET": ACCESS_SECRET,
        }), patch("tools.artifact_search.snapshot_http.build_opener", return_value=opener), \
             patch("tools.artifact_search.snapshot_http.load_manifest", return_value={}):
            runtime.source_for(handoff)
        self._assert_access_headers(opener.requests[0][0])

    def test_cross_origin_redirect_is_rejected_by_no_redirect_handler(self):
        request = Request(
            BASE_URL + "/manifest.json",
            headers={"Authorization": "Bearer " + API_KEY,
                     "CF-Access-Client-Id": ACCESS_ID,
                     "CF-Access-Client-Secret": ACCESS_SECRET},
        )
        with self.assertRaisesRegex(SnapshotError, "source_redirect_rejected"):
            _NoRedirect().redirect_request(
                request, None, 302, "Found", {}, "https://other.example.test/collect",
            )


if __name__ == "__main__":
    import unittest

    unittest.main()
