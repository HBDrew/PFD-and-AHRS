#!/usr/bin/env python3
"""
pfd.py – GI-275 inspired PFD for Raspberry Pi 4 with full OpenGL SVT.

This version targets the Pi 4's GPU (VideoCore VI) and will be migrated
to OpenGL ES vector graphics for true Synthetic Vision Technology (SVT)
including terrain rendering above the horizon line.

Currently runs the pygame-based renderer (inherited from the original
codebase).  The OpenGL migration is planned — see svt_renderer.py for
the scaffold.

Run:  python3 pfd.py           (connects to Pico W at 192.168.4.1)
      python3 pfd.py --demo    (Sedona demo, no hardware needed)
      python3 pfd.py --sim     (windowed for desktop testing)
"""

import math
import sys
import time
import threading
import argparse
import os
import io
import gzip
import socket
import subprocess
import urllib.request

# Add shared modules to path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'shared'))

os.environ.setdefault("SDL_VIDEODRIVER", "kmsdrm")  # overridden by --sim
os.environ["SDL_AUDIODRIVER"] = "dummy"  # suppress ALSA underrun spam

import pygame
import pygame.gfxdraw

from config import *   # noqa: F403
from sse_client import SSEClient
from terrain import get_elevation_ft
from svt_renderer import render_svt as render_svt_pygame

# Try to load the OpenGL SVT renderer.  Falls back to pygame on failure.
# NOTE: GL SVT is disabled while we resolve EGL/KMS device contention on
# Pi 4 hardware — the standalone EGL context locks the V3D GPU and prevents
# pygame's KMS/DRM display from rendering.  The pygame scanline SVT works
# at ~17 fps in the meantime.  Set _FORCE_PYGAME_SVT = False to re-test.
# GL SVT availability is probed LAZILY on first render frame — not at
# import time — so pygame can grab KMS/DRM first without the EGL probe
# stealing the GPU device.  _SVT_GL_AVAILABLE starts as None (unknown)
# and gets set to True/False on the first frame.
# GL SVT disabled — EGL context creation disrupts KMS/DRM display on
# this Pi 4 kernel/mesa combination regardless of device_index or timing.
# Needs further investigation with a different EGL approach (perhaps
# piglit/gbm backend or sharing pygame's own GL context).
_SVT_GL_AVAILABLE = False
_gl_available = None
try:
    from svt_renderer_gl import render_svt_gl, render_svt_into_current_fb
except Exception:
    render_svt_gl = None
    render_svt_into_current_fb = None

# Shared-context composite path (pygame.OPENGL + moderngl).  Imported lazily
# because it's only needed when SVT_RENDERER == "opengl_shared".
try:
    from svt_composite_gl import setup_gl_display, Compositor
    HAS_SHARED_GL = True
except Exception:
    setup_gl_display = None
    Compositor = None
    HAS_SHARED_GL = False

# Populated by main() when opengl_shared setup succeeds.  render() and _flip()
# check `_shared_gl_ctx is not None` to take the GL composite path.
_shared_gl_ctx = None
_shared_gl_compositor = None

import obstacles as obs_mod
import airports as apt_mod
import runways as rwy_mod
import water as water_mod
import settings as _settings
import moving_map as _map_mod
import sun as _sun_mod
import hits as _hits_mod

DEG = math.pi / 180

# ── Colour palette ────────────────────────────────────────────────────────────
SKY_TOP    = ( 10,  42,  80)
SKY_HOR    = ( 58, 130, 200)
GND_HOR    = (130,  85,  45)
GND_BOT    = ( 60,  40,  20)
WHITE      = (255, 255, 255)
YELLOW     = (255, 215,   0)
CYAN       = (  0, 220, 220)
RED        = (220,  30,  30)
ORANGE     = (220, 100,   0)
GREEN_ARC  = ( 30, 200,  50)
YELLOW_ARC = (240, 200,   0)
TAPE_BG    = (  0,   8,  22, 195)
DIMGREY    = ( 80,  80,  90)
LTGREY     = (180, 180, 190)
MAGENTA    = (220,   0, 220)

# ── Shared state ─────────────────────────────────────────────────────────────
_state_lock = threading.Lock()
state = {
    "roll": 0.0, "pitch": 0.0, "yaw": 0.0, "ay": 0.0,
    "lat": DEMO_LAT, "lon": DEMO_LON,
    "speed": 0.0, "track": 0.0, "fix": 0, "sats": 0,
    "alt": 0.0, "gps_alt": 0.0, "vspeed": 0.0,
    "baro_src": "gps", "baro_hpa": BARO_DEFAULT_HPA,
    "pitch_trim": 0.0, "roll_trim": 0.0, "yaw_trim": 0.0,
    "ahrs_ok": False, "gps_ok": False, "baro_ok": False,
}

# ── Display values (smoothed) ─────────────────────────────────────────────────
disp = dict(state)
disp["hdg_bug"]       = 0.0
disp["trk_bug"]       = 0.0  # GPS-track bug (used when heading source is "trk")
disp["alt_bug"]       = 0.0
disp["baro_hpa"]      = BARO_DEFAULT_HPA
disp["show_demo"]     = False
disp["mode"]          = "pfd"       # "pfd"|"setup"|"flight_profile"|"numpad"|"keyboard"
                                     # |"display_setup"|"ahrs_setup"|"connectivity_setup"|"system_setup"
                                     # |"sim_setup"|"sim_controls"
disp["numpad_target"] = ""          # "alt_bug"|"hdg_bug"|"spd_bug"|fp key
disp["numpad_buf"]    = ""          # digits entered so far
disp["numpad_prev"]   = "pfd"       # mode to return to on cancel/enter
disp["kbd_target"]    = ""          # field being edited in keyboard mode
disp["kbd_buf"]       = ""          # text entered so far
disp["kbd_prev"]      = "flight_profile"  # mode to return to on DONE/CANCEL
disp["fp"] = {                      # flight-profile values
    "tail":   "N12345", "actype": "C172S",
    "vs0":    VS0,  "vs1": VS1,  "vfe": VFE,
    "vno":    VNO,  "vne": VNE,  "va":  VA,
    "vy":     VY,   "vx":  VX,
}
disp["display_mode"]  = "pfd"       # "pfd" | "mfd" (MFD not yet implemented)
disp["td"] = {                      # terrain download state
    "downloading": False,
    "dl_region":   "",
    "dl_current":  0,
    "dl_total":    0,
    "dl_status":   "",
    "dl_cancel":   False,
}
disp["wd"] = {                      # water-mask rasterise state (companion
    "downloading": False,           # to terrain download — produces a .water
    "dl_current":  0,               # file per existing SRTM tile from the
    "dl_total":    0,               # Natural Earth ocean+lakes shapefiles)
    "dl_status":   "",
    "dl_cancel":   False,
}
disp["od"] = {                      # obstacle download/parse state
    "downloading": False,
    "dl_status":   "",
    "dl_cancel":   False,
    "parsing":     False,
    "records":     0,       # record count after successful load
    "used_mb":     0.0,
    "dl_date":     None,    # datetime.date of last download (or None)
    "expired":     False,   # True when file is > OBSTACLE_EXPIRY_DAYS old
    "age_days":    0,
}
_obstacles = None           # loaded obstacle array (module-level)
_airports  = None           # loaded airport array (module-level)
_runways   = None           # loaded runway array (module-level)
disp["ad"] = {                      # airport download/parse state
    "downloading": False,
    "dl_status":   "",
    "dl_cancel":   False,
    "parsing":     False,
    "records":     0,
    "used_mb":     0.0,
    "dl_date":     None,    # datetime.date of last download
    "expired":     False,   # True when CSV is > AIRPORT_EXPIRY_DAYS old
    "age_days":    0,
    # Per-category display filters (bool).  Users toggle these on the
    # AIRPORT DATA screen to reduce clutter.
    "show_public":   True,      # S/M/L — small/medium/large airports
    "show_heli":     True,      # H — heliports
    "show_seaplane": False,     # W — seaplane bases
    "show_other":    False,     # B — balloonports + misc
    # Runway + extended-centerline rendering (Pi 4 primarily; Pi Zero
    # renders the polygons as 2D projections too).
    "show_runways":     True,
    "show_centerlines": True,
    # SVT water bodies (oceans + lakes from Natural Earth, rasterised into
    # the data/water/ companion of the SRTM tiles).  Off-by-default has no
    # effect when no .water tiles are present.
    "show_water":       True,
}
disp["ds"] = {                      # display settings
    "spd_unit":  "kt",   "alt_unit":   "ft",
    "baro_unit": "inhg", "brightness": 8,  "night_mode": False,
    # Lower-left moving-map inset
    "map_enabled":       False,
    "map_orient":        "trk",     # "trk" | "nrth"
    "map_zoom_nm":       5,         # one of 1, 2, 5, 10, 20, 40
    "map_show_terrain":  True,
    "map_show_water":    True,      # reserved — water tile overlay
    "map_show_airports": True,
    "map_show_runways":  True,
    "map_show_obstacles": True,
    "map_show_directto": True,
    # Real-time SVT sun position (off → SE / mid-morning fixed lighting)
    "sun_realtime":      True,
}
disp["ss"] = {                      # AHRS / sensor settings
    "pitch_trim":    0.0, "roll_trim": 0.0,
    "mag_cal":       "idle", "mounting": "normal",
    "mag_cal_deltas": [0.0] * 4,     # per-cardinal (N/E/S/W) heading
                                      # corrections from the compass-cal
                                      # wizard.  Piecewise-linear
                                      # interpolation between cardinals
                                      # gives each 90° quadrant its own
                                      # correction curve.

    # Heading source — matches the iPhone display:
    #   "mag"  : magnetic heading from AHRS (cyan/white "M" subscript).
    #   "trk"  : GPS ground track via complementary filter (magenta "G").
    #            Falls back to "G?" when GPS fix is missing or speed < 3 kt
    #            (track is unreliable below taxi speed).
    #   "auto" : prefer "trk" when GPS is moving, fall back to "mag" otherwise.
    "hdg_src":       "auto",
    "airspeed_src":  "gps",   # "gps" | "ias"  — speed source (GPS groundspeed or IAS sensor)
}
disp["cs"] = {                      # connectivity settings
    "ahrs_url":  PICO_URL, "wifi_ssid": "AHRS-Link",
    "wifi_pass": "",        "wifi_ok":  False,
    "wifi_actual": "",      # SSID actually associated now (from iwgetid -r)
    "scan_state": "",   "scan_nets": [], "scan_scroll": 0, "scan_error": "",
    "ahrs_ok":   False,     "test_msg": "", "apply_msg": "",
    # AHRS link diagnostics (populated by the transport client thread)
    "ahrs_transport": "",   # "usb" | "wifi" | ""
    "ahrs_port":      "",   # /dev/ttyACM0 or the SSE URL
    "ahrs_rx":        0,    # count of $AHRS, lines parsed OK
    "ahrs_err":       0,    # count of parse / IO errors
    "ahrs_last_err":  "",   # most recent error message
}
disp["sim"] = {                     # flight simulator state
    "preset_idx": 0,    # index into SIM_PRESETS
    "init_alt":   5000.0,
    "init_hdg":   0.0,
    "init_spd":   90.0,
    "gps_fail":   False,
    "baro_fail":  False,
    "ahrs_fail":  False,
    # Autopilot source for the sim:
    #   "bugs" — follow hdg_bug + alt_bug (default — what the sim has
    #             always done).
    #   "fp"   — follow the active direct-to (bearing to waypoint) +
    #             alt_bug; if a synthetic approach is active, slide
    #             down the 3° glideslope to the threshold once the
    #             aircraft has intercepted it.
    "follow_mode": "bugs",
}
disp["nav"] = {                     # rudimentary direct-to-airport navigation
    "ident":   "",      # ICAO/local ID of active waypoint, "" = none
    "lat":     0.0,
    "lon":     0.0,
    "elev_ft": 0.0,
    "act_lat": 0.0,     # aircraft lat at activation (CDI course reference)
    "act_lon": 0.0,
}

# Synthetic approach (HITS).  Active when the pilot has selected a
# specific runway end via the APPR picker.  When set, the renderer
# draws cyan HITS boxes along the extended centreline at a 3°
# glideslope, and the direct-to is auto-pointed at the threshold so
# the CDI / ETE line up with the runway instead of the airport
# centroid.  Cleared on PFD restart (not persisted) — fresh approach
# each flight.
disp["approach"] = {
    "active":          False,
    "airport":         "",         # parent airport ident (e.g. "KSEZ")
    "runway":          "",         # runway-end ident (e.g. "03")
    "thresh_lat":      0.0,
    "thresh_lon":      0.0,
    "thresh_elev_ft":  0.0,
    "course_deg":      0.0,        # true course TO the threshold
}

SMOOTH_K = 0.25   # IIR coefficient (higher = faster response)

# Heading-source resolution thresholds — match the iPhone display.
HDG_TRK_MIN_KT = 3.0   # below this speed, GPS track is unreliable


def _resolve_hdg_source(hdg_src_pref, gps_ok, ahrs_ok, speed_kt):
    """Resolve the user's preference (hdg_src in {"mag","trk","auto"}) +
    runtime conditions into the active source, label, and colour shown in
    the heading box / on the tape.

    Mirrors iphone_display/index.html#_activeHdg so the two displays use
    identical UX:

        Returns (use_track: bool, label: str, color: tuple).

    label is one of "M" / "G" / "M?" / "G?" / "?" ; color is white for
    valid magnetic, MAGENTA for valid GPS track, amber for unavailable.
    """
    track_ok = gps_ok and (speed_kt or 0.0) > HDG_TRK_MIN_KT
    mag_ok   = ahrs_ok

    if hdg_src_pref == "mag":
        if mag_ok:
            return False, "M", WHITE
        return False, "M?", AMBER
    if hdg_src_pref == "trk":
        if track_ok:
            return True, "G", MAGENTA
        # Track unavailable (no GPS fix or below the speed threshold).
        # Fall back to MAG rather than keep returning use_track=True —
        # otherwise the GPS-slaved complementary filter has nothing to
        # slave against and the displayed heading drifts toward stale
        # GPS track (typically 0° on a stationary aircraft).  Amber
        # "G?" label still surfaces that the pilot's chosen source
        # isn't currently usable.
        if mag_ok:
            return False, "G?", AMBER
        return False, "?", AMBER
    # auto: prefer TRK when GPS is moving, fall back to MAG
    if track_ok:
        return True, "G", MAGENTA
    if mag_ok:
        return False, "M", WHITE
    return False, "?", AMBER

# ── Module-level SSE handle (set in main, restarted by handle_event) ─────────
_sse_client  = None
_sim_state   = None   # SimFlyState instance when sim is running, else None
_link_lost_t = None   # monotonic timestamp when link first dropped (None if connected)

# ── GPS-slaved heading complementary filter ───────────────────────────────────
# Propagate heading using the AHRS gyro yaw-rate (smooth, 30 Hz) and
# slowly slave the absolute reference toward the GPS ground track (1–5 Hz,
# noisy).  This mirrors how real GPS/IRS heading modes work.
_gps_hdg      = None   # current complementary-filter output (degrees, 0–360)
_prev_yaw_disp = None  # disp["yaw"] value from the previous frame


# ── GPS heading complementary filter ─────────────────────────────────────────

def _update_gps_heading(yaw_now: float, track: float, gps_ok: bool) -> float:
    """
    Complementary filter for GPS-slaved heading.

    High-frequency path: AHRS yaw rate (smooth, 30 Hz) propagates _gps_hdg.
    Low-frequency path:  GPS ground track slowly slaves the absolute value.

    Returns the filtered heading in degrees [0, 360).
    """
    global _gps_hdg, _prev_yaw_disp

    if _gps_hdg is None:
        # Initialise from GPS track if available, else fall back to yaw
        _gps_hdg       = track if gps_ok else yaw_now
        _prev_yaw_disp = yaw_now
        return _gps_hdg

    # ── Gyro propagation ───────────────────────────────────────────────────────
    # Use the frame-to-frame change in the AHRS yaw (already smoothed) as a
    # proxy for the gyro turn rate.  Normalise to (−180, +180] to handle the
    # 359° → 0° wrap correctly.
    delta = ((yaw_now - _prev_yaw_disp) + 180) % 360 - 180
    _gps_hdg = (_gps_hdg + delta) % 360
    _prev_yaw_disp = yaw_now

    # ── GPS slaving ────────────────────────────────────────────────────────────
    # Pull _gps_hdg toward the GPS track at rate GPS_HDG_SLAVE_K per frame.
    # Signed error handles the 359°/0° wrap.
    if gps_ok:
        err = ((track - _gps_hdg) + 180) % 360 - 180
        _gps_hdg = (_gps_hdg + err * GPS_HDG_SLAVE_K) % 360

    return _gps_hdg


# ── Connectivity helpers ──────────────────────────────────────────────────────

def _wifi_ssid_current():
    """Return currently-associated WiFi SSID, or '' if not connected / unsupported."""
    try:
        r = subprocess.run(["iwgetid", "-r"],
                           capture_output=True, text=True, timeout=2)
        return r.stdout.strip()
    except Exception:
        return ""


def _poll_wifi_status():
    """Background thread: update disp['cs']['wifi_ok'] and the actual
    connected SSID (wifi_actual) every 5 s."""
    while True:
        ssid = _wifi_ssid_current()
        disp["cs"]["wifi_actual"] = ssid
        disp["cs"]["wifi_ok"]     = bool(ssid)
        time.sleep(5)


def _SPD_DISP_FACTOR():
    """Current speed display conversion factor (kt → display unit)."""
    return {"kt": 1.0, "mph": 1.15078, "kph": 1.852}.get(
        disp["ds"].get("spd_unit", "kt"), 1.0)


def _ALT_DISP_FACTOR():
    """Current altitude display conversion factor (ft → display unit)."""
    return {"ft": 1.0, "m": 0.3048}.get(disp["ds"].get("alt_unit", "ft"), 1.0)


def _push_baro_to_pico(qnh_hpa: float):
    """Fire-and-forget HTTP GET to the Pico W's /baro?qnh=X endpoint.

    The firmware uses this to update its internal BME280 QNH so future
    altitude samples are computed against the pilot's altimeter setting.
    Without this call, the PFD would show the pilot's entered baro
    locally but the AHRS-derived altitude would still reflect the
    firmware's default QNH — a silent miscalibration.

    Runs in a background thread because the Pico's HTTP handler can
    take 100–300 ms to respond and must not block the PFD frame loop.
    Failures are logged but don't alter disp — the local baro value
    sticks regardless of whether the Pico is reachable.
    """
    base = disp.get("cs", {}).get("ahrs_url", "http://192.168.4.1").rstrip("/")
    url  = f"{base}/baro?qnh={qnh_hpa:.2f}"

    def _worker():
        try:
            import urllib.request
            with urllib.request.urlopen(url, timeout=3) as resp:
                resp.read()
        except Exception as e:
            print(f"[PFD] /baro push failed ({url}): {e}")

    threading.Thread(target=_worker, daemon=True, name="BaroPush").start()


def _poll_ahrs_diag():
    """Background thread: mirror the AHRS transport client's diagnostic
    counters (rx_count, err_count, last_err) into disp['cs'] so the
    Connectivity screen can render them."""
    while True:
        c = _sse_client
        if c is not None:
            disp["cs"]["ahrs_rx"]       = getattr(c, "rx_count",  0)
            disp["cs"]["ahrs_err"]      = getattr(c, "err_count", 0)
            disp["cs"]["ahrs_last_err"] = getattr(c, "last_err", "")
        time.sleep(1)


def _apply_wifi(ssid, password):
    """Connect wlan0 to ssid using nmcli (NetworkManager) or wpa_supplicant,
    whichever is managing the interface.  Returns (success: bool, message: str).
    For nmcli a sudoers entry for nmcli is required (see /etc/sudoers.d/pfd-nmcli).
    For wpa_supplicant the process must be root (or have a sudoers entry).
    """
    if not ssid:
        return False, "SSID required"

    # Mirror wifi_switch.sh: prefer nmcli when NetworkManager is running.
    try:
        nm = subprocess.run(["systemctl", "is-active", "--quiet", "NetworkManager"],
                            capture_output=True, timeout=5)
        use_nm = nm.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        use_nm = False

    try:
        if use_nm:
            CON = "pfd-wifi"
            subprocess.run(["sudo", "nmcli", "con", "delete", CON],
                           capture_output=True, timeout=5)
            cmd = ["sudo", "nmcli", "dev", "wifi", "connect", ssid]
            if password:
                cmd += ["password", password]
            cmd += ["name", CON]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                err = (r.stderr or r.stdout).strip()
                return False, err[:80] if err else "nmcli connect failed"
            return True, "WiFi connected"
        else:
            net_block = (
                f'network={{\n'
                f'    ssid="{ssid}"\n'
                + (f'    psk="{password}"\n    key_mgmt=WPA-PSK\n' if password
                   else '    key_mgmt=NONE\n')
                + '}\n'
            )
            conf = (
                "ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev\n"
                "update_config=1\ncountry=US\n\n"
                + net_block
            )
            with open("/etc/wpa_supplicant/wpa_supplicant.conf", "w") as f:
                f.write(conf)
            r = subprocess.run(["wpa_cli", "-i", "wlan0", "reconfigure"],
                               capture_output=True, text=True, timeout=5)
            if r.returncode != 0:
                return False, "wpa_cli failed"
            return True, "WiFi config applied — connecting…"
    except PermissionError:
        return False, "Permission denied — run with sudo"
    except FileNotFoundError as e:
        return False, f"Not found: {e.filename}"
    except subprocess.TimeoutExpired:
        return False, "Timed out connecting"
    except Exception as e:
        return False, str(e)[:60]


def _restart_sse(url):
    """Stop the current SSE client and start a new one pointing at url."""
    global _sse_client
    if _sse_client:
        _sse_client.stop()
    sse_url = url.rstrip("/") + "/events"
    _sse_client = SSEClient(sse_url, state, _state_lock)
    _sse_client.start()
    print(f"[PFD] SSE → {sse_url}")


def _test_ahrs_connection(url):
    """TCP connect test to the AHRS host. Returns (ok: bool, msg: str)."""
    try:
        stripped = url.replace("http://", "").replace("https://", "")
        host_port, *_ = stripped.split("/")
        host, *port_part = host_port.split(":")
        port = int(port_part[0]) if port_part else 80
        s = socket.socket()
        s.settimeout(3)
        s.connect((host, port))
        s.close()
        return True, f"Reached {host}:{port} \u2713"
    except Exception as e:
        return False, str(e)[:50]


# ── Backlight control ─────────────────────────────────────────────────────────

_BACKLIGHT_PATHS = [
    "/sys/class/backlight/rpi_backlight/brightness",
    "/sys/class/backlight/10-0045/brightness",
]
_backlight_path     = None
_backlight_max_path = None   # max_brightness sysfs node

def _init_backlight():
    """Find the active backlight sysfs node (called once at startup)."""
    global _backlight_path, _backlight_max_path
    for p in _BACKLIGHT_PATHS:
        if os.path.exists(p):
            _backlight_path     = p
            _backlight_max_path = os.path.join(os.path.dirname(p), "max_brightness")
            print(f"[BL] Using backlight: {p}")
            break

def _set_backlight(level: int):
    """Set brightness 1–10 → 0..max_brightness (or 0..255 fallback)."""
    if _backlight_path is None:
        return
    try:
        max_b = 255
        if _backlight_max_path and os.path.exists(_backlight_max_path):
            with open(_backlight_max_path) as f:
                max_b = int(f.read().strip())
        raw = max(0, min(max_b, int((level - 1) / 9.0 * max_b)))
        with open(_backlight_path, "w") as f:
            f.write(str(raw))
    except OSError:
        pass


def smooth_state():
    """Copy live state → display values with IIR smoothing for analogue fields."""
    with _state_lock:
        snap = dict(state)
    for k in ("roll", "pitch", "ay", "speed", "alt", "vspeed"):
        disp[k] = disp[k] * (1 - SMOOTH_K) + snap[k] * SMOOTH_K
    # Heading: handle 0/360 wraparound
    dh = ((snap["yaw"] - disp["yaw"] + 180) % 360) - 180
    disp["yaw"] = (disp["yaw"] + dh * SMOOTH_K) % 360
    # Boolean / discrete fields: copy directly.
    # NOTE: baro_hpa is NOT copied here — it's a user-set Kollsman-window value
    # owned by disp[] and pushed outward to the Pico W via _push_baro_to_pico().
    # Copying it from state every frame would clobber numpad/± button entries
    # whenever the SSE stream carried a stale QNH echo back from the firmware.
    for k in ("lat", "lon", "track", "fix", "sats",
              "gps_alt", "baro_src",
              "ahrs_ok", "gps_ok", "baro_ok",
              "pitch_trim", "roll_trim", "yaw_trim"):
        disp[k] = snap[k]


# ── Font helpers ──────────────────────────────────────────────────────────────
_fonts = {}

def _get_font(size: int, bold: bool = False):
    # Scale font size proportionally when display is larger than 640×480
    _fs = getattr(sys.modules[__name__], '_font_scale', None)
    if _fs is None:
        try:
            from config import FONT_SCALE
            _fs = FONT_SCALE
        except ImportError:
            _fs = 1.0
        sys.modules[__name__]._font_scale = _fs
    size = int(size * _fs)
    key = (size, bold)
    if key not in _fonts:
        if bold:
            paths = [
                "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
            ]
        else:
            paths = [
                "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
            ]
        fnt = None
        for p in paths:
            try:
                fnt = pygame.font.Font(p, size)
                break
            except Exception:
                continue
        if fnt is None:
            fnt = pygame.font.SysFont("monospace", size, bold=bold)
        _fonts[key] = fnt
    return _fonts[key]


def _text(surf, txt, size, colour, cx=None, cy=None, x=None, y=None, bold=False):
    """Render text centred on (cx,cy) or top-left at (x,y)."""
    fnt = _get_font(size, bold)
    img = fnt.render(str(txt), True, colour)
    if cx is not None:
        rx = cx - img.get_width() // 2
    else:
        rx = x
    if cy is not None:
        ry = cy - img.get_height() // 2
    else:
        ry = y
    surf.blit(img, (rx, ry))
    return img.get_width()


def _chamfer(pts, indices, r=3):
    """Round polygon corners at given indices with smooth arcs (works for 90° corners)."""
    import math
    n = len(pts)
    out = []
    for i, p in enumerate(pts):
        if i not in indices:
            out.append(p)
            continue
        prev_p = pts[(i - 1) % n]
        next_p = pts[(i + 1) % n]
        dx1 = prev_p[0] - p[0]; dy1 = prev_p[1] - p[1]
        l1 = (dx1*dx1 + dy1*dy1) ** 0.5
        if l1: dx1 /= l1; dy1 /= l1
        dx2 = next_p[0] - p[0]; dy2 = next_p[1] - p[1]
        l2 = (dx2*dx2 + dy2*dy2) ** 0.5
        if l2: dx2 /= l2; dy2 /= l2
        sx = p[0] + dx1*r;  sy = p[1] + dy1*r
        ex = p[0] + dx2*r;  ey = p[1] + dy2*r
        acx = p[0] + dx1*r + dx2*r
        acy = p[1] + dy1*r + dy2*r
        a1 = math.atan2(sy - acy, sx - acx)
        a2 = math.atan2(ey - acy, ex - acx)
        cross = dx1 * dy2 - dy1 * dx2
        da = a2 - a1
        if cross < 0:
            while da < 0: da += 2 * math.pi
        else:
            while da > 0: da -= 2 * math.pi
        for j in range(5):
            angle = a1 + da * j / 4
            out.append((int(round(acx + r * math.cos(angle))),
                        int(round(acy + r * math.sin(angle)))))
    return out


def _rolling_drum(surf, bx, by, bw, bh, value, n_digits, color, font_sz,
                  suppress_leading=False, power_offset=0, show_adjacent=False,
                  adj_slot_h=None):
    """
    Veeder-Root rolling-drum digit readout for pygame.
    show_adjacent=True: adjacent digits are ~50% visible above/below (true drum look).
    Cascading: every digit carries smoothly when the digit below approaches 9→0.
    """
    char_w  = bw // n_digits
    f       = _get_font(font_sz, bold=True)
    val_int = round(abs(value))
    slot_h  = ((adj_slot_h if adj_slot_h is not None else bh // 2)
               if show_adjacent else bh)

    for col_i in range(n_digits):
        power = power_offset + n_digits - 1 - col_i
        if suppress_leading and power > 0 and val_int < 10 ** power:
            continue

        if power == 0:
            d_cont = float(value % 10.0)
        else:
            lower_cont = (value % (10 ** power)) / (10 ** (power - 1))
            carry_frac = max(0.0, lower_cont - 9.0)
            d_lo   = (int(value) // (10 ** power)) % 10
            d_cont = float(d_lo) + carry_frac

        d_lo   = int(d_cont)
        frac   = d_cont - d_lo
        d_hi   = (d_lo + 1) % 10
        scroll = int(frac * slot_h)
        cx     = bx + col_i * char_w

        img_lo = f.render(str(d_lo), True, color)
        img_hi = f.render(str(d_hi), True, color)
        gw     = img_lo.get_width()
        gh     = img_lo.get_height()
        tx     = max(0, (char_w - gw) // 2)
        cell   = pygame.Surface((char_w, bh), pygame.SRCALPHA)

        if show_adjacent:
            d_prev   = (d_lo - 1 + 10) % 10
            d_hi2    = (d_lo + 2) % 10
            img_prev = f.render(str(d_prev), True, color)
            img_hi2  = f.render(str(d_hi2),  True, color)
            ty_lo    = bh // 2 - gh // 2 + scroll   # reversed: lo scrolls down
            cell.blit(img_hi2,  (tx, ty_lo - 2 * slot_h))  # two steps above
            cell.blit(img_hi,   (tx, ty_lo - slot_h))       # hi  (higher) above
            cell.blit(img_lo,   (tx, ty_lo))
            cell.blit(img_prev, (tx, ty_lo + slot_h))       # prev (lower) below
        else:
            ty_lo = (bh - gh) // 2 + scroll   # reversed: lo scrolls down
            cell.blit(img_hi, (tx, ty_lo - bh))   # hi (higher) one slot above
            cell.blit(img_lo, (tx, ty_lo))

        surf.blit(cell, (cx, by))


def _drum_shade(surf, bx, by, bw, bh):
    """Overlay a top-and-bottom fade-to-dark gradient on the drum window."""
    shade = pygame.Surface((bw, bh), pygame.SRCALPHA)
    shade.fill((0, 0, 0, 0))
    fade = bh // 3
    for i in range(fade):
        a = int(210 * (fade - i) / fade)
        pygame.draw.line(shade, (0, 5, 15, a), (0, i),      (bw-1, i))
        pygame.draw.line(shade, (0, 5, 15, a), (0, bh-1-i), (bw-1, bh-1-i))
    surf.blit(shade, (bx, by))


def _rolling_drum_alt20(surf, bx, by, bw, bh, alt, color, font_sz, show_adjacent=False,
                        adj_slot_h=None):
    """
    Altimeter Veeder-Root drum: both digits scroll together in 20-foot steps.
    Labels '00','20','40','60','80' move as a unit.
    """
    _LABELS = ("00", "20", "40", "60", "80")
    f = _get_font(font_sz, bold=True)

    drum_pos = (alt % 100) / 20
    d_lo_idx = int(drum_pos) % 5
    frac     = drum_pos - int(drum_pos)
    slot_h   = ((adj_slot_h if adj_slot_h is not None else bh // 2)
                if show_adjacent else bh)
    scroll   = int(frac * slot_h)

    img_lo = f.render(_LABELS[d_lo_idx], True, color)
    gw = img_lo.get_width()
    gh = img_lo.get_height()
    tx = max(0, (bw - gw) // 2)
    cell = pygame.Surface((bw, bh), pygame.SRCALPHA)

    if show_adjacent:
        d_prev_idx = (d_lo_idx - 1 + 5) % 5
        d_hi_idx   = (d_lo_idx + 1) % 5
        d_hi2_idx  = (d_lo_idx + 2) % 5
        img_prev = f.render(_LABELS[d_prev_idx], True, color)
        img_hi   = f.render(_LABELS[d_hi_idx],   True, color)
        img_hi2  = f.render(_LABELS[d_hi2_idx],  True, color)
        ty_lo    = bh // 2 - gh // 2 + scroll   # reversed: lo scrolls down
        cell.blit(img_hi2,  (tx, ty_lo - 2 * slot_h))  # two steps above
        cell.blit(img_hi,   (tx, ty_lo - slot_h))       # hi  (higher) above
        cell.blit(img_lo,   (tx, ty_lo))
        cell.blit(img_prev, (tx, ty_lo + slot_h))       # prev (lower) below
    else:
        d_hi_idx = (d_lo_idx + 1) % 5
        img_hi = f.render(_LABELS[d_hi_idx], True, color)
        ty_lo = (bh - gh) // 2 + scroll   # reversed: lo scrolls down
        cell.blit(img_hi, (tx, ty_lo - bh))   # hi (higher) one slot above
        cell.blit(img_lo, (tx, ty_lo))

    surf.blit(cell, (bx, by))


def lerp(a, b, t):
    return a + (b - a) * max(0.0, min(1.0, t))


def lerp_col(a, b, t):
    return tuple(int(lerp(a[i], b[i], t)) for i in range(3))


# ── SVT background ────────────────────────────────────────────────────────────
_svt_cache: dict = {}
_svt_frame = 0
SVT_UPDATE_FRAMES = 3   # update terrain every N frames (~10 Hz at 30 fps)


def get_svt_surface(ai_w, ai_h, pitch, roll, hdg, alt, lat, lon):
    """Dispatch to OpenGL or pygame SVT renderer based on config + availability.

    With the OpenGL renderer, we render every frame (no caching) since the
    GPU update is essentially free at the Pi 4's frame rate.  The pygame
    fallback caches every SVT_UPDATE_FRAMES frames to keep up with 30 fps.
    """
    global _svt_frame
    _svt_frame += 1

    use_gl = (SVT_RENDERER == "opengl") and _SVT_GL_AVAILABLE
    if use_gl:
        surf = render_svt_gl(SRTM_DIR, ai_w, ai_h, pitch, roll, hdg, alt, lat, lon)
        if surf is not None:
            return surf
        # Fall through to pygame on render failure

    key = "svt"
    if key not in _svt_cache or _svt_frame % SVT_UPDATE_FRAMES == 0:
        surf = render_svt_pygame(
            SRTM_DIR, ai_w, ai_h, pitch, roll, hdg, alt, lat, lon
        )
        _svt_cache[key] = surf
    return _svt_cache[key]


def draw_ai_background(surf, ai_rect, pitch, roll, hdg, alt, lat, lon):
    """Draw SVT sky/terrain background into ai_rect region of surf."""
    ax, ay, aw, ah = ai_rect
    bg = get_svt_surface(aw, ah, pitch, roll, hdg, alt, lat, lon)
    surf.blit(bg, (ax, ay))


def draw_simple_ai_background(surf, ai_rect, pitch, roll):
    """
    Fallback SVT background (no SRTM tiles loaded).
    Draws sky/ground split directly into ai_rect using polygon fill + clipping.
    No large surface rotation — runs in < 1 ms on Pi Zero 2W.
    """
    ax, ay, aw, ah = ai_rect
    GND_NEAR = ( 80, 110,  40)
    GND_MID  = (120,  85,  38)
    GND_FAR  = ( 70,  50,  25)

    px_per_deg = ah / 48.0   # scale with AI height (10.0 at 480px)
    old_clip   = surf.get_clip()
    surf.set_clip(pygame.Rect(ax, ay, aw, ah))

    cx  = ax + aw // 2
    cy  = ay + ah // 2
    # Pitch up (positive) = nose up = horizon BELOW screen centre.
    pitch_py = int(-pitch * px_per_deg)

    # Horizon passes through (hcx, hcy) tilted by roll
    hcx = cx
    hcy = cy - pitch_py
    roll_rad = math.radians(roll)
    cos_r, sin_r = math.cos(roll_rad), math.sin(roll_rad)

    # Extend horizon line well beyond the rect so clipping takes care of edges.
    # Line direction in pygame Y-down is (cos_r, -sin_r); for positive roll
    # (right bank) that's "right and up" → LEFT-DOWN, RIGHT-UP, matching the
    # pitch-ladder white horizon and the SVT GL terrain horizon.
    R  = aw + ah
    h1 = (hcx - R * cos_r, hcy + R * sin_r)
    h2 = (hcx + R * cos_r, hcy - R * sin_r)

    # Classify each corner relative to the (h1, h2) line.  The implicit
    # equation of that line is sin_r*(px-hcx) + cos_r*(py-hcy) = 0; sky is
    # the side where py is "above" (smaller y in pygame), giving
    #     sky_side(p) = (hcy - py)*cos_r + (hcx - px)*sin_r > 0.
    # NB: the older form used (px - hcx) which flipped the sin term and
    # silently classified points against a different line — visible only at
    # non-zero roll, where the polygon edge and the drawn h1-h2 white line
    # disagreed.
    def _sky_side(px, py):
        return (hcy - py) * cos_r + (hcx - px) * sin_r > 0

    corners = [(ax, ay), (ax + aw, ay), (ax + aw, ay + ah), (ax, ay + ah)]

    # Build sky polygon: traverse rect corners in order, insert horizon
    # intersection points where the boundary crosses from sky to ground or vice versa.
    sky_poly = []
    for i, c in enumerate(corners):
        nc = corners[(i + 1) % 4]
        c_sky  = _sky_side(c[0],  c[1])
        nc_sky = _sky_side(nc[0], nc[1])
        if c_sky:
            sky_poly.append(c)
        if c_sky != nc_sky:
            dx, dy = nc[0] - c[0], nc[1] - c[1]
            denom  = dy * cos_r + dx * sin_r
            if abs(denom) > 1e-6:
                t  = ((hcy - c[1]) * cos_r + (hcx - c[0]) * sin_r) / denom
                sky_poly.append((c[0] + t * dx, c[1] + t * dy))

    # Fill ground first (covers whole rect), then paint sky polygon on top
    surf.fill(GND_MID, (ax, ay, aw, ah))
    if sky_poly and len(sky_poly) >= 3:
        pygame.draw.polygon(surf, SKY_HOR, sky_poly)
    elif all(_sky_side(c[0], c[1]) for c in corners):
        surf.fill(SKY_HOR, (ax, ay, aw, ah))

    # Horizon line (extended; clipped to AI rect by set_clip above)
    pygame.draw.line(surf, WHITE,
                     (int(h1[0]), int(h1[1])), (int(h2[0]), int(h2[1])), 2)

    surf.set_clip(old_clip)


# ── Pitch ladder ──────────────────────────────────────────────────────────────
def draw_zero_pitch_line(surf, ai_rect, pitch, roll):
    """Draw the zero-pitch reference line across the AI.

    The line represents the aircraft's 0° pitch reference — i.e. the
    straight-and-level horizon line in the sky frame.  It moves with pitch
    (drops below AI centre when pitched up, rises when pitched down) and
    rotates with roll, tracking where the "true" horizon would be on a
    flat-earth model.

    Rendered as a pair of hash marks with a gap for the aircraft symbol.
    Cyan to distinguish it from the white terrain horizon of the SVT.
    """
    ax, ay, aw, ah = ai_rect
    cy = ay + ah // 2
    cx = ax + aw // 2
    gap_half = int(aw * 0.20)
    end_half = int(aw * 0.42)

    # Same pitch scale as the pitch ladder so the zero-pitch hash marks
    # line up exactly with the ladder's 0° bar position.
    px_per_deg = ah / 48.0
    pitch_px = int(pitch * px_per_deg)   # + = nose up = line below centre

    # Rotate around (cx, cy) by -roll and offset vertically by pitch_px.
    # Endpoints before rotation lie on a horizontal line at y=pitch_px below cy.
    theta = math.radians(-roll)
    c = math.cos(theta)
    s = math.sin(theta)

    def rot(dx, dy):
        return (cx + int(dx * c - dy * s),
                cy + int(dx * s + dy * c))

    l1 = rot(-end_half, pitch_px); l2 = rot(-gap_half, pitch_px)
    r1 = rot( gap_half, pitch_px); r2 = rot( end_half, pitch_px)
    pygame.draw.line(surf, CYAN, l1, l2, 2)
    pygame.draw.line(surf, CYAN, r1, r2, 2)


def draw_pitch_ladder(surf, ai_rect, pitch, roll):
    """
    White pitch ladder lines drawn directly in rotated coordinates.
    No intermediate surface or transform.rotate — fast on Pi Zero 2W.
    """
    ax, ay, aw, ah = ai_rect
    cx, cy = ax + aw // 2, ay + ah // 2

    px_per_deg = ah / 48.0   # scale with AI height (10.0 at 480px)
    pitch_px   = int(pitch * px_per_deg)

    major_half = int(aw * 0.07)   # ~34 px
    minor_half = int(aw * 0.04)   # ~19 px

    # Precompute rotation basis (pygame CCW rotation in Y-down screen coords):
    #   rotated_x = x * cos_r + y * sin_r
    #   rotated_y = -x * sin_r + y * cos_r
    roll_rad = math.radians(roll)
    cos_r    = math.cos(roll_rad)
    sin_r    = math.sin(roll_rad)

    def _rv(x, y):
        """Rotate vector (x,y) and offset to surf coords."""
        return (int(cx + x * cos_r + y * sin_r),
                int(cy - x * sin_r + y * cos_r))

    # Clip to AI rect so lines don't bleed into tapes / heading tape
    old_clip = surf.get_clip()
    surf.set_clip(pygame.Rect(ax, ay, aw, ah))

    for deg in range(-30, 35, 5):
        rel_y = pitch_px - int(deg * px_per_deg)  # y offset from AI center

        # Cull lines too far from the visible window (±185 px from centre)
        if rel_y < -185 or rel_y > 185:
            continue

        major = (deg % 10 == 0)
        half  = major_half if major else minor_half

        if deg == 0:
            # Horizon line
            p1 = _rv(-half, rel_y)
            p2 = _rv( half, rel_y)
            pygame.draw.line(surf, (255, 255, 255, 200), p1, p2, 4)
            continue

        col = (255, 255, 255, 220)
        p1  = _rv(-half, rel_y)
        p2  = _rv( half, rel_y)

        if major:
            pygame.draw.line(surf, col, p1, p2, 4)
        else:
            pygame.draw.line(surf, col, p1, p2, 2)

        # Tick marks: 8 px inward (toward horizon = toward centre of AI)
        tick = 8 if deg > 0 else -8
        pygame.draw.aaline(surf, col, p1, _rv(-half, rel_y + tick))
        pygame.draw.aaline(surf, col, p2, _rv( half, rel_y + tick))

        # Degree labels at major lines (drawn without rotation for speed)
        if major:
            lbl = str(abs(deg))
            fnt = _get_font(16)
            img = fnt.render(lbl, True, (255, 255, 255))
            # Position label just outside each end of the line
            lx1, ly1 = _rv(-half - img.get_width() - 4, rel_y - 8)
            lx2, ly2 = _rv( half + 4,                   rel_y - 8)
            surf.blit(img, (lx1, ly1))
            surf.blit(img, (lx2, ly2))

    surf.set_clip(old_clip)


# gfxdraw.filled_polygon and gfxdraw.aapolygon both fail to write
# per-pixel alpha on SRCALPHA surfaces — fills/outlines come out invisible
# on the shared-GL composite surface. pygame.draw.polygon writes the fill
# at alpha=255 correctly. aa=True adds a pygame.draw.aalines pass for
# anti-aliased edges; on small markers (doghouses, etc.) that AA pass
# bleeds outward as a visible halo, so callers that prefer hard edges
# pass aa=False.
def _filled_polygon(surf, points, color, aa=True):
    pygame.draw.polygon(surf, color, points)
    if aa:
        pygame.draw.aalines(surf, color, True, points)


# ── Roll arc ──────────────────────────────────────────────────────────────────
def _doghouse_pts(cx, cy, ang_rad, r, size=11, inward=True):
    """
    Pentagon 'doghouse' pointer at radius r.
    inward=True : tip at r points toward centre (used outside the arc).
    inward=False: tip at r points away from centre (used inside the arc).
    """
    out_x  = math.cos(ang_rad);  out_y  = math.sin(ang_rad)
    perp_x = -out_y;             perp_y =  out_x
    if inward:
        tip_r  = r
        base_r = r + size * 1.3
        roof_r = r + size * 0.6
    else:
        tip_r  = r
        base_r = r - size * 1.3
        roof_r = r - size * 0.6
    half_w  = size * 0.7
    roof_hw = size * 0.35
    return [
        (int(cx + base_r * out_x - half_w  * perp_x),
         int(cy + base_r * out_y - half_w  * perp_y)),
        (int(cx + roof_r * out_x - roof_hw * perp_x),
         int(cy + roof_r * out_y - roof_hw * perp_y)),
        (int(cx + tip_r  * out_x), int(cy + tip_r  * out_y)),
        (int(cx + roof_r * out_x + roof_hw * perp_x),
         int(cy + roof_r * out_y + roof_hw * perp_y)),
        (int(cx + base_r * out_x + half_w  * perp_x),
         int(cy + base_r * out_y + half_w  * perp_y)),
    ]


def draw_roll_arc(surf, roll):
    """Draw GI-275 style roll scale: arc, tick marks, doghouse zero marker,
    and doghouse roll pointer.
    Uses pygame.draw.arc (single C call) instead of 121-iteration Python loop.
    """
    cx, cy = CX, ROLL_CY

    # ── Arc: 120° span centred at 12 o'clock, rotated by roll ────────────────
    # Drawn as a single thick polyline along the centreline radius (not a
    # band polygon): a band gets a weird double-AA halo when run through
    # _filled_polygon (aalines traces both inner and outer edges), whereas
    # a thick line is one clean stroke.
    _ARC_STEPS = 80
    _ARC_THICK = 4  # pixels of arc band thickness
    arc_pts = []
    for i in range(_ARC_STEPS + 1):
        # Sky-pointer design: arc rotates WITH the sky/horizon so the fixed
        # aircraft reference at the top of the screen reads the current bank.
        # In pygame Y-down, right bank (positive roll) rotates the sky CCW
        # visually, which means pygame angles DECREASE (hence -roll).
        ang = (-90 - roll - 60 + i * 120.0 / _ARC_STEPS) * DEG
        cos_a, sin_a = math.cos(ang), math.sin(ang)
        arc_pts.append((int(cx + ROLL_R * cos_a),
                        int(cy + ROLL_R * sin_a)))
    pygame.draw.lines(surf, WHITE, False, arc_pts, _ARC_THICK)

    # ── Tick marks — rotate with sky, solid white, 2px width ─────────────────
    for deg2, length in [(10, 9), (20, 9), (30, 13),
                         (-10, 9), (-20, 9), (-30, 13),
                         (45, 9), (-45, 9), (60, 11), (-60, 11)]:
        ang = (-90 - roll + deg2) * DEG   # rotate with the sky
        cos_a, sin_a = math.cos(ang), math.sin(ang)
        x1 = int(cx + (ROLL_R - length) * cos_a)
        y1 = int(cy + (ROLL_R - length) * sin_a)
        x2 = int(cx + (ROLL_R + _ARC_THICK) * cos_a)
        y2 = int(cy + (ROLL_R + _ARC_THICK) * sin_a)
        pygame.draw.line(surf, WHITE, (x1, y1), (x2, y2), 2)
        # Hollow triangles at ±45
        if abs(deg2) == 45:
            perp = ang + math.pi / 2
            tx2, ty2 = int(5 * math.cos(perp)), int(5 * math.sin(perp))
            mx, my = (x1 + x2) // 2, (y1 + y2) // 2
            inner_x = int(cx + (ROLL_R - 16) * cos_a)
            inner_y = int(cy + (ROLL_R - 16) * sin_a)
            tri = [(mx - tx2, my - ty2), (mx + tx2, my + ty2), (inner_x, inner_y)]
            pygame.gfxdraw.aapolygon(surf, tri, LTGREY)

    # Moving upper doghouse — OUTSIDE arc, tip at arc, rotates with the arc
    # (sky pointer).  At right bank it moves to upper-left (same direction
    # as the arc's 0° tick).
    upper_ang = (-90 - roll) * DEG
    tri0 = _doghouse_pts(cx, cy, upper_ang, ROLL_R + 2, size=10, inward=True)
    _filled_polygon(surf, tri0, WHITE, aa=False)

    # Fixed lower doghouse — INSIDE arc, tip at arc-8, fixed at 12 o'clock
    roll_ang = -math.pi / 2
    rp_pts = _doghouse_pts(cx, cy, roll_ang, ROLL_R - 8, size=10, inward=False)
    _filled_polygon(surf, rp_pts, WHITE, aa=False)


# ── Aircraft symbol ───────────────────────────────────────────────────────────
AMBER      = (255, 190,  30)   # slightly warmer than YELLOW for symbol fill
AMBER_DARK = (180, 120,   0)   # shadow/outline

def draw_aircraft_symbol(surf):
    """Swept delta wing aircraft reference with engine nacelles, 1.5× scale."""
    # Wing panels — apex at (CX, CY), trailing edge at CY+44 (1.5× original 29)
    # Outer strip = leading-edge side (lighter/top); Inner strip = trailing-edge side (darker/bottom)
    # Fills — inner/outer strips, no outline so colour-split edge stays clean
    # Inner edge moved ±69 → ±57 (50% wider base; outer edge ±93 unchanged)
    # Bisect at ±75 = midpoint of ±57..±93, giving equal-width inner/outer strips
    li = [(CX, CY), (CX - 75, CY + 44), (CX - 57, CY + 44)]   # L inner (darker)
    lo = [(CX, CY), (CX - 93, CY + 44), (CX - 75, CY + 44)]   # L outer (lighter)
    ri = [(CX, CY), (CX + 57, CY + 44), (CX + 75, CY + 44)]   # R inner (darker)
    ro = [(CX, CY), (CX + 75, CY + 44), (CX + 93, CY + 44)]   # R outer (lighter)
    _filled_polygon(surf, li, AMBER_DARK)
    pygame.gfxdraw.aapolygon(surf, li, AMBER_DARK)
    _filled_polygon(surf, lo, AMBER)
    pygame.gfxdraw.aapolygon(surf, lo, AMBER)
    _filled_polygon(surf, ri, AMBER_DARK)
    pygame.gfxdraw.aapolygon(surf, ri, AMBER_DARK)
    _filled_polygon(surf, ro, AMBER)
    pygame.gfxdraw.aapolygon(surf, ro, AMBER)

    # Engine nacelles — fills
    lu = [(CX - 93, CY), (CX - 99, CY - 6), (CX - 138, CY - 6), (CX - 138, CY)]
    ll = [(CX - 93, CY), (CX - 138, CY),    (CX - 138, CY + 6), (CX - 99, CY + 6)]
    ru = [(CX + 93, CY), (CX + 99, CY - 6), (CX + 138, CY - 6), (CX + 138, CY)]
    rl = [(CX + 93, CY), (CX + 138, CY),    (CX + 138, CY + 6), (CX + 99, CY + 6)]
    _filled_polygon(surf, lu, AMBER)
    pygame.gfxdraw.aapolygon(surf, lu, AMBER)
    _filled_polygon(surf, ll, AMBER_DARK)
    pygame.gfxdraw.aapolygon(surf, ll, AMBER_DARK)
    _filled_polygon(surf, ru, AMBER)
    pygame.gfxdraw.aapolygon(surf, ru, AMBER)
    _filled_polygon(surf, rl, AMBER_DARK)
    pygame.gfxdraw.aapolygon(surf, rl, AMBER_DARK)

    # Outer perimeter outlines — no line across the inner colour-split edge
    BLK = (0, 0, 0)
    lw = [(CX, CY), (CX - 93, CY + 44), (CX - 57, CY + 44)]
    rw = [(CX, CY), (CX + 57, CY + 44), (CX + 93, CY + 44)]
    ln = [(CX - 93, CY), (CX - 99, CY - 6), (CX - 138, CY - 6), (CX - 138, CY + 6), (CX - 99, CY + 6)]
    rn = [(CX + 93, CY), (CX + 99, CY - 6), (CX + 138, CY - 6), (CX + 138, CY + 6), (CX + 99, CY + 6)]
    pygame.gfxdraw.aapolygon(surf, lw, BLK)
    pygame.gfxdraw.aapolygon(surf, rw, BLK)
    pygame.gfxdraw.aapolygon(surf, ln, BLK)
    pygame.gfxdraw.aapolygon(surf, rn, BLK)


# ── Slip/skid indicator ───────────────────────────────────────────────────────
def draw_slip_ball(surf, ay):
    """Slip indicator: thin bar that slides under the fixed zero-bank triangle."""
    slip_y = ROLL_CY - ROLL_R + 24  # below lower doghouse base (y≈40)
    max_d  = 12
    defl   = int(max(-max_d, min(max_d, (ay / 0.2) * max_d)))
    pygame.draw.rect(surf, WHITE, (CX + defl - 8, slip_y, 16, 4))


# ── Speed tape ────────────────────────────────────────────────────────────────
PX_PER_KT  = TAPE_H / 120.0   # 120 kt visible range
PX_PER_FT  = TAPE_H / 600.0   # 600 ft visible range
PX_PER_DEG = DISPLAY_W / 120.0  # 120° visible heading range (half spacing)


def spd_y(v, speed): return int(TAPE_MID - (v - speed) * PX_PER_KT)
def alt_y(ft, alt):  return int(TAPE_MID - (ft - alt)  * PX_PER_FT)


_spd_tape_bg = None   # cached speed-tape background surface
_alt_tape_bg = None   # cached alt-tape background surface


def draw_speed_tape(surf, speed, gs_bug=None,
                    vs0=VS0, vs1=VS1, vfe=VFE, vno=VNO, vne=VNE,
                    airspeed_src="gps"):
    """Left airspeed tape with GI-275-style V-speed colour bands.
    V-speed params should already be in the same unit as *speed*."""
    # Background — cached to avoid a new SRCALPHA Surface allocation every frame
    global _spd_tape_bg
    if _spd_tape_bg is None:
        _spd_tape_bg = pygame.Surface((SPD_W, TAPE_BOT), pygame.SRCALPHA)
        _spd_tape_bg.fill(TAPE_BG)
    surf.blit(_spd_tape_bg, (SPD_X, 0))
    pygame.draw.line(surf, (255, 255, 255, 60), (SPD_X + SPD_W, 0),
                     (SPD_X + SPD_W, TAPE_BOT), 1)

    def sy(v): return spd_y(v, speed)

    # V-speed colour bands (right edge of tape)
    def _band(v_lo, v_hi, col, bar_x, bar_w=4):
        y1 = sy(v_hi)
        y2 = sy(v_lo)
        y1c = max(TAPE_TOP, min(TAPE_BOT, y1))
        y2c = max(TAPE_TOP, min(TAPE_BOT, y2))
        if y1c < y2c:
            pygame.draw.rect(surf, col, (bar_x, y1c, bar_w, y2c - y1c))

    # White arc: Vs0 – Vfe  (flap range)
    _band(vs0, vfe, WHITE, SPD_X + SPD_W - 10, 3)
    # Green arc: Vs1 – Vno  (normal ops)
    _band(vs1, vno, GREEN_ARC, SPD_X + SPD_W - 5, 4)
    # Yellow arc: Vno – Vne (caution)
    _band(vno, vne, YELLOW_ARC, SPD_X + SPD_W - 5, 4)
    # Red Vne line
    vne_y = sy(vne)
    if TAPE_TOP < vne_y < TAPE_BOT:
        pygame.draw.line(surf, RED, (SPD_X + SPD_W - 16, vne_y),
                         (SPD_X + SPD_W, vne_y), 3)

    # Tick marks and numbers
    base = int(round(speed / 20)) * 20
    for v in range(base - 100, base + 100, 10):
        if v < 0: continue
        vy = sy(v)
        if not (TAPE_TOP + 15 < vy < TAPE_BOT - 15):
            continue
        major = (v % 20 == 0)
        tl = 12 if major else 7
        pygame.draw.line(surf, LTGREY,
                         (SPD_X, vy), (SPD_X + tl, vy),
                         2 if major else 1)
        if major:
            _text(surf, str(v), 17, (230, 230, 230), bold=True,
                  x=SPD_X + tl + 2, y=vy - 9)

    # GS/IAS bug — color reflects source: magenta=GPS groundspeed, cyan=IAS sensor.
    if gs_bug is not None:
        gby = max(TAPE_TOP, min(TAPE_BOT, spd_y(gs_bug, speed)))
        gb = [(SPD_X,      gby - 17),
              (SPD_X + 14, gby - 17), (SPD_X + 14, gby - 5), (SPD_X + 7, gby),
              (SPD_X + 14, gby + 5),  (SPD_X + 14, gby + 17), (SPD_X, gby + 17)]
        spd_bug_col = MAGENTA if airspeed_src == "gps" else CYAN
        surf.set_clip((0, TAPE_TOP, DISPLAY_W, TAPE_BOT - TAPE_TOP))
        pygame.draw.polygon(surf, spd_bug_col, gb)
        surf.set_clip(None)

    # Speed readout box — stepped Veeder-Root style.
    # Scale widths with font so digits fit on 1024×600 (FONT_SCALE≈1.25).
    _sp = SPD_X
    _fs = getattr(sys.modules[__name__], '_font_scale', 1.0)
    _ptr_r  = int(18 * _fs)
    _inn_w  = int(35 * _fs)
    _drm_sw = int(18 * _fs)
    _inn_r  = _ptr_r + _inn_w
    _box_r  = _inn_r + _drm_sw
    _half_in = int(16 * _fs)
    _half_out = int(30 * _fs)
    pts_s = _chamfer([(_sp,          TAPE_MID),
                      (_sp + _ptr_r, TAPE_MID - _half_in), (_sp + _inn_r, TAPE_MID - _half_in),
                      (_sp + _inn_r, TAPE_MID - _half_out), (_sp + _box_r, TAPE_MID - _half_out),
                      (_sp + _box_r, TAPE_MID + _half_out),
                      (_sp + _inn_r, TAPE_MID + _half_out), (_sp + _inn_r, TAPE_MID + _half_in),
                      (_sp + _ptr_r, TAPE_MID + _half_in)], {2, 3, 4, 5, 6, 7}, r=3)
    _filled_polygon(surf, pts_s, (0, 10, 30))
    spd_col = RED if speed > vne else (YELLOW if speed > vno else WHITE)
    _rolling_drum(surf, _sp + _ptr_r + 1, TAPE_MID - _half_in + 1, _inn_w - 2, _half_in * 2 - 2, speed, 2, spd_col, 24,
                  power_offset=1, suppress_leading=True)
    _rolling_drum(surf, _sp + _inn_r + 1, TAPE_MID - _half_out + 1, _drm_sw - 2, _half_out * 2 - 2, speed, 1, spd_col, 24,
                  show_adjacent=True, adj_slot_h=int(23 * _fs))
    _drum_shade(surf, _sp + _inn_r + 1, TAPE_MID - _half_out + 1, _drm_sw - 2, _half_out * 2 - 2)
    # Border drawn LAST so drum shade doesn't cover the inner pixels
    pygame.draw.polygon(surf, WHITE, pts_s, width=2)
    pygame.gfxdraw.aapolygon(surf, pts_s, WHITE)

    # GS bug button — top strip of speed tape; color matches bug triangle
    gs_str = f"{round(gs_bug):3d}" if gs_bug is not None else "---"
    spd_box_col = MAGENTA if airspeed_src == "gps" else CYAN
    _cyan_box(surf, gs_str, x=SPD_X, y=0, w=SPD_W, h=TAPE_TOP, col=spd_box_col)


# ── Altitude tape ──────────────────────────────────────────────────────────────
def draw_alt_tape(surf, alt, vspeed, baro_hpa, baro_src, alt_bug=None, baro_ok=True):
    """Right altitude tape with VSI and baro setting."""
    global _alt_tape_bg
    if _alt_tape_bg is None:
        _alt_tape_bg = pygame.Surface((ALT_W, TAPE_BOT), pygame.SRCALPHA)
        _alt_tape_bg.fill(TAPE_BG)
    surf.blit(_alt_tape_bg, (ALT_X, 0))
    pygame.draw.line(surf, (255, 255, 255, 60), (ALT_X, 0),
                     (ALT_X, TAPE_BOT), 1)

    def ay2(ft): return alt_y(ft, alt)

    # Tick marks and numbers — every 50ft minor, every 100ft major with label
    base = int(round(alt / 50)) * 50
    for ft in range(base - 450, base + 450, 50):
        fy = ay2(ft)
        if not (TAPE_TOP + 8 < fy < TAPE_BOT - 8):
            continue
        major = (ft % 100 == 0)
        tl = 12 if major else 7
        pygame.draw.line(surf, LTGREY,
                         (ALT_X + ALT_W - tl, fy), (ALT_X + ALT_W, fy),
                         2 if major else 1)
        if major:
            s = str(ft)
            if ft >= 1000:
                # Thousands digit slightly larger than hundreds+
                f_l = _get_font(16, bold=True)
                f_s = _get_font(13, bold=True)
                thou, rest = s[:1], s[1:]
                tw_l = f_l.size(thou)[0]
                tw_s = f_s.size(rest)[0]
                x0 = ALT_X + ALT_W - tl - 2 - tw_l - tw_s
                _text(surf, thou, 16, (230, 230, 230), bold=True, x=x0,        y=fy - 10)
                _text(surf, rest, 13, (230, 230, 230), bold=True, x=x0 + tw_l, y=fy -  8)
            else:
                lw = _get_font(13, bold=True).size(s)[0]
                _text(surf, s, 13, (230, 230, 230), bold=True,
                      x=ALT_X + ALT_W - tl - 2 - lw, y=fy - 8)

    # ALT bug button — top strip of alt tape; color matches bug triangle
    alt_str = f"{round(alt_bug):5d}" if alt_bug is not None else "-----"
    alt_box_col = CYAN if baro_ok else MAGENTA
    _cyan_box(surf, alt_str, x=ALT_X + 1, y=0, w=ALT_W - 1, h=TAPE_TOP, col=alt_box_col)

    # VS bar — 5px wide on the outer (right) edge of the alt tape.
    # Visible whenever climbing/descending; covered by alt bug only when at bug altitude.
    # 2000 fpm ≡ 200 ft on the tape scale.
    _vs_scale = 200 * PX_PER_FT / 2000   # px per fpm
    _vs_px    = int(abs(vspeed) * _vs_scale)
    if abs(vspeed) > 30 and _vs_px > 0:
        if vspeed > 0:
            _vsy1 = max(TAPE_TOP, TAPE_MID - _vs_px)
            _vsy2 = TAPE_MID
        else:
            _vsy1 = TAPE_MID
            _vsy2 = min(TAPE_BOT, TAPE_MID + _vs_px)
        pygame.draw.rect(surf, MAGENTA, (ALT_X + ALT_W - 5, _vsy1, 5, _vsy2 - _vsy1))

    # Altitude bug — color reflects source: cyan=baro/pressure transducer, magenta=GPS alt (baro failed).
    if alt_bug is not None:
        aby = max(TAPE_TOP, min(TAPE_BOT, ay2(alt_bug)))
        bug = [(ALT_X + ALT_W,      aby - 17),
               (ALT_X + ALT_W - 14, aby - 17), (ALT_X + ALT_W - 14, aby - 5), (ALT_X + ALT_W - 7, aby),
               (ALT_X + ALT_W - 14, aby + 5),  (ALT_X + ALT_W - 14, aby + 17), (ALT_X + ALT_W, aby + 17)]
        alt_bug_col = CYAN if baro_ok else MAGENTA
        surf.set_clip((0, TAPE_TOP, DISPLAY_W, TAPE_BOT - TAPE_TOP))
        pygame.draw.polygon(surf, alt_bug_col, bug)
        surf.set_clip(None)

    # Altitude readout box — stepped Veeder-Root style, scaled with FONT_SCALE.
    _fs = getattr(sys.modules[__name__], '_font_scale', 1.0)
    R = ALT_X + ALT_W
    _ptr_w  = int(18 * _fs)
    _drm_w  = int(31 * _fs)
    _inn_w  = int(47 * _fs)
    _box_w  = _inn_w + _drm_w + _ptr_w
    _drm_l  = _ptr_w + _drm_w
    _half_in = int(16 * _fs)
    _half_out = int(30 * _fs)
    pts_a = _chamfer([(R,              TAPE_MID),
                      (R - _ptr_w,     TAPE_MID - _half_in), (R - _ptr_w, TAPE_MID - _half_out),
                      (R - _drm_l,     TAPE_MID - _half_out), (R - _drm_l, TAPE_MID - _half_in),
                      (R - _box_w,     TAPE_MID - _half_in),
                      (R - _box_w,     TAPE_MID + _half_in),
                      (R - _drm_l,     TAPE_MID + _half_in), (R - _drm_l, TAPE_MID + _half_out),
                      (R - _ptr_w,     TAPE_MID + _half_out), (R - _ptr_w, TAPE_MID + _half_in)], {2, 3, 4, 5, 6, 7, 8, 9}, r=3)
    _filled_polygon(surf, pts_a, (0, 10, 30))

    # VSI readout — extends left beyond the tape if the text needs room
    _ny   = TAPE_MID + _half_in
    _nh   = int(22 * _fs)
    if abs(vspeed) > 30:
        _varr = "▲" if vspeed > 0 else "▼"
        _vstr = f"{_varr}{abs(vspeed)/1000:.1f}"
        _vcol = (0, 220, 0) if vspeed > 0 else (255, 140, 0)
    else:
        _vstr = "—"
        _vcol = LTGREY
    _vf = _get_font(13, bold=True)
    _vtw = _vf.size(_vstr)[0] + 12
    _vsi_min_w = R - _drm_l - ALT_X
    _nw = max(_vsi_min_w, _vtw)
    _nx = R - _drm_l - _nw
    pygame.draw.rect(surf, (0, 8, 22), (_nx, _ny, _nw, _nh), border_radius=3)
    pygame.draw.rect(surf, (70, 100, 130), (_nx, _ny, _nw, _nh), width=1, border_radius=3)
    _text(surf, _vstr, 13, _vcol, bold=True, cx=_nx + _nw // 2, cy=_ny + _nh // 2)

    # Inner: cascade from drum; carry starts when drum_pos > 4 (last 20 ft before rollover)
    carry_frac = max(0.0, (alt % 100) / 20 - 4.0)
    alt_inner  = float(alt // 100) + carry_frac
    # round() (not int()) so IIR-smoothed alt of e.g. 9999.99 still
    # picks the 3-drum branch that renders the leading "1" at 10000.
    inner_int  = round(alt_inner)
    _cell = int(16 * _fs)
    _inn_right = R - _drm_l
    _inn_h = _half_in * 2 - 2
    if inner_int < 10:
        _rolling_drum(surf, _inn_right - _cell, TAPE_MID - _half_in + 1, _cell, _inn_h, alt_inner, 1, WHITE, 24)
    elif inner_int < 100:
        _rolling_drum(surf, _inn_right - _cell * 2, TAPE_MID - _half_in + 1, _cell, _inn_h, alt_inner, 1, WHITE, 24,
                      power_offset=1)
        _rolling_drum(surf, _inn_right - _cell, TAPE_MID - _half_in + 1, _cell, _inn_h, alt_inner, 1, WHITE, 22)
    else:
        _rolling_drum(surf, _inn_right - _cell * 3, TAPE_MID - _half_in + 1, _cell * 2, _inn_h, alt_inner, 2, WHITE, 22,
                      suppress_leading=True, power_offset=1)
        _rolling_drum(surf, _inn_right - _cell, TAPE_MID - _half_in + 1, _cell, _inn_h, alt_inner, 1, WHITE, 22)
    _drm_x = R - _drm_l + 1
    _drm_render_w = _drm_w - 2
    _drm_h = _half_out * 2 - 2
    _rolling_drum_alt20(surf, _drm_x, TAPE_MID - _half_out + 1, _drm_render_w, _drm_h, alt, WHITE, 18,
                        show_adjacent=True, adj_slot_h=int(18 * _fs))
    _drum_shade(surf, _drm_x, TAPE_MID - _half_out + 1, _drm_render_w, _drm_h)
    # Border drawn LAST so drum shade doesn't cover the inner pixels
    pygame.draw.polygon(surf, WHITE, pts_a, width=2)
    pygame.gfxdraw.aapolygon(surf, pts_a, WHITE)


# ── Heading tape ──────────────────────────────────────────────────────────────
_CARDINALS = {0: "N", 45: "NE", 90: "E", 135: "SE",
              180: "S", 225: "SW", 270: "W", 315: "NW"}


def draw_heading_tape(surf, hdg, hdg_bug=None, track=None, yaw=None,
                      gps_ok=False, ahrs_ok=True, use_track=False,
                      hdg_label="M", hdg_color=WHITE):
    """Bottom heading strip with bug, current-heading box, and a
    cross-source diamond showing the *other* heading source's value.

    Args:
        hdg:        the heading value to display in the box (track or yaw)
        track:      raw GPS track (deg)        — used for cross-source pointer
        yaw:        raw AHRS magnetic yaw (deg) — used for cross-source pointer
        use_track:  True if the active source is GPS track
        hdg_label:  subscript "M" / "G" / "M?" / "G?" / "?"
        hdg_color:  WHITE for valid mag, MAGENTA for valid track, AMBER for ?
    """
    hdg_surf = pygame.Surface((DISPLAY_W, HDG_H), pygame.SRCALPHA)
    hdg_surf.fill((0, 8, 22, 210))
    surf.blit(hdg_surf, (0, HDG_Y))
    pygame.draw.line(surf, (255, 255, 255, 80), (0, HDG_Y), (DISPLAY_W, HDG_Y), 1)

    # Tick marks
    for i in range(-70, 71):
        deg = int((round(hdg) + i + 3600)) % 360
        off = i - (hdg - round(hdg))
        x = int(CX + off * PX_PER_DEG)
        if not (0 < x < DISPLAY_W):
            continue
        if deg % 5 == 0:
            th = int(HDG_H * (0.35 if deg % 10 == 0 else 0.18))
            pygame.draw.line(surf, (200, 200, 200),
                             (x, HDG_Y), (x, HDG_Y + th),
                             2 if deg % 10 == 0 else 1)
        if deg % 10 == 0:
            lbl = _CARDINALS.get(deg, str(deg // 10))
            col = YELLOW if deg in _CARDINALS else (230, 230, 230)
            _text(surf, lbl, 17, col, bold=True, cx=x, cy=HDG_Y + HDG_H * 3 // 4)

    # Heading bug chevron — color matches the active source
    if hdg_bug is not None:
        off = ((hdg_bug - hdg + 180) % 360) - 180
        hbx = int(CX + off * PX_PER_DEG)
        hbx = max(SPD_W, min(ALT_X, hbx))   # clamp to inner edges of tap buttons
        bug = [(hbx - 17, HDG_Y + 14), (hbx - 17, HDG_Y),
               (hbx - 5,  HDG_Y), (hbx, HDG_Y + 7), (hbx + 5, HDG_Y),
               (hbx + 17, HDG_Y), (hbx + 17, HDG_Y + 14)]
        hdg_bug_col = MAGENTA if use_track else CYAN
        _filled_polygon(surf, bug, hdg_bug_col)
        pygame.gfxdraw.aapolygon(surf, bug, hdg_bug_col)

    # Cross-source pointer — when the OTHER source is also valid, mark its
    # value on the tape so the pilot can see the discrepancy (wind correction
    # angle, mag variation, compass drift) without having to flip modes.
    # Cyan triangle = magnetic; magenta triangle = GPS track.
    cross_val = None
    cross_col = None
    if use_track and ahrs_ok and yaw is not None:
        cross_val = yaw
        cross_col = CYAN
    elif (not use_track) and gps_ok and track is not None:
        cross_val = track
        cross_col = MAGENTA
    if cross_val is not None:
        off = ((cross_val - hdg + 180) % 360) - 180
        if abs(off) > 1.0:   # avoid clutter when sources agree
            tx = int(CX + off * PX_PER_DEG)
            if 0 < tx < DISPLAY_W:
                tri = [(tx, HDG_Y + 2),
                       (tx - 8, HDG_Y + 18), (tx + 8, HDG_Y + 18)]
                _filled_polygon(surf, tri, cross_col)
                pygame.draw.polygon(surf, WHITE, tri, 1)

    # Heading box — colour reflects active source.
    hdg_col = hdg_color
    # Measure actual rendered width of "133°" to size the box
    _hf = _get_font(17)
    _hdg_str = f"{round(hdg) % 360:03d}\u00b0"
    _hw = _hf.size(_hdg_str)[0]
    # Subscript can be 1 char ("M"/"G") or 2 chars ("M?"/"G?"); pad accordingly.
    sub_pad = 22 if len(hdg_label) > 1 else 14
    bw = max(66, _hw + sub_pad + 14)
    bh = max(28, _hf.get_height() + 8)
    bx, by2 = CX - bw // 2, HDG_Y - bh - 2
    th = bw // 3
    td = 14
    tx = CX - th // 2
    pts_h = _chamfer([(bx,      by2),
                      (bx + bw, by2),
                      (bx + bw, by2 + bh),
                      (tx + th, by2 + bh),
                      (CX,      by2 + bh + td),
                      (tx,      by2 + bh),
                      (bx,      by2 + bh)], {0, 1, 2, 6}, r=3)
    _filled_polygon(surf, pts_h, (0, 0, 0))
    pygame.draw.polygon(surf, hdg_col, pts_h, width=2)
    pygame.gfxdraw.aapolygon(surf, pts_h, hdg_col)
    # Three-digit readout — centred in the box
    _text(surf, _hdg_str, 17, hdg_col, cx=CX, cy=by2 + bh // 2)
    # Source subscript ("M" / "G" / "M?" / "G?" / "?") — outboard of ° glyph
    deg_right = CX + _hw // 2 + 3
    _text(surf, hdg_label, 8, hdg_col, x=deg_right, y=by2 + bh - 12)


# ── Terrain / obstacle proximity alert ───────────────────────────────────────
# alert_level: 0 = none, 1 = caution (amber), 2 = warning (red flash)
_terrain_alert_level = 0


def _alert_radius_nm(speed_kt: float) -> float:
    """
    Compute obstacle alert radius from current airspeed.
    radius = speed × ALERT_TIME_S, clamped to [MIN, MAX].
    Gives a constant time-to-obstacle regardless of airspeed.
    """
    dyn = speed_kt * ALERT_TIME_S / 3600.0
    return max(ALERT_RADIUS_MIN_NM, min(ALERT_RADIUS_MAX_NM, dyn))


def _update_terrain_alert(lat, lon, alt_ft, speed_kt, gps_ok):
    """
    Compute the current terrain/obstacle alert level and store it globally.
    Called once per render frame with current aircraft position and airspeed.
      0 — no alert
      1 — CAUTION  (clearance < TERRAIN_CAUTION_FT or obstacle < OBSTACLE_CAUTION_FT)
      2 — WARNING  (clearance < TERRAIN_WARNING_FT or obstacle < OBSTACLE_WARNING_FT)
    """
    global _terrain_alert_level
    if not gps_ok:
        _terrain_alert_level = 0
        return

    level = 0

    # ── Terrain clearance (sampled at current position) ──────────────────────
    if _has_terrain:
        elev = get_elevation_ft(SRTM_DIR, lat, lon)
        clearance = alt_ft - elev
        if clearance < TERRAIN_WARNING_FT:
            level = max(level, 2)
        elif clearance < TERRAIN_CAUTION_FT:
            level = max(level, 1)

    # ── Obstacle clearance (time-based lookahead radius) ─────────────────────
    if _obstacles is not None:
        radius = _alert_radius_nm(speed_kt)
        nearby = obs_mod.query_nearby(_obstacles, lat, lon,
                                      radius_nm=radius,
                                      alt_ft=alt_ft,
                                      below_ft=OBSTACLE_CAUTION_FT)
        if len(nearby) > 0:
            # Vectorised clearance check — nearby is a structured array.
            clearance = alt_ft - nearby["msl_ft"]
            if (clearance < OBSTACLE_WARNING_FT).any():
                level = max(level, 2)
            elif (clearance < OBSTACLE_CAUTION_FT).any():
                level = max(level, 1)

    _terrain_alert_level = level


def draw_terrain_alert(surf):
    """
    Draw the TERRAIN / PULL UP alert banner in the centre of the badge strip
    (y = 0..22, same row as the status badges).  Level 2 flashes at 1 Hz.
    """
    level = _terrain_alert_level
    if level == 0:
        return

    # Flash at 1 Hz for WARNING (level 2): on for 500 ms, off for 500 ms
    if level == 2:
        if (pygame.time.get_ticks() // 500) % 2 == 1:
            return  # off phase — nothing drawn

    # Banner dimensions — centred in the AI strip, above pitch ladder
    bw = 140; bh = 16
    bx = CX - bw // 2
    by = 3

    if level == 2:
        bg  = (180, 0, 0)
        fg  = (255, 255, 255)
        lbl = "PULL UP"
        sub = "TERRAIN"
    else:
        bg  = (160, 110, 0)
        fg  = (255, 235, 0)
        lbl = "TERRAIN"
        sub = "CAUTION"

    # Filled rounded rectangle
    pygame.draw.rect(surf, bg, (bx, by, bw, bh), border_radius=3)
    pygame.draw.rect(surf, fg, (bx, by, bw, bh), width=1, border_radius=3)

    # Two-word label: primary left, secondary right.  Nudged up 2 px so
    # the descenders don't kiss the lower edge of the rounded rect.
    _text(surf, lbl, 11, fg, bold=True, x=bx + 6, y=by)
    _text(surf, sub, 9,  fg, bold=False, x=bx + bw - 52, y=by + 2)


# ── Status badges ─────────────────────────────────────────────────────────────
def draw_status_badges(surf, ahrs_ok, gps_ok, baro_ok, baro_src, sats, connected,
                       use_track=False):
    """
    Badges are shown only when something requires pilot attention.
    Nominal state = clean strip.  Problem state = badge appears.

    Left  (from AI_X): AHRS FAIL, NO LINK, NO TER, NO OBS, EXP OBS, NO APT, EXP APT
    Right (to ALT_X):  GPS TRK (info), GPS ALT (only when baro absent),
                       GPS Xsat (acquiring), NO GPS (absent)
    """
    f10 = _get_font(10)

    # ── Left badges: problems only ──────────────────────────────────────────
    bx = AI_X + 4
    def badge_l(text, bg, fg=(255, 255, 255)):
        nonlocal bx
        w = f10.size(text)[0] + 10
        pygame.draw.rect(surf, bg, (bx, 4, w, 15))
        _text(surf, text, 10, fg, x=bx + 5, y=5)
        bx += w + 2

    if not ahrs_ok:
        badge_l("AHRS FAIL", (150, 0, 0))
    if not connected:
        badge_l("NO LINK", (130, 0, 0))

    # Data-availability — only shown when something is missing/stale
    _AMBER = (130, 90, 0)
    if not _has_terrain:
        badge_l("NO TER", _AMBER, (220, 180, 60))

    od = disp["od"]
    if od.get("records", 0) == 0:
        badge_l("NO OBS", _AMBER, (220, 180, 60))
    elif od.get("expired", False):
        badge_l("EXP OBS", (120, 55, 0), (255, 160, 40))

    ad = disp["ad"]
    if ad.get("records", 0) == 0:
        badge_l("NO APT", _AMBER, (220, 180, 60))
    elif ad.get("expired", False):
        badge_l("EXP APT", (120, 55, 0), (255, 160, 40))

    # ── Right badges: problems only ─────────────────────────────────────────
    rx = ALT_X - 4
    def badge_r(text, bg, fg=(255, 255, 255)):
        nonlocal rx
        w = f10.size(text)[0] + 10
        rx -= w + 2
        pygame.draw.rect(surf, bg, (rx, 4, w, 15))
        _text(surf, text, 10, fg, x=rx + 5, y=5)

    # GPS-slaved heading mode indicator — magenta badge (matches track-pointer colour)
    if use_track and gps_ok:
        badge_r("GPS TRK", (70, 0, 70), (220, 80, 220))

    # Show GPS ALT only when baro sensor is absent (pilot needs to know alt source)
    if not baro_ok:
        badge_r("GPS ALT", (80, 80, 0), (220, 220, 100))

    # GPS state:
    #   fix valid          → no badge (clean)
    #   satellites visible → amber sat-count (acquiring, no fix yet)
    #   no satellites      → red NO GPS (hardware absent / no signal)
    if not gps_ok:
        if sats > 0:
            badge_r(f"GPS {sats}sat", (120, 80, 0), (220, 180, 60))
        else:
            badge_r("NO GPS", (150, 0, 0))


# ── Red-X failure overlays ────────────────────────────────────────────────────
def draw_red_x(surf, x, y, w, h, label):
    """Semi-transparent dark overlay with red X and label."""
    ov = pygame.Surface((w, h), pygame.SRCALPHA)
    ov.fill((20, 0, 0, 160))
    surf.blit(ov, (x, y))
    pygame.draw.line(surf, RED, (x + 4, y + 4), (x + w - 4, y + h - 4), 3)
    pygame.draw.line(surf, RED, (x + w - 4, y + 4), (x + 4, y + h - 4), 3)
    if label:
        _text(surf, label, 14, RED, bold=True, cx=x + w // 2, cy=y + h // 2 - 8)
        _text(surf, "FAIL", 14, RED, bold=True, cx=x + w // 2, cy=y + h // 2 + 8)


def draw_failure_overlays(surf, ahrs_ok, gps_ok, baro_ok, sats=0):
    ai_h_used = TAPE_H
    ai_y = TAPE_TOP
    ai_w = ALT_X - SPD_W
    if not ahrs_ok:
        # Cover AI center + heading strip
        draw_red_x(surf, SPD_W, ai_y, ai_w, ai_h_used, "ATTITUDE")
        draw_red_x(surf, 0, HDG_Y, DISPLAY_W, HDG_H, "HDG")
    # Red X on speed/alt tapes only when GPS is truly absent (no satellites).
    # While acquiring (sats > 0 but no fix) the tape stays live — data may
    # still be usable and the amber badge is sufficient warning.
    if not gps_ok and sats == 0:
        draw_red_x(surf, SPD_X, ai_y, SPD_W, ai_h_used, "AIRSPD")
    if not baro_ok and not gps_ok and sats == 0:
        draw_red_x(surf, ALT_X, ai_y, ALT_W, ai_h_used, "ALT")


# ── Demo animation ────────────────────────────────────────────────────────────
class DemoState:
    """Sedona AZ flight scenario animation."""
    SCENARIOS = [
        # (label, roll, pitch, hdg, alt, spd, vs, ay, hdg_bug, alt_bug, duration_s)
        ("Level cruise SE – Sedona Valley 8500 ft",
          0,  2, 133, 8500, 115,    0, 0.00, 133, 8500, 8.0),
        ("Climbing left turn – departing NW",
         -18, 4, 218, 7200, 108,  650, -0.08, 250, 9500, 8.0),
        ("Descending final – Rwy 03 KSEZ",
          0, -3,  19, 6200,  90, -500, 0.00,  19, 4900, 8.0),
        ("Short final – gear speed",
          0, -2,  19, 5200,  75, -600, 0.00,  19, 4900, 6.0),
    ]

    def __init__(self):
        self._idx  = 0
        self._t0   = time.monotonic()
        self._apply(0)

    def _apply(self, idx):
        sc = self.SCENARIOS[idx % len(self.SCENARIOS)]
        self.label   = sc[0]
        self._target = dict(zip(
            ("roll", "pitch", "yaw", "alt", "speed", "vspeed", "ay",
             "hdg_bug", "alt_bug", "_dur"),
            sc[1:]))
        self._target["lat"] = DEMO_LAT
        self._target["lon"] = DEMO_LON
        self._target["fix"] = 1
        self._target["sats"] = 8
        self._target["ahrs_ok"] = True
        self._target["gps_ok"]  = True
        self._target["baro_ok"] = False
        self._target["baro_src"] = "gps"

    def tick(self):
        elapsed = time.monotonic() - self._t0
        sc_dur  = self._target.get("_dur", 8.0)
        if elapsed > sc_dur:
            self._idx += 1
            self._t0   = time.monotonic()
            self._apply(self._idx)
        # Apply sensor values to shared state
        with _state_lock:
            for k in ("roll", "pitch", "yaw", "alt", "speed", "vspeed",
                      "ay", "lat", "lon", "fix", "sats",
                      "ahrs_ok", "gps_ok", "baro_ok", "baro_src"):
                if k in self._target:
                    state[k] = self._target[k]
            state["gps_alt"] = state["alt"]
            state["track"]   = state["yaw"]
        # Apply bug positions directly to disp (they're not in state)
        disp["hdg_bug"] = self._target.get("hdg_bug", disp["hdg_bug"])
        disp["alt_bug"] = self._target.get("alt_bug", disp["alt_bug"])


# ── Flight simulator ─────────────────────────────────────────────────────────
class SimFlyState:
    """Interactive flight simulator. Bugs (hdg_bug, alt_bug, spd_bug) are
    autopilot targets; sensor failure flags in disp['sim'] control which
    instruments are serviceable."""

    def __init__(self):
        sim = disp["sim"]
        preset = SIM_PRESETS[sim["preset_idx"]]
        self._last_t = time.monotonic()
        with _state_lock:
            state["lat"]     = preset[2]
            state["lon"]     = preset[3]
            state["alt"]     = sim["init_alt"]
            state["gps_alt"] = sim["init_alt"]
            state["yaw"]     = sim["init_hdg"]
            state["track"]   = sim["init_hdg"]
            state["speed"]   = sim["init_spd"]
            state["pitch"]   = 0.0
            state["roll"]    = 0.0
            state["vspeed"]  = 0.0
            state["ay"]      = 0.0
            state["fix"]     = 1
            state["sats"]    = 8
            state["ahrs_ok"] = True
            state["gps_ok"]  = True
            state["baro_ok"] = False
            state["baro_src"] = "gps"
        # Seed bugs so the aircraft holds its initial state.  Both bugs
        # start at the same value; whichever is active in the current
        # heading-source mode is what the autopilot follows.
        disp["hdg_bug"] = sim["init_hdg"]
        disp["trk_bug"] = sim["init_hdg"]
        disp["alt_bug"] = sim["init_alt"]
        if disp.get("spd_bug") is None:
            disp["spd_bug"] = sim["init_spd"]

    def tick(self):
        now = time.monotonic()
        dt  = min(now - self._last_t, 0.1)
        self._last_t = now

        sim = disp["sim"]
        gps_fail  = sim.get("gps_fail",  False)
        ahrs_fail = sim.get("ahrs_fail", False)
        baro_fail = sim.get("baro_fail", False)

        with _state_lock:
            # ── Targets from bugs ──────────────────────────────────────────────
            # Honour whichever bug the pilot is actually looking at.  TRK mode
            # closes the heading loop on GPS track instead of magnetic yaw —
            # otherwise the AP holds yaw at the bug, wind crabs the track off
            # to one side, and the displayed track never matches what the
            # pilot dialled in.  By driving bank from track error, yaw
            # naturally settles a few degrees into the wind so track lands
            # on the bug.
            _bk = _active_bug_key()
            tgt_hdg = disp.get(_bk)
            if tgt_hdg is None:
                tgt_hdg = state["yaw"]
            tgt_alt = disp["alt_bug"] if disp.get("alt_bug") is not None else state["alt"]
            tgt_spd = disp.get("spd_bug") or 90.0

            # FOLLOW FLIGHT-PLAN mode — overrides bug-based targets.
            # The autopilot flies a 45° intercept to the course (D2 line
            # for plain direct-to, final approach course when an
            # approach is loaded), ramping down to a gentle cross-track
            # correction once within ~0.3 nm of track.  Standard
            # behaviour for any modern avionics — far cleaner than
            # "always turn toward the destination" which over-shoots
            # parallel courses and never settles on track.
            if sim.get("follow_mode") == "fp":
                nv = disp.get("nav") or {}
                if nv.get("ident"):
                    cur_lat = state["lat"]
                    cur_lon = state["lon"]
                    wp_lat  = float(nv["lat"])
                    wp_lon  = float(nv["lon"])
                    dist_nm, brg = _nav_geo_dist_brg(
                        cur_lat, cur_lon, wp_lat, wp_lon)

                    ap = disp.get("approach") or {}
                    if ap.get("active"):
                        # Approach: course is the published runway
                        # heading (true).  Final is short enough (5–10
                        # nm) that flat-earth XTK at the threshold is
                        # equivalent to the spherical version, and is
                        # what the CDI itself uses on the approach path.
                        course_deg = float(ap["course_deg"])
                        cl_lat = float(ap["thresh_lat"])
                        cl_lon = float(ap["thresh_lon"])
                        cos_lat = max(1e-6, math.cos(math.radians(cl_lat)))
                        de_nm = (cur_lon - cl_lon) * 60.0 * cos_lat
                        dn_nm = (cur_lat - cl_lat) * 60.0
                        course_rad = math.radians(course_deg)
                        xtk = (de_nm * math.cos(course_rad)
                               - dn_nm * math.sin(course_rad))
                    else:
                        # D2: fly the great-circle to the waypoint.
                        # ``brg`` (computed above) is the local GC
                        # tangent at the current position — that's the
                        # desired track once we're back on course, and
                        # it tracks the GC bend naturally as we cross
                        # the leg.  Cross-track is the spherical
                        # perpendicular distance from the act→wpt great
                        # circle, matching the CDI's reference and the
                        # SVT trace.  Holding the *initial* bearing from
                        # the activation point (the previous behaviour)
                        # plus a flat-earth XTK at the waypoint produced
                        # phantom XTKs of tens to hundreds of nm on long
                        # legs — pure equirectangular distortion — and
                        # the AP saturated at 45° intercept aimed at the
                        # wrong heading.
                        course_deg = brg
                        ax_lat = float(nv.get("act_lat", cur_lat))
                        ax_lon = float(nv.get("act_lon", cur_lon))
                        xtk = _nav_xtk_nm(ax_lat, ax_lon,
                                          wp_lat, wp_lon,
                                          cur_lat, cur_lon)

                    tgt_hdg = _sim_intercept_heading(
                        course_deg, xtk,
                        approach=bool(ap.get("active")))

                    if ap.get("active"):
                        # Standard glideslope capture: only ever
                        # intercept the GS FROM ABOVE.  Never command
                        # a climb to chase the GS — that's wrong
                        # avionics behaviour (and would be wrong for
                        # the real AP when we wire this in later).
                        # Above the GS: track it (sim's existing
                        # ±1500 fpm VS clamp caps the descent rate).
                        # Below the GS: hold current altitude — the
                        # GS will descend toward the aircraft as it
                        # closes on the threshold and naturally
                        # capture from above.  Small +20 ft hysteresis
                        # avoids per-frame jitter at the boundary.
                        thresh_elev = float(ap["thresh_elev_ft"])
                        gs_deg      = 3.0
                        gs_alt = thresh_elev + (
                            dist_nm * 6076.12
                            * math.tan(math.radians(gs_deg)))
                        if state["alt"] >= gs_alt - 20:
                            tgt_alt = max(gs_alt, thresh_elev + 5)
                        else:
                            tgt_alt = state["alt"]

            # ── Heading / bank ─────────────────────────────────────────────────
            # Coordinated-turn model: BANK is the only commanded
            # quantity; yaw rate falls out of the bank via
            # ω = g·tan(φ)/V.  The previous version commanded yaw
            # directly and let bank be a separate cosmetic display,
            # which produced "flat yaw" — the AI airplane swung in
            # heading with wings level — particularly during the last
            # few degrees of a CDI capture where bank was nearly zero
            # but yaw was still being driven.
            #
            # Reference for the heading-hold error: yaw in MAG mode,
            # track in TRK mode.  state["track"] gets refreshed below
            # from the wind solution; first frame falls back to yaw to
            # avoid a transient.
            if _bk == "trk_bug":
                ref_hdg = state.get("track", state["yaw"]) or state["yaw"]
            else:
                ref_hdg = state["yaw"]
            hdg_err = ((tgt_hdg - ref_hdg + 180) % 360) - 180

            # Bank command — proportional, ±25° saturation at ~5°
            # error.  Below saturation, bank rolls out smoothly with
            # heading error so we settle on track without overshoot.
            bank = max(-25.0, min(25.0, hdg_err * 5.0))
            state["roll"] = bank if not ahrs_fail else 0.0
            state["ay"]   = -bank / 600.0   # slip ball

            # Yaw follows from actual bank — coordinated turn.
            v_fps = max(20.0, state["speed"] * 1.6878)
            yaw_rate_dps = math.degrees(
                32.174 * math.tan(math.radians(bank)) / v_fps)
            state["yaw"] = (state["yaw"] + yaw_rate_dps * dt) % 360

            # ── Altitude / VS / pitch ──────────────────────────────────────────
            # On approach, the GS itself is descending at ~530 fpm at
            # 100 kt, so a pure proportional alt-error controller can
            # only catch it asymptotically — sim flies persistently
            # above the GS.  Add the GS descent rate as feedforward and
            # bump the closed-loop gain so the diamond actually centres.
            _ap_now = disp.get("approach") or {}
            _on_appr_descent = (_ap_now.get("active")
                                and tgt_alt < state["alt"] - 5.0)
            alt     = state["alt"]
            alt_err = tgt_alt - alt
            if abs(alt_err) < 5.0:
                state["alt"] = tgt_alt
                vs_fpm = 0.0
            elif _on_appr_descent:
                gs_descent_fpm = -(state["speed"] * 6076.12
                                    * math.tan(math.radians(3.0)) / 60.0)
                vs_fpm = max(-1500.0, min(1500.0,
                                          alt_err * 6.0 + gs_descent_fpm))
                state["alt"] = alt + vs_fpm / 60.0 * dt
            else:
                vs_fpm  = max(-1500.0, min(1500.0, alt_err * 2.0))
                state["alt"] = alt + vs_fpm / 60.0 * dt
            state["gps_alt"] = state["alt"]
            state["vspeed"]  = vs_fpm
            state["pitch"]   = max(-10.0, min(10.0, vs_fpm / 100.0)) if not ahrs_fail else 0.0

            # ── Speed / acceleration ───────────────────────────────────────────
            spd     = state["speed"]
            spd_err = tgt_spd - spd
            d_spd   = max(-2.0 * dt, min(2.0 * dt, spd_err * 0.5))
            state["speed"] = max(0.0, spd + d_spd)

            # ── Position ───────────────────────────────────────────────────────
            # Constant simulated wind from 270° (west) at 7 kt — yields a
            # ~3–5° crab angle at typical training-aircraft speeds.  Just
            # enough that the cross-source pointer on the heading tape
            # has something to show (track ≠ heading) without the magenta
            # direct-to course visibly leaning off the nose.
            SIM_WIND_FROM_DEG = 270.0
            SIM_WIND_KT       = 7.0
            wind_rad = math.radians(SIM_WIND_FROM_DEG + 180.0)  # blowing TO
            wind_n_kt = SIM_WIND_KT * math.cos(wind_rad)
            wind_e_kt = SIM_WIND_KT * math.sin(wind_rad)
            tas_kt    = state["speed"]   # treat sim speed as TAS
            hdg_rad   = math.radians(state["yaw"])
            ac_n_kt   = tas_kt * math.cos(hdg_rad)
            ac_e_kt   = tas_kt * math.sin(hdg_rad)
            gnd_n_kt  = ac_n_kt + wind_n_kt
            gnd_e_kt  = ac_e_kt + wind_e_kt
            gs_kt     = math.hypot(gnd_n_kt, gnd_e_kt)
            track_deg = math.degrees(math.atan2(gnd_e_kt, gnd_n_kt)) % 360.0

            nm_s           = gs_kt / 3600.0
            track_rad      = math.radians(track_deg)
            nm_per_deg_lat = 60.0
            nm_per_deg_lon = max(1.0, 60.0 * math.cos(math.radians(state["lat"])))
            state["lat"]  += nm_s * dt * math.cos(track_rad) / nm_per_deg_lat
            state["lon"]  += nm_s * dt * math.sin(track_rad) / nm_per_deg_lon
            state["track"] = track_deg

            # ── Sensor failure simulation ──────────────────────────────────────
            state["gps_ok"]  = not gps_fail
            state["fix"]     = 0 if gps_fail else 1
            state["sats"]    = 0 if gps_fail else 8
            state["ahrs_ok"] = not ahrs_fail
            state["baro_ok"] = not baro_fail
            state["baro_src"] = "baro" if (not baro_fail) else "gps"


# ── Sim setup screen ─────────────────────────────────────────────────────────

# Airport grid: 4 cols × 3 rows, 8px gap, y starts at 52 (below header)
_SIM_COLS      = 4
_SIM_ROWS_     = 3        # number of preset rows (underscore to avoid shadowing)
_SIM_BTN_W    = 148
_SIM_BTN_H    = 54
_SIM_GAP      = 8
_SIM_GRID_X0  = (DISPLAY_W - _SIM_COLS * _SIM_BTN_W - (_SIM_COLS - 1) * _SIM_GAP) // 2
_SIM_GRID_Y0  = 52

# Condition boxes row
_SIM_COND_Y   = _SIM_GRID_Y0 + _SIM_ROWS_ * _SIM_BTN_H + (_SIM_ROWS_ - 1) * _SIM_GAP + _SIM_GAP + 8
_SIM_COND_H   = 44
_SIM_COND_W   = (DISPLAY_W - 2 * 12) // 3     # ~205 px each

# Sensor-failure toggles row
_SIM_FAIL_Y   = _SIM_COND_Y + _SIM_COND_H + 8
_SIM_FAIL_H   = 40
_SIM_FAIL_BW  = 70    # ON / FAIL button width each
_SIM_FAIL_GAP = 4     # gap between ON / FAIL pair

# START / CANCEL
_SIM_ACT_Y    = _SIM_FAIL_Y + _SIM_FAIL_H + 10
_SIM_ACT_H    = 54


def _sim_preset_rect(idx):
    """Return (x, y, w, h) for a preset button by index."""
    col = idx % _SIM_COLS
    row = idx // _SIM_COLS
    x = _SIM_GRID_X0 + col * (_SIM_BTN_W + _SIM_GAP)
    y = _SIM_GRID_Y0 + row * (_SIM_BTN_H + _SIM_GAP)
    return x, y, _SIM_BTN_W, _SIM_BTN_H


def _sim_cond_rect(idx):
    """Return (x, y, w, h) for condition box (ALT=0, HDG=1, SPD=2)."""
    mx = 12
    x = mx + idx * (_SIM_COND_W + 4)
    return x, _SIM_COND_Y, _SIM_COND_W, _SIM_COND_H


def _sim_fail_x(col_idx):
    """Left x of the ON/FAIL pair for GPS=0, BARO=1, AHRS=2."""
    total_pair = _SIM_FAIL_BW * 2 + _SIM_FAIL_GAP
    section_w  = DISPLAY_W // 3
    # centre the pair inside its section
    return col_idx * section_w + (section_w - total_pair) // 2


def _sim_fail_btn_pair(surf, col_idx, label, failed, y=None):
    """Draw a sensor ON/FAIL segmented pair at col_idx (0=GPS,1=BARO,2=AHRS)."""
    bx = _sim_fail_x(col_idx)
    by = y if y is not None else _SIM_FAIL_Y
    section_w = DISPLAY_W // 3
    # section label
    _text(surf, label, 11, (120, 140, 165),
          cx=col_idx * section_w + section_w // 2, y=by - 14)
    # ON button
    on_active = not failed
    on_bg = (0, 55, 20) if on_active else (0, 10, 20)
    on_oc = (40, 200, 60) if on_active else (40, 70, 55)
    on_tc = (60, 220, 80) if on_active else (70, 110, 90)
    pygame.draw.rect(surf, on_bg, (bx, by, _SIM_FAIL_BW, _SIM_FAIL_H), border_radius=5)
    pygame.draw.rect(surf, on_oc, (bx, by, _SIM_FAIL_BW, _SIM_FAIL_H), width=2, border_radius=5)
    _text(surf, "ON", 13, on_tc, bold=on_active, cx=bx + _SIM_FAIL_BW // 2, cy=by + _SIM_FAIL_H // 2)
    # FAIL button
    fail_x = bx + _SIM_FAIL_BW + _SIM_FAIL_GAP
    fail_active = failed
    fail_bg = (50, 5, 5) if fail_active else (12, 0, 0)
    fail_oc = (200, 40, 40) if fail_active else (80, 35, 35)
    fail_tc = RED if fail_active else (120, 60, 60)
    pygame.draw.rect(surf, fail_bg, (fail_x, by, _SIM_FAIL_BW, _SIM_FAIL_H), border_radius=5)
    pygame.draw.rect(surf, fail_oc, (fail_x, by, _SIM_FAIL_BW, _SIM_FAIL_H), width=2, border_radius=5)
    _text(surf, "FAIL", 11, fail_tc, bold=fail_active, cx=fail_x + _SIM_FAIL_BW // 2, cy=by + _SIM_FAIL_H // 2)


def draw_sim_setup(surf):
    """Full-screen flight simulator setup screen."""
    _screen_header(surf, "FLIGHT SIMULATOR")
    sim = disp["sim"]
    selected = sim["preset_idx"]

    # ── Airport preset grid ───────────────────────────────────────────────────
    for idx, (icao, city, *_) in enumerate(SIM_PRESETS):
        px, py, pw, ph = _sim_preset_rect(idx)
        active = (idx == selected)
        bg = (0, 35, 55) if active else (0, 12, 32)
        oc = CYAN if active else (50, 70, 100)
        pygame.draw.rect(surf, bg, (px, py, pw, ph), border_radius=6)
        glow_h = ph // 5
        for i in range(glow_h):
            t = 1.0 - i / glow_h
            gc = ((int(t * 20), int(50 + t * 50), int(65 + t * 60)) if active
                  else (int(15 + t * 25), int(20 + t * 40), int(40 + t * 60)))
            pygame.draw.line(surf, gc, (px + 6, py + 1 + i), (px + pw - 6, py + 1 + i))
        pygame.draw.rect(surf, oc, (px, py, pw, ph), width=2 if active else 1, border_radius=6)
        _text(surf, icao, 15, WHITE if active else (180, 195, 210), bold=True,
              cx=px + pw // 2, cy=py + ph // 2 - 8)
        _text(surf, city, 9, (100, 130, 155) if not active else CYAN,
              cx=px + pw // 2, cy=py + ph // 2 + 8)

    # ── Initial conditions row ─────────────────────────────────────────────────
    cond_labels = ["ALT (ft)", "HDG (°)", "SPEED (kt)"]
    cond_keys   = ["init_alt", "init_hdg", "init_spd"]
    cond_vals   = [int(sim["init_alt"]), int(sim["init_hdg"]), int(sim["init_spd"])]

    for i, (lbl, val) in enumerate(zip(cond_labels, cond_vals)):
        cx2, cy2, cw, ch = _sim_cond_rect(i)
        pygame.draw.rect(surf, (0, 18, 38), (cx2, cy2, cw, ch), border_radius=5)
        pygame.draw.rect(surf, CYAN, (cx2, cy2, cw, ch), width=1, border_radius=5)
        _text(surf, lbl, 9, (100, 140, 170), cx=cx2 + cw // 2, y=cy2 + 4)
        _text(surf, str(val), 17, CYAN, bold=True, cx=cx2 + cw // 2, cy=cy2 + ch // 2 + 5)
        _text(surf, "tap to set", 8, (70, 100, 130), cx=cx2 + cw // 2, y=cy2 + ch - 12)

    # ── Sensor failure toggles ────────────────────────────────────────────────
    _sim_fail_btn_pair(surf, 0, "GPS",  sim.get("gps_fail",  False))
    _sim_fail_btn_pair(surf, 1, "BARO", sim.get("baro_fail", False))
    _sim_fail_btn_pair(surf, 2, "AHRS", sim.get("ahrs_fail", False))

    # ── START / CANCEL buttons ────────────────────────────────────────────────
    bx = 12; bw = DISPLAY_W - 24
    half = (bw - 10) // 2
    _action_btn(surf, bx,          _SIM_ACT_Y, half, _SIM_ACT_H, "START SIM", "ok")
    _action_btn(surf, bx + half + 10, _SIM_ACT_Y, half, _SIM_ACT_H, "CANCEL",    "danger")


def sim_setup_hit(x, y):
    """Return action string for the sim setup screen tap, or None."""
    # BACK button
    if _back_hit(x, y):
        return "back"

    # Airport preset grid
    for idx in range(len(SIM_PRESETS)):
        px, py, pw, ph = _sim_preset_rect(idx)
        if px <= x <= px + pw and py <= y <= py + ph:
            return f"preset:{idx}"

    # Initial conditions tappable boxes
    sim = disp["sim"]
    for i, key in enumerate(("init_alt", "init_hdg", "init_spd")):
        cx2, cy2, cw, ch = _sim_cond_rect(i)
        if cx2 <= x <= cx2 + cw and cy2 <= y <= cy2 + ch:
            return f"cond:{key}"

    # Sensor failure toggles
    for col_idx, sensor in enumerate(("gps", "baro", "ahrs")):
        bx = _sim_fail_x(col_idx)
        by = _SIM_FAIL_Y
        if by <= y <= by + _SIM_FAIL_H:
            if bx <= x <= bx + _SIM_FAIL_BW:
                return f"sensor_on:{sensor}"
            fail_x = bx + _SIM_FAIL_BW + _SIM_FAIL_GAP
            if fail_x <= x <= fail_x + _SIM_FAIL_BW:
                return f"sensor_fail:{sensor}"

    # START / CANCEL
    bx_btn = 12; bw_btn = DISPLAY_W - 24
    half = (bw_btn - 10) // 2
    if _SIM_ACT_Y <= y <= _SIM_ACT_Y + _SIM_ACT_H:
        if bx_btn <= x <= bx_btn + half:
            return "start"
        if bx_btn + half + 10 <= x <= bx_btn + half + 10 + half:
            return "cancel"

    return None


# ── Sim watermark / quick-exit button ────────────────────────────────────────
# Sized to read clearly from a normal cockpit viewing distance so pilots
# notice it's tappable.  Tap → opens the SIM CONTROLS overlay (which has
# the EXIT SIM action).  Distinct names from the sim-setup grid above
# (which also defines _SIM_BTN_W/H) — they're different controls and
# would otherwise shadow each other through the global namespace.
_SIM_EXIT_W = 88
_SIM_EXIT_H = 32
_SIM_EXIT_X = CX - _SIM_EXIT_W // 2
_SIM_EXIT_Y = CY - 36 - _SIM_EXIT_H


# ── Sim controls overlay ─────────────────────────────────────────────────────

_SIMCTRL_W = 320
_SIMCTRL_H = 320
_SIMCTRL_X = (DISPLAY_W - _SIMCTRL_W) // 2
_SIMCTRL_Y = (DISPLAY_H - _SIMCTRL_H) // 2 - 10

_SIMCTRL_ROW_Y0  = _SIMCTRL_Y + 36   # first sensor row top
_SIMCTRL_ROW_H   = 32
_SIMCTRL_ROW_GAP = 4
_SIMCTRL_BW      = 70     # ON / FAIL button width

_SIMCTRL_FOLLOW_BW   = 110    # FOLLOW BUGS / FLT PLAN button width


def _simctrl_follow_y() -> int:
    return _SIMCTRL_ROW_Y0 + 3 * (_SIMCTRL_ROW_H + _SIMCTRL_ROW_GAP) + 8


def _simctrl_exit_setup_y() -> int:
    return _simctrl_follow_y() + _SIMCTRL_ROW_H + 14


def _simctrl_exit_sim_y() -> int:
    return _simctrl_exit_setup_y() + 44 + 8


def draw_sim_controls(surf):
    """Semi-transparent overlay drawn on top of the live PFD."""
    sim = disp["sim"]

    # Background panel
    panel = pygame.Surface((_SIMCTRL_W, _SIMCTRL_H), pygame.SRCALPHA)
    panel.fill((0, 10, 28, 220))
    surf.blit(panel, (_SIMCTRL_X, _SIMCTRL_Y))
    pygame.draw.rect(surf, CYAN, (_SIMCTRL_X, _SIMCTRL_Y, _SIMCTRL_W, _SIMCTRL_H),
                     width=2, border_radius=8)

    # Title
    _text(surf, "SIM CONTROLS", 14, CYAN, bold=True,
          cx=_SIMCTRL_X + _SIMCTRL_W // 2, cy=_SIMCTRL_Y + 16)

    # Sensor rows: GPS / BARO / AHRS
    sensors = [("GPS",  "gps_fail"), ("BARO", "baro_fail"), ("AHRS", "ahrs_fail")]
    for ri, (label, key) in enumerate(sensors):
        row_y = _SIMCTRL_ROW_Y0 + ri * (_SIMCTRL_ROW_H + _SIMCTRL_ROW_GAP)
        failed = sim.get(key, False)

        _text(surf, label, 12, (160, 175, 200), bold=True,
              x=_SIMCTRL_X + 14, cy=row_y + _SIMCTRL_ROW_H // 2)

        on_active = not failed
        on_bg = (0, 50, 20) if on_active else (0, 8, 16)
        on_oc = (40, 190, 60) if on_active else (35, 60, 45)
        on_tc = (60, 220, 80) if on_active else (60, 100, 75)
        ox = _SIMCTRL_X + _SIMCTRL_W - 2 * _SIMCTRL_BW - 8 - 6
        pygame.draw.rect(surf, on_bg, (ox, row_y, _SIMCTRL_BW, _SIMCTRL_ROW_H), border_radius=4)
        pygame.draw.rect(surf, on_oc, (ox, row_y, _SIMCTRL_BW, _SIMCTRL_ROW_H), width=2, border_radius=4)
        _text(surf, "ON", 12, on_tc, bold=on_active,
              cx=ox + _SIMCTRL_BW // 2, cy=row_y + _SIMCTRL_ROW_H // 2)

        fx = ox + _SIMCTRL_BW + 6
        fail_active = failed
        fail_bg = (50, 5, 5) if fail_active else (12, 0, 0)
        fail_oc = (200, 40, 40) if fail_active else (75, 30, 30)
        fail_tc = RED if fail_active else (110, 55, 55)
        pygame.draw.rect(surf, fail_bg, (fx, row_y, _SIMCTRL_BW, _SIMCTRL_ROW_H), border_radius=4)
        pygame.draw.rect(surf, fail_oc, (fx, row_y, _SIMCTRL_BW, _SIMCTRL_ROW_H), width=2, border_radius=4)
        _text(surf, "FAIL", 11, fail_tc, bold=fail_active,
              cx=fx + _SIMCTRL_BW // 2, cy=row_y + _SIMCTRL_ROW_H // 2)

    # FOLLOW row — segmented control selecting the autopilot source.
    fy = _simctrl_follow_y()
    _text(surf, "FOLLOW", 12, (160, 175, 200), bold=True,
          x=_SIMCTRL_X + 14, cy=fy + _SIMCTRL_ROW_H // 2)
    follow = sim.get("follow_mode", "bugs")
    fx_b = _SIMCTRL_X + _SIMCTRL_W - 2 * _SIMCTRL_FOLLOW_BW - 8 - 6
    for i, (val, lbl) in enumerate((("bugs", "BUGS"), ("fp", "FLT PLAN"))):
        bx = fx_b + i * (_SIMCTRL_FOLLOW_BW + 6)
        active = (follow == val)
        bg = (0, 55, 65) if active else (0, 10, 25)
        oc = CYAN       if active else (50, 68, 92)
        tc = CYAN       if active else (130, 148, 168)
        pygame.draw.rect(surf, bg, (bx, fy, _SIMCTRL_FOLLOW_BW, _SIMCTRL_ROW_H), border_radius=4)
        pygame.draw.rect(surf, oc, (bx, fy, _SIMCTRL_FOLLOW_BW, _SIMCTRL_ROW_H), width=2, border_radius=4)
        _text(surf, lbl, 12, tc, bold=active,
              cx=bx + _SIMCTRL_FOLLOW_BW // 2, cy=fy + _SIMCTRL_ROW_H // 2)

    # EXIT SETUP — closes the overlay, sim continues running.
    es_y = _simctrl_exit_setup_y()
    _action_btn(surf, _SIMCTRL_X + 14, es_y,
                _SIMCTRL_W - 28, 44, "EXIT SETUP", "normal")

    # EXIT SIM — kills the sim and returns to live AHRS.
    xs_y = _simctrl_exit_sim_y()
    _action_btn(surf, _SIMCTRL_X + 14, xs_y,
                _SIMCTRL_W - 28, 44, "EXIT SIM", "danger")


def sim_controls_hit(x, y):
    """Return action for a tap on the sim_controls overlay, or None."""
    # Outside the panel — ignore (do not propagate to PFD)
    if not (_SIMCTRL_X <= x <= _SIMCTRL_X + _SIMCTRL_W and
            _SIMCTRL_Y <= y <= _SIMCTRL_Y + _SIMCTRL_H):
        return None

    sensors = [("gps", "gps_fail"), ("baro", "baro_fail"), ("ahrs", "ahrs_fail")]
    for ri, (key_short, _key) in enumerate(sensors):
        row_y = _SIMCTRL_ROW_Y0 + ri * (_SIMCTRL_ROW_H + _SIMCTRL_ROW_GAP)
        if not (row_y <= y <= row_y + _SIMCTRL_ROW_H):
            continue
        ox = _SIMCTRL_X + _SIMCTRL_W - 2 * _SIMCTRL_BW - 8 - 6
        fx = ox + _SIMCTRL_BW + 6
        if ox <= x <= ox + _SIMCTRL_BW:
            return f"sensor_on:{key_short}"
        if fx <= x <= fx + _SIMCTRL_BW:
            return f"sensor_fail:{key_short}"

    # FOLLOW row
    fy = _simctrl_follow_y()
    if fy <= y <= fy + _SIMCTRL_ROW_H:
        fx_b = _SIMCTRL_X + _SIMCTRL_W - 2 * _SIMCTRL_FOLLOW_BW - 8 - 6
        for i, val in enumerate(("bugs", "fp")):
            bx = fx_b + i * (_SIMCTRL_FOLLOW_BW + 6)
            if bx <= x <= bx + _SIMCTRL_FOLLOW_BW:
                return f"follow:{val}"

    # EXIT SETUP
    es_y = _simctrl_exit_setup_y()
    if (es_y <= y <= es_y + 44 and
            _SIMCTRL_X + 14 <= x <= _SIMCTRL_X + _SIMCTRL_W - 14):
        return "exit_setup"

    # EXIT SIM
    xs_y = _simctrl_exit_sim_y()
    if (xs_y <= y <= xs_y + 44 and
            _SIMCTRL_X + 14 <= x <= _SIMCTRL_X + _SIMCTRL_W - 14):
        return "exit_sim"

    return "noop"   # tapped inside panel but not on a control — consume event


# ── Nav-confirm modal ("Activate Direct to XXXX?") ──────────────────────────
# Standard avionics convention: typing/refreshing a waypoint pops up a
# confirmation, requiring a second ENTER (or tap on ACTIVATE) before the
# active flight plan changes.  Prevents accidental flight-plan edits from
# a stray screen tap.
_NAVCNF_W   = 360
_NAVCNF_H   = 170
_NAVCNF_BTN_H = 48


def _navcnf_geom():
    bx = (DISPLAY_W - _NAVCNF_W) // 2
    by = (DISPLAY_H - _NAVCNF_H) // 2
    btn_y = by + _NAVCNF_H - _NAVCNF_BTN_H - 14
    btn_w = (_NAVCNF_W - 14 - 14 - 12) // 2
    bx_l  = bx + 14
    bx_r  = bx + _NAVCNF_W - 14 - btn_w
    return bx, by, btn_y, btn_w, bx_l, bx_r


def draw_nav_confirm(surf):
    """Centered "Activate Direct to XXXX?" modal."""
    ident = disp.get("nav_confirm_ident", "")
    bx, by, btn_y, btn_w, bx_l, bx_r = _navcnf_geom()

    _draw_veil(surf)
    panel = pygame.Surface((_NAVCNF_W, _NAVCNF_H), pygame.SRCALPHA)
    panel.fill((0, 12, 32, 235))
    surf.blit(panel, (bx, by))
    pygame.draw.rect(surf, CYAN, (bx, by, _NAVCNF_W, _NAVCNF_H),
                     width=2, border_radius=10)

    _text(surf, "DIRECT TO", 13, (160, 200, 230), bold=True,
          cx=bx + _NAVCNF_W // 2, cy=by + 22)
    _text(surf, ident or "—", 32, MAGENTA, bold=True,
          cx=bx + _NAVCNF_W // 2, cy=by + 64)
    _text(surf, "Activate?", 14, (200, 215, 235),
          cx=bx + _NAVCNF_W // 2, cy=by + 96)

    _action_btn(surf, bx_l, btn_y, btn_w, _NAVCNF_BTN_H, "CANCEL",   "danger")
    _action_btn(surf, bx_r, btn_y, btn_w, _NAVCNF_BTN_H, "ACTIVATE", "ok")


def nav_confirm_hit(x, y):
    """Return 'activate' / 'cancel' / 'noop' / None for a tap on the modal."""
    bx, by, btn_y, btn_w, bx_l, bx_r = _navcnf_geom()
    if not (bx <= x <= bx + _NAVCNF_W and by <= y <= by + _NAVCNF_H):
        return None
    if btn_y <= y <= btn_y + _NAVCNF_BTN_H:
        if bx_l <= x <= bx_l + btn_w:
            return "cancel"
        if bx_r <= x <= bx_r + btn_w:
            return "activate"
    return "noop"


def _nav_confirm_apply():
    """Activate the pending direct-to and dismiss the modal.  Any
    active approach is cleared — pilot is explicitly choosing a new
    D2, so the existing HITS corridor no longer applies."""
    ident = disp.get("nav_confirm_ident", "")
    if ident:
        _nav_set_by_ident(ident)
        ap = disp.get("approach")
        if ap and ap.get("active"):
            ap["active"] = False
    disp["nav_confirm_ident"] = ""
    disp["mode"] = disp.get("nav_confirm_prev", "pfd")


def _nav_confirm_cancel():
    disp["nav_confirm_ident"] = ""
    disp["mode"] = disp.get("nav_confirm_prev", "pfd")


def _nav_open_confirm(ident: str, prev_mode: str) -> bool:
    """Switch to the nav_confirm modal for `ident`.  Returns False if
    the ident is empty (caller should fall through to its no-op
    path)."""
    if not ident:
        return False
    disp["nav_confirm_ident"] = ident
    disp["nav_confirm_prev"]  = prev_mode
    disp["mode"] = "nav_confirm"
    return True


# ── Compass calibration wizard ───────────────────────────────────────────────
# Cardinal walk-through: pilot points the aircraft at N / E / S / W in turn
# and taps CAPTURE.  We record (expected, raw) at each cardinal, then
# compute the circular mean of the (expected − raw) deltas and persist it
# as a single additive offset in ss["mag_cal_offset"].  The offset is added
# to the corrected yaw at render time so MAG-mode display reads true.  TRK
# mode is unaffected (the complementary filter sees yaw deltas, which a
# constant offset drops out of).  Same approach the iPhone display uses.
_MAG_CAL_CARDINALS = [("NORTH",   0.0),
                      ("EAST",   90.0),
                      ("SOUTH", 180.0),
                      ("WEST",  270.0)]
_MCAL_W   = 460
_MCAL_H   = 260
_MCAL_BTN_H = 44


def _apply_mag_cal(raw_hdg, deltas):
    """Piecewise-linear mag cal correction.  `deltas` is a 4-list of
    signed (expected − raw) deltas at N / E / S / W.  Linearly
    interpolates between adjacent cardinals so each 90° quadrant gets
    its own correction curve — same convention as a real aircraft
    compass swing card.  Returns calibrated heading in [0, 360)."""
    if not deltas or len(deltas) != 4 or not any(deltas):
        return raw_hdg % 360.0
    h = raw_hdg % 360.0
    band = int(h // 90) % 4
    t = (h - band * 90.0) / 90.0
    d0 = deltas[band]
    d1 = deltas[(band + 1) % 4]
    # Short-arc interpolation in case adjacent deltas straddle ±180
    # (rare with sane mag bias but keeps the math safe).
    diff = ((d1 - d0 + 540.0) % 360.0) - 180.0
    delta = d0 + diff * t
    return (h + delta) % 360.0


def _mag_cal_open(prev_mode: str):
    disp["mag_cal_wiz"] = {"step": 0, "samples": [], "msg": "",
                           "prev": prev_mode}
    disp["mode"] = "mag_cal"


def _mag_cal_capture():
    wiz = disp.get("mag_cal_wiz") or {}
    step = wiz.get("step", 0)
    if step >= len(_MAG_CAL_CARDINALS):
        return
    raw = float(disp.get("_yaw_uncal", disp.get("yaw", 0.0)))
    expected = _MAG_CAL_CARDINALS[step][1]
    wiz.setdefault("samples", []).append((expected, raw))
    wiz["step"] = step + 1
    wiz["msg"] = f"Captured {_MAG_CAL_CARDINALS[step][0]}."
    if wiz["step"] >= len(_MAG_CAL_CARDINALS):
        # Per-cardinal piecewise-linear cal: store the signed
        # (expected − raw) deltas at N / E / S / W.  Each quadrant
        # gets its own correction curve via interpolation in
        # _apply_mag_cal — same pattern as an aircraft compass
        # correction card.  Strictly more capable than a single
        # offset (which only fixes the average bias).
        deltas = []
        for exp, rawv in wiz["samples"]:
            d = ((exp - rawv + 540.0) % 360.0) - 180.0
            deltas.append(round(d, 2))
        disp["ss"]["mag_cal_deltas"] = deltas
        # Drop legacy single-offset key — superseded.
        disp["ss"].pop("mag_cal_offset", None)
        disp["ss"]["mag_cal"] = "done"
        _settings.mark_dirty()
        max_abs = max(abs(d) for d in deltas)
        wiz["msg"] = (f"Done — N{deltas[0]:+.1f}° E{deltas[1]:+.1f}° "
                      f"S{deltas[2]:+.1f}° W{deltas[3]:+.1f}°")
        wiz["step"]    = 0
        wiz["samples"] = []


def _mag_cal_restart():
    wiz = disp.get("mag_cal_wiz") or {}
    wiz["step"] = 0
    wiz["samples"] = []
    wiz["msg"] = "Restarted."


def _mag_cal_reset():
    """Wipe the stored cal.  Useful after moving the AHRS to a
    different aircraft / panel."""
    disp["ss"]["mag_cal_deltas"] = [0.0, 0.0, 0.0, 0.0]
    disp["ss"].pop("mag_cal_offset", None)   # legacy key
    disp["ss"]["mag_cal"] = "idle"
    _settings.mark_dirty()
    wiz = disp.get("mag_cal_wiz") or {}
    wiz["step"] = 0
    wiz["samples"] = []
    wiz["msg"] = "Calibration cleared."


def _mag_cal_close():
    wiz = disp.get("mag_cal_wiz") or {}
    disp["mode"] = wiz.get("prev", "ahrs_setup")


def _mcal_geom():
    bx = (DISPLAY_W - _MCAL_W) // 2
    by = (DISPLAY_H - _MCAL_H) // 2
    btn_y = by + _MCAL_H - _MCAL_BTN_H - 14
    btn_w = (_MCAL_W - 14 - 14 - 3 * 8) // 4
    btn_xs = [bx + 14 + i * (btn_w + 8) for i in range(4)]
    return bx, by, btn_y, btn_w, btn_xs


def draw_mag_cal(surf):
    """Compass-calibration modal — cardinal walk-through."""
    wiz = disp.get("mag_cal_wiz") or {"step": 0, "samples": [], "msg": ""}
    step = wiz.get("step", 0)
    card_name, card_exp = _MAG_CAL_CARDINALS[min(step,
                                                  len(_MAG_CAL_CARDINALS) - 1)]
    bx, by, btn_y, btn_w, btn_xs = _mcal_geom()

    _draw_veil(surf)
    panel = pygame.Surface((_MCAL_W, _MCAL_H), pygame.SRCALPHA)
    panel.fill((0, 12, 32, 235))
    surf.blit(panel, (bx, by))
    pygame.draw.rect(surf, CYAN, (bx, by, _MCAL_W, _MCAL_H),
                     width=2, border_radius=10)

    _text(surf, "COMPASS CAL", 14, (160, 200, 230), bold=True,
          cx=bx + _MCAL_W // 2, cy=by + 22)

    instr = (f"Step {step + 1} of {len(_MAG_CAL_CARDINALS)} — "
             f"point aircraft  {card_name}  ({int(card_exp):03d}°)")
    _text(surf, instr, 14, WHITE, cx=bx + _MCAL_W // 2, cy=by + 56)

    raw = float(disp.get("_yaw_uncal", disp.get("yaw", 0.0))) % 360.0
    cur_deltas = disp["ss"].get("mag_cal_deltas") or [0.0] * 4
    applied = _apply_mag_cal(raw, cur_deltas)

    _text(surf, "RAW", 11, (170, 185, 210), bold=True,
          x=bx + 30, y=by + 92)
    _text(surf, f"{raw:6.1f}°", 22, CYAN, bold=True,
          x=bx + 30, y=by + 108)

    _text(surf, "APPLIED", 11, (170, 185, 210), bold=True,
          x=bx + _MCAL_W // 2 + 20, y=by + 92)
    _text(surf, f"{applied:6.1f}°", 22, WHITE, bold=True,
          x=bx + _MCAL_W // 2 + 20, y=by + 108)

    # Per-cardinal deltas — the compass swing card.
    cards_y = by + 154
    for i, (name, _exp) in enumerate(_MAG_CAL_CARDINALS):
        cx_card = bx + 60 + i * (_MCAL_W - 120) // 3
        d = cur_deltas[i]
        _text(surf, name[0], 11, (170, 185, 210), bold=True,
              cx=cx_card, cy=cards_y)
        _text(surf, f"{d:+.1f}°", 13,
              WHITE if abs(d) > 0.05 else (110, 120, 140),
              bold=True, cx=cx_card, cy=cards_y + 16)

    msg = wiz.get("msg", "") or ""
    if msg:
        _text(surf, msg, 12, (60, 220, 80), cx=bx + _MCAL_W // 2,
              cy=by + 178)

    # Left button reads CANCEL only when there's something to cancel —
    # i.e. partial captures haven't been committed yet.  Once the
    # 4-cardinal walk completes, the offset is already persisted and
    # the button just closes the modal, so EXIT is the honest label.
    in_progress = step > 0 and step < len(_MAG_CAL_CARDINALS)
    left_lbl   = "CANCEL" if in_progress else "EXIT"
    left_style = "danger" if in_progress else "ok"
    _action_btn(surf, btn_xs[0], btn_y, btn_w, _MCAL_BTN_H, left_lbl, left_style)
    _action_btn(surf, btn_xs[1], btn_y, btn_w, _MCAL_BTN_H, "RESET",    "warn")
    _action_btn(surf, btn_xs[2], btn_y, btn_w, _MCAL_BTN_H, "RESTART",  "warn")
    _action_btn(surf, btn_xs[3], btn_y, btn_w, _MCAL_BTN_H,
                f"⊕ CAPTURE {card_name[0]}", "ok")


def mag_cal_hit(x, y):
    bx, by, btn_y, btn_w, btn_xs = _mcal_geom()
    if not (bx <= x <= bx + _MCAL_W and by <= y <= by + _MCAL_H):
        return None
    if btn_y <= y <= btn_y + _MCAL_BTN_H:
        for i, action in enumerate(("cancel", "reset", "restart", "capture")):
            if btn_xs[i] <= x <= btn_xs[i] + btn_w:
                return action
    return "noop"


# ── Touch handler ─────────────────────────────────────────────────────────────
_touch_t0      = {}
_bug_dragging  = None    # "hdg" | "alt"
_active_fingers = {}     # finger_id → touch-down time (ms)
_multitouch_t0  = None   # time when 2nd finger touched down

# Moving-map inset state.  _last_map_rect is updated each frame by the
# render loop so the touch handler can hit-test against it for the
# left/right tap-zoom affordance.
_last_map_rect = None


def _current_str_for_kbd(target, prev_mode):
    """String form of current keyboard-editable value for pre-population."""
    if prev_mode == "connectivity_setup":
        v = disp["cs"].get(target, "")
    else:
        v = disp["fp"].get(target, "")
    return str(v) if v not in (None, 0, "") else ""


def _active_bug_key():
    """Return "trk_bug" if the active heading source is GPS track, else
    "hdg_bug".  Used so all bug-set affordances (tap on heading box,
    arrow keys, numpad open, knob nudge) write to the bug that matches
    what the pilot is actually looking at."""
    ss = disp.get("ss", {})
    pref = ss.get("hdg_src", "auto")
    use_track, _, _ = _resolve_hdg_source(
        pref, disp.get("gps_ok", False), disp.get("ahrs_ok", False),
        disp.get("speed", 0.0))
    return "trk_bug" if use_track else "hdg_bug"


def _open_numpad(target):
    """Switch to numpad mode for the given bug target.  The buffer
    starts empty; draw_numpad falls back to showing the current value
    as a placeholder until the user types.  First digit replaces the
    placeholder naturally (because buf was empty), and backspace is a
    no-op until something is actually typed."""
    disp["numpad_target"] = target
    disp["numpad_buf"]    = ""
    disp["numpad_prev"]   = disp["mode"]
    disp["mode"]          = "numpad"


def handle_event(event, demo_mode):
    global _bug_dragging, _active_fingers, _multitouch_t0, _sim_state

    if event.type == pygame.QUIT:
        return False

    # ── Keyboard shortcuts ────────────────────────────────────────────────────
    if event.type == pygame.KEYDOWN:
        if event.key == pygame.K_ESCAPE:
            if disp["mode"] == "nav_confirm":
                _nav_confirm_cancel()
            elif disp["mode"] == "mag_cal":
                _mag_cal_close()
            elif disp["mode"] != "pfd":
                disp["mode"] = "pfd"   # ESC exits any overlay
            else:
                return False
        if event.key == pygame.K_RETURN and disp["mode"] == "nav_confirm":
            _nav_confirm_apply()
            return True
        if event.key == pygame.K_RETURN and disp["mode"] == "mag_cal":
            _mag_cal_capture()
            return True
        if event.key == pygame.K_d:
            return "toggle_demo"
        if disp["mode"] == "pfd":
            if event.key == pygame.K_UP:
                disp["alt_bug"] = round(disp["alt_bug"] / 100) * 100 + 100
            if event.key == pygame.K_DOWN:
                disp["alt_bug"] = round(disp["alt_bug"] / 100) * 100 - 100
            if event.key == pygame.K_LEFT:
                _bk = _active_bug_key()
                disp[_bk] = (round(disp[_bk]) - 10) % 360
            if event.key == pygame.K_RIGHT:
                _bk = _active_bug_key()
                disp[_bk] = (round(disp[_bk]) + 10) % 360
            if event.key in (pygame.K_PLUS, pygame.K_EQUALS):
                disp["baro_hpa"] = round(disp["baro_hpa"] * 100 + 1) / 100
            if event.key == pygame.K_MINUS:
                disp["baro_hpa"] = round(disp["baro_hpa"] * 100 - 1) / 100

    # ── Multi-finger tracking (FINGERDOWN / FINGERUP only) ───────────────────
    if event.type == pygame.FINGERDOWN:
        _active_fingers[event.finger_id] = pygame.time.get_ticks()
        if len(_active_fingers) >= 2 and _multitouch_t0 is None:
            _multitouch_t0 = pygame.time.get_ticks()

    if event.type == pygame.FINGERUP:
        _active_fingers.pop(event.finger_id, None)
        if len(_active_fingers) < 2:
            _multitouch_t0 = None

    # ── Single-touch / mouse ──────────────────────────────────────────────────
    if event.type in (pygame.MOUSEBUTTONDOWN, pygame.FINGERDOWN):
        # Skip if this is part of a multi-touch gesture
        if len(_active_fingers) >= 2:
            return True

        pos = event.pos if hasattr(event, "pos") else (
            int(event.x * DISPLAY_W), int(event.y * DISPLAY_H))
        x, y = pos

        mode = disp["mode"]

        # ── Setup screen taps ─────────────────────────────────────────────
        if mode == "setup":
            idx = setup_hit(x, y)
            if   idx == 5: disp["mode"] = "pfd"
            elif idx == 0: disp["mode"] = "flight_profile"
            elif idx == 1: disp["mode"] = "display_setup"
            elif idx == 2: disp["mode"] = "ahrs_setup"
            elif idx == 3:
                actual = disp["cs"].get("wifi_actual", "")
                if actual:
                    disp["cs"]["wifi_ssid"] = actual
                disp["mode"] = "connectivity_setup"
            elif idx == 4: disp["mode"] = "system_setup"
            return True

        # ── Display settings taps ─────────────────────────────────────────
        if mode == "display_setup":
            action = display_setup_hit(x, y, disp["ds"])
            if action == "back":
                disp["mode"] = "setup"
            elif action and action.startswith("set:"):
                _, key, val_str = action.split(":", 2)
                # Coerce by token: True/False → bool, digits → int, else str.
                # Lets one action protocol carry the mix of types now in
                # disp["ds"] (units strings, map enable/layer bools, zoom int).
                if val_str in ("True", "False"):
                    val = (val_str == "True")
                elif val_str.lstrip("-").isdigit():
                    val = int(val_str)
                else:
                    val = val_str
                disp["ds"][key] = val
                _settings.mark_dirty()
            elif action and action.startswith("inc:brightness:"):
                delta = int(action.split(":")[-1])
                disp["ds"]["brightness"] = max(1, min(10, disp["ds"]["brightness"] + delta))
                _set_backlight(disp["ds"]["brightness"])
                _settings.mark_dirty()
            return True

        # ── Approach selection taps ───────────────────────────────────────
        if mode == "approach_select":
            action = approach_select_hit(x, y)
            if action == "back":
                disp["mode"] = "pfd"
            elif action == "cancel":
                _approach_cancel()
                disp["mode"] = "pfd"
            elif action and action.startswith("select:"):
                idx = int(action.split(":", 1)[1])
                ident = (disp.get("nav") or {}).get("ident", "") or \
                        (disp.get("approach") or {}).get("airport", "")
                ends = _apr_runway_ends(ident)
                if 0 <= idx < len(ends):
                    _approach_activate(ends[idx])
                    disp["mode"] = "pfd"
            return True

        # ── AHRS / Sensors taps ───────────────────────────────────────────
        if mode == "ahrs_setup":
            action = ahrs_setup_hit(x, y, disp["ss"])
            if action == "back":
                disp["mode"] = "setup"
            elif action and action.startswith("trim:"):
                _, key, delta_str = action.split(":")
                disp["ss"][key] = round(disp["ss"].get(key, 0.0) + float(delta_str), 1)
                _settings.mark_dirty()
            elif action == "mag_cal_open":
                _mag_cal_open("ahrs_setup")
            elif action and action.startswith("set:"):
                _, key, val = action.split(":", 2)
                disp["ss"][key] = val
                _settings.mark_dirty()
            return True

        # ── Connectivity taps ─────────────────────────────────────────────
        if mode == "connectivity_setup":
            action = connectivity_setup_hit(x, y, disp["cs"])
            if action == "back":
                disp["mode"] = "setup"
            elif action and action.startswith("edit:"):
                key = action.split(":", 1)[1]
                disp["kbd_target"] = key
                disp["kbd_prev"]   = "connectivity_setup"
                disp["kbd_buf"]    = _current_str_for_kbd(key, "connectivity_setup")
                disp["kbd_shift"]  = False
                disp["mode"]       = "keyboard"
            elif action == "scan_wifi":
                _do_scan()
            elif action == "apply_wifi":
                disp["cs"]["apply_msg"] = "Applying…"
                def _do_apply():
                    ok, msg = _apply_wifi(disp["cs"]["wifi_ssid"],
                                          disp["cs"]["wifi_pass"])
                    disp["cs"]["apply_msg"] = msg
                threading.Thread(target=_do_apply, daemon=True).start()
            elif action == "test_ahrs":
                disp["cs"]["test_msg"] = "Testing…"
                def _do_test():
                    ok, msg = _test_ahrs_connection(disp["cs"]["ahrs_url"])
                    disp["cs"]["test_msg"] = msg
                    if ok:
                        _restart_sse(disp["cs"]["ahrs_url"])
                threading.Thread(target=_do_test, daemon=True).start()
            return True

        # ── WiFi scan screen taps ─────────────────────────────────────────────
        if mode == "wifi_scan":
            action = wifi_scan_hit(x, y, disp["cs"])
            if action == "back":
                disp["mode"] = "connectivity_setup"
            elif action == "rescan":
                _do_scan()
            elif action and action.startswith("select:"):
                idx  = int(action.split(":", 1)[1])
                nets = disp["cs"].get("scan_nets", [])
                if 0 <= idx < len(nets):
                    net = nets[idx]
                    disp["cs"]["wifi_ssid"] = net["ssid"]
                    disp["cs"]["wifi_pass"] = ""
                    if net.get("secured"):
                        disp["kbd_target"] = "wifi_pass"
                        disp["kbd_prev"]   = "connectivity_setup"
                        disp["kbd_buf"]    = ""
                        disp["kbd_shift"]  = False
                        disp["mode"]       = "keyboard"
                    else:
                        disp["mode"] = "connectivity_setup"
            elif action == "scroll_up":
                disp["cs"]["scan_scroll"] = max(0, disp["cs"].get("scan_scroll", 0) - 1)
            elif action == "scroll_down":
                ws_btn_y = DISPLAY_H - _WS_BTN_H - 8
                list_h   = ws_btn_y - _WS_LIST_Y - 8
                visible  = list_h // _WS_ITEM_H
                max_s    = max(0, len(disp["cs"].get("scan_nets", [])) - visible)
                disp["cs"]["scan_scroll"] = min(max_s, disp["cs"].get("scan_scroll", 0) + 1)
            return True

        # ── System screen taps ────────────────────────────────────────────
        if mode == "system_setup":
            action = system_setup_hit(x, y)
            if action == "back":
                disp["mode"] = "setup"
            elif action == "terrain_data":
                disp["mode"] = "terrain_data"
            elif action == "obstacle_data":
                disp["mode"] = "obstacle_data"
            elif action == "airport_data":
                disp["mode"] = "airport_data"
            elif action == "simulator":
                disp["mode"] = "sim_setup"
            elif action == "reset_defaults":
                for k,v in [("vs0",VS0),("vs1",VS1),("vfe",VFE),("vno",VNO),
                             ("vne",VNE),("va",VA),("vy",VY),("vx",VX)]:
                    disp["fp"][k] = v
                disp["ds"].update(spd_unit="kt", alt_unit="ft", baro_unit="inhg",
                                   brightness=8, night_mode=False)
                disp["ss"].update(pitch_trim=0.0, roll_trim=0.0)
            elif action == "quit":
                _settings.flush()
                pygame.quit()
                sys.exit(0)
            return True

        # ── Sim setup screen taps ─────────────────────────────────────────
        if mode == "sim_setup":
            action = sim_setup_hit(x, y)
            if action == "back":
                disp["mode"] = "system_setup"
            elif action and action.startswith("preset:"):
                disp["sim"]["preset_idx"] = int(action.split(":")[1])
            elif action and action.startswith("cond:"):
                key = action.split(":")[1]
                target_map = {
                    "init_alt": "sim_init_alt",
                    "init_hdg": "sim_init_hdg",
                    "init_spd": "sim_init_spd",
                }
                _open_numpad(target_map[key])
            elif action and action.startswith("sensor_on:"):
                sensor = action.split(":")[1]
                disp["sim"][sensor + "_fail"] = False
            elif action and action.startswith("sensor_fail:"):
                sensor = action.split(":")[1]
                disp["sim"][sensor + "_fail"] = True
            elif action == "start":
                _sim_state = SimFlyState()
                # Pause live AHRS transport so its writes don't clobber the
                # sim's writes to state.  Resumed on exit_sim.
                if _sse_client is not None:
                    _sse_client.paused = True
                disp["mode"] = "pfd"
            elif action == "cancel":
                disp["mode"] = "system_setup"
            return True

        # ── Sim controls overlay taps ─────────────────────────────────────
        if mode == "sim_controls":
            action = sim_controls_hit(x, y)
            if action == "exit_sim":
                _sim_state = None
                # Resume live AHRS transport — sim no longer owns state
                if _sse_client is not None:
                    _sse_client.paused = False
                disp["mode"] = "pfd"
            elif action == "exit_setup":
                # Close the overlay; the sim keeps running.  No state
                # touched — pilot just wants to fly the sim now.
                disp["mode"] = "pfd"
            elif action and action.startswith("sensor_on:"):
                sensor = action.split(":")[1]
                disp["sim"][sensor + "_fail"] = False
            elif action and action.startswith("sensor_fail:"):
                sensor = action.split(":")[1]
                disp["sim"][sensor + "_fail"] = True
            elif action and action.startswith("follow:"):
                disp["sim"]["follow_mode"] = action.split(":", 1)[1]
            # "noop" or None: consume the event either way
            return True

        # ── Nav-confirm modal (Activate Direct to XXXX?) ──────────────────
        if mode == "nav_confirm":
            action = nav_confirm_hit(x, y)
            if action == "activate":
                _nav_confirm_apply()
            elif action == "cancel":
                _nav_confirm_cancel()
            # "noop" / None / outside-panel: consume to keep the modal up
            return True

        # ── Compass calibration wizard taps ──────────────────────────────
        if mode == "mag_cal":
            action = mag_cal_hit(x, y)
            if action == "capture":
                _mag_cal_capture()
            elif action == "restart":
                _mag_cal_restart()
            elif action == "reset":
                _mag_cal_reset()
            elif action == "cancel":
                _mag_cal_close()
            # "noop" / None / outside-panel: keep the modal up
            return True

        # ── Obstacle data screen taps ─────────────────────────────────────
        if mode == "obstacle_data":
            action = obstacle_data_hit(x, y, disp["od"])
            if action == "back":
                disp["mode"] = "system_setup"
            elif action == "cancel":
                disp["od"]["dl_cancel"] = True
            elif action == "download":
                if not disp["od"]["downloading"]:
                    _od_start_download()
            return True

        # ── Airport data screen taps ──────────────────────────────────────
        if mode == "airport_data":
            action = airport_data_hit(x, y, disp["ad"])
            if action == "back":
                disp["mode"] = "system_setup"
            elif action == "cancel":
                disp["ad"]["dl_cancel"] = True
            elif action == "download":
                if not disp["ad"]["downloading"]:
                    _ad_start_download()
            elif isinstance(action, str) and action.startswith("toggle:"):
                key = action.split(":", 1)[1]
                disp["ad"][key] = not disp["ad"].get(key, False)
                _settings.mark_dirty()
            elif action == "nav_direct":
                # Open with empty buf so the first keystroke replaces the
                # current ident, matching the heading/altitude/airspeed
                # numpad UX.  The active ident still shows as the
                # placeholder (resolved via disp["nav"]["ident"] in the
                # keyboard renderer).
                disp["kbd_target"] = "nav_ident"
                disp["kbd_prev"]   = "airport_data"
                disp["kbd_buf"]    = ""
                disp["kbd_error"]  = ""
                disp["mode"]       = "keyboard"
            elif action == "nav_nearest":
                # Same confirmation gate as a typed waypoint — surface
                # the resolved ident so the pilot can verify before the
                # flight plan changes.
                _nav_open_confirm(_nav_lookup_nearest(), "airport_data")
            elif action == "nav_clear":
                _nav_clear()
            return True

        # ── Terrain data screen taps ──────────────────────────────────────
        if mode == "terrain_data":
            action = terrain_data_hit(x, y, disp["td"])
            if action == "back":
                disp["mode"] = "system_setup"
            elif action == "cancel":
                disp["td"]["dl_cancel"] = True
                disp["wd"]["dl_cancel"] = True
            elif action == "current_area":
                if not disp["td"]["downloading"]:
                    _td_start_current_area()
            elif action == "water_masks":
                if not disp["wd"]["downloading"]:
                    _wd_start_download()
            elif action and action.startswith("region:"):
                if not disp["td"]["downloading"]:
                    idx = int(action.split(":")[1])
                    _td_start_download(_TD_REGIONS[idx])
            return True

        # ── Flight Profile screen taps ────────────────────────────────────
        if mode == "flight_profile":
            key = flight_profile_hit(x, y, disp["fp"])
            if key == "__back__":
                disp["mode"] = "setup"
            elif key is not None:
                ftype = next((f[4] for f in _FP_FIELDS if f[0]==key), "num")
                if ftype == "kbd":
                    disp["kbd_target"] = key
                    disp["kbd_prev"]   = "flight_profile"
                    disp["kbd_buf"]    = _current_str_for_kbd(key, "flight_profile")
                    disp["kbd_shift"]  = False
                    disp["mode"]       = "keyboard"
                else:
                    disp["numpad_target"] = key
                    disp["numpad_prev"]   = "flight_profile"
                    disp["numpad_buf"]    = ""
                    disp["mode"]          = "numpad"
            return True

        # ── Keyboard taps ─────────────────────────────────────────────────
        if mode == "keyboard":
            hit = keyboard_hit(x, y)
            if hit:
                lbl, sty = hit
                target  = disp["kbd_target"]
                _CS_MAX = {"wifi_ssid": 32, "wifi_pass": 63, "ahrs_url": 80}
                if disp.get("kbd_prev") == "connectivity_setup":
                    max_len = _CS_MAX.get(target, 32)
                else:
                    max_len = next((f[3] for f in _FP_FIELDS if f[0]==target), 16)
                if sty == 'n':                # character / space
                    ch = ' ' if lbl == 'SPACE' else lbl
                    if len(disp["kbd_buf"]) < max_len:
                        disp["kbd_buf"] += ch
                    disp["kbd_error"] = ""    # any keystroke clears the error
                elif sty == 'del':            # backspace
                    disp["kbd_buf"] = disp["kbd_buf"][:-1]
                    disp["kbd_error"] = ""
                elif sty in ('shift', 'shift_on'):
                    disp["kbd_shift"] = not disp.get("kbd_shift", False)
                elif sty == 'x':              # CANCEL
                    disp["kbd_buf"] = ""
                    disp["kbd_error"] = ""
                    disp["mode"] = disp["kbd_prev"]
                elif sty == 'nrst':           # DIRECT TO NEAREST
                    # Resolve the nearest ident, close the keyboard,
                    # and route through the confirmation modal so the
                    # pilot can verify before the flight plan changes.
                    nearest = _nav_lookup_nearest()
                    prev = disp["kbd_prev"]
                    disp["kbd_buf"] = ""
                    disp["kbd_error"] = ""
                    if not _nav_open_confirm(nearest, prev):
                        # No nearest airport (no fix or empty DB) —
                        # just close the keyboard.
                        disp["mode"] = prev
                elif sty == 'clrfp':          # CANCEL FLIGHT PLAN
                    _nav_clear()
                    disp["kbd_buf"] = ""
                    disp["kbd_error"] = ""
                    disp["mode"] = disp["kbd_prev"]
                elif sty == 'appr':           # APPR — open runway picker
                    # Resolve the airport ident: typed buf wins over the
                    # currently-active D2 ident.  Validate against the
                    # airport DB (matches ENTER's path), and either
                    # surface "UNKNOWN WAYPOINT" or jump to the picker.
                    buf = disp["kbd_buf"].strip().upper()
                    cur_ident = disp.get("nav", {}).get("ident", "")
                    target_ident = buf or cur_ident
                    if not target_ident:
                        disp["kbd_error"] = "ENTER AIRPORT FIRST"
                    elif buf and not _nav_lookup_ident(buf):
                        disp["kbd_error"] = f"UNKNOWN WAYPOINT  {buf}"
                    elif not _ident_has_runways(target_ident):
                        disp["kbd_error"] = f"NO RUNWAYS  {target_ident}"
                    else:
                        # If the typed buf is a fresh airport, activate
                        # the D2 first so the picker sees it as the
                        # current airport.  Then jump to the picker.
                        if buf and buf != cur_ident:
                            _nav_set_by_ident(buf)
                        disp["kbd_buf"] = ""
                        disp["kbd_error"] = ""
                        disp["mode"] = "approach_select"
                elif sty == 'ok':             # ENTER
                    buf = disp["kbd_buf"].strip()
                    if target == "nav_ident":
                        # Three paths:
                        #   1. Buffer empty AND a waypoint is already
                        #      active → "keep what's there but
                        #      re-activate so the magenta line redraws
                        #      from my current position".  Confirm.
                        #   2. Buffer has text and resolves to a known
                        #      airport → confirm.
                        #   3. Buffer has text but doesn't resolve →
                        #      stay on the keyboard and surface
                        #      "UNKNOWN WAYPOINT" so the pilot can fix
                        #      the typo without retyping from scratch.
                        cur_ident = disp.get("nav", {}).get("ident", "")
                        if buf:
                            candidate = buf.upper()
                            if _nav_lookup_ident(candidate):
                                disp["nav_confirm_ident"] = candidate
                                disp["nav_confirm_prev"]  = disp["kbd_prev"]
                                disp["kbd_buf"] = ""
                                disp["kbd_error"] = ""
                                disp["mode"] = "nav_confirm"
                            else:
                                disp["kbd_error"] = f"UNKNOWN WAYPOINT  {candidate}"
                            return True
                        if cur_ident:
                            disp["nav_confirm_ident"] = cur_ident
                            disp["nav_confirm_prev"]  = disp["kbd_prev"]
                            disp["kbd_buf"] = ""
                            disp["kbd_error"] = ""
                            disp["mode"] = "nav_confirm"
                            return True
                        # Empty buf with no active waypoint → close.
                        disp["kbd_buf"] = ""
                        disp["kbd_error"] = ""
                        disp["mode"] = disp["kbd_prev"]
                        return True
                    elif buf:
                        if disp["kbd_prev"] == "connectivity_setup":
                            disp["cs"][target] = buf
                            # Changing AHRS URL live-restarts the SSE stream
                            if target == "ahrs_url":
                                _restart_sse(buf)
                        else:
                            disp["fp"][target] = buf
                        _settings.mark_dirty()
                    disp["kbd_buf"] = ""
                    disp["mode"] = disp["kbd_prev"]
            return True

        # ── Numpad taps ───────────────────────────────────────────────────
        if mode == "numpad":
            hit = numpad_hit(x, y)
            if hit:
                lbl, sty = hit
                target = disp["numpad_target"]
                _NP_MAX = {"baro_hpa": 4}   # targets needing >3 digits
                max_digits = _NP_MAX.get(target, 3)
                if sty == 'n':                # digit
                    if len(disp["numpad_buf"]) < max_digits:
                        disp["numpad_buf"] += lbl
                elif sty == 'del':            # backspace
                    disp["numpad_buf"] = disp["numpad_buf"][:-1]
                elif sty == 'x':              # CANCEL
                    disp["mode"] = disp["numpad_prev"]
                    disp["numpad_buf"] = ""
                elif sty == 'ok':             # ENTER
                    buf = disp["numpad_buf"]
                    if buf:
                        val = int(buf)
                        # Unit factors: user types values in whatever unit is
                        # currently displayed, but bugs are stored canonically
                        # (kt for speed, ft for altitude).  Divide by the
                        # factor to convert display → canonical.
                        ds = disp["ds"]
                        spd_factor = {"kt": 1.0, "mph": 1.15078,
                                      "kph": 1.852}.get(ds.get("spd_unit", "kt"), 1.0)
                        alt_factor = {"ft": 1.0,
                                      "m":  0.3048}.get(ds.get("alt_unit", "ft"), 1.0)
                        if target == "alt_bug":
                            # Input is hundreds of display-unit altitude.
                            disp["alt_bug"] = float(val * 100) / alt_factor
                        elif target == "hdg_bug":
                            disp["hdg_bug"] = float(val % 360)
                        elif target == "trk_bug":
                            disp["trk_bug"] = float(val % 360)
                        elif target == "spd_bug":
                            disp["spd_bug"] = float(val) / spd_factor
                        elif target == "baro_hpa":
                            baro_unit = ds.get("baro_unit", "inhg")
                            if baro_unit == "hpa":
                                new_hpa = float(val)
                            else:   # inHg: 4 digits → insert decimal after 2
                                new_hpa = round(val / 100.0 * 33.8639, 2)
                            # baro_hpa is a user-set value.  Write it into disp
                            # (immediate UI feedback), into state (so other
                            # readers see it) and push to the Pico W so the
                            # firmware recomputes altitude against the new QNH.
                            disp["baro_hpa"] = new_hpa
                            with _state_lock:
                                state["baro_hpa"] = new_hpa
                            _push_baro_to_pico(new_hpa)
                        elif target == "sim_init_alt":
                            disp["sim"]["init_alt"] = float(val * 100) / alt_factor
                        elif target == "sim_init_hdg":
                            disp["sim"]["init_hdg"] = float(val % 360)
                        elif target == "sim_init_spd":
                            # sim_init_spd title is explicitly "(kt)" so no
                            # conversion here — entered value is already kt.
                            disp["sim"]["init_spd"] = float(val)
                        elif target in disp["fp"]:   # V-speed field (always kt)
                            disp["fp"][target] = val
                        _settings.mark_dirty()
                    disp["mode"] = disp["numpad_prev"]
                    disp["numpad_buf"] = ""
            return True

        # ── PFD taps ──────────────────────────────────────────────────────
        # Tap on SIM button → open sim controls overlay (which has EXIT SIM)
        if _sim_state is not None and mode == "pfd":
            if (_SIM_EXIT_X <= x <= _SIM_EXIT_X + _SIM_EXIT_W and
                    _SIM_EXIT_Y <= y <= _SIM_EXIT_Y + _SIM_EXIT_H):
                disp["mode"] = "sim_controls"
                return True

        # Tap on the CDI strip → open keyboard for waypoint entry.  Strip
        # is rendered whenever GPS is fixed (with or without an active
        # waypoint), and the hit region matches the translucent backplate.
        nv = disp.get("nav", {})
        if mode == "pfd" and disp.get("gps_ok", False):
            _cdi_bar_w = max(140, int(DISPLAY_W * 0.20))
            _cdi_bar_y = HDG_Y - 50
            _cdi_l = CX - _cdi_bar_w // 2 - 18
            _cdi_r = CX + _cdi_bar_w // 2 + 18
            _cdi_t = _cdi_bar_y - 32
            _cdi_b = _cdi_bar_y + 12
            if _cdi_l <= x <= _cdi_r and _cdi_t <= y <= _cdi_b:
                # Empty buf → first keystroke replaces the current ident
                # (matches the heading/altitude/airspeed numpad behaviour).
                # Existing ident still appears as the keyboard placeholder.
                disp["kbd_target"] = "nav_ident"
                disp["kbd_prev"]   = "pfd"
                disp["kbd_buf"]    = ""
                disp["kbd_error"]  = ""
                disp["mode"]       = "keyboard"
                return True

        # Tap on the moving-map inset → cycle range one step.  Right
        # half zooms IN (smaller range), left half zooms OUT.
        if (mode == "pfd" and _last_map_rect is not None
                and disp["ds"].get("map_enabled", False)):
            mrx, mry, mrw, mrh = _last_map_rect
            if mrx <= x <= mrx + mrw and mry <= y <= mry + mrh:
                cur = int(disp["ds"].get("map_zoom_nm", 5))
                if x >= mrx + mrw / 2:
                    disp["ds"]["map_zoom_nm"] = _map_mod.zoom_in(cur)
                else:
                    disp["ds"]["map_zoom_nm"] = _map_mod.zoom_out(cur)
                _settings.mark_dirty()
                return True

        # Tap on alt bug button (top of alt tape) — hit region extends to HDG_H
        # for easy touch even though the visual button only fills TAPE_TOP
        if ALT_X <= x <= DISPLAY_W and 0 <= y <= HDG_H:
            _open_numpad("alt_bug")
            return True
        # Tap on GS bug button (top of speed tape) — same extended hit region
        if SPD_X <= x <= SPD_X + SPD_W and 0 <= y <= HDG_H:
            _open_numpad("spd_bug")
            return True
        # Tap on speed VR readout (centre of speed tape) → open speed numpad
        if SPD_X <= x <= SPD_X + SPD_W and TAPE_MID - 30 <= y <= TAPE_MID + 30:
            _open_numpad("spd_bug")
            return True
        # Tap on alt VR readout (centre of alt tape) → open alt numpad
        if ALT_X <= x <= DISPLAY_W and TAPE_MID - 30 <= y <= TAPE_MID + 30:
            _open_numpad("alt_bug")
            return True
        # Tap on hdg bug button → open numpad (track bug if in TRK mode)
        if SPD_X <= x <= SPD_X + SPD_W and HDG_Y <= y <= DISPLAY_H:
            _open_numpad(_active_bug_key())
            return True
        # Tap on heading readout box (centre of heading tape) → open hdg numpad
        if CX - 40 <= x <= CX + 40 and HDG_Y - 40 <= y <= HDG_Y:
            _open_numpad(_active_bug_key())
            return True
        # Tap on baro button → open numpad
        if ALT_X <= x <= DISPLAY_W and HDG_Y <= y <= DISPLAY_H:
            _open_numpad("baro_hpa")
            return True
        # Tap on alt tape → adjust alt bug by position
        if ALT_X <= x <= DISPLAY_W and TAPE_TOP <= y <= TAPE_BOT:
            ft = round(disp["alt"] + (TAPE_MID - y) / PX_PER_FT)
            disp["alt_bug"] = round(ft / 100) * 100
        # Tap on heading tape → adjust active bug by position.  In TRK mode
        # the centre reference is GPS track (matching what the box shows),
        # so the bug lands under the finger relative to the displayed value.
        if HDG_Y <= y <= DISPLAY_H:
            off = (x - CX) / PX_PER_DEG
            _bk = _active_bug_key()
            _ref = disp.get("track", disp["yaw"]) if _bk == "trk_bug" else disp["yaw"]
            disp[_bk] = round(_ref + off) % 360

    return True


# ── Setup / numpad screens ────────────────────────────────────────────────────

_SETUP_ITEMS = [
    (0, 0, "FLIGHT PROFILE",  "V-speeds · Aircraft · Tail #"),
    (1, 0, "DISPLAY",         "Units · Brightness · Night mode"),
    (0, 1, "AHRS / SENSORS",  "Trim · Mag cal · Mounting"),
    (1, 1, "CONNECTIVITY",    "WiFi · AHRS link"),
    (0, 2, "SYSTEM",          "Version · Diagnostics · Reset"),
    (1, 2, "EXIT",            "Return to PFD"),
]
_S_MX=15; _S_MY=50; _S_GX=10; _S_GY=12
_S_BW = (DISPLAY_W - 2*_S_MX - _S_GX) // 2
_S_BH = (DISPLAY_H - _S_MY - 14 - 2*_S_GY) // 3
_S_COLS = [_S_MX, _S_MX + _S_BW + _S_GX]
_S_ROWS = [_S_MY, _S_MY + _S_BH + _S_GY, _S_MY + 2*(_S_BH + _S_GY)]


def _setup_button(surf, bx, by, bw, bh, label, subtitle="", exit_btn=False, r=8):
    bg = (28, 6, 6) if exit_btn else (0, 12, 32)
    pygame.draw.rect(surf, bg, (bx, by, bw, bh), border_radius=r)
    glow_h = bh // 5
    for i in range(glow_h):
        t = 1.0 - i / glow_h
        gc = ((int(45+t*55), int(8+t*12),  int(8+t*12)) if exit_btn
              else (int(15+t*35), int(20+t*50), int(40+t*80)))
        pygame.draw.line(surf, gc, (bx+r, by+1+i), (bx+bw-r, by+1+i))
    oc = (200, 55, 55) if exit_btn else WHITE
    pygame.draw.rect(surf, oc, (bx, by, bw, bh), width=2, border_radius=r)
    lh = 22
    total_h = lh + (16 if subtitle else 0)
    ly = by + (bh - total_h) // 2
    _text(surf, label,    19, WHITE,         bold=True, cx=bx+bw//2, cy=ly+lh//2)
    if subtitle:
        _text(surf, subtitle, 11, (155,170,190),           cx=bx+bw//2, cy=ly+lh+10)


# ── Header BACK button — sized to comfortably fit the scaled label font ───────
# On 640×480 the 72 px width is fine; on 1024×600 (FONT_SCALE≈1.25) the arrow
# + "BACK" at 19pt bold exceeds 72 px, so we scale width with the font.
try:
    from config import FONT_SCALE as _FS_FOR_BACK
except ImportError:
    _FS_FOR_BACK = 1.0
_BACK_BX = 8
_BACK_BY = 6
_BACK_BW = max(72, int(72 * _FS_FOR_BACK + 0.5))
_BACK_BH = 31

# Bug tap-button height — match the heading tape height for easy touch
_BUG_BTN_H = HDG_H


def _draw_back_button(surf):
    _setup_button(surf, _BACK_BX, _BACK_BY, _BACK_BW, _BACK_BH,
                  "\u2190 BACK", r=5)


def _back_hit(x, y):
    return (_BACK_BX <= x <= _BACK_BX + _BACK_BW
            and _BACK_BY <= y <= _BACK_BY + _BACK_BH)


def draw_setup_screen(surf):
    """Full-screen setup main menu — entered via 2-finger hold."""
    surf.fill((0, 8, 22))
    pygame.draw.rect(surf, (0, 18, 45), (0, 0, DISPLAY_W, 44))
    pygame.draw.line(surf, WHITE, (0, 43), (DISPLAY_W-1, 43), 1)
    _text(surf, "SETUP", 22, WHITE, bold=True, cx=DISPLAY_W//2, cy=22)
    _text(surf, "2-finger hold to enter  ·  EXIT to return", 10, (110,120,140),
          x=DISPLAY_W-340, y=15)
    for col, row, lbl, sub in _SETUP_ITEMS:
        exit_btn = (lbl == "EXIT")
        _setup_button(surf, _S_COLS[col], _S_ROWS[row], _S_BW, _S_BH,
                      lbl, sub, exit_btn)


def setup_hit(x, y):
    """Return index 0–5 of the tapped setup button, or None."""
    for idx, (col, row, *_) in enumerate(_SETUP_ITEMS):
        bx = _S_COLS[col]; by = _S_ROWS[row]
        if bx <= x <= bx+_S_BW and by <= y <= by+_S_BH:
            return idx
    return None


# Numpad constants — shared between draw and hit-test
_NP_KEYS = [
    [('7','n'), ('8','n'), ('9','n')],
    [('4','n'), ('5','n'), ('6','n')],
    [('1','n'), ('2','n'), ('3','n')],
    # Bottom row: 4 keys share the same 384-px row width as the 3-col rows above.
    # Each tuple's optional 3rd element overrides the default _NP_PW width.
    [('CANCEL','x',87), ('0','n',87), ('\u232b','del',87), ('ENTER','ok',87)],
]
_NP_PW=120; _NP_PH=64; _NP_GX=12; _NP_GY=10
_NP_TW = 3*_NP_PW + 2*_NP_GX   # 384
_NP_X0 = (DISPLAY_W - _NP_TW) // 2  # 128
_NP_Y0 = 118


def _np_row_layout(row):
    """Return [(label, style, bx, bw), ...] for one numpad row, centered.
    Keys may be 2-tuples (label, style) or 3-tuples (label, style, width);
    2-tuples default to _NP_PW."""
    widths = [(k[2] if len(k) >= 3 else _NP_PW) for k in row]
    total = sum(widths) + _NP_GX * (len(row) - 1)
    x = (DISPLAY_W - total) // 2
    out = []
    for i, k in enumerate(row):
        out.append((k[0], k[1], x, widths[i]))
        x += widths[i] + _NP_GX
    return out


def _numpad_key(surf, bx, by, bw, label, style, r=8):
    if style == 'x':
        bg=(28,6,6);  oc=(200,55,55); tc=(220,80,80)
    elif style == 'ok':
        bg=(5,25,10); oc=(50,200,80); tc=(60,220,90)
    elif style == 'del':
        bg=(30,18,5); oc=(200,140,40);tc=(220,160,50)
    else:
        bg=(0,12,32); oc=WHITE;       tc=WHITE
    pygame.draw.rect(surf, bg, (bx, by, bw, _NP_PH), border_radius=r)
    glow_h = _NP_PH // 5
    for i in range(glow_h):
        t = 1.0 - i / glow_h
        gc = ((int(45+t*55), int(8+t*12),  int(8+t*12))  if style=='x'  else
              (int(5+t*15),  int(40+t*60), int(10+t*20)) if style=='ok' else
              (int(40+t*50), int(25+t*35), int(5+t*10))  if style=='del' else
              (int(15+t*30), int(20+t*45), int(40+t*75)))
        pygame.draw.line(surf, gc, (bx+r, by+1+i), (bx+bw-r, by+1+i))
    pygame.draw.rect(surf, oc, (bx, by, bw, _NP_PH), width=2, border_radius=r)
    fs = 18 if len(label) > 3 else 20
    _text(surf, label, fs, tc, bold=True, cx=bx+bw//2, cy=by+_NP_PH//2)


def _fmt_decimal(digits: str, decimal_after: int) -> str:
    """Insert a decimal point into a digit string.
    '2992', decimal_after=2 → '29.92'
    '29',   decimal_after=2 → '29'   (still typing)
    """
    if decimal_after and len(digits) > decimal_after:
        return digits[:decimal_after] + "." + digits[decimal_after:]
    return digits


def draw_numpad(surf, title, current_val, entered="", suffix="",
                transparent=False, decimal_after=0):
    """Full-screen numeric entry pad.
    suffix:        appended in dim cyan (e.g. '00' for alt_bug).
    transparent:   skip background fill; semi-opaque header over live PFD.
    decimal_after: auto-insert '.' after this many digits (0 = no decimal).
                   current_val should be the integer form (e.g. 2992 for 29.92).
    """
    if not transparent:
        surf.fill((0, 8, 22))
    hdr = pygame.Surface((DISPLAY_W, 44), pygame.SRCALPHA)
    hdr.fill((0, 18, 45, 220 if transparent else 255))
    surf.blit(hdr, (0, 0))
    pygame.draw.line(surf, WHITE, (0, 43), (DISPLAY_W-1, 43), 1)
    _text(surf, title, 18, WHITE, bold=True, cx=DISPLAY_W//2, cy=22)

    # Value display box
    raw_str  = entered if entered else str(current_val)
    base_str = _fmt_decimal(raw_str, decimal_after) if decimal_after else raw_str
    pygame.draw.rect(surf, (0,15,38), (80, 50, DISPLAY_W-161, 50), border_radius=6)
    pygame.draw.rect(surf, WHITE,     (80, 50, DISPLAY_W-161, 50), width=1, border_radius=6)
    if suffix:
        f32 = _get_font(32, bold=True)
        bw  = f32.size(base_str)[0]
        sw  = f32.size(suffix)[0]
        bx_str = DISPLAY_W//2 - (bw + sw)//2
        surf.blit(f32.render(base_str, True, CYAN), (bx_str, 59))
        surf.blit(f32.render(suffix,   True, (0,100,100)), (bx_str + bw, 59))
    else:
        _text(surf, base_str, 32, CYAN, bold=True, cx=DISPLAY_W//2, cy=75)

    # "Current:" hint — format with decimal too
    cur_raw = _fmt_decimal(str(current_val), decimal_after) if decimal_after else str(current_val)
    cur_display = f"{cur_raw}{suffix}" if suffix else cur_raw
    _text(surf, f"Current: {cur_display}", 10, (110,120,140), cx=DISPLAY_W//2, cy=108)
    for ri, row in enumerate(_NP_KEYS):
        by = _NP_Y0 + ri*(_NP_PH+_NP_GY)
        for lbl, sty, bx, bw in _np_row_layout(row):
            _numpad_key(surf, bx, by, bw, lbl, sty)


def numpad_hit(x, y):
    """Return (label, style) of the tapped numpad key, or None."""
    for ri, row in enumerate(_NP_KEYS):
        by = _NP_Y0 + ri*(_NP_PH+_NP_GY)
        if not (by <= y <= by+_NP_PH):
            continue
        for lbl, sty, bx, bw in _np_row_layout(row):
            if bx <= x <= bx+bw:
                return (lbl, sty)
    return None


# ── Flight Profile screen ────────────────────────────────────────────────────

_FP_FIELDS = [
    ("tail",  "TAIL NUMBER",          "",   8, "kbd"),
    ("actype","AIRCRAFT TYPE",        "",   8, "kbd"),
    ("vs0",   "VS0 \u2014 Stall flaps",  "kt", 3, "num"),
    ("vs1",   "VS1 \u2014 Stall clean",  "kt", 3, "num"),
    ("vfe",   "VFE \u2014 Max flaps",    "kt", 3, "num"),
    ("vno",   "VNO \u2014 Max cruise",   "kt", 3, "num"),
    ("vne",   "VNE \u2014 Never exceed", "kt", 3, "num"),
    ("va",    "VA  \u2014 Manoeuvre",    "kt", 3, "num"),
    ("vy",    "VY  \u2014 Best rate",    "kt", 3, "num"),
    ("vx",    "VX  \u2014 Best angle",   "kt", 3, "num"),
]

_FP_MX=12; _FP_GAP=8; _FP_H1=58; _FP_H2=48; _FP_Y0=50


def _fp_field(surf, bx, by, bw, bh, label, value, units="", r=6):
    pygame.draw.rect(surf, (0, 12, 32), (bx, by, bw, bh), border_radius=r)
    glow_h = bh // 5
    for i in range(glow_h):
        t = 1.0 - i / glow_h
        gc = (int(15+t*35), int(20+t*50), int(40+t*80))
        pygame.draw.line(surf, gc, (bx+r, by+1+i), (bx+bw-r, by+1+i))
    pygame.draw.rect(surf, WHITE, (bx, by, bw, bh), width=2, border_radius=r)
    _text(surf, label, 11, (155,170,190), x=bx+10, y=by+6)
    val_str = str(value) if value not in (None, "", 0) else "---"
    if units and val_str != "---":
        val_str = f"{val_str} {units}"
    _text(surf, val_str, 18, WHITE, bold=True,
          cx=bx+bw - _get_font(18,bold=True).size(val_str)[0]//2 - 12,
          cy=by+bh//2)


def draw_flight_profile(surf, fp_vals):
    """Full-screen Flight Profile setup screen."""
    surf.fill((0, 8, 22))
    pygame.draw.rect(surf, (0, 18, 45), (0, 0, DISPLAY_W, 44))
    pygame.draw.line(surf, WHITE, (0, 43), (DISPLAY_W-1, 43), 1)
    _draw_back_button(surf)
    _text(surf, "FLIGHT PROFILE", 20, WHITE, bold=True, cx=DISPLAY_W//2, cy=22)

    MX=_FP_MX; GAP=_FP_GAP
    FW = DISPLAY_W - 2*MX

    # Aircraft info (full-width)
    y = _FP_Y0
    for key in ("tail", "actype"):
        _, label, units, _, _ = next(f for f in _FP_FIELDS if f[0]==key)
        _fp_field(surf, MX, y, FW, _FP_H1, label, fp_vals.get(key,"---"), units)
        y += _FP_H1 + GAP

    # Section divider
    y += 2
    pygame.draw.line(surf, (40,60,90), (MX, y), (DISPLAY_W-MX, y), 1)
    y += 4
    _text(surf, "V-SPEEDS  (knots) \u2014 tap to edit", 11, (120,140,165), x=MX, y=y)
    y += 18

    # V-speed grid: 4 rows × 2 cols
    V_KEYS = [k for k,*_ in _FP_FIELDS if k not in ("tail","actype")]
    BW = (FW - GAP) // 2
    BH = (DISPLAY_H - y - GAP*3 - 4) // 4
    COLS = [MX, MX+BW+GAP]
    for i, key in enumerate(V_KEYS):
        _, label, units, _, _ = next(f for f in _FP_FIELDS if f[0]==key)
        bx = COLS[i%2]; by = y + (i//2)*(BH+GAP)
        _fp_field(surf, bx, by, BW, BH, label, fp_vals.get(key,"---"), units)


def flight_profile_hit(x, y, fp_vals):
    """Return the field key tapped, or None."""
    MX=_FP_MX; GAP=_FP_GAP; FW=DISPLAY_W-2*MX
    # BACK button
    if _back_hit(x, y):
        return "__back__"
    # Aircraft fields
    fy = _FP_Y0
    for key in ("tail","actype"):
        if MX<=x<=MX+FW and fy<=y<=fy+_FP_H1:
            return key
        fy += _FP_H1+GAP
    # V-speed grid
    fy += 26   # divider + label
    V_KEYS = [k for k,*_ in _FP_FIELDS if k not in ("tail","actype")]
    BW = (FW-GAP)//2; BH = (DISPLAY_H-fy-GAP*3-4)//4
    COLS = [MX, MX+BW+GAP]
    for i, key in enumerate(V_KEYS):
        bx=COLS[i%2]; by=fy+(i//2)*(BH+GAP)
        if bx<=x<=bx+BW and by<=y<=by+BH:
            return key
    return None


# ── Keyboard screen ────────────────────────────────────────────────────────────

# Normal layout: uppercase letters + digits.
_KB_ROWS_NORMAL = [
    [('1',60,'n'),('2',60,'n'),('3',60,'n'),('4',60,'n'),('5',60,'n'),
     ('6',60,'n'),('7',60,'n'),('8',60,'n'),('9',60,'n'),('0',60,'n')],
    [('Q',60,'n'),('W',60,'n'),('E',60,'n'),('R',60,'n'),('T',60,'n'),
     ('Y',60,'n'),('U',60,'n'),('I',60,'n'),('O',60,'n'),('P',60,'n')],
    [('A',60,'n'),('S',60,'n'),('D',60,'n'),('F',60,'n'),('G',60,'n'),
     ('H',60,'n'),('J',60,'n'),('K',60,'n'),('L',60,'n')],
    [('Z',60,'n'),('X',60,'n'),('C',60,'n'),('V',60,'n'),('B',60,'n'),
     ('N',60,'n'),('M',60,'n'),('.',60,'n'),(':',60,'n'),('\u232b',60,'del')],
    # \u21e7 (shift arrow) replaces hyphen; SPACE narrowed 20 px to compensate.
    [('CANCEL',108,'x'),('\u21e7',80,'shift'),('SPACE',212,'n'),('ENTER',108,'ok')],
]
# Shift layout: lowercase letters + symbol row instead of digits.
_KB_ROWS_SHIFT = [
    [('!',60,'n'),('@',60,'n'),('#',60,'n'),('$',60,'n'),('%',60,'n'),
     ('^',60,'n'),('&',60,'n'),('*',60,'n'),('(',60,'n'),(')',60,'n')],
    [('q',60,'n'),('w',60,'n'),('e',60,'n'),('r',60,'n'),('t',60,'n'),
     ('y',60,'n'),('u',60,'n'),('i',60,'n'),('o',60,'n'),('p',60,'n')],
    [('a',60,'n'),('s',60,'n'),('d',60,'n'),('f',60,'n'),('g',60,'n'),
     ('h',60,'n'),('j',60,'n'),('k',60,'n'),('l',60,'n')],
    [('z',60,'n'),('x',60,'n'),('c',60,'n'),('v',60,'n'),('b',60,'n'),
     ('n',60,'n'),('m',60,'n'),('-',60,'n'),('_',60,'n'),('\u232b',60,'del')],
    [('CANCEL',108,'x'),('\u21e7',80,'shift_on'),('SPACE',212,'n'),('ENTER',108,'ok')],
]

def _current_kb_rows():
    return _KB_ROWS_SHIFT if disp.get('kbd_shift') else _KB_ROWS_NORMAL

_KB_ROW_H=66; _KB_GAP_Y=6; _KB_GAP_X=4; _KB_Y0=112

# Nav-ident extras row: two big action buttons below the QWERTY when the
# keyboard is open for waypoint entry.  Only rendered/hit-tested on
# displays tall enough to fit them — short displays (480 px) keep the
# bare keyboard and the pilot uses the usual CANCEL/DONE flow.
_KB_NAV_BTN_Y = _KB_Y0 + 5 * _KB_ROW_H + 4 * _KB_GAP_Y + 12
_KB_NAV_BTN_H = 70


def _kb_nav_extras_visible():
    return (disp.get("kbd_target") == "nav_ident"
            and DISPLAY_H >= _KB_NAV_BTN_Y + _KB_NAV_BTN_H + 6)


def _kb_nav_extras_geometry():
    """(x positions, btn_w) for the three nav-ident extras buttons:
    DIRECT TO NEAREST · CANCEL FLIGHT PLAN · APPR."""
    pad = 12
    gap = 8
    btn_w = (DISPLAY_W - 2 * pad - 2 * gap) // 3
    bx_l = pad
    bx_m = pad + btn_w + gap
    bx_r = pad + 2 * (btn_w + gap)
    return bx_l, bx_m, bx_r, btn_w


def _kb_row_x0(row):
    total = sum(w for _,w,_ in row) + _KB_GAP_X*(len(row)-1)
    return (DISPLAY_W - total) // 2


def _kb_key(surf, bx, by, bw, bh, label, style, r=6):
    if style=='x':
        bg=(28,6,6);  oc=(200,55,55); tc=(220,80,80)
    elif style=='ok':
        bg=(5,25,10); oc=(50,200,80); tc=(60,220,90)
    elif style=='del':
        bg=(30,18,5); oc=(200,140,40);tc=(220,160,50)
    elif style=='shift':
        bg=(10,20,45); oc=(90,130,200); tc=(110,160,230)
    elif style=='shift_on':
        bg=(45,32,5);  oc=(220,160,40); tc=(255,200,60)
    else:
        bg=(0,12,32); oc=WHITE;       tc=WHITE
    pygame.draw.rect(surf, bg, (bx,by,bw,bh), border_radius=r)
    glow_h = bh//5
    for i in range(glow_h):
        t = 1.0-i/glow_h
        gc=((int(45+t*55),int(8+t*12),int(8+t*12))  if style=='x'  else
            (int(5+t*15),int(40+t*60),int(10+t*20)) if style=='ok' else
            (int(40+t*50),int(25+t*35),int(5+t*10)) if style=='del'else
            (int(15+t*30),int(20+t*45),int(40+t*75)))
        pygame.draw.line(surf, gc, (bx+r,by+1+i),(bx+bw-r,by+1+i))
    pygame.draw.rect(surf, oc, (bx,by,bw,bh), width=2, border_radius=r)
    fs = 13 if len(label)>2 else 18
    _text(surf, label, fs, tc, bold=True, cx=bx+bw//2, cy=by+bh//2)


def draw_keyboard(surf, title, current_val, entered="", transparent=False,
                  error=""):
    """Full-screen QWERTY keyboard for text entry."""
    if not transparent:
        surf.fill((0,8,22))
    hdr = pygame.Surface((DISPLAY_W, 44), pygame.SRCALPHA)
    hdr.fill((0, 18, 45, 220 if transparent else 255))
    surf.blit(hdr, (0, 0))
    pygame.draw.line(surf,WHITE,(0,43),(DISPLAY_W-1,43),1)
    _text(surf,title,17,WHITE,bold=True,cx=DISPLAY_W//2,cy=22)
    disp_str = (entered if entered else str(current_val)) + "\u2502"
    pygame.draw.rect(surf,(0,15,38),(10,50,DISPLAY_W-21,50),border_radius=6)
    pygame.draw.rect(surf,WHITE,(10,50,DISPLAY_W-21,50),width=1,border_radius=6)
    _text(surf,disp_str,28,CYAN,bold=True,cx=DISPLAY_W//2,cy=75)
    if error:
        # Error overrides the "Current:" hint so the pilot's eye lands on
        # the problem.  Cleared on the next keystroke or backspace.
        _text(surf, error, 12, (255, 90, 90), bold=True,
              cx=DISPLAY_W//2, cy=104)
    else:
        _text(surf, f"Current: {current_val}", 10, (110, 120, 140),
              cx=DISPLAY_W//2, cy=104)
    y = _KB_Y0
    for row in _current_kb_rows():
        x = _kb_row_x0(row)
        for label,kw,style in row:
            _kb_key(surf,x,y,kw,_KB_ROW_H,label,style)
            x += kw+_KB_GAP_X
        y += _KB_ROW_H+_KB_GAP_Y

    # Nav-ident extras: jump straight to NEAREST, wipe the active flight
    # plan, or open the approach runway picker — all without typing.
    # Only on tall enough displays.
    if _kb_nav_extras_visible():
        bx_l, bx_m, bx_r, btn_w = _kb_nav_extras_geometry()
        # Resolve the nearest ident so the pilot sees what they're
        # about to activate before tapping.  Empty string falls back
        # to the generic label when there's no fix / no airports.
        nrst = _nav_lookup_nearest()
        nrst_lbl = f"DIRECT TO {nrst}" if nrst else "DIRECT TO NEAREST"
        _action_btn(surf, bx_l, _KB_NAV_BTN_Y, btn_w, _KB_NAV_BTN_H,
                    nrst_lbl, "ok")
        _action_btn(surf, bx_m, _KB_NAV_BTN_Y, btn_w, _KB_NAV_BTN_H,
                    "CANCEL FLIGHT PLAN", "danger")
        _action_btn(surf, bx_r, _KB_NAV_BTN_Y, btn_w, _KB_NAV_BTN_H,
                    "APPR", "normal")


def keyboard_hit(x, y):
    """Return (label, style) of the tapped key, or None.

    Style 'nrst' / 'clrfp' are synthetic — emitted by the nav-ident
    extras buttons (not part of _KB_ROWS).  The dispatcher in the main
    event loop handles them by activating the nearest airport or
    clearing the active waypoint, then closing the keyboard."""
    if (_kb_nav_extras_visible()
            and _KB_NAV_BTN_Y <= y <= _KB_NAV_BTN_Y + _KB_NAV_BTN_H):
        bx_l, bx_m, bx_r, btn_w = _kb_nav_extras_geometry()
        if bx_l <= x <= bx_l + btn_w:
            return ("NRST", "nrst")
        if bx_m <= x <= bx_m + btn_w:
            return ("CLRFP", "clrfp")
        if bx_r <= x <= bx_r + btn_w:
            return ("APPR", "appr")
    ky = _KB_Y0
    for row in _current_kb_rows():
        if ky <= y <= ky+_KB_ROW_H:
            kx = _kb_row_x0(row)
            for label,kw,style in row:
                if kx <= x <= kx+kw:
                    return (label, style)
                kx += kw+_KB_GAP_X
        ky += _KB_ROW_H+_KB_GAP_Y
    return None


# ── Sub-setup screens (Display · AHRS · Connectivity · System) ───────────────

_SS_MX  = 12     # side margin
_SS_Y0  = 52     # first row top (44px title bar + 8px gap)
_SS_RH  = 62     # row height (62 lets 6 rows fit in 480px)
_SS_GAP = 6      # gap between rows


def _ss_row_y(i):
    return _SS_Y0 + i * (_SS_RH + _SS_GAP)


def _screen_header(surf, title):
    surf.fill((0, 8, 22))
    pygame.draw.rect(surf, (0, 18, 45), (0, 0, DISPLAY_W, 44))
    pygame.draw.line(surf, WHITE, (0, 43), (DISPLAY_W-1, 43), 1)
    _draw_back_button(surf)
    _text(surf, title, 20, WHITE, bold=True, cx=DISPLAY_W//2, cy=22)


def _setting_row(surf, row_i, label, sub="", _y_override=None):
    """Draw settings row background + label. Returns (bx, by, bw, bh)."""
    bx = _SS_MX; by = _y_override if _y_override is not None else _ss_row_y(row_i)
    bw = DISPLAY_W - 2*_SS_MX; bh = _SS_RH
    pygame.draw.rect(surf, (0, 12, 32), (bx, by, bw, bh), border_radius=6)
    gh = bh // 6
    for i in range(gh):
        t = 1.0 - i/gh
        gc = (int(15+t*25), int(20+t*40), int(40+t*65))
        pygame.draw.line(surf, gc, (bx+6, by+1+i), (bx+bw-6, by+1+i))
    pygame.draw.rect(surf, (55, 75, 105), (bx, by, bw, bh), width=1, border_radius=6)
    _text(surf, label, 14, WHITE, bold=True, x=bx+14, y=by+10)
    if sub:
        _text(surf, sub, 10, (120, 135, 155), x=bx+14, y=by+32)
    return bx, by, bw, bh


def _seg_btn(surf, bx, by, bw, bh, label, active, r=5):
    """Segmented-control button — CYAN highlight when active."""
    bg = (0, 55, 65) if active else (0, 10, 25)
    oc = CYAN        if active else (50, 68, 92)
    tc = CYAN        if active else (130, 148, 168)
    pygame.draw.rect(surf, bg, (bx, by, bw, bh), border_radius=r)
    if active:
        gh = bh // 4
        for i in range(gh):
            t = 1.0 - i/gh
            gc = (int(t*20), int(60+t*40), int(70+t*50))
            pygame.draw.line(surf, gc, (bx+r, by+1+i), (bx+bw-r, by+1+i))
    pygame.draw.rect(surf, oc, (bx, by, bw, bh), width=2, border_radius=r)
    _text(surf, label, 14, tc, bold=active, cx=bx+bw//2, cy=by+bh//2)


def _step_btn(surf, bx, by, bw, bh, label):
    """+/- stepper button."""
    if label == "+":
        bg=(8,28,12); oc=(50,180,70); tc=(70,220,90)
    else:
        bg=(30,12,12); oc=(180,50,50); tc=(220,80,80)
    pygame.draw.rect(surf, bg, (bx, by, bw, bh), border_radius=5)
    pygame.draw.rect(surf, oc, (bx, by, bw, bh), width=2, border_radius=5)
    _text(surf, label, 20, tc, bold=True, cx=bx+bw//2, cy=by+bh//2)


def _action_btn(surf, bx, by, bw, bh, label, style="normal", r=6):
    """Standalone action button (normal / ok / warn / danger)."""
    if style == "danger":
        bg=(35,5,5);   oc=(200,40,40);  tc=RED
    elif style == "warn":
        bg=(30,20,5);  oc=(200,140,40); tc=YELLOW
    elif style == "ok":
        bg=(5,28,10);  oc=(40,180,60);  tc=(60,220,80)
    else:
        bg=(0,18,45);  oc=WHITE;        tc=WHITE
    pygame.draw.rect(surf, bg, (bx, by, bw, bh), border_radius=r)
    gh = bh // 5
    for i in range(gh):
        t = 1.0 - i/gh
        if   style == "danger": gc=(int(bg[0]+t*40),int(bg[1]+t*10),int(bg[2]+t*10))
        elif style == "warn":   gc=(int(bg[0]+t*35),int(bg[1]+t*25),int(bg[2]+t*5))
        elif style == "ok":     gc=(int(bg[0]+t*10),int(bg[1]+t*35),int(bg[2]+t*10))
        else:                   gc=(int(bg[0]+t*15),int(bg[1]+t*25),int(bg[2]+t*50))
        pygame.draw.line(surf, gc, (bx+r, by+1+i), (bx+bw-r, by+1+i))
    pygame.draw.rect(surf, oc, (bx, by, bw, bh), width=2, border_radius=r)
    _text(surf, label, 15, tc, bold=True, cx=bx+bw//2, cy=by+bh//2)


# ── Display settings screen ───────────────────────────────────────────────────

_DSP_BTN_H = 40    # control button height inside row
_DSP_BTN_G = 6     # gap between buttons
_DSP_SW    = 40    # stepper +/- button width
_DSP_VW    = 70    # stepper value-display box width

_DSP_ROWS = [
    # (key, label, sub, opts_vals, opts_labels, btn_w)   None → stepper
    ("spd_unit",   "SPEED UNITS",  "Knots · Miles · Km/h",
     ["kt","mph","kph"], ["KT","MPH","KPH"], 80),
    ("alt_unit",   "ALTITUDE",     "Feet or Metres",
     ["ft","m"],         ["FT","M"],         100),
    ("baro_unit",  "PRESSURE",     "Inches Hg or hPa",
     ["inhg","hpa"],     ["inHg","hPa"],     100),
    ("brightness", "BRIGHTNESS",   "Screen brightness 1\u201310",
     None, None, None),
    # MAP INSET row carries TWO segmented controls: enable + orientation.
    # Custom drawing/hit-test below handles the second pair.  Listed here
    # as a single key so it occupies one row in the standard loop.
    ("map_enabled", "MAP INSET",    "Lower-left 2D moving map \u00b7 orient",
     [False, True],      ["OFF", "ON"],       80),
    ("map_zoom_nm", "MAP RANGE",    "Default radius (nm)",
     [1, 2, 5, 10, 20, 40], ["1","2","5","10","20","40"], 50),
    ("sun_realtime","SUN POSITION", "Real-time from UTC + GPS",
     [False, True],      ["FIXED", "REAL"],   80),
]
# Trailing controls for the MAP INSET row (orientation), drawn to the
# left of the standard segmented control by hand.
_DSP_MAP_ORIENT_OPTS  = ["trk", "nrth"]
_DSP_MAP_ORIENT_LBLS  = ["TRK\u2191", "N\u2191"]
_DSP_MAP_ORIENT_BW    = 80
_DSP_MAP_ORIENT_GAP   = 24    # gap between orient pair and on/off pair

# Multi-toggle MAP LAYERS row \u2014 packed five toggles (terrain / water /
# airports / runways / obstacles) into one row.  Drawn separately from
# _DSP_ROWS because the standard row schema is one control per row.
_DSP_MAP_LAYERS = [
    ("map_show_terrain",   "TER"),
    ("map_show_water",     "WTR"),
    ("map_show_airports",  "APT"),
    ("map_show_runways",   "RWY"),
    ("map_show_obstacles", "OBS"),
]
_DSP_LAYERS_ROW_INDEX = len(_DSP_ROWS)
_DSP_LAYERS_BTN_W     = 70
_DSP_LAYERS_BTN_G     = 6


def _dsp_rx(row, bx, bw):
    """Left x of control group (right-aligned, 14 px margin)."""
    *_, opts_v, opts_l, bw_each = row
    if opts_v is None:
        total = _DSP_SW + _DSP_BTN_G + _DSP_VW + _DSP_BTN_G + _DSP_SW
    else:
        total = len(opts_v)*bw_each + (len(opts_v)-1)*_DSP_BTN_G
    return bx + bw - total - 14


def _dsp_layers_geom(bx, bw):
    """Right-aligned geometry for the MAP LAYERS multi-toggle row."""
    n = len(_DSP_MAP_LAYERS)
    total = n * _DSP_LAYERS_BTN_W + (n - 1) * _DSP_LAYERS_BTN_G
    return bx + bw - total - 14


def draw_display_setup(surf, ds):
    _screen_header(surf, "DISPLAY")
    for ri, row in enumerate(_DSP_ROWS):
        key, label, sub, opts_v, opts_l, bw_each = row
        bx, by, bw, bh = _setting_row(surf, ri, label, sub)
        ry = by + (bh - _DSP_BTN_H) // 2
        rx = _dsp_rx(row, bx, bw)
        if opts_v is None:                              # brightness stepper
            val = ds.get("brightness", 8)
            _step_btn(surf, rx, ry, _DSP_SW, _DSP_BTN_H, "\u2212")
            vx = rx + _DSP_SW + _DSP_BTN_G
            pygame.draw.rect(surf, (0,18,38), (vx, ry, _DSP_VW, _DSP_BTN_H), border_radius=4)
            pygame.draw.rect(surf, (60,80,110), (vx, ry, _DSP_VW, _DSP_BTN_H), width=1, border_radius=4)
            _text(surf, str(val), 18, WHITE, bold=True, cx=vx+_DSP_VW//2, cy=ry+_DSP_BTN_H//2)
            _step_btn(surf, vx+_DSP_VW+_DSP_BTN_G, ry, _DSP_SW, _DSP_BTN_H, "+")
        else:                                           # segmented control
            cur = ds.get(key, opts_v[0])
            for i, (v, lbl) in enumerate(zip(opts_v, opts_l)):
                _seg_btn(surf, rx+i*(bw_each+_DSP_BTN_G), ry, bw_each, _DSP_BTN_H, lbl, v==cur)
            # MAP INSET row also carries the orient pair to its left
            if key == "map_enabled":
                cur_or = ds.get("map_orient", "trk")
                ox = rx - (_DSP_MAP_ORIENT_GAP
                           + 2 * _DSP_MAP_ORIENT_BW + _DSP_BTN_G)
                for i, (v, lbl) in enumerate(zip(_DSP_MAP_ORIENT_OPTS,
                                                 _DSP_MAP_ORIENT_LBLS)):
                    _seg_btn(surf,
                             ox + i * (_DSP_MAP_ORIENT_BW + _DSP_BTN_G),
                             ry, _DSP_MAP_ORIENT_BW, _DSP_BTN_H,
                             lbl, v == cur_or)

    # MAP LAYERS — five-toggle packed row.  Drawn after the homogeneous
    # rows because the standard schema is one control per row.
    bx, by, bw, bh = _setting_row(surf, _DSP_LAYERS_ROW_INDEX,
                                  "MAP LAYERS",
                                  "Per-layer visibility on the map inset")
    ry = by + (bh - _DSP_BTN_H) // 2
    rx = _dsp_layers_geom(bx, bw)
    for i, (key, lbl) in enumerate(_DSP_MAP_LAYERS):
        active = bool(ds.get(key, True))
        _seg_btn(surf,
                 rx + i * (_DSP_LAYERS_BTN_W + _DSP_LAYERS_BTN_G),
                 ry, _DSP_LAYERS_BTN_W, _DSP_BTN_H, lbl, active)


def display_setup_hit(x, y, ds):
    """Return action string or None."""
    if _back_hit(x, y):
        return "back"
    for ri, row in enumerate(_DSP_ROWS):
        key, *_, opts_v, opts_l, bw_each = row
        by = _ss_row_y(ri)
        if not (by <= y <= by+_SS_RH):
            continue
        bx = _SS_MX; bw = DISPLAY_W - 2*_SS_MX
        ry = by + (_SS_RH - _DSP_BTN_H) // 2
        rx = _dsp_rx(row, bx, bw)
        if not (ry <= y <= ry+_DSP_BTN_H):
            continue
        if opts_v is None:
            if rx <= x <= rx+_DSP_SW:
                return "inc:brightness:-1"
            plus_x = rx + _DSP_SW + _DSP_BTN_G + _DSP_VW + _DSP_BTN_G
            if plus_x <= x <= plus_x+_DSP_SW:
                return "inc:brightness:1"
        else:
            for i, v in enumerate(opts_v):
                bx_b = rx + i*(bw_each+_DSP_BTN_G)
                if bx_b <= x <= bx_b+bw_each:
                    return f"set:{key}:{v}"
            # MAP INSET row carries the orient pair to its left
            if key == "map_enabled":
                ox = rx - (_DSP_MAP_ORIENT_GAP
                           + 2 * _DSP_MAP_ORIENT_BW + _DSP_BTN_G)
                for i, v in enumerate(_DSP_MAP_ORIENT_OPTS):
                    bx_b = ox + i * (_DSP_MAP_ORIENT_BW + _DSP_BTN_G)
                    if bx_b <= x <= bx_b + _DSP_MAP_ORIENT_BW:
                        return f"set:map_orient:{v}"

    # MAP LAYERS multi-toggle row
    by = _ss_row_y(_DSP_LAYERS_ROW_INDEX)
    if by <= y <= by + _SS_RH:
        bx = _SS_MX; bw = DISPLAY_W - 2*_SS_MX
        ry = by + (_SS_RH - _DSP_BTN_H) // 2
        if ry <= y <= ry + _DSP_BTN_H:
            rx = _dsp_layers_geom(bx, bw)
            for i, (key, _lbl) in enumerate(_DSP_MAP_LAYERS):
                bx_b = rx + i * (_DSP_LAYERS_BTN_W + _DSP_LAYERS_BTN_G)
                if bx_b <= x <= bx_b + _DSP_LAYERS_BTN_W:
                    new_val = not bool(ds.get(key, True))
                    return f"set:{key}:{new_val}"
    return None


# ── AHRS / Sensors screen ─────────────────────────────────────────────────────

_SS_TRIM_SW = 40   # stepper button width
_SS_TRIM_VW = 90   # trim value box width
_SS_TRIM_H  = 40   # stepper/control height
_SS_TRIM_G  = 6    # gap

_SS_MAG_LABELS = {
    "idle":    ("IDLE",       (100,110,130)),
    "running": ("RUNNING\u2026", YELLOW),
    "done":    ("DONE  \u2713",  (50,200,80)),
    "error":   ("ERROR",      RED),
}


def _trim_stepper(surf, bx, by, bw, bh, val, key):
    """Draw [-][val°][+] stepper right-aligned in a settings row."""
    total = _SS_TRIM_SW + _SS_TRIM_G + _SS_TRIM_VW + _SS_TRIM_G + _SS_TRIM_SW
    rx = bx + bw - total - 14
    ry = by + (bh - _SS_TRIM_H) // 2
    _step_btn(surf, rx, ry, _SS_TRIM_SW, _SS_TRIM_H, "\u2212")
    vx = rx + _SS_TRIM_SW + _SS_TRIM_G
    pygame.draw.rect(surf, (0,18,38), (vx, ry, _SS_TRIM_VW, _SS_TRIM_H), border_radius=4)
    pygame.draw.rect(surf, (60,80,110), (vx, ry, _SS_TRIM_VW, _SS_TRIM_H), width=1, border_radius=4)
    _text(surf, f"{val:+.1f}\u00b0", 16, WHITE, bold=True,
          cx=vx+_SS_TRIM_VW//2, cy=ry+_SS_TRIM_H//2)
    _step_btn(surf, vx+_SS_TRIM_VW+_SS_TRIM_G, ry, _SS_TRIM_SW, _SS_TRIM_H, "+")
    return rx   # leftmost x of stepper (for hit detection)


# ── Approach selection screen (HITS / synthetic approaches) ──────────────────
#
# Pilot taps APPR on the CDI strip → opens this screen.  Lists runway
# ends for the airport currently held in disp["nav"]["ident"]; a tap on
# any end activates HITS to that threshold and retargets the direct-to.

_APR_HEADER_Y = 64       # below the title bar
_APR_GRID_TOP = 110
_APR_TILE_W   = 280
_APR_TILE_H   = 84
_APR_TILE_GX  = 18
_APR_TILE_GY  = 12
_APR_COLS     = 2
_APR_CANCEL_H = 44


def _apr_runway_ends(ident: str) -> list:
    """Return [(rwy_id, lat, lon, elev_ft, hdg_deg, length_ft), ...] for the
    airport ``ident``, one entry per runway end (so a single physical
    runway yields two entries, e.g. RWY 03 and RWY 21)."""
    if _runways is None or not ident:
        return []
    if hasattr(_runways, "dtype"):
        mask = _runways["airport"] == ident
        rows = _runways[mask]
        ends = []
        for r in rows:
            ends.append((str(r["le_ident"]), float(r["le_lat"]),
                         float(r["le_lon"]), float(r["le_elev_ft"]),
                         float(r["le_hdg"]), float(r["length_ft"])))
            ends.append((str(r["he_ident"]), float(r["he_lat"]),
                         float(r["he_lon"]), float(r["he_elev_ft"]),
                         float(r["he_hdg"]), float(r["length_ft"])))
        return ends
    # Pure-Python fallback (legacy path).
    return [
        (e[0], e[1], e[2], e[3], e[4], r.length_ft)
        for r in _runways if r.airport == ident
        for e in (
            (r.le_ident, r.le_lat, r.le_lon, r.le_elev_ft, r.le_hdg),
            (r.he_ident, r.he_lat, r.he_lon, r.he_elev_ft, r.he_hdg),
        )
    ]


def _apr_grid_origin():
    """Top-left of the runway tile grid (centred horizontally)."""
    grid_w = _APR_COLS * _APR_TILE_W + (_APR_COLS - 1) * _APR_TILE_GX
    return (DISPLAY_W - grid_w) // 2, _APR_GRID_TOP


def _apr_tile_rect(i: int) -> pygame.Rect:
    """Rect for the i-th runway tile (row-major, _APR_COLS columns)."""
    gx, gy = _apr_grid_origin()
    col = i % _APR_COLS
    row = i // _APR_COLS
    x = gx + col * (_APR_TILE_W + _APR_TILE_GX)
    y = gy + row * (_APR_TILE_H + _APR_TILE_GY)
    return pygame.Rect(x, y, _APR_TILE_W, _APR_TILE_H)


def draw_approach_select(surf):
    """Approach-runway picker screen.  Draws runway tiles for the parent
    airport (read from disp["nav"]["ident"]), plus a CANCEL APPROACH
    button when an approach is currently active."""
    _screen_header(surf, "APPROACH")

    nv  = disp.get("nav") or {}
    ap  = disp.get("approach") or {}
    ident = nv.get("ident", "") or ap.get("airport", "")

    # Header line — the airport this picker is for.
    if ident:
        _text(surf, ident, 22, WHITE, bold=True,
              cx=DISPLAY_W // 2, cy=_APR_HEADER_Y)
    else:
        _text(surf, "Set a Direct-To airport first",
              16, (200, 180, 80),
              cx=DISPLAY_W // 2, cy=_APR_HEADER_Y)
        return

    ends = _apr_runway_ends(ident)
    if not ends:
        _text(surf, f"No runway data loaded for {ident}",
              16, (200, 180, 80),
              cx=DISPLAY_W // 2, cy=_APR_HEADER_Y + 36)
        return

    cur_runway = ap.get("runway", "") if ap.get("active") else ""
    for i, (rid, _la, _lo, elev, hdg, length) in enumerate(ends):
        rect = _apr_tile_rect(i)
        if rect.bottom > DISPLAY_H - _APR_CANCEL_H - 10:
            break    # ran out of room; pagination would go here
        active = (rid == cur_runway)
        bg = (0, 55, 65) if active else (0, 18, 38)
        oc = CYAN      if active else (60, 80, 110)
        pygame.draw.rect(surf, bg, rect, border_radius=8)
        pygame.draw.rect(surf, oc, rect, width=2, border_radius=8)
        _text(surf, f"RWY {rid}", 22, WHITE, bold=True,
              cx=rect.centerx, cy=rect.y + 24)
        _text(surf, f"{int(round(length)):,} ft   {int(round(hdg))}°",
              14, (180, 195, 220),
              cx=rect.centerx, cy=rect.y + 56)

    # CANCEL APPROACH button — only when an approach is active.
    if ap.get("active"):
        bx = DISPLAY_W // 2 - 130
        by = DISPLAY_H - _APR_CANCEL_H - 12
        _action_btn(surf, bx, by, 260, _APR_CANCEL_H,
                    "CANCEL APPROACH", style="warn")


def approach_select_hit(x, y):
    """Return action string for a tap on the approach-select screen, or
    None if nothing was hit."""
    if _back_hit(x, y):
        return "back"

    nv  = disp.get("nav") or {}
    ap  = disp.get("approach") or {}
    ident = nv.get("ident", "") or ap.get("airport", "")
    if not ident:
        return None

    ends = _apr_runway_ends(ident)
    for i in range(len(ends)):
        rect = _apr_tile_rect(i)
        if rect.bottom > DISPLAY_H - _APR_CANCEL_H - 10:
            break
        if rect.collidepoint(x, y):
            return f"select:{i}"

    if ap.get("active"):
        bx = DISPLAY_W // 2 - 130
        by = DISPLAY_H - _APR_CANCEL_H - 12
        if bx <= x <= bx + 260 and by <= y <= by + _APR_CANCEL_H:
            return "cancel"
    return None


def _approach_activate(rwy_end):
    """Activate HITS to the given runway end and retarget the
    direct-to so the CDI / ETE / inset all line up with the threshold
    instead of the airport centroid."""
    rid, la, lo, elev, hdg, _length = rwy_end
    ident = disp.get("nav", {}).get("ident", "") or \
            disp.get("approach", {}).get("airport", "")
    disp["approach"] = {
        "active":          True,
        "airport":         ident,
        "runway":          rid,
        "thresh_lat":      float(la),
        "thresh_lon":      float(lo),
        "thresh_elev_ft":  float(elev),
        "course_deg":      float(hdg),
    }
    # Retarget D2 to the threshold so CDI deviation, ETE and the map
    # inset's magenta line all land on the runway end the pilot is
    # flying to.  Capture the current aircraft position as the
    # activation point so the CDI's course reference is sensible.
    # Keep nv["ident"] as the plain airport ident (no slash-form) so
    # downstream airport-database lookups still resolve; draw_cdi
    # appends the runway suffix to its readout when an approach is
    # active.
    disp["nav"] = {
        "ident":   ident,
        "lat":     float(la),
        "lon":     float(lo),
        "elev_ft": float(elev),
        "act_lat": float(disp.get("lat", la)),
        "act_lon": float(disp.get("lon", lo)),
    }


def _approach_cancel():
    """Clear the active approach.  Leaves the direct-to pointing at
    the threshold (pilot can re-issue a D2 to the airport centroid
    if they want)."""
    disp["approach"]["active"] = False


def draw_ahrs_setup(surf, ss):
    _screen_header(surf, "AHRS / SENSORS")

    # Row 0: Pitch trim
    bx, by, bw, bh = _setting_row(surf, 0, "PITCH TRIM", "Horizon offset correction")
    _trim_stepper(surf, bx, by, bw, bh, ss.get("pitch_trim", 0.0), "pitch_trim")

    # Row 1: Roll trim
    bx, by, bw, bh = _setting_row(surf, 1, "ROLL TRIM", "Wing-level correction")
    _trim_stepper(surf, bx, by, bw, bh, ss.get("roll_trim", 0.0), "roll_trim")

    # Row 2: Magnetometer calibration — cardinal walk-through wizard
    bx, by, bw, bh = _setting_row(surf, 2, "MAGNETOMETER", "Compass calibration")
    cal = ss.get("mag_cal", "idle")
    state_lbl, state_col = _SS_MAG_LABELS.get(cal, ("?", WHITE))
    _text(surf, state_lbl, 13, state_col, bold=True, x=bx+220, y=by+(bh-30)//2)
    cur_deltas = ss.get("mag_cal_deltas") or []
    if cur_deltas and any(abs(d) > 0.05 for d in cur_deltas):
        peak = max(abs(d) for d in cur_deltas)
        _text(surf, f"max |Δ| {peak:.1f}°", 11, (140, 160, 190),
              x=bx+220, y=by+(bh-30)//2 + 18)
    elif abs(ss.get("mag_cal_offset", 0.0)) > 0.05:
        # Legacy single-offset still on disk (pre-piecewise)
        _text(surf, f"offset {ss['mag_cal_offset']:+.1f}°", 11,
              (140, 160, 190), x=bx+220, y=by+(bh-30)//2 + 18)
    cbx = bx+bw-138-14; cby = by+(bh-_DSP_BTN_H)//2
    _action_btn(surf, cbx, cby, 138, _DSP_BTN_H, "CALIBRATE", "ok")

    # Row 3: AHRS orientation (which side of the board the connector
    # points toward, viewed from the pilot's seat).  Pitch / roll / heading
    # get remapped at render time so the AHRS reads correctly regardless
    # of how it's bolted in.
    bx, by, bw, bh = _setting_row(surf, 3, "ORIENTATION",
                                   "Direction the connector faces")
    cur_ori = ss.get("orientation", "right")
    opts_ori = [("forward", "FWD"), ("left", "LEFT"),
                ("right",   "RIGHT"), ("aft", "AFT")]
    seg_w = 88
    total_ori = 4 * seg_w + 3 * _DSP_BTN_G
    rx = bx + bw - total_ori - 14
    ry = by + (bh - _DSP_BTN_H) // 2
    for i, (v, lbl) in enumerate(opts_ori):
        _seg_btn(surf, rx + i * (seg_w + _DSP_BTN_G), ry, seg_w, _DSP_BTN_H,
                 lbl, v == cur_ori)

    # Row 4: Mounting (right-side-up vs upside-down).  Independent of
    # orientation — combines with it at render time.
    bx, by, bw, bh = _setting_row(surf, 4, "MOUNTING",
                                   "Right-side-up or inverted")
    cur = ss.get("mounting", "normal")
    opts = [("normal","NORMAL"),("inverted","INVERTED")]
    total = 2*120 + _DSP_BTN_G
    rx = bx + bw - total - 14
    ry = by + (bh - _DSP_BTN_H) // 2
    for i, (v, lbl) in enumerate(opts):
        _seg_btn(surf, rx+i*(120+_DSP_BTN_G), ry, 120, _DSP_BTN_H, lbl, v==cur)

    # Row 5: Heading source (MAG / TRK / AUTO) — matches the iPhone display.
    # Sub-line documents what each option does so a pilot can pick without
    # reaching for the manual.
    bx, by, bw, bh = _setting_row(
        surf, 5, "HEADING SOURCE",
        "MAG=compass  TRK=GPS track  AUTO=TRK when moving, MAG otherwise")
    cur_src = ss.get("hdg_src", "auto")
    opts_src = [("mag", "MAG"), ("trk", "TRK"), ("auto", "AUTO")]
    seg_w = 96
    total_src = 3 * seg_w + 2 * _DSP_BTN_G
    rx = bx + bw - total_src - 14
    ry = by + (bh - _DSP_BTN_H) // 2
    for i, (v, lbl) in enumerate(opts_src):
        _seg_btn(surf, rx + i * (seg_w + _DSP_BTN_G), ry, seg_w, _DSP_BTN_H,
                 lbl, v == cur_src)

    # Row 6: Airspeed source (GPS groundspeed vs dedicated IAS sensor)
    bx, by, bw, bh = _setting_row(surf, 6, "AIRSPEED SOURCE",
                                   "GPS groundspeed or IAS sensor")
    cur_as = ss.get("airspeed_src", "gps")
    opts_as = [("gps", "GPS GS"), ("ias", "IAS SENSOR")]
    total_as = 2*120 + _DSP_BTN_G
    rx = bx + bw - total_as - 14
    ry = by + (bh - _DSP_BTN_H) // 2
    for i, (v, lbl) in enumerate(opts_as):
        active = v == cur_as
        if v == "ias":
            # IAS sensor not yet wired — show as future/disabled
            pygame.draw.rect(surf, (18, 18, 20),
                             (rx+i*(120+_DSP_BTN_G), ry, 120, _DSP_BTN_H), border_radius=6)
            pygame.draw.rect(surf, (55, 55, 65),
                             (rx+i*(120+_DSP_BTN_G), ry, 120, _DSP_BTN_H), width=2, border_radius=6)
            _text(surf, lbl, 13, (75, 75, 88), bold=False,
                  cx=rx+i*(120+_DSP_BTN_G)+60, cy=ry+_DSP_BTN_H//2-7)
            _text(surf, "future", 9, (60, 60, 72),
                  cx=rx+i*(120+_DSP_BTN_G)+60, cy=ry+_DSP_BTN_H//2+8)
        else:
            _seg_btn(surf, rx+i*(120+_DSP_BTN_G), ry, 120, _DSP_BTN_H, lbl, active)


def ahrs_setup_hit(x, y, ss):
    if _back_hit(x, y):
        return "back"
    bw = DISPLAY_W - 2*_SS_MX
    total = _SS_TRIM_SW + _SS_TRIM_G + _SS_TRIM_VW + _SS_TRIM_G + _SS_TRIM_SW
    rx_trim = _SS_MX + bw - total - 14
    for ri in range(7):
        by = _ss_row_y(ri)
        if not (by <= y <= by+_SS_RH):
            continue
        bx = _SS_MX
        if ri in (0, 1):
            key = "pitch_trim" if ri == 0 else "roll_trim"
            ry = by + (_SS_RH - _SS_TRIM_H) // 2
            if not (ry <= y <= ry+_SS_TRIM_H):
                continue
            if rx_trim <= x <= rx_trim+_SS_TRIM_SW:
                return f"trim:{key}:-0.1"
            plus_x = rx_trim + _SS_TRIM_SW + _SS_TRIM_G + _SS_TRIM_VW + _SS_TRIM_G
            if plus_x <= x <= plus_x+_SS_TRIM_SW:
                return f"trim:{key}:+0.1"
        elif ri == 2:
            cbx = _SS_MX + bw - 138 - 14
            cby = by + (_SS_RH - _DSP_BTN_H) // 2
            if cbx <= x <= cbx + 138 and cby <= y <= cby + _DSP_BTN_H:
                return "mag_cal_open"
        elif ri == 3:
            seg_w = 88
            total_o = 4 * seg_w + 3 * _DSP_BTN_G
            rx = bx + bw - total_o - 14
            ry = by + (_SS_RH - _DSP_BTN_H) // 2
            for i, v in enumerate(("forward", "left", "right", "aft")):
                xi = rx + i * (seg_w + _DSP_BTN_G)
                if xi <= x <= xi + seg_w and ry <= y <= ry + _DSP_BTN_H:
                    return f"set:orientation:{v}"
        elif ri == 4:
            total_m = 2*120 + _DSP_BTN_G
            rx = bx + bw - total_m - 14
            ry = by + (_SS_RH - _DSP_BTN_H) // 2
            for i, v in enumerate(("normal","inverted")):
                if rx+i*(120+_DSP_BTN_G) <= x <= rx+i*(120+_DSP_BTN_G)+120:
                    if ry <= y <= ry+_DSP_BTN_H:
                        return f"set:mounting:{v}"
        elif ri == 5:
            seg_w = 96
            total_src = 3 * seg_w + 2 * _DSP_BTN_G
            rx = bx + bw - total_src - 14
            ry = by + (_SS_RH - _DSP_BTN_H) // 2
            for i, v in enumerate(("mag", "trk", "auto")):
                xi = rx + i * (seg_w + _DSP_BTN_G)
                if xi <= x <= xi + seg_w and ry <= y <= ry + _DSP_BTN_H:
                    return f"set:hdg_src:{v}"
        elif ri == 6:
            total_as = 2*120 + _DSP_BTN_G
            rx = bx + bw - total_as - 14
            ry = by + (_SS_RH - _DSP_BTN_H) // 2
            # Only GPS GS (index 0) is active; IAS SENSOR (index 1) is future/disabled
            if rx <= x <= rx+120:
                if ry <= y <= ry+_DSP_BTN_H:
                    return "set:airspeed_src:gps"
    return None


# ── WiFi network scan ─────────────────────────────────────────────────────────

def _scan_wifi():
    """Return [{ssid, signal, secured}] sorted by signal desc, deduped by SSID."""
    try:
        r = subprocess.run(
            ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY",
             "dev", "wifi", "list", "--rescan", "yes"],
            capture_output=True, text=True, timeout=20,
        )
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip()[:80] or "nmcli scan failed")
        seen = {}
        for line in r.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = re.split(r"(?<!\\):", line)
            ssid = parts[0].replace("\\:", ":").strip()
            if not ssid:
                continue
            try:
                signal = int(parts[1]) if len(parts) > 1 else 0
            except ValueError:
                continue
            secured = len(parts) > 2 and parts[2].strip() not in ("", "--")
            if ssid not in seen or signal > seen[ssid]["signal"]:
                seen[ssid] = {"ssid": ssid, "signal": signal, "secured": secured}
        return sorted(seen.values(), key=lambda n: n["signal"], reverse=True)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Scan timed out")
    except FileNotFoundError:
        raise RuntimeError("nmcli not found")


def _do_scan():
    disp["cs"]["scan_state"]  = "scanning"
    disp["cs"]["scan_nets"]   = []
    disp["cs"]["scan_scroll"] = 0
    disp["cs"]["scan_error"]  = ""
    disp["mode"] = "wifi_scan"
    def _worker():
        try:
            nets = _scan_wifi()
            disp["cs"]["scan_nets"]  = nets
            disp["cs"]["scan_state"] = "done"
        except RuntimeError as e:
            disp["cs"]["scan_error"] = str(e)
            disp["cs"]["scan_state"] = "error"
    threading.Thread(target=_worker, daemon=True).start()


_WS_ITEM_H = 56
_WS_LIST_Y = 82
_WS_BTN_H  = 46


def _signal_bars(signal):
    if signal >= 75: return 4
    if signal >= 50: return 3
    if signal >= 25: return 2
    return 1


def draw_wifi_scan(surf, cs):
    ws_btn_y = DISPLAY_H - _WS_BTN_H - 8
    surf.fill((0, 8, 22))
    _screen_header(surf, "WIFI NETWORKS")
    state = cs.get("scan_state", "")
    nets  = cs.get("scan_nets", [])

    if state == "scanning":
        _text(surf, "Scanning\u2026", 22, CYAN, bold=True,
              cx=DISPLAY_W//2, cy=DISPLAY_H//2 - 16)
        _text(surf, "This may take a few seconds", 13, (110, 130, 160),
              cx=DISPLAY_W//2, cy=DISPLAY_H//2 + 16)
    elif state == "error":
        _text(surf, cs.get("scan_error", "Scan failed"), 15, (220, 80, 80),
              bold=True, cx=DISPLAY_W//2, cy=DISPLAY_H//2 - 10)
    elif state == "done":
        if not nets:
            _text(surf, "No networks found", 18, (180, 180, 180),
                  bold=True, cx=DISPLAY_W//2, cy=DISPLAY_H//2 - 10)
        else:
            _text(surf, f"{len(nets)} network{'s' if len(nets) != 1 else ''} \u2014 tap to select",
                  11, (100, 130, 160), cx=DISPLAY_W//2, cy=62)
            scroll  = cs.get("scan_scroll", 0)
            list_h  = ws_btn_y - _WS_LIST_Y - 8
            visible = list_h // _WS_ITEM_H
            for i, net in enumerate(nets[scroll:scroll + visible]):
                iy      = _WS_LIST_Y + i * _WS_ITEM_H
                row_col = (0, 22, 48) if i % 2 == 0 else (0, 16, 36)
                pygame.draw.rect(surf, row_col,
                                 (_SS_MX, iy, DISPLAY_W - 2*_SS_MX, _WS_ITEM_H - 2))
                bars    = _signal_bars(net["signal"])
                bar_col = ((60, 220, 80) if bars >= 3 else
                           (220, 180, 60) if bars == 2 else (200, 80, 80))
                bx0 = _SS_MX + 12
                for b in range(4):
                    bh  = 8 + b * 7
                    bby = iy + _WS_ITEM_H//2 + 16 - bh
                    col = bar_col if b < bars else (35, 48, 62)
                    pygame.draw.rect(surf, col, (bx0 + b * 10, bby, 7, bh))
                ssid = net["ssid"]
                if len(ssid) > 30:
                    ssid = ssid[:29] + "\u2026"
                _text(surf, ssid, 16, WHITE, bold=True, x=bx0 + 52, y=iy + 8)
                _text(surf, f"{net['signal']}%", 11, (100, 130, 160), x=bx0 + 52, y=iy + 30)
                lock_lbl = "WPA" if net["secured"] else "OPEN"
                lock_col = (200, 160, 60) if net["secured"] else (60, 200, 100)
                _text(surf, lock_lbl, 11, lock_col, bold=True,
                      x=DISPLAY_W - _SS_MX - 52, y=iy + _WS_ITEM_H//2 - 8)
            if scroll > 0:
                _text(surf, "\u25b2", 16, (100, 140, 180),
                      cx=DISPLAY_W//2, cy=_WS_LIST_Y - 16)
            if scroll + visible < len(nets):
                _text(surf, "\u25bc", 16, (100, 140, 180),
                      cx=DISPLAY_W//2, cy=ws_btn_y - 16)

    bw = DISPLAY_W - 2*_SS_MX
    _action_btn(surf, _SS_MX, ws_btn_y, bw, _WS_BTN_H, "RESCAN", "normal")


def wifi_scan_hit(x, y, cs):
    ws_btn_y = DISPLAY_H - _WS_BTN_H - 8
    if _back_hit(x, y):
        return "back"
    bw = DISPLAY_W - 2*_SS_MX
    if ws_btn_y <= y <= ws_btn_y + _WS_BTN_H and _SS_MX <= x <= _SS_MX + bw:
        return "rescan"
    if cs.get("scan_state") == "done":
        nets    = cs.get("scan_nets", [])
        scroll  = cs.get("scan_scroll", 0)
        list_h  = ws_btn_y - _WS_LIST_Y - 8
        visible = list_h // _WS_ITEM_H
        if _WS_LIST_Y - 20 <= y < _WS_LIST_Y and scroll > 0:
            return "scroll_up"
        if ws_btn_y - 20 <= y < ws_btn_y and scroll + visible < len(nets):
            return "scroll_down"
        if _WS_LIST_Y <= y < _WS_LIST_Y + visible * _WS_ITEM_H:
            idx = (y - _WS_LIST_Y) // _WS_ITEM_H + scroll
            if 0 <= idx < len(nets):
                return f"select:{idx}"
    return None


# ── Connectivity screen ───────────────────────────────────────────────────────

_CS_FIELDS = [
    ("ahrs_url",  "AHRS URL",        "Pico W access-point address"),
    ("wifi_ssid", "WiFi SSID",       "Network name to join"),
    ("wifi_pass", "WiFi PASSWORD",   "WPA2 passphrase"),
]
_CS_BTN_Y  = _ss_row_y(len(_CS_FIELDS) + 2) + 4   # below fields + STATUS + AHRS LINK rows
_CS_BTN_H  = 50


def _cs_val_box(surf, bx, by, bw, bh, key, val):
    """Draw the right-side value box for a connectivity field."""
    masked = key == "wifi_pass" and val
    display = "\u25cf" * min(len(val), 16) if masked else val
    vbx = bx+210; vby = by+12; vbw = bx+bw-vbx-12; vbh = bh-24
    pygame.draw.rect(surf, (0,20,42), (vbx, vby, vbw, vbh), border_radius=4)
    pygame.draw.rect(surf, CYAN, (vbx, vby, vbw, vbh), width=1, border_radius=4)
    _text(surf, display or "\u2014", 12, CYAN, bold=bool(val),
          cx=vbx+vbw//2, cy=vby+vbh//2)
    _text(surf, "tap to edit", 9, (80,100,125), x=vbx+6, y=vby+vbh-13)


def draw_connectivity_setup(surf, cs):
    _screen_header(surf, "CONNECTIVITY")
    bx = _SS_MX; bw = DISPLAY_W - 2*_SS_MX

    # Rows 0-2: editable fields (URL / SSID / password)
    for ri, (key, label, sub) in enumerate(_CS_FIELDS):
        bx2, by, _, bh = _setting_row(surf, ri, label, sub)
        _cs_val_box(surf, bx2, by, bw, bh, key, cs.get(key, ""))

    # Row 3: live status
    stat_ri = len(_CS_FIELDS)
    bx2, by, _, bh = _setting_row(surf, stat_ri, "STATUS", "Live connection state")
    for i, (ok_key, ok_y_lbl, ok_n_lbl) in enumerate([
            ("ahrs_ok", "AHRS  CONNECTED", "AHRS  NO LINK"),
            ("wifi_ok", "WiFi  CONNECTED", "WiFi  NO LINK")]):
        ok  = cs.get(ok_key, False)
        col = (60,220,80) if ok else (200,50,50)
        lbl = ok_y_lbl if ok else ok_n_lbl
        # When WiFi is up, show the actually-connected SSID (truncated)
        # instead of the generic "CONNECTED" text so the user can see
        # which network they're on.
        if ok and ok_key == "wifi_ok":
            actual = cs.get("wifi_actual", "")
            if actual:
                if len(actual) > 18:
                    actual = actual[:17] + "\u2026"
                lbl = f"WiFi: {actual}"
        cy  = by + bh//4 + i*bh//2
        pygame.draw.circle(surf, col, (bx2+238, cy), 6)
        _text(surf, lbl, 13, col, bold=True, x=bx2+252, y=cy-9)

    # AHRS transport diagnostics — shown on a separate row under STATUS.
    # Visible even when ahrs_ok=False so the user can tell WHY the link
    # isn't working (port open? lines parsing? specific error?).
    diag_ri = stat_ri + 1
    bx2, by, bw2, bh = _setting_row(
        surf, diag_ri, "AHRS LINK",
        f"{cs.get('ahrs_transport','?').upper()}  {cs.get('ahrs_port','')}")
    rx  = cs.get("ahrs_rx",  0)
    err = cs.get("ahrs_err", 0)
    lerr = cs.get("ahrs_last_err", "")
    _text(surf, f"RX:{rx}  ERR:{err}",
          12, (180,200,220), bold=True, x=bx2+14, y=by+bh-22)
    if lerr:
        shown = lerr if len(lerr) < 44 else lerr[:43] + "\u2026"
        _text(surf, shown, 10, (230,150,80), x=bx2+132, y=by+bh-20)

    # Live AHRS values on the right side of the row — lets the user
    # confirm sensor output is sane (e.g. RX growing but all zeros =
    # firmware alive but WT901 silent).
    live = (f"R {disp.get('roll',0):+5.1f}\u00b0  "
            f"P {disp.get('pitch',0):+5.1f}\u00b0  "
            f"Y {disp.get('yaw',0):5.1f}\u00b0  "
            f"ALT {int(disp.get('alt',0))}'")
    _text(surf, live, 11, (130,200,230), bold=True,
          x=bx2+bw2-14-_get_font(11, bold=True).size(live)[0],
          y=by+bh-20)

    # Status messages from last apply / test
    for msg, col, y_off in [
            (cs.get("apply_msg",""), (100,180,80), _CS_BTN_Y - 20),
            (cs.get("test_msg",""),  (100,160,220), _CS_BTN_Y - 8)]:
        if msg:
            _text(surf, msg, 10, col, cx=DISPLAY_W//2, y=y_off)

    # Action buttons (SCAN / APPLY / TEST)
    third = (bw - 20) // 3
    _action_btn(surf, bx,                _CS_BTN_Y, third, _CS_BTN_H, "SCAN WIFI",  "normal")
    _action_btn(surf, bx+third+10,       _CS_BTN_Y, third, _CS_BTN_H, "APPLY WIFI", "warn")
    _action_btn(surf, bx+2*(third+10),   _CS_BTN_Y, third, _CS_BTN_H, "TEST AHRS",  "ok")


def connectivity_setup_hit(x, y, cs):
    if _back_hit(x, y):
        return "back"
    bx = _SS_MX; bw = DISPLAY_W - 2*_SS_MX
    # Editable field rows
    for ri, (key, _, __) in enumerate(_CS_FIELDS):
        by = _ss_row_y(ri)
        if by <= y <= by+_SS_RH:
            vbx = bx+210
            if vbx <= x <= bx+bw-12:
                return f"edit:{key}"
    # Action buttons
    third = (bw - 20) // 3
    if _CS_BTN_Y <= y <= _CS_BTN_Y+_CS_BTN_H:
        if bx <= x <= bx+third:
            return "scan_wifi"
        if bx+third+10 <= x <= bx+2*third+10:
            return "apply_wifi"
        if bx+2*(third+10) <= x <= bx+3*third+20:
            return "test_ahrs"
    return None


# ── System screen ─────────────────────────────────────────────────────────────

_SYS_VERSION = "0.1.0"
_SYS_BUILD   = "2026-04-10"
_SYS_INFO_Y  = 56
_SYS_INFO_LH = 28


_SYS_N_LINES = 6
_SYS_IH      = _SYS_N_LINES * _SYS_INFO_LH + 16
_SYS_MODE_Y    = _SYS_INFO_Y + _SYS_IH + 8        # DISPLAY MODE row top
_SYS_TERRAIN_Y = _SYS_MODE_Y + _SS_RH + 8         # TERRAIN DATA row top
_SYS_BTN_Y     = _SYS_TERRAIN_Y + _SS_RH + 8      # action buttons top
_SYS_BTN_H     = 54


def _sys_data_tile(surf, bx, by, bw, bh, label, sub, active=True):
    """Half-width tappable tile for data download rows (terrain / obstacle)."""
    # Background gradient
    for i in range(bh):
        t = 1.0 - i / bh
        if active:
            c = (int(t*8), int(12+t*18), int(28+t*35))
        else:
            c = (int(t*5), int(t*7), int(t*12))
        pygame.draw.line(surf, c, (bx, by+i), (bx+bw, by+i))
    bc = (55,75,105) if active else (28,35,48)
    pygame.draw.rect(surf, bc, (bx, by, bw, bh), width=1, border_radius=4)
    lc = WHITE if active else (55,62,72)
    sc = (100,120,145) if active else (42,48,58)
    _text(surf, label, 13, lc, bold=True, x=bx+12, y=by+10)
    _text(surf, sub,   11, sc,             x=bx+12, y=by+28)
    if active:
        _text(surf, "\u25b6", 16, (60,80,110), x=bx+bw-22, y=by+(bh-18)//2)
    else:
        _text(surf, "future", 10, (48,55,65), x=bx+bw-50, y=by+bh-18)


def draw_system_setup(surf):
    _screen_header(surf, "SYSTEM")
    bx = _SS_MX; bw = DISPLAY_W - 2*_SS_MX
    _gps_ok   = disp.get("gps_ok", False)
    _gps_sats = int(disp.get("sats", 0) or 0)
    if _gps_ok:
        _gps_status = f"fix \u00b7 {_gps_sats} sat{'s' if _gps_sats != 1 else ''}"
    elif _gps_sats > 0:
        _gps_status = f"acquiring \u00b7 {_gps_sats} sat{'s' if _gps_sats != 1 else ''}"
    else:
        _gps_status = "no signal"
    lines = [
        ("Firmware version",  _SYS_VERSION),
        ("Build date",        _SYS_BUILD),
        ("Display",           f"{DISPLAY_W}\u00d7{DISPLAY_H}  HDMI"),
        ("Hardware",          "Pi 4 + Pico W  (OpenGL SVT)"),
        ("GPS",               _gps_status),
        ("SRTM terrain data", "loaded" if os.path.isdir(SRTM_DIR) else "not found"),
    ]
    pygame.draw.rect(surf, (0,12,32), (bx, _SYS_INFO_Y, bw, _SYS_IH), border_radius=6)
    pygame.draw.rect(surf, (55,75,105), (bx, _SYS_INFO_Y, bw, _SYS_IH), width=1, border_radius=6)
    for i, (k, v) in enumerate(lines):
        ty = _SYS_INFO_Y + 10 + i*_SYS_INFO_LH
        _text(surf, k, 12, (120,140,165), x=bx+14, y=ty)
        _text(surf, v, 13, WHITE, bold=True, x=bx+310, y=ty)

    # DISPLAY MODE row
    _setting_row(surf, 0, "DISPLAY MODE", "Primary Flight Display or Multi-Function Display",
                 _y_override=_SYS_MODE_Y)
    cur = disp.get("display_mode", "pfd")
    btn_h_m = _DSP_BTN_H; btn_w_m = 110; gap_m = _DSP_BTN_G
    rx = bx + bw - 2*(btn_w_m+gap_m) + gap_m - 14
    ry = _SYS_MODE_Y + (_SS_RH - btn_h_m) // 2
    _seg_btn(surf, rx,              ry, btn_w_m, btn_h_m, "PFD", cur == "pfd")
    # MFD — disabled placeholder
    pygame.draw.rect(surf, (0,8,18), (rx+btn_w_m+gap_m, ry, btn_w_m, btn_h_m), border_radius=5)
    pygame.draw.rect(surf, (35,45,60), (rx+btn_w_m+gap_m, ry, btn_w_m, btn_h_m), width=2, border_radius=5)
    _text(surf, "MFD", 14, (50,60,75), bold=False, cx=rx+btn_w_m+gap_m+btn_w_m//2, cy=ry+btn_h_m//2-7)
    _text(surf, "coming soon", 9, (45,55,70), cx=rx+btn_w_m+gap_m+btn_w_m//2, cy=ry+btn_h_m//2+8)

    # Data download tiles: TERRAIN | OBSTACLE | AIRPORT (three columns)
    third = (bw - 16) // 3
    n_tiles, used_mb = _td_disk_stats()
    _sys_data_tile(surf, bx,              _SYS_TERRAIN_Y, third, _SS_RH,
                   "TERRAIN",
                   f"{n_tiles} tile{'s' if n_tiles != 1 else ''}  \u00b7  {used_mb:.1f} MB",
                   active=True)
    od_cnt     = disp["od"].get("records", 0)
    od_mb      = disp["od"].get("used_mb", 0.0)
    od_expired = disp["od"].get("expired", False)
    if od_cnt:
        if od_expired:
            od_sub = f"{od_cnt:,} obs  \u00b7  \u26a0 EXP"
        else:
            od_sub = f"{od_cnt:,} obs  \u00b7  {od_mb:.1f} MB"
    else:
        od_sub = "Tap to download"
    _sys_data_tile(surf, bx+third+8,      _SYS_TERRAIN_Y, third, _SS_RH,
                   "OBSTACLE", od_sub, active=True)
    ad_cnt     = disp["ad"].get("records", 0)
    ad_expired = disp["ad"].get("expired", False)
    if ad_cnt:
        if ad_expired:
            ad_sub = f"{ad_cnt:,} apts  \u00b7  \u26a0 EXP"
        else:
            ad_sub = f"{ad_cnt:,} airports"
    else:
        ad_sub = "Tap to download"
    _sys_data_tile(surf, bx+2*(third+8),  _SYS_TERRAIN_Y, third, _SS_RH,
                   "AIRPORTS", ad_sub, active=True)

    half_w = (bw - 10) // 2
    _action_btn(surf, bx,            _SYS_BTN_Y, half_w, _SYS_BTN_H, "SIMULATOR", "ok")
    _action_btn(surf, bx+half_w+10,  _SYS_BTN_Y, half_w, _SYS_BTN_H, "RESET DEFAULTS", "danger")

    # QUIT button at the very bottom
    quit_y = _SYS_BTN_Y + _SYS_BTN_H + 10
    _action_btn(surf, bx, quit_y, bw, _SYS_BTN_H, "QUIT PFD", "danger")


def system_setup_hit(x, y):
    if _back_hit(x, y):
        return "back"
    bx = _SS_MX; bw = DISPLAY_W - 2*_SS_MX
    if _SYS_TERRAIN_Y <= y <= _SYS_TERRAIN_Y+_SS_RH:
        third = (bw - 16) // 3
        if bx <= x <= bx+third:
            return "terrain_data"
        if bx+third+8 <= x <= bx+2*third+8:
            return "obstacle_data"
        if bx+2*(third+8) <= x <= bx+2*(third+8)+third:
            return "airport_data"
    half_w = (bw - 10) // 2
    if _SYS_BTN_Y <= y <= _SYS_BTN_Y+_SYS_BTN_H:
        if bx <= x <= bx+half_w:
            return "simulator"
        if bx+half_w+10 <= x <= bx+half_w+10+half_w:
            return "reset_defaults"
    quit_y = _SYS_BTN_Y + _SYS_BTN_H + 10
    if quit_y <= y <= quit_y+_SYS_BTN_H and bx <= x <= bx+bw:
        return "quit"
    return None


# ── Terrain data screen ──────────────────────────────────────────────────────

# Download source: Mapzen/Nextzen AWS public bucket — .hgt.gz, no auth required
_SRTM_BASE = "https://elevation-tiles-prod.s3.amazonaws.com/skadi"

# Preset download regions: (label, subtitle, lat_min, lat_max, lon_min, lon_max)
_TD_REGIONS = [
    ("US Southwest", "AZ \u00b7 NM \u00b7 NV \u00b7 UT \u00b7 CO",   31, 42, -115, -103),
    ("US Pacific",   "CA \u00b7 OR \u00b7 WA",                         32, 49, -125, -114),
    ("US Southeast", "FL \u00b7 GA \u00b7 AL \u00b7 NC \u00b7 SC",    24, 37,  -92,  -74),
    ("US Northeast", "NY \u00b7 PA \u00b7 NE states",                  37, 48,  -80,  -66),
    ("US Midwest",   "OH \u00b7 MI \u00b7 IL \u00b7 MN \u00b7 WI",    37, 49, -103,  -80),
    ("All CONUS",    "Lower 48 \u2014 ~2 GB",                          24, 49, -125,  -66),
    ("Alaska",       "Southern AK corridor",                            55, 64, -165, -131),
    ("Europe West",  "UK \u00b7 FR \u00b7 DE \u00b7 ES \u00b7 IT",    36, 58,   -9,   15),
    ("All Europe",   "UK to Turkey \u2014 ~3 GB",                      35, 60,  -12,   30),
]

_TD_COLS = 2
_TD_MX   = 12
_TD_MY   = 84      # top of region grid (below title bar + status strip)
_TD_GAP  = 8


def _td_tile_name(lat, lon):
    """Return the standard HGT filename for a 1°×1° tile by its SW corner."""
    ns = "N" if lat >= 0 else "S"
    ew = "W" if lon < 0 else "E"
    return f"{ns}{abs(lat):02d}{ew}{abs(lon):03d}.hgt"


def _td_tile_url(lat, lon):
    """Return (url, local_filename) for a tile."""
    ns = "N" if lat >= 0 else "S"
    fname_gz = _td_tile_name(lat, lon) + ".gz"
    folder   = f"{ns}{abs(lat):02d}"
    return f"{_SRTM_BASE}/{folder}/{fname_gz}"


def _td_tiles_for_region(lat_min, lat_max, lon_min, lon_max):
    """Enumerate all 1°×1° tile SW-corner coords for a lat/lon bounding box."""
    tiles = []
    for lat in range(lat_min, lat_max):
        for lon in range(lon_min, lon_max):
            tiles.append((lat, lon))
    return tiles


def _td_disk_stats():
    """Return (tile_count, total_mb) of HGT files in SRTM_DIR."""
    if not os.path.isdir(SRTM_DIR):
        return 0, 0.0
    total = 0
    for f in os.listdir(SRTM_DIR):
        if f.endswith(".hgt"):
            total += os.path.getsize(os.path.join(SRTM_DIR, f))
    count = sum(1 for f in os.listdir(SRTM_DIR) if f.endswith(".hgt"))
    return count, total / 1_048_576


def _td_region_tile_count(region):
    """Return number of tiles for a preset region."""
    _, _, lat_min, lat_max, lon_min, lon_max = region
    return (lat_max - lat_min) * (lon_max - lon_min)


def _td_download_thread(tiles, region_name):
    """Background download of a list of (lat, lon) tiles."""
    td = disp["td"]
    td["downloading"] = True
    td["dl_region"]   = region_name
    td["dl_total"]    = len(tiles)
    td["dl_current"]  = 0
    td["dl_cancel"]   = False
    os.makedirs(SRTM_DIR, exist_ok=True)
    ok = skip = err = 0
    for i, (lat, lon) in enumerate(tiles):
        if td["dl_cancel"]:
            td["dl_status"] = f"Cancelled  ({ok} new, {skip} skipped)"
            td["downloading"] = False
            return
        td["dl_current"] = i
        fname = _td_tile_name(lat, lon)
        dest  = os.path.join(SRTM_DIR, fname)
        if os.path.exists(dest):
            skip += 1
            td["dl_status"] = f"Skipping {fname}"
            continue
        url = _td_tile_url(lat, lon)
        td["dl_status"] = f"Downloading {fname}\u2026"
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                gz_data = resp.read()
            with gzip.open(io.BytesIO(gz_data)) as gz_f:
                hgt_data = gz_f.read()
            with open(dest, "wb") as f:
                f.write(hgt_data)
            ok += 1
        except Exception as exc:
            td["dl_status"] = f"Error {fname}: {exc}"
            err += 1
    td["dl_current"] = len(tiles)
    td["dl_status"]  = (f"Done \u2713  {ok} downloaded"
                        + (f", {skip} skipped" if skip else "")
                        + (f", {err} errors"   if err  else ""))
    td["downloading"] = False
    global _has_terrain
    _has_terrain = _check_terrain()


def _td_start_download(region):
    """Kick off a background download for a preset region."""
    label, sub, lat_min, lat_max, lon_min, lon_max = region
    tiles = _td_tiles_for_region(lat_min, lat_max, lon_min, lon_max)
    t = threading.Thread(target=_td_download_thread,
                         args=(tiles, label), daemon=True)
    t.start()


def _td_start_current_area():
    """Download tiles around the current GPS position (±2° box)."""
    lat = int(disp.get("lat", DEMO_LAT))
    lon = int(disp.get("lon", DEMO_LON))
    tiles = _td_tiles_for_region(lat-2, lat+3, lon-2, lon+3)
    t = threading.Thread(target=_td_download_thread,
                         args=(tiles, "Current Area"), daemon=True)
    t.start()


# ── Water-mask download ───────────────────────────────────────────────────────
# Companion to the SRTM download flow: pulls the Natural Earth 10m ocean +
# lakes shapefiles once (~12 MB combined), then for each existing .hgt tile
# rasterises a 1201×1201 binary water mask in-process using pyshp + pygame's
# C-based polygon fill.  Runs in a daemon thread so the UI stays responsive;
# status reported via disp["wd"].
#
# Speed vs the old gdal_rasterize subprocess flow:
#   - shapefiles parsed ONCE per process (cached at module level), not once
#     per tile per shapefile.
#   - pygame.draw.polygon is a single C scanline fill, no subprocess startup.
#   - bbox prefilter discards inland tiles in microseconds.
# Typical 25-tile "current area": old ~3 minutes, new ~5 seconds.

_WD_NE_FILES   = ("ne_10m_ocean", "ne_10m_lakes")
_WD_NE_PRIMARY = "https://naciscdn.org/naturalearth/10m/physical"
_WD_NE_MIRROR  = ("https://github.com/nvkelso/natural-earth-vector/"
                  "raw/master/zips/10m_physical")
_WD_TILE_RES   = 1201

# Per-shapefile cache of (bbox, [ring, ...]) tuples; built lazily on first use.
_wd_shapes_cache = {}


def _wd_shapes_dir():
    """Where Natural Earth .shp files live (alongside data/srtm/)."""
    return os.path.join(os.path.dirname(SRTM_DIR), "natural_earth")


def _wd_ensure_shapefiles(wd):
    """Download + unzip the Natural Earth shapefiles if not present.
    Returns the list of shapefile names found, or None on failure."""
    import zipfile as _zipfile
    sdir = _wd_shapes_dir()
    os.makedirs(sdir, exist_ok=True)
    found = []
    for name in _WD_NE_FILES:
        shp_path = os.path.join(sdir, name + ".shp")
        if os.path.exists(shp_path):
            found.append(name)
            continue
        zip_path = os.path.join(sdir, name + ".zip")
        wd["dl_status"] = f"Downloading {name}.zip…"
        urls = (f"{_WD_NE_PRIMARY}/{name}.zip",
                f"{_WD_NE_MIRROR}/{name}.zip")
        ok = False
        for url in urls:
            try:
                with urllib.request.urlopen(url, timeout=30) as resp:
                    blob = resp.read()
                with open(zip_path, "wb") as f:
                    f.write(blob)
                ok = True
                break
            except Exception as e:
                wd["dl_status"] = f"  retry: {e}"
        if not ok:
            wd["dl_status"] = f"Failed to download {name}.zip"
            return None
        try:
            with _zipfile.ZipFile(zip_path) as z:
                z.extractall(sdir)
            os.remove(zip_path)
        except Exception as e:
            wd["dl_status"] = f"Unzip failed: {e}"
            return None
        if not os.path.exists(shp_path):
            wd["dl_status"] = f"{name}.shp missing after unzip"
            return None
        found.append(name)
    return found


def _wd_load_shapes(name, wd):
    """Return cached parsed shapefile for `name`.  Cached as a dict with
    flat numpy arrays (much smaller + faster than nested Python lists),
    persisted to disk as .npz so subsequent runs skip the slow shapefile
    parse entirely:

        {'points':       (N, 2) float32,    flat list of all (lon, lat)
         'ring_starts':  (M+1,) int32,       indices into points[] per ring
         'ring_bboxes':  (M, 4) float32}     (lon_min, lat_min, lon_max, lat_max)

    Pure numpy means the worker thread never has to touch pygame/SDL
    (which has global locks that can deadlock the render thread when a
    huge polygon is being filled in C).
    """
    import numpy as _np
    if name in _wd_shapes_cache:
        return _wd_shapes_cache[name]

    sdir = _wd_shapes_dir()
    npz_path = os.path.join(sdir, name + ".npz")

    # Fast path: previously-parsed cache on disk.
    if os.path.exists(npz_path):
        wd["dl_status"] = f"Loading cached {name}…"
        try:
            with _np.load(npz_path, allow_pickle=False) as z:
                cache = {
                    "points":      z["points"],
                    "ring_starts": z["ring_starts"],
                    "ring_bboxes": z["ring_bboxes"],
                }
            _wd_shapes_cache[name] = cache
            return cache
        except Exception:
            pass   # fall through to re-parse

    # Slow path: parse the .shp.  Big shapefiles (ocean has 600 K points)
    # can take 30 s the first time; cache to .npz makes the second run
    # instant.
    import shapefile as _shapefile   # pyshp
    shp_path = os.path.join(sdir, name + ".shp")
    wd["dl_status"] = f"Parsing {name}.shp (one-time, ~30 s)…"

    all_pts = []
    ring_starts = [0]
    ring_bboxes = []
    cum = 0
    with _shapefile.Reader(shp_path) as sf:
        for shp in sf.iterShapes():
            parts = list(shp.parts) + [len(shp.points)]
            for i in range(len(parts) - 1):
                ring = shp.points[parts[i]:parts[i + 1]]
                if len(ring) < 3:
                    continue
                arr = _np.asarray(ring, dtype=_np.float32)
                all_pts.append(arr)
                cum += len(arr)
                ring_starts.append(cum)
                ring_bboxes.append((arr[:, 0].min(), arr[:, 1].min(),
                                    arr[:, 0].max(), arr[:, 1].max()))

    if not all_pts:
        cache = {
            "points":      _np.zeros((0, 2), dtype=_np.float32),
            "ring_starts": _np.zeros((1,), dtype=_np.int32),
            "ring_bboxes": _np.zeros((0, 4), dtype=_np.float32),
        }
    else:
        cache = {
            "points":      _np.concatenate(all_pts, axis=0).astype(_np.float32),
            "ring_starts": _np.asarray(ring_starts, dtype=_np.int32),
            "ring_bboxes": _np.asarray(ring_bboxes, dtype=_np.float32),
        }

    # Persist cache for next time.
    try:
        _np.savez(npz_path,
                  points=cache["points"],
                  ring_starts=cache["ring_starts"],
                  ring_bboxes=cache["ring_bboxes"])
    except OSError:
        pass

    _wd_shapes_cache[name] = cache
    return cache


def _wd_fill_ring(out, ring_xy_px):
    """Burn one polygon ring (N×2 float pixel coords) into out (H, W)
    uint8 array via numpy scanline fill.  Even–odd fill rule means
    polygon-with-holes (e.g. ocean cut by continents) renders correctly
    when outer + inner rings are passed in.
    """
    import numpy as _np
    n = len(ring_xy_px)
    if n < 3:
        return
    H, W = out.shape

    x1 = ring_xy_px[:, 0]
    y1 = ring_xy_px[:, 1]
    x2 = _np.roll(x1, -1)
    y2 = _np.roll(y1, -1)

    # Edge prefilter: drop edges entirely above or below the tile in Y.
    # Do NOT prefilter in X — closed polygons whose perimeter lies far
    # east/west of the tile (e.g. the Pacific Ocean ring's antimeridian
    # and Asian-coast edges for an offshore CONUS tile) still contribute
    # scanline-crossing parity.  Their xs values land outside [0, W) and
    # are clipped during fill, but keeping them is what makes the
    # even–odd count even on every scanline.
    keep = ~(((y1 < 0) & (y2 < 0)) | ((y1 >= H) & (y2 >= H)))
    if not keep.any():
        return
    x1 = x1[keep]; y1 = y1[keep]
    x2 = x2[keep]; y2 = y2[keep]

    y_min = max(0, int(_np.floor(min(y1.min(), y2.min()))))
    y_max = min(H - 1, int(_np.ceil(max(y1.max(), y2.max()))))
    if y_min > y_max:
        return

    for y in range(y_min, y_max + 1):
        # Edges crossing scanline y (top-inclusive, bottom-exclusive)
        crosses = ((y1 <= y) & (y < y2)) | ((y2 <= y) & (y < y1))
        if not crosses.any():
            continue
        with _np.errstate(divide="ignore", invalid="ignore"):
            xs = x1[crosses] + (y - y1[crosses]) * \
                 (x2[crosses] - x1[crosses]) / (y2[crosses] - y1[crosses])
        xs = _np.sort(xs)
        # Even–odd fill between successive intersection pairs
        for k in range(0, len(xs) - 1, 2):
            x_start = max(0, int(_np.ceil(xs[k])))
            x_end   = min(W, int(xs[k + 1]) + 1)
            if x_start < x_end:
                out[y, x_start:x_end] ^= 1   # XOR for even–odd


def _wd_rasterise_tile(lat_int, lon_int, shape_names, wd):
    """Build a (res×res) uint8 0/1 water mask for the 1°×1° tile at
    (lat_int, lon_int).  Pure numpy — no pygame/SDL on the worker
    thread, so the main render loop stays unblocked even on a huge
    polygon."""
    import numpy as _np
    res = _WD_TILE_RES
    out = _np.zeros((res, res), dtype=_np.uint8)

    tile_lon0 = float(lon_int)
    tile_lon1 = float(lon_int + 1)
    tile_lat0 = float(lat_int)
    tile_lat1 = float(lat_int + 1)
    scale = float(res - 1)

    for name in shape_names:
        cache = _wd_load_shapes(name, wd)
        ring_starts = cache["ring_starts"]
        bboxes      = cache["ring_bboxes"]
        points      = cache["points"]
        if len(bboxes) == 0:
            continue

        # Vectorised per-ring bbox prefilter
        keep_rings = ~((bboxes[:, 2] < tile_lon0) | (bboxes[:, 0] > tile_lon1) |
                       (bboxes[:, 3] < tile_lat0) | (bboxes[:, 1] > tile_lat1))
        ring_idx = _np.flatnonzero(keep_rings)
        if ring_idx.size == 0:
            continue

        for ri in ring_idx:
            s = ring_starts[ri]
            e = ring_starts[ri + 1]
            ring = points[s:e]
            # Convert lon/lat → pixel coords (col 0 = west, row 0 = north).
            xy = _np.empty_like(ring)
            xy[:, 0] = (ring[:, 0] - tile_lon0) * scale
            xy[:, 1] = (tile_lat1 - ring[:, 1]) * scale
            _wd_fill_ring(out, xy)

    # XOR fill produced 0/1 pixels but inner rings (continents inside
    # ocean) flip back to 0, which is what we want — water=1 only.
    return out


def _wd_existing_srtm_tiles():
    """Enumerate (lat_int, lon_int) for every .hgt file in SRTM_DIR."""
    tiles = []
    if not os.path.isdir(SRTM_DIR):
        return tiles
    for f in os.listdir(SRTM_DIR):
        if not f.endswith(".hgt") or len(f) < 11:
            continue
        try:
            ns = f[0]
            lat_int = int(f[1:3]) * (1 if ns == "N" else -1)
            ew = f[3]
            lon_int = int(f[4:7]) * (1 if ew == "E" else -1)
        except ValueError:
            continue
        tiles.append((lat_int, lon_int))
    return sorted(tiles)


def _wd_target_tiles(buffer_deg=1):
    """SRTM tiles plus a `buffer_deg` border in every direction.

    Adding buffer tiles means the rasteriser also produces water masks
    for offshore tiles where the SRTM coverage stops (e.g. just east of
    the Florida coast, or west of California).  Those tiles have no
    .hgt file but a valid .water mask — Natural Earth's ocean polygon
    fills them entirely with water — so the SVT outer mesh paints
    Pacific/Atlantic blue instead of defaulting to flat brown."""
    have = set(_wd_existing_srtm_tiles())
    extended = set(have)
    for lat_int, lon_int in have:
        for dlat in range(-buffer_deg, buffer_deg + 1):
            for dlon in range(-buffer_deg, buffer_deg + 1):
                la = lat_int + dlat
                lo = lon_int + dlon
                if -90 <= la <= 89 and -180 <= lo <= 179:
                    extended.add((la, lo))
    return sorted(extended)


def _wd_download_thread():
    """Background worker: download Natural Earth + rasterise per-tile masks."""
    from water import save_tile, _tile_key as _water_tile_key

    wd = disp["wd"]
    wd["downloading"] = True
    wd["dl_cancel"]   = False
    wd["dl_current"]  = 0
    wd["dl_total"]    = 0

    # Pure-python path: needs pyshp.  If missing, tell the user how to
    # install it without forcing them to install gdal-bin (heavy + slow).
    try:
        import shapefile  # noqa: F401
    except ImportError:
        wd["dl_status"] = ("Install pyshp: sudo pip3 install "
                           "--break-system-packages pyshp")
        wd["downloading"] = False
        return

    wd["dl_status"] = "Loading Natural Earth shapefiles…"
    found = _wd_ensure_shapefiles(wd)
    if found is None:
        wd["downloading"] = False
        return

    tiles = _wd_target_tiles(buffer_deg=1)
    if not tiles:
        wd["dl_status"] = "No SRTM tiles on disk — download terrain first"
        wd["downloading"] = False
        return

    os.makedirs(WATER_DIR, exist_ok=True)
    wd["dl_total"] = len(tiles)
    ok = skip = err = 0
    try:
        for i, (lat_int, lon_int) in enumerate(tiles):
            if wd["dl_cancel"]:
                wd["dl_status"] = (f"Cancelled  ({ok} new, "
                                   f"{skip} skipped, {err} errors)")
                return
            wd["dl_current"] = i
            key = _water_tile_key(lat_int, lon_int)
            out_path = os.path.join(WATER_DIR, key)
            if os.path.exists(out_path):
                skip += 1
                continue
            wd["dl_status"] = f"Rasterising {key}…"
            try:
                arr = _wd_rasterise_tile(lat_int, lon_int, found, wd)
                save_tile(out_path, arr)
                ok += 1
            except Exception as e:
                wd["dl_status"] = f"{key}: {e}"
                err += 1
        wd["dl_current"] = len(tiles)
        wd["dl_status"]  = (f"Done ✓  {ok} new"
                            + (f", {skip} skipped" if skip else "")
                            + (f", {err} errors"   if err   else ""))
    finally:
        wd["downloading"] = False


def _wd_start_download():
    """Kick off the water-mask rasterise in a daemon thread."""
    if disp["wd"]["downloading"]:
        return
    t = threading.Thread(target=_wd_download_thread, daemon=True,
                         name="WaterMaskDL")
    t.start()


# ── Obstacle data download ─────────────────────────────────────────────────────

def _od_load_obstacles():
    """(Re-)load the obstacle cache into module-level _obstacles."""
    import datetime
    global _obstacles
    os.makedirs(OBSTACLE_DIR, exist_ok=True)
    arr = obs_mod.load(OBSTACLE_DIR)
    _obstacles = arr
    cnt, mb = obs_mod.disk_stats(OBSTACLE_DIR)
    disp["od"]["records"] = cnt
    disp["od"]["used_mb"] = mb
    dl_date = obs_mod.download_date(OBSTACLE_DIR)
    disp["od"]["dl_date"] = dl_date
    if dl_date is not None:
        age = (datetime.date.today() - dl_date).days
        disp["od"]["age_days"] = age
        disp["od"]["expired"]  = age > OBSTACLE_EXPIRY_DAYS
    else:
        disp["od"]["age_days"] = 0
        disp["od"]["expired"]  = False


def _od_download_thread():
    """Background thread: download DOF ZIP, extract DAT, parse cache."""
    import zipfile
    import tempfile

    od = disp["od"]
    od["downloading"] = True
    od["dl_cancel"]   = False
    od["dl_status"]   = "Connecting to FAA\u2026"

    os.makedirs(OBSTACLE_DIR, exist_ok=True)
    dat_path   = os.path.join(OBSTACLE_DIR, obs_mod.DOF_FILENAME)
    cache_path = os.path.join(OBSTACLE_DIR, obs_mod.CACHE_FILENAME)

    try:
        # ── Download ZIP ──────────────────────────────────────────────────────
        od["dl_status"] = "Downloading DAILY_DOF_DAT.ZIP\u2026"
        req = urllib.request.Request(
            obs_mod.DOF_ZIP_URL,
            headers={"User-Agent": "PFD-AHRS/1.0"}
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            chunk_size  = 65536
            buf = io.BytesIO()
            while True:
                if od["dl_cancel"]:
                    od["dl_status"]   = "Cancelled"
                    od["downloading"] = False
                    return
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                buf.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = int(downloaded * 100 / total)
                    od["dl_status"] = f"Downloading\u2026 {pct}%  ({downloaded//1024} / {total//1024} KB)"
                else:
                    od["dl_status"] = f"Downloading\u2026 {downloaded//1024} KB"

        # ── Extract DAT ───────────────────────────────────────────────────────
        od["dl_status"] = "Extracting\u2026"
        buf.seek(0)
        with zipfile.ZipFile(buf) as zf:
            dat_name = next((n for n in zf.namelist()
                             if n.upper().endswith(".DAT")), None)
            if dat_name is None:
                od["dl_status"]   = "Error: no DAT in ZIP"
                od["downloading"] = False
                return
            with zf.open(dat_name) as src, open(dat_path, "wb") as dst:
                dst.write(src.read())

        # ── Parse and cache ───────────────────────────────────────────────────
        od["dl_status"] = "Parsing obstacle records\u2026"
        od["parsing"]   = True
        _od_load_obstacles()
        od["parsing"]   = False

        cnt = disp["od"]["records"]
        od["dl_status"] = f"Done \u2713  {cnt:,} obstacles loaded"

    except Exception as exc:
        od["dl_status"] = f"Error: {exc}"
    finally:
        od["downloading"] = False


def _od_start_download():
    t = threading.Thread(target=_od_download_thread, daemon=True,
                         name="ObstacleDownload")
    t.start()


# ── Obstacle data screen ──────────────────────────────────────────────────────

_OD_MX = 12   # horizontal margin

def draw_obstacle_data(surf, od):
    """Full-screen obstacle data management screen."""
    surf.fill((0, 0, 0))
    _screen_header(surf, "OBSTACLE DATA")
    bx = _OD_MX; bw = DISPLAY_W - 2*_OD_MX

    cnt      = od.get("records", 0)
    used_mb  = od.get("used_mb", 0.0)

    # ── Status strip ─────────────────────────────────────────────────────────
    pygame.draw.rect(surf, (0,12,32), (bx, 52, bw, 28), border_radius=4)
    pygame.draw.rect(surf, (40,60,90), (bx, 52, bw, 28), width=1, border_radius=4)
    if cnt:
        stat_str = f"{cnt:,} obstacles  \u00b7  {used_mb:.1f} MB on disk"
        stat_col = (60, 220, 80)
    else:
        stat_str = "No obstacle data on disk"
        stat_col = YELLOW
    _text(surf, stat_str, 12, stat_col, bold=True, cx=DISPLAY_W//2, cy=66)

    downloading = od.get("downloading", False)
    parsing     = od.get("parsing", False)

    # ── Info panel ────────────────────────────────────────────────────────────
    info_y = 92
    info_h = 90
    pygame.draw.rect(surf, (0,10,26), (bx, info_y, bw, info_h), border_radius=6)
    pygame.draw.rect(surf, (40,55,80), (bx, info_y, bw, info_h), width=1, border_radius=6)
    _text(surf, "FAA Digital Obstacle File (DOF)", 13, WHITE, bold=True,
          cx=DISPLAY_W//2, cy=info_y+16)
    _text(surf, "Covers all US obstacles > 200 ft AGL (towers, antennas, wind turbines\u2026)",
          10, (140,160,185), cx=DISPLAY_W//2, cy=info_y+34)
    _text(surf, "Single file \u2248 15\u201320 MB \u00b7 Updated every 28 days by the FAA",
          10, (120,140,165), cx=DISPLAY_W//2, cy=info_y+50)
    _text(surf, "Displayed on AI as red/amber symbols within 10 nm and \u00b12000 ft",
          10, (120,140,165), cx=DISPLAY_W//2, cy=info_y+66)
    _text(surf, "WiFi (home network) required for download",
          10, (160,130,60), cx=DISPLAY_W//2, cy=info_y+80)

    # ── Download / Update button ──────────────────────────────────────────────
    btn_y = info_y + info_h + 14
    btn_h = 54
    if downloading or parsing:
        bg = (0,20,10); oc = (40,140,60)
    else:
        bg = (0,18,45); oc = WHITE
    pygame.draw.rect(surf, bg, (bx, btn_y, bw, btn_h), border_radius=6)
    gh = btn_h // 5
    if not (downloading or parsing):
        for i in range(gh):
            t2 = 1.0 - i/gh
            gc = (int(15+t2*25), int(20+t2*40), int(40+t2*65))
            pygame.draw.line(surf, gc, (bx+6, btn_y+1+i), (bx+bw-6, btn_y+1+i))
    pygame.draw.rect(surf, oc, (bx, btn_y, bw, btn_h), width=2, border_radius=6)
    btn_label = "UPDATE" if cnt else "DOWNLOAD"
    tc = (70,80,90) if (downloading or parsing) else WHITE
    _text(surf, btn_label, 15, tc, bold=True, cx=DISPLAY_W//2, cy=btn_y+btn_h//2-8)
    sub = "DAILY_DOF_DAT.ZIP  from  aeronav.faa.gov"
    _text(surf, sub, 10, (100,120,140) if not (downloading or parsing) else (60,80,70),
          cx=DISPLAY_W//2, cy=btn_y+btn_h//2+10)

    # ── Progress / status area ────────────────────────────────────────────────
    prog_y = btn_y + btn_h + 10
    prog_h = 48
    pygame.draw.rect(surf, (0,10,24), (bx, prog_y, bw, prog_h), border_radius=6)
    pygame.draw.rect(surf, (35,50,75), (bx, prog_y, bw, prog_h), width=1, border_radius=6)

    status_msg = od.get("dl_status", "")
    if downloading:
        # Parse percentage out of status message for progress bar
        pct = 0
        try:
            if "%" in status_msg:
                pct = int(status_msg.split("%")[0].split()[-1])
        except (ValueError, IndexError):
            pct = 0
        bar_w = int((bw - 20) * pct / 100)
        pygame.draw.rect(surf, (0,22,12), (bx+10, prog_y+28, bw-20, 10), border_radius=3)
        if bar_w > 0:
            pygame.draw.rect(surf, (40,180,60), (bx+10, prog_y+28, bar_w, 10), border_radius=3)
        _text(surf, status_msg, 10, (140,160,180), cx=DISPLAY_W//2, cy=prog_y+16)
        # CANCEL
        _action_btn(surf, bw-80, prog_y+6, 72, 32, "CANCEL", "danger", r=5)
    elif parsing:
        _text(surf, status_msg, 10, (140,180,140), cx=DISPLAY_W//2, cy=prog_y+24)
    else:
        col = (60,220,80) if status_msg.startswith("Done") else (160,160,170)
        _text(surf, status_msg, 10, col, cx=DISPLAY_W//2, cy=prog_y+24)

    # ── Clearance legend ──────────────────────────────────────────────────────
    leg_y = prog_y + prog_h + 12
    leg_h = 34
    pygame.draw.rect(surf, (0,8,20), (bx, leg_y, bw, leg_h), border_radius=4)
    _text(surf, "Clearance legend:", 10, (120,140,165), x=bx+10, y=leg_y+8)
    for dx, col, lbl in [(120, RED, "< 100 ft  WARNING"),
                          (270, YELLOW, "< 500 ft  CAUTION"),
                          (430, WHITE,       "> 500 ft  CLEAR")]:
        pygame.draw.rect(surf, col, (bx+dx, leg_y+8, 10, 10))
        _text(surf, lbl, 9, (160,170,180), x=bx+dx+14, y=leg_y+8)


def obstacle_data_hit(x, y, od):
    """Return action string or None."""
    if _back_hit(x, y):
        return "back"
    bx = _OD_MX; bw = DISPLAY_W - 2*_OD_MX
    btn_y = 92 + 90 + 14   # info_y + info_h + 14
    btn_h = 54
    # Cancel during download
    if od.get("downloading"):
        prog_y = btn_y + btn_h + 10
        if (bx+bw-80 <= x <= bx+bw and prog_y+6 <= y <= prog_y+38):
            return "cancel"
    # Download/Update button
    if bx <= x <= bx+bw and btn_y <= y <= btn_y+btn_h:
        return "download"
    return None


# ── Airport data download ─────────────────────────────────────────────────────

_AD_MX = 12   # horizontal margin for airport data screen

def _ad_load_airports():
    """(Re-)load the airport and runway caches into module-level arrays."""
    import airports as apt_mod
    global _airports, _runways
    os.makedirs(AIRPORT_DIR, exist_ok=True)
    _airports = apt_mod.load(AIRPORT_DIR)
    _runways  = rwy_mod.load(AIRPORT_DIR)
    cnt, mb = apt_mod.disk_stats(AIRPORT_DIR)
    # Sum runway cache size into the total disk usage reported
    rcnt, rmb = rwy_mod.disk_stats(AIRPORT_DIR)
    disp["ad"]["records"] = cnt
    disp["ad"]["used_mb"] = mb + rmb
    disp["ad"]["runway_count"] = rcnt
    dl_date = apt_mod.download_date(AIRPORT_DIR)
    disp["ad"]["dl_date"] = dl_date
    if dl_date is not None:
        import datetime as _dt
        age = (_dt.date.today() - dl_date).days
        disp["ad"]["age_days"] = age
        disp["ad"]["expired"]  = age > AIRPORT_EXPIRY_DAYS
    else:
        disp["ad"]["age_days"] = 0
        disp["ad"]["expired"]  = False


def _ad_download_thread():
    """Background thread: download OurAirports CSV, parse, cache."""
    import airports as apt_mod

    ad = disp["ad"]
    ad["downloading"] = True
    ad["dl_cancel"]   = False
    ad["dl_status"]   = "Connecting to OurAirports\u2026"

    os.makedirs(AIRPORT_DIR, exist_ok=True)

    def _download_file(url, path, label):
        """Stream-download to path.tmp, atomic rename on success, update
        status with percentage.  Honours ad['dl_cancel']."""
        ad["dl_status"] = f"Downloading {label}\u2026"
        req = urllib.request.Request(url, headers={"User-Agent": "PFD-AHRS/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            with open(path + ".tmp", "wb") as out:
                while True:
                    if ad["dl_cancel"]:
                        try: os.remove(path + ".tmp")
                        except Exception: pass
                        return False
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    out.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = int(downloaded * 100 / total)
                        ad["dl_status"] = f"{label}: {pct}%  ({downloaded//1024} / {total//1024} KB)"
                    else:
                        ad["dl_status"] = f"{label}: {downloaded//1024} KB"
        os.replace(path + ".tmp", path)
        return True

    csv_apt   = os.path.join(AIRPORT_DIR, apt_mod.CSV_FILENAME)
    csv_rwy   = os.path.join(AIRPORT_DIR, rwy_mod.CSV_FILENAME)
    cache_apt = os.path.join(AIRPORT_DIR, apt_mod.CACHE_FILENAME)
    cache_rwy = os.path.join(AIRPORT_DIR, rwy_mod.CACHE_FILENAME)

    try:
        if not _download_file(apt_mod.AIRPORTS_CSV_URL, csv_apt, "airports.csv"):
            ad["dl_status"]   = "Cancelled"
            ad["downloading"] = False
            return
        if not _download_file(rwy_mod.RUNWAYS_CSV_URL, csv_rwy, "runways.csv"):
            ad["dl_status"]   = "Cancelled"
            ad["downloading"] = False
            return

        # Invalidate both caches so parser rebuilds fresh
        for p in (cache_apt, cache_rwy):
            try: os.remove(p)
            except Exception: pass

        ad["dl_status"] = "Parsing airport + runway records\u2026"
        ad["parsing"]   = True
        _ad_load_airports()
        ad["parsing"]   = False

        cnt = ad["records"]
        rwy_cnt = ad.get("runway_count", 0)
        ad["dl_status"] = f"Done \u2713  {cnt:,} airports, {rwy_cnt:,} runways"

    except Exception as exc:
        ad["dl_status"] = f"Error: {exc}"
    finally:
        ad["downloading"] = False


def _ad_start_download():
    t = threading.Thread(target=_ad_download_thread, daemon=True,
                         name="AirportDownload")
    t.start()


def draw_airport_data(surf, ad):
    """Full-screen airport data management screen."""
    surf.fill((0, 0, 0))
    _screen_header(surf, "AIRPORT DATA")
    bx = _AD_MX; bw = DISPLAY_W - 2*_AD_MX

    cnt      = ad.get("records", 0)
    used_mb  = ad.get("used_mb", 0.0)
    expired  = ad.get("expired", False)
    age      = ad.get("age_days", 0)

    # Status strip
    pygame.draw.rect(surf, (0,12,32), (bx, 52, bw, 28), border_radius=4)
    pygame.draw.rect(surf, (40,60,90), (bx, 52, bw, 28), width=1, border_radius=4)
    if cnt:
        age_str = f"  \u00b7  {age} day{'' if age == 1 else 's'} old"
        if expired:
            age_str += "  (expired)"
            stat_col = (220, 130, 60)
        else:
            stat_col = (60, 220, 80)
        stat_str = f"{cnt:,} airports  \u00b7  {used_mb:.1f} MB on disk{age_str}"
    else:
        stat_str = "No airport data on disk"
        stat_col = YELLOW
    _text(surf, stat_str, 12, stat_col, bold=True, cx=DISPLAY_W//2, cy=66)

    downloading = ad.get("downloading", False)
    parsing     = ad.get("parsing", False)

    # Info panel
    info_y = 92
    info_h = 90
    pygame.draw.rect(surf, (0,10,26), (bx, info_y, bw, info_h), border_radius=6)
    pygame.draw.rect(surf, (40,55,80), (bx, info_y, bw, info_h), width=1, border_radius=6)
    _text(surf, "OurAirports Global Database", 13, WHITE, bold=True,
          cx=DISPLAY_W//2, cy=info_y+16)
    _text(surf, "\u2248 72,000 airports worldwide (~20K in the US)",
          10, (140,160,185), cx=DISPLAY_W//2, cy=info_y+34)
    _text(surf, "Single CSV \u2248 12 MB \u00b7 Community-maintained, updated frequently",
          10, (120,140,165), cx=DISPLAY_W//2, cy=info_y+50)
    _text(surf, "Displayed on AI as cyan rings (airports), magenta H (heliports)",
          10, (120,140,165), cx=DISPLAY_W//2, cy=info_y+66)
    _text(surf, "WiFi (home network) required for download",
          10, (160,130,60), cx=DISPLAY_W//2, cy=info_y+80)

    # Download / Update button
    btn_y = info_y + info_h + 14
    btn_h = 54
    if downloading or parsing:
        bg = (0,20,10); oc = (40,140,60)
    else:
        bg = (0,18,45); oc = WHITE
    pygame.draw.rect(surf, bg, (bx, btn_y, bw, btn_h), border_radius=6)
    gh = btn_h // 5
    if not (downloading or parsing):
        for i in range(gh):
            t2 = 1.0 - i/gh
            gc = (int(15+t2*25), int(20+t2*40), int(40+t2*65))
            pygame.draw.line(surf, gc, (bx+6, btn_y+1+i), (bx+bw-6, btn_y+1+i))
    pygame.draw.rect(surf, oc, (bx, btn_y, bw, btn_h), width=2, border_radius=6)
    btn_label = "UPDATE" if cnt else "DOWNLOAD"
    tc = (70,80,90) if (downloading or parsing) else WHITE
    _text(surf, btn_label, 15, tc, bold=True, cx=DISPLAY_W//2, cy=btn_y+btn_h//2-8)
    sub = "airports.csv  from  ourairports-data"
    _text(surf, sub, 10, (100,120,140) if not (downloading or parsing) else (60,80,70),
          cx=DISPLAY_W//2, cy=btn_y+btn_h//2+10)

    # Progress / status area
    prog_y = btn_y + btn_h + 10
    prog_h = 48
    pygame.draw.rect(surf, (0,10,24), (bx, prog_y, bw, prog_h), border_radius=6)
    pygame.draw.rect(surf, (35,50,75), (bx, prog_y, bw, prog_h), width=1, border_radius=6)

    status_msg = ad.get("dl_status", "")
    if downloading:
        pct = 0
        try:
            if "%" in status_msg:
                pct = int(status_msg.split("%")[0].split()[-1])
        except (ValueError, IndexError):
            pct = 0
        bar_w = int((bw - 20) * pct / 100)
        pygame.draw.rect(surf, (0,22,12), (bx+10, prog_y+28, bw-20, 10), border_radius=3)
        if bar_w > 0:
            pygame.draw.rect(surf, (40,180,60), (bx+10, prog_y+28, bar_w, 10), border_radius=3)
        _text(surf, status_msg, 10, (140,160,180), cx=DISPLAY_W//2, cy=prog_y+16)
        _action_btn(surf, bw-80, prog_y+6, 72, 32, "CANCEL", "danger", r=5)
    elif parsing:
        _text(surf, status_msg, 10, (140,180,140), cx=DISPLAY_W//2, cy=prog_y+24)
    else:
        col = (60,220,80) if status_msg.startswith("Done") else (160,160,170)
        _text(surf, status_msg, 10, col, cx=DISPLAY_W//2, cy=prog_y+24)

    # Symbol legend
    leg_y = prog_y + prog_h + 12
    leg_h = 34
    pygame.draw.rect(surf, (0,8,20), (bx, leg_y, bw, leg_h), border_radius=4)
    _text(surf, "Symbol legend:", 10, (120,140,165), x=bx+10, y=leg_y+8)
    # Public airport ring
    lx = bx + 120; ly = leg_y + 13
    pygame.draw.circle(surf, (120, 220, 255), (lx, ly), 5, 0)
    pygame.draw.circle(surf, (0, 10, 30), (lx, ly), 3, 0)
    _text(surf, "PUBLIC", 9, (160,170,180), x=lx+10, y=leg_y+8)
    # Heliport H
    _text(surf, "H", 11, (220, 120, 220), bold=True, cx=bx+230, cy=ly)
    _text(surf, "HELIPORT", 9, (160,170,180), x=bx+240, y=leg_y+8)
    # Seaplane base
    sx = bx + 340; sy = ly
    pygame.draw.circle(surf, (150, 200, 255), (sx, sy), 4, 1)
    pygame.draw.line(surf, (150, 200, 255), (sx - 4, sy + 5), (sx + 4, sy + 5), 1)
    _text(surf, "SEAPLANE", 9, (160,170,180), x=sx+10, y=leg_y+8)

    # ── Display filters — toggle which airport types render on the AI ────
    filt_y = leg_y + leg_h + 14
    filt_h = 40
    _text(surf, "Display filters — tap to toggle:",
          11, (140,160,185), x=bx+6, y=filt_y-14)
    btn_w = (bw - 30) // 4
    # Row 1: airport type filters
    for i, (key, lbl) in enumerate([("show_public",   "PUBLIC"),
                                     ("show_heli",     "HELIPORTS"),
                                     ("show_seaplane", "SEAPLANE"),
                                     ("show_other",    "OTHER")]):
        bxi = bx + i * (btn_w + 10)
        _seg_btn(surf, bxi, filt_y, btn_w, filt_h, lbl, ad.get(key, False), r=6)
    # Row 2: runway + centerline + water overlays (3 wide tiles)
    row2_y = filt_y + filt_h + 10
    row2_w = (bw - 20) // 3
    _seg_btn(surf, bx,                       row2_y, row2_w, filt_h,
             "RUNWAYS", ad.get("show_runways", False), r=6)
    _seg_btn(surf, bx + row2_w + 10,         row2_y, row2_w, filt_h,
             "EXT CENTERLINES", ad.get("show_centerlines", False), r=6)
    _seg_btn(surf, bx + 2 * (row2_w + 10),   row2_y, row2_w, filt_h,
             "WATER", ad.get("show_water", False), r=6)

    # Row 3: direct-to navigation controls (3 tiles).  "DIRECT TO" opens the
    # QWERTY keyboard for ident entry; "NEAREST" picks the closest public
    # airport using current GPS pos; "CLEAR" cancels the active waypoint.
    nv = disp.get("nav", {})
    nav_ident = nv.get("ident", "")
    row3_y = row2_y + filt_h + 10
    nav_w = (bw - 20) // 3
    nav_lbl = f"DIRECT  →  {nav_ident}" if nav_ident else "DIRECT  →"
    nrst_ident = _nav_lookup_nearest()
    nrst_lbl = f"NEAREST  {nrst_ident}" if nrst_ident else "NEAREST"
    _seg_btn(surf, bx,                       row3_y, nav_w, filt_h,
             nav_lbl,   bool(nav_ident), r=6)
    _seg_btn(surf, bx + nav_w + 10,          row3_y, nav_w, filt_h,
             nrst_lbl,  False,            r=6)
    _seg_btn(surf, bx + 2 * (nav_w + 10),    row3_y, nav_w, filt_h,
             "CLEAR",   False,            r=6)


def airport_data_hit(x, y, ad):
    """Return action string or None."""
    if _back_hit(x, y):
        return "back"
    bx = _AD_MX; bw = DISPLAY_W - 2*_AD_MX
    btn_y = 92 + 90 + 14
    btn_h = 54
    # Filter toggle strip (same geometry as in draw_airport_data)
    prog_y = btn_y + btn_h + 10
    prog_h = 48
    leg_y  = prog_y + prog_h + 12
    leg_h  = 34
    filt_y = leg_y + leg_h + 14
    filt_h = 40
    btn_w  = (bw - 30) // 4
    # Row 1: airport type filters
    if filt_y <= y <= filt_y + filt_h:
        for i, key in enumerate(["show_public", "show_heli",
                                 "show_seaplane", "show_other"]):
            bxi = bx + i * (btn_w + 10)
            if bxi <= x <= bxi + btn_w:
                return f"toggle:{key}"
    # Row 2: runway + centerline + water toggles (3 wide tiles)
    row2_y = filt_y + filt_h + 10
    row2_w = (bw - 20) // 3
    if row2_y <= y <= row2_y + filt_h:
        if bx <= x <= bx + row2_w:
            return "toggle:show_runways"
        if bx + row2_w + 10 <= x <= bx + 2 * row2_w + 10:
            return "toggle:show_centerlines"
        if bx + 2 * (row2_w + 10) <= x <= bx + 2 * (row2_w + 10) + row2_w:
            return "toggle:show_water"
    # Row 3: direct-to navigation (3 tiles)
    row3_y = row2_y + filt_h + 10
    nav_w = (bw - 20) // 3
    if row3_y <= y <= row3_y + filt_h:
        if bx <= x <= bx + nav_w:
            return "nav_direct"
        if bx + nav_w + 10 <= x <= bx + 2 * nav_w + 10:
            return "nav_nearest"
        if bx + 2 * (nav_w + 10) <= x <= bx + 2 * (nav_w + 10) + nav_w:
            return "nav_clear"
    if ad.get("downloading"):
        if (bx+bw-80 <= x <= bx+bw and prog_y+6 <= y <= prog_y+38):
            return "cancel"
    if bx <= x <= bx+bw and btn_y <= y <= btn_y+btn_h:
        return "download"
    return None


def draw_terrain_data(surf, td):
    """Full-screen terrain data management screen."""
    _screen_header(surf, "TERRAIN DATA")
    bx = _TD_MX; bw = DISPLAY_W - 2*_TD_MX
    n_tiles, used_mb = _td_disk_stats()

    # Status strip
    pygame.draw.rect(surf, (0,12,32), (bx, 52, bw, 28), border_radius=4)
    pygame.draw.rect(surf, (40,60,90), (bx, 52, bw, 28), width=1, border_radius=4)
    stat_str = (f"{n_tiles} tile{'s' if n_tiles != 1 else ''} on disk  \u00b7  {used_mb:.1f} MB used"
                if n_tiles else "No tiles on disk  \u00b7  SVT uses flat terrain")
    stat_col = (60,220,80) if n_tiles else YELLOW
    _text(surf, stat_str, 12, stat_col, bold=True, cx=DISPLAY_W//2, cy=66)

    downloading = td.get("downloading", False)
    rows = (len(_TD_REGIONS) + _TD_COLS - 1) // _TD_COLS
    available_h = DISPLAY_H - _TD_MY - _TD_GAP*(rows-1) - 8
    bh = available_h // (rows + 1)   # +1 row for the "Current Area" button

    # ── Top row: Current Area | Water Masks ──────────────────────────────────
    # Two side-by-side full-height tiles.  Left = SRTM tile download for the
    # current GPS area; right = rasterise Natural Earth water masks for the
    # SRTM tiles already on disk so SVT paints oceans + lakes blue.
    half_w = (bw - _TD_GAP) // 2
    wd = disp["wd"]
    wd_busy = wd.get("downloading", False)

    # Left: Current Area (terrain SRTM)
    cur_col = (50,50,70) if downloading else (0,18,45)
    cur_oc  = (70,70,95)  if downloading else WHITE
    pygame.draw.rect(surf, cur_col, (bx, _TD_MY, half_w, bh), border_radius=6)
    gh = bh // 5
    for i in range(gh):
        t = 1.0 - i/gh
        gc = (int(15+t*25), int(20+t*40), int(40+t*65)) if not downloading else (int(20+t*20),int(20+t*20),int(30+t*30))
        pygame.draw.line(surf, gc, (bx+6, _TD_MY+1+i), (bx+half_w-6, _TD_MY+1+i))
    pygame.draw.rect(surf, cur_oc, (bx, _TD_MY, half_w, bh), width=2, border_radius=6)
    _text(surf, "DOWNLOAD CURRENT AREA", 13, cur_oc, bold=True,
          cx=bx+half_w//2, cy=_TD_MY+bh//2-8)
    lat_i = int(disp.get("lat", DEMO_LAT)); lon_i = int(disp.get("lon", DEMO_LON))
    area_str = (f"25 tiles  {lat_i}\u00b0{'N' if lat_i>=0 else 'S'} "
                f"{abs(lon_i)}\u00b0{'W' if lon_i<0 else 'E'}  \u2248 35 MB")
    _text(surf, area_str, 10, (120,140,165),
          cx=bx+half_w//2, cy=_TD_MY+bh//2+10)

    # Right: Water Masks (rasterise Natural Earth for existing SRTM tiles)
    wd_x = bx + half_w + _TD_GAP
    wd_n_tiles, wd_used_mb = water_mod.disk_stats(WATER_DIR)
    if wd_busy:
        wd_bg = (50,50,70); wd_oc = (70,70,95)
    else:
        wd_bg = (0,15,30);  wd_oc = (90, 160, 210)   # cyan-ish for water
    pygame.draw.rect(surf, wd_bg, (wd_x, _TD_MY, half_w, bh), border_radius=6)
    for i in range(gh):
        t = 1.0 - i/gh
        gc = ((int(10+t*20), int(40+t*55), int(60+t*70)) if not wd_busy
              else (int(20+t*20),int(20+t*20),int(30+t*30)))
        pygame.draw.line(surf, gc,
                         (wd_x+6, _TD_MY+1+i),
                         (wd_x+half_w-6, _TD_MY+1+i))
    pygame.draw.rect(surf, wd_oc, (wd_x, _TD_MY, half_w, bh),
                     width=2, border_radius=6)
    _text(surf, "DOWNLOAD WATER MASKS", 13, wd_oc, bold=True,
          cx=wd_x+half_w//2, cy=_TD_MY+bh//2-8)
    if wd_n_tiles:
        sub = f"{wd_n_tiles} masks on disk  \u00b7  {wd_used_mb:.1f} MB"
    else:
        sub = "Natural Earth ocean + lakes  \u2248 12 MB once"
    _text(surf, sub, 10, (120,160,190),
          cx=wd_x+half_w//2, cy=_TD_MY+bh//2+10)

    # ── Preset region grid ────────────────────────────────────────────────────
    grid_y = _TD_MY + bh + _TD_GAP
    btn_w = (bw - _TD_GAP) // 2
    for idx, region in enumerate(_TD_REGIONS):
        col = idx % _TD_COLS; row = idx // _TD_COLS
        rx = bx + col*(btn_w+_TD_GAP)
        ry = grid_y + row*(bh+_TD_GAP)
        label, sub, *_ = region
        n = _td_region_tile_count(region)
        mb = n * 1.5
        is_active = downloading and td.get("dl_region","") == label

        if is_active:
            bg=(0,28,18); oc=(40,180,60)
        elif downloading:
            bg=(0,8,18); oc=(35,45,60)
        else:
            bg=(0,12,32); oc=(55,75,105)

        pygame.draw.rect(surf, bg, (rx, ry, btn_w, bh), border_radius=6)
        if not downloading:
            gh2 = bh // 6
            for i in range(gh2):
                t2 = 1.0-i/gh2
                gc2=(int(15+t2*20),int(20+t2*35),int(40+t2*60))
                pygame.draw.line(surf, gc2, (rx+6, ry+1+i), (rx+btn_w-6, ry+1+i))
        pygame.draw.rect(surf, oc, (rx, ry, btn_w, bh), width=2, border_radius=6)
        tc = (40,180,60) if is_active else ((70,80,90) if downloading else WHITE)
        _text(surf, label, 14, tc, bold=True, cx=rx+btn_w//2, cy=ry+bh//2-12)
        _text(surf, sub,   10, (100,120,140) if not is_active else (60,180,80),
              cx=rx+btn_w//2, cy=ry+bh//2+4)
        _text(surf, f"\u223c{n} tiles  {mb:.0f} MB", 9, (70,85,105),
              cx=rx+btn_w//2, cy=ry+bh//2+18)

    # ── Download progress overlay ─────────────────────────────────────────────
    # Either terrain or water rasterisation may be active; whichever is
    # busy gets the bottom strip + cancel button.
    active_dict = None
    if downloading:
        active_dict = td
    elif wd_busy:
        active_dict = wd
    if active_dict is not None:
        prog_y = DISPLAY_H - 58
        cur = active_dict.get("dl_current", 0)
        total = max(1, active_dict.get("dl_total", 1))
        frac = cur / total
        pygame.draw.rect(surf, (0,12,32), (bx, prog_y, bw, 50), border_radius=6)
        pygame.draw.rect(surf, (55,75,105), (bx, prog_y, bw, 50), width=1, border_radius=6)
        bar_w = int((bw - 20) * frac)
        pygame.draw.rect(surf, (0,25,12), (bx+10, prog_y+28, bw-20, 12), border_radius=3)
        if bar_w > 0:
            pygame.draw.rect(surf, (40,180,60), (bx+10, prog_y+28, bar_w, 12), border_radius=3)
        _text(surf, active_dict.get("dl_status",""), 10, (140,160,180),
              cx=DISPLAY_W//2, cy=prog_y+14)
        pct = f"{int(frac*100)}%  ({cur}/{total})"
        _text(surf, pct, 10, (60,220,80), cx=DISPLAY_W//2, cy=prog_y+43)
        # CANCEL button
        _action_btn(surf, DISPLAY_W-100-bx, prog_y+6, 92, 36, "CANCEL", "danger", r=5)
    else:
        # Show whichever last status we have (prefer water if just finished)
        last = wd.get("dl_status") or td.get("dl_status", "")
        if last:
            _text(surf, last, 11, (80,160,100), cx=DISPLAY_W//2, cy=DISPLAY_H-12)


def terrain_data_hit(x, y, td):
    """Return action string or None."""
    if _back_hit(x, y):
        return "back"
    bx = _TD_MX; bw = DISPLAY_W - 2*_TD_MX
    rows = (len(_TD_REGIONS) + _TD_COLS - 1) // _TD_COLS
    available_h = DISPLAY_H - _TD_MY - _TD_GAP*(rows-1) - 8
    bh = available_h // (rows + 1)
    half_w = (bw - _TD_GAP) // 2

    # Cancel button during download (terrain or water)
    if td.get("downloading") or disp["wd"].get("downloading"):
        prog_y = DISPLAY_H - 58
        if (DISPLAY_W-100-bx <= x <= DISPLAY_W-bx and
                prog_y+6 <= y <= prog_y+42):
            return "cancel"

    # Top row: left half = current area; right half = water masks
    if _TD_MY <= y <= _TD_MY + bh:
        if bx <= x <= bx + half_w:
            return "current_area"
        wd_x = bx + half_w + _TD_GAP
        if wd_x <= x <= wd_x + half_w:
            return "water_masks"

    # Region grid
    grid_y = _TD_MY + bh + _TD_GAP
    btn_w = (bw - _TD_GAP) // 2
    for idx, region in enumerate(_TD_REGIONS):
        col = idx % _TD_COLS; row = idx // _TD_COLS
        rx = bx + col*(btn_w+_TD_GAP)
        ry = grid_y + row*(bh+_TD_GAP)
        if rx <= x <= rx+btn_w and ry <= y <= ry+bh:
            return f"region:{idx}"
    return None


# ── Cyan tap-buttons (HDG bug, BARO, ALT bug) ────────────────────────────────
# These sit just below the heading tape and below each tape's bottom edge,
# styled as cyan-bordered dark boxes matching the GI-275 blue label style.

def _cyan_box(surf, value_str, x, y, w=74, h=22, font_sz=14, col=None):
    """Illuminated tap button: r=3 corners, 2px border, top glow, no label.
    col defaults to CYAN; pass MAGENTA for GPS-sourced values."""
    if col is None:
        col = CYAN
    cr, cg, cb = col
    # Background fill
    pygame.draw.rect(surf, (0, 20, 35), (x, y, w, h), border_radius=3)
    # Top glow — tinted to border colour
    glow_h = max(4, h // 3)
    for i in range(glow_h):
        t = 1.0 - i / glow_h
        gr = min(255, int(cr * t * 0.35))
        gg = min(255, int(20 + cg * t * 0.45))
        gb = min(255, int(35 + cb * t * 0.50))
        pygame.draw.line(surf, (gr, gg, gb), (x + 2, y + 1 + i), (x + w - 3, y + 1 + i))
    # 2px border (matching veeder-root outline width)
    pygame.draw.rect(surf, col, (x, y, w, h), width=2, border_radius=3)
    # Value text — centred H+V
    _text(surf, value_str, font_sz, col, bold=True, cx=x + w // 2, cy=y + h // 2)


def draw_tap_buttons(surf, hdg, hdg_bug, baro_hpa, baro_src, alt_bug,
                     use_track=False, baro_ok=True):
    """
    Tap buttons in the heading strip — left and right only so the centre
    heading readout remains unobstructed:
      • Left  (under speed tape) : HDG bug  — MAGENTA=GPS TRK, CYAN=MAG
      • Right (under alt tape)   : Baro setting — CYAN=baro sensor, MAGENTA=GPS ALT
    IAS and ALT bug buttons are drawn at the tops of their own tapes.
    """
    y = HDG_Y

    # HDG bug — left side of heading strip; color matches heading bug triangle
    _hdg_btn = f"{round(hdg_bug) % 360:03d}\u00b0" if hdg_bug is not None else "---\u00b0"
    hdg_box_col = MAGENTA if use_track else CYAN
    _cyan_box(surf, _hdg_btn, x=SPD_X, y=y, w=SPD_W, h=HDG_H, col=hdg_box_col)

    # Baro — right side of heading strip; CYAN when baro sensor active, MAGENTA when GPS ALT
    # Accept any non-"gps" baro_src as meaning baro sensor is active (firmware uses
    # "bme280", sim/demo/preview code uses "baro" — both mean the same thing).
    if baro_ok and baro_src != "gps":
        baro_unit = disp["ds"].get("baro_unit", "inhg")
        if baro_unit == "hpa":
            baro_lbl = f"{baro_hpa:.0f} hPa"
            baro_fsz = 12
        else:
            baro_lbl = f"{baro_hpa / 33.8639:.2f} IN"
            baro_fsz = 12   # wider string needs slightly smaller font
        baro_col = CYAN
    else:
        baro_lbl = "GPS ALT"
        baro_fsz = 14
        baro_col = MAGENTA
    _cyan_box(surf, baro_lbl,
              x=ALT_X + 1, y=y, w=ALT_W - 1, h=HDG_H, font_sz=baro_fsz, col=baro_col)


# ── Veil surface for transparent overlay modes (allocated once) ───────────────
_veil_surf = None

def _draw_veil(surf):
    """Alpha-blend a dark overlay onto surf for numpad/keyboard transparency."""
    global _veil_surf
    if _veil_surf is None:
        _veil_surf = pygame.Surface((DISPLAY_W, DISPLAY_H), pygame.SRCALPHA)
        _veil_surf.fill((0, 5, 15, 180))
    surf.blit(_veil_surf, (0, 0))


# ── Obstacle symbol renderer ──────────────────────────────────────────────────

_OBS_RADIUS_NM  = OBSTACLE_RADIUS_NM
_OBS_BELOW_FT   = OBSTACLE_BELOW_FT
_OBS_CAUTION_FT = OBSTACLE_CAUTION_FT
_OBS_MIN_AGL_FT = 25.0    # hide DOF entries shorter than this so airport-
                          # surface clutter (signs, low markers, taxiway
                          # lighting) doesn't paint phantom towers on
                          # the runway / ramp.  Anything 25 ft or taller
                          # is something a VFR pilot might want to know
                          # about.
# Airport-boundary declutter: anything shorter than _OBS_AIRPORT_FLOOR_FT
# AGL gets hidden when it sits within _OBS_AIRPORT_RADIUS_NM of any
# runway centroid.  Real airport surface infrastructure (terminal
# buildings, light poles, jet bridges) is typically 25-49 ft AGL and
# clutters the runway view; tall obstructions like the ATC tower
# (321 ft AGL at PHX) stay visible because they exceed the floor.
_OBS_AIRPORT_RADIUS_NM = 1.0
_OBS_AIRPORT_FLOOR_FT  = 50.0

# Cache rendered obstacle labels keyed on (text, colour) — pygame.font
# rendering is ~1 ms each call, and a busy metro view can show 50+ towers
# whose MSL labels repeat (round-100 buckets).  Cleared on font/style
# change, capped to keep memory bounded.
_obs_label_cache = {}
_OBS_LABEL_CACHE_MAX = 256


def _obs_label_blit(surf, text, color, cx, cy):
    """Blit a small obstacle MSL label, reusing cached pygame surfaces
    so the heavy text rendering only happens once per (text, color)."""
    key = (text, color)
    img = _obs_label_cache.get(key)
    if img is None:
        font = _get_font(8, False)
        img = font.render(text, True, color)
        if len(_obs_label_cache) >= _OBS_LABEL_CACHE_MAX:
            _obs_label_cache.clear()
        _obs_label_cache[key] = img
    rect = img.get_rect(center=(cx, cy))
    surf.blit(img, rect)
_OBS_WARNING_FT = OBSTACLE_WARNING_FT

def draw_obstacle_symbols(surf, ai_rect, lat, lon, alt_ft,
                          hdg_deg, pitch_deg, roll_deg):
    """
    Project nearby obstacles onto the AI viewport as red/amber tower symbols.

    Vectorised projection: candidate obstacles come back from
    obs_mod.query_nearby as a numpy structured array, all bearing/
    distance/vertical-angle math runs over the whole batch in numpy,
    and we only fall into a Python loop to issue the pygame draw calls
    for the obstacles whose top anchor lands inside the AI rect.
    """
    import numpy as _np
    nearby = obs_mod.query_nearby(_obstacles, lat, lon,
                                  radius_nm=_OBS_RADIUS_NM,
                                  alt_ft=alt_ft,
                                  below_ft=_OBS_BELOW_FT)
    if nearby is None or len(nearby) == 0:
        return

    ax, ay_r, aw, ah = ai_rect
    cx = ax + aw // 2
    cy = ay_r + ah // 2

    # Same px/deg as the SVT and airport overlays (ai_h / 48° vertical FOV).
    # Obstacles previously used a hardcoded 8 px/deg, which didn't match the
    # SVT camera so towers shifted relative to the terrain on turn/pitch
    # and floated above ground.
    PX_PER_DEG = ah / 48.0

    nm_per_deg_lat = 60.0
    nm_per_deg_lon = 60.0 * math.cos(math.radians(lat))

    ob_lat = nearby["lat"].astype(_np.float64)
    ob_lon = nearby["lon"].astype(_np.float64)
    ob_msl = nearby["msl_ft"].astype(_np.float64)
    ob_agl = nearby["agl_ft"].astype(_np.float64)

    dlat_nm = (ob_lat - lat) * nm_per_deg_lat
    dlon_nm = (ob_lon - lon) * nm_per_deg_lon
    dist_nm = _np.hypot(dlat_nm, dlon_nm)
    bearing = _np.degrees(_np.arctan2(dlon_nm, dlat_nm)) % 360.0
    rel_brg = (bearing - hdg_deg + 180.0) % 360.0 - 180.0

    dist_ft = dist_nm * 6076.0
    # Avoid div-zero in arctan2 for co-located obstacles (just clip distance)
    dist_ft_safe = _np.maximum(dist_ft, 1.0)
    top_diff_ft  = ob_msl - alt_ft
    base_diff_ft = (ob_msl - ob_agl) - alt_ft
    top_vert_deg  = _np.degrees(_np.arctan2(top_diff_ft,  dist_ft_safe))
    base_vert_deg = _np.degrees(_np.arctan2(base_diff_ft, dist_ft_safe))

    cos_r = math.cos(math.radians(roll_deg))
    sin_r = math.sin(math.radians(roll_deg))
    sxr = rel_brg * PX_PER_DEG
    syr_top  = (pitch_deg - top_vert_deg)  * PX_PER_DEG
    syr_base = (pitch_deg - base_vert_deg) * PX_PER_DEG

    sx_top  = (cx + sxr * cos_r - syr_top  * sin_r).astype(_np.int32)
    sy_top  = (cy + sxr * sin_r + syr_top  * cos_r).astype(_np.int32)
    sy_base = (cy + sxr * sin_r + syr_base * cos_r).astype(_np.int32)

    # Airport-boundary declutter.  For every obstacle, find the
    # nearest runway centroid; if it's within _OBS_AIRPORT_RADIUS_NM
    # AND the obstacle is shorter than _OBS_AIRPORT_FLOOR_FT AGL,
    # it's airport-surface clutter (terminal buildings, jet bridges,
    # taxiway lighting) and gets hidden.  Tall airport obstructions
    # (ATC tower, long-range antennas) clear the floor and stay
    # visible.  Outside the airport boundary the only height filter
    # is _OBS_MIN_AGL_FT.
    inside_airport = _np.zeros(len(ob_lat), dtype=bool)
    if _runways is not None and len(_runways) > 0:
        nearby_rwys = rwy_mod.query_nearby(
            _runways, lat, lon,
            radius_nm=_OBS_RADIUS_NM + _OBS_AIRPORT_RADIUS_NM)
        if nearby_rwys:
            rwy_lat = _np.fromiter(
                (rw.centre_lat for rw in nearby_rwys),
                dtype=_np.float64, count=len(nearby_rwys))
            rwy_lon = _np.fromiter(
                (rw.centre_lon for rw in nearby_rwys),
                dtype=_np.float64, count=len(nearby_rwys))
            # Min-distance per obstacle against all nearby runway
            # centroids.  Broadcast (N_obs, 1) - (1, N_rwy).
            dlat_r = (ob_lat[:, None] - rwy_lat[None, :]) * nm_per_deg_lat
            dlon_r = (ob_lon[:, None] - rwy_lon[None, :]) * nm_per_deg_lon
            min_d_nm = _np.sqrt(dlat_r * dlat_r + dlon_r * dlon_r).min(axis=1)
            inside_airport = min_d_nm <= _OBS_AIRPORT_RADIUS_NM

    # Visibility mask: not co-located + top anchor inside AI rect +
    # AGL clears the global floor + (if inside an airport boundary,
    # AGL also clears the airport-clutter floor).
    agl_ok = (ob_agl >= _OBS_MIN_AGL_FT) & (
        ~inside_airport | (ob_agl >= _OBS_AIRPORT_FLOOR_FT))
    visible = ((dist_nm >= 0.01)
               & agl_ok
               & (sx_top >= ax + 4) & (sx_top <= ax + aw - 4)
               & (sy_top >= ay_r + 4) & (sy_top <= ay_r + ah - 4))
    visible_idx = _np.flatnonzero(visible)
    if visible_idx.size == 0:
        return

    clearance = alt_ft - ob_msl
    ob_lit = nearby["lit"]

    for i in visible_idx:
        cl = clearance[i]
        if cl < _OBS_WARNING_FT:
            col = RED
        elif cl < _OBS_CAUTION_FT:
            col = YELLOW
        else:
            col = WHITE

        sx, sy = int(sx_top[i]),  int(sy_top[i])
        by     = int(sy_base[i])
        tower_h = max(6, by - sy)
        apex = (sx, sy)
        base_half = max(3, tower_h // 3)
        left_base  = (sx - base_half, sy + tower_h)
        right_base = (sx + base_half, sy + tower_h)
        pygame.draw.line(surf, col, left_base,  apex, 2)
        pygame.draw.line(surf, col, right_base, apex, 2)

        if ob_lit[i]:
            r = 4
            star_col = (255, 230, 100)
            pygame.draw.line(surf, star_col, (sx - r, sy),     (sx + r, sy),     2)
            pygame.draw.line(surf, star_col, (sx,     sy - r), (sx,     sy + r), 2)
            pygame.draw.line(surf, star_col, (sx - r, sy - r), (sx + r, sy + r), 1)
            pygame.draw.line(surf, star_col, (sx - r, sy + r), (sx + r, sy - r), 1)

        # Selective labelling — pygame text render is ~1 ms each, so we
        # only label the towers that the pilot most needs to read.  Tall
        # towers (≥ 1000 ft AGL) anywhere in range, plus everything
        # within 1 nm.  Label is the obstacle's true MSL top (was
        # bucketed to nearest 100 ft, which read like the obstacle's
        # height instead of its altitude — "1100" looked like a 1100-ft
        # tower when it was a 50-ft pole at 1185 MSL).
        if ob_agl[i] >= 1000 or dist_nm[i] < 1.0:
            lbl = f"{int(ob_msl[i])}"
            _obs_label_blit(surf, lbl, col, sx, sy - 14)


def draw_airport_symbols(surf, ai_rect, lat, lon, alt_ft,
                         hdg_deg, pitch_deg, roll_deg):
    """
    Project nearby airports onto the AI viewport as small symbols + labels.

    Symbol shape encodes airport type:
      S/M/L (small/medium/large public airport) → cyan circle
      H (heliport) → magenta "H"
      W (seaplane base) → cyan circle with wavy underscore
      B (balloonport) → small cyan triangle

    Label (ident) shown only within AIRPORT_LABEL_NM to avoid clutter.
    """
    import airports as apt_mod
    if _airports is None:
        return

    # Per-category filter — user toggles on the AIRPORT DATA screen
    ad = disp["ad"]
    show = {
        "S": ad.get("show_public",   True),
        "M": ad.get("show_public",   True),
        "L": ad.get("show_public",   True),
        "H": ad.get("show_heli",     True),
        "W": ad.get("show_seaplane", False),
        "B": ad.get("show_other",    False),
    }
    if not any(show.values()):
        return

    import numpy as _np
    nearby = apt_mod.query_nearby(_airports, lat, lon,
                                  radius_nm=AIRPORT_RADIUS_NM)
    if nearby is None or len(nearby) == 0:
        return

    ax, ay_r, aw, ah = ai_rect
    cx = ax + aw // 2
    cy = ay_r + ah // 2

    # Same pixel-per-degree scale as the pitch ladder and SVT projection
    # (ai_h / 48° vertical FOV), so airport symbols align with the 3D view.
    PX_PER_DEG = ah / 48.0
    nm_per_deg_lat = 60.0
    nm_per_deg_lon = 60.0 * math.cos(math.radians(lat))
    cos_r = math.cos(math.radians(roll_deg))
    sin_r = math.sin(math.radians(roll_deg))

    max_rel_brg = (aw // 2) / PX_PER_DEG

    APT_PUBLIC  = (120, 220, 255)   # cyan — public paved/unpaved
    APT_HELI    = (220, 120, 220)   # magenta — heliport
    APT_WATER   = (150, 200, 255)   # lighter blue — seaplane base
    APT_OTHER   = (180, 180, 200)   # grey — other

    # Vectorised type filter (still in user's show set) on the structured
    # array so we never project airports that wouldn't draw anyway.
    atype_col = nearby["atype"]
    type_mask = _np.zeros(len(nearby), dtype=bool)
    for t, on in show.items():
        if on:
            type_mask |= (atype_col == t)
    if not type_mask.any():
        return
    nearby = nearby[type_mask]

    apt_lat = nearby["lat"].astype(_np.float64)
    apt_lon = nearby["lon"].astype(_np.float64)
    apt_elev = nearby["elev_ft"].astype(_np.float64)

    dlat_nm = (apt_lat - lat) * nm_per_deg_lat
    dlon_nm = (apt_lon - lon) * nm_per_deg_lon
    dist_nm = _np.hypot(dlat_nm, dlon_nm)
    bearing = _np.degrees(_np.arctan2(dlon_nm, dlat_nm)) % 360.0
    rel_brg = (bearing - hdg_deg + 180.0) % 360.0 - 180.0

    dist_ft = dist_nm * 6076.0
    dist_ft_safe = _np.maximum(dist_ft, 1.0)
    alt_diff_ft = apt_elev - alt_ft
    vert_deg = _np.degrees(_np.arctan2(alt_diff_ft, dist_ft_safe))

    sxr = rel_brg * PX_PER_DEG
    syr = (pitch_deg - vert_deg) * PX_PER_DEG
    sx_arr = (cx + sxr * cos_r - syr * sin_r).astype(_np.int32)
    sy_arr = (cy + sxr * sin_r + syr * cos_r).astype(_np.int32)

    visible = ((dist_nm >= 0.05)
               & (_np.abs(rel_brg) <= max_rel_brg)
               & (vert_deg <= 0)
               & (sx_arr >= ax + 8) & (sx_arr <= ax + aw - 8)
               & (sy_arr >= ay_r + 8) & (sy_arr <= ay_r + ah - 8))
    visible_idx = _np.flatnonzero(visible)
    if visible_idx.size == 0:
        return

    # Render farthest first so nearer ones are drawn on top (Z-order ish).
    # nearby is already sorted by distance ascending (query_nearby), so we
    # walk visible_idx in reverse.
    apt_ident = nearby["ident"]
    apt_atype = nearby["atype"]
    for i in reversed(visible_idx.tolist()):
        atype = str(apt_atype[i])
        sx, sy = int(sx_arr[i]), int(sy_arr[i])

        if atype == "H":
            col = APT_HELI
            _text(surf, "H", 12, col, bold=True, cx=sx, cy=sy)
        elif atype == "W":
            col = APT_WATER
            pygame.draw.circle(surf, col, (sx, sy), 4, 1)
            pygame.draw.line(surf, col, (sx - 4, sy + 5), (sx + 4, sy + 5), 1)
        elif atype == "B":
            col = APT_OTHER
            pts = [(sx, sy - 4), (sx - 4, sy + 3), (sx + 4, sy + 3)]
            pygame.draw.polygon(surf, col, pts, 1)
        else:
            col = APT_PUBLIC
            pygame.draw.circle(surf, col, (sx, sy), 5, 0)
            pygame.draw.circle(surf, (0, 10, 30), (sx, sy), 3, 0)
            if atype in ("M", "L"):
                pygame.draw.circle(surf, col, (sx, sy), 7, 1)

        if dist_nm[i] <= AIRPORT_LABEL_NM:
            lbl = str(apt_ident[i])
            font_sz = 9
            f = _get_font(font_sz, bold=True)
            tw, th = f.size(lbl)
            sign_w = tw + 8
            sign_h = th + 4
            post_h = 22
            sign_x = sx - sign_w // 2
            sign_y = sy - post_h - sign_h
            if sign_y < ay_r + 2:
                sign_y = ay_r + 2
                post_h = max(4, sy - sign_y - sign_h)
            pygame.draw.line(surf, col, (sx, sy - 6), (sx, sign_y + sign_h), 1)
            pygame.draw.rect(surf, (0, 10, 26),
                             (sign_x, sign_y, sign_w, sign_h), border_radius=2)
            pygame.draw.rect(surf, col,
                             (sign_x, sign_y, sign_w, sign_h), width=1, border_radius=2)
            _text(surf, lbl, font_sz, col, bold=True,
                  cx=sx, cy=sign_y + sign_h // 2)


# ── Direct-to navigation ─────────────────────────────────────────────────────
# Rudimentary GPS nav: a single active waypoint (an airport in our DB).  The
# magenta course trace runs along the ground from the aircraft to the
# waypoint; the CDI shows perpendicular cross-track from the great-circle
# course originating at the activation point.

_CDI_FULL_SCALE_NM      = 1.0    # ±1 nm full-scale en-route / D2
_CDI_APPR_FULL_SCALE_NM = 0.3    # ±0.3 nm full-scale on approach (RNAV/LPV)
_EARTH_R_NM             = 3440.065  # Earth mean radius (km/1.852)


def _ident_has_runways(ident: str) -> bool:
    """True when the airport ``ident`` has runway records loaded — used
    to decide whether to surface the APPR button on the nav-confirm
    modal."""
    if not ident or _runways is None:
        return False
    return len(_apr_runway_ends(ident)) > 0


def _nav_geo_dist_brg(la1, lo1, la2, lo2):
    """Great-circle distance (nm) and initial bearing (deg) from 1 to 2.
    Haversine + spherical-law-of-cosines bearing — accurate at any leg
    length, unlike flat-earth approximations that drift on long legs."""
    phi1 = math.radians(la1)
    phi2 = math.radians(la2)
    dphi = math.radians(la2 - la1)
    dlam = math.radians(lo2 - lo1)
    a = (math.sin(dphi * 0.5) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlam * 0.5) ** 2)
    dist = 2.0 * _EARTH_R_NM * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    y = math.sin(dlam) * math.cos(phi2)
    x = (math.cos(phi1) * math.sin(phi2)
         - math.sin(phi1) * math.cos(phi2) * math.cos(dlam))
    brg = math.degrees(math.atan2(y, x)) % 360.0
    return dist, brg


def _nav_xtk_nm(act_lat, act_lon, wpt_lat, wpt_lon, cur_lat, cur_lon):
    """Signed great-circle cross-track distance (nm) from the course
    (act → wpt) to the current position.  Positive = right of course."""
    d13, brg13 = _nav_geo_dist_brg(act_lat, act_lon, cur_lat, cur_lon)
    _,   brg12 = _nav_geo_dist_brg(act_lat, act_lon, wpt_lat, wpt_lon)
    if d13 < 1e-6:
        return 0.0
    return _EARTH_R_NM * math.asin(
        math.sin(d13 / _EARTH_R_NM)
        * math.sin(math.radians(brg13 - brg12))
    )


def _sim_intercept_heading(course_deg, xtk_nm, max_intercept=45.0,
                           approach=False):
    """Standard avionics intercept logic for the sim's FOLLOW FLT-PLAN
    autopilot.  Returns a target heading (true degrees) that brings the
    aircraft onto the course at a 45° intercept angle when far from
    track, ramping down to a gentle XTK correction once within the
    inner band.

    ``course_deg`` is the direction the course flows (TOWARD the
    destination), in true degrees.  ``xtk_nm`` is signed cross-track
    distance, positive = aircraft right of course.  The caller picks
    the right geometry for its leg length: spherical XTK
    (``_nav_xtk_nm``) for D2 legs that can run thousands of nm, or a
    cheap flat-earth projection at the threshold for short approach
    finals where the two are equivalent.

    ``approach=True`` switches to tighter approach scaling that
    matches the ±0.3 nm CDI: gentle band shrinks from 0.3 → 0.1 nm,
    full intercept reached at 0.5 nm instead of 1.5 nm, and the gentle
    gain triples so the AP actually settles on centreline rather than
    wallowing at half-scale deflection.

    Convention: positive XTK = aircraft is right of course → returned
    heading is to the LEFT of ``course_deg``."""
    if approach:
        gentle_nm   = 0.1
        full_nm     = 0.5
        gentle_gain = 100.0   # deg per nm
        gentle_cap  = 25.0
    else:
        gentle_nm   = 0.3
        full_nm     = 1.5
        gentle_gain = 30.0
        gentle_cap  = 20.0

    xtk_abs = abs(xtk_nm)
    if xtk_abs < gentle_nm:
        correction = max(-gentle_cap, min(gentle_cap, -xtk_nm * gentle_gain))
    elif xtk_abs >= full_nm:
        correction = -max_intercept if xtk_nm > 0 else max_intercept
    else:
        t = (xtk_abs - gentle_nm) / (full_nm - gentle_nm)
        sign = -1.0 if xtk_nm > 0 else 1.0
        gentle = -xtk_nm * gentle_gain
        correction = gentle * (1.0 - t) + max_intercept * sign * t
    return (course_deg + correction) % 360


def _nav_gc_interp(la1, lo1, la2, lo2, f):
    """Lat/lon at fraction f ∈ [0, 1] along the great circle from 1 to 2.
    Standard slerp on the unit sphere; degenerates gracefully when the
    endpoints coincide."""
    phi1 = math.radians(la1); lam1 = math.radians(lo1)
    phi2 = math.radians(la2); lam2 = math.radians(lo2)
    dphi = phi2 - phi1
    dlam = lam2 - lam1
    a = (math.sin(dphi * 0.5) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlam * 0.5) ** 2)
    d = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    if d < 1e-9:
        return la1, lo1
    sd = math.sin(d)
    A = math.sin((1.0 - f) * d) / sd
    B = math.sin(f * d) / sd
    x = A * math.cos(phi1) * math.cos(lam1) + B * math.cos(phi2) * math.cos(lam2)
    y = A * math.cos(phi1) * math.sin(lam1) + B * math.cos(phi2) * math.sin(lam2)
    z = A * math.sin(phi1) + B * math.sin(phi2)
    return (math.degrees(math.atan2(z, math.hypot(x, y))),
            math.degrees(math.atan2(y, x)))


def _nav_lookup_ident(ident: str):
    """Return (ident, lat, lon, elev_ft) for the first matching airport,
    or None.  Shared between the activate path and the confirmation
    modal (which validates before showing the prompt)."""
    if _airports is None or not ident:
        return None
    if hasattr(_airports, "dtype"):
        mask = _airports["ident"] == ident
        rows = _airports[mask]
        if len(rows) == 0:
            return None
        row = rows[0]
        return (str(row["ident"]), float(row["lat"]),
                float(row["lon"]), float(row["elev_ft"]))
    for rec in _airports:
        if rec[0] == ident:
            return (rec[0], float(rec[2]), float(rec[3]), float(rec[4]))
    return None


def _nav_set_by_ident(ident: str) -> bool:
    """Activate direct-to to the airport with this ident.  Returns True on hit."""
    hit = _nav_lookup_ident(ident)
    if hit is None:
        return False
    lat = disp.get("lat", 0.0)
    lon = disp.get("lon", 0.0)
    ai, alat, alon, aelev = hit
    disp["nav"]["ident"]   = ai
    disp["nav"]["lat"]     = alat
    disp["nav"]["lon"]     = alon
    disp["nav"]["elev_ft"] = aelev
    disp["nav"]["act_lat"] = lat
    disp["nav"]["act_lon"] = lon
    _settings.mark_dirty()
    return True


# Cached nearest-airport ident.  query_nearby with a 100 nm radius
# costs ~1 ms — fine on demand, wasteful at 30 Hz when the keyboard or
# AIRPORTS screen is up.  Refresh when the aircraft has moved >0.6 nm
# (lat/lon rounded to 0.01°) or 2 s has elapsed.
_nav_nearest_cache = {"lat": None, "lon": None, "ident": "", "ts": 0.0}


def _nav_lookup_nearest():
    """Return the ident of the nearest public airport (S/M/L) within
    100 nm, or "" if no airports / no fix.  Result is cached for ~0.6 nm
    of motion or 2 s of wall time so repeated render-frame calls from
    the keyboard / AIRPORTS screen don't hammer the spatial query."""
    if _airports is None:
        return ""
    lat = disp.get("lat", 0.0)
    lon = disp.get("lon", 0.0)
    rlat = round(lat, 2)
    rlon = round(lon, 2)
    now = time.monotonic()
    c = _nav_nearest_cache
    if (rlat == c["lat"] and rlon == c["lon"]
            and now - c["ts"] < 2.0):
        return c["ident"]
    nearby = apt_mod.query_nearby(_airports, lat, lon, radius_nm=100.0)
    ident = ""
    if nearby is not None and len(nearby) > 0:
        if hasattr(nearby, "dtype"):
            for i in range(len(nearby)):
                if str(nearby["atype"][i]) in ("S", "M", "L"):
                    ident = str(nearby["ident"][i])
                    break
        else:
            for apt in nearby:
                if apt.atype in ("S", "M", "L"):
                    ident = apt.ident
                    break
    c["lat"] = rlat
    c["lon"] = rlon
    c["ts"]  = now
    c["ident"] = ident
    return ident


def _nav_set_nearest() -> bool:
    """Activate direct-to to the nearest public airport (S/M/L) within 100 nm."""
    ident = _nav_lookup_nearest()
    if not ident:
        return False
    return _nav_set_by_ident(ident)


def _nav_clear() -> None:
    disp["nav"]["ident"]   = ""
    disp["nav"]["lat"]     = 0.0
    disp["nav"]["lon"]     = 0.0
    disp["nav"]["elev_ft"] = 0.0
    disp["nav"]["act_lat"] = 0.0
    disp["nav"]["act_lon"] = 0.0
    _settings.mark_dirty()


_DIRECT_TO_DRAPE_OFFSET_FT = 200.0  # ft above terrain.  Has to clear the
                                    # GPU mesh's bilinear interpolation
                                    # between its ~250 m grid samples — at
                                    # narrow ridges that interpolated
                                    # surface can sit hundreds of feet
                                    # above the DEM cell our line samples.

# Cached trace vertex array.  Key = (act_lat, act_lon, wpt_lat, wpt_lon);
# rebuilt only when the active direct-to changes.  Without this, the per-
# frame DEM sampling costs ~5 µs per step × hundreds of steps and visibly
# drops the GL frame rate.
#
# The actual SRTM sampling happens off-thread because cold-tile reads on
# a long course (up to 400 steps × several ms each) used to freeze the
# screen for 1-4 seconds after the user pressed ENTER on a new waypoint.
_direct_to_trace_cache_key = None
_direct_to_trace_cache_arr = None
_direct_to_trace_thread    = None


def _build_direct_to_trace_async(key, wpt_elev):
    """Worker: sample the great-circle course over SRTM and publish
    partial vertex arrays to the cache as we go.  The near end of the
    magenta line appears within the first second; the far end fills in
    as more SRTM tiles are read.  Exits early if the user changes the
    active direct-to mid-build."""
    global _direct_to_trace_cache_arr
    act_lat, act_lon, wpt_lat, wpt_lon = key
    course_nm, _ = _nav_geo_dist_brg(act_lat, act_lon, wpt_lat, wpt_lon)
    if course_nm < 0.05:
        return
    # 0.2 nm step: catches Sedona-grade terrain features that fit
    # between coarser samples.  Cap at 1000 so very long courses
    # don't blow the SRTM read budget on the worker thread.
    n_steps = max(8, int(course_nm / 0.2))
    n_steps = min(n_steps, 1000)

    # Publish the partial vertex array every PUBLISH_EVERY samples.
    # Sampling proceeds near→far (i=0 is the activation point near the
    # aircraft), so each publish extends the visible line outward.
    PUBLISH_EVERY = 8
    try:
        import numpy as _np
        _have_np = True
    except ImportError:
        _have_np = False

    # Track raw terrain elevations (pre-drape) so we can apply a
    # rolling max over each vertex's two neighbours before the
    # offset is added.  Without the rolling max, the line between
    # two valley samples can still dip below a peak that sits
    # between them.
    raw_elevs = []
    pts = []
    for i in range(0, n_steps + 1):
        # Bail if the user changed direct-to — the next call to
        # build_direct_to_trace_vertices() will overwrite the cache key
        # and start a fresh worker for the new course.
        if _direct_to_trace_cache_key != key:
            return
        t = i / n_steps
        s_lat, s_lon = _nav_gc_interp(act_lat, act_lon, wpt_lat, wpt_lon, t)
        try:
            terrain_elev = get_elevation_ft(SRTM_DIR, s_lat, s_lon)
            if terrain_elev is None or terrain_elev < -100:
                terrain_elev = wpt_elev
        except Exception:
            terrain_elev = wpt_elev
        raw_elevs.append(terrain_elev)

        # Rolling max over (i-1, i, i+1) becomes the publish elev for
        # vertex i.  Vertex i+1 isn't sampled yet, so we fall back to
        # max(i-1, i) for the trailing vertex; it gets corrected when
        # i+1 lands on the next iteration.
        if i == 0:
            commit_idx = 0
            commit_elev = raw_elevs[0]
        else:
            commit_idx = i - 1
            commit_elev = max(raw_elevs[commit_idx],
                              raw_elevs[commit_idx + 1])
            if commit_idx > 0:
                commit_elev = max(commit_elev, raw_elevs[commit_idx - 1])
        # Recover the lat/lon for commit_idx
        ct = commit_idx / n_steps
        c_lat, c_lon = _nav_gc_interp(act_lat, act_lon, wpt_lat, wpt_lon, ct)
        # Either append a new vertex or rewrite the previous one with
        # the now-known forward neighbour.
        if commit_idx == len(pts):
            pts.append((c_lat, c_lon, commit_elev + _DIRECT_TO_DRAPE_OFFSET_FT))
        else:
            pts[commit_idx] = (c_lat, c_lon,
                               commit_elev + _DIRECT_TO_DRAPE_OFFSET_FT)
        # On the final iteration, also commit the last vertex (which
        # only has a backward neighbour).
        if i == n_steps:
            last_elev = max(raw_elevs[i], raw_elevs[i - 1])
            l_lat, l_lon = s_lat, s_lon
            if i == len(pts):
                pts.append((l_lat, l_lon,
                            last_elev + _DIRECT_TO_DRAPE_OFFSET_FT))
            else:
                pts[i] = (l_lat, l_lon,
                          last_elev + _DIRECT_TO_DRAPE_OFFSET_FT)

        if (i + 1) % PUBLISH_EVERY == 0 or i == n_steps:
            arr = _np.array(pts, dtype=_np.float32) if _have_np else list(pts)
            # Re-check ownership immediately before publishing.  In
            # CPython attribute writes are atomic, so the render thread
            # always sees either the previous snapshot or this one.
            if _direct_to_trace_cache_key == key:
                _direct_to_trace_cache_arr = arr


def build_direct_to_trace_vertices():
    """Return the cached trace for the active direct-to.  May return a
    partial vertex array while the background sampler is still walking
    the course — the magenta line grows from the aircraft outward as
    more vertices are added.  Returns None until the worker publishes
    its first chunk (~4 samples / 2 nm)."""
    global _direct_to_trace_cache_key, _direct_to_trace_cache_arr
    global _direct_to_trace_thread

    nv = disp.get("nav", {})
    if not nv.get("ident"):
        _direct_to_trace_cache_key = None
        _direct_to_trace_cache_arr = None
        return None
    wpt_lat  = float(nv["lat"])
    wpt_lon  = float(nv["lon"])
    wpt_elev = float(nv.get("elev_ft", 0.0))
    act_lat  = float(nv.get("act_lat", 0.0))
    act_lon  = float(nv.get("act_lon", 0.0))
    if act_lat == 0.0 and act_lon == 0.0:
        return None

    key = (act_lat, act_lon, wpt_lat, wpt_lon)
    if key == _direct_to_trace_cache_key:
        # Either still building or fully done — return whatever's been
        # published so far so the renderer can show the partial line.
        return _direct_to_trace_cache_arr

    # New course.  Claim the cache key and invalidate the stale arr;
    # any in-flight worker for the previous course will see the
    # mismatched key on its next iteration and exit before stomping
    # the new cache.  We always start a fresh worker (we don't poll
    # is_alive) so back-to-back waypoint changes don't drop a build.
    _direct_to_trace_cache_key = key
    _direct_to_trace_cache_arr = None
    _direct_to_trace_thread = threading.Thread(
        target=_build_direct_to_trace_async,
        args=(key, wpt_elev),
        daemon=True,
        name="direct-to-trace",
    )
    _direct_to_trace_thread.start()
    return None


def draw_direct_to_trace(surf, ai_rect, lat, lon, alt_ft,
                         hdg_deg, pitch_deg, roll_deg):
    """2D pygame fallback: solid magenta course trace projected without
    depth occlusion.  Used only when the OpenGL SVT path is unavailable;
    in the GL path the trace renders through the depth buffer via
    svt_renderer_gl.render_polyline_latlonelev so ridges occlude it
    naturally."""
    verts = build_direct_to_trace_vertices()
    if verts is None or len(verts) < 2:
        return

    ax, ay_r, aw, ah = ai_rect
    cx_ai = ax + aw // 2
    cy_ai = ay_r + ah // 2
    px_per_deg = ah / 48.0

    old_clip = surf.get_clip()
    surf.set_clip(pygame.Rect(ax, ay_r, aw, ah))

    prev_pt = None
    for s_lat, s_lon, elev_ft in verts:
        pt = _project_latlon(float(s_lat), float(s_lon), lat, lon, alt_ft,
                             float(elev_ft),
                             hdg_deg, pitch_deg, roll_deg, cx_ai, cy_ai,
                             px_per_deg, max_fov_deg=None,
                             ground_only=False)
        if pt is None:
            prev_pt = None
            continue
        if prev_pt is not None:
            pygame.draw.line(surf, MAGENTA, prev_pt, pt, 3)
        prev_pt = pt

    surf.set_clip(old_clip)


# ── AGL readout (lower-right, just left of alt tape) ──────────────────────────
# Two-line stack: small "AGL" label on top, value below.  Grew vertically
# to give 4-digit values (e.g. 5,500) full clearance on either side of
# the inset box.
_AGL_W = 78
_AGL_H = 42


def draw_agl_readout(surf, alt_ft, ground_elev_ft, gps_ok):
    """Show "AGL\\n1234" in the lower-right corner of the AI region —
    just left of the altitude tape and just above the heading tape.
    Hidden when there's no GPS fix (no terrain lookup) or when the
    SRTM sample comes back as None / clearly invalid (out of coverage)."""
    if not gps_ok or ground_elev_ft is None:
        return
    if ground_elev_ft < -100.0:
        # Out of SRTM coverage (terrain.py returns extreme negatives
        # for missing tiles); don't display rather than misleading.
        return

    bx = ALT_X - 2 - _AGL_W
    by = HDG_Y - 2 - _AGL_H

    plate = pygame.Surface((_AGL_W, _AGL_H), pygame.SRCALPHA)
    plate.fill((0, 8, 22, 180))
    surf.blit(plate, (bx, by))
    pygame.draw.rect(surf, (140, 150, 170), (bx, by, _AGL_W, _AGL_H),
                     width=1, border_radius=4)

    # Round to the nearest 10 ft.  Both the GPS altitude and the SRTM
    # terrain sample have ~10–30 ft of real precision, so a 1-ft display
    # only shows GPS/DEM jitter in the last digit.
    agl_ft = int(round((alt_ft - ground_elev_ft) / 10.0)) * 10
    # Below the ground in the SRTM sample is sensor / DEM disagreement
    # (baro miss-set, runway elev vs SRTM, missing tile), not useful
    # info — show dashes rather than a misleading negative value.
    if agl_ft <= 0:
        val_str = "---"
        val_col = (170, 185, 210)
    else:
        val_str = f"{agl_ft:,}"
        val_col = WHITE
    _text(surf, "AGL", 11, (170, 185, 210), bold=True,
          cx=bx + _AGL_W // 2, cy=by + 11)
    _text(surf, val_str, 18, val_col, bold=True,
          cx=bx + _AGL_W // 2, cy=by + _AGL_H - 14)


def draw_cdi(surf):
    """Course Deviation Indicator strip above the heading readout box.

    Classic XTK CDI: the diamond shows where the activation→waypoint great
    circle is relative to your current position.  Right-of-course → course
    is to your left → diamond deflects LEFT → fly LEFT to intercept (fly
    TO the needle).  Full-scale at ±_CDI_FULL_SCALE_NM.

    With no active waypoint we still draw the empty bar + a "DIRECT  →"
    placeholder so the strip is always tappable and the pilot has a fixed
    entry point for the keyboard.  Caller gates the whole thing on
    gps_ok — a fix is required before the strip means anything."""
    nv = disp.get("nav", {})
    ident = nv.get("ident", "")
    have_wpt = bool(ident)

    # Bar geometry: sit just above the heading readout box.  Box height is
    # max(28, font_h+8); place the bar with a small margin above.
    bar_w = max(140, int(DISPLAY_W * 0.20))
    bar_h = 6
    bar_y = HDG_Y - 50            # leaves room for the readout box (28-32 px)
    bar_x = CX - bar_w // 2

    # Translucent backplate sized for the larger readout font
    plate = pygame.Surface((bar_w + 36, 44), pygame.SRCALPHA)
    plate.fill((0, 8, 22, 180))
    surf.blit(plate, (bar_x - 18, bar_y - 32))

    # Bar + tick marks (centre + ±50% + full-scale dots)
    pygame.draw.rect(surf, (60, 80, 110), (bar_x, bar_y, bar_w, bar_h),
                     border_radius=2)
    for frac in (-1.0, -0.5, 0.0, 0.5, 1.0):
        tx = bar_x + int((frac + 1.0) * 0.5 * bar_w)
        if frac == 0.0:
            pygame.draw.line(surf, WHITE,
                             (tx, bar_y - 4), (tx, bar_y + bar_h + 4), 2)
        else:
            pygame.draw.circle(surf, (180, 200, 220),
                               (tx, bar_y + bar_h // 2), 2)

    if have_wpt:
        lat = disp.get("lat", 0.0)
        lon = disp.get("lon", 0.0)
        wpt_lat = float(nv["lat"])
        wpt_lon = float(nv["lon"])

        dist_nm, brg = _nav_geo_dist_brg(lat, lon, wpt_lat, wpt_lon)

        ap = disp.get("approach") or {}
        appr_active = ap.get("active") and ap.get("airport") == ident
        if appr_active:
            # Approach: CDI reference is the extended runway centreline
            # (threshold + published course) — NOT the line from the
            # pilot's activation point to the threshold, which only
            # coincides with the centreline if they tapped DIRECT while
            # already perfectly lined up.
            cl_lat = float(ap["thresh_lat"])
            cl_lon = float(ap["thresh_lon"])
            course_deg = float(ap["course_deg"])
            cos_lat = max(1e-6, math.cos(math.radians(cl_lat)))
            de_nm = (lon - cl_lon) * 60.0 * cos_lat
            dn_nm = (lat - cl_lat) * 60.0
            course_rad = math.radians(course_deg)
            xtk = (de_nm * math.cos(course_rad)
                   - dn_nm * math.sin(course_rad))
            full_scale = _CDI_APPR_FULL_SCALE_NM
        else:
            act_lat = float(nv.get("act_lat", lat))
            act_lon = float(nv.get("act_lon", lon))
            xtk = _nav_xtk_nm(act_lat, act_lon, wpt_lat, wpt_lon, lat, lon)
            full_scale = _CDI_FULL_SCALE_NM

        # Diamond shows where the course line is relative to the aircraft.
        # Right-of-course (positive xtk) → diamond LEFT (course is to your left).
        xtk_clamped = max(-1.0, min(1.0, xtk / full_scale))
        dx_diamond = -int(xtk_clamped * (bar_w / 2))
        dcx = CX + dx_diamond
        dcy = bar_y + bar_h // 2
        dpts = [(dcx, dcy - 9), (dcx + 8, dcy), (dcx, dcy + 9), (dcx - 8, dcy)]
        _filled_polygon(surf, dpts, MAGENTA)

        # Readout: ident · BRG · DIST — centred above the bar.  Font 50% larger
        # than original (11→16); positioned so the text bottom sits 3 px higher
        # than the original layout would have placed it.  When an approach is
        # active, append the runway suffix to the airport ident.
        ident_lbl = f"{ident}/{ap['runway']}" if appr_active else ident
        readout = f"{ident_lbl}  {int(round(brg)) % 360:03d}°  {dist_nm:.1f}NM"
        _text(surf, readout, 16, MAGENTA, bold=True, cx=CX, cy=bar_y - 20)
    else:
        # No active waypoint — leave the bar empty (no diamond) and show
        # the "DIRECT  →" affordance so the pilot knows tapping opens the
        # keyboard.
        _text(surf, "DIRECT  →", 16, MAGENTA, bold=True, cx=CX, cy=bar_y - 20)


# ── Vertical Deviation Indicator (glideslope) ────────────────────────────────

_VDI_FULL_SCALE_DEG = 0.7   # ±0.7° full-scale (LPV / ILS-equivalent)


def draw_vdi(surf):
    """Vertical deviation indicator — only painted when an approach is
    active.  Sits just inside the right edge of the AI (left of the
    altitude tape) so the pilot's eye can sweep CDI → AI → VDI without
    leaving the primary scan.

    Convention matches every modern PFD: the diamond shows where the
    glideslope is relative to the aircraft.  Above GS → diamond moves
    DOWN (fly down to the diamond); below GS → diamond moves UP."""
    ap = disp.get("approach") or {}
    if not ap.get("active"):
        return

    lat = float(disp.get("lat", 0.0))
    lon = float(disp.get("lon", 0.0))
    alt = float(disp.get("alt", 0.0))   # baro altitude, ft
    th_lat  = float(ap["thresh_lat"])
    th_lon  = float(ap["thresh_lon"])
    th_elev = float(ap["thresh_elev_ft"])

    dist_nm, _ = _nav_geo_dist_brg(lat, lon, th_lat, th_lon)
    dist_ft = dist_nm * 6076.12
    if dist_ft < 100.0:
        return  # over the threshold — VDI no longer meaningful

    elev_angle_deg = math.degrees(math.atan2(alt - th_elev, dist_ft))
    dev_deg = elev_angle_deg - 3.0   # + above GS, - below GS

    # Bar geometry — vertical strip just inside the alt tape.
    bar_w = 6
    bar_h = max(160, int(AI_H * 0.55))
    bar_x = ALT_X - 24 - bar_w // 2
    bar_y = AI_Y + (AI_H - bar_h) // 2

    plate_w = 36
    plate = pygame.Surface((plate_w, bar_h + 40), pygame.SRCALPHA)
    plate.fill((0, 8, 22, 180))
    surf.blit(plate, (bar_x - (plate_w - bar_w) // 2, bar_y - 20))

    pygame.draw.rect(surf, (60, 80, 110), (bar_x, bar_y, bar_w, bar_h),
                     border_radius=2)

    # Tick marks (centre + ±50% + full-scale).  Centre line crosses the
    # bar; outer dots mark the 0.35° / 0.7° boundaries.
    cx_bar = bar_x + bar_w // 2
    for frac in (-1.0, -0.5, 0.0, 0.5, 1.0):
        ty = bar_y + int((frac + 1.0) * 0.5 * bar_h)
        if frac == 0.0:
            pygame.draw.line(surf, WHITE,
                             (bar_x - 4, ty), (bar_x + bar_w + 4, ty), 2)
        else:
            pygame.draw.circle(surf, (180, 200, 220), (cx_bar, ty), 2)

    # Diamond — clamped to ±full-scale.  Above GS → diamond moves DOWN.
    dev_clamped = max(-1.0, min(1.0, dev_deg / _VDI_FULL_SCALE_DEG))
    dy_diamond = int(dev_clamped * (bar_h / 2))
    dcx = cx_bar
    dcy = bar_y + bar_h // 2 + dy_diamond
    dpts = [(dcx, dcy - 8), (dcx + 9, dcy), (dcx, dcy + 8), (dcx - 9, dcy)]
    _filled_polygon(surf, dpts, MAGENTA)

    _text(surf, "G", 12, (180, 200, 220), bold=True,
          cx=cx_bar, cy=bar_y - 10)
    _text(surf, "S", 12, (180, 200, 220), bold=True,
          cx=cx_bar, cy=bar_y + bar_h + 10)


# ── Runway polygons + extended centerlines ───────────────────────────────────

_RUNWAY_MAX_RANGE_NM       = 8.0    # only draw runways within this range
_CENTERLINE_RANGE_NM       = 15.0   # draw extended centerlines within this range
_CENTERLINE_EXTEND_NM      = 5.0    # centerline extends this far from threshold
_CENTERLINE_DASH_NM        = 0.5    # dash length (nm)
_CENTERLINE_GLIDE_DEG      = 3.0    # rise centerline at standard glideslope

# Runway corners use the surveyed threshold elevation directly — the
# polygon stays on the ground where it belongs.  Earlier versions
# tried to "anchor" the corner elev to the aircraft's altitude when
# close, but that made the polygon track your altitude (not the
# ground), so descending through it looked like the runway rose to
# meet you and then vanished as the anchor disengaged.  Whatever
# small wobble baro/GPS noise produces near a corner is honest sensor
# behaviour, not something to mask.


def _clip_polygon_forward(corners, ref_lat, ref_lon, hdg_rad,
                          nm_per_deg_lat, nm_per_deg_lon):
    """Sutherland-Hodgman clip of a polygon against the perpendicular
    line through the aircraft (the "forward" half-plane).  Input is a
    list of (lat, lon, elev) tuples; output is the clipped polygon
    (also (lat, lon, elev)).  Empty list ⇒ polygon is entirely
    behind.

    Used by the runway draw to keep the polygon visible during a
    fly-over / takeoff roll, where one threshold is ahead and the
    other is behind — the unclipped quad would project corners to
    opposite ends of the AI and paint across the whole screen."""
    if not corners:
        return []

    cos_h = math.cos(hdg_rad)
    sin_h = math.sin(hdg_rad)

    def _fwd(p):
        # Dot product of (p - aircraft) with the heading unit vector
        # in nm.  Positive = ahead.
        dlat_nm = (p[0] - ref_lat) * nm_per_deg_lat
        dlon_nm = (p[1] - ref_lon) * nm_per_deg_lon
        return dlat_nm * cos_h + dlon_nm * sin_h

    fwds = [_fwd(p) for p in corners]
    n = len(corners)
    out = []
    for i in range(n):
        A   = corners[i]
        B   = corners[(i + 1) % n]
        fA  = fwds[i]
        fB  = fwds[(i + 1) % n]
        if fA >= 0:
            out.append(A)
        # Edge crosses the forward / behind boundary — interpolate the
        # crossing point and add it to the output polygon.
        if (fA >= 0) != (fB >= 0):
            t = fA / (fA - fB)
            out.append((
                A[0] + t * (B[0] - A[0]),
                A[1] + t * (B[1] - A[1]),
                A[2] + t * (B[2] - A[2]),
            ))
    return out


def _project_latlon(lat_deg, lon_deg, ref_lat, ref_lon, ref_alt_ft,
                    elev_ft, hdg_deg, pitch_deg, roll_deg,
                    cx, cy, px_per_deg, max_fov_deg=None,
                    ground_only=False, nm_per_deg_lon=None):
    """Project a lat/lon/elevation point onto the AI screen.
    Returns (sx, sy), or None when culled.
    max_fov_deg: cull points whose bearing is more than this off the nose.
    ground_only: cull points that aren't physically ground features in
                 front of the aircraft — i.e. above our altitude or
                 behind us.  We don't cull on screen-space position any
                 more (the previous syr<0 check rejected valid below-
                 horizon targets when pitch was more nose-down than the
                 angle to them, which made runways disappear on steep
                 descent).
    nm_per_deg_lon: optional precomputed 60·cos(ref_lat) — pass it from
                 hot loops (runway/centerline draws) so we don't redo
                 the cos every call."""
    nm_per_deg_lat = 60.0
    if nm_per_deg_lon is None:
        nm_per_deg_lon = 60.0 * math.cos(math.radians(ref_lat))
    dlat_nm = (lat_deg - ref_lat) * nm_per_deg_lat
    dlon_nm = (lon_deg - ref_lon) * nm_per_deg_lon
    dist_nm = math.hypot(dlat_nm, dlon_nm)
    if dist_nm < 0.001:
        return (cx, cy)
    bearing = math.degrees(math.atan2(dlon_nm, dlat_nm)) % 360.0
    rel_brg = (bearing - hdg_deg + 180) % 360 - 180
    if max_fov_deg is not None and abs(rel_brg) > max_fov_deg:
        return None
    if ground_only and abs(rel_brg) > 90.0:
        return None
    dist_ft = dist_nm * 6076.0
    alt_diff = elev_ft - ref_alt_ft
    vert_deg = math.degrees(math.atan2(alt_diff, dist_ft))
    if ground_only and vert_deg > 0.0:
        return None   # ground feature above our altitude — won't render
    sxr = rel_brg * px_per_deg
    syr = (pitch_deg - vert_deg) * px_per_deg
    cos_r = math.cos(math.radians(roll_deg))
    sin_r = math.sin(math.radians(roll_deg))
    return (int(cx + sxr * cos_r - syr * sin_r),
            int(cy + sxr * sin_r + syr * cos_r))


def draw_runway_symbols(surf, ai_rect, lat, lon, alt_ft,
                        hdg_deg, pitch_deg, roll_deg):
    """Project nearby runway polygons (and optional extended centerlines)
    onto the AI.  Runways anchor to their own threshold elevations so they
    sit flat on the terrain."""
    if _runways is None:
        return

    ad = disp["ad"]
    show_rwy   = ad.get("show_runways",     True)
    show_cline = ad.get("show_centerlines", True)
    if not (show_rwy or show_cline):
        return

    # When a direct-to waypoint is active, restrict extended centerlines to
    # the selected airport — keeps the approach picture uncluttered when
    # several airports are within the centerline range.
    sel_ident = (disp.get("nav") or {}).get("ident", "")

    nearby = rwy_mod.query_nearby(_runways, lat, lon,
                                  radius_nm=max(_RUNWAY_MAX_RANGE_NM,
                                                _CENTERLINE_RANGE_NM))
    if not nearby:
        return

    ax, ay_r, aw, ah = ai_rect
    cx = ax + aw // 2
    cy = ay_r + ah // 2
    px_per_deg = ah / 48.0

    ASPHALT = (60, 60, 65)
    STRIPE  = (230, 230, 235)
    CLINE   = (220, 230, 240)
    BOX_COL = (60, 220, 80)   # green airport-environment box
    NUM_COL = (240, 240, 250) # runway-number text

    nm_per_deg_lat = 60.0
    nm_per_deg_lon = 60.0 * math.cos(math.radians(lat))
    hdg_rad        = math.radians(hdg_deg)

    def _proj(la, lo, elev):
        # ground_only=False — wrap / behind handling is done by the
        # forward-half polygon clip in the loop, not the projection.
        return _project_latlon(la, lo, lat, lon, alt_ft,
                               elev, hdg_deg, pitch_deg, roll_deg,
                               cx, cy, px_per_deg, ground_only=False,
                               nm_per_deg_lon=nm_per_deg_lon)

    def _fwd_dist_nm(la, lo):
        """Forward distance in nm — positive when ahead of the wing
        line.  Used to gate single-point projections (centerline
        midpoints) without running them through the polygon clip."""
        dlat_nm = (la - lat) * nm_per_deg_lat
        dlon_nm = (lo - lon) * nm_per_deg_lon
        return dlat_nm * math.cos(hdg_rad) + dlon_nm * math.sin(hdg_rad)

    def _in_ai(sx, sy):
        return ax <= sx <= ax + aw and ay_r <= sy <= ay_r + ah

    # Set the AI clip rect once around the whole pass — hoisted from the
    # per-runway loop so we don't burn dozens of pygame.set_clip calls
    # per frame at busy fields.
    ai_clip_rect = pygame.Rect(ax, ay_r, aw, ah)
    old_clip = surf.get_clip()
    surf.set_clip(ai_clip_rect)

    try:
        for r in nearby:
            # Distance to runway centre (for range culling)
            d_nm = math.hypot((r.centre_lat - lat) * nm_per_deg_lat,
                              (r.centre_lon - lon) * nm_per_deg_lon)

            # ── Runway polygon ────────────────────────────────────────────────
            if show_rwy and d_nm <= _RUNWAY_MAX_RANGE_NM:
                # Runway axis in lat/lon (he - le)
                ax_lat = r.he_lat - r.le_lat
                ax_lon = r.he_lon - r.le_lon
                axis_len_nm = math.hypot(ax_lat * nm_per_deg_lat,
                                         ax_lon * nm_per_deg_lon)
                if axis_len_nm < 0.01:
                    continue
                # Unit perpendicular in nm: rotate axis 90° CW
                perp_lat_nm =  (ax_lon * nm_per_deg_lon) / axis_len_nm
                perp_lon_nm = -(ax_lat * nm_per_deg_lat) / axis_len_nm
                half_w_nm = r.width_ft / 6076.0
                perp_lat = (perp_lat_nm * half_w_nm) / nm_per_deg_lat
                perp_lon = (perp_lon_nm * half_w_nm) / nm_per_deg_lon

                # Clip the runway quad against the forward half-plane
                # through the aircraft.  When you're on or past one
                # threshold (taxi, takeoff roll, low fly-over), the
                # behind half is dropped and the polygon stays
                # visible without painting across the AI.  Returns
                # 0, 3, 4, or 5 corners.
                quad = [
                    (r.le_lat + perp_lat, r.le_lon + perp_lon, r.le_elev_ft),
                    (r.he_lat + perp_lat, r.he_lon + perp_lon, r.he_elev_ft),
                    (r.he_lat - perp_lat, r.he_lon - perp_lon, r.he_elev_ft),
                    (r.le_lat - perp_lat, r.le_lon - perp_lon, r.le_elev_ft),
                ]
                quad = _clip_polygon_forward(
                    quad, lat, lon, hdg_rad,
                    nm_per_deg_lat, nm_per_deg_lon)
                if len(quad) < 3:
                    continue   # entirely behind us

                projected = [_proj(p[0], p[1], p[2]) for p in quad]
                if any(pt is None for pt in projected):
                    continue
                # Bounding-box check — close-up the polygon can be
                # bigger than the screen and all corners can sit off
                # the bottom while the polygon still crosses the AI.
                xs = [pt[0] for pt in projected]
                ys = [pt[1] for pt in projected]
                xs_min, xs_max = min(xs), max(xs)
                ys_min, ys_max = min(ys), max(ys)
                if (xs_max >= ax and xs_min <= ax + aw and
                        ys_max >= ay_r and ys_min <= ay_r + ah):
                    _filled_polygon(surf, projected, ASPHALT)
                    pygame.gfxdraw.aapolygon(surf, projected, STRIPE)

                    # Centreline: dashed segments from LE midpoint to
                    # HE midpoint.  Clip the midline against the
                    # forward half-plane too so the dashes (and the
                    # runway-number labels) don't draw behind us.
                    midline = _clip_polygon_forward(
                        [(r.le_lat, r.le_lon, r.le_elev_ft),
                         (r.he_lat, r.he_lon, r.he_elev_ft)],
                        lat, lon, hdg_rad,
                        nm_per_deg_lat, nm_per_deg_lon)
                    mid_le = mid_he = None
                    if len(midline) >= 2:
                        mid_le = _proj(midline[0][0], midline[0][1], midline[0][2])
                        mid_he = _proj(midline[1][0], midline[1][1], midline[1][2])
                    if mid_le is not None and mid_he is not None:
                        dx = mid_he[0] - mid_le[0]
                        dy = mid_he[1] - mid_le[1]
                        seg_len = math.hypot(dx, dy)
                        if seg_len > 4:
                            n_dashes = max(2, int(seg_len / 14))
                            inv_n = 1.0 / n_dashes
                            for k in range(0, n_dashes, 2):
                                t0 = k * inv_n
                                t1 = (k + 1) * inv_n
                                pygame.draw.aaline(
                                    surf, STRIPE,
                                    (mid_le[0] + dx * t0, mid_le[1] + dy * t0),
                                    (mid_le[0] + dx * t1, mid_le[1] + dy * t1))
                        # Runway numbers: offset 8 % in from each
                        # threshold.  Only draw the end whose actual
                        # threshold is still ahead of us — once you've
                        # passed it the marker would otherwise float
                        # on the runway surface where the LE / HE
                        # marker physically isn't.
                        if seg_len > 36:
                            sz = 11 if seg_len < 120 else 13
                            if _fwd_dist_nm(r.le_lat, r.le_lon) > 0:
                                le_id = str(r.le_ident).strip().lstrip("0") or "0"
                                _text(surf, le_id, sz, NUM_COL, bold=True,
                                      cx=int(mid_le[0] + dx * 0.08),
                                      cy=int(mid_le[1] + dy * 0.08))
                            if _fwd_dist_nm(r.he_lat, r.he_lon) > 0:
                                he_id = str(r.he_ident).strip().lstrip("0") or "0"
                                _text(surf, he_id, sz, NUM_COL, bold=True,
                                      cx=int(mid_he[0] - dx * 0.08),
                                      cy=int(mid_he[1] - dy * 0.08))

                    # Airport environment box: a runway-axis-aligned
                    # rectangle 2.5× the runway's width, extending
                    # past each threshold so it reads as a frame.
                    # Clipped against the forward half-plane like the
                    # runway polygon for consistent fly-over behaviour.
                    #
                    # Suppressed close-in (within 2 nm AND under 500 ft
                    # above the threshold elev) — at that range the box
                    # is mostly off-screen anyway and its corners
                    # foreshorten enough to look broken; pilot is on
                    # final / rolling out and the runway polygon itself
                    # is the primary cue.
                    field_elev = min(r.le_elev_ft, r.he_elev_ft)
                    box_close_in = (d_nm < 2.0
                                    and (alt_ft - field_elev) < 500.0)
                    BOX_W_SCALE   = 2.5
                    BOX_LEN_PAD   = 0.10
                    axis_lat_unit = ax_lat / axis_len_nm
                    axis_lon_unit = ax_lon / axis_len_nm
                    pad_nm = axis_len_nm * BOX_LEN_PAD
                    ext_lat = axis_lat_unit * pad_nm
                    ext_lon = axis_lon_unit * pad_nm
                    pl = perp_lat * BOX_W_SCALE
                    po = perp_lon * BOX_W_SCALE
                    box_quad = _clip_polygon_forward(
                        [(r.le_lat - ext_lat + pl, r.le_lon - ext_lon + po, r.le_elev_ft),
                         (r.he_lat + ext_lat + pl, r.he_lon + ext_lon + po, r.he_elev_ft),
                         (r.he_lat + ext_lat - pl, r.he_lon + ext_lon - po, r.he_elev_ft),
                         (r.le_lat - ext_lat - pl, r.le_lon - ext_lon - po, r.le_elev_ft)],
                        lat, lon, hdg_rad,
                        nm_per_deg_lat, nm_per_deg_lon)
                    if len(box_quad) >= 3 and not box_close_in:
                        box_pts = [_proj(p[0], p[1], p[2]) for p in box_quad]
                        if all(pt is not None for pt in box_pts):
                            bxs = [pt[0] for pt in box_pts]
                            bys = [pt[1] for pt in box_pts]
                            if (max(bxs) >= ax and min(bxs) <= ax + aw and
                                    max(bys) >= ay_r and min(bys) <= ay_r + ah):
                                pygame.draw.lines(surf, BOX_COL, True,
                                                  box_pts, 2)

            # ── Extended centerlines from each threshold ──────────────────
            # Only show centerlines if within a somewhat larger range —
            # navigation aid for approach planning.  When a direct-to
            # waypoint is active, render only the selected airport's
            # centerlines.
            if (show_cline and d_nm <= _CENTERLINE_RANGE_NM and
                    (not sel_ident or str(r.airport) == sel_ident)):
                _draw_extended_centerline(
                    surf, ai_rect, r, lat, lon, alt_ft,
                    hdg_deg, pitch_deg, roll_deg, cx, cy, px_per_deg,
                    CLINE, nm_per_deg_lat, nm_per_deg_lon,
                )
    finally:
        surf.set_clip(old_clip)


def _draw_extended_centerline(surf, ai_rect, r, lat, lon, alt_ft,
                              hdg_deg, pitch_deg, roll_deg,
                              cx, cy, px_per_deg, col,
                              nm_per_deg_lat, nm_per_deg_lon):
    """Dashed line extending OUTWARD from each threshold along the reciprocal
    of the runway axis, rising at the standard glideslope so the dashes
    represent the visual approach path rather than ground projection.

    Caller (draw_runway_symbols) holds the AI clip, so we don't set/restore
    it here.  nm_per_deg_lon is passed through to _project_latlon so the
    inner cos() isn't recomputed on every dash endpoint."""
    # Axis unit vector from LE → HE in degrees
    ax_dlat = r.he_lat - r.le_lat
    ax_dlon = r.he_lon - r.le_lon
    axis_len_nm = math.hypot(ax_dlat * nm_per_deg_lat,
                             ax_dlon * nm_per_deg_lon)
    if axis_len_nm < 0.01:
        return
    # Unit vector components in degrees-per-nm
    u_dlat = ax_dlat / axis_len_nm
    u_dlon = ax_dlon / axis_len_nm

    dash_nm = _CENTERLINE_DASH_NM
    gap_nm  = _CENTERLINE_DASH_NM * 0.6
    step_nm = dash_nm + gap_nm
    n_steps = int(_CENTERLINE_EXTEND_NM / step_nm)

    # Vertical rise per nm along the approach path (ft).
    rise_ft_per_nm = math.tan(math.radians(_CENTERLINE_GLIDE_DEG)) * 6076.0

    # Angular cutoff: skip segments whose endpoint is more than 60° off the
    # nose — past that the flat-earth bearing math wraps and dashes behind
    # the aircraft would streak horizontally across the AI.
    _FOV = 60.0

    # For each threshold, extend OUTWARD (opposite of the axis toward
    # the other end).  Threshold elevations are surveyed values used
    # directly so the near end of the centerline starts on the actual
    # ground, not pinned to the aircraft altitude.
    for thresh_lat, thresh_lon, thresh_elev, sign in (
        (r.le_lat, r.le_lon, r.le_elev_ft, -1),
        (r.he_lat, r.he_lon, r.he_elev_ft, +1),
    ):
        s_dlat = sign * u_dlat
        s_dlon = sign * u_dlon
        for i in range(n_steps):
            start = step_nm * i
            end   = start + dash_nm
            ps = _project_latlon(thresh_lat + s_dlat * start,
                                 thresh_lon + s_dlon * start,
                                 lat, lon, alt_ft,
                                 thresh_elev + start * rise_ft_per_nm,
                                 hdg_deg, pitch_deg, roll_deg,
                                 cx, cy, px_per_deg, max_fov_deg=_FOV,
                                 ground_only=False,
                                 nm_per_deg_lon=nm_per_deg_lon)
            if ps is None:
                continue
            pe = _project_latlon(thresh_lat + s_dlat * end,
                                 thresh_lon + s_dlon * end,
                                 lat, lon, alt_ft,
                                 thresh_elev + end * rise_ft_per_nm,
                                 hdg_deg, pitch_deg, roll_deg,
                                 cx, cy, px_per_deg, max_fov_deg=_FOV,
                                 ground_only=False,
                                 nm_per_deg_lon=nm_per_deg_lon)
            if pe is None:
                continue
            pygame.draw.line(surf, col, ps, pe, 2)


# ── Main render function ──────────────────────────────────────────────────────
def render(surf, demo_mode, connected, data_stale=False):
    mode = disp.get("mode", "pfd")

    # In shared-GL composite mode, surf is an SRCALPHA overlay — pre-clear
    # it each frame so stale pixels from the previous frame don't leak
    # through transparent regions.  The GL framebuffer gets a safe clear
    # below (before any terrain render) so setup screens (which return
    # early) show on top of a blank background instead of last frame's
    # terrain.
    if _shared_gl_ctx is not None:
        surf.fill((0, 0, 0, 0))
        _shared_gl_ctx.screen.use()
        _shared_gl_ctx.viewport = (0, 0, DISPLAY_W, DISPLAY_H)
        _shared_gl_ctx.clear(0.0, 0.0, 0.0, 1.0)

    # ── Full-screen replacement screens (no PFD behind them) ─────────────────
    if mode == "setup":
        draw_setup_screen(surf); return
    if mode == "flight_profile":
        draw_flight_profile(surf, disp["fp"]); return
    if mode == "display_setup":
        draw_display_setup(surf, disp["ds"]); return
    if mode == "approach_select":
        draw_approach_select(surf); return
    if mode == "ahrs_setup":
        draw_ahrs_setup(surf, disp["ss"]); return
    if mode == "wifi_scan":
        draw_wifi_scan(surf, disp["cs"]); return
    if mode == "connectivity_setup":
        draw_connectivity_setup(surf, disp["cs"]); return
    if mode == "system_setup":
        draw_system_setup(surf); return
    if mode == "terrain_data":
        draw_terrain_data(surf, disp["td"]); return
    if mode == "obstacle_data":
        draw_obstacle_data(surf, disp["od"]); return
    if mode == "airport_data":
        draw_airport_data(surf, disp["ad"]); return
    if mode == "sim_setup":
        draw_sim_setup(surf); return

    # ── PFD always renders for pfd / numpad / keyboard modes ─────────────────
    # Shared-GL path already cleared surf to transparent at the top of
    # render() — skip the opaque fill here so the AI region can show
    # terrain through the compositor.
    if _shared_gl_ctx is None:
        surf.fill((0, 0, 0))

    roll    = disp["roll"]
    pitch   = disp["pitch"]
    alt     = disp["alt"]
    speed   = disp["speed"]
    vspeed  = disp["vspeed"]
    ay      = disp["ay"]
    lat     = disp["lat"]
    lon     = disp["lon"]
    track   = disp["track"]
    baro_hpa = disp["baro_hpa"]
    baro_src = disp["baro_src"]
    ahrs_ok  = disp["ahrs_ok"]
    gps_ok   = disp["gps_ok"]
    baro_ok  = disp["baro_ok"]
    sats     = disp["sats"]
    hdg_bug  = disp["hdg_bug"]
    trk_bug  = disp.get("trk_bug", 0.0)
    alt_bug  = disp["alt_bug"]

    # ── AHRS convention correction + orientation + mounting + trim ─────────
    # Base correction: the AHRS firmware reports roll and yaw with an
    # ENU-like sign convention (positive roll = LEFT wing down,
    # positive yaw = CCW from above) while the display assumes NED
    # (positive roll = RIGHT wing down, positive yaw = CW from above).
    # Pitch sign matches between ENU and NED.  Negate roll up front so
    # orientation rotations layer on top of a clean NED-aligned base;
    # yaw is negated below in the heading-source path.  The simulator
    # and demo path generate aircraft-frame (NED) values directly, so
    # skip the correction in those modes.
    #
    # ORIENTATION (which side of the AHRS the connector points toward,
    # viewed from the pilot's seat) is applied next as a yaw-axis
    # rotation that remaps the IMU's pitch / roll axes onto the
    # aircraft's, and adds a magnetic heading offset so the compass
    # reads correctly.  MOUNTING (right-side-up vs inverted) is the
    # final independent flip about the longitudinal axis.  Trim is
    # added last.
    ss = disp["ss"]
    pitch_trim = ss.get("pitch_trim", 0.0)
    roll_trim  = ss.get("roll_trim",  0.0)
    ahrs_synthetic = demo_mode or (_sim_state is not None)
    ahrs_sign = 1 if ahrs_synthetic else -1   # -1 negates for ENU→NED

    if not ahrs_synthetic:
        roll = -roll   # base ENU→NED roll correction
        orientation = ss.get("orientation", "right")
        if orientation == "forward":
            pitch, roll = -roll, pitch
            hdg_offset = 90.0
        elif orientation == "left":
            pitch, roll = -pitch, -roll
            hdg_offset = 180.0
        elif orientation == "aft":
            pitch, roll = roll, -pitch
            hdg_offset = 270.0
        else:    # "right" — default, no rotation
            hdg_offset = 0.0
        if ss.get("mounting") == "inverted":
            pitch = -pitch
            roll  = -roll
        # Trim only matters for the real AHRS — sim / demo write
        # aircraft-frame values directly so a non-zero trim would
        # show as a permanent wing-down on a level sim flight.
        pitch += pitch_trim
        roll  += roll_trim
    else:
        # Sim / demo: state values are already aircraft-frame (NED).
        # Skip every AHRS-mounting compensation, including trim.
        hdg_offset = 0.0
    # hdg_offset is applied later, after `hdg` is resolved from the
    # heading-source selector (mag / trk / auto).

    # ── Stale-data timeout: no link for > STALE_TIMEOUT_S → treat as AHRS fail
    if data_stale:
        ahrs_ok = False

    # ── Heading source selection ──────────────────────────────────────────────
    # The user picks a preference (mag / trk / auto); _resolve_hdg_source
    # turns that into the actual source given runtime conditions and
    # produces the label + colour that the heading box / setup show.
    hdg_pref = ss.get("hdg_src", "auto")
    use_track, hdg_label, hdg_color = _resolve_hdg_source(
        hdg_pref, gps_ok, ahrs_ok, speed)
    # Apply the AHRS-orientation magnetic offset to the raw yaw before
    # feeding it to the heading-source selector, AND negate raw yaw to
    # convert the firmware's ENU-style positive-yaw-CCW convention to
    # the display's NED-style positive-yaw-CW.  Sim / demo skip the
    # negation (they generate NED directly).  In TRK mode the
    # complementary filter combines yaw with GPS ground track, so the
    # corrected yaw must go IN to the filter — applying a correction
    # to the filter's output would shift the GPS-track component too.
    # Piecewise-linear mag cal: each 90° quadrant has its own
    # correction curve from the cardinal-walk wizard.  Linearly
    # interpolated by _apply_mag_cal between the four captured
    # deltas at N / E / S / W.  Shifts MAG-mode display but doesn't
    # affect TRK mode (the complementary filter operates on yaw
    # deltas — a smoothly-varying correction's derivative just
    # blends in with normal yaw motion).
    yaw_corr_uncal = (ahrs_sign * disp["yaw"] + hdg_offset) % 360.0
    if ahrs_synthetic:
        yaw_corr = yaw_corr_uncal
    else:
        deltas = ss.get("mag_cal_deltas")
        if not deltas:
            # Backward-compat: if a legacy single-offset is on disk,
            # promote it to a uniform 4-cardinal correction.
            old_off = ss.get("mag_cal_offset", 0.0)
            deltas = [old_off] * 4 if old_off else None
        yaw_corr = _apply_mag_cal(yaw_corr_uncal, deltas) if deltas else yaw_corr_uncal
    # Stash the uncalibrated yaw so the compass-cal wizard can read
    # the current "what the AHRS sees right now" value when the
    # pilot taps CAPTURE on each cardinal.
    disp["_yaw_uncal"] = yaw_corr_uncal
    if use_track:
        # Complementary filter: AHRS yaw rate propagates each frame, GPS
        # track slowly slaves the absolute reference.  Smoother than raw
        # GPS track at low speeds.
        hdg = _update_gps_heading(yaw_corr, disp["track"], gps_ok)
    else:
        global _gps_hdg, _prev_yaw_disp  # reset filter when not using TRK
        _gps_hdg = _prev_yaw_disp = None
        hdg = yaw_corr

    # ── Airspeed source selection ─────────────────────────────────────────────
    # "gps" (default): GPS groundspeed  → bug triangle is magenta
    # "ias":           IAS sensor        → bug triangle is cyan (future)
    airspeed_src = ss.get("airspeed_src", "gps")

    # ── Unit conversions ──────────────────────────────────────────────────────
    ds = disp["ds"]
    spd_unit = ds.get("spd_unit", "kt")
    alt_unit = ds.get("alt_unit", "ft")
    spd_factor = {"kt": 1.0, "mph": 1.15078, "kph": 1.852}.get(spd_unit, 1.0)
    alt_factor = {"ft": 1.0, "m": 0.3048}.get(alt_unit, 1.0)

    speed_d   = speed * spd_factor
    alt_d     = alt   * alt_factor
    alt_bug_d = (alt_bug * alt_factor) if alt_bug is not None else None
    gs_bug_d  = (disp.get("spd_bug") * spd_factor) if disp.get("spd_bug") is not None else None

    # V-speeds from flight profile, converted to display unit
    fp = disp["fp"]
    vs0_d = fp.get("vs0", VS0) * spd_factor
    vs1_d = fp.get("vs1", VS1) * spd_factor
    vfe_d = fp.get("vfe", VFE) * spd_factor
    vno_d = fp.get("vno", VNO) * spd_factor
    vne_d = fp.get("vne", VNE) * spd_factor

    ai_rect = (AI_X, AI_Y, AI_W, AI_H)

    # 0a. Lazy GL SVT probe — runs once on first frame, after pygame display
    # is already initialised so KMS/DRM is safely held before EGL touches GPU.
    global _SVT_GL_AVAILABLE
    if _SVT_GL_AVAILABLE is None:
        if _gl_available is not None:
            _SVT_GL_AVAILABLE = _gl_available()
            print(f"[PFD] OpenGL SVT: {'enabled' if _SVT_GL_AVAILABLE else 'unavailable (pygame fallback)'}")
        else:
            _SVT_GL_AVAILABLE = False

    # 0. Compute terrain/obstacle alert level for this frame
    _update_terrain_alert(lat, lon, alt, speed, gps_ok)

    # 1. AI background — draw full-width so tapes are transparent over sky/ground.
    # Shared-GL composite path renders sky+terrain directly into the default
    # framebuffer instead of blitting a pygame surface; the 2D overlay here
    # leaves the AI region transparent so terrain shows through.
    #
    # Camera altitude floor: clamp the alt passed to the SVT renderer so the
    # camera never punches through terrain (the displayed altimeter tape is
    # untouched — the aircraft's *real* altitude can still go below the
    # ground in degenerate sensor states or sim error).  100 ft above the
    # interpolated SRTM elevation keeps the eye-point above the *mesh* —
    # the rendered triangulation of a coarser grid can sit a few tens of
    # feet above the get_elevation_ft sample at the same lat/lon, and at
    # the discard-square boundary the camera sees through to sky if any
    # mesh polygon clips the eye plane.  100 ft also keeps the runway in
    # frame when the aircraft is rolling on it.
    _ground_elev_ft = get_elevation_ft(SRTM_DIR, lat, lon) if gps_ok else 0.0
    alt_render = max(alt, _ground_elev_ft + 100.0)
    _full_ai = (0, 0, DISPLAY_W, HDG_Y)
    # Without a GPS fix we don't know where the aircraft is, so any SVT
    # rendering would be lying about the world.  Fall back to plain
    # blue-over-brown so attitude (pitch/roll from AHRS) is still
    # readable but the synthetic terrain is suppressed.
    # Real-time sun position drives terrain shading when enabled and a
    # GPS fix is live.  Off / no-fix → renderer falls back to its
    # built-in SE / mid-morning constants (bright daylight that always
    # reads).
    _sun_az = _sun_el = _sun_int = None
    if ds.get("sun_realtime", True) and gps_ok:
        try:
            _sun_az, _sun_el, _sun_int = _sun_mod.solar_position(lat, lon)
        except Exception:
            _sun_az = _sun_el = _sun_int = None

    # Below-horizon mesh-gap colour: same 6-band palette the GLSL
    # clearance_color() uses on the terrain mesh, driven by the SRTM
    # clearance under the aircraft (the same value the AGL readout
    # reflects).  This makes any gap blend continuously with the band
    # the surrounding mesh is painting — red where mesh is red, deep
    # orange in the 200–300 ft band, amber 300–700, brown 700–1200,
    # dark brown 1200–2200, very dark > 2200.  Bands match
    # FRAGMENT_SHADER clearance_color() exactly so the gap and the
    # nearest mesh fragment never differ by more than the band edge.
    _clr = alt - _ground_elev_ft if gps_ok else 9999.0
    if   _clr < 200:  _below_col = (0.86, 0.12, 0.12)
    elif _clr < 300:  _below_col = (0.86, 0.31, 0.0)
    elif _clr < 700:  _below_col = (0.78, 0.51, 0.0)
    elif _clr < 1200: _below_col = (0.55, 0.39, 0.16)
    elif _clr < 2200: _below_col = (0.39, 0.29, 0.14)
    else:             _below_col = (0.27, 0.22, 0.11)

    if _shared_gl_ctx is not None and gps_ok:
        # Render terrain into the AI region of the default framebuffer.
        # GL viewport origin is bottom-left: pygame AI row 0..HDG_Y maps to
        # GL rows HDG_H..DISPLAY_H.
        _shared_gl_ctx.viewport = (0, HDG_H, DISPLAY_W, HDG_Y)
        # Build polyline list for depth-tested rendering on top of terrain.
        # When an approach is active, the HITS boxes replace the magenta
        # D2 trace — the boxes already convey the path in 3D and the
        # extra magenta line clutters the corridor.
        _gl_polylines = []
        _ap = disp.get("approach") or {}
        if not _ap.get("active"):
            _trace_verts = build_direct_to_trace_vertices()
            if _trace_verts is not None and len(_trace_verts) >= 2:
                _gl_polylines.append((
                    _trace_verts,
                    (220 / 255.0, 0.0, 220 / 255.0, 1.0),
                    3.0,
                ))
        # HITS boxes — cyan rectangles along the extended centreline
        # at 3° glideslope when a synthetic approach is active.  The
        # boxes feed the same depth-tested polyline path as the D2
        # trace, so terrain occludes them naturally where ridges
        # block the corridor.
        if _ap.get("active"):
            _gl_polylines.extend(_hits_mod.build_box_polylines(
                _ap["thresh_lat"], _ap["thresh_lon"],
                _ap["thresh_elev_ft"], _ap["course_deg"],
            ))
        render_svt_into_current_fb(
            _shared_gl_ctx, SRTM_DIR,
            DISPLAY_W, HDG_Y,
            pitch, roll, hdg, alt_render, lat, lon,
            polylines=_gl_polylines,
            water_dir=WATER_DIR,
            water_enable=disp["ad"].get("show_water", True),
            airports_arr=_airports,
            sun_az_deg=_sun_az,
            sun_el_deg=_sun_el,
            sun_intensity=_sun_int,
            below_horizon_color=_below_col,
        )
        _shared_gl_ctx.viewport = (0, 0, DISPLAY_W, DISPLAY_H)
    elif _has_terrain and gps_ok:
        draw_ai_background(surf, _full_ai, pitch, roll, hdg, alt_render, lat, lon)
    else:
        draw_simple_ai_background(surf, _full_ai, pitch, roll)

    # 1b. Symbol overlays on the AI — runways, airports, obstacles, and the
    # direct-to course trace.  The draw functions already project with the
    # given roll_deg (cos/sin in their per-feature math), so we pass the
    # real roll and write straight onto the main surface.  An older path
    # drew everything onto a SRCALPHA overlay rotated by pygame at the end,
    # which cost ~20 ms/frame in a turn at 1024×600 — replaced with the
    # per-feature projection-roll for that win.
    if gps_ok and (
            _runways is not None or _airports is not None or _obstacles is not None):
        # Roll sign: the per-feature projections rotate by (cos/sin) in math
        # convention but write to screen-Y-down pixels, which flips CW/CCW.
        # The original SRCALPHA-rotate path used pygame.transform.rotate's
        # screen convention, so we negate roll here to match it — symbols
        # and terrain bank together as the pilot expects.
        _ov_roll = -roll
        if _runways is not None:
            draw_runway_symbols(surf, _full_ai, lat, lon, alt_render, hdg, pitch, _ov_roll)
        if _airports is not None:
            draw_airport_symbols(surf, _full_ai, lat, lon, alt_render, hdg, pitch, _ov_roll)
        if _obstacles is not None:
            # Obstacles project from REAL alt, not alt_render.  The
            # camera-floor clamp pushes alt_render up to ~100 ft above
            # the SRTM-derived ground when we're near the surface, and
            # using that for obstacle math made low-AGL FAA DOF entries
            # (jetway masts, terminal cornices, lighting around major
            # airports like PHX) project below the horizon — they
            # painted "phantom" tower symbols all over the runway and
            # ramp because their real top elevation reads below the
            # clamped camera position.  Real alt restores the correct
            # geometry: a 30 ft tower 1000 ft away is above the horizon
            # by 1.7°, regardless of where the SVT camera floor sits.
            draw_obstacle_symbols(surf, _full_ai, lat, lon, alt, hdg, pitch, _ov_roll)
        # Direct-to course trace — depth-tested 3D in the shared-GL path,
        # 2D pygame fallback when no GL.  Suppressed when an approach is
        # active (HITS boxes carry the path instead).
        if _shared_gl_ctx is None and not (disp.get("approach") or {}).get("active"):
            draw_direct_to_trace(surf, _full_ai, lat, lon, alt_render, hdg, pitch, _ov_roll)

    # 1c. Zero-pitch reference line — always horizontal across AI at
    # screen-centre, regardless of actual horizon position.  Critical with
    # 3D SVT because high terrain shifts the visible horizon away from 0°.
    _svt_3d_active = ((SVT_RENDERER == "opengl" and _SVT_GL_AVAILABLE)
                      or _shared_gl_ctx is not None)
    if _svt_3d_active:
        draw_zero_pitch_line(surf, ai_rect, pitch, roll)

    # 1d. Lower-left moving-map inset (pure-pygame; reuses the airport,
    # runway, obstacle and SRTM caches the SVT already keeps loaded).
    # Drawn after symbols so the inset frame sits on top, before the
    # pitch ladder so the ladder reads through unobstructed.
    if ds.get("map_enabled", False) and gps_ok:
        _miw = max(140, int(AI_W * 0.30))
        _mih = max(120, int(AI_H * 0.40))
        rect = (AI_X + 6,
                AI_Y + AI_H - _mih - 6,
                _miw, _mih)
        global _last_map_rect
        _last_map_rect = rect
        d2_src = disp.get("nav") or {}
        # Tag the dict the inset receives with the current approach
        # state so moving_map.render can colour the course line cyan
        # (approach) vs magenta (regular D2).  When approach is active,
        # carry the final-approach course so the inset can draw the
        # cyan line FROM the threshold OUT along the corridor (matches
        # what the pilot sees as HITS boxes on the SVT) instead of as
        # a generic D2 line from activation point to threshold.
        d2 = dict(d2_src)
        _ap = disp.get("approach") or {}
        d2["approach_active"] = bool(_ap.get("active"))
        if _ap.get("active"):
            d2["approach_course_deg"] = float(_ap.get("course_deg", 0.0))
            d2["approach_final_nm"]   = _hits_mod.DEFAULT_FINAL_NM
        # GPS track sticks at its last value when groundspeed drops to
        # zero (stationary on the ramp), so passing it straight to the
        # inset would freeze the rotation at whatever heading we last
        # taxied in.  Below taxi-speed (3 kt — same threshold the AUTO
        # heading source uses) suppress track and let the inset fall
        # back to mag heading so yawing the nose visibly rotates the
        # map in TRK↑ mode.
        _map_track = track if speed >= 3.0 else None
        # Translate the main-PFD airport-type filters (managed on the
        # AIRPORT DATA screen) into the set of atype letters the inset
        # should draw.  Sharing the flags with the main PFD means the
        # pilot sets type filters once and both displays honour them.
        _ad = disp.get("ad", {})
        _types_vis = set()
        if _ad.get("show_public", True):
            _types_vis.update({"S", "M", "L"})
        if _ad.get("show_heli", True):
            _types_vis.add("H")
        if _ad.get("show_seaplane", False):
            _types_vis.add("W")
        if _ad.get("show_other", False):
            _types_vis.add("B")
        _map_mod.render(
            surf, rect, lat, lon, alt, hdg, _map_track,
            ds.get("map_orient", "trk"),
            int(ds.get("map_zoom_nm", 5)),
            ds,
            airports_arr=_airports,
            runways_arr=_runways,
            obstacles_arr=_obstacles,
            srtm_dir=SRTM_DIR,
            water_dir=WATER_DIR,
            direct_to=d2 if d2.get("ident") else None,
            font=_get_font(11, bold=True),
            airport_types_visible=_types_vis,
            gs_kt=speed,
        )

    # 2. Pitch ladder (with roll rotation)
    draw_pitch_ladder(surf, ai_rect, pitch, roll)

    # 3. Speed tape (display unit, fp V-speeds)
    draw_speed_tape(surf, speed_d, gs_bug=gs_bug_d,
                    vs0=vs0_d, vs1=vs1_d, vfe=vfe_d, vno=vno_d, vne=vne_d,
                    airspeed_src=airspeed_src)

    # 4. Alt tape (display unit)
    draw_alt_tape(surf, alt_d, vspeed, baro_hpa, baro_src, alt_bug_d,
                  baro_ok=baro_ok)

    # 4b. AGL readout — sits in the gap between the alt tape and the
    # heading tape.  Reuses the SRTM sample we already took for the
    # camera-floor clamp, so it costs ~nothing.  Uses real (sensor)
    # alt, not the clamped alt_render, so a punched-ground state still
    # reads negative as a warning.
    draw_agl_readout(surf, alt, _ground_elev_ft, gps_ok)

    # 5. Heading tape — show the bug that matches the active source: TRK
    # mode shows the track bug, MAG mode shows the heading bug.  Setting
    # one doesn't disturb the other so flipping sources preserves both.
    active_bug = trk_bug if use_track else hdg_bug
    draw_heading_tape(surf, hdg, active_bug, track=track, yaw=disp["yaw"],
                      gps_ok=gps_ok, ahrs_ok=ahrs_ok, use_track=use_track,
                      hdg_label=hdg_label, hdg_color=hdg_color)

    # 5b. CDI — direct-to course deviation indicator above the heading box.
    # No-op when no waypoint is active.
    if gps_ok:
        draw_cdi(surf)
        draw_vdi(surf)   # vertical glideslope, only paints when approach active

    # 6. Roll arc
    draw_roll_arc(surf, roll)

    # 7. Aircraft symbol
    draw_aircraft_symbol(surf)

    # 8. Slip ball
    draw_slip_ball(surf, ay)

    # 9. Status badges
    draw_status_badges(surf, ahrs_ok, gps_ok, baro_ok, baro_src, sats, connected,
                       use_track=use_track)

    # 9b. Terrain / obstacle proximity alert banner (centre of badge strip)
    draw_terrain_alert(surf)

    # 10. Failure overlays
    draw_failure_overlays(surf, ahrs_ok, gps_ok, baro_ok, sats)

    # 11. Tap-buttons for heading bug, baro, and alt bug (color = data source)
    draw_tap_buttons(surf, hdg, active_bug, baro_hpa, baro_src, alt_bug,
                     use_track=use_track, baro_ok=baro_ok)

    # 12. Demo / SIM watermark.  When the simulator is running, the
    # watermark doubles as a clearly-tappable button — pilots otherwise
    # don't realise the small "SIM" text is interactive and end up
    # killing the whole PFD process to leave the simulator.
    if demo_mode:
        _text(surf, "DEMO", 14, (255, 60, 60), cx=CX, cy=CY - 20)
    elif _sim_state is not None:
        _action_btn(surf, _SIM_EXIT_X, _SIM_EXIT_Y, _SIM_EXIT_W, _SIM_EXIT_H,
                    "SIM  ✕", style="danger", r=6)

    # ── Overlay modes: veil + UI drawn on top of live PFD ────────────────────
    if mode == "sim_controls":
        draw_sim_controls(surf)

    elif mode == "nav_confirm":
        draw_nav_confirm(surf)

    elif mode == "mag_cal":
        draw_mag_cal(surf)

    elif mode == "numpad":
        _draw_veil(surf)
        target  = disp.get("numpad_target", "")
        buf     = disp.get("numpad_buf", "")
        baro_unit = disp["ds"].get("baro_unit", "inhg")
        # Build baro current value in integer entry form
        if baro_unit == "hpa":
            baro_cur  = int(round(disp["baro_hpa"]))
            baro_title = "SET BARO  (hPa)"
            baro_dec   = 0
        else:
            baro_cur  = int(round(disp["baro_hpa"] / 33.8639 * 100))  # e.g. 2992
            baro_title = "SET BARO  (in Hg)"
            baro_dec   = 2
        # Bug entries are shown (and entered) in current display unit.  Storage
        # stays canonical (kt/ft) — conversion is applied at commit time.
        spd_unit_lbl = {"kt": "kt", "mph": "mph", "kph": "kph"}.get(
            disp["ds"].get("spd_unit", "kt"), "kt")
        alt_unit_lbl = {"ft": "ft", "m": "m"}.get(
            disp["ds"].get("alt_unit", "ft"), "ft")
        spd_bug_src  = "IAS" if airspeed_src == "ias" else "GS"
        spd_bug_title = f"SET {spd_bug_src} BUG  ({spd_unit_lbl})"
        titles  = {"alt_bug":   f"SET ALTITUDE BUG  (\u00d7100 {alt_unit_lbl})",
                   "hdg_bug":   "SET HEADING BUG",
                   "trk_bug":   "SET TRACK BUG",
                   "spd_bug":   spd_bug_title,
                   "baro_hpa":  baro_title,
                   "sim_init_alt": f"SET INITIAL ALTITUDE  (\u00d7100 {alt_unit_lbl})",
                   "sim_init_hdg": "SET INITIAL HEADING",
                   "sim_init_spd": "SET INITIAL SPEED  (kt)"}
        curvals = {"alt_bug":   int(round(disp.get("alt_bug", 0)
                                          * _ALT_DISP_FACTOR())) // 100,
                   "hdg_bug":   int(disp.get("hdg_bug", 0)),
                   "trk_bug":   int(disp.get("trk_bug", 0)),
                   "spd_bug":   int(round(disp.get("spd_bug", 0)
                                          * _SPD_DISP_FACTOR())),
                   "baro_hpa":  baro_cur,
                   "sim_init_alt": int(round(disp["sim"]["init_alt"]
                                             * _ALT_DISP_FACTOR())) // 100,
                   "sim_init_hdg": int(disp["sim"]["init_hdg"]),
                   "sim_init_spd": int(disp["sim"]["init_spd"])}
        for fkey, flabel, *rest in _FP_FIELDS:
            if fkey not in titles:
                titles[fkey]  = f"SET {flabel}"
                _v = disp["fp"].get(fkey, 0)
                # Only numeric fields are numpad-editable; skip string fields (tail, actype)
                if rest and len(rest) >= 3 and rest[2] == "kbd":
                    continue
                try:
                    curvals[fkey] = int(_v)
                except (ValueError, TypeError):
                    continue
        dec = baro_dec if target == "baro_hpa" else 0
        # sim_init_alt also uses ×100 suffix like alt_bug
        sim_alt_suffix = "00" if target == "sim_init_alt" else ""
        draw_numpad(surf, titles.get(target, "ENTER VALUE"),
                    curvals.get(target, 0), buf,
                    suffix=("00" if target == "alt_bug" else sim_alt_suffix),
                    transparent=True,
                    decimal_after=dec)

    elif mode == "keyboard":
        _draw_veil(surf)
        target = disp.get("kbd_target", "")
        buf    = disp.get("kbd_buf", "")
        prev   = disp.get("kbd_prev", "flight_profile")
        if target == "nav_ident":
            # Direct-to ident lives under disp["nav"], not the connectivity
            # or flight-profile dicts.  Surface it as the placeholder so
            # the user sees what's active while typing the replacement.
            cur   = disp.get("nav", {}).get("ident", "")
            title = "WAYPOINT"
        elif prev == "connectivity_setup":
            cur   = disp["cs"].get(target, "")
            title = {"ahrs_url": "AHRS URL", "wifi_ssid": "WiFi SSID",
                     "wifi_pass": "WiFi PASSWORD"}.get(target, "ENTER TEXT")
        else:
            cur   = disp["fp"].get(target, "")
            title = next((f[1] for f in _FP_FIELDS if f[0]==target), "ENTER TEXT")
        draw_keyboard(surf, f"ENTER {title}", cur, buf, transparent=True,
                      error=disp.get("kbd_error", ""))


# ── Terrain availability (computed once at import time) ───────────────────────
def _check_terrain():
    if not os.path.isdir(SRTM_DIR):
        return False
    return any(f.endswith(".hgt") for f in os.listdir(SRTM_DIR))

_has_terrain = _check_terrain()


def _startup_load_obstacles():
    """Background thread: load obstacle cache at startup without blocking."""
    _od_load_obstacles()
    cnt = disp["od"]["records"]
    if cnt:
        print(f"[PFD] Obstacles: {cnt:,} records loaded")
    else:
        print("[PFD] Obstacles: no data on disk")


def _startup_load_airports():
    """Background thread: load airport cache at startup without blocking."""
    _ad_load_airports()
    if _airports is not None:
        print(f"[PFD] Airports: {len(_airports):,} records loaded")
    else:
        print("[PFD] Airports: no data on disk")


# ── Main entry point ──────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="PFD Display")
    parser.add_argument("--demo", action="store_true",
                        help="Run Sedona demo (no Pico W needed)")
    parser.add_argument("--sim",  action="store_true",
                        help="Windowed mode for desktop testing")
    # Screenshot mode: render one frame to a PNG then exit.
    # Useful for capturing SVT terrain renders with real SRTM tiles on hardware.
    parser.add_argument("--screenshot", metavar="FILE",
                        help="Render one PFD frame to FILE (.png) and exit")
    parser.add_argument("--screenshots", metavar="DIR",
                        help="Generate a full set of preview PNGs to DIR and exit")
    parser.add_argument("--ss-lat",    type=float, default=DEMO_LAT, metavar="DEG",
                        help="Screenshot latitude  (default: Sedona)")
    parser.add_argument("--ss-lon",    type=float, default=DEMO_LON, metavar="DEG",
                        help="Screenshot longitude (default: Sedona)")
    parser.add_argument("--ss-alt",    type=float, default=DEMO_ALT, metavar="FT",
                        help="Screenshot altitude ft MSL")
    parser.add_argument("--ss-hdg",    type=float, default=DEMO_HDG, metavar="DEG",
                        help="Screenshot heading degrees")
    parser.add_argument("--ss-pitch",  type=float, default=2.0,      metavar="DEG",
                        help="Screenshot pitch degrees (nose-up positive)")
    parser.add_argument("--ss-roll",   type=float, default=0.0,      metavar="DEG",
                        help="Screenshot roll degrees (right-wing-down positive)")
    parser.add_argument("--ss-speed",  type=float, default=115.0,    metavar="KT",
                        help="Screenshot groundspeed kt")
    parser.add_argument("--ss-vspeed", type=float, default=0.0,      metavar="FPM",
                        help="Screenshot vertical speed fpm")
    args = parser.parse_args()

    if args.sim or not FULLSCREEN:
        # Desktop / windowed mode — let SDL auto-detect the display server
        # (x11 on X.Org, wayland on Wayfire/Weston, etc.) instead of forcing
        # kmsdrm which is only correct for bare-console fullscreen.
        os.environ.pop("SDL_VIDEODRIVER", None)
        os.environ.pop("SDL_FBDEV", None)

    # Restore persisted user settings (V-speeds, units, brightness, trims,
    # airport filters, etc.).  Must run BEFORE _set_backlight so the
    # restored brightness is used.  No-op on first run (no file yet).
    if _settings.load_into(disp, SETTINGS_PATH):
        print(f"[PFD] Settings restored from {SETTINGS_PATH}")
    # Migrate legacy hdg_src values: "gps" was the old name for "trk".
    if disp["ss"].get("hdg_src") == "gps":
        disp["ss"]["hdg_src"] = "trk"
    if disp["ss"].get("hdg_src") not in ("mag", "trk", "auto"):
        disp["ss"]["hdg_src"] = "auto"
    _settings.start(disp, SETTINGS_PATH)

    _init_backlight()
    _set_backlight(disp["ds"].get("brightness", 8))

    # Load obstacle + airport databases in background (non-blocking)
    threading.Thread(target=_startup_load_obstacles, daemon=True,
                     name="ObstacleLoad").start()
    threading.Thread(target=_startup_load_airports, daemon=True,
                     name="AirportLoad").start()

    # Disable vsync so display.flip() doesn't block waiting for the display's
    # vsync signal (which was taking ~82 ms at ~12 Hz on KMS/DRM, halving FPS).
    os.environ.setdefault("SDL_RENDER_VSYNC", "0")
    os.environ.setdefault("SDL_VIDEO_KMSDRM_VSYNC", "0")

    # pygame.init() returns (n_success, n_failed).  If the video subsystem
    # silently fails (typically because something else is holding the DRM
    # master after a previous crash), every later display call errors with
    # "video system not initialized".  Detect and exit clean — without this
    # systemd would tight-loop the service for hours.
    init_ok, init_failed = pygame.init()
    if init_failed > 0 or not pygame.display.get_init():
        sys.stderr.write(
            f"[PFD] pygame.init failed (success={init_ok}, failed={init_failed}).\n"
            f"[PFD] Video subsystem is not initialised — usually means another\n"
            f"[PFD] process is holding /dev/dri/card0.  Try `sudo lsof /dev/dri/card*`\n"
            f"[PFD] or reboot to clear stuck DRM state.  Exiting.\n"
        )
        sys.exit(2)
    try:
        pygame.mouse.set_visible(False)
    except pygame.error:
        # Mouse subsystem not available under some headless drivers; ignore.
        pass

    # ── Shared-context GL composite path ─────────────────────────────────────
    # When SVT_RENDERER == "opengl_shared", pygame owns the display in
    # pygame.OPENGL mode and moderngl attaches to that context.  Terrain
    # renders directly into the default framebuffer; the 2D PFD layer is
    # drawn onto a separate SRCALPHA surface and uploaded as a GL texture
    # for alpha-blended composite each frame.  Falls back to the normal
    # pygame-display path on any setup failure.  Disabled in screenshot
    # modes since those read pixels back from `surf`, which in shared-GL
    # mode contains only the 2D overlay (no terrain).
    _use_shared_gl = False
    gl_ctx = None
    gl_compositor = None
    use_shared_req = (SVT_RENDERER == "opengl_shared"
                      and HAS_SHARED_GL
                      and render_svt_into_current_fb is not None
                      and not args.screenshot
                      and not args.screenshots)
    if use_shared_req:
        try:
            screen, gl_ctx = setup_gl_display(
                DISPLAY_W, DISPLAY_H,
                fullscreen=(not args.sim) and FULLSCREEN,
            )
            gl_compositor = Compositor(
                gl_ctx, DISPLAY_W, DISPLAY_H,
                rotate_deg=DISPLAY_ROTATE,
            )
            surf = pygame.Surface((DISPLAY_W, DISPLAY_H), pygame.SRCALPHA)
            _sw = _sh = _sx = _sy = None
            _use_shared_gl = True
            global _shared_gl_ctx, _shared_gl_compositor
            _shared_gl_ctx = gl_ctx
            _shared_gl_compositor = gl_compositor
            print("[PFD] SVT renderer: opengl_shared (pygame.OPENGL composite)")
        except Exception as e:
            print(f"[PFD] opengl_shared setup failed ({e}); falling back to pygame display")
            _use_shared_gl = False
            gl_ctx = None
            gl_compositor = None

    if not _use_shared_gl:
        if (not args.sim) and FULLSCREEN:
            if DISPLAY_ROTATE:
                # Rotated display: need explicit native-res surface + manual transform.
                info = pygame.display.Info()
                _native_w = info.current_w if info.current_w > 0 else DISPLAY_W
                _native_h = info.current_h if info.current_h > 0 else DISPLAY_H
                screen = pygame.display.set_mode(
                    (_native_w, _native_h),
                    pygame.FULLSCREEN | pygame.NOFRAME
                )
                _scale = min(_native_w / DISPLAY_W, _native_h / DISPLAY_H)
                _sw = int(DISPLAY_W * _scale)
                _sh = int(DISPLAY_H * _scale)
                _sx = (_native_w - _sw) // 2
                _sy = (_native_h - _sh) // 2
                surf = pygame.Surface((DISPLAY_W, DISPLAY_H))
            else:
                # Use SDL2's built-in logical scaling (pygame.SCALED). SDL2 scales
                # the 640×480 logical surface to the physical display size in C,
                # which is ~10× faster than pygame.transform.scale() in Python
                # (~80 ms saved per frame on Pi Zero 2W scaling to 1080p).
                # SDL_RENDER_VSYNC=0 (set above) disables vsync on the SDL_Renderer
                # that SCALED creates internally, removing the vsync-wait overhead.
                screen = pygame.display.set_mode(
                    (DISPLAY_W, DISPLAY_H),
                    pygame.FULLSCREEN | pygame.SCALED
                )
                surf = screen
                _sw = _sh = _sx = _sy = None
        else:
            screen = pygame.display.set_mode((DISPLAY_W, DISPLAY_H))
            surf = screen
            _sw = _sh = _sx = _sy = None

    def _flip():
        """Present the PFD surface to the physical display.

        In the normal (non-rotated) fullscreen path, surf IS screen and SDL2
        handles the logical→physical scaling internally via pygame.SCALED, so
        this function is just a pygame.display.flip() call.

        The rotated path (DISPLAY_ROTATE != 0) still does an explicit
        transform+scale because SDL2's logical-size API doesn't handle rotation.

        In shared-GL composite mode, terrain has already been rendered into
        the default framebuffer by render(); here we just upload the 2D
        overlay surface as a texture and draw it as a fullscreen quad.
        """
        if _use_shared_gl:
            gl_compositor.upload_and_draw(surf)
            pygame.display.flip()
            return
        if surf is not screen:
            # Rotated display — manual transform + scale
            s = pygame.transform.rotate(surf, DISPLAY_ROTATE)
            screen.fill((0, 0, 0))
            screen.blit(pygame.transform.scale(s, (_sw, _sh)), (_sx, _sy))
        pygame.display.flip()

    pygame.display.set_caption("PFD")
    clock = pygame.time.Clock()

    # ── Screenshot mode ───────────────────────────────────────────────────────
    # Seed state directly (bypasses IIR smoothing), render one frame, save PNG.
    # Run on the Pi with SRTM tiles installed to capture real SVT renders.
    #   python3 pfd.py --screenshot ~/ss/sedona_cruise.png
    #   python3 pfd.py --screenshot ~/ss/custom.png --ss-lat 34.87 --ss-lon -111.76 \
    #                  --ss-alt 8500 --ss-hdg 133 --ss-pitch 5 --ss-roll -18
    if args.screenshot:
        snap = {
            "lat": args.ss_lat, "lon": args.ss_lon,
            "alt": args.ss_alt, "yaw": args.ss_hdg,
            "track": args.ss_hdg, "pitch": args.ss_pitch,
            "roll": args.ss_roll, "speed": args.ss_speed,
            "vspeed": args.ss_vspeed, "ay": 0.0,
            "gps_ok": True, "baro_ok": True, "ahrs_ok": True,
            "sats": 8, "gps_alt": args.ss_alt,
            "baro_hpa": BARO_DEFAULT_HPA, "baro_src": "baro",
            "fix": True, "pitch_trim": 0.0, "roll_trim": 0.0, "yaw_trim": 0.0,
        }
        with _state_lock:
            state.update(snap)
        disp.update(snap)           # bypass IIR: seed disp directly from snap
        disp["hdg_bug"] = args.ss_hdg
        disp["alt_bug"] = args.ss_alt
        smooth_state()              # now a no-op (disp already matches state)
        render(surf, demo_mode=False, connected=True, data_stale=False)
        _flip()
        outpath = os.path.abspath(args.screenshot)
        os.makedirs(os.path.dirname(outpath) or ".", exist_ok=True)
        pygame.image.save(surf, outpath)
        print(f"[PFD] Screenshot → {outpath}")
        pygame.quit()
        return

    # ── Batch screenshots mode ────────────────────────────────────────────────
    if args.screenshots:
        outdir = os.path.abspath(args.screenshots)
        os.makedirs(outdir, exist_ok=True)

        # Load airport DB synchronously so symbols appear in preview renders
        _startup_load_airports()

        def _save(fname):
            smooth_state()
            render(surf, demo_mode=False, connected=True, data_stale=False)
            _flip()
            pygame.image.save(surf, os.path.join(outdir, fname))
            print(f"  → {fname}")

        def _seed(**kwargs):
            """Seed state + disp with a scene's flight values."""
            snap = {
                "lat": kwargs.get("lat", DEMO_LAT),
                "lon": kwargs.get("lon", DEMO_LON),
                "yaw": kwargs.get("hdg", 133),
                "track": kwargs.get("track", kwargs.get("hdg", 133)),
                "roll": kwargs.get("roll", 0),
                "pitch": kwargs.get("pitch", 2),
                "speed": kwargs.get("speed", 115),
                "alt": kwargs.get("alt", 8500),
                "vspeed": kwargs.get("vspeed", 0),
                "ay": kwargs.get("ay", 0),
                "gps_ok": kwargs.get("gps_ok", True),
                "baro_ok": kwargs.get("baro_ok", True),
                "ahrs_ok": kwargs.get("ahrs_ok", True),
                "sats": kwargs.get("sats", 8),
                "gps_alt": kwargs.get("alt", 8500),
                "baro_hpa": BARO_DEFAULT_HPA,
                "baro_src": "baro" if kwargs.get("baro_ok", True) else "gps",
                "fix": kwargs.get("gps_ok", True),
                "pitch_trim": 0.0, "roll_trim": 0.0, "yaw_trim": 0.0,
            }
            with _state_lock:
                state.update(snap)
            disp.update(snap)
            disp["hdg_bug"] = kwargs.get("hdg_bug", kwargs.get("hdg", 133))
            disp["alt_bug"] = kwargs.get("alt_bug", kwargs.get("alt", 8500))
            if "spd_bug" in kwargs:
                disp["spd_bug"] = kwargs["spd_bug"]
            else:
                disp["spd_bug"] = 0
            disp["mode"]    = "pfd"
            disp["ss"]["hdg_src"] = kwargs.get("hdg_src", "mag")

        # ── Flight scenes ─────────────────────────────────────────────────────
        _seed(roll=0,   pitch=2,  hdg=133, alt=8500, speed=115, vspeed=0)
        _save("preview_sedona_level.png")

        _seed(roll=-18, pitch=6,  hdg=145, alt=7800, speed=95,  vspeed=500,  ay=0.12)
        _save("preview_sedona_climb_turn.png")

        _seed(roll=0,   pitch=-3, hdg=200, alt=5800, speed=90,  vspeed=-700)
        _save("preview_sedona_approach.png")

        # GPS TRK heading mode
        _seed(roll=0, pitch=2, hdg=133, alt=8500, speed=115, hdg_src="trk")
        _save("preview_gps_trk_mode.png")

        # Badge states
        _seed(roll=0, pitch=0, hdg=133, alt=8500, speed=115, baro_ok=False)
        _save("preview_badges_no_data.png")

        # Expired obstacles: set od state
        _seed(roll=0, pitch=0, hdg=133, alt=8500, speed=115)
        disp["od"]["expired"] = True
        disp["od"]["records"] = 76842
        _save("preview_badges_exp_obs.png")
        disp["od"]["expired"] = False

        # ── PFD hero shot (matches root pfd_preview.png) ──────────────────────
        _seed(roll=0, pitch=2, hdg=133, alt=8500, speed=115)
        _save("pfd_preview.png")

        # ── Numpad overlays (PFD underneath) ──────────────────────────────────
        _seed(roll=0, pitch=2, hdg=133, alt=8500, speed=115)
        disp["mode"] = "numpad"
        disp["numpad_target"] = "alt_bug"
        disp["numpad_buf"]    = "85"
        _save("preview_numpad_alt.png")

        disp["numpad_target"] = "hdg_bug"
        disp["numpad_buf"]    = "133"
        _save("preview_numpad_hdg.png")

        disp["ds"]["baro_unit"] = "inhg"
        disp["numpad_target"] = "baro_hpa"
        disp["numpad_buf"]    = "2992"
        _save("preview_numpad_baro_inhg.png")

        disp["ds"]["baro_unit"] = "hpa"
        disp["numpad_target"] = "baro_hpa"
        disp["numpad_buf"]    = "1013"
        _save("preview_numpad_baro_hpa.png")
        disp["ds"]["baro_unit"] = "inhg"

        # ── Keyboard overlay ──────────────────────────────────────────────────
        disp["mode"] = "keyboard"
        disp["kbd_target"] = "tail"
        disp["kbd_buf"]    = "N12345"
        disp["kbd_prev"]   = "flight_profile"
        _save("preview_keyboard.png")

        # ── Setup screens ─────────────────────────────────────────────────────
        for screen_mode, fname in [
            ("setup",               "preview_setup_main.png"),
            ("flight_profile",      "preview_setup_flight_profile.png"),
            ("display_setup",       "preview_setup_display.png"),
            ("ahrs_setup",          "preview_setup_ahrs.png"),
            ("connectivity_setup",  "preview_setup_connectivity.png"),
            ("system_setup",        "preview_setup_system.png"),
        ]:
            disp["mode"] = screen_mode
            _save(fname)

        # AHRS setup with GPS TRK selected
        disp["ss"]["hdg_src"] = "trk"
        disp["mode"] = "ahrs_setup"
        _save("preview_setup_ahrs_gpstrk.png")
        disp["ss"]["hdg_src"] = "mag"

        # ── Terrain data screen states ────────────────────────────────────────
        disp["mode"] = "terrain_data"
        disp["td"]["downloading"] = False
        disp["td"]["dl_region"]   = ""
        disp["td"]["dl_current"]  = 0
        disp["td"]["dl_total"]    = 0
        disp["td"]["dl_status"]   = ""
        _save("preview_terrain_idle.png")

        disp["td"]["downloading"] = True
        disp["td"]["dl_region"]   = "US Southwest"
        disp["td"]["dl_current"]  = 47
        disp["td"]["dl_total"]    = 132
        disp["td"]["dl_status"]   = "Downloading N35W111.hgt\u2026"
        _save("preview_terrain_downloading.png")

        disp["td"]["downloading"] = False
        disp["td"]["dl_region"]   = ""

        # ── Obstacle data screen states ───────────────────────────────────────
        disp["mode"] = "obstacle_data"
        disp["od"]["downloading"] = False
        disp["od"]["records"]     = 0
        disp["od"]["used_mb"]     = 0.0
        disp["od"]["dl_status"]   = ""
        _save("preview_obstacle_idle.png")

        disp["od"]["records"] = 76842
        disp["od"]["used_mb"] = 19.4
        disp["od"]["dl_status"] = "Done \u2713  76,842 obstacles loaded"
        _save("preview_obstacle_loaded.png")

        disp["od"]["downloading"] = True
        disp["od"]["records"]     = 0
        disp["od"]["dl_status"]   = "Downloading\u2026 38%  (7,440 / 19,584 KB)"
        _save("preview_obstacle_downloading.png")

        disp["od"]["downloading"] = False
        disp["od"]["dl_status"]   = ""

        # ── Airport data screen states ────────────────────────────────────────
        disp["mode"] = "airport_data"
        disp["ad"]["downloading"] = False
        disp["ad"]["records"]     = 0
        disp["ad"]["used_mb"]     = 0.0
        disp["ad"]["dl_status"]   = ""
        disp["ad"]["expired"]     = False
        _save("preview_airport_idle.png")

        disp["ad"]["records"] = 72007
        disp["ad"]["used_mb"] = 12.3
        disp["ad"]["age_days"] = 5
        disp["ad"]["dl_status"] = "Done \u2713  72,007 airports loaded"
        _save("preview_airport_loaded.png")

        disp["ad"]["downloading"] = True
        disp["ad"]["records"]     = 0
        disp["ad"]["dl_status"]   = "Downloading\u2026 42%  (5,280 / 12,500 KB)"
        _save("preview_airport_downloading.png")

        disp["ad"]["downloading"] = False
        disp["ad"]["dl_status"]   = ""

        # ── Direct-to keyboard with placeholder + NEAREST extras row ─────────
        # Set up an active waypoint so the placeholder shows and the
        # NEAREST extras button has something to render.
        _seed(roll=0, pitch=2, hdg=133, alt=8500, speed=115)
        disp["nav"] = {
            "ident": "KSEZ", "lat": 34.85, "lon": -111.79, "elev_ft": 4830,
            "act_lat": 34.84, "act_lon": -111.80,
        }
        disp["kbd_target"] = "nav_ident"
        disp["kbd_prev"]   = "pfd"
        disp["kbd_buf"]    = ""
        disp["kbd_error"]  = ""
        disp["mode"]       = "keyboard"
        _save("preview_direct_to_keyboard.png")

        # Same keyboard mid-error: pilot typed an unknown ident
        disp["kbd_buf"]   = "ZZZZ"
        disp["kbd_error"] = "UNKNOWN WAYPOINT  ZZZZ"
        _save("preview_unknown_waypoint.png")
        disp["kbd_buf"]   = ""
        disp["kbd_error"] = ""

        # ── Direct-to confirmation modal (Activate Direct to KSEZ?) ──────────
        _seed(roll=0, pitch=2, hdg=133, alt=8500, speed=115)
        disp["nav_confirm_ident"] = "KSEZ"
        disp["nav_confirm_prev"]  = "pfd"
        disp["mode"]              = "nav_confirm"
        _save("preview_nav_confirm.png")

        # ── Compass calibration wizard mid-walk (step 2 = EAST) ──────────────
        # Pre-load the wizard with one captured cardinal so the modal shows
        # the "Captured NORTH." status, the step-2 NORTH→EAST transition, and
        # the four cardinal Δ slots (still zeroed before the cal commits on
        # the 4th capture).  Heading aligned near EAST so RAW reads right.
        _seed(roll=0, pitch=0, hdg=88, alt=4830, speed=0,
              vspeed=0, ay=0, gps_ok=True)
        disp["yaw"]        = 88.0
        disp["_yaw_uncal"] = 88.0
        disp["mag_cal_wiz"] = {
            "step": 1, "samples": [(0.0, 358.5)],
            "msg":  "Captured NORTH.",
            "prev": "ahrs_setup",
        }
        disp["mode"] = "mag_cal"
        _save("preview_compass_cal.png")

        # Compass cal completed — show the four cardinal Δ values
        disp["ss"]["mag_cal_deltas"] = [1.2, -0.8, 0.7, -1.5]
        disp["mag_cal_wiz"] = {
            "step": 0, "samples": [],
            "msg":  "Done — N+1.2° E-0.8° S+0.7° W-1.5°",
            "prev": "ahrs_setup",
        }
        _save("preview_compass_cal_done.png")
        disp["ss"]["mag_cal_deltas"] = [0.0] * 4

        # AGL readout — close-up of lower-right corner via a normal cruise
        # frame; the 78×42 box appears above the heading tape automatically.
        _seed(roll=0, pitch=2, hdg=133, alt=6800, speed=115, vspeed=0)
        disp["mode"] = "pfd"
        _save("preview_agl_readout.png")

        # AHRS orientation row variants — capture each of the four selections
        # so the docs can show the highlight state of each segment.
        for orient, fname in (("forward", "preview_setup_ahrs_orient_fwd.png"),
                              ("left",    "preview_setup_ahrs_orient_left.png"),
                              ("right",   "preview_setup_ahrs_orient_right.png"),
                              ("aft",     "preview_setup_ahrs_orient_aft.png")):
            disp["ss"]["orientation"] = orient
            disp["mode"] = "ahrs_setup"
            _save(fname)
        disp["ss"]["orientation"] = "right"

        # ── Terrain proximity alert scenes ────────────────────────────────────
        # Force terrain alert by seeding alert state directly.  Renders over
        # normal PFD so alerts show even without SRTM tiles loaded.
        import pfd as _pfd_self  # noqa — reference this module
        # Caution: alt slightly above a simulated terrain high point
        _seed(roll=0, pitch=-2, hdg=133, alt=5500, speed=95, vspeed=-200)
        try:
            # Poke the terrain-alert module state if accessible
            globals()['_terrain_alert_level'] = 1  # caution (amber)
            globals()['_terrain_alert_alpha'] = 1.0
        except Exception:
            pass
        _save("preview_terrain_caution.png")

        _seed(roll=0, pitch=-5, hdg=133, alt=5200, speed=95, vspeed=-400)
        try:
            globals()['_terrain_alert_level'] = 2  # warning (red flashing)
            globals()['_terrain_alert_alpha'] = 1.0
        except Exception:
            pass
        _save("preview_terrain_warning.png")

        # Reset alert state
        try:
            globals()['_terrain_alert_level'] = 0
            globals()['_terrain_alert_alpha'] = 0.0
        except Exception:
            pass

        # ── VR cascade demo (alt = 9980 — shows rolling digits mid-cascade) ──
        _seed(roll=0, pitch=0, hdg=133, alt=9980, speed=115, vspeed=0)
        _save("preview_vr_cascade.png")

        disp["mode"] = "pfd"
        print(f"\n[PFD] Batch screenshots → {outdir}")
        pygame.quit()
        return

    demo_mode  = args.demo
    demo       = DemoState() if demo_mode else None
    connected  = False
    data_stale = False
    global _link_lost_t, _multitouch_t0, _active_fingers

    if not demo_mode:
        global _sse_client
        # Try USB serial first — direct connection, no WiFi needed
        try:
            from serial_client import SerialClient
            _usb_port = SerialClient.find_port()
        except ImportError:
            _usb_port = None
        if _usb_port:
            _sse_client = SerialClient(_usb_port, state, _state_lock)
            _sse_client.start()
            disp["cs"]["ahrs_transport"] = "usb"
            disp["cs"]["ahrs_port"]      = _usb_port
            print(f"[PFD] AHRS via USB serial: {_usb_port}")
        else:
            _sse_client = SSEClient(SSE_URL, state, _state_lock)
            _sse_client.start()
            disp["cs"]["ahrs_transport"] = "wifi"
            disp["cs"]["ahrs_port"]      = SSE_URL
            print(f"[PFD] AHRS via WiFi SSE: {SSE_URL}")
        threading.Thread(target=_poll_wifi_status, daemon=True,
                         name="WiFiPoll").start()
        threading.Thread(target=_poll_ahrs_diag,  daemon=True,
                         name="AhrsDiag").start()
    else:
        # Seed initial state for demo
        state["alt"]   = DEMO_ALT
        state["speed"] = 115.0
        state["yaw"]   = DEMO_HDG
        state["lat"]   = DEMO_LAT
        state["lon"]   = DEMO_LON
        disp["hdg_bug"] = DEMO_HDG
        disp["alt_bug"] = DEMO_ALT
        print("[PFD] Demo mode — Sedona AZ")

    running = True
    while running:
        # Update demo state
        if demo_mode and demo:
            demo.tick()

        # Update flight simulator state (mutually exclusive with demo)
        if _sim_state is not None:
            _sim_state.tick()

        # Smooth sensor values into display values
        smooth_state()

        # Events
        for event in pygame.event.get():
            result = handle_event(event, demo_mode)
            if result is False:
                running = False
            elif result == "toggle_demo":
                demo_mode = not demo_mode
                if demo_mode:
                    demo = DemoState()

        if _sse_client:
            connected = _sse_client.connected
            disp["cs"]["ahrs_ok"] = connected
            # Stale-data timeout: track when link first dropped
            if not connected:
                if _link_lost_t is None:
                    _link_lost_t = time.monotonic()
                data_stale = (time.monotonic() - _link_lost_t) > STALE_TIMEOUT_S
            else:
                _link_lost_t = None
                data_stale   = False

        # Sim or demo provides its own data — SSE link state is irrelevant
        if _sim_state is not None or demo_mode:
            connected  = True
            data_stale = False

        # 2-finger hold → enter setup screen (EXIT button returns to PFD)
        if (_multitouch_t0 is not None
                and len(_active_fingers) >= 2
                and pygame.time.get_ticks() - _multitouch_t0 >= LONG_PRESS_MS
                and disp["mode"] == "pfd"):
            disp["mode"] = "setup"
            _active_fingers.clear()
            _multitouch_t0 = None

        # Render
        _t0 = time.monotonic()
        render(surf, demo_mode, connected, data_stale=data_stale)
        _t1 = time.monotonic()
        _flip()
        _t2 = time.monotonic()
        clock.tick(TARGET_FPS)

        # Print frame timing every 60 frames so we can diagnose bottlenecks
        if not hasattr(main, '_frame_n'):
            main._frame_n = 0
        main._frame_n += 1
        if main._frame_n % 60 == 0:
            render_ms = (_t1 - _t0) * 1000
            flip_ms   = (_t2 - _t1) * 1000
            fps       = clock.get_fps()
            print(f"[PFD] fps={fps:.1f}  render={render_ms:.1f}ms  flip={flip_ms:.1f}ms")

    if _sse_client:
        _sse_client.stop()
    # Flush any pending settings changes to disk before exiting
    _settings.flush()
    pygame.quit()


if __name__ == "__main__":
    main()
