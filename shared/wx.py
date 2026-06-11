"""
wx.py – Weather data layer (METARs now; NEXRAD + FIS-B later).

Mirrors the ADS-B design: a source-agnostic snapshot the renderer consumes,
fed here from the internet (aviationweather.gov Data API — free, no key) and
later, optionally, from FIS-B 978 UAT uplink frames.  The display centres
its query on the aircraft's own GPS, so the same module works on the bench
(static position) and in flight over Starlink / cabin Wi-Fi.

What this provides:
  • parse_metars()         – AWC METAR JSON  -> station dicts
  • derive_flight_category – ceiling/visibility -> VFR/MVFR/IFR/LIFR
  • FLIGHT_CAT_COLORS      – the standard EFB colour ramp
  • WxClient               – background poller (threaded), snapshot()

Advisory only — like all in-cockpit weather, METARs are observations that
age; treat them as situational awareness, not a dispatch product.
"""

import calendar
import json
import math
import os
import random
import threading
import time
import urllib.request

# Standard EFB flight-category colours (green / blue / red / magenta).
FLIGHT_CAT_COLORS = {
    "VFR":  (0, 200, 0),
    "MVFR": (40, 120, 255),
    "IFR":  (235, 40, 40),
    "LIFR": (220, 0, 220),
}
_UNKNOWN_COLOR = (160, 160, 160)

_AWC_METAR = "https://aviationweather.gov/api/data/metar"
_AWC_TAF   = "https://aviationweather.gov/api/data/taf"
_AWC_AIRSIGMET = "https://aviationweather.gov/api/data/airsigmet"
# Winds/temps aloft: Open-Meteo's free pressure-level forecast (no key).  AWC
# retired the text FB winds product; the GFS grids behind it now come as JSON
# here, batched many points per request.
_OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
# FAA NOTAM API — needs free developer credentials (client_id / client_secret
# from https://api.faa.gov), supplied via env.  No key → the fetch no-ops, so
# the rest of the weather suite is unaffected.
_FAA_NOTAM = "https://external-api.faa.gov/notamapi/v1/notams"
_FAA_NOTAM_ID_ENV = "FAA_NOTAM_CLIENT_ID"
_FAA_NOTAM_SECRET_ENV = "FAA_NOTAM_CLIENT_SECRET"
_UA = "PFD-and-AHRS/wx (experimental EFB; contact via repo)"
_NM_PER_DEG = 60.0
_M_TO_FT = 3.280839895

# Pressure levels we pull (hPa) — enough to bracket our FD altitudes up to
# ~39,000 ft (~200 hPa).  Each level gives wind, temp, and its geopotential
# height, which we interpolate onto the standard altitudes.
_OM_LEVELS = (1000, 925, 850, 700, 600, 500, 400, 300, 250, 200)

# AWC airsigmet ``hazard`` codes -> the hazard names our graphics/picker use
# (must match shared/fisb.py _GFX_HAZARD so internet and radio overlays share a
# legend and the SIGMET/AIRMET split in _HAZARD_KIND lines up).
_AWC_HAZARD = {
    "TURB": "Turbulence", "ICE": "Icing", "IFR": "IFR",
    "MTN OBSCN": "Mtn Obscuration", "MTOS": "Mtn Obscuration",
    "CONVECTIVE": "Convective", "CONV": "Convective", "ASH": "Ash",
}


def cat_color(fltcat):
    return FLIGHT_CAT_COLORS.get(fltcat, _UNKNOWN_COLOR)


# ── Field parsing ─────────────────────────────────────────────────────────────
def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_visibility(v):
    """AWC visibility comes as a number, '10+', or a fraction like '1 1/2'.
    Return statute miles as a float, or None."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("+", "")
    if not s:
        return None
    try:
        if " " in s:                      # "1 1/2"
            whole, frac = s.split(" ", 1)
            num, den = frac.split("/")
            return float(whole) + float(num) / float(den)
        if "/" in s:                       # "1/2"
            num, den = s.split("/")
            return float(num) / float(den)
        return float(s)
    except (ValueError, ZeroDivisionError):
        return None


def ceiling_ft(clouds):
    """Lowest broken/overcast/obscured layer base in ft AGL, or None for
    no ceiling (clear / few / scattered only)."""
    if not clouds:
        return None
    bases = [c.get("base") for c in clouds
             if c.get("cover") in ("BKN", "OVC", "OVX") and c.get("base") is not None]
    return min(bases) if bases else None


def derive_flight_category(visib_mi, ceil_ft):
    """FAA flight-category rules from visibility (sm) + ceiling (ft AGL)."""
    vis = 99.0 if visib_mi is None else visib_mi
    ceil = 1e9 if ceil_ft is None else ceil_ft
    if ceil < 500 or vis < 1:
        return "LIFR"
    if ceil < 1000 or vis < 3:
        return "IFR"
    if ceil <= 3000 or vis <= 5:
        return "MVFR"
    return "VFR"


def parse_metars(data, now=None):
    """Parse an AWC METAR JSON array into station dicts.  Tolerates missing
    fields and derives the flight category when the feed omits fltCat."""
    now = now if now is not None else time.time()
    out = []
    for m in data or []:
        lat, lon = _num(m.get("lat")), _num(m.get("lon"))
        icao = (m.get("icaoId") or "").strip()
        if lat is None or lon is None or not icao:
            continue
        vis = parse_visibility(m.get("visib"))
        ceil = ceiling_ft(m.get("clouds"))
        cat = (m.get("fltCat") or "").strip().upper()
        if cat not in FLIGHT_CAT_COLORS:
            cat = derive_flight_category(vis, ceil)
        obs = _num(m.get("obsTime"))
        age_min = None if obs is None else max(0.0, (now - obs) / 60.0)
        out.append({
            "icao": icao, "lat": lat, "lon": lon,
            "fltcat": cat,
            "wdir": m.get("wdir"), "wspd": _num(m.get("wspd")),
            "wgst": _num(m.get("wgst")),
            "visib_mi": vis, "ceiling_ft": ceil,
            "altim_hpa": _num(m.get("altim")),
            "temp_c": _num(m.get("temp")), "dewp_c": _num(m.get("dewp")),
            "wx": (m.get("wxString") or "").strip(),
            "name": (m.get("name") or "").strip(),
            "raw": (m.get("rawOb") or "").strip(),
            "age_min": age_min,
            "src": "INET",          # internet (AWC); FIS-B tags its own "RDR"
        })
    return out


# ── Fetch ─────────────────────────────────────────────────────────────────────
def fetch_metars(lat, lon, radius_nm, timeout=12):
    """Fetch METARs within a bbox around (lat, lon) from the AWC Data API.
    Raises on network/JSON error so the caller can retry."""
    dlat = radius_nm / _NM_PER_DEG
    dlon = radius_nm / (_NM_PER_DEG * max(0.05, math.cos(math.radians(lat))))
    # AWC bbox order: minLat,minLon,maxLat,maxLon
    bbox = f"{lat-dlat:.4f},{lon-dlon:.4f},{lat+dlat:.4f},{lon+dlon:.4f}"
    url = f"{_AWC_METAR}?format=json&bbox={bbox}"
    req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8", "ignore"))
    return parse_metars(data)


# ── TAF (forecast) ─────────────────────────────────────────────────────────────
def parse_tafs(data):
    """Parse an AWC TAF JSON array into ``{icao, lat, lon, raw}`` dicts.

    Only the raw text + position are taken here — the same structured TAF
    decoder used for FIS-B TAFs (fisb.parse_taf) renders these in the picker, so
    radio and internet forecasts read identically.  Tolerates the feed's varied
    field names for the raw text (``rawTAF`` / ``rawOb`` / ``raw_text``)."""
    out = []
    for t in data or []:
        icao = (t.get("icaoId") or t.get("stationId") or "").strip()
        raw = (t.get("rawTAF") or t.get("rawOb") or t.get("raw_text") or "").strip()
        if not icao or not raw:
            continue
        out.append({
            "icao": icao,
            "lat": _num(t.get("lat")), "lon": _num(t.get("lon")),
            "raw": raw, "src": "INET",
        })
    return out


def fetch_tafs(lat, lon, radius_nm, timeout=12):
    """Fetch TAFs within a bbox around (lat, lon) from the AWC Data API."""
    dlat = radius_nm / _NM_PER_DEG
    dlon = radius_nm / (_NM_PER_DEG * max(0.05, math.cos(math.radians(lat))))
    bbox = f"{lat-dlat:.4f},{lon-dlon:.4f},{lat+dlat:.4f},{lon+dlon:.4f}"
    url = f"{_AWC_TAF}?format=json&bbox={bbox}"
    req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8", "ignore"))
    return parse_tafs(data)


# ── AIRMET / SIGMET (text + geometry) ──────────────────────────────────────────
def _airsig_coords(item):
    """Pull a [(lat, lon)] ring from an AWC airsigmet record, tolerating the
    feed's two shapes: a ``coords`` list of {lat,lon}, or top-level
    ``lat``/``lon`` arrays."""
    verts = []
    coords = item.get("coords")
    if isinstance(coords, list):
        for c in coords:
            la, lo = _num(c.get("lat")), _num(c.get("lon"))
            if la is not None and lo is not None:
                verts.append((la, lo))
    if not verts:
        la, lo = item.get("lat"), item.get("lon")
        if isinstance(la, list) and isinstance(lo, list):
            for a, o in zip(la, lo):
                a, o = _num(a), _num(o)
                if a is not None and o is not None:
                    verts.append((a, o))
    return verts


def parse_airsigmets(data):
    """Parse an AWC airsigmet JSON array into advisory dicts:
    ``{kind, hazard, raw, valid_from, valid_to, vertices}``.

    ``kind`` is "AIRMET"/"SIGMET" (OUTLOOKs map to SIGMET — they're convective
    outlooks); ``hazard`` is normalised to our legend names; ``vertices`` is the
    area ring (possibly empty for a text-only bulletin)."""
    out = []
    for it in data or []:
        raw = (it.get("rawAirSigmet") or it.get("rawSigmet")
               or it.get("raw_text") or "").strip()
        atype = (it.get("airSigmetType") or "").strip().upper()
        kind = "SIGMET" if atype in ("SIGMET", "OUTLOOK") else "AIRMET"
        hz = (it.get("hazard") or "").strip().upper()
        hazard = _AWC_HAZARD.get(hz, "Advisory")
        # Convective is a SIGMET hazard regardless of the type field.
        if hazard in ("Convective", "Ash"):
            kind = "SIGMET"
        out.append({
            "kind": kind, "hazard": hazard, "raw": raw,
            "valid_from": _num(it.get("validTimeFrom")),
            "valid_to": _num(it.get("validTimeTo")),
            "vertices": _airsig_coords(it), "src": "INET",
        })
    return out


def fetch_airsigmets(lat=None, lon=None, radius_nm=None, timeout=12):
    """Fetch domestic AIRMETs + SIGMETs (CONUS) from the AWC Data API.

    The airsigmet endpoint returns the whole active CONUS set (a short list);
    position args are accepted for the poller's uniform fetch signature but the
    feed isn't bbox-filtered — ranking/nearest-first happens at read time."""
    url = f"{_AWC_AIRSIGMET}?format=json"
    req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8", "ignore"))
    return parse_airsigmets(data)


