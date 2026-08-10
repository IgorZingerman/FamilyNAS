#!/usr/bin/env python3
"""FamilyNAS dropbox watcher: auto-imports files dropped into the Samba
`dropbox` share into Immich (photos/) or Jellyfin (media/), attributed to the
uploading family member via Samba file ownership. Stdlib-only; shells out to
`inotifywait` and `curl`. Runs as root via systemd
(familynas-dropbox-watcher.service — see the accompanying unit file).

Edit the path/URL constants below to match your own deployment before use.
"""
import csv
import json
import os
import pwd
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone

DROPBOX_ROOT = "/tank/dropbox"
ARCHIVE_PHOTOS_DIR = os.path.join(DROPBOX_ROOT, "archive", "photos")
FAILED_PHOTOS_DIR = os.path.join(DROPBOX_ROOT, "failed", "photos")
FAILED_MEDIA_DIR = os.path.join(DROPBOX_ROOT, "failed", "media")

# Where media/ contents get moved. These must match whatever your Jellyfin
# docker-compose.yml actually mounts as its movie/music library paths — if
# you ever repoint one, repoint the other (see docs/troubleshooting.md's
# "config-drift" gotcha in the full write-up this repo is based on).
MOVIES_DEST = "/tank/media/movies"
MUSIC_DEST = "/tank/media/music"

IMMICH_URL = "http://127.0.0.1:2283"
JELLYFIN_URL = "http://127.0.0.1:8096"
IMMICH_KEYS_FILE = "/etc/familynas/immich-api-keys.env"
JELLYFIN_KEY_FILE = "/etc/familynas/jellyfin-api-key"
LOG_FILE = "/var/log/familynas-dropbox.log"

AUDIO_EXTS = {".mp3", ".m4a", ".flac", ".wav", ".aac", ".ogg", ".opus"}
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".wmv", ".webm"}


def load_immich_keys():
    """/etc/familynas/immich-api-keys.env format: one `username=api-key` per
    line. Each key needs asset.upload + asset.read + asset.update permissions
    (the last one is required for favorites-tagging — see
    docs/troubleshooting.md for what happens if you forget it)."""
    keys = {}
    with open(IMMICH_KEYS_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or "=" not in line:
                continue
            user, key = line.split("=", 1)
            keys[user] = key
    return keys


def load_jellyfin_key():
    with open(JELLYFIN_KEY_FILE) as f:
        return f.read().strip()


def log_event(uploader, category, filename, status, detail=""):
    is_new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(["timestamp", "uploader", "category", "filename", "status", "detail"])
        writer.writerow([datetime.now(timezone.utc).isoformat(), uploader, category, filename, status, detail])


def owner_username(path):
    uid = os.stat(path).st_uid
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return f"uid-{uid}"


def unique_dest(dest_dir, filename):
    """Avoid clobbering an existing file with the same name at the destination."""
    dest = os.path.join(dest_dir, filename)
    if not os.path.exists(dest):
        return dest
    base, ext = os.path.splitext(filename)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return os.path.join(dest_dir, f"{base}_{ts}{ext}")


def move_to(path, dest_dir):
    os.makedirs(dest_dir, exist_ok=True)
    dest = unique_dest(dest_dir, os.path.basename(path))
    shutil.move(path, dest)
    return dest


def curl_json(args, timeout=30):
    """Run curl with -w to capture the HTTP status code alongside the body,
    so success/failure is judged by the actual response status rather than
    curl's own exit code (which is 0 even for 4xx/5xx responses) or by
    guessing from response-body shape. See docs/troubleshooting.md — this
    exact mistake silently turned a failed API call into a logged 'success'
    during development."""
    result = subprocess.run(
        ["curl", "-s", "-w", "\n%{http_code}"] + args,
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"curl failed: {result.stderr.strip()}")
    body, _, status = result.stdout.rpartition("\n")
    if not status.isdigit() or not (200 <= int(status) < 300):
        raise RuntimeError(f"HTTP {status}: {body.strip()[:200]}")
    return json.loads(body) if body.strip() else None


def upload_to_immich(path, api_key):
    """Returns (asset_id, was_duplicate) on success, raises on failure."""
    mtime = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc).isoformat()
    device_asset_id = f"dropbox-{uuid.uuid4()}"
    data = curl_json(
        [
            "-X", "POST", f"{IMMICH_URL}/api/assets",
            "-H", f"x-api-key: {api_key}",
            "-F", f"deviceAssetId={device_asset_id}",
            "-F", "deviceId=familynas-dropbox",
            "-F", f"fileCreatedAt={mtime}",
            "-F", f"fileModifiedAt={mtime}",
            "-F", f"assetData=@{path};filename={os.path.basename(path)}",
        ],
        timeout=120,
    )
    if not data or "id" not in data:
        raise RuntimeError(f"upload rejected: {data}")
    return data["id"], data.get("status") == "duplicate"


