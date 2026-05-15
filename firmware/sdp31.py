# ---------------------------------------------------------------------------
# sdp31.py  –  Sensirion SDP31-500Pa differential-pressure driver
# ---------------------------------------------------------------------------
# Datasheet: Sensirion SDP3x_Sensors_Datasheet (rev. 1.0)
#
# The SDP31-500Pa measures bidirectional differential pressure across a
# pitot/static pair (P_pitot − P_static).  Together with the BME280 it
# completes the air-data computer:
#   IAS_kt = sqrt(2·dp / ρ₀) · (m/s → kt)
#   TAS_kt = IAS_kt · sqrt(ρ₀ / ρ)      where ρ from BME280 P & T
#
# Wiring (I2C1, shared with the BME280):
#   VDD → 3V3(OUT)  GND → GND
#   SDA → GP2  (pin 4)    SCL → GP3  (pin 5)
#   ADDR pin floating → 0x21  (default; 0x22 if ADDR tied to VDD)
#
# I²C protocol (16-bit command + 9-byte read frame):
#   Start continuous w/ averaging: 0x36 0x03
#   Start continuous w/o averaging: 0x36 0x15
#   Stop continuous:                0x3F 0xF9
#   Trigger one-shot:               0x36 0x2D
#   Read 9 bytes: dp[2] + crc + temp[2] + crc + scale[2] + crc
#
# Each 2-byte word is followed by a CRC-8 (poly 0x31, init 0xFF).
# Pressure_Pa = dp_raw / scale     (scale is a uint16 read from the sensor;
#                                   typically 60 for SDP31-500Pa)
# Temp_C      = t_raw  / 200       (signed int16, °C per LSB = 1/200)
#
# Public attributes:
#   sdp.dp_pa        – differential pressure in Pa (signed; + for pitot > static)
#   sdp.temperature_c
#   sdp.scale        – scale factor read once at startup
#   sdp.last_update_ms
#
# Public methods:
#   sdp.update()     – read one 9-byte frame; updates dp_pa and temperature_c
#   sdp.zero()       – capture current dp as the in-flight zero offset
# ---------------------------------------------------------------------------

import utime
from machine import I2C, Pin


# CRC-8 lookup is unnecessary for 2-byte words; the inline loop runs in
# ~30 µs on the RP2040 and keeps the module RAM-light.
def _crc8(data: bytes) -> int:
    crc = 0xFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ 0x31) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc


class SDP31:

    _ADDR_DEFAULT = 0x21   # ADDR floating; 0x22 when ADDR tied to VDD

    _CMD_START_CONT_AVG = b'\x36\x03'   # continuous, mass-flow averaging on
    _CMD_STOP_CONT      = b'\x3F\xF9'
    _CMD_RESET          = b'\x00\x06'   # general-call soft reset

    def __init__(self, i2c_id=1, sda=2, scl=3, addr=None):
        # The SDP31 shares I2C1 with the BME280.  Re-instantiating I2C on
        # the same pins is safe — MicroPython's I2C is just a handle to the
        # underlying peripheral state.
        self._i2c  = I2C(i2c_id, sda=Pin(sda), scl=Pin(scl), freq=400_000)
        self._addr = addr if addr is not None else self._ADDR_DEFAULT

        self.dp_pa          = 0.0   # signed; pitot − static
        self.temperature_c  = 0.0
        self.scale          = 60    # SDP31-500Pa nominal; overwritten on first read
        self.last_update_ms = 0
        self._dp_zero       = 0.0   # captured by zero(); subtracted on update()

        # Soft reset → stop any prior continuous mode → start fresh.
        # An I/O error here propagates; main.py catches it and skips
        # initialising the air-data path.
        try:
            self._i2c.writeto(0x00, b'\x06')        # general-call reset
        except OSError:
            pass                                    # reset is best-effort
        utime.sleep_ms(20)
        try:
            self._i2c.writeto(self._addr, self._CMD_STOP_CONT)
            utime.sleep_ms(2)
        except OSError:
            pass
        self._i2c.writeto(self._addr, self._CMD_START_CONT_AVG)
        # First valid measurement ready ~8 ms after START_CONT.
        utime.sleep_ms(20)

    # ── Public ────────────────────────────────────────────────────────────────

    def update(self):
        """
        Read one 9-byte measurement frame.  Returns True on success.
        Updates self.dp_pa (zero-offset applied) and self.temperature_c.
        Silently returns False on CRC mismatch or I²C error so the caller
        can keep the last-good value rather than glitching to 0.
        """
        try:
            buf = self._i2c.readfrom(self._addr, 9)
        except OSError:
            return False
        if len(buf) != 9:
            return False

        # Validate each 2-byte word against its CRC.  A CRC failure
        # generally means we caught a measurement mid-conversion — back
        # off and let the caller try again next tick.
        if (_crc8(buf[0:2]) != buf[2]
                or _crc8(buf[3:5]) != buf[5]
                or _crc8(buf[6:8]) != buf[8]):
            return False

        # Pressure: signed int16 / scale (Pa)
        dp_raw = (buf[0] << 8) | buf[1]
        if dp_raw & 0x8000:
            dp_raw -= 0x10000
        # Temperature: signed int16 / 200 (°C)
        t_raw  = (buf[3] << 8) | buf[4]
        if t_raw & 0x8000:
            t_raw -= 0x10000
        # Scale factor: uint16
        scale  = (buf[6] << 8) | buf[7]
        if scale > 0:
            self.scale = scale

        self.dp_pa         = dp_raw / float(self.scale) - self._dp_zero
        self.temperature_c = t_raw / 200.0
        self.last_update_ms = utime.ticks_ms()
        return True

    def zero(self):
        """Capture the current measurement as the in-flight zero offset.
        Use when the pitot/static lines are confirmed equalised (sensor on
        the bench, or aircraft stationary with the pitot covered)."""
        if self.update():
            self._dp_zero += self.dp_pa
            self.dp_pa = 0.0
