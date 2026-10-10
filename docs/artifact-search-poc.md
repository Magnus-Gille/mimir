# Local artifact-search prototype (#40)

This is an independent, local CLI under `tools/artifact_search/`. It reads an
explicit archive manifest and writes a private SQLite index outside the archive.
It does not add server endpoints, alter sync/deployment, schedule jobs or change
original files. Embedding calls require an explicit private M5 route and command.

## Ownership and next boundary

Mimir owns original artifact storage and authorized file access. Extraction,
indexing and query ranking belong to an independently deployed artifact-search
component, code-owned in this repository. This directory houses the feasibility
spike. See [the ownership/placement decision](artifact-search-architecture.md).
Gille Inference owns local embedding serving. Munin remains the selected-summary
and operational-memory layer. A durable job may later be dispatched by Hugin.

This follows the owner's accepted #40 sketch. The future service runs on M5 and
pulls published originals from Mimir, so search does not depend on an awake laptop.
The prototype currently runs locally; the remote adapter and authenticated
agent-facing service are follow-up work in the Mimir repository.

## Run

Use Python 3.10+ with SQLite FTS5. PDF extraction additionally needs Poppler's
`pdftotext` on PATH; all other extraction uses the Python standard library. The
existing Mimir Node dependencies are unchanged. Run from the repository root:

```sh
mkdir -m 700 /tmp/artifact-search-state
# corpus.json is a complete JSON array of root-relative source paths, for example:
# ["presentations/deck.html", "reports/brief.pdf"]
python3 -m tools.artifact_search.cli index \
  --root "$HOME/mimir" \
  --db /tmp/artifact-search-state/search.sqlite \
  --manifest /tmp/artifact-search-state/corpus.json

printf '%s' 'strawberry Navier Stokes' | \
  python3 -m tools.artifact_search.cli search \
  --db /tmp/artifact-search-state/search.sqlite --top 5

python3 -m tools.artifact_search.cli evaluate \
  --db /tmp/artifact-search-state/search.sqlite \
  --cases /tmp/artifact-search-state/queries.json --top 5

PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover \
  -s tools/artifact_search -p 'test_*.py'
```

### Opt-in M5 vector indexing and comparison

Load the private route through the existing approved `m5-auth` helper; do not
print or save its exported bearer. `REVISION` is the accepted 40-character gateway
SHA, a colon, and the operator-verified 64-character model-artifact SHA-256.
The client verifies the gateway revision before and after each embedding call.
The artifact checksum is an operator pin, not a model fingerprint attested by the API.

```sh
eval "$(m5-auth --env --tailnet)"
# Set REVISION to the exact previously verified release:artifact checksum.
python3 -m tools.artifact_search.cli embed \
  --db /tmp/artifact-search-state/search.sqlite \
  --embedding-revision "$REVISION" --dimensions 256

printf '%s' 'beskrivningen av bilden jag söker' | \
  python3 -m tools.artifact_search.cli search \
  --db /tmp/artifact-search-state/search.sqlite --mode hybrid \
  --embedding-revision "$REVISION" --dimensions 256

python3 -m tools.artifact_search.cli compare \
  --db /tmp/artifact-search-state/search.sqlite \
  --cases /tmp/artifact-search-state/queries.json --top 5 \
  --embedding-revision "$REVISION" --dimensions 256
```

`embed` sends only current, filtered extracted text/title, batches within the API's
16-item/32-KiB limits and commits only complete unit vectors. Re-running resumes
from the content cache. It pins model/revision/dimensions, task prefixes,
normalization, segmentation and the extraction/detector pipeline. Private input
is not sent to a public provider, proxy or redirect. Credentials stay in memory.
Long Retry-After delays stop the command with bounded status/delay diagnostics;
short (at most two-second) delays allow one retry. Do not retry before that delay.

`vector` uses maximum segment cosine per original unit; `hybrid` uses fixed RRF60
over BM25 and vector ranks. Hybrid explicitly falls back to lexical on a model
outage or when no usable vectors are cached for the selected identity, with a
distinct warning for each case. `--min-score` is an optional cosine floor (default zero), not calibrated
answerability. Vector search can return irrelevant results for an absent topic.
No threshold is tuned on the reported comparison set.

