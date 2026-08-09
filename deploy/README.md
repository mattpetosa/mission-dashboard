# Deploying mission-dashboard

Upcoming launches and active crewed missions from Launch Library 2, over a
baked SVG world map with a live day/night terminator.

| What | Where |
|---|---|
| Application | `/home/matt/mission-dashboard` |
| Served static files | `/var/www/mission.mhpserver.cc` (deploy target) |
| API | gunicorn on `127.0.0.1:8793`, proxied at `/api/` |
| Service | `mission-backend.service` |
| Cache | `cache/` — 30MB of API responses and proxied imagery, not in git |

**No credentials.** Launch Library 2 is a public API and needs no key, so
there is no `.env` and nothing secret to move. This is the only project here
that can be stood up from a clone alone.

## Standing it up

```bash
sudo apt install -y python3-venv nginx git
git clone <repo> /srv/mission-dashboard && cd /srv/mission-dashboard
python3 -m venv venv && ./venv/bin/pip install flask gunicorn requests

sudo cp deploy/systemd/mission-backend.service /etc/systemd/system/
sudo cp deploy/nginx/ssl-params.conf /etc/nginx/snippets/
sudo cp deploy/nginx/mission.mhpserver.cc.conf /etc/nginx/sites-available/
sudo ln -s /etc/nginx/sites-available/mission.mhpserver.cc.conf /etc/nginx/sites-enabled/

sudo systemctl daemon-reload && sudo systemctl enable --now mission-backend
sudo nginx -t && sudo systemctl reload nginx
./deploy/deploy.sh --static
```

The unit hardcodes `/home/matt/mission-dashboard`; edit it before enabling.

## The rate limit shapes everything

Launch Library 2 allows very few requests per hour for anonymous callers, and
`cache/` exists entirely to stay inside it. Two consequences:

- **A fresh box starts cold.** The first page load is slow and the pre-warmer
  needs a few minutes to fill in. This is expected, not a fault.
- **Don't clear `cache/` casually.** Doing so mid-day can exhaust the hour's
  budget and leave the dashboard empty until it resets.

The image proxy exists for the same reason plus CSP: mission imagery is
fetched server-side, cached, and re-served same-origin.

## The world map is a build artifact

`www/assets/world.js` is generated, not hand-written:

```bash
./venv/bin/python tools/build_world.py
```

It's committed because regenerating it needs the source geometry and it
changes almost never. If the map ever renders blank, check that file exists
before looking anywhere else.

## Data quality filters worth keeping

LL2's feed contains entries that are technically launches but nonsense on a
mission dashboard — Starman, mass simulators, and similar. `ll2.py` filters
them deliberately. If something odd starts appearing, that filter is where to
look, and the right fix is usually to widen it rather than special-case the
display.

## Backups

There is nothing to back up. No database, no credentials, no user state — the
cache is disposable and the rest is in git. Losing this box costs the time to
re-clone and re-warm the cache.
