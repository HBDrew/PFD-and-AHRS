#!/usr/bin/env python3
"""Build the parallel SRTM3 tint cache (pi4/data/srtm3).

Decimates every full-resolution SRTM1 tile in pi4/data/srtm down to SRTM3
(every 3rd sample) and writes it to pi4/data/srtm3, so the moving-map tint
reads cheap ~2.8 MB tiles instead of decimating 26 MB SRTM1 files on the fly.
Your SRTM1 set is left untouched (the PFD's 3D SVT scene still uses it).

Run this once after downloading/refreshing terrain (it's idempotent — re-run
to pick up newly added tiles; already-built tiles are skipped):

    python3 pi4/build_srtm3.py

The on-demand cache still works without this; the batch just front-loads the
work so the first big pan at wide zoom doesn't hitch.
"""
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "shared"))

import terrain  # noqa: E402

SRTM_DIR = os.path.join(_HERE, "data", "srtm")
CACHE_DIR = os.path.join(_HERE, "data", "srtm3")


def main():
    if not os.path.isdir(SRTM_DIR):
        print(f"No SRTM dir at {SRTM_DIR} — nothing to do.")
        return
    n_src = len([f for f in os.listdir(SRTM_DIR) if f.endswith(".hgt")])
    print(f"Building SRTM3 cache from {n_src} tiles in {SRTM_DIR}")
    print(f"  → {CACHE_DIR}")
    t0 = time.time()

    def prog(done, total, name):
        if done % 5 == 0 or done == total:
            print(f"\r  {done}/{total}  {name}      ", end="", flush=True)

    built, skipped, errors = terrain.build_srtm3_cache(
        SRTM_DIR, CACHE_DIR, progress=prog)
    dt = time.time() - t0
    print(f"\nDone in {dt:.1f}s — built {built}, skipped {skipped}, "
          f"errors {errors}")
    if built:
        mb = built * terrain.SRTM3_SAMPLES * terrain.SRTM3_SAMPLES * 2 / 1e6
        print(f"  ~{mb:.0f} MB of SRTM3 tiles written")


if __name__ == "__main__":
    main()
