# Project Status

**Last reconciled:** 2026-07-14
**Branch:** `main`
**Production release:** PR [#20](https://github.com/Magnus-Gille/mimir/pull/20), merged as `8c4a0d7`

## Phase

Operational maintenance. File-serving, sync, deployment hardening, and dashboard
reconciliation are deployed and accepted.

## Current State

- PR #20 is live on the NAS at exact commit
  `8c4a0d73a3fbb30d827e49f631e3493b562ef219`.
- Loopback health at `127.0.0.1:3031/health` passes.
- `mimir.service` and `mimir-offsite.timer` are active and enabled.
- The deployed `.env` has mode `0600`, remote Git metadata is absent, and the
  deployment marker matches the accepted commit.
- The production audit reports zero drift. Heimdall reads the authoritative
  deployment path and reports the accepted release.
- The encrypted off-site backup remains accepted end to end: `cryptcheck` found
  zero differences, and a full scratch restore matched an immutable 1,643-file
  source snapshot exactly.

PR #20 closes external-symlink jail escapes, opens files before stat/streaming to
survive sync races, canonicalizes configured roots, and keeps inbox import and
secret scanning fail-closed across invocations. It also corrects remote delete
safety and makes deployment deterministic and attributable: clean committed input,
`npm ci`, complete unit refresh, strict `.env` permissions, loopback health
verification, and atomic recreation of `.deployed-commit` only after acceptance.

## Blockers

None. Mímir remains intentionally loopback-only behind the tunnel.

## Next Steps

1. Verify the next scheduled off-site run and Heimdall freshness.
2. Continue issue #12's health/probe decision without widening the loopback bind.
3. Keep backup restore evidence current.

## Release Validation

PR #20 passed before merge:

- `npm test` - 143 tests
- `npm run lint`
- `npx tsc --noEmit`
- `npm run build`
- `npm audit` - 0 vulnerabilities
- `bash -n scripts/*.sh`
- `shellcheck --severity=warning scripts/*.sh`
- `git diff --check`

Production acceptance verified the exact deployed commit, loopback health, unit
state, `.env` permissions, absence of remote Git metadata, and zero deployment
drift. This status reconciliation uses that recorded acceptance evidence and did
not redeploy or rerun the backup.
