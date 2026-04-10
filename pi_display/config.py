"""
config.py – PFD display configuration for Pi Zero 2W + 640×480 DSI.
Edit V-speeds for your specific aircraft.
"""
import os

# ── Server ────────────────────────────────────────────────────────────────────
PICO_URL   = "http://192.168.4.1"        # Pico W AP address
SSE_URL    = PICO_URL + "/events"
SSE_TIMEOUT = 10                         # seconds before reconnect

# ── Display ───────────────────────────────────────────────────────────────────
DISPLAY_W  = 640
DISPLAY_H  = 480
FULLSCREEN = True                        # set False for windowed testing
TARGET_FPS = 30

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

# ── V-speeds (knots) – Cessna 172S defaults ───────────────────────────────────
# Override in a local config_local.py or via setup screen
VS0  =  48   # Stall speed, flaps down
VS1  =  55   # Stall speed, clean
VFE  =  85   # Max flap extension
VNO  = 129   # Max structural cruising
VNE  = 163   # Never exceed
VA   = 105   # Maneuvering (at gross)
VY   =  74   # Best rate of climb
VX   =  62   # Best angle of climb
# Glide  79

# ── Barometric defaults ───────────────────────────────────────────────────────
BARO_DEFAULT_HPA = 1013.25

# ── Terrain / SVT ─────────────────────────────────────────────────────────────
_HERE         = os.path.dirname(os.path.abspath(__file__))
SRTM_DIR      = os.path.join(_HERE, "data", "srtm")
TERRAIN_CAUTION_FT = 500    # terrain within 500 ft → yellow
TERRAIN_WARNING_FT = 100    # terrain within 100 ft → red

# ── Obstacle database (FAA DOF) ───────────────────────────────────────────────
OBSTACLE_DIR        = os.path.join(_HERE, "data", "obstacles")
OBSTACLE_RADIUS_NM  = 10.0    # AI symbol render radius
OBSTACLE_WINDOW_FT  = 2000.0  # only show obstacles within ±2000 ft of alt
OBSTACLE_CAUTION_FT  = 500    # amber below this clearance
OBSTACLE_WARNING_FT  = 100    # red below this clearance
OBSTACLE_EXPIRY_DAYS = 28     # FAA DOF update cycle (days)

# ── Proximity alert lookahead ─────────────────────────────────────────────────
# Alert radius is computed dynamically: radius_nm = speed_kt * ALERT_TIME_S / 3600
# giving a constant time-to-obstacle regardless of airspeed.
# Result is clamped to [ALERT_RADIUS_MIN_NM, ALERT_RADIUS_MAX_NM].
#
# At  60 kt → 1.0 nm  (60 s × 60 kt / 3600)
# At  90 kt → 1.5 nm
# At 120 kt → 2.0 nm
# At 180 kt → 3.0 nm  (capped)
ALERT_TIME_S         = 60     # lookahead window in seconds
ALERT_RADIUS_MIN_NM  = 1.0    # floor — always check at least this far ahead
ALERT_RADIUS_MAX_NM  = 3.0    # ceiling — never exceed this radius

# ── Demo mode ─────────────────────────────────────────────────────────────────
DEMO_LAT = 34.8697
DEMO_LON = -111.7610
DEMO_ALT = 8500.0   # ft MSL starting altitude
DEMO_HDG = 133.0    # degrees

# ── Touch / interaction ───────────────────────────────────────────────────────
LONG_PRESS_MS = 800  # ms for long-press to enter setup

# ── Try to import local overrides ─────────────────────────────────────────────
try:
    from config_local import *  # noqa: F401,F403
except ImportError:
    pass
