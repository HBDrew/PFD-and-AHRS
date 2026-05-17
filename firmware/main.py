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
    MS4525_ENABLE, MS4525_I2C_ID, MS4525_SDA_PIN, MS4525_SCL_PIN,
    MS4525_I2C_ADDR, MS4525_PSI_RANGE, MS4525_AUTO_ZERO_AT_BOOT,
    WT901_AY_SIGN,
    AHRS_PITCH_TRIM, AHRS_ROLL_TRIM, AHRS_YAW_TRIM,
    AHRS_CONNECTOR, AHRS_MOUNTING,
    AHRS_FILTER_ENABLE, AHRS_KP_ACC, AHRS_KI_ACC, AHRS_KP_MAG,
    AHRS_ACCEL_GATE_G, AHRS_USE_MAG,
    AHRS_GPS_TRACK_ENABLE, AHRS_GPS_TRACK_MIN_KT,
    AHRS_GPS_TRACK_INTERVAL_S, AHRS_GPS_TRACK_ALPHA,
    AHRS_FWD_IN_SENSOR, AHRS_ALIGN_DURATION_S, FW_VERSION,
    AP_SSID, AP_PASSWORD, HTTP_PORT, BROADCAST_HZ,
)
from wt901        import WT901
from gps          import GPS
from web_server   import start_server
from ahrs_filter  import Mahony
import airdata
import math

TRIMS_FILE  = 'trims.json'
MAGDEV_FILE = 'magdev.json'
MAGCAL_FILE = 'magcal.json'   # hard-iron offsets (mx_off, my_off, mz_off)
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


def load_magcal():
    """Hard-iron offsets (mx_off, my_off, mz_off) — subtracted from raw mag
    before it's handed to the Mahony filter. Captured at install time by
    the 8-cardinal cal wizard. Default (0,0,0) = no correction."""
    try:
        with open(MAGCAL_FILE, 'r') as f:
            d = ujson.loads(f.read())
        return (float(d.get('mx_off', 0.0)),
                float(d.get('my_off', 0.0)),
                float(d.get('mz_off', 0.0)))
    except Exception:
        return (0.0, 0.0, 0.0)


def save_magcal(offset):
    try:
        with open(MAGCAL_FILE, 'w') as f:
            f.write(ujson.dumps({'mx_off': offset[0],
                                  'my_off': offset[1],
                                  'mz_off': offset[2]}))
    except Exception as e:
        print(f'save_magcal failed: {e}')


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
    # On-Pico Mahony filter state (broadcast for display diagnostics)
    'att_src'   : 'wt901', # 'mahony' when filter is active, else 'wt901'
    'att_aid'   : 'basic', # 'tas' | 'gs' | 'basic' — centripetal speed source
    'ahrs_aligning': True, # True for the first AHRS_ALIGN_DURATION_S after the
                           # filter starts receiving gyro data — display shows
                           # an "AHRS ALIGN" banner so the pilot knows the
                           # attitude is still settling.
    'fw_ver'    : FW_VERSION, # Firmware date code (broadcast to display)
    # Sensor health flags (set every sensor_loop tick)
    'ahrs_ok':   False,
    'gps_ok':    False,
    'gps_comm':  False,  # True when GPS UART is sending valid NMEA sentences
    'baro_ok':   False,
    # Magnetic deviation table (36 corrections at 10° steps; loaded from magdev.json)
    '_magdev'  : [],
    # Hard-iron offsets (mx_off, my_off, mz_off) — subtracted from raw mag
    # in _run_filter_step before mag is handed to the Mahony filter. Captured
    # by the 8-cardinal cal wizard from the display. Loaded from magcal.json.
    '_mag_offset': (0.0, 0.0, 0.0),
    # Raw mag readings (unscaled int counts) — broadcast for the cal wizard
    # to compute fresh hard-iron offsets each calibration run.
    'mx': 0.0, 'my': 0.0, 'mz': 0.0,
    # Pre-correction heading (post yaw_trim, pre magdev) — broadcast for cal panel
    'yaw_raw'  : 0.0,
    # WT901's PKT_ANGLE yaw (post-remap) — broadcast for the cal wizard's
    # RAW HDG display. Fast response, no Mahony filter dynamics in the way.
    'yaw_wt901': 0.0,
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


