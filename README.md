# Mímir

Self-hosted authenticated file server for the Grimnir personal AI system. Mímir
serves documents, presentations, PDFs, images, and other artifacts over HTTP with
Bearer-token auth, temporary HMAC share links, and a small operational surface that
is easy to run on a NAS Pi.

Mímir is the file archive in Grimnir:

- **Munin** stores memory, summaries, and extracted document text.
- **Mímir** serves the original files when an agent needs the full artifact.
- **Hugin** dispatches and coordinates tasks.

Agents normally discover documents through Munin first. If the full file is needed,
they follow the Mímir URL from the Munin entry. There is intentionally no Mímir MCP
server; plain HTTP works across agents and environments.

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

MIMIR_API_KEY=dev-key MIMIR_ROOT_DIR=./tests/__test_fixtures__ npm run dev
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
| `MIMIR_ROOT_DIR` | `/home/magnus/mimir` | Root directory to serve |
| `MIMIR_ALLOWED_HOSTS` | - | Extra allowed Host headers, comma-separated |
| `MIMIR_RATE_LIMIT` | `60` | Requests per minute per IP |
| `MIMIR_SHARE_SECRET` | - | HMAC secret that enables `/share/:token` |
| `MIMIR_BASE_URL` | `https://mimir.gille.ai` | Base URL used by the share CLI |
| `MIMIR_QUARANTINE_DIR` | `<target-dir>-quarantine` | Secret-scan quarantine directory |
| `HEIMDALL_HUB_URL` | - | Heimdall panel push endpoint |
| `HEIMDALL_FLEET_TOKEN` | - | Heimdall panel push token |
| `MIMIR_OFFSITE_REMOTE` | `mimir-crypt` | rclone crypt remote name for offsite backup |
| `MIMIR_OFFSITE_ROOT` | `/home/magnus/mimir` | Directory pushed offsite |
| `MIMIR_OFFSITE_RETENTION_DAYS` | `30` | Offsite archive retention |
| `MIMIR_OFFSITE_MAX_DELETE` | `1000` | Abort offsite run above this delete count |
| `MIMIR_OFFSITE_MAX_DELETE_PCT` | `25` | Abort offsite run above this delete percentage |

## Security Model

Mímir is designed to sit behind Cloudflare Access and still defend itself at the
origin:

- Cloudflare Access service-token policy at `mimir.gille.ai`.
- Bearer-token auth at the app for `/files/*` and `/list/*`.
- HMAC-signed, expiring public share links for `/share/:token`.
- Timing-safe Bearer token comparison.
- Path traversal prevention with a resolved-path jail.
- DNS rebinding protection through `MIMIR_ALLOWED_HOSTS`.
- In-memory per-IP rate limiting.
- Security headers: `nosniff`, `DENY` frame policy, noindex, no-store, no-referrer.
- Dotfiles hidden from directory listings.
- systemd sandboxing on the NAS Pi.
- Ingest-time secret scanning before imported files can reach the servable tree.

## Deployment

Mímir runs on the NAS Pi from `/home/magnus/mimir-server`, serving artifacts from
`/home/magnus/mimir`. Deploy from this repo:

```bash
./scripts/deploy-nas.sh [hostname-or-ip]
```

The target host is environment-specific; pass it explicitly when deploying outside
the maintainer's machine. The Pi needs `/home/magnus/mimir-server/.env` with at
least:

```bash
MIMIR_API_KEY=<generate with: openssl rand -hex 32>
MIMIR_ALLOWED_HOSTS=mimir.gille.ai
```

Optional production settings include `MIMIR_SHARE_SECRET`,
`HEIMDALL_HUB_URL`, and `HEIMDALL_FLEET_TOKEN`.

`mimir.service` runs the HTTP server with `ProtectSystem=strict`,
`ReadOnlyPaths=/home/magnus/mimir`, and write access only to the server directory.

### Cloudflare Tunnel

Production is expected to run behind a Cloudflare Tunnel and Cloudflare Access.
Keep tunnel IDs, service-token names, and any provider-side identifiers out of the
repository.

- Public URL: `https://mimir.gille.ai`
- Access app: `mimir.gille.ai`
- Access policy: service-token auth at the edge
- DNS: CNAME `mimir.gille.ai` to the tunnel target

Share URLs require a Cloudflare Access bypass policy for `/share/*`; the HMAC token
is the authentication layer for recipients.

## Syncing Artifacts

The archive source of truth is `~/mimir/` on the laptop and Pi. Files arrive through
rsync; there is no upload endpoint.

Manual sync:

```bash
./scripts/sync-artifacts.sh [hostname-or-ip]
```

Automatic sync is handled by the launchd agent `com.magnusgille.mimir-sync`, which
runs every 30 minutes and skips silently when the NAS is unreachable.

```bash
launchctl list | grep mimir
launchctl start com.magnusgille.mimir-sync
cat ~/.local/share/mimir/logs/sync-stdout.log
```

The sync flow imports files from the NAS inbox, scans newly imported files for
secrets, then mirrors laptop `~/mimir/` to NAS `~/mimir/` with a delete safety gate.

Local helper scripts use SSH host alias `nas` by default. Override with
`MIMIR_NAS_HOST=<host>` for host-only commands or `MIMIR_NAS=<user@host>` for rsync
and share helpers.

## Secret Scanning

`scripts/sync-artifacts.sh` and `scripts/sync-artifacts-daemon.sh` scan newly
imported inbox files before they can be served. The scanner detects known secret
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

`scripts/backup-artifacts.sh` backs up `/home/magnus/mimir` to
`/mnt/timemachine/backups/mimir/` on the NAS disk.

### Encrypted Offsite Copy

`scripts/offsite-backup.sh` is the Grimnir reference implementation for encrypted
cloud backup. It runs daily from `mimir-offsite.timer` at 03:30 local time with a
jitter of up to 15 minutes.

The job:

- Requires an rclone `crypt` remote and fails closed if the remote is not crypt.
- Encrypts file contents and filenames before pushing to OneDrive.
- Mirrors current state to `<remote>:current`.
- Moves overwritten or deleted files to `<remote>:archive/<utc-run>/`.
- Prunes archive run directories older than the retention horizon by timestamped name.
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
configured. Missing Heimdall env vars are non-fatal.

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
│   └── offsite-backup.md
├── scripts/
│   ├── backup-artifacts.sh
│   ├── deploy-nas.sh
│   ├── offsite-backup.sh
│   ├── share.sh
│   ├── sync-artifacts-daemon.sh
│   └── sync-artifacts.sh
├── src/
│   ├── heimdall-report.ts
│   ├── index.ts
│   ├── secret-scan.ts
│   ├── share-token.ts
│   └── cli/
│       ├── secret-scan.ts
│       └── share.ts
└── tests/
    ├── heimdall-report.test.ts
    ├── secret-scan.test.ts
    ├── server.test.ts
    └── share-token.test.ts
```

## Design Decisions

- Single-purpose Express server; the filesystem is the source of truth.
- No upload endpoint; files are imported and synced over rsync.
- No MCP server; Munin provides discovery and Mímir provides authenticated HTTP files.
- Share links are stateless HMAC tokens rather than database rows.
- Backups are split by responsibility: Mímir owns artifact replication, Brokkr owns
  the destination disk and platform substrate.

## License

MIT
