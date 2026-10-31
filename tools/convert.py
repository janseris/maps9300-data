#!/usr/bin/env python3
"""Mapy 9300 data: OpenStreetMap places and buildings as small static files for the Nokia 9300.

Reads an OSM extract (.osm.pbf, e.g. Geofabrik's czech-republic-latest.osm.pbf) and writes, per
zoom 15 cell (the same cells the app already uses for places, ~1.2 x 0.8 km here):

  <out>/v1/p/15/<x>/<y>.z   places: the app's Overpass CSV lines, zlib-compressed
  <out>/v1/d/15/<x>/<y>.z   place details: "<type> <id>\t<tags as JSON>" lines, zlib
  <out>/v1/b/15/<x>/<y>.z   buildings: binary outlines with heights (format below), zlib
  <out>/v1/meta.txt         date, source, counts and sizes

A cell with nothing in it has no file (HTTP 404 = empty). See FORMAT.md.

Usage: convert.py <extract.osm.pbf> <out dir> [--no-buildings]
Needs pyosmium (pip install osmium).
"""
import json
import math
import os
import struct
import sys
import time
import zlib
from collections import defaultdict

import osmium

ZOOM = 15

# The app's Overpass CSV columns after type|id|lat|lon (poi.cpp KTags; keep in the same order).
CSV_TAGS = ["name", "amenity", "shop", "tourism", "historic", "leisure", "military", "cuisine",
            "opening_hours", "website", "contact:website", "phone", "contact:phone", "addr:street",
            "addr:housenumber", "addr:city", "wheelchair"]

# Tags left out of the details (editing notes, not for a reader).
SKIP_PREFIXES = ("source", "note", "fixme", "FIXME", "check_date", "survey", "created_by",
                 "ref:ruian", "ref:ruian:", "uir_adr", "lastcheck", "last_checked")

LEISURE_PLACES = {"park", "playground", "garden"}
NODE_AMENITIES = {"drinking_water", "parking", "shelter", "toilets", "atm", "charging_station"}


def is_place(tags, osm_type):
    """The same selection as the app's Overpass query (poi.cpp PoiQuery)."""
    named = "name" in tags
    if named and ("amenity" in tags or "shop" in tags or "tourism" in tags):
        return True
    if "historic" in tags:
        return True
    if tags.get("leisure") in LEISURE_PLACES:
        return True
    if tags.get("military") == "bunker":
        return True
    if osm_type == "node" and tags.get("amenity") in NODE_AMENITIES:
        return True
    if osm_type == "way" and tags.get("amenity") == "parking":
        return True
    return False


def cell_of(lat, lon):
    n = 1 << ZOOM
    x = int((lon + 180.0) / 360.0 * n)
    r = math.radians(lat)
    y = int((1.0 - math.log(math.tan(r) + 1.0 / math.cos(r)) / math.pi) / 2.0 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def cell_bounds(x, y):
    """(west, north, east, south) of a cell in degrees."""
    n = float(1 << ZOOM)
    def lat(yy):
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy / n))))
    return x / n * 360.0 - 180.0, lat(y), (x + 1) / n * 360.0 - 180.0, lat(y + 1)


def clean(v):
    return v.replace("|", "/").replace("\n", " ").replace("\r", " ")


def csv_line(osm_type, osm_id, lat, lon, tags):
    cols = [osm_type, str(osm_id), "%.7f" % lat, "%.7f" % lon]
    cols += [clean(tags.get(k, "")) for k in CSV_TAGS]
    return "|".join(cols)


def detail_tags(tags):
    return {k: v for k, v in tags.items() if not k.startswith(SKIP_PREFIXES)}


def height_of(tags):
    """Height in metres (0: unknown) and levels (0: unknown) from height / building:levels."""
    h = 0.0
    v = tags.get("height") or tags.get("building:height")
    if v:
        try:
            h = float(v.split()[0].replace(",", ".").rstrip("m"))
        except ValueError:
            h = 0.0
    levels = 0
    v = tags.get("building:levels")
    if v:
        try:
            levels = int(float(v.replace(",", ".")))
        except ValueError:
            levels = 0
    return h, levels


def zigzag(v):
    return v << 1 if v >= 0 else ((-v) << 1) - 1


def varint(v):
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


