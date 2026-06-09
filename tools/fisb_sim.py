#!/usr/bin/env python3
"""
fisb_sim.py – ground FIS-B simulator for building/testing the WX screens.

Broadcasts synthetic FIS-B weather as GDL90 0x07 uplink frames to UDP :4000 —
exactly what tools/dump978_gdl90_bridge.py emits from a real dongle — so the
whole display pipeline (ADSBClient → fisb.FisbWeather → _update_weather → map
dots / picker / WX status / ground-station symbols) runs with no radio and no
reception.  Use it to design and exercise the weather UI on the ground.

The scenario is a realistic Arizona field around the Sedona demo center: real
ICAO idents (so the display's airport DB geolocates them), spanning all four
flight categories in a rough north=worse / south=better gradient, broadcast
from two ground stations (Phoenix + Flagstaff).  METAR timestamps are re-stamped
to the current UTC each cycle so the dots stay "fresh" and ages tick like live.

Usage:
    python3 tools/fisb_sim.py                      # broadcast to 255.255.255.255:4000
    python3 tools/fisb_sim.py --out-host 127.0.0.1 # loopback only
    python3 tools/fisb_sim.py --period 8           # seconds between cycles
    python3 tools/fisb_sim.py --selftest           # offline decode round-trip

Run it INSTEAD of the live bridge while testing:
    sudo systemctl stop dump978-gdl90.service
    python3 tools/fisb_sim.py
"""

import argparse
import os
import socket
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "shared"))

import gdl90  # noqa: E402
import fisb   # noqa: E402


# Two synthetic FIS-B ground stations (lat, lon, site_id).
TOWER_PHX = (33.43, -112.01, 1)
TOWER_FLG = (35.14, -111.67, 2)

# Per-category weather recipe: wind, visibility(+wx), sky, temp/dew, altimeter.
_RECIPE = {
    "VFR":  ("24008KT", "10SM",       "FEW120", "28", "06", "A3001"),
    "MVFR": ("31012KT", "5SM",        "BKN035", "19", "12", "A2998"),
    "IFR":  ("09010KT", "2SM BR",     "OVC008", "12", "10", "A2995"),
    "LIFR": ("VRB04KT", "1/2SM FG",   "OVC003", "05", "05", "A3010"),
}

# (ICAO, category, tower) — real AZ airports so the display can geolocate them.
# Rough gradient: clear in the Phoenix basin, IMC up in the high country.
SCENARIO = [
    ("KPHX", "VFR",  TOWER_PHX), ("KSDL", "VFR",  TOWER_PHX),
    ("KDVT", "VFR",  TOWER_PHX), ("KCHD", "VFR",  TOWER_PHX),
    ("KIWA", "VFR",  TOWER_PHX), ("KGYR", "VFR",  TOWER_PHX),
    ("KFFZ", "VFR",  TOWER_PHX), ("KCGZ", "VFR",  TOWER_PHX),
    ("KTUS", "VFR",  TOWER_PHX),
    ("KPRC", "MVFR", TOWER_PHX), ("KSEZ", "MVFR", TOWER_PHX),
    ("KIGM", "MVFR", TOWER_FLG),
    ("KINW", "IFR",  TOWER_FLG), ("KSOW", "IFR",  TOWER_FLG),
    ("KGCN", "IFR",  TOWER_FLG),
    ("KFLG", "LIFR", TOWER_FLG), ("KPGA", "LIFR", TOWER_FLG),
]


# Stations that also broadcast a TAF (forecast), to exercise the TAF readout.
TAF_STATIONS = {"KPHX", "KSEZ", "KPRC", "KFLG", "KGCN"}


def metar_for(icao, category, now):
    """Build a raw METAR string for ``icao`` at category, stamped to ``now``."""
    wind, vis, sky, temp, dew, altim = _RECIPE[category]
    t = time.gmtime(now)
    ts = f"{t.tm_mday:02d}{t.tm_hour:02d}{t.tm_min:02d}"
    return f"{icao} {ts}Z {wind} {vis} {sky} {temp}/{dew} {altim}"


