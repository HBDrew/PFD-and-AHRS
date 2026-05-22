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
import os
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
    # TFRs come from per-type datasets (stadium, defense, etc.) —
    # checked before Restricted so a "Defense TFR" labelled as some
    # restricted-area variant still classifies as TFR.
    (re.compile(r"TFR|Stadium|Sport|Defense", re.I), "TFR"),
    # SUA — TYPE_CODE values are "MOA", "R-####", "P-####", "A-####",
    # "W-####".  Prohibited / Alert / Warning historically all read
    # as "R"; we now split P (Prohibited) into its own class for
    # heavier visual emphasis.
    (re.compile(r"MOA",                       re.I), "MOA"),
    (re.compile(r"^P-|Prohibited",            re.I), "P"),
    (re.compile(r"^R-|Restricted",            re.I), "R"),
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


def _safe_ring(coords):
    """Return [(lat, lon), ...] from a list of [lon, lat] pairs.
    Skips any individual vertex whose coords aren't two finite floats
    (the FAA dumps occasionally include null coords or 3D points where
    the third entry is altitude or NaN).  Returns None if fewer than
    3 valid vertices remain — caller drops the ring."""
    out = []
    for p in coords:
        if not isinstance(p, (list, tuple)) or len(p) < 2:
            continue
        try:
            lon = float(p[0]); lat = float(p[1])
        except (TypeError, ValueError):
            continue
        # Bounds sanity — anything past these is corrupt.  Floats
        # comparisons here also reject NaN (NaN < NaN is False, so
        # the conjunction fails).
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            continue
        out.append((lat, lon))
    return out if len(out) >= 3 else None


def _polygons_from(geometry):
    """Yield closed rings as [(lat, lon), ...] from a GeoJSON geometry.
    Handles Polygon and MultiPolygon; ignores inner rings (holes)
    because our renderer doesn't draw them yet.  Defensive — bad
    vertices and bad ring shapes get dropped silently rather than
    crashing the build."""
    if not geometry:
        return
    gtype = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if gtype == "Polygon" and coords:
        ring = _safe_ring(coords[0])
        if ring:
            yield ring
    elif gtype == "MultiPolygon":
        for poly in coords:
            if not poly:
                continue
            ring = _safe_ring(poly[0])
            if ring:
                yield ring


# Douglas-Peucker simplification tolerance in degrees.  At mid-latitudes
# 1 deg lat ≈ 60 NM, so 0.002 deg ≈ 0.12 NM — well below per-pixel
# resolution on the moving map but tight enough that the polygon
# notches + shelves of Class B/C are visibly preserved.  First pass at
# 0.005 deg produced visibly-clobbered vertices on real flight data;
# 0.002 is the sweet spot — file size ~5 MB (vs 168 MB raw), still
# comfortably loadable on Pi Zero (416 MB RAM).
_SIMPLIFY_TOL_DEG = 0.002


def _perp_dist2(p, a, b):
    """Squared perpendicular distance from p to line a-b in 2D.
    Treating lat/lon as planar is fine for the small distances inside
    one airspace polygon — error <0.1% even for the largest Class B."""
    ax, ay = a
    bx, by = b
    px, py = p
    dx = bx - ax; dy = by - ay
    if dx == 0.0 and dy == 0.0:
        # Degenerate segment — fall back to distance from p to a.
        ex = px - ax; ey = py - ay
        return ex * ex + ey * ey
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    # Clamp to [0,1] so the "perpendicular" distance to the SEGMENT,
    # not the infinite line, is what we measure.
    if t < 0.0: t = 0.0
    elif t > 1.0: t = 1.0
    fx = ax + t * dx
    fy = ay + t * dy
    ex = px - fx; ey = py - fy
    return ex * ex + ey * ey


def _douglas_peucker(points, tol_deg=_SIMPLIFY_TOL_DEG):
    """Iterative Douglas-Peucker on a list of (lat, lon) tuples.
    Iterative rather than recursive because some FAA polygons have
    thousands of vertices and would blow Python's recursion limit."""
    n = len(points)
    if n < 3:
        return list(points)
    tol2 = tol_deg * tol_deg
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        pa = points[a]; pb = points[b]
        max_d2 = -1.0
        max_i = -1
        for i in range(a + 1, b):
            d2 = _perp_dist2(points[i], pa, pb)
            if d2 > max_d2:
                max_d2 = d2; max_i = i
        if max_d2 > tol2:
            keep[max_i] = True
            stack.append((a, max_i))
            stack.append((max_i, b))
    return [points[i] for i in range(n) if keep[i]]


