# Project Status

**Last reconciled:** 2026-07-13
**Branch:** `agent/harden-file-and-deploy`
**Production release:** PR [#18](https://github.com/Magnus-Gille/mimir/pull/18), merged as `b197e1d`

## Phase

Maintenance hardening. The encrypted off-site backup repair remains deployed and
accepted; the current branch is an unreleased hardening candidate.

## Current State

- PR #18 is live on the NAS at merge commit `b197e1d`.
- Acceptance used an immutable temporary source snapshot and completed a real encrypted backup of 1,643 files.
- `cryptcheck` reported zero differences.
- A full scratch restore compared exactly with the immutable source snapshot: 1,643 of 1,643 files, zero differences.
- The deployment-stable heartbeat at `/var/lib/mimir/offsite.stamp` was fresh, and Heimdall reported `pass`.
- `mimir-offsite.timer` was enabled only after all acceptance checks passed and is active for daily runs.

Draft PR [#20](https://github.com/Magnus-Gille/mimir/pull/20) closes external-symlink
jail escapes, opens files before stat/streaming to survive sync races, canonicalizes
relative configured roots, and makes inbox import and secret scanning durably fail
closed across invocations through out-of-tree pending staging. It calculates delete
safety against the real remote population with an absolute
backstop, and makes deployment deterministic and attributable. Deployment now
requires a clean commit, uses `npm ci`, refreshes all shipped units, enforces `.env`
mode `0600`, excludes both `.git` checkout shapes, clears stale remote Git metadata,
invalidates the prior acceptance marker before remote artifact mutation, verifies
loopback health, and only then atomically recreates `.deployed-commit`. Failed remote
mutations remain markerless while retaining the captured prior SHA for rollback. It
has not been merged or deployed.

The first restore attempt had already proved decryption, but comparison with the live source correctly observed three files that changed after the backup. The immutable-snapshot acceptance above removed that race and is the authoritative release evidence.

## Blockers

None in the implementation or test suite. Draft PR #20 is published for review. No
merge or deployment has occurred.

Existing issue #12 (health/probe state) and issue #11 (deployed-environment consolidation) remain separate follow-up work.

## Next Steps

1. Complete review of draft PR #20 and resolve any remaining findings.
2. After approval and green CI, merge and deploy from a clean canonical `main`.
3. Accept production by verifying loopback health, exact `.deployed-commit`, `.env`
   mode `0600`, refreshed HTTP/offsite units, and the next automatic sync.
4. Continue issue #12's Heimdall probe decision without widening Mímir's loopback bind.

## Release Validation

The unreleased hardening candidate passed:

- `npm test` — 142 tests
- `npm run lint`
- `npx tsc --noEmit`
- `npm run build`
- `npm audit` — 0 vulnerabilities
- `bash -n scripts/*.sh`
- `shellcheck --severity=warning scripts/*.sh`
- `git diff --check`

Before deployment, PR #18 passed:

- `npm test` — 112 tests
- `npm run build`
- `npm run lint`
- `bash -n scripts/offsite-backup.sh scripts/deploy-nas.sh`
- `shellcheck --severity=warning scripts/offsite-backup.sh scripts/deploy-nas.sh`
- `git diff --check`

The production acceptance evidence above was recorded during the completed release. This status reconciliation did not contact production or rerun a backup.
