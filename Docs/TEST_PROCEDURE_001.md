# PFD System — Bench Test Procedure

| Field | Value |
|-------|-------|
| Document No. | TP-001 |
| Title | Initial Hardware Bring-Up — Display Unit and AHRS Unit |
| Project | Pico-AHRS / PFD |
| Date | 2026-04-11 |
| Version | 0.1 |
| Performed by | _____________ |
| Date performed | _____________ |

---

## Equipment Required

- Raspberry Pi Zero 2W with 640×480 DSI touchscreen
- Pico W AHRS unit (ICM-42688-P, BME280, GPS module)
- 5 V USB power supply (×2)
- USB keyboard (for initial startup commands)
- Level surface (bench or table)
- GitHub branch: `claude/pico-ahrs-flight-display-GAxs8`

**Pass/Fail legend:**  ✓ Pass   ✗ Fail   N/T Not tested   N/A Not applicable

---

## Phase 1 — Display Unit (standalone, no AHRS)

### 1.1 Power-On and Boot

| Step | Action | Expected Result | Result | Notes |
|------|--------|----------------|--------|-------|
| 1.1.1 | Apply 5 V to Pi Zero 2W | Green power LED illuminates | | |
| 1.1.2 | Observe DSI panel | Backlight illuminates within 10 s | | |
| 1.1.3 | Wait for desktop or console prompt | OS boot completes without kernel panic | | |

### 1.2 First Launch — Demo Mode

| Step | Action | Expected Result | Result | Notes |
|------|--------|----------------|--------|-------|
| 1.2.1 | Run `python3 pi_display/pfd.py --demo` | PFD fills screen within 3 s | | |
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
| 1.3.1 | Place two fingers on screen and hold 0.8 s | Setup menu appears with 6 tiles | | |
| 1.3.2 | Tap **EXIT** tile | Returns to PFD | | |
| 1.3.3 | Re-enter setup; tap **DISPLAY** | Display settings screen opens | | |
| 1.3.4 | Tap **+** brightness button | Screen brightens one step | | |
| 1.3.5 | Tap **−** brightness button | Screen dims one step | | |
| 1.3.6 | Tap **BACK** | Returns to setup menu | | |

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
| 1.6.3 | Confirm 12 airport presets visible | KSEZ, KPHX, KDEN, KLAX, KSFO, KLAS, KSEA, KATL, KJFK, KORD, KDFW, KMIA | | |
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
| 3.1.3 | Launch `python3 pi_display/pfd.py` (no --demo) | PFD starts | | |
| 3.1.4 | Observe top strip within 5 s | `NO LINK` badge clears | | |
| 3.1.5 | Observe baro button (bottom-right) | Shows `29.92 IN` in cyan (not `GPS ALT`) | | |
| 3.1.6 | Observe alt bug box (top-right) | Cyan border | | |
| 3.1.7 | Observe speed bug box (top-left) | Magenta border — GPS GS | | |

### 3.2 Attitude Response

| Step | Action | Expected Result | Result | Notes |
|------|--------|----------------|--------|-------|
| 3.2.1 | Hold AHRS unit level | AI horizon centred; slip ball centred | | |
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
| 3.4.1 | With GPS fix acquired: observe track pointer | Cyan tick on heading tape shows GPS track | | |
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

---

*TP-001 v0.1 — covers software branch `claude/pico-ahrs-flight-display-GAxs8`*
