"""
enclosure.py – parametric AHRS sensor-hub enclosure (v1, for road testing).

Run:
    python3 enclosure.py            # writes enclosure_bottom.step / .stl
                                    # and enclosure_lid.step / .stl

Target board: pico-w-ahrs-sensor-hub-aead v0.2 (89 × 50 mm, R3 corners).
Mounting holes and outline taken from the Edge_Cuts Gerber/SVG.

Coordinate convention (all dimensions in mm):
    +X  = forward (the GPS / antenna end)
    -X  = aft (the USB micro-B end)            ← "connector aft" orientation
    +Z  = up  (out the lid, away from PCB bottom)

Notes:
- Box is sized for the GPS patch antenna (~20 mm above PCB top).
- MS4525 pressure transducer is on a separate flying board inside the
  enclosure; lid carries two 4-mm-OD tube ports for pitot / static.
- BME280 needs ambient pressure; small vent on a side wall, NOT facing
  forward (no ram-pressure contamination).
- Material assumption: PLA for v1 (warps above ~55°C — fine for bench
  and road test, NOT fine for a baked-in-the-sun dashboard).
"""

import cadquery as cq

# ─── Board geometry (from Edge_Cuts SVG) ─────────────────────────────
BOARD_X = 89.0          # long axis
BOARD_Y = 50.0          # short axis
BOARD_T = 1.6           # PCB thickness
BOARD_R = 3.0           # PCB corner radius

# Mounting holes, in board coordinates (origin at PCB corner)
BOARD_HOLES = [
    (5.93,  4.41),
    (86.59, 4.41),
    (5.93,  45.64),
    (86.59, 45.64),
]
PCB_HOLE_DIA = 3.4      # PCB drill (M3 clearance)

# ─── Component envelope ──────────────────────────────────────────────
GPS_ANT_H = 20.0        # tallest component: GPS patch antenna above PCB top
HEADROOM  = 2.5         # clearance above tallest component → cavity ceiling

# ─── Enclosure geometry ─────────────────────────────────────────────
# Single-screw-per-corner design: one long M3 screw enters the lid top,
# passes through a hanging post under the lid, through the PCB clearance
# hole, and threads into a standoff rising from the floor. The hanging
# post + standoff sandwich the PCB; the screw head clamps the lid down
# onto the wall. Four screws do everything.
CLEAR_XY     = 2.5      # cavity-wall clearance around PCB (no corner posts
                        # to fit anymore, so we can run tighter)
WALL         = 2.0      # side-wall thickness
FLOOR        = 2.0      # bottom-shell floor thickness
LID_T        = 2.5      # lid plate thickness
OUTER_R      = 4.0      # outer corner fillet (cosmetic)

# PCB standoffs (under the four PCB mounting holes; bottom shell)
STANDOFF_H        = 5.0     # PCB sits this high above the floor (header tails)
STANDOFF_DIA      = 6.0
STANDOFF_BORE_DIA = 2.7     # M3 self-tap pilot
STANDOFF_BORE_D   = 6.0     # how deep the thread bore goes into the standoff

# Lid hanging posts (mirror the standoffs — hang DOWN from the lid to
# clamp the PCB from above)
LID_POST_DIA      = 6.0     # OD matches the standoff
LID_POST_BORE_DIA = 3.4     # M3 clearance bore (screw shaft passes through)

# USB micro-B cutout (right-angle boot ~12 × 8 → generous slot)
USB_W         = 16.0        # along Y (short edge of board)
USB_H         = 11.0        # in Z
USB_Y_CENTER  = 0.0         # centered on short edge; tweak if PCB offset
USB_Z_CENTER  = STANDOFF_H + BOARD_T + 2.0   # 2 mm above PCB top

# BME280 vent (on a long side wall, well away from the USB end)
VENT_DIA = 4.0
VENT_X   = -10.0            # near aft-ish, well shielded from ram pressure
VENT_Z   = STANDOFF_H + BOARD_T + 4.0

