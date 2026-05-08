"""
moving_map.py – 2D top-down moving-map inset for the lower-left of the AI.

Pure pygame; no GL.  Reuses the existing shared/airports, shared/runways,
shared/obstacles and shared/terrain caches that the SVT and overlays
already keep loaded.

Layers (painter's order):
  1. Black background
  2. Hypsometric terrain tint   (cached surface, rebuilt only on pan/zoom)
  3. Runway centerlines
  4. Obstacle dots
  5. Airport markers
  6. Direct-to course line + waypoint diamond
  7. Own-ship symbol at centre
  8. Range ring + corner labels
  9. Frame border

Track-up: own-ship at centre, map rotates so track is up.
North-up: own-ship anchored at centre, north stays up.
"""

import math
import os
import pygame

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

import sys as _sys
_HERE = os.path.dirname(os.path.abspath(__file__))
_sys.path.insert(0, os.path.join(_HERE, "..", "shared"))

import airports as _apt_mod    # noqa: E402
import runways as _rwy_mod     # noqa: E402
import obstacles as _obs_mod   # noqa: E402
import water as _water_mod     # noqa: E402
from terrain import (          # noqa: E402
    load_tile, interp_colour, PALETTE_ABSOLUTE,
)


# Water tint — slightly darker than the SVT mid-distance water_color so
# the inset reads as "ocean" rather than "sky reflection" against the
# panel background.
_WATER_TINT_RGB = (45, 80, 120)


_NM_PER_DEG_LAT = 60.0

# Inset chrome
_BG          = (0, 0, 0)
_FRAME       = (60, 80, 110)
_LABEL       = (180, 200, 220)
_RING        = (110, 140, 180)
_OWNSHIP     = (255, 220, 50)
_RWY_COL     = (220, 220, 230)
_OBS_COL     = (220, 80, 80)
_APT_PUB     = (60, 220, 80)
_APT_HELI    = (200, 80, 200)
_APT_WATER   = (80, 160, 220)
_APT_OTHER   = (200, 160, 80)
_D2_MAGENTA  = (220, 0, 220)


# ── Hypsometric terrain tint cache ────────────────────────────────────────────
# Building the tint is the only expensive work on the inset.  At cruise the
# centre moves slowly, so quantising it lets one cached surface serve many
# frames.  The cache holds a few entries to absorb pan motion and zoom changes
# without thrashing.

_tint_cache: dict = {}
_TINT_CACHE_MAX = 6
_TINT_N = 64       # elevation samples per side; smoothscaled up to fit


def _quantise_centre(lat, lon, range_nm):
    """Snap the centre to ~10% of the visible range so light pan motion
    re-uses the same cached surface."""
    step_deg = max(0.002, (range_nm / _NM_PER_DEG_LAT) * 0.10)
    cos_lat = max(0.05, math.cos(math.radians(lat)))
    return (round(lat / step_deg) * step_deg,
            round(lon / (step_deg / cos_lat)) * (step_deg / cos_lat))


