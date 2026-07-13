#!/usr/bin/env python3
"""build_terrain.py - one-off terrain converter for Parking Mania Nuyina.

Fetches OSM data for the Hobart bounding box from the Overpass API,
stitches the coastline into closed land polygons, projects everything
to a local metric grid (y-down, origin at the NW bbox corner), simplifies
with Douglas-Peucker, and writes ../hobart-terrain.js as a classic script
defining the global HOBART_TERRAIN (works over file://).

Usage:  python build_terrain.py [--refresh]
  --refresh   ignore overpass-cache.json and re-fetch from the API

Python 3 stdlib only. Data (c) OpenStreetMap contributors, ODbL.
"""

import json
import math
import os
import sys
import urllib.request
from datetime import date

# ---------------- configuration ----------------

BBOX = {"south": -42.947382, "west": 147.294690, "north": -42.832922, "east": 147.430067}
ORIGIN = {"lat": BBOX["north"], "lon": BBOX["west"]}
M_PER_DEG_LAT = 110574.0
M_PER_DEG_LON = 111320.0 * math.cos(math.radians(ORIGIN["lat"]))
DP_TOLERANCE = 1.5          # m, Douglas-Peucker
WATER_MAX_AREA = 0.5e6      # m^2, keep only small water polys (docks)
BRIDGE_MIN_DIAG = 300.0     # m, ignore small road bridges
AROUND_M = 600              # m, decoration layers only this close to the coastline
BUILDING_MIN_AREA = 60.0    # m^2, drop garden sheds / garages
ROAD_WIDTHS = {             # rendered stroke width in meters, by highway class
    "motorway": 16, "trunk": 14, "primary": 12, "secondary": 10, "tertiary": 9,
    "motorway_link": 8, "trunk_link": 8, "primary_link": 7, "secondary_link": 7,
    "tertiary_link": 7, "residential": 7, "unclassified": 7, "service": 4,
}
GREEN_TAGS = {
    "landuse": {"grass", "forest", "meadow", "recreation_ground", "village_green", "cemetery"},
    "leisure": {"park", "garden", "pitch", "golf_course", "playground"},
    "natural": {"wood", "scrub", "grassland", "heath"},
}
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(HERE, "overpass-cache.json")
OUT_FILE = os.path.join(HERE, "..", "hobart-terrain.js")
ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
# overpass-api.de returns Apache 406 for the default Python-urllib agent
USER_AGENT = "parking-mania-nuyina-terrain-builder/1.0 (hobby game)"

QUERY = f"""[out:json][timeout:240][bbox:{BBOX['south']},{BBOX['west']},{BBOX['north']},{BBOX['east']}];
way["natural"="coastline"]->.coast;
(
  .coast;
  way["man_made"="pier"];
  way["man_made"="breakwater"];
  way["man_made"="quay"];
  way["natural"="water"];
  relation["natural"="water"];
  way["man_made"="bridge"];
  way["highway"]["bridge"];
  node["man_made"="bridge_support"];
  way["man_made"="bridge_support"];
  way["building"](around.coast:{AROUND_M});
  way["man_made"="storage_tank"](around.coast:{AROUND_M});
  way["highway"~"^(motorway|trunk|primary|secondary|tertiary|residential|unclassified|service|motorway_link|trunk_link|primary_link|secondary_link|tertiary_link)$"](around.coast:{AROUND_M});
  way["landuse"~"^(grass|forest|meadow|recreation_ground|village_green|cemetery)$"](around.coast:{AROUND_M});
  way["leisure"~"^(park|garden|pitch|golf_course|playground)$"](around.coast:{AROUND_M});
  way["natural"~"^(wood|scrub|grassland|heath)$"](around.coast:{AROUND_M});
);
out body;
>;
out skel qt;"""

# Hand patches for broken OSM data (chains ending mid-bbox). Keyed by way id.
FIXUPS = {}

# ---------------- projection ----------------


def proj(lat, lon):
    return ((lon - ORIGIN["lon"]) * M_PER_DEG_LON, (ORIGIN["lat"] - lat) * M_PER_DEG_LAT)


def unproj(x, y):
    return (ORIGIN["lat"] - y / M_PER_DEG_LAT, ORIGIN["lon"] + x / M_PER_DEG_LON)


WORLD_W = proj(BBOX["north"], BBOX["east"])[0]
WORLD_H = proj(BBOX["south"], BBOX["west"])[1]

# ---------------- fetch ----------------


