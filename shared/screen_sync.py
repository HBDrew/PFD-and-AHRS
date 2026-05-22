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

Transport selection: callers can restrict outgoing broadcasts to a
single category of interface — USB-gadget (``usb*``/``enx*``) or
local network (``wl*``/``eth*``/``en*``) — via ``set_transport()``.
The listener still accepts from anywhere; the filter only narrows
where *we* send.  This lets a user force USB-only sync to verify the
USB link is actually carrying packets when both transports are up.
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
KIND_FPL   = "fpl"    # full flight-plan waypoint list + active_idx
KIND_HELLO = "hello"  # heartbeat, no payload — keeps peer-status live

ALL_KINDS = (KIND_BUGS, KIND_BARO, KIND_NAV, KIND_AHRS, KIND_GPS, KIND_FPL)

# Transport selectors.  "auto" sends on every usable interface (USB
# and network alike); "usb" / "net" restrict sends to one category.
# Interfaces are classified by name (see _classify_iface).
TRANSPORT_AUTO = "auto"
TRANSPORT_USB  = "usb"
TRANSPORT_NET  = "net"
ALL_TRANSPORTS = (TRANSPORT_AUTO, TRANSPORT_USB, TRANSPORT_NET)

# Heartbeat interval (s) — the listener marks a peer "stale" if no
# packet of any kind has arrived in PEER_STALE_AFTER_S.
HEARTBEAT_INTERVAL_S = 2.0
PEER_STALE_AFTER_S   = 6.0


