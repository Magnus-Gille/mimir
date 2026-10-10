# Preparing a first search installation

The runtime, selected-corpus publisher and handoff refresher are packaged from
one explicitly approved 40-character commit. Installation and credential access
remain separate owner-approved actions. The service performs lexical retrieval;
the offline answerability callback is not part of its serving path.

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
