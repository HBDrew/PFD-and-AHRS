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


def _rolling_drum(surf, bx, by, bw, bh, value, n_digits, color, font_sz,
                  suppress_leading=False):
    """
    Veeder-Root style rolling-drum digit readout for pygame.
    Only the units digit animates continuously; higher digits snap.
    suppress_leading: skip leading-zero digit cells (e.g. for altitude).
    """
    char_w  = bw // n_digits
    f       = _get_font(font_sz, bold=True)
    val_int = int(abs(value))

    for col_i in range(n_digits):
        power = n_digits - 1 - col_i   # 0 = units, 1 = tens, …
        # Suppress leading zeros: skip cell if all higher digits are zero
        if suppress_leading and power > 0 and val_int < 10 ** power:
            continue
        if power == 0:
            d_cont = float(value % 10.0)
        else:
            d_cont = float((int(value) // (10 ** power)) % 10)
        d_lo = int(d_cont)
        frac  = d_cont - d_lo
        d_hi  = (d_lo + 1) % 10

        cx = bx + col_i * char_w
        # Cell surface — pygame clips blit content to bounds automatically
        cell = pygame.Surface((char_w, bh), pygame.SRCALPHA)

        img_lo = f.render(str(d_lo), True, color)
        img_hi = f.render(str(d_hi), True, color)

        scroll  = int(frac * bh)
        tx_lo   = max(0, (char_w - img_lo.get_width()) // 2)
        ty_base = max(0, (bh    - img_lo.get_height()) // 2)

        cell.blit(img_lo, (tx_lo, ty_base - scroll))
        cell.blit(img_hi, (max(0, (char_w - img_hi.get_width()) // 2),
                            ty_base - scroll + bh))
        surf.blit(cell, (cx, by))


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
    Fallback SVT background (no SRTM tiles loaded).
    Renders sky gradient + perspective terrain with foreshortening
    and Sedona-style mesa silhouettes.
    """
    ax, ay, aw, ah = ai_rect

    pad = int(max(aw, ah) * 0.85)
    cw, ch = aw + pad * 2, ah + pad * 2
    canvas = pygame.Surface((cw, ch))

    px_per_deg = 10.0
    hy = ch // 2 + int(pitch * px_per_deg)  # horizon y in canvas

    # ── Sky gradient ──────────────────────────────────────────────────────────
    for row in range(max(0, min(ch, hy + 1))):
        t = max(0.0, min(1.0, 1.0 - (hy - row) / max(1, hy)))
        col = lerp_col(SKY_TOP, SKY_HOR, t)
        pygame.draw.line(canvas, col, (0, row), (cw, row))

    # ── Ground with perspective foreshortening ────────────────────────────────
    # Near terrain (bottom) is lighter/more saturated; far (near horizon) darker.
    GND_NEAR = ( 80, 110,  40)  # greener close terrain
    GND_MID  = (120,  85,  38)  # midrange brownish
    GND_FAR  = ( 70,  50,  25)  # dark near horizon
    for row in range(max(0, hy), ch):
        t = (row - hy) / max(1, ch - hy)          # 0=horizon, 1=bottom
        # foreshorten: most variation in the top quarter (distant terrain)
        if t < 0.15:
            col = lerp_col(GND_FAR, GND_MID, t / 0.15)
        elif t < 0.5:
            col = lerp_col(GND_MID, GND_NEAR, (t - 0.15) / 0.35)
        else:
            col = GND_NEAR
        pygame.draw.line(canvas, col, (0, row), (cw, row))

    # ── Horizon line ──────────────────────────────────────────────────────────
    if 0 < hy < ch:
        pygame.draw.line(canvas, WHITE, (0, hy), (cw, hy), 2)

    # ── Rotate for roll ───────────────────────────────────────────────────────
    rotated = pygame.transform.rotate(canvas, roll)
    rw, rh = rotated.get_size()
    ox = max(0, (rw - aw) // 2)
    oy = max(0, (rh - ah) // 2)
    blit_w = min(aw, rw - ox)
    blit_h = min(ah, rh - oy)
    crop = rotated.subsurface(pygame.Rect(ox, oy, blit_w, blit_h))
    surf.blit(crop, (ax, ay))


# ── Pitch ladder ──────────────────────────────────────────────────────────────
def draw_pitch_ladder(surf, ai_rect, pitch, roll):
    """White pitch ladder lines, rotated with roll, overlaid on AI."""
    ax, ay, aw, ah = ai_rect
    cx, cy = ax + aw // 2, ay + ah // 2

    px_per_deg = 10.0   # 10 display pixels per degree of pitch

    # Render on a transparent canvas, then rotate
    pad = int(max(aw, ah) * 0.75)
    cw, ch = aw + pad * 2, ah + pad * 2
    canvas = pygame.Surface((cw, ch), pygame.SRCALPHA)
    ccx, ccy = cw // 2, ch // 2

    pitch_px = int(pitch * px_per_deg)

    # Line half-widths based on AI width.
    # GI-275 style: major ~7% each side, minor ~4%.
    major_half = int(aw * 0.07)   # ~34 px
    minor_half = int(aw * 0.04)   # ~19 px

    for deg in range(-30, 35, 5):
        if deg == 0:
            continue
        row_y = ccy + pitch_px - int(deg * px_per_deg)
        if row_y < ccy - 185:   # don't draw above roll arc area (display y < 44)
            continue
        if row_y > ccy + 185:   # don't draw below heading tape (display y > 414)
            continue
        if not (10 < row_y < ch - 10):
            continue
        major = (deg % 10 == 0)
        half  = major_half if major else minor_half
        col   = (255, 255, 255, 220)
        if major:
            pygame.draw.line(canvas, col, (ccx - half, row_y), (ccx + half, row_y), 2)
        else:
            pygame.draw.aaline(canvas, col, (ccx - half, row_y), (ccx + half, row_y))
        # End tick marks (inward — up for positive pitch, down for negative)
        tick_dir = 8 if deg > 0 else -8
        pygame.draw.aaline(canvas, col, (ccx - half, row_y), (ccx - half, row_y + tick_dir))
        pygame.draw.aaline(canvas, col, (ccx + half, row_y), (ccx + half, row_y + tick_dir))
        # Degree labels at major lines
        if major:
            lbl = str(abs(deg))
            fnt = _get_font(16)
            img = fnt.render(lbl, True, (255, 255, 255, 220))
            canvas.blit(img, (ccx - half - img.get_width() - 4, row_y - 9))
            canvas.blit(img, (ccx + half + 4, row_y - 9))

    # Horizon line (0°) — same width as major pitch lines
    hy = ccy + pitch_px
    if 0 < hy < ch:
        pygame.draw.line(canvas, (255, 255, 255, 200),
                         (ccx - major_half, hy), (ccx + major_half, hy), 2)

    # Rotate with roll
    rotated = pygame.transform.rotate(canvas, roll)
    rw, rh = rotated.get_size()
    ox, oy = (rw - aw) // 2, (rh - ah) // 2
    crop = pygame.Surface((aw, ah), pygame.SRCALPHA)
    crop.blit(rotated, (0, 0), pygame.Rect(ox, oy, aw, ah))
    surf.blit(crop, (ax, ay))


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
    and doghouse roll pointer."""
    cx, cy = CX, ROLL_CY

    # Arc (-60° to +60° of bank, mapped to screen top), rotates with roll
    for a in range(-150, -29):
        a1, a2 = (a + roll) * DEG, (a + 1 + roll) * DEG
        x1 = int(cx + ROLL_R * math.cos(a1))
        y1 = int(cy + ROLL_R * math.sin(a1))
        x2 = int(cx + ROLL_R * math.cos(a2))
        y2 = int(cy + ROLL_R * math.sin(a2))
        # Draw at r and r+1 to approximate 2px AA arc
        pygame.draw.aaline(surf, LTGREY, (x1, y1), (x2, y2))
        x1b = int(cx + (ROLL_R + 1) * math.cos(a1))
        y1b = int(cy + (ROLL_R + 1) * math.sin(a1))
        x2b = int(cx + (ROLL_R + 1) * math.cos(a2))
        y2b = int(cy + (ROLL_R + 1) * math.sin(a2))
        pygame.draw.aaline(surf, LTGREY, (x1b, y1b), (x2b, y2b))

    # Tick marks
    for deg2, length in [(10, 9), (20, 9), (30, 13),
                         (-10, 9), (-20, 9), (-30, 13),
                         (45, 9), (-45, 9), (60, 11), (-60, 11)]:
        ang = (-90 + deg2 + roll) * DEG
        x1 = int(cx + (ROLL_R - length) * math.cos(ang))
        y1 = int(cy + (ROLL_R - length) * math.sin(ang))
        x2 = int(cx + ROLL_R * math.cos(ang))
        y2 = int(cy + ROLL_R * math.sin(ang))
        pygame.draw.aaline(surf, LTGREY, (x1, y1), (x2, y2))
        # Hollow triangles at ±45
        if abs(deg2) == 45:
            perp = ang + math.pi / 2
            tx2, ty2 = int(5 * math.cos(perp)), int(5 * math.sin(perp))
            mx, my = (x1 + x2) // 2, (y1 + y2) // 2
            inner_x = int(cx + (ROLL_R - 16) * math.cos(ang))
            inner_y = int(cy + (ROLL_R - 16) * math.sin(ang))
            tri = [(mx - tx2, my - ty2), (mx + tx2, my + ty2), (inner_x, inner_y)]
            pygame.gfxdraw.aapolygon(surf, tri, LTGREY)

    # Moving upper doghouse — OUTSIDE arc, tip at arc, moves with roll arc
    upper_ang = (-90 + roll) * DEG
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
    """GI-275 style amber aircraft reference: outward triangles + centre ring."""
    ws  = 62   # half wingspan (outer tip)
    gap = 10   # distance from centre to inner base of each triangle
    h   = 5    # half-height at the triangle base

    droop = int((ws - gap) * math.tan(20 * DEG))   # ~19 px dihedral droop at tip

    # Left wing — upper half AMBER, lower half darker, full outline AA
    l_top  = [(CX - ws, CY - h + droop), (CX - ws, CY + droop),     (CX - gap, CY)]
    l_bot  = [(CX - ws, CY + droop),     (CX - ws, CY + h + droop), (CX - gap, CY)]
    l_full = [(CX - ws, CY - h + droop), (CX - ws, CY + h + droop), (CX - gap, CY)]
    pygame.gfxdraw.filled_polygon(surf, l_top, AMBER)
    pygame.gfxdraw.filled_polygon(surf, l_bot, AMBER_DARK)
    pygame.gfxdraw.aapolygon(surf, l_full, AMBER)

    # Right wing — mirror
    r_top  = [(CX + ws, CY - h + droop), (CX + ws, CY + droop),     (CX + gap, CY)]
    r_bot  = [(CX + ws, CY + droop),     (CX + ws, CY + h + droop), (CX + gap, CY)]
    r_full = [(CX + ws, CY - h + droop), (CX + ws, CY + h + droop), (CX + gap, CY)]
    pygame.gfxdraw.filled_polygon(surf, r_top, AMBER)
    pygame.gfxdraw.filled_polygon(surf, r_bot, AMBER_DARK)
    pygame.gfxdraw.aapolygon(surf, r_full, AMBER)

    # Centre reference circle (open ring)
    pygame.gfxdraw.filled_circle(surf, CX, CY, 6, AMBER)
    pygame.gfxdraw.aacircle(surf,     CX, CY, 6, AMBER)
    pygame.gfxdraw.filled_circle(surf, CX, CY, 3, (0, 0, 0, 0))  # hollow centre
    pygame.gfxdraw.aacircle(surf,     CX, CY, 3, AMBER)


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
PX_PER_DEG = DISPLAY_W / 60.0  # 60° visible heading range


def spd_y(v, speed): return int(TAPE_MID - (v - speed) * PX_PER_KT)
def alt_y(ft, alt):  return int(TAPE_MID - (ft - alt)  * PX_PER_FT)


def draw_speed_tape(surf, speed, gs_bug=None):
    """Left airspeed tape with GI-275-style V-speed colour bands."""
    # Background (full height including top strip, matching alt tape)
    tape_surf = pygame.Surface((SPD_W, TAPE_BOT), pygame.SRCALPHA)
    tape_surf.fill(TAPE_BG)
    surf.blit(tape_surf, (SPD_X, 0))
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
                         (SPD_X, vy), (SPD_X + tl, vy),
                         2 if major else 1)
        if major:
            _text(surf, str(v), 17, (230, 230, 230), bold=True,
                  x=SPD_X + tl + 2, y=vy - 9)

    # Speed readout box — convex point on outer (left) edge, 90° tip
    bh = 44
    by = TAPE_MID - bh // 2
    pd = bh // 2  # 22px → 135° corners, 90° tip
    pts = [(SPD_X + SPD_W, by), (SPD_X + pd, by),
           (SPD_X, TAPE_MID),                        # point at outer/left edge
           (SPD_X + pd, by + bh), (SPD_X + SPD_W, by + bh)]
    pygame.gfxdraw.filled_polygon(surf, pts, (0, 10, 30))
    pygame.gfxdraw.aapolygon(surf, pts, WHITE)
    spd_col = RED if speed > VNE else (YELLOW if speed > VNO else WHITE)
    # Drum starts at flat part of box (SPD_X+pd), same font_sz as alt
    _rolling_drum(surf, SPD_X + pd + 2, TAPE_MID - 17, SPD_W - pd - 4, 34, speed, 3, spd_col, 20)

    # GS bug — 7-point, 28px tall × 14px deep matching heading bug rotated 90°
    if gs_bug is not None:
        gby = spd_y(gs_bug, speed)
        if TAPE_TOP < gby < TAPE_BOT:
            gb = [(SPD_X,      gby - 14),
                  (SPD_X + 14, gby - 14), (SPD_X + 14, gby - 5), (SPD_X + 7, gby),
                  (SPD_X + 14, gby + 5),  (SPD_X + 14, gby + 14), (SPD_X, gby + 14)]
            pygame.draw.polygon(surf, CYAN, gb)

    # GS bug button — top strip of speed tape
    gs_str = f"{round(gs_bug):3d}" if gs_bug is not None else "---"
    _cyan_box(surf, "GS", gs_str, x=SPD_X, y=2, w=SPD_W, h=18)


# ── Altitude tape ──────────────────────────────────────────────────────────────
def draw_alt_tape(surf, alt, vspeed, baro_hpa, baro_src, alt_bug=None):
    """Right altitude tape with VSI and baro setting."""
    tape_surf = pygame.Surface((ALT_W, TAPE_H), pygame.SRCALPHA)
    tape_surf.fill(TAPE_BG)
    surf.blit(tape_surf, (ALT_X, TAPE_TOP))
    pygame.draw.line(surf, (255, 255, 255, 60), (ALT_X, TAPE_TOP),
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

    # ALT bug button — top strip of alt tape
    alt_str = f"{round(alt_bug):5d}" if alt_bug is not None else "-----"
    _cyan_box(surf, "ALT", alt_str, x=ALT_X, y=2, w=ALT_W, h=18)

    # Altitude readout box — convex point on outer (right) edge, 90° tip
    bh = 44
    by = TAPE_MID - bh // 2
    pd = bh // 2  # 22px → 135° corners, 90° tip
    pts = [(ALT_X, by), (ALT_X + ALT_W - pd, by),
           (ALT_X + ALT_W, TAPE_MID),            # point at outer/right edge
           (ALT_X + ALT_W - pd, by + bh), (ALT_X, by + bh)]
    pygame.gfxdraw.filled_polygon(surf, pts, (0, 10, 30))
    pygame.gfxdraw.aapolygon(surf, pts, WHITE)
    # Rolling drum: 4 digits, fits in flat part of box, same font_sz as speed
    _rolling_drum(surf, ALT_X + 2, TAPE_MID - 17, ALT_W - pd - 4, 34, alt, 4, WHITE, 20,
                  suppress_leading=True)

    # Altitude bug — 7-point, 28px tall × 14px deep matching heading bug rotated 90°
    if alt_bug is not None:
        aby = ay2(alt_bug)
        if TAPE_TOP < aby < TAPE_BOT:
            bug = [(ALT_X + ALT_W,      aby - 14),
                   (ALT_X + ALT_W - 14, aby - 14), (ALT_X + ALT_W - 14, aby - 5), (ALT_X + ALT_W - 7, aby),
                   (ALT_X + ALT_W - 14, aby + 5),  (ALT_X + ALT_W - 14, aby + 14), (ALT_X + ALT_W, aby + 14)]
            pygame.draw.polygon(surf, CYAN, bug)

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

    # (ALT FT label replaced by ALT bug button at top)


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
            # Wide, short bug with V-notch at top matching speed/alt bug style
            bug = [(hbx - 14, HDG_Y + 14), (hbx - 14, HDG_Y),
                   (hbx - 5,  HDG_Y), (hbx, HDG_Y + 7), (hbx + 5, HDG_Y),
                   (hbx + 14, HDG_Y), (hbx + 14, HDG_Y + 14)]
            pygame.gfxdraw.filled_polygon(surf, bug, CYAN)
            pygame.gfxdraw.aapolygon(surf, bug, CYAN)

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
    _text(surf, f"{round(hdg) % 360:03d}\u00b0", 20, WHITE, bold=True, cx=CX, cy=by2 + bh // 2)

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


# ── Cyan tap-buttons (HDG bug, BARO, ALT bug) ────────────────────────────────
# These sit just below the heading tape and below each tape's bottom edge,
# styled as cyan-bordered dark boxes matching the GI-275 blue label style.

def _cyan_box(surf, label, value_str, x, y, w=80, h=22):
    """Draw a dark box with cyan border and label:value text."""
    pygame.draw.rect(surf, (0, 20, 35), (x, y, w, h))
    pygame.draw.rect(surf, CYAN, (x, y, w, h), 1)
    full = f"{label} {value_str}"
    _text(surf, full, 13, CYAN, cx=x + w // 2, cy=y + h // 2)


def draw_tap_buttons(surf, hdg, hdg_bug, baro_hpa, baro_src, alt_bug):
    """
    Cyan tap zones in the heading strip — left and right only so the centre
    heading readout remains unobstructed:
      • Left  (under speed tape) : HDG bug
      • Right (under alt tape)   : QNH / BARO
    IAS and ALT bug buttons are drawn at the tops of their own tapes.
    """
    y = HDG_Y + 2

    # HDG bug — left side of heading strip, under speed tape
    _cyan_box(surf, "HDG", f"{round(hdg_bug):03d}\u00b0",
              x=SPD_X, y=y, w=SPD_W + 10, h=20)

    # QNH / BARO — right side of heading strip, under alt tape
    baro_lbl = f"{baro_hpa:.2f}hP" if baro_src == "bme280" else "GPS ALT"
    _cyan_box(surf, "QNH", baro_lbl,
              x=ALT_X - 10, y=y, w=ALT_W + 10, h=20)


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
    if _has_terrain:
        draw_ai_background(surf, ai_rect, pitch, roll, hdg, alt, lat, lon)
    else:
        draw_simple_ai_background(surf, ai_rect, pitch, roll)

    # 2. Pitch ladder (with roll rotation)
    draw_pitch_ladder(surf, ai_rect, pitch, roll)

    # 3. Speed tape
    draw_speed_tape(surf, speed, gs_bug=disp.get("spd_bug"))

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

    # 11. Cyan tap-buttons for heading bug, baro, and alt bug
    draw_tap_buttons(surf, hdg, hdg_bug, baro_hpa, baro_src, alt_bug)

    # 12. Demo watermark
    if demo_mode:
        _text(surf, "DEMO", 14, (255, 60, 60), cx=CX, cy=CY - 20)


# ── Terrain availability (computed once at import time) ───────────────────────
def _check_terrain():
    if not os.path.isdir(SRTM_DIR):
        return False
    return any(f.endswith(".hgt") for f in os.listdir(SRTM_DIR))

_has_terrain = _check_terrain()


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
