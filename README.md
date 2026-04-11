# AHRS PFD — Raspberry Pi Pico W + Pi Zero 2W Display

A two-board avionic display system:

| Board | Role |
|-------|------|
| **Pico W** | Reads WT901 AHRS, GPS, BME280 baro. Serves data via Wi-Fi SSE |
| **Pi Zero 2W** | Renders GI-275 inspired PFD with synthetic vision on a 640×480 DSI touchscreen |

---

## Hardware

### Pico W sensor board
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
| KLAYERS 3.5" DSI 640×480 touchscreen | IPS, capacitive touch |
| 64 GB microSD (Class 10 / A1) | Raspberry Pi OS Lite 64-bit |
| USB-C power (5V 2A+) | For Pi Zero 2W |

---

## Pi Zero 2W — First-Time Setup

### 1. Flash SD card

Download **Raspberry Pi OS Lite (64-bit)** from raspberrypi.com.

Using **Raspberry Pi Imager**:
1. Choose OS → Raspberry Pi OS (other) → Raspberry Pi OS Lite (64-bit)
2. Click ⚙ **Advanced options** before writing:
   - Enable SSH (use password auth)
   - Set hostname: `pfd`
   - Set username/password (e.g. `pi` / `pfd1234`)
   - **Configure wireless LAN** → enter your home WiFi SSID + password
   - Set locale/timezone
3. Write to SD card.

### 2. Boot and find the Pi

Insert SD, attach the DSI display cable, power on.

Find the IP address on your home network:

```bash
# From a Mac/Linux machine on the same WiFi:
ping pfd.local

# Or check your router's DHCP lease list
```

### 3. SSH in

```bash
ssh pi@pfd.local
# password: pfd1234 (or whatever you set)
```

### 4. Install git and clone the repo

Git is not pre-installed on Raspberry Pi OS Lite — install it first:

```bash
sudo apt update && sudo apt install git -y
```

Then clone:

```bash
cd ~
git clone https://github.com/HBDrew/PFD-and-AHRS.git
cd PFD-and-AHRS
```

**No internet on the Pi yet?** Alternatives:
- Download the ZIP from GitHub on your PC, copy to a USB drive, then `cp -r /media/pi/DRIVE/pfd-and-ahrs ~/`
- Use **WinSCP** (Windows) or `scp` (Mac/Linux) to transfer the folder over SSH

### 5. Run the install script

```bash
sudo bash setup.sh
```

This will:
- Install Python/pygame/numpy
- Configure the DSI display framebuffer (640×480)
- Create `pi_display/data/srtm/` directory
- Install `pfd.service` to auto-start on boot
- Add `pi_display/download_terrain.py` helper

### 6. Download Sedona terrain tiles (while on home WiFi)

```bash
bash fetch_sedona_tiles.sh
```

This downloads 4 SRTM elevation tiles (~11 MB) covering Sedona AZ for demo mode. For full US terrain coverage (~5 GB):

```bash
python3 pi_display/download_terrain.py
```

### 7. Test demo mode (no Pico W needed)

```bash
python3 pi_display/pfd.py --demo
```

You should see the GI-275 style PFD animating through Sedona flight scenarios on the DSI display.

### 8. Reboot — PFD starts automatically

```bash
sudo reboot
```

The `pfd.service` systemd unit will start `pfd.py` automatically on boot, connecting to the Pico W AP.

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

See `Docs/USER_MANUAL.md` for full operational documentation.

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

The values in `pi_display/config.py` serve as the factory defaults only — edit them there if you want a different baseline for a new installation.

---

## Baro Setting

The baro setting is adjusted from the Pico W web interface (`http://192.168.4.1`). If the BME280 is installed it drives the altimeter; otherwise GPS altitude is used.

---

## Troubleshooting

### PFD screen blank on boot
```bash
sudo systemctl status pfd.service
sudo journalctl -u pfd.service -n 50
```

### Display wrong orientation / resolution
Check `/boot/firmware/config.txt` — `setup.sh` should have added:
```
framebuffer_width=640
framebuffer_height=480
```

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

### Terrain not showing in demo
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
| V3 | 100 Hz WT901 + magnetic deviation calibration |
| V4 | TruTrak Vizion RS-232 autopilot interface |
| V5 | Aviation database, moving map, synthetic runway |
