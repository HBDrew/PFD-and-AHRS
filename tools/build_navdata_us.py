#!/usr/bin/env python3
"""
build_navdata_us.py — FAA NASR + CIFP → navdata cache converter.

Produces the on-disk nav-data cache that shared/navdata.py loads:

    navdata_fixes.npy     named fixes (intersections)        ← NASR FIX
    navdata_navaids.npy   VOR/VORTAC/VOR-DME/NDB navaids      ← NASR NAV
    navdata.json          airways + procedures + holds + cycle
                                                              ← NASR AWY + CIFP

Run this on your laptop (any machine with Python 3.8+ and numpy); drop the
three output files at  <pi4|pi_zero>/data/navdata/ .  Mirrors
tools/build_airspaces_us.py in spirit: a once-per-28-day-cycle converter you
run off-aircraft.

════════════════════════════════════════════════════════════════════════════
Where to get the input files  (free, US-only, 28-day cycle)
════════════════════════════════════════════════════════════════════════════

1. FAA NASR Subscription  →  fixes, navaids, airways
   https://www.faa.gov/air_traffic/flight_info/aeronav/aero_data/NASR_Subscription/
   Download the current 28-Day NASR Subscription (CSV ZIP).  Unzip it; you want
   these CSV files (modern NASR is CSV with a header row):

       FIX_BASE.csv     named fixes / intersections
       NAV_BASE.csv     navaids (VOR/VORTAC/VOR-DME/NDB/…)
       AWY_BASE.csv     airway identifiers
       AWY_SEG.csv      airway segments (ordered fix sequence)

2. FAA CIFP (Coded Instrument Flight Procedures)  →  approaches + holds
   https://www.faa.gov/air_traffic/flight_info/aeronav/digital_products/cifp/download/
   Download the current CIFP ZIP; unzip it; you want the single ARINC-424 file:

       FAACIFP18        fixed-column ARINC-424 records (132 chars/line)

   The approach legs, missed-approach legs and holding patterns are parsed
   from here.  ARINC-424 is fixed-column; the column offsets below
   (the _A424_* constants) follow the FAA CIFP / ARINC-424 spec, but if a
   future cycle shifts a field, tune those constants — every offset is named.

════════════════════════════════════════════════════════════════════════════
Usage
════════════════════════════════════════════════════════════════════════════

    python3 tools/build_navdata_us.py \\
        --nasr  /path/to/unzipped_NASR_dir \\
        --cifp  /path/to/FAACIFP18 \\
        --out   pi4/data/navdata

    # Both inputs are optional — pass either or both.  A NASR-only run gives
    # fixes/navaids/airways (no procedures); a CIFP-only run gives
    # procedures/holds (no fix/navaid arrays).  --cycle stamps the cache
    # (e.g. 2406); if omitted it is inferred from the NASR dir name / today.

    # Verify the result by loading it back:
    python3 tools/build_navdata_us.py --verify pi4/data/navdata

Approaches kept by default: RNAV (GPS), GPS, RNP, VOR, LOC, ILS, NDB, LDA,
VOR/DME, TACAN.  SIDs/STARs are parsed too (subsection D/E) but the runtime
currently only consumes approaches + holds; they are written for free.
"""

import argparse
import csv
import json
import math
import os
import re
import sys
from collections import OrderedDict, defaultdict

try:
    import numpy as np
except ImportError:
    sys.stderr.write("ERROR: numpy is required (pip install numpy)\n")
    sys.exit(2)

# Cache file names + dtypes must match shared/navdata.py exactly.
FIXES_FILE   = "navdata_fixes.npy"
NAVAIDS_FILE = "navdata_navaids.npy"
JSON_FILE    = "navdata.json"

_FIX_DTYPE = [("ident", "U5"), ("lat", "f4"), ("lon", "f4")]
_NAV_DTYPE = [("ident", "U4"), ("ntype", "U4"), ("lat", "f4"), ("lon", "f4"),
              ("freq", "f4"), ("name", "U32")]