# Cap input to DP at this many vertices.  DP is O(n²) worst case in
# pure Python; on a Pi Zero a single 5000-vertex polygon was taking
# ~30 s and the DC SFRA / Defense TFR / Class B can easily hit that.
# We uniform-stride pre-sample down to _DP_INPUT_CAP first so the DP
# itself sees at most ~500 points — turns the worst case from minutes
# into well under a second.  Accuracy loss is tiny: uniform sample at
# stride 10 still preserves overall shape, and DP then cleans up the
# straight segments.
_DP_INPUT_CAP = 500


def _decimate_ring(ring, tol_deg=_SIMPLIFY_TOL_DEG):
    """Decimate a closed ring.  Strips the duplicate close vertex
    (Polygon rings repeat the first vertex at the end), simplifies the
    open polyline, then puts the close vertex back so the renderer
    still gets a closed polygon."""
    if len(ring) < 4:
        return ring
    closed = ring[0] == ring[-1]
    open_pts = ring[:-1] if closed else ring
    # Pre-sample huge rings before DP so the O(n²) inner loop doesn't
    # spend minutes on a single airspace.
    if len(open_pts) > _DP_INPUT_CAP:
        stride = len(open_pts) // _DP_INPUT_CAP
        # +1 guarantees stride ≥ 1 even if math rounds to 0 in edge cases.
        if stride < 1:
            stride = 1
        open_pts = open_pts[::stride]
    simplified = _douglas_peucker(open_pts, tol_deg)
    if len(simplified) < 3:
        # Decimation collapsed the polygon — bail out and keep original
        # so we don't lose a small but valid airspace.
        return ring
    if closed:
        simplified.append(simplified[0])
    return simplified


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
        simplified = _decimate_ring(poly)
        yield {
            "class":      cls,
            "ident":      ident,
            "name":       name,
            "floor_ft":   int(flr),
            "ceiling_ft": int(clg),
            "polygon":    [[lat, lon] for lat, lon in simplified],
        }


def build_from_dir(data_dir, source_note=""):
    """Find every *.geojson file in `data_dir`, run the FAA →
    airspaces.json conversion across all of them, write the combined
    output to `<data_dir>/airspaces.json`.  Returns a stats dict
    suitable for surfacing in a UI.

    This is the entrypoint the Pi-side AIRSPACE DATA screens call to
    do the conversion in-process — no separate laptop step needed."""
    from pathlib import Path as _P
    # Streaming write: open the output now, append per-record as we
    # convert, never accumulate the full output in memory.  Important
    # on the Pi Zero where a 600 MB Class Airspace dump + 8000 output
    # records + final json.dumps would push the process well past the
    # 512 MB RAM ceiling.
    stats = {"B": 0, "C": 0, "D": 0, "MOA": 0, "R": 0, "P": 0, "TFR": 0,
             "files": 0, "skipped_no_class": 0,
             "skipped_no_polygon": 0, "skipped_bad_feature": 0,
             "errors": [], "records": 0}
    data_path = _P(data_dir)
    if not data_path.is_dir():
        stats["errors"].append(f"directory not found: {data_dir}")
        return stats
    # Per-file size cap.  We stream-parse, so a multi-hundred-MB file
    # is OK — the cap is just a "is this clearly corrupt or pointing
    # at the wrong dataset" sanity check.  FAA Class Airspace can be
    # ~600 MB if Class E coverage is included; SUA + TFRs are much
    # smaller.  Cap at 2 GB.
    MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024
    # Filename-based class hint — used when feature properties don't
    # clearly identify the class.  Lets the FAA Stadium TFR / Defense
    # TFR / Prohibited Areas datasets classify cleanly even when their
    # TYPE_CODE / LOCAL_TYPE columns use names I haven't seen yet.
    def _hint_for(name):
        n = name.lower()
        if "tfr" in n or "stadium" in n or "defense" in n: return "TFR"
        if "prohibit" in n:                                return "P"
        return None

    out_path = data_path / "airspaces.json"
    tmp_path = data_path / "airspaces.json.tmp"
    out_f = None
    try:
        out_f = open(tmp_path, "w", encoding="utf-8")
        # Compact JSON — no indent.  ~half the file size + memory of
        # the indented form, irrelevant for human reading because
        # the file is consumed by airspaces._records_from_raw().
        src = source_note or "FAA GeoJSON (pi-side build)"
        out_f.write('{"version":1,"source":')
        out_f.write(json.dumps(src))
        out_f.write(',"airspaces":[')
        first_rec = True

        for gj in sorted(data_path.glob("*.geojson")):
            stats["files"] += 1
            try:
                sz = gj.stat().st_size
                if sz > MAX_FILE_BYTES:
                    stats["errors"].append(
                        f"{gj.name}: {sz/1024/1024:.1f} MB exceeds cap "
                        f"— skipped")
                    continue
            except OSError as e:
                stats["errors"].append(f"{gj.name}: {e}")
                continue
            hint = _hint_for(gj.name)
            # Stream-parse the input + stream-write the output so even
            # a 600 MB Class Airspace dump processes in tens of MB of
            # working memory.  Per-feature try/except so one bad
            # polygon can't kill the whole build.
            try:
                for feat in stream_features(gj):
                    try:
                        had_class = had_poly = False
                        for rec in feature_to_records(feat, class_hint=hint):
                            had_class = had_poly = True
                            if not first_rec:
                                out_f.write(',')
                            out_f.write(json.dumps(rec, separators=(',', ':')))
                            first_rec = False
                            stats[rec["class"]] = stats.get(rec["class"], 0) + 1
                            stats["records"] += 1
                        if not had_class:
                            stats["skipped_no_class"] += 1
                        elif not had_poly:
                            stats["skipped_no_polygon"] += 1
                    except Exception as e:    # noqa: BLE001
                        stats["skipped_bad_feature"] += 1
                        if len(stats["errors"]) < 5:
                            stats["errors"].append(f"{gj.name} feature: {e}")
            except OSError as e:
                stats["errors"].append(f"{gj.name}: {e}")
                continue

        out_f.write(']}')
        out_f.close()
        out_f = None
        # Atomic replace so a partial write doesn't corrupt the
        # existing airspaces.json a reader might be holding open.
        os.replace(str(tmp_path), str(out_path))
    except OSError as e:
        stats["errors"].append(f"write {out_path.name}: {e}")
    finally:
        if out_f is not None:
            try: out_f.close()
            except Exception: pass
            try: os.remove(str(tmp_path))
            except Exception: pass

    stats["output"] = str(out_path)
    return stats


