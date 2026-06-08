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
_UA = "PFD-and-AHRS/wx (experimental EFB; contact via repo)"
_NM_PER_DEG = 60.0


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
