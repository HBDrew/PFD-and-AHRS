"""
sse_client.py – Threaded SSE client for Pico W AHRS data.

Connects to http://192.168.4.1/events, parses JSON data: lines,
and merges updates into the shared state dict with a lock.

Usage:
    from sse_client import SSEClient
    client = SSEClient("http://192.168.4.1/events", state, state_lock)
    client.start()          # starts background thread
    client.connected        # True once first event received
    client.stop()           # graceful shutdown
"""

import threading
import socket
import time
import json


class SSEClient(threading.Thread):
    def __init__(self, url: str, state: dict, lock: threading.Lock,
                 reconnect_delay: float = 3.0):
        super().__init__(daemon=True, name="SSEClient")
        self.url            = url
        self.state          = state
        self.lock           = lock
        self.reconnect_delay = reconnect_delay
        self.connected      = False
        self.paused         = False  # when True, skip state.update so sim/demo win
        self._stop_event    = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        while not self._stop_event.is_set():
            try:
                self._connect_and_read()
            except Exception as e:
                print(f"[SSE] Error: {e}")
            self.connected = False
            if not self._stop_event.is_set():
                print(f"[SSE] Reconnecting in {self.reconnect_delay}s…")
                time.sleep(self.reconnect_delay)

    def _connect_and_read(self):
        # Parse URL
        url = self.url
        if url.startswith("http://"):
            url = url[7:]
        host, _, path = url.partition("/")
        path = "/" + path
        port = 80
        if ":" in host:
            host, port_str = host.rsplit(":", 1)
            port = int(port_str)

        print(f"[SSE] Connecting to {host}:{port}{path}")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(15)
        sock.connect((host, port))

        try:
            request = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host}\r\n"
                f"Accept: text/event-stream\r\n"
                f"Connection: keep-alive\r\n"
                f"\r\n"
            )
            sock.sendall(request.encode())

            # Read HTTP headers
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = sock.recv(1024)
                if not chunk:
                    raise ConnectionError("Connection closed during headers")
                buf += chunk

            # Anything after headers is the SSE body
            _, _, body = buf.partition(b"\r\n\r\n")
            sock.settimeout(30)  # longer timeout for SSE stream

            # Read SSE events line by line
            remainder = body
            while not self._stop_event.is_set():
                # Accumulate until we have a complete line
                while b"\n" not in remainder:
                    chunk = sock.recv(4096)
                    if not chunk:
                        raise ConnectionError("Stream closed")
                    remainder += chunk

                line, _, remainder = remainder.partition(b"\n")
                line = line.decode("utf-8", errors="ignore").rstrip("\r")

                if line.startswith("data:"):
                    payload = line[5:].strip()
                    try:
                        update = json.loads(payload)
                        # Keep consuming events (don't let the stream
                        # buffer) but don't merge into state while paused —
                        # sim/demo owns the state dict until unpaused.
                        if not self.paused:
                            with self.lock:
                                self.state.update(update)
                        self.connected = True
                    except json.JSONDecodeError:
                        pass

        finally:
            sock.close()
