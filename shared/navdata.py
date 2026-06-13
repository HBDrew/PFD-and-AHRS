"""
navdata.py – IFR nav-data runtime: fixes, navaids, airways, procedures, holds.

US-only foundation for the IFR flight-plan features (approaches, airways on the
map, holds).  Mirrors shared/airports.py: a build step (tools/build_navdata_us
.py) converts the FAA 28-day data into a compact on-disk cache, and this module
loads it and answers spatial / by-ident queries cheaply at runtime.  Shared by
pi4 + pi_zero (+ iPhone later).

Source data (free, US-only, 28-day cycle):
  • FAA NASR  → named FIXES (intersections), VOR/NDB/DME NAVAIDS, and Victor/Jet
    AIRWAYS as ordered fix sequences.
  • FAA CIFP (ARINC-424) → PROCEDURES (SID/STAR/approach incl. transitions +
    final approach fix + missed-approach legs) and HOLDS, keyed by airport.

On-disk cache layout (under data/navdata/, written by the build tool):
  navdata_fixes.npy    structured array, sorted by lat (for np.searchsorted)
                         dtype: ident U5, lat f4, lon f4
  navdata_navaids.npy  structured array, sorted by lat
                         dtype: ident U4, ntype U4, lat f4, lon f4, freq f4,
                                name U32
  navdata.json         the procedural data + cycle stamp:
    {
      "cycle":   "2406",                 # 28-day cycle (stale-badge stamp)
      "airways": { "V16": ["DRK","FLG",…], … },        # ordered fix idents
      "holds":   { "DRK": {"course":270,"turn":"R","leg_nm":4,"leg_min":0}, … },
      "procedures": {
        "KFLG": {
          "RNAV (GPS) RWY 03": {
            "type": "RNAV",
            "transitions": { "BANYO": [<leg>, …], … },  # IAF → IF
            "final":   [<leg>, …],                       # IF → FAF → MAP
            "missed":  [<leg>, …]
          }, …
        }, …
      }
    }
  Each <leg> is:
    {"fix":"FAFXX", "leg_type":"TF", "course":30.0|null,
     "alt_ft":2000|null, "alt_type":"AT"|"AB"|"BL"|null,
     "lat":34.1|null, "lon":-111.7|null}   # lat/lon resolved at build time

Everything degrades gracefully: a missing cache → load() returns None and the
UI shows a "NO NAVDATA" badge (callers check available()); a present-but-partial
cache (e.g. fixes but no procedures) still answers the queries it can.
"""

import datetime
import json
import math
import os

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:                       # pragma: no cover
    np = None
    HAS_NUMPY = False

FIXES_FILE   = "navdata_fixes.npy"
NAVAIDS_FILE = "navdata_navaids.npy"
JSON_FILE    = "navdata.json"

# Files the device fetches for a full cache, in download order.
DOWNLOAD_FILES = (FIXES_FILE, NAVAIDS_FILE, JSON_FILE)

# The nav-data cache is built off-aircraft by tools/build_navdata_us.py (the
# raw FAA NASR + CIFP files are far too big to parse on a Pi), then published
# as assets on a GitHub release so the device can pull them on tap.
#
# Default host: a FIXED release tag ("navdata") on this repo — re-upload the
# three assets to that tag each 28-day cycle (tools/publish_navdata.sh) and the
# URL below never changes.  A fixed tag is used (not /releases/latest/...) so a
# future app-version release can't steal "latest" and 404 the nav-data fetch.
#
# The DOWNLOAD button is live whenever this is set; until the first release is
# published, a tap simply reports the host's 404.  Point it elsewhere (S3, a
# personal server, …) in config_local.py for a custom host:
#     import navdata; navdata.DOWNLOAD_BASE_URL = "https://you.example/navdata/"
# Set to "" to disable the button and copy the cache to data/navdata/ by hand.
DOWNLOAD_BASE_URL = \
    "https://github.com/HBDrew/PFD-and-AHRS/releases/download/navdata/"

# Stale after this many days (one 28-day cycle + a few days' grace).
EXPIRY_DAYS = 32

_FIX_DTYPE = [("ident", "U5"), ("lat", "f4"), ("lon", "f4")]
_NAV_DTYPE = [("ident", "U4"), ("ntype", "U4"), ("lat", "f4"), ("lon", "f4"),
              ("freq", "f4"), ("name", "U32")]

_NM_PER_DEG_LAT = 60.0


