"""
svt_renderer_gl.py – OpenGL ES SVT renderer for Pi 4.

Hybrid architecture: this module renders only the SVT terrain background.
The rest of the PFD (tapes, ladder, drum boxes, UI) continues to be
drawn by pygame in pfd.py.  This module exposes one function:

    render_svt_gl(srtm_dir, ai_w, ai_h, pitch, roll, hdg, alt, lat, lon)
        → pygame.Surface

which is a drop-in replacement for the pygame scanline renderer.
The rendered Surface can be blitted into the AI region of the main PFD.

Implementation:
  - Standalone EGL context (offscreen, no display required)
  - Terrain mesh built from SRTM tiles within MESH_RADIUS_NM of aircraft
  - World coordinates: X=East, Y=North, Z=Up (metres relative to aircraft)
  - Vertex shader: world → clip space using look-at + perspective matrices
  - Fragment shader: clearance-based color palette (matches pygame version)
  - Sky gradient rendered as a fullscreen quad behind the terrain
  - Result read back via glReadPixels → pygame.Surface

Falls back to None if EGL/moderngl unavailable.  pfd.py handles fallback
to the pygame renderer.
"""

import math
import os
import threading

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

try:
    import moderngl
    HAS_MODERNGL = True
except ImportError:
    HAS_MODERNGL = False

import pygame

# Import shared terrain utilities
from terrain import load_tile, get_elevation_ft
# Water-mask loader (Natural Earth ocean+lakes rasterised per SRTM tile).
# Optional — if no .water tiles are present the renderer paints terrain
# everywhere as before.
try:
    from water import load_tile as load_water_tile
    HAS_WATER = True
except ImportError:
    HAS_WATER = False
    load_water_tile = None

# ── Constants ─────────────────────────────────────────────────────────────────
MESH_RADIUS_NM    = 20.0        # nm — terrain mesh extent around aircraft
MESH_GRID_N       = 300         # mesh resolution (300×300 = 90K vertices)

# Outer (far-LOD) mesh — drawn underneath the inner mesh to fill the gap
# between the inner mesh edge and the geometric horizon with real terrain
# silhouettes instead of a flat haze gradient.  Coarser sampling so the
# extra coverage doesn't blow up vertex count.
OUTER_MESH_RADIUS_NM = 75.0     # nm — out to ~75 nm ridges silhouette
OUTER_MESH_GRID_N    = 60       # 60x60 = 3600 verts (~5x coarser than inner)

# Mesh sizing strategy:
#   "constant"  — always use MESH_RADIUS_NM regardless of altitude.  Keeps the
#                 grid spacing consistent and predictable; simpler to reason
#                 about distances.  Recommended default.
#   "altitude"  — scale radius with sqrt(alt_ft) so that higher altitudes
#                 show more terrain (up to MESH_RADIUS_MAX_NM).  Clamped at
#                 MESH_RADIUS_MIN_NM on the low end.  Keeps the ~90 m vertex
#                 spacing since MESH_GRID_N scales with the radius.
MESH_SIZE_MODE    = "constant"  # "constant" | "altitude"
MESH_RADIUS_MIN_NM = 10.0       # floor (altitude mode)
MESH_RADIUS_MAX_NM = 40.0       # ceiling (altitude mode)
NM_TO_M        = 1852.0         # nautical miles → metres
FT_TO_M        = 0.3048         # feet → metres
M_TO_FT        = 1.0 / FT_TO_M

# Vertical FOV chosen to match the pygame pitch-ladder scale exactly:
# pitch_ladder uses px_per_deg = ai_h / 48, so vertical FOV = 48° ensures the
# SVT horizon, pitch-ladder 0° bar, and zero-pitch reference line all align
# at the same screen position for any given pitch angle.
V_FOV_DEG      = 48.0           # vertical field of view
NEAR_PLANE_M   = 50.0
FAR_PLANE_M    = OUTER_MESH_RADIUS_NM * NM_TO_M * 1.5

# ── Distance grid overlay ─────────────────────────────────────────────────────
# Cyan-tinted lines on the terrain to help judge distance and orientation.
# Aligned with cardinal directions (N/S and E/W).
GRID_SPACING_NM   = 0.5         # minor grid spacing (0.5 nm squares)
GRID_MAJOR_EVERY  = 4           # major (brighter) line every N minor lines (= 2 nm)
GRID_FADE_NM      = MESH_RADIUS_NM   # grid fades out at the mesh edge

# ── Sun-angle lighting ────────────────────────────────────────────────────────
# Direction FROM terrain TOWARD the sun, in world frame (X=East, Y=North, Z=Up).
# Default: mid-morning sun from the SE at 45° elevation.
#   azimuth_deg measured from North, clockwise (compass bearing of the sun)
#   elevation_deg above horizon (0° = horizon, 90° = directly overhead)
SUN_AZIMUTH_DEG   = 135.0       # SE (compass bearing)
SUN_ELEVATION_DEG = 45.0        # sun height above horizon
SUN_INTENSITY     = 0.75        # 0.0 = lighting off, 1.0 = full strength
SUN_AMBIENT       = 0.45        # 0.0 = pitch-black shadows, 1.0 = no shadow

# ── GLSL shaders ──────────────────────────────────────────────────────────────

VERTEX_SHADER = """
#version 300 es
precision highp float;

in vec3 in_pos;          // (East, North, Up_absolute) in metres. Origin at
                         // mesh centre. East metric scales with cos(mesh_lat)
                         // (mesh covers a fixed *angular* span); Z is absolute
                         // elevation above sea level.
in float in_water;       // 0.0 = land, 1.0 = water (sampled at vertex from
                         // Natural Earth ocean+lakes mask).

uniform mat4 u_mvp;
uniform float u_alt_m;        // aircraft altitude (m, absolute) per-frame, so
                              // colour updates smoothly with altitude.
uniform float u_cos_mesh_lat; // cos(mesh_lat). Used to convert in_pos.x from
                              // mesh-local metric east back to equator-
                              // equivalent metric (lon * NM*60) so the grid
                              // pattern is in absolute world coords and stays
                              // anchored across mesh recentres at any latitude.
uniform vec2 u_world_offset;  // mesh centre's (lon*NM*60, lat*NM*60) modulo
                              // the grid period — equator-equivalent metric.
uniform vec2 u_aircraft_xy;   // aircraft (east, north) in this mesh's frame
                              // — used by the outer mesh to discard fragments
                              // inside the inner mesh's coverage square.

out float v_clearance_ft;
out vec3 v_world_pos;
out float v_dist_m;
out vec2 v_offset_from_aircraft;
out float v_water;

void main() {
    gl_Position = u_mvp * vec4(in_pos, 1.0);
    v_clearance_ft = (u_alt_m - in_pos.z) * 3.28084;
    // Equator-equivalent metric — invariant per world point regardless of
    // which mesh (which mesh_lat) we're in. North is already metric.
    float vx_eq = in_pos.x / u_cos_mesh_lat;
    v_world_pos = vec3(vec2(vx_eq, in_pos.y) + u_world_offset, in_pos.z);
    v_dist_m = length(in_pos.xy);
    v_offset_from_aircraft = in_pos.xy - u_aircraft_xy;
    v_water = in_water;
}
"""

