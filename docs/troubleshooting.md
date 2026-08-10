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
