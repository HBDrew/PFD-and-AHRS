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

# Same scenes as the regular --screenshots batch mode
SCENES = [
    ("preview_sedona_level",       0,   2, 133, 8500, 115,    0,   0),
    ("preview_sedona_climb_turn", -18,  6, 145, 7800, 95,   500, 0.12),
    ("preview_sedona_approach",    0,  -3, 200, 5800, 90,  -700,   0),
    ("preview_low_altitude",      10,   0, 133, 4500, 95,     0,   0),
    ("preview_high_altitude",      0,   0, 133, 12000, 115,   0,   0),
    ("preview_climb_left",       -15,   8, 100, 6500, 95,   500, -0.10),
]


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

    surf = pygame.Surface((DISPLAY_W, DISPLAY_H))

    print(f"Rendering full-PFD previews with OpenGL SVT to {args.outdir}")
    print(f"Resolution: {DISPLAY_W}×{DISPLAY_H}")
    print(f"SVT_RENDERER: {pfd.SVT_RENDERER}  GL_AVAILABLE: {pfd._SVT_GL_AVAILABLE}")
    print()

    for name, roll, pitch, hdg, alt, speed, vspeed, ay in SCENES:
        seed_state(roll, pitch, hdg, alt, speed, vspeed, ay)
        pfd.smooth_state()
        pfd.render(surf, demo_mode=False, connected=True, data_stale=False)
        outpath = os.path.join(args.outdir, f"{name}.png")
        pygame.image.save(surf, outpath)
        print(f"  → {os.path.basename(outpath)}")

    print("\nDone.")


if __name__ == "__main__":
    main()
