# Next-session backlog

Logged at the end of the radio/ADS-B session (branch
`claude/nooelec-radio-equipment-0fuq7s`, merged to main). Context for picking
these up cold.

## FIS-B WX UI — remaining TODO (branch `claude/open-items-followup-93awex`)

Done this session: METAR, TAF (decoded/labeled + nearest + direction),
AIRMET/SIGMET/NOTAM text (nearest-first + on-route + valid times), graphical
AIRMET/SIGMET hazard areas on MET (tap-for-bulletin), winds aloft (table +
graphical barbs on the WND overlay with an altitude selector), and a TFC-page
aircraft detail card.  All driven/tested by `tools/fisb_sim.py`.

Still open:
- **NEXRAD radar (the big one):** decode the FIS-B regional/CONUS run-length
  block raster into the existing NEXRAD render path; drive with synthetic
  rasters from the sim.  Highest impact, hardest decode, wants real-frame
  validation.
- **Winds vertical profile / route cross-section (FMS-type, Option B):** a
  chart of altitude vs. position along the active route (wind/temp per level).
  Niche; needs a solid route.  Deferred — do after NEXRAD.
- **Real-frame validation** of every binary decode written blind: APDU
  timestamps, the 8-byte uplink/station header, graphical geometry overlays,
  and the winds bulletin envelope (the per-code FD decode is standard; the
  `WINDS <id> <alt> <code>…` framing is a sim stand-in).

## Progress (branch `claude/open-items-followup-93awex`)

- **Map cleanup items 1–3: done.** Airport dots are now neutral white on both
  renderers; the big MFD draws METAR dots on every page (and the MET overlay
  now *hides* airport dots so it reads as a clean weather-only picture — guard
  against clutter); the doubled D2 / airport-loop label is de-duplicated.
- **FIS-B decoder Stage 1: done in `shared/fisb.py`** (+ `shared/test_fisb.py`,
  78 checks). DLAC 6-bit text decode, the information-frame walk, the APDU
  header (product id + T-opt timestamp), raw-METAR parsing → the same station
  dicts `wx.parse_metars` emits (tagged `src="RDR"`). Framing + DLAC are
  round-trip-tested; **APDU timestamps still need a sanity check against live
  978 frames.**
- **FIS-B store + app wiring: done.** `fisb.FisbWeather` (thread-safe, fed off
  the GDL90 stream by `ADSBClient`, geolocation deferred) + `merge_metar_sources`
  (RDR wins, INET backfills). `_update_weather` in both apps merges radio over
  internet (throttled ~3 s geolocation; merge skipped entirely when no radio WX,
  so the internet path is unchanged). MFD WX status shows the R/I split.

### FIS-B — what's left
1. **Hardware/reception** (the prereq below): install dump978 on `--device
   978`, get its uplink frames to the app, confirm `uplink_count` climbs. This
   is now the *only* thing between us and radio weather on screen — the whole
   software path is in and tested with synthetic frames.
2. **Validate against live frames:** confirm METARs actually populate
   (`weather.n_rdr` > 0, "WX R… I…" on the status line) and sanity-check the
   APDU timestamp decode (the one part not pinned to the wire).
3. **Stages 2–3:** winds aloft / AIRMET-SIGMET / NOTAM text (extend
   `FisbWeather`/decoder for non-METAR text products), then FIS-B NEXRAD
   (the block-based run-length raster) feeding the existing NEXRAD render path.

## Big item: FIS-B weather over 978 UAT

Goal: weather over the radio (no internet needed) so LTE/Starlink becomes a
bonus, mirroring the **radio-primary / internet-bonus** model we already built
for traffic (with source attribution: RDR vs INET counts in the status line).

State of play:
- The **978 dongle is free** (serial `978`); readsb runs on the `1090` dongle.
- `shared/gdl90.py` already decodes the GDL90 **uplink** frame (`kind="uplink"`)
  and the app counts `uplink_count` — but the FIS-B APDU **payload is not
  parsed** yet.

Prereq (hardware / on the Pi):
- Install **dump978** on `--device 978` (wiedehopf's dump978 install), and get
  its UAT **uplink** frames to the display — either via a GDL90 978 path
  (Stratux-style, message id `0x07` over UDP 4000) or by reading dump978's raw
  output directly.
- Confirm reception first: `uplink_count` climbing. **Caveat:** FIS-B is
  line-of-sight from ground transmitters and is sparse on the ground / at low
  altitude — may see little at the house until airborne near a station. This is
  exactly why internet stays as backfill.

Decoder work (`shared/fisb.py`, staged for early wins):
1. **METAR/TAF text first** (easy, high value) — decode to the same METAR dicts
   the picker + overlay already consume. Instant payoff.
2. Winds aloft + AIRMET/SIGMET/NOTAM text.
3. **FIS-B NEXRAD** last (the hard one — custom block-based run-length raster,
   regional + CONUS, not a PNG) — feeds the same NEXRAD render path.

Then extend the source-attribution UI: weather should show RDR (FIS-B) vs INET
source, same as traffic. AUTO = FIS-B preferred, internet backfills gaps.

## Map cleanup items

1. **Airport dot color.** Public airport dots are currently green (`_APT_PUB`),
   which now reads as a VFR weather station since METAR dots are flight-category
   colored (green=VFR / blue=MVFR / red=IFR / magenta=LIFR). Make airport dots
   **white** (or another neutral) so airports vs WX stations are unambiguous.
   Files: `_APT_PUB` in `pi4/moving_map.py` and `pi_zero/moving_map.py`.

2. **Always-on METAR dots on pi4.** We now poll METARs continuously (for the
   airport-tap picker). Consider drawing the METAR dots on the pi4 MFD on
   *all* pages, not only when the MET overlay is selected — pi4 has the screen
   + horsepower. (Keep piZ gated to the overlay.) Touches the METAR draw gate
   in `pi4/moving_map._draw_metars` and/or how `metars=` is passed in
   `pi4/pfd.draw_mfd`.

3. **Doubled destination label.** When an airport is the active Direct-To, the
   magenta D2 ident label is drawn **on top of** the white airport-loop label
   for the same field — overlapping/doubled text. Pick one: when an airport's
   ident == the active D2 ident, **skip the airport-loop label** for it (let the
   magenta D2 label be the only one). Both renderers: airport label loop vs the
   D2 diamond label section in `moving_map.py`.
