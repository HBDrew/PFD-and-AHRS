#!/usr/bin/env python3
"""
Generate static PFD preview images at 640x480 (Pi Zero 2W / KLAYERS 3.5" DSI).
Sedona AZ demo scenarios — GI-275 inspired layout.
"""

from PIL import Image, ImageDraw, ImageFont
import math, os

W, H = 640, 480

# ── Layout ────────────────────────────────────────────────────────────────────
SPD_X  = 0;    SPD_W = 74
ALT_W  = 82;   ALT_X = W - ALT_W
HDG_H  = 44;   HDG_Y = H - HDG_H
TAPE_TOP = 22; TAPE_BOT = HDG_Y
TAPE_H   = TAPE_BOT - TAPE_TOP
TAPE_MID = (TAPE_TOP + TAPE_BOT) // 2
CX = W // 2;   CY = TAPE_MID
ROLL_R   = 148; ROLL_CY = ROLL_R + 16
BALL_Y   = HDG_Y - 30
DEG      = math.pi / 180

# ── Sedona palette ────────────────────────────────────────────────────────────
SKY_TOP    = (14,  42,  80)
SKY_HOR    = (58, 130, 200)
ROCK_LIGHT = (192,  82,  38)
ROCK_MID   = (160,  58,  22)
ROCK_DARK  = (110,  36,  12)
VALLEY     = (168,  98,  48)
JUNIPER    = ( 52,  80,  34)
SAND       = (196, 158,  88)
WHITE      = (255, 255, 255)
YELLOW     = (255, 215,   0)
CYAN       = (  0, 230, 230)
RED        = (220,  30,  30)
GREEN_ARC  = ( 30, 200,  50)
YELLOW_ARC = (240, 200,   0)

def lerp_color(a, b, t):
    return tuple(int(a[i] + (b[i]-a[i])*t) for i in range(3))

