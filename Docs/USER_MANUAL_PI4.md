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

The Pi 4 version renders a **full 3D Synthetic Vision Terrain (SVT)** background behind the attitude indicator. When SRTM terrain tiles are loaded, the AI background shows real terrain rendered in true 3D perspective from the aircraft's position.

**Key capability:** Unlike 2D scanline renderers, the Pi 4's OpenGL-based SVT shows terrain features that are **above the aircraft's altitude** — mountain peaks and ridges are visible **above the horizon line** in correct geometric perspective. This gives the pilot a natural, out-the-window view of the terrain environment.

Terrain colouring indicates clearance:
- **Red** — terrain within **100 ft** below aircraft altitude (immediate proximity)
- **Yellow/amber** — terrain within **500 ft** below aircraft altitude (caution)
- **Brown earth tones** — terrain more than 500 ft below, darkening with distance

When no terrain data is available the background is the traditional blue-over-brown split.

### Pitch ladder

Grey pitch bars at ±5°, ±10°, ±15°, ±20°, ±30°. GI-275 style. Horizon bar is white.

### Roll arc and pointer

Graduated arc at the top of the AI. Moving doghouse tracks bank angle. Fixed wings-level reference. Ticks at 10°, 20°, 30°, 45°, 60°.

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
| `GPS TRK` | Magenta | GPS TRK heading mode active |
| `GPS ALT` | Amber | Altitude from GPS (baro failed) |
| `GPS` *N*`sat` | Amber | GPS acquiring — *N* satellites |
| `NO GPS` | Red | No GPS signal |

---

## 7. Setting Bugs

Three bugs — altitude, heading, ground-speed.

### Numpad entry

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

Two-finger hold 0.8 s → six tiles: FLIGHT PROFILE, DISPLAY, AHRS / SENSORS, CONNECTIVITY, SYSTEM, EXIT.

---

## 9. Flight Profile — V-Speeds and Callsign

Cessna 172S defaults: VS0=48, VS1=55, VFE=85, VNO=129, VNE=163, VA=105, VY=74, VX=62 kt.

Tap any V-speed box to change. RESET DEFAULTS restores all. Tap CALLSIGN to enter tail number via keyboard.

---

## 10. Display Settings

**Speed:** KT / MPH / KPH. **Altitude:** FT / M. **Pressure:** inHg / hPa. **Brightness:** 1–10 steps.

---

## 11. AHRS / Sensors

**Pitch/Roll trim:** ±0.5° steps. **Mounting:** NORMAL / INVERTED. **Heading source:** MAG / GPS TRK. **Airspeed source:** GPS GS / IAS SENSOR (future).

---

## 12. Connectivity

AHRS URL (default `http://192.168.4.1`), WiFi SSID/password, TEST AHRS, APPLY WIFI.

---

## 13. System

Version info, terrain/obstacle status, DIAGNOSTICS (future), RESET DEFAULTS, FLIGHT SIMULATOR.

---

## 14. Terrain Data Download

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

FAA DOF adds tower/antenna/wind-turbine symbols. Within 10 nm and ±2000 ft.

Tap DOWNLOAD to fetch. Symbols colour-coded:

| Colour | Meaning |
|--------|---------|
| Red | Within 100 ft below |
| Amber | Within 500 ft below |
| Green | Cleared > 500 ft |

Red dot = lit obstacle. 28-day update cycle.

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