Comparison embeds each distinct query once and shares those vectors across vector
and hybrid ranking. Thus retrieval quality is comparable, but the modes' reported
wall times are not latency comparisons: the hybrid pass reuses query embeddings.
Reports include each case's actual retrieval mode; fallback is never counted as
unlabelled semantic success. Repeated runs may see concurrent original changes.

Queries may be supplied with `--query` or stdin. Prefer stdin for private query
text. JSON query cases have `label`, `query`, and `expected`, where each expected
location has `path`, `kind` (`slide`, `page`, or `section`), and 1-based `number`.
An empty expected array is a negative case requiring no results. Evaluation
reports labels/ranks/timings, not source text or paths. Labels should be safe to
include in reports. Hit@K and MRR count positive cases; negative-case success and
overall case pass rate are reported separately.

## Source locations and retrieval

- Static HTML slides: outermost `.slide` / `data-slide` elements, or Reveal leaf
  sections. DOM order determines slide numbers, including empty slides. Inline
  letter styling preserves words. Headings and speaker notes are searchable;
  scripts, styles, templates and navigation are omitted.
- Other static HTML: outermost sections or the whole document. A unique existing
  element ID becomes the anchor. No anchor is invented from a slide number.
- PDF: text per original physical page, including blank-page offsets. Image-only
  PDFs are explicitly excluded as `no_searchable_text`; OCR is not implemented.
- Markdown/text: basic heading sections or a whole-file section. Markdown section
  numbers are extraction order, not source line numbers. JavaScript-generated
  slides, full browser layout, image meaning and PPTX are not supported.

SQLite FTS5/BM25 ranks Unicode-normalized query terms, with extra title weight.
Common Swedish/English query words are ignored; remaining terms are OR-combined.
A title can fall back to the filename stem. Exact equation/term queries work well;
generic descriptions can match an unrelated shared word. Scores are not a claim
of semantic understanding. Equal ranks sort by source path then numeric location.

Each hit includes root-relative path, original SHA-256, location, title, excerpt,
and index timestamp. Optional `--base-url https://files.example.com` constructs
an authorized `/files/...` reference; it grants no access and stores no token.
PDF links include `#page=N`; HTML fragments use only real unique IDs.

The original hash is verified against the local root before returning a hit.
Optional Mimir URLs are references only: this prototype does not verify that the
NAS copy has the same bytes. Before claiming a remotely served version matches,
an authorized caller must fetch it and compare the hash. Opening the file later
can still race a subsequent source change; consumers should retain the hash.

## Updates and data handling

Every indexing run synchronizes the complete manifest. Changed hashes replace
all units atomically; unchanged hashes and pipeline versions reuse units.
Renames become removal plus insertion. Files omitted from the manifest, removed,
unreadable, or newly excluded lose their old searchable rows. During queries,
changed files and outdated pipeline versions are counted as stale and omitted;
missing/unreadable sources are counted separately. No stale excerpt is returned.
Hash validation reads current bytes even when size and mtime are unchanged.

The index is bound to one canonical root. Source path traversal, symlink files and
symlink directories are rejected using directory-relative NOFOLLOW opens. Hidden
paths, `secrets`, `credentials`, unsupported types and sources over 20 MiB are
excluded. Only the current process's readable sources are used.

The designated index directory must be private (0700), the SQLite file private
(0600), and both outside the served/archive tree. Existing unrelated directory
permissions are not changed. The index holds source excerpts and must receive the
same retention and backup protection as the source corpus. This owner-only CLI
does not implement multi-user ACLs. Do not expose its database or output through
Mimir's serving root or a public endpoint.

Known secret formats are checked in raw text/HTML and extracted text (including
PDF text) before indexing. Detection excludes the file; originals are never
quarantined, moved, or edited. This detector is a filter, not proof that arbitrary
secrets are absent. It does not rely on ingest scanning, which some direct-copy
paths bypass. Lexical indexing/search make no model calls. Corpus manifests, query labels,
excerpts and the real index remain operator-local and untracked.

## Local feasibility result (2026-10-09)

