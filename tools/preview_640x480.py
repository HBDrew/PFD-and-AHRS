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
PX_PER_DEG = W / 120.0

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
MAGENTA    = (220,   0, 220)

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
        sx = p[0] + dx1*r;  sy = p[1] + dy1*r   # arc start
        ex = p[0] + dx2*r;  ey = p[1] + dy2*r   # arc end
        acx = p[0] + dx1*r + dx2*r              # arc centre
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


def rolling_drum(img, bx, by, bw, bh, value, n_digits, color, font_sz,
                 suppress_leading=False, power_offset=0, show_adjacent=False,
                 adj_slot_h=None):
    """
    Veeder-Root rolling-drum digit readout.
    show_adjacent=True: adjacent digits are ~50% visible above/below (true drum look).
    Cascading: every digit carries smoothly when the digit below approaches 9→0.
    """
    char_w  = bw // n_digits
    f       = fnt(font_sz, bold=True)
    val_int = int(abs(value))
    try:
        ref_bbox  = f.getbbox("0")
        ch        = ref_bbox[3] - ref_bbox[1]
        cw_ch     = ref_bbox[2] - ref_bbox[0]
        ch_offset = ref_bbox[1]           # glyph-top offset for precise centering
    except Exception:
        ch = font_sz; cw_ch = font_sz // 2; ch_offset = 0

    col_rgba = color + (255,)
    slot_h   = ((adj_slot_h if adj_slot_h is not None else bh // 2)
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
        tx     = max(0, (char_w - cw_ch) // 2)
        cx_pos = bx + col_i * char_w

        if show_adjacent:
            d_prev  = (d_lo - 1 + 10) % 10
            ty_lo   = bh // 2 - ch // 2 - ch_offset + scroll   # reversed: lo scrolls down
            ty_prev = ty_lo + slot_h                             # prev (lower) below
            ty_hi   = ty_lo - slot_h                             # hi  (higher) above
            cell = Image.new('RGBA', (char_w, bh + 2 * slot_h), (0, 0, 0, 0))
            cd   = ImageDraw.Draw(cell)
            cd.text((tx, ty_prev + slot_h), str(d_prev), fill=col_rgba, font=f)
            cd.text((tx, ty_lo   + slot_h), str(d_lo),   fill=col_rgba, font=f)
            cd.text((tx, ty_hi   + slot_h), str(d_hi),   fill=col_rgba, font=f)
            cropped = cell.crop((0, slot_h, char_w, slot_h + bh))
        else:
            ty_lo = (bh - ch) // 2 - ch_offset + scroll   # reversed: lo scrolls down
            ty_hi = ty_lo - bh                              # hi (higher) is one slot above
            cell  = Image.new('RGBA', (char_w, bh * 2), (0, 0, 0, 0))
            cd    = ImageDraw.Draw(cell)
            cd.text((tx, ty_hi + bh), str(d_hi), fill=col_rgba, font=f)
            cd.text((tx, ty_lo + bh), str(d_lo), fill=col_rgba, font=f)
            cropped = cell.crop((0, bh, char_w, bh * 2))

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


def rolling_drum_alt20(img, bx, by, bw, bh, alt, color, font_sz, show_adjacent=False,
                       adj_slot_h=None):
    """
    Altimeter Veeder-Root drum: both digits scroll together in 20-foot steps.
    Labels '00','20','40','60','80' move as a unit so tens AND units roll together.
    """
    _LABELS = ("00", "20", "40", "60", "80")
    f = fnt(font_sz, bold=True)
    try:
        ref_bbox  = f.getbbox("00")
        ch        = ref_bbox[3] - ref_bbox[1]
        cw        = ref_bbox[2] - ref_bbox[0]
        ch_offset = ref_bbox[1]
    except Exception:
        ch = font_sz; cw = font_sz; ch_offset = 0
    col_rgba = color + (255,)
    tx = max(0, (bw - cw) // 2)

    drum_pos = (alt % 100) / 20
    d_lo_idx = int(drum_pos) % 5
    frac     = drum_pos - int(drum_pos)
    slot_h   = ((adj_slot_h if adj_slot_h is not None else bh // 2)
                if show_adjacent else bh)
    scroll   = int(frac * slot_h)

    if show_adjacent:
        d_prev_idx = (d_lo_idx - 1 + 5) % 5
        d_hi_idx   = (d_lo_idx + 1) % 5
        d_hi2_idx  = (d_lo_idx + 2) % 5
        ty_lo   = bh // 2 - ch // 2 - ch_offset + scroll   # reversed: lo scrolls down
        ty_prev = ty_lo + slot_h                             # prev (lower) below
        ty_hi   = ty_lo - slot_h                             # hi  (higher) above
        cell = Image.new('RGBA', (bw, bh + 2 * slot_h), (0, 0, 0, 0))
        cd   = ImageDraw.Draw(cell)
        cd.text((tx, ty_lo - slot_h),  _LABELS[d_hi2_idx],  fill=col_rgba, font=f)
        cd.text((tx, ty_prev + slot_h), _LABELS[d_prev_idx], fill=col_rgba, font=f)
        cd.text((tx, ty_lo   + slot_h), _LABELS[d_lo_idx],   fill=col_rgba, font=f)
        cd.text((tx, ty_hi   + slot_h), _LABELS[d_hi_idx],   fill=col_rgba, font=f)
        cropped = cell.crop((0, slot_h, bw, slot_h + bh))
    else:
        d_hi_idx = (d_lo_idx + 1) % 5
        ty_lo = (bh - ch) // 2 - ch_offset + scroll   # reversed: lo scrolls down
        ty_hi = ty_lo - bh                              # hi (higher) is one slot above
        cell  = Image.new('RGBA', (bw, bh * 2), (0, 0, 0, 0))
        cd    = ImageDraw.Draw(cell)
        cd.text((tx, ty_hi + bh), _LABELS[d_hi_idx],  fill=col_rgba, font=f)
        cd.text((tx, ty_lo + bh), _LABELS[d_lo_idx],  fill=col_rgba, font=f)
        cropped = cell.crop((0, bh, bw, 2 * bh))
    img.paste(cropped, (bx, by), cropped)


def draw_scene(roll, pitch, hdg, alt, speed, vspeed, ay,
               hdg_bug, alt_bug, label, filename,
               gs_bug=None, ahrs_ok=True, gps_ok=True, baro_ok=False, sats=8,
               baro_hpa=1013.25):

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

    def cyan_box(value_str, bx, by, bw=74, bh=22, font_sz=14):
        """Illuminated tap button: r=3 corners, 2px cyan border, top glow, no label."""
        # Background fill
        draw.rounded_rectangle([(bx, by), (bx+bw-1, by+bh-1)], radius=3,
                                fill=(0, 20, 35))
        # Top glow — simulates illuminated button face
        glow_h = max(4, bh // 3)
        for i in range(glow_h):
            t = 1.0 - i / glow_h
            r = min(255, int(t * 60))
            g = min(255, int(20 + t * 100))
            b = min(255, int(35 + t * 120))
            draw.line([(bx+2, by+1+i), (bx+bw-3, by+1+i)], fill=(r, g, b))
        # 2px cyan border (matching veeder-root outline width)
        draw.rounded_rectangle([(bx, by), (bx+bw-1, by+bh-1)], radius=3,
                                outline=CYAN, width=2)
        # Value text — centred H+V
        f = fnt(font_sz, bold=True)
        bb = draw.textbbox((0, 0), value_str, font=f)
        tw, th = bb[2]-bb[0], bb[3]-bb[1]
        draw.text((bx + (bw-tw)//2 - bb[0],
                   by + (bh-th)//2 - bb[1]),
                  value_str, fill=CYAN, font=f)

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

    # Helper: draw a filled polygon clipped to a vertical tape range.
    def _clipped_poly(pts, fill, y0=TAPE_TOP, y1=TAPE_BOT):
        _t = Image.new('RGBA', (W, H), (0, 0, 0, 0))
        _d = ImageDraw.Draw(_t)
        _d.polygon(pts, fill=fill + (255,))
        if y0 > 0:
            _d.rectangle([(0, 0), (W-1, y0-1)], fill=(0, 0, 0, 0))
        if y1 < H - 1:
            _d.rectangle([(0, y1+1), (W-1, H-1)], fill=(0, 0, 0, 0))
        img.paste(_t.convert('RGB'), (0, 0), _t.split()[3])

    # GS bug — before speed box so box draws on top (bug goes behind readout)
    # Stores half-visible at tape edge when outside visible range.
    if gs_bug is not None:
        gby = max(TAPE_TOP, min(TAPE_BOT, spd_y(gs_bug)))
        gb = [(SPD_X,    gby-17),
              (SPD_X+14, gby-17), (SPD_X+14, gby-5), (SPD_X+7, gby),
              (SPD_X+14, gby+5),  (SPD_X+14, gby+17), (SPD_X, gby+17)]
        _clipped_poly(gb, CYAN)

    # Speed readout box — stepped Veeder-Root style (from SVG spec)
    # Layout: pointer(15) → inner section(32) → drum section(19) = 66px total
    # Inner: ±15 from TAPE_MID;  Drum: ±29 from TAPE_MID (taller window)
    pts_s = _chamfer([(SPD_X,    TAPE_MID),
                      (SPD_X+15, TAPE_MID-15), (SPD_X+47, TAPE_MID-15),
                      (SPD_X+47, TAPE_MID-29), (SPD_X+66, TAPE_MID-29),
                      (SPD_X+66, TAPE_MID+29),
                      (SPD_X+47, TAPE_MID+29), (SPD_X+47, TAPE_MID+15),
                      (SPD_X+15, TAPE_MID+15)], {2, 3, 4, 5, 6, 7})
    draw.polygon(pts_s, fill=(0, 10, 30))
    draw.line(pts_s + [pts_s[0]], fill=WHITE, width=2)
    spd_col = RED if speed > VNE else (YELLOW if speed > VNO else WHITE)
    # Inner section: hundreds + tens at same font as drum, cascade-rolling
    rolling_drum(img, SPD_X+16, TAPE_MID-14, 30, 28, speed, 2, spd_col, 24, power_offset=1)
    # Drum section: units digit, adjacent digits ~50% visible
    rolling_drum(img, SPD_X+48, TAPE_MID-28, 17, 56, speed, 1, spd_col, 24,
                 show_adjacent=True, adj_slot_h=23)
    _drum_shade(img,   SPD_X+48, TAPE_MID-28, 17, 56)   # 1px inset from border

    # GS bug button — top strip of speed tape
    gs_str = f"{round(gs_bug):3d}" if gs_bug is not None else "---"
    cyan_box(gs_str, SPD_X, 2, bw=SPD_W, bh=22)

    # ── 4. ALT TAPE CONTENT ───────────────────────────────────────────────────
    def alt_y(ft): return int(TAPE_MID - (ft - alt) * PX_PER_FT)

    # ALT bug button — top strip of alt tape
    cyan_box(f"{round(alt_bug):5d}" if alt_bug is not None else "-----",
             ALT_X, 2, bw=ALT_W, bh=22)

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

    # VS bar — 5px wide on the outer (right) edge of the alt tape.
    # Visible whenever climbing/descending; covered by alt bug only when at bug altitude.
    # 2000 fpm ≡ 200 ft ≡ 200×PX_PER_FT pixels on the tape.
    _vs_scale = 200 * PX_PER_FT / 2000   # px per fpm
    _vs_px    = int(abs(vspeed) * _vs_scale)
    if abs(vspeed) > 30 and _vs_px > 0:
        if vspeed > 0:
            _vsy1 = max(TAPE_TOP, TAPE_MID - _vs_px)
            _vsy2 = TAPE_MID
        else:
            _vsy1 = TAPE_MID
            _vsy2 = min(TAPE_BOT, TAPE_MID + _vs_px)
        draw.rectangle([(ALT_X+ALT_W-5, _vsy1), (ALT_X+ALT_W, _vsy2)], fill=MAGENTA)

    # Alt bug — stores half-visible at tape edge when outside visible range.
    if alt_bug is not None:
        aby = max(TAPE_TOP, min(TAPE_BOT, alt_y(alt_bug)))
        bug = [(ALT_X+ALT_W,    aby-17),
               (ALT_X+ALT_W-14, aby-17), (ALT_X+ALT_W-14, aby-5), (ALT_X+ALT_W-7, aby),
               (ALT_X+ALT_W-14, aby+5),  (ALT_X+ALT_W-14, aby+17), (ALT_X+ALT_W, aby+17)]
        _clipped_poly(bug, CYAN)

    # Altitude readout box — stepped Veeder-Root style (from SVG spec)
    # Layout: inner section(42) → drum section(24) → pointer(15) = 81px total
    # Inner: ±15;  Drum: ±29  (pointer on the RIGHT, inner on the LEFT)
    R = ALT_X + ALT_W   # right edge = 640
    pts_a = _chamfer([(R,    TAPE_MID),
                      (R-15, TAPE_MID-15), (R-15, TAPE_MID-29),
                      (R-39, TAPE_MID-29), (R-39, TAPE_MID-15),
                      (R-81, TAPE_MID-15),
                      (R-81, TAPE_MID+15),
                      (R-39, TAPE_MID+15), (R-39, TAPE_MID+29),
                      (R-15, TAPE_MID+29), (R-15, TAPE_MID+15)], {2, 3, 4, 5, 6, 7, 8, 9})
    draw.polygon(pts_a, fill=(0, 10, 30))

    # VSI box — drawn BEFORE the outline so the 2px white line draws on top,
    # framing the shared top and right edges cleanly.
    _R39  = ALT_X + ALT_W - 39    # 601 = left edge of drum section
    _nx   = ALT_X                  # 566 — flush with tape left edge
    _ny   = TAPE_MID + 15          # 244 — flush with inner-box bottom path
    _nw   = _R39 - ALT_X          # 35  — flush with drum-section left path
    _nh   = 22                     # extends to y=265 (7px below outer box bottom)
    if abs(vspeed) > 30:
        _varr = "\u25b2" if vspeed > 0 else "\u25bc"
        _vstr = f"{_varr}{abs(vspeed)/1000:.1f}"
        _vcol = (0, 220, 0) if vspeed > 0 else (255, 140, 0)
    else:
        _vstr = "\u2014"
        _vcol = LTGREY
    draw.rounded_rectangle([(_nx, _ny), (_nx+_nw-1, _ny+_nh-1)],
                            radius=3, fill=(0, 8, 22), outline=(70, 100, 130), width=1)
    _fv = fnt(13, bold=True)
    _bb = draw.textbbox((0, 0), _vstr, font=_fv)
    _tw, _th = _bb[2]-_bb[0], _bb[3]-_bb[1]
    draw.text((_nx + (_nw-_tw)//2 - _bb[0],
               _ny + (_nh-_th)//2 - _bb[1]),
              _vstr, fill=_vcol, font=_fv)

    # Alt box outline — drawn AFTER VSI box so it frames the shared edges
    draw.line(pts_a + [pts_a[0]], fill=WHITE, width=2)
    # Inner: cascade from drum; carry starts when drum_pos > 4 (last 20 ft before rollover)
    carry_frac = max(0.0, (alt % 100) / 20 - 4.0)
    alt_inner  = float(alt // 100) + carry_frac
    inner_int  = int(alt_inner)
    if inner_int < 10:                      # alt < 1,000 ft — hundreds only
        rolling_drum(img, R-80, TAPE_MID-14, 41, 28, alt_inner, 1, WHITE, 24)
    elif inner_int < 100:                   # 1,000–9,999 ft — thousands (24pt) + hundreds (22pt)
        # Thousands in right cell of 28px slot (R-66..R-52); ten-thousands slot left empty
        rolling_drum(img, R-66, TAPE_MID-14, 14, 28, alt_inner, 1, WHITE, 24,
                     power_offset=1)
        rolling_drum(img, R-52, TAPE_MID-14, 12, 28, alt_inner, 1, WHITE, 22)
    else:                                   # alt ≥ 10,000 ft — ten-thou+thou (22pt) + hundreds
        rolling_drum(img, R-80, TAPE_MID-14, 28, 28, alt_inner, 2, WHITE, 22,
                     suppress_leading=True, power_offset=1)
        rolling_drum(img, R-52, TAPE_MID-14, 12, 28, alt_inner, 1, WHITE, 22)
    # Drum: 20-ft labels scroll together, adjacent labels half-visible
    rolling_drum_alt20(img, R-38, TAPE_MID-28, 22, 56, alt, WHITE, 18,
                       show_adjacent=True, adj_slot_h=18)
    _drum_shade(img,   R-38, TAPE_MID-28, 22, 56)   # 1px inset from border

    # ── 5. HEADING TAPE CONTENT ───────────────────────────────────────────────
    CARDS = {0:'N',45:'NE',90:'E',135:'SE',180:'S',225:'SW',270:'W',315:'NW'}
    _hf = fnt(17, bold=True)
    for i in range(-70, 71):
        deg = int((round(hdg) + i + 3600)) % 360
        off = i - (hdg - round(hdg))
        x = int(CX + off * PX_PER_DEG)
        if not (0 < x < W): continue
        if deg % 5 == 0:
            th = int(HDG_H * (0.35 if deg % 10 == 0 else 0.18))
            draw.line([(x, HDG_Y), (x, HDG_Y+th)],
                      fill=(200,200,200), width=2 if deg%10==0 else 1)
        if deg % 10 == 0:
            lbl = CARDS.get(deg, str(deg // 10))
            col = YELLOW if deg in CARDS else (230,230,230)
            tw = int(draw.textlength(lbl, font=_hf))
            draw.text((x - tw // 2, HDG_Y+HDG_H-18), lbl, fill=col, font=_hf)

    # Heading bug — stores at button edges when outside visible tape range.
    if hdg_bug is not None:
        hb_off = ((hdg_bug - hdg + 180) % 360) - 180
        hb_x   = int(CX + hb_off * PX_PER_DEG)
        hb_x   = max(SPD_W, min(ALT_X, hb_x))   # clamp to inner edges of tap buttons
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
    pts_h = _chamfer([(bx,       by2),
                      (bx+bw2,   by2),
                      (bx+bw2,   by2+bh2),
                      (tx+th,    by2+bh2),
                      (CX,       by2+bh2+td),
                      (tx,       by2+bh2),
                      (bx,       by2+bh2)], {0, 1, 2, 6})
    draw.polygon(pts_h, fill=(0, 0, 0))
    draw.line(pts_h + [pts_h[0]], fill=WHITE, width=2)
    _hbf  = fnt(17)
    _hstr = f"{round(hdg)%360:03d}\u00b0"
    _hbb  = draw.textbbox((0, 0), _hstr, font=_hbf)   # (l, t, r, b)
    _htw  = _hbb[2] - _hbb[0]
    _hth  = _hbb[3] - _hbb[1]
    draw.text((CX - _htw // 2 - _hbb[0],
               by2 + (bh2 - _hth) // 2 - _hbb[1]),
              _hstr, fill=WHITE, font=_hbf)

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
    # Inner edge moved from ±69 → ±57 (50% wider base; outer edge ±93 unchanged)
    # Bisect at ±75 = midpoint of ±57..±93, giving equal-width inner/outer strips
    draw.polygon([(CX, CY), (CX-75, CY+44), (CX-57, CY+44)], fill=AMBER_DARK)  # L inner
    draw.polygon([(CX, CY), (CX-93, CY+44), (CX-75, CY+44)], fill=AMBER)       # L outer
    draw.polygon([(CX, CY), (CX+57, CY+44), (CX+75, CY+44)], fill=AMBER_DARK)  # R inner
    draw.polygon([(CX, CY), (CX+75, CY+44), (CX+93, CY+44)], fill=AMBER)       # R outer
    # Engine nacelles — fills
    draw.polygon([(CX-93, CY), (CX-99, CY-6), (CX-138, CY-6), (CX-138, CY)],   fill=AMBER)       # L upper
    draw.polygon([(CX-93, CY), (CX-138, CY),  (CX-138, CY+6), (CX-99, CY+6)],  fill=AMBER_DARK)  # L lower
    draw.polygon([(CX+93, CY), (CX+99, CY-6), (CX+138, CY-6), (CX+138, CY)],   fill=AMBER)       # R upper
    draw.polygon([(CX+93, CY), (CX+138, CY),  (CX+138, CY+6), (CX+99, CY+6)],  fill=AMBER_DARK)  # R lower
    # Outer perimeter outlines only (no line across the inner colour split)
    draw.polygon([(CX, CY), (CX-93, CY+44), (CX-57, CY+44)], outline=BLK)  # L wing
    draw.polygon([(CX, CY), (CX+57, CY+44), (CX+93, CY+44)], outline=BLK)  # R wing
    draw.polygon([(CX-93, CY), (CX-99, CY-6), (CX-138, CY-6), (CX-138, CY+6), (CX-99, CY+6)], outline=BLK)  # L nacelle
    draw.polygon([(CX+93, CY), (CX+99, CY-6), (CX+138, CY-6), (CX+138, CY+6), (CX+99, CY+6)], outline=BLK)  # R nacelle

    # ── 8. CYAN TAP-BUTTONS ──────────────────────────────────────────────────
    # IAS and ALT bug buttons sit at the TOP of their respective tapes (drawn
    # above in sections 3 and 4).  HDG and BARO sit at the BASE of the heading
    # strip, left and right — keeping the centre clear for the heading readout.
    btn_y = HDG_Y + 2
    cyan_box(f"{round(hdg_bug)%360:03d}\u00b0" if hdg_bug is not None else "---\u00b0",
             SPD_X, btn_y, bw=SPD_W, bh=22)
    baro_val = f"{baro_hpa/33.8639:.2f} IN" if baro_ok else "GPS ALT"
    baro_fsz = 12 if baro_ok else 14   # "29.92 IN" is wider, use smaller font
    cyan_box(baro_val, ALT_X, btn_y, bw=ALT_W, bh=22, font_sz=baro_fsz)

    # ── 9. SLIP INDICATOR — thin bar below zero-bank triangle pointer ─────────
    # Slides ±12 px under the fixed doghouse, same width as its base (16 px)
    slip_y = ROLL_CY - ROLL_R + 24  # below lower doghouse base (y≈40)
    max_d  = 12
    defl   = int(max(-max_d, min(max_d, (ay / 0.2) * max_d)))
    draw.rectangle([(CX + defl - 8, slip_y),
                    (CX + defl + 8, slip_y + 4)], fill=WHITE)

    # ── 10. STATUS BADGES ─────────────────────────────────────────────────────
    # Left badges (AHRS, LINK) — just right of speed tape, clear of tape area
    # Right badges (GPS sats, ALT mode) — just left of alt tape
    _bf = fnt(10)

    bx_l = AI_X + 4
    def badge_l(text, bg, fg=WHITE):
        nonlocal bx_l
        tw = int(draw.textlength(text, font=_bf)) + 10
        draw.rectangle([(bx_l, 4), (bx_l+tw, 19)], fill=bg)
        draw.text((bx_l+5, 5), text, fill=fg, font=_bf)
        bx_l += tw + 2

    badge_l("AHRS" if ahrs_ok else "AHRS FAIL",
            (0, 100, 80) if ahrs_ok else (150, 0, 0))
    badge_l("LINK", (0, 130, 0))

    bx_r = ALT_X - 4
    def badge_r(text, bg, fg=WHITE):
        nonlocal bx_r
        tw = int(draw.textlength(text, font=_bf)) + 10
        bx_r -= tw + 2
        draw.rectangle([(bx_r, 4), (bx_r+tw, 19)], fill=bg)
        draw.text((bx_r+5, 5), text, fill=fg, font=_bf)

    alt_lbl = "BARO ALT" if baro_ok else "GPS ALT"
    badge_r(alt_lbl,
            (0, 80, 120) if baro_ok else (80, 80, 0),
            WHITE if baro_ok else (220, 220, 100))
    badge_r(f"GPS {sats}sat" if gps_ok else "NO GPS",
            (0, 150, 0) if gps_ok else (100, 100, 0))

    # ── 11. SCENARIO LABEL ────────────────────────────────────────────────────
    lw = len(label)*6 + 16
    draw.rectangle([(CX-lw//2, HDG_Y-50),(CX+lw//2, HDG_Y-32)],
                   fill=(0,0,0))
    draw.text((CX-lw//2+8, HDG_Y-48), label, fill=(255,210,60), font=fnt(10))

    img.save(filename)
    print(f"Saved {filename}")


# ── Setup screen previews ─────────────────────────────────────────────────────

def _setup_btn(draw, bx, by, bw, bh, label, subtitle="", exit_btn=False, r=8):
    """Draw one setup-menu button (shared by setup and numpad screens)."""
    bg = (28, 6, 6) if exit_btn else (0, 12, 32)
    draw.rounded_rectangle([(bx, by), (bx+bw-1, by+bh-1)], radius=r, fill=bg)
    glow_h = bh // 5
    for i in range(glow_h):
        t = 1.0 - i / glow_h
        if exit_btn:
            gc = (int(45+t*55), int(8+t*12), int(8+t*12))
        else:
            gc = (int(15+t*35), int(20+t*50), int(40+t*80))
        draw.line([(bx+r, by+1+i), (bx+bw-r, by+1+i)], fill=gc)
    oc = (200, 55, 55) if exit_btn else WHITE
    draw.rounded_rectangle([(bx, by), (bx+bw-1, by+bh-1)], radius=r, outline=oc, width=2)
    lf = fnt(19, bold=True)
    lh = 22
    total_h = lh + (16 if subtitle else 0)
    ly = by + (bh - total_h) // 2
    lw = int(draw.textlength(label, font=lf))
    draw.text((bx + (bw-lw)//2, ly), label, fill=WHITE, font=lf)
    if subtitle:
        sf = fnt(11)
        sw = int(draw.textlength(subtitle, font=sf))
        draw.text((bx + (bw-sw)//2, ly+lh+4), subtitle, fill=(155, 170, 190), font=sf)


def draw_setup_screen(filename):
    """Render the setup/settings main page."""
    img  = Image.new('RGB', (W, H), (0, 8, 22))
    draw = ImageDraw.Draw(img)

    # Title bar
    draw.rectangle([(0, 0), (W-1, 43)], fill=(0, 18, 45))
    draw.line([(0, 43), (W-1, 43)], fill=WHITE, width=1)
    tf = fnt(22, bold=True)
    draw.text(((W - int(draw.textlength("SETUP", font=tf)))//2, 8),
              "SETUP", fill=WHITE, font=tf)
    hf = fnt(10)
    hint = "2-finger hold to enter  ·  EXIT to return"
    draw.text((W - int(draw.textlength(hint, font=hf)) - 6, 15),
              hint, fill=(110, 120, 140), font=hf)

    # Button grid — 2 cols × 3 rows
    MX = 15; MY = 50; GAP_X = 10; GAP_Y = 12
    BW = (W  - 2*MX - GAP_X) // 2       # 300
    BH = (H  - MY - 14 - 2*GAP_Y) // 3  # 130
    COLS = [MX, MX + BW + GAP_X]
    ROWS = [MY, MY + BH + GAP_Y, MY + 2*(BH + GAP_Y)]

    items = [
        (0, 0, "FLIGHT PROFILE",  "V-speeds · Aircraft · Tail #",    False),
        (1, 0, "DISPLAY",         "Units · Brightness · Night mode", False),
        (0, 1, "AHRS / SENSORS",  "Trim · Mag cal · Mounting",       False),
        (1, 1, "CONNECTIVITY",    "WiFi · AHRS link",                False),
        (0, 2, "SYSTEM",          "Version · Diagnostics · Reset",   False),
        (1, 2, "EXIT",            "Return to PFD",                   True),
    ]
    for col, row, lbl, sub, ex in items:
        _setup_btn(draw, COLS[col], ROWS[row], BW, BH, lbl, sub, ex)

    img.save(filename)
    print(f"Saved {filename}")


# Numpad key table: (label, style)  styles: 'n'=normal  'x'=cancel  'ok'=enter
_NP_KEYS = [
    [('7','n'), ('8','n'), ('9','n')],
    [('4','n'), ('5','n'), ('6','n')],
    [('1','n'), ('2','n'), ('3','n')],
    [('CANCEL','x'), ('0','n'), ('ENTER','ok')],
]
_NP_PW = 120; _NP_PH = 64; _NP_GX = 12; _NP_GY = 10
_NP_TW = 3*_NP_PW + 2*_NP_GX   # 384  — total numpad width
_NP_X0 = (W - _NP_TW) // 2     # 128  — left edge of numpad


def _numpad_btn(draw, col, row, label, style, r=8):
    bx = _NP_X0 + col * (_NP_PW + _NP_GX)
    by = 118     + row * (_NP_PH + _NP_GY)
    if style == 'x':
        bg=(28,6,6);   oc=(200,55,55);  tc=(220,80,80)
    elif style == 'ok':
        bg=(5,25,10);  oc=(50,200,80);  tc=(60,220,90)
    else:
        bg=(0,12,32);  oc=WHITE;        tc=WHITE
    draw.rounded_rectangle([(bx, by), (bx+_NP_PW-1, by+_NP_PH-1)], radius=r, fill=bg)
    glow_h = _NP_PH // 5
    for i in range(glow_h):
        t = 1.0 - i / glow_h
        if style == 'x':
            gc = (int(45+t*55), int(8+t*12),  int(8+t*12))
        elif style == 'ok':
            gc = (int(5+t*15),  int(40+t*60), int(10+t*20))
        else:
            gc = (int(15+t*30), int(20+t*45), int(40+t*75))
        draw.line([(bx+r, by+1+i), (bx+_NP_PW-r, by+1+i)], fill=gc)
    draw.rounded_rectangle([(bx, by), (bx+_NP_PW-1, by+_NP_PH-1)], radius=r, outline=oc, width=2)
    lf = fnt(20, bold=True)
    lw = int(draw.textlength(label, font=lf))
    draw.text((bx + (_NP_PW-lw)//2, by + (_NP_PH-22)//2), label, fill=tc, font=lf)


def draw_numpad_screen(title, current_val, filename, entered="", suffix=""):
    """Render a full-screen numeric-entry pad (alt bug, hdg bug, GS bug, etc.).
    suffix: appended in dim cyan after the entered digits (e.g. '00' for alt bug).
    """
    img  = Image.new('RGB', (W, H), (0, 8, 22))
    draw = ImageDraw.Draw(img)

    # Title bar
    draw.rectangle([(0, 0), (W-1, 43)], fill=(0, 18, 45))
    draw.line([(0, 43), (W-1, 43)], fill=WHITE, width=1)
    tf = fnt(18, bold=True)
    draw.text(((W - int(draw.textlength(title, font=tf)))//2, 12),
              title, fill=WHITE, font=tf)

    # Value display box
    base_str = entered if entered else str(current_val)
    draw.rounded_rectangle([(80, 50), (W-81, 100)], radius=6, fill=(0, 15, 38))
    draw.rounded_rectangle([(80, 50), (W-81, 100)], radius=6, outline=WHITE, width=1)
    vf = fnt(32, bold=True)
    if suffix:
        bw = int(draw.textlength(base_str, font=vf))
        sw = int(draw.textlength(suffix,   font=vf))
        bx_str = (W - bw - sw) // 2
        draw.text((bx_str,      55), base_str, fill=CYAN,         font=vf)
        draw.text((bx_str + bw, 55), suffix,   fill=(0, 100, 100), font=vf)
    else:
        vw = int(draw.textlength(base_str, font=vf))
        draw.text(((W-vw)//2, 55), base_str, fill=CYAN, font=vf)
    hf = fnt(10)
    cur_display = f"{current_val}{suffix}" if suffix else str(current_val)
    hint = f"Current: {cur_display}"
    hw = int(draw.textlength(hint, font=hf))
    draw.text(((W-hw)//2, 104), hint, fill=(110, 120, 140), font=hf)

    # 4 × 3 numpad grid
    for ri, row in enumerate(_NP_KEYS):
        for ci, (lbl, sty) in enumerate(row):
            _numpad_btn(draw, ci, ri, lbl, sty)

    img.save(filename)
    print(f"Saved {filename}")


# ── Flight Profile screen ────────────────────────────────────────────────────

# Field definitions: (key, label, units, max_digits, input_type)
# input_type: 'num' = numpad   'kbd' = keyboard
_FP_FIELDS = [
    ("tail",     "TAIL NUMBER",   "",    8,  "kbd"),
    ("actype",   "AIRCRAFT TYPE", "",    8,  "kbd"),
    ("vs0",      "VS0 — Stall flaps", "kt", 3, "num"),
    ("vs1",      "VS1 — Stall clean", "kt", 3, "num"),
    ("vfe",      "VFE — Max flaps",   "kt", 3, "num"),
    ("vno",      "VNO — Max cruise",  "kt", 3, "num"),
    ("vne",      "VNE — Never exceed","kt", 3, "num"),
    ("va",       "VA  — Manoeuvre",   "kt", 3, "num"),
    ("vy",       "VY  — Best rate",   "kt", 3, "num"),
    ("vx",       "VX  — Best angle",  "kt", 3, "num"),
]

_FP_DEFAULTS = {
    "tail":"N12345", "actype":"C172S",
    "vs0":48, "vs1":55, "vfe":85, "vno":129, "vne":163, "va":105, "vy":74, "vx":62,
}

# Layout constants
_FP_MX = 12          # side margin
_FP_GAP = 8          # gap between rows
_FP_H1 = 58          # height of text-entry fields (tail, type)
_FP_H2 = 48          # height of V-speed fields
_FP_Y0 = 50          # first field top


def _fp_field(draw, bx, by, bw, bh, label, value, units="", r=6, highlight=False):
    """Draw a single Flight Profile field row."""
    bg = (0, 22, 48) if highlight else (0, 12, 32)
    oc = CYAN if highlight else WHITE
    draw.rounded_rectangle([(bx, by), (bx+bw-1, by+bh-1)], radius=r, fill=bg)
    # top glow
    glow_h = bh // 5
    for i in range(glow_h):
        t = 1.0 - i / glow_h
        gc = (int(15+t*35), int(20+t*50), int(40+t*80))
        draw.line([(bx+r, by+1+i), (bx+bw-r, by+1+i)], fill=gc)
    draw.rounded_rectangle([(bx, by), (bx+bw-1, by+bh-1)], radius=r, outline=oc, width=2)
    # Label — left side, small
    lf = fnt(11)
    draw.text((bx+10, by+6), label, fill=(155, 170, 190), font=lf)
    # Value — right side, larger
    vf = fnt(18, bold=True)
    val_str = str(value) if value not in (None, "", 0) else "---"
    if units and val_str != "---":
        val_str = f"{val_str} {units}"
    vw = int(draw.textlength(val_str, font=vf))
    vc = CYAN if highlight else WHITE
    draw.text((bx+bw-vw-12, by+(bh-20)//2), val_str, fill=vc, font=vf)


def draw_flight_profile_screen(filename, values=None):
    """Render the Flight Profile setup screen.

    Layout:
      - Title bar + BACK
      - Two full-width text fields: TAIL NUMBER, AIRCRAFT TYPE
      - Section label: V-SPEEDS
      - 4 rows × 2 columns for the 8 V-speed fields
    """
    if values is None:
        values = _FP_DEFAULTS.copy()

    img  = Image.new('RGB', (W, H), (0, 8, 22))
    draw = ImageDraw.Draw(img)

    # Title bar
    draw.rectangle([(0, 0), (W-1, 43)], fill=(0, 18, 45))
    draw.line([(0, 43), (W-1, 43)], fill=WHITE, width=1)
    _setup_btn(draw, 8, 6, 72, 31, "\u2190 BACK", r=5)
    tf = fnt(20, bold=True)
    draw.text(((W - int(draw.textlength("FLIGHT PROFILE", font=tf)))//2, 10),
              "FLIGHT PROFILE", fill=WHITE, font=tf)

    MX = _FP_MX; GAP = _FP_GAP
    FW = W - 2*MX   # 616 — full-width field

    # ── Aircraft info fields (full-width, taller for alpha entry) ─────────────
    y = _FP_Y0
    for key in ("tail", "actype"):
        _, label, units, _, _ = next(f for f in _FP_FIELDS if f[0] == key)
        _fp_field(draw, MX, y, FW, _FP_H1, label, values.get(key,"---"), units)
        y += _FP_H1 + GAP

    # ── V-speeds section header ────────────────────────────────────────────────
    y += 2
    draw.line([(MX, y), (W-MX, y)], fill=(40, 60, 90), width=1)
    y += 4
    sf = fnt(11)
    draw.text((MX, y), "V-SPEEDS  (knots) — tap to edit", fill=(120, 140, 165), font=sf)
    y += 18

    # ── V-speed grid: 4 rows × 2 cols ─────────────────────────────────────────
    V_KEYS = [k for k,*_ in _FP_FIELDS if k not in ("tail","actype")]
    BW = (FW - GAP) // 2   # ≈ 303
    BH = (H - y - GAP*3 - 4) // 4  # fill remaining height equally
    COLS = [MX, MX + BW + GAP]

    for i, key in enumerate(V_KEYS):
        _, label, units, _, _ = next(f for f in _FP_FIELDS if f[0] == key)
        col = i % 2
        row = i // 2
        bx = COLS[col]
        by = y + row * (BH + GAP)
        _fp_field(draw, bx, by, BW, BH, label, values.get(key,"---"), units)

    img.save(filename)
    print(f"Saved {filename}")


# ── Keyboard screen ────────────────────────────────────────────────────────────

# Row definitions: list of (label, width) — widths sum to ≤640 per row
_KB_ROW_H = 66     # key height
_KB_GAP_Y = 6      # vertical gap between rows
_KB_GAP_X = 4      # horizontal gap between keys

_KB_ROWS = [
    # (label, width, style)  styles: 'n'=normal  'x'=cancel  'ok'=done  'del'=backspace
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

_KB_Y0 = 112   # top of first key row (below title + display box)


def _kb_row_width(row):
    return sum(w for _, w, _ in row) + _KB_GAP_X * (len(row)-1)


def _kb_row_x0(row):
    return (W - _kb_row_width(row)) // 2


def _kb_key(draw, bx, by, bw, bh, label, style, r=6):
    if style == 'x':
        bg=(28,6,6);   oc=(200,55,55);  tc=(220,80,80)
    elif style == 'ok':
        bg=(5,25,10);  oc=(50,200,80);  tc=(60,220,90)
    elif style == 'del':
        bg=(30,18,5);  oc=(200,140,40); tc=(220,160,50)
    else:
        bg=(0,12,32);  oc=WHITE;        tc=WHITE
    draw.rounded_rectangle([(bx,by),(bx+bw-1,by+bh-1)], radius=r, fill=bg)
    glow_h = bh // 5
    for i in range(glow_h):
        t = 1.0 - i / glow_h
        if style == 'x':
            gc=(int(45+t*55),int(8+t*12),int(8+t*12))
        elif style == 'ok':
            gc=(int(5+t*15),int(40+t*60),int(10+t*20))
        elif style == 'del':
            gc=(int(40+t*50),int(25+t*35),int(5+t*10))
        else:
            gc=(int(15+t*30),int(20+t*45),int(40+t*75))
        draw.line([(bx+r,by+1+i),(bx+bw-r,by+1+i)], fill=gc)
    draw.rounded_rectangle([(bx,by),(bx+bw-1,by+bh-1)], radius=r, outline=oc, width=2)
    fs = 13 if len(label) > 2 else 18
    lf = fnt(fs, bold=True)
    lw = int(draw.textlength(label, font=lf))
    lh = fs + 2
    draw.text((bx+(bw-lw)//2, by+(bh-lh)//2), label, fill=tc, font=lf)


def draw_keyboard_screen(title, current_val, filename, entered=""):
    """Render a full-screen QWERTY keyboard for text entry."""
    img  = Image.new('RGB', (W, H), (0, 8, 22))
    draw = ImageDraw.Draw(img)

    # Title bar
    draw.rectangle([(0,0),(W-1,43)], fill=(0,18,45))
    draw.line([(0,43),(W-1,43)], fill=WHITE, width=1)
    tf = fnt(17, bold=True)
    draw.text(((W-int(draw.textlength(title,font=tf)))//2, 12),
              title, fill=WHITE, font=tf)

    # Text display box
    disp_str = entered if entered else str(current_val)
    draw.rounded_rectangle([(10,50),(W-11,100)], radius=6, fill=(0,15,38))
    draw.rounded_rectangle([(10,50),(W-11,100)], radius=6, outline=WHITE, width=1)
    vf = fnt(28, bold=True)
    cursor = disp_str + "\u2502"   # add blinking-cursor character
    vw = int(draw.textlength(cursor, font=vf))
    draw.text(((W-vw)//2, 57), cursor, fill=CYAN, font=vf)
    hf = fnt(10)
    hint = f"Current: {current_val}"
    draw.text(((W-int(draw.textlength(hint,font=hf)))//2, 104),
              hint, fill=(110,120,140), font=hf)

    # Keyboard rows
    y = _KB_Y0
    for row in _KB_ROWS:
        x = _kb_row_x0(row)
        for label, kw, style in row:
            _kb_key(draw, x, y, kw, _KB_ROW_H, label, style)
            x += kw + _KB_GAP_X
        y += _KB_ROW_H + _KB_GAP_Y

    img.save(filename)
    print(f"Saved {filename}")


# ── Sub-setup screens (Display · AHRS · Connectivity · System) ───────────────

_SS_MX  = 12
_SS_Y0  = 52
_SS_RH  = 68
_SS_GAP = 8


def _ss_row_y(i):
    return _SS_Y0 + i * (_SS_RH + _SS_GAP)


def _screen_header_p(draw, title):
    draw.rectangle([(0,0),(W-1,43)], fill=(0,18,45))
    draw.line([(0,43),(W-1,43)], fill=WHITE, width=1)
    _setup_btn(draw, 8, 6, 72, 31, "\u2190 BACK", r=5)
    tf = fnt(20, bold=True)
    draw.text(((W-int(draw.textlength(title,font=tf)))//2, 10), title, fill=WHITE, font=tf)


def _setting_row_p(draw, row_i, label, sub="", _y_override=None):
    bx = _SS_MX; by = _y_override if _y_override is not None else _ss_row_y(row_i)
    bw = W - 2*_SS_MX; bh = _SS_RH
    draw.rounded_rectangle([(bx,by),(bx+bw-1,by+bh-1)], radius=6, fill=(0,12,32))
    gh = bh // 6
    for i in range(gh):
        t = 1.0 - i/gh
        gc = (int(15+t*25), int(20+t*40), int(40+t*65))
        draw.line([(bx+6,by+1+i),(bx+bw-6,by+1+i)], fill=gc)
    draw.rounded_rectangle([(bx,by),(bx+bw-1,by+bh-1)], radius=6, outline=(55,75,105), width=1)
    draw.text((bx+14, by+10), label, fill=WHITE, font=fnt(14,bold=True))
    if sub:
        draw.text((bx+14, by+32), sub, fill=(120,135,155), font=fnt(10))
    return bx, by, bw, bh


def _seg_btn_p(draw, bx, by, bw, bh, label, active, r=5):
    bg = (0,55,65) if active else (0,10,25)
    oc = CYAN      if active else (50,68,92)
    tc = CYAN      if active else (130,148,168)
    draw.rounded_rectangle([(bx,by),(bx+bw-1,by+bh-1)], radius=r, fill=bg)
    if active:
        gh = bh // 4
        for i in range(gh):
            t = 1.0-i/gh
            draw.line([(bx+r,by+1+i),(bx+bw-r,by+1+i)],
                      fill=(int(t*20), int(60+t*40), int(70+t*50)))
    draw.rounded_rectangle([(bx,by),(bx+bw-1,by+bh-1)], radius=r, outline=oc, width=2)
    lf = fnt(14, bold=active)
    lw = int(draw.textlength(label, font=lf))
    draw.text((bx+(bw-lw)//2, by+(bh-16)//2), label, fill=tc, font=lf)


def _step_btn_p(draw, bx, by, bw, bh, label):
    if label == "+":
        bg=(8,28,12);  oc=(50,180,70);  tc=(70,220,90)
    else:
        bg=(30,12,12); oc=(180,50,50);  tc=(220,80,80)
    draw.rounded_rectangle([(bx,by),(bx+bw-1,by+bh-1)], radius=5, fill=bg)
    draw.rounded_rectangle([(bx,by),(bx+bw-1,by+bh-1)], radius=5, outline=oc, width=2)
    lf = fnt(20, bold=True)
    lw = int(draw.textlength(label, font=lf))
    draw.text((bx+(bw-lw)//2, by+(bh-22)//2), label, fill=tc, font=lf)


def _action_btn_p(draw, bx, by, bw, bh, label, style="normal", r=6):
    if   style == "danger": bg=(35,5,5);  oc=(200,40,40);  tc=RED
    elif style == "warn":   bg=(30,20,5); oc=(200,140,40); tc=YELLOW
    elif style == "ok":     bg=(5,28,10); oc=(40,180,60);  tc=(60,220,80)
    else:                   bg=(0,18,45); oc=WHITE;        tc=WHITE
    draw.rounded_rectangle([(bx,by),(bx+bw-1,by+bh-1)], radius=r, fill=bg)
    gh = bh // 5
    for i in range(gh):
        t = 1.0 - i/gh
        if   style == "danger": gc=(int(bg[0]+t*40),int(bg[1]+t*10),int(bg[2]+t*10))
        elif style == "warn":   gc=(int(bg[0]+t*35),int(bg[1]+t*25),int(bg[2]+t*5))
        elif style == "ok":     gc=(int(bg[0]+t*10),int(bg[1]+t*35),int(bg[2]+t*10))
        else:                   gc=(int(bg[0]+t*15),int(bg[1]+t*25),int(bg[2]+t*50))
        draw.line([(bx+r,by+1+i),(bx+bw-r,by+1+i)], fill=gc)
    draw.rounded_rectangle([(bx,by),(bx+bw-1,by+bh-1)], radius=r, outline=oc, width=2)
    lf = fnt(15, bold=True)
    lw = int(draw.textlength(label, font=lf))
    draw.text((bx+(bw-lw)//2, by+(bh-17)//2), label, fill=tc, font=lf)


# Display settings ─────────────────────────────────────────────────────────────

_DSP_BTN_H = 40; _DSP_BTN_G = 6; _DSP_SW = 40; _DSP_VW = 70

_DSP_ROWS = [
    ("spd_unit",   "SPEED UNITS",  "Knots \u00b7 Miles \u00b7 Km/h",
     ["kt","mph","kph"], ["KT","MPH","KPH"], 80),
    ("alt_unit",   "ALTITUDE",     "Feet or Metres",
     ["ft","m"],         ["FT","M"],         100),
    ("baro_unit",  "PRESSURE",     "Inches Hg or hPa",
     ["inhg","hpa"],     ["inHg","hPa"],     100),
    ("brightness", "BRIGHTNESS",   "Screen brightness 1\u201310",
     None, None, None),
    ("night_mode", "NIGHT MODE",   "Dim red cockpit lighting",
     [False,True],       ["OFF","ON"],        100),
]


def _dsp_rx_p(row, bx, bw):
    *_, opts_v, opts_l, bw_each = row
    if opts_v is None:
        total = _DSP_SW + _DSP_BTN_G + _DSP_VW + _DSP_BTN_G + _DSP_SW
    else:
        total = len(opts_v)*bw_each + (len(opts_v)-1)*_DSP_BTN_G
    return bx + bw - total - 14


def draw_display_screen(filename, ds=None):
    if ds is None:
        ds = {"spd_unit":"kt","alt_unit":"ft","baro_unit":"inhg","brightness":8,"night_mode":False}
    img  = Image.new('RGB',(W,H),(0,8,22))
    draw = ImageDraw.Draw(img)
    _screen_header_p(draw, "DISPLAY")
    for ri, row in enumerate(_DSP_ROWS):
        key, label, sub, opts_v, opts_l, bw_each = row
        is_night = (key == "night_mode")
        bx, by, bw, bh = _setting_row_p(draw, ri, label, sub)
        if is_night:
            # Dim veil + "future" label
            overlay = Image.new('RGBA', (bw, bh), (0,0,0,160))
            img.paste(Image.new('RGB',(bw,bh),(0,0,0)), (bx,by),
                      Image.new('L',(bw,bh),160))
            draw.text((bx+bw-60, by+bh-18), "future", fill=(90,90,100), font=fnt(10))
            continue
        ry = by + (bh - _DSP_BTN_H) // 2
        rx = _dsp_rx_p(row, bx, bw)
        if opts_v is None:
            val = ds.get("brightness", 8)
            _step_btn_p(draw, rx, ry, _DSP_SW, _DSP_BTN_H, "\u2212")
            vx = rx + _DSP_SW + _DSP_BTN_G
            draw.rounded_rectangle([(vx,ry),(vx+_DSP_VW-1,ry+_DSP_BTN_H-1)],
                                    radius=4, fill=(0,18,38))
            draw.rounded_rectangle([(vx,ry),(vx+_DSP_VW-1,ry+_DSP_BTN_H-1)],
                                    radius=4, outline=(60,80,110), width=1)
            vf = fnt(18, bold=True)
            vw = int(draw.textlength(str(val), font=vf))
            draw.text((vx+(_DSP_VW-vw)//2, ry+(_DSP_BTN_H-20)//2), str(val), fill=WHITE, font=vf)
            _step_btn_p(draw, vx+_DSP_VW+_DSP_BTN_G, ry, _DSP_SW, _DSP_BTN_H, "+")
        else:
            cur = ds.get(key, opts_v[0])
            for i,(v,lbl) in enumerate(zip(opts_v, opts_l)):
                _seg_btn_p(draw, rx+i*(bw_each+_DSP_BTN_G), ry, bw_each, _DSP_BTN_H, lbl, v==cur)
    img.save(filename)
    print(f"Saved {filename}")


# AHRS / Sensors ───────────────────────────────────────────────────────────────

_SS_TRIM_SW = 40; _SS_TRIM_VW = 90; _SS_TRIM_H = 40; _SS_TRIM_G = 6
_SS_MAG_LABELS = {
    "idle":    ("IDLE",          (100,110,130)),
    "running": ("RUNNING\u2026", YELLOW),
    "done":    ("DONE  \u2713",  (50,200,80)),
    "error":   ("ERROR",         RED),
}


def draw_ahrs_screen(filename, ss=None):
    if ss is None:
        ss = {"pitch_trim":0.0,"roll_trim":0.0,"mag_cal":"idle","mounting":"normal"}
    img  = Image.new('RGB',(W,H),(0,8,22))
    draw = ImageDraw.Draw(img)
    _screen_header_p(draw, "AHRS / SENSORS")

    def _trim_row(ri, key, label, sub):
        bx, by, bw, bh = _setting_row_p(draw, ri, label, sub)
        val = ss.get(key, 0.0)
        total = _SS_TRIM_SW + _SS_TRIM_G + _SS_TRIM_VW + _SS_TRIM_G + _SS_TRIM_SW
        rx = bx + bw - total - 14
        ry = by + (bh - _SS_TRIM_H) // 2
        _step_btn_p(draw, rx, ry, _SS_TRIM_SW, _SS_TRIM_H, "\u2212")
        vx = rx + _SS_TRIM_SW + _SS_TRIM_G
        draw.rounded_rectangle([(vx,ry),(vx+_SS_TRIM_VW-1,ry+_SS_TRIM_H-1)],
                                radius=4, fill=(0,18,38))
        draw.rounded_rectangle([(vx,ry),(vx+_SS_TRIM_VW-1,ry+_SS_TRIM_H-1)],
                                radius=4, outline=(60,80,110), width=1)
        vf = fnt(16, bold=True)
        s = f"{val:+.1f}\u00b0"
        vw = int(draw.textlength(s, font=vf))
        draw.text((vx+(_SS_TRIM_VW-vw)//2, ry+(_SS_TRIM_H-18)//2), s, fill=WHITE, font=vf)
        _step_btn_p(draw, vx+_SS_TRIM_VW+_SS_TRIM_G, ry, _SS_TRIM_SW, _SS_TRIM_H, "+")

    _trim_row(0, "pitch_trim", "PITCH TRIM", "Horizon offset correction")
    _trim_row(1, "roll_trim",  "ROLL TRIM",  "Wing-level correction")

    # Row 2: magnetometer — CALIBRATE is greyed out (future)
    bx, by, bw, bh = _setting_row_p(draw, 2, "MAGNETOMETER", "Compass calibration")
    cal = ss.get("mag_cal", "idle")
    state_lbl, state_col = _SS_MAG_LABELS.get(cal, ("?", WHITE))
    draw.text((bx+220, by+(bh-15)//2), state_lbl, fill=state_col, font=fnt(13,bold=True))
    cbx = bx+bw-138-14; cby = by+(bh-36)//2
    draw.rounded_rectangle([(cbx,cby),(cbx+137,cby+35)], radius=6, fill=(18,18,20))
    draw.rounded_rectangle([(cbx,cby),(cbx+137,cby+35)], radius=6, outline=(55,55,65), width=2)
    cw = int(draw.textlength("CALIBRATE", font=fnt(15)))
    draw.text((cbx+(138-cw)//2, cby+9), "CALIBRATE", fill=(75,75,88), font=fnt(15))
    fw = int(draw.textlength("future", font=fnt(9)))
    draw.text((cbx+(138-fw)//2, cby+26), "future", fill=(60,60,72), font=fnt(9))

    # Row 3: mounting
    bx, by, bw, bh = _setting_row_p(draw, 3, "MOUNTING", "Board orientation")
    cur = ss.get("mounting", "normal")
    opts = [("normal","NORMAL"),("inverted","INVERTED")]
    total = 2*120 + _DSP_BTN_G
    rx = bx + bw - total - 14
    ry = by + (bh - _DSP_BTN_H) // 2
    for i,(v,lbl) in enumerate(opts):
        _seg_btn_p(draw, rx+i*(120+_DSP_BTN_G), ry, 120, _DSP_BTN_H, lbl, v==cur)

    img.save(filename)
    print(f"Saved {filename}")


# Connectivity ─────────────────────────────────────────────────────────────────

_CS_FIELDS = [
    ("ahrs_url",  "AHRS URL",       "Pico W access-point address"),
    ("wifi_ssid", "WiFi SSID",      "Network name to join"),
    ("wifi_pass", "WiFi PASSWORD",  "WPA2 passphrase"),
]
_CS_BTN_Y = _ss_row_y(len(_CS_FIELDS) + 1) + 4
_CS_BTN_H = 50


def draw_connectivity_screen(filename, cs=None):
    if cs is None:
        cs = {
            "ahrs_url":  "http://192.168.4.1",
            "wifi_ssid": "AHRS-Link",
            "wifi_pass": "secret123",
            "ahrs_ok":   False,
            "wifi_ok":   False,
            "apply_msg": "",
            "test_msg":  "",
        }
    img  = Image.new('RGB',(W,H),(0,8,22))
    draw = ImageDraw.Draw(img)
    _screen_header_p(draw, "CONNECTIVITY")
    bw = W - 2*_SS_MX

    # Rows 0-2: editable fields
    for ri,(key, label, sub) in enumerate(_CS_FIELDS):
        bx, by, _, bh = _setting_row_p(draw, ri, label, sub)
        val = cs.get(key, "")
        # Password masking
        display = ("\u25cf" * min(len(val), 16)) if (key == "wifi_pass" and val) else val
        vbx = bx+210; vby = by+12; vbw = bx+bw-vbx-12; vbh = bh-24
        draw.rounded_rectangle([(vbx,vby),(vbx+vbw-1,vby+vbh-1)], radius=4, fill=(0,20,42))
        draw.rounded_rectangle([(vbx,vby),(vbx+vbw-1,vby+vbh-1)], radius=4, outline=CYAN, width=1)
        vf = fnt(12, bold=bool(val))
        txt = display or "\u2014"
        vw = int(draw.textlength(txt, font=vf))
        draw.text((vbx+(vbw-vw)//2, vby+(vbh-14)//2), txt, fill=CYAN, font=vf)
        draw.text((vbx+6, vby+vbh-13), "tap to edit", fill=(80,100,125), font=fnt(9))

    # Row 3: STATUS — live dots
    bx, by, _, bh = _setting_row_p(draw, 3, "STATUS", "Connection state")
    for i,(ok_k, ok_y, ok_n) in enumerate([
            ("ahrs_ok","AHRS  \u2713 CONNECTED","AHRS  \u2717 NO LINK"),
            ("wifi_ok","WiFi  \u2713 CONNECTED","WiFi  \u2717 NO LINK")]):
        ok  = cs.get(ok_k, False)
        col = (60,220,80) if ok else (200,50,50)
        lbl = ok_y if ok else ok_n
        cy  = by + bh//4 + i*bh//2
        draw.ellipse([(bx+222,cy-5),(bx+232,cy+5)], fill=col)
        draw.text((bx+240, cy-8), lbl, fill=col, font=fnt(12,bold=True))

    # apply_msg / test_msg feedback lines
    msg_y = _CS_BTN_Y - 28
    if cs.get("apply_msg"):
        draw.text((_SS_MX+6, msg_y), cs["apply_msg"], fill=(140,200,140), font=fnt(11))
    if cs.get("test_msg"):
        draw.text((W//2+6, msg_y), cs["test_msg"], fill=(140,200,140), font=fnt(11))

    # Two action buttons: [APPLY WIFI]   [TEST AHRS]
    half = (bw - 10) // 2
    _action_btn_p(draw, _SS_MX,          _CS_BTN_Y, half, _CS_BTN_H, "APPLY WIFI", "ok")
    _action_btn_p(draw, _SS_MX+half+10,  _CS_BTN_Y, half, _CS_BTN_H, "TEST AHRS",  "normal")

    img.save(filename)
    print(f"Saved {filename}")


# System ───────────────────────────────────────────────────────────────────────

_SYS_VERSION = "0.1.0"
_SYS_BUILD   = "2026-04-10"
_SYS_INFO_LH = 28


def draw_system_screen(filename, display_mode="pfd"):
    img  = Image.new('RGB',(W,H),(0,8,22))
    draw = ImageDraw.Draw(img)
    _screen_header_p(draw, "SYSTEM")
    bx = _SS_MX; bw = W - 2*_SS_MX
    lines = [
        ("Firmware version",  _SYS_VERSION),
        ("Build date",        _SYS_BUILD),
        ("Display",           "640\u00d7480  DSI"),
        ("Hardware",          "Pi Zero 2W + Pico W"),
        ("SRTM terrain data", "not found"),
    ]
    ih = len(lines)*_SYS_INFO_LH + 16
    INFO_Y = 56
    draw.rounded_rectangle([(bx,INFO_Y),(bx+bw-1,INFO_Y+ih-1)], radius=6, fill=(0,12,32))
    draw.rounded_rectangle([(bx,INFO_Y),(bx+bw-1,INFO_Y+ih-1)], radius=6,
                            outline=(55,75,105), width=1)
    for i,(k,v) in enumerate(lines):
        ty = INFO_Y + 10 + i*_SYS_INFO_LH
        draw.text((bx+14, ty), k, fill=(120,140,165), font=fnt(12))
        draw.text((bx+310, ty), v, fill=WHITE, font=fnt(13,bold=True))

    # DISPLAY MODE row
    mode_y = INFO_Y + ih + 8
    _setting_row_p(draw, 0, "DISPLAY MODE",
                   "Primary Flight Display or Multi-Function Display",
                   _y_override=mode_y)
    btn_w_m = 110; gap_m = _DSP_BTN_G; btn_h_m = _DSP_BTN_H
    rx = bx + bw - 2*(btn_w_m+gap_m) + gap_m - 14
    ry = mode_y + (_SS_RH - btn_h_m) // 2
    _seg_btn_p(draw, rx, ry, btn_w_m, btn_h_m, "PFD", display_mode == "pfd")
    # MFD — disabled placeholder
    draw.rounded_rectangle([(rx+btn_w_m+gap_m, ry),
                             (rx+btn_w_m+gap_m+btn_w_m-1, ry+btn_h_m-1)],
                            radius=5, fill=(0,8,18))
    draw.rounded_rectangle([(rx+btn_w_m+gap_m, ry),
                             (rx+btn_w_m+gap_m+btn_w_m-1, ry+btn_h_m-1)],
                            radius=5, outline=(35,45,60), width=2)
    mx = rx+btn_w_m+gap_m+btn_w_m//2
    lf_m = fnt(14)
    draw.text((mx - int(draw.textlength("MFD",font=lf_m))//2, ry+4),
              "MFD", fill=(50,60,75), font=lf_m)
    sf_m = fnt(9)
    draw.text((mx - int(draw.textlength("coming soon",font=sf_m))//2, ry+btn_h_m-17),
              "coming soon", fill=(45,55,70), font=sf_m)

    terrain_y = mode_y + _SS_RH + 8
    half = (bw - 8) // 2

    def _data_tile_p(tx, label, sub, active=True):
        for i in range(_SS_RH):
            t = 1.0 - i/_SS_RH
            if active:
                c = (int(t*8), int(12+t*18), int(28+t*35))
            else:
                c = (int(t*5), int(t*7), int(t*12))
            draw.line([(tx, terrain_y+i),(tx+half-1, terrain_y+i)], fill=c)
        bc = (55,75,105) if active else (28,35,48)
        draw.rounded_rectangle([(tx,terrain_y),(tx+half-1,terrain_y+_SS_RH-1)],
                                radius=4, outline=bc, width=1)
        lc = WHITE if active else (55,62,72)
        sc = (100,120,145) if active else (42,48,58)
        draw.text((tx+12, terrain_y+10), label, fill=lc, font=fnt(13,bold=True))
        draw.text((tx+12, terrain_y+28), sub,   fill=sc, font=fnt(11))
        if active:
            draw.text((tx+half-22, terrain_y+(_SS_RH-18)//2), "\u25b6",
                      fill=(60,80,110), font=fnt(16))
        else:
            draw.text((tx+half-50, terrain_y+_SS_RH-18), "future",
                      fill=(48,55,65), font=fnt(10))

    _data_tile_p(bx,        "TERRAIN DATA",  "12 tiles on disk  \u00b7  11.8 MB", active=True)
    _data_tile_p(bx+half+8, "OBSTACLE DATA", "FAA digital obstacle file",         active=False)

    btn_y = terrain_y + _SS_RH + 8; btn_h = 54
    half_w = (bw - 10) // 2
    _action_btn_p(draw, bx,           btn_y, half_w, btn_h, "DIAGNOSTICS",   "normal")
    _action_btn_p(draw, bx+half_w+10, btn_y, half_w, btn_h, "RESET DEFAULTS","danger")

    img.save(filename)
    print(f"Saved {filename}")


# ── Terrain data screen ───────────────────────────────────────────────────────

_TD_REGIONS_P = [
    ("US Southwest", "AZ \u00b7 NM \u00b7 NV \u00b7 UT \u00b7 CO",   31, 42, -115, -103),
    ("US Pacific",   "CA \u00b7 OR \u00b7 WA",                         32, 49, -125, -114),
    ("US Southeast", "FL \u00b7 GA \u00b7 AL \u00b7 NC \u00b7 SC",    24, 37,  -92,  -74),
    ("US Northeast", "NY \u00b7 PA \u00b7 NE states",                  37, 48,  -80,  -66),
    ("Alaska",       "Southern AK corridor",                            55, 64, -165, -131),
    ("Europe West",  "UK \u00b7 FR \u00b7 DE \u00b7 ES \u00b7 IT",    36, 58,   -9,   15),
]
_TD_COLS_P = 2
_TD_MX_P   = 12
_TD_MY_P   = 84
_TD_GAP_P  = 8


def draw_terrain_data_screen(filename, n_tiles=0, downloading=False,
                              dl_region="", dl_current=0, dl_total=1, dl_status=""):
    img  = Image.new('RGB', (W, H), (0, 8, 22))
    draw = ImageDraw.Draw(img)
    _screen_header_p(draw, "TERRAIN DATA")
    bx = _TD_MX_P; bw = W - 2*_TD_MX_P
    used_mb = n_tiles * 2.8   # approx

    # Status strip
    draw.rounded_rectangle([(bx,52),(bx+bw-1,79)], radius=4, fill=(0,12,32))
    draw.rounded_rectangle([(bx,52),(bx+bw-1,79)], radius=4, outline=(40,60,90), width=1)
    stat_str = (f"{n_tiles} tiles on disk  \u00b7  {used_mb:.0f} MB used"
                if n_tiles else "No tiles on disk  \u00b7  SVT uses flat terrain")
    stat_col = (60,220,80) if n_tiles else YELLOW
    sf = fnt(12, bold=True)
    sw = int(draw.textlength(stat_str, font=sf))
    draw.text(((W-sw)//2, 57), stat_str, fill=stat_col, font=sf)

    rows = (len(_TD_REGIONS_P) + _TD_COLS_P - 1) // _TD_COLS_P
    available_h = H - _TD_MY_P - _TD_GAP_P*(rows-1) - 8
    bh = available_h // (rows + 1)

    # Current area button (full width)
    draw.rounded_rectangle([(bx,_TD_MY_P),(bx+bw-1,_TD_MY_P+bh-1)],
                            radius=6, fill=(0,18,45))
    gh = bh // 5
    for i in range(gh):
        t = 1.0 - i/gh
        draw.line([(bx+6,_TD_MY_P+1+i),(bx+bw-6,_TD_MY_P+1+i)],
                  fill=(int(15+t*25),int(20+t*40),int(40+t*65)))
    draw.rounded_rectangle([(bx,_TD_MY_P),(bx+bw-1,_TD_MY_P+bh-1)],
                            radius=6, outline=WHITE, width=2)
    lf = fnt(15, bold=True)
    lw = int(draw.textlength("DOWNLOAD CURRENT AREA", font=lf))
    draw.text(((W-lw)//2, _TD_MY_P+bh//2-18), "DOWNLOAD CURRENT AREA",
              fill=WHITE, font=lf)
    sf2 = fnt(10)
    area_str = "\u223c25 tiles around current GPS position  \u2248 35 MB"
    aw = int(draw.textlength(area_str, font=sf2))
    draw.text(((W-aw)//2, _TD_MY_P+bh//2+4), area_str, fill=(120,140,165), font=sf2)

    # Preset region grid
    grid_y = _TD_MY_P + bh + _TD_GAP_P
    btn_w  = (bw - _TD_GAP_P) // 2
    for idx, region in enumerate(_TD_REGIONS_P):
        col = idx % _TD_COLS_P; row = idx // _TD_COLS_P
        rx = bx + col*(btn_w+_TD_GAP_P)
        ry = grid_y + row*(bh+_TD_GAP_P)
        label, sub, lat_min, lat_max, lon_min, lon_max = region
        n = (lat_max-lat_min)*(lon_max-lon_min)
        mb = n * 1.5
        is_active = downloading and dl_region == label
        if is_active:
            bg=(0,28,18); oc=(40,180,60); tc=(40,180,60)
        elif downloading:
            bg=(0,8,18); oc=(35,45,60); tc=(70,80,90)
        else:
            bg=(0,12,32); oc=(55,75,105); tc=WHITE
        draw.rounded_rectangle([(rx,ry),(rx+btn_w-1,ry+bh-1)], radius=6, fill=bg)
        if not downloading:
            gh2 = bh // 6
            for i in range(gh2):
                t2 = 1.0-i/gh2
                draw.line([(rx+6,ry+1+i),(rx+btn_w-6,ry+1+i)],
                          fill=(int(15+t2*20),int(20+t2*35),int(40+t2*60)))
        draw.rounded_rectangle([(rx,ry),(rx+btn_w-1,ry+bh-1)], radius=6, outline=oc, width=2)
        lf2 = fnt(14, bold=True)
        lw2 = int(draw.textlength(label, font=lf2))
        draw.text((rx+(btn_w-lw2)//2, ry+bh//2-18), label, fill=tc, font=lf2)
        sf3 = fnt(10); sw3 = int(draw.textlength(sub, font=sf3))
        draw.text((rx+(btn_w-sw3)//2, ry+bh//2-2), sub, fill=(100,120,140), font=sf3)
        cnt_str = f"\u223c{n} tiles  {mb:.0f} MB"
        sf4 = fnt(9); sw4 = int(draw.textlength(cnt_str, font=sf4))
        draw.text((rx+(btn_w-sw4)//2, ry+bh//2+12), cnt_str, fill=(70,85,105), font=sf4)

    # Progress overlay if downloading
    if downloading:
        frac = dl_current / max(1, dl_total)
        prog_y = H - 58
        draw.rounded_rectangle([(bx,prog_y),(bx+bw-1,prog_y+49)],
                                radius=6, fill=(0,12,32))
        draw.rounded_rectangle([(bx,prog_y),(bx+bw-1,prog_y+49)],
                                radius=6, outline=(55,75,105), width=1)
        bar_w = int((bw-20)*frac)
        draw.rounded_rectangle([(bx+10,prog_y+28),(bx+bw-11,prog_y+39)],
                                radius=3, fill=(0,25,12))
        if bar_w:
            draw.rounded_rectangle([(bx+10,prog_y+28),(bx+10+bar_w,prog_y+39)],
                                    radius=3, fill=(40,180,60))
        sf5 = fnt(10)
        draw.text(((W-int(draw.textlength(dl_status,font=sf5)))//2, prog_y+10),
                  dl_status, fill=(140,160,180), font=sf5)
        pct = f"{int(frac*100)}%  ({dl_current}/{dl_total})"
        draw.text(((W-int(draw.textlength(pct,font=sf5)))//2, prog_y+40),
                  pct, fill=(60,220,80), font=sf5)
        _action_btn_p(draw, W-100-bx, prog_y+6, 92, 36, "CANCEL", "danger", r=5)

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

draw_setup_screen(os.path.join(OUT, "preview_setup_main.png"))
draw_flight_profile_screen(os.path.join(OUT, "preview_setup_flight_profile.png"))
draw_keyboard_screen("ENTER TAIL NUMBER", "N12345",
                     os.path.join(OUT, "preview_keyboard.png"),
                     entered="N1234")

draw_numpad_screen("SET ALTITUDE BUG  (\u00d7100 ft)", 85,
                   os.path.join(OUT, "preview_numpad_alt.png"),
                   entered="95", suffix="00")

draw_numpad_screen("SET HEADING BUG", 133,
                   os.path.join(OUT, "preview_numpad_hdg.png"),
                   entered="250")

draw_terrain_data_screen(os.path.join(OUT, "preview_terrain_idle.png"), n_tiles=0)
draw_terrain_data_screen(os.path.join(OUT, "preview_terrain_downloading.png"),
                         n_tiles=12, downloading=True, dl_region="US Southwest",
                         dl_current=47, dl_total=132,
                         dl_status="Downloading N35W111.hgt\u2026")
draw_display_screen(os.path.join(OUT, "preview_setup_display.png"))
draw_ahrs_screen(os.path.join(OUT, "preview_setup_ahrs.png"),
                 ss={"pitch_trim":1.5,"roll_trim":-0.5,"mag_cal":"done","mounting":"normal"})
draw_connectivity_screen(os.path.join(OUT, "preview_setup_connectivity.png"),
                         cs={"ahrs_url":"http://192.168.4.1","wifi_ssid":"AHRS-Link",
                             "wifi_pass":"mypassword",
                             "ahrs_ok":True,"wifi_ok":False,
                             "apply_msg":"WiFi config applied — connecting…",
                             "test_msg": "Reached 192.168.4.1:80 \u2713"})
draw_system_screen(os.path.join(OUT, "preview_setup_system.png"))

print("Done.")
