"""
test_localtime.py – local-time helpers + TAF local annotation.

Exercises the longitude FALLBACK path (forced on, so the result is
deterministic and independent of whether timezonefinder is installed) plus the
fisb time formatters and parse_taf local annotation.

Run:  python3 shared/test_localtime.py
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import localtime  # noqa: E402
import fisb        # noqa: E402

_passed = 0


def check(cond, msg):
    global _passed
    assert cond, f"FAIL: {msg}"
    _passed += 1


def _force_fallback():
    """Disable the exact path so offset_hours uses round(lon/15)."""
    localtime._tf = None
    localtime._tf_tried = True
    localtime._tz_cache.clear()


def test_longitude_fallback_offsets():
    _force_fallback()
    check(localtime.available() is False, "exact path reported unavailable")
    check(localtime.offset_hours(34.85, -111.79) == -7.0, "Sedona → UTC-7")
    check(localtime.offset_hours(33.94, -118.41) == -8.0, "LA → UTC-8 (no DST in fallback)")
    check(localtime.offset_hours(40.71, -74.01) == -5.0, "NYC → UTC-5")
    check(localtime.offset_hours(51.5, 0.0) == 0.0, "London → UTC+0")
    check(localtime.abbrev(34.85, -111.79) == "", "fallback has no zone abbrev")


def test_offset_none_without_longitude():
    _force_fallback()
    check(localtime.offset_hours(34.0, None) is None, "no lon → None")


def test_lhh_whole_and_fractional():
    check(fisb._lhh("1218", -7) == "11", "18Z - 7h = 11 local")
    check(fisb._lhh("1201", -7) == "18", "01Z - 7h wraps to 18 (prev day)")
    check(fisb._lhh("1218", 5.5) == "23:30", "18Z + 5:30 = 23:30 (half-hour zone)")
    check(fisb._lhh("12xx", -7) == "??", "garbage → ??")


def test_lhhmm_fm_minutes():
    check(fisb._lhhmm("130130", -7) == "18:30", "0130Z - 7h = 18:30 local")
    check(fisb._lhhmm("130000", 5.5) == "05:30", "0000Z + 5:30 = 05:30 local")


def test_parse_taf_local_annotation():
    raw = ("TAF KSEZ 121730Z 1218/1318 24008KT P6SM FEW120 "
           "FM130100 28006KT P6SM SCT250 "
           "TEMPO 1306/1310 BKN040 PROB30 1310/1314 5SM -SHRA")
    # No offset → Zulu only (unchanged behaviour).
    p0 = fisb.parse_taf(raw)
    check("L)" not in p0["periods"][0]["label"], "no offset → no local suffix")
    # With offset → each period label gains a local annotation.
    p = fisb.parse_taf(raw, local_offset_h=-7)
    labels = [g["label"] for g in p["periods"]]
    check(any("(11–11L)" in l for l in labels), f"INITIAL local 11-11, got {labels}")
    check(any("From 01:00Z  (18:00L)" == l for l in labels),
          f"FM local 18:00, got {labels}")
    check(any("Temp 06Z–10Z  (23–03L)" == l for l in labels),
          f"TEMPO local 23-03, got {labels}")
    check(any(l.startswith("30% 10Z–14Z") and "(03–07L)" in l for l in labels),
          f"PROB local 03-07, got {labels}")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"ALL LOCALTIME TESTS PASSED ({_passed} checks, {len(tests)} cases)")


if __name__ == "__main__":
    main()
