# ADS-B IN — Traffic (and FIS-B weather) on the PFD

This document describes the ADS-B IN feature: how the display ingests
traffic, how to wire up the Nooelec NESDR Nano 2 dual-band receiver, and
what is implemented vs. planned.

## What it does

Live aircraft traffic is drawn on the moving-map inset of both displays
(Pi Zero MFD and Pi 4) as TCAS-style diamonds with a heading leader and a
relative-altitude data tag. Targets are colour-coded by threat:

| Colour | Class | Meaning |
|--------|-------|---------|
| Cyan (hollow diamond) | other | has a position, outside the proximate envelope |
| Amber (filled) | proximate | within 6 NM **and** 1200 ft |
| Red (filled) | alert | within 3 NM **and** 600 ft, or the source flagged a traffic alert |

The data tag reads relative altitude in hundreds of feet with a trend
arrow, e.g. `+12↑` (1200 ft above and climbing), `−05↓` (500 ft below,
descending).

Toggle the layer at **Setup → Display → MAP LAYERS → TFC**.

## Architecture

The display side is **hardware-independent** — it speaks GDL90 over UDP,
the lingua franca of portable ADS-B receivers (Stratux, Sentry, GDL90
bridges). Nothing in the PFD knows about SDR dongles.

```
 1090ES  ── NESDR ──►  dump1090-fa ──┐
                                     ├─ SBS-1 :30003 ─► adsb_gdl90_bridge.py ──┐
 978 UAT ── NESDR ──►  dump978-fa  ──┘                                         │
                                                                              GDL90/UDP :4000
 Internet aggregator  ──►  adsb_internet_feed.py  ───────────────────────────►│  (any/all
 (airplanes.live / opensky, via Wi-Fi / Starlink)                             │   sources fan in)
                                                                              ▼
                                            shared/adsb.py  (ADSBClient: UDP listener,
                                            target table, staleness aging, snapshot)
                                                          │
                                            shared/gdl90.py (deframe + CRC + decode)
                                                          │
                              pfd.py _update_traffic()  → disp["traffic"]
                                                          │
                              moving_map.render(traffic=…) → diamonds
```

### Modules

| File | Role | Tested by |
|------|------|-----------|
| `shared/gdl90.py` | GDL90 framing (0x7E / byte-stuffing / CRC-16) + message decoders (Traffic 0x14, Ownship 0x0A/0x0B, Heartbeat 0x00, FIS-B uplink 0x07). Also an encoder for tests/bridges. | `shared/test_gdl90.py` |
| `shared/adsb.py` | `ADSBClient` threaded UDP listener (SSEClient-style counters), target table with aging, `relative()` range/bearing/rel-alt, `threat_level()`, `demo_targets()`. | `shared/test_adsb.py` |
| `tools/adsb_gdl90_bridge.py` | dump1090 SBS-1 (TCP 30003) → GDL90/UDP bridge using the encoder above. | `--selftest` |
| `tools/adsb_internet_feed.py` | Internet ADS-B aggregator → GDL90/UDP feed (test source + Starlink in-flight source). | `--selftest` |
| `tools/install_adsb.sh` | Installs rtl-sdr + dump1090/dump978 + the bridge systemd unit. | — |

Because every source emits the same GDL90/UDP, they are interchangeable and
can even run side by side — a local SDR receiver, the dump1090 bridge, and
the internet feed all just fan into the one listener on :4000.

### Display wiring

`pfd.py` (both variants) starts an `ADSBClient` at boot when
`cs["adsb_enabled"]` is set, and once per frame calls `_update_traffic()`,
which snapshots the listener (or `adsb.demo_targets()` in `--demo`),
relativises each target against the current ownship fix, classifies its
threat, sorts nearest-first, and stores the result in `disp["traffic"]`.
`moving_map.render(traffic=…)` draws it, gated by `ds["map_show_traffic"]`.

Link diagnostics are mirrored into `cs["adsb_online"|"adsb_rx"|"adsb_err"|
"adsb_last_err"|"adsb_uplink"]` (runtime-only, not persisted).

## Hardware setup (Nooelec NESDR Nano 2 dual-band)

The bundle is two RTL-SDR dongles plus 978/1090 antennas. Run the receiver
on the display Pi, a dedicated Pi (a Pi 5 has ample headroom), or point the
display at an existing Stratux on the network.

```bash
sudo bash tools/install_adsb.sh
sudo reboot                      # so the DVB-T driver blacklist takes effect
```

Give each dongle a distinct USB serial so the decoders grab the right band:

```bash
rtl_eeprom -d 0 -s 1090          # dongle on the 1090 antenna
rtl_eeprom -d 1 -s 978           # dongle on the 978 antenna
```

Then set `RECEIVER_SERIAL` in `/etc/default/dump1090-fa` (=1090) and
`/etc/default/dump978-fa` (=978). dump1090-fa publishes SBS-1 on TCP 30003;
fold 978 traffic into the same feed so one bridge covers both bands.

Verify:

```bash
systemctl status adsb-gdl90.service
python3 tools/adsb_gdl90_bridge.py --selftest
```

## Testing without hardware

```bash
python3 shared/test_gdl90.py     # decoder unit tests
python3 shared/test_adsb.py      # listener + geometry + UDP loopback
python3 pi4/pfd.py --demo --sim  # synthetic traffic orbits the demo aircraft
```

