# Mímir — AGENTS.md

## What this project is

Mímir is a self-hosted authenticated file server for the Grimnir personal AI system. Named after the Norse figure of wisdom. Serves documents, presentations, PDFs, and images over HTTPS with Bearer token auth.

Part of the Grimnir system: **Munin** (memory/brain), **Mímir** (file archive), **Hugin** (task dispatcher).

## Architecture

- **Runtime:** Node.js 20+, TypeScript (strict mode)
- **Framework:** Express (minimal — static file serving + auth + directory listing)
- **Auth:** Bearer token (`MIMIR_API_KEY`), timing-safe comparison
- **Deployment:** Linux, an authenticated reverse proxy or private tunnel, and systemd
- **Storage:** A configurable local archive with optional local and encrypted offsite copies. Mímir owns artifact replication; the **destination disk** — mount, capacity, shares, and hardware health — belongs to the platform layer ([Brokkr](https://github.com/Magnus-Gille/brokkr) in the full ecosystem).
- **Server code:** Kept separate from the served artifact directory

### How agents use Mímir

Agents don't talk to Mímir directly via MCP. Instead:
1. Agent queries Munin for document context (summaries + extracted text in `documents/*` entries)
2. If the agent needs the full file, it follows the Mímir URL from the Munin entry
3. Only environments that can pass Bearer headers (Claude Code, Codex) can fetch full files
4. Web/Mobile agents get summaries from Munin — sufficient for ~90% of queries

### Security (2-layer, same model as Munin)

1. **Edge authentication** — An identity-aware proxy is recommended for non-local access
2. **Bearer token** — `MIMIR_API_KEY` at origin, timing-safe comparison
3. **App hardening:**
   - Path traversal prevention (lexical + realpath jail; external symlinks rejected)
   - Rate limiting (60 req/min per IP)
   - DNS rebinding protection via allowed hosts
   - Security headers (X-Content-Type-Options, X-Frame-Options, CSP, X-Robots-Tag)
   - Dotfiles hidden from directory listings
   - systemd sandboxing (ProtectSystem=strict, ReadOnlyPaths for artifacts, NoNewPrivileges)
   - Ingest-time secret scan (`src/secret-scan.ts`) — newly-imported inbox files are scanned
     for known secret formats before they reach the servable tree; hits are quarantined and
     alerted (Heimdall panel, or a loud log if the panel push isn't configured). See mimir#13.

## Reference docs

AGENTS keeps behavioral and safety rules inline. For lookup-only material, open
the single best-matching repo doc below. Exception: provenance or relocation
record questions should inspect the normative
`docs/workload-requirement-v1.json` and
`docs/workload-requirement-v1.provenance.json` records directly rather than
answering only from this index.

- `docs/agent-reference.md` — endpoint map, project structure, operator command examples, checked-in unit files, deployment `.env` example, and the full environment-variable catalog.
- `docs/offsite-backup.md` — encrypted offsite backup setup, crypt remote/key custody, retention, verification, disaster recovery, and the `mimir-offsite.service` / `mimir-offsite.timer` flow.
- `docs/heimdall-freshness-surface.md` — Heimdall probe surface contract, permissions, freshness/error classification, and install/remove steps.
- `docs/relocation.md` — ADR-007 relocation boundary, read-only hook bindings, evidence receipts, and drain/compensate rules.
- `docs/workload-requirement-v1.json` — machine-readable relocation requirement record.
- `docs/workload-requirement-v1.provenance.json` — pinned Grimnir source revision and SHA-256 digests for vendored contract artifacts.

## How to build

```bash
npm install
npm run build
```

## How to test

```bash
npm test
```

## How to run locally

```bash
MIMIR_API_KEY=dev-key MIMIR_ROOT_DIR=./tests/__test_fixtures__ npm run dev
```

## Deployment to NAS Pi

```bash
./scripts/deploy-nas.sh [hostname-or-ip]
```

The target host is environment-specific; pass it explicitly when deploying outside
the maintainer's machine. Do not commit private Tailscale IPs or hostnames.

Deployment requires a clean Git worktree. The script installs production dependencies
with `npm ci`, refreshes all Mímir systemd units, verifies health over loopback, and only
then atomically records the exact accepted commit in `.deployed-commit`. Before the first
remote code-tree mutation it captures the previous SHA for rollback, removes the acceptance
marker, and removes stale remote `.git` metadata; interrupted or rejected deployments stay
markerless. Source `.git` files and directories are excluded from transfer. The script
stops before code-tree mutation and reports marker state as unknown if the invalidation SSH
command itself has an indeterminate outcome. It prints a clean-worktree redeploy command
using the captured rollback target when available. The remote `.env` is enforced as mode
`0600` without displaying its values.

The checked-in service/unit path expectations and deployment `.env` example live
in `docs/agent-reference.md`.

### Reverse proxy or tunnel

- **Public URL:** Choose an environment-specific hostname such as `https://files.example.com`
- **Edge policy:** Require service or user authentication for protected routes
- **Origin:** Keep the default loopback bind and prevent direct access

Do not commit tunnel IDs, service-token names, Cloudflare secrets, or private
network addresses.

## Syncing files from laptop

Command examples for `scripts/sync-artifacts-daemon.sh` and
`scripts/sync-artifacts.sh` live in `docs/agent-reference.md`.

### Ingest secret scan

Every inbox import (`sync-artifacts.sh` / `sync-artifacts-daemon.sh` Step 1) first lands in
durable staging at `MIMIR_SYNC_STATE_DIR/import-pending`, outside `~/mimir/`. Every staged
file is passed to `src/cli/secret-scan.ts --stdin` on every invocation until scanning
succeeds. Clean files are then promoted with no-overwrite semantics; a collision remains
staged and blocks mirroring so neither copy is lost. Import, scan, or promotion failures
also leave staging intact and block this and later mirrors. Same detector class Munin uses
at write-time (known secret-format
regexes: AWS/GitHub/Slack/Stripe/Google keys, private key blocks, JWTs, generic quoted
`key=value` assignments), re-implemented locally in `src/secret-scan.ts` — not imported
across repos. A hit is moved to `MIMIR_QUARANTINE_DIR` (default `<root>-quarantine`,
outside the servable tree) and never reaches the NAS; the alert always logs loudly and
additionally pushes a `fail`-state Heimdall panel when `HEIMDALL_HUB_URL`/
`HEIMDALL_FLEET_TOKEN` are set. Manual full-tree audit: `node dist/cli/secret-scan.js
~/mimir` (omit `--stdin` to walk the whole tree).

## Sharing files

Command examples and TTL formats for `scripts/share.sh` live in
`docs/agent-reference.md`.

**Requires:** `MIMIR_SHARE_SECRET` in the Pi's `.env` file. Generate with `openssl rand -hex 32`.

**Edge policy:** If `/share/*` bypasses proxy authentication, the HMAC token is the only recipient credential. Tokens can leak through browser history and access logs, and are limited to seven days.

## Offsite backup (cloud)

Mímir's offsite copy must remain client-side encrypted and fail closed if the
remote is not a verified crypt. Full setup, retention, verification, disaster
recovery, and `scripts/offsite-backup.sh` / `mimir-offsite.*` details live in
`docs/offsite-backup.md`.

> **Boundary:** this is *cloud replication of Mímir's own artifacts* — a service
> concern, so it lives here. The destination **disk** and **Time Machine** stay Brokkr's
> (TM is a machine-level backup and does not go to cloud — see the doc/architecture).

## Key design decisions

- Single-file server (~200 lines) — no need for complexity
- No MCP — plain HTTP is universally accessible. MCP can be added later if needed (via Munin proxy)
- No upload endpoint — files arrive via rsync from laptop
- Range request support for large PDFs (streaming)
- Artifacts on SD card (51GB free), backed up to NAS disk hourly
- Separate from Time Machine mount to keep backups safe
