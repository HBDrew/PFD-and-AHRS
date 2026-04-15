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

# Use SDL dummy driver before pygame import — no display server needed
os.environ['SDL_VIDEODRIVER'] = 'dummy'
import pygame
pygame.init()
# Mouse subsystem may not init under dummy driver; ignore failure
try:
    pygame.mouse.set_visible(False)
except pygame.error:
    pass

# Now import pfd module — it'll see SVT_RENDERER from config
import pfd

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
]


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

    surf = pygame.Surface((DISPLAY_W, DISPLAY_H))

    print(f"Rendering full-PFD previews with OpenGL SVT to {args.outdir}")
    print(f"Resolution: {DISPLAY_W}×{DISPLAY_H}")
    print(f"SVT_RENDERER: {pfd.SVT_RENDERER}  GL_AVAILABLE: {pfd._SVT_GL_AVAILABLE}")
    print()

    for scene in SCENES:
        name, roll, pitch, hdg, alt, speed, vspeed, ay = scene[:8]
        lat = scene[8] if len(scene) > 8 else DEMO_LAT
        lon = scene[9] if len(scene) > 9 else DEMO_LON
        seed_state(roll, pitch, hdg, alt, speed, vspeed, ay, lat, lon)
        pfd.smooth_state()
        pfd.render(surf, demo_mode=False, connected=True, data_stale=False)
        outpath = os.path.join(args.outdir, f"{name}.png")
        pygame.image.save(surf, outpath)
        print(f"  → {os.path.basename(outpath)}")

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

    print("\nDone.")


if __name__ == "__main__":
    main()
