"""Pure vector and cache primitives for the artifact-search prototype.

This module deliberately has no model, filesystem, network, or environment
dependencies.  Embeddings are supplied by the caller and cache transactions
remain under the caller's control.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import numbers
import sqlite3
from typing import Iterable


SUPPORTED_DIMENSIONS = frozenset({128, 256, 512, 768})
DEFAULT_DIMENSIONS = 256
DEFAULT_MODEL = "embeddinggemma-2"
MAX_REVISION_LENGTH = 128
MAX_TITLE_LENGTH = 512
MAX_PIPELINE_VERSION_LENGTH = 128
SEGMENTATION_VERSION = "segments-v1"
NORMALIZATION_VERSION = "l2-v1"
DOCUMENT_PREFIX = "title: "
QUERY_PREFIX = "task: search result | query: "


def _require_string(value: object, name: str, *, max_length: int | None = None) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if max_length is not None and len(value) > max_length:
        raise ValueError(f"{name} is too long")
    return value


def _require_dimensions(dimensions: object, *, supported_only: bool = False) -> int:
    if isinstance(dimensions, bool) or not isinstance(dimensions, int):
        raise TypeError("dimensions must be an integer")
    if dimensions <= 0:
        raise ValueError("dimensions must be positive")
    if supported_only and dimensions not in SUPPORTED_DIMENSIONS:
        raise ValueError("dimensions must be one of 128, 256, 512, or 768")
    return dimensions


@dataclass(frozen=True)
class EmbeddingIdentity:
    """The model identity that makes a vector cache entry reusable safely."""

    revision: str
    dimensions: int = DEFAULT_DIMENSIONS
    model: str = DEFAULT_MODEL

    def __post_init__(self) -> None:
        revision = _require_string(self.revision, "revision", max_length=MAX_REVISION_LENGTH)
        if not revision.strip():
            raise ValueError("revision must be nonempty")
        _require_dimensions(self.dimensions, supported_only=True)
        if self.model != DEFAULT_MODEL:
            raise ValueError(f"model must be exactly {DEFAULT_MODEL!r}")


def document_segments(title: str, text: str, max_chars: int = 4000) -> list[str]:
    """Split document text into nonempty, lossless embedding inputs."""

    title = _require_string(title, "title")[:MAX_TITLE_LENGTH]
    text = _require_string(text, "text")
    if isinstance(max_chars, bool) or not isinstance(max_chars, int):
        raise TypeError("max_chars must be an integer")
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")

    title_value = title or "none"
    return [
        f"{DOCUMENT_PREFIX}{title_value} | text: {text[start:start + max_chars]}"
        for start in range(0, len(text), max_chars)
        if text[start:start + max_chars]
    ]


def query_input(query: str) -> str:
    """Format a query using the embedding model's task prefix."""

    return QUERY_PREFIX + _require_string(query, "query")


def _coerce_vector(vector: object, dimensions: int) -> list[float]:
    _require_dimensions(dimensions)
    if not isinstance(vector, list):
        raise TypeError("vector must be a list")
    if len(vector) != dimensions:
        raise ValueError("vector has the wrong number of dimensions")

    values: list[float] = []
    for item in vector:
        if isinstance(item, bool) or not isinstance(item, numbers.Real):
            raise TypeError("vector values must be real numbers")
        try:
            value = float(item)
        except OverflowError:  # e.g. a JSON integer such as 10**400
            raise ValueError("vector values must be finite") from None
        if not math.isfinite(value):
            raise ValueError("vector values must be finite")
        values.append(value)
    return values


def _norm(values: Iterable[float]) -> float:
    """Compute an L2 norm with scaling to avoid intermediate overflow."""

    values = list(values)
    scale = max((abs(value) for value in values), default=0.0)
    if scale == 0.0:
        return 0.0
    return scale * math.sqrt(sum((value / scale) ** 2 for value in values))


def _unit(values: list[float]) -> list[float]:
    """Normalize finite values without forming an overflowing raw norm."""

    scale = max((abs(value) for value in values), default=0.0)
    if scale == 0.0:
        raise ValueError("zero vector cannot be normalized")
    scaled = [value / scale for value in values]
    scaled_norm = math.sqrt(sum(value * value for value in scaled))
    return [value / scaled_norm for value in scaled]


def normalize(vector: list, dimensions: int) -> list[float]:
    """Return an L2-normalized vector after validating its shape and values."""

    values = _coerce_vector(vector, dimensions)
    return _unit(values)


