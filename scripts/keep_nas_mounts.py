#!/usr/bin/env python3
"""
keep_nas_mounts.py — keep macOS SMB mounts for the Jellyfin libraries from
going stale during multi-day ffmpeg runs.

Finder/SMB on macOS often looks mounted but then every access hangs in
uninterruptible sleep (`U` state). ffmpeg reading `/Volumes/tv` will sit at
0% CPU until the share is force-unmounted. This script periodically `stat`s
each mount (with a timeout), touches a hidden keepalive file, and with
`--apply` force-unmounts + remounts a share that is missing or hung.

Remount uses Keychain credentials via `osascript` `mount volume`. After a
forced unmount, in-flight ffmpeg jobs on that share are dead — restart
`pretranscode_apple.py encode`.

Usage
-----
  python3 scripts/keep_nas_mounts.py
  python3 scripts/keep_nas_mounts.py --once
  python3 scripts/keep_nas_mounts.py --loop --interval 30 --apply
  python3 scripts/keep_nas_mounts.py --once --apply

Requires config/pretranscode/.env (MOVIES_HOST, TV_HOST, MOVIES_SMB, TV_SMB).
Dry-run by default: reports ok/missing/hung and prints remount commands.
"""

from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = REPO_ROOT / "config" / "pretranscode" / ".env"

DEFAULT_TIMEOUT = 12
DEFAULT_INTERVAL = 30
KEEPALIVE_NAME = ".familynas-keepalive"
SMB_PORT = 445
REACH_TIMEOUT = 3.0
MAX_BACKOFF = 900  # seconds; cap for repeated-failure interval in --loop


def load_env() -> dict[str, str]:
    if not ENV_FILE.is_file():
        sys.exit(
            f"Missing {ENV_FILE}. Copy config/pretranscode/.env.example to "
            ".env and set MOVIES_HOST / TV_HOST / MOVIES_SMB / TV_SMB."
        )
    values: dict[str, str] = {}
    for raw in ENV_FILE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip().strip('"').strip("'")
    return values


def run_timed(cmd: list[str], timeout: float) -> subprocess.CompletedProcess | None:
    """Return None if the command exceeds timeout (typical hung SMB).

    Do not use subprocess.run(..., timeout=): a child in uninterruptible
    sleep ignores SIGKILL, and wait() blocks forever.
    """
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        return subprocess.CompletedProcess(cmd, 1, "", str(exc))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            stdout, stderr = proc.communicate()
            return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
        time.sleep(0.1)
    try:
        proc.kill()
    except OSError:
        pass
    return None


def probe(path: Path, timeout: float) -> str:
    """Return ok, missing, hung, broken, or error.

    stat alone is not enough: a wedged smbfs mount still stats fine while
    every readdir returns EACCES, which is what strands ffmpeg. So list the
    directory too.
    """
    result = run_timed(["/usr/bin/stat", "-f", "%N", str(path)], timeout)
    if result is None:
        return "hung"
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").lower()
        if "no such file" in err or "not a directory" in err:
            return "missing"
        return "error"

    listing = run_timed(["/bin/ls", "-1", str(path)], timeout)
    if listing is None:
        return "hung"
    if listing.returncode != 0:
        return "broken"
    return "ok"


def touch_keepalive(path: Path, timeout: float) -> str:
    """Write a hidden file so the share sees regular write traffic."""
    marker = path / KEEPALIVE_NAME
    result = run_timed(["/usr/bin/touch", str(marker)], timeout)
    if result is None:
        return "hung"
    if result.returncode != 0:
        return "denied"
    return "ok"


def force_unmount(path: Path, timeout: float) -> bool:
    result = run_timed(["/usr/sbin/diskutil", "unmount", "force", str(path)], timeout)
    if result is not None and result.returncode == 0:
        return True
    result = run_timed(["/sbin/umount", "-f", str(path)], timeout)
    return result is not None and result.returncode == 0


def mount_smb(url: str, timeout: float) -> bool:
    script = f'mount volume "{url}"'
    result = run_timed(["/usr/bin/osascript", "-e", script], timeout)
    if result is not None and result.returncode == 0:
        return True
    if result is not None and (result.stderr or result.stdout):
        print(f"         osascript: {(result.stderr or result.stdout).strip()}", file=sys.stderr, flush=True)
    opened = run_timed(["/usr/bin/open", url], timeout)
    return opened is not None and opened.returncode == 0


def env_hint(smb_url: str) -> str:
    """Host part of an smb:// URL, for the printed ssh suggestion."""
    rest = smb_url.split("://", 1)[-1]
    host = rest.split("/", 1)[0]
    return host.split("@")[-1]


