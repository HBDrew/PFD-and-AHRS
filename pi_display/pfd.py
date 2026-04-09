#!/usr/bin/env python3
"""
pfd.py – GI-275 inspired PFD for Pi Zero 2W + 640x480 DSI display.

Run:  python3 pfd.py           (connects to Pico W at 192.168.4.1)
      python3 pfd.py --demo    (Sedona demo, no hardware needed)
      python3 pfd.py --sim     (windowed 640x480 for desktop testing)
"""

import math
import sys
import time
import threading
import argparse
import os

os.environ.setdefault("SDL_FBDEV", "/dev/fb0")
os.environ.setdefault("SDL_VIDEODRIVER", "fbcon")   # overridden by --sim

import pygame
import pygame.gfxdraw

from config import *   # noqa: F403
from sse_client import SSEClient
from terrain import render_svt, get_elevation_ft

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
disp["hdg_bug"]    = 0.0
disp["alt_bug"]    = 0.0
disp["baro_hpa"]   = BARO_DEFAULT_HPA
disp["show_demo"]  = False

SMOOTH_K = 0.25   # IIR coefficient (higher = faster response)


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
    key = (size, bold)
    if key not in _fonts:
        paths = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
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


def lerp(a, b, t):
    return a + (b - a) * max(0.0, min(1.0, t))


def lerp_col(a, b, t):
    return tuple(int(lerp(a[i], b[i], t)) for i in range(3))


# ── SVT background ────────────────────────────────────────────────────────────
_svt_cache: dict = {}
_svt_frame = 0
SVT_UPDATE_FRAMES = 3   # update terrain every N frames (~10 Hz at 30 fps)


