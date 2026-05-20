#!/usr/bin/env python3
"""
compact_srtm.py — Decimate SRTM1 .hgt tiles to SRTM3 resolution in place.

The Mapzen / AWS skadi downloader pulls SRTM1 tiles (3601² @ 25 MB
each unzipped).  On the Pi Zero MFD we never use SRTM1's detail —
the tint only samples ~2300 points across the inset, and the per-tile
RAM cost (52 MB as float32) is what was OOM-ing wider zooms before
the load-time decimation was added.

The runtime fix decimates on every cache miss, which still costs a
~25 MB int16 read peak per tile.  Running this script once over the
srtm dir converts every SRTM1 .hgt file to SRTM3 (1201²) in place,
shrinking disk usage ~8.7× (3.7 GB → ~430 MB for CONUS) and making
load_tile's fast path the only one needed afterwards.

Decimation is nearest-neighbour every-third-sample
(3601 → 1201 since (3601-1)//3 + 1 = 1201).  Lossy by design; the
tint sampler at 48² grid spacing was already coarser than SRTM3's
90 m resolution.

Atomic write: each tile is rewritten to <name>.hgt.tmp then renamed
over the original, so an interrupted run can't leave a half-written
file.  Already-SRTM3 tiles (1201² files) are skipped.

Usage:
    python3 tools/compact_srtm.py --srtm-dir ~/PFD-and-AHRS/pi_zero/data/srtm
    python3 tools/compact_srtm.py --srtm-dir <dir> --dry-run

Run after a download completes.  pi4's SVT keeps SRTM1 by pointing
its SRTM_DIR somewhere else (or running with --srtm-dir on a copy).
"""

import argparse
import os
import sys
import time


SRTM1_SAMPLES = 3601
SRTM3_SAMPLES = 1201
SRTM1_BYTES = SRTM1_SAMPLES * SRTM1_SAMPLES * 2
SRTM3_BYTES = SRTM3_SAMPLES * SRTM3_SAMPLES * 2


def _is_srtm1(path):
    return os.path.getsize(path) == SRTM1_BYTES


def _is_srtm3(path):
    return os.path.getsize(path) == SRTM3_BYTES


def _decimate(path, dry_run=False):
    """Read an SRTM1 .hgt, decimate to SRTM3, atomic-rewrite in place.
    Returns the number of bytes reclaimed."""
    try:
        import numpy as np
    except ImportError:
        print("ERROR: numpy required.  pip install numpy", file=sys.stderr)
        sys.exit(2)

    saved = SRTM1_BYTES - SRTM3_BYTES
    if dry_run:
        return saved

    raw = np.fromfile(path, dtype='>i2').reshape((SRTM1_SAMPLES, SRTM1_SAMPLES))
    small = raw[::3, ::3].copy()
    del raw

    tmp_path = path + ".tmp"
    small.astype('>i2').tofile(tmp_path)
    if os.path.getsize(tmp_path) != SRTM3_BYTES:
        # Sanity check before destroying the original.
        os.remove(tmp_path)
        raise RuntimeError(f"size mismatch on {tmp_path}: "
                           f"{os.path.getsize(tmp_path)} != {SRTM3_BYTES}")
    os.replace(tmp_path, path)
    return saved


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--srtm-dir", required=True,
                    help="Directory containing .hgt files")
    ap.add_argument("--dry-run", action="store_true",
                    help="Report what would change without writing.")
    args = ap.parse_args()

    if not os.path.isdir(args.srtm_dir):
        print(f"ERROR: {args.srtm_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    entries = sorted(f for f in os.listdir(args.srtm_dir) if f.endswith(".hgt"))
    if not entries:
        print(f"No .hgt files in {args.srtm_dir}")
        return

    n_srtm1 = n_srtm3 = n_other = n_err = 0
    bytes_saved = 0
    t0 = time.monotonic()
    last_print = t0

    for i, name in enumerate(entries, start=1):
        path = os.path.join(args.srtm_dir, name)
        size = os.path.getsize(path)
        if size == SRTM3_BYTES:
            n_srtm3 += 1
            continue
        if size != SRTM1_BYTES:
            print(f"  skip {name} (unexpected size {size})")
            n_other += 1
            continue
        try:
            bytes_saved += _decimate(path, dry_run=args.dry_run)
            n_srtm1 += 1
        except Exception as exc:
            print(f"  ERROR {name}: {exc}")
            n_err += 1
            continue

        now = time.monotonic()
        if now - last_print >= 1.0 or i == len(entries):
            elapsed = now - t0
            rate = i / max(elapsed, 0.001)
            eta = (len(entries) - i) / rate if rate > 0 else 0
            tag = "(dry-run) " if args.dry_run else ""
            print(f"  {tag}{i}/{len(entries)}  {name}  "
                  f"{bytes_saved/1e9:.2f} GB saved  "
                  f"ETA {int(eta)}s")
            last_print = now

    print()
    print(f"SRTM1 processed: {n_srtm1}")
    print(f"SRTM3 skipped:   {n_srtm3}")
    print(f"Other / wrong:   {n_other}")
    print(f"Errors:          {n_err}")
    print(f"Disk reclaimed:  {bytes_saved/1e9:.2f} GB"
          + ("  (dry-run; no files changed)" if args.dry_run else ""))


if __name__ == "__main__":
    main()
