"""
screen_sync.py — UDP-broadcast peer-to-peer state sync between PFD screens.

Each screen opens one UDP socket on `port` and:

  * Publishes selected categories (bugs, baro, nav, AHRS, GPS) by
    broadcasting a small JSON packet to every up interface — USB
    direct, Cabin WiFi, Pico-W AP, all at once.  The peer hears
    whichever interface delivers it first.
  * Listens for the same broadcasts from peers and dispatches them
    to per-category callbacks the host process registers.

Architecture is strictly peer-to-peer (no master).  Each instance
generates a UUID at boot which it stamps onto every packet so it can
ignore its own broadcasts (we always see them on the bound socket).

Conflict resolution for slow-rate state (bugs/baro/nav) is last-write-
wins on the tuple ``(wallclock_ts, seq, src_id)``.  High-rate streams
(AHRS, GPS) just take the most recent — no resolution needed.
"""

import json
import socket
import subprocess
import threading
import time
import uuid


DEFAULT_PORT = 49737

# Per-process instance ID, stamped on every outgoing packet so we can
# discard our own echoes (UDP broadcast on the bound socket loops back).
INSTANCE_ID = str(uuid.uuid4())

# Category constants — pass these to publish_kinds / consume_kinds so
# callers don't have to remember the string literals.
KIND_BUGS  = "bugs"   # alt_bug, spd_bug, hdg_bug, vs_bug
KIND_BARO  = "baro"   # baro_hpa
KIND_NAV   = "nav"    # D2 ident, lat/lon, activation point
KIND_AHRS  = "ahrs"   # pitch, roll, yaw
KIND_GPS   = "gps"    # lat, lon, alt, speed, track
KIND_HELLO = "hello"  # heartbeat, no payload — keeps peer-status live

ALL_KINDS = (KIND_BUGS, KIND_BARO, KIND_NAV, KIND_AHRS, KIND_GPS)

# Heartbeat interval (s) — the listener marks a peer "stale" if no
# packet of any kind has arrived in PEER_STALE_AFTER_S.
HEARTBEAT_INTERVAL_S = 2.0
PEER_STALE_AFTER_S   = 6.0


class ScreenSync:
    """UDP broadcast sync.  One per process.  Thread-safe."""

    def __init__(self, port=DEFAULT_PORT,
                 publish_kinds=None, consume_kinds=None):
        self.port = port
        self._publish_kinds = set(publish_kinds or [])
        self._consume_kinds = set(consume_kinds or [])
        self._seq = 0
        self._lock = threading.Lock()
        self._sock = None
        self._listener = None
        self._heartbeat = None
        self._running = False
        # LWW per kind: kind -> (ts, seq, src_id)
        self._last_accepted = {}
        # Per-kind callbacks: kind -> fn(payload_dict)
        self._callbacks = {}
        # Peer liveness: src_id -> last_seen_monotonic
        self._peers = {}
        # Cached broadcast addrs.  Re-enumerated every 5s so hot-plugged
        # interfaces (e.g. USB ethernet gadget) get picked up.
        self._baddrs = []
        self._baddrs_t = 0.0

    # ── lifecycle ──────────────────────────────────────────────────────────

    def start(self):
        if self._running:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", self.port))
        sock.settimeout(0.5)
        self._sock = sock
        self._running = True
        self._listener = threading.Thread(
            target=self._listen_loop, name="screen_sync_listen",
            daemon=True)
        self._listener.start()
        self._heartbeat = threading.Thread(
            target=self._heartbeat_loop, name="screen_sync_heartbeat",
            daemon=True)
        self._heartbeat.start()

    def stop(self):
        self._running = False
        try:
            if self._sock is not None:
                self._sock.close()
        except OSError:
            pass
        self._sock = None

    # ── configuration ──────────────────────────────────────────────────────

    def set_publish_kinds(self, kinds):
        with self._lock:
            self._publish_kinds = set(kinds)

    def set_consume_kinds(self, kinds):
        with self._lock:
            self._consume_kinds = set(kinds)

    def publish_enabled(self, kind):
        return kind in self._publish_kinds

    def consume_enabled(self, kind):
        return kind in self._consume_kinds

    def on(self, kind, callback):
        """Register a callback invoked with the payload dict when a peer
        publishes `kind` AND consume is enabled for that kind."""
        self._callbacks[kind] = callback

    # ── publish ────────────────────────────────────────────────────────────

    def publish(self, kind, payload):
        """Broadcast `payload` (a dict) under `kind`.  No-op when sync is
        stopped or `kind` isn't in the publish set."""
        if not self._running or self._sock is None:
            return
        if kind != KIND_HELLO and kind not in self._publish_kinds:
            return
        with self._lock:
            self._seq += 1
            seq = self._seq
        msg = {
            "src":  INSTANCE_ID,
            "kind": kind,
            "ts":   time.time(),
            "seq":  seq,
            "data": payload,
        }
        data = json.dumps(msg, separators=(",", ":")).encode("utf-8")
        for baddr in self._get_broadcast_addrs():
            try:
                self._sock.sendto(data, (baddr, self.port))
            except OSError:
                pass

    # ── peer status (for UI) ───────────────────────────────────────────────

    def peer_status(self):
        """Return ``(n_peers_alive, age_s_of_most_recent_or_None)``.  Used
        by the connectivity setup screen to show a green/grey badge."""
        now = time.monotonic()
        active = [(sid, ts) for sid, ts in self._peers.items()
                  if now - ts < PEER_STALE_AFTER_S]
        if not active:
            return 0, None
        recent = max(ts for _, ts in active)
        return len(active), now - recent

    def first_peer_id(self):
        """Return the first 8 chars of the most-recently-seen peer's
        UUID, or ``""``.  Convenient for compact UI badges."""
        n, _ = self.peer_status()
        if n == 0:
            return ""
        return max(self._peers.items(), key=lambda kv: kv[1])[0][:8]

    # ── internals ──────────────────────────────────────────────────────────

    def _get_broadcast_addrs(self):
        now = time.monotonic()
        if self._baddrs and now - self._baddrs_t < 5.0:
            return self._baddrs
        self._baddrs = _enumerate_broadcast_addrs()
        self._baddrs_t = now
        return self._baddrs

    def _listen_loop(self):
        while self._running:
            try:
                data, _addr = self._sock.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                msg = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            src = msg.get("src", "")
            if src == INSTANCE_ID or not src:
                continue
            kind = msg.get("kind", "")
            # Heartbeats (and every other packet) refresh peer liveness.
            self._peers[src] = time.monotonic()
            if kind == KIND_HELLO:
                continue
            if kind not in self._consume_kinds:
                continue
            ts  = float(msg.get("ts", 0.0))
            seq = int(msg.get("seq", 0))
            # LWW: only accept if (ts, seq, src) is newer than what we
            # have on file for this kind.  This makes a remote screen's
            # most-recent edit stick across reconnects.
            with self._lock:
                last = self._last_accepted.get(kind)
                if last is not None:
                    if (ts, seq, src) <= last:
                        continue
                self._last_accepted[kind] = (ts, seq, src)
            cb = self._callbacks.get(kind)
            if cb is None:
                continue
            try:
                cb(msg.get("data", {}))
            except Exception:
                # A bad callback must not take the listener down.
                pass

    def _heartbeat_loop(self):
        while self._running:
            time.sleep(HEARTBEAT_INTERVAL_S)
            if not self._running:
                break
            # Heartbeat is special — always sent (not gated on
            # publish_kinds) so peer-status badges work even when no
            # category is being shared.
            try:
                self.publish(KIND_HELLO, {})
            except Exception:
                pass


