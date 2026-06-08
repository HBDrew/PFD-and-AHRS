"""
test_nexrad.py – unit tests for the NEXRAD fetch layer (no pygame, no net).

Run:  python3 shared/test_nexrad.py
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import nexrad  # noqa: E402

_passed = 0


def check(cond, msg):
    global _passed
    assert cond, f"FAIL: {msg}"
    _passed += 1


def test_bbox():
    w, s, e, n = nexrad.bbox_for(34.0, -111.0, 60.0)
    check(abs((n - s) - 2.0) < 1e-6, f"lat span 2° for 60 NM, got {n - s}")
    # lon span widened by 1/cos(lat).
    check(e - w > n - s, "lon span wider than lat span (cos correction)")
    check(w < -111.0 < e and s < 34.0 < n, "center inside bbox")


def test_wms_url():
    url = nexrad.wms_url((-112.0, 33.0, -110.0, 35.0), 480, 480)
    check("nexrad-n0q" in url, "layer in url")
    check("4326" in url, "srs in url (EPSG:4326, colon url-encoded)")
    check("transparent=true" in url, "transparent in url")
    # WMS 1.1.1 bbox order = west,south,east,north.
    check("bbox=-112.00000%2C33.00000%2C-110.00000%2C35.00000" in url
          or "bbox=-112.00000,33.00000,-110.00000,35.00000" in url,
          f"bbox W,S,E,N order in url: {url}")


def test_image_size():
    # Wider-than-tall bbox → width capped at max, height scaled down.
    w, h = nexrad.image_size((-112.0, 34.0, -110.0, 35.0), max_px=480)
    check(w == 480 and h == 240, f"2°×1° → 480×240, got {w}×{h}")
    w, h = nexrad.image_size((-111.0, 33.0, -110.0, 35.0), max_px=480)
    check(h == 480 and w == 240, f"1°×2° → 240×480, got {w}×{h}")


def test_client_fetch_and_seq():
    calls = {"n": 0}

    def fake(lat, lon, radius, max_px):
        calls["n"] += 1
        return (b"PNG" + bytes([calls["n"]]),
                nexrad.bbox_for(lat, lon, radius))

    c = nexrad.NexradClient(view_fn=lambda: (34.0, -111.0, 100.0),
                            fetch_fn=fake)
    png, bbox, seq = c.snapshot()
    check(png is None and seq == 0, "empty before first fetch")
    c._fetch(34.0, -111.0, 100.0)
    png, bbox, seq = c.snapshot()
    check(png == b"PNG\x01" and seq == 1, "first image + seq bump")
    check(bbox is not None and c.rx_count == 1 and c.connected, "counters")
    c._fetch(34.0, -111.0, 100.0)
    _, _, seq2 = c.snapshot()
    check(seq2 == 2, "seq increments on each new image")


def test_client_view_following():
    c = nexrad.NexradClient(view_fn=lambda: (34.0, -111.0, 100.0),
                            fetch_fn=lambda *a: (b"x", (0, 0, 1, 1)),
                            move_refetch_frac=0.4)
    now = 1000.0
    check(c._should_fetch(34.0, -111.0, 100.0, now), "first fetch always")
    c._fetch(34.0, -111.0, 100.0)
    c._fetched_at = now
    check(not c._should_fetch(34.0, -111.0, 100.0, now + 1), "parked: no")
    check(c._should_fetch(36.0, -111.0, 100.0, now + 1), "panned: yes")
    check(c._should_fetch(34.0, -111.0, 220.0, now + 1), "zoomed: yes")
    check(c._should_fetch(34.0, -111.0, 100.0, now + 9999), "periodic: yes")
    check(c.paused is True, "starts paused (off until selected)")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"ALL NEXRAD TESTS PASSED ({_passed} checks, {len(tests)} cases)")


if __name__ == "__main__":
    main()
