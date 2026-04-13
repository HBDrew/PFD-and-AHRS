# Display Unit (Pi 4) — High-Level Requirements

| Field          | Value                                         |
|----------------|-----------------------------------------------|
| Document No.   | HLR-DISP-PI4-001                              |
| Title          | Display Unit (Pi 4) — High-Level Requirements |
| Project        | Pico-AHRS / PFD                               |
| Date           | 2026-04-13                                    |
| Version        | 0.2                                           |

---

## 1. Overview

The Pi 4 display unit is the high-performance variant of the pilot-facing PFD. It runs on a Raspberry Pi 4 (2 GB) and renders a full Primary Flight Display with true Synthetic Vision Terrain (SVT) using OpenGL ES vector graphics. The SVT renderer uses a 3D perspective projection so that terrain features — including mountain peaks and ridges above the aircraft's altitude — are visible above the horizon line. The display unit receives flight-state data from the AHRS unit over a Wi-Fi SSE stream and provides the same touch-based interface, menus, simulator, and demo mode as the Pi Zero 2W variant. The Pi 4's VideoCore VI GPU and additional RAM allow sustained 30 fps rendering of the full SVT terrain mesh, vector-drawn instruments, and anti-aliased graphics.

For the lightweight variant without SVT, see HLR-DISP-ZERO-001.

---

## 2. Hardware Platform

> **REQ-DISP-PI4-HW-001** The processor shall be a Raspberry Pi 4 with a minimum of 2 GB RAM.

> **REQ-DISP-PI4-HW-002** The display hardware is TBD. The software shall support configurable display resolution with a minimum of 640×480 pixels.

> **REQ-DISP-PI4-HW-003** The touchscreen shall support at minimum 2-point simultaneous touch detection to allow two-finger gestures for menu access.

> **REQ-DISP-PI4-HW-004** The display unit shall be powered from an aircraft 5 V supply (3 A minimum for Pi 4).

> **REQ-DISP-PI4-HW-005** The GPU memory allocation shall be set to a minimum of 256 MB to support OpenGL ES terrain rendering.

---

## 3. Rendering Performance

> **REQ-DISP-PI4-REND-001** The PFD shall render at a sustained minimum of 30 frames per second under all normal operating conditions including full SVT terrain rendering.

> **REQ-DISP-PI4-REND-002** PFD instruments (airspeed tape, altitude tape, heading tape, pitch ladder, roll arc, and aircraft symbol) shall be rendered as vector geometry using OpenGL ES, not as raster bitmaps.

> **REQ-DISP-PI4-REND-003** The renderer shall use OpenGL ES 2.0 at minimum, with OpenGL ES 3.0 preferred. Rendering shall use EGL direct rendering without requiring an X11 or Wayland compositor.

> **REQ-DISP-PI4-REND-004** Anti-aliasing shall be applied via multisample anti-aliasing (MSAA) at 4× minimum to ensure smooth edges on vector instruments and terrain geometry.

> **REQ-DISP-PI4-REND-005** An IIR low-pass smoothing filter with coefficient α = 0.25 per frame shall be applied to the attitude, altitude, airspeed, and vertical speed values before rendering.

---

## 4. Data Stale Detection

> **REQ-DISP-PI4-STALE-001** If no valid SSE event is received from the AHRS unit within 3 seconds, the display shall treat all received data as stale.

> **REQ-DISP-PI4-STALE-002** While data is stale, the display shall show a `NO LINK` status badge and shall set `ahrs_ok = false` internally.

---

## 5. Airspeed Tape

> **REQ-DISP-PI4-SPD-001** The airspeed tape shall display a Veeder-Root style drum readout centred on the current airspeed value, with smooth continuous digit roll as speed changes.

> **REQ-DISP-PI4-SPD-002** V-speed colour arcs shall be drawn on the right edge of the airspeed tape: white (VS0–VFE), green (VS1–VNO), yellow (VNO–VNE), and a red radial line at VNE.

> **REQ-DISP-PI4-SPD-003** The drum numerals shall change colour based on airspeed: white below VNO, yellow above VNO, red above VNE.

> **REQ-DISP-PI4-SPD-004** A speed bug chevron shall be rendered at the currently set speed bug value.

> **REQ-DISP-PI4-SPD-005** The speed bug chevron and readout button shall be MAGENTA when the source is GPS groundspeed, and CYAN when the source is an IAS sensor.

