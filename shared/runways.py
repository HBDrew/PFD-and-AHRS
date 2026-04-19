"""
runways.py – Runway database parser and spatial query.

Companion to airports.py: parses OurAirports runways.csv into a compact
NumPy structured array so runway polygons and extended centerlines can
be rendered on the attitude indicator.

CSV columns (OurAirports schema, subset we use):
    airport_ident            — links to airports ("KSEZ")
    length_ft, width_ft      — runway dimensions
    surface                  — "ASP" / "CON" / "TURF" / "DIRT" / ...
    lighted                  — "1" / "0" or "yes" / "no"
    closed                   — "1" / "0" (filtered out on parse)
    le_ident, he_ident       — low-end and high-end identifiers ("09","27")
    le_latitude_deg, le_longitude_deg, le_elevation_ft, le_heading_degT
    he_latitude_deg, he_longitude_deg, he_elevation_ft, he_heading_degT

Spatial query returns runways within a radius (e.g. the airport query
radius) so caller can draw runway polygons at close range.

Public API
----------
load(data_dir)                 → numpy structured array or None
query_nearby(arr, lat, lon, r) → list of RunwayRecord
"""

import os
import math
import csv

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

CSV_FILENAME   = "runways.csv"
CACHE_FILENAME = "runways_cache.npy"
RUNWAYS_CSV_URL = "https://raw.githubusercontent.com/davidmegginson/ourairports-data/main/runways.csv"


class RunwayRecord:
    """One physical runway (two thresholds)."""
    __slots__ = ("airport", "length_ft", "width_ft", "surface", "lighted",
                 "le_ident", "he_ident",
                 "le_lat", "le_lon", "le_elev_ft", "le_hdg",
                 "he_lat", "he_lon", "he_elev_ft", "he_hdg")

    def __init__(self, airport, length_ft, width_ft, surface, lighted,
                 le_ident, he_ident,
                 le_lat, le_lon, le_elev_ft, le_hdg,
                 he_lat, he_lon, he_elev_ft, he_hdg):
        self.airport   = airport
        self.length_ft = length_ft
        self.width_ft  = width_ft
        self.surface   = surface
        self.lighted   = lighted
        self.le_ident  = le_ident
        self.he_ident  = he_ident
        self.le_lat    = le_lat
        self.le_lon    = le_lon
        self.le_elev_ft = le_elev_ft
        self.le_hdg    = le_hdg
        self.he_lat    = he_lat
        self.he_lon    = he_lon
        self.he_elev_ft = he_elev_ft
        self.he_hdg    = he_hdg

    @property
    def centre_lat(self) -> float:
        return (self.le_lat + self.he_lat) / 2.0

    @property
    def centre_lon(self) -> float:
        return (self.le_lon + self.he_lon) / 2.0


def _truthy(s: str) -> bool:
    return s.strip().lower() in ("1", "yes", "true", "y")


def _f(s: str, default: float = 0.0) -> float:
    s = (s or "").strip()
    if not s:
        return default
    try:
        return float(s)
    except ValueError:
        return default