A read-only trial selected 20 local artifacts: 18 HTML files and 2 PDFs. Eighteen
files yielded 373 units (371 HTML slides and 2 PDF pages). One HTML file matched a
known-secret format and one PDF had no extractable text; both were excluded.
The complete literal `strawberry Navier Stokes` query returned a verified original
math slide first. The labeled benchmark contains the issue's concrete query,
several exploratory reformulations, two PDF lookups, and a negative query.
All nine positive cases found an accepted source location in the first five
results (MRR 0.944); the negative case returned none. The ten-case run took about
83 ms on the local laptop, including hash validation. This is a warm, small-corpus
observation, not a latency guarantee. One source was concurrently updated by other
work; the index incorporated its new version. The other 19 original byte hashes,
sizes and mtimes matched the initial inventory. Tool source access uses read-only
opens; synthetic regression tests separately verify archive immutability and
same-size/same-mtime stale detection.

The corpus is intentionally small and contains revisions/copies of the math
slide; its top-K results do not demonstrate corpus-wide relevance or superiority
to embeddings. Expected math locations were confirmed from original slide text,
so this is a feasibility check, not an independently curated relevance benchmark.
Only the concrete issue query is owner-supplied; other cases are exploratory.
Full private results and manifests are not committed.

### Text/vector comparison after M5 integration

The second run used the same 20-file selection. A concurrent source update brought
the searchable corpus to 374 units (372 slides and 2 PDF pages) in 18 files.
372 distinct title/text units were cached at 256 dimensions. Six additional
paraphrase/miss cases were written before inspecting their retrieval results;
the fixed 16-case file SHA-256 was
`2ba47d68374b933e6fd1cecfee00799535762c0dd052de7928dbe8c993fc7310`.
The query set has 14 positives and 2 negatives, mainly the issue's math-slide topic
and CV retrieval. Labels are conductor-curated feasibility labels, not an
independent human-held-out relevance benchmark. Existing gold locations were
reused for the new paraphrases; many math locations are revisions/copies.

| Mode | Positive Hit@5 | MRR | Correct empty results on negatives |
| --- | --- | --- | --- |
| Lexical | 10/14 (71.4%) | 0.631 | 1/2 |
| Vector, 256 dimensions | 11/14 (78.6%) | 0.631 | 0/2 |
| Hybrid, fixed RRF60 | 10/14 (71.4%) | 0.679 | 0/2 |

Vector ranking recovered two newly added paraphrases that lexical search missed,
but missed one positive that lexical found. Fixed hybrid fusion did not preserve
those two semantic gains. Both semantic modes returned candidates for both
negative queries at the preselected zero cosine floor. Therefore retain lexical
as the CLI default, and expose semantic/hybrid as explicitly selected modes.
Do not present these scores as an adequate production ranking/abstention policy.
The next evaluation must test diverse queries, duplicates/distractors, candidate
selection/fusion and reliable absence handling before widening the corpus.

The comparison used one shared query embedding per case (16 calls), no changed
threshold and no reranking-model calls. Whole-run wall times were approximately
51 ms lexical, 4.28 s vector and 2.98 s hybrid; the latter reuses query embeddings
and these times must not be compared as service latency or capacity claims.
Gateway release was pinned to `e6a86a45aa15fc4a25dd95d53746e7e944ef05a9`;
model artifact identity was the operator checksum pin described above, not an
API-attested weight fingerprint.

An initial embedding build stopped with a sanitized HTTP error after 304 cache
rows; that invocation did not retain its status code, so the cause is unknown.
The CLI now preserves bounded HTTP-status/Retry-After diagnostics. Resuming reused
306 unit references and completed 68 more without writing originals. Synthetic
tests separately cover batch count/shape failures and incomplete-unit non-caching.
Another regression covers query-time reindex with SQLite row-ID reuse: freshness
checks compare path, byte hash, extraction version and unit content after the
model call, not only row IDs. No old snippet is returned as a new source version.

## Bounded follow-ups

1. Implement the source snapshot adapter and the separately packaged M5 service
   described in [the issue drafts](artifact-search-followups.md).
2. Curate an independent, diverse set of real user queries and accepted locations,
   difficult misses and distractors. Compare lexical/vector/hybrid and evaluate
   abstention before tuning thresholds or selecting a production default.
3. Keep model serving in Gille Inference. Add an attested model/runtime fingerprint
   before automating upgrades; the prototype's artifact revision is operator-pinned.
4. Add OCR or image embeddings only for measured misses where extracted text is
   insufficient; the currently drafted M5 API contract covers text/code only.
5. Design authenticated agent retrieval, per-corpus permissions, authorized remote
   byte verification and index retention before multi-user or server deployment.
