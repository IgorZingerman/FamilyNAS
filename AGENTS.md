# FamilyNAS (UnDeadDrobo) — Agent guide

Self-hosted family NAS documentation and tooling: Immich + Jellyfin + Samba
dropbox on Ubuntu/ZFS, plus Python scripts for recovering data from a failed
Drobo and post-import cleanup. This repo is **docs + config templates +
scripts** — not a deployed application tree.

## Start here

| Need | Document |
| ---- | -------- |
| What the stack is and why | [docs/architecture.md](docs/architecture.md) |
| Step-by-step NAS build | [docs/setup-guide.md](docs/setup-guide.md) |
| Recovery pipeline (`organize_recovery.py`) | [docs/data-recovery.md](docs/data-recovery.md) |
| Gotchas (Avahi, Immich subpaths, curl exit codes, GPU…) | [docs/troubleshooting.md](docs/troubleshooting.md) |
| Human-facing overview + stack diagram | [README.md](README.md) |
| Deployable templates (compose, Caddy, Samba, systemd) | [config/](config/) |
| Env template (keys only) | [config/.env.example](config/.env.example) |

**Cursor rules:** `.cursor/rules/*.mdc` (always-on project basics + Python script conventions).

## Repository layout

```
docs/                  # Architecture, setup, recovery, troubleshooting
config/                # Canonical deploy templates — copy/adapt to the NAS
  dropbox-watcher.py   # Production watcher (systemd); edit paths/keys here
scripts/               # Local/recovery/maintenance utilities
  organize_recovery.py # Recovery CLI — stdlib only, dry-run by default
  reorganize_music.py  # Music library flatten — requires mutagen
  queue_watch.py       # Immich job-queue tracker (SSH to NAS)
  archive_cleanup.py   # Move misfiled Immich assets to archive (runs on NAS)
  build_albums.py      # Build Immich albums from transfer log (runs on NAS)
scan_results/          # gitignored — personal scan manifests
logs/                  # gitignored — diagnostic dumps
```

Reference hardware host: **`igorbot`** on the LAN (`igorbot.local`). Hostnames
in docs (`nas.local`, `photos.local`, …) are **examples** — pick your own and
stay consistent across Caddy, Samba, and Avahi.

## Patterns agents must follow

1. **Dry-run by default** — Python utilities print what they *would* do; filesystem
   or API writes require explicit `--apply`. Never remove that guard.
2. **Never hard-delete** — Quarantine, archive, skip, or Immich soft-trash only.
   Recovery and cleanup scripts must not silently destroy the only copy of data.
3. **Edit `config/` for deployment templates** — `config/dropbox-watcher.py` is
   the canonical watcher; keep it in sync with `config/familynas-dropbox-watcher.service`.
4. **Secrets stay out of git** — No `.env`, Immich API keys, per-person credential
   files, `scan_results/`, `logs/`, or CSV manifests. Use `config/.env.example`
   for key names only.
5. **Minimal diffs** — Match existing Python style (stdlib where possible, argparse
   CLI, module docstring with usage). Prefer extending existing scripts over
   new one-offs for the same workflow.
6. **Docs are the source of truth** — When behavior changes, update the relevant
   doc (`docs/*.md` or script docstring) in the same change. Link new workflows
   from this file if they're agent-relevant.

## Local / remote commands

| Command | Purpose |
| ------- | ------- |
| `python3 -m py_compile scripts/*.py` | Syntax-check Python changes |
| `python3 scripts/organize_recovery.py scan <src> --out manifest.csv` | Catalog one recovery drive (dry run) |
| `python3 scripts/queue_watch.py` | Snapshot Immich queue depths via SSH |
| `ssh igorbot …` | Live NAS operations (deployed stack lives there) |

Scripts marked "run on igorbot" (`build_albums.py`, `archive_cleanup.py`) expect
`localhost:2283` Immich API access and paths under `/tank/…` on the NAS host.

## Agent boundaries

**OK without asking:** read code and docs, scoped fixes on a branch, update docs
for a change, dry-run recovery commands against local/external volumes, syntax-check
Python.

**Ask first:** run `--apply` against live NAS paths (`/tank/…`), deploy config
changes to igorbot, Immich API mutations (album create, asset trash), SSH changes
on the production box, or anything that moves/deletes user data.

**Never:** commit secrets or personal scan data; hard-delete files in recovery
scripts; push directly to `main` without a PR if using branch workflow; invent
parallel recovery pipelines when `organize_recovery.py` already covers the flow.

## Verification by change type

| Area | Check |
| ---- | ----- |
| Python scripts | `python3 -m py_compile` on touched files |
| Recovery logic | Dry-run first; confirm `--apply` still gated |
| Config templates | Cross-check paths against `docs/setup-guide.md` and `config/docker-compose.yml` |
| Docs | Link from README or this file when adding a new major workflow |
