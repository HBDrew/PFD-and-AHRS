# ---------------------------------------------------------------------------
# ms4525.py  –  TE Connectivity MS4525DO differential-pressure driver
# ---------------------------------------------------------------------------
# Datasheet: MS4525DO_B4 (TE rev. 4).  Continuous-conversion I²C variant —
# the part starts converting on power-up and the host just reads 4 bytes
# at the configured address.  No command sequence required.
#
# Frame format (4 bytes, MSB first):
#   Byte 0: bit7-6 = status, bit5-0 = pressure[13:8]
#   Byte 1: pressure[7:0]
#   Byte 2: temperature[10:3]
#   Byte 3: bit7-5 = temperature[2:0], bit4-0 = unused
#
# Status codes:
#   0 = Normal       — fresh measurement, use the data
#   1 = Command mode — should not appear on a DO part in continuous read
#   2 = Stale        — same data as previous read (conversion not yet complete);
#                      still usable, just no new info
#   3 = Diagnostic fault — discard frame
#
# Pressure decoding (14-bit unsigned):
#   The output spans 10%-90% of the 14-bit count range (0–16383):
#     counts at -full-scale = 0.1 * 16384 ≈ 1638
#     counts at  0 psi      = 0.5 * 16384 ≈ 8192
#     counts at +full-scale = 0.9 * 16384 ≈ 14746
#   For a ±1 psi variant (-DS5AI001DP, the typical pitot part):
#     dp_pa = (counts - 8192) * (2 * 1 psi * 6894.757 Pa/psi) / 13108
#           = (counts - 8192) * 1.0521
#   The psi_range constructor arg generalises to other variants (±2 psi,
#   ±5 psi, etc.) — supply the full-scale rating from the part number.
#
# Temperature decoding (11-bit unsigned, °C):
#   t_c = (raw / 2047) * 200 - 50    # -50 °C to +150 °C linear span
#
# Wiring (I²C1, shared with the BME280):
#   VDD → 3V3(OUT)  GND → GND
#   SDA → GP2  (pin 4)    SCL → GP3  (pin 5)
#   Default I²C address: 0x28 (A-cal); B-cal variant is 0x36.
#
# Public interface mirrors firmware/sdp31.py so main.py can substitute one
# for the other transparently — both expose dp_pa, temperature_c,
# last_update_ms, update(), and zero().
# ---------------------------------------------------------------------------

import utime
from machine import I2C, Pin


class MS4525:

    _ADDR_DEFAULT = 0x28        # A-cal variant; B-cal = 0x36
    _PSI_TO_PA    = 6894.757
    _COUNTS_ZERO  = 8192        # 50 % of 14-bit range — output at 0 Δp
    _COUNTS_SPAN  = 13108       # 80 % of 14-bit range — counts per 2·full-scale

    def __init__(self, i2c_id=1, sda=2, scl=3, addr=None, psi_range=1.0):
        # Shares I²C1 with BME280; re-instantiating the bus on the same pins
        # is a no-op — MicroPython's I2C is just a handle to the peripheral.
        self._i2c  = I2C(i2c_id, sda=Pin(sda), scl=Pin(scl), freq=400_000)
        self._addr = addr if addr is not None else self._ADDR_DEFAULT

        # Pa per count: covers all -001D / -002D / -005D variants by supplying
        # the appropriate psi_range.  For ±1 psi: ≈ 1.052 Pa/count.
        self._pa_per_count = (2.0 * psi_range * self._PSI_TO_PA) / self._COUNTS_SPAN
        self._psi_range    = psi_range

        self.dp_pa          = 0.0
        self.temperature_c  = 0.0
        self.last_update_ms = 0
        self._dp_zero       = 0.0   # captured by zero(); subtracted on update()

        # Probe: the MS4525DO begins streaming as soon as power and clock are
        # present.  First valid frame is ready within a few ms.  An OSError
        # here propagates so main.py can skip the air-data path cleanly.
        utime.sleep_ms(10)
        self._i2c.readfrom(self._addr, 4)   # discard the first frame

    # ── Public ────────────────────────────────────────────────────────────────

    def update(self):
        """
        Read one 4-byte frame.  Returns True on success.  Silently returns
        False on I²C error, status==fault, or unexpected command-mode reply,
        so the caller can keep the last-good value rather than glitching.
        Status==stale (2) is accepted — the value is just unchanged from the
        previous read, not invalid.
        """
        try:
            buf = self._i2c.readfrom(self._addr, 4)
        except OSError:
            return False
        if len(buf) != 4:
            return False

        status = (buf[0] >> 6) & 0x03
        if status == 1 or status == 3:
            return False    # command-mode reply or fault — skip

        p_raw = ((buf[0] & 0x3F) << 8) | buf[1]
        t_raw = (buf[2] << 3) | (buf[3] >> 5)

        self.dp_pa         = ((p_raw - self._COUNTS_ZERO)
                              * self._pa_per_count
                              - self._dp_zero)
        self.temperature_c = (t_raw / 2047.0) * 200.0 - 50.0
        self.last_update_ms = utime.ticks_ms()
        return True

    def zero(self):
        """Capture the current measurement as the in-flight zero offset.
        Use when the pitot/static lines are confirmed equalised (sensor on
        the bench, or aircraft stationary with the pitot covered).  Mirrors
        the SDP31 method so the same calling code works for both."""
        if self.update():
            self._dp_zero += self.dp_pa
            self.dp_pa = 0.0