_G_PER_M_S2 = 1.0 / 9.80665
_KT_TO_M_S  = 0.514444


def _hdg_offset_for(connector):
    """Connector orientation → heading offset applied in the Euler remap."""
    if connector == 'forward': return 90.0
    if connector == 'left':    return 180.0
    if connector == 'aft':     return 270.0
    return 0.0   # 'right' (default)


def _apply_remap(roll, pitch, yaw, connector, mounting):
    """Map WT901 sensor-frame Euler (ENU-convention) → aircraft body NED.
    Returns (body_roll, body_pitch, body_yaw_raw_unwrapped, hdg_off).
    yaw return is pre-trim and pre-magdev; caller applies those."""
    _r = -roll
    _p = pitch
    if connector == 'forward':
        _p, _r = -_r, _p
    elif connector == 'left':
        _p, _r = -_p, -_r
    elif connector == 'aft':
        _p, _r = _r, -_p
    # 'right' is no-op
    if mounting == 'inverted':
        _p = -_p
        _r = -_r
    return _r, _p, -yaw, _hdg_offset_for(connector)


def _run_filter_step(ahrs, ahrs_filter, dt):
    """Centripetal-corrected accel + gyro (+mag) → one Mahony step. Returns
    ('tas'|'gs'|'basic', a_c_magnitude_g) for diagnostics."""
    gx = math.radians(ahrs.wx)
    gy = math.radians(ahrs.wy)
    gz = math.radians(ahrs.wz)

    # Speed source ladder: TAS (physically correct) → GS → none.
    if state.get('airdata_ok') and state.get('tas_kt', 0.0) > 5.0:
        v_kt    = state['tas_kt']
        att_aid = 'tas'
    elif state.get('gps_ok') and state.get('speed', 0.0) > 5.0:
        v_kt    = state['speed']
        att_aid = 'gs'
    else:
        v_kt    = 0.0
        att_aid = 'basic'

    # Centripetal accel in sensor frame: a_c = ω × V_fwd_sensor (m/s²)
    v_ms = v_kt * _KT_TO_M_S
    vx = AHRS_FWD_IN_SENSOR[0] * v_ms
    vy = AHRS_FWD_IN_SENSOR[1] * v_ms
    vz = AHRS_FWD_IN_SENSOR[2] * v_ms
    acx = (gy*vz - gz*vy) * _G_PER_M_S2
    acy = (gz*vx - gx*vz) * _G_PER_M_S2
    acz = (gx*vy - gy*vx) * _G_PER_M_S2

    # WT901 reads specific force (stationary level = +1g on Z): adding the
    # inertial centripetal vector recovers the gravity direction.
    ax = ahrs.ax + acx
    ay = ahrs.ay + acy
    az = ahrs.az + acz

    if AHRS_USE_MAG and ahrs.new_mag:
        # Subtract hard-iron offsets before the filter sees mag. Without this
        # the filter's mag-correction term fights the gyro in a biased
        # magnetic environment — see Docs/REQUIREMENTS_AHRS.md REQ-AHRS-CAL-003.
        off = state['_mag_offset']
        mx = ahrs.mx - off[0]
        my = ahrs.my - off[1]
        mz = ahrs.mz - off[2]
        # Also publish raw mag so the cal wizard can compute fresh offsets.
        state['mx'] = ahrs.mx
        state['my'] = ahrs.my
        state['mz'] = ahrs.mz
        ahrs.new_mag = False
    else:
        mx = my = mz = None

    ahrs_filter.update(gx, gy, gz, ax, ay, az, mx, my, mz, dt=dt)
    a_c_mag = math.sqrt(acx*acx + acy*acy + acz*acz)
    return att_aid, a_c_mag


