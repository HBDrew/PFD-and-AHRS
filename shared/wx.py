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

import json
import math
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
_UA = "PFD-and-AHRS/wx (experimental EFB; contact via repo)"
_NM_PER_DEG = 60.0

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
        prev_view = None
        while not self._stop.is_set():
            if not self.paused:
                try:
                    lat, lon, radius = self.view_fn()
                    # Debounce: only act once the view has settled (two
                    # consecutive slices agree) so a long pan doesn't fire a
                    # burst of fetches mid-drag.
                    cur = (round(lat, 2), round(lon, 2), round(radius))
                    settled = (cur == prev_view)
                    prev_view = cur
                    now = time.monotonic()
                    if settled and self._should_fetch(lat, lon, radius, now):
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
        prev_view = None
        while not self._stop.is_set():
            if not self.paused:
                try:
                    lat, lon, radius = self.view_fn()
                    cur = (round(lat, 2), round(lon, 2), round(radius))
                    settled = (cur == prev_view)
                    prev_view = cur
                    now = time.monotonic()
                    if settled and self._should_fetch(lat, lon, radius, now):
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
