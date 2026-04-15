# Display Unit (Pi Zero 2W) — High-Level Requirements

| Field          | Value                                              |
|----------------|----------------------------------------------------|
| Document No.   | HLR-DISP-ZERO-001                                  |
| Title          | Display Unit (Pi Zero 2W) — High-Level Requirements|
| Project        | Pico-AHRS / PFD                                    |
| Date           | 2026-04-13                                         |
| Version        | 0.2                                                |

---

## 1. Overview

The display unit (Pi Zero 2W variant) is the pilot-facing component of the Pico-AHRS / PFD system optimised for deployment on Raspberry Pi Zero 2W hardware. It connects to a touchscreen display (resolution TBD) and is responsible for receiving the flight-state SSE stream from the AHRS unit, rendering a full Primary Flight Display (PFD) at 30 fps directly on the framebuffer, and providing a touch-based interface for pilot configuration and bug setting. The display unit contains all rendering logic, terrain awareness alerting, configuration menus, a built-in flight simulator, and a scripted demo mode.

This variant deliberately omits Synthetic Vision Terrain (SVT) background rendering on the attitude indicator in order to remain within the GPU and memory budget of the Pi Zero 2W. The attitude indicator instead uses a plain horizon: a solid blue upper half representing sky and a solid brown lower half representing ground. SRTM elevation tile data is still consumed for Terrain Awareness and Warning System (TAWS) proximity alerting — generating TERRAIN CAUTION and PULL UP / TERRAIN banners — but that data is never used to texture or colour the attitude indicator background.

The Pi 4 variant of this display (document HLR-DISP-PI4-001) provides the full Synthetic Vision Terrain background capability. Operators requiring SVT should refer to that document.

The display unit operates independently of the AHRS unit for configuration and simulation purposes, and degrades gracefully to stale-data indication when the SSE link is interrupted.

---

## 2. Hardware Platform

The following requirements define the minimum acceptable hardware configuration for the Pi Zero 2W display unit. The display unit must be capable of sustained 30 fps rendering of full-screen PFD graphics without relying on a graphical desktop environment.

> **REQ-DISP-ZERO-HW-001** The processor shall be a Raspberry Pi Zero 2W.

> **REQ-DISP-ZERO-HW-002** The display shall be a touchscreen compatible with the Raspberry Pi Zero 2W. The specific display model and resolution are TBD; the software shall be configurable to target the installed display resolution.

> **REQ-DISP-ZERO-HW-003** The touchscreen shall support at minimum 2-point simultaneous touch detection to allow two-finger gestures for menu access.

> **REQ-DISP-ZERO-HW-004** The display unit shall be powered from an aircraft 5 V supply.

---

## 3. Rendering Performance

All PFD rendering is performed by `pi_display/pfd.py` using the pygame library writing directly to the Linux framebuffer. The requirements below govern the frame rate target, the display stack, and the signal smoothing applied to raw sensor values before they reach the rendering pipeline.

> **REQ-DISP-ZERO-REND-001** The PFD shall render at a sustained minimum of 30 frames per second under all normal operating conditions.

> **REQ-DISP-ZERO-REND-002** All rendering shall target the pygame framebuffer interface, writing to `/dev/fb0` or an equivalent framebuffer device. No X11 or Wayland compositor shall be required for the display to operate.

> **REQ-DISP-ZERO-REND-003** An IIR low-pass smoothing filter with coefficient α = 0.25 per frame shall be applied to the attitude (roll, pitch, yaw), altitude, airspeed, and vertical speed values before they are passed to the rendering routines, in order to eliminate jitter caused by sensor noise.

---

## 4. Data Stale Detection

The display unit must detect and clearly indicate to the pilot when the SSE data link to the AHRS unit has been interrupted. The requirements below define the timeout threshold and the required indication behaviour.

> **REQ-DISP-ZERO-STALE-001** If no valid SSE event is received from the AHRS unit within 3 seconds, the display shall treat all received data as stale.

> **REQ-DISP-ZERO-STALE-002** While data is stale, the display shall show a `NO LINK` status badge and shall set `ahrs_ok = false` internally, causing the AHRS FAIL indication to activate on the relevant instruments.

---

## 5. Airspeed Tape