### Testing with real internet data

`tools/adsb_internet_feed.py` pulls live aircraft from a public ADS-B
aggregator and emits them as GDL90/UDP — so you can see *real* traffic on
the map with no SDR and no Pico. Run the display and the feed together:

```bash
# terminal 1 — the PFD (demo flies the Sedona pattern; live traffic wins
# over the synthetic targets, so point the feed at the demo location)
python3 pi4/pfd.py --demo --sim

# terminal 2 — feed real traffic within 80 NM of the demo position
python3 tools/adsb_internet_feed.py --lat 34.8697 --lon -111.7610 --radius 80
```

Point it somewhere busy (`--lat 33.94 --lon -118.40` for KLAX) to stress the
renderer with dozens of targets. Sources: `airplanes_live` (default),
`adsb_lol`, `adsb_fi`, `opensky` — none need an API key.

The "live wins over demo" rule (`_update_traffic`) means any real source —
internet feed, dump1090 bridge, or hardware receiver — overrides the
synthetic demo targets the moment frames start arriving.

## In-flight over Starlink / cabin Wi-Fi

With internet in the aircraft, the same feed becomes a real traffic source —
no receiver required. Run it on the display Pi and have it re-centre on your
own GPS so the query box follows the flight:

```bash
python3 tools/adsb_internet_feed.py --follow-ahrs http://192.168.4.1/events --radius 60
```

(Or install it as a systemd service alongside `adsb-gdl90.service`.)

**Advisory only.** Internet traffic depends on ground-station coverage and
connectivity — it lags a few seconds, has coverage gaps, and won't reliably
include UAT-only / non-transponder aircraft. It is **not** a substitute for
see-and-avoid or a certified traffic system. A local SDR receiver (the
Nooelec) gives better own-ship-relative traffic because it's direct
line-of-sight and works without internet; treat the internet feed as a
convenient backup / test source. Keep the poll interval ≥ 1 s out of
courtesy to the free aggregators.

## Implemented vs. planned

**Implemented (V5.4):**
- GDL90 decode + UDP ingest + target management (tested)
- Relative geometry + TCAS-style threat classification
- Traffic diamonds on both moving maps + TFC layer toggle
- dump1090 → GDL90 bridge + receiver installer
- Demo traffic generator

- Internet traffic feed (`tools/adsb_internet_feed.py`) — no-hardware test
  source and Starlink/cabin-Wi-Fi in-flight source; live traffic overrides
  the demo targets automatically.
- Traffic declutter — altitude-band (±2k/5k/10k) and range (5/10/20/40 nm)
  filters on Setup → Display; alert-class threats always survive.
- **Weather — METARs.** `shared/wx.py` polls aviationweather.gov for the
  current **map view** (not just the aircraft) — `WxClient` takes a
  `view_fn -> (lat, lon, radius)` and re-fetches when you pan far enough,
  zoom, or the periodic refresh is due (debounced so a drag doesn't spam the
  API). Query radius scales with zoom (`WX_RADIUS_ZOOM_K`, clamped
  `WX_MIN/MAX_RADIUS_NM`), so panning/zooming over CONUS loads weather for
  wherever you're looking. Flight-category station dots (green VFR / blue
  MVFR / red IFR / magenta LIFR) render on both maps. Tap a dot on the MFD
  for the decoded readout. Source-agnostic `disp["weather"]`.
- **Overlay quick-cycle** (`shared/mapoverlay.py`, unit-tested). Traffic
  stays on always (safety); a single on-map control cycles the heavy
  overlays one-at-a-time to keep the map readable and CPU low:
  **Airspace → Traffic → METAR → NEXRAD** (labels ASP / TFC / MET / NEX).
  "Traffic" (TFC) is the traffic-only state — no heavy overlay added;
  traffic + your base map still show. On the Pi Zero MFD it's the button
  under the RNG label; on the Pi 4 inset it's the **bottom-left corner**
  (label shows the current state). Both drive the same `ds["map_show_*"]`
  booleans the Setup → Display pills (MET / NEX / ASP) use, so they stay
  consistent. The weather poller only runs while METAR/NEXRAD is the active
  overlay — no CPU/network spent on weather you're not looking at.

**Planned:**
- **NEXRAD raster** weather overlay (internet first — aviationweather.gov /
  IEM tiles — then FIS-B). Its own `map_show_nexrad` layer + OVLY cycle
  slot. Will reuse the same view-following fetch (`view_fn`) as METARs so it
  loads for whatever map area is panned/zoomed into. Needs image fetch +
  georeferencing + alpha blit under the symbols.
- **FIS-B (978 UAT) weather** without internet: the uplink frames (0x07)
  are already captured/counted; decoding the APDU + NEXRAD blocks feeds the
  same `disp["weather"]`.
- METAR detail readout (tap a station → raw METAR / wind / ceiling / vis).
- On-screen TFC status / count + audible/visual traffic alert ("TRAFFIC")
  driven by `disp["traffic"]["alert"]`.
- Connectivity-screen ADS-B LINK row (diagnostics are already plumbed in
  `cs["adsb_*"]`).
- Direct 978 UAT path in the bridge (currently folded via the SBS feed).
