# VEIL map data

VEIL's own copy of where surveillance cameras and license-plate readers are, rebuilt every day and served as plain files with GitHub Pages. The VEIL app reads from here first. It asks Overpass directly only for squares this data doesn't cover, or when this site can't answer.

Status page: https://danom5.github.io/veil-data/

## What's in it

| Source | What it adds | License |
|---|---|---|
| OpenStreetMap, which holds everything mapped through DeFlock | Flock cameras, other plate readers, outdoor security cameras | © OpenStreetMap contributors, ODbL |
| Iowa DOT ArcGIS layer | Plate readers permitted on Iowa state highways, with the agency that runs each one | Public government records |

Indoor cameras and other surveillance types are dropped, using the same rules as the app. When an agency record is within 25 m of a plate reader already on OpenStreetMap, it counts as the same device.

**Not included on purpose:** Google Maps and Apple Maps. Their terms forbid copying their data into another database, and neither one lists cameras or plate readers anyway. The app still uses Apple Maps as the base map.

**Coverage:** the 50 states, DC and Puerto Rico. The rectangles in `sources.json` also take in border cities such as Toronto and Tijuana. To add a region, add a rectangle there.

## How it works

1. `.github/workflows/refresh.yml` runs every day at 3:17 AM Pacific. You can also start it by hand from **Actions → Refresh map data → Run workflow**.
2. `build_tiles.py` asks Overpass for each 2-degree box in turn (about 420 requests, spaced out to respect Overpass's fair-use rules), then pulls each enabled ArcGIS layer.
3. It writes one file per 0.1-degree square (`tiles/<lat>_<lon>.json`), plus `index.json` and the status page, and GitHub Pages publishes them.

**Safety:** if any source fails, or the total drops more than 30% compared with the live data, the run stops and nothing is published. Yesterday's data stays live.

**Privacy:** the app asks for a whole square (about 7 miles across), never an exact location. GitHub, like Overpass, sees the IP address making each request. Moving the site to VEIL's own domain later removes GitHub's name from the app's traffic.

## Adding an ArcGIS layer

Add an entry under `"arcgis"` in `sources.json` with these fields:

- **`url`:** the layer's FeatureServer URL.
- **`tags`:** fixed OpenStreetMap-style tags for every point in the layer.
- **`fields`:** which column becomes the `operator` and which becomes the `direction`.
- **`coded`:** any device-type codes that mean Flock.

Before setting `"enabled": true`, check that the agency allows reuse of the data.

## Test

```
python3 -m unittest -v test_build_tiles.py   # offline, runs before every publish
python3 build_tiles.py --out site            # a real build (about 20-40 minutes)
```
