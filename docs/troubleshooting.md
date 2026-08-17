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
