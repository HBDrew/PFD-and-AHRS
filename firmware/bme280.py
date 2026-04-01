# ---------------------------------------------------------------------------
# bme280.py  –  BME280 barometric pressure / temperature driver
# ---------------------------------------------------------------------------
# Datasheet: Bosch BST-BME280-DS002
#
# Wiring (I2C1):
#   VCC → 3V3(OUT)   GND → GND
#   SDA → GP2        SCL → GP3
#   SDO → GND  (I2C address 0x76; pull SDO high for 0x77)
#
# Public attributes:
#   bme.temperature_c   – degrees Celsius (float)
#   bme.pressure_pa     – Pascals (float)
#   bme.vspeed_fpm      – vertical speed ft/min, EMA-smoothed (float)
#   bme.qnh_hpa         – current QNH setting (writeable, float)
#
# Public methods:
#   bme.update()                    – read sensor, update all attributes
#   bme.altitude_ft()               – feet MSL corrected for qnh_hpa
#   bme.calibrate_to_alt_ft(ft)     – back-calculate qnh_hpa from known elevation
# ---------------------------------------------------------------------------

import struct
import utime
from machine import I2C, Pin


class BME280:

    _CHIP_ID       = 0x60
    _ADDR_DEFAULT  = 0x76   # SDO=GND; use 0x77 if SDO=VCC

    # Register addresses
    _REG_ID        = 0xD0
    _REG_CTRL_HUM  = 0xF2   # set to 0x00 – we don't use humidity
    _REG_CTRL_MEAS = 0xF4   # osrs_t + osrs_p + mode
    _REG_CONFIG    = 0xF5   # t_sb + filter
    _REG_DATA      = 0xF7   # 6 bytes: P[19:0] then T[19:0]
    _REG_CAL1      = 0x88   # 24 bytes T1–T3, P1–P9

    # osrs_t=101(×16), osrs_p=101(×16), mode=11(normal continuous)
    _CTRL_MEAS_VAL = 0b10110111
    # t_sb=000(0.5 ms standby), filter=100(coeff 16), spi3w_en=0
    _CONFIG_VAL    = 0b00010000

    def __init__(self, i2c_id=1, sda=2, scl=3, addr=None, qnh_hpa=1013.25):
        self._i2c  = I2C(i2c_id, sda=Pin(sda), scl=Pin(scl), freq=400_000)
        self._addr = addr if addr is not None else self._ADDR_DEFAULT

        self.qnh_hpa       = qnh_hpa
        self.temperature_c = 0.0
        self.pressure_pa   = 0.0
        self.vspeed_fpm    = 0.0

        # EMA VSI state
        self._last_alt_ft  = None
        self._last_ts_ms   = None
        self._t_fine       = 0

        chip_id = self._i2c.readfrom_mem(self._addr, self._REG_ID, 1)[0]
        if chip_id != self._CHIP_ID:
            raise RuntimeError(
                'BME280 not found (chip_id=0x{:02x}, expected 0x{:02x})'.format(
                    chip_id, self._CHIP_ID))

        self._read_calibration()
        self._i2c.writeto_mem(self._addr, self._REG_CTRL_HUM,  bytes([0x00]))
        self._i2c.writeto_mem(self._addr, self._REG_CONFIG,    bytes([self._CONFIG_VAL]))
        self._i2c.writeto_mem(self._addr, self._REG_CTRL_MEAS, bytes([self._CTRL_MEAS_VAL]))

    # ── Public ────────────────────────────────────────────────────────────────

    def update(self):
        """Read raw data, apply compensation, update temperature_c, pressure_pa,
        and vspeed_fpm.  Call at 10–50 Hz from the main sensor loop."""
        raw   = self._i2c.readfrom_mem(self._addr, self._REG_DATA, 6)
        adc_P = (raw[0] << 12) | (raw[1] << 4) | (raw[2] >> 4)
        adc_T = (raw[3] << 12) | (raw[4] << 4) | (raw[5] >> 4)

        self.temperature_c = self._compensate_t(adc_P=0, adc_T=adc_T)
        self.pressure_pa   = self._compensate_p(adc_P)

        # Vertical speed: exponential moving average of altitude derivative
        # τ = 2 s; α = dt/(τ+dt) adapts to actual sample interval
        alt = self.altitude_ft()
        now = utime.ticks_ms()
        if self._last_alt_ft is not None and self._last_ts_ms is not None:
            dt_s = utime.ticks_diff(now, self._last_ts_ms) / 1000.0
            if dt_s > 0.005:  # guard against spurious zero dt
                raw_vsi = (alt - self._last_alt_ft) / dt_s * 60.0  # ft/min
                alpha   = dt_s / (2.0 + dt_s)
                self.vspeed_fpm = self.vspeed_fpm * (1.0 - alpha) + raw_vsi * alpha
                self.vspeed_fpm = max(-6000.0, min(6000.0, self.vspeed_fpm))
        self._last_alt_ft = alt
        self._last_ts_ms  = now

    def altitude_ft(self):
        """Pressure altitude in feet MSL corrected for self.qnh_hpa.
        Uses the ICAO hypsometric formula.
        GPS GGA orthometric (MSL) altitude is the appropriate reference for
        calibrate_to_alt_ft() — do not use GPS height-above-ellipsoid."""
        ratio = self.pressure_pa / (self.qnh_hpa * 100.0)
        alt_m = 44330.0 * (1.0 - ratio ** (1.0 / 5.255))
        return alt_m * 3.28084

    def calibrate_to_alt_ft(self, known_alt_ft):
        """Back-calculate QNH from the current pressure and a known MSL elevation.
        Use GPS altitude (fix required) or a published aerodrome elevation.
        Updates self.qnh_hpa in-place."""
        known_alt_m = known_alt_ft / 3.28084
        factor = (1.0 - known_alt_m / 44330.0) ** 5.255
        if factor > 0:
            self.qnh_hpa = round(self.pressure_pa / factor / 100.0, 2)

    # ── Bosch compensation formulas (float) ───────────────────────────────────

    def _compensate_t(self, adc_P, adc_T):
        """Compute _t_fine (shared with pressure compensation) and return °C."""
        var1 = (adc_T / 16384.0 - self._T1 / 1024.0) * self._T2
        var2 = (adc_T / 131072.0 - self._T1 / 8192.0) ** 2 * self._T3
        self._t_fine = var1 + var2
        return self._t_fine / 5120.0

    def _compensate_p(self, adc_P):
        """Return compensated pressure in Pa.  Requires _t_fine to be current."""
        var1 = self._t_fine / 2.0 - 64000.0
        var2 = var1 * var1 * self._P6 / 32768.0
        var2 = var2 + var1 * self._P5 * 2.0
        var2 = var2 / 4.0 + self._P4 * 65536.0
        var1 = (self._P3 * var1 * var1 / 524288.0 + self._P2 * var1) / 524288.0
        var1 = (1.0 + var1 / 32768.0) * self._P1
        if var1 == 0.0:
            return 0.0
        p    = 1048576.0 - adc_P
        p    = (p - var2 / 4096.0) * 6250.0 / var1
        var1 = self._P9 * p * p / 2147483648.0
        var2 = p * self._P8 / 32768.0
        return p + (var1 + var2 + self._P7) / 16.0

    def _read_calibration(self):
        """Read 24-byte factory calibration block from 0x88."""
        cal = self._i2c.readfrom_mem(self._addr, self._REG_CAL1, 24)
        # T1 unsigned, T2/T3 signed; P1 unsigned, P2-P9 signed
        (self._T1, self._T2, self._T3,
         self._P1, self._P2, self._P3, self._P4,
         self._P5, self._P6, self._P7, self._P8, self._P9
         ) = struct.unpack('<HhhHhhhhhhhh', cal)
