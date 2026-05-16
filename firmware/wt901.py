# ---------------------------------------------------------------------------
# wt901.py  –  Driver for the WITMOTION WT901 9-axis AHRS
# ---------------------------------------------------------------------------
# The WT901 streams 11-byte binary packets over UART:
#   Byte  0   : 0x55  (header)
#   Byte  1   : packet type
#   Bytes 2-9 : four signed 16-bit little-endian words (data)
#   Byte 10   : checksum = (sum of bytes 0..9) & 0xFF
#
# Packet types parsed here:
#   0x51 – acceleration   (ax, ay, az, temp)     g
#   0x52 – angular rate   (wx, wy, wz, temp)     deg/s  (raw/32768 * 2000)
#   0x53 – Euler angles   (roll, pitch, yaw)     deg    (legacy / sanity ref)
#   0x54 – magnetometer   (mx, my, mz, temp)     raw    (unitless integer)
#   0x59 – quaternion     (q0, q1, q2, q3)       raw/32768
#
# Raw IMU outputs (accel + gyro + mag) feed the on-Pico Mahony filter in
# firmware/ahrs_filter.py. PKT_ANGLE is retained for diagnostics and as a
# fallback if the filter is disabled.
# ---------------------------------------------------------------------------

import struct
from machine import UART, Pin


class WT901:
    HEADER     = 0x55
    PKT_ACCEL  = 0x51
    PKT_GYRO   = 0x52
    PKT_ANGLE  = 0x53
    PKT_MAG    = 0x54
    PKT_QUAT   = 0x59
    PKT_LEN    = 11

    def __init__(self, uart_id=0, tx=0, rx=1, baud=9600):
        self._uart = UART(uart_id, baudrate=baud, tx=Pin(tx), rx=Pin(rx),
                          rxbuf=256)
        self._buf  = bytearray()

        # Latest WT901 fused Euler (degrees) — kept as a reference/fallback.
        self.roll  = 0.0
        self.pitch = 0.0
        self.yaw   = 0.0

        # Latest accelerations (g, sensor frame)
        self.ax = 0.0
        self.ay = 0.0
        self.az = 0.0

        # Latest angular rates (deg/s, sensor frame)
        self.wx = 0.0
        self.wy = 0.0
        self.wz = 0.0

        # Latest magnetometer (raw int counts, sensor frame). The WT901 does
        # not document a physical unit; for Mahony we only need a direction
        # vector so the raw counts are fine after hard/soft-iron correction.
        self.mx = 0.0
        self.my = 0.0
        self.mz = 0.0

        # Latest quaternion (PKT_QUAT 0x59).
        self.q0 = 1.0
        self.q1 = 0.0
        self.q2 = 0.0
        self.q3 = 0.0

        # Per-packet freshness flags — set when a packet of that type is
        # parsed during update(), cleared by the caller after consumption.
        self.new_accel = False
        self.new_gyro  = False
        self.new_mag   = False
        self.new_angle = False
        self.new_quat  = False

    # ------------------------------------------------------------------
    def update(self):
        """
        Drain the UART RX buffer and parse any complete packets.
        Call this frequently (e.g. every 20 ms) from the main loop.
        Returns True if any packet was successfully parsed in this call.
        """
        available = self._uart.any()
        if available:
            self._buf.extend(self._uart.read(available))

        updated = False

        while len(self._buf) >= self.PKT_LEN:
            # Re-sync: discard bytes until we find the header.
            # NOTE: MicroPython (at least v1.27.0 on Pico W) does NOT
            # implement `del bytearray[i]` / `del bytearray[a:b]` — use
            # slice reassignment instead so we work on all builds.
            if self._buf[0] != self.HEADER:
                self._buf = self._buf[1:]
                continue

            pkt = self._buf[:self.PKT_LEN]

            # Validate checksum
            if self._checksum(pkt) != pkt[10]:
                # Bad packet – drop the header byte and re-sync
                self._buf = self._buf[1:]
                continue

            ptype = pkt[1]

            if ptype == self.PKT_ANGLE:
                roll_raw  = struct.unpack_from('<h', pkt, 2)[0]
                pitch_raw = struct.unpack_from('<h', pkt, 4)[0]
                yaw_raw   = struct.unpack_from('<h', pkt, 6)[0]
                self.roll  = roll_raw  / 32768.0 * 180.0
                self.pitch = pitch_raw / 32768.0 * 180.0
                self.yaw   = yaw_raw   / 32768.0 * 180.0
                if self.yaw < 0:
                    self.yaw += 360.0
                self.new_angle = True
                updated = True

            elif ptype == self.PKT_ACCEL:
                self.ax = struct.unpack_from('<h', pkt, 2)[0] / 32768.0 * 16.0
                self.ay = struct.unpack_from('<h', pkt, 4)[0] / 32768.0 * 16.0
                self.az = struct.unpack_from('<h', pkt, 6)[0] / 32768.0 * 16.0
                self.new_accel = True
                updated = True

            elif ptype == self.PKT_GYRO:
                # raw / 32768 * 2000 deg/s   (WT901 default ±2000°/s range)
                self.wx = struct.unpack_from('<h', pkt, 2)[0] / 32768.0 * 2000.0
                self.wy = struct.unpack_from('<h', pkt, 4)[0] / 32768.0 * 2000.0
                self.wz = struct.unpack_from('<h', pkt, 6)[0] / 32768.0 * 2000.0
                self.new_gyro = True
                updated = True

            elif ptype == self.PKT_MAG:
                # raw int16 counts. Datasheet doesn't specify gauss/LSB; for
                # Mahony correction we only use direction, so leave as int.
                self.mx = float(struct.unpack_from('<h', pkt, 2)[0])
                self.my = float(struct.unpack_from('<h', pkt, 4)[0])
                self.mz = float(struct.unpack_from('<h', pkt, 6)[0])
                self.new_mag = True
                updated = True

            elif ptype == self.PKT_QUAT:
                # raw / 32768 → unit quaternion
                self.q0 = struct.unpack_from('<h', pkt, 2)[0] / 32768.0
                self.q1 = struct.unpack_from('<h', pkt, 4)[0] / 32768.0
                self.q2 = struct.unpack_from('<h', pkt, 6)[0] / 32768.0
                self.q3 = struct.unpack_from('<h', pkt, 8)[0] / 32768.0
                self.new_quat = True
                updated = True

            self._buf = self._buf[self.PKT_LEN:]

        return updated

    # ------------------------------------------------------------------
    @staticmethod
    def _checksum(pkt):
        return sum(pkt[:10]) & 0xFF
