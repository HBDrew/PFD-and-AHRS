"""
airspaces.py — US airspace polygon loader + spatial query.

Data model: a list of airspace records, each describing one airspace
boundary as a closed polygon in lat/lon with class + altitude limits.

Supported classes (first-cut, US only):
    "B"   Class B (terminal control area, surface up to ceiling)
    "C"   Class C (typical 10 nm radius, surface up to 4000 AGL)
    "D"   Class D (typical 4 nm radius, surface up to 2500 AGL)
    "MOA" Military Operations Area
    "R"   Restricted (R-####)

Storage format on disk (data_dir/airspaces.json):

    {
      "version": 1,
      "source": "FAA NASR YYYY-MM-DD" or similar,
      "airspaces": [
        {
          "class":      "B",
          "ident":      "PHX",                # short label drawn on map
          "name":       "Phoenix Class B",
          "floor_ft":   0,                    # MSL; surface = 0
          "ceiling_ft": 10000,
          "polygon":    [[lat1, lon1], [lat2, lon2], ...]
        },
        ...
      ]
    }

The first-cut bundles a minimal example dataset (a few real airspaces
around the southwest US) so the render path can be verified end-to-end
before the pilot drops in a full nationwide file.  Production builds
should overwrite `airspaces.json` with NASR-derived data.

Spatial query: bbox-based.  Each airspace's bbox is precomputed at
load time; query_nearby returns those whose bbox intersects the view
rect.  The render path then clips polygon edges against the view.
For ~200 US airspaces this is a few microseconds per frame even on
the Pi Zero.
"""

import json
import math
import os

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False


CACHE_FILENAME = "airspaces.json"

# ── Direct-download sources ───────────────────────────────────────────
# Map of `output filename → download URL`.  The AIRSPACE DATA screens
# on both Pis iterate this dict, fetch each URL, save it under the
# given filename in AIRSPACE_DIR, then auto-run the *.geojson →
# airspaces.json converter.
#
# Defaults below use the FAA Open Data ArcGIS portal — the canonical
# source for US Class Airspace and Special Use Airspace polygons.  The
# `opendata.arcgis.com/api/v3/datasets/<UUID>_<LAYER>/downloads/data`
# pattern is stable; the UUIDs are tied to the FAA's published
# datasets and only change if the FAA re-publishes under a new item
# (rare — typical 28-day chart cycle updates the DATA, not the URL).
#
# If a default 404s in the future:
#   1. Visit https://adds-faa.opendata.arcgis.com/
#   2. Find "Class Airspace" (or "Special Use Airspace")
#   3. Click "I want to use this" → "Download" → "GeoJSON"
#   4. Copy the URL → paste it here.
#
# Leaving a URL empty disables that source — BUILD still works on any
# *.geojson files the pilot drops in manually.
DOWNLOAD_SOURCES = {
    "class_airspace.geojson": (
        "https://hub.arcgis.com/api/v3/datasets/"
        "c6a62360338e408cb1512366ad61559e_0/downloads/data"
        "?format=geojson&spatialRefId=4326&where=1%3D1"
    ),
    "special_use_airspace.geojson": (
        "https://hub.arcgis.com/api/v3/datasets/"
        "dd0d1b726e504137ab3c41b21835d05b_0/downloads/data"
        "?format=geojson&spatialRefId=4326&where=1%3D1"
    ),
}
# Compat shim — older callers read a single DOWNLOAD_URL; we now expose
# the dict but keep the name around so any code still referencing it
# treats absence as "no auto-download configured".
DOWNLOAD_URL = ""

# Valid class strings — anything else gets silently dropped at load.
VALID_CLASSES = ("B", "C", "D", "MOA", "R")

# Bundled tiny example so the render path works without a download.
# Real-world data should overwrite the json file at install / setup.
# Polygons are approximate; for actual flight reference download a
# fresh NASR-based file.
_EXAMPLE_DATA = {
    "version": 1,
    "source":  "bundled-example",
    "airspaces": [
        # KPHX Class B — rough hexagonal approximation around Sky Harbor.
        {"class": "B", "ident": "PHX",
         "name":  "Phoenix Class B",
         "floor_ft": 0, "ceiling_ft": 10000,
         "polygon": [
             [33.770, -112.295], [33.685, -112.405],
             [33.305, -112.355], [33.220, -112.045],
             [33.305, -111.760], [33.685, -111.715],
             [33.770, -112.295],
         ]},
        # KFLG Class D — rough 4-nm circle around Flagstaff Pulliam.
        {"class": "D", "ident": "FLG",
         "name":  "Flagstaff Class D",
         "floor_ft": 7014, "ceiling_ft": 9700,
         "polygon": _circle_polygon_placeholder(35.1385, -111.671, 4.4)
                    if False else [
             [35.207, -111.671], [35.181, -111.581],
             [35.122, -111.554], [35.071, -111.626],
             [35.075, -111.730], [35.137, -111.790],
             [35.199, -111.752], [35.207, -111.671],
         ]},
        # KPRC Class D — Prescott.
        {"class": "D", "ident": "PRC",
         "name":  "Prescott Class D",
         "floor_ft": 5045, "ceiling_ft": 7600,
         "polygon": [
             [34.708, -112.420], [34.682, -112.343],
             [34.616, -112.345], [34.594, -112.421],
             [34.628, -112.499], [34.696, -112.481],
             [34.708, -112.420],
         ]},
        # Bagdad MOA — generic rectangle west of Prescott.
        {"class": "MOA", "ident": "BAGDAD",
         "name":  "Bagdad MOA",
         "floor_ft": 500, "ceiling_ft": 18000,
         "polygon": [
             [34.95, -113.30], [34.95, -112.85],
             [34.45, -112.85], [34.45, -113.30],
             [34.95, -113.30],
         ]},
        # R-2304 — generic restricted area north of Phoenix.
        {"class": "R", "ident": "R-2304",
         "name":  "Gladden Restricted",
         "floor_ft": 0, "ceiling_ft": 9000,
         "polygon": [
             [33.85, -113.50], [33.85, -113.10],
             [33.55, -113.10], [33.55, -113.50],
             [33.85, -113.50],
         ]},
    ],
}


