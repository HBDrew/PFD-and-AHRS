"""fpllib.py — pure merge for the synced saved-flight-plan library.

Saved plans sync between displays (screen-sync KIND_FPLLIB).  A plain union
merge can't express deletion, so a plan deleted on one screen is re-broadcast
by a peer that still holds it and resurrects (FPLLIB-DELETE-RESURRECT).

This module merges plan lists with **deletion tombstones** so a delete sticks
across the panel, and a later re-create (same name, newer timestamp) wins back
over the tombstone.  Pure and unit-tested — the pfd apply/publish paths just
feed it the local + peer state.

Plan shape:    {"name": str, "waypoints": [...], "ts": float epoch}
Tombstones:    {NAME_UPPER: deleted_ts}
"""

DEFAULT_TTL_S = 86400.0   # forget a deletion after 24 h so the set stays small


def _merge_tombstones(local_deleted, peer_deleted, now, ttl_s):
    """Newest deleted_ts per name from both sides, expired ones dropped."""
    out = {}
    for src in (local_deleted or {}, peer_deleted or {}):
        for k, ts in src.items():
            ku = str(k).upper()
            try:
                ts = float(ts)
            except (TypeError, ValueError):
                continue
            if now - ts <= ttl_s and ts > out.get(ku, -1.0):
                out[ku] = ts
    return out


def merge_plan_lib(local_plans, local_deleted, peer_plans, peer_deleted,
                   now, max_plans=0, ttl_s=DEFAULT_TTL_S):
    """Merge a saved-plan library with a peer's, honouring tombstones.

    Returns ``(plans, deleted)`` — the merged plan list (LWW by ``ts``) and
    the merged/pruned tombstone dict.  A plan is suppressed while a tombstone
    for its name is at least as new as the plan's own timestamp; a plan newer
    than the tombstone (a re-create) is adopted and clears the tombstone.
    """
    deleted = _merge_tombstones(local_deleted, peer_deleted, now, ttl_s)

    plans = {}
    for p in (local_plans or []):
        nm = str(p.get("name", "")).strip()
        if nm:
            plans[nm.upper()] = dict(p)

    # Fold in peer plans (last-writer-wins by ts), respecting tombstones.
    for p in (peer_plans or []):
        nm = str(p.get("name", "")).strip()
        if not nm:
            continue
        ku = nm.upper()
        pts = float(p.get("ts", 0.0))
        dts = deleted.get(ku)
        if dts is not None and pts <= dts:
            continue                      # deleted at/after this version
        cur = plans.get(ku)
        if cur is None or pts > float(cur.get("ts", 0.0)):
            plans[ku] = dict(p)
        if dts is not None and pts > dts:
            del deleted[ku]               # re-created after the tombstone

    # Drop local plans that our own (merged) tombstones have buried.
    for ku, dts in list(deleted.items()):
        cur = plans.get(ku)
        if cur is not None and float(cur.get("ts", 0.0)) <= dts:
            del plans[ku]

    out = sorted(plans.values(),
                 key=lambda p: (-float(p.get("ts", 0.0)),
                                str(p.get("name", "")).upper()))
    if max_plans and len(out) > max_plans:
        out = out[:max_plans]
    return out, deleted
