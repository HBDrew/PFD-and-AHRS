"""
test_adsb.py – standalone unit tests for the ADS-B traffic manager.

Run:  python3 shared/test_adsb.py
"""

import os
import sys
import time
import socket

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import gdl90  # noqa: E402
import adsb   # noqa: E402

_passed = 0


def check(cond, msg):
    global _passed
    assert cond, f"FAIL: {msg}"
    _passed += 1


def approx(a, b, tol):
    return abs(a - b) <= tol


def test_relative_geometry():
    # Target due north, 2 NM, 1000 ft above.
    own = (34.0, -111.0, 8000)
    tlat = 34.0 + 2.0 / 60.0          # 2 NM north
    rel = adsb.relative({"lat": tlat, "lon": -111.0, "alt_ft": 9000}, *own)
    check(approx(rel["range_nm"], 2.0, 0.02), f"range {rel['range_nm']}")
    check(approx(rel["bearing_deg"], 0.0, 1.0) or
          approx(rel["bearing_deg"], 360.0, 1.0), f"bearing {rel['bearing_deg']}")
    check(rel["rel_alt_ft"] == 1000, f"rel_alt {rel['rel_alt_ft']}")
    # Target due east.
    tlon = -111.0 + (2.0 / 60.0) / __import__("math").cos(
        __import__("math").radians(34.0))
    rel = adsb.relative({"lat": 34.0, "lon": tlon, "alt_ft": 8000}, *own)
    check(approx(rel["bearing_deg"], 90.0, 1.0), f"east bearing {rel['bearing_deg']}")


def test_relative_missing_position():
    rel = adsb.relative({"lat": None, "lon": None, "alt_ft": None},
                        34.0, -111.0, 8000)
    check(rel["range_nm"] is None and rel["bearing_deg"] is None, "no position")
    check(rel["rel_alt_ft"] is None, "no rel alt")


def test_threat_levels():
    near = adsb.relative({"lat": 34.0 + 1.0 / 60.0, "lon": -111.0,
                          "alt_ft": 8100}, 34.0, -111.0, 8000)
    check(adsb.threat_level(near) == "alert", "close+co-altitude → alert")
    mid = adsb.relative({"lat": 34.0 + 5.0 / 60.0, "lon": -111.0,
                         "alt_ft": 8800}, 34.0, -111.0, 8000)
    check(adsb.threat_level(mid) == "proximate", "5 NM/800 ft → proximate")
    far = adsb.relative({"lat": 34.0 + 20.0 / 60.0, "lon": -111.0,
                         "alt_ft": 8000}, 34.0, -111.0, 8000)
    check(adsb.threat_level(far) == "other", "20 NM → other")
    flagged = adsb.relative({"lat": 34.5, "lon": -111.0, "alt_ft": 8000,
                             "alert": True}, 34.0, -111.0, 8000)
    check(adsb.threat_level(flagged) == "alert", "source alert flag honoured")


def test_filter_targets():
    own = (34.0, -111.0, 8000)
    near_alert = adsb.relative({"lat": 34.0 + 1.0 / 60.0, "lon": -111.0,
                                "alt_ft": 8100}, *own)
    near_alert["threat"] = "alert"
    co_alt_far = adsb.relative({"lat": 34.0 + 30.0 / 60.0, "lon": -111.0,
                                "alt_ft": 8000}, *own)        # 30 NM, co-alt
    co_alt_far["threat"] = "other"
    high_near = adsb.relative({"lat": 34.0 + 2.0 / 60.0, "lon": -111.0,
                               "alt_ft": 16000}, *own)        # 2 NM, +8000 ft
    high_near["threat"] = "other"
    no_alt = adsb.relative({"lat": 34.0 + 2.0 / 60.0, "lon": -111.0,
                            "alt_ft": None}, *own)
    no_alt["threat"] = "other"
    targets = [near_alert, co_alt_far, high_near, no_alt]

    # No limits → everything passes.
    check(len(adsb.filter_targets(targets)) == 4, "no filter keeps all")

    # ±5000 ft band drops the +8000 ft target, keeps the unknown-altitude one.
    band = adsb.filter_targets(targets, alt_band_ft=5000)
    check(high_near not in band, "high target dropped by alt band")
    check(no_alt in band, "unknown-altitude target kept under alt band")
    check(near_alert in band, "alert target survives alt band")

    # 10 NM range drops the 30 NM target.
    rng = adsb.filter_targets(targets, range_nm=10)
    check(co_alt_far not in rng, "far target dropped by range")
    check(high_near in rng, "near target kept by range")

    # A genuine alert is never decluttered, even with tight filters.
    tight = adsb.filter_targets(targets, alt_band_ft=10, range_nm=0.5)
    check(near_alert in tight, "alert never hidden by filters")
    check(co_alt_far not in tight and high_near not in tight,
          "non-threats hidden by tight filters")
    # keep_alert=False lets even an alert be filtered (not used by the app).
    check(near_alert not in adsb.filter_targets(
        [near_alert], range_nm=0.5, keep_alert=False),
        "keep_alert=False allows filtering threats")


