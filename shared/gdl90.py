"""
gdl90.py – GDL90 message decoder for ADS-B IN traffic + weather.

GDL90 is the de-facto interface format emitted by portable ADS-B receivers
(Stratux, and most commercial GDL90 bridges) over UDP.  A Nooelec NESDR
Nano 2 dual-band bundle feeds dump1090 / dump978 (1090ES + 978 UAT); a
GDL90 source broadcasts the decoded reports as UDP datagrams (port 4000 by
convention) which this module turns into Python dictionaries.

This module is transport-agnostic and side-effect free — it only deframes
bytes and decodes message payloads.  The threaded UDP listener that feeds
it lives in shared/adsb.py.

Reference: FAA "GDL 90 Data Interface Specification" (560-1058-00 Rev A),
§2.2 (framing / CRC) and §3.4 (Traffic / Ownship reports).

Decoded message types:
    Heartbeat              (0x00)
    Ownship Report         (0x0A)
    Ownship Geo Altitude   (0x0B)
    Traffic Report         (0x14)
    Uplink Data / FIS-B    (0x07)   — captured, payload left raw
    ForeFlight extension   (0x65)   — sub-id 0 = device ID, 1 = AHRS
                                      (Sentry / Stratus / ForeFlight sources)

Each decoder returns a dict with a "kind" key; unknown / malformed
messages are skipped.
"""

import time

# ── Framing constants ─────────────────────────────────────────────────────────
_FLAG    = 0x7E    # frame delimiter
_ESC     = 0x7D    # control-escape byte
_ESC_XOR = 0x20    # XOR mask applied to the escaped byte

# Message IDs
MSG_HEARTBEAT   = 0x00
MSG_UPLINK      = 0x07   # FIS-B uplink (weather)
MSG_OWNSHIP     = 0x0A
MSG_OWNSHIP_GEO = 0x0B
MSG_TRAFFIC     = 0x14
MSG_FOREFLIGHT  = 0x65   # ForeFlight GDL90 extension (Sentry AHRS / device ID)
FF_SUBID_ID     = 0x00   #   sub-id 0: device identification
FF_SUBID_AHRS   = 0x01   #   sub-id 1: AHRS (roll/pitch/heading/IAS/TAS)

# Emitter category codes (subset commonly seen — others fall through to "").
EMITTER_CATEGORIES = {
    0: "", 1: "LIGHT", 2: "SMALL", 3: "LARGE", 4: "VLARGE", 5: "HEAVY",
    6: "HIPERF", 7: "ROTOR", 9: "GLIDER", 10: "BALLOON", 11: "SKYDIVE",
    14: "UAV", 15: "SPACE", 17: "SURF-VEH", 18: "SURF-VEH", 19: "OBSTACLE",
}


# ── CRC-16 (GDL90 flavour, polynomial 0x1021) ─────────────────────────────────
def _build_crc_table():
    """Pre-compute the 256-entry CRC-16-CCITT table exactly as the GDL90
    spec's crcInit() does."""
    table = [0] * 256
    for i in range(256):
        crc = (i << 8) & 0xFFFF
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
        table[i] = crc
    return table


_CRC_TABLE = _build_crc_table()


def crc_compute(block):
    """Compute the GDL90 CRC-16 over `block` (a bytes/bytearray of the
    message payload, excluding flags and the trailing CRC)."""
    crc = 0
    for b in block:
        crc = (_CRC_TABLE[crc >> 8] ^ ((crc << 8) & 0xFFFF) ^ b) & 0xFFFF
    return crc


# ── Framing ───────────────────────────────────────────────────────────────────
def _unstuff(frame):
    """Reverse GDL90 byte-stuffing on a single frame's interior bytes
    (already stripped of the surrounding 0x7E flags).  Returns the raw
    bytes, or None if an escape sequence is truncated."""
    out = bytearray()
    i = 0
    n = len(frame)
    while i < n:
        b = frame[i]
        if b == _ESC:
            i += 1
            if i >= n:
                return None           # dangling escape
            out.append(frame[i] ^ _ESC_XOR)
        else:
            out.append(b)
        i += 1
    return bytes(out)


def iter_frames(buf):
    """Split a raw byte stream into deframed, CRC-checked message payloads.

    Yields each valid message's payload bytes (message id + body, CRC
    stripped).  Handles back-to-back frames in a single UDP datagram and
    shared flag bytes.  Frames that fail unstuffing or CRC are silently
    dropped — partial/garbled datagrams shouldn't crash the listener.
    """
    n = len(buf)
    i = 0
    while i < n:
        # Find the opening flag.
        if buf[i] != _FLAG:
            i += 1
            continue
        # Find the closing flag.
        j = i + 1
        while j < n and buf[j] != _FLAG:
            j += 1
        if j >= n:
            return                     # no closing flag — wait for more data
        inner = buf[i + 1:j]
        # An empty inner block means two adjacent flags (idle fill); skip
        # the first flag and let the loop re-anchor on the second.
        if inner:
            raw = _unstuff(inner)
            if raw is not None and len(raw) >= 3:
                payload, crc_lo, crc_hi = raw[:-2], raw[-2], raw[-1]
                got = crc_lo | (crc_hi << 8)
                if crc_compute(payload) == got:
                    yield payload
        i = j                          # closing flag may open the next frame