def mark_favorite(asset_id, api_key):
    curl_json(
        [
            "-X", "PUT", f"{IMMICH_URL}/api/assets",
            "-H", f"x-api-key: {api_key}",
            "-H", "Content-Type: application/json",
            "-d", json.dumps({"ids": [asset_id], "isFavorite": True}),
        ]
    )


def refresh_jellyfin_library(jf_key):
    try:
        subprocess.run(
            ["curl", "-s", "-X", "POST", f"{JELLYFIN_URL}/Library/Refresh",
             "-H", f"X-Emby-Token: {jf_key}"],
            capture_output=True, timeout=30,
        )
    except Exception as e:
        print(f"warning: jellyfin refresh failed: {e}", file=sys.stderr)


def handle_photo(path, is_favorite, immich_keys):
    filename = os.path.basename(path)
    uploader = owner_username(path)
    category = "favorites" if is_favorite else "photos"

    api_key = immich_keys.get(uploader)
    if not api_key:
        move_to(path, FAILED_PHOTOS_DIR)
        log_event(uploader, category, filename, "failed", f"no Immich API key on file for user '{uploader}'")
        return

    try:
        asset_id, was_dup = upload_to_immich(path, api_key)
        if is_favorite:
            mark_favorite(asset_id, api_key)
        date_dir = os.path.join(ARCHIVE_PHOTOS_DIR, datetime.now().strftime("%Y-%m-%d"))
        move_to(path, date_dir)
        detail = "duplicate, already in library" if was_dup else f"asset {asset_id}"
        log_event(uploader, category, filename, "success", detail)
    except Exception as e:
        move_to(path, FAILED_PHOTOS_DIR)
        log_event(uploader, category, filename, "failed", str(e))


def handle_media(path, jf_key):
    filename = os.path.basename(path)
    uploader = owner_username(path)
    ext = os.path.splitext(filename)[1].lower()

    if ext in AUDIO_EXTS:
        dest_dir, kind = MUSIC_DEST, "music"
    elif ext in VIDEO_EXTS:
        dest_dir, kind = MOVIES_DEST, "movie"
    else:
        move_to(path, FAILED_MEDIA_DIR)
        log_event(uploader, "media", filename, "failed", f"unrecognized extension '{ext}'")
        return

    try:
        dest = move_to(path, dest_dir)
        refresh_jellyfin_library(jf_key)
        log_event(uploader, "media", filename, "success", f"moved to {kind} library: {dest}")
    except Exception as e:
        log_event(uploader, "media", filename, "failed", str(e))


def classify_and_handle(path, immich_keys, jf_key):
    if not os.path.isfile(path):
        return
    filename = os.path.basename(path)
    if filename.startswith("."):
        return  # ignore dotfiles/junk

    rel = os.path.relpath(path, DROPBOX_ROOT)
    top = rel.split(os.sep)[0]

    if top in ("archive", "failed"):
        return  # our own moves land here; never reprocess

    if rel.startswith(os.path.join("photos", "favorites") + os.sep):
        handle_photo(path, is_favorite=True, immich_keys=immich_keys)
    elif top == "photos":
        handle_photo(path, is_favorite=False, immich_keys=immich_keys)
    elif top == "media":
        handle_media(path, jf_key=jf_key)
    else:
        log_event(owner_username(path), "unknown", filename, "failed", f"file dropped outside known folders: {rel}")


def main():
    immich_keys = load_immich_keys()
    jf_key = load_jellyfin_key()

    proc = subprocess.Popen(
        ["inotifywait", "-m", "-r", "-e", "close_write,moved_to", "--format", "%w%f", DROPBOX_ROOT],
        stdout=subprocess.PIPE, text=True, bufsize=1,
    )
    print(f"familynas-dropbox-watcher started, watching {DROPBOX_ROOT}", flush=True)
    for line in proc.stdout:
        path = line.rstrip("\n")
        try:
            classify_and_handle(path, immich_keys, jf_key)
        except Exception as e:
            print(f"unexpected error handling {path}: {e}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
