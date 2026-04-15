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
├── display/           # Pico W browser-based preview UI
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

### Pico W sensor board (shared)
| Part | Notes |
|------|-------|
| Raspberry Pi Pico W | Any revision |
| WitMotion WT901 AHRS | UART, 9-DOF IMU |
| u-blox NEO-6M GPS module | UART |
| BME280 baro (optional) | I2C, improves altitude accuracy |
| MAX3232 breakout | For future TruTrak autopilot RS-232 |

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
├── config.py          ← edit WiFi credentials and pin assignments here
├── web_server.py
├── wt901.py
├── gps.py
└── bme280.py          ← only needed if BME280 is connected
```

### Edit config.py

```python
AP_SSID     = "PFD_AP"      # must match wifi_switch.sh PICO_SSID
AP_PASSWORD = "picoahrs1"   # must match wifi_switch.sh PICO_PSK
```

### Wiring

#### WT901 AHRS → Pico W
| WT901 | Pico W | Notes |
|-------|--------|-------|
| VCC (5V) | VBUS (pin 40) | 5V from USB |
| GND | GND (pin 38) | |
| TX | GP1 (pin 2) | UART0 RX |
| RX | GP0 (pin 1) | UART0 TX |

#### NEO-6M GPS → Pico W
| NEO-6M | Pico W | Notes |
|--------|--------|-------|
| VCC | 3V3 (pin 36) | |
| GND | GND | |
| TX | GP5 (pin 7) | UART1 RX |
| RX | GP4 (pin 6) | UART1 TX |

#### BME280 (optional) → Pico W
| BME280 | Pico W | Notes |
|--------|--------|-------|
| VCC | 3V3 | |
| GND | GND | |
| SDA | GP2 (pin 4) | I2C1 |
| SCL | GP3 (pin 5) | I2C1 |

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
| `Docs/USER_MANUAL_ZERO.md` | Pi Zero 2W pilot's user manual |
| `Docs/USER_MANUAL_PI4.md` | Pi 4 pilot's user manual |
| `Docs/TEST_PROCEDURE_ZERO.md` | Pi Zero 2W bench test procedure (TP-ZERO-001) |
| `Docs/TEST_PROCEDURE_PI4.md` | Pi 4 bench test procedure (TP-PI4-001) — adds an OpenGL SVT phase |

---

## AHRS Trim Calibration

If the horizon is not level on the ground, adjust the trim offsets via the on-screen setup menu:

1. Two-finger hold anywhere on the PFD for 0.8 s to open the setup menu
2. Tap **AHRS / SENSORS**
3. Adjust **PITCH TRIM** and **ROLL TRIM** with the − / + buttons (0.5° steps)
4. Tap **EXIT** to return to the PFD — changes take effect immediately

If the Pico W sensor board is mounted upside-down, set **MOUNTING** to **INVERTED** on the same screen.

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
| V5 | 100 Hz WT901 + magnetic deviation calibration |
| V6 | TruTrak Vizion RS-232 autopilot interface |
| V7 | Moving map / MFD (separate dedicated hardware unit) |
| V8 | Flight path vector, highway-in-the-sky waypoint tunnel |
| V9 | Time-of-day sun position, texture-mapped terrain |
