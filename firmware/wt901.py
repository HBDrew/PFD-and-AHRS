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
import utime
from machine import UART, Pin


class WT901:
    HEADER     = 0x55
    PKT_ACCEL  = 0x51
    PKT_GYRO   = 0x52
    PKT_ANGLE  = 0x53
    PKT_MAG    = 0x54
    PKT_QUAT   = 0x59
    PKT_LEN    = 11

    # ── WitMotion WIT-standard config protocol ─────────────────────────────
    # Every config write is  0xFF 0xAA <reg> <lo> <hi> .  Newer WT901 firmware
    # requires an unlock write before any register write is accepted, and a
    # save write to persist changes to the chip's internal flash.
    _UNLOCK        = b'\xff\xaa\x69\x88\xb5'
    _REG_SAVE      = 0x00   # write 0x0000 → persist config to flash
    _REG_RSW       = 0x02   # return-data switch (which packet types stream)
    _REG_RRATE     = 0x03   # output / return rate
    _REG_BAUD      = 0x04   # UART baud rate
    _REG_BANDWIDTH = 0x1F   # sensor digital low-pass (anti-alias) bandwidth

    # Return-rate (0x03) codes
    RRATE_10HZ  = 0x06
    RRATE_20HZ  = 0x07
    RRATE_50HZ  = 0x08
    RRATE_100HZ = 0x09
    RRATE_200HZ = 0x0B
    # Baud (0x04) codes
    BAUD_9600   = 0x02
    BAUD_115200 = 0x06
    # Bandwidth (0x1F) codes — the DLPF cutoff.  MUST sit below half the output
    # rate or vibration above Nyquist folds down (aliases) into the attitude
    # band, where the Mahony filter can't tell it from real motion.
    BW_256HZ = 0x00
    BW_98HZ  = 0x02
    BW_42HZ  = 0x03
    BW_20HZ  = 0x04
    BW_10HZ  = 0x05
    BW_5HZ   = 0x06

    def __init__(self, uart_id=0, tx=0, rx=1, baud=9600):
        self._uart_id = uart_id
        self._tx      = tx
        self._rx      = rx
        self._baud    = baud
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

        # Cumulative parse counters — useful for diagnosing missing packet
        # types (e.g., PKT_ANGLE silently disabled by a chip config glitch).
        self.cnt_accel = 0
        self.cnt_gyro  = 0
        self.cnt_mag   = 0
        self.cnt_angle = 0
        self.cnt_quat  = 0
        self.cnt_bad_cksum = 0

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
                self.cnt_bad_cksum += 1
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
                self.cnt_angle += 1
                updated = True

            elif ptype == self.PKT_ACCEL:
                self.ax = struct.unpack_from('<h', pkt, 2)[0] / 32768.0 * 16.0
                self.ay = struct.unpack_from('<h', pkt, 4)[0] / 32768.0 * 16.0
                self.az = struct.unpack_from('<h', pkt, 6)[0] / 32768.0 * 16.0
                self.new_accel = True
                self.cnt_accel += 1
                updated = True

            elif ptype == self.PKT_GYRO:
                # raw / 32768 * 2000 deg/s   (WT901 default ±2000°/s range)
                self.wx = struct.unpack_from('<h', pkt, 2)[0] / 32768.0 * 2000.0
                self.wy = struct.unpack_from('<h', pkt, 4)[0] / 32768.0 * 2000.0
                self.wz = struct.unpack_from('<h', pkt, 6)[0] / 32768.0 * 2000.0
                self.new_gyro = True
                self.cnt_gyro += 1
                updated = True

            elif ptype == self.PKT_MAG:
                # raw int16 counts. Datasheet doesn't specify gauss/LSB; for
                # Mahony correction we only use direction, so leave as int.
                self.mx = float(struct.unpack_from('<h', pkt, 2)[0])
                self.my = float(struct.unpack_from('<h', pkt, 4)[0])
                self.mz = float(struct.unpack_from('<h', pkt, 6)[0])
                self.new_mag = True
                self.cnt_mag += 1
                updated = True

            elif ptype == self.PKT_QUAT:
                # raw / 32768 → unit quaternion
                self.q0 = struct.unpack_from('<h', pkt, 2)[0] / 32768.0
                self.q1 = struct.unpack_from('<h', pkt, 4)[0] / 32768.0
                self.q2 = struct.unpack_from('<h', pkt, 6)[0] / 32768.0
                self.q3 = struct.unpack_from('<h', pkt, 8)[0] / 32768.0
                self.new_quat = True
                self.cnt_quat += 1
                updated = True

            self._buf = self._buf[self.PKT_LEN:]

        return updated

    # ------------------------------------------------------------------
    @staticmethod
    def _checksum(pkt):
        return sum(pkt[:10]) & 0xFF

    # ------------------------------------------------------------------
    def configure_default_output(self):
        """Send WitMotion config sequence to ensure ACC + GYRO + ANGLE + MAG
        packets are streaming, then persist to the WT901's internal flash.
        Used at firmware boot to recover from a chip whose Return-data-Switch
        (RSW) register has somehow been zeroed for one or more packet types
        — observed in bench testing where PKT_ANGLE (0x53) stopped arriving
        after the chip had been on a workbench through many reboots.

        Protocol: each WT901 config write is a 5-byte packet
            0xFF 0xAA <reg> <data_lo> <data_hi>
        and writes during normal streaming are filtered out by the chip's
        own RX-line parser, so we can send these while data packets are
        flowing the other direction.

        RSW bit map (register 0x02):
            bit 1 = ACC   (0x51)
            bit 2 = GYRO  (0x52)
            bit 3 = ANGLE (0x53)   ← the one we keep losing
            bit 4 = MAG   (0x54)
        0x001E enables all four (the WitMotion factory default).
        """
        # Unlock register set — required by newer WT901 firmware before
        # any config write will be accepted.
        self._uart.write(bytes([0xFF, 0xAA, 0x69, 0x88, 0xB5]))
        utime.sleep_ms(100)
        # RSW = 0x001E → ACC + GYRO + ANGLE + MAG streaming.
        self._uart.write(bytes([0xFF, 0xAA, 0x02, 0x1E, 0x00]))
        utime.sleep_ms(100)
        # Save to internal flash so the setting survives the next power
        # cycle without having to re-send.
        self._uart.write(bytes([0xFF, 0xAA, 0x00, 0x00, 0x00]))
        utime.sleep_ms(200)

    # ------------------------------------------------------------------
    def _write_reg(self, reg, lo, hi=0x00):
        """Unlock, then write one WitMotion config register (not persisted)."""
        self._uart.write(self._UNLOCK)
        utime.sleep_ms(50)
        self._uart.write(bytes([0xFF, 0xAA, reg, lo & 0xFF, hi & 0xFF]))
        utime.sleep_ms(50)

    def _save_config(self):
        """Persist the current register set to the chip's internal flash."""
        self._uart.write(bytes([0xFF, 0xAA, self._REG_SAVE, 0x00, 0x00]))
        utime.sleep_ms(200)

    def set_bandwidth(self, bw_code, save=True):
        """Set the sensor DLPF (anti-alias) bandwidth register (0x1F).

        The WT901 ships at 20 Hz bandwidth.  When the raw IMU is sampled slowly
        (e.g. ~10–20 Hz over a 9600-baud link) that default is *wider* than
        Nyquist, so engine/prop vibration aliases straight into the attitude
        solution.  Dropping the bandwidth to match the sample rate filters that
        vibration out in the sensor's own front end — before it can alias."""
        self._write_reg(self._REG_BANDWIDTH, bw_code)
        if save:
            self._save_config()

    def set_output_rate(self, rate_code, save=True):
        """Set the return-rate register (0x03)."""
        self._write_reg(self._REG_RRATE, rate_code)
        if save:
            self._save_config()

    def _reopen_uart(self, baud):
        """Re-initialise the Pico-side UART at a new baud and drop any partial
        packet in the buffer (the old baud's bytes are meaningless now)."""
        try:
            self._uart.deinit()
        except Exception:
            pass
        self._uart = UART(self._uart_id, baudrate=baud, tx=Pin(self._tx),
                          rx=Pin(self._rx), rxbuf=256)
        self._baud = baud
        self._buf  = bytearray()

    def _wait_for_valid(self, timeout_ms=800):
        """Return True once a checksum-valid packet is parsed within the window.
        Used to confirm the chip is actually talking at the current UART baud."""
        start  = utime.ticks_ms()
        before = (self.cnt_accel + self.cnt_gyro + self.cnt_angle
                  + self.cnt_mag + self.cnt_quat)
        while utime.ticks_diff(utime.ticks_ms(), start) < timeout_ms:
            self.update()
            after = (self.cnt_accel + self.cnt_gyro + self.cnt_angle
                     + self.cnt_mag + self.cnt_quat)
            if after > before:
                return True
            utime.sleep_ms(20)
        return False

    def configure_high_rate(self, target_baud=115200,
                            target_baud_code=None,
                            rate_code=None, bw_code=None):
        """Raise the WT901 to a high output rate + matching baud so the on-Pico
        Mahony filter gets properly oversampled raw IMU data (REQ-AHRS-SF-003).

        Self-healing and fail-safe — the AHRS is the only attitude source, so
        this must never leave it mute:
          1. Probe the target baud first, then 9600, to find where the chip is
             actually talking (handles a config already half-applied by a prior
             boot).
          2. Apply bandwidth + output rate at that working baud.
          3. Switch the chip's baud, persist, reopen the UART, and verify real
             packets arrive.  If they don't, revert to a known-good 9600 /
             low-rate config so streaming continues; the saved baud is still
             recoverable next boot because step 1 probes the target first.

        Returns the baud the link ended up running at."""
        if target_baud_code is None:
            target_baud_code = self.BAUD_115200
        if rate_code is None:
            rate_code = self.RRATE_100HZ
        if bw_code is None:
            bw_code = self.BW_20HZ

        # 1. Find a baud where valid packets already arrive.
        working = None
        for b in (target_baud, 9600):
            self._reopen_uart(b)
            if self._wait_for_valid(600):
                working = b
                break
        if working is None:
            # Chip silent at both rates — leave the UART at 9600 and bail; the
            # caller's RSW/bandwidth best-effort still runs.
            self._reopen_uart(9600)
            return 9600

        if working == target_baud:
            # Already at the target baud (config persisted on an earlier boot) —
            # just (re)assert rate + bandwidth and persist.
            self.set_bandwidth(bw_code, save=False)
            self.set_output_rate(rate_code, save=False)
            self._save_config()
            return target_baud

        # 2. We're talking at 9600.  Apply bandwidth + rate here first.
        self.set_bandwidth(bw_code, save=False)
        self.set_output_rate(rate_code, save=False)

        # 3. Switch the chip baud, persist, reopen, and verify.
        self._write_reg(self._REG_BAUD, target_baud_code)
        self._save_config()
        self._reopen_uart(target_baud)
        if self._wait_for_valid(1000):
            return target_baud

        # Switch didn't take — the chip is almost certainly still at 9600 (a
        # successful switch would have passed the check above), so it can still
        # hear us at 9600.  Put it back to a safe low-rate config and persist.
        self._reopen_uart(9600)
        self._write_reg(self._REG_BAUD, self.BAUD_9600)
        # 10 Hz keeps the 9600 link at ~46% utilisation (four 11-byte packets)
        # so it can't drop packets; 5 Hz bandwidth is the matched anti-alias.
        self.set_output_rate(self.RRATE_10HZ, save=False)
        self.set_bandwidth(self.BW_5HZ, save=False)
        self._save_config()
        return 9600