The airspeed tape occupies the left side of the PFD. It presents the current speed value using a Veeder-Root drum readout and provides visual encoding of V-speed thresholds through colour arcs and numeral colouring. The requirements below govern the tape rendering, V-speed arcs, numeral colouring, speed bug behaviour, and source selection.

> **REQ-DISP-ZERO-SPD-001** The airspeed tape shall display a Veeder-Root style drum readout centred on the current airspeed value, with smooth continuous digit roll as speed changes.

> **REQ-DISP-ZERO-SPD-002** V-speed colour arcs shall be drawn on the right edge of the airspeed tape as follows: a white arc spanning the flap operating speed range (VS0 to VFE), a green arc spanning the normal operating speed range (VS1 to VNO), a yellow arc spanning the caution speed range (VNO to VNE), and a red radial line at VNE.

> **REQ-DISP-ZERO-SPD-003** The drum numerals shall change colour based on the current airspeed: white when below VNO, yellow when above VNO, and red when above VNE.

> **REQ-DISP-ZERO-SPD-004** A speed bug chevron shall be rendered on the airspeed tape at the position corresponding to the currently set speed bug value.

> **REQ-DISP-ZERO-SPD-005** The speed bug chevron and the speed readout button at the top of the tape shall be displayed in MAGENTA when the airspeed source is GPS groundspeed, and in CYAN when the airspeed source is an indicated airspeed (IAS) sensor.

> **REQ-DISP-ZERO-SPD-006** The pilot shall be able to set the speed bug value by tapping the speed readout button at the top of the tape, which shall open a numpad entry overlay for direct numeric input.

> **REQ-DISP-ZERO-SPD-007** The airspeed source — GPS groundspeed or IAS sensor — shall be selectable from within the AHRS / Sensors sub-menu of the setup interface.

---

## 6. Altitude Tape and VSI

The altitude tape occupies the right side of the PFD. It presents the current altitude using a Veeder-Root drum readout, provides an altitude bug, and includes an integrated vertical speed indicator bar. The requirements below govern the tape rendering, the drum increment, the altitude bug, the baro setting button, QNH entry, and the VSI bar.

> **REQ-DISP-ZERO-ALT-001** The altitude tape shall display a Veeder-Root drum readout with smooth continuous digit roll, advancing in 50 ft increments, centred on the current altitude value.

> **REQ-DISP-ZERO-ALT-002** An altitude bug chevron shall be rendered on the altitude tape at the position corresponding to the currently set target altitude.

> **REQ-DISP-ZERO-ALT-003** The altitude bug chevron and the altitude readout button shall be displayed in CYAN when the altitude value is derived from the barometric sensor, and in MAGENTA when the altitude value is derived from GPS because the barometric sensor has failed or is absent.

> **REQ-DISP-ZERO-ALT-004** The baro setting button shall display the current QNH setting in CYAN — formatted as `29.92 IN` or `1013 hPa` depending on the selected unit — when the barometric sensor is active and providing valid data. When the barometric sensor is absent or invalid, the button shall display `GPS ALT` in MAGENTA.

> **REQ-DISP-ZERO-ALT-005** The pilot shall be able to enter a QNH baro setting by tapping the baro setting button, which shall open a numpad entry overlay. Entry shall be accepted in either inches of mercury (four digits with an automatic decimal point inserted after the second digit) or hectopascals (four-digit integer), according to the currently selected pressure unit.

> **REQ-DISP-ZERO-ALT-006** The altitude bug shall be settable by tapping the altitude readout button and entering a value in hundreds of feet via the numpad overlay, or by tapping directly on the altitude tape at the desired target altitude position.

> **REQ-DISP-ZERO-ALT-007** A vertical speed indicator bar shall run along the inner edge of the altitude tape, scaled to ±2000 fpm full deflection. The bar shall turn amber in colour when the magnitude of the vertical speed exceeds ±1500 fpm.

---

## 7. Attitude Indicator

The attitude indicator (AI) occupies the centre of the PFD. It renders the aircraft's roll and pitch attitude against a plain horizon background, and includes a pitch ladder, roll arc, slip/skid ball, and fixed aircraft symbol. Synthetic Vision Terrain (SVT) background rendering is not provided in this variant; SRTM tile data is used solely for TAWS proximity alerting as described in Section 9.