FRAGMENT_SHADER = """
#version 300 es
precision highp float;

in float v_clearance_ft;
in vec3 v_world_pos;
in float v_dist_m;
in vec2 v_offset_from_aircraft;
in float v_water;
out vec4 frag_color;

uniform float u_grid_spacing_m;     // metres per grid square (e.g. 1852 = 1 nm)
uniform float u_grid_major_every;   // major line every N squares (e.g. 5)
uniform float u_grid_max_dist_m;    // grid fades to invisible at this distance
uniform float u_discard_inside_m;   // outer mesh: drop fragments inside the
                                    // inner mesh's square coverage zone so
                                    // it doesn't clobber inner-mesh detail
                                    // and grid lines via depth-test ties.
                                    // 0.0 = no discard (inner mesh).
uniform vec3  u_sun_dir;            // unit vector pointing TOWARD the sun
uniform float u_sun_intensity;      // 0.0 = no lighting, 1.0 = full
uniform float u_ambient;            // 0.0 = pitch black shadows, 1.0 = no shadow
uniform float u_water_enable;       // 0.0 = ignore water flag, 1.0 = render
uniform float u_alert_enable;       // 0.0 = neutral browns only (taxi /
                                    // rollout, below Vso); 1.0 = full
                                    // red/orange/amber proximity palette.
                                    // water with the water palette (lets the
                                    // pilot disable water rendering for debug).

// Clearance-based color palette (matches pygame PALETTE_RELATIVE).
// Bands shifted +200 ft vs the textbook "red when terrain reaches altitude":
// red at clearance < 200 ft gives the pilot a 200 ft buffer warning before
// actual contact, matching TERRAIN_WARNING_FT in shared/config_base.py.
vec3 clearance_color(float c) {
    // Garmin-style ground inhibit: when u_alert_enable is 0 (groundspeed
    // below user-set Vso — taxi, rollout) the warning bands are skipped
    // so terrain reads as neutral browns instead of painting the entire
    // foreground red.  Above Vso the full palette engages.
    if (u_alert_enable > 0.5) {
        if (c < 200.0)  return vec3(0.86, 0.12, 0.12);
        if (c < 300.0)  return vec3(0.86, 0.31, 0.0);
        if (c < 700.0)  return vec3(0.78, 0.51, 0.0);
    }
    if (c < 1200.0) return vec3(0.55, 0.39, 0.16);
    if (c < 2200.0) return vec3(0.39, 0.29, 0.14);
    return vec3(0.27, 0.22, 0.11);
}

// Water palette — deep navy with a subtle horizon-distance lift toward
// lighter blue so distant water reads as ocean rather than a flat slab.
vec3 water_color(float dist_m) {
    vec3 deep   = vec3(0.05, 0.16, 0.30);
    vec3 far    = vec3(0.18, 0.32, 0.46);
    float t = clamp(dist_m / 30000.0, 0.0, 1.0);
    return mix(deep, far, t);
}

// Per-fragment normal from screen-space derivatives of world position.
// Gives flat-shaded lighting (constant across each triangle face) — cheap,
// no per-vertex normal buffer required.
vec3 compute_normal() {
    vec3 dx = dFdx(v_world_pos);
    vec3 dy = dFdy(v_world_pos);
    vec3 n  = normalize(cross(dx, dy));
    // World frame is +Z up; ensure normal points upward (cross product sign
    // depends on triangle winding order which we don't control perfectly).
    if (n.z < 0.0) n = -n;
    return n;
}

// Anti-aliased grid line: returns 0.0 (no line) to 1.0 (full line).
// Uses screen-space derivatives for consistent line width regardless of distance.
float grid_line(vec2 pos, float spacing, float line_width_px) {
    vec2 grid = abs(fract(pos / spacing - 0.5) - 0.5) / fwidth(pos / spacing);
    float line = min(grid.x, grid.y);
    return 1.0 - smoothstep(0.0, line_width_px, line);
}

void main() {
    // Outer mesh: kill fragments inside the inner mesh's square so the
    // inner tier owns the foreground exclusively (no z-tie clobbering).
    if (u_discard_inside_m > 0.0
        && abs(v_offset_from_aircraft.x) < u_discard_inside_m
        && abs(v_offset_from_aircraft.y) < u_discard_inside_m) {
        discard;
    }

    // Water test: any fragment whose interpolated water flag clears the
    // 0.5 threshold is painted as ocean/lake.  Lighting and the grid
    // overlay are both suppressed on water — a flat blue surface looks
    // wrong with shaded normals, and the grid lines double-up against
    // the coastline.
    bool is_water = (u_water_enable > 0.5) && (v_water > 0.5);
    vec3 base;
    if (is_water) {
        base = water_color(v_dist_m);
    } else {
        base = clearance_color(v_clearance_ft);

        // ── Sun-angle lighting ─────────────────────────────────────────
        // Simple Lambertian diffuse term on the terrain color.  Faces
        // pointing toward the sun appear brighter; faces in shadow
        // darken toward ambient.
        if (u_sun_intensity > 0.001) {
            vec3 n = compute_normal();
            float diffuse = max(0.0, dot(n, u_sun_dir));
            float light = mix(u_ambient, 1.0, diffuse) * u_sun_intensity
                        + (1.0 - u_sun_intensity);
            base *= light;
        }
    }

    // Distance-based grid fade: full strength near, fades out at u_grid_max_dist_m
    float fade = 1.0 - smoothstep(u_grid_max_dist_m * 0.5, u_grid_max_dist_m, v_dist_m);

    if (!is_water && fade > 0.01 && u_grid_spacing_m > 0.0) {
        float minor = grid_line(v_world_pos.xy, u_grid_spacing_m, 1.0);
        float major = grid_line(v_world_pos.xy,
                                u_grid_spacing_m * u_grid_major_every, 1.5);

        float t_dark = smoothstep(500.0, 0.0, v_clearance_ft);
        vec3 minor_light = vec3(0.85, 0.95, 1.00);
        vec3 major_light = vec3(0.40, 0.90, 1.00);
        vec3 minor_dark  = vec3(0.05, 0.05, 0.10);
        vec3 major_dark  = vec3(0.00, 0.10, 0.30);

        vec3 minor_col = mix(minor_light, minor_dark, t_dark);
        vec3 major_col = mix(major_light, major_dark, t_dark);
        float minor_strength = mix(0.40, 0.65, t_dark);
        float major_strength = mix(0.60, 0.85, t_dark);

        base = mix(base, minor_col, minor * minor_strength * fade);
        base = mix(base, major_col, major * major_strength * fade);
    }

    frag_color = vec4(base, 1.0);
}
"""

SKY_VERTEX_SHADER = """
#version 300 es
precision highp float;

in vec2 in_pos;          // fullscreen quad in NDC
out vec2 v_ndc;

void main() {
    // z = 0.99999 (just inside the far plane).  The original 0.999 was too
    // shallow: with NEAR_PLANE_M=50 / FAR_PLANE_M≈208 km, the perspective
    // projection puts terrain at ~37 nm at z_ndc≈0.999, so the sky's depth
    // and any terrain beyond ~37 nm tied or lost to the sky in the depth
    // test.  Result was that the outer mesh's middle distance (37–75 nm
    // band) got overpainted with sky-blue, reading as a gap between the
    // visible high-res inner mesh (< 20 nm) and the silhouette of the
    // outer mesh's far edge (~75 nm at the horizon).  Pushing sky to
    // 0.99999 means terrain anywhere inside the far plane wins the depth
    // test, so the outer mesh becomes continuously visible.
    gl_Position = vec4(in_pos, 0.99999, 1.0);
    v_ndc = in_pos;                            // pass through raw NDC (-1..1)
}
"""

SKY_FRAGMENT_SHADER = """
#version 300 es
precision highp float;

in vec2 v_ndc;
out vec4 frag_color;

uniform float u_horizon_y;   // NDC Y of horizon line at x=0 (-1..1)
uniform float u_roll_rad;    // camera roll in radians
uniform float u_aspect;      // aspect ratio (w/h) for rotation correction
uniform vec3  u_below_horizon_color;  // colour for below-horizon mesh gaps —
                                      // CPU sets this from the current TAWS
                                      // alert level so the gap matches the
                                      // surrounding terrain palette (red when
                                      // foreground terrain is red, etc.)

void main() {
    // Un-roll the NDC point so horizon becomes a horizontal line again.
    // Stretch x by aspect ratio so the rotation is angle-preserving (otherwise
    // a banked horizon would appear at the wrong angle on non-square displays).
    float x_sq = v_ndc.x * u_aspect;
    float y_sq = v_ndc.y;
    float c = cos(u_roll_rad);
    float s = sin(u_roll_rad);
    float y_unrolled = -x_sq * s + y_sq * c;

    // Above the rolled horizon: sky gradient (horizon-blue → zenith-blue).
    // Below the rolled horizon: blend horizon-blue (at the horizon line)
    // into u_below_horizon_color (the TAWS-aware ground colour) over the
    // first ~0.30 NDC of vertical distance.  At the horizon line this
    // reads as "more sky" and matches the above-horizon gradient — so
    // over water we don't get a spurious brown band where the mesh ends
    // — while gaps deeper below the horizon (under-wing at steep bank
    // and low altitude) still pick up the terrain-evocative ground
    // colour.  Wherever the terrain mesh renders, depth-test wins so
    // this only shows in genuine mesh gaps.
    vec3 horizon_col = vec3(0.23, 0.51, 0.78);
    if (y_unrolled < u_horizon_y) {
        float t_below = clamp((u_horizon_y - y_unrolled) / 0.30, 0.0, 1.0);
        vec3 below = mix(horizon_col, u_below_horizon_color, t_below);
        frag_color = vec4(below, 1.0);
    } else {
        float t = (y_unrolled - u_horizon_y) / max(0.001, 1.0 - u_horizon_y);
        vec3 zenith_col  = vec3(0.04, 0.16, 0.31);
        frag_color = vec4(mix(horizon_col, zenith_col, t), 1.0);
    }
}
"""


# ── Polyline (depth-tested 3D line) shader ────────────────────────────────────
# Used to draw the magenta direct-to course trace as 3D world geometry so the
# terrain depth buffer occludes line segments hidden behind ridges.

LINE_VERTEX_SHADER = """
#version 300 es
precision highp float;
in vec3 in_pos;            // (East, North, Up_absolute) in metres, mesh-local frame
uniform mat4 u_mvp;
void main() {
    gl_Position = u_mvp * vec4(in_pos, 1.0);
}
"""

LINE_FRAGMENT_SHADER = """
#version 300 es
precision highp float;
uniform vec4 u_color;
out vec4 frag_color;
void main() { frag_color = u_color; }
"""