# ════════════════════════════════════════════════════════════════════════════
# NASR CSV  →  fixes / navaids / airways
# ════════════════════════════════════════════════════════════════════════════
#
# Modern NASR CSV files carry a header row; we look up columns by name so we
# don't depend on column order.  Field names vary slightly between cycles, so
# each logical field lists a few accepted header spellings (first hit wins).

_FIX_COLS = {
    "ident": ("FIX_ID", "FIX_IDENT", "NAME"),
    "lat":   ("LAT_DECIMAL", "LATITUDE", "LAT_DEG"),
    "lon":   ("LONG_DECIMAL", "LONGITUDE", "LONG_DEG"),
}
_NAV_COLS = {
    "ident": ("NAV_ID", "NAVAID_ID", "IDENT"),
    "type":  ("NAV_TYPE", "TYPE", "FACILITY_TYPE"),
    "lat":   ("LAT_DECIMAL", "LATITUDE", "LAT_DEG"),
    "lon":   ("LONG_DECIMAL", "LONGITUDE", "LONG_DEG"),
    "freq":  ("FREQ", "FREQUENCY"),
    "name":  ("NAME", "NAV_NAME"),
}
_AWY_SEG_COLS = {
    "awy":   ("AWY_ID", "AWY_DESIGNATION", "AIRWAY_ID"),
    "seq":   ("SEQ", "SEQUENCE", "POINT_SEQ"),
    "ident": ("FIX_ID", "POINT_ID", "NAV_ID", "EFF_FIX_ID"),
}

# NASR navaid TYPE values → our compact ntype.  Anything not mapped is kept
# verbatim (upper, ≤4 chars) so unusual types still survive.
_NAV_TYPE_MAP = {
    "VOR": "VOR", "VOR/DME": "VOR", "VORTAC": "VOR", "VOR-DME": "VOR",
    "TACAN": "TAC", "DME": "DME",
    "NDB": "NDB", "NDB/DME": "NDB", "MARINE NDB": "NDB", "NDB-DME": "NDB",
}


def _pick(header_map, names):
    """First header in `names` that exists in header_map → its index, else None."""
    for n in names:
        if n in header_map:
            return header_map[n]
    return None


def _open_csv(path):
    fh = open(path, "r", encoding="utf-8-sig", newline="")
    rdr = csv.reader(fh)
    header = next(rdr, None)
    if header is None:
        fh.close()
        return None, None, None
    hmap = {h.strip().upper(): i for i, h in enumerate(header)}
    return fh, rdr, hmap


def _to_float(s):
    """Parse a NASR coordinate/number cell → float, or None.  Handles decimal
    degrees directly and the occasional 'DD-MM-SS.sssH' formatted string."""
    if s is None:
        return None
    s = s.strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        pass
    m = re.match(r"^\s*(\d+)[-:](\d+)[-:](\d+(?:\.\d+)?)\s*([NSEW])?\s*$", s)
    if m:
        d, mm, ss, hemi = m.group(1), m.group(2), m.group(3), m.group(4)
        val = int(d) + int(mm) / 60.0 + float(ss) / 3600.0
        if hemi in ("S", "W"):
            val = -val
        return val
    return None


def _find_nasr_file(nasr_dir, *candidates):
    """Locate a NASR CSV by trying candidate basenames case-insensitively,
    searching the dir tree (NASR ZIPs nest CSVs in subfolders)."""
    wanted = {c.lower() for c in candidates}
    for root, _dirs, files in os.walk(nasr_dir):
        for f in files:
            if f.lower() in wanted:
                return os.path.join(root, f)
    return None


