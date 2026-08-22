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

That pattern got *more* extreme the more drives were added, not less: the
first two drives were connected and imported together, and a third drive —
connected later, once a compatible enclosure was available — turned out to
be **~90% duplicate of what the first two had already produced**. Of 1,964
files the tool's own cross-drive dedup identified as "new" from that third
drive, Immich's own upload-time content-checksum check independently found
1,920 were already-uploaded duplicates, leaving only 44 genuinely new
photos. Nothing was lost either way — Immich's checksum dedup is a real
safety net, not just the tool's own pass — but it's worth expecting this
pattern if you're recovering from several backup drives accumulated by the
same household over the years: **each additional drive you find is
increasingly likely to be "yet another backup of the same stuff"** rather
than new content, especially past the first two.

**Why the tool's own dedup didn't already catch that 90%:** cross-drive
duplicate detection picks one "keeper" file per set of identical content
across all connected drives, but it does so from the raw manifests alone —
it has no memory of what a *previous, separate* import run already
physically transferred and uploaded. If the keeper it picks for a duplicate
set happens to be the new drive's copy rather than the already-imported
drive's copy, the tool will stage it as "new" even though the content is
already live in your library. This isn't a bug so much as a blind spot
inherent to running dedup in multiple passes across time — if you're doing a
multi-phase recovery like this, expect your own tool's dedup numbers to be
optimistic on any phase after the first, and let the destination
application's own upload-time checksum check (Immich has one; your target
app may not) be the final word before trusting "N new files" at face value.

## Mounting an old macOS drive (APFS) on Linux

If your NAS build runs Linux but your recovered drives are Mac-formatted
(APFS), there's no built-in kernel support for APFS on a stock Linux distro.
What actually worked, after two dead ends:

- **`libfsapfs-utils`'s `fsapfsmount`** — read-only APFS support via FUSE,
  but the distro-packaged binary wasn't built with FUSE support at all
  (`ldd` showed no `libfuse` linkage). Would need building from source.
- **`apfs-fuse`** (a from-source FUSE driver) — got further, but hit a real
  C++ scoping bug in its PList parser against a modern GCC (15.2). Fixing it
  meant hand-patching unfamiliar parsing code in a project whose whole job is
  handling untrusted binary input from an old drive — not a trade worth
  making just to save an install step.
- **`apfs-dkms`** (packages as the `linux-apfs-rw` kernel module) — a real
  out-of-tree kernel module, DKMS-built against your running kernel, with
  experimental write support. This is what actually worked. Mount strictly
  read-only regardless of its write-support claims, since you're reading a
  drive you can't afford to damage:

  ```bash
  sudo apt-get install apfs-dkms
  # Find the APFS container's volumes first — a container can hold more than
  # one (e.g. a "Data" volume alongside an unrelated Time Machine backup
  # volume on the same physical drive):
  sudo fsapfsinfo /dev/sdX
  sudo mkdir /mnt/recovery
  sudo mount -t apfs -o ro,vol=N /dev/sdX /mnt/recovery
  ```

  **The volume index is 0-based for `mount`'s own `vol=` option, even though
  `fsapfsinfo` numbers volumes starting at 1 in its own output.** Don't trust
  the number from `fsapfsinfo` directly — try `vol=0` for what `fsapfsinfo`
  calls volume 1, and confirm with `ls` that the mounted content is actually
  what you expect before trusting it.

