# Mímir

Self-hosted authenticated file server for the Grimnir personal AI system. Mímir
serves documents, presentations, PDFs, images, and other artifacts over HTTP with
Bearer-token auth, temporary HMAC share links, and a small operational surface that
is easy to run on a NAS Pi.

This Grimnir component is unrelated to Grafana Mimir.

Mímir is the file archive in Grimnir:

- **Munin** stores memory, summaries, and extracted document text.
- **Mímir** serves the original files when an agent needs the full artifact.
- **Hugin** dispatches and coordinates tasks.

Agents normally discover documents through Munin first. If the full file is needed,
they follow the Mímir URL from the Munin entry. There is intentionally no Mímir MCP
server; plain HTTP works across agents and environments.

## Standalone or with Grimnir

Mímir has no runtime dependency on the rest of Grimnir. Standalone deployments can
use the HTTP API directly as a small authenticated archive. In a Grimnir deployment,
Munin records summaries and Mímir URLs, Hugin produces artifacts into an inbox, and
Heimdall can consume the optional service descriptor and status panels. Those
integrations are opt-in; missing integration variables do not prevent startup.

## Endpoints

| Endpoint | Method | Auth | Purpose |
| --- | --- | --- | --- |
| `/health` | GET | None | Health check |
| `/heimdall.json` | GET | None | Heimdall service descriptor |
| `/files/*` | GET | Bearer | Serve a file from the archive |
| `/list/*` | GET | Bearer | JSON directory listing |
| `/share/:token` | GET | HMAC token in URL | Temporary public file link |

`/files/*` and `/share/:token` support range requests for large files. Share links
serve browser-safe media inline when appropriate and download other files as
attachments; `?dl=1` or `?download=1` forces attachment mode.

## Quick Start

```bash
npm install
npm run build
mkdir -p data

MIMIR_API_KEY=dev-key MIMIR_ROOT_DIR=./data npm run dev
```

Fetch a file or list a directory:

```bash
curl -H "Authorization: Bearer dev-key" \
  http://localhost:3031/files/example.txt

curl -H "Authorization: Bearer dev-key" \
  http://localhost:3031/list/
```

## Development

```bash
npm run build
npm test
npm run lint
```

The server is Node.js 20+ with strict TypeScript, Express 5, and Vitest integration
tests. Test coverage includes file serving, auth, share tokens, secret scanning, and
Heimdall reporting helpers.

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `MIMIR_PORT` | `3031` | HTTP server port |
| `MIMIR_HOST` | `127.0.0.1` | Bind address |
| `MIMIR_API_KEY` | - | Bearer token, required |
| `MIMIR_ROOT_DIR` | `./data` | Root directory to serve; it must already exist |
| `MIMIR_ALLOWED_HOSTS` | - | Extra allowed Host headers, comma-separated |
| `MIMIR_TRUST_PROXY` | `false` | Express trusted-proxy value, such as `loopback` or a hop count |
| `MIMIR_RATE_LIMIT` | `60` | Requests per minute per IP |
| `MIMIR_INSTANCE_ID` | `default` | Stable instance identity reported to Heimdall |
| `MIMIR_DEPLOY_HOST` | `localhost` | Deployment host label reported to Heimdall |
| `MIMIR_SYNC_MAX_DELETE` | `1000` | Abort laptop→NAS sync at this many deletions |
| `MIMIR_SYNC_MAX_DELETE_PCT` | `20` | Abort sync above this share of the actual remote population |
| `MIMIR_SYNC_STATE_DIR` | `$XDG_STATE_HOME/mimir` or `~/.local/state/mimir` | Durable out-of-tree staging for unverified inbox imports |
| `MIMIR_BACKUP_LOG` | `$XDG_STATE_HOME/mimir/backup.log` or `~/.local/state/mimir/backup.log` | Local backup log, kept outside the deployed code tree |
| `MIMIR_SHARE_SECRET` | - | HMAC secret that enables `/share/:token` |
| `MIMIR_BASE_URL` | `http://127.0.0.1:3031` | Base URL used by the share CLI |
| `MIMIR_QUARANTINE_DIR` | `<target-dir>-quarantine` | Secret-scan quarantine directory |
| `HEIMDALL_HUB_URL` | - | Heimdall panel push endpoint |
| `HEIMDALL_FLEET_TOKEN` | - | Heimdall panel push token |
| `MIMIR_OFFSITE_REMOTE` | `mimir-crypt` | rclone crypt remote name for offsite backup |
| `MIMIR_OFFSITE_ROOT` | `$HOME/mimir` | Directory pushed offsite |
| `MIMIR_OFFSITE_RETENTION_DAYS` | `30` | Offsite archive retention |
| `MIMIR_OFFSITE_MAX_DELETE` | `1000` | Abort offsite run above this delete count |
| `MIMIR_OFFSITE_MAX_DELETE_PCT` | `25` | Abort offsite run above this delete percentage |

## Security Model

Mímir is designed to sit behind an authenticated reverse proxy and still defend
itself at the origin:

