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
import math
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
        minutes = ((frame[2] & 0x1f) << 1) | (frame[3] >> 7)   # 6-bit minutes
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


# ── Text-product classification ─────────────────────────────────────────────────
# FIS-B text products (METAR/TAF/PIREP/AIRMET/SIGMET/NOTAM) all arrive as DLAC
# records; the product is told apart by the report text itself.  One classifier
# routes each record so the store and the WX screens can file it by type.
_RE_TAF_VALID = re.compile(r"\b\d{4}/\d{4}\b")      # TAF validity period DDHH/DDHH


def classify_text(rec):
    """Classify one decoded text record into a FIS-B product type:
    METAR / TAF / AIRMET / SIGMET / NOTAM / PIREP / OTHER (None if blank)."""
    if not rec:
        return None
    s = rec.strip().upper()
    if not s:
        return None
    first = s.split(None, 1)[0]
    if first == "TAF" or first.startswith("TAF"):
        return "TAF"
    if first in ("METAR", "SPECI"):
        return "METAR"
    if first in ("WINDS", "FD"):
        return "WINDS"
    if "AIRMET" in s:
        return "AIRMET"
    if "SIGMET" in s:
        return "SIGMET"
    if s.startswith("!") or "NOTAM" in s:
        return "NOTAM"
    if first in ("UA", "UUA") or "/OV " in s:
        return "PIREP"
    # Bare report (no keyword): a DDHH/DDHH validity window means TAF — check it
    # first, since TAFs also carry a DDHHMMZ issue time that would otherwise look
    # like a METAR obs time.  Otherwise an ICAO + DDHHMMZ is a METAR.
    if _RE_STATION.match(s):
        if _RE_TAF_VALID.search(s):
            return "TAF"
        if _RE_DDHHMM.search(s):
            return "METAR"
    return "OTHER"


def taf_ident(rec):
    """Station ICAO a TAF is for, or None.  Skips a leading TAF/AMD/COR."""
    if not rec:
        return None
    s = re.sub(r"^\s*TAF\s+(AMD\s+|COR\s+)?", "", rec.strip().upper())
    m = _RE_STATION.match(s)
    return m.group(1) if m else None


_RE_VALID_RANGE = re.compile(r"VALID\s+(\d{6})/(\d{6})")
_RE_VALID_UNTIL = re.compile(r"VALID\s+UNTIL\s+(\d{6})")


def advisory_valid(text):
    """Short valid-time string from an AIRMET/SIGMET bulletin (the DDHHMM
    'VALID UNTIL …' or 'VALID …/…' field) — e.g. 'until 21:00Z' / '16:55Z–
    20:55Z', or None.  Same idea as the TAF validity header."""
    if not text:
        return None
    s = text.upper()
    m = _RE_VALID_RANGE.search(s)
    if m:
        a, b = m.group(1), m.group(2)
        return f"{a[2:4]}:{a[4:6]}Z–{b[2:4]}:{b[4:6]}Z"
    m = _RE_VALID_UNTIL.search(s)
    if m:
        u = m.group(1)
        return f"until {u[2:4]}:{u[4:6]}Z"
    return None


# ── TAF decoding (raw forecast → readable per-period lines) ─────────────────────
_RE_VALIDITY = re.compile(r"^\d{4}/\d{4}$")
_RE_FM       = re.compile(r"^FM(\d{2})(\d{2})(\d{2})$")     # FMDDHHMM
_RE_ISSUED   = re.compile(r"^\d{6}Z$")
_RE_GROUP    = re.compile(r"^(FM\d{6}|BECMG|TEMPO|PROB\d{2})$")
# Present-weather tokens (optional intensity/proximity + 1-3 two-letter codes).
_RE_WXTOK    = re.compile(
    r"(?<![A-Z0-9])([-+]|VC)?((?:MI|PR|BC|DR|BL|SH|TS|FZ|DZ|RA|SN|SG|IC|PL|GR|"
    r"GS|UP|BR|FG|FU|VA|DU|SA|HZ|PY|PO|SQ|FC|SS|DS){1,3})\b")


def _hhz(ddhh):
    return f"{ddhh[2:4]}Z" if ddhh and len(ddhh) >= 4 else "?"


def _taf_wind(text):
    m = _RE_WIND.search(text)
    if not m:
        return None
    d, s, g = m.group(1), int(m.group(2)), m.group(3)
    if d != "VRB" and s == 0:
        return "calm"
    dd = "VRB" if d == "VRB" else f"{int(d):03d}°"
    return dd + f" {s} kt" + (f" G{int(g)}" if g else "")


def _taf_vis(text):
    if re.search(r"\bP6SM\b", text):
        return "6+ sm"
    m = _RE_VIS_SM.search(text)
    if not m:
        return None
    if m.group(5) is not None:                 # bare fraction "1/2SM"
        return f"{m.group(5)}/{m.group(6)} sm"
    whole = m.group(2)
    if m.group(3) is not None:                  # "1 1/2SM"
        return f"{whole} {m.group(3)}/{m.group(4)} sm"
    return f"{'<' if m.group(1) else ''}{whole} sm"


def _taf_sky(text):
    layers = []
    if re.search(r"\b(SKC|CLR|NSC|NCD)\b", text):
        layers.append("clear")
    for cover, base in _RE_CLOUD.findall(text):
        layers.append(f"{cover} {int(base) * 100:,}")
    return " / ".join(layers) if layers else None


def _taf_ceiling(text):
    """Lowest BKN/OVC/VV base in ft, or None."""
    bases = [int(b) * 100 for c, b in _RE_CLOUD.findall(text)
             if c in ("BKN", "OVC", "VV")]
    return min(bases) if bases else None