def parse_fixes(nasr_dir):
    path = _find_nasr_file(nasr_dir, "FIX_BASE.csv", "FIX.csv")
    if not path:
        print("  FIX_BASE.csv not found — skipping fixes")
        return None
    fh, rdr, hmap = _open_csv(path)
    if rdr is None:
        return None
    ci = {k: _pick(hmap, v) for k, v in _FIX_COLS.items()}
    if ci["ident"] is None or ci["lat"] is None or ci["lon"] is None:
        print(f"  {os.path.basename(path)}: missing ident/lat/lon columns "
              f"(have: {sorted(hmap)[:12]}…) — skipping fixes")
        fh.close()
        return None
    rows, seen = [], set()
    for r in rdr:
        try:
            ident = r[ci["ident"]].strip().upper()
            lat = _to_float(r[ci["lat"]])
            lon = _to_float(r[ci["lon"]])
        except IndexError:
            continue
        if not ident or lat is None or lon is None:
            continue
        if ident in seen:           # NASR repeats a fix per artcc/airway use
            continue
        seen.add(ident)
        rows.append((ident[:5], lat, lon))
    fh.close()
    if not rows:
        return None
    arr = np.array(rows, dtype=_FIX_DTYPE)
    arr = arr[np.argsort(arr["lat"], kind="stable")]
    print(f"  fixes:   {len(arr):,}")
    return arr


def parse_navaids(nasr_dir):
    path = _find_nasr_file(nasr_dir, "NAV_BASE.csv", "NAV.csv")
    if not path:
        print("  NAV_BASE.csv not found — skipping navaids")
        return None
    fh, rdr, hmap = _open_csv(path)
    if rdr is None:
        return None
    ci = {k: _pick(hmap, v) for k, v in _NAV_COLS.items()}
    if ci["ident"] is None or ci["lat"] is None or ci["lon"] is None:
        print(f"  {os.path.basename(path)}: missing ident/lat/lon columns "
              f"(have: {sorted(hmap)[:12]}…) — skipping navaids")
        fh.close()
        return None
    rows, seen = [], set()
    for r in rdr:
        try:
            ident = r[ci["ident"]].strip().upper()
            lat = _to_float(r[ci["lat"]])
            lon = _to_float(r[ci["lon"]])
        except IndexError:
            continue
        if not ident or lat is None or lon is None:
            continue
        raw_t = (r[ci["type"]].strip().upper() if ci["type"] is not None else "")
        ntype = _NAV_TYPE_MAP.get(raw_t, raw_t[:4])
        freq = _to_float(r[ci["freq"]]) if ci["freq"] is not None else None
        name = (r[ci["name"]].strip() if ci["name"] is not None else "")[:32]
        key = (ident, ntype)
        if key in seen:
            continue
        seen.add(key)
        rows.append((ident[:4], ntype, lat, lon, freq or 0.0, name))
    fh.close()
    if not rows:
        return None
    arr = np.array(rows, dtype=_NAV_DTYPE)
    arr = arr[np.argsort(arr["lat"], kind="stable")]
    print(f"  navaids: {len(arr):,}")
    return arr


def parse_airways(nasr_dir):
    path = _find_nasr_file(nasr_dir, "AWY_SEG.csv", "AWY_BASE.csv")
    if not path:
        print("  AWY_SEG.csv not found — skipping airways")
        return None
    fh, rdr, hmap = _open_csv(path)
    if rdr is None:
        return None
    ci = {k: _pick(hmap, v) for k, v in _AWY_SEG_COLS.items()}
    if ci["awy"] is None or ci["seq"] is None or ci["ident"] is None:
        print(f"  {os.path.basename(path)}: missing awy/seq/ident columns "
              f"(have: {sorted(hmap)[:12]}…) — skipping airways")
        fh.close()
        return None
    segs = defaultdict(list)        # awy → list of (seq, ident)
    for r in rdr:
        try:
            awy = r[ci["awy"]].strip().upper()
            ident = r[ci["ident"]].strip().upper()
            seq_raw = r[ci["seq"]].strip()
        except IndexError:
            continue
        if not awy or not ident:
            continue
        try:
            seq = float(seq_raw)
        except ValueError:
            seq = len(segs[awy])
        segs[awy].append((seq, ident))
    fh.close()
    airways = {}
    for awy, pts in segs.items():
        pts.sort(key=lambda p: p[0])
        seq = []
        for _s, ident in pts:        # dedupe consecutive repeats
            if not seq or seq[-1] != ident:
                seq.append(ident)
        if len(seq) >= 2:
            airways[awy] = seq
    print(f"  airways: {len(airways):,}")
    return airways