See [`troubleshooting.md`](troubleshooting.md#shutilcopy2-can-silently-run-5-8x-slower-than-cp-against-a-non-standard-kernel-filesystem)
for a real, substantial (~8x) copy-throughput regression this specific kernel
module caused in a naive Python copy loop, and the one-line fix.

## Catching corrupted files that "copy" successfully but don't actually play

A successful byte-for-byte copy only proves the *copy* worked — it says
nothing about whether the *original* file was ever intact in the first
place. Years-old recovered media (partial downloads, interrupted transfers
from a decade ago) can copy perfectly cleanly and still be unplayable.

The real example that turned this up: after importing recovered TV episodes
into Jellyfin, one specific episode threw `Unable to find a valid media
source to play`. Cross-referencing Jellyfin's own library data
(`GET /Items?...&Fields=MediaSources,RunTimeTicks` — any item missing
`MediaSources`/`RunTimeTicks` never had its media streams successfully
probed) against the full episode list immediately isolated the exact file.
Jellyfin's own logs confirmed the underlying cause: `ffprobe failed -
streams and format are both null` — the file wasn't a partial/corrupt copy
from *this* recovery, it was already broken on the original source drive,
sitting in a folder literally named `_incomplete - Season 5` from whoever
originally organized it years earlier. A complete, playable copy of the same
episode existed elsewhere on the same drive, so nothing was actually missing
— the broken copy was just quarantined out to an archive folder rather than
left cluttering the working library.

**General lesson:** if something in a bulk-recovered library won't play,
check whether the *server* thinks it has valid stream data before assuming
your copy/transfer pipeline is at fault — a query for items missing media
stream info is a fast way to find every broken file across a large library
at once, rather than waiting for someone to click play on each one.

## Organizing a messy inherited music library by artist

Music recovered from several old backup drives tends to arrive in whatever
folder structure each drive's original owner happened to use — some
already `Artist/Album/Track`, some a flat dump of an old iTunes library
export, some just "Downloads" folders. Reorganizing it into one consistent
`Artist/Album/Track` layout that a media server can browse sensibly needs
more than moving folders around, because **folder names turned out to be an
unreliable signal for who the artist actually is** — one real folder found
during this project literally merged two unrelated artists' albums under one
combined directory name.

What worked: read each file's own embedded tags (via
[`mutagen`](https://mutagen.readthedocs.io/), the one non-stdlib dependency
this project's tooling uses, specifically because there's no standard-library
way to read ID3/FLAC/M4A tags) rather than trusting folder names, and only
fall back to a folder-name guess for the files that genuinely have no usable
tags at all.

**Even the folder-name fallback needs a blocklist.** The first pass at this
fallback — just take the top-level source folder name — produced garbage
like an "artist" folder literally named after someone's laptop backup
("2023 Work Mac") or a generic dump folder ("Downloads - Unsorted"), because
those *were* the top-level folder names for a lot of untagged files. The
fix: walk the path from the source root downward and skip any folder name
that's a known non-artist label (a curated blocklist built by actually
sampling which top-level folders were producing nonsense — the same
"verify against real sampled data before trusting a rule" discipline as the
photo-classification rules above) — for a well-organized subtree like
`.../My Music/matchbox twenty/<album>/<track>`, this correctly skips past
the generic wrapper folders and lands on the real artist two levels down.

**Two dedup passes, not one:** exact byte-identical duplicates (same
SHA256, e.g. the literal same file present via two different backup drives)
are one category; **same-song near-duplicates** — the same track ripped or
downloaded more than once, different file bytes, same Artist+Title tags —
are a separate, arguably more common category that byte-hashing alone can't
catch. Grouping by (artist, title) tag pair and keeping only the largest
file in each group (a reasonable, cheap proxy for "probably better quality")
handled this; on one real ~11,000-track library, this near-duplicate pass
alone collapsed **1,719 tracks** that exact-hash dedup had completely missed.

**Extension-based classification isn't enough to keep non-music out either.**
`.wav`/`.m4a`/`.wma` are legitimate audio extensions, so a Quake 3 game
install's sound-effect files and a folder of personal iPhone voice memos both
matched "looks like music" on extension alone. These only surfaced by
noticing which folders were producing implausible "artist" names during the
tag-fallback analysis above (`baseq3/`, `Voice Memos/`) — another case where
sampling real data caught what a rule based on extension/structure alone
would have missed.

**Write into a new destination tree, don't reorganize in place.** Copying
into a fresh `Artist/Album/Track` tree alongside the untouched original,
rather than moving files in place, means the current working library (and
whatever's already serving it — Jellyfin, in this case) keeps working
throughout, and the whole operation is trivially reversible if the result
needs another pass — just delete the new tree and try again, nothing about
the source was ever at risk.

The tool: [`scripts/reorganize_music.py`](../scripts/reorganize_music.py) —
same dry-run-by-default, quarantine-not-delete conventions as
`organize_recovery.py` above. Needs `mutagen` installed
(`apt install python3-mutagen` or `pip install mutagen`); everything else in
this repo's scripts is deliberately stdlib-only, this is the one exception,
because there's no standard-library way to read audio tags.

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
