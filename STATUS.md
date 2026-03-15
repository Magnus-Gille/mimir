# Project Status

**Last session:** 2026-03-15
**Branch:** main

## Completed This Session
- **Refactored Pi directory layout for symmetric paths** (c184963)
  - Server moved from `~/mimir` to `~/mimir-server` on Pi
  - Artifacts now at `~/mimir/` on both laptop and Pi (symmetric)
  - Updated: systemd unit, .env, deploy script, sync scripts, backup script, crontab, CLAUDE.md, src/index.ts default
  - Migrated existing artifacts from `~/artifacts/mgc/` to `~/mimir/` on Pi
  - Service restarted and verified healthy (serving from `/home/magnus/mimir`)

## In Progress
- None

## Blockers
- None

## Next Steps
1. **Review overnight task output** — architecture guide quality, diagrams, accuracy
2. **Build `/submit-task` skill** — natural task submission from any environment
3. **Hugin Phase 2** — email delivery for task results (morning briefing)
4. **Mark Mimir as `maintenance`** — MVP + indexing + path fix all complete
5. 11 open tickets in `feedback/munin-memory`
