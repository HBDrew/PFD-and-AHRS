"""
fisb.py – FIS-B (Flight Information Services – Broadcast) decoder for 978 UAT
uplink frames.

This is the radio-side weather decoder that mirrors the radio-primary /
internet-bonus model we already built for traffic: weather over the air with no
internet needed.  It takes the **uplink** payload that ``gdl90.decode_message``
already surfaces (``kind="uplink"``) and pulls the text weather reports out of
it, normalising METARs into the *same* station dicts that ``wx.parse_metars``
produces — so the airport-tap picker and the MET overlay consume them with no
changes downstream.

Pipeline (UAT Ground Uplink, per RTCA DO-282B / DO-267A):

    GDL90 Uplink Data msg (0x07)
      └─ id(1) + Time-of-Reception(3) + UAT uplink payload(432)
           └─ UAT header(8) + application data block(424)
                └─ a run of "information frames": 9-bit length + 4-bit type
                     └─ type 0 = FIS-B APDU: product id + optional timestamp
                          └─ product 413 etc. = DLAC-packed report text

Staging note: the **framing** (information-frame walk) and the **DLAC** 6-bit
text decode are the well-specified, dump978-confirmed parts and are covered by
round-trip tests in ``test_fisb.py``.  The FIS-B APDU header bit layout follows
DO-267A; the *data offset* per time-option is pinned by the round-trip tests,
but the decoded **timestamps** should be sanity-checked against live 978 frames
before they're trusted for anything beyond display.  Reception is line-of-sight
from ground stations and sparse on the ground / at low altitude — internet
backfills the gaps, same as traffic.

This module is transport-agnostic and side-effect free.
"""

import calendar
import re
import threading
import time

import wx as _wx


# ── FIS-B product IDs (DO-267A / SDDS) ──────────────────────────────────────────
# Only the ones we care about near-term are named; everything else falls back to
# "Product <n>" so an unexpected id is still visible rather than silently
# dropped.
PRODUCT_NAMES = {
    8:   "NOTAM",
    11:  "AIRMET",
    12:  "SIGMET",
    13:  "SUA Status",
    14:  "G-AIRMET",
    15:  "CWA",
    16:  "NOTAM-TFR",
    63:  "NEXRAD Regional",
    64:  "NEXRAD CONUS",
    70:  "Icing (Low)",
    71:  "Icing (High)",
    84:  "Cloud Tops",
    90:  "Turbulence (Low)",
    91:  "Turbulence (High)",
    103: "Lightning",
    413: "Generic Text",      # METAR / SPECI / TAF / PIREP / Winds Aloft
}

# FIS-B information-frame types (the 4-bit type in the frame header).
FRAME_TYPE_FISB_APDU = 0


def product_name(pid):
    return PRODUCT_NAMES.get(pid, "Product %d" % pid)


# ── DLAC: the FIS-B 6-bit "aviation" character set ──────────────────────────────
# 64-entry alphabet, packed 6 bits/char MSB-first.  Index 0 (ETX, 0x03) marks
# end-of-text; 0x1e (record separator) splits reports.  Indices 32..63 map to
# ASCII 0x20..0x3f (space ! " # ... 0-9 : ; < = > ?).
_DLAC = (
    "\x03"                          # 0   : ETX
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"    # 1-26: A-Z
    "\x1a\t\x1e\n|"                 # 27-31
    + "".join(chr(c) for c in range(0x20, 0x40))   # 32-63: space..?
)
assert len(_DLAC) == 64


def dlac_decode(data):
    """Decode DLAC-packed bytes into a string.  Bits are taken big-endian, 6 at
    a time; leftover bits (< 6) at the tail are ignored."""
    out = []
    bits = 0
    nbits = 0
    for byte in data:
        bits = (bits << 8) | byte
        nbits += 8
        while nbits >= 6:
            nbits -= 6
            out.append(_DLAC[(bits >> nbits) & 0x3f])
    return "".join(out)


