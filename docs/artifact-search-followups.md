# Mímir #40 follow-up implementation issues

The approved drafts below were published in `Magnus-Gille/mimir` on 2026-10-09.

- [#41 — Build the Mímir source adapter and immutable snapshot contract](https://github.com/Magnus-Gille/mimir/issues/41)
- [#42 — Package the separately deployable M5 search runtime](https://github.com/Magnus-Gille/mimir/issues/42), blocked by #41.
- [#43 — Integrate agent retrieval and evaluate the corpus](https://github.com/Magnus-Gille/mimir/issues/43), blocked by #41 and #42.

All three are sub-issues of #40. Their published bodies match the approved drafts
below. Production rollout requires separate approval.

## 1. Build the Mímir source adapter and immutable snapshot contract

**Target repository:** `Magnus-Gille/mimir`
**Parent:** #40
**Filed by:** mimir

### Problem

The future search component needs a complete, repeatable view of Mímir's artifact
corpus without coupling the archive to a laptop mirror or requiring a live NAS
filesystem. The adapter must distinguish a complete inventory from an unavailable
or partial one, and must account for additions, changes, renames, and deletions.

### Acceptance and test checks

- Produce a versioned, immutable snapshot manifest with stable artifact identity,
  relative path, size, modification metadata, and content byte hash.
- Complete inventory/snapshot runs discover the corpus deterministically; a missing,
  interrupted, or unavailable inventory fails closed and cannot publish a snapshot.
- Incremental runs detect added, changed, renamed, and deleted artifacts, with tests
  covering each transition and no-overwrite/collision behavior.
- Verify fetched bytes against the manifest hash before a snapshot is accepted;
  mismatches fail the run and identify the affected artifact.
- Tests run against a local fixture and a NAS-like remote fixture, with the adapter
  contract demonstrating localhost and NAS independence.
- Document how a later indexer pulls an authorized immutable snapshot from Mímir;
  do not introduce a laptop-to-M5 archive mirror.

### Ordering

This is dependency 1. Evaluation fixtures can start in parallel, but the M5 runtime
depends on this snapshot contract.

### Out of scope

No search index, vector or embedding generation, M5 service, new Mímir Express search
endpoint, deployment, credential provisioning, or production rollout.

## 2. Package the separately deployable M5 search runtime

**Target repository:** `Magnus-Gille/mimir`
**Parent:** #40
**Filed by:** mimir

### Problem

Mímir needs a separately packaged search component that extracts and indexes
authorized immutable snapshots while keeping model serving in `gille-inference`.
The runtime needs owner-only access, bounded operational behavior, and a recoverable
index lifecycle without turning Mímir into a search service.

### Acceptance and test checks

- Package a standalone M5 search service with a private persistent index and an
  authenticated owner-only API; expose no new Mímir Express search endpoint.
- Pull only authorized immutable snapshots from the source-adapter contract and
  reject incomplete, unavailable, or hash-invalid snapshots.
- Implement resource-bounded, low-priority, resumable index jobs with explicit
  cancellation/retry behavior and tests for restart and interruption.
- Expose health and index metadata sufficient to report source identity, indexed
  snapshot, corpus counts, job state, and freshness; stale or failed state is
  observable and fails closed for retrieval.
- Provide tested restore and full-rebuild procedures from a snapshot, including
  corruption or missing-index recovery.
- Bind deployment acceptance to an exact immutable release and rollback checks for the packaged runtime,
  with deterministic tests; this issue does not authorize deployment.
- Keep the model-serving/API boundary in `gille-inference`; the local prototype has
  lexical/vector/hybrid modes. Published quality evidence belongs in the PoC report,
  not an assumed production acceptance.

### Ordering

This is dependency 2 and follows the source adapter. Evaluation fixtures can start
early and run independently.

### Out of scope

No laptop-to-M5 archive mirror, new Mímir Express endpoint, production deployment,
new credentials, model runtime reimplementation, or claim of vector quality/results.

## 3. Integrate agent retrieval and evaluate the corpus

**Target repository:** `Magnus-Gille/mimir`
**Parent:** #40
**Filed by:** mimir

### Problem

Agents need a retrieval path with explicit authorization, provenance, and measurable
quality. The integration must preserve Mímir's bearer-auth model and avoid exposing
bearer credentials in result URLs. Evaluation must use independent held-out labels
instead of inferred or self-confirming results.

### Acceptance and test checks

- Define and test the authenticated retrieval reference contract: authorization is
  checked before retrieval, and result URLs never contain bearer tokens.
- Integrate agent retrieval with exact provenance for every result, including source
  snapshot identity, artifact path or stable identity, byte hash, and retrieval
  decision metadata.
- Build independent held-out positive, miss, and distractor labels from real corpus
  tasks; document fixture provenance and prevent index/training leakage into labels.
- Report reproducible lexical, vector, and hybrid metrics when each implementation
  is available, with query-level results and a clear baseline. The local prototype
  supplies all three modes; this draft does not assume they meet production gates.
- Verify candidate selection and fusion on duplicate-rich distractors: the spike's
  fixed RRF60 did not preserve two vector-only gains. Define independent miss and
  abstention acceptance criteria; neither semantic mode passed the spike's two
  negative cases at its untuned zero cosine floor.
- Exercise permission-denied, missing, stale, hash-mismatch, and unavailable-source
  cases, and verify that failures do not disclose protected artifact content.
- For justified OCR or image-representation misses, record the evidence and route
  model-runtime work to `gille-inference#410`; do not reimplement model serving in
  Mímir.

### Ordering

This is dependency 3 and follows the source adapter and M5 runtime. Evaluation
fixtures and held-out label design can start early, before the runtime is complete.

### Out of scope

No new Mímir Express search endpoint, bearer token in a result URL, laptop-to-M5
archive mirror, model-serving reimplementation, production deployment, new
credentials, or rollout approval.
