#!/usr/bin/env python3
"""
organize_recovery.py - catalog, de-duplicate, and sort files recovered from
the Drobo N5 onto a fresh destination drive.

Nothing is ever deleted. Duplicates are MOVED into a quarantine folder
(mirroring their original relative path) so you can spot-check and delete
them yourself once you trust the results. Every command defaults to a dry
run; pass --apply to actually touch the filesystem.

Subcommands
-----------
  scan        Walk SOURCE, catalog every file (with a full content hash),
              detect true (content-based) duplicates within this run, write
              manifest.csv + dupes.csv.
  merge-dupes Find true duplicates ACROSS multiple manifest.csv files (e.g.
              from separate drives scanned at different times, never
              connected simultaneously) and write a combined dupes.csv.
  report      Summarize a dupes.csv: wasted space, biggest offenders.
  dedupe      Move duplicate files out of SOURCE into a quarantine dir,
              keeping one "best" copy of each set in place.
  sort-media  Sort photos/videos into DEST/Photos|Videos/YYYY/YYYY-MM using
              capture-date metadata (exiftool if installed, else mtime).
  transfer    Classify every file in one or more manifests into a category
              (photos/movies/music/archive) by path pattern, pick one keeper
              per duplicate set (from a dupes.csv), and COPY (never move)
              survivors into DEST/<category>/... - duplicates are skipped
              entirely, never read or copied.
  verify      Flag zero-byte or unreadable files (common sign of a bad
              recovery pass).

Typical flow (one source drive, standalone)
--------------------------------------------
  python3 organize_recovery.py scan /Volumes/RecoveredData --out manifest.csv
  python3 organize_recovery.py report manifest.csv
  python3 organize_recovery.py dedupe manifest.csv --quarantine /Volumes/NewDrive/_Duplicates --apply
  python3 organize_recovery.py sort-media /Volumes/RecoveredData /Volumes/NewDrive/Sorted --apply
  python3 organize_recovery.py verify /Volumes/RecoveredData

Typical flow (multiple drives, never all connected at once)
-------------------------------------------------------------
  python3 organize_recovery.py scan /Volumes/DriveA --out manifest_a.csv
  python3 organize_recovery.py scan /Volumes/DriveB --out manifest_b.csv
  python3 organize_recovery.py scan /Volumes/DriveC --out manifest_c.csv
  python3 organize_recovery.py merge-dupes manifest_a.csv manifest_b.csv manifest_c.csv --out dupes_all.csv
  python3 organize_recovery.py report dupes_all.csv
  python3 organize_recovery.py transfer manifest_a.csv manifest_b.csv manifest_c.csv --dupes dupes_all.csv --dest /srv/media-staging/recovery-import --apply
"""

import argparse
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path

JUNK_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini", ".Spotlight-V100", ".Trashes", ".fseventsd"}
QUICK_CHUNK = 65536  # bytes read from head+tail for the cheap pre-filter hash

PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".bmp", ".gif",
              ".cr2", ".cr3", ".nef", ".arw", ".dng", ".raf", ".orf", ".rw2"}
VIDEO_EXTS = {".mov", ".mp4", ".m4v", ".avi", ".mts", ".m2ts", ".3gp", ".mkv", ".wmv"}
AUDIO_EXTS = {".mp3", ".m4a", ".flac", ".wav", ".aac", ".ogg", ".wma", ".aiff"}


def is_junk(name: str) -> bool:
    return name in JUNK_NAMES or name.startswith("._") or name.startswith(".DS_Store")


def iter_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        # never descend into a quarantine/sorted output, or a junk directory like .fseventsd
        dirnames[:] = [d for d in dirnames if d not in {"_Duplicates", "Sorted"} and not is_junk(d)]
        for name in filenames:
            if is_junk(name):
                continue
            yield Path(dirpath) / name


def full_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- scan ----

