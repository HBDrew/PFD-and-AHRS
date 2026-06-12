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
# Tape widths and heading strip height stay at their hand-tuned pixel sizes.
# Extra horizontal pixels go to the wider AI region.  Vertical elements scale
# with display height so the heading strip and top strip remain proportional.
_SY = DISPLAY_H / 480.0   # vertical scale factor

SPD_X      = 0
SPD_W      = 89                          # 74 + 15px for 1024×600 readability
ALT_W      = 97                          # 82 + 15px for 1024×600 readability
ALT_X      = DISPLAY_W - ALT_W
HDG_H      = int(44  * _SY)
HDG_Y      = DISPLAY_H - HDG_H
TAPE_TOP   = int(22  * _SY)
TAPE_BOT   = HDG_Y
TAPE_H     = TAPE_BOT - TAPE_TOP
TAPE_MID   = (TAPE_TOP + TAPE_BOT) // 2
CX         = SPD_W + (DISPLAY_W - SPD_W - ALT_W) // 2  # centre of AI region
CY         = TAPE_MID
_S_MIN     = min(DISPLAY_W / 640.0, _SY)  # shorter axis keeps arc circular
ROLL_R     = int(148 * _S_MIN)
ROLL_CY    = ROLL_R + int(30 * _S_MIN)   # lowered (was 16) so the roll-arc apex
                                         # AND its upper sky-pointer doghouse
                                         # (~15px above the apex) clear the top
                                         # data-ribbon band
BALL_Y     = HDG_Y - int(30 * _SY)

# AI (Attitude Indicator) region
AI_X = SPD_W
AI_W = ALT_X - SPD_W
AI_Y = TAPE_TOP
AI_H = TAPE_H

# Font scale factor — used by pfd.py to scale all font sizes
FONT_SCALE = _S_MIN

# ── SVT / Terrain ────────────────────────────────────────────────────────────
_HERE         = os.path.dirname(os.path.abspath(__file__))
SRTM_DIR      = os.path.join(_HERE, "data", "srtm")
WATER_DIR     = os.path.join(_HERE, "data", "water")

# ── SVT renderer selection ────────────────────────────────────────────────────
# "opengl"        — full 3D synthetic vision via moderngl + standalone EGL
#                    context.  Renders into an offscreen FBO, reads pixels
#                    back, blits onto the pygame display.  Known to disrupt
#                    KMS/DRM on Pi 4 kernel 6.12 + mesa 25.0 V3D; lazy-probed.
# "opengl_shared" — pygame owns the display via pygame.OPENGL | DOUBLEBUF,
#                    moderngl attaches to pygame's GL context (NOT standalone),
#                    terrain renders directly into the default framebuffer
#                    and is composited under a 2D overlay surface uploaded
#                    as a GL texture.  Avoids the standalone-EGL/KMS conflict.
#                    Validated on Pi 4 + ROADOM display — current default.
# "pygame"        — legacy 2D scanline renderer (fallback for testing/debug).
# Auto-falls back to "pygame" at runtime if the chosen GL path fails.
SVT_RENDERER = "opengl_shared"

# ── Obstacle database (FAA DOF) ───────────────────────────────────────────────
OBSTACLE_DIR  = os.path.join(_HERE, "data", "obstacles")

# ── Airport database (OurAirports CSV) ────────────────────────────────────────
AIRPORT_DIR   = os.path.join(_HERE, "data", "airports")

# ── Airspace boundaries (Class B/C/D + MOA + Restricted, US first-cut) ───────
# JSON file at AIRSPACE_DIR/airspaces.json — see shared/airspaces.py
# for the schema.  Falls back to a bundled example when no file is on
# disk; pilot uses the AIRSPACE DATA subscreen's BUILD button to
# convert FAA GeoJSON exports in-place on this Pi.
AIRSPACE_DIR  = os.path.join(_HERE, "data", "airspaces")

# ── User settings persistence ─────────────────────────────────────────────────
# JSON file that stores V-speeds, units, brightness, AHRS trims, airport
# filters and other user-configurable values across reboots.
SETTINGS_PATH = os.path.join(_HERE, "data", "settings.json")

# ── Try to import local overrides ─────────────────────────────────────────────
# config_local.py can override DISPLAY_PROFILE, DISPLAY_W, DISPLAY_H, etc.
try:
    from config_local import *  # noqa: F401,F403
except ImportError:
    pass

# ── MFD font scale (physical size, derived AFTER local overrides) ─────────────
# Both ROADOM panels are 1024×600, but the 10" is physically larger so the
# same pixel font reads bigger.  Scale MFD label text (airport names, strip,
# source status) down on the 10".  config_local.py may set MFD_FONT_SCALE
# explicitly to fine-tune; otherwise it's derived from DISPLAY_PROFILE.
if "MFD_FONT_SCALE" not in dir():
    MFD_FONT_SCALE = 0.55 if DISPLAY_PROFILE == "roadom_10" else 1.0

# The bottom data strip needs only a gentle reduction on the 10" — the map
# airport names had to shrink a lot more, so the strip uses its own scale to
# keep caption/value text legible.  config_local.py may override.
if "MFD_STRIP_FONT_SCALE" not in dir():
    MFD_STRIP_FONT_SCALE = 0.8 if DISPLAY_PROFILE == "roadom_10" else 1.0
