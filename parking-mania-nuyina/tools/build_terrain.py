#!/usr/bin/env python3
"""build_terrain.py - OSM terrain converter for Parking Mania Nuyina.

Fetches OSM data for a configured region (one or more lat/lon rectangles)
from the Overpass API, stitches the coastline into closed land polygons,
projects everything to a local metric grid (y-down, origin at the NW corner
of the region's bounding box), simplifies with Douglas-Peucker, and writes
../<region>-terrain.js as a classic script defining a global (works over
file://).

Multi-rectangle regions: each rectangle is fetched and processed by the
single-rectangle pipeline (clip + close against that rectangle), then the
results are translated into region coordinates and concatenated. Land
polygons from overlapping rectangles simply overlap - the game fills land
without strokes and collides against any polygon, so no boolean union is
needed. Decorations are deduplicated by OSM id.

Usage:  python build_terrain.py [region] [--refresh]
  region      hobart (default), singapore, or heard
  --refresh   ignore the per-rectangle caches and re-fetch from the API

Python 3 stdlib only. Data (c) OpenStreetMap contributors, ODbL.
"""

import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import date

# ---------------- shared configuration ----------------

DP_TOLERANCE = 1.5          # m, Douglas-Peucker
WATER_MAX_AREA = 0.5e6      # m^2, keep only small water polys (docks)
BRIDGE_MIN_DIAG = 300.0     # m, ignore small road bridges
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
ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
# overpass-api.de returns Apache 406 for the default Python-urllib agent
USER_AGENT = "parking-mania-nuyina-terrain-builder/1.0 (hobby game)"
FETCH_TRIES = 2             # attempts per endpoint; Overpass 504s are transient
FETCH_BACKOFF = 15          # s between attempts on the same endpoint

# The King George VI graving dock at Sembawang (OSM way 252392151) is mapped
# as natural=water on top of the land polygon, but the game's water layer is
# visual only, so the basin must be carved out of the collidable land. The
# path runs from the west mouth corner down the west wall, around the head,
# and back up the east wall; the walls sit ~4 m outside the OSM water edge
# (clear width ~45 m for a 25.6 m beam) and the mouth ends are extended ~8 m
# seaward so they splice cleanly across the coastline at the dock mouth.
KGVI_CARVE = {
    "name": "King George VI graving dock",
    "path": [
        (1.4638531, 103.8237751),   # W mouth corner, seaward of the coastline
        (1.4611827, 103.8222823),   # SW corner (head)
        (1.4609841, 103.8226370),   # SE corner (head)
        (1.4636545, 103.8241298),   # E mouth corner, seaward of the coastline
    ],
    "probe_inside": (1.4624029, 103.8231973),  # dock centroid
    "max_snap": 30.0,   # m, path endpoints must be this close to a land ring
}

