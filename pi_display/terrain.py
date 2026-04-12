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
SRTM3_SAMPLES = 1201        # samples per side (SRTM3 – 3 arc-second)
SRTM1_SAMPLES = 3601        # samples per side (SRTM1 – 1 arc-second, Mapzen/AWS)
SRTM_SAMPLES  = SRTM3_SAMPLES   # legacy alias
SRTM_STEP_DEG = 1 / 1200    # degrees between samples (SRTM3 default)
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


_SVT_STEP = 4  # block-sample ground pixels by this factor (4 → 16× fewer SRTM lookups)


def _render_svt_numpy(surf, ai_w, ai_h, cx, cy, horizon_y,
                      pitch_deg, hdg_deg, alt_ft, lat, lon,
                      px_per_deg_v, px_per_deg_h, srtm_dir):
    """
    Fully vectorised SVT render — zero Python loops over pixels.

    Ground pixels are block-sampled at 1/_SVT_STEP resolution for SRTM
    lookups, then nearest-neighbour expanded back to full resolution.
    For a 484×200 ground area with STEP=4: 121×50 = ~6 K lookups instead
    of 97 K, yielding a ~16× speedup on the bilinear-interpolation step.
    """
    pixels = pygame.surfarray.pixels3d(surf)   # shape (ai_w, ai_h, 3)
    alpha  = pygame.surfarray.pixels_alpha(surf) # shape (ai_w, ai_h)

    hy = int(max(0, min(ai_h, horizon_y)))

    # ── Sky rows: broadcast gradient across all columns ───────────────────────
    if hy > 0:
        sy = np.arange(hy, dtype=np.float32)                      # (hy,)
        t  = np.clip(1.0 - (horizon_y - sy) / max(1.0, horizon_y), 0.0, 1.0)
        pixels[:, :hy, 0] = (10  + t * 80 ).astype(np.uint8)     # broadcast (ai_w,hy)
        pixels[:, :hy, 1] = (42  + t * 110).astype(np.uint8)
        pixels[:, :hy, 2] = (80  + t * 140).astype(np.uint8)
        alpha [:, :hy]    = 255

    # ── Ground rows ───────────────────────────────────────────────────────────
    n_gnd = ai_h - hy
    if n_gnd <= 0:
        del pixels, alpha
        return

    STEP = _SVT_STEP

    # Block-sample: compute SRTM elevations on a coarse grid, then expand.
    # Sampled row y-coords (every STEP rows starting from hy)
    gy_s     = np.arange(hy, ai_h, STEP, dtype=np.float32)   # (n_gnd_s,)
    # Sampled column x-coords (every STEP columns)
    col_s    = np.arange(0, ai_w, STEP, dtype=np.float32)    # (ai_w_s,)
    n_gnd_s  = len(gy_s)
    ai_w_s   = len(col_s)

    # Pitch angle each sampled row looks below the horizon (degrees)
    angle_below = np.maximum((gy_s - horizon_y) / px_per_deg_v, 0.1)  # (n_gnd_s,)
    angle_rad   = angle_below * _DEG

    # Ground distance in nm for each sampled row (flat-earth)
    dist_nm = (alt_ft / np.where(angle_rad > 0.001,
                                 np.tan(angle_rad), 1e9)) / _NM_FT  # (n_gnd_s,)

    # Column bearing offsets for sampled columns only
    bear_offsets = (col_s - cx) / px_per_deg_h                        # (ai_w_s,)
    bear_rad     = ((hdg_deg + bear_offsets) % 360) * _DEG            # (ai_w_s,)
    cos_bear     = np.cos(bear_rad)   # (ai_w_s,)
    sin_bear     = np.sin(bear_rad)   # (ai_w_s,)
    cos_lat      = max(1e-6, math.cos(lat * _DEG))

    # Broadcast (n_gnd_s, 1) × (1, ai_w_s) → (n_gnd_s, ai_w_s) terrain positions
    d_lat    = (dist_nm[:, None] / 60.0) * cos_bear[None, :]
    d_lon    = (dist_nm[:, None] / 60.0) * sin_bear[None, :] / cos_lat
    terr_lat = lat + d_lat    # (n_gnd_s, ai_w_s)
    terr_lon = lon + d_lon    # (n_gnd_s, ai_w_s)

    # ── Vectorised SRTM lookup on coarse grid ─────────────────────────────────
    elev_arr = np.zeros((n_gnd_s, ai_w_s), dtype=np.float32)

    lat_int_arr = np.floor(terr_lat).astype(np.int32)   # (n_gnd_s, ai_w_s)
    lon_int_arr = np.floor(terr_lon).astype(np.int32)   # (n_gnd_s, ai_w_s)
    # Encode with offset so negative longitudes encode correctly into int64
    enc = ((lat_int_arr.astype(np.int64) + 90)  * 1000 +
           (lon_int_arr.astype(np.int64) + 360))
    tile_keys = np.unique(enc)

    for key in tile_keys:
        tla = int(key) // 1000 - 90
        tlo = int(key) %  1000 - 360

        result = load_tile(srtm_dir, tla, tlo)
        if result is None:
            continue
        tile_data, n_s = result   # tile_data: numpy (n_s, n_s) float32

        mask = (lat_int_arr == tla) & (lon_int_arr == tlo)   # (n_gnd_s, ai_w_s)
        if not mask.any():
            continue

        step = 1.0 / (n_s - 1)

        # SRTM tile indices for every sampled pixel (clipped to valid range)
        row_f = np.clip((tla + 1 - terr_lat) / step, 0, n_s - 1)
        col_f = np.clip((terr_lon - tlo)      / step, 0, n_s - 1)
        r0 = np.minimum(row_f.astype(np.int32), n_s - 2)
        c0 = np.minimum(col_f.astype(np.int32), n_s - 2)
        r1 = r0 + 1
        c1 = c0 + 1
        dr = (row_f - r0).astype(np.float32)
        dc = (col_f - c0).astype(np.float32)

        # Bilinear interpolation using fancy indexing (all in numpy C layer)
        elev = (tile_data[r0, c0] * (1 - dr) * (1 - dc) +
                tile_data[r0, c1] * (1 - dr) * dc        +
                tile_data[r1, c0] * dr        * (1 - dc) +
                tile_data[r1, c1] * dr        * dc)       # (n_gnd_s, ai_w_s)

        elev_arr[mask] = elev[mask]

    # ── Vectorised colour from clearance ──────────────────────────────────────
    clearance = alt_ft - elev_arr   # (n_gnd_s, ai_w_s)

    thresholds = np.array([-9999, 0, 100, 500, 1000, 2000], dtype=np.float32)
    pal_r = np.array([220, 220, 200, 140, 100,  70], dtype=np.uint8)
    pal_g = np.array([ 30,  80, 130, 100,  75,  55], dtype=np.uint8)
    pal_b = np.array([ 30,   0,   0,  40,  35,  28], dtype=np.uint8)

    bins = np.searchsorted(thresholds, clearance, side='right') - 1
    bins = np.clip(bins, 0, len(pal_r) - 1)   # (n_gnd_s, ai_w_s)

    # Nearest-neighbour upsample: repeat each sample STEP times in both axes,
    # then clip back to the exact ground dimensions.
    bins_full = np.repeat(bins, STEP, axis=0)[:n_gnd]    # (n_gnd, ai_w_s)
    bins_full = np.repeat(bins_full, STEP, axis=1)[:, :ai_w]  # (n_gnd, ai_w)

    # Assign colours — transpose because pixels array is (ai_w, ai_h, 3)
    pixels[:, hy:, 0] = pal_r[bins_full].T         # (ai_w, n_gnd)
    pixels[:, hy:, 1] = pal_g[bins_full].T
    pixels[:, hy:, 2] = pal_b[bins_full].T
    alpha [:, hy:]    = 255

    del pixels, alpha   # release surfarray lock



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
