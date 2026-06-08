"""
adsb_feed.py – Built-in internet ADS-B traffic feed (in-app thread).

Pulls aircraft from a public aggregator (airplanes.live / adsb.lol / adsb.fi
— no key) so the display has an internet traffic source WITHOUT running a
separate script (and it survives reboots).  Controlled by the
``traffic_source`` setting (auto / radio / internet) via the ``paused`` flag.

Two delivery modes:

  * **in-process** (``loopback=False``, the in-app default) — decoded targets
    are kept in a thread-safe table and read with ``snapshot()``, exactly like
    ``ADSBClient``.  This keeps the internet feed on a SEPARATE path from the
    GDL90/UDP radio listener, so the two sources never co-mingle: anything on
    UDP is genuinely from the radio/SDR bridge, and anything here is genuinely
    from the internet.  The app tags each list with its source and can show
    split counts, and "radio" can never silently be internet.

  * **loopback** (``loopback=True``) — the legacy behaviour: encode GDL90 and
    send Traffic Reports + a Heartbeat to (out_host, out_port).  Used by the
    standalone tools/adsb_internet_feed.py off-board feeder, which has no
    in-process consumer to hand a snapshot to.
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

DEFAULT_STALE_S = 60.0    # drop an internet target after this long with no update


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


def _as_target(ac):
    """Aggregator aircraft dict -> decoded-GDL90-traffic shape (the same keys
    ADSBClient.snapshot() returns), tagged source='internet' so the renderer
    and status line can tell it apart from radio."""
    try:
        addr = int(ac["hex"], 16)
    except (ValueError, KeyError, TypeError):
        return None
    icao = f"{addr:06X}"
    return {
        "kind":      "traffic",
        "icao":      icao,
        "address":   addr,
        "lat":       ac["lat"],
        "lon":       ac["lon"],
        "alt_ft":    ac.get("alt_ft"),
        "gs_kt":     ac.get("gs", 0) or 0,
        "track_deg": ac.get("track", 0.0) or 0.0,
        "vvel_fpm":  ac.get("vr", 0) or 0,
        "callsign":  ac.get("flight", ""),
        "emitter":   "",
        "airborne":  True,
        "alert":     False,
        "src":       "internet",
    }


class TrafficFeed(threading.Thread):
    """Polls an aggregator for aircraft near ``pos_fn()``.  Starts paused; the
    app un-pauses it based on the traffic_source setting.  Diagnostics mirror
    ADSBClient (connected / rx_count / err_count / updated_s / n).

    When ``loopback`` is False (in-app default), decoded targets are kept in a
    table and read via ``snapshot()`` — a path entirely separate from the
    GDL90/UDP radio listener.  When True, GDL90 frames are sent to
    (out_host, out_port) instead (legacy / standalone feeder)."""

    def __init__(self, pos_fn, out_host="127.0.0.1", out_port=4000,
                 source="airplanes_live", radius_nm=80.0, interval_s=8.0,
                 fetch_fn=None, loopback=False, stale_s=DEFAULT_STALE_S):
        super().__init__(daemon=True, name="TrafficFeed")
        self.pos_fn     = pos_fn
        self.dest       = (out_host, out_port)
        self.source     = source
        self.radius_nm  = radius_nm
        self.interval_s = max(2.0, interval_s)
        self.fetch_fn   = fetch_fn or fetch
        self.loopback   = loopback
        self.stale_s    = stale_s
        self.paused     = True
        self.connected  = False
        self.rx_count   = 0
        self.err_count  = 0
        self.last_err   = ""
        self.updated_s  = 0.0
        self.n          = 0
        self._targets   = {}     # icao -> target dict (+ "last_s") [in-process]
        self._lock      = threading.Lock()
        self._stop      = threading.Event()
        self._sock      = (socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                           if loopback else None)

    def stop(self):
        self._stop.set()

    # ── readout (in-process mode) ────────────────────────────────────────────
    def _expire(self, now=None):
        now = now if now is not None else time.monotonic()
        cutoff = now - self.stale_s
        with self._lock:
            for k in [k for k, v in self._targets.items()
                      if v.get("last_s", 0) < cutoff]:
                del self._targets[k]

    def snapshot(self):
        """Live internet targets (copies), aged out past stale_s.  Empty when
        paused/idle.  Each carries src='internet'."""
        self._expire()
        with self._lock:
            return [dict(v) for v in self._targets.values()]

    def _emit_once(self):
        lat, lon = self.pos_fn()
        acs = self.fetch_fn(self.source, lat, lon, self.radius_nm)
        now = time.monotonic()
        if self.loopback:
            # Legacy: heartbeat first so the listener stays 'up' at 0 aircraft.
            self._sock.sendto(gdl90.encode_heartbeat(), self.dest)
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
        else:
            fresh = {}
            for ac in acs:
                t = _as_target(ac)
                if t is None:
                    continue
                t["last_s"] = now
                fresh[t["icao"]] = t
            with self._lock:
                self._targets.update(fresh)
            self._expire(now)
            with self._lock:
                self.n = len(self._targets)
        self.rx_count += 1
        self.updated_s = now
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
                # Drop stale internet targets while paused so switching to
                # RADIO clears the internet picture instead of freezing it.
                if not self.loopback:
                    with self._lock:
                        self._targets.clear()
                    self.n = 0
            slept = 0.0
            while slept < self.interval_s and not self._stop.is_set():
                time.sleep(min(1.0, self.interval_s - slept))
                slept += 1.0
