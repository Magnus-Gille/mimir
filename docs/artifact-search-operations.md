# Preparing a first search installation

The runtime, selected-corpus publisher and handoff refresher are packaged from
one explicitly approved 40-character commit. Installation and credential access
remain separate owner-approved actions. The service performs lexical retrieval
by default. Semantic modes stay off until the variables in
[artifact-search-semantic.md](artifact-search-semantic.md) are set in the reader's
`runtime.env`, which needs its own approval because it adds the existing M5 route
to that file. The offline answerability callback is not part of the serving path.

## Private source configuration

`search.pyz ops publish --config <publisher.json>` reads exactly a sorted
allowlist, twice, using no-follow file descriptors. Missing files, symlinks,
known secrets, changed reads, replacement source roots and oversized corpora
reject the entire observation. Originals are never modified. Success publishes
an immutable complete bundle and then a 30-minute handoff. Freshness is never
renewed from cached objects. Old snapshots are retained; the publisher stops
at its 512 MiB store budget rather than automatically pruning files.

Publisher JSON must be owner-owned, mode `0600`, with exactly these fields:

```json
{
  "schema": "mimir-search-operations-v1",
  "origin": "https://files.example.com",
  "source_id": "11111111-1111-4111-8111-111111111111",
  "paths": ["presentations/one.html", "presentations/two.html"],
  "publish_prefix": "artifact-snapshots/pilot",
  "archive_root": "/srv/mimir",
  "root_device": 0,
  "root_inode": 0,
  "publish_store": "/srv/mimir/artifact-snapshots/pilot",
  "handoff": "/srv/mimir/artifact-snapshots/pilot/handoff.json"
}
```

The device/inode zeroes are placeholders: obtain the real source directory
identity during read-only preflight and approve that binding. The allowlist is
the complete selected scope. Changing its meaning requires a new source UUID
and separate derived state. The root/store need trusted parent directories;
the private store must be readable by the OS user serving Mímir files.

`search.pyz ops refresh --config <reader.json>` fetches the handoff through the
configured HTTPS origin, validates its source/origin/prefix and exact allowlist,
verifies every immutable object, rejects expired or backward observations, then
atomically installs the private local handoff. Failed refresh preserves the last
observation without extending its expiry. The runtime independently verifies
original hashes on retrieval and fails closed after the lease expires.

Reader configuration keeps the common fields above. Omit `archive_root`,
`root_device`, `root_inode`, `publish_store`; set `handoff` to the private absolute
path consumed by the reader host's runtime.

## Authentication

