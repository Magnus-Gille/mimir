"""Release and rollback acceptance must be tied to approved bytes, never HEAD."""
import hashlib
import json
import unittest
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

    def test_health_is_not_release_acceptance_without_identity_and_readiness(self):
        data = self.package()
        sha = hashlib.sha256(data).hexdigest()
        good = {'release': 'a' * 40, 'package_sha256': sha, 'ready': True}
        self.assertTrue(verify(data, 'a' * 40, sha, good)['deployment_accepted'])
        for changes in ({'ready': False}, {'release': 'b' * 40}, {'package_sha256': 'bad'}):
            with self.assertRaises(ValueError):
                verify(data, 'a' * 40, sha, {**good, **changes})

    def test_rollback_requires_previous_package_identity(self):
        old, new = self.package('a' * 40), self.package('b' * 40)
        old_sha, new_sha = (hashlib.sha256(item).hexdigest() for item in (old, new))
        live = {'release': 'b' * 40, 'package_sha256': new_sha, 'ready': True}
        with self.assertRaises(ValueError):
            verify(old, 'a' * 40, old_sha, live)
        live.update(release='a' * 40, package_sha256=old_sha)
        self.assertTrue(verify(old, 'a' * 40, old_sha, live)['deployment_accepted'])


if __name__ == '__main__':
    unittest.main()
