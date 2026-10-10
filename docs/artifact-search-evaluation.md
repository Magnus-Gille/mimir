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
array. A distractor is a difficult positive query surrounded by similar but
irrelevant candidates, so it must also have at least one accepted expected
location and is scored with the positive targets. It remains a separate label
class for reporting. Expected locations use only `slide`, `page`, or `section`,
a 1-based integer number, and a lowercase SHA-256. Every expected path and
hash must occur in the complete snapshot manifest with the same hash.
Provenance contains nonempty `source_task` and `evidence` strings.

When a case contains `sha256`, the evaluator requires the returned hit to carry
the same exact artifact hash as well as the path and locator. A stale or
hash-less hit does not satisfy the label. Legacy evaluator cases may omit
`sha256` and retain their path/locator-only behavior; held-out labels always
include it. Search hits may also omit optional source or snapshot identifiers in
legacy retrieval modes, so this harness does not claim source-ID attestation
beyond the path/hash binding above.

Draft files are useful for preparation and validation. Before any held-out
ranking or result is produced, an approved label set must complete one of two
review paths:

1. An independent human review by a reviewer whose identity is different from
   the author.
2. A separate, source-grounded automated audit against the frozen snapshot.
   The audit records its auditor identity, requested model or rule revision,
   runtime-observed model or revision when available, and evidence for every
   case. Ambiguous cases are excluded from the approved set rather than being
   guessed.

Both paths require a nonempty `frozen_at` and a reviewer identity distinct from
the author. The `reviewer` and `status: "approved"` fields record that this
process review happened; the harness does not independently verify a person's
identity or an automated auditor's claims. This is a process option only: the
code gate still requires approved, frozen labels and does not autoapprove
drafts.

An automated source-audited benchmark is curated evidence, not human approval
or evidence from actual user queries. It cannot, by itself, establish broad
retrieval quality. Its source-audit receipt must be separate from ranking and
calibration work, and the calibration split remains disjoint from the held-out
queries.

For an automated audit, retain a concise evidence receipt containing:

- the complete source snapshot identifier and SHA-256;
- one verdict per case, with alternate valid locators where applicable;
- a full-corpus check for miss cases, rather than a partial-corpus absence;
- excluded or ambiguous cases and the reason for each exclusion; and
- the requested model or revision and the runtime-observed model or revision,
  with unknown values recorded as unknown rather than inferred.

## Calibration and held-out separation

Calibration and held-out files are supplied separately to `validate_split` and
`run_heldout`. The harness rejects the same file, equal byte hashes, and any
overlapping case IDs or normalized queries (case-folded with repeated
whitespace collapsed). This is a procedural independence control: separate
files, IDs, and query text reduce accidental tuning leakage, but they are not a
cryptographic guarantee that a person did not inspect or reuse a query.

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
pass rate, distractor pass rate, and per-case `case_id`, rank, pass state, and
mode. It deliberately
omits query text, expected paths, source text, and snippets.

The reproducible JSON entry point is `python -m tools.artifact_search.heldout_cli`
with `--labels`, `--snapshot-manifest`, `--calibration`,
`--expected-label-sha256`, and `--db`. It accepts `--top` and
`--mode lexical|vector|hybrid`; vector and hybrid additionally require the existing
`--embedding-revision` and may set `--dimensions` and `--min-score`. The
output preserves the aggregate report, adds `requested_mode`, and exposes an
`effective_modes` mapping by case ID so hybrid lexical fallback remains
visible. The callback and M5 client are created lazily only after the held-out
preflight has accepted the approved, frozen, digest-pinned split.

Positive Hit@k and MRR include both `positive` and `distractor` cases because
both have accepted targets. Distractor pass rate reports that subset. Negative
pass rate covers `miss` cases and uses the evaluator's negative result: a miss
passes only when the search returns no result at all. An unrelated hit therefore
fails the miss case. No quality threshold is chosen here. Semantic quality
gates, answerability thresholds, and claims about real-corpus relevance remain
unapproved until an owner-curated evaluation has been reviewed independently.

Synthetic tests in `tools/artifact_search/test_heldout.py` cover duplicate IDs,
distractor and miss cases, empty and unknown labels, unsupported locators,
approval and reviewer failures, label digest mismatch, snapshot mismatch, and
calibration/held-out overlap.

## Source-audited comparison, 2026-10-10

A separate curator proposed twelve new questions from four event presentations
(87 extracted units); a separate source-only reviewer checked every question,
alternate answers and corpus-wide misses before labels were frozen. Eight cases
have answers, including two distractors, and four have no answer in the selected
corpus. Answer locations and queries are disjoint from the earlier consumed set
and calibration labels. Neither curator nor reviewer saw retrieval results.
The conductor kept the new question details blind until the retrieval policy was
locked. This is automated source review, not human review or a historical query
sample. The private corpus, labels, audit receipts and query-level reports are
excluded from Git.

