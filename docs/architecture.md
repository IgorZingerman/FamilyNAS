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

### Why raidz2 + mirror instead of one big vdev

With mismatched drive sizes (in our case 4×8TB + 3×4TB) and drives of uncertain
health history, a single vdev spanning all drives would either truncate every
drive to the smallest capacity (wasteful) or use raidz1 (only survives one
failure — risky with drives whose reliability isn't yet proven). The layout used
here:

- **raidz2 across the size-matched 8TB drives** — survives 2 simultaneous
  failures, no capacity wasted since all members match.
- **A separate mirror across the 4TB drives**, striped into the same pool.
- **One drive deliberately excluded** from the pool entirely, reserved as a
  physically separate `zfs send`/`receive` backup target — actual 3-2-1 backup
  protection, not just redundancy within one pool (a pool, however redundant,
  is still one failure domain for things like fire, theft, or a bad `zpool`
  operation).

If you don't have mismatched drives, a single raidz2 vdev across everything is
simpler and equally sound.

**Sequencing tip:** if you don't have every intended drive available on day one,
build the pool now with what you have as a narrower raidz2, and use [OpenZFS
raidz expansion](https://openzfs.org/wiki/Raidz_expansion) (`zpool attach`,
supported since OpenZFS 2.3) to widen it later without ever dropping below your
target redundancy level. The parity level (raidz2) is fixed at vdev creation and
survives expansion — only the width changes. Data written before expansion keeps
a less space-efficient ratio permanently, but that's a one-time, non-dangerous
cost, not a design flaw.

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
| Samba shares | Linux system accounts (no shell) | File access to `movies`/`music`/`timemachine` shares |
| Jellyfin | Its own username/password | Media library access |
| Immich | Email/password | Photo library access |

Using the **same password across all three** for each person is a deliberate
usability tradeoff for a home/family context — not a general security
recommendation, but reasonable when the alternative is family members unable to
remember which of three unrelated systems wants which password.