# ── Winds / temps aloft (Open-Meteo pressure levels → FD altitudes) ─────────────
def _dirspd_to_uv(dir_deg, spd):
    """Wind direction (deg FROM) + speed → (u, v) components.  The convention
    is internal only — _uv_to_dirspd inverts it exactly, so it round-trips."""
    r = math.radians(dir_deg)
    return spd * math.sin(r), spd * math.cos(r)


def _uv_to_dirspd(u, v):
    d = math.degrees(math.atan2(u, v)) % 360.0
    return (d or 360.0), math.hypot(u, v)


def interp_winds(samples, alts):
    """Interpolate pressure-level wind samples onto the standard FD altitudes.

    ``samples`` is ``[(height_ft, dir_deg, spd_kt, temp_c), …]`` (any order);
    ``alts`` the target altitudes (ft).  Interpolates the wind in u/v space
    (so direction wraps correctly) and temperature linearly by geopotential
    height.  Target altitudes outside the sampled column are skipped — no
    extrapolation.  Returns ``[{alt_ft, dir, spd, temp, lv}]``."""
    pts = sorted((s for s in samples if s[0] is not None), key=lambda s: s[0])
    if len(pts) < 2:
        return []
    out = []
    for a in alts:
        if a < pts[0][0] or a > pts[-1][0]:
            continue
        # Bracketing levels.
        for i in range(len(pts) - 1):
            h0, h1 = pts[i][0], pts[i + 1][0]
            if h0 <= a <= h1:
                f = 0.0 if h1 == h0 else (a - h0) / (h1 - h0)
                u0, v0 = _dirspd_to_uv(pts[i][1], pts[i][2])
                u1, v1 = _dirspd_to_uv(pts[i + 1][1], pts[i + 1][2])
                u = u0 + (u1 - u0) * f
                v = v0 + (v1 - v0) * f
                d, sp = _uv_to_dirspd(u, v)
                t0, t1 = pts[i][3], pts[i + 1][3]
                temp = None if (t0 is None or t1 is None) else \
                    int(round(t0 + (t1 - t0) * f))
                lv = sp < 3.0     # light & variable below ~3 kt
                out.append({"alt_ft": a,
                            "dir": None if lv else int(round(d / 10.0) * 10) or 360,
                            "spd": None if lv else int(round(sp)),
                            "temp": temp, "lv": lv})
                break
    return out


def parse_open_meteo_winds(data, alts, now=None, hour_offset=0, series_h=0):
    """Parse an Open-Meteo pressure-level response into winds-aloft dicts:
    ``{station, lat, lon, levels, src, hour_offset}``.  ``data`` is one point
    object or a list of them (Open-Meteo returns a list when several coords are
    queried).  For each point the hour nearest ``now + hour_offset`` is taken
    and the levels interpolated onto ``alts``.  ``station`` is a stable
    coordinate label (the grid carries no ICAO).

    When ``series_h`` > 0 each column also carries the forecast SERIES — the
    per-hour ``levels`` spanning ``[now, now + series_h h]`` plus ``t0``/``step_s``
    — so the draw side can retarget to any valid hour (``now``, ``now + offset``)
    without a re-fetch.  We already download a 48 h forecast per call, so the
    series is free; keeping it lets the picture roll forward to the correct hour
    on its own instead of freezing at the fetch-time snapshot."""
    now = now if now is not None else time.time()
    target = now + hour_offset * 3600.0
    points = data if isinstance(data, list) else [data]
    out = []
    for p in points:
        if not isinstance(p, dict):
            continue
        lat, lon = _num(p.get("latitude")), _num(p.get("longitude"))
        hourly = p.get("hourly") or {}
        times = hourly.get("time") or []
        if lat is None or lon is None or not times:
            continue
        epochs = [_iso_to_epoch(t) for t in times]
        levels = _levels_at(hourly, _nearest_epoch_index(epochs, target), alts)
        if not levels:
            continue
        col = {"station": f"{lat:.2f},{lon:.2f}",
               "lat": lat, "lon": lon, "levels": levels, "src": "INET",
               "hour_offset": hour_offset}
        if series_h > 0:
            t0, step_s, series = _build_winds_series(
                hourly, epochs, alts, now, series_h)
            if series:
                col["t0"], col["step_s"] = t0, step_s
                col["series"], col["alts"] = series, list(alts)
        out.append(col)
    return out


