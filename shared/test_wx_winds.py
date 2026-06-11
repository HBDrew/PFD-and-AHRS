"""
test_wx_winds.py – the WindsUSCache peer-defer logic.

Regression for the "everyone waits on someone else" deadlock: a stale peer
rebroadcast must NOT mark a peer active (or no screen ever re-pulls); only a
genuinely fresher zone should suppress our own fetch.

Run:  python3 shared/test_wx_winds.py
"""

import os
import sys
import time
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import wx  # noqa: E402

_passed = 0


def check(cond, msg):
    global _passed
    if not cond:
        raise AssertionError("FAIL: " + msg)
    _passed += 1


def _cache():
    d = tempfile.mkdtemp()
    return wx.WindsUSCache(
        bbox=(24.0, 50.0, -125.0, -66.0), rows=2, cols=3, spacing_nm=60.0,
        disk_path=os.path.join(d, "winds.json"),
        locate_fn=lambda: (34.0, -112.0), peer_grace_s=360.0,
        fetch_jitter_s=0.0)   # deterministic unless a test sets _fetch_jitter


def test_stale_peer_does_not_suppress_fetch():
    c = _cache()
    now = time.time()
    # We already hold zone 0 fetched 7 h ago (stale, > max_age_s 6 h).
    c._data[0] = {"cols": [], "fetched": now - 7 * 3600}
    check(not c._peer_active(), "no peer active initially")

    # A peer rebroadcasts the SAME (or older) stale zone — must NOT adopt and
    # must NOT mark a peer active, so our own fetch isn't suppressed.
    c.ingest_packed(wx.pack_winds_zone(0, now - 8 * 3600, []))
    check(not c._peer_active(), "stale peer rebroadcast does not mark peer active")
    check(c._data[0]["fetched"] == now - 7 * 3600, "stale peer zone not adopted")


def test_fresh_peer_suppresses_fetch():
    c = _cache()
    now = time.time()
    c._data[0] = {"cols": [], "fetched": now - 7 * 3600}
    # A peer feeds a FRESHER zone → adopt it AND defer (peer is feeding us).
    c.ingest_packed(wx.pack_winds_zone(0, now, []))
    check(c._peer_active(), "fresh peer feed marks peer active (we defer)")
    check(c._data[0]["fetched"] > now - 60.0, "fresher peer zone adopted")


def test_freshest_stale_screen_still_fetches():
    # Two screens, both stale, exchanging broadcasts.  The one holding the
    # freshest (but still stale) data must NOT defer to the other's older
    # rebroadcast — so it re-pulls and becomes the feeder (deadlock broken).
    a, b = _cache(), _cache()
    now = time.time()
    a._data[0] = {"cols": [], "fetched": now - 9 * 3600}    # fresher-stale
    b._data[0] = {"cols": [], "fetched": now - 10 * 3600}   # older-stale
    a.ingest_packed(wx.pack_winds_zone(0, b._data[0]["fetched"], []))  # b's (older)
    b.ingest_packed(wx.pack_winds_zone(0, a._data[0]["fetched"], []))  # a's (newer)
    check(not a._peer_active(), "freshest-stale screen doesn't defer → it fetches")


def test_fetch_jitter_staggers_due_time():
    # Per-device stagger: when several screens hold equally-stale data, the
    # lower-jitter one becomes "due" first (it fetches + feeds the rest); the
    # higher-jitter one waits, so they don't all hit Open-Meteo at once.
    now = time.time()
    a, b = _cache(), _cache()
    a._fetch_jitter = 10.0
    b._fetch_jitter = 120.0
    age_s = 6 * 3600 + 60          # 6 h 1 m — between the two thresholds
    for c in (a, b):
        for i in range(len(c.zones)):
            c._data[i] = {"cols": [], "fetched": now - age_s}
    check(a._due_zone(34.0, -112.0, now) is not None,
          "low-jitter screen is due first (becomes the feeder)")
    check(b._due_zone(34.0, -112.0, now) is None,
          "high-jitter screen waits (adopts the feeder's data instead)")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"ALL WINDS TESTS PASSED ({_passed} checks, {len(tests)} cases)")


if __name__ == "__main__":
    main()
