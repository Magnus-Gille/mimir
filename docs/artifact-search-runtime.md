# Standalone snapshot search runtime

The separately packaged Python service consumes the #41 source contract. It does
not add an Express route, provision credentials, install a service or deploy to M5.
Lexical retrieval is the default. Opt-in `vector` and `hybrid` modes, which
are off unless the operator enables them, are specified in
[artifact-search-semantic.md](artifact-search-semantic.md). They are not accepted
as production-quality retrieval.
Python 3.10+ with SQLite FTS5 is required; PDF extraction also requires Poppler.

## Trusted freshness handoff

An immutable snapshot proves bytes, not that they are the newest bytes. The owner
must supply a private, owner-owned `0600` JSON handoff through a trusted publisher
path, atomically replacing it after a successful complete source observation.
Never renew it from a cached index or simply extend its deadline when the source
cannot be observed. Automatic producer installation is a separately approved
operational action. The maximum observation lease is one hour.

```json
{
  "schema": "mimir-search-handoff-v1",
  "source_id": "11111111-1111-4111-8111-111111111111",
  "snapshot_id": "<64 lowercase hex characters>",
  "observed_at": 0,
  "expires_at": 3600,
  "kind": "http",
  "bundle": "https://files.example.com/files/snapshots/<snapshot-id>",
  "original": "https://files.example.com/files/presentations",
  "mimir_origin": "https://files.example.com",
  "original_prefix": "presentations"
}
```

Times above are placeholders for Unix seconds; this example is deliberately
expired. Snapshot/source pins, original prefix and HTTPS origin are trusted owner
inputs, never request parameters. The original and bundle must share one origin.
`MIMIR_API_KEY` is supplied through the existing private environment mechanism;
it is never a command argument or URL. `kind: local` is for local fixtures or an
explicitly approved co-located source: `bundle` and `original` are absolute paths.
It does not create a laptop mirror. Scope/binding changes require separate state
and source identity; backward observations are rejected against the saved job.

The runtime rereads the handoff at job and query boundaries and after source
verification. Expired/missing handoffs, changed snapshot pins, incomplete jobs,
failed jobs and corrupt indexes prevent retrieval. Every returned original is
read through the authorized source path and hash-checked before any result is
returned; one failure suppresses the entire response. A reference remains a
point-in-time observation, not a guarantee against a later source edit.

## Job lifecycle and recovery

Use explicit private paths outside the served archive:

```bash
python3 -m tools.artifact_search.runtime serve \
  --state /private/path/search-state --handoff /private/path/handoff.json
```

The service binds only `127.0.0.1:8791`. A distinct, already-provisioned
`MIMIR_SEARCH_API_KEY` is required for all API reads. No remote listener or edge
policy is installed here. The packaged launcher additionally requires
`MIMIR_SEARCH_RELEASE` to match its exact release commit.

One low-priority child processes one artifact per step, committing it atomically.
A killed step resumes from completed artifact transactions. A complete index is
accepted only when all manifest records, including exclusions, are accounted for.
Images and empty text are explicit coverage exclusions; extraction failures do
not silently accept a partial index. No derived original-tree mirror is retained.

The parent bounds each child to 90 seconds; CPU is limited to 60 seconds and file
size to 128 MiB. Linux also enforces a 768 MiB address-space limit. macOS lacks an
equivalent enforced memory cap here: input, unit, extracted-text, process count
and duration caps still apply. Only one request and one index child run at once.
Snapshot limits remain 20 MiB/file, 256 MiB/corpus and 10,000 entries. Index limits
include 50,000 units and one million extracted characters per artifact. State over
512 MiB, excluding the separately capped 128 MiB of optional vector caches, blocks
further job work. Pruning is explicit and never deletes originals.
HTTP pulls retain no-redirect/no-proxy behavior and source request pacing.

Transient source conditions — `source_unavailable`, HTTP 408/429/5xx and a lapsed
lease (`source_stale`) — keep the job `pending` with that error code, a
`transient_failures` count and a `retry_after` time. The worker backs off from 5 s
doubling to a 300 s cap and resumes by itself once the source answers or the
refresher installs a renewed lease. The job is never ready meanwhile, so retrieval
stays unavailable and rate exhaustion is still not an authorization bypass. Every
other failure (authorization, missing or mismatched objects, scope changes,
corrupt state) remains a terminal `failed` job. A job left `failed` with a
transient code by an earlier release resumes the same way.

Use the same `--state` and `--handoff` for these local control commands:

- `status`: bounded health JSON; no content or credentials.
- `cancel`: stop at the next artifact boundary; retain committed work.
- `retry`: resume a failed/cancelled job after the underlying cause is fixed;
  it also clears any transient backoff, as `cancel` and `rebuild` do.
- `rebuild`: preserve the prior database under a `.saved-*` name and rebuild from
  the pinned source. This is also missing/corrupt-index recovery and full restore.

Commands serialize through a private file lock. Retry never manufactures a new
freshness lease. A worker timeout is cancelled and requires explicit retry.
Old snapshot databases and saved recovery files are retained; inspect and prune
only with normal explicit deletion authorization. Restoring a database alone is
insufficient: the current valid source handoff and original hashes must agree.

