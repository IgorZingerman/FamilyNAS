# Troubleshooting & gotchas

Every one of these was hit during the actual build, not theoretical. Documented
here so you don't have to re-derive them.

## Avahi self-aliasing doesn't work via `/etc/avahi/hosts`

**Symptom:** you want your NAS to answer to multiple mDNS names (e.g.
`photos.local` and `media.local` both pointing at the same box). The
seemingly-obvious approach — adding static entries to `/etc/avahi/hosts` in the
same format as `/etc/hosts` — fails on restart with:

```
avahi-daemon: Static host name photos.local: avahi_server_add_address failure: Local name collision
```

**Root cause:** `/etc/avahi/hosts` is meant for publishing addresses of *other*
devices on the network that can't announce themselves (e.g. a printer without
mDNS support) — not for giving your own machine additional names. Avahi's
collision detection treats "this address is already claimed under a different
name" (which is exactly what's happening — the address is already claimed under
your machine's real hostname) as a naming conflict, and refuses.

**Fix:** run a dedicated `avahi-publish -a -R <name> <address>` process per
alias as a long-running service — this registers via a different code path that
doesn't hit the same self-collision check. See
[`config/avahi-alias@.service`](../config/avahi-alias@.service) for a systemd
template; instantiate per hostname (`avahi-alias@photos.local`).

## 5-second stall on every proxied request

**Symptom:** `curl http://photos.local/` (or any browser request) takes a
suspiciously *exact* ~5.0 seconds, every single time, while the identical
request against `127.0.0.1` or the machine's primary hostname is instant
(~10-40ms).

**Diagnosis approach:** an exact, round-number delay (5.0s, 10.0s) that repeats
identically on every request is the signature of a resolver or dial *timeout*
being hit, not real work being slow — real app/network slowness has variance,
timeouts don't. Rule out the backend first (test it directly, bypassing the
proxy, over loopback) before suspecting the app.

