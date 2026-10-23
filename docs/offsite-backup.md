# Offsite backup — encrypted cloud copy (mimir#9)

The **reference implementation** of the Grimnir offsite-backup pattern. Ships the
Mímir artifact archive to cloud storage as an encrypted third copy, and defines
the mechanism that munin-memory#172 and brokkr#1 (Photos) copy-adapt.

## Where this fits

Grimnir backups follow **3-2-1**, split by data class:

- **Copy 1** — live `/home/mimir/mimir/` (or another configured root).
- **Copy 2** — a separately mounted disk such as `/mnt/backup/mimir/` (`backup-artifacts.sh`).
- **Copy 3 (this)** — encrypted push to a cloud provider. Geographically offsite, automatic,
  survives loss of the whole property.

Assume an archive may contain sensitive data. The cloud copy is therefore
**client-side encrypted**: the provider only stores opaque blobs, including filenames.

## What the job does

`scripts/offsite-backup.sh`, run on the NAS Pi by `mimir-offsite.timer` (daily 03:30):

1. Preflight: rclone present, source exists, and the remote is a **verified crypt
   remote** with filename encryption on and content encryption not disabled
   (`no_data_encryption`) — fails *closed* otherwise, so a misconfig can never
   upload plaintext. `RCLONE_*` environment variables that override the crypt or
   per-remote config, add filter rules, follow links or ignore case also abort the
   run (`reason=rclone_env`); ordinary tuning such as `RCLONE_TRANSFERS` is allowed.