# ── interface enumeration ─────────────────────────────────────────────────


def _enumerate_broadcast_addrs():
    """Return broadcast addresses for every IPv4 interface that's up.

    Falls back to the limited broadcast 255.255.255.255 if `ip` isn't
    available (non-Linux host, missing iproute2, etc.).  In that case
    the kernel routes through the default interface only — fine for
    bench testing.
    """
    addrs = []
    seen = set()
    try:
        out = subprocess.check_output(
            ["ip", "-4", "-o", "addr", "show"],
            stderr=subprocess.DEVNULL, timeout=1.0).decode()
        for line in out.splitlines():
            parts = line.split()
            # Format: "2: eth0    inet 192.168.1.5/24 brd 192.168.1.255 ..."
            try:
                inet_i = parts.index("inet")
                brd_i  = parts.index("brd", inet_i)
                baddr  = parts[brd_i + 1]
            except (ValueError, IndexError):
                continue
            if baddr in seen or baddr == "0.0.0.0":
                continue
            seen.add(baddr)
            addrs.append(baddr)
    except (FileNotFoundError, OSError, subprocess.SubprocessError,
            subprocess.TimeoutExpired):
        pass
    if not addrs:
        addrs = ["255.255.255.255"]
    return addrs


# ── tiny CLI test harness ─────────────────────────────────────────────────


def _cli_test():
    """Run as ``python3 screen_sync.py <name>`` on two machines (or two
    terminals on one machine — they'll see each other on loopback if it
    has a broadcast addr).  Each press of Enter publishes a counter
    under ``bugs``; incoming packets print to stdout."""
    import sys
    name = sys.argv[1] if len(sys.argv) > 1 else "anon"
    ss = ScreenSync(publish_kinds={KIND_BUGS},
                    consume_kinds={KIND_BUGS, KIND_AHRS,
                                   KIND_NAV, KIND_BARO, KIND_GPS})

    def show(payload):
        print(f"  ← {payload}")

    ss.on(KIND_BUGS, show)
    ss.start()
    print(f"[{name}] instance {INSTANCE_ID[:8]} listening on UDP {DEFAULT_PORT}")
    print(f"[{name}] broadcast targets: {_enumerate_broadcast_addrs()}")
    print(f"[{name}] press ENTER to publish a counter, Ctrl-C to quit")
    counter = 0
    try:
        while True:
            input()
            counter += 1
            ss.publish(KIND_BUGS, {"alt_bug": 5000 + counter,
                                   "from": name})
            n, age = ss.peer_status()
            age_s = f"{age:.1f}s" if age is not None else "—"
            print(f"  → sent #{counter} (peers={n} age={age_s})")
    except KeyboardInterrupt:
        ss.stop()
        print()


if __name__ == "__main__":
    _cli_test()