- An optional identity-aware proxy provides an outer authentication layer.
- Bearer-token auth at the app for `/files/*` and `/list/*`.
- HMAC-signed, expiring public share links for `/share/:token`.
- Timing-safe Bearer token comparison.
- Path traversal prevention with lexical and realpath containment; symlinks may
  target files inside the archive but never escape it.
- DNS rebinding protection through `MIMIR_ALLOWED_HOSTS`.
- In-memory per-IP rate limiting.
- Security headers: `nosniff`, `DENY` frame policy, noindex, no-store, no-referrer.
- Dotfiles hidden from directory listings.
- systemd sandboxing on the NAS Pi.
- Ingest-time secret scanning before imported files can reach the servable tree.

Forwarded client addresses are ignored by default. Set `MIMIR_TRUST_PROXY` only
when direct origin access is blocked and the configured value precisely identifies
your proxy. A broad value such as `true` can let direct clients spoof the address
used by rate limiting.

## Deployment

The included Linux service template runs as a dedicated `mimir` user from
`/home/mimir/mimir-server`, serving `/home/mimir/mimir`. Deploy from this repo:

```bash
./scripts/deploy-nas.sh [hostname-or-ip]
```

The target host is required; pass it explicitly or set `MIMIR_NAS_HOST`. The host
needs `/home/mimir/mimir-server/.env` with at
least:

```bash
MIMIR_API_KEY=<generate with: openssl rand -hex 32>
MIMIR_ROOT_DIR=/home/mimir/mimir
MIMIR_ALLOWED_HOSTS=files.example.com
MIMIR_TRUST_PROXY=loopback
```

Optional production settings include `MIMIR_SHARE_SECRET` together with its
required public `MIMIR_BASE_URL`. Heimdall reporting is also optional, but
`HEIMDALL_HUB_URL` and `HEIMDALL_FLEET_TOKEN` are a pair: configure both or
neither:

```bash
HEIMDALL_HUB_URL=https://monitoring.example.com/api/panels
HEIMDALL_FLEET_TOKEN=<provision out of band>
```

The authoritative runtime file is the deployed
`/home/<deployment-user>/mimir-server/.env`, not a development checkout's
`.env`.

The deployment account defaults to `mimir`. Set `MIMIR_DEPLOY_USER` locally to
use another Linux account; the deploy script renders all installed systemd paths
and `User=` directives for that account.

Deployments must start from a clean Git worktree. The script uses deterministic
production dependency installation, refreshes the HTTP and offsite systemd units,
checks the service through its loopback-only listener, and atomically advances
`.deployed-commit` only after health passes. It first captures the previous SHA as the
rollback target, then removes the marker before any remote code-tree mutation; a failed
dependency, unit, restart, or health step therefore leaves the artifact explicitly
unaccepted. Both source worktree `.git` files and checkout `.git` directories are excluded,
and stale remote Git metadata is removed before transfer. If the invalidation SSH command
has an indeterminate outcome, the script stops before code-tree mutation and reports marker
state as unknown for manual verification. It prints the exact clean-worktree redeploy
command and enforces mode `0600` on the remote `.env`.
The remote preflight warns when Heimdall reporting is intentionally disabled
and rejects a partial Heimdall pair before the build or any remote mutation.

`mimir.service` runs the HTTP server with `ProtectSystem=strict`,
`ReadOnlyPaths=/home/mimir/mimir`, and write access only to the server directory.

### Reverse proxy or tunnel

Production should keep the default loopback bind and publish it through a reverse
proxy or private tunnel. Configure the proxy to authenticate `/files/*` and
`/list/*`, preserve the application Bearer check, and avoid exposing the origin.
If `/share/*` bypasses proxy authentication, the expiring HMAC token is its only
credential; tokens can appear in browser history and access logs.

## Syncing Artifacts

The archive source of truth is `~/mimir/` on the laptop and Pi. Files arrive through
rsync; there is no upload endpoint.

Manual sync:

```bash
./scripts/sync-artifacts.sh [hostname-or-ip]
```

Automatic sync can invoke `scripts/sync-artifacts-daemon.sh` from a scheduler. The
script skips safely when its target is unreachable.

```bash
MIMIR_NAS=archive@files.internal ./scripts/sync-artifacts-daemon.sh
```

The sync flow imports files from the NAS inbox, scans newly imported files for
secrets, then mirrors laptop `~/mimir/` to NAS `~/mimir/` with a delete safety gate.
Import and scanner failures stop before any mirror. The gate compares deletions with
the actual remote population and also passes an absolute maximum to the real rsync.

Local helper scripts require either `MIMIR_NAS_HOST=<host>` or
`MIMIR_NAS=<user@host>`. Paths can be overridden with `MIMIR_LOCAL_ROOT`,
`MIMIR_REMOTE_ROOT`, and `MIMIR_REMOTE_INBOX`.

## Secret Scanning

