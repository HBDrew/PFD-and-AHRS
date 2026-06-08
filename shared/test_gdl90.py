"""
test_gdl90.py – standalone unit tests for the GDL90 decoder.

No pytest dependency (matches the repo's other test scripts).  Run:

    python3 shared/test_gdl90.py

Exits non-zero on the first failed assertion.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import gdl90  # noqa: E402

_passed = 0


def check(cond, msg):
    global _passed
    assert cond, f"FAIL: {msg}"
    _passed += 1


def approx(a, b, tol):
    return abs(a - b) <= tol


# ── CRC against the worked example in the GDL90 spec ──────────────────────────
# Heartbeat message 00 81 41 DB D0 08 02 frames as
#   7E 00 81 41 DB D0 08 02 B3 8B 7E
# The CRC is appended LSB-first (B3 then 8B), so the CRC *integer* is 0x8BB3.
def test_crc_spec_vector():
    payload = bytes([0x00, 0x81, 0x41, 0xDB, 0xD0, 0x08, 0x02])
    crc = gdl90.crc_compute(payload)
    check(crc == 0x8BB3, f"spec heartbeat CRC expected 0x8BB3 got 0x{crc:04X}")
    frame = gdl90.frame_message(payload)
    check(frame[-3] == 0xB3 and frame[-2] == 0x8B,
          "CRC bytes appended LSB-first as B3 8B")


def test_frame_roundtrip_crc():
    payload = bytes([0x00, 0x81, 0x41, 0xDB, 0xD0, 0x08, 0x02])
    frame = gdl90.frame_message(payload)
    check(frame[0] == 0x7E and frame[-1] == 0x7E, "frame is flag-delimited")
    msgs = list(gdl90.iter_frames(frame))
    check(len(msgs) == 1, "exactly one frame decoded")
    check(msgs[0] == payload, "round-trip payload matches")


def test_byte_stuffing():
    # Payload containing both reserved bytes must survive stuffing.
    payload = bytes([0x14, 0x7E, 0x7D, 0x00, 0x7E])
    frame = gdl90.frame_message(payload)
    # The interior must not contain a bare 0x7E except as the flags.
    interior = frame[1:-1]
    check(0x7E not in interior, "stuffed interior has no bare flag byte")
    msgs = list(gdl90.iter_frames(frame))
    check(msgs and msgs[0] == payload, "stuffed payload round-trips")


def test_traffic_decode_roundtrip():
    frame = gdl90.encode_traffic(
        address=0xA8C4F2, lat=34.8697, lon=-111.7610, alt_ft=8500,
        gs_kt=145, track_deg=270.0, vvel_fpm=-640, callsign="N172SP",
        emitter=1, alert=True)
    msgs = gdl90.decode_stream(frame)
    check(len(msgs) == 1 and msgs[0]["kind"] == "traffic", "one traffic msg")
    t = msgs[0]
    check(t["icao"] == "A8C4F2", f"icao {t['icao']}")
    check(t["address"] == 0xA8C4F2, "address int")
    check(approx(t["lat"], 34.8697, 0.001), f"lat {t['lat']}")
    check(approx(t["lon"], -111.7610, 0.001), f"lon {t['lon']}")
    # 25 ft altitude quantisation → within half a step of the input.
    check(approx(t["alt_ft"], 8500, 13), f"alt {t['alt_ft']}")
    check(t["gs_kt"] == 145, f"gs {t['gs_kt']}")
    check(approx(t["track_deg"], 270.0, 1.5), f"track {t['track_deg']}")
    # 64 fpm quantisation.
    check(approx(t["vvel_fpm"], -640, 64), f"vvel {t['vvel_fpm']}")
    check(t["callsign"] == "N172SP", f"callsign '{t['callsign']}'")
    check(t["alert"] is True, "alert flag set")
    check(t["airborne"] is True, "airborne flag set")


def test_negative_latlon():
    # Southern + western hemisphere round-trip (two's-complement path).
    frame = gdl90.encode_traffic(0x010203, lat=-33.95, lon=-118.40,
                                 alt_ft=2000)
    t = gdl90.decode_stream(frame)[0]
    check(approx(t["lat"], -33.95, 0.001), f"neg lat {t['lat']}")
    check(approx(t["lon"], -118.40, 0.001), f"neg lon {t['lon']}")


def test_invalid_altitude():
    frame = gdl90.encode_traffic(0x010203, lat=0.0, lon=0.0, alt_ft=None)
    t = gdl90.decode_stream(frame)[0]
    check(t["alt_ft"] is None, "invalid altitude decodes to None")


def test_ownship_msg_id():
    frame = gdl90.encode_traffic(0x404040, lat=40.0, lon=-105.0, alt_ft=5400,
                                 msg_id=gdl90.MSG_OWNSHIP)
    t = gdl90.decode_stream(frame)[0]
    check(t["kind"] == "ownship", "ownship kind from 0x0A id")


def test_multiple_frames_one_datagram():
    f1 = gdl90.encode_traffic(0x111111, 34.0, -111.0, 7000, callsign="AAA")
    f2 = gdl90.encode_traffic(0x222222, 35.0, -112.0, 9000, callsign="BBB")
    msgs = gdl90.decode_stream(f1 + f2)
    check(len(msgs) == 2, f"two frames in one datagram, got {len(msgs)}")
    check({m["icao"] for m in msgs} == {"111111", "222222"}, "both addrs")


def test_shared_flag_frames():
    # Some emitters share a single 0x7E between back-to-back frames.
    f1 = gdl90.encode_traffic(0x111111, 34.0, -111.0, 7000)
    f2 = gdl90.encode_traffic(0x222222, 35.0, -112.0, 9000)
    joined = f1[:-1] + f2          # drop f1's trailing flag, f2 opens with one
    msgs = gdl90.decode_stream(joined)
    check(len(msgs) == 2, f"shared-flag framing decodes 2, got {len(msgs)}")


def test_corrupt_crc_dropped():
    frame = bytearray(gdl90.encode_traffic(0x333333, 34.0, -111.0, 7000))
    frame[5] ^= 0xFF              # flip a payload byte → CRC mismatch
    msgs = gdl90.decode_stream(bytes(frame))
    check(len(msgs) == 0, "corrupt frame dropped on CRC check")


def test_partial_datagram_no_crash():
    # A truncated frame (no closing flag) must not raise and must yield
    # nothing — the listener will get the rest in the next datagram.
    frame = gdl90.encode_traffic(0x444444, 34.0, -111.0, 7000)
    msgs = gdl90.decode_stream(frame[:len(frame) // 2])
    check(msgs == [], "truncated frame yields nothing without crashing")


def test_heartbeat_decode():
    payload = bytes([0x00, 0x81, 0x41, 0xDB, 0xD0, 0x08, 0x02])
    msgs = gdl90.decode_stream(gdl90.frame_message(payload))
    check(len(msgs) == 1 and msgs[0]["kind"] == "heartbeat", "heartbeat kind")
    check(msgs[0]["gps_valid"] is True, "heartbeat gps_valid bit")


def test_uplink_captured():
    payload = bytes([gdl90.MSG_UPLINK]) + bytes(range(40))
    msgs = gdl90.decode_stream(gdl90.frame_message(payload))
    check(len(msgs) == 1 and msgs[0]["kind"] == "uplink", "uplink captured")
    check(msgs[0]["len"] == 41, "uplink length surfaced")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"ALL GDL90 TESTS PASSED ({_passed} checks, {len(tests)} cases)")


if __name__ == "__main__":
    main()