# Pneumatic ports through the lid (4 mm OD tube — just through-holes for
# now; long-term we'll move to bulkhead fittings that split static / pitot /
# AOA, but for v1 the MS4525 floats inside and tubes poke through directly.)
TUBE_OD       = 4.0
PORT_BORE_DIA = TUBE_OD + 1.0       # 5 mm hole — slack so tube slides through
# Place both ports aft of the GPS antenna, side-by-side on Y:
PORT_X        = -20.0       # aft of center
PORT_Y_OFFSET = 8.0         # half-spacing between the two ports

# ─── Derived ─────────────────────────────────────────────────────────
CAV_X   = BOARD_X + 2 * CLEAR_XY
CAV_Y   = BOARD_Y + 2 * CLEAR_XY
CAV_Z   = STANDOFF_H + BOARD_T + GPS_ANT_H + HEADROOM
OUT_X   = CAV_X + 2 * WALL
OUT_Y   = CAV_Y + 2 * WALL
OUT_Z   = FLOOR + CAV_Z         # bottom-shell height (lid sits on top)

# Mounting hole positions in *world* coords (PCB centered in cavity).
# The standoffs, the lid hanging posts, and the screw clearance holes all
# share these XY positions — that's the whole point of the single-screw
# design.
HOLES = [
    (hx - BOARD_X / 2, hy - BOARD_Y / 2)
    for (hx, hy) in BOARD_HOLES
]

# Length of the hanging post on the lid: spans from lid bottom down to
# PCB top. Cavity height minus PCB top elevation within the cavity.
PCB_TOP_Z       = STANDOFF_H + BOARD_T          # PCB top inside the cavity
LID_POST_LEN    = CAV_Z - PCB_TOP_Z             # distance lid → PCB top


# ─── BOTTOM SHELL ────────────────────────────────────────────────────
def make_bottom():
    # Outer block
    shell = (
        cq.Workplane("XY")
        .rect(OUT_X, OUT_Y)
        .extrude(OUT_Z)
        .edges("|Z").fillet(OUTER_R)
    )

    # Hollow out the cavity (open top)
    cavity_cut = (
        cq.Workplane("XY", origin=(0, 0, FLOOR))
        .rect(CAV_X, CAV_Y)
        .extrude(CAV_Z + 1)         # +1 to over-cut through the top
    )
    shell = shell.cut(cavity_cut)

    # PCB standoffs in the floor (Ø6 × 5 mm tall with M3 self-tap bore
    # going down from the top, deep enough for solid thread engagement)
    for (hx, hy) in HOLES:
        standoff = (
            cq.Workplane("XY", origin=(hx, hy, FLOOR))
            .circle(STANDOFF_DIA / 2)
            .extrude(STANDOFF_H)
        )
        bore = (
            cq.Workplane("XY",
                         origin=(hx, hy, FLOOR + STANDOFF_H - STANDOFF_BORE_D))
            .circle(STANDOFF_BORE_DIA / 2)
            .extrude(STANDOFF_BORE_D + 0.5)
        )
        shell = shell.union(standoff).cut(bore)

    # USB cutout on the -X short edge
    usb = (
        cq.Workplane("YZ", origin=(-OUT_X / 2 - 0.5, USB_Y_CENTER, USB_Z_CENTER))
        .rect(USB_W, USB_H)
        .extrude(WALL + 2)
    )
    shell = shell.cut(usb)

    # BME280 vent on the +Y long wall (away from forward direction)
    vent = (
        cq.Workplane("XZ", origin=(VENT_X, OUT_Y / 2 + 0.5, VENT_Z))
        .circle(VENT_DIA / 2)
        .extrude(-WALL - 2)
    )
    shell = shell.cut(vent)

    return shell


