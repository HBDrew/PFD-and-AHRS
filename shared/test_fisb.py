"""
test_fisb.py – round-trip tests for the FIS-B 978 UAT weather decoder.

Run directly: ``python3 shared/test_fisb.py`` (no pytest dependency, matching
the other shared/test_*.py modules).

There are no captured live 978 frames in the tree yet (reception is pending
hardware on the Pi), so these tests pin the framing + DLAC + APDU layout by
encoding synthetic frames with a matching encoder and decoding them back.  The
encoder lives here in the test, never in the shipping module.
"""

import time

import fisb
import gdl90


_checks = 0
_cases = 0


def check(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        raise AssertionError(msg)


def case(name):
    global _cases
    _cases += 1


# ── Synthetic encoders (inverse of the decoder, for tests only) ─────────────────
_DLAC_REV = {ch: i for i, ch in enumerate(fisb._DLAC)}


def dlac_encode(text):
    bits = 0
    nbits = 0
    out = bytearray()
    for ch in text:
        bits = (bits << 6) | _DLAC_REV[ch]
        nbits += 6
        while nbits >= 8:
            nbits -= 8
            out.append((bits >> nbits) & 0xff)
    if nbits:
        out.append((bits << (8 - nbits)) & 0xff)
    return bytes(out)


def build_apdu_notime(product_id, payload):
    """FIS-B APDU with T-opt 0 (no timestamp): 2-byte header + payload."""
    o0 = (0 << 5) | ((product_id >> 6) & 0x1f)
    o1 = ((product_id & 0x3f) << 2)
    return bytes([o0, o1]) + payload


def build_apdu_monthday(product_id, month, day, hours, minutes, payload):
    """FIS-B APDU with T-opt 2 (month/day/hours/minutes): 5-byte header."""
    o0 = (2 << 5) | ((product_id >> 6) & 0x1f)
    o1 = ((product_id & 0x3f) << 2) | ((month >> 2) & 0x03)
    o2 = ((month & 0x03) << 6) | ((day & 0x1f) << 1) | ((hours >> 4) & 0x01)
    o3 = ((hours & 0x0f) << 4) | ((minutes >> 2) & 0x0f)
    o4 = ((minutes & 0x03) << 6)
    return bytes([o0, o1, o2, o3, o4]) + payload


def build_info_frame(frame_type, data):
    length = len(data)
    b0 = (length >> 1) & 0xff
    b1 = ((length & 0x01) << 7) | (frame_type & 0x0f)
    return bytes([b0, b1]) + data


def build_app_data(frames, total=424):
    block = b"".join(frames)
    block += b"\x00\x00"                    # zero-length frame = end/FILL
    return block.ljust(total, b"\x00")[:total]


def build_uplink_header(lat, lon, position_valid=True, site_id=0):
    """Encode the 8-byte UAT uplink header — inverse of decode_ground_station."""
    raw_lat = int(round((lat % 360.0) / 360.0 * 16777216.0)) & 0x7FFFFF  # 23 bits
    raw_lon = int(round((lon % 360.0) / 360.0 * 16777216.0)) & 0xFFFFFF  # 24 bits
    h = bytearray(8)
    h[0] = (raw_lat >> 15) & 0xFF
    h[1] = (raw_lat >> 7) & 0xFF
    h[2] = ((raw_lat << 1) & 0xFE) | ((raw_lon >> 23) & 0x01)
    h[3] = (raw_lon >> 15) & 0xFF
    h[4] = (raw_lon >> 7) & 0xFF
    h[5] = ((raw_lon << 1) & 0xFE) | (0x01 if position_valid else 0x00)
    h[6] = 0
    h[7] = (site_id & 0x0F) << 4
    return bytes(h)


def build_uplink_payload(app_data, header=None):
    head = header if header is not None else (b"\x00" * fisb._UAT_HEADER_LEN)
    return head + app_data


# A tiny airport DB for geolocation.
_COORDS = {
    "KSEZ": (34.8485, -111.7884),
    "KPHX": (33.4343, -112.0116),
    "KFLG": (35.1385, -111.6713),
}


def _locate(icao):
    return _COORDS.get(icao)


# ── DLAC round-trip ─────────────────────────────────────────────────────────────
def test_dlac():
    case("DLAC alphabet")
    check(len(fisb._DLAC) == 64, "DLAC alphabet must be 64 entries")
    check(fisb._DLAC[1] == "A" and fisb._DLAC[26] == "Z", "A-Z mapping")
    check(fisb._DLAC[32] == " " and fisb._DLAC[48] == "0", "ASCII tail mapping")

    case("DLAC encode/decode round-trip")
    for text in ("KSEZ 121753Z", "METAR KPHX 121751Z 28015G25KT 10SM",
                 "A", "AB", "ABC", "ABCD", "0123456789"):
        decoded = fisb.dlac_decode(dlac_encode(text))
        # Trailing zero bits can decode to a spurious ETX; compare the prefix.
        check(decoded[:len(text)] == text,
              "DLAC round-trip failed for %r -> %r" % (text, decoded))


# ── Information-frame framing ───────────────────────────────────────────────────
def test_framing():
    case("single information frame")
    payload = b"hello-fisb-payload"
    frame = build_info_frame(fisb.FRAME_TYPE_FISB_APDU, payload)
    frames = list(fisb.iter_information_frames(frame))
    check(len(frames) == 1, "expected one frame")
    check(frames[0][0] == 0 and frames[0][1] == payload, "frame type/data")

    case("multiple frames stop at FILL")
    block = build_app_data([build_info_frame(0, b"AAAA"),
                            build_info_frame(5, b"BB"),
                            build_info_frame(0, b"CCCCCC")])
    frames = list(fisb.iter_information_frames(block))
    check([f[0] for f in frames] == [0, 5, 0], "three frames before FILL")
    check(frames[1][1] == b"BB", "second frame data")

    case("truncated tail is ignored, not guessed")
    # length says 10 but only 3 bytes follow → decoder must stop cleanly.
    bad = build_info_frame(0, b"xxxxxxxxxx")[:5]
    check(list(fisb.iter_information_frames(bad)) == [], "truncated -> empty")


# ── APDU header ─────────────────────────────────────────────────────────────────
def test_apdu():
    case("APDU T-opt 0 product id + data offset")
    apdu = fisb.decode_apdu(build_apdu_notime(413, b"\x01\x02\x03"))
    check(apdu["product_id"] == 413, "product id 413")
    check(apdu["name"] == "Generic Text", "product name")
    check(apdu["t_opt"] == 0 and apdu["hours"] is None, "no timestamp")
    check(apdu["data"] == b"\x01\x02\x03", "data offset 2")

    case("APDU T-opt 2 month/day/hours/minutes round-trip")
    apdu = fisb.decode_apdu(
        build_apdu_monthday(11, month=6, day=9, hours=17, minutes=53,
                            payload=b"DATA"))
    check(apdu["product_id"] == 11 and apdu["name"] == "AIRMET", "AIRMET id")
    check((apdu["month"], apdu["day"], apdu["hours"], apdu["minutes"])
          == (6, 9, 17, 53), "timestamp fields round-trip")
    check(apdu["data"] == b"DATA", "data offset 5")

    case("short frame -> None")
    check(fisb.decode_apdu(b"\x00") is None, "1-byte frame rejected")


# ── Raw-METAR parsing ───────────────────────────────────────────────────────────
def test_metar_parse():
    case("VFR METAR fields")
    p = fisb.parse_metar_text(
        "KSEZ 091753Z 24008KT 10SM FEW120 28/06 A3001")
    check(p["icao"] == "KSEZ", "station id")
    check(p["wdir"] == 240 and p["wspd"] == 8, "wind")
    check(abs(p["visib_mi"] - 10.0) < 1e-6, "visibility 10sm")
    check(p["ceiling_ft"] is None, "FEW is not a ceiling")
    check(p["temp_c"] == 28 and p["dewp_c"] == 6, "temp/dewp")
    check(abs(p["altim_hpa"] - 1016.1) < 0.5, "altimeter inHg->hPa")
    check(p["fltcat"] == "VFR", "VFR category")

    case("IFR METAR via low ceiling + gust + fraction visibility")
    p = fisb.parse_metar_text(
        "METAR KPHX 091751Z 28015G25KT 1 1/2SM BR OVC007 22/19 A2992")
    check(p["icao"] == "KPHX", "station id after METAR keyword")
    check(p["wgst"] == 25, "gust")
    check(abs(p["visib_mi"] - 1.5) < 1e-6, "1 1/2 SM")
    check(p["ceiling_ft"] == 700, "OVC007 -> 700 ft")
    check(p["fltcat"] == "IFR", "IFR category")

    case("LIFR via 1/2SM + VV002")
    p = fisb.parse_metar_text("KFLG 091756Z VRB03KT 1/2SM FG VV002 05/05 A3010")
    check(p["wdir"] is None and p["wspd"] == 3, "VRB wind")
    check(abs(p["visib_mi"] - 0.5) < 1e-6, "half-mile vis")
    check(p["ceiling_ft"] == 200, "VV002 -> 200 ft")
    check(p["fltcat"] == "LIFR", "LIFR category")

    case("non-METAR text rejected")
    check(fisb.parse_metar_text("TAF KSEZ 091720Z 0918/1018 24010KT") is None,
          "TAF is not a METAR observation")
    check(fisb.parse_metar_text("") is None, "empty -> None")


# ── Station dict (the picker/overlay bridge) ────────────────────────────────────
def test_metar_station():
    case("station dict matches wx shape + geolocates")
    now = time.time()
    st = fisb.metar_station("KSEZ 091753Z 24008KT 10SM FEW120 28/06 A3001",
                            _locate, now=now)
    for key in ("icao", "lat", "lon", "fltcat", "wdir", "wspd",
                "visib_mi", "ceiling_ft", "altim_hpa", "temp_c", "dewp_c",
                "raw", "age_min"):
        check(key in st, "station dict missing key %r" % key)
    check(st["icao"] == "KSEZ" and abs(st["lat"] - 34.8485) < 1e-4, "geoloc")
    check(st["src"] == "RDR", "source attribution = radio")
    check(st["age_min"] is not None and st["age_min"] >= 0, "age computed")

    case("unlocatable station -> None")
    check(fisb.metar_station("ZZZZ 091753Z 24008KT 10SM 28/06 A3001",
                             _locate, now=now) is None,
          "no coords -> dropped")


# ── Full pipeline through the real GDL90 deframer ───────────────────────────────
def test_pipeline():
    case("GDL90 0x07 -> APDUs -> METAR stations")
    reports = ("KSEZ 091753Z 24008KT 10SM FEW120 28/06 A3001"
               "\x1e"
               "KPHX 091751Z 28015G25KT 1 1/2SM BR OVC007 22/19 A2992"
               "\x03")
    app = build_app_data([
        build_info_frame(fisb.FRAME_TYPE_FISB_APDU,
                         build_apdu_notime(413, dlac_encode(reports)))])
    uplink_payload = build_uplink_payload(app)

    # Wrap as a real GDL90 Uplink Data message: id(1)+ToR(3)+payload(432),
    # frame it with CRC/flags, then run it back through the shipping deframer.
    body = bytes([gdl90.MSG_UPLINK]) + b"\x00\x00\x00" + uplink_payload
    framed = gdl90.frame_message(body)
    msgs = gdl90.decode_stream(framed)
    uplinks = [m for m in msgs if m.get("kind") == "uplink"]
    check(len(uplinks) == 1, "deframer surfaced one uplink")

    apdus = fisb.decode_gdl90_uplink(uplinks[0])
    check(len(apdus) == 1 and apdus[0]["product_id"] == 413,
          "one text APDU recovered")

    stations = fisb.metars_from_apdus(apdus, _locate)
    idents = sorted(s["icao"] for s in stations)
    check(idents == ["KPHX", "KSEZ"], "both METARs recovered: %r" % idents)
    phx = next(s for s in stations if s["icao"] == "KPHX")
    check(phx["fltcat"] == "IFR", "KPHX classified IFR end-to-end")
    check(all(s["src"] == "RDR" for s in stations), "all tagged RDR")


def _uplink_msg(reports_text):
    """Wrap report text in a GDL90 0x07 uplink message dict (as the deframer
    would surface it)."""
    app = build_app_data([
        build_info_frame(fisb.FRAME_TYPE_FISB_APDU,
                         build_apdu_notime(413, dlac_encode(reports_text)))])
    raw = (bytes([gdl90.MSG_UPLINK]) + b"\x00\x00\x00"
           + build_uplink_payload(app))
    return {"kind": "uplink", "raw": raw}


def test_store():
    case("store ingests + geolocates + counts")
    store = fisb.FisbWeather()
    store.ingest_gdl90_msg(_uplink_msg(
        "KSEZ 091753Z 24008KT 10SM FEW120 28/06 A3001\x1e"
        "KPHX 091751Z 28015G25KT 1 1/2SM BR OVC007 22/19 A2992\x03"))
    check(store.uplink_count == 1, "one uplink ingested")
    check(store.count() == 2, "two METARs stored")
    sts = store.metar_stations(_locate)
    check(sorted(s["icao"] for s in sts) == ["KPHX", "KSEZ"], "geolocated both")
    check(all(s["src"] == "RDR" for s in sts), "tagged RDR")

    case("latest report per station replaces the old one")
    store.ingest_gdl90_msg(_uplink_msg(
        "KSEZ 091853Z 24008KT 1/2SM FG OVC003 28/06 A3001\x03"))
    check(store.count() == 2, "still two stations (KSEZ updated, not added)")
    sez = next(s for s in store.metar_stations(_locate) if s["icao"] == "KSEZ")
    check(sez["fltcat"] == "LIFR", "KSEZ now LIFR from the newer report")

    case("receipt-expired entries are pruned")
    mono = time.monotonic()
    store.ingest_gdl90_msg(_uplink_msg("KFLG 091756Z 24008KT 10SM 05/M02 A3010\x03"),
                           now_mono=mono - 10000.0)        # long ago
    sts = store.metar_stations(_locate, now_mono=mono)
    check("KFLG" not in [s["icao"] for s in sts], "stale KFLG pruned on read")

    case("unlocatable stored station is skipped, not crashed")
    store2 = fisb.FisbWeather()
    store2.ingest_gdl90_msg(_uplink_msg("ZZZZ 091753Z 24008KT 10SM 28/06 A3001\x03"))
    check(store2.metar_stations(_locate) == [], "no coords -> not drawn")


def test_ground_station():
    case("uplink header position round-trips")
    # Phoenix-ish tower.
    hdr = build_uplink_header(33.43, -112.01, position_valid=True, site_id=5)
    gs = fisb.decode_ground_station(hdr + b"\x00" * 100)
    check(gs is not None and gs["position_valid"], "position valid")
    check(abs(gs["lat"] - 33.43) < 0.01, f"lat {gs['lat']}")
    check(abs(gs["lon"] - (-112.01)) < 0.01, f"lon {gs['lon']}")
    check(gs["site_id"] == 5, "site id")

    case("position-invalid header flagged, short payload -> None")
    hdr0 = build_uplink_header(0, 0, position_valid=False)
    check(fisb.decode_ground_station(hdr0 + b"\x00" * 100)["position_valid"]
          is False, "invalid flag honoured")
    check(fisb.decode_ground_station(b"\x00\x00") is None, "short -> None")

    case("store tracks the heard station with recency + count")
    store = fisb.FisbWeather()
    app = build_app_data([build_info_frame(
        fisb.FRAME_TYPE_FISB_APDU,
        build_apdu_notime(413, dlac_encode(
            "KPHX 091751Z 28015G25KT 10SM 22/19 A2992\x03")))])
    payload = build_uplink_payload(
        app, header=build_uplink_header(33.43, -112.01, site_id=5))
    mono = time.monotonic()
    store.ingest_uplink(payload, now_mono=mono)
    gss = store.ground_stations(now_mono=mono)
    check(len(gss) == 1, "one station heard")
    check(abs(gss[0]["lat"] - 33.43) < 0.01 and gss[0]["count"] == 1,
          "station position + count")
    check(gss[0]["age_s"] < 0.01, "fresh (age ~0)")
    # Same tower again → count climbs, still one station.
    store.ingest_uplink(payload, now_mono=mono + 1.0)
    gss = store.ground_stations(now_mono=mono + 1.0)
    check(len(gss) == 1 and gss[0]["count"] == 2, "re-heard increments count")

    case("position-invalid uplink records no station")
    store2 = fisb.FisbWeather()
    payload0 = build_uplink_payload(
        app, header=build_uplink_header(0, 0, position_valid=False))
    store2.ingest_uplink(payload0)
    check(store2.ground_stations() == [], "no station without a valid position")

    case("stations age out")
    gss = store.ground_stations(now_mono=mono + 1.0 + store.station_expire_s + 1)
    check(gss == [], "station pruned past station_expire_s")


def test_taf_decode():
    case("parse_taf splits periods and decodes elements")
    raw = ("TAF KPHX 091720Z 0918/1024 28015G25KT 1 1/2SM BR OVC007 "
           "FM092200 25008KT P6SM SCT100 "
           "TEMPO 0922/1002 2SM -SHRA BKN030 "
           "PROB30 1002/1006 VRB06KT 1/2SM TSRA OVC015")
    p = fisb.parse_taf(raw)
    check(p is not None and p["icao"] == "KPHX", "station")
    check((p["valid_from"], p["valid_to"]) == ("0918", "1024"), "validity")
    kinds = [g["kind"] for g in p["periods"]]
    check(kinds == ["INITIAL", "FM", "TEMPO", "PROB"], f"period kinds {kinds}")

    init = p["periods"][0]
    check(init["wind"] == "280° 15 kt G25", f"initial wind: {init['wind']}")
    check(init["vis"] == "1 1/2 sm", f"initial vis: {init['vis']}")
    check(init["sky"] == "OVC 700", f"initial sky: {init['sky']}")
    check(init["ceiling_ft"] == 700, "initial ceiling ft")
    check(init["wx"] == "mist", f"initial wx decoded: {init['wx']}")

    fm = p["periods"][1]
    check(fm["label"] == "From 22:00Z", f"FM label {fm['label']}")
    check(fm["wind"] == "250° 8 kt" and fm["vis"] == "6+ sm", "FM wind/vis")
    check(fm["sky"] == "SCT 10,000" and fm["ceiling_ft"] is None,
          "FM sky (SCT is not a ceiling)")

    tempo = p["periods"][2]
    check(tempo["wx"] == "light rain showers", f"TEMPO wx: {tempo['wx']}")

    pr = p["periods"][3]
    check(pr["label"].startswith("30% "), f"PROB label {pr['label']}")
    check(pr["wx"] == "thunderstorm w/ rain", f"PROB wx: {pr['wx']}")

    case("taf_lines: header + one line per period")
    lines = fisb.taf_lines(raw)
    check(len(lines) == 5, f"1 header + 4 periods, got {len(lines)}")
    check(lines[0].startswith("KPHX valid 18Z–24Z"), f"header {lines[0]}")
    check(fisb.taf_lines("not a taf") == [], "non-TAF -> []")


def test_classify_and_taf():
    case("classify_text routes each product type")
    check(fisb.classify_text("KSEZ 091753Z 24008KT 10SM 28/06 A3001") == "METAR",
          "bare METAR")
    check(fisb.classify_text("METAR KPHX 091751Z 28015KT") == "METAR", "METAR kw")
    check(fisb.classify_text("TAF KSEZ 091720Z 0918/1018 24010KT") == "TAF",
          "TAF kw")
    check(fisb.classify_text("KSEZ 091720Z 0918/1018 24010KT P6SM") == "TAF",
          "bare TAF (validity window, no obs time)")
    check(fisb.classify_text("SFOT WA 091045 AIRMET TANGO ...") == "AIRMET",
          "AIRMET")
    check(fisb.classify_text("CONVECTIVE SIGMET 12C ...") == "SIGMET", "SIGMET")
    check(fisb.classify_text("!SEZ 06/001 SEZ RWY 03/21 CLSD") == "NOTAM",
          "NOTAM (leading !)")
    check(fisb.classify_text("") is None, "blank -> None")

    case("taf_ident pulls the station, skipping TAF/AMD/COR")
    check(fisb.taf_ident("TAF KSEZ 091720Z 0918/1018 24010KT") == "KSEZ",
          "TAF KSEZ")
    check(fisb.taf_ident("TAF AMD KPHX 091930Z 0920/1024 28012KT") == "KPHX",
          "TAF AMD KPHX")

    case("store keeps TAFs separate from METARs, retrievable by ident")
    store = fisb.FisbWeather()
    reports = ("KSEZ 091753Z 24008KT 10SM FEW120 28/06 A3001\x1e"
               "TAF KSEZ 091720Z 0918/1018 24010KT P6SM FEW120\x03")
    store.ingest_gdl90_msg(_uplink_msg(reports))
    check(store.count() == 1, "one METAR stored")
    check(store.taf_count == 1, "one TAF stored")
    taf = store.taf_for("KSEZ")
    check(taf is not None and taf.startswith("TAF KSEZ"), f"TAF retrieved: {taf}")
    check(store.taf_for("KXXX") is None, "no TAF for unknown station")
    check(store.taf_stations() == ["KSEZ"], "taf_stations lists TAF holders")

    case("TAF ages out past taf_expire_s")
    mono = time.monotonic()
    store.ingest_uplink(
        _uplink_msg("TAF KFLG 091720Z 0918/1018 VRB03KT\x03")["raw"][4:],
        now_mono=mono - 1.0)
    check(store.taf_for("KFLG", now_mono=mono) is not None, "fresh TAF kept")
    check(store.taf_for("KFLG", now_mono=mono + store.taf_expire_s + 1) is None,
          "stale TAF pruned")


def test_advisories():
    case("store files AIRMET/SIGMET/NOTAM text by kind")
    store = fisb.FisbWeather()
    recs = ("\x1e".join([
        "KSEZ 091753Z 24008KT 10SM 28/06 A3001",
        "WAUS46 KKCI 091445 AIRMET TANGO FOR TURB VALID UNTIL 092100",
        "WSUS01 KKCI 091455 CONVECTIVE SIGMET 12C VALID UNTIL 091655",
        "!SEZ 06/001 SEZ RWY 03/21 CLSD WEF 0906011200",
    ]) + "\x03")
    store.ingest_gdl90_msg(_uplink_msg(recs))
    check(store.count() == 1, "METAR still stored alongside")
    air = store.advisories("AIRMET")
    sig = store.advisories("SIGMET")
    nts = store.advisories("NOTAM")
    check(len(air) == 1 and "AIRMET TANGO" in air[0], f"AIRMET: {air}")
    check(len(sig) == 1 and "SIGMET 12C" in sig[0], f"SIGMET: {sig}")
    check(len(nts) == 1 and nts[0].startswith("!SEZ"), f"NOTAM: {nts}")
    check(len(store.advisories()) == 3, "all kinds when unfiltered")

    case("re-broadcast de-dupes (keyed by text)")
    store.ingest_gdl90_msg(_uplink_msg(
        "WAUS46 KKCI 091445 AIRMET TANGO FOR TURB VALID UNTIL 092100\x03"))
    check(len(store.advisories("AIRMET")) == 1, "same bulletin not duplicated")

    case("advisories age out")
    mono = time.monotonic()
    store2 = fisb.FisbWeather()
    store2.ingest_uplink(
        _uplink_msg("WSUS01 KKCI CONVECTIVE SIGMET 9C VALID UNTIL 1655\x03")
        ["raw"][4:], now_mono=mono)
    check(len(store2.advisories("SIGMET", now_mono=mono)) == 1, "fresh kept")
    check(store2.advisories(
        "SIGMET", now_mono=mono + store2.advisory_expire_s + 1) == [],
        "stale pruned")


def test_graphics():
    case("graphic geometry round-trips (encode → decode)")
    gfx = [
        {"hazard": "Turbulence", "vertices": [(36.5, -113.0), (36.8, -110.0),
                                              (35.0, -109.5), (34.5, -112.5)],
         "text": "AIRMET TANGO ... MOD TURB BLW FL180"},
        {"hazard": "Convective", "vertices": [(35.4, -111.9), (35.5, -111.2),
                                              (34.9, -111.0)]},
    ]
    payload = fisb.encode_graphics_uplink(gfx, station=(33.43, -112.01, 1))
    apdus = fisb.decode_uplink(payload)
    check(apdus and apdus[0]["product_id"] == 14, "graphical product id 14")
    recs = fisb.decode_graphic_records(apdus[0]["data"])
    check(len(recs) == 2, f"two hazard areas, got {len(recs)}")
    check(recs[0]["hazard"] == "Turbulence" and len(recs[0]["vertices"]) == 4,
          "turbulence polygon w/ 4 vertices")
    la, lo = recs[0]["vertices"][0]
    check(abs(la - 36.5) < 0.001 and abs(lo - (-113.0)) < 0.001,
          f"vertex round-trips: {(la, lo)}")
    check(recs[0]["text"] == "AIRMET TANGO ... MOD TURB BLW FL180",
          f"paired bulletin text round-trips: {recs[0]['text']}")
    check(recs[1]["hazard"] == "Convective" and recs[1]["text"] is None,
          "second area hazard, no text")

    case("store files graphics; ground station still decodes alongside")
    store = fisb.FisbWeather()
    store.ingest_uplink(payload)
    gs = store.graphics()
    check(len(gs) == 2, f"two graphics stored, got {len(gs)}")
    check({g["hazard"] for g in gs} == {"Turbulence", "Convective"}, "hazards")
    check(len(store.ground_stations()) == 1, "station header still read")

    case("graphics age out")
    mono = time.monotonic()
    store2 = fisb.FisbWeather()
    store2.ingest_uplink(payload, now_mono=mono)
    check(len(store2.graphics(now_mono=mono)) == 2, "fresh kept")
    check(store2.graphics(now_mono=mono + store2.graphics_expire_s + 1) == [],
          "stale pruned")


def test_ranking():
    case("polygon distance: inside is 0, outside is nearest edge")
    sq = [(34.0, -112.0), (35.0, -112.0), (35.0, -111.0), (34.0, -111.0)]
    check(fisb.poly_distance_nm(34.5, -111.5, sq) == 0.0, "inside -> 0")
    d = fisb.poly_distance_nm(34.5, -110.0, sq)   # ~1 deg lon east of edge
    check(40 < d < 60, f"~50 nm east of the edge, got {d:.0f}")

    case("route distance + rank: on-route first, then nearest, un-located last")
    route = [(34.0, -112.0), (36.0, -112.0)]      # a N-S line at lon -112
    items = [
        {"text": "FAR AIRMET", "verts": [(38.0, -108.0), (38.2, -107.8),
                                         (37.8, -107.6)]},          # far NE
        {"text": "ON ROUTE SIGMET", "verts": [(35.0, -112.2), (35.2, -112.0),
                                              (34.8, -111.9)]},     # on the line
        {"text": "TEXT ONLY", },                                    # un-located
    ]
    ranked = fisb.rank_advisories(items, 34.0, -112.0, route_pts=route,
                                  buffer_nm=30.0)
    check(ranked[0]["text"] == "ON ROUTE SIGMET" and ranked[0]["on_route"],
          f"on-route first: {ranked[0]}")
    check(ranked[-1]["text"] == "TEXT ONLY" and ranked[-1]["dist"] is None,
          "un-locatable sorts last")
    check(ranked[1]["text"] == "FAR AIRMET" and not ranked[1]["on_route"],
          "far one not flagged on-route")


def test_encoders():
    case("encode_text_uplink round-trips through the decoder")
    reports = ["KSEZ 091753Z 24008KT 10SM FEW120 28/06 A3001",
               "KFLG 091756Z VRB03KT 1/2SM FG VV002 05/05 A3010"]
    payload = fisb.encode_text_uplink(reports, station=(35.14, -111.67, 2))
    check(len(payload) == 432, "default payload is 432 bytes")
    apdus = fisb.decode_uplink(payload)
    recs = [r for a in apdus for r in fisb.text_records(a["data"])]
    check(recs == reports, f"reports survive encode→decode: {recs}")
    gs = fisb.decode_ground_station(payload)
    check(gs["position_valid"] and abs(gs["lat"] - 35.14) < 0.01
          and gs["site_id"] == 2, "station header round-trips")

    case("encode_dlac is the inverse of dlac_decode")
    for s in ("METAR KPHX 121751Z", "0123456789", "A/B-C.D"):
        check(fisb.dlac_decode(fisb.encode_dlac(s))[:len(s)] == s,
              f"dlac round-trip {s!r}")


def test_merge():
    case("RDR overrides INET per station; INET backfills")
    inet = [
        {"icao": "KSEZ", "lat": 1.0, "lon": 2.0, "fltcat": "VFR"},
        {"icao": "KABC", "lat": 3.0, "lon": 4.0, "fltcat": "MVFR"},
    ]
    rdr = [
        {"icao": "KSEZ", "lat": 1.0, "lon": 2.0, "fltcat": "IFR", "src": "RDR"},
        {"icao": "KPHX", "lat": 5.0, "lon": 6.0, "fltcat": "VFR", "src": "RDR"},
    ]
    merged = fisb.merge_metar_sources(rdr, inet)
    by = {m["icao"]: m for m in merged}
    check(set(by) == {"KSEZ", "KABC", "KPHX"}, "union of stations")
    check(by["KSEZ"]["fltcat"] == "IFR" and by["KSEZ"]["src"] == "RDR",
          "radio wins for KSEZ")
    check(by["KABC"]["src"] == "INET", "internet-only station tagged INET")
    check(by["KPHX"]["src"] == "RDR", "radio-only station kept")
    n_rdr = sum(1 for m in merged if m["src"] == "RDR")
    n_inet = sum(1 for m in merged if m["src"] == "INET")
    check((n_rdr, n_inet) == (2, 1), "source counts for the status line")

    case("merge tolerates empty / None inputs")
    check(fisb.merge_metar_sources(None, None) == [], "both empty")
    check(len(fisb.merge_metar_sources([], inet)) == 2, "inet only")


def main():
    test_dlac()
    test_framing()
    test_apdu()
    test_metar_parse()
    test_metar_station()
    test_pipeline()
    test_store()
    test_ground_station()
    test_classify_and_taf()
    test_taf_decode()
    test_advisories()
    test_graphics()
    test_ranking()
    test_encoders()
    test_merge()
    print("ALL FIS-B TESTS PASSED (%d checks, %d cases)" % (_checks, _cases))


if __name__ == "__main__":
    main()
