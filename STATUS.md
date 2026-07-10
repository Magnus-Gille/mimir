# Project Status

**Last session:** 2026-07-10
**Branch:** `codex/p0-offsite-backup-repair`
**Pull request:** [#18](https://github.com/Magnus-Gille/mimir/pull/18) — ready for review
**Implementation head before this status update:** `c502aed`

## Completed This Session

- Repaired the P0 encrypted offsite-backup deployment failure:
  - Excludes transient `.git` internals from rclone sync, delete preflight, counts, and verification guidance.
  - Uses tagged seven-character base-62 run directories beside `current/`, avoiding the old deep archive path under OneDrive's encrypted path limit.
  - Moves heartbeat/log state to systemd-managed `/var/lib/mimir`, outside the rsync `--delete` deploy tree.
  - Makes deployment fail before build/sync when the remote `.env` lacks the server or Heimdall contract values.
  - Validates retention/delete thresholds before any backup operation.
- Ran a direct Codex-native PR review for data-loss, rclone, systemd, shell, and deploy risks.
  - Fixed swallowed rclone listing failures that could bypass the percentage delete gate.
  - Tagged the compact archive namespace so pruning cannot mistake unrelated root directories for owned archives.
  - Updated README and operating docs to match the final archive layout.
- Validation passed without contacting or mutating the backup remote:
  - `npm test` — 112 tests passing
  - `npm run build`
  - `npm run lint`
  - `bash -n scripts/offsite-backup.sh scripts/deploy-nas.sh`
  - `shellcheck --severity=warning scripts/offsite-backup.sh scripts/deploy-nas.sh`
  - `git diff --check`

## In Progress

- PR #18 is ready for review; merge and deployment are intentionally left to the release workflow.

## Blockers

- None.

## Next Steps

1. Merge PR #18 after required GitHub checks pass.
2. Deploy the server tree only after `deploy-nas.sh` passes its remote environment preflight.
3. Install/reload both offsite systemd units and verify `StateDirectory=mimir` plus `/var/lib/mimir` state.
4. Confirm the rclone config permissions and crypt/filename-encryption settings without printing credentials.
5. Run `--dry-run`; only then run one real backup, cryptcheck, and scratch restore/diff before enabling the timer.
