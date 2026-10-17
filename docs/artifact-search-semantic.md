# Decision: semantic retrieval in the search runtime

Date: 2026-10-17. Scope: opt-in vector and hybrid retrieval in the always-on
snapshot runtime (`tools/artifact_search/runtime.py`), using the EmbeddingGemma 2
endpoint already served by the M5 gateway.
Status: implemented behind a flag that is **off by default**. It is not deployed,
and no M5 service, unit, credential or gateway setting has changed. Quality is
not accepted, so lexical retrieval stays the default.

## Context

The served runtime is lexical-only (FTS5/BM25) over a four-presentation pilot
snapshot of 87 extracted units. Semantic retrieval has so far existed only in the
offline prototype (`semantic.py`). On the 2026-10-10 fresh source-audited set,
256-dimensional vector and bounded-RRF60 hybrid retrieval found 8/8 answerable
cases against lexical's 5/8. **Every mode failed every absent-answer case
(0/4).** The calibration sweep found no global cosine floor that separated misses
from valid answers. That set, and every later set listed in
[the evaluation record](artifact-search-evaluation.md), is consumed. Those sets
are regression evidence only.

## Decisions

### 1. Where vectors are computed and cached

- **Computed by the runtime's own index child.** Vectors are built only after the
  snapshot's lexical index is complete and the job is `ready`. Each step embeds a
  bounded slice: at most 8 batches, and no new unit after 15 s. All gateway
  requests, retries and Retry-After sleeps in a step share one absolute deadline,
  60 s after the step started, and each call is also capped at 8 s. That keeps
  every step well inside the 90 s kill that would otherwise cancel the lexical
  job. A step that runs out of time keeps its committed units and continues in
  the next step. Only complete per-unit vectors are committed. Vector work never
  delays, fails or cancels lexical readiness. Unexpected vector-step errors are
  recorded as `vector_step_failed` instead of crashing the child, and an invalid
  `vectors` job field is dropped rather than making `job.json` unreadable.
  Residual risk: socket timeouts apply per read, so a gateway that trickles bytes
  could still stretch a call.
- **Cached in the runtime state directory.** Each embedding identity gets one
  private (`0600`) SQLite file, `vectors-<dims>d-<identity-hash>.sqlite`. The
  identity hash covers the cache schema, model, gateway revision and
  operator-verified model checksum, dimensions, task prefixes, normalization,
  segmentation and the extraction pipeline. Entries are keyed by content, reusing
  the prototype's `artifact-vectors-v1` keys, so an unchanged unit is never
  re-embedded when a new snapshot arrives.
- **Bound to snapshots explicitly.** A `snapshot_coverage` row is written only
  after every eligible unit of that snapshot has a valid cached vector. Without
  that row, semantic modes are unavailable and queries fall back to lexical.
  A new snapshot starts with no coverage. A changed configuration produces a
  different file, so the old cache is never reused.
- **Bounded.** All vector caches together are capped at 128 MiB. Before each
  step, the runtime also keeps 32 MiB of the 512 MiB state budget free for
  lexical work, enforced through SQLite `max_page_count`. Exhausting either
  limit fails the vector state with `vector_budget_exceeded`; it can never trip
  the lexical job's `state_budget_exceeded`. Caches for old identities are kept
  until an operator prunes them, with normal deletion authorization. Semantic
  modes handle at most 5,000 eligible units; a larger corpus is reported as a
  failed vector state. 256 dimensions is the default. 768 dimensions are accepted,
  but the JSON cache costs roughly three times as much, so choose them only from
  calibration evidence.
- **Known secrets never leave the host.** Units that match the known-secret
  detector are not embedded and not ranked semantically. The index already
  excludes such artifacts; this is defence in depth.

### 2. How the runtime calls the gateway

Only the existing private route is used, through the prototype's `M5Embedder`. It
requires an explicit private `/v1` base URL (loopback, RFC 1918, CGNAT or ULA),
disables proxies and refuses redirects. It sends the bearer only in a header and
checks the gateway release before and after every call. The runtime reads the
existing variable names `M5_OPENAI_BASE_URL` and `M5_API_KEY` from its private
environment. No provider, fallback route or credential is added.
Putting those values into the reader's `runtime.env` is a new credential
**destination**, so it needs separate owner approval (see owner decisions below).
A missing route is reported as `embedding_route_unavailable`, and lexical
retrieval keeps serving.

Interactive queries use a 3 s per-call timeout inside a 4 s absolute budget per
query, so the single-request server is never blocked for long. After any
query-embedding failure, a 30 s in-process breaker sends queries straight to
lexical without waiting. Index-time
failures are classified as follows:

