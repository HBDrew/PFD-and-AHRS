#!/usr/bin/env python3
"""
diag_navdata.py — dump what the *device's own* nav-data cache actually holds
for an airport, so picker/approach bugs can be diagnosed against real data
(not synthetic test seeds).

Run it ON THE DEVICE (or anywhere the cache lives):

    python3 tools/diag_navdata.py KFLG
    python3 tools/diag_navdata.py KPHX KFLG

It prints, per airport:
  • every procedure in the cache, its `type`, and whether the approach picker
    (_appr_published's type+name filter) would KEEP or DROP it — so a SID
    leaking into the approach list, or a real approach being filtered out, is
    obvious at a glance;
  • the runway DB rows (le/he idents) so a runway-marker mismatch shows up.

Paste the output back and the exact cause is no longer a guess.
"""
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "shared"))
sys.path.insert(0, os.path.join(_ROOT, "pi4"))

import navdata as nd_mod          # noqa: E402
import runways as rwy_mod         # noqa: E402

# Mirror pfd.py's approach-list filter exactly so this reports what the UI does.
_APPR_NAME_RE = re.compile(
    r"\b(?:RWY|RNAV|ILS|LOC|VOR|NDB|GPS|GLS|LDA|TACAN|RNP|SDF|MLS)\b", re.I)


def _kept_by_picker(pid, p):
    if not p or p.get("type") in ("SID", "STAR"):
        return False
    return bool(_APPR_NAME_RE.search(pid))


def _candidate_dirs(kind):
    """Where the cache might live (pi4 first, then pi_zero)."""
    return [os.path.join(_ROOT, "pi4", "data", kind),
            os.path.join(_ROOT, "pi_zero", "data", kind)]


def _load(kind, loader):
    for d in _candidate_dirs(kind):
        if os.path.isdir(d):
            obj = loader(d)
            if obj is not None:
                return obj, d
    return None, None


def main(airports):
    nav, nav_dir = _load("navdata", nd_mod.load)
    rwys, rwy_dir = _load("airports", rwy_mod.load)
    print(f"navdata cache : {nav_dir or 'NOT FOUND'}")
    print(f"runway  cache : {rwy_dir or 'NOT FOUND'}")
    if nav is None:
        print("\nNo nav-data cache found — download it on the DATA & MAPS page first.")
        return
    print(f"cycle: {getattr(nav, 'cycle', '?')}\n")

    for ap in airports:
        ap = ap.strip().upper()
        print("=" * 70)
        print(f"AIRPORT {ap}")
        print("=" * 70)
        pids = nav.procedures_for(ap)
        if not pids:
            print("  (no procedures in cache for this airport)")
        def _fmt(lg):
            """fix[leg_type crs/turn] — shows what drives holds + courses."""
            f = lg.get("fix") or "·"
            lt = lg.get("leg_type") or ""
            extra = []
            if lg.get("course") is not None:
                extra.append(f"{lg['course']:.0f}°")
            if lg.get("turn"):
                extra.append(f"{lg['turn']}turn")
            tag = f"[{lt}{(' ' + ' '.join(extra)) if extra else ''}]" if lt else ""
            return f"{f}{tag}"

        for pid in pids:
            p = nav.procedure(ap, pid) or {}
            keep = _kept_by_picker(pid, p)
            transd = p.get("transitions") or {}
            fin = [_fmt(lg) for lg in (p.get("final") or [])]
            mis = [_fmt(lg) for lg in (p.get("missed") or [])]
            flag = "KEEP (approach list)" if keep else "drop"
            print(f"\n  {pid!r}")
            print(f"      type={p.get('type')!r}   picker={flag}")
            for tname, tlegs in transd.items():
                print(f"      trans[{tname}] ={[_fmt(lg) for lg in tlegs]}")
            print(f"      final ={fin}")
            print(f"      missed={mis}")
            # Any fix that will render a holding pattern (HM/HF/HA leg or a
            # published hold entry) — scans transitions + final + missed, so a
            # HILPT hiding in a transition (e.g. at SEZCY) shows up here too.
            holdy = []
            all_legs = [lg for tl in transd.values() for lg in tl]
            all_legs += (p.get("final") or []) + (p.get("missed") or [])
            for lg in all_legs:
                fx = lg.get("fix")
                if (lg.get("leg_type") or "").upper() in ("HM", "HF", "HA", "PI") \
                        or (fx and nav.hold(fx)):
                    holdy.append(f"{fx}({lg.get('leg_type')} crs={lg.get('course')} "
                                 f"turn={lg.get('turn')})")
            if holdy:
                print(f"      HOLDS  ={holdy}")

        # Runway DB rows for this airport (zero-padding etc. visible here).
        if rwys is not None and hasattr(rwys, "dtype"):
            rows = rwys[rwys["airport"] == ap]
            print(f"\n  runway DB rows for {ap}: {len(rows)}")
            for r in rows:
                print(f"      {str(r['le_ident'])!r}/{str(r['he_ident'])!r}  "
                      f"len={float(r['length_ft']):.0f}ft")
        else:
            print(f"\n  runway DB: not loaded")
        print()


if __name__ == "__main__":
    args = sys.argv[1:] or ["KFLG"]
    main(args)
