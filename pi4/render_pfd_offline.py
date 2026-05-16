#!/usr/bin/env python3
"""
render_pfd_offline.py – Generate full PFD preview PNGs with OpenGL SVT,
without requiring a display server.

This bypasses pygame.display.init() (which conflicts with EGL on Xvfb)
by creating offscreen pygame.Surface objects directly and calling the
PFD's render() function on them.  Useful for:

  - Generating preview PNGs in CI / headless environments
  - Validating the OpenGL SVT integration before hardware is available

On real Pi 4 hardware running pfd.py normally, the display path
(KMS/DRM) doesn't conflict with EGL — the standard --screenshots
mode in pfd.py works fine.

Usage:
    python3 pi4/render_pfd_offline.py [output_dir]
"""
import os
import sys
import argparse

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, '..', 'shared'))
sys.path.insert(0, _HERE)

# Use SDL dummy driver before pygame import — no display server needed.
# Also force a dummy audio driver so pygame doesn't open an idle ALSA
# stream (which logs harmless but noisy "underrun" lines).
os.environ['SDL_VIDEODRIVER'] = 'dummy'
os.environ['SDL_AUDIODRIVER'] = 'dummy'
import pygame
pygame.init()
# Mouse subsystem may not init under dummy driver; ignore failure
try:
    pygame.mouse.set_visible(False)
except pygame.error:
    pass

# Now import pfd module — it'll see SVT_RENDERER from config
import pfd

# pfd.py hard-codes _SVT_GL_AVAILABLE = False to avoid disrupting KMS/DRM
# on live Pi 4 hardware.  Live pfd.py uses SVT_RENDERER="opengl_shared",
# which composites GL terrain through pygame.OPENGL + a shared moderngl
# Context — that path needs an actual display surface, which we don't have
# under SDL_VIDEODRIVER=dummy.
#
# For the offline tool we switch to the standalone GL renderer
# (SVT_RENDERER="opengl"), which creates its own offscreen EGL context and
# returns a pygame.Surface, then enable it.  Without this override the
# offline path silently falls through to the pygame 2D scanline raster and
# the captured PNGs lose the 3D mesh look.  Probe failures are surfaced
# loudly so we don't ship raster-mode previews by accident.
_offline_gl_ok = False
try:
    from svt_renderer_gl import is_available as _gl_probe
    if _gl_probe():
        pfd._SVT_GL_AVAILABLE = True
        pfd.SVT_RENDERER       = "opengl"
        _offline_gl_ok = True
    else:
        print("[render_pfd_offline] GL probe returned False — captures will "
              "use the 2D raster fallback, not the 3D mesh.", file=sys.stderr)
except Exception as exc:
    print(f"[render_pfd_offline] GL probe raised: {exc!r} — captures will "
          "use the 2D raster fallback.", file=sys.stderr)

from config import (DISPLAY_W, DISPLAY_H, BARO_DEFAULT_HPA,
                    DEMO_LAT, DEMO_LON)

