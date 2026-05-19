"""
serial_client.py – Threaded USB serial client for Pico W AHRS data.

Reads JSON lines from /dev/ttyACM0 (Pico W USB serial) and merges
updates into the shared state dict — same interface as SSEClient.

The Pico W firmware outputs lines prefixed with "$AHRS," followed by
a JSON object.  Other lines (REPL, debug prints) are ignored.

Usage:
    from serial_client import SerialClient
    client = SerialClient("/dev/ttyACM0", state, state_lock)
    client.start()          # starts background thread
    client.connected        # True once first valid line received
    client.rx_count         # count of $AHRS, lines successfully parsed
    client.err_count        # count of errors (parse or IO)
    client.last_err         # string describing the most recent error
    client.stop()           # graceful shutdown
"""

import threading
import time
import json
import os


class SerialClient(threading.Thread):
    PREFIX = "$AHRS,"

    # If we go this long without a valid $AHRS line, assume the USB endpoint
    # has gone silent (Pico re-enumerated, hub glitch, mpremote raced the
    # port) and force a close+reopen. pyserial's readline() will sit in
    # read() forever on a quiet-but-open fd, so we need an upper bound on
    # "looks alive" silence.
    STALE_DATA_TIMEOUT_S = 5.0

    def __init__(self, port: str, state: dict, lock: threading.Lock,
                 baud: int = 115200, reconnect_delay: float = 3.0):
        super().__init__(daemon=True, name="SerialClient")
        self.port            = port
        self.baud            = baud
        self.state           = state
        self.lock            = lock
        self.reconnect_delay = reconnect_delay
        self.connected       = False
        self.paused          = False # when True, skip state.update so sim/demo win
        self.rx_count        = 0     # $AHRS, lines parsed OK
        self.err_count       = 0     # JSON/IO errors
        self.last_err        = ""    # most recent error message
        self.stale_resets    = 0     # count of stale-data forced reconnects
        self._stop_event     = threading.Event()
        self._ser            = None  # set while port is open; used by write()

    def stop(self):
        self._stop_event.set()

    def write(self, data: bytes):
        """Send bytes to the Pico over the same USB serial port. Thread-safe."""
        ser = self._ser
        if ser is not None:
            try:
                ser.write(data)
                ser.flush()   # push bytes out of the OS buffer immediately
            except Exception as e:
                print(f"[Serial] write error: {e}")

    @staticmethod
    def find_port():
        """Return the first available Pico W USB serial device, or None."""
        for candidate in ("/dev/ttyACM0", "/dev/ttyACM1", "/dev/ttyUSB0"):
            if os.path.exists(candidate):
                return candidate
        return None

    def run(self):
        while not self._stop_event.is_set():
            # Re-resolve the port on each reconnect — if the Pico
            # re-enumerated as ttyACM1 we want to follow it rather than
            # block forever on the now-dead ttyACM0.
            resolved = self.find_port() or self.port
            if resolved != self.port:
                print(f"[Serial] Port changed: {self.port} → {resolved}")
                self.port = resolved
            try:
                self._read_loop()
            except Exception as e:
                self.err_count += 1
                self.last_err = f"{type(e).__name__}: {e}"
                print(f"[Serial] Error: {self.last_err}")
            self.connected = False
            if not self._stop_event.is_set():
                print(f"[Serial] Reconnecting in {self.reconnect_delay}s…")
                time.sleep(self.reconnect_delay)

    def _read_loop(self):
        import serial
        print(f"[Serial] Opening {self.port} @ {self.baud}")
        ser = serial.Serial(self.port, self.baud, timeout=2)
        self._ser = ser
        last_rx = time.monotonic()
        try:
            while not self._stop_event.is_set():
                raw = ser.readline()
                # Stale-data watchdog: pyserial's readline() returns b""
                # after the read timeout when no bytes arrive, so a quiet
                # but still-open fd shows up as a tight empty-string loop
                # here. If we go STALE_DATA_TIMEOUT_S without a valid
                # $AHRS line, raise to trigger the outer reconnect path.
                if time.monotonic() - last_rx > self.STALE_DATA_TIMEOUT_S:
                    self.stale_resets += 1
                    raise IOError(
                        f"no $AHRS data for "
                        f"{self.STALE_DATA_TIMEOUT_S:.0f}s — forcing reconnect"
                    )
                if not raw:
                    continue
                line = raw.decode("utf-8", errors="ignore").strip()
                if line.startswith("$ORIENT_ACK,"):
                    print(f"[Serial] {line}")
                if not line.startswith(self.PREFIX):
                    # Non-$AHRS line — Pico boot output, exception traceback,
                    # or other diagnostic print. Relay to journalctl so we
                    # can see WHY the Pico reset / what it logged at boot.
                    # Skip pure-empty lines and the visual divider banners.
                    if line and not line.startswith("─"):
                        print(f"[Pico] {line}")
                    continue
                payload = line[len(self.PREFIX):]
                try:
                    update = json.loads(payload)
                    # Keep reading (drain the buffer) but don't merge into
                    # state while paused — sim/demo owns the state dict
                    # until unpaused.
                    if not self.paused:
                        with self.lock:
                            self.state.update(update)
                    self.connected = True
                    self.rx_count += 1
                    last_rx = time.monotonic()
                except json.JSONDecodeError as e:
                    self.err_count += 1
                    self.last_err = f"JSON: {e.msg} @ col {e.colno}"
        finally:
            self._ser = None
            ser.close()
