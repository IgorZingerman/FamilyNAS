#!/usr/bin/env python3
"""
queue_watch.py - track Immich background job queue depth over time on
igorbot (and, for the ML-offloaded queues, the Mac running the offload
container) and render a progress table with deltas, trend arrows, and a
rough ETA per queue.

Each run: fetches current queue depths over SSH, appends a timestamped
checkpoint to a local history file, then prints a table using the last
few checkpoints. Run it again later (e.g. every couple hours) to see
progress build up - history persists between runs.

Usage:
  python3 queue_watch.py                          # default host/history file
  python3 queue_watch.py --host igorz@igorbot.local
  python3 queue_watch.py --columns 4               # show more checkpoints
  python3 queue_watch.py --history-file /path/to/history.json
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# (queue name, where it actually runs) - update this if you offload
# different/additional queues elsewhere.
QUEUES = [
    ("thumbnailGeneration", "NUC"),
    ("faceDetection", "Mac"),
    ("ocr", "Mac"),
    ("smartSearch", "Mac"),
    ("facialRecognition", "Mac"),
    ("videoConversion", "NUC"),
    ("metadataExtraction", "NUC"),
]

DEFAULT_HOST = "igorz@igorbot.local"
DEFAULT_HISTORY = Path(__file__).with_name(".queue_watch_history.json")

# thermal_zone "type" -> friendly label. Zones not listed here are skipped
# (e.g. "acpitz" reports a bogus negative value on this hardware - not a
# real sensor reading, so it's deliberately excluded rather than shown).
TEMP_ZONES = {
    "x86_pkg_temp": "CPU package",
    "pch_skylake": "PCH/chipset",
    "iwlwifi_1": "WiFi",
}


def fetch_queue_depths_and_temps(host: str) -> tuple[dict, dict]:
    names = " ".join(name for name, _ in QUEUES)
    remote_cmd = (
        f"for q in {names}; do "
        f'n=$(sudo docker exec immich_redis redis-cli LLEN "immich_bull:$q:wait"); '
        f'echo "QUEUE $q $n"; done; '
        f"for f in /sys/class/thermal/thermal_zone*/type; do "
        f'zone=$(cat "$f"); temp=$(cat "${{f%type}}temp"); '
        f'echo "TEMP $zone $temp"; done'
    )
    try:
        out = subprocess.run(
            ["ssh", host, remote_cmd],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout
    except subprocess.CalledProcessError as e:
        sys.exit(f"SSH command failed: {e.stderr.strip()}")
    except subprocess.TimeoutExpired:
        sys.exit("SSH command timed out - is the host reachable?")

    values = {}
    temps = {}
    for line in out.strip().splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        kind, name, raw = parts
        if not raw.lstrip("-").isdigit():
            continue
        if kind == "QUEUE":
            values[name] = int(raw)
        elif kind == "TEMP" and name in TEMP_ZONES:
            millideg = int(raw)
            temps[TEMP_ZONES[name]] = millideg / 1000.0

    missing = [name for name, _ in QUEUES if name not in values]
    if missing:
        sys.exit(f"Didn't get a reading for: {', '.join(missing)} - check queue names/host.")
    return values, temps


def load_history(path: Path) -> list:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def save_history(path: Path, history: list):
    path.write_text(json.dumps(history, indent=2))


def fmt_time(iso: str) -> str:
    dt = datetime.fromisoformat(iso)
    return dt.strftime("%a %m-%d, %-I:%M %p") if hasattr(dt, "strftime") else iso


def fmt_delta(delta: int) -> str:
    sign = "+" if delta > 0 else ""  # negative sign comes from the number itself
    return f"{sign}{delta:,}"


def estimate_eta(prev_val: int, prev_ts: str, cur_val: int, cur_ts: str) -> str:
    if cur_val == 0:
        return "done"
    prev_dt = datetime.fromisoformat(prev_ts)
    cur_dt = datetime.fromisoformat(cur_ts)
    minutes = (cur_dt - prev_dt).total_seconds() / 60
    if minutes <= 0:
        return "n/a"
    delta = cur_val - prev_val
    if delta > 0:
        return "no ETA — still growing"
    if delta == 0:
        return "no ETA — stalled (no change)"
    rate_per_min = -delta / minutes
    if rate_per_min <= 0:
        return "n/a"
    remaining_min = cur_val / rate_per_min
    hours = remaining_min / 60
    if hours < 1:
        return f"~{remaining_min:.0f} min"
    if hours < 48:
        return f"~{hours:.1f} hrs"
    return f"~{hours / 24:.1f} days"


def trend_arrow(prev_val: int, cur_val: int) -> str:
    if cur_val == 0:
        return "✓"  # done
    if cur_val < prev_val:
        return "↑"  # improving - queue shrinking
    if cur_val > prev_val:
        return "↓"  # worsening - queue growing
    return "→"  # unchanged


def render_table(rows: list, headers: list):
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))

    def hline(left, mid, right):
        return left + mid.join("─" * (w + 2) for w in widths) + right

    def data_row(cells):
        return "│" + "│".join(f" {str(c):<{w}} " for c, w in zip(cells, widths)) + "│"

    lines = [hline("┌", "┬", "┐")]
    lines.append(data_row(headers))
    lines.append(hline("├", "┼", "┤"))
    for i, row in enumerate(rows):
        if i > 0:
            lines.append(hline("├", "┼", "┤"))
        lines.append(data_row(row))
    lines.append(hline("└", "┴", "┘"))
    return "\n".join(lines)


def temp_trend_label(prev_temps: dict, cur_temps: dict) -> str:
    if "CPU package" not in prev_temps or "CPU package" not in cur_temps:
        return "Temps: (no prior reading yet to compare against)"
    delta = cur_temps["CPU package"] - prev_temps["CPU package"]
    if abs(delta) <= 1:
        return "Temps: stable."
    if delta > 1:
        return f"Temps: rising (+{delta:.1f}°C since last check)."
    return f"Temps: falling ({delta:.1f}°C since last check)."


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default=DEFAULT_HOST, help=f"SSH host (default: {DEFAULT_HOST})")
    p.add_argument("--history-file", type=Path, default=DEFAULT_HISTORY,
                    help=f"Where to persist checkpoints (default: {DEFAULT_HISTORY})")
    p.add_argument("--columns", type=int, default=3,
                    help="How many checkpoints to show, including this one (default: 3)")
    p.add_argument("--no-fetch", action="store_true",
                    help="Don't fetch a new reading, just re-render the table from history")
    args = p.parse_args()

    history = load_history(args.history_file)

    if not args.no_fetch:
        values, temps = fetch_queue_depths_and_temps(args.host)
        checkpoint = {"timestamp": datetime.now().astimezone().isoformat(),
                      "values": values, "temps": temps}
        history.append(checkpoint)
        save_history(args.history_file, history)

    if not history:
        sys.exit("No history yet - run without --no-fetch first.")

    shown = history[-args.columns:]

    cur_temps = shown[-1].get("temps") or {}
    if cur_temps:
        prev_temps = shown[-2].get("temps", {}) if len(shown) >= 2 else {}
        print(f"\n{temp_trend_label(prev_temps, cur_temps)}\n")
        temp_rows = [[sensor, f"{cur_temps[sensor]:g}°C"]
                     for sensor in TEMP_ZONES.values() if sensor in cur_temps]
        print(render_table(temp_rows, ["Sensor", "Reading"]))

    headers = ["Queue", "Where"]
    for i, cp in enumerate(shown):
        label = fmt_time(cp["timestamp"])
        headers.append(label if i == 0 else f"{label} (Δ)")
    headers += ["Trend", "Est. finish*"]

    rows = []
    for name, where in QUEUES:
        cells = [name, where]
        for i, cp in enumerate(shown):
            val = cp["values"].get(name, 0)
            if i == 0:
                cells.append(f"{val:,}")
            else:
                prev_val = shown[i - 1]["values"].get(name, 0)
                cells.append(f"{val:,} ({fmt_delta(val - prev_val)})")

        if len(shown) >= 2:
            prev_val = shown[-2]["values"].get(name, 0)
            cur_val = shown[-1]["values"].get(name, 0)
            cells.append(trend_arrow(prev_val, cur_val))
            cells.append(estimate_eta(prev_val, shown[-2]["timestamp"], cur_val, shown[-1]["timestamp"]))
        else:
            cells.append("-")
            cells.append("need 2+ checkpoints")
        rows.append(cells)

    print(f"\nQueue depth ({fmt_time(shown[-1]['timestamp'])}):\n")
    print(render_table(rows, headers))
    print("\n* Rough linear extrapolation from the most recent interval only - "
          "not a firm prediction, rates shift as job dependencies resolve.")


if __name__ == "__main__":
    main()