# ── Information-frame framing ───────────────────────────────────────────────────
def iter_information_frames(app_data):
    """Walk the 424-byte application-data block as a run of information frames.

    Each frame header is 2 bytes::

        length = (b0 << 1) | (b1 >> 7)      # 9 bits
        reserved = (b1 >> 4) & 0x07         # 3 bits
        frame_type = b1 & 0x0f              # 4 bits

    followed by ``length`` data bytes.  A zero-length frame (the FILL byte
    pattern) marks the end of real frames — the rest of the block is padding.
    Yields ``(frame_type, frame_bytes)`` and stops cleanly on a truncated tail.
    """
    i = 0
    n = len(app_data)
    while i + 2 <= n:
        b0 = app_data[i]
        b1 = app_data[i + 1]
        length = (b0 << 1) | (b1 >> 7)
        frame_type = b1 & 0x0f
        if length == 0:
            break                           # FILL / end of frames
        start = i + 2
        end = start + length
        if end > n:
            break                           # truncated — stop, don't guess
        yield frame_type, app_data[start:end]
        i = end


# ── FIS-B APDU header ───────────────────────────────────────────────────────────
def decode_apdu(frame):
    """Decode one FIS-B APDU (the data of a frame-type-0 information frame).

    Header layout (DO-267A)::

        octet0:  S(1) | T-opt(2) | product-id[10:6](5)
        octet1:  product-id[5:0](6) | (2 bits consumed by the timestamp)

    The 2-bit T-opt selects how many timestamp bytes follow, which fixes where
    the report data starts:

        0  no timestamp  -> data at offset 2
        1  hours+minutes -> data at offset 4
        2  +month/day    -> data at offset 5  (Hours/Mins + date)
        3  reserved      -> treated as offset 4

    Returns a dict; ``data`` is the report payload (DLAC-packed for text
    products).  Returns ``None`` if the frame is too short to hold a header.
    """
    if len(frame) < 2:
        return None
    s_flag = (frame[0] >> 7) & 0x01
    t_opt = (frame[0] >> 5) & 0x03
    product_id = ((frame[0] & 0x1f) << 6) | (frame[1] >> 2)

    hours = minutes = month = day = None
    if t_opt == 0:
        offset = 2
    elif t_opt == 1:
        if len(frame) < 4:
            return None
        hours = ((frame[1] & 0x03) << 3) | (frame[2] >> 5)
        minutes = frame[2] & 0x1f          # 5-bit minutes (coarse)
        offset = 4
    elif t_opt == 2:
        if len(frame) < 5:
            return None
        month = ((frame[1] & 0x03) << 2) | (frame[2] >> 6)
        day = (frame[2] >> 1) & 0x1f
        hours = ((frame[2] & 0x01) << 4) | (frame[3] >> 4)
        minutes = ((frame[3] & 0x0f) << 2) | (frame[4] >> 6)
        offset = 5
    else:                                   # 3: reserved
        offset = 4

    return {
        "product_id": product_id,
        "name": product_name(product_id),
        "s_flag": bool(s_flag),
        "t_opt": t_opt,
        "month": month, "day": day,
        "hours": hours, "minutes": minutes,
        "data": frame[offset:],
    }


def text_records(apdu_data):
    """DLAC-decode a text-product APDU payload and split it into individual
    report strings.  Reports are separated by ETX (0x03) and/or RS (0x1e);
    blank fragments are dropped and surrounding whitespace trimmed."""
    text = dlac_decode(apdu_data)
    parts = re.split(r"[\x03\x1e]", text)
    return [p.strip() for p in parts if p.strip()]


