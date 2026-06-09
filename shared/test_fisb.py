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


def build_uplink_payload(app_data):
    return (b"\x00" * fisb._UAT_HEADER_LEN) + app_data


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


def main():
    test_dlac()
    test_framing()
    test_apdu()
    test_metar_parse()
    test_metar_station()
    test_pipeline()
    print("ALL FIS-B TESTS PASSED (%d checks, %d cases)" % (_checks, _cases))


if __name__ == "__main__":
    main()
