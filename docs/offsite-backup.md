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
   remote** with filename encryption on — fails *closed* otherwise, so a misconfig
   can never upload plaintext.
2. Delete-count gate: if the sync would move an implausible share of `current/` to the
   archive (e.g. the source was wiped), it aborts *before* touching the remote.
3. `rclone sync /home/mimir/mimir/ → mimir-crypt:current` — mirrors the current state (the
   destination is auto-created on first run).
4. Overwritten/deleted files are **moved** to a compact per-run sibling of `current/`
   via `--backup-dir` (never destroyed), giving **30-day version history**. The
   tagged seven-character base-62 timestamp avoids adding encrypted path depth under
   common provider path limits. `--max-delete` is a second-line guard.
5. Prunes whole archive run-dirs older than 30 days **by their encoded timestamp** — not
   by object mtime (sync preserves source mtimes, so mtime-based pruning would wrongly
   delete a just-archived *old* file the moment it was archived).
6. Writes a heartbeat stamp and pushes a `pass`/`fail` **Heimdall status panel**.

The mirror path is fail-loud: any error exits non-zero **and** pushes a `fail` panel.
Archive pruning is best-effort — a prune failure logs a warning but still reports `pass`,
so transient retention drift can't mask an otherwise-healthy backup.

If the storage provider rejects files because their encrypted path is too long, the run
still exits non-zero with the original rclone exit code, but the log and the `fail` panel
report it as **"backup incomplete"** with a count of the affected files (see
[Path-length budget](#path-length-budget)) instead of the generic "backup failed".

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
can push deep trees past the provider's path limit; see
[Path-length budget](#path-length-budget). The encoding cannot be changed later on an
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
# MIMIR_OFFSITE_STAMP=$MIMIR_OFFSITE_STATE_DIR/offsite.stamp
# MIMIR_OFFSITE_LOG=$MIMIR_OFFSITE_STATE_DIR/offsite-backup.log
# MIMIR_OFFSITE_DRYRUN=1                            # same as passing --dry-run
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
rclone cryptcheck /home/mimir/mimir/ mimir-crypt:current --exclude '**/.git/**'

# c) RESTORE TEST — decrypt to a scratch dir, diff against source.
rclone copy mimir-crypt:current /tmp/mimir-restore
diff -r --exclude=.git /home/mimir/mimir/ /tmp/mimir-restore && echo "RESTORE OK"; rm -rf /tmp/mimir-restore

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

## Path-length budget

With the default crypt `filename_encoding = base32`, each path segment is padded to a
16-byte block and then base32-encoded, so a 64-character plaintext segment becomes 128
characters on the provider. Deep or hash-named trees can therefore exceed provider
URL-path limits (OneDrive: 400 characters for the whole path, including the crypt base
path, and 255 per segment) even when the plaintext paths look short.

**Symptoms**

- `rclone sync` exits 6 every night; the log has `ERROR : <path>: Failed to copy:
  ... pathIsTooLong ...` lines.
- The log also says `not deleting files as there were IO errors`: rclone withholds all
  deletions for that run, so `current/` keeps files that no longer exist in the source.
- The log shows `offsite backup incomplete: N file(s) exceed the storage provider's
  encrypted path-length limit`, and the Heimdall panel reads `backup incomplete (exit 6):
  N files exceed provider path-length limit; deletions withheld`. N counts distinct
  affected paths from this run only.

The affected files are not backed up offsite until the budget problem is fixed. Remedies
need an owner decision:

1. **Durable fix:** create a new crypt remote with `filename_encoding = base32768`
   (rclone recommends it for providers that count characters rather than bytes, such as
   OneDrive and Dropbox), upload the archive into it once, then retire the old remote.
   Retiring the old remote deletes data and is a separate destructive-action decision;
   the new remote also needs its own key-custody entry (step 4).
2. **Shorten or relocate** the deep subtrees in the archive so the encrypted paths fit.
3. **Exclude** them (`RCLONE_FILTERS` in the script). This clears the error but loses
   offsite coverage for those files.

## Reuse (munin-memory#172, brokkr#1)

Copy `scripts/offsite-backup.sh` into the target repo and adjust the config block:

- `MIMIR_OFFSITE_SERVICE` → `munin` / `brokkr` (Heimdall panel id).
- `MIMIR_OFFSITE_ROOT` → that service's data dir.
- `MIMIR_OFFSITE_REMOTE` → its own crypt remote (own key — do **not** share the mimir key).
- **Munin:** snapshot the SQLite DB (`VACUUM INTO`) *before* the sync — never upload a
  live DB file mid-write. See munin-memory#172.
- **Photos:** `--backup-dir`/history is wasted on append-only media — drop it and use a
  plain mirror. See brokkr#1.