> **REQ-DISP-PI4-SPD-006** The pilot shall be able to set the speed bug via numpad entry by tapping the readout button.

> **REQ-DISP-PI4-SPD-007** The airspeed source shall be selectable from the AHRS / Sensors sub-menu.

---

## 6. Altitude Tape and VSI

> **REQ-DISP-PI4-ALT-001** The altitude tape shall display a Veeder-Root drum readout advancing in 50 ft increments.

> **REQ-DISP-PI4-ALT-002** An altitude bug chevron shall be rendered at the currently set target altitude.

> **REQ-DISP-PI4-ALT-003** The altitude bug and readout button shall be CYAN when barometric, MAGENTA when GPS-derived.

> **REQ-DISP-PI4-ALT-004** The baro setting button shall display the current QNH in CYAN when the barometric sensor is active, or `GPS ALT` in MAGENTA when absent.

> **REQ-DISP-PI4-ALT-005** The pilot shall be able to enter a QNH baro setting via numpad in either inHg or hPa.

> **REQ-DISP-PI4-ALT-006** The altitude bug shall be settable by tapping the readout button or by tapping directly on the altitude tape.

> **REQ-DISP-PI4-ALT-007** A vertical speed indicator bar shall run along the inner edge of the altitude tape, scaled to ±2000 fpm, turning amber above ±1500 fpm.

---

## 7. Attitude Indicator and Synthetic Vision

The attitude indicator on the Pi 4 variant provides full 3D Synthetic Vision Terrain rendering, including terrain features visible above the horizon line.

> **REQ-DISP-PI4-AI-001** The SVT terrain background shall be rendered using a 3D perspective projection from the aircraft's current position (latitude, longitude, altitude) looking along the current heading vector.

> **REQ-DISP-PI4-AI-002** Terrain features whose elevation exceeds the aircraft's current altitude shall be visible above the horizon line as mountain peaks and ridges, rendered in correct geometric perspective.

> **REQ-DISP-PI4-AI-003** The terrain mesh shall be constructed from SRTM elevation data within a configurable radius of the aircraft position, with a default radius of 5 nautical miles.

> **REQ-DISP-PI4-AI-004** The terrain mesh shall update at a minimum rate of 10 Hz to ensure smooth visual tracking during turns and altitude changes.

> **REQ-DISP-PI4-AI-005** Terrain shall be coloured by clearance relative to the aircraft altitude: terrain within 100 ft below shall be red, terrain within 500 ft below shall be yellow/amber, and terrain more than 500 ft below shall use brown earth tones darkening with increasing clearance.

> **REQ-DISP-PI4-AI-006** When SRTM terrain tiles are not present, a plain horizon shall be displayed using a solid blue upper half and solid brown lower half.

> **REQ-DISP-PI4-AI-007** A sky gradient background shall be rendered behind the terrain mesh, darker at zenith and lighter near the horizon.

> **REQ-DISP-PI4-AI-008** Pitch ladder lines shall be drawn at ±5°, ±10°, ±15°, ±20°, and ±30° from the horizon, consistent with GI-275 styling.

> **REQ-DISP-PI4-AI-009** A roll arc shall be rendered with a moving doghouse bank angle pointer and fixed wings-level reference mark.

> **REQ-DISP-PI4-AI-010** A slip/skid ball indicator shall deflect laterally in proportion to lateral acceleration (`ay`).

> **REQ-DISP-PI4-AI-011** A fixed amber delta-wing aircraft symbol shall be rendered at the centre of the attitude indicator.

---

## 8. Heading Tape and Source Modes

> **REQ-DISP-PI4-HDG-001** A heading tape shall scroll horizontally across the bottom of the display with cardinal and intercardinal labels.

> **REQ-DISP-PI4-HDG-002** A central heading box shall display the current heading as a 3-digit value with degree symbol.

> **REQ-DISP-PI4-HDG-003** A subscript `M` (magnetometer) or `G` (GPS track) shall appear in the heading box.

> **REQ-DISP-PI4-HDG-004** In GPS TRK mode, the heading box border shall be MAGENTA.

> **REQ-DISP-PI4-HDG-005** GPS TRK mode shall slave the heading to GPS ground track using a complementary filter (K = 0.05 default).

> **REQ-DISP-PI4-HDG-006** A heading bug shall be rendered on the tape, colour-coded by heading source (CYAN for MAG, MAGENTA for GPS TRK).

