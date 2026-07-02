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
import gc
import machine
from machine import Pin, WDT

from config import (
    WT901_UART_ID, WT901_TX_PIN, WT901_RX_PIN, WT901_BAUD,
    WT901_FORCE_DEFAULT_OUTPUT,
    WT901_HIGH_RATE, WT901_LOWRATE_BANDWIDTH_CODE,
    WT901_HIGHRATE_RATE_CODE, WT901_HIGHRATE_BANDWIDTH_CODE,
    WT901_TARGET_BAUD, WT901_TARGET_BAUD_CODE,
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
    AHRS_ACCEL_GATE_G, AHRS_USE_MAG, AHRS_BIAS_CLAMP_DPS,
    AHRS_MAG_GYRO_GATE_LO_DPS, AHRS_MAG_GYRO_GATE_HI_DPS,
    AHRS_GPS_TRACK_ENABLE, AHRS_GPS_TRACK_MIN_KT,
    AHRS_GPS_TRACK_INTERVAL_S, AHRS_GPS_TRACK_ALPHA,
    AHRS_FWD_IN_SENSOR, AHRS_ALIGN_DURATION_S, FW_VERSION,
    AHRS_PITCH_ALIGN, AHRS_ROLL_ALIGN,
    AHRS_DYN_GYRO_LO_DPS, AHRS_DYN_GYRO_HI_DPS,
    AHRS_DYN_AC_LO_G, AHRS_DYN_AC_HI_G, AHRS_DYN_KP_SCALE_MIN,
    AHRS_CENTRI_GYRO_TAU_S, AHRS_LINEAR_ACCEL_AID, AHRS_CENTRIPETAL_AID,
    AHRS_DEBUG_PRINT, AHRS_DEBUG_PRINT_DECIM,
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
BARO_FILE   = 'baro.json'     # pilot QNH so the altimeter setting survives reboot
BOOT_LOG_FILE = 'boot_log.json'  # boot count + last reset cause + last alive ms

# Human-readable map for machine.reset_cause(). Values vary by port; the names
# we care about are PWRON_RESET (clean power-up), WDT_RESET (watchdog fired,
# usually means a task starved the scheduler > 8 s), SOFT_RESET (machine.reset()
# or REPL Ctrl-D), HARD_RESET (RUN pin or external reset).
_RESET_NAMES = {}
for _n in ('PWRON_RESET', 'WDT_RESET', 'SOFT_RESET', 'HARD_RESET',
           'DEEPSLEEP_RESET', 'BROWN_OUT_RESET'):
    _v = getattr(machine, _n, None)
    if _v is not None:
        _RESET_NAMES[_v] = _n


def load_boot_log():
    try:
        with open(BOOT_LOG_FILE, 'r') as f:
            d = ujson.loads(f.read())
        return {
            'boot_count':       int(d.get('boot_count', 0)),
            'last_cause':       int(d.get('last_cause', -1)),
            'last_alive_ms':    int(d.get('last_alive_ms', 0)),
        }
    except Exception:
        return {'boot_count': 0, 'last_cause': -1, 'last_alive_ms': 0}


def save_boot_log(d):
    try:
        with open(BOOT_LOG_FILE, 'w') as f:
            f.write(ujson.dumps(d))
    except Exception as e:
        print(f'save_boot_log failed: {e}')


async def alive_ticker():
    """Persist current uptime to flash every 60 s. On the next boot, the boot
    logger reports the value as 'last alive ms' — i.e. how long we ran before
    we died. Lets us bracket the death to within a one-minute window without
    needing to be attached to the REPL when it happens.

    Interval chosen against RP2040 flash endurance (~100k erase cycles per
    sector): 60 s gives ≳ 5 years of continuous operation before we'd start
    worrying about wear. 5 s — what we used during the bring-up soak — would
    have burned through that in under a week."""
    while True:
        try:
            d = load_boot_log()
            d['last_alive_ms'] = utime.ticks_ms()
            save_boot_log(d)
        except Exception:
            pass
        await asyncio.sleep(60)


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


def load_baro():
    """Return the persisted QNH (hPa), or the ISA default if none/invalid.
    Range-checked so a corrupt file can't seed an absurd altimeter setting."""
    try:
        with open(BARO_FILE, 'r') as f:
            qnh = float(ujson.loads(f.read()).get('baro_hpa', BME280_QNH_DEFAULT))
        if 800.0 <= qnh <= 1100.0:
            return qnh
    except Exception:
        pass
    return BME280_QNH_DEFAULT


def save_baro(state):
    try:
        with open(BARO_FILE, 'w') as f:
            f.write(ujson.dumps({'baro_hpa': state['baro_hpa']}))
    except Exception as e:
        print(f'save_baro failed: {e}')


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
    """Returns (connector, mounting, pitch_align, roll_align).  The two
    align fields are fine-grained input-side rotations that compensate
    for sensor mounting that's a few degrees off the four 90° connector
    quanta — see config.AHRS_PITCH_ALIGN."""
    _valid_c = ('forward', 'right', 'left', 'aft')
    _valid_m = ('normal', 'inverted')
    try:
        with open(ORIENT_FILE, 'r') as f:
            d = ujson.loads(f.read())
        c  = d.get('connector', AHRS_CONNECTOR)
        m  = d.get('mounting',  AHRS_MOUNTING)
        pa = float(d.get('pitch_align', AHRS_PITCH_ALIGN))
        ra = float(d.get('roll_align',  AHRS_ROLL_ALIGN))
        if c not in _valid_c: c = AHRS_CONNECTOR
        if m not in _valid_m: m = AHRS_MOUNTING
        # Clamp to ±15° — the flight-zero (LEVEL) cage folds the mounting
        # residual into the alignment, so give it headroom beyond a hand-dialed
        # trim; the connector quanta still handle larger (90°) rotations.
        if pa < -15.0: pa = -15.0
        elif pa > 15.0: pa = 15.0
        if ra < -15.0: ra = -15.0
        elif ra > 15.0: ra = 15.0
        return c, m, pa, ra
    except Exception:
        return AHRS_CONNECTOR, AHRS_MOUNTING, AHRS_PITCH_ALIGN, AHRS_ROLL_ALIGN


def save_orient(state):
    try:
        with open(ORIENT_FILE, 'w') as f:
            f.write(ujson.dumps({'connector':   state['orientation'],
                                  'mounting':    state['mounting'],
                                  'pitch_align': state['pitch_align'],
                                  'roll_align':  state['roll_align']}))
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
    # Input-side axis alignment (degrees; airframe convention).
    # Applied to raw gyro/accel/mag before the Mahony filter to kill
    # yaw → pitch/roll coupling from imperfect sensor mounting.
    'pitch_align': AHRS_PITCH_ALIGN,
    'roll_align':  AHRS_ROLL_ALIGN,
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
    'ahrs_zupt' : False,   # True when Zero-Velocity Update is engaged (gyro
                           # input forced to zero because the airframe is
                           # confirmed stationary — prevents long-term drift).
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
    # by the tumble-cal wizard from the display. Loaded from magcal.json.
    '_mag_offset': (0.0, 0.0, 0.0),
    # Tumble-cal state: when active, accumulate min/max of each mag axis
    # for offset computation on $MAGOFF,FINISH.
    '_magtumble_active': False,
    '_magtumble_min'   : [None, None, None],
    '_magtumble_max'   : [None, None, None],
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

# ── Linear-acceleration aid for the filter ──────────────────────────────────
# Without this, forward acceleration / braking show up as pitch changes —
# the accelerometer measures (gravity − dV/dt), so an accelerating airframe
# looks like nose-up to the filter and a braking one looks like nose-down.
# We differentiate the active speed source (TAS preferred, GS fallback),
# low-pass-filter the result, and add it as a body-forward component to the
# existing centripetal-correction subtraction.
_LIN_ACCEL_ALPHA       = 0.1     # IIR α for smoothing dV/dt — τ ≈ 200 ms at 50 Hz
_LIN_ACCEL_MIN_DT_S    = 0.05    # minimum dt before computing a new sample
_LIN_ACCEL_DV_SPIKE_MS = 5.0     # m/s — guards against fix loss / source switch
_lin_v_ms     = 0.0
_lin_v_t_ms   = 0
_lin_a_filt   = 0.0     # filtered linear accel (m/s²), forward = positive
_lin_v_valid  = False   # only differentiate once we've seen two consecutive valid samples

# Low-passed gyro (rad/s) for the centripetal + gain-scheduling math only.
# Tracks the true (slow) turn rate while rejecting the fast, zero-mean gyro
# vibration that would otherwise fabricate ~1 g of bogus centripetal accel.
_gyro_lpf       = [0.0, 0.0, 0.0]
_gyro_lpf_valid = False


def _update_linear_accel():
    """Compute forward linear acceleration from rate of change of the active
    speed source. Returns m/s² along aircraft +forward (matches the sign
    convention of AHRS_FWD_IN_SENSOR — a positive value = accelerating).
    Smoothed with a 200 ms time constant to suppress GPS-rate jitter and
    SDP/MS4525 noise."""
    global _lin_v_ms, _lin_v_t_ms, _lin_a_filt, _lin_v_valid

    # Same source ladder as the centripetal correction: TAS preferred → GS.
    if state.get('airdata_ok') and state.get('tas_kt', 0.0) > 5.0:
        v_kt = state['tas_kt']
        v_ok = True
    elif state.get('gps_ok') and state.get('speed', 0.0) > 1.0:
        v_kt = state['speed']
        v_ok = True
    else:
        v_kt = 0.0
        v_ok = False

    now_ms = utime.ticks_ms()
    v_ms = v_kt * _KT_TO_M_S

    if not v_ok:
        # Lost the source — decay the filtered value toward zero and reset
        # the differentiator state so the next valid sample doesn't compute
        # a huge bogus dV/dt against a stale prior value.
        _lin_a_filt *= 0.5
        _lin_v_valid = False
        return _lin_a_filt

    if not _lin_v_valid:
        # First valid sample after a gap — prime the differentiator.
        _lin_v_ms = v_ms
        _lin_v_t_ms = now_ms
        _lin_v_valid = True
        return _lin_a_filt

    dt_s = utime.ticks_diff(now_ms, _lin_v_t_ms) / 1000.0
    if dt_s < _LIN_ACCEL_MIN_DT_S:
        return _lin_a_filt   # too soon — hold last filtered value

    dv_ms = v_ms - _lin_v_ms
    # Reject obviously bogus jumps (GPS reacquisition, source switch).
    if abs(dv_ms) > _LIN_ACCEL_DV_SPIKE_MS:
        _lin_v_ms = v_ms
        _lin_v_t_ms = now_ms
        return _lin_a_filt

    raw_a = dv_ms / dt_s
    _lin_a_filt = (1.0 - _LIN_ACCEL_ALPHA) * _lin_a_filt + _LIN_ACCEL_ALPHA * raw_a
    _lin_v_ms = v_ms
    _lin_v_t_ms = now_ms
    return _lin_a_filt


# ── Zero-Velocity Update (ZUPT) ──────────────────────────────────────────────
# When we're confident the airframe is stationary, force gyro input to the
# Mahony to zero. This eliminates any drift from gyro bias (temperature,
# component aging) over long stationary periods — the user left the system
# powered overnight and saw the attitude indicator slowly walk in roll/pitch.
# The accel correction stays active so roll/pitch lock to the gravity vector;
# yaw freezes at its last estimate (mag aiding still allowed if enabled).
_ZUPT_GS_THRESHOLD_KT     = 1.0     # GPS GS below this counts as still
_ZUPT_IAS_THRESHOLD_KT    = 5.0     # IAS below this counts as still
_ZUPT_GYRO_THRESHOLD_DPS  = 1.0     # gyro magnitude below this (deg/s)
_ZUPT_ACCEL_GATE_G        = 0.05    # |a|-1g| below this (tighter than filter)
_ZUPT_QUIET_DURATION_MS   = 3000    # all conditions sustained this long → engage

_zupt_quiet_since_ms = None         # monotonic ms when conditions first held


def _detect_stationary(ahrs, now_ms):
    """Conservative stationary detector. Returns True only after every
    condition has held continuously for _ZUPT_QUIET_DURATION_MS:
      - GPS groundspeed below threshold (or no fix at all)
      - Air-data unavailable OR IAS below threshold
      - Gyro magnitude below threshold (no rotation)
      - Accel magnitude within tight band of 1g (no linear motion)
    All four must hold so we don't accidentally ZUPT during a hover, a
    coordinated zero-G push-over, or any other "GPS is zero but the
    aircraft is moving" edge case."""
    global _zupt_quiet_since_ms

    gs = state.get('speed', 0.0) or 0.0
    gps_quiet = (not state.get('gps_ok')) or gs < _ZUPT_GS_THRESHOLD_KT
    ias = state.get('ias_kt', 0.0) or 0.0
    air_quiet = (not state.get('airdata_ok')) or ias < _ZUPT_IAS_THRESHOLD_KT

    gyro_mag = math.sqrt(ahrs.wx*ahrs.wx + ahrs.wy*ahrs.wy + ahrs.wz*ahrs.wz)
    gyro_quiet = gyro_mag < _ZUPT_GYRO_THRESHOLD_DPS

    accel_mag = math.sqrt(ahrs.ax*ahrs.ax + ahrs.ay*ahrs.ay + ahrs.az*ahrs.az)
    accel_quiet = abs(accel_mag - 1.0) < _ZUPT_ACCEL_GATE_G

    if gps_quiet and air_quiet and gyro_quiet and accel_quiet:
        if _zupt_quiet_since_ms is None:
            _zupt_quiet_since_ms = now_ms
        elif utime.ticks_diff(now_ms, _zupt_quiet_since_ms) >= _ZUPT_QUIET_DURATION_MS:
            return True
    else:
        _zupt_quiet_since_ms = None
    return False


def _hdg_offset_for(connector):
    """Connector orientation → heading offset applied in the Euler remap."""
    if connector == 'forward': return 90.0
    if connector == 'left':    return 180.0
    if connector == 'aft':     return 270.0
    return 0.0   # 'right' (default)


def _fwd_in_sensor_for(connector):
    """Aircraft +forward unit vector expressed in the WT901 sensor frame,
    derived from the connector orientation. Used by both the centripetal
    correction (a_c = ω × V_fwd_sensor) and the linear-acceleration aid
    (a_lin · fwd_sensor) — both need to subtract inertial acceleration along
    the *actual* aircraft forward direction, which depends on how the chip
    is rotated in the airframe.

    Reference: in 'right' mode (connector points right of aircraft), the
    WT901's +Y axis points along aircraft forward. Each 90° change in
    connector orientation rotates the chip by 90° about its +Z axis, so
    the aircraft-forward direction in chip frame rotates correspondingly.

    Returns a 3-tuple (x, y, z) unit vector. Caller may also use the
    AHRS_FWD_IN_SENSOR config constant as a manual override for installations
    that don't match the standard connector geometry."""
    if connector == 'forward':
        return (1.0, 0.0, 0.0)
    if connector == 'left':
        return (0.0, -1.0, 0.0)
    if connector == 'aft':
        return (-1.0, 0.0, 0.0)
    return (0.0, 1.0, 0.0)   # 'right' (default)


def _ac_axes_in_sensor(connector):
    """Return (roll_axis, pitch_axis) as 3-tuples expressed in the WT901
    sensor frame, given the connector orientation.  Derived from the
    same axis mapping that _apply_remap uses on the output Euler — kept
    in sync by inspection (a mismatch would make ALIGN behave the
    opposite of what the user expects)."""
    if connector == 'forward':
        # output: airframe roll = +sensor_pitch (axis = sensor +Y)
        #         airframe pitch = +sensor_roll (axis = sensor +X)
        return ((0.0, 1.0, 0.0), (1.0, 0.0, 0.0))
    if connector == 'left':
        # output: airframe roll = +sensor_roll (axis = sensor +X)
        #         airframe pitch = -sensor_pitch (axis = sensor -Y)
        return ((1.0, 0.0, 0.0), (0.0, -1.0, 0.0))
    if connector == 'aft':
        # output: airframe roll = -sensor_pitch (axis = sensor -Y)
        #         airframe pitch = -sensor_roll (axis = sensor -X)
        return ((0.0, -1.0, 0.0), (-1.0, 0.0, 0.0))
    # 'right' (default): airframe roll = -sensor_roll, pitch = +sensor_pitch
    return ((-1.0, 0.0, 0.0), (0.0, 1.0, 0.0))


def _rot_about_axis(vec, axis, theta_rad):
    """Rotate `vec` around the unit-vector `axis` by `theta_rad` radians.
    Rodrigues' formula.  Exact (not small-angle) so the math stays
    correct even if the pilot dials in a larger trim."""
    if theta_rad == 0.0:
        return vec
    s = math.sin(theta_rad)
    c = math.cos(theta_rad)
    kx, ky, kz = axis
    vx, vy, vz = vec
    # k × v
    cx = ky * vz - kz * vy
    cy = kz * vx - kx * vz
    cz = kx * vy - ky * vx
    # k · v
    d = kx * vx + ky * vy + kz * vz
    return (vx * c + cx * s + kx * d * (1.0 - c),
            vy * c + cy * s + ky * d * (1.0 - c),
            vz * c + cz * s + kz * d * (1.0 - c))


def _apply_axis_align(gx, gy, gz, ax, ay, az, mx, my, mz,
                       pitch_align_deg, roll_align_deg, connector):
    """Rotate raw sensor vectors by the NEGATIVE of the alignment
    angles around the airframe pitch and roll axes (expressed in
    sensor frame).  Compensates for small mounting misalignment that
    would otherwise couple yaw rate into the pitch/roll channels.

    No-op when both align values are exactly zero — fast path for the
    common case of an aligned sensor."""
    if pitch_align_deg == 0.0 and roll_align_deg == 0.0:
        return gx, gy, gz, ax, ay, az, mx, my, mz
    roll_axis, pitch_axis = _ac_axes_in_sensor(connector)
    pa = -math.radians(pitch_align_deg)
    ra = -math.radians(roll_align_deg)
    g = _rot_about_axis((gx, gy, gz), pitch_axis, pa)
    g = _rot_about_axis(g,            roll_axis,  ra)
    a = _rot_about_axis((ax, ay, az), pitch_axis, pa)
    a = _rot_about_axis(a,            roll_axis,  ra)
    if mx is not None:
        m = _rot_about_axis((mx, my, mz), pitch_axis, pa)
        m = _rot_about_axis(m,            roll_axis,  ra)
        return g[0], g[1], g[2], a[0], a[1], a[2], m[0], m[1], m[2]
    return g[0], g[1], g[2], a[0], a[1], a[2], mx, my, mz


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

    # Low-pass the gyro for the inertial-accel corrections and gain scheduling.
    # The full-rate (gx,gy,gz) still drives the Mahony integration below; only
    # the centripetal term and the dyn-gain detectors use this smoothed rate,
    # so residual vibration can't manufacture a fake centripetal vector or
    # falsely trip the maneuver detector.
    global _gyro_lpf, _gyro_lpf_valid
    if not _gyro_lpf_valid:
        _gyro_lpf = [gx, gy, gz]
        _gyro_lpf_valid = True
    else:
        _ga = dt / (AHRS_CENTRI_GYRO_TAU_S + dt)
        _gyro_lpf[0] += _ga * (gx - _gyro_lpf[0])
        _gyro_lpf[1] += _ga * (gy - _gyro_lpf[1])
        _gyro_lpf[2] += _ga * (gz - _gyro_lpf[2])
    gx_s, gy_s, gz_s = _gyro_lpf

    # ZUPT: when we're sure the airframe is stationary, two things happen.
    #
    # 1. The filter's bias estimate is actively TRACKED from the raw gyro
    #    readings. Stationary by definition means true_rate=0, so the gyro
    #    reading equals the bias. Seed the estimate from the current gyro
    #    sample on the rising edge of ZUPT (single-sample MEMS gyro noise
    #    < 0.05°/s, far better than a stale value), then LPF it with
    #    α=0.001 (τ ≈ 20 s at 50 Hz) so the estimate tracks slow temperature
    #    drift in the chip without chasing single-sample noise.
    # 2. freeze_bias=True is passed to the filter so its own
    #    accel-cross-product integrator doesn't compete with our
    #    gyro-derived tracking.
    #
    # Why this beats the previous "just freeze bias" approach: a frozen
    # estimate stays put while the chip's real bias slowly drifts. The
    # filter then sees an apparent rate of (real_bias − frozen_estimate)
    # which it integrates as rotation. Over hours, that walks the attitude
    # by several degrees on a stationary bench. Tracking the bias from
    # raw gyro instead keeps the estimate aligned with the real bias.
    zupt = _detect_stationary(ahrs, utime.ticks_ms())
    state['ahrs_zupt'] = zupt
    _was_zupt = state.get('_zupt_prev', False)
    state['_zupt_prev'] = zupt
    if zupt:
        if not _was_zupt:
            # ZUPT just engaged — seed bias from the current gyro reading.
            ahrs_filter.bx = gx
            ahrs_filter.by = gy
            ahrs_filter.bz = gz
        else:
            # Slow-track real bias drift via gyro readings.
            _bias_alpha = 0.001
            ahrs_filter.bx = (1.0 - _bias_alpha) * ahrs_filter.bx + _bias_alpha * gx
            ahrs_filter.by = (1.0 - _bias_alpha) * ahrs_filter.by + _bias_alpha * gy
            ahrs_filter.bz = (1.0 - _bias_alpha) * ahrs_filter.bz + _bias_alpha * gz
        # Don't zero the gyro here — the filter's internal "gyro − bias"
        # correction yields ≈0 because bias now accurately tracks the
        # gyro reading. Zeroing gyro AND keeping a stale bias is what
        # caused the drift bug in the first place.

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

    # Aircraft-forward unit vector in chip frame — depends on connector
    # orientation. Both centripetal (ω × V) and linear-accel (dV/dt) use it
    # to project the speed-source-derived inertial acceleration onto the
    # right chip axis. Hardcoding it to (0,1,0) was correct only for the
    # 'right' connector default; in 'aft' (or any rotated mounting), this
    # silently sent corrections along the wrong axis and pitch ended up off
    # by 8-10° during straight-line braking.
    fwd = _fwd_in_sensor_for(state['orientation'])

    # Centripetal accel in sensor frame: a_c = ω × V_fwd_sensor (m/s²).
    # Gated behind AHRS_CENTRIPETAL_AID: in a vibrating cockpit on GPS-only
    # speed this term (driven by a noisy/biased gyro × a large V) injects a
    # steady 0.09-0.14 g into the accel that biases pitch and drops it out of
    # the 1 g gate — net-negative like the mag and linear aids. With it off the
    # filter is plain accel+gyro; the magnitude gate + gyro-rate gain schedule
    # still de-weight the accel through real turns.
    acx = acy = acz = 0.0
    if AHRS_CENTRIPETAL_AID:
        v_ms = v_kt * _KT_TO_M_S
        vx = fwd[0] * v_ms
        vy = fwd[1] * v_ms
        vz = fwd[2] * v_ms
        acx = (gy_s*vz - gz_s*vy) * _G_PER_M_S2
        acy = (gz_s*vx - gx_s*vz) * _G_PER_M_S2
        acz = (gx_s*vy - gy_s*vx) * _G_PER_M_S2

    # Linear-acceleration aid: forward-axis a = dV/dt projected onto sensor
    # forward direction. Without this, braking and accelerating in
    # straight-line flight produce false pitch changes. Same sign convention
    # as centripetal — added to ahrs.ax/y/z to recover gravity.
    if AHRS_LINEAR_ACCEL_AID:
        a_lin_ms2 = _update_linear_accel()
        state['_a_lin_ms2'] = a_lin_ms2   # diagnostic
        acx += fwd[0] * a_lin_ms2 * _G_PER_M_S2
        acy += fwd[1] * a_lin_ms2 * _G_PER_M_S2
        acz += fwd[2] * a_lin_ms2 * _G_PER_M_S2
    else:
        state['_a_lin_ms2'] = 0.0

    # WT901 reads specific force (stationary level = +1g on Z): adding the
    # inertial centripetal+linear vector recovers the gravity direction.
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
        # Tumble-cal: while $MAGOFF,START is active, track min/max of raw
        # mag on every axis. Many samples → good ellipse-center estimate
        # on FINISH.
        if state.get('_magtumble_active'):
            mn = state['_magtumble_min']
            mx_arr = state['_magtumble_max']
            for i, v in enumerate((ahrs.mx, ahrs.my, ahrs.mz)):
                if mn[i] is None or v < mn[i]: mn[i] = v
                if mx_arr[i] is None or v > mx_arr[i]: mx_arr[i] = v
        ahrs.new_mag = False
    else:
        mx = my = mz = None

    # Input-side axis alignment — rotates gyro/accel/mag so a small
    # sensor mounting misalignment doesn't couple yaw rate into the
    # pitch/roll channels.  No-op when both align values are 0.
    gx, gy, gz, ax, ay, az, mx, my, mz = _apply_axis_align(
        gx, gy, gz, ax, ay, az, mx, my, mz,
        state.get('pitch_align', 0.0),
        state.get('roll_align',  0.0),
        state['orientation'])

    # Dynamic gain scheduling: scale kp_acc down during active maneuvers
    # so the gyro dominates through turns / climbs / taxi bumps.  Two
    # detectors, both ramping linearly between LO (factor = 1) and HI
    # (factor = MIN); whichever is more dynamic wins.
    #
    #   gyro factor: keys off total rotation rate.  Catches yaw-only
    #     events (uncoordinated skid, rudder kicks) where the centripetal
    #     compensation can't see what's happening because V × ω depends
    #     on coordinated flight.
    #
    #   centripetal factor: keys off magnitude of the (V × ω) vector
    #     we already subtracted from accel.  Catches sustained turns
    #     where the residual after imperfect comp + small alignment
    #     error would otherwise drag attitude.
    #
    # During quiescent flight both factors = 1.0 and the filter has full
    # accel authority; in a turn they drop to AHRS_DYN_KP_SCALE_MIN
    # (default 0.1) and the gyro carries the load.
    # Smoothed rate (rotation preserves magnitude through the small align) so
    # gyro vibration doesn't false-trigger the maneuver detector and throttle
    # the accel authority during otherwise-quiescent flight.
    gyro_mag_dps = math.degrees(math.sqrt(gx_s*gx_s + gy_s*gy_s + gz_s*gz_s))
    a_c_mag = math.sqrt(acx*acx + acy*acy + acz*acz)
    g_lo, g_hi = AHRS_DYN_GYRO_LO_DPS, AHRS_DYN_GYRO_HI_DPS
    c_lo, c_hi = AHRS_DYN_AC_LO_G, AHRS_DYN_AC_HI_G
    floor = AHRS_DYN_KP_SCALE_MIN
    if gyro_mag_dps <= g_lo:
        gf = 1.0
    elif gyro_mag_dps >= g_hi:
        gf = floor
    else:
        gf = 1.0 - (gyro_mag_dps - g_lo) / (g_hi - g_lo) * (1.0 - floor)
    if a_c_mag <= c_lo:
        cf = 1.0
    elif a_c_mag >= c_hi:
        cf = floor
    else:
        cf = 1.0 - (a_c_mag - c_lo) / (c_hi - c_lo) * (1.0 - floor)
    dyn_scale = gf if gf < cf else cf
    state['_dyn_kp_scale'] = dyn_scale   # diagnostic — surfaces in $AHRS

    # Pass ZUPT state into the filter so it can freeze the bias integrator
    # while stationary. Otherwise the integrator winds up to its clamp over
    # hours and the clamped bias produces a persistent attitude offset that
    # the accel correction can't pull back from.
    ahrs_filter.update(gx, gy, gz, ax, ay, az, mx, my, mz,
                       dt=dt, freeze_bias=zupt,
                       dyn_kp_scale=dyn_scale)
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
    # Hardware watchdog. Auto-reboots the Pico if sensor_loop fails to feed
    # within the timeout — recovers from MicroPython lock-ups that would
    # otherwise leave the AHRS dark (e.g., asyncio stall, USB CDC backpressure
    # blocking a print(), unhandled exception in an async task). The 8 s
    # window is well above any legitimate loop latency but well below pilot
    # patience after a stall.
    #
    # WDT is now initialised in main() and fed by a dedicated async task
    # — see the wdt_loop coroutine below. Sensor_loop no longer touches
    # the watchdog directly so HTTP-serve or GC pauses in other tasks
    # can't starve the WDT feed.
    _wdt = None
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

                # TEMPORARY — debug-print for the roll→yaw coupling bug.
                # Capture: stop pfd.service on the Pi, run
                #   python3 -m mpremote connect /dev/ttyACM0 | tee ahrs_debug.log
                # roll the unit slowly ±30° about the bench-bank axis, then
                # grep ahrs_debug.log for '$AHRSDBG'.  Set AHRS_DEBUG_PRINT
                # = False in config.py and re-flash to silence.
                if (AHRS_DEBUG_PRINT
                        and tick % AHRS_DEBUG_PRINT_DECIM == 0):
                    fq = ahrs_filter
                    try:
                        print('$AHRSDBG,'
                              'q={:.4f},{:.4f},{:.4f},{:.4f},'
                              'sf_rpy={:.2f},{:.2f},{:.2f},'
                              'bdy_rpy={:.2f},{:.2f},{:.2f},'
                              'acc={:.3f},{:.3f},{:.3f},'
                              'gyr={:.2f},{:.2f},{:.2f},'
                              'mag={:.0f},{:.0f},{:.0f},'
                              'mag_err={:.4f},{:.4f},{:.4f},'
                              'acc_w={:.2f},mag_w={:.2f},a_c_g={:.3f},'
                              'bias={:.2f},{:.2f},{:.2f}'
                              .format(fq.q0, fq.q1, fq.q2, fq.q3,
                                      f_roll, f_pitch, f_yaw,
                                      state['roll'], state['pitch'],
                                      state['yaw'],
                                      ahrs.ax, ahrs.ay, ahrs.az,
                                      ahrs.wx, ahrs.wy, ahrs.wz,
                                      ahrs.mx, ahrs.my, ahrs.mz,
                                      fq.last_mag_err_x,
                                      fq.last_mag_err_y,
                                      fq.last_mag_err_z,
                                      fq.last_accel_weight,
                                      fq.last_mag_weight,
                                      state.get('_a_centri_g', 0.0),
                                      math.degrees(fq.bx),
                                      math.degrees(fq.by),
                                      math.degrees(fq.bz)))
                    except Exception:
                        pass

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
        if state.get('_save_baro'):
            save_baro(state)
            state['_save_baro'] = False

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
                state['_save_baro'] = True         # persist the calibrated QNH

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
                    'orientation','mounting','pitch_align','roll_align',
                    'ias_kt','tas_kt','dp_pa','oat_c','dens_alt_ft',
                    'wind_dir','wind_kt','airdata_ok',
                    'att_src','att_aid','ahrs_aligning','ahrs_zupt',
                    'mx','my','mz','fw_ver','yaw_wt901',
                )}
                print('$AHRS,' + ujson.dumps(_usb))
            except Exception:
                pass

        # Heartbeat LED: blink every 2 s (100 × 20 ms)
        if tick % 100 == 0:
            led.toggle()

        # WT901 packet-type counters every 10 s. Lets us diagnose situations
        # where individual packet types stop arriving (e.g., PKT_ANGLE 0x53
        # disappearing while ACC/GYRO/MAG keep flowing). If cnt_angle is
        # stuck at the same value across reports, the chip isn't sending
        # PKT_ANGLE regardless of our RSW reconfigure attempts.
        if tick % 500 == 0:
            print(f'[WT901] pkt counts  acc={ahrs.cnt_accel} '
                  f'gyro={ahrs.cnt_gyro} angle={ahrs.cnt_angle} '
                  f'mag={ahrs.cnt_mag} quat={ahrs.cnt_quat} '
                  f'bad_cksum={ahrs.cnt_bad_cksum}')

        # Periodic explicit GC. MicroPython's incremental GC can starve when
        # the loop allocates heavily (JSON encoding at BROADCAST_HZ, the WT901
        # driver's bytearray slicing). Forcing a collect every ~2 s keeps the
        # heap from fragmenting into the slow path. Cheap on the RP2350.
        if tick % 100 == 0:
            gc.collect()

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
            state['_magtumble_active'] = False
            print('$MAGOFF_ACK,CLEARED')
        elif payload == 'START':
            # Begin tumble-cal min/max tracking. Sensor loop accumulates
            # the min and max of every raw mag axis until FINISH lands.
            state['_magtumble_active'] = True
            state['_magtumble_min'] = [None, None, None]
            state['_magtumble_max'] = [None, None, None]
            print('$MAGOFF_ACK,START_OK')
        elif payload == 'FINISH':
            if state.get('_magtumble_active') and all(v is not None
                    for v in state.get('_magtumble_min', [None]*3)):
                mn = state['_magtumble_min']
                mx = state['_magtumble_max']
                off = (0.5 * (mn[0] + mx[0]),
                       0.5 * (mn[1] + mx[1]),
                       0.5 * (mn[2] + mx[2]))
                spread = (mx[0]-mn[0], mx[1]-mn[1], mx[2]-mn[2])
                state['_mag_offset'] = off
                state['_save_magcal'] = True
                state['_magtumble_active'] = False
                print(f'$MAGOFF_ACK,FINISH_OK,'
                      f'{off[0]:.1f},{off[1]:.1f},{off[2]:.1f},'
                      f'spread={spread[0]:.0f},{spread[1]:.0f},{spread[2]:.0f}')
            else:
                state['_magtumble_active'] = False
                print('$MAGOFF_ACK,FINISH_ERR no samples')
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
    elif line.startswith('$ALIGN,'):
        # $ALIGN,<pitch_deg>,<roll_deg> — input-side axis alignment.
        # Clamped to ±15° before applying so a typo doesn't send the
        # filter into an unrecoverable state on the next packet (the
        # flight-zero LEVEL cage uses the full ±15° range).
        parts = line[7:].split(',')
        if len(parts) == 2:
            try:
                pa = float(parts[0].strip())
                ra = float(parts[1].strip())
                if pa < -15.0: pa = -15.0
                elif pa > 15.0: pa = 15.0
                if ra < -15.0: ra = -15.0
                elif ra > 15.0: ra = 15.0
                state['pitch_align'] = pa
                state['roll_align']  = ra
                state['_save_orient'] = True
                print(f'$ALIGN_ACK,{pa:+.2f},{ra:+.2f},OK')
            except ValueError as e:
                print(f'$ALIGN_ACK,ERR parse: {e}')
        else:
            print('$ALIGN_ACK,ERR bad format')
    elif line.startswith('$BARO,'):
        # $BARO,<qnh_hpa>     — set the Kollsman QNH (mirrors HTTP /baro?qnh)
        # $BARO,CAL,<alt_ft>  — back-calc QNH from a known field elevation
        # This is the USB-serial twin of the WiFi-only /baro endpoint: without
        # it, a display on the USB link (the Pi Zero's primary path) can't move
        # the AHRS-computed altitude at all.
        payload = line[6:].strip()
        if payload.startswith('CAL,'):
            try:
                state['_cal_ft'] = float(payload[4:])
                print('$BARO_ACK,CAL_OK')
            except ValueError as e:
                print(f'$BARO_ACK,CAL_ERR {e}')
        else:
            try:
                qnh = float(payload)
                if 800.0 <= qnh <= 1100.0:
                    state['baro_hpa'] = round(qnh, 2)
                    state['_save_baro'] = True   # persist across reboots
                    print(f'$BARO_ACK,{qnh:.2f},OK')
                else:
                    print(f'$BARO_ACK,ERR range {qnh:.2f}')
            except ValueError as e:
                print(f'$BARO_ACK,ERR {e}')


