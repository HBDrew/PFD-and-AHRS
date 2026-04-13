"""
config.py – PFD display configuration for Raspberry Pi 4 (full SVT version).

Platform-specific display settings.  Shared settings (V-speeds, SSE,
thresholds, presets) are inherited from shared/config_base.py.

The Pi 4 version targets a higher-resolution display and uses OpenGL
for vector graphics and full Synthetic Vision Terrain rendering.

Display resolution is TBD — will be updated when the final display
hardware is selected.
"""
import os
import sys

# ── Import shared config ─────────────────────────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'shared'))
from config_base import *   # noqa: F401,F403

# ── Display ───────────────────────────────────────────────────────────────────
# Starting with 640×480 — will increase when display hardware is selected.
# The Pi 4 GPU can comfortably drive 1024×768 or 1280×800 with OpenGL.
DISPLAY_W      = 640
DISPLAY_H      = 480
FULLSCREEN     = True   # set False for windowed testing
TARGET_FPS     = 30
DISPLAY_ROTATE = 0      # degrees CCW: 0, 90, 180, 270

# ── Layout (matches preview_640x480.py constants) ────────────────────────────
# These will be recalculated when the display resolution changes.
SPD_X      = 0
SPD_W      = 74
ALT_W      = 82
ALT_X      = DISPLAY_W - ALT_W          # 558
HDG_H      = 44
HDG_Y      = DISPLAY_H - HDG_H          # 436
TAPE_TOP   = 22
TAPE_BOT   = HDG_Y                       # 436
TAPE_H     = TAPE_BOT - TAPE_TOP         # 414
TAPE_MID   = (TAPE_TOP + TAPE_BOT) // 2 # 229
CX         = DISPLAY_W // 2             # 320
CY         = TAPE_MID                   # 229
ROLL_R     = 148
ROLL_CY    = ROLL_R + 16                # 164
BALL_Y     = HDG_Y - 30                 # 406

# AI (Attitude Indicator) region
AI_X = SPD_W                            # 74
AI_W = ALT_X - SPD_W                   # 484
AI_Y = TAPE_TOP                         # 22
AI_H = TAPE_H                           # 414

# ── SVT / Terrain ────────────────────────────────────────────────────────────
# Full SVT rendering enabled on Pi 4.
# SRTM tiles are used for both terrain rendering and TAWS alerting.
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
try:
    from config_local import *  # noqa: F401,F403
except ImportError:
    pass
