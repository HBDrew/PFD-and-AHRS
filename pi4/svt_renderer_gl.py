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

# ── Constants ─────────────────────────────────────────────────────────────────
MESH_RADIUS_NM    = 20.0        # nm — terrain mesh extent around aircraft
MESH_GRID_N       = 300         # mesh resolution (300×300 = 90K vertices)

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
FAR_PLANE_M    = MESH_RADIUS_NM * NM_TO_M * 1.5

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

out float v_clearance_ft;
out vec3 v_world_pos;
out float v_dist_m;

void main() {
    gl_Position = u_mvp * vec4(in_pos, 1.0);
    v_clearance_ft = (u_alt_m - in_pos.z) * 3.28084;
    // Equator-equivalent metric — invariant per world point regardless of
    // which mesh (which mesh_lat) we're in. North is already metric.
    float vx_eq = in_pos.x / u_cos_mesh_lat;
    v_world_pos = vec3(vec2(vx_eq, in_pos.y) + u_world_offset, in_pos.z);
    v_dist_m = length(in_pos.xy);
}
"""

FRAGMENT_SHADER = """
#version 300 es
precision highp float;

in float v_clearance_ft;
in vec3 v_world_pos;
in float v_dist_m;
out vec4 frag_color;

uniform float u_grid_spacing_m;     // metres per grid square (e.g. 1852 = 1 nm)
uniform float u_grid_major_every;   // major line every N squares (e.g. 5)
uniform float u_grid_max_dist_m;    // grid fades to invisible at this distance
uniform vec3  u_sun_dir;            // unit vector pointing TOWARD the sun
uniform float u_sun_intensity;      // 0.0 = no lighting, 1.0 = full
uniform float u_ambient;            // 0.0 = pitch black shadows, 1.0 = no shadow

