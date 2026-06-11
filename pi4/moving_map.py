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
import runways as _rwy_mod     # noqa: E402
import obstacles as _obs_mod   # noqa: E402
import water as _water_mod     # noqa: E402
from terrain import load_tile  # noqa: E402
import terrain as _terrain_mod  # noqa: E402  (for disk_reads() diagnostics)

# Tint build diagnostics — OFF by default; set PFD_TINT_DEBUG=1 to log one
# line per async build (tile count, cold disk reads, elapsed ms) plus a KICK
# line (raw vs quantised centre, step) for diagnosing a persistent
# "BUILDING…".
_TINT_DEBUG = os.environ.get("PFD_TINT_DEBUG", "0") != "0"
_tint_build_seq = 0


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
# METAR flight-category colours — mirror shared/wx.FLIGHT_CAT_COLORS.
_WX_CAT_COLORS = {"VFR": (0, 200, 0), "MVFR": (40, 120, 255),
                  "IFR": (235, 40, 40), "LIFR": (220, 0, 220)}
_WX_UNKNOWN    = (160, 160, 160)

# Airspace outline + fill colours by class.  Mirrors piZ moving_map so
# a polygon reads the same on both inset views.  Fill is low-alpha
# so the shape shades without hiding terrain underneath.
_AIRSPACE_COLORS = {
    "B":   ((100, 140, 255), (100, 140, 255, 40)),
    "C":   ((220,  80, 220), (220,  80, 220, 40)),
    "D":   ((110, 170, 255), (110, 170, 255, 30)),
    "MOA": ((230, 170,  60), (230, 170,  60, 35)),
    "R":   ((230,  60,  60), (230,  60,  60, 50)),
    # Prohibited — heavier red than Restricted.  Pilots want clear
    # visual emphasis between "restricted when active" and "never".
    "P":   ((255,  20,  60), (255,  20,  60, 80)),
    # TFRs — red-orange, same family as R/P but distinguishable.
    "TFR": ((255, 130,   0), (255, 130,   0, 70)),
}
_AIRSPACE_DEFAULT = ((200, 200, 200), (200, 200, 200, 30))


