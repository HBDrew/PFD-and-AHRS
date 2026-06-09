#!/usr/bin/env python3
"""
dump978_gdl90_bridge.py – dump978 (raw UAT uplink) → GDL90/UDP bridge.

FIS-B weather (METAR/TAF/NEXRAD…) arrives on 978 UAT as *uplink* frames from
ground stations.  dump978-fa emits those on its raw message port as lines that
start with ``+`` (downlink / traffic lines start with ``-``).  The PFD already
ingests GDL90 ``Uplink Data`` (0x07) messages off UDP :4000 — ``ADSBClient``
folds them into ``fisb.FisbWeather`` and the display merges them over the
internet METARs.  The one missing link is the *transport*: nothing carried the
dump978 uplink frames onto the wire.  This bridge does exactly that — read the
``+`` lines, wrap each payload in a GDL90 0x07 frame, broadcast to :4000.

It sits alongside the traffic bridge (``adsb_gdl90_bridge.py``, SBS-1 → GDL90);
both fan into the same listener, so traffic and weather can run as two units.

Usage:
    # real: forward dump978's raw uplinks to the display
    python3 tools/dump978_gdl90_bridge.py \
        --raw-host 127.0.0.1 --raw-port 30978 \
        --out-host 255.255.255.255 --out-port 4000

    # prove the display path end-to-end with NO radio: inject a synthetic
    # METAR every few seconds and watch "WX R1 …" light up on the MFD.
    python3 tools/dump978_gdl90_bridge.py --emit-test-wx \
        --out-host 255.255.255.255 --out-port 4000

    python3 tools/dump978_gdl90_bridge.py --selftest      # offline round-trip
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


# ── dump978 raw-line parsing ────────────────────────────────────────────────────
def parse_uplink_line(line):
    """Return the raw UAT uplink payload bytes from one dump978 raw line, or
    ``None`` if the line isn't an uplink (``+``) frame.

    dump978 raw format:  ``+<hex>;<metadata>``  (uplink, FIS-B weather)
                         ``-<hex>;<metadata>``  (downlink, traffic — ignored here)
    The hex between the leading sign and the ``;`` is the message payload.
    """
    line = line.strip()
    if not line or line[0] != "+":
        return None
    hexpart = line[1:].split(";", 1)[0].strip()
    if not hexpart or len(hexpart) % 2:
        return None
    try:
        return bytes.fromhex(hexpart)
    except ValueError:
        return None


def forward_uplink(payload, sock, dest):
    """Wrap a UAT uplink payload in a GDL90 0x07 frame and send it."""
    sock.sendto(gdl90.encode_uplink(payload), dest)


# ── Synthetic test weather (no radio needed) ────────────────────────────────────
_DLAC_REV = {ch: i for i, ch in enumerate(fisb._DLAC)}


def _dlac_encode(text):
    bits = nbits = 0
    out = bytearray()
    for ch in text:
        bits = (bits << 6) | _DLAC_REV.get(ch, _DLAC_REV[" "])
        nbits += 6
        while nbits >= 8:
            nbits -= 8
            out.append((bits >> nbits) & 0xFF)
    if nbits:
        out.append((bits << (8 - nbits)) & 0xFF)
    return bytes(out)


def _encode_uplink_header(lat, lon, site_id=0):
    """8-byte UAT uplink header with a valid ground-station position — inverse
    of fisb.decode_ground_station, so the test feed also exercises the FIS-B
    tower symbol."""
    raw_lat = int(round((lat % 360.0) / 360.0 * 16777216.0)) & 0x7FFFFF
    raw_lon = int(round((lon % 360.0) / 360.0 * 16777216.0)) & 0xFFFFFF
    h = bytearray(8)
    h[0] = (raw_lat >> 15) & 0xFF
    h[1] = (raw_lat >> 7) & 0xFF
    h[2] = ((raw_lat << 1) & 0xFE) | ((raw_lon >> 23) & 0x01)
    h[3] = (raw_lon >> 15) & 0xFF
    h[4] = (raw_lon >> 7) & 0xFF
    h[5] = ((raw_lon << 1) & 0xFE) | 0x01           # position_valid
    h[7] = (site_id & 0x0F) << 4
    return bytes(h)


def make_test_uplink(metar_texts, station=(33.43, -112.01)):
    """Build a 432-byte UAT uplink payload carrying ``metar_texts`` as one
    product-413 (Generic Text) FIS-B APDU, from a ground station at
    ``station`` — the same shape dump978 would hand us for real text weather."""
    text = "\x1e".join(metar_texts) + "\x03"           # RS-separated, ETX-ended
    dlac = _dlac_encode(text)
    pid = 413
    apdu = bytes([(pid >> 6) & 0x1f, (pid & 0x3f) << 2]) + dlac   # T-opt 0
    length = len(apdu)
    info = bytes([(length >> 1) & 0xFF,
                  ((length & 1) << 7) | fisb.FRAME_TYPE_FISB_APDU]) + apdu
    app = (info + b"\x00\x00").ljust(424, b"\x00")[:424]
    header = _encode_uplink_header(station[0], station[1], site_id=1)
    return header + app                                 # 8-byte header + 424


_TEST_METARS = [
    "KSEZ 091753Z 24008KT 10SM FEW120 28/06 A3001",      # VFR
    "KPHX 091751Z 28015G25KT 1 1/2SM BR OVC007 22/19 A2992",  # IFR
    "KFLG 091756Z VRB03KT 1/2SM FG VV002 05/05 A3010",   # LIFR
]


def emit_test_wx(out, dest, period_s=5.0):
    payload = make_test_uplink(_TEST_METARS)
    print(f"[978-bridge] TEST MODE: emitting {len(_TEST_METARS)} synthetic "
          f"METARs to {dest[0]}:{dest[1]} every {period_s:g}s "
          "(KSEZ/KPHX/KFLG — expect 'WX R3 …' on the MFD)")
    while True:
        forward_uplink(payload, out, dest)
        time.sleep(period_s)


# ── Main loop ─────────────────────────────────────────────────────────────────
def run(raw_host, raw_port, out_host, out_port):
    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    out.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    dest = (out_host, out_port)
    while True:
        try:
            print(f"[978-bridge] connecting to dump978 raw "
                  f"{raw_host}:{raw_port}")
            s = socket.create_connection((raw_host, raw_port), timeout=10)
            # create_connection leaves the 10 s connect timeout on the socket;
            # reset to a long idle timeout so a quiet feed (FIS-B is bursty and
            # sparse on the ground) doesn't make recv() time out and tear the
            # connection down — we'd miss frames in the reconnect gaps.  A real
            # disconnect still surfaces as an empty recv below.
            s.settimeout(120.0)
            print(f"[978-bridge] connected; forwarding uplink frames to "
                  f"{out_host}:{out_port} (quiet until a station is in range)")
            buf = b""
            n_up = 0
            last_log = time.monotonic()
            while True:
                try:
                    chunk = s.recv(4096)
                except socket.timeout:
                    continue              # idle feed — stay connected
                if not chunk:
                    raise ConnectionError("dump978 raw feed closed")
                buf += chunk
                while b"\n" in buf:
                    line, _, buf = buf.partition(b"\n")
                    payload = parse_uplink_line(line.decode("ascii", "ignore"))
                    if payload is not None:
                        forward_uplink(payload, out, dest)
                        n_up += 1
                now = time.monotonic()
                if now - last_log >= 30.0 and n_up:
                    print(f"[978-bridge] {n_up} uplink frames forwarded")
                    last_log = now
        except (OSError, ConnectionError) as e:
            print(f"[978-bridge] {type(e).__name__}: {e}; retry in 3s")
            time.sleep(3.0)


def selftest():
    """Round-trip: build a synthetic dump978 ``+`` line, parse it, send a GDL90
    0x07 frame to a local socket, and confirm the display path recovers the
    METARs through the *real* decoder."""
    payload = make_test_uplink(_TEST_METARS)
    line = "+" + payload.hex() + ";rs=1;"
    parsed = parse_uplink_line(line)
    assert parsed == payload, "uplink line parses back to the payload"
    assert parse_uplink_line("-08abcd;") is None, "downlink line ignored"

    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    rx.bind(("127.0.0.1", 0))
    port = rx.getsockname()[1]
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    forward_uplink(parsed, tx, ("127.0.0.1", port))
    rx.settimeout(2.0)
    data, _ = rx.recvfrom(8192)

    msgs = gdl90.decode_stream(data)
    assert msgs and msgs[0]["kind"] == "uplink", "decodes as a GDL90 uplink"
    apdus = fisb.decode_gdl90_uplink(msgs[0])
    coords = {"KSEZ": (34.8485, -111.7884), "KPHX": (33.4343, -112.0116),
              "KFLG": (35.1385, -111.6713)}
    sts = fisb.metars_from_apdus(apdus, lambda i: coords.get(i))
    got = sorted(s["icao"] for s in sts)
    assert got == ["KFLG", "KPHX", "KSEZ"], f"all METARs recovered: {got}"
    cats = {s["icao"]: s["fltcat"] for s in sts}
    assert cats["KPHX"] == "IFR" and cats["KFLG"] == "LIFR", "categories survive"
    rx.close(); tx.close()
    print("978-BRIDGE SELFTEST PASSED")


def main():
    ap = argparse.ArgumentParser(
        description="dump978 raw uplink → GDL90/UDP bridge (FIS-B weather)")
    ap.add_argument("--raw-host", default="127.0.0.1")
    ap.add_argument("--raw-port", type=int, default=30978,
                    help="dump978 raw message port (see /etc/default/dump978-fa)")
    ap.add_argument("--out-host", default="255.255.255.255",
                    help="UDP destination (broadcast by default)")
    ap.add_argument("--out-port", type=int, default=4000)
    ap.add_argument("--emit-test-wx", action="store_true",
                    help="inject synthetic METARs instead of reading dump978")
    ap.add_argument("--test-period", type=float, default=5.0)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        selftest()
        return
    if args.emit_test_wx:
        out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        out.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        emit_test_wx(out, (args.out_host, args.out_port), args.test_period)
        return
    run(args.raw_host, args.raw_port, args.out_host, args.out_port)


if __name__ == "__main__":
    main()
