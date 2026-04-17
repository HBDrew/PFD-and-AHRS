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
#version 330 core

in vec3 in_pos;          // world position (East, North, Up) in metres, relative to aircraft
in float in_clearance;   // aircraft_alt_m - terrain_alt_m at this vertex (metres)

uniform mat4 u_mvp;

out float v_clearance_ft;   // pass clearance in FEET to fragment
out vec3 v_world_pos;       // world position for grid overlay
out float v_dist_m;         // horizontal distance from aircraft (for fade)

void main() {
    gl_Position = u_mvp * vec4(in_pos, 1.0);
    v_clearance_ft = in_clearance * 3.28084;  // m → ft
    v_world_pos = in_pos;
    v_dist_m = length(in_pos.xy);
}
"""

FRAGMENT_SHADER = """
#version 330 core

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
#version 330 core

in vec2 in_pos;          // fullscreen quad in NDC
out vec2 v_ndc;

void main() {
    gl_Position = vec4(in_pos, 0.999, 1.0);   // far plane — drawn behind terrain
    v_ndc = in_pos;                            // pass through raw NDC (-1..1)
}
"""

SKY_FRAGMENT_SHADER = """
#version 330 core

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
_terrain_vbo_clr = None  # terrain vertex clearances VBO
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
        try:
            _ctx = moderngl.create_standalone_context(backend='egl')
        except Exception as e:
            print(f"[SVT-GL] EGL context creation failed: {e}")
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
    global _mesh_key, _mesh_radius_m, _terrain_vao, _terrain_vbo_pos, _terrain_vbo_clr, _terrain_ibo

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
    positions = np.stack([east, north, elev_m - alt_m], axis=-1).astype(np.float32)
    clearances = (alt_m - elev_m).astype(np.float32)

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
        _terrain_vbo_clr.release()
        _terrain_ibo.release()

    _terrain_vbo_pos = _ctx.buffer(positions.tobytes())
    _terrain_vbo_clr = _ctx.buffer(clearances.tobytes())
    _terrain_ibo     = _ctx.buffer(indices.tobytes())
    _terrain_vao = _ctx.vertex_array(
        _terrain_prog,
        [(_terrain_vbo_pos, '3f', 'in_pos'),
         (_terrain_vbo_clr, '1f', 'in_clearance')],
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

    # Camera
    fwd, up = _attitude_basis(pitch_deg, roll_deg, hdg_deg)
    eye = np.array([0.0, 0.0, 0.0], dtype=np.float32)
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
    """Return True if the GL backend can be initialised."""
    if not (HAS_MODERNGL and HAS_NUMPY):
        return False
    try:
        return _init_gl(64, 64)
    except Exception:
        return False