# ── Module-level state ────────────────────────────────────────────────────────
_ctx        = None       # moderngl Context
_fbo        = None       # framebuffer object
_color_tex  = None       # color attachment
_depth_buf  = None       # depth attachment
_terrain_prog = None     # shader program for terrain
_sky_prog     = None     # shader program for sky
_sky_vao      = None     # fullscreen quad VAO
_terrain_vao  = None     # terrain mesh VAO
_terrain_vbo_pos = None  # terrain vertex positions VBO
_terrain_ibo     = None  # terrain triangle indices IBO
# Polyline rendering (HITS boxes + direct-to course trace) in the
# standalone-EGL path — separate from _SharedState's polyline buffers.
_line_prog       = None  # shader program for depth-tested polylines
_line_vbo        = None  # reusable VBO, grown on demand
_line_vao        = None  # vertex array
_line_capacity   = 0     # current VBO byte capacity
_last_line_width = None  # last value pushed to ctx.line_width (cache to skip GL state changes)

_fbo_size   = (0, 0)     # current FBO (w, h)
_mesh_key   = None       # cache key (lat_q, lon_q, alt_q) — mesh rebuild trigger
_mesh_radius_m = MESH_RADIUS_NM * NM_TO_M  # current mesh radius (for grid fade)
_mesh_lat   = 0.0        # last-built mesh centre lat (= aircraft lat in standalone path)
_mesh_lon   = 0.0        # last-built mesh centre lon


def _init_gl(width: int, height: int) -> bool:
    """Create EGL context and FBO at the requested size.  Returns True on success."""
    global _ctx, _fbo, _color_tex, _depth_buf, _terrain_prog, _sky_prog, _sky_vao
    global _fbo_size

    if not (HAS_MODERNGL and HAS_NUMPY):
        return False

    if _ctx is not None and _fbo_size == (width, height):
        return True

    if _ctx is None:
        # Try each EGL device index — on Pi 4 device 0 is typically the
        # KMS display card (conflicts with pygame) and device 1 is the
        # render-only node (/dev/dri/renderD128) which can coexist.
        for dev_idx in (1, 0, None):
            try:
                kw = dict(backend='egl', require=300)
                if dev_idx is not None:
                    kw['device_index'] = dev_idx
                _ctx = moderngl.create_standalone_context(**kw)
                renderer = _ctx.info.get("GL_RENDERER", "?")
                print(f"[SVT-GL] EGL context OK (device_index={dev_idx}): {renderer}")
                break
            except Exception as e:
                print(f"[SVT-GL] EGL device_index={dev_idx} failed: {e}")
                _ctx = None
        if _ctx is None:
            return False

    # (Re)allocate FBO at requested size
    if _fbo is not None:
        _fbo.release()
        _color_tex.release()
        _depth_buf.release()
    _color_tex = _ctx.texture((width, height), 4)
    _depth_buf = _ctx.depth_renderbuffer((width, height))
    _fbo = _ctx.framebuffer(color_attachments=[_color_tex],
                            depth_attachment=_depth_buf)
    _fbo_size = (width, height)

    # Compile shaders once
    global _line_prog, _line_vbo, _line_vao, _line_capacity
    if _terrain_prog is None:
        _terrain_prog = _ctx.program(vertex_shader=VERTEX_SHADER,
                                     fragment_shader=FRAGMENT_SHADER)
        _sky_prog = _ctx.program(vertex_shader=SKY_VERTEX_SHADER,
                                 fragment_shader=SKY_FRAGMENT_SHADER)
        _line_prog = _ctx.program(vertex_shader=LINE_VERTEX_SHADER,
                                  fragment_shader=LINE_FRAGMENT_SHADER)

    # Sky quad: fullscreen triangle pair in NDC
    if _sky_vao is None:
        sky_verts = np.array([
            -1, -1,   1, -1,   1,  1,
            -1, -1,   1,  1,  -1,  1,
        ], dtype=np.float32)
        sky_vbo = _ctx.buffer(sky_verts.tobytes())
        _sky_vao = _ctx.vertex_array(_sky_prog, [(sky_vbo, '2f', 'in_pos')])

    # Polyline VBO+VAO — single reusable buffer, grown as needed in
    # _render_standalone_polyline.  Same shader as the shared-context
    # path so HITS / direct-to traces look identical between live PFD
    # and offline preview captures.
    if _line_vao is None:
        _line_capacity = 4096   # ~340 vec3 vertices initial
        _line_vbo = _ctx.buffer(reserve=_line_capacity)
        _line_vao = _ctx.vertex_array(_line_prog, [(_line_vbo, '3f', 'in_pos')])

    return True


def _build_mesh(srtm_dir: str, lat: float, lon: float, alt_ft: float):
    """Sample SRTM around aircraft into a vertex+index buffer.

    Returns (positions [N×3 float32 metres], clearances [N float32 metres]).
    Aircraft is at origin (0,0,0); +X=East, +Y=North, +Z=Up; alt is mesh-relative.
    """
    global _mesh_key, _mesh_radius_m, _terrain_vao, _terrain_vbo_pos, _terrain_ibo
    global _mesh_lat, _mesh_lon

    # In the standalone path the aircraft is the mesh origin so the
    # polyline transform needs the aircraft lat/lon as its reference
    # frame.  Stash on every call (cheap) so render_svt_gl()'s caller
    # doesn't have to thread it through separately.
    _mesh_lat = lat
    _mesh_lon = lon

    # Cache key: quantize lat/lon/alt so we don't rebuild every frame
    # 0.005° ≈ 0.3 nm at mid-latitudes; 200 ft alt steps
    key = (round(lat, 3), round(lon, 3), round(alt_ft / 200) * 200)
    if key == _mesh_key and _terrain_vao is not None:
        return

    # Mesh radius: constant or altitude-dependent
    if MESH_SIZE_MODE == "altitude":
        # ~ sqrt(alt/1000) * 6  → 1000ft:6nm, 5000ft:13nm, 10000ft:19nm, 30000ft:33nm
        r_nm = max(MESH_RADIUS_MIN_NM,
                   min(MESH_RADIUS_MAX_NM,
                       6.0 * math.sqrt(max(100.0, alt_ft) / 1000.0)))
    else:
        r_nm = MESH_RADIUS_NM
    radius_m = r_nm * NM_TO_M
    _mesh_radius_m = radius_m   # publish for grid fade uniform
    n = MESH_GRID_N
    alt_m = alt_ft * FT_TO_M

    # Grid in local East/North metres
    grid_1d = np.linspace(-radius_m, radius_m, n, dtype=np.float32)
    east, north = np.meshgrid(grid_1d, grid_1d)   # both (n, n)

    # Convert each grid point to lat/lon for SRTM lookup
    cos_lat = max(1e-6, math.cos(math.radians(lat)))
    dlat = north / NM_TO_M / 60.0                  # metres → degrees
    dlon = east  / NM_TO_M / 60.0 / cos_lat
    sample_lat = lat + dlat
    sample_lon = lon + dlon

    # Vectorized SRTM lookup (one tile lookup per unique tile)
    elev_ft = np.zeros((n, n), dtype=np.float32)
    lat_int_arr = np.floor(sample_lat).astype(np.int32)
    lon_int_arr = np.floor(sample_lon).astype(np.int32)
    enc = ((lat_int_arr.astype(np.int64) + 90) * 1000 +
           (lon_int_arr.astype(np.int64) + 360))
    for tile_key in np.unique(enc):
        tla = int(tile_key) // 1000 - 90
        tlo = int(tile_key) %  1000 - 360
        result = load_tile(srtm_dir, tla, tlo)
        if result is None:
            continue
        tile_data, n_s = result
        mask = (lat_int_arr == tla) & (lon_int_arr == tlo)
        if not mask.any():
            continue
        step = 1.0 / (n_s - 1)
        row_i = np.clip(np.round((tla + 1 - sample_lat) / step).astype(np.int32),
                        0, n_s - 1)
        col_i = np.clip(np.round((sample_lon - tlo) / step).astype(np.int32),
                        0, n_s - 1)
        elev_ft[mask] = tile_data[row_i, col_i][mask]

    # Clamp sub-sea-level samples (SRTM1 ocean bathymetry) to the
    # waterline so the mesh doesn't crater offshore.  See _build_tier_mesh
    # for the rationale.
    np.maximum(elev_ft, 0.0, out=elev_ft)

    elev_m = elev_ft * FT_TO_M

    # Build vertex array: position (east, north, up) and clearance (metres)
    # Z = absolute elevation (matches shader expectation). Aircraft alt
    # applied per-frame via u_alt_m uniform.
    positions = np.stack([east, north, elev_m], axis=-1).astype(np.float32)

    # Build triangle indices (two triangles per quad)
    # Vertex (i, j) → flat index i*n + j
    i, j = np.meshgrid(np.arange(n - 1), np.arange(n - 1), indexing='ij')
    v0 = (i     * n + j    ).astype(np.uint32)
    v1 = (i     * n + j + 1).astype(np.uint32)
    v2 = ((i+1) * n + j    ).astype(np.uint32)
    v3 = ((i+1) * n + j + 1).astype(np.uint32)
    tri1 = np.stack([v0, v2, v1], axis=-1).reshape(-1)
    tri2 = np.stack([v1, v2, v3], axis=-1).reshape(-1)
    indices = np.concatenate([tri1, tri2]).astype(np.uint32)

    # Upload to GPU (release old buffers if present)
    if _terrain_vbo_pos is not None:
        _terrain_vao.release()
        _terrain_vbo_pos.release()
        _terrain_ibo.release()

    _terrain_vbo_pos = _ctx.buffer(positions.tobytes())
    _terrain_ibo     = _ctx.buffer(indices.tobytes())
    # The vertex shader takes an `in_water` attribute (per-vertex 0/1 from
    # the water mask).  The standalone path doesn't sample water — uploading
    # zeros keeps the program link happy and renders everything as land.
    _water_zeros = np.zeros(positions.shape[:2], dtype=np.float32)
    _terrain_vbo_water = _ctx.buffer(_water_zeros.flatten().tobytes())
    _terrain_vao = _ctx.vertex_array(
        _terrain_prog,
        [(_terrain_vbo_pos,   '3f', 'in_pos'),
         (_terrain_vbo_water, '1f', 'in_water')],
        index_buffer=_terrain_ibo,
    )

    _mesh_key = key