- **Transient** (transport errors, an exhausted deadline, HTTP 408/429/5xx): the vector state becomes
  `unavailable`, with backoff doubling from 5 s to a 300 s cap.
- **Operator-actionable** (authentication, gateway-revision drift, malformed
  responses, cache corruption or a full cache): the vector state becomes `failed`
  until `retry` is run.

### 3. Modes and explicit lexical fallback

`POST /search` accepts an optional `"mode": "lexical" | "hybrid" | "vector"`. If
it is omitted, the configured default applies, and that default is `lexical`.

- `vector`: maximum segment cosine per unit (`decision: cosine_max_segment`).
- `hybrid`: equal-weight RRF60 over the first `max(top, 5)` candidates from each
  channel (`decision: rrf60_bounded`). This is the bounded window that recovered
  calibration Hit@5 offline.

A semantic request that cannot be served semantically returns ordinary lexical
results with `mode: lexical_fallback` and a bounded `fallback_reason`. It never
returns a mislabelled or partial semantic answer. The reasons are:

| Reason | Meaning |
| --- | --- |
| `semantic_disabled` | The flag is off. |
| a configuration error code | The configuration is invalid; the code names the problem. |
| `vectors_incomplete` | No coverage record exists for this snapshot yet. |
| `embedding_unavailable` | The gateway failed, or the breaker is open. |
| `embedding_route_unavailable` | The route environment is missing or invalid. |

Requests that omit `mode` keep the exact v1 receipt, which the installed client
validates field by field (see the [runtime contract](artifact-search-runtime.md)).
An explicit `mode`, including `"lexical"`, returns the extended shape:
`requested_mode` and per-result `score_kind`, plus `cosine` and an `embedding`
identity block for semantic results. A non-lexical default therefore also needs a
client release that accepts the extended shape. Originals are verified exactly as
for lexical results: one stale or changed original suppresses the whole response.

### 4. Abstention for negatives

Raw cosine is not a probability, because the nearest neighbour of an absent topic
still scores. Semantic responses therefore carry an explicit `abstention` block:
`policy`, `abstained`, `reason` and `signal`. The signal holds the top cosine,
the z-score of the top unit against this query's whole-corpus score distribution,
and the unit count. Two optional thresholds withhold *all* results, giving
`results: []` and `abstained: true`:

- `MIMIR_SEARCH_ABSTAIN_MIN_COSINE`: an absolute floor.
- `MIMIR_SEARCH_ABSTAIN_MIN_ZSCORE`: the top unit must stand out from the corpus.
  Fewer than 8 units abstain, because a margin cannot be estimated.

Identical scores are exactly flat, because the statistics use `math.fsum` with a
negligible-spread guard, so a flat distribution can never pass the margin.
The margin was chosen over the gap to the second-best unit because the corpus is
full of revisions and copies, so near-duplicates make that gap meaningless.

With no threshold set, the policy is `none` and nothing is withheld. That is only
acceptable for explicitly requested evaluation modes. **Configuring a semantic
default without an abstention policy is a configuration error**
(`semantic_default_requires_abstention`), which disables semantic retrieval and
keeps lexical as the default. Thresholds must come from a separate calibration
split, never from a held-out or consumed set. This retrieval-level policy is not
an answer check. The offline `answerability.py` remains a separate, unintegrated
second stage, and only the native callback route has passed it.

### 5. The default stays lexical

Switching the default requires every gate in the evaluation plan below to pass,
followed by owner approval and a separately approved configuration change.
Rollback means setting `MIMIR_SEARCH_SEMANTIC=off` or reverting to the previous
release. The vector cache is inert derived state, and older releases ignore the
`vectors` job field.

## Configuration

All values come from the reader's private environment file. Each is an operator
input, never a request parameter.

| Variable | Values | Default |
| --- | --- | --- |
| `MIMIR_SEARCH_SEMANTIC` | `off`, `on` | `off` |
| `MIMIR_SEARCH_DEFAULT_MODE` | `lexical`, `hybrid`, `vector` (non-lexical requires `on` plus an abstention threshold) | `lexical` |
| `MIMIR_SEARCH_EMBEDDING_REVISION` | `<40-hex gateway SHA>:<64-hex operator-verified model SHA-256>` | required when `on` |
| `MIMIR_SEARCH_EMBEDDING_DIMENSIONS` | `256`, `768` | `256` |
| `MIMIR_SEARCH_ABSTAIN_MIN_COSINE` | number in [-1, 1] | unset |
| `MIMIR_SEARCH_ABSTAIN_MIN_ZSCORE` | number in [0, 100] | unset |
| `M5_OPENAI_BASE_URL`, `M5_API_KEY` | the existing private M5 route | unset |

