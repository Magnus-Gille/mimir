# Mímir — CLAUDE.md

## What this project is

Mímir is a self-hosted authenticated file server for the Jarvis personal AI system. Named after the Norse figure of wisdom. Serves documents, presentations, PDFs, and images over HTTPS with Bearer token auth.

Part of the Hugin & Munin system: **Munin** (memory/brain), **Mímir** (file archive), **Hugin** (signal hunter).

## Architecture

- **Runtime:** Node.js 20+, TypeScript (strict mode)
- **Framework:** Express (minimal — static file serving + auth + directory listing)
- **Auth:** Bearer token (`MIMIR_API_KEY`), timing-safe comparison
- **Deployment:** NAS Pi (Pi 2), Cloudflare Tunnel, systemd
- **Storage:** `/home/magnus/artifacts/` on SD card, backed up to `/mnt/timemachine/backups/mimir/`

### Endpoints

| Endpoint | Method | Auth | Purpose |
|----------|--------|------|---------|
| `/health` | GET | None | Health check |
| `/files/*` | GET | Bearer | Serve file from archive |
| `/list/*` | GET | Bearer | JSON directory listing |

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
│   └── index.ts           # Express server — all in one file
├── tests/
│   └── server.test.ts     # supertest integration tests
└── scripts/
    ├── deploy-nas.sh           # Deploy to NAS Pi
    ├── sync-artifacts.sh       # Manual rsync mgc/ from laptop to NAS
    ├── sync-artifacts-daemon.sh # Launchd daemon wrapper (auto-sync)
    └── backup-artifacts.sh     # Backup artifacts SD→NAS disk (cron on Pi)
```

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

The NAS Pi needs a `.env` file at `/home/magnus/mimir/.env`:
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

Syncs `~/mgc/` to `/home/magnus/artifacts/mgc/` on the NAS Pi.

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MIMIR_PORT` | `3031` | HTTP server port |
| `MIMIR_HOST` | `127.0.0.1` | Bind address (localhost for tunnel) |
| `MIMIR_API_KEY` | — | Bearer token (required) |
| `MIMIR_ROOT_DIR` | `/home/magnus/artifacts` | Root directory to serve |
| `MIMIR_ALLOWED_HOSTS` | — | Extra allowed Host headers (comma-separated) |
| `MIMIR_RATE_LIMIT` | `60` | Max requests per minute per IP |

## Key design decisions

- Single-file server (~200 lines) — no need for complexity
- No MCP — plain HTTP is universally accessible. MCP can be added later if needed (via Munin proxy)
- No upload endpoint — files arrive via rsync from laptop
- Range request support for large PDFs (streaming)
- Artifacts on SD card (51GB free), backed up to NAS disk hourly
- Separate from Time Machine mount to keep backups safe
