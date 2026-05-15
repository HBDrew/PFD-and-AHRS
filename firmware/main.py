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
import sys
from machine import Pin

from config import (
    WT901_UART_ID, WT901_TX_PIN, WT901_RX_PIN, WT901_BAUD,
    GPS_UART_ID,  GPS_TX_PIN,  GPS_RX_PIN,  GPS_BAUD,
    BME280_ENABLE, BME280_I2C_ID, BME280_SDA_PIN, BME280_SCL_PIN,
    BME280_I2C_ADDR, BME280_QNH_DEFAULT,
    WT901_AY_SIGN,
    AHRS_PITCH_TRIM, AHRS_ROLL_TRIM, AHRS_YAW_TRIM,
    AHRS_CONNECTOR, AHRS_MOUNTING,
    AP_SSID, AP_PASSWORD, HTTP_PORT, BROADCAST_HZ,
)
from wt901      import WT901
from gps        import GPS
from web_server import start_server

TRIMS_FILE  = 'trims.json'
MAGDEV_FILE = 'magdev.json'
ORIENT_FILE = 'orient.json'


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


def load_magdev():
    try:
        with open(MAGDEV_FILE, 'r') as f:
            t = ujson.loads(f.read()).get('corrections', [])
        return [float(x) for x in t] if len(t) == 36 else []
    except Exception:
        return []


def save_magdev(table):
    try:
        with open(MAGDEV_FILE, 'w') as f:
            f.write(ujson.dumps({'corrections': table}))
    except Exception as e:
        print(f'save_magdev failed: {e}')


def load_orient():
    _valid_c = ('forward', 'right', 'left', 'aft')
    _valid_m = ('normal', 'inverted')
    try:
        with open(ORIENT_FILE, 'r') as f:
            d = ujson.loads(f.read())
        c = d.get('connector', AHRS_CONNECTOR)
        m = d.get('mounting',  AHRS_MOUNTING)
        if c not in _valid_c: c = AHRS_CONNECTOR
        if m not in _valid_m: m = AHRS_MOUNTING
        return c, m
    except Exception:
        return AHRS_CONNECTOR, AHRS_MOUNTING


def save_orient(state):
    try:
        with open(ORIENT_FILE, 'w') as f:
            f.write(ujson.dumps({'connector': state['orientation'],
                                  'mounting':  state['mounting']}))
    except Exception as e:
        print(f'save_orient failed: {e}')



