"""
mission.mhpwebserver.com -- backend for the launch & active-mission dashboard.

Serves one normalised JSON document assembled from Launch Library 2 (see
ll2.py), plus a caching image proxy. Visitors never touch the upstream API or
its CDN directly: the request budget is shared and finite, and proxying the
imagery keeps visitor IPs off a third-party host.
"""

import hashlib
import logging
import mimetypes
import os
import re
import threading
import time
from collections import OrderedDict
from urllib.parse import urlparse

import requests
from flask import Flask, Response, jsonify, request, send_file

import ll2

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("mission")

app = Flask(__name__)

STORE = ll2.FeedStore()
ll2.start_refresher(STORE)

# ---------------------------------------------------------------------------
# Payload cache
#
# build_payload() walks a few hundred records. That is fast, but it is also
# entirely determined by the feed version, so rebuilding per request would be
# pure waste on a page that polls.
# ---------------------------------------------------------------------------
_payload_lock = threading.Lock()
_payload_cache = {"version": None, "body": None, "built_at": 0}


def current_payload():
    _feeds, version = STORE.snapshot()
    with _payload_lock:
        cached = _payload_cache
        # Feed status (ages, errors) is part of the payload and moves with the
        # clock, so a version match is only good for a short while.
        if cached["version"] == version and cached["body"] and time.time() - cached["built_at"] < 30:
            return cached["body"]
    body = ll2.build_payload(STORE)
    with _payload_lock:
        _payload_cache.update({"version": version, "body": body, "built_at": time.time()})
    return body


@app.route("/api/data")
def api_data():
    payload = current_payload()
    resp = jsonify(payload)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/health")
def api_health():
    status = STORE.status()
    ready = any(f["cached"] for f in status.values())
    return (
        jsonify(
            {
                "ok": ready,
                "feeds": status,
                "server_time": ll2.iso(time.time()),
            }
        ),
        200 if ready else 503,
    )


# ---------------------------------------------------------------------------
# Image proxy
# ---------------------------------------------------------------------------

# Exact-match allowlist. Anything not on it is refused outright -- this endpoint
# takes a URL from the client, so an open fetcher here would be an SSRF hole
# straight into the LAN. It is defined next to the normaliser (ll2.IMAGE_HOSTS)
# so the payload can never advertise an image this route would refuse.
ALLOWED_IMAGE_HOSTS = ll2.IMAGE_HOSTS

IMG_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "img")
os.makedirs(IMG_CACHE, exist_ok=True)
IMG_MAX_BYTES = 8 * 1024 * 1024

# The cache is bounded because /api/img is unauthenticated and i.ytimg.com is on
# the allowlist: every YouTube video id is a distinct, allowed, real image, so a
# script walking ids could otherwise fill the disk one 100KB thumbnail at a
# time. The real working set is ~50MB (a few hundred files), so this leaves a
# large margin for a busy day and still has a ceiling.
IMG_CACHE_MAX_BYTES = int(float(os.environ.get("IMG_CACHE_MAX_MB", "256")) * 1024 * 1024)
# How far below the cap a sweep trims, so eviction is occasional rather than
# once per download at the ceiling.
IMG_CACHE_TRIM_TO = 0.80
# Nothing this new is ever evicted: it is either being served right now or was
# just warmed, and evicting it would only cause an immediate refetch.
IMG_EVICT_MIN_AGE = 300
IMG_SWEEP_INTERVAL = 60
# Freshness is tracked by mtime, updated on serve -- but only when it is
# already this stale, so a hot image doesn't cost a syscall per request.
IMG_TOUCH_AFTER = 3600

# Bounded too: one entry per distinct URL is unbounded memory on the same
# enumeration attack.
IMG_LOCKS_MAX = 512
_img_locks = OrderedDict()
_img_locks_guard = threading.Lock()
_last_sweep = 0.0
_sweep_guard = threading.Lock()


def _img_lock(key):
    """Per-file download lock, LRU-bounded.

    Dropping a lock another thread is holding would let two downloads of the
    same URL run at once; they are skipped. That is safe anyway -- each writes
    its own temp file and os.replace()s it into place -- but there is no reason
    to invite it.
    """
    with _img_locks_guard:
        lock = _img_locks.get(key)
        if lock is None:
            lock = _img_locks[key] = threading.Lock()
        else:
            _img_locks.move_to_end(key)
        excess = len(_img_locks) - IMG_LOCKS_MAX
        if excess > 0:
            for old_key in list(_img_locks)[:excess]:
                if old_key != key and not _img_locks[old_key].locked():
                    del _img_locks[old_key]
        return lock