def cmd_scan(args):
    root = Path(args.source).resolve()
    if not root.is_dir():
        sys.exit(f"Not a directory: {root}")

    print(f"Scanning {root} ...")
    entries = []  # dicts: path, size, mtime, full_hash
    bad = []
    n = 0
    for path in iter_files(root):
        n += 1
        try:
            st = path.stat()
        except OSError as e:
            bad.append((str(path), str(e)))
            continue
        entry = {"path": path, "size": st.st_size, "mtime": st.st_mtime}
        if st.st_size > 0:
            try:
                entry["full_hash"] = full_hash(path)
            except OSError as e:
                bad.append((str(path), str(e)))
                continue
        else:
            entry["full_hash"] = ""
        entries.append(entry)
        if n % 500 == 0:
            print(f"  ...{n} files cataloged")
    print(f"Found {n} files ({len(bad)} unreadable).")

    manifest_path = Path(args.out)
    with open(manifest_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["path", "size", "mtime_iso", "full_hash"])
        for e in entries:
            w.writerow([str(e["path"]), e["size"], datetime.fromtimestamp(e["mtime"]).isoformat(),
                        e["full_hash"]])
    print(f"Wrote manifest: {manifest_path}  ({len(entries)} rows, includes content hashes)")

    dup_groups = find_dup_groups(entries)
    dupes_path = Path(args.dupes_out) if args.dupes_out else manifest_path.with_name("dupes.csv")
    write_dupes_csv(dupes_path, dup_groups)
    wasted = sum((len(g) - 1) * g[0]["size"] for g in dup_groups)
    print(f"Wrote duplicate report: {dupes_path}  ({len(dup_groups)} sets, "
          f"{wasted / (1024**3):.2f} GB reclaimable within this drive)")

    if bad:
        bad_path = manifest_path.with_name("unreadable.csv")
        with open(bad_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["path", "error"])
            w.writerows(bad)
        print(f"{len(bad)} files could not be read - see {bad_path}")


def find_dup_groups(entries):
    by_hash = defaultdict(list)
    for e in entries:
        if e["full_hash"]:  # skip zero-byte files, not meaningful duplicates
            by_hash[e["full_hash"]].append(e)
    return [group for group in by_hash.values() if len(group) >= 2]


def write_dupes_csv(dupes_path, dup_groups):
    with open(dupes_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["group_id", "hash", "path", "size", "mtime_iso"])
        for gid, group in enumerate(dup_groups, start=1):
            for e in group:
                w.writerow([gid, e["full_hash"], str(e["path"]), e["size"],
                            datetime.fromtimestamp(e["mtime"]).isoformat()])


# ----------------------------------------------------------- merge-dupes --

def cmd_merge_dupes(args):
    entries = []
    for manifest_path in args.manifests:
        with open(manifest_path, newline="") as f:
            reader = csv.DictReader(f)
            if "full_hash" not in (reader.fieldnames or []):
                sys.exit(f"{manifest_path} has no full_hash column - re-run `scan` "
                          f"with the current version of this script to produce it.")
            count = 0
            for row in reader:
                entries.append({"path": row["path"], "size": int(row["size"]),
                                 "mtime": datetime.fromisoformat(row["mtime_iso"]).timestamp(),
                                 "full_hash": row["full_hash"]})
                count += 1
            print(f"Loaded {count} rows from {manifest_path}")

    dup_groups = find_dup_groups(entries)
    out_path = Path(args.out)
    write_dupes_csv(out_path, dup_groups)
    wasted = sum((len(g) - 1) * g[0]["size"] for g in dup_groups)
    print(f"Wrote duplicate report: {out_path}  ({len(dup_groups)} sets across "
          f"{len(args.manifests)} manifests, {wasted / (1024**3):.2f} GB reclaimable)")


# -------------------------------------------------------------- report ----

