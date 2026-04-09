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


def load_fonts():
    sizes = [10, 11, 13, 14, 15, 16, 18, 20, 22, 26]
    fonts = {}
    base = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono"
    for sz in sizes:
        for bold in (False, True):
            suffix = "-Bold" if bold else ""
            try:
                fonts[(sz, bold)] = ImageFont.truetype(f"{base}{suffix}.ttf", sz)
            except Exception:
                fonts[(sz, bold)] = ImageFont.load_default()
    return fonts

FNT = load_fonts()

def fnt(size, bold=False):
    return FNT.get((size, bold), FNT.get((size, False), ImageFont.load_default()))


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

    focal    = 260.0
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

    # Pitch ladder — half in display pixels (canvas 1:1 with output)
    major_half = int(AI_W * 0.22)   # ~108 px
    minor_half = int(AI_W * 0.13)   # ~64 px
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

    # Speed tape bg
    od.rectangle([(SPD_X, TAPE_TOP), (SPD_X+SPD_W, TAPE_BOT)], fill=TAPE_FILL)
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
        draw.line([(SPD_X+SPD_W-tl, vy), (SPD_X+SPD_W, vy)],
                  fill=LTGREY, width=2 if major else 1)
        if major:
            draw.text((SPD_X+2, vy-9), str(v), fill=(230,230,230), font=fnt(17, bold=True))

    # Speed box (pentagon pointing right)
    bh = 36; by = TAPE_MID - bh//2
    pts = [(SPD_X, by), (SPD_X+SPD_W, by),
           (SPD_X+SPD_W+12, TAPE_MID),
           (SPD_X+SPD_W, by+bh), (SPD_X, by+bh)]
    draw.polygon(pts, fill=(0, 10, 30))
    draw.line(pts + [pts[0]], fill=WHITE, width=2)
    spd_col = RED if speed > VNE else (YELLOW if speed > VNO else WHITE)
    draw.text((SPD_X+3, TAPE_MID-14), f"{round(speed):3d}",
              fill=spd_col, font=fnt(26, bold=True))

    # Header
    draw.text((SPD_X+3, TAPE_TOP+2), "GS KT", fill=(140,200,255), font=fnt(10))

    # ── 4. ALT TAPE CONTENT ───────────────────────────────────────────────────
    def alt_y(ft): return int(TAPE_MID - (ft - alt) * PX_PER_FT)

    # Selected altitude (cyan, top strip)
    draw.text((ALT_X + ALT_W//2 - 22, 5),
              f"{round(alt_bug):5d}", fill=CYAN, font=fnt(16, bold=True))

    # Tick marks + labels
    base_a = round(alt / 100) * 100
    for ft in range(base_a - 400, base_a + 400, 100):
        fy = alt_y(ft)
        if not (TAPE_TOP + 12 < fy < TAPE_BOT - 12): continue
        major = (ft % 500 == 0)
        tl = 14 if major else 7
        draw.line([(ALT_X, fy), (ALT_X+tl, fy)],
                  fill=LTGREY, width=2 if major else 1)
        if ft % 200 == 0:
            draw.text((ALT_X+tl+2, fy-8), str(ft),
                      fill=(230,230,230), font=fnt(15, bold=True))

    # Altitude bug
    aby = alt_y(alt_bug)
    if TAPE_TOP < aby < TAPE_BOT:
        bug = [(ALT_X, aby-10), (ALT_X+20, aby-10),
               (ALT_X+26, aby), (ALT_X+20, aby+10), (ALT_X, aby+10)]
        draw.polygon(bug, fill=CYAN)

    # Alt box (pentagon pointing left)
    bh = 36; by = TAPE_MID - bh//2
    pts = [(ALT_X+ALT_W, by), (ALT_X, by),
           (ALT_X-12, TAPE_MID),
           (ALT_X, by+bh), (ALT_X+ALT_W, by+bh)]
    draw.polygon(pts, fill=(0, 10, 30))
    draw.line(pts + [pts[0]], fill=WHITE, width=2)
    draw.text((ALT_X+3, TAPE_MID-14), f"{round(alt):5d}",
              fill=WHITE, font=fnt(24, bold=True))

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

    # Header
    draw.text((ALT_X+6, TAPE_TOP+2), "ALT FT", fill=(140,200,255), font=fnt(10))

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

    # ── 6. ROLL ARC ──────────────────────────────────────────────────────────
    cx2, cy2 = CX, ROLL_CY
    # Arc
    for a in range(-150, -29):
        a1, a2 = a*DEG, (a+1)*DEG
        x1,y1 = int(cx2+ROLL_R*math.cos(a1)), int(cy2+ROLL_R*math.sin(a1))
        x2,y2 = int(cx2+ROLL_R*math.cos(a2)), int(cy2+ROLL_R*math.sin(a2))
        draw.line([(x1,y1),(x2,y2)], fill=LTGREY, width=2)

    # Tick marks
    for deg2, l2 in [(0,18),(10,10),(20,10),(30,14),(-10,10),(-20,10),(-30,14),
                      (45,10),(-45,10),(60,12),(-60,12)]:
        ang = (-90+deg2)*DEG
        x1=int(cx2+(ROLL_R-l2)*math.cos(ang)); y1=int(cy2+(ROLL_R-l2)*math.sin(ang))
        x2=int(cx2+ROLL_R*math.cos(ang));      y2=int(cy2+ROLL_R*math.sin(ang))
        draw.line([(x1,y1),(x2,y2)], fill=LTGREY, width=2 if deg2==0 else 1)

    # Doghouse pentagon helper
    def doghouse_pts(ang_rad, r, size=11):
        out_x = math.cos(ang_rad); out_y = math.sin(ang_rad)
        perp_x = -out_y;           perp_y =  out_x
        base_r = r + size*1.3;  roof_r = r + size*0.6
        half_w = size*0.7;      roof_hw = size*0.35
        return [
            (int(cx2 + base_r*out_x - half_w*perp_x),
             int(cy2 + base_r*out_y - half_w*perp_y)),
            (int(cx2 + roof_r*out_x - roof_hw*perp_x),
             int(cy2 + roof_r*out_y - roof_hw*perp_y)),
            (int(cx2 + r*out_x), int(cy2 + r*out_y)),   # tip (points inward)
            (int(cx2 + roof_r*out_x + roof_hw*perp_x),
             int(cy2 + roof_r*out_y + roof_hw*perp_y)),
            (int(cx2 + base_r*out_x + half_w*perp_x),
             int(cy2 + base_r*out_y + half_w*perp_y)),
        ]

    # Fixed zero-bank doghouse (white, at top of arc)
    zero_ang = -math.pi / 2
    dh0 = doghouse_pts(zero_ang, ROLL_R, size=11)
    draw.polygon(dh0, fill=WHITE)
    draw.line(dh0 + [dh0[0]], fill=(80,80,90), width=1)

    # Moving roll pointer doghouse (white)
    ra = (-90 - roll) * DEG
    dh1 = doghouse_pts(ra, ROLL_R - 2, size=10)
    draw.polygon(dh1, fill=WHITE)
    draw.line(dh1 + [dh1[0]], fill=(40,40,50), width=1)

    # ── 7. AIRCRAFT SYMBOL (shaded amber, GI-275 style) ──────────────────────
    AMBER      = (255, 190,  30)
    AMBER_DARK = (180, 120,   0)
    ws = 72; hw = int(ws * 0.22)
    # Left wing — dark layer then bright highlight
    lwing_d = [(CX-ws, CY-1),(CX-hw,CY-1),(CX-hw,CY+6),(CX-ws,CY+4)]
    lwing_h = [(CX-ws, CY-3),(CX-hw,CY-3),(CX-hw,CY+4),(CX-ws,CY+2)]
    draw.polygon(lwing_d, fill=AMBER_DARK)
    draw.polygon(lwing_h, fill=AMBER)
    # Right wing
    rwing_d = [(CX+hw,CY-1),(CX+ws,CY-1),(CX+ws,CY+4),(CX+hw,CY+6)]
    rwing_h = [(CX+hw,CY-3),(CX+ws,CY-3),(CX+ws,CY+2),(CX+hw,CY+4)]
    draw.polygon(rwing_d, fill=AMBER_DARK)
    draw.polygon(rwing_h, fill=AMBER)
    # Wing-tip down-ticks
    draw.line([(CX-ws, CY+4),(CX-ws, CY+12)], fill=AMBER_DARK, width=4)
    draw.line([(CX-ws, CY+2),(CX-ws, CY+10)], fill=AMBER,      width=2)
    draw.line([(CX+ws, CY+4),(CX+ws, CY+12)], fill=AMBER_DARK, width=4)
    draw.line([(CX+ws, CY+2),(CX+ws, CY+10)], fill=AMBER,      width=2)
    # Centre hub
    draw.ellipse([(CX-7, CY-7),(CX+7, CY+7)], fill=AMBER_DARK)
    draw.ellipse([(CX-5, CY-5),(CX+5, CY+5)], fill=AMBER)
    draw.ellipse([(CX-2, CY-2),(CX+2, CY+2)], fill=WHITE)

    # ── 8. CYAN TAP-BUTTONS (HDG / QNH / ALT bug) ───────────────────────────
    def cyan_box(label, value_str, bx, by, bw=84, bh=20):
        draw.rectangle([(bx, by), (bx+bw, by+bh)], fill=(0, 20, 35))
        draw.rectangle([(bx, by), (bx+bw, by+bh)], outline=CYAN, width=1)
        txt = f"{label} {value_str}"
        draw.text((bx + bw//2 - len(txt)*4, by + 4), txt,
                  fill=CYAN, font=fnt(13, bold=True))

    btn_y = HDG_Y + 2   # sit inside the heading stripe
    cyan_box("HDG", f"{round(hdg_bug):03d}\u00b0", SPD_X,      btn_y, bw=SPD_W+10)
    cyan_box("QNH", "GPS ALT",                      CX-50,      btn_y, bw=100)
    cyan_box("ALT", f"{round(alt_bug):5d}",         ALT_X-10,   btn_y, bw=ALT_W+10)

    # ── 9. SLIP BALL ─────────────────────────────────────────────────────────
    bw3=52; bh3=16; br=8
    bx3=CX-bw3//2; by3=BALL_Y-bh3//2
    draw.rounded_rectangle([(bx3, by3), (bx3+bw3, by3+bh3)],
                            radius=bh3//2, fill=(0,0,0), outline=(100,100,110))
    mk = br+4
    draw.line([(CX-mk, by3+2),(CX-mk, by3+bh3-2)], fill=WHITE, width=2)
    draw.line([(CX+mk, by3+2),(CX+mk, by3+bh3-2)], fill=WHITE, width=2)
    max_d = bw3//2 - br - 2
    defl  = int(max(-max_d, min(max_d, (ay/0.2)*max_d)))
    draw.ellipse([(CX+defl-br, BALL_Y-br),(CX+defl+br, BALL_Y+br)], fill=WHITE)

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