> **REQ-DISP-ZERO-AI-001** The AI background SHALL use a plain horizon at all times: the upper half of the attitude sphere shall be rendered as a solid blue representing sky, and the lower half shall be rendered as a solid brown representing ground, divided by the horizon line. The display SHALL NOT render a synthetic vision terrain background on the attitude indicator regardless of whether SRTM terrain tiles are present on storage. SRTM tiles, when present, are used exclusively for TAWS proximity alerting (Section 9) and shall have no effect on the AI background appearance.

> **REQ-DISP-ZERO-AI-002** Pitch ladder lines shall be drawn at ±5°, ±10°, ±15°, ±20°, and ±30° from the horizon, styled consistent with the Garmin GI-275 pitch ladder convention, with longer lines at the larger pitch angles and text labels at ±10°, ±20°, and ±30°.

> **REQ-DISP-ZERO-AI-003** A roll arc shall be rendered at the top of the attitude indicator with a moving doghouse-style bank angle pointer that tracks the current roll angle, and a fixed wings-level reference mark at the 0° roll position.

> **REQ-DISP-ZERO-AI-004** Terrain proximity colouring SHALL NOT be applied to the AI background. Terrain proximity conditions are communicated to the pilot exclusively through the TAWS banner system defined in Section 9 (TERRAIN CAUTION and PULL UP / TERRAIN banners). No tinting or colouring of the attitude indicator background based on terrain proximity is performed in this variant.

> **REQ-DISP-ZERO-AI-005** A slip/skid ball indicator shall be displayed below the roll arc, deflecting laterally in proportion to the lateral acceleration value (`ay`) received from the AHRS unit.

> **REQ-DISP-ZERO-AI-006** A fixed amber delta-wing aircraft symbol shall be rendered at the centre of the attitude indicator to serve as the pitch and roll reference for the pilot.

---

## 8. Heading Tape and Source Modes

The heading tape runs across the bottom of the PFD and displays the current magnetic heading or GPS ground track. It supports a heading bug, a GPS track pointer, and two selectable heading source modes. The requirements below govern the tape layout, the heading box, source mode indication, the heading bug, and the GPS track pointer.

> **REQ-DISP-ZERO-HDG-001** A heading tape shall be rendered horizontally across the bottom of the display, showing compass cardinal and intercardinal labels that scroll continuously as the aircraft turns.

> **REQ-DISP-ZERO-HDG-002** A central heading box shall be superimposed over the heading tape and shall display the current heading as a 3-digit numeric value followed by a degree symbol.

> **REQ-DISP-ZERO-HDG-003** A subscript letter shall appear to the lower-right of the degree symbol in the heading box: `M` when the heading source is the magnetometer, and `G` when the heading source is GPS ground track.

> **REQ-DISP-ZERO-HDG-004** In MAG mode, the heading box border shall be rendered in a white or dim neutral colour. In GPS TRK mode, the heading box border shall be rendered in MAGENTA to indicate that the heading is GPS-derived.

> **REQ-DISP-ZERO-HDG-005** A GPS TRK heading source mode shall be selectable from within the AHRS / Sensors sub-menu. When GPS TRK mode is active, the display shall continuously slave the gyro-derived heading to GPS ground track using a complementary filter with a default blending coefficient of K = 0.05.

> **REQ-DISP-ZERO-HDG-006** A heading bug shall be rendered on the heading tape at the position corresponding to the currently set target heading. The heading bug and its associated readout button shall be displayed in CYAN when the heading source is MAG, and in MAGENTA when the heading source is GPS TRK.

> **REQ-DISP-ZERO-HDG-007** The pilot shall be able to set the heading bug by tapping the heading readout button and entering a value via the numpad overlay, or by tapping directly on the heading tape at the desired heading position.

> **REQ-DISP-ZERO-HDG-008** When GPS fix is valid and the active heading source is MAG, a CYAN tick mark representing the GPS ground track shall be rendered on the heading tape at the current GPS track angle, providing a wind-correction-angle reference.

---

## 9. Terrain and Obstacle Proximity Alerting

