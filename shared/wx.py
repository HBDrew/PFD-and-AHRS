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


def parse_open_meteo_winds(data, alts, now=None, hour_offset=0):
    """Parse an Open-Meteo pressure-level response into winds-aloft dicts:
    ``{station, lat, lon, levels, src, hour_offset}``.  ``data`` is one point
    object or a list of them (Open-Meteo returns a list when several coords are
    queried).  For each point the hour nearest ``now + hour_offset`` is taken
    and the levels interpolated onto ``alts``.  ``station`` is a stable
    coordinate label (the grid carries no ICAO)."""
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
        hi = _nearest_hour_index(times, target)
        samples = []
        for hpa in _OM_LEVELS:
            hgt = _series_at(hourly.get(f"geopotential_height_{hpa}hPa"), hi)
            spd = _series_at(hourly.get(f"windspeed_{hpa}hPa"), hi)
            wdir = _series_at(hourly.get(f"winddirection_{hpa}hPa"), hi)
            temp = _series_at(hourly.get(f"temperature_{hpa}hPa"), hi)
            if hgt is None or spd is None or wdir is None:
                continue
            samples.append((hgt * _M_TO_FT, wdir, spd, temp))
        levels = interp_winds(samples, alts)
        if not levels:
            continue
        out.append({"station": f"{lat:.2f},{lon:.2f}",
                    "lat": lat, "lon": lon, "levels": levels, "src": "INET",
                    "hour_offset": hour_offset})
    return out


def _series_at(series, idx):
    if not isinstance(series, list) or idx is None or idx >= len(series):
        return None
    return _num(series[idx])


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


def _winds_grid_points(lat, lon, range_nm, aspect=1.0, cols=5, rows=4):
    """A lat/lon grid that fills the *visible* map, for batched winds barbs.

    ``range_nm`` is the map's shorter-axis half-extent (its vertical half-height
    in landscape); ``aspect`` = screen width/height widens the grid horizontally
    so barbs reach the left/right edges instead of bunching in a centre column.
    The 0.82 inset keeps the edge barbs (and their temp tags) on screen."""
    span_lat = range_nm * 0.82
    span_lon = range_nm * 0.82 * max(1.0, aspect)
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


def _fetch_om_winds(pts, alts, hour_offset, timeout):
    """Issue one batched Open-Meteo pressure-level request for ``pts``
    (``[(lat, lon), ...]``) and parse the response onto ``alts``."""
    lats = ",".join(f"{p[0]:.3f}" for p in pts)
    lons = ",".join(f"{p[1]:.3f}" for p in pts)
    fields = []
    for hpa in _OM_LEVELS:
        fields += [f"windspeed_{hpa}hPa", f"winddirection_{hpa}hPa",
                   f"temperature_{hpa}hPa", f"geopotential_height_{hpa}hPa"]
    # forecast_days=2 so a +24 h offset still has data late in the UTC day.
    url = (f"{_OPEN_METEO}?latitude={lats}&longitude={lons}"
           f"&hourly={','.join(fields)}&windspeed_unit=kn"
           f"&forecast_days=2&timeformat=iso8601&timezone=UTC")
    req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8", "ignore"))
    return parse_open_meteo_winds(data, alts, hour_offset=hour_offset)


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
        if radius > 1.5 * frad or radius < 0.6 * frad:
            return True                                  # zoom changed a lot
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
        if radius > 1.6 * frad or radius < 0.5 * frad:
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