# ── Raw-METAR text parsing ──────────────────────────────────────────────────────
# FIS-B text reports arrive as the raw observation string (no structured JSON
# like the AWC feed), so we parse the fields we need to colour the dot and fill
# the picker.  Deliberately tolerant: anything we can't read stays None.
_RE_STATION = re.compile(r"\b([A-Z][A-Z0-9]{3})\b")
_RE_DDHHMM  = re.compile(r"\b(\d{2})(\d{2})(\d{2})Z\b")
_RE_WIND    = re.compile(r"\b(\d{3}|VRB)(\d{2,3})(?:G(\d{2,3}))?KT\b")
_RE_VIS_SM  = re.compile(r"\b(M)?(\d{1,2})(?:\s+(\d)/(\d))?SM\b|\b(\d)/(\d)SM\b")
_RE_CLOUD   = re.compile(r"\b(FEW|SCT|BKN|OVC|VV)(\d{3})\b")
_RE_ALT_A   = re.compile(r"\bA(\d{4})\b")
_RE_ALT_Q   = re.compile(r"\bQ(\d{4})\b")
_RE_TEMP    = re.compile(r"\b(M?\d{2})/(M?\d{2})\b")

_INHG_TO_HPA = 33.8638866667


def _signed(token):
    """METAR temperatures: 'M05' -> -5, '12' -> 12."""
    if token is None:
        return None
    token = token.strip()
    if token.startswith("M"):
        return -int(token[1:])
    return int(token)


def parse_metar_text(raw):
    """Parse a raw METAR/SPECI string into the field subset the display needs.

    Returns a dict with icao / wind / visibility / ceiling / altimeter / temp
    and a derived flight category, or ``None`` if it doesn't look like a METAR.
    """
    if not raw:
        return None
    s = raw.strip().upper()
    # Strip a leading METAR / SPECI keyword so the station regex lands on the id.
    body = re.sub(r"^(METAR|SPECI)\s+", "", s)

    mstn = _RE_STATION.match(body)
    mtime = _RE_DDHHMM.search(body)
    # A METAR is an ICAO id immediately followed by a DDHHMMZ group.  Require
    # both so we don't mistake a TAF / PIREP / NOTAM line for an observation.
    if not mstn or not mtime:
        return None
    icao = mstn.group(1)

    out = {
        "icao": icao,
        "obs_day": int(mtime.group(1)),
        "obs_hour": int(mtime.group(2)),
        "obs_min": int(mtime.group(3)),
        "wdir": None, "wspd": None, "wgst": None,
        "visib_mi": None, "ceiling_ft": None,
        "altim_hpa": None, "temp_c": None, "dewp_c": None,
        "raw": raw.strip(),
    }

    mw = _RE_WIND.search(body)
    if mw:
        out["wdir"] = None if mw.group(1) == "VRB" else int(mw.group(1))
        out["wspd"] = int(mw.group(2))
        out["wgst"] = int(mw.group(3)) if mw.group(3) else None

    mv = _RE_VIS_SM.search(body)
    if mv:
        if mv.group(5) is not None:         # bare fraction "1/2SM"
            out["visib_mi"] = int(mv.group(5)) / int(mv.group(6))
        else:
            whole = int(mv.group(2))
            if mv.group(3) is not None:     # "1 1/2SM"
                whole += int(mv.group(3)) / int(mv.group(4))
            # "M1/4SM" style (< value) keeps the small number — good enough for
            # category, which only cares that it's below the IFR thresholds.
            out["visib_mi"] = float(whole)

    bases = [int(g2) * 100 for g1, g2 in _RE_CLOUD.findall(body)
             if g1 in ("BKN", "OVC", "VV")]
    if bases:
        out["ceiling_ft"] = min(bases)

    ma = _RE_ALT_A.search(body)
    mq = _RE_ALT_Q.search(body)
    if ma:
        out["altim_hpa"] = round(int(ma.group(1)) / 100.0 * _INHG_TO_HPA, 1)
    elif mq:
        out["altim_hpa"] = float(int(mq.group(1)))

    mt = _RE_TEMP.search(body)
    if mt:
        out["temp_c"] = _signed(mt.group(1))
        out["dewp_c"] = _signed(mt.group(2))

    out["fltcat"] = _wx.derive_flight_category(out["visib_mi"],
                                               out["ceiling_ft"])
    return out


