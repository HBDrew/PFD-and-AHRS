"""
datasets.py — the registry the GUI renders: one entry per data product.

Each Dataset declares the inputs the pilot must pick, the builder that turns
them into a cache, the files that make up the product (for deploy/publish), and
— for nav data — where to publish it.  Adding a new product is one entry here.
"""

import glob
import os
import shutil

from . import paths
from . import builders

import navdata as nd_mod


class Input:
    """A user-picked build input surfaced as a file/dir picker (or text)."""
    def __init__(self, key, label, kind, patterns=None, optional=False, hint=""):
        self.key = key
        self.label = label
        self.kind = kind          # "dir" | "file" | "text"
        self.patterns = patterns or []
        self.optional = optional
        self.hint = hint


class Dataset:
    def __init__(self, key, label, blurb, out_subdir, build, deploy_subdir,
                 products, inputs=None, publish=None):
        self.key = key
        self.label = label
        self.blurb = blurb
        self.out_subdir = out_subdir          # workspace subfolder
        self.build_fn = build
        self.deploy_subdir = deploy_subdir    # data/<this> on the device
        self.products = products              # basenames or a glob like "*.hgt"
        self.inputs = inputs or []
        self.publish = publish                # dict | None

    # ── workspace / product helpers ────────────────────────────────────────────
    def workspace(self):
        return paths.workspace_for(self.out_subdir)

    def product_paths(self, base=None):
        base = base or self.workspace()
        out = []
        for p in self.products:
            if any(ch in p for ch in "*?["):
                out.extend(sorted(glob.glob(os.path.join(base, p))))
            else:
                fp = os.path.join(base, p)
                if os.path.exists(fp):
                    out.append(fp)
        return out

    def is_built(self):
        return bool(self.product_paths())

    def status(self):
        files = self.product_paths()
        if not files:
            return "not built"
        total = sum(os.path.getsize(f) for f in files)
        if self.key == "navdata":
            st = nd_mod.cache_stats(self.workspace())
            if st.get("present"):
                return (f"cycle {st.get('cycle') or '—'} · "
                        f"{st.get('procedures',0):,} appr · {total/1e6:.1f} MB")
        return f"{len(files)} file(s) · {total/1e6:.1f} MB"

    def build(self, inputs, log):
        return self.build_fn(inputs, self.workspace(), log)

    def deploy(self, device_data_dir, log):
        files = self.product_paths()
        if not files:
            raise ValueError(f"{self.label}: nothing built to deploy")
        dest = os.path.join(device_data_dir, self.deploy_subdir)
        os.makedirs(dest, exist_ok=True)
        for f in files:
            shutil.copy2(f, os.path.join(dest, os.path.basename(f)))
        log(f"Deployed {len(files)} file(s) → {dest}")
        return dest


# ── publish target for nav data, derived from the on-device download URL ───────
def _navdata_publish():
    url = (nd_mod.DOWNLOAD_BASE_URL or "").rstrip("/")
    # .../<owner>/<repo>/releases/download/<tag>
    try:
        parts = url.split("github.com/", 1)[1].split("/")
        owner, repo = parts[0], parts[1]
        tag = parts[-1]
    except Exception:
        owner, repo, tag = "HBDrew", "PFD-and-AHRS", "navdata"
    return {"owner": owner, "repo": repo, "tag": tag,
            "files": list(nd_mod.DOWNLOAD_FILES)}


DATASETS = [
    Dataset(
        key="navdata", label="Nav Data",
        blurb="FAA NASR + CIFP → fixes, navaids, airways, approaches, holds.",
        out_subdir="navdata", deploy_subdir="navdata",
        build=builders.build_navdata,
        products=list(nd_mod.DOWNLOAD_FILES),
        inputs=[
            Input("nasr", "NASR folder", "dir",
                  hint="unzipped NASR Subscription (FIX_BASE/NAV_BASE/AWY_SEG.csv)"),
            Input("cifp", "CIFP file (FAACIFP18)", "file",
                  patterns=[("CIFP", "FAACIFP*"), ("All files", "*")]),
            Input("cycle", "Cycle (e.g. 2406)", "text", optional=True),
        ],
        publish=_navdata_publish(),
    ),
    Dataset(
        key="airspace", label="Airspace",
        blurb="FAA GeoJSON → Class B/C/D + MOA/Restricted/Prohibited polygons.",
        out_subdir="airspaces", deploy_subdir="airspaces",
        build=builders.build_airspace, products=["airspaces.json"],
        inputs=[Input("geojson_dir", "GeoJSON folder", "dir",
                      hint="folder of FAA *.geojson (Class Airspace, SUA, TFR)")],
    ),
    Dataset(
        key="airports", label="Airports",
        blurb="OurAirports global database (downloaded automatically).",
        out_subdir="airports", deploy_subdir="airports",
        build=builders.build_airports,
        products=["airports.csv", "airports_cache.npy"],
    ),
    Dataset(
        key="obstacles", label="Obstacles",
        blurb="FAA Digital Obstacle File (downloaded automatically).",
        out_subdir="obstacles", deploy_subdir="obstacles",
        build=builders.build_obstacles,
        products=["DAILY_DOF_DAT.DAT", "dof_cache.npy"],
    ),
    Dataset(
        key="terrain", label="Terrain (SRTM)",
        blurb="Compact raw SRTM .hgt tiles for on-device TAWS / SVT.",
        out_subdir="srtm", deploy_subdir="srtm",
        build=builders.build_terrain, products=["*.hgt"],
        inputs=[Input("srtm_dir", "Raw SRTM folder", "dir",
                      hint="folder of .hgt tiles to compact")],
    ),
    Dataset(
        key="water", label="Water Tiles",
        blurb="Natural Earth water shapefile → coastline / lake tiles.",
        out_subdir="water", deploy_subdir="water",
        build=builders.build_water, products=["*.npy", "*.npz"],
        inputs=[
            Input("shapes", "Water shapefile (.shp)", "file",
                  patterns=[("Shapefile", "*.shp"), ("All files", "*")]),
            Input("bbox", "BBox  'SWlat,SWlon NElat,NElon'", "text",
                  optional=True, hint="leave blank for the whole shapefile"),
        ],
    ),
]


def by_key(key):
    for d in DATASETS:
        if d.key == key:
            return d
    return None
