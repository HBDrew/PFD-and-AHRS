#!/usr/bin/env python3
"""
Generate static PFD preview images at 640x480 (Pi Zero 2W / KLAYERS 3.5" DSI).
Sedona AZ demo scenarios — GI-275 inspired layout.

Layout matches pi_display/pfd.py exactly.
"""

from PIL import Image, ImageDraw, ImageFont
import math, os, random

W, H = 640, 480

# ── Layout (must match pi_display/pfd.py / config.py) ────────────────────────
SPD_X    = 0;    SPD_W = 74
ALT_W    = 74;   ALT_X = W - ALT_W          # 566
HDG_H    = 44;   HDG_Y = H - HDG_H          # 436
TAPE_TOP = 22;   TAPE_BOT = HDG_Y           # 436
TAPE_H   = TAPE_BOT - TAPE_TOP              # 414
TAPE_MID = (TAPE_TOP + TAPE_BOT) // 2       # 229
CX = W // 2;     CY = TAPE_MID             # 320, 229
ROLL_R   = 148;  ROLL_CY = ROLL_R + 16     # 164
BALL_Y   = HDG_Y - 30                       # 406
DEG      = math.pi / 180
AI_X     = SPD_W                            # 74
AI_W     = ALT_X - SPD_W                   # 492

# V-speeds (Cessna 172S)
VS0=48; VS1=55; VFE=85; VNO=129; VNE=163
PX_PER_KT  = TAPE_H / 120.0
PX_PER_FT  = TAPE_H / 600.0
PX_PER_DEG = W / 60.0

# ── Colour palette ─────────────────────────────────────────────────────────────
SKY_TOP    = ( 10,  42,  80)
SKY_HOR    = ( 58, 130, 200)
ROCK_LIGHT = (192,  82,  38)
ROCK_MID   = (160,  58,  22)
ROCK_DARK  = (110,  36,  12)
VALLEY     = (168,  98,  48)
JUNIPER    = ( 52,  80,  34)
SAND       = (196, 158,  88)
WHITE      = (255, 255, 255)
YELLOW     = (255, 215,   0)
CYAN       = (  0, 220, 220)
RED        = (220,  30,  30)
LTGREY     = (180, 180, 190)
GREEN_ARC  = ( 30, 200,  50)
YELLOW_ARC = (240, 200,   0)

def lerp_color(a, b, t):
    t = max(0.0, min(1.0, t))
    return tuple(int(a[i] + (b[i]-a[i])*t) for i in range(3))


_fnt_cache: dict = {}

def fnt(size, bold=False):
    key = (size, bold)
    if key in _fnt_cache:
        return _fnt_cache[key]
    base = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono"
    suffix = "-Bold" if bold else ""
    try:
        _fnt_cache[key] = ImageFont.truetype(f"{base}{suffix}.ttf", size)
    except Exception:
        _fnt_cache[key] = ImageFont.load_default()
    return _fnt_cache[key]


