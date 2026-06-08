"""
nexrad.py – NEXRAD reflectivity fetch (Iowa Environmental Mesonet WMS).

Pulls a composite base-reflectivity (N0Q) PNG for the current map view from
IEM's free WMS (no key).  Like the METAR poller it's view-driven, so it
loads radar for wherever the map is panned/zoomed, and only while the NEXRAD
overlay is the active layer (CPU/network are spent only when you're looking
at it).

This module is pygame-free and testable: it returns raw PNG bytes plus the
geographic bbox; the renderer decodes + georeferences the image (north-up,
one scale+blit) — see moving_map._draw_nexrad.

IEM WMS, EPSG:4326 (plate carrée — linear in lat/lon, easy to place on our
equirectangular map).  Layer ``nexrad-n0q`` updates ~every 5 minutes.
"""

import math
import threading
import time
import urllib.parse
import urllib.request

_WMS = "https://mesonet.agron.iastate.edu/cgi-bin/wms/nexrad/n0q.cgi"
_UA = "PFD-and-AHRS/nexrad (experimental EFB; contact via repo)"
_NM_PER_DEG = 60.0


def bbox_for(lat, lon, radius_nm):
    """(west, south, east, north) degrees for a view centred on lat/lon."""
    dlat = radius_nm / _NM_PER_DEG
    dlon = radius_nm / (_NM_PER_DEG * max(0.05, math.cos(math.radians(lat))))
    return (lon - dlon, lat - dlat, lon + dlon, lat + dlat)


def wms_url(bbox, width, height):
    """Build an IEM WMS GetMap URL.  bbox = (west, south, east, north).
    WMS 1.1.1 + EPSG:4326 takes bbox as minx,miny,maxx,maxy = W,S,E,N."""
    w, s, e, n = bbox
    q = {
        "service": "WMS", "version": "1.1.1", "request": "GetMap",
        "layers": "nexrad-n0q", "styles": "",
        "srs": "EPSG:4326",
        "bbox": f"{w:.5f},{s:.5f},{e:.5f},{n:.5f}",
        "width": str(int(width)), "height": str(int(height)),
        "format": "image/png", "transparent": "true",
    }
    return _WMS + "?" + urllib.parse.urlencode(q)


def image_size(bbox, max_px=480):
    """Pixel (width, height) for an undistorted plate-carrée image of bbox —
    proportional to the degree extents, capped at max_px on the long side."""
    w, s, e, n = bbox
    deg_w = max(1e-6, e - w)
    deg_h = max(1e-6, n - s)
    if deg_w >= deg_h:
        width = max_px
        height = max(1, int(round(max_px * deg_h / deg_w)))
    else:
        height = max_px
        width = max(1, int(round(max_px * deg_w / deg_h)))
    return width, height


def fetch_png(lat, lon, radius_nm, max_px=480, timeout=12):
    """Fetch the NEXRAD PNG for the view.  Returns (png_bytes, bbox).
    Raises on network error so the caller can retry."""
    bbox = bbox_for(lat, lon, radius_nm)
    width, height = image_size(bbox, max_px)
    url = wms_url(bbox, width, height)
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return data, bbox


def _nm_between(a_lat, a_lon, b_lat, b_lon):
    dlat = (b_lat - a_lat) * _NM_PER_DEG
    dlon = ((b_lon - a_lon) * _NM_PER_DEG
            * math.cos(math.radians((a_lat + b_lat) / 2)))
    return math.hypot(dlat, dlon)


class NexradClient(threading.Thread):
    """View-driven NEXRAD poller (mirrors wx.WxClient).  ``view_fn`` ->
    (center_lat, center_lon, radius_nm).  ``fetch_fn`` is injectable for
    tests; defaults to fetch_png.  ``snapshot()`` -> (png_bytes, bbox, seq)
    where seq increments on each new image so the renderer knows when to
    re-decode (and only then)."""

    def __init__(self, view_fn, interval_s=300.0, fetch_fn=None,
                 max_px=480, move_refetch_frac=0.4, poll_slice_s=0.7):
        super().__init__(daemon=True, name="NexradClient")
        self.view_fn    = view_fn
        self.interval_s = max(60.0, interval_s)
        self.fetch_fn   = fetch_fn or fetch_png
        self.max_px     = max_px
        self.move_frac  = move_refetch_frac
        self.slice_s    = poll_slice_s
        self.connected  = False
        self.paused     = True            # off until NEXRAD overlay selected
        self.rx_count   = 0
        self.err_count  = 0
        self.last_err   = ""
        self.updated_s  = 0.0
        self._png       = None
        self._bbox      = None
        self._seq       = 0
        self._fetched_at = 0.0
        self._fetch_ctr  = None
        self._lock      = threading.Lock()
        self._stop      = threading.Event()

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
        if radius > 1.5 * frad or radius < 0.6 * frad:
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
                        self._fetch(lat, lon, radius)
                except Exception as e:                       # noqa: BLE001
                    self.err_count += 1
                    self.last_err = f"{type(e).__name__}: {e}"
                    self.connected = False
                    print(f"[NEXRAD] {self.last_err}")
            slept = 0.0
            while slept < self.slice_s and not self._stop.is_set():
                time.sleep(min(0.2, self.slice_s - slept))
                slept += 0.2

    def _fetch(self, lat, lon, radius):
        png, bbox = self.fetch_fn(lat, lon, radius, self.max_px)
        with self._lock:
            self._png = png
            self._bbox = bbox
            self._seq += 1
        self._fetched_at = time.monotonic()
        self._fetch_ctr = (lat, lon, radius)
        self.updated_s = self._fetched_at
        self.rx_count += 1
        self.connected = True

    def snapshot(self):
        with self._lock:
            return self._png, self._bbox, self._seq
