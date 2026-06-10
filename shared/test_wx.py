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
    calls = {"n": 0, "last": None}

    def fake_fetch(lat, lon, radius):
        calls["n"] += 1
        calls["last"] = (lat, lon, radius)
        return [{"icao": "KTST", "lat": lat, "lon": lon, "fltcat": "VFR"}]

    c = wx.WxClient(view_fn=lambda: (34.0, -111.0, 100.0), fetch_fn=fake_fetch)
    c._fetch(34.0, -111.0, 100.0)
    check(c.count() == 1, "snapshot populated")
    check(c.rx_count == 1 and c.connected, "rx counted, online")
    check(calls["last"] == (34.0, -111.0, 100.0), "fetch got the view")
    check(c.snapshot()[0]["icao"] == "KTST", "snapshot content")

    # stale snapshot retained if a later fetch fails (run loop catches it).
    def boom(lat, lon, radius):
        raise OSError("no net")
    c.fetch_fn = boom
    raised = False
    try:
        c._fetch(34.0, -111.0, 100.0)
    except OSError:
        raised = True
    check(raised, "_fetch surfaces errors to the run loop")
    check(c.count() == 1, "stale snapshot retained on error")


def test_parse_tafs():
    data = [
        {"icaoId": "KSEZ", "lat": 34.85, "lon": -111.79,
         "rawTAF": "KSEZ 091120Z 0912/1012 28005KT P6SM SKC"},
        {"stationId": "KFLG", "lat": 35.14, "lon": -111.67,
         "raw_text": "KFLG 091120Z 0912/1012 24010KT 6SM"},
        {"icaoId": "", "rawTAF": "no id"},                  # dropped
        {"icaoId": "KNOPE"},                                  # no raw -> dropped
    ]
    ts = wx.parse_tafs(data)
    check(len(ts) == 2, f"two TAFs parsed, got {len(ts)}")
    by = {t["icao"]: t for t in ts}
    check(by["KSEZ"]["raw"].startswith("KSEZ"), "rawTAF taken")
    check(by["KFLG"]["raw"].startswith("KFLG"), "raw_text fallback taken")
    check(by["KSEZ"]["src"] == "INET", "tagged INET")


def test_parse_airsigmets():
    data = [
        {"airSigmetType": "AIRMET", "hazard": "TURB",
         "rawAirSigmet": "AIRMET TANGO ...",
         "validTimeFrom": 1_700_000_000, "validTimeTo": 1_700_021_600,
         "coords": [{"lat": 34.0, "lon": -112.0}, {"lat": 35.0, "lon": -112.0},
                    {"lat": 35.0, "lon": -111.0}]},
        {"airSigmetType": "SIGMET", "hazard": "CONVECTIVE",
         "rawAirSigmet": "SIGMET ...",
         "lat": [33.0, 34.0, 34.0], "lon": [-113.0, -113.0, -112.0]},
        {"airSigmetType": "AIRMET", "hazard": "IFR",
         "rawAirSigmet": "AIRMET SIERRA ..."},                # text-only
    ]
    a = wx.parse_airsigmets(data)
    check(len(a) == 3, f"three advisories, got {len(a)}")
    check(a[0]["kind"] == "AIRMET" and a[0]["hazard"] == "Turbulence",
          "AIRMET/TURB mapped")
    check(len(a[0]["vertices"]) == 3, "coords ring parsed")
    check(a[1]["kind"] == "SIGMET" and a[1]["hazard"] == "Convective",
          "convective -> SIGMET")
    check(len(a[1]["vertices"]) == 3, "lat/lon array ring parsed")
    check(a[2]["vertices"] == [], "text-only has empty ring")


