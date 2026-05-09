# Pi 4 Display Unit — Bench Test Procedure

| Field | Value |
|-------|-------|
| Document No. | TP-PI4-001 |
| Title | Initial Hardware Bring-Up — Pi 4 Display + AHRS Unit |
| Project | Pico-AHRS / PFD |
| Date | 2026-05-09 |
| Version | 0.3 |
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
| 1.1.1 | Apply 5 V / 3 A to Pi 4 | Green power LED illuminates; HDMI (or DPI) display powers up | | |
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
| 1.4.1 | Tap alt bug box (top-right of alt tape) | Numpad overlay appears, title "SET ALTITUDE BUG (×100 ft)"; current value shown as dim `Current: <n>` placeholder below the empty entry line | | |
| 1.4.2 | Tap **ENTER** without typing | Numpad closes; alt bug value unchanged (placeholder-not-pre-populated UX check) | | |
| 1.4.3 | Re-open numpad; type `85` then tap **ENTER** | Numpad closes; alt bug readout shows `8500`; bug chevron moves to 8500 ft | | |
| 1.4.4 | Re-open numpad; type `9` then tap **⌫** (backspace) | Buffer empties one digit at a time — numpad still open | | |
| 1.4.5 | Tap HDG bug box (bottom-left of heading strip) | Numpad overlay appears, title "SET HDG BUG"; full heading-strip-height hit region (regression check) | | |
| 1.4.6 | Type `270` then tap **ENTER** | HDG bug readout shows `270°`; bug chevron moves on tape | | |
| 1.4.7 | Tap speed bug box (top of speed tape) | Numpad overlay appears, title "SET SPD BUG" | | |
| 1.4.8 | Type `90` then tap **ENTER** | Speed bug readout shows `90`; bug chevron visible on tape | | |
| 1.4.9 | Tap baro box (bottom-right of heading strip) | Numpad title "SET BARO inHg" or "SET BARO hPa" matching Display Settings unit | | |
| 1.4.10 | Tap anywhere on heading tape | HDG bug jumps to tapped heading | | |
| 1.4.11 | Tap anywhere on altitude tape | Alt bug jumps to nearest 100 ft | | |

### 1.4.5 Touch — Keyboard Entry

| Step | Action | Expected Result | Result | Notes |
|------|--------|----------------|--------|-------|
| 1.4.5.1 | Open Flight Profile; tap **CALLSIGN** | Keyboard overlay appears; current tail shown as placeholder, buffer empty | | |
| 1.4.5.2 | Verify row 4 contents | `Z X C V B N M . : ⌫` — period, colon, and backspace must all be present | | |
| 1.4.5.3 | Type `N` then `.` then `1` | Buffer accepts period character (regression check) | | |
| 1.4.5.4 | Tap **⌫** three times | Three chars removed from buffer end | | |
| 1.4.5.5 | Tap **CANCEL** | Keyboard closes; callsign unchanged | | |
| 1.4.5.6 | Connectivity → tap AHRS URL | Keyboard opens pre-populated with current URL (`http://192.168.4.1` by default) | | |
| 1.4.5.7 | Confirm `:` key enters colon | Buffer accepts colon — needed for URLs | | |

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

### 3.6 Connectivity Screen Diagnostics