def draw_scene(roll, pitch, hdg, alt, speed, vspeed, ay,
               hdg_bug, alt_bug, label, filename):

    img  = Image.new('RGB', (W, H), (0, 0, 0))
    draw = ImageDraw.Draw(img)

    # ── 1. SKY / GROUND (rotated with roll) ───────────────────────────────────
    ai_img  = Image.new('RGBA', (W*4, H*4), (0, 0, 0, 0))
    ai_draw = ImageDraw.Draw(ai_img)

    focal    = 260.0
    pitch_px = focal * math.tan(pitch * DEG)
    # horizon y in enlarged canvas
    hy = H*2 + int(-pitch_px)

    # Sky gradient
    for y in range(hy):
        t   = max(0, min(1, 1 - y / (H*2)))
        col = lerp_color(SKY_HOR, SKY_TOP, t)
        ai_draw.line([(0, y), (W*4, y)], fill=col + (255,))

    # Ground — Sedona-colored layers
    for y in range(hy, H*4):
        depth = (y - hy) / (H*2)
        t     = min(1, depth * 1.5)
        if depth < 0.08:
            col = lerp_color(VALLEY, ROCK_MID, depth / 0.08)
        elif depth < 0.35:
            td = (depth - 0.08) / 0.27
            col = lerp_color(ROCK_MID, ROCK_LIGHT, td)
        else:
            col = lerp_color(ROCK_LIGHT, SAND, min(1,(depth-0.35)/0.4))
        ai_draw.line([(0, y), (W*4, y)], fill=col + (255,))

    # Terrain texture — canyon features, junipers
    import random; random.seed(42)
    for _ in range(280):
        tx = random.randint(0, W*4)
        ty = random.randint(hy + 20, H*4 - 10)
        tw = random.randint(6, 60)
        th = random.randint(3, 18)
        kind = random.random()
        if kind < 0.25:
            c = JUNIPER + (180,)
        elif kind < 0.55:
            c = ROCK_DARK + (140,)
        else:
            c = ROCK_LIGHT + (100,)
        ai_draw.ellipse([(tx, ty), (tx+tw, ty+th)], fill=c)

    # Ridgeline silhouette in distance
    pts = []
    ridge_y = hy + 28
    for x in range(0, W*4, 6):
        ny = ridge_y + int(18*math.sin(x*0.007) + 12*math.sin(x*0.019) +
                            8*math.sin(x*0.041))
        pts.extend([(x, ny)])
    for i in range(len(pts)-1):
        ai_draw.line([pts[i], pts[i+1]], fill=ROCK_DARK+(220,), width=3)

    # Horizon line
    ai_draw.line([(0, hy), (W*4, hy)], fill=(255,255,255,220), width=2)

    # Pitch ladder
    try:
        fnt_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 18)
    except:
        fnt_small = ImageFont.load_default()
    for deg in range(-30, 35, 5):
        if deg == 0: continue
        ly   = hy - int(focal * math.tan(deg * DEG))
        half = int(W*4 * (0.18 if deg % 10 == 0 else 0.10))
        lw   = 2 if deg % 10 == 0 else 1
        ai_draw.line([(W*2 - half, ly), (W*2 + half, ly)],
                     fill=(255,255,255,230), width=lw)
        td = 1 if deg > 0 else -1
        ai_draw.line([(W*2 - half, ly), (W*2 - half, ly + td*10)],
                     fill=(255,255,255,200), width=lw)
        ai_draw.line([(W*2 + half, ly), (W*2 + half, ly + td*10)],
                     fill=(255,255,255,200), width=lw)
        if deg % 10 == 0:
            ai_draw.text((W*2 - half - 46, ly - 10),
                         str(abs(deg)), fill=(255,255,255,230), font=fnt_small)
            ai_draw.text((W*2 + half + 8, ly - 10),
                         str(abs(deg)), fill=(255,255,255,230), font=fnt_small)

    # Rotate and crop AI
    ai_rot = ai_img.rotate(-roll, center=(W*2, H*2))
    ai_crop = ai_rot.crop((W*2 - CX, H*2 - CY, W*2 + (W-CX), H*2 + (H-CY)))
    ai_rgb = ai_crop.convert('RGB')
    img.paste(ai_rgb, (0, 0))

    draw = ImageDraw.Draw(img)

    # ── 2. TAPES — semi-transparent overlay ───────────────────────────────────
    tape_overlay = Image.new('RGBA', (W, H), (0,0,0,0))
    to = ImageDraw.Draw(tape_overlay)

    # Speed tape
    to.rectangle([(SPD_X, TAPE_TOP), (SPD_X+SPD_W, TAPE_BOT)],
                 fill=(0, 8, 25, 175))
    to.line([(SPD_X+SPD_W, TAPE_TOP), (SPD_X+SPD_W, TAPE_BOT)],
            fill=(255,255,255,60), width=1)

    # Alt tape
    to.rectangle([(ALT_X, TAPE_TOP), (ALT_X+ALT_W, TAPE_BOT)],
                 fill=(0, 8, 25, 175))
    to.line([(ALT_X, TAPE_TOP), (ALT_X, TAPE_BOT)],
            fill=(255,255,255,60), width=1)

    # Heading tape
    to.rectangle([(0, HDG_Y), (W, H)], fill=(0, 8, 22, 210))
    to.line([(0, HDG_Y), (W, HDG_Y)], fill=(255,255,255,80), width=1)

    img = Image.alpha_composite(img.convert('RGBA'), tape_overlay).convert('RGB')
    draw = ImageDraw.Draw(img)

    try:
        fnt      = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 16)
        fnt_sm   = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 13)
        fnt_lg   = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 22)
        fnt_xl   = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 26)
        fnt_hdr  = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 11)
    except:
        fnt = fnt_sm = fnt_lg = fnt_xl = fnt_hdr = ImageFont.load_default()

    # ── 3. SPEED TAPE content ─────────────────────────────────────────────────
    draw.text((SPD_X+4, TAPE_TOP+3), "GS KT", fill=(140,200,255), font=fnt_hdr)

    # V-speed arcs (colored bars on right edge of speed tape)
    PX_PER_KT = TAPE_H / 150.0
    def spd_y(kt): return TAPE_MID - int((kt - speed) * PX_PER_KT)
    # Green arc Vs1-Vno (48-129)
    y1g = spd_y(129); y2g = spd_y(48)
    if y1g < TAPE_BOT and y2g > TAPE_TOP:
        draw.line([(SPD_X+SPD_W-4, max(y1g,TAPE_TOP)),
                   (SPD_X+SPD_W-4, min(y2g,TAPE_BOT))],
                  fill=GREEN_ARC, width=4)
    # White arc Vso-Vfe (40-85)
    y1w = spd_y(85); y2w = spd_y(40)
    if y1w < TAPE_BOT and y2w > TAPE_TOP:
        draw.line([(SPD_X+SPD_W-9, max(y1w,TAPE_TOP)),
                   (SPD_X+SPD_W-9, min(y2w,TAPE_BOT))],
                  fill=WHITE, width=3)
    # Yellow arc Vno-Vne (129-163)
    y1y = spd_y(163); y2y = spd_y(129)
    if y1y < TAPE_BOT and y2y > TAPE_TOP:
        draw.line([(SPD_X+SPD_W-4, max(y1y,TAPE_TOP)),
                   (SPD_X+SPD_W-4, min(y2y,TAPE_BOT))],
                  fill=YELLOW_ARC, width=4)
    # Red line Vne (163)
    yvne = spd_y(163)
    if TAPE_TOP < yvne < TAPE_BOT:
        draw.line([(SPD_X+SPD_W-14, yvne), (SPD_X+SPD_W, yvne)],
                  fill=RED, width=3)

    spd_base = round(speed / 20) * 20
    for v in range(spd_base - 120, spd_base + 120, 20):
        if v < 0: continue
        sy = spd_y(v)
        if TAPE_TOP + 20 < sy < TAPE_BOT - 20:
            draw.line([(SPD_X+SPD_W-14, sy), (SPD_X+SPD_W, sy)],
                      fill=(255,255,255,200), width=1)
            draw.text((SPD_X+2, sy-8), f"{v:3d}", fill=(230,230,230), font=fnt)

    # Speed box
    bh = 32; by = TAPE_MID - bh//2
    pts = [(SPD_X, by), (SPD_X+SPD_W, by),
           (SPD_X+SPD_W+9, TAPE_MID),
           (SPD_X+SPD_W, by+bh), (SPD_X, by+bh)]
    draw.polygon(pts, fill=(0,10,30))
    draw.line(pts + [pts[0]], fill=WHITE, width=2)
    draw.text((SPD_X+4, TAPE_MID-13), f"{round(speed):3d}",
              fill=WHITE, font=fnt_xl)

    # ── 4. ALT TAPE content ───────────────────────────────────────────────────
    draw.text((ALT_X+8, TAPE_TOP+3), "ALT FT", fill=(140,200,255), font=fnt_hdr)

    PX_PER_FT = TAPE_H / 600.0
    def alt_y(ft): return TAPE_MID - int((ft - alt) * PX_PER_FT)

    base_alt = round(alt / 100) * 100
    for ft in range(base_alt - 300, base_alt + 300, 100):
        ay2 = alt_y(ft)
        if TAPE_TOP + 20 < ay2 < TAPE_BOT - 20:
            maj = ft % 500 == 0
            tl  = 14 if maj else 7
            draw.line([(ALT_X, ay2), (ALT_X+tl, ay2)],
                      fill=(255,255,255, 200 if maj else 120), width=2 if maj else 1)
            if ft % 200 == 0:
                draw.text((ALT_X+tl+3, ay2-8), f"{ft}",
                          fill=(230,230,230), font=fnt_sm)

    # Altitude bug
    ab_y = alt_y(alt_bug)
    if TAPE_TOP < ab_y < TAPE_BOT:
        bug_pts = [(ALT_X, ab_y-10), (ALT_X+18, ab_y-10),
                   (ALT_X+24, ab_y), (ALT_X+18, ab_y+10), (ALT_X, ab_y+10)]
        draw.polygon(bug_pts, fill=CYAN)

    # Alt box
    bh = 32; by = TAPE_MID - bh//2
    pts = [(ALT_X+ALT_W, by), (ALT_X, by),
           (ALT_X-9, TAPE_MID),
           (ALT_X, by+bh), (ALT_X+ALT_W, by+bh)]
    draw.polygon(pts, fill=(0,10,30))
    draw.line(pts + [pts[0]], fill=WHITE, width=2)
    draw.text((ALT_X+4, TAPE_MID-13), f"{round(alt):5d}",
              fill=WHITE, font=fnt_lg)

    # VSI below alt box
    arrow = "▲" if vspeed > 30 else ("▼" if vspeed < -30 else "—")
    vcol  = (0,220,0) if vspeed > 100 else ((255,140,0) if vspeed < -100 else (160,160,160))
    draw.text((ALT_X+6, TAPE_MID+22), f"{arrow}{abs(round(vspeed/10)*10)}",
              fill=vcol, font=fnt_sm)
    draw.text((ALT_X+18, TAPE_MID+38), "fpm", fill=(120,160,200), font=fnt_hdr)

    # QNH below VSI
    draw.text((ALT_X+6, TAPE_MID+54), "29.92", fill=(80,200,255), font=fnt_hdr)
    draw.text((ALT_X+14, TAPE_MID+67), "hPa B", fill=(80,200,255), font=fnt_hdr)

    # ── 5. HEADING TAPE ───────────────────────────────────────────────────────
    CARDS = {0:'N',45:'NE',90:'E',135:'SE',180:'S',225:'SW',270:'W',315:'NW'}
    PX_PER_DEG = W / 60.0
    for i in range(-34, 35):
        deg = int((round(hdg) + i + 3600)) % 360
        off = i - (hdg - round(hdg))
        x   = int(CX + off * PX_PER_DEG)
        if not (0 < x < W): continue
        if deg % 5 == 0:
            th = int(HDG_H * (0.38 if deg % 10 == 0 else 0.20))
            draw.line([(x, HDG_Y), (x, HDG_Y+th)],
                      fill=(200,200,200), width=2 if deg%10==0 else 1)
        if deg % 10 == 0:
            lbl = CARDS.get(deg, f"{deg:03d}")
            col = YELLOW if deg in CARDS else (230,230,230)
            draw.text((x-8, HDG_Y+HDG_H-16), lbl, fill=col, font=fnt_sm)

    # Heading bug
    hb_off  = ((hdg_bug - hdg + 180) % 360) - 180
    hb_x    = int(CX + hb_off * PX_PER_DEG)
    if 0 < hb_x < W:
        bug_pts = [(hb_x-8, HDG_Y), (hb_x+8, HDG_Y),
                   (hb_x+8, HDG_Y+10), (hb_x+4, HDG_Y+18),
                   (hb_x-4, HDG_Y+18), (hb_x-8, HDG_Y+10)]
        draw.polygon(bug_pts, fill=CYAN)

    # Heading box
    bw = 60; bh2 = 22
    draw.rectangle([(CX-bw//2, HDG_Y-bh2-2), (CX+bw//2, HDG_Y-2)],
                   fill=(0,0,0))
    draw.rectangle([(CX-bw//2, HDG_Y-bh2-2), (CX+bw//2, HDG_Y-2)],
                   outline=WHITE, width=1)
    draw.text((CX-22, HDG_Y-bh2+2), f"{round(hdg):03d}°",
              fill=WHITE, font=fnt_lg)

    # Heading bug triangle pointer at top
    draw.polygon([(CX-7, HDG_Y-1), (CX+7, HDG_Y-1), (CX, HDG_Y-10)],
                 fill=YELLOW)

    # ── 6. ROLL INDICATOR ─────────────────────────────────────────────────────
    # Arc
    for a in range(-150, -29):
        ax1 = CX + int(ROLL_R * math.cos(a*DEG))
        ay1 = ROLL_CY + int(ROLL_R * math.sin(a*DEG))
        ax2 = CX + int(ROLL_R * math.cos((a+1)*DEG))
        ay2 = ROLL_CY + int(ROLL_R * math.sin((a+1)*DEG))
        draw.line([(ax1,ay1),(ax2,ay2)], fill=(200,200,200), width=2)

    for deg2 in [0,10,20,30,45,60,-10,-20,-30,-45,-60]:
        ang = (-90 + deg2) * DEG
        l2  = 18 if deg2==0 else (14 if abs(deg2)==30 else 10)
        x1  = CX + int((ROLL_R-l2)*math.cos(ang))
        y1  = ROLL_CY + int((ROLL_R-l2)*math.sin(ang))
        x2  = CX + int(ROLL_R*math.cos(ang))
        y2  = ROLL_CY + int(ROLL_R*math.sin(ang))
        draw.line([(x1,y1),(x2,y2)], fill=(200,200,200),
                  width=2 if deg2==0 else 1)

    # Roll pointer
    ra  = (-90 - roll) * DEG
    rpx = CX + int((ROLL_R-3)*math.cos(ra))
    rpy = ROLL_CY + int((ROLL_R-3)*math.sin(ra))
    ang2 = ra + math.pi/2
    tx = int(10*math.cos(ang2)); ty = int(10*math.sin(ang2))
    draw.polygon([(rpx, rpy),
                  (rpx - tx - int(6*math.cos(ra)),
                   rpy - ty - int(6*math.sin(ra))),
                  (rpx + tx - int(6*math.cos(ra)),
                   rpy + ty - int(6*math.sin(ra)))],
                 fill=WHITE)

    # ── 7. AIRCRAFT SYMBOL ────────────────────────────────────────────────────
    ws = 80
    draw.line([(CX-ws, TAPE_MID), (CX-int(ws*.22), TAPE_MID)],
              fill=YELLOW, width=3)
    draw.line([(CX-int(ws*.22), TAPE_MID),
               (CX-int(ws*.22), TAPE_MID+9)], fill=YELLOW, width=3)
    draw.line([(CX+ws, TAPE_MID), (CX+int(ws*.22), TAPE_MID)],
              fill=YELLOW, width=3)
    draw.line([(CX+int(ws*.22), TAPE_MID),
               (CX+int(ws*.22), TAPE_MID+9)], fill=YELLOW, width=3)
    draw.ellipse([(CX-5, TAPE_MID-5), (CX+5, TAPE_MID+5)], fill=YELLOW)

    # ── 8. SLIP BALL ──────────────────────────────────────────────────────────
    bw2=54; bh3=16; r=6
    draw.rounded_rectangle([(CX-bw2//2, BALL_Y-bh3//2),
                             (CX+bw2//2, BALL_Y+bh3//2)],
                            radius=bh3//2, fill=(0,0,0,160),
                            outline=(200,200,200))
    mk = r + 4
    draw.line([(CX-mk, BALL_Y-bh3//2+2), (CX-mk, BALL_Y+bh3//2-2)],
              fill=WHITE, width=2)
    draw.line([(CX+mk, BALL_Y-bh3//2+2), (CX+mk, BALL_Y+bh3//2-2)],
              fill=WHITE, width=2)
    defl = max(-1,min(1, ay/0.2)) * (bw2//2 - r - 2)
    draw.ellipse([(CX+int(defl)-r, BALL_Y-r),
                  (CX+int(defl)+r, BALL_Y+r)], fill=WHITE)

    # ── 9. GPS STATUS ─────────────────────────────────────────────────────────
    draw.text((SPD_X+SPD_W+6, TAPE_TOP+2), "GPS 8sat",
              fill=(0,200,0), font=fnt_hdr)

    # ── 10. STATUS BADGES ─────────────────────────────────────────────────────
    def badge(x, y, txt, bg, fg=(255,255,255)):
        tw  = len(txt)*7 + 8
        draw.rectangle([(x, y), (x+tw, y+16)], fill=bg)
        draw.text((x+4, y+2), txt, fill=fg, font=fnt_hdr)
        return x - tw - 4

    bx = W - 4
    bx = badge(bx - len("LINK OK")*7-8, 4, "LINK OK", (0,140,0))
    bx = badge(bx - len("TERRAIN OK")*7-8, 4, "TERRAIN OK", (0,100,50))
    bx = badge(bx - len("QNH 2992")*7-8, 4, "QNH 2992", (0,50,110))
    badge(bx - len("TRIM")*7-8, 4, "TRIM", (50,40,0), (255,210,100))

    # ── 11. DEMO / SCENARIO LABEL ─────────────────────────────────────────────
    lw = len(label)*7 + 16
    draw.rectangle([(CX-lw//2, H-HDG_H-20), (CX+lw//2, H-HDG_H-4)],
                   fill=(0,0,0,180))
    draw.text((CX-lw//2+8, H-HDG_H-18), label,
              fill=(255,210,60), font=fnt_hdr)

    # DEMO watermark
    draw.text((CX-28, TAPE_MID-8), "DEMO",
              fill=(255,60,60), font=fnt_hdr)

    img.save(filename)
    print(f"Saved {filename}")


# ── Render 3 scenarios ────────────────────────────────────────────────────────
os.makedirs('/home/user/PFD-and-AHRS/tools', exist_ok=True)

draw_scene(
    roll=0, pitch=2, hdg=133, alt=8500, speed=115,
    vspeed=0, ay=0, hdg_bug=133, alt_bug=8500,
    label="Sedona Valley — Level cruise SE at 8,500 ft",
    filename="/home/user/PFD-and-AHRS/tools/preview_sedona_level.png"
)

draw_scene(
    roll=-18, pitch=4, hdg=218, alt=7200, speed=108,
    vspeed=650, ay=-0.08, hdg_bug=250, alt_bug=9500,
    label="Sedona — Climbing left turn, departing NW",
    filename="/home/user/PFD-and-AHRS/tools/preview_sedona_climb_turn.png"
)

draw_scene(
    roll=0, pitch=-3, hdg=19, alt=6200, speed=90,
    vspeed=-500, ay=0, hdg_bug=19, alt_bug=4900,
    label="Sedona (KSEZ) — Descending final Rwy 03",
    filename="/home/user/PFD-and-AHRS/tools/preview_sedona_approach.png"
)

print("Done.")
