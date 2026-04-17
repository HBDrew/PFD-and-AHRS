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
    client.stop()           # graceful shutdown
"""

import threading
import time
import json
import os


class SerialClient(threading.Thread):
    PREFIX = "$AHRS,"

    def __init__(self, port: str, state: dict, lock: threading.Lock,
                 baud: int = 115200, reconnect_delay: float = 3.0):
        super().__init__(daemon=True, name="SerialClient")
        self.port            = port
        self.baud            = baud
        self.state           = state
        self.lock            = lock
        self.reconnect_delay = reconnect_delay
        self.connected       = False
        self._stop_event     = threading.Event()

    def stop(self):
        self._stop_event.set()

    @staticmethod
    def find_port():
        """Return the first available Pico W USB serial device, or None."""
        for candidate in ("/dev/ttyACM0", "/dev/ttyACM1", "/dev/ttyUSB0"):
            if os.path.exists(candidate):
                return candidate
        return None

    def run(self):
        while not self._stop_event.is_set():
            try:
                self._read_loop()
            except Exception as e:
                print(f"[Serial] Error: {e}")
            self.connected = False
            if not self._stop_event.is_set():
                print(f"[Serial] Reconnecting in {self.reconnect_delay}s…")
                time.sleep(self.reconnect_delay)

    def _read_loop(self):
        import serial
        print(f"[Serial] Opening {self.port} @ {self.baud}")
        ser = serial.Serial(self.port, self.baud, timeout=2)
        try:
            while not self._stop_event.is_set():
                raw = ser.readline()
                if not raw:
                    continue
                line = raw.decode("utf-8", errors="ignore").strip()
                if not line.startswith(self.PREFIX):
                    continue
                payload = line[len(self.PREFIX):]
                try:
                    update = json.loads(payload)
                    with self.lock:
                        self.state.update(update)
                    self.connected = True
                except json.JSONDecodeError:
                    pass
        finally:
            ser.close()
