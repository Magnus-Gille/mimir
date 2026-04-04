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
npm test          # 47 tests
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

## Security

- Path traversal prevention (resolve + startsWith jail)
- Timing-safe token comparison
- Rate limiting per IP
- DNS rebinding protection
- Security headers (CSP, X-Frame-Options, X-Content-Type-Options, noindex)
- Dotfiles hidden from listings
- systemd sandboxing on the Pi

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
└── cli/
    └── share.ts       # CLI for generating share URLs
tests/
├── server.test.ts     # Integration tests (supertest)
└── share-token.test.ts
scripts/
├── deploy-nas.sh      # Deploy to remote host
├── share.sh           # Generate share URL
├── sync-artifacts.sh  # Rsync files to remote host
└── backup-artifacts.sh
```

## License

MIT