def cmd_report(args):
    groups = defaultdict(list)
    with open(args.dupes_csv, newline="") as f:
        for row in csv.DictReader(f):
            groups[row["group_id"]].append(row)

    if not groups:
        print("No duplicates recorded.")
        return

    total_wasted = 0
    by_ext_wasted = defaultdict(int)
    for gid, rows in groups.items():
        size = int(rows[0]["size"])
        wasted = size * (len(rows) - 1)
        total_wasted += wasted
        ext = Path(rows[0]["path"]).suffix.lower() or "(none)"
        by_ext_wasted[ext] += wasted

    print(f"{len(groups)} duplicate sets, {total_wasted / (1024**3):.2f} GB reclaimable\n")
    print("Reclaimable space by file type:")
    for ext, wasted in sorted(by_ext_wasted.items(), key=lambda kv: -kv[1])[:15]:
        print(f"  {ext:>10}  {wasted / (1024**2):>10.1f} MB")

    print("\nLargest duplicate sets:")
    ranked = sorted(groups.items(), key=lambda kv: -(int(kv[1][0]["size"]) * (len(kv[1]) - 1)))
    for gid, rows in ranked[:10]:
        size = int(rows[0]["size"])
        print(f"  set {gid}: {len(rows)} copies x {size/(1024**2):.1f} MB")
        for row in rows:
            print(f"      {row['path']}")


# -------------------------------------------------------------- dedupe ----

def choose_keeper(rows, prefer_root):
    def score(row):
        p = row["path"]
        prefer = 0 if (prefer_root and p.startswith(prefer_root)) else 1
        depth = p.count(os.sep)
        return (prefer, depth, row["mtime_iso"], len(p))
    return sorted(rows, key=score)[0]


def cmd_dedupe(args):
    groups = defaultdict(list)
    with open(args.dupes_csv, newline="") as f:
        for row in csv.DictReader(f):
            groups[row["group_id"]].append(row)

    quarantine = Path(args.quarantine).resolve()
    all_paths = [row["path"] for rows in groups.values() for row in rows]
    common_root = Path(os.path.commonpath(all_paths)) if all_paths else Path("/")
    if common_root.is_file():
        common_root = common_root.parent

    moves = []
    for gid, rows in groups.items():
        keeper = choose_keeper(rows, args.prefer_root)
        for row in rows:
            if row is keeper:
                continue
            src = Path(row["path"])
            try:
                rel = src.relative_to(common_root)
            except ValueError:
                rel = src.name
            dest = quarantine / rel
            moves.append((src, dest, keeper["path"]))

    print(f"{len(moves)} duplicate files to move into {quarantine}")
    if not args.apply:
        print("DRY RUN - no files touched. Re-run with --apply to execute.")
        for src, dest, keeper in moves[:20]:
            print(f"  MOVE  {src}\n    ->  {dest}\n   (keeping {keeper})")
        if len(moves) > 20:
            print(f"  ...and {len(moves) - 20} more")
        return

    log_path = Path(args.log) if args.log else quarantine.with_name(quarantine.name + "_move_log.csv")
    quarantine.mkdir(parents=True, exist_ok=True)
    with open(log_path, "w", newline="") as logf:
        w = csv.writer(logf)
        w.writerow(["moved_from", "moved_to", "kept_original"])
        for src, dest, keeper in moves:
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(src), str(dest))
                w.writerow([str(src), str(dest), keeper])
            except OSError as e:
                print(f"  FAILED to move {src}: {e}")
    print(f"Moved {len(moves)} duplicates. Undo log: {log_path}")
    print("To undo a move: `mv \"<moved_to>\" \"<moved_from>\"` using the log above.")


# --------------------------------------------------------------- transfer --

# Path-substring markers (checked case-insensitively against the full source
# path) used to route files into a category. Order matters: first match wins,
# most-specific first. Anything matching nothing falls through to "archive"
# so nothing is ever silently dropped.
PHOTO_MARKERS = ["photoslibrary", "photolibrary", "icloud photos"]
MOVIE_MARKERS = ["itunes library/movies", "itunes library/home videos", "home videos",
                  "igorandiedrobo/movies"]