The display unit implements a basic terrain awareness and warning function using SRTM elevation tiles and FAA Digital Obstacle File data. In this Pi Zero 2W variant, SRTM data is used exclusively for TAWS proximity alerting and does not affect the attitude indicator background rendering. The requirements below govern the alert thresholds, alert presentation, obstacle detection range, and the in-app data download capability.

> **REQ-DISP-ZERO-TAWS-001** A TERRAIN CAUTION banner rendered in amber shall be displayed when the MSL elevation of terrain or an obstacle within the look-ahead area is within 500 ft below the current aircraft MSL altitude.

> **REQ-DISP-ZERO-TAWS-002** A PULL UP / TERRAIN banner rendered in red shall be displayed, flashing at 1 Hz, when the MSL elevation of terrain or an obstacle within the look-ahead area is within 100 ft below the current aircraft MSL altitude.

> **REQ-DISP-ZERO-TAWS-003** Obstacle proximity alerting shall activate when a charted obstacle is located within 3 nautical miles of the current aircraft position.

> **REQ-DISP-ZERO-TAWS-004** SRTM terrain elevation tiles shall be downloadable by geographic region from within the PFD user interface, without requiring the use of any external computer tools or command-line utilities.

> **REQ-DISP-ZERO-TAWS-005** FAA Digital Obstacle File data shall be downloadable from within the PFD user interface. Downloaded obstacle data shall be considered expired after 28 days from the download date, and the display shall indicate expired obstacle data via the `EXP OBS` status badge.

---

## 9A. Airport Display

The display unit shall show nearby airports on the attitude indicator to give the pilot immediate awareness of emergency-landing options and navigational references.

> **REQ-DISP-ZERO-APT-001** Airports within a configurable radius of the aircraft (default 20 nm) shall be rendered on the attitude indicator as small symbols projected onto the AI using the same pixel-per-degree scale as the pitch ladder.

> **REQ-DISP-ZERO-APT-002** Airport symbol style shall encode airport type:
>
> - Public airport (small / medium / large) — cyan ring with dark centre; outer ring added for medium and large airports
> - Heliport — magenta letter "H"
> - Seaplane base — cyan circle with a wavy underscore
> - Balloonport — grey triangle

> **REQ-DISP-ZERO-APT-003** The airport identifier (ICAO/local code) shall be rendered within a closer configurable range (default 15 nm) as a "road sign" — a dark-filled, coloured-bordered text box mounted on a thin vertical post anchored at the airport symbol. The post shall lift the sign clear of the airport symbol so the label remains legible against busy terrain. The sign border colour shall match the symbol colour. The sign shall be auto-sized to the text width plus padding and clamped to remain within the AI rectangle.

> **REQ-DISP-ZERO-APT-004** Airports outside the attitude indicator's angular field of view shall be culled so that no symbol appears clipped at the edge of the AI rectangle.

> **REQ-DISP-ZERO-APT-005** Airport symbols shall be drawn before obstacle symbols in the Z-order so that close-in towers and obstructions appear on top of airport symbols at the same screen position.

> **REQ-DISP-ZERO-APT-006** The airport database shall be the OurAirports global CSV (approximately 72,000 airports worldwide). Closed airports and records with missing coordinates shall be filtered out at parse time.

> **REQ-DISP-ZERO-APT-007** The airport database shall be downloadable from within the PFD user interface via a dedicated AIRPORT DATA screen. The screen shall show record count, disk usage, age in days since last download, and shall indicate an expired dataset when older than 60 days (configurable).

> **REQ-DISP-ZERO-APT-008** Downloaded airport CSV shall be parsed into a NumPy structured-array cache (.npy) on first access so that subsequent PFD launches load the database without re-parsing the text CSV.

> **REQ-DISP-ZERO-APT-009** A `NO APT` status badge (amber) shall be displayed when no airport data is loaded. An `EXP APT` status badge (orange) shall be displayed when the loaded airport data is older than the configured expiry.

> **REQ-DISP-ZERO-APT-010** The AIRPORT DATA screen shall provide four independently toggleable display filters controlling which airport types render on the attitude indicator:
>
> - PUBLIC — small / medium / large public-use airports (default: on)
> - HELI — heliports (default: on)
> - WATER — seaplane bases (default: off)
> - OTHER — balloonports and uncategorised types (default: off)
>
> Filter state shall persist across the session. When all four filters are off, no airport symbols shall render.

