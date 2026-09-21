#!/usr/bin/env python3
"""
build_albums.py - build real Immich albums from the recovered-photo transfer
log, using the locked-in path-pattern plan (see project memory /
conversation history for the full rationale behind each rule).

Reads transfer_log.csv (photos category only), matches each source path
against ALBUM_RULES, resolves the uploaded filename to an Immich asset ID via
the search API, and creates/populates one album per matched group.

Run on igorbot (localhost API + local transfer_log.csv, avoids shipping a
huge CSV over the network). Dry-run by default; --apply to actually create
albums.

Usage:
  python3 build_albums.py --log ~/familynas/recovery_scan/transfer_log.csv \
      --api-key <igor's personal API key, needs album.create/album.update/asset.read>
  python3 build_albums.py --log ... --api-key ... --apply
"""

import argparse
import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

import urllib.request
import urllib.error
import json

API_BASE = "http://localhost:2283/api"


def match_year_folder(prefix: str, year_range: range, album_prefix: str):
    """Returns a matcher for '<prefix>/<year>/...' paths, one album per year."""
    def matcher(source: str):
        low = source.lower()
        idx = low.find(prefix.lower())
        if idx == -1:
            return None
        rest = source[idx + len(prefix):].lstrip("/\\")
        m = re.match(r"(\d{4})", rest)
        if not m:
            return None
        year = int(m.group(1))
        if year not in year_range:
            return None
        return f"{album_prefix} {year}"
    return matcher


def match_prefix(prefix: str, album_name: str, exclude: tuple = ()):
    """exclude: substrings that disqualify a match even if prefix matches -
    used to keep auto-generated cache/derivative folders (e.g. an iPhoto
    library's Thumbnails/Previews) out of an album that's meant to be the
    real original photos."""
    def matcher(source: str):
        low = source.lower()
        if prefix.lower() not in low:
            return None
        if any(ex.lower() in low for ex in exclude):
            return None
        return album_name
    return matcher


# Order matters: first match wins. Each entry is a matcher function taking
# the full source path and returning an album name, or None if it doesn't apply.
ALBUM_RULES = [
    match_year_folder("IgorAndieDrobo/Previews", range(2011, 2016), "Family Photos"),
    match_prefix("IgorAndieDrobo/Wedding Raw Photos", "Wedding"),
    match_year_folder("Buick_2.0/Andie/Pictures", range(2001, 2009), "Andie's Photos"),
    match_prefix("GoProImports/2017", "GoPro Imports 2017"),
    match_prefix("IgorAndieDrobo/Public/Mom & Dad Backup", "Mom & Dad Backup"),
    match_prefix("Buick_2.0/Andie/Docs", "Family Docs & Trips"),
    match_prefix("Pictures/ginger_wedpix_rawfiles", "Ginger's Wedding"),
    match_prefix("Photos Migrated/Pictures", "Photos Migrated (Old iPhoto Import)",
                 exclude=("migratedphotolibrary/Thumbnails", "migratedphotolibrary/Previews")),
    match_prefix("Buick_2.0/Andie/Pictures/UBB", "UBB"),
]


def classify_album(source: str):
    for matcher in ALBUM_RULES:
        album = matcher(source)
        if album:
            return album
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
    """Paginate through every asset and build originalFileName -> asset id.
    Warns (doesn't fail) on duplicate filenames - first one wins, matches
    whatever transfer.py's collision-safe renaming already guaranteed should
    be rare/nonexistent in practice."""
    index = {}
    dupes = 0
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
            name = item["originalFileName"]
            if name in index:
                dupes += 1
            else:
                index[name] = item["id"]
        next_page = body["assets"].get("nextPage")
        print(f"  ...indexed {len(index)} assets (page {page})", end="\r")
        if not next_page:
            break
        page = int(next_page)
    print(f"\nIndexed {len(index)} unique filenames ({dupes} duplicate filenames skipped)")
    return index


def load_album_groups(log_path: Path) -> dict:
    groups = defaultdict(list)
    unmatched_photos = 0
    with open(log_path, newline="") as f:
        for row in csv.DictReader(f):
            if row["category"] != "photos":
                continue
            album = classify_album(row["source"])
            if album is None:
                unmatched_photos += 1
                continue
            filename = Path(row["dest"]).name
            groups[album].append(filename)
    print(f"{unmatched_photos} photos not matched to any album rule "
          f"(expected - most photos live in opaque library bundles with no "
          f"recoverable album info, or are confirmed non-personal content)")
    return groups


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--log", required=True, type=Path, help="Path to transfer_log.csv")
    p.add_argument("--api-key", required=True, help="Immich API key for the target account")
    p.add_argument("--apply", action="store_true", help="Actually create albums (default: dry run)")
    args = p.parse_args()

    if not args.log.exists():
        sys.exit(f"Not found: {args.log}")

    groups = load_album_groups(args.log)

    print(f"\n{len(groups)} albums to build:")
    for name, filenames in sorted(groups.items()):
        print(f"  {name}: {len(filenames)} files")

    print("\nIndexing existing Immich assets by filename (paginated, may take a bit)...")
    filename_index = build_filename_index(args.api_key)

    plan = {}  # album_name -> [asset_ids]
    total_unmatched = 0
    for name, filenames in groups.items():
        ids = []
        unmatched = []
        for fn in filenames:
            aid = filename_index.get(fn)
            if aid:
                ids.append(aid)
            else:
                unmatched.append(fn)
        plan[name] = ids
        total_unmatched += len(unmatched)
        if unmatched:
            print(f"  [{name}] {len(unmatched)} filenames not found in Immich "
                  f"(sample: {unmatched[:3]})")

    print(f"\n{sum(len(v) for v in plan.values())} assets resolved, "
          f"{total_unmatched} filenames could not be matched to an uploaded asset "
          f"(expected: covers the 292 known upload failures - see project notes).")

    if not args.apply:
        print("\nDRY RUN - no albums created. Re-run with --apply to execute.")
        return

    for name, ids in plan.items():
        if not ids:
            print(f"Skipping '{name}' - no resolved assets")
            continue
        status, body = api_request("POST", "/albums", args.api_key,
                                    {"albumName": name, "assetIds": ids[:1000]})
        if status != 201:
            print(f"FAILED to create album '{name}' (HTTP {status}): {body}")
            continue
        album_id = body["id"]
        print(f"Created '{name}' (id {album_id}) with first {min(len(ids), 1000)} assets")

        # add remaining assets in batches of 1000 if the album is larger
        remaining = ids[1000:]
        for i in range(0, len(remaining), 1000):
            batch = remaining[i:i + 1000]
            status, body = api_request("PUT", f"/albums/{album_id}/assets", args.api_key,
                                        {"ids": batch})
            if status != 200:
                print(f"  FAILED to add batch to '{name}' (HTTP {status}): {body}")
        print(f"  '{name}' complete: {len(ids)} total assets")

    print("\nDone.")


if __name__ == "__main__":
    main()
