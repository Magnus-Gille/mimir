"""Explicit authenticated Mímir bundle reader; no redirects, proxies or writes."""
from __future__ import annotations

import ipaddress
from http.client import HTTPException
import os
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, ProxyHandler, HTTPRedirectHandler, build_opener

from .snapshot import HEX, MAX_BYTES, MAX_MANIFEST_BYTES, SnapshotError, _uuid, load_manifest


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SnapshotError('source_redirect_rejected')


def source_headers(url, api_key, access_client_id='', access_client_secret=''):
    """Only explicit owner credentials; never derive auth from URLs or responses."""
    if (not isinstance(api_key, str) or not api_key or len(api_key) > 4096 or
            any(ord(c) < 33 or ord(c) > 126 for c in api_key)):
        raise SnapshotError('invalid_source_credential')
    if (not all(isinstance(v, str) and len(v) <= 4096 and
                all(33 <= ord(c) <= 126 for c in v)
                for v in (access_client_id, access_client_secret)) or
            bool(access_client_id) != bool(access_client_secret)):
        raise SnapshotError('invalid_edge_credentials')
    if access_client_id and urlsplit(url).scheme != 'https':
        raise SnapshotError('edge_credentials_require_https')
    headers = {'Authorization': 'Bearer ' + api_key, 'Accept-Encoding': 'identity'}
    if access_client_id:
        headers.update({'CF-Access-Client-Id': access_client_id,
                        'CF-Access-Client-Secret': access_client_secret})
    return headers


class HttpSnapshotSource:
    def __init__(self, base_url, expected_sha256, source_id, api_key, timeout=15, opener=None,
                 *, access_client_id='', access_client_secret=''):
        _uuid(source_id)
        if not isinstance(expected_sha256, str) or not HEX.fullmatch(expected_sha256):
            raise SnapshotError('invalid_manifest_pin')
        if not isinstance(base_url, str) or any(ord(c) < 33 or ord(c) == 127 for c in base_url):
            raise SnapshotError('invalid_source_url')
        try:
            url = urlsplit(base_url)
            host = url.hostname
            port = url.port
        except ValueError:
            raise SnapshotError('invalid_source_url') from None
        if (not host or url.username is not None or url.password is not None or url.query or url.fragment or
                '\\' in base_url or not re.fullmatch(r'/files/[A-Za-z0-9/_-]+', url.path) or
                '//' in url.path or url.path.rstrip('/').split('/')[-1] != expected_sha256 or
                url.scheme not in {'https', 'http'} or port == 0):
            raise SnapshotError('invalid_source_url')
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if url.scheme == 'http' and not (host == 'localhost' or address and address.is_loopback):
            raise SnapshotError('source_requires_https')
        if address and (address.is_unspecified or address.is_multicast or address.is_link_local):
            raise SnapshotError('invalid_source_url')
        headers = source_headers(base_url, api_key, access_client_id, access_client_secret)
        if type(timeout) not in {int, float} or not 0 < timeout <= 60:
            raise SnapshotError('invalid_source_timeout')
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.headers = headers
        self.timeout = timeout
        self.last_request = None
        self.opener = opener or build_opener(ProxyHandler({}), _NoRedirect())
        self.snapshot_id = expected_sha256
        self.manifest_bytes = self._get('manifest.json', MAX_MANIFEST_BYTES)
        self.manifest = load_manifest(self.manifest_bytes, expected_sha256, source_id)

    @classmethod
    def from_env(cls, expected_sha256, source_id):
        return cls(os.environ.get('MIMIR_SNAPSHOT_URL', ''), expected_sha256, source_id,
                   os.environ.get('MIMIR_API_KEY', ''),
                   access_client_id=os.environ.get('MIMIR_CF_ACCESS_CLIENT_ID', ''),
                   access_client_secret=os.environ.get('MIMIR_CF_ACCESS_CLIENT_SECRET', ''))

    def _get(self, relative, limit):
        if relative != 'manifest.json' and not re.fullmatch(r'objects/[0-9a-f]{64}', relative):
            raise SnapshotError('invalid_object_path')
        request = Request(self.base_url + '/' + relative, headers=self.headers)
        # Leave margin under Mímir's default 60/IP/minute window, including
        # the manifest request. Shared-IP activity can still yield a fail-closed 429.
        if self.last_request is not None:
            delay = 1.1 - (time.monotonic() - self.last_request)
            if delay > 0:
                time.sleep(delay)
        self.last_request = time.monotonic()
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if response.status != 200 or response.headers.get('Content-Encoding', 'identity') != 'identity':
                    raise SnapshotError('invalid_source_response')
                size = response.headers.get('Content-Length')
                if size is not None and (not size.isdecimal() or int(size) > limit):
                    raise SnapshotError('oversized_source_response')
                data = response.read(limit + 1)
                if len(data) > limit or size is not None and len(data) != int(size):
                    raise SnapshotError('truncated_or_oversized_source_response')
                return data
        except HTTPError as error:
            raise SnapshotError('source_http_' + str(error.code)) from None
        except (OSError, URLError, HTTPException):
            raise SnapshotError('source_unavailable') from None

    def read_object(self, item):
        try:
            return self._get(item['object'], MAX_BYTES)
        except SnapshotError as error:
            raise SnapshotError(str(error), item['path']) from None