class ScreenSync:
    """UDP broadcast sync.  One per process.  Thread-safe."""

    def __init__(self, port=DEFAULT_PORT,
                 publish_kinds=None, consume_kinds=None,
                 enabled=True, transport=TRANSPORT_AUTO):
        self.port = port
        self._publish_kinds = set(publish_kinds or [])
        self._consume_kinds = set(consume_kinds or [])
        self._enabled = bool(enabled)
        self._transport = (transport if transport in ALL_TRANSPORTS
                           else TRANSPORT_AUTO)
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
        # Cached interface info.  Re-enumerated every 5s so hot-plugged
        # interfaces (e.g. USB ethernet gadget) get picked up.
        self._ifaces = []
        self._ifaces_t = 0.0
        # Per-interface counters for the diagnostics row.
        self._tx_by_iface = {}    # name -> count
        self._rx_by_iface = {}    # name -> count

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

    def set_enabled(self, enabled):
        """Master switch.  When False, publish() is a no-op, the
        heartbeat is suppressed, and incoming packets are dropped (so
        peer status reverts to NO PEER)."""
        with self._lock:
            self._enabled = bool(enabled)
            if not self._enabled:
                self._peers.clear()

    def enabled(self):
        return self._enabled

    def set_transport(self, transport):
        """Restrict outgoing sends to "usb", "net", or "auto" (both)."""
        with self._lock:
            if transport in ALL_TRANSPORTS:
                self._transport = transport
            # Invalidate iface cache so the next send picks up the
            # filter immediately rather than after the 5s TTL.
            self._ifaces_t = 0.0

    def transport(self):
        return self._transport

    def on(self, kind, callback):
        """Register a callback invoked with the payload dict when a peer
        publishes `kind` AND consume is enabled for that kind."""
        self._callbacks[kind] = callback

    # ── publish ────────────────────────────────────────────────────────────

    def publish(self, kind, payload):
        """Broadcast `payload` (a dict) under `kind`.  No-op when sync is
        stopped, disabled, or `kind` isn't in the publish set."""
        if not self._running or self._sock is None or not self._enabled:
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
        for iface in self._eligible_ifaces():
            try:
                self._sock.sendto(data, (iface["baddr"], self.port))
            except OSError:
                continue
            name = iface["name"]
            with self._lock:
                self._tx_by_iface[name] = (
                    self._tx_by_iface.get(name, 0) + 1)

    # ── peer status (for UI) ───────────────────────────────────────────────

    def peer_status(self):
        """Return ``(n_peers_alive, age_s_of_most_recent_or_None)``.  Used
        by the connectivity setup screen to show a green/grey badge."""
        if not self._enabled:
            return 0, None
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

    def iface_stats(self):
        """Return per-interface diagnostics for the Screen Sync setup
        screen.  List of dicts: ``{name, category, baddr, tx, rx,
        eligible}``.  ``eligible`` reflects the current transport
        filter — useful for showing greyed-out rows for interfaces
        that exist but aren't being used."""
        ifaces = self._get_interfaces()
        t = self._transport
        out = []
        with self._lock:
            for i in ifaces:
                cat = i["category"]
                if t == TRANSPORT_AUTO:
                    eligible = cat in ("usb", "net")
                else:
                    eligible = cat == t
                out.append({
                    "name":     i["name"],
                    "category": cat,
                    "baddr":    i["baddr"],
                    "tx":       self._tx_by_iface.get(i["name"], 0),
                    "rx":       self._rx_by_iface.get(i["name"], 0),
                    "eligible": eligible,
                })
        # Also include a synthetic row for unclassified RX (packets
        # whose source IP didn't match any known interface subnet),
        # but only if we actually received any.
        with self._lock:
            unk = self._rx_by_iface.get("?", 0)
        if unk:
            out.append({
                "name": "?", "category": "other", "baddr": "",
                "tx": 0, "rx": unk, "eligible": False,
            })
        return out

    # ── internals ──────────────────────────────────────────────────────────

    def _get_interfaces(self):
        now = time.monotonic()
        if self._ifaces and now - self._ifaces_t < 5.0:
            return self._ifaces
        self._ifaces = _enumerate_interfaces()
        self._ifaces_t = now
        return self._ifaces

    def _eligible_ifaces(self):
        """Interfaces to broadcast through, honouring the transport
        filter.  Falls back to a single limited-broadcast pseudo-iface
        when no interfaces are enumerable (non-Linux, no iproute2)."""
        ifaces = self._get_interfaces()
        if not ifaces:
            return [{"name": "?", "baddr": "255.255.255.255",
                     "category": "other", "ip": "", "prefix": 0}]
        t = self._transport
        if t == TRANSPORT_AUTO:
            picked = [i for i in ifaces if i["category"] in ("usb", "net")]
        else:
            picked = [i for i in ifaces if i["category"] == t]
        if not picked:
            # User selected a category with no live interfaces.  Don't
            # silently fall back to the other category — that would
            # defeat the whole point of the selector.  Return empty;
            # peer status will show NO PEER until they fix it.
            return []
        return picked

    def _classify_sender(self, src_ip):
        """Map a packet's source IP to one of our local interface names
        by subnet match.  Returns the interface name or None."""
        for i in self._get_interfaces():
            if i["prefix"] > 0 and _ip_in_subnet(src_ip, i["ip"],
                                                  i["prefix"]):
                return i["name"]
        return None

    def _listen_loop(self):
        while self._running:
            try:
                data, addr = self._sock.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError:
                break
            # Even when disabled we keep draining the socket so its
            # receive buffer doesn't fill — but we don't count, don't
            # touch peer state, and don't fire callbacks.
            if not self._enabled:
                continue
            try:
                msg = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            src = msg.get("src", "")
            if src == INSTANCE_ID or not src:
                continue
            # Per-interface RX accounting — classify by sender subnet.
            src_ip = addr[0] if isinstance(addr, tuple) else ""
            iface_name = self._classify_sender(src_ip) or "?"
            with self._lock:
                self._rx_by_iface[iface_name] = (
                    self._rx_by_iface.get(iface_name, 0) + 1)
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
            if not self._enabled:
                continue
            # Heartbeat is special — always sent (not gated on
            # publish_kinds) so peer-status badges work even when no
            # category is being shared.
            try:
                self.publish(KIND_HELLO, {})
            except Exception:
                pass