class NavData:
    """Loaded nav-data: spatial fix/navaid arrays + procedural dicts.  Cheap to
    hold; query via the methods below.  Any component may be empty/None."""

    __slots__ = ("fixes", "navaids", "airways", "holds", "procedures", "cycle")

    def __init__(self, fixes=None, navaids=None, airways=None, holds=None,
                 procedures=None, cycle=""):
        self.fixes      = fixes
        self.navaids    = navaids
        self.airways    = airways or {}
        self.holds      = holds or {}
        self.procedures = procedures or {}
        self.cycle      = cycle

    # ── availability ──────────────────────────────────────────────────────────
    def has_fixes(self):
        return self.fixes is not None and len(self.fixes) > 0

    def has_navaids(self):
        return self.navaids is not None and len(self.navaids) > 0

    def has_procedures(self):
        return bool(self.procedures)

    # ── by-ident lookups ──────────────────────────────────────────────────────
    def fix(self, ident):
        """(ident, lat, lon) for a named fix, or None."""
        return _exact(self.fixes, ident)

    def navaid(self, ident):
        """(ident, ntype, lat, lon, freq, name) for a VOR/NDB/DME, or None."""
        if not HAS_NUMPY or self.navaids is None or not ident:
            return None
        rows = self.navaids[self.navaids["ident"] == ident.strip().upper()]
        if len(rows) == 0:
            return None
        r = rows[0]
        return (str(r["ident"]), str(r["ntype"]), float(r["lat"]),
                float(r["lon"]), float(r["freq"]), str(r["name"]))

    def waypoint(self, ident):
        """Resolve any waypoint ident to (ident, lat, lon) — a fix first, then a
        navaid (VORs/NDBs are valid flight-plan/airway points too).  None if
        unknown."""
        f = self.fix(ident)
        if f is not None:
            return f
        nv = self.navaid(ident)
        if nv is not None:
            return (nv[0], nv[2], nv[3])
        return None

    # ── airways ──────────────────────────────────────────────────────────────
    def airway(self, ident):
        """Ordered list of (ident, lat, lon) for an airway (e.g. 'V16'), or []
        when unknown.  Points that don't resolve to a fix/navaid are dropped."""
        seq = self.airways.get((ident or "").strip().upper())
        if not seq:
            return []
        out = []
        for fid in seq:
            wp = self.waypoint(fid)
            if wp is not None:
                out.append(wp)
        return out

    def airway_between(self, ident, from_fix, to_fix):
        """The (ident, lat, lon) slice of an airway between two fixes inclusive,
        in the correct direction, or [] if either endpoint isn't on it."""
        seq = self.airways.get((ident or "").strip().upper()) or []
        a = (from_fix or "").strip().upper()
        b = (to_fix or "").strip().upper()
        if a not in seq or b not in seq:
            return []
        i, j = seq.index(a), seq.index(b)
        leg = seq[i:j + 1] if i <= j else list(reversed(seq[j:i + 1]))
        return [wp for fid in leg if (wp := self.waypoint(fid)) is not None]

    # ── procedures ────────────────────────────────────────────────────────────
    def procedures_for(self, airport):
        """Procedure idents available at an airport, e.g.
        ['RNAV (GPS) RWY 03', 'VOR RWY 21', …]  (empty if none/unknown)."""
        return sorted((self.procedures.get((airport or "").strip().upper()) or {}).keys())

    def procedure(self, airport, ident):
        """The procedure dict ({type, transitions, final, missed}) for an
        airport + procedure ident, or None."""
        return (self.procedures.get((airport or "").strip().upper()) or {}).get(ident)

    # ── holds ─────────────────────────────────────────────────────────────────
    def hold(self, fix_ident):
        """Published holding pattern at a fix → {course, turn, leg_nm, leg_min}
        or None."""
        return self.holds.get((fix_ident or "").strip().upper())


# ── exact lookup on a (ident, lat, lon[, …]) structured array ──────────────────
def _exact(arr, ident):
    if not HAS_NUMPY or arr is None or not ident:
        return None
    rows = arr[arr["ident"] == ident.strip().upper()]
    if len(rows) == 0:
        return None
    r = rows[0]
    return (str(r["ident"]), float(r["lat"]), float(r["lon"]))


# ── load ──────────────────────────────────────────────────────────────────────
def load(data_dir: str):
    """Load the nav-data cache from `data_dir`, or None when nothing is present.
    A partial cache (e.g. fixes only) still returns a usable NavData."""
    if not data_dir or not os.path.isdir(data_dir):
        return None
    fixes = _load_array(os.path.join(data_dir, FIXES_FILE))
    navs  = _load_array(os.path.join(data_dir, NAVAIDS_FILE))
    airways = holds = procedures = None
    cycle = ""
    jpath = os.path.join(data_dir, JSON_FILE)
    if os.path.exists(jpath):
        try:
            with open(jpath, "r", encoding="utf-8") as fh:
                d = json.load(fh)
            airways    = d.get("airways")
            holds      = d.get("holds")
            procedures = d.get("procedures")
            cycle      = d.get("cycle", "")
        except Exception:
            pass
    if fixes is None and navs is None and not procedures:
        return None
    return NavData(fixes=fixes, navaids=navs, airways=airways, holds=holds,
                   procedures=procedures, cycle=cycle)