TV_MARKERS = ["itunes library/tv shows", "tv shows"]
MUSIC_MARKERS = ["itunes music", "music library"]


JUNK_DIR_NAMES = {".fseventsd", ".Trashes", ".Spotlight-V100"}


def has_junk_dir(path_str):
    """Defense-in-depth for manifests scanned before the iter_files junk-dir
    fix - catches leaked .fseventsd/.Trashes/.Spotlight-V100 entries even in
    already-completed manifests, without needing to rescan."""
    return any(p in JUNK_DIR_NAMES for p in Path(path_str).parts)


def classify(path_str):
    """Return (category, dest_subpath). dest_subpath is the path fragment
    after the matched marker, used to preserve the meaningful part of the
    original folder structure (e.g. the movie/album name) under DEST/<category>/.
    "photos" returns None for dest_subpath - the caller flattens those since
    Immich's own importer doesn't care about source folder structure."""
    low = path_str.lower()
    suffix = Path(path_str).suffix.lower()
    for marker in PHOTO_MARKERS:
        if marker in low:
            # Photos/iPhoto library bundles are full of internal bookkeeping
            # files alongside the actual images/videos - old iPhoto's
            # per-object metadata (.apversion/.apmaster/.apdetected/etc.),
            # .plist prefs, thumbnail caches, analysis databases. Only treat
            # it as a real photo/video if the extension says so; anything
            # else inside the bundle falls through to archive instead of
            # getting flattened into the Immich-import folder as junk.
            if suffix in PHOTO_EXTS or suffix in VIDEO_EXTS:
                return "photos", None
            break
    for marker in TV_MARKERS:
        idx = low.find(marker)
        if idx != -1:
            after = path_str[idx + len(marker):].lstrip("/\\")
            return "tv", after or Path(path_str).name
    for marker in MOVIE_MARKERS:
        idx = low.find(marker)
        if idx != -1:
            after = path_str[idx + len(marker):].lstrip("/\\")
            return "movies", after or Path(path_str).name
    for marker in MUSIC_MARKERS:
        idx = low.find(marker)
        if idx != -1:
            after = path_str[idx + len(marker):].lstrip("/\\")
            return "music", after or Path(path_str).name
    # loose audio/photo files outside a proper library folder (e.g. a
    # downloaded audiobook, or a camera RAW file dumped straight in a work
    # folder) still belong in their category, matched by extension as a
    # fallback. Video extensions are deliberately NOT given this treatment -
    # unlike audio/photo formats, a loose .mp4/.mkv is genuinely ambiguous
    # (could be a screen recording, work file, etc.) and misrouting it is a
    # bigger mistake than leaving it in archive for a human to sort.
    suffix = Path(path_str).suffix.lower()
    if suffix in AUDIO_EXTS:
        return "music", strip_volume_prefix(path_str)
    if suffix in PHOTO_EXTS:
        return "photos", None
    return "archive", strip_volume_prefix(path_str)


def strip_volume_prefix(path_str):
    """/Volumes/SomeDrive/rest/of/path -> rest/of/path (macOS mount convention).
    Falls back to the original path if it doesn't look like a /Volumes/ path."""
    parts = Path(path_str).parts
    if len(parts) >= 3 and parts[1] == "Volumes":
        rest = parts[3:]
        return str(Path(*rest)) if rest else Path(path_str).name
    return path_str.lstrip("/\\")


def parse_remap(specs):
    """--remap OLD=NEW - translates the source-drive root recorded in a
    manifest (from whatever machine ran `scan`) to wherever that drive is
    actually mounted on THIS machine right now. Classification is unaffected
    since it only depends on the relative path after the drive root."""
    remaps = []
    for spec in specs or []:
        if "=" not in spec:
            sys.exit(f"--remap must be OLD=NEW, got: {spec}")
        old, new = spec.split("=", 1)
        remaps.append((old, new))
    return remaps


