# Mimir

Self-hosted authenticated file server for personal AI systems. Serves documents, presentations, PDFs, and images over HTTPS with Bearer token auth.

Named after the Norse figure of wisdom. Part of the [Grimnir](https://github.com/Magnus-Gille) system — where **Munin** is memory, **Mimir** is the file archive, and **Hugin** is the task dispatcher.

## How it works

AI agents query **Munin** for document context (summaries, extracted text). When an agent needs the full file, it follows the Mimir URL from the Munin entry. Plain HTTP — no MCP required.

```
Agent → Munin (discovery) → Mimir URL → full file
```

## Endpoints

| Endpoint | Method | Auth | Purpose |
|----------|--------|------|---------|
| `/health` | GET | None | Health check |
| `/files/*` | GET | Bearer | Serve a file |
| `/list/*` | GET | Bearer | JSON directory listing |
| `/share/:token` | GET | None (HMAC token) | Temporary public link |

## Quick start

```bash
npm install
npm run build

# Run locally with test fixtures
MIMIR_API_KEY=dev-key MIMIR_ROOT_DIR=./tests/__test_fixtures__ npm run dev
```

```bash
# Fetch a file
curl -H "Authorization: Bearer dev-key" http://localhost:3031/files/example.txt

# List a directory
curl -H "Authorization: Bearer dev-key" http://localhost:3031/list/
```

## Share URLs

Generate temporary public URLs for files without requiring the recipient to have an API key:

```bash
./scripts/share.sh ~/mimir/presentations/deck.pdf       # 24h default
./scripts/share.sh ~/mimir/presentations/deck.pdf 7d    # custom TTL
```

TTL formats: `1h`, `6h`, `12h`, `24h`, `3d`, `7d`. Requires `MIMIR_SHARE_SECRET` on the server.

## Testing

```bash
npm test          # 97 tests
npm run test:watch
```

## Deployment

Designed to run behind a reverse proxy or tunnel (e.g., Cloudflare Tunnel). Binds to localhost by default.

```bash
./scripts/deploy-nas.sh [hostname-or-ip]
```

The remote host needs a `.env` file with at least `MIMIR_API_KEY` set.

### Syncing files

Files arrive via rsync from `~/mimir/` on the laptop — no upload endpoint.

**Automatic:** Launchd agent runs every 30 minutes (`com.magnusgille.mimir-sync`).

**Manual:**
```bash
./scripts/sync-artifacts.sh [hostname-or-ip]
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `MIMIR_API_KEY` | — | Bearer token (required) |
| `MIMIR_ROOT_DIR` | `/home/magnus/mimir` | Root directory to serve |
| `MIMIR_PORT` | `3031` | HTTP server port |
| `MIMIR_HOST` | `127.0.0.1` | Bind address |
| `MIMIR_ALLOWED_HOSTS` | — | Extra allowed Host headers (comma-separated) |
| `MIMIR_RATE_LIMIT` | `60` | Max requests per minute per IP |
| `MIMIR_SHARE_SECRET` | — | HMAC secret for share links (optional) |
| `MIMIR_BASE_URL` | `https://mimir.gille.ai` | Base URL for generated share links |
| `MIMIR_QUARANTINE_DIR` | `<root>-quarantine` | Where ingest-time secret-scan hits are moved |
| `HEIMDALL_HUB_URL` / `HEIMDALL_FLEET_TOKEN` | — | Heimdall panel push (status reporting + secret-scan alerts) |

## Security

- Path traversal prevention (resolve + startsWith jail)
- Timing-safe token comparison
- Rate limiting per IP
- DNS rebinding protection
- Security headers (CSP, X-Frame-Options, X-Content-Type-Options, noindex)
- Dotfiles hidden from listings
- systemd sandboxing on the Pi
- Ingest-time secret scan (see below) — hits are quarantined before they can be served

### Secret scanning on ingest

Every rsync inbox import is scanned for known secret formats (AWS/GitHub/Slack/Stripe/Google
keys, private key blocks, JWTs, generic quoted `key=value` assignments) before the file is
mirrored to the NAS's Bearer-servable tree. A hit is moved to a quarantine directory
(`MIMIR_QUARANTINE_DIR`, default `<root>-quarantine`) and logged loudly; if `HEIMDALL_HUB_URL`
/ `HEIMDALL_FLEET_TOKEN` are set, a `fail`-state Heimdall panel is also pushed. See
`src/secret-scan.ts`.

## Design decisions

- **Single-file server** (~300 lines) — no need for complexity at this scale
- **No upload endpoint** — files arrive via rsync
- **No MCP** — plain HTTP is universally accessible
- **No database** — the filesystem is the source of truth

## Project structure

```
src/
├── index.ts           # Express server
├── share-token.ts     # HMAC token generation + validation
├── secret-scan.ts     # Ingest-time secret scan + quarantine
├── heimdall-report.ts # Periodic self-report + panel push helper
└── cli/
    ├── share.ts       # CLI for generating share URLs
    └── secret-scan.ts # CLI wrapper for the ingest secret scan
tests/
├── server.test.ts     # Integration tests (supertest)
├── share-token.test.ts
└── secret-scan.test.ts
scripts/
├── deploy-nas.sh      # Deploy to remote host
├── share.sh           # Generate share URL
├── sync-artifacts.sh  # Rsync files to remote host (scans on import)
└── backup-artifacts.sh
```

## License

MIT
