# AHRS PFD — Raspberry Pi Pico W

A miniature Attitude & Heading Reference System that streams a forward-looking
Primary Flight Display with synthetic vision to a phone browser over local WiFi.
**No cell data or internet required.**

---

## Hardware

| Part | Notes |
|------|-------|
| Raspberry Pi Pico W | RP2040 + CYW43439 WiFi |
| WITMOTION WT901 | 9-axis AHRS (accel + gyro + mag + Kalman fusion), outputs Euler angles |
| GY-NEO6MV2 | u-blox NEO-6M GPS, NMEA UART, 1–5 Hz update |

### Why these parts?

- **WT901** does all the sensor-fusion on-chip (Kalman filter). The Pico W simply reads finished roll/pitch/yaw — no quaternion math needed on the microcontroller.
- **Pico W** is more than powerful enough: the RP2040 runs at 133 MHz, and *all rendering happens on the phone browser*. The Pico only reads UART, parses bytes, and streams tiny JSON packets at 10 Hz.
- **GY-NEO6MV2** is widely available and gives reliable position, groundspeed, track, and altitude. The 1 Hz GPS update rate is adequate for a moving map / altitude readout.

> **Future upgrade path:** swap the NEO-6M for a u-blox M8N/M9N for 10 Hz updates and better sensitivity.

---

## Wiring

```
WT901 AHRS          Pico W
──────────          ──────
VCC    ──────────►  3V3(OUT)  pin 36
GND    ──────────►  GND       pin 38
TXD    ──────────►  GP1       pin 2   (UART0 RX)
RXD    ──────────►  GP0       pin 1   (UART0 TX – optional, for config)

GY-NEO6MV2 GPS     Pico W
──────────────     ──────
VCC    ──────────►  VSYS      pin 39  (or 3V3 – check your module)
GND    ──────────►  GND       pin 38
TXD    ──────────►  GP5       pin 7   (UART1 RX)
RXD    ──────────►  GP4       pin 6   (UART1 TX – optional, for UBX config)
```

> **Note:** UART0 (GP0/GP1) is shared with USB-serial.  
> If you need USB debug output *while flying*, move the WT901 to GP12 (TX) / GP13 (RX) and update `WT901_TX_PIN` / `WT901_RX_PIN` in `firmware/config.py`.

---

## Project structure

```
pfd-and-ahrs/
├── firmware/           ← copy all .py files to Pico W root
│   ├── config.py       pin assignments, WiFi credentials, baud rates
│   ├── wt901.py        WT901 UART binary packet driver
│   ├── gps.py          NMEA parser (GPRMC + GPGGA)
│   ├── web_server.py   async HTTP + Server-Sent Events server
│   └── main.py         entry point – ties everything together
└── display/
    └── index.html      ← also copy to Pico W root as "index.html"
```

---

## Installation

### 1. Flash MicroPython

Download the latest **Raspberry Pi Pico W** build from  
https://micropython.org/download/RPI_PICO_W/

Hold **BOOTSEL**, plug in USB, drag the `.uf2` file onto the `RPI-RP2` drive.

### 2. Copy files to the Pico W

Using **Thonny** (or `mpremote`, `rshell`, `ampy`):

```
firmware/config.py      → /config.py
firmware/wt901.py       → /wt901.py
firmware/gps.py         → /gps.py
firmware/web_server.py  → /web_server.py
firmware/main.py        → /main.py
display/index.html      → /index.html
```

`main.py` runs automatically on boot.

### 3. Configure (optional)

Edit `config.py` before copying:

```python
AP_SSID     = "AHRS-PFD"   # WiFi network name
AP_PASSWORD = "ahrs1234"   # min 8 chars; "" for open network
WT901_BAUD  = 9600         # increase to 115200 after configuring the WT901
```

### 4. Connect your phone

1. Power the Pico W.  
2. Wait ~5 s for the WiFi AP to start (onboard LED blinks at 0.5 Hz).  
3. On your phone: connect to WiFi network **AHRS-PFD** (password: `ahrs1234`).  
4. Open a browser and navigate to **http://192.168.4.1**

The PFD loads immediately and starts receiving live data.

---

## Display features

| Element | Description |
|---------|-------------|
| Synthetic horizon | Sky (gradient blue) / Ground (gradient brown), rolls and pitches with the aircraft |
| Pitch ladder | ±30° with degree labels every 10°, serif ticks pointing toward horizon |
| Roll arc | Fixed arc at top; white triangle indicator rotates with roll |
| Aircraft symbol | Fixed gold wings at screen centre |
| Slip/skid ball | Simplified lateral deviation indicator |
| Speed box (left) | GPS groundspeed in knots |
| Altitude box (right) | GPS altitude in feet MSL |
| VSI (below alt) | Vertical speed in ft/min with up/down arrow |
| Heading tape (bottom) | Scrolling tape, cardinal points highlighted, current heading readout box |
| GPS status (top-left) | Fix quality and satellite count |
| Link status (top-right) | Green / amber / red based on SSE data freshness |

---

## Architecture

```
Pico W                              Phone browser
──────────────────────────────      ─────────────────────────────
WiFi AP (192.168.4.1)  ◄────────── STA (192.168.4.x)
  │
  ├─ GET /              ──────────► index.html (served once)
  │
  └─ GET /events        ──────────► EventSource SSE stream
       │                              │
       │  data: {"roll":...}\n\n      ▼
       │  10 Hz                   requestAnimationFrame render
       │                          Canvas 2D  synthetic vision
       ▼
  sensor_loop (50 Hz)
    ├─ WT901.update()   ◄── UART0 binary packets
    └─ GPS.update()     ◄── UART1 NMEA sentences
```

**Why SSE instead of WebSocket?**  
Standard Pico W MicroPython firmware may not include SHA-1 (required for the
WebSocket handshake). SSE is plain HTTP chunked transfer — no crypto needed,
same libraries that are already available, and the browser reconnects
automatically if the connection drops.

---

## Limitations & known issues

- **GPS vertical speed** is derived from successive altitude readings (1 Hz) and
  will be noisy at low climb/descent rates. It is useful for trend only.
- **Slip/skid ball** uses roll angle as a proxy. A true coordinated-turn
  indicator requires lateral accelerometer data; this can be added by reading
  the WT901's 0x51 acceleration packet (code already parses `ax`).
- **No airspeed.** The NEO-6M provides *groundspeed* only. Connecting a pitot
  tube + differential pressure sensor to an ADC pin would provide IAS.
- The display is optimised for **landscape orientation** on a phone. Portrait
  works but is less comfortable as a forward-looking display.
- The Pico W serves **one SSE client at a time** efficiently; a second tab or
  device will work but both streams share the same 10 Hz server loop.