def apply_remap(path_str, remaps):
    for old, new in remaps:
        if path_str.startswith(old):
            return new + path_str[len(old):]
    return path_str


def dest_root_for(category, args):
    override = {"photos": args.dest_photos, "movies": args.dest_movies, "tv": args.dest_tv,
                "music": args.dest_music, "archive": args.dest_archive}[category]
    if override:
        return Path(override)
    if args.dest:
        return Path(args.dest) / category
    sys.exit(f"No destination given for category '{category}' - pass --dest or --dest-{category}")


def fast_copy(src, dest, bufsize=8 * 1024 * 1024):
    """Like shutil.copy2, but uses an explicit large-buffer read/write loop
    instead of shutil's automatic os.sendfile()/fcopyfile() fast-path
    selection. That automatic fast-path was observed to degrade badly
    (~12MB/s vs ~85-100MB/s for plain `cp`/`dd`) when copying off a
    non-standard kernel filesystem module (linux-apfs-rw, used to read an
    APFS source drive) - reason not fully diagnosed, but an explicit
    read/write loop reliably matches `cp`-level throughput regardless of
    the source filesystem's sendfile() support/quirks."""
    with open(src, "rb") as fsrc, open(dest, "wb") as fdst:
        shutil.copyfileobj(fsrc, fdst, length=bufsize)
    shutil.copystat(src, dest)


def cmd_transfer(args):
    remaps = parse_remap(args.remap)
    manifest_rows = {}
    for mpath in args.manifests:
        with open(mpath, newline="") as f:
            count = 0
            for row in csv.DictReader(f):
                manifest_rows[row["path"]] = row
                count += 1
            print(f"Loaded {count} rows from {mpath}")
    print(f"{len(manifest_rows)} unique file paths total")

    skip_paths = set()
    if args.dupes:
        groups = defaultdict(list)
        with open(args.dupes, newline="") as f:
            for row in csv.DictReader(f):
                groups[row["group_id"]].append(row)
        for gid, rows in groups.items():
            keeper = choose_keeper(rows, args.prefer_root)
            for row in rows:
                if row is not keeper:
                    skip_paths.add(row["path"])
        print(f"{len(groups)} duplicate sets -> {len(skip_paths)} redundant copies will be skipped")

    plan = []  # (src_path, dest_path, category, size)
    seen_photo_names = defaultdict(int)
    counts = defaultdict(lambda: [0, 0])  # category -> [files, bytes]
    skipped_bytes = 0
    skipped_junk = 0
    for path, row in manifest_rows.items():
        if path in skip_paths:
            skipped_bytes += int(row["size"])
            continue
        if has_junk_dir(path):
            skipped_junk += 1
            continue
        category, subpath = classify(path)  # classification uses the recorded path, unaffected by remap
        actual_src = apply_remap(path, remaps)
        root = dest_root_for(category, args)
        if category == "photos":
            name = Path(path).name
            seen_photo_names[name] += 1
            if seen_photo_names[name] > 1:
                stem, suffix = os.path.splitext(name)
                name = f"{stem}_{seen_photo_names[name]}{suffix}"
            dest = root / name
        else:
            dest = root / subpath
        plan.append((Path(actual_src), dest, category, int(row["size"])))
        counts[category][0] += 1
        counts[category][1] += int(row["size"])

    print(f"\n{len(plan)} files to copy "
          f"(skipping {len(skip_paths)} known duplicates [{skipped_bytes / (1024**3):.2f} GB saved], "
          f"{skipped_junk} macOS junk-directory leftovers):")
    for cat, (n, size) in sorted(counts.items()):
        print(f"  {cat:>8}: {n:>8} files, {size / (1024**3):>8.2f} GB")

    if not args.apply:
        print("\nDRY RUN - no files touched. Re-run with --apply to execute.")
        for src, dest, cat, _size in plan[:20]:
            print(f"  [{cat}] {src}\n    -> {dest}")
        if len(plan) > 20:
            print(f"  ...and {len(plan) - 20} more")
        return

    if args.log:
        log_path = Path(args.log)
    elif args.dest:
        log_path = Path(args.dest) / "transfer_log.csv"
    else:
        sys.exit("Pass --log explicitly when not using --dest (no default location to write it)")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    copied = 0
    failed = 0
    with open(log_path, "w", newline="") as logf:
        w = csv.writer(logf)
        w.writerow(["source", "dest", "category", "size"])
        for src, dest, cat, size in plan:
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                fast_copy(str(src), str(dest))
                w.writerow([str(src), str(dest), cat, size])
                copied += 1
            except OSError as e:
                print(f"  FAILED to copy {src}: {e}")
                failed += 1
    print(f"\nCopied {copied} files ({failed} failed). Log: {log_path}")