# ─── LID ─────────────────────────────────────────────────────────────
def make_lid():
    lid = (
        cq.Workplane("XY")
        .rect(OUT_X, OUT_Y)
        .extrude(LID_T)
        .edges("|Z").fillet(OUTER_R)
    )

    # Recessed lip on the underside that fits into the cavity (locating)
    lip_t  = 1.5
    lip_xy = 0.4   # clearance per side so the lip slides into the cavity
    lip = (
        cq.Workplane("XY", origin=(0, 0, -lip_t))
        .rect(CAV_X - 2 * lip_xy, CAV_Y - 2 * lip_xy)
        .extrude(lip_t)
    )
    lid = lid.union(lip)

    # Hanging posts at the PCB mounting hole positions — they extend
    # down from the lid bottom into the cavity, contacting the PCB top
    # so that tightening the four screws clamps the PCB between each
    # post and its matching floor standoff.
    for (hx, hy) in HOLES:
        post = (
            cq.Workplane("XY", origin=(hx, hy, -LID_POST_LEN))
            .circle(LID_POST_DIA / 2)
            .extrude(LID_POST_LEN)
        )
        # Screw clearance bore — runs from above the lid top all the
        # way through the post so a single long M3 can pass through.
        bore = (
            cq.Workplane("XY", origin=(hx, hy, -LID_POST_LEN - 0.5))
            .circle(LID_POST_BORE_DIA / 2)
            .extrude(LID_POST_LEN + LID_T + 1)
        )
        # Countersink for the screw head, recessed into the lid top
        cs = (
            cq.Workplane("XY", origin=(hx, hy, LID_T))
            .circle(6.2 / 2)        # M3 head clearance
            .extrude(-1.6)
        )
        lid = lid.union(post).cut(bore).cut(cs)

    # Pneumatic ports (simple through-holes for now — tubes pass through
    # the lid and plug directly into the MS4525 inside)
    for sy in (-PORT_Y_OFFSET, +PORT_Y_OFFSET):
        bore = (
            cq.Workplane("XY", origin=(PORT_X, sy, -lip_t - 0.5))
            .circle(PORT_BORE_DIA / 2)
            .extrude(LID_T + lip_t + 1)
        )
        lid = lid.cut(bore)

    # Orientation cue: embossed arrow pointing +X (forward) on the lid top.
    # A simple isoceles triangle, raised 0.6 mm. Skip if cadquery's text-style
    # feature isn't needed.
    arrow_pts = [
        ( 10.0,  0.0),
        (  0.0,  4.0),
        (  0.0, -4.0),
    ]
    arrow = (
        cq.Workplane("XY", origin=(20.0, 0.0, LID_T))
        .polyline(arrow_pts).close()
        .extrude(0.6)
    )
    lid = lid.union(arrow)

    # "FWD" label as small raised bars to the side (machine-readable proxy)
    # — three short bars, can be filed off if user doesn't want it.
    for i in range(3):
        bar = (
            cq.Workplane("XY", origin=(-20.0 + i * 4.0, 0.0, LID_T))
            .rect(2.0, 8.0)
            .extrude(0.6)
        )
        lid = lid.union(bar)

    return lid


# ─── Export ─────────────────────────────────────────────────────────
def main():
    bottom = make_bottom()
    lid    = make_lid()

    bottom.val().exportStep("enclosure_bottom.step")
    lid.val().exportStep("enclosure_lid.step")

    cq.exporters.export(bottom, "enclosure_bottom.stl")
    cq.exporters.export(lid,    "enclosure_lid.stl")

    print(f"Box outer: {OUT_X:.1f} × {OUT_Y:.1f} × {OUT_Z + LID_T:.1f} mm")
    print(f"Cavity:    {CAV_X:.1f} × {CAV_Y:.1f} × {CAV_Z:.1f} mm")
    print(f"PCB rests on Ø{STANDOFF_DIA} × {STANDOFF_H} standoffs, "
          f"top of PCB at z = {FLOOR + STANDOFF_H + BOARD_T:.1f} mm")
    print(f"Files written: enclosure_bottom.{{step,stl}}, "
          f"enclosure_lid.{{step,stl}}")


if __name__ == "__main__":
    main()
