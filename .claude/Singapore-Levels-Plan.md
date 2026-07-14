# Singapore level set: Changi → King George VI dry dock

## Context

The game currently has one map (Hobart, `HOBART_TERRAIN`) and 6 levels. The user wants a Singapore chapter: sail from Changi Bay up the East Johor Strait and west to Sembawang shipyard, ending **inside the King George VI graving dock**. Terrain is the union of 4 user-given lat/lon rectangles forming a connected corridor. New gameplay: small-boat traffic that actively avoids the player, plus LNG/cargo vessels on fixed routes (dashed route line visible when near) that do NOT avoid the player. Minimal wind, no current.

**User decisions (confirmed via questions):**
- Final berth **inside KGVI dry dock** (OSM way 252392151, 327×37 m, axis ≈29.3°, entrance NNE) — requires carving the dock out of collidable land (OSM maps it as `natural=water` over land; the game's water layer is visual-only).
- **Any vessel contact = crash** (small boats included).
- **Split into 3 staged levels** (appended as Levels 7–9). The user's original berth coordinate (1.4644853, 103.8229628 — in the wharf basin, 65 m off a quay face running ≈29°) becomes the Stage-2 berth; Stage 3 is the short dock entry.

**Verified geography (Overpass recon):** the KGVI dock mouth midpoint (1.4636908, 103.8239172) lies exactly on coastline way 1138707326's segment between (1.4633250, 103.8245580) and (1.4640054, 103.8233659) — the carve splice is local and well-defined. Start point (1.3525647, 104.0532788) is open water with >400 m clearance.

**Key sizing correction:** Hobart berths sit 20 m off quays in open water, so `wid: 32` is unconstrained there. Inside the dock, `checkBerth` allows ship center `across ≤ wid/2` and the hull adds beam/2 = 12.8 m. Against a 37 m dock → wid ≤ 11. So: **author the carve ~4 m outside the OSM water edge per side (≈45 m clear) and use `wid: 14`**.

## Files

- `parking-mania-nuyina/parking-mania-nuyina.html` — game (all logic, lines 25–1347)
- `parking-mania-nuyina/tools/build_terrain.py` — terrain builder (bbox line 25, query 55–77, cache 45, pipeline 459–606, close_against_bbox 366–410, emit 670–685)
- `parking-mania-nuyina/tools/preview.html` — dataset viewer / coordinate picker
- `parking-mania-nuyina/singapore-terrain.js` — NEW generated output (`SINGAPORE_TERRAIN`)
- `parking-mania-nuyina/hobart-terrain.js` — must stay byte-identical (regression artifact)

## Phase 1 — Builder refactor: regions + multi-rect (Hobart unchanged)

Strategy: run the existing single-rect pipeline **verbatim in rect-local coords** (rect NW = 0,0), then translate outputs into region coords. `clip_*`, `peri_t`, `close_against_bbox`, `stitch_chains` stay untouched.

1. `REGIONS` dict replacing hardcoded BBOX/ORIGIN/CACHE_FILE/OUT_FILE/QUERY/FIXUPS: per region = `rects[]`, `cache_files[]` (hobart keeps `overpass-cache.json`; singapore gets `overpass-cache-sg-0..3.json`), `out_file`, `global_name`, `fixups`, `carves`, `probes`, `params` (AROUND_M, BUILDING_MIN_AREA overrides). Singapore rects (S,W,N,E):
   - (1.4222030, 103.7783103) – (1.4872579, 103.9041084)
   - (1.394027, 103.888527) – (1.452211, 103.946499)
   - (1.3624150, 103.9258767) – (1.4382378, 104.1238541)
   - (1.3120178, 103.9925566) – (1.3866969, 104.1444767)
   Region origin = NW of union bbox (1.487257912, 103.778310291). **`M_PER_DEG_LON` computed once at region origin, not per rect** (per-rect scales would misalign seam duplicates ~1.5 m).
2. `set_world(rect)` sets module globals ORIGIN/WORLD_W/WORLD_H/PERIM per rect; `make_query(rect, params)` parameterizes the f-string; `fetch_osm(rect, cache_file, refresh)` with retry (2 attempts/endpoint, 15 s backoff — overpass-api.de 504s are transient; custom UA already set, line 53).
3. `process_rect(...)`: factor main() 459–606 into a function returning per-layer lists in rect-local coords; annotate non-land items with `_id` + `_full` (fully inside rect) for dedupe.
4. Mid-bbox hardening (Johor OSM quality unknown): keep the hard failure + printed endpoint lat/lons (lines 378–380); add a coordinate-join fallback after `stitch_chains` joining open chains whose endpoints coincide within 0.5 m (gated per-region param, Hobart path untouched).
5. `merge_region(...)`: translate each rect's output by its NW offset, concat. Dedupe decorations: emit `_full` items once; skip clipped fragments whose id is `_full` in another rect; keep true straddlers. **Land rings never deduped** — overlapping copies in seam corridors are fine (land/water render fill-only, drawPolyList 584–594; collision is any-poly).
6. `emit(...)`: parameterize global name + out file; meta gets union bbox + `rects[]` (preview seam outlines). CLI: `python build_terrain.py [hobart|singapore] [--refresh]`, default hobart.
7. Verify: rebuild hobart from cache → byte-identical except header date; `#test` green.

## Phase 2 — Carve-splice (builder), applied after land merge, before DP

Config `KGVI_CARVE`: ordered lat/lon path, first & last points at the dock mouth (extended ~5 m past the coastline), running inland around the basin — from the OSM dock corners (1.4636092,103.8240632 / 1.4610334,103.8226233 / 1.4611965,103.8223313 / 1.4637723,103.8237712), **offset ~4 m outward per side (≈45 m clear width)**; plus `probe_inside` = dock centroid (1.4624029, 103.8231973), `max_snap: 30`.

`apply_carve(land, carve)`:
- Nearest-boundary-point of path endpoints A/B per ring (new `seg_nearest` helper); rings with both < max_snap are targets (≥1 required, else error with distances). Applied to **every** matching ring (overlap duplicates must all be carved — one uncarved copy bricks the level).
- Parametrize A/B by cumulative arc length; **replace the shorter arc** (the ~40 m of waterfront across the mouth) with the carve path (auto-reversed so its first point is nearer A). Preserving ring traversal direction keeps winding correct — the notch subtracts automatically.
- Hard asserts: carve midpoints inside ring pre-splice; area decreases by ≈ dock area (±2×); `probe_inside` inside→outside; then region-wide `probe_inside` hits 0 land polys. Add to per-region probes so every rebuild re-validates.

## Phase 3 — Preview upgrade (`tools/preview.html`)

- `?region=singapore|hobart` param → `document.write` the right script src; `const T = window[REGION_GLOBAL]`.
- Add wheel zoom + drag pan (at full extent, the 37 m dock is <2 px — picking impossible without zoom); draw `meta.rects` outlines; click readout (world m + lat/lon) inverts the view transform.

## Phase 4 — Generate Singapore terrain + tune

1. `python tools/build_terrain.py singapore --refresh` (4 fetches, per-rect caches survive a mid-run 504).
2. Resolve any `fragment ends mid-bbox` via join-fallback stats, then per-way `fixups`.
3. Probes: mid-strait off Sembawang (~1.457,103.840→water), Punggol town (~1.403,103.902→land), mid-Serangoon Harbour (~1.408,103.930→water), Pulau Ubin interior (~1.413,103.960→land), KGVI centroid (→water post-carve).
4. Size gate: buildings dominate (Hobart: 14k buildings ≈ 80% of 3.1 MB). Start with `AROUND_M: 400`, `BUILDING_MIN_AREA: 120`; if output > ~8 MB tighten further (drop `service` roads). Rebuilds from cache are cheap.
5. Eyeball in preview: seams, Ubin, dock notch at high zoom. Known accepted quirk: reservoirs (Serangoon/Lower Seletar) render land-coloured (`WATER_MAX_AREA` drops them — correct for collision truthfulness).

## Phase 5 — Game terrain registry

The 7 `HOBART_TERRAIN` read sites: guard 38, click-logger 245, drawRoads 624, bakeTerrain 672–677, drawBridgeDeck 713, resetLevel 1270, test 1335–37 (leave test as-is, Hobart-scoped).

- Add `<script src="singapore-terrain.js">` (line 24 area); guard checks both globals.
- `const TERRAINS = {hobart: HOBART_TERRAIN, singapore: SINGAPORE_TERRAIN}; const OBSTACLES = {hobart: PYLONS, singapore: []}; var terrain, obstacles;` — set in resetLevel from `level.terrain || "hobart"`; line 1270 → `collidables = terrain.land.concat(terrain.piers, obstacles)`. resetLevel already re-bakes (1273), so terrain switches are covered.
- `drawPylons` → `drawObstacles(vb)` using `obstacles` (call site renderWorld:963). Replace remaining reads with `terrain.*`.
- Title hint line 1172 → "1-9: jump to a level" (digit-jump code 206–209 already handles any LEVELS length).

## Phase 6 — The three levels (append to LEVELS, `terrain: "singapore"`)

Coords below: **[D]** = derived from lat/lon (origin above, mPerDegLat 110574, mPerDegLon ≈111282.5); **[P]** = pick in preview during implementation. All: `wind {speed 1–2, gust:false}`, `current {speed: 0}`.

- **Level 7 — Serangoon Harbour: Punggol anchorage.** start [D] (30599, 14893) hdg [P] up-channel; berth = anchorage box off Punggol [P] (~15767, 7659), hdg = fairway axis, `len 250, wid 60` (existing checkBerth works verbatim as an anchorage: align ±8°, SOG<0.5, hold 3 s).
- **Level 8 — Johor Strait: Sembawang quay.** start = L7 berth; berth [D] near user's coordinate (4969, 2518), hdg 29, `len 190, wid 32` — nudge during authoring to sit ~20–25 m off the quay face (Hobart convention) along the 29° face near the user's point.
- **Level 9 — King George VI graving dock.** start = L8 berth; berth [D] dock centroid (4995, 2748) re-picked post-carve on the carved axis, hdg 29.3, `len 240, wid 14`. alongTol ±55 m keeps the bow ~29 m off the head wall. Light traffic only.

Flag for playtest: L7 leg ≈16 km ≈ 8 min flat-out; if it drags, move the start up-channel.

## Phase 7 — NPC traffic system (new; house style: plain functions, module state)

Per-level config: `traffic: { boats: {count, area:[x0,y0,x1,y1]}, vessels: [{kind:"lng"|"cargo", path:[[x,y]…], dockHold, startFrac}] }` — vessel routes [P]: eastern entrance → Tanjung Langsat / Pasir Gudang (Johor shore); west strait → Senoko jetty. `startFrac` staggers spawns so levels start alive.

- **Kinds:** `launch {len 14, beam 4.5, speed 5, turn 0.9}`, `lng {290, 46, 4.0, 0.03}`, `cargo {200, 32, 5.0, 0.05}`; 5–6-pt hulls generated from len/beam.
- **Water mask** (small-boat land avoidance): built in `initTraffic()` per level, **scoped to `boats.area`** (whole world would be ~790 MB; a 6×4 km area at 4 m/px is ~1.5 MB retained Uint8Array). Offscreen canvas: fill white, fill `terrain.land`+`terrain.piers`+`obstacles` black via existing `drawPolyPath` (577), one getImageData → Uint8Array. `isWater(x,y)`; out-of-area = false (doubles as containment).
- **Boats:** wander to rejection-sampled water targets 200–800 m away; **flee player** inside 400 m (and big vessels inside 500 m) via repulsion added to desired velocity; 3-whisker `isWater` probes (±0.5 rad, 25/50/75 m) steer around land; turn/accel clamped; on-land safety respawn.
- **Vessels:** kinematic path-followers, states run → dock(hold) → teleport-respawn at path[0]; waypoint advance <60 m; docking speed fade `min(speed, max(0.6, d/80))`; heading turn-rate-clamped. No player avoidance.
- **Collision:** generalize `shipHullWorld` → `hullWorld(hull,x,y,hdg)` (shipHullWorld delegates, zero behavior change); `checkNpcCollision()` mirrors checkCollision (bbox cull, segSeg, robustPointInPolygon both ways) player-vs-each-NPC. gameLoop playing block → `stepShip(); stepNpcs(); if (checkCollision() || checkNpcCollision()) gameState="crashed"; else checkBerth();` (NPCs freeze on panels, like the ship).
- **Rendering:** `drawNpcs(vb)` in renderWorld between drawWake (964) and drawShip (965). LNG: dark-red hull, 4 white tank domes, aft house; cargo: slate hull + seeded container grid; launch: white hull/grey cabin. **Dashed route** when `dist(ship, vessel) < 1800 m`: `ctx.setLineDash([10,6])` (precedent line 701), 2 px rgba(255,255,255,0.5) polyline from vessel through remaining waypoints, circle at the dock point, reset dash.

## Phase 8 — Tests + verification

1. **`#test-sg`** branch beside line 1310 (hash compare is exact, so it coexists with `#test`), same `#test-result` JSON div: auto-drive moved/u; teleport asserts (clear water near L7 start; Punggol shore hit [P]; then startLevel(8), ship at L9 berth center aligned/stopped → `!checkCollision() && berthConditionsMet` — **this is the carve regression**); traffic asserts (npcs.length matches config; vessel advances >5 m in 1.5 s; boat distance increases after teleporting player 50 m from it); terrain counts. ~7 s span → headless budget 10000.
2. **Headless (PowerShell `&`, not Git Bash):** `& "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --headless=new --disable-gpu --dump-dom --virtual-time-budget=6000 "file:///...parking-mania-nuyina.html#test"` → `"pass":true`; same with `#test-sg` @10000.
3. **Hobart regression** after every builder commit: regenerate from cache, diff minus header date line → empty; `#test` green.
4. **Perf sanity:** temporary `performance.now()` around checkCollision + bakeTerrain on L8 (worst seams). Budgets: collision <4 ms, bake <50 ms. Expected fine (Hobart land is 1,684 verts; segSeg is cheap). Fallback if exceeded: init-time 250 m edge-bucket grid in the game (do NOT split rings at build time — connector edges create phantom walls in open water).
5. **Manual playthrough** L7→L9: berth chaining via Space, crash/retry at the dock, C-overlay, watch a vessel full dock-hold-respawn cycle, hunt seam artifacts.

## Risks (likelihood × impact)

1. Decoration/file-size blowup (H×M) → per-region params + Phase 4 gate.
2. Johor coastline gaps → mid-bbox failures (M×M) → join fallback + FIXUPS + printed diagnostics.
3. Carve mis-splice (M×H) → arc-length shorter-arc rule + area/probe asserts + `#test-sg` + preview zoom.
4. Dock too tight to be fun (M×M) → 45 m carve, wid 14; both tunable in one place.
5. Overpass 504s (M×L) → retries, per-rect caches.
6. Boat AI stuck (M×L) → whiskers + respawn net; cosmetic failure mode.
7. Frame cost (L×M) → measure; edge-grid fallback.
8. Hobart regression (L×H) → byte-diff + `#test` every commit.

## Commit breakdown

1. builder: region configs, per-rect pipeline, caches/fixups/probes, retries (Hobart byte-identical)
2. tools: preview region param + pan/zoom + rect outlines
3. builder: carve-splice + Singapore region; generate + commit singapore-terrain.js
4. game: terrain registry, scoped obstacles, level.terrain, guard, script tag, 1-9 hint
5. game: Levels 7–9 authored coords + `#test-sg`
6. game: NPC traffic system + routes + traffic assertions
7. (only if measured) collision edge grid
