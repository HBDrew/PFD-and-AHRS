# ---------------------------------------------------------------------------
# main.py  –  AHRS PFD main application
# ---------------------------------------------------------------------------
# Boot sequence
# 1. Start WiFi access point (SSID / password from config.py)
# 2. Initialise WT901 AHRS on UART0
# 3. Initialise GPS on UART1
# 4. Optionally initialise BME280 barometer on I2C1 (GP2/GP3)
# 5. Start async HTTP/SSE server
# 6. Sensor-read loop runs concurrently, updating shared state dict
#
# Altitude source priority: BME280 (if present) → GPS
# If BME280 is not connected or fails to init, GPS altitude is used and
# state['baro_src'] = 'gps'.  No restart required.
#
# On the phone: connect to the WiFi AP, open http://192.168.4.1
# ---------------------------------------------------------------------------

import uasyncio as asyncio
import network
import utime
import ujson
from machine import Pin

from config import (
    WT901_UART_ID, WT901_TX_PIN, WT901_RX_PIN, WT901_BAUD,
    GPS_UART_ID,  GPS_TX_PIN,  GPS_RX_PIN,  GPS_BAUD,
    BME280_ENABLE, BME280_I2C_ID, BME280_SDA_PIN, BME280_SCL_PIN,
    BME280_I2C_ADDR, BME280_QNH_DEFAULT,
    WT901_AY_SIGN,
    AHRS_PITCH_TRIM, AHRS_ROLL_TRIM, AHRS_YAW_TRIM,
    AP_SSID, AP_PASSWORD, HTTP_PORT, BROADCAST_HZ,
)
from wt901      import WT901
from gps        import GPS
from web_server import start_server

TRIMS_FILE = 'trims.json'


def load_trims():
    try:
        with open(TRIMS_FILE, 'r') as f:
            t = ujson.loads(f.read())
        return {k: float(t.get(k, d)) for k, d in
                [('pitch_trim', AHRS_PITCH_TRIM),
                 ('roll_trim',  AHRS_ROLL_TRIM),
                 ('yaw_trim',   AHRS_YAW_TRIM)]}
    except Exception:
        return {'pitch_trim': AHRS_PITCH_TRIM,
                'roll_trim':  AHRS_ROLL_TRIM,
                'yaw_trim':   AHRS_YAW_TRIM}


def save_trims(state):
    try:
        with open(TRIMS_FILE, 'w') as f:
            f.write(ujson.dumps({'pitch_trim': state['pitch_trim'],
                                  'roll_trim':  state['roll_trim'],
                                  'yaw_trim':   state['yaw_trim']}))
    except Exception as e:
        print(f'save_trims failed: {e}')

# ── Onboard LED ─────────────────────────────────────────────────────────────
led = Pin('LED', Pin.OUT)

