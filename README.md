# AHRS PFD — Dual Display System

A two-board avionic display system with **two display versions** that share a single AHRS source:

| Board | Role |
|-------|------|
| **Pico W** | Reads IMU (ICM-42688-P or WT901), GPS, BME280 baro. Serves state to the display over Wi-Fi SSE |
| **Pi Zero 2W** | Waveshare 3.5" DPI 640×480 PFD — plain horizon, TAWS alerting, airport/runway overlays, no SVT |
| **Pi 4** | 1024×600 PFD — true 3D Synthetic Vision Terrain rendered through OpenGL ES + same overlays |

Both displays connect to the same Pico W AHRS unit over Wi-Fi SSE. The feature set (airports, runways, extended centerlines, obstacles, TAWS, persistence, simulator, demo mode) is identical across both — the Pi 4 adds the OpenGL SVT terrain background with mountains that rise above the horizon line.

---

## Repository Structure

```
PFD-and-AHRS/
├── firmware/          # Pico W AHRS sensor firmware (shared by both displays)
├── shared/            # Common Python modules
│   ├── sse_client.py  # SSE streaming client
│   ├── terrain.py     # SRTM tile loader + TAWS clearance query
│   ├── obstacles.py   # FAA DOF parser + spatial query
│   ├── airports.py    # OurAirports parser + spatial query
│   ├── runways.py     # OurAirports runways parser + spatial query
│   ├── settings.py    # Debounced atomic JSON settings persistence
│   └── config_base.py # SIM_PRESETS + shared constants
├── pi_zero/           # Pi Zero 2W display version (no SVT)
│   ├── pfd.py         # Pygame PFD — plain horizon
│   ├── config.py      # 640×480 display config
│   ├── setup.sh       # One-shot install for Pi Zero 2W
│   ├── data/          # SRTM tiles (TAWS) + obstacles + airports + settings.json
│   └── previews/      # Screenshots for this version
├── pi4/               # Pi 4 display version (full SVT)
│   ├── pfd.py         # PFD with OpenGL SVT terrain background
│   ├── svt_renderer.py    # Pygame SVT fallback (when EGL unavailable)
│   ├── svt_renderer_gl.py # OpenGL ES 3.0 SVT renderer (moderngl + EGL)
│   ├── render_pfd_offline.py # Offline preview PNG generator
│   ├── config.py      # Display profile (waveshare_35 / roadom_7)
│   ├── setup.sh       # One-shot install for Pi 4
│   ├── data/          # SRTM tiles + obstacles + airports + settings.json
│   └── previews/      # Screenshots (setup screens + pfd_gl/ OpenGL flight scenes)
├── pi_display/        # Original combined codebase (preserved for reference)
├── iphone_display/    # iPhone / browser PFD (served over AHRS-Link AP by the Pico W)
├── tools/             # Preview image generator
├── fetch_sedona_tiles.sh   # Quick-start: download KSEZ area SRTM tiles
├── fetch_airports.sh       # Download OurAirports CSV + runways CSV
├── wifi_switch.sh          # Toggle Pi between home WiFi and Pico W AP
├── Docs/              # Requirements, user manuals, test procedures
└── README.md
```

---

## Display Version Comparison

| Feature | Pi Zero 2W | Pi 4 |
|---------|-----------|------|
| Processor | ARM Cortex-A53, 512 MB RAM | ARM Cortex-A72, 2–8 GB RAM |
| Display | Waveshare 3.5" DPI LCD (640×480, 40-pin GPIO parallel RGB, I2C cap touch, PWM backlight) | ROADOM 7" HDMI IPS (1024×600, USB cap touch) — or Waveshare 3.5" DPI as a fallback |
| Graphics | Pygame / SDL2 framebuffer | Pygame UI + OpenGL ES 3.0 SVT (via moderngl/EGL) |
| SVT terrain background | No — plain sky/ground horizon | Yes — full 3D perspective mesh |
| Terrain above horizon | No | Yes — mountain peaks + ridges rise above horizon line |
| Sun-angle shading + distance grid | No | Yes |
| TAWS caution / warning banners | Yes | Yes |
| Obstacle symbols (caret + lit star + R/Y/W height colour) | Yes | Yes |
| Airport symbols (public ring / heli H / seaplane / balloon) | Yes | Yes |
| Road-sign airport labels on posts | Yes | Yes |
| Runway polygons (within 8 NM) | Yes | Yes |
| Extended dashed centerlines (within 15 NM) | Yes | Yes |
| Type filters (PUBLIC / HELI / SEAPLANE / OTHER) | Yes | Yes |
| Overlay toggles (RUNWAYS / EXT C/LINES) | Yes | Yes |
| Settings persistence (atomic JSON, debounced writer) | Yes | Yes |
| Built-in flight simulator (12 presets + failure injection) | Yes | Yes |
| Demo mode (scripted Sedona flight) | Yes | Yes |
| Target frame rate | 30 fps | 30 fps |

