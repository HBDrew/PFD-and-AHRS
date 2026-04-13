"""
svt_renderer.py – Synthetic Vision Terrain renderer for Pi 4.

CURRENT STATE (Phase 1):
    Pygame-based scanline SVT renderer carried forward from the original
    codebase.  Renders terrain below the horizon using SRTM elevation
    data with clearance-based colouring.

PLANNED (Phase 2 – OpenGL migration):
    Full OpenGL ES 2.0/3.0 vector graphics renderer using the Pi 4's
    VideoCore VI GPU.  This will enable:
    - True 3D perspective terrain mesh (terrain visible ABOVE the horizon)
    - Texture-mapped terrain with elevation colouring
    - Vector-drawn PFD instruments (tapes, ladder, arcs as GL geometry)
    - Higher display resolutions without performance penalty
    - Synthetic runway rendering
    - Smooth anti-aliased rendering via MSAA

    Candidate libraries:
    - moderngl + EGL (pure Python, good Pi 4 support)
    - pi3d (Pi-optimised OpenGL ES wrapper)
    - Raw GLES2 via ctypes (maximum control, most work)

SRTM data is loaded via shared/terrain.py (tile cache + elevation lookups).
"""

import math
import os

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

import pygame

# Import shared terrain utilities
from terrain import (
    load_tile, get_elevation_ft,
    PALETTE_RELATIVE, interp_colour,
    HAS_NUMPY as TERRAIN_HAS_NUMPY,
)

# ── Constants ─────────────────────────────────────────────────────────────────
_DEG = math.pi / 180
_NM_FT = 6076.12
_SVT_STEP = 8   # block-sample stride (8 → 64× fewer SRTM lookups per frame)