# Common present-weather tokens → plain words; uncommon ones fall back to the
# raw code (lowercased) so nothing is silently dropped.
_WX_WORDS = {
    "BR": "mist", "FG": "fog", "HZ": "haze", "FU": "smoke", "DU": "dust",
    "SA": "sand", "VA": "volcanic ash", "SQ": "squalls", "FC": "funnel cloud",
    "RA": "rain", "-RA": "light rain", "+RA": "heavy rain",
    "SHRA": "rain showers", "-SHRA": "light rain showers",
    "+SHRA": "heavy rain showers",
    "TSRA": "thunderstorm w/ rain", "-TSRA": "thunderstorm w/ rain",
    "TS": "thunderstorm", "VCTS": "thunderstorms in vicinity",
    "VCSH": "showers in vicinity",
    "SN": "snow", "-SN": "light snow", "+SN": "heavy snow",
    "SHSN": "snow showers", "-SHSN": "light snow showers",
    "DZ": "drizzle", "-DZ": "light drizzle", "FZRA": "freezing rain",
    "FZDZ": "freezing drizzle", "FZFG": "freezing fog",
    "GR": "hail", "GS": "small hail", "PL": "ice pellets", "IC": "ice crystals",
    "BLSN": "blowing snow", "BLDU": "blowing dust", "BLSA": "blowing sand",
}


def decode_wx(token):
    """Decode one present-weather token to words (e.g. '-SHRA' -> 'light rain
    showers'); unknown tokens return the raw code lowercased."""
    if token in _WX_WORDS:
        return _WX_WORDS[token]
    bare = token.lstrip("+-")
    return _WX_WORDS.get(bare, token.lower())


def _taf_wx(text):
    toks = ["".join(t) for t in _RE_WXTOK.findall(text)]
    words = [decode_wx(t) for t in toks if t]
    return ", ".join(words) if words else None


def _taf_summary(text):
    """Compact one-line summary of a TAF group (wind, vis, wx, sky)."""
    parts = [p for p in (_taf_wind(text), _taf_vis(text), _taf_wx(text),
                         _taf_sky(text)) if p]
    return ", ".join(parts) if parts else "—"


def parse_taf(raw):
    """Parse a raw TAF into ``{icao, issued, valid_from, valid_to, periods}``,
    where each period is ``{kind, label, summary, raw}`` (kind = INITIAL / FM /
    BECMG / TEMPO / PROB).  Returns None if it doesn't look like a TAF."""
    if not raw:
        return None
    s = re.sub(r"^\s*TAF\s+(AMD\s+|COR\s+)?", "", raw.strip().upper())
    toks = s.split()
    if len(toks) < 3 or not re.match(r"^[A-Z][A-Z0-9]{3}$", toks[0]):
        return None
    icao = toks[0]
    issued = next((t for t in toks[1:4] if _RE_ISSUED.match(t)), None)
    vi = next((i for i, t in enumerate(toks[1:5], 1) if _RE_VALIDITY.match(t)),
              None)
    if vi is None:
        return None
    vfrom, vto = toks[vi].split("/")
    body = toks[vi + 1:]

    periods = []
    cur = {"kind": "INITIAL", "from": vfrom, "to": vto, "prob": None, "toks": []}
    i = 0
    while i < len(body):
        t = body[i]
        if _RE_FM.match(t):
            periods.append(cur)
            cur = {"kind": "FM", "from": t[2:8], "to": None, "prob": None,
                   "toks": []}
            i += 1
            continue
        if t in ("BECMG", "TEMPO") or re.match(r"^PROB\d{2}$", t):
            periods.append(cur)
            kind = t if t in ("BECMG", "TEMPO") else "PROB"
            prob = t[4:] if t.startswith("PROB") else None
            j = i + 1
            # PROB may be followed by TEMPO, then the DDHH/DDHH window.
            if kind == "PROB" and j < len(body) and body[j] == "TEMPO":
                j += 1
            frm = to = None
            if j < len(body) and _RE_VALIDITY.match(body[j]):
                frm, to = body[j].split("/")
                j += 1
            cur = {"kind": kind, "from": frm, "to": to, "prob": prob,
                   "toks": []}
            i = j
            continue
        cur["toks"].append(t)
        i += 1
    periods.append(cur)

    out = []
    for g in periods:
        text = " ".join(g["toks"])
        if g["kind"] == "INITIAL":
            label = f"{_hhz(g['from'])}–{_hhz(g['to'])}"
        elif g["kind"] == "FM":
            f = g["from"]
            label = f"From {f[2:4]}:{f[4:6]}Z"
        elif g["kind"] == "BECMG":
            label = f"Becoming by {_hhz(g['to'])}"
        elif g["kind"] == "TEMPO":
            label = f"Temp {_hhz(g['from'])}–{_hhz(g['to'])}"
        else:  # PROB
            label = f"{g['prob']}% {_hhz(g['from'])}–{_hhz(g['to'])}"
        out.append({"kind": g["kind"], "label": label, "raw": text,
                    "wind": _taf_wind(text), "vis": _taf_vis(text),
                    "wx": _taf_wx(text), "sky": _taf_sky(text),
                    "ceiling_ft": _taf_ceiling(text),
                    "summary": _taf_summary(text)})
    return {"icao": icao, "issued": issued,
            "valid_from": vfrom, "valid_to": vto, "periods": out}


def taf_lines(raw):
    """Readable lines for a TAF: a validity header + one line per period.
    Returns [] if the text doesn't parse as a TAF."""
    p = parse_taf(raw)
    if not p:
        return []
    lines = [f"{p['icao']} valid {_hhz(p['valid_from'])}–{_hhz(p['valid_to'])}"]
    for g in p["periods"]:
        lines.append(f"{g['label']}:  {g['summary']}")
    return lines


