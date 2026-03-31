# ---------------------------------------------------------------------------
# gps.py  –  NMEA parser for the u-blox NEO-6M (GY-NEO6MV2 module)
# ---------------------------------------------------------------------------
# Parses two NMEA sentences:
#   $GPRMC / $GNRMC  – position, ground speed, track, date
#   $GPGGA / $GNGGA  – position, altitude, fix quality, satellite count
#
# Vertical speed is derived by differencing successive altitude readings.
# Altitude is converted from metres (GPGGA) to feet.
# Speed is left in knots (GPRMC native unit).
# ---------------------------------------------------------------------------

import utime
from machine import UART, Pin


class GPS:
    def __init__(self, uart_id=1, tx=4, rx=5, baud=9600):
        self._uart = UART(uart_id, baudrate=baud, tx=Pin(tx), rx=Pin(rx),
                          rxbuf=512)
        self._buf = b''

        self.lat         = 0.0   # decimal degrees, negative = South
        self.lon         = 0.0   # decimal degrees, negative = West
        self.alt_ft      = 0.0   # altitude feet MSL
        self.speed_kt    = 0.0   # groundspeed knots
        self.track_deg   = 0.0   # track / course over ground (degrees true)
        self.fix         = 0     # 0=none, 1=GPS, 2=DGPS
        self.sats        = 0     # satellites in use
        self.vspeed_fpm  = 0.0   # vertical speed ft/min (computed)

        self._prev_alt_ft   = 0.0
        self._prev_time_ms  = utime.ticks_ms()

    # ------------------------------------------------------------------
    def update(self):
        """
        Drain the UART RX buffer and parse any complete NMEA sentences.
        Returns True if at least one sentence was processed.
        """
        available = self._uart.any()
        if available:
            self._buf += self._uart.read(available)

        updated = False

        while b'\n' in self._buf:
            line, self._buf = self._buf.split(b'\n', 1)
            try:
                sentence = line.decode('ascii').strip()
            except Exception:
                continue

            if self._parse_sentence(sentence):
                updated = True

        # Keep buffer from growing unboundedly if no newlines arrive
        if len(self._buf) > 512:
            self._buf = b''

        return updated

    # ------------------------------------------------------------------
    def _parse_sentence(self, sentence):
        """Parse one NMEA sentence string. Returns True if handled."""
        if not sentence.startswith('$'):
            return False
        if '*' not in sentence:
            return False

        star = sentence.rfind('*')
        body = sentence[1:star]          # between $ and *
        cs_str = sentence[star + 1: star + 3]

        # Verify NMEA checksum (XOR of all chars between $ and *)
        try:
            expected_cs = int(cs_str, 16)
        except ValueError:
            return False

        actual_cs = 0
        for ch in body:
            actual_cs ^= ord(ch)
        if actual_cs != expected_cs:
            return False

        parts = body.split(',')
        msg   = parts[0]  # e.g. "GPRMC"

        if msg in ('GPRMC', 'GNRMC'):
            return self._parse_rmc(parts)
        if msg in ('GPGGA', 'GNGGA'):
            return self._parse_gga(parts)
        return False

    # ------------------------------------------------------------------
    def _parse_rmc(self, p):
        """
        $GPRMC,hhmmss.ss,A,llll.ll,N/S,yyyyy.yy,E/W,speed,track,ddmmyy,...
        Status 'A' = active/valid; 'V' = void/invalid
        """
        if len(p) < 9:
            return False
        if p[2] != 'A':          # void fix – don't update position
            return False
        try:
            self.lat       = self._dd(p[3], p[4])
            self.lon       = self._dd(p[5], p[6])
            self.speed_kt  = float(p[7]) if p[7] else 0.0
            self.track_deg = float(p[8]) if p[8] else self.track_deg
        except (ValueError, IndexError):
            return False
        return True

    # ------------------------------------------------------------------
    def _parse_gga(self, p):
        """
        $GPGGA,time,lat,N/S,lon,E/W,fix,sats,hdop,alt,M,geoid,M,...
        fix: 0=invalid, 1=GPS, 2=DGPS
        alt: metres above mean sea level
        """
        if len(p) < 10:
            return False
        try:
            self.fix  = int(p[6])  if p[6]  else 0
            self.sats = int(p[7])  if p[7]  else 0

            new_alt_ft = float(p[9]) * 3.28084 if p[9] else self.alt_ft

            # Compute vertical speed from successive altitude readings
            now_ms = utime.ticks_ms()
            dt_s   = utime.ticks_diff(now_ms, self._prev_time_ms) / 1000.0
            if dt_s >= 0.9 and self.fix > 0:   # only update once GPS gives new alt
                self.vspeed_fpm = (new_alt_ft - self._prev_alt_ft) / dt_s * 60.0
                # Clamp to ±6000 fpm to discard startup spikes
                self.vspeed_fpm = max(-6000.0, min(6000.0, self.vspeed_fpm))
                self._prev_alt_ft  = new_alt_ft
                self._prev_time_ms = now_ms

            self.alt_ft = new_alt_ft
        except (ValueError, IndexError):
            return False
        return True

    # ------------------------------------------------------------------
    @staticmethod
    def _dd(raw, direction):
        """
        Convert NMEA lat/lon string (dddmm.mmmm) + hemisphere to decimal degrees.
        """
        if not raw:
            return 0.0
        dot = raw.index('.')
        deg = int(raw[:dot - 2])
        mn  = float(raw[dot - 2:])
        result = deg + mn / 60.0
        if direction in ('S', 'W'):
            result = -result
        return result