REGIONS = {
    "hobart": {
        "rects": [{"south": -42.947382, "west": 147.294690,
                   "north": -42.832922, "east": 147.430067}],
        "cache_files": ["overpass-cache.json"],
        "out_file": "../hobart-terrain.js",
        "global_name": "HOBART_TERRAIN",
        # Hand patches for broken OSM data (chains ending mid-bbox), way id -> node list.
        "fixups": {},
        "carves": [],
        "coast_joins": [],
        "join_eps": 0.0,            # m, 0 disables coordinate-joining of open chains
        "centerline_fallback": True,  # no bridge outline -> buffer the centerline (Tasman Bridge)
        "around_m": 600,            # decoration layers only this close to the coastline
        "building_min_area": 60.0,  # m^2, drop garden sheds / garages
        "probes": [("water", -42.880, 147.360, "mid-Derwent"),
                   ("land", -42.882, 147.325, "Hobart CBD")],
    },
    "singapore": {
        # Union of four overlapping rectangles forming the corridor from the
        # Johor Strait at Sembawang, past Punggol/Seletar, through Serangoon
        # Harbour and Pulau Ubin, out to Changi Bay.
        "rects": [
            {"south": 1.4222030060838888, "west": 103.77831029103474,
             "north": 1.487257912327686, "east": 103.90410839369433},
            {"south": 1.394027, "west": 103.888527,
             "north": 1.452211, "east": 103.946499},
            {"south": 1.3624150112632847, "west": 103.9258767356537,
             "north": 1.4382378390320683, "east": 104.12385407754422},
            {"south": 1.312017845643442, "west": 103.99255660427656,
             "north": 1.3866969080192064, "east": 104.1444767173245},
        ],
        "cache_files": ["overpass-cache-sg-0.json", "overpass-cache-sg-1.json",
                        "overpass-cache-sg-2.json", "overpass-cache-sg-3.json"],
        "out_file": "../singapore-terrain.js",
        "global_name": "SINGAPORE_TERRAIN",
        "fixups": {},
        "carves": [KGVI_CARVE],
        # OSM's coastline stops on either side of the reservoir dams; each
        # pair bridges (chain end) -> (chain start) with a straight segment
        # along the dam crest. Points are the printed break diagnostics.
        "coast_joins": [
            ((1.4254574, 103.8886735), (1.4257500, 103.8881975)),   # Punggol Barat channel dam
            ((1.4266361, 103.8691816), (1.4248110, 103.8672220)),   # Lower Seletar dam
            ((1.4179026, 103.9340319), (1.4178389, 103.9341981)),   # Coney Island west gap
            ((1.4149793, 103.9381315), (1.4149261, 103.9390356)),   # Serangoon East dam
            ((1.4175970, 103.8998362), (1.4183101, 103.8979293)),   # Sungei Punggol mouth dam
            ((1.3887320, 103.9727962), (1.3874870, 103.9728687)),   # Pasir Ris river mouth
            ((1.3815227, 104.0295767), (1.3808240, 104.0303110)),   # Changi Creek
            # Pulau Ubin's ring arrives in eleven pieces; these close it up
            ((1.4060531, 103.9506150), (1.4059252, 103.9507253)),
            ((1.4062569, 103.9525920), (1.4060936, 103.9531001)),
            ((1.4021696, 103.9569824), (1.4020768, 103.9570061)),
            ((1.4016514, 103.9574371), (1.4007873, 103.9577518)),   # Ubin town jetty
            ((1.4001256, 103.9623206), (1.4002443, 103.9626240)),
            ((1.4054596, 103.9739733), (1.4059045, 103.9739875)),
            ((1.4059845, 103.9743862), (1.4059972, 103.9744170)),
            ((1.4071904, 103.9764358), (1.4071424, 103.9764870)),
            ((1.4178923, 103.9750963), (1.4178511, 103.9747226)),
            ((1.4178388, 103.9742937), (1.4179728, 103.9742267)),
            ((1.4165755, 103.9592690), (1.4165605, 103.9590472)),
            ((1.3595258, 104.0552502), (1.3581053, 104.0569639)),   # Changi Bay reclamation
        ],
        "join_eps": 0.5,
        "centerline_fallback": False,  # coastal viaducts are not harbour bridges
        "around_m": 400,            # Singapore's coastal strip is dense; keep it tight
        "building_min_area": 120.0,
        "probes": [("water", 1.3525647, 104.0532788, "Changi anchorage (L7 start)"),
                   ("land", 1.410, 103.965, "Pulau Ubin"),
                   ("land", 1.398, 103.908, "Punggol"),
                   ("land", 1.480, 103.860, "Johor shore"),
                   ("water", 1.4624029, 103.8231973, "KGVI dock (carved)")],
    },
    "heard": {
        # North-west Heard Island: the offshore approach past Red Island and
        # the whole of Atlas Cove.  The compact rectangle keeps the generated
        # terrain light while leaving several kilometres around both gameplay
        # coordinates for collision and camera rendering.
        "rects": [{"south": -53.045, "west": 73.245,
                   "north": -52.935, "east": 73.425}],
        "cache_files": ["overpass-cache-heard.json"],
        "out_file": "../heard-terrain.js",
        "global_name": "HEARD_TERRAIN",
        "fixups": {},
        "carves": [],
        "coast_joins": [],
        "join_eps": 0.5,
        "centerline_fallback": False,
        "around_m": 600,
        "building_min_area": 20.0,
        "shallow_m": 200.0,
        "probes": [
            ("water", -52.97273, 73.26765, "Heard Island approach (L10 start)"),
            ("water", -53.021397, 73.383085, "Atlas Cove stationkeeping target"),
            ("land", -53.01909761, 73.39338781, "Atlas Cove station shore"),
        ],
    },
}

# ---------------- projection ----------------
# proj/unproj and the clippers work against one axis-aligned rectangle at a
# time (NW corner origin, y down). set_world() points them at a rectangle;
# the longitude scale is fixed once per region so that overlapping rectangles
# land on exactly the same region grid.

M_PER_DEG_LAT = 110574.0
M_PER_DEG_LON = None
ORIGIN = None
WORLD_W = 0.0
WORLD_H = 0.0
PERIM = 0.0


def set_lon_scale(origin_lat):
    global M_PER_DEG_LON
    M_PER_DEG_LON = 111320.0 * math.cos(math.radians(origin_lat))