def _load_array(path):
    if not HAS_NUMPY or not os.path.exists(path):
        return None
    try:
        arr = np.load(path, allow_pickle=False)
    except Exception:
        return None
    # Spatial queries binary-search the lat column, so guarantee it's sorted.
    if len(arr) > 1 and not (np.diff(arr["lat"]) >= 0).all():
        arr = arr[np.argsort(arr["lat"], kind="stable")]
    return arr


# ── spatial queries ───────────────────────────────────────────────────────────
def _nearby(arr, lat, lon, radius_nm):
    """Rows of a lat-sorted (ident, lat, lon, …) array within radius_nm, nearest
    first.  Same lat-band searchsorted + equirectangular cull as airports.py."""
    if not HAS_NUMPY or arr is None or len(arr) == 0:
        return arr[:0] if arr is not None else None
    dlat = radius_nm / _NM_PER_DEG_LAT
    lo = np.searchsorted(arr["lat"], lat - dlat, side="left")
    hi = np.searchsorted(arr["lat"], lat + dlat, side="right")
    band = arr[lo:hi]
    if len(band) == 0:
        return band
    cos_lat = max(0.05, math.cos(math.radians(lat)))
    dy = (band["lat"] - lat) * _NM_PER_DEG_LAT
    dx = (band["lon"] - lon) * _NM_PER_DEG_LAT * cos_lat
    d2 = dx * dx + dy * dy
    keep = d2 <= radius_nm * radius_nm
    out = band[keep]
    return out[np.argsort(d2[keep], kind="stable")]


def nearby_fixes(nd, lat, lon, radius_nm=40.0):
    """Named fixes within radius (structured rows, nearest first)."""
    return _nearby(nd.fixes, lat, lon, radius_nm) if nd is not None else None


def nearby_navaids(nd, lat, lon, radius_nm=80.0):
    """VOR/NDB/DME navaids within radius (structured rows, nearest first)."""
    return _nearby(nd.navaids, lat, lon, radius_nm) if nd is not None else None


# ── on-disk status (for the DATA / DOWNLOADS screen) ───────────────────────────
def download_date(data_dir):
    """datetime.date the cache was last written (navdata.json mtime), or None."""
    if not data_dir:
        return None
    jp = os.path.join(data_dir, JSON_FILE)
    if not os.path.exists(jp):
        return None
    try:
        return datetime.date.fromtimestamp(os.path.getmtime(jp))
    except OSError:
        return None


def disk_bytes(data_dir):
    """Total bytes the cache occupies on disk (0 when absent)."""
    total = 0
    for f in DOWNLOAD_FILES:
        p = os.path.join(data_dir or "", f)
        if os.path.exists(p):
            try:
                total += os.path.getsize(p)
            except OSError:
                pass
    return total


def cache_stats(data_dir):
    """A light summary dict for the DATA screen — counts, size, cycle, age.
    Loads the cache (cheap enough for a settings screen; not a hot path)."""
    out = {"present": False, "cycle": "", "fixes": 0, "navaids": 0,
           "airways": 0, "procedures": 0, "holds": 0, "mb": 0.0,
           "date": None, "age_days": 0, "expired": False}
    nd = load(data_dir)
    if nd is None:
        return out
    out["present"]    = True
    out["cycle"]      = nd.cycle
    out["fixes"]      = len(nd.fixes) if nd.has_fixes() else 0
    out["navaids"]    = len(nd.navaids) if nd.has_navaids() else 0
    out["airways"]    = len(nd.airways)
    out["procedures"] = sum(len(v) for v in nd.procedures.values())
    out["holds"]      = len(nd.holds)
    out["mb"]         = disk_bytes(data_dir) / (1024.0 * 1024.0)
    d = download_date(data_dir)
    if d is not None:
        out["date"]     = d
        out["age_days"] = (datetime.date.today() - d).days
        out["expired"]  = out["age_days"] > EXPIRY_DAYS
    return out


def download_url(filename):
    """Full URL for a cache file, or None when no source is configured."""
    base = (DOWNLOAD_BASE_URL or "").strip()
    if not base:
        return None
    if not base.endswith("/"):
        base += "/"
    return base + filename
