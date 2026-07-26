# Heimdall backup/sync freshness surface

Issue [#35](https://github.com/Magnus-Gille/mimir/issues/35) defines a deliberately
small hand-off for Heimdall's restricted storage probe. It is **not** a view of the
archive, backup destination, logs, or a user's home directory.

## Read contract

The root installer creates `/var/lib/mimir/heimdall-freshness` with ownership
`$MIMIR_FRESHNESS_PUBLISHER_USER:heimdall-storage-probe` and mode `2750`. The
publisher user is deliberately explicit: it must be the actual account that runs
the installed backup/sync publication commands, not an assumed service account. The
setgid bit makes records retain the probe group. Each of the only two allowed records
has mode `0640`: `backup.json` and `sync.json`.

The dedicated probe identity is a **member** of `heimdall-storage-probe`; it has
directory `r-x` and record `r--`, never write. The publisher is the explicitly
configured directory owner and must not be the probe identity. A platform owner adds
the probe identity to that dedicated group as part of its owner-only overlay; do not
add it to a broad service or home-directory group.

Every record has exactly this shape:

```json
{"schema_version":1,"state":"fresh","observed_at":"2026-07-26T10:00:00Z"}
```

`state` is either `fresh` or `error`; `observed_at` is the publisher's UTC
second-resolution clock. There are no paths, filenames from an artifact tree,
counts, messages, logs, or user-provided content. `scripts/publish-freshness.sh`
writes a temporary file in the target directory, sets `0640`, then renames it, so a
reader sees either the preceding complete record or the next complete record.

The probe must classify each fixed record independently:

| Condition | Meaning |
| --- | --- |
| Record absent | `absent` — publisher has not established evidence. |
| JSON invalid or schema/state unknown | `error` — fail closed. |
| `state: error` | `error` — the publisher observed a failed run. |
| `state: fresh`, but `observed_at` exceeds the probe threshold | `stale`. |
| `state: fresh` within threshold | `fresh`. |

Staleness is intentionally a reader policy: backup and sync schedules can differ
without changing the publication format.

## Installation and rollback

On the NAS, after the actual publisher user and the dedicated probe identity exist,
the platform owner explicitly binds the surface to that publisher:

```bash
sudo MIMIR_FRESHNESS_PUBLISHER_USER=<runtime-or-backup-publisher> \
  ./scripts/install-heimdall-freshness-surface.sh
sudo usermod -a -G heimdall-storage-probe heimdall-storage-probe
```

The second command is owner-overlay configuration, not an instruction for a Mímir
deploy script. It takes effect when the probe starts a new login/session. Check the
contract without reading artifacts:

```bash
stat -c '%U:%G %a %n' /var/lib/mimir/heimdall-freshness \
  /var/lib/mimir/heimdall-freshness/backup.json \
  /var/lib/mimir/heimdall-freshness/sync.json
```

`backup-artifacts.sh` publishes `backup` after a successful copy and `error` after a
mount or rsync failure. The sync daemon publishes `sync` after a successful mirror
and attempts to publish `error` when a mirror fails. Its remote SSH account must be
the configured publisher (or otherwise have only the narrowly delegated ability to run
the publisher); it must never be the probe identity. Set
`MIMIR_REMOTE_FRESHNESS_PUBLISHER` to that deployment's publisher script path when
enabling sync evidence; it has no fixed default. Until it is configured, the sync
daemon preserves its existing `MIMIR_REMOTE_SYNC_STAMP` heartbeat. Once configured,
it writes only the new metadata record, avoiding conflicting freshness signals.

To remove the surface without touching archive data or any unexpected state:

```bash
sudo ./scripts/install-heimdall-freshness-surface.sh --remove
sudo gpasswd -d heimdall-storage-probe heimdall-storage-probe
```

The remover deletes only the two fixed records and uses `rmdir`, which refuses to
remove an unexpected file. It intentionally retains the group; remove that group
only after confirming no other configuration uses it.