def set_world(rect):
    global ORIGIN, WORLD_W, WORLD_H, PERIM
    ORIGIN = {"lat": rect["north"], "lon": rect["west"]}
    WORLD_W = (rect["east"] - rect["west"]) * M_PER_DEG_LON
    WORLD_H = (rect["north"] - rect["south"]) * M_PER_DEG_LAT
    PERIM = 2 * (WORLD_W + WORLD_H)


def proj(lat, lon):
    return ((lon - ORIGIN["lon"]) * M_PER_DEG_LON, (ORIGIN["lat"] - lat) * M_PER_DEG_LAT)


def unproj(x, y):
    return (ORIGIN["lat"] - y / M_PER_DEG_LAT, ORIGIN["lon"] + x / M_PER_DEG_LON)

# ---------------- fetch ----------------


def make_query(rect, around_m):
    return f"""[out:json][timeout:240][bbox:{rect['south']},{rect['west']},{rect['north']},{rect['east']}];
way["natural"="coastline"]->.coast;
(
  .coast;
  way["man_made"="pier"];
  way["man_made"="breakwater"];
  way["man_made"="quay"];
  way["natural"="water"];
  relation["natural"="water"];
  way["natural"="glacier"];
  relation["natural"="glacier"];
  way["man_made"="bridge"];
  way["highway"]["bridge"];
  node["man_made"="bridge_support"];
  way["man_made"="bridge_support"];
  way["building"](around.coast:{around_m});
  way["man_made"="storage_tank"](around.coast:{around_m});
  way["highway"~"^(motorway|trunk|primary|secondary|tertiary|residential|unclassified|service|motorway_link|trunk_link|primary_link|secondary_link|tertiary_link)$"](around.coast:{around_m});
  way["landuse"~"^(grass|forest|meadow|recreation_ground|village_green|cemetery)$"](around.coast:{around_m});
  way["leisure"~"^(park|garden|pitch|golf_course|playground)$"](around.coast:{around_m});
  way["natural"~"^(wood|scrub|grassland|heath)$"](around.coast:{around_m});
);
out body;
>;
out skel qt;"""


