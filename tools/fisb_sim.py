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


# Area advisories (AIRMET/SIGMET/NOTAM) — broadcast from the Phoenix tower.
ADVISORIES = [
    "WAUS46 KKCI 091445 AIRMET TANGO FOR TURB VALID UNTIL 092100 "
    "FROM 40NW PGA TO 30E SJN TO 20S TUS TO 50W BXK MOD TURB BLW FL180",
    "WAUS41 KSLC 091445 AIRMET SIERRA FOR IFR VALID UNTIL 092100 "
    "AZ MTNS OCNL CIG BLW 010 VIS BLW 3SM BR/FG CONDS ENDG 18-20Z",
    "WSUS01 KKCI 091455 CONVECTIVE SIGMET 12C VALID UNTIL 091655 "
    "AZ FROM 30NW FLG TO 20E SEZ ISOL TS MOV LTL DVLPG TOPS TO FL410",
    "!FDC 1/2345 SEZ AIRSPACE SEDONA AZ TEMPORARY FLIGHT RESTRICTION "
    "WI AN AREA DEFINED AS 5NM RADIUS OF SEZ SFC-080 WEF 0906011200-0906012359",
    "!SEZ 06/001 SEZ RWY 03/21 CLSD WEF 0906011200-0906302359",
]


# Graphical hazard areas (polygons) — drawn shaded on the MET page; each carries
# its own paired bulletin (tap the shape to read it).
GRAPHICS = [
    {"hazard": "Turbulence",     # high country, NE Arizona
     "vertices": [(36.6, -113.2), (36.9, -109.8), (34.9, -109.3), (34.6, -112.8)],
     "text": ("AIRMET TANGO FOR TURB VALID UNTIL 092100. FROM 40NW PGA TO 30E "
              "SJN TO 20S SOW TO 50W FLG. MOD TURB BTN FL180 AND FL410. "
              "CONDS CONTG BYD 21Z THRU 03Z.")},
    {"hazard": "Convective",     # cell near Flagstaff / Sedona
     "vertices": [(35.5, -112.0), (35.6, -111.1), (34.9, -111.0), (34.8, -112.0)],
     "text": ("CONVECTIVE SIGMET 12C VALID UNTIL 091655. AZ. FROM 30NW FLG TO "
              "20E SEZ. ISOL TS MOV LTL. TOPS TO FL410. HAIL TO 1 IN PSBL.")},
    {"hazard": "Icing",          # north rim / Page
     "vertices": [(37.0, -112.4), (37.1, -111.0), (36.2, -110.9), (36.1, -112.3)],
     "text": ("AIRMET ZULU FOR ICE VALID UNTIL 092100. FROM PGA TO RQE TO FLG. "
              "MOD ICE BTN FRZLVL AND FL200. FRZLVL 090-110.")},
]


# Winds & temps aloft (FD codes) — high stations omit the low levels.
WINDS = [
    ("SEZ", "WINDS SEZ 6000 2416 9000 2522+04 12000 2635-02 18000 2752-15 "
            "24000 2867-26 30000 730438 34000 731548 39000 732156"),
    ("PHX", "WINDS PHX 3000 2010 6000 2218 9000 2528+08 12000 2740+02 "
            "18000 2960-12 24000 2977-25 30000 740540 34000 741750 "
            "39000 742259"),
    ("FLG", "WINDS FLG 9000 2615 12000 2730-04 18000 2850-17 24000 2965-28 "
            "30000 750542 34000 751652 39000 752160"),
    ("PRC", "WINDS PRC 9000 2518 12000 2632-03 18000 2748-16 24000 2860-27 "
            "30000 731040 34000 731850 39000 732158"),
]


def build_cycle(now):
    """Return a list of UAT uplink payloads for this instant (one per station;
    TAF stations carry their METAR + TAF together; plus area advisories, winds
    aloft, and the graphical hazard areas)."""
    payloads = []
    for icao, cat, tower in SCENARIO:
        reports = [metar_for(icao, cat, now)]
        if icao in TAF_STATIONS:
            reports.append(taf_for(icao, cat, now))
        payloads.append(fisb.encode_text_uplink(reports, station=tower))
    for _id, rec in WINDS:
        payloads.append(fisb.encode_text_uplink([rec], station=TOWER_PHX))
    # Area advisories — one uplink each, from the Phoenix tower.
    for adv in ADVISORIES:
        payloads.append(fisb.encode_text_uplink([adv], station=TOWER_PHX))
    # Graphical hazard areas — one uplink each (keeps each frame small).
    for g in GRAPHICS:
        payloads.append(fisb.encode_graphics_uplink([g], station=TOWER_PHX))
    # NEXRAD — a synthetic storm cell near Flagstaff, one uplink per block.
    # Stamp the mosaic ~8 min behind real time so the receipt-vs-valid age badge
    # has something realistic to show (FIS-B radar always lags its valid time).
    tm = time.gmtime(now - 8 * 60)
    valid_hm = (tm.tm_hour, tm.tm_min)
    for bn, intens in _nexrad_storm(35.0, -111.6):
        payloads.append(fisb.encode_nexrad_uplink(bn, intens, station=TOWER_PHX,
                                                  valid_hm=valid_hm))
    return payloads


def _nexrad_storm(clat, clon):
    """Synthetic NEXRAD blocks: a radial intensity cell centred on (clat, clon).
    Returns [(block_num, intensities[128]), …] for non-empty blocks."""
    blocks = []
    base_ring = int(clat / (4.0 / 60.0))
    base_col = int(((clon + 360.0) % 360.0) / (48.0 / 60.0))
    for dring in range(-8, 9):
        for dcol in range(-1, 2):
            bn = (base_ring + dring) * 450 + (base_col + dcol)
            lat_n, lon_w, lat_span, lon_span = fisb._nx_block_geo(bn, 0, False)
            bin_lat, bin_lon = lat_span / 4.0, lon_span / 32.0
            intens = [0] * 128
            for b in range(128):
                bcol, brow = b % 32, b // 32
                blat = lat_n - (brow + 0.5) * bin_lat
                blon = lon_w + (bcol + 0.5) * bin_lon
                d = fisb.nm_between(clat, clon, blat, blon)
                if d <= 32.0:
                    intens[b] = max(0, min(7, int(round(7 - d / 5.0))))
            if any(intens):
                blocks.append((bn, intens))
    return blocks


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

    # Advisories recovered.
    air = store.advisories("AIRMET")
    sig = store.advisories("SIGMET")
    nts = store.advisories("NOTAM")
    assert len(air) == 2 and len(sig) == 1 and len(nts) == 2, \
        f"advisories: AIRMET={len(air)} SIGMET={len(sig)} NOTAM={len(nts)}"

    gfx = store.graphics()
    assert len(gfx) == len(GRAPHICS), f"graphics: {len(gfx)} vs {len(GRAPHICS)}"

    winds = store.winds_stations()
    assert len(winds) == len(WINDS), f"winds: {len(winds)} vs {len(WINDS)}"

    cells = store.nexrad_cells()
    assert cells and store.nexrad_count > 0, "NEXRAD cells decoded"
    assert all(1 <= c["i"] <= 7 for c in cells), "valid intensities"

    print(f"FISB-SIM SELFTEST PASSED ({len(got)} METARs, all categories, "
          f"{len(gss)} stations, {len(tafs)} TAFs, "
          f"{len(air)}+{len(sig)}+{len(nts)} AIRMET/SIGMET/NOTAM, "
          f"{len(gfx)} graphics, {len(winds)} winds, "
          f"{len(cells)} NEXRAD cells)")


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