# ════════════════════════════════════════════════════════════════════════════
# CIFP (ARINC-424)  →  procedures + holds
# ════════════════════════════════════════════════════════════════════════════
#
# The FAA CIFP file (FAACIFP18) is fixed-column ARINC-424 (v18).  We parse two
# things: airport approach procedures (and SID/STAR for free) from the
# SUSAP… records, and enroute holds from the EP… records.
#
# Column offsets are 0-based [start:end) slices.  These follow the ARINC-424
# spec field layout for the SID/STAR/Approach airport record (section P,
# subsections D=SID, E=STAR, F=Approach).  If a cycle shifts a field, tune the
# named constant — run with --dump-cifp N to eyeball raw records.

_A424_SECTION    = 4              # col 5: section code  ('P'=airport, 'E'=enroute)
_A424_APT_ICAO   = slice(6, 10)   # airport ICAO ident (e.g. 'KFLG')
_A424_SUBSECTION = 12             # col 13: subsection (D=SID,E=STAR,F=Approach)
_A424_PROC_ID    = slice(13, 19)  # procedure identifier (e.g. 'R03  ')
_A424_TRANS_ID   = slice(20, 25)  # transition identifier (IAF name, blank=common)
_A424_SEQ        = slice(26, 29)  # sequence number within the route
_A424_FIX_ID     = slice(29, 34)  # fix identifier at this leg
_A424_TURN_DIR   = 43             # col 44: turn direction (L/R/blank)
_A424_PATH_TERM  = slice(47, 49)  # path & termination (IF/TF/CF/DF/RF/HM/…)
_A424_MAG_CRS    = slice(70, 74)  # magnetic course, tenths of a degree
_A424_ALT_DESC   = 82             # col 83: altitude description (+ - @ B G …)
_A424_ALT1       = slice(84, 89)  # altitude 1 (ft, or 'FL###')
_A424_ALT2       = slice(89, 94)  # altitude 2 (for between-altitudes 'B')

# Enroute holding-pattern record (section E, subsection P) field layout.
_A424_HOLD_FIX   = slice(29, 34)  # the holding fix
_A424_HOLD_CRS   = slice(51, 55)  # inbound holding course, tenths of a degree
_A424_HOLD_TURN  = 50             # turn direction (L/R)
_A424_HOLD_LEG_T = slice(55, 58)  # leg length (time, tenths of a minute)
_A424_HOLD_LEG_D = slice(58, 62)  # leg length (distance, tenths of an nm)

# Approach subsection 'F' route types we keep, plus SID(D)/STAR(E) for free.
_KEEP_SUBSECTIONS = {"D", "E", "F"}

# ARINC approach route-type letter (col 20) → human approach kind for naming.
_APPR_ROUTE_TYPE = {
    "B": "LOC/BC", "D": "VOR/DME", "F": "FMS", "G": "IGS", "I": "ILS",
    "J": "GLS", "L": "LOC", "M": "MLS", "N": "NDB", "P": "GPS",
    "Q": "NDB/DME", "R": "RNAV", "S": "VOR", "T": "TACAN", "U": "SDF",
    "V": "VOR", "W": "MLS", "X": "LDA", "Y": "ILS/DME",
}

_LEG_TYPES = {"IF", "TF", "CF", "DF", "RF", "FA", "CA", "VA", "FM", "VM",
              "CD", "VD", "CR", "VR", "CI", "VI", "PI", "HA", "HF", "HM",
              "AF", "FC", "FD"}


def _a424_str(line, sl):
    try:
        return line[sl].strip()
    except IndexError:
        return ""


