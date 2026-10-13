#!/usr/bin/env python3
"""Builds VEIL's map data: one small JSON file per 0.1-degree square (about 7 miles across).

Sources (see sources.json):
  * OpenStreetMap, which holds everything mapped through DeFlock, fetched box by box from Overpass.
  * Public ArcGIS layers from government agencies (e.g. Iowa DOT plate-reader permits).

Output (the folder given by --out, published as a static website):
  tiles/<lat>_<lon>.json   {"elements": [...]} in the same shape the app already reads from Overpass
  index.json               when it was built, what it covers, counts, credits
  index.html               a plain status page

Safety: if any source fails, or the total drops sharply versus the last published build,
the script exits with an error and nothing is published, so the live data stays as it was.

Only the Python standard library is used. Run the offline self-test with:
  python3 -m unittest -v test_build_tiles.py
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "VEIL-data-builder/1.0 (privacy app map data; https://github.com/danoM5/veil-data)"
FLOCK_WIKIDATA = "Q108485435"
KEEP_TAGS = ("surveillance:type", "surveillance", "manufacturer", "manufacturer:wikidata", "operator", "direction",
             "highway", "enforcement", "maxspeed")
WAY_OFFSET = 1_000_000_000_000        # keeps way / relation ids from colliding with node ids
RELATION_OFFSET = 2_000_000_000_000


# ---------------------------------------------------------------- what VEIL maps

def kind(tags: dict) -> str | None:
    """Same rules as Watcher.kind in the app. None = not shown (indoor cameras, guards, etc.)."""
    enforcement = (tags.get("enforcement") or "").lower()
    if enforcement == "traffic_signals":
        return "redLightCamera"
    if enforcement in ("maxspeed", "average_speed") or (tags.get("highway") or "").lower() == "speed_camera":
        return "speedCamera"
    if tags.get("type") == "enforcement" or (tags.get("man_made") and tags.get("man_made") != "surveillance"):
        return None  # other enforcement (weight, tolls...) or a mast that only carries cameras
    stype = (tags.get("surveillance:type") or "").lower()
    if stype == "alpr":
        flock = tags.get("manufacturer:wikidata") == FLOCK_WIKIDATA or \
            (tags.get("manufacturer") or "").lower().startswith("flock")
        return "flock" if flock else "plateReader"
    if stype in ("", "camera"):
        return None if (tags.get("surveillance") or "").lower() == "indoor" else "securityCamera"
    return None


def slim(tags: dict) -> dict:
    out = {k: str(tags[k]) for k in KEEP_TAGS if tags.get(k) not in (None, "")}
    if "direction" not in out and tags.get("camera:direction"):
        out["direction"] = str(tags["camera:direction"])
    return out


def tile_key(lat: float, lon: float, size: float) -> tuple[int, int]:
    # Must match WatcherMap.tile(for:) in the app: Int((lat / 0.1).rounded(.down)).
    return math.floor(lat / size), math.floor(lon / size)


def meters(a_lat, a_lon, b_lat, b_lon) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


# ---------------------------------------------------------------- network

def read_json(path: str):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def http(url: str, data: bytes | None = None, timeout: int = 360) -> bytes:
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
    if data is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def boxes(coverage: list[dict]):
    """Splits each coverage area into query boxes of `step` degrees."""
    for area in coverage:
        step = area["step"]
        lat = area["south"]
        while lat < area["north"] - 1e-9:
            lon = area["west"]
            n = min(lat + step, area["north"])
            while lon < area["east"] - 1e-9:
                e = min(lon + step, area["east"])
                yield (round(lat, 4), round(lon, 4), round(n, 4), round(e, 4))
                lon = e
            lat = n


def overpass_query(box) -> str:
    s, w, n, e = box
    b = f"({s},{w},{n},{e})"
    # Surveillance cameras and plate readers, speed cameras, and enforcement relations
    # (red-light and speed cameras mapped as a relation between the device and the road).
    return (f'[out:json][timeout:300];(nwr["man_made"="surveillance"]{b};node["highway"="speed_camera"]{b};'
            f'relation["type"="enforcement"]["enforcement"~"^(traffic_signals|maxspeed|average_speed)$"]{b};);'
            'out center tags;')


def fetch_overpass(box, cfg: dict, log) -> list[dict]:
    body = urllib.parse.urlencode({"data": overpass_query(box)}).encode()
    last = None
    for attempt in range(3):
        for endpoint in cfg["endpoints"]:
            try:
                data = json.loads(http(endpoint, body))
                remark = data.get("remark") or ""
                if "error" in remark.lower():  # Overpass ran out of time or memory: the list is incomplete
                    raise ValueError(remark[:200])
                return data["elements"]
            except (urllib.error.URLError, TimeoutError, ValueError, KeyError, OSError) as err:
                last = err
                log(f"    {endpoint.split('/')[2]} failed ({err}); trying another")
                time.sleep(5)
        time.sleep(30 * (attempt + 1))
    raise RuntimeError(f"Overpass failed for box {box}: {last}")


def fetch_arcgis(src: dict, log) -> list[dict]:
    features, offset, page = [], 0, 1000
    while True:
        params = urllib.parse.urlencode({
            "where": "1=1", "outFields": "*", "returnGeometry": "true", "outSR": "4326",
            "f": "json", "resultOffset": offset, "resultRecordCount": page, "orderByFields": src.get("order_by", "objectid"),
        })
        data = json.loads(http(f"{src['url']}/query?{params}", timeout=120))
        if "error" in data:
            raise RuntimeError(f"ArcGIS {src['id']}: {data['error']}")
        batch = data.get("features", [])
        features += batch
        if not data.get("exceededTransferLimit") or not batch:
            return features
        offset += len(batch)


# ---------------------------------------------------------------- OpenStreetMap extract files

def _unescape(s: str) -> str:
    # OPL writes special characters as %<hex code point>%.
    out, i = [], 0
    while i < len(s):
        if s[i] == "%":
            j = s.find("%", i + 1)
            if j > i + 1:
                try:
                    out.append(chr(int(s[i + 1:j], 16))); i = j + 1; continue
                except ValueError:
                    pass
        out.append(s[i]); i += 1
    return "".join(out)


def _opl_tags(raw: str) -> dict:
    tags = {}
    for pair in raw.split(",") if raw else []:
        k, _, v = pair.partition("=")
        tags[_unescape(k)] = _unescape(v)
    return tags


def read_opl(lines):
    """Turns `osmium cat -f opl,add_metadata=false` output into Overpass-style elements.

    Ways and relations get a `center`: the middle of the box around their nodes, as Overpass's
    `out center` gives. Only what `matches_query` selects is returned."""
    nodes: dict[int, tuple] = {}
    boxes_: dict[int, tuple] = {}   # way id -> (south, west, north, east)
    for line in lines:
        line = line.strip()
        if not line:
            continue
        parts = line.split(" ")
        kind_, oid = parts[0][0], int(parts[0][1:])
        fields = {p[0]: p[1:] for p in parts[1:] if p}
        tags = _opl_tags(fields.get("T", ""))
        if kind_ == "n":
            x, y = fields.get("x", ""), fields.get("y", "")
            if not x or not y:
                continue
            lat, lon = float(y), float(x)
            nodes[oid] = (lat, lon)
            if matches_query("node", tags):
                yield {"type": "node", "id": oid, "lat": lat, "lon": lon, "tags": tags}
            continue
        pts = []
        if kind_ == "w":
            pts = [nodes[int(r[1:])] for r in fields.get("N", "").split(",") if r and int(r[1:]) in nodes]
            if pts:
                box = (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))
                boxes_[oid] = box
            el_type = "way"
        elif kind_ == "r":
            el_type = "relation"
            for m in fields.get("M", "").split(","):
                if not m:
                    continue
                ref = int(m[1:].split("@", 1)[0])
                if m[0] == "n" and ref in nodes:
                    pts.append(nodes[ref])
                elif m[0] == "w" and ref in boxes_:
                    s_, w_, n_, e_ = boxes_[ref]
                    pts += [(s_, w_), (n_, e_)]
        else:
            continue
        if pts and matches_query(el_type, tags):
            south, north = min(p[0] for p in pts), max(p[0] for p in pts)
            west, east = min(p[1] for p in pts), max(p[1] for p in pts)
            yield {"type": el_type, "id": oid, "center": {"lat": (south + north) / 2, "lon": (west + east) / 2}, "tags": tags}


# ---------------------------------------------------------------- conversion

ENFORCEMENT_KINDS = ("traffic_signals", "maxspeed", "average_speed")


def matches_query(el_type: str, tags: dict) -> bool:
    """The same selection the Overpass query makes; extract files also carry the nodes and ways
    these objects reference, which must not be mistaken for cameras."""
    if tags.get("man_made") == "surveillance":
        return True
    if el_type == "node" and tags.get("highway") == "speed_camera":
        return True
    return el_type == "relation" and tags.get("type") == "enforcement" and tags.get("enforcement") in ENFORCEMENT_KINDS


def from_osm(el: dict) -> dict | None:
    if not matches_query(el.get("type", "node"), el.get("tags", {})):
        return None
    if "lat" in el and "lon" in el:
        lat, lon = el["lat"], el["lon"]
    elif "center" in el:
        lat, lon = el["center"]["lat"], el["center"]["lon"]
    else:
        return None
    tags = el.get("tags", {})
    if kind(tags) is None:
        return None
    offset = {"node": 0, "way": WAY_OFFSET, "relation": RELATION_OFFSET}.get(el.get("type"), 0)
    return {"id": int(el["id"]) + offset, "lat": round(lat, 6), "lon": round(lon, 6), "tags": slim(tags), "src": "osm"}


def from_arcgis(feature: dict, src: dict) -> dict | None:
    geom, attrs = feature.get("geometry") or {}, feature.get("attributes") or {}
    lat, lon = geom.get("y"), geom.get("x")
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    tags = dict(src.get("tags", {}))
    for tag, field in src.get("fields", {}).items():
        value = attrs.get(field)
        if value not in (None, ""):
            tags[tag] = str(value).strip()
    for field, table in src.get("coded", {}).items():
        tags.update(table.get(str(attrs.get(field)), {}))
    if kind(tags) is None:
        return None
    key = attrs.get("globalid") or attrs.get("objectid") or f"{lat},{lon}"
    digest = int(hashlib.sha256(f"{src['id']}:{key}".encode()).hexdigest()[:12], 16)
    return {"id": -(digest or 1), "lat": round(lat, 6), "lon": round(lon, 6), "tags": slim(tags), "src": src["id"]}


def merge(osm: list[dict], extra: list[dict], dedupe_m: float) -> tuple[list[dict], int]:
    """OSM first; an agency point within `dedupe_m` of an OSM plate reader is treated as the same device."""
    grid: dict[tuple[int, int], list[dict]] = {}
    for el in osm:
        if kind(el["tags"]) in ("flock", "plateReader"):
            grid.setdefault(tile_key(el["lat"], el["lon"], 0.001), []).append(el)
    kept, dupes = list(osm), 0
    for el in extra:
        a, b = tile_key(el["lat"], el["lon"], 0.001)
        near = (o for da in (-1, 0, 1) for db in (-1, 0, 1) for o in grid.get((a + da, b + db), []))
        if any(meters(el["lat"], el["lon"], o["lat"], o["lon"]) <= dedupe_m for o in near):
            dupes += 1
        else:
            kept.append(el)
    return kept, dupes


def in_coverage(lat: float, lon: float, coverage: list[dict]) -> bool:
    return any(a["south"] <= lat <= a["north"] and a["west"] <= lon <= a["east"] for a in coverage)


# ---------------------------------------------------------------- output

def write_site(out: str, elements: list[dict], cfg: dict, sources: list[dict], now: str) -> dict:
    size = cfg["tile_size"]
    tiles: dict[tuple[int, int], list[dict]] = {}
    for el in elements:
        tiles.setdefault(tile_key(el["lat"], el["lon"], size), []).append(el)
    os.makedirs(os.path.join(out, "tiles"), exist_ok=True)
    credits = [s["attribution"] for s in sources if s.get("count")]
    for (a, b), els in tiles.items():
        els.sort(key=lambda e: e["id"])
        doc = {"version": 1, "generated": now, "attribution": credits, "elements": els}
        with open(os.path.join(out, "tiles", f"{a}_{b}.json"), "w", encoding="utf-8") as f:
            json.dump(doc, f, separators=(",", ":"), ensure_ascii=False)
    counts: dict[str, int] = {}
    for el in elements:
        k = kind(el["tags"])
        counts[k] = counts.get(k, 0) + 1
    index = {
        "version": 1,
        "generated": now,
        "tile_size": size,
        "tile_url": "tiles/{lat}_{lon}.json",
        "coverage": [[a["south"], a["west"], a["north"], a["east"]] for a in cfg["coverage"]],
        "total": len(elements),
        "counts": counts,
        "tiles": len(tiles),
        "sources": sources,
        "attribution": credits,
    }
    with open(os.path.join(out, "index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, indent=1, ensure_ascii=False)
    with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as f:
        f.write(status_page(index))
    return index


def status_page(ix: dict) -> str:
    labels = {"flock": "Flock cameras", "plateReader": "Other plate readers", "securityCamera": "Security cameras",
              "speedCamera": "Speed cameras", "redLightCamera": "Red-light cameras"}
    rows = "".join(f"<tr><td>{labels.get(k, k)}</td><td>{v:,}</td></tr>" for k, v in sorted(ix["counts"].items()))
    srcs = "".join(f"<li>{html.escape(s['name'])}: {s.get('count', 0):,}</li>" for s in ix["sources"])
    credit = "".join(f"<li>{html.escape(c)}</li>" for c in ix["attribution"])
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>VEIL map data</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:640px;margin:40px auto;padding:0 16px;color:#111;background:#fff}}
td{{padding:4px 16px 4px 0}}@media(prefers-color-scheme:dark){{body{{color:#eee;background:#111}}}}</style></head><body>
<h1>VEIL map data</h1><p>Updated {html.escape(ix['generated'])} &middot; {ix['total']:,} devices in {ix['tiles']:,} squares.</p>
<table>{rows}</table><h2>Sources</h2><ul>{srcs}</ul><h2>Credits</h2><ul>{credit}</ul>
<p>Locations can be missing or out of date. Add a missing plate reader at <a href="https://deflock.org">deflock.org</a>.</p>
</body></html>"""


