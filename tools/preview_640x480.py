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
                 suppress_leading=False, power_offset=0):
    """
    Veeder-Root style rolling-drum digit readout.
    Cascading: every digit carries smoothly when the digit below it approaches 9→0.
    power_offset: the power of the LOWEST digit rendered (0=units, 1=tens, etc.)
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
        power = power_offset + n_digits - 1 - col_i   # absolute decimal power of this column
        if suppress_leading and power > 0 and val_int < 10 ** power:
            continue

        if power == 0:
            d_cont = float(value % 10.0)
        else:
            # Cascade: scroll when the complete lower-order portion approaches rollover
            lower_cont = (value % (10 ** power)) / (10 ** (power - 1))
            carry_frac = max(0.0, lower_cont - 9.0)   # 0 normally, 0→1 near rollover
            d_lo   = (int(value) // (10 ** power)) % 10
            d_cont = float(d_lo) + carry_frac

        d_lo  = int(d_cont)
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


def _drum_shade(img, bx, by, bw, bh):
    """Overlay a top-and-bottom fade-to-dark gradient on the drum window."""
    shade = Image.new('RGBA', (bw, bh), (0, 0, 0, 0))
    sd    = ImageDraw.Draw(shade)
    fade  = bh // 3
    for i in range(fade):
        a = int(210 * (fade - i) / fade)
        sd.line([(0, i),       (bw-1, i)],       fill=(0, 5, 15, a))
        sd.line([(0, bh-1-i),  (bw-1, bh-1-i)],  fill=(0, 5, 15, a))
    bg     = img.convert('RGBA').crop((bx, by, bx+bw, by+bh))
    shaded = Image.alpha_composite(bg, shade)
    img.paste(shaded.convert('RGB'), (bx, by))


def rolling_drum_alt20(img, bx, by, bw, bh, alt, color, font_sz):
    """
    Altimeter Veeder-Root drum: rolls in 20-foot steps.
    Left sub-column scrolls the tens digit (0→2→4→6→8→0).
    Right sub-column shows a fixed '0' (units always zero in 20 ft resolution).
    """
    f = fnt(font_sz, bold=True)
    try:
        ref_bbox = f.getbbox("0")
        ch  = ref_bbox[3] - ref_bbox[1]
        cw  = ref_bbox[2] - ref_bbox[0]
    except Exception:
        ch = font_sz; cw = font_sz // 2
    col_rgba = color + (255,)

    # 5 drum positions per 100 ft (one step every 20 ft)
    drum_pos = (alt % 100) / 20         # 0.0 – 5.0
    d_lo_idx = int(drum_pos) % 5        # 0–4
    frac     = drum_pos - int(drum_pos)
    d_lo_dig = d_lo_idx * 2             # 0, 2, 4, 6, 8
    d_hi_dig = ((d_lo_idx + 1) % 5) * 2  # next even digit (8 wraps → 0)

    # ── Left sub-column: rolling tens digit ──
    lcw    = bw - cw - 1               # width for rolling col, leaving room for fixed '0'
    scroll = int(frac * bh)
    tx_l   = max(0, (lcw - cw) // 2)
    ty_lo  = (bh - ch) // 2 - scroll
    ty_hi  = ty_lo + bh
    cell_l = Image.new('RGBA', (lcw, bh * 2), (0, 0, 0, 0))
    cd     = ImageDraw.Draw(cell_l)
    cd.text((tx_l, ty_lo), str(d_lo_dig), fill=col_rgba, font=f)
    cd.text((tx_l, ty_hi), str(d_hi_dig), fill=col_rgba, font=f)
    cropped_l = cell_l.crop((0, 0, lcw, bh))
    img.paste(cropped_l, (bx, by), cropped_l)

    # ── Right sub-column: fixed units '0' ──
    rcw   = bw - lcw
    tx_r  = max(0, (rcw - cw) // 2)
    ty_r  = (bh - ch) // 2
    cell_r = Image.new('RGBA', (rcw, bh), (0, 0, 0, 0))
    cd_r  = ImageDraw.Draw(cell_r)
    cd_r.text((tx_r, ty_r), "0", fill=col_rgba, font=f)
    img.paste(cell_r, (bx + lcw, by), cell_r)


def draw_scene(roll, pitch, hdg, alt, speed, vspeed, ay,
               hdg_bug, alt_bug, label, filename,
               gs_bug=None, ahrs_ok=True, gps_ok=True, baro_ok=False, sats=8):

    img  = Image.new('RGB', (W, H), (0, 0, 0))
    draw = ImageDraw.Draw(img)

    # ── 1. SVT SKY / GROUND background (rotated with roll) ───────────────────
    # Render at 4× size for rotation quality then crop
    S = 4
    ai_img  = Image.new('RGBA', (W*S, H*S), (0, 0, 0, 0))
    ai_draw = ImageDraw.Draw(ai_img)

    focal    = 520.0
    pitch_px = focal * math.tan(pitch * DEG)
    hy = H*2 + int(pitch_px)    # horizon row in enlarged canvas (centred at H*2)

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
        if ly < H*2 - CY + 44:    # don't draw above roll arc area (display y < 44)
            continue
        if ly > H*2 - CY + HDG_Y - 22:  # don't draw below heading tape (display y > 414)
            continue
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

    # GS bug — before speed box so box draws on top (bug goes behind readout)
    if gs_bug is not None:
        gby = spd_y(gs_bug)
        if TAPE_TOP < gby < TAPE_BOT:
            gb = [(SPD_X,    gby-17),
                  (SPD_X+14, gby-17), (SPD_X+14, gby-5), (SPD_X+7, gby),
                  (SPD_X+14, gby+5),  (SPD_X+14, gby+17), (SPD_X, gby+17)]
            draw.polygon(gb, fill=CYAN)

    # Speed readout box — stepped Veeder-Root style (from SVG spec)
    # Layout: pointer(15) → inner section(32) → drum section(19) = 66px total
    # Inner: ±15 from TAPE_MID;  Drum: ±29 from TAPE_MID (taller window)
    pts_s = [(SPD_X,    TAPE_MID),
             (SPD_X+15, TAPE_MID-15), (SPD_X+47, TAPE_MID-15),
             (SPD_X+47, TAPE_MID-29), (SPD_X+66, TAPE_MID-29),
             (SPD_X+66, TAPE_MID+29),
             (SPD_X+47, TAPE_MID+29), (SPD_X+47, TAPE_MID+15),
             (SPD_X+15, TAPE_MID+15)]
    draw.polygon(pts_s, fill=(0, 10, 30))
    draw.line(pts_s + [pts_s[0]], fill=WHITE, width=2)
    spd_col = RED if speed > VNE else (YELLOW if speed > VNO else WHITE)
    # Inner section: hundreds + tens, cascade-rolling, clipped to 30px window
    rolling_drum(img, SPD_X+16, TAPE_MID-14, 30, 28, speed, 2, spd_col, 16, power_offset=1)
    # Drum section: units digit, full 58px window (shows incoming/outgoing digit)
    rolling_drum(img, SPD_X+48, TAPE_MID-28, 17, 56, speed, 1, spd_col, 22)
    _drum_shade(img,   SPD_X+47, TAPE_MID-29, 19, 58)

    # GS bug button — top strip of speed tape
    gs_str = f"{round(gs_bug):3d}" if gs_bug is not None else "---"
    cyan_box("GS", gs_str, SPD_X, 2, bw=SPD_W, bh=18)

    # ── 4. ALT TAPE CONTENT ───────────────────────────────────────────────────
    def alt_y(ft): return int(TAPE_MID - (ft - alt) * PX_PER_FT)

    # ALT bug button — top strip of alt tape
    cyan_box("ALT", f"{round(alt_bug):5d}", ALT_X, 2, bw=ALT_W, bh=18)

    # Tick marks + labels — every 50ft minor, every 100ft major with label
    base_a = round(alt / 50) * 50
    for ft in range(base_a - 450, base_a + 450, 50):
        fy = alt_y(ft)
        if not (TAPE_TOP + 8 < fy < TAPE_BOT - 8): continue
        major = (ft % 100 == 0)
        tl = 12 if major else 7
        draw.line([(ALT_X+ALT_W-tl, fy), (ALT_X+ALT_W, fy)],
                  fill=LTGREY, width=2 if major else 1)
        if major:
            s = str(ft)
            if ft >= 1000:
                # Thousands digit slightly larger than hundreds
                f_l = fnt(16, bold=True);  f_s = fnt(13, bold=True)
                thou = s[:1];  rest = s[1:]
                tw_l = int(f_l.getlength(thou));  tw_s = int(f_s.getlength(rest))
                x0 = ALT_X + ALT_W - tl - 2 - tw_l - tw_s
                draw.text((x0,        fy - 10), thou, fill=(230, 230, 230), font=f_l)
                draw.text((x0+tw_l,   fy -  8), rest, fill=(230, 230, 230), font=f_s)
            else:
                f_s = fnt(13, bold=True)
                tw = int(f_s.getlength(s))
                draw.text((ALT_X+ALT_W-tl-2-tw, fy-8), s, fill=(230,230,230), font=f_s)

    # Alt bug — before alt box so box draws on top (bug goes behind readout)
    aby = alt_y(alt_bug)
    if TAPE_TOP < aby < TAPE_BOT:
        bug = [(ALT_X+ALT_W,    aby-17),
               (ALT_X+ALT_W-14, aby-17), (ALT_X+ALT_W-14, aby-5), (ALT_X+ALT_W-7, aby),
               (ALT_X+ALT_W-14, aby+5),  (ALT_X+ALT_W-14, aby+17), (ALT_X+ALT_W, aby+17)]
        draw.polygon(bug, fill=CYAN)

    # Altitude readout box — stepped Veeder-Root style (from SVG spec)
    # Layout: inner section(42) → drum section(24) → pointer(15) = 81px total
    # Inner: ±15;  Drum: ±29  (pointer on the RIGHT, inner on the LEFT)
    R = ALT_X + ALT_W   # right edge = 640
    pts_a = [(R,    TAPE_MID),
             (R-15, TAPE_MID-15), (R-15, TAPE_MID-29),
             (R-39, TAPE_MID-29), (R-39, TAPE_MID-15),
             (R-81, TAPE_MID-15),
             (R-81, TAPE_MID+15),
             (R-39, TAPE_MID+15), (R-39, TAPE_MID+29),
             (R-15, TAPE_MID+29), (R-15, TAPE_MID+15)]
    draw.polygon(pts_a, fill=(0, 10, 30))
    draw.line(pts_a + [pts_a[0]], fill=WHITE, width=2)
    # Inner section: alt//100 as 3-digit cascade; carry starts when drum hits its last
    # 20-ft step (drum_pos > 4.0, i.e. alt%100 > 80 ft) so all digits roll together.
    carry_frac = max(0.0, (alt % 100) / 20 - 4.0)
    alt_inner  = float(alt // 100) + carry_frac
    rolling_drum(img, R-80, TAPE_MID-14, 40, 28, alt_inner, 3, WHITE, 14,
                 suppress_leading=True)
    # Drum section: 20 ft steps — tens digit scrolls 0→2→4→6→8→0, units fixed '0'
    rolling_drum_alt20(img, R-38, TAPE_MID-28, 22, 56, alt, WHITE, 16)
    _drum_shade(img,   R-39, TAPE_MID-29, 24, 58)

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

    # Heading bug — wide, short, V-notch at top matching speed/alt bug style
    hb_off = ((hdg_bug - hdg + 180) % 360) - 180
    hb_x   = int(CX + hb_off * PX_PER_DEG)
    if 0 < hb_x < W:
        bug = [(hb_x-17, HDG_Y+14), (hb_x-17, HDG_Y),
               (hb_x-5,  HDG_Y), (hb_x, HDG_Y+7), (hb_x+5, HDG_Y),
               (hb_x+17, HDG_Y), (hb_x+17, HDG_Y+14)]
        draw.polygon(bug, fill=CYAN)

    # Heading box — rectangle with small centered triangle tab on bottom
    # Tab is 1/3 of box width, starts 1/3 of the way along the bottom edge
    bw2, bh2 = 58, 22
    bx = CX - bw2//2; by2 = HDG_Y - bh2 - 2
    th = bw2 // 3          # triangle base width ≈ 19px
    td = bh2 // 2          # triangle depth = 11px
    tx = CX - th // 2      # triangle left base x
    pts_h = [(bx,       by2),
             (bx+bw2,   by2),
             (bx+bw2,   by2+bh2),
             (tx+th,    by2+bh2),
             (CX,       by2+bh2+td),
             (tx,       by2+bh2),
             (bx,       by2+bh2)]
    draw.polygon(pts_h, fill=(0, 0, 0))
    draw.line(pts_h + [pts_h[0]], fill=WHITE, width=2)
    draw.text((CX-22, by2+2), f"{round(hdg)%360:03d}\u00b0",
              fill=WHITE, font=fnt(18))

    # ── 6. ROLL ARC (rendered at 2× for anti-aliasing) ───────────────────────
    # Drawing at 2× then scaling down via LANCZOS gives smooth arc and shapes.
    ARC_S = 2
    arc_img = Image.new('RGBA', (W * ARC_S, H * ARC_S), (0, 0, 0, 0))
    arc_d   = ImageDraw.Draw(arc_img)

    def a(v): return int(round(v * ARC_S))
    acx, acy = a(CX), a(ROLL_CY)

    # Arc from -60° to +60° bank (screen angles -150° to -30°), rotates with roll
    for ang in range(-150, -29):
        a1, a2 = (ang + roll) * DEG, (ang + 1 + roll) * DEG
        arc_d.line([
            (acx + int(a(ROLL_R) * math.cos(a1)), acy + int(a(ROLL_R) * math.sin(a1))),
            (acx + int(a(ROLL_R) * math.cos(a2)), acy + int(a(ROLL_R) * math.sin(a2))),
        ], fill=LTGREY + (255,), width=a(2))

    # Tick marks
    for deg2, l2 in [(0, 18), (10, 10), (20, 10), (30, 14),
                     (-10, 10), (-20, 10), (-30, 14),
                     (45, 10), (-45, 10), (60, 12), (-60, 12)]:
        ang2 = (-90 + deg2 + roll) * DEG
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

    # Moving upper doghouse — OUTSIDE arc, tip at arc, moves with roll arc
    tri0 = doghouse_pts_2x((-90 + roll) * DEG, ROLL_R, size=10)
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

    # Fixed lower doghouse — INSIDE arc, tip at arc-8, fixed at 12 o'clock
    dh1 = doghouse_pts_inside(-math.pi/2, ROLL_R - 8, size=10)
    arc_d.polygon(dh1, fill=WHITE + (255,))

    # Scale down with Lanczos (anti-aliasing via supersampling)
    arc_1x = arc_img.resize((W, H), Image.LANCZOS)
    img = Image.alpha_composite(img.convert('RGBA'), arc_1x).convert('RGB')
    draw = ImageDraw.Draw(img)

    # ── 7. AIRCRAFT SYMBOL — swept delta wings + engine nacelles (1.5× scale) ──
    AMBER = (255, 190, 30)
    AMBER_DARK = (180, 120, 0)
    # Wing panels — apex at (CX, CY), trailing edge at CY+44 (1.5× original 29)
    # Outer strip = leading-edge side (lighter/top); Inner strip = trailing-edge side (darker/bottom)
    BLK = (0, 0, 0)
    # Fills — no outline so the inner colour-split edge stays clean
    draw.polygon([(CX, CY), (CX-81, CY+44), (CX-69, CY+44)], fill=AMBER_DARK)  # L inner
    draw.polygon([(CX, CY), (CX-93, CY+44), (CX-81, CY+44)], fill=AMBER)       # L outer
    draw.polygon([(CX, CY), (CX+69, CY+44), (CX+81, CY+44)], fill=AMBER_DARK)  # R inner
    draw.polygon([(CX, CY), (CX+81, CY+44), (CX+93, CY+44)], fill=AMBER)       # R outer
    # Engine nacelles — fills
    draw.polygon([(CX-93, CY), (CX-99, CY-6), (CX-138, CY-6), (CX-138, CY)],   fill=AMBER)       # L upper
    draw.polygon([(CX-93, CY), (CX-138, CY),  (CX-138, CY+6), (CX-99, CY+6)],  fill=AMBER_DARK)  # L lower
    draw.polygon([(CX+93, CY), (CX+99, CY-6), (CX+138, CY-6), (CX+138, CY)],   fill=AMBER)       # R upper
    draw.polygon([(CX+93, CY), (CX+138, CY),  (CX+138, CY+6), (CX+99, CY+6)],  fill=AMBER_DARK)  # R lower
    # Outer perimeter outlines only (no line across the inner colour split)
    draw.polygon([(CX, CY), (CX-93, CY+44), (CX-69, CY+44)], outline=BLK)  # L wing
    draw.polygon([(CX, CY), (CX+69, CY+44), (CX+93, CY+44)], outline=BLK)  # R wing
    draw.polygon([(CX-93, CY), (CX-99, CY-6), (CX-138, CY-6), (CX-138, CY+6), (CX-99, CY+6)], outline=BLK)  # L nacelle
    draw.polygon([(CX+93, CY), (CX+99, CY-6), (CX+138, CY-6), (CX+138, CY+6), (CX+99, CY+6)], outline=BLK)  # R nacelle

    # ── 8. CYAN TAP-BUTTONS ──────────────────────────────────────────────────
    # IAS and ALT bug buttons sit at the TOP of their respective tapes (drawn
    # above in sections 3 and 4).  HDG and QNH sit at the BASE of the heading
    # strip, left and right — keeping the centre clear for the heading readout.
    btn_y = HDG_Y + 2
    cyan_box("HDG", f"{round(hdg_bug):03d}\u00b0", SPD_X,    btn_y, bw=SPD_W+10)   # left
    cyan_box("QNH", "GPS ALT",                      ALT_X-10, btn_y, bw=ALT_W+10)  # right

    # ── 9. SLIP INDICATOR — thin bar below zero-bank triangle pointer ─────────
    # Slides ±12 px under the fixed doghouse, same width as its base (16 px)
    slip_y = ROLL_CY - ROLL_R + 24  # below lower doghouse base (y≈40)
    max_d  = 12
    defl   = int(max(-max_d, min(max_d, (ay / 0.2) * max_d)))
    draw.rectangle([(CX + defl - 8, slip_y),
                    (CX + defl + 8, slip_y + 4)], fill=WHITE)

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
    draw.rectangle([(CX-lw//2, HDG_Y-50),(CX+lw//2, HDG_Y-32)],
                   fill=(0,0,0))
    draw.text((CX-lw//2+8, HDG_Y-48), label, fill=(255,210,60), font=fnt(10))

    img.save(filename)
    print(f"Saved {filename}")


# ── Render 3 Sedona scenarios ─────────────────────────────────────────────────
OUT = os.path.dirname(os.path.abspath(__file__))

draw_scene(
    roll=0, pitch=2, hdg=133, alt=8499.7, speed=114.7,
    vspeed=0, ay=0.0, hdg_bug=133, alt_bug=8500,
    gs_bug=115,
    label="Sedona Valley — Level cruise SE at 8,500 ft",
    filename=os.path.join(OUT, "preview_sedona_level.png"),
    ahrs_ok=True, gps_ok=True, baro_ok=False, sats=8,
)

# Cascade demo: all drum digits mid-transition (4999.7 ft → 5000, 119.7 kt → 120)
draw_scene(
    roll=0, pitch=0, hdg=270, alt=4999.7, speed=119.7,
    vspeed=0, ay=0.0, hdg_bug=270, alt_bug=5000,
    gs_bug=120,
    label="Veeder-Root cascade demo — all digits mid-roll",
    filename=os.path.join(OUT, "preview_vr_cascade.png"),
    ahrs_ok=True, gps_ok=True, baro_ok=False, sats=8,
)

draw_scene(
    roll=-18, pitch=4, hdg=218, alt=7200, speed=108,
    vspeed=650, ay=-0.08, hdg_bug=250, alt_bug=9500,
    gs_bug=115,
    label="Sedona — Climbing left turn, departing NW",
    filename=os.path.join(OUT, "preview_sedona_climb_turn.png"),
    ahrs_ok=True, gps_ok=True, baro_ok=False, sats=8,
)

draw_scene(
    roll=0, pitch=-3, hdg=19, alt=6200, speed=90,
    vspeed=-500, ay=0.0, hdg_bug=19, alt_bug=4900,
    gs_bug=90,
    label="Sedona (KSEZ) — Descending final Rwy 03",
    filename=os.path.join(OUT, "preview_sedona_approach.png"),
    ahrs_ok=True, gps_ok=True, baro_ok=False, sats=8,
)

print("Done.")