def _levels_to_row(levels, alts):
    """Flatten ``levels`` dicts to ``[dir,spd,temp]`` per altitude (in ``alts``
    order) — the compact per-hour form stored in a column's forecast series."""
    by = {lv.get("alt_ft"): lv for lv in levels}
    row = []
    for a in alts:
        lv = by.get(a)
        row += [lv.get("dir"), lv.get("spd"), lv.get("temp")] if lv \
            else [None, None, None]
    return row


def _row_to_levels(row, alts):
    """Expand a flat series row back to ``levels`` dicts (calm/None-speed levels
    are dropped, matching the packed LAN form)."""
    out = []
    for k, a in enumerate(alts):
        b = k * 3
        if b + 2 >= len(row):
            break
        d, s, t = row[b], row[b + 1], row[b + 2]
        if s is None:
            continue
        out.append({"alt_ft": a, "dir": d, "spd": s, "temp": t, "lv": False})
    return out


def _build_winds_series(hourly, epochs, alts, now, series_h):
    """Compact per-hour forecast for the hours in ``[now, now + series_h h]``,
    aligned to a uniform ``t0 + i*step_s`` grid.  Returns ``(t0, step_s, rows)``
    where each row is the flat ``[dir,spd,temp …]`` for that hour (kept as flat
    lists, not dicts, so a 30 h national series is a few MB of RAM, not ~90).
    A failed hour stays in place as an empty row so the index grid is intact."""
    valid = [(i, ep) for i, ep in enumerate(epochs) if ep is not None]
    if len(valid) < 2:
        return None, 3600, []
    step_s = valid[1][1] - valid[0][1] or 3600
    lo, hi_t = now - step_s * 0.5, now + series_h * 3600.0 + step_s * 0.5
    t0, rows = None, []
    for i, ep in valid:
        if ep < lo:
            continue
        if ep > hi_t:
            break
        if t0 is None:
            t0 = ep
        rows.append(_levels_to_row(_levels_at(hourly, i, alts), alts))
    return t0, step_s, rows


def winds_levels_at(col, target_ts, alts=None):
    """The winds ``levels`` of a column valid at ``target_ts`` (epoch s).

    Columns that carry a forecast ``series`` are retargeted to the nearest hour
    (expanded from the compact flat row); a target outside the held window
    returns ``[]`` (drawn blank — we don't pass off a wrong-time forecast as
    current).  Columns without a series (a peer's now-snapshot, radio winds, or
    a disk-loaded snapshot) fall back to their single ``levels``."""
    series = col.get("series")
    t0 = col.get("t0")
    if not series or t0 is None:
        return col.get("levels", [])
    step = col.get("step_s") or 3600
    fi = (target_ts - t0) / step
    if fi < -0.5 or fi > len(series) - 0.5:
        return []
    row = series[max(0, min(len(series) - 1, int(round(fi))))]
    return _row_to_levels(row, alts or col.get("alts") or _winds_alts())


def _winds_alts():
    from fisb import WINDS_ALTS
    return list(WINDS_ALTS)


def _winds_cols_snapshot(cols, target_ts):
    """Collapse series-carrying columns to a single-hour snapshot valid at
    ``target_ts`` — the compact form shared over the LAN (a peer adopts just the
    current hour, which ``pack_winds_zone`` already packs)."""
    out = []
    for c in cols:
        levels = winds_levels_at(c, target_ts)
        if not levels:
            continue
        out.append({"station": c.get("station"), "lat": c["lat"],
                    "lon": c["lon"], "levels": levels, "src": "INET",
                    "hour_offset": 0})
    return out


def _series_at(series, idx):
    if not isinstance(series, list) or idx is None or idx >= len(series):
        return None
    return _num(series[idx])


def _iso_to_epoch(t):
    """ISO 'YYYY-MM-DDTHH:MM' (UTC) -> epoch seconds, or None."""
    try:
        return calendar.timegm(time.strptime(t[:16], "%Y-%m-%dT%H:%M"))
    except (ValueError, TypeError):
        return None


def _nearest_epoch_index(epochs, target):
    """Index of the epoch nearest ``target`` (skips None entries)."""
    best_i, best_d = None, None
    for i, ep in enumerate(epochs):
        if ep is None:
            continue
        d = abs(ep - target)
        if best_d is None or d < best_d:
            best_i, best_d = i, d
    return best_i


def _levels_at(hourly, hi, alts, levels_hpa=_OM_LEVELS):
    """Interpolated winds ``levels`` for one hourly index of an Open-Meteo
    pressure-level response (``[]`` when that hour has no usable column)."""
    if hi is None:
        return []
    samples = []
    for hpa in levels_hpa:
        hgt = _series_at(hourly.get(f"geopotential_height_{hpa}hPa"), hi)
        spd = _series_at(hourly.get(f"windspeed_{hpa}hPa"), hi)
        wdir = _series_at(hourly.get(f"winddirection_{hpa}hPa"), hi)
        temp = _series_at(hourly.get(f"temperature_{hpa}hPa"), hi)
        if hgt is None or spd is None or wdir is None:
            continue
        samples.append((hgt * _M_TO_FT, wdir, spd, temp))
    return interp_winds(samples, alts)


def _nearest_hour_index(times, now):
    """Index into Open-Meteo's hourly ``time`` array nearest to ``now`` (epoch
    s).  Times are ISO 'YYYY-MM-DDTHH:MM' in UTC."""
    best_i, best_d = None, None
    for i, t in enumerate(times):
        try:
            tm = time.strptime(t[:16], "%Y-%m-%dT%H:%M")
            ep = calendar.timegm(tm)
        except (ValueError, TypeError):
            continue
        d = abs(ep - now)
        if best_d is None or d < best_d:
            best_i, best_d = i, d
    return best_i


def _winds_grid_points(lat, lon, range_nm, aspect=1.0, cols=5, rows=4,
                       spacing_nm=None):
    """A lat/lon grid that fills the *visible* map, for batched winds barbs.

    ``range_nm`` is the map's shorter-axis half-extent (its vertical half-height
    in landscape); ``aspect`` = screen width/height widens the grid horizontally
    so barbs reach the left/right edges instead of bunching in a centre column.
    The 0.82 inset keeps the edge barbs (and their temp tags) on screen.
    ``spacing_nm`` (when given) sizes the grid to roughly that barb spacing
    instead of the fixed cols×rows — used for the wide cached area so the
    barb density stays constant regardless of how big the cache is."""
    span_lat = range_nm * 0.82
    span_lon = range_nm * 0.82 * max(1.0, aspect)
    if spacing_nm:
        rows = max(2, min(13, int(round(2 * span_lat / spacing_nm)) + 1))
        cols = max(2, min(15, int(round(2 * span_lon / spacing_nm)) + 1))
    dlat = span_lat / _NM_PER_DEG
    dlon = span_lon / (_NM_PER_DEG * max(0.05, math.cos(math.radians(lat))))
    pts = []
    for r in range(rows):
        fy = 0.5 if rows == 1 else r / (rows - 1)
        for c in range(cols):
            fx = 0.5 if cols == 1 else c / (cols - 1)
            pts.append((lat - dlat + 2 * dlat * fy,
                        lon - dlon + 2 * dlon * fx))
    return pts


def fetch_winds_grid(lat, lon, range_nm, aspect=1.7, alts=None,
                     hour_offset=0, timeout=15):
    """Fetch winds/temps aloft for a grid filling the visible map in one
    Open-Meteo request, parsed onto the standard FD altitudes.  ``hour_offset``
    selects the forecast hour (0 = now, +N hours ahead)."""
    from fisb import WINDS_ALTS
    alts = alts if alts is not None else WINDS_ALTS
    pts = _winds_grid_points(lat, lon, range_nm, aspect)
    return _fetch_om_winds(pts, alts, hour_offset, timeout)


