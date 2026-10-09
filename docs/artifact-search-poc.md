# Local artifact-search prototype (#40)

This is an independent, local CLI under `tools/artifact_search/`. It reads an
explicit archive manifest and writes a private SQLite index outside the archive.
It does not add server endpoints, alter sync/deployment, schedule jobs, call an
embedding provider, or change original files.

## Ownership and next boundary

Mimir owns original artifact storage and authorized file access. Extraction,
indexing and query ranking belong to a separate artifact-search component; this
directory houses the feasibility spike, not a new Mimir production service.
Gille Inference owns local embedding serving. Munin remains the selected-summary
and operational-memory layer. A durable job may later be dispatched by Hugin.

This follows the owner's accepted #40 sketch. Production ownership/repository and
an authenticated agent-facing interface must be agreed before promotion beyond
the local prototype. Future implementation issues belong to that selected owner.

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
paths bypass. No network or model calls are made. Corpus manifests, query labels,
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

## Bounded follow-ups

1. Select the permanent artifact-search owning project and curate an independent,
   more diverse query set with expected original locations and difficult misses.
2. Once Gille Inference has a verified embedding runtime, add an optional local
   client to `POST /v1/embeddings`, model `embeddinggemma-2`. Pin checkpoint,
   tokenizer, dimension, normalization and query/document prefixes in the cache
   identity; rebuild embeddings when those change. Keep lexical search available
   during model outages and respect gateway admission/retry behavior.
3. Measure lexical, vector and hybrid ranking on the same held-out queries before
   selecting fusion weights or increasing corpus size. A small corpus needs no
   separate vector database initially.
4. Add OCR or image embeddings only for measured misses where extracted text is
   insufficient; the currently drafted M5 API contract covers text/code only.
5. Design authenticated agent retrieval, per-corpus permissions, authorized remote
   byte verification and index retention before multi-user or server deployment.