# Same scenes as the regular --screenshots batch mode.
# Each scene: (name, roll, pitch, hdg, alt, speed, vspeed, ay, [lat, lon])
# Optional lat/lon trailing entries override DEMO_LAT/DEMO_LON.
SCENES = [
    ("preview_sedona_level",       0,   2, 133, 8500, 115,    0,   0),
    ("preview_sedona_climb_turn", -18,  6, 145, 7800, 95,   500, 0.12),
    ("preview_sedona_approach",    0,  -3, 200, 5800, 90,  -700,   0),
    ("preview_low_altitude",      10,   0, 133, 4500, 95,     0,   0),
    ("preview_high_altitude",      0,   0, 133, 12000, 115,   0,   0),
    ("preview_climb_left",       -15,   8, 100, 6500, 95,   500, -0.10),
    # Combined SVT + airport + obstacle: approaching Sedona (KSEZ) from NE at
    # pattern altitude with a tall tower in view and rising terrain all around.
    ("preview_svt_airports_obstacles", -4, -2, 226, 5500, 85, -300, 0),
    # Dedicated runway approach scene: short final to KSEZ RWY 03, ~2.5 NM
    # SSW of the threshold on a 3° glideslope at ~700 ft AGL.  Shows runway
    # polygons and extended dashed centerlines prominently.
    ("preview_runway_approach",    0,  -3,  33, 5500,  80, -500,   0,
                                   34.809, -111.823),
    # Direct-to KSEZ scene: 6 NM SSW of the airport at 6500 ft, 0.5 NM right
    # of an established 026° course (so the CDI diamond deflects).  Shows
    # the magenta course trace, KSEZ environment box, RWY 03/21 extended
    # centerlines (filtered to the selected waypoint), and the CDI strip.
    ("preview_direct_to_ksez",     2,  -2,  30, 6500, 110, -300, 0.02,
                                   34.7600, -111.8630),
    # Synthetic approach to KSEZ RWY 03 — short final, ~3 NM out, slightly
    # above the 3° glideslope so the VDI diamond deflects DOWN (fly down
    # to it).  Same flight params as preview_runway_approach but with
    # disp["approach"] activated so the documentation captures HITS,
    # VDI, ±0.3 nm cyan CDI, and the cyan inset trace.
    ("preview_synthetic_approach", 0,  -3,  33, 5500,  85, -500,   0,
                                   34.809, -111.823),
    # Dedicated HITS scene — closer to the threshold (~1.3 NM out) and
    # lower (5100 ft, ~270 ft above threshold) so the cyan HITS boxes
    # are large and prominent in the foreground rather than crowded
    # behind the extended centreline at the 3-NM scene above.
    ("preview_hits_boxes",         0,  -2,  33, 5100,  80, -500,   0,
                                   34.842, -111.811),
    # Unusual-attitude recovery — 75° right bank, +25° pitch.  Trips
    # both EXTREME thresholds so the chevron stack + roll-recovery arc
    # render simultaneously, plus the declutter strips overlays.
    ("preview_unusual_attitude",  75,  25, 130, 7500, 105,  200, 0.18),
    # Sim-running scene — flight scene with the SIM watermark in place.
    # Setup hook (_setup_sim_running) puts disp["sim"]["enabled"] = True.
    ("preview_sim_running",        0,   2, 133, 8500, 115,    0,   0),
    # TAWS caution + warning previews — punched low into rising
    # terrain so the look-ahead trips the alert.  GPS fix is required
    # for the alert to evaluate; the helpers below set alt + ground
    # elev so worst_clearance lands in the caution / warning band.
    ("preview_terrain_caution",    0,  -2, 200, 5800, 110, -400,   0,
                                   34.860, -111.770),
    ("preview_terrain_warning",    0,  -4, 200, 5500, 110, -600,   0,
                                   34.860, -111.770),
]


# Scene-specific extra setup hooks: keyed by scene name, each callable runs
# AFTER seed_state() but BEFORE render().  Used to inject state that doesn't
# fit the (roll, pitch, hdg, alt, ...) tuple — e.g., direct-to nav.
def _setup_direct_to_ksez():
    pfd.disp["nav"] = {
        "ident":   "KSEZ",
        "lat":     34.8485,
        "lon":     -111.7882,
        "elev_ft": 4827.0,
        # Activation 15 NM SSW so cross-track is a real measurement rather
        # than zero-by-construction (which it would be if we recomputed
        # bearing every frame).
        "act_lat": 34.7100,
        "act_lon": -111.9200,
    }
    # Show the moving-map inset on this scene so the documentation
    # captures the magenta GC course line in context.  Inset is normally
    # opt-in via the DISPLAY setup screen.
    pfd.disp["ds"]["map_enabled"] = True
    pfd.disp["ds"]["map_zoom_nm"] = 10


