"""
case.py – parametric handheld case for the Pi Zero 2W + Waveshare 3.5" DPI LCD.

Run:
    python3 case.py            # writes case.step / .stl

Target stack (from refs/3_5inch-dpi-lcd.stp):
    - Waveshare 3.5" DPI LCD, 65 x 77 x ~10 mm
        * PCB 65 x 77 x 1.6 mm
        * Two M2.5 brass standoffs at (12.28, 15.59) and (12.28, 73.60)
        * 40-pin male GPIO header along the +X long edge, extending ~12 mm
          below the PCB bottom into a Pi Zero on the back side.
        * Cover glass top sits at +7.6 mm above PCB top.
    - Raspberry Pi Zero 2W (65 x 30 x ~5 mm), piggy-backed via the 40-pin
      header. Two micro-USB ports (PWR + OTG) face the +Y "top" of the case
      so cables exit at the top long edge.

Design intent — trip-grade, single-piece back tray, FDM-PLA:
    - Open front: case walls come up flush with the cover glass so the
      touchscreen is fully exposed but the glass edge is protected.
    - Display secured by 2x M2.5 screws up through the back into the
      existing brass posts. No extra inserts needed.
    - Pi Zero pocketed under the display, captured by its 40-pin header
      mate + 4x M2.5 self-tap screws into integrated standoffs.
    - USB PWR/OTG cutout on the +Y wall.
    - microSD slot on the -Y wall.
    - Ventilation slats on the back over the Pi Zero SoC.
    - Fixed integrated kickstand feet on the back (two triangular ribs)
      so it stands at ~17 deg on a table.

Hardware to grab (from the M2.5 assortment kit):
    - 2x M2.5x16  -- display brass-post screws, inserted from the BACK of
                     the case, head buried in the floor counterbore.
                     Threads into the female bore inside the brass post.
    - 4x M2.5x8   -- Pi Zero corner screws, inserted from the FRONT (the
                     open top of the case), self-tap into the printed
                     standoff pilot bores.

Coordinate convention (all dimensions in mm):
    +X = right (display long edge, GPIO-header side)
    +Y = top (cable side)
    +Z = forward, out the screen (toward the pilot's face)

PCB-frame origin matches the STEP file (PCB corner at 0,0). The case is
modelled in the same frame so STEP positions map directly.
"""

import cadquery as cq

# ─── Display board (from STEP) ───────────────────────────────────────
DISP_X = 65.0
DISP_Y = 77.0
DISP_T = 1.6                    # PCB thickness
GLASS_TOP_Z = 7.6               # cover-glass top above PCB top
GLASS_INSET = 0.5               # cover glass inset from PCB edge (per STEP)
GLASS_X = DISP_X - 2 * GLASS_INSET
GLASS_Y = DISP_Y - 2 * GLASS_INSET

# Two brass mounting posts on the -X long edge.
DISP_HOLES = [
    (12.28, 15.59),
    (12.28, 73.60),
]
DISP_POST_OD = 5.5              # brass standoff OD (we clear around it)
DISP_SCREW   = 2.5              # M2.5 thread
DISP_SCREW_CLEAR = 2.9          # M2.5 clearance bore

# 40-pin male header strip on the +X long edge of the display PCB.
# Hangs ~12 mm below PCB bottom (per STEP bounding box, -14.1 mm).
HEADER_X_MIN = 58.0             # mate-area extends roughly from X=58..65
HEADER_Y_MIN = 8.5              # 40-pin block ~58 mm along Y
HEADER_Y_MAX = 68.5
HEADER_DROP  = 12.0             # how far the header sticks out the back of PCB

# Inter-board spacing forced by the header: top of Pi Zero PCB sits this
# far below the display PCB bottom. Standard Pi-shield stack ~ 8.5 mm.
STACK_GAP = 8.5

