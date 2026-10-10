# Artifact search evaluation labels (#43)

The held-out harness provides reproducible evidence for the local artifact
search prototype. It validates labels against one complete, immutable #41
snapshot and runs them through the existing evaluator with an explicitly
provided local `search_fn`. It does not fetch a corpus, call a model, access a
service, or claim that labels are independently approved.

## Label file

The file is canonical UTF-8 JSON: keys sorted, compact separators, non-ASCII
characters retained, and one final LF. Duplicate keys, unknown fields, and
noncanonical bytes are rejected. The top-level schema is
`mimir-artifact-evaluation-v1`:

```json
{
  "author": "label-author",
  "cases": [
    {
      "expected": [
        {"kind": "page", "number": 1, "path": "reports/example.pdf", "sha256": "<64 lowercase hex>"}
      ],
      "id": "query-001",
      "provenance": {"evidence": "owner supplied source note", "source_task": "task-id"},
      "query": "question text",
      "type": "positive"
    }
  ],
  "corpus_snapshot_id": "<sha256 of the #41 manifest bytes>",
  "corpus_source_id": "<source UUID>",
  "created_at": "2026-10-10T00:00:00Z",
  "frozen_at": "2026-10-10T00:00:00Z",
  "reviewer": "independent-reviewer",
  "schema": "mimir-artifact-evaluation-v1",
  "status": "approved"
}
```

`type` is one of `positive`, `miss`, or `distractor`. A positive case must
have at least one expected location. A miss must have an empty `expected`
array. Distractors are retained as a separate label class and may be empty or
carry explicitly recorded expected locations, but are never silently relabeled
as positives. Expected locations use only `slide`, `page`, or `section`, a
1-based integer number, and a lowercase SHA-256. Every expected path and hash
must occur in the complete snapshot manifest with the same hash. Provenance
contains nonempty `source_task` and `evidence` strings.

Draft files are useful for preparation and validation. A held-out run requires
`status: "approved"`, a nonempty `frozen_at`, and a reviewer whose identity is
different from the author. Approval is a process assertion recorded in the
label; this harness does not independently verify a person's identity. A
source-grounded curator draft remains proposed and pending owner review until
that review is recorded; the harness must not be used to call it an
independently approved benchmark.

## Calibration and held-out separation

Calibration and held-out files are supplied separately to `validate_split` and
`run_heldout`. The harness rejects the same file, equal byte hashes, and any
overlapping case IDs. This is a procedural independence control: separate files
and IDs reduce accidental tuning leakage, but they are not a cryptographic
guarantee that a person did not inspect or reuse a query.

The expected held-out label SHA-256 is supplied out of band to
`run_heldout(expected_label_sha256=...)`. The runner compares it with the exact
label bytes read from disk, then verifies that the label snapshot ID is the
SHA-256 of a complete, canonical #41 manifest. A changed label or snapshot
fails before search runs.

## Report and interpretation

`run_heldout` passes normalized `{label, query, expected}` cases to the
existing `evaluate()` function and requires a callable `search_fn`; it performs
no network or default search import. The report contains the exact label byte
SHA, snapshot ID, source ID, aggregate mode, positive Hit@k and MRR, negative
pass rate, and per-case `case_id`, rank, pass state, and mode. It deliberately
omits query text, expected paths, source text, and snippets.

Positive metrics include only `type: "positive"`. Negative pass rate covers
`miss` and `distractor` cases and counts a case as passing when no expected
location is returned. No quality threshold is chosen here. Semantic quality
gates, answerability thresholds, and claims about real-corpus relevance remain
unapproved until an owner-curated evaluation has been reviewed independently.

Synthetic tests in `tools/artifact_search/test_heldout.py` cover duplicate IDs,
distractor and miss cases, empty and unknown labels, unsupported locators,
approval and reviewer failures, label digest mismatch, snapshot mismatch, and
calibration/held-out overlap.