def cosine(a: list, b: list) -> float:
    """Return cosine similarity, validating both vectors in the process."""

    if not isinstance(a, list) or not isinstance(b, list):
        raise TypeError("vectors must be lists")
    if len(a) != len(b):
        raise ValueError("vectors must have the same dimensions")
    if not a:
        raise ValueError("vectors must not be empty")
    dimensions = len(a)
    left = _coerce_vector(a, dimensions)
    right = _coerce_vector(b, dimensions)
    try:
        left_unit = _unit(left)
        right_unit = _unit(right)
    except ValueError as exc:
        raise ValueError("cosine is undefined for a zero vector") from exc
    return sum(x * y for x, y in zip(left_unit, right_unit))


def cache_key(
    identity: EmbeddingIdentity,
    title: str,
    text: str,
    pipeline_version: str,
) -> str:
    """Build a content-only key for document segment vectors."""

    if not isinstance(identity, EmbeddingIdentity):
        raise TypeError("identity must be an EmbeddingIdentity")
    title = _require_string(title, "title")[:MAX_TITLE_LENGTH]
    text = _require_string(text, "text")
    pipeline_version = _require_string(
        pipeline_version,
        "pipeline_version",
        max_length=MAX_PIPELINE_VERSION_LENGTH,
    )
    payload = {
        "cache_version": "artifact-vectors-v1",
        "model": identity.model,
        "revision": identity.revision,
        "dimensions": identity.dimensions,
        "document_prefix": DOCUMENT_PREFIX,
        "query_prefix": QUERY_PREFIX,
        "normalization": NORMALIZATION_VERSION,
        "segmentation": SEGMENTATION_VERSION,
        "pipeline": pipeline_version,
        "title": title,
        "text": text,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def create_schema(conn: sqlite3.Connection) -> None:
    """Create the vector cache table if it does not exist."""

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS artifact_vectors (
            key TEXT PRIMARY KEY,
            dimensions INTEGER NOT NULL,
            vectors TEXT NOT NULL
        )
        """
    )


def _validated_cached_vectors(raw: object, dimensions: int) -> list[list[float]] | None:
    if not isinstance(raw, str):
        return None
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(decoded, list) or not decoded:
        return None

    result: list[list[float]] = []
    for vector in decoded:
        try:
            values = _coerce_vector(vector, dimensions)
        except (TypeError, ValueError):
            return None
        norm = _norm(values)
        if norm == 0.0 or not math.isclose(norm, 1.0, rel_tol=1e-5, abs_tol=1e-6):
            return None
        result.append(values)
    return result


def get_cached(
    conn: sqlite3.Connection,
    key: str,
    dimensions: int,
) -> list[list[float]] | None:
    """Read and validate a cache row; malformed rows are treated as misses."""

    _require_string(key, "key")
    _require_dimensions(dimensions)
    row = conn.execute(
        "SELECT dimensions, vectors FROM artifact_vectors WHERE key = ?",
        (key,),
    ).fetchone()
    if row is None or row[0] != dimensions:
        return None
    return _validated_cached_vectors(row[1], dimensions)


def put_cached(
    conn: sqlite3.Connection,
    key: str,
    vectors: list[list[float]],
    dimensions: int,
) -> None:
    """Validate and upsert vectors without committing the caller's transaction."""

    _require_string(key, "key")
    _require_dimensions(dimensions)
    if not isinstance(vectors, list):
        raise TypeError("vectors must be a list")
    if not vectors:
        raise ValueError("vectors must be nonempty")
    normalized = [normalize(vector, dimensions) for vector in vectors]
    encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    conn.execute(
        """
        INSERT INTO artifact_vectors(key, dimensions, vectors)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            dimensions = excluded.dimensions,
            vectors = excluded.vectors
        """,
        (key, dimensions, encoded),
    )


def reciprocal_rank_fusion(
    lexical: list[int],
    semantic: list[int],
    constant: int = 60,
) -> dict[int, float]:
    """Combine two ranked ID lists, counting each list's duplicate once."""

    if isinstance(constant, bool) or not isinstance(constant, int):
        raise TypeError("constant must be an integer")
    if constant < 0:
        raise ValueError("constant must be nonnegative")

    scores: dict[int, float] = {}
    for ranked in (lexical, semantic):
        if not isinstance(ranked, list):
            raise TypeError("ranked results must be lists")
        seen: set[int] = set()
        for rank, item in enumerate(ranked, 1):
            if isinstance(item, bool) or not isinstance(item, int):
                raise TypeError("result IDs must be integers")
            if item in seen:
                continue
            seen.add(item)
            scores[item] = scores.get(item, 0.0) + 1.0 / (constant + rank)
    return scores


__all__ = [
    "EmbeddingIdentity",
    "document_segments",
    "query_input",
    "normalize",
    "cosine",
    "cache_key",
    "create_schema",
    "get_cached",
    "put_cached",
    "reciprocal_rank_fusion",
]
