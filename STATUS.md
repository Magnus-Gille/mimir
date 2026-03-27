# Project Status

**Last session:** 2026-03-27
**Branch:** main
**Last commit:** 8f3f046 feat: add temporary share URLs with HMAC-signed tokens

## Completed This Session
- **Implemented share URLs feature** (8f3f046)
  - `src/share-token.ts` — HMAC token generation + validation (dot-separated format)
  - `src/cli/share.ts` — Pi-side CLI, verifies file exists before minting
  - `scripts/share.sh` — laptop wrapper: rsync + ssh + pbcopy
  - `/share/:token` endpoint in `src/index.ts` (no Bearer auth, HMAC validates)
- **Server hardening** (same commit)
  - Extracted rate limiting into standalone middleware (was inside auth middleware)
  - Added `trust proxy` for Cloudflare Tunnel
  - Added `Cache-Control: no-store` and `Referrer-Policy: no-referrer`
  - Extracted file-serving into shared `serveFile()` helper (used by `/files/*` and `/share/*`)
- **Debated plan with Codex** before implementing (debate/share-urls-summary.md)
  - 14 critique points, 11 changed the plan, 21% self-review catch rate
  - Key shifts: Pi-only minting (no shared secret), Node for token logic, hardening as prerequisite
- **Deployed to Pi** and configured `MIMIR_SHARE_SECRET` in .env
- **Configured CF Access bypass** — "Public share links" bypass policy on mimir.gille.ai app
- **Created `/share` skill** — `~/.claude/skills/share/SKILL.md`
- **Tested end-to-end** — `/share` of timeline-v3.png returned 200 image/png via public URL
- 47 tests passing (37 server + 10 token unit)

## In Progress
- None

## Blockers
- None

## Next Steps
1. **Pull Pi changes** from fix-backup-delete task (b71faa1) — verify scripts locally
2. **Hugin Phase 2** — email delivery for task results (morning briefing)
3. 11 open tickets in `feedback/munin-memory`
