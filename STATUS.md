# Project Status

**Last reconciled:** 2026-07-10
**Branch:** `main`
**Release:** PR [#18](https://github.com/Magnus-Gille/mimir/pull/18), merged as `b197e1d`

## Phase

Maintenance. The encrypted off-site backup repair is merged, deployed to the NAS, accepted end to end, and scheduled.

## Current State

- PR #18 is live on the NAS at merge commit `b197e1d`.
- Acceptance used an immutable temporary source snapshot and completed a real encrypted backup of 1,643 files.
- `cryptcheck` reported zero differences.
- A full scratch restore compared exactly with the immutable source snapshot: 1,643 of 1,643 files, zero differences.
- The deployment-stable heartbeat at `/var/lib/mimir/offsite.stamp` was fresh, and Heimdall reported `pass`.
- `mimir-offsite.timer` was enabled only after all acceptance checks passed and is active for daily runs.

The first restore attempt had already proved decryption, but comparison with the live source correctly observed three files that changed after the backup. The immutable-snapshot acceptance above removed that race and is the authoritative release evidence.

## Blockers

None for PR #18 or its deployment.

Existing issue #12 (health/probe state) and issue #11 (deployed-environment consolidation) remain separate follow-up work.

## Next Steps

1. Verify the next scheduled off-site run and Heimdall freshness on 2026-07-11.
2. Continue issue #12's health/probe decision.
3. Continue issue #11's deployed-environment regression/consolidation follow-up.
4. Define a documented cleanup policy for legacy remote `.git` debris; do not delete backup data ad hoc.

## Release Validation

Before deployment, PR #18 passed:

- `npm test` — 112 tests
- `npm run build`
- `npm run lint`
- `bash -n scripts/offsite-backup.sh scripts/deploy-nas.sh`
- `shellcheck --severity=warning scripts/offsite-backup.sh scripts/deploy-nas.sh`
- `git diff --check`

The production acceptance evidence above was recorded during the completed release. This status reconciliation did not contact production or rerun a backup.
