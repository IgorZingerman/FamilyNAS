# Architecture & decisions

This document explains *why* the stack looks the way it does. Several choices
here exist because a simpler/more obvious approach was tried first and hit a
real problem — those are called out explicitly, since the reasoning matters more
than the specific commands if your hardware or requirements differ.

## Hardware

Reference build: an Intel NUC7i5BNH (Kaby Lake, dual-core i5-7260U, 16GB RAM,
one internal 2.5" SATA + one M.2 bay, both already used by the boot SSD) plus a
TerraMaster D6-320 — a "dumb" USB-attached JBOD enclosure with **no forced
hardware RAID**, giving individual disk passthrough. That passthrough property
is a hard requirement: hardware-RAID enclosures hide individual disks from the
OS, which breaks ZFS's ability to detect and repair per-disk corruption.

This hardware has real constraints worth knowing before you commit to a similar
build:

- **No Thunderbolt, no USB-C** on this NUC generation — only USB 3.0 Gen1
  (5Gbps) Type-A. A USB-C-to-USB-A cable is required for the enclosure; don't
  assume a spare USB-C cable will work.
- **16GB RAM is enough** for general file-serving ZFS use *without dedup* — the
  "1GB RAM per 1TB" rule you'll see quoted everywhere is specifically about
  dedup, which most home NAS setups don't need. Don't over-invest in RAM
  upgrades for a single-purpose file/media server.
- **Only one drive bay internally** — any redundant array requires external
  USB-attached drives. This pushes SMART reliability monitoring to matter more
  (USB bridges can interfere with SMART passthrough — verify with
  `smartctl -d sat` before trusting any drive).
- Check your NIC's actual negotiated speed (`ethtool <iface>`) before blaming
  slow transfers on anything else — a bad cable silently negotiating 100Mbps
  instead of Gigabit looks identical to a real bottleneck until you check.
- **This CPU generation runs hot under sustained load** (photo-library ML
  processing, not just idle file-serving) — package temperature reached the
  low-to-mid 90s°C under a large bulk import, uncomfortably close to this
  chip's throttle point. A passive heatsink plus a small fan (correctly
  oriented — see [`docs/troubleshooting.md`](troubleshooting.md)) brought it
  down to a comfortable margin. Don't assume "small NAS box" means "never
  needs active cooling" once you add real compute workloads like Immich's ML
  pipeline on top of file-serving.
