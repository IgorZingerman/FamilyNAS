#!/usr/bin/env python3
"""
archive_cleanup.py - move confirmed-non-photo content (security camera
snapshots, work/website files, software junk) that got swept into Immich by
the extension-based classification fallback, OUT of Immich and into a plain
archive folder instead.

For each matched asset: copy the physical file from Immich's storage to the
archive destination, verify the copy (size match), then soft-trash the asset
in Immich (never a hard delete - uses the trash, which has a 30-day retention
window per this server's config, so this is recoverable if something's wrong).

Nothing is ever deleted outright. Dry-run by default; --apply to execute.

Usage:
  python3 archive_cleanup.py --log ~/familynas/recovery_scan/transfer_log.csv \
      --api-key <igor's personal API key> --archive-dest /tank/archive/_immich-cleanup
  python3 archive_cleanup.py --log ... --api-key ... --archive-dest ... --apply
"""

import argparse
import csv
import json
import shutil
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

API_BASE = "http://localhost:2283/api"
IMMICH_DATA_PREFIX = "/data"  # container-side prefix in originalPath
IMMICH_HOST_DATA_ROOT = "/tank/apps/immich/data"  # host-side UPLOAD_LOCATION


def match_prefix(prefix: str, category: str):
    def matcher(source: str):
        return category if prefix.lower() in source.lower() else None
    return matcher


def match_any(substrings: tuple, category: str):
    def matcher(source: str):
        low = source.lower()
        return category if any(s.lower() in low for s in substrings) else None
    return matcher


# Order matters: first match wins.
# NOTE: "IgorAndieDrobo/Public/Igors Y! Laptop" was in this list originally
# (assumed Yahoo-work content from the folder name) but real sampling found
# it's actually a personal laptop backup with real family photos, scanned
# original prints, and wedding place-card designs - removed. Always verify
# a junk-classification rule against real sampled paths before trusting it,
# same lesson as the earlier PHOTO_MARKERS/Photos-Migrated bugs.
JUNK_RULES = [
    match_prefix("iKorbDrives/Pictures/Sharx", "sharx-security-camera"),
    match_prefix("iKorbDrives/www", "ikorb-website"),
    match_prefix("iKorbDrives/Team", "ikorb-team-drive"),
    match_prefix("IgorAndieDrobo/Public/Yahoo! Ads", "yahoo-ads"),
    match_prefix("Buick_2.0/Andie/Pictures/Desktop Themes", "desktop-themes"),
    # "Downloads/" deliberately excluded here - real sampling found genuine
    # camera/iPhone-export photos saved directly into Downloads folders
    # (e.g. "20220623_161529.jpg", iPhone HEIC exports). Only match markers
    # that are unambiguously software/project artifacts, not a general
    # "things downloaded" folder which legitimately holds real photos too.
    match_any(("/DLs/", "/dls/", "node_modules", "/Projects/",
               "buildroot", ".app/", "GettingStarted", "Program Files"),
              "misc-downloads-software-junk"),
]


def classify_junk(source: str):
    for matcher in JUNK_RULES:
        cat = matcher(source)
        if cat:
            return cat
    return None