def get_svt_surface(ai_w, ai_h, pitch, roll, hdg, alt, lat, lon):
    global _svt_frame
    _svt_frame += 1
    key = "svt"
    if key not in _svt_cache or _svt_frame % SVT_UPDATE_FRAMES == 0:
        surf = render_svt(
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
    Fallback: simple sky/ground gradient (no SRTM data).
    Used in demo mode when no terrain tiles are loaded.
    """
    ax, ay, aw, ah = ai_rect

    # Work on an oversized canvas to allow rotation
    pad = int(max(aw, ah) * 0.8)
    cw, ch = aw + pad * 2, ah + pad * 2
    canvas = pygame.Surface((cw, ch))

    px_per_deg = ch / 40.0
    hy = ch // 2 + int(pitch * px_per_deg)

    # Sky
    for row in range(min(ch, hy + 1)):
        t = max(0.0, min(1.0, 1.0 - (hy - row) / max(1, hy)))
        col = lerp_col(SKY_TOP, SKY_HOR, t)
        pygame.draw.line(canvas, col, (0, row), (cw, row))

    # Ground
    for row in range(max(0, hy), ch):
        t = max(0.0, min(1.0, (row - hy) / max(1, ch - hy)))
        col = lerp_col(GND_HOR, GND_BOT, t)
        pygame.draw.line(canvas, col, (0, row), (cw, row))

    # White horizon line
    if 0 < hy < ch:
        pygame.draw.line(canvas, WHITE, (0, hy), (cw, hy), 2)

    # Rotate for roll
    rotated = pygame.transform.rotate(canvas, roll)
    rw, rh = rotated.get_size()
    ox, oy = (rw - aw) // 2, (rh - ah) // 2
    crop = rotated.subsurface(pygame.Rect(ox, oy, aw, ah))
    surf.blit(crop, (ax, ay))


# ── Pitch ladder ──────────────────────────────────────────────────────────────
def draw_pitch_ladder(surf, ai_rect, pitch, roll):
    """White pitch ladder lines, rotated with roll, overlaid on AI."""
    ax, ay, aw, ah = ai_rect
    cx, cy = ax + aw // 2, ay + ah // 2

    focal      = 260.0
    px_per_deg = focal   # pixels per degree at center

    # Render on a transparent canvas, then rotate
    pad = int(max(aw, ah) * 0.75)
    cw, ch = aw + pad * 2, ah + pad * 2
    canvas = pygame.Surface((cw, ch), pygame.SRCALPHA)
    ccx, ccy = cw // 2, ch // 2

    pitch_px = int(pitch * px_per_deg / 10.0)   # 10 px / degree approx

    for deg in range(-30, 35, 5):
        if deg == 0:
            continue
        row_y = ccy - pitch_px - int(deg * px_per_deg / 10.0)
        if not (10 < row_y < ch - 10):
            continue
        major = (deg % 10 == 0)
        half  = int(cw * (0.17 if major else 0.09))
        lw    = 2 if major else 1
        col   = (255, 255, 255, 220)
        pygame.draw.line(canvas, col, (ccx - half, row_y), (ccx + half, row_y), lw)
        # End tick marks
        tick_dir = 8 if deg > 0 else -8
        pygame.draw.line(canvas, col, (ccx - half, row_y),
                         (ccx - half, row_y + tick_dir), lw)
        pygame.draw.line(canvas, col, (ccx + half, row_y),
                         (ccx + half, row_y + tick_dir), lw)
        # Degree labels at major lines
        if major:
            lbl = str(abs(deg))
            fnt = _get_font(16)
            img = fnt.render(lbl, True, (255, 255, 255, 220))
            canvas.blit(img, (ccx - half - img.get_width() - 4, row_y - 9))
            canvas.blit(img, (ccx + half + 4, row_y - 9))

    # Horizon line (0°)
    hy = ccy - pitch_px
    if 0 < hy < ch:
        pygame.draw.line(canvas, (255, 255, 255, 200), (0, hy), (cw, hy), 2)

    # Rotate with roll
    rotated = pygame.transform.rotate(canvas, roll)
    rw, rh = rotated.get_size()
    ox, oy = (rw - aw) // 2, (rh - ah) // 2
    crop = pygame.Surface((aw, ah), pygame.SRCALPHA)
    crop.blit(rotated, (0, 0), pygame.Rect(ox, oy, aw, ah))
    surf.blit(crop, (ax, ay))


# ── Roll arc ──────────────────────────────────────────────────────────────────
def draw_roll_arc(surf, roll):
    """Draw roll scale arc and pointer at top of AI."""
    cx, cy = CX, ROLL_CY

    # Arc from -60 to +60 degrees (mapped to screen angles)
    for a in range(-150, -29):
        a1 = (a) * DEG
        a2 = (a + 1) * DEG
        x1 = int(cx + ROLL_R * math.cos(a1))
        y1 = int(cy + ROLL_R * math.sin(a1))
        x2 = int(cx + ROLL_R * math.cos(a2))
        y2 = int(cy + ROLL_R * math.sin(a2))
        pygame.draw.line(surf, LTGREY, (x1, y1), (x2, y2), 2)

    # Tick marks
    for deg2, length in [(0, 18), (10, 10), (20, 10), (30, 14),
                         (-10, 10), (-20, 10), (-30, 14),
                         (45, 10), (-45, 10), (60, 12), (-60, 12)]:
        ang = (-90 + deg2) * DEG
        x1 = int(cx + (ROLL_R - length) * math.cos(ang))
        y1 = int(cy + (ROLL_R - length) * math.sin(ang))
        x2 = int(cx + ROLL_R * math.cos(ang))
        y2 = int(cy + ROLL_R * math.sin(ang))
        pygame.draw.line(surf, LTGREY, (x1, y1), (x2, y2),
                         2 if deg2 == 0 else 1)
        # Hollow triangles at ±45
        if abs(deg2) == 45:
            mx = (x1 + x2) // 2
            my = (y1 + y2) // 2
            perp = (ang + math.pi / 2)
            tx, ty = int(6 * math.cos(perp)), int(6 * math.sin(perp))
            pygame.draw.polygon(surf, LTGREY,
                [(mx - tx, my - ty), (mx + tx, my + ty),
                 (int(cx + (ROLL_R - 20) * math.cos(ang)),
                  int(cy + (ROLL_R - 20) * math.sin(ang)))], 1)

    # Fixed sky pointer (upward-pointing triangle at top center)
    pygame.draw.polygon(surf, WHITE,
        [(CX - 7, ROLL_CY - ROLL_R + 2),
         (CX + 7, ROLL_CY - ROLL_R + 2),
         (CX,     ROLL_CY - ROLL_R + 14)])

    # Roll pointer (moves with roll)
    ra = (-90 - roll) * DEG
    rpx = int(cx + (ROLL_R - 3) * math.cos(ra))
    rpy = int(cy + (ROLL_R - 3) * math.sin(ra))
    perp = ra + math.pi / 2
    tx = int(9 * math.cos(perp))
    ty = int(9 * math.sin(perp))
    fx = int(6 * math.cos(ra))
    fy = int(6 * math.sin(ra))
    pygame.draw.polygon(surf, WHITE,
        [(rpx, rpy),
         (rpx - tx - fx, rpy - ty - fy),
         (rpx + tx - fx, rpy + ty - fy)])


# ── Aircraft symbol ───────────────────────────────────────────────────────────
def draw_aircraft_symbol(surf):
    ws = 78
    hw = int(ws * 0.22)
    # Wings
    pygame.draw.line(surf, YELLOW, (CX - ws, CY), (CX - hw, CY), 4)
    pygame.draw.line(surf, YELLOW, (CX + hw, CY), (CX + ws, CY), 4)
    # Wing tips (downward ticks)
    pygame.draw.line(surf, YELLOW, (CX - hw, CY), (CX - hw, CY + 10), 4)
    pygame.draw.line(surf, YELLOW, (CX + hw, CY), (CX + hw, CY + 10), 4)
    # Centre dot
    pygame.draw.circle(surf, YELLOW, (CX, CY), 5)


# ── Slip/skid indicator ───────────────────────────────────────────────────────
def draw_slip_ball(surf, ay):
    bw, bh, br = 52, 16, 8
    bx, by2 = CX - bw // 2, BALL_Y - bh // 2
    # Tube outline
    pygame.draw.rect(surf, (10, 10, 20), (bx, by2, bw, bh), border_radius=bh // 2)
    pygame.draw.rect(surf, DIMGREY, (bx, by2, bw, bh), 1, border_radius=bh // 2)
    # Centre tick marks
    mk = br + 4
    pygame.draw.line(surf, WHITE, (CX - mk, by2 + 2), (CX - mk, by2 + bh - 2), 2)
    pygame.draw.line(surf, WHITE, (CX + mk, by2 + 2), (CX + mk, by2 + bh - 2), 2)
    # Ball
    max_defl = bw // 2 - br - 2
    defl = int(max(-max_defl, min(max_defl, (ay / 0.2) * max_defl)))
    pygame.draw.circle(surf, WHITE, (CX + defl, BALL_Y), br)


# ── Speed tape ────────────────────────────────────────────────────────────────
PX_PER_KT  = TAPE_H / 120.0   # 120 kt visible range
PX_PER_FT  = TAPE_H / 600.0   # 600 ft visible range
PX_PER_DEG = DISPLAY_W / 60.0  # 60° visible heading range


def spd_y(v, speed): return int(TAPE_MID - (v - speed) * PX_PER_KT)
def alt_y(ft, alt):  return int(TAPE_MID - (ft - alt)  * PX_PER_FT)


def draw_speed_tape(surf, speed, hdg_bug_spd=None):
    """Left airspeed tape with GI-275-style V-speed colour bands."""
    # Background
    tape_surf = pygame.Surface((SPD_W, TAPE_H), pygame.SRCALPHA)
    tape_surf.fill(TAPE_BG)
    surf.blit(tape_surf, (SPD_X, TAPE_TOP))
    pygame.draw.line(surf, (255, 255, 255, 60), (SPD_X + SPD_W, TAPE_TOP),
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
    _band(VS0, VFE, WHITE, SPD_X + SPD_W - 10, 3)
    # Green arc: Vs1 – Vno  (normal ops)
    _band(VS1, VNO, GREEN_ARC, SPD_X + SPD_W - 5, 4)
    # Yellow arc: Vno – Vne (caution)
    _band(VNO, VNE, YELLOW_ARC, SPD_X + SPD_W - 5, 4)
    # Red Vne line
    vne_y = sy(VNE)
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
                         (SPD_X + SPD_W - tl, vy), (SPD_X + SPD_W, vy),
                         2 if major else 1)
        if major:
            _text(surf, str(v), 15, (230, 230, 230),
                  x=SPD_X + 2, y=vy - 8)

    # Speed readout box (pentagon pointing right)
    bh = 32
    by = TAPE_MID - bh // 2
    pts = [(SPD_X, by), (SPD_X + SPD_W, by),
           (SPD_X + SPD_W + 10, TAPE_MID),
           (SPD_X + SPD_W, by + bh), (SPD_X, by + bh)]
    pygame.draw.polygon(surf, (0, 10, 30), pts)
    pygame.draw.polygon(surf, WHITE, pts, 2)
    spd_col = RED if speed > VNE else (YELLOW if speed > VNO else WHITE)
    _text(surf, f"{round(speed):3d}", 22, spd_col, cx=SPD_X + SPD_W // 2 - 2, cy=TAPE_MID)

    # Header label
    _text(surf, "GS KT", 10, (140, 200, 255), x=SPD_X + 3, y=TAPE_TOP + 2)


# ── Altitude tape ──────────────────────────────────────────────────────────────
def draw_alt_tape(surf, alt, vspeed, baro_hpa, baro_src, alt_bug=None):
    """Right altitude tape with VSI and baro setting."""
    tape_surf = pygame.Surface((ALT_W, TAPE_H), pygame.SRCALPHA)
    tape_surf.fill(TAPE_BG)
    surf.blit(tape_surf, (ALT_X, TAPE_TOP))
    pygame.draw.line(surf, (255, 255, 255, 60), (ALT_X, TAPE_TOP),
                     (ALT_X, TAPE_BOT), 1)

    def ay2(ft): return alt_y(ft, alt)

    # Tick marks and numbers
    base = int(round(alt / 100)) * 100
    for ft in range(base - 400, base + 400, 100):
        fy = ay2(ft)
        if not (TAPE_TOP + 12 < fy < TAPE_BOT - 12):
            continue
        major = (ft % 500 == 0)
        tl = 14 if major else 7
        pygame.draw.line(surf, LTGREY,
                         (ALT_X, fy), (ALT_X + tl, fy),
                         2 if major else 1)
        if ft % 200 == 0:
            _text(surf, str(ft), 13, (230, 230, 230), x=ALT_X + tl + 2, y=fy - 7)

    # Altitude bug
    if alt_bug is not None:
        aby = ay2(alt_bug)
        if TAPE_TOP < aby < TAPE_BOT:
            bug = [(ALT_X, aby - 10), (ALT_X + 20, aby - 10),
                   (ALT_X + 26, aby), (ALT_X + 20, aby + 10), (ALT_X, aby + 10)]
            pygame.draw.polygon(surf, CYAN, bug)

    # Selected altitude display at top
    if alt_bug is not None:
        pygame.draw.rect(surf, (0, 40, 60), (ALT_X, 0, ALT_W, TAPE_TOP))
        _text(surf, f"{round(alt_bug):5d}", 14, CYAN, cx=ALT_X + ALT_W // 2, cy=11)

    # Altitude readout box (pentagon pointing left)
    bh = 32
    by = TAPE_MID - bh // 2
    pts = [(ALT_X + ALT_W, by), (ALT_X, by),
           (ALT_X - 10, TAPE_MID),
           (ALT_X, by + bh), (ALT_X + ALT_W, by + bh)]
    pygame.draw.polygon(surf, (0, 10, 30), pts)
    pygame.draw.polygon(surf, WHITE, pts, 2)
    _text(surf, f"{round(alt):5d}", 20, WHITE, cx=ALT_X + ALT_W // 2 + 2, cy=TAPE_MID)

    # VSI (vertical speed)
    arrow = "▲" if vspeed > 30 else ("▼" if vspeed < -30 else "—")
    vcol  = (0, 220, 0) if vspeed > 50 else ((255, 140, 0) if vspeed < -50 else LTGREY)
    vs_txt = f"{arrow}{abs(round(vspeed / 10) * 10):4d}"
    _text(surf, vs_txt, 13, vcol, x=ALT_X + 4, y=TAPE_MID + 20)
    _text(surf, "fpm", 10, (120, 160, 200), x=ALT_X + 18, y=TAPE_MID + 36)

    # Baro setting
    baro_str = f"{baro_hpa:.2f}" if baro_src == "bme280" else "GPS ALT"
    baro_col = CYAN if baro_src == "bme280" else (180, 180, 100)
    _text(surf, baro_str, 11, baro_col, x=ALT_X + 4, y=TAPE_MID + 52)
    if baro_src == "bme280":
        _text(surf, "hPa", 10, (100, 160, 200), x=ALT_X + 18, y=TAPE_MID + 65)

    # Header label
    _text(surf, "ALT FT", 10, (140, 200, 255), x=ALT_X + 6, y=TAPE_TOP + 2)


# ── Heading tape ──────────────────────────────────────────────────────────────
_CARDINALS = {0: "N", 45: "NE", 90: "E", 135: "SE",
              180: "S", 225: "SW", 270: "W", 315: "NW"}


def draw_heading_tape(surf, hdg, hdg_bug=None, track=None, gps_ok=False):
    """Bottom heading strip with bug and current-heading box."""
    hdg_surf = pygame.Surface((DISPLAY_W, HDG_H), pygame.SRCALPHA)
    hdg_surf.fill((0, 8, 22, 210))
    surf.blit(hdg_surf, (0, HDG_Y))
    pygame.draw.line(surf, (255, 255, 255, 80), (0, HDG_Y), (DISPLAY_W, HDG_Y), 1)

    # Tick marks
    for i in range(-35, 36):
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
            lbl = _CARDINALS.get(deg, f"{deg:03d}")
            col = YELLOW if deg in _CARDINALS else (230, 230, 230)
            _text(surf, lbl, 13, col, cx=x, y=HDG_Y + HDG_H - 15)

    # Heading bug
    if hdg_bug is not None:
        off = ((hdg_bug - hdg + 180) % 360) - 180
        hbx = int(CX + off * PX_PER_DEG)
        if 0 < hbx < DISPLAY_W:
            bug = [(hbx - 8, HDG_Y), (hbx + 8, HDG_Y),
                   (hbx + 8, HDG_Y + 10),
                   (hbx + 4, HDG_Y + 18),
                   (hbx - 4, HDG_Y + 18),
                   (hbx - 8, HDG_Y + 10)]
            pygame.draw.polygon(surf, CYAN, bug)

    # GPS track pointer (magenta, when GPS OK)
    if gps_ok and track is not None:
        off = ((track - hdg + 180) % 360) - 180
        tx = int(CX + off * PX_PER_DEG)
        if 0 < tx < DISPLAY_W:
            pygame.draw.polygon(surf, (220, 60, 220),
                [(tx, HDG_Y + 4), (tx - 5, HDG_Y + 14), (tx + 5, HDG_Y + 14)])

    # Current heading box
    bw, bh = 58, 22
    bx, by2 = CX - bw // 2, HDG_Y - bh - 2
    pygame.draw.rect(surf, (0, 0, 0), (bx, by2, bw, bh))
    pygame.draw.rect(surf, WHITE, (bx, by2, bw, bh), 1)
    _text(surf, f"{round(hdg) % 360:03d}°", 18, WHITE, cx=CX, cy=by2 + bh // 2)

    # Fixed triangle pointer above heading box
    pygame.draw.polygon(surf, YELLOW,
        [(CX - 7, HDG_Y - 1), (CX + 7, HDG_Y - 1), (CX, HDG_Y - 11)])


# ── Status badges ─────────────────────────────────────────────────────────────
def draw_status_badges(surf, ahrs_ok, gps_ok, baro_ok, baro_src, sats, connected):
    """Top-right status badges."""
    x = DISPLAY_W - 4
    fnt = _get_font(10)

    def badge(text, bg, fg=(255, 255, 255)):
        nonlocal x
        w = fnt.size(text)[0] + 10
        x -= w + 2
        pygame.draw.rect(surf, bg, (x, 4, w, 15))
        _text(surf, text, 10, fg, x=x + 5, y=5)
        return x

    link_col = (0, 130, 0) if connected else (130, 0, 0)
    badge("LINK" if connected else "NO LINK", link_col)

    gps_col = (0, 150, 0) if gps_ok else (130, 130, 0)
    badge(f"GPS {sats}sat" if gps_ok else "NO GPS", gps_col)

    if baro_ok:
        badge("BARO", (0, 80, 120))
    else:
        badge("GPS ALT", (80, 80, 0), (220, 220, 100))

    ahrs_col = (0, 100, 80) if ahrs_ok else (150, 0, 0)
    badge("AHRS" if ahrs_ok else "AHRS FAIL", ahrs_col)


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


def draw_failure_overlays(surf, ahrs_ok, gps_ok, baro_ok):
    ai_h_used = TAPE_H
    ai_y = TAPE_TOP
    ai_w = ALT_X - SPD_W
    if not ahrs_ok:
        # Cover AI center + heading strip
        draw_red_x(surf, SPD_W, ai_y, ai_w, ai_h_used, "ATTITUDE")
        draw_red_x(surf, 0, HDG_Y, DISPLAY_W, HDG_H, "HDG")
    if not gps_ok:
        draw_red_x(surf, SPD_X, ai_y, SPD_W, ai_h_used, "AIRSPD")
    if not baro_ok and not gps_ok:
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
             "yaw_bug", "alt_bug", "_dur"),
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
        # Apply to shared state
        with _state_lock:
            for k in ("roll", "pitch", "yaw", "alt", "speed", "vspeed",
                      "ay", "lat", "lon", "fix", "sats",
                      "ahrs_ok", "gps_ok", "baro_ok", "baro_src"):
                if k in self._target:
                    state[k] = self._target[k]
            state["gps_alt"] = state["alt"]
            state["track"]   = state["yaw"]


# ── Touch handler ─────────────────────────────────────────────────────────────
_touch_t0    = {}
_bug_dragging = None   # "hdg" | "alt"


def handle_event(event, demo_mode):
    global _bug_dragging
    if event.type == pygame.QUIT:
        return False

    if event.type == pygame.KEYDOWN:
        if event.key == pygame.K_ESCAPE:
            return False
        if event.key == pygame.K_d:
            return "toggle_demo"
        if event.key == pygame.K_UP:
            disp["alt_bug"] = round(disp["alt_bug"] / 100) * 100 + 100
        if event.key == pygame.K_DOWN:
            disp["alt_bug"] = round(disp["alt_bug"] / 100) * 100 - 100
        if event.key == pygame.K_LEFT:
            disp["hdg_bug"] = (round(disp["hdg_bug"]) - 10) % 360
        if event.key == pygame.K_RIGHT:
            disp["hdg_bug"] = (round(disp["hdg_bug"]) + 10) % 360
        if event.key == pygame.K_PLUS or event.key == pygame.K_EQUALS:
            disp["baro_hpa"] = round(disp["baro_hpa"] * 100 + 1) / 100
        if event.key == pygame.K_MINUS:
            disp["baro_hpa"] = round(disp["baro_hpa"] * 100 - 1) / 100

    if event.type in (pygame.MOUSEBUTTONDOWN, pygame.FINGERDOWN):
        pos = event.pos if hasattr(event, "pos") else (
            int(event.x * DISPLAY_W), int(event.y * DISPLAY_H))
        x, y = pos
        # Touch on alt tape → adjust alt bug
        if ALT_X <= x <= DISPLAY_W and TAPE_TOP <= y <= TAPE_BOT:
            ft = round(disp["alt"] + (TAPE_MID - y) / PX_PER_FT)
            disp["alt_bug"] = round(ft / 100) * 100
        # Touch on heading tape → adjust hdg bug
        if HDG_Y <= y <= DISPLAY_H:
            off = (x - CX) / PX_PER_DEG
            disp["hdg_bug"] = round(disp["yaw"] + off) % 360

    return True


# ── Main render function ──────────────────────────────────────────────────────
def render(surf, demo_mode, connected):
    surf.fill((0, 0, 0))

    roll    = disp["roll"]
    pitch   = disp["pitch"]
    hdg     = disp["yaw"]
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

    ai_rect = (AI_X, AI_Y, AI_W, AI_H)

    # 1. SVT / AI background
    import os
    has_terrain = any(
        f.endswith(".hgt")
        for f in os.listdir(SRTM_DIR)
        if os.path.isfile(os.path.join(SRTM_DIR, f))
    ) if os.path.isdir(SRTM_DIR) else False

    if has_terrain:
        draw_ai_background(surf, ai_rect, pitch, roll, hdg, alt, lat, lon)
    else:
        draw_simple_ai_background(surf, ai_rect, pitch, roll)

    # 2. Pitch ladder (with roll rotation)
    draw_pitch_ladder(surf, ai_rect, pitch, roll)

    # 3. Speed tape
    draw_speed_tape(surf, speed)

    # 4. Alt tape
    draw_alt_tape(surf, alt, vspeed, baro_hpa, baro_src, alt_bug)

    # 5. Heading tape
    draw_heading_tape(surf, hdg, hdg_bug, track, gps_ok)

    # 6. Roll arc
    draw_roll_arc(surf, roll)

    # 7. Aircraft symbol
    draw_aircraft_symbol(surf)

    # 8. Slip ball
    draw_slip_ball(surf, ay)

    # 9. Status badges
    draw_status_badges(surf, ahrs_ok, gps_ok, baro_ok, baro_src, sats, connected)

    # 10. Failure overlays
    draw_failure_overlays(surf, ahrs_ok, gps_ok, baro_ok)

    # 11. Demo watermark
    if demo_mode:
        _text(surf, "DEMO", 14, (255, 60, 60, 200), cx=CX, cy=CY - 20)


# ── Main entry point ──────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="PFD Display")
    parser.add_argument("--demo", action="store_true",
                        help="Run Sedona demo (no Pico W needed)")
    parser.add_argument("--sim",  action="store_true",
                        help="Windowed mode for desktop testing")
    args = parser.parse_args()

    if args.sim or not FULLSCREEN:
        # Desktop / windowed mode
        os.environ["SDL_VIDEODRIVER"] = "x11"
        os.environ.pop("SDL_FBDEV", None)

    pygame.init()
    pygame.mouse.set_visible(False)

    if (not args.sim) and FULLSCREEN:
        surf = pygame.display.set_mode(
            (DISPLAY_W, DISPLAY_H),
            pygame.FULLSCREEN | pygame.NOFRAME
        )
    else:
        surf = pygame.display.set_mode((DISPLAY_W, DISPLAY_H))

    pygame.display.set_caption("PFD")
    clock = pygame.time.Clock()

    demo_mode = args.demo
    demo      = DemoState() if demo_mode else None
    sse       = None
    connected = False

    if not demo_mode:
        sse = SSEClient(SSE_URL, state, _state_lock)
        sse.start()
        print(f"[PFD] Connecting to {SSE_URL}")
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

        if sse:
            connected = sse.connected

        # Render
        render(surf, demo_mode, connected)
        pygame.display.flip()
        clock.tick(TARGET_FPS)

    if sse:
        sse.stop()
    pygame.quit()


if __name__ == "__main__":
    main()
