#!/usr/bin/env python3
"""
winds_check.py — diagnose the internet winds-aloft fetch (Open-Meteo).

Run this ON A DISPLAY PI (the one with internet):

    python3 tools/winds_check.py                 # default location (KDVT area)
    python3 tools/winds_check.py 33.69 -112.08   # your lat lon

It runs three checks and prints exactly what comes back, so we can see whether:
  [1] Open-Meteo is even reachable from this Pi,
  [2] the multi-point grid request the app makes actually succeeds (and how
      many points it can take before the free tier refuses it),
  [3] the parsed barbs carry real wind/temp at the standard altitudes.

No pygame / display needed — it only imports shared/wx.py.
"""
import sys
import os
import time
import json
import urllib.request
import urllib.error

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.join(_HERE, "..", "shared"), os.path.join(_HERE, "shared")):
    if os.path.isdir(_p):
        sys.path.insert(0, _p)

import wx  # noqa: E402

ALTS = [3000, 6000, 9000, 12000, 18000, 24000, 30000, 34000, 39000]


def _single_point(lat, lon):
    print("\n[1] Single-point Open-Meteo request "
          "(basic reachability + API sanity)...")
    url = (f"{wx._OPEN_METEO}?latitude={lat:.3f}&longitude={lon:.3f}"
           f"&hourly=windspeed_500hPa,winddirection_500hPa,temperature_500hPa"
           f"&windspeed_unit=kn&forecast_days=1&timezone=UTC")
    try:
        t = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": wx._UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read().decode("utf-8", "ignore"))
        h = d.get("hourly", {})
        n = len(h.get("time", []))
        print(f"    OK  ({time.time() - t:.1f}s) — {n} hourly steps; "
              f"500 hPa now ≈ {h['winddirection_500hPa'][0]}°/"
              f"{h['windspeed_500hPa'][0]} kt, "
              f"{h['temperature_500hPa'][0]}°C")
        return True
    except urllib.error.HTTPError as e:
        print(f"    HTTP {e.code} {e.reason} — Open-Meteo refused even ONE "
              f"point.  (403/429 = blocked or rate-limited.)")
        return False
    except Exception as e:                                       # noqa: BLE001
        print(f"    FAILED: {type(e).__name__}: {e}")
        print("    -> no internet route to api.open-meteo.com from this Pi.")
        return False


def _grid(lat, lon, n_axis):
    rng = 80.0 * 2.0                              # WINDS_CACHE_MARGIN at 80 nm
    sp = (2.0 * rng * 0.82) / max(1, n_axis - 1)
    pts = wx._winds_grid_points(lat, lon, rng, 1.0, spacing_nm=sp)
    nb = (len(pts) + wx._OM_MAX_BATCH - 1) // wx._OM_MAX_BATCH
    print(f"\n[2] Grid request: {len(pts)} points (±{rng:.0f} nm, ~{n_axis}/axis), "
          f"sent as {nb} batch(es) of ≤{wx._OM_MAX_BATCH} — what the app does...")
    try:
        t = time.time()
        cols = wx.fetch_winds(lat, lon, rng, aspect=1.0, spacing_nm=sp,
                              alts=ALTS, timeout=30)
        print(f"    OK  ({time.time() - t:.1f}s) — {len(cols)} winds columns")
        if not cols:
            print("    !! ZERO columns parsed — request returned but had no "
                  "pressure-level data.")
            return
        c = cols[0]
        print(f"\n[3] First column @ {c['station']}:")
        any_data = False
        for lv in c["levels"]:
            spd = lv.get("spd")
            any_data = any_data or (spd is not None)
            print(f"      {lv['alt_ft']:6d} ft : dir={lv.get('dir')}  "
                  f"spd={spd} kt  temp={lv.get('temp')}°C")
        if not any_data:
            print("    !! levels present but all wind values are None.")
    except urllib.error.HTTPError as e:
        print(f"    HTTP {e.code} {e.reason} — the {len(pts)}-point request "
              f"was REFUSED.")
        print("    -> Too many locations for the free tier.  Lower "
              "WINDS_GRID_AXIS_PTS in shared/config_base.py.")
    except Exception as e:                                       # noqa: BLE001
        print(f"    FAILED: {type(e).__name__}: {e}")


def main():
    lat = float(sys.argv[1]) if len(sys.argv) > 1 else 33.690
    lon = float(sys.argv[2]) if len(sys.argv) > 2 else -112.080
    print(f"Winds check @ {lat:.3f}, {lon:.3f}")
    print("=" * 56)
    if not _single_point(lat, lon):
        print("\nStopping: basic Open-Meteo access failed (see above).")
        return
    _grid(lat, lon, n_axis=8)
    print("\nDone.  If [1] works but [2] is refused, it's the point count.")
    print("If [2] returns columns with real numbers, the fetch is fine and "
          "the issue is display-side.")


if __name__ == "__main__":
    main()
