# Project Status

**Last session:** 2026-07-09
**Branch:** main
**Last commit:** docs: refresh project instructions and readme

## Completed This Session
- **Imported Claude project instructions for Codex**
  - Added tracked `AGENTS.md` based on the project `CLAUDE.md`
  - Checked project `.claude/settings.local.json`; applicable settings are permission/tooling allow-list entries, not runtime docs
  - Checked Claude MCP inventory: `friction-mcp` connected; `munin-memory`, Playwright, arxiv, and M5 configured but failing Claude health checks at the time of this session
  - No project-local Claude skills were present under `.claude`
- **Rewrote `README.md`**
  - Updated endpoint list with `/heimdall.json`
  - Added current security model, Cloudflare Access/tunnel notes, NAS deployment details, artifact sync flow, ingest secret scan, share links, Heimdall reporting, and encrypted offsite backup overview
  - Updated dev/test commands and project structure
- Verification passed:
  - `npm run build`
  - `npm test` — 108 tests passing
  - `npm run lint`

## In Progress
- None

## Blockers
- None

## Next Steps
1. Delete stale branch `feat/offsite-cloud-backup` (local + origin) — already squash-merged as PR #10 (head 6311b75 == branch head); verified nothing unmerged. Deletion needs manual confirmation.
2. #12: decide Mímir bind/probe approach so it reports live health in Heimdall.
3. #11: add regression guard for missing `HEIMDALL_*` in the deployed `.env` and consolidate the two `.env` locations.
