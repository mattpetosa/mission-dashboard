"""Bake Natural Earth 110m country outlines into one compact SVG path.

Run once at build time; the output is committed as a static asset. Doing this
offline rather than loading map tiles at runtime keeps the dashboard
self-contained and means a visitor's browser never talks to a tile server.

Projection is plate carree (equirectangular): x = lon, y = -lat, linearly
scaled. That is the projection the day/night terminator and the pad markers in
app.js assume, so all three stay in agreement by construction.
"""

import json
import math

SRC = "ne_110m_admin_0_countries.geojson"  # from nvkelso/natural-earth-vector (public domain)
OUT = "/var/www/mission.mhpwebserver.com/assets/world.js"

W, H = 2000.0, 1000.0          # full -180..180 / 90..-90 canvas
TOLERANCE = 1.6                # RDP tolerance in projected units (~0.29 deg)
MIN_AREA = 6.0                 # drop islands smaller than this (projected units^2)


def project(lon, lat):
    return ((lon + 180.0) / 360.0 * W, (90.0 - lat) / 180.0 * H)


def rdp(points, eps):
    """Ramer-Douglas-Peucker, iterative to avoid recursion limits on big rings."""
    if len(points) < 3:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        start, end = stack.pop()
        ax, ay = points[start]
        bx, by = points[end]
        dx, dy = bx - ax, by - ay
        norm = math.hypot(dx, dy)
        best_i, best_d = -1, 0.0
        for i in range(start + 1, end):
            px, py = points[i]
            if norm == 0:
                d = math.hypot(px - ax, py - ay)
            else:
                d = abs(dy * px - dx * py + bx * ay - by * ax) / norm
            if d > best_d:
                best_i, best_d = i, d
        if best_d > eps and best_i != -1:
            keep[best_i] = True
            stack.append((start, best_i))
            stack.append((best_i, end))
    return [p for p, k in zip(points, keep) if k]


def ring_area(points):
    a = 0.0
    for i in range(len(points)):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % len(points)]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2.0


def rings_of(geom):
    t = geom["type"]
    if t == "Polygon":
        return geom["coordinates"]
    if t == "MultiPolygon":
        return [ring for poly in geom["coordinates"] for ring in poly]
    return []


def main():
    data = json.load(open(SRC))
    parts = []
    kept = dropped = 0

    for feat in data["features"]:
        geom = feat.get("geometry") or {}
        name = (feat.get("properties") or {}).get("NAME", "")
        # Antarctica is a huge low-information band in this projection and no
        # launch site is anywhere near it -- the viewBox crops it off anyway.
        if name == "Antarctica":
            continue
        for ring in rings_of(geom):
            pts = [project(lon, lat) for lon, lat, *_ in ring]
            if len(pts) < 4:
                continue
            if ring_area(pts) < MIN_AREA:
                dropped += 1
                continue
            simple = rdp(pts, TOLERANCE)
            if len(simple) < 3:
                dropped += 1
                continue
            kept += 1
            d = "M" + "L".join(f"{x:.1f} {y:.1f}" for x, y in simple) + "Z"
            parts.append(d)

    path = "".join(parts)
    js = (
        "/* Country outlines, Natural Earth 110m (public domain, CC0),\n"
        "   simplified and pre-projected to plate carree by build_world.py.\n"
        "   Baked in at build time so the map needs no tile server at runtime. */\n"
        "window.WORLD_PATH = %s;\n"
        "window.WORLD_SIZE = { w: %d, h: %d };\n"
    ) % (json.dumps(path), int(W), int(H))

    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(js)

    print(f"rings kept={kept} dropped={dropped}")
    print(f"path chars={len(path):,}  output={len(js):,} bytes -> {OUT}")


if __name__ == "__main__":
    main()
