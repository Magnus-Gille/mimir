# Agent reference

Lookup-only material extracted from `AGENTS.md`. Behavioral rules, safety
constraints, and ownership boundaries stay inline in `AGENTS.md`.

## Endpoint map

| Endpoint | Method | Auth | Purpose |
|----------|--------|------|---------|
| `/health` | GET | None | Health check |
| `/files/*` | GET | Bearer | Serve a file from the archive |
| `/list/*` | GET | Bearer | JSON directory listing |
| `/share/:token` | GET | None (HMAC token) | Temporary public file sharing |

## Project structure

```
mimir/
├── package.json
├── tsconfig.json
├── AGENTS.md
├── STATUS.md
├── mimir.service
├── mimir-offsite.service
├── mimir-offsite.timer
├── src/
│   ├── index.ts
│   ├── share-token.ts
│   ├── secret-scan.ts
│   ├── heimdall-report.ts
│   ├── node-substrate.ts
│   ├── relocation-verify.ts
│   └── cli/
│       ├── share.ts
│       ├── secret-scan.ts
│       └── relocation-verify.ts
├── docs/
│   ├── index.md
│   ├── agent-reference.md
│   ├── offsite-backup.md
│   ├── heimdall-freshness-surface.md
│   ├── relocation.md
│   ├── workload-requirement-v1.json
│   ├── workload-requirement-v1.provenance.json
│   └── vendor/grimnir/
├── tests/
│   ├── server.test.ts
│   ├── share-token.test.ts
│   ├── secret-scan.test.ts
│   ├── heimdall-report.test.ts
│   ├── freshness-surface.test.ts
│   ├── relocation-verify.test.ts
│   ├── scripts.test.ts
│   ├── systemd-unit-contract.test.ts
│   ├── workload-contract.test.ts
│   ├── instructions-guidance.test.ts
│   └── ab-instructions-probes.json
└── scripts/
    ├── deploy-nas.sh
    ├── render-systemd-unit.sh
    ├── relocation-verify.sh
    ├── share.sh
    ├── sync-artifacts.sh
    ├── sync-artifacts-daemon.sh
    ├── backup-artifacts.sh
    ├── publish-freshness.sh
    ├── install-heimdall-freshness-surface.sh
    └── offsite-backup.sh
```

## Deployment and operator commands

Build, test, and local dev:

```bash
npm install
npm run build
npm test
MIMIR_API_KEY=dev-key MIMIR_ROOT_DIR=./tests/__test_fixtures__ npm run dev
```

NAS deploy:

```bash
./scripts/deploy-nas.sh [hostname-or-ip]
```

Laptop-to-NAS sync:

```bash
MIMIR_NAS=archive@files.internal ./scripts/sync-artifacts-daemon.sh
./scripts/sync-artifacts.sh [hostname-or-ip]
```

Share-link helper:

```bash
./scripts/share.sh ~/mimir/presentations/deck.pdf
./scripts/share.sh ~/mimir/presentations/deck.pdf 7d
./scripts/share.sh presentations/deck.pdf 1h
```

TTL formats: `1h`, `6h`, `12h`, `24h`, `3d`, `7d`.

## Checked-in service expectations

The checked-in Linux service expects `.env` at
`/home/magnus/mimir-server/.env`:

```dotenv
MIMIR_API_KEY=<generate with: openssl rand -hex 32>
MIMIR_ROOT_DIR=/home/magnus/mimir
MIMIR_ALLOWED_HOSTS=files.example.com
MIMIR_TRUST_PROXY=loopback
```

Related checked-in unit files:

- `mimir.service`
- `mimir-offsite.service`
- `mimir-offsite.timer`

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
| `MIMIR_OFFSITE_REMOTE` | `mimir-crypt` | rclone crypt remote name |
| `MIMIR_OFFSITE_ROOT` | `$HOME/mimir` | Directory pushed offsite |
| `MIMIR_OFFSITE_RETENTION_DAYS` | `30` | Archive prune horizon for deleted/changed files |
| `MIMIR_OFFSITE_MAX_DELETE` | `1000` | Abort a run that would delete at least this many files |
| `MIMIR_OFFSITE_MAX_DELETE_PCT` | `25` | Abort a run that would delete more than this percent of `current/` |
| `MIMIR_OFFSITE_STATE_DIR` | `$XDG_STATE_HOME/mimir` or `~/.local/state/mimir` | Deployment-stable heartbeat and log directory |
| `MIMIR_QUARANTINE_DIR` | `<target-dir>-quarantine` | Where ingest secret-scan hits are moved |
| `HEIMDALL_HUB_URL` / `HEIMDALL_FLEET_TOKEN` | — | Heimdall panel push for periodic self-report and secret-scan `fail` alerts |
