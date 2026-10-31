# Mapy 9300 data

OpenStreetMap places and buildings for **Mapy 9300 native** (a map app for the Nokia 9300), cut into
small static files so the phone can download only the areas it shows, with no live query server.
Built monthly by GitHub Actions from [Geofabrik](https://download.geofabrik.de/)'s Czech Republic
extract and published on GitHub Pages.

Data © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright), available under the
Open Database License (ODbL 1.0).

## Files

One file per **zoom 15 cell** (standard XYZ tile numbers, ~1.2 × 0.8 km in Czechia), each
zlib-compressed (`uncompress()` in zlib). A cell with nothing in it has no file: **HTTP 404 means
empty**.

| Path | Content (after decompression) |
|---|---|
| `v1/p/15/<x>/<y>.z` | Places: one line each, `|`-separated, the same columns as the app's Overpass CSV: `type|id|lat|lon|name|amenity|shop|tourism|historic|leisure|military|cuisine|opening_hours|website|contact:website|phone|contact:phone|addr:street|addr:housenumber|addr:city|wheelchair` (UTF-8; `|` and newlines inside values replaced) |
| `v1/d/15/<x>/<y>.z` | Place details: `<type> <id>` TAB `<all tags as one JSON object>` per line, without editing notes (`source*`, `note`, `fixme`, `check_date`...) |
| `v1/b/15/<x>/<y>.z` | Buildings (binary, little-endian): `"MB1\0"`, u32 count; per building: u16 height in 0.1 m (0 = unknown; from `height`), u8 levels (0 = unknown; `building:levels`), u16 point count, the first point as i32 x, i32 y, then zigzag varint deltas of x and y. x and y are in 1/65536 of the cell from its north-west corner (~2 cm); a building is in the cell of its centre and may reach past it. Outer rings only |
| `v1/meta.txt` | Build date, source, counts and sizes |

Which places: the same as the app's Overpass query: named amenities, shops and tourism; all historic
objects; parks, playgrounds, gardens; bunkers; drinking water, parking, shelters, toilets, ATMs and
charging stations (nodes); parking areas. A way or relation's position is the middle of its bounding
box, as Overpass's `center`.

## Building

```
pip install osmium
python tools/convert.py czech-republic-latest.osm.pbf site     # --no-buildings: places only
```

The workflow `.github/workflows/build-data.yml` runs it on the 2nd of every month (and on demand: Actions →
build-data → Run workflow) and deploys `site/` to GitHub Pages.

## Setup (once)

1. Move `tools/build-data.yml` to `.github/workflows/build-data.yml` (the remote tools may not
   write into `.github/`). Create a **public** GitHub repository (Pages on a free account needs a
   public one) and push this folder to it.
2. Settings → Pages → Source: **GitHub Actions**.
3. Actions → build-data → **Run workflow**. The log ends with `meta.txt`: counts and sizes.
4. The data is then at `https://<user>.github.io/<repo>/v1/meta.txt`.