def _setup_synthetic_approach():
    """KSEZ RWY 03 short final — sets disp["approach"] so HITS, VDI,
    ±0.3 nm CDI and the cyan inset trace all render.  Pulls the
    threshold + course out of the loaded runway cache so the HITS
    boxes and runway polygon align with the actual airport label.
    Activation point computed 3 NM back along the reciprocal of the
    real published course."""
    thr_lat, thr_lon, thr_elev, course = _find_ksez_rwy_03()
    act_lat, act_lon = _back_along_course(thr_lat, thr_lon, course, 3.0)
    pfd.disp["nav"] = {
        "ident":   "KSEZ",
        "lat":     thr_lat,
        "lon":     thr_lon,
        "elev_ft": thr_elev,
        "act_lat": act_lat,
        "act_lon": act_lon,
    }
    pfd.disp["approach"] = {
        "active":         True,
        "airport":        "KSEZ",
        "runway":         "03",
        "thresh_lat":     thr_lat,
        "thresh_lon":     thr_lon,
        "thresh_elev_ft": thr_elev,
        "course_deg":     course,
    }
    pfd.disp["ds"]["map_enabled"] = True
    pfd.disp["ds"]["map_zoom_nm"] = 5


def _setup_approach_picker():
    """Approach runway-picker modal for KSEZ.  Reads runway tiles from
    the (synthetic) runway array we already inject for the in-flight
    approach scene."""
    pfd.disp["nav"] = {
        "ident":   "KSEZ",
        "lat":     34.8485,
        "lon":     -111.7882,
        "elev_ft": 4827.0,
        "act_lat": 34.8485,
        "act_lon": -111.7882,
    }
    pfd.disp["approach"] = {"active": False}
    pfd.disp["mode"] = "approach_select"


def _setup_hits_boxes():
    """Mid-final HITS-box documentation shot.  Aircraft sits 1.5 NM
    back along the published course, on the 3° glideslope, lined up.
    From 1.5 NM out the nearest 5–6 HITS boxes (every 1000 ft from
    1000 ft to ~5 NM) all sit ahead of the aircraft and read at
    legible sizes — the closest box is ~8000 ft ahead, ~14 px wide;
    the boxes step up the centreline and visually stack toward the
    threshold.

    Pulls the real KSEZ RWY 03 threshold + course out of the loaded
    runway cache so the boxes align with the actual airport label.
    Falls back to the synthetic threshold (good enough for CI) when
    no runway cache is on disk.

    The SCENES tuple's seeded lat/lon/alt are overwritten here so the
    aircraft is positioned correctly relative to the real threshold."""
    import math

    thr_lat, thr_lon, thr_elev, course = _find_ksez_rwy_03()

    final_nm = 1.5    # aircraft distance back from threshold along final
    # Aircraft position 1.5 NM along the reciprocal of the published course.
    ac_lat, ac_lon = _back_along_course(thr_lat, thr_lon, course, final_nm)
    # Activation 2.5 NM further back so the CDI has history.
    act_lat, act_lon = _back_along_course(thr_lat, thr_lon, course, 4.0)

    # Glideslope altitude at 1.5 NM out — pilot's eye-line on the 3°
    # path that the HITS boxes are centred on.  Add a small 30 ft
    # offset above GS so the VDI diamond deflects DOWN modestly
    # (half-scale or so), which matches the "slightly hot, on path"
    # documentation framing.
    nm_to_ft   = 6076.12
    gs_height  = final_nm * nm_to_ft * math.tan(math.radians(3.0))
    ac_alt_ft  = thr_elev + gs_height + 30.0

    snap = {
        "lat":  ac_lat,   "lon":     ac_lon,
        "alt":  ac_alt_ft, "gps_alt": ac_alt_ft,
        "yaw":  course,   "track":   course,
    }
    with pfd._state_lock:
        pfd.state.update(snap)
    pfd.disp.update(snap)

    pfd.disp["nav"] = {
        "ident":   "KSEZ",
        "lat":     thr_lat,
        "lon":     thr_lon,
        "elev_ft": thr_elev,
        "act_lat": act_lat,
        "act_lon": act_lon,
    }
    pfd.disp["approach"] = {
        "active":         True,
        "airport":        "KSEZ",
        "runway":         "03",
        "thresh_lat":     thr_lat,
        "thresh_lon":     thr_lon,
        "thresh_elev_ft": thr_elev,
        "course_deg":     course,
    }
    # Inset hidden for this shot so the boxes own the frame.
    pfd.disp["ds"]["map_enabled"] = False