def _a424_alt(line):
    """(alt_ft, alt_type) from the altitude-description + ALT1/ALT2 fields.
    alt_type: 'AT' | 'AB' (at-or-above) | 'BL' (at-or-below) | 'WN' (window)."""
    desc = line[_A424_ALT_DESC] if len(line) > _A424_ALT_DESC else " "
    raw1 = _a424_str(line, _A424_ALT1)
    if not raw1:
        return None, None

    def _parse_alt(s):
        s = s.strip().upper()
        if not s:
            return None
        if s.startswith("FL"):
            try:
                return int(s[2:]) * 100
            except ValueError:
                return None
        try:
            return int(s)
        except ValueError:
            return None

    a1 = _parse_alt(raw1)
    if a1 is None:
        return None, None
    type_map = {"+": "AB", "-": "BL", "@": "AT", " ": "AT", "B": "WN",
                "G": "AT", "H": "AB", "I": "AT", "J": "AB", "V": "AT"}
    return a1, type_map.get(desc, "AT")


def _a424_course(line, sl):
    raw = _a424_str(line, sl)
    if not raw or not raw.isdigit():
        return None
    return round(int(raw) / 10.0, 1)


def parse_cifp(cifp_path, fixes=None, navaids=None, keep_sidstar=True,
               dump=0):
    """Parse FAACIFP18 → (procedures, holds).  fixes/navaids (the NASR arrays)
    are used to resolve each leg fix's lat/lon at build time so the runtime
    doesn't have to."""
    procedures = defaultdict(lambda: defaultdict(lambda: {
        "type": None, "transitions": OrderedDict(),
        "_route_types": set(), "final": [], "missed": [], "_subsec": None}))
    holds = {}

    # Fast ident → (lat, lon) resolver from the NASR arrays.
    coord = {}
    if fixes is not None:
        for r in fixes:
            coord.setdefault(str(r["ident"]), (float(r["lat"]), float(r["lon"])))
    if navaids is not None:
        for r in navaids:
            coord.setdefault(str(r["ident"]), (float(r["lat"]), float(r["lon"])))

    dumped = 0
    n_legs = 0
    with open(cifp_path, "r", encoding="latin-1") as fh:
        for line in fh:
            if len(line) < 50:
                continue
            if line[0] not in ("S", "E"):   # S=standard record, E=enroute hold
                continue
            section = line[_A424_SECTION] if len(line) > _A424_SECTION else " "

            # ── enroute holds ──────────────────────────────────────────────
            # Enroute (section 'E') records carry the subsection code at col 6
            # (index 5), NOT col 13 like airport records.  'P' = holding pattern.
            if section == "E" and len(line) > 5 and line[5] == "P":
                if dump and dumped < dump:
                    print(f"  HOLD: {line.rstrip()[:90]}")
                    dumped += 1
                fix = _a424_str(line, _A424_HOLD_FIX).upper()
                if not fix:
                    continue
                crs = _a424_course(line, _A424_HOLD_CRS)
                turn = (line[_A424_HOLD_TURN]
                        if len(line) > _A424_HOLD_TURN else " ").strip().upper()
                leg_t = _a424_course(line, _A424_HOLD_LEG_T)   # tenths→min
                leg_draw = _a424_str(line, _A424_HOLD_LEG_D)
                leg_nm = (round(int(leg_draw) / 10.0, 1)
                          if leg_draw.isdigit() else 0)
                holds[fix] = {"course": crs or 0, "turn": turn or "R",
                              "leg_nm": leg_nm, "leg_min": leg_t or 0}
                continue

            # ── airport SID/STAR/Approach legs ─────────────────────────────
            if section != "P":
                continue
            subsec = (line[_A424_SUBSECTION]
                      if len(line) > _A424_SUBSECTION else " ")
            if subsec not in _KEEP_SUBSECTIONS:
                continue
            if subsec in ("D", "E") and not keep_sidstar:
                continue

            apt = _a424_str(line, _A424_APT_ICAO).upper()
            proc_id = _a424_str(line, _A424_PROC_ID).upper()
            if not apt or not proc_id:
                continue
            leg_type = _a424_str(line, _A424_PATH_TERM).upper()
            if leg_type and leg_type not in _LEG_TYPES:
                # Not a leg row (could be a continuation / data record) — skip.
                continue

            if dump and dumped < dump:
                print(f"  RAW[{apt} {subsec} {proc_id}]: {line.rstrip()[:90]}")
                dumped += 1

            trans = _a424_str(line, _A424_TRANS_ID).upper()
            fix_id = _a424_str(line, _A424_FIX_ID).upper()
            crs = _a424_course(line, _A424_MAG_CRS)
            alt_ft, alt_type = _a424_alt(line)
            ll = coord.get(fix_id)
            leg = {
                "fix": fix_id, "leg_type": leg_type or "TF",
                "course": crs,
                "alt_ft": alt_ft, "alt_type": alt_type,
                "lat": ll[0] if ll else None, "lon": ll[1] if ll else None,
            }
            route_type = line[19] if len(line) > 19 else " "

            pr = procedures[apt][proc_id]
            pr["_subsec"] = subsec
            if route_type.strip():
                pr["_route_types"].add(route_type.strip())
            if trans:
                pr["transitions"].setdefault(trans, []).append(leg)
            elif leg_type in ("HM",) or "MISSED" in proc_id:
                pr["missed"].append(leg)
            else:
                pr["final"].append(leg)
            n_legs += 1

    # Heuristic: the missed approach in CIFP is flagged by route type 'M' on
    # the common-route legs; without per-leg route-type tracking we approximate
    # by treating trailing HM/HA/HF and climb legs after the MAP as missed.
    out = {}
    for apt, procs in procedures.items():
        out_procs = {}
        for pid, pr in procs.items():
            subsec = pr["_subsec"]
            name = _name_procedure(pid, subsec, pr["_route_types"])
            out_procs[name] = {
                "type": _proc_type(subsec, pr["_route_types"]),
                "transitions": {k: v for k, v in pr["transitions"].items()},
                "final": pr["final"],
                "missed": pr["missed"],
            }
        if out_procs:
            out[apt] = out_procs

    print(f"  procedures: {sum(len(v) for v in out.values()):,} "
          f"at {len(out):,} airports   ({n_legs:,} legs)")
    print(f"  holds:      {len(holds):,}")
    return out, holds


