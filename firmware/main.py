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
    SDP31_ENABLE, SDP31_I2C_ID, SDP31_SDA_PIN, SDP31_SCL_PIN,
    SDP31_I2C_ADDR, SDP31_AUTO_ZERO_AT_BOOT,
    WT901_AY_SIGN,
    AHRS_PITCH_TRIM, AHRS_ROLL_TRIM, AHRS_YAW_TRIM,
    AHRS_CONNECTOR, AHRS_MOUNTING,
    AP_SSID, AP_PASSWORD, HTTP_PORT, BROADCAST_HZ,
)
from wt901      import WT901
from gps        import GPS
from web_server import start_server
import airdata

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
    # Air data (SDP33-1500Pa + BME280 density correction)
    'ias_kt'     : 0.0,  # indicated airspeed (knots) — ρ₀ reference
    'tas_kt'     : 0.0,  # true airspeed (knots) — density-corrected
    'dp_pa'      : 0.0,  # raw differential pressure (Pa); diagnostic
    'oat_c'      : 0.0,  # outside air temperature (°C) from BME280
    'dens_alt_ft': 0.0,  # density altitude (ft)
    'wind_dir'   : 0.0,  # wind direction (deg from); 0 = wind absent or unknown
    'wind_kt'    : 0.0,  # wind speed (knots)
    'airdata_ok' : False, # True when SDP33 + BME280 both fresh
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
async def sensor_loop(ahrs: WT901, gps: GPS, baro, sdp):
    """
    Poll all sensors at ~50 Hz and push values into the shared state dict.
    The web server reads state independently at BROADCAST_HZ.

    baro: BME280 instance or None (falls back to GPS altitude).
    sdp:  SDP31  instance or None (no air-data, GPS GS used for speed).
    """
    tick = 0
    last_ahrs_ms = utime.ticks_ms()   # 5-second window before ahrs_ok goes False
    last_gps_nmea_ms = None           # last time a valid NMEA sentence arrived
    last_sdp_ms = None                # last successful SDP33 read
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

        # ── Air data (SDP33 + BME280) ──
        # IAS / TAS / density-altitude / wind solution.  Runs whenever the
        # SDP33 is present; outputs are valid even with no GPS (just no
        # wind solution).  Pi4 / iPhone use ias_kt for the speed tape when
        # airdata_ok is True, falling back to GPS GS otherwise.
        if sdp is not None:
            try:
                if sdp.update():
                    last_sdp_ms = utime.ticks_ms()
            except Exception as e:
                print(f'[AHRS] SDP33 read error: {e}')
            state['dp_pa']      = sdp.dp_pa
            state['ias_kt']     = airdata.ias_kt(sdp.dp_pa)
            # Use BME280 absolute pressure (not QNH-corrected) for density.
            if baro is not None:
                state['oat_c']       = baro.temperature_c
                state['tas_kt']      = airdata.tas_kt(
                    state['ias_kt'], baro.pressure_pa, baro.temperature_c)
                state['dens_alt_ft'] = airdata.density_alt_ft(
                    baro.pressure_pa, baro.temperature_c)
            else:
                # Without BME280 we can't compute density — report IAS as
                # TAS (correct at sea-level ISA, increasingly wrong with
                # altitude). Down-stream consumers can flag the missing
                # baro via baro_ok.
                state['tas_kt']      = state['ias_kt']
                state['dens_alt_ft'] = 0.0
            # Wind: needs TAS, heading, and a valid GPS track + GS.
            if gps.fix > 0 and state['tas_kt'] > 5.0:
                wd, ws = airdata.wind_solution(
                    state['tas_kt'], state['yaw'],
                    gps.speed_kt, gps.track_deg)
                if wd is not None:
                    state['wind_dir'] = wd
                    state['wind_kt']  = ws
            # 5-second freshness window mirrors the AHRS / GPS health gates.
            state['airdata_ok'] = (last_sdp_ms is not None and
                                   utime.ticks_diff(utime.ticks_ms(),
                                                    last_sdp_ms) < 5000)
            # One-shot zero capture after the boot settle window — sensor's
            # internal averaging has a few hundred ms transient before
            # readings stabilise.
            if (state.get('_sdp_zero') or
                    (SDP31_AUTO_ZERO_AT_BOOT and tick == 100)):
                try:
                    sdp.zero()
                    print(f'[AHRS] SDP33 zero captured (dp_offset cleared)')
                except Exception as e:
                    print(f'[AHRS] SDP33 zero failed: {e}')
                state['_sdp_zero'] = False
        else:
            state['airdata_ok'] = False

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
                    'ias_kt','tas_kt','dp_pa','oat_c','dens_alt_ft',
                    'wind_dir','wind_kt','airdata_ok',
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

    Drains all available bytes on each pass before yielding back to the
    event loop.  Reading one byte per 20 ms tick would take ~440 ms for a
    22-byte command; draining the full ring buffer avoids that latency.
    """
    try:
        import uselect
        poll = uselect.poll()
        poll.register(sys.stdin, uselect.POLLIN)
    except Exception:
        return

    buf = bytearray()
    while True:
        # Drain all bytes currently in the ring buffer before sleeping.
        while poll.poll(0):
            try:
                ch = sys.stdin.read(1)
                if not ch:
                    break
                b = ord(ch)
                if b == 10:    # LF — end of line
                    if buf:
                        line = buf.decode('utf-8', 'ignore').strip()
                        buf = bytearray()
                        if line:
                            _process_stdin_line(line)
                elif b != 13:  # ignore CR
                    buf.append(b)
                    if len(buf) > 600:
                        buf = bytearray()
            except Exception:
                break
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

    sdp = None
    if SDP31_ENABLE:
        try:
            from sdp31 import SDP31
            sdp = SDP31(
                i2c_id = SDP31_I2C_ID,
                sda    = SDP31_SDA_PIN,
                scl    = SDP31_SCL_PIN,
                addr   = SDP31_I2C_ADDR,
            )
            print(f'SDP33  I2C{SDP31_I2C_ID}'
                  f' @ 0x{SDP31_I2C_ADDR:02x}'
                  f' (GP{SDP31_SDA_PIN} SDA, GP{SDP31_SCL_PIN} SCL)'
                  f'  scale={sdp.scale}  auto-zero={SDP31_AUTO_ZERO_AT_BOOT}')
        except Exception as e:
            print(f'SDP33 not found ({e})  –  airspeed will fall back to GPS GS')
            sdp = None

    await asyncio.gather(
        sensor_loop(ahrs, gps, baro, sdp),
        start_server(state, port=HTTP_PORT),
        stdin_cmd_loop(),
    )


try:
    asyncio.run(main())
except KeyboardInterrupt:
    print('Stopped by user')
finally:
    led.off()