def _setup_sim_running():
    """Live-PFD scene with the SIM ✕ button + watermark visible.  Need
    BOTH disp["sim"]["enabled"] = True (in case any future code reads
    it) AND a truthy pfd._sim_state, since the watermark / X button
    render path checks `_sim_state is not None`.  A bare object()
    sentinel is enough — the render path never calls methods on it
    (only main()'s tick loop does, which doesn't run in offline mode)."""
    pfd.disp["sim"]["enabled"]     = True
    pfd.disp["sim"]["paused"]      = False
    pfd.disp["sim"]["follow_mode"] = "bugs"
    pfd._sim_state = object()
    pfd.disp["ds"]["map_enabled"] = True
    pfd.disp["ds"]["map_zoom_nm"] = 10


_REAL_UPDATE_TERRAIN_ALERT = pfd._update_terrain_alert


def _force_terrain_alert(level):
    """Pin the alert level for a single render.  Offline previews don't
    load real SRTM tiles, so the look-ahead math zero-clears the alert
    every frame — we monkey-patch _update_terrain_alert to a no-op
    after seeding the desired level.  main() restores the real
    function once the SCENES loop ends so subsequent modals don't
    inherit a stale pinned alert."""
    pfd._terrain_alert_level = level

    def _noop(*args, **kwargs):
        pass

    pfd._update_terrain_alert = _noop


def _clear_approach_state():
    """Wipe direct-to + approach state so a scene gets a clean PFD
    without leftover KSEZ/03 readout, GS diamond, HITS boxes, or
    cyan inset trace inherited from a previous approach scene."""
    pfd.disp["nav"] = {
        "ident": "", "lat": 0.0, "lon": 0.0, "elev_ft": 0.0,
        "act_lat": 0.0, "act_lon": 0.0,
    }
    pfd.disp["approach"] = {"active": False}


def _setup_terrain_caution():
    # Clear the SIM watermark + approach state left over from the
    # preceding sim_running and synthetic_approach scenes so the TAWS
    # shot renders a clean PFD with just the alert banner.
    pfd.disp["sim"]["enabled"] = False
    pfd._sim_state = None
    _clear_approach_state()
    _force_terrain_alert(1)


def _setup_terrain_warning():
    pfd.disp["sim"]["enabled"] = False
    pfd._sim_state = None
    _clear_approach_state()
    _force_terrain_alert(2)


SCENE_EXTRA_SETUP = {
    "preview_direct_to_ksez":     _setup_direct_to_ksez,
    "preview_synthetic_approach": _setup_synthetic_approach,
    "preview_hits_boxes":         _setup_hits_boxes,
    "preview_sim_running":        _setup_sim_running,
    "preview_terrain_caution":    _setup_terrain_caution,
    "preview_terrain_warning":    _setup_terrain_warning,
}


def _inject_synthetic_runways():
    """Synthesise a KSEZ runway record so the approach-picker modal,
    the runway centreline overlays, and the active-approach scene all
    have something to read **when no real runway data is on disk**.
    On a host with the OurAirports runway cache loaded (pfd._runways is
    populated), this is a no-op: the real data already has KSEZ
    RWY 03/21 at its actual threshold coordinates and overriding it
    with a synthetic record would mis-align the runway polygon
    relative to the airport label."""
    if getattr(pfd, "_runways", None) is not None and len(pfd._runways) > 0:
        return  # real runway data is loaded — don't clobber it

    try:
        import numpy as np
    except ImportError:
        return
    dtype = np.dtype([
        ("airport",   "U7"),
        ("length_ft", "f4"),
        ("width_ft",  "f4"),
        ("surface",   "U6"),
        ("lighted",   "?"),
        ("le_ident",  "U4"),
        ("he_ident",  "U4"),
        ("le_lat",    "f4"), ("le_lon", "f4"),
        ("le_elev_ft","f4"), ("le_hdg", "f4"),
        ("he_lat",    "f4"), ("he_lon", "f4"),
        ("he_elev_ft","f4"), ("he_hdg", "f4"),
    ])
    # KSEZ RWY 03/21 — placeholder geometry used only when no real
    # runway cache is on disk (CI environments, fresh installs).  The
    # coordinates here are intentionally approximate; the live render
    # uses the OurAirports cache via _ad_load_airports() and is exact.
    records = [
        ("KSEZ", 5132.0, 100.0, "ASPH", True,
         "03", "21",
         34.8634, -111.7934, 4827.0,  33.0,
         34.8763, -111.7818, 4827.0, 213.0),
    ]
    pfd._runways = np.array(records, dtype=dtype)