def apply_magdev(yaw, table):
    """Interpolate a 36-point (10°/slot) deviation table and apply to yaw."""
    if len(table) != 36:
        return yaw
    idx = (yaw % 360) / 10.0
    i0 = int(idx) % 36
    i1 = (i0 + 1) % 36
    frac = idx - int(idx)
    c0, c1 = table[i0], table[i1]
    dc = c1 - c0
    if dc > 180:  dc -= 360
    elif dc < -180: dc += 360
    return (yaw + c0 + frac * dc) % 360

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
    # AHRS orientation (reflects config.py values; broadcast for Pi4 info display)
    'orientation': AHRS_CONNECTOR,
    'mounting':    AHRS_MOUNTING,
    # Sensor health flags (set every sensor_loop tick)
    'ahrs_ok':   False,
    'gps_ok':    False,
    'gps_comm':  False,  # True when GPS UART is sending valid NMEA sentences
    'baro_ok':   False,
    # Magnetic deviation table (36 corrections at 10° steps; loaded from magdev.json)
    '_magdev'  : [],
    # Pre-correction heading (post yaw_trim, pre magdev) — broadcast for cal panel
    'yaw_raw'  : 0.0,
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
    last_gps_nmea_ms = None           # last time a valid NMEA sentence arrived
    while True:
        # ── AHRS ──
        try:
            if ahrs.update():
                # ENU→NED base conversion (WT901 uses ENU: roll left-positive,
                # yaw CCW-positive).  Orientation remaps axes so the display
                # reads correctly regardless of how the sensor is mounted.
                _r = -ahrs.roll
                _p = ahrs.pitch
                _conn = state['orientation']
                if _conn == 'forward':
                    _p, _r = -_r, _p
                    _hdg_off = 90.0
                elif _conn == 'left':
                    _p, _r = -_p, -_r
                    _hdg_off = 180.0
                elif _conn == 'aft':
                    _p, _r = _r, -_p
                    _hdg_off = 270.0
                else:    # 'right' — default, connector points to the right
                    _hdg_off = 0.0
                if state['mounting'] == 'inverted':
                    _p = -_p
                    _r = -_r
                # Trim applied in NED frame (after axis remapping)
                state['roll']    = _r + state['roll_trim']
                state['pitch']   = _p + state['pitch_trim']
                # Yaw: negate ENU→NED, apply orientation offset, then trim
                _raw             = (-ahrs.yaw - state['yaw_trim'] + _hdg_off) % 360
                state['yaw_raw'] = _raw
                state['yaw']     = apply_magdev(_raw, state['_magdev'])
                state['ay']      = ahrs.ay * WT901_AY_SIGN
                last_ahrs_ms   = utime.ticks_ms()
        except Exception as e:
            print(f'[AHRS] WT901 read error: {e}')
        state['ahrs_ok'] = utime.ticks_diff(utime.ticks_ms(), last_ahrs_ms) < 5000
        state['gps_ok']  = gps.fix > 0
        state['baro_ok'] = baro is not None
        # Persist trims to flash if web endpoint set the flag
        if state.get('_save_trims'):
            save_trims(state)
            state['_save_trims'] = False
        if state.get('_save_magdev'):
            save_magdev(state['_magdev'])
            state['_save_magdev'] = False
        if state.get('_save_orient'):
            save_orient(state)
            state['_save_orient'] = False

        await asyncio.sleep_ms(0)   # yield so web server can handle requests

        # ── GPS (always poll for position; altitude used as fallback/reference) ──
        try:
            if gps.update():
                last_gps_nmea_ms = utime.ticks_ms()
        except Exception as e:
            print(f'[AHRS] GPS read error: {e}')
        # gps_comm: True while NMEA sentences have arrived within the last 5 s
        if last_gps_nmea_ms is not None:
            state['gps_comm'] = utime.ticks_diff(utime.ticks_ms(), last_gps_nmea_ms) < 5000
        else:
            state['gps_comm'] = False
        state['lat']     = gps.lat
        state['lon']     = gps.lon
        state['speed']   = gps.speed_kt
        state['track']   = gps.track_deg
        state['fix']     = gps.fix
        state['sats']    = gps.sats
        state['gps_alt'] = gps.alt_ft  # always keep GPS alt for calibration ref

        await asyncio.sleep_ms(0)   # yield so web server can handle requests

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

            try:
                baro.update()
                state['alt']      = baro.altitude_ft()
                state['vspeed']   = baro.vspeed_fpm
                state['baro_src'] = 'bme280'
            except Exception as e:
                print(f'[AHRS] BME280 read error: {e}')
                # fall back to GPS altitude on I2C error
                state['alt']      = gps.alt_ft
                state['vspeed']   = gps.vspeed_fpm
                state['baro_src'] = 'gps'
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
                    'roll','pitch','yaw','yaw_raw','ay','lat','lon','speed','track',
                    'fix','sats','alt','gps_alt','vspeed','baro_src','baro_hpa',
                    'ahrs_ok','gps_ok','gps_comm','baro_ok','pitch_trim','roll_trim','yaw_trim',
                    'orientation','mounting',
                )}
                print('$AHRS,' + ujson.dumps(_usb))
            except Exception:
                pass

        # Heartbeat LED: blink every 2 s (100 × 20 ms)
        if tick % 100 == 0:
            led.toggle()

        await asyncio.sleep_ms(20)   # 50 Hz poll