def _proc_type(subsec, route_types):
    if subsec == "D":
        return "SID"
    if subsec == "E":
        return "STAR"
    # Approach: classify by the most specific route-type letter present.
    for letter in ("R", "P", "I", "L", "V", "S", "N", "X", "D", "T"):
        if letter in route_types:
            return _APPR_ROUTE_TYPE.get(letter, "APPR")
    return "APPR"


def _name_procedure(pid, subsec, route_types):
    """Build a human procedure name from the CIFP procedure ident.  Approach
    idents are terse (e.g. 'R03', 'I07', 'V21'); expand to 'RNAV (GPS) RWY 03'
    etc.  SIDs/STARs keep their ident (they're already readable)."""
    if subsec in ("D", "E"):
        return pid
    m = re.match(r"^([A-Z])([0-9]{2})([A-Z]?)$", pid)
    if not m:
        return pid
    letter, rwy, suffix = m.group(1), m.group(2), m.group(3)
    kind = {
        "R": "RNAV (GPS)", "P": "GPS", "I": "ILS", "L": "LOC",
        "V": "VOR", "S": "VOR", "N": "NDB", "X": "LDA", "D": "VOR/DME",
        "T": "TACAN", "B": "LOC BC", "Q": "NDB/DME", "H": "RNAV (RNP)",
    }.get(letter, letter)
    suff = {"Z": " Z", "Y": " Y", "X": " X", "W": " W", "V": " V"}.get(suffix, "")
    return f"{kind} RWY {rwy}{suff}"


# ════════════════════════════════════════════════════════════════════════════
# write / verify
# ════════════════════════════════════════════════════════════════════════════

def _infer_cycle(nasr_dir):
    """Best-effort 28-day cycle stamp 'YYMM' from a NASR dir name like
    '28DaySubscription_Effective_2024-06-13', else from today's date."""
    if nasr_dir:
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", os.path.basename(
            os.path.normpath(nasr_dir)))
        if m:
            return m.group(1)[2:] + m.group(2)
    import datetime
    t = datetime.date.today()
    return f"{t.year % 100:02d}{t.month:02d}"