def _airspace_alt_label(asp):
    """Format ceiling/floor as a sectional-style "100/SFC" string.
    Hundreds of feet, surface = "SFC".  Returns "" when both are
    missing so unlabeled polygons don't draw a meaningless fraction."""
    flr = asp.get("floor_ft", 0) or 0
    clg = asp.get("ceiling_ft", 0) or 0
    if flr == 0 and clg == 0:
        return ""
    def fmt(ft):
        if ft <= 0:
            return "SFC"
        return str(int(ft) // 100)
    top = fmt(clg) if clg > 0 else ""
    bot = fmt(flr)
    if not top:
        return bot
    return f"{top}/{bot}"


def _airspaces_query_nearby(airspaces, lat, lon, radius_nm):
    """Inline bbox cull — keeps render-side independent of any shared
    helper so the pi4 module stays self-contained."""
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
_STATE_LINE  = (110, 130, 160)      # muted slate-blue: admin_1 boundaries
                                    # — visible over tint without competing
                                    # with airports / D2
_COUNTRY_LINE = (200, 180, 140)     # warm tan: admin_0 boundaries — distinct
                                    # from state lines so the two layers can
                                    # overlap (e.g. US states + Canada border)
                                    # and still read as separate features


# ── Hypsometric terrain tint cache ────────────────────────────────────────────
# Building the tint is the only expensive work on the inset.  At cruise the
# centre moves slowly, so quantising it lets one cached surface serve many
# frames.  The cache holds a few entries to absorb pan motion and zoom changes
# without thrashing.

_tint_cache: dict = {}
_TINT_CACHE_MAX = 6
_TINT_N = 64       # elevation samples per side; smoothscaled up to fit

# Cached darkening veil keyed by (w, h).  On the big full-screen MFD this is a
# multi-MB SRCALPHA surface; re-allocating + filling it every frame (the veil
# is NOT skipped during a fast pan) was a major source of SDL surface churn —
# the main reason wide-zoom panning on the MET page felt heavier than piZ.
# One entry in practice (the map rect size is fixed at runtime).
_veil_cache: dict = {}

# Cached airport ident labels keyed by (ident, font id).  Each font.render is a
# real cost on the big MFD; caching lets repeated frames reuse rendered labels.
# Capped (LRU) so a long flight doesn't grow it unbounded as the visible set
# shifts.  (Airports only draw <=40 nm, but this keeps close-range pan smooth.)
import collections as _collections_mm
_APT_LABEL_CACHE_MAX = 256
_apt_label_cache: "_collections_mm.OrderedDict" = _collections_mm.OrderedDict()


def _quantise_centre(lat, lon, range_nm):
    """Snap the centre to ~10% of the visible range so light pan / flight
    motion re-uses the same cached surface.

    The longitude grid spacing must be derived from the *quantised*
    latitude, not the raw one.  `step_deg / cos(raw_lat)` drifts every
    frame as the aircraft's latitude creeps, which stretches the lon grid
    and shifts q_lon a hair each frame even when the cell index is
    unchanged — so the cache key never stabilises and the tint rebuilds
    every frame (a continuous "BUILDING…" flash at 80 nm; silent per-frame
    sync rebuilds at 40 nm).  cos(q_lat) is stable because q_lat is already
    snapped, so q_lon holds steady within a cell."""
    step_deg = max(0.002, (range_nm / _NM_PER_DEG_LAT) * 0.10)
    q_lat = round(lat / step_deg) * step_deg
    lon_step = step_deg / max(0.05, math.cos(math.radians(q_lat)))
    return (q_lat, round(lon / lon_step) * lon_step)


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
_TINT_SYNC_MAX_NM = 40           # range cap for synchronous builds
_TINT_RENDER_MAX_NM = 80         # range cap for rendering the tint at all
                                 # — above this, SRTM I/O is too heavy

# Below this groundspeed, GPS track is noisy / arbitrary — the inset
# rotation falls back to magnetic heading so it doesn't jitter or jump
# while taxiing or holding short.  Matches HDG_TRK_MIN_KT in pfd.py.
_TRK_MIN_KT = 3.0
_tint_async_lock = threading.Lock()
_tint_pending: set = set()       # keys currently being built on a worker
_tint_ready:   dict = {}         # key -> (rgb uint8, elevs float32)
_TINT_READY_MAX = 6              # cap so stale finished builds don't pile up


def _build_tint_pixels(srtm_dir, water_dir, c_lat, c_lon, range_nm, oversize):
    """Numpy-only pixel builder for the hypsometric tint. Returns
    (rgb (n, n, 3) uint8, elevs (n, n) float32, n_tiles int) — north-up —
    or (None, None, 0) when numpy isn't available. Safe to call from a
    background worker because it never touches a pygame surface."""
    if not HAS_NUMPY:
        return None, None, 0
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

        # The tint only samples a 48×48 grid, so SRTM3 (90 m) resolution is
        # indistinguishable from SRTM1 (30 m) here — request decimated tiles
        # (~2.8 MB vs ~26 MB) so the wide-zoom build isn't an I/O storm.  The
        # PFD SVT 3D scene keeps its own full-SRTM1 tiles (separate cache key).
        sres = load_tile(srtm_dir, tla, tlo, prefer="srtm3")
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
    return rgb, elevs, int(len(np.unique(enc)))


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
    rgb, elevs, _nt = _build_tint_pixels(srtm_dir, water_dir,
                                         c_lat, c_lon, range_nm, oversize)
    return _finalize_tint_surface(rgb, target_px), elevs


def _tint_async_worker(srtm_dir, water_dir, c_lat, c_lon,
                       range_nm, oversize, key):
    """Worker thread: do the heavy numpy work, post the result for the
    main thread to convert into a pygame surface on the next render."""
    try:
        import time as _t
        _t0 = _t.perf_counter()
        _dr0 = _terrain_mod.disk_reads() if _terrain_mod is not None else 0
        rgb, elevs, _ntiles = _build_tint_pixels(srtm_dir, water_dir,
                                                  c_lat, c_lon, range_nm, oversize)
        if _TINT_DEBUG:
            _dr1 = _terrain_mod.disk_reads() if _terrain_mod is not None else 0
            global _tint_build_seq
            _tint_build_seq += 1
            print(f"[tint] #{_tint_build_seq} R={range_nm:.0f} os={oversize:.2f} "
                  f"tiles={_ntiles} cold={_dr1 - _dr0} "
                  f"in={(_t.perf_counter() - _t0) * 1000:.0f}ms "
                  f"cache={len(_tint_cache)} pend={len(_tint_pending)}",
                  flush=True)
        with _tint_async_lock:
            _tint_ready[key] = (rgb, elevs)
            # Cap _tint_ready: when the aircraft moves faster than builds
            # finish (e.g. sim cruising across the country), completed results
            # for stale keys pile up and are never picked up by the main
            # thread.  Prune oldest above the cap so memory stays bounded.
            # (pi_zero already does this; the pi4 port had dropped it, which
            # leaked ~28 KB per stale tile over a long flight.)
            while len(_tint_ready) > _TINT_READY_MAX:
                _tint_ready.pop(next(iter(_tint_ready)))
    except Exception as e:
        import traceback
        print(f"[moving_map] async tint build FAILED: {e}", flush=True)
        traceback.print_exc()
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
    if not in_flight and _TINT_DEBUG:
        _step = max(0.002, (range_nm / _NM_PER_DEG_LAT) * 0.10)
        _cl = max(0.05, math.cos(math.radians(c_lat)))
        print(f"[tint] KICK c=({c_lat!r},{c_lon!r}) q=({q_lat!r},{q_lon!r}) "
              f"step={_step:.6f} lonstep={_step / _cl:.6f} "
              f"NM={_NM_PER_DEG_LAT!r} key={key}", flush=True)
    if not in_flight:
        threading.Thread(
            target=_tint_async_worker,
            args=(srtm_dir, water_dir, q_lat, q_lon,
                  range_nm, oversize, key),
            daemon=True, name="MapTintBuild").start()
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


# ── Cached border rasterisation ─────────────────────────────────────────────
# Rasterising the 10m admin_0/admin_1 polylines was the dominant MET-page cost
# on the Pi 4 (~27 ms): full quality, but redrawn from scratch every frame even
# while the view is parked.  Cache the rendered lines into an SRCALPHA surface
# keyed by a *fine* (≈3 px) centre quantum + range + rotation, so a stationary
# (or slowly-drifting-at-wide-zoom) map blits one surface instead of projecting
# thousands of vertices each frame.  No decimation → borders stay crisp.  The
# quantum is fine enough that the small positional offset is sub-pixel-ish and
# never visibly jumps; rebuilds happen only when the view actually moves a few
# px, and panning skips borders entirely (handled at the call site).
_border_cache = {"key": None, "surf": None, "blat": 0.0, "blon": 0.0}


def _draw_borders_cached(surf, rect, state_lines, country_lines, settings,
                         range_nm, lat, lon, cos_lat, cx, cy, px_per_nm,
                         sin_r, cos_r, rot_deg, fast):
    x, y, w, h = rect
    show_state = state_lines is not None and \
        settings.get("map_show_state_lines", True)
    show_ctry = country_lines is not None and \
        settings.get("map_show_country_lines", True)
    if not show_state and not show_ctry:
        return
    # Quantise the centre to ~3 px of motion so a parked map reuses the surface.
    qdeg = max(1e-6, 3.0 / max(0.01, px_per_nm) / _NM_PER_DEG_LAT)
    qlat = round(lat / qdeg)
    qlon = round(lon / (qdeg / cos_lat))
    key = (qlat, qlon, round(range_nm, 1), round(rot_deg / 2.0),
           show_state, show_ctry, w, h)
    c = _border_cache
    # Rebuild only when parked (not fast) and the view actually changed — or the
    # first time.  During a pan we keep the last raster and shift it (below) so
    # borders stay on screen and track the map without a ~27 ms re-projection
    # every frame.  They snap to a fresh raster on release.
    if c["surf"] is None or (not fast and c["key"] != key):
        bs = pygame.Surface((w, h), pygame.SRCALPHA)
        lcx, lcy = cx - x, cy - y       # centre is (w/2, h/2) inside bs
        if show_state:
            _draw_polylines(bs, state_lines, range_nm, lat, lon, cos_lat,
                            lcx, lcy, px_per_nm, sin_r, cos_r, _STATE_LINE)
        if show_ctry:
            _draw_polylines(bs, country_lines, range_nm, lat, lon, cos_lat,
                            lcx, lcy, px_per_nm, sin_r, cos_r, _COUNTRY_LINE)
        c.update(key=key, surf=bs, blat=lat, blon=lon)
    # Shift the cached raster by the screen-space centre movement since it was
    # built (zero when parked → exact; a few px while panning → tracks the map).
    e_nm = (c["blon"] - lon) * _NM_PER_DEG_LAT * cos_lat
    n_nm = (c["blat"] - lat) * _NM_PER_DEG_LAT
    dx = (e_nm * cos_r - n_nm * sin_r) * px_per_nm
    dy = -(e_nm * sin_r + n_nm * cos_r) * px_per_nm
    surf.blit(c["surf"], (x + dx, y + dy))


# ── NEXRAD reflectivity raster ──────────────────────────────────────────────
_NEXRAD_ALPHA = 150
_nexrad_scaled = {"seq": None, "w": 0, "h": 0, "surf": None}
_nexrad_rot    = {"key": None, "surf": None}


def _draw_nexrad(surf, nexrad, project, px_per_nm, cos_lat, rot_deg):
    """Blit NEXRAD geo-locked to its lat/lon bbox: positioned via the same
    `project` as every other layer (pans + rotates with the map) and rotated
    to match in track-up.  Scaled + rotated surfaces are cached.  Mirrors the
    piZ version."""
    surface, bbox, seq = nexrad
    if surface is None or bbox is None:
        return
    w, s, e, n = bbox
    dest_w = int(round((e - w) * _NM_PER_DEG_LAT * cos_lat * px_per_nm))
    dest_h = int(round((n - s) * _NM_PER_DEG_LAT * px_per_nm))
    if dest_w < 2 or dest_h < 2 or dest_w > 4000 or dest_h > 4000:
        return
    c = _nexrad_scaled
    if c["seq"] != seq or c["w"] != dest_w or c["h"] != dest_h:
        scaled = pygame.transform.smoothscale(surface, (dest_w, dest_h))
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
    cxp, cyp = project((s + n) / 2.0, (w + e) / 2.0)
    rect = img.get_rect(center=(int(round(cxp)), int(round(cyp))))
    surf.blit(img, rect.topleft)


# ── FIS-B NEXRAD cells (intensity raster from the radio) ─────────────────────
# Precip intensity 1-7 → the usual green/yellow/red/magenta reflectivity ramp.
_NEXRAD_CELL_COL = {
    1: (2, 200, 2), 2: (1, 140, 1), 3: (245, 240, 0), 4: (245, 165, 0),
    5: (230, 0, 0), 6: (175, 0, 0), 7: (255, 0, 255),
}
_NEXRAD_CELL_ALPHA = 135


def _draw_nexrad_cells(surf, cells, project, rect):
    """Shade FIS-B NEXRAD intensity cells.  Each cell is projected as a quad
    (its four lat/lon corners) so it stays correct under track-up rotation."""
    x, y, w, h = rect
    overlay = pygame.Surface((w, h), pygame.SRCALPHA)
    for c in cells:
        la, lo, dla, dlo = c["lat"], c["lon"], c["dlat"], c["dlon"]
        pts = [project(la, lo), project(la, lo + dlo),
               project(la - dla, lo + dlo), project(la - dla, lo)]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        if max(xs) < x or min(xs) > x + w or max(ys) < y or min(ys) > y + h:
            continue
        col = _NEXRAD_CELL_COL.get(c["i"], (255, 255, 255))
        pygame.draw.polygon(
            overlay, (col[0], col[1], col[2], _NEXRAD_CELL_ALPHA),
            [(int(p[0] - x), int(p[1] - y)) for p in pts])
    surf.blit(overlay, (x, y))


# ── Weather (METAR) layer ───────────────────────────────────────────────────
_METAR_MAX_DRAW = 160      # cap dots per frame; at wide zoom the rest are clutter


def _draw_metars(surf, metars, rect, lat, lon, cos_lat, cx, cy, px_per_nm,
                 sin_r, cos_r, max_draw=_METAR_MAX_DRAW):
    """Draw METAR stations as flight-category-coloured dots (green/blue/red/
    magenta).

    Vectorised: at wide zoom the WX poller pulls a ~250 nm radius (hundreds of
    stations), and the old per-station projection closure loop dominated the
    MET-page frame time on the Pi 4.  Project every station in one numpy pass,
    cull to the visible window, and cap the drawn count to the nearest-to-centre
    (far dots are unreadable clutter at wide zoom anyway)."""
    x, y, w, h = rect
    if not metars:
        return
    if not HAS_NUMPY:                       # always present on the Pi builds
        for m in metars:
            la, lo = m.get("lat"), m.get("lon")
            if la is None or lo is None:
                continue
            e_nm = (lo - lon) * _NM_PER_DEG_LAT * cos_lat
            n_nm = (la - lat) * _NM_PER_DEG_LAT
            sx = cx + (e_nm * cos_r - n_nm * sin_r) * px_per_nm
            sy = cy - (e_nm * sin_r + n_nm * cos_r) * px_per_nm
            if not (x - 6 <= sx <= x + w + 6 and y - 6 <= sy <= y + h + 6):
                continue
            _metar_dot(surf, int(sx), int(sy),
                       _WX_CAT_COLORS.get(m.get("fltcat"), _WX_UNKNOWN))
        return

    rows = [m for m in metars
            if m.get("lat") is not None and m.get("lon") is not None]
    if not rows:
        return
    las = np.array([m["lat"] for m in rows], dtype=np.float64)
    los = np.array([m["lon"] for m in rows], dtype=np.float64)
    e_nm = (los - lon) * (_NM_PER_DEG_LAT * cos_lat)
    n_nm = (las - lat) * _NM_PER_DEG_LAT
    if sin_r != 0.0 or cos_r != 1.0:        # track-up rotation
        ex = e_nm * cos_r - n_nm * sin_r
        ny = e_nm * sin_r + n_nm * cos_r
    else:
        ex, ny = e_nm, n_nm
    sx = cx + ex * px_per_nm
    sy = cy - ny * px_per_nm

    vis = ((sx >= x - 6) & (sx <= x + w + 6)
           & (sy >= y - 6) & (sy <= y + h + 6))
    idx = np.flatnonzero(vis)
    if idx.size > max_draw:                 # keep the nearest-to-centre dots
        d2 = (sx[idx] - cx) ** 2 + (sy[idx] - cy) ** 2
        idx = idx[np.argpartition(d2, max_draw)[:max_draw]]
    sxi = sx.astype(np.int32)
    syi = sy.astype(np.int32)
    for i in idx:
        _metar_dot(surf, int(sxi[i]), int(syi[i]),
                   _WX_CAT_COLORS.get(rows[i].get("fltcat"), _WX_UNKNOWN))


def _metar_dot(surf, ix, iy, col):
    """One flight-category dot: dark halo + coloured fill + crisp edge.
    Smaller than the piZ MFD dots — the inset packs airports/traffic in too."""
    pygame.draw.circle(surf, (5, 5, 5), (ix, iy), 8)
    pygame.draw.circle(surf, col, (ix, iy), 6)
    pygame.draw.circle(surf, (5, 5, 5), (ix, iy), 6, 1)


_GND_STATION = (90, 210, 230)    # FIS-B ground station: teal, distinct from
                                 # category dots / white airports / traffic

# Graphical-hazard area colours (G-AIRMET / SIGMET) by hazard type.
_HAZARD_COL = {
    "Turbulence":      (235, 175, 60),
    "Icing":           (120, 200, 235),
    "IFR":             (200, 180, 120),
    "Convective":      (235, 80, 80),
    "Mtn Obscuration": (185, 150, 110),
    "Ash":             (205, 120, 205),
    "Advisory":        (180, 180, 185),
}


def _draw_wx_graphics(surf, graphics, project, rect, font):
    """Shade FIS-B graphical hazard areas (polygons) with a translucent fill +
    coloured outline + a hazard label at the centroid.  Drawn under the station
    dots so the point weather stays legible."""
    x, y, w, h = rect
    overlay = pygame.Surface((w, h), pygame.SRCALPHA)
    labels = []
    for g in graphics:
        verts = g.get("vertices") or []
        if len(verts) < 2:
            continue
        col = _HAZARD_COL.get(g.get("hazard"), _HAZARD_COL["Advisory"])
        pts = [project(la, lo) for (la, lo) in verts]
        local = [(int(px - x), int(py - y)) for (px, py) in pts]
        if g.get("geom") == "polygon" and len(local) >= 3:
            pygame.draw.polygon(overlay, (col[0], col[1], col[2], 55), local)
            pygame.draw.polygon(overlay, (col[0], col[1], col[2], 220), local, 2)
        else:
            pygame.draw.lines(overlay, (col[0], col[1], col[2], 220), False,
                              local, 2)
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        labels.append((g.get("hazard", ""), col, int(cx), int(cy)))
    surf.blit(overlay, (x, y))
    if font is not None:
        for name, col, cx, cy in labels:
            img = font.render(name, True, col)
            surf.blit(img, (cx - img.get_width() // 2, cy - 7))


def _draw_ground_stations(surf, stations, project, font, rect, scale=1.0):
    """Draw the FIS-B ground station(s) currently being heard as an upward
    transmitter triangle + 'FISB <age>s' label.  This is both the "where's my
    weather coming from" cue and the live reception indicator (a symbol here
    means FIS-B is arriving over the radio)."""
    x, y, w, h = rect
    r = max(3, int(round(4 * scale)))
    for s in stations:
        la, lo = s.get("lat"), s.get("lon")
        if la is None or lo is None:
            continue
        sx, sy = project(la, lo)
        if not (x - 8 <= sx <= x + w + 8 and y - 8 <= sy <= y + h + 8):
            continue
        ix, iy = int(sx), int(sy)
        pts = [(ix, iy - r), (ix - r, iy + r), (ix + r, iy + r)]
        pygame.draw.polygon(surf, (5, 5, 5), pts)        # dark backing
        pygame.draw.polygon(surf, _GND_STATION, pts, 2)  # tower outline
        pygame.draw.line(surf, _GND_STATION, (ix, iy - r),
                         (ix, iy - r - r), 1)             # antenna mast
        if font is not None:
            age = s.get("age_s")
            lbl = "FISB" if age is None else f"FISB {int(age)}s"
            surf.blit(font.render(lbl, True, _GND_STATION),
                      (ix + r + 3, iy - 7))


# ── Winds-aloft barbs ────────────────────────────────────────────────────────
_WIND_BARB = (180, 220, 245)


# Cached text glyphs for the winds layer.  font.render() is ~0.5-1 ms each and
# the temp tag was re-rendered for every barb every frame, which dominated the
# inset/MFD frame time once a full grid was up.  Distinct temp strings are few,
# so a tiny keyed cache turns each tag back into a cheap blit.
_wx_glyph_cache = {}


def _wx_glyph(font, text, color):
    key = (id(font), text, color)
    g = _wx_glyph_cache.get(key)
    if g is None:
        if len(_wx_glyph_cache) > 512:
            _wx_glyph_cache.clear()
        g = font.render(text, True, color)
        _wx_glyph_cache[key] = g
    return g


def _draw_wind_barb(surf, cx, cy, from_dir, speed_kt, rot_deg, col, scale=1.0):
    """Standard meteorological wind barb: shaft toward the source direction,
    50 kt pennants / 10 kt full barbs / 5 kt half barbs."""
    a = math.radians((from_dir - rot_deg) % 360.0)
    sx, sy = math.sin(a), -math.cos(a)               # along shaft, toward source
    L = 30 * scale
    ex, ey = cx + sx * L, cy + sy * L
    pygame.draw.line(surf, col, (cx, cy), (int(ex), int(ey)), 2)
    pygame.draw.circle(surf, col, (int(cx), int(cy)), 3)
    if speed_kt is None or speed_kt < 3:
        return
    barb_ang = a + math.radians(120)                 # barbs lean back from source
    bx, by = math.sin(barb_ang), -math.cos(barb_ang)
    spd = int(round(speed_kt / 5.0)) * 5
    n50, spd = divmod(spd, 50)
    n10, spd = divmod(spd, 10)
    n5 = spd // 5
    step, full, half = 7 * scale, 12 * scale, 6 * scale
    pos = 0.0

    def shaft_pt(dist):                              # march back toward station
        return (ex - sx * dist, ey - sy * dist)

    for _ in range(n50):
        p0, p1 = shaft_pt(pos), shaft_pt(pos + step)
        pygame.draw.polygon(surf, col, [p0, p1,
                                        (p0[0] + bx * full, p0[1] + by * full)])
        pos += step
    for _ in range(n10):
        p0 = shaft_pt(pos)
        pygame.draw.line(surf, col, p0,
                         (p0[0] + bx * full, p0[1] + by * full), 2)
        pos += step
    for _ in range(n5):
        if pos == 0.0:
            pos = step                               # lone half barb sits inboard
        p0 = shaft_pt(pos)
        pygame.draw.line(surf, col, p0,
                         (p0[0] + bx * half, p0[1] + by * half), 2)
        pos += step


# Minimum on-screen spacing between barbs (px).  This thins only where the
# national grid would crowd a SMALL viewport at WIDE zoom (the PFD inset at
# 160 nm) — the big MFD and the closer inset zooms keep every barb because they
# already exceed this spacing.
_WINDS_MIN_BARB_PX = 70.0


def _winds_decimate(barbs, project, rect, range_nm):
    """Pick ~one barb per WORLD cell (quantised lat/lon) — NOT per screen cell —
    so the displayed set stays put as the map pans/rotates.  The old
    screen-anchored grid reshuffled which barb filled each cell every time the
    map moved (the "grid moves around when I pan" bug).  Cell size scales with
    the zoom AND a minimum on-screen spacing (so a small inset at wide zoom
    thins to stay readable while the big MFD keeps the full grid), and it also
    dedupes the duplicate points adjacent zone grids share on their seams.
    Returns [(b, sx, sy)] — the barb nearest each cell's world centre."""
    x, y, w, h = rect
    px_per_nm = (min(w, h) / 2.0) / max(1.0, range_nm)
    pixel_nm = _WINDS_MIN_BARB_PX / max(1e-6, px_per_nm)
    cell_deg = max(0.15, max(range_nm / 5.0, pixel_nm) / 60.0)
    best = {}
    for b in barbs:
        la, lo = b.get("lat"), b.get("lon")
        if la is None or lo is None:
            continue
        sx, sy = project(la, lo)
        if not (x - 36 <= sx <= x + w + 36 and y - 36 <= sy <= y + h + 36):
            continue
        ci, cj = round(la / cell_deg), round(lo / cell_deg)
        d2 = (la - ci * cell_deg) ** 2 + (lo - cj * cell_deg) ** 2
        cur = best.get((ci, cj))
        if cur is None or d2 < cur[0]:
            best[(ci, cj)] = (d2, b, sx, sy)
    return [(v[1], v[2], v[3]) for v in best.values()]


def _draw_winds_barbs(surf, barbs, project, rot_deg, rect, font, scale=1.0,
                      range_nm=80.0):
    """Draw winds-aloft barbs (with a temperature tag), decimated to ~one per
    world cell so the spread stays clean, cheap, and steady under pan."""
    for b, sx, sy in _winds_decimate(barbs, project, rect, range_nm):
        if b.get("lv"):
            pygame.draw.circle(surf, _WIND_BARB, (int(sx), int(sy)), 4, 1)
            if font is not None:
                surf.blit(_wx_glyph(font, "LV", _WIND_BARB),
                          (int(sx) + 6, int(sy) - 7))
        else:
            _draw_wind_barb(surf, sx, sy, b.get("dir", 0), b.get("spd", 0),
                            rot_deg, _WIND_BARB, scale)
        if font is not None and b.get("temp") is not None:
            surf.blit(_wx_glyph(font, f"{b['temp']:+d}°", (200, 210, 225)),
                      (int(sx) + 6, int(sy) + 5))


# ── ADS-B traffic layer ─────────────────────────────────────────────────────
_TFC_COLORS = {"alert": _TFC_ALERT, "proximate": _TFC_PROXIMATE,
               "other": _TFC_OTHER}


def _draw_traffic(surf, traffic, project, rot_deg, px_per_nm, font, rect):
    """Draw ADS-B traffic diamonds with a heading leader and relative-
    altitude data tag.  ``traffic`` is a list of relativised target dicts
    (see adsb.relative / threat_level).  Mirrors the piZ implementation so
    a target reads identically on both inset views."""
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

        trk = t.get("track_deg")
        if trk is not None:
            a = math.radians((trk - rot_deg) % 360.0)
            lx = ix + 12 * math.sin(a)
            ly = iy - 12 * math.cos(a)
            pygame.draw.line(surf, col, (ix, iy), (int(lx), int(ly)), 1)

        d = 5
        pts = [(ix, iy - d), (ix + d, iy), (ix, iy + d), (ix - d, iy)]
        if threat in ("alert", "proximate"):
            pygame.draw.polygon(surf, col, pts)
        else:
            pygame.draw.polygon(surf, col, pts, 1)

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
    """Map rotation in degrees (track-up uses track when valid, else heading;
    north-up is 0).  Callers pass track_deg=None when GPS track is unreliable."""
    if orient != "trk":
        return 0.0
    if track_deg is None or track_deg == 0.0:
        return float(hdg_deg or 0.0)
    return float(track_deg)


def make_projector(rect, lat, lon, orient, range_nm, hdg_deg, track_deg):
    """Return (project, unproject) closures matching render()'s projection,
    for hit-testing (airport/METAR taps) and pan drags."""
    x, y, w, h = rect
    rot_deg = _rot_deg_for(orient, hdg_deg, track_deg)
    px_per_nm = min(w, h) / 2.0 / max(0.5, range_nm)
    cx, cy = x + w / 2.0, y + h / 2.0
    cos_lat = max(0.05, math.cos(math.radians(lat)))
    rr = math.radians(rot_deg)
    sin_r, cos_r = math.sin(rr), math.cos(rr)

    def project(la, lo):
        n_nm = (la - lat) * _NM_PER_DEG_LAT
        e_nm = (lo - lon) * _NM_PER_DEG_LAT * cos_lat
        if rot_deg:
            e_nm, n_nm = (e_nm * cos_r - n_nm * sin_r,
                          e_nm * sin_r + n_nm * cos_r)
        return cx + e_nm * px_per_nm, cy - n_nm * px_per_nm

    def unproject(sx, sy):
        e_nm = (sx - cx) / px_per_nm
        n_nm = -(sy - cy) / px_per_nm
        if rot_deg:
            e_nm, n_nm = (e_nm * cos_r + n_nm * sin_r,
                          -e_nm * sin_r + n_nm * cos_r)
        return (lat + n_nm / _NM_PER_DEG_LAT,
                lon + e_nm / (_NM_PER_DEG_LAT * cos_lat))

    return project, unproject


def render(surf, rect, lat, lon, alt_ft, hdg_deg, track_deg, orient,
           range_nm, settings,
           airports_arr=None, runways_arr=None, obstacles_arr=None,
           srtm_dir="", water_dir="", direct_to=None, font=None,
           airport_types_visible=None, gs_kt=0.0, vso_kt=None,
           range_label=None, state_lines=None, country_lines=None,
           fpl_remaining=None, airspaces=None, airspace_visible=None,
           traffic=None, metars=None, nexrad=None,
           draw_corner_labels=True, own_lat=None, own_lon=None,
           symbol_scale=1.0, fast=False, ground_stations=None,
           wx_graphics=None, winds_barbs=None, nexrad_cells=None):
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
    # noisy / stale / arbitrary and would make the inset jitter or jump.
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

    # Weather overlay active → drop terrain tint + obstacle/tower symbols
    # (declutters the weather picture and skips the costly tint build).  The
    # winds page drops the tint whenever the WND overlay is selected — NOT only
    # when barbs are loaded — so an empty/loading cache doesn't flip the tint
    # build on and off frame to frame (the 80 nm async build flickering between
    # BUILDING… and terrain).  Its barbs read better over a plain background and
    # this also avoids the wide-zoom SRTM-tile storm that can OOM/lock a Pi 4.
    wx_active = ((metars and settings.get("map_show_metar", False))
                 or (nexrad is not None
                     and settings.get("map_show_nexrad", False))
                 or settings.get("map_show_winds", False))

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
    tint_drawn = False
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
            tint_drawn = True

            # SVT-style clearance overlay (red / orange / amber).  Inhibit
            # below Vso so taxi and rollout don't paint the inset red —
            # mirrors how the PFD's TAWS banner is gated.
            if vso_kt is not None and gs_kt >= vso_kt and elev_grid is not None:
                overlay = _build_alert_overlay(
                    elev_grid, alt_ft, max(w, h))
                if overlay is not None:
                    if orient == "trk" and rot_deg != 0.0:
                        overlay_r = pygame.transform.rotate(overlay, rot_deg)
                    else:
                        overlay_r = overlay
                    o_rect = overlay_r.get_rect(center=(int(cx), int(cy)))
                    surf.blit(overlay_r, o_rect)

    # Slightly darker veil under vector layers so labels read cleanly — but
    # only meaningful when the bright terrain tint is behind it.  Above the
    # tint range (160 nm), on the MET overlay, and during a fast pan the base
    # is plain black (_BG), so a 60-alpha black veil is a no-op — skip it and
    # save a full-screen alpha blit every frame.  Cached by (w, h) so when it
    # IS drawn it isn't re-allocated.
    if tint_drawn:
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
    # FIS-B (radio) NEXRAD cells on the same overlay.
    if (nexrad_cells and settings.get("map_show_nexrad", False)
            and not fast):
        _draw_nexrad_cells(surf, nexrad_cells, _project, rect)

    # ── State / province lines + country lines ─────────────────────────────
    # Only useful once the inset is showing whole-region context; at close
    # ranges they're indistinguishable noise and never within the visible
    # bbox.  Bbox-culled in lat/lon space so the per-frame cost stays
    # microseconds at any range.  Country lines paint after state lines so
    # international borders stack visibly on top of admin_1 polygons that
    # happen to share the perimeter (e.g. US/Canada).
    if range_nm >= 20:
        _draw_borders_cached(surf, rect, state_lines, country_lines, settings,
                             range_nm, lat, lon, cos_lat, cx, cy, px_per_nm,
                             sin_r, cos_r, rot_deg, fast)

    # ── Airspaces (Class B/C/D + MOA + Restricted) ──────────────────────────
    # Drawn between context lines and runways so airspaces sit UNDER
    # obstacles + airports + D2 (flight-critical, must read on top)
    # but OVER state/country lines.  Per-class display gates live in
    # the same `settings` dict as the other layer toggles.
    if (airspaces is not None
            and settings.get("map_show_airspaces", True)
            and range_nm <= 80 and not fast):
        nearby_as = _airspaces_query_nearby(airspaces, lat, lon,
                                             range_nm * 1.4)
        x_r, y_r, w_r, h_r = rect
        for asp in nearby_as:
            cls = asp["class"]
            if airspace_visible is not None and cls not in airspace_visible:
                continue
            if not settings.get(f"map_show_airspace_{cls.lower()}", True):
                continue
            col, fill = _AIRSPACE_COLORS.get(cls, _AIRSPACE_DEFAULT)
            pts = [(int(px), int(py)) for px, py in
                   (_project(la, lo) for la, lo in asp["polygon"])]
            if len(pts) < 3:
                continue
            xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
            if (max(xs) < x_r or min(xs) > x_r + w_r
                    or max(ys) < y_r or min(ys) > y_r + h_r):
                continue
            if fill is not None:
                bx0 = max(x_r, min(xs)); by0 = max(y_r, min(ys))
                bx1 = min(x_r + w_r, max(xs)); by1 = min(y_r + h_r, max(ys))
                bw_  = max(1, bx1 - bx0); bh_  = max(1, by1 - by0)
                fs = pygame.Surface((bw_, bh_), pygame.SRCALPHA)
                shifted = [(p[0] - bx0, p[1] - by0) for p in pts]
                pygame.draw.polygon(fs, fill, shifted)
                surf.blit(fs, (bx0, by0))
            pygame.draw.polygon(surf, col, pts, 2)
            if font is not None:
                w_px = max(xs) - min(xs); h_px = max(ys) - min(ys)
                if w_px > 60 and h_px > 30:
                    cxp = sum(xs) // len(xs); cyp = sum(ys) // len(ys)
                    id_surf = font.render(asp["ident"], True, col)
                    alt_str = _airspace_alt_label(asp)
                    if alt_str:
                        alt_surf = font.render(alt_str, True, col)
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
    if (settings.get("map_show_runways", True) and runways_arr is not None
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
    # Per-type filtering matches the main PFD: caller passes a set of
    # visible atype letters (S/M/L = public, H = helo, W = water, B = other).
    # Default ``None`` = show every type the master toggle covers.
    # Above 40 nm the airport dots smear into noise — the destination is
    # still marked by the D2 waypoint diamond drawn below, which is what
    # the pilot actually cares about at whole-leg scale.
    # When the MET (or NEXRAD) overlay is active the page becomes a dedicated
    # weather picture — drop the airport dots/labels so the flight-category
    # METAR dots aren't fighting the white airport symbols for the same pixels.
    # (Off the overlay, both draw: white airports + always-on METAR dots.)
    MAX_AIRPORTS_DRAWN = 60
    if (not fast and settings.get("map_show_airports", True)
            and not wx_active
            and airports_arr is not None and range_nm <= 40):
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

            def _r(v):    # scale dot radius for the big full-screen MFD
                return max(1, int(round(v * symbol_scale)))
            # query_nearby returns nearest-first, so the cap drops the farthest
            # fields — bounds the worst case in dense metros without thinning the
            # richer set the big screen is meant to show.  More generous than
            # piZ's 40 (more pixels to fill).
            drawn = 0
            for i in range(len(nearby)):
                if drawn >= MAX_AIRPORTS_DRAWN:
                    break
                atype = str(types[i])
                if (airport_types_visible is not None
                        and atype not in airport_types_visible):
                    continue
                ax2, ay2 = _project(float(lats[i]), float(lons[i]))
                ix, iy = int(ax2), int(ay2)
                if atype == "H":
                    pygame.draw.circle(surf, _APT_HELI, (ix, iy), _r(3))
                elif atype == "W":
                    pygame.draw.circle(surf, _APT_WATER, (ix, iy), _r(3), 1)
                elif atype == "B":
                    pygame.draw.circle(surf, _APT_OTHER, (ix, iy), _r(2))
                else:
                    pygame.draw.circle(surf, _APT_PUB, (ix, iy),
                                       _r(4) if atype in ("M", "L") else _r(3))
                # Dots show to 40 nm (the big MFD has the room).  Labels: all
                # types within 10 nm; only M/L out to 20 nm so the wider view
                # stays readable without hiding the dots themselves.  Rendered
                # idents are cached (font.render is a real cost repeated every
                # frame for the same fields while parked over an area).
                ident_str = str(ids[i])
                if (font is not None
                        and ident_str.strip().upper() != _d2_ident
                        and (range_nm <= 10
                             or (range_nm <= 20 and atype in ("M", "L")))):
                    cache_key = (ident_str, id(font))
                    lbl = _apt_label_cache.get(cache_key)
                    if lbl is None:
                        lbl = font.render(ident_str, True, _APT_PUB)
                        _apt_label_cache[cache_key] = lbl
                        if len(_apt_label_cache) > _APT_LABEL_CACHE_MAX:
                            _apt_label_cache.popitem(last=False)
                    else:
                        _apt_label_cache.move_to_end(cache_key)
                    surf.blit(lbl, (ix + _r(5) + 2, iy - 7))
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
                             (int(fx),  int(fy)), 2)
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
        # Always-on D2 ident label next to the waypoint diamond.  The
        # airport loop caps labels at <= 10 nm; at wider zooms the
        # diamond would otherwise be unlabelled.  Match the course /
        # diamond colour so it reads as "the D2".
        d2_ident = str(direct_to.get("ident", ""))
        if d2_ident and font is not None:
            d2_lbl = font.render(d2_ident, True, course_col)
            surf.blit(d2_lbl, (int(wpx) + d + 3, int(wpy) - d - 2))

        # Multi-leg FPL polyline (same render as piZ): every waypoint
        # past the active one, joined with a dimmer-magenta GC line +
        # small diamonds + ident labels.  fpl_remaining is the list
        # synced from the MFD over screen sync; None when no plan is
        # active or only one leg remains.
        if (fpl_remaining is not None
                and len(fpl_remaining) >= 2
                and not approach_active):
            faded = (140, 0, 140)
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
                    surf.blit(font.render(next_ident, True, faded),
                              (int(npx) + d + 3, int(npy) - d - 2))
                from_la, from_lo = next_la, next_lo

    # ── Weather (METAR station dots) ───────────────────────────────────────
    # The big MFD draws the flight-category dots on *every* page (we already
    # poll METARs continuously for the airport-tap picker, and there's screen
    # + horsepower to spare).  The MET overlay toggle still drives the heavier
    # weather-focus mode (terrain declutter + NEXRAD) via `wx_active` above; it
    # no longer gates the dots themselves.  (piZ stays gated to the overlay.)
    #
    # Draw the FIS-B ground stations FIRST so the flight-category station dots
    # always land on top of them (the dots carry the weather; the tower is just
    # context).  METARs are already drawn after the magenta D2/FPL above, so a
    # dot on a waypoint stays visible over the diamond too.
    #
    # Graphical hazard areas (G-AIRMET/SIGMET polygons) shade *under* the dots
    # and towers, only on the MET overlay (weather-focus page).
    if (wx_graphics and settings.get("map_show_metar", False) and not fast):
        _draw_wx_graphics(surf, wx_graphics, _project, rect, font)
    if ground_stations and settings.get("map_show_metar", False) and not fast:
        _draw_ground_stations(surf, ground_stations, _project, font, rect,
                              symbol_scale)
    # Always-on flight-category dots — except on the winds / NEXRAD focus pages,
    # where they'd fight the barbs / radar for the same pixels (keep those clean).
    if (metars and not fast
            and not settings.get("map_show_winds", False)
            and not settings.get("map_show_nexrad", False)):
        _draw_metars(surf, metars, rect, lat, lon, cos_lat, cx, cy, px_per_nm,
                     sin_r, cos_r)
    # Winds-aloft barbs — their own overlay (WND).
    if winds_barbs and settings.get("map_show_winds", False) and not fast:
        # Smaller than the full symbol scale so the denser barb grid stays
        # readable on the big MFD (pi4 only — piZ keeps its own size).
        _draw_winds_barbs(surf, winds_barbs, _project, rot_deg, rect, font,
                          symbol_scale * 0.6, range_nm)

    # ── ADS-B traffic ──────────────────────────────────────────────────────
    # Above map features (incl. weather) but below the range ring + own-ship
    # chevron so the pilot's own symbol always stays on top.  On the weather-
    # focus pages (MET / WND / NEXRAD) show ONLY alert-level traffic — declutter
    # the picture without ever hiding a genuine threat.
    if traffic and settings.get("map_show_traffic", True):
        if (settings.get("map_show_metar", False)
                or settings.get("map_show_winds", False)
                or settings.get("map_show_nexrad", False)):
            tfc = [t for t in traffic if t.get("threat") == "alert"]
        else:
            tfc = traffic
        if tfc:
            _draw_traffic(surf, tfc, _project, rot_deg, px_per_nm, font, rect)

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
    # When panned (own_lat/own_lon given and away from the map centre),
    # project the aircraft to its real screen position instead of pinning
    # the chevron to the centre.
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
            # Great-circle distance to the waypoint.  The earlier
            # flat-earth approximation (Pythagorean on lat/lon × NM
            # per deg) overestimates by ~4 % at 700 nm leg lengths
            # — visible as a ~15 min ETE delta against pi_zero's GC
            # calc on the same flight plan.
            phi1 = math.radians(lat)
            phi2 = math.radians(direct_to["lat"])
            dphi = phi2 - phi1
            dlam = math.radians(direct_to["lon"] - lon)
            _a = (math.sin(dphi * 0.5) ** 2
                  + math.cos(phi1) * math.cos(phi2)
                  * math.sin(dlam * 0.5) ** 2)
            d_nm = 3440.065 * 2.0 * math.atan2(
                math.sqrt(_a), math.sqrt(1.0 - _a))
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