# ── USB serial command reader ($MAGDEV, $ORIENT from Pi4) ───────────────────
def _process_stdin_line(line):
    """Dispatch one command line received from the Pi4 over USB serial."""
    if line.startswith('$MAGDEV,'):
        payload = line[8:]
        if payload == 'CLEAR':
            state['_magdev'] = []
            state['_save_magdev'] = True
            print('$MAGDEV_ACK,0,CLEARED')
        else:
            try:
                vals = [float(x) for x in payload.split(',') if x.strip()]
                if len(vals) == 36:
                    state['_magdev'] = vals
                    state['_save_magdev'] = True
                    print(f'$MAGDEV_ACK,{len(vals)},OK')
                else:
                    print(f'$MAGDEV_ACK,0,ERR got {len(vals)}')
            except Exception as e:
                print(f'$MAGDEV_ACK,0,ERR {e}')
    elif line.startswith('$ORIENT,'):
        parts = line[8:].split(',')
        if len(parts) == 2:
            c = parts[0].strip()
            m = parts[1].strip()
            if (c in ('forward', 'right', 'left', 'aft')
                    and m in ('normal', 'inverted')):
                state['orientation'] = c
                state['mounting']    = m
                state['_save_orient'] = True
                print(f'$ORIENT_ACK,{c},{m},OK')
            else:
                print(f'$ORIENT_ACK,ERR invalid: {c},{m}')
        else:
            print('$ORIENT_ACK,ERR bad format')


async def stdin_cmd_loop():
    """
    Read commands sent by the Pi4 over USB serial.
    Handles: $MAGDEV,... and $ORIENT,connector,mounting

    Primary: asyncio.StreamReader(sys.stdin) — integrates with the event
    loop's _io_queue via ioctl(MP_STREAM_POLL_RD), reliably waking on USB
    CDC data.  Falls back to uselect polling if StreamReader is unavailable.
    """
    # Primary: event-loop-integrated I/O — more reliable than manual uselect
    # on Pico W USB CDC.
    try:
        reader = asyncio.StreamReader(sys.stdin)
        print('[stdin] using asyncio.StreamReader')
        while True:
            try:
                raw = await reader.readline()
                if raw:
                    line = raw.decode('utf-8', 'ignore').strip()
                    if line:
                        _process_stdin_line(line)
            except Exception as e:
                print(f'[stdin] readline error: {e}')
                await asyncio.sleep_ms(100)
        return
    except Exception as e:
        print(f'[stdin] StreamReader unavailable ({e}), falling back to uselect')

    # Fallback: manual uselect polling.
    try:
        import uselect
        poll = uselect.poll()
        poll.register(sys.stdin, uselect.POLLIN)
    except Exception:
        return

    buf = bytearray()
    while True:
        if poll.poll(0):
            try:
                ch = sys.stdin.read(1)
                if ch:
                    b = ord(ch)
                    if b == 10:    # LF — end of line
                        if buf:
                            line = buf.decode('utf-8', 'ignore').strip()
                            buf = bytearray()
                            if line:
                                _process_stdin_line(line)
                    elif b == 13:  # CR — ignore (\r\n line endings)
                        pass
                    else:
                        buf.append(b)
                        if len(buf) > 600:
                            buf = bytearray()
            except Exception:
                pass
        await asyncio.sleep_ms(20)


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
    _c, _m = load_orient()
    state['orientation'] = _c
    state['mounting']    = _m
    print(f'Orientation: connector={_c}  mounting={_m}')
    state['_magdev'] = load_magdev()
    if state['_magdev']:
        print(f'Magdev table loaded: {len(state["_magdev"])} corrections')
    else:
        print('Magdev: no calibration file — heading uncorrected')

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
        stdin_cmd_loop(),
    )


try:
    asyncio.run(main())
except KeyboardInterrupt:
    print('Stopped by user')
finally:
    led.off()
