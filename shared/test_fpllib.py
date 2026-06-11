"""
test_fpllib.py – unit tests for the tombstone-aware saved-plan merge.

Run:  python3 shared/test_fpllib.py
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import fpllib  # noqa: E402

_passed = 0


def check(cond, msg):
    global _passed
    if not cond:
        raise AssertionError("FAIL: " + msg)
    _passed += 1


def _names(plans):
    return sorted(p["name"].upper() for p in plans)


def test_union_no_deletes():
    a = [{"name": "ALPHA", "waypoints": [], "ts": 10.0}]
    b = [{"name": "BRAVO", "waypoints": [], "ts": 11.0}]
    plans, deleted = fpllib.merge_plan_lib(a, {}, b, {}, now=20.0)
    check(_names(plans) == ["ALPHA", "BRAVO"], "union keeps both plans")
    check(deleted == {}, "no tombstones")


def test_delete_sticks():
    # A deleted ALPHA (tombstone t=15); a peer B still broadcasts ALPHA (t=10).
    local_plans = []
    local_deleted = {"ALPHA": 15.0}
    peer_plans = [{"name": "ALPHA", "waypoints": [], "ts": 10.0}]
    plans, deleted = fpllib.merge_plan_lib(local_plans, local_deleted,
                                           peer_plans, {}, now=20.0)
    check(_names(plans) == [], "deleted plan does not resurrect from peer")
    check("ALPHA" in deleted, "tombstone retained")


def test_delete_propagates_to_peer():
    # B still has ALPHA but receives A's tombstone → drops it.
    local_plans = [{"name": "ALPHA", "waypoints": [], "ts": 10.0}]
    peer_deleted = {"ALPHA": 15.0}
    plans, deleted = fpllib.merge_plan_lib(local_plans, {}, [], peer_deleted,
                                           now=20.0)
    check(_names(plans) == [], "peer tombstone removes our live plan")
    check(deleted.get("ALPHA") == 15.0, "adopted peer tombstone")


def test_recreate_wins_over_tombstone():
    # We hold a tombstone (t=15) but a peer re-created ALPHA later (t=30).
    local_deleted = {"ALPHA": 15.0}
    peer_plans = [{"name": "ALPHA", "waypoints": [{"ident": "KSEZ"}],
                   "ts": 30.0}]
    plans, deleted = fpllib.merge_plan_lib([], local_deleted, peer_plans, {},
                                           now=40.0)
    check(_names(plans) == ["ALPHA"], "newer re-create beats the tombstone")
    check("ALPHA" not in deleted, "tombstone cleared by the re-create")


def test_lww_newer_version():
    a = [{"name": "ALPHA", "waypoints": [{"ident": "OLD"}], "ts": 10.0}]
    b = [{"name": "ALPHA", "waypoints": [{"ident": "NEW"}], "ts": 20.0}]
    plans, _ = fpllib.merge_plan_lib(a, {}, b, {}, now=30.0)
    check(len(plans) == 1, "one ALPHA after LWW")
    check(plans[0]["waypoints"][0]["ident"] == "NEW", "newer ts wins")


def test_tombstone_expiry():
    # A tombstone older than the TTL is forgotten, so the plan can return.
    local_deleted = {"ALPHA": 100.0}
    peer_plans = [{"name": "ALPHA", "waypoints": [], "ts": 50.0}]
    plans, deleted = fpllib.merge_plan_lib(
        [], local_deleted, peer_plans, {},
        now=100.0 + fpllib.DEFAULT_TTL_S + 1.0)
    check(_names(plans) == ["ALPHA"], "expired tombstone lets plan return")
    check(deleted == {}, "expired tombstone dropped")


def test_cap():
    plans_in = [{"name": f"P{i}", "waypoints": [], "ts": float(i)}
                for i in range(10)]
    plans, _ = fpllib.merge_plan_lib(plans_in, {}, [], {}, now=100.0,
                                     max_plans=3)
    check(len(plans) == 3, "capped to max_plans")
    check(_names(plans) == ["P7", "P8", "P9"], "cap keeps the newest by ts")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"ALL FPLLIB TESTS PASSED ({_passed} checks, {len(tests)} cases)")


if __name__ == "__main__":
    main()
