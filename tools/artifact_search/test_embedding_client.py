import io
import json
import unittest
from urllib.error import HTTPError

from tools.artifact_search.vectors import EmbeddingIdentity


class Transport:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return io.BytesIO(json.dumps(result).encode())


class ClientTests(unittest.TestCase):
    def identity(self):
        return EmbeddingIdentity('a'*40 + ':' + 'b'*64, 128)

    def release(self):
        return {'gateway': {'revision': 'a'*40}}

    def payload(self):
        return {'object': 'list', 'model': 'embeddinggemma-2', 'usage': {'prompt_tokens': 8, 'total_tokens': 8},
                'data': [{'object': 'embedding', 'index': 0, 'embedding': [1.0]+[0.0]*127}]}

    def test_private_routing_and_immutable_release_binding(self):
        from tools.artifact_search.embedding_client import M5Embedder
        transport = Transport([self.release(), self.payload(), self.release()])
        client = M5Embedder(self.identity(), 'http://127.0.0.1:8080/v1', 'synthetic-key', opener=transport)
        self.assertEqual(client.embed(['title: none | text: test'])[0][0], 1.0)
        self.assertEqual([r.get_method() for r in transport.requests], ['GET', 'POST', 'GET'])
        sent = json.loads(transport.requests[1].data)
        self.assertEqual(sent['dimensions'], 128)
        self.assertEqual(sent['input'], ['title: none | text: test'])

    def test_public_or_credential_routes_rejected_before_transport(self):
        from tools.artifact_search.embedding_client import M5Embedder
        for url in ('https://example.com/v1','http://8.8.8.8/v1','http://key@localhost/v1','http://localhost/v1?token=x','http://169.254.169.254/v1'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                M5Embedder(self.identity(), url, 'synthetic-key')

    def test_release_change_or_bad_vectors_are_never_returned(self):
        from tools.artifact_search.embedding_client import M5Embedder, EmbedderUnavailable
        for responses in ([{'gateway': {'revision': 'c'*40}}], [{'gateway': []}],
                          [self.release(), {**self.payload(), 'data': []}],
                          [self.release(), self.payload(), {'gateway': {'revision': 'c'*40}}]):
            with self.subTest(responses=responses), self.assertRaises(EmbedderUnavailable):
                M5Embedder(self.identity(),'http://localhost:8080/v1','synthetic-key',opener=Transport(responses)).embed(['x'])

    def test_busy_retry_is_bounded_and_never_reflects_response_body(self):
        from tools.artifact_search.embedding_client import M5Embedder, EmbedderUnavailable
        busy = HTTPError('http://localhost/v1',503,'private upstream data',{'Retry-After':'20'},io.BytesIO(b'secret response'))
        with self.assertRaises(EmbedderUnavailable) as caught:
            M5Embedder(self.identity(),'http://localhost/v1','synthetic-key',opener=Transport([busy])).embed(['x'])
        self.assertNotIn('secret',str(caught.exception))
        self.assertEqual(caught.exception.retry_after,20)

    def test_batch_body_limits_reject_without_model_call(self):
        from tools.artifact_search.embedding_client import M5Embedder
        transport = Transport([])
        client = M5Embedder(self.identity(),'http://localhost/v1','synthetic-key',opener=transport)
        for inputs in ([], ['x']*17, ['å'*16000]*2, ['x'*16385]):
            with self.subTest(count=len(inputs)), self.assertRaises(ValueError):
                client.embed(inputs)
        self.assertEqual(transport.requests, [])

    def test_absolute_deadline_bounds_requests_retries_and_sleeps(self):
        from tools.artifact_search.embedding_client import M5Embedder, EmbedderUnavailable
        now = [0.0]

        class SlowBusy:
            def __init__(self):
                self.timeouts = []

            def open(self, request, timeout):
                self.timeouts.append(timeout)
                now[0] += timeout  # every request uses its full allowance, then is busy
                raise HTTPError('http://localhost/v1', 503, 'busy', {'Retry-After': '2'}, io.BytesIO(b''))

        def sleep(seconds):
            now[0] += seconds

        transport = SlowBusy()
        client = M5Embedder(self.identity(), 'http://localhost/v1', 'synthetic-key', timeout=8,
                            opener=transport, sleep=sleep, deadline=11.0, clock=lambda: now[0])
        with self.assertRaises(EmbedderUnavailable) as caught:
            client.embed(['x'])
        self.assertLessEqual(now[0], 11.0)
        self.assertEqual(transport.timeouts[0], 8)
        self.assertLessEqual(sum(transport.timeouts), 11.0)
        expired = M5Embedder(self.identity(), 'http://localhost/v1', 'synthetic-key', opener=Transport([]),
                             deadline=5.0, clock=lambda: 5.0)
        with self.assertRaises(EmbedderUnavailable) as late:
            expired.embed(['x'])
        self.assertEqual(str(late.exception), 'embedding_deadline_exceeded')

    def test_overflowing_numbers_are_an_invalid_response(self):
        from tools.artifact_search.embedding_client import M5Embedder, EmbedderUnavailable
        payload = self.payload()
        payload['data'][0]['embedding'] = [10 ** 400] + [0.0] * 127
        client = M5Embedder(self.identity(), 'http://localhost/v1', 'synthetic-key',
                            opener=Transport([self.release(), payload, self.release()]))
        with self.assertRaises(EmbedderUnavailable) as caught:
            client.embed(['x'])
        self.assertEqual(str(caught.exception), 'embedding_invalid_response')