def write_cache(out_dir, fixes, navaids, airways, procedures, holds, cycle):
    os.makedirs(out_dir, exist_ok=True)
    if fixes is not None:
        np.save(os.path.join(out_dir, FIXES_FILE), fixes)
    if navaids is not None:
        np.save(os.path.join(out_dir, NAVAIDS_FILE), navaids)
    doc = {"cycle": cycle,
           "airways": airways or {},
           "holds": holds or {},
           "procedures": procedures or {}}
    with open(os.path.join(out_dir, JSON_FILE), "w", encoding="utf-8") as fh:
        json.dump(doc, fh, separators=(",", ":"))
    jsz = os.path.getsize(os.path.join(out_dir, JSON_FILE)) / 1024.0
    print(f"\nwrote → {out_dir}")
    print(f"  {JSON_FILE}: {jsz:.0f} KB   cycle={cycle}")


def verify(data_dir):
    """Load the cache through the runtime module and print a summary."""
    here = os.path.dirname(os.path.abspath(__file__))
    shared = os.path.join(os.path.dirname(here), "shared")
    if shared not in sys.path:
        sys.path.insert(0, shared)
    import navdata
    nd = navdata.load(data_dir)
    if nd is None:
        print(f"VERIFY: no cache loaded from {data_dir}")
        return 1
    print(f"VERIFY {data_dir}:")
    print(f"  cycle:      {nd.cycle}")
    print(f"  fixes:      {len(nd.fixes) if nd.has_fixes() else 0:,}")
    print(f"  navaids:    {len(nd.navaids) if nd.has_navaids() else 0:,}")
    print(f"  airways:    {len(nd.airways):,}")
    print(f"  procedures: {sum(len(v) for v in nd.procedures.values()):,} "
          f"at {len(nd.procedures):,} airports")
    print(f"  holds:      {len(nd.holds):,}")
    # A couple of spot checks if data is present.
    if nd.procedures:
        apt = next(iter(sorted(nd.procedures)))
        print(f"  e.g. {apt}: {nd.procedures_for(apt)[:6]}")
    if nd.has_navaids():
        r = nd.navaids[len(nd.navaids) // 2]
        print(f"  e.g. navaid {str(r['ident'])} {str(r['ntype'])} "
              f"{float(r['freq']):.2f}")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="FAA NASR + CIFP → shared/navdata.py cache.")
    ap.add_argument("--nasr", help="unzipped NASR Subscription dir "
                    "(FIX_BASE.csv / NAV_BASE.csv / AWY_SEG.csv)")
    ap.add_argument("--cifp", help="path to the FAACIFP18 ARINC-424 file")
    ap.add_argument("--out", default="navdata",
                    help="output dir (default: ./navdata)")
    ap.add_argument("--cycle", help="28-day cycle stamp (e.g. 2406); "
                    "inferred from --nasr dir name / today if omitted")
    ap.add_argument("--no-sidstar", action="store_true",
                    help="skip SIDs/STARs (keep approaches only)")
    ap.add_argument("--dump-cifp", type=int, default=0, metavar="N",
                    help="print the first N raw CIFP leg records (debug)")
    ap.add_argument("--verify", metavar="DIR",
                    help="just load+summarise an existing cache, then exit")
    args = ap.parse_args()

    if args.verify:
        sys.exit(verify(args.verify))

    if not args.nasr and not args.cifp:
        ap.error("pass --nasr and/or --cifp (or --verify DIR)")

    fixes = navaids = airways = None
    procedures = holds = None

    if args.nasr:
        print(f"NASR {args.nasr}:")
        fixes = parse_fixes(args.nasr)
        navaids = parse_navaids(args.nasr)
        airways = parse_airways(args.nasr)

    if args.cifp:
        print(f"CIFP {args.cifp}:")
        procedures, holds = parse_cifp(
            args.cifp, fixes=fixes, navaids=navaids,
            keep_sidstar=not args.no_sidstar, dump=args.dump_cifp)

    cycle = args.cycle or _infer_cycle(args.nasr)
    write_cache(args.out, fixes, navaids, airways, procedures, holds, cycle)
    print()
    verify(args.out)


if __name__ == "__main__":
    main()
