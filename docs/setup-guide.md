# Setup guide

Read [`architecture.md`](architecture.md) first if you haven't — it explains the
*why* behind each step here. Commands below assume Ubuntu Server (24.04/26.04)
on the NAS host, and a Mac as the primary client, but the app-layer steps are
distro-agnostic.

Throughout: replace `nas` with whatever hostname you actually pick, and replace
every password placeholder with your own — **do not reuse the example values
verbatim.**

## 1. Base OS + ZFS

```bash
sudo apt-get update
sudo apt-get install -y zfsutils-linux
zfs --version   # confirm it matches your kernel's already-loaded module version
```

Before trusting any drive going into the pool, run a SMART long self-test on
each one, and if the drives are behind a USB enclosure, verify SMART
passthrough works at all:

```bash
sudo smartctl -a /dev/sdX              # does it report real data, or garbage?
sudo smartctl -d sat -a /dev/sdX       # try forcing SAT passthrough if the above fails
sudo smartctl -t long /dev/sdX         # long self-test; takes hours, check smartctl -a later
```

Create the pool matching your own drive layout (see
[`architecture.md`](architecture.md#storage-layout) for the reasoning):

```bash
sudo zpool create tank raidz2 /dev/disk/by-id/<disk1> /dev/disk/by-id/<disk2> /dev/disk/by-id/<disk3> /dev/disk/by-id/<disk4>
sudo zpool add tank mirror /dev/disk/by-id/<disk5> /dev/disk/by-id/<disk6>
```

Always reference drives by `/dev/disk/by-id/...`, never `/dev/sdX` — device
letters aren't stable across reboots, especially with USB enclosures.

Create the datasets the rest of this guide expects:

```bash
sudo zfs create -o recordsize=1M tank/apps/immich/data
sudo zfs create tank/apps/jellyfin/config
sudo zfs create tank/media/movies
sudo zfs create tank/media/music
sudo zfs create tank/backups/timemachine
```

## 2. Boot-disk prep (Postgres + transcode cache)

If your boot disk has spare LVM capacity, extend it now — Postgres and the
Jellyfin transcode cache both want to live here, not on the pool (see
[`architecture.md`](architecture.md#why-the-database-and-transcode-cache-live-on-the-boot-ssd-not-the-pool)):

```bash
df -T /                                   # confirm filesystem type first
sudo lvextend -l +100%FREE /dev/<vg>/<lv>
sudo resize2fs /dev/mapper/<vg>-<lv>      # ext4; use xfs_growfs for XFS

sudo mkdir -p /var/lib/immich/pgdata /var/lib/jellyfin/config /var/lib/jellyfin/cache
sudo chown -R $USER:$USER /var/lib/immich /var/lib/jellyfin
```

## 3. Docker + the Immich/Jellyfin stack

```bash
sudo apt-get install -y docker.io docker-compose-plugin
mkdir ~/nas-stack && cd ~/nas-stack
curl -fsSL -o docker-compose.yml https://github.com/immich-app/immich/releases/latest/download/docker-compose.yml
curl -fsSL -o .env https://github.com/immich-app/immich/releases/latest/download/example.env
```

Edit `.env`:
```
UPLOAD_LOCATION=/tank/apps/immich/data
DB_DATA_LOCATION=/var/lib/immich/pgdata
TZ=<your timezone, e.g. America/Los_Angeles>
IMMICH_VERSION=release
DB_PASSWORD=<generate a random one, e.g. `openssl rand -base64 24`>
```

Append the Jellyfin service block from
[`config/docker-compose.yml`](../config/docker-compose.yml) to the downloaded
compose file (it's the same file, kept here as a reference/diff target since
Immich's upstream file changes over time). Key details:
- `devices: ["/dev/dri:/dev/dri"]` + `group_add: ["<render-gid>"]` for Intel
  QuickSync hardware transcoding — find your `render` group's GID with
  `getent group render`.
- Volumes point at the ZFS dataset paths from step 1.

```bash
docker compose config   # validates before you commit to `up`
docker compose up -d
docker compose ps       # wait for all services "healthy"
```

Create the Immich admin account (first user to sign up becomes admin) by
visiting `http://<nas-ip>:2283` and following the prompt, or via the API:

```bash
curl -X POST http://<nas-ip>:2283/api/auth/admin-sign-up \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@example.com","password":"<choose one>","name":"Admin"}'
```

Complete Jellyfin's setup wizard at `http://<nas-ip>:8096` (or via its
`/Startup/*` API endpoints — see Jellyfin's API docs), then add `Music` and
`Movies` libraries pointing at `/data/music` and `/data/movies` respectively
(the container paths, mapped to the ZFS datasets via the compose file).

## 4. Samba (family file access)

```bash
sudo apt-get install -y samba samba-common-bin
sudo groupadd familymedia
```

For each family member:
```bash
sudo useradd -M -s /usr/sbin/nologin -G familymedia <name>
sudo smbpasswd -a <name>     # prompts for a password interactively
```

Ensure the ZFS datasets have the right group/permissions before first use:
```bash
sudo chgrp familymedia /tank/media/movies /tank/media/music /tank/backups/timemachine
sudo chmod 2775 /tank/media/movies /tank/media/music /tank/backups/timemachine
```

Append the share definitions from
[`config/smb.conf.snippet`](../config/smb.conf.snippet) to
`/etc/samba/smb.conf`, then:

```bash
sudo testparm -s              # validate syntax before relying on it
sudo systemctl enable --now smbd
```

`nmbd` will likely refuse to start if `disable netbios = yes` is set in your
distro's default `smb.conf` — that's expected on modern Ubuntu; discovery works
via mDNS instead (see step 6), which is what modern macOS/Windows prefer anyway.

## 5. Reverse proxy (friendly URLs)

```bash
sudo apt-get install -y caddy
```

Use [`config/Caddyfile.example`](../config/Caddyfile.example) as a starting
point — copy to `/etc/caddy/Caddyfile`, adjust hostnames and backend ports to
match your setup, then:

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl enable --now caddy
```

**Free up port 80 first** if anything else is already using it — Caddy needs
it, and only one process can bind it.

Build a homepage linking to your apps using
[`config/nas-home/index.html`](../config/nas-home/index.html) as a template;
Caddy's default site block in the example Caddyfile serves it.

## 6. mDNS aliases (so hostnames actually resolve)

**Do not use `/etc/avahi/hosts`** for aliasing your own machine's address to new
names — see [`troubleshooting.md`](troubleshooting.md#avahi-self-aliasing-doesnt-work-via-etcavahihosts)
for why it silently fails. Instead, install the two systemd templates from
[`config/`](../config/) and instantiate one pair (IPv4 + IPv6) per hostname:

```bash
sudo cp config/avahi-alias@.service config/avahi-alias6@.service /etc/systemd/system/
sudo systemctl daemon-reload

# Repeat for each hostname you configured in the Caddyfile:
sudo systemctl enable --now avahi-alias@photos.local
sudo systemctl enable --now avahi-alias6@photos.local
```

Verify before trusting it:
```bash
avahi-resolve -n photos.local
curl -w '%{http_code} (%{time_total}s)\n' -o /dev/null -s http://photos.local/
```

If that curl takes a suspiciously exact ~5 seconds instead of being instant, see
[`troubleshooting.md`](troubleshooting.md#5-second-stall-on-every-proxied-request) —
you likely published only one address family and are hitting a Happy Eyeballs
stall, not a real performance problem.

## 7. Client setup

- **Photos**: install the official Immich app (iOS/Android) and point it at
  `http://photos.local` (or your own hostname) for automatic camera-roll backup.
  For bulk-importing an existing photo library, use the official CLI
  (`npm install -g @immich/cli`, then `immich login-key <url>/api <api-key>` and
  `immich upload --recursive <folder>`) rather than dragging thousands of files
  through the browser.
- **Movies**: [Infuse](https://firecore.com/infuse) (paid, uses native Apple
  playback) or [Swiftfin](https://github.com/jellyfin/Swiftfin) (free,
  open-source) pointed at `http://media.local`.
- **Music**: [Finamp](https://github.com/jmshrv/finamp) or Amperfy — both
  support offline caching and native CarPlay integration.
- **File uploads**: Finder → `⌘K` → `smb://nas.local` (or click the server
  under Finder's Network sidebar once mDNS propagates), authenticate with a
  Samba account from step 4.
- **Time Machine**: add the `timemachine` share as a backup destination in
  System Settings — **only after** you've set a real `fruit:time machine max
  size` cap (see [`architecture.md`](architecture.md#why-samba-and-why-per-person-accounts)),
  since enabling it starts an automatic background backup immediately.

## Ongoing maintenance (not automated by this guide)

- ZFS snapshot policy for the app/media datasets (e.g. `sanoid`, or a simple
  cron `zfs snapshot`).
- `pg_dump` cron for Immich's Postgres data, since it lives off-pool and isn't
  covered by ZFS snapshots.
- SMART monitoring / periodic long self-tests on all pool drives.