async def wdt_loop(wdt):
    """Dedicated watchdog-feed task. Runs independently of sensor_loop and
    the HTTP server so a stall in one task can't starve the WDT. Feed
    cadence is 1 s — well below the 8 s timeout window. If the entire
    asyncio scheduler ever stops running (true firmware deadlock), even
    this task stops and the WDT correctly fires."""
    if wdt is None:
        return
    while True:
        try:
            wdt.feed()
        except Exception:
            pass
        await asyncio.sleep_ms(1000)


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

    # Boot-cause logger. machine.reset_cause() returns the reason for THIS
    # boot — i.e. how the previous run ended. Pair it with the persisted
    # last-alive timestamp to bracket how long we ran before dying.
    try:
        cause = machine.reset_cause()
        cause_name = _RESET_NAMES.get(cause, f'UNKNOWN({cause})')
        prev = load_boot_log()
        boot_n = prev['boot_count'] + 1
        print(f'Boot #{boot_n}  reset_cause={cause_name}'
              f'  prev_alive_ms={prev["last_alive_ms"]}')
        save_boot_log({
            'boot_count':    boot_n,
            'last_cause':    cause,
            'last_alive_ms': 0,
        })
        state['boot_count']   = boot_n
        state['reset_cause']  = cause_name
    except Exception as e:
        print(f'Boot logger failed: {e}')

    setup_ap()

    ahrs = WT901(WT901_UART_ID, WT901_TX_PIN, WT901_RX_PIN, WT901_BAUD)
    gps  = GPS(GPS_UART_ID, GPS_TX_PIN, GPS_RX_PIN, GPS_BAUD)
    print(f'WT901  UART{WT901_UART_ID} @ {WT901_BAUD} baud  (GP{WT901_RX_PIN} RX)')
    print(f'NEO-6M UART{GPS_UART_ID}  @ {GPS_BAUD} baud  (GP{GPS_RX_PIN} RX)')

    # Re-assert WT901 Return-Data-Switch in case the chip's config got
    # zeroed out (we lost PKT_ANGLE during bench testing; this restores it).
    if WT901_FORCE_DEFAULT_OUTPUT:
        try:
            ahrs.configure_default_output()
            print('WT901  RSW reconfigured (ACC + GYRO + ANGLE + MAG)')
        except Exception as e:
            print(f'WT901  RSW reconfigure failed: {e}')

    # Sensor front-end: anti-alias bandwidth (+ optional high-rate link).
    # Without this the raw IMU is sampled slowly with a wide-open bandwidth,
    # so cabin/engine vibration aliases into the Mahony solution.
    try:
        if WT901_HIGH_RATE:
            got = ahrs.configure_high_rate(
                target_baud=WT901_TARGET_BAUD,
                target_baud_code=WT901_TARGET_BAUD_CODE,
                rate_code=WT901_HIGHRATE_RATE_CODE,
                bw_code=WT901_HIGHRATE_BANDWIDTH_CODE)
            print(f'WT901  high-rate link @ {got} baud '
                  f'(rate=0x{WT901_HIGHRATE_RATE_CODE:02x}, '
                  f'bw=0x{WT901_HIGHRATE_BANDWIDTH_CODE:02x})')
        else:
            ahrs.set_bandwidth(WT901_LOWRATE_BANDWIDTH_CODE)
            print(f'WT901  anti-alias bandwidth set '
                  f'(0x{WT901_LOWRATE_BANDWIDTH_CODE:02x})')
    except Exception as e:
        print(f'WT901  bandwidth/rate config failed: {e}')

    state.update(load_trims())
    print(f'Trims loaded: pitch={state["pitch_trim"]}° roll={state["roll_trim"]}° yaw={state["yaw_trim"]}°')
    _c, _m, _pa, _ra = load_orient()
    state['orientation'] = _c
    state['mounting']    = _m
    state['pitch_align'] = _pa
    state['roll_align']  = _ra
    print(f'Orientation: connector={_c}  mounting={_m}'
          f'  pitch_align={_pa:+.1f}°  roll_align={_ra:+.1f}°')
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

    # Persisted altimeter setting (QNH) — survives power cycles. The sensor
    # loop syncs baro.qnh_hpa from state['baro_hpa'] every tick, so setting it
    # here is enough; we also seed the BME280 directly for the first samples.
    state['baro_hpa'] = load_baro()
    print(f'QNH loaded: {state["baro_hpa"]} hPa')

    baro = None
    if BME280_ENABLE:
        try:
            from bme280 import BME280
            baro = BME280(
                i2c_id  = BME280_I2C_ID,
                sda     = BME280_SDA_PIN,
                scl     = BME280_SCL_PIN,
                addr    = BME280_I2C_ADDR,
                qnh_hpa = state['baro_hpa'],
            )
            state['baro_src'] = 'bme280'
            print(f'BME280  I2C{BME280_I2C_ID}'
                  f' @ 0x{BME280_I2C_ADDR:02x}'
                  f' (GP{BME280_SDA_PIN} SDA, GP{BME280_SCL_PIN} SCL)'
                  f'  QNH={state["baro_hpa"]} hPa')
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
            kp_acc               = AHRS_KP_ACC,
            ki_acc               = AHRS_KI_ACC,
            kp_mag               = AHRS_KP_MAG,
            accel_gate_g         = AHRS_ACCEL_GATE_G,
            mag_gyro_gate_lo_dps = AHRS_MAG_GYRO_GATE_LO_DPS,
            mag_gyro_gate_hi_dps = AHRS_MAG_GYRO_GATE_HI_DPS,
            bias_clamp_rad_s     = math.radians(AHRS_BIAS_CLAMP_DPS),
        )
        print(f'Mahony AHRS filter enabled  Kp_acc={AHRS_KP_ACC} '
              f'Ki_acc={AHRS_KI_ACC} Kp_mag={AHRS_KP_MAG}'
              f'  mag_gate={AHRS_MAG_GYRO_GATE_LO_DPS}..{AHRS_MAG_GYRO_GATE_HI_DPS}°/s'
              f'  use_mag={AHRS_USE_MAG}  gps_track_aid={AHRS_GPS_TRACK_ENABLE}')
    else:
        print('Mahony filter disabled — using WT901 PKT_ANGLE Euler output')

    # Hardware watchdog — separate task from sensor_loop so HTTP serving
    # latency or GC pauses can't starve the feed. 8 s timeout window.
    try:
        _wdt = WDT(timeout=8000)
        print('Watchdog enabled (8 s timeout, dedicated feed task)')
    except Exception as e:
        print(f'Watchdog init failed ({e}) — running unprotected')
        _wdt = None

    await asyncio.gather(
        sensor_loop(ahrs, gps, baro, sdp, ahrs_filter, sdp_auto_zero),
        start_server(state, port=HTTP_PORT),
        stdin_cmd_loop(),
        wdt_loop(_wdt),
        alive_ticker(),
    )


try:
    asyncio.run(main())
except KeyboardInterrupt:
    print('Stopped by user')
finally:
    led.off()