# ---------------------------------------------------------------- main

def build(cfg: dict, out: str, fixtures: str | None = None, previous: str | None = None, log=print,
          opl_files: list[str] | None = None) -> dict:
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    osm_by_id: dict[int, dict] = {}
    # Normal path: OpenStreetMap extract files (downloaded and filtered by the workflow).
    for path in opl_files or []:
        before = len(osm_by_id)
        with open(path, encoding="utf-8") as f:
            for el in read_opl(f):
                item = from_osm(el)
                if item and in_coverage(item["lat"], item["lon"], cfg["coverage"]):
                    osm_by_id[item["id"]] = item
        log(f"OpenStreetMap {os.path.basename(path)}: {len(osm_by_id) - before:,} devices")
    if opl_files and not osm_by_id:
        raise RuntimeError("The OpenStreetMap files held no devices; the download or filter went wrong.")
    # Fallback / test path: Overpass, box by box.
    all_boxes = [] if opl_files else list(boxes(cfg["coverage"]))
    if all_boxes:
        log(f"OpenStreetMap: {len(all_boxes)} boxes")
    for i, box in enumerate(all_boxes, 1):
        if fixtures:
            path = os.path.join(fixtures, "osm", "{}_{}_{}_{}.json".format(*box))
            raw = read_json(path)["elements"] if os.path.exists(path) else []
        else:
            raw = fetch_overpass(box, cfg["osm"], log)
            time.sleep(cfg["osm"].get("pause_seconds", 2))
        for el in raw:
            item = from_osm(el)
            if item:
                osm_by_id[item["id"]] = item
        if i % 25 == 0 or i == len(all_boxes):
            log(f"  {i}/{len(all_boxes)} boxes, {len(osm_by_id):,} devices so far")
    osm = list(osm_by_id.values())
    sources = [{"id": "osm", "name": cfg["osm"]["name"], "attribution": cfg["osm"]["attribution"],
                "license_url": cfg["osm"]["license_url"], "count": len(osm)}]

    extra: list[dict] = []
    for src in cfg.get("arcgis", []):
        if not src.get("enabled"):
            continue
        if fixtures:
            path = os.path.join(fixtures, "arcgis", f"{src['id']}.json")
            features = read_json(path)["features"] if os.path.exists(path) else []
        else:
            features = fetch_arcgis(src, log)
        items = [x for x in (from_arcgis(f, src) for f in features) if x and in_coverage(x["lat"], x["lon"], cfg["coverage"])]
        log(f"{src['name']}: {len(items)} devices")
        extra += items
        sources.append({"id": src["id"], "name": src["name"], "attribution": src["attribution"],
                        "url": src["url"], "count": len(items)})

    elements, dupes = merge(osm, extra, cfg.get("dedupe_meters", 25))
    for s in sources[1:]:
        s["count"] = sum(1 for e in elements if e["src"] == s["id"])
    log(f"Merged: {len(elements):,} devices ({dupes} agency points already on OpenStreetMap)")

    if previous:
        try:
            prev_total = json.loads(http(previous, timeout=30)).get("total", 0)
        except Exception as err:  # first run, or site not live yet
            prev_total = 0
            log(f"No previous build to compare with ({err})")
        if prev_total and len(elements) < prev_total * (1 - cfg.get("max_drop_fraction", 0.3)):
            raise RuntimeError(f"Total fell from {prev_total:,} to {len(elements):,}; not publishing. "
                               "Run again later, or raise max_drop_fraction if the drop is real.")

    index = write_site(out, elements, cfg, sources, now)
    log(f"Wrote {index['tiles']:,} tiles to {out}/")
    return index


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--config", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "sources.json"))
    p.add_argument("--out", default="site")
    p.add_argument("--fixtures", help="read saved responses from this folder instead of the network")
    p.add_argument("--previous", help="URL of the last published index.json, for the drop check")
    p.add_argument("--opl", action="append", help="OpenStreetMap extract in OPL text (from osmium); repeatable")
    args = p.parse_args(argv)
    cfg = read_json(args.config)
    try:
        build(cfg, args.out, args.fixtures, args.previous, log=lambda m: print(m, flush=True), opl_files=args.opl)
    except RuntimeError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
