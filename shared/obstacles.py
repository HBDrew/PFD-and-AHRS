"""
obstacles.py – FAA Digital Obstacle File (DOF) parser and spatial query.

Shared by both Pi Zero 2W and Pi 4 display versions.

The FAA DOF is a fixed-width text file published at:
  https://aeronav.faa.gov/Obst_Data/DAILY_DOF_DAT.ZIP

Each data record is a fixed-width line containing obstacle position,
type, AGL height, MSL height, and lighting code.

After the first download the file is parsed and a compact NumPy binary
cache (.npy) is written next to the raw DAT file for fast subsequent
loads.  Without NumPy a plain list-of-tuples fallback is used (slower
spatial queries, but correct).

Public API
----------
load(data_dir)           → list/array or None
query_nearby(obstacles, lat, lon, radius_nm, alt_ft, window_ft)
                         → list of ObstacleRecord
disk_stats(data_dir)     → (record_count: int, used_mb: float)
"""

import os
import math
import struct
import datetime as _dt

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

# ── File paths ────────────────────────────────────────────────────────────────
DOF_FILENAME   = "DAILY_DOF_DAT.DAT"
CACHE_FILENAME = "dof_cache.npy"        # numpy structured array
DOF_ZIP_URL    = "https://aeronav.faa.gov/Obst_Data/DAILY_DOF_DAT.ZIP"

# ── Obstacle types (OAS code → human label) ──────────────────────────────────
_TYPE_MAP = {
    "ANTENNA":     "ANT",
    "TOWER":       "TWR",
    "BUILDING":    "BLD",
    "POWERLINE":   "PWR",
    "WINDMILL":    "WND",
    "LANDFILL":    "LFL",
    "VEGETATION":  "VEG",
    "CRANE":       "CRN",
    "BRIDGE":      "BRG",
    "SIGN":        "SGN",
    "SILO":        "SLO",
    "SMOKESTACK":  "SMK",
    "STACK":       "STK",
    "TANK":        "TNK",
    "TRAMWAY":     "TRM",
    "UTILITY":     "UTL",
    "RIG":         "RIG",
}

# ── Lighting codes ─────────────────────────────────────────────────────────────
_LGT_MAP = {
    "R": True,   # Red
    "D": True,   # Dual medium intensity
    "H": True,   # High intensity
    "M": True,   # Medium intensity
    "S": True,   # Strobe
    "F": True,   # Flood
    "C": True,   # Marked/lighted (coloured marking)
    "N": False,  # None
    "U": False,  # Unknown
    "": False,
}


class ObstacleRecord:
    """Lightweight obstacle descriptor."""
    __slots__ = ("lat", "lon", "agl_ft", "msl_ft", "otype", "lit")

    def __init__(self, lat, lon, agl_ft, msl_ft, otype, lit):
        self.lat    = lat
        self.lon    = lon
        self.agl_ft = agl_ft
        self.msl_ft = msl_ft
        self.otype  = otype   # short string e.g. "TWR"
        self.lit    = lit     # bool


# ── DOF fixed-width column offsets ────────────────────────────────────────────
# Offsets calibrated to the current FAA DAILY_DOF_DAT.DAT format (verified
# 2026-05).  Sample data line:
#
#   01-003083 O US AL GULF SHORES      30 14 57.82N 087 40 32.58W BLDG…
#   0123456789012345678901234567890123456789012345678901234567890123456…
#             1111111111222222222233333333334444444444555555555566666666…
#
# (Was previously off by ~13 cols on lat/lon, and DMS was assumed to be
# hyphen-separated which silently zeroed every record.)
_COL_LAT_START   = 35
_COL_LAT_END     = 47
_COL_LON_START   = 48
_COL_LON_END     = 61
_COL_TYPE_START  = 62
_COL_TYPE_END    = 79
_COL_AGL_START   = 83
_COL_AGL_END     = 88
_COL_MSL_START   = 89
_COL_MSL_END     = 94
_COL_LGT         = 95


def _parse_dms(s: str) -> float:
    """
    Parse a DOF DMS string to decimal degrees.
    Latitude format:  'DD MM SS.SSN' (N/S)
    Longitude format: 'DDD MM SS.SSEW' (E/W)
    The trailing hemisphere letter is concatenated to the seconds field.
    """
    s = s.strip()
    if not s:
        return 0.0
    hemi = s[-1]
    parts = s[:-1].split()
    if len(parts) != 3:
        return 0.0
    try:
        deg = float(parts[0])
        mn  = float(parts[1])
        sec = float(parts[2])
    except ValueError:
        return 0.0
    dd = deg + mn / 60.0 + sec / 3600.0
    if hemi in ("S", "W"):
        dd = -dd
    return dd


