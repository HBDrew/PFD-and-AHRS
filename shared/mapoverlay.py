"""
mapoverlay.py – Map overlay quick-cycle (shared by both displays).

One on-map control steps the heavy *non-traffic* overlays one at a time so
the map stays readable.  Traffic is NOT part of this — it's always on
(safety) and drawn regardless of the overlay state.

States, in cycle order (matches the pilot's mental model):
    asp     – Airspace shading
    tfc     – Traffic only (no heavy overlay added; traffic + base map)
    wx      – METAR station dots
    nexrad  – NEXRAD reflectivity raster

These map onto the same ds booleans the Setup → Display pills toggle, so
the quick-cycle and the granular pills stay consistent.  Selecting one
overlay clears the others (one-at-a-time); if the pills have enabled more
than one, the state reads "multi" and the next cycle collapses to a single
overlay.
"""

ORDER = ["asp", "tfc", "wx", "wnd", "nexrad"]

LABELS = {"asp": "ASP", "tfc": "TFC", "wx": "MET", "wnd": "WND",
          "nexrad": "NEX", "multi": "MULTI"}

# Overlay key → ds setting it drives.  "tfc" has no key (it's "none on").
_KEYS = {
    "asp":    "map_show_airspaces",
    "wx":     "map_show_metar",
    "wnd":    "map_show_winds",
    "nexrad": "map_show_nexrad",
}


def state(ds):
    """Current overlay state from the ds booleans."""
    active = [k for k, key in _KEYS.items() if ds.get(key)]
    if not active:
        return "tfc"
    if len(active) == 1:
        return active[0]
    return "multi"


def label(ds):
    return LABELS.get(state(ds), "OVLY")


def apply(ds, st):
    """Set the ds booleans for state `st` (exclusive — clears the others).
    Does not touch traffic or any base layer."""
    for k, key in _KEYS.items():
        ds[key] = (st == k)


def cycle(ds):
    """Advance to the next overlay in ORDER and apply it.  A "multi" state
    collapses to the first overlay.  Returns the new state."""
    cur = state(ds)
    if cur in ORDER:
        nxt = ORDER[(ORDER.index(cur) + 1) % len(ORDER)]
    else:
        nxt = ORDER[0]
    apply(ds, nxt)
    return nxt
