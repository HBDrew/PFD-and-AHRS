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
                         ┌─────────────── on the Pi (or a companion box) ───────────────┐
 1090ES  ── NESDR ──►  dump1090-fa ──┐
                                     ├─► SBS-1 :30003 ─► adsb_gdl90_bridge.py ─► GDL90/UDP :4000 ─┐
 978 UAT ── NESDR ──►  dump978-fa  ──┘                                                            │
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
| `tools/install_adsb.sh` | Installs rtl-sdr + dump1090/dump978 + the bridge systemd unit. | — |

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

## Implemented vs. planned

**Implemented (V5.4):**
- GDL90 decode + UDP ingest + target management (tested)
- Relative geometry + TCAS-style threat classification
- Traffic diamonds on both moving maps + TFC layer toggle
- dump1090 → GDL90 bridge + receiver installer
- Demo traffic generator

**Planned:**
- FIS-B graphical weather (NEXRAD raster, METAR/TAF/winds text) — the
  uplink frames (0x07) are already captured and counted; decoding the
  FIS-B APDU + NEXRAD blocks is the next milestone.
- On-screen TFC status / count + audible/visual traffic alert ("TRAFFIC")
  driven by `disp["traffic"]["alert"]`.
- Connectivity-screen ADS-B LINK row (diagnostics are already plumbed in
  `cs["adsb_*"]`).
- Direct 978 UAT path in the bridge (currently folded via the SBS feed).