# ---------------------------------------------------------------- verify --

def cmd_verify(args):
    root = Path(args.source).resolve()
    zero = []
    unreadable = []
    n = 0
    for path in iter_files(root):
        n += 1
        try:
            st = path.stat()
        except OSError as e:
            unreadable.append((str(path), str(e)))
            continue
        if st.st_size == 0:
            zero.append(str(path))
    print(f"Checked {n} files.")
    print(f"Zero-byte files: {len(zero)}")
    print(f"Unreadable files: {len(unreadable)}")
    if zero or unreadable:
        out = Path(args.out) if args.out else root.parent / "verify_report.csv"
        with open(out, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["issue", "path"])
            for p in zero:
                w.writerow(["zero_byte", p])
            for p, err in unreadable:
                w.writerow(["unreadable:" + err, p])
        print(f"Details written to {out}")


# ------------------------------------------------------------ sort-media --

def have_exiftool():
    return shutil.which("exiftool") is not None


def exif_dates(paths, batch_size=500):
    """Yield (path_str, iso_date_or_None) using one or more batched exiftool calls."""
    paths = [str(p) for p in paths]
    for i in range(0, len(paths), batch_size):
        chunk = paths[i:i + batch_size]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as tf:
            tf.write("\n".join(chunk))
            listfile = tf.name
        try:
            out = subprocess.run(
                ["exiftool", "-j", "-DateTimeOriginal", "-CreateDate", "-@", listfile],
                capture_output=True, text=True, check=False,
            )
            data = json.loads(out.stdout or "[]")
        except (subprocess.SubprocessError, json.JSONDecodeError):
            data = []
        finally:
            os.unlink(listfile)
        found = {d.get("SourceFile"): (d.get("DateTimeOriginal") or d.get("CreateDate")) for d in data}
        for p in chunk:
            yield p, found.get(p)


