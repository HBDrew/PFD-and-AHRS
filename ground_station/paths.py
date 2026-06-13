"""
paths.py — locate the repo + its tools/shared modules, in dev and when frozen.

The ground station reuses the existing build tools (tools/build_*.py) and the
runtime modules (shared/*.py) in-process — subprocess-calling python won't work
once we're a one-file PyInstaller binary.  This module puts the repo root,
tools/ and shared/ on sys.path so those imports resolve either way, and exposes
where the built data should land (each device's data/ dir) for deploy.
"""

import os
import sys

# Two roots, because they differ once frozen:
#   CODE_ROOT  — where the tools/shared sources live, for in-process imports.
#                In dev that's the repo (two levels up); when frozen the modules
#                are bundled in the PyInstaller archive (importable directly) and
#                also extracted under sys._MEIPASS, which we add for safety.
#   BASE       — where the app *runs from*: the dir holding the .exe when frozen
#                (drop it in your repo checkout to deploy into it), else the repo.
_FROZEN = getattr(sys, "frozen", False)
if _FROZEN:
    CODE_ROOT = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    BASE = os.path.dirname(os.path.abspath(sys.executable))
else:
    CODE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    BASE = CODE_ROOT

ROOT = CODE_ROOT  # back-compat alias

for _p in (CODE_ROOT, os.path.join(CODE_ROOT, "tools"),
           os.path.join(CODE_ROOT, "shared")):
    if _p not in sys.path and os.path.isdir(_p):
        sys.path.insert(0, _p)

# Default build workspace — where freshly built caches are written before they
# are deployed to a device data/ dir or published.  Sits next to the app.
WORKSPACE = os.path.join(BASE, "groundstation_out")

# Device data directories the app can deploy into.  Only the ones that exist
# in this checkout are offered as deploy targets.
_DEVICES = ("pi4", "pi_zero", "pi_display")


def device_data_dirs():
    """{device_name: <base>/<device>/data} for every device dir present next to
    the app (run from a checkout, or drop the .exe in one)."""
    out = {}
    for dev in _DEVICES:
        if os.path.isdir(os.path.join(BASE, dev)):
            out[dev] = os.path.join(BASE, dev, "data")
    return out


def workspace_for(subdir):
    """Build-output dir for a dataset (created on demand)."""
    p = os.path.join(WORKSPACE, subdir)
    os.makedirs(p, exist_ok=True)
    return p