def _route_winds_points(route, width_nm=25.0, step_nm=None, max_points=96):
    """Grid points covering a *route corridor* for the winds barbs.

    ``route`` is the active course as ``[(lat, lon), ...]`` (ownship first,
    then the remaining FPL legs / the single direct-to waypoint).  Returns
    samples taken along the polyline at ~``step_nm`` spacing, each with a
    lateral offset of ±``width_nm`` either side of the course so the barbs
    blanket a band along the whole route, not just a patch around the
    aircraft.  Deduped by a coarse snap and capped at ``max_points`` (the
    along-track spacing widens automatically to stay under the cap — Open-
    Meteo's multi-point request and the barb render both want a bounded set)."""
    if not route or len(route) < 2:
        return []
    seglen = [_nm_between(route[i][0], route[i][1],
                          route[i + 1][0], route[i + 1][1])
              for i in range(len(route) - 1)]
    total = sum(seglen)
    lat_lines = 3                                   # centre + both sides
    if step_nm is None:
        step_nm = max(15.0, total / max(1.0, (max_points / lat_lines) - 1))
    offs = (0.0, width_nm, -width_nm)
    seen, pts = set(), []

    def _add(la, lo):
        key = (round(la, 1), round(lo, 1))          # ~6 nm snap for dedup
        if key in seen:
            return
        seen.add(key)
        pts.append((la, lo))

    for i in range(len(route) - 1):
        (la1, lo1), (la2, lo2) = route[i], route[i + 1]
        seg = seglen[i]
        if seg < 1e-6:
            continue
        mid_lat = (la1 + la2) / 2.0
        brg = math.degrees(math.atan2(
            (lo2 - lo1) * math.cos(math.radians(mid_lat)), (la2 - la1)))
        perp = math.radians(brg + 90.0)
        cos_p, sin_p = math.cos(perp), math.sin(perp)
        n = max(1, int(math.ceil(seg / step_nm)))
        for s in range(n + 1):
            f = s / n
            la = la1 + (la2 - la1) * f
            lo = lo1 + (lo2 - lo1) * f
            coslat = max(0.05, math.cos(math.radians(la)))
            for d in offs:
                dla = d * cos_p / _NM_PER_DEG
                dlo = d * sin_p / (_NM_PER_DEG * coslat)
                _add(la + dla, lo + dlo)
                if len(pts) >= max_points:
                    return pts
    return pts


def fetch_winds_route(route, width_nm=25.0, alts=None, hour_offset=0,
                      timeout=15, max_points=96):
    """Fetch winds/temps aloft along a route corridor (see
    ``_route_winds_points``) in one batched Open-Meteo request."""
    from fisb import WINDS_ALTS
    alts = alts if alts is not None else WINDS_ALTS
    pts = _route_winds_points(route, width_nm=width_nm, max_points=max_points)
    if not pts:
        return []
    return _fetch_om_winds(pts, alts, hour_offset, timeout)


def fetch_winds(lat, lon, range_nm, aspect=1.0, route=None,
                route_width_nm=25.0, spacing_nm=None, alts=None, hour_offset=0,
                timeout=15, max_points=120):
    """Winds/temps aloft for a WIDE cached area, PLUS a corridor along an
    active route when one is given — in a single batched request.

    ``range_nm`` here is the cache half-extent (much wider than any one zoom);
    the renderer culls to the current view, so zoom and small pans need no
    refetch.  ``spacing_nm`` keeps the barb density constant across the cache.
    The optional route corridor adds points along the off-screen part of an
    active D2/FPL course.  Points are deduped on a fine snap (true duplicates
    only) and capped at ``max_points`` (grid first, then as much corridor as
    fits) to keep the request and the barb render bounded."""
    from fisb import WINDS_ALTS
    alts = alts if alts is not None else WINDS_ALTS
    pts = list(_winds_grid_points(lat, lon, range_nm, aspect,
                                  spacing_nm=spacing_nm))
    if route and len(route) >= 2:
        # The visible-area grid already owns the on-screen picture.  Keep only
        # the corridor points that fall OUTSIDE the visible window so a short
        # direct-to doesn't stack a second batch of barbs on top of the grid;
        # the corridor then just extends data coverage along the part of the
        # route that runs off-screen (it scrolls into view, cleanly gridded,
        # as the aircraft flies the leg).
        reach = range_nm
        pts += [p for p in _route_winds_points(route, width_nm=route_width_nm,
                                               max_points=max_points)
                if _nm_between(lat, lon, p[0], p[1]) > reach]
    # Drop only true duplicates (~0.06 nm) — a coarse snap here would collapse
    # the regular visible-area grid at close zoom (e.g. a 5 nm inset has ~2.7
    # nm row spacing) and leave a handful of unevenly-placed barbs.  The
    # corridor builder already thins its own points internally.
    seen, uniq = set(), []
    for p in pts:
        k = (round(p[0], 3), round(p[1], 3))
        if k in seen:
            continue
        seen.add(k)
        uniq.append(p)
        if len(uniq) >= max_points:
            break
    if not uniq:
        return []
    return _fetch_om_winds(uniq, alts, hour_offset, timeout)


# Open-Meteo allows up to 1000 locations per call.  We stay well under that with
# one big batch (a coarse CONUS grid is ~450 points), so the whole national grid
# comes down in ONE or two requests rather than dozens of tiny ones.  Each call's
# cost is metered by locations, so keep a single call under the per-minute
# budget — 250 keeps us safe even at full national density.
_OM_MAX_BATCH = 250

# Pressure levels that bracket the standard FD altitudes up to ~18,000 ft
# (~500 hPa).  Capping the vertical here drops 4 of the 10 levels, cutting the
# per-location variable cost (and the response size) by ~40 %.
_OM_LEVELS_LOW = (1000, 925, 850, 700, 600, 500)


def _fetch_om_batch(pts, alts, hour_offset, timeout, levels=None, model=None,
                    series_h=0):
    """One Open-Meteo pressure-level request for up to ``_OM_MAX_BATCH`` points.
    ``levels`` caps the vertical (defaults to the full set); ``model`` pins an
    explicit model (e.g. ``gfs025`` — required for pressure levels).  ``series_h``
    keeps the per-hour forecast series (see ``parse_open_meteo_winds``)."""
    levels = levels if levels is not None else _OM_LEVELS
    lats = ",".join(f"{p[0]:.3f}" for p in pts)
    lons = ",".join(f"{p[1]:.3f}" for p in pts)
    fields = []
    for hpa in levels:
        fields += [f"windspeed_{hpa}hPa", f"winddirection_{hpa}hPa",
                   f"temperature_{hpa}hPa", f"geopotential_height_{hpa}hPa"]
    # forecast_days=2 so a +24 h offset still has data late in the UTC day.
    url = (f"{_OPEN_METEO}?latitude={lats}&longitude={lons}"
           f"&hourly={','.join(fields)}&windspeed_unit=kn"
           f"&forecast_days=2&timeformat=iso8601&timezone=UTC")
    if model:
        url += f"&models={model}"
    req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8", "ignore"))
    return parse_open_meteo_winds(data, alts, hour_offset=hour_offset,
                                  series_h=series_h)


def _fetch_om_winds(pts, alts, hour_offset, timeout, levels=None, model=None,
                    series_h=0):
    """Fetch winds/temps aloft for ``pts`` (``[(lat, lon), ...]``) in batches of
    ``_OM_MAX_BATCH``, combining the parsed columns.  Returns whatever batches
    succeed; raises only if every batch fails (so one transient error doesn't
    blank the whole picture)."""
    out, last_err = [], None
    for i in range(0, len(pts), _OM_MAX_BATCH):
        try:
            out.extend(_fetch_om_batch(pts[i:i + _OM_MAX_BATCH], alts,
                                       hour_offset, timeout, levels, model,
                                       series_h))
        except Exception as e:                                   # noqa: BLE001
            last_err = e
    if not out and last_err is not None:
        raise last_err
    return out


