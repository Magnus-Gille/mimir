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
calls. Each candidate has `candidate_id`, `text`, and optional `source` metadata
(up to 1000 characters) identifying the document and locator. Metadata is passed
separately, treated as untrusted context, and never inserted into a quoted excerpt. It does not alter the lexical service, retrieval defaults, or deployment.

The code divides candidate text into bounded, overlapping, verbatim spans with
stable IDs for that call. The model selects IDs rather than copying quotations;
the code resolves selected IDs back to exact original text. The first assessment
decomposes the question into required facts, properties and relationships. Each
requirement must provide an `answer` field and a source-grounding or missing-
information `justification` before its `status`. Supported requirements need a
concrete answer; other statuses may leave the answer blank. The parser enforces the response
field order `id`, `requirement`, `evidence`, `answer`, `justification`, `status`.
Answers and justifications are bounded to 2000 characters; supported answers and
all justifications must be nonblank. Any missing, partial or contradicted
requirement causes abstention.
For a proposed complete answer, a second check receives the original question
and only the selected spans, without the first assessment's requirements or
verdict. It independently checks all requested parts, entity and time scope,
units and precision. Equivalent wording, translations, headings and diagrams
can supply explicit support; a related topic or a less precise value cannot.
Both calls may use the same model, so this is not independent model review.

Two fully supported assessments are required for `answerable: true`. Recognized
explicit Swedish and English calendar-date requests also require a valid full date in a
selected source span. This conservative check recognizes common numeric dates
and Swedish/English month names, validates calendar days and leap years, and
does not combine fragments across spans. Unsupported formats abstain; this is
not a universal date parser. A date elsewhere in the excerpt is not proof of
its relationship to the question: the model assessments and independent evidence
audit must still check that relationship.

The result includes exact quoted evidence, both structured assessments and, when
applicable, the calendar-precision check. The module
validates bounded JSON, schema and status values, unique requirement IDs, and
valid source-span selections. The verifier can select only spans it was supplied.
Malformed output or callback failure raises an error; neither is
a successful abstention. An empty candidate list returns an explicit missing
result without a completion call.

The callback is responsible for transport, model identity, generation completion
and structured-output settings. The evaluation caller must also bind the excerpts
to the retrieved originals and revalidate their freshness after inference. These
checks are separate from semantic correctness: valid JSON and matching quotes do
not prove that a question was fully answered. Mocked unit tests establish the
protocol behavior; real source-audited cases and independent evidence review are
required to assess answer completeness.


The calendar guard corrected the earlier month-only exact-date false positive:
all 12 calibration cases and all 12 consumed regression cases passed. Replaying
the same real model responses after deterministic parser corrections preserved
all outcomes. These are calibration and regression evidence, not fresh acceptance.

The first new 16-case precision test still failed: 7/8 positives, 2/2 distractors,
and 6/8 misses. It exposed an unrecognized Swedish calendar-date wording, now
covered by a regression test. A stronger independent source-only re-audit also
excluded two negative labels as ambiguous. The frozen labels and failed report
remain intact; neither relabeling nor replaying this consumed set establishes a
passing held-out result. Replacement labels require a new source-only review
before inference, with the same predeclared thresholds.


A second source-audited 16-case trial also failed with Qwen: 6/8 expected positive
locations, 2/2 distractors, and 6/8 misses. Requiring concrete answers and
justifications made the failures inspectable but did not improve those rates.
Independent review of the selected evidence confirmed unsupported assertions,
an incomplete answer, and a question with conflicting cross-document context.
The consumed reports remain available privately. Subsequent blind questions
must identify their event/document context where facts repeat, and their gold
locations must account for equivalent answers within that context.

The caller now supplies verified document/locator provenance separately from
excerpt text. Model selection is evaluated on consumed calibration and regression
cases before another blind test; improving prompt structure alone is not a
semantic acceptance result.

### Scoped native-callback acceptance

Auditing concrete answers exposed errors that matching quotations alone missed:
incorrect entity/value pairings, omitted source locations, and changed technical
terms. Gemma and the smaller native callback failed semantic or regression
checks. Those results remain preserved; they are not accepted retrospectively.
The prompts now require concrete location answers, preserve source terminology,
and prohibit unsupported relationships between neighboring metrics.

At code revision `99ae4032456ba29c0febd7b940659b3a46690c8d`, separate native agent
contexts were requested with `gpt-6-sol/high` for assessment and verification.
The verifier received only the original question and selected source spans.
An independent source/answer auditor was requested with `gpt-6.1-sol/high`.
The harness does not expose the actual model or effort, so these are requested
routes, not runtime identity or model-weight attestations.

| Set | Answerable cases passed | Misses correctly declined | Distractor subset passed |
| --- | ---: | ---: | ---: |
| Calibration | 8/8 | 4/4 | — |
| Consumed regression | 8/8 | 4/4 | 2/2 |
| New frozen blind test | 8/8 | 8/8 | 2/2 |

The blind test had no schema or execution errors. Independent audits found all
24 accepted answers across these sets complete and supported in both stages.
The unchanged gate required at least 7/8 blind positives, both distractors,
all eight misses, and complete independent answer/evidence review. It passes
for this offline native-callback experiment. All 168 Python tests pass.

The new questions and expected locations were disjoint from earlier sets and
were independently source-audited before inference. The pinned corpus contains
87 extracted text units from four decks; fresh positive cases cover the three
decks with unused eligible locations. The misses include incomplete multipart
answers, insufficient precision, wrong premises, and absent facts. This is a
curated text benchmark, not historical user-query evidence, human approval, or
an assessment of all original HTML metadata and visuals.

Private request/response files bind the exact prompts, source excerpts, labels,
code and scoring protocol by hash. The offline bridge replays the real callback
contract, rechecks every retrieved source including abstentions, and scores
only final accepted evidence. Failed output is never a successful abstention.
The asynchronous native workflow provides no serving-latency measurement.
Qwen/Gemma on M5 remain unaccepted for this answer-checking policy; M5 supplies
the existing embeddings. No answerability endpoint, runtime default, original
archive, or deployment was changed. Production integration and broader
independent evaluation remain separate work.