def test_interp_winds():
    # Two levels; check the midpoint interpolates wind (u/v) + temp sanely.
    samples = [(3000, 270, 20, 0), (9000, 280, 40, -10)]
    lv = wx.interp_winds(samples, [3000, 6000, 9000, 12000])
    by = {x["alt_ft"]: x for x in lv}
    check(set(by) == {3000, 6000, 9000}, "12000 above column dropped (no extrap)")
    check(by[3000]["spd"] == 20 and by[9000]["spd"] == 40, "endpoints preserved")
    check(28 <= by[6000]["spd"] <= 32, f"mid speed ~30: {by[6000]['spd']}")
    check(270 <= by[6000]["dir"] <= 290, f"mid dir between: {by[6000]['dir']}")
    check(by[6000]["temp"] == -5, f"mid temp -5: {by[6000]['temp']}")
    # Light & variable when interpolated speed is tiny.
    calm = wx.interp_winds([(3000, 0, 1, 5), (9000, 0, 1, 0)], [6000])
    check(calm and calm[0]["lv"] and calm[0]["dir"] is None, "light&variable flagged")


def test_parse_open_meteo_winds():
    now = 1_700_000_000
    iso = time.strftime("%Y-%m-%dT%H:%M", time.gmtime(now))
    # One grid point, one hour; two pressure levels with heights/wind/temp.
    pt = {
        "latitude": 34.5, "longitude": -111.8,
        "hourly": {
            "time": [iso],
            "geopotential_height_850hPa": [1500.0],  # ~4921 ft
            "windspeed_850hPa": [25.0], "winddirection_850hPa": [250.0],
            "temperature_850hPa": [5.0],
            "geopotential_height_500hPa": [5600.0],  # ~18372 ft
            "windspeed_500hPa": [55.0], "winddirection_500hPa": [270.0],
            "temperature_500hPa": [-20.0],
        },
    }
    out = wx.parse_open_meteo_winds([pt], [6000, 9000, 12000], now=now)
    check(len(out) == 1, "one grid column parsed")
    w = out[0]
    check(w["lat"] == 34.5 and w["lon"] == -111.8, "carries its own position")
    check(w["station"] == "34.50,-111.80", "coordinate station id")
    check(w["src"] == "INET", "tagged INET")
    alts = {lv["alt_ft"] for lv in w["levels"]}
    check(alts == {6000, 9000, 12000}, f"interp onto in-range alts: {alts}")
    check(all(250 <= lv["dir"] <= 270 for lv in w["levels"]), "dirs in band")


def test_awcpoller_injected_fetch():
    calls = {"n": 0}

    def fake(lat, lon, radius):
        calls["n"] += 1
        return [{"icao": "KSEZ", "raw": "x"}]

    c = wx.AwcPoller(view_fn=lambda: (34.0, -111.0, 100.0), fetch_fn=fake)
    check(c._should_fetch(34.0, -111.0, 100.0, 1000.0), "first fetch always")
    check(c.count() == 0, "empty before fetch")


def test_wxclient_view_following():
    """The poller re-fetches when the view pans far enough, zooms a lot, or
    the periodic refresh is due — but not while parked on the same view."""
    c = wx.WxClient(view_fn=lambda: (34.0, -111.0, 100.0),
                    fetch_fn=lambda *a: [], move_refetch_frac=0.45)
    now = 1000.0
    check(c._should_fetch(34.0, -111.0, 100.0, now), "first fetch always")
    c._fetch(34.0, -111.0, 100.0)
    c._fetched_at = now
    check(not c._should_fetch(34.0, -111.0, 100.0, now + 1),
          "no refetch parked on same view")
    # 2° lat ≈ 120 NM pan > 0.45×100 → refetch.
    check(c._should_fetch(36.0, -111.0, 100.0, now + 1),
          "refetch after panning away")
    # small nudge stays put.
    check(not c._should_fetch(34.1, -111.0, 100.0, now + 1),
          "small pan does not refetch")
    check(c._should_fetch(34.0, -111.0, 220.0, now + 1),
          "refetch after zooming out")
    check(c._should_fetch(34.0, -111.0, 100.0, now + 999),
          "periodic refresh when due")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"ALL WX TESTS PASSED ({_passed} checks, {len(tests)} cases)")


if __name__ == "__main__":
    main()
