# Mission Control

A live dashboard for orbital spaceflight: what's launching next, who's off the
planet right now, and the agencies and companies flying them — over a world map
with a real-time day/night terminator.

### → **[mission.mhpserver.cc](https://mission.mhpserver.cc)** — live site

![Launch schedule](docs/screenshots/launch-schedule.png)

Data comes from [The Space Devs' Launch Library 2](https://thespacedevs.com/llapi).
No API key, no database, no accounts — clone it and it runs.

---

## What it does

### Launch schedule

The next launch off the pad gets the hero treatment: a live countdown to T‑0,
the pad, the orbit, and the live video beside it. Below it, the next 40 launches
on the manifest, searchable by mission, rocket or pad, and filterable by
operator.

**Launch dates lie, and the dashboard says so.** LL2 publishes a precision field
alongside every launch time; anything coarser than "to the hour" is an estimate
that can move by days, and those launches are labelled **Date unconfirmed**
rather than being shown as if T‑0 were settled. The **Confirmed / Estimated**
filter exists so you can look at only the launches that are actually going to
happen when they say.

### Live video

The panel beside the countdown carries whatever coverage exists for that launch.
When there is an embeddable stream it plays in place; when the only coverage is
somewhere that cannot be embedded — SpaceX's own webcasts live on X — it opens
there instead; and when nothing has been published yet the panel says so and
waits, rather than pretending a dead link is a stream.

It is a **facade, not an embed**: what loads with the page is a poster served
through the same image proxy as everything else here. The stream host is
contacted only after the play button is pressed, and then through
`youtube-nocookie.com`.

Launches often carry several streams — an official one plus re-streams from
NASASpaceflight or Space Affairs. The panel plays the best one it *can* play,
names its publisher, and links the rest, because a re-stream badged as the
operator's own feed would be a lie the layout tells for free. (LL2's type name
for those is `Unofficial Webcast`, which contains the string `official` — worth
knowing before writing that check.)

### Launch map

![Launch map](docs/screenshots/launch-map.png)

Every site with a launch booked, plotted where it actually sits, with marker
size scaled to how many launches it has scheduled. The shaded band is the
Earth's night side, computed from the sun's real subsolar point and redrawn
every minute. Select a site for its manifest.

There is **no tile server** — the coastlines are Natural Earth outlines
simplified and pre-projected into a single SVG path at build time, so the map
is a 60 KB static asset and the browser never talks to a third party to draw it.

### Active missions

![Active missions](docs/screenshots/active-missions.png)

Everyone currently in space, with who they fly for and when they went up; every
spacecraft in flight, crewed and cargo, with time on orbit and time docked; and
the stations they're flying to.

### Operators

![Operators](docs/screenshots/operators.png)

The agencies and companies flying the manifest, with their all-time record —
launches, failures, current success streak, and what each has coming up. This
view costs zero extra API requests: the detailed launch feed already carries
full provider statistics, so it's a regroup of data the dashboard has paid for.

---

## How it works

**A visitor request never reaches the upstream API.** LL2's public tier allows
roughly 15 requests an hour per IP, and that single constraint shaped the whole
backend:

- Five feeds on staggered TTLs — the launch schedule every 20 minutes, crew and
  spacecraft rosters hourly, station manifests every 6 hours.
- A background thread refreshes **at most one feed per 60-second tick**, so a
  cold start can't bunch five calls into one second.
- Every raw response is written to disk and reloaded on boot, so **restarting
  the service costs nothing** against the budget.
- A failing feed backs off exponentially on its own schedule, so one broken
  endpoint can't burn the budget the healthy ones depend on.

Steady state is about 6 requests an hour. A cold start, an API outage and a
rate-limit lockout all degrade the same way: the dashboard keeps serving the
last good copy and reports in the footer how old it is.

There is deliberately **no manual refresh endpoint** — it would be
unauthenticated, and even on a five-minute cooldown a caller hammering it would
add enough forced fetches to blow the hourly limit and take the data stale for
everyone.

**Images are proxied, not hotlinked.** `/api/img` fetches mission photos and
agency logos server-side against an exact-host allowlist (an open fetcher here
would be an SSRF hole straight into the LAN), caches them to disk, and re-serves
them same-origin — so no visitor IP is handed to a third-party CDN. A background
pre-warmer pulls every image a new payload references, because without it the
first visitor after a refresh eats ~75 cold misses and watches the logos trickle
in.

**The upstream feed needs filtering.** LL2's "currently in space" endpoints
include Starman — the mannequin in the Tesla Roadster — and the Roadster itself,
which would otherwise put 11 people in space instead of 10 and count a lump of
ballast as a mission in flight. Placeholder strings (`"Unknown"`, `"TBD"`,
`"Details TBD."`) and zero-length durations are collapsed to nothing at the
normaliser rather than rendered, so the page never says "unknown mission · to
Unknown".

**The frontend is one HTML file, one stylesheet and one script.** No framework,
no build step, no bundler. It polls a single normalised JSON document once a
minute and renders four views from it.

## Layout

```
app.py                     Flask app — /api/data, /api/health, image proxy
ll2.py                     Launch Library 2 client: feed cache, refresher, normaliser
tools/build_world.py       generates www/assets/world.js from Natural Earth geometry
www/
  index.html               the whole page
  assets/app.js            views, countdowns, map rendering, terminator
  assets/style.css         dark-only design system
  assets/world.js          generated — pre-projected country outlines
deploy/                    nginx vhost, systemd unit, deploy script, runbook
```

## Running it

```bash
git clone https://github.com/mattpetosa/mission-dashboard.git
cd mission-dashboard
python3 -m venv venv && ./venv/bin/pip install flask gunicorn requests
./venv/bin/python app.py       # http://127.0.0.1:8793
```

Then serve `www/` on the same origin with `/api/` proxied to that port — see
[`deploy/README.md`](deploy/README.md) for the nginx vhost, the systemd unit and
the deploy script.

**The first few minutes are cold.** The cache starts empty and the refresher
fills one feed per minute by design, so give it five minutes before judging it.
For the same reason, don't clear `cache/` casually — doing so mid-day can spend
the hour's request budget and leave the dashboard empty until it resets.

## Data and credits

Launch, crew, spacecraft and agency data from
[The Space Devs — Launch Library 2](https://thespacedevs.com/llapi), used under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Coastline geometry
from [Natural Earth](https://www.naturalearthdata.com/) (public domain).
Mission imagery belongs to the agencies and operators credited on each item.
