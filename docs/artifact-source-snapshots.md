# Artifact source snapshots (Mímir #41)

The standalone source adapter captures a complete selected corpus, then provides
the same pinned manifest/object contract over a local directory or Mímir's
existing authenticated `/files` surface. It has no search, extraction, embedding,
upload endpoint or service deployment. Python 3.10+ and its standard library are
required. The producer and consumer do not depend on a laptop or NAS filesystem
layout being shared.

## Contract and acceptance boundary

A bundle has this layout:

```text
<store>/<manifest-byte-sha256>/manifest.json
<store>/<manifest-byte-sha256>/objects/<original-byte-sha256>
```

`manifest.json` is canonical UTF-8 JSON: keys sorted, compact separators,
non-ASCII characters retained, and exactly one final LF. Duplicate JSON keys,
unknown fields, noncanonical bytes, unsupported versions and incomplete
inventories are rejected. The snapshot ID is the SHA-256 of those exact bytes,
not an mtime or row number. The consumer requires that ID and the source UUID
from the trusted publisher configuration; it does not discover a mutable
"latest" pointer or trust a hash asserted by the downloaded manifest.

The version-one manifest contains exactly:

| Field | Contract |
| --- | --- |
| `schema` | `mimir-artifact-snapshot-v1` |
| `complete` | Boolean `true`; never a partial update list |
| `source_id` | Canonical owner-assigned UUID for one authorized corpus scope |
| `policy` | Exact supported policy version, sorted suffixes and per-file byte cap |
| `artifacts` | Path-sorted records with unique `artifact_id` UUIDs and unique paths |
| `excluded` | Path-sorted `{path, reason}` records for explicit policy exclusions |

Each artifact has exactly `artifact_id`, `path`, `size`, `mtime_ns`, `sha256` and
`object`. Paths are relative to the selected source root; `size` and `mtime_ns`
are nonnegative integers (booleans rejected); `sha256` is lowercase hexadecimal.
`object` must be exactly `objects/<sha256>`, never a URL or arbitrary fetch path.
The source UUID's trusted configuration also binds the original Mímir path prefix.
The bundle URL is not the original-file prefix. Future retrieval must retain that
mapping, artifact path/hash/identity and snapshot ID in provenance.

A capture enumerates the full selected root with no-follow directory handles,
reads regular files from the same descriptors used for their metadata, and
compares a second complete inventory before publication. Read/list errors,
symlinks, oversized allowed files and concurrent source changes reject the whole
run. They do not become exclusions or deletions. A changed mount or unavailable
root must not be substituted with another directory under the same source UUID.
Empty captures require `--allow-empty`, including intentional full removal.

The policy currently includes HTML, Markdown, text, PDF and common image files
(`.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.svg`). Hidden and credential paths,
unsupported formats and known raw secret formats are explicitly excluded. This
is a known-format byte detector, not a guarantee that arbitrary or compressed
secrets are absent. It complements Mímir's ingest scanner; later extraction must
apply its own content filter before model input. No OCR or document parsing is
performed here.

Bounds are 20 MiB per artifact, 256 MiB summed artifact bytes, 10,000 inventory
entries, 4 MiB manifest bytes, 2,048 path characters and 64 path components. Limits
fail the entire run. Select a bounded corpus rather than silently truncating it.

## Identities and changes

Use the same source UUID and accepted previous manifest for successive captures.
Unchanged paths retain their artifact identity even when bytes change. A removed
path and new path retain identity as a rename only when their content hash is
unique in both complete inventories. Duplicate-content ambiguity is reported as
removal plus addition; the adapter does not guess. A rename accompanied by content
change cannot be inferred automatically. New IDs are deterministic UUID5 values
within the source namespace. The delta reports added, changed, renamed and removed
records; changes include byte or modification-metadata changes.

Snapshots are separate immutable observations; source edits never rewrite earlier
accepted bundles. Captures currently read the complete selected corpus to prove
its hashes. They report incremental deltas, but do not yet skip unchanged reads
or fetches. Within acquisition, duplicate objects are fetched only once.

Consumers verify manifest identity/schema before requesting objects, then verify
every object's size and hash before acceptance. Failed or interrupted acquisition
does not publish a reduced corpus. Consumers must not feed failed/partial runs to
the existing search CLI's complete-manifest deletion path.

## Local use

Choose one stable UUID for the scope; changing the selection's meaning requires
a new source identity. Source and store paths are explicit. The store must be
outside the selected original tree and have mode `0700`; files are created `0600`.
Originals are opened read-only. Examples use synthetic/local placeholder paths:

```bash
python3 -m tools.artifact_search.snapshot_cli create \
  --root /path/to/approved-corpus --store /path/to/private-snapshots \
  --source-id 11111111-1111-4111-8111-111111111111

# Use the returned manifest path as --previous on the next capture.
python3 -m tools.artifact_search.snapshot_cli create \
  --root /path/to/approved-corpus --store /path/to/private-snapshots \
  --source-id 11111111-1111-4111-8111-111111111111 \
  --previous /path/to/private-snapshots/<prior-id>/manifest.json

python3 -m tools.artifact_search.snapshot_cli acquire-local \
  --bundle /path/to/published-bundle/<id> --store /path/to/private-cache \
  --source-id 11111111-1111-4111-8111-111111111111 --snapshot-id <id>
```

`diff --before <manifest.json> --after <manifest.json> --source-id <uuid>` compares
two pinned complete manifests without modifying state. Commands emit JSON; errors
emit bounded reason codes and an affected relative artifact path when known,
never source bytes, credentials, HTTP response bodies or extractor stderr.

## Mímir HTTP acquisition and placement

An operator-approved publisher can materialize a finished bundle under an
authorized Mímir serving directory, outside the selected source subtree. Existing
file routes serve its manifest and extensionless objects under the same Bearer
boundary. No `/list` crawl, new server route, shared filesystem, laptop mirror or
model runtime is involved. The approved scope must exclude the bundle directory
so later inventories do not recursively capture derived state.

Configure `MIMIR_SNAPSHOT_URL` as the credential-free HTTPS bundle URL ending in
`/files/<bundle-prefix>/<id>` and supply `MIMIR_API_KEY` through the existing private
environment mechanism. Credentials have no CLI argument or URL form. An example
URL is `https://files.example.com/files/artifact-snapshots/<id>`; the literal ID
must equal the expected manifest pin. HTTP is allowed only on loopback for local
fixtures. Proxies and redirects are disabled; downloads request identity encoding
and accept full `200` responses only, with bounded reads and request timeouts.
One client leaves at least 1.1 seconds between request starts, including the
manifest, to stay below Mímir's default 60 requests/IP/minute. Other clients on
the same IP can still exhaust that shared limit; a `429` stops this acquisition.
Missing objects, auth failures, rate limits or an unavailable source stop the run;
there is no alternate provider or automatic retry loop.

```bash
python3 -m tools.artifact_search.snapshot_cli acquire-http \
  --store /path/to/private-cache \
  --source-id 11111111-1111-4111-8111-111111111111 --snapshot-id <id>
```

The source identity, URL, expected snapshot ID and credential access remain trusted
operator inputs. This adapter supports Mímir's owner Bearer boundary; it does not
invent multi-user ACLs or provision edge/service credentials. An edge proxy must
already admit the configured reader. Publishing into a live serving tree and
installing producer jobs require their normal deployment approval.

On M5 the bundle cache is disposable derived state, not a second authoritative
archive or a laptop synchronization target. The indexer in #42 will consume the
manifest's original path/identity and blob bytes, not treat `objects/<hash>` as
the original filename. Freshness and a trusted newest-observation handoff are
also #42 work; a valid old snapshot does not prove current-original freshness.

## Publication, collisions and recovery

The producer/consumer writes and fsyncs private staged objects first, reserves a
snapshot directory exclusively, then links `manifest.json` as the final commit
marker. Files and manifests are never overwritten. A repeat reuses an existing
bundle only after validating its pinned manifest and all referenced objects;
corrupt or incomplete collisions fail and remain untouched. No mutable current
pointer is advanced. Consumers accept only complete validated bundles.

Interruption before the marker may leave a reserved markerless directory; it is
not accepted and a retry does not overwrite it. Preserve earlier accepted bundles,
inspect the failed directory, and explicitly remove/quarantine that failed state
before retrying the same snapshot ID. Cache pruning, recovery automation, scheduling,
credentials and production installation belong to the #42 runtime lifecycle.

## Verification

Run `python3 -m unittest discover -s tools/artifact_search -p 'test_*.py'`.
Synthetic tests cover complete local and authenticated HTTP acquisition, exact
body hashes, adds/changes/renames/removals, duplicate ambiguity, source races,
inaccessible/missing sources, interrupted publication, no-overwrite collisions,
malformed/partial manifests and redirect/auth/changed-object failures. HTTP fixtures
need an isolated loopback port. Existing Mímir tests, lint and build also remain
required. The Express integration includes 60 unique objects through its default
rate limit, using matched accelerated clocks only in the test.
These fixtures do not constitute a live NAS deployment check.