def parse_exif_date(s):
    if not s:
        return None
    try:
        # exiftool format: "2019:06:14 12:03:11"
        return datetime.strptime(s.split("+")[0].split(".")[0].strip(), "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None


def cmd_sort_media(args):
    root = Path(args.source).resolve()
    dest_root = Path(args.dest).resolve()

    media = []
    for path in iter_files(root):
        ext = path.suffix.lower()
        if ext in PHOTO_EXTS:
            media.append((path, "Photos"))
        elif ext in VIDEO_EXTS:
            media.append((path, "Videos"))

    print(f"Found {len(media)} photo/video files under {root}")

    dates = {}
    if have_exiftool():
        print("Using exiftool for capture dates...")
        for path_str, iso in exif_dates([p for p, _ in media]):
            dt = parse_exif_date(iso)
            if dt:
                dates[path_str] = dt
    else:
        print("exiftool not found (brew install exiftool for accurate capture dates) "
              "- falling back to file modified time for all files.")

    plan = []
    for path, kind in media:
        dt = dates.get(str(path))
        if dt is None:
            dt = datetime.fromtimestamp(path.stat().st_mtime)
        subdir = dest_root / kind / f"{dt.year:04d}" / f"{dt.year:04d}-{dt.month:02d}"
        target = subdir / path.name
        plan.append((path, target))

    # avoid collisions when two different source files would land on the same target name
    seen = defaultdict(int)
    final_plan = []
    for src, target in plan:
        key = str(target)
        seen[key] += 1
        if seen[key] > 1:
            target = target.with_name(f"{target.stem}_{seen[key]}{target.suffix}")
        final_plan.append((src, target))

    print(f"{len(final_plan)} files planned for {'copy' if args.copy else 'move'} into {dest_root}")
    if not args.apply:
        print("DRY RUN - no files touched. Re-run with --apply to execute.")
        for src, target in final_plan[:20]:
            print(f"  {src} -> {target}")
        if len(final_plan) > 20:
            print(f"  ...and {len(final_plan) - 20} more")
        return

    for src, target in final_plan:
        target.parent.mkdir(parents=True, exist_ok=True)
        if args.copy:
            fast_copy(str(src), str(target))
        else:
            shutil.move(str(src), str(target))
    print(f"Done. {len(final_plan)} files organized under {dest_root}")


# ------------------------------------------------------------------ main --

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="Catalog files and detect content-based duplicates")
    s.add_argument("source")
    s.add_argument("--out", default="manifest.csv")
    s.add_argument("--dupes-out", default=None)
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("merge-dupes", help="Find true duplicates across multiple manifest.csv files")
    s.add_argument("manifests", nargs="+")
    s.add_argument("--out", default="dupes.csv")
    s.set_defaults(func=cmd_merge_dupes)

    s = sub.add_parser("report", help="Summarize a dupes.csv")
    s.add_argument("dupes_csv")
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("dedupe", help="Move duplicate files into a quarantine folder")
    s.add_argument("dupes_csv")
    s.add_argument("--quarantine", required=True)
    s.add_argument("--prefer-root", default=None,
                    help="Path prefix to prefer as the 'keeper' when a set has multiple copies")
    s.add_argument("--log", default=None)
    s.add_argument("--apply", action="store_true")
    s.set_defaults(func=cmd_dedupe)

    s = sub.add_parser("transfer", help="Classify and copy deduped files into DEST/<category>/...")
    s.add_argument("manifests", nargs="+")
    s.add_argument("--dupes", default=None,
                    help="Combined dupes.csv - only one keeper per set is copied, the rest are skipped")
    s.add_argument("--dest", default=None,
                    help="Default root - categories land in DEST/<category>/ unless overridden below")
    s.add_argument("--dest-photos", default=None, help="Override destination root for the photos category")
    s.add_argument("--dest-movies", default=None, help="Override destination root for the movies category")
    s.add_argument("--dest-tv", default=None, help="Override destination root for the tv category")
    s.add_argument("--dest-music", default=None, help="Override destination root for the music category")
    s.add_argument("--dest-archive", default=None, help="Override destination root for the archive category")
    s.add_argument("--prefer-root", default=None,
                    help="Path prefix to prefer as the 'keeper' when a duplicate set spans multiple sources")
    s.add_argument("--remap", action="append", default=None,
                    help="OLD=NEW - translate a manifest's recorded source root to where that drive is "
                         "actually mounted on this machine (repeatable, e.g. the manifest was scanned on "
                         "a Mac at /Volumes/X but is being applied on a Linux box where X is mounted at /mnt/x)")
    s.add_argument("--log", default=None)
    s.add_argument("--apply", action="store_true")
    s.set_defaults(func=cmd_transfer)

    s = sub.add_parser("verify", help="Flag zero-byte / unreadable files")
    s.add_argument("source")
    s.add_argument("--out", default=None)
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("sort-media", help="Sort photos/videos by capture date")
    s.add_argument("source")
    s.add_argument("dest")
    s.add_argument("--copy", action="store_true", help="Copy instead of move")
    s.add_argument("--apply", action="store_true")
    s.set_defaults(func=cmd_sort_media)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