# ── Shared state (read by web_server, written by sensor_loop) ───────────────
state = {
    '_broadcast_hz': BROADCAST_HZ,
    # AHRS
    'roll'     : 0.0,   # degrees  (+right wing down)
    'pitch'    : 0.0,   # degrees  (+nose up)
    'yaw'      : 0.0,   # degrees  magnetic heading [0, 360)
    'ay'       : 0.0,   # lateral acceleration g (+right); drives slip ball
    # GPS position (always from GPS)
    'lat'      : 0.0,   # decimal degrees
    'lon'      : 0.0,   # decimal degrees
    'speed'    : 0.0,   # groundspeed knots
    'track'    : 0.0,   # track over ground degrees true
    'fix'      : 0,     # 0=none 1=GPS 2=DGPS
    'sats'     : 0,     # satellites in use
    'gps_alt'  : 0.0,   # GPS MSL altitude ft (always present for calibration ref)
    # Altitude (BME280 when available, else GPS)
    'alt'      : 0.0,   # feet MSL – displayed altitude
    'vspeed'   : 0.0,   # vertical speed ft/min
    'baro_src' : 'gps', # 'bme280' | 'gps'
    # Barometric setting (user-adjustable via /baro endpoint)
    'baro_hpa' : BME280_QNH_DEFAULT,  # QNH in hPa; written by /baro, broadcast via SSE
    # AHRS trim offsets (degrees; adjustable via /trim, persisted to trims.json)
    'pitch_trim': 0.0,
    'roll_trim':  0.0,
    'yaw_trim':   0.0,
    # Sensor health flags (set every sensor_loop tick)
    'ahrs_ok':   False,
    'gps_ok':    False,
    'baro_ok':   False,
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
async def sensor_loop(ahrs: WT901, gps: GPS, baro):
    """
    Poll all sensors at ~50 Hz and push values into the shared state dict.
    The web server reads state independently at BROADCAST_HZ.

    baro: BME280 instance or None (falls back to GPS altitude).
    """
    tick = 0
    last_ahrs_ms = utime.ticks_ms()   # 5-second window before ahrs_ok goes False
    while True:
        # ── AHRS ──
        if ahrs.update():
            state['roll']  = ahrs.roll  + state['roll_trim']
            state['pitch'] = ahrs.pitch + state['pitch_trim']
            state['yaw']   = (ahrs.yaw  + state['yaw_trim']) % 360
            state['ay']    = ahrs.ay * WT901_AY_SIGN
            last_ahrs_ms   = utime.ticks_ms()
        state['ahrs_ok'] = utime.ticks_diff(utime.ticks_ms(), last_ahrs_ms) < 5000
        state['gps_ok']  = gps.fix > 0
        state['baro_ok'] = baro is not None
        # Persist trims to flash if web endpoint set the flag
        if state.get('_save_trims'):
            save_trims(state)
            state['_save_trims'] = False

        # ── GPS (always poll for position; altitude used as fallback/reference) ──
        gps.update()
        state['lat']     = gps.lat
        state['lon']     = gps.lon
        state['speed']   = gps.speed_kt
        state['track']   = gps.track_deg
        state['fix']     = gps.fix
        state['sats']    = gps.sats
        state['gps_alt'] = gps.alt_ft  # always keep GPS alt for calibration ref

        # ── Altitude source ──
        if baro is not None:
            # Sync QNH from state (user may have adjusted via /baro endpoint)
            baro.qnh_hpa = state['baro_hpa']

            # Handle "Set Alt Here" calibration request from display
            cal_ft = state.get('_cal_ft')
            if cal_ft is not None:
                baro.calibrate_to_alt_ft(cal_ft)
                state['baro_hpa'] = baro.qnh_hpa   # broadcast updated QNH back
                state['_cal_ft']  = None

            baro.update()
            state['alt']      = baro.altitude_ft()
            state['vspeed']   = baro.vspeed_fpm
            state['baro_src'] = 'bme280'
        else:
            state['alt']      = gps.alt_ft
            state['vspeed']   = gps.vspeed_fpm
            state['baro_src'] = 'gps'

        # USB serial output: emit $AHRS,{json} at BROADCAST_HZ so the Pi
        # can read AHRS data over USB without WiFi.  50 Hz poll / broadcast_hz
        # gives the tick interval.
        tick += 1
        usb_interval = max(1, 50 // state['_broadcast_hz'])
        if tick % usb_interval == 0:
            try:
                _usb = {k: state[k] for k in (
                    'roll','pitch','yaw','ay','lat','lon','speed','track',
                    'fix','sats','alt','gps_alt','vspeed','baro_src','baro_hpa',
                    'ahrs_ok','gps_ok','baro_ok','pitch_trim','roll_trim','yaw_trim',
                )}
                print('$AHRS,' + ujson.dumps(_usb))
            except Exception:
                pass

        # Heartbeat LED: blink every 2 s (100 × 20 ms)
        if tick % 100 == 0:
            led.toggle()

        await asyncio.sleep_ms(20)   # 50 Hz poll


# ── Entry point ───────────────────────────────────────────────────────────────
async def main():
    print('─' * 40)
    print('AHRS PFD  –  starting up')
    print('─' * 40)

    setup_ap()

    ahrs = WT901(WT901_UART_ID, WT901_TX_PIN, WT901_RX_PIN, WT901_BAUD)
    gps  = GPS(GPS_UART_ID, GPS_TX_PIN, GPS_RX_PIN, GPS_BAUD)
    print(f'WT901  UART{WT901_UART_ID} @ {WT901_BAUD} baud  (GP{WT901_RX_PIN} RX)')
    print(f'NEO-6M UART{GPS_UART_ID}  @ {GPS_BAUD} baud  (GP{GPS_RX_PIN} RX)')

    state.update(load_trims())
    print(f'Trims loaded: pitch={state["pitch_trim"]}° roll={state["roll_trim"]}° yaw={state["yaw_trim"]}°')

    baro = None
    if BME280_ENABLE:
        try:
            from bme280 import BME280
            baro = BME280(
                i2c_id  = BME280_I2C_ID,
                sda     = BME280_SDA_PIN,
                scl     = BME280_SCL_PIN,
                addr    = BME280_I2C_ADDR,
                qnh_hpa = BME280_QNH_DEFAULT,
            )
            state['baro_src'] = 'bme280'
            print(f'BME280  I2C{BME280_I2C_ID}'
                  f' @ 0x{BME280_I2C_ADDR:02x}'
                  f' (GP{BME280_SDA_PIN} SDA, GP{BME280_SCL_PIN} SCL)'
                  f'  QNH={BME280_QNH_DEFAULT} hPa')
        except Exception as e:
            print(f'BME280 not found ({e})  –  using GPS altitude')
            baro = None

    await asyncio.gather(
        sensor_loop(ahrs, gps, baro),
        start_server(state, port=HTTP_PORT),
    )


try:
    asyncio.run(main())
except KeyboardInterrupt:
    print('Stopped by user')
finally:
    led.off()