def _circle_polygon_placeholder(lat, lon, radius_nm, n=12):
    """Generate a regular polygon approximation of a circle.  Unused in
    the current example data (idents have hand-tuned polygons) but kept
    for the production loader's MOA / restricted-area fallback when
    only a centre+radius is in the source dataset."""
    cos_lat = max(0.05, math.cos(math.radians(lat)))
    out = []
    for i in range(n + 1):
        a = i * 2 * math.pi / n
        d_lat = radius_nm / 60.0 * math.cos(a)
        d_lon = radius_nm / 60.0 / cos_lat * math.sin(a)
        out.append([lat + d_lat, lon + d_lon])
    return out


def _bbox_of(polygon):
    """Return (lat_min, lat_max, lon_min, lon_max) for a polygon."""
    lats = [p[0] for p in polygon]
    lons = [p[1] for p in polygon]
    return (min(lats), max(lats), min(lons), max(lons))


def load(data_dir: str):
    """Load airspaces from data_dir/airspaces.json.  Returns a list of
    record dicts with bboxes precomputed, or None when no file exists.

    Each record:
        {class, ident, name, floor_ft, ceiling_ft, polygon, bbox}
    where bbox = (lat_min, lat_max, lon_min, lon_max)."""
    path = os.path.join(data_dir, CACHE_FILENAME)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return None
    return _records_from_raw(raw)


def load_bundled_example():
    """Return the bundled small example dataset.  Used as a startup
    fallback when no airspaces.json has been provided yet, so the
    render path can be visually verified."""
    return _records_from_raw(_EXAMPLE_DATA)


def _records_from_raw(raw):
    out = []
    for entry in raw.get("airspaces", []):
        cls = str(entry.get("class", "")).upper()
        if cls not in VALID_CLASSES:
            continue
        poly = entry.get("polygon") or []
        if len(poly) < 3:
            continue
        try:
            poly = [(float(p[0]), float(p[1])) for p in poly]
        except (TypeError, ValueError, IndexError):
            continue
        out.append({
            "class":      cls,
            "ident":      str(entry.get("ident", "")),
            "name":       str(entry.get("name", "")),
            "floor_ft":   int(entry.get("floor_ft",   0)),
            "ceiling_ft": int(entry.get("ceiling_ft", 0)),
            "polygon":    poly,
            "bbox":       _bbox_of(poly),
        })
    return out


_NM_PER_DEG_LAT = 60.0


def query_nearby(airspaces, lat, lon, radius_nm):
    """Return airspace records whose bbox overlaps a lat/lon disk of
    radius_nm centred on (lat, lon).  Cheap bbox cull — caller does
    the polygon-vs-view clip in screen space."""
    if not airspaces:
        return []
    cos_lat = max(0.05, math.cos(math.radians(lat)))
    d_lat = radius_nm / _NM_PER_DEG_LAT
    d_lon = radius_nm / _NM_PER_DEG_LAT / cos_lat
    lat_lo = lat - d_lat; lat_hi = lat + d_lat
    lon_lo = lon - d_lon; lon_hi = lon + d_lon
    out = []
    for a in airspaces:
        bla_lo, bla_hi, blo_lo, blo_hi = a["bbox"]
        if (bla_hi < lat_lo or bla_lo > lat_hi
                or blo_hi < lon_lo or blo_lo > lon_hi):
            continue
        out.append(a)
    return out


def write_example(data_dir: str):
    """Write the bundled example dataset to data_dir/airspaces.json.
    Useful as a setup helper so the user has SOMETHING to see while
    they figure out how to drop in real NASR-derived data."""
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, CACHE_FILENAME), "w",
              encoding="utf-8") as fh:
        json.dump(_EXAMPLE_DATA, fh, indent=2)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        if sys.argv[1] == "write-example":
            write_example(sys.argv[2] if len(sys.argv) > 2 else "data")
            print(f"wrote {CACHE_FILENAME} (example dataset)")
        elif sys.argv[1] == "dump":
            records = load(sys.argv[2] if len(sys.argv) > 2 else "data")
            if records is None:
                print("no airspaces.json found")
            else:
                for r in records:
                    print(f"  {r['class']:<3} {r['ident']:<10} "
                          f"{r['floor_ft']:>5}-{r['ceiling_ft']:>5} ft  "
                          f"{r['name']}")