class Collector(osmium.SimpleHandler):
    def __init__(self, buildings):
        super().__init__()
        self.want_buildings = buildings
        self.places = defaultdict(list)       # cell -> csv lines
        self.details = defaultdict(list)      # cell -> detail lines
        self.buildings = defaultdict(list)    # cell -> (height, levels, [(lat, lon)])
        self.n_places = 0
        self.n_buildings = 0

    def add_place(self, osm_type, osm_id, lat, lon, tags):
        c = cell_of(lat, lon)
        self.places[c].append(csv_line(osm_type, osm_id, lat, lon, tags))
        self.details[c].append("%s %d\t%s" % (osm_type, osm_id,
                               json.dumps(detail_tags(tags), ensure_ascii=False, separators=(",", ":"))))
        self.n_places += 1

    def add_building(self, tags, ring):
        if len(ring) < 3:
            return
        lat = sum(p[0] for p in ring) / len(ring)
        lon = sum(p[1] for p in ring) / len(ring)
        h, levels = height_of(tags)
        self.buildings[cell_of(lat, lon)].append((h, levels, ring))
        self.n_buildings += 1

    def node(self, n):
        if not n.tags:
            return
        tags = {t.k: t.v for t in n.tags}
        if is_place(tags, "node"):
            self.add_place("node", n.id, n.location.lat, n.location.lon, tags)

    def way(self, w):
        if not w.tags:
            return
        tags = {t.k: t.v for t in w.tags}
        place = is_place(tags, "way")
        building = self.want_buildings and "building" in tags and w.is_closed()
        if not place and not building:
            return
        try:
            pts = [(nd.location.lat, nd.location.lon) for nd in w.nodes if nd.location.valid()]
        except osmium.InvalidLocationError:
            return
        if not pts:
            return
        if place:
            # Overpass's "center" for a way: the middle of its bounding box
            lats = [p[0] for p in pts]
            lons = [p[1] for p in pts]
            self.add_place("way", w.id, (min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2, tags)
        if building:
            self.add_building(tags, pts[:-1] if pts[0] == pts[-1] else pts)

    def area(self, a):
        # multipolygon relations only (closed ways were handled in way())
        if a.from_way():
            return
        tags = {t.k: t.v for t in a.tags}
        place = is_place(tags, "relation")
        building = self.want_buildings and "building" in tags
        if not place and not building:
            return
        rings = []
        for outer in a.outer_rings():
            rings.append([(nd.lat, nd.lon) for nd in outer if nd.location.valid()])
        rings = [r for r in rings if len(r) >= 3]
        if not rings:
            return
        if place:
            lats = [p[0] for r in rings for p in r]
            lons = [p[1] for r in rings for p in r]
            self.add_place("relation", a.orig_id(), (min(lats) + max(lats)) / 2,
                           (min(lons) + max(lons)) / 2, tags)
        if building:
            for r in rings:
                self.add_building(tags, r[:-1] if r[0] == r[-1] else r)


def building_blob(cell, items):
    """Binary outlines, positions in 1/65536 of the cell (~2 cm), little-endian:
    "MB1\\0", u32 count; each: u16 height in 0.1 m (0 unknown), u8 levels (0 unknown),
    u16 points, the first point as i32 x, i32 y (a building may reach past its cell, whose
    cell holds its centre), then zigzag varint deltas of x and y."""
    west, north, east, south = cell_bounds(*cell)
    out = bytearray(b"MB1\0")
    out += struct.pack("<I", len(items))
    for h, levels, ring in items:
        pts = []
        for lat, lon in ring:
            x = int(round((lon - west) / (east - west) * 65535))
            y = int(round((north - lat) / (north - south) * 65535))
            pts.append((x, y))
        out += struct.pack("<HBH", min(int(round(h * 10)), 65535), min(levels, 255), len(pts))
        x0, y0 = pts[0]
        out += struct.pack("<ii", x0, y0)
        px, py = x0, y0
        for x, y in pts[1:]:
            out += varint(zigzag(x - px)) + varint(zigzag(y - py))
            px, py = x, y
    return bytes(out)


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    z = zlib.compress(data, 9)
    with open(path, "wb") as f:
        f.write(z)
    return len(z)


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    src, out = sys.argv[1], sys.argv[2]
    buildings = "--no-buildings" not in sys.argv
    t0 = time.time()
    c = Collector(buildings)
    c.apply_file(src, locations=True, idx="flex_mem")
    sizes = {"p": [0, 0], "d": [0, 0], "b": [0, 0]}
    root = os.path.join(out, "v1")
    for cell, lines in c.places.items():
        sizes["p"][0] += 1
        sizes["p"][1] += write(os.path.join(root, "p", str(ZOOM), str(cell[0]), "%d.z" % cell[1]),
                               ("\n".join(lines) + "\n").encode("utf-8"))
    for cell, lines in c.details.items():
        sizes["d"][0] += 1
        sizes["d"][1] += write(os.path.join(root, "d", str(ZOOM), str(cell[0]), "%d.z" % cell[1]),
                               ("\n".join(lines) + "\n").encode("utf-8"))
    for cell, items in c.buildings.items():
        sizes["b"][0] += 1
        sizes["b"][1] += write(os.path.join(root, "b", str(ZOOM), str(cell[0]), "%d.z" % cell[1]),
                               building_blob(cell, items))
    meta = [
        "format 1",
        "date %s" % time.strftime("%Y-%m-%d", time.gmtime()),
        "source %s" % os.path.basename(src),
        "places %d in %d cells, %d bytes" % (c.n_places, sizes["p"][0], sizes["p"][1]),
        "details %d cells, %d bytes" % (sizes["d"][0], sizes["d"][1]),
        "buildings %d in %d cells, %d bytes" % (c.n_buildings, sizes["b"][0], sizes["b"][1]),
        "seconds %d" % (time.time() - t0),
        "Data (c) OpenStreetMap contributors, ODbL 1.0, https://www.openstreetmap.org/copyright",
    ]
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "meta.txt"), "w") as f:
        f.write("\n".join(meta) + "\n")
    print("\n".join(meta))


if __name__ == "__main__":
    main()
