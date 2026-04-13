"""
config.py – PFD display configuration for Pi Zero 2W (no SVT version).

Platform-specific display settings.  Shared settings (V-speeds, SSE,
thresholds, presets) are inherited from shared/config_base.py.

The display resolution is set to 640×480 for the current DSI panel.
This will be updated when the final display is selected.
"""
import os
import sys

# ── Import shared config ─────────────────────────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'shared'))
from config_base import *   # noqa: F401,F403

# ── Display ───────────────────────────────────────────────────────────────────
DISPLAY_W      = 640
DISPLAY_H      = 480
FULLSCREEN     = True   # set False for windowed testing
TARGET_FPS     = 30
DISPLAY_ROTATE = 0      # degrees CCW: 0, 90, 180, 270

# ── Layout (matches preview_640x480.py constants) ────────────────────────────
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

# ── Terrain / SVT ─────────────────────────────────────────────────────────────
# SVT rendering is disabled on Pi Zero 2W, but SRTM tiles are still used
# for TAWS proximity alerting (terrain elevation lookups).
_HERE         = os.path.dirname(os.path.abspath(__file__))
SRTM_DIR      = os.path.join(_HERE, "data", "srtm")

# ── Obstacle database (FAA DOF) ───────────────────────────────────────────────
OBSTACLE_DIR  = os.path.join(_HERE, "data", "obstacles")

# ── Try to import local overrides ─────────────────────────────────────────────
try:
    from config_local import *  # noqa: F401,F403
except ImportError:
    pass
