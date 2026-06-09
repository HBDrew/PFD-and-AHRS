"""
test_mapoverlay.py – unit tests for the map overlay quick-cycle.

Run:  python3 shared/test_mapoverlay.py
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import mapoverlay as ovl  # noqa: E402

_passed = 0


def check(cond, msg):
    global _passed
    assert cond, f"FAIL: {msg}"
    _passed += 1


def test_default_state():
    check(ovl.state({}) == "tfc", "empty ds = traffic-only")
    check(ovl.label({}) == "TFC", "default label TFC")


def test_cycle_sequence():
    ds = {}
    seq = [ovl.cycle(ds) for _ in range(6)]
    # From tfc → wx → wnd → nexrad → asp → tfc → wx
    # (ORDER = asp, tfc, wx, wnd, nexrad)
    check(seq == ["wx", "wnd", "nexrad", "asp", "tfc", "wx"], f"cycle seq {seq}")


def test_apply_exclusive():
    ds = {}
    ovl.apply(ds, "wx")
    check(ds["map_show_metar"] and not ds["map_show_airspaces"]
          and not ds["map_show_nexrad"], "wx exclusive")
    ovl.apply(ds, "asp")
    check(ds["map_show_airspaces"] and not ds["map_show_metar"]
          and not ds["map_show_nexrad"], "asp exclusive")
    ovl.apply(ds, "tfc")
    check(not any(ds[k] for k in ("map_show_metar", "map_show_airspaces",
                                  "map_show_nexrad")), "tfc clears all")


def test_multi_collapses():
    ds = {"map_show_metar": True, "map_show_airspaces": True}
    check(ovl.state(ds) == "multi", "two overlays = multi")
    check(ovl.label(ds) == "MULTI", "multi label")
    nxt = ovl.cycle(ds)
    check(nxt == ovl.ORDER[0] == "asp", "multi collapses to first")
    check(ovl.state(ds) == "asp", "now single asp")


def test_labels():
    ds = {}
    ovl.apply(ds, "nexrad")
    check(ovl.label(ds) == "NEX", "nexrad label")
    ovl.apply(ds, "wx")
    check(ovl.label(ds) == "MET", "metar label")


def test_traffic_and_base_untouched():
    ds = {"map_show_traffic": True, "map_show_terrain": True,
          "map_show_airports": True}
    for _ in range(4):
        ovl.cycle(ds)
        check(ds["map_show_traffic"] is True, "traffic never touched")
        check(ds["map_show_terrain"] is True, "terrain never touched")
        check(ds["map_show_airports"] is True, "airports never touched")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"ALL MAPOVERLAY TESTS PASSED ({_passed} checks, {len(tests)} cases)")


if __name__ == "__main__":
    main()