Source reads require `MIMIR_API_KEY`. When the existing edge uses Cloudflare
Access service authentication, also supply `MIMIR_CF_ACCESS_CLIENT_ID` and
`MIMIR_CF_ACCESS_CLIENT_SECRET` through the private environment. Incomplete,
malformed or non-HTTPS edge credentials fail before network access. The pair
accompanies manifest, object, handoff and original requests while preserving
Bearer authentication, disabled proxies and redirect rejection, following the
[Cloudflare service-token contract](https://developers.cloudflare.com/cloudflare-one/access-controls/service-credentials/service-tokens/).

The distinct `MIMIR_SEARCH_API_KEY` authenticates local search callers. The
publisher needs no source/search credentials. Never put credentials in arguments,
source configuration, receipts or Git. Existing edge policy must authorize the
service identity; a 403 alone does not identify the blocking policy. Do not change
or bypass edge policy during setup.

## Units, sync and activation gates

Render user units locally with `python3 -m tools.artifact_search.service_units`
using `--role publisher` or `--role reader`, `--home`, `--release` and
`--output-directory`. This only writes reviewable unit files. The publisher runs
every five minutes; reader refresh runs every minute. Initial refresh must
succeed before the loopback runtime starts. Package paths and release values
are immutable. Verify user systemd and lingering before choosing these units.

Before publication, update the **actually running laptop sync scripts** to this
release. Manual and daemon mirrors both protect and exclude the reserved
`artifact-snapshots/` namespace: laptop sync must neither erase NAS snapshots nor
replace them with sender files. Preview and mirror use identical filters.
Merging code alone does not establish that the live sync scripts use the filter.

The final approval must identify repository/release/package digest, both hosts,
source/root binding, exact units, credential source/destination, installation,
activation, verification and rollback commands. Refuse conflicting installation
paths/units on first install. An HTML-only pilot needs no PDF dependency install.

After approved setup, publish once and independently verify the observation,
probe the authorized edge/source path, then activate the reader. Acceptance
requires complete indexing, expected retrieval with original SHA/locator, denied
unauthenticated access, a loopback-only listener, and fresh health **and retrieval**
receipts accepted by the package verifier against independently approved pins.
Test stale and changed-source rejection on synthetic originals, never edited
production files. Check existing file and inference service health before/after.

First-install rollback stops/disables only the new reader/refresh and publisher
units/timers, preserving state, packages and receipts. Existing file/inference
services remain untouched. Deletion and credential revocation require explicit
scope. Later upgrades retain a known-good package/receipt and repeat the same
fresh acceptance checks after rollback.

## Gateway revision drift

The reader pins `MIMIR_SEARCH_EMBEDDING_REVISION=<gateway SHA>:<model SHA-256>`
and checks the gateway release through `/portal/me` before every embedding
call. **Every gille-inference gateway deploy changes the gateway SHA.** Semantic
retrieval then fails closed with `embedding_gateway_revision_mismatch`: queries
return `lexical_fallback` / `embedding_unavailable`, and the vector cache may
still look `complete`. The fail-closed behaviour is intended. The re-pin below
is an explicit operator step after each gateway deploy, never automatic.

### Alert

The reader role renders `mimir-search-semantic-check.service` and a five-minute
`.timer`. `search.pyz ops semantic-check --port 8791` does three things:

1. It reads the serving runtime's loopback `/health` with `MIMIR_SEARCH_API_KEY`.
   This is the pin the running process actually loaded.
2. It reads the live gateway revision through `/portal/me` with the reader's own
   `M5_API_KEY`.
3. It classifies `semantic.error`, the vector state and the comparison.

| Result | State |
| --- | --- |
| `semantic_disabled`, `embedding_gateway_revision_current`, `vectors_complete`, `vectors_building` | `pass` |
| `embedding_unavailable` (transient gateway failure), `vectors_not_started`, `vectors_identity_stale`, an `unavailable` vector error | `warn` |
| `embedding_gateway_revision_mismatch`, a `semantic.error` code, `embedding_route_unavailable`, `runtime_health_unavailable`, a `failed` vector error | `fail` |

Each run pushes one `mimir/search-semantic` Heimdall status panel when
`HEIMDALL_HUB_URL` and `HEIMDALL_FLEET_TOKEN` are set. Keep them in the optional
`~/.config/mimir-search/heimdall.env` (`0600`), not in `runtime.env`. Without a hub,
a non-pass result logs `[mimir-search] ALERT …` to the journal. A `fail` always
exits non-zero, so the unit shows as failed. Output holds bounded codes and counts
only: no credentials, route URLs or response bodies.

```bash
journalctl --user -u mimir-search-semantic-check.service -n 5
```

### Re-pin after a gateway deploy

The package entrypoint needs `MIMIR_SEARCH_RELEASE` set to the installed
release, as the units do. Run it on the reader host as the reader user.

1. Dry run (the default). It prints `old_revision` and `new_revision` and writes nothing:

   ```bash
   MIMIR_SEARCH_RELEASE=<release> python3 ~/.local/share/mimir-search/releases/<release>/search.pyz \
     ops repin-embedding --env-file ~/.config/mimir-search/runtime.env --model-file <installed GGUF>
   ```

   The command reads `M5_OPENAI_BASE_URL` and `M5_API_KEY` from the env file in
   process. It refuses a non-private route, a symlinked or non-`0600` env file,
   and duplicate pin or route keys. It parses only single-line `KEY=value`
   assignments, unquoted or fully quoted, without backslashes. Anything that
   systemd might read differently, such as whitespace before `=`, multiline
   quotes or escapes, is refused with `unsupported_env_syntax`. Before any network call, it hashes the
   installed GGUF and refuses `model_sha256_mismatch` if the model changed.
2. Compare `new_revision` with the gateway deploy receipt, such as the deploy
   marker or the merged gille-inference SHA. Do not apply a revision you cannot
   attribute to an approved deploy.
3. Apply exactly that revision:

   ```bash
   … ops repin-embedding --env-file … --model-file … --apply --expect-gateway <40-hex SHA from step 2>
   ```

   The command refuses `gateway_revision_unexpected` if the live gateway no longer
   reports that SHA. It writes `runtime.env.pre-repin-<UTC>` (`0600`, exclusive)
   next to the env file, or in `--backup-dir`. It then replaces `runtime.env`
   atomically and changes only the gateway half of the pin line. A lock
   (`.runtime.env.repin.lock`) held from the read to the replace refuses a
   concurrent re-pin with `repin_in_progress`. A hand edit made during the run
   aborts it with `env_file_changed`. Don't edit the file by hand while
   applying: an edit in the instant before the replace can still be lost.
4. Restart the runtime with `systemctl --user restart mimir-search.service`. The
   vector cache rebuilds under the new identity. The old cache is inert and
   is kept. The next `semantic-check` run should report
   `embedding_gateway_revision_current`, then `vectors_complete`.

Rollback: copy the backup back over `runtime.env` (keep `0600`) and restart. A
**model** change is out of scope for this command. It needs a newly
operator-verified SHA-256 and a deliberate pin edit, which also gives the vector
cache a new identity.
