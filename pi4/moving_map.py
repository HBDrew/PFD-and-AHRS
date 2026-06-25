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
_MISSED_AMBER = (255, 170, 60)      # missed-approach path / hold (dashed)


def _dashed_polyline(surf, color, pts, dash=9, gap=6, width=2):
    """Draw a dashed line through screen-space points (ints)."""
    if not pts or len(pts) < 2:
        return
    for (x0, y0), (x1, y1) in zip(pts[:-1], pts[1:]):
        seg = math.hypot(x1 - x0, y1 - y0)
        if seg < 1:
            continue
        ux, uy = (x1 - x0) / seg, (y1 - y0) / seg
        d = 0.0
        while d < seg:
            a = d
            b = min(d + dash, seg)
            pygame.draw.line(surf, color,
                             (int(x0 + ux * a), int(y0 + uy * a)),
                             (int(x0 + ux * b), int(y0 + uy * b)), width)
            d += dash + gap


def _place_label(surf, font, text, color, ax, ay, placed):
    """Blit ``text`` to the SIDE of the anchor (ax, ay) — never centred over it,
    which would sit on the course line.  Labels are right- or left-justified and
    staggered side-to-side for consecutive fixes; the first candidate that
    doesn't overlap an already-placed rect wins.  Appends the chosen rect to
    ``placed`` so clustered fixes (RWY 03 / FAF / IF near the airport) don't
    stack."""
    if not text or font is None:
        return
    img = font.render(text, True, color)
    w, h = img.get_size()
    d = 6
    step = h + 2
    # Side-anchored candidates, stepped vertically so a crowded cluster can
    # stack labels up/down beside it instead of overlapping.  Never centred
    # over the fix (that sits on the course line).
    rights, lefts = [], []
    for m in (0, 1, -1, 2, -2, 3, -3):
        rights.append((ax + d, ay - h // 2 + m * step))
        lefts.append((ax - w - d, ay - h // 2 + m * step))
    # Stagger: alternate which side we try first so neighbours land opposite.
    cands = []
    first, second = (rights, lefts) if (len(placed) % 2 == 0) else (lefts, rights)
    for a, b in zip(first, second):
        cands.append(a)
        cands.append(b)
    for cx, cy in cands:
        r = pygame.Rect(cx, cy, w, h)
        if not any(r.colliderect(pr) for pr in placed):
            surf.blit(img, (cx, cy))
            placed.append(r)
            return
    cx, cy = cands[0]
    surf.blit(img, (cx, cy))
    placed.append(pygame.Rect(cx, cy, w, h))


def _hold_racetrack_pts(la, lo, course_deg, turn, leg_nm, cos_lat):
    """Lat/lon points tracing a holding racetrack at (la, lo): inbound on
    ``course_deg`` to the fix, ``turn`` ('R'/'L') onto the parallel outbound
    leg, joined by 180° arcs.  Returned as a closed loop of (lat, lon)."""
    c = math.radians(course_deg)
    # Unit vectors in (East, North) nm.  u = inbound heading toward the fix.
    ue, un = math.sin(c), math.cos(c)
    if (turn or "R").upper().startswith("L"):       # left turns
        se, sn = -math.cos(c), math.sin(c)
    else:                                           # right turns (default)
        se, sn = math.cos(c), -math.sin(c)
    leg = max(1.0, float(leg_nm or 4.0))
    r = max(0.5, leg * 0.32)                        # turn radius (visual)
    # Fix F at origin; A = start of inbound leg; arcs centred r to the side.
    en = []
    N = 12
    # inbound straight: A -> F
    en.append((-leg * ue, -leg * un))
    en.append((0.0, 0.0))
    # turn 1 at the fix: F -> F2 (bulges forward, +u)
    c1e, c1n = r * se, r * sn
    for k in range(N + 1):
        ph = math.pi * k / N
        en.append((c1e + r * (-se * math.cos(ph) + ue * math.sin(ph)),
                   c1n + r * (-sn * math.cos(ph) + un * math.sin(ph))))
    # outbound straight F2 -> A2 is implicit (next arc start); turn 2 at A end
    c2e, c2n = -leg * ue + r * se, -leg * un + r * sn
    for k in range(N + 1):
        ph = math.pi * k / N
        en.append((c2e + r * (se * math.cos(ph) - ue * math.sin(ph)),
                   c2n + r * (sn * math.cos(ph) - un * math.sin(ph))))
    out = []
    for e, n in en:
        out.append((la + n / _NM_PER_DEG_LAT,
                    lo + e / (_NM_PER_DEG_LAT * max(0.05, cos_lat))))
    return out
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

# Cached ROTATED tint.  pygame.transform.rotate of the big (~hypot(w,h)) tint is
# the single most expensive op in the MFD render (~10 ms each, every frame) — the
# north-up tint is already cached, but the ROTATION was redone every frame even
# on a dead-steady heading.  Cache the rotated surface keyed on the source
# surface identity + the rounded angle (same trick _draw_nexrad uses), so a
# steady heading reuses it and only a turn (or a tint rebuild) re-rotates.
_tint_rot: dict = {"src": None, "deg": None, "surf": None}


def _rot_cached(cache, src, rot_deg):
    """Return src rotated by rot_deg, reusing the cached rotation when the
    source surface and rounded angle are unchanged."""
    deg_key = round(rot_deg)
    if cache["surf"] is None or cache["src"] is not src or cache["deg"] != deg_key:
        cache["surf"] = pygame.transform.rotate(src, rot_deg)
        cache["src"] = src
        cache["deg"] = deg_key
    return cache["surf"]

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
    (rgb (n, n, 3) uint8, elevs (n, n) float32, a_lat, a_lon, n_tiles int) —
    north-up — or (None, None, c_lat, c_lon, 0) when numpy isn't available.
    `a_lat`/`a_lon` are the true centre of the world-anchored sample grid
    (see below); the caller blits at _project(a_lat, a_lon) so registration
    stays exact.  Safe to call from a background worker because it never
    touches a pygame surface."""
    if not HAS_NUMPY:
        return None, None, c_lat, c_lon, 0
    n = _TINT_N
    span_nm = 2.0 * range_nm * oversize
    span_lat = span_nm / _NM_PER_DEG_LAT
    cos_lat = max(0.05, math.cos(math.radians(c_lat)))
    span_lon = span_lat / cos_lat

    # World-anchor the sample lattice.  If the grid's top-left is pinned to
    # the (quantised) centre, every rebuild re-anchors the n×n grid and the
    # coarse hypsometric shading re-samples onto shifted absolute points — a
    # visible "pop" at each cell crossing while panning.  Snapping the
    # top-left corner to a fixed global multiple of the sample spacing makes
    # consecutive builds sample the SAME absolute lat/lon points wherever
    # their windows overlap, so a pan slides a stable field with no pop.
    # (cos_lat uses the quantised c_lat, so dlon is stable within a cell;
    # lon spacing is world-stable to within a hair over a few cells — fine.)
    dlat = span_lat / (n - 1)
    dlon = span_lon / (n - 1)
    lat_top = round((c_lat + span_lat * 0.5) / dlat) * dlat
    lon_lf  = round((c_lon - span_lon * 0.5) / dlon) * dlon
    lat_bot = lat_top - span_lat
    lon_rt  = lon_lf + span_lon
    # The anchored grid's centre differs from c_lat/c_lon by up to half a
    # sample; thread it back so the blit lands exactly under the aircraft.
    a_lat = lat_top - span_lat * 0.5
    a_lon = lon_lf + span_lon * 0.5

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
    return rgb, elevs, a_lat, a_lon, int(len(np.unique(enc)))


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
        return (pygame.transform.smoothscale(tile, (target_px, target_px)),
                None, c_lat, c_lon)
    rgb, elevs, a_lat, a_lon, _nt = _build_tint_pixels(
        srtm_dir, water_dir, c_lat, c_lon, range_nm, oversize)
    return _finalize_tint_surface(rgb, target_px), elevs, a_lat, a_lon


def _tint_async_worker(srtm_dir, water_dir, c_lat, c_lon,
                       range_nm, oversize, key):
    """Worker thread: do the heavy numpy work, post the result for the
    main thread to convert into a pygame surface on the next render."""
    try:
        import time as _t
        _t0 = _t.perf_counter()
        _dr0 = _terrain_mod.disk_reads() if _terrain_mod is not None else 0
        rgb, elevs, a_lat, a_lon, _ntiles = _build_tint_pixels(
            srtm_dir, water_dir, c_lat, c_lon, range_nm, oversize)
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
            _tint_ready[key] = (rgb, elevs, a_lat, a_lon)
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
        return None, None, c_lat, c_lon
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
        rgb, elevs, a_lat, a_lon = ready
        entry = (_finalize_tint_surface(rgb, target_px), elevs, a_lat, a_lon)
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
    return None, None, q_lat, q_lon


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


# ── Airspaces (cached) ──────────────────────────────────────────────────────
# Same idea as _draw_borders_cached: the airspace layer used to re-project +
# fill + outline + label EVERY nearby polygon EVERY frame (a SRCALPHA Surface
# alloc per polygon) — the bulk of the "mfd asp" render time.  Bake the rotated
# fills+outlines into one offscreen raster keyed on the quantised view, shift it
# for pan, and rebuild only when the centre/zoom/rotation/toggles actually
# change.  Labels stay OUT of the raster (they'd rotate sideways with it): they
# pre-render at build time and blit upright per frame at the projected centroid.
_airspace_cache = {"key": None, "surf": None, "blat": 0.0, "blon": 0.0,
                   "labels": []}


def _draw_airspaces_cached(surf, rect, airspaces, airspace_visible, settings,
                           range_nm, lat, lon, cos_lat, cx, cy, px_per_nm,
                           sin_r, cos_r, rot_deg, font, fast):
    if (airspaces is None
            or not settings.get("map_show_airspaces", True)
            or range_nm > 80):
        return
    x, y, w, h = rect
    # Rebuild signature: any centre/zoom/rotation move or class-toggle change.
    vis_sig = (tuple(sorted(airspace_visible))
               if airspace_visible is not None else None)
    tog_sig = tuple(sorted((k, settings[k]) for k in settings
                           if k.startswith("map_show_airspace_")))
    qdeg = max(1e-6, 3.0 / max(0.01, px_per_nm) / _NM_PER_DEG_LAT)
    qlat = round(lat / qdeg)
    qlon = round(lon / (qdeg / cos_lat))
    key = (qlat, qlon, round(range_nm, 1), round(rot_deg / 2.0),
           vis_sig, tog_sig, w, h)
    c = _airspace_cache
    if c["surf"] is None or (not fast and c["key"] != key):
        bs = pygame.Surface((w, h), pygame.SRCALPHA)
        lcx, lcy = cx - x, cy - y       # build centre = (w/2, h/2) inside bs
        labels = []
        for asp in _airspaces_query_nearby(airspaces, lat, lon, range_nm * 1.4):
            cls = asp["class"]
            if airspace_visible is not None and cls not in airspace_visible:
                continue
            if not settings.get(f"map_show_airspace_{cls.lower()}", True):
                continue
            col, fill = _AIRSPACE_COLORS.get(cls, _AIRSPACE_DEFAULT)
            pts = []
            for la, lo in asp["polygon"]:
                n_nm = (la - lat) * _NM_PER_DEG_LAT
                e_nm = (lo - lon) * _NM_PER_DEG_LAT * cos_lat
                e2 = e_nm * cos_r - n_nm * sin_r
                n2 = e_nm * sin_r + n_nm * cos_r
                pts.append((int(lcx + e2 * px_per_nm), int(lcy - n2 * px_per_nm)))
            if len(pts) < 3:
                continue
            xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
            if max(xs) < 0 or min(xs) > w or max(ys) < 0 or min(ys) > h:
                continue
            # Fill via a per-polygon SRCALPHA surface blitted onto bs — same
            # alpha handling as the original (draws onto SRCALPHA can drop the
            # fill; a blit composites it correctly and lets overlaps blend).
            if fill is not None:
                bx0 = max(0, min(xs)); by0 = max(0, min(ys))
                bx1 = min(w, max(xs)); by1 = min(h, max(ys))
                bw_ = max(1, bx1 - bx0); bh_ = max(1, by1 - by0)
                fs = pygame.Surface((bw_, bh_), pygame.SRCALPHA)
                pygame.draw.polygon(fs, fill, [(p[0] - bx0, p[1] - by0)
                                               for p in pts])
                bs.blit(fs, (bx0, by0))
            pygame.draw.polygon(bs, col, pts, 2)
            if font is not None:
                w_px = max(xs) - min(xs); h_px = max(ys) - min(ys)
                if w_px > 60 and h_px > 30:
                    # Centroid in lat/lon (view-independent) so the upright
                    # label re-projects to the right spot on every frame.
                    poly = asp["polygon"]
                    clat = sum(p[0] for p in poly) / len(poly)
                    clon = sum(p[1] for p in poly) / len(poly)
                    id_surf = font.render(asp["ident"], True, col)
                    alt_str = _airspace_alt_label(asp)
                    alt_surf = (font.render(alt_str, True, col)
                                if alt_str else None)
                    labels.append((clat, clon, id_surf, alt_surf))
        c.update(key=key, surf=bs, blat=lat, blon=lon, labels=labels)
    # Shift the cached raster by the screen-space centre movement since build.
    e_nm = (c["blon"] - lon) * _NM_PER_DEG_LAT * cos_lat
    n_nm = (c["blat"] - lat) * _NM_PER_DEG_LAT
    dx = (e_nm * cos_r - n_nm * sin_r) * px_per_nm
    dy = -(e_nm * sin_r + n_nm * cos_r) * px_per_nm
    surf.blit(c["surf"], (x + dx, y + dy))
    # Upright labels, re-projected per frame at the current view.
    if font is not None:
        for clat, clon, id_surf, alt_surf in c["labels"]:
            n_nm = (clat - lat) * _NM_PER_DEG_LAT
            e_nm = (clon - lon) * _NM_PER_DEG_LAT * cos_lat
            e2 = e_nm * cos_r - n_nm * sin_r
            n2 = e_nm * sin_r + n_nm * cos_r
            cxp = int(cx + e2 * px_per_nm); cyp = int(cy - n2 * px_per_nm)
            if alt_surf is not None:
                gap = 1
                total_h = id_surf.get_height() + gap + alt_surf.get_height()
                y0 = cyp - total_h // 2
                surf.blit(id_surf, (cxp - id_surf.get_width() // 2, y0))
                surf.blit(alt_surf, (cxp - alt_surf.get_width() // 2,
                                     y0 + id_surf.get_height() + gap))
            else:
                surf.blit(id_surf, (cxp - id_surf.get_width() // 2,
                                    cyp - id_surf.get_height() // 2))


# ── Obstacle + airport symbols (cached) ─────────────────────────────────────
# Both are static dots that used to re-project + re-draw every frame — the
# obstacle loop is UNCAPPED, so a dense area paints hundreds of circles per
# frame.  Bake both into one rotated raster keyed on the quantised view (same
# trick as the airspace/border caches); airport LABELS stay out of the raster
# (they'd rotate) and blit upright per frame, with the live Direct-To skip.
_symbols_cache = {"key": None, "surf": None, "blat": 0.0, "blon": 0.0,
                  "labels": []}


def _draw_symbols_cached(surf, rect, obstacles_arr, airports_arr, settings,
                         range_nm, lat, lon, alt_ft, cos_lat, cx, cy, px_per_nm,
                         sin_r, cos_r, rot_deg, font, symbol_scale,
                         airport_types_visible, direct_to, wx_active, fast):
    show_obs = (settings.get("map_show_obstacles", True)
                and obstacles_arr is not None and range_nm <= 10
                and not wx_active)
    show_apt = (settings.get("map_show_airports", True)
                and airports_arr is not None and range_nm <= 40
                and not wx_active)
    if not show_obs and not show_apt:
        return
    x, y, w, h = rect

    def _r(v):
        return max(1, int(round(v * symbol_scale)))

    apt_types_sig = (tuple(sorted(airport_types_visible))
                     if airport_types_visible is not None else None)
    qdeg = max(1e-6, 3.0 / max(0.01, px_per_nm) / _NM_PER_DEG_LAT)
    qlat = round(lat / qdeg)
    qlon = round(lon / (qdeg / cos_lat))
    key = (qlat, qlon, round(range_nm, 1), round(rot_deg / 2.0),
           show_obs, show_apt, apt_types_sig, round(symbol_scale, 2),
           round(alt_ft / 100.0) if show_obs else 0, w, h)
    c = _symbols_cache
    if c["surf"] is None or (not fast and c["key"] != key):
        bs = pygame.Surface((w, h), pygame.SRCALPHA)
        lcx, lcy = cx - x, cy - y
        labels = []

        def _lproj(la, lo):
            n_nm = (la - lat) * _NM_PER_DEG_LAT
            e_nm = (lo - lon) * _NM_PER_DEG_LAT * cos_lat
            e2 = e_nm * cos_r - n_nm * sin_r
            n2 = e_nm * sin_r + n_nm * cos_r
            return int(lcx + e2 * px_per_nm), int(lcy - n2 * px_per_nm)

        if show_obs:
            nearby = _obs_mod.query_nearby(obstacles_arr, lat, lon,
                                           radius_nm=range_nm * 1.4,
                                           alt_ft=alt_ft)
            if HAS_NUMPY and hasattr(nearby, "dtype") and len(nearby) > 0:
                for la, lo in zip(nearby["lat"], nearby["lon"]):
                    ox, oy = _lproj(float(la), float(lo))
                    pygame.draw.circle(bs, _OBS_COL, (ox, oy), 2)
            else:
                for o in nearby:
                    ox, oy = _lproj(o.lat, o.lon)
                    pygame.draw.circle(bs, _OBS_COL, (ox, oy), 2)

        if show_apt:
            nearby = _apt_mod.query_nearby(airports_arr, lat, lon,
                                           radius_nm=range_nm * 1.4)
            if HAS_NUMPY and hasattr(nearby, "dtype") and len(nearby) > 0:
                ids = nearby["ident"]; types = nearby["atype"]
                lats = nearby["lat"]; lons = nearby["lon"]
                drawn = 0
                for i in range(len(nearby)):
                    if drawn >= 60:               # MAX_AIRPORTS_DRAWN
                        break
                    atype = str(types[i])
                    if (airport_types_visible is not None
                            and atype not in airport_types_visible):
                        continue
                    ix, iy = _lproj(float(lats[i]), float(lons[i]))
                    if atype == "H":
                        pygame.draw.circle(bs, _APT_HELI, (ix, iy), _r(3))
                    elif atype == "W":
                        pygame.draw.circle(bs, _APT_WATER, (ix, iy), _r(3), 1)
                    elif atype == "B":
                        pygame.draw.circle(bs, _APT_OTHER, (ix, iy), _r(2))
                    else:
                        pygame.draw.circle(bs, _APT_PUB, (ix, iy),
                                           _r(4) if atype in ("M", "L") else _r(3))
                    ident_str = str(ids[i])
                    if (font is not None
                            and (range_nm <= 10
                                 or (range_nm <= 20 and atype in ("M", "L")))):
                        ck = (ident_str, id(font))
                        lbl = _apt_label_cache.get(ck)
                        if lbl is None:
                            lbl = font.render(ident_str, True, _APT_PUB)
                            _apt_label_cache[ck] = lbl
                            if len(_apt_label_cache) > _APT_LABEL_CACHE_MAX:
                                _apt_label_cache.popitem(last=False)
                        else:
                            _apt_label_cache.move_to_end(ck)
                        labels.append((float(lats[i]), float(lons[i]), lbl,
                                       ident_str.strip().upper()))
                    drawn += 1
        c.update(key=key, surf=bs, blat=lat, blon=lon, labels=labels)
    # Shift the cached raster by the screen-space centre movement since build.
    e_nm = (c["blon"] - lon) * _NM_PER_DEG_LAT * cos_lat
    n_nm = (c["blat"] - lat) * _NM_PER_DEG_LAT
    dx = (e_nm * cos_r - n_nm * sin_r) * px_per_nm
    dy = -(e_nm * sin_r + n_nm * cos_r) * px_per_nm
    surf.blit(c["surf"], (x + dx, y + dy))
    # Airport labels: upright, per frame, with the live Direct-To skip so the
    # white loop label never doubles the magenta D2 label on the same field.
    if font is not None and c["labels"]:
        d2_ident = (str(direct_to.get("ident", "")).strip().upper()
                    if direct_to and direct_to.get("ident") else "")
        loff = _r(5) + 2
        for la, lo, lbl, ident_up in c["labels"]:
            if ident_up == d2_ident:
                continue
            n_nm = (la - lat) * _NM_PER_DEG_LAT
            e_nm = (lo - lon) * _NM_PER_DEG_LAT * cos_lat
            e2 = e_nm * cos_r - n_nm * sin_r
            n2 = e_nm * sin_r + n_nm * cos_r
            ix = int(cx + e2 * px_per_nm); iy = int(cy - n2 * px_per_nm)
            surf.blit(lbl, (ix + loff, iy - 7))


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
_METAR_LABEL_MAX_NM = 160  # show station idents only when map range is below this
_METAR_LABEL_COL = (215, 215, 215)   # neutral — the dot already carries category
_METAR_LABEL_CACHE_MAX = 256
_metar_label_cache: "_collections_mm.OrderedDict" = _collections_mm.OrderedDict()


def _draw_metars(surf, metars, rect, lat, lon, cos_lat, cx, cy, px_per_nm,
                 sin_r, cos_r, max_draw=_METAR_MAX_DRAW, font=None,
                 range_nm=None):
    """Draw METAR stations as flight-category-coloured dots (green/blue/red/
    magenta), with the ICAO ident labelled when zoomed in (range below
    _METAR_LABEL_MAX_NM).

    Vectorised: at wide zoom the WX poller pulls a ~250 nm radius (hundreds of
    stations), and the old per-station projection closure loop dominated the
    MET-page frame time on the Pi 4.  Project every station in one numpy pass,
    cull to the visible window, and cap the drawn count to the nearest-to-centre
    (far dots are unreadable clutter at wide zoom anyway)."""
    x, y, w, h = rect
    if not metars:
        return
    do_labels = (font is not None and range_nm is not None
                 and range_nm < _METAR_LABEL_MAX_NM)
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
            if do_labels:
                _metar_label(surf, int(sx), int(sy), m.get("icao", ""), font)
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
        ix, iy = int(sxi[i]), int(syi[i])
        _metar_dot(surf, ix, iy,
                   _WX_CAT_COLORS.get(rows[i].get("fltcat"), _WX_UNKNOWN))
        if do_labels:
            _metar_label(surf, ix, iy, rows[i].get("icao", ""), font)


def _metar_dot(surf, ix, iy, col):
    """One flight-category dot: dark halo + coloured fill + crisp edge.
    Smaller than the piZ MFD dots — the inset packs airports/traffic in too."""
    pygame.draw.circle(surf, (5, 5, 5), (ix, iy), 8)
    pygame.draw.circle(surf, col, (ix, iy), 6)
    pygame.draw.circle(surf, (5, 5, 5), (ix, iy), 6, 1)


def _metar_label(surf, ix, iy, icao, font):
    """Blit the station ICAO ident just above-right of its dot.  Renders are
    cached (LRU) with a 1 px dark backing baked in for contrast over terrain —
    font.render every frame for a screenful of stations is the cost to avoid."""
    if not icao:
        return
    ck = (icao, id(font))
    lbl = _metar_label_cache.get(ck)
    if lbl is None:
        fg = font.render(icao, True, _METAR_LABEL_COL)
        sh = font.render(icao, True, (0, 0, 0))
        lbl = pygame.Surface((fg.get_width() + 1, fg.get_height() + 1),
                             pygame.SRCALPHA)
        lbl.blit(sh, (1, 1))
        lbl.blit(fg, (0, 0))
        _metar_label_cache[ck] = lbl
        if len(_metar_label_cache) > _METAR_LABEL_CACHE_MAX:
            _metar_label_cache.popitem(last=False)
    else:
        _metar_label_cache.move_to_end(ck)
    surf.blit(lbl, (ix + 9, iy - 6))


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
           wx_graphics=None, winds_barbs=None, nexrad_cells=None,
           approach_path=None, runway_marker=None,
           missed_path=None, holds=None):
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
        # Build a square tint around the QUANTISED centre, at the same
        # px_per_nm as the vector features, large enough to cover the
        # (non-square) screen plus rotation + snapped-centre margin.
        #  • Scale: size to min(w,h)/(2*range) — the features' px_per_nm —
        #    NOT max(w,h), or terrain renders ~max/min too big on a non-square
        #    MFD (e.g. 1.7x on 1024x600) and slides faster than the map on a
        #    pan, throwing peaks several NM off.
        #  • Offset: blit at the *projected quantised centre*, not the screen
        #    centre, or the tint (built around the snapped centre) jumps up to
        #    a full cell (~8 NM) when the snap crosses a boundary mid-pan.
        cover_px = (math.hypot(w, h) if orient == "trk" else float(max(w, h)))
        cover_px *= 1.06   # margin for rotation + the snapped-centre offset
        oversize = cover_px / max(1.0, float(min(w, h)))
        _wd = water_dir if settings.get("map_show_water", True) else ""
        tint, elev_grid, a_lat, a_lon = _tint_get(
            srtm_dir, _wd, lat, lon, range_nm, min(w, h), oversize)
        # Blit at the *anchored grid centre* (a_lat/a_lon), not the screen
        # centre — the world-anchored lattice snaps the grid corner to a
        # global multiple of the sample spacing, so its centre differs from
        # the aircraft by up to half a sample; projecting that exact point
        # keeps the tint registered as the snap crosses a cell boundary.
        qx, qy = _project(a_lat, a_lon)
        if tint is None and range_nm > _TINT_SYNC_MAX_NM and font is not None:
            # Async build in flight — breadcrumb at centre so the pilot sees
            # the map is still alive while the worker loads tiles.
            wait_surf = font.render("BUILDING…", True, _LABEL)
            surf.blit(wait_surf,
                      (int(cx) - wait_surf.get_width() // 2,
                       int(cy) - wait_surf.get_height() // 2))
        if tint is not None:
            if orient == "trk" and rot_deg != 0.0:
                tint_r = _rot_cached(_tint_rot, tint, rot_deg)
            else:
                tint_r = tint
            tr = tint_r.get_rect(center=(int(qx), int(qy)))
            surf.blit(tint_r, tr)
            tint_drawn = True

            # SVT-style clearance overlay (red / orange / amber).  Inhibit
            # below Vso so taxi and rollout don't paint the inset red —
            # mirrors how the PFD's TAWS banner is gated.
            if vso_kt is not None and gs_kt >= vso_kt and elev_grid is not None:
                overlay = _build_alert_overlay(
                    elev_grid, alt_ft, int(min(w, h) * oversize))
                if overlay is not None:
                    if orient == "trk" and rot_deg != 0.0:
                        overlay_r = pygame.transform.rotate(overlay, rot_deg)
                    else:
                        overlay_r = overlay
                    o_rect = overlay_r.get_rect(center=(int(qx), int(qy)))
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
    _draw_airspaces_cached(surf, rect, airspaces, airspace_visible, settings,
                           range_nm, lat, lon, cos_lat, cx, cy, px_per_nm,
                           sin_r, cos_r, rot_deg, font, fast)

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

    # ── Obstacle + airport symbols (cached — see _draw_symbols_cached) ───────
    # Obstacles only ≤10 nm, airports ≤40 nm; both static dots, so they're
    # baked into one rotated raster keyed on the view and blitted per frame.
    _draw_symbols_cached(surf, rect, obstacles_arr, airports_arr, settings,
                         range_nm, lat, lon, alt_ft, cos_lat, cx, cy, px_per_nm,
                         sin_r, cos_r, rot_deg, font, symbol_scale,
                         airport_types_visible, direct_to, wx_active, fast)

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

    # ── Loaded published approach path (cyan) ──────────────────────────────
    # The transition + final legs of a loaded CIFP approach, drawn as a cyan
    # polyline with fix diamonds + idents so the pilot can see the whole
    # procedure (the final-approach course line is the last leg).  Independent
    # of direct_to so it also draws on the approach preview (no D2 set).
    _appr_label_rects = []                     # de-conflict approach labels
    if approach_path is not None and len(approach_path) >= 2:
        d2 = 4

        def _appr_fix(la, lo, ident):
            px, py = _project(la, lo)
            pygame.draw.polygon(surf, _HITS_CYAN,
                                [(int(px), int(py) - d2), (int(px) + d2, int(py)),
                                 (int(px), int(py) + d2), (int(px) - d2, int(py))])
            # Skip the label for the runway/MAP fix when the runway bar already
            # labels it (avoids the redundant "RW03" over "RWY 03").
            if ident and not (
                    runway_marker is not None and ident[:2] == "RW"
                    and ident[2:3].isdigit()):
                _place_label(surf, font, ident, _HITS_CYAN,
                             int(px), int(py), _appr_label_rects)

        fla, flo, fid = approach_path[0]
        _appr_fix(fla, flo, fid)               # the first fix (IAF) too
        for nla, nlo, nident in approach_path[1:]:
            pygame.draw.line(surf, _HITS_CYAN,
                             (int(_project(fla, flo)[0]), int(_project(fla, flo)[1])),
                             (int(_project(nla, nlo)[0]), int(_project(nla, nlo)[1])), 2)
            _appr_fix(nla, nlo, nident)
            fla, flo = nla, nlo

    # ── Runway marker — the physical runway the approach lands on ───────────
    # ((le_lat, le_lon), (he_lat, he_lon), label): a single clean white bar
    # between the two real runway thresholds, so its position + orientation are
    # exact.  Explicit (not the runway-DB layer) so it shows at preview zoom.
    if runway_marker is not None:
        (a_la, a_lo), (b_la, b_lo), rlabel = runway_marker
        ax, ay = _project(a_la, a_lo)
        bx, by = _project(b_la, b_lo)
        pygame.draw.line(surf, (245, 245, 250),
                         (int(ax), int(ay)), (int(bx), int(by)), 5)
        if rlabel:
            _place_label(surf, font, rlabel, (210, 220, 235),
                         int(bx), int(by), _appr_label_rects)  # de-conflicted

    # ── Missed approach (dashed amber) + holding patterns ───────────────────
    # missed_path: [(la, lo, ident), …].  Per the plate it does NOT touch the
    # runway — it starts at the climb point off the MAP.  holds: list of
    # (la, lo, course, turn, leg_nm) racetracks (HILPT at the IF, missed hold…).
    if missed_path is not None and len(missed_path) >= 2:
        scr = [(_project(p[0], p[1])) for p in missed_path]
        _dashed_polyline(surf, _MISSED_AMBER,
                         [(int(sx), int(sy)) for sx, sy in scr], width=2)
        d2 = 4
        for (la_m, lo_m, ident), (sx, sy) in list(zip(missed_path, scr))[1:]:
            px, py = int(sx), int(sy)
            pygame.draw.polygon(surf, _MISSED_AMBER,
                                [(px, py - d2), (px + d2, py),
                                 (px, py + d2), (px - d2, py)], 1)
            _place_label(surf, font, ident, _MISSED_AMBER,
                         px, py, _appr_label_rects)
    for h in (holds or []):
        h_la, h_lo, h_crs, h_turn, h_leg = h
        loop = _hold_racetrack_pts(h_la, h_lo, h_crs, h_turn, h_leg, cos_lat)
        _dashed_polyline(surf, _MISSED_AMBER,
                         [(int(_project(a, b)[0]), int(_project(a, b)[1]))
                          for a, b in loop], width=2)

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
                     sin_r, cos_r, font=font, range_nm=range_nm)
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
