#!/usr/bin/env python3
"""
test_svt_gl.py – Standalone test/preview generator for the OpenGL SVT renderer.

Renders a series of test scenes around Sedona AZ and saves them as PNGs.
Useful for iterating on the GL renderer without running the full PFD.

Usage:
    python3 pi4/test_svt_gl.py [output_dir]

Default output: pi4/previews/svt_gl_*.png
"""
import os
import sys
import argparse

# Add shared modules to path
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, '..', 'shared'))
sys.path.insert(0, _HERE)

import pygame
pygame.init()

from svt_renderer_gl import render_svt_gl, is_available

SRTM_DIR = os.path.join(_HERE, "data", "srtm")

# Scenes: (filename_suffix, lat, lon, alt_ft, hdg, pitch, roll, label)
SCENES = [
    ("level_north",        34.8697, -111.7610,  8500,   0,   0,   0,
     "Sedona, level cruise, heading north"),
    ("level_south",        34.8697, -111.7610,  8500, 180,   0,   0,
     "Sedona, level cruise, heading south"),
    ("level_east",         34.8697, -111.7610,  8500,  90,   0,   0,
     "Sedona, level cruise, heading east"),
    ("approach",           34.8697, -111.7610,  5800, 200,  -3,   0,
     "Sedona, approach, descending"),
    ("low_altitude",       34.8697, -111.7610,  4500, 133,   0,  10,
     "Sedona, low altitude with right bank"),
    ("high_altitude",      34.8697, -111.7610, 12000, 133,   0,   0,
     "Sedona, high cruise"),
    ("climb_left",         34.8697, -111.7610,  6500, 100,   8, -15,
     "Sedona, climbing left turn"),
    ("flagstaff_view",     35.1983, -111.6513,  9000, 270,   0,   0,
     "Flagstaff area, looking west"),
]


def main():
    parser = argparse.ArgumentParser(description="OpenGL SVT preview generator")
    parser.add_argument("outdir", nargs="?",
                        default=os.path.join(_HERE, "previews"),
                        help="Output directory for preview PNGs")
    parser.add_argument("--width",  type=int, default=775,
                        help="Render width (default: AI_W at 1024×600)")
    parser.add_argument("--height", type=int, default=523,
                        help="Render height (default: AI_H at 1024×600)")
    args = parser.parse_args()

    if not is_available():
        print("ERROR: OpenGL SVT renderer not available.")
        print("       Install:  pip3 install moderngl numpy")
        print("       And libs: apt install libegl1-mesa-dev libgles2-mesa-dev")
        sys.exit(1)

    if not os.path.isdir(SRTM_DIR):
        print(f"WARNING: SRTM directory not found: {SRTM_DIR}")
        print("         Run: bash fetch_sedona_tiles.sh")
        # Continue anyway — terrain will be all zeros (sea level)

    os.makedirs(args.outdir, exist_ok=True)

    print(f"Rendering {len(SCENES)} OpenGL SVT preview scenes to {args.outdir}")
    print(f"Resolution: {args.width}×{args.height}")
    print()

    for suffix, lat, lon, alt, hdg, pitch, roll, label in SCENES:
        out = os.path.join(args.outdir, f"svt_gl_{suffix}.png")
        surf = render_svt_gl(SRTM_DIR, args.width, args.height,
                             pitch, roll, hdg, alt, lat, lon)
        if surf is None:
            print(f"  ✗ {suffix}: render failed")
            continue
        pygame.image.save(surf, out)
        size_kb = os.path.getsize(out) // 1024
        print(f"  → {os.path.basename(out)}  ({size_kb} KB)  — {label}")

    print(f"\nDone. View with any image viewer.")


if __name__ == "__main__":
    main()