## Authenticated retrieval contract

All routes require `Authorization: Bearer <owner-search-key>`. Supply the header
using the caller's configured credential mechanism, never by embedding it in a
shell command or result URL. Only the exact loopback Host is accepted.

- `GET /health` and `/index`: source/snapshot IDs, coverage counts, job state,
  observed/expiry times, readiness, package release and package checksum. HTTP 503
  means not ready; authentication failures reveal no index metadata. Readiness
  covers the index and trusted handoff only. It cannot certify every original;
  originals are checked on retrieval and deployment acceptance also needs a probe.
- `POST /search`: JSON `{"query":"...","top":5}`, with an optional
  `"mode": "lexical"|"hybrid"|"vector"`. Up to five results and 4,096 query
  characters; the whole body is bounded to 8,192 bytes. No retrieval jobs,
  source URLs or mutable configuration are accepted through this API. A semantic
  mode that cannot be served returns lexical results labelled `lexical_fallback`
  with a `fallback_reason`.

Each result contains artifact ID/path/hash, source ID, snapshot ID, original
credential-free Mímir URL, physical page/slide/section locator, bounded title and
snippet, BM25 score and `verification: original_sha256_matched`. Response metadata
names the lexical/BM25 decision, observation deadline and whether it returned no
results.

**Compatibility contract.** A request that omits `mode`, while the configured
default is lexical, returns exactly this v1 shape with no added keys:

- response keys: `mode`, `decision`, `results`, `snapshot_id`, `source_id`,
  `observed_at`, `expires_at`, `verified_at`, `release`, `package_sha256`,
  `abstained`;
- result keys: `path`, `artifact_id`, `sha256`, `locator`, `title`, `snippet`,
  `score`, `mimir_url`, `source_id`, `snapshot_id`, `verification`.

The installed `mimir-search` client validates these exact sets. Sending any
explicit `mode`, including `"lexical"`, opts into the extended shape:
`requested_mode` plus per-result `score_kind`, and for semantic responses
`fallback_reason`, `abstention`, `embedding`, `candidate_window` and `cosine`. A
semantic configured default cannot be expressed in v1, so the client must accept
the extended shape before any default is changed. Scores are not confidence probabilities; empty results do not certify
that an answer is absent. Result references confer no file access: agents must
fetch through their own authorized Mímir path and verify the byte hash again.

Source unavailable, permission denied, stale, missing and hash-mismatch cases
return only `retrieval_unavailable`, never a partial excerpt or provider response.
Source details and query strings are not written to access logs. Owner-only access
is not a multi-tenant ACL implementation.

## Immutable release and rollback acceptance

Build from an explicitly reviewed 40-character commit, not the current checkout:

```bash
python3 -m tools.artifact_search.package_runtime build \
  --release <exact-40-character-commit> --package /private/path/search.pyz
```

The reproducible zipapp contains only the runtime and its standard-library source
dependencies, with per-file digests. The output receipt includes the package
SHA-256. Store the approved revision/checksum separately from the package. Before
an approved deployment, verify those expected values using the trusted verifier:

```bash
python3 -m tools.artifact_search.package_runtime verify \
  --release <approved-commit> --package /private/path/search.pyz \
  --sha256 <approved-package-sha256>
```

Run the approved package with `python3 /private/path/search.pyz serve ...` and the
existing private environment. After an explicitly approved installation, save an
authenticated health response and successful `/search` probe privately. The probe
must retrieve at least one known original; no-hit queries cannot prove the source
read path. Supply `--health-json <path> --retrieval-json <path> --snapshot-id <pin>
--source-id <uuid>` to the same verifier, using the independently approved source
and snapshot pins. Acceptance requires ready=true, the exact approved release and
package checksum in both responses, matching fresh handoffs, and a successful
original-hash verification no older than 30 seconds. It is point-in-time evidence,
not a guarantee against subsequent source changes. Merely building, starting a
process or seeing HTTP 200 is not deployment evidence. Development module runs
are not release acceptance.

Rollback uses the preserved previous package and its separately retained receipt,
then the same exact-release verification and fresh health acceptance. Stop the
service before switching packages. If index schema/pipeline differs, use a separate
private state directory and rebuild from a currently authorized snapshot; do not
silently reuse an incompatible database. These procedures provide the package and
acceptance contract; service installation, credential provisioning, trusted
publisher wiring and a live M5 smoke test remain operational prerequisites.
See [first-install preparation](artifact-search-operations.md) for the selected
publisher, trusted refresher, edge authentication and user-unit renderer.

## Verification and quality status

Run all `test_*.py` tests under `tools/artifact_search`. Fixtures cover transaction
resume, cancellation/retry, rebuild/recovery, source freshness, original changes,
authentication and provenance, package tampering and rollback acceptance. They do
not constitute deployment verification or real-corpus relevance acceptance.
See [artifact-search-evaluation.md](artifact-search-evaluation.md) for the held-out
label workflow and independent source-audit alternative to owner review.
