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
from terrain import load_tile  # noqa: E402


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
_HITS_CYAN   = (0, 200, 255)        # matches HITS palette in hits.py


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


# Vectorised palette breakpoints, used by np.interp inside _build_tint.
# Mirrors PALETTE_ABSOLUTE in shared/terrain.py — keep in sync.
if HAS_NUMPY:
    _PAL_X = np.array([0, 2000, 4000, 6000, 8000, 10000, 13000],
                      dtype=np.float32)
    _PAL_R = np.array([30,  80, 130, 160, 190, 210, 240], dtype=np.float32)
    _PAL_G = np.array([100, 110,  95,  65,  80, 195, 240], dtype=np.float32)
    _PAL_B = np.array([30,  40,  45,  25,  35, 185, 245], dtype=np.float32)


def _build_tint(srtm_dir, water_dir, c_lat, c_lon, range_nm, size_px, oversize):
    """Render a north-up hypsometric tint surface centred on (c_lat, c_lon).

    Fully vectorised over the (n × n) sample grid: sample points are
    grouped by their integer SRTM/water tile, the tile is loaded once,
    and bulk fancy-indexing fills the elevation and water arrays.  Then
    np.interp does the elevation → RGB palette lookup for all pixels in
    three calls (one per channel).  No per-cell Python loops in the hot
    path, so cache misses rebuild in a few ms even with water sampling
    enabled.

    `range_nm` is the radius shown at the inset's shorter axis, so the
    full inset diameter is 2·range_nm.  `oversize` (≥ 1.0) inflates the
    rendered area so a track-up rotation doesn't reveal the corners."""
    n = _TINT_N
    span_nm = 2.0 * range_nm * oversize
    span_lat = span_nm / _NM_PER_DEG_LAT
    cos_lat = max(0.05, math.cos(math.radians(c_lat)))
    span_lon = span_lat / cos_lat

    target_px = max(8, int(size_px * oversize))

    if not HAS_NUMPY:
        tile = pygame.Surface((n, n))
        tile.fill(_BG)
        return pygame.transform.smoothscale(tile, (target_px, target_px))

    lat_top = c_lat + span_lat * 0.5
    lat_bot = c_lat - span_lat * 0.5
    lon_lf  = c_lon - span_lon * 0.5
    lon_rt  = c_lon + span_lon * 0.5

    # Sample lat/lon as (n × n) grids (broadcast — no copy).
    rows_lat = np.linspace(lat_top, lat_bot, n, dtype=np.float64)
    cols_lon = np.linspace(lon_lf,  lon_rt,  n, dtype=np.float64)
    sample_lat = np.broadcast_to(rows_lat[:, None], (n, n))
    sample_lon = np.broadcast_to(cols_lon[None, :], (n, n))

    elevs = np.zeros((n, n), dtype=np.float32)
    water = np.zeros((n, n), dtype=bool)

    lat_int = np.floor(sample_lat).astype(np.int32)
    lon_int = np.floor(sample_lon).astype(np.int32)
    # Pack (lat, lon) into a single integer key so np.unique groups by tile.
    enc = ((lat_int.astype(np.int64) + 90) * 1000 +
           (lon_int.astype(np.int64) + 360))

    for tile_key in np.unique(enc):
        tla = int(tile_key) // 1000 - 90
        tlo = int(tile_key) %  1000 - 360
        mask = (lat_int == tla) & (lon_int == tlo)
        if not mask.any():
            continue

        # SRTM bulk-sample
        sres = load_tile(srtm_dir, tla, tlo)
        if sres is not None:
            sarr, sn = sres
            sstep = 1.0 / (sn - 1)
            srow = np.clip(
                np.round((tla + 1 - sample_lat) / sstep).astype(np.int32),
                0, sn - 1)
            scol = np.clip(
                np.round((sample_lon - tlo) / sstep).astype(np.int32),
                0, sn - 1)
            elevs[mask] = sarr[srow[mask], scol[mask]]

        # Water bulk-sample (only when a tile exists for this 1° square).
        if water_dir:
            wres = _water_mod.load_tile(water_dir, tla, tlo)
            if wres is not None:
                wmask, wn = wres
                wstep = 1.0 / (wn - 1)
                wrow = np.clip(
                    np.round((tla + 1 - sample_lat) / wstep).astype(np.int32),
                    0, wn - 1)
                wcol = np.clip(
                    np.round((sample_lon - tlo) / wstep).astype(np.int32),
                    0, wn - 1)
                water[mask] = wmask[wrow[mask], wcol[mask]] > 0

    # Vectorised palette lookup: one np.interp per channel, then stack.
    rgb_r = np.interp(elevs, _PAL_X, _PAL_R).astype(np.uint8)
    rgb_g = np.interp(elevs, _PAL_X, _PAL_G).astype(np.uint8)
    rgb_b = np.interp(elevs, _PAL_X, _PAL_B).astype(np.uint8)
    rgb = np.stack([rgb_r, rgb_g, rgb_b], axis=-1)
    if water.any():
        rgb[water] = _WATER_TINT_RGB

    # pygame.surfarray expects (w, h, 3); our rgb is (rows, cols, 3) so swap.
    tile = pygame.surfarray.make_surface(rgb.swapaxes(0, 1))
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
           airport_types_visible=None, gs_kt=0.0):
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

    # ── Direct-to / approach course line + waypoint diamond ─────────────────
    # Course line is drawn STATICALLY from the activation point to the
    # waypoint — same convention the SVT direct-to trace uses.  This way
    # the line represents the chosen course, not a constantly-updating
    # bearing-to-waypoint arrow as the aircraft drifts off course.
    # Colour follows mode: cyan when an approach is loaded (matches the
    # HITS palette), magenta when this is a regular direct-to.
    if (settings.get("map_show_directto", True)
            and direct_to is not None and direct_to.get("ident")):
        approach_active = bool(direct_to.get("approach_active"))
        course_col = _HITS_CYAN if approach_active else _D2_MAGENTA
        # Activation point — fall back to the waypoint itself if no
        # activation lat/lon was captured (rare; happens before the
        # first frame after a fresh activate).
        ax_lat = float(direct_to.get("act_lat") or direct_to["lat"])
        ax_lon = float(direct_to.get("act_lon") or direct_to["lon"])
        ax_x, ax_y = _project(ax_lat, ax_lon)
        wpx, wpy = _project(direct_to["lat"], direct_to["lon"])
        pygame.draw.line(surf, course_col,
                         (int(ax_x), int(ax_y)),
                         (int(wpx), int(wpy)), 2)
        d = 5
        pygame.draw.polygon(surf, course_col,
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

        # ETE — only when a direct-to is active.  Bottom-right corner,
        # magenta to match the D2 course-line convention.  Uses GPS
        # ground speed; below 3 kt (taxi threshold) the ETE is unstable
        # so we render dashes rather than a noise-driven number.
        if direct_to is not None and direct_to.get("ident"):
            n_nm = (direct_to["lat"] - lat) * _NM_PER_DEG_LAT
            e_nm = ((direct_to["lon"] - lon)
                    * _NM_PER_DEG_LAT * cos_lat)
            d_nm = math.hypot(n_nm, e_nm)
            if gs_kt >= 3.0 and d_nm > 0.0:
                hours = d_nm / gs_kt
                if hours < 1.0:
                    mm_, ss_ = divmod(int(round(hours * 3600)), 60)
                    ete_lbl = f"ETE {mm_}:{ss_:02d}"
                elif hours < 10.0:
                    h_  = int(hours)
                    mm_ = int(round((hours - h_) * 60))
                    if mm_ == 60:
                        h_ += 1
                        mm_ = 0
                    ete_lbl = f"ETE {h_}:{mm_:02d}"
                else:
                    ete_lbl = "ETE --:--"
            else:
                ete_lbl = "ETE --:--"
            ete_col = (_HITS_CYAN
                       if direct_to.get("approach_active")
                       else _D2_MAGENTA)
            ete_surf = font.render(ete_lbl, True, ete_col)
            ete_w = ete_surf.get_width()
            ete_h = ete_surf.get_height()
            # Small dark backplate so the magenta text reads over any
            # tint colour underneath.
            plate = pygame.Surface((ete_w + 6, ete_h + 2), pygame.SRCALPHA)
            plate.fill((0, 0, 0, 160))
            surf.blit(plate,
                      (x + w - ete_w - 7, y + h - ete_h - 4))
            surf.blit(ete_surf,
                      (x + w - ete_w - 4, y + h - ete_h - 3))


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