| Step | Action | Expected Result | Result | Notes |
|------|--------|----------------|--------|-------|
| 3.6.1 | Setup → **CONNECTIVITY** with link up | STATUS row: AHRS green "CONNECTED"; WiFi green "WiFi: `<ssid>`" showing the actual network name the Pi is associated to (not the AHRS URL) | | |
| 3.6.2 | Observe AHRS LINK row | Subtitle shows transport + port (`USB /dev/ttyACM0` if Pico is USB-connected, otherwise `WIFI http://192.168.4.1`) | | |
| 3.6.3 | Observe RX: counter | Increments ~20×/s while link is healthy | | |
| 3.6.4 | Observe live R / P / Y / ALT on right-hand side | Values match the current AHRS readings; tilt the AHRS unit and confirm R and P change here as on the main PFD | | |
| 3.6.5 | Power off AHRS unit for 5 s then back on | RX counter pauses, then resumes; ERR may tick up once; last error string may briefly show | | |
| 3.6.6 | Disconnect WT901 TX line, reconnect | RX still increments (firmware alive) but R/P/Y freeze at last values — confirms the diagnostic catches "transport OK, sensor dead" | | |
| 3.6.7 | Tap **TEST AHRS** | Blue status message appears below the buttons — success or specific error | | |

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
| 5.1.2 | Observe region grid | Nine preset region tiles visible: US Southwest, US Pacific, US Southeast, US Northeast, US Midwest, All CONUS, Alaska, Europe West, All Europe. Each shows tile count + size estimate | | |
| 5.1.3 | Observe top tile | **DOWNLOAD CURRENT AREA** tile shows `~25 tiles around <lat>°N <lon>°W  ≈ 35 MB` (actual counts vary with latitude) | | |
| 5.1.4 | Tap preset region **US Southwest** | Download starts immediately (no separate DOWNLOAD button); progress strip appears at top | | |
| 5.1.5 | Observe progress | Per-tile status updates; current-of-total counter advances | | |
| 5.1.6 | Tap **CANCEL** mid-download | Download halts; already-downloaded tiles retained on disk | | |
| 5.1.7 | Tap same region again | Download resumes — already-present tiles are skipped, only missing ones fetch | | |
| 5.1.8 | Wait for completion | Done ✓ message; disk tile count + MB updates at top of screen | | |
| 5.1.9 | Return to PFD, observe status badges | `NO TER` badge clears | | |
| 5.1.10 | If on Pico W AP (no internet): tap any region | "WiFi (home network) required" guard message appears | | |

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

## Phase 5.6 — Veeder-Root Drum Cascade

These checks verify the rolling-drum regressions fixed during software v0.2 development (speed `1` at 100 kt, altitude `1` at 10000 ft, airspeed ones-drum "1 above 0" visibility). Runs in the flight simulator using the preview generator states.

| Step | Action | Expected Result | Result | Notes |
|------|--------|----------------|--------|-------|
| 5.6.1 | Sim at 99 kt, continue accelerating | Airspeed drum at 99 shows "0" and "9" stacked; a faint "1" is visible ABOVE the "0" before the crossing to 100 (d_hi2 preview slot) | | |
| 5.6.2 | Continue through 100 kt | Tens digit cascades from "9" to "0"; leading "1" column emerges smoothly; no blank slot | | |
| 5.6.3 | Sim at 9980 ft, climb at 500 fpm | Altitude drum shows "99" and "80" stacked; leading column reveals "1" peeking above the "99" before the crossing to 10000 | | |
| 5.6.4 | Continue through 10000 ft | Leading column cascades 9→0; new "1" column appears; tens row advances to "00" — no "0000" flash or missing-digit visual defect | | |
| 5.6.5 | Sim descends from 100 ft to 90 ft | Airspeed/altitude drums narrow from three cells to two without snapping (the two-drum path is entered at `alt_inner ≥ 99.5`, not at integer 100, so the transition is smooth) | | |

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

## Phase 6.5 — AHRS Mounting Orientation

Run this with the AHRS unit live (Phase 3 link up). Verify each orientation maps the AHRS-reported pitch / roll / yaw correctly to the displayed attitude indicator.

| Step | Action | Expected Result | Result | Notes |
|------|--------|----------------|--------|-------|
| 6.5.1 | Open Setup → AHRS / SENSORS. Confirm 7 rows visible: PITCH TRIM, ROLL TRIM, MAGNETOMETER, ORIENTATION, MOUNTING, HEADING SOURCE, AIRSPEED SOURCE | All rows present | | |
| 6.5.2 | Confirm trim ± steppers operate at 0.1° per tap | Value changes by 0.1° each tap; format `+0.1°` / `-0.5°` etc. | | |
| 6.5.3 | Set ORIENTATION = RIGHT, MOUNTING = NORMAL. Bench-tilt AHRS box right (top tipping toward aircraft right wing) | AI banks right (right wing of airplane symbol drops, horizon tilts left side high) | | |
| 6.5.4 | Bench-pitch AHRS box nose up | AI horizon descends | | |
| 6.5.5 | Yaw AHRS box clockwise from above | Heading number on tape increases | | |
| 6.5.6 | Set ORIENTATION = FORWARD (connector physically toward nose). Repeat 6.5.3 - 6.5.5 with the new mounting | All directions correct (right roll → right bank, nose up → horizon descends, CW yaw → heading increases) | | |
| 6.5.7 | Set ORIENTATION = LEFT. Repeat | All directions correct | | |
| 6.5.8 | Set ORIENTATION = AFT. Repeat | All directions correct | | |
| 6.5.9 | Return ORIENTATION to actual physical mounting. Set MOUNTING = INVERTED with the AHRS still right-side-up (deliberate mismatch) | Pitch and roll on AI both invert from physical motion | | |
| 6.5.10 | Set MOUNTING back to NORMAL | Display correct again | | |
| 6.5.11 | Sim mode: launch sim with `python3 pi4/pfd.py --sim --demo`. Verify orientation / trim / mounting do NOT affect sim attitude (left turn → bank left, no pitch coupling) | Sim attitude tracks autopilot commands only | | |