# ── interface enumeration ─────────────────────────────────────────────────


def _classify_iface(name):
    """Map an interface name to one of: 'usb', 'net', 'loop', 'other'.

    USB-gadget interfaces on Raspberry Pi OS come up as ``usb0`` when
    the dwc2 + g_ether modules are loaded; systemd's predictable
    naming uses ``enx<MAC>`` for USB ethernet.  Everything else with
    a routable broadcast is treated as 'net' (wireless, on-board
    ethernet, Pico-W AP)."""
    if name == "lo":
        return "loop"
    if name.startswith("usb") or name.startswith("enx"):
        return "usb"
    if (name.startswith("wl") or name.startswith("eth")
            or name.startswith("en")):
        # 'en*' catches enp/eno predictable names.  'enx*' was already
        # caught above as USB.
        return "net"
    return "other"


def _ip_to_int(ip_str):
    a, b, c, d = (int(p) for p in ip_str.split("."))
    return (a << 24) | (b << 16) | (c << 8) | d


def _ip_in_subnet(ip_str, subnet_ip, prefix):
    """True if ip_str is inside subnet_ip/prefix."""
    if prefix <= 0 or prefix > 32:
        return False
    try:
        ip_i  = _ip_to_int(ip_str)
        net_i = _ip_to_int(subnet_ip)
    except (ValueError, AttributeError):
        return False
    mask = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF
    return (ip_i & mask) == (net_i & mask)


def _enumerate_interfaces():
    """Return list of dicts describing every IPv4 interface that's up:
    ``{name, ip, prefix, baddr, category}``.

    Empty list if `ip` isn't available — in that case publish() falls
    back to the limited broadcast 255.255.255.255 via _eligible_ifaces."""
    out = []
    seen = set()
    try:
        ip_out = subprocess.check_output(
            ["ip", "-4", "-o", "addr", "show"],
            stderr=subprocess.DEVNULL, timeout=1.0).decode()
        for line in ip_out.splitlines():
            parts = line.split()
            # Format: "2: eth0    inet 192.168.1.5/24 brd 192.168.1.255 ..."
            if len(parts) < 2:
                continue
            name = parts[1]
            try:
                inet_i = parts.index("inet")
                cidr   = parts[inet_i + 1]
                ip_str, prefix_s = cidr.split("/")
                prefix = int(prefix_s)
            except (ValueError, IndexError):
                continue
            try:
                brd_i = parts.index("brd", inet_i)
                baddr = parts[brd_i + 1]
            except (ValueError, IndexError):
                continue
            if baddr in ("0.0.0.0",) or baddr in seen:
                continue
            cat = _classify_iface(name)
            if cat == "loop":
                continue
            seen.add(baddr)
            out.append({
                "name":     name,
                "ip":       ip_str,
                "prefix":   prefix,
                "baddr":    baddr,
                "category": cat,
            })
    except (FileNotFoundError, OSError, subprocess.SubprocessError,
            subprocess.TimeoutExpired):
        pass
    return out


def _enumerate_broadcast_addrs():
    """Compat shim — return just the broadcast addrs.  Falls back to
    255.255.255.255 if `ip` isn't available.  Used by the CLI test."""
    ifaces = _enumerate_interfaces()
    if not ifaces:
        return ["255.255.255.255"]
    return [i["baddr"] for i in ifaces]


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
    print(f"[{name}] interfaces:")
    for i in _enumerate_interfaces():
        print(f"    {i['name']:<8} {i['category']:<5} "
              f"{i['ip']}/{i['prefix']}  brd {i['baddr']}")
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
            for s in ss.iface_stats():
                mark = "*" if s["eligible"] else " "
                print(f"    {mark} {s['name']:<8} {s['category']:<5} "
                      f"tx={s['tx']:<4} rx={s['rx']:<4}")
    except KeyboardInterrupt:
        ss.stop()
        print()


if __name__ == "__main__":
    _cli_test()