# ── Winds & temps aloft (FD codes → per-altitude dir/speed/temp) ────────────────
# The FIS-B winds product is delivered as text; we decode the standard FD code
# (ddss[±]tt) per altitude.  The bulletin envelope here is "WINDS <id> <alt>
# <code> …" pairs (what the simulator emits); real-world FD bulletin column
# parsing is a refinement, but the per-code decode is the standard one.
# Standard winds-aloft forecast altitudes (FD levels) the UI offers — one
# source of truth shared by the radio FD decode, the internet (Open-Meteo)
# interpolation, and the altitude selector.
WINDS_ALTS = (3000, 6000, 9000, 12000, 18000, 24000, 30000, 34000, 39000)


def decode_fd_code(code, alt_ft):
    """Decode one FD winds code at ``alt_ft`` → {alt_ft, dir, spd, temp, lv}.
    'ddss' = dir (tens of deg) + speed kt; dd>36 means dir-=50, speed+=100;
    '9900' = light & variable; trailing ±tt (or implied-negative tt at high
    levels) is the temperature in °C."""
    code = (code or "").strip().upper()
    if len(code) < 4 or not code[:4].isdigit():
        return None
    if code[:4] == "9900":
        lvl = {"alt_ft": alt_ft, "dir": None, "spd": None, "temp": None,
               "lv": True}
    else:
        dd, ss = int(code[:2]), int(code[2:4])
        spd = ss
        if dd > 36:
            dd -= 50
            spd += 100
        direction = dd * 10 or 360
        lvl = {"alt_ft": alt_ft, "dir": direction, "spd": spd, "temp": None,
               "lv": False}
    rest = code[4:].strip()
    if rest:
        if rest[0] in "+-":
            try:
                lvl["temp"] = int(rest)
            except ValueError:
                pass
        elif rest.lstrip("-").isdigit():
            lvl["temp"] = -int(rest)        # high levels: sign omitted, negative
    return lvl


def parse_winds_aloft(text):
    """Parse a winds-aloft record ('WINDS <id> <alt> <code> <alt> <code> …')
    into ``{station, levels:[{alt_ft, dir, spd, temp, lv}, …]}`` or None."""
    if not text:
        return None
    toks = text.split()
    if len(toks) < 4 or toks[0].upper() not in ("WINDS", "FD"):
        return None
    station = toks[1].upper()
    levels = []
    i = 2
    while i + 1 < len(toks):
        try:
            alt = int(toks[i])
        except ValueError:
            break
        lvl = decode_fd_code(toks[i + 1], alt)
        if lvl:
            levels.append(lvl)
        i += 2
    return {"station": station, "levels": levels} if levels else None


# ── FIS-B graphics (G-AIRMET / SIGMET hazard areas → polygons) ──────────────────
# Graphical products carry the hazard *geometry* (a separate product from the
# text bulletins).  We decode the geometric overlay's vertex list so the MET
# overlay can shade the area.  NOTE: this follows the DO-358 geometric-overlay
# vertex encoding (24-bit lat/lon, LSB 180/2^23) but is a simplified record
# layout — it wants validation against real 978 graphical frames; the store /
# render / tap pipeline is exercised by the simulator's matching encoder.
GRAPHICS_PRODUCTS = {14, 15}          # G-AIRMET, CWA (graphical hazard areas)
_GFX_LSB = 180.0 / (1 << 23)
_GFX_HAZARD = {0: "Turbulence", 1: "Icing", 2: "IFR", 3: "Convective",
               4: "Mtn Obscuration", 5: "Ash", 15: "Advisory"}
_GFX_HAZARD_REV = {v: k for k, v in _GFX_HAZARD.items()}


def _gfx_dec_coord(b0, b1, b2):
    v = (b0 << 16) | (b1 << 8) | b2
    if v & 0x800000:
        v -= 0x1000000
    return round(v * _GFX_LSB, 5)


