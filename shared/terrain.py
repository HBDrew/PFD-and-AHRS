"""
terrain.py – SRTM elevation loader and query functions.

Shared by both Pi Zero 2W and Pi 4 display versions.

SRTM3 tiles: 1°×1° at 3 arc-second resolution (~90 m), 1201×1201 samples.
SRTM1 tiles: 1°×1° at 1 arc-second resolution (~30 m), 3601×3601 samples.
File naming: N34W112.hgt (SW corner lat/lon, always positive-integer degrees).

This module provides:
  - Tile loading and caching
  - Single-point elevation lookup (for TAWS alerting)
  - Colour palettes for terrain rendering

SVT rendering is handled separately by each display version:
  - Pi Zero: no SVT (plain sky/ground horizon)
  - Pi 4: OpenGL-based 3D SVT renderer (pi4/svt_renderer.py)
"""

import os
import math
import struct

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False


# ── SRTM constants ─────────────────────────────────────────────────────────────
SRTM3_SAMPLES = 1201        # samples per side (SRTM3 – 3 arc-second)
SRTM1_SAMPLES = 3601        # samples per side (SRTM1 – 1 arc-second, Mapzen/AWS)
SRTM_SAMPLES  = SRTM3_SAMPLES   # legacy alias
SRTM_STEP_DEG = 1 / 1200    # degrees between samples (SRTM3 default)
VOID_ELEV     = -32768      # SRTM void marker

# ── Terrain colour palette (elevation-relative to aircraft) ───────────────────
# colours keyed by (clearance_ft): clearance = aircraft_alt - terrain_elev
# Negative = terrain ABOVE aircraft
PALETTE_RELATIVE = [
    (-9999,  (220,  30,  30)),   # terrain above aircraft  → red
    (    0,  (220,  80,   0)),   # 0–100 ft clearance       → deep orange
    (  100,  (200, 130,   0)),   # 100–500 ft               → amber
    (  500,  (140, 100,  40)),   # 500–1000 ft              → brown
    ( 1000,  (100,  75,  35)),   # 1000–2000 ft             → dark brown
    ( 2000,  ( 70,  55,  28)),   # 2000+ ft below           → very dark
]

# Absolute elevation palette (used when no aircraft alt available / demo)
PALETTE_ABSOLUTE = [
    (    0, ( 30, 100,  30)),    # sea level / low          → dark green
    ( 2000, ( 80, 110,  40)),    # low hills                → olive
    ( 4000, (130,  95,  45)),    # high plateau             → tan-brown
    ( 6000, (160,  65,  25)),    # Sedona / mesa            → reddish-brown
    ( 8000, (190,  80,  35)),    # high terrain             → light red-brown
    (10000, (210, 195, 185)),    # very high                → grey-white
    (13000, (240, 240, 245)),    # snow                     → white
]


def interp_colour(palette, value):
    """Linear interpolate colour from a (threshold, colour) palette."""
    if value <= palette[0][0]:
        return palette[0][1]
    for i in range(1, len(palette)):
        lo_v, lo_c = palette[i-1]
        hi_v, hi_c = palette[i]
        if value <= hi_v:
            t = (value - lo_v) / (hi_v - lo_v)
            return tuple(int(lo_c[j] + t * (hi_c[j] - lo_c[j])) for j in range(3))
    return palette[-1][1]


# ── SRTM tile cache ────────────────────────────────────────────────────────────
_tile_cache: dict = {}


def _tile_key(lat_int: int, lon_int: int) -> str:
    ns = 'N' if lat_int >= 0 else 'S'
    ew = 'E' if lon_int >= 0 else 'W'
    return f"{ns}{abs(lat_int):02d}{ew}{abs(lon_int):03d}.hgt"


def load_tile(srtm_dir: str, lat_int: int, lon_int: int):
    """
    Load (or return cached) SRTM tile.
    Returns (data, n_samples) where data is a numpy array or flat list,
    or None if the tile is not found.
    Auto-detects SRTM1 (3601×3601, Mapzen/AWS) vs SRTM3 (1201×1201).
    """
    key = _tile_key(lat_int, lon_int)
    if key in _tile_cache:
        return _tile_cache[key]

    path = os.path.join(srtm_dir, key)
    if not os.path.exists(path):
        _tile_cache[key] = None
        return None

    # Detect resolution from file size (2 bytes per sample)
    file_bytes = os.path.getsize(path)
    if file_bytes == SRTM1_SAMPLES * SRTM1_SAMPLES * 2:
        n_samples = SRTM1_SAMPLES
    else:
        n_samples = SRTM3_SAMPLES  # default / fallback

    if HAS_NUMPY:
        data = np.fromfile(path, dtype='>i2').reshape((n_samples, n_samples))
        data = data.astype(np.float32)
        data[data == VOID_ELEV] = 0
        data *= 3.28084   # metres → feet
    else:
        # Pure-Python fallback (slow, 2-byte big-endian signed int)
        with open(path, 'rb') as f:
            raw = f.read()
        n = n_samples * n_samples
        data = list(struct.unpack(f'>{n}h', raw))
        data = [0 if v == VOID_ELEV else v * 3.28084 for v in data]

    result = (data, n_samples)
    _tile_cache[key] = result
    return result


def get_elevation_ft(srtm_dir: str, lat: float, lon: float) -> float:
    """
    Sample terrain elevation at (lat, lon) in feet MSL.
    Returns 0 if no SRTM data available.
    """
    lat_int = int(math.floor(lat))
    lon_int = int(math.floor(lon))
    result = load_tile(srtm_dir, lat_int, lon_int)
    if result is None:
        return 0.0

    tile, n_samples = result
    step_deg = 1.0 / (n_samples - 1)

    # Row 0 = northernmost; row (n_samples-1) = southernmost
    row = (lat_int + 1 - lat) / step_deg
    col = (lon - lon_int) / step_deg
    row = max(0, min(n_samples - 1, row))
    col = max(0, min(n_samples - 1, col))

    if HAS_NUMPY:
        # Bilinear interpolation
        r0, c0 = int(row), int(col)
        r1 = min(r0 + 1, n_samples - 1)
        c1 = min(c0 + 1, n_samples - 1)
        dr, dc = row - r0, col - c0
        v = (tile[r0, c0] * (1-dr) * (1-dc) +
             tile[r0, c1] * (1-dr) * dc +
             tile[r1, c0] * dr * (1-dc) +
             tile[r1, c1] * dr * dc)
        return float(v)
    else:
        r0, c0 = int(row), int(col)
        idx = r0 * n_samples + c0
        return float(tile[idx])


def tile_exists(srtm_dir: str, lat: float, lon: float) -> bool:
    """Return True if an SRTM tile exists for the given coordinates."""
    lat_int = int(math.floor(lat))
    lon_int = int(math.floor(lon))
    key = _tile_key(lat_int, lon_int)
    path = os.path.join(srtm_dir, key)
    return os.path.exists(path)


def tile_name(lat_int: int, lon_int: int) -> str:
    """Public alias for _tile_key."""
    return _tile_key(lat_int, lon_int)