def _obs_age_min(parsed, now):
    """Minutes since the METAR's DDHHMMZ stamp, using ``now`` (epoch seconds,
    UTC) to resolve the day-of-month.  Handles the month wrap when the report
    day is greater than today (i.e. last month)."""
    try:
        tm = time.gmtime(now)
        obs = time.struct_time((
            tm.tm_year, tm.tm_mon, parsed["obs_day"],
            parsed["obs_hour"], parsed["obs_min"], 0, 0, 0, 0))
        obs_epoch = calendar.timegm(obs)
        # Report dated later in the month than 'now' → it's from last month.
        if obs_epoch - now > 12 * 3600:
            prev_mon = tm.tm_mon - 1 or 12
            prev_year = tm.tm_year - (1 if tm.tm_mon == 1 else 0)
            obs = time.struct_time((
                prev_year, prev_mon, parsed["obs_day"],
                parsed["obs_hour"], parsed["obs_min"], 0, 0, 0, 0))
            obs_epoch = calendar.timegm(obs)
        return max(0.0, (now - obs_epoch) / 60.0)
    except (ValueError, OverflowError):
        return None


def _station_from_parsed(parsed, lat, lon, now):
    """Build the wx-shaped station dict from a parsed METAR + coordinates."""
    return {
        "icao": parsed["icao"],
        "lat": float(lat), "lon": float(lon),
        "fltcat": parsed["fltcat"],
        "wdir": parsed["wdir"], "wspd": parsed["wspd"], "wgst": parsed["wgst"],
        "visib_mi": parsed["visib_mi"], "ceiling_ft": parsed["ceiling_ft"],
        "altim_hpa": parsed["altim_hpa"],
        "temp_c": parsed["temp_c"], "dewp_c": parsed["dewp_c"],
        "wx": "",
        "name": "",
        "raw": parsed["raw"],
        "age_min": _obs_age_min(parsed, now),
        "src": "RDR",                       # radio (FIS-B), vs "INET" for AWC
    }


def metar_station(raw, locate, now=None):
    """Turn a raw METAR string into a station dict matching
    ``wx.parse_metars`` output, geolocating the ICAO id via ``locate``.

    ``locate(icao) -> (lat, lon) | None`` — supply an airport-DB lookup (FIS-B
    text carries no position).  Returns ``None`` if the text isn't a METAR or
    the station can't be located (no point drawing a dot with no coordinates).
    """
    parsed = parse_metar_text(raw)
    if parsed is None:
        return None
    pos = locate(parsed["icao"]) if locate else None
    if not pos or pos[0] is None or pos[1] is None:
        return None
    now = now if now is not None else time.time()
    return _station_from_parsed(parsed, pos[0], pos[1], now)


# ── Top-level uplink decode ─────────────────────────────────────────────────────
_UAT_HEADER_LEN = 8       # 8-byte UAT uplink header precedes the app-data block


def decode_uplink(uplink_payload):
    """Decode one 432-byte UAT uplink payload into its FIS-B APDU reports.

    Accepts either the bare 432-byte uplink payload or anything longer where
    the first ``_UAT_HEADER_LEN`` bytes are the UAT header (we only skip the
    header; positional decode of it is a later item).  Returns a list of APDU
    dicts (see ``decode_apdu``), each with a ``frame_type`` added.
    """
    if not uplink_payload or len(uplink_payload) <= _UAT_HEADER_LEN:
        return []
    app_data = uplink_payload[_UAT_HEADER_LEN:]
    apdus = []
    for frame_type, frame in iter_information_frames(app_data):
        if frame_type != FRAME_TYPE_FISB_APDU:
            continue
        apdu = decode_apdu(frame)
        if apdu is not None:
            apdu["frame_type"] = frame_type
            apdus.append(apdu)
    return apdus


