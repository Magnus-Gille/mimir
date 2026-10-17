# Documentation index

AGENTS keeps behavioral and safety rules inline. Use this page for the
repository's lookup-only documentation map.

- `docs/agent-reference.md` — endpoint map, project structure, operator command examples, checked-in unit files, deployment `.env` example, and the full environment-variable catalog.
- `docs/offsite-backup.md` — encrypted offsite backup setup, crypt remote/key custody, retention, verification, disaster recovery, and the `mimir-offsite.service` / `mimir-offsite.timer` flow.
- `docs/heimdall-freshness-surface.md` — Heimdall probe surface contract, permissions, freshness/error classification, and install/remove steps.
- `docs/relocation.md` — ADR-007 relocation boundary, read-only hook bindings, evidence receipts, drain/compensate rules, and the routing point for machine-readable relocation/provenance questions.
- `docs/artifact-search-poc.md` — independent local CLI for the #40 artifact-search spike, exact source locations, private index handling, validation and follow-up ownership.
- `docs/artifact-search-architecture.md` — #40 ownership and placement decision, source/index/model boundaries, update and access contracts.
- `docs/artifact-search-followups.md` — published implementation issues for the source adapter, M5 runtime and agent retrieval.
- `docs/artifact-source-snapshots.md` — #41 immutable source manifest/object contract, local/HTTP acquisition, identity/delta behavior and acceptance boundary.
- `docs/artifact-search-runtime.md` — standalone search service, trusted freshness handoff, authenticated provenance, recovery and immutable package acceptance.
- `docs/artifact-search-client.md` — SSH client, CLI and agent command adapter, provenance checks, failure states and activation boundary.
- `docs/artifact-search-operations.md` — first-install source publication, trusted refresh, edge authentication, user units, sync protection, and the gateway revision drift alert and re-pin.
- `docs/artifact-search-semantic.md` — opt-in vector/hybrid runtime modes: vector cache placement, gateway route, explicit lexical fallback, abstention, configuration and the fresh blind evaluation plan.
- `docs/artifact-search-evaluation.md` — held-out label provenance, approval, calibration separation and reproducible metrics.
- `docs/workload-requirement-v1.json` — machine-readable relocation requirement record.
- `docs/workload-requirement-v1.provenance.json` — pinned Grimnir source revision and SHA-256 digests for vendored contract artifacts.