def conus_grid_points(bbox, spacing_nm):
    """Coarse lat/lon grid covering ``bbox`` = (min_lat, max_lat, min_lon,
    max_lon) at ~``spacing_nm`` spacing — the national winds grid.  Longitude
    step widens with latitude so the spacing stays ~uniform in nm."""
    min_lat, max_lat, min_lon, max_lon = bbox
    dlat = spacing_nm / 60.0
    pts = []
    la = min_lat
    while la <= max_lat + 1e-6:
        coslat = max(0.2, math.cos(math.radians(la)))
        dlon = spacing_nm / (60.0 * coslat)
        lo = min_lon
        while lo <= max_lon + 1e-6:
            pts.append((round(la, 3), round(lo, 3)))
            lo += dlon
        la += dlat
    return pts


def fetch_winds_us(bbox, spacing_nm, alts=None, hour_offset=0, timeout=30,
                   model="gfs025", max_alt_ft=18000, series_h=0):
    """Fetch one coarse winds grid over ``bbox`` in a single (or few) batched
    call(s), capped at ``max_alt_ft`` and pinned to ``model``.  Returns the
    parsed winds columns (each ``{station, lat, lon, levels, ...}``).  ``series_h``
    keeps the per-hour forecast series so the cache can roll forward to ``now``
    between fetches (see ``parse_open_meteo_winds``)."""
    from fisb import WINDS_ALTS
    alts = alts if alts is not None else WINDS_ALTS
    alts = [a for a in alts if a <= max_alt_ft]
    levels = _OM_LEVELS_LOW if max_alt_ft <= 18000 else _OM_LEVELS
    pts = conus_grid_points(bbox, spacing_nm)
    if not pts:
        return []
    return _fetch_om_winds(pts, alts, hour_offset, timeout, levels, model,
                           series_h)


def conus_zones(bbox, rows, cols):
    """Split ``bbox`` into ``rows × cols`` sub-boxes (each a winds zone)."""
    min_lat, max_lat, min_lon, max_lon = bbox
    dlat = (max_lat - min_lat) / rows
    dlon = (max_lon - min_lon) / cols
    return [(min_lat + r * dlat, min_lat + (r + 1) * dlat,
             min_lon + c * dlon, min_lon + (c + 1) * dlon)
            for r in range(rows) for c in range(cols)]


def pack_winds_zone(idx, fetched_ts, cols):
    """Compact a zone for LAN sharing: ``{z, t, c}`` where each column is
    ``[lat, lon, dir0, spd0, temp0, dir1, ...]`` with dir/spd/temp per standard
    altitude (in WINDS_ALTS order).  ~10× smaller than the raw dicts so a zone
    fits in one UDP datagram."""
    from fisb import WINDS_ALTS
    rows = []
    for c in cols:
        by_alt = {lv.get("alt_ft"): lv for lv in c.get("levels", [])}
        row = [round(float(c["lat"]), 3), round(float(c["lon"]), 3)]
        for a in WINDS_ALTS:
            lv = by_alt.get(a)
            if lv:
                row += [lv.get("dir"), lv.get("spd"), lv.get("temp")]
            else:
                row += [None, None, None]
        rows.append(row)
    return {"z": int(idx), "t": round(float(fetched_ts), 1), "c": rows}


def unpack_winds_zone(data):
    """Inverse of ``pack_winds_zone`` → ``(idx, fetched_ts, cols)``."""
    from fisb import WINDS_ALTS
    idx = int(data["z"])
    ts = float(data["t"])
    cols = []
    for row in data.get("c", []):
        if len(row) < 2:
            continue
        lat, lon = row[0], row[1]
        levels = []
        for k, a in enumerate(WINDS_ALTS):
            base = 2 + k * 3
            if base + 2 >= len(row):
                break
            d, s, t = row[base], row[base + 1], row[base + 2]
            if s is not None:
                levels.append({"alt_ft": a, "dir": d, "spd": s,
                               "temp": t, "lv": False})
        cols.append({"station": f"{lat:.2f},{lon:.2f}", "lat": lat,
                     "lon": lon, "levels": levels, "src": "INET",
                     "hour_offset": 0})
    return idx, ts, cols


