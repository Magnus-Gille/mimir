# Mímir — CLAUDE.md

## What this project is

Mímir is a self-hosted authenticated file server for the Grimnir personal AI system. Named after the Norse figure of wisdom. Serves documents, presentations, PDFs, and images over HTTPS with Bearer token auth.

Part of the Grimnir system: **Munin** (memory/brain), **Mímir** (file archive), **Hugin** (task dispatcher).

## Architecture

- **Runtime:** Node.js 20+, TypeScript (strict mode)
- **Framework:** Express (minimal — static file serving + auth + directory listing)
- **Auth:** Bearer token (`MIMIR_API_KEY`), timing-safe comparison
- **Deployment:** NAS Pi (Pi 2), Cloudflare Tunnel, systemd
- **Storage:** `~/mimir/` on both laptop and Pi (symmetric), backed up to `/mnt/timemachine/backups/mimir/`. Mímir owns this rsync (`backup-artifacts.sh`); the **destination disk** — its mount, capacity, Samba/Time Machine share, and hardware health — is owned by **Brokkr**, the platform/substrate layer ([brokkr](https://github.com/Magnus-Gille/brokkr) repo). Boundary: Mímir guarantees *what* gets backed up; Brokkr guarantees the disk it lands on is mounted, healthy, and has headroom.
- **Server code:** `~/mimir-server/` on Pi (separate from artifacts)

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

1. **Cloudflare Access** — Service Token (`munin-memory-mcp`) required at edge. CF Access app: `mimir.gille.ai`
2. **Bearer token** — `MIMIR_API_KEY` at origin, timing-safe comparison
3. **App hardening:**
   - Path traversal prevention (resolve + startsWith jail to root dir)
   - Rate limiting (60 req/min per IP)
   - DNS rebinding protection via allowed hosts
   - Security headers (X-Content-Type-Options, X-Frame-Options, CSP, X-Robots-Tag)
   - Dotfiles hidden from directory listings
   - systemd sandboxing (ProtectSystem=strict, ReadOnlyPaths for artifacts, NoNewPrivileges)

## Project structure

```
mimir/
├── package.json
├── tsconfig.json
├── CLAUDE.md              # This file
├── mimir.service          # systemd unit file
├── src/
│   ├── index.ts           # Express server
│   ├── share-token.ts     # HMAC token generation + validation
│   └── cli/
│       └── share.ts       # Pi-side CLI for generating share URLs
├── tests/
│   ├── server.test.ts     # supertest integration tests
│   └── share-token.test.ts # Token unit tests
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

Default host: `100.99.119.52` (NAS Pi via Tailscale).

The NAS Pi needs a `.env` file at `/home/magnus/mimir-server/.env`:
```
MIMIR_API_KEY=<generate with: openssl rand -hex 32>
MIMIR_ALLOWED_HOSTS=mimir.gille.ai
```

### Tunnel infrastructure

- **Tunnel ID:** `9e8bc8af-dcf6-459d-90ed-f014c714b7d2`
- **cloudflared:** v2026.3.0, systemd service (enabled), config at `/etc/cloudflared/config.yml`
- **CF Access App:** `mimir.gille.ai` with Service Token Auth policy (reuses `munin-memory-mcp` token)
- **DNS:** CNAME `mimir.gille.ai` → tunnel
- **Public URL:** `https://mimir.gille.ai`

## Syncing files from laptop

**Automatic (launchd):** Runs every 30 minutes via `com.magnusgille.mimir-sync` launch agent. Checks NAS reachability before syncing — skips silently if offline.

```bash
# Manage the agent
launchctl list | grep mimir          # Check status
launchctl start com.magnusgille.mimir-sync  # Trigger manual sync
cat ~/.local/share/mimir/logs/sync-stdout.log  # View logs
```

**Manual:**
```bash
./scripts/sync-artifacts.sh [hostname-or-ip]
```

Syncs `~/mimir/` to `~/mimir/` on the NAS Pi. Symmetric paths on both machines — no excludes needed.

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MIMIR_PORT` | `3031` | HTTP server port |
| `MIMIR_HOST` | `127.0.0.1` | Bind address (localhost for tunnel) |
| `MIMIR_API_KEY` | — | Bearer token (required) |
| `MIMIR_ROOT_DIR` | `/home/magnus/mimir` | Root directory to serve |
| `MIMIR_ALLOWED_HOSTS` | — | Extra allowed Host headers (comma-separated) |
| `MIMIR_RATE_LIMIT` | `60` | Max requests per minute per IP |
| `MIMIR_SHARE_SECRET` | — | HMAC secret for share links (optional, enables `/share`) |
| `MIMIR_BASE_URL` | `https://mimir.gille.ai` | Base URL for generated share links (CLI only) |
| `MIMIR_OFFSITE_REMOTE` | `mimir-crypt` | rclone crypt remote name (offsite backup) |
| `MIMIR_OFFSITE_ROOT` | `/home/magnus/mimir` | Directory pushed offsite |
| `MIMIR_OFFSITE_RETENTION_DAYS` | `30` | Archive (deleted/changed file) prune horizon |
| `MIMIR_OFFSITE_MAX_DELETE` | `1000` | Abort a run that would delete more than this |

## Sharing files

Generate a temporary public URL for any file in `~/mimir/`:

```bash
./scripts/share.sh ~/mimir/presentations/deck.pdf       # 24h default
./scripts/share.sh ~/mimir/presentations/deck.pdf 7d    # custom TTL
./scripts/share.sh presentations/deck.pdf 1h             # relative path ok
```

The script: syncs the file to Pi, generates an HMAC-signed token on the Pi, prints the URL and copies to clipboard. TTL formats: `1h`, `6h`, `12h`, `24h`, `3d`, `7d`.

**Requires:** `MIMIR_SHARE_SECRET` in the Pi's `.env` file. Generate with `openssl rand -hex 32`.

**CF Access:** The `/share/*` path needs a Cloudflare Access bypass policy (Allow Everyone) since recipients don't have service tokens. The HMAC token provides authentication instead.

## Offsite backup (cloud)

The third copy in a 3-2-1 strategy: `scripts/offsite-backup.sh` pushes `~/mimir/`
to OneDrive as a **client-side-encrypted** copy via an `rclone crypt` remote (contents
*and* filenames encrypted — required because `mgc/` is client data). Runs on the Pi via
`mimir-offsite.timer` (daily). Mirrors `current/` and keeps 30 days of deleted/changed
versions in `archive/<date>/` (`--backup-dir`, never destructive), with a `--max-delete`
guard, a heartbeat stamp, and a `pass`/`fail` Heimdall panel. Fail-loud throughout.

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
