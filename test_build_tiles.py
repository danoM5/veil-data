"""Offline self-test for build_tiles.py. Runs before every publish; no network needed."""
import json
import os
import tempfile
import unittest

import build_tiles as bt

CFG = {
    "tile_size": 0.1,
    "dedupe_meters": 25,
    "max_drop_fraction": 0.3,
    "coverage": [{"name": "Test", "south": 41.0, "west": -94.0, "north": 43.0, "east": -92.0, "step": 2}],
    "osm": {"name": "OSM", "attribution": "© OpenStreetMap contributors", "license_url": "x", "endpoints": []},
    "arcgis": [{
        "id": "iowa-dot", "enabled": True, "name": "Iowa DOT", "attribution": "Iowa DOT", "url": "x",
        "tags": {"man_made": "surveillance", "surveillance": "outdoor", "surveillance:type": "ALPR"},
        "fields": {"operator": "applicant", "direction": "camerafacedirection"},
        "coded": {"devicetype": {"0": {"manufacturer": "Flock Safety", "manufacturer:wikidata": "Q108485435"}}},
    }],
}

OSM = {"elements": [
    {"type": "node", "id": 1, "lat": 41.5868, "lon": -93.6250,
     "tags": {"man_made": "surveillance", "surveillance:type": "ALPR", "manufacturer": "Flock Safety",
              "manufacturer:wikidata": "Q108485435", "camera:direction": "90", "name": "dropped"}},
    {"type": "node", "id": 2, "lat": 41.5900, "lon": -93.6000,
     "tags": {"man_made": "surveillance", "surveillance": "indoor"}},
    {"type": "way", "id": 3, "center": {"lat": 41.5912, "lon": -93.6105},
     "tags": {"man_made": "surveillance", "surveillance": "public", "surveillance:type": "camera"}},
    {"type": "node", "id": 4, "lat": 42.0300, "lon": -93.6300,
     "tags": {"man_made": "surveillance", "surveillance:type": "guard"}},
]}

ARCGIS = {"features": [
    # 10 m from OSM node 1: same device, must be dropped
    {"attributes": {"objectid": 1, "globalid": "{A}", "applicant": "Des Moines PD", "devicetype": 0,
                    "camerafacedirection": 90}, "geometry": {"x": -93.6250, "y": 41.58689}},
    # new device, Flock by device code
    {"attributes": {"objectid": 2, "globalid": "{B}", "applicant": "Ames PD", "devicetype": 0,
                    "camerafacedirection": 180}, "geometry": {"x": -93.6200, "y": 42.0300}},
    # new device, unknown maker
    {"attributes": {"objectid": 3, "globalid": "{C}", "applicant": "Story County", "devicetype": 2,
                    "camerafacedirection": None}, "geometry": {"x": -93.4500, "y": 42.0100}},
    # outside coverage: ignored
    {"attributes": {"objectid": 4, "globalid": "{D}", "applicant": "Elsewhere", "devicetype": 0},
     "geometry": {"x": -80.0, "y": 30.0}},
]}


class BuildTilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        fx = os.path.join(self.tmp.name, "fx")
        os.makedirs(os.path.join(fx, "osm"))
        os.makedirs(os.path.join(fx, "arcgis"))
        box = next(bt.boxes(CFG["coverage"]))
        with open(os.path.join(fx, "osm", "{}_{}_{}_{}.json".format(*box)), "w") as f:
            json.dump(OSM, f)
        with open(os.path.join(fx, "arcgis", "iowa-dot.json"), "w") as f:
            json.dump(ARCGIS, f)
        self.fx, self.out = fx, os.path.join(self.tmp.name, "site")

    def tearDown(self):
        self.tmp.cleanup()

    def build(self):
        return bt.build(CFG, self.out, fixtures=self.fx, log=lambda m: None)

    def test_kinds_match_the_app(self):
        self.assertEqual(bt.kind({"surveillance:type": "ALPR", "manufacturer": "Flock Safety"}), "flock")
        self.assertEqual(bt.kind({"surveillance:type": "ALPR", "manufacturer:wikidata": "Q108485435"}), "flock")
        self.assertEqual(bt.kind({"surveillance:type": "ALPR", "manufacturer": "Motorola"}), "plateReader")
        self.assertEqual(bt.kind({}), "securityCamera")
        self.assertIsNone(bt.kind({"surveillance": "indoor"}))
        self.assertIsNone(bt.kind({"surveillance:type": "guard"}))

    def test_tile_key_matches_app_rounding(self):
        self.assertEqual(bt.tile_key(34.11843, -118.30041, 0.1), (341, -1184))
        self.assertEqual(bt.tile_key(-0.05, -0.05, 0.1), (-1, -1))

    def test_boxes_cover_the_area_without_gaps(self):
        area = [{"south": 0, "west": 0, "north": 5, "east": 3, "step": 2}]
        got = list(bt.boxes(area))
        self.assertEqual(len(got), 6)
        self.assertEqual(sum((n - s) * (e - w) for s, w, n, e in got), 15)

    def test_build_merges_filters_and_dedupes(self):
        ix = self.build()
        self.assertEqual(ix["total"], 4)  # OSM 1 + OSM way 3 + Ames + Story County
        self.assertEqual(ix["counts"], {"flock": 2, "securityCamera": 1, "plateReader": 1})
        self.assertEqual({s["id"]: s["count"] for s in ix["sources"]}, {"osm": 2, "iowa-dot": 2})

    def test_tile_files_are_what_the_app_reads(self):
        self.build()
        path = os.path.join(self.out, "tiles", "415_-937.json")
        doc = bt.read_json((path))
        els = {e["id"]: e for e in doc["elements"]}
        self.assertEqual(els[1]["tags"]["direction"], "90")          # camera:direction folded in
        self.assertNotIn("name", els[1]["tags"])                      # unused tags dropped
        self.assertIn(bt.WAY_OFFSET + 3, els)                         # way ids can't clash with nodes
        ames = [e for e in bt.read_json((os.path.join(self.out, "tiles", "420_-937.json")))["elements"]
                if e["src"] == "iowa-dot"][0]
        self.assertLess(ames["id"], 0)
        self.assertEqual(ames["tags"]["operator"], "Ames PD")
        self.assertEqual(ames["tags"]["manufacturer"], "Flock Safety")
        self.assertIn("© OpenStreetMap contributors", doc["attribution"])

    def test_ids_are_stable_between_runs(self):
        a = bt.from_arcgis(ARCGIS["features"][1], CFG["arcgis"][0])
        b = bt.from_arcgis(ARCGIS["features"][1], CFG["arcgis"][0])
        self.assertEqual(a["id"], b["id"])

    def test_index_and_status_page(self):
        self.build()
        ix = bt.read_json((os.path.join(self.out, "index.json")))
        self.assertEqual(ix["coverage"], [[41.0, -94.0, 43.0, -92.0]])
        self.assertEqual(ix["tile_url"], "tiles/{lat}_{lon}.json")
        self.assertIn("VEIL map data", open(os.path.join(self.out, "index.html"), encoding="utf-8").read())

    def test_refuses_to_publish_after_a_big_drop(self):
        prev = os.path.join(self.tmp.name, "prev.json")
        with open(prev, "w") as f:
            json.dump({"total": 100}, f)
        with self.assertRaises(RuntimeError):
            bt.build(CFG, self.out, fixtures=self.fx, previous="file://" + prev, log=lambda m: None)
        self.assertFalse(os.path.exists(os.path.join(self.out, "index.json")))

    def test_real_config_is_valid(self):
        cfg = bt.read_json((os.path.join(os.path.dirname(os.path.abspath(__file__)), "sources.json")))
        n = len(list(bt.boxes(cfg["coverage"])))
        self.assertLess(n, 1000)  # stays well inside Overpass's fair-use limits
        for src in cfg["arcgis"]:
            self.assertTrue(src["url"].startswith("https://"))


if __name__ == "__main__":
    unittest.main()