def decode_gdl90_uplink(msg):
    """Convenience bridge from a ``gdl90.decode_message`` result.

    ``msg`` is the dict with ``kind == "uplink"`` (its ``raw`` is the deframed
    0x07 message: id + 3-byte Time-of-Reception + 432-byte uplink payload).
    Returns the APDU list, or ``[]`` for anything that isn't a usable uplink.
    """
    if not msg or msg.get("kind") != "uplink":
        return []
    raw = msg.get("raw")
    if not raw or len(raw) < 4:
        return []
    return decode_uplink(raw[4:])           # skip id(1) + Time-of-Reception(3)


def decode_ground_station(uplink_payload):
    """Decode the transmitting ground station's position from the 8-byte UAT
    uplink header (bit layout per DO-282B / dump978's uat_decode.c).

    Returns ``{lat, lon, position_valid, site_id}`` or ``None`` if the payload
    is too short.  ``lat``/``lon`` are only meaningful when ``position_valid``.
    This is the tower you're actually hearing — both the "where's my FIS-B
    coming from" answer and the reception indicator, straight out of the frame
    (no station database needed).
    """
    f = uplink_payload
    if not f or len(f) < 8:
        return None
    raw_lat = (f[0] << 15) | (f[1] << 7) | (f[2] >> 1)
    raw_lon = ((f[2] & 0x01) << 23) | (f[3] << 15) | (f[4] << 7) | (f[5] >> 1)
    lat = raw_lat * 360.0 / 16777216.0
    if lat > 90:
        lat -= 180
    lon = raw_lon * 360.0 / 16777216.0
    if lon > 180:
        lon -= 360
    return {
        "lat": lat, "lon": lon,
        "position_valid": bool(f[5] & 0x01),
        "site_id": f[7] >> 4,
    }


def metars_from_apdus(apdus, locate, now=None):
    """Collect every METAR station dict from a list of decoded APDUs.

    Walks the text products, parses each record, and keeps the ones that are
    METARs *and* could be geolocated — ready to merge into the same list the
    picker and MET overlay already draw."""
    now = now if now is not None else time.time()
    stations = []
    for apdu in apdus:
        if not apdu.get("data"):
            continue
        for rec in text_records(apdu["data"]):
            st = metar_station(rec, locate, now)
            if st is not None:
                stations.append(st)
    return stations