# ── Field decoders ────────────────────────────────────────────────────────────
def _u24(b, off):
    return (b[off] << 16) | (b[off + 1] << 8) | b[off + 2]


def _u16(b, off):
    return (b[off] << 8) | b[off + 1]


def _s16(b, off):
    """16-bit big-endian two's-complement → signed int."""
    v = _u16(b, off)
    return v - 0x10000 if v & 0x8000 else v


def _semicircle_to_deg(raw24):
    """24-bit two's-complement semicircle → signed degrees
    (resolution 180 / 2^23)."""
    if raw24 & 0x800000:
        raw24 -= 0x1000000
    return raw24 * (180.0 / 0x800000)


def _decode_report(b):
    """Decode the 27-byte Traffic/Ownship report body (identical layout
    for 0x0A and 0x14).  `b` is the full payload including the message id
    at b[0].  Returns a dict of decoded fields."""
    alert        = (b[1] >> 4) & 0x0F     # 1 = traffic alert active
    addr_type    = b[1] & 0x0F
    address      = _u24(b, 2)

    lat = _semicircle_to_deg(_u24(b, 5))
    lon = _semicircle_to_deg(_u24(b, 8))

    # Altitude: 12 bits across b[11] and the high nibble of b[12].
    alt_raw = (b[11] << 4) | (b[12] >> 4)
    if alt_raw == 0xFFF:
        alt_ft = None                      # invalid / unavailable
    else:
        alt_ft = alt_raw * 25 - 1000

    misc = b[12] & 0x0F                     # track type + airborne flags
    nic  = (b[13] >> 4) & 0x0F             # containment (integrity)
    nacp = b[13] & 0x0F                     # accuracy

    # Horizontal velocity (12 bits) + vertical velocity (12 bits, signed).
    h_raw = (b[14] << 4) | (b[15] >> 4)
    v_raw = ((b[15] & 0x0F) << 8) | b[16]
    gs_kt = None if h_raw == 0xFFF else h_raw
    if v_raw == 0x800:
        vvel_fpm = None                    # no vertical-rate info
    else:
        if v_raw & 0x800:
            v_raw -= 0x1000
        vvel_fpm = v_raw * 64

    track_deg = b[17] * (360.0 / 256.0)
    emitter   = EMITTER_CATEGORIES.get(b[18], "")
    callsign  = bytes(b[19:27]).decode("ascii", "ignore").strip()

    return {
        "alert":     bool(alert),
        "addr_type": addr_type,
        "address":   address,
        "icao":      f"{address:06X}",
        "lat":       lat,
        "lon":       lon,
        "alt_ft":    alt_ft,
        "misc":      misc,
        "airborne":  bool(misc & 0x08),
        "nic":       nic,
        "nacp":      nacp,
        "gs_kt":     gs_kt,
        "vvel_fpm":  vvel_fpm,
        "track_deg": track_deg,
        "emitter":   emitter,
        "callsign":  callsign,
    }


def _decode_heartbeat(b):
    """Heartbeat (0x00): status bits + UAT timestamp + message counts."""
    if len(b) < 7:
        return None
    st1 = b[1]
    st2 = b[2]
    # Timestamp: 17 bits — low 16 in b[3..4] (LSB first) + bit16 in st2.
    ts = (b[4] << 8) | b[3]
    ts |= ((st2 & 0x80) << 9)
    return {
        "kind":          "heartbeat",
        "gps_valid":     bool(st1 & 0x80),
        "maint_req":     bool(st1 & 0x40),
        "uat_initialized": bool(st1 & 0x01),
        "utc_ok":        bool(st2 & 0x01),
        "timestamp":     ts,
    }


def _decode_ownship_geo(b):
    """Ownship Geometric Altitude (0x0B): WGS-84 geo altitude (5 ft res)
    + vertical figure of merit."""
    if len(b) < 5:
        return None
    geo_raw = (b[1] << 8) | b[2]
    if geo_raw & 0x8000:
        geo_raw -= 0x10000
    return {
        "kind":       "ownship_geo",
        "geo_alt_ft": geo_raw * 5,
        "vpl_warn":   bool(b[3] & 0x80),
        "vfom_m":     ((b[3] & 0x7F) << 8) | b[4],
    }


