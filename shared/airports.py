"""
airports.py – Airport database parser and spatial query.

Parses OurAirports.com CSV format (available free from
https://ourairports.com/data/airports.csv) into a compact NumPy
structured array for fast spatial queries.

CSV columns (OurAirports schema, subset we use):
    ident          — ICAO/IATA/local identifier (e.g. "KSEZ")
    type           — small_airport | medium_airport | large_airport |
                     heliport | seaplane_base | closed | balloonport
    name           — airport name
    latitude_deg   — decimal degrees
    longitude_deg  — decimal degrees
    elevation_ft   — field elevation (may be blank)
    iso_country    — ISO country code (e.g. "US")

A compact .npy cache is written next to the raw CSV for fast subsequent
loads.  Records with "closed" status or missing lat/lon are dropped.

Public API:
    load(data_dir)              → numpy structured array or None
    query_nearby(arr, lat, lon, radius_nm)
                                → list of AirportRecord within radius
    disk_stats(data_dir)        → (record_count, used_mb)
    download_date(data_dir)     → datetime.date or None
    is_expired(data_dir, days)  → bool
"""

import os
import math
import csv
import datetime as _dt

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

# ── File paths and source ─────────────────────────────────────────────────────
CSV_FILENAME   = "airports.csv"
CACHE_FILENAME = "airports_cache.npy"
AIRPORTS_CSV_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"

# ── Airport type shortcodes ──────────────────────────────────────────────────
_TYPE_MAP = {
    "small_airport":   "S",   # public small
    "medium_airport":  "M",   # public medium
    "large_airport":   "L",   # public large (commercial)
    "heliport":        "H",
    "seaplane_base":   "W",   # water
    "balloonport":     "B",
    # closed / unknown → filtered out during parse
}


class AirportRecord:
    """Lightweight airport descriptor (one per nearby airport)."""
    __slots__ = ("ident", "atype", "lat", "lon", "elev_ft", "name")

    def __init__(self, ident, atype, lat, lon, elev_ft, name=""):
        self.ident   = ident     # e.g. "KSEZ"
        self.atype   = atype     # one of _TYPE_MAP values
        self.lat     = lat
        self.lon     = lon
        self.elev_ft = elev_ft
        self.name    = name


