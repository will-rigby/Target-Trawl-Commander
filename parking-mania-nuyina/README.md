# Parking Mania: Nuyina

Berth the icebreaker RSV Nuyina across ten levels spanning Hobart, Singapore,
and Heard Island. Open [parking-mania-nuyina.html](parking-mania-nuyina.html)
in a browser to play.

## Project layout

- `parking-mania-nuyina.html` - the game itself (canvas renderer, physics, levels).
- `hobart-terrain.js`, `singapore-terrain.js`, `heard-terrain.js` - generated
  terrain data (land, quays, roads, buildings, vegetation) for each region.
- `tools/build_terrain.py` - fetches OSM data via the Overpass API, stitches
  coastline into land polygons, projects to a local metric grid, and writes
  the `*-terrain.js` files. Run with `python build_terrain.py [hobart|singapore|heard] [--refresh]`.
- `tools/overpass-cache*.json` - cached Overpass API responses used by the
  build script.
- `tools/preview.html` - standalone terrain preview for checking a build
  before wiring it into the game.

## Attribution

Terrain data for all three regions (Hobart, Singapore, Heard Island) is
derived from OpenStreetMap data, © OpenStreetMap contributors, available
under the [Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/1-0/).
See https://www.openstreetmap.org/copyright for full attribution terms.

Data was fetched via the [Overpass API](https://overpass-api.de/) and
processed by `tools/build_terrain.py` into simplified coastline, quay,
road, building, and vegetation geometry for gameplay.
