#!/usr/bin/env python3
"""
build_airspaces_us.py — FAA GeoJSON → airspaces.json converter.

Produces an airspaces.json file matching the schema in
shared/airspaces.py from the FAA's published GeoJSON exports.  Run
this on your laptop (any machine with Python 3.8+); drop the output
file at pi_zero/data/airspaces/airspaces.json.

────────────────────────────────────────────────────────────────────
Where to get the input files
────────────────────────────────────────────────────────────────────

The FAA publishes Class Airspace and Special Use Airspace as GeoJSON
through its ArcGIS open-data portal.  Search for these datasets:

    "Class Airspace"             — B/C/D boundaries
    "Special Use Airspace"       — MOA / Restricted / Prohibited

On each dataset page, click "I want to use this" → "Download" →
choose "GeoJSON".  You'll get a single .geojson file per dataset.

A typical 28-day refresh: download the two GeoJSON files, run this
script, replace pi_zero/data/airspaces/airspaces.json.

────────────────────────────────────────────────────────────────────
Usage
────────────────────────────────────────────────────────────────────

    python3 tools/build_airspaces_us.py \\
        --class-airspace  Class_Airspace.geojson \\
        --sua             Special_Use_Airspace.geojson \\
        --out             airspaces.json

    # Either input is optional — pass one or both.
    # --out defaults to airspaces.json in the current directory.

────────────────────────────────────────────────────────────────────
Schema produced
────────────────────────────────────────────────────────────────────

See the docstring at the top of shared/airspaces.py for the full
schema.  Each record:

    {
      "class":      "B" | "C" | "D" | "MOA" | "R",
      "ident":      short identifier (≤ 16 chars)
      "name":       full descriptive name
      "floor_ft":   integer MSL (surface = 0)
      "ceiling_ft": integer MSL
      "polygon":    [[lat, lon], ...]   # closed ring, ≥ 3 points
    }

MultiPolygon features in the source GeoJSON are split into one
record per sub-polygon (sharing the parent's metadata).  Polygons
with fewer than 3 points are dropped.
"""

import argparse
import json
import re
import sys
from pathlib import Path


# ── Class / type → schema-class mapping ─────────────────────────────────
# FAA datasets use different field names + values for class.  Each
# pattern is matched case-insensitively against the entire property
# string (LOCAL_TYPE / TYPE_CODE / CLASS_CODE / etc.); the first match
# wins.  Add patterns here when you encounter a new FAA field-name flavour.

_CLASS_MAP = (
    # Class airspace (B/C/D) — FAA's CLASS_CODE is usually one letter
    # but sometimes "Class B"; we tolerate both.
    (re.compile(r"^\s*B\b|^\s*Class\s*B\b",  re.I), "B"),
    (re.compile(r"^\s*C\b|^\s*Class\s*C\b",  re.I), "C"),
    (re.compile(r"^\s*D\b|^\s*Class\s*D\b",  re.I), "D"),
    # SUA — TYPE_CODE values are "MOA", "R-####", "P-####", "A-####",
    # "W-####".  We treat Prohibited / Alert / Warning as "R" for
    # rendering (all critical, drawn in red); the name still encodes
    # which sub-type so the pilot can read it on the map.
    (re.compile(r"MOA",                       re.I), "MOA"),
    (re.compile(r"^R-|Restricted",            re.I), "R"),
    (re.compile(r"^P-|Prohibited",            re.I), "R"),
    (re.compile(r"^W-|Warning",               re.I), "R"),
    (re.compile(r"^A-|Alert",                 re.I), "R"),
)


def classify(*candidates):
    """Return the schema class string from a series of candidate
    property values, or None if nothing matches.  Tries each candidate
    in order — the first non-empty string that matches a _CLASS_MAP
    pattern wins."""
    for cand in candidates:
        if not cand:
            continue
        s = str(cand)
        for pat, cls in _CLASS_MAP:
            if pat.search(s):
                return cls
    return None


# ── Altitude string parsing ─────────────────────────────────────────────
# FAA encodes altitudes in a few different ways across datasets.  We
# handle the common ones:
#   "SFC", "GND", "Surface"        → 0   (surface)
#   "UNL", "UNLIMITED"             → 99000   (sentinel high)
#   "FL180", "FL 180"              → 18000
#   "12,500", "12500", "12500 ft"  → 12500
#   "12500 MSL", "MSL 12500"       → 12500
#   "1500 AGL"                     → still 1500 (we don't separately
#                                    track AGL; pilots get a reasonable
#                                    approximation)

