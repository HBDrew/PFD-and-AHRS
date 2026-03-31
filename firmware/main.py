# ---------------------------------------------------------------------------
# main.py  –  AHRS PFD main application
# ---------------------------------------------------------------------------
# Boot sequence
# 1. Start WiFi access point (SSID / password from config.py)
# 2. Initialise WT901 AHRS on UART0
# 3. Initialise GPS on UART1
# 4. Start async HTTP/SSE server
# 5. Sensor-read loop runs concurrently, updating shared state dict
#
# On the phone: connect to the WiFi AP, open http://192.168.4.1
# ---------------------------------------------------------------------------

import uasyncio as asyncio
import network
import utime
from machine import Pin

from config import (
    WT901_UART_ID, WT901_TX_PIN, WT901_RX_PIN, WT901_BAUD,
    GPS_UART_ID, GPS_TX_PIN, GPS_RX_PIN, GPS_BAUD,
    AP_SSID, AP_PASSWORD, HTTP_PORT, BROADCAST_HZ,
)
from wt901 import WT901
from gps   import GPS
from web_server import start_server

# ── Onboard LED ─────────────────────────────────────────────────────────────
led = Pin('LED', Pin.OUT)

# ── Shared state (read by web_server, written by sensor_loop) ───────────────
state = {
    '_broadcast_hz': BROADCAST_HZ,
    # AHRS
    'roll'   : 0.0,   # degrees  (+right wing down)
    'pitch'  : 0.0,   # degrees  (+nose up)
    'yaw'    : 0.0,   # degrees  magnetic heading [0, 360)
    # GPS
    'lat'    : 0.0,   # decimal degrees
    'lon'    : 0.0,   # decimal degrees
    'alt'    : 0.0,   # feet MSL
    'speed'  : 0.0,   # groundspeed knots
    'track'  : 0.0,   # track over ground degrees true
    'vspeed' : 0.0,   # vertical speed ft/min
    'fix'    : 0,     # 0=none 1=GPS 2=DGPS
    'sats'   : 0,     # satellites in use
}


# ── WiFi AP setup ────────────────────────────────────────────────────────────
def setup_ap():
    ap = network.WLAN(network.AP_IF)
    ap.active(True)
    if AP_PASSWORD:
        ap.config(essid=AP_SSID, password=AP_PASSWORD, security=4)  # WPA2
    else:
        ap.config(essid=AP_SSID, security=0)
    timeout = 10_000  # ms
    start   = utime.ticks_ms()
    while not ap.active():
        if utime.ticks_diff(utime.ticks_ms(), start) > timeout:
            raise RuntimeError('WiFi AP failed to start')
        utime.sleep_ms(100)
    ip = ap.ifconfig()[0]
    print(f'WiFi AP "{AP_SSID}" active  →  http://{ip}')
    return ip


# ── Sensor loop ──────────────────────────────────────────────────────────────
async def sensor_loop(ahrs: WT901, gps: GPS):
    """
    Poll both sensors at ~50 Hz and push values into the shared state dict.
    The web server reads state independently at BROADCAST_HZ.
    """
    tick = 0
    while True:
        # ── AHRS ──
        ahrs.update()
        state['roll']  = ahrs.roll
        state['pitch'] = ahrs.pitch
        state['yaw']   = ahrs.yaw

        # ── GPS ──
        gps.update()
        state['lat']    = gps.lat
        state['lon']    = gps.lon
        state['alt']    = gps.alt_ft
        state['speed']  = gps.speed_kt
        state['track']  = gps.track_deg
        state['vspeed'] = gps.vspeed_fpm
        state['fix']    = gps.fix
        state['sats']   = gps.sats

        # Heartbeat LED: blink every 2 s
        tick += 1
        if tick % 100 == 0:     # 100 × 20 ms = 2 s
            led.toggle()

        await asyncio.sleep_ms(20)   # 50 Hz poll


# ── Entry point ───────────────────────────────────────────────────────────────
async def main():
    print('─' * 40)
    print('AHRS PFD  –  starting up')
    print('─' * 40)

    setup_ap()

    ahrs = WT901(WT901_UART_ID, WT901_TX_PIN, WT901_RX_PIN, WT901_BAUD)
    gps  = GPS(GPS_UART_ID,  GPS_TX_PIN,  GPS_RX_PIN,  GPS_BAUD)
    print(f'WT901  UART{WT901_UART_ID} @ {WT901_BAUD} baud  (GP{WT901_RX_PIN} RX)')
    print(f'NEO-6M UART{GPS_UART_ID}  @ {GPS_BAUD} baud  (GP{GPS_RX_PIN} RX)')

    await asyncio.gather(
        sensor_loop(ahrs, gps),
        start_server(state, port=HTTP_PORT),
    )


try:
    asyncio.run(main())
except KeyboardInterrupt:
    print('Stopped by user')
finally:
    led.off()
