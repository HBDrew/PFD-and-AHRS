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
_FORCE_PYGAME_SVT = True
try:
    from svt_renderer_gl import render_svt_gl, is_available as _gl_available
    _SVT_GL_AVAILABLE = False if _FORCE_PYGAME_SVT else _gl_available()
except Exception as e:
    print(f"[PFD] OpenGL SVT unavailable: {e}")
    _SVT_GL_AVAILABLE = False
    render_svt_gl = None

import obstacles as obs_mod
import airports as apt_mod
import runways as rwy_mod
import settings as _settings

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
}
disp["ds"] = {                      # display settings
    "spd_unit":  "kt",   "alt_unit":   "ft",
    "baro_unit": "inhg", "brightness": 8,  "night_mode": False,
}
disp["ss"] = {                      # AHRS / sensor settings
    "pitch_trim":    0.0, "roll_trim": 0.0,
    "mag_cal":       "idle", "mounting": "normal",
    "hdg_src":       "mag",   # "mag" | "gps"  — heading source (magnetic or GPS track)
    "airspeed_src":  "gps",   # "gps" | "ias"  — speed source (GPS groundspeed or IAS sensor)
}
disp["cs"] = {                      # connectivity settings
    "ahrs_url":  PICO_URL, "wifi_ssid": "AHRS-Link",
    "wifi_pass": "",        "wifi_ok":  False,
    "ahrs_ok":   False,     "test_msg": "", "apply_msg": "",
}
disp["sim"] = {                     # flight simulator state
    "preset_idx": 0,    # index into SIM_PRESETS
    "init_alt":   5000.0,
    "init_hdg":   0.0,
    "init_spd":   90.0,
    "gps_fail":   False,
    "baro_fail":  False,
    "ahrs_fail":  False,
}

SMOOTH_K = 0.25   # IIR coefficient (higher = faster response)

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
    """Background thread: update disp['cs']['wifi_ok'] every 5 s."""
    while True:
        disp["cs"]["wifi_ok"] = bool(_wifi_ssid_current())
        time.sleep(5)