def test_demo_targets_shape():
    ts = adsb.demo_targets(34.0, -111.0, 8000, t=5.0)
    check(len(ts) == 4, f"4 demo targets, got {len(ts)}")
    for d in ts:
        check(d["lat"] is not None and d["lon"] is not None, "demo has position")
        rel = adsb.relative(d, 34.0, -111.0, 8000)
        check(rel["range_nm"] is not None, "demo target relativises")
    # Deterministic in t.
    a = adsb.demo_targets(34.0, -111.0, 8000, t=5.0)
    b = adsb.demo_targets(34.0, -111.0, 8000, t=5.0)
    check(a == b, "demo targets reproducible for same t")


def test_ingest_and_expire():
    c = adsb.ADSBClient(stale_s=0.2)
    f = gdl90.encode_traffic(0xABCDEF, 34.1, -111.1, 7500, callsign="TEST1")
    c._ingest(f)
    check(c.count() == 1, "ingest stored one target")
    snap = c.snapshot()
    check(snap[0]["icao"] == "ABCDEF", "snapshot icao")
    check(snap[0]["callsign"] == "TEST1", "snapshot callsign")
    check(c.rx_count == 1, "rx_count incremented")
    # Position-less report is dropped.
    f0 = gdl90.encode_traffic(0x000111, 0.0, 0.0, 5000)
    c._ingest(f0)
    check(c.count() == 1, "lat/lon 0,0 report dropped")
    # Expiry after the stale window.
    time.sleep(0.25)
    check(c.snapshot() == [], "target expired after stale window")
    check(c.count() == 0, "expired target pruned")


def test_uplink_feeds_fisb_store():
    """A GDL90 0x07 uplink datagram carrying a METAR must increment the FIS-B
    counter *and* land in the client's FIS-B weather store."""
    import fisb
    import test_fisb as tf
    c = adsb.ADSBClient(stale_s=5.0)
    app = tf.build_app_data([tf.build_info_frame(
        fisb.FRAME_TYPE_FISB_APDU,
        tf.build_apdu_notime(413, tf.dlac_encode(
            "KSEZ 091753Z 24008KT 10SM FEW120 28/06 A3001\x03")))])
    body = (bytes([gdl90.MSG_UPLINK]) + b"\x00\x00\x00"
            + tf.build_uplink_payload(app))
    c._ingest(gdl90.frame_message(body))
    check(c.uplink_count == 1, "uplink frame counted")
    check(c.fisb.count() == 1, "METAR folded into the FIS-B store")
    sts = c.fisb.metar_stations(
        lambda i: (34.8485, -111.7884) if i == "KSEZ" else None)
    check(len(sts) == 1 and sts[0]["icao"] == "KSEZ" and sts[0]["src"] == "RDR",
          "store geolocates the METAR as a RDR station")


def test_udp_loopback():
    """End-to-end: bind the listener, send a real UDP datagram to it,
    and confirm the target shows up."""
    port = 47654
    c = adsb.ADSBClient(port=port, stale_s=5.0, bind_addr="127.0.0.1")
    c.start()
    time.sleep(0.3)                                  # let the socket bind
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    frame = gdl90.encode_traffic(0x123456, 34.2, -111.2, 6500,
                                 callsign="UDP1")
    try:
        for _ in range(20):
            tx.sendto(frame, ("127.0.0.1", port))
            time.sleep(0.05)
            if c.count() >= 1:
                break
        snap = c.snapshot()
        check(any(t["icao"] == "123456" for t in snap),
              f"UDP-delivered target present, got {[t['icao'] for t in snap]}")
        check(c.connected is True, "client marked connected after rx")
    finally:
        tx.close()
        c.stop()
        c.join(timeout=2.0)
    check(not c.is_alive(), "client thread stops cleanly")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"ALL ADSB TESTS PASSED ({_passed} checks, {len(tests)} cases)")


if __name__ == "__main__":
    main()
