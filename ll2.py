"""
Launch Library 2 (thespacedevs.com) client.

The public tier of LL2 allows roughly 15 requests per hour per IP, which is the
single constraint that shapes this whole module: we can never fetch on behalf of
a visitor. Instead a background thread refreshes a handful of feeds on staggered
TTLs (~6 requests/hour total at steady state), writes each raw response to disk,
and the web layer only ever reads from that cache. A cold start, an API outage
and a rate-limit lockout all degrade the same way -- we keep serving the last
good copy and report how old it is.
"""

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone

import requests

log = logging.getLogger("mission.ll2")

API_ROOT = "https://ll.thespacedevs.com/2.3.0"
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
USER_AGENT = "mission.mhpserver.cc (personal launch dashboard)"

# name -> (path, query params, ttl seconds)
#
# TTLs are deliberately uneven. Launch schedules slip by the minute so
# `upcoming` gets the lion's share of our request budget; crew rosters and
# station manifests change a few times a year and cost us almost nothing.
FEEDS = {
    "upcoming": (
        "/launches/upcoming/",
        {"limit": 40, "mode": "detailed", "hide_recent_previous": "true"},
        20 * 60,
    ),
    "previous": (
        "/launches/previous/",
        {"limit": 12, "mode": "detailed"},
        60 * 60,
    ),
    "astronauts": (
        "/astronauts/",
        {"in_space": "true", "limit": 30},
        60 * 60,
    ),
    "spacecraft": (
        "/spacecraft/",
        {"in_space": "true", "limit": 20},
        60 * 60,
    ),
    "stations": (
        "/space_stations/",
        {"status": 1, "limit": 10},
        6 * 60 * 60,
    ),
}

# A feed that errors backs off on its own schedule so one broken endpoint can't
# burn the shared request budget that the healthy ones depend on.
BACKOFF_BASE = 5 * 60
BACKOFF_MAX = 4 * 60 * 60