# ── Math helpers ──────────────────────────────────────────────────────────────

def _perspective(fov_y_deg: float, aspect: float, near: float, far: float):
    """Build a right-handed perspective projection matrix (column-major)."""
    f = 1.0 / math.tan(math.radians(fov_y_deg) / 2.0)
    m = np.zeros((4, 4), dtype=np.float32)
    m[0, 0] = f / aspect
    m[1, 1] = f
    m[2, 2] = (far + near) / (near - far)
    m[2, 3] = (2 * far * near) / (near - far)
    m[3, 2] = -1.0
    return m


def _look_at(eye, target, up):
    """Right-handed look-at view matrix (column-major)."""
    eye = np.asarray(eye, dtype=np.float32)
    f = np.asarray(target, dtype=np.float32) - eye
    f /= np.linalg.norm(f)
    up = np.asarray(up, dtype=np.float32)
    s = np.cross(f, up); s /= np.linalg.norm(s)
    u = np.cross(s, f)
    m = np.eye(4, dtype=np.float32)
    m[0, :3] = s
    m[1, :3] = u
    m[2, :3] = -f
    m[:3, 3] = -m[:3, :3] @ eye
    return m


def _attitude_basis(pitch_deg: float, roll_deg: float, hdg_deg: float):
    """Compute camera (forward, up) world vectors from aircraft attitude.
    World: X=East, Y=North, Z=Up.
    Aircraft conventions: pitch+ = nose up, roll+ = right wing down,
    hdg = compass degrees (0=N, 90=E).
    """
    p = math.radians(pitch_deg)
    r = math.radians(roll_deg)
    h = math.radians(hdg_deg)

    # Heading: forward at (sin h, cos h, 0); right at (cos h, -sin h, 0)
    fwd0 = np.array([math.sin(h), math.cos(h), 0.0])
    rgt0 = np.array([math.cos(h), -math.sin(h), 0.0])
    up0  = np.array([0.0, 0.0, 1.0])

    # Pitch: rotate forward/up around right axis (positive pitch = nose up)
    fwd1 = fwd0 * math.cos(p) + up0 * math.sin(p)
    up1  = -fwd0 * math.sin(p) + up0 * math.cos(p)
    rgt1 = rgt0

    # Roll: rotate right/up around forward axis (positive roll = right wing down)
    rgt2 = rgt1 * math.cos(r) - up1 * math.sin(r)
    up2  = rgt1 * math.sin(r) + up1 * math.cos(r)
    fwd2 = fwd1

    return fwd2, up2


def _horizon_y_ndc(pitch_deg: float, fov_y_deg: float) -> float:
    """NDC Y of the horizon line for the sky shader.

    With camera pitched up by +P degrees, the geometric horizon at infinity
    appears at angle -P below the camera forward vector.  In OpenGL NDC
    (Y-up), "below center" is negative Y.  So:
        y_horizon = tan(-pitch) / tan(fov/2)  =  -tan(pitch) / tan(fov/2)
    Pitched up  → negative Y (horizon below centre).
    Pitched down → positive Y (horizon above centre).
    """
    half_fov = math.radians(fov_y_deg) / 2.0
    return max(-1.0, min(1.0, -math.tan(math.radians(pitch_deg)) / math.tan(half_fov)))


# ── Public render function ────────────────────────────────────────────────────

def _render_standalone_polyline(mvp, vertices_latlonelev, rgba, line_width):
    """Render one depth-tested polyline through the standalone-EGL path's
    framebuffer.  Vertices are (lat_deg, lon_deg, elev_ft); they're
    converted to the same mesh-local metric frame the terrain mesh uses
    so the terrain's depth buffer occludes line segments hidden behind
    ridges.  Mirrors _SharedState.render_polyline_latlonelev() — same
    shader, same coordinate convention — but operates on the
    module-level VBO/VAO instead of an instance attribute."""
    global _line_capacity
    if vertices_latlonelev is None or len(vertices_latlonelev) < 2:
        return
    v = np.asarray(vertices_latlonelev, dtype=np.float32)
    cos_mlat = max(1e-6, math.cos(math.radians(_mesh_lat)))
    east_m  = (v[:, 1] - _mesh_lon) * 60.0 * NM_TO_M * cos_mlat
    north_m = (v[:, 0] - _mesh_lat) * 60.0 * NM_TO_M
    up_m    = v[:, 2] * FT_TO_M
    world = np.stack([east_m, north_m, up_m], axis=1).astype(np.float32)
    data = world.tobytes()
    # Grow buffer if this polyline outgrew the previous capacity.
    if len(data) > _line_capacity:
        _line_vbo.release()
        _line_vao.release()
        _line_capacity = max(len(data), _line_capacity * 2)
        globals()['_line_vbo'] = _ctx.buffer(reserve=_line_capacity)
        globals()['_line_vao'] = _ctx.vertex_array(
            _line_prog, [(_line_vbo, '3f', 'in_pos')])
    _line_vbo.write(data)
    _line_prog['u_mvp'].write(mvp.T.tobytes())
    _line_prog['u_color'].value = rgba
    # Width support is driver-dependent; many GLES drivers cap at 1 px.
    global _last_line_width
    if _last_line_width != line_width:
        try:
            _ctx.line_width = float(line_width)
        except Exception:
            pass
        _last_line_width = line_width
    _line_vao.render(mode=moderngl.LINE_STRIP, vertices=len(world))


def render_svt_gl(
    srtm_dir: str,
    ai_w: int,
    ai_h: int,
    pitch_deg: float,
    roll_deg: float,
    hdg_deg: float,
    alt_ft: float,
    lat: float,
    lon: float,
    v_fov_deg: float = V_FOV_DEG,
    sun_az_deg: float | None = None,
    sun_el_deg: float | None = None,
    sun_intensity: float | None = None,
    below_horizon_color: tuple = (0.35, 0.27, 0.15),
    polylines: list | None = None,
):
    """Render the SVT terrain background using OpenGL.
    Returns a pygame.Surface (ai_w × ai_h, RGBA) or None if GL failed.

    ``polylines`` is the same list-of-(verts, rgba, line_width) tuples
    the shared-GL path accepts — HITS boxes, direct-to course trace,
    etc.  Each polyline is rendered AFTER the terrain pass so the depth
    buffer occludes segments hidden behind ridges, then the FBO is read
    back to a pygame.Surface as before.
    """
    if not _init_gl(ai_w, ai_h):
        return None

    _build_mesh(srtm_dir, lat, lon, alt_ft)

    # Camera — vertices store absolute elevation, so eye Z = aircraft alt.
    alt_m = alt_ft * FT_TO_M
    fwd, up = _attitude_basis(pitch_deg, roll_deg, hdg_deg)
    eye = np.array([0.0, 0.0, alt_m], dtype=np.float32)
    target = eye + fwd
    view = _look_at(eye, target, up)
    proj = _perspective(v_fov_deg, ai_w / ai_h, NEAR_PLANE_M, FAR_PLANE_M)
    mvp = proj @ view

    # Render
    _fbo.use()
    _ctx.enable(moderngl.DEPTH_TEST)
    _ctx.clear(0.04, 0.16, 0.31, 1.0)   # default sky-blue (will be overdrawn)

    # Sky: write at far plane so it's behind terrain
    horizon_y = _horizon_y_ndc(pitch_deg, v_fov_deg)
    _sky_prog['u_horizon_y'].value = horizon_y
    _sky_prog['u_roll_rad'].value  = math.radians(roll_deg)
    _sky_prog['u_aspect'].value    = ai_w / ai_h
    _sky_prog['u_below_horizon_color'].value = below_horizon_color
    _ctx.disable(moderngl.DEPTH_TEST)
    _sky_vao.render()
    _ctx.enable(moderngl.DEPTH_TEST)

    # Terrain
    if _terrain_vao is not None:
        _terrain_prog['u_mvp'].write(mvp.T.tobytes())   # column-major for GL
        _terrain_prog['u_alt_m'].value            = alt_m
        # Legacy path: aircraft sits at mesh origin; cos at aircraft lat.
        _terrain_prog['u_cos_mesh_lat'].value     = max(
            1e-6, math.cos(math.radians(lat)))
        _terrain_prog['u_world_offset'].value     = (0.0, 0.0)
        _terrain_prog['u_aircraft_xy'].value      = (0.0, 0.0)
        _terrain_prog['u_grid_spacing_m'].value   = GRID_SPACING_NM * NM_TO_M
        _terrain_prog['u_grid_major_every'].value = float(GRID_MAJOR_EVERY)
        _terrain_prog['u_grid_max_dist_m'].value  = _mesh_radius_m
        _terrain_prog['u_discard_inside_m'].value = 0.0
        _terrain_prog['u_water_enable'].value     = 0.0   # standalone has no water data
        _terrain_prog['u_alert_enable'].value     = 1.0   # standalone has no speed gate
        # Sun direction vector (world frame: X=East, Y=North, Z=Up).
        # Caller-supplied az/el override the module defaults so a real-time
        # solar-position feed can drive the lighting from UTC + GPS.
        _az = SUN_AZIMUTH_DEG   if sun_az_deg   is None else sun_az_deg
        _el = SUN_ELEVATION_DEG if sun_el_deg   is None else sun_el_deg
        _si = SUN_INTENSITY     if sun_intensity is None else sun_intensity
        az_rad = math.radians(_az)
        el_rad = math.radians(_el)
        sun_x = math.cos(el_rad) * math.sin(az_rad)
        sun_y = math.cos(el_rad) * math.cos(az_rad)
        sun_z = math.sin(el_rad)
        _terrain_prog['u_sun_dir'].value       = (sun_x, sun_y, sun_z)
        _terrain_prog['u_sun_intensity'].value = _si
        _terrain_prog['u_ambient'].value       = SUN_AMBIENT
        _terrain_vao.render()

    # 3D polylines (HITS boxes + direct-to course trace) — drawn AFTER
    # the terrain pass so the terrain's depth buffer occludes segments
    # hidden behind ridges.
    if polylines:
        for verts, rgba, width in polylines:
            _render_standalone_polyline(mvp, verts, rgba, width)

    # Read pixels back into pygame Surface (flip Y: OpenGL origin is bottom-left)
    raw = _fbo.read(components=3, alignment=1)
    arr = np.frombuffer(raw, dtype=np.uint8).reshape((ai_h, ai_w, 3))[::-1, :, :]
    surf = pygame.image.frombuffer(arr.tobytes(), (ai_w, ai_h), 'RGB')
    return surf