def _find_ksez_rwy_03():
    """Pull the real KSEZ RWY 03 LE threshold (lat, lon, elev_ft) and
    true course out of the loaded runway cache.  Falls back to the
    synthetic placeholder coords if (a) there's no runway data, or
    (b) KSEZ isn't in the cache.  Returns (thresh_lat, thresh_lon,
    thresh_elev_ft, course_deg)."""
    runways = getattr(pfd, "_runways", None)
    if runways is None or len(runways) == 0:
        return 34.8634, -111.7934, 4827.0, 33.0   # synthetic fallback

    # Find the KSEZ RWY 03 record — case-insensitive on the ident.
    for rec in runways:
        if str(rec["airport"]).upper() != "KSEZ":
            continue
        # Look for the end that identifies as "03" (or "3"); the LE/HE
        # naming convention is independent of which numeric end "03"
        # lives on, so we check both pairs and pick whichever matches.
        for end_prefix in ("le", "he"):
            ident = str(rec[f"{end_prefix}_ident"]).strip().lstrip("0")
            if ident == "3":
                return (float(rec[f"{end_prefix}_lat"]),
                        float(rec[f"{end_prefix}_lon"]),
                        float(rec[f"{end_prefix}_elev_ft"]),
                        float(rec[f"{end_prefix}_hdg"]))
    # KSEZ not in cache; fall back to synthetic
    return 34.8634, -111.7934, 4827.0, 33.0


def _back_along_course(thr_lat, thr_lon, course_deg, dist_nm):
    """Compute a lat/lon `dist_nm` behind the threshold along the
    reciprocal of `course_deg` — i.e. the upwind point a final-
    approach aircraft would be at relative to a given threshold."""
    import math
    back_brg = (course_deg + 180.0) % 360.0
    br = math.radians(back_brg)
    cos_lat = max(0.05, math.cos(math.radians(thr_lat)))
    d_lat = (dist_nm / 60.0) * math.cos(br)
    d_lon = (dist_nm / 60.0) * math.sin(br) / cos_lat
    return thr_lat + d_lat, thr_lon + d_lon


def _inject_synthetic_obstacles():
    """Create a small set of synthetic obstacles near Sedona for preview
    rendering.  Real obstacle data comes from FAA DOF (gitignored, ~20 MB)
    but for preview PNGs a handful of towers is enough to demonstrate the
    symbol rendering and its interaction with airports + SVT.

    Ground elevations are looked up from SRTM at each tower location so the
    base of the caret symbol anchors exactly to the rendered terrain.
    If SRTM is unavailable the call falls back to a reasonable default.
    """
    try:
        import numpy as np
    except ImportError:
        return

    try:
        from terrain import get_elevation_ft
        srtm_dir = os.path.join(_HERE, "data", "srtm")
    except ImportError:
        get_elevation_ft = None
        srtm_dir = None

    def _ground_ft(lat, lon, fallback):
        if get_elevation_ft is None or srtm_dir is None:
            return fallback
        try:
            v = get_elevation_ft(srtm_dir, lat, lon)
            return v if v > 0 else fallback
        except Exception:
            return fallback

    # Tower locations + AGL heights (keeping all ≤ ~350 ft AGL for realism).
    # Each entry: (lat, lon, agl_ft, otype, lit, fallback_ground_ft)
    towers = [
        (34.8825, -111.7420, 250, "TWR", True,  5900),
        (34.8650, -111.7100, 180, "ANT", False, 5700),
        (34.8550, -111.7250, 310, "TWR", True,  5700),
        (34.8720, -111.7520, 220, "ANT", False, 5500),
    ]

    records = []
    for lat, lon, agl, otype, lit, fb_ground in towers:
        ground = _ground_ft(lat, lon, fb_ground)
        msl = ground + agl
        records.append((lat, lon, agl, msl, otype, lit))

    arr = np.array(records,
                   dtype=[("lat","f4"),("lon","f4"),
                          ("agl_ft","f4"),("msl_ft","f4"),
                          ("otype","U3"),("lit","?")])
    pfd._obstacles = arr
    pfd.disp["od"]["records"] = len(arr)
    pfd.disp["od"]["used_mb"] = 0.01
    pfd.disp["od"]["expired"] = False