def _parse_csv(csv_path: str):
    records = []
    with open(csv_path, "r", encoding="utf-8", errors="ignore", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            if _truthy(row.get("closed") or ""):
                continue
            le_lat = _f(row.get("le_latitude_deg"))
            le_lon = _f(row.get("le_longitude_deg"))
            he_lat = _f(row.get("he_latitude_deg"))
            he_lon = _f(row.get("he_longitude_deg"))
            # Need both ends positioned to render the polygon
            if (le_lat == 0 and le_lon == 0) or (he_lat == 0 and he_lon == 0):
                continue
            try:
                length = _f(row.get("length_ft"))
                width  = _f(row.get("width_ft"), 50.0)
                if length < 100:   # sanity — skip ultra-short/null records
                    continue
                le_hdg = _f(row.get("le_heading_degT"))
                he_hdg = _f(row.get("he_heading_degT"))
                records.append((
                    (row.get("airport_ident") or "").strip().upper(),
                    length, width,
                    (row.get("surface") or "").strip().upper()[:6],
                    _truthy(row.get("lighted") or ""),
                    (row.get("le_ident") or "").strip().upper()[:4],
                    (row.get("he_ident") or "").strip().upper()[:4],
                    le_lat, le_lon,
                    _f(row.get("le_elevation_ft")), le_hdg,
                    he_lat, he_lon,
                    _f(row.get("he_elevation_ft")), he_hdg,
                ))
            except (ValueError, TypeError):
                continue
    return records


def _build_cache(csv_path: str, cache_path: str):
    records = _parse_csv(csv_path)
    if HAS_NUMPY and records:
        dtype = np.dtype([
            ("airport",   "U7"),
            ("length_ft", "f4"),
            ("width_ft",  "f4"),
            ("surface",   "U6"),
            ("lighted",   "?"),
            ("le_ident",  "U4"),
            ("he_ident",  "U4"),
            ("le_lat",    "f4"), ("le_lon", "f4"),
            ("le_elev_ft","f4"), ("le_hdg", "f4"),
            ("he_lat",    "f4"), ("he_lon", "f4"),
            ("he_elev_ft","f4"), ("he_hdg", "f4"),
        ])
        arr = np.array(records, dtype=dtype)
        np.save(cache_path, arr)
        return arr
    return records


def load(data_dir: str):
    """Load runway DB from data_dir.  Cache preferred; falls back to CSV parse.
    Returns numpy structured array or None."""
    csv_path   = os.path.join(data_dir, CSV_FILENAME)
    cache_path = os.path.join(data_dir, CACHE_FILENAME)
    if HAS_NUMPY and os.path.exists(cache_path):
        if (not os.path.exists(csv_path) or
                os.path.getmtime(cache_path) >= os.path.getmtime(csv_path)):
            try:
                return np.load(cache_path, allow_pickle=False)
            except Exception:
                pass
    if not os.path.exists(csv_path):
        return None
    return _build_cache(csv_path, cache_path)


# ── Spatial query ─────────────────────────────────────────────────────────────

_NM_PER_DEG_LAT = 60.0


def query_nearby(runways, lat: float, lon: float, radius_nm: float = 10.0):
    """Return list of RunwayRecord whose centre is within radius_nm."""
    if runways is None:
        return []

    nm_per_deg_lon = _NM_PER_DEG_LAT * math.cos(math.radians(lat))
    dlat = radius_nm / _NM_PER_DEG_LAT
    dlon = radius_nm / nm_per_deg_lon if nm_per_deg_lon > 0 else radius_nm

    results = []

    if HAS_NUMPY and hasattr(runways, "dtype"):
        # Mid-point latitude/longitude for coarse filter
        mid_lat = (runways["le_lat"] + runways["he_lat"]) * 0.5
        mid_lon = (runways["le_lon"] + runways["he_lon"]) * 0.5
        mask = (
            (mid_lat >= lat - dlat) & (mid_lat <= lat + dlat) &
            (mid_lon >= lon - dlon) & (mid_lon <= lon + dlon)
        )
        candidates = runways[mask]
        for row in candidates:
            cdlat = float((row["le_lat"] + row["he_lat"]) * 0.5) - lat
            cdlon = float((row["le_lon"] + row["he_lon"]) * 0.5) - lon
            d_nm = math.hypot(cdlat * _NM_PER_DEG_LAT, cdlon * nm_per_deg_lon)
            if d_nm <= radius_nm:
                results.append(RunwayRecord(
                    airport=str(row["airport"]),
                    length_ft=float(row["length_ft"]),
                    width_ft=float(row["width_ft"]),
                    surface=str(row["surface"]),
                    lighted=bool(row["lighted"]),
                    le_ident=str(row["le_ident"]),
                    he_ident=str(row["he_ident"]),
                    le_lat=float(row["le_lat"]),
                    le_lon=float(row["le_lon"]),
                    le_elev_ft=float(row["le_elev_ft"]),
                    le_hdg=float(row["le_hdg"]),
                    he_lat=float(row["he_lat"]),
                    he_lon=float(row["he_lon"]),
                    he_elev_ft=float(row["he_elev_ft"]),
                    he_hdg=float(row["he_hdg"]),
                ))
    return results


def disk_stats(data_dir: str):
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
    return record_count, total_bytes / 1_048_576