---

## Phase 6.6 — Compass Calibration Wizard

| Step | Action | Expected Result | Result | Notes |
|------|--------|----------------|--------|-------|
| 6.6.1 | Setup → AHRS / SENSORS. Tap CALIBRATE button (was greyed in pre-V5; now active green) | Modal opens with title "COMPASS CAL" | | |
| 6.6.2 | Verify modal shows: step text "Step 1 of 4 — point aircraft NORTH (000°)", live RAW heading, live APPLIED heading, four cardinal Δ slots (initially `+0.0°` × 4), buttons EXIT / RESET / RESTART / ⊕ CAPTURE N | All elements present | | |
| 6.6.3 | Point aircraft NORTH. Tap ⊕ CAPTURE N (or press physical ENTER) | Step advances to "Step 2 of 4 — EAST"; "Captured NORTH." status message appears | | |
| 6.6.4 | Point EAST, tap CAPTURE E | Step advances to SOUTH | | |
| 6.6.5 | Point SOUTH, tap CAPTURE S | Step advances to WEST | | |
| 6.6.6 | Point WEST, tap CAPTURE W | "Done" message with all four Δ values displayed (e.g. `N +1.2° E -0.8° S +0.7° W -1.5°`) | | |
| 6.6.7 | Tap EXIT (was CANCEL pre-completion; now reads EXIT in green since cal is committed) | Modal closes; AHRS Setup screen shows `max \|Δ\| 1.5°` (or whichever) under MAGNETOMETER row | | |
| 6.6.8 | Verify heading on PFD reads near a known landmark's true bearing (within ~2° after cal vs pre-cal error) | Heading accurate at the cardinal where pre-cal error was largest | | |
| 6.6.9 | Verify cal persists: power-cycle the Pi, confirm `data/settings.json` has `mag_cal_deltas` key with four values, confirm `max \|Δ\|` still shown on AHRS Setup row | Persisted correctly | | |
| 6.6.10 | Re-open CALIBRATE wizard, tap RESET | Stored cal cleared, status returns to IDLE, `max \|Δ\|` text disappears | | |
| 6.6.11 | RESTART test: open wizard, capture N + E (partial), tap RESTART | Step returns to N; no commit | | |
| 6.6.12 | CANCEL test: open wizard, capture N + E (partial), tap CANCEL (now red because partial captures exist) | Modal closes with NO change; cal remains at zero | | |

---

## Phase 6.7 — Direct-to Navigation

Requires GPS fix and OurAirports data loaded. Use a known nearby airport ident.

