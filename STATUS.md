# Project Status

**Last session:** 2026-03-25
**Branch:** main

## Completed This Session
- **Reviewed Claude's revised share-URL response** and wrote Round 2 rebuttal
  - New artifact: `debate/share-urls-codex-rebuttal-1.md`
  - Concluded that Claude's major concessions are genuine: Pi-only minting, Node-owned token logic, remote verification, simpler token format
  - Main remaining objection: CF Access bypass is only acceptable after public-route hardening; rate limiting and proxy handling are prerequisites, not orthogonal cleanup
  - New issues flagged: config-source drift between server and Pi CLI, missing CLI build/deploy contract, cache/referrer policy for temporary public URLs, need to share file-serving logic instead of forking `/files/*`
- **Reviewed Claude's stateless HMAC share-URL plan** and wrote a grounded critique
  - New artifact: `debate/share-urls-codex-critique.md`
  - Main conclusion: HMAC is defensible server-side, but local shell-based token generation with a shared secret on both machines is probably not the simplest design
  - Stronger alternative: sync file to Pi, verify remote presence, mint URL on Pi/server side using Node
- **Debated SD card intermediate architecture** with Codex (debate/sd-intermediate-summary.md)
  - Original proposal to remove SD tier was withdrawn after debate
  - Concluded: keep SD for fault isolation, fix `--delete` in backup script
- **Submitted fix-backup-delete task to Hugin** — completed in ~2 min (b71faa1 on Pi)
  - backup-artifacts.sh: removed `--delete`, added mount check (HD is now append-only)
  - sync-artifacts.sh: added `--delete` with 20% safety threshold (laptop = source of truth)
  - sync-artifacts-daemon.sh: added matching safety threshold
  - CLAUDE.md updated on Pi

## In Progress
- No implementation in progress
- Open design question is narrower now: whether to proceed with Pi-minted stateless HMAC tokens after first landing the public-route hardening and shared config/file-serving refactors

## Blockers
- None

## Next Steps
1. **If proceeding with share URLs:** land prerequisites first — extract pre-auth rate limiting, configure proxy trust correctly, and design shared file-serving/config helpers
2. **Then finalize the Pi-side minting workflow** — especially canonical env loading and CLI deployment path on the Pi
3. **Pull Pi changes** once pushed — verify scripts look correct locally
4. **Hugin Phase 2** — email delivery for task results (morning briefing)
5. 11 open tickets in `feedback/munin-memory`