Both versions share the same AHRS unit, instrument layout, menus, simulator, demo mode, and touch interface. A single `shared/` module tree is used by both.

---

## Hardware

### AHRS PCB rev A (shared by both display variants)

The AHRS sensor head is now a single integrated PCB carrying every sensor on one board. Wiring is fixed in the board layout — no bench breakouts, no jumpers, no per-build pinout drift.

| Part | Bus | Address / Port | Role |
|------|-----|----------------|------|
| Raspberry Pi Pico W | — | USB-C (debug + power) | Host processor + WiFi AP |
| WitMotion WT901 | UART0 (GP0/GP1, 9600 baud) | — | 9-DOF IMU, fused Euler output |
| u-blox NEO-6M | UART1 (GP4/GP5, 9600 baud) | — | NMEA GPS (GGA + RMC) |
| Bosch BME280 | I²C1 (GP2/GP3, 400 kHz) | `0x76` | Static pressure + OAT |
| Sensirion SDP33-1500Pa | I²C1 (GP2/GP3) | `0x21` | Differential pressure (pitot − static) |
| *(reserved)* SDP3x AOA twin | I²C1 (GP2/GP3) | `0x22` | Future AOA probe — see `Docs/BUGS_AND_TODO.md → AOA-PROBE` |
| MAX3232 breakout | — | — | RS-232 driver for future TruTrak autopilot tie-in |

Power draw is ~130 mA at 5 V from the Pico's USB-C — typical wallwart / cigarette-lighter 5 V supply works fine; aircraft-grade builds run through a regulated 5 V converter.

Pneumatic plumbing for the air-data path:

- SDP33 `+` port → pitot tube (ram pressure)
- SDP33 `−` port → static reference (teed into the BME280's open port)

The combination produces the full pitot-static set — IAS, TAS, density altitude, and a wind solution from the AHRS heading + GPS track — broadcast on the same `$AHRS` SSE / USB JSON packet. See `Docs/REQUIREMENTS_AHRS.md §7B` for the field-level requirements and `Docs/USER_MANUAL_PI4.md §21` for the pilot-facing description.

### Pi Zero 2W display
| Part | Notes |
|------|-------|
| Raspberry Pi Zero 2W | 512 MB RAM |
| Waveshare 3.5" DPI LCD | 640×480 IPS, DPI parallel RGB via 40-pin GPIO header, 5-point I2C capacitive touch, PWM backlight on GPIO 18, device-tree overlay `waveshare-35dpi-3b-4b` |
| 64 GB microSD (Class 10 / A1) | Raspberry Pi OS Lite 64-bit |
| USB-C power (5V 2A+) | |

### Pi 4 display
| Part | Notes |
|------|-------|
| Raspberry Pi 4 | 2 GB+ RAM recommended |
| ROADOM 7" HDMI IPS | 1024×600, HDMI video + USB capacitive touch (no software backlight control). Alternative: ROADOM 10" same electronics, or Waveshare 3.5" DPI as a Pi 4 fallback (`DISPLAY_PROFILE = "waveshare_35"`). |
| 64 GB microSD (Class 10 / A1) | Raspberry Pi OS Lite 64-bit |
| USB-C power (5V 3A) | |

---

## Quick Start — Pi Zero 2W

### 1. Flash SD card

Download **Raspberry Pi OS Lite (64-bit)**. Use **Raspberry Pi Imager** with SSH enabled, hostname `pfd`, and your Wi-Fi credentials.

### 2. Clone and install

```bash
ssh pi@pfd.local
sudo apt update && sudo apt install git -y
git clone https://github.com/HBDrew/PFD-and-AHRS.git
cd PFD-and-AHRS
sudo bash pi_zero/setup.sh
```

### 3. Download runtime data

```bash
bash fetch_sedona_tiles.sh        # SRTM tiles for KSEZ area (TAWS)
bash fetch_airports.sh pi_zero    # OurAirports airports + runways CSVs
# FAA obstacle data downloads in-app from Setup → System → OBSTACLES
```

### 4. Test demo mode

```bash
python3 pi_zero/pfd.py --demo --sim
```

### 5. Reboot — PFD starts automatically

```bash
sudo reboot
```

---

## Quick Start — Pi 4

### 1. Flash SD card

Same as above, but use a Pi 4 with hostname `pfd4`. Set `DISPLAY_PROFILE` in `pi4/config.py` to match the connected panel (`roadom_7` for 1024×600 HDMI, `waveshare_35` for 640×480 DPI).

### 2. Clone and install

```bash
ssh pi@pfd4.local
sudo apt update && sudo apt install git -y
git clone https://github.com/HBDrew/PFD-and-AHRS.git
cd PFD-and-AHRS
sudo bash pi4/setup.sh
```

### 3. Download runtime data

```bash
bash fetch_sedona_tiles.sh        # SRTM tiles — powers TAWS and the SVT terrain mesh
bash fetch_airports.sh pi4        # OurAirports airports + runways CSVs
# FAA obstacle data downloads in-app from Setup → System → OBSTACLES
```

### 4. Test demo mode

```bash
python3 pi4/pfd.py --demo --sim
```

You should see the SVT terrain mesh behind the attitude indicator with cyan distance grid and sun-angle shading. If the console logs `SVT_RENDERER: opengl  GL_AVAILABLE: True` you're running the full OpenGL path; a `pygame` fallback indicates EGL couldn't initialise.

### 5. Reboot — PFD starts automatically

```bash
sudo reboot
```

---

## Pico W — Setup

### Copy firmware files

Connect Pico W via USB. Copy the `firmware/` folder contents to the Pico root (using Thonny, rshell, or mpremote):

```
firmware/
├── main.py
├── config.py          ← WiFi credentials, pin map, sensor enables
├── web_server.py
├── wt901.py
├── gps.py
├── bme280.py          ← BME280 (static pressure + OAT)
├── sdp31.py           ← SDP33-1500Pa differential pressure (IAS)
└── airdata.py         ← IAS / TAS / density alt / wind triangle
```

### Edit config.py

```python
AP_SSID     = "PFD_AP"      # must match wifi_switch.sh PICO_SSID
AP_PASSWORD = "picoahrs1"   # must match wifi_switch.sh PICO_PSK
```

`config.py` also carries the sensor enable flags (`BME280_ENABLE`, `SDP31_ENABLE`) so a bench Pico W without one or both sensors still boots cleanly — the firmware sets `baro_ok` / `airdata_ok = False` and the displays fall back automatically.

### Wiring — AHRS PCB rev A

The pin map matches the [hardware table](#ahrs-pcb-rev-a-shared-by-both-display-variants) above. On the PCB build everything is already wired; on the bench-breakout build see the [Appendix](#appendix-bench-breakout-wiring) below.

---

## Flight Mode Workflow

### Switch Pi to Pico W AP
```bash
sudo bash wifi_switch.sh flight
```

### Switch Pi to home WiFi (for updates/terrain)
```bash
sudo bash wifi_switch.sh home
```

### Check current WiFi
```bash
bash wifi_switch.sh status
```

---

## Touchscreen Controls

| Action | Effect |
|--------|--------|
| Tap altitude tape | Set altitude bug to tapped position |
| Tap top of alt tape | Open altitude bug numpad |
| Tap top of speed tape | Open speed bug numpad |
| Tap heading tape | Set heading bug to tapped position |
| Tap bottom-left of heading strip | Open heading bug numpad |
| Tap bottom-right of heading strip | Open baro setting numpad |
| Two-finger hold (0.8 s) | Open setup menu |
| Setup → System → AIRPORTS | Airport data screen (filters, runway/centerline toggles, UPDATE) |
| Tap SIM watermark (during sim) | Open SIM CONTROLS overlay (failure injection + EXIT) |
| (keyboard) D | Toggle demo mode |
| (keyboard) Esc | Quit |

See `Docs/USER_MANUAL_ZERO.md` or `Docs/USER_MANUAL_PI4.md` for full operational documentation. Filter states, bug values, brightness, baro unit, and every other user-set value persist across power cycles in `data/settings.json`.

---

## Documentation

| Document | Description |
|----------|-------------|
| `Docs/REQUIREMENTS_AHRS.md` | AHRS unit high-level requirements (shared) |
| `Docs/REQUIREMENTS_DISPLAY_ZERO.md` | Pi Zero 2W display HLRs (no SVT) |
| `Docs/REQUIREMENTS_DISPLAY_PI4.md` | Pi 4 display HLRs (full SVT + OpenGL) |
| `Docs/USER_MANUAL_IPHONE.md` | iPhone / browser PFD pilot's user manual |
| `Docs/USER_MANUAL_ZERO.md` | Pi Zero 2W pilot's user manual |
| `Docs/USER_MANUAL_PI4.md` | Pi 4 pilot's user manual |
| `Docs/TEST_PROCEDURE_ZERO.md` | Pi Zero 2W bench test procedure (TP-ZERO-001) |
| `Docs/TEST_PROCEDURE_PI4.md` | Pi 4 bench test procedure (TP-PI4-001) — adds an OpenGL SVT phase |

---

## AHRS Setup

Configure how the AHRS box is physically mounted in the aircraft, fine-trim residual offsets, and run the compass calibration wizard. All on the same screen:

**Setup → AHRS / SENSORS**

| Row | Control | What it does |
|-----|---------|--------------|
| PITCH TRIM | ± steppers (0.1° per tap) | Fine-trim if the horizon sits above/below level on the ground |
| ROLL TRIM | ± steppers (0.1° per tap) | Fine-trim if a wing reads low on the ground |
| MAGNETOMETER | CALIBRATE button | Opens the 8-point compass-cal wizard (N / NE / E / SE / S / SW / W / NW). Cal table is stored *on the AHRS* in flash, shared by every display |
| ORIENTATION | FWD / LEFT / RIGHT / AFT | Which side of the AHRS the connector points toward, viewed from the pilot's seat. Default is RIGHT. Stored on the AHRS (`orient.json` on Pico flash) |
| MOUNTING | NORMAL / INVERTED | Whether the AHRS is right-side-up or upside-down. Independent of orientation. Stored on the AHRS |
| HEADING SOURCE | MAG / TRK / AUTO | Magnetometer, GPS ground track, or auto-select (TRK in motion, MAG when stationary) |
| AIRSPEED SOURCE | GPS GS / IAS SENSOR | IAS SENSOR by default — SDP33-1500Pa with BME280 density correction; falls back to GPS groundspeed when the air-data path reports unhealthy |

Trim, orientation, mounting, and the compass cal all combine cleanly — orientation remaps pitch/roll axes for a non-RIGHT mounting, mounting flips for upside-down, the cal corrects the residual mag bias, and trim is the final fine-tuning. Sim and Demo modes bypass every AHRS-mounting compensation so a calibrated trim doesn't show up as wing-down on a level sim flight.

### Compass Calibration

Tap CALIBRATE to open the 8-point walk-through wizard:

1. Point the aircraft's nose **NORTH** (000°) and tap **⊕ CAPTURE N**.
2. **NE** (045°), tap CAPTURE NE.
3. **EAST** (090°), tap CAPTURE E.
4. **SE** (135°), tap CAPTURE SE.
5. **SOUTH** (180°), tap CAPTURE S.
6. **SW** (225°), tap CAPTURE SW.
7. **WEST** (270°), tap CAPTURE W.
8. **NW** (315°), tap CAPTURE NW.

The wizard builds a 36-point deviation table (linear interpolation between the 8 captures, one slot per 10°) and pushes it to the AHRS over USB serial (`$MAGDEV,<36 floats>`) or the Wi-Fi config endpoint (`GET /magcal?action=set&t=…`). The Pico W writes the table to `magdev.json` on its flash and applies it before broadcasting yaw — so every display (Pi 4, Pi Zero, iPhone PWA) reads the same calibrated heading off the SSE / `$AHRS` stream without per-display calibration. The Pi 4 also keeps a belt-and-braces local copy in `pi4/data/settings.json` (`pi4_magdev`) that it applies on top of the broadcast yaw if the push to the Pico fails. AHRS orientation + mounting are persisted the same way (`orient.json` on Pico flash, via `$ORIENT,connector,mounting`).

Buttons:
- **EXIT** — close the modal (the cal is committed on the 8th capture, so this is non-destructive). Reads CANCEL only mid-walk when there are partial captures still in flight to the AHRS.
- **RESET** — wipe the stored cal back to zero (sends `$MAGDEV,CLEAR` to the AHRS).
- **RESTART** — abandon partial captures and start the eight-capture sequence over.
- **⊕ CAPTURE X** — record the current heading at the cardinal / intercardinal indicated by X (N / NE / E / SE / S / SW / W / NW).

The status row on the AHRS Setup screen shows `max |Δ|` (worst-case residual across the eight captures) so a glance tells you whether the cal is current.

The cal only shifts MAG-mode display — TRK mode is unaffected because the complementary filter operates on yaw deltas, where a constant-shape correction's frame-to-frame derivative is negligible.

---

## Direct-to Navigation

Tap the empty **CDI strip** above the heading box (or **DIRECT TO** on the AIRPORT DATA screen) to open the on-screen keyboard for waypoint entry.

| Action | Effect |
|--------|--------|
| Type ident → ENTER | Look up the airport. If found, prompt "Activate Direct to *XXXX*?" with ACTIVATE / CANCEL. Two-tap commit prevents accidental flight-plan edits. |
| Hit ENTER on empty buffer (with an active waypoint) | Re-activate the existing waypoint. Use this to refresh the magenta course line from your current position without retyping. |
| Type unknown ident → ENTER | Keyboard stays open with a red "UNKNOWN WAYPOINT *XXXX*" hint. Backspace or any keystroke clears the error so you can correct in place. |
| **DIRECT TO *XXXX*** button (on tall keyboards) | Resolves and shows the nearest public airport's ident on the button itself, then routes through the same Activate? confirmation. |
| **CANCEL FLIGHT PLAN** | Wipes the active direct-to. |

After activation, a magenta course-trace line draws from your activation point to the destination, **draped over the SRTM terrain mesh**. The trace is sampled in a background thread (every 0.2 NM) so the line shows up immediately at the near end and grows outward toward the waypoint as the worker walks the SRTM tiles — no UI freeze even on long cross-country courses.

The **CDI** strip above the heading box shows ident · bearing · distance and a magenta XTK diamond at full-scale ±1 NM.

---

## AGL Readout

A small "AGL" box in the lower-right corner of the AI shows your real altitude above the SRTM terrain at your current lat/lon — independent of baro, calculated from real GPS / sensor altitude minus the local ground elevation. Reads dashes when at or below ground level (sensor / DEM disagreement). Hidden when there's no GPS fix or the SRTM lookup returns "missing tile".

---

## V-Speed Configuration

V-speeds are set via the on-screen setup menu and take effect immediately on the speed tape:

1. Two-finger hold anywhere on the PFD for 0.8 s to open the setup menu
2. Tap **FLIGHT PROFILE**
3. Tap any V-speed field and enter the value with the numpad

Default values (Cessna 172S) are restored by tapping **RESET DEFAULTS** on the Flight Profile screen, or via **System → RESET DEFAULTS**.

---

## Troubleshooting

### PFD screen blank on boot
```bash
sudo systemctl status pfd.service
sudo journalctl -u pfd.service -n 50
```

### Display wrong orientation / resolution
Check `/boot/firmware/config.txt` — `setup.sh` should have added the framebuffer settings.

### "NO LINK" badge on PFD — can't connect to Pico W
1. Check Pi is on Pico W AP: `bash wifi_switch.sh status`
2. Check Pico W is powered and booted: LED should be blinking
3. Verify `config.py` AP_SSID matches `wifi_switch.sh` PICO_SSID
4. Try: `curl http://192.168.4.1/state` from the Pi

### "AHRS FAIL" on display
- WT901 UART wiring (check TX→RX cross)
- WT901 baud rate (default 9600 in `config.py`)
- Allow 5 seconds after Pico boot for sensor to initialise

### No GPS fix
- GPS needs open-sky view; initial fix can take 2–5 minutes cold-start
- Check NEO-6M LED: 1 Hz blink = fix acquired, fast blink = searching

### Terrain not showing in demo (Pi 4 only)
```bash
bash fetch_sedona_tiles.sh
```
After downloading, restart PFD: `sudo systemctl restart pfd.service`

If the Pi 4 SVT falls back to the flat blue/brown split even with tiles present, check the startup console for `SVT_RENDERER: opengl  GL_AVAILABLE: True`. If it says `GL_AVAILABLE: False`, the EGL context failed to create — typically a GPU memory allocation issue. Check `gpu_mem=256` is set in `/boot/firmware/config.txt`.

### Airport symbols / runways / centerlines not appearing
```bash
bash fetch_airports.sh           # downloads to both pi_zero and pi4
bash fetch_airports.sh pi4       # just pi4
```
Check that the RUNWAYS / EXT C/LINES toggles on the AIRPORT DATA screen are enabled (they persist across power cycles in `settings.json`).

### Settings don't persist across reboots
`data/settings.json` is written atomically on a background thread with a 1.5 s debounce. If a setting isn't persisting, wait 3+ seconds before power-cycling. Verify the file exists and is readable:
```bash
cat pi4/data/settings.json     # (or pi_zero/)
```
The Wi-Fi password is intentionally not persisted — this is not a bug.

---

## Updating

```bash
sudo bash wifi_switch.sh home   # get on internet
cd ~/PFD-and-AHRS
git pull
sudo systemctl restart pfd.service
```

---

## Autostart on Boot

The `pi4/setup.sh` and `setup.sh` installers both create and enable a `pfd.service` systemd unit so the PFD launches automatically on every power-up — no keyboard, no SSH, no manual command. If you've already installed but want to refresh just the service definition (for example after pulling a fix to the unit's environment), run the standalone helper:

```bash
sudo bash tools/install_autostart.sh
```

This writes a clean unit, reloads systemd, and starts the service. Common control commands:

```bash
sudo systemctl status  pfd.service     # current state + last few log lines
sudo journalctl -u     pfd.service -n 100 --no-pager   # recent log
sudo systemctl restart pfd.service     # bounce
sudo systemctl stop    pfd.service     # take it down (until reboot)
sudo systemctl disable pfd.service     # don't auto-start next boot
```

The unit uses `SDL_VIDEODRIVER=kmsdrm`, `SupplementaryGroups=video render input`, and `StartLimitIntervalSec=0` so the service can grab the framebuffer cleanly and keeps retrying after transient crashes instead of giving up.

---

## Branch Model

This repository carries two independent device timelines on the remote:

| Branch | Targets | Notes |
|--------|---------|-------|
| `main` | Pi 4 + Pi Zero 2W displays | Default branch. The displays auto-pull this on update. |
| `iphone-main` | Browser / iPhone display under `iphone_display/` | Independent history — used for in-flight iPhone testing without disturbing the dedicated displays. |

Shared work (firmware, `shared/` modules, docs touched by both) is generally applied twice — once on each branch — rather than merging across the timelines. The two branches can be merged later if/when the device feature sets converge.

---

## Roadmap

| Phase | Feature |
|-------|---------|
| ✅ V1 | AHRS PFD — Pico W + phone browser display |
| ✅ V2 | Pi Zero 2W dedicated display with SVT terrain |
| ✅ V3 | Split into Pi Zero 2W (no SVT) and Pi 4 (full SVT) versions |
| ✅ V4 | Pi 4 OpenGL ES SVT with sun shading, distance grid, 3D terrain above horizon |
| ✅ V4.1 | Airport database (OurAirports, 72k airports) + type filters + road-sign labels |
| ✅ V4.2 | FAA obstacle database + caret symbols with R/Y/W height colour coding |
| ✅ V4.3 | Runway polygons + extended dashed centerlines with toggles |
| ✅ V4.4 | User settings persistence (atomic JSON, debounced writer) |
| ✅ V4.5 | Runway forward-clip + per-corner anchoring so the polygon stays visible during taxi / fly-over and never wraps as a phantom across the AI |
| ✅ V4.6 | Obstacle airport-boundary clutter filter — hides terminal / ramp infrastructure under 50 ft AGL within 1 NM of any runway centroid; true MSL labels |
| ✅ V4.7 | AGL readout (lower-right of AI) + magenta direct-to course trace draped over SRTM terrain (async, progressive build) |
| ✅ V4.8 | Direct-to nav with on-screen keyboard, "Activate Direct to XXXX?" confirmation modal, NEAREST quick-button showing the resolved ident |
| ✅ V4.9 | AHRS 4-way mounting orientation (FWD / LEFT / RIGHT / AFT) with magnetic offset, ENU→NED base correction, sim/demo bypass |
| ✅ V5.0 | Compass calibration wizard — 8-point walk-through, 36-slot deviation table stored on the AHRS in flash so every display reads the same calibrated heading |
| ✅ V5.1 | AHRS PCB rev A — single-board Pico W + WT901 + NEO-6M + BME280 + SDP33-1500Pa; full pitot-static air-data set (IAS / TAS / density alt / wind triangle) on the SSE / USB stream |
| ✅ V5.2 | EGPWS-style voice callouts (TERRAIN / OBSTACLE / SINK RATE / PULL UP / BANK ANGLE), unusual-attitude recovery cues, look-ahead TAWS, ±25° forward-wedge obstacle filter |
| V6 | TruTrak Vizion RS-232 autopilot interface |
| V7 | Moving map / MFD (separate dedicated hardware unit) |
| V8 | Flight path vector, highway-in-the-sky waypoint tunnel |
| V9 | Time-of-day sun position, texture-mapped terrain |
| V10 | Setup-screen vertical scrolling for compact display profiles |

---

## Appendix — Bench breakout wiring

The original development build wired the Pico W to four separate breakout boards. The same firmware runs on the bench rig as on the PCB — the pin map below matches `firmware/config.py` and produces an identical `$AHRS` packet. Use this when bringing up a Pico without the rev A board.

### WT901 AHRS → Pico W
| WT901 | Pico W | Notes |
|-------|--------|-------|
| VCC (5V) | VBUS (pin 40) | 5 V from USB |
| GND | GND (pin 38) | |
| TX | GP1 (pin 2) | UART0 RX |
| RX | GP0 (pin 1) | UART0 TX |

### NEO-6M GPS → Pico W
| NEO-6M | Pico W | Notes |
|--------|--------|-------|
| VCC | 3V3 (pin 36) | |
| GND | GND | |
| TX | GP5 (pin 7) | UART1 RX |
| RX | GP4 (pin 6) | UART1 TX |

### BME280 → Pico W
| BME280 | Pico W | Notes |
|--------|--------|-------|
| VCC | 3V3 | |
| GND | GND | |
| SDA | GP2 (pin 4) | I²C1 (shared with SDP33) |
| SCL | GP3 (pin 5) | I²C1 (shared with SDP33) |

### SDP33-1500Pa → Pico W
| SDP33 | Pico W | Notes |
|-------|--------|-------|
| VDD | 3V3 | |
| GND | GND | |
| SDA | GP2 (pin 4) | I²C1 (shared with BME280) |
| SCL | GP3 (pin 5) | I²C1 (shared with BME280) |
| ADDR | floating | I²C address `0x21`; tie to VDD for `0x22` (AOA twin) |
| `+` port | airframe pitot | ram pressure |
| `−` port | airframe static (teed into BME280's open port) | static reference |

If your bench Pico has only some of these sensors connected, leave the corresponding `*_ENABLE` flag in `firmware/config.py` as default (the firmware probes for each sensor and gracefully falls back when one is missing — speed tape switches to GPS GS, altitude tape switches to GPS ALT, etc.).