def api_request(method: str, path: str, api_key: str, body=None, retries: int = 3):
    url = API_BASE + path
    data = json.dumps(body).encode() if body is not None else None
    last_error = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, method=method, headers={
            "x-api-key": api_key,
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except json.JSONDecodeError:
                return e.code, raw.decode(errors="replace")
        except (TimeoutError, urllib.error.URLError) as e:
            last_error = e
            if attempt < retries - 1:
                print(f"  (retrying after transient error: {e})", file=sys.stderr)
    sys.exit(f"Request to {path} failed after {retries} attempts: {last_error}")


def build_filename_index(api_key: str) -> dict:
    index = {}
    page = 1
    while True:
        status, body = api_request("POST", "/search/metadata", api_key,
                                    {"size": 1000, "page": page})
        if status != 200:
            sys.exit(f"search/metadata failed (HTTP {status}): {body}")
        items = body["assets"]["items"]
        if not items:
            break
        for item in items:
            index[item["originalFileName"]] = (item["id"], item["originalPath"])
        next_page = body["assets"].get("nextPage")
        print(f"  ...indexed {len(index)} assets (page {page})", end="\r")
        if not next_page:
            break
        page = int(next_page)
    print(f"\nIndexed {len(index)} unique filenames")
    return index


def load_junk_groups(log_path: Path) -> dict:
    groups = defaultdict(list)
    for row in csv.DictReader(open(log_path, newline="")):
        if row["category"] != "photos":
            continue
        cat = classify_junk(row["source"])
        if cat is None:
            continue
        groups[cat].append((Path(row["dest"]).name, row["source"]))
    return groups


def host_path_for(original_path: str) -> Path:
    if not original_path.startswith(IMMICH_DATA_PREFIX):
        sys.exit(f"Unexpected originalPath (doesn't start with {IMMICH_DATA_PREFIX}): {original_path}")
    return Path(IMMICH_HOST_DATA_ROOT + original_path[len(IMMICH_DATA_PREFIX):])


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--log", required=True, type=Path)
    p.add_argument("--api-key", required=True)
    p.add_argument("--archive-dest", required=True, type=Path,
                    help="Root archive folder, e.g. /tank/archive/_immich-cleanup")
    p.add_argument("--log-file", default=None, type=Path,
                    help="Where to write the copy/trash audit log (default: <archive-dest>/cleanup_log.csv)")
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()

    if not args.log.exists():
        sys.exit(f"Not found: {args.log}")

    groups = load_junk_groups(args.log)
    print(f"{len(groups)} junk categories found:")
    for cat, items in sorted(groups.items()):
        print(f"  {cat}: {len(items)} files")

    print("\nIndexing existing Immich assets by filename (paginated, may take a bit)...")
    filename_index = build_filename_index(args.api_key)

    plan = []  # (category, filename, asset_id, original_path)
    unmatched = 0
    for cat, items in groups.items():
        for filename, source in items:
            entry = filename_index.get(filename)
            if entry is None:
                unmatched += 1
                continue
            asset_id, original_path = entry
            plan.append((cat, filename, asset_id, original_path))

    print(f"\n{len(plan)} assets resolved to real Immich assets, "
          f"{unmatched} filenames not found (expected: covers known upload failures / renamed collisions)")

    if not args.apply:
        print("\nDRY RUN - no files touched, nothing trashed. Re-run with --apply to execute.")
        by_cat = defaultdict(int)
        for cat, *_ in plan:
            by_cat[cat] += 1
        for cat, n in sorted(by_cat.items()):
            print(f"  {cat}: {n} assets would be archived")
        return

    log_path = args.log_file or (args.archive_dest / "cleanup_log.csv")
    args.archive_dest.mkdir(parents=True, exist_ok=True)

    copied_ids = []
    failed = []
    with open(log_path, "w", newline="") as logf:
        w = csv.writer(logf)
        w.writerow(["category", "asset_id", "immich_host_path", "archive_dest", "status"])
        for i, (cat, filename, asset_id, original_path) in enumerate(plan):
            src = host_path_for(original_path)
            dest = args.archive_dest / cat / filename
            # avoid collisions across categories/duplicate filenames
            if dest.exists():
                stem, suffix = dest.stem, dest.suffix
                dest = dest.with_name(f"{stem}_{asset_id[:8]}{suffix}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(src, dest)
                if src.stat().st_size != dest.stat().st_size:
                    raise IOError(f"size mismatch after copy: {src.stat().st_size} != {dest.stat().st_size}")
                w.writerow([cat, asset_id, str(src), str(dest), "copied"])
                copied_ids.append(asset_id)
            except OSError as e:
                w.writerow([cat, asset_id, str(src), str(dest), f"FAILED: {e}"])
                failed.append((asset_id, str(e)))
            if (i + 1) % 5000 == 0:
                print(f"  ...{i + 1}/{len(plan)} copied")

    print(f"\n{len(copied_ids)} files copied successfully, {len(failed)} failed to copy")
    if failed:
        print(f"  sample failures: {failed[:5]}")
        print("  (these were NOT trashed in Immich, since the copy didn't succeed)")

    print(f"\nTrashing {len(copied_ids)} successfully-archived assets in Immich (soft delete, 30-day recovery window)...")
    for i in range(0, len(copied_ids), 1000):
        batch = copied_ids[i:i + 1000]
        status, body = api_request("DELETE", "/assets", args.api_key,
                                    {"ids": batch, "force": False})
        if status not in (200, 204):
            print(f"  FAILED to trash batch {i}-{i+len(batch)} (HTTP {status}): {body}")
        else:
            print(f"  trashed {i + len(batch)}/{len(copied_ids)}")

    print(f"\nDone. Audit log: {log_path}")
    print(f"Archived files live under: {args.archive_dest}")
    print("Trashed assets remain recoverable from Immich's trash for 30 days if needed.")


if __name__ == "__main__":
    main()
