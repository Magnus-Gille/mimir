# Mímir

Self-hosted authenticated file server for the [Jarvis](https://github.com/magnusgille) personal AI system. Named after the Norse figure of wisdom.

Serves documents, presentations, PDFs, and images over HTTPS with Bearer token auth. Part of the Hugin & Munin system: **Munin** (memory/brain), **Mímir** (file archive), **Hugin** (signal hunter).

## How it works

AI agents query **Munin** for document context (summaries, extracted text). When an agent needs the full file, it follows the Mímir URL from the Munin entry. Plain HTTP — no MCP required.

```
Agent → Munin (discovery) → Mímir URL → full file
```

## Endpoints

| Endpoint | Auth | Purpose |
|----------|------|---------|
| `GET /health` | None | Health check |
| `GET /files/*` | Bearer | Serve file from archive |
| `GET /list/*` | Bearer | JSON directory listing |

## Quick start

```bash
npm install
npm run build

# Run locally with test fixtures
MIMIR_API_KEY=dev-key MIMIR_ROOT_DIR=./tests/__test_fixtures__ npm run dev
```

## Testing

```bash
npm test
```

## Deployment

Runs on a Raspberry Pi behind Cloudflare Tunnel with 2-layer auth:

1. **Cloudflare Access** — Service token at edge
2. **Bearer token** — `MIMIR_API_KEY` at origin (timing-safe comparison)

```bash
./scripts/deploy-nas.sh [hostname-or-ip]
```

## Syncing files

Files arrive via rsync from `~/mimir/` on the laptop — no upload endpoint.

**Automatic:** Launchd agent runs every 30 minutes (`com.magnusgille.mimir-sync`).

**Manual:**
```bash
./scripts/sync-artifacts.sh [hostname-or-ip]
```

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MIMIR_PORT` | `3031` | HTTP server port |
| `MIMIR_HOST` | `127.0.0.1` | Bind address |
| `MIMIR_API_KEY` | — | Bearer token (required) |
| `MIMIR_ROOT_DIR` | `/home/magnus/artifacts` | Root directory to serve |
| `MIMIR_ALLOWED_HOSTS` | — | Extra allowed Host headers |
| `MIMIR_RATE_LIMIT` | `60` | Max requests per minute per IP |

## Security

- Path traversal prevention (resolve + startsWith jail)
- Rate limiting per IP
- DNS rebinding protection
- Security headers (CSP, X-Frame-Options, X-Content-Type-Options)
- Dotfiles hidden from listings
- systemd sandboxing on the Pi

## License

MIT