| Step | Action | Expected Result | Result | Notes |
|------|--------|----------------|--------|-------|
| 6.7.1 | On PFD, tap CDI strip above the heading box | Keyboard opens with "ENTER WAYPOINT" title | | |
| 6.7.2 | If a waypoint was previously active, confirm its ident is shown as a dim placeholder (input buffer is empty) | Placeholder visible; first keystroke replaces | | |
| 6.7.3 | Type a known nearby airport ident (e.g. KSEZ); tap ENTER | Modal "Activate Direct to KSEZ?" appears with CANCEL / ACTIVATE buttons | | |
| 6.7.4 | Tap ACTIVATE (or press physical ENTER) | Modal closes; CDI strip shows ident · BRG · DIST; magenta course-trace line appears on AI from current pos toward waypoint | | |
| 6.7.5 | Verify the magenta line is draped over terrain (does not cut through visible peaks/ridges in SVT view) | Line stays above terrain | | |
| 6.7.6 | Verify line builds progressively for a long course (>50 NM): near end visible immediately, far end fills in over a few seconds | Progressive build; no UI freeze | | |
| 6.7.7 | Tap CDI strip again, immediately press ENTER without typing | Modal pops with the same active ident (re-activate path) | | |
| 6.7.8 | Tap ACTIVATE | Course line redraws from current position (if you've moved since first activation) | | |
| 6.7.9 | Tap CDI, type a fictitious ident (e.g. ZZZZ), press ENTER | Keyboard stays open; red "UNKNOWN WAYPOINT ZZZZ" text under entry field | | |
| 6.7.10 | Backspace once | Error text clears; entry buffer reads "ZZZ" | | |
| 6.7.11 | Tap CANCEL on keyboard | Keyboard closes; existing active waypoint unchanged | | |
| 6.7.12 | Open Setup → AIRPORTS → DIRECT TO. Verify keyboard NEAREST extras row shows resolved ident on the green button (e.g. "DIRECT TO KSEZ") | Button label contains an actual ident, not "DIRECT TO NEAREST" placeholder | | |
| 6.7.13 | Tap the NEAREST button | Same Activate? modal flow | | |
| 6.7.14 | Tap CDI → CANCEL FLIGHT PLAN button on keyboard | Active waypoint cleared; CDI strip returns to "DIRECT  →" prompt; magenta line disappears | | |

---

## Phase 6.7.5 — Synthetic Approach (HITS + VDI + APPR runway picker)

Requires GPS fix, OurAirports + runway data loaded, and SRTM tiles for the area. Best run in the simulator at KSEZ (or any field with two distinct runway ends).

| Step | Action | Expected Result | Result | Notes |
|------|--------|----------------|--------|-------|
| 6.7.5.1 | In flight or sim, tap CDI strip → type a known airport ident with runway data (e.g. `KSEZ`) | Keyboard shows the ident; bottom row of nav-extras includes an **APPR** button (only visible when runway data is loaded for the typed ident) | | |
| 6.7.5.2 | Tap **APPR** | Runway picker opens with one tile per runway end, each showing ident, course, and length | | |
| 6.7.5.3 | Tap a runway tile (e.g. RWY 03 at KSEZ) | Approach activates; CDI ident readout becomes `KSEZ/03`; magenta D2 trace disappears from AI | | |
| 6.7.5.4 | Observe AI for HITS boxes | Cyan rectangular boxes (300×200 ft) draw along the extended centreline at 1000 ft spacing, starting ~1000 ft outside the threshold and continuing to 5 NM final | | |
| 6.7.5.5 | Observe HITS occlusion | Boxes are correctly occluded by intervening ridges / obstacles (depth-tested against SVT mesh) | | |
| 6.7.5.6 | Observe VDI on right side of AI | Vertical bar with a magenta diamond appears just inside the alt tape; only paints with approach active; `G` and `S` markers visible above and below the bar | | |
| 6.7.5.7 | Climb above the 3° GS | VDI diamond moves DOWN — fly down to the diamond | | |
| 6.7.5.8 | Descend below the GS | Diamond moves UP — fly up to the diamond | | |
| 6.7.5.9 | Observe CDI scaling | Full-scale deflection visibly tighter than en-route mode (now ±0.3 NM, was ±1.0 NM); ident readout still shows `KSEZ/03` | | |
| 6.7.5.10 | Verify CDI reference is the extended centreline | Move the simulator laterally across the centreline; diamond crosses centre at the extended runway centreline, NOT at the line from the original activation point to the threshold | | |
| 6.7.5.11 | Observe lower-left moving-map inset | Course trace is **cyan** (matches HITS / VDI), drawn from the threshold along the reciprocal of the published course; ETE label in the corner is also cyan | | |
| 6.7.5.12 | Open Setup → AIRPORTS → DIRECT TO → APPR (or re-tap CDI → APPR) | Runway picker opens with **CANCEL APPROACH** button at the bottom | | |
| 6.7.5.13 | Tap **CANCEL APPROACH** | HITS boxes disappear; VDI hides; CDI rescales to ±1 NM; ident readout drops the runway suffix; inset trace returns to magenta | | |

---

## Phase 6.7.6 — Sim AP Follow Modes (FOLLOW BUGS / FLT PLAN)

Requires the simulator running with a direct-to or synthetic approach active.

| Step | Action | Expected Result | Result | Notes |
|------|--------|----------------|--------|-------|
| 6.7.6.1 | Sim at KSEZ, set HDG bug 270°, FOLLOW = BUGS (default) | AP turns to and holds 270° regardless of any active D2 / approach | | |
| 6.7.6.2 | Tap SIM watermark → SIM CONTROLS → FOLLOW row → **FLT PLAN** | Button highlights; HDG bug commanded value is now overridden by intercept logic | | |
| 6.7.6.3 | With a D2 to a waypoint 30 NM out, position aircraft 2 NM right of course | AP commands a heading roughly 45° left of course (full intercept) | | |
| 6.7.6.4 | Continue tracking; cross-track decreases through 1.5 NM, then 0.3 NM | Intercept angle linearly reduces; inside 0.3 NM the AP rolls out wings-level on track | | |
| 6.7.6.5 | Activate a synthetic approach (Phase 6.7.5) | Intercept tuning tightens: full-intercept threshold becomes 0.5 NM, gentle band 0.1 NM, inner-band gain triples — AP visibly settles on centreline at the new ±0.3 NM CDI scale | | |
| 6.7.6.6 | Coordinated-turn check: observe AI during any intercept | Bank command precedes any heading change. Wings level → no yaw motion. The AI airplane never flat-yaws (no heading scroll without a visible bank). | | |
| 6.7.6.7 | At ~25° bank command, verify yaw rate ≈ `g·tan(25°)/V` ≈ 5°/s at 100 kt | Sim turns roughly one full circle in ~70 s at full bank | | |
| 6.7.6.8 | GS-capture-from-above check: with approach active, climb the sim above the GS, then descend toward it | AP captures the diamond on first crossing; descent rate tracks `V × tan(3°)` ≈ 530 fpm at 100 kt without persistent lag below the diamond | | |
| 6.7.6.9 | Below-GS check: position sim below the GS by 200 ft and let it run | AP holds altitude (does NOT command a climb to the GS); diamond drifts down toward the aircraft as distance closes; capture from above resumes when the diamond reaches the aircraft | | |
| 6.7.6.10 | Tap SIM CONTROLS → **EXIT SETUP** | SIM CONTROLS overlay closes; sim continues running (this verifies the EXIT SETUP button is wired — pre-fix the overlay had no exit affordance) | | |

---

## Phase 6.8 — AGL Readout

| Step | Action | Expected Result | Result | Notes |
|------|--------|----------------|--------|-------|
| 6.8.1 | With GPS fix and SRTM tiles loaded, observe lower-right of AI | Box visible: small "AGL" label on top, value below, light-grey border | | |
| 6.8.2 | At a known airport elevation, compare reading to (real_alt − field_elev) | Within ±20 ft of expected | | |
| 6.8.3 | At cruise altitude over varied terrain, verify AGL number tracks terrain elevation changes | Number changes as terrain below the aircraft changes | | |
| 6.8.4 | Sit on the runway, simulate baro setting low by 30 ft | AGL reads "---" instead of negative | | |
| 6.8.5 | Disable GPS (Setup → SIM CONTROLS → GPS FAIL or unplug GPS) | AGL box hides | | |
| 6.8.6 | Re-enable GPS but at a location with no SRTM tile | AGL box hides | | |

---

## Phase 6.9 — Autostart on Boot

| Step | Action | Expected Result | Result | Notes |
|------|--------|----------------|--------|-------|
| 6.9.1 | Confirm `pfd.service` is enabled: `sudo systemctl is-enabled pfd.service` | Output: `enabled` | | |
| 6.9.2 | Power-cycle the Pi 4 (pull power, restore power) | Pi boots; PFD launches automatically — display shows the AI within ~10–20 s of power-on with no manual intervention required | | |
| 6.9.3 | `sudo systemctl status pfd.service` | Active (running) | | |
| 6.9.4 | If the unit is stale (e.g. after pulling a fix to the env vars), refresh with `sudo bash tools/install_autostart.sh`. Verify the script reports "Done" and that `systemctl status` shows the new unit running | Service refreshed without reinstalling apt/pip dependencies | | |
| 6.9.5 | Test crash recovery: SSH in and `sudo killall python3` (or `kill <pid>`) | Service restarts within 5 s (Restart=always, RestartSec=5) | | |

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
| Phase 5.6 — Drum cascade | Y / N | | |
| Phase 6 — AI overlays | Y / N | | |
| Phase 6.5 — AHRS orientation | Y / N | | |
| Phase 6.6 — Compass cal wizard | Y / N | | |
| Phase 6.7 — Direct-to navigation | Y / N | | |
| Phase 6.7.5 — Synthetic approach (HITS + VDI) | Y / N | | |
| Phase 6.7.6 — Sim AP follow modes / intercept / GS capture | Y / N | | |
| Phase 6.8 — AGL readout | Y / N | | |
| Phase 6.9 — Autostart on boot | Y / N | | |
| Phase 7 — Settings persistence | Y / N | | |

---

*TP-PI4-001 v0.3 — covers main with synthetic-approach feature set landed. For the Pi Zero 2W variant see TP-ZERO-001.*