def _apply_wifi(ssid, password):
    """Write wpa_supplicant.conf and call wpa_cli reconfigure.
    Returns (success: bool, message: str).
    Requires root (or a sudoers entry for the wpa_supplicant path).
    """
    if not ssid:
        return False, "SSID required"
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
    try:
        with open("/etc/wpa_supplicant/wpa_supplicant.conf", "w") as f:
            f.write(conf)
        subprocess.run(["wpa_cli", "-i", "wlan0", "reconfigure"],
                       capture_output=True, timeout=5)
        return True, "WiFi config applied — connecting…"
    except PermissionError:
        return False, "Permission denied — run with sudo"
    except FileNotFoundError:
        return False, "wpa_cli not found"
    except Exception as e:
        return False, str(e)[:50]


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
    # Boolean / discrete fields: copy directly
    for k in ("lat", "lon", "track", "fix", "sats",
              "gps_alt", "baro_src", "baro_hpa",
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
    val_int = int(abs(value))
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
            img_prev = f.render(str(d_prev), True, color)
            ty_lo    = bh // 2 - gh // 2 + scroll   # reversed: lo scrolls down
            cell.blit(img_hi,   (tx, ty_lo - slot_h))   # hi  (higher) above
            cell.blit(img_lo,   (tx, ty_lo))
            cell.blit(img_prev, (tx, ty_lo + slot_h))   # prev (lower) below
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
    pitch_py = int(pitch * px_per_deg)

    # Horizon passes through (hcx, hcy) tilted by roll
    hcx = cx
    hcy = cy - pitch_py
    roll_rad = math.radians(roll)
    cos_r, sin_r = math.cos(roll_rad), math.sin(roll_rad)

    # Extend horizon line well beyond the rect so clipping takes care of edges
    R  = aw + ah
    h1 = (hcx - R * cos_r, hcy + R * sin_r)
    h2 = (hcx + R * cos_r, hcy - R * sin_r)

    # Classify each corner: sky side = dot product with "up" normal > 0
    def _sky_side(px, py):
        return (hcy - py) * cos_r + (px - hcx) * sin_r > 0

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
            denom  = -dy * cos_r + dx * sin_r
            if abs(denom) > 1e-6:
                t  = (-(hcy - c[1]) * cos_r - (c[0] - hcx) * sin_r) / denom
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
            pygame.draw.line(surf, (255, 255, 255, 200), p1, p2, 2)
            continue

        col = (255, 255, 255, 220)
        p1  = _rv(-half, rel_y)
        p2  = _rv( half, rel_y)

        if major:
            pygame.draw.line(surf, col, p1, p2, 2)
        else:
            pygame.draw.aaline(surf, col, p1, p2)

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
    # Solid filled polygon band between inner and outer radius for a bold,
    # truly solid arc.  Outer edge traced forward, inner edge traced backward
    # to form a closed polygon that pygame fills completely.
    _ARC_STEPS = 80
    _ARC_THICK = 2  # pixels of arc band thickness
    arc_outer = []
    arc_inner = []
    for i in range(_ARC_STEPS + 1):
        # Sky-pointer design: arc rotates WITH the sky/horizon so the fixed
        # aircraft reference at the top of the screen reads the current bank.
        # In pygame Y-down, right bank (positive roll) rotates the sky CCW
        # visually, which means pygame angles DECREASE (hence -roll).
        ang = (-90 - roll - 60 + i * 120.0 / _ARC_STEPS) * DEG
        cos_a, sin_a = math.cos(ang), math.sin(ang)
        arc_outer.append((int(cx + (ROLL_R + _ARC_THICK) * cos_a),
                          int(cy + (ROLL_R + _ARC_THICK) * sin_a)))
        arc_inner.append((int(cx + ROLL_R * cos_a),
                          int(cy + ROLL_R * sin_a)))
    arc_band = arc_outer + list(reversed(arc_inner))
    pygame.gfxdraw.filled_polygon(surf, arc_band, WHITE)
    pygame.gfxdraw.aapolygon(surf, arc_band, WHITE)

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
    pygame.gfxdraw.filled_polygon(surf, tri0, WHITE)
    pygame.gfxdraw.aapolygon(surf, tri0, WHITE)

    # Fixed lower doghouse — INSIDE arc, tip at arc-8, fixed at 12 o'clock
    roll_ang = -math.pi / 2
    rp_pts = _doghouse_pts(cx, cy, roll_ang, ROLL_R - 8, size=10, inward=False)
    pygame.gfxdraw.filled_polygon(surf, rp_pts, WHITE)
    pygame.gfxdraw.aapolygon(surf, rp_pts, WHITE)


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
    pygame.gfxdraw.filled_polygon(surf, li, AMBER_DARK)
    pygame.gfxdraw.aapolygon(surf, li, AMBER_DARK)
    pygame.gfxdraw.filled_polygon(surf, lo, AMBER)
    pygame.gfxdraw.aapolygon(surf, lo, AMBER)
    pygame.gfxdraw.filled_polygon(surf, ri, AMBER_DARK)
    pygame.gfxdraw.aapolygon(surf, ri, AMBER_DARK)
    pygame.gfxdraw.filled_polygon(surf, ro, AMBER)
    pygame.gfxdraw.aapolygon(surf, ro, AMBER)

    # Engine nacelles — fills
    lu = [(CX - 93, CY), (CX - 99, CY - 6), (CX - 138, CY - 6), (CX - 138, CY)]
    ll = [(CX - 93, CY), (CX - 138, CY),    (CX - 138, CY + 6), (CX - 99, CY + 6)]
    ru = [(CX + 93, CY), (CX + 99, CY - 6), (CX + 138, CY - 6), (CX + 138, CY)]
    rl = [(CX + 93, CY), (CX + 138, CY),    (CX + 138, CY + 6), (CX + 99, CY + 6)]
    pygame.gfxdraw.filled_polygon(surf, lu, AMBER)
    pygame.gfxdraw.aapolygon(surf, lu, AMBER)
    pygame.gfxdraw.filled_polygon(surf, ll, AMBER_DARK)
    pygame.gfxdraw.aapolygon(surf, ll, AMBER_DARK)
    pygame.gfxdraw.filled_polygon(surf, ru, AMBER)
    pygame.gfxdraw.aapolygon(surf, ru, AMBER)
    pygame.gfxdraw.filled_polygon(surf, rl, AMBER_DARK)
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
    _drm_sw = int(26 * _fs)
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
    pygame.gfxdraw.filled_polygon(surf, pts_s, (0, 10, 30))
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
    _cyan_box(surf, gs_str, x=SPD_X, y=2, w=SPD_W, h=22, col=spd_box_col)


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
    _cyan_box(surf, alt_str, x=ALT_X + 1, y=2, w=ALT_W - 1, h=22, col=alt_box_col)

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

    # Altitude readout box — stepped Veeder-Root style.
    # Extra tape width distributed across all sections:
    #   inner 42→47, drum 24→31, pointer 15→18 = total 81→96
    R = ALT_X + ALT_W
    _ptr_w  = 18   # pointer section width
    _drm_w  = 31   # drum (twenties) section width
    _inn_w  = 47   # inner (hundreds thru ten-thousands) section width
    _box_w  = _inn_w + _drm_w + _ptr_w   # 96 total
    _drm_l  = _ptr_w + _drm_w            # 49 — drum left edge from R
    pts_a = _chamfer([(R,              TAPE_MID),
                      (R - _ptr_w,     TAPE_MID - 15), (R - _ptr_w, TAPE_MID - 29),
                      (R - _drm_l,     TAPE_MID - 29), (R - _drm_l, TAPE_MID - 15),
                      (R - _box_w,     TAPE_MID - 15),
                      (R - _box_w,     TAPE_MID + 15),
                      (R - _drm_l,     TAPE_MID + 15), (R - _drm_l, TAPE_MID + 29),
                      (R - _ptr_w,     TAPE_MID + 29), (R - _ptr_w, TAPE_MID + 15)], {2, 3, 4, 5, 6, 7, 8, 9}, r=3)
    pygame.gfxdraw.filled_polygon(surf, pts_a, (0, 10, 30))

    # VSI readout — drawn BEFORE the outline so the 2px white line frames shared edges
    _nx   = ALT_X
    _ny   = TAPE_MID + 15
    _nw   = R - _drm_l - ALT_X
    _nh   = 22
    if abs(vspeed) > 30:
        _varr = "▲" if vspeed > 0 else "▼"
        _vstr = f"{_varr}{abs(vspeed)/1000:.1f}"
        _vcol = (0, 220, 0) if vspeed > 0 else (255, 140, 0)
    else:
        _vstr = "—"
        _vcol = LTGREY
    pygame.draw.rect(surf, (0, 8, 22), (_nx, _ny, _nw, _nh), border_radius=3)
    pygame.draw.rect(surf, (70, 100, 130), (_nx, _ny, _nw, _nh), width=1, border_radius=3)
    _text(surf, _vstr, 13, _vcol, bold=True, cx=_nx + _nw // 2, cy=_ny + _nh // 2)

    # Inner: cascade from drum; carry starts when drum_pos > 4 (last 20 ft before rollover)
    carry_frac = max(0.0, (alt % 100) / 20 - 4.0)
    alt_inner  = float(alt // 100) + carry_frac
    inner_int  = int(alt_inner)
    # Inner digits: right-justified against the drum section.
    _cell = 16                          # px per digit cell
    _inn_right = R - _drm_l            # right edge of inner section
    if inner_int < 10:
        _rolling_drum(surf, _inn_right - _cell, TAPE_MID - 14, _cell, 28, alt_inner, 1, WHITE, 24)
    elif inner_int < 100:
        _rolling_drum(surf, _inn_right - _cell * 2, TAPE_MID - 14, _cell, 28, alt_inner, 1, WHITE, 24,
                      power_offset=1)
        _rolling_drum(surf, _inn_right - _cell, TAPE_MID - 14, _cell, 28, alt_inner, 1, WHITE, 22)
    else:
        _rolling_drum(surf, _inn_right - _cell * 3, TAPE_MID - 14, _cell * 2, 28, alt_inner, 2, WHITE, 22,
                      suppress_leading=True, power_offset=1)
        _rolling_drum(surf, _inn_right - _cell, TAPE_MID - 14, _cell, 28, alt_inner, 1, WHITE, 22)
    # Drum: 20-ft labels
    _drm_x = R - _drm_l + 1
    _drm_render_w = _drm_w - 2
    _rolling_drum_alt20(surf, _drm_x, TAPE_MID - 28, _drm_render_w, 56, alt, WHITE, 18,
                        show_adjacent=True, adj_slot_h=18)
    _drum_shade(surf, _drm_x, TAPE_MID - 28, _drm_render_w, 56)
    # Border drawn LAST so drum shade doesn't cover the inner pixels
    pygame.draw.polygon(surf, WHITE, pts_a, width=2)
    pygame.gfxdraw.aapolygon(surf, pts_a, WHITE)


# ── Heading tape ──────────────────────────────────────────────────────────────
_CARDINALS = {0: "N", 45: "NE", 90: "E", 135: "SE",
              180: "S", 225: "SW", 270: "W", 315: "NW"}


def draw_heading_tape(surf, hdg, hdg_bug=None, track=None, gps_ok=False, hdg_src="mag"):
    """Bottom heading strip with bug and current-heading box.

    hdg_src="gps" means hdg is already the GPS track value; the magenta track
    pointer is suppressed (it would just sit at centre) and the readout box
    shows a small "TRK" sub-label instead of "MAG".
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

    # Heading bug — color reflects source: magenta=GPS track, cyan=magnetic.
    # hdg_bug=0 means "not set" (same convention as alt_bug and spd_bug).
    if hdg_bug is not None and hdg_bug != 0:
        off = ((hdg_bug - hdg + 180) % 360) - 180
        hbx = int(CX + off * PX_PER_DEG)
        hbx = max(SPD_W, min(ALT_X, hbx))   # clamp to inner edges of tap buttons
        bug = [(hbx - 17, HDG_Y + 14), (hbx - 17, HDG_Y),
               (hbx - 5,  HDG_Y), (hbx, HDG_Y + 7), (hbx + 5, HDG_Y),
               (hbx + 17, HDG_Y), (hbx + 17, HDG_Y + 14)]
        hdg_bug_col = MAGENTA if hdg_src == "gps" else CYAN
        pygame.gfxdraw.filled_polygon(surf, bug, hdg_bug_col)
        pygame.gfxdraw.aapolygon(surf, bug, hdg_bug_col)

    # GPS track pointer (magenta, when GPS OK and heading source is MAG)
    # Suppressed in GPS TRK mode — hdg is already the track value.
    # Also suppressed when track ≈ hdg (within 1°) to avoid clutter at centre.
    if gps_ok and track is not None and hdg_src != "gps":
        off = ((track - hdg + 180) % 360) - 180
        if abs(off) > 1.0:  # only show when there's visible wind/crab angle
            tx = int(CX + off * PX_PER_DEG)
            if 0 < tx < DISPLAY_W:
                pygame.draw.polygon(surf, (220, 60, 220),
                    [(tx, HDG_Y + 4), (tx - 5, HDG_Y + 14), (tx + 5, HDG_Y + 14)])

    # Heading box — scaled for font size. GPS TRK → magenta, MAG → white.
    hdg_col = MAGENTA if hdg_src == "gps" else WHITE
    # Measure actual rendered width of "133°" to size the box
    _hf = _get_font(17)
    _hdg_str = f"{round(hdg) % 360:03d}\u00b0"
    _hw = _hf.size(_hdg_str)[0]
    bw = max(66, _hw + 28)  # text width + padding for M subscript + margins
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
    pygame.gfxdraw.filled_polygon(surf, pts_h, (0, 0, 0))
    pygame.draw.polygon(surf, hdg_col, pts_h, width=2)
    pygame.gfxdraw.aapolygon(surf, pts_h, hdg_col)
    # Three-digit readout — centred in the box
    _text(surf, _hdg_str, 17, hdg_col, cx=CX, cy=by2 + bh // 2)
    # G/M subscript — outboard of the ° glyph
    deg_right = CX + _hw // 2 + 3
    src_lbl  = "G" if hdg_src == "gps" else "M"
    _text(surf, src_lbl, 8, hdg_col, x=deg_right, y=by2 + bh - 12)


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
                                      window_ft=OBSTACLE_CAUTION_FT)
        for ob in nearby:
            clearance = alt_ft - ob.msl_ft
            if clearance < OBSTACLE_WARNING_FT:
                level = max(level, 2)
                break
            elif clearance < OBSTACLE_CAUTION_FT:
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

    # Two-word label: primary left, secondary right
    _text(surf, lbl, 11, fg, bold=True, x=bx + 6, y=by + 2)
    _text(surf, sub, 9,  fg, bold=False, x=bx + bw - 52, y=by + 4)


# ── Status badges ─────────────────────────────────────────────────────────────
def draw_status_badges(surf, ahrs_ok, gps_ok, baro_ok, baro_src, sats, connected,
                       hdg_src="mag"):
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
    if hdg_src == "gps" and gps_ok:
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
        # Seed bugs so the aircraft holds its initial state
        disp["hdg_bug"] = sim["init_hdg"]
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
            tgt_hdg = disp.get("hdg_bug", state["yaw"])  or state["yaw"]
            tgt_alt = disp.get("alt_bug", state["alt"])  or state["alt"]
            tgt_spd = disp.get("spd_bug") or 90.0

            # ── Heading / bank ─────────────────────────────────────────────────
            hdg     = state["yaw"]
            hdg_err = ((tgt_hdg - hdg + 180) % 360) - 180
            turn_rate = 3.0  # standard rate deg/s
            d_hdg = max(-turn_rate * dt, min(turn_rate * dt, hdg_err * 0.4))
            state["yaw"]  = (hdg + d_hdg) % 360
            bank          = max(-25.0, min(25.0, hdg_err * 1.8))
            state["roll"] = bank if not ahrs_fail else 0.0
            state["ay"]   = -bank / 600.0  # slip ball

            # ── Altitude / VS / pitch ──────────────────────────────────────────
            alt     = state["alt"]
            alt_err = tgt_alt - alt
            if abs(alt_err) < 5.0:
                state["alt"] = tgt_alt
                vs_fpm = 0.0
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
            nm_s           = state["speed"] / 3600.0
            hdg_rad        = math.radians(state["yaw"])
            nm_per_deg_lat = 60.0
            nm_per_deg_lon = max(1.0, 60.0 * math.cos(math.radians(state["lat"])))
            state["lat"]  += nm_s * dt * math.cos(hdg_rad) / nm_per_deg_lat
            state["lon"]  += nm_s * dt * math.sin(hdg_rad) / nm_per_deg_lon
            state["track"] = state["yaw"]

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


# ── Sim controls overlay ─────────────────────────────────────────────────────

_SIMCTRL_W = 280
_SIMCTRL_H = 200
_SIMCTRL_X = (DISPLAY_W - _SIMCTRL_W) // 2
_SIMCTRL_Y = (DISPLAY_H - _SIMCTRL_H) // 2 - 10

_SIMCTRL_ROW_Y0  = _SIMCTRL_Y + 36   # first sensor row top
_SIMCTRL_ROW_H   = 34
_SIMCTRL_ROW_GAP = 4
_SIMCTRL_BW      = 70     # ON / FAIL button width


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

        # Row label
        _text(surf, label, 12, (160, 175, 200), bold=True,
              x=_SIMCTRL_X + 14, cy=row_y + _SIMCTRL_ROW_H // 2)

        # ON button
        on_active = not failed
        on_bg = (0, 50, 20) if on_active else (0, 8, 16)
        on_oc = (40, 190, 60) if on_active else (35, 60, 45)
        on_tc = (60, 220, 80) if on_active else (60, 100, 75)
        ox = _SIMCTRL_X + _SIMCTRL_W - 2 * _SIMCTRL_BW - 8 - 6
        pygame.draw.rect(surf, on_bg, (ox, row_y, _SIMCTRL_BW, _SIMCTRL_ROW_H), border_radius=4)
        pygame.draw.rect(surf, on_oc, (ox, row_y, _SIMCTRL_BW, _SIMCTRL_ROW_H), width=2, border_radius=4)
        _text(surf, "ON", 12, on_tc, bold=on_active,
              cx=ox + _SIMCTRL_BW // 2, cy=row_y + _SIMCTRL_ROW_H // 2)

        # FAIL button
        fx = ox + _SIMCTRL_BW + 6
        fail_active = failed
        fail_bg = (50, 5, 5) if fail_active else (12, 0, 0)
        fail_oc = (200, 40, 40) if fail_active else (75, 30, 30)
        fail_tc = RED if fail_active else (110, 55, 55)
        pygame.draw.rect(surf, fail_bg, (fx, row_y, _SIMCTRL_BW, _SIMCTRL_ROW_H), border_radius=4)
        pygame.draw.rect(surf, fail_oc, (fx, row_y, _SIMCTRL_BW, _SIMCTRL_ROW_H), width=2, border_radius=4)
        _text(surf, "FAIL", 11, fail_tc, bold=fail_active,
              cx=fx + _SIMCTRL_BW // 2, cy=row_y + _SIMCTRL_ROW_H // 2)

    # EXIT SIM button
    exit_y = _SIMCTRL_ROW_Y0 + len(sensors) * (_SIMCTRL_ROW_H + _SIMCTRL_ROW_GAP) + 6
    _action_btn(surf,
                _SIMCTRL_X + 14, exit_y,
                _SIMCTRL_W - 28, _SIMCTRL_H - (exit_y - _SIMCTRL_Y) - 10,
                "EXIT SIM", "danger")


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

    # EXIT SIM button area
    exit_y = _SIMCTRL_ROW_Y0 + len(sensors) * (_SIMCTRL_ROW_H + _SIMCTRL_ROW_GAP) + 6
    exit_h = _SIMCTRL_H - (exit_y - _SIMCTRL_Y) - 10
    if (exit_y <= y <= exit_y + exit_h and
            _SIMCTRL_X + 14 <= x <= _SIMCTRL_X + _SIMCTRL_W - 14):
        return "exit_sim"

    return "noop"   # tapped inside panel but not on a control — consume event


# ── Touch handler ─────────────────────────────────────────────────────────────
_touch_t0      = {}
_bug_dragging  = None    # "hdg" | "alt"
_active_fingers = {}     # finger_id → touch-down time (ms)
_multitouch_t0  = None   # time when 2nd finger touched down


def _open_numpad(target):
    """Switch to numpad mode for the given bug target."""
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
            if disp["mode"] != "pfd":
                disp["mode"] = "pfd"   # ESC exits any overlay
            else:
                return False
        if event.key == pygame.K_d:
            return "toggle_demo"
        if disp["mode"] == "pfd":
            if event.key == pygame.K_UP:
                disp["alt_bug"] = round(disp["alt_bug"] / 100) * 100 + 100
            if event.key == pygame.K_DOWN:
                disp["alt_bug"] = round(disp["alt_bug"] / 100) * 100 - 100
            if event.key == pygame.K_LEFT:
                disp["hdg_bug"] = (round(disp["hdg_bug"]) - 10) % 360
            if event.key == pygame.K_RIGHT:
                disp["hdg_bug"] = (round(disp["hdg_bug"]) + 10) % 360
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
            elif idx == 3: disp["mode"] = "connectivity_setup"
            elif idx == 4: disp["mode"] = "system_setup"
            return True

        # ── Display settings taps ─────────────────────────────────────────
        if mode == "display_setup":
            action = display_setup_hit(x, y, disp["ds"])
            if action == "back":
                disp["mode"] = "setup"
            elif action and action.startswith("set:"):
                _, key, val_str = action.split(":", 2)
                disp["ds"][key] = (val_str == "True") if key == "night_mode" else val_str
                _settings.mark_dirty()
            elif action and action.startswith("inc:brightness:"):
                delta = int(action.split(":")[-1])
                disp["ds"]["brightness"] = max(1, min(10, disp["ds"]["brightness"] + delta))
                _set_backlight(disp["ds"]["brightness"])
                _settings.mark_dirty()
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
            elif action == "mag_cal":
                disp["ss"]["mag_cal"] = "running"
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
                disp["kbd_buf"]    = ""
                disp["kbd_prev"]   = "connectivity_setup"
                disp["mode"]       = "keyboard"
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
                disp["mode"] = "pfd"
            elif action == "cancel":
                disp["mode"] = "system_setup"
            return True

        # ── Sim controls overlay taps ─────────────────────────────────────
        if mode == "sim_controls":
            action = sim_controls_hit(x, y)
            if action == "exit_sim":
                _sim_state = None
                disp["mode"] = "pfd"
            elif action and action.startswith("sensor_on:"):
                sensor = action.split(":")[1]
                disp["sim"][sensor + "_fail"] = False
            elif action and action.startswith("sensor_fail:"):
                sensor = action.split(":")[1]
                disp["sim"][sensor + "_fail"] = True
            # "noop" or None: consume the event either way
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
            return True

        # ── Terrain data screen taps ──────────────────────────────────────
        if mode == "terrain_data":
            action = terrain_data_hit(x, y, disp["td"])
            if action == "back":
                disp["mode"] = "system_setup"
            elif action == "cancel":
                disp["td"]["dl_cancel"] = True
            elif action == "current_area":
                if not disp["td"]["downloading"]:
                    _td_start_current_area()
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
                    disp["kbd_buf"]    = ""
                    disp["mode"]       = "keyboard"
                else:
                    disp["numpad_target"] = key
                    disp["numpad_buf"]    = ""
                    disp["numpad_prev"]   = "flight_profile"
                    disp["mode"]          = "numpad"
            return True

        # ── Keyboard taps ─────────────────────────────────────────────────
        if mode == "keyboard":
            hit = keyboard_hit(x, y)
            if hit:
                lbl, sty = hit
                target  = disp["kbd_target"]
                max_len = next((f[3] for f in _FP_FIELDS if f[0]==target), 16)
                if sty == 'n':                # character / space
                    ch = ' ' if lbl == 'SPACE' else lbl
                    if len(disp["kbd_buf"]) < max_len:
                        disp["kbd_buf"] += ch
                elif sty == 'del':            # backspace
                    disp["kbd_buf"] = disp["kbd_buf"][:-1]
                elif sty == 'x':              # CANCEL
                    disp["kbd_buf"] = ""
                    disp["mode"] = disp["kbd_prev"]
                elif sty == 'ok':             # DONE
                    buf = disp["kbd_buf"].strip()
                    if buf:
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
                elif sty == 'x':              # CANCEL
                    disp["mode"] = disp["numpad_prev"]
                    disp["numpad_buf"] = ""
                elif sty == 'ok':             # ENTER
                    buf = disp["numpad_buf"]
                    if buf:
                        val = int(buf)
                        if target == "alt_bug":
                            disp["alt_bug"] = float(val * 100)   # input is hundreds of ft
                        elif target == "hdg_bug":
                            disp["hdg_bug"] = float(val % 360)
                        elif target == "spd_bug":
                            disp["spd_bug"] = float(val)
                        elif target == "baro_hpa":
                            baro_unit = disp["ds"].get("baro_unit", "inhg")
                            if baro_unit == "hpa":
                                disp["baro_hpa"] = float(val)
                            else:   # inHg: 4 digits → insert decimal after 2
                                disp["baro_hpa"] = round(val / 100.0 * 33.8639, 2)
                        elif target == "sim_init_alt":
                            disp["sim"]["init_alt"] = float(val * 100)
                        elif target == "sim_init_hdg":
                            disp["sim"]["init_hdg"] = float(val % 360)
                        elif target == "sim_init_spd":
                            disp["sim"]["init_spd"] = float(val)
                        elif target in disp["fp"]:   # V-speed field
                            disp["fp"][target] = val
                        _settings.mark_dirty()
                    disp["mode"] = disp["numpad_prev"]
                    disp["numpad_buf"] = ""
            return True

        # ── PFD taps ──────────────────────────────────────────────────────
        # Tap on SIM watermark → open sim controls overlay
        if _sim_state is not None and mode == "pfd":
            if CX - 30 <= x <= CX + 30 and CY - 30 <= y <= CY - 10:
                disp["mode"] = "sim_controls"
                return True

        # Tap on alt bug button → open numpad
        if ALT_X <= x <= DISPLAY_W and 2 <= y <= 24:
            _open_numpad("alt_bug")
            return True
        # Tap on GS bug button → open numpad
        if SPD_X <= x <= SPD_X + SPD_W and 2 <= y <= 24:
            _open_numpad("spd_bug")
            return True
        # Tap on hdg bug button → open numpad
        if SPD_X <= x <= SPD_X + SPD_W and HDG_Y + 2 <= y <= HDG_Y + 24:
            _open_numpad("hdg_bug")
            return True
        # Tap on baro button → open numpad
        if ALT_X <= x <= DISPLAY_W and HDG_Y + 2 <= y <= HDG_Y + 24:
            _open_numpad("baro_hpa")
            return True
        # Tap on alt tape → adjust alt bug by position
        if ALT_X <= x <= DISPLAY_W and TAPE_TOP <= y <= TAPE_BOT:
            ft = round(disp["alt"] + (TAPE_MID - y) / PX_PER_FT)
            disp["alt_bug"] = round(ft / 100) * 100
        # Tap on heading tape → adjust hdg bug by position
        if HDG_Y <= y <= DISPLAY_H:
            off = (x - CX) / PX_PER_DEG
            disp["hdg_bug"] = round(disp["yaw"] + off) % 360

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
    [('CANCEL','x'), ('0','n'), ('ENTER','ok')],
]
_NP_PW=120; _NP_PH=64; _NP_GX=12; _NP_GY=10
_NP_TW = 3*_NP_PW + 2*_NP_GX   # 384
_NP_X0 = (DISPLAY_W - _NP_TW) // 2  # 128
_NP_Y0 = 118


def _numpad_key(surf, col, row, label, style, r=8):
    bx = _NP_X0 + col*(_NP_PW+_NP_GX)
    by = _NP_Y0 + row*(_NP_PH+_NP_GY)
    if style == 'x':
        bg=(28,6,6);  oc=(200,55,55); tc=(220,80,80)
    elif style == 'ok':
        bg=(5,25,10); oc=(50,200,80); tc=(60,220,90)
    else:
        bg=(0,12,32); oc=WHITE;       tc=WHITE
    pygame.draw.rect(surf, bg, (bx, by, _NP_PW, _NP_PH), border_radius=r)
    glow_h = _NP_PH // 5
    for i in range(glow_h):
        t = 1.0 - i / glow_h
        gc = ((int(45+t*55), int(8+t*12),  int(8+t*12))  if style=='x'  else
              (int(5+t*15),  int(40+t*60), int(10+t*20)) if style=='ok' else
              (int(15+t*30), int(20+t*45), int(40+t*75)))
        pygame.draw.line(surf, gc, (bx+r, by+1+i), (bx+_NP_PW-r, by+1+i))
    pygame.draw.rect(surf, oc, (bx, by, _NP_PW, _NP_PH), width=2, border_radius=r)
    _text(surf, label, 20, tc, bold=True, cx=bx+_NP_PW//2, cy=by+_NP_PH//2)


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
        for ci, (lbl, sty) in enumerate(row):
            _numpad_key(surf, ci, ri, lbl, sty)


def numpad_hit(x, y):
    """Return (label, style) of the tapped numpad key, or None."""
    for ri, row in enumerate(_NP_KEYS):
        for ci, (lbl, sty) in enumerate(row):
            bx = _NP_X0 + ci*(_NP_PW+_NP_GX)
            by = _NP_Y0 + ri*(_NP_PH+_NP_GY)
            if bx <= x <= bx+_NP_PW and by <= y <= by+_NP_PH:
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

_KB_ROWS = [
    [('1',60,'n'),('2',60,'n'),('3',60,'n'),('4',60,'n'),('5',60,'n'),
     ('6',60,'n'),('7',60,'n'),('8',60,'n'),('9',60,'n'),('0',60,'n')],
    [('Q',60,'n'),('W',60,'n'),('E',60,'n'),('R',60,'n'),('T',60,'n'),
     ('Y',60,'n'),('U',60,'n'),('I',60,'n'),('O',60,'n'),('P',60,'n')],
    [('A',60,'n'),('S',60,'n'),('D',60,'n'),('F',60,'n'),('G',60,'n'),
     ('H',60,'n'),('J',60,'n'),('K',60,'n'),('L',60,'n')],
    [('Z',60,'n'),('X',60,'n'),('C',60,'n'),('V',60,'n'),('B',60,'n'),
     ('N',60,'n'),('M',60,'n'),('-',60,'n'),('\u232b',88,'del')],
    [('CANCEL',108,'x'),('SPACE',292,'n'),('DONE',108,'ok')],
]
_KB_ROW_H=66; _KB_GAP_Y=6; _KB_GAP_X=4; _KB_Y0=112


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


def draw_keyboard(surf, title, current_val, entered="", transparent=False):
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
    _text(surf,f"Current: {current_val}",10,(110,120,140),cx=DISPLAY_W//2,cy=104)
    y = _KB_Y0
    for row in _KB_ROWS:
        x = _kb_row_x0(row)
        for label,kw,style in row:
            _kb_key(surf,x,y,kw,_KB_ROW_H,label,style)
            x += kw+_KB_GAP_X
        y += _KB_ROW_H+_KB_GAP_Y


def keyboard_hit(x, y):
    """Return (label, style) of the tapped key, or None."""
    ky = _KB_Y0
    for row in _KB_ROWS:
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
    ("night_mode", "NIGHT MODE",   "Dim red cockpit lighting",
     [False, True],      ["OFF","ON"],        100),
]


def _dsp_rx(row, bx, bw):
    """Left x of control group (right-aligned, 14 px margin)."""
    *_, opts_v, opts_l, bw_each = row
    if opts_v is None:
        total = _DSP_SW + _DSP_BTN_G + _DSP_VW + _DSP_BTN_G + _DSP_SW
    else:
        total = len(opts_v)*bw_each + (len(opts_v)-1)*_DSP_BTN_G
    return bx + bw - total - 14


def draw_display_setup(surf, ds):
    _screen_header(surf, "DISPLAY")
    for ri, row in enumerate(_DSP_ROWS):
        key, label, sub, opts_v, opts_l, bw_each = row
        is_night = (key == "night_mode")
        bx, by, bw, bh = _setting_row(surf, ri, label, sub)
        if is_night:
            # Overlay dim veil to show greyed-out state
            veil = pygame.Surface((bw, bh), pygame.SRCALPHA)
            veil.fill((0, 0, 0, 160))
            surf.blit(veil, (bx, by))
            _text(surf, "future", 10, (90,90,100), x=bx+bw-60, y=by+bh-18)
            continue
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


def display_setup_hit(x, y, ds):
    """Return action string or None."""
    if _back_hit(x, y):
        return "back"
    for ri, row in enumerate(_DSP_ROWS):
        key, *_, opts_v, opts_l, bw_each = row
        if key == "night_mode":
            continue   # greyed out — no interaction
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


def draw_ahrs_setup(surf, ss):
    _screen_header(surf, "AHRS / SENSORS")

    # Row 0: Pitch trim
    bx, by, bw, bh = _setting_row(surf, 0, "PITCH TRIM", "Horizon offset correction")
    _trim_stepper(surf, bx, by, bw, bh, ss.get("pitch_trim", 0.0), "pitch_trim")

    # Row 1: Roll trim
    bx, by, bw, bh = _setting_row(surf, 1, "ROLL TRIM", "Wing-level correction")
    _trim_stepper(surf, bx, by, bw, bh, ss.get("roll_trim", 0.0), "roll_trim")

    # Row 2: Magnetometer calibration (greyed out — not yet implemented)
    bx, by, bw, bh = _setting_row(surf, 2, "MAGNETOMETER", "Compass calibration")
    cal = ss.get("mag_cal", "idle")
    state_lbl, state_col = _SS_MAG_LABELS.get(cal, ("?", WHITE))
    _text(surf, state_lbl, 13, state_col, bold=True, x=bx+220, y=by+(bh-18)//2)
    # Draw disabled button (dim, no interaction)
    cbx = bx+bw-138-14; cby = by+(bh-36)//2
    pygame.draw.rect(surf, (18,18,20), (cbx, cby, 138, 36), border_radius=6)
    pygame.draw.rect(surf, (55,55,65), (cbx, cby, 138, 36), width=2, border_radius=6)
    _text(surf, "CALIBRATE", 15, (75,75,88), bold=False, cx=cbx+69, cy=cby+18)
    _text(surf, "future", 9, (60,60,72), cx=cbx+69, cy=cby+29)

    # Row 3: Mounting orientation
    bx, by, bw, bh = _setting_row(surf, 3, "MOUNTING", "Board orientation")
    cur = ss.get("mounting", "normal")
    opts = [("normal","NORMAL"),("inverted","INVERTED")]
    total = 2*120 + _DSP_BTN_G
    rx = bx + bw - total - 14
    ry = by + (bh - _DSP_BTN_H) // 2
    for i, (v, lbl) in enumerate(opts):
        _seg_btn(surf, rx+i*(120+_DSP_BTN_G), ry, 120, _DSP_BTN_H, lbl, v==cur)

    # Row 4: Heading source (MAG compass vs GPS track)
    bx, by, bw, bh = _setting_row(surf, 4, "HEADING SOURCE",
                                   "Primary heading reference")
    cur_src = ss.get("hdg_src", "mag")
    opts_src = [("mag", "MAG"), ("gps", "GPS TRK")]
    total_src = 2*120 + _DSP_BTN_G
    rx = bx + bw - total_src - 14
    ry = by + (bh - _DSP_BTN_H) // 2
    for i, (v, lbl) in enumerate(opts_src):
        _seg_btn(surf, rx+i*(120+_DSP_BTN_G), ry, 120, _DSP_BTN_H, lbl, v==cur_src)

    # Row 5: Airspeed source (GPS groundspeed vs dedicated IAS sensor)
    bx, by, bw, bh = _setting_row(surf, 5, "AIRSPEED SOURCE",
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
    for ri in range(5):
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
                return f"trim:{key}:-0.5"
            plus_x = rx_trim + _SS_TRIM_SW + _SS_TRIM_G + _SS_TRIM_VW + _SS_TRIM_G
            if plus_x <= x <= plus_x+_SS_TRIM_SW:
                return f"trim:{key}:+0.5"
        elif ri == 2:
            pass  # CALIBRATE is greyed out (future feature)
        elif ri == 3:
            total_m = 2*120 + _DSP_BTN_G
            rx = bx + bw - total_m - 14
            ry = by + (_SS_RH - _DSP_BTN_H) // 2
            for i, v in enumerate(("normal","inverted")):
                if rx+i*(120+_DSP_BTN_G) <= x <= rx+i*(120+_DSP_BTN_G)+120:
                    if ry <= y <= ry+_DSP_BTN_H:
                        return f"set:mounting:{v}"
        elif ri == 4:
            total_src = 2*120 + _DSP_BTN_G
            rx = bx + bw - total_src - 14
            ry = by + (_SS_RH - _DSP_BTN_H) // 2
            for i, v in enumerate(("mag", "gps")):
                if rx+i*(120+_DSP_BTN_G) <= x <= rx+i*(120+_DSP_BTN_G)+120:
                    if ry <= y <= ry+_DSP_BTN_H:
                        return f"set:hdg_src:{v}"
        elif ri == 5:
            total_as = 2*120 + _DSP_BTN_G
            rx = bx + bw - total_as - 14
            ry = by + (_SS_RH - _DSP_BTN_H) // 2
            # Only GPS GS (index 0) is active; IAS SENSOR (index 1) is future/disabled
            if rx <= x <= rx+120:
                if ry <= y <= ry+_DSP_BTN_H:
                    return "set:airspeed_src:gps"
    return None


# ── Connectivity screen ───────────────────────────────────────────────────────

_CS_FIELDS = [
    ("ahrs_url",  "AHRS URL",        "Pico W access-point address"),
    ("wifi_ssid", "WiFi SSID",       "Network name to join"),
    ("wifi_pass", "WiFi PASSWORD",   "WPA2 passphrase"),
]
_CS_BTN_Y  = _ss_row_y(len(_CS_FIELDS) + 1) + 4   # below fields + status row
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
        cy  = by + bh//4 + i*bh//2
        pygame.draw.circle(surf, col, (bx2+238, cy), 6)
        _text(surf, lbl, 13, col, bold=True, x=bx2+252, y=cy-9)

    # Status messages from last apply / test
    for msg, col, y_off in [
            (cs.get("apply_msg",""), (100,180,80), _CS_BTN_Y - 20),
            (cs.get("test_msg",""),  (100,160,220), _CS_BTN_Y - 8)]:
        if msg:
            _text(surf, msg, 10, col, cx=DISPLAY_W//2, y=y_off)

    # Action buttons
    half = (bw - 10) // 2
    _action_btn(surf, bx,          _CS_BTN_Y, half, _CS_BTN_H, "APPLY WIFI", "warn")
    _action_btn(surf, bx+half+10,  _CS_BTN_Y, half, _CS_BTN_H, "TEST AHRS",  "ok")


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
    half = (bw - 10) // 2
    if _CS_BTN_Y <= y <= _CS_BTN_Y+_CS_BTN_H:
        if bx <= x <= bx+half:
            return "apply_wifi"
        if bx+half+10 <= x <= bx+half+10+half:
            return "test_ahrs"
    return None


# ── System screen ─────────────────────────────────────────────────────────────

_SYS_VERSION = "0.1.0"
_SYS_BUILD   = "2026-04-10"
_SYS_INFO_Y  = 56
_SYS_INFO_LH = 28


_SYS_N_LINES = 5
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
    lines = [
        ("Firmware version",  _SYS_VERSION),
        ("Build date",        _SYS_BUILD),
        ("Display",           f"{DISPLAY_W}\u00d7{DISPLAY_H}  HDMI"),
        ("Hardware",          "Pi 4 + Pico W  (OpenGL SVT)"),
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
    # Row 2: runway + centerline overlays (2 wide tiles)
    row2_y = filt_y + filt_h + 10
    big_w = (bw - 10) // 2
    _seg_btn(surf, bx,                row2_y, big_w, filt_h,
             "RUNWAYS", ad.get("show_runways", False), r=6)
    _seg_btn(surf, bx + big_w + 10,   row2_y, big_w, filt_h,
             "EXT CENTERLINES", ad.get("show_centerlines", False), r=6)


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
    # Row 2: runway + centerline toggles (2 wide tiles)
    row2_y = filt_y + filt_h + 10
    big_w = (bw - 10) // 2
    if row2_y <= y <= row2_y + filt_h:
        if bx <= x <= bx + big_w:
            return "toggle:show_runways"
        if bx + big_w + 10 <= x <= bx + big_w + 10 + big_w:
            return "toggle:show_centerlines"
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

    # ── Current Area button (full width) ─────────────────────────────────────
    cur_col = (50,50,70) if downloading else (0,18,45)
    cur_oc  = (70,70,95)  if downloading else WHITE
    pygame.draw.rect(surf, cur_col, (bx, _TD_MY, bw, bh), border_radius=6)
    gh = bh // 5
    for i in range(gh):
        t = 1.0 - i/gh
        gc = (int(15+t*25), int(20+t*40), int(40+t*65)) if not downloading else (int(20+t*20),int(20+t*20),int(30+t*30))
        pygame.draw.line(surf, gc, (bx+6, _TD_MY+1+i), (bx+bw-6, _TD_MY+1+i))
    pygame.draw.rect(surf, cur_oc, (bx, _TD_MY, bw, bh), width=2, border_radius=6)
    _text(surf, "DOWNLOAD CURRENT AREA", 15, cur_oc, bold=True,
          cx=DISPLAY_W//2, cy=_TD_MY+bh//2-8)
    lat_i = int(disp.get("lat", DEMO_LAT)); lon_i = int(disp.get("lon", DEMO_LON))
    area_str = f"25 tiles around {lat_i}\u00b0{'N' if lat_i>=0 else 'S'}  {abs(lon_i)}\u00b0{'W' if lon_i<0 else 'E'}  \u2248 35 MB"
    _text(surf, area_str, 10, (120,140,165), cx=DISPLAY_W//2, cy=_TD_MY+bh//2+10)

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
    if downloading:
        prog_y = DISPLAY_H - 58
        cur = td.get("dl_current", 0); total = max(1, td.get("dl_total", 1))
        frac = cur / total
        pygame.draw.rect(surf, (0,12,32), (bx, prog_y, bw, 50), border_radius=6)
        pygame.draw.rect(surf, (55,75,105), (bx, prog_y, bw, 50), width=1, border_radius=6)
        bar_w = int((bw - 20) * frac)
        pygame.draw.rect(surf, (0,25,12), (bx+10, prog_y+28, bw-20, 12), border_radius=3)
        if bar_w > 0:
            pygame.draw.rect(surf, (40,180,60), (bx+10, prog_y+28, bar_w, 12), border_radius=3)
        _text(surf, td.get("dl_status",""), 10, (140,160,180),
              cx=DISPLAY_W//2, cy=prog_y+14)
        pct = f"{int(frac*100)}%  ({cur}/{total})"
        _text(surf, pct, 10, (60,220,80), cx=DISPLAY_W//2, cy=prog_y+43)
        # CANCEL button
        _action_btn(surf, DISPLAY_W-100-bx, prog_y+6, 92, 36, "CANCEL", "danger", r=5)
    else:
        # Show last status message if any
        last = td.get("dl_status", "")
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

    # Cancel button during download
    if td.get("downloading"):
        prog_y = DISPLAY_H - 58
        if (DISPLAY_W-100-bx <= x <= DISPLAY_W-bx and
                prog_y+6 <= y <= prog_y+42):
            return "cancel"

    # Current Area button
    if bx <= x <= bx+bw and _TD_MY <= y <= _TD_MY+bh:
        return "current_area"

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
                     hdg_src="mag", baro_ok=True):
    """
    Tap buttons in the heading strip — left and right only so the centre
    heading readout remains unobstructed:
      • Left  (under speed tape) : HDG bug  — MAGENTA=GPS TRK, CYAN=MAG
      • Right (under alt tape)   : Baro setting — CYAN=baro sensor, MAGENTA=GPS ALT
    IAS and ALT bug buttons are drawn at the tops of their own tapes.
    """
    y = HDG_Y + 2

    # HDG bug — left side of heading strip; color matches heading bug triangle
    _hdg_btn = f"{round(hdg_bug) % 360:03d}\u00b0" if hdg_bug is not None else "---\u00b0"
    hdg_box_col = MAGENTA if hdg_src == "gps" else CYAN
    _cyan_box(surf, _hdg_btn, x=SPD_X, y=y, w=SPD_W, h=22, col=hdg_box_col)

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
              x=ALT_X + 1, y=y, w=ALT_W - 1, h=22, font_sz=baro_fsz, col=baro_col)


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
_OBS_WINDOW_FT  = OBSTACLE_WINDOW_FT
_OBS_CAUTION_FT = OBSTACLE_CAUTION_FT
_OBS_WARNING_FT = OBSTACLE_WARNING_FT

def draw_obstacle_symbols(surf, ai_rect, lat, lon, alt_ft,
                          hdg_deg, pitch_deg, roll_deg):
    """
    Project nearby obstacles onto the AI viewport as red/amber tower symbols.

    Each obstacle is placed at its bearing/distance from the aircraft.
    We compute a synthetic azimuth and elevation angle, then rotate it
    by roll and translate by pitch — the same coordinate system as the
    pitch ladder — to get pixel position.
    """
    nearby = obs_mod.query_nearby(_obstacles, lat, lon,
                                  radius_nm=_OBS_RADIUS_NM,
                                  alt_ft=alt_ft,
                                  window_ft=_OBS_WINDOW_FT)
    if not nearby:
        return

    ax, ay_r, aw, ah = ai_rect
    cx = ax + aw // 2
    cy = ay_r + ah // 2

    # Pixels per degree (same scale as pitch ladder: 8px/deg at default)
    PX_PER_DEG = 8.0

    nm_per_deg_lat = 60.0
    nm_per_deg_lon = 60.0 * math.cos(math.radians(lat))

    for ob in nearby:
        # Bearing from aircraft to obstacle (degrees true)
        dlat_nm = (ob.lat - lat) * nm_per_deg_lat
        dlon_nm = (ob.lon - lon) * nm_per_deg_lon
        dist_nm = math.hypot(dlat_nm, dlon_nm)
        if dist_nm < 0.01:
            continue
        bearing = math.degrees(math.atan2(dlon_nm, dlat_nm)) % 360.0

        # Relative bearing (from nose)
        rel_brg = (bearing - hdg_deg + 180) % 360 - 180   # −180…+180

        # Project BOTH the base (at ground = msl_ft - agl_ft) and the top
        # (at msl_ft).  This anchors the tower to the terrain rather than
        # leaving it as a fixed-height floating symbol.
        dist_ft     = dist_nm * 6076.0
        top_diff_ft = ob.msl_ft - alt_ft
        base_diff_ft = (ob.msl_ft - ob.agl_ft) - alt_ft    # ground at obstacle
        top_vert_deg  = math.degrees(math.atan2(top_diff_ft,  dist_ft))
        base_vert_deg = math.degrees(math.atan2(base_diff_ft, dist_ft))

        cos_r = math.cos(math.radians(roll_deg))
        sin_r = math.sin(math.radians(roll_deg))

        def _project(vert_deg):
            sxr = rel_brg * PX_PER_DEG
            syr = -(vert_deg + pitch_deg) * PX_PER_DEG
            return (int(cx + sxr * cos_r - syr * sin_r),
                    int(cy + sxr * sin_r + syr * cos_r))

        bx, by = _project(base_vert_deg)   # tower base (on the ground)
        sx, sy = _project(top_vert_deg)    # tower top (at MSL height)

        # Clip based on the top anchor (most relevant for visibility)
        if not (ax + 4 <= sx <= ax + aw - 4 and ay_r + 4 <= sy <= ay_r + ah - 4):
            continue

        # Colour by clearance — red/yellow/white (standard aviation convention)
        clearance = alt_ft - ob.msl_ft
        if clearance < _OBS_WARNING_FT:
            col = RED
        elif clearance < _OBS_CAUTION_FT:
            col = YELLOW
        else:
            col = WHITE

        # Draw tower as a caret/chevron shape: apex at the top (MSL height),
        # base anchored to the ground.  Tapers from base to apex so tall
        # towers look like obelisks rather than needles.
        # Minimum 6 px height so short antennas stay visible at long range.
        tower_h = max(6, by - sy)
        apex = (sx, sy)
        base_half = max(3, tower_h // 3)   # base width ~2/3 of height
        left_base  = (sx - base_half, sy + tower_h)
        right_base = (sx + base_half, sy + tower_h)
        pygame.draw.line(surf, col, left_base,  apex, 2)
        pygame.draw.line(surf, col, right_base, apex, 2)

        # Lit tower: 4-point asterisk/star at the apex
        if ob.lit:
            r = 4
            star_col = (255, 230, 100)     # bright yellow star
            pygame.draw.line(surf, star_col, (sx - r, sy),     (sx + r, sy),     2)
            pygame.draw.line(surf, star_col, (sx,     sy - r), (sx,     sy + r), 2)
            pygame.draw.line(surf, star_col, (sx - r, sy - r), (sx + r, sy + r), 1)
            pygame.draw.line(surf, star_col, (sx - r, sy + r), (sx + r, sy - r), 1)

        # Height label for tall/close obstacles (above the apex)
        if ob.agl_ft >= 500 or dist_nm < 3.0:
            lbl = f"{int(ob.msl_ft//100)*100}"
            _text(surf, lbl, 8, col, cx=sx, cy=sy - 14)


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

    nearby = apt_mod.query_nearby(_airports, lat, lon,
                                  radius_nm=AIRPORT_RADIUS_NM)
    if not nearby:
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

    # Maximum relative bearing that will actually render within the AI rect.
    # Half-width in px / px_per_deg gives the angular extent of the AI.
    max_rel_brg = (aw // 2) / PX_PER_DEG

    APT_PUBLIC  = (120, 220, 255)   # cyan — public paved/unpaved
    APT_HELI    = (220, 120, 220)   # magenta — heliport
    APT_WATER   = (150, 200, 255)   # lighter blue — seaplane base
    APT_OTHER   = (180, 180, 200)   # grey — other

    # Render farthest first so nearer ones are drawn on top (Z-order ish)
    for apt in reversed(nearby):
        if not show.get(apt.atype, False):
            continue
        dlat_nm = (apt.lat - lat) * nm_per_deg_lat
        dlon_nm = (apt.lon - lon) * nm_per_deg_lon
        dist_nm = math.hypot(dlat_nm, dlon_nm)
        if dist_nm < 0.05:
            continue
        bearing = math.degrees(math.atan2(dlon_nm, dlat_nm)) % 360.0
        rel_brg = (bearing - hdg_deg + 180) % 360 - 180
        # Cull airports outside the AI's angular field of view
        if abs(rel_brg) > max_rel_brg:
            continue

        dist_ft = dist_nm * 6076.0
        alt_diff_ft = apt.elev_ft - alt_ft          # negative = below aircraft
        vert_deg = math.degrees(math.atan2(alt_diff_ft, dist_ft))

        screen_x_raw = rel_brg * PX_PER_DEG
        screen_y_raw = -(vert_deg + pitch_deg) * PX_PER_DEG
        # Cull airports that project above the horizon — ground features
        # floating in blue sky are visually confusing and never useful.
        if screen_y_raw < 0:
            continue
        sx = cx + int(screen_x_raw * cos_r - screen_y_raw * sin_r)
        sy = cy + int(screen_x_raw * sin_r + screen_y_raw * cos_r)

        if not (ax + 8 <= sx <= ax + aw - 8 and ay_r + 8 <= sy <= ay_r + ah - 8):
            continue

        # Symbol by type
        if apt.atype == "H":
            col = APT_HELI
            _text(surf, "H", 12, col, bold=True, cx=sx, cy=sy)
        elif apt.atype == "W":
            col = APT_WATER
            pygame.draw.circle(surf, col, (sx, sy), 4, 1)
            pygame.draw.line(surf, col, (sx - 4, sy + 5), (sx + 4, sy + 5), 1)
        elif apt.atype == "B":
            col = APT_OTHER
            pts = [(sx, sy - 4), (sx - 4, sy + 3), (sx + 4, sy + 3)]
            pygame.draw.polygon(surf, col, pts, 1)
        else:  # S / M / L = public airport
            col = APT_PUBLIC
            # Filled outer ring, dark centre — runway-ring style
            pygame.draw.circle(surf, col, (sx, sy), 5, 0)
            pygame.draw.circle(surf, (0, 10, 30), (sx, sy), 3, 0)
            # Medium/large airports get a larger ring
            if apt.atype in ("M", "L"):
                pygame.draw.circle(surf, col, (sx, sy), 7, 1)

        # Ident label as a small "road sign" on a post above the symbol.
        # Post anchors at the airport, sign sits ~25 px above so the label
        # rises clear of nearby terrain features.
        if dist_nm <= AIRPORT_LABEL_NM:
            lbl = apt.ident
            font_sz = 9
            f = _get_font(font_sz, bold=True)
            tw, th = f.size(lbl)
            sign_w = tw + 8
            sign_h = th + 4
            post_h = 22
            sign_x = sx - sign_w // 2
            sign_y = sy - post_h - sign_h
            # Clamp sign to stay on screen
            if sign_y < ay_r + 2:
                sign_y = ay_r + 2
                post_h = max(4, sy - sign_y - sign_h)
            # Post: thin vertical line from symbol up to bottom of sign
            pygame.draw.line(surf, col, (sx, sy - 6), (sx, sign_y + sign_h), 1)
            # Sign: dark fill with coloured border
            pygame.draw.rect(surf, (0, 10, 26),
                             (sign_x, sign_y, sign_w, sign_h), border_radius=2)
            pygame.draw.rect(surf, col,
                             (sign_x, sign_y, sign_w, sign_h), width=1, border_radius=2)
            _text(surf, lbl, font_sz, col, bold=True,
                  cx=sx, cy=sign_y + sign_h // 2)


# ── Runway polygons + extended centerlines ───────────────────────────────────

_RUNWAY_MAX_RANGE_NM       = 8.0    # only draw runways within this range
_CENTERLINE_RANGE_NM       = 15.0   # draw extended centerlines within this range
_CENTERLINE_EXTEND_NM      = 10.0   # centerline extends this far from threshold
_CENTERLINE_DASH_NM        = 0.5    # dash length (nm)


def _project_latlon(lat_deg, lon_deg, ref_lat, ref_lon, ref_alt_ft,
                    elev_ft, hdg_deg, pitch_deg, roll_deg,
                    cx, cy, px_per_deg, max_fov_deg=None):
    """Project a lat/lon/elevation point onto the AI screen.
    Uses the same flat-earth atan2 math as obstacle/airport symbols so
    everything stays aligned.  Returns (sx, sy), or ``None`` when the point
    is more than ``max_fov_deg`` off the nose — used by the extended-
    centerline renderer so far-field dashes behind the aircraft (where the
    flat-earth projection wraps and would streak across the AI) are culled."""
    nm_per_deg_lat = 60.0
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
    dist_ft = dist_nm * 6076.0
    alt_diff = elev_ft - ref_alt_ft
    vert_deg = math.degrees(math.atan2(alt_diff, dist_ft))
    cos_r = math.cos(math.radians(roll_deg))
    sin_r = math.sin(math.radians(roll_deg))
    sxr = rel_brg * px_per_deg
    syr = -(vert_deg + pitch_deg) * px_per_deg
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

    nm_per_deg_lat = 60.0
    nm_per_deg_lon = 60.0 * math.cos(math.radians(lat))

    def _proj(la, lo, elev):
        return _project_latlon(la, lo, lat, lon, alt_ft,
                               elev, hdg_deg, pitch_deg, roll_deg,
                               cx, cy, px_per_deg)

    def _in_ai(sx, sy):
        return ax <= sx <= ax + aw and ay_r <= sy <= ay_r + ah

    for r in nearby:
        # Distance to runway centre (for range culling)
        d_nm = math.hypot((r.centre_lat - lat) * nm_per_deg_lat,
                          (r.centre_lon - lon) * nm_per_deg_lon)

        # ── Runway polygon ────────────────────────────────────────────────
        if show_rwy and d_nm <= _RUNWAY_MAX_RANGE_NM:
            # Perpendicular offset in lat/lon to span the runway width.
            # width_ft → degrees of lat (flat-earth ok for <1nm)
            half_w_deg_lat = (r.width_ft / 2.0) / 6076.0 / 60.0
            half_w_deg_lon = half_w_deg_lat / max(1e-6, math.cos(math.radians(lat)))
            # Runway axis in lat/lon (he - le)
            ax_lat = r.he_lat - r.le_lat
            ax_lon = r.he_lon - r.le_lon
            # Perpendicular = rotate axis by 90°.  In (lat, lon) pairs,
            # rotating (dlat, dlon) by 90° → (-dlon * cos_lat_ratio, dlat / cos_lat_ratio).
            # We need a unit perpendicular at runway width, so:
            axis_len_nm = math.hypot(ax_lat * nm_per_deg_lat,
                                     ax_lon * nm_per_deg_lon)
            if axis_len_nm < 0.01:
                continue
            # Unit vector along axis in nm: (ax_lat_nm, ax_lon_nm) / len
            # Perpendicular (in nm): (+ax_lon_nm, -ax_lat_nm) / len  → rotate 90° CW
            perp_lat_nm =  (ax_lon * nm_per_deg_lon) / axis_len_nm
            perp_lon_nm = -(ax_lat * nm_per_deg_lat) / axis_len_nm
            half_w_nm = r.width_ft / 6076.0
            perp_lat = (perp_lat_nm * half_w_nm) / nm_per_deg_lat
            perp_lon = (perp_lon_nm * half_w_nm) / nm_per_deg_lon

            p1 = _proj(r.le_lat + perp_lat, r.le_lon + perp_lon, r.le_elev_ft)
            p2 = _proj(r.he_lat + perp_lat, r.he_lon + perp_lon, r.he_elev_ft)
            p3 = _proj(r.he_lat - perp_lat, r.he_lon - perp_lon, r.he_elev_ft)
            p4 = _proj(r.le_lat - perp_lat, r.le_lon - perp_lon, r.le_elev_ft)
            # Draw polygon if at least some corner is in the AI
            if _in_ai(*p1) or _in_ai(*p2) or _in_ai(*p3) or _in_ai(*p4):
                old_clip = surf.get_clip()
                surf.set_clip(pygame.Rect(ax, ay_r, aw, ah))
                pygame.gfxdraw.filled_polygon(surf, [p1, p2, p3, p4], ASPHALT)
                pygame.gfxdraw.aapolygon(surf, [p1, p2, p3, p4], STRIPE)
                # Centreline stripe: from LE midpoint to HE midpoint
                mid_le = _proj(r.le_lat, r.le_lon, r.le_elev_ft)
                mid_he = _proj(r.he_lat, r.he_lon, r.he_elev_ft)
                pygame.draw.aaline(surf, STRIPE, mid_le, mid_he)
                surf.set_clip(old_clip)

        # ── Extended centerlines from each threshold ──────────────────────
        # Only show centerlines if within a somewhat larger range — they're
        # a navigation aid for approach planning.
        if show_cline and d_nm <= _CENTERLINE_RANGE_NM:
            _draw_extended_centerline(
                surf, ai_rect, r, lat, lon, alt_ft,
                hdg_deg, pitch_deg, roll_deg, cx, cy, px_per_deg,
                CLINE, nm_per_deg_lat, nm_per_deg_lon,
            )


def _draw_extended_centerline(surf, ai_rect, r, lat, lon, alt_ft,
                              hdg_deg, pitch_deg, roll_deg,
                              cx, cy, px_per_deg, col,
                              nm_per_deg_lat, nm_per_deg_lon):
    """Dashed line extending OUTWARD from each threshold along the reciprocal
    of the runway axis, out to _CENTERLINE_EXTEND_NM."""
    ax, ay_r, aw, ah = ai_rect

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
    n_steps = int(_CENTERLINE_EXTEND_NM / (dash_nm + gap_nm))

    old_clip = surf.get_clip()
    surf.set_clip(pygame.Rect(ax, ay_r, aw, ah))

    # Angular cutoff: skip segments whose endpoint is more than 60° off the
    # nose — past that the flat-earth bearing math wraps and dashes behind
    # the aircraft would streak horizontally across the AI.  60° is well
    # beyond the ~40° half-FOV implied by px_per_deg so on-screen dashes
    # are never clipped.
    _FOV = 60.0

    # For each threshold, extend OUTWARD (opposite of the axis toward the
    # other end).  For LE: step in -u direction.  For HE: step in +u.
    for thresh_lat, thresh_lon, thresh_elev, sign in (
        (r.le_lat, r.le_lon, r.le_elev_ft, -1),
        (r.he_lat, r.he_lon, r.he_elev_ft, +1),
    ):
        for i in range(n_steps):
            start = (dash_nm + gap_nm) * i
            end   = start + dash_nm
            s_lat = thresh_lat + sign * u_dlat * start
            s_lon = thresh_lon + sign * u_dlon * start
            e_lat = thresh_lat + sign * u_dlat * end
            e_lon = thresh_lon + sign * u_dlon * end
            ps = _project_latlon(s_lat, s_lon, lat, lon, alt_ft,
                                 thresh_elev, hdg_deg, pitch_deg, roll_deg,
                                 cx, cy, px_per_deg, max_fov_deg=_FOV)
            if ps is None:
                continue
            pe = _project_latlon(e_lat, e_lon, lat, lon, alt_ft,
                                 thresh_elev, hdg_deg, pitch_deg, roll_deg,
                                 cx, cy, px_per_deg, max_fov_deg=_FOV)
            if pe is None:
                continue
            pygame.draw.aaline(surf, col, ps, pe)

    surf.set_clip(old_clip)


# ── Main render function ──────────────────────────────────────────────────────
def render(surf, demo_mode, connected, data_stale=False):
    mode = disp.get("mode", "pfd")

    # ── Full-screen replacement screens (no PFD behind them) ─────────────────
    if mode == "setup":
        draw_setup_screen(surf); return
    if mode == "flight_profile":
        draw_flight_profile(surf, disp["fp"]); return
    if mode == "display_setup":
        draw_display_setup(surf, disp["ds"]); return
    if mode == "ahrs_setup":
        draw_ahrs_setup(surf, disp["ss"]); return
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
    alt_bug  = disp["alt_bug"]

    # ── AHRS trim + mounting correction ──────────────────────────────────────
    ss = disp["ss"]
    pitch_trim = ss.get("pitch_trim", 0.0)
    roll_trim  = ss.get("roll_trim",  0.0)
    if ss.get("mounting") == "inverted":
        pitch = -pitch + pitch_trim
        roll  = -roll  + roll_trim
    else:
        pitch = pitch + pitch_trim
        roll  = roll  + roll_trim

    # ── Stale-data timeout: no link for > STALE_TIMEOUT_S → treat as AHRS fail
    if data_stale:
        ahrs_ok = False

    # ── Heading source selection ──────────────────────────────────────────────
    # MAG  (default): use magnetometer/gyro yaw from AHRS (already fused)
    # GPS TRK:        complementary filter — gyro propagates each frame,
    #                 GPS track slowly slaves the absolute reference.
    #                 Falls back to yaw if GPS fix is lost.
    hdg_src = ss.get("hdg_src", "mag")
    if hdg_src == "gps":
        hdg = _update_gps_heading(disp["yaw"], disp["track"], gps_ok)
    else:
        global _gps_hdg, _prev_yaw_disp  # reset filter when switching back to MAG
        _gps_hdg = _prev_yaw_disp = None
        hdg = disp["yaw"]

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

    # 0. Compute terrain/obstacle alert level for this frame
    _update_terrain_alert(lat, lon, alt, speed, gps_ok)

    # 1. AI background — draw full-width so tapes are transparent over sky/ground
    _full_ai = (0, 0, DISPLAY_W, HDG_Y)
    if _has_terrain:
        draw_ai_background(surf, _full_ai, pitch, roll, hdg, alt, lat, lon)
    else:
        draw_simple_ai_background(surf, _full_ai, pitch, roll)

    # 1b. Runway polygons + extended centerlines (drawn BEFORE airport
    # symbols so the airport ring sits on top of the runway at the airport
    # centre).
    if _runways is not None and gps_ok:
        draw_runway_symbols(surf, ai_rect, lat, lon, alt, hdg, pitch, roll)

    # 1c. Airport symbols projected onto AI
    if _airports is not None and gps_ok:
        draw_airport_symbols(surf, ai_rect, lat, lon, alt, hdg, pitch, roll)

    # 1d. Obstacle symbols projected onto AI
    if _obstacles is not None and gps_ok:
        draw_obstacle_symbols(surf, ai_rect, lat, lon, alt, hdg, pitch, roll)

    # 1c. Zero-pitch reference line — always horizontal across AI at
    # screen-centre, regardless of actual horizon position.  Critical with
    # 3D SVT because high terrain shifts the visible horizon away from 0°.
    if SVT_RENDERER == "opengl" and _SVT_GL_AVAILABLE:
        draw_zero_pitch_line(surf, ai_rect, pitch, roll)

    # 2. Pitch ladder (with roll rotation)
    draw_pitch_ladder(surf, ai_rect, pitch, roll)

    # 3. Speed tape (display unit, fp V-speeds)
    draw_speed_tape(surf, speed_d, gs_bug=gs_bug_d,
                    vs0=vs0_d, vs1=vs1_d, vfe=vfe_d, vno=vno_d, vne=vne_d,
                    airspeed_src=airspeed_src)

    # 4. Alt tape (display unit)
    draw_alt_tape(surf, alt_d, vspeed, baro_hpa, baro_src, alt_bug_d,
                  baro_ok=baro_ok)

    # 5. Heading tape
    draw_heading_tape(surf, hdg, hdg_bug, track, gps_ok, hdg_src=hdg_src)

    # 6. Roll arc
    draw_roll_arc(surf, roll)

    # 7. Aircraft symbol
    draw_aircraft_symbol(surf)

    # 8. Slip ball
    draw_slip_ball(surf, ay)

    # 9. Status badges
    draw_status_badges(surf, ahrs_ok, gps_ok, baro_ok, baro_src, sats, connected,
                       hdg_src=hdg_src)

    # 9b. Terrain / obstacle proximity alert banner (centre of badge strip)
    draw_terrain_alert(surf)

    # 10. Failure overlays
    draw_failure_overlays(surf, ahrs_ok, gps_ok, baro_ok, sats)

    # 11. Tap-buttons for heading bug, baro, and alt bug (color = data source)
    draw_tap_buttons(surf, hdg, hdg_bug, baro_hpa, baro_src, alt_bug,
                     hdg_src=hdg_src, baro_ok=baro_ok)

    # 12. Demo / SIM watermark
    if demo_mode:
        _text(surf, "DEMO", 14, (255, 60, 60), cx=CX, cy=CY - 20)
    elif _sim_state is not None:
        _text(surf, "SIM", 14, (255, 100, 60), cx=CX, cy=CY - 20)

    # ── Overlay modes: veil + UI drawn on top of live PFD ────────────────────
    if mode == "sim_controls":
        draw_sim_controls(surf)

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
        spd_bug_title = "SET IAS BUG" if airspeed_src == "ias" else "SET GS BUG"
        titles  = {"alt_bug":   "SET ALTITUDE BUG  (\u00d7100 ft)",
                   "hdg_bug":   "SET HEADING BUG",
                   "spd_bug":   spd_bug_title,
                   "baro_hpa":  baro_title,
                   "sim_init_alt": "SET INITIAL ALTITUDE  (\u00d7100 ft)",
                   "sim_init_hdg": "SET INITIAL HEADING",
                   "sim_init_spd": "SET INITIAL SPEED (kt)"}
        curvals = {"alt_bug":   int(disp.get("alt_bug", 0)) // 100,
                   "hdg_bug":   int(disp.get("hdg_bug", 0)),
                   "spd_bug":   int(disp.get("spd_bug", 0)),
                   "baro_hpa":  baro_cur,
                   "sim_init_alt": int(disp["sim"]["init_alt"]) // 100,
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
        if prev == "connectivity_setup":
            cur   = disp["cs"].get(target, "")
            title = {"ahrs_url": "AHRS URL", "wifi_ssid": "WiFi SSID",
                     "wifi_pass": "WiFi PASSWORD"}.get(target, "ENTER TEXT")
        else:
            cur   = disp["fp"].get(target, "")
            title = next((f[1] for f in _FP_FIELDS if f[0]==target), "ENTER TEXT")
        draw_keyboard(surf, f"ENTER {title}", cur, buf, transparent=True)


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

    pygame.init()
    pygame.mouse.set_visible(False)

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
        """
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
        _seed(roll=0, pitch=2, hdg=133, alt=8500, speed=115, hdg_src="gps")
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
        disp["ss"]["hdg_src"] = "gps"
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
        _sse_client = SSEClient(SSE_URL, state, _state_lock)
        _sse_client.start()
        print(f"[PFD] Connecting to {SSE_URL}")
        threading.Thread(target=_poll_wifi_status, daemon=True,
                         name="WiFiPoll").start()
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
