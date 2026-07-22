# Project Status

**Last reconciled:** 2026-07-22
**Branch:** `main`
**Production code release:** PR [#20](https://github.com/Magnus-Gille/mimir/pull/20), merged as `8c4a0d7`

## Phase

Operational maintenance. File-serving, sync, deployment hardening, and dashboard
reconciliation are deployed and accepted.

## Current State

- PR #20 remains the production code release. Deploying later documentation-only
  commits advances the accepted repository marker without changing runtime code.
- Loopback health at `127.0.0.1:3031/health` passes.
- `mimir.service` and `mimir-offsite.timer` are active and enabled.
- The deployed `.env` has mode `0600`, remote Git metadata is absent, and the
  deployment marker matches the accepted commit.
- The production audit reports zero drift. Heimdall reads the authoritative
  deployment path and reports the accepted release.
- The encrypted off-site backup remains accepted end to end: `cryptcheck` found
  zero differences, and a full scratch restore matched an immutable 1,643-file
  source snapshot exactly.
- The scheduled off-site run on 2026-07-22 completed successfully at 03:39 CEST:
  1,712 files were mirrored, the heartbeat timestamp agrees with the service
  result, and the next timer run is scheduled for 2026-07-23 at 03:37 CEST.
- Heimdall's authoritative `mimir/offsite` panel records that run as `pass` with
  the same file count. The Mimir service page now renders `Healthy` from pushed
  panels, while explicitly reporting that no probe endpoint is used.
- Issue [#12](https://github.com/Magnus-Gille/mimir/issues/12)'s acceptance
  criterion is therefore met without widening the loopback-only listener. The
  verified resolution was recorded on the issue and it was closed as completed
  on 2026-07-22.
- Local `main` was fast-forwarded through PR
  [#21](https://github.com/Magnus-Gille/mimir/pull/21), which made `AGENTS.md`
  canonical and reduced `CLAUDE.md` to its adapter. PR
  [#23](https://github.com/Magnus-Gille/mimir/pull/23) records this operational
  reconciliation without changing application behavior.

PR #20 closes external-symlink jail escapes, opens files before stat/streaming to
survive sync races, canonicalizes configured roots, and keeps inbox import and
secret scanning fail-closed across invocations. It also corrects remote delete
safety and makes deployment deterministic and attributable: clean committed input,
`npm ci`, complete unit refresh, strict `.env` permissions, loopback health
verification, and atomic recreation of `.deployed-commit` only after acceptance.

## Blockers

None. Mímir remains intentionally loopback-only behind the tunnel.

## Next Steps

1. Keep scheduled-run and Heimdall freshness evidence current.
2. Refresh the full restore evidence periodically.

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
drift. The 2026-07-22 reconciliation rechecked the deployed marker, loopback
health, service/timer state, scheduled backup result, heartbeat, and authoritative
Heimdall panel/service-page state. It did not redeploy or rerun the backup.