# ── Live store + source merge ───────────────────────────────────────────────────
class FisbWeather:
    """Thread-safe store of the most recent FIS-B text weather, fed raw GDL90
    uplink messages off the radio.

    Keeps the latest *parsed* METAR per station (re-parsing is wasted work; the
    same report rebroadcasts every cycle).  Geolocation is deferred to read
    time via a caller-supplied ``locate`` so this module stays free of any
    airport-DB dependency — the app owns the airports array and passes a lookup
    in when it builds the draw list.

    Entries age out by *receipt* time (``expire_s``): if a ground station drops
    off the air we stop showing its last report after the window, and the
    internet poller backfills.  The METAR's own ``age_min`` (from its DDHHMMZ
    stamp) is recomputed on every read so the picker shows true observation age.
    """

    def __init__(self, expire_s=4500.0, station_expire_s=120.0):
        self.expire_s = expire_s             # 75 min: METARs refresh hourly
        self.station_expire_s = station_expire_s   # towers rebroadcast often
        self.uplink_count = 0                # uplink frames ingested
        self.metar_count = 0                 # METAR records parsed OK (cumulative)
        self._lock = threading.Lock()
        self._metars = {}                    # icao -> (parsed_dict, recv_monotonic)
        self._stations = {}                  # (lat,lon) -> {.., last_mono, count}

    def ingest_gdl90_msg(self, msg, now_mono=None):
        """Decode one ``kind=="uplink"`` GDL90 message and fold its weather +
        ground-station info into the store.  Cheap to call from the UDP path."""
        if not msg or msg.get("kind") != "uplink":
            return
        raw = msg.get("raw")
        if not raw or len(raw) < 4:
            return
        self.ingest_uplink(raw[4:], now_mono)   # drop id + Time-of-Reception

    def ingest_uplink(self, uplink_payload, now_mono=None):
        """Ingest a bare UAT uplink payload: record the transmitting ground
        station (from the header) and fold in any METARs (from the APDUs)."""
        if not uplink_payload:
            return
        now_mono = now_mono if now_mono is not None else time.monotonic()
        self.uplink_count += 1

        gs = decode_ground_station(uplink_payload)
        metars = {}
        for apdu in decode_uplink(uplink_payload):
            if not apdu.get("data"):
                continue
            for rec in text_records(apdu["data"]):
                parsed = parse_metar_text(rec)
                if parsed is not None:
                    metars[parsed["icao"]] = parsed

        with self._lock:
            # Tower we're hearing — keyed by rounded position so one physical
            # station de-dupes while distinct towers stay separate.
            if gs and gs["position_valid"]:
                key = (round(gs["lat"], 3), round(gs["lon"], 3))
                prev = self._stations.get(key)
                self._stations[key] = {
                    "lat": gs["lat"], "lon": gs["lon"],
                    "site_id": gs["site_id"],
                    "last_mono": now_mono,
                    "count": (prev["count"] + 1) if prev else 1,
                }
            for icao, parsed in metars.items():
                self._metars[icao] = (parsed, now_mono)
                self.metar_count += 1

    def metar_stations(self, locate, now=None, now_mono=None):
        """Return wx-shaped station dicts for every stored, still-fresh METAR
        that ``locate`` can geolocate.  Prunes receipt-expired entries."""
        now = now if now is not None else time.time()
        now_mono = now_mono if now_mono is not None else time.monotonic()
        with self._lock:
            expired = [k for k, (_p, recv) in self._metars.items()
                       if now_mono - recv > self.expire_s]
            for k in expired:
                del self._metars[k]
            items = list(self._metars.items())
        out = []
        for icao, (parsed, _recv) in items:
            pos = locate(icao) if locate else None
            if not pos or pos[0] is None or pos[1] is None:
                continue
            out.append(_station_from_parsed(parsed, pos[0], pos[1], now))
        return out

    def ground_stations(self, now_mono=None):
        """FIS-B ground stations heard within ``station_expire_s``, each as
        ``{lat, lon, site_id, age_s, count}``.  Drawing these is both the
        "where's my weather coming from" map cue and the live reception
        indicator (a dot here == FIS-B is being received right now)."""
        now_mono = now_mono if now_mono is not None else time.monotonic()
        with self._lock:
            stale = [k for k, v in self._stations.items()
                     if now_mono - v["last_mono"] > self.station_expire_s]
            for k in stale:
                del self._stations[k]
            out = [{"lat": v["lat"], "lon": v["lon"], "site_id": v["site_id"],
                    "age_s": now_mono - v["last_mono"], "count": v["count"]}
                   for v in self._stations.values()]
        return out

    def count(self):
        with self._lock:
            return len(self._metars)


def merge_metar_sources(rdr, inet):
    """Merge radio (FIS-B) and internet (AWC) METAR lists into one draw list.

    Radio wins per station — it's local and, when present, at least as fresh as
    the internet pull, and keeping one dot per field avoids double-drawing.
    Internet backfills every station radio didn't deliver.  Each dict is tagged
    ``src`` ("RDR"/"INET") so the status line can count the two sources."""
    by = {}
    for m in inet or []:
        d = dict(m)
        d.setdefault("src", "INET")
        by[d.get("icao")] = d
    for m in rdr or []:
        d = dict(m)
        d.setdefault("src", "RDR")
        by[d.get("icao")] = d
    by.pop(None, None)
    return list(by.values())
