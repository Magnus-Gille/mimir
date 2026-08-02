# Agent reference

Lookup-only material extracted from `AGENTS.md`. Behavioral rules, safety
constraints, and ownership boundaries stay inline in `AGENTS.md`.

## Endpoint map

| Endpoint | Method | Auth | Purpose |
|----------|--------|------|---------|
| `/health` | GET | None | Health check |
| `/heimdall.json` | GET | None | Heimdall service descriptor |
| `/files/*` | GET | Bearer | Serve a file from the archive |
| `/list/*` | GET | Bearer | JSON directory listing |
| `/share/:token` | GET | None (HMAC token) | Temporary public file sharing |

## Project structure

```
mimir/
├── package.json
├── tsconfig.json
├── README.md              # Product overview and operator walkthrough
├── AGENTS.md              # Behavioral and safety rules for repository agents
├── PROJECT_STATUS.md      # Public status and current phase
├── mimir.service
├── mimir-offsite.service
├── mimir-offsite.timer
├── src/
│   ├── index.ts           # Express server + HTTP route surface
│   ├── share-token.ts     # HMAC share-token generation + validation
│   ├── secret-scan.ts     # Ingest-time secret scanner + quarantine policy
│   ├── heimdall-report.ts # Heimdall descriptor/reporting helpers
│   ├── node-substrate.ts  # Vendored Grimnir schema loader + validator
│   ├── relocation-verify.ts # ADR-007 relocation checks + receipts
│   └── cli/
│       ├── share.ts       # Pi-side share-link CLI
│       ├── secret-scan.ts # CLI wrapper for the secret scanner
│       └── relocation-verify.ts # CLI for relocation preflight/verify
├── docs/
│   ├── index.md           # Lookup-only doc map
│   ├── agent-reference.md # This extracted operator/agent reference
│   ├── offsite-backup.md  # Cloud-backup setup, retention, and DR
│   ├── heimdall-freshness-surface.md # Restricted freshness probe contract
│   ├── relocation.md      # ADR-007 boundary + normative record routing
│   ├── workload-requirement-v1.json # Machine-readable relocation requirement
│   ├── workload-requirement-v1.provenance.json # Pinned provenance + digests
│   └── vendor/grimnir/    # Vendored upstream schema + fixtures
├── tests/
│   ├── server.test.ts     # HTTP serving, auth, and directory-listing coverage
│   ├── share-token.test.ts # Share-link token unit tests
│   ├── secret-scan.test.ts # Secret-scan + quarantine regression tests
│   ├── heimdall-report.test.ts # Heimdall descriptor/reporting checks
│   ├── freshness-surface.test.ts # Freshness publisher + installer coverage
│   ├── relocation-verify.test.ts # Relocation hook + receipt fail-closed tests
│   ├── scripts.test.ts    # Deploy/sync/share/offsite shell-script contracts
│   ├── systemd-unit-contract.test.ts # Checked-in unit rendering/paths
│   ├── workload-contract.test.ts # Vendored schema + manifest compatibility
│   ├── instructions-guidance.test.ts # Guidance/index clean-clone regressions
│   └── ab-instructions-probes.json # Frozen retrieval/control probe fixture set
└── scripts/
    ├── deploy-nas.sh      # Clean-worktree NAS deploy + acceptance marker
    ├── render-systemd-unit.sh # Deployment-user-aware unit rendering helper
    ├── relocation-verify.sh # Thin wrapper for the read-only relocation hooks
    ├── share.sh           # Sync + generate + print share URLs
    ├── sync-artifacts.sh  # Manual laptop-to-NAS sync entrypoint
    ├── sync-artifacts-daemon.sh # Scheduler-friendly auto-sync wrapper
    ├── backup-artifacts.sh # Local SD-to-NAS backup copy
    ├── publish-freshness.sh # Atomic freshness metadata publisher
    ├── install-heimdall-freshness-surface.sh # Least-authority probe setup
    └── offsite-backup.sh  # Encrypted offsite backup runner
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
| `MIMIR_FRESHNESS_DIR` | `/var/lib/mimir/heimdall-freshness` | Installed metadata-only backup/sync freshness surface (optional) |
| `MIMIR_FRESHNESS_PUBLISHER_USER` | — (required by installer) | OS account that runs the backup/sync freshness publisher |
| `MIMIR_REMOTE_FRESHNESS_DIR` | `/var/lib/mimir/heimdall-freshness` | Remote sync publisher freshness surface (sync daemon) |
| `MIMIR_REMOTE_FRESHNESS_PUBLISHER` | — | Deployment-specific remote command allowed to publish sync freshness when enabled |
| `MIMIR_REMOTE_SYNC_STAMP` | `/home/mimir/mimir-sync.stamp` | Legacy sync heartbeat retained until the new publisher is configured |
| `MIMIR_SHARE_SECRET` | — | HMAC secret for share links (optional, enables `/share`) |
| `MIMIR_BASE_URL` | `http://127.0.0.1:3031` | Base URL for generated share links (CLI only) |
| `MIMIR_OFFSITE_REMOTE` | `mimir-crypt` | rclone crypt remote name |
| `MIMIR_OFFSITE_ROOT` | `$HOME/mimir` | Directory pushed offsite |
| `MIMIR_OFFSITE_RETENTION_DAYS` | `30` | Archive prune horizon for deleted/changed files |
| `MIMIR_OFFSITE_MAX_DELETE` | `1000` | Abort a run that would delete at least this many files |
| `MIMIR_OFFSITE_MAX_DELETE_PCT` | `25` | Abort a run that would delete more than this percent of `current/`; the count and percentage guards are OR-ed, so whichever trips first aborts |
| `MIMIR_OFFSITE_STATE_DIR` | `$XDG_STATE_HOME/mimir` or `~/.local/state/mimir` | Deployment-stable heartbeat and log directory |
| `MIMIR_QUARANTINE_DIR` | `<target-dir>-quarantine` | Where ingest secret-scan hits are moved |
| `HEIMDALL_HUB_URL` / `HEIMDALL_FLEET_TOKEN` | — | Heimdall panel push for periodic self-report and secret-scan `fail` alerts |