def is_available() -> bool:
    """Lightweight check — can we create an EGL context at all?
    Does NOT keep the context alive (that would lock the GPU and prevent
    pygame's KMS/DRM display from initialising).  The real context is
    created lazily on first render via _init_gl()."""
    if not (HAS_MODERNGL and HAS_NUMPY):
        return False
    try:
        ctx = moderngl.create_standalone_context(backend='egl', require=300)
        renderer = ctx.info.get("GL_RENDERER", "unknown")
        print(f"[SVT-GL] EGL probe OK: {renderer}")
        ctx.release()
        return True
    except Exception as e:
        print(f"[SVT-GL] EGL probe failed: {e}")
        return False


# ── Shared-context path (pygame.OPENGL composite) ─────────────────────────────
# Companion to render_svt_gl() above.  Instead of creating its own standalone
# EGL context and rendering to an offscreen FBO, this path assumes the caller
# already owns a moderngl Context (typically attached to pygame's GL surface
# via moderngl.create_context()) and renders directly into the currently-bound
# framebuffer (usually the default screen framebuffer).  There is NO pixel
# readback — the output stays on the GPU, ready to be composited with the 2D
# PFD overlay by svt_composite_gl.Compositor.
#
# State is cached per-context in _SharedState (keyed by id(ctx)) so multiple
# ctxs won't cross-contaminate; in practice pfd.py creates exactly one.
_shared_state = {}


