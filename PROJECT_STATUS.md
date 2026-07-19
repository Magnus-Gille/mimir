# Project status

Mímir is in maintenance and is suitable for small, self-hosted deployments.

## Available

- Authenticated file reads and directory listings
- Expiring HMAC share links with a seven-day maximum TTL
- Path and symlink containment within a configured archive root
- Range requests, security headers, Host validation, and rate limiting
- Optional secret-scanned rsync ingest, Heimdall reporting, and encrypted backups
- Linux systemd and deployment examples with generic paths

## Known limitations

- One process and one filesystem; there is no clustering or object-store backend.
- Rate limits are in memory and reset when the process restarts.
- Share links cannot be individually revoked without rotating the share secret.
- The helper deployment and sync scripts assume Linux/Unix tools and SSH access.
- Operators must supply and audit their own reverse proxy, identity policy, storage,
  backup provider, and restore process.

Operational deployment state belongs in an untracked local `STATUS.md`, not in the
public repository.
