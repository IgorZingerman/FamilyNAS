# Recovering data from an old/failed NAS

If you're migrating onto this stack from a previous NAS (especially one that
failed or is being retired after years of accumulated backups), you're likely
sitting on multiple external drives with heavily overlapping content — the
same photo library backed up several different times over the years, the same
movie collection copied onto more than one drive "just in case," and so on.
Importing all of that as-is wastes real capacity and clutters your new photo
library with duplicates. This is the tool and process used to clean that up
before it ever touched this stack's real storage.

## The tool: `organize_recovery.py`

A single-file, stdlib-only Python 3 CLI (no dependencies to install) with
these subcommands:

```
scan        Catalog every file with a full content hash (sha256), detect true
            (byte-identical) duplicates within one drive.
merge-dupes Find duplicates ACROSS multiple already-scanned drives, without
            needing them all connected at once — useful if you have more
            source drives than USB ports/enclosure bays available at a time.
report      Wasted-space summary from a dupes.csv: total reclaimable space,
            biggest duplicate sets.
dedupe      Move duplicate files into a quarantine folder (never deletes).
transfer    Classify files into categories (photos/movies/tv/music/archive) by
            path pattern, skip known duplicates, copy survivors to destination.
verify      Flag zero-byte / unreadable files (a common sign of a bad copy).
sort-media  Sort photos/videos into DEST/Photos|Videos/YYYY/YYYY-MM by capture
            date (uses `exiftool` if installed, falls back to file mtime).
```

Every command defaults to a dry run and prints exactly what it *would* do;
destructive operations require an explicit `--apply`. Nothing is ever
deleted — `dedupe`/`transfer` quarantine or skip duplicates, they don't remove
your only copy of anything, even after `--apply`.

### Typical flow — drives connected one/two at a time

```bash
python3 organize_recovery.py scan /Volumes/DriveA --out manifest_a.csv
python3 organize_recovery.py scan /Volumes/DriveB --out manifest_b.csv
python3 organize_recovery.py scan /Volumes/DriveC --out manifest_c.csv

python3 organize_recovery.py merge-dupes manifest_a.csv manifest_b.csv manifest_c.csv \
    --out dupes_all.csv
python3 organize_recovery.py report dupes_all.csv

python3 organize_recovery.py transfer manifest_a.csv manifest_b.csv manifest_c.csv \
    --dupes dupes_all.csv \
    --dest-photos /path/to/staging/photos \
    --dest-movies /path/to/staging/movies \
    --dest-tv /path/to/staging/tv \
    --dest-music /path/to/staging/music \
    --dest-archive /path/to/staging/archive \
    --apply
```

`scan` hashes every file up front (a single full read per file, not a
size/quick-hash-then-full-hash multi-pass), so `merge-dupes` can compare
across drives purely from the saved manifests — you never need every source
drive physically connected at the same time.

If a manifest was produced on a different machine than the one running
`transfer` (e.g. you scanned on a Mac but the actual copy runs on Linux where
the same drive mounts at a different path), use `--remap
"/old/mount/path=/new/mount/path"` (repeatable) to translate source paths —
classification itself is unaffected since it only depends on the path
*relative to* the drive root.

### How `transfer` classifies files

It walks each file's full recorded path and matches against known library
patterns (`.photoslibrary` bundles, `iTunes Library/Movies`, etc.), falling
back to file extension for anything outside a recognized library folder, and
finally to an `archive` catch-all for everything else — so nothing is ever
silently dropped, even content it doesn't recognize.

**Verify classification against a real sample of your actual data before
trusting it at scale.** Two real misclassifications turned up during this
project's own recovery, both only visible once actually inspecting file
contents rather than assuming path patterns generalize:

- A legacy iPhoto library bundle extension (`.migratedphotolibrary`) wasn't
  in the initial pattern list, misrouting roughly 300K real photos into the
  generic `archive` bucket instead of the photos category.
- A 163K-file motion-triggered security-camera snapshot archive, nested
  inside what otherwise looked like a normal `Pictures` folder, would have
  been swept into the photo library as if it were personal photos — caught
  only by sampling actual file paths inside that folder rather than trusting
  the folder name.

Neither is a flaw in the tool itself so much as a reminder that path-based
heuristics need validating against your *specific* data before a bulk
`--apply` run — a rule that works cleanly for one backup drive's folder
structure won't necessarily generalize to another's.

## What this actually looked like on real data

Across three source drives (roughly 11.7TB raw, spanning years of
accumulated backups), cross-drive deduplication found **~67% of the data was
duplicate** — the same photo/iTunes libraries had been backed up onto
multiple different drives over time without anyone realizing how much
overlap had accumulated. True unique data came out to roughly 3.9TB, which
is what actually needed importing.

## After this: importing into the real stack

Once `transfer` has landed deduped, categorized files into staging
directories, get them into the actual stack:

- **Photos** → the official [`@immich/cli`](https://www.npmjs.com/package/@immich/cli)
  (`immich upload --recursive <staging-photos-dir>`) against your running
  Immich instance. See [`troubleshooting.md`](troubleshooting.md) for two real
  bugs this hit on a several-hundred-thousand-file upload (filename parsing,
  a CLI/Node version mismatch) and how to work around them.
- **Movies/TV/music** → copy straight into the relevant `tank/media/...`
  dataset; Jellyfin picks them up on its next library scan.

If your NAS hardware is modest, a bulk import of this size is also where
you're most likely to hit the CPU/thermal/job-queue issues documented in
[`troubleshooting.md`](troubleshooting.md) and the offload option in
[`architecture.md`](architecture.md#offloading-immichs-ml-workload-to-a-second-machine)
— worth reading both before kicking off a similarly large import, not after.