`scripts/sync-artifacts.sh` and `scripts/sync-artifacts-daemon.sh` first import
inbox files into durable out-of-tree staging, then scan every staged file before
collision-safe promotion into the served tree. Import, scan, or promotion failures
leave staging intact and block this and later mirrors until the content is verified,
quarantined, or the collision is resolved. The scanner detects known secret
formats, including AWS, GitHub, Slack, Stripe, Google keys, private key blocks, JWTs,
and generic quoted `key=value` assignments.

Hits are moved to `MIMIR_QUARANTINE_DIR` and logged loudly. If Heimdall variables are
configured, a fail-state panel is pushed as well.

Manual full-tree audit:

```bash
npm run build
node dist/cli/secret-scan.js ~/mimir
```

## Sharing Files

Generate a temporary public URL for a file in `~/mimir/`:

```bash
./scripts/share.sh ~/mimir/presentations/deck.pdf       # 24h default
./scripts/share.sh ~/mimir/presentations/deck.pdf 7d    # custom TTL
./scripts/share.sh presentations/deck.pdf 1h            # relative path ok
```

Supported TTLs are `1h`, `6h`, `12h`, `24h`, `3d`, and `7d`.

The script syncs the file to the Pi, asks the Pi-side CLI to generate an HMAC token,
prints the URL, and copies it to the clipboard. `MIMIR_SHARE_SECRET` must be set on
the Pi.

## Backups

Mímir owns backup of its artifact archive. The destination disk, mount health,
capacity, Samba, and Time Machine concerns belong to the Brokkr platform layer.

### NAS Disk Copy

`scripts/backup-artifacts.sh` makes an append-only local copy. Its generic defaults
are `/home/mimir/mimir/` and `/mnt/backup/mimir/`; override the source, destination,
mount point, and log path for your host.

### Encrypted Offsite Copy

`scripts/offsite-backup.sh` is the Grimnir reference implementation for encrypted
cloud backup. It runs daily from `mimir-offsite.timer` at 03:30 local time with a
jitter of up to 15 minutes.

The job:

- Requires an rclone `crypt` remote and fails closed if the remote is not crypt.
- Encrypts file contents and filenames before pushing to the configured provider.
- Mirrors current state to `<remote>:current`.
- Moves overwritten or deleted files to tagged seven-character run directories beside `current/`.
- Prunes only tagged archive run directories older than the retention horizon by encoded timestamp.
- Aborts implausible deletes with count and percentage gates.
- Writes a heartbeat stamp and pushes a Heimdall pass/fail panel.

Setup, key custody, restore testing, and disaster recovery are documented in
[`docs/offsite-backup.md`](docs/offsite-backup.md).

## Heimdall

Mímir exposes `GET /heimdall.json` for service discovery and can push two live panels
to Heimdall every 60 seconds when `HEIMDALL_HUB_URL` and `HEIMDALL_FLEET_TOKEN` are
set:

- `health`: pass-state status with the served root directory.
- `uptime`: current process uptime in hours.

Secret-scan and offsite-backup failures also push fail-state panels when Heimdall is
configured. Missing Heimdall env vars are non-fatal. A temporary push failure is
logged with the affected panel and retried by the next 60-second report cycle;
successful pushes are intentionally silent.

## Project Structure

```text
mimir/
├── CLAUDE.md
├── AGENTS.md
├── README.md
├── mimir.service
├── mimir-offsite.service
├── mimir-offsite.timer
├── docs/
│   ├── offsite-backup.md
│   ├── relocation.md
│   ├── workload-requirement-v1.json
│   ├── workload-requirement-v1.provenance.json
│   └── vendor/
│       └── grimnir/            # SHA-pinned normative schema + fixture manifest
├── scripts/
│   ├── backup-artifacts.sh
│   ├── deploy-nas.sh
│   ├── offsite-backup.sh
│   ├── relocation-verify.sh
│   ├── share.sh
│   ├── sync-artifacts-daemon.sh
│   └── sync-artifacts.sh
├── src/
│   ├── heimdall-report.ts
│   ├── index.ts
│   ├── node-substrate.ts
│   ├── relocation-verify.ts
│   ├── secret-scan.ts
│   ├── share-token.ts
│   └── cli/
│       ├── relocation-verify.ts
│       ├── secret-scan.ts
│       └── share.ts
└── tests/
    ├── heimdall-report.test.ts
    ├── relocation-verify.test.ts
    ├── secret-scan.test.ts
    ├── server.test.ts
    ├── share-token.test.ts
    └── workload-contract.test.ts
```

## Design Decisions

- Single-purpose Express server; the filesystem is the source of truth.
- No upload endpoint; files are imported and synced over rsync.
- No MCP server; Munin provides discovery and Mímir provides authenticated HTTP files.
- Share links are stateless HMAC tokens rather than database rows.
- Backups are split by responsibility: Mímir owns artifact replication, Brokkr owns
  the destination disk and platform substrate.

## Project policy

- [Security policy](SECURITY.md)
- [Contributing guide](CONTRIBUTING.md)
- [Public project status](PROJECT_STATUS.md)

## License

MIT
