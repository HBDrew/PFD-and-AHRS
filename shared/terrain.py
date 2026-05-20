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
# Negative = terrain ABOVE aircraft.  Bands shifted +200 ft vs the textbook
# "red when terrain reaches altitude": red at clearance < 200 ft gives the
# pilot a 200 ft buffer warning before actual contact, in sync with
# TERRAIN_WARNING_FT (200) and TERRAIN_CAUTION_FT (700) in config_base.py.
PALETTE_RELATIVE = [
    (-9999,  (220,  30,  30)),   # terrain above aircraft  → red
    (  200,  (220,  80,   0)),   # 200–300 ft clearance     → deep orange
    (  300,  (200, 130,   0)),   # 300–700 ft               → amber
    (  700,  (140, 100,  40)),   # 700–1200 ft              → brown
    ( 1200,  (100,  75,  35)),   # 1200–2200 ft             → dark brown
    ( 2200,  ( 70,  55,  28)),   # 2200+ ft below           → very dark
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
# LRU-capped to bound RSS on a Pi 4: each SRTM3 tile is ~5.7 MB in memory
# (1201² float32) and SRTM1 tiles are ~52 MB (3601² float32).  Without a cap
# the cache grew unbounded as the user visited new sim airports across CONUS,
# eventually OOM-killing the process.  32 entries is enough for the inner +
# outer mesh extents (~50 nm radius → ~9 tiles) plus comfortable hysteresis.
import collections as _collections
# 16 entries: enough for a 50-nm MFD bbox (~9 tiles) plus hysteresis as
# the aircraft moves into new ones.  Previously 32, which let SRTM1
# tiles (52 MB each) snowball past the Pi Zero 2W's 512 MB RAM during
# extended bench sessions and trigger an oom-kill / reboot.
_TILE_CACHE_MAX = 16
_tile_cache: "_collections.OrderedDict[str, object]" = _collections.OrderedDict()

# Optional resolution gate.  pi_zero/pfd.py calls
# terrain.set_resolution_preference("srtm3") at startup so the tile
# loader treats SRTM1 .hgt files as missing.  This avoids loading
# 52 MB float32 arrays per tile on a 512 MB Pi Zero 2W when the MFD
# tint only samples ~2300 elevation points anyway.  pi4 leaves this
# at "any" so the full-resolution SVT scene still uses SRTM1 when
# present.
_PREFER_RESOLUTION = "any"   # "any" | "srtm3"


def set_resolution_preference(pref: str):
    """Configure which SRTM resolutions the tile loader is willing to
    read.  ``"any"`` (default) opens any .hgt file regardless of size;
    ``"srtm3"`` skips the 25 MB SRTM1 files and only reads SRTM3."""
    global _PREFER_RESOLUTION
    _PREFER_RESOLUTION = pref


def _tile_key(lat_int: int, lon_int: int) -> str:
    ns = 'N' if lat_int >= 0 else 'S'
    ew = 'E' if lon_int >= 0 else 'W'
    return f"{ns}{abs(lat_int):02d}{ew}{abs(lon_int):03d}.hgt"


def _cache_put(key, value):
    """Insert with LRU eviction.  None entries (missing tiles) count toward
    the cap too — without that, repeatedly probing for absent tiles would
    fill the dict with sentinels and starve real tiles."""
    _tile_cache[key] = value
    _tile_cache.move_to_end(key)
    while len(_tile_cache) > _TILE_CACHE_MAX:
        _tile_cache.popitem(last=False)


def load_tile(srtm_dir: str, lat_int: int, lon_int: int):
    """
    Load (or return cached) SRTM tile.
    Returns (data, n_samples) where data is a numpy array or flat list,
    or None if the tile is not found.
    Auto-detects SRTM1 (3601×3601, Mapzen/AWS) vs SRTM3 (1201×1201).
    """
    key = _tile_key(lat_int, lon_int)
    if key in _tile_cache:
        _tile_cache.move_to_end(key)   # mark as most-recently-used
        return _tile_cache[key]

    path = os.path.join(srtm_dir, key)
    if not os.path.exists(path):
        _cache_put(key, None)
        return None

    # Detect resolution from file size (2 bytes per sample)
    file_bytes = os.path.getsize(path)
    if file_bytes == SRTM1_SAMPLES * SRTM1_SAMPLES * 2:
        # Honour the caller's resolution preference — on memory-tight
        # systems (pi_zero) we refuse to load SRTM1 entirely and let the
        # caller treat the tile as missing.  Cache the None so we don't
        # re-stat the file on every sample.
        if _PREFER_RESOLUTION == "srtm3":
            _cache_put(key, None)
            return None
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
    _cache_put(key, result)
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


# ── Coarse global terrain (Mapzen Terrarium PNG, zoom 5) ─────────────────────
# Companion to the high-res SRTM cache.  ~576 PNG tiles cover lat -60°..+75°
# at ~5 km/px — enough resolution for the iPhone-style "mountains above
# horizon" silhouette anywhere in the world without the per-region SRTM
# download.  Same source the iPhone PFD uses (terrain.js).
#
#   URL pattern:  https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png
#   Elevation:    R*256 + G + B/256 - 32768   (metres)

MAPZEN_BASE   = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium"
COARSE_Z      = 5
_COARSE_LAT_LO = -60.0
_COARSE_LAT_HI =  75.0
_COARSE_CACHE_MAX = 64
_coarse_cache = _collections.OrderedDict()


def coarse_latlon_to_tile(lat: float, lon: float, z: int = COARSE_Z):
    """Return (x, y) Mapzen tile coords for the given lat/lon at zoom z."""
    n = 1 << z
    lat = max(-85.0, min(85.0, lat))
    x  = int((lon + 180.0) / 360.0 * n)
    lr = math.log(math.tan((90.0 + lat) * math.pi / 360.0))
    y  = int((1.0 - lr / math.pi) / 2.0 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def coarse_tile_url(z: int, x: int, y: int) -> str:
    return f"{MAPZEN_BASE}/{z}/{x}/{y}.png"


def coarse_tile_path(coarse_dir: str, z: int, x: int, y: int) -> str:
    return os.path.join(coarse_dir, f"{z}_{x}_{y}.png")


def coarse_tile_list(z: int = COARSE_Z):
    """Enumerate (z, x, y) tile triples for the useful lat band at zoom z."""
    n = 1 << z
    _, y_top    = coarse_latlon_to_tile(_COARSE_LAT_HI, 0.0, z)
    _, y_bottom = coarse_latlon_to_tile(_COARSE_LAT_LO, 0.0, z)
    return [(z, tx, ty) for tx in range(n)
                        for ty in range(y_top, y_bottom + 1)]


def _load_coarse_tile(coarse_dir: str, z: int, x: int, y: int):
    """Return (elev_ft_array, n_samples) for one Mapzen tile, or None."""
    key = (z, x, y)
    if key in _coarse_cache:
        _coarse_cache.move_to_end(key)
        return _coarse_cache[key]
    path = coarse_tile_path(coarse_dir, z, x, y)
    if not os.path.exists(path) or not HAS_NUMPY:
        _coarse_cache[key] = None
        while len(_coarse_cache) > _COARSE_CACHE_MAX:
            _coarse_cache.popitem(last=False)
        return None
    try:
        import pygame
        img = pygame.image.load(path)
        # pygame.surfarray.array3d returns (W, H, 3); transpose to (H, W, 3)
        arr = pygame.surfarray.array3d(img).transpose(1, 0, 2)
        r = arr[:, :, 0].astype(np.float32)
        g = arr[:, :, 1].astype(np.float32)
        b = arr[:, :, 2].astype(np.float32)
        elev_m  = r * 256.0 + g + b / 256.0 - 32768.0
        elev_ft = elev_m * 3.28084
        result  = (elev_ft, elev_ft.shape[0])
    except Exception:
        result = None
    _coarse_cache[key] = result
    while len(_coarse_cache) > _COARSE_CACHE_MAX:
        _coarse_cache.popitem(last=False)
    return result


def get_coarse_elevation_ft(coarse_dir: str, lat: float, lon: float) -> float:
    """Return elevation (ft MSL) sampled from the Mapzen coarse layer.  0.0
    when the tile is absent (we treat missing data as sea level — same
    convention as the SRTM fallback in get_elevation_ft)."""
    if not coarse_dir:
        return 0.0
    z = COARSE_Z
    tx, ty = coarse_latlon_to_tile(lat, lon, z)
    res = _load_coarse_tile(coarse_dir, z, tx, ty)
    if res is None:
        return 0.0
    tile, n_samples = res
    # Convert lat/lon to fractional pixel coords within the tile
    n = 1 << z
    x_world = (lon + 180.0) / 360.0 * n
    lr      = math.log(math.tan((90.0 + max(-85.0, min(85.0, lat))) * math.pi / 360.0))
    y_world = (1.0 - lr / math.pi) / 2.0 * n
    col_frac = (x_world - tx) * n_samples
    row_frac = (y_world - ty) * n_samples
    col = max(0, min(n_samples - 1, int(col_frac)))
    row = max(0, min(n_samples - 1, int(row_frac)))
    return float(tile[row, col])


def get_elevation_ft_combined(srtm_dir: str, coarse_dir: str,
                               lat: float, lon: float) -> float:
    """Look up elevation, preferring high-res SRTM when the tile is
    available locally and falling back to the Mapzen coarse layer."""
    if srtm_dir and tile_exists(srtm_dir, lat, lon):
        return get_elevation_ft(srtm_dir, lat, lon)
    return get_coarse_elevation_ft(coarse_dir, lat, lon)


def coarse_disk_stats(coarse_dir: str):
    """Return (tile_count, used_mb) for the Mapzen coarse cache on disk."""
    if not coarse_dir or not os.path.isdir(coarse_dir):
        return 0, 0.0
    total = 0
    count = 0
    for fn in os.listdir(coarse_dir):
        if fn.endswith(".png"):
            count += 1
            try:
                total += os.path.getsize(os.path.join(coarse_dir, fn))
            except OSError:
                pass
    return count, total / 1_048_576