def fetch_osm(refresh):
    if not refresh and os.path.exists(CACHE_FILE):
        print("Using cached Overpass response:", CACHE_FILE)
        with open(CACHE_FILE, encoding="utf8") as f:
            return json.load(f)
    body = ("data=" + urllib.parse.quote(QUERY)).encode()
    for url in ENDPOINTS:
        try:
            print("Fetching from", url, "...")
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded",
                         "User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=240) as res:
                data = json.load(res)
            with open(CACHE_FILE, "w", encoding="utf8") as f:
                json.dump(data, f)
            print("  OK,", len(data["elements"]), "elements, cached to", CACHE_FILE)
            return data
        except Exception as err:  # noqa: BLE001 - report and try mirror
            print("  fetch failed:", err)
    raise RuntimeError("All Overpass endpoints failed")

# ---------------- geometry helpers ----------------


def poly_area(pts):
    s = 0.0
    n = len(pts)
    for i in range(n):
        ax, ay = pts[i]
        bx, by = pts[(i + 1) % n]
        s += ax * by - bx * ay
    return abs(s / 2)


def poly_bbox(pts):
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return [min(xs), min(ys), max(xs), max(ys)]


def point_in_poly(pts, p):
    inside = False
    px, py = p
    j = len(pts) - 1
    for i in range(len(pts)):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if (yi > py) != (yj > py) and px < (xj - xi) * (py - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def seg_dist(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    len2 = dx * dx + dy * dy
    t = 0.0 if len2 == 0 else ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / len2
    t = max(0.0, min(1.0, t))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


def douglas_peucker(pts, tol):
    """Iterative DP: keeps endpoints, removes points within tol of chords."""
    n = len(pts)
    if n <= 2:
        return list(pts)
    keep = [False] * n
    keep[0] = keep[n - 1] = True
    stack = [(0, n - 1)]
    while stack:
        lo, hi = stack.pop()
        max_d, max_i = -1.0, lo
        a, b = pts[lo], pts[hi]
        for i in range(lo + 1, hi):
            d = seg_dist(pts[i], a, b)
            if d > max_d:
                max_d, max_i = d, i
        if max_d > tol:
            keep[max_i] = True
            stack.append((lo, max_i))
            stack.append((max_i, hi))
    return [p for p, k in zip(pts, keep) if k]


def simplify_ring(pts, tol):
    """DP on the open vertex list of a closed ring."""
    if len(pts) < 4:
        return list(pts)
    closed = list(pts) + [pts[0]]
    simp = douglas_peucker(closed, tol)
    return simp[:-1]

# ---------------- stitching ----------------


def stitch_chains(way_list, allow_reverse=True):
    """way_list: [{'id', 'nodes'}] -> chains of node ids.

    Coastline calls this with allow_reverse=False: reversing a coastline way
    would flip its land-on-left orientation, so consistently-oriented OSM data
    never needs it.
    """
    pool = [{"id": w["id"], "nodes": list(w["nodes"]), "used": False} for w in way_list]
    chains = []
    for start in pool:
        if start["used"]:
            continue
        start["used"] = True
        nodes = list(start["nodes"])
        ids = [start["id"]]
        extended = True
        while extended and nodes[0] != nodes[-1]:
            extended = False
            for w in pool:
                if w["used"]:
                    continue
                wn = w["nodes"]
                if wn[0] == nodes[-1]:
                    nodes += wn[1:]
                elif wn[-1] == nodes[0]:
                    nodes = wn[:-1] + nodes
                elif allow_reverse and wn[-1] == nodes[-1]:
                    nodes += list(reversed(wn[:-1]))
                elif allow_reverse and wn[0] == nodes[0]:
                    nodes = list(reversed(wn[1:])) + nodes
                else:
                    continue
                ids.append(w["id"])
                w["used"] = True
                extended = True
        chains.append({"nodes": nodes, "ids": ids, "closed": nodes[0] == nodes[-1]})
    return chains

# ---------------- bbox clipping (Liang-Barsky per segment) ----------------

EDGE_EPS = 0.01  # m, snap-to-boundary tolerance


def snap_to_bbox(p):
    x, y = p
    if abs(x) < EDGE_EPS:
        x = 0.0
    if abs(x - WORLD_W) < EDGE_EPS:
        x = WORLD_W
    if abs(y) < EDGE_EPS:
        y = 0.0
    if abs(y - WORLD_H) < EDGE_EPS:
        y = WORLD_H
    return (x, y)


def clip_segment(p, q):
    dx, dy = q[0] - p[0], q[1] - p[1]
    t0, t1 = 0.0, 1.0
    for den, num in ((-dx, p[0]), (dx, WORLD_W - p[0]), (-dy, p[1]), (dy, WORLD_H - p[1])):
        if den == 0:
            if num < 0:
                return None
            continue
        t = num / den
        if den < 0:
            if t > t1:
                return None
            if t > t0:
                t0 = t
        else:
            if t < t0:
                return None
            if t < t1:
                t1 = t
    return (t0, t1)


def lerp(p, q, t):
    return (p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t)


def clip_chain(pts):
    """Clip a projected polyline to the bbox -> list of fragments."""
    frags = []
    cur = None

    def push_point(p):
        nonlocal cur
        p = snap_to_bbox(p)
        if cur is None:
            cur = [p]
        elif (cur[-1][0] - p[0]) ** 2 + (cur[-1][1] - p[1]) ** 2 > 1e-8:
            cur.append(p)

    def close_frag():
        nonlocal cur
        if cur and len(cur) >= 2:
            frags.append(cur)
        cur = None

    for i in range(len(pts) - 1):
        p, q = pts[i], pts[i + 1]
        c = clip_segment(p, q)
        if c is None:
            close_frag()
            continue
        t0, t1 = c
        if t0 > 0:
            close_frag()  # entering through the boundary
        push_point(lerp(p, q, t0) if t0 > 0 else p)
        push_point(lerp(p, q, t1) if t1 < 1 else q)
        if t1 < 1:
            close_frag()  # exited through the boundary
    close_frag()
    return frags

def clip_poly_rect(pts):
    """Sutherland-Hodgman clip of a polygon to the world rect [0,W]x[0,H]."""
    def clip(poly, inside, cross):
        out = []
        for i in range(len(poly)):
            cur, prv = poly[i], poly[i - 1]
            if inside(cur) != inside(prv):
                out.append(cross(prv, cur))
            if inside(cur):
                out.append(cur)
        return out

    def x_cross(lim):
        return lambda a, b: lerp(a, b, (lim - a[0]) / (b[0] - a[0]))

    def y_cross(lim):
        return lambda a, b: lerp(a, b, (lim - a[1]) / (b[1] - a[1]))

    poly = list(pts)
    for inside, cross in ((lambda p: p[0] >= 0.0, x_cross(0.0)),
                          (lambda p: p[0] <= WORLD_W, x_cross(WORLD_W)),
                          (lambda p: p[1] >= 0.0, y_cross(0.0)),
                          (lambda p: p[1] <= WORLD_H, y_cross(WORLD_H))):
        if not poly:
            return []
        poly = clip(poly, inside, cross)
    return poly

# ---------------- close fragments against the bbox ----------------
# Perimeter parameter t walks: N edge E->W, W edge N->S, S edge W->E,
# E edge S->N (keeps land on the left, matching OSM coastline direction).
# Corners: NW t=W, SW t=W+H, SE t=2W+H, NE t=2W+2H (=0).

PERIM = 2 * (WORLD_W + WORLD_H)


def peri_t(p):
    x, y = p
    if abs(y) < EDGE_EPS:
        return WORLD_W - x
    if abs(x) < EDGE_EPS:
        return WORLD_W + y
    if abs(y - WORLD_H) < EDGE_EPS:
        return WORLD_W + WORLD_H + x
    if abs(x - WORLD_W) < EDGE_EPS:
        return 2 * WORLD_W + WORLD_H + (WORLD_H - y)
    return None


def cyc(d):
    return d % PERIM


def close_against_bbox(fragments):
    corners = [
        {"t": WORLD_W, "p": (0.0, 0.0)},                       # NW
        {"t": WORLD_W + WORLD_H, "p": (0.0, WORLD_H)},         # SW
        {"t": 2 * WORLD_W + WORLD_H, "p": (WORLD_W, WORLD_H)}, # SE
        {"t": PERIM, "p": (WORLD_W, 0.0)},                     # NE
    ]
    frags = [{"pts": pts, "tIn": peri_t(pts[0]), "tOut": peri_t(pts[-1]), "used": False}
             for pts in fragments]
    bad = [f for f in frags if f["tIn"] is None or f["tOut"] is None]
    if bad:
        for f in bad:
            print("  BROKEN: fragment endpoint not on bbox boundary near",
                  unproj(*f["pts"][0]), "->", unproj(*f["pts"][-1]))
        raise RuntimeError(f"{len(bad)} coastline fragment(s) end mid-bbox - see FIXUPS")
    polys = []
    for start in frags:
        if start["used"]:
            continue
        start["used"] = True
        poly = list(start["pts"])
        t = start["tOut"]
        for guard in range(len(frags) + 5):
            if guard > len(frags) + 3:
                raise RuntimeError("bbox closure did not terminate")
            best, best_d = None, math.inf
            for f in frags:
                if f["used"] and f is not start:
                    continue
                d = cyc(f["tIn"] - t)
                if d < best_d:
                    best_d, best = d, f
            between = sorted(
                (c for c in ({"t": c["t"], "p": c["p"], "d": cyc(c["t"] - t)} for c in corners)
                 if EDGE_EPS < c["d"] < best_d - EDGE_EPS),
                key=lambda c: c["d"])
            for c in between:
                poly.append(c["p"])
            if best is start:
                break
            best["used"] = True
            poly.extend(best["pts"])
            t = best["tOut"]
        polys.append(poly)
    return polys

# ---------------- open-way buffering (linear piers) ----------------


def buffer_polyline(pts, width):
    hw = width / 2
    left, right = [], []
    n = len(pts)
    for i in range(n):
        prev = pts[max(0, i - 1)]
        nxt = pts[min(n - 1, i + 1)]
        nx, ny = -(nxt[1] - prev[1]), nxt[0] - prev[0]
        ln = math.hypot(nx, ny) or 1.0
        nx, ny = nx / ln, ny / ln
        left.append((pts[i][0] + nx * hw, pts[i][1] + ny * hw))
        right.append((pts[i][0] - nx * hw, pts[i][1] - ny * hw))
    return left + list(reversed(right))

# ---------------- main ----------------


def main():
    refresh = "--refresh" in sys.argv
    osm = fetch_osm(refresh)

    nodes = {}      # id -> (x, y) projected
    node_tags = {}
    ways = {}       # id -> {'id', 'nodes', 'tags'}
    rels = []
    for el in osm["elements"]:
        if el["type"] == "node":
            nodes[el["id"]] = proj(el["lat"], el["lon"])
            if el.get("tags"):
                node_tags[el["id"]] = el["tags"]
        elif el["type"] == "way":
            ways[el["id"]] = {"id": el["id"], "nodes": el["nodes"], "tags": el.get("tags", {})}
        elif el["type"] == "relation":
            rels.append(el)

    def way_pts(w):
        return [nodes[i] for i in w["nodes"] if i in nodes]

    def by_tag(k, v=None):
        return [w for w in ways.values()
                if (w["tags"].get(k) == v if v else k in w["tags"])]

    stats = {}

    # ---- coastline -> land polygons ----
    coast_ways = [{"id": w["id"], "nodes": FIXUPS.get(w["id"], w["nodes"])}
                  for w in by_tag("natural", "coastline")]
    stats["coastline ways"] = len(coast_ways)
    chains = stitch_chains(coast_ways, allow_reverse=False)
    stats["coastline chains"] = len(chains)

    land = []
    open_fragments = []
    for chain in chains:
        pts = [nodes[i] for i in chain["nodes"] if i in nodes]
        if chain["closed"]:
            pts = pts[:-1]
        inside = all(-EDGE_EPS <= x <= WORLD_W + EDGE_EPS and -EDGE_EPS <= y <= WORLD_H + EDGE_EPS
                     for x, y in pts)
        if chain["closed"] and inside:
            land.append(pts)  # island / ring fully inside
            continue
        closed_pts = pts + [pts[0]] if chain["closed"] else pts
        open_fragments.extend(clip_chain(closed_pts))
    stats["clipped fragments"] = len(open_fragments)
    land.extend(close_against_bbox(open_fragments))
    stats["land polygons"] = len(land)

    # ---- piers / breakwaters / quays ----
    piers = []
    pier_ways = [w for w in ways.values()
                 if w["tags"].get("man_made") in ("pier", "breakwater", "quay")]
    stats["pier/quay/breakwater ways"] = len(pier_ways)
    for w in pier_ways:
        pts = way_pts(w)
        if len(pts) < 2:
            continue
        if w["nodes"][0] == w["nodes"][-1]:
            pts = pts[:-1]
            if len(pts) < 3:
                continue
        else:
            try:
                width = float(w["tags"].get("width", ""))
            except ValueError:
                width = 8.0
            pts = buffer_polyline(pts, width or 8.0)
            print("  buffered open", w["tags"]["man_made"], w["id"],
                  w["tags"].get("name", ""), "width", width or 8.0)
        piers.append({"name": w["tags"].get("name") or f"{w['tags']['man_made']}-{w['id']}",
                      "pts": pts})

    # ---- small water polygons (docks) ----
    water = []

    def add_water_ring(pts, name):
        if len(pts) >= 3 and poly_area(pts) <= WATER_MAX_AREA:
            water.append({"name": name, "pts": pts})

    for w in by_tag("natural", "water"):
        if w["tags"].get("water") == "river":
            continue
        if w["nodes"][0] != w["nodes"][-1]:
            continue
        add_water_ring(way_pts(w)[:-1], w["tags"].get("name") or f"water-{w['id']}")
    for rel in rels:
        tags = rel.get("tags", {})
        if tags.get("natural") != "water" or tags.get("water") == "river":
            continue
        outers = [ways[m["ref"]] for m in rel.get("members", [])
                  if m["type"] == "way" and m.get("role") in ("outer", "") and m["ref"] in ways]
        for ring in stitch_chains(outers):
            if not ring["closed"]:
                print("  skipped unstitchable water relation", rel["id"], tags.get("name", ""))
                continue
            add_water_ring([nodes[i] for i in ring["nodes"][:-1] if i in nodes],
                           tags.get("name") or f"water-rel-{rel['id']}")
    stats["water polygons kept"] = len(water)

    # ---- bridge deck, centerline, supports ----
    bridge_deck = []
    for w in by_tag("man_made", "bridge"):
        if w["nodes"][0] != w["nodes"][-1]:
            continue
        pts = way_pts(w)[:-1]
        bb = poly_bbox(pts)
        if math.hypot(bb[2] - bb[0], bb[3] - bb[1]) < BRIDGE_MIN_DIAG:
            continue
        bridge_deck.append({"name": w["tags"].get("name") or f"bridge-{w['id']}", "pts": pts})
    stats["bridge deck polygons"] = len(bridge_deck)

    hwy_bridge_ways = [w for w in ways.values() if "highway" in w["tags"] and "bridge" in w["tags"]]
    bridge_centerline = []
    best_len = 0.0
    for c in stitch_chains(hwy_bridge_ways):
        pts = [nodes[i] for i in c["nodes"] if i in nodes]
        length = sum(math.dist(pts[i - 1], pts[i]) for i in range(1, len(pts)))
        if length > best_len:
            best_len, bridge_centerline = length, pts
    stats["bridge centerline length m"] = round(best_len)
    if not bridge_deck and len(bridge_centerline) >= 2:
        print("  no man_made=bridge outline; buffering centerline +/-9 m")
        bridge_deck.append({"name": "bridge-buffered",
                            "pts": buffer_polyline(bridge_centerline, 18.0)})

    bridge_supports = []
    for w in by_tag("man_made", "bridge_support"):
        pts = way_pts(w)
        if w["nodes"][0] == w["nodes"][-1]:
            pts = pts[:-1]
        bridge_supports.append({"name": f"support-{w['id']}", "pts": pts})
    for nid, tags in node_tags.items():
        if tags.get("man_made") == "bridge_support":
            bridge_supports.append({"name": f"support-node-{nid}", "pts": [nodes[nid]]})
    stats["bridge supports"] = len(bridge_supports)

    # ---- decoration layers (visual only, not collidable) ----
    buildings = []
    for w in ways.values():
        t = w["tags"]
        if "building" not in t and t.get("man_made") != "storage_tank":
            continue
        if w["nodes"][0] != w["nodes"][-1]:
            continue
        pts = clip_poly_rect(way_pts(w)[:-1])
        if len(pts) >= 3 and poly_area(pts) >= BUILDING_MIN_AREA:
            buildings.append({"name": t.get("name") or f"bldg-{w['id']}", "pts": pts})
    stats["buildings kept"] = len(buildings)

    vegetation = []
    for w in ways.values():
        t = w["tags"]
        if not any(t.get(k) in v for k, v in GREEN_TAGS.items()):
            continue
        if "building" in t or w["nodes"][0] != w["nodes"][-1]:
            continue
        pts = clip_poly_rect(way_pts(w)[:-1])
        if len(pts) >= 3:
            vegetation.append({"name": t.get("name") or f"veg-{w['id']}", "pts": pts})
    stats["vegetation polygons"] = len(vegetation)

    roads = []
    for w in by_tag("highway"):
        t = w["tags"]
        width = ROAD_WIDTHS.get(t.get("highway"))
        if width is None or "bridge" in t or t.get("tunnel"):
            continue  # the Tasman Bridge deck is its own layer
        if t.get("service") in ("parking_aisle", "driveway"):
            continue
        for frag in clip_chain(way_pts(w)):
            roads.append({"name": t.get("name") or f"road-{w['id']}", "w": width, "pts": frag})
    stats["road segments"] = len(roads)

    # ---- simplify + finalize ----
    counts = {"raw": 0, "simplified": 0}

    def finalize(items, is_ring, tol=DP_TOLERANCE):
        out_list = []
        for item in items:
            src = item["pts"]
            counts["raw"] += len(src)
            pts = simplify_ring(src, tol) if is_ring else src
            pts = [[round(x, 1), round(y, 1)] for x, y in pts]
            counts["simplified"] += len(pts)
            out_list.append({"name": item["name"], "pts": pts,
                             "bbox": [round(v, 1) for v in poly_bbox(pts)]})
        return out_list

    out = {
        "meta": {
            "bbox": BBOX,
            "origin": ORIGIN,
            "mPerDegLat": M_PER_DEG_LAT,
            "mPerDegLon": round(M_PER_DEG_LON, 1),
            "widthM": round(WORLD_W, 1),
            "heightM": round(WORLD_H, 1),
        },
        "land": finalize([{"name": f"land-{i}", "pts": pts} for i, pts in enumerate(land)], True),
        "piers": finalize(piers, True),
        "water": finalize(water, True),
        "vegetation": finalize(vegetation, True, 2.0),
        "buildings": finalize(buildings, True, 0.5),
        "bridgeDeck": finalize(bridge_deck, True),
        "bridgeSupports": [{"name": s["name"],
                            "pts": [[round(x, 1), round(y, 1)] for x, y in s["pts"]]}
                           for s in bridge_supports],
        "bridgeCenterline": [[round(x, 1), round(y, 1)] for x, y in bridge_centerline],
    }
    road_out = []
    for r in roads:
        counts["raw"] += len(r["pts"])
        pts = [[round(x, 1), round(y, 1)] for x, y in douglas_peucker(r["pts"], 1.0)]
        counts["simplified"] += len(pts)
        road_out.append({"name": r["name"], "w": r["w"], "pts": pts,
                         "bbox": [round(v, 1) for v in poly_bbox(pts)]})
    out["roads"] = road_out
    stats["vertices before DP"] = counts["raw"]
    stats["vertices after DP"] = counts["simplified"]

    # ---- sanity probes ----
    def land_hits(lat, lon):
        p = proj(lat, lon)
        return sum(1 for poly in out["land"] if point_in_poly(poly["pts"], p))

    water_hits = land_hits(-42.880, 147.360)  # mid-Derwent
    cbd_hits = land_hits(-42.882, 147.325)    # Hobart CBD
    print("probe mid-Derwent land hits:", water_hits, "(want 0)")
    print("probe Hobart CBD land hits:", cbd_hits, "(want 1)")
    if water_hits != 0 or cbd_hits != 1:
        raise RuntimeError("sanity probes failed - land/water inverted or stitching broken")

    # ---- emit ----
    def js(obj):
        return json.dumps(obj, separators=(",", ":"))

    lines = [
        f"// GENERATED by tools/build_terrain.py on {date.today().isoformat()} - do not edit by hand.",
        "// Regenerate with: python tools/build_terrain.py",
        "// Data (c) OpenStreetMap contributors, ODbL - openstreetmap.org/copyright",
        "var HOBART_TERRAIN = {",
        f"meta: {js(out['meta'])},",
    ]
    for key in ("land", "piers", "water", "vegetation", "buildings", "roads",
                "bridgeDeck", "bridgeSupports"):
        lines.append(f"{key}: [")
        lines.extend(js(poly) + "," for poly in out[key])
        lines.append("],")
    lines.append(f"bridgeCenterline: {js(out['bridgeCenterline'])}")
    lines.append("};")
    with open(OUT_FILE, "w", encoding="utf8", newline="\n") as f:
        f.write("\n".join(lines))

    print("\n--- stats ---")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    print("wrote", os.path.normpath(OUT_FILE),
          f"({os.path.getsize(OUT_FILE) // 1024} KB)")


if __name__ == "__main__":
    main()
