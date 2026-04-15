# AHRS PFD — Pi 4 Pilot's User Manual

**Software version 0.2 · Hardware: Raspberry Pi Pico W + Pi 4 (2 GB) · Display: ROADOM 7" HDMI 1024×600 (or Waveshare 3.5" DPI 640×480)**

*Full SVT version — OpenGL vector graphics with 3D terrain rendering*

> This manual covers the Pi 4 version with full SVT. For the Pi Zero 2W version (no SVT), see USER_MANUAL_ZERO.md.

---

## Contents

1. [Screen Overview](#1-screen-overview)
2. [Airspeed Tape](#2-airspeed-tape)
3. [Altitude Tape and VSI](#3-altitude-tape-and-vsi)
4. [Attitude Indicator](#4-attitude-indicator)
5. [Heading Tape](#5-heading-tape)
6. [Status Badges](#6-status-badges)
7. [Setting Bugs](#7-setting-bugs)
8. [Setup Menu](#8-setup-menu)
9. [Flight Profile — V-Speeds and Callsign](#9-flight-profile--v-speeds-and-callsign)
10. [Display Settings](#10-display-settings)
11. [AHRS / Sensors](#11-ahrs--sensors)
12. [Connectivity](#12-connectivity)
13. [System](#13-system)
14. [Terrain Data Download](#14-terrain-data-download)
15. [Obstacle Data Download](#15-obstacle-data-download)
16. [Demo Mode](#16-demo-mode)
17. [Flight Simulator](#17-flight-simulator)

---

## 1. Screen Overview

The Pi 4 version supports two display options:

| Display | Resolution | Interface | Size |
|---------|-----------|-----------|------|
| ROADOM 7" HDMI (default) | 1024×600 | HDMI + USB touch | 7" IPS, 178° viewing |
| ROADOM 10" HDMI | 1024×600 | HDMI + USB touch | 10" IPS, 178° viewing |
| Waveshare 3.5" DPI | 640×480 | DPI GPIO + I2C touch | 3.5" IPS, panel mount |

All layout elements scale automatically to the display resolution. To switch displays, edit `DISPLAY_PROFILE` in `pi4/config.py`.

The display is divided into five fixed zones (sizes shown for 1024×600 default):

| Zone | Width / Height | Content |
|------|---------------|---------|
| Left tape | 118 px wide | Airspeed |
| Right tape | 131 px wide | Altitude + VSI |
| Centre AI | remainder (~775 px) | Attitude + synthetic vision terrain |
| Bottom strip | 55 px tall | Heading tape |
| Top strip | 28 px tall | Bug readouts |

Everything is rendered at 30 fps using OpenGL ES vector graphics directly on the framebuffer.

---

## 2. Airspeed Tape

### Reading the tape

The tape scrolls so that current airspeed is always at the centred Veeder-Root drum readout. The drum shows two-digit resolution; the tenths digit rolls smoothly.

### Colour arcs (right edge of tape)

| Arc | Colour | Meaning |
|-----|--------|---------|
| White | White | VS0 – VFE — flap operating range |
| Green | Green | VS1 – VNO — normal operating range |
| Yellow | Yellow | VNO – VNE — caution / structural |
| Red line | Red | VNE — never-exceed |

The drum numerals turn **yellow** above VNO and **red** above VNE.

### Speed bug

A chevron marker tracks the speed bug. Tap the readout button at the **top** of the tape to set a value.

| Colour | Source |
|--------|--------|
| Magenta | GPS groundspeed (GS) — current default |
| Cyan | IAS sensor — when pitot/static is fitted |

---

## 3. Altitude Tape and VSI

![Descending final approach](../pi4/previews/preview_sedona_approach.png)

### Altitude tape

Veeder-Root drum on the right side. Scrolls in 50 ft increments. **Altitude bug** settable via numpad (entry in hundreds of feet) or by tapping the tape.

| Colour | Source |
|--------|--------|
| Cyan | Barometric altitude (BME280 active) |
| Magenta | GPS altitude (baro failed or absent) |

### Baro setting

| Display | Colour | Meaning |
|---------|--------|---------|
| `29.92 IN` | Cyan | BME280 active; inHg |
| `1013 hPa` | Cyan | BME280 active; hPa |
| `GPS ALT` | Magenta | No baro sensor |

### VSI

Green/amber bar along the inner edge of the alt tape. ±2000 fpm scale. Amber above ±1500 fpm.

---

## 4. Attitude Indicator

### Synthetic vision background

The Pi 4 renders a **full 3D Synthetic Vision Terrain (SVT)** background behind the attitude indicator using OpenGL ES. A terrain mesh is built from SRTM elevation data within a 20 nm radius of the aircraft and rendered through a true perspective projection from the aircraft's position.

**Key capability:** Unlike 2D scanline renderers, the OpenGL SVT shows terrain features that are **above the aircraft's altitude** — mountain peaks and ridges rise **above the horizon line** in correct geometric perspective. This gives the pilot a natural, out-the-window view of the terrain environment.

#### Clearance colouring

Terrain is coloured by clearance below the aircraft:

| Clearance | Colour | Meaning |
|-----------|--------|---------|
| Above aircraft altitude | Red | Terrain is higher than you — obstacle |
| 0–100 ft below | Deep orange | Immediate proximity |
| 100–500 ft below | Amber | Caution |
| 500–1000 ft below | Brown | Safe clearance |
| 1000–2000 ft below | Dark brown | Well clear |
| More than 2000 ft below | Very dark | Far below |

Beyond the 20 nm mesh edge the rendering fades to a dusty atmospheric-haze gradient that blends with distant terrain so there is no visible seam.

#### Sun-angle lighting

Terrain is shaded by a directional sun source so that slopes facing the sun appear brighter and slopes in shadow darken toward the ambient level. Ridge-lines, valleys, canyons, and mesa edges become immediately recognisable. Default sun position is 45° elevation, SE azimuth (mid-morning). This is configurable in `pi4/svt_renderer_gl.py`.

#### Distance grid

A cyan grid aligned with cardinal directions is overlaid on the terrain to help judge distance and orientation.

- Minor lines every **0.5 nm** (counts squares to estimate distance)
- Major lines every **2 nm** (slightly brighter for longer-range reference)
- Lines fade toward the mesh edge to avoid clutter at the visible horizon
- Grid colours switch automatically: light cyan-white on brown terrain, dark blue on red/orange "above aircraft" terrain for contrast

The grid doubles as a heading reference — the N/S grid line is always parallel to true north, so a glance at the grid orientation relative to the aircraft symbol gives a crude no-compass heading check.

#### Zero-pitch reference line

A pair of short cyan hash marks across the AI mark the aircraft's **0° pitch reference** in the sky frame. With 3D SVT the visible horizon may be higher or lower than the actual 0° pitch position depending on terrain (for example, mountains above your altitude push the apparent horizon up). The zero-pitch line is independent of terrain — it always shows where level flight would put the nose.

- Drops below AI centre in climbs
- Rises above AI centre in descents
- Tilts with the horizon during banks
- Aligned exactly with the pitch ladder's 0° bar

When terrain is at or below your altitude, the zero-pitch line and the visible SVT horizon coincide.

#### No terrain data

When no SRTM tiles are loaded the SVT falls back to a traditional blue-sky-over-brown-ground split. The `NO TER` badge appears in the status strip.

### Pitch ladder

Grey pitch bars at ±5°, ±10°, ±15°, ±20°, ±30°. GI-275 style. Horizon bar is white. Scale is 10 px/° (matches the SVT horizon projection exactly so the 0° bar and the terrain horizon align at all pitches).

### Roll arc and pointer

Graduated arc at the top of the AI implementing the **sky-pointer** convention. The arc and tick marks (10°, 20°, 30°, 45°, 60°) rotate with the sky so that the fixed aircraft reference at the very top of the screen reads the current bank angle on the arc. A moving doghouse outside the arc marks the sky's "up" direction; a fixed reference inside the arc at the top marks the aircraft's current bank position.

### Aircraft symbol

Amber swept-delta wing symbol fixed at AI centre.

### Slip ball

White ball, deflects laterally with lateral acceleration. Centred = coordinated flight.

### Terrain / obstacle proximity alert

**TERRAIN CAUTION** (amber, steady) — terrain or obstacle within **500 ft** below.

**PULL UP TERRAIN** (red, 1 Hz flash) — terrain or obstacle within **100 ft** below.

Requires GPS fix and SRTM tiles or obstacle data loaded.

---

## 5. Heading Tape

### Heading source modes

![Heading tape — MAG mode](../pi4/previews/preview_sedona_level.png)

**MAG mode (default):** Magnetometer heading. Dim border, `M` subscript.

**GPS TRK mode:** Heading slewed to GPS track via complementary filter. Magenta border, `G` subscript. `GPS TRK` badge appears.

### Track pointer

In MAG mode with GPS fix, a **cyan** tick shows GPS ground track (wind/crab indication).

### Heading bug

Chevron on the tape. CYAN (MAG) / MAGENTA (GPS TRK). Settable via numpad or tap on tape.

---

## 6. Status Badges

Blank during normal flight. Appear only when attention required.

| Badge | Colour | Meaning |
|-------|--------|---------|
| `AHRS FAIL` | Red | IMU data absent or invalid |
| `NO LINK` | Red | SSE stream not connected |
| `NO TER` | Amber | No SRTM terrain tiles loaded |
| `NO OBS` | Amber | No FAA obstacle data loaded |
| `EXP OBS` | Orange | Obstacle data > 28 days old |
| `NO APT` | Amber | No airport data loaded |
| `EXP APT` | Orange | Airport data older than expiry |
| `GPS TRK` | Magenta | GPS TRK heading mode active |
| `GPS ALT` | Amber | Altitude from GPS (baro failed) |
| `GPS` *N*`sat` | Amber | GPS acquiring — *N* satellites |
| `NO GPS` | Red | No GPS signal |

---

## 7. Setting Bugs

Three bugs — altitude, heading, ground-speed.

### Numpad entry

![Altitude bug numpad](../pi4/previews/preview_numpad_alt.png)

Tap the readout button. Numpad overlays the live PFD.

**Altitude** — hundreds of feet (`85` → `8500 ft`). **Heading** — 3 digits. **GS** — whole knots.

### Baro entry

**inHg**: four digits, auto-decimal after second (`2992` → `29.92`). **hPa**: plain integer.

### Tape taps

Tap heading tape → jump HDG bug. Tap altitude tape → jump alt bug (nearest 100 ft).

### Clear

Enter `0` + ENTER.

---

## 8. Setup Menu

![Main setup screen](../pi4/previews/preview_setup_main.png)

Two-finger hold 0.8 s → six tiles: FLIGHT PROFILE, DISPLAY, AHRS / SENSORS, CONNECTIVITY, SYSTEM, EXIT.

---

## 9. Flight Profile — V-Speeds and Callsign

![Flight profile screen](../pi4/previews/preview_setup_flight_profile.png)

Cessna 172S defaults: VS0=48, VS1=55, VFE=85, VNO=129, VNE=163, VA=105, VY=74, VX=62 kt.

Tap any V-speed box to change. RESET DEFAULTS restores all. Tap CALLSIGN to enter tail number via keyboard.

---

## 10. Display Settings

![Display settings screen](../pi4/previews/preview_setup_display.png)

**Speed:** KT / MPH / KPH. **Altitude:** FT / M. **Pressure:** inHg / hPa. **Brightness:** 1–10 steps.

---

## 11. AHRS / Sensors

![AHRS setup screen](../pi4/previews/preview_setup_ahrs.png)

**Pitch/Roll trim:** ±0.5° steps. **Mounting:** NORMAL / INVERTED. **Heading source:** MAG / GPS TRK. **Airspeed source:** GPS GS / IAS SENSOR (future).

---

## 12. Connectivity

![Connectivity screen](../pi4/previews/preview_setup_connectivity.png)

AHRS URL (default `http://192.168.4.1`), WiFi SSID/password, TEST AHRS, APPLY WIFI.

---

## 13. System

![System screen](../pi4/previews/preview_setup_system.png)

Version info, terrain/obstacle status, DIAGNOSTICS (future), RESET DEFAULTS, FLIGHT SIMULATOR.

All configurable settings — V-speeds, tail number, units, backlight brightness, colour scheme, heading-source mode, Wi-Fi SSID, airport display filters, and the runway/centerline overlay toggles — persist across power cycles in `pi4/data/settings.json`. The file is written atomically on a background thread with a 1.5 s debounce, so rapid successive taps produce a single write with no UI stutter. The Wi-Fi password is intentionally *not* stored — it must be re-entered when joining a new network.

---

## 14. Terrain Data Download

![Terrain idle screen](../pi4/previews/preview_terrain_idle.png)

SRTM elevation tiles provide the ground texture for the SVT background and power the TAWS proximity alerting. Without tiles the AI shows a plain blue/brown split.

Tiles stored in `pi4/data/srtm/` as `.hgt` files (~1 MB each).

### Preset regions

US Southwest, US Pacific, US Southeast, US Northeast, Alaska, Canada. Tap to download. Progress bar updates. CANCEL aborts; downloaded tiles kept.

### Current area

Downloads ±2° around GPS position. Requires fix.

### WiFi

Pi must be on internet-reachable network. Switch via Connectivity screen.

---

## 15. Obstacle Data Download

![Obstacle data screen — idle](../pi4/previews/preview_obstacle_idle.png)

FAA DOF adds tower/antenna/wind-turbine symbols. Within 10 nm and ±2000 ft.

Tap DOWNLOAD to fetch. Symbols colour-coded:

| Colour | Meaning |
|--------|---------|
| Red | Within 100 ft below |
| Amber | Within 500 ft below |
| White | Cleared > 500 ft |

Red dot = lit obstacle. 28-day update cycle.

---

## 15A. Airport Data Download

![Airport data screen — loaded](../pi4/previews/preview_airport_loaded.png)

The OurAirports.com global database adds airport and heliport symbols to the attitude indicator within 20 nm of the aircraft. The database covers approximately 72,000 airports worldwide including ~20,000 in the US.

### Symbols on the AI

| Symbol | Meaning |
|--------|---------|
| Cyan ring (filled centre) | Public airport (small / medium / large) |
| Cyan ring with outer ring | Medium or large public airport |
| Magenta "H" | Heliport |
| Cyan circle with wavy underscore | Seaplane base |
| Grey triangle | Balloonport |

The airport identifier (e.g. "KSEZ") is rendered within 15 nm as a small "road sign" — a coloured text box mounted on a thin vertical post that lifts the label clear of the symbol and any nearby terrain features. The sign border matches the symbol colour (cyan for public airports, magenta for heliports). Beyond 15 nm only the symbol is drawn to reduce clutter at distance.

### Display filters

The AIRPORT DATA screen has four type filters and two overlay toggles at the bottom. Tap any tile to toggle its state.

| Filter | Controls | Default |
|--------|----------|---------|
| **PUBLIC** | Small / medium / large public-use airports | On |
| **HELIPORTS** | Hospital helipads, rooftop pads, private helis | On |
| **SEAPLANE** | Seaplane bases (water operations) | Off |
| **OTHER** | Balloonports and uncategorised types | Off |
| **RUNWAYS** | Paved/unpaved runway polygons (within 8 nm) | On |
| **EXT CENTERLINES** | Dashed extended centerlines off each threshold (within 15 nm) | On |

This lets you declutter the AI to show only the types relevant to your flight — for example, turn off HELIPORTS when operating in dense urban airspace where helipads would swamp the display, or turn off EXT CENTERLINES en-route and only re-enable during terminal-area operations.

All filter and toggle states persist across power cycles — you don't need to re-configure on every startup.

### Runways and extended centerlines

![Runway approach — KSEZ RWY 03](../pi4/previews/pfd_gl/preview_runway_approach.png)

Within 8 nm of an airport, the PFD overlays a scaled polygon for each runway threshold-to-threshold, projected in the same perspective as the rest of the attitude indicator so runways translate, rotate, and scale naturally with the aircraft's position, bank and pitch. Width is taken from the OurAirports database.

Extended centerlines (dashed yellow) extend 10 nm outward from each runway threshold along its exact bearing, visible within 15 nm of the airport. This provides an at-a-glance final-approach reference for non-precision and visual approaches — the same kind of cue you get from a flight director's course bar, but derived purely from the runway geometry rather than a flight-plan waypoint.

Runway data comes from OurAirports `runways.csv` (approximately 14,700 runways worldwide) and is downloaded alongside the airport CSV in a single UPDATE action.

### Downloading

Tap **AIRPORTS** on the System screen to open the airport data screen.

Tap **DOWNLOAD** (or **UPDATE** if data is already present) to fetch `airports.csv` (~12 MB) plus `runways.csv` (~3 MB) from the OurAirports GitHub mirror.

![Airport data screen — downloading](../pi4/previews/preview_airport_downloading.png)

A progress bar updates as the download runs. After completion the CSV is parsed into a NumPy cache for fast future loads. Tap **CANCEL** to abort at any time — no partial data is kept.

### Update schedule

The OurAirports database is community-maintained and updated frequently. A 60-day local expiry is enforced; after that the `EXP APT` badge appears in the status strip to remind the pilot to refresh. The data is usable past expiry — the badge is an advisory only.

### WiFi requirement

The Pi 4 must be on an internet-reachable network to download. Use the Connectivity screen to switch to home Wi-Fi, download here, then switch back to the Pico W AP for flight.

---

## 16. Demo Mode

Scripted Sedona, AZ flight. No hardware needed.

```bash
python3 pi4/pfd.py --demo
python3 pi4/pfd.py --demo --sim   # windowed
```

Cycles: level cruise → climbing left turn → level → descending right turn. SVT terrain renders if Sedona tiles are present.

---

## 17. Flight Simulator

Autopilot holds heading, altitude, speed bugs. 12 US airport presets. Failure injection (GPS/BARO/AHRS). Tap `SIM` watermark for controls.

---

## Quick-Reference Card

| Action | How |
|--------|-----|
| Open setup | Two-finger hold 0.8 s |
| Close setup | Tap EXIT |
| Set alt bug | Tap top of alt tape → numpad |
| Set HDG bug | Tap bottom-left of heading strip → numpad |
| Set GS bug | Tap top of speed tape → numpad |
| Tap alt tape | Jumps alt bug |
| Tap heading tape | Jumps HDG bug |
| Adjust baro | Tap bottom-right → numpad |
| Brightness | Setup → Display → − / + |
| Start sim | Setup → System → FLIGHT SIMULATOR → START |
| SIM controls | Tap SIM watermark |
| Exit sim | SIM controls → EXIT SIM |

---

*This document covers the Pi 4 version with full SVT. For the Pi Zero 2W version (no SVT), see USER_MANUAL_ZERO.md.*
