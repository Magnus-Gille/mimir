# Decision: independent artifact retrieval on M5

Date: 2026-10-09. Scope: Mímir #40 feasibility and implementation ownership.
Status: design selected for the spike; snapshot and standalone runtime packages
are implemented. Production rollout and quality acceptance remain follow-up work.
This decision does not relocate or reconfigure a live service.

## Ownership and placement

The `artifact-search` component is code-owned in `Magnus-Gille/mimir`, maintained
alongside its source contract but packaged and run independently of the Express
file server. The prototype remains under `tools/artifact_search`. A separate
repository is not required to enforce a separate process, interface and data store.

| Node/component | Responsibility |
| --- | --- |
| Laptop | Author material, publish via existing artifact sync, and use search. |
| NAS / Mímir | Published original files, authorized serving and artifact replication. |
| M5 / artifact-search | Extract, index, rank and serve authenticated retrieval; retain private derived state. |
| M5 / Gille Inference | EmbeddingGemma 2 model runtime, authentication, quota and admission for inference. |
| Control Pi / Hugin and Munin | Durable task coordination when needed, selected summaries and operational memory. |

Physical co-location does not merge these service responsibilities. Hugin is not
required for each interactive query. Munin does not ingest the whole archive.
Mímir gains no search endpoint inside its file-serving Express process.

## Data flow and availability

The laptop publishes to Mímir through the existing sync path. An indexer on M5
obtains a complete authorized inventory and fetches changed originals from Mímir.
It stores a private, replaceable extraction cache and index outside any serving
root. There is no second laptop-to-M5 archive mirror or reverse file synchronization.

Document text and queries reach the existing private M5 embedding API with the
different Google document/query prefixes. Original paths are not sent as model
input. Model calls respect ordinary credits, quota, admission and Retry-After.
The current API accepts text/code; OCR and multimodal inference are later options.

Agents call the authenticated retrieval component, then fetch the returned file
reference through Mímir using their own authorized file-access path. A reference
contains relative path, original byte SHA-256, physical page/DOM slide/section,
real anchor when present, and observation/snapshot metadata. It grants no access;
bearer credentials are never placed in URLs or excerpts.

When the laptop is off, published material remains searchable. Unpublished local
edits are not visible. If the embedding runtime is unavailable but the retrieval
service is healthy, lexical search remains available with explicit fallback state.
If M5 is down, search is down while independent Mímir file references still work.
Mímir relocation (#39) preserves its source interface and is not a search dependency.

## Versioning, updates and recovery

The future source adapter publishes only complete inventories. An interrupted,
partial or unreachable inventory cannot imply deletions. Fetch bytes must match
the accepted snapshot hash; otherwise retry a new observation, never silently
combine old metadata with new content. A local manifest is likewise a complete
selected corpus, not a partial update list.

Changes replace all extracted units; renames update references; confirmed removals
drop units and unused cache state. The prototype represents a rename as delete
plus insert, while reusing vectors for identical text. Every returned reference
must still match the current original hash. Row IDs and timestamps alone are not
freshness proof. Failure to verify the source suppresses its excerpts.

Cache identity includes model ID, verified artifact/runtime revision, dimensions,
task prefixes, normalization, segmentation and extraction/detector version plus
text/title content hashes. The prototype pins a gateway release and an
operator-verified model-artifact checksum; it checks the gateway revision around
each model call. The gateway does not advertise an attested model fingerprint:
operators must verify the checksum before selecting a revision and update it after
a model/runtime change. A machine-readable fingerprint is a follow-up requirement.

Start with 256-dimensional normalized vectors and compare cosine ranking with
FTS5/BM25 and reciprocal-rank fusion. No separate vector database is necessary
for the bounded spike. A similarity score is not a calibrated probability that
an answer exists; miss/abstention behavior needs independent evaluation.

The index and extraction cache are derived, private data; rebuilding from
published originals is the recovery authority. Preserve approved corpus selection,
configuration and model identity separately from disposable cache rows. Define
retention, rebuild time and backup treatment before production deployment.

## Access and resource boundaries

The spike is an owner-only CLI. It scans only an explicit allowlist, rejects
symlinks/hidden and credential paths, filters known secret formats, and opens
originals read-only. Private DB/directory permissions are enforced. Only an
explicit private embedding route is allowed; proxies, redirects and public
provider fallback are disabled. Real corpus data never enters public artifacts.

The future service needs a scoped read-only source identity, a distinct retrieval
authentication boundary, and permission filtering before ranking, excerpts and
model input. Do not treat Mímir's single owner bearer as a multi-user ACL system.
Credential provisioning and production rollout require their own approval.

Index jobs must be bounded, resumable and low priority, with cancellation and
memory/disk ceilings. No competing model runtime or inference admission bypass
is introduced. Incomplete units are not cached; successfully completed batches
can be reused after interruption. Health must distinguish source freshness,
index coverage and model availability.

## Scope of this implementation

The original verified CLI spike runs on the laptop and calls the private M5 runtime. It
implements extraction, a local hash-checked source adapter, vector caching,
lexical/vector/hybrid retrieval and a fixed-query comparison. The subsequent
[snapshot adapter](artifact-source-snapshots.md) provides complete pinned local
and HTTP snapshots. The [standalone runtime](artifact-search-runtime.md) adds an
owner-authenticated lexical API, trusted freshness handoff, resumable indexing
and exact-release packaging. It is ready for separately approved installation;
no live M5 search deployment is claimed. Held-out owner review and quality
evaluation remain pending; OCR and per-user ACLs remain out of scope.

The bounded next work is specified in [artifact-search-followups.md](artifact-search-followups.md).
Run instructions and measured limitations are in [artifact-search-poc.md](artifact-search-poc.md).