---

## 10. Status Badges

Status badges provide at-a-glance annunciation of system conditions that require pilot awareness. The badge strip is intentionally blank during fully normal operation so that any badge appearance is immediately conspicuous. The requirements below govern when the badge strip is used and which specific badges must be implemented.

> **REQ-DISP-ZERO-BADGE-001** Status badges shall appear in the badge strip only when a condition exists that requires pilot attention. The badge strip shall be completely blank during fully normal operation with all systems healthy and all data current.

> **REQ-DISP-ZERO-BADGE-002** The following status badges shall be implemented, with the indicated colour and trigger condition:
>
> - `AHRS FAIL` (red) — the AHRS unit is not providing valid attitude data.
> - `NO LINK` (red) — no SSE event has been received within the stale timeout period.
> - `NO TER` (amber) — no SRTM terrain tiles are present for the current region.
> - `NO OBS` (amber) — no FAA Digital Obstacle File data is loaded.
> - `EXP OBS` (orange) — the loaded obstacle data has exceeded the 28-day validity period.
> - `NO APT` (amber) — no airport data is loaded.
> - `EXP APT` (orange) — the loaded airport data is older than the configured expiry.
> - `GPS TRK` (magenta) — GPS TRK heading source mode is active.
> - `GPS ALT` (amber) — altitude is being sourced from GPS because the barometric sensor has failed.
> - `GPS Nsat` (amber) — GPS fix is valid but the satellite count is below the minimum reliable threshold.
> - `NO GPS` (red) — no valid GPS fix is available.

---

## 11. Colour Coding — Data Source Convention

A consistent colour coding convention is used throughout the PFD to communicate to the pilot whether a displayed value originates from an onboard sensor or from GPS. This convention must be applied uniformly to every affected element so that the pilot can immediately identify the data source of any readout or control element without consulting documentation.

> **REQ-DISP-ZERO-COLOR-001** All values and controls associated with data derived from GPS shall be displayed in MAGENTA.

> **REQ-DISP-ZERO-COLOR-002** All values and controls associated with data derived from onboard sensors — including the IMU, barometric pressure sensor, and any connected indicated airspeed sensor — shall be displayed in CYAN.

> **REQ-DISP-ZERO-COLOR-003** The MAGENTA / CYAN data source colour convention shall be applied consistently to all of the following elements: the speed bug and speed readout button, the altitude bug and altitude readout button, the baro setting button, the heading bug and heading readout button, the heading box border, and the heading source subscript letter.

---

## 12. Setup and Configuration

The setup interface provides access to all pilot-configurable parameters. It is designed to be inaccessible by accidental single-finger touches and is organised into sub-menus grouped by function. The requirements below govern the gesture used to open the setup menu, the required sub-menu structure, default V-speed values, unit options, and brightness control.

> **REQ-DISP-ZERO-SETUP-001** The setup menu shall be opened by a two-finger press-and-hold gesture on the touchscreen, sustained for at least 0.8 seconds.

> **REQ-DISP-ZERO-SETUP-002** The setup menu shall contain the following sub-menus: Flight Profile (for aircraft callsign and V-speed entry), Display (for unit selection and brightness adjustment), AHRS / Sensors (for pitch/roll trim, mounting orientation, heading source mode, and airspeed source selection), Connectivity (for AHRS SSE URL and Wi-Fi configuration), and System (for system information display and terrain/obstacle data downloads).

> **REQ-DISP-ZERO-SETUP-003** The factory default V-speed values shall correspond to the Cessna 172S: VS0 = 48 kt, VS1 = 55 kt, VFE = 85 kt, VNO = 129 kt, VNE = 163 kt.

> **REQ-DISP-ZERO-SETUP-004** Display units shall be independently selectable for each measurement type: airspeed in knots, miles per hour, or kilometres per hour; altitude in feet or metres; and pressure in inches of mercury or hectopascals.

> **REQ-DISP-ZERO-SETUP-005** Display backlight brightness shall be adjustable in 10 discrete steps from minimum to maximum brightness.

---

## 13. Flight Simulator

