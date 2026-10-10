"""Release and rollback acceptance must be tied to approved bytes, never HEAD."""
import hashlib
import json
import unittest
import time
from unittest.mock import patch

from tools.artifact_search.package_runtime import build, verify


class PackageTests(unittest.TestCase):
    def package(self, revision='a' * 40):
        def git(args, **_kwargs):
            if 'rev-parse' in args:
                return (revision + '\n').encode()
            return b'# synthetic source\n'
        with patch('subprocess.check_output', side_effect=git):
            return build('.', revision)

    def test_only_explicit_exact_release_accepted(self):
        for value in ('HEAD', 'main', 'abc1234', 'A' * 40, ''):
            with self.assertRaises(ValueError):
                build('.', value)

    def test_deterministic_package_and_tamper_rejected(self):
        data = self.package()
        self.assertEqual(data, self.package())
        sha = hashlib.sha256(data).hexdigest()
        self.assertFalse(verify(data, 'a' * 40, sha)['deployment_accepted'])
        with self.assertRaises(ValueError):
            verify(data + b'changed', 'a' * 40, sha)
        with self.assertRaises(ValueError):
            verify(data, 'b' * 40, sha)

    def evidence(self, sha, revision='a' * 40):
        now = int(time.time())
        health = {'release': revision, 'package_sha256': sha, 'ready': True,
                  'snapshot_id': '1' * 64, 'source_id': '11111111-1111-4111-8111-111111111111',
                  'observed_at': now - 10, 'expires_at': now + 60}
        probe = {**health, 'verified_at': now, 'results': [{'sha256': '2' * 64,
                 'verification': 'original_sha256_matched'}]}
        return health, probe

    def test_health_is_not_release_acceptance_without_identity_and_readiness(self):
        data = self.package()
        sha = hashlib.sha256(data).hexdigest()
        good, probe = self.evidence(sha)
        with self.assertRaises(ValueError):
            verify(data, 'a' * 40, sha, good)
        bound = dict(retrieval=probe, expected_snapshot=good['snapshot_id'], expected_source=good['source_id'])
        self.assertTrue(verify(data, 'a' * 40, sha, good, **bound)['deployment_accepted'])
        for changes in ({'ready': False}, {'release': 'b' * 40}, {'package_sha256': 'bad'}):
            with self.assertRaises(ValueError):
                verify(data, 'a' * 40, sha, {**good, **changes}, **bound)
        for changes in ({'results': []}, {'verified_at': int(time.time()) - 31},
                        {'snapshot_id': '3' * 64}, {'package_sha256': 'bad'}):
            with self.assertRaises(ValueError):
                verify(data, 'a' * 40, sha, good, **{**bound, 'retrieval': {**probe, **changes}})

    def test_rollback_requires_previous_package_identity(self):
        old, new = self.package('a' * 40), self.package('b' * 40)
        old_sha, new_sha = (hashlib.sha256(item).hexdigest() for item in (old, new))
        live, probe = self.evidence(new_sha, 'b' * 40)
        bound = dict(retrieval=probe, expected_snapshot=live['snapshot_id'], expected_source=live['source_id'])
        with self.assertRaises(ValueError):
            verify(old, 'a' * 40, old_sha, live, **bound)
        live, probe = self.evidence(old_sha)
        self.assertTrue(verify(old, 'a' * 40, old_sha, live, **{**bound, 'retrieval': probe})['deployment_accepted'])


if __name__ == '__main__':
    unittest.main()
