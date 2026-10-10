"""Explicit private M5 API client; no credential discovery or public fallback."""
from __future__ import annotations

import ipaddress
import json
import os
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .vectors import EmbeddingIdentity, normalize


class EmbedderUnavailable(RuntimeError):
    def __init__(self, reason='embedding_unavailable', status=None, retry_after=None):
        super().__init__(reason)
        self.status = status
        self.retry_after = retry_after


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def payload_bytes(inputs, identity):
    return json.dumps({'model': identity.model, 'input': inputs,
                      'dimensions': identity.dimensions, 'encoding_format': 'float'},
                     ensure_ascii=False, separators=(',', ':')).encode('utf-8')


class M5Embedder:
    def __init__(self, identity: EmbeddingIdentity, base_url: str, api_key: str,
                 timeout=30, opener=None, sleep=time.sleep):
        parts = urlsplit(base_url)
        try:
            _ = parts.port
            private = parts.hostname == 'localhost'
            if not private:
                address = ipaddress.ip_address(parts.hostname or '')
                networks = ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '100.64.0.0/10', 'fc00::/7')
                private = address.is_loopback or any(address in ipaddress.ip_network(net) for net in networks)
        except ValueError:
            private = False
        if (not private or parts.scheme not in {'http', 'https'} or parts.username or
                parts.password or parts.query or parts.fragment or parts.path.rstrip('/') != '/v1'):
            raise ValueError('Embedding API must be an explicit private M5 /v1 base')
        if not isinstance(api_key, str) or not api_key or any(not 33 <= ord(char) <= 126 for char in api_key):
            raise ValueError('M5_API_KEY is required')
        # Revision binds gateway code plus the operator-verified model artifact checksum.
        if not re.fullmatch(r'[0-9a-f]{40}:[0-9a-f]{64}', identity.revision):
            raise ValueError('Embedding revision must be gateway SHA:model SHA256')
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 60:
            raise ValueError('Embedding timeout must be 1..60 seconds')
        self.identity = identity
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.timeout = timeout
        self.opener = opener or build_opener(ProxyHandler({}), _NoRedirect())
        self.sleep = sleep

    @classmethod
    def from_env(cls, identity):
        return cls(identity, os.environ.get('M5_OPENAI_BASE_URL', ''), os.environ.get('M5_API_KEY', ''))

    def _request(self, url, body=None):
        for attempt in range(2):
            req = Request(url, data=body, method='GET' if body is None else 'POST',
                          headers={'Authorization': 'Bearer ' + self.api_key,
                                   'Content-Type': 'application/json', 'User-Agent': 'mimir-artifact-search/1'})
            try:
                with self.opener.open(req, timeout=self.timeout) as response:
                    raw = response.read(4*1024*1024 + 1)
                if len(raw) > 4*1024*1024:
                    raise EmbedderUnavailable('embedding_response_too_large')
                return json.loads(raw)
            except HTTPError as error:
                try:
                    delay = float(error.headers.get('Retry-After', ''))
                    if not 0 <= delay <= 600:
                        delay = None
                except (ValueError, TypeError):
                    delay = None
                if attempt == 0 and error.code in {429, 503} and delay is not None and delay <= 2:
                    error.close()
                    self.sleep(delay)
                    continue
                error.close()
                raise EmbedderUnavailable('embedding_http_error', error.code, delay) from error
            except (URLError, OSError) as error:
                raise EmbedderUnavailable('embedding_transport_error') from error
            except (ValueError, UnicodeError) as error:
                raise EmbedderUnavailable('embedding_invalid_json') from error

    def _verify_release(self):
        body = self._request(self.base_url[:-3] + '/portal/me')
        gateway = body.get('gateway') if isinstance(body, dict) else None
        if not isinstance(gateway, dict) or gateway.get('revision') != self.identity.revision.split(':')[0]:
            raise EmbedderUnavailable('embedding_gateway_revision_mismatch')

    def embed(self, inputs):
        if (not isinstance(inputs, list) or not 1 <= len(inputs) <= 16 or
                any(not isinstance(item, str) or not 1 <= len(item) <= 16384 for item in inputs)):
            raise ValueError('Embedding input must contain 1..16 bounded strings')
        payload = payload_bytes(inputs, self.identity)
        if len(payload) > 32768:
            raise ValueError('Embedding request exceeds 32 KiB')
        self._verify_release()
        body = self._request(self.base_url + '/embeddings', payload)
        try:
            if body['object'] != 'list' or body['model'] != self.identity.model or len(body['data']) != len(inputs):
                raise ValueError('invalid envelope')
            tokens = body['usage']['prompt_tokens']
            if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 1 or body['usage']['total_tokens'] != tokens:
                raise ValueError('invalid usage')
            vectors = []
            for index, item in enumerate(body['data']):
                if item['index'] != index or item['object'] != 'embedding':
                    raise ValueError('invalid order')
                vectors.append(normalize(item['embedding'], self.identity.dimensions))
        except (KeyError, ValueError, TypeError, IndexError) as error:
            raise EmbedderUnavailable('embedding_invalid_response') from error
        self._verify_release()
        return vectors