def _decode_foreflight(b):
    """ForeFlight GDL90 extension message (0x65).  b[1] is a sub-id:
      0x00 → device identification, 0x01 → AHRS.

    AHRS body (after the id byte): sub-id(1) roll(2) pitch(2) heading(2)
    ias(2) tas(2), big-endian.  Roll/pitch are signed 1/10°, sentinel
    0x7FFF = invalid; airspeeds are unsigned knots, sentinel 0xFFFF.  The
    heading word's top bit flags reference frame; the low 15 bits are 1/10°.

    NOTE: the true-vs-magnetic polarity of the heading MSB and the roll/pitch
    sign convention are coded from the published spec — confirm against a live
    Sentry before trusting `heading_true` / the roll sign for attitude."""
    if len(b) < 2:
        return None
    sub = b[1]
    if sub == FF_SUBID_ID:
        # version(1) serial(8) name(8) longname(16) capabilities(4)
        d = {"kind": "ff_id"}
        if len(b) >= 3:
            d["version"] = b[2]
        if len(b) >= 19:
            d["name"] = b[11:19].split(b"\x00")[0].decode("ascii", "ignore").strip()
        if len(b) >= 35:
            d["long_name"] = b[19:35].split(b"\x00")[0].decode("ascii", "ignore").strip()
        return d
    if sub == FF_SUBID_AHRS and len(b) >= 12:
        roll_raw = _u16(b, 2)
        pitch_raw = _u16(b, 4)
        hdg_raw = _u16(b, 6)
        ias_raw = _u16(b, 8)
        tas_raw = _u16(b, 10)
        d = {"kind": "ahrs"}
        d["roll"]  = None if roll_raw == 0x7FFF else _s16(b, 2) / 10.0
        d["pitch"] = None if pitch_raw == 0x7FFF else _s16(b, 4) / 10.0
        if hdg_raw == 0xFFFF:
            d["heading"] = None
            d["heading_true"] = None
        else:
            d["heading"] = (hdg_raw & 0x7FFF) / 10.0
            d["heading_true"] = (hdg_raw & 0x8000) == 0
        d["ias_kt"] = None if ias_raw == 0xFFFF else ias_raw
        d["tas_kt"] = None if tas_raw == 0xFFFF else tas_raw
        return d
    return None


def decode_message(payload):
    """Decode one deframed payload (message id at payload[0]).  Returns a
    dict with a "kind" field, or None for unknown / malformed messages."""
    if not payload:
        return None
    mid = payload[0]
    if mid == MSG_TRAFFIC and len(payload) >= 27:
        d = _decode_report(payload)
        d["kind"] = "traffic"
        return d
    if mid == MSG_OWNSHIP and len(payload) >= 27:
        d = _decode_report(payload)
        d["kind"] = "ownship"
        return d
    if mid == MSG_OWNSHIP_GEO:
        return _decode_ownship_geo(payload)
    if mid == MSG_HEARTBEAT:
        return _decode_heartbeat(payload)
    if mid == MSG_UPLINK:
        # FIS-B uplink — TIS-B/FIS-B application data.  Decoding the
        # APDU (text weather, NEXRAD blocks) is a separate effort; for
        # now surface the raw payload so the listener can count it.
        return {"kind": "uplink", "len": len(payload), "raw": bytes(payload)}
    if mid == MSG_FOREFLIGHT:
        return _decode_foreflight(payload)
    return None


def decode_stream(buf):
    """Convenience: deframe + decode a raw byte buffer into a list of
    message dicts (skipping unknowns).  Used by the UDP listener and by
    tests."""
    out = []
    for payload in iter_frames(buf):
        msg = decode_message(payload)
        if msg is not None:
            out.append(msg)
    return out


# ── Encoder (test + loopback aid) ─────────────────────────────────────────────
def _stuff(raw):
    """Apply GDL90 byte-stuffing to a payload+CRC block."""
    out = bytearray()
    for b in raw:
        if b in (_FLAG, _ESC):
            out.append(_ESC)
            out.append(b ^ _ESC_XOR)
        else:
            out.append(b)
    return out


def frame_message(payload):
    """Wrap a message payload (id + body, no CRC) in a complete GDL90
    frame with CRC and flags.  Primarily for tests and synthetic feeds."""
    payload = bytes(payload)
    crc = crc_compute(payload)
    body = bytearray(payload)
    body.append(crc & 0xFF)
    body.append((crc >> 8) & 0xFF)
    return bytes([_FLAG]) + bytes(_stuff(body)) + bytes([_FLAG])