def taf_for(icao, category, now):
    """Build a one-line TAF for ``icao`` valid now → +24 h, with a FM group that
    improves to VFR a few hours out."""
    wind, vis, sky, _t, _d, _a = _RECIPE[category]
    t = time.gmtime(now)
    iss = f"{t.tm_mday:02d}{t.tm_hour:02d}{t.tm_min:02d}"
    end = time.gmtime(now + 24 * 3600)
    valid = f"{t.tm_mday:02d}{t.tm_hour:02d}/{end.tm_mday:02d}{end.tm_hour:02d}"
    fm = time.gmtime(now + 4 * 3600)
    fmgrp = f"FM{fm.tm_mday:02d}{fm.tm_hour:02d}00"
    return (f"TAF {icao} {iss}Z {valid} {wind} {vis} {sky} "
            f"{fmgrp} 25010KT P6SM FEW120")


def build_cycle(now):
    """Return a list of UAT uplink payloads for this instant (one per station;
    TAF stations carry their METAR + TAF together)."""
    payloads = []
    for icao, cat, tower in SCENARIO:
        reports = [metar_for(icao, cat, now)]
        if icao in TAF_STATIONS:
            reports.append(taf_for(icao, cat, now))
        payloads.append(fisb.encode_text_uplink(reports, station=tower))
    return payloads


def run(out_host, out_port, period_s):
    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    out.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    dest = (out_host, out_port)
    cats = {}
    for _i, c, _t in SCENARIO:
        cats[c] = cats.get(c, 0) + 1
    spread = " ".join(f"{c}:{n}" for c, n in
                      sorted(cats.items(), key=lambda kv: kv[0]))
    print(f"[fisb-sim] broadcasting {len(SCENARIO)} METARs ({spread}) from "
          f"2 stations to {out_host}:{out_port} every {period_s:g}s")
    print("[fisb-sim] on the MFD: MET overlay → dots + 2 'FISB' towers; "
          "WX status shows 'R{}' ; tap it for RADIO/AUTO/INET".format(len(SCENARIO)))
    cycle = 0
    while True:
        now = time.time()
        for payload in build_cycle(now):
            out.sendto(gdl90.encode_uplink(payload), dest)
        cycle += 1
        if cycle % 10 == 1:
            print(f"[fisb-sim] cycle {cycle}: sent {len(SCENARIO)} uplink frames")
        time.sleep(period_s)


def selftest():
    """Build one cycle and decode it back through the real gdl90 + fisb path."""
    store = fisb.FisbWeather()
    now = time.time()
    for payload in build_cycle(now):
        frame = gdl90.encode_uplink(payload)
        for msg in gdl90.decode_stream(frame):
            store.ingest_gdl90_msg(msg)

    # Every scenario station should have decoded into a stored METAR.
    coords = {icao: (34.0, -111.0) for icao, _c, _t in SCENARIO}  # any coords
    sts = store.metar_stations(lambda i: coords.get(i))
    got = {s["icao"] for s in sts}
    want = {icao for icao, _c, _t in SCENARIO}
    assert got == want, f"missing METARs: {want - got}"

    # Flight categories must survive end-to-end.
    cat = {s["icao"]: s["fltcat"] for s in sts}
    want_cat = {icao: c for icao, c, _t in SCENARIO}
    bad = {i: (cat[i], want_cat[i]) for i in want_cat if cat[i] != want_cat[i]}
    assert not bad, f"category mismatch: {bad}"

    # Both ground stations recovered from the headers.
    gss = store.ground_stations()
    assert len(gss) == 2, f"expected 2 towers, got {len(gss)}"

    # TAFs recovered for the TAF stations.
    tafs = [i for i in TAF_STATIONS if store.taf_for(i)]
    assert sorted(tafs) == sorted(TAF_STATIONS), \
        f"missing TAFs: {set(TAF_STATIONS) - set(tafs)}"

    print(f"FISB-SIM SELFTEST PASSED ({len(got)} METARs, all categories, "
          f"{len(gss)} stations, {len(tafs)} TAFs)")


def main():
    ap = argparse.ArgumentParser(description="Ground FIS-B weather simulator")
    ap.add_argument("--out-host", default="255.255.255.255",
                    help="UDP destination (broadcast by default)")
    ap.add_argument("--out-port", type=int, default=4000)
    ap.add_argument("--period", type=float, default=8.0,
                    help="seconds between full cycles")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        selftest()
        return
    run(args.out_host, args.out_port, args.period)


if __name__ == "__main__":
    main()
