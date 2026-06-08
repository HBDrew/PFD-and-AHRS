#!/usr/bin/env python3
"""
adsb_gdl90_bridge.py – dump1090 (SBS-1) → GDL90/UDP bridge.

The PFD consumes GDL90 traffic over UDP (shared/adsb.py).  A Nooelec NESDR
Nano 2 dual-band bundle is driven by dump1090 (1090ES) and dump978 (978
UAT); dump1090-fa exposes a SBS-1 "BaseStation" text feed on TCP 30003.
This bridge reads that feed, accumulates per-aircraft state, and emits
GDL90 Traffic Reports on a UDP broadcast — closing the loop end-to-end
using the same encoder the display's decoder is tested against.

978 UAT traffic can be folded into the same SBS feed (e.g. piping
dump978-fa through the readsb/dump1090 SBS combiner), so a single bridge
instance covers both bands.

Usage:
    python3 tools/adsb_gdl90_bridge.py \
        --sbs-host 127.0.0.1 --sbs-port 30003 \
        --out-host 255.255.255.255 --out-port 4000

Run --selftest to exercise the SBS parser + emit path with no network.
"""

import argparse
import os
import socket
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "shared"))

import gdl90  # noqa: E402


# ── SBS-1 BaseStation parsing ─────────────────────────────────────────────────
# Field indices in the comma-separated MSG record (dump1090 / BaseStation):
#   1  transmission type   4  hex ident     10 callsign      11 altitude
#   12 ground speed        13 track         14 latitude      15 longitude
#   16 vertical rate       21 is-on-ground
def _to_float(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def parse_sbs_line(line, table, now):
    """Merge one SBS-1 line into `table` (hex -> aircraft dict).  Returns
    the hex ident touched, or None if the line wasn't a usable MSG."""
    parts = line.strip().split(",")
    if len(parts) < 17 or parts[0] != "MSG":
        return None
    hexid = parts[4].strip().upper()
    if not hexid:
        return None
    ac = table.setdefault(hexid, {"hex": hexid})
    ac["last_s"] = now

    cs = parts[10].strip()
    if cs:
        ac["callsign"] = cs
    alt = _to_float(parts[11])
    if alt is not None:
        ac["alt_ft"] = alt
    gs = _to_float(parts[12])
    if gs is not None:
        ac["gs_kt"] = gs
    trk = _to_float(parts[13])
    if trk is not None:
        ac["track_deg"] = trk
    lat = _to_float(parts[14])
    lon = _to_float(parts[15])
    if lat is not None and lon is not None and not (lat == 0.0 and lon == 0.0):
        ac["lat"] = lat
        ac["lon"] = lon
        ac["pos_s"] = now
    vr = _to_float(parts[16])
    if vr is not None:
        ac["vvel_fpm"] = vr
    return hexid


def emit_frames(table, sock, dest, now, pos_max_age=30.0):
    """Send a GDL90 Traffic Report for every aircraft with a recent
    position.  Returns the number of frames sent."""
    sent = 0
    for hexid, ac in table.items():
        if "lat" not in ac or "lon" not in ac:
            continue
        if now - ac.get("pos_s", 0) > pos_max_age:
            continue
        try:
            addr = int(hexid, 16)
        except ValueError:
            continue
        frame = gdl90.encode_traffic(
            address=addr,
            lat=ac["lat"], lon=ac["lon"],
            alt_ft=ac.get("alt_ft"),
            gs_kt=ac.get("gs_kt", 0) or 0,
            track_deg=ac.get("track_deg", 0.0) or 0.0,
            vvel_fpm=ac.get("vvel_fpm", 0) or 0,
            callsign=ac.get("callsign", ""))
        sock.sendto(frame, dest)
        sent += 1
    return sent


def prune(table, now, stale_s=60.0):
    for hexid in [h for h, a in table.items()
                  if now - a.get("last_s", 0) > stale_s]:
        del table[hexid]


# ── Main loop ─────────────────────────────────────────────────────────────────
def run(sbs_host, sbs_port, out_host, out_port, emit_hz=1.0):
    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    out.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    dest = (out_host, out_port)
    table = {}
    emit_period = 1.0 / max(0.1, emit_hz)

    while True:
        try:
            print(f"[bridge] connecting to dump1090 SBS {sbs_host}:{sbs_port}")
            s = socket.create_connection((sbs_host, sbs_port), timeout=10)
            s.settimeout(emit_period)
            buf = b""
            last_emit = time.monotonic()
            while True:
                try:
                    chunk = s.recv(4096)
                    if not chunk:
                        raise ConnectionError("SBS feed closed")
                    buf += chunk
                    while b"\n" in buf:
                        line, _, buf = buf.partition(b"\n")
                        parse_sbs_line(line.decode("ascii", "ignore"),
                                       table, time.monotonic())
                except socket.timeout:
                    pass
                now = time.monotonic()
                if now - last_emit >= emit_period:
                    n = emit_frames(table, out, dest, now)
                    prune(table, now)
                    last_emit = now
                    if int(now) % 10 == 0:
                        print(f"[bridge] {len(table)} tracked, {n} emitted")
        except (OSError, ConnectionError) as e:
            print(f"[bridge] {type(e).__name__}: {e}; retry in 3s")
            time.sleep(3.0)


def selftest():
    """Parse a couple of synthetic SBS lines, emit to a local socket, and
    confirm the resulting GDL90 decodes back to the same aircraft."""
    table = {}
    now = time.monotonic()
    # MSG type 1 (callsign) then type 3 (position+altitude).
    l1 = "MSG,1,1,1,A8C4F2,1,,,,,N172SP,,,,,,,,,,,"
    l3 = "MSG,3,1,1,A8C4F2,1,,,,,,9500,,,34.8697,-111.7610,,,,,,0"
    parse_sbs_line(l1, table, now)
    parse_sbs_line(l3, table, now)
    assert table["A8C4F2"]["callsign"] == "N172SP", "callsign merged"
    assert abs(table["A8C4F2"]["lat"] - 34.8697) < 1e-4, "lat merged"

    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    rx.bind(("127.0.0.1", 0))
    port = rx.getsockname()[1]
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    n = emit_frames(table, tx, ("127.0.0.1", port), now)
    assert n == 1, f"one frame emitted, got {n}"
    rx.settimeout(2.0)
    data, _ = rx.recvfrom(8192)
    msgs = gdl90.decode_stream(data)
    assert msgs and msgs[0]["icao"] == "A8C4F2", "round-trips to GDL90"
    assert msgs[0]["callsign"] == "N172SP", "callsign survives"
    assert abs(msgs[0]["lat"] - 34.8697) < 1e-3, "position survives"

    # Stale prune.
    prune(table, now + 120, stale_s=60)
    assert table == {}, "stale aircraft pruned"
    rx.close(); tx.close()
    print("BRIDGE SELFTEST PASSED")


def main():
    ap = argparse.ArgumentParser(description="dump1090 SBS-1 → GDL90/UDP bridge")
    ap.add_argument("--sbs-host", default="127.0.0.1")
    ap.add_argument("--sbs-port", type=int, default=30003)
    ap.add_argument("--out-host", default="255.255.255.255",
                    help="UDP destination (broadcast by default)")
    ap.add_argument("--out-port", type=int, default=4000)
    ap.add_argument("--emit-hz", type=float, default=1.0)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        selftest()
        return
    run(args.sbs_host, args.sbs_port, args.out_host, args.out_port,
        args.emit_hz)


if __name__ == "__main__":
    main()