# ─── Pi Zero 2W (standard) ───────────────────────────────────────────
PI_X = 65.0                     # long axis
PI_Y = 30.0                     # short axis
PI_T = 1.4                      # PCB thickness
# Pi Zero mounting holes — corners, 58 x 23 spacing, 3.5 mm inset, Ø2.75
PI_HOLE_INSET_X = 3.5
PI_HOLE_INSET_Y = 3.5
PI_HOLES_LOCAL = [
    (PI_HOLE_INSET_X,            PI_HOLE_INSET_Y),
    (PI_X - PI_HOLE_INSET_X,     PI_HOLE_INSET_Y),
    (PI_HOLE_INSET_X,            PI_Y - PI_HOLE_INSET_Y),
    (PI_X - PI_HOLE_INSET_X,     PI_Y - PI_HOLE_INSET_Y),
]
PI_SCREW = 2.5

# Pi Zero placement in display coords: the 40-pin female header on the Pi
# sits under the male header on the display (X = 58..65). The header is
# centred ~3.5 mm in from one long edge of the Pi Zero, so the Pi Zero's
# long edge that holds the header is at the display's +X side. The other
# long edge of the Pi Zero (with the USBs) hangs OFF the display board on
# the +Y side per the user's "cables out the top" choice — but wait, the
# Pi Zero's USBs are on a SHORT edge, not a long edge.
#
# Pi Zero short edge with USBs (PWR + OTG) faces +Y (top). The Pi Zero's
# 65 mm long axis runs along the X direction of the display, parallel to
# the display's long axis. To put the USB short edge at +Y, the Pi Zero
# is rotated 90 deg from the display, so its X axis (long) lies along the
# display's Y axis.
#
# In display-frame coords, the Pi Zero occupies:
#   X in [HEADER_X_MIN - PI_Y + GPIO_INSET, HEADER_X_MIN + GPIO_INSET]
#     i.e. the Pi Zero's 30-mm short axis lies along the display X axis,
#     aligned so the GPIO row sits over the display's header region.
#   Y in [PI_Y_OFFSET, PI_Y_OFFSET + PI_X]
#     i.e. the Pi Zero's 65-mm long axis runs in display Y.
#
# We position the Pi Zero so its +Y (USB) short edge points at +Y of the
# display (top of the case).
GPIO_INSET = 3.5                # GPIO header is ~3.5 mm in from Pi Zero edge

# Pi Zero short axis (30 mm) in display X. Place so the GPIO row aligns
# with the display header column (X = HEADER_X_MIN..65).
PI_X0_DISP = HEADER_X_MIN - (PI_Y - GPIO_INSET)   # left edge of Pi Zero in display X
PI_X1_DISP = PI_X0_DISP + PI_Y                    # right edge of Pi Zero in display X

# Pi Zero long axis (65 mm) along display Y. Aligned so its +Y short edge
# is at the +Y top, with a small margin from the display top edge.
PI_TOP_MARGIN = 1.0                               # gap between Pi Zero top edge and display top edge
PI_Y1_DISP = DISP_Y - PI_TOP_MARGIN               # top edge of Pi Zero (where USBs are)
PI_Y0_DISP = PI_Y1_DISP - PI_X                    # bottom edge of Pi Zero

# Pi Zero mounting holes in display-frame coords (after the 90-deg rotation).
# Pi local (x, y) → display (PI_X0_DISP + y, PI_Y0_DISP + x)
PI_HOLES_DISP = [
    (PI_X0_DISP + ly, PI_Y0_DISP + lx)
    for (lx, ly) in PI_HOLES_LOCAL
]

# Pi Zero USB ports — both micro-B, on the +Y short edge of the Pi Zero,
# near one long edge. In display frame this is around
#   X = PI_X0_DISP + a small offset, Y = PI_Y1_DISP (Y_TOP)
# Real Pi Zero 2W: PWR at ~12.4 mm from the camera-connector edge,
# OTG at ~41.4 mm. We treat them as a single wide slot for cable clearance.
USB_SLOT_W = 36.0               # wide enough to cover both micro-USBs
USB_SLOT_H = 8.0                # vertical (Z) opening height
USB_SLOT_X_CENTER = PI_X0_DISP + PI_Y / 2    # centred on the Pi Zero short axis
# microSD on the -Y short edge of the Pi Zero, opposite the USBs.
SD_SLOT_W = 14.0
SD_SLOT_H = 4.0

