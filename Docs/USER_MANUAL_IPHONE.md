# AHRS PFD — iPhone / Browser Pilot's User Manual

**Software version 0.2 · Hardware: Raspberry Pi Pico W AHRS unit · Display: any iPhone / Android / tablet browser**

*No dedicated display — any smartphone, tablet, or laptop on the Pico W AP becomes the PFD. HTML5 Canvas rendering, SSE-driven at ~20 Hz.*

> This manual covers the iPhone / browser version. For the dedicated Pi Zero 2W display see `USER_MANUAL_ZERO.md`; for the Pi 4 SVT display see `USER_MANUAL_PI4.md`.

---

## Contents

1. [What It Is](#1-what-it-is)
2. [Connecting](#2-connecting)
3. [Screen Overview](#3-screen-overview)
4. [Attitude Indicator](#4-attitude-indicator)
5. [Airspeed Tape](#5-airspeed-tape)
6. [Altitude Tape and VSI](#6-altitude-tape-and-vsi)
7. [Heading Tape](#7-heading-tape)
8. [Slip / Skid Ball](#8-slip--skid-ball)
9. [Badges and Status](#9-badges-and-status)
10. [Baro Setting (QNH)](#10-baro-setting-qnh)
11. [AHRS Trim](#11-ahrs-trim)
12. [Terrain Download](#12-terrain-download)
13. [Demo Mode](#13-demo-mode)
14. [Feature Differences vs Pi 4 / Pi Zero](#14-feature-differences-vs-pi-4--pi-zero)

---

## 1. What It Is

The iPhone display is the **original** PFD for this project and still the simplest way to get flying: with only a Pico W AHRS unit you can use any phone, tablet, or laptop as the display. No dedicated hardware, no dedicated display, no install — just join the `AHRS-Link` Wi-Fi and open a browser.

Everything you see is rendered by the phone's browser from a single HTML file (`iphone_display/index.html`) served by the Pico W itself. The Pico streams live AHRS / GPS / baro state over Server-Sent Events (SSE) at ~20 Hz; the browser redraws the PFD at up to 60 fps with smoothing in between.

![Level cruise — southbound over the Swiss Alps](../iphone_display/previews/preview_level_cruise.png)

## 2. Connecting

1. Power the Pico W AHRS unit.
2. After ~5 s, a Wi-Fi AP named **`AHRS-Link`** appears. Join it with your phone. DHCP assigns you an address on `192.168.4.x`.
3. Open `http://192.168.4.1` in Safari / Chrome / Firefox. The PFD fills the screen.
4. **Add to Home Screen** (Safari → Share → Add to Home Screen) so it launches fullscreen without the address bar. The `apple-mobile-web-app-capable` and safe-area-inset metadata are already in place — the PFD runs edge-to-edge on iPhones with a notch or Dynamic Island.

Lock your phone into portrait orientation, dim the screen for night flying, and you have a three-axis PFD with no extra hardware.

## 3. Screen Overview

![Live link active — green "LINK OK" badge top-right](../iphone_display/previews/preview_live_link_ok.png)

The PFD renders full-bleed in **portrait** (landscape is not yet optimised). The layout is:

| Zone | Content |
|------|---------|
| Top strip | Status badges (GPS, TRIM, QNH, TERRAIN, LINK) |
| Centre | Attitude Indicator — horizon, pitch ladder, roll arc, aircraft symbol |
| Left tape | Airspeed |
| Right tape | Altitude + VSI (green triangle with number + "fpm") |
| Bottom strip | Heading tape + heading readout box |

Tapping any top-strip badge opens the corresponding control panel (QNH, TRIM, TERRAIN). Tapping the speed readout toggles KT ↔ MPH; the QNH readout inside the baro panel toggles hPa ↔ inHg. These unit preferences persist in `localStorage`, so your phone remembers them between flights.

## 4. Attitude Indicator

Sky-blue above, brown ground below, split by a white horizon line. The aircraft symbol (two yellow chevrons + centre dot) is fixed at screen centre; the horizon tilts and translates with roll and pitch.

### Pitch ladder

Short white bars at ±10°, ±20°, ±30°, scale matches the horizon projection so pitch bars, horizon, and zero-pitch line all line up at any attitude.

### Roll arc

Arc across the top of the AI with ticks at 10°, 20°, 30°, 45°, 60° each side of level. A small doghouse at the arc's apex is the fixed sky pointer — when you bank, the arc rotates with the sky, and the doghouse still indicates "up". Read bank angle by checking where the fixed aircraft reference (at the very top) lines up with the rotating ticks.

![Standard-rate right turn](../iphone_display/previews/preview_right_turn.png)

![45° steep bank — slip ball deflects into the turn](../iphone_display/previews/preview_steep_bank_45.png)

### Terrain awareness (no mesh — colour only)

When SRTM tiles are loaded, the ground colour bands by aircraft-to-terrain clearance:

| Clearance | Ground colour |
|-----------|---------------|
| Aircraft below terrain | Red |
| 0–500 ft clear | Amber |
| 500–1 000 ft clear | Yellow |
| > 1 000 ft clear | Normal brown |

There is **no 3D terrain mesh** on the iPhone — it renders terrain as a colour-graded ground plane only. For the full synthetic-vision 3D terrain, use the Pi 4 display.

## 5. Airspeed Tape

![Climbing left turn +800 fpm](../iphone_display/previews/preview_climb_left.png)

The tape scrolls past a centre readout box showing current groundspeed. The `GS KT▼` label in the top-left corner is tappable — tap to toggle the display unit:

| Display | Internal | Label |
|---------|----------|-------|
| Knots | kt | `GS KT▼` |
| Miles per hour | mph | `GS MPH▼` |

The unit choice persists in `localStorage` so the app remembers it next launch. (The iPhone display does **not** yet have V-speed colour arcs on the tape — that's on the port list, see §14.)

## 6. Altitude Tape and VSI

Altitude scrolls in 50 ft steps with labels every 100 ft. Current value is in the centre readout box on the right.

Under the altitude box, the VSI readout shows vertical speed in feet per minute with a green up / red down triangle. Below it the current QNH setting is displayed in the active unit (hPa or inHg).

![Descending into the valley -700 fpm](../iphone_display/previews/preview_descent_valley.png)

## 7. Heading Tape

Bottom of the screen — compass scale scrolling past a centre triangle that marks current magnetic heading. The heading readout box displays the integer heading in degrees. Long-press not required: heading source is whatever the Pico W's fused AHRS provides (magnetometer + gyro).

## 8. Slip / Skid Ball

A small ball slides left / right below the roll arc. Centred = coordinated flight. Deflected = uncoordinated — step on the rudder toward the ball to re-centre it.

![Turbulence with intermittent slip](../iphone_display/previews/preview_turbulence_slip.png)

The ball reacts to lateral acceleration (`ay`) from the IMU, not to the AHRS-computed bank angle, so it catches slips and skids independently of your attitude.

## 9. Badges and Status

Top strip, left to right:

| Badge | Colours | Meaning |
|-------|---------|---------|
| `GPS nSAT` / `DGPS nSAT` / `NO FIX` | green → amber → red | GPS fix status + satellite count |
| `TRIM` | gold | Tap to adjust pitch / roll / heading mounting trim |
| `QNH 29.92` / `QNH 1013` | cyan | Current baro setting; tap to adjust |
| `NO TERRAIN` / `TERRAIN OK` / `TERRAIN…` | grey / green / amber | SRTM tile load state; tap to open download panel |
| `LINK OK` / `LINK WARN` / `NO LINK` | green → amber → red | Health of the SSE stream from the Pico W (based on last-message age) |

![No link — full failure-flag overlay with red X across every instrument](../iphone_display/previews/preview_no_link.png)

When the SSE stream stops, the entire PFD goes to **NO LINK** state: red X across the AI, tapes, heading, and the `LINK` badge turns red. Tapes freeze at their last values. This is the correct in-flight failure mode for the phone display — every instrument is clearly invalid, the pilot knows to revert to steam gauges or the aircraft's primary PFD.

## 10. Baro Setting (QNH)

Tap the `QNH` badge at the top to open the altimeter panel.

![Baro panel — hPa units with ±1 / ±0.1 steppers](../iphone_display/previews/preview_panel_baro_hpa.png)

| Control | Effect |
|---------|--------|
| **−1 hPa** / **+1 hPa** | Coarse step (useful when you know the current altimeter setting) |
| **−0.1** / **+0.1** | Fine step |
| **STD 1013** | One-tap reset to standard pressure (1013 hPa / 29.92 inHg) |
| **⊕ GPS Alt** | Solves QNH from GPS altitude — useful on the ground to set QNH without an ATIS |
| **Manual alt** | Type a known field elevation and tap **Set Alt** — the Pico back-solves QNH via `/baro?cal_ft=`  |
| **Speed: KT / MPH** | Toggle airspeed unit |
| **QNH: hPa / inHg** | Toggle pressure unit |
| **✕ Close** | Close panel |

![Baro panel — inHg units](../iphone_display/previews/preview_panel_baro_inhg.png)

Each ± button sends an HTTP `GET /baro?qnh=X` to the Pico W. The firmware updates its internal QNH and broadcasts the new value via SSE, so the PFD reflects the change within ~50 ms. All adjustments survive Pico reboots (the firmware stores QNH in its settings).

## 11. AHRS Trim

If the horizon isn't level on the ground with the aircraft wings level, open the TRIM panel to add a mounting-correction offset.

![Trim panel — three axes with ±0.5° and ±0.1° steppers](../iphone_display/previews/preview_panel_trim.png)

Pitch, roll, and heading each have **−0.5°**, **−0.1°**, **+0.1°**, **+0.5°** buttons. Values are pushed to the Pico W and applied to the raw sensor output before the PFD sees them, so the same trim persists across all displays (iPhone, Pi Zero, Pi 4) connected to the same Pico.

**Reset All** zeroes all three axes. The panel also provides a current-value readout on the right of each row.

## 12. Terrain Download

Tap the `TERRAIN` badge to open the tile-download panel.

![Terrain panel — Coarse global vs Regional detail](../iphone_display/previews/preview_panel_terrain.png)

The iPhone version has a simplified two-button interface:

| Button | Effect |
|--------|--------|
| **⬇ Coarse global** (~10 MB) | Low-resolution global SRTM coverage — enough for the ground-clearance colour bands anywhere in the world |
| **⬇ Regional detail** (~3 MB) | Higher-resolution tiles around your current GPS position |
| **✕ Close** | Close the panel |

Progress bar and tile count appear during download. Tiles persist in the Pico W's flash so you don't re-download on every boot.

> **For more granular region selection** (US Southwest, US Pacific, All CONUS, All Europe, etc.) use the Pi 4 display — it has a 9-region grid with per-region size estimates.

## 13. Demo Mode

Open `http://192.168.4.1/preview.html` (or copy `iphone_display/preview.html` to the Pico W and navigate there) to run a self-contained simulator. It cycles through six scenarios on a 5-6 s loop:

1. Level cruise heading south (Alps ahead)
2. Standard-rate right turn
3. Climbing left turn +800 fpm
4. Steep bank 45° (slip demo)
5. Descent into valley -700 fpm
6. Turbulence with random slip

The simulator uses the exact same rendering code as the live display, so it's a faithful preview / training tool. No AHRS, no GPS, no baro sensor needed. Position drifts slowly along track so the terrain-awareness colour bands update as if you were flying.

## 14. Feature Differences vs Pi 4 / Pi Zero

The iPhone display was the original product and still covers the core instrument cluster. Several features that live on the Pi displays are not yet on the iPhone:

| Feature | iPhone | Pi Zero | Pi 4 |
|---------|:------:|:-------:|:----:|
| Horizon + pitch ladder + roll arc + slip ball | ✓ | ✓ | ✓ |
| Airspeed / altitude / heading tapes | ✓ | ✓ | ✓ |
| QNH adjust with Pico push | ✓ | ✓ | ✓ |
| AHRS trim adjust with Pico push | ✓ | ✓ | ✓ |
| Terrain-aware ground colour bands | ✓ | ✓ | ✓ |
| SRTM tile download (in-app) | ✓ (2 presets) | ✓ (9 regions) | ✓ (9 regions) |
| Settings persistence | localStorage | `data/settings.json` | `data/settings.json` |
| Unit toggles (KT↔MPH, hPa↔inHg) | ✓ | ✓ | ✓ |
| Demo mode | ✓ (preview.html) | ✓ | ✓ |
| Veeder-Root rolling drum (smooth 10/100/1000 crossings) | ✗ — plain scrolling | ✓ | ✓ |
| Speed / altitude / heading bug chevrons | ✗ | ✓ | ✓ |
| Numpad / keyboard for bug + field entry | ✗ | ✓ | ✓ |
| V-speed colour arcs (white / green / yellow / VNE) | ✗ | ✓ | ✓ |
| Setup menus (Flight Profile / Display / AHRS / Connectivity / System) | ✗ | ✓ | ✓ |
| TAWS CAUTION / PULL-UP banners | ✗ | ✓ | ✓ |
| Airport / runway / obstacle overlays | ✗ | ✓ | ✓ |
| Full flight simulator with scenario selection | ✗ (preview.html only) | ✓ | ✓ |
| 3D SVT terrain mesh | ✗ | ✗ | ✓ |

The short-term port list (bucket A) for the iPhone: V-speed colour arcs, speed / altitude / heading bug chevrons (with Pico-side endpoints for bug values), and unit-aware numpad entry for bugs. See `Docs/BUGS_AND_TODO.md` for the current prioritisation.

---

*This document covers the iPhone / browser PFD. For the dedicated Pi displays see USER_MANUAL_ZERO.md or USER_MANUAL_PI4.md.*
