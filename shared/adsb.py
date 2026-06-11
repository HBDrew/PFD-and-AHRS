"""
adsb.py – Threaded GDL90/UDP listener + traffic manager for ADS-B IN.

Binds a UDP socket (port 4000 by convention) and consumes GDL90 datagrams
from an ADS-B source — a Nooelec NESDR Nano 2 dual-band receiver feeding
dump1090 + dump978 through a GDL90 bridge (the same wire format Stratux and
commercial portable receivers emit).  Decoded Traffic Reports are kept in a
table keyed by ICAO address, aged out after a staleness window, and exposed
as a thread-safe snapshot for the renderer.

Interface deliberately mirrors SSEClient (start / stop / connected /
rx_count / err_count / last_err) so the Connectivity screen can show an
ADS-B LINK row the same way it shows the AHRS link.

Usage:
    from adsb import ADSBClient
    adsb = ADSBClient(port=4000, stale_s=60)
    adsb.start()
    targets = adsb.snapshot()      # list of live target dicts
    adsb.stop()
"""

import threading
import socket
import time
import math

import gdl90
import fisb


# Reasons a target row is interesting enough to keep / draw.
DEFAULT_PORT     = 4000
DEFAULT_STALE_S  = 60.0     # drop a target after this long with no update
_SOCK_TIMEOUT_S  = 1.0      # recv timeout so stop() is responsive