# ─── Stack Z layout ─────────────────────────────────────────────────
# Pi Zero PCB bottom -> Pi Zero PCB top -> 40-pin gap -> brass-post bottom
#   -> 4 mm post body -> display PCB bottom -> 1.6 mm PCB -> cover glass.
# All Z values are relative to the inside floor of the case.
FLOOR_T       = 2.0                            # bottom-shell thickness
PI_BOT_Z      = FLOOR_T + 3.0                  # 3 mm clearance under Pi Zero (SD card lives here)
PI_TOP_Z      = PI_BOT_Z + PI_T

POST_PROTRUSION = 4.0                          # brass post extends 4 mm below the display PCB
DISP_POST_BOT_Z = PI_TOP_Z + STACK_GAP         # case standoff top supports the brass-post bottom
DISP_BOT_Z      = DISP_POST_BOT_Z + POST_PROTRUSION
DISP_TOP_Z      = DISP_BOT_Z + DISP_T
GLASS_Z         = DISP_TOP_Z + GLASS_TOP_Z     # top of cover glass

# ─── Case shell ──────────────────────────────────────────────────────
WALL = 2.0
CASE_CLEAR_X = 1.5              # interior clearance per side around the display footprint
CASE_CLEAR_Y = 1.5
# Inside cavity must contain both the display (65 x 77) AND the Pi Zero
# (which extends from PI_X0_DISP to PI_X1_DISP in X). If the Pi extends
# beyond the display footprint, expand. PI_X0_DISP is computed above; if
# negative, the Pi extends past the display's -X edge.
INNER_X_MIN = min(0.0, PI_X0_DISP) - CASE_CLEAR_X
INNER_X_MAX = max(DISP_X, PI_X1_DISP) + CASE_CLEAR_X
INNER_Y_MIN = 0.0 - CASE_CLEAR_Y
INNER_Y_MAX = DISP_Y + CASE_CLEAR_Y
INNER_X = INNER_X_MAX - INNER_X_MIN
INNER_Y = INNER_Y_MAX - INNER_Y_MIN
INNER_Z = GLASS_Z                       # interior height matches top of glass

OUTER_X = INNER_X + 2 * WALL
OUTER_Y = INNER_Y + 2 * WALL
OUTER_Z = INNER_Z + FLOOR_T
OUTER_R = 4.0                           # outer corner fillet

# Helpers — translate from display-frame (PCB corner at 0,0) into the
# centred world frame the script builds in.
SHIFT_X = -(INNER_X_MIN + INNER_X / 2.0)        # so case is X-centred at world 0
SHIFT_Y = -(INNER_Y_MIN + INNER_Y / 2.0)

def disp_to_world(x, y):
    return (x + SHIFT_X, y + SHIFT_Y)

# ─── Standoff specs ──────────────────────────────────────────────────
# Standoffs for the display: rise from the floor up to DISP_BOT_Z so the
# display PCB sits on them. Each has a clearance bore for an M2.5 SHCS
# coming up from BELOW (through a counterbored hole in the floor) into the
# brass post threaded above. Length under the post is ignored — we sink
# the screw all the way and rely on the brass thread.
DISP_STANDOFF_OD = 7.5          # generous OD around the 5.5 brass post
DISP_BOLT_HEAD_OD = 5.0         # M2.5 SHCS head ~4.5; +0.5 clearance
DISP_BOLT_HEAD_T  = 2.5         # head height

# Standoffs for the Pi Zero: rise from the floor up to PI_BOT_Z. M2.5
# self-tap bore in the top.
PI_STANDOFF_OD     = 5.0
PI_STANDOFF_BORE   = 2.1        # M2.5 self-tap pilot
PI_STANDOFF_BORE_D = 5.0