def rolling_drum(img, bx, by, bw, bh, value, n_digits, color, font_sz,
                 suppress_leading=False):
    """
    Veeder-Root style rolling-drum digit readout.
    Only the units digit animates continuously; higher digits snap.
    suppress_leading: skip leading-zero digit cells (e.g. altitude).
    """
    char_w  = bw // n_digits
    f       = fnt(font_sz, bold=True)
    val_int = int(abs(value))
    try:
        ref_bbox = f.getbbox("0")
        ch = ref_bbox[3] - ref_bbox[1]
        cw_ch = ref_bbox[2] - ref_bbox[0]
    except Exception:
        ch = font_sz; cw_ch = font_sz // 2

    col_rgba = color + (255,)

    for col_i in range(n_digits):
        power = n_digits - 1 - col_i   # 0 = units, 1 = tens, …
        if suppress_leading and power > 0 and val_int < 10 ** power:
            continue
        if power == 0:
            d_cont = float(value % 10.0)
        else:
            d_cont = float((int(value) // (10 ** power)) % 10)
        d_lo = int(d_cont)
        frac  = d_cont - d_lo
        d_hi  = (d_lo + 1) % 10

        scroll = int(frac * bh)
        tx     = max(0, (char_w - cw_ch) // 2)
        ty_lo  = (bh - ch) // 2 - scroll
        ty_hi  = ty_lo + bh

        # Render into a 2× tall cell, then crop to bh (auto-clips scrolled digits)
        cell   = Image.new('RGBA', (char_w, bh * 2), (0, 0, 0, 0))
        cd     = ImageDraw.Draw(cell)
        cd.text((tx, ty_lo), str(d_lo), fill=col_rgba, font=f)
        cd.text((tx, ty_hi), str(d_hi), fill=col_rgba, font=f)

        cx_pos  = bx + col_i * char_w
        cropped = cell.crop((0, 0, char_w, bh))
        img.paste(cropped, (cx_pos, by), cropped)


def draw_scene(roll, pitch, hdg, alt, speed, vspeed, ay,
               hdg_bug, alt_bug, label, filename,
               ahrs_ok=True, gps_ok=True, baro_ok=False, sats=8):

    img  = Image.new('RGB', (W, H), (0, 0, 0))
    draw = ImageDraw.Draw(img)

    # ── 1. SVT SKY / GROUND background (rotated with roll) ───────────────────
    # Render at 4× size for rotation quality then crop
    S = 4
    ai_img  = Image.new('RGBA', (W*S, H*S), (0, 0, 0, 0))
    ai_draw = ImageDraw.Draw(ai_img)

    focal    = 520.0
    pitch_px = focal * math.tan(pitch * DEG)
    hy = H*2 + int(-pitch_px)   # horizon row in enlarged canvas (centred at H*2)

    # Sky gradient
    for y in range(max(0, hy)):
        t   = max(0.0, min(1.0, 1.0 - y / max(1, hy)))
        col = lerp_color(SKY_HOR, SKY_TOP, t)
        ai_draw.line([(0, y), (W*S, y)], fill=col + (255,))

    # Ground — perspective foreshortening: dark/muted near horizon, richer close
    GND_FAR  = ( 70,  50,  25)
    GND_MID  = (120,  78,  35)
    GND_NEAR = ( 80, 105,  38)
    for y in range(max(0, hy), H*S):
        depth = (y - hy) / max(1, H*2)   # 0=horizon, 1=bottom
        if depth < 0.12:
            col = lerp_color(GND_FAR,  GND_MID,  depth / 0.12)
        elif depth < 0.45:
            col = lerp_color(GND_MID,  GND_NEAR, (depth - 0.12) / 0.33)
        elif depth < 0.65:
            # Sedona red-rock zone at mid-distance
            col = lerp_color(GND_NEAR, ROCK_MID, (depth - 0.45) / 0.20)
        else:
            col = lerp_color(ROCK_MID, ROCK_LIGHT, min(1.0, (depth - 0.65) / 0.35))
        ai_draw.line([(0, y), (W*S, y)], fill=col + (255,))

    # Sedona canyon / juniper texture
    random.seed(42)
    for _ in range(280):
        tx = random.randint(0, W*S)
        ty = random.randint(hy + 20, H*S - 10)
        tw = random.randint(6, 60)
        th = random.randint(3, 18)
        kind = random.random()
        c = (JUNIPER + (180,) if kind < 0.25
             else ROCK_DARK + (140,) if kind < 0.55
             else ROCK_LIGHT + (100,))
        ai_draw.ellipse([(tx, ty), (tx+tw, ty+th)], fill=c)

    # Distant ridgeline silhouette
    rpts = []
    ridge_y = hy + 28
    for x in range(0, W*S, 6):
        ny = ridge_y + int(18*math.sin(x*0.007) + 12*math.sin(x*0.019) +
                            8*math.sin(x*0.041))
        rpts.append((x, ny))
    for i in range(len(rpts) - 1):
        ai_draw.line([rpts[i], rpts[i+1]], fill=ROCK_DARK+(220,), width=3)

    # Mesa / butte silhouettes (Sedona flat-tops)
    MESA      = (160,  60,  22, 240)
    MESA_DARK = (110,  36,  12, 240)
    for mx, mw, mh in [(W*0.18, W*0.20, 32), (W*0.55, W*0.25, 44),
                        (W*0.82, W*0.17, 26)]:
        base = hy + 8
        top  = base - mh
        mesa_pts = [(int(mx - mw/2), base), (int(mx + mw/2), base),
                    (int(mx + mw/2 - 8), top), (int(mx - mw/2 + 8), top)]
        ai_draw.polygon(mesa_pts, fill=MESA_DARK)
        # Lit top edge
        ai_draw.line([(int(mx - mw/2 + 10), top),
                      (int(mx + mw/2 - 10), top)],
                     fill=MESA, width=4)

    # Horizon line
    ai_draw.line([(0, hy), (W*S, hy)], fill=(255,255,255,220), width=2)

    # Pitch ladder — half-widths in display pixels
    # GI-275: major ~14% AI width each side, minor ~8%
    major_half = int(AI_W * 0.07)   # ~34 px
    minor_half = int(AI_W * 0.04)   # ~20 px
    fn18 = fnt(18, bold=True)
    for deg in range(-30, 35, 5):
        if deg == 0: continue
        ly   = hy - int(focal * math.tan(deg * DEG))
        major = (deg % 10 == 0)
        half  = major_half if major else minor_half
        lw    = 3 if major else 1
        ai_draw.line([(W*2 - half, ly), (W*2 + half, ly)],
                     fill=(255,255,255,230), width=lw)
        td = 8 if deg > 0 else -8
        ai_draw.line([(W*2 - half, ly), (W*2 - half, ly + td)],
                     fill=(255,255,255,200), width=lw)
        ai_draw.line([(W*2 + half, ly), (W*2 + half, ly + td)],
                     fill=(255,255,255,200), width=lw)
        if major:
            lbl = str(abs(deg))
            ai_draw.text((W*2 - half - 40, ly - 10),
                         lbl, fill=(255,255,255,230), font=fn18)
            ai_draw.text((W*2 + half + 8,  ly - 10),
                         lbl, fill=(255,255,255,230), font=fn18)

    # Rotate and crop to display size
    ai_rot  = ai_img.rotate(-roll, center=(W*2, H*2))
    ai_crop = ai_rot.crop((W*2 - CX, H*2 - CY,
                           W*2 + (W - CX), H*2 + (H - CY)))
    img.paste(ai_crop.convert('RGB'), (0, 0))
    draw = ImageDraw.Draw(img)

    # ── 2. SEMI-TRANSPARENT TAPE OVERLAYS ────────────────────────────────────
    ov = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(ov)

    TAPE_FILL = (0, 8, 22, 185)

    # Speed tape bg (extends to y=0 for top button strip)
    od.rectangle([(SPD_X, 0), (SPD_X+SPD_W, TAPE_BOT)], fill=TAPE_FILL)
    od.line([(SPD_X+SPD_W, TAPE_TOP), (SPD_X+SPD_W, TAPE_BOT)],
            fill=(255,255,255,50), width=1)

    # Alt tape bg (includes top selected-alt strip)
    od.rectangle([(ALT_X, 0), (ALT_X+ALT_W, TAPE_BOT)], fill=TAPE_FILL)
    od.line([(ALT_X, TAPE_TOP), (ALT_X, TAPE_BOT)],
            fill=(255,255,255,50), width=1)

    # Heading tape bg
    od.rectangle([(0, HDG_Y), (W, H)], fill=(0, 8, 22, 210))
    od.line([(0, HDG_Y), (W, HDG_Y)], fill=(255,255,255,70), width=1)

    img = Image.alpha_composite(img.convert('RGBA'), ov).convert('RGB')
    draw = ImageDraw.Draw(img)

    def cyan_box(label, value_str, bx, by, bw=84, bh=20):
        draw.rectangle([(bx, by), (bx+bw, by+bh)], fill=(0, 20, 35))
        draw.rectangle([(bx, by), (bx+bw, by+bh)], outline=CYAN, width=1)
        txt = f"{label} {value_str}"
        draw.text((bx + bw//2 - len(txt)*4, by + 4), txt,
                  fill=CYAN, font=fnt(13, bold=True))

    # ── 3. SPEED TAPE CONTENT ─────────────────────────────────────────────────
    def spd_y(v): return int(TAPE_MID - (v - speed) * PX_PER_KT)

    # V-speed colour bands (right edge)
    def band(v_lo, v_hi, col, bx, bw):
        y1 = max(TAPE_TOP, min(TAPE_BOT, spd_y(v_hi)))
        y2 = max(TAPE_TOP, min(TAPE_BOT, spd_y(v_lo)))
        if y1 < y2:
            draw.rectangle([(bx, y1), (bx+bw, y2)], fill=col)

    band(VS0, VFE, WHITE,      SPD_X+SPD_W-10, 3)   # white flap arc
    band(VS1, VNO, GREEN_ARC,  SPD_X+SPD_W-5,  4)   # green normal
    band(VNO, VNE, YELLOW_ARC, SPD_X+SPD_W-5,  4)   # yellow caution
    vne_y = spd_y(VNE)
    if TAPE_TOP < vne_y < TAPE_BOT:
        draw.line([(SPD_X+SPD_W-16, vne_y), (SPD_X+SPD_W, vne_y)],
                  fill=RED, width=3)

    # Tick marks + labels
    base_v = round(speed / 20) * 20
    for v in range(base_v - 100, base_v + 100, 10):
        if v < 0: continue
        vy = spd_y(v)
        if not (TAPE_TOP + 15 < vy < TAPE_BOT - 15): continue
        major = (v % 20 == 0)
        tl = 12 if major else 7
        draw.line([(SPD_X, vy), (SPD_X+tl, vy)],
                  fill=LTGREY, width=2 if major else 1)
        if major:
            draw.text((SPD_X+tl+2, vy-9), str(v), fill=(230,230,230), font=fnt(17, bold=True))

    # Speed box — concave notch on outer (left/screen-edge) side
    bh = 44; by = TAPE_MID - bh//2
    pts = [(SPD_X+SPD_W, by), (SPD_X, by),
           (SPD_X+10, TAPE_MID),                 # notch on outer edge
           (SPD_X, by+bh), (SPD_X+SPD_W, by+bh)]
    draw.polygon(pts, fill=(0, 10, 30))
    draw.line(pts + [pts[0]], fill=WHITE, width=2)
    spd_col = RED if speed > VNE else (YELLOW if speed > VNO else WHITE)
    rolling_drum(img, SPD_X+3, TAPE_MID-17, 64, 34, speed, 3, spd_col, 26)

    # IAS bug button — top strip of speed tape
    cyan_box("IAS", "---", SPD_X, 2, bw=SPD_W, bh=18)

    # ── 4. ALT TAPE CONTENT ───────────────────────────────────────────────────
    def alt_y(ft): return int(TAPE_MID - (ft - alt) * PX_PER_FT)

    # ALT bug button — top strip of alt tape
    cyan_box("ALT", f"{round(alt_bug):5d}", ALT_X, 2, bw=ALT_W, bh=18)

    # Tick marks + labels
    base_a = round(alt / 100) * 100
    for ft in range(base_a - 400, base_a + 400, 100):
        fy = alt_y(ft)
        if not (TAPE_TOP + 12 < fy < TAPE_BOT - 12): continue
        major = (ft % 500 == 0)
        tl = 12 if major else 7
        draw.line([(ALT_X+ALT_W-tl, fy), (ALT_X+ALT_W, fy)],
                  fill=LTGREY, width=2 if major else 1)
        if ft % 200 == 0:
            f15 = fnt(15, bold=True)
            tw = int(f15.getlength(str(ft)))
            draw.text((ALT_X+ALT_W-tl-2-tw, fy-8), str(ft),
                      fill=(230,230,230), font=f15)

    # Altitude bug — on outer (right/screen-edge) side
    aby = alt_y(alt_bug)
    if TAPE_TOP < aby < TAPE_BOT:
        bug = [(ALT_X+ALT_W, aby-10), (ALT_X+ALT_W-20, aby-10),
               (ALT_X+ALT_W-26, aby), (ALT_X+ALT_W-20, aby+10), (ALT_X+ALT_W, aby+10)]
        draw.polygon(bug, fill=CYAN)

    # Alt box — concave notch on outer (right/screen-edge) side
    bh = 44; by = TAPE_MID - bh//2
    pts = [(ALT_X, by), (ALT_X+ALT_W, by),
           (ALT_X+ALT_W-10, TAPE_MID),           # notch on outer edge
           (ALT_X+ALT_W, by+bh), (ALT_X, by+bh)]
    draw.polygon(pts, fill=(0, 10, 30))
    draw.line(pts + [pts[0]], fill=WHITE, width=2)
    # Rolling drum: 5 digits, no leading zeros
    rolling_drum(img, ALT_X+2, TAPE_MID-17, 70, 34, alt, 5, WHITE, 20,
                 suppress_leading=True)

    # VSI
    arrow = "\u25b2" if vspeed > 30 else ("\u25bc" if vspeed < -30 else "\u2014")
    vcol  = (0,220,0) if vspeed > 50 else ((255,140,0) if vspeed < -50 else LTGREY)
    draw.text((ALT_X+4, TAPE_MID+20),
              f"{arrow}{abs(round(vspeed/10)*10):4d}", fill=vcol, font=fnt(13))
    draw.text((ALT_X+18, TAPE_MID+36), "fpm", fill=(120,160,200), font=fnt(10))

    # Baro
    baro_str = "GPS ALT" if not baro_ok else "1013.25"
    baro_col = CYAN if baro_ok else (180, 180, 100)
    draw.text((ALT_X+4, TAPE_MID+52), baro_str, fill=baro_col, font=fnt(11))
    if baro_ok:
        draw.text((ALT_X+14, TAPE_MID+65), "hPa", fill=(100,160,200), font=fnt(10))

    # (ALT FT label replaced by ALT bug button at top)

    # ── 5. HEADING TAPE CONTENT ───────────────────────────────────────────────
    CARDS = {0:'N',45:'NE',90:'E',135:'SE',180:'S',225:'SW',270:'W',315:'NW'}
    for i in range(-35, 36):
        deg = int((round(hdg) + i + 3600)) % 360
        off = i - (hdg - round(hdg))
        x = int(CX + off * PX_PER_DEG)
        if not (0 < x < W): continue
        if deg % 5 == 0:
            th = int(HDG_H * (0.35 if deg % 10 == 0 else 0.18))
            draw.line([(x, HDG_Y), (x, HDG_Y+th)],
                      fill=(200,200,200), width=2 if deg%10==0 else 1)
        if deg % 10 == 0:
            lbl = CARDS.get(deg, f"{deg:03d}")
            col = YELLOW if deg in CARDS else (230,230,230)
            draw.text((x-7, HDG_Y+HDG_H-15), lbl, fill=col, font=fnt(13))

    # Heading bug
    hb_off = ((hdg_bug - hdg + 180) % 360) - 180
    hb_x   = int(CX + hb_off * PX_PER_DEG)
    if 0 < hb_x < W:
        bug = [(hb_x-8, HDG_Y), (hb_x+8, HDG_Y), (hb_x+8, HDG_Y+10),
               (hb_x+4, HDG_Y+18), (hb_x-4, HDG_Y+18), (hb_x-8, HDG_Y+10)]
        draw.polygon(bug, fill=CYAN)

    # Heading box
    bw2, bh2 = 58, 22
    bx = CX - bw2//2; by2 = HDG_Y - bh2 - 2
    draw.rectangle([(bx, by2), (bx+bw2, by2+bh2)], fill=(0,0,0))
    draw.rectangle([(bx, by2), (bx+bw2, by2+bh2)], outline=WHITE, width=1)
    draw.text((CX-22, by2+2), f"{round(hdg)%360:03d}\u00b0",
              fill=WHITE, font=fnt(18))

    # Triangle pointer above heading box
    draw.polygon([(CX-7, HDG_Y-1), (CX+7, HDG_Y-1), (CX, HDG_Y-11)], fill=YELLOW)

    # ── 6. ROLL ARC (rendered at 2× for anti-aliasing) ───────────────────────
    # Drawing at 2× then scaling down via LANCZOS gives smooth arc and shapes.
    ARC_S = 2
    arc_img = Image.new('RGBA', (W * ARC_S, H * ARC_S), (0, 0, 0, 0))
    arc_d   = ImageDraw.Draw(arc_img)

    def a(v): return int(round(v * ARC_S))
    acx, acy = a(CX), a(ROLL_CY)

    # Arc from -60° to +60° bank (screen angles -150° to -30°)
    for ang in range(-150, -29):
        a1, a2 = ang * DEG, (ang + 1) * DEG
        arc_d.line([
            (acx + int(a(ROLL_R) * math.cos(a1)), acy + int(a(ROLL_R) * math.sin(a1))),
            (acx + int(a(ROLL_R) * math.cos(a2)), acy + int(a(ROLL_R) * math.sin(a2))),
        ], fill=LTGREY + (255,), width=a(2))

    # Tick marks
    for deg2, l2 in [(0, 18), (10, 10), (20, 10), (30, 14),
                     (-10, 10), (-20, 10), (-30, 14),
                     (45, 10), (-45, 10), (60, 12), (-60, 12)]:
        ang2 = (-90 + deg2) * DEG
        arc_d.line([
            (acx + int((a(ROLL_R) - a(l2)) * math.cos(ang2)),
             acy + int((a(ROLL_R) - a(l2)) * math.sin(ang2))),
            (acx + int( a(ROLL_R)          * math.cos(ang2)),
             acy + int( a(ROLL_R)          * math.sin(ang2))),
        ], fill=LTGREY + (255,), width=a(2) if deg2 == 0 else a(1))

    # Pentagon doghouse helper at 2× scale
    def doghouse_pts_2x(ang_rad, r, size=11):
        ox = math.cos(ang_rad); oy = math.sin(ang_rad)
        px = -oy;               py =  ox
        br = r + size * 1.3;  rr = r + size * 0.6
        hw = size * 0.7;      rh = size * 0.35
        return [
            (int(acx + a(br)*ox - a(hw)*px), int(acy + a(br)*oy - a(hw)*py)),
            (int(acx + a(rr)*ox - a(rh)*px), int(acy + a(rr)*oy - a(rh)*py)),
            (int(acx + a(r)*ox),              int(acy + a(r)*oy)),
            (int(acx + a(rr)*ox + a(rh)*px), int(acy + a(rr)*oy + a(rh)*py)),
            (int(acx + a(br)*ox + a(hw)*px), int(acy + a(br)*oy + a(hw)*py)),
        ]

    # Fixed zero-bank marker: downward white triangle above arc at 12 o'clock
    top_x = acx + int(a(ROLL_R) * math.cos(-math.pi/2))
    top_y = acy + int(a(ROLL_R) * math.sin(-math.pi/2))
    tri0 = [(top_x - a(8), top_y - a(13)),
            (top_x + a(8), top_y - a(13)),
            (top_x, top_y)]
    arc_d.polygon(tri0, fill=WHITE + (255,))

    # Moving roll pointer: doghouse INSIDE arc, tip pointing outward
    def doghouse_pts_inside(ang_rad, r, size=10):
        ox = math.cos(ang_rad); oy = math.sin(ang_rad)
        px = -oy;               py =  ox
        # inward=False: tip at r (outer), base at r - size*1.3 (inner)
        tr = r;           br = r - size * 1.3;  rr = r - size * 0.6
        hw = size * 0.7;  rh = size * 0.35
        return [
            (int(acx + a(br)*ox - a(hw)*px), int(acy + a(br)*oy - a(hw)*py)),
            (int(acx + a(rr)*ox - a(rh)*px), int(acy + a(rr)*oy - a(rh)*py)),
            (int(acx + a(tr)*ox),             int(acy + a(tr)*oy)),
            (int(acx + a(rr)*ox + a(rh)*px), int(acy + a(rr)*oy + a(rh)*py)),
            (int(acx + a(br)*ox + a(hw)*px), int(acy + a(br)*oy + a(hw)*py)),
        ]

    dh1 = doghouse_pts_inside((-90 - roll) * DEG, ROLL_R - 8, size=10)
    arc_d.polygon(dh1, fill=WHITE + (255,))

    # Scale down with Lanczos (anti-aliasing via supersampling)
    arc_1x = arc_img.resize((W, H), Image.LANCZOS)
    img = Image.alpha_composite(img.convert('RGBA'), arc_1x).convert('RGB')
    draw = ImageDraw.Draw(img)

    # ── 7. AIRCRAFT SYMBOL — GI-275 style outward triangles + centre ring ────
    AMBER = (255, 190, 30)
    AMBER_DARK = (180, 120, 0)
    ws = 62; gap = 10; h = 5
    droop = int((ws - gap) * math.tan(20 * DEG))   # ~19 px dihedral droop at tip
    # Left wing — upper half lighter, lower half darker (depth shading)
    draw.polygon([(CX-ws, CY-h+droop), (CX-ws, CY+droop),    (CX-gap, CY)], fill=AMBER)
    draw.polygon([(CX-ws, CY+droop),   (CX-ws, CY+h+droop),  (CX-gap, CY)], fill=AMBER_DARK)
    # Right wing — mirror
    draw.polygon([(CX+ws, CY-h+droop), (CX+ws, CY+droop),    (CX+gap, CY)], fill=AMBER)
    draw.polygon([(CX+ws, CY+droop),   (CX+ws, CY+h+droop),  (CX+gap, CY)], fill=AMBER_DARK)
    # Centre ring
    draw.ellipse([(CX-6, CY-6),(CX+6, CY+6)], outline=AMBER, width=2)

    # ── 8. CYAN TAP-BUTTONS ──────────────────────────────────────────────────
    # IAS and ALT bug buttons sit at the TOP of their respective tapes (drawn
    # above in sections 3 and 4).  HDG and QNH sit at the BASE of the heading
    # strip, left and right — keeping the centre clear for the heading readout.
    btn_y = HDG_Y + 2
    cyan_box("HDG", f"{round(hdg_bug):03d}\u00b0", SPD_X,    btn_y, bw=SPD_W+10)   # left
    cyan_box("QNH", "GPS ALT",                      ALT_X-10, btn_y, bw=ALT_W+10)  # right

    # ── 9. SLIP INDICATOR (rectangular, GI-275 style) ────────────────────────
    bw3=54; bh3=10
    bx3=CX-bw3//2; by3=BALL_Y-bh3//2
    draw.rectangle([(bx3, by3), (bx3+bw3, by3+bh3)],
                   fill=(15,15,25), outline=(100,100,110))
    draw.line([(CX, by3+2),(CX, by3+bh3-2)], fill=(160,160,170), width=1)
    pw = 10; max_d = bw3//2 - pw//2 - 2
    defl = int(max(-max_d, min(max_d, (ay/0.2)*max_d)))
    draw.rectangle([(CX+defl-pw//2, by3+1),(CX+defl+pw//2, by3+bh3-1)],
                   fill=WHITE)

    # ── 10. STATUS BADGES (top-right, AHRS / BARO / GPS / LINK) ──────────────
    bx_r = W - 4
    def badge(text, bg, fg=WHITE):
        nonlocal bx_r
        tw = len(text)*6 + 10
        bx_r -= tw + 2
        draw.rectangle([(bx_r, 4), (bx_r+tw, 19)], fill=bg)
        draw.text((bx_r+5, 5), text, fill=fg, font=fnt(10))

    badge("LINK",            (0, 130, 0))
    badge(f"GPS {sats}sat" if gps_ok else "NO GPS",
          (0, 150, 0) if gps_ok else (100, 100, 0))
    badge("BARO" if baro_ok else "GPS ALT",
          (0, 80, 120) if baro_ok else (80, 80, 0),
          WHITE if baro_ok else (220, 220, 100))
    badge("AHRS" if ahrs_ok else "AHRS FAIL",
          (0, 100, 80) if ahrs_ok else (150, 0, 0))

    # ── 11. SCENARIO LABEL ────────────────────────────────────────────────────
    lw = len(label)*6 + 16
    draw.rectangle([(CX-lw//2, HDG_Y-22),(CX+lw//2, HDG_Y-4)],
                   fill=(0,0,0))
    draw.text((CX-lw//2+8, HDG_Y-20), label, fill=(255,210,60), font=fnt(10))

    img.save(filename)
    print(f"Saved {filename}")


# ── Render 3 Sedona scenarios ─────────────────────────────────────────────────
OUT = os.path.dirname(os.path.abspath(__file__))

draw_scene(
    roll=0, pitch=2, hdg=133, alt=8500, speed=115,
    vspeed=0, ay=0.0, hdg_bug=133, alt_bug=8500,
    label="Sedona Valley — Level cruise SE at 8,500 ft",
    filename=os.path.join(OUT, "preview_sedona_level.png"),
    ahrs_ok=True, gps_ok=True, baro_ok=False, sats=8,
)

draw_scene(
    roll=-18, pitch=4, hdg=218, alt=7200, speed=108,
    vspeed=650, ay=-0.08, hdg_bug=250, alt_bug=9500,
    label="Sedona — Climbing left turn, departing NW",
    filename=os.path.join(OUT, "preview_sedona_climb_turn.png"),
    ahrs_ok=True, gps_ok=True, baro_ok=False, sats=8,
)

draw_scene(
    roll=0, pitch=-3, hdg=19, alt=6200, speed=90,
    vspeed=-500, ay=0.0, hdg_bug=19, alt_bug=4900,
    label="Sedona (KSEZ) — Descending final Rwy 03",
    filename=os.path.join(OUT, "preview_sedona_approach.png"),
    ahrs_ok=True, gps_ok=True, baro_ok=False, sats=8,
)

print("Done.")
