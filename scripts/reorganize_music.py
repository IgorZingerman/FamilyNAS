#!/usr/bin/env python3
"""
reorganize_music.py - flatten the chaotic /tank/media/music tree (a raw dump
of whatever top-level folder names each source drive happened to use - some
already artist-named, some whole iTunes library exports, some merged/misnamed
directories) into a clean <Artist>/<Album>/<track> layout driven by each
file's own embedded tags rather than its folder path, since folder names
turned out to be unreliable (see e.g. "Eagle-Eye Cherry_Santana" - two
different artists' albums merged under one combined directory name).

Also filters out non-music junk that got swept in by the original
extension-based classification fallback (iOS .ipa app installers under
"Mobile Applications/", website assets under "iKorbDrives/www/", stray
.txt/.plist/.xml/.pdf files, a misfiled Tarzan .mp4) - same category of
mistake as PHOTO_MARKERS/junk-dir issues documented in organize_recovery.py,
just discovered in the music tree instead.

Dedup: exact byte-identical files (SHA256) are collapsed to one copy.
Near-duplicates (same Artist+Title tag pair, different files - e.g. the same
song ripped twice at different bitrates, or a DRM'd .m4p next to a clean
.mp3) keep only the largest file as a "probably better quality" heuristic;
everything else in that group is logged as a skipped duplicate, never
deleted outright.

Writes into a NEW sibling tree (/tank/media/music_organized by default)
rather than reorganizing in place, so the current working library keeps
serving Jellyfin untouched until the result is verified - swap it in
manually once you're happy with it.

Dry-run by default; --apply to actually copy files.

Usage:
  python3 reorganize_music.py --source /tank/media/music \
      --dest /tank/media/music_organized --archive-dest /tank/archive/music-junk
  python3 reorganize_music.py --source ... --dest ... --archive-dest ... --apply
"""

import argparse
import hashlib
import shutil
import sys
from collections import defaultdict
from pathlib import Path

from mutagen import File as MutagenFile

AUDIO_EXTS = {".mp3", ".m4a", ".m4p", ".flac", ".wav", ".aac", ".ogg", ".opus", ".wma"}

# Extensions/paths that are unambiguously not music, regardless of where
# extension-based classification swept them into the music tree.
JUNK_EXTS = {".ipa", ".txt", ".plist", ".xml", ".pdf", ".aiff", ".app", ".mp4",
             ".jpg", ".jpeg", ".png", ".ds_store", ".itl", ".itdb"}
# "Voice Memos" and game/software asset dirs matched real audio extensions
# (.wav/.m4a/.wma) so extension filtering alone didn't catch them - found by
# sampling which top-level folders were being used as a fallback "artist"
# name for untagged files (see dry-run analysis) and checking a few by hand:
# personal voice recordings and Quake 3 sound effects, not music.
JUNK_PATH_MARKERS = ("Mobile Applications/", "iKorbDrives/www/", ".app/",
                      "Voice Memos/", "Quake 3", "/baseq3/", "/UBB/")

# Top-level (or generic mid-path) folder names seen in the recovered tree
# that are dump/source-drive labels, never real artist names - e.g.
# "IDE Drives/Compaq/My Documents/My Music/matchbox twenty/<album>/<track>"
# where the real artist is 2 levels further in than the top folder. Used to
# skip past these when guessing an artist for untagged files instead of
# fabricating a fake artist like "IDE Drives" or "2023 Work Mac".
GENERIC_FOLDER_BLOCKLIST = {
    "igorandiedrobo", "buick_2.0", "2023 work mac", "downloads - unsorted",
    "downloads", "ide drives", "music", "itunes library", "backup itunes",
    "public", "pictures", "documents", "my music", "my documents", "compaq",
}

UNKNOWN_ARTIST = "Unknown Artist"
UNKNOWN_ALBUM = "Unknown Album"