# ─── Kickstand feet ──────────────────────────────────────────────────
# Two triangular ribs on the -Z back face. Lifting the bottom (-Y) edge
# off the table by FOOT_RISE while the top (+Y) edge sits down on the
# back of the case gives a viewing angle of about
#   atan(FOOT_RISE / OUTER_Y)  ~= 18 deg at FOOT_RISE=25.
FOOT_RISE = 25.0                # how far the feet stick out behind the case
FOOT_DEPTH = 18.0               # how wide along Y the feet extend
FOOT_THICK = 6.0                # how thick (along X) each foot is
FOOT_X_OFFSET = OUTER_X / 2 - 12.0   # feet near the X-ends


# ─── BUILD ───────────────────────────────────────────────────────────
def make_case():
    # Outer brick
    shell = (
        cq.Workplane("XY")
        .rect(OUTER_X, OUTER_Y)
        .extrude(OUTER_Z)
        .edges("|Z").fillet(OUTER_R)
    )

    # Hollow out interior cavity (open top toward +Z)
    cavity = (
        cq.Workplane("XY", origin=(0, 0, FLOOR_T))
        .rect(INNER_X, INNER_Y)
        .extrude(INNER_Z + 1.0)
    )
    shell = shell.cut(cavity)

    # Display standoffs — rise from floor (inside) up to DISP_POST_BOT_Z
    # (the bottom of the brass post on the display), with an M2.5 clearance
    # bore through floor + standoff so an M2.5x16 screw from BELOW threads
    # up into the brass post.
    for (dx, dy) in DISP_HOLES:
        wx, wy = disp_to_world(dx, dy)
        post = (
            cq.Workplane("XY", origin=(wx, wy, FLOOR_T))
            .circle(DISP_STANDOFF_OD / 2)
            .extrude(DISP_POST_BOT_Z - FLOOR_T)
        )
        shell = shell.union(post)
        # Clearance bore through the standoff + floor
        bore = (
            cq.Workplane("XY", origin=(wx, wy, -0.5))
            .circle(DISP_SCREW_CLEAR / 2)
            .extrude(DISP_POST_BOT_Z + 0.5 + 0.5)
        )
        shell = shell.cut(bore)
        # Counterbore on the outside (bottom) for the M2.5 screw head
        cb = (
            cq.Workplane("XY", origin=(wx, wy, 0))
            .circle(DISP_BOLT_HEAD_OD / 2)
            .extrude(DISP_BOLT_HEAD_T)
        )
        shell = shell.cut(cb)

    # Pi Zero standoffs — rise from floor up to PI_BOT_Z, M2.5 self-tap.
    for (dx, dy) in PI_HOLES_DISP:
        wx, wy = disp_to_world(dx, dy)
        # If a Pi-Zero hole happens to land inside a display standoff
        # footprint, skip (the display standoff also clamps that region).
        too_close = any(
            ((wx - disp_to_world(*h)[0]) ** 2 + (wy - disp_to_world(*h)[1]) ** 2)
            ** 0.5 < (DISP_STANDOFF_OD + PI_STANDOFF_OD) / 2 + 0.5
            for h in DISP_HOLES
        )
        if too_close:
            continue
        post = (
            cq.Workplane("XY", origin=(wx, wy, FLOOR_T))
            .circle(PI_STANDOFF_OD / 2)
            .extrude(PI_BOT_Z - FLOOR_T)
        )
        bore = (
            cq.Workplane("XY",
                         origin=(wx, wy, PI_BOT_Z - PI_STANDOFF_BORE_D))
            .circle(PI_STANDOFF_BORE / 2)
            .extrude(PI_STANDOFF_BORE_D + 0.5)
        )
        shell = shell.union(post).cut(bore)

    # USB PWR + OTG cutout on the +Y wall, centred over the Pi Zero short edge.
    usb_wx, _ = disp_to_world(USB_SLOT_X_CENTER, 0)
    usb_cz = PI_TOP_Z + USB_SLOT_H / 2 - 1.0       # USB bodies straddle PCB top
    usb_cut = (
        cq.Workplane("XZ", origin=(usb_wx, OUTER_Y / 2 + 0.5, usb_cz))
        .rect(USB_SLOT_W, USB_SLOT_H)
        .extrude(WALL + 2)
    )
    shell = shell.cut(usb_cut)

    # microSD slot on the -Y wall, centred over the Pi Zero opposite short edge.
    sd_cut = (
        cq.Workplane("XZ", origin=(usb_wx, -OUTER_Y / 2 - 0.5,
                                   PI_TOP_Z - SD_SLOT_H / 2 - 1.0))
        .rect(SD_SLOT_W, SD_SLOT_H)
        .extrude(-WALL - 2)
    )
    shell = shell.cut(sd_cut)

    # Ventilation slats on the floor (Pi Zero side). Six narrow slots so
    # the SoC can dump heat downward when the case lies face-up.
    slat_w = 2.0
    slat_l = 30.0
    slat_n = 6
    slat_pitch = 4.0
    slat_x0 = usb_wx - (slat_n - 1) * slat_pitch / 2
    for i in range(slat_n):
        sx = slat_x0 + i * slat_pitch
        vent = (
            cq.Workplane("XY", origin=(sx, 0, -0.5))
            .rect(slat_w, slat_l)
            .extrude(FLOOR_T + 1.0)
        )
        # Only cut the vents where they land over the Pi Zero footprint.
        shell = shell.cut(vent)

    # Kickstand feet — two triangular ribs on the back face, at the TOP
    # (+Y) edge. Each is a right triangle in the YZ plane:
    #   base on the case back at Z=0, running from +OUTER_Y/2 inward by
    #     FOOT_DEPTH (toward -Y),
    #   vertical leg from +OUTER_Y/2,0 going down to +OUTER_Y/2,-FOOT_RISE
    #     (this is the rear contact point on the desk),
    #   hypotenuse closes the triangle.
    # When the case is laid down, the -Y bottom edge and the foot tips
    # both rest on the desk; the screen face (+Z, OUTER_Z high) tilts up
    # toward the user. Cables exit the +Y wall in +Y direction, the foot
    # extends in -Z direction — orthogonal, no conflict.
    for sign in (-1, +1):
        fx = sign * FOOT_X_OFFSET
        tri_pts = [
            ( OUTER_Y / 2,                         0),             # foot base, top-back corner of case
            ( OUTER_Y / 2 - FOOT_DEPTH,            0),             # along the back face toward -Y
            ( OUTER_Y / 2,                         -FOOT_RISE),    # rear contact point
        ]
        foot = (
            cq.Workplane("YZ", origin=(fx, 0, 0))
            .polyline(tri_pts).close()
            .extrude(FOOT_THICK / 2, both=True)
        )
        shell = shell.union(foot)

    return shell


# ─── EXPORT ─────────────────────────────────────────────────────────
def main():
    case = make_case()
    case.val().exportStep("case.step")
    cq.exporters.export(case, "case.stl")
    print(f"Case outer: {OUTER_X:.1f} x {OUTER_Y:.1f} x {OUTER_Z:.1f} mm "
          f"(+{FOOT_RISE:.0f} mm foot)")
    print(f"Stack Z: floor={FLOOR_T}, pi_bot={PI_BOT_Z}, "
          f"pi_top={PI_TOP_Z}, disp_bot={DISP_BOT_Z}, "
          f"disp_top={DISP_TOP_Z}, glass_top={GLASS_Z}")
    print(f"Pi Zero placed at display X[{PI_X0_DISP:.2f}, {PI_X1_DISP:.2f}] "
          f"Y[{PI_Y0_DISP:.2f}, {PI_Y1_DISP:.2f}]")
    print("Files written: case.step, case.stl")


if __name__ == "__main__":
    main()
