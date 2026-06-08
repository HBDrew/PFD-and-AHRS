"""
test_wx.py – standalone unit tests for the weather data layer.

Run:  python3 shared/test_wx.py
"""

import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import wx  # noqa: E402

_passed = 0


def check(cond, msg):
    global _passed
    assert cond, f"FAIL: {msg}"
    _passed += 1


def approx(a, b, tol):
    return a is not None and abs(a - b) <= tol


def test_visibility():
    check(wx.parse_visibility("10+") == 10.0, "10+ -> 10")
    check(approx(wx.parse_visibility("1 1/2"), 1.5, 1e-9), "mixed fraction")
    check(approx(wx.parse_visibility("1/2"), 0.5, 1e-9), "bare fraction")
    check(wx.parse_visibility(3.0) == 3.0, "numeric passthrough")
    check(wx.parse_visibility(None) is None, "None -> None")
    check(wx.parse_visibility("garbage") is None, "garbage -> None")


def test_ceiling():
    clouds = [{"cover": "FEW", "base": 4000}, {"cover": "BKN", "base": 1200},
              {"cover": "OVC", "base": 2500}]
    check(wx.ceiling_ft(clouds) == 1200, "lowest BKN/OVC base")
    check(wx.ceiling_ft([{"cover": "FEW", "base": 4000}]) is None,
          "few-only -> no ceiling")
    check(wx.ceiling_ft([]) is None, "empty -> None")
    check(wx.ceiling_ft(None) is None, "None -> None")


def test_flight_category():
    check(wx.derive_flight_category(10, None) == "VFR", "clear+10sm VFR")
    check(wx.derive_flight_category(10, 5000) == "VFR", "high ceiling VFR")
    check(wx.derive_flight_category(10, 3000) == "MVFR", "3000 ceiling MVFR")
    check(wx.derive_flight_category(4, None) == "MVFR", "4sm MVFR")
    check(wx.derive_flight_category(2, 800) == "IFR", "800 ceiling IFR")
    check(wx.derive_flight_category(0.5, 5000) == "LIFR", "half-mile LIFR")
    check(wx.derive_flight_category(10, 400) == "LIFR", "400 ceiling LIFR")


def test_cat_color():
    check(wx.cat_color("VFR") == (0, 200, 0), "VFR green")
    check(wx.cat_color("LIFR") == (220, 0, 220), "LIFR magenta")
    check(wx.cat_color("???") == (160, 160, 160), "unknown grey")


def test_parse_metars():
    now = 1_700_000_000
    data = [
        {"icaoId": "KLAX", "lat": 33.94, "lon": -118.40, "fltCat": "VFR",
         "visib": "10+", "wdir": 250, "wspd": 8, "altim": 1013.2,
         "temp": 18.0, "dewp": 12.0, "obsTime": now - 600,
         "rawOb": "KLAX ...", "name": "Los Angeles Intl"},
        # fltCat omitted -> derived from low ceiling/visibility (IFR).
        {"icaoId": "KSMO", "lat": 34.02, "lon": -118.45,
         "visib": "2", "clouds": [{"cover": "OVC", "base": 700}],
         "obsTime": now - 120},
        {"icaoId": "", "lat": 1, "lon": 1},          # no id -> dropped
        {"icaoId": "KNOPOS"},                          # no position -> dropped
    ]
    ms = wx.parse_metars(data, now=now)
    check(len(ms) == 2, f"two valid stations, got {len(ms)}")
    by = {m["icao"]: m for m in ms}
    check(by["KLAX"]["fltcat"] == "VFR", "explicit fltCat kept")
    check(by["KLAX"]["visib_mi"] == 10.0, "visib parsed")
    check(approx(by["KLAX"]["age_min"], 10.0, 0.1), "age computed")
    check(by["KSMO"]["fltcat"] == "IFR", "derived IFR from ceiling 700")
    check(by["KSMO"]["ceiling_ft"] == 700, "ceiling captured")


def test_wxclient_injected_fetch():
    calls = {"n": 0}

    def fake_fetch(lat, lon, radius):
        calls["n"] += 1
        return [{"icao": "KTST", "lat": lat, "lon": lon, "fltcat": "VFR"}]

    c = wx.WxClient(pos_fn=lambda: (34.0, -111.0), fetch_fn=fake_fetch)
    c._poll_once()
    check(c.count() == 1, "snapshot populated")
    check(c.rx_count == 1 and c.connected, "rx counted, online")
    snap = c.snapshot()
    check(snap[0]["icao"] == "KTST", "snapshot content")

    def boom(lat, lon, radius):
        raise OSError("no net")
    c.fetch_fn = boom
    c._poll_once()
    check(c.err_count == 1 and not c.connected, "error path counts + offline")
    check(c.count() == 1, "stale snapshot retained on error")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"ALL WX TESTS PASSED ({_passed} checks, {len(tests)} cases)")


if __name__ == "__main__":
    main()
