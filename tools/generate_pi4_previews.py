#!/usr/bin/env python3
"""
Generate 1024×600 preview images for Pi 4 by scaling the 640×480 originals.

The 640×480 previews already show blue-over-brown (plain horizon) backgrounds,
which is correct for the initial Pi 4 previews.  Once the OpenGL SVT renderer
is built, these will be replaced with true SVT terrain renders.

Usage:
    python3 tools/generate_pi4_previews.py

Reads from:  tools/preview_*.png  +  pfd_preview.png
Writes to:   pi4/previews/
"""

import os
import sys

try:
    from PIL import Image
except ImportError:
    print("ERROR: Pillow is required.  Install with:  pip3 install Pillow")
    sys.exit(1)

SRC_DIR   = os.path.join(os.path.dirname(__file__), "..", "tools")
EXTRA_SRC = os.path.join(os.path.dirname(__file__), "..", "pfd_preview.png")
OUT_DIR   = os.path.join(os.path.dirname(__file__), "..", "pi4", "previews")
TARGET_W  = 1024
TARGET_H  = 600

os.makedirs(OUT_DIR, exist_ok=True)

count = 0
for fname in sorted(os.listdir(SRC_DIR)):
    if not fname.endswith(".png"):
        continue
    src_path = os.path.join(SRC_DIR, fname)
    dst_path = os.path.join(OUT_DIR, fname)
    img = Image.open(src_path)
    scaled = img.resize((TARGET_W, TARGET_H), Image.LANCZOS)
    scaled.save(dst_path)
    count += 1
    print(f"  {fname}  640×480 → {TARGET_W}×{TARGET_H}")

# Also scale the root pfd_preview.png
if os.path.exists(EXTRA_SRC):
    img = Image.open(EXTRA_SRC)
    scaled = img.resize((TARGET_W, TARGET_H), Image.LANCZOS)
    scaled.save(os.path.join(OUT_DIR, "pfd_preview.png"))
    count += 1
    print(f"  pfd_preview.png  → {TARGET_W}×{TARGET_H}")

print(f"\nDone — {count} previews scaled to {TARGET_W}×{TARGET_H} in {OUT_DIR}")
