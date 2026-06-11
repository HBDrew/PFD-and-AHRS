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


def test_peer_feed_is_per_zone():
    # A peer feeding ONE zone must not stop us pulling other still-stale zones
    # (the global defer stalled the refresh into 6-min lurches).
    c = _cache()
    now = time.time()
    for i in range(len(c.zones)):
        c._data[i] = {"cols": [], "fetched": now - 7 * 3600}   # all stale
    c.ingest_packed(wx.pack_winds_zone(0, now, []))            # peer feeds zone 0
    due = c._due_zone(34.0, -112.0, now)
    check(due is not None, "still has a due zone after a peer fed one")
    check(due != 0, "the zone a peer just fed is not re-fetched, but others are")


def test_status_bands_fresh_stale_expired():
    # status() must split zones into fresh (<6h), stale (6h..24h, still drawn)
    # and expired (>=24h, dropped).  age_s reports the oldest STILL-VALID zone.
    c = _cache()
    now = time.time()
    c._data[0] = {"cols": [{}], "fetched": now - 1 * 3600}    # fresh
    c._data[1] = {"cols": [{}], "fetched": now - 8 * 3600}    # stale
    c._data[2] = {"cols": [{}], "fetched": now - 30 * 3600}   # expired
    fresh, total, age_s, stale, expired = c.status()
    check(fresh == 1, "one fresh zone counted")
    check(stale == 1, "one stale zone counted")
    check(expired == 1, "one expired zone counted")
    check(total == len(c.zones), "total is the zone count")
    # oldest VALID zone is the 8 h stale one (the 30 h expired one is excluded).
    check(7.5 * 3600 < age_s < 8.5 * 3600, "age_s is oldest non-expired zone")


def test_expired_zone_not_served():
    # A day-old forecast is worse than nothing — columns()/count() must drop it.
    c = _cache()
    now = time.time()
    c._data[0] = {"cols": [{"a": 1}], "fetched": now - 2 * 3600}    # valid
    c._data[1] = {"cols": [{"b": 2}], "fetched": now - 26 * 3600}   # expired
    check(len(c.columns()) == 1, "expired zone's columns are not served")
    check(c.count() == 1, "expired zone is not counted")


def test_stale_zones_lists_off_indices():
    # stale_zones() names every zone without CURRENT data (stale/expired/unloaded).
    c = _cache()
    now = time.time()
    c._data[0] = {"cols": [], "fetched": now - 1 * 3600}     # fresh -> not listed
    c._data[1] = {"cols": [], "fetched": now - 8 * 3600}     # stale -> listed
    c._data[2] = {"cols": [], "fetched": now - 30 * 3600}    # expired -> listed
    # zones 3..n are never loaded -> listed
    off = c.stale_zones()
    check(0 not in off, "fresh zone is not flagged")
    check(1 in off and 2 in off, "stale and expired zones are flagged")
    check(all(i in off for i in range(3, len(c.zones))),
          "never-loaded zones are flagged too")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"ALL WINDS TESTS PASSED ({_passed} checks, {len(tests)} cases)")


if __name__ == "__main__":
    main()