class _SharedState:
    __slots__ = ("ctx", "terrain_prog", "sky_prog", "sky_vao",
                 # Inner (high-detail) mesh — ~20 nm
                 "terrain_vao", "terrain_vbo_pos", "terrain_vbo_water",
                 "terrain_ibo", "mesh_key", "mesh_radius_m",
                 "mesh_center_lat", "mesh_center_lon",
                 # Outer (far-LOD) mesh — ~75 nm, coarse silhouettes
                 "outer_vao", "outer_vbo_pos", "outer_vbo_water", "outer_ibo",
                 "outer_mesh_key", "outer_mesh_radius_m",
                 "outer_mesh_center_lat", "outer_mesh_center_lon",
                 # Water-mask data dir (set by render_svt_into_current_fb).
                 # Empty string disables water sampling.
                 "water_dir",
                 # Direct-to polyline
                 "line_prog", "line_vbo", "line_vao", "line_capacity",
                 "_last_line_width",
                 # Async mesh-rebuild state (worker thread + pending CPU
                 # result + target key).  See build_mesh / build_outer_mesh.
                 "_inner_thread", "_inner_pending", "_inner_target_key",
                 "_outer_thread", "_outer_pending", "_outer_target_key")

    def __init__(self, ctx):
        self.ctx = ctx
        self.terrain_prog = ctx.program(vertex_shader=VERTEX_SHADER,
                                        fragment_shader=FRAGMENT_SHADER)
        self.sky_prog = ctx.program(vertex_shader=SKY_VERTEX_SHADER,
                                    fragment_shader=SKY_FRAGMENT_SHADER)
        sky_verts = np.array([
            -1, -1,   1, -1,   1,  1,
            -1, -1,   1,  1,  -1,  1,
        ], dtype=np.float32)
        sky_vbo = ctx.buffer(sky_verts.tobytes())
        self.sky_vao = ctx.vertex_array(self.sky_prog,
                                        [(sky_vbo, '2f', 'in_pos')])
        self.terrain_vao = None
        self.terrain_vbo_pos = None
        self.terrain_vbo_water = None
        self.terrain_ibo = None
        self.mesh_key = None
        self.mesh_radius_m = MESH_RADIUS_NM * NM_TO_M
        self.mesh_center_lat = 0.0
        self.mesh_center_lon = 0.0
        self.outer_vao = None
        self.outer_vbo_pos = None
        self.outer_vbo_water = None
        self.outer_ibo = None
        self.outer_mesh_key = None
        self.outer_mesh_radius_m = OUTER_MESH_RADIUS_NM * NM_TO_M
        self.outer_mesh_center_lat = 0.0
        self.outer_mesh_center_lon = 0.0
        self.water_dir = ""
        # Polyline rendering — single reusable VBO, grown as needed.
        self.line_prog = ctx.program(vertex_shader=LINE_VERTEX_SHADER,
                                     fragment_shader=LINE_FRAGMENT_SHADER)
        self.line_vbo = ctx.buffer(reserve=4096)   # ~340 vec3 vertices
        self.line_vao = ctx.vertex_array(
            self.line_prog, [(self.line_vbo, '3f', 'in_pos')])
        self.line_capacity = 4096
        self._last_line_width = None

        # Async mesh-rebuild state.  When the snap grid moves we kick off
        # a background thread that does the CPU-side numpy work (SRTM
        # sampling, water mask sampling, airport burn, position/index
        # array build) and stashes the result in `_pending_*`.  The next
        # call to build_mesh / build_outer_mesh on the main thread sees
        # the pending result and does the (small) GL upload.  The OLD
        # mesh keeps rendering until the swap, so the per-snap-step
        # 100-500 ms stall is hidden.
        self._inner_thread       = None
        self._inner_pending      = None
        self._inner_target_key   = None
        self._outer_thread       = None
        self._outer_pending      = None
        self._outer_target_key   = None

    def render_polyline_latlonelev(self, mvp, vertices_latlonelev,
                                    rgba=(0.86, 0.0, 0.86, 1.0),
                                    line_width=3.0):
        """Render a depth-tested polyline through the terrain's depth buffer.

        vertices_latlonelev: ndarray of shape (N, 3) — (lat_deg, lon_deg, elev_ft).
        Vertices are converted to the mesh-local metric frame so they share
        the same MVP as the terrain mesh; the depth buffer set by the terrain
        therefore occludes line segments hidden behind ridges.
        """
        if vertices_latlonelev is None or len(vertices_latlonelev) < 2:
            return
        v = np.asarray(vertices_latlonelev, dtype=np.float32)
        cos_mlat = max(1e-6, math.cos(math.radians(self.mesh_center_lat)))
        # (East, North, Up_absolute) in the mesh-local frame the terrain uses.
        east_m  = (v[:, 1] - self.mesh_center_lon) * 60.0 * NM_TO_M * cos_mlat
        north_m = (v[:, 0] - self.mesh_center_lat) * 60.0 * NM_TO_M
        up_m    = v[:, 2] * FT_TO_M
        world = np.stack([east_m, north_m, up_m], axis=1).astype(np.float32)
        data = world.tobytes()
        # Grow the GPU buffer if the polyline outgrew it.
        if len(data) > self.line_capacity:
            self.line_vbo.release()
            self.line_vao.release()
            self.line_capacity = max(len(data), self.line_capacity * 2)
            self.line_vbo = self.ctx.buffer(reserve=self.line_capacity)
            self.line_vao = self.ctx.vertex_array(
                self.line_prog, [(self.line_vbo, '3f', 'in_pos')])
        self.line_vbo.write(data)
        self.line_prog['u_mvp'].write(mvp.T.tobytes())
        self.line_prog['u_color'].value = rgba
        # Width support is driver-dependent; many GLES drivers cap at 1 px.
        # Set anyway so V3D and llvmpipe can honour wider lines when they do.
        # Cached: line_width can be a sync-causing GL state change on some
        # drivers — only push it to the GPU when the value actually changes.
        if self._last_line_width != line_width:
            try:
                self.ctx.line_width = float(line_width)
            except Exception:
                pass
            self._last_line_width = line_width
        self.line_vao.render(mode=moderngl.LINE_STRIP, vertices=len(world))

    def render_polylines_latlonelev_batched(self, mvp, polylines):
        """Draw many depth-tested polylines in as few GL calls as possible.

        Groups the polylines by (colour, width) and emits ONE GL_LINES batch per
        group — a single buffer upload + a single draw call — instead of one of
        each per polyline.  The per-polyline path wrote the shared line VBO and
        drew it once for every box, which forced a CPU↔GPU buffer-sync stall per
        box; with ~100 HITS boxes up that dropped the frame rate to a crawl.
        """
        if not polylines:
            return
        cos_mlat = max(1e-6, math.cos(math.radians(self.mesh_center_lat)))
        groups = {}                       # (rgba, width) → [segment arrays]
        for verts, rgba, width in polylines:
            if verts is None or len(verts) < 2:
                continue
            v = np.asarray(verts, dtype=np.float32)
            east_m  = (v[:, 1] - self.mesh_center_lon) * 60.0 * NM_TO_M * cos_mlat
            north_m = (v[:, 0] - self.mesh_center_lat) * 60.0 * NM_TO_M
            up_m    = v[:, 2] * FT_TO_M
            world = np.stack([east_m, north_m, up_m], axis=1).astype(np.float32)
            # Expand the strip into GL_LINES segment pairs: v0v1 v1v2 v2v3 …
            seg = np.empty((2 * (len(world) - 1), 3), dtype=np.float32)
            seg[0::2] = world[:-1]
            seg[1::2] = world[1:]
            groups.setdefault((tuple(rgba), float(width)), []).append(seg)
        if not groups:
            return
        self.line_prog['u_mvp'].write(mvp.T.tobytes())
        for (rgba, width), segs in groups.items():
            data_arr = np.concatenate(segs, axis=0) if len(segs) > 1 else segs[0]
            data = data_arr.tobytes()
            if len(data) > self.line_capacity:
                self.line_vbo.release()
                self.line_vao.release()
                self.line_capacity = max(len(data), self.line_capacity * 2)
                self.line_vbo = self.ctx.buffer(reserve=self.line_capacity)
                self.line_vao = self.ctx.vertex_array(
                    self.line_prog, [(self.line_vbo, '3f', 'in_pos')])
            self.line_vbo.write(data)
            self.line_prog['u_color'].value = rgba
            if self._last_line_width != width:
                try:
                    self.ctx.line_width = float(width)
                except Exception:
                    pass
                self._last_line_width = width
            self.line_vao.render(mode=moderngl.LINES, vertices=len(data_arr))

    def _tier_target_key(self, lat, lon, radius_nm, grid_n, airports_arr):
        """Compute the cache key a tier WOULD have for this position.
        Used by the async dispatcher to decide whether the cached mesh
        is still valid before paying for a CPU build."""
        radius_m = radius_nm * NM_TO_M
        sample_step_m = (2.0 * radius_m) / (grid_n - 1)
        m_per_deg_lat = 60.0 * NM_TO_M
        snap_dlat = sample_step_m / m_per_deg_lat
        mesh_lat = round(lat / snap_dlat) * snap_dlat
        snap_dlon = snap_dlat
        mesh_lon = round(lon / snap_dlon) * snap_dlon
        apt_marker = 0 if airports_arr is None else len(airports_arr)
        return (round(mesh_lat, 6), round(mesh_lon, 6),
                grid_n, round(radius_nm, 1), apt_marker)

    def _build_tier_mesh_cpu(self, srtm_dir, lat, lon, radius_nm, grid_n,
                             current_key, airports_arr=None):
        """CPU-only half of the mesh build — SRTM/water sampling, airport
        burn, position + index array assembly.  Returns a dict of numpy
        arrays + meta, or None when the cached tier is still valid.

        This routine does NOT touch the GL context, so it's safe to call
        from a worker thread.  The tiny GL upload step lives in
        _upload_tier_mesh and runs on the main thread.
        """
        radius_m = radius_nm * NM_TO_M
        sample_step_m = (2.0 * radius_m) / (grid_n - 1)

        # Snap mesh_lat to a fixed lat grid (lat snap is metric — no cos
        # dependency).
        m_per_deg_lat = 60.0 * NM_TO_M
        snap_dlat = sample_step_m / m_per_deg_lat
        mesh_lat = round(lat / snap_dlat) * snap_dlat

        # Snap mesh_lon to a *cos-independent* grid: equator-equivalent
        # angular step, same as snap_dlat (see comment in upstream version
        # of this routine for why the cos factor must NOT be in snap_dlon).
        snap_dlon = snap_dlat
        mesh_lon = round(lon / snap_dlon) * snap_dlon

        cos_snap = max(1e-6, math.cos(math.radians(mesh_lat)))

        # Key includes radius/grid so a tier change forces a rebuild even
        # if mesh_lat/lon happen to land on the same snapped point.  The
        # `apt_marker` differentiates "no airports loaded yet" from "airports
        # available" so the mesh rebuilds once the async airport DB loader
        # finishes — otherwise initial frames at start-up cache an un-burned
        # mesh and KSQL stays in the bay until the aircraft moves enough to
        # trigger a snap-step rebuild.
        apt_marker = 0 if airports_arr is None else len(airports_arr)
        key = (round(mesh_lat, 6), round(mesh_lon, 6),
               grid_n, round(radius_nm, 1), apt_marker)
        if key == current_key:
            return None

        # Sample grid: integer multiples of snap_dlat from mesh centre, in
        # both axes. North step is metric; east step is metric * cos(mesh_lat).
        n_half = int(round(radius_m / sample_step_m))
        i_array = np.arange(-n_half, n_half + 1, dtype=np.float32)
        north_1d = i_array * sample_step_m
        east_1d  = i_array * sample_step_m * cos_snap
        east, north = np.meshgrid(east_1d, north_1d)
        n = i_array.size

        cos_lat = max(1e-6, math.cos(math.radians(mesh_lat)))
        dlat = north / NM_TO_M / 60.0
        dlon = east  / NM_TO_M / 60.0 / cos_lat
        sample_lat = mesh_lat + dlat
        sample_lon = mesh_lon + dlon

        elev_ft = np.zeros((n, n), dtype=np.float32)
        water_flag = np.zeros((n, n), dtype=np.float32)
        lat_int_arr = np.floor(sample_lat).astype(np.int32)
        lon_int_arr = np.floor(sample_lon).astype(np.int32)
        enc = ((lat_int_arr.astype(np.int64) + 90) * 1000 +
               (lon_int_arr.astype(np.int64) + 360))
        water_dir = self.water_dir
        for tile_key in np.unique(enc):
            tla = int(tile_key) // 1000 - 90
            tlo = int(tile_key) %  1000 - 360
            tile_mask = (lat_int_arr == tla) & (lon_int_arr == tlo)
            if not tile_mask.any():
                continue
            result = load_tile(srtm_dir, tla, tlo)
            if result is not None:
                tile_data, n_s = result
                step = 1.0 / (n_s - 1)
                row_i = np.clip(np.round((tla + 1 - sample_lat) / step).astype(np.int32),
                                0, n_s - 1)
                col_i = np.clip(np.round((sample_lon - tlo) / step).astype(np.int32),
                                0, n_s - 1)
                elev_ft[tile_mask] = tile_data[row_i, col_i][tile_mask]
            # Water mask, sampled the same way.  Optional — tiles without
            # a companion .water file just leave water_flag at 0 (all land).
            if HAS_WATER and water_dir and load_water_tile is not None:
                wres = load_water_tile(water_dir, tla, tlo)
                if wres is not None:
                    wmask, w_n = wres
                    wstep = 1.0 / (w_n - 1)
                    wrow = np.clip(np.round((tla + 1 - sample_lat) / wstep).astype(np.int32),
                                   0, w_n - 1)
                    wcol = np.clip(np.round((sample_lon - tlo) / wstep).astype(np.int32),
                                   0, w_n - 1)
                    water_flag[tile_mask] = wmask[wrow, wcol][tile_mask].astype(np.float32)

        # Override water_flag → 0 for vertices near known airport
        # positions.  Natural Earth 10 m coastlines aren't precise enough
        # to capture e.g. KSQL's peninsula in San Francisco Bay, so the
        # raw water mask paints those airports under water.  Burning a
        # small disk of "land" around each airport restores them.
        if airports_arr is not None and len(airports_arr) > 0:
            extent_deg = (radius_nm / 60.0) + 0.05
            apt_lat = airports_arr['lat']
            apt_lon = airports_arr['lon']
            in_range = ((apt_lat >= mesh_lat - extent_deg) &
                        (apt_lat <= mesh_lat + extent_deg) &
                        (apt_lon >= mesh_lon - extent_deg / cos_snap) &
                        (apt_lon <= mesh_lon + extent_deg / cos_snap))
            nearby_apts = airports_arr[in_range]
            if len(nearby_apts) > 0:
                # ~0.3 nm radius covers most GA fields; bigger fields
                # like KOAK/KLAX are still mostly captured because their
                # SRTM elevations + the runway polygons drawn over the
                # AI overlay anchor them visually.
                burn_deg_sq = (0.3 / 60.0) ** 2
                # Vectorised distance test: stack airports to (M, 1, 1) and
                # broadcast against the (n, n) sample grid in one shot
                # instead of looping in Python.  Memory peak is M·n·n bools
                # (~1.8 MB at M=20, n=300) which is well under what the
                # per-iter dlat/dlon allocations cost in the loop form.
                a_lat = np.asarray(nearby_apts['lat'],
                                   dtype=np.float32).reshape(-1, 1, 1)
                a_lon = np.asarray(nearby_apts['lon'],
                                   dtype=np.float32).reshape(-1, 1, 1)
                dlat_a = sample_lat[None, :, :] - a_lat
                dlon_a = (sample_lon[None, :, :] - a_lon) * cos_snap
                near_apt = ((dlat_a * dlat_a + dlon_a * dlon_a)
                            < burn_deg_sq).any(axis=0)
                water_flag[near_apt] = 0.0

        # SRTM1 (3601×3601) tiles ship with real ocean bathymetry —
        # N33W119 reaches –5800 ft.  Without clamping, water vertices
        # render as a deep crater off the coast.  Clamping to ≥ 0 leaves
        # every land sample untouched (KLAX's 125 ft is preserved) and
        # pulls only sub-sea-level samples up to the waterline.
        np.maximum(elev_ft, 0.0, out=elev_ft)

        elev_m = elev_ft * FT_TO_M

        # Z = absolute elevation. Aircraft alt is applied per-frame in the
        # vertex shader (u_alt_m uniform).
        positions = np.stack([east, north, elev_m], axis=-1).astype(np.float32)

        i, j = np.meshgrid(np.arange(n - 1), np.arange(n - 1), indexing='ij')
        v0 = (i     * n + j    ).astype(np.uint32)
        v1 = (i     * n + j + 1).astype(np.uint32)
        v2 = ((i+1) * n + j    ).astype(np.uint32)
        v3 = ((i+1) * n + j + 1).astype(np.uint32)
        tri1 = np.stack([v0, v2, v1], axis=-1).reshape(-1)
        tri2 = np.stack([v1, v2, v3], axis=-1).reshape(-1)
        indices = np.concatenate([tri1, tri2]).astype(np.uint32)

        return {
            'positions': positions,
            'water_flag': water_flag,
            'indices': indices,
            'key': key,
            'mesh_lat': mesh_lat,
            'mesh_lon': mesh_lon,
            'radius_m': radius_m,
        }

    def _upload_tier_mesh(self, cpu_result):
        """GL-side half of the mesh build — must run on the main thread.
        Allocates VBO/IBO/VAO from the numpy arrays produced by
        _build_tier_mesh_cpu and returns a dict the caller swaps into
        the tier slot."""
        positions  = cpu_result['positions']
        water_flag = cpu_result['water_flag']
        indices    = cpu_result['indices']
        new_vbo       = self.ctx.buffer(positions.tobytes())
        new_vbo_water = self.ctx.buffer(water_flag.flatten().tobytes())
        new_ibo       = self.ctx.buffer(indices.tobytes())
        new_vao = self.ctx.vertex_array(
            self.terrain_prog,
            [(new_vbo,       '3f', 'in_pos'),
             (new_vbo_water, '1f', 'in_water')],
            index_buffer=new_ibo,
        )
        return {
            'vao': new_vao, 'vbo': new_vbo, 'vbo_water': new_vbo_water,
            'ibo': new_ibo,
            'key': cpu_result['key'],
            'mesh_lat': cpu_result['mesh_lat'],
            'mesh_lon': cpu_result['mesh_lon'],
            'radius_m': cpu_result['radius_m'],
        }

    def _swap_inner(self, cpu_result):
        """Upload a CPU-built inner mesh and swap it in.  Releases the
        previous tier's GL buffers."""
        new = self._upload_tier_mesh(cpu_result)
        if self.terrain_vao is not None:
            self.terrain_vao.release()
            self.terrain_vbo_pos.release()
            if self.terrain_vbo_water is not None:
                self.terrain_vbo_water.release()
            self.terrain_ibo.release()
        self.terrain_vao = new['vao']
        self.terrain_vbo_pos = new['vbo']
        self.terrain_vbo_water = new['vbo_water']
        self.terrain_ibo = new['ibo']
        self.mesh_key = new['key']
        self.mesh_center_lat = new['mesh_lat']
        self.mesh_center_lon = new['mesh_lon']

    def _swap_outer(self, cpu_result):
        new = self._upload_tier_mesh(cpu_result)
        if self.outer_vao is not None:
            self.outer_vao.release()
            self.outer_vbo_pos.release()
            if self.outer_vbo_water is not None:
                self.outer_vbo_water.release()
            self.outer_ibo.release()
        self.outer_vao = new['vao']
        self.outer_vbo_pos = new['vbo']
        self.outer_vbo_water = new['vbo_water']
        self.outer_ibo = new['ibo']
        self.outer_mesh_key = new['key']
        self.outer_mesh_center_lat = new['mesh_lat']
        self.outer_mesh_center_lon = new['mesh_lon']
        self.outer_mesh_radius_m = new['radius_m']

    def build_mesh(self, srtm_dir, lat, lon, alt_ft, airports_arr=None):
        """Inner mesh — high resolution, small radius for foreground detail.

        Async: kicks off the CPU half in a worker thread when the snap
        grid moves and the previous build (if any) has finished.  The
        old mesh keeps rendering until the worker's result lands."""
        if MESH_SIZE_MODE == "altitude":
            r_nm = max(MESH_RADIUS_MIN_NM,
                       min(MESH_RADIUS_MAX_NM,
                           6.0 * math.sqrt(max(100.0, alt_ft) / 1000.0)))
        else:
            r_nm = MESH_RADIUS_NM
        self.mesh_radius_m = r_nm * NM_TO_M

        # 1. If the worker stashed a result since the last frame, swap
        #    it in (cheap GL upload — buffer + vao, no SRTM I/O).
        if self._inner_pending is not None:
            cpu_result = self._inner_pending
            self._inner_pending = None
            self._swap_inner(cpu_result)

        # First frame: no mesh on the GPU at all.  Build synchronously so
        # the user doesn't see a blank AI for the ~100 ms it takes the
        # worker to finish.  Subsequent rebuilds are async.
        if self.mesh_key is None:
            cpu_result = self._build_tier_mesh_cpu(
                srtm_dir, lat, lon, r_nm, MESH_GRID_N, None,
                airports_arr=airports_arr)
            if cpu_result is not None:
                self._swap_inner(cpu_result)
            return

        # 2. Decide whether to dispatch a new build.
        target_key = self._tier_target_key(lat, lon, r_nm, MESH_GRID_N,
                                           airports_arr)
        if target_key == self.mesh_key:
            return  # cached mesh still valid
        if (self._inner_thread is not None
                and self._inner_thread.is_alive()
                and target_key == self._inner_target_key):
            return  # already building this exact tile, don't pile up
        if self._inner_thread is not None and self._inner_thread.is_alive():
            return  # different target — let the in-flight build finish first

        self._inner_target_key = target_key
        self._inner_thread = threading.Thread(
            target=self._inner_worker,
            args=(srtm_dir, lat, lon, r_nm, MESH_GRID_N, airports_arr),
            daemon=True,
        )
        self._inner_thread.start()

    def _inner_worker(self, srtm_dir, lat, lon, r_nm, grid_n, airports_arr):
        try:
            cpu_result = self._build_tier_mesh_cpu(
                srtm_dir, lat, lon, r_nm, grid_n,
                self.mesh_key, airports_arr=airports_arr)
        except Exception as e:
            print(f"[SVT-GL] inner mesh worker failed: {e}")
            return
        if cpu_result is not None:
            self._inner_pending = cpu_result

    def build_outer_mesh(self, srtm_dir, lat, lon, alt_ft, airports_arr=None):
        """Outer mesh — coarse, large radius.  Same async pattern as
        build_mesh."""
        if self._outer_pending is not None:
            cpu_result = self._outer_pending
            self._outer_pending = None
            self._swap_outer(cpu_result)

        if self.outer_mesh_key is None:
            cpu_result = self._build_tier_mesh_cpu(
                srtm_dir, lat, lon,
                OUTER_MESH_RADIUS_NM, OUTER_MESH_GRID_N, None,
                airports_arr=airports_arr)
            if cpu_result is not None:
                self._swap_outer(cpu_result)
            return

        target_key = self._tier_target_key(lat, lon,
                                           OUTER_MESH_RADIUS_NM,
                                           OUTER_MESH_GRID_N, airports_arr)
        if target_key == self.outer_mesh_key:
            return
        if (self._outer_thread is not None
                and self._outer_thread.is_alive()
                and target_key == self._outer_target_key):
            return
        if self._outer_thread is not None and self._outer_thread.is_alive():
            return

        self._outer_target_key = target_key
        self._outer_thread = threading.Thread(
            target=self._outer_worker,
            args=(srtm_dir, lat, lon, airports_arr),
            daemon=True,
        )
        self._outer_thread.start()

    def _outer_worker(self, srtm_dir, lat, lon, airports_arr):
        try:
            cpu_result = self._build_tier_mesh_cpu(
                srtm_dir, lat, lon,
                OUTER_MESH_RADIUS_NM, OUTER_MESH_GRID_N,
                self.outer_mesh_key, airports_arr=airports_arr)
        except Exception as e:
            print(f"[SVT-GL] outer mesh worker failed: {e}")
            return
        if cpu_result is not None:
            self._outer_pending = cpu_result


