# Next-session backlog

Logged at the end of the radio/ADS-B session (branch
`claude/nooelec-radio-equipment-0fuq7s`, merged to main). Context for picking
these up cold.

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
