"""
test_navdata.py – unit tests for the IFR nav-data runtime (shared/navdata.py).

Builds a tiny synthetic cache in a temp dir (no FAA data needed) and exercises
the load + query API.  Run:  python3 shared/test_navdata.py
"""

import os
import sys
import json
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import numpy as np   # noqa: E402
import navdata        # noqa: E402

_passed = 0


def check(cond, msg):
    global _passed
    assert cond, f"FAIL: {msg}"
    _passed += 1


def _build(tmp):
    # Fixes around northern Arizona.
    fixes = np.array([
        ("BANYO", 35.10, -111.90),
        ("DRAGN", 34.90, -111.60),
        ("FLGAF", 35.05, -111.70),   # ~ the FAF
        ("FARAW", 33.00, -118.00),   # far away (LA basin)
    ], dtype=navdata._FIX_DTYPE)
    fixes = fixes[np.argsort(fixes["lat"], kind="stable")]
    np.save(os.path.join(tmp, navdata.FIXES_FILE), fixes)

    navaids = np.array([
        ("DRK", "VOR", 34.70, -111.30, 114.10, "DRAKE"),
        ("FLG", "VOR", 35.14, -111.67, 108.20, "FLAGSTAFF"),
    ], dtype=navdata._NAV_DTYPE)
    navaids = navaids[np.argsort(navaids["lat"], kind="stable")]
    np.save(os.path.join(tmp, navdata.NAVAIDS_FILE), navaids)

    doc = {
        "cycle": "2406",
        "airways": {"V291": ["DRK", "DRAGN", "FLG"]},
        "holds": {"FLGAF": {"course": 30, "turn": "R", "leg_nm": 4, "leg_min": 0}},
        "procedures": {
            "KFLG": {
                "RNAV (GPS) RWY 03": {
                    "type": "RNAV",
                    "transitions": {"BANYO": [
                        {"fix": "BANYO", "leg_type": "IF", "course": None,
                         "alt_ft": 9000, "alt_type": "AB",
                         "lat": 35.10, "lon": -111.90}]},
                    "final": [
                        {"fix": "FLGAF", "leg_type": "TF", "course": 30.0,
                         "alt_ft": 8500, "alt_type": "AT",
                         "lat": 35.05, "lon": -111.70}],
                    "missed": [
                        {"fix": "FLG", "leg_type": "DF", "course": None,
                         "alt_ft": 10000, "alt_type": "AB",
                         "lat": 35.14, "lon": -111.67}],
                },
            }
        },
    }
    with open(os.path.join(tmp, navdata.JSON_FILE), "w", encoding="utf-8") as fh:
        json.dump(doc, fh)


def test_load_and_availability():
    with tempfile.TemporaryDirectory() as tmp:
        _build(tmp)
        nd = navdata.load(tmp)
        check(nd is not None, "cache loads")
        check(nd.has_fixes() and nd.has_navaids() and nd.has_procedures(),
              "all components present")
        check(nd.cycle == "2406", "cycle stamp")
    check(navdata.load("/no/such/dir") is None, "missing dir → None")


def test_by_ident():
    with tempfile.TemporaryDirectory() as tmp:
        _build(tmp)
        nd = navdata.load(tmp)
        f = nd.fix("BANYO")
        check(f and f[0] == "BANYO" and abs(f[1] - 35.10) < 0.01, "fix by ident")
        check(nd.fix("bany o".replace(" ", "")) is not None, "case-insensitive")
        check(nd.fix("NOPE") is None, "unknown fix → None")
        nv = nd.navaid("DRK")
        check(nv and nv[1] == "VOR" and abs(nv[4] - 114.10) < 0.01, "navaid + freq")
        # waypoint() resolves a fix, then falls back to a navaid.
        check(nd.waypoint("DRAGN")[0] == "DRAGN", "waypoint → fix")
        check(nd.waypoint("FLG")[0] == "FLG", "waypoint → navaid fallback")
        check(nd.waypoint("NOPE") is None, "waypoint unknown → None")


def test_spatial():
    with tempfile.TemporaryDirectory() as tmp:
        _build(tmp)
        nd = navdata.load(tmp)
        near = navdata.nearby_fixes(nd, 35.05, -111.70, radius_nm=40)
        idents = [str(r["ident"]) for r in near]
        check("FLGAF" in idents and "BANYO" in idents, "nearby fixes found")
        check("FARAW" not in idents, "far fix excluded")
        check(idents[0] == "FLGAF", "nearest-first ordering")
        nv = navdata.nearby_navaids(nd, 35.05, -111.70, radius_nm=80)
        check("FLG" in [str(r["ident"]) for r in nv], "nearby navaid found")


def test_airways():
    with tempfile.TemporaryDirectory() as tmp:
        _build(tmp)
        nd = navdata.load(tmp)
        aw = nd.airway("V291")
        check([p[0] for p in aw] == ["DRK", "DRAGN", "FLG"], "airway resolves in order")
        seg = nd.airway_between("V291", "FLG", "DRK")
        check([p[0] for p in seg] == ["FLG", "DRAGN", "DRK"], "airway reversed slice")
        check(nd.airway("V999") == [], "unknown airway → []")


def test_procedures_and_holds():
    with tempfile.TemporaryDirectory() as tmp:
        _build(tmp)
        nd = navdata.load(tmp)
        check(nd.procedures_for("KFLG") == ["RNAV (GPS) RWY 03"], "proc list")
        p = nd.procedure("KFLG", "RNAV (GPS) RWY 03")
        check(p and p["type"] == "RNAV", "procedure type")
        check(p["final"][0]["fix"] == "FLGAF", "final approach fix")
        check(p["missed"][0]["leg_type"] == "DF", "missed-approach leg type")
        check(nd.procedure("KFLG", "NOPE") is None, "unknown procedure → None")
        h = nd.hold("FLGAF")
        check(h and h["turn"] == "R" and h["leg_nm"] == 4, "hold params")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"ALL NAVDATA TESTS PASSED ({_passed} checks, {len(tests)} cases)")


if __name__ == "__main__":
    main()
