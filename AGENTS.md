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

### Endpoints

| Endpoint | Method | Auth | Purpose |
|----------|--------|------|---------|
| `/health` | GET | None | Health check |
| `/files/*` | GET | Bearer | Serve file from archive |
| `/list/*` | GET | Bearer | JSON directory listing |
| `/share/:token` | GET | None (HMAC token) | Temporary public file sharing |

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

## Project structure

```
mimir/
├── package.json
├── tsconfig.json
├── AGENTS.md              # This file
├── mimir.service          # systemd unit file
├── src/
│   ├── index.ts           # Express server
│   ├── share-token.ts     # HMAC token generation + validation
│   ├── secret-scan.ts     # Ingest-time secret scan + quarantine (mimir#13)
│   ├── heimdall-report.ts # Periodic self-report + panel push helper
│   └── cli/
│       ├── share.ts       # Pi-side CLI for generating share URLs
│       └── secret-scan.ts # CLI wrapper for the ingest secret scan
├── tests/
│   ├── server.test.ts     # supertest integration tests
│   ├── share-token.test.ts # Token unit tests
│   └── secret-scan.test.ts # Secret scan + quarantine unit tests
└── scripts/
    ├── deploy-nas.sh           # Deploy to NAS Pi
    ├── share.sh                # Generate share URL (sync + ssh + clipboard)
    ├── sync-artifacts.sh       # Manual rsync ~/mimir/ from laptop to NAS
    ├── sync-artifacts-daemon.sh # Launchd daemon wrapper (auto-sync)
    ├── backup-artifacts.sh     # Backup artifacts SD→NAS disk (cron on Pi)
    └── offsite-backup.sh       # Encrypted push to cloud (rclone crypt; systemd timer)
```

Offsite backup also ships `mimir-offsite.service` + `mimir-offsite.timer` (systemd
units, repo root) and `docs/offsite-backup.md` (setup + runbook + disaster recovery).

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

The generic Linux service expects `.env` at `/home/mimir/mimir-server/.env`:
```
MIMIR_API_KEY=<generate with: openssl rand -hex 32>
MIMIR_ROOT_DIR=/home/mimir/mimir
MIMIR_ALLOWED_HOSTS=files.example.com
MIMIR_TRUST_PROXY=loopback
```

### Reverse proxy or tunnel

- **Public URL:** Choose an environment-specific hostname such as `https://files.example.com`
- **Edge policy:** Require service or user authentication for protected routes
- **Origin:** Keep the default loopback bind and prevent direct access

Do not commit tunnel IDs, service-token names, Cloudflare secrets, or private
network addresses.

## Syncing files from laptop

**Automatic:** Run `scripts/sync-artifacts-daemon.sh` from a scheduler. It checks target reachability before syncing and skips if offline.

```bash
MIMIR_NAS=archive@files.internal ./scripts/sync-artifacts-daemon.sh
```

**Manual:**
```bash
./scripts/sync-artifacts.sh [hostname-or-ip]
```