class ADSBClient(threading.Thread):
    def __init__(self, port=DEFAULT_PORT, stale_s=DEFAULT_STALE_S,
                 bind_addr="0.0.0.0"):
        super().__init__(daemon=True, name="ADSBClient")
        self.port       = port
        self.stale_s    = stale_s
        self.bind_addr  = bind_addr
        self.connected  = False        # True once a datagram has arrived recently
        self.paused     = False        # demo/sim can pause live ingest

        # Diagnostic counters (parity with SSEClient / SerialClient).
        self.rx_count   = 0            # GDL90 messages decoded OK
        self.err_count  = 0            # socket / decode errors
        self.last_err   = ""
        self.uplink_count = 0          # FIS-B uplink frames seen (weather)
        # FIS-B text-weather store fed off the same GDL90 stream.  Geolocation
        # is deferred — the app passes its airports lookup in at read time.
        self.fisb         = fisb.FisbWeather()

        self._targets    = {}          # icao -> target dict (+ "last_s")
        self._ownship    = None        # last ownship report, if the source sends one
        self._heartbeat  = None
        self._last_rx_s  = 0.0
        self._lock       = threading.Lock()
        self._stop_event = threading.Event()

    # ── lifecycle ────────────────────────────────────────────────────────────
    def stop(self):
        self._stop_event.set()

    def run(self):
        while not self._stop_event.is_set():
            try:
                self._listen()
            except Exception as e:                       # noqa: BLE001
                self.err_count += 1
                self.last_err = f"{type(e).__name__}: {e}"
                print(f"[ADSB] Error: {self.last_err}")
                if not self._stop_event.is_set():
                    time.sleep(2.0)
            self.connected = False

    def _listen(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except OSError:
            pass
        sock.bind((self.bind_addr, self.port))
        sock.settimeout(_SOCK_TIMEOUT_S)
        print(f"[ADSB] Listening for GDL90 on {self.bind_addr}:{self.port}")
        try:
            while not self._stop_event.is_set():
                try:
                    data, _addr = sock.recvfrom(8192)
                except socket.timeout:
                    self._expire()              # still age out stale targets
                    self.connected = (time.monotonic() - self._last_rx_s
                                      < self.stale_s)
                    continue
                if not data or self.paused:
                    continue
                self._ingest(data)
        finally:
            sock.close()

    # ── ingest ───────────────────────────────────────────────────────────────
    def _ingest(self, data):
        now = time.monotonic()
        for msg in gdl90.decode_stream(data):
            self.rx_count += 1
            self._last_rx_s = now
            self.connected = True
            kind = msg.get("kind")
            if kind == "traffic":
                self._store_traffic(msg, now)
            elif kind == "ownship":
                with self._lock:
                    self._ownship = msg
            elif kind == "heartbeat":
                with self._lock:
                    self._heartbeat = msg
            elif kind == "uplink":
                self.uplink_count += 1
                # Fold any FIS-B text weather (METARs) into the store; the
                # internet poller backfills whatever the radio didn't deliver.
                self.fisb.ingest_gdl90_msg(msg, now_mono=now)
        self._expire(now)

    def _store_traffic(self, msg, now):
        icao = msg.get("icao")
        if not icao:
            return
        # Drop position-less reports — nothing to draw.  (lat==lon==0 is the
        # GDL90 "no position" sentinel.)
        if msg.get("lat") == 0.0 and msg.get("lon") == 0.0:
            return
        msg = dict(msg)
        msg["last_s"] = now
        with self._lock:
            self._targets[icao] = msg

    def _expire(self, now=None):
        now = now if now is not None else time.monotonic()
        cutoff = now - self.stale_s
        with self._lock:
            stale = [k for k, v in self._targets.items()
                     if v.get("last_s", 0) < cutoff]
            for k in stale:
                del self._targets[k]

    # ── readout ──────────────────────────────────────────────────────────────
    def snapshot(self):
        """Return a list of live target dicts (copies).  Cheap enough to
        call once per frame."""
        self._expire()
        with self._lock:
            return [dict(v) for v in self._targets.values()]

    def ownship(self):
        with self._lock:
            return dict(self._ownship) if self._ownship else None

    def count(self):
        with self._lock:
            return len(self._targets)


# ── Relative geometry (pure, testable) ────────────────────────────────────────
_NM_PER_DEG_LAT = 60.0


def relative(target, own_lat, own_lon, own_alt_ft):
    """Annotate a target with range_nm / bearing_deg (true, from ownship)
    and rel_alt_ft (target minus ownship, +above).  Returns a new dict;
    range/bearing are None if positions are missing.

    Uses an equirectangular approximation — accurate to well under 1% at
    the few-mile ranges that matter for traffic, and far cheaper than a
    full haversine when run over every target each frame."""
    out = dict(target)
    tlat = target.get("lat")
    tlon = target.get("lon")
    if tlat is None or tlon is None:
        out["range_nm"] = None
        out["bearing_deg"] = None
    else:
        dlat = tlat - own_lat
        dlon = (tlon - own_lon) * math.cos(math.radians((tlat + own_lat) / 2))
        dy = dlat * _NM_PER_DEG_LAT
        dx = dlon * _NM_PER_DEG_LAT
        out["range_nm"] = math.hypot(dx, dy)
        brg = math.degrees(math.atan2(dx, dy))
        out["bearing_deg"] = brg % 360.0
    talt = target.get("alt_ft")
    if talt is None or own_alt_ft is None:
        out["rel_alt_ft"] = None
    else:
        out["rel_alt_ft"] = talt - own_alt_ft
    return out


def threat_level(rel, proximate_nm=6.0, proximate_ft=1200,
                 alert_nm=3.0, alert_ft=600,
                 tau_s=30.0, floor_nm=1.0, floor_ft=400):
    """Classify a relativised target for symbol colouring + RA, TCAS-style:
        "alert"      — a real resolution threat (see below)
        "proximate"  — within the static proximate envelope (amber advisory)
        "other"      — everything else with a position
    `rel` is a dict from relative(), optionally carrying ``closure_kt``
    (range-rate, +ve = closing) for the time-based alert.

    The "alert" (red / "Traffic, Traffic") tier is **closure/time-based**,
    not a flat ring, so parallel, diverging, or co-altitude-but-not-closing
    traffic no longer nuisance-trips it:
      • Hard floor backstop — anything inside ``floor_nm`` / ``floor_ft``
        alerts regardless of closure (something right on top of us).
      • Tau alert — the target is actually converging (``closure_kt`` > 0)
        AND time-to-zero-range ``tau = range / closure`` is ≤ ``tau_s``
        AND it's within the vertical protected band (``alert_ft``) and the
        advisory range.  Diverging / non-closing traffic never trips this.
    ``alert_nm`` is retained for signature compatibility (the old static
    range ring) but no longer gates the alert tier."""
    if rel.get("alert"):
        return "alert"
    rng = rel.get("range_nm")
    if rng is None:
        return "other"
    ra = rel.get("rel_alt_ft")
    ra_abs = abs(ra) if ra is not None else 1e9
    # Hard floor backstop — close in both range and altitude.
    if rng <= floor_nm and ra_abs <= floor_ft:
        return "alert"
    # Tau-based RA — converging, soon, and vertically close.
    closure_kt = rel.get("closure_kt")
    if (closure_kt is not None and closure_kt > 0.0
            and ra_abs <= alert_ft and rng <= proximate_nm):
        tau = rng / (closure_kt / 3600.0)   # seconds to zero range
        if tau <= tau_s:
            return "alert"
    # Proximate (amber) advisory — static envelope, unchanged.
    if rng <= proximate_nm and ra_abs <= proximate_ft:
        return "proximate"
    return "other"


def filter_targets(targets, alt_band_ft=0, range_nm=0, keep_alert=True):
    """Declutter a relativised+classified target list.

    Hides targets whose relative altitude exceeds ±``alt_band_ft`` or whose
    range exceeds ``range_nm`` (0 = no limit for either).  Targets with an
    unknown altitude or range are never hidden by that respective filter —
    better to show an uncertain target than drop it.  When ``keep_alert`` is
    set, "alert"-class threats always pass regardless of the filters, so the
    declutter view can never suppress a genuine collision hazard."""
    out = []
    for t in targets:
        if keep_alert and t.get("threat") == "alert":
            out.append(t)
            continue
        ra = t.get("rel_alt_ft")
        if alt_band_ft and ra is not None and abs(ra) > alt_band_ft:
            continue
        rng = t.get("range_nm")
        if range_nm and rng is not None and rng > range_nm:
            continue
        out.append(t)
    return out


# ── Demo traffic generator ────────────────────────────────────────────────────
# Lets the ADS-B render path be exercised in --demo without a receiver.  A
# handful of targets orbit / track past the demo aircraft position.
_DEMO_SEEDS = [
    # (icao, callsign, radius_nm, deg_per_s, alt_offset_ft, emitter)
    ("AC82EC", "N512TQ",  3.0,  6.0,  +500, 1),
    ("A1B2C3", "SWA1234", 7.0, -3.0, +1500, 3),
    ("DD0042", "HELI7",   1.5, 18.0,  -300, 7),
    ("4CA1FD", "RYR88K", 10.0, -2.0, -2000, 3),
]


def demo_targets(center_lat, center_lon, center_alt_ft, t):
    """Return a list of synthetic target dicts (same shape as decoded
    traffic) positioned at time `t` seconds.  Pure function of t so it's
    smooth and reproducible."""
    out = []
    for icao, cs, radius_nm, dps, dalt, emitter in _DEMO_SEEDS:
        ang = math.radians((dps * t) % 360.0)
        dlat = (radius_nm / _NM_PER_DEG_LAT) * math.cos(ang)
        dlon = (radius_nm / _NM_PER_DEG_LAT) * math.sin(ang) / max(
            0.1, math.cos(math.radians(center_lat)))
        # Heading is tangent to the orbit.
        track = (math.degrees(ang) + (90 if dps >= 0 else -90)) % 360.0
        out.append({
            "kind": "traffic", "icao": icao, "callsign": cs,
            "lat": center_lat + dlat, "lon": center_lon + dlon,
            "alt_ft": center_alt_ft + dalt,
            "gs_kt": 120 + int(40 * math.sin(ang)),
            "track_deg": track,
            "vvel_fpm": int(500 * math.sin(ang)),
            "emitter": gdl90.EMITTER_CATEGORIES.get(emitter, ""),
            "airborne": True, "alert": False, "last_s": t,
        })
    return out