_ALT_FL_RE   = re.compile(r"FL\s*(\d+)",            re.I)
_ALT_NUM_RE  = re.compile(r"(-?\d[\d,]*)")
_ALT_SFC_RE  = re.compile(r"(?:SFC|GND|Surface)",   re.I)
_ALT_UNL_RE  = re.compile(r"(?:UNL|UNLIMIT)",       re.I)


def parse_altitude_ft(s, default=0):
    """Parse a FAA altitude string to integer feet MSL.  Returns
    `default` on a value we can't make sense of."""
    if s is None:
        return default
    s = str(s).strip()
    if not s:
        return default
    if _ALT_SFC_RE.search(s):
        return 0
    if _ALT_UNL_RE.search(s):
        return 99000
    fl = _ALT_FL_RE.search(s)
    if fl:
        return int(fl.group(1)) * 100
    num = _ALT_NUM_RE.search(s)
    if num:
        try:
            return int(num.group(1).replace(",", ""))
        except ValueError:
            return default
    return default


# ── GeoJSON feature → schema record ─────────────────────────────────────


def _ident_from(props):
    """Pick a short ident from common FAA property names.  Returns
    upper-case, truncated to 16 chars."""
    for key in ("IDENT", "DESIGNATOR", "AIRSPACE_DESIG", "ICAO_ID",
                "ICAO", "NAME", "AIRSPACE_NAM"):
        v = props.get(key)
        if v:
            return str(v).strip().upper()[:16]
    return ""


def _name_from(props):
    for key in ("NAME", "AIRSPACE_NAM", "FACILITY",
                "FULL_NAME", "DESCRIPTION"):
        v = props.get(key)
        if v:
            return str(v).strip()
    return ""


def _floor_from(props):
    for key in ("LOWER_VAL", "LOWER_DESC", "FLOOR", "ALT_LOW",
                "LOW_ALT", "LO_ALT", "LOWER_LIMIT", "FROM_ALT"):
        if key in props and props[key] not in (None, ""):
            return parse_altitude_ft(props[key])
    return 0


def _ceiling_from(props):
    for key in ("UPPER_VAL", "UPPER_DESC", "CEILING", "ALT_HIGH",
                "HIGH_ALT", "HI_ALT", "UPPER_LIMIT", "TO_ALT"):
        if key in props and props[key] not in (None, ""):
            return parse_altitude_ft(props[key])
    return 0


def _polygons_from(geometry):
    """Yield closed rings as [(lat, lon), ...] from a GeoJSON geometry.
    Handles Polygon and MultiPolygon; ignores inner rings (holes)
    because our renderer doesn't draw them yet."""
    if not geometry:
        return
    gtype = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if gtype == "Polygon":
        if coords and len(coords[0]) >= 3:
            yield [(float(p[1]), float(p[0])) for p in coords[0]]
    elif gtype == "MultiPolygon":
        for poly in coords:
            if poly and len(poly[0]) >= 3:
                yield [(float(p[1]), float(p[0])) for p in poly[0]]


def feature_to_records(feature, class_hint=None):
    """Convert one GeoJSON feature → 0 or more schema records.
    `class_hint` lets the caller force a class when the source file
    is class-specific (e.g. SUA features that don't all label
    themselves)."""
    props = feature.get("properties", {}) or {}
    geometry = feature.get("geometry", {}) or {}

    cls = class_hint or classify(
        props.get("TYPE_CODE"), props.get("CLASS_CODE"),
        props.get("LOCAL_TYPE"), props.get("CLASS"),
        props.get("TYPE"), _name_from(props),
    )
    if cls is None:
        return

    ident = _ident_from(props)
    name  = _name_from(props)
    flr   = _floor_from(props)
    clg   = _ceiling_from(props)

    for poly in _polygons_from(geometry):
        yield {
            "class":      cls,
            "ident":      ident,
            "name":       name,
            "floor_ft":   int(flr),
            "ceiling_ft": int(clg),
            "polygon":    [[lat, lon] for lat, lon in poly],
        }


