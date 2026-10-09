"""Pure artifact identity assignment and manifest delta helpers.

The source adapter owns validation and publication of manifests.  This module
only compares already-validated artifact records and never mutates either
input manifest or artifact list.
"""

from __future__ import annotations

from collections import Counter
from typing import Any
from uuid import UUID, uuid5


_CONTENT_FIELDS = ("sha256", "size", "mtime_ns")


def _source_uuid(source_id: str) -> UUID:
    """Parse a source UUID while keeping the public error a ValueError."""
    try:
        return UUID(str(source_id))
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("source_id must be a UUID") from error


def _artifacts(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("manifest artifacts must be a list")
    return artifacts


def _copy_artifact(artifact: dict[str, Any]) -> dict[str, Any]:
    """Copy a record so callers cannot observe mutation through this module."""
    return dict(artifact)


def _path_key(artifact: dict[str, Any]) -> str:
    return artifact["path"]


def _new_id(namespace: UUID, path: str, digest: str, used: set[str]) -> str:
    """Return the normal UUID5 identity, adding a deterministic collision salt."""
    identity = str(uuid5(namespace, f"{path}\0{digest}"))
    if identity not in used:
        return identity

    # A collision with a prior identity must not make a newly selected artifact
    # inherit that identity.  The suffix is deterministic and stays in the
    # source namespace while retaining the unsalted UUID5 in the usual case.
    attempt = 1
    while True:
        candidate = str(uuid5(namespace, f"{path}\0{digest}\0{attempt}"))
        if candidate not in used:
            return candidate
        attempt += 1


def assign_ids(
    source_id: str,
    artifacts: list[dict[str, Any]],
    previous: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Assign stable identities to a complete current artifact inventory.

    A path match always retains its previous identity, including when the
    artifact content or metadata changed.  For unmatched paths, a content hash
    is treated as a rename only when that hash occurs exactly once in each
    complete old and new inventory.  All other new artifacts receive a
    deterministic UUID5 identity.
    """
    namespace = _source_uuid(source_id)
    old: list[dict[str, Any]] = []
    if previous is not None:
        previous_source = _source_uuid(previous.get("source_id"))
        if previous_source != namespace:
            raise ValueError("previous manifest source_id does not match source_id")
        old = _artifacts(previous)

    old_by_path = {artifact["path"]: artifact for artifact in old}
    old_hashes = Counter(artifact["sha256"] for artifact in old)
    new_hashes = Counter(artifact["sha256"] for artifact in artifacts)
    new_paths = {artifact["path"] for artifact in artifacts}
    removed_by_hash = {
        candidate["sha256"]: candidate
        for candidate in old
        if candidate["path"] not in new_paths and old_hashes[candidate["sha256"]] == 1
    }
    used_ids = {
        artifact["artifact_id"]
        for artifact in old
        if "artifact_id" in artifact
    }

    output: list[dict[str, Any]] = []
    for artifact in sorted(artifacts, key=_path_key):
        path = artifact["path"]
        prior = old_by_path.get(path)
        assigned = _copy_artifact(artifact)

        if prior is not None:
            identity = prior["artifact_id"]
        else:
            rename_candidate = removed_by_hash.get(artifact["sha256"])
            if new_hashes[artifact["sha256"]] == 1 and rename_candidate is not None:
                identity = rename_candidate["artifact_id"]
            else:
                identity = _new_id(namespace, path, artifact["sha256"], used_ids)

        assigned["artifact_id"] = identity
        used_ids.add(identity)
        output.append(assigned)

    return output


def _assert_same_scope(previous: dict[str, Any], current: dict[str, Any]) -> None:
    previous_source = _source_uuid(previous.get("source_id"))
    current_source = _source_uuid(current.get("source_id"))
    if previous_source != current_source:
        raise ValueError("manifest source_id values do not match")


def _entry(before: dict[str, Any], after: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {"before": _copy_artifact(before), "after": _copy_artifact(after)}


def diff_manifests(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Return deterministic additions, content changes, renames, and removals."""
    if previous is None:
        added = [_copy_artifact(artifact) for artifact in sorted(_artifacts(current), key=_path_key)]
        return {"added": added, "changed": [], "renamed": [], "removed": []}

    _assert_same_scope(previous, current)
    old_by_id = {artifact["artifact_id"]: artifact for artifact in _artifacts(previous)}
    new_by_id = {artifact["artifact_id"]: artifact for artifact in _artifacts(current)}

    added = [
        _copy_artifact(new_by_id[identity])
        for identity in sorted(new_by_id.keys(), key=lambda value: _path_key(new_by_id[value]))
        if identity not in old_by_id
    ]
    removed = [
        _copy_artifact(old_by_id[identity])
        for identity in sorted(old_by_id.keys(), key=lambda value: _path_key(old_by_id[value]))
        if identity not in new_by_id
    ]

    changed: list[dict[str, dict[str, Any]]] = []
    renamed: list[dict[str, dict[str, Any]]] = []
    for identity in sorted(
        old_by_id.keys() & new_by_id.keys(),
        key=lambda value: (_path_key(new_by_id[value]), _path_key(old_by_id[value])),
    ):
        before = old_by_id[identity]
        after = new_by_id[identity]
        item = _entry(before, after)
        if any(before[field] != after[field] for field in _CONTENT_FIELDS):
            changed.append(item)
        if before["path"] != after["path"]:
            renamed.append(item)

    return {"added": added, "changed": changed, "renamed": renamed, "removed": removed}


__all__ = ["assign_ids", "diff_manifests"]