**Root cause:** the mDNS alias (via `avahi-publish`, see above) only published
an IPv4 (A) record. A client doing standard dual-stack resolution ("Happy
Eyeballs") tries both address families and waits for a response before falling
back — but mDNS has no concept of a negative response, so a missing AAAA record
just... never answers, and the client sits out its own internal timeout (often
~5s) before giving up on IPv6 and using the IPv4 address it already has.
Hostnames that *did* have both A and AAAA published (e.g. the machine's real
hostname, managed by `avahi-daemon` itself) never hit this, which is why the
same test against them was instant — that inconsistency is the actual clue.

**Fix:** publish a matching AAAA record for every alias too — a second
`avahi-publish -a -R <name> <ipv6-address>` instance per name. See
[`config/avahi-alias6@.service`](../config/avahi-alias6@.service).

## Docker silently creates an empty directory for a nonexistent bind-mount path

**Symptom:** you point a Docker Compose volume at a path that doesn't exist yet
(e.g. a ZFS dataset you haven't created), expecting an error. Instead, the
container starts successfully — with an empty directory silently created at
that path on the host, on whatever filesystem the parent directory happens to
live on (which may very much not be where you intended data to end up).

**Why this matters:** if you're staging a stack before your real storage exists
(e.g. testing against a temporary directory before your final mount point is
ready), it is very easy to end up with stray directories at the *real* intended
path, sitting on the wrong filesystem — which can then conflict with actually
mounting your real storage there later (a mount point generally needs to be
empty, and now it technically already contains an accidentally-created empty
directory tree, which is usually harmless but worth cleaning up deliberately
rather than being surprised by it).

**Fix:** there's no config flag for this — it's just Docker's default bind-mount
behavior. Practical mitigation: if you're staging multiple config surfaces that
each independently reference the same "real" path (in our case: Samba's
`smb.conf`, Immich's `.env`, and Jellyfin's `docker-compose.yml` volumes all
separately hardcoded `/tank/...` paths), **grep all of them** whenever you
temporarily repoint one during a staging period — they don't share a single
source of truth, and it's easy to fix one and forget the others.

## Immich cannot be served under a URL subpath

**Symptom:** you want `nas.local/photos` instead of a dedicated `photos.local`
hostname, to avoid setting up mDNS aliases. Reverse-proxying Immich under a path
prefix loads the UI shell, but JS/CSS assets 404, API calls fail, or the
websocket connection breaks.

**Root cause:** this isn't a proxy misconfiguration — [Immich's own
documentation states this is unsupported](https://docs.immich.app/administration/reverse-proxy/):
the web app's asset references, API calls, and websocket connections all assume
they're mounted at the root of a (sub)domain, with no built-in base-path
configuration.

**Fix:** give Immich its own hostname. If you're also running other apps that
*do* support subpaths, consider standardizing all of them on hostnames anyway
for consistency (see [`architecture.md`](architecture.md#why-hostnames-instead-of-ports-and-why-not-path-based-routing))
rather than mixing schemes — a future app hitting this same limitation is
likely, not a one-off.

## JavaScript falsy-string trap in env-var-configured base paths

**Symptom:** an app configurable via an environment variable like `BASE_PATH`
still redirects to its old default path even after you set the env var to an
empty string to disable the path prefix.

**Root cause:** a common JS pattern is `const BASE = process.env.BASE_PATH ||
'/default'`. In JavaScript, an empty string is falsy — so `BASE_PATH=""`
doesn't override the default at all, it silently falls through to it. This is a
generic gotcha, not specific to any one app; anything using this exact `|| ` idiom
in Node/JS will have it.

**Fix (without touching app code):** if the app also does something like
`.replace(/\/$/, '')` on the value afterward (stripping a trailing slash), you
can exploit that: set the variable to `"/"` instead of `""`. It's truthy (so it
passes the `||` check and doesn't fall back to the default), but reduces to an
empty string after the trailing-slash strip — landing you on the same
root-serving code path you actually wanted. Check your specific app's source
before relying on this trick; it works only if a similar normalization step
exists downstream.

## A bulk import can fill storage faster than you'd expect — check where it's actually landing first

**Symptom:** partway through a large bulk photo/video upload, Postgres crashes
with `FATAL: could not write lock file "postmaster.pid": No space left on
device`, and the whole Immich stack goes down (the server can't reach a dead
database) — not just the upload, everything.

**Root cause:** Immich's own asset storage location (`UPLOAD_LOCATION`) had
been left pointed at a small boot drive "to be repointed at the real storage
pool later" — but the bulk upload itself needed exactly the space that
repointing was waiting on, so it filled the boot disk completely before ever
getting the chance.

**Fix:** move the storage location to real capacity *before* starting a bulk
write, not after. If you're already mid-incident: `rsync -a
--remove-source-files <old-location>/ <new-location>/` moves each file across
and only deletes the source copy once that specific file's copy succeeds —
safe to run even at 0 bytes free, since it only ever reads from/deletes off
the full disk while writing to the one with room. Then update
`UPLOAD_LOCATION` in `.env` and recreate just the `immich-server` and
`database` containers.

**General lesson:** before starting any bulk write operation, confirm where
the destination *actually* lives and whether it has room for the full
operation — "repoint later" is a trap when the current location can't hold
the full result in the meantime.

## Immich's upload CLI can choke on certain filenames, and version-mismatch silently

**Symptom 1:** `immich upload --recursive` crashes outright during its initial
file crawl with `ENOENT: no such file or directory`, pointing at a file whose
name looks completely normal in a plain directory listing.

**Root cause:** apostrophes and backslashes in filenames can trip up the
CLI's own path handling — the file exists exactly where you'd expect, the CLI
is just misparsing its own generated path internally.

**Fix:** rename affected files to strip the problem characters before
uploading (only changes the local copy waiting to be imported, not your
original source data).

**Symptom 2:** the CLI crashes mid-upload with `TypeError: this.store.values(...).toArray is not a function` — a JavaScript runtime error, not anything about your files.

**Root cause:** a specific CLI release used a newer JavaScript language
feature than the Node.js version it was actually running against supported.

**Fix:** pin the CLI to a slightly older release (`npm install -g
@immich/cli@<previous-version>`) rather than always grabbing latest — worth
doing generally when running a long, expensive, hard-to-restart operation
like a several-hundred-thousand-file upload.

## Reconciling "missing" asset counts in Immich after a bulk import

**Symptom:** you bulk-upload N files, but Immich's various stats endpoints
report fewer visible library items than N, and different endpoints
(`/api/assets/statistics`, `/api/search/metadata`, `/api/duplicates`) disagree
with each other.

**Not a bug, most likely explanation:** Immich automatically pairs iPhone Live
Photos — a HEIC image plus its companion MOV video, matched via Apple's
`ContentIdentifier` metadata embedded in both files — into a single visible
timeline entry. If your import set included a lot of iPhone photos with video
companions, this fully accounts for the gap: `files uploaded − live-photo pairs
= visible library count`. Verify by checking the `livePhotoVideoId` field on
returned assets from `/api/search/metadata`, and cross-check against the
filesystem directly (count files under Immich's `upload/` storage folder) as
ground truth — it's more reliable than trusting any single stats endpoint,
especially right after a large import while background jobs (thumbnail
generation, metadata extraction, video transcoding) are still draining. Check
`/api/jobs` for queue depth before concluding anything is actually missing.

## `curl`'s exit code doesn't mean the HTTP request succeeded

**Symptom:** a script shells out to `curl` to call an API, checks
`subprocess.run(...).returncode`, and treats `0` as success — but the API call
was actually rejected (e.g. `401 Unauthorized` due to a missing permission
scope on an API key), and the script logs and behaves as if it worked.

**Root cause:** curl's own exit code reflects whether curl itself managed to
make the request and get *a* response — it's 0 as long as the connection
succeeded and it received an HTTP response of any kind, including a 4xx or
5xx. It does **not** reflect the HTTP status code. Concretely, this bit an
Immich API key that was created with only `asset.upload` and `asset.read`
permissions but was also used to tag assets as favorites (which needs
`asset.update`) — the tagging call returned `401 Authentication required`,
curl exited `0` because it successfully received that 401 response, and
naive `returncode`-only error handling logged the operation as a success.

**Fix:** never trust `returncode` alone for a curl-shelled-out API call. Use
`curl -s -w '\n%{http_code}'` to append the actual HTTP status code to the
output, split it off, and explicitly check it's in the 2xx range before
treating the call as successful:

```python
result = subprocess.run(["curl", "-s", "-w", "\n%{http_code}"] + args,
                         capture_output=True, text=True)
body, _, status = result.stdout.rpartition("\n")
if not status.isdigit() or not (200 <= int(status) < 300):
    raise RuntimeError(f"HTTP {status}: {body.strip()[:200]}")
```

This generalizes beyond curl: any time a script shells out to a CLI tool that
wraps a network call, "the subprocess exited 0" and "the operation actually
succeeded" are two different claims, and conflating them is an easy way to
build a pipeline that silently logs failures as successes.

## A background job queue can grow instead of shrink under CPU overload

**Symptom:** after a large bulk import, Immich's background job queue depth
(thumbnails, face detection, etc.) is climbing instead of draining — even
though the system is clearly doing real work (high CPU usage, no errors in
the logs at first glance).

**Root cause:** system load average had climbed to roughly 5x the CPU's core
count. Job queues built on a stalled-worker safety mechanism (this affects
Immich's BullMQ-based queue, and would affect any similarly-built queue)
assume a worker has died if it doesn't check in within a timeout — under
severe CPU contention, workers are often still genuinely working, just too
slow to check in on time, so already-in-progress jobs get silently re-queued
as if new. The more overloaded the system gets, the more jobs get bounced
back to the queue, which just adds more load — a self-reinforcing loop.

**Diagnosis:** check `uptime`'s load average against your actual core count
*before* assuming a queue-depth-increasing pattern is a bug in the app itself.
A load average several times your core count, combined with a queue that's
growing rather than shrinking, is the signature of this specific loop.

**Fix:** lower job concurrency (how many jobs run in parallel) so each one
reliably finishes inside the stall timeout instead of getting bounced.
Counterintuitively, doing *less* work at once resolved it — the queue
actually started draining once jobs stopped being redundantly repeated.

**Watching progress:** [`scripts/queue_watch.py`](../scripts/queue_watch.py)
polls each queue's depth over SSH, keeps a local history file between runs,
and prints a table of recent checkpoints with deltas, a trend arrow per
queue, and a rough ETA — along with a small hardware-temperature table if
you're also watching for thermal issues (see
[Hardware](architecture.md#hardware)). Update the `QUEUES` list and
`TEMP_ZONES` mapping at the top of the script to match your own setup.

## Docker-Desktop-alternative VMs have their own fixed resource limits, separate from your host machine's

**Symptom:** a container you've offloaded work to (see
[`architecture.md`](architecture.md#offloading-immichs-ml-workload-to-a-second-machine))
appears to accept connections fine (`/ping`-style health checks succeed), but
real requests fail intermittently with generic "fetch failed" / connection-reset
errors, and the container's own logs show something like `Worker was sent
SIGKILL! Perhaps out of memory?` in a crash-restart loop.

**Root cause:** Docker Desktop and alternatives like Rancher Desktop run
containers inside a lightweight Linux VM with **its own fixed memory/CPU
allocation, entirely separate from your host machine's total resources** — by
default, often just a few GB, regardless of how much RAM the physical machine
actually has. If the workload needs more memory than that VM was given (e.g.
several ML models loaded into memory simultaneously), it gets OOM-killed
inside the VM even though the host itself has plenty of free RAM sitting idle.

**Diagnosis:** `docker info` reports the VM's allocated resources (`Total
Memory`, `CPUs`), not the host's — compare that against what your host
actually has free before concluding a workload "just doesn't fit."

**Fix (Rancher Desktop):** `rdctl set --virtual-machine.memory-in-gb <N>
--virtual-machine.number-cpus <N>` resizes the VM (triggers a brief restart of
the Docker daemon and any running containers — anything with `restart:
unless-stopped` comes back up automatically once it's done). Docker Desktop
has an equivalent resource slider in its GUI preferences. Check real host
headroom first (CPU idle%, and actual memory pressure/swap usage, not just
raw "free" memory which macOS in particular treats as an aggressive cache
rather than a true measure of availability) before deciding how far to push
the allocation — over-allocating can starve the host itself or worsen
existing swap pressure rather than helping.

## Fan-cooling a passive heatsink can make temperatures *worse* if misoriented

**Symptom:** you add a fan to an existing passive heatsink expecting lower
temperatures, and they hold flat or get slightly worse.

**Root cause:** a fan blowing air *at* a finned heatsink (forcing air through
the fin channels) is generally more effective than one pulling air away from
it — but only if that now-heated air actually has somewhere to escape
afterward. In a mostly-enclosed case with no clear exit path, moving air
around just recirculates the same hot air back over the components instead of
carrying it away.

**Fix:** confirm both (1) the fan is oriented to blow directly into the fins,
not just positioned nearby, and (2) there's an actual exit path for the
now-heated air once it's passed through — an open vent, a gap in the case,
anywhere it can leave rather than circulate. Both matter; either alone isn't
enough.

## Immich doesn't inherit Jellyfin's GPU access, even on the same host

**Symptom:** Jellyfin's Intel QuickSync hardware transcoding already works
(fast, low CPU), but Immich's own video transcoding (`videoConversion` jobs)
is still visibly slow and CPU-bound — no errors, it just never gets faster.

**Root cause:** GPU device access in Docker is granted per-container, not
per-host. Jellyfin having `devices: ["/dev/dri:/dev/dri"]` in its own service
block says nothing about `immich-server`'s container, which does its own
separate ffmpeg-based transcoding and needs the exact same device
passthrough independently.

**Fix:** two things, both required — neither alone is enough:
1. Add the same `devices`/`group_add` block to `immich-server` in your
   compose file that Jellyfin already has (see
   [`config/docker-compose.yml`](../config/docker-compose.yml)) and recreate
   the container.
2. In Immich's admin UI (Administration → Settings → Video Transcoding
   Settings), explicitly set **Hardware Acceleration** to match your GPU
   (`qsv` for Intel QuickSync) — device access alone doesn't make Immich
   *use* the GPU, it's a separate opt-in setting. Confirm it's actually
   active by checking `immich_server`'s logs for `"...with QSV-accelerated
   encoding and decoding"` on a freshly-started transcode job — logs still
   saying `"...without hardware acceleration"` mean one of the two steps
   above didn't take.

Real result from making this change: video transcode throughput went from
roughly one video every several seconds to several videos per second, and
CPU package temperature *dropped* a couple degrees under the same workload
(GPU now doing the encode/decode work instead of the CPU) — worth doing on
any Intel NAS box running both apps together.

## Recreating `immich-server` can trigger a large, blocking reindex if you've imported a lot since its last restart

**Symptom:** you recreate the `immich-server` container for an unrelated
config change (e.g. adding GPU device access, bumping a resource limit) and
it comes up reporting `unhealthy` for several minutes — the API doesn't
respond at all (connection refused / timeout) during this window, even
though the container itself is running and not crash-looping.

**Root cause:** on startup, Immich checks whether its Postgres vector
indexes (`face_index`, `clip_index`) need rebuilding based on how much the
underlying tables have grown. After a large bulk import, the answer can be
"yes, substantially" — logs will show `Reindexing face_index (This may take
a while, do not restart)` and similar for `clip_index`. This is a real,
CPU-intensive `CREATE INDEX` + `VACUUM ANALYZE` operation running inside
Postgres (confirm with `SELECT pid, state, query FROM pg_stat_activity WHERE
state != 'idle'` if you want to see it actively working rather than take it
on faith), not a hang — but the server can't answer API requests until it
finishes, and it can genuinely take many minutes on hundreds of thousands of
rows.

**Fix:** don't restart the container again while this is running — the log
message's "do not restart" warning is not a suggestion; interrupting it
mid-rebuild is the actual risk here, not the wait itself. Just wait it out;
`docker logs -f immich_server` will show it transition back to normal
request-handling log lines once done. If you're about to recreate
`immich-server` for a planned change right after a big import, expect this
and don't be alarmed by several minutes of "unhealthy."

## `shutil.copy2()` can silently run 5-8x slower than `cp` against a non-standard kernel filesystem

**Symptom:** a Python file-copy script (in this case, `organize_recovery.py`'s
`transfer` command) runs at ~12MB/s copying files, while `cp` or `dd` against
the exact same source file on the exact same source/destination pair hits
85-100MB/s. The slowdown isn't universal — it's specific to reading from a
particular source filesystem.

**Diagnosis approach:** isolate variables one at a time rather than guessing.
`dd if=<file> of=/dev/null bs=1M` measured raw *read* throughput at
100MB/s+, proving the source disk/filesystem itself wasn't the bottleneck.
Plain `cp` on a different real file from the same source, timed by hand,
matched that ~85MB/s. That leaves exactly one variable: the Python script's
own copy mechanism.

**Root cause:** `shutil.copy2()` automatically tries to use zero-copy
fast-path syscalls (`os.sendfile()` on Linux, `fcopyfile()` on macOS) instead
of a plain read/write loop, when it thinks the underlying filesystem
supports them efficiently. In this case the source was mounted via
`linux-apfs-rw` — a third-party, out-of-tree kernel module providing APFS
read support on Linux (used to recover data from an old macOS-formatted
drive) — and `sendfile()`'s fast path apparently degrades badly against it,
for reasons not fully root-caused (a reasonable guess is the module doesn't
implement the same efficient in-kernel data path a mainstream filesystem
does, so `sendfile()` ends up worse than a plain buffered copy instead of
better). This is specific to unusual/non-mainstream filesystem drivers —
copying between two ordinary filesystems (ext4, XFS, APFS-on-macOS itself,
etc.) is exactly the case `sendfile()` is supposed to help with.

**Fix:** bypass `shutil.copy2()`'s automatic fast-path selection with an
explicit buffered copy:

```python
def fast_copy(src, dest, bufsize=8 * 1024 * 1024):
    with open(src, "rb") as fsrc, open(dest, "wb") as fdst:
        shutil.copyfileobj(fsrc, fdst, length=bufsize)
    shutil.copystat(src, dest)
```

This reliably matched `cp`-level throughput regardless of the source
filesystem's `sendfile()` support/quirks. **General lesson:** if a Python
copy loop is mysteriously slow against one particular filesystem/mount while
`cp`/`dd` on the identical files are fast, suspect `shutil`'s automatic
fast-path selection before suspecting your own code's logic — it's invisible
in a stack trace or profiler line count, since the slow part is happening
inside a syscall `shutil` chose on your behalf.

## Firefox's DNS-over-HTTPS breaks `.local`/mDNS hostnames

**Symptom:** a `.local` hostname (e.g. `photos.local`, served via mDNS/Avahi
the way everything in this repo is) works fine in Safari, `ping`, and Time
Machine's disk picker, but fails to resolve in Firefox specifically — Firefox
reports it can't find the site, on a machine where every other resolution path
works.

**Root cause:** Firefox has DNS-over-HTTPS (DoH) enabled by default in many
regions, which sends DNS queries to an external resolver (typically
Cloudflare) over HTTPS instead of asking the OS's native resolver. That
external resolver has no knowledge of your LAN's mDNS names and correctly
returns "this doesn't exist" — a clean, valid negative response, not a
timeout or error. Firefox's fallback-to-OS-resolver logic normally only
triggers when the DoH resolver is *unreachable*, not when it gives a valid
"not found" answer, so it never falls through to the OS resolver that would
have handled `.local` correctly via Bonjour/mDNSResponder (macOS) or
`nss-mdns` (Linux). Safari, `ping`, and anything else using the OS resolver
directly never hits this, which is the tell — if only one browser fails while
everything else on the same machine works, suspect that browser's own DNS
path rather than the server.

**Fix:** in Firefox, go to Settings → General → scroll to "Network Settings"
→ Settings... → uncheck **"Enable DNS over HTTPS."** Alternatively, leave DoH
on and exclude local domains specifically: `about:config` →
`network.trr.excluded-domains` → add `local`, or set `network.trr.mode` to
`5` (disabled by user choice). No separate cache-flush is needed — this isn't
a caching problem, it's a resolver returning a genuine (if useless, for your
purposes) answer every time.

## Bulk-importing an unfamiliar music library can hit MusicBrainz's rate limit hard

**Symptom:** after pointing Jellyfin at a large, freshly-reorganized music
library (thousands of artist folders it's never seen before), the library
scan's progress bar crawls to a near-stop partway through and stays there for
a long time, even though the process is still technically "Running."

**Root cause:** Jellyfin looks up artist/album metadata (photos, bios, sort
names) from MusicBrainz's free public API by default. MusicBrainz enforces a
strict per-client rate limit (roughly 1 request/second for anonymous
clients). Pointing Jellyfin at thousands of never-before-seen artists in one
scan means thousands of individual lookups queued up against that limit —
`docker logs jellyfin` (or its own log files under `/config/log/`) will show
repeated `HTTP 503 - The MusicBrainz web server is currently busy` errors
during this window, which is MusicBrainz's rate limiter, not a Jellyfin bug
or a network problem on your end.

**Fix:** there isn't one that speeds this up — it's an external service's
rate limit, working as designed. What matters is knowing it's *not* actually
stuck: local playback and browsing already work off the files/folder
structure on disk the moment the scan indexes them; only the cosmetic
metadata (artist images, bios) trickles in slowly afterward as MusicBrainz
allows more requests through. Don't kill and restart the scan thinking
something's wrong — that just restarts the same rate-limited queue from
scratch.

## Immich has no cross-account write access, even for jointly-owned content

**Symptom:** a family/household photo library that should logically belong
to everyone ends up entirely under one person's Immich account after a bulk
import, and there's no setting that lets a second account manage
(delete/edit/reorganize) that content under their own login — Partner Sharing
only grants read/download access.

**Root cause:** every Immich asset has exactly one owner, and there's no
ownership-transfer API and no "grant another account write access to my
library" feature. This is a deliberate design boundary, not a missing
feature waiting to be toggled on — see
[`architecture.md`](architecture.md#immich-has-no-real-concept-of-a-jointly-owned-library)
for the full reasoning and what to do about it (short version: share login
credentials for jointly-owned content, or consolidate onto one account
*before* a large import rather than after).
