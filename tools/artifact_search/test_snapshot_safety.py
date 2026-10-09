"""Regression cases for source races and snapshot publication boundaries."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.artifact_search import snapshot

SOURCE_ID = '11111111-1111-4111-8111-111111111111'


class SnapshotSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'originals'
        self.store = Path(self.temp.name) / 'private'
        self.root.mkdir()
        (self.root / 'a.txt').write_text('alpha')

    def tearDown(self):
        self.temp.cleanup()

    def test_same_length_source_change_and_added_file_reject_whole_run(self):
        for mutation in ('changed', 'added'):
            with self.subTest(mutation=mutation):
                destination = self.store / mutation
                read = snapshot._read
                def racing_read(root, path, *args):
                    value = read(root, path, *args)
                    if Path(root).resolve() == self.root.resolve():
                        if mutation == 'changed':
                            (self.root / 'a.txt').write_text('omega')
                        else:
                            (self.root / 'b.txt').write_text('beta')
                    return value
                with patch.object(snapshot, '_read', racing_read):
                    with self.assertRaisesRegex(snapshot.SnapshotError, 'source_changed'):
                        snapshot.create_snapshot(self.root, destination, SOURCE_ID)
                self.assertFalse(destination.exists())

    def test_source_file_and_bundle_object_symlinks_are_not_followed(self):
        outside = Path(self.temp.name) / 'outside.txt'
        outside.write_text('private outside')
        (self.root / 'link.txt').symlink_to(outside)
        with self.assertRaisesRegex(snapshot.SnapshotError, 'source_not_regular'):
            snapshot.create_snapshot(self.root, self.store, SOURCE_ID)
        (self.root / 'link.txt').unlink()
        result = snapshot.create_snapshot(self.root, self.store, SOURCE_ID)
        source = snapshot.LocalSnapshotSource(Path(result['manifest_path']).parent,
                                              result['snapshot_id'], SOURCE_ID)
        blob = source.root / source.manifest['artifacts'][0]['object']
        blob.unlink()
        blob.symlink_to(outside)
        with self.assertRaises(snapshot.SnapshotError):
            snapshot.acquire_snapshot(source, Path(self.temp.name) / 'receiver')
        self.assertEqual(outside.read_text(), 'private outside')

    def test_interrupt_before_commit_marker_preserves_previous_snapshot(self):
        first = snapshot.create_snapshot(self.root, self.store, SOURCE_ID)
        old = Path(first['manifest_path'])
        old_bytes = old.read_bytes()
        (self.root / 'a.txt').write_text('changed')
        link = snapshot.os.link
        attempted = []
        def interrupted_link(src, dst, **kwargs):
            if Path(dst).name == 'manifest.json':
                attempted.append(dst)
                raise OSError('synthetic publication interruption')
            return link(src, dst, **kwargs)
        with patch.object(snapshot.os, 'link', interrupted_link):
            with self.assertRaises(OSError):
                snapshot.create_snapshot(self.root, self.store, SOURCE_ID, previous=old)
        self.assertEqual(len(attempted), 1)
        self.assertFalse(Path(attempted[0]).exists())
        self.assertEqual(old.read_bytes(), old_bytes)
        self.assertEqual(list(self.store.glob('.pending-*')), [])
        self.assertEqual(list(self.store.rglob('manifest.json')), [old])

    def test_store_permissions_and_symlink_collision_are_fail_closed(self):
        self.store.mkdir(mode=0o755)
        with self.assertRaisesRegex(snapshot.SnapshotError, 'store_must_be_private'):
            snapshot.create_snapshot(self.root, self.store, SOURCE_ID)
        self.assertEqual(list(self.store.iterdir()), [])
        self.store.chmod(0o700)
        result = snapshot.create_snapshot(self.root, self.store, SOURCE_ID)
        alias = Path(self.temp.name) / 'alias'
        alias.symlink_to(self.store, target_is_directory=True)
        with self.assertRaises(snapshot.SnapshotError):
            snapshot.create_snapshot(self.root, alias, SOURCE_ID)
        self.assertTrue(Path(result['manifest_path']).is_file())

    def test_inventory_bound_counts_excluded_paths_during_enumeration(self):
        for name in ('.one', '.two', '.three'):
            (self.root / name).write_text('ignored')
        with patch.object(snapshot, 'MAX_ARTIFACTS', 3):
            with self.assertRaisesRegex(snapshot.SnapshotError, 'inventory_limit_exceeded'):
                snapshot.create_snapshot(self.root, self.store, SOURCE_ID)
        self.assertFalse(self.store.exists())
