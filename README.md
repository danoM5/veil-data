# VEIL map data

VEIL's own copy of where surveillance cameras and license-plate readers are, rebuilt every week and served as plain files with GitHub Pages. The VEIL app reads from here first. It asks Overpass directly only for squares this data doesn't cover, or when this site can't answer.

Status page: https://danom5.github.io/veil-data/

## What's in it

| Source | What it adds | License |
|---|---|---|
| OpenStreetMap, which holds everything mapped through DeFlock | Flock cameras, other plate readers, outdoor security cameras, speed and red-light cameras | © OpenStreetMap contributors, ODbL |
| Iowa DOT ArcGIS layer | Plate readers permitted on Iowa state highways, with the agency that runs each one | Public government records |

Indoor cameras and other surveillance types are dropped, using the same rules as the app. When an agency record is within 25 m of a plate reader already on OpenStreetMap, it counts as the same device.

**Speed and red-light camera alerts** are legal across the US but banned in some countries (for example France and Germany); turn that layer off before launching there.

**Not included on purpose:** Google Maps and Apple Maps. Their terms forbid copying their data into another database, and neither one lists cameras or plate readers anyway. The app still uses Apple Maps as the base map.

**Coverage:** the 50 states, DC and Puerto Rico. The rectangles in `sources.json` also take in border cities such as Toronto and Tijuana. To add a region, add a rectangle there.

## How it works

1. `.github/workflows/refresh.yml` runs every Monday at 3:17 AM Pacific. You can also start it by hand from **Actions → Refresh map data → Run workflow**.
2. It downloads Geofabrik's OpenStreetMap files for the US, Canada and Mexico (one file each, about 15 GB in all), keeps only surveillance devices, speed cameras and enforcement records with `osmium`, and deletes each big file straight away. The public Overpass servers time out on requests this size, so they are only used by the app for single squares.
3. `build_tiles.py` reads the filtered files, pulls each enabled ArcGIS layer, and writes one file per 0.1-degree square (`tiles/<lat>_<lon>.json`), plus `index.json` and the status page. GitHub Pages publishes them.

A run takes about 20–30 minutes. **Safety:** if any source fails, or the total drops more than 30% compared with the live data, the run stops and nothing is published. The previous data stays live.

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
python3 build_tiles.py --opl us.opl --out site  # from an osmium OPL export (see the workflow)
```