def evict_images(force=False):
    """Trim the image cache back under its cap, oldest first.

    Cheap enough to call after every download: it scans the directory only once
    a minute, and does nothing at all unless the cap is exceeded.
    """
    global _last_sweep
    now = time.time()
    with _sweep_guard:
        if not force and now - _last_sweep < IMG_SWEEP_INTERVAL:
            return 0
        _last_sweep = now

    entries = []
    total = 0
    try:
        with os.scandir(IMG_CACHE) as it:
            for entry in it:
                try:
                    if not entry.is_file():
                        continue
                    stat = entry.stat()
                except OSError:
                    continue
                total += stat.st_size
                entries.append((stat.st_mtime, stat.st_size, entry.path))
    except FileNotFoundError:
        return 0

    if total <= IMG_CACHE_MAX_BYTES:
        return 0

    target = int(IMG_CACHE_MAX_BYTES * IMG_CACHE_TRIM_TO)
    entries.sort()  # oldest mtime first
    removed = 0
    for mtime, size, path in entries:
        if total <= target:
            break
        if now - mtime < IMG_EVICT_MIN_AGE:
            continue  # in flight or just warmed
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("could not evict %s: %s", path, exc)
            continue
        total -= size
        removed += 1
    if removed:
        log.info("image cache over %dMB: evicted %d files, now ~%dMB",
                 IMG_CACHE_MAX_BYTES // (1024 * 1024), removed, total // (1024 * 1024))
    return removed


class ImageError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def img_cache_path(url):
    """Cache path for an allowlisted image URL, or None if it isn't allowed."""
    parsed = urlparse(url or "")
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_IMAGE_HOSTS:
        return None
    ext = os.path.splitext(parsed.path)[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        ext = ".img"
    return os.path.join(IMG_CACHE, hashlib.sha256(url.encode("utf-8")).hexdigest()[:32] + ext)


def fetch_image(url, path):
    """Download an image into the cache unless it is already there."""
    if os.path.exists(path):
        return path
    with _img_lock(os.path.basename(path)):
        if os.path.exists(path):  # another thread may have won the race
            return path
        # Unique per attempt: two threads can legitimately be downloading the
        # same URL (see _img_lock), and they must not share a temp file.
        tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
        try:
            r = requests.get(url, timeout=30, stream=True, headers={"User-Agent": ll2.USER_AGENT})
            r.raise_for_status()
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip()
            if not ctype.startswith("image/"):
                raise ImageError(415, "not an image")
            written = 0
            with open(tmp, "wb") as fh:
                for chunk in r.iter_content(64 * 1024):
                    written += len(chunk)
                    if written > IMG_MAX_BYTES:
                        raise ImageError(413, "image too large")
                    fh.write(chunk)
            os.replace(tmp, path)
        except ImageError:
            raise
        except Exception as exc:
            log.warning("image proxy failed for %s: %s", url, exc)
            raise ImageError(502, "upstream fetch failed")
        finally:
            # A partial download must never be left behind: it is bytes the cap
            # accounts for and nothing will ever serve.
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    evict_images()
    return path


def warm_images():
    """Pre-fetch every image the payload references.

    Without this the first visitor after a feed refresh triggers ~75 cold proxy
    misses at once, which trickle in over a minute or more while logos and crew
    portraits render as empty boxes. Warming in the background means the browser
    is always served from disk.
    """

    def urls_in(payload):
        out = []
        for launch in (payload.get("upcoming") or []) + (payload.get("previous") or []):
            out += [launch.get("image"), launch.get("patch")]
            if launch.get("provider"):
                out.append(launch["provider"].get("logo"))
            # Stream posters: the hero video box is the first thing a visitor
            # looks at, so its poster must not be a cold miss.
            for stream in launch.get("streams") or []:
                out += [stream.get("thumb"), stream.get("thumb_alt")]
        for group in ("astronauts", "spacecraft", "stations", "operators"):
            for item in payload.get(group) or []:
                out.append(item.get("image") or item.get("logo"))
        return [u for u in out if u]

    def loop():
        seen_version = None
        while True:
            try:
                payload = current_payload()
                if payload.get("version") != seen_version:
                    seen_version = payload.get("version")
                    pending = []
                    for url in dict.fromkeys(urls_in(payload)):
                        path = img_cache_path(url)
                        if path and not os.path.exists(path):
                            pending.append((url, path))
                    if pending:
                        log.info("warming %d images", len(pending))
                        for url, path in pending:
                            try:
                                fetch_image(url, path)
                            except ImageError:
                                pass
                            time.sleep(0.25)  # be a polite CDN client
            except Exception as exc:
                log.exception("image warmer failed: %s", exc)
            time.sleep(120)

    threading.Thread(target=loop, name="img-warmer", daemon=True).start()


@app.route("/api/img")
def api_img():
    url = request.args.get("u", "")
    if not url:
        return jsonify({"error": "missing u"}), 400

    path = img_cache_path(url)
    if not path:
        return jsonify({"error": "host not allowed"}), 403

    try:
        fetch_image(url, path)
    except ImageError as exc:
        return jsonify({"error": exc.message}), exc.status

    # mtime is the cache's LRU clock, so a hit counts as a use. Only written
    # when it is already stale, to keep this off the hot path.
    try:
        if time.time() - os.path.getmtime(path) > IMG_TOUCH_AFTER:
            os.utime(path, None)
    except OSError:
        pass

    ctype = mimetypes.guess_type(path)[0] or "image/jpeg"
    resp = send_file(path, mimetype=ctype, conditional=True)
    # Upstream art is immutable per URL, so this is the one thing on the box
    # worth letting the browser and Cloudflare hold on to.
    resp.headers["Cache-Control"] = "public, max-age=604800"
    return resp


# Deliberately no manual /api/refresh route. It would be unauthenticated, and
# even at a 5-minute cooldown a caller hammering it adds 12 forced upstream
# fetches an hour on top of the ~6 the scheduler already spends -- enough to
# blow the 15/hour public limit and take the dashboard's data stale for
# everyone. The background refresher is the only thing that talks to LL2.

warm_images()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8793, debug=False)