def load_geojson(path):
    """Load a GeoJSON file and return the feature list.  Defensive
    against:
        * BOM at the start of the file (some Windows-saved exports)
        * GeoJSON with no top-level 'type' but a 'features' array
        * features list as None instead of empty list

    Whole-file load — prefer stream_features() for inputs over a few
    MB so the Pi Zero doesn't OOM."""
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top-level isn't a JSON object")
    if data.get("type") == "FeatureCollection":
        return data.get("features") or []
    if data.get("type") == "Feature":
        return [data]
    if isinstance(data.get("features"), list):
        return data["features"]
    raise ValueError(f"{path}: not a GeoJSON Feature/FeatureCollection")


def stream_features(path):
    """Yield Features from a GeoJSON FeatureCollection one at a time
    without loading the whole file into memory.  Required for the
    Pi Zero (512 MB RAM) on multi-hundred-MB FAA dumps that include
    Class E or other very-detailed polygons we don't ultimately keep.

    State machine: find ``"features": [``, then track brace depth +
    string state to identify each top-level object inside the array.
    JSON-parse each individually.  Malformed individual features
    log + skip rather than abort the whole stream."""
    OPEN_BRACE  = 0x7B   # {
    CLOSE_BRACE = 0x7D   # }
    QUOTE       = 0x22   # "
    BACKSLASH   = 0x5C   # \\
    CLOSE_BRKT  = 0x5D   # ]
    READ_BLOCK  = 64 * 1024

    with open(path, "rb") as fh:
        # ── Locate the start of the features array ────────────────────
        head = b""
        features_start = -1
        # Cap the head scan to a few MB to avoid pathological inputs
        # where "features" never appears.
        while features_start < 0 and len(head) < 8 * 1024 * 1024:
            more = fh.read(READ_BLOCK)
            if not more:
                break
            head += more
            f_idx = head.find(b'"features"')
            if f_idx < 0:
                continue
            bracket = head.find(b'[', f_idx)
            if bracket >= 0:
                features_start = bracket + 1
                break
        if features_start < 0:
            return
        leftover = head[features_start:]

        # ── Stream the rest, yielding one feature per top-level {…} ──
        depth = 0
        in_str = False
        esc = False
        buf = bytearray()
        # Re-emit leftover bytes first, then continue reading.
        def _byte_stream():
            for b in leftover:
                yield b
            while True:
                chunk = fh.read(READ_BLOCK)
                if not chunk:
                    return
                for b in chunk:
                    yield b

        for b in _byte_stream():
            if in_str:
                buf.append(b)
                if esc:
                    esc = False
                elif b == BACKSLASH:
                    esc = True
                elif b == QUOTE:
                    in_str = False
                continue
            if b == QUOTE:
                in_str = True
                if depth > 0:
                    buf.append(b)
                continue
            if b == OPEN_BRACE:
                if depth == 0:
                    buf = bytearray()
                buf.append(b)
                depth += 1
                continue
            if b == CLOSE_BRACE:
                if depth > 0:
                    buf.append(b)
                depth -= 1
                if depth == 0 and buf:
                    try:
                        feat = json.loads(buf.decode("utf-8", errors="replace"))
                        yield feat
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        pass
                    buf = bytearray()
                continue
            if b == CLOSE_BRKT and depth == 0:
                return   # end of features array
            if depth > 0:
                buf.append(b)


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
