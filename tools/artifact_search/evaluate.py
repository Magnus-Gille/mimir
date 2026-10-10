"""Small, deterministic lexical-search benchmark helper."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
import time
from typing import Any


def evaluate(db: Path, cases: list[dict], top: int = 5, search_fn=None) -> dict:
    """Evaluate search results without returning query or source-content data."""
    _validate_cases(cases)
    if isinstance(top, bool) or not isinstance(top, int) or top <= 0 or top > 100:
        raise ValueError("top must be an integer from 1 through 100")
    if search_fn is not None and not callable(search_fn):
        raise TypeError("search_fn must be callable")

    if search_fn is None:
        core_name = f"{__package__}.core" if __package__ else "core"
        core = import_module(core_name)
        search_fn = core.search
    started = time.perf_counter()
    results: list[dict[str, Any]] = []
    successful = 0
    positive_count = 0
    positive_hits = 0
    negative_hits = 0
    reciprocal_total = 0.0

    for case in cases:
        case_started = time.perf_counter()
        search_output = search_fn(db, case["query"], top)
        if isinstance(search_output, dict):
            hits = search_output.get("results", [])
            mode = search_output.get("mode", "lexical")
        else:
            hits = search_output
            mode = "lexical"
        if hits is None:
            hits = []
        else:
            hits = list(hits)[:top]
        expected = case["expected"]
        rank = _matching_rank(hits, expected) if expected else None
        hit = rank is not None if expected else not hits
        if hit:
            successful += 1
        if expected:
            positive_count += 1
            if rank is not None:
                positive_hits += 1
                reciprocal_total += 1.0 / rank
        elif hit:
            negative_hits += 1
        results.append(
            {
                "label": case["label"],
                "hit": hit,
                "rank": rank,
                "mode": mode,
                "elapsed_ms": _elapsed_ms(case_started),
            }
        )

    case_count = len(cases)
    return {
        "case_count": case_count,
        "positive_case_count": positive_count,
        "hit_at_k": positive_hits / positive_count if positive_count else 0.0,
        "case_pass_rate": successful / case_count,
        "negative_case_count": case_count - positive_count,
        "negative_pass_rate": negative_hits / (case_count - positive_count) if case_count > positive_count else None,
        "mean_reciprocal_rank": reciprocal_total / positive_count if positive_count else 0.0,
        "top": top,
        "elapsed_ms": _elapsed_ms(started),
        "results": results,
    }


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 3)


def _validate_cases(cases: object) -> None:
    if not isinstance(cases, list) or not cases:
        raise ValueError("cases must be a non-empty list")
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("each case must be an object")
        if not isinstance(case.get("label"), str) or not isinstance(case.get("query"), str):
            raise ValueError("each case needs string label and query")
        expected = case.get("expected")
        if not isinstance(expected, list):
            raise ValueError("each case needs an expected list")
        for item in expected:
            if not isinstance(item, dict):
                raise ValueError("each expected item must be an object")
            if not isinstance(item.get("path"), str) or not isinstance(item.get("kind"), str):
                raise ValueError("expected items need string path and kind")
            if isinstance(item.get("number"), bool) or not isinstance(item.get("number"), int):
                raise ValueError("expected items need an integer number")
            if item['number'] < 1 or item['kind'] not in {'slide', 'page', 'section'}:
                raise ValueError('expected locations need a supported kind and 1-based number')


def _field(value: object, name: str, default: object = None) -> object:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _matches(hit: object, expected: dict) -> bool:
    locator = _field(hit, "locator")
    return (
        _field(hit, "path") == expected["path"]
        and _field(locator, "kind") == expected["kind"]
        and _field(locator, "number") == expected["number"]
    )


def _matching_rank(hits: list[object], expected: list[dict]) -> int | None:
    for rank, hit in enumerate(hits, 1):
        if any(_matches(hit, item) for item in expected):
            return rank
    return None


class _MemoizedEmbedder:
    """Share and safely copy query embeddings across compare evaluations."""

    def __init__(self, embedder, identity):
        if embedder.identity != identity:
            raise ValueError('Embedding identity mismatch')
        self._embedder = embedder
        self.identity = identity
        self._cache: dict[tuple[str, ...], list[list[float]]] = {}
        self.calls = 0

    def embed(self, inputs):
        from .vectors import normalize

        if not isinstance(inputs, list) or not inputs or not all(isinstance(item, str) for item in inputs):
            raise ValueError("Embedding input must be a nonempty list of strings")
        cache_key = tuple(inputs)
        cached = self._cache.get(cache_key)
        if cached is None:
            raw = self._embedder.embed(list(inputs))
            if not isinstance(raw, list) or len(raw) != len(inputs):
                raise ValueError("Embedding count mismatch")
            validated = [normalize(vector, self.identity.dimensions) for vector in raw]
            self._cache[cache_key] = [list(vector) for vector in validated]
            self.calls += 1
            cached = self._cache[cache_key]
        return [list(vector) for vector in cached]


def compare(db, cases: list[dict], embedder, identity, top: int = 5, min_score: float = 0.0) -> dict:
    """Compare lexical, vector, and hybrid retrieval on one shared query cache."""

    memoized = _MemoizedEmbedder(embedder, identity)
    reports = {
        "lexical": evaluate(db, cases, top=top),
    }
    semantic_name = f"{__package__}.semantic" if __package__ else "semantic"
    semantic = import_module(semantic_name)

    def vector_search(search_db, query, search_top):
        return semantic.semantic_search(
            search_db,
            query,
            memoized,
            identity,
            top=search_top,
            mode="vector",
            min_score=min_score,
        )

    def hybrid_search(search_db, query, search_top):
        return semantic.semantic_search(
            search_db,
            query,
            memoized,
            identity,
            top=search_top,
            mode="hybrid",
            min_score=min_score,
        )

    reports["vector"] = evaluate(db, cases, top=top, search_fn=vector_search)
    reports["hybrid"] = evaluate(db, cases, top=top, search_fn=hybrid_search)
    return {
        **reports,
        "query_embedding_calls": memoized.calls,
        "shared_query_embeddings": True,
        "cosine_floor": min_score,
    }