Calibration used a separate eight-positive/four-miss set. With 256-dimensional
EmbeddingGemma 2, the old hybrid's unbounded RRF ranked weak shared-tail matches
above strong single-channel results. Limiting each channel to `max(top, 5)` before
equal-weight RRF60 raised calibration Hit@5 from 5/8 to 8/8. No global cosine
floor separated the calibration misses from valid matches: one absent-answer
query scored above most valid answers. The zero floor was therefore retained as
a retrieval baseline, without an answerability claim.

The settings and engineering gate were recorded before the fresh test: top 5,
256 dimensions, zero cosine floor, equal-weight RRF60, bounded window as above;
at least 7/8 positive hits, MRR at least 0.75, both distractors, all four misses,
and no lost positives relative to the old hybrid. A single shared cache supplied
the same twelve query embeddings to vector and both hybrid implementations.
The gateway release was checked around each call; the model artifact checksum
was verified read-only on the host and remains an operator pin, not API attestation.

| Fresh test mode | Positive Hit@5 | MRR | Distractors | Misses |
| --- | --- | --- | --- | --- |
| Lexical | 5/8 | 0.5250 | 2/2 | 0/4 |
| Vector | 8/8 | 0.9375 | 2/2 | 0/4 |
| Previous unbounded hybrid | 5/8 | 0.6250 | 2/2 | 0/4 |
| Bounded hybrid | 8/8 | 0.8750 | 2/2 | 0/4 |

The bounded hybrid lost no previously passing cases. Its engineering gate still
**fails** on absent-answer cases; neither semantic mode is accepted as an
answerability policy, and lexical remains the default. Returning a related source
is not evidence that it answers the question. These measurements cover the local
prototype, not a deployed search runtime. After this run the fresh set is consumed
regression evidence; further tuning needs separate calibration and a new blind
acceptance set. The next missing capability is evidence-based answer checking or
abstention, evaluated separately from candidate relevance.

### Separate quoted-evidence pilot

A private experiment asked the existing M5 `qwen3-30b-instruct` route whether
the five retrieved excerpts directly answered a question. It required strict
JSON, a complete generation and an exact contiguous supporting quotation; cited
sources were revalidated after the additional call. The API reported
`Qwen3-30B-A3B-Instruct-2507-Q4_K_M.gguf`; the chat-model artifact was not attested.
This is an experiment, not an integrated runtime or a new default.

The prompt first passed all eight answerable and four absent-answer calibration
cases. It was then frozen and tested on a third, separately source-audited set
whose twelve queries and answer locations were disjoint from both earlier sets
and calibration. It retained all eight accepted answer locations, including both
distractors, and correctly abstained on three of four misses. The remaining false
positive accepted month-level evidence for an exact-date question. The quote was
present in the source, but did not satisfy the requested precision.

The predeclared all-misses gate therefore still **fails**. Quote matching checks
source presence, not whether the evidence fully answers the question. No prompt
or labels were changed after the test, no answer-checking feature is enabled by
this result, and the consumed set is now regression evidence. A future integrated
check needs tests for partial answers and requested precision, distinct from its
retrieval and exact-quotation checks.


### Offline completeness and precision check

`tools/artifact_search/answerability.py` provides an opt-in, transport-independent
`check_answerability(question, candidates, complete)` function for offline
experiments. It accepts up to five excerpts and makes at most two completion
calls. It does not alter the lexical service, retrieval defaults, or deployment.

The first assessment decomposes the question into required facts, properties and
relationships. Any missing, partial or contradicted requirement causes abstention.
For a proposed complete answer, a second check receives the original question
and only the cited excerpts, without the first assessment's requirements or
verdict. It independently checks all requested parts, entity and time scope,
units and precision. Equivalent wording, translations, headings and diagrams
can supply explicit support; a related topic or a less precise value cannot.
Both calls may use the same model, so this is not independent model review.

Only two fully supported assessments produce `answerable: true`. The result
includes exact quoted evidence and both structured assessments. The module
validates bounded JSON, schema and status values, unique requirement IDs, and
exact source citations. The verifier can cite only text contained in a quote it
was supplied. Malformed output or callback failure raises an error; neither is
a successful abstention. An empty candidate list returns an explicit missing
result without a completion call.

The callback is responsible for transport, model identity, generation completion
and structured-output settings. The evaluation caller must also bind the excerpts
to the retrieved originals and revalidate their freshness after inference. These
checks are separate from semantic correctness: valid JSON and matching quotes do
not prove that a question was fully answered. Mocked unit tests establish the
protocol behavior; real source-audited cases and independent evidence review are
required to assess answer completeness.
