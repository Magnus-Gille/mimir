"""Contract tests for the pure snapshot-delta helpers."""

from __future__ import annotations

import copy
import unittest
from uuid import UUID, uuid5

from tools.artifact_search.snapshot_delta import assign_ids, diff_manifests


SOURCE = "11111111-1111-4111-8111-111111111111"


def artifact(path: str, digest: str, size: int = 10, mtime_ns: int = 1) -> dict:
    return {"path": path, "sha256": digest, "size": size, "mtime_ns": mtime_ns}


def manifest(source_id: str, artifacts: list[dict]) -> dict:
    return {"source_id": source_id, "artifacts": artifacts}


class SnapshotDeltaTests(unittest.TestCase):
    def test_assigns_ids_and_does_not_mutate_inputs(self) -> None:
        current = [artifact("z.txt", "z"), artifact("a.txt", "a")]
        before = copy.deepcopy(current)
        result = assign_ids(SOURCE, current)

        self.assertEqual(current, before)
        self.assertEqual([item["path"] for item in result], ["a.txt", "z.txt"])
        self.assertEqual(
            result[0]["artifact_id"],
            str(uuid5(UUID(SOURCE), "a.txt\0a")),
        )
        self.assertEqual(
            result[1]["artifact_id"],
            str(uuid5(UUID(SOURCE), "z.txt\0z")),
        )

    def test_same_path_keeps_id_when_content_changes(self) -> None:
        old = assign_ids(SOURCE, [artifact("doc.txt", "old", 3, 1)])
        current = assign_ids(
            SOURCE,
            [artifact("doc.txt", "new", 4, 2)],
            manifest(SOURCE, old),
        )
        self.assertEqual(current[0]["artifact_id"], old[0]["artifact_id"])

    def test_unique_hash_rename_reuses_id_and_duplicate_hash_is_ambiguous(self) -> None:
        old = assign_ids(
            SOURCE,
            [artifact("gone.txt", "unique"), artifact("one.bin", "duplicate"), artifact("two.bin", "duplicate")],
        )
        renamed = assign_ids(
            SOURCE,
            [artifact("moved.txt", "unique"), artifact("one-new.bin", "duplicate"), artifact("two-new.bin", "duplicate")],
            manifest(SOURCE, old),
        )
        old_by_path = {item["path"]: item for item in old}
        new_by_path = {item["path"]: item for item in renamed}
        self.assertEqual(new_by_path["moved.txt"]["artifact_id"], old_by_path["gone.txt"]["artifact_id"])
        self.assertNotIn(new_by_path["one-new.bin"]["artifact_id"], {old_by_path["one.bin"]["artifact_id"], old_by_path["two.bin"]["artifact_id"]})
        self.assertNotEqual(new_by_path["one-new.bin"]["artifact_id"], new_by_path["two-new.bin"]["artifact_id"])

    def test_rejects_cross_source_previous_manifest_and_avoids_id_collision(self) -> None:
        with self.assertRaises(ValueError):
            assign_ids(SOURCE, [artifact("doc.txt", "new")], manifest("22222222-2222-4222-8222-222222222222", []))

        colliding = str(uuid5(UUID(SOURCE), "new.txt\0new"))
        old = [dict(artifact("old.txt", "old"), artifact_id=colliding)]
        current = assign_ids(SOURCE, [artifact("new.txt", "new")], manifest(SOURCE, old))
        self.assertNotEqual(current[0]["artifact_id"], colliding)

    def test_diff_reports_add_change_rename_remove_and_both_change_rename(self) -> None:
        old = assign_ids(
            SOURCE,
            [artifact("same.txt", "same"), artifact("changed.txt", "old", 3, 1), artifact("removed.txt", "gone"), artifact("rename.txt", "move", 4, 1)],
        )
        current = assign_ids(
            SOURCE,
            [artifact("same.txt", "same"), artifact("changed.txt", "new", 5, 2), artifact("renamed.txt", "move", 7, 2), artifact("added.txt", "add")],
            manifest(SOURCE, old),
        )
        delta = diff_manifests(manifest(SOURCE, old), manifest(SOURCE, current))
        self.assertEqual([item["path"] for item in delta["added"]], ["added.txt"])
        self.assertEqual([(item["before"]["path"], item["after"]["path"]) for item in delta["changed"]], [("changed.txt", "changed.txt"), ("rename.txt", "renamed.txt")])
        self.assertEqual([(item["before"]["path"], item["after"]["path"]) for item in delta["renamed"]], [("rename.txt", "renamed.txt")])
        self.assertEqual([item["path"] for item in delta["removed"]], ["removed.txt"])

    def test_diff_without_previous_marks_everything_added(self) -> None:
        current = manifest(SOURCE, assign_ids(SOURCE, [artifact("b.txt", "b"), artifact("a.txt", "a")]))
        delta = diff_manifests(None, current)
        self.assertEqual([item["path"] for item in delta["added"]], ["a.txt", "b.txt"])
        self.assertEqual(delta["changed"], [])
        self.assertEqual(delta["renamed"], [])
        self.assertEqual(delta["removed"], [])

    def test_large_inventory_preserves_ids_and_infers_one_rename(self) -> None:
        count = 10_000
        previous_artifacts = [
            artifact(f"old/{index:05d}.txt", f"digest-{index}")
            for index in range(count)
        ]
        previous = assign_ids(SOURCE, previous_artifacts)
        current_artifacts = [
            artifact(
                f"renamed/{index:05d}.txt" if index == count - 1 else f"old/{index:05d}.txt",
                f"digest-{index}",
            )
            for index in range(count)
        ]
        current = assign_ids(SOURCE, current_artifacts, manifest(SOURCE, previous))

        self.assertEqual(len(current), count)
        self.assertEqual(
            current[-1]["artifact_id"],
            previous[-1]["artifact_id"],
        )
        self.assertEqual(len({item["artifact_id"] for item in current}), count)


if __name__ == "__main__":
    unittest.main()