def _parse_record(line: str):
    """
    Parse one DOF data line.  Returns (lat, lon, agl, msl, otype_str, lit)
    or None if the line is malformed or a header/comment.
    """
    if len(line) < 98:
        return None
    if not line[0].isdigit() and line[0] != ' ':
        return None

    lat_str  = line[_COL_LAT_START:_COL_LAT_END]
    lon_str  = line[_COL_LON_START:_COL_LON_END]
    type_str = line[_COL_TYPE_START:_COL_TYPE_END].strip()
    agl_str  = line[_COL_AGL_START:_COL_AGL_END].strip()
    msl_str  = line[_COL_MSL_START:_COL_MSL_END].strip()
    lgt_char = line[_COL_LGT:_COL_LGT+1].strip()

    try:
        lat = _parse_dms(lat_str)
        lon = _parse_dms(lon_str)
        if lat == 0.0 and lon == 0.0:
            return None
        agl = float(agl_str) if agl_str else 0.0
        msl = float(msl_str) if msl_str else 0.0
    except ValueError:
        return None

    otype = _TYPE_MAP.get(type_str.upper(), type_str[:3].upper() if type_str else "OBS")
    lit   = _LGT_MAP.get(lgt_char.upper(), False)

    return lat, lon, agl, msl, otype, lit


# ── Numpy dtype for the cache array ──────────────────────────────────────────
_DTYPE = np.dtype([
    ("lat",    np.float32),
    ("lon",    np.float32),
    ("agl_ft", np.float16),
    ("msl_ft", np.float16),
    ("otype",  "U3"),
    ("lit",    np.bool_),
]) if HAS_NUMPY else None


def _build_cache(dat_path: str, cache_path: str):
    """Parse DOF DAT and write numpy cache.  Returns the numpy structured
    array (or the list of tuples when numpy is unavailable) so the first
    load matches what np.load(cache_path) returns on subsequent calls.

    The array is sorted by latitude before saving — query_nearby uses
    np.searchsorted to slice a small lat band out of the 615 k-record
    table in O(log N) instead of scanning the whole column every frame
    (~22 ms → ~1 ms on the Pi)."""
    records = []
    with open(dat_path, "r", encoding="latin-1", errors="ignore") as fh:
        for line in fh:
            rec = _parse_record(line)
            if rec is not None:
                records.append(rec)

    if HAS_NUMPY and records:
        arr = np.array(records,
                       dtype=[("lat","f4"),("lon","f4"),
                               ("agl_ft","f4"),("msl_ft","f4"),
                               ("otype","U3"),("lit","?")])
        arr = arr[np.argsort(arr["lat"], kind="stable")]
        np.save(cache_path, arr)
        return arr

    return records


def load(data_dir: str):
    """
    Load obstacle database from data_dir.
    Checks for numpy cache first; falls back to parsing the raw DAT.
    Returns a numpy structured array (if numpy available) or list of tuples,
    or None if neither file exists.

    Returned array is sorted ascending by latitude so query_nearby can
    binary-search the lat band.  Existing un-sorted caches are sorted in
    memory at load (cheap) — saves a one-time re-rasterise.
    """
    dat_path   = os.path.join(data_dir, DOF_FILENAME)
    cache_path = os.path.join(data_dir, CACHE_FILENAME)

    if HAS_NUMPY and os.path.exists(cache_path):
        if (not os.path.exists(dat_path) or
                os.path.getmtime(cache_path) >= os.path.getmtime(dat_path)):
            try:
                arr = np.load(cache_path, allow_pickle=False)
                if len(arr) > 1 and not (np.diff(arr["lat"]) >= 0).all():
                    arr = arr[np.argsort(arr["lat"], kind="stable")]
                return arr
            except Exception:
                pass

    if not os.path.exists(dat_path):
        return None

    return _build_cache(dat_path, cache_path)


# ── Spatial query ─────────────────────────────────────────────────────────────

_NM_PER_DEG_LAT = 60.0

# Per-load cache of the contiguous lat column, keyed on id(arr).  The
# obstacle structured array uses a 32-byte stride (because of the U3
# otype field), and reading a strided column of 615 k rows blows the
# Pi's cache.  Copying the column once into a flat float32 array and
# searchsorted-ing on that drops the per-query cost ~10×.
_lat_index_cache = (None, None)


def _get_lat_index(arr):
    global _lat_index_cache
    if _lat_index_cache[0] is not arr:
        _lat_index_cache = (arr, np.ascontiguousarray(arr["lat"]))
    return _lat_index_cache[1]