Syncs `~/mimir/` to `~/mimir/` on the NAS Pi. Symmetric paths on both machines — no excludes needed.

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

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MIMIR_PORT` | `3031` | HTTP server port |
| `MIMIR_HOST` | `127.0.0.1` | Bind address (localhost for tunnel) |
| `MIMIR_API_KEY` | — | Bearer token (required) |
| `MIMIR_ROOT_DIR` | `./data` | Root directory to serve |
| `MIMIR_ALLOWED_HOSTS` | — | Extra allowed Host headers (comma-separated) |
| `MIMIR_TRUST_PROXY` | `false` | Explicit trusted-proxy value or hop count |
| `MIMIR_RATE_LIMIT` | `60` | Max requests per minute per IP |
| `MIMIR_INSTANCE_ID` | `default` | Stable instance identity reported to Heimdall |
| `MIMIR_DEPLOY_HOST` | `localhost` | Deployment host label reported to Heimdall |
| `MIMIR_SYNC_MAX_DELETE` | `1000` | Abort laptop→NAS mirror at or above this many deletions |
| `MIMIR_SYNC_MAX_DELETE_PCT` | `20` | Abort mirror above this percentage of the actual remote population |
| `MIMIR_SYNC_STATE_DIR` | `$XDG_STATE_HOME/mimir` or `~/.local/state/mimir` | Durable out-of-tree staging for unverified inbox imports |
| `MIMIR_BACKUP_LOG` | `$XDG_STATE_HOME/mimir/backup.log` or `~/.local/state/mimir/backup.log` | Local backup log, kept outside the deployed code tree |
| `MIMIR_SHARE_SECRET` | — | HMAC secret for share links (optional, enables `/share`) |
| `MIMIR_BASE_URL` | `http://127.0.0.1:3031` | Base URL for generated share links (CLI only) |
| `MIMIR_OFFSITE_REMOTE` | `mimir-crypt` | rclone crypt remote name (offsite backup) |
| `MIMIR_OFFSITE_ROOT` | `$HOME/mimir` | Directory pushed offsite |
| `MIMIR_OFFSITE_RETENTION_DAYS` | `30` | Archive (deleted/changed file) prune horizon |
| `MIMIR_OFFSITE_MAX_DELETE` | `1000` | Abort a run that would delete more than this many files |
| `MIMIR_OFFSITE_MAX_DELETE_PCT` | `25` | ...or more than this % of `current/` (whichever trips first) |
| `MIMIR_OFFSITE_STATE_DIR` | `$XDG_STATE_HOME/mimir` or `~/.local/state/mimir` | Deployment-stable heartbeat/log directory |
| `MIMIR_QUARANTINE_DIR` | `<target-dir>-quarantine` | Where ingest secret-scan hits are moved (see below) |
| `HEIMDALL_HUB_URL` / `HEIMDALL_FLEET_TOKEN` | — | Heimdall panel push — periodic self-report and secret-scan `fail` alerts |

## Sharing files

Generate a temporary public URL for any file in `~/mimir/`:

```bash
./scripts/share.sh ~/mimir/presentations/deck.pdf       # 24h default
./scripts/share.sh ~/mimir/presentations/deck.pdf 7d    # custom TTL
./scripts/share.sh presentations/deck.pdf 1h             # relative path ok
```

The script: syncs the file to Pi, generates an HMAC-signed token on the Pi, prints the URL and copies to clipboard. TTL formats: `1h`, `6h`, `12h`, `24h`, `3d`, `7d`.

**Requires:** `MIMIR_SHARE_SECRET` in the Pi's `.env` file. Generate with `openssl rand -hex 32`.

**Edge policy:** If `/share/*` bypasses proxy authentication, the HMAC token is the only recipient credential. Tokens can leak through browser history and access logs, and are limited to seven days.

## Offsite backup (cloud)

The third copy in a 3-2-1 strategy: `scripts/offsite-backup.sh` pushes `~/mimir/`
to cloud object storage as a **client-side-encrypted** copy via an `rclone crypt` remote (contents
*and* filenames encrypted — required for any sensitive archive; the script fails
*closed* if the remote isn't a verified crypt). Runs on the Pi via `mimir-offsite.timer`
(daily). Mirrors `current/` and keeps 30 days of deleted/changed versions in tagged,
seven-character per-run sibling dirs, pruned **by name** (`--backup-dir`, never destructive).
Guards: a preflight delete-count gate (+ `--max-delete`) aborts an implausible wipe.
Emits a heartbeat stamp and a `pass`/`fail` Heimdall panel. The mirror is fail-loud;
archive pruning is best-effort (warns, still `pass`).

This is the **reference implementation** of the Grimnir offsite-backup pattern
(mimir#9); munin-memory#172 and brokkr#1 copy-adapt it (each with its **own** crypt key
— never shared). Full setup, key custody, verification, and disaster-recovery steps:
[`docs/offsite-backup.md`](docs/offsite-backup.md).

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