2. No-cloud exclusions: folders marked `no-cloud` in the archive's
   `classification.yaml` are excluded from every following step, before any remote
   call. An unreadable or invalid classification file aborts the run (see
   [No-cloud folders](#no-cloud-folders-policy-decision-d)).
3. Delete-count gate: if the sync would move an implausible share of `current/` to the
   archive (e.g. the source was wiped), it aborts *before* touching the remote.
4. `rclone sync /home/mimir/mimir/ → mimir-crypt:current` — mirrors the current state (the
   destination is auto-created on first run).
5. Overwritten/deleted files are **moved** to a compact per-run sibling of `current/`
   via `--backup-dir` (never destroyed), giving **30-day version history**. The
   tagged seven-character base-62 timestamp avoids adding encrypted path depth under
   common provider path limits. `--max-delete` is a second-line guard.
6. Prunes whole archive run-dirs older than 30 days **by their encoded timestamp** — not
   by object mtime (sync preserves source mtimes, so mtime-based pruning would wrongly
   delete a just-archived *old* file the moment it was archived).
7. Writes a heartbeat stamp and pushes a `pass`/`fail` **Heimdall status panel**.

The mirror path is fail-loud: any error exits non-zero **and** pushes a `fail` panel.
Archive pruning is best-effort — a prune failure logs a warning but still reports `pass`,
so transient retention drift can't mask an otherwise-healthy backup.

Operational output is deliberately content-blind. The private log and inherited
stderr contain only UTC timestamps, stage/state/exit code, and numeric change
metrics (including `no_cloud_markers` and `no_cloud_unmatched` counts); rclone
output, source/destination/remote/config/log paths, archive names, artifact
filenames, marked folder names, and artifact content are discarded. The one exception is a
count: rclone's sync log (`--use-json-log`) is streamed through an in-memory
counter, and when a failed sync includes recognized provider path-length
rejections the `sync` fail event adds
`pathlen_rejections=N doc=offsite-backup.md#path-length-budget` (see
[Path-length budget](#path-length-budget)). The rclone log itself is never written
to disk, the log, the journal or Heimdall. Heimdall receives the same
bounded status fields. The configured state directory may be shared with other
Mímir state surfaces and is never tightened by this job; the default offsite
state is kept in its private `offsite-private` child with mode `0700`. The log
and heartbeat are owner-only `0600` files, and the service uses `UMask=0077`.
Custom `MIMIR_OFFSITE_LOG` and `MIMIR_OFFSITE_STAMP` locations are accepted only
when their directly used parent directories are real, owner-controlled private
directories. Symlinked, non-regular, foreign-owned, or multiply-linked existing
state files are rejected before any write. Existing historical root-level logs
and stamps are not deleted or moved automatically; inspect and migrate or retain
them explicitly as a deployment follow-up.

---

## One-time setup

### 1. Install rclone on the Pi

```bash
ssh <nas-host>
sudo -v ; curl https://rclone.org/install.sh | sudo bash
rclone version   # confirm
```

### 2. Authorize a storage provider

For an OAuth-backed provider on a headless host, mint the token on a machine with a
browser and transfer it using a secure channel. The example below uses OneDrive;
rclone also supports S3-compatible storage, WebDAV, and other providers.

On the **laptop** (install rclone first: `brew install rclone`):

```bash
rclone authorize "onedrive"
# → opens a browser, authenticate to the intended backup account
# → prints a JSON token blob. Copy the whole {...}.
```

On the **Pi**, `rclone config`:

```
n) New remote
name> storage
Storage> onedrive
client_id>            (blank)
client_secret>        (blank)
region> global
Edit advanced config? n
Use auto config? n                     ← headless
config_token> <paste the JSON from the laptop>
Choose the connection type appropriate for the account
Yes this is OK> y
```

Verify: `rclone lsd storage:` lists the provider's top-level folders.

### 3. Create the crypt remote (client-side encryption)

```
rclone config
n) New remote
name> mimir-crypt
Storage> crypt
remote> storage:encrypted/mimir         ← where encrypted blobs land
filename_encryption> standard           ← encrypt filenames too
directory_name_encryption> true
Password or pass phrase for encryption:
  g) Generate random password  → choose a LONG one (or paste your own)
Password or pass phrase for salt (password2):
  g) Generate random password  → generate a separate salt
Edit advanced config? y
filename_encoding> base32768            ← shortest encrypted names; ⏎ for the rest
Edit advanced config? n
Yes this is OK> y
```

Use `filename_encoding = base32768` for providers that count characters rather than
bytes (OneDrive, Dropbox). The default `base32` roughly doubles every path segment and
can push deep trees past the provider's path limit (OneDrive: 400 characters for the whole
path; mimir#58). The encoding cannot be changed later on an
existing crypt remote: every object name would change, so switching requires a new
remote and a full re-upload.

> **Use the same `mimir-crypt` remote name the script defaults to**, or set
> `MIMIR_OFFSITE_REMOTE` in `.env` to whatever you named it.

### 4. 🔑 Key custody (do NOT skip — this is the single point of no return)

The crypt **password + salt** are the only way to decrypt the offsite copy. They
live (obscured) inside `~/.config/rclone/rclone.conf` on the Pi. **If the Pi dies
and you don't have them elsewhere, every byte in cloud storage is permanently unreadable.**

- Reveal them once: `rclone config show mimir-crypt` (shows `password` / `password2`
  in obscured form) — or better, note the *plaintext* password + salt you set in step 3.
- Store the **plaintext password + salt** in your password manager (and/or the
  fireproof safe), under an entry like `mimir-crypt (rclone offsite key)`. Record the
  **remote path** and the **`filename_encoding`** in the same entry: a restore with the
  wrong encoding or path finds nothing, without an obvious error.
- **Never** commit them, never write them to `~/mimir/` (that would encrypt the key
  into the very backup it unlocks), never store them in Munin.

Lock the config down:

```bash
chmod 600 ~/.config/rclone/rclone.conf
```

### 5. Environment

The job reads optional Heimdall vars from `/home/mimir/mimir-server/.env` (already
present for the server). No secrets are added there — the crypt key stays in the
rclone config. Optional overrides (defaults shown):

```
# MIMIR_OFFSITE_REMOTE=mimir-crypt                 # crypt remote NAME (no ':' / path)
# MIMIR_OFFSITE_ROOT=$HOME/mimir                    # directory pushed offsite
# MIMIR_OFFSITE_RETENTION_DAYS=30                  # archive prune horizon
# MIMIR_OFFSITE_MAX_DELETE=1000                    # abort if a run removes ≥ this many files
# MIMIR_OFFSITE_MAX_DELETE_PCT=25                  # ...or more than this % of current/
# MIMIR_OFFSITE_SERVICE=mimir                      # Heimdall service id (sibling repos override)
# MIMIR_OFFSITE_PANEL=offsite                      # Heimdall panel id
# MIMIR_OFFSITE_STATE_DIR=$HOME/.local/state/mimir       # systemd uses /var/lib/mimir
# MIMIR_OFFSITE_STAMP=$MIMIR_OFFSITE_STATE_DIR/offsite-private/offsite.stamp
# MIMIR_OFFSITE_LOG=$MIMIR_OFFSITE_STATE_DIR/offsite-private/offsite-backup.log
# MIMIR_OFFSITE_DRYRUN=1                            # same as passing --dry-run
# MIMIR_OFFSITE_CLASSIFICATION=$MIMIR_OFFSITE_ROOT/classification.yaml  # no-cloud markers
# MIMIR_OFFSITE_CLASSIFICATION_REQUIRED=0          # 1: an absent file aborts the run
# RCLONE_BIN=rclone                                # rclone binary path
```

### 6. Install the timer

`deploy-nas.sh` ships the script with the rest of `scripts/`. Install the units:

```bash
sudo cp /home/mimir/mimir-server/mimir-offsite.service /etc/systemd/system/
sudo cp /home/mimir/mimir-server/mimir-offsite.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mimir-offsite.timer
systemctl list-timers mimir-offsite.timer   # confirm next run
```

The checked-in unit renders the shared `StateDirectory=mimir` traversal and
`UMask=0077`; the script creates and secures its dedicated `offsite-private`
child. Do not grant Heimdall access to this log or state directory; use the fixed
metadata-only freshness surface when a restricted probe needs backup health.

---

## Verification (acceptance criteria)

Run these once after setup. **A backup you haven't restored from is not a backup.**

```bash
# a) First push, then confirm ONLY encrypted blobs are in cloud storage.
/home/mimir/mimir-server/scripts/offsite-backup.sh
rclone ls storage:encrypted/mimir | head       # filenames must be opaque
#   ↳ inspect the provider UI too: names unreadable, no plaintext content.

# b) Integrity: cryptcheck compares hashes THROUGH the crypt layer (plain `check`
#    can silently degrade to size/modtime on a crypt remote).
#    Once a folder is marked no-cloud, add its exclusions (see No-cloud folders):
#    --filter-from <(scripts/no-cloud-filter.sh --exclude /home/mimir/mimir/classification.yaml)
rclone cryptcheck /home/mimir/mimir/ mimir-crypt:current --exclude '**/.git/**'

# c) RESTORE TEST — decrypt to a scratch dir, diff against source.
rclone copy mimir-crypt:current /tmp/mimir-restore
diff -r --exclude=.git /home/mimir/mimir/ /tmp/mimir-restore && echo "RESTORE OK"; rm -rf /tmp/mimir-restore
#    With no-cloud markers the restore lacks those folders by design; compare against
#    a copy of the source made with the same --filter-from exclusions.

# d) 30-day history: change a file across two runs, confirm the prior version is
#    preserved in the most recent archive run-dir.
echo old > /home/mimir/mimir/_probe.txt; ./scripts/offsite-backup.sh
echo new > /home/mimir/mimir/_probe.txt;  ./scripts/offsite-backup.sh
LATEST=$(rclone lsf --dirs-only mimir-crypt: | grep -E '^a[0-9A-Za-z]{6}/$' | sort | tail -1)
rclone lsf "mimir-crypt:${LATEST}" | grep _probe   # prior version preserved
rm /home/mimir/mimir/_probe.txt

# e) Fail-loud: point at a bad remote, confirm non-zero exit + a fail panel in Heimdall.
MIMIR_OFFSITE_REMOTE=does-not-exist ./scripts/offsite-backup.sh; echo "exit=$? (want non-zero)"
```

Dry-run any time without touching the remote: `./scripts/offsite-backup.sh --dry-run`.

## Disaster recovery (Pi is gone)

1. On any machine: `brew install rclone` (or the install script).
2. `rclone config` → recreate the `storage` remote (re-authorize) **and** the
   `mimir-crypt` remote using the **password + salt from your password manager**
   (step 4). The remote path and `filename_encoding` must match what the entry records
   (for example `storage:encrypted/mimir` and `base32768`, set under advanced config).
3. `rclone copy mimir-crypt:current ~/mimir-restored` → plaintext archive back.
4. Folders marked `no-cloud`, and `classification.yaml` while any marker exists, are
   not in the cloud by design. Restore them from the newest offline medium (see
   [Offline copy](#offline-copy-of-no-cloud-folders)).

## Path-length budget

Crypt encodes every encrypted path segment, and providers limit the length of the
*encoded* path, not the plaintext one. With `filename_encoding = base32`, each segment
is padded to a 16-byte block and base32-encoded, so a 64-character segment becomes 128
characters. Deep or hash-named trees can then exceed a provider limit even when the
plaintext paths look short. OneDrive allows 400 characters for the whole path,
including the crypt remote's base path, and 255 per segment. `base32768` (the setup
default above) keeps encoded names much shorter, but a deep enough tree can still
exceed the limit.

**Symptoms**

- The `sync` stage fails, typically with rclone exit 6 (non-retryable errors):
  `stage=sync state=fail exit=6 pathlen_rejections=N doc=offsite-backup.md#path-length-budget`
  in the log, the journal and the Heimdall panel. N is the number of distinct
  objects (rclone's `object` field) whose error record in this run carries a
  recognized path-length rejection: OneDrive/SharePoint `pathIsTooLong`, or a
  `file name too long` (ENAMETOOLONG) error. Retries and rclone's summary records
  are not counted. Names are never reported.
- Everything else still uploads, but rclone withholds **all deletions** for a run
  with IO errors, so `current/` keeps files that no longer exist in the source and
  the archive gets no moved-aside versions for them.
- No heartbeat stamp is written, so freshness checks go stale as well.

A sync failure without `pathlen_rejections` had no *recognized* path-length
rejection; another provider may phrase it differently, so check the encrypted path
lengths below if the failure persists.

**Finding the affected paths** (operator only, in a private terminal; never paste
the output into tickets, logs or Munin): list deep plaintext paths with
`rclone lsf -R --files-only ~/mimir --exclude '**/.git/**'`, then count the
characters of a candidate's encrypted path with
`printf '%s' "$(rclone backend encode mimir-crypt: 'current/<plaintext path>')" | LC_ALL=C.UTF-8 wc -m`
and add the length of the crypt remote's base path (for example `encrypted/mimir/`). No upload is needed.

The affected files are not backed up offsite until the budget problem is fixed.
Remedies need an owner decision:

1. **Shorter encoding:** if the remote still uses `base32`, create a new crypt remote
   with `filename_encoding = base32768` (step 3), upload into it once, verify it
   (cryptcheck and a restore test), and switch `MIMIR_OFFSITE_REMOTE`. The new remote
   needs its own key-custody entry (step 4). Retiring the old remote deletes data and
   is a separate destructive-action decision. mimir#58 was fixed this way.
2. **Shorten or relocate** the deep subtrees in the archive so the encrypted paths fit.
3. **Exclude** them (`RCLONE_FILTERS` in the script). This clears the error but loses
   offsite coverage for those files.

## No-cloud folders (policy decision D)

Data-classification policy v1.1 (in force since 2026-10-20; the policy lives in
the archive at `mgc/internal/compliance/data-classification-policy.md`) allows
client-side-encrypted cloud backup of L3 material, **except** folders whose
contract forbids any third-party storage, even encrypted. Those folders are marked
`no-cloud`, never reach the offsite remote, and get a copy on a local offline
medium instead.

### The marker

The classification file is `<archive root>/classification.yaml`: `~/mimir/` on the
laptop and `/home/mimir/mimir/` on the NAS, where it arrives with the normal sync.
`MIMIR_OFFSITE_CLASSIFICATION` overrides the path (tests use this). A folder is
marked with the flow-mapping form; plain entries carry no marker:

```yaml
default: client-confidential
paths:
  mgc/customers/:          client-confidential               # no marker
  mgc/customers/<client>/: {level: client-restricted, no-cloud: true}
```

`scripts/no-cloud-filter.sh` parses exactly this schema with a strict, dependency-free
parser (awk), so a typo fails closed instead of uploading the folder:

- Accepted: blank lines and `#` comments; `default: <level>` at most once; `paths:`
  exactly once; below it, one `<path>: <level>` or `<path>: {level: <level>, no-cloud: true|false}`
  entry per line, all at the same space indentation, each with an optional ` # comment`.
- A marked `<path>` must be a relative folder path ending in `/`, with no `.`, `..`
  or empty segments. Levels are lowercase tokens.
- **Invalid**: any other key or top-level key (for example `no_cloud`, `version`),
  a value other than `true`/`false`, duplicates, tabs, CR line endings, a byte-order
  mark, quotes, block mappings, lists, and a file without `paths:` (an empty or
  truncated file is invalid).
- Marked paths are matched exactly and case-sensitively. Glob characters in folder
  names are escaped. Non-ASCII letters match their literal spelling and both their
  precomposed (NFC) and decomposed (NFD, as macOS Finder often stores them) forms;
  the archive already holds NFD folder names while the YAML is usually NFC, so a
  byte-exact rule would miss them. This needs the standard-library `unicodedata`
  of `python3`, used only when a marked path is non-ASCII; without it, the run
  fails closed with `reason=unicode`.
- While at least one folder is marked, `classification.yaml` itself is treated as
  no-cloud too, because it names those folders. Copies uploaded before the first
  marker stay in the cloud until the purge, which removes them along with the
  folders. The offline copy includes the file.

### What the nightly job does with it

| Classification file | Result |
|---|---|
| Absent | Proceeds with zero exclusions and logs `no_cloud_markers=0 source=absent`. Set `MIMIR_OFFSITE_CLASSIFICATION_REQUIRED=1` once the first folder is marked, so a lost file aborts the run instead. |
| Present and valid | Each marked folder becomes rclone `--exclude` rules for the sync **and both sides of the delete gate**. Logs `stage=classification state=pass exit=0 no_cloud_markers=N source=file no_cloud_unmatched=M`; the complete event and the Heimdall panel carry `no_cloud_markers=N`. |
| Unreadable (symlink, not a regular file, no read access) | Aborts before any remote call: `stage=classification state=fail exit=1 reason=unreadable`, plus a fail panel. |
| Invalid | Aborts the same way with `reason=invalid line=N` (`line=0`: the whole file, for example no `paths:` section). |

`no_cloud_unmatched` counts markers whose folder does not exist in the archive (a
typo, or a folder not synced yet); only the count is reported.

The exclusions are read once, before the sync. If the classification file's
marker set is different after the sync (an archive sync delivered a new marker
mid-run), the run fails with `stage=classification state=fail exit=1
reason=changed_during_run`: that marker was not applied, so its folder may have
been uploaded. Check with a dry run and purge if needed. Edits that leave the
marker set unchanged do not fail the run. The dry run
(`offsite-backup.sh --dry-run`) prints the same counts without touching the remote.
`files=` on the complete event still counts the whole source tree, marked folders
included.

Excluding a folder never deletes anything in the cloud: rclone leaves excluded
destination files alone, and the delete gate ignores them on both sides. Copies
uploaded before the marker therefore stay in `current/` and in archive run-dirs
until the owner removes them (below). The delete gate keeps working unchanged.

### Marking a folder (runbook)

1. Add the marker to `~/mimir/classification.yaml` **before** the folder's content
   is added to the archive, or at least before the next NAS sync, and let it sync.
2. On the NAS, check the counts:
   `sudo -u mimir env MIMIR_OFFSITE_STATE_DIR=/var/lib/mimir /home/mimir/mimir-server/scripts/offsite-backup.sh --dry-run`
   must show `no_cloud_markers` with the new count and `no_cloud_unmatched=0`.
   Then set `MIMIR_OFFSITE_CLASSIFICATION_REQUIRED=1` in `.env`.
3. If the folder had already been uploaded, remove its cloud copies (below).
4. Make the offline copy (below), and repeat it whenever the folder changes.
5. Record the decision (date, folder, contract reason) where the policy requires.

To see the exclusions themselves, only in a private terminal (they name clients):
`scripts/no-cloud-filter.sh --exclude <archive root>/classification.yaml`.

### Removing earlier cloud copies

`scripts/offsite-purge-no-cloud.sh` deletes the objects under the marked folders
from `current/`, every tagged archive run-dir and the legacy `archive/<ts>` layout.
It is never a side effect of the nightly run. Run it on the NAS as the backup user,
outside the 03:30 window, and **only the owner** runs the execute step:

```bash
cd /home/mimir/mimir-server
export MIMIR_OFFSITE_STATE_DIR=/var/lib/mimir
scripts/offsite-purge-no-cloud.sh                  # plan: deletes nothing
#   stage=plan state=pass exit=0 no_cloud_markers=1 current_objects=12 archive_objects=3 archive_dirs=2 plan=<id> expires=<UTC>
scripts/offsite-purge-no-cloud.sh --execute <id>   # the owner's approval of that plan
#   stage=execute state=pass exit=0 planned_objects=15 remaining_objects=0 receipt=<id>
```

- **Plan** counts the matching objects per directory, deletes nothing and writes a
  single-use plan valid for 24 hours to
  `$MIMIR_OFFSITE_STATE_DIR/offsite-private/no-cloud-purge/plan-<id>` (mode `0600`).
  The plan stores counts and a digest salted with a random nonce, never paths.
- **Execute** claims the plan, recounts, and refuses (`reason=plan_mismatch`) when
  the markers, the remote's crypt section (its backing path and keys, so the same
  remote name pointing elsewhere does not match) or the planned objects (path, size
  and modification time of each) changed since the plan; an expired, unknown or
  reused plan is refused too. The same `rclone_env` and crypt checks as the nightly
  job apply. Each `rclone delete` is confined
  to the marked folders by filters and capped by `--max-delete` at the planned
  count, then empty marked directories are removed with `rmdirs`. It then counts
  again and writes `receipt-<id>` (mode `0600`): plan id, plan and execution times,
  marker and object counts, `delete_failures`, `rmdirs_warnings`,
  `remaining_objects`, `outcome`, and `provider_trash=owner_action_required`. It
  exits non-zero unless every delete succeeded and nothing remains.
- **Provider recycle bin**: most providers keep deleted files for a while (OneDrive:
  the recycle bin, plus the second-stage bin on business accounts). Empty it in the
  provider's UI to finish. On OneDrive for Business/SharePoint,
  `RCLONE_ONEDRIVE_HARD_DELETE=true` before `--execute` deletes permanently instead.
  Record the step next to the receipt.

Agents never run `--execute`; it permanently deletes cloud data and needs the
owner's just-in-time approval.

### Offline copy of no-cloud folders

`scripts/offline-copy-no-cloud.sh` keeps the marked folders on a local offline
medium such as an external SSD, which is attached only for the copy. It is
owner-invoked, typically on the laptop where `~/mimir` is the primary copy.

**One-time setup**

1. Format the medium (an encrypted APFS or LUKS volume adds a second layer).
2. Create a separate crypt remote whose `remote =` is a directory on the medium,
   with its **own** password and salt in the password manager (never the offsite key):
   ```
   [mimir-offline-crypt]
   type = crypt
   remote = /Volumes/MimirOffline/vault
   filename_encryption = standard
   directory_name_encryption = true
   filename_encoding = base32
   password = <obscured>
   password2 = <obscured>
   ```
   Use `base32` here: local filesystems limit names to 255 bytes, and macOS volumes
   are usually case-insensitive, which rules out `base64`.

**Each copy**

```bash
MIMIR_OFFLINE_MOUNT=/Volumes/MimirOffline scripts/offline-copy-no-cloud.sh
```

The script fails closed and writes nothing until every preflight passes: the
classification is valid; the medium is a mount point; no `RCLONE_*` setting other
than the config location and password is in the environment (such settings could
redirect the backing path); and the remote is a crypt remote with content,
standard filename and directory-name encryption whose `remote =` path, after
resolving symlinks, lies on the medium and contains no symlinks (it can never send
data to the cloud). A marker that matches no file
fails the run, because macOS matches names case- and normalization-insensitively
while rclone filters do not; `--allow-unmatched` overrides this. Then it:

1. syncs only the marked folders into `<remote>:current`, moving replaced or deleted
   files into `<remote>:history/<UTC id>` (nothing is pruned);
2. verifies with `rclone cryptcheck --one-way`;
3. writes a receipt to `~/.local/state/mimir/offline-private/receipts/` (mode
   `0600`) and an encrypted copy to `<remote>:receipts/`, with counts, the history id
   and the verification result only;
4. unmounts the medium (`--keep-mounted` skips this; a failed unmount exits 3 after
   the receipt exists).

Store the medium offline, away from the NAS. Restore with
`rclone copy mimir-offline-crypt:current <target>` using the key from the password
manager.

## Reuse (munin-memory#172, brokkr#1)

Copy `scripts/offsite-backup.sh` into the target repo and adjust the config block:

- `MIMIR_OFFSITE_SERVICE` → `munin` / `brokkr` (Heimdall panel id).
- `MIMIR_OFFSITE_ROOT` → that service's data dir.
- `MIMIR_OFFSITE_REMOTE` → its own crypt remote (own key — do **not** share the mimir key).
- **Munin:** snapshot the SQLite DB (`VACUUM INTO`) *before* the sync — never upload a
  live DB file mid-write. See munin-memory#172.
- **Photos:** `--backup-dir`/history is wasted on append-only media — drop it and use a
  plain mirror. See brokkr#1.
- **no-cloud markers** are Mímir-specific: a copy that has no `classification.yaml`
  can drop the classification stage and `no-cloud-filter.sh`.