def query_nearby(obstacles, lat: float, lon: float,
                 radius_nm: float = 5.0,
                 alt_ft: float = 0.0,
                 below_ft: float = 1000.0,
                 above_ft: float = 0.0,
                 window_ft: float = None):
    """
    Return obstacles within radius_nm of (lat, lon) whose MSL height is
    within `below_ft` below the aircraft.  Towers above the aircraft are
    always returned (collision risk); set `above_ft > 0` to also filter
    them.

    With numpy available, returns the filtered structured-array slice —
    callers iterate by row['lat'] / row['msl_ft'] / etc.  The pure-Python
    fallback returns a list of ObstacleRecord.  Per-record object
    construction was the dominant cost at high obstacle density (KPHX
    metro etc.); the structured-array return drops that cost to zero
    and lets callers vectorise downstream math.
    """
    if obstacles is None:
        return []

    # Back-compat: legacy callers pass window_ft for a symmetric ±window.
    # Map it to the new asymmetric below_ft (above always allowed) — the
    # net effect is "show all towers within window_ft below us, plus all
    # towers at or above the aircraft".
    if window_ft is not None:
        below_ft = window_ft
        above_ft = 0.0

    nm_per_deg_lon = _NM_PER_DEG_LAT * math.cos(math.radians(lat))
    dlat = radius_nm / _NM_PER_DEG_LAT
    dlon = radius_nm / nm_per_deg_lon if nm_per_deg_lon > 0 else radius_nm

    if HAS_NUMPY and hasattr(obstacles, "dtype"):
        # Binary-search the latitude band first.  Array is sorted by lat
        # at load time so this turns the O(N) full-table scan into
        # O(log N) plus a small slice pass.  searchsorted on the strided
        # structured-array column is slow (cache thrashing on 32-byte
        # stride over 615 k rows ≈ 20 MB of memory traffic to read a
        # 2.4 MB column), so we keep a flat contiguous lat copy keyed on
        # id(obstacles) — refreshed only when the array object changes.
        lat_col = _get_lat_index(obstacles)
        i_lo = int(np.searchsorted(lat_col, lat - dlat, side="left"))
        i_hi = int(np.searchsorted(lat_col, lat + dlat, side="right"))
        candidates = obstacles[i_lo:i_hi]
        # Bbox finish on lon over the small lat slice
        if len(candidates) > 0:
            lon_mask = ((candidates["lon"] >= lon - dlon) &
                        (candidates["lon"] <= lon + dlon))
            candidates = candidates[lon_mask]

        if below_ft > 0:
            candidates = candidates[
                candidates["msl_ft"] >= alt_ft - below_ft]
        if above_ft > 0:
            candidates = candidates[
                candidates["msl_ft"] <= alt_ft + above_ft]

        # Vectorised circle test (the prior loop was ~5–10 ms at KPHX).
        if len(candidates) > 0:
            dlat_r = candidates["lat"] - lat
            dlon_r = candidates["lon"] - lon
            dist_sq = ((dlat_r * _NM_PER_DEG_LAT) ** 2
                       + (dlon_r * nm_per_deg_lon) ** 2)
            candidates = candidates[dist_sq <= radius_nm * radius_nm]
        # Return as recarray so callers can use either row.field or
        # row["field"]; the render path uses attribute access.
        return candidates.view(np.recarray)

    results = []
    for rec in obstacles:
        rlat, rlon, ragl, rmsl, rtype, rlit = rec
        if abs(rlat - lat) > dlat or abs(rlon - lon) > dlon:
            continue
        if below_ft > 0 and rmsl < alt_ft - below_ft:
            continue
        if above_ft > 0 and rmsl > alt_ft + above_ft:
            continue
        dlat_r = rlat - lat
        dlon_r = rlon - lon
        dist_nm = math.hypot(dlat_r * _NM_PER_DEG_LAT,
                             dlon_r * nm_per_deg_lon)
        if dist_nm <= radius_nm:
            results.append(ObstacleRecord(
                lat=rlat, lon=rlon,
                agl_ft=ragl, msl_ft=rmsl,
                otype=rtype, lit=rlit,
            ))

    return results


# ── Disk stats ────────────────────────────────────────────────────────────────

def disk_stats(data_dir: str):
    """Return (record_count, used_mb) for what's on disk."""
    dat_path   = os.path.join(data_dir, DOF_FILENAME)
    cache_path = os.path.join(data_dir, CACHE_FILENAME)

    total_bytes = 0
    record_count = 0

    for p in (dat_path, cache_path):
        if os.path.exists(p):
            total_bytes += os.path.getsize(p)

    if HAS_NUMPY and os.path.exists(cache_path):
        try:
            arr = np.load(cache_path, allow_pickle=False)
            record_count = len(arr)
        except Exception:
            pass
    elif os.path.exists(dat_path):
        with open(dat_path, "rb") as f:
            record_count = sum(1 for _ in f) - 4

    return record_count, total_bytes / 1_048_576


# ── Download date / expiry ─────────────────────────────────────────────────────

def download_date(data_dir: str):
    """
    Return the download date of the obstacle file as a datetime.date,
    or None if no file is present.
    """
    dat_path = os.path.join(data_dir, DOF_FILENAME)
    if not os.path.exists(dat_path):
        return None
    return _dt.date.fromtimestamp(os.path.getmtime(dat_path))


def is_expired(data_dir: str, expiry_days: int = 28) -> bool:
    """Return True if the obstacle file is older than expiry_days."""
    d = download_date(data_dir)
    if d is None:
        return False
    return (_dt.date.today() - d).days > expiry_days
