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
    # Per-device stagger: when several screens hold equally-stale SERIES data,
    # the lower-jitter one becomes "due" first (it fetches + feeds the rest); the
    # higher-jitter one waits, so they don't all hit Open-Meteo at once.
    now = time.time()
    a, b = _cache(), _cache()
    a._fetch_jitter = 10.0
    b._fetch_jitter = 120.0
    age_s = 6 * 3600 + 60          # 6 h 1 m — between the two thresholds
    for c in (a, b):
        col, _ = _series_col(now - age_s)
        for i in range(len(c.zones)):
            c._data[i] = {"cols": [col], "fetched": now - age_s}
    check(a._due_zone(34.0, -112.0, now) is not None,
          "low-jitter screen is due first (becomes the feeder)")
    check(b._due_zone(34.0, -112.0, now) is None,
          "high-jitter screen waits (adopts the feeder's data instead)")


def test_seriesless_cache_is_due():
    # A pre-series disk cache (or a peer snapshot) has no series — it must be
    # treated as due regardless of age, so a deploy re-pulls to populate the
    # series (and the +Nh offset starts working / screens reconcile).
    c = _cache()
    now = time.time()
    for i in range(len(c.zones)):
        c._data[i] = {"cols": [_snap_col(25)], "fetched": now}   # fresh, NO series
    check(c._due_zone(34.0, -112.0, now) is not None,
          "series-less data is due even when its timestamp looks fresh")
    # But once it has a fresh series, it's not due (re-pull on the 6 h cadence).
    col, _ = _series_col(now)
    for i in range(len(c.zones)):
        c._data[i] = {"cols": [col], "fetched": now}
    check(c._due_zone(34.0, -112.0, now) is None,
          "a fresh series is not due")


def test_pure_adopter_defers_to_feeder():
    # One feeder, not three: with a live feeder (peer active) and NO series of
    # our own, we must not fetch at all — the rest adopt.  This is what stops the
    # 3-way pile-onto-the-same-zone that tripped the 429.
    c = _cache()
    now = time.time()
    for i in range(len(c.zones)):
        c._data[i] = {"cols": [_snap_col(20)], "fetched": now}   # snapshots, no series
    c._last_peer_rx = time.monotonic()           # a feeder is actively sharing
    check(c._due_zone(34.0, -112.0, now) is None,
          "pure adopter defers entirely while a feeder is active")
    c._last_peer_rx = 0.0                         # no feeder around
    check(c._due_zone(34.0, -112.0, now) is not None,
          "with no feeder, a series-less screen bootstraps (becomes the feeder)")


def test_feeder_still_pulls_other_zones():
    # A FEEDER (already holds its own series) isn't blocked from pulling other
    # still-series-less zones just because a peer fed one of them.
    c = _cache()
    now = time.time()
    own, _ = _series_col(now)
    c._data[0] = {"cols": [own], "fetched": now}                 # our own fresh series
    for i in range(1, len(c.zones)):
        c._data[i] = {"cols": [_snap_col(20)], "fetched": now - 7 * 3600}  # stale, no series
    p = wx.pack_winds_zone(1, now, [_snap_col(30)]); p["st"] = now
    c.ingest_packed(p)                                           # a peer feeds zone 1
    due = c._due_zone(34.0, -112.0, now)
    check(due is not None, "feeder still has a due zone")
    check(due not in (0, 1),
          "not zone 0 (own fresh series) nor zone 1 (a peer just fed it)")


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


def _series_col(now, spd0=20, n=31, alt=6000):
    """A column carrying a forecast series whose speed grows 1 kt per hour."""
    t0 = (int(now) // 3600) * 3600
    rows = [[270, spd0 + i, -5] for i in range(n)]
    return {"lat": 34.0, "lon": -112.0, "station": "x", "alts": [alt],
            "t0": t0, "step_s": 3600, "series": rows, "levels": []}, t0


def _snap_col(spd, alt=6000):
    return {"lat": 34.0, "lon": -112.0,
            "levels": [{"alt_ft": alt, "dir": 270, "spd": spd, "temp": -5,
                        "lv": False}]}


def test_series_retargets_by_time():
    # The picture rolls forward to the right hour on its own: the same stored
    # column reads a different (correct) hour as now advances.
    now = time.time()
    col, t0 = _series_col(now)
    check(wx.winds_levels_at(col, t0)[0]["spd"] == 20, "series reads the now hour")
    check(wx.winds_levels_at(col, t0 + 6 * 3600)[0]["spd"] == 26,
          "series rolls forward to +6 h")
    check(wx.winds_levels_at(col, t0 + 50 * 3600) == [],
          "a target outside the held window blanks (no wrong-time forecast)")


def test_peer_snapshot_rolls_forward():
    # An adopting screen (no series of its own) must roll its barbs forward as
    # the feeder re-broadcasts a fresher now-snapshot of the SAME model run.
    c = _cache()
    run = time.time() - 1800
    p = wx.pack_winds_zone(0, run, [_snap_col(20)]); p["st"] = run
    c.ingest_packed(p)
    check(c._data[0]["cols"][0]["levels"][0]["spd"] == 20, "first snapshot adopted")
    p2 = wx.pack_winds_zone(0, run, [_snap_col(33)]); p2["st"] = run + 600
    c.ingest_packed(p2)
    check(c._data[0]["cols"][0]["levels"][0]["spd"] == 33,
          "newer snapshot of the same run rolls the barbs forward")
    # A relayed (frozen) snapshot carries an older st — must be ignored, so a
    # dead feeder's relays can't pin us (the deferral lapses → we re-pull).
    p3 = wx.pack_winds_zone(0, run, [_snap_col(20)]); p3["st"] = run + 300
    c.ingest_packed(p3)
    check(c._data[0]["cols"][0]["levels"][0]["spd"] == 33,
          "an older relayed snapshot is ignored")


def test_series_holder_ignores_peer_snapshot():
    # A screen that fetched (holds the full series) must NOT downgrade to a
    # peer's single-hour snapshot — even a newer run — or it loses the series
    # (and with it the page offset / roll-forward).
    c = _cache()
    now = time.time()
    col, _ = _series_col(now)
    c._data[0] = {"cols": [col], "fetched": now}
    p = wx.pack_winds_zone(0, now + 1000, [_snap_col(99)]); p["st"] = now + 1000
    c.ingest_packed(p)
    check("series" in c._data[0]["cols"][0], "kept our own series")
    check(not c._peer_active(), "ignoring the snapshot doesn't mark a peer active")


def test_zone_packet_is_now_snapshot():
    # A feeder shares a compact single-hour snapshot stamped st=now (so adopters
    # roll forward), not the whole series (too big for one datagram).
    c = _cache()
    now = time.time()
    col, _ = _series_col(now)
    c._data[0] = {"cols": [col], "fetched": now}
    pkt = c._zone_packet(0)
    check(pkt is not None and "st" in pkt, "feeder packet carries a snapshot time")
    check(abs(pkt["st"] - now) < 5, "feeder stamps st = now")
    _idx, _ts, cols = wx.unpack_winds_zone(pkt)
    check(bool(cols and cols[0]["levels"]), "packet carries a single-hour snapshot")
    check("series" not in cols[0], "the heavy series is not shared over the LAN")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"ALL WINDS TESTS PASSED ({_passed} checks, {len(tests)} cases)")


if __name__ == "__main__":
    main()
