# CLAUDE.md

Guidance for Claude Code when working in this repository.

**AGENTS.md is the canonical entry map** — read it first for doc index, repo
layout, boundaries, and verification. This file adds Claude-specific command
cheatsheets that complement it.

## What this is

FamilyNAS documentation and tooling: a self-hosted NAS stack (Immich, Jellyfin,
Samba dropbox on Ubuntu/ZFS) plus Python scripts for recovering data from a
failed Drobo and post-import cleanup. Edited on a Mac; the live stack runs on
**igorbot** (`igorbot.local`).

## Do not

- **Never hard-delete** in recovery/cleanup scripts — quarantine, archive, skip,
  or Immich soft-trash only.
- **Dry-run by default** — filesystem and API writes require explicit `--apply`.
- Don't commit `.env`, API keys, `scan_results/`, `logs/`, or CSV manifests.
- Don't edit `scripts/dropbox-watcher.py` — **`config/dropbox-watcher.py`** is
  the canonical deploy template.

## Commands

No build step or test runner. Verification is syntax-check + dry-run:

```bash
# Syntax-check changed Python
python3 -m py_compile scripts/organize_recovery.py

# Recovery pipeline (dry run until --apply)
python3 scripts/organize_recovery.py scan /Volumes/DriveA --out manifest_a.csv
python3 scripts/organize_recovery.py report manifest_a.csv

# Immich queue snapshot (SSH to NAS)
python3 scripts/queue_watch.py

# Music reorg (requires mutagen; dry run by default)
python3 scripts/reorganize_music.py --source /tank/media/music \
    --dest /tank/media/music_organized --archive-dest /tank/archive/music-junk
```

Scripts that talk to Immich API (`build_albums.py`, `archive_cleanup.py`) run
**on igorbot** against `localhost:2283` — not from the Mac.

## Where to look next

| Need | Doc |
| ---- | --- |
| Agent entry map, boundaries | [AGENTS.md](AGENTS.md) |
| Recovery workflow | [docs/data-recovery.md](docs/data-recovery.md) |
| NAS architecture & decisions | [docs/architecture.md](docs/architecture.md) |
| Build steps | [docs/setup-guide.md](docs/setup-guide.md) |
| Gotchas | [docs/troubleshooting.md](docs/troubleshooting.md) |