def _build_tint(srtm_dir, water_dir, c_lat, c_lon, range_nm, size_px, oversize):
    """Render a north-up hypsometric tint surface centred on (c_lat, c_lon).

    `range_nm` is the radius shown at the inset's shorter axis, so the full
    inset diameter is 2·range_nm.  `oversize` (≥ 1.0) inflates the rendered
    area so a track-up rotation doesn't reveal the corners.  Water cells
    (sampled from ``water_dir``) override the elevation palette so the
    inset reads "ocean" rather than "low green land" off-coast."""
    n = _TINT_N
    span_nm = 2.0 * range_nm * oversize
    span_lat = span_nm / _NM_PER_DEG_LAT
    cos_lat = max(0.05, math.cos(math.radians(c_lat)))
    span_lon = span_lat / cos_lat

    tile = pygame.Surface((n, n))

    if HAS_NUMPY:
        # Vectorised: for each integer SRTM tile that the patch overlaps,
        # bulk-sample its grid into our (n × n) image.  Avoids 4096 Python
        # bilinear lookups for cache misses.
        lat_top = c_lat + span_lat * 0.5
        lat_bot = c_lat - span_lat * 0.5
        lon_lf  = c_lon - span_lon * 0.5
        lon_rt  = c_lon + span_lon * 0.5

        rows_lat = np.linspace(lat_top, lat_bot, n, dtype=np.float64)
        cols_lon = np.linspace(lon_lf,  lon_rt,  n, dtype=np.float64)
        elevs = np.zeros((n, n), dtype=np.float32)
        water = np.zeros((n, n), dtype=bool)

        # Group columns by their integer-lon tile to amortise tile loads.
        for la_idx, la in enumerate(rows_lat):
            lat_int = int(math.floor(la))
            for lo_idx, lo in enumerate(cols_lon):
                lon_int = int(math.floor(lo))
                tile_data = load_tile(srtm_dir, lat_int, lon_int)
                if tile_data is not None:
                    arr, ns = tile_data
                    step = 1.0 / (ns - 1)
                    row = (lat_int + 1 - la) / step
                    col = (lo - lon_int) / step
                    row = max(0, min(ns - 1, row))
                    col = max(0, min(ns - 1, col))
                    r0, c0 = int(row), int(col)
                    elevs[la_idx, lo_idx] = float(arr[r0, c0])
                if water_dir:
                    try:
                        water[la_idx, lo_idx] = _water_mod.is_water(
                            water_dir, float(la), float(lo))
                    except Exception:
                        pass

        # Map to RGB via the absolute palette, then build a Surface.
        rgb = np.zeros((n, n, 3), dtype=np.uint8)
        for r in range(n):
            for c in range(n):
                if water[r, c]:
                    rgb[r, c] = _WATER_TINT_RGB
                else:
                    rgb[r, c] = interp_colour(
                        PALETTE_ABSOLUTE, float(elevs[r, c]))
        # Pygame surface is (w, h) but make_surface wants (w, h, 3) → use
        # the array transposed to (n, n, 3) -> (cols, rows, 3).
        tile = pygame.surfarray.make_surface(rgb.swapaxes(0, 1))
    else:
        # Pure-Python fallback — slower but correct
        tile.fill(_BG)

    target_px = max(8, int(size_px * oversize))
    return pygame.transform.smoothscale(tile, (target_px, target_px))


def _tint_get(srtm_dir, water_dir, c_lat, c_lon, range_nm, size_px, oversize):
    if not srtm_dir:
        return None
    q_lat, q_lon = _quantise_centre(c_lat, c_lon, range_nm)
    # water_dir is part of the cache key so toggling water tiles on/off
    # invalidates stale tints.  Empty string == no water sampling.
    key = (round(q_lat, 4), round(q_lon, 4),
           float(range_nm), int(size_px), round(oversize, 2),
           bool(water_dir))
    if key in _tint_cache:
        # Move to end to mark MRU
        s = _tint_cache.pop(key)
        _tint_cache[key] = s
        return s
    surf = _build_tint(srtm_dir, water_dir, q_lat, q_lon,
                       range_nm, size_px, oversize)
    _tint_cache[key] = surf
    while len(_tint_cache) > _TINT_CACHE_MAX:
        _tint_cache.pop(next(iter(_tint_cache)))
    return surf


# ── Public API ────────────────────────────────────────────────────────────────

