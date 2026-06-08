#!/usr/bin/env python3
"""
adsb_internet_feed.py – Internet ADS-B → GDL90/UDP feed.

Pulls live aircraft from a public ADS-B aggregator and emits GDL90 Traffic
Reports on UDP :4000 — the exact contract the PFD's traffic listener
already speaks (shared/adsb.py).  Two uses:

  1. TEST without hardware — point it at a busy area and watch real traffic
     appear on the moving map (no SDR, no Pico needed).
  2. IN-FLIGHT over Starlink / cabin Wi-Fi — run it on the display Pi to get
     traffic from the internet when you have connectivity but no receiver.

Sources (no API key needed for the community feeds):
  • airplanes.live / adsb.lol / adsb.fi   — readsb "point" API
  • opensky                               — OpenSky Network states API

Examples:
    # Test: traffic within 80 NM of KLAX, broadcast to the local PFD
    python3 tools/adsb_internet_feed.py --lat 33.94 --lon -118.40 --radius 80

    # In-flight: re-centre on the aircraft's own GPS (from the AHRS SSE)
    python3 tools/adsb_internet_feed.py --follow-ahrs http://192.168.4.1/events

    # Offline parser smoke test (no network)
    python3 tools/adsb_internet_feed.py --selftest

IMPORTANT — advisory only.  Internet traffic depends on ground-station
coverage and connectivity: it lags a few seconds, has gaps (and won't
reliably include UAT-only / non-transponder aircraft), and is NOT a
substitute for see-and-avoid or a certified traffic system.  A local SDR
receiver (the Nooelec) gives better own-ship-relative traffic because it's
direct line-of-sight and needs no internet.  Be polite to the free feeds:
keep the poll interval >= 1 s and the radius sane.
"""

import argparse
import json
import math
import os
import socket
import sys
import time
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "shared"))

import gdl90  # noqa: E402

_UA = "PFD-and-AHRS/adsb_internet_feed (experimental EFB; contact via repo)"

# readsb "point" API hosts — all share /v2/point/<lat>/<lon>/<radius_nm>.
_POINT_HOSTS = {
    "airplanes_live": "https://api.airplanes.live/v2/point",
    "adsb_lol":       "https://api.adsb.lol/v2/point",
    "adsb_fi":        "https://opendata.adsb.fi/api/v2/point",
}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ── Source parsers → common aircraft dicts ────────────────────────────────────
def parse_readsb_point(payload):
    """Parse a readsb point-API JSON dict into a list of aircraft dicts."""
    out = []
    for ac in payload.get("ac", []) or []:
        hexid = str(ac.get("hex", "")).strip().upper().replace("~", "")
        if not hexid:
            continue
        lat = _num(ac.get("lat"))
        lon = _num(ac.get("lon"))
        if lat is None or lon is None:
            continue
        alt = ac.get("alt_baro")
        alt_ft = None if alt in (None, "ground") else _num(alt)
        out.append({
            "hex": hexid,
            "lat": lat, "lon": lon,
            "alt_ft": alt_ft,
            "gs_kt": _num(ac.get("gs")) or 0,
            "track_deg": _num(ac.get("track")) or 0.0,
            "vvel_fpm": _num(ac.get("baro_rate"))
            or _num(ac.get("geom_rate")) or 0,
            "callsign": str(ac.get("flight", "")).strip(),
        })
    return out


def parse_opensky(payload):
    """Parse an OpenSky /states/all JSON dict into aircraft dicts.
    State vector fields: 0 icao24, 1 callsign, 5 lon, 6 lat,
    7 baro_alt(m), 9 velocity(m/s), 10 true_track, 11 vert_rate(m/s)."""
    out = []
    M_TO_FT = 3.28084
    MS_TO_KT = 1.94384
    MS_TO_FPM = 196.850
    for s in payload.get("states", []) or []:
        if len(s) < 12 or s[5] is None or s[6] is None:
            continue
        out.append({
            "hex": str(s[0]).strip().upper(),
            "lat": float(s[6]), "lon": float(s[5]),
            "alt_ft": None if s[7] is None else float(s[7]) * M_TO_FT,
            "gs_kt": 0 if s[9] is None else float(s[9]) * MS_TO_KT,
            "track_deg": 0.0 if s[10] is None else float(s[10]),
            "vvel_fpm": 0 if s[11] is None else float(s[11]) * MS_TO_FPM,
            "callsign": (s[1] or "").strip(),
        })
    return out


