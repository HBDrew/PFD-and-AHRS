#!/usr/bin/env python3
"""build_water_tiles.py – Rasterise Natural Earth ocean + lakes vectors
into per-tile binary water masks for the SVT renderer.

Input:  Natural Earth 10m physical vectors
          ne_10m_ocean.shp   (~10 MB shapefile, single global ocean polygon)
          ne_10m_lakes.shp   (~2 MB shapefile, ~1400 named lakes)
Output: One <NXXEYYY>.water file per requested tile (~180 KB each).

Usage:
    python3 tools/build_water_tiles.py \
        --shapes  natural_earth/             # dir containing the .shp files
        --out     pi4/data/water/            # destination
        --tiles   N34W112 N34W111 ...        # explicit list
    or
    python3 tools/build_water_tiles.py \
        --shapes natural_earth/ \
        --out    pi4/data/water/ \
        --bbox   N33W113 N36W110             # SW–NE corners

Requires: gdal (via apt-get install gdal-bin) for gdal_rasterize, or
          fiona+shapely+rasterio for the in-process Python path.

This script is run once on a build machine (or on the Pi after
`sudo apt-get install gdal-bin`); the resulting .water files are
small enough to ship alongside SRTM tiles."""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile

try:
    import numpy as np
except ImportError:
    print("error: numpy required", file=sys.stderr)
    sys.exit(1)

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "shared"))
from water import save_tile, WATER_TILE_RES   # noqa: E402


_TILE_RE = re.compile(r"^([NS])(\d{1,2})([EW])(\d{1,3})$")


def _parse_tile(name):
    m = _TILE_RE.match(name)
    if not m:
        raise ValueError(f"bad tile name: {name}")
    ns, lat, ew, lon = m.groups()
    lat_int = int(lat) * (1 if ns == "N" else -1)
    lon_int = int(lon) * (1 if ew == "E" else -1)
    return lat_int, lon_int


def _tile_name(lat_int, lon_int):
    ns = "N" if lat_int >= 0 else "S"
    ew = "E" if lon_int >= 0 else "W"
    return f"{ns}{abs(lat_int):02d}{ew}{abs(lon_int):03d}"


def _bbox_tiles(sw_name, ne_name):
    sw_lat, sw_lon = _parse_tile(sw_name)
    ne_lat, ne_lon = _parse_tile(ne_name)
    out = []
    for la in range(min(sw_lat, ne_lat), max(sw_lat, ne_lat) + 1):
        for lo in range(min(sw_lon, ne_lon), max(sw_lon, ne_lon) + 1):
            out.append(_tile_name(la, lo))
    return out


def _rasterise_with_gdal(shp_paths, lat_int, lon_int, res):
    """Rasterise the union of one or more shapefiles into a (res, res)
    uint8 0/1 mask covering the 1°×1° tile at (lat_int, lon_int)."""
    tmpdir = tempfile.mkdtemp(prefix="water_")
    try:
        out_tif = os.path.join(tmpdir, "mask.tif")
        # Initialise the output raster with zeros (land).  -ts fixes the size
        # to res×res samples spanning exactly one degree, edge-aligned.
        cmd = [
            "gdal_rasterize",
            "-burn", "1",
            "-of", "GTiff",
            "-ot", "Byte",
            "-init", "0",
            "-ts", str(res), str(res),
            "-te", str(lon_int), str(lat_int),
                   str(lon_int + 1), str(lat_int + 1),
            "-co", "COMPRESS=NONE",
        ]
        for shp in shp_paths:
            cmd += [shp]
        cmd += [out_tif]
        # gdal_rasterize wants a single source then dest; loop to OR-merge.
        first = True
        for shp in shp_paths:
            cmd_one = [
                "gdal_rasterize",
                "-burn", "1",
                "-of", "GTiff",
                "-ot", "Byte",
                "-ts", str(res), str(res),
                "-te", str(lon_int), str(lat_int),
                       str(lon_int + 1), str(lat_int + 1),
            ]
            if first:
                cmd_one += ["-init", "0", "-co", "COMPRESS=NONE",
                            shp, out_tif]
                first = False
            else:
                cmd_one += [shp, out_tif]
            subprocess.run(cmd_one, check=True,
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)

        # Read the GeoTIFF back as a flat byte array.  We avoid a rasterio
        # dep by using gdal_translate to a headerless binary.
        out_bin = os.path.join(tmpdir, "mask.bin")
        subprocess.run(
            ["gdal_translate", "-of", "ENVI", "-ot", "Byte",
             out_tif, out_bin],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        with open(out_bin, "rb") as f:
            data = f.read()
        if len(data) != res * res:
            raise RuntimeError(f"unexpected raster size: {len(data)}")
        mask = np.frombuffer(data, dtype=np.uint8).reshape(res, res)
        # gdal_rasterize fills row 0 = top (north) which matches SRTM's row
        # convention.  Threshold to {0,1}.
        return (mask > 0).astype(np.uint8)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shapes", required=True,
                    help="directory containing ne_10m_ocean.shp + "
                         "ne_10m_lakes.shp")
    ap.add_argument("--out", required=True,
                    help="output directory for .water files")
    ap.add_argument("--tiles", nargs="*",
                    help="explicit tile list, e.g. N34W112 N34W111")
    ap.add_argument("--bbox", nargs=2, metavar=("SW", "NE"),
                    help="bounding-box tile pair, e.g. N33W113 N36W110")
    ap.add_argument("--res", type=int, default=WATER_TILE_RES,
                    help=f"samples per side (default {WATER_TILE_RES})")
    ap.add_argument("--force", action="store_true",
                    help="overwrite existing .water files")
    args = ap.parse_args()

    if not (args.tiles or args.bbox):
        ap.error("specify --tiles or --bbox")

    if shutil.which("gdal_rasterize") is None:
        sys.exit("error: gdal_rasterize not found.  Install with: "
                 "sudo apt-get install gdal-bin")

    shp_paths = []
    for name in ("ne_10m_ocean.shp", "ne_10m_lakes.shp"):
        p = os.path.join(args.shapes, name)
        if os.path.exists(p):
            shp_paths.append(p)
        else:
            print(f"  warn: missing {name}, skipping", file=sys.stderr)
    if not shp_paths:
        sys.exit("error: no input shapefiles found in " + args.shapes)

    if args.bbox:
        tiles = _bbox_tiles(*args.bbox)
    else:
        tiles = args.tiles

    os.makedirs(args.out, exist_ok=True)

    print(f"Rasterising {len(shp_paths)} shapefiles into "
          f"{len(tiles)} tiles → {args.out}")
    n_done = 0
    n_skip = 0
    for tile in tiles:
        try:
            lat_int, lon_int = _parse_tile(tile)
        except ValueError as e:
            print(f"  skip {tile}: {e}")
            continue
        out_path = os.path.join(args.out, tile + ".water")
        if os.path.exists(out_path) and not args.force:
            print(f"  {tile}: already present (use --force to overwrite)")
            n_skip += 1
            continue
        mask = _rasterise_with_gdal(shp_paths, lat_int, lon_int, args.res)
        save_tile(out_path, mask)
        sz_kb = os.path.getsize(out_path) / 1024
        water_pct = 100.0 * mask.mean()
        print(f"  {tile}: {water_pct:5.1f}% water  ({sz_kb:.0f} KB)")
        n_done += 1

    print(f"\nDone — {n_done} tiles written, {n_skip} already present.")


if __name__ == "__main__":
    main()