def render_svt_into_current_fb(
    ctx,
    srtm_dir: str,
    ai_w: int,
    ai_h: int,
    pitch_deg: float,
    roll_deg: float,
    hdg_deg: float,
    alt_ft: float,
    lat: float,
    lon: float,
    v_fov_deg: float = V_FOV_DEG,
    polylines=None,
    water_dir: str = "",
    water_enable: bool = True,
    airports_arr=None,
    sun_az_deg: float | None = None,
    sun_el_deg: float | None = None,
    sun_intensity: float | None = None,
    below_horizon_color: tuple = (0.35, 0.27, 0.15),
    alert_enable: bool = True,
) -> bool:
    """Render the SVT terrain+sky directly into the currently-bound
    framebuffer using the caller-provided moderngl context.

    Unlike render_svt_gl(), this does NOT create an EGL context, does NOT
    allocate an FBO, and does NOT read pixels back.  Intended for the
    pygame.OPENGL shared-context composite path: pfd.py owns the GL
    context via pygame.display.set_mode(..., pygame.OPENGL) and moderngl
    attached to it via create_context().

    The caller is responsible for binding the framebuffer (e.g.
    ctx.screen.use()) and setting the viewport BEFORE calling this —
    that way a single render can target the full display or a sub-region.

    Returns True on success, False if moderngl/numpy unavailable.
    """
    if not (HAS_MODERNGL and HAS_NUMPY):
        return False

    st = _shared_state.get(id(ctx))
    if st is None:
        st = _SharedState(ctx)
        _shared_state[id(ctx)] = st

    # Update water dir before building meshes so the new value is applied on
    # any rebuild this frame.  Changing water_dir invalidates the existing
    # meshes (per-vertex water flag depends on it), so trigger a rebuild
    # by clearing the cache keys.
    if st.water_dir != water_dir:
        st.water_dir = water_dir
        st.mesh_key = None
        st.outer_mesh_key = None

    st.build_mesh(srtm_dir, lat, lon, alt_ft, airports_arr=airports_arr)
    st.build_outer_mesh(srtm_dir, lat, lon, alt_ft, airports_arr=airports_arr)

    alt_m = alt_ft * FT_TO_M
    fwd, up = _attitude_basis(pitch_deg, roll_deg, hdg_deg)
    proj = _perspective(v_fov_deg, ai_w / ai_h, NEAR_PLANE_M, FAR_PLANE_M)

    ctx.enable(moderngl.DEPTH_TEST)
    # Explicit depth clear — without it, per-frame depth state from the 2D
    # composite pass would persist and cause terrain depth fighting.
    ctx.clear(0.04, 0.16, 0.31, 1.0, depth=1.0)

    horizon_y = _horizon_y_ndc(pitch_deg, v_fov_deg)
    st.sky_prog['u_horizon_y'].value = horizon_y
    st.sky_prog['u_roll_rad'].value  = math.radians(roll_deg)
    st.sky_prog['u_aspect'].value    = ai_w / ai_h
    st.sky_prog['u_below_horizon_color'].value = below_horizon_color
    ctx.disable(moderngl.DEPTH_TEST)
    st.sky_vao.render()
    ctx.enable(moderngl.DEPTH_TEST)

    # Caller-supplied az/el override the module defaults so a real-time
    # solar-position feed can drive lighting from UTC + GPS.
    _az = SUN_AZIMUTH_DEG   if sun_az_deg    is None else sun_az_deg
    _el = SUN_ELEVATION_DEG if sun_el_deg    is None else sun_el_deg
    _si = SUN_INTENSITY     if sun_intensity is None else sun_intensity
    az_rad = math.radians(_az)
    el_rad = math.radians(_el)
    sun_dir = (
        math.cos(el_rad) * math.sin(az_rad),  # east
        math.cos(el_rad) * math.cos(az_rad),  # north
        math.sin(el_rad),                     # up
    )
    grid_period_m = GRID_SPACING_NM * NM_TO_M * GRID_MAJOR_EVERY

    def _render_tier(vao, mesh_lat, mesh_lon, mesh_radius_m,
                     grid_spacing_m, grid_max_dist_m, discard_inside_m=0.0):
        """Render one mesh tier with the right per-tier uniforms.

        discard_inside_m > 0 makes the fragment shader drop pixels inside
        a square of that half-extent around the aircraft.  Used for the
        outer mesh to keep it from clobbering the inner mesh's foreground.
        """
        cos_mlat = max(1e-6, math.cos(math.radians(mesh_lat)))
        cam_north = (lat - mesh_lat) * 60.0 * NM_TO_M
        cam_east  = (lon - mesh_lon) * 60.0 * NM_TO_M * cos_mlat
        eye = np.array([cam_east, cam_north, alt_m], dtype=np.float32)
        target = eye + fwd
        view = _look_at(eye, target, up)
        local_mvp = proj @ view

        st.terrain_prog['u_mvp'].write(local_mvp.T.tobytes())
        st.terrain_prog['u_alt_m'].value        = alt_m
        st.terrain_prog['u_cos_mesh_lat'].value = cos_mlat
        st.terrain_prog['u_aircraft_xy'].value  = (cam_east, cam_north)
        mesh_world_e_eq = mesh_lon * 60.0 * NM_TO_M
        mesh_world_n    = mesh_lat * 60.0 * NM_TO_M
        st.terrain_prog['u_world_offset'].value = (
            mesh_world_e_eq % grid_period_m,
            mesh_world_n    % grid_period_m,
        )
        st.terrain_prog['u_grid_spacing_m'].value   = grid_spacing_m
        st.terrain_prog['u_grid_major_every'].value = float(GRID_MAJOR_EVERY)
        st.terrain_prog['u_grid_max_dist_m'].value  = grid_max_dist_m
        st.terrain_prog['u_discard_inside_m'].value = float(discard_inside_m)
        st.terrain_prog['u_water_enable'].value     = 1.0 if water_enable else 0.0
        st.terrain_prog['u_alert_enable'].value     = 1.0 if alert_enable else 0.0
        st.terrain_prog['u_sun_dir'].value       = sun_dir
        st.terrain_prog['u_sun_intensity'].value = _si
        st.terrain_prog['u_ambient'].value       = SUN_AMBIENT
        vao.render()
        return local_mvp

    # Outer mesh first — distant ridges paint silhouettes behind the inner
    # mesh's foreground detail.  Grid lines disabled (u_grid_spacing_m = 0)
    # because they'd look weird drawn over coarse 4–5 km triangles.
    # discard_inside_m sits well INSIDE the inner mesh's nominal radius so
    # the two tiers overlap by a wide band — eliminates any visible gap from
    # the inner mesh's snap-grid offset, perspective foreshortening, or
    # the discard's square-vs-mesh-square corner mismatch.  Inner draws
    # second so it owns the foreground; the wide overlap is cheap because
    # the outer mesh has only ~3.6 K verts.
    if st.outer_vao is not None:
        _render_tier(st.outer_vao,
                     st.outer_mesh_center_lat, st.outer_mesh_center_lon,
                     st.outer_mesh_radius_m,
                     grid_spacing_m=0.0,
                     grid_max_dist_m=st.outer_mesh_radius_m,
                     discard_inside_m=st.mesh_radius_m * 0.80)

    # Inner mesh — overdraws the outer in the 0–20 nm zone.  Cyan
    # distance-grid lines are enabled here.
    inner_mvp = None
    if st.terrain_vao is not None:
        inner_mvp = _render_tier(st.terrain_vao,
                                  st.mesh_center_lat, st.mesh_center_lon,
                                  st.mesh_radius_m,
                                  grid_spacing_m=GRID_SPACING_NM * NM_TO_M,
                                  grid_max_dist_m=st.mesh_radius_m)

    # 3D polylines (e.g. magenta direct-to course trace) — drawn AFTER terrain
    # with depth test still on, so the depth buffer set by both terrain
    # tiers occludes line segments that fall behind ridges.  Polyline uses
    # the inner mesh's MVP frame (course is mostly within mesh radius).
    if polylines and inner_mvp is not None:
        st.render_polylines_latlonelev_batched(inner_mvp, polylines)

    ctx.disable(moderngl.DEPTH_TEST)
    return True