def fetch_osm(query, cache_file, refresh):
    if not refresh and os.path.exists(cache_file):
        print("Using cached Overpass response:", cache_file)
        with open(cache_file, encoding="utf8") as f:
            return json.load(f)
    body = ("data=" + urllib.parse.quote(query)).encode()
    for url in ENDPOINTS:
        for attempt in range(1, FETCH_TRIES + 1):
            try:
                print(f"Fetching from {url} (attempt {attempt}) ...")
                req = urllib.request.Request(
                    url, data=body,
                    headers={"Content-Type": "application/x-www-form-urlencoded",
                             "User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=240) as res:
                    data = json.load(res)
                with open(cache_file, "w", encoding="utf8") as f:
                    json.dump(data, f)
                print("  OK,", len(data["elements"]), "elements, cached to", cache_file)
                return data
            except Exception as err:  # noqa: BLE001 - report and try again / next mirror
                print("  fetch failed:", err)
                if attempt < FETCH_TRIES:
                    time.sleep(FETCH_BACKOFF)
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


def coord_join(chain_objs, eps):
    """Join open coastline chains whose endpoints coincide within eps meters.

    Fixes the most common OSM breakage (duplicated nodes at way junctions)
    without hand fixups. Orientation is never reversed (land-on-left). Chains
    whose own ends meet within eps are marked closed.
    """
    if eps <= 0:
        return chain_objs
    out = [c for c in chain_objs if c["closed"]]
    open_chains = [c for c in chain_objs if not c["closed"]]
    changed = True
    while changed:
        changed = False
        for i, ci in enumerate(open_chains):
            if ci is None:
                continue
            for j, cj in enumerate(open_chains):
                if i == j or cj is None:
                    continue
                gap = math.dist(ci["pts"][-1], cj["pts"][0])
                if gap <= eps:
                    print(f"  joined coastline chains near {unproj(*ci['pts'][-1])}"
                          f" (gap {gap:.2f} m)")
                    ci["pts"] = ci["pts"] + cj["pts"]
                    open_chains[j] = None
                    changed = True
                    break
            if changed:
                break
    for c in open_chains:
        if c is None:
            continue
        if len(c["pts"]) >= 4 and math.dist(c["pts"][0], c["pts"][-1]) <= eps:
            print(f"  closed coastline chain near {unproj(*c['pts'][0])}")
            c["closed"] = True
        out.append(c)
    return out

def apply_coast_joins(chain_objs, region, tol=20.0):
    """Bridge hand-listed coastline gaps: each pair (A, B) joins the open
    chain ending near A to the open chain starting near B with a straight
    segment (a reservoir dam crest, say). If only one side exists in this
    rectangle (the gap straddles its boundary), the chain is extended to the
    far point instead so it clips cleanly at the rectangle edge."""
    for a_ll, b_ll in region["coast_joins"]:
        a, b = proj(*a_ll), proj(*b_ll)
        opens = [c for c in chain_objs if not c["closed"]]
        end_c = min(opens, key=lambda c: math.dist(c["pts"][-1], a), default=None)
        if end_c is not None and math.dist(end_c["pts"][-1], a) > tol:
            end_c = None
        start_c = min(opens, key=lambda c: math.dist(c["pts"][0], b), default=None)
        if start_c is not None and math.dist(start_c["pts"][0], b) > tol:
            start_c = None
        if end_c and start_c and end_c is start_c:
            print(f"  coast join: closed a ring across {a_ll} -> {b_ll}")
            end_c["closed"] = True
        elif end_c and start_c:
            print(f"  coast join: bridged {a_ll} -> {b_ll}")
            end_c["pts"] = end_c["pts"] + start_c["pts"]
            chain_objs.remove(start_c)
        elif end_c:
            print(f"  coast join: extended chain end to {b_ll}")
            end_c["pts"] = end_c["pts"] + [b]
        elif start_c:
            print(f"  coast join: extended chain start to {a_ll}")
            start_c["pts"] = [a] + start_c["pts"]

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


def offset_ring(pts, distance_m):
    """Approximate an outward parallel ring at a fixed metric distance.

    Coastline rings can have either winding after clipping, so determine the
    outward side by probing the longest edge. Vertex joins use capped mitres;
    the cap prevents sharp coves and headlands from producing enormous spikes.
    The original land is rendered over these rings, leaving only the seaward
    strip visible.
    """
    if len(pts) < 3 or distance_m <= 0:
        return list(pts)

    longest = max(range(len(pts)),
                  key=lambda i: math.dist(pts[i], pts[(i + 1) % len(pts)]))
    a, b = pts[longest], pts[(longest + 1) % len(pts)]
    dx, dy = b[0] - a[0], b[1] - a[1]
    ln = math.hypot(dx, dy) or 1.0
    left = (-dy / ln, dx / ln)
    mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
    left_probe = (mid[0] + left[0] * 2, mid[1] + left[1] * 2)
    outward_sign = -1.0 if point_in_poly(pts, left_probe) else 1.0

    out = []
    n = len(pts)
    for i, p in enumerate(pts):
        prev, nxt = pts[(i - 1) % n], pts[(i + 1) % n]
        e1 = (p[0] - prev[0], p[1] - prev[1])
        e2 = (nxt[0] - p[0], nxt[1] - p[1])
        l1, l2 = math.hypot(*e1) or 1.0, math.hypot(*e2) or 1.0
        n1 = (-e1[1] / l1 * outward_sign, e1[0] / l1 * outward_sign)
        n2 = (-e2[1] / l2 * outward_sign, e2[0] / l2 * outward_sign)
        bx, by = n1[0] + n2[0], n1[1] + n2[1]
        bl = math.hypot(bx, by)
        if bl < 1e-6:
            bx, by, scale = n2[0], n2[1], distance_m
        else:
            bx, by = bx / bl, by / bl
            denom = max(0.35, bx * n2[0] + by * n2[1])
            scale = min(distance_m / denom, distance_m * 2.5)
        q = (p[0] + bx * scale, p[1] + by * scale)
        # Very tight concavities can point a miter back into the land. A plain
        # edge-normal offset is a safer local fallback for the shallow zone.
        if point_in_poly(pts, q):
            q = (p[0] + n2[0] * distance_m, p[1] + n2[1] * distance_m)
        out.append(q)
    return out

# ---------------- carve-splice (dock basins cut into collidable land) ----------------


def nearest_on_ring(ring, p):
    """Nearest point on a closed ring's boundary -> (dist, arc position, point)."""
    best = (math.inf, 0.0, ring[0])
    s = 0.0
    n = len(ring)
    for i in range(n):
        a, b = ring[i], ring[(i + 1) % n]
        seg = math.dist(a, b)
        if seg > 0:
            t = ((p[0] - a[0]) * (b[0] - a[0]) + (p[1] - a[1]) * (b[1] - a[1])) / (seg * seg)
            t = max(0.0, min(1.0, t))
            q = (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))
            d = math.dist(p, q)
            if d < best[0]:
                best = (d, s + t * seg, q)
        s += seg
    return best


def ring_length(ring):
    return sum(math.dist(ring[i], ring[(i + 1) % len(ring)]) for i in range(len(ring)))


def apply_carve(land, carve):
    """Splice a hand-authored detour into every land ring near its endpoints.

    The carve path enters land at its first point and leaves at its last; the
    short boundary arc between the two nearest-point projections (the bit of
    waterfront crossing the dock mouth) is replaced with the detour, notching
    the basin out of the polygon. Ring traversal order is preserved, so the
    winding (land-on-left) survives without any signed-area fixups.
    """
    path_m = [proj(lat, lon) for lat, lon in carve["path"]]
    probe = proj(*carve["probe_inside"])
    expected = poly_area(path_m)
    a_pt, b_pt = path_m[0], path_m[-1]
    carved = 0
    for idx, ring in enumerate(land):
        dA, sA, pA = nearest_on_ring(ring, a_pt)
        dB, sB, pB = nearest_on_ring(ring, b_pt)
        if dA > carve["max_snap"] or dB > carve["max_snap"]:
            continue
        for mid in path_m[1:-1]:
            if not point_in_poly(ring, mid):
                raise RuntimeError(f"carve {carve['name']}: interior point not inside land")
        if not point_in_poly(ring, probe):
            raise RuntimeError(f"carve {carve['name']}: probe not inside land pre-splice")
        L = ring_length(ring)
        if (sB - sA) % L <= (sA - sB) % L:
            s1, p1, s2, p2, ins = sA, pA, sB, pB, list(path_m)
        else:
            s1, p1, s2, p2, ins = sB, pB, sA, pA, list(reversed(path_m))
        # collect original vertices on the long arc, in ring order from s2 to s1
        span = (s1 - s2) % L
        keep = []
        s = 0.0
        for i in range(len(ring)):
            d = (s - s2) % L
            if EDGE_EPS < d < span - EDGE_EPS:
                keep.append((d, ring[i]))
            s += math.dist(ring[i], ring[(i + 1) % len(ring)])
        keep.sort(key=lambda kv: kv[0])
        new_ring = [p1] + ins + [p2] + [p for _, p in keep]
        new_ring = [p for i, p in enumerate(new_ring)
                    if i == 0 or math.dist(p, new_ring[i - 1]) > 0.01]
        removed = poly_area(ring) - poly_area(new_ring)
        if not (0.3 * expected <= removed <= 2.5 * expected):
            raise RuntimeError(f"carve {carve['name']}: removed {removed:.0f} m^2, "
                               f"expected ~{expected:.0f} m^2")
        if point_in_poly(new_ring, probe):
            raise RuntimeError(f"carve {carve['name']}: probe still inside land post-splice")
        land[idx] = new_ring
        carved += 1
        print(f"  carved {carve['name']} out of land ring {idx} "
              f"({removed:.0f} m^2, snap {dA:.1f}/{dB:.1f} m)")
    if carved == 0:
        raise RuntimeError(f"carve {carve['name']}: no land ring within "
                           f"{carve['max_snap']} m of the carve mouth")

# ---------------- per-rectangle pipeline ----------------


def process_rect(osm, region):
    """Run the single-rectangle pipeline; set_world(rect) must be active.

    Returns raw (unsimplified, rect-local) layers. Non-land items carry _id
    (OSM id) and _full (entirely inside this rectangle) for cross-rectangle
    deduplication.
    """
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

    def all_inside(pts):
        return all(-EDGE_EPS <= x <= WORLD_W + EDGE_EPS and -EDGE_EPS <= y <= WORLD_H + EDGE_EPS
                   for x, y in pts)

    stats = {}

    # ---- coastline -> land polygons ----
    fixups = region["fixups"]
    coast_ways = [{"id": w["id"], "nodes": fixups.get(w["id"], w["nodes"])}
                  for w in by_tag("natural", "coastline")]
    stats["coastline ways"] = len(coast_ways)
    chains = stitch_chains(coast_ways, allow_reverse=False)
    chain_objs = [{"pts": [nodes[i] for i in c["nodes"] if i in nodes], "closed": c["closed"]}
                  for c in chains]
    chain_objs = [c for c in chain_objs if len(c["pts"]) >= 2]
    chain_objs = coord_join(chain_objs, region["join_eps"])
    apply_coast_joins(chain_objs, region)
    stats["coastline chains"] = len(chain_objs)

    land = []
    open_fragments = []
    for chain in chain_objs:
        pts = chain["pts"]
        if chain["closed"] and math.dist(pts[0], pts[-1]) < 1e-9:
            pts = pts[:-1]
        if chain["closed"] and all_inside(pts):
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
                      "pts": pts, "_id": w["id"], "_full": True})

    # ---- small water polygons (docks) ----
    water = []

    def add_water_ring(pts, name, wid):
        if len(pts) >= 3 and poly_area(pts) <= WATER_MAX_AREA:
            water.append({"name": name, "pts": pts, "_id": wid, "_full": True})

    for w in by_tag("natural", "water"):
        if w["tags"].get("water") == "river":
            continue
        if w["nodes"][0] != w["nodes"][-1]:
            continue
        add_water_ring(way_pts(w)[:-1], w["tags"].get("name") or f"water-{w['id']}", w["id"])
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
                           tags.get("name") or f"water-rel-{rel['id']}", f"rel-{rel['id']}")
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
        bridge_deck.append({"name": w["tags"].get("name") or f"bridge-{w['id']}", "pts": pts,
                            "_id": w["id"], "_full": True})
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
    if region["centerline_fallback"] and not bridge_deck and len(bridge_centerline) >= 2:
        print("  no man_made=bridge outline; buffering centerline +/-9 m")
        bridge_deck.append({"name": "bridge-buffered",
                            "pts": buffer_polyline(bridge_centerline, 18.0),
                            "_id": "bridge-buffered", "_full": False})

    bridge_supports = []
    for w in by_tag("man_made", "bridge_support"):
        pts = way_pts(w)
        if w["nodes"][0] == w["nodes"][-1]:
            pts = pts[:-1]
        bridge_supports.append({"name": f"support-{w['id']}", "pts": pts,
                                "_id": w["id"], "_full": True})
    for nid, tags in node_tags.items():
        if tags.get("man_made") == "bridge_support":
            bridge_supports.append({"name": f"support-node-{nid}", "pts": [nodes[nid]],
                                    "_id": f"node-{nid}", "_full": True})
    stats["bridge supports"] = len(bridge_supports)

    # ---- decoration layers (visual only, not collidable) ----
    buildings = []
    for w in ways.values():
        t = w["tags"]
        if "building" not in t and t.get("man_made") != "storage_tank":
            continue
        if w["nodes"][0] != w["nodes"][-1]:
            continue
        raw = way_pts(w)[:-1]
        pts = clip_poly_rect(raw)
        if len(pts) >= 3 and poly_area(pts) >= region["building_min_area"]:
            buildings.append({"name": t.get("name") or f"bldg-{w['id']}", "pts": pts,
                              "_id": w["id"], "_full": all_inside(raw)})
    stats["buildings kept"] = len(buildings)

    vegetation = []
    for w in ways.values():
        t = w["tags"]
        if not any(t.get(k) in v for k, v in GREEN_TAGS.items()):
            continue
        if "building" in t or w["nodes"][0] != w["nodes"][-1]:
            continue
        raw = way_pts(w)[:-1]
        pts = clip_poly_rect(raw)
        if len(pts) >= 3:
            vegetation.append({"name": t.get("name") or f"veg-{w['id']}", "pts": pts,
                               "_id": w["id"], "_full": all_inside(raw)})
    stats["vegetation polygons"] = len(vegetation)

    glaciers = []
    glacier_member_ids = {
        m["ref"] for rel in rels if rel.get("tags", {}).get("natural") == "glacier"
        for m in rel.get("members", []) if m.get("type") == "way" and m.get("role") in ("outer", "")
    }
    for w in by_tag("natural", "glacier"):
        if w["id"] in glacier_member_ids or w["nodes"][0] != w["nodes"][-1]:
            continue
        raw = way_pts(w)[:-1]
        pts = clip_poly_rect(raw)
        if len(pts) >= 3:
            glaciers.append({"name": w["tags"].get("name") or f"glacier-{w['id']}",
                             "pts": pts, "_id": w["id"], "_full": all_inside(raw)})
    for rel in rels:
        tags = rel.get("tags", {})
        if tags.get("natural") != "glacier":
            continue
        outers = [ways[m["ref"]] for m in rel.get("members", [])
                  if m.get("type") == "way" and m.get("role") in ("outer", "") and m["ref"] in ways]
        for ri, ring in enumerate(stitch_chains(outers)):
            if not ring["closed"]:
                print("  skipped unstitchable glacier relation", rel["id"], tags.get("name", ""))
                continue
            raw = [nodes[i] for i in ring["nodes"][:-1] if i in nodes]
            pts = clip_poly_rect(raw)
            if len(pts) >= 3:
                glaciers.append({"name": tags.get("name") or f"glacier-rel-{rel['id']}-{ri}",
                                 "pts": pts, "_id": f"rel-{rel['id']}-{ri}",
                                 "_full": all_inside(raw)})
    stats["glacier polygons"] = len(glaciers)

    roads = []
    for w in by_tag("highway"):
        t = w["tags"]
        width = ROAD_WIDTHS.get(t.get("highway"))
        if width is None or "bridge" in t or t.get("tunnel"):
            continue  # bridge decks are their own layer
        if t.get("service") in ("parking_aisle", "driveway"):
            continue
        pts = way_pts(w)
        full = all_inside(pts)
        for frag in clip_chain(pts):
            roads.append({"name": t.get("name") or f"road-{w['id']}", "w": width, "pts": frag,
                          "_id": w["id"], "_full": full})
    stats["road segments"] = len(roads)

    return {"land": land, "piers": piers, "water": water, "bridge_deck": bridge_deck,
            "bridge_centerline": bridge_centerline, "bridge_supports": bridge_supports,
            "buildings": buildings, "vegetation": vegetation, "glaciers": glaciers,
            "roads": roads}, stats