def _sky_colour(y: int, ai_h: int, horizon_y: float):
    """Blue sky gradient: darker at top, lighter near horizon."""
    t = max(0.0, min(1.0, 1.0 - (horizon_y - y) / max(1, horizon_y)))
    r = int(10  + t * 80)
    g = int(42  + t * 110)
    b = int(80  + t * 140)
    return (r, g, b, 255)


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
    Render a synthetic-vision terrain background (pygame scanline version).

    Returns a pygame.Surface (ai_w × ai_h, RGBA) suitable for blitting
    onto the AI region.

    This will be replaced by an OpenGL renderer in Phase 2.
    """
    surf = pygame.Surface((ai_w, ai_h), pygame.SRCALPHA)
    cx, cy = ai_w // 2, ai_h // 2

    px_per_deg_v = ai_h / v_fov_deg
    px_per_deg_h = ai_w / h_fov_deg

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
        rotated = pygame.transform.rotate(surf, roll_deg)
        rx, ry = rotated.get_size()
        ox, oy = (rx - ai_w) // 2, (ry - ai_h) // 2
        surf = rotated.subsurface(
            pygame.Rect(ox, oy, ai_w, ai_h)
        ).copy()

    return surf


def _render_svt_numpy(surf, ai_w, ai_h, cx, cy, horizon_y,
                      pitch_deg, hdg_deg, alt_ft, lat, lon,
                      px_per_deg_v, px_per_deg_h, srtm_dir):
    """
    Fully vectorised SVT render — zero Python loops over pixels.

    Ground pixels are block-sampled at 1/_SVT_STEP resolution for SRTM
    lookups, then nearest-neighbour expanded back to full resolution.
    """
    pixels = pygame.surfarray.pixels3d(surf)
    alpha  = pygame.surfarray.pixels_alpha(surf)
    try:
        hy = int(max(0, min(ai_h, horizon_y)))

        # ── Sky rows ──────────────────────────────────────────────────────────
        if hy > 0:
            sy = np.arange(hy, dtype=np.float32)
            t  = np.clip(1.0 - (horizon_y - sy) / max(1.0, horizon_y), 0.0, 1.0)
            pixels[:, :hy, 0] = (10  + t * 80 ).astype(np.uint8)
            pixels[:, :hy, 1] = (42  + t * 110).astype(np.uint8)
            pixels[:, :hy, 2] = (80  + t * 140).astype(np.uint8)
            alpha [:, :hy]    = 255

        # ── Ground rows ───────────────────────────────────────────────────────
        n_gnd = ai_h - hy
        if n_gnd <= 0:
            return

        STEP = _SVT_STEP

        gy_s     = np.arange(hy, ai_h, STEP, dtype=np.float32)
        col_s    = np.arange(0, ai_w, STEP, dtype=np.float32)
        n_gnd_s  = len(gy_s)
        ai_w_s   = len(col_s)

        angle_below = np.maximum((gy_s - horizon_y) / px_per_deg_v, 0.1)
        angle_rad   = angle_below * _DEG
        dist_nm = (alt_ft / np.where(angle_rad > 0.001,
                                     np.tan(angle_rad), 1e9)) / _NM_FT

        bear_offsets = (col_s - cx) / px_per_deg_h
        bear_rad     = ((hdg_deg + bear_offsets) % 360) * _DEG
        cos_bear     = np.cos(bear_rad)
        sin_bear     = np.sin(bear_rad)
        cos_lat      = max(1e-6, math.cos(lat * _DEG))

        d_lat    = (dist_nm[:, None] / 60.0) * cos_bear[None, :]
        d_lon    = (dist_nm[:, None] / 60.0) * sin_bear[None, :] / cos_lat
        terr_lat = lat + d_lat
        terr_lon = lon + d_lon

        # ── Vectorised SRTM lookup on coarse grid ────────────────────────────
        elev_arr = np.zeros((n_gnd_s, ai_w_s), dtype=np.float32)

        lat_int_arr = np.floor(terr_lat).astype(np.int32)
        lon_int_arr = np.floor(terr_lon).astype(np.int32)
        enc = ((lat_int_arr.astype(np.int64) + 90)  * 1000 +
               (lon_int_arr.astype(np.int64) + 360))
        tile_keys = np.unique(enc)

        for key in tile_keys:
            tla = int(key) // 1000 - 90
            tlo = int(key) %  1000 - 360

            result = load_tile(srtm_dir, tla, tlo)
            if result is None:
                continue
            tile_data, n_s = result

            mask = (lat_int_arr == tla) & (lon_int_arr == tlo)
            if not mask.any():
                continue

            step = 1.0 / (n_s - 1)

            row_i = np.clip(
                np.round((tla + 1 - terr_lat) / step).astype(np.int32),
                0, n_s - 1)
            col_i = np.clip(
                np.round((terr_lon - tlo)      / step).astype(np.int32),
                0, n_s - 1)
            elev_arr[mask] = tile_data[row_i, col_i][mask]

        # ── Vectorised colour from clearance ──────────────────────────────────
        clearance = alt_ft - elev_arr

        thresholds = np.array([-9999, 0, 100, 500, 1000, 2000], dtype=np.float32)
        pal_r = np.array([220, 220, 200, 140, 100,  70], dtype=np.uint8)
        pal_g = np.array([ 30,  80, 130, 100,  75,  55], dtype=np.uint8)
        pal_b = np.array([ 30,   0,   0,  40,  35,  28], dtype=np.uint8)

        bins = np.searchsorted(thresholds, clearance, side='right') - 1
        bins = np.clip(bins, 0, len(pal_r) - 1)

        bins_full = np.repeat(bins, STEP, axis=0)[:n_gnd]
        bins_full = np.repeat(bins_full, STEP, axis=1)[:, :ai_w]

        pixels[:, hy:, 0] = pal_r[bins_full].T
        pixels[:, hy:, 1] = pal_g[bins_full].T
        pixels[:, hy:, 2] = pal_b[bins_full].T
        alpha [:, hy:]    = 255

    finally:
        del pixels, alpha


def _render_svt_software(surf, ai_w, ai_h, cx, cy, horizon_y,
                         pitch_deg, hdg_deg, alt_ft, lat, lon,
                         px_per_deg_v, px_per_deg_h, srtm_dir):
    """Pure-Python fallback SVT (renders every 8 pixels)."""
    STEP = 8
    for y in range(0, ai_h, STEP):
        if y < horizon_y:
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
                col = interp_colour(PALETTE_RELATIVE, alt_ft - elev)
                pygame.draw.rect(surf, col + (255,), (x, y, STEP, STEP))


# ──────────────────────────────────────────────────────────────────────────────
# PHASE 2 STUB: OpenGL SVT renderer
# ──────────────────────────────────────────────────────────────────────────────
#
# The following class is a placeholder for the OpenGL-based SVT renderer
# that will replace the pygame scanline renderer above.
#
# Design goals for the OpenGL SVT:
#   1. Build a terrain mesh from SRTM elevation data (triangle strip grid)
#   2. Render the mesh with a perspective projection camera positioned at
#      the aircraft's lat/lon/alt, looking along the heading vector
#   3. Apply clearance-based vertex colouring (same palette as above)
#   4. Terrain ABOVE the horizon is naturally visible in 3D projection
#   5. Sky is rendered as a gradient background behind the terrain mesh
#   6. Later: texture mapping, synthetic runway, moving map underlay
#
# class OpenGLSVTRenderer:
#     """OpenGL ES SVT renderer for Pi 4 (VideoCore VI GPU)."""
#
#     def __init__(self, display_w, display_h):
#         self.w = display_w
#         self.h = display_h
#         # TODO: EGL context creation
#         # TODO: Shader compilation (vertex + fragment)
#         # TODO: Terrain mesh VBO construction
#
#     def update_terrain_mesh(self, srtm_dir, lat, lon, radius_nm=5.0):
#         """Rebuild the terrain mesh around the current position."""
#         # TODO: Sample SRTM grid in a radius around aircraft
#         # TODO: Build triangle strip with elevation vertices
#         # TODO: Upload to GPU VBO
#         pass
#
#     def render(self, pitch, roll, hdg, alt, lat, lon) -> pygame.Surface:
#         """Render one SVT frame and return as a pygame Surface."""
#         # TODO: Set camera position/orientation from aircraft state
#         # TODO: Render terrain mesh with clearance colouring
#         # TODO: Read framebuffer back to pygame Surface
#         pass
