"""Private SQLite text index with hash-checked, root-contained source references."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

from .extract import EXTRACTOR_VERSION, ExtractionError, extract

MAX_BYTES = 20 * 1024 * 1024
PIPELINE_VERSION = 'indexer-1:known-secrets-1:' + EXTRACTOR_VERSION
SUFFIXES = {'.html', '.htm', '.pdf', '.md', '.txt'}
STOPWORDS = set('the a an of to from and or in on with for is it that this where which find show slide slides page pages från till och eller i på med för är det den som hitta visa'.split())
SECRET_PATTERNS = tuple(re.compile(pattern, re.I) for pattern in (
    r'AKIA[0-9A-Z]{16}',
    r'aws_secret_access_key\s*[:=]\s*[\'\"]?[A-Za-z0-9/+=]{40}',
    r'gh[pousr]_[A-Za-z0-9]{36,255}',
    r'github_pat_[A-Za-z0-9]{20,}_[A-Za-z0-9]{50,}',
    r'xox[baprs]-[0-9A-Za-z-]{10,72}',
    r'sk_live_[0-9a-zA-Z]{16,}',
    r'AIza[0-9A-Za-z_-]{35}',
    r'-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----',
    r'eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}',
    r'(?:api[_-]?key|secret|token|password)\s*[:=]\s*[\'\"][A-Za-z0-9_\-/+]{16,}[\'\"]',
))


def has_secret(text: str) -> bool:
    """Known-format detector, not a guarantee that arbitrary secrets are absent."""
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


def safe_relative(value: str) -> str:
    if not isinstance(value, str) or not value or '\x00' in value or '\\' in value:
        raise ValueError('invalid_path')
    path = PurePosixPath(value)
    parts = value.split('/')
    if path.is_absolute() or any(part in {'', '.', '..'} for part in parts):
        raise ValueError('invalid_path')
    if any(part.startswith('.') or part.casefold() in {'secrets', 'credentials', 'node_modules'} for part in parts):
        raise ValueError('excluded_path')
    if path.suffix.lower() not in SUFFIXES:
        raise ValueError('unsupported_format')
    return str(path)


def read_source(root: Path, relative: str) -> tuple[bytes, os.stat_result]:
    """Open each directory with NOFOLLOW; bytes and digest share one snapshot."""
    parts = safe_relative(relative).split('/')
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, 'rb') as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_BYTES:
                raise ValueError('unsupported_or_oversized_source')
            data = source.read(MAX_BYTES + 1)
            after = os.fstat(source.fileno())
            if len(data) > MAX_BYTES or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError('source_changed_during_read')
            return data, after
    finally:
        os.close(directory)


def _outside(root: Path, db: Path) -> None:
    resolved = db.resolve()
    if resolved == root or root in resolved.parents:
        raise ValueError('Index must be outside the source archive')
    if db.is_symlink():
        raise ValueError('Index must not be a symlink')


def _metadata(connection: sqlite3.Connection, key: str) -> str:
    try:
        row = connection.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
    except sqlite3.DatabaseError:
        raise ValueError('Not an artifact-search index') from None
    if row is None:
        raise ValueError('Missing index metadata')
    return row[0]


def _open_index(root: Path, db: Path) -> sqlite3.Connection:
    _outside(root, db)
    db.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if db.parent.is_symlink() or db.parent.stat().st_mode & 0o077:
        raise ValueError('Index parent directory must be private (mode 0700)')
    new = not db.exists()
    if new:
        fd = os.open(db, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(fd)
    elif not stat.S_ISREG(db.stat().st_mode) or db.stat().st_mode & 0o077:
        raise ValueError('Index must be a private regular file (mode 0600)')
    connection = sqlite3.connect(db, timeout=10)
    connection.execute('PRAGMA foreign_keys=ON')
    connection.execute('PRAGMA trusted_schema=OFF')
    try:
        if new:
            connection.executescript('''
                CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE files(path TEXT PRIMARY KEY, sha256 TEXT NOT NULL,
                  size INTEGER NOT NULL, modified_ns INTEGER NOT NULL,
                  indexed_at TEXT NOT NULL, extractor_version TEXT NOT NULL);
                CREATE TABLE chunks(id INTEGER PRIMARY KEY, path TEXT NOT NULL
                  REFERENCES files(path) ON DELETE CASCADE, kind TEXT NOT NULL,
                  number INTEGER NOT NULL, anchor TEXT, title TEXT NOT NULL, text TEXT NOT NULL);
                CREATE VIRTUAL TABLE text_index USING fts5(title, text);
            ''')
            connection.executemany('INSERT INTO metadata VALUES (?, ?)', [('schema_version', '1'), ('root', str(root))])
            connection.commit()
        if _metadata(connection, 'schema_version') != '1' or _metadata(connection, 'root') != str(root):
            raise ValueError('Index version or source root mismatch')
        return connection
    except BaseException:
        connection.close()
        raise


def _remove(connection: sqlite3.Connection, path: str) -> None:
    connection.execute('DELETE FROM text_index WHERE rowid IN (SELECT id FROM chunks WHERE path=?)', (path,))
    connection.execute('DELETE FROM files WHERE path=?', (path,))


def index_corpus(root: Path, db: Path, paths: list[str]) -> dict:
    """A manifest is the complete selected corpus, not a partial update list."""
    root = Path(root).resolve(strict=True)
    if not root.is_dir() or not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        raise ValueError('Manifest must be an array of relative source paths')
    summary = dict(indexed_files=0, indexed_units=0, unchanged_files=0, removed_files=0, excluded_files=[])
    connection = _open_index(root, Path(db).absolute())
    try:
        with connection:
            existing = {row[0] for row in connection.execute('SELECT path FROM files')}
            retained = set()
            for supplied in sorted(set(paths)):
                path = None
                try:
                    path = safe_relative(supplied)
                    data, info = read_source(root, path)
                    digest = hashlib.sha256(data).hexdigest()
                    old = connection.execute('SELECT sha256,extractor_version FROM files WHERE path=?', (path,)).fetchone()
                    # Raw markup can contain a secret in a hidden script; fail before extraction.
                    if Path(path).suffix.lower() != '.pdf' and has_secret(data.decode('utf-8', errors='replace')):
                        raise ValueError('known_secret_format')
                    units = None
                    if old == (digest, PIPELINE_VERSION):
                        retained.add(path)
                        summary['unchanged_files'] += 1
                        continue
                    units = extract(data, Path(path).suffix.lower(), Path(path).name)
                    if has_secret('\n'.join(unit.text for unit in units)):
                        raise ValueError('known_secret_format')
                    units = [unit for unit in units if unit.text.strip()]
                    if not units:
                        raise ValueError('no_searchable_text')
                except (OSError, ValueError, ExtractionError) as error:
                    reason = str(error) if isinstance(error, ValueError) else ('extraction_failed' if isinstance(error, ExtractionError) else 'source_unavailable')
                    summary['excluded_files'].append({'path': supplied, 'reason': reason})
                    continue
                _remove(connection, path)
                now = datetime.now(timezone.utc).isoformat()
                connection.execute('INSERT INTO files VALUES (?,?,?,?,?,?)', (path, digest, len(data), info.st_mtime_ns, now, PIPELINE_VERSION))
                for unit in units:
                    title = unit.title or Path(path).stem
                    cursor = connection.execute('INSERT INTO chunks(path,kind,number,anchor,title,text) VALUES (?,?,?,?,?,?)', (path, unit.kind, unit.number, unit.anchor, title, unit.text))
                    connection.execute('INSERT INTO text_index(rowid,title,text) VALUES (?,?,?)', (cursor.lastrowid, title, unit.text))
                retained.add(path)
                summary['indexed_files'] += 1
                summary['indexed_units'] += len(units)
            for removed in sorted(existing - retained):
                _remove(connection, removed)
                summary['removed_files'] += 1
        return summary
    finally:
        connection.close()


def _match_query(query: str) -> str:
    words = re.findall(r'\w+', query.casefold(), re.UNICODE)
    words = list(dict.fromkeys(word for word in words if word not in STOPWORDS))[:64]
    return ' OR '.join('"' + word.replace('"', '""') + '"' for word in words)


def _reference(base_url: str | None, path: str, kind: str, number: int, anchor: str | None) -> str | None:
    if base_url is None:
        return None
    parsed = urlsplit(base_url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
        raise ValueError('Mimir base URL must be a credential-free HTTPS origin')
    url = base_url.rstrip('/') + '/files/' + quote(path, safe='/')
    if kind == 'page':
        url += '#page=' + str(number)
    elif anchor:
        url += '#' + quote(anchor, safe='')
    return url


def search(db: Path, query: str, top: int = 5, base_url: str | None = None) -> dict:
    if isinstance(top, bool) or not isinstance(top, int) or not 1 <= top <= 100 or not query.strip() or len(query) > 4096:
        raise ValueError('Query must be nonempty and top must be 1..100')
    _reference(base_url, 'probe.txt', 'section', 1, None)
    db = Path(db).absolute()
    if db.is_symlink() or db.stat().st_mode & 0o077:
        raise ValueError('Index must be private and not a symlink')
    connection = sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    output = dict(results=[], stale_candidates=0, unavailable_candidates=0)
    try:
        if _metadata(connection, 'schema_version') != '1':
            raise ValueError('Index version mismatch')
        root = Path(_metadata(connection, 'root'))
        _outside(root, db)
        match = _match_query(query)
        if not match:
            return output
        rows = connection.execute('''
          SELECT c.*, f.sha256, f.indexed_at, f.extractor_version,
            snippet(text_index,1,'','','…',32) AS snippet
          FROM text_index JOIN chunks c ON c.id=text_index.rowid JOIN files f ON f.path=c.path
          WHERE text_index MATCH ? ORDER BY bm25(text_index,3.0,1.0),c.path,c.number,c.id
        ''', (match,))
        checked = {}
        for row in rows:
            path = row['path']
            if path not in checked:
                try:
                    data, _ = read_source(root, path)
                    checked[path] = 'current' if hashlib.sha256(data).hexdigest() == row['sha256'] and row['extractor_version'] == PIPELINE_VERSION else 'stale'
                except (OSError, ValueError):
                    checked[path] = 'unavailable'
            state = checked[path]
            if state != 'current':
                output['stale_candidates' if state == 'stale' else 'unavailable_candidates'] += 1
                continue
            if len(output['results']) < top:
                output['results'].append(dict(path=path, sha256=row['sha256'], locator=dict(kind=row['kind'], number=row['number'], anchor=row['anchor']), title=row['title'], snippet=row['snippet'], mimir_url=_reference(base_url,path,row['kind'],row['number'],row['anchor']), indexed_at=row['indexed_at']))
        return output
    finally:
        connection.close()