def _gfx_enc_coord(deg):
    v = int(round(deg / _GFX_LSB)) & 0xFFFFFF
    return bytes([(v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF])


def decode_graphic_records(data):
    """Decode hazard-area geometry records from a graphical product's APDU data.

    Record: hazard(1) geom(1) nverts(1), then nverts × (lat[3], lon[3]) as
    24-bit signed (LSB 180/2^23), then a 2-byte DLAC text length + that many
    DLAC bytes (the bulletin paired with this area; 0 = none).  Returns
    ``[{hazard, geom, vertices:[(lat,lon)], text}]``."""
    out = []
    i, n = 0, len(data)
    while i + 3 <= n:
        hazard, geom, nv = data[i], data[i + 1], data[i + 2]
        i += 3
        if nv == 0 or i + nv * 6 > n:
            break
        verts = []
        for _k in range(nv):
            verts.append((_gfx_dec_coord(data[i], data[i + 1], data[i + 2]),
                          _gfx_dec_coord(data[i + 3], data[i + 4], data[i + 5])))
            i += 6
        if i + 2 > n:
            break
        tlen = (data[i] << 8) | data[i + 1]
        i += 2
        text = None
        if tlen:
            if i + tlen > n:
                break
            text = dlac_decode(data[i:i + tlen]).split("\x03")[0].strip() or None
            i += tlen
        out.append({"hazard": _GFX_HAZARD.get(hazard, "Advisory"),
                    "geom": "polygon" if geom == 0 else "point",
                    "vertices": verts, "text": text})
    return out


# ── FIS-B NEXRAD (run-length block raster → intensity cells) ────────────────────
# DO-358 / cyoung-stratux extract_nexrad.c.  North America is a grid of blocks;
# each block is 32 (lon) × 4 (lat) bins, filled west→east then north→south, run-
# length coded (intensity = byte & 7, run = (byte>>3)+1).  Block header (3 bytes):
# bit7 RLE-vs-empty, bit6 N/S, bits5-4 scale factor, bits3-0|byte1|byte2 = block#.
NEXRAD_PRODUCTS = {63, 64}              # 63 regional, 64 CONUS
_NX_BLOCK_WIDTH = 48.0 / 60.0          # 0.8° lon (< 60°N)
_NX_WIDE_WIDTH = 96.0 / 60.0           # 1.6° lon (>= 60°N)
_NX_BLOCK_HEIGHT = 4.0 / 60.0          # 0.0667° lat
_NX_BLOCKS_PER_RING = 450
_NX_THRESHOLD = 405000
_NX_BINS_LON = 32
_NX_BINS_LAT = 4


def _nx_block_geo(bn, sf, ns):
    """North-edge lat, west-edge lon, lat span, lon span (deg) for block bn."""
    scale = {1: 5.0, 2: 9.0}.get(sf, 1.0)
    b = (bn & ~1) if bn >= _NX_THRESHOLD else bn
    raw_lat = _NX_BLOCK_HEIGHT * (b // _NX_BLOCKS_PER_RING)
    raw_lon = (b % _NX_BLOCKS_PER_RING) * _NX_BLOCK_WIDTH
    lon_span = (_NX_WIDE_WIDTH if b >= _NX_THRESHOLD else _NX_BLOCK_WIDTH) * scale
    lat_span = _NX_BLOCK_HEIGHT * scale
    lon_w = raw_lon - 360.0 if raw_lon > 180.0 else raw_lon
    lat_n = (-raw_lat) if ns else (raw_lat + _NX_BLOCK_HEIGHT)
    return lat_n, lon_w, lat_span, lon_span


def decode_nexrad_block(data):
    """Decode one NEXRAD block record → ``{block_num, cells, empty}`` where each
    cell is ``{lat, lon, dlat, dlon, i}`` (NW corner + size, intensity 1-7;
    intensity 0 / empty blocks contribute no cells).  Same-intensity bins in a
    row are merged into one rectangle."""
    if len(data) < 3:
        return None
    rle = (data[0] & 0x80) != 0
    ns = (data[0] & 0x40) != 0
    sf = (data[0] & 0x30) >> 4
    bn = ((data[0] & 0x0f) << 16) | (data[1] << 8) | data[2]
    lat_n, lon_w, lat_span, lon_span = _nx_block_geo(bn, sf, ns)
    bin_lat = lat_span / _NX_BINS_LAT
    bin_lon = lon_span / _NX_BINS_LON
    cells = []
    if rle:
        bins = []
        for byte in data[3:]:
            bins.extend([byte & 7] * ((byte >> 3) + 1))
            if len(bins) >= 128:
                break
        bins = bins[:128]
        idx, n = 0, len(bins)
        while idx < n:
            inten = bins[idx]
            row, col = divmod(idx, _NX_BINS_LON)
            j = idx + 1
            while j < n and j // _NX_BINS_LON == row and bins[j] == inten:
                j += 1
            if inten > 0:
                cells.append({"lat": lat_n - row * bin_lat,
                              "lon": lon_w + col * bin_lon,
                              "dlat": bin_lat, "dlon": bin_lon * (j - idx),
                              "i": inten})
            idx = j
    return {"block_num": bn, "cells": cells, "empty": not rle}


def encode_nexrad_block(block_num, intensities, sf=0, ns=False):
    """Build a NEXRAD block record from 128 bin intensities (0-7).  Inverse of
    decode_nexrad_block (RLE)."""
    h0 = (0x80 | (0x40 if ns else 0) | ((sf & 0x3) << 4)
          | ((block_num >> 16) & 0x0f))
    out = bytearray([h0, (block_num >> 8) & 0xff, block_num & 0xff])
    i, n = 0, len(intensities)
    while i < n:
        val = intensities[i] & 7
        run = 1
        while i + run < n and (intensities[i + run] & 7) == val and run < 32:
            run += 1
        out.append(((run - 1) << 3) | val)
        i += run
    return bytes(out)


def _encode_apdu_header(product_id, valid_hm=None):
    """APDU header bytes — the inverse of ``decode_apdu``'s header read.

    With no time, a 2-byte ``t_opt=0`` header.  With ``valid_hm=(hours,
    minutes)``, a 4-byte ``t_opt=1`` header carrying the product valid time in
    the same hours(5)+minutes(5) layout ``decode_apdu`` reads (minutes coarse to
    5 bits — matching the simplified decode flagged for real-frame validation)."""
    if valid_hm is None:
        return bytes([(product_id >> 6) & 0x1f,
                      (product_id & 0x3f) << 2])
    hours, minutes = valid_hm
    hours &= 0x1f
    minutes &= 0x3f
    return bytes([
        (1 << 5) | ((product_id >> 6) & 0x1f),               # S=0, t_opt=1
        ((product_id & 0x3f) << 2) | ((hours >> 3) & 0x03),
        ((hours & 0x07) << 5) | ((minutes >> 1) & 0x1f),
        (minutes & 0x01) << 7,                               # 6th minute bit (offset=4)
    ])


def encode_nexrad_uplink(block_num, intensities, sf=0, station=None,
                         product_id=63, total=432, valid_hm=None):
    """One NEXRAD block as a UAT uplink payload (for the simulator / tests).

    ``valid_hm=(hours, minutes)`` stamps the APDU with a product valid time so
    the receipt-vs-valid age path can be exercised end to end."""
    block = encode_nexrad_block(block_num, intensities, sf)
    apdu = _encode_apdu_header(product_id, valid_hm) + block
    return _pack_uplink(product_id, apdu, station, total)


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


def valid_age_min(valid_min, now=None):
    """Age in minutes of a product whose valid time is ``valid_min`` (UTC
    minute-of-day, 0-1439), given wall-clock ``now`` (epoch s).  Handles the
    midnight wrap (a 23:5x product seen just after 00:00 is minutes old, not
    nearly a day).  Returns None if ``valid_min`` is None."""
    if valid_min is None:
        return None
    now = now if now is not None else time.time()
    tm = time.gmtime(now)
    now_min = tm.tm_hour * 60 + tm.tm_min + tm.tm_sec / 60.0
    age = now_min - valid_min
    if age < -1.0:                  # product time "ahead" -> it's from before midnight
        age += 1440.0
    return max(0.0, age)


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

    ADVISORY_KINDS = ("AIRMET", "SIGMET", "NOTAM")

    def __init__(self, expire_s=4500.0, station_expire_s=120.0,
                 taf_expire_s=10800.0, advisory_expire_s=14400.0,
                 graphics_expire_s=14400.0, winds_expire_s=21600.0,
                 nexrad_expire_s=900.0):
        self.expire_s = expire_s             # 75 min: METARs refresh hourly
        self.station_expire_s = station_expire_s   # towers rebroadcast often
        self.taf_expire_s = taf_expire_s     # 3 h: TAFs reissue ~6 h, valid ~24-30 h
        self.advisory_expire_s = advisory_expire_s  # 4 h: AIRMET/SIGMET/NOTAM text
        self.graphics_expire_s = graphics_expire_s  # 4 h: graphical hazard areas
        self.winds_expire_s = winds_expire_s        # 6 h: winds aloft forecast
        self.nexrad_expire_s = nexrad_expire_s      # 15 min: NEXRAD blocks
        self.uplink_count = 0                # uplink frames ingested
        self.metar_count = 0                 # METAR records parsed OK (cumulative)
        self.taf_count = 0                   # TAF records stored (cumulative)
        self.advisory_count = 0              # AIRMET/SIGMET/NOTAM records stored
        self.graphic_count = 0               # graphical hazard records stored
        self.winds_count = 0                 # winds-aloft records stored
        self.nexrad_count = 0                # NEXRAD blocks stored
        self._lock = threading.Lock()
        self._metars = {}                    # icao -> (parsed_dict, recv_monotonic)
        self._tafs = {}                      # icao -> (raw_text, recv_monotonic, source)
        self._stations = {}                  # (lat,lon) -> {.., last_mono, count}
        self._advisories = {k: {} for k in self.ADVISORY_KINDS}  # kind -> {text: recv}
        self._graphics = {}                  # key -> (graphic_dict, recv_monotonic)
        self._winds = {}                     # station -> (winds_dict, recv_monotonic)
        self._nexrad = {}                    # block_num -> (cells, recv_monotonic, valid_min)

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
        tafs = {}
        advisories = {k: set() for k in self.ADVISORY_KINDS}
        graphics = []
        winds = {}
        nexrad = {}
        for apdu in decode_uplink(uplink_payload):
            if not apdu.get("data"):
                continue
            if apdu.get("product_id") in NEXRAD_PRODUCTS:
                blk = decode_nexrad_block(apdu["data"])
                if blk is not None:
                    # Capture the product *valid* time from the APDU header (when
                    # present) — for NEXRAD this is the mosaic generation time,
                    # which can lag receipt by minutes.  Surfacing it is what
                    # keeps stale radar from looking current.
                    vmin = None
                    if apdu.get("hours") is not None and apdu.get("minutes") is not None:
                        vmin = apdu["hours"] * 60 + apdu["minutes"]
                    nexrad[blk["block_num"]] = (blk["cells"], vmin)
                continue
            if apdu.get("product_id") in GRAPHICS_PRODUCTS:
                graphics.extend(decode_graphic_records(apdu["data"]))
                continue
            for rec in text_records(apdu["data"]):
                kind = classify_text(rec)
                if kind == "METAR":
                    parsed = parse_metar_text(rec)
                    if parsed is not None:
                        metars[parsed["icao"]] = parsed
                elif kind == "TAF":
                    ident = taf_ident(rec)
                    if ident:
                        tafs[ident] = rec.strip()
                elif kind == "WINDS":
                    w = parse_winds_aloft(rec)
                    if w:
                        winds[w["station"]] = w
                elif kind in self.ADVISORY_KINDS:
                    advisories[kind].add(rec.strip())

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
            for icao, raw in tafs.items():
                self._tafs[icao] = (raw, now_mono, "RDR")
                self.taf_count += 1
            for kind, texts in advisories.items():
                for text in texts:
                    self._advisories[kind][text] = now_mono
                    self.advisory_count += 1
            for g in graphics:
                key = (g["hazard"], g["geom"], tuple(g["vertices"]))
                self._graphics[key] = (g, now_mono)
                self.graphic_count += 1
            for station, w in winds.items():
                w.setdefault("src", "RDR")
                self._winds[station] = (w, now_mono)
                self.winds_count += 1
            for bnum, (cells, vmin) in nexrad.items():
                self._nexrad[bnum] = (cells, now_mono, vmin)
                self.nexrad_count += 1

    # ── Internet backfill ──────────────────────────────────────────────────────
    # The AWC pollers feed parsed products in through these.  Radio-primary /
    # internet-bonus mirrors the METAR merge: a radio TAF is never overwritten by
    # an internet one (radio is local and at least as fresh); advisories and
    # graphics dedupe by content, so internet simply backfills areas the radio
    # hasn't delivered.  All keep the same store shapes the readers already use,
    # so taf_for / advisories / graphics surface internet data with no read-path
    # change.
    def add_taf(self, icao, raw, source="INET", now_mono=None):
        """Store one internet/other-source TAF.  A fresh radio ("RDR") TAF wins —
        an INET update won't clobber it — but INET refreshes its own entries and
        backfills stations radio hasn't sent."""
        if not icao or not raw:
            return
        now_mono = now_mono if now_mono is not None else time.monotonic()
        with self._lock:
            cur = self._tafs.get(icao)
            if (cur is not None and cur[2] == "RDR" and source != "RDR"
                    and now_mono - cur[1] <= self.taf_expire_s):
                return
            self._tafs[icao] = (raw.strip(), now_mono, source)
            self.taf_count += 1

    def add_tafs(self, tafs, source="INET", now_mono=None):
        """Bulk add_taf from a list of ``{icao, raw}`` dicts."""
        now_mono = now_mono if now_mono is not None else time.monotonic()
        for t in tafs or []:
            self.add_taf(t.get("icao"), t.get("raw"), source, now_mono)

    def add_advisory(self, kind, text, now_mono=None):
        """Store one advisory bulletin (AIRMET/SIGMET/NOTAM).  Dedupes by text,
        so a radio and internet copy of the same bulletin collapse to one."""
        if kind not in self.ADVISORY_KINDS or not text:
            return
        now_mono = now_mono if now_mono is not None else time.monotonic()
        with self._lock:
            self._advisories[kind][text.strip()] = now_mono
            self.advisory_count += 1

    def add_graphic(self, g, now_mono=None):
        """Store one graphical hazard area (``{hazard, geom, vertices, text}``).
        Keyed by hazard+geometry+text so identical areas dedupe across sources."""
        if not g or not g.get("vertices"):
            return
        now_mono = now_mono if now_mono is not None else time.monotonic()
        verts = tuple((round(la, 4), round(lo, 4)) for la, lo in g["vertices"])
        key = (g.get("hazard"), g.get("geom", "polygon"), verts,
               (g.get("text") or "").strip())
        gg = {"hazard": g.get("hazard"), "geom": g.get("geom", "polygon"),
              "vertices": list(g["vertices"]), "text": g.get("text")}
        with self._lock:
            self._graphics[key] = (gg, now_mono)
            self.graphic_count += 1

    def add_airsigmets(self, items, now_mono=None):
        """Fold an AWC airsigmet snapshot (``{kind, hazard, raw, vertices,
        valid_from, valid_to}``) into both the text advisories and, when it
        carries an area, the graphics overlay — the one feed populates the
        picker bulletins and the MET-page shapes together."""
        now_mono = now_mono if now_mono is not None else time.monotonic()
        for it in items or []:
            kind, raw = it.get("kind"), it.get("raw")
            if raw:
                self.add_advisory(kind, raw, now_mono)
            verts = it.get("vertices") or []
            if len(verts) >= 3:
                self.add_graphic({"hazard": it.get("hazard", "Advisory"),
                                  "geom": "polygon", "vertices": verts,
                                  "text": raw}, now_mono)

    def add_winds(self, w, source="INET", now_mono=None):
        """Store one winds-aloft column.  Internet (Open-Meteo grid) entries
        carry their own ``lat``/``lon`` (the readers geolocate by those rather
        than the airport DB).  Radio FD stations and internet grid points use
        different ``station`` keys, so they coexist; ``src`` is tagged for the
        readout."""
        if not w or not w.get("station") or not w.get("levels"):
            return
        now_mono = now_mono if now_mono is not None else time.monotonic()
        ww = dict(w)
        ww.setdefault("src", source)
        with self._lock:
            self._winds[ww["station"]] = (ww, now_mono)
            self.winds_count += 1

    def add_winds_list(self, winds, source="INET", now_mono=None):
        now_mono = now_mono if now_mono is not None else time.monotonic()
        for w in winds or []:
            self.add_winds(w, source, now_mono)

    def set_winds(self, winds, source="INET", now_mono=None):
        """Replace *all* winds columns of ``source`` with this snapshot.  The
        internet grid is a complete picture of the current view, so replacing it
        each poll (rather than accumulating) stops barbs piling up as the view
        drifts — radio (``RDR``) columns are untouched."""
        now_mono = now_mono if now_mono is not None else time.monotonic()
        with self._lock:
            for k in [s for s, (w, _r) in self._winds.items()
                      if (w.get("src") or "RDR") == source]:
                del self._winds[k]
        self.add_winds_list(winds, source, now_mono)

    def add_notams(self, texts, now_mono=None):
        """Fold internet NOTAM bulletins (already-formatted text) into the
        advisory store, deduped by text like the other advisories."""
        now_mono = now_mono if now_mono is not None else time.monotonic()
        for t in texts or []:
            self.add_advisory("NOTAM", t, now_mono)

    def graphics(self, now_mono=None):
        """Active graphical hazard areas (``{hazard, geom, vertices}``), pruned
        past ``graphics_expire_s``."""
        now_mono = now_mono if now_mono is not None else time.monotonic()
        with self._lock:
            stale = [k for k, (_g, recv) in self._graphics.items()
                     if now_mono - recv > self.graphics_expire_s]
            for k in stale:
                del self._graphics[k]
            return [g for (g, _r) in self._graphics.values()]

    def nexrad_cells(self, now_mono=None):
        """All NEXRAD intensity cells from fresh blocks (flattened), pruned past
        ``nexrad_expire_s``.  Each cell: ``{lat, lon, dlat, dlon, i}``."""
        now_mono = now_mono if now_mono is not None else time.monotonic()
        with self._lock:
            stale = [k for k, (_c, recv, _v) in self._nexrad.items()
                     if now_mono - recv > self.nexrad_expire_s]
            for k in stale:
                del self._nexrad[k]
            out = []
            for cells, _r, _v in self._nexrad.values():
                out.extend(cells)
        return out

    def nexrad_status(self, now=None, now_mono=None):
        """Provenance of the current FIS-B radar picture, or ``None`` when no
        fresh blocks: ``{n_blocks, recv_age_s, valid_age_min}``.

        ``valid_age_min`` is the *oldest* (worst-case) product valid time across
        the contributing blocks — the conservative number to badge, since a
        FIS-B NEXRAD mosaic's valid time can lag its broadcast/receipt by
        minutes.  ``None`` when no block carried a timestamp.  Caveat: the APDU
        time decode is the approximate layout flagged for real-frame validation,
        so treat the figure as indicative until pinned to live frames."""
        now = now if now is not None else time.time()
        now_mono = now_mono if now_mono is not None else time.monotonic()
        with self._lock:
            stale = [k for k, (_c, recv, _v) in self._nexrad.items()
                     if now_mono - recv > self.nexrad_expire_s]
            for k in stale:
                del self._nexrad[k]
            if not self._nexrad:
                return None
            items = list(self._nexrad.values())
        newest_recv = max(recv for _c, recv, _v in items)
        recv_age_s = max(0.0, now_mono - newest_recv)
        ages = [valid_age_min(v, now) for _c, _r, v in items if v is not None]
        valid_age = max(ages) if ages else None
        return {"n_blocks": len(items), "recv_age_s": recv_age_s,
                "valid_age_min": valid_age}

    def advisories(self, kind=None, now_mono=None):
        """Active advisory bulletin texts (AIRMET/SIGMET/NOTAM).  ``kind`` filters
        to one type; otherwise all.  Prunes entries older than
        ``advisory_expire_s``."""
        now_mono = now_mono if now_mono is not None else time.monotonic()
        kinds = [kind] if kind else list(self.ADVISORY_KINDS)
        out = []
        with self._lock:
            for k in kinds:
                d = self._advisories.get(k, {})
                stale = [t for t, recv in d.items()
                         if now_mono - recv > self.advisory_expire_s]
                for t in stale:
                    del d[t]
                out.extend(sorted(d.keys()))
        return out

    def taf_stations(self, now_mono=None):
        """ICAOs that currently have a fresh TAF (prunes stale ones)."""
        now_mono = now_mono if now_mono is not None else time.monotonic()
        with self._lock:
            stale = [i for i, (_r, recv, _s) in self._tafs.items()
                     if now_mono - recv > self.taf_expire_s]
            for i in stale:
                del self._tafs[i]
            return list(self._tafs.keys())

    def winds_for(self, station, now_mono=None):
        """Winds-aloft dict {station, levels} for a station id, or None."""
        if not station:
            return None
        now_mono = now_mono if now_mono is not None else time.monotonic()
        with self._lock:
            v = self._winds.get(station)
            if not v:
                return None
            w, recv = v
            if now_mono - recv > self.winds_expire_s:
                del self._winds[station]
                return None
            return w

    def winds_stations(self, now_mono=None):
        """Station ids that currently have fresh winds aloft."""
        now_mono = now_mono if now_mono is not None else time.monotonic()
        with self._lock:
            stale = [s for s, (_w, recv) in self._winds.items()
                     if now_mono - recv > self.winds_expire_s]
            for s in stale:
                del self._winds[s]
            return list(self._winds.keys())

    def taf_for(self, icao, now_mono=None):
        """Raw TAF text for ``icao`` if heard within ``taf_expire_s``, else
        None.  Prunes the entry once stale."""
        if not icao:
            return None
        now_mono = now_mono if now_mono is not None else time.monotonic()
        with self._lock:
            v = self._tafs.get(icao)
            if not v:
                return None
            raw, recv, _src = v
            if now_mono - recv > self.taf_expire_s:
                del self._tafs[icao]
                return None
            return raw

    def taf_source(self, icao, now_mono=None):
        """Source tag ("RDR"/"INET") of the stored TAF for ``icao``, or None."""
        if not icao:
            return None
        now_mono = now_mono if now_mono is not None else time.monotonic()
        with self._lock:
            v = self._tafs.get(icao)
            if not v:
                return None
            _raw, recv, src = v
            if now_mono - recv > self.taf_expire_s:
                return None
            return src

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


# ── Geometry + advisory ranking (nearest-first, on-route flag) ──────────────────
# Pure helpers in equirectangular nm — accurate enough for ranking hazard areas
# by distance and flagging the ones near the active route.  No hiding: callers
# sort/label with these, they never drop an advisory.
def nm_between(a_lat, a_lon, b_lat, b_lon):
    dlat = (b_lat - a_lat) * 60.0
    dlon = (b_lon - a_lon) * 60.0 * math.cos(math.radians((a_lat + b_lat) / 2.0))
    return math.hypot(dlat, dlon)


def point_in_polygon(lat, lon, verts):
    inside = False
    n = len(verts)
    j = n - 1
    for i in range(n):
        yi, xi = verts[i]
        yj, xj = verts[j]
        if (yi > lat) != (yj > lat) and \
                lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def _seg_distance_nm(plat, plon, alat, alon, blat, blon):
    """Distance (nm) from point P to segment A–B, in a local nm frame at P."""
    cl = math.cos(math.radians(plat))
    ax, ay = (alon - plon) * 60.0 * cl, (alat - plat) * 60.0
    bx, by = (blon - plon) * 60.0 * cl, (blat - plat) * 60.0
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    if seg2 <= 1e-12:
        return math.hypot(ax, ay)
    t = max(0.0, min(1.0, -(ax * dx + ay * dy) / seg2))
    return math.hypot(ax + t * dx, ay + t * dy)


def poly_distance_nm(lat, lon, verts):
    """0 if the point is inside the polygon, else the nearest-edge distance."""
    if len(verts) >= 3 and point_in_polygon(lat, lon, verts):
        return 0.0
    best = float("inf")
    n = len(verts)
    for i in range(n):
        a = verts[i]
        b = verts[(i + 1) % n]
        best = min(best, _seg_distance_nm(lat, lon, a[0], a[1], b[0], b[1]))
    return best


def route_distance_nm(lat, lon, route_pts):
    """Min distance (nm) from a point to a route polyline, or None if no route."""
    if not route_pts or len(route_pts) < 2:
        return None
    best = float("inf")
    for i in range(len(route_pts) - 1):
        a, b = route_pts[i], route_pts[i + 1]
        best = min(best, _seg_distance_nm(lat, lon, a[0], a[1], b[0], b[1]))
    return best


def rank_advisories(items, ac_lat, ac_lon, route_pts=None, buffer_nm=30.0):
    """Annotate + sort advisory ``items`` nearest-first with an on-route flag.

    Each item: ``{text, verts?, point?}`` — ``verts`` for a polygon hazard,
    ``point=(lat,lon)`` for a located bulletin (e.g. a NOTAM's airport), neither
    for an un-locatable one.  Returns ``[{text, dist, on_route}]`` sorted
    on-route first, then by distance (un-locatable last).  Nothing is dropped."""
    out = []
    for it in items:
        verts = it.get("verts")
        point = it.get("point")
        dist = None
        loc = None
        if verts:
            loc = (sum(v[0] for v in verts) / len(verts),
                   sum(v[1] for v in verts) / len(verts))
            if ac_lat is not None and ac_lon is not None:
                dist = poly_distance_nm(ac_lat, ac_lon, verts)
        elif point:
            loc = point
            if ac_lat is not None and ac_lon is not None:
                dist = nm_between(ac_lat, ac_lon, point[0], point[1])
        on_route = False
        if loc and route_pts:
            rd = route_distance_nm(loc[0], loc[1], route_pts)
            on_route = rd is not None and rd <= buffer_nm
        out.append({"text": it["text"], "dist": dist, "on_route": on_route})
    out.sort(key=lambda e: (not e["on_route"],
                            e["dist"] if e["dist"] is not None else 1e9))
    return out


# ── Encoders (test feeds / simulators; mirror the decoders) ─────────────────────
_DLAC_REV = {ch: i for i, ch in enumerate(_DLAC)}


def encode_dlac(text):
    """Pack a string into DLAC 6-bit bytes (inverse of ``dlac_decode``)."""
    bits = nbits = 0
    out = bytearray()
    for ch in text:
        bits = (bits << 6) | _DLAC_REV.get(ch, _DLAC_REV[" "])
        nbits += 6
        while nbits >= 8:
            nbits -= 8
            out.append((bits >> nbits) & 0xFF)
    if nbits:
        out.append((bits << (8 - nbits)) & 0xFF)
    return bytes(out)


def encode_ground_station_header(lat, lon, site_id=0, position_valid=True):
    """Build the 8-byte UAT uplink header (inverse of decode_ground_station)."""
    raw_lat = int(round((lat % 360.0) / 360.0 * 16777216.0)) & 0x7FFFFF
    raw_lon = int(round((lon % 360.0) / 360.0 * 16777216.0)) & 0xFFFFFF
    h = bytearray(8)
    h[0] = (raw_lat >> 15) & 0xFF
    h[1] = (raw_lat >> 7) & 0xFF
    h[2] = ((raw_lat << 1) & 0xFE) | ((raw_lon >> 23) & 0x01)
    h[3] = (raw_lon >> 15) & 0xFF
    h[4] = (raw_lon >> 7) & 0xFF
    h[5] = ((raw_lon << 1) & 0xFE) | (0x01 if position_valid else 0x00)
    h[7] = (site_id & 0x0F) << 4
    return bytes(h)


def encode_text_uplink(reports, station=None, product_id=413, total=432):
    """Build a UAT uplink payload carrying ``reports`` (a list of text strings)
    as one text-product FIS-B APDU, optionally from ground station
    ``station=(lat, lon[, site_id])``.  Mirrors the decode path end-to-end:
    iter_information_frames → decode_apdu → text_records.  Used by the dump978
    test emitter and the ground simulator."""
    text = "\x1e".join(reports) + "\x03"               # RS-separated, ETX-ended
    dlac = encode_dlac(text)
    apdu = bytes([(product_id >> 6) & 0x1f,
                  (product_id & 0x3f) << 2]) + dlac     # T-opt 0 header + data
    return _pack_uplink(product_id, apdu, station, total)


def _pack_uplink(product_id, apdu_payload, station, total):
    """Wrap one APDU payload (product header bytes + data) into a UAT uplink
    payload with an optional ground-station header.  Shared by the encoders."""
    length = len(apdu_payload)
    info = bytes([(length >> 1) & 0xFF,
                  ((length & 1) << 7) | FRAME_TYPE_FISB_APDU]) + apdu_payload
    app_len = total - _UAT_HEADER_LEN
    app = (info + b"\x00\x00").ljust(app_len, b"\x00")[:app_len]
    if station:
        header = encode_ground_station_header(
            station[0], station[1],
            site_id=station[2] if len(station) > 2 else 0)
    else:
        header = b"\x00" * _UAT_HEADER_LEN
    return header + app


def encode_graphics_uplink(graphics, station=None, product_id=14, total=432):
    """Build a UAT uplink payload carrying ``graphics`` (list of
    ``{hazard, vertices:[(lat,lon)]}``) as one graphical-product APDU — inverse
    of decode_graphic_records.  For the simulator / tests."""
    body = bytearray()
    for g in graphics:
        hz = _GFX_HAZARD_REV.get(g.get("hazard"), 15)
        verts = g.get("vertices", [])
        body += bytes([hz, 0, len(verts) & 0xFF])
        for la, lo in verts:
            body += _gfx_enc_coord(la) + _gfx_enc_coord(lo)
        txt = g.get("text") or ""
        dt = encode_dlac(txt + "\x03") if txt else b""
        body += bytes([(len(dt) >> 8) & 0xFF, len(dt) & 0xFF]) + dt
    apdu = bytes([(product_id >> 6) & 0x1f,
                  (product_id & 0x3f) << 2]) + bytes(body)
    return _pack_uplink(product_id, apdu, station, total)
