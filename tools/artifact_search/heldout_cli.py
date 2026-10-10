"""Reproducible JSON CLI for the approved held-out artifact-search set."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any

from . import core
from .embedding_client import EmbedderUnavailable, M5Embedder
from .heldout import HeldoutError, run_heldout
from .semantic import semantic_search
from .vectors import EmbeddingIdentity, SUPPORTED_DIMENSIONS


_EMBEDDER_REASONS = frozenset(
    {
        "embedding_gateway_revision_mismatch",
        "embedding_http_error",
        "embedding_invalid_json",
        "embedding_invalid_or_unavailable",
        "embedding_invalid_response",
        "embedding_response_too_large",
        "embedding_transport_error",
    }
)


def _validate_top(top: object) -> int:
    if isinstance(top, bool) or not isinstance(top, int) or not 1 <= top <= 100:
        raise ValueError("invalid_top")
    return top


def _validate_min_score(min_score: object) -> float:
    # Do not let NaN or infinity reach semantic_search or JSON output.
    import math

    if isinstance(min_score, bool) or not isinstance(min_score, (int, float)):
        raise ValueError("invalid_min_score")
    value = float(min_score)
    if not math.isfinite(value) or not -1.0 <= value <= 1.0:
        raise ValueError("invalid_min_score")
    return value


def run_cli(
    labels: Path,
    snapshot_manifest: Path,
    calibration: Path,
    expected_label_sha256: str,
    db: Path,
    *,
    top: int = 5,
    mode: str = "lexical",
    embedding_revision: str | None = None,
    dimensions: int = 256,
    min_score: float = 0.0,
) -> dict[str, Any]:
    """Run held-out evaluation with all model construction deferred to search."""

    if mode not in {"lexical", "vector", "hybrid"}:
        raise ValueError("invalid_mode")
    top = _validate_top(top)
    min_score = _validate_min_score(min_score)
    if mode != "lexical":
        if not isinstance(embedding_revision, str) or not embedding_revision:
            raise ValueError("embedding_revision_required")
        if dimensions not in SUPPORTED_DIMENSIONS:
            raise ValueError("invalid_dimensions")

    # The callback is intentionally the only place where search/index work and
    # M5 construction can occur. run_heldout performs every label/snapshot
    # preflight before evaluate invokes this callback.
    embedder: M5Embedder | None = None
    identity: EmbeddingIdentity | None = None

    def search_fn(search_db: Path, query: str, search_top: int) -> dict[str, Any]:
        nonlocal embedder, identity
        if mode == "lexical":
            return core.search(search_db, query, search_top)
        if identity is None:
            identity = EmbeddingIdentity(embedding_revision, dimensions)  # validated above
            embedder = M5Embedder.from_env(identity)
        return semantic_search(
            search_db,
            query,
            embedder,
            identity,
            top=search_top,
            mode=mode,
            min_score=min_score,
        )

    report = run_heldout(
        labels,
        snapshot_manifest,
        db,
        calibration_path=calibration,
        expected_label_sha256=expected_label_sha256,
        search_fn=search_fn,
        top=top,
    )
    # run_heldout's result modes are the effective modes returned by each
    # search. Keep its aggregate ``mode`` for compatibility, while making the
    # requested mode explicit so hybrid lexical fallback is never mislabelled.
    report = dict(report)
    report["requested_mode"] = mode
    report["effective_modes"] = {
        item["case_id"]: item["mode"] for item in report["results"]
    }
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--snapshot-manifest", type=Path, required=True)
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument("--expected-label-sha256", required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--mode", choices=("lexical", "vector", "hybrid"), default="lexical")
    parser.add_argument("--embedding-revision")
    parser.add_argument("--dimensions", type=int, default=256, choices=sorted(SUPPORTED_DIMENSIONS))
    parser.add_argument("--min-score", type=float, default=0.0)
    return parser


def _diagnostic(error: BaseException) -> dict[str, Any]:
    if isinstance(error, HeldoutError):
        return {"error": str(error)}
    if isinstance(error, EmbedderUnavailable):
        reason = str(error)
        payload: dict[str, Any] = {
            "error": reason if reason in _EMBEDDER_REASONS else "embedding_unavailable",
            "http_status": error.status,
            "retry_after": error.retry_after,
        }
        return payload
    if isinstance(error, ValueError):
        code = str(error)
        safe_codes = {
            "embedding_revision_required",
            "invalid_dimensions",
            "invalid_min_score",
            "invalid_mode",
            "invalid_top",
        }
        return {"error": code if code in safe_codes else "evaluation_invalid"}
    if isinstance(error, sqlite3.Error):
        return {"error": "index_unavailable"}
    if isinstance(error, OSError):
        return {"error": "evaluation_io_error"}
    if isinstance(error, TypeError):
        return {"error": "evaluation_invalid"}
    return {"error": "evaluation_failed"}


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = run_cli(
            args.labels,
            args.snapshot_manifest,
            args.calibration,
            args.expected_label_sha256,
            args.db,
            top=args.top,
            mode=args.mode,
            embedding_revision=args.embedding_revision,
            dimensions=args.dimensions,
            min_score=args.min_score,
        )
    except Exception as error:  # Keep the JSON CLI free of tracebacks and source text.
        print(json.dumps(_diagnostic(error), ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


run_heldout_cli = run_cli


if __name__ == "__main__":
    raise SystemExit(main())
