#!/usr/bin/env python3
"""
render_preview_gl.py – Generate Pi 4 PFD preview PNGs using the OpenGL SVT.

Standalone preview script that combines the OpenGL SVT renderer with the
pygame UI overlays, without requiring a display server.  Useful for:
  - Testing the OpenGL SVT in environments without KMS
  - Generating preview screenshots showing the full PFD with real terrain
  - Iterating on shaders/colors offline

Usage:
    python3 pi4/render_preview_gl.py [output_dir]
"""
import os
import sys
import argparse

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, '..', 'shared'))
sys.path.insert(0, _HERE)

# Use SDL dummy driver — no display server required for surface ops
os.environ['SDL_VIDEODRIVER'] = 'dummy'
import pygame
pygame.init()
# Ensure mouse subsystem is initialized for dummy driver
try:
    pygame.mouse.set_visible(False)
except pygame.error:
    pass

from svt_renderer_gl import render_svt_gl, is_available

# Scenes for full preview set
SCENES = [
    ("svt_gl_sedona_level",      34.8697, -111.7610, 8500, 133,  2, 0,
     "Sedona, level cruise"),
    ("svt_gl_sedona_climb_turn", 34.8697, -111.7610, 7800, 145,  6, -18,
     "Sedona, climbing left turn"),
    ("svt_gl_sedona_approach",   34.8697, -111.7610, 5800, 200, -3, 0,
     "Sedona, approach"),
    ("svt_gl_low_altitude",      34.8697, -111.7610, 4500, 133,  0, 10,
     "Sedona, low altitude with right bank"),
    ("svt_gl_high_altitude",     34.8697, -111.7610, 12000, 133, 0, 0,
     "Sedona, high altitude"),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("outdir", nargs="?",
                        default=os.path.join(_HERE, "previews"))
    parser.add_argument("--width",  type=int, default=775)
    parser.add_argument("--height", type=int, default=523)
    args = parser.parse_args()

    if not is_available():
        print("ERROR: OpenGL SVT renderer not available.")
        sys.exit(1)

    srtm_dir = os.path.join(_HERE, "data", "srtm")
    if not os.path.isdir(srtm_dir):
        print(f"WARNING: SRTM directory missing: {srtm_dir}")

    os.makedirs(args.outdir, exist_ok=True)
    print(f"Rendering OpenGL SVT scenes to {args.outdir}")
    print(f"Resolution: {args.width}×{args.height}")
    print()

    for suffix, lat, lon, alt, hdg, pitch, roll, label in SCENES:
        out = os.path.join(args.outdir, f"{suffix}.png")
        surf = render_svt_gl(srtm_dir, args.width, args.height,
                             pitch, roll, hdg, alt, lat, lon)
        if surf is None:
            print(f"  ✗ {suffix}: render failed")
            continue
        pygame.image.save(surf, out)
        print(f"  → {os.path.basename(out)}  — {label}")

    print("\nDone.")


if __name__ == "__main__":
    main()
