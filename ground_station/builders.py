"""
builders.py — build each data product into a workspace dir.

Every builder has the same shape:

    build(inputs: dict, out_dir: str, log) -> dict   # stats, or raises

`inputs` carries the user-picked files/dirs the dataset declares (see
datasets.py); `out_dir` is the workspace folder to write the cache into; `log`
streams human-readable progress.  Builders reuse the existing tools/shared code
in-process so the whole thing packages into one binary.
"""

import glob
import os
import shutil
import urllib.request
import zipfile

from . import paths  # noqa: F401  (ensures repo tools/shared are on sys.path)

import build_navdata_us
import build_airspaces_us
import compact_srtm
import build_water_tiles
import airports as apt_mod
import obstacles as obs_mod
import navdata as nd_mod
from .runners import run_main


# ── helpers ────────────────────────────────────────────────────────────────────
def _download(url, dest, log, label=None):
    label = label or os.path.basename(dest)
    log(f"Downloading {label} …")
    req = urllib.request.Request(url, headers={"User-Agent": "PFD-GroundStation/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        got = 0
        with open(dest + ".tmp", "wb") as out:
            while True:
                chunk = resp.read(262144)
                if not chunk:
                    break
                out.write(chunk)
                got += len(chunk)
                if total:
                    log(f"  {label}: {got*100//total}%  "
                        f"({got//1024} / {total//1024} KB)")
                else:
                    log(f"  {label}: {got//1024} KB")
    os.replace(dest + ".tmp", dest)
    log(f"  {label}: done ({os.path.getsize(dest)//1024} KB)")


# ── nav data (FAA NASR + CIFP) ─────────────────────────────────────────────────
def build_navdata(inputs, out_dir, log):
    nasr = inputs.get("nasr")
    cifp = inputs.get("cifp")
    if not nasr and not cifp:
        raise ValueError("Pick a NASR folder and/or a CIFP (FAACIFP18) file")
    argv = ["--out", out_dir]
    if nasr:
        argv += ["--nasr", nasr]
    if cifp:
        argv += ["--cifp", cifp]
    cycle = inputs.get("cycle")
    if cycle:
        argv += ["--cycle", cycle]
    if not run_main(build_navdata_us, argv, log):
        raise RuntimeError("nav-data build failed (see log)")
    return nd_mod.cache_stats(out_dir)


# ── airspace (FAA GeoJSON) ─────────────────────────────────────────────────────
def build_airspace(inputs, out_dir, log):
    src = inputs.get("geojson_dir")
    if not src or not os.path.isdir(src):
        raise ValueError("Pick a folder containing the FAA *.geojson files")
    files = glob.glob(os.path.join(src, "*.geojson"))
    if not files:
        raise ValueError(f"No *.geojson files in {src}")
    # Stage the geojson into the workspace so build_from_dir writes the result
    # alongside them without touching the user's source folder.
    for f in files:
        dst = os.path.join(out_dir, os.path.basename(f))
        if os.path.abspath(f) != os.path.abspath(dst):
            shutil.copy2(f, dst)
    log(f"Building airspaces.json from {len(files)} GeoJSON file(s) …")
    stats = build_airspaces_us.build_from_dir(out_dir, source_note="ground-station")
    log(f"  {stats.get('records', 0)} polygons "
        f"(B={stats.get('B',0)} C={stats.get('C',0)} D={stats.get('D',0)} "
        f"MOA={stats.get('MOA',0)} R={stats.get('R',0)} P={stats.get('P',0)})")
    # Leave only the product in the workspace; drop the staged source copies.
    for f in files:
        staged = os.path.join(out_dir, os.path.basename(f))
        if os.path.exists(staged):
            os.remove(staged)
    return stats


# ── airports (OurAirports CSV) ─────────────────────────────────────────────────
def build_airports(inputs, out_dir, log):
    csv_path   = os.path.join(out_dir, apt_mod.CSV_FILENAME)
    cache_path = os.path.join(out_dir, apt_mod.CACHE_FILENAME)
    _download(apt_mod.AIRPORTS_CSV_URL, csv_path, log, "airports.csv")
    if os.path.exists(cache_path):
        os.remove(cache_path)
    log("Parsing airport records …")
    arr = apt_mod.load(out_dir)              # builds airports_cache.npy
    n = 0 if arr is None else len(arr)
    log(f"  {n:,} airports")
    return {"records": n}


# ── obstacles (FAA Digital Obstacle File) ──────────────────────────────────────
def build_obstacles(inputs, out_dir, log):
    zip_path   = os.path.join(out_dir, "DAILY_DOF_DAT.ZIP")
    dat_path   = os.path.join(out_dir, obs_mod.DOF_FILENAME)
    cache_path = os.path.join(out_dir, obs_mod.CACHE_FILENAME)
    _download(obs_mod.DOF_ZIP_URL, zip_path, log, "DAILY_DOF_DAT.ZIP")
    log("Unzipping …")
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(out_dir)
    os.remove(zip_path)
    if os.path.exists(cache_path):
        os.remove(cache_path)
    if not os.path.exists(dat_path):
        # The FAA ZIP sometimes nests the .DAT or uses a slightly different
        # name; find the first .DAT and normalise it.
        cands = glob.glob(os.path.join(out_dir, "*.DAT")) + \
                glob.glob(os.path.join(out_dir, "*.dat"))
        if cands:
            shutil.move(cands[0], dat_path)
    log("Parsing obstacle records …")
    arr = obs_mod.load(out_dir)              # builds dof_cache.npy
    n = 0 if arr is None else len(arr)
    log(f"  {n:,} obstacles")
    return {"records": n}


# ── terrain (SRTM compaction) ──────────────────────────────────────────────────
def build_terrain(inputs, out_dir, log):
    srtm = inputs.get("srtm_dir")
    if not srtm or not os.path.isdir(srtm):
        raise ValueError("Pick a folder of raw SRTM .hgt tiles to compact")
    argv = ["--srtm-dir", srtm, "--output-dir", out_dir]
    if not run_main(compact_srtm, argv, log):
        raise RuntimeError("SRTM compaction failed (see log)")
    n = len(glob.glob(os.path.join(out_dir, "*.hgt")))
    return {"records": n}


# ── water tiles (Natural Earth shapefile) ──────────────────────────────────────
def build_water(inputs, out_dir, log):
    shapes = inputs.get("shapes")
    bbox   = inputs.get("bbox")              # "SW NE" e.g. "33,-113 37,-110"
    if not shapes or not os.path.isfile(shapes):
        raise ValueError("Pick a Natural Earth water shapefile (.shp)")
    argv = ["--shapes", shapes, "--out", out_dir]
    if bbox:
        parts = bbox.split()
        if len(parts) == 2:
            argv += ["--bbox", parts[0], parts[1]]
    if not run_main(build_water_tiles, argv, log):
        raise RuntimeError("water-tile build failed (see log)")
    n = len(glob.glob(os.path.join(out_dir, "*.npy")))
    return {"records": n}