# ── Sensor loop ──────────────────────────────────────────────────────────────
async def sensor_loop(ahrs: WT901, gps: GPS, baro, sdp, ahrs_filter,
                      sdp_auto_zero=False):
    """
    Poll all sensors at ~50 Hz and push values into the shared state dict.
    The web server reads state independently at BROADCAST_HZ.

    baro:        BME280 instance or None (falls back to GPS altitude).
    sdp:         SDP31  instance or None (no air-data, GPS GS used for speed).
    ahrs_filter: Mahony instance, or None to fall back to WT901 PKT_ANGLE.
    """
    tick = 0
    last_ahrs_ms = utime.ticks_ms()   # 5-second window before ahrs_ok goes False
    last_gps_nmea_ms = None           # last time a valid NMEA sentence arrived
    last_sdp_ms = None                # last successful SDP33 read
    filt_last_ms     = None           # for filter dt computation
    filt_seeded      = False          # PKT_ANGLE-based seeding done?
    gps_slave_last_ms = utime.ticks_ms()
    align_start_ms   = None           # first gyro packet timestamp; gates
                                      # the AHRS_ALIGN_DURATION_S align banner
    _align_ms        = int(AHRS_ALIGN_DURATION_S * 1000)
    while True:
        # ── AHRS ──
        try:
            ahrs.update()   # drain UART; per-packet flags set on driver
            _conn = state['orientation']
            _mount = state['mounting']
            _hdg_off = _hdg_offset_for(_conn)

            # Always publish the WT901's internal PKT_ANGLE yaw, post-remap,
            # so the cal wizard's RAW HDG display can show a responsive
            # heading regardless of whether the Mahony filter is also running.
            # The WT901's internal Kalman is much faster than our Mahony in a
            # biased-mag environment, so this gives the pilot a snappy display
            # for capture timing during the 8-cardinal cal procedure.
            _r_wt, _p_wt, _y_wt, _ = _apply_remap(
                ahrs.roll, ahrs.pitch, ahrs.yaw, _conn, _mount)
            state['yaw_wt901'] = (_y_wt - state['yaw_trim'] + _hdg_off) % 360

            if ahrs_filter is not None and ahrs.new_gyro:
                # Filter dt from gyro packet arrival times (clamped 1–100 ms).
                now_ms = utime.ticks_ms()
                if filt_last_ms is None:
                    dt = 0.02
                else:
                    dt_ms = utime.ticks_diff(now_ms, filt_last_ms)
                    if dt_ms < 1:    dt_ms = 1
                    elif dt_ms > 100: dt_ms = 100
                    dt = dt_ms / 1000.0
                filt_last_ms = now_ms

                # Align window: track elapsed time since first gyro packet
                # and broadcast ahrs_aligning so the display can show the
                # AHRS ALIGN banner during the filter's settling window.
                if align_start_ms is None:
                    align_start_ms = now_ms
                state['ahrs_aligning'] = (
                    utime.ticks_diff(now_ms, align_start_ms) < _align_ms)

                # First-run seed from PKT_ANGLE so we don't coast from
                # identity through several seconds of accel-pull convergence.
                if (not filt_seeded) and ahrs.new_angle:
                    ahrs_filter.seed_from_euler_deg(
                        ahrs.roll, ahrs.pitch, ahrs.yaw)
                    filt_seeded = True

                att_aid, a_c_mag = _run_filter_step(ahrs, ahrs_filter, dt)
                state['att_aid']        = att_aid
                state['_a_centri_g']    = a_c_mag
                state['_accel_weight']  = ahrs_filter.last_accel_weight
                ahrs.new_gyro  = False
                ahrs.new_accel = False

                # Filter Euler is in WT901 sensor frame; pass through the
                # existing remap so trim/magdev semantics are preserved.
                f_roll, f_pitch, f_yaw = ahrs_filter.euler_deg()
                if f_yaw < 0:
                    f_yaw += 360.0
                _r, _p, _y, _ = _apply_remap(
                    f_roll, f_pitch, f_yaw, _conn, _mount)
                state['roll']    = _r + state['roll_trim']
                state['pitch']   = _p + state['pitch_trim']
                _raw             = (_y - state['yaw_trim'] + _hdg_off) % 360
                state['yaw_raw'] = _raw
                state['yaw']     = apply_magdev(_raw, state['_magdev'])
                state['ay']      = ahrs.ay * WT901_AY_SIGN
                state['att_src'] = 'mahony'
                last_ahrs_ms     = utime.ticks_ms()

                # GPS-track yaw slaving — low-rate nudge so short-term gyro
                # dynamics still dominate. Target is in the filter's sensor
                # frame so we invert the remap: state_yaw_raw = -f_yaw + off
                # → target_f_yaw = off - state_track - yaw_trim (mod 360).
                if (AHRS_GPS_TRACK_ENABLE
                        and state['gps_ok']
                        and state['speed'] >= AHRS_GPS_TRACK_MIN_KT
                        and utime.ticks_diff(utime.ticks_ms(),
                                             gps_slave_last_ms)
                            >= int(AHRS_GPS_TRACK_INTERVAL_S * 1000)):
                    target_sensor_yaw = (
                        _hdg_off - state['track'] - state['yaw_trim']) % 360
                    ahrs_filter.nudge_yaw_toward_deg(
                        target_sensor_yaw, AHRS_GPS_TRACK_ALPHA)
                    gps_slave_last_ms = utime.ticks_ms()

            elif ahrs_filter is None and ahrs.new_angle:
                # Fallback path: use WT901's internal Euler (pre-filter behaviour).
                now_ms = utime.ticks_ms()
                if align_start_ms is None:
                    align_start_ms = now_ms
                state['ahrs_aligning'] = (
                    utime.ticks_diff(now_ms, align_start_ms) < _align_ms)
                _r, _p, _y, _ = _apply_remap(
                    ahrs.roll, ahrs.pitch, ahrs.yaw, _conn, _mount)
                state['roll']    = _r + state['roll_trim']
                state['pitch']   = _p + state['pitch_trim']
                _raw             = (_y - state['yaw_trim'] + _hdg_off) % 360
                state['yaw_raw'] = _raw
                state['yaw']     = apply_magdev(_raw, state['_magdev'])
                state['ay']      = ahrs.ay * WT901_AY_SIGN
                state['att_src'] = 'wt901'
                state['att_aid'] = 'basic'
                ahrs.new_angle   = False
                last_ahrs_ms     = now_ms
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
        if state.get('_save_magcal'):
            save_magcal(state['_mag_offset'])
            state['_save_magcal'] = False
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
                    (sdp_auto_zero and tick == 100)):
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
                    'att_src','att_aid','ahrs_aligning',
                    'mx','my','mz','fw_ver','yaw_wt901',
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
    elif line.startswith('$MAGOFF,'):
        payload = line[8:]
        if payload == 'CLEAR':
            state['_mag_offset'] = (0.0, 0.0, 0.0)
            state['_save_magcal'] = True
            print('$MAGOFF_ACK,CLEARED')
        else:
            try:
                vals = [float(x) for x in payload.split(',') if x.strip()]
                if len(vals) == 3:
                    state['_mag_offset'] = (vals[0], vals[1], vals[2])
                    state['_save_magcal'] = True
                    print(f'$MAGOFF_ACK,OK,{vals[0]:.1f},{vals[1]:.1f},{vals[2]:.1f}')
                else:
                    print(f'$MAGOFF_ACK,ERR got {len(vals)}')
            except Exception as e:
                print(f'$MAGOFF_ACK,ERR {e}')
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
    state['_mag_offset'] = load_magcal()
    if any(abs(v) > 1e-6 for v in state['_mag_offset']):
        print(f'Hard-iron offsets loaded: '
              f'mx={state["_mag_offset"][0]:.1f} '
              f'my={state["_mag_offset"][1]:.1f} '
              f'mz={state["_mag_offset"][2]:.1f}')
    else:
        print('Hard-iron offsets: none stored — raw mag fed to filter')

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

    # Air-data transducer: MS4525DO preferred when enabled, else SDP3x.
    # Both expose the same dp_pa / update() / zero() surface, so the rest of
    # the air-data path (airdata.py, sensor_loop) doesn't care which one ran.
    sdp = None
    sdp_auto_zero = False
    if MS4525_ENABLE:
        try:
            from ms4525 import MS4525
            sdp = MS4525(
                i2c_id    = MS4525_I2C_ID,
                sda       = MS4525_SDA_PIN,
                scl       = MS4525_SCL_PIN,
                addr      = MS4525_I2C_ADDR,
                psi_range = MS4525_PSI_RANGE,
            )
            sdp_auto_zero = MS4525_AUTO_ZERO_AT_BOOT
            print(f'MS4525  I2C{MS4525_I2C_ID}'
                  f' @ 0x{MS4525_I2C_ADDR:02x}'
                  f' (GP{MS4525_SDA_PIN} SDA, GP{MS4525_SCL_PIN} SCL)'
                  f'  ±{MS4525_PSI_RANGE} psi  auto-zero={MS4525_AUTO_ZERO_AT_BOOT}')
        except Exception as e:
            print(f'MS4525 not found ({e})')
            sdp = None
    if sdp is None and SDP31_ENABLE:
        try:
            from sdp31 import SDP31
            sdp = SDP31(
                i2c_id = SDP31_I2C_ID,
                sda    = SDP31_SDA_PIN,
                scl    = SDP31_SCL_PIN,
                addr   = SDP31_I2C_ADDR,
            )
            sdp_auto_zero = SDP31_AUTO_ZERO_AT_BOOT
            print(f'SDP33  I2C{SDP31_I2C_ID}'
                  f' @ 0x{SDP31_I2C_ADDR:02x}'
                  f' (GP{SDP31_SDA_PIN} SDA, GP{SDP31_SCL_PIN} SCL)'
                  f'  scale={sdp.scale}  auto-zero={SDP31_AUTO_ZERO_AT_BOOT}')
        except Exception as e:
            print(f'SDP33 not found ({e})  –  airspeed will fall back to GPS GS')
            sdp = None
    if sdp is None:
        print('No air-data transducer present — IAS/TAS unavailable, '
              'speed tape falls back to GPS GS')

    ahrs_filter = None
    if AHRS_FILTER_ENABLE:
        ahrs_filter = Mahony(
            kp_acc       = AHRS_KP_ACC,
            ki_acc       = AHRS_KI_ACC,
            kp_mag       = AHRS_KP_MAG,
            accel_gate_g = AHRS_ACCEL_GATE_G,
        )
        print(f'Mahony AHRS filter enabled  Kp_acc={AHRS_KP_ACC} '
              f'Ki_acc={AHRS_KI_ACC} Kp_mag={AHRS_KP_MAG}'
              f'  use_mag={AHRS_USE_MAG}  gps_track_aid={AHRS_GPS_TRACK_ENABLE}')
    else:
        print('Mahony filter disabled — using WT901 PKT_ANGLE Euler output')

    await asyncio.gather(
        sensor_loop(ahrs, gps, baro, sdp, ahrs_filter, sdp_auto_zero),
        start_server(state, port=HTTP_PORT),
        stdin_cmd_loop(),
    )


try:
    asyncio.run(main())
except KeyboardInterrupt:
    print('Stopped by user')
finally:
    led.off()
