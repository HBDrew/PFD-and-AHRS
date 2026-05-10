#!/usr/bin/env python3
"""
render_inset_preview.py — emit a documentation PNG of the moving-map
inset on a long direct-to leg, where the great-circle vs rhumb
difference is the relevant correctness story.

Output:
  pi4/previews/preview_inset_long_d2.png  (480 × 360, pure pygame)

The inset is self-contained pure pygame (no GL, no SVT compositor),
so this script doesn't need an EGL display or SRTM tiles.  Scene is a
mid-leg snapshot of a transcontinental D2 (KSEZ → KOSH) with the
ownship sitting on the great circle — the polyline now matches the
CDI reference and the SVT direct-to trace.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "shared"))
sys.path.insert(0, os.path.join(_HERE, "..", "pi4"))

os.environ["SDL_VIDEODRIVER"] = "dummy"
os.environ["SDL_AUDIODRIVER"] = "dummy"
import pygame
pygame.init()
try:
    pygame.font.init()
except pygame.error:
    pass

import moving_map as _map_mod  # noqa: E402


def _gc_brg(la1, lo1, la2, lo2):
    import math
    phi1 = math.radians(la1); phi2 = math.radians(la2)
    dlam = math.radians(lo2 - lo1)
    y = math.sin(dlam) * math.cos(phi2)
    x = (math.cos(phi1) * math.sin(phi2)
         - math.sin(phi1) * math.cos(phi2) * math.cos(dlam))
    return (math.degrees(math.atan2(y, x)) % 360.0)


def main():
    out_dir = os.path.join(_HERE, "..", "pi4", "previews")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "preview_inset_long_d2.png")

    # KSEZ (Sedona, AZ) → KOSH (Oshkosh, WI): ~1200 nm.  Place the
    # aircraft at a point that lies on the great circle between them —
    # use the GC slerp helper so the ownship sits exactly on the line
    # the inset draws, which is the whole point of the documentation.
    KSEZ = (34.8485, -111.7882)
    KOSH = (43.9844, -88.5571)
    la_cur, lo_cur = _map_mod._gc_interp(KSEZ[0], KSEZ[1], KOSH[0], KOSH[1], 0.45)
    track = _gc_brg(la_cur, lo_cur, *KOSH)   # local GC tangent toward KOSH

    # Background gradient so the inset doesn't sit on flat black —
    # gives the documentation image a visible frame and a hint of the
    # darker panel chrome that surrounds it on the live PFD.
    W, H = 480, 360
    surf = pygame.Surface((W, H))
    for y in range(H):
        t = y / max(1, H - 1)
        r = int(8  + 14 * t)
        g = int(12 + 18 * t)
        b = int(22 + 28 * t)
        pygame.draw.line(surf, (r, g, b), (0, y), (W, y))

    # Inset rect: leave some breathing room around the chrome.
    margin = 16
    inset_rect = (margin, margin, W - 2 * margin, H - 2 * margin)

    settings = {
        "map_show_terrain":  False,   # no SRTM in this environment
        "map_show_water":    False,
        "map_show_airports": False,
        "map_show_runways":  False,
        "map_show_obstacles": False,
        "map_show_directto": True,
    }
    direct_to = {
        "ident":   "KOSH",
        "lat":     KOSH[0],
        "lon":     KOSH[1],
        "elev_ft": 808.0,
        "act_lat": KSEZ[0],
        "act_lon": KSEZ[1],
    }
    range_nm = 600   # wide range so the leg's GC bend is visible
    font = pygame.font.SysFont("monospace", 12, bold=True)

    _map_mod.render(
        surf, inset_rect, la_cur, lo_cur, 12000.0, track, track,
        "trk", range_nm, settings,
        airports_arr=None, runways_arr=None, obstacles_arr=None,
        srtm_dir="", water_dir="",
        direct_to=direct_to, font=font,
        airport_types_visible=set(), gs_kt=120.0,
    )

    # Caption strip below the inset — labels what the picture is, so
    # the documentation reader doesn't have to context-switch.
    cap = pygame.font.SysFont("monospace", 11)
    label = "MOVING-MAP INSET  TRK 058°  RNG 600 NM  D2 KOSH  great-circle course"
    img = cap.render(label, True, (190, 210, 230))
    surf.blit(img, ((W - img.get_width()) // 2, H - 14))

    pygame.image.save(surf, out_path)
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
