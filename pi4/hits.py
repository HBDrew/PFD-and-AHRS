"""
hits.py – Highway-In-The-Sky box generator for synthetic approaches.

Given a runway threshold (lat, lon, elev_ft) and an approach course (the
heading the aircraft flies TOWARD the threshold — i.e. the runway
heading), generate a list of 3D rectangular boxes spaced along the
extended centreline at a 3° glideslope.

Output format matches what svt_renderer_gl.render_polyline_latlonelev
consumes, so the boxes feed directly into the existing depth-tested
polyline pipeline that the magenta direct-to course trace already uses.
Each box is one closed-loop polyline of 5 (lat, lon, elev_ft) vertices —
top-left → top-right → bottom-right → bottom-left → top-left.

Geometry defaults (matched to the design choices for this build):
  Glideslope:   3.0°
  Box width:  300 ft        (≈ FAA Cat-I final approach corridor)
  Box height: 200 ft
  Box centre on glideslope (pilot eye-line through the middle of the box).
  Spacing:    1000 ft        (~6 s between boxes at 100 kt GS)
  Final:      5 nm           (boxes from 1000 ft → 5 nm out from threshold)
"""

import math


_NM_TO_FT = 6076.12

DEFAULT_GS_DEG     = 3.0
DEFAULT_BOX_W_FT   = 300.0
DEFAULT_BOX_H_FT   = 200.0
DEFAULT_SPACING_FT = 1000.0
DEFAULT_FINAL_NM   = 5.0

# Cyan — visually distinct from the magenta direct-to trace and the
# white-ish runway centreline so the three layers don't compete.
HITS_COLOR      = (0.0, 200 / 255.0, 255 / 255.0, 1.0)
HITS_LINE_WIDTH = 2.0


def build_box_polylines(thresh_lat: float,
                        thresh_lon: float,
                        thresh_elev_ft: float,
                        course_deg: float,
                        gs_deg: float = DEFAULT_GS_DEG,
                        box_w_ft: float = DEFAULT_BOX_W_FT,
                        box_h_ft: float = DEFAULT_BOX_H_FT,
                        spacing_ft: float = DEFAULT_SPACING_FT,
                        final_nm: float = DEFAULT_FINAL_NM,
                        color: tuple = HITS_COLOR,
                        line_width: float = HITS_LINE_WIDTH
                        ) -> list:
    """Generate HITS box polylines along the approach.

    ``course_deg`` is the bearing the aircraft flies TO the threshold
    (i.e. the runway heading).  Boxes are placed back along the
    reciprocal direction at glideslope altitude.

    Returns a list of ``(verts, rgba, line_width)`` tuples ready to be
    appended to the renderer's ``polylines`` list.  ``verts`` is a list
    of ``(lat, lon, elev_ft)`` tuples — the renderer converts to
    mesh-local metres before drawing.
    """
    polylines = []

    # Bearing FROM the threshold AWAY from the runway (where the
    # approach corridor lives).  course_deg is the runway heading; flip
    # 180° to walk back along final.
    away_rad = math.radians((course_deg + 180.0) % 360.0)
    sin_a, cos_a = math.sin(away_rad), math.cos(away_rad)

    # Perpendicular to the path (right wing of an inbound aircraft).
    perp_rad = math.radians((course_deg + 90.0) % 360.0)
    sin_p, cos_p = math.sin(perp_rad), math.cos(perp_rad)

    half_w_ft   = box_w_ft / 2.0
    half_h_ft   = box_h_ft / 2.0
    cos_lat     = max(1e-6, math.cos(math.radians(thresh_lat)))
    deg_per_ft_lat = 1.0 / (60.0 * _NM_TO_FT)
    deg_per_ft_lon = deg_per_ft_lat / cos_lat
    tan_gs      = math.tan(math.radians(gs_deg))

    # Pre-compute the perpendicular lat/lon offsets (constant per call).
    d_lat_perp = cos_p * half_w_ft * deg_per_ft_lat
    d_lon_perp = sin_p * half_w_ft * deg_per_ft_lon

    # Step from the threshold outward in spacing_ft increments.  Start at
    # i=1 so the closest box is one spacing-step out (no box AT the
    # threshold itself — pilot is on visual final inside the inner box).
    n_boxes = int(final_nm * _NM_TO_FT / spacing_ft)
    for i in range(1, n_boxes + 1):
        d_ft = i * spacing_ft

        # Centre of the box, in lat/lon.
        center_lat = thresh_lat + d_ft * cos_a * deg_per_ft_lat
        center_lon = thresh_lon + d_ft * sin_a * deg_per_ft_lon

        # Glideslope altitude — pilot eye-line through the box centre.
        glide_alt_ft = thresh_elev_ft + d_ft * tan_gs
        top_alt = glide_alt_ft + half_h_ft
        bot_alt = glide_alt_ft - half_h_ft

        # Four corners + close the loop.  Order: TL → TR → BR → BL → TL.
        tl = (center_lat + d_lat_perp, center_lon + d_lon_perp, top_alt)
        tr = (center_lat - d_lat_perp, center_lon - d_lon_perp, top_alt)
        br = (center_lat - d_lat_perp, center_lon - d_lon_perp, bot_alt)
        bl = (center_lat + d_lat_perp, center_lon + d_lon_perp, bot_alt)
        polylines.append(([tl, tr, br, bl, tl], color, line_width))

    return polylines
