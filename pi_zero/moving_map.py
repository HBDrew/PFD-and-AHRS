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
import threading
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
try:
    import runways as _rwy_mod   # noqa: E402
except ImportError:
    _rwy_mod = None
import obstacles as _obs_mod   # noqa: E402
import water as _water_mod     # noqa: E402
from terrain import load_tile  # noqa: E402


# Water tint — slightly darker than the SVT mid-distance water_color so
# the inset reads as "ocean" rather than "sky reflection" against the
# panel background.
_WATER_TINT_RGB = (45, 80, 120)


_NM_PER_DEG_LAT = 60.0


def _gc_interp(la1, lo1, la2, lo2, f):
    """Lat/lon at fraction f ∈ [0, 1] along the great circle from 1 to 2.
    Same slerp the SVT direct-to trace uses — keeps the inset's D2 line
    visually consistent with the CDI (which measures XTK off the GC) and
    the 3D trace painted on the AI."""
    phi1 = math.radians(la1); lam1 = math.radians(lo1)
    phi2 = math.radians(la2); lam2 = math.radians(lo2)
    dphi = phi2 - phi1
    dlam = lam2 - lam1
    a = (math.sin(dphi * 0.5) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlam * 0.5) ** 2)
    d = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    if d < 1e-9:
        return la1, lo1
    sd = math.sin(d)
    A = math.sin((1.0 - f) * d) / sd
    B = math.sin(f * d) / sd
    x = A * math.cos(phi1) * math.cos(lam1) + B * math.cos(phi2) * math.cos(lam2)
    y = A * math.cos(phi1) * math.sin(lam1) + B * math.cos(phi2) * math.sin(lam2)
    z = A * math.sin(phi1) + B * math.sin(phi2)
    return (math.degrees(math.atan2(z, math.hypot(x, y))),
            math.degrees(math.atan2(y, x)))

# Inset chrome
_BG          = (0, 0, 0)
_FRAME       = (60, 80, 110)
_LABEL       = (180, 200, 220)
_RING        = (110, 140, 180)
_OWNSHIP     = (255, 220, 50)
_RWY_COL     = (220, 220, 230)
_OBS_COL     = (220, 80, 80)
_APT_PUB     = (235, 235, 235)   # neutral white: METAR dots own the green/blue/red
_APT_HELI    = (200, 80, 200)
_APT_WATER   = (80, 160, 220)
_APT_OTHER   = (200, 160, 80)
_D2_MAGENTA  = (220, 0, 220)
_HITS_CYAN   = (0, 200, 255)        # matches HITS palette in hits.py
# ADS-B traffic symbol colours, TCAS-style: red resolution-class alert,
# amber proximate, cyan everything else with a position.
_TFC_ALERT     = (255, 60, 60)
_TFC_PROXIMATE = (255, 180, 0)
_TFC_OTHER     = (0, 220, 255)
# METAR flight-category colours — mirror shared/wx.FLIGHT_CAT_COLORS so a
# station dot reads the same standard VFR/MVFR/IFR/LIFR ramp pilots expect.
_WX_CAT_COLORS = {"VFR": (0, 200, 0), "MVFR": (40, 120, 255),
                  "IFR": (235, 40, 40), "LIFR": (220, 0, 220)}
_WX_UNKNOWN    = (160, 160, 160)
_STATE_LINE  = (110, 130, 160)      # muted slate-blue: admin_1 boundaries
                                    # — visible over tint without competing
                                    # with airports / D2
_COUNTRY_LINE = (200, 180, 140)     # warm tan: admin_0 boundaries — distinct
                                    # from state lines so the two layers can
                                    # overlap (e.g. US states + Canada border)
                                    # and still read as separate features


# ── Airspace palette (outline RGB, fill RGBA-or-None) ───────────────────────
# Class B uses blue (matches FAA charting convention); C is magenta;
# D dashed blue (but pygame doesn't do dashed natively, so solid blue
# at thinner stroke); MOA amber; Restricted red.  Fill is low-alpha so
# the polygon shades without hiding terrain underneath.
_AIRSPACE_COLORS = {
    "B":   ((100, 140, 255), (100, 140, 255, 40)),
    "C":   ((220,  80, 220), (220,  80, 220, 40)),
    "D":   ((110, 170, 255), (110, 170, 255, 30)),
    "MOA": ((230, 170,  60), (230, 170,  60, 35)),
    "R":   ((230,  60,  60), (230,  60,  60, 50)),
    # Prohibited — deeper red than Restricted, more opaque fill so a
    # pilot doesn't have to read the ident to know it's a hard NO.
    "P":   ((255,  20,  60), (255,  20,  60, 80)),
    # TFRs — red-orange, distinct from R/P but in the same "do not
    # enter" colour family.  Stadium + Defense TFRs both render here;
    # the ident encodes which sub-type the pilot can read on the map.
    "TFR": ((255, 130,   0), (255, 130,   0, 70)),
}
_AIRSPACE_DEFAULT = ((200, 200, 200), (200, 200, 200, 30))


