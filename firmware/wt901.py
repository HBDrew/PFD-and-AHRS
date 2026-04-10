# ---------------------------------------------------------------------------
# wt901.py  –  Driver for the WITMOTION WT901 9-axis AHRS
# ---------------------------------------------------------------------------
# The WT901 streams 11-byte binary packets over UART:
#   Byte  0   : 0x55  (header)
#   Byte  1   : packet type
#   Bytes 2-9 : four signed 16-bit little-endian words (data)
#   Byte 10   : checksum = (sum of bytes 0..9) & 0xFF
#
# Packet types used here:
#   0x51 – acceleration  (ax, ay, az, temp)
#   0x52 – angular rate  (wx, wy, wz, temp)
#   0x53 – Euler angles  (roll, pitch, yaw, temp)
#           value → degrees : raw_int16 / 32768.0 * 180.0
# ---------------------------------------------------------------------------

import struct
from machine import UART, Pin


class WT901:
    HEADER     = 0x55
    PKT_ACCEL  = 0x51
    PKT_GYRO   = 0x52
    PKT_ANGLE  = 0x53
    PKT_LEN    = 11

    def __init__(self, uart_id=0, tx=0, rx=1, baud=9600):
        self._uart = UART(uart_id, baudrate=baud, tx=Pin(tx), rx=Pin(rx),
                          rxbuf=256)
        self._buf  = bytearray()

        # Latest values (degrees)
        self.roll  = 0.0
        self.pitch = 0.0
        self.yaw   = 0.0

        # Latest accelerations (g)
        self.ax = 0.0
        self.ay = 0.0
        self.az = 0.0

    # ------------------------------------------------------------------
    def update(self):
        """
        Drain the UART RX buffer and parse any complete packets.
        Call this frequently (e.g. every 20 ms) from the main loop.
        Returns True if Euler angles were updated in this call.
        """
        available = self._uart.any()
        if available:
            self._buf.extend(self._uart.read(available))

        updated = False

        while len(self._buf) >= self.PKT_LEN:
            # Re-sync: discard bytes until we find the header
            if self._buf[0] != self.HEADER:
                del self._buf[0]
                continue

            pkt = self._buf[:self.PKT_LEN]

            # Validate checksum
            if self._checksum(pkt) != pkt[10]:
                # Bad packet – drop the header byte and re-sync
                del self._buf[0]
                continue

            ptype = pkt[1]

            if ptype == self.PKT_ANGLE:
                roll_raw  = struct.unpack_from('<h', pkt, 2)[0]
                pitch_raw = struct.unpack_from('<h', pkt, 4)[0]
                yaw_raw   = struct.unpack_from('<h', pkt, 6)[0]
                self.roll  = roll_raw  / 32768.0 * 180.0
                self.pitch = pitch_raw / 32768.0 * 180.0
                self.yaw   = yaw_raw   / 32768.0 * 180.0
                # Normalise yaw to [0, 360)
                if self.yaw < 0:
                    self.yaw += 360.0
                updated = True

            elif ptype == self.PKT_ACCEL:
                self.ax = struct.unpack_from('<h', pkt, 2)[0] / 32768.0 * 16.0
                self.ay = struct.unpack_from('<h', pkt, 4)[0] / 32768.0 * 16.0
                self.az = struct.unpack_from('<h', pkt, 6)[0] / 32768.0 * 16.0

            del self._buf[:self.PKT_LEN]

        return updated

    # ------------------------------------------------------------------
    @staticmethod
    def _checksum(pkt):
        return sum(pkt[:10]) & 0xFF