// Clearance-based color palette (matches pygame PALETTE_RELATIVE)
vec3 clearance_color(float c) {
    if (c < 0.0)    return vec3(0.86, 0.12, 0.12);
    if (c < 100.0)  return vec3(0.86, 0.31, 0.0);
    if (c < 500.0)  return vec3(0.78, 0.51, 0.0);
    if (c < 1000.0) return vec3(0.55, 0.39, 0.16);
    if (c < 2000.0) return vec3(0.39, 0.29, 0.14);
    return vec3(0.27, 0.22, 0.11);
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
    vec3 base = clearance_color(v_clearance_ft);

    // ── Sun-angle lighting ───────────────────────────────────────────────
    // Simple Lambertian diffuse term on the terrain color.  Faces pointing
    // toward the sun appear brighter; faces in shadow darken toward ambient.
    if (u_sun_intensity > 0.001) {
        vec3 n = compute_normal();
        float diffuse = max(0.0, dot(n, u_sun_dir));
        // light_factor: at N·L = 0 → ambient; at N·L = 1 → full illumination
        float light = mix(u_ambient, 1.0, diffuse) * u_sun_intensity
                    + (1.0 - u_sun_intensity);
        base *= light;
    }

    // Distance-based grid fade: full strength near, fades out at u_grid_max_dist_m
    float fade = 1.0 - smoothstep(u_grid_max_dist_m * 0.5, u_grid_max_dist_m, v_dist_m);

    if (fade > 0.01 && u_grid_spacing_m > 0.0) {
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
    gl_Position = vec4(in_pos, 0.999, 1.0);   // far plane — drawn behind terrain
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

void main() {
    // Un-roll the NDC point so horizon becomes a horizontal line again.
    // Stretch x by aspect ratio so the rotation is angle-preserving (otherwise
    // a banked horizon would appear at the wrong angle on non-square displays).
    float x_sq = v_ndc.x * u_aspect;
    float y_sq = v_ndc.y;
    float c = cos(u_roll_rad);
    float s = sin(u_roll_rad);
    float y_unrolled = -x_sq * s + y_sq * c;

    // Sky gradient above horizon; atmospheric-haze gradient below horizon.
    // The "below" gradient fills the gap between the mesh edge and the
    // true geometric horizon (which at altitude is 100+ nm away — far
    // beyond the 20 nm terrain mesh).  Colored to blend naturally with
    // the terrain mesh so there's no visible seam.
    if (y_unrolled < u_horizon_y) {
        // Below horizon: haze-tinted ground (lighter at horizon, darker deep down)
        float t = clamp((u_horizon_y - y_unrolled) / max(0.001, u_horizon_y + 1.0),
                        0.0, 1.0);
        vec3 haze   = vec3(0.42, 0.33, 0.22);  // dusty atmospheric haze
        vec3 ground = vec3(0.27, 0.22, 0.11);  // darker distant ground
        frag_color = vec4(mix(haze, ground, t), 1.0);
    } else {
        float t = (y_unrolled - u_horizon_y) / max(0.001, 1.0 - u_horizon_y);
        vec3 horizon_col = vec3(0.23, 0.51, 0.78);
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

_fbo_size   = (0, 0)     # current FBO (w, h)
_mesh_key   = None       # cache key (lat_q, lon_q, alt_q) — mesh rebuild trigger
_mesh_radius_m = MESH_RADIUS_NM * NM_TO_M  # current mesh radius (for grid fade)


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
    if _terrain_prog is None:
        _terrain_prog = _ctx.program(vertex_shader=VERTEX_SHADER,
                                     fragment_shader=FRAGMENT_SHADER)
        _sky_prog = _ctx.program(vertex_shader=SKY_VERTEX_SHADER,
                                 fragment_shader=SKY_FRAGMENT_SHADER)

    # Sky quad: fullscreen triangle pair in NDC
    if _sky_vao is None:
        sky_verts = np.array([
            -1, -1,   1, -1,   1,  1,
            -1, -1,   1,  1,  -1,  1,
        ], dtype=np.float32)
        sky_vbo = _ctx.buffer(sky_verts.tobytes())
        _sky_vao = _ctx.vertex_array(_sky_prog, [(sky_vbo, '2f', 'in_pos')])

    return True


def _build_mesh(srtm_dir: str, lat: float, lon: float, alt_ft: float):
    """Sample SRTM around aircraft into a vertex+index buffer.

    Returns (positions [N×3 float32 metres], clearances [N float32 metres]).
    Aircraft is at origin (0,0,0); +X=East, +Y=North, +Z=Up; alt is mesh-relative.
    """
    global _mesh_key, _mesh_radius_m, _terrain_vao, _terrain_vbo_pos, _terrain_ibo

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
    _terrain_vao = _ctx.vertex_array(
        _terrain_prog,
        [(_terrain_vbo_pos, '3f', 'in_pos')],
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
):
    """Render the SVT terrain background using OpenGL.
    Returns a pygame.Surface (ai_w × ai_h, RGBA) or None if GL failed.
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
        _terrain_prog['u_grid_spacing_m'].value   = GRID_SPACING_NM * NM_TO_M
        _terrain_prog['u_grid_major_every'].value = float(GRID_MAJOR_EVERY)
        _terrain_prog['u_grid_max_dist_m'].value  = _mesh_radius_m
        # Sun direction vector (world frame: X=East, Y=North, Z=Up)
        az_rad = math.radians(SUN_AZIMUTH_DEG)
        el_rad = math.radians(SUN_ELEVATION_DEG)
        sun_x = math.cos(el_rad) * math.sin(az_rad)   # east component
        sun_y = math.cos(el_rad) * math.cos(az_rad)   # north component
        sun_z = math.sin(el_rad)                      # up component
        _terrain_prog['u_sun_dir'].value       = (sun_x, sun_y, sun_z)
        _terrain_prog['u_sun_intensity'].value = SUN_INTENSITY
        _terrain_prog['u_ambient'].value       = SUN_AMBIENT
        _terrain_vao.render()

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
                 "terrain_vao", "terrain_vbo_pos",
                 "terrain_ibo", "mesh_key", "mesh_radius_m",
                 "mesh_center_lat", "mesh_center_lon",
                 "line_prog", "line_vbo", "line_vao", "line_capacity")

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
        self.terrain_ibo = None
        self.mesh_key = None
        self.mesh_radius_m = MESH_RADIUS_NM * NM_TO_M
        self.mesh_center_lat = 0.0
        self.mesh_center_lon = 0.0
        # Polyline rendering — single reusable VBO, grown as needed.
        self.line_prog = ctx.program(vertex_shader=LINE_VERTEX_SHADER,
                                     fragment_shader=LINE_FRAGMENT_SHADER)
        self.line_vbo = ctx.buffer(reserve=4096)   # ~340 vec3 vertices
        self.line_vao = ctx.vertex_array(
            self.line_prog, [(self.line_vbo, '3f', 'in_pos')])
        self.line_capacity = 4096

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
        try:
            self.ctx.line_width = float(line_width)
        except Exception:
            pass
        self.line_vao.render(mode=moderngl.LINE_STRIP, vertices=len(world))

    def build_mesh(self, srtm_dir, lat, lon, alt_ft):
        """Same mesh-building logic as the module-level _build_mesh, but
        operating on this instance's ctx/buffers instead of module globals.

        Mesh is centered on a quantized world-grid point (not the moving
        aircraft) so terrain features stay in stable world positions and
        don't visibly jump every time we recenter. Aircraft offset from
        the mesh centre is applied at render time via the camera eye.
        """
        # World-aligned sampling: both the mesh centre AND the per-vertex
        # sample positions are snapped to multiples of sample_step_m from
        # a fixed world origin. Result: every sample is at the same world
        # location regardless of which mesh it's part of, so a recentre
        # only changes WHICH samples are visible — the geometry of any
        # given mountain is identical before and after.
        if MESH_SIZE_MODE == "altitude":
            r_nm = max(MESH_RADIUS_MIN_NM,
                       min(MESH_RADIUS_MAX_NM,
                           6.0 * math.sqrt(max(100.0, alt_ft) / 1000.0)))
        else:
            r_nm = MESH_RADIUS_NM
        radius_m = r_nm * NM_TO_M
        self.mesh_radius_m = radius_m
        alt_m = alt_ft * FT_TO_M

        # Sample step in metres, derived from MESH_GRID_N for backward-
        # compatible vertex count at the configured radius.
        sample_step_m = (2.0 * radius_m) / (MESH_GRID_N - 1)

        # Snap mesh_lat to a fixed lat grid (lat snap is metric — no cos
        # dependency).
        m_per_deg_lat = 60.0 * NM_TO_M
        snap_dlat = sample_step_m / m_per_deg_lat
        mesh_lat = round(lat / snap_dlat) * snap_dlat

        # Snap mesh_lon to a *cos-independent* grid: equator-equivalent
        # angular step, same as snap_dlat. If we instead derived snap_dlon
        # from cos(mesh_lat), every lat-cell crossing would shift the lon
        # snap lattice by tens of metres (cos changes ~2e-5 per snap step;
        # multiplied by the integer cell count from the prime meridian,
        # that's ~28 m of mesh_lon resnap per N/S recentre — visible as a
        # foreground morph because the same vertex index samples a slightly
        # different world point each rebuild).
        snap_dlon = snap_dlat
        mesh_lon = round(lon / snap_dlon) * snap_dlon

        # cos at the snapped lat is used for mesh-local metric projection
        # (vertex east position) and for SRTM lookup conversion. It still
        # changes per lat cell, but only affects vertex *positions* by a
        # tiny amount (~i * step * Δcos ≈ 1 m at the mesh edge, ~0 m at
        # the centre) — far smaller than the 28 m grid-snap shift it
        # replaces, and the foreground (i near 0) is rock-stable.
        cos_snap = max(1e-6, math.cos(math.radians(mesh_lat)))

        key = (round(mesh_lat, 6), round(mesh_lon, 6))
        if key == self.mesh_key and self.terrain_vao is not None:
            return

        # Sample grid: integer multiples of snap_dlat from mesh centre, in
        # both axes. North step is metric; east step is metric * cos(mesh_lat)
        # so that the angular spacing in longitude is snap_dlat at any
        # latitude. Result: every (lat, lon) sample point lies on a fixed
        # global angular grid — same world point always sampled the same way.
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

        elev_m = elev_ft * FT_TO_M

        # Z = absolute elevation. Aircraft alt is applied per-frame in the
        # vertex shader (u_alt_m uniform) so colour updates smoothly with
        # altitude instead of jumping at mesh-rebuild boundaries.
        positions = np.stack([east, north, elev_m], axis=-1).astype(np.float32)

        i, j = np.meshgrid(np.arange(n - 1), np.arange(n - 1), indexing='ij')
        v0 = (i     * n + j    ).astype(np.uint32)
        v1 = (i     * n + j + 1).astype(np.uint32)
        v2 = ((i+1) * n + j    ).astype(np.uint32)
        v3 = ((i+1) * n + j + 1).astype(np.uint32)
        tri1 = np.stack([v0, v2, v1], axis=-1).reshape(-1)
        tri2 = np.stack([v1, v2, v3], axis=-1).reshape(-1)
        indices = np.concatenate([tri1, tri2]).astype(np.uint32)

        if self.terrain_vbo_pos is not None:
            self.terrain_vao.release()
            self.terrain_vbo_pos.release()
            self.terrain_ibo.release()

        self.terrain_vbo_pos = self.ctx.buffer(positions.tobytes())
        self.terrain_ibo     = self.ctx.buffer(indices.tobytes())
        self.terrain_vao = self.ctx.vertex_array(
            self.terrain_prog,
            [(self.terrain_vbo_pos, '3f', 'in_pos')],
            index_buffer=self.terrain_ibo,
        )

        self.mesh_key = key
        self.mesh_center_lat = mesh_lat
        self.mesh_center_lon = mesh_lon


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

    st.build_mesh(srtm_dir, lat, lon, alt_ft)

    # Aircraft offset from mesh centre, in metres. Mesh sits on a quantized
    # world grid; aircraft slides through it smoothly.
    cos_mlat = max(1e-6, math.cos(math.radians(st.mesh_center_lat)))
    cam_north = (lat - st.mesh_center_lat) * 60.0 * NM_TO_M
    cam_east  = (lon - st.mesh_center_lon) * 60.0 * NM_TO_M * cos_mlat
    alt_m = alt_ft * FT_TO_M

    # Vertices store absolute elevation, so eye Z must be aircraft alt
    # (not 0). Camera-to-vertex relative geometry is unchanged.
    fwd, up = _attitude_basis(pitch_deg, roll_deg, hdg_deg)
    eye = np.array([cam_east, cam_north, alt_m], dtype=np.float32)
    target = eye + fwd
    view = _look_at(eye, target, up)
    proj = _perspective(v_fov_deg, ai_w / ai_h, NEAR_PLANE_M, FAR_PLANE_M)
    mvp = proj @ view

    ctx.enable(moderngl.DEPTH_TEST)
    # Explicit depth clear — without it, per-frame depth state from the 2D
    # composite pass would persist and cause terrain depth fighting.
    ctx.clear(0.04, 0.16, 0.31, 1.0, depth=1.0)

    horizon_y = _horizon_y_ndc(pitch_deg, v_fov_deg)
    st.sky_prog['u_horizon_y'].value = horizon_y
    st.sky_prog['u_roll_rad'].value  = math.radians(roll_deg)
    st.sky_prog['u_aspect'].value    = ai_w / ai_h
    ctx.disable(moderngl.DEPTH_TEST)
    st.sky_vao.render()
    ctx.enable(moderngl.DEPTH_TEST)

    if st.terrain_vao is not None:
        st.terrain_prog['u_mvp'].write(mvp.T.tobytes())
        st.terrain_prog['u_alt_m'].value            = alt_m
        st.terrain_prog['u_cos_mesh_lat'].value     = cos_mlat
        # World offset is in equator-equivalent metric (lon*NM*60, lat*NM*60),
        # NO cos factor — same projection used for the per-fragment
        # v_world_pos in the shader, so the same world point always lands
        # at the same v_world_pos value regardless of mesh_lat.
        grid_period_m = GRID_SPACING_NM * NM_TO_M * GRID_MAJOR_EVERY
        mesh_world_e_eq = st.mesh_center_lon * 60.0 * NM_TO_M
        mesh_world_n    = st.mesh_center_lat * 60.0 * NM_TO_M
        st.terrain_prog['u_world_offset'].value = (
            mesh_world_e_eq % grid_period_m,
            mesh_world_n    % grid_period_m,
        )
        st.terrain_prog['u_grid_spacing_m'].value   = GRID_SPACING_NM * NM_TO_M
        st.terrain_prog['u_grid_major_every'].value = float(GRID_MAJOR_EVERY)
        st.terrain_prog['u_grid_max_dist_m'].value  = st.mesh_radius_m
        az_rad = math.radians(SUN_AZIMUTH_DEG)
        el_rad = math.radians(SUN_ELEVATION_DEG)
        sun_x = math.cos(el_rad) * math.sin(az_rad)
        sun_y = math.cos(el_rad) * math.cos(az_rad)
        sun_z = math.sin(el_rad)
        st.terrain_prog['u_sun_dir'].value       = (sun_x, sun_y, sun_z)
        st.terrain_prog['u_sun_intensity'].value = SUN_INTENSITY
        st.terrain_prog['u_ambient'].value       = SUN_AMBIENT
        st.terrain_vao.render()

    # 3D polylines (e.g. magenta direct-to course trace) — drawn AFTER terrain
    # with depth test still on, so the depth buffer set by the terrain mesh
    # occludes line segments that fall behind ridges.
    if polylines:
        for verts, rgba, width in polylines:
            st.render_polyline_latlonelev(mvp, verts, rgba=rgba,
                                          line_width=width)

    ctx.disable(moderngl.DEPTH_TEST)
    return True