Any invalid value disables semantic retrieval. `GET /health` then reports the
error code under `semantic.error`. The `semantic` block in `/health` reports the
flag, default mode, identity, thresholds and vector build state. It never
includes credentials, and it does not affect `ready`.

## Evaluation plan (fresh blind cases)

Every earlier query set is consumed. That covers the 2026-10-09 feasibility
cases, the 2026-10-10 lexical held-out set, the fresh retrieval set, the third
quoted-evidence set, both 16-case precision sets and the native-callback blind
set. They may be replayed as regression evidence only, never used for tuning or
acceptance.

0. **Runtime regression replay.** Run the consumed sets through the served code
   path with `heldout_cli --runtime-state <copy> --runtime-handoff <path> --mode
   lexical|vector|hybrid`, which reports `evaluated_path: runtime`. The runtime
   differs from the offline prototype: BM25 has no title weight, and there is no
   cosine floor except through abstention. So this replay checks the runtime
   implementation; it is not quality evidence.
1. **Calibration (thresholds and dimensions).** Use the existing calibration v2
   split (8 answerable, 4 misses), plus a new calibration supplement written by a
   separate curator: at least 8 answerable cases and 8 misses. The misses must
   cover absent facts, wrong premises, near-topic absences and out-of-corpus
   topics. Record `abstention.signal` for every case. Choose `min_zscore` and/or
   `min_cosine`, and 256 versus 768 dimensions, to maximise abstention on misses
   while losing at most one answerable case. Then freeze a protocol file, with its
   SHA-256, that names the gateway release, model checksum, dimensions,
   thresholds, window, top 5 and code revision.
2. **Fresh blind acceptance set.** A new curator works in a separate context from
   the frozen snapshot and source text only. A source-only auditor checks every
   case, its alternate locations and every miss against the whole corpus before
   the labels are frozen. The conductor stays blind to the questions until the
   protocol is frozen. The set has at least 24 cases:
   - At least 12 answerable, including at least 3 distractors, at least 4
     paraphrases with no lexical overlap with the gold unit, and at least 2
     Swedish/English cross-language queries.
   - At least 12 misses.

   Name the event or document context wherever facts repeat across decks. Case
   IDs and normalized queries must be disjoint from every earlier set, which
   `validate_split` checks against calibration. Prefer gold locations that no
   earlier set used.
3. **Predeclared gates for making `hybrid` the default:**
   - positive Hit@5 at least 10/12, with no answerable case lost relative to
     lexical on the same set;
   - MRR at least 0.75;
   - all distractors;
   - **all misses abstained**;
   - zero schema, transport or fallback-labelling errors;
   - effective mode `hybrid` for every case, so fallback cannot count as success.

   Operational gates, measured on M5 against a private state copy:
   - warm query p95 under 1.5 s, including the gateway round trips;
   - a gateway-outage drill that returns `lexical_fallback` within 3 s, followed
     by breaker short-circuiting.

   If ranking passes but abstention does not, `hybrid` may be offered only as an
   explicitly requested mode, and the default stays lexical.
4. **Recording.** Commit only aggregate metrics. Private labels, audit receipts,
   protocol and query-level reports stay out of Git (mode `0600`, as before). The
   set is consumed after a single run.

The 87-unit pilot corpus has few unused answer locations left, so a credible
fresh set may be impossible until the corpus is widened (owner decision 1).

## Owner decisions flagged

1. **Widening the indexed collection** beyond the four pilot presentations is a
   separate decision. A new allowlist means a new source UUID and new derived
   state ([operations](artifact-search-operations.md)). It also brings more
   private text to the M5 embedding model (it stays on M5), state-budget and
   rebuild-time checks, and a fresh secret-scan review. It is also what makes
   enough fresh evaluation cases possible, so decide it before step 2.
2. **Credential destination.** Enabling the flag on M5 needs the existing M5
   route in the reader's `runtime.env`. A gateway-scoped key, owned by
   gille-inference, would be preferable to reusing an owner key if the gateway
   offers one.
3. **Deployment, enabling the flag and flipping the default** are each separately
   approved operational actions.

## Follow-ups (not in this change)

- A `--mode` option in the `mimir-search` client (`Magnus-Gille/claude-skills`),
  so agents can request `hybrid` explicitly once it is enabled.
- An attested model fingerprint from the gateway; the checksum is still an
  operator pin.
- Integrating an evidence-based answer check as a second abstention stage, once
  an M5-hosted model passes it.
- A packed float cache, if the corpus outgrows the 5,000-unit semantic ceiling.