class WindsUSCache(threading.Thread):
    """National winds-aloft cache, split into zones and fetched lazily.

    Winds aloft (GFS) only reissue every ~6 h, so there's no point chasing the
    map view — we pull a coarse coordinate-list grid for each zone, ONE zone per
    refresh tick (the zone the aircraft is in first, then outward), timestamp
    each, and never re-pull a zone until it's ``max_age_s`` stale.  The whole
    thing is persisted to disk so a restart reloads instantly with zero calls.
    ``enabled`` is set by the app (only while the WND overlay is up), so we make
    no calls at all when winds aren't being looked at.

    ``locate_fn`` -> (lat, lon) of the aircraft; ``hour_offset_fn`` -> the
    selected forecast-time offset.  Diagnostics mirror the pollers."""

    def __init__(self, bbox, rows, cols, spacing_nm, disk_path, locate_fn,
                 hour_offset_fn=None, model="gfs025", max_alt_ft=18000,
                 max_age_s=6 * 3600, slice_s=20.0, publish_fn=None,
                 peer_grace_s=360.0, startup_grace_s=45.0,
                 fetch_jitter_s=180.0, expire_s=24 * 3600, series_h=30):
        super().__init__(daemon=True, name="WindsUSCache")
        self.zones = conus_zones(bbox, rows, cols)
        self.spacing_nm = spacing_nm
        self.disk_path = disk_path
        self.locate_fn = locate_fn
        self.hour_offset_fn = hour_offset_fn or (lambda: 0)
        self.model = model
        self.max_alt_ft = max_alt_ft
        self.max_age_s = max_age_s
        # We keep a SERIES of forecast hours per column (free — the fetch already
        # downloads ~48 h), spanning ``series_h`` ahead of now.  The draw side
        # retargets to ``now`` (inset) or ``now + offset`` (WND page) out of this
        # series, so the picture rolls forward to the correct hour on its own
        # between the 6 h re-pulls instead of freezing at the fetch snapshot.
        # 30 h covers the +24 h selector plus a 6 h refresh interval of drift.
        self.series_h = series_h
        # Hard expiry: a GFS winds-aloft forecast is refreshed on a 6 h cycle, so
        # past max_age_s (6 h) a zone is "stale" — still a usable fallback while
        # we re-pull.  But once it's a full day old it's no forecast at all any
        # more; data this old is worse than nothing, so we stop SERVING it
        # (columns()/barbs drop it) and treat it as missing everywhere.
        self.expire_s = max(expire_s, max_age_s)
        self.slice_s = slice_s
        # LAN sharing: publish_fn(packed_zone_dict) broadcasts a zone to peer
        # screens; when a peer is feeding us (a winds packet arrived within
        # peer_grace_s) we DON'T hit Open-Meteo ourselves.  startup_grace_s
        # lets peer broadcasts arrive before we'd fetch on a cold boot.
        self.publish_fn = publish_fn
        self.peer_grace_s = peer_grace_s
        self.startup_grace_s = startup_grace_s
        self.enabled = False
        self.updated_s = 0.0
        self.rx_count = 0
        self.err_count = 0
        self.last_err = ""
        self.connected = False
        self._data = {}                 # zone idx -> {"cols": [...], "fetched": epoch}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._backoff_until = 0.0       # monotonic; set after a failed fetch
        self._fail_streak = 0
        self._last_peer_rx = 0.0        # monotonic of last adopted peer zone
        self._zone_peer_rx = {}         # idx -> monotonic of last FRESH peer feed
        self._started_at = time.monotonic()
        # Per-device fetch stagger: each screen treats a zone as "due" a
        # different random amount past max_age_s, so when several screens hold
        # equally-stale data they don't all hit Open-Meteo at the same instant
        # (which would risk the per-IP 429 lockout the LAN-share exists to
        # avoid).  The lowest-jitter screen fetches first and feeds the rest,
        # which then adopt + defer before their own (later) threshold.
        self._fetch_jitter = random.uniform(0.0, max(0.0, fetch_jitter_s))
        self._bcast_idx = 0             # round-robin broadcast cursor
        self._load_disk()

    def stop(self):
        self._stop.set()

    def columns(self):
        """All cached winds columns, merged across zones — EXCLUDING any zone
        past ``expire_s`` (a day-old forecast is no longer valid, so we don't
        draw it even as a fallback; that zone reads blank until it re-pulls)."""
        now = time.time()
        with self._lock:
            out = []
            for rec in self._data.values():
                if now - rec.get("fetched", 0.0) >= self.expire_s:
                    continue
                out.extend(rec["cols"])
            return out

    def count(self):
        now = time.time()
        with self._lock:
            return sum(len(rec["cols"]) for rec in self._data.values()
                       if now - rec.get("fetched", 0.0) < self.expire_s)

    def status(self):
        """``(fresh, total, age_s, stale, expired)`` for a status readout.

        Three bands by age: ``fresh`` (< ``max_age_s``, current),
        ``stale`` (``max_age_s``..``expire_s``, past the forecast cycle but still
        drawn as a fallback while we re-pull), and ``expired`` (>= ``expire_s``,
        a day old — no longer drawn at all).  ``age_s`` is the oldest STILL-VALID
        (non-expired) zone's age, so it matches what's actually on screen (None
        when nothing valid is loaded).

        Zones are refreshed in place and never dropped, so a plain loaded-count
        sits at ``total/total`` forever and tells you nothing once primed — what
        matters is how many zones still hold current data, which is what this
        reports."""
        now = time.time()
        total = len(self.zones)
        with self._lock:
            ages = [now - rec["fetched"] for rec in self._data.values()
                    if rec.get("fetched")]
        fresh = sum(1 for a in ages if a < self.max_age_s)
        stale = sum(1 for a in ages if self.max_age_s <= a < self.expire_s)
        expired = sum(1 for a in ages if a >= self.expire_s)
        valid = [a for a in ages if a < self.expire_s]
        return fresh, total, (max(valid) if valid else None), stale, expired

    def stale_zones(self):
        """Indices of zones with no CURRENT data — sorted.  A zone counts unless
        it's loaded and within ``max_age_s`` (so this includes stale, expired and
        never-loaded zones alike), letting a status line name *which* are off."""
        now = time.time()
        total = len(self.zones)
        with self._lock:
            ok = {i for i, rec in self._data.items()
                  if rec.get("fetched") and (now - rec["fetched"]) < self.max_age_s}
        return [i for i in range(total) if i not in ok]

    def force_refresh(self):
        """Mark every zone stale (e.g. when the forecast time changes)."""
        with self._lock:
            self._data = {}
        self.updated_s = time.monotonic()

    def _peer_active(self):
        """True when a peer has fed us a zone within ``peer_grace_s`` — while a
        peer is sharing we leave Open-Meteo alone and just adopt its data."""
        return (self._last_peer_rx > 0.0
                and (time.monotonic() - self._last_peer_rx) < self.peer_grace_s)

    def ingest_packed(self, data):
        """Adopt a zone shared by a peer screen (``pack_winds_zone`` form, plus a
        ``st`` snapshot-valid-time).  Used as the screen-sync KIND_WINDS callback.

        Two timestamps coordinate the panel: ``t`` is the model RUN (stable
        across a feeder's re-broadcasts; bumps every 6 h re-pull) and ``st`` is
        the SNAPSHOT valid-time, which a live feeder advances to *now* on every
        broadcast.  We adopt on a newer run OR a newer snapshot of the same run —
        the latter is how a peer's now-snapshot rolls our barbs forward so an
        adopting screen's inset also stays current.  A relayed (frozen) snapshot
        carries an un-advancing ``st``, so when the feeder dies the deferral
        lapses and a screen re-pulls (no 'everyone waits on someone else')."""
        try:
            idx, ts, cols = unpack_winds_zone(data)
        except Exception:                                        # noqa: BLE001
            return
        try:
            st = float(data.get("st", ts))
        except (TypeError, ValueError):
            st = ts
        with self._lock:
            cur = self._data.get(idx)
            # We hold our OWN forecast series for this zone (we fetched it) — a
            # peer's single-hour snapshot is a downgrade (no series → no page
            # offset, no roll-forward), so keep ours.  Our own 6 h re-pull picks
            # up any newer model run.
            if cur is not None and any(c.get("series") for c in cur["cols"]):
                return
            if cur is not None:
                cur_run = cur.get("fetched", 0.0)
                cur_st = cur.get("snap_ts", cur_run)
                if ts < cur_run:
                    return                   # older model run — ignore
                if ts == cur_run and st <= cur_st:
                    return                   # same run, no newer snapshot
            self._data[idx] = {"cols": cols, "fetched": ts, "snap_ts": st}
        self._last_peer_rx = time.monotonic()
        self._zone_peer_rx[idx] = self._last_peer_rx   # per-zone: a peer owns this one
        self.updated_s = time.monotonic()
        print(f"[WX:winds] adopted zone {idx} from peer ({len(cols)} cols)")

    def _zone_packet(self, idx):
        """Build the LAN packet for a held zone: a single-hour snapshot valid at
        *now* (the full 30 h series is ~30× too big for one UDP datagram, and a
        peer only needs the current hour).  A feeder re-derives the snapshot from
        its series and stamps ``st=now`` so adopters roll forward; a screen that
        only holds a peer snapshot relays it with the snapshot's own (frozen)
        ``st`` so it can't masquerade as a live feed.  ``None`` when the zone has
        nothing valid to send (expired, or its series no longer reaches now)."""
        with self._lock:
            rec = self._data.get(idx)
            if not rec:
                return None
            cols = list(rec["cols"])
            ts = rec["fetched"]
            snap_ts = rec.get("snap_ts")
        now = time.time()
        if now - ts >= self.expire_s:
            return None
        if any(c.get("series") for c in cols):
            snap, st = _winds_cols_snapshot(cols, now), now
        else:
            snap, st = cols, (snap_ts if snap_ts else ts)
        if not snap:
            return None
        pkt = pack_winds_zone(idx, ts, snap)
        pkt["st"] = round(float(st), 1)
        return pkt

    def _broadcast_one(self):
        """Round-robin broadcast one held zone to peers (cheap, LAN-local)."""
        if self.publish_fn is None:
            return
        with self._lock:
            if not self._data:
                return
            keys = sorted(self._data.keys())
            idx = keys[self._bcast_idx % len(keys)]
            self._bcast_idx += 1
        pkt = self._zone_packet(idx)
        if pkt is None:
            return
        try:
            self.publish_fn(pkt)
        except Exception:                                        # noqa: BLE001
            pass

    def _due_zone(self, lat, lon, now):
        """Index of the most-due zone — the aircraft's own zone first, then the
        nearest other stale zone — or None when every zone is fresh."""
        cand = []
        now_m = time.monotonic()
        due_age = self.max_age_s + self._fetch_jitter   # per-device stagger
        for i, z in enumerate(self.zones):
            rec = self._data.get(i)
            has_series = rec is not None and any(c.get("series")
                                                 for c in rec["cols"])
            # A zone with a fresh SERIES is not due — re-pull only on the ~6 h
            # GFS cadence.  But a zone with NO series (a pre-series disk cache
            # from before this feature, or a peer's single-hour snapshot) IS due
            # regardless of age: we want our own forecast series so the +Nh
            # time offset works AND so a fresh pull reconciles screens that are
            # each sitting on a different stale snapshot.
            if has_series and (now - rec["fetched"]) < due_age:
                continue
            # A peer is actively keeping THIS zone fresh for us → leave it to
            # them.  Per-zone (not a global defer), so a feeder isn't blocked
            # from pulling other still-stale zones just because it adopted one
            # — that global block stalled the refresh after a couple of zones.
            if (now_m - self._zone_peer_rx.get(i, 0.0)) < self.peer_grace_s:
                continue
            inside = (z[0] <= lat <= z[1] and z[2] <= lon <= z[3])
            cy, cx = (z[0] + z[1]) / 2.0, (z[2] + z[3]) / 2.0
            cand.append((0 if inside else 1, _nm_between(lat, lon, cy, cx), i))
        if not cand:
            return None
        cand.sort()
        return cand[0][2]

    def refresh_one(self):
        """Fetch the single most-due zone (local first).  True if it fetched."""
        try:
            lat, lon = self.locate_fn()
        except Exception:                                        # noqa: BLE001
            return False
        now = time.time()
        idx = self._due_zone(lat, lon, now)
        if idx is None:
            return False
        try:
            # Fetch the whole series from *now* (hour_offset 0): the draw side
            # retargets to now / now+offset locally, so the per-screen forecast
            # offset no longer drives the fetch (and no longer forces a re-pull).
            cols = fetch_winds_us(self.zones[idx], self.spacing_nm,
                                  hour_offset=0, model=self.model,
                                  max_alt_ft=self.max_alt_ft,
                                  series_h=self.series_h)
            with self._lock:
                self._data[idx] = {"cols": cols, "fetched": now}
            self.updated_s = time.monotonic()
            self.rx_count += 1
            self.connected = True
            self._fail_streak = 0
            self._backoff_until = 0.0
            self._save_disk()
            print(f"[WX:winds] fetched zone {idx} ({len(cols)} cols)")
            # Share it with peer screens straight away so they don't re-fetch
            # (a now-snapshot derived from the fresh series, with st=now).
            if self.publish_fn is not None:
                pkt = self._zone_packet(idx)
                if pkt is not None:
                    try:
                        self.publish_fn(pkt)
                    except Exception:                            # noqa: BLE001
                        pass
            return True
        except Exception as e:                                   # noqa: BLE001
            self.err_count += 1
            self.last_err = f"{type(e).__name__}: {e}"
            self.connected = False
            # Back off exponentially after a failure (rate limit / no internet)
            # so we don't hammer a locked door — 1, 2, 4 … min, capped at 30.
            self._fail_streak += 1
            wait = min(1800.0, 60.0 * (2 ** (self._fail_streak - 1)))
            self._backoff_until = time.monotonic() + wait
            print(f"[WX:winds] {self.last_err} — backing off {int(wait)}s")
            return False

    def run(self):
        while not self._stop.is_set():
            now_m = time.monotonic()
            # Always share what we have (feeds peers; a screen with internet
            # thereby supplies the rest of the panel).
            self._broadcast_one()
            # Fetch from Open-Meteo only when enabled, not backing off, and past
            # the startup grace (give peers a chance to feed us first).  Whether
            # a peer is covering the most-due zone is decided per-zone inside
            # _due_zone — a global "a peer fed me" gate here stalled the refresh
            # for peer_grace_s after adopting a single zone, leaving other zones
            # stale (the 6-min-lurch you saw).
            # The startup grace is staggered per-device by the fetch jitter so a
            # synchronised event — every screen booting together, or all of them
            # finding a pre-series cache due at once after a deploy — doesn't make
            # them all hit Open-Meteo in the same instant (429 risk).  The
            # lowest-jitter screen pulls first and feeds the rest, which adopt and
            # defer before their own (later) gate opens.
            if (self.enabled and now_m >= self._backoff_until
                    and (now_m - self._started_at)
                    >= self.startup_grace_s + self._fetch_jitter):
                self.refresh_one()                # one zone per tick, paced
            slept = 0.0
            while slept < self.slice_s and not self._stop.is_set():
                time.sleep(0.2)
                slept += 0.2

    def _save_disk(self):
        try:
            os.makedirs(os.path.dirname(self.disk_path), exist_ok=True)
            with self._lock:
                payload = {"saved": time.time(),
                           "zones": {str(i): rec
                                     for i, rec in self._data.items()}}
            tmp = self.disk_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(payload, f)
            os.replace(tmp, self.disk_path)
        except Exception as e:                                   # noqa: BLE001
            print(f"[WX:winds] disk save failed: {e}")

    def _load_disk(self):
        try:
            if not os.path.exists(self.disk_path):
                return
            with open(self.disk_path) as f:
                payload = json.load(f)
            data = {}
            for k, rec in (payload.get("zones") or {}).items():
                data[int(k)] = {"cols": rec.get("cols", []),
                                "fetched": float(rec.get("fetched", 0.0))}
            with self._lock:
                self._data = data
            if data:
                self.updated_s = time.monotonic()   # so the app folds it in
            print(f"[WX:winds] loaded {self.count()} cached columns "
                  f"({len(data)} zones) from disk")
        except Exception as e:                                   # noqa: BLE001
            print(f"[WX:winds] disk load failed: {e}")