def encode_heartbeat(gps_valid=True, utc_ok=True, timestamp=None):
    """Build a GDL90 Heartbeat (0x00) frame — the once-per-second 'I'm alive'
    beacon a real ADS-B source emits regardless of traffic.  Sources that send
    this let the receiver distinguish 'link up, no aircraft in range' from
    'link down'.  Inverse of _decode_heartbeat."""
    if timestamp is None:
        t = time.gmtime()
        timestamp = t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec
    ts = timestamp & 0x1FFFF
    st1 = (0x80 if gps_valid else 0) | 0x01        # GPS valid + UAT initialised
    st2 = (0x01 if utc_ok else 0) | (((ts >> 16) & 1) << 7)
    body = bytes([MSG_HEARTBEAT, st1, st2, ts & 0xFF, (ts >> 8) & 0xFF,
                  0x00, 0x00])
    return frame_message(body)


def encode_traffic(address, lat, lon, alt_ft, gs_kt=0, track_deg=0.0,
                   vvel_fpm=0, callsign="", emitter=1, alert=False,
                   msg_id=MSG_TRAFFIC):
    """Build a Traffic (or Ownship) report frame.  Inverse of
    _decode_report — used by tests to produce known-good frames."""
    b = bytearray(28)
    b[0] = msg_id
    b[1] = ((1 if alert else 0) << 4) | 0x00
    b[2] = (address >> 16) & 0xFF
    b[3] = (address >> 8) & 0xFF
    b[4] = address & 0xFF

    def _deg_to_semi(d):
        v = int(round(d / (180.0 / 0x800000)))
        return v & 0xFFFFFF

    la = _deg_to_semi(lat)
    lo = _deg_to_semi(lon)
    b[5], b[6], b[7] = (la >> 16) & 0xFF, (la >> 8) & 0xFF, la & 0xFF
    b[8], b[9], b[10] = (lo >> 16) & 0xFF, (lo >> 8) & 0xFF, lo & 0xFF

    if alt_ft is None:
        alt_raw = 0xFFF
    else:
        alt_raw = max(0, min(0xFFE, int(round((alt_ft + 1000) / 25))))
    b[11] = (alt_raw >> 4) & 0xFF
    misc = 0x09                              # airborne + true-track
    b[12] = ((alt_raw & 0x0F) << 4) | misc
    b[13] = (11 << 4) | 10                    # NIC / NACp placeholders

    h = max(0, min(0xFFE, int(round(gs_kt))))
    v = int(round((vvel_fpm or 0) / 64))
    v &= 0xFFF
    b[14] = (h >> 4) & 0xFF
    b[15] = ((h & 0x0F) << 4) | ((v >> 8) & 0x0F)
    b[16] = v & 0xFF
    b[17] = int(round(track_deg / (360.0 / 256.0))) & 0xFF
    b[18] = emitter & 0xFF
    cs = (callsign + " " * 8)[:8].encode("ascii", "ignore")
    b[19:27] = cs
    b[27] = 0x00
    return frame_message(b)


def encode_uplink(payload, tor=0):
    """Build an Uplink Data (0x07) frame: msg id + 3-byte Time-of-Reception +
    the UAT uplink ``payload`` (typically 432 bytes from dump978).  Inverse of
    the decode path that surfaces ``kind="uplink"`` — used by the dump978→GDL90
    bridge and by tests.  ``tor`` is the 24-bit reception timestamp (we don't
    interpret it on decode, so any value round-trips)."""
    body = bytes([MSG_UPLINK,
                  tor & 0xFF, (tor >> 8) & 0xFF, (tor >> 16) & 0xFF]) \
        + bytes(payload)
    return frame_message(body)


def encode_foreflight_ahrs(roll_deg=None, pitch_deg=None, heading_deg=None,
                           heading_true=True, ias_kt=None, tas_kt=None):
    """Build a ForeFlight AHRS (0x65 / sub-id 1) frame.  Inverse of
    _decode_foreflight — used by tests and the sim to stand in for a Sentry.
    None on any field encodes the spec's invalid sentinel."""
    def _put(val, sentinel):
        v = sentinel if val is None else int(round(val)) & 0xFFFF
        return bytes([(v >> 8) & 0xFF, v & 0xFF])

    b = bytearray([MSG_FOREFLIGHT, FF_SUBID_AHRS])
    b += _put(None if roll_deg is None else roll_deg * 10, 0x7FFF)
    b += _put(None if pitch_deg is None else pitch_deg * 10, 0x7FFF)
    if heading_deg is None:
        b += bytes([0xFF, 0xFF])
    else:
        h = int(round(heading_deg * 10)) & 0x7FFF
        if not heading_true:
            h |= 0x8000
        b += bytes([(h >> 8) & 0xFF, h & 0xFF])
    b += _put(ias_kt, 0xFFFF)
    b += _put(tas_kt, 0xFFFF)
    return frame_message(bytes(b))