def sanitize(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return ""
    for bad in '/\\:*?"<>|':
        name = name.replace(bad, "-")
    return name.rstrip(". ")


def read_tags(path: Path):
    """Returns (artist, album, title) - any of which may be None if unreadable
    or absent. Never raises; unreadable files (including permission errors -
    some recovered files came through as root-only 0600) are treated as
    tag-less rather than aborting the whole scan."""
    try:
        audio = MutagenFile(path, easy=True)
    except Exception:
        return None, None, None
    if audio is None or not audio.tags:
        return None, None, None
    tags = audio.tags

    def first(key):
        vals = tags.get(key)
        return sanitize(vals[0]) if vals else None

    artist = first("artist") or first("albumartist")
    album = first("album")
    title = first("title")
    return artist or None, album or None, title or None


def is_junk(path: Path) -> bool:
    if path.name.startswith("."):
        return True
    if path.suffix.lower() in JUNK_EXTS:
        return True
    s = str(path)
    return any(marker in s for marker in JUNK_PATH_MARKERS)


def sha256_of(path: Path, bufsize=8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(bufsize):
            h.update(chunk)
    return h.hexdigest()


def fast_copy(src: Path, dest: Path, bufsize=8 * 1024 * 1024):
    with open(src, "rb") as fsrc, open(dest, "wb") as fdst:
        shutil.copyfileobj(fsrc, fdst, length=bufsize)
    shutil.copystat(src, dest)


def iter_files(root: Path):
    for p in root.rglob("*"):
        if p.is_file():
            yield p


def fallback_artist_from_path(path: Path, source_root: Path) -> str:
    """Best guess when tags don't give us an artist: walk the path from the
    source root downward and take the first folder name that isn't a known
    dump/source-drive label (see GENERIC_FOLDER_BLOCKLIST). For well-organized
    subtrees this correctly skips e.g. "IDE Drives/Compaq/My Documents/
    My Music/" and lands on "matchbox twenty". For messier ones it's still
    just a best guess (e.g. "Igors Y! Laptop" isn't a real artist either),
    but it's transparent and reviewable rather than silently wrong."""
    rel = path.relative_to(source_root)
    folders = rel.parts[:-1]  # exclude the filename itself
    for folder in folders:
        if folder.strip().lower() not in GENERIC_FOLDER_BLOCKLIST:
            return sanitize(folder) or UNKNOWN_ARTIST
    return UNKNOWN_ARTIST


def build_plan(source_root: Path):
    """Returns (music_plan, junk_files, unreadable_count).
    music_plan: list of (path, artist, album, title, size, sha256)"""
    candidates = []
    junk_files = []
    for path in iter_files(source_root):
        if is_junk(path):
            junk_files.append(path)
            continue
        if path.suffix.lower() not in AUDIO_EXTS:
            junk_files.append(path)
            continue
        candidates.append(path)

    print(f"Scanning {len(candidates)} audio files for tags + hashing (this takes a while)...")
    entries = []
    no_tags = 0
    unreadable = []
    for i, path in enumerate(candidates):
        try:
            size = path.stat().st_size
            digest = sha256_of(path)
        except OSError as e:
            unreadable.append((path, str(e)))
            continue
        artist, album, title = read_tags(path)
        if not artist:
            artist = fallback_artist_from_path(path, source_root)
            no_tags += 1
        if not album:
            album = UNKNOWN_ALBUM
        entries.append((path, artist, album, title, size, digest))
        if (i + 1) % 1000 == 0:
            print(f"  ...{i + 1}/{len(candidates)}")

    print(f"{no_tags} files had no usable artist tag (used top-level folder name instead)")
    if unreadable:
        print(f"{len(unreadable)} files could not be read at all (permissions/IO errors) - "
              f"treated as junk, sample: {unreadable[0]}")
    return entries, junk_files + [p for p, _ in unreadable]


def dedup(entries):
    """Collapses exact byte-duplicates (by sha256) and same Artist+Title
    near-duplicates (keeping the largest file). Returns (keepers, skipped)
    where skipped is a list of (path, reason, kept_instead_path)."""
    by_hash = defaultdict(list)
    for e in entries:
        by_hash[e[5]].append(e)

    hash_keepers = []
    skipped = []
    for digest, group in by_hash.items():
        group.sort(key=lambda e: str(e[0]))
        keeper = group[0]
        hash_keepers.append(keeper)
        for dupe in group[1:]:
            skipped.append((dupe[0], "exact duplicate (same content)", keeper[0]))

    by_artist_title = defaultdict(list)
    for e in hash_keepers:
        _, artist, _, title, *_ = e
        key = (artist.lower(), (title or "").lower())
        by_artist_title[key].append(e)

    final_keepers = []
    for key, group in by_artist_title.items():
        if len(group) == 1 or not key[1]:
            # no title tag to group on safely, or only one candidate -
            # keep all of them rather than risk merging unrelated tracks
            final_keepers.extend(group)
            continue
        group.sort(key=lambda e: e[4], reverse=True)  # largest first
        keeper = group[0]
        final_keepers.append(keeper)
        for dupe in group[1:]:
            skipped.append((dupe[0], "same artist+title, smaller file", keeper[0]))

    return final_keepers, skipped


def plan_destinations(keepers, dest_root: Path):
    """Returns list of (src, dest) with collision-safe naming."""
    used = set()
    plan = []
    for path, artist, album, title, size, digest in keepers:
        artist_dir = sanitize(artist) or UNKNOWN_ARTIST
        album_dir = sanitize(album) or UNKNOWN_ALBUM
        filename = path.name
        dest = dest_root / artist_dir / album_dir / filename
        base_dest = dest
        n = 1
        while dest in used:
            n += 1
            dest = base_dest.with_name(f"{base_dest.stem}_{n}{base_dest.suffix}")
        used.add(dest)
        plan.append((path, dest))
    return plan


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", required=True, type=Path)
    p.add_argument("--dest", required=True, type=Path,
                    help="New organized tree root, e.g. /tank/media/music_organized")
    p.add_argument("--archive-dest", required=True, type=Path,
                    help="Where non-music junk gets moved, e.g. /tank/archive/music-junk")
    p.add_argument("--log", type=Path, default=None,
                    help="CSV log of src->dest + skipped duplicates (default: <dest>/reorganize_log.csv)")
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()

    if not args.source.exists():
        sys.exit(f"Not found: {args.source}")

    entries, junk_files = build_plan(args.source)
    keepers, skipped = dedup(entries)
    plan = plan_destinations(keepers, args.dest)

    artists = sorted({e[1] for e in keepers})
    exact_dupes = sum(1 for _, reason, _ in skipped if reason.startswith("exact"))
    near_dupes = len(skipped) - exact_dupes

    print(f"\n{len(entries)} audio files scanned")
    print(f"{len(junk_files)} non-music junk files found (extensions/paths don't match audio)")
    print(f"{len(artists)} distinct artists identified")
    print(f"{exact_dupes} exact-duplicate files collapsed")
    print(f"{near_dupes} same-song near-duplicates collapsed (kept the largest file)")
    print(f"{len(plan)} files will be copied into the organized tree")

    if not args.apply:
        print("\nDRY RUN - nothing copied/moved. Re-run with --apply to execute.")
        print("\nSample of planned layout:")
        for src, dest in plan[:15]:
            print(f"  {src}\n    -> {dest}")
        print("\nSample junk files that would move to archive:")
        for f in junk_files[:15]:
            print(f"  {f}")
        return

    log_path = args.log or (args.dest / "reorganize_log.csv")
    args.dest.mkdir(parents=True, exist_ok=True)
    args.archive_dest.mkdir(parents=True, exist_ok=True)

    import csv
    with open(log_path, "w", newline="") as logf:
        w = csv.writer(logf)
        w.writerow(["action", "source", "dest_or_reason"])

        copied = 0
        failed = 0
        for src, dest in plan:
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                fast_copy(src, dest)
                w.writerow(["copied", str(src), str(dest)])
                copied += 1
            except OSError as e:
                w.writerow(["FAILED", str(src), str(e)])
                failed += 1
            if copied % 1000 == 0 and copied:
                print(f"  ...{copied}/{len(plan)} copied")

        for path, reason, kept in skipped:
            w.writerow(["skipped_duplicate", str(path), f"{reason}; kept {kept}"])

        for path in junk_files:
            dest = args.archive_dest / path.relative_to(args.source)
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                fast_copy(path, dest)
                w.writerow(["archived_junk", str(path), str(dest)])
            except OSError as e:
                w.writerow(["FAILED_junk", str(path), str(e)])

    print(f"\nDone. {copied} copied ({failed} failed), {len(skipped)} duplicates skipped, "
          f"{len(junk_files)} junk files archived.")
    print(f"Log: {log_path}")
    print(f"\nOrganized tree is at {args.dest} - the original {args.source} was NOT modified.")
    print("Once you've verified it in Jellyfin, swap the docker-compose mount over and remove the old tree.")


if __name__ == "__main__":
    main()