def seed_state(roll, pitch, hdg, alt, speed, vspeed, ay, lat=DEMO_LAT, lon=DEMO_LON):
    """Inject test values directly into pfd state, bypassing IIR smoothing."""
    snap = {
        "lat": lat, "lon": lon,
        "yaw": hdg, "track": hdg,
        "roll": roll, "pitch": pitch,
        "speed": speed, "alt": alt,
        "vspeed": vspeed, "ay": ay,
        "gps_ok": True, "baro_ok": True, "ahrs_ok": True,
        "sats": 8, "gps_alt": alt,
        "baro_hpa": BARO_DEFAULT_HPA, "baro_src": "bme280",
        "fix": True, "pitch_trim": 0.0, "roll_trim": 0.0, "yaw_trim": 0.0,
    }
    with pfd._state_lock:
        pfd.state.update(snap)
    pfd.disp.update(snap)
    pfd.disp["hdg_bug"] = hdg
    pfd.disp["alt_bug"] = alt
    pfd.disp["spd_bug"] = 0
    pfd.disp["mode"] = "pfd"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("outdir", nargs="?",
                        default=os.path.join(_HERE, "previews", "pfd_gl"))
    args = parser.parse_args()

    os.makedirs(args.outdir, exist_ok=True)

    # Force terrain detection so SVT actually renders (otherwise pfd uses
    # the simple sky/ground split when no SRTM tiles)
    pfd._has_terrain = True

    # Load airport database synchronously (normally loaded by background thread)
    pfd._startup_load_airports()

    # Inject synthetic obstacles so previews show the combined symbol stack
    # (real FAA DOF data is ~20 MB, gitignored, downloaded at install time)
    _inject_synthetic_obstacles()
    # Same idea for runways — needed by the approach picker, the active
    # approach scene (HITS / VDI), and the runway centreline overlays.
    _inject_synthetic_runways()

    surf = pygame.Surface((DISPLAY_W, DISPLAY_H))

    print(f"Rendering full-PFD previews with OpenGL SVT to {args.outdir}")
    print(f"Resolution: {DISPLAY_W}×{DISPLAY_H}")
    print(f"SVT_RENDERER: {pfd.SVT_RENDERER}  GL_AVAILABLE: {pfd._SVT_GL_AVAILABLE}")
    if not _offline_gl_ok:
        print("WARNING: GL not available — flight scenes will be rendered "
              "with the 2D raster fallback (pixelated terrain) instead of "
              "the 3D OpenGL mesh.", file=sys.stderr)
    print()

    for scene in SCENES:
        name, roll, pitch, hdg, alt, speed, vspeed, ay = scene[:8]
        lat = scene[8] if len(scene) > 8 else DEMO_LAT
        lon = scene[9] if len(scene) > 9 else DEMO_LON
        seed_state(roll, pitch, hdg, alt, speed, vspeed, ay, lat, lon)
        extra = SCENE_EXTRA_SETUP.get(name)
        if extra is not None:
            extra()
        pfd.smooth_state()
        pfd.render(surf, demo_mode=False, connected=True, data_stale=False)
        outpath = os.path.join(args.outdir, f"{name}.png")
        pygame.image.save(surf, outpath)
        print(f"  → {os.path.basename(outpath)}")

    # Restore the real terrain-alert evaluator + clear any pinned level
    # before we move on to setup screens and modal previews — those run
    # the full render pipeline and would otherwise inherit a stale
    # PULL UP banner from the last TAWS scene.  Also clear the sim
    # enable flag in case the SCENES list gets reordered and the
    # sim_running scene ends up last.
    pfd._update_terrain_alert = _REAL_UPDATE_TERRAIN_ALERT
    pfd._terrain_alert_level  = 0
    pfd.disp["sim"]["enabled"] = False
    pfd._sim_state             = None

    # ── Setup screens (no GL needed) ─────────────────────────────────────────
    # Move output dir up one level so setup PNGs go alongside the existing
    # preview_setup_*.png files in pi4/previews/, not into the pfd_gl subdir.
    setup_outdir = os.path.dirname(args.outdir) if args.outdir.endswith("pfd_gl") \
                                                  else args.outdir

    # Seed plausible non-empty data so the setup tiles show realistic stats
    pfd.disp["od"]["records"] = 76842
    pfd.disp["od"]["used_mb"] = 19.4
    pfd.disp["ad"]["records"] = 72007
    pfd.disp["ad"]["used_mb"] = 12.3
    pfd.disp["ad"]["age_days"] = 5

    for screen_mode, fname in [
        ("setup",               "preview_setup_main.png"),
        ("flight_profile",      "preview_setup_flight_profile.png"),
        ("display_setup",       "preview_setup_display.png"),
        ("ahrs_setup",          "preview_setup_ahrs.png"),
        ("connectivity_setup",  "preview_setup_connectivity.png"),
        ("system_setup",        "preview_setup_system.png"),
        ("airport_data",        "preview_airport_loaded.png"),
        ("sim_setup",           "preview_sim_setup.png"),
    ]:
        pfd.disp["mode"] = screen_mode
        pfd.render(surf, demo_mode=False, connected=True, data_stale=False)
        outpath = os.path.join(setup_outdir, fname)
        pygame.image.save(surf, outpath)
        print(f"  → {os.path.basename(outpath)}")

    # Airport data "downloading" variant (no record counts yet, progress string set)
    pfd.disp["mode"] = "airport_data"
    pfd.disp["ad"]["downloading"] = True
    pfd.disp["ad"]["records"]     = 0
    pfd.disp["ad"]["used_mb"]     = 0.0
    pfd.disp["ad"]["dl_status"]   = "Downloading\u2026 42%  (5,280 / 12,500 KB)"
    pfd.render(surf, demo_mode=False, connected=True, data_stale=False)
    pygame.image.save(surf, os.path.join(setup_outdir, "preview_airport_downloading.png"))
    print("  → preview_airport_downloading.png")
    # Restore loaded state so any later scenes see normal stats
    pfd.disp["ad"]["downloading"] = False
    pfd.disp["ad"]["records"]     = 72007
    pfd.disp["ad"]["used_mb"]     = 12.3
    pfd.disp["ad"]["dl_status"]   = "Done \u2713  72,007 airports loaded"

    # \u2500\u2500 New modals + feature scenes (V4.5\u2013V5.0) \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    # Direct-to keyboard with KSEZ as placeholder + NEAREST extras row.  The
    # background is the live PFD so the modal sits on a real attitude scene.
    seed_state(roll=0, pitch=2, hdg=133, alt=8500, speed=115, vspeed=0, ay=0)
    pfd.disp["nav"] = {
        "ident": "KSEZ", "lat": 34.85, "lon": -111.79, "elev_ft": 4830,
        "act_lat": 34.84, "act_lon": -111.80,
    }
    pfd.disp["kbd_target"] = "nav_ident"
    pfd.disp["kbd_prev"]   = "pfd"
    pfd.disp["kbd_buf"]    = ""
    pfd.disp["kbd_error"]  = ""
    pfd.disp["mode"]       = "keyboard"
    pfd.render(surf, demo_mode=False, connected=True, data_stale=False)
    pygame.image.save(surf, os.path.join(setup_outdir, "preview_direct_to_keyboard.png"))
    print("  \u2192 preview_direct_to_keyboard.png")

    # Same keyboard mid-error: pilot typed an unknown ident.
    pfd.disp["kbd_buf"]   = "ZZZZ"
    pfd.disp["kbd_error"] = "UNKNOWN WAYPOINT  ZZZZ"
    pfd.render(surf, demo_mode=False, connected=True, data_stale=False)
    pygame.image.save(surf, os.path.join(setup_outdir, "preview_unknown_waypoint.png"))
    print("  \u2192 preview_unknown_waypoint.png")
    pfd.disp["kbd_buf"]   = ""
    pfd.disp["kbd_error"] = ""

    # "Activate Direct to KSEZ?" confirmation modal.
    seed_state(roll=0, pitch=2, hdg=133, alt=8500, speed=115, vspeed=0, ay=0)
    pfd.disp["nav_confirm_ident"] = "KSEZ"
    pfd.disp["nav_confirm_prev"]  = "pfd"
    pfd.disp["mode"]              = "nav_confirm"
    pfd.render(surf, demo_mode=False, connected=True, data_stale=False)
    pygame.image.save(surf, os.path.join(setup_outdir, "preview_nav_confirm.png"))
    print("  \u2192 preview_nav_confirm.png")

    # Approach runway-picker modal \u2014 opened when the pilot taps APPR on
    # the nav-confirm modal.  Reads runway tiles from the synthetic
    # KSEZ runway record we injected at startup.
    pfd.disp["nav"] = {
        "ident":   "KSEZ", "lat": 34.8485, "lon": -111.7882,
        "elev_ft": 4827.0, "act_lat": 34.8485, "act_lon": -111.7882,
    }
    pfd.disp["approach"] = {"active": False}
    pfd.disp["mode"]     = "approach_select"
    pfd.render(surf, demo_mode=False, connected=True, data_stale=False)
    pygame.image.save(surf, os.path.join(setup_outdir, "preview_approach_picker.png"))
    print("  \u2192 preview_approach_picker.png")
    pfd.disp["mode"] = "pfd"

    # Compass calibration wizard mid-walk (step 2 = EAST), one cardinal already
    # captured.  Heading near 088 so RAW reads near east.
    seed_state(roll=0, pitch=0, hdg=88, alt=4830, speed=0, vspeed=0, ay=0)
    pfd.disp["yaw"]        = 88.0
    pfd.disp["_yaw_uncal"] = 88.0
    pfd.disp["mag_cal_wiz"] = {
        "step": 1, "samples": [(0.0, 358.5)],
        "msg":  "Captured NORTH.",
        "prev": "ahrs_setup",
    }
    pfd.disp["mode"] = "mag_cal"
    pfd.render(surf, demo_mode=False, connected=True, data_stale=False)
    pygame.image.save(surf, os.path.join(setup_outdir, "preview_compass_cal.png"))
    print("  \u2192 preview_compass_cal.png")

    # Wizard after the four-cardinal walk completes \u2014 shows the four delta values.
    pfd.disp["ss"]["mag_cal_deltas"] = [1.2, -0.8, 0.7, -1.5]
    pfd.disp["mag_cal_wiz"] = {
        "step": 0, "samples": [],
        "msg":  "Done \u2014 N+1.2 E-0.8 S+0.7 W-1.5",
        "prev": "ahrs_setup",
    }
    pfd.render(surf, demo_mode=False, connected=True, data_stale=False)
    pygame.image.save(surf, os.path.join(setup_outdir, "preview_compass_cal_done.png"))
    print("  \u2192 preview_compass_cal_done.png")
    pfd.disp["ss"]["mag_cal_deltas"] = [0.0] * 4

    # AGL readout - normal cruise frame (the box appears bottom-right of AI).
    seed_state(roll=0, pitch=2, hdg=133, alt=6800, speed=115, vspeed=0, ay=0)
    pfd.disp["mode"] = "pfd"
    pfd.render(surf, demo_mode=False, connected=True, data_stale=False)
    pygame.image.save(surf, os.path.join(setup_outdir, "preview_agl_readout.png"))
    print("  \u2192 preview_agl_readout.png")

    # AHRS Setup with each ORIENTATION segment highlighted.
    for orient, fname in (("forward", "preview_setup_ahrs_orient_fwd.png"),
                          ("left",    "preview_setup_ahrs_orient_left.png"),
                          ("right",   "preview_setup_ahrs_orient_right.png"),
                          ("aft",     "preview_setup_ahrs_orient_aft.png")):
        pfd.disp["ss"]["orientation"] = orient
        pfd.disp["mode"] = "ahrs_setup"
        pfd.render(surf, demo_mode=False, connected=True, data_stale=False)
        pygame.image.save(surf, os.path.join(setup_outdir, fname))
        print(f"  \u2192 {fname}")
    pfd.disp["ss"]["orientation"] = "right"

    print("\nDone.")


if __name__ == "__main__":
    main()