# ---------------- region assembly ----------------

DEDUPE_LAYERS = ("piers", "water", "bridge_deck", "bridge_supports", "buildings",
                 "vegetation", "glaciers", "roads")


def merge_region(per_rect, offsets):
    """Translate per-rectangle layers into region coordinates and concatenate.

    Items fully inside some rectangle are emitted once (first rectangle wins);
    clipped fragments of such items are dropped. Land rings are never deduped:
    overlapping copies in the rectangle-overlap corridors render and collide
    correctly as-is.
    """
    def shift_pts(pts, off):
        return [(x + off[0], y + off[1]) for x, y in pts]

    merged = {"land": []}
    for ri, layers in enumerate(per_rect):
        for ring in layers["land"]:
            merged["land"].append(shift_pts(ring, offsets[ri]))

    for key in DEDUPE_LAYERS:
        full_ids = {it["_id"] for layers in per_rect for it in layers[key] if it["_full"]}
        emitted = set()
        out = []
        for ri, layers in enumerate(per_rect):
            for it in layers[key]:
                if it["_full"]:
                    if it["_id"] in emitted:
                        continue
                    emitted.add(it["_id"])
                elif it["_id"] in full_ids:
                    continue
                shifted = dict(it)
                shifted["pts"] = shift_pts(it["pts"], offsets[ri])
                del shifted["_id"], shifted["_full"]
                out.append(shifted)
        merged[key] = out

    best_len, best_line = 0.0, []
    for ri, layers in enumerate(per_rect):
        pts = layers["bridge_centerline"]
        length = sum(math.dist(pts[i - 1], pts[i]) for i in range(1, len(pts)))
        if length > best_len:
            best_len, best_line = length, shift_pts(pts, offsets[ri])
    merged["bridge_centerline"] = best_line
    merged["bridge_centerline_len"] = round(best_len)
    return merged