def _parse_csv(csv_path: str):
    """Parse OurAirports CSV.  Returns list of tuples suitable for numpy array.

    Each tuple: (ident, atype, lat, lon, elev_ft)
    """
    records = []
    with open(csv_path, "r", encoding="utf-8", errors="ignore", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                atype_full = (row.get("type") or "").strip().lower()
                if atype_full not in _TYPE_MAP:
                    continue
                lat_s = (row.get("latitude_deg") or "").strip()
                lon_s = (row.get("longitude_deg") or "").strip()
                if not lat_s or not lon_s:
                    continue
                lat = float(lat_s)
                lon = float(lon_s)
                ident = (row.get("ident") or "").strip().upper()
                if not ident:
                    continue
                elev_s = (row.get("elevation_ft") or "").strip()
                try:
                    elev = float(elev_s) if elev_s else 0.0
                except ValueError:
                    elev = 0.0
                records.append((ident, _TYPE_MAP[atype_full], lat, lon, elev))
            except (ValueError, TypeError):
                continue
    return records


def _build_cache(csv_path: str, cache_path: str):
    records = _parse_csv(csv_path)
    if HAS_NUMPY and records:
        arr = np.array(records,
                       dtype=[("ident",   "U7"),
                              ("atype",   "U1"),
                              ("lat",     "f4"),
                              ("lon",     "f4"),
                              ("elev_ft", "f4")])
        # Sort by lat so query_nearby can binary-search the lat band.
        arr = arr[np.argsort(arr["lat"], kind="stable")]
        np.save(cache_path, arr)
        return arr
    return records


def load(data_dir: str):
    """Load airport DB from data_dir.  Cache preferred; falls back to CSV parse.
    Returns numpy structured array or None if no data present.

    The array is sorted by latitude so spatial queries can binary-search
    the lat band.  Existing un-sorted caches are sorted in memory at
    load (cheap)."""
    csv_path   = os.path.join(data_dir, CSV_FILENAME)
    cache_path = os.path.join(data_dir, CACHE_FILENAME)

    if HAS_NUMPY and os.path.exists(cache_path):
        if (not os.path.exists(csv_path) or
                os.path.getmtime(cache_path) >= os.path.getmtime(csv_path)):
            try:
                arr = np.load(cache_path, allow_pickle=False)
                if len(arr) > 1 and not (np.diff(arr["lat"]) >= 0).all():
                    arr = arr[np.argsort(arr["lat"], kind="stable")]
                return arr
            except Exception:
                pass

    if not os.path.exists(csv_path):
        return None

    return _build_cache(csv_path, cache_path)


# ── Spatial query ─────────────────────────────────────────────────────────────

_NM_PER_DEG_LAT = 60.0

# Per-load cache of the contiguous lat column for searchsorted; same trick
# as obstacles.py.  72 k records is small, but the strided structured
# access still costs a few ms when run every frame.
_lat_index_cache = (None, None)


def _get_lat_index(arr):
    global _lat_index_cache
    if _lat_index_cache[0] is not arr:
        _lat_index_cache = (arr, np.ascontiguousarray(arr["lat"]))
    return _lat_index_cache[1]


def query_nearby(airports, lat: float, lon: float, radius_nm: float = 20.0):
    """Return airports within radius_nm of (lat, lon).

    With numpy available, returns a structured-array slice sorted by
    distance (nearest first); the per-row Python loop that built
    AirportRecord objects in the previous version added ~5 ms per call
    at 20 nm density.  Pure-Python fallback returns the AirportRecord
    list as before so non-numpy callers still work.
    """
    if airports is None:
        return []

    nm_per_deg_lon = _NM_PER_DEG_LAT * math.cos(math.radians(lat))
    dlat = radius_nm / _NM_PER_DEG_LAT
    dlon = radius_nm / nm_per_deg_lon if nm_per_deg_lon > 0 else radius_nm

    if HAS_NUMPY and hasattr(airports, "dtype"):
        lat_col = _get_lat_index(airports)
        i_lo = int(np.searchsorted(lat_col, lat - dlat, side="left"))
        i_hi = int(np.searchsorted(lat_col, lat + dlat, side="right"))
        candidates = airports[i_lo:i_hi]
        if len(candidates) > 0:
            lon_mask = ((candidates["lon"] >= lon - dlon) &
                        (candidates["lon"] <= lon + dlon))
            candidates = candidates[lon_mask]
        if len(candidates) > 0:
            dlat_r = candidates["lat"] - lat
            dlon_r = candidates["lon"] - lon
            dist_sq = ((dlat_r * _NM_PER_DEG_LAT) ** 2
                       + (dlon_r * nm_per_deg_lon) ** 2)
            within = dist_sq <= radius_nm * radius_nm
            candidates = candidates[within]
            dist_sq = dist_sq[within]
            # Sort by distance ascending (nearest first) — caller relies
            # on this ordering for the "draw farthest first" z-order and
            # for picking the nearest in nav_set_nearest.
            order = np.argsort(dist_sq)
            candidates = candidates[order]
        return candidates

    results = []
    for rec in airports:
        rident, ratype, rlat, rlon, relev = rec
        if abs(rlat - lat) > dlat or abs(rlon - lon) > dlon:
            continue
        dlat_r = rlat - lat
        dlon_r = rlon - lon
        dist_nm = math.hypot(dlat_r * _NM_PER_DEG_LAT,
                             dlon_r * nm_per_deg_lon)
        if dist_nm <= radius_nm:
            results.append((dist_nm, AirportRecord(
                ident=rident, atype=ratype,
                lat=rlat, lon=rlon, elev_ft=relev,
            )))
    results.sort(key=lambda t: t[0])
    return [r for _, r in results]


# ── Disk stats / expiry ───────────────────────────────────────────────────────

def disk_stats(data_dir: str):
    """Return (record_count, used_mb)."""
    csv_path   = os.path.join(data_dir, CSV_FILENAME)
    cache_path = os.path.join(data_dir, CACHE_FILENAME)

    total_bytes = 0
    record_count = 0

    for p in (csv_path, cache_path):
        if os.path.exists(p):
            total_bytes += os.path.getsize(p)

    if HAS_NUMPY and os.path.exists(cache_path):
        try:
            arr = np.load(cache_path, allow_pickle=False)
            record_count = len(arr)
        except Exception:
            pass
    elif os.path.exists(csv_path):
        with open(csv_path, "rb") as f:
            record_count = sum(1 for _ in f) - 1   # minus header

    return record_count, total_bytes / 1_048_576


def download_date(data_dir: str):
    """Return datetime.date of the airport data based on CSV mtime, or None."""
    csv_path = os.path.join(data_dir, CSV_FILENAME)
    if not os.path.exists(csv_path):
        return None
    return _dt.date.fromtimestamp(os.path.getmtime(csv_path))


def is_expired(data_dir: str, expiry_days: int = 28) -> bool:
    """Return True if the airport CSV is older than expiry_days."""
    d = download_date(data_dir)
    if d is None:
        return False
    return (_dt.date.today() - d).days > expiry_days