class FeedStore:
    """Raw LL2 responses, cached on disk and refreshed by a background thread."""

    def __init__(self):
        self._lock = threading.Lock()
        self._feeds = {}  # name -> {"data":..., "fetched_at": float, "failures": int, "next_try": float, "error": str|None}
        self._version = 0
        os.makedirs(CACHE_DIR, exist_ok=True)
        self._load_from_disk()

    # ---------- persistence ----------

    def _path(self, name):
        return os.path.join(CACHE_DIR, f"{name}.json")

    def _load_from_disk(self):
        for name in FEEDS:
            try:
                with open(self._path(name), "r", encoding="utf-8") as fh:
                    blob = json.load(fh)
                self._feeds[name] = {
                    "data": blob["data"],
                    "fetched_at": blob.get("fetched_at", 0),
                    "failures": 0,
                    "next_try": 0,
                    "error": None,
                }
                log.info("loaded cached feed %s from disk", name)
            except FileNotFoundError:
                pass
            except Exception as exc:  # corrupt cache must never block startup
                log.warning("ignoring unreadable cache for %s: %s", name, exc)

    def _save_to_disk(self, name, entry):
        tmp = self._path(name) + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"fetched_at": entry["fetched_at"], "data": entry["data"]}, fh)
            os.replace(tmp, self._path(name))
        except Exception as exc:
            log.warning("could not persist feed %s: %s", name, exc)

    # ---------- fetching ----------

    def _fetch(self, name):
        path, params, _ttl = FEEDS[name]
        url = API_ROOT + path
        resp = requests.get(
            url,
            params=params,
            timeout=45,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        if resp.status_code == 429:
            raise RuntimeError("rate limited by LL2 (429)")
        resp.raise_for_status()
        return resp.json()

    def refresh(self, name, force=False):
        """Fetch one feed if it is stale (or forced). Returns True on a fresh fetch."""
        now = time.time()
        with self._lock:
            entry = self._feeds.get(name)
            if not force and entry:
                _p, _q, ttl = FEEDS[name]
                if now - entry["fetched_at"] < ttl:
                    return False
                if now < entry.get("next_try", 0):
                    return False

        try:
            data = self._fetch(name)
        except Exception as exc:
            with self._lock:
                entry = self._feeds.setdefault(
                    name, {"data": None, "fetched_at": 0, "failures": 0, "next_try": 0, "error": None}
                )
                entry["failures"] += 1
                entry["error"] = str(exc)
                delay = min(BACKOFF_BASE * (2 ** (entry["failures"] - 1)), BACKOFF_MAX)
                entry["next_try"] = time.time() + delay
            log.warning("feed %s failed (%s); retrying in %ds", name, exc, int(delay))
            return False

        entry = {"data": data, "fetched_at": time.time(), "failures": 0, "next_try": 0, "error": None}
        with self._lock:
            self._feeds[name] = entry
            self._version += 1
        self._save_to_disk(name, entry)
        log.info("refreshed feed %s (%s results)", name, len(data.get("results", [])))
        return True

    # ---------- reading ----------

    def snapshot(self):
        with self._lock:
            return (
                {n: dict(e) for n, e in self._feeds.items()},
                self._version,
            )

    def status(self):
        feeds, _v = self.snapshot()
        out = {}
        for name in FEEDS:
            e = feeds.get(name)
            out[name] = {
                "cached": bool(e and e.get("data")),
                "fetched_at": iso(e["fetched_at"]) if e and e.get("fetched_at") else None,
                "age_seconds": int(time.time() - e["fetched_at"]) if e and e.get("fetched_at") else None,
                "ttl_seconds": FEEDS[name][2],
                "failures": e["failures"] if e else 0,
                "error": e.get("error") if e else None,
            }
        return out


def iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def start_refresher(store):
    """Background loop: refresh at most one stale feed per tick.

    One-at-a-time is intentional. Refreshing everything the moment it expires
    would bunch five requests into the same second and, on a cold start, trip
    the hourly limit before the dashboard has ever rendered.
    """

    def loop():
        # Prime whatever is missing first, most important feed first.
        for name in ("upcoming", "astronauts", "spacecraft", "stations", "previous"):
            feeds, _ = store.snapshot()
            if not (feeds.get(name) or {}).get("data"):
                store.refresh(name)
                time.sleep(3)
        while True:
            try:
                for name in FEEDS:
                    if store.refresh(name):
                        break  # one network call per tick
            except Exception as exc:
                log.exception("refresher tick failed: %s", exc)
            time.sleep(60)

    t = threading.Thread(target=loop, name="ll2-refresher", daemon=True)
    t.start()
    return t


# --------------------------------------------------------------------------
# Normalisation
#
# LL2's detailed payloads are enormous (a single launch is ~15KB) and most of it
# is of no use to the dashboard. Everything below trims each record to the
# fields the UI actually renders, which keeps /api/data around 100KB instead of
# several megabytes.
# --------------------------------------------------------------------------


# LL2 records use a handful of literal placeholder strings where a value is
# simply not known yet. Passing them through produces sentences like "unknown
# mission to Unknown", so they are collapsed to None at the boundary and the UI
# omits the field entirely instead.
_PLACEHOLDERS = {"unknown", "n/a", "na", "tbd", "tba", "none", "-", "--", "?"}


# Descriptions get their own pass: LL2 uses a stock "Details TBD." sentence for
# missions whose payload has not been announced, which is worse than no prose.
_PLACEHOLDER_PROSE = re.compile(r"^(details?\s*(are\s*)?)?(tbd|tba|unknown|n/?a)\.?$", re.I)


def _clean(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in _PLACEHOLDERS or _PLACEHOLDER_PROSE.match(text):
        return None
    return text


def _duration(value):
    """Drop zero-length ISO-8601 durations ("P0D") -- they mean "never", not 0m."""
    text = _clean(value)
    if not text:
        return None
    if not re.search(r"[1-9]", text):
        return None
    return text


def _coord(value):
    """Coordinates arrive as floats or as strings depending on the endpoint."""
    if value is None or value == "":
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if abs(f) <= 180.0 else None


def _img(obj, prefer_thumb=False):
    """LL2 image objects vary by response mode; some are plain strings."""
    if not obj:
        return None
    if isinstance(obj, str):
        return obj or None
    if isinstance(obj, dict):
        if prefer_thumb:
            return obj.get("thumbnail_url") or obj.get("image_url") or None
        return obj.get("image_url") or obj.get("thumbnail_url") or None
    return None


def _country(obj):
    """`country` is a list in 2.3.0 for agencies, a dict for pads."""
    if isinstance(obj, list):
        return [c.get("name") for c in obj if isinstance(c, dict) and c.get("name")]
    if isinstance(obj, dict) and obj.get("name"):
        return [obj["name"]]
    if isinstance(obj, str) and obj:
        return [obj]
    return []


def _codes(obj):
    if isinstance(obj, list):
        return [c.get("alpha_2_code") for c in obj if isinstance(c, dict) and c.get("alpha_2_code")]
    if isinstance(obj, dict) and obj.get("alpha_2_code"):
        return [obj["alpha_2_code"]]
    return []


def norm_provider(p):
    if not isinstance(p, dict):
        return None
    return {
        "id": p.get("id"),
        "name": p.get("name"),
        "abbrev": p.get("abbrev") or p.get("name"),
        "type": (p.get("type") or {}).get("name") if isinstance(p.get("type"), dict) else p.get("type"),
        "country": _country(p.get("country")),
        "country_codes": _codes(p.get("country")),
        "logo": _img(p.get("logo")) or _img(p.get("social_logo")),
        "image": _img(p.get("image")),
        "description": p.get("description"),
        "administrator": p.get("administrator"),
        "founding_year": p.get("founding_year"),
        "info_url": p.get("info_url"),
        "wiki_url": p.get("wiki_url"),
        "total_launch_count": p.get("total_launch_count"),
        "successful_launches": p.get("successful_launches"),
        "failed_launches": p.get("failed_launches"),
        "pending_launches": p.get("pending_launches"),
        "consecutive_successful_launches": p.get("consecutive_successful_launches"),
        "successful_landings": p.get("successful_landings"),
        "attempted_landings": p.get("attempted_landings"),
    }


def _webcast(launch):
    vids = launch.get("vid_urls") or []
    best = None
    for v in vids:
        if not isinstance(v, dict) or not v.get("url"):
            continue
        if best is None or (v.get("priority") or 99) < (best.get("priority") or 99):
            best = v
    if not best:
        return None
    return {
        "url": best.get("url"),
        "title": best.get("title"),
        "publisher": best.get("publisher") or best.get("source"),
        "live": bool(launch.get("webcast_live")),
    }


def norm_launch(l):
    rocket_cfg = ((l.get("rocket") or {}).get("configuration")) or {}
    mission = l.get("mission") or {}
    pad = l.get("pad") or {}
    loc = pad.get("location") or {}
    status = l.get("status") or {}
    patches = l.get("mission_patches") or []
    patch = _img(patches[0]) if patches and isinstance(patches[0], dict) else None

    return {
        "id": l.get("id"),
        "name": l.get("name"),
        "slug": l.get("slug"),
        "net": l.get("net"),
        "net_precision": (l.get("net_precision") or {}).get("abbrev"),
        "window_start": l.get("window_start"),
        "window_end": l.get("window_end"),
        "status": {
            "id": status.get("id"),
            "name": status.get("name"),
            "abbrev": status.get("abbrev"),
            "description": status.get("description"),
        },
        "probability": l.get("probability"),
        "weather_concerns": l.get("weather_concerns"),
        "failreason": l.get("failreason") or None,
        "image": _img(l.get("image")),
        "patch": patch,
        "provider": norm_provider(l.get("launch_service_provider")),
        "rocket": {
            "name": _clean(rocket_cfg.get("full_name")) or _clean(rocket_cfg.get("name")),
            "family": _clean(rocket_cfg.get("name")),
            "variant": _clean(rocket_cfg.get("variant")),
            "reusable": rocket_cfg.get("reusable"),
            "description": rocket_cfg.get("description"),
            "wiki_url": rocket_cfg.get("wiki_url"),
            "length": rocket_cfg.get("length"),
            "leo_capacity": rocket_cfg.get("leo_capacity"),
            "total_launch_count": rocket_cfg.get("total_launch_count"),
            "successful_launches": rocket_cfg.get("successful_launches"),
            "failed_launches": rocket_cfg.get("failed_launches"),
        },
        "mission": {
            "name": _clean(mission.get("name")),
            "type": _clean(mission.get("type")),
            "description": _clean(mission.get("description")),
            "orbit": _clean((mission.get("orbit") or {}).get("name")),
            "orbit_abbrev": _clean((mission.get("orbit") or {}).get("abbrev")),
        },
        "pad": {
            "name": _clean(pad.get("name")),
            "location": _clean(loc.get("name")),
            "country": (_country(pad.get("country")) or [None])[0],
            "country_code": (_codes(pad.get("country")) or [None])[0],
            "latitude": _coord(pad.get("latitude")),
            "longitude": _coord(pad.get("longitude")),
            # The site's own coordinates, used to group pads onto one map
            # marker -- individual pads at a site sit a few km apart, which is
            # well under one pixel on a world map.
            "location_latitude": _coord(loc.get("latitude")),
            "location_longitude": _coord(loc.get("longitude")),
            "map_url": pad.get("map_url"),
        },
        "programs": [
            {"name": p.get("name"), "image": _img(p.get("image")), "url": p.get("info_url") or p.get("wiki_url")}
            for p in (l.get("program") or [])
            if isinstance(p, dict) and p.get("name")
        ],
        "webcast": _webcast(l),
        "last_updated": l.get("last_updated"),
        "update": ((l.get("updates") or [{}])[0] or {}).get("comment"),
    }


def norm_astronaut(a):
    agency = a.get("agency") or {}
    nat = a.get("nationality")
    return {
        "id": a.get("id"),
        "name": a.get("name"),
        "image": _img(a.get("image"), prefer_thumb=True),
        "agency": agency.get("abbrev") or agency.get("name"),
        "agency_name": agency.get("name"),
        "nationality": _country(nat),
        "nationality_codes": _codes(nat),
        "category": _clean((a.get("type") or {}).get("name") if isinstance(a.get("type"), dict) else a.get("type")),
        "category_id": (a.get("type") or {}).get("id") if isinstance(a.get("type"), dict) else None,
        "in_space_since": a.get("last_flight"),
        "time_in_space": a.get("time_in_space"),
        "wiki": a.get("wiki"),
    }


def norm_spacecraft(s):
    cfg = s.get("spacecraft_config") or {}
    agency = cfg.get("agency") or {}
    return {
        "id": s.get("id"),
        "name": s.get("name"),
        "serial": s.get("serial_number"),
        "image": _img(s.get("image")),
        "description": _clean(s.get("description")),
        "type": _clean((cfg.get("type") or {}).get("name") if isinstance(cfg.get("type"), dict) else cfg.get("type")),
        "config": _clean(cfg.get("name")),
        "crew_capacity": cfg.get("crew_capacity"),
        "agency": agency.get("abbrev") or agency.get("name"),
        "agency_name": agency.get("name"),
        "time_in_space": _duration(s.get("time_in_space")),
        "time_docked": _duration(s.get("time_docked")),
        "flights_count": s.get("flights_count"),
        "status": (s.get("status") or {}).get("name") if isinstance(s.get("status"), dict) else s.get("status"),
    }


def norm_station(s):
    owners = s.get("owners") or []
    return {
        "id": s.get("id"),
        "name": s.get("name"),
        "image": _img(s.get("image")),
        "status": (s.get("status") or {}).get("name") if isinstance(s.get("status"), dict) else s.get("status"),
        "founded": s.get("founded"),
        "description": _clean(s.get("description")),
        "orbit": _clean(s.get("orbit")),
        "type": (s.get("type") or {}).get("name") if isinstance(s.get("type"), dict) else s.get("type"),
        "owners": [o.get("abbrev") or o.get("name") for o in owners if isinstance(o, dict)],
        "height": s.get("height"),
        "width": s.get("width"),
        "mass": s.get("mass"),
        "volume": s.get("volume"),
        "onboard_crew": s.get("onboard_crew"),
        "active_expeditions": [
            e.get("name") for e in (s.get("active_expeditions") or []) if isinstance(e, dict) and e.get("name")
        ],
        "docked_vehicles": [
            {
                "name": (d.get("spacecraft_flight") or {}).get("spacecraft", {}).get("name")
                or d.get("name")
                or "Docked vehicle",
                "docking": d.get("docking"),
            }
            for d in (s.get("docking_location") or [])
            if isinstance(d, dict)
        ],
    }


def _results(feeds, name):
    entry = feeds.get(name) or {}
    data = entry.get("data") or {}
    res = data.get("results")
    return res if isinstance(res, list) else []


def build_payload(store):
    """Assemble the single JSON document the dashboard renders from."""
    feeds, version = store.snapshot()

    upcoming = [norm_launch(l) for l in _results(feeds, "upcoming") if isinstance(l, dict)]
    previous = [norm_launch(l) for l in _results(feeds, "previous") if isinstance(l, dict)]
    # LL2's in-space astronaut roster includes non-human entries -- Starman, the
    # mannequin in the Roadster, has been "in space" since 2018 and would inflate
    # a headline that literally reads "people are in space right now".
    astronauts = [
        a for a in (norm_astronaut(a) for a in _results(feeds, "astronauts") if isinstance(a, dict))
        if a.get("category_id") != 6 and (a.get("category") or "").lower() != "non-human"
    ]
    # Mass simulators (the Falcon Heavy demo's Tesla Roadster) are ballast that
    # happens to still be in space -- not a spacecraft flying a mission.
    spacecraft = [
        s for s in (norm_spacecraft(s) for s in _results(feeds, "spacecraft") if isinstance(s, dict))
        if (s.get("type") or "").lower() != "mass simulator"
    ]
    stations = [norm_station(s) for s in _results(feeds, "stations") if isinstance(s, dict)]

    upcoming.sort(key=lambda l: l.get("net") or "9999")
    previous.sort(key=lambda l: l.get("net") or "", reverse=True)

    sites = build_sites(upcoming)

    return {
        "generated_at": iso(time.time()),
        "version": version,
        "upcoming": upcoming,
        "previous": previous,
        "astronauts": astronauts,
        "spacecraft": spacecraft,
        "stations": stations,
        "operators": build_operators(upcoming, previous),
        "sites": sites,
        "stats": {
            "upcoming_count": len(upcoming),
            "humans_in_space": len(astronauts),
            "spacecraft_in_space": len(spacecraft),
            "active_stations": len(stations),
            "operators_count": len({l["provider"]["id"] for l in upcoming if l.get("provider")}),
            "sites_count": len(sites),
        },
        "feeds": store.status(),
        "source": {
            "name": "The Space Devs - Launch Library 2",
            "url": "https://thespacedevs.com/llapi",
            "license": "CC BY 4.0",
        },
    }


def build_sites(upcoming):
    """Group scheduled launches onto map markers, one per launch site.

    Grouping is by site rather than by pad on purpose: a world map pixel covers
    roughly 20km at this scale, so Vandenberg's SLC-4E and SLC-2W (14km apart)
    would land on top of each other as separate dots. One marker per site, with
    the individual pads listed in its detail panel, is both readable and honest.
    """
    sites = {}

    for launch in upcoming:
        pad = launch.get("pad") or {}
        name = pad.get("location") or pad.get("name")
        lat = pad.get("location_latitude")
        lon = pad.get("location_longitude")
        if lat is None or lon is None:
            lat, lon = pad.get("latitude"), pad.get("longitude")
        if not name or lat is None or lon is None:
            continue
        # 0,0 is "no data" upstream, not a launch site in the Gulf of Guinea.
        if lat == 0 and lon == 0:
            continue

        site = sites.setdefault(
            name,
            {
                "name": name,
                "country": pad.get("country"),
                "country_code": pad.get("country_code"),
                "latitude": lat,
                "longitude": lon,
                "pads": [],
                "launches": [],
            },
        )

        pad_name = pad.get("name")
        # "Unknown Pad" is upstream's placeholder; the site name says more.
        if pad_name and not pad_name.lower().startswith("unknown"):
            if pad_name not in site["pads"]:
                site["pads"].append(pad_name)

        provider = launch.get("provider") or {}
        site["launches"].append(
            {
                "id": launch.get("id"),
                "name": launch.get("name"),
                "mission": (launch.get("mission") or {}).get("name"),
                "net": launch.get("net"),
                "net_precision": launch.get("net_precision"),
                "status": (launch.get("status") or {}).get("abbrev"),
                "status_id": (launch.get("status") or {}).get("id"),
                "provider": provider.get("abbrev") or provider.get("name"),
                "pad": pad_name,
                "rocket": (launch.get("rocket") or {}).get("name"),
            }
        )

    out = []
    for site in sites.values():
        site["launches"].sort(key=lambda l: l.get("net") or "9999")
        site["count"] = len(site["launches"])
        site["next_net"] = site["launches"][0]["net"] if site["launches"] else None
        out.append(site)

    out.sort(key=lambda s: (-s["count"], s["name"]))
    return out


def build_operators(upcoming, previous):
    """Roll launches up per launch service provider.

    The detailed launch payload already embeds full agency statistics, so the
    operator table costs no extra API requests -- it is a regroup of data we
    have already paid for.
    """
    by_id = {}
    for launch in upcoming:
        p = launch.get("provider")
        if not p or p.get("id") is None:
            continue
        op = by_id.setdefault(p["id"], {**p, "upcoming": [], "recent": []})
        op["upcoming"].append(
            {
                "id": launch["id"],
                "name": launch["name"],
                "net": launch["net"],
                "status": launch["status"]["abbrev"],
                "mission": launch["mission"]["name"],
            }
        )

    for launch in previous:
        p = launch.get("provider")
        if not p or p.get("id") is None:
            continue
        op = by_id.setdefault(p["id"], {**p, "upcoming": [], "recent": []})
        op["recent"].append(
            {
                "id": launch["id"],
                "name": launch["name"],
                "net": launch["net"],
                "status": launch["status"]["abbrev"],
                "success": launch["status"].get("id") == 3,
            }
        )

    ops = list(by_id.values())
    for op in ops:
        total = op.get("total_launch_count") or 0
        ok = op.get("successful_launches") or 0
        op["success_rate"] = round(100.0 * ok / total, 1) if total else None
        op["next_net"] = op["upcoming"][0]["net"] if op["upcoming"] else None

    # Operators with something on the manifest come first, then by fleet size.
    ops.sort(key=lambda o: (0 if o["upcoming"] else 1, -(len(o["upcoming"])), -(o.get("total_launch_count") or 0)))
    return ops
