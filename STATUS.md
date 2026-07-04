# Project Status

**Last session:** 2026-07-04
**Branch:** main
**Last commit:** bd8c238 fix: share links download text files with correct utf-8 charset (#17)

## Completed This Session
- **Fixed /share serving bugs** (PR #17, squash-merged + deployed to NAS Pi)
  - Markdown/CSV now served as `attachment` (download) instead of inline raw text
  - `charset=utf-8` on all `text/*` responses (Swedish text was mojibake under browsers' Latin-1 guess)
  - `Content-Disposition` filenames formatted via `content-disposition` pkg (RFC 6266/5987) — quotes, control chars and non-latin1 names no longer break the header or 500
  - `scripts/share.sh`: macOS openrsync ignores the `/./` marker in `rsync --relative` (file landed at `/home/magnus/mimir/Users/magnus/mimir/...`) — now syncs to an explicit destination; also rejects `..` segments and `printf %q`-quotes all remote-side args
  - Cross-model Codex review (gpt-5.5) before merge: 3 findings, all fixed red/green
  - 108 tests passing
- **Cleaned stale STATUS.md** — an uncommitted March-era edit had been sitting in the working tree since 2026-03-28; replaced with this entry

## In Progress
- None

## Blockers
- None

## Next Steps
1. Delete stale branch `feat/offsite-cloud-backup` (local + origin) — already squash-merged as PR #10 (head 6311b75 == branch head); verified nothing unmerged. Deletion needs manual confirmation.
2. **Hugin Phase 2** — email delivery for task results (morning briefing)
3. 11 open tickets in `feedback/munin-memory`
