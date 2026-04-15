# Pi 4 Display Unit — Bench Test Procedure

| Field | Value |
|-------|-------|
| Document No. | TP-PI4-001 |
| Title | Initial Hardware Bring-Up — Pi 4 Display + AHRS Unit |
| Project | Pico-AHRS / PFD |
| Date | 2026-04-15 |
| Version | 0.2 |
| Performed by | _____________ |
| Date performed | _____________ |

---

## Equipment Required

- Raspberry Pi 4 (2 GB RAM minimum) with a 1024×600 display (e.g. ROADOM 7" IPS HDMI + USB touch) — or a Waveshare 3.5" 640×480 DPI panel with `DISPLAY_PROFILE = "waveshare_35"` in `pi4/config.py`
- Pico W AHRS unit (ICM-42688-P or WT901, BME280, u-blox GPS module)
- 5 V / 3 A USB-C power supply for Pi 4; 5 V / 2 A for Pico W
- USB keyboard (for initial startup commands)
- Smartphone or laptop (for Pico W AP validation and internet-tethered downloads)
- Level surface (bench or table)
- GitHub branch: `claude/split-display-versions-YJ9h8`

**Pass/Fail legend:**  ✓ Pass   ✗ Fail   N/T Not tested   N/A Not applicable

---

## Phase 1 — Display Unit (standalone, no AHRS)

### 1.1 Power-On and Boot

| Step | Action | Expected Result | Result | Notes |
|------|--------|----------------|--------|-------|
| 1.1.1 | Apply 5 V / 3 A to Pi 4 | Green power LED illuminates; HDMI (or DSI) display powers up | | |
| 1.1.2 | Observe display panel | Backlight or video signal within 10 s; no "no signal" banner once fully booted | | |
| 1.1.3 | Wait for console prompt | OS boot completes without kernel panic; `/boot/firmware/config.txt` has the HDMI force-hotplug + resolution overrides applied by `pi4/setup.sh` | | |
| 1.1.4 | Verify `config.py` DISPLAY_PROFILE | Matches the connected panel (`roadom_7` for 1024×600; `waveshare_35` for 640×480) | | |

### 1.2 First Launch — Demo Mode

| Step | Action | Expected Result | Result | Notes |
|------|--------|----------------|--------|-------|
| 1.2.1 | Run `python3 pi4/pfd.py --demo` | PFD fills screen within 3 s; frame rate is visibly smooth (target 30 fps) | | |
| 1.2.2 | Observe top strip | `NO LINK` badge present (no AHRS connected — expected) | | |
| 1.2.3 | Observe AI | Blue sky / brown ground horizon visible | | |
| 1.2.4 | Observe AI animation | Horizon animates through scripted demo scenarios | | |
| 1.2.5 | Observe airspeed tape | Speed value scrolls; V-speed arcs visible | | |
| 1.2.6 | Observe altitude tape | Altitude scrolls; VSI bar deflects on climb/descent | | |
| 1.2.7 | Observe heading tape | Heading tape scrolls; bug marker visible | | |
| 1.2.8 | Observe heading box (centre bottom) | "133°" with `M` subscript; heading box white border | | |
| 1.2.9 | Observe speed bug box (top-left) | Magenta border and text — GPS GS source | | |
| 1.2.10 | Observe alt bug box (top-right) | Magenta border — GPS ALT (demo has no baro) | | |
| 1.2.11 | Observe baro button (bottom-right) | Magenta `GPS ALT` label | | |
| 1.2.12 | Observe HDG bug button (bottom-left) | Cyan border — MAG mode | | |
| 1.2.13 | Observe `DEMO` watermark | Red `DEMO` text visible at centre AI | | |

### 1.3 Touch — Setup Menu Access

| Step | Action | Expected Result | Result | Notes |
|------|--------|----------------|--------|-------|
| 1.3.1 | Place two fingers on screen and hold 0.8 s | Setup menu appears with 6 tiles: FLIGHT PROFILE, DISPLAY, AHRS / SENSORS, CONNECTIVITY, SYSTEM, EXIT | | |
| 1.3.2 | Tap **EXIT** tile | Returns to PFD | | |
| 1.3.3 | Re-enter setup; tap **DISPLAY** | Display settings screen opens | | |
| 1.3.4 | Observe **BACK** button in header | "← BACK" text fits cleanly inside the button outline with no overflow (regression check — the button auto-scales with FONT_SCALE so the label never clips on 1024×600) | | |
| 1.3.5 | Tap **+** brightness button | Backlight brightens one step if panel supports PWM (ROADOM HDMI panels have no software backlight — brightness value changes but physical output may not) | | |
| 1.3.6 | Tap **−** brightness button | Value decreases | | |
| 1.3.7 | Tap **BACK** | Returns to setup menu | | |
| 1.3.8 | Tap **FLIGHT PROFILE** | V-speeds screen opens with VS0/VS1/VFE/VNO/VNE + tail/actype fields | | |
| 1.3.9 | Tap **BACK**; tap **CONNECTIVITY** | Connectivity screen opens; current SSID + AHRS link status visible | | |
| 1.3.10 | Tap **BACK**; tap **SYSTEM** | System screen opens with version, TERRAIN / OBSTACLES / AIRPORTS data tiles, FLIGHT SIMULATOR, RESET DEFAULTS | | |

### 1.4 Touch — Bug Setting

| Step | Action | Expected Result | Result | Notes |
|------|--------|----------------|--------|-------|
| 1.4.1 | Tap alt bug box (top-right of alt tape) | Numpad overlay appears, title "SET ALT" | | |
| 1.4.2 | Type `85` then tap **ENTER** | Numpad closes; alt bug readout shows `8500`; bug chevron moves to 8500 ft | | |
| 1.4.3 | Tap HDG bug box (bottom-left) | Numpad overlay appears, title "SET HDG" | | |
| 1.4.4 | Type `270` then tap **ENTER** | HDG bug readout shows `270°`; bug chevron moves on tape | | |
| 1.4.5 | Tap speed bug box (top-left) | Numpad overlay appears, title "SET SPD" | | |
| 1.4.6 | Type `90` then tap **ENTER** | Speed bug readout shows `90`; bug chevron visible on tape | | |
| 1.4.7 | Tap anywhere on heading tape | HDG bug jumps to tapped heading | | |
| 1.4.8 | Tap anywhere on altitude tape | Alt bug jumps to nearest 100 ft | | |

### 1.5 Touch — AHRS / Sensors Screen

| Step | Action | Expected Result | Result | Notes |
|------|--------|----------------|--------|-------|
| 1.5.1 | Enter setup; tap **AHRS / SENSORS** | AHRS setup screen opens | | |
| 1.5.2 | Observe Row 1 (PITCH TRIM) | `−` and `+` buttons present; current value `+0.0°` | | |
| 1.5.3 | Tap `+` pitch trim button | Value increments by 0.5° | | |
| 1.5.4 | Tap `−` pitch trim button | Value decrements back to 0.0° | | |
| 1.5.5 | Observe Row 4 (HEADING SOURCE) | **MAG** button active (cyan), **GPS TRK** available | | |
| 1.5.6 | Tap **GPS TRK** | Button highlights; return to PFD | | |
| 1.5.7 | Observe heading box | Border turns magenta; `G` subscript appears | | |
| 1.5.8 | Observe HDG bug button | Border turns magenta | | |
| 1.5.9 | Return to AHRS setup; tap **MAG** | Heading box returns to white border; `M` subscript | | |
| 1.5.10 | Observe Row 5 (AIRSPEED SOURCE) | **GPS GS** button active; **IAS SENSOR** greyed out | | |

### 1.6 Flight Simulator

| Step | Action | Expected Result | Result | Notes |
|------|--------|----------------|--------|-------|
| 1.6.1 | Enter setup; tap **SYSTEM** | System screen opens | | |
| 1.6.2 | Tap **FLIGHT SIMULATOR** tile | Simulator setup screen opens with airport grid | | |
| 1.6.3 | Confirm 12 airport presets visible | KSEZ, KPHX, KDEN, KLAX, KSFO, KLAS, KSEA, KOSH, KJFK, KORD, KDFW, KMIA | | |
| 1.6.4 | Tap **KSEZ** (Sedona AZ) | Preset highlights cyan | | |
| 1.6.5 | Tap **START** | PFD returns; `SIM` watermark visible at AI centre | | |
| 1.6.6 | Observe aircraft behaviour | Speed, altitude, heading hold set values | | |
| 1.6.7 | Set alt bug to `9500` | Aircraft climbs toward 9500 ft | | |
| 1.6.8 | Set HDG bug to `270` | Aircraft turns to 270° | | |
| 1.6.9 | Tap `SIM` watermark | SIM controls overlay appears | | |
| 1.6.10 | Tap **BARO → FAIL** | Alt bug/box/tape turn magenta; `GPS ALT` badge appears | | |
| 1.6.11 | Tap **BARO → ON** | Alt bug/box return to cyan; badge clears | | |
| 1.6.12 | Tap **GPS → FAIL** | `NO GPS` badge appears; speed tape shows `---` | | |
| 1.6.13 | Tap **GPS → ON** | Speed recovers; badge clears | | |
| 1.6.14 | Tap **AHRS → FAIL** | `AHRS FAIL` badge appears; horizon freezes | | |
| 1.6.15 | Tap **AHRS → ON** | Horizon resumes | | |
| 1.6.16 | Tap **EXIT SIM** | Simulator stops; watermark clears | | |

---

## Phase 2 — AHRS Unit (standalone)

### 2.1 Pico W Power-On

| Step | Action | Expected Result | Result | Notes |
|------|--------|----------------|--------|-------|
| 2.1.1 | Apply 5 V to Pico W AHRS unit | Pico W power LED illuminates | | |
| 2.1.2 | Wait 5 s | Pico W WiFi AP `AHRS-Link` visible on a phone/laptop | | |
| 2.1.3 | Connect a device to `AHRS-Link` | DHCP address assigned (typically `192.168.4.x`) | | |
| 2.1.4 | Open browser to `http://192.168.4.1` | JSON or SSE stream response visible | | |
| 2.1.5 | Observe SSE event format | Events contain `pitch`, `roll`, `yaw`, `alt`, `speed` fields | | |
| 2.1.6 | Tilt the AHRS unit ~30° | `roll` value in stream changes correspondingly | | |
| 2.1.7 | Pitch the AHRS unit ~15° nose-up | `pitch` value increases | | |

### 2.2 BME280 Barometric Sensor

| Step | Action | Expected Result | Result | Notes |
|------|--------|----------------|--------|-------|
| 2.2.1 | Observe SSE stream `baro_src` field | Value is `bme280` (not `gps`) | | |
| 2.2.2 | Observe `baro_hpa` field | Value is within ±5 hPa of local altimeter setting | | |
| 2.2.3 | Observe `alt` field | Pressure altitude within ±50 ft of field elevation | | |

### 2.3 GPS Module

| Step | Action | Expected Result | Result | Notes |
|------|--------|----------------|--------|-------|
| 2.3.1 | Place unit near window or outside | GPS acquiring — `sats` field > 0 within 2 min | | |
| 2.3.2 | Wait for fix | `fix` field = 1; `sats` ≥ 4 | | |
| 2.3.3 | Observe `lat` / `lon` fields | Values match known position within ~50 m | | |
| 2.3.4 | Observe `speed` field | Near-zero when stationary | | |

---

## Phase 3 — Integrated System

### 3.1 Link Establishment

| Step | Action | Expected Result | Result | Notes |
|------|--------|----------------|--------|-------|
| 3.1.1 | Start AHRS unit; confirm AP `AHRS-Link` is visible | AP broadcasting | | |
| 3.1.2 | Configure Pi Zero 2W WiFi to join `AHRS-Link` (via Connectivity screen) | Pi associates | | |
| 3.1.3 | Launch `python3 pi4/pfd.py` (no --demo) | PFD starts | | |
| 3.1.4 | Observe top strip within 5 s | `NO LINK` badge clears | | |
| 3.1.5 | Observe baro button (bottom-right) | Shows `29.92 IN` in cyan (not `GPS ALT`) | | |
| 3.1.6 | Observe alt bug box (top-right) | Cyan border | | |
| 3.1.7 | Observe speed bug box (top-left) | Magenta border — GPS GS | | |

### 3.2 Attitude Response

| Step | Action | Expected Result | Result | Notes |
|------|--------|----------------|--------|-------|
| 3.2.1 | Hold AHRS unit level | AI horizon centred; slip bar centred under zero-bank doghouse | | |
| 3.2.2 | Roll AHRS unit ~30° right | AI rolls right; bank pointer deflects right | | |
| 3.2.3 | Roll AHRS unit ~30° left | AI rolls left | | |
| 3.2.4 | Pitch AHRS unit ~10° nose-up | Horizon bar drops; pitch ladder moves down | | |
| 3.2.5 | Pitch AHRS unit ~10° nose-down | Horizon bar rises | | |
| 3.2.6 | Rotate AHRS unit slowly in yaw | Heading tape scrolls in correct direction | | |
| 3.2.7 | Observe attitude response lag | Horizon responds within 1–2 frames of movement (≤ 100 ms) | | |

### 3.3 Horizon Trim (if needed)

| Step | Action | Expected Result | Result | Notes |
|------|--------|----------------|--------|-------|
| 3.3.1 | Place AHRS unit on a known-level surface | | | |
| 3.3.2 | Observe AI horizon position | Horizon bar aligned with aircraft symbol | | |
| 3.3.3 | If horizon is high/low: enter setup → AHRS / SENSORS → PITCH TRIM | Adjust in 0.5° steps until aligned | | |
| 3.3.4 | If horizon is tilted: ROLL TRIM | Adjust until wings-level | | |
| 3.3.5 | Confirm trim holds across power cycles | Re-launch pfd.py; horizon remains corrected | | |

### 3.4 GPS Integration

| Step | Action | Expected Result | Result | Notes |
|------|--------|----------------|--------|-------|
| 3.4.1 | With GPS fix acquired: observe track pointer | **Magenta** tick on heading tape shows GPS ground track (suppressed if track ≈ hdg within 1°) | | |
| 3.4.2 | Observe top strip | No `GPS` badge (fix is valid) | | |
| 3.4.3 | Enable GPS TRK mode in AHRS / SENSORS | Heading box border turns magenta; `G` subscript | | |
| 3.4.4 | Rotate AHRS unit slowly | Heading follows GPS track via complementary filter | | |
| 3.4.5 | Return to MAG mode | Heading box returns to white; `M` subscript | | |

### 3.5 Link Loss and Recovery

| Step | Action | Expected Result | Result | Notes |
|------|--------|----------------|--------|-------|
| 3.5.1 | With link active: power off AHRS unit | After 3 s: `NO LINK` badge appears | | |
| 3.5.2 | Observe tapes | Values freeze at last received value | | |
| 3.5.3 | Power AHRS unit back on | Within 5 s: `NO LINK` badge clears; tapes resume | | |

---

## Phase 4 — Baro Setting Verification

| Step | Action | Expected Result | Result | Notes |
|------|--------|----------------|--------|-------|
| 4.1 | Obtain current local altimeter setting (ASOS/ATIS) | Record value: __________ inHg | | |
| 4.2 | Tap baro button (bottom-right) | Numpad opens with current hPa value | | |
| 4.3 | Enter correct inHg value (e.g. `2992` for 29.92) | Baro button updates; altitude corrects | | |
| 4.4 | Verify indicated altitude | Within ±75 ft of field elevation at known location | | |
| 4.5 | Switch baro unit to hPa in Display Settings | Baro button shows hPa value | | |
| 4.6 | Tap baro button and enter hPa value | Altitude unchanged; unit label updates | | |

---

## Phase 5 — Data Downloads

Requires Pi to be on an internet-reachable WiFi (use `sudo bash wifi_switch.sh home`, or configure a home SSID via Connectivity setup).

### 5.1 Terrain Data (SRTM tiles — powers TAWS + SVT mesh)

| Step | Action | Expected Result | Result | Notes |
|------|--------|----------------|--------|-------|
| 5.1.1 | Setup → System → **TERRAIN** | Terrain data screen opens; idle state if no tiles present | | |
| 5.1.2 | Tap preset region **US Southwest** | Region highlights | | |
| 5.1.3 | Tap **DOWNLOAD** | Progress bar advances; per-tile status updates | | |
| 5.1.4 | Wait for completion | Done ✓ message; record count and MB displayed | | |
| 5.1.5 | Return to PFD, observe status badges | `NO TER` badge clears | | |
| 5.1.6 | If on Pico W AP (no internet): tap DOWNLOAD | "WiFi (home network) required" guard message appears | | |

### 5.2 Obstacle Data (FAA DOF)

| Step | Action | Expected Result | Result | Notes |
|------|--------|----------------|--------|-------|
| 5.2.1 | Setup → System → **OBSTACLES** | Obstacle data screen opens | | |
| 5.2.2 | Tap **DOWNLOAD** | Progress bar runs; ~20 MB CSV downloads then parses | | |
| 5.2.3 | Wait for completion | "Done ✓  ~76,000 obstacles loaded" | | |
| 5.2.4 | Return to PFD | `NO OBS` badge clears | | |

### 5.3 Airport + Runway Data (OurAirports)

| Step | Action | Expected Result | Result | Notes |
|------|--------|----------------|--------|-------|
| 5.3.1 | Setup → System → **AIRPORTS** | Airport data screen opens | | |
| 5.3.2 | Tap **DOWNLOAD** (or **UPDATE** if present) | Progress bar runs; airports.csv (~12 MB) then runways.csv (~3 MB) download | | |
| 5.3.3 | Wait for completion | "Done ✓  72,007 airports, 14,727 runways" (counts may vary by data version) | | |
| 5.3.4 | Observe screen footer | Two-row toggle panel: PUBLIC / HELIPORTS / SEAPLANE / OTHER on row 1; RUNWAYS / EXT CENTERLINES on row 2 | | |
| 5.3.5 | Return to PFD | `NO APT` badge clears | | |

---

## Phase 5.5 — OpenGL Synthetic Vision Terrain (Pi 4 only)

Requires SRTM tiles loaded (Phase 5.1 complete). Use `pi4/pfd.py --demo --sim` or the flight simulator (KSEZ preset) for repeatable scenes.

| Step | Action | Expected Result | Result | Notes |
|------|--------|----------------|--------|-------|
| 5.5.1 | Observe PFD startup console | Lines showing `SVT_RENDERER: opengl` and `GL_AVAILABLE: True`; no fallback-to-pygame message | | |
| 5.5.2 | Observe AI background at KSEZ at 6500 ft | 3D terrain mesh visible, not a flat blue/brown split — mesas + Mogollon Rim recognisable | | |
| 5.5.3 | Descend to 5500 ft near Sedona | Mountain peaks rise **above the horizon line** as they exceed aircraft altitude (this is the defining Pi 4 capability) | | |
| 5.5.4 | Observe terrain shading | Sun-angle shading creates distinct bright/dark slopes; ridges and valleys readable | | |
| 5.5.5 | Observe distance grid | Cyan grid on terrain: 0.5 nm minor + 2 nm major lines; fades toward mesh edge | | |
| 5.5.6 | Fly into rising terrain | Terrain colour banding shifts: brown (1000+ ft clear) → amber (100–500 ft) → orange (0–100 ft) → red (above aircraft) | | |
| 5.5.7 | Observe zero-pitch reference line | Short cyan hash marks across AI, always at aircraft 0° pitch regardless of terrain altitude | | |
| 5.5.8 | Roll sim 30° | Terrain mesh rolls with horizon; grid remains screen-aligned to terrain | | |
| 5.5.9 | Delete or rename `pi4/data/srtm` directory; relaunch | SVT falls back to blue/brown split; `NO TER` badge appears | | |
| 5.5.10 | Restore SRTM directory; relaunch | Terrain mesh returns | | |
| 5.5.11 | Observe sustained frame rate during continuous demo | 30 fps target; no visible stutter on ROADOM 1024×600 panel | | |

---

## Phase 6 — Attitude Indicator Overlays

All overlays presume Phase 5 downloads completed. Run in demo or simulator mode.

### 6.1 Airport Symbols + Road-Sign Labels

| Step | Action | Expected Result | Result | Notes |
|------|--------|----------------|--------|-------|
| 6.1.1 | Sim at KSEZ, alt 6000 ft, heading 200° | Cyan-ring airport symbol visible for KSEZ in lower AI | | |
| 6.1.2 | Observe near KSEZ | Magenta "H" symbols for nearby heliports (e.g. 4BAZ) | | |
| 6.1.3 | Observe airport label rendering | Ident ("KSEZ") rendered as small box on short vertical post within 15 nm | | |
| 6.1.4 | Open AIRPORT DATA; tap **HELIPORTS** to toggle OFF | H symbols disappear from AI | | |
| 6.1.5 | Tap **HELIPORTS** again | Heliports reappear | | |
| 6.1.6 | Toggle all four type filters OFF | No airport symbols rendered (sanity check) | | |
| 6.1.7 | Re-enable PUBLIC + HELIPORTS | Symbols restored | | |

### 6.2 Runway Polygons + Extended Centerlines

| Step | Action | Expected Result | Result | Notes |
|------|--------|----------------|--------|-------|
| 6.2.1 | Sim at KSEZ, set alt 5500, heading 033°, position 2 NM SW of RWY 03 threshold | Two tan runway polygons visible below horizon near airport symbol, rendered on top of the SVT mesh | | |
| 6.2.2 | Observe extended centerlines | Dashed white lines extending 10 nm from each threshold along runway axis, visible within 15 nm | | |
| 6.2.3 | Airport Data → toggle **RUNWAYS** OFF | Polygons disappear; centerlines remain | | |
| 6.2.4 | Toggle **EXT CENTERLINES** OFF (RUNWAYS still off) | Centerlines disappear | | |
| 6.2.5 | Re-enable both | Polygons + centerlines restored | | |
| 6.2.6 | Roll simulator ±30° | Runway polygons rotate correctly with horizon; no "phantom" horizontal streaks across the AI (regression check) | | |
| 6.2.7 | Z-order check | Cyan airport ring at runway intersection sits on **top** of the runway asphalt | | |

### 6.3 Obstacle Symbols

| Step | Action | Expected Result | Result | Notes |
|------|--------|----------------|--------|-------|
| 6.3.1 | Position sim with known tower in view | Caret-shape obstacle symbol visible on AI, anchored to terrain | | |
| 6.3.2 | Observe colour coding | Red = above aircraft alt; Yellow = within 500 ft below; White = more than 500 ft below | | |
| 6.3.3 | Observe lit indicator | Star (★) above caret for lit towers; plain caret for unlit | | |

### 6.4 TAWS Proximity Alerts

| Step | Action | Expected Result | Result | Notes |
|------|--------|----------------|--------|-------|
| 6.4.1 | Descend sim toward rising terrain (e.g. Mogollon Rim north of KSEZ) | Amber `TERRAIN` caution banner appears at ~500 ft clearance; SVT terrain colour band shifts to amber simultaneously | | |
| 6.4.2 | Continue descent | Red `PULL UP` warning banner appears at ~100 ft clearance; terrain colour bands to orange/red | | |
| 6.4.3 | Climb away from terrain | Banners clear; terrain colours return to brown | | |

---

## Phase 7 — User Settings Persistence

| Step | Action | Expected Result | Result | Notes |
|------|--------|----------------|--------|-------|
| 7.1 | Record current settings: brightness __, baro unit __, speed unit __, HDG bug __, ALT bug __, HDG source __, tail # __ | | | |
| 7.2 | Adjust brightness to an unusual value (e.g. 3) | Value changes | | |
| 7.3 | Set HDG bug to 270°, ALT bug to 9500 | Bugs reflect new values | | |
| 7.4 | Toggle RUNWAYS off on AIRPORT DATA | State recorded | | |
| 7.5 | Wait 3 s (allow debounce writer to flush) | | | |
| 7.6 | `sudo reboot` the Pi 4 | Pi reboots | | |
| 7.7 | After PFD relaunch, observe startup console | "[PFD] Settings restored from …/settings.json" line present | | |
| 7.8 | Verify brightness, HDG bug, ALT bug, RUNWAYS toggle | All match values from step 7.3–7.4 | | |
| 7.9 | Check `pi4/data/settings.json` exists and contains adjusted values | JSON has expected keys (fp, ds, ad, hdg_bug, alt_bug, etc.) | | |
| 7.10 | Verify Wi-Fi password is **not** in the file | `password` key absent from the `cs.networks[*]` entries (only SSID + known=true) | | |

---

## Anomaly Log

Use this table to record any unexpected behaviour for later investigation.

| # | Step | Observed Behaviour | Suspected Cause | Resolved Y/N |
|---|------|--------------------|-----------------|--------------|
| 1 | | | | |
| 2 | | | | |
| 3 | | | | |
| 4 | | | | |
| 5 | | | | |

---

## Sign-Off

| Phase | All steps pass? | Tester initials | Date |
|-------|----------------|-----------------|------|
| Phase 1 — Display standalone | Y / N | | |
| Phase 2 — AHRS standalone | Y / N | | |
| Phase 3 — Integrated | Y / N | | |
| Phase 4 — Baro verification | Y / N | | |
| Phase 5 — Data downloads | Y / N | | |
| Phase 5.5 — OpenGL SVT | Y / N | | |
| Phase 6 — AI overlays | Y / N | | |
| Phase 7 — Settings persistence | Y / N | | |

---

*TP-PI4-001 v0.2 — covers software branch `claude/split-display-versions-YJ9h8`. For the Pi Zero 2W variant see TP-ZERO-001.*
