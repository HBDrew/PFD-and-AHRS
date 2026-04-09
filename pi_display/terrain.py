"""
terrain.py – SRTM elevation loader and SVT terrain renderer.

SRTM3 tiles: 1°×1° at 3 arc-second resolution (~90 m), 1201×1201 samples.
File naming: N34W112.hgt (SW corner lat/lon, always positive-integer degrees).

SVT rendering uses a scanline perspective projection:
  For each row below the horizon in the AI viewport, compute the ground
  distance represented by that row (given aircraft altitude and pitch angle),
  sample the terrain elevation at that bearing/distance, then colour by
  elevation relative to the aircraft altitude.
"""

import os
import math
import struct

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

import pygame

# ── SRTM constants ─────────────────────────────────────────────────────────────
SRTM_SAMPLES  = 1201        # samples per side (SRTM3)
SRTM_STEP_DEG = 1 / 1200    # degrees between samples
VOID_ELEV     = -32768      # SRTM void marker

# ── Terrain colour palette (elevation-relative to aircraft) ───────────────────
# colours keyed by (clearance_ft): clearance = aircraft_alt - terrain_elev
# Negative = terrain ABOVE aircraft
_PALETTE = [
    (-9999,  (220,  30,  30)),   # terrain above aircraft  → red
    (    0,  (220,  80,   0)),   # 0–100 ft clearance       → deep orange
    (  100,  (200, 130,   0)),   # 100–500 ft               → amber
    (  500,  (140, 100,  40)),   # 500–1000 ft              → brown
    ( 1000,  (100,  75,  35)),   # 1000–2000 ft             → dark brown
    ( 2000,  ( 70,  55,  28)),   # 2000+ ft below           → very dark
]

# Absolute elevation palette (used when no aircraft alt available / demo)
_ABS_PALETTE = [
    (    0, ( 30, 100,  30)),    # sea level / low          → dark green
    ( 2000, ( 80, 110,  40)),    # low hills                → olive
    ( 4000, (130,  95,  45)),    # high plateau             → tan-brown
    ( 6000, (160,  65,  25)),    # Sedona / mesa            → reddish-brown
    ( 8000, (190,  80,  35)),    # high terrain             → light red-brown
    (10000, (210, 195, 185)),    # very high                → grey-white
    (13000, (240, 240, 245)),    # snow                     → white
]