def render(surf, rect, lat, lon, alt_ft, hdg_deg, track_deg, orient,
           range_nm, settings,
           airports_arr=None, runways_arr=None, obstacles_arr=None,
           srtm_dir="", water_dir="", direct_to=None, font=None,
           airport_types_visible=None):
    """Draw the moving-map inset into ``surf`` at ``rect = (x, y, w, h)``.

    ``orient`` is "trk" or "nrth"; ``range_nm`` is the half-extent shown
    at the inset's shorter axis (snap to 1/2/5/10/20/40 nm).  ``settings``
    is ``disp["ds"]`` — used for per-layer toggles.

    ``direct_to`` is an optional dict ``{"lat", "lon", "ident"}``; when
    present, draws the magenta course line and waypoint diamond.
    """
    x, y, w, h = rect
    if w < 16 or h < 16:
        return

    pygame.draw.rect(surf, _BG, rect)

    # Map rotation: track-up rotates so current track points up.
    # Fall back to magnetic heading when GPS track is None or 0 — that's
    # the typical "stationary, GPS hasn't computed a track yet" state.
    # Using hdg in that case keeps the inset rotating with the nose so
    # the toggle is visibly different from north-up even before takeoff.
    if orient == "trk":
        if track_deg is None or track_deg == 0.0:
            rot_deg = float(hdg_deg or 0.0)
        else:
            rot_deg = float(track_deg)
    else:
        rot_deg = 0.0

    half_min = min(w, h) / 2
    px_per_nm = half_min / max(0.5, range_nm)
    cx, cy = x + w / 2.0, y + h / 2.0
    cos_lat = max(0.05, math.cos(math.radians(lat)))

    # World → inset projection, with optional track-up rotation.
    # Sign matches `pygame.transform.rotate(tint, rot_deg)` below: that
    # call rotates the cached terrain surface CCW by rot_deg so that
    # current track ends up at the top of the inset.  Projecting world
    # points the same direction (CCW by rot_deg in the math frame where
    # +n is up) keeps runways, airports, obstacles and the direct-to
    # course line visually aligned with the rotated tint instead of
    # mirrored across the centre.
    if rot_deg != 0.0:
        rr = math.radians(rot_deg)
        sin_r, cos_r = math.sin(rr), math.cos(rr)
    else:
        sin_r, cos_r = 0.0, 1.0

    def _project(la, lo):
        n_nm = (la - lat) * _NM_PER_DEG_LAT
        e_nm = (lo - lon) * _NM_PER_DEG_LAT * cos_lat
        if rot_deg != 0.0:
            e2 = e_nm * cos_r - n_nm * sin_r
            n2 = e_nm * sin_r + n_nm * cos_r
            e_nm, n_nm = e2, n2
        return cx + e_nm * px_per_nm, cy - n_nm * px_per_nm

    old_clip = surf.get_clip()
    surf.set_clip(rect)

    # ── Hypsometric terrain tint ─────────────────────────────────────────────
    # Water sampling is gated on the same map_show_water toggle the user
    # already has on the Display setup screen — off → cells render as
    # whatever PALETTE_ABSOLUTE puts at sea level (dark green), on →
    # ocean cells override with a water-blue tint.
    if settings.get("map_show_terrain", True) and srtm_dir:
        oversize = 1.0 if orient == "nrth" else 1.45
        _wd = water_dir if settings.get("map_show_water", True) else ""
        tint = _tint_get(srtm_dir, _wd, lat, lon, range_nm,
                         max(w, h), oversize)
        if tint is not None:
            if orient == "trk" and rot_deg != 0.0:
                tint_r = pygame.transform.rotate(tint, rot_deg)
            else:
                tint_r = tint
            tr = tint_r.get_rect(center=(int(cx), int(cy)))
            surf.blit(tint_r, tr)

    # Slightly darker veil under vector layers so labels read cleanly
    veil = pygame.Surface((w, h), pygame.SRCALPHA)
    veil.fill((0, 0, 0, 60))
    surf.blit(veil, (x, y))

    # ── Runways ──────────────────────────────────────────────────────────────
    if settings.get("map_show_runways", True) and runways_arr is not None:
        nearby = _rwy_mod.query_nearby(runways_arr, lat, lon,
                                       radius_nm=range_nm * 1.4)
        for r in nearby:
            x1, y1 = _project(r.le_lat, r.le_lon)
            x2, y2 = _project(r.he_lat, r.he_lon)
            w_px = max(1, int(px_per_nm * (r.width_ft / 6076.0) * 0.5))
            pygame.draw.line(surf, _RWY_COL,
                             (int(x1), int(y1)), (int(x2), int(y2)), w_px)

    # ── Obstacles ────────────────────────────────────────────────────────────
    # Match the SVT display convention: only show obstacles within
    # 1000 ft below the aircraft (collision-risk window); obstacles
    # above are always shown (they're a hazard).  The defaults in
    # obstacles.query_nearby already encode that, so just pass alt_ft.
    if (settings.get("map_show_obstacles", True)
            and obstacles_arr is not None):
        nearby = _obs_mod.query_nearby(obstacles_arr, lat, lon,
                                       radius_nm=range_nm * 1.4,
                                       alt_ft=alt_ft)
        if HAS_NUMPY and hasattr(nearby, "dtype") and len(nearby) > 0:
            for la, lo in zip(nearby["lat"], nearby["lon"]):
                ox, oy = _project(float(la), float(lo))
                pygame.draw.circle(surf, _OBS_COL, (int(ox), int(oy)), 2)
        else:
            for o in nearby:
                ox, oy = _project(o.lat, o.lon)
                pygame.draw.circle(surf, _OBS_COL, (int(ox), int(oy)), 2)

    # ── Airports ─────────────────────────────────────────────────────────────
    # Per-type filtering matches the main PFD: caller passes a set of
    # visible atype letters (S/M/L = public, H = helo, W = water, B = other).
    # Default ``None`` = show every type the master toggle covers.
    if settings.get("map_show_airports", True) and airports_arr is not None:
        nearby = _apt_mod.query_nearby(airports_arr, lat, lon,
                                       radius_nm=range_nm * 1.4)
        if HAS_NUMPY and hasattr(nearby, "dtype") and len(nearby) > 0:
            ids   = nearby["ident"]
            types = nearby["atype"]
            lats  = nearby["lat"]
            lons  = nearby["lon"]
            for i in range(len(nearby)):
                atype = str(types[i])
                if (airport_types_visible is not None
                        and atype not in airport_types_visible):
                    continue
                ax2, ay2 = _project(float(lats[i]), float(lons[i]))
                ix, iy = int(ax2), int(ay2)
                if atype == "H":
                    pygame.draw.circle(surf, _APT_HELI, (ix, iy), 3)
                elif atype == "W":
                    pygame.draw.circle(surf, _APT_WATER, (ix, iy), 3, 1)
                elif atype == "B":
                    pygame.draw.circle(surf, _APT_OTHER, (ix, iy), 2)
                else:
                    pygame.draw.circle(surf, _APT_PUB, (ix, iy),
                                       4 if atype in ("M", "L") else 3)
                if font is not None and range_nm <= 10:
                    lbl = font.render(str(ids[i]), True, _APT_PUB)
                    surf.blit(lbl, (ix + 5, iy - 7))

    # ── Direct-to course + waypoint diamond ──────────────────────────────────
    if (settings.get("map_show_directto", True)
            and direct_to is not None and direct_to.get("ident")):
        wpx, wpy = _project(direct_to["lat"], direct_to["lon"])
        pygame.draw.line(surf, _D2_MAGENTA, (int(cx), int(cy)),
                         (int(wpx), int(wpy)), 2)
        d = 5
        pygame.draw.polygon(surf, _D2_MAGENTA,
                            [(int(wpx),     int(wpy) - d),
                             (int(wpx) + d, int(wpy)),
                             (int(wpx),     int(wpy) + d),
                             (int(wpx) - d, int(wpy))])

    # ── Range ring ───────────────────────────────────────────────────────────
    pygame.draw.circle(surf, _RING, (int(cx), int(cy)),
                       int(range_nm * px_per_nm), 1)

    # ── Own-ship chevron ─────────────────────────────────────────────────────
    # Track-up: chevron always points up.  North-up: chevron rotates to
    # current track so the pilot still sees direction of motion (falling
    # back to hdg when GPS track isn't reliable, same rule as above).
    if orient == "trk":
        own_rot = 0.0
    else:
        if track_deg is None or track_deg == 0.0:
            own_rot = float(hdg_deg or 0.0)
        else:
            own_rot = float(track_deg)
    s = 7
    base_pts = [(0, -s), (s, s), (0, s * 0.4), (-s, s)]
    cr = math.cos(math.radians(own_rot))
    sr = math.sin(math.radians(own_rot))
    rotated = [(cx + p[0] * cr - p[1] * sr,
                cy + p[0] * sr + p[1] * cr) for p in base_pts]
    pygame.draw.polygon(surf, _OWNSHIP,
                        [(int(rx), int(ry)) for rx, ry in rotated])

    surf.set_clip(old_clip)

    # ── Frame + corner labels ────────────────────────────────────────────────
    pygame.draw.rect(surf, _FRAME, rect, width=1)

    if font is not None:
        rng_lbl = f"{range_nm:g} NM"
        orient_lbl = "TRK↑" if orient == "trk" else "N↑"
        rng_surf = font.render(rng_lbl, True, _LABEL)
        orient_surf = font.render(orient_lbl, True, _LABEL)
        surf.blit(rng_surf, (x + 4, y + 2))
        surf.blit(orient_surf,
                  (x + w - orient_surf.get_width() - 4, y + 2))


def hit_test(rect, x, y) -> bool:
    """Return True if (x, y) falls inside the inset rect — used by the
    main event dispatcher to decide whether a touch belongs to the map."""
    rx, ry, rw, rh = rect
    return rx <= x <= rx + rw and ry <= y <= ry + rh


# ── Pinch-zoom state ──────────────────────────────────────────────────────────
# Snap-points for the discrete zoom levels.

ZOOM_LEVELS = (1, 2, 5, 10, 20, 40)


def zoom_in(current_nm: int) -> int:
    """Step the range to the next-smaller snap point (zoom in)."""
    levels = ZOOM_LEVELS
    for i, lvl in enumerate(levels):
        if current_nm <= lvl and i > 0:
            return levels[i - 1]
    return levels[0]


def zoom_out(current_nm: int) -> int:
    """Step the range to the next-larger snap point (zoom out)."""
    levels = ZOOM_LEVELS
    for i, lvl in enumerate(levels):
        if current_nm < lvl:
            return lvl
    return levels[-1]