> **REQ-DISP-PI4-HDG-007** The heading bug shall be settable via numpad or by tapping the heading tape.

> **REQ-DISP-PI4-HDG-008** A CYAN GPS ground track tick mark shall be rendered on the heading tape when in MAG mode with valid GPS fix.

---

## 9. Terrain and Obstacle Proximity Alerting

> **REQ-DISP-PI4-TAWS-001** A TERRAIN CAUTION banner (amber, steady) shall be displayed when terrain or an obstacle is within 500 ft below aircraft altitude.

> **REQ-DISP-PI4-TAWS-002** A PULL UP / TERRAIN banner (red, 1 Hz flash) shall be displayed when terrain or an obstacle is within 100 ft below aircraft altitude.

> **REQ-DISP-PI4-TAWS-003** Obstacle proximity alerting shall activate within 3 nautical miles of the aircraft.

> **REQ-DISP-PI4-TAWS-004** SRTM terrain tiles shall be downloadable from within the PFD user interface.

> **REQ-DISP-PI4-TAWS-005** FAA Digital Obstacle File data shall be downloadable from within the PFD user interface, with a 28-day expiry indication.

---

## 10. Status Badges

> **REQ-DISP-PI4-BADGE-001** Status badges shall appear only when a condition requires pilot attention.

> **REQ-DISP-PI4-BADGE-002** Required badges: `AHRS FAIL` (red), `NO LINK` (red), `NO TER` (amber), `NO OBS` (amber), `EXP OBS` (orange), `GPS TRK` (magenta), `GPS ALT` (amber), `GPS Nsat` (amber), `NO GPS` (red).

---

## 11. Colour Coding — Data Source Convention

> **REQ-DISP-PI4-COLOR-001** GPS-derived values and controls shall be MAGENTA.

> **REQ-DISP-PI4-COLOR-002** Onboard-sensor-derived values and controls shall be CYAN.

> **REQ-DISP-PI4-COLOR-003** The colour convention shall apply to: speed bug/button, altitude bug/button, baro button, heading bug/button, heading box border, and heading source subscript.

---

## 12. Setup and Configuration

> **REQ-DISP-PI4-SETUP-001** The setup menu shall be opened by a two-finger press-and-hold for at least 0.8 seconds.

> **REQ-DISP-PI4-SETUP-002** Sub-menus: Flight Profile, Display, AHRS / Sensors, Connectivity, System.

> **REQ-DISP-PI4-SETUP-003** Factory default V-speeds: VS0=48, VS1=55, VFE=85, VNO=129, VNE=163 (Cessna 172S).

> **REQ-DISP-PI4-SETUP-004** Display units independently selectable for speed (kt/mph/kph), altitude (ft/m), pressure (inHg/hPa).

> **REQ-DISP-PI4-SETUP-005** Backlight brightness adjustable in 10 steps.

---

## 13. Flight Simulator

> **REQ-DISP-PI4-SIM-001** The display shall include a built-in flight simulator driving all PFD instruments through an internal autopilot model.

> **REQ-DISP-PI4-SIM-002** 12 preset departure airports across the US.

> **REQ-DISP-PI4-SIM-003** Failure injection: GPS, baro, and AHRS failures independently toggleable.

> **REQ-DISP-PI4-SIM-004** A `SIM` watermark shall be displayed during simulator operation; tapping it opens SIM CONTROLS.

> **REQ-DISP-PI4-SIM-005** The autopilot model shall respond to heading, altitude, and speed bug changes in real time.

---

## 14. Demo Mode

> **REQ-DISP-PI4-DEMO-001** A scripted demo mode shall animate a flight over Sedona, Arizona, driving all instruments without hardware.

> **REQ-DISP-PI4-DEMO-002** Demo mode shall be launchable with the `--demo` command-line flag.

---

## 15. Future Planned Features

The following features are planned for future versions of the Pi 4 display and are not required for the initial release:

- Synthetic runway rendering using airport database coordinates
- Texture-mapped terrain with satellite imagery or elevation-derived texturing
- Moving map underlay (planned for separate dedicated hardware)
- Flight path vector (velocity vector symbol on the AI)
- Highway-in-the-sky (HITS) waypoint tunnel rendering

---

*This document covers the Pi 4 variant with full SVT. For the Pi Zero 2W variant without SVT, see HLR-DISP-ZERO-001.*
