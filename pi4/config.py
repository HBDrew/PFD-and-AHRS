"""
config.py – PFD display configuration for Raspberry Pi 4 (full SVT version).

Platform-specific display settings.  Shared settings (V-speeds, SSE,
thresholds, presets) are inherited from shared/config_base.py.

The Pi 4 version supports multiple display sizes.  Layout constants are
computed dynamically from DISPLAY_W / DISPLAY_H so that the PFD scales
correctly to any resolution.

Supported displays:
  - Waveshare 3.5" DPI LCD   640×480   (DPI 40-pin GPIO, I2C touch)
  - ROADOM 7" HDMI            1024×600  (HDMI + USB capacitive touch)
  - ROADOM 10" HDMI           1024×600  (HDMI + USB capacitive touch)

Set DISPLAY_PROFILE below, or override DISPLAY_W / DISPLAY_H directly.
"""
import os
import sys

# ── Import shared config ─────────────────────────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'shared'))
from config_base import *   # noqa: F401,F403

# ── Display profile ──────────────────────────────────────────────────────────
# Select one:  "waveshare_35"  |  "roadom_7"  |  "roadom_10"  |  "custom"
DISPLAY_PROFILE = "roadom_7"

_PROFILES = {
    "waveshare_35": {
        "w": 640,  "h": 480,
        "interface": "dpi",         # DPI parallel RGB via 40-pin GPIO
        "touch": "i2c",             # I2C capacitive (5-point)
        "backlight": "pwm_gpio18",  # PWM on GPIO 18
        "overlay": "waveshare-35dpi-3b-4b",
        "notes": "Waveshare 3.5inch DPI LCD, IPS, 60 Hz",
    },
    "roadom_7": {
        "w": 1024, "h": 600,
        "interface": "hdmi",        # HDMI video
        "touch": "usb_hid",         # USB capacitive touch
        "backlight": "none",        # No software backlight control
        "overlay": None,
        "notes": "ROADOM LE070-01 7\" IPS, HDMI, 60 Hz, 178° viewing",
    },
    "roadom_10": {
        "w": 1024, "h": 600,
        "interface": "hdmi",
        "touch": "usb_hid",
        "backlight": "none",
        "overlay": None,
        "notes": "ROADOM 10\" IPS, HDMI, 60 Hz, 178° viewing",
    },
    "custom": {
        "w": 640,  "h": 480,       # override in config_local.py
        "interface": "hdmi",
        "touch": "usb_hid",
        "backlight": "none",
        "overlay": None,
        "notes": "Custom display — set w/h in config_local.py",
    },
}

_profile    = _PROFILES.get(DISPLAY_PROFILE, _PROFILES["roadom_7"])
DISPLAY_W   = _profile["w"]
DISPLAY_H   = _profile["h"]
DISPLAY_IF  = _profile["interface"]   # "dpi" | "hdmi"
TOUCH_IF    = _profile["touch"]       # "i2c" | "usb_hid"
BL_MODE     = _profile["backlight"]   # "pwm_gpio18" | "sysfs" | "none"
DT_OVERLAY  = _profile["overlay"]     # DT overlay name or None

FULLSCREEN     = True   # set False for windowed testing
TARGET_FPS     = 30
DISPLAY_ROTATE = 0      # degrees CCW: 0, 90, 180, 270

# ── Dynamic layout ───────────────────────────────────────────────────────────
# All layout constants are computed as proportions of the display resolution.
# Reference design: 640×480.  Constants scale linearly from there.
_SX = DISPLAY_W / 640.0   # horizontal scale factor
_SY = DISPLAY_H / 480.0   # vertical scale factor

SPD_X      = 0
SPD_W      = int(74  * _SX)
ALT_W      = int(82  * _SX)
ALT_X      = DISPLAY_W - ALT_W
HDG_H      = int(44  * _SY)
HDG_Y      = DISPLAY_H - HDG_H
TAPE_TOP   = int(22  * _SY)
TAPE_BOT   = HDG_Y
TAPE_H     = TAPE_BOT - TAPE_TOP
TAPE_MID   = (TAPE_TOP + TAPE_BOT) // 2
CX         = DISPLAY_W // 2
CY         = TAPE_MID
_S_MIN     = min(_SX, _SY)             # use shorter axis to keep arc circular
ROLL_R     = int(148 * _S_MIN)
ROLL_CY    = ROLL_R + int(16 * _S_MIN)
BALL_Y     = HDG_Y - int(30 * _SY)

# AI (Attitude Indicator) region
AI_X = SPD_W
AI_W = ALT_X - SPD_W
AI_Y = TAPE_TOP
AI_H = TAPE_H

# Font scale factor — used by pfd.py to scale all font sizes
FONT_SCALE = min(_SX, _SY)

# ── SVT / Terrain ────────────────────────────────────────────────────────────
_HERE         = os.path.dirname(os.path.abspath(__file__))
SRTM_DIR      = os.path.join(_HERE, "data", "srtm")

# ── OpenGL rendering options (Phase 2) ────────────────────────────────────────
# SVT_RENDERER    = "pygame"    # "pygame" (current) | "opengl" (planned)
# SVT_MESH_RADIUS = 5.0         # nm — terrain mesh radius around aircraft
# SVT_MESH_RES    = 90           # metres — terrain mesh grid resolution
# SVT_MSAA        = 4            # anti-aliasing sample count

# ── Obstacle database (FAA DOF) ───────────────────────────────────────────────
OBSTACLE_DIR  = os.path.join(_HERE, "data", "obstacles")

# ── Try to import local overrides ─────────────────────────────────────────────
# config_local.py can override DISPLAY_PROFILE, DISPLAY_W, DISPLAY_H, etc.
try:
    from config_local import *  # noqa: F401,F403
except ImportError:
    pass