The built-in flight simulator allows the pilot or technician to exercise all PFD instruments and alerting functions without any connection to the AHRS unit or to an actual aircraft. It uses an internal autopilot model to generate realistic flight dynamics. The requirements below govern the simulator's operational modes, airport presets, failure injection capability, in-flight controls, and PFD interactivity.

> **REQ-DISP-ZERO-SIM-001** The display unit shall include a built-in flight simulator that drives all PFD instruments — attitude, altitude, airspeed, heading, vertical speed, GPS position, and status badges — through an internal autopilot model, without requiring any external hardware or network connection.

> **REQ-DISP-ZERO-SIM-002** The simulator shall provide 12 preset departure airports located across geographically diverse regions of the United States, allowing the pilot to observe terrain awareness and TAWS alerting behaviour in different terrain environments.

> **REQ-DISP-ZERO-SIM-003** The simulator shall allow independent injection of the following failure modes during a simulated flight: GPS failure (forcing GPS-derived values to become invalid), barometric sensor failure (forcing fallback to GPS altitude and activating the `GPS ALT` badge), and AHRS failure (activating the `AHRS FAIL` badge and flagging attitude data as invalid).

> **REQ-DISP-ZERO-SIM-004** A `SIM` watermark shall be displayed on the PFD at all times during simulator operation. Tapping the `SIM` watermark shall open a SIM CONTROLS overlay that exposes the failure injection toggles and simulator parameters.

> **REQ-DISP-ZERO-SIM-005** While the simulator is running, the autopilot model shall respond in real time to changes in the heading bug, altitude bug, and speed bug as set by the pilot through the normal PFD touch interface.

---

## 14. Demo Mode

Demo mode provides a self-contained, scripted demonstration of the PFD that can be run without any AHRS hardware, network connection, or pilot interaction. It is intended for exhibition, software testing, and system verification purposes. The requirements below define the demo flight sequence, the geographic setting, and the launch method.

> **REQ-DISP-ZERO-DEMO-001** The display unit shall include a scripted demo mode that animates a pre-defined flight sequence in the vicinity of Sedona, Arizona, driving all PFD instruments through the scripted profile without requiring any Pico W hardware, SSE connection, or external input.

> **REQ-DISP-ZERO-DEMO-002** Demo mode shall be launchable by invoking the PFD software from the command line with the `--demo` flag, with no additional configuration required.

---

## 15. Relationship to Pi 4 Variant and SVT Capability

This section documents the deliberate architectural scope boundary between the Pi Zero 2W display variant and the full-capability Pi 4 variant, to prevent ambiguity in requirements tracing and software integration.

> **REQ-DISP-ZERO-ARCH-001** This document (HLR-DISP-ZERO-001) governs the Pi Zero 2W display variant exclusively. The Pi 4 display variant is governed by a separate document, HLR-DISP-PI4-001, which supersedes this document in all respects where full SVT capability is required.

> **REQ-DISP-ZERO-ARCH-002** The Pi Zero 2W variant SHALL NOT implement Synthetic Vision Terrain background rendering on the attitude indicator. This exclusion is a deliberate design constraint arising from the Pi Zero 2W's limited GPU memory bandwidth and VideoCore IV capability relative to the per-frame cost of SRTM tile sampling and terrain mesh rasterisation at 30 fps.

> **REQ-DISP-ZERO-ARCH-003** SRTM terrain elevation data downloaded and stored on the Pi Zero 2W display unit SHALL be used for TAWS proximity alerting only (Section 9). The presence or absence of SRTM tiles SHALL NOT affect the rendering of the attitude indicator background, which shall remain a plain sky/ground horizon at all times.

> **REQ-DISP-ZERO-ARCH-004** The software architecture of the Pi Zero 2W display variant shall be structured such that the SVT rendering code path is either absent or disabled at build/configuration time, so that no terrain tile loading, sampling, or rasterisation is performed on the rendering thread, ensuring the 30 fps frame budget is not compromised by unused code paths.

> **REQ-DISP-ZERO-ARCH-005** Operators who require Synthetic Vision Terrain background rendering on the attitude indicator shall use the Pi 4 variant of the display unit, as defined in HLR-DISP-PI4-001. The Pi Zero 2W variant is the recommended choice for weight-sensitive or cost-sensitive installations where TAWS alerting without SVT is operationally acceptable.