# ── NOTAMs (FAA NOTAM API — needs developer credentials) ────────────────────────
def have_notam_creds(client_id=None, client_secret=None):
    """True when both FAA NOTAM credentials are available — the passed pair
    (entered in-app) takes precedence over the environment."""
    cid = client_id or os.environ.get(_FAA_NOTAM_ID_ENV)
    csec = client_secret or os.environ.get(_FAA_NOTAM_SECRET_ENV)
    return bool(cid and csec)


def parse_notams(data):
    """Parse an FAA NOTAM API (geoJson) response into advisory text strings.

    Prefers the human-readable ``formattedText`` translation, falls back to the
    raw ICAO ``text``; prefixes the location id so the advisory list can
    geolocate the NOTAM by airport (``_notam_locate``).  De-duplicates."""
    out, seen = [], set()
    for it in (data or {}).get("items") or []:
        core = ((it.get("properties") or {}).get("coreNOTAMData") or {})
        notam = core.get("notam") or {}
        loc = (notam.get("icaoLocation") or notam.get("location") or "").strip()
        text = ""
        for tr in core.get("notamTranslation") or []:
            ft = (tr.get("formattedText") or tr.get("simpleText") or "").strip()
            if ft:
                text = ft
                break
        if not text:
            text = (notam.get("text") or "").strip()
        if not text:
            continue
        text = " ".join(text.split())          # collapse newlines / runs
        if loc and loc.upper() not in text[:12].upper():
            text = f"{loc} {text}"
        if text not in seen:
            seen.add(text)
            out.append(text)
    return out


