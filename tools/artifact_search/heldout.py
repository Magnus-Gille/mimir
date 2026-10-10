"""Strict, offline validation and execution for held-out search labels.

This module deliberately wraps :func:`evaluate.evaluate` instead of changing
its scoring contract.  Labels are immutable inputs: validation binds every
expected location to one complete #41 snapshot, and execution requires an
operator supplied byte digest plus an approved reviewer distinct from the
author.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable, Mapping
import uuid

from .evaluate import evaluate
from .snapshot import load_manifest


SCHEMA = "mimir-artifact-evaluation-v1"
REPORT_SCHEMA = "mimir-artifact-evaluation-report-v1"
CASE_TYPES = frozenset({"positive", "miss", "distractor"})
LOCATOR_KINDS = frozenset({"slide", "page", "section"})
REPORT_MODES = frozenset({"lexical", "vector", "hybrid", "lexical_fallback"})
SHA256 = re.compile(r"^[0-9a-f]{64}$")

_TOP_LEVEL = frozenset(
    {
        "schema",
        "corpus_snapshot_id",
        "corpus_source_id",
        "author",
        "reviewer",
        "status",
        "created_at",
        "frozen_at",
        "cases",
    }
)
_CASE_FIELDS = frozenset({"id", "query", "type", "expected", "provenance"})
_EXPECTED_FIELDS = frozenset({"path", "kind", "number", "sha256"})
_PROVENANCE_FIELDS = frozenset({"source_task", "evidence"})


class HeldoutError(ValueError):
    """A label, snapshot, digest, or split contract was not satisfied."""


def _error(code: str) -> HeldoutError:
    # Keep diagnostics independent of query and source content.
    return HeldoutError(code)


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _error("duplicate_json_key")
        result[key] = value
    return result


def _canonical(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _parse_bytes(data: bytes) -> dict[str, Any]:
    if not isinstance(data, bytes):
        raise _error("invalid_label_bytes")
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, HeldoutError):
        raise _error("invalid_label_json") from None
    if not isinstance(value, dict):
        raise _error("invalid_label_object")
    try:
        canonical = _canonical(value)
    except (TypeError, ValueError, OverflowError):
        raise _error("invalid_label_json") from None
    if canonical != data:
        raise _error("noncanonical_label")
    return value


def _text(value: object, code: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise _error(code)
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise _error(code) from None
    return value


def _sha(value: object, code: str = "invalid_sha256") -> str:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise _error(code)
    return value


def _uuid(value: object, code: str) -> str:
    if not isinstance(value, str):
        raise _error(code)
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise _error(code) from None
    if str(parsed) != value:
        raise _error(code)
    return value


def _timestamp(value: object, code: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    value = _text(value, code)
    # RFC 3339 with an explicit UTC offset prevents ambiguous frozen labels.
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _error(code) from None
    if parsed.tzinfo is None:
        raise _error(code)
    return value


def _validate_expected(expected: object) -> list[dict[str, Any]]:
    if not isinstance(expected, list):
        raise _error("expected_must_be_list")
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()
    for item in expected:
        if not isinstance(item, dict) or set(item) != _EXPECTED_FIELDS:
            raise _error("invalid_expected_locator")
        path = _text(item.get("path"), "invalid_locator_path")
        kind = _text(item.get("kind"), "unsupported_locator")
        number = item.get("number")
        if kind not in LOCATOR_KINDS:
            raise _error("unsupported_locator")
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise _error("invalid_locator_number")
        digest = _sha(item.get("sha256"))
        identity = (path, kind, number)
        if identity in seen:
            raise _error("duplicate_expected_locator")
        seen.add(identity)
        normalized.append({"path": path, "kind": kind, "number": number, "sha256": digest})
    return normalized


def _validate_cases(cases: object) -> tuple[list[dict[str, Any]], set[str]]:
    if not isinstance(cases, list) or not cases:
        raise _error("cases_must_be_nonempty")
    normalized: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    for case in cases:
        if not isinstance(case, dict) or set(case) != _CASE_FIELDS:
            raise _error("invalid_case")
        case_id = _text(case.get("id"), "invalid_case_id")
        if case_id in identifiers:
            raise _error("duplicate_case_id")
        identifiers.add(case_id)
        query = _text(case.get("query"), "empty_query")
        case_type = _text(case.get("type"), "unknown_case_type")
        if case_type not in CASE_TYPES:
            raise _error("unknown_case_type")
        expected = _validate_expected(case.get("expected"))
        if case_type == "miss" and expected:
            raise _error("miss_has_answers")
        if case_type == "positive" and not expected:
            raise _error("positive_has_no_answers")
        if case_type == "distractor" and not expected:
            raise _error("distractor_has_no_answers")
        provenance = case.get("provenance")
        if not isinstance(provenance, dict) or set(provenance) != _PROVENANCE_FIELDS:
            raise _error("invalid_provenance")
        source_task = _text(provenance.get("source_task"), "invalid_provenance")
        evidence = _text(provenance.get("evidence"), "invalid_provenance")
        normalized.append(
            {
                "id": case_id,
                "query": query,
                "type": case_type,
                "expected": expected,
                "provenance": {"source_task": source_task, "evidence": evidence},
            }
        )
    return normalized, identifiers


def _snapshot_artifacts(snapshot_manifest: Mapping[str, Any]) -> dict[str, str]:
    if (
        snapshot_manifest.get("schema") != "mimir-artifact-snapshot-v1"
        or snapshot_manifest.get("complete") is not True
        or not isinstance(snapshot_manifest.get("artifacts"), list)
    ):
        raise _error("incomplete_snapshot")
    result: dict[str, str] = {}
    for artifact in snapshot_manifest["artifacts"]:
        if not isinstance(artifact, dict) or not isinstance(artifact.get("path"), str):
            raise _error("invalid_snapshot")
        path = artifact["path"]
        digest = _sha(artifact.get("sha256"), "invalid_snapshot")
        if path in result:
            raise _error("invalid_snapshot")
        result[path] = digest
    return result


def _bind_expected_to_snapshot(cases: list[dict[str, Any]], snapshot_manifest: Mapping[str, Any]) -> None:
    artifacts = _snapshot_artifacts(snapshot_manifest)
    for case in cases:
        for expected in case["expected"]:
            if expected["path"] not in artifacts or artifacts[expected["path"]] != expected["sha256"]:
                raise _error("expected_snapshot_mismatch")


def validate_evaluation(
    value: Mapping[str, Any],
    *,
    snapshot_manifest: Mapping[str, Any] | None = None,
    snapshot_id: str | None = None,
    source_id: str | None = None,
    require_approved: bool = False,
) -> dict[str, Any]:
    """Validate and return a detached, normalized evaluation object.

    Draft labels are valid inputs to this function.  ``require_approved`` is
    reserved for held-out execution, where reviewer independence is required.
    """

    if not isinstance(value, Mapping) or set(value) != _TOP_LEVEL:
        raise _error("invalid_evaluation_schema")
    if value.get("schema") != SCHEMA:
        raise _error("unsupported_evaluation_schema")
    label_snapshot_id = _sha(value.get("corpus_snapshot_id"), "invalid_snapshot_id")
    label_source_id = _uuid(value.get("corpus_source_id"), "invalid_source_id")
    if snapshot_id is not None and label_snapshot_id != _sha(snapshot_id, "snapshot_id_mismatch"):
        raise _error("snapshot_id_mismatch")
    if source_id is not None and label_source_id != _uuid(source_id, "source_id_mismatch"):
        raise _error("source_id_mismatch")
    author = _text(value.get("author"), "invalid_author")
    reviewer = value.get("reviewer")
    if reviewer is not None:
        reviewer = _text(reviewer, "invalid_reviewer")
    status = _text(value.get("status"), "invalid_status")
    if status not in {"draft", "approved"}:
        raise _error("invalid_status")
    created_at = _timestamp(value.get("created_at"), "invalid_created_at")
    frozen_at = _timestamp(value.get("frozen_at"), "invalid_frozen_at", nullable=True)
    if status == "approved":
        if reviewer is None or reviewer == author:
            raise _error("reviewer_not_independent")
        if frozen_at is None:
            raise _error("approved_label_not_frozen")
    if require_approved and status != "approved":
        raise _error("approval_required")
    cases, _ = _validate_cases(value.get("cases"))
    if snapshot_manifest is not None:
        if snapshot_manifest.get("source_id") != label_source_id:
            raise _error("source_id_mismatch")
        _bind_expected_to_snapshot(cases, snapshot_manifest)
    return {
        "schema": SCHEMA,
        "corpus_snapshot_id": label_snapshot_id,
        "corpus_source_id": label_source_id,
        "author": author,
        "reviewer": reviewer,
        "status": status,
        "created_at": created_at,
        "frozen_at": frozen_at,
        "cases": cases,
    }


def read_evaluation(path: Path) -> tuple[dict[str, Any], str]:
    """Read canonical label bytes and return ``(value, exact_sha256)``."""

    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError:
        raise _error("label_read_failed") from None
    return _parse_bytes(data), hashlib.sha256(data).hexdigest()


def _read_snapshot(path: Path, snapshot_id: str, source_id: str) -> tuple[dict[str, Any], str]:
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError:
        raise _error("snapshot_read_failed") from None
    actual_id = hashlib.sha256(data).hexdigest()
    if actual_id != snapshot_id:
        raise _error("snapshot_id_mismatch")
    try:
        manifest = load_manifest(data, actual_id, source_id)
    except ValueError:
        raise _error("invalid_snapshot") from None
    return manifest, actual_id


def validate_split(calibration_path: Path, heldout_path: Path) -> dict[str, Any]:
    """Require separate, hash-distinct calibration and held-out labels."""

    calibration_path = Path(calibration_path)
    heldout_path = Path(heldout_path)
    try:
        calibration_real = calibration_path.resolve()
        heldout_real = heldout_path.resolve()
    except OSError:
        raise _error("label_read_failed") from None
    if calibration_real == heldout_real:
        raise _error("calibration_heldout_same_file")
    calibration, calibration_sha = read_evaluation(calibration_path)
    heldout, heldout_sha = read_evaluation(heldout_path)
    if calibration_sha == heldout_sha:
        raise _error("calibration_heldout_same_hash")
    calibration_cases, calibration_ids = _validate_cases(calibration["cases"])
    heldout_cases, heldout_ids = _validate_cases(heldout["cases"])
    if calibration_ids & heldout_ids:
        raise _error("calibration_heldout_case_overlap")
    normalize_query = lambda value: " ".join(value.split()).casefold()
    calibration_queries = {normalize_query(case["query"]) for case in calibration_cases}
    heldout_queries = {normalize_query(case["query"]) for case in heldout_cases}
    if calibration_queries & heldout_queries:
        raise _error("calibration_heldout_query_overlap")
    return {
        "calibration_sha256": calibration_sha,
        "heldout_sha256": heldout_sha,
        "calibration_case_count": len(calibration_ids),
        "heldout_case_count": len(heldout_ids),
    }


def run_heldout(
    label_path: Path,
    snapshot_manifest_path: Path,
    db: Path,
    *,
    calibration_path: Path,
    expected_label_sha256: str,
    search_fn: Callable[..., Any],
    top: int = 5,
) -> dict[str, Any]:
    """Run an approved held-out set through an explicitly supplied search function."""

    label_path = Path(label_path)
    snapshot_manifest_path = Path(snapshot_manifest_path)
    db = Path(db)
    calibration_path = Path(calibration_path)
    if not callable(search_fn):
        raise TypeError("search_fn must be callable")
    expected_label_sha256 = _sha(expected_label_sha256, "label_digest_required")
    split = validate_split(calibration_path, label_path)
    labels, actual_label_sha = read_evaluation(label_path)
    if actual_label_sha != expected_label_sha256:
        raise _error("label_digest_mismatch")
    manifest, snapshot_id = _read_snapshot(
        snapshot_manifest_path,
        labels["corpus_snapshot_id"],
        labels["corpus_source_id"],
    )
    validated = validate_evaluation(
        labels,
        snapshot_manifest=manifest,
        snapshot_id=snapshot_id,
        source_id=labels["corpus_source_id"],
        require_approved=True,
    )
    normalized_cases = [
        {"label": case["id"], "query": case["query"], "expected": case["expected"]}
        for case in validated["cases"]
    ]
    raw_report = evaluate(db, normalized_cases, top=top, search_fn=search_fn)
    by_id = {case["id"]: case for case in validated["cases"]}
    results: list[dict[str, Any]] = []
    target_ranks: list[int] = []
    miss_count = 0
    miss_passes = 0
    distractor_count = 0
    distractor_passes = 0
    modes: list[str] = []
    for item in raw_report["results"]:
        case = by_id[item["label"]]
        candidate_mode = item.get("mode")
        mode = candidate_mode if candidate_mode in REPORT_MODES else "unknown"
        modes.append(mode)
        rank = item.get("rank")
        passed = bool(item["hit"])
        if case["type"] in {"positive", "distractor"}:
            if rank is not None:
                target_ranks.append(rank)
            if case["type"] == "distractor":
                distractor_count += 1
                if passed:
                    distractor_passes += 1
        else:  # miss: evaluate() sets hit only when the result list is empty.
            miss_count += 1
            if passed:
                miss_passes += 1
        results.append({"case_id": case["id"], "rank": rank, "pass": passed, "mode": mode})
    positive_count = sum(case["type"] in {"positive", "distractor"} for case in validated["cases"])
    mode = modes[0] if modes and all(item == modes[0] for item in modes) else "mixed"
    return {
        "schema": REPORT_SCHEMA,
        "label_sha256": actual_label_sha,
        "snapshot_id": snapshot_id,
        "source_id": validated["corpus_source_id"],
        "mode": mode,
        "top": raw_report["top"],
        "case_count": len(results),
        "positive_case_count": positive_count,
        "hit_at_k": len(target_ranks) / positive_count if positive_count else 0.0,
        "positive_hit_at_k": len(target_ranks) / positive_count if positive_count else 0.0,
        "mean_reciprocal_rank": sum(1.0 / rank for rank in target_ranks) / positive_count if positive_count else 0.0,
        "positive_mrr": sum(1.0 / rank for rank in target_ranks) / positive_count if positive_count else 0.0,
        "negative_case_count": miss_count,
        "negative_pass_rate": miss_passes / miss_count if miss_count else None,
        "miss_case_count": miss_count,
        "miss_pass_rate": miss_passes / miss_count if miss_count else None,
        "distractor_case_count": distractor_count,
        "distractor_pass_rate": distractor_passes / distractor_count if distractor_count else None,
        "calibration_sha256": split["calibration_sha256"],
        "results": results,
    }


# Short aliases keep the API easy to discover without changing the contract.
load_evaluation = read_evaluation
run = run_heldout
