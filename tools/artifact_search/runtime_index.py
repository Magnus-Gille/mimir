"""Private, restartable SQLite FTS index for one immutable artifact snapshot.

This module deliberately owns only the index storage boundary.  Snapshot
acquisition, freshness, authentication, and lifecycle decisions belong to its
caller.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import time
from typing import Any

from .core import PIPELINE_VERSION, has_secret
from .extract import ExtractionError, extract


MAX_ARTIFACT_BYTES = 20 * 1024 * 1024
MAX_TOTAL_UNITS = 50_000
MAX_EXTRACTED_CHARS = 1_000_000
MAX_QUERY_CHARS = 4_096
MAX_TOP = 20
MAX_SNIPPET_CHARS = 512
MAX_TITLE_CHARS = 512
MAX_DB_PAGES = 131_072  # 512 MiB at SQLite's default 4 KiB page size.
SCHEMA_VERSION = "runtime-index-1"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
_HEX = re.compile(r"^[0-9a-f]{64}$")
_TOKEN = re.compile(r"\w+", re.UNICODE)


def _bounded_error(code: str, detail: str | None = None) -> ValueError:
    """Return a stable, non-sensitive error for storage/contract failures."""
    return ValueError(code if detail is None else f"{code}:{detail}")


def _canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _private_parent(path: Path, *, create: bool) -> Path:
    parent = path.parent
    try:
        parent_info = parent.lstat()
    except FileNotFoundError:
        parent_info = None
    except OSError as exc:
        raise _bounded_error("index_parent_unavailable") from exc
    if parent_info is not None and stat.S_ISLNK(parent_info.st_mode):
        raise _bounded_error("index_parent_symlink")
    if parent_info is None:
        if not create:
            raise _bounded_error("index_parent_missing")
        try:
            parent.mkdir(parents=True, mode=0o700)
        except OSError as exc:
            raise _bounded_error("index_parent_unavailable") from exc
    try:
        info = parent.lstat()
    except OSError as exc:
        raise _bounded_error("index_parent_unavailable") from exc
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
        raise _bounded_error("index_parent_not_private")
    return parent


def _check_path(path: str) -> str:
    if not isinstance(path, str) or not path or len(path) > 2048:
        raise _bounded_error("invalid_path")
    if "\\" in path or "\x00" in path or any(ord(char) < 32 or ord(char) == 127 for char in path):
        raise _bounded_error("invalid_path")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts) or PurePosixPath(path).is_absolute():
        raise _bounded_error("invalid_path")
    return str(PurePosixPath(path))


def _artifact_id(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise _bounded_error("invalid_artifact_id")
    return value


def _manifest_artifact(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise _bounded_error("invalid_artifact")
    artifact_id = _artifact_id(item.get("artifact_id"))
    path = _check_path(item.get("path"))
    size = item.get("size")
    sha256 = item.get("sha256")
    if type(size) is not int or not 0 <= size <= MAX_ARTIFACT_BYTES:
        raise _bounded_error("invalid_artifact")
    if not isinstance(sha256, str) or not _HEX.fullmatch(sha256):
        raise _bounded_error("invalid_artifact")
    return {"artifact_id": artifact_id, "path": path, "size": size, "sha256": sha256}


def _manifest_exclusion(item: Any) -> dict[str, str]:
    if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not isinstance(item.get("reason"), str):
        raise _bounded_error("invalid_exclusion")
    return {"path": _check_path(item["path"]), "reason": item["reason"]}


def _query_match(query: str) -> str:
    words: list[str] = []
    for word in _TOKEN.findall(query.casefold()):
        if word not in words:
            words.append(word)
        if len(words) >= 64:
            break
    # Quoted tokens keep FTS operators and punctuation from becoming syntax.
    return " OR ".join('"' + word.replace('"', '""') + '"' for word in words)


class SnapshotIndex:
    """A private SQLite FTS5 index bound to one snapshot and source identity."""

    def __init__(self, db: Path, snapshot_id: str, source_id: str, create: bool = False):
        self.db = Path(db).absolute()
        self.snapshot_id = snapshot_id
        self.source_id = source_id
        if not isinstance(snapshot_id, str) or not snapshot_id or len(snapshot_id) > 256:
            raise _bounded_error("invalid_snapshot_id")
        if not isinstance(source_id, str) or not source_id or len(source_id) > 256:
            raise _bounded_error("invalid_source_id")
        _private_parent(self.db, create=create)
        if self.db.is_symlink():
            raise _bounded_error("index_symlink")
        new = not self.db.exists()
        if not new:
            try:
                info = self.db.lstat()
            except OSError as exc:
                raise _bounded_error("index_unavailable") from exc
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise _bounded_error("index_not_private")
        elif not create:
            raise _bounded_error("index_missing")
        try:
            if new:
                fd = os.open(self.db, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                os.close(fd)
            self.connection = sqlite3.connect(self.db, timeout=5.0)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute("PRAGMA trusted_schema=OFF")
            self.connection.execute("PRAGMA busy_timeout=5000")
            self.connection.execute(f"PRAGMA max_page_count={MAX_DB_PAGES}")
            if new:
                self._create_schema()
            else:
                self._validate_metadata()
        except (sqlite3.DatabaseError, OSError) as exc:
            try:
                self.connection.close()
            except Exception:
                pass
            if isinstance(exc, sqlite3.DatabaseError):
                raise _bounded_error("index_corrupt") from None
            raise _bounded_error("index_unavailable") from exc

    def _create_schema(self) -> None:
        try:
            self.connection.executescript(
                """
                CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE files(
                    artifact_id TEXT PRIMARY KEY,
                    path TEXT NOT NULL UNIQUE,
                    sha256 TEXT NOT NULL,
                    size INTEGER NOT NULL
                );
                CREATE TABLE units(
                    id INTEGER PRIMARY KEY,
                    artifact_id TEXT NOT NULL REFERENCES files(artifact_id) ON DELETE CASCADE,
                    path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    number INTEGER NOT NULL,
                    anchor TEXT,
                    title TEXT NOT NULL,
                    text TEXT NOT NULL
                );
                CREATE TABLE exclusions(
                    path TEXT PRIMARY KEY,
                    artifact_id TEXT UNIQUE,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL
                );
                CREATE VIRTUAL TABLE search_fts USING fts5(title, text);
                CREATE INDEX units_artifact_idx ON units(artifact_id);
                """
            )
            self.connection.executemany(
                "INSERT INTO metadata(key,value) VALUES (?,?)",
                [
                    ("schema_version", SCHEMA_VERSION),
                    ("pipeline_version", PIPELINE_VERSION),
                    ("snapshot_id", self.snapshot_id),
                    ("source_id", self.source_id),
                    ("complete", "0"),
                ],
            )
            self.connection.commit()
        except sqlite3.DatabaseError as exc:
            raise _bounded_error("index_corrupt") from exc

    def _metadata(self, key: str) -> str:
        row = self.connection.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        if row is None:
            raise _bounded_error("index_metadata_missing")
        return str(row[0])

    def _validate_metadata(self) -> None:
        try:
            values = {key: self._metadata(key) for key in ("schema_version", "pipeline_version", "snapshot_id", "source_id", "complete")}
        except (sqlite3.DatabaseError, ValueError) as exc:
            if isinstance(exc, ValueError):
                raise
            raise _bounded_error("index_corrupt") from None
        if values["schema_version"] != SCHEMA_VERSION or values["pipeline_version"] != PIPELINE_VERSION:
            raise _bounded_error("index_version_mismatch")
        if values["snapshot_id"] != self.snapshot_id or values["source_id"] != self.source_id:
            raise _bounded_error("index_identity_mismatch")
        if values["complete"] not in {"0", "1"}:
            raise _bounded_error("index_metadata_invalid")

    def _is_complete(self) -> bool:
        return self._metadata("complete") == "1"

    def _run(self, callback):
        deadline = time.monotonic() + 5.0
        self.connection.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
        try:
            return callback()
        except sqlite3.OperationalError as exc:
            if "interrupted" in str(exc).lower() or "busy" in str(exc).lower() or "locked" in str(exc).lower():
                raise _bounded_error("index_operation_timeout") from None
            raise _bounded_error("index_operation_failed") from None
        except sqlite3.DatabaseError:
            raise _bounded_error("index_corrupt") from None
        finally:
            self.connection.set_progress_handler(None, 0)

    def _remove_artifact(self, artifact_id: str) -> None:
        rows = self.connection.execute("SELECT id FROM units WHERE artifact_id=?", (artifact_id,)).fetchall()
        for row in rows:
            self.connection.execute("DELETE FROM search_fts WHERE rowid=?", (row[0],))
        self.connection.execute("DELETE FROM units WHERE artifact_id=?", (artifact_id,))
        self.connection.execute("DELETE FROM files WHERE artifact_id=?", (artifact_id,))
        self.connection.execute("DELETE FROM exclusions WHERE artifact_id=?", (artifact_id,))

    def put(self, item: dict, data: bytes) -> None:
        """Atomically index one manifest artifact, or persist its exclusion."""
        if self._is_complete():
            raise _bounded_error("index_complete")
        record = _manifest_artifact(item)
        if not isinstance(data, bytes) or len(data) != record["size"] or hashlib.sha256(data).hexdigest() != record["sha256"]:
            raise _bounded_error("artifact_hash_mismatch", record["path"])

        def operation() -> None:
            existing = self.connection.execute(
                "SELECT artifact_id,path,sha256 FROM files WHERE artifact_id=? OR path=?",
                (record["artifact_id"], record["path"]),
            ).fetchall()
            old_exclusion = self.connection.execute(
                "SELECT artifact_id, path, reason FROM exclusions WHERE artifact_id=? OR path=?",
                (record["artifact_id"], record["path"]),
            ).fetchall()
            if any((row[0], row[1], row[2]) == (record["artifact_id"], record["path"], record["sha256"]) for row in existing) and old_exclusion == []:
                return
            self._remove_artifact(record["artifact_id"])
            # A manifest path is also unique.  Remove a stale row when a
            # caller retries the path with a newly assigned artifact identity.
            old_path = self.connection.execute("SELECT artifact_id FROM files WHERE path=?", (record["path"],)).fetchone()
            if old_path is not None:
                self._remove_artifact(str(old_path[0]))
            self.connection.execute("DELETE FROM exclusions WHERE path=?", (record["path"],))

            if has_secret(data.decode("utf-8", errors="replace")):
                self.connection.execute(
                    "INSERT INTO exclusions(path,artifact_id,reason,status) VALUES (?,?,?,?)",
                    (record["path"], record["artifact_id"], "known_secret_format", "excluded"),
                )
                return
            suffix = Path(record["path"]).suffix.lower()
            if suffix in IMAGE_SUFFIXES:
                self.connection.execute(
                    "INSERT INTO exclusions(path,artifact_id,reason,status) VALUES (?,?,?,?)",
                    (record["path"], record["artifact_id"], "unsupported_format", "excluded"),
                )
                return
            try:
                units = extract(data, suffix, Path(record["path"]).name)
            except ExtractionError as exc:
                raise _bounded_error("extraction_failed") from exc
            total_chars = sum(len(unit.title) + len(unit.text) for unit in units)
            if total_chars > MAX_EXTRACTED_CHARS:
                raise _bounded_error("extracted_text_limit", record["path"])
            units = [unit for unit in units if unit.text.strip() or unit.title.strip()]
            if has_secret("\n".join(f"{unit.title}\n{unit.text}" for unit in units)):
                self.connection.execute(
                    "INSERT INTO exclusions(path,artifact_id,reason,status) VALUES (?,?,?,?)",
                    (record["path"], record["artifact_id"], "known_secret_format", "excluded"),
                )
                return
            if not units:
                self.connection.execute(
                    "INSERT INTO exclusions(path,artifact_id,reason,status) VALUES (?,?,?,?)",
                    (record["path"], record["artifact_id"], "no_searchable_text", "excluded"),
                )
                return
            current_count = int(self.connection.execute("SELECT COUNT(*) FROM units").fetchone()[0])
            if current_count + len(units) > MAX_TOTAL_UNITS:
                raise _bounded_error("unit_limit")
            self.connection.execute(
                "INSERT INTO files(artifact_id,path,sha256,size) VALUES (?,?,?,?)",
                (record["artifact_id"], record["path"], record["sha256"], record["size"]),
            )
            for unit in units:
                title = (unit.title or Path(record["path"]).stem)[:MAX_TITLE_CHARS]
                text = unit.text[:MAX_EXTRACTED_CHARS]
                cursor = self.connection.execute(
                    "INSERT INTO units(artifact_id,path,sha256,kind,number,anchor,title,text) VALUES (?,?,?,?,?,?,?,?)",
                    (record["artifact_id"], record["path"], record["sha256"], unit.kind, int(unit.number), unit.anchor, title, text),
                )
                self.connection.execute("INSERT INTO search_fts(rowid,title,text) VALUES (?,?,?)", (cursor.lastrowid, title, text))

        try:
            def transaction() -> None:
                self.connection.execute("BEGIN")
                operation()
                self.connection.commit()
            self._run(transaction)
        except Exception:
            try:
                self.connection.rollback()
            except sqlite3.DatabaseError:
                pass
            raise

    def finish(self, manifest: dict) -> dict:
        """Validate the complete manifest set and atomically record completion."""
        if not isinstance(manifest, dict) or manifest.get("source_id") != self.source_id or manifest.get("complete") is not True:
            raise _bounded_error("manifest_mismatch")
        artifacts = [_manifest_artifact(item) for item in manifest.get("artifacts", [])]
        excluded = [_manifest_exclusion(item) for item in manifest.get("excluded", [])]
        artifact_ids = [item["artifact_id"] for item in artifacts]
        artifact_paths = [item["path"] for item in artifacts]
        all_paths = artifact_paths + [item["path"] for item in excluded]
        if len(set(artifact_ids)) != len(artifact_ids) or len(set(all_paths)) != len(all_paths):
            raise _bounded_error("manifest_mismatch")

        try:
            fingerprint = hashlib.sha256(_canonical_json(manifest)).hexdigest()
        except (TypeError, ValueError, OverflowError):
            raise _bounded_error("manifest_mismatch") from None

        def operation() -> dict:
            if self._is_complete():
                if self._metadata("manifest_hash") != fingerprint:
                    raise _bounded_error("manifest_mismatch")
                result = self.stats()
                self.connection.commit()
                return result
            for entry in excluded:
                existing = self.connection.execute("SELECT artifact_id,reason FROM exclusions WHERE path=?", (entry["path"],)).fetchone()
                if existing is None:
                    self.connection.execute("INSERT INTO exclusions(path,artifact_id,reason,status) VALUES (?,?,?,?)", (entry["path"], None, entry["reason"], "manifest_excluded"))
                elif existing[0] is not None or existing[1] != entry["reason"]:
                    raise _bounded_error("manifest_mismatch")

            rows = self.connection.execute("SELECT artifact_id,path,sha256,size FROM files").fetchall()
            file_by_id = {row[0]: row for row in rows}
            exclusion_rows = self.connection.execute("SELECT artifact_id,path FROM exclusions WHERE artifact_id IS NOT NULL").fetchall()
            exclusion_by_id = {row[0]: row for row in exclusion_rows}
            if set(file_by_id) | set(exclusion_by_id) != set(artifact_ids) or set(file_by_id) & set(exclusion_by_id):
                raise _bounded_error("manifest_mismatch")
            for item in artifacts:
                row = file_by_id.get(item["artifact_id"])
                if row is not None:
                    if (row[1], row[2], row[3]) != (item["path"], item["sha256"], item["size"]):
                        raise _bounded_error("manifest_mismatch")
                else:
                    row = exclusion_by_id[item["artifact_id"]]
                    if row[1] != item["path"]:
                        raise _bounded_error("manifest_mismatch")
            current_paths = {row[1] for row in rows} | {row[1] for row in exclusion_rows}
            current_paths |= {row[0] for row in self.connection.execute("SELECT path FROM exclusions WHERE artifact_id IS NULL")}
            if current_paths != set(all_paths):
                raise _bounded_error("manifest_mismatch")
            self.connection.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES ('manifest_hash',?)", (fingerprint,))
            self.connection.execute("UPDATE metadata SET value='1' WHERE key='complete'")
            self.connection.commit()
            return self.stats()

        try:
            def transaction() -> dict:
                self.connection.execute("BEGIN")
                return operation()
            return self._run(transaction)
        except Exception:
            try:
                self.connection.rollback()
            except sqlite3.DatabaseError:
                pass
            raise

    def completed(self) -> set[str]:
        try:
            rows = self.connection.execute("SELECT artifact_id FROM files UNION SELECT artifact_id FROM exclusions WHERE artifact_id IS NOT NULL").fetchall()
            return {str(row[0]) for row in rows}
        except sqlite3.DatabaseError:
            raise _bounded_error("index_corrupt") from None

    def stats(self) -> dict:
        try:
            file_count, unit_count = self.connection.execute("SELECT (SELECT COUNT(*) FROM files), (SELECT COUNT(*) FROM units)").fetchone()
            excluded_count = self.connection.execute("SELECT COUNT(*) FROM exclusions").fetchone()[0]
            return {"file_count": int(file_count), "unit_count": int(unit_count), "excluded_count": int(excluded_count), "complete": self._is_complete()}
        except sqlite3.DatabaseError:
            raise _bounded_error("index_corrupt") from None

    def search(self, query: str, top: int = 5) -> list[dict]:
        if not isinstance(query, str) or len(query) > MAX_QUERY_CHARS or not query.strip():
            raise _bounded_error("invalid_query")
        if isinstance(top, bool) or not isinstance(top, int) or not 1 <= top <= MAX_TOP:
            raise _bounded_error("invalid_top")
        match = _query_match(query)
        if not match:
            return []

        def operation() -> list[dict]:
            rows = self.connection.execute(
                """
                SELECT u.path, u.artifact_id, u.sha256, u.kind, u.number, u.anchor,
                       u.title, u.text, snippet(search_fts, 1, '[', ']', '…', 48) AS excerpt,
                       bm25(search_fts) AS score
                FROM search_fts JOIN units u ON u.id=search_fts.rowid
                WHERE search_fts MATCH ?
                ORDER BY score ASC, u.path ASC, u.number ASC, u.id ASC LIMIT ?
                """,
                (match, top),
            ).fetchall()
            results = []
            for row in rows:
                snippet = (row[8] or row[7])[:MAX_SNIPPET_CHARS]
                results.append({
                    "path": row[0],
                    "artifact_id": row[1],
                    "sha256": row[2],
                    "locator": {"kind": row[3], "number": int(row[4]), "anchor": row[5]},
                    "title": row[6][:MAX_TITLE_CHARS],
                    "snippet": snippet,
                    "score": float(row[9]),
                })
            return results

        return self._run(operation)

    def close(self) -> None:
        try:
            self.connection.close()
        except sqlite3.DatabaseError:
            raise _bounded_error("index_close_failed") from None


__all__ = ["SnapshotIndex"]