- **Check drive recording technology (SMR vs. CMR) before choosing a redundant
  array layout** — see [Storage layout](#storage-layout) below; this
  materially changed our pool design partway through the build.

## Storage layout

### Why ZFS on bare Ubuntu, not a NAS appliance OS

TrueNAS SCALE (or similar appliance OSes) is the more common recommendation for
a dedicated NAS, and it's a reasonable choice if you're starting from a blank
machine. We didn't use it here because the host was already running Ubuntu with
other unrelated workloads (Docker projects, etc.) — reinstalling to an appliance
OS would have meant giving those up. **If you're starting fresh and this machine
has no other purpose, TrueNAS SCALE is worth strongly considering** — it gets
you snapshot/SMART/dataset management UI for free instead of hand-rolling it.
Ubuntu + native `zfsutils-linux` is the right choice specifically when the box
already has other jobs.

### Why mirrors, not raidz2 — check your drive recording technology first

The original plan here **was** raidz2 across the size-matched 8TB drives (see
git history of this doc for that reasoning — it's not wrong in general). It
changed after checking each drive's model family with `smartctl -d sat -i
/dev/sdX` and finding the 8TB drives were **SMR (shingled magnetic
recording)**, not conventional (CMR) recording.

**Why that matters specifically for raidz:** SMR drives write in overlapping
shingled tracks and funnel incoming writes through a small conventional-recording
cache before "washing" them onto the shingled zones in the background. A raidz
resilver (rebuilding after a failed/replaced drive) is a large, sustained,
scattered-write workload — exactly the pattern that overwhelms that cache.
Real-world reports of this exact combination describe a resilver that should
take hours stretching to multiple *days*, or timing out before completing —
undermining the one moment raidz2 exists to handle safely. Given this project
started because a NAS array had already failed once, that risk wasn't worth
taking.

**Check before you commit to a layout:**

```bash
smartctl -d sat -i /dev/sdX | grep -i "model family\|device model"
```

Model families containing "SMR" in their description (or a quick web search of
the exact model number) tell you which recording technology you have. **If your
drives are conventional (CMR)**, raidz2 remains a perfectly sound,
more storage-efficient choice — none of this applies to you.

**What we built instead**, given 4×8TB + 3×4TB owned (one 8TB and one 4TB held
back, not part of this pool):

- **A 2-way mirror across two of the 8TB drives**, with the third 8TB held
  as a **hot spare** — ZFS swaps it in automatically the moment a mirror
  member fails, without any manual intervention.
- **A separate 2-way mirror across the 4TB drives**, striped into the same pool.
- The remaining 8TB and 4TB drives deliberately excluded from the pool
  entirely, reserved as a future physically separate `zfs send`/`receive`
  backup target — actual 3-2-1 backup protection, not just redundancy within
  one pool (a pool, however redundant, is still one failure domain for things
  like fire, theft, or a bad `zpool` operation).

At this drive count, **mirrors and raidz2 give identical usable capacity** —
both lose exactly one drive's worth of space per matched pair — so this cost
nothing in storage efficiency, only gained a resilver pattern (a simple
sequential copy) that's far gentler on SMR drives than raidz's scattered
rebuild I/O, plus a hot spare that heals a lost mirror member automatically.

**Sequencing tip:** if you don't have every intended drive available on day
one, build the pool now with the mirrors you can, and `zpool add` another
mirror vdev later once more drives free up — ZFS stripes it into the existing
pool automatically. (The equivalent move for a raidz2-based layout is
[OpenZFS raidz expansion](https://openzfs.org/wiki/Raidz_expansion)
(`zpool attach`, supported since OpenZFS 2.3) instead — same idea, different
mechanism, since raidz vdevs widen in place rather than getting striped
alongside a new one.)

### Why the database and transcode cache live on the boot SSD, not the pool

If your redundant pool is USB-attached spinning HDDs (as ours is), don't put
Postgres's data directory there. Databases do lots of small random writes;
spinning disks behind a USB bridge handle that badly, and it will make the
photo app feel sluggish. Likewise, a video transcode cache is pure ephemeral
scratch data — there's no reason to burn HDD I/O or wear on the redundant array
for it.

Both went on the boot SSD instead. The consequence: the database needs its own
backup plan (a `pg_dump` cron), since it's no longer covered by whatever ZFS
snapshot policy you set up on the pool.

## Application layer

### Why Immich + Jellyfin specifically

Immich is the most complete open-source Apple Photos/iCloud replacement
currently available: native multi-user accounts, official iOS/Android apps with
automatic background camera-roll backup, face/object recognition, Live Photo
support. Jellyfin is a mature, zero-paywall media server with broad client
support (Infuse/Swiftfin for video, Finamp/Amperfy for music, both with native
Apple ecosystem integration including CarPlay).

**Use Immich's official released `docker-compose.yml` and `.env`**, downloaded
directly from their GitHub releases, rather than hand-writing the compose file.
Immich's Postgres image bundles specific vector-extension versions
(`vectorchord`/`pgvectors`) that are tightly paired to each Immich release — a
generic `postgres:16` image will not work, and a mismatched custom image can
break on upgrade. Jellyfin isn't part of Immich's release, so it gets
hand-appended as an extra service in the same compose file (see
[`config/docker-compose.yml`](../config/docker-compose.yml)).

### Offloading Immich's ML workload to a second machine

If your NAS hardware is modest (see [Hardware](#hardware) above), a large bulk
photo import can genuinely overwhelm it — not just slowly, but in a way that
gets *worse* over time. What we hit: after importing several hundred thousand
photos, system load average climbed to roughly 5x the CPU's core count.
Under that much contention, the background job queue's stalled-worker
detection assumed jobs had died (workers couldn't check in fast enough) and
re-queued already-in-progress work — creating a loop where the backlog grew
even though real processing was happening. **Lowering job concurrency**
(Admin → System Settings → Job Settings in Immich) so each job actually
finishes before hitting that stall timeout fixed the loop — fewer jobs running
in parallel, but the total queue actually drained instead of regenerating.

The bigger fix, if you have any other capable machine on the same LAN sitting
idle: **Immich's ML workload is a genuinely separable microservice**, not a
hack to split off. Face detection, search embeddings (CLIP), and OCR all run
through `immich-machine-learning`, which the server talks to over HTTP — it
doesn't have to run on the same box as everything else.

**What can move, and what can't:**
- **Can offload:** face detection/recognition, smart search embeddings, OCR —
  anything that routes through `immich-machine-learning`.
- **Can't offload this way:** thumbnail generation and video transcoding —
  these run inside the main Immich server's own job workers, not the ML
  service, and Immich doesn't support clustering multiple server instances
  against one database. They stay on whichever machine hosts Immich itself.

**How:** run `immich-machine-learning` (**matching your server's exact
version tag** — the server/ML communication protocol isn't guaranteed
compatible across versions) on the other machine, with a persistent volume for
its model cache and port `3003` published. Then in Immich's admin System
Settings → Machine Learning, point `Machine Learning URLs` at that machine's
`http://<lan-ip>:3003` instead of the local instance, and stop the
now-redundant local ML container to actually free its resources.

**If that second machine runs Docker Desktop or an alternative like Rancher
Desktop:** these tools run containers inside a lightweight VM with a **fixed
resource allocation separate from the host's total RAM/CPU** — by default,
usually far too small to hold multiple ML models loaded simultaneously.
See [`docs/troubleshooting.md`](troubleshooting.md) for what that looked like
and the fix.

Once genuinely offloaded, there's often real headroom to push further: with
compute no longer local, you can also raise per-job-type concurrency for the
now-offloaded job types specifically (they're no longer contending with the
original machine's CPU), meaningfully increasing throughput beyond just
relocating the work.

### Why Samba, and why per-person accounts

Immich already solves "how do family members get photos onto the server" —
browser drag-and-drop, or the mobile app's automatic backup, both built in.
Jellyfin has **no equivalent** — it's a read-only consumption server that just
scans folders on disk. Something else has to get files into those folders.

A Samba (SMB) network share is the natural fit for Mac/Windows clients: Finder
and Explorer both have native SMB support, no extra software needed. Design
choices worth calling out:

- **Per-person accounts, not one shared login.** Files written by each account
  land owned by that account's UID, so `ls -la` shows who added what, and access
  can be revoked individually later without affecting everyone else.
- **A shared Unix group** (e.g. `familymedia`) owns the actual share
  directories with the setgid bit set, so every account can read/write the
  shared library — it's one common library, not per-person silos, even though
  each account is distinct.
- **`vfs objects = fruit streams_xattr`** on every share. This is the standard
  fix for macOS↔Samba interop: proper icons/thumbnails in Finder, and critically,
  it prevents macOS from littering the share with `._AppleDouble` sidecar files
  that would otherwise confuse Jellyfin's library scanner into treating junk
  files as media.
- **A dedicated Time Machine share** using Samba's `fruit:time machine = yes`,
  shared by all family Macs rather than one share per person — each Mac
  automatically gets its own private sparse-bundle disk image inside the shared
  folder. Set `fruit:time machine max size` to a real cap before anyone enables
  it as a backup destination — without one, each Mac assumes the *entire* pool's
  free space is available to it, and one runaway backup can starve the space
  meant for photos and media.
- There is **no built-in self-service SMB password change** exposed by Finder.
  Realistic options are: everyone shares one initial password (acceptable for a
  trusted home LAN), or anyone who wants a unique one runs `smbpasswd -U <name>
  -r <server>` from a machine with Samba's client tools installed.

### Why a drop-folder instead of relying on the web UI

Immich's web UI (drag-and-drop) is fine for a handful of photos, tedious for
"I have a folder of vacation photos to get off my laptop." The fix is a
watched drop-folder: family members copy files into a Samba share, and a
background process picks them up automatically. A few decisions worth
explaining:

**Folder structure decides classification — no content sniffing needed.**
The dropbox share has two top-level folders family members drop directly into:

```
dropbox/
  photos/            <- photos & personal videos, imported into Immich
    favorites/         <- same as above, plus auto-tagged as an Immich favorite
                          (feeds Immich's built-in Favorites view — handy for
                          things like pulling photos for a yearly calendar)
  media/
    movies/            <- routed into Jellyfin's movies library
    tv/                <- routed into Jellyfin's TV Shows library
    music/             <- routed into Jellyfin's music library
  archive/photos/<date>/...   <- copies of successfully-imported photos (safety net)
  failed/{photos,media}/...   <- anything that failed processing, never silently dropped
```

The obvious alternative — auto-detecting file type and guessing intent — has a
real ambiguity problem: an `.mp4` could be a home video (belongs in Immich), a
ripped movie, or a TV episode, and there's no reliable way to tell a movie
apart from a TV episode from the file alone (both are just video files with
similar extensions). Splitting `photos/` (personal content, handled by Immich)
from `media/` (library content, handled by Jellyfin) resolves the
photo-vs-media ambiguity; **within** `media/`, movie-vs-TV is resolved by which
subfolder the file was dropped into rather than guessed — a file dropped loose
directly in `media/` with no subfolder falls back to an audio/video extension
guess, which can only get you as far as "definitely music," defaulting
anything video-shaped to movies.

**Attribution is nearly free.** Because the Samba accounts writing into this
share don't use `force user` (see above), every dropped file already carries
the uploader's UID as owner and its mtime as drop time. The watcher just reads
`stat()` on each file — no separate tracking mechanism needed.

**A watcher, not a poller.** `inotifywait -m -r -e close_write,moved_to`,
running as a `Restart=always` systemd service, reacts within moments of a
file finishing copying — `close_write` specifically fires only once the
writer (Samba, on the uploading client's behalf) closes the file, so a file
is never grabbed mid-copy. Events are processed one at a time deliberately,
not in parallel: on modest hardware already busy running Immich's ML
pipeline, queuing uploads sequentially is both simpler and considerably
kinder to the box than firing a burst of concurrent API calls when someone
drops fifty photos at once.

**Photos are copied into Immich, then the original is archived, not
deleted.** Immich has its own internal asset storage — uploading is a copy,
not a move — so the dropbox original becomes a redundant safety net rather
than the canonical copy. It's moved into a dated `archive/` folder rather than
deleted, in keeping with a broader "never delete automatically, always
quarantine" philosophy: automated pipelines that silently delete originals are
one bug away from data loss, and a dated archive folder costs little to keep.

**Movies/TV/music are moved, not copied.** Unlike photos, the dropbox file *is*
becoming its final canonical copy at `/tank/media/movies`, `/tv`, or `/music`
— there's nothing else to preserve, so a plain move is correct.

**Failures never disappear silently.** Any error — upload failed, unrecognized
file extension, missing credentials for that uploader — moves the file to
`failed/<category>/` and logs the reason. A file that's left stuck in place
looks like "nothing happened" to whoever dropped it; a file that's silently
deleted on error is far worse. Every event (success or failure) is logged with
timestamp, uploader, category, filename, and outcome — for movies/TV/music
this log is the *only* record of who uploaded what, since Jellyfin has no
per-file uploader concept of its own.

**Per-person Immich attribution requires per-person API keys.** For a dropped
photo to show up owned by the actual person in Immich (not one shared
uploader account), the watcher needs to act on that person's behalf — which
means a real Immich account and a personal API key per family member, stored
server-side (see [`docs/setup-guide.md`](setup-guide.md) for where and how).
**The API key needs `asset.update` permission, not just `asset.upload` and
`asset.read`**, if you want the favorites-tagging feature — see
[`docs/troubleshooting.md`](troubleshooting.md) for what happens if you miss
this (it fails silently in a non-obvious way).

### Why hostnames instead of ports, and why not path-based routing

Nobody remembers `http://server:2283`. The fix is a reverse proxy (here, Caddy)
in front of everything, serving each app under a plain name on port 80.

**Path-based routing (`/photos`, `/media`) was considered and rejected for
Immich specifically** — [Immich's own documentation states it cannot be served
under a subpath](https://docs.immich.app/administration/reverse-proxy/); its web
app's JS/CSS/API/websocket references all assume they're mounted at the root of
a (sub)domain. This isn't a proxy misconfiguration, it's an upstream limitation.
Rather than mixing schemes (hostnames for Immich, paths for everything else),
**every app got its own hostname** — one consistent pattern that's guaranteed to
keep working as you add more apps later, several of which may have the same
"can't do subpaths" limitation as Immich.

If none of your apps have this limitation, path-based routing under one
hostname is a perfectly reasonable alternative and saves you from needing mDNS
aliases at all.

See [`docs/troubleshooting.md`](troubleshooting.md) for the two non-obvious
Avahi/mDNS bugs this setup hit, and a JavaScript environment-variable trap that
affected one of the proxied apps.

## Family-facing accounts summary

Three independent account systems exist, deliberately kept separate rather than
unified behind a single sign-on (that's a reasonable future improvement, not
done here to keep scope small):

| System | Login style | Scope |
|---|---|---|
| Samba shares | Linux system accounts (no shell) | File access to `movies`/`tv`/`music`/`timemachine`/`dropbox` shares |
| Jellyfin | Its own username/password | Media library access |
| Immich | Email/password | Photo library access |

A fourth, server-side-only credential layer backs the dropbox watcher: one
Immich **API key** per family member (distinct from their login password —
used by the automation to upload on their behalf) plus one Jellyfin API key
for triggering library scans. These aren't accounts a person logs into
directly; they live in root-only files on the server (see
[`docs/setup-guide.md`](setup-guide.md)) and should never be committed
anywhere.

Using the **same password across all three** for each person is a deliberate
usability tradeoff for a home/family context — not a general security
recommendation, but reasonable when the alternative is family members unable to
remember which of three unrelated systems wants which password.

### Immich has no real concept of a jointly-owned library

Worth deciding **before** you pick per-person vs. shared Immich accounts, not
after a bulk import: every Immich asset has exactly one owner, permanently.
There's no ownership-transfer feature and no way to grant another account
write access (move, delete, edit, organize into albums) over assets it doesn't
own.

The built-in **Partner Sharing** feature (Account Settings → Partner Sharing)
looks like it solves this, but it's read-only by design — a partner can browse
and download everything in your library, but can't touch it. This is
intentional on Immich's part, not a bug: it protects each account's library
even from other trusted accounts on the same server, including admins (an
admin account gets server/user management capabilities, not a backdoor into
individually curating someone else's personal timeline).

This bit us directly: a large recovered family photo library ended up
uploaded entirely under one person's account, and the other spouse — despite
being an equal owner of that content in every real sense — had no way to
delete duplicates, fix albums, or reorganize any of it under their own login.
Partner Sharing let them *see* everything, but not touch it.

**If a library is genuinely jointly owned/managed** (the common case for a
couple or family's shared photo collection, as opposed to one person's private
photos), the two real options are:

- **Share the login credentials for whichever account owns that content.**
  Not elegant, but it's the only way to get real write access, and for a
  two-person household it's a perfectly reasonable choice — you're not
  protecting the content from each other.
- **Consolidate onto one shared account for jointly-owned content up front**,
  before a bulk import, and reserve individual accounts for content that's
  genuinely personal (e.g. each person's own phone camera roll via the mobile
  app's auto-backup). Retrofitting this after a large import means either
  re-uploading everything under the new account (no ownership-transfer API to
  do it in place) or living with the credential-sharing workaround above.

Decide this **before** running a large import, not after — it's much easier to
land content under the right account from the start than to move it later.
