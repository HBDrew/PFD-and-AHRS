"""
adsb_feed.py – Built-in internet ADS-B traffic feed (in-app thread).

Pulls aircraft from a public aggregator (airplanes.live / adsb.lol / adsb.fi
— no key) and sends GDL90 Traffic Reports + a Heartbeat to the local UDP
listener, so the display has an internet traffic source WITHOUT running a
separate script (and it survives reboots).  Controlled by the
``traffic_source`` setting (auto / radio / internet) via the ``paused`` flag.

This is the in-app sibling of tools/adsb_internet_feed.py (which stays as the
standalone CLI / off-board feeder).  Both speak the same GDL90/UDP so they're
interchangeable; the display merges whatever arrives on the port by ICAO.
"""

import json
import math      # noqa: F401  (kept for parity / future bbox use)
import socket
import threading
import time
import urllib.request

import gdl90

_UA = "PFD-and-AHRS/adsb_feed (experimental EFB)"
_POINT_HOSTS = {
    "airplanes_live": "https://api.airplanes.live/v2/point",
    "adsb_lol":       "https://api.adsb.lol/v2/point",
    "adsb_fi":        "https://opendata.adsb.fi/api/v2/point",
}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_point(payload):
    """readsb point-API JSON -> list of aircraft dicts."""
    out = []
    for ac in payload.get("ac", []) or []:
        hexid = str(ac.get("hex", "")).strip().upper().replace("~", "")
        lat, lon = _num(ac.get("lat")), _num(ac.get("lon"))
        if not hexid or lat is None or lon is None:
            continue
        alt = ac.get("alt_baro")
        out.append({
            "hex": hexid, "lat": lat, "lon": lon,
            "alt_ft": None if alt in (None, "ground") else _num(alt),
            "gs": _num(ac.get("gs")) or 0,
            "track": _num(ac.get("track")) or 0.0,
            "vr": _num(ac.get("baro_rate")) or _num(ac.get("geom_rate")) or 0,
            "flight": str(ac.get("flight", "")).strip(),
        })
    return out


def fetch(source, lat, lon, radius_nm, timeout=10):
    base = _POINT_HOSTS.get(source, _POINT_HOSTS["airplanes_live"])
    url = f"{base}/{lat:.5f}/{lon:.5f}/{min(250, int(radius_nm))}"
    req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return parse_point(json.loads(r.read().decode("utf-8", "ignore")))


class TrafficFeed(threading.Thread):
    """Polls an aggregator for aircraft near ``pos_fn()`` and emits GDL90 to
    (out_host, out_port).  Starts paused; the app un-pauses it based on the
    traffic_source setting.  Diagnostics mirror ADSBClient."""

    def __init__(self, pos_fn, out_host="127.0.0.1", out_port=4000,
                 source="airplanes_live", radius_nm=80.0, interval_s=8.0,
                 fetch_fn=None):
        super().__init__(daemon=True, name="TrafficFeed")
        self.pos_fn     = pos_fn
        self.dest       = (out_host, out_port)
        self.source     = source
        self.radius_nm  = radius_nm
        self.interval_s = max(2.0, interval_s)
        self.fetch_fn   = fetch_fn or fetch
        self.paused     = True
        self.connected  = False
        self.rx_count   = 0
        self.err_count  = 0
        self.last_err   = ""
        self.updated_s  = 0.0
        self.n          = 0
        self._stop      = threading.Event()
        self._sock      = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def stop(self):
        self._stop.set()

    def _emit_once(self):
        # Heartbeat first so the listener stays 'up' even with 0 aircraft.
        self._sock.sendto(gdl90.encode_heartbeat(), self.dest)
        lat, lon = self.pos_fn()
        acs = self.fetch_fn(self.source, lat, lon, self.radius_nm)
        sent = 0
        for ac in acs:
            try:
                addr = int(ac["hex"], 16)
            except (ValueError, KeyError):
                continue
            self._sock.sendto(gdl90.encode_traffic(
                addr, ac["lat"], ac["lon"], ac.get("alt_ft"),
                gs_kt=ac.get("gs", 0) or 0,
                track_deg=ac.get("track", 0.0) or 0.0,
                vvel_fpm=ac.get("vr", 0) or 0,
                callsign=ac.get("flight", "")), self.dest)
            sent += 1
        self.n = sent
        self.rx_count += 1
        self.updated_s = time.monotonic()
        self.connected = True

    def run(self):
        while not self._stop.is_set():
            if not self.paused:
                try:
                    self._emit_once()
                except Exception as e:                       # noqa: BLE001
                    self.err_count += 1
                    self.last_err = f"{type(e).__name__}: {e}"
                    self.connected = False
            else:
                self.connected = False
            slept = 0.0
            while slept < self.interval_s and not self._stop.is_set():
                time.sleep(min(1.0, self.interval_s - slept))
                slept += 1.0