def _interp_colour(palette, value):
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
    Returns a numpy array [1201×1201] int16, or None if not found.
    """
    key = _tile_key(lat_int, lon_int)
    if key in _tile_cache:
        return _tile_cache[key]

    path = os.path.join(srtm_dir, key)
    if not os.path.exists(path):
        _tile_cache[key] = None
        return None

    if HAS_NUMPY:
        data = np.fromfile(path, dtype='>i2').reshape((SRTM_SAMPLES, SRTM_SAMPLES))
        data = data.astype(np.float32)
        data[data == VOID_ELEV] = 0
        data *= 3.28084   # metres → feet
    else:
        # Pure-Python fallback (slow, 2-byte big-endian signed int)
        with open(path, 'rb') as f:
            raw = f.read()
        n = SRTM_SAMPLES * SRTM_SAMPLES
        data = list(struct.unpack(f'>{n}h', raw))
        data = [0 if v == VOID_ELEV else v * 3.28084 for v in data]

    _tile_cache[key] = data
    return data


def get_elevation_ft(srtm_dir: str, lat: float, lon: float) -> float:
    """
    Sample terrain elevation at (lat, lon) in feet MSL.
    Returns 0 if no SRTM data available.
    """
    lat_int = int(math.floor(lat))
    lon_int = int(math.floor(lon))
    tile = load_tile(srtm_dir, lat_int, lon_int)
    if tile is None:
        return 0.0

    # Row 0 = northernmost; row 1200 = southernmost
    row = (lat_int + 1 - lat) / SRTM_STEP_DEG
    col = (lon - lon_int) / SRTM_STEP_DEG
    row = max(0, min(SRTM_SAMPLES - 1, row))
    col = max(0, min(SRTM_SAMPLES - 1, col))

    if HAS_NUMPY:
        # Bilinear interpolation
        r0, c0 = int(row), int(col)
        r1 = min(r0 + 1, SRTM_SAMPLES - 1)
        c1 = min(c0 + 1, SRTM_SAMPLES - 1)
        dr, dc = row - r0, col - c0
        v = (tile[r0, c0] * (1-dr) * (1-dc) +
             tile[r0, c1] * (1-dr) * dc +
             tile[r1, c0] * dr * (1-dc) +
             tile[r1, c1] * dr * dc)
        return float(v)
    else:
        r0, c0 = int(row), int(col)
        idx = r0 * SRTM_SAMPLES + c0
        return float(tile[idx])


# ── SVT terrain surface renderer ───────────────────────────────────────────────
_DEG = math.pi / 180
_NM_FT = 6076.12


def render_svt(
    srtm_dir: str,
    ai_w: int,
    ai_h: int,
    pitch_deg: float,
    roll_deg: float,
    hdg_deg: float,
    alt_ft: float,
    lat: float,
    lon: float,
    v_fov_deg: float = 40.0,
    h_fov_deg: float = 55.0,
) -> pygame.Surface:
    """
    Render a synthetic-vision terrain background.
    Returns a pygame.Surface (ai_w × ai_h, RGBA) suitable for blitting
    onto the AI region.

    Uses a scanline perspective projection: each pixel row corresponds to
    a pitch angle; for rows below the horizon, we compute the terrain
    distance and sample elevation.
    """
    surf = pygame.Surface((ai_w, ai_h), pygame.SRCALPHA)
    cx, cy = ai_w // 2, ai_h // 2

    px_per_deg_v = ai_h / v_fov_deg
    px_per_deg_h = ai_w / h_fov_deg

    # Horizon row (before roll) in the unrotated AI frame
    horizon_y = cy + pitch_deg * px_per_deg_v

    if HAS_NUMPY:
        _render_svt_numpy(surf, ai_w, ai_h, cx, cy, horizon_y,
                          pitch_deg, hdg_deg, alt_ft, lat, lon,
                          px_per_deg_v, px_per_deg_h,
                          srtm_dir)
    else:
        _render_svt_software(surf, ai_w, ai_h, cx, cy, horizon_y,
                              pitch_deg, hdg_deg, alt_ft, lat, lon,
                              px_per_deg_v, px_per_deg_h,
                              srtm_dir)

    # Apply roll rotation
    if abs(roll_deg) > 0.5:
        # pygame.transform.rotate rotates CCW; we want CW for right-bank
        rotated = pygame.transform.rotate(surf, roll_deg)
        # Crop back to ai_w × ai_h centered
        rx, ry = rotated.get_size()
        ox, oy = (rx - ai_w) // 2, (ry - ai_h) // 2
        surf = rotated.subsurface(
            pygame.Rect(ox, oy, ai_w, ai_h)
        ).copy()

    return surf


def _sky_colour(y: int, ai_h: int, horizon_y: float):
    """Blue sky gradient: darker at top, lighter near horizon."""
    t = max(0.0, min(1.0, 1.0 - (horizon_y - y) / max(1, horizon_y)))
    r = int(10  + t * 80)
    g = int(42  + t * 110)
    b = int(80  + t * 140)
    return (r, g, b, 255)


def _ground_colour_abs(elev_ft: float):
    """Colour terrain by absolute elevation (feet)."""
    return _interp_colour(_ABS_PALETTE, elev_ft) + (255,)


def _ground_colour_rel(clearance_ft: float):
    """Colour terrain by clearance (aircraft_alt - terrain_elev)."""
    return _interp_colour(_PALETTE, clearance_ft) + (255,)


def _render_svt_numpy(surf, ai_w, ai_h, cx, cy, horizon_y,
                      pitch_deg, hdg_deg, alt_ft, lat, lon,
                      px_per_deg_v, px_per_deg_h, srtm_dir):
    """Numpy-accelerated row-by-row SVT render."""
    pixels = pygame.surfarray.pixels3d(surf)
    alpha  = pygame.surfarray.pixels_alpha(surf)

    # Sky rows
    sky_rows = range(0, min(ai_h, int(horizon_y) + 1))
    for y in sky_rows:
        t = max(0.0, min(1.0, 1.0 - (horizon_y - y) / max(1.0, horizon_y)))
        r = int(10  + t * 80)
        g = int(42  + t * 110)
        b = int(80  + t * 140)
        pixels[:, y, 0] = r
        pixels[:, y, 1] = g
        pixels[:, y, 2] = b
        alpha[:, y] = 255

    # Ground rows
    for y in range(max(0, int(horizon_y)), ai_h):
        # Pitch angle this row looks down (degrees below horizon)
        angle_below = (y - horizon_y) / px_per_deg_v  # > 0 below horizon
        if angle_below < 0.1:
            angle_below = 0.1
        angle_rad = angle_below * _DEG

        # Ground distance (flat-earth approximation)
        dist_ft = alt_ft / math.tan(angle_rad) if angle_rad > 0.001 else 9e6
        dist_nm = dist_ft / _NM_FT

        # x-column bearing offsets
        col_indices = np.arange(ai_w, dtype=np.float32)
        bear_offsets = (col_indices - cx) / px_per_deg_h  # degrees

        # For each column, compute lat/lon of terrain point
        bear_deg = (hdg_deg + bear_offsets) % 360
        bear_rad = bear_deg * _DEG

        # Flat-earth displacement
        d_lat = (dist_nm / 60.0) * np.cos(bear_rad)
        d_lon = (dist_nm / 60.0) * np.sin(bear_rad) / max(0.001, math.cos(lat * _DEG))

        terr_lat = lat + d_lat
        terr_lon = lon + d_lon

        # Sample elevation (vectorised lookup per column)
        for x in range(ai_w):
            elev = get_elevation_ft(srtm_dir, float(terr_lat[x]), float(terr_lon[x]))
            clearance = alt_ft - elev
            col = _interp_colour(_PALETTE, clearance)
            pixels[x, y, 0] = col[0]
            pixels[x, y, 1] = col[1]
            pixels[x, y, 2] = col[2]
            alpha[x, y] = 255

    del pixels, alpha  # release surfarray lock


def _render_svt_software(surf, ai_w, ai_h, cx, cy, horizon_y,
                         pitch_deg, hdg_deg, alt_ft, lat, lon,
                         px_per_deg_v, px_per_deg_h, srtm_dir):
    """Pure-Python fallback SVT (renders every 8 pixels → fast enough for Pi)."""
    STEP = 8  # pixel stride for sampling
    for y in range(0, ai_h, STEP):
        if y < horizon_y:
            # Sky
            t = max(0.0, min(1.0, 1.0 - (horizon_y - y) / max(1.0, horizon_y)))
            col = (int(10 + t*80), int(42 + t*110), int(80 + t*140))
            pygame.draw.rect(surf, col + (255,), (0, y, ai_w, STEP))
        else:
            angle_below = max(0.1, (y - horizon_y) / px_per_deg_v)
            angle_rad = angle_below * _DEG
            dist_ft = alt_ft / math.tan(angle_rad) if angle_rad > 0.001 else 9e6
            dist_nm = dist_ft / _NM_FT

            for x in range(0, ai_w, STEP):
                bear_off = (x - cx) / px_per_deg_h
                bear_rad = ((hdg_deg + bear_off) % 360) * _DEG
                d_lat = (dist_nm / 60.0) * math.cos(bear_rad)
                d_lon = (dist_nm / 60.0) * math.sin(bear_rad) / max(0.001, math.cos(lat * _DEG))
                elev = get_elevation_ft(srtm_dir, lat + d_lat, lon + d_lon)
                col = _interp_colour(_PALETTE, alt_ft - elev)
                pygame.draw.rect(surf, col + (255,), (x, y, STEP, STEP))