# ── Fetch ─────────────────────────────────────────────────────────────────────
def _get_json(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def fetch(source, lat, lon, radius_nm):
    """Fetch + parse one round of aircraft from `source`.  Returns a list of
    aircraft dicts (raises on network/JSON error so the caller can retry)."""
    if source == "opensky":
        # Convert centre+radius to a lat/lon bounding box.
        dlat = radius_nm / 60.0
        dlon = radius_nm / (60.0 * max(0.05, math.cos(math.radians(lat))))
        url = ("https://opensky-network.org/api/states/all?"
               f"lamin={lat-dlat:.4f}&lamax={lat+dlat:.4f}"
               f"&lomin={lon-dlon:.4f}&lomax={lon+dlon:.4f}")
        return parse_opensky(_get_json(url))
    base = _POINT_HOSTS.get(source)
    if base is None:
        raise ValueError(f"unknown source '{source}'")
    url = f"{base}/{lat:.5f}/{lon:.5f}/{min(250, int(radius_nm))}"
    return parse_readsb_point(_get_json(url))


def emit(aircraft, sock, dest):
    """Encode each aircraft as a GDL90 Traffic Report and send it.  Returns
    the number of frames sent."""
    sent = 0
    for ac in aircraft:
        try:
            addr = int(ac["hex"], 16)
        except (ValueError, KeyError):
            continue
        frame = gdl90.encode_traffic(
            address=addr, lat=ac["lat"], lon=ac["lon"],
            alt_ft=ac.get("alt_ft"),
            gs_kt=ac.get("gs_kt", 0) or 0,
            track_deg=ac.get("track_deg", 0.0) or 0.0,
            vvel_fpm=ac.get("vvel_fpm", 0) or 0,
            callsign=ac.get("callsign", ""))
        sock.sendto(frame, dest)
        sent += 1
    return sent


# ── Optional GPS-follow (in-flight) ───────────────────────────────────────────
def _make_position_source(args):
    """Return a callable -> (lat, lon).  Static unless --follow-ahrs is set,
    in which case it tracks the aircraft's own GPS from the AHRS SSE stream
    (reusing shared/sse_client) so the query box moves with the flight."""
    if not args.follow_ahrs:
        return lambda: (args.lat, args.lon)

    import threading
    from sse_client import SSEClient
    state, lock = {"lat": args.lat, "lon": args.lon}, threading.Lock()
    SSEClient(args.follow_ahrs, state, lock).start()

    def _pos():
        with lock:
            la, lo = state.get("lat"), state.get("lon")
        if la and lo:
            return float(la), float(lo)
        return args.lat, args.lon
    return _pos


# ── Main loop ─────────────────────────────────────────────────────────────────
def run(args):
    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    out.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    dest = (args.out_host, args.out_port)
    interval = max(1.0, args.interval)
    pos = _make_position_source(args)
    print(f"[feed] source={args.source} radius={args.radius}nm "
          f"-> {args.out_host}:{args.out_port} every {interval:g}s")
    while True:
        lat, lon = pos()
        # Always send a heartbeat first — the 'I'm alive' beacon a real
        # source emits every second.  This is what lets the display tell
        # "feed running, no aircraft in range" (link up) apart from "feed
        # not running" (link down), even when 0 aircraft are returned.
        try:
            out.sendto(gdl90.encode_heartbeat(), dest)
        except OSError:
            pass
        try:
            acs = fetch(args.source, lat, lon, args.radius)
            n = emit(acs, out, dest)
            print(f"[feed] @({lat:.3f},{lon:.3f}) {len(acs)} aircraft, "
                  f"{n} frames sent")
        except Exception as e:                                   # noqa: BLE001
            print(f"[feed] {type(e).__name__}: {e}")
        time.sleep(interval)


# ── Selftest (no network) ─────────────────────────────────────────────────────
def selftest():
    sample_readsb = {"ac": [
        {"hex": "a8c4f2", "flight": "N172SP ", "lat": 34.8697,
         "lon": -111.7610, "alt_baro": 9500, "gs": 145, "track": 270,
         "baro_rate": -640},
        {"hex": "abc123", "flight": "SWA42  ", "lat": 34.9, "lon": -111.8,
         "alt_baro": "ground", "gs": 0, "track": 0},
        {"hex": "nopos", "flight": "X", "alt_baro": 1000},   # dropped, no lat/lon
    ]}
    acs = parse_readsb_point(sample_readsb)
    assert len(acs) == 2, f"readsb: 2 with position, got {len(acs)}"
    assert acs[0]["callsign"] == "N172SP", "callsign trimmed"
    assert acs[1]["alt_ft"] is None, "'ground' altitude -> None"

    sample_os = {"states": [
        ["a8c4f2", "DAL123 ", "US", 0, 0, -111.76, 34.87, 2895.6, False,
         74.6, 270.0, -3.25, None, None, None, "0000", False, 0],
    ]}
    acs2 = parse_opensky(sample_os)
    assert len(acs2) == 1, "opensky: one state parsed"
    assert abs(acs2[0]["alt_ft"] - 9500) < 50, f"m->ft alt {acs2[0]['alt_ft']}"
    assert abs(acs2[0]["gs_kt"] - 145) < 3, f"m/s->kt {acs2[0]['gs_kt']}"

    # End-to-end: emit -> decode round-trip.
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    rx.bind(("127.0.0.1", 0))
    port = rx.getsockname()[1]
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    n = emit(acs, tx, ("127.0.0.1", port))
    assert n == 2, f"emitted 2 frames, got {n}"
    rx.settimeout(2.0)
    got = []
    for _ in range(2):
        data, _a = rx.recvfrom(8192)
        got += gdl90.decode_stream(data)
    icaos = {m["icao"] for m in got}
    assert "A8C4F2" in icaos and "ABC123" in icaos, f"round-trip icaos {icaos}"
    rx.close(); tx.close()
    print("INTERNET-FEED SELFTEST PASSED")


def main():
    ap = argparse.ArgumentParser(description="Internet ADS-B -> GDL90/UDP feed")
    ap.add_argument("--source", default="airplanes_live",
                    choices=list(_POINT_HOSTS) + ["opensky"])
    ap.add_argument("--lat", type=float, default=34.8697, help="centre lat")
    ap.add_argument("--lon", type=float, default=-111.7610, help="centre lon")
    ap.add_argument("--radius", type=float, default=80.0, help="nm (max 250)")
    ap.add_argument("--follow-ahrs", metavar="SSE_URL", default=None,
                    help="re-centre on the aircraft GPS from this AHRS SSE URL")
    ap.add_argument("--out-host", default="255.255.255.255")
    ap.add_argument("--out-port", type=int, default=4000)
    ap.add_argument("--interval", type=float, default=8.0, help="poll seconds")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        selftest()
        return
    run(args)


if __name__ == "__main__":
    main()