def mounts_from_env(env: dict[str, str]) -> list[tuple[str, Path, str, Path]]:
    """(label, mount_path, smb_url, keepalive_dir)."""
    movies = Path(env.get("MOVIES_HOST") or "/Volumes/movies")
    tv = Path(env.get("TV_HOST") or "/Volumes/tv")
    output = Path(env.get("OUTPUT_HOST") or (movies / ".apple-directplay"))
    movies_smb = env.get("MOVIES_SMB") or "smb://igor@igorbot.local/movies"
    tv_smb = env.get("TV_SMB") or "smb://igor@igorbot.local/tv"
    movies_keep = output if str(output).startswith(str(movies)) else movies
    return [
        ("movies", movies, movies_smb, movies_keep),
        ("tv", tv, tv_smb, tv),
    ]


def check_one(
    label: str,
    path: Path,
    smb_url: str,
    keep_dir: Path,
    *,
    apply: bool,
    timeout: float,
    no_touch: bool,
) -> str:
    status = probe(path, timeout)
    extra = ""
    if status == "ok" and not no_touch:
        touched = touch_keepalive(keep_dir, timeout)
        if touched == "ok":
            extra = f" keepalive={keep_dir / KEEPALIVE_NAME}"
        elif touched == "denied":
            extra = " keepalive-readonly"
        else:
            extra = " keepalive-hung"
            status = "hung"

    print(f"{label:8} {status:7} {path}{extra}", flush=True)

    if status == "ok":
        return status
    if not apply:
        print(f"         would unmount force {path}", flush=True)
        print(f"         would mount {smb_url}", flush=True)
        return status

    if status != "missing":
        print(f"         unmount force {path}", flush=True)
        if not force_unmount(path, timeout=max(timeout, 20)):
            # Mounting over a wedged mount point spawns mount_smbfs processes
            # that are themselves unkillable, so stop here.
            print(
                f"         WEDGED: cannot unmount {path}. Reboot the Mac to clear it.\n"
                f"         On the NAS, drop the stale session first:\n"
                f"           ssh {env_hint(smb_url)} \"sudo smbstatus -S\"  # find the pid for this share\n"
                f"           ssh {env_hint(smb_url)} \"sudo kill <pid>\"",
                file=sys.stderr,
                flush=True,
            )
            return "wedged"

    print(f"         mount {smb_url}", flush=True)
    if not mount_smb(smb_url, timeout=max(timeout, 45)):
        print(f"         remount failed for {label}", file=sys.stderr, flush=True)
        return "error"
    time.sleep(2)
    again = probe(path, timeout)
    print(f"         after remount: {again}", flush=True)
    return again


def nas_reachable(smb_url: str) -> bool:
    """Quick TCP check on the SMB port. False when away from the home network,
    so we never unmount/remount (and never trigger Finder connect popups)."""
    host = env_hint(smb_url).split("@")[-1]
    try:
        with socket.create_connection((host, SMB_PORT), timeout=REACH_TIMEOUT):
            return True
    except OSError:
        return False


def run_pass(args: argparse.Namespace, env: dict[str, str]) -> int:
    worst = 0
    for label, path, url, keep in mounts_from_env(env):
        if not nas_reachable(url):
            print(f"{label:8} away    NAS unreachable; skipping", flush=True)
            worst = 2
            continue
        status = check_one(
            label,
            path,
            url,
            keep,
            apply=args.apply,
            timeout=args.timeout,
            no_touch=args.no_touch,
        )
        if status != "ok":
            worst = 1
    return worst


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--apply", action="store_true", help="Force-unmount and remount hung/missing shares")
    p.add_argument("--once", action="store_true", help="Single check (default if --loop is not set)")
    p.add_argument("--loop", action="store_true", help="Keep checking until Ctrl-C")
    p.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL,
        help=f"seconds between checks in --loop (default: {DEFAULT_INTERVAL})",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"seconds before treating stat/touch as hung (default: {DEFAULT_TIMEOUT})",
    )
    p.add_argument(
        "--no-touch",
        action="store_true",
        help="Only stat the mount; do not write .familynas-keepalive",
    )
    return p


def main() -> int:
    args = build_parser().parse_args()
    env = load_env()
    if not args.loop:
        return run_pass(args, env)
    print(
        f"keep_nas_mounts loop every {args.interval}s "
        f"({'apply' if args.apply else 'dry-run'}) timeout={args.timeout}s",
        flush=True,
    )
    rc = 0
    delay = max(5, args.interval)
    try:
        while True:
            rc = run_pass(args, env)
            # Back off while anything is failing/away; reset once all ok.
            if rc == 0:
                delay = max(5, args.interval)
            else:
                delay = min(delay * 2, MAX_BACKOFF)
            time.sleep(delay)
    except KeyboardInterrupt:
        print("stopped", flush=True)
        return rc


if __name__ == "__main__":
    sys.exit(main())