def build_region(name, refresh):
    region = REGIONS[name]
    rects = region["rects"]
    union = {"south": min(r["south"] for r in rects),
             "west": min(r["west"] for r in rects),
             "north": max(r["north"] for r in rects),
             "east": max(r["east"] for r in rects)}
    set_lon_scale(union["north"])

    per_rect = []
    offsets = []
    stats = {}
    for i, rect in enumerate(rects):
        if len(rects) > 1:
            print(f"--- rectangle {i + 1}/{len(rects)} ---")
        cache_file = os.path.join(HERE, region["cache_files"][i])
        osm = fetch_osm(make_query(rect, region["around_m"]), cache_file, refresh)
        set_world(rect)
        layers, rect_stats = process_rect(osm, region)
        per_rect.append(layers)
        offsets.append(((rect["west"] - union["west"]) * M_PER_DEG_LON,
                        (union["north"] - rect["north"]) * M_PER_DEG_LAT))
        for k, v in rect_stats.items():
            stats[k] = stats.get(k, 0) + v

    set_world(union)  # region frame for carves, probes, meta
    merged = merge_region(per_rect, offsets)
    stats["bridge centerline length m"] = merged["bridge_centerline_len"]
    if len(rects) > 1:
        stats["land polygons"] = len(merged["land"])

    for carve in region["carves"]:
        apply_carve(merged["land"], carve)

    shallow_m = region.get("shallow_m", 0.0)
    shallows = []
    shallow_bands = []
    if shallow_m > 0:
        for i, ring in enumerate(merged["land"]):
            shallows.append({"name": f"shallow-{i}-{shallow_m:g}m",
                             "pts": offset_ring(ring, shallow_m), "distance": shallow_m})
            for d in (shallow_m * 0.8, shallow_m * 0.6,
                      shallow_m * 0.4, shallow_m * 0.2):
                if d < shallow_m:
                    shallow_bands.append({"name": f"shallow-{i}-{d:g}m",
                                          "pts": offset_ring(ring, d), "distance": d})
        stats["shallow collision polygons"] = len(shallows)
        stats["shallow gradient bands"] = len(shallow_bands)

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
            entry = {"name": item["name"], "pts": pts,
                     "bbox": [round(v, 1) for v in poly_bbox(pts)]}
            if "distance" in item:
                entry["distance"] = item["distance"]
            out_list.append(entry)
        return out_list

    out = {
        "meta": {
            "bbox": union,
            "origin": {"lat": union["north"], "lon": union["west"]},
            "mPerDegLat": M_PER_DEG_LAT,
            "mPerDegLon": round(M_PER_DEG_LON, 1),
            "widthM": round(WORLD_W, 1),
            "heightM": round(WORLD_H, 1),
        },
        "land": finalize([{"name": f"land-{i}", "pts": pts}
                          for i, pts in enumerate(merged["land"])], True),
        "shallows": finalize(shallows, True, 0.75),
        "shallowBands": finalize(shallow_bands, True, 0.75),
        "piers": finalize(merged["piers"], True),
        "water": finalize(merged["water"], True),
        "vegetation": finalize(merged["vegetation"], True, 2.0),
        "glaciers": finalize(merged["glaciers"], True, 1.0),
        "buildings": finalize(merged["buildings"], True, 0.5),
        "bridgeDeck": finalize(merged["bridge_deck"], True),
        "bridgeSupports": [{"name": s["name"],
                            "pts": [[round(x, 1), round(y, 1)] for x, y in s["pts"]]}
                           for s in merged["bridge_supports"]],
        "bridgeCenterline": [[round(x, 1), round(y, 1)] for x, y in merged["bridge_centerline"]],
    }
    if len(rects) > 1:
        out["meta"]["rects"] = [
            [round((r["west"] - union["west"]) * M_PER_DEG_LON, 1),
             round((union["north"] - r["north"]) * M_PER_DEG_LAT, 1),
             round((r["east"] - union["west"]) * M_PER_DEG_LON, 1),
             round((union["north"] - r["south"]) * M_PER_DEG_LAT, 1)] for r in rects]
    road_out = []
    for r in merged["roads"]:
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

    failed = []
    for want, lat, lon, label in region["probes"]:
        hits = land_hits(lat, lon)
        ok = hits == 0 if want == "water" else hits >= 1
        print(f"probe {label} land hits: {hits} (want {want})")
        if not ok:
            failed.append(label)
    if failed:
        raise RuntimeError(f"sanity probes failed ({', '.join(failed)}) - "
                           "land/water inverted or stitching broken")

    # ---- emit ----
    def js(obj):
        return json.dumps(obj, separators=(",", ":"))

    regen = "python tools/build_terrain.py" + ("" if name == "hobart" else " " + name)
    lines = [
        f"// GENERATED by tools/build_terrain.py on {date.today().isoformat()} - do not edit by hand.",
        f"// Regenerate with: {regen}",
        "// Data (c) OpenStreetMap contributors, ODbL - openstreetmap.org/copyright",
        f"var {region['global_name']} = {{",
        f"meta: {js(out['meta'])},",
    ]
    for key in ("land", "shallows", "shallowBands", "piers", "water", "vegetation",
                "glaciers", "buildings", "roads",
                "bridgeDeck", "bridgeSupports"):
        lines.append(f"{key}: [")
        lines.extend(js(poly) + "," for poly in out[key])
        lines.append("],")
    lines.append(f"bridgeCenterline: {js(out['bridgeCenterline'])}")
    lines.append("};")
    out_file = os.path.join(HERE, region["out_file"])
    with open(out_file, "w", encoding="utf8", newline="\n") as f:
        f.write("\n".join(lines))

    print("\n--- stats ---")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    print("wrote", os.path.normpath(out_file),
          f"({os.path.getsize(out_file) // 1024} KB)")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    refresh = "--refresh" in sys.argv
    name = args[0] if args else "hobart"
    if name not in REGIONS:
        raise SystemExit(f"unknown region {name!r} - choose from {sorted(REGIONS)}")
    build_region(name, refresh)


if __name__ == "__main__":
    main()
