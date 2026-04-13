# AHRS PFD — Dual Display System

A two-board avionic display system with **two display versions**:

| Board | Role |
|-------|------|
| **Pico W** | Reads WT901 AHRS, GPS, BME280 baro. Serves data via Wi-Fi SSE |
| **Pi Zero 2W** | Lightweight PFD — plain horizon, TAWS alerting, no SVT |
| **Pi 4** | Full PFD — OpenGL vector graphics with true 3D Synthetic Vision Terrain |

Both display versions connect to the same Pico W AHRS unit over Wi-Fi SSE.

---

## Repository Structure

```
PFD-and-AHRS/
├── firmware/          # Pico W AHRS sensor firmware (shared by both displays)
├── shared/            # Common Python modules (SSE client, terrain loader, obstacles)
├── pi_zero/           # Pi Zero 2W display version (no SVT)
│   ├── pfd.py         # Pygame-based PFD renderer — plain horizon
│   ├── config.py      # Zero-specific display config
│   ├── setup.sh       # One-shot install for Pi Zero 2W
│   ├── data/          # SRTM tiles (TAWS only) + obstacles
│   └── previews/      # Screenshots for this version
├── pi4/               # Pi 4 display version (full SVT)
│   ├── pfd.py         # PFD renderer with full SVT terrain
│   ├── svt_renderer.py # SVT terrain renderer (pygame now, OpenGL planned)
│   ├── config.py      # Pi 4 display config
│   ├── setup.sh       # One-shot install for Pi 4
│   ├── data/          # SRTM tiles + obstacles
│   └── previews/      # Screenshots for this version
├── pi_display/        # Original combined codebase (preserved for reference)
├── display/           # Pico W browser-based preview UI
├── tools/             # Preview image generator
├── Docs/              # Requirements, user manuals, test procedures
└── README.md
```

---

## Display Version Comparison

| Feature | Pi Zero 2W | Pi 4 |
|---------|-----------|------|
| Processor | ARM Cortex-A53, 512 MB RAM | ARM Cortex-A72, 2–8 GB RAM |
| Graphics | Pygame / SDL2 framebuffer | OpenGL ES vector graphics |
| SVT terrain background | No — plain sky/ground horizon | Yes — full 3D perspective |
| Terrain above horizon | No | Yes — mountain peaks visible |
| TAWS alerting (banners) | Yes | Yes |
| Obstacle symbols on AI | Yes | Yes |
| Display resolution | TBD | TBD (higher) |
| Target frame rate | 30 fps | 30 fps |
| All other PFD features | Identical | Identical |

Both versions share the same AHRS unit, instrument layout, menus, simulator, demo mode, and touch interface.

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
| Display | TBD |
| 64 GB microSD (Class 10 / A1) | Raspberry Pi OS Lite 64-bit |
| USB-C power (5V 2A+) | |

### Pi 4 display
| Part | Notes |
|------|-------|
| Raspberry Pi 4 | 2 GB+ RAM recommended |
| Display | TBD (higher resolution) |
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

### 3. Download terrain tiles (for TAWS alerting)

```bash
bash fetch_sedona_tiles.sh
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

Same as above, but use a Pi 4 with hostname `pfd4`.

### 2. Clone and install

```bash
ssh pi@pfd4.local
sudo apt update && sudo apt install git -y
git clone https://github.com/HBDrew/PFD-and-AHRS.git
cd PFD-and-AHRS
sudo bash pi4/setup.sh
```

### 3. Download terrain tiles (for SVT + TAWS)

```bash
bash fetch_sedona_tiles.sh
```

### 4. Test demo mode

```bash
python3 pi4/pfd.py --demo --sim
```

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
| Tap heading tape | Set heading bug to tapped position |
| Tap bottom-left of heading strip | Open heading bug numpad |
| Tap bottom-right of heading strip | Open baro setting numpad |
| Two-finger hold (0.8 s) | Open setup menu |
| (keyboard) D | Toggle demo mode |
| (keyboard) Esc | Quit |

See `Docs/USER_MANUAL_ZERO.md` or `Docs/USER_MANUAL_PI4.md` for full operational documentation.

---

## Documentation

| Document | Description |
|----------|-------------|
| `Docs/REQUIREMENTS_AHRS.md` | AHRS unit high-level requirements (shared) |
| `Docs/REQUIREMENTS_DISPLAY_ZERO.md` | Pi Zero 2W display HLRs (no SVT) |
| `Docs/REQUIREMENTS_DISPLAY_PI4.md` | Pi 4 display HLRs (full SVT + OpenGL) |
| `Docs/USER_MANUAL_ZERO.md` | Pi Zero 2W pilot's user manual |
| `Docs/USER_MANUAL_PI4.md` | Pi 4 pilot's user manual |
| `Docs/TEST_PROCEDURE_001.md` | Bench test procedure |

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
| V4 | Pi 4 OpenGL vector graphics + true 3D SVT |
| V5 | 100 Hz WT901 + magnetic deviation calibration |
| V6 | TruTrak Vizion RS-232 autopilot interface |
| V7 | Aviation database, moving map, synthetic runway |