def fetch_notams(lat, lon, radius_nm, timeout=15, page_size=50,
                 client_id=None, client_secret=None):
    """Fetch NOTAMs within ``radius_nm`` (capped at the API's 100 nm) of a point.
    Credentials come from the passed pair (entered in-app) or, failing that, the
    environment.  Returns ``[]`` — a harmless no-op — when none are configured,
    so the rest of the weather suite runs unaffected without an FAA key."""
    cid = client_id or os.environ.get(_FAA_NOTAM_ID_ENV)
    csec = client_secret or os.environ.get(_FAA_NOTAM_SECRET_ENV)
    if not cid or not csec:
        return []
    rad = max(1, min(100, int(round(radius_nm))))
    url = (f"{_FAA_NOTAM}?responseFormat=geoJson"
           f"&locationLongitude={lon:.4f}&locationLatitude={lat:.4f}"
           f"&locationRadius={rad}&pageSize={page_size}&pageNum=1"
           f"&sortBy=effectiveStartDate&sortOrder=Desc")
    req = urllib.request.Request(url, headers={
        "client_id": cid, "client_secret": csec,
        "User-Agent": _UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8", "ignore"))
    return parse_notams(data)


# ── Background poller ─────────────────────────────────────────────────────────
def _nm_between(a_lat, a_lon, b_lat, b_lon):
    """Approximate great-circle distance in NM (equirectangular — plenty
    accurate for deciding when the map view has moved enough to re-fetch)."""
    dlat = (b_lat - a_lat) * _NM_PER_DEG
    dlon = ((b_lon - a_lon) * _NM_PER_DEG
            * math.cos(math.radians((a_lat + b_lat) / 2)))
    return math.hypot(dlat, dlon)


class WxClient(threading.Thread):
    """Polls weather for the current *map view* and exposes a snapshot.

    ``view_fn`` is a callable -> (center_lat, center_lon, radius_nm) describing
    what the map is showing right now; the poller follows it so panning /
    zooming over CONUS loads weather for the viewed area, not just near the
    aircraft.  It re-fetches when the view has settled and either moved a good
    fraction of the radius, changed zoom, or the periodic refresh is due.

    ``fetch_fn`` is injectable for tests; it defaults to the AWC HTTP fetch.
    Diagnostics (rx_count / err_count / last_err / online) mirror ADSBClient."""

    def __init__(self, view_fn, interval_s=120.0, fetch_fn=None,
                 move_refetch_frac=0.45, poll_slice_s=0.7):
        super().__init__(daemon=True, name="WxClient")
        self.view_fn    = view_fn
        self.interval_s = max(30.0, interval_s)
        self.fetch_fn   = fetch_fn or fetch_metars
        self.move_frac  = move_refetch_frac
        self.slice_s    = poll_slice_s
        self.connected  = False
        self.paused     = False
        self.rx_count   = 0
        self.err_count  = 0
        self.last_err   = ""
        self.updated_s  = 0.0
        self._metars      = []
        self._fetched_at  = 0.0          # monotonic of last successful fetch
        self._stamp_at    = 0.0          # monotonic of last *age* stamp
        self._fetch_ctr   = None         # (lat, lon, radius) of last fetch
        self._lock        = threading.Lock()
        self._stop        = threading.Event()

    def stop(self):
        self._stop.set()

    def _should_fetch(self, lat, lon, radius, now):
        if self._fetch_ctr is None:
            return True
        if now - self._fetched_at >= self.interval_s:
            return True                                  # periodic refresh
        flat, flon, frad = self._fetch_ctr
        if _nm_between(flat, flon, lat, lon) > self.move_frac * frad:
            return True                                  # panned far enough
        if radius > 1.4 * frad or radius < 0.7 * frad:
            return True                                  # zoom changed a step
        return False

    def run(self):
        prev = None
        while not self._stop.is_set():
            if not self.paused:
                try:
                    lat, lon, radius = self.view_fn()
                    now = time.monotonic()
                    # Debounce an active pan-DRAG only, never ownship motion.
                    # A finger fling jumps many nm between 0.7 s slices; an
                    # aircraft in flight (even a time-compressed sim) moves a
                    # small fraction of the radius per slice.  The old
                    # exact-equality "settled" test almost never held for a
                    # moving ownship, so it froze every view-driven product in
                    # flight (winds stuck at the departure field).  The
                    # periodic refresh is always allowed through regardless.
                    step = (_nm_between(prev[0], prev[1], lat, lon)
                            if prev is not None else 0.0)
                    prev = (lat, lon)
                    dragging = step > max(self.move_frac * radius, 2.0)
                    periodic_due = (now - self._fetched_at) >= self.interval_s
                    if ((not dragging or periodic_due)
                            and self._should_fetch(lat, lon, radius, now)):
                        self._fetch(lat, lon, radius)
                except Exception as e:                       # noqa: BLE001
                    self.err_count += 1
                    self.last_err = f"{type(e).__name__}: {e}"
                    self.connected = False
                    print(f"[WX] {self.last_err}")
            slept = 0.0
            while slept < self.slice_s and not self._stop.is_set():
                time.sleep(min(0.2, self.slice_s - slept))
                slept += 0.2

    def _fetch(self, lat, lon, radius):
        metars = self.fetch_fn(lat, lon, radius)
        now = time.monotonic()
        with self._lock:
            self._metars = metars
        self._fetched_at = now
        self._fetch_ctr  = (lat, lon, radius)
        # Advance the displayed data age only on the first fetch or the
        # periodic refresh — a pan / zoom refetch loads a different area of
        # the same-vintage observations, so it must not reset the "age" the
        # pilot sees (observations update ~hourly, not when you scroll).
        if self._stamp_at == 0.0 or (now - self._stamp_at) >= self.interval_s:
            self.updated_s = now
            self._stamp_at = now
        self.rx_count   += 1
        self.connected   = True

    def snapshot(self):
        with self._lock:
            return list(self._metars)

    def count(self):
        with self._lock:
            return len(self._metars)


class AwcPoller(threading.Thread):
    """View-driven AWC poller for a single product (TAF, AIRMET/SIGMET, …).

    Shares WxClient's debounce + re-fetch-on-move logic but is product-agnostic:
    ``fetch_fn(lat, lon, radius)`` returns whatever list the caller wants and
    ``snapshot()`` hands it back.  ``updated_s`` bumps on every successful fetch
    so the app can feed the store only when data actually changed.  These
    products refresh slowly (TAFs ~6 h, AIRMET/SIGMET ~hourly), so the default
    interval is longer than the METAR poller's."""

    def __init__(self, view_fn, fetch_fn, interval_s=300.0, name="AwcPoller",
                 move_refetch_frac=0.6, poll_slice_s=0.7):
        super().__init__(daemon=True, name=name)
        self.view_fn    = view_fn
        self.fetch_fn   = fetch_fn
        self.interval_s = max(60.0, interval_s)
        self.move_frac  = move_refetch_frac
        self.slice_s    = poll_slice_s
        self.connected  = False
        self.paused     = False
        self.rx_count   = 0
        self.err_count  = 0
        self.last_err   = ""
        self.updated_s  = 0.0
        self._items      = []
        self._fetched_at = 0.0
        self._fetch_ctr  = None
        self._lock       = threading.Lock()
        self._stop       = threading.Event()

    def stop(self):
        self._stop.set()

    def force_refresh(self):
        """Make the next poll re-fetch even if the view hasn't moved — used when
        a parameter the fetch depends on (e.g. winds forecast time) changes."""
        self._fetch_ctr = None

    def _should_fetch(self, lat, lon, radius, now):
        if self._fetch_ctr is None:
            return True
        if now - self._fetched_at >= self.interval_s:
            return True
        flat, flon, frad = self._fetch_ctr
        if _nm_between(flat, flon, lat, lon) > self.move_frac * frad:
            return True
        if radius > 1.4 * frad or radius < 0.7 * frad:   # any discrete zoom step
            return True
        return False

    def run(self):
        prev = None
        while not self._stop.is_set():
            if not self.paused:
                try:
                    lat, lon, radius = self.view_fn()
                    now = time.monotonic()
                    # Debounce an active pan-DRAG only, not ownship motion —
                    # see WxClient.run for the full rationale.  The old
                    # exact-equality settle test froze these products (TAF,
                    # AIRMET/SIGMET, winds, NOTAM) on a moving aircraft.
                    step = (_nm_between(prev[0], prev[1], lat, lon)
                            if prev is not None else 0.0)
                    prev = (lat, lon)
                    dragging = step > max(self.move_frac * radius, 2.0)
                    periodic_due = (now - self._fetched_at) >= self.interval_s
                    if ((not dragging or periodic_due)
                            and self._should_fetch(lat, lon, radius, now)):
                        items = self.fetch_fn(lat, lon, radius)
                        with self._lock:
                            self._items = items
                        self._fetched_at = now
                        self._fetch_ctr  = (lat, lon, radius)
                        self.updated_s   = now
                        self.rx_count   += 1
                        self.connected   = True
                except Exception as e:                       # noqa: BLE001
                    self.err_count += 1
                    self.last_err = f"{type(e).__name__}: {e}"
                    self.connected = False
                    print(f"[WX:{self.name}] {self.last_err}")
            slept = 0.0
            while slept < self.slice_s and not self._stop.is_set():
                time.sleep(min(0.2, self.slice_s - slept))
                slept += 0.2

    def snapshot(self):
        with self._lock:
            return list(self._items)

    def count(self):
        with self._lock:
            return len(self._items)