def _airspace_alt_label(asp):
    """Format ceiling/floor as a sectional-style "100/SFC" string —
    altitudes are in hundreds of feet, surface = "SFC", unlimited or
    missing ceiling = blank.  Returns "" when both are 0 / unknown so
    we don't dirty the map with empty fractions."""
    flr = asp.get("floor_ft", 0) or 0
    clg = asp.get("ceiling_ft", 0) or 0
    if flr == 0 and clg == 0:
        return ""
    def fmt(ft):
        if ft <= 0:
            return "SFC"
        # Round to hundreds.  Values like 4500 → "45"; 18000+ shown
        # verbatim because pilots read "180" as flight-level intuitively.
        return str(int(ft) // 100)
    top = fmt(clg) if clg > 0 else ""
    bot = fmt(flr)
    if not top:
        return bot
    return f"{top}/{bot}"


def _airspaces_query_nearby(airspaces, lat, lon, radius_nm):
    """Inline bbox cull — keeps the render path independent of the
    shared.airspaces import (which not every host carries — pi4 might
    use a different airspace data path later)."""
    if not airspaces:
        return []
    nm_per_deg = 60.0
    cos_lat = max(0.05, math.cos(math.radians(lat)))
    d_lat = radius_nm / nm_per_deg
    d_lon = radius_nm / nm_per_deg / cos_lat
    lat_lo = lat - d_lat; lat_hi = lat + d_lat
    lon_lo = lon - d_lon; lon_hi = lon + d_lon
    out = []
    for a in airspaces:
        bla_lo, bla_hi, blo_lo, blo_hi = a["bbox"]
        if (bla_hi < lat_lo or bla_lo > lat_hi
                or blo_hi < lon_lo or blo_lo > lon_hi):
            continue
        out.append(a)
    return out


# ── Hypsometric terrain tint cache ────────────────────────────────────────────
# Building the tint is the only expensive work on the inset.  At cruise the
# centre moves slowly, so quantising it lets one cached surface serve many
# frames.  The cache holds a few entries to absorb pan motion and zoom changes
# without thrashing.

_tint_cache: dict = {}
_TINT_CACHE_MAX = 4   # pi4 uses 6; pi_zero gets less RAM, so trim.
_TINT_N = 48          # elevation samples per side; smoothscaled up to fit.
                      # pi4 uses 64 — 48 cuts the build cost by ~45 %.

# Cached overlay surfaces keyed by (w, h) so render() doesn't allocate
# a fresh full-screen SRCALPHA each frame.  Only one entry in practice
# (the inset size doesn't change at runtime).
_veil_cache: dict = {}

# Cached airport ident labels keyed by (ident, font id).  The 33 pt
# font on pi_zero MFD makes each font.render call a real cost; the
# cache holds rendered labels so repeated frames reuse them.  Capped
# at 256 entries (LRU eviction) so a long cross-country flight doesn't
# grow the cache unbounded as the visible set shifts.
import collections as _collections_mm
_APT_LABEL_CACHE_MAX = 256
_apt_label_cache: "_collections_mm.OrderedDict" = _collections_mm.OrderedDict()


def _quantise_centre(lat, lon, range_nm):
    """Snap the centre to ~25% of the visible range so light pan motion
    re-uses the same cached surface.  Was 10% — too fine at sim speeds
    where the aircraft crossed cell boundaries faster than the async
    tint worker could finish, leaving "BUILDING…" flashing constantly."""
    step_deg = max(0.002, (range_nm / _NM_PER_DEG_LAT) * 0.25)
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


# Wide-zoom tints (80 nm and above) sample ~64 SRTM tiles, which evicts the
# 32-entry SRTM cache and forces ~30 s of disk I/O on the render thread when
# the user steps zoom out. Mirror the SVT outer-mesh strategy: numpy work
# happens on a worker thread, pygame surface finalisation stays on the main
# thread (pygame's surface APIs are not thread-safe), and the renderer
# paints around a None tint while the build is in flight.
#
# pi_zero values are tighter than pi4's because the Pi Zero 2W only has
# 512 MB RAM and a much slower SD card: keeping sync builds at <= 5 nm
# (tiny bbox, 1-2 tiles) means the main thread stalls only on the rare
# very-zoomed-in render.  Wider zooms go async with a "BUILDING…" hint.
_TINT_SYNC_MAX_NM = 5            # range cap for synchronous builds
_TINT_RENDER_MAX_NM = 40         # range cap for rendering the tint at all.
                                 # With terrain.set_resolution_preference
                                 # ("srtm3") forced on at startup, the
                                 # SRTM tile bbox no longer pulls 52 MB
                                 # SRTM1 files into RAM — even at 40 nm
                                 # zoom the in-memory tile data stays
                                 # bounded (~25 MB for 4 SRTM3 tiles).
                                 # Vector layers still render past 40 nm
                                 # if the user steps zoom up; the tint
                                 # just stops appearing there.
_tint_async_lock = threading.Lock()
_tint_pending: set = set()       # keys currently being built on a worker
_tint_ready:   dict = {}         # key -> (rgb uint8, elevs float32)
_TINT_READY_MAX = 6              # cap above ensures stale results don't pile up

# Below this groundspeed, GPS track is noisy / arbitrary — the map
# rotation falls back to magnetic heading so the inset doesn't jitter
# or jump while taxiing or holding short.  Matches HDG_TRK_MIN_KT in
# pi_zero/pi4 pfd.py.
_TRK_MIN_KT = 3.0


def _build_tint_pixels(srtm_dir, water_dir, c_lat, c_lon, range_nm, oversize):
    """Numpy-only pixel builder for the hypsometric tint. Returns
    (rgb (n, n, 3) uint8, elevs (n, n) float32) — north-up — or
    (None, None) when numpy isn't available. Safe to call from a
    background worker because it never touches a pygame surface."""
    if not HAS_NUMPY:
        return None, None
    n = _TINT_N
    span_nm = 2.0 * range_nm * oversize
    span_lat = span_nm / _NM_PER_DEG_LAT
    cos_lat = max(0.05, math.cos(math.radians(c_lat)))
    span_lon = span_lat / cos_lat

    lat_top = c_lat + span_lat * 0.5
    lat_bot = c_lat - span_lat * 0.5
    lon_lf  = c_lon - span_lon * 0.5
    lon_rt  = c_lon + span_lon * 0.5

    rows_lat = np.linspace(lat_top, lat_bot, n, dtype=np.float64)
    cols_lon = np.linspace(lon_lf,  lon_rt,  n, dtype=np.float64)
    sample_lat = np.broadcast_to(rows_lat[:, None], (n, n))
    sample_lon = np.broadcast_to(cols_lon[None, :], (n, n))

    elevs = np.zeros((n, n), dtype=np.float32)
    water = np.zeros((n, n), dtype=bool)

    lat_int = np.floor(sample_lat).astype(np.int32)
    lon_int = np.floor(sample_lon).astype(np.int32)
    enc = ((lat_int.astype(np.int64) + 90) * 1000 +
           (lon_int.astype(np.int64) + 360))

    for tile_key in np.unique(enc):
        tla = int(tile_key) // 1000 - 90
        tlo = int(tile_key) %  1000 - 360
        mask = (lat_int == tla) & (lon_int == tlo)
        if not mask.any():
            continue

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

    rgb_r = np.interp(elevs, _PAL_X, _PAL_R).astype(np.uint8)
    rgb_g = np.interp(elevs, _PAL_X, _PAL_G).astype(np.uint8)
    rgb_b = np.interp(elevs, _PAL_X, _PAL_B).astype(np.uint8)
    rgb = np.stack([rgb_r, rgb_g, rgb_b], axis=-1)
    if water.any():
        rgb[water] = _WATER_TINT_RGB
    return rgb, elevs


def _finalize_tint_surface(rgb, target_px):
    """Main-thread pygame finalize: rgb (n, n, 3) uint8 → smoothscaled
    Surface at target_px. Called from _tint_get when picking up a
    background-built result."""
    if rgb is None:
        surf = pygame.Surface((target_px, target_px))
        surf.fill(_BG)
        return surf
    tile = pygame.surfarray.make_surface(rgb.swapaxes(0, 1))
    return pygame.transform.smoothscale(tile, (target_px, target_px))


def _build_tint(srtm_dir, water_dir, c_lat, c_lon, range_nm, size_px, oversize):
    """Synchronous build path — used for close-range tints where the I/O
    cost is bounded by the SRTM tile cache. Wide ranges go through the
    async path in _tint_get instead."""
    target_px = max(8, int(size_px * oversize))
    if not HAS_NUMPY:
        n = _TINT_N
        tile = pygame.Surface((n, n))
        tile.fill(_BG)
        return pygame.transform.smoothscale(tile, (target_px, target_px)), None
    rgb, elevs = _build_tint_pixels(srtm_dir, water_dir,
                                    c_lat, c_lon, range_nm, oversize)
    return _finalize_tint_surface(rgb, target_px), elevs


def _tint_async_worker(srtm_dir, water_dir, c_lat, c_lon,
                       range_nm, oversize, key):
    """Worker thread: do the heavy numpy work, post the result for the
    main thread to convert into a pygame surface on the next render."""
    try:
        rgb, elevs = _build_tint_pixels(srtm_dir, water_dir,
                                        c_lat, c_lon, range_nm, oversize)
        with _tint_async_lock:
            _tint_ready[key] = (rgb, elevs)
            # Cap _tint_ready: when the aircraft moves faster than
            # builds finish (e.g. sim cruising), completed results for
            # stale keys pile up and never get picked up by the main
            # thread.  Prune oldest entries above the cap so memory
            # stays bounded.
            while len(_tint_ready) > _TINT_READY_MAX:
                _tint_ready.pop(next(iter(_tint_ready)))
    except Exception as e:
        print(f"[moving_map] async tint build failed: {e}")
    finally:
        with _tint_async_lock:
            _tint_pending.discard(key)


# SVT clearance bands.  Mirror the palette in svt_renderer.py so a pixel
# painted red on the AI shows up red on the inset and vice versa.
_ALERT_RED_FT    = 0     # clearance < 0 ft  → terrain at/above aircraft
_ALERT_ORANGE_FT = 100   # clearance < 100 ft → SVT "warning" band
_ALERT_AMBER_FT  = 500   # clearance < 500 ft → SVT "caution" band
_ALERT_RGBA = {
    "red":    (220,  30,  30, 220),
    "orange": (220,  80,   0, 200),
    "amber":  (200, 130,   0, 170),
}


def _build_alert_overlay(elev_grid, alt_ft, target_px):
    """RGBA overlay painting clearance < 500 ft pixels in SVT colours.

    Reuses the cached north-up (n × n) elevation grid; the comparison
    against current alt_ft happens every frame so the overlay tracks
    altitude even while the underlying hypsometric tint is cache-hot."""
    if not HAS_NUMPY or elev_grid is None:
        return None
    n = elev_grid.shape[0]
    clearance = alt_ft - elev_grid

    rgba = np.zeros((n, n, 4), dtype=np.uint8)
    m_red    = clearance < _ALERT_RED_FT
    m_orange = (~m_red) & (clearance < _ALERT_ORANGE_FT)
    m_amber  = (clearance >= _ALERT_ORANGE_FT) & (clearance < _ALERT_AMBER_FT)
    rgba[m_red]    = _ALERT_RGBA["red"]
    rgba[m_orange] = _ALERT_RGBA["orange"]
    rgba[m_amber]  = _ALERT_RGBA["amber"]

    if not (m_red.any() or m_orange.any() or m_amber.any()):
        return None

    tile = pygame.image.frombuffer(rgba.tobytes(), (n, n), 'RGBA')
    return pygame.transform.smoothscale(tile, (target_px, target_px))


def _tint_get(srtm_dir, water_dir, c_lat, c_lon, range_nm, size_px, oversize):
    if not srtm_dir:
        return None, None
    q_lat, q_lon = _quantise_centre(c_lat, c_lon, range_nm)
    # water_dir is part of the cache key so toggling water tiles on/off
    # invalidates stale tints.  Empty string == no water sampling.
    key = (round(q_lat, 4), round(q_lon, 4),
           float(range_nm), int(size_px), round(oversize, 2),
           bool(water_dir))
    if key in _tint_cache:
        entry = _tint_cache.pop(key)
        _tint_cache[key] = entry
        return entry

    target_px = max(8, int(size_px * oversize))

    # If a background worker has finished a build for this key, finalize
    # it to a pygame surface here on the main thread and cache the result.
    with _tint_async_lock:
        ready = _tint_ready.pop(key, None)
    if ready is not None:
        rgb, elevs = ready
        entry = (_finalize_tint_surface(rgb, target_px), elevs)
        _tint_cache[key] = entry
        while len(_tint_cache) > _TINT_CACHE_MAX:
            _tint_cache.pop(next(iter(_tint_cache)))
        return entry

    # Close ranges: sync build (fits in the SRTM tile cache, fast enough
    # that the render thread won't notice).
    if range_nm <= _TINT_SYNC_MAX_NM:
        entry = _build_tint(srtm_dir, water_dir, q_lat, q_lon,
                            range_nm, size_px, oversize)
        _tint_cache[key] = entry
        while len(_tint_cache) > _TINT_CACHE_MAX:
            _tint_cache.pop(next(iter(_tint_cache)))
        return entry

    # Wide ranges: hand off to a worker so the render thread stays
    # responsive. Renderer paints a no-tint inset until the result
    # comes back on a later frame.
    with _tint_async_lock:
        in_flight = key in _tint_pending
        if not in_flight:
            _tint_pending.add(key)
    if not in_flight:
        threading.Thread(
            target=_tint_async_worker,
            args=(srtm_dir, water_dir, q_lat, q_lon,
                  range_nm, oversize, key),
            daemon=True, name="MapTintBuild").start()
    # Async build in flight — instead of returning (None, None) and
    # flashing "BUILDING…", reuse the most recent cached tint at the
    # same range / size / oversize / water-mode.  Visually the user
    # sees the previous tint centered slightly off the aircraft for
    # the duration of the build (≤2.5 nm offset at 25 % quantisation);
    # the new tint slides in seamlessly once the worker finishes.
    # range_nm and size/oversize/water_dir must match — different
    # zoom levels render at different target_px so they're not
    # interchangeable as fallback surfaces.
    for cached_key in reversed(_tint_cache):
        if (cached_key[2] == key[2]   # range_nm
                and cached_key[3] == key[3]   # size_px
                and cached_key[4] == key[4]   # oversize
                and cached_key[5] == key[5]): # water_dir bool
            return _tint_cache[cached_key]
    return None, None


def _draw_polylines(surf, lines, range_nm, lat, lon, cos_lat,
                    cx, cy, px_per_nm, sin_r, cos_r, color):
    """Draw a Natural Earth polyline cache (admin_0 or admin_1) inside
    the inset bbox.

    Two-stage filtering:
      (1) vectorised bbox-vs-window AABB cull on every polyline's stored
          bbox, rejects ~99 % of the world's polylines in microseconds.
      (2) full numpy projection of each surviving ring's (lon, lat)
          vertex array to screen (x, y) in one vector pass per ring —
          no Python per-vertex function call overhead.  Replaces the
          original `[project_fn(la, lo) for lo, la in ring]` list
          comprehension that dominated frame cost at wide zooms where
          admin_1 polylines have hundreds of vertices each.
    """
    if lines is None or not HAS_NUMPY:
        return

    seg_starts = lines["seg_starts"]
    seg_bboxes = lines["seg_bboxes"]   # (M, 4) lon_min, lat_min, lon_max, lat_max
    points     = lines["points"]       # (N, 2) lon, lat
    if len(seg_starts) <= 1:
        return

    # Visible lat/lon window — generous margin so a polyline that just
    # clips the corner of the inset still draws cleanly.
    d_lat = range_nm * 1.6 / _NM_PER_DEG_LAT
    d_lon = range_nm * 1.6 / (_NM_PER_DEG_LAT * cos_lat)
    lat_min, lat_max = lat - d_lat, lat + d_lat
    lon_min, lon_max = lon - d_lon, lon + d_lon

    # Vectorised AABB test: rejects ~99 % of the world's polylines.
    overlaps = ((seg_bboxes[:, 0] <= lon_max)
                & (seg_bboxes[:, 2] >= lon_min)
                & (seg_bboxes[:, 1] <= lat_max)
                & (seg_bboxes[:, 3] >= lat_min))
    visible_idx = np.flatnonzero(overlaps)
    if visible_idx.size == 0:
        return

    # Per-frame projection constants — fold the lat/lon → nm conversion
    # and the nm → pixel scaling together so the per-ring numpy pass is
    # just a few vector ops on N points.  Rotation is always applied;
    # when rot_deg == 0 the (sin_r, cos_r) = (0, 1) values reduce it to
    # the identity, no branch needed.
    lon_scale = _NM_PER_DEG_LAT * cos_lat * px_per_nm
    lat_scale = _NM_PER_DEG_LAT * px_per_nm
    sx, sy, sw, sh = surf.get_clip()
    sx_max = sx + sw
    sy_max = sy + sh

    for idx in visible_idx:
        s = int(seg_starts[idx])
        e = int(seg_starts[idx + 1])
        if e - s < 2:
            continue
        ring = points[s:e]
        # Vectorised projection: lat/lon (N×2 float32) → screen x,y in a
        # single numpy pass.  Same math as the closure `_project()` used
        # by every other vector layer, just whole-array.
        e_px = (ring[:, 0] - lon) * lon_scale
        n_px = (ring[:, 1] - lat) * lat_scale
        xs = cx + e_px * cos_r - n_px * sin_r
        ys = cy - (e_px * sin_r + n_px * cos_r)
        # Screen-bbox reject — also vectorised.
        if xs.max() < sx or xs.min() > sx_max:
            continue
        if ys.max() < sy or ys.min() > sy_max:
            continue
        # pygame.draw.lines wants a sequence of 2-element sequences;
        # column_stack + tolist gives a Python list of [x, y] pairs
        # without a per-vertex Python loop.
        pts = np.column_stack([xs, ys]).astype(np.int32).tolist()
        pygame.draw.lines(surf, color, False, pts, 1)


# ── NEXRAD reflectivity raster ──────────────────────────────────────────────
_NEXRAD_ALPHA = 150          # overlay opacity (0-255) — see through to terrain
# Cache of the scaled+dimmed (north-up) surface so the per-frame cost is a
# single blit; rebuilt only when the image (seq) or destination size (zoom)
# changes.  A second cache holds the rotated surface for track-up, keyed by
# rounded heading so steady flight reuses it (only turns regenerate).
_nexrad_scaled = {"seq": None, "w": 0, "h": 0, "surf": None}
_nexrad_rot    = {"key": None, "surf": None}


def _draw_nexrad(surf, nexrad, project, px_per_nm, cos_lat, rot_deg):
    """Blit the NEXRAD image geo-locked to its lat/lon bbox: positioned via
    the same `project` as every other layer (so it pans AND rotates with the
    map) and rotated to match in track-up.  The scaled+dimmed surface and the
    rotated surface are both cached, so the per-frame cost is one blit.
    ``nexrad`` is (pygame_surface, (w,s,e,n) bbox, seq)."""
    surface, bbox, seq = nexrad
    if surface is None or bbox is None:
        return
    w, s, e, n = bbox
    # Image pixel size = the bbox's unrotated screen extent at this zoom.
    dest_w = int(round((e - w) * _NM_PER_DEG_LAT * cos_lat * px_per_nm))
    dest_h = int(round((n - s) * _NM_PER_DEG_LAT * px_per_nm))
    if dest_w < 2 or dest_h < 2 or dest_w > 4000 or dest_h > 4000:
        return
    c = _nexrad_scaled
    if c["seq"] != seq or c["w"] != dest_w or c["h"] != dest_h:
        scaled = pygame.transform.smoothscale(surface, (dest_w, dest_h))
        # Dim by multiplying the per-pixel alpha (set_alpha is ignored on
        # per-pixel-alpha surfaces), keeping the transparent no-echo areas.
        scaled.fill((255, 255, 255, _NEXRAD_ALPHA),
                    special_flags=pygame.BLEND_RGBA_MULT)
        c.update(seq=seq, w=dest_w, h=dest_h, surf=scaled)
    base = c["surf"]
    if rot_deg:
        rk = (seq, dest_w, dest_h, round(rot_deg))
        rc = _nexrad_rot
        if rc["key"] != rk:
            rc["key"] = rk
            rc["surf"] = pygame.transform.rotate(base, rot_deg)
        img = rc["surf"]
    else:
        img = base
    # Centre on the bbox centre run through the map projection — this pans
    # with the map (pan moves the centre) and rotates with it (track-up),
    # so the radar stays pinned to the ground instead of the screen centre.
    cxp, cyp = project((s + n) / 2.0, (w + e) / 2.0)
    rect = img.get_rect(center=(int(round(cxp)), int(round(cyp))))
    surf.blit(img, rect.topleft)


# ── Weather (METAR) layer ───────────────────────────────────────────────────
def _draw_metars(surf, metars, project, rect):
    """Draw METAR stations as flight-category-coloured dots (green/blue/red/
    magenta).  Most stations sit on airports, so this effectively colours the
    field by current conditions.  ``metars`` are station dicts from
    shared/wx.parse_metars."""
    x, y, w, h = rect
    for m in metars:
        la, lo = m.get("lat"), m.get("lon")
        if la is None or lo is None:
            continue
        sx, sy = project(la, lo)
        if not (x - 6 <= sx <= x + w + 6 and y - 6 <= sy <= y + h + 6):
            continue
        col = _WX_CAT_COLORS.get(m.get("fltcat"), _WX_UNKNOWN)
        ix, iy = int(sx), int(sy)
        pygame.draw.circle(surf, (5, 5, 5), (ix, iy), 10)     # dark halo
        pygame.draw.circle(surf, col, (ix, iy), 8)
        pygame.draw.circle(surf, (5, 5, 5), (ix, iy), 8, 1)   # crisp edge


# ── ADS-B traffic layer ─────────────────────────────────────────────────────
_TFC_COLORS = {"alert": _TFC_ALERT, "proximate": _TFC_PROXIMATE,
               "other": _TFC_OTHER}


def _draw_traffic(surf, traffic, project, rot_deg, px_per_nm, font,
                  rect):
    """Draw ADS-B traffic diamonds with a heading leader and relative-
    altitude data tag.

    ``traffic`` is a list of relativised target dicts (see adsb.relative /
    threat_level) each carrying lat/lon, optional rel_alt_ft, vvel_fpm,
    track_deg and a "threat" class.  Filled diamond = proximate/alert,
    hollow = other.  The tag reads relative altitude in hundreds of feet
    with a trend arrow (↑ climb, ↓ descent)."""
    x, y, w, h = rect
    for t in traffic:
        tlat, tlon = t.get("lat"), t.get("lon")
        if tlat is None or tlon is None:
            continue
        sx, sy = project(tlat, tlon)
        if not (x - 12 <= sx <= x + w + 12 and y - 12 <= sy <= y + h + 12):
            continue
        ix, iy = int(sx), int(sy)
        threat = t.get("threat", "other")
        col = _TFC_COLORS.get(threat, _TFC_OTHER)

        # Heading leader — target track rotated into the (possibly track-up)
        # map frame.  Short fixed length; just communicates direction.
        trk = t.get("track_deg")
        if trk is not None:
            a = math.radians((trk - rot_deg) % 360.0)
            lx = ix + 12 * math.sin(a)
            ly = iy - 12 * math.cos(a)
            pygame.draw.line(surf, col, (ix, iy), (int(lx), int(ly)), 1)

        # Diamond.  Filled for proximate/alert so they pop against terrain.
        d = 5
        pts = [(ix, iy - d), (ix + d, iy), (ix, iy + d), (ix - d, iy)]
        if threat in ("alert", "proximate"):
            pygame.draw.polygon(surf, col, pts)
        else:
            pygame.draw.polygon(surf, col, pts, 1)

        # Data tag: relative altitude (hundreds of ft) + trend arrow.
        if font is not None:
            ra = t.get("rel_alt_ft")
            if ra is not None:
                hundreds = int(round(ra / 100.0))
                sign = "+" if hundreds >= 0 else "−"
                tag = f"{sign}{abs(hundreds):02d}"
            else:
                tag = "?"
            vv = t.get("vvel_fpm")
            if vv is not None and abs(vv) >= 200:
                tag += "↑" if vv > 0 else "↓"
            lbl = font.render(tag, True, col)
            surf.blit(lbl, (ix + d + 2, iy - d - 1))


# ── Public API ────────────────────────────────────────────────────────────────

def _rot_deg_for(orient, hdg_deg, track_deg):
    """Resolve map rotation in degrees CCW (in the math frame where +n is up).
    Track-up uses track when valid, falls back to heading; north-up is 0."""
    if orient != "trk":
        return 0.0
    if track_deg is None or track_deg == 0.0:
        return float(hdg_deg or 0.0)
    return float(track_deg)


def make_projector(rect, lat, lon, orient, range_nm, hdg_deg, track_deg):
    """Return (project, unproject) closures bound to the given map params.

    project(la, lo) → (sx, sy) screen coordinates inside ``rect``.
    unproject(sx, sy) → (la, lo) world coordinates.

    Used by callers that need to hit-test features (e.g. tap-an-airport)
    or apply pan-by-drag deltas in world coordinates.  Shares the same
    rotation + small-angle equirectangular math as ``render()``."""
    x, y, w, h = rect
    rot_deg = _rot_deg_for(orient, hdg_deg, track_deg)
    half_min = min(w, h) / 2
    px_per_nm = half_min / max(0.5, range_nm)
    cx, cy = x + w / 2.0, y + h / 2.0
    cos_lat = max(0.05, math.cos(math.radians(lat)))
    if rot_deg != 0.0:
        rr = math.radians(rot_deg)
        sin_r, cos_r = math.sin(rr), math.cos(rr)
    else:
        sin_r, cos_r = 0.0, 1.0

    def project(la, lo):
        n_nm = (la - lat) * _NM_PER_DEG_LAT
        e_nm = (lo - lon) * _NM_PER_DEG_LAT * cos_lat
        if rot_deg != 0.0:
            e2 = e_nm * cos_r - n_nm * sin_r
            n2 = e_nm * sin_r + n_nm * cos_r
            e_nm, n_nm = e2, n2
        return cx + e_nm * px_per_nm, cy - n_nm * px_per_nm

    def unproject(sx, sy):
        e_nm = (sx - cx) / px_per_nm
        n_nm = -(sy - cy) / px_per_nm
        if rot_deg != 0.0:
            e2 = e_nm * cos_r + n_nm * sin_r
            n2 = -e_nm * sin_r + n_nm * cos_r
            e_nm, n_nm = e2, n2
        return (lat + n_nm / _NM_PER_DEG_LAT,
                lon + e_nm / (_NM_PER_DEG_LAT * cos_lat))

    return project, unproject


def render(surf, rect, lat, lon, alt_ft, hdg_deg, track_deg, orient,
           range_nm, settings,
           airports_arr=None, runways_arr=None, obstacles_arr=None,
           srtm_dir="", water_dir="", direct_to=None, font=None,
           airport_types_visible=None, gs_kt=0.0, vso_kt=None,
           range_label=None, state_lines=None, country_lines=None,
           own_lat=None, own_lon=None, draw_corner_labels=True,
           fpl_remaining=None, airspaces=None, airspace_visible=None,
           traffic=None, metars=None, nexrad=None, fast=False):
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
    # Fall back to magnetic heading whenever GPS groundspeed is below
    # the track-valid threshold (~3 kt) — at low GS, GPS track is
    # noisy / stale / arbitrary and would make the map jitter or jump.
    # Using hdg in that case keeps the inset rotating with the nose so
    # the toggle is still visibly different from north-up before takeoff.
    if orient == "trk":
        if (track_deg is None or track_deg == 0.0
                or (gs_kt or 0.0) < _TRK_MIN_KT):
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

    # When a weather overlay (METAR dots / NEXRAD raster) is active, drop the
    # terrain tint + obstacle/tower symbols: they clutter the weather picture
    # and the tint build is the most expensive thing the inset does, so this
    # is also a real CPU saving while weather is up.
    wx_active = ((metars and settings.get("map_show_metar", False))
                 or (nexrad is not None
                     and settings.get("map_show_nexrad", False)))

    # ── Hypsometric terrain tint ─────────────────────────────────────────────
    # Water sampling is gated on the same map_show_water toggle the user
    # already has on the Display setup screen — off → cells render as
    # whatever PALETTE_ABSOLUTE puts at sea level (dark green), on →
    # ocean cells override with a water-blue tint.
    # Above _TINT_RENDER_MAX_NM the tint build pulls in ~64 SRTM1 tiles
    # (≈1.6 GB across reads). Even with the async worker the SD-card I/O
    # storm can OOM/swap a Pi 4 hard enough to lock the PFD up. Drop the
    # tint entirely at the widest zoom — state lines, the D2 line, and
    # the range ring still give whole-leg context.
    if (settings.get("map_show_terrain", True) and srtm_dir
            and range_nm <= _TINT_RENDER_MAX_NM and not wx_active
            and not fast):
        oversize = 1.0 if orient == "nrth" else 1.45
        _wd = water_dir if settings.get("map_show_water", True) else ""
        tint, elev_grid = _tint_get(srtm_dir, _wd, lat, lon, range_nm,
                                    max(w, h), oversize)
        if tint is None and range_nm > _TINT_SYNC_MAX_NM and font is not None:
            # Async build in flight at 80 nm — small breadcrumb at centre
            # so the pilot sees the inset is still alive while the
            # worker churns through SRTM tile loads.
            wait_surf = font.render("BUILDING…", True, _LABEL)
            surf.blit(wait_surf,
                      (int(cx) - wait_surf.get_width() // 2,
                       int(cy) - wait_surf.get_height() // 2))
        if tint is not None:
            if orient == "trk" and rot_deg != 0.0:
                tint_r = pygame.transform.rotate(tint, rot_deg)
            else:
                tint_r = tint
            tr = tint_r.get_rect(center=(int(cx), int(cy)))
            surf.blit(tint_r, tr)

            # SVT-style clearance overlay (red / orange / amber).  Inhibit
            # below Vso so taxi and rollout don't paint the inset red —
            # mirrors how the PFD's TAWS banner is gated.
            if vso_kt is not None and gs_kt >= vso_kt and elev_grid is not None:
                # Cap target_px the same way as the tint so the overlay
                # stays proportional and bilinear-stretches to the inset
                # at blit time.  Without this the overlay was a full
                # max(w, h)² surface (1.6 MB at 640²) and dominated the
                # memory budget at wider zooms.
                overlay = _build_alert_overlay(
                    elev_grid, alt_ft,
                    max(w, h))
                if overlay is not None:
                    if orient == "trk" and rot_deg != 0.0:
                        overlay_r = pygame.transform.rotate(overlay, rot_deg)
                    else:
                        overlay_r = overlay
                    o_rect = overlay_r.get_rect(center=(int(cx), int(cy)))
                    surf.blit(overlay_r, o_rect)

    # Slightly darker veil under vector layers so labels read cleanly.
    # Cache by (w, h) — on the pi_zero MFD this is a full-screen-sized
    # SRCALPHA surface (640×480 = ~1.2 MB), and re-allocating it every
    # frame at 12+ fps was a major source of SDL surface churn.
    veil = _veil_cache.get((w, h))
    if veil is None:
        veil = pygame.Surface((w, h), pygame.SRCALPHA)
        veil.fill((0, 0, 0, 60))
        _veil_cache[(w, h)] = veil
    surf.blit(veil, (x, y))

    # ── NEXRAD reflectivity (under symbols, over terrain) ──────────────────
    if (nexrad is not None and settings.get("map_show_nexrad", False)
            and not fast):
        _draw_nexrad(surf, nexrad, _project, px_per_nm, cos_lat, rot_deg)

    # ── State / province lines + country lines ─────────────────────────────
    # Only useful once the inset is showing whole-region context; at close
    # ranges they're indistinguishable noise and never within the visible
    # bbox.  Bbox-culled in lat/lon space so the per-frame cost stays
    # microseconds at any range.  Country lines paint after state lines so
    # international borders stack visibly on top of admin_1 polygons that
    # happen to share the perimeter (e.g. US/Canada).
    if (state_lines is not None
            and settings.get("map_show_state_lines", True)
            and range_nm >= 20):
        _draw_polylines(surf, state_lines, range_nm, lat, lon, cos_lat,
                        cx, cy, px_per_nm, sin_r, cos_r, _STATE_LINE)
    if (country_lines is not None
            and settings.get("map_show_country_lines", True)
            and range_nm >= 20):
        _draw_polylines(surf, country_lines, range_nm, lat, lon, cos_lat,
                        cx, cy, px_per_nm, sin_r, cos_r, _COUNTRY_LINE)

    # ── Airspaces (Class B/C/D + MOA + Restricted) ──────────────────────────
    # Drawn between context lines and the rest so airspaces sit UNDER
    # obstacles + airports + D2 (those are flight-critical and need to
    # read on top) but OVER state/country lines.  Polygon points are
    # projected the same way runways/airports are: cull by bbox first,
    # then convert each (lat,lon) → screen.  Open outline + a low-alpha
    # fill so the interior shades without obscuring terrain.
    #
    # Per-class display toggles ride in the same `settings` dict as the
    # other layer toggles; missing keys default to ON.  airspace_visible
    # is an additional CALLER filter (set of class strings) so the
    # PFD's setup screen can layer in its own visibility logic without
    # mutating settings.
    if (airspaces is not None
            and settings.get("map_show_airspaces", True)
            and range_nm <= 80 and not fast):
        nearby_as = _airspaces_query_nearby(airspaces, lat, lon,
                                             range_nm * 1.4)
        for asp in nearby_as:
            cls = asp["class"]
            if airspace_visible is not None and cls not in airspace_visible:
                continue
            if not settings.get(f"map_show_airspace_{cls.lower()}", True):
                continue
            col, fill = _AIRSPACE_COLORS.get(cls, (_AIRSPACE_DEFAULT, None))
            pts = [(int(px), int(py)) for px, py in
                   (_project(la, lo) for la, lo in asp["polygon"])]
            if len(pts) < 3:
                continue
            # Quick reject when the entire projected polygon falls off
            # screen.  Bbox cull above kept us close but the rotation
            # can still spin a polygon outside the inset rect.
            xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
            if (max(xs) < x or min(xs) > x + w
                    or max(ys) < y or min(ys) > y + h):
                continue
            if fill is not None:
                # Translucent fill via per-shape SRCALPHA surface — cheap
                # because the polygon count per frame is small.
                bx0 = max(x, min(xs)); by0 = max(y, min(ys))
                bx1 = min(x + w, max(xs)); by1 = min(y + h, max(ys))
                bw_  = max(1, bx1 - bx0); bh_  = max(1, by1 - by0)
                fs = pygame.Surface((bw_, bh_), pygame.SRCALPHA)
                shifted = [(p[0] - bx0, p[1] - by0) for p in pts]
                pygame.draw.polygon(fs, fill, shifted)
                surf.blit(fs, (bx0, by0))
            pygame.draw.polygon(surf, col, pts, 2)
            # Ident label near the polygon's centroid — drawn only when
            # the airspace covers enough screen area to read (otherwise
            # the label clutters at wide zooms).
            if font is not None:
                w_px = max(xs) - min(xs); h_px = max(ys) - min(ys)
                if w_px > 60 and h_px > 30:
                    cxp = sum(xs) // len(xs); cyp = sum(ys) // len(ys)
                    alt_str = _airspace_alt_label(asp)
                    key = (asp["ident"], alt_str, id(font), "asp")
                    cached = _apt_label_cache.get(key)
                    if cached is None:
                        id_surf = font.render(asp["ident"], True, col)
                        alt_surf = (font.render(alt_str, True, col)
                                    if alt_str else None)
                        cached = (id_surf, alt_surf)
                        _apt_label_cache[key] = cached
                        if len(_apt_label_cache) > _APT_LABEL_CACHE_MAX:
                            _apt_label_cache.popitem(last=False)
                    id_surf, alt_surf = cached
                    if alt_surf is not None:
                        gap = 1
                        total_h = id_surf.get_height() + gap + alt_surf.get_height()
                        y0 = cyp - total_h // 2
                        surf.blit(id_surf,
                                  (cxp - id_surf.get_width() // 2, y0))
                        surf.blit(alt_surf,
                                  (cxp - alt_surf.get_width() // 2,
                                   y0 + id_surf.get_height() + gap))
                    else:
                        surf.blit(id_surf,
                                  (cxp - id_surf.get_width() // 2,
                                   cyp - id_surf.get_height() // 2))

    # ── Runways ──────────────────────────────────────────────────────────────
    # Runway rectangles only carry useful detail at terminal-area zooms —
    # above 5 nm they collapse to single pixels and clutter the screen.
    if (_rwy_mod is not None
            and settings.get("map_show_runways", True) and runways_arr is not None
            and range_nm <= 5):
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
    # Above 10 nm range, obstacle dots turn into useless speckle (and
    # the pilot is too far away to care), so they're hidden regardless
    # of the master toggle.
    if (not fast and settings.get("map_show_obstacles", True)
            and obstacles_arr is not None
            and range_nm <= 10 and not wx_active):
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
    # Per-type filtering matches the main PFD; caller passes a set of
    # visible atype letters (S/M/L = public, H = helo, W = water, B = other).
    # On the pi_zero MFD, ``query_nearby`` can return 300+ airports in
    # dense areas at 20 nm, and the per-airport pygame.draw.circle calls
    # add up fast (~75 µs each × 300 = 22 ms just for dots) plus a font
    # render per ident at narrow zooms.  We bound the cost three ways:
    #
    #   1. Hard cap the rendered count at MAX_AIRPORTS_DRAWN.  Results
    #      are sorted nearest-first by query_nearby, so the cap throws
    #      away the farthest ones first — the pilot already can't read
    #      a label that's 25 nm away anyway.
    #   2. At wider zooms, narrow the visible set to "important" types
    #      only.  >10 nm drops "B"/"H"/"W"; >20 nm drops "S" too.
    #   3. Labels stay capped at <= 10 nm where they're actually legible.
    MAX_AIRPORTS_DRAWN = 40
    if (not fast and settings.get("map_show_airports", True)
            and airports_arr is not None and range_nm <= 40):
        if range_nm > 20:
            allowed_types = {"L"}
        elif range_nm > 10:
            allowed_types = {"M", "L"}
        elif range_nm > 5:
            allowed_types = {"S", "M", "L"}
        else:
            allowed_types = None    # all visible types
        # When a field is the active Direct-To, its magenta D2 label is drawn
        # below — skip the white airport-loop label for it so the two don't
        # stack into doubled text on the same dot.
        _d2_ident = (str(direct_to.get("ident", "")).strip().upper()
                     if direct_to and direct_to.get("ident") else "")
        nearby = _apt_mod.query_nearby(airports_arr, lat, lon,
                                       radius_nm=range_nm * 1.4)
        if HAS_NUMPY and hasattr(nearby, "dtype") and len(nearby) > 0:
            ids   = nearby["ident"]
            types = nearby["atype"]
            lats  = nearby["lat"]
            lons  = nearby["lon"]
            drawn = 0
            for i in range(len(nearby)):
                if drawn >= MAX_AIRPORTS_DRAWN:
                    break
                atype = str(types[i])
                if allowed_types is not None and atype not in allowed_types:
                    continue
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
                ident_str = str(ids[i])
                if (font is not None and range_nm <= 10
                        and ident_str.strip().upper() != _d2_ident):
                    # Cache rendered idents — at 33 pt, font.render on
                    # Pi Zero costs ~250 µs/call, and the same idents
                    # repeat every frame as long as the aircraft sits
                    # over the same area.
                    cache_key = (ident_str, id(font))
                    lbl = _apt_label_cache.get(cache_key)
                    if lbl is None:
                        lbl = font.render(ident_str, True, _APT_PUB)
                        _apt_label_cache[cache_key] = lbl
                        if len(_apt_label_cache) > _APT_LABEL_CACHE_MAX:
                            _apt_label_cache.popitem(last=False)
                    else:
                        _apt_label_cache.move_to_end(cache_key)
                    surf.blit(lbl, (ix + 5, iy - 7))
                drawn += 1

    # ── Direct-to / approach course line + waypoint diamond ─────────────────
    # Two distinct line shapes:
    #
    #   D2 (magenta):   from the activation point to the waypoint —
    #                   represents the chosen course, not a moving
    #                   bearing-to-waypoint arrow.  Same convention the
    #                   SVT direct-to trace uses.
    #
    #   APPROACH (cyan): from the runway threshold OUT along the final
    #                   approach course (reciprocal of the runway
    #                   heading) for the same final length as the HITS
    #                   boxes.  Mirrors the corridor the pilot sees in
    #                   3D so the inset and the SVT match.
    #
    # The diamond marker stays at the waypoint / threshold either way.
    if (settings.get("map_show_directto", True)
            and direct_to is not None and direct_to.get("ident")):
        approach_active = bool(direct_to.get("approach_active"))
        course_col = _HITS_CYAN if approach_active else _D2_MAGENTA
        wpx, wpy = _project(direct_to["lat"], direct_to["lon"])

        if approach_active:
            # Walk back from the threshold along the reciprocal of the
            # final approach course for `approach_final_nm` (default 5).
            course_deg = float(direct_to.get("approach_course_deg", 0.0))
            final_nm   = float(direct_to.get("approach_final_nm", 5.0))
            away_rad   = math.radians((course_deg + 180.0) % 360.0)
            t_lat = float(direct_to["lat"])
            t_lon = float(direct_to["lon"])
            cos_t = max(0.05, math.cos(math.radians(t_lat)))
            far_lat = t_lat + final_nm * math.cos(away_rad) / _NM_PER_DEG_LAT
            far_lon = t_lon + (final_nm * math.sin(away_rad)
                               / (_NM_PER_DEG_LAT * cos_t))
            fx, fy = _project(far_lat, far_lon)
            pygame.draw.line(surf, course_col,
                             (int(wpx), int(wpy)),
                             (int(fx),  int(fy)), 3)
        else:
            # Plain D2: polyline along the great-circle from activation
            # to waypoint.  Drawing two endpoints joined by a straight
            # line in equirectangular projection turns into a rhumb-ish
            # path that diverges from the actual GC by tens of nm on
            # transcontinental legs — the CDI (which uses spherical XTK)
            # and the SVT trace (which slerps the GC) then disagree
            # visibly with the inset.  Sample the GC and stitch with a
            # polyline so all three views match.  Fall back to the
            # waypoint itself if no act_lat/lon was set.
            ax_lat = float(direct_to.get("act_lat") or direct_to["lat"])
            ax_lon = float(direct_to.get("act_lon") or direct_to["lon"])
            n_seg = 20
            pts = []
            for i in range(n_seg + 1):
                f = i / n_seg
                la, lo = _gc_interp(ax_lat, ax_lon,
                                    direct_to["lat"], direct_to["lon"], f)
                px, py = _project(la, lo)
                pts.append((int(px), int(py)))
            pygame.draw.lines(surf, course_col, False, pts, 3)

        d = 5
        pygame.draw.polygon(surf, course_col,
                            [(int(wpx),     int(wpy) - d),
                             (int(wpx) + d, int(wpy)),
                             (int(wpx),     int(wpy) + d),
                             (int(wpx) - d, int(wpy))])

        # Multi-leg FPL polyline: every waypoint past the current one,
        # joined with a dimmer magenta line + small diamonds.  The
        # active leg itself is already drawn above by the D2 line; we
        # just extend the route forward.  Each leg is sampled along
        # the great circle for the same reason the D2 line is.  Line
        # width matches the D2 line (2 px) so the route reads as one
        # continuous course on the map.  Every waypoint gets a label,
        # regardless of zoom — at en-route ranges where airport labels
        # are gated out the FPL idents are still the pilot's primary
        # reference.
        if (fpl_remaining is not None
                and len(fpl_remaining) >= 2
                and not approach_active):
            faded = (140, 0, 140)   # dimmer magenta — clearly past-active
            from_la, from_lo, _from_ident = fpl_remaining[0]
            for next_la, next_lo, next_ident in fpl_remaining[1:]:
                n_seg = 16
                pts = []
                for i in range(n_seg + 1):
                    f = i / n_seg
                    la, lo = _gc_interp(from_la, from_lo,
                                         next_la, next_lo, f)
                    px, py = _project(la, lo)
                    pts.append((int(px), int(py)))
                pygame.draw.lines(surf, faded, False, pts, 3)
                npx, npy = _project(next_la, next_lo)
                pygame.draw.polygon(surf, faded,
                                    [(int(npx),     int(npy) - d),
                                     (int(npx) + d, int(npy)),
                                     (int(npx),     int(npy) + d),
                                     (int(npx) - d, int(npy))])
                if next_ident and font is not None:
                    key = (next_ident, id(font), "fpl")
                    lbl = _apt_label_cache.get(key)
                    if lbl is None:
                        lbl = font.render(next_ident, True, faded)
                        _apt_label_cache[key] = lbl
                        if len(_apt_label_cache) > _APT_LABEL_CACHE_MAX:
                            _apt_label_cache.popitem(last=False)
                    surf.blit(lbl, (int(npx) + d + 3, int(npy) - d - 2))
                from_la, from_lo = next_la, next_lo
        # Always-on D2 ident label next to the waypoint diamond.  The
        # airport-loop above caps labels at <= 10 nm and 40 nearest;
        # at wider zooms (or in dense areas where the airport got
        # decimated out) the diamond would otherwise be unlabelled.
        # Match the course / diamond colour so it reads as "the D2".
        d2_ident = str(direct_to.get("ident", ""))
        if d2_ident and font is not None:
            d2_lbl = _apt_label_cache.get((d2_ident, id(font), "d2"))
            if d2_lbl is None:
                d2_lbl = font.render(d2_ident, True, course_col)
                _apt_label_cache[(d2_ident, id(font), "d2")] = d2_lbl
                if len(_apt_label_cache) > _APT_LABEL_CACHE_MAX:
                    _apt_label_cache.popitem(last=False)
            surf.blit(d2_lbl, (int(wpx) + d + 3, int(wpy) - d - 2))

    # ── Weather (METAR station dots) ───────────────────────────────────────
    if metars and settings.get("map_show_metar", True) and not fast:
        _draw_metars(surf, metars, _project, rect)

    # ── ADS-B traffic ──────────────────────────────────────────────────────
    # Drawn above map features (incl. weather) but below the range ring +
    # own-ship chevron so the pilot's own symbol always stays on top.
    if traffic and settings.get("map_show_traffic", True):
        _draw_traffic(surf, traffic, _project, rot_deg, px_per_nm, font, rect)

    # ── Range ring ───────────────────────────────────────────────────────────
    # Shrink the ring 2 px inside the inset's shorter axis so the frame
    # border (drawn after clip release) and pygame's half-open clip rect
    # don't nibble the top and bottom scanlines of the outline.
    pygame.draw.circle(surf, _RING, (int(cx), int(cy)),
                       max(1, int(range_nm * px_per_nm) - 2), 1)

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
    # When the caller passes own_lat/own_lon (pan mode: map centred away
    # from the aircraft), project the aircraft to its actual screen pos
    # instead of pinning the chevron to the rect centre.
    if (own_lat is not None and own_lon is not None
            and (abs(own_lat - lat) > 1e-9 or abs(own_lon - lon) > 1e-9)):
        ox, oy = _project(own_lat, own_lon)
    else:
        ox, oy = cx, cy
    s = 7
    base_pts = [(0, -s), (s, s), (0, s * 0.4), (-s, s)]
    cr = math.cos(math.radians(own_rot))
    sr = math.sin(math.radians(own_rot))
    rotated = [(ox + p[0] * cr - p[1] * sr,
                oy + p[0] * sr + p[1] * cr) for p in base_pts]
    pygame.draw.polygon(surf, _OWNSHIP,
                        [(int(rx), int(ry)) for rx, ry in rotated])

    surf.set_clip(old_clip)

    # ── Frame + corner labels ────────────────────────────────────────────────
    pygame.draw.rect(surf, _FRAME, rect, width=1)

    if font is not None and draw_corner_labels:
        if range_label:
            # Caller supplied a custom prefix — used by AUTO mode to show
            # "AUTO 20 NM" instead of a bare distance.
            rng_lbl = f"{range_label} {range_nm:g} NM"
        else:
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
                elif hours < 99.0:
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
# Snap-points for the discrete zoom levels.  ZOOM_AUTO (== 0) is a sentinel:
# the inset picks the smallest standard step that fits the active direct-to
# (capped at 80 nm) and forces north-up.  AUTO sits at the end of the cycle
# so a left-tap on the largest standard level (80) flips to AUTO when a
# direct-to is active, and stays at 80 when one isn't.

ZOOM_AUTO   = 0
ZOOM_LEVELS = (1, 2, 5, 10, 20, 40, 80, 160)


def zoom_in(current_nm: int) -> int:
    """Step the range to the next-smaller snap point (zoom in)."""
    if current_nm == ZOOM_AUTO:
        return ZOOM_LEVELS[-1]
    levels = ZOOM_LEVELS
    for i, lvl in enumerate(levels):
        if current_nm <= lvl and i > 0:
            return levels[i - 1]
    return levels[0]


def zoom_out(current_nm: int, allow_auto: bool = False) -> int:
    """Step the range to the next-larger snap point (zoom out).
    Caller passes ``allow_auto=True`` when a direct-to is active so AUTO
    becomes reachable from the largest standard step."""
    if current_nm == ZOOM_AUTO:
        return ZOOM_AUTO
    levels = ZOOM_LEVELS
    for i, lvl in enumerate(levels):
        if current_nm < lvl:
            return lvl
    return ZOOM_AUTO if allow_auto else levels[-1]


def auto_fit_range(d_nm: float) -> int:
    """Pick the smallest standard zoom step that contains a given distance.
    Distance comes from current-position → direct-to-destination; the
    caller adds whatever margin it wants before invoking this. Caps at
    the largest standard step (currently 160 nm)."""
    for lvl in ZOOM_LEVELS:
        if d_nm <= lvl:
            return lvl
    return ZOOM_LEVELS[-1]