def build_from_dir(data_dir, source_note=""):
    """Find every *.geojson file in `data_dir`, run the FAA →
    airspaces.json conversion across all of them, write the combined
    output to `<data_dir>/airspaces.json`.  Returns a stats dict
    suitable for surfacing in a UI.

    This is the entrypoint the Pi-side AIRSPACE DATA screens call to
    do the conversion in-process — no separate laptop step needed."""
    from pathlib import Path as _P
    out_records = []
    stats = {"B": 0, "C": 0, "D": 0, "MOA": 0, "R": 0,
             "files": 0, "skipped_no_class": 0,
             "skipped_no_polygon": 0, "errors": []}
    data_path = _P(data_dir)
    if not data_path.is_dir():
        stats["errors"].append(f"directory not found: {data_dir}")
        return stats
    for gj in sorted(data_path.glob("*.geojson")):
        stats["files"] += 1
        try:
            features = load_geojson(gj)
        except (json.JSONDecodeError, ValueError, OSError) as e:
            stats["errors"].append(f"{gj.name}: {e}")
            continue
        for feat in features:
            had_class = had_poly = False
            for rec in feature_to_records(feat):
                had_class = had_poly = True
                out_records.append(rec)
                stats[rec["class"]] = stats.get(rec["class"], 0) + 1
            if not had_class:
                stats["skipped_no_class"] += 1
            elif not had_poly:
                stats["skipped_no_polygon"] += 1
    out_path = data_path / "airspaces.json"
    try:
        out_path.write_text(json.dumps({
            "version": 1,
            "source":  source_note or "FAA GeoJSON (pi-side build)",
            "airspaces": out_records,
        }, indent=2))
    except OSError as e:
        stats["errors"].append(f"write {out_path.name}: {e}")
    stats["records"] = len(out_records)
    stats["output"]  = str(out_path)
    return stats


def load_geojson(path):
    """Load a GeoJSON file and return the feature list."""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if data.get("type") == "FeatureCollection":
        return data.get("features", [])
    if data.get("type") == "Feature":
        return [data]
    raise ValueError(f"{path}: not a GeoJSON Feature/FeatureCollection")


# ── Main ────────────────────────────────────────────────────────────────


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[1].strip())
    p.add_argument("--class-airspace", help="GeoJSON file with B/C/D")
    p.add_argument("--sua",            help="GeoJSON file with SUA (MOA/R/P)")
    p.add_argument("--out", default="airspaces.json",
                   help="output file (default: airspaces.json)")
    p.add_argument("--source-note", default="",
                   help="optional 'source' string written into the output")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="print one line per emitted record")
    args = p.parse_args()

    if not args.class_airspace and not args.sua:
        p.error("at least one of --class-airspace / --sua is required")

    out_records = []
    stats = {"B": 0, "C": 0, "D": 0, "MOA": 0, "R": 0,
             "skipped_no_class": 0, "skipped_no_polygon": 0}

    def _process(path, class_hint=None, label=""):
        if not path:
            return
        path = Path(path)
        if not path.exists():
            print(f"  ⚠ {path} not found — skipped", file=sys.stderr)
            return
        print(f"  reading {path}{label}", file=sys.stderr)
        try:
            features = load_geojson(path)
        except (json.JSONDecodeError, ValueError) as e:
            print(f"  ✗ {path}: {e}", file=sys.stderr)
            return
        before = len(out_records)
        for feat in features:
            had_class = False
            had_poly  = False
            for rec in feature_to_records(feat, class_hint=class_hint):
                had_class = True
                had_poly  = True
                out_records.append(rec)
                stats[rec["class"]] = stats.get(rec["class"], 0) + 1
                if args.verbose:
                    print(f"    + {rec['class']:<4} {rec['ident']:<10} "
                          f"{rec['floor_ft']:>5}-{rec['ceiling_ft']:>5} ft  "
                          f"{rec['name']}")
            if not had_class:
                stats["skipped_no_class"] += 1
            elif not had_poly:
                stats["skipped_no_polygon"] += 1
        added = len(out_records) - before
        print(f"    {added} record(s) from {path.name}", file=sys.stderr)

    _process(args.class_airspace, class_hint=None,
             label=" (Class B/C/D)")
    _process(args.sua,            class_hint=None,
             label=" (SUA)")

    # Write the output
    out = {
        "version": 1,
        "source":  args.source_note or "FAA GeoJSON (Class Airspace + SUA)",
        "airspaces": out_records,
    }
    Path(args.out).write_text(json.dumps(out, indent=2))
    print(f"\nwrote {args.out}", file=sys.stderr)
    print(f"  total records: {len(out_records)}", file=sys.stderr)
    for cls in ("B", "C", "D", "MOA", "R"):
        print(f"    {cls:<3}: {stats.get(cls, 0)}", file=sys.stderr)
    if stats["skipped_no_class"]:
        print(f"  skipped (class unclassified): {stats['skipped_no_class']}",
              file=sys.stderr)
    if stats["skipped_no_polygon"]:
        print(f"  skipped (no polygon):         {stats['skipped_no_polygon']}",
              file=sys.stderr)


if __name__ == "__main__":
    main()
