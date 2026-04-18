"""
svt_composite_gl.py – pygame.OPENGL + moderngl shared-context compositor.

This module implements "approach A" for the GL SVT integration:

  1. pygame owns the display via pygame.OPENGL | pygame.DOUBLEBUF.
     SDL2/KMS is happy because pygame grabs the GL context first.
  2. moderngl attaches to pygame's GL context via create_context()
     (NOT create_standalone_context — that's what broke before by
     stealing the GPU device from KMS/DRM).
  3. Each frame:
       a. GL terrain is rendered directly to the default framebuffer
          (owned by pygame's GL surface).
       b. The 2D PFD layer (tapes, drums, text, buttons) is drawn by
          the existing pygame code to an offscreen SRCALPHA Surface
          with the AI region left transparent.
       c. That Surface is uploaded as a GL texture and drawn as a
          fullscreen quad on top of the terrain with alpha blending.
       d. pygame.display.flip() presents the composed result.

The pygame 2D drawing code is untouched — it just writes to an
offscreen Surface instead of the display. Performance cost vs. the
pure-pygame path is one texture upload per frame (~1024×600×4 =
2.4 MB) plus one quad draw — well inside Pi 4 V3D budget.

Public API:
    setup_gl_display(w, h, fullscreen) → (screen, ctx)
    Compositor(ctx, w, h) — holds quad shader + reusable texture
      .upload_and_draw(pygame_surface)
      .release()

Failure mode: any exception during GL init should propagate. Callers
in pfd.py are responsible for catching and falling back to the
pygame-only SVT_RENDERER path.
"""

import os

import pygame

try:
    import moderngl
    HAS_MODERNGL = True
except ImportError:
    HAS_MODERNGL = False

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False


# ── GLSL: fullscreen-quad overlay shader ─────────────────────────────────────
# Renders a 2D texture covering the entire viewport with straight-alpha
# blending. The quad's vertices are in NDC (-1..1) so no MVP matrix needed.
# We flip V on the texture lookup because pygame's surface origin is top-left
# but OpenGL's texture origin is bottom-left.

_VERTEX_SHADER = """
#version 300 es
precision highp float;

in vec2 in_pos;   // NDC position (-1..1)
in vec2 in_uv;    // texture coord (0..1), already V-flipped by caller
out vec2 v_uv;

void main() {
    v_uv = in_uv;
    gl_Position = vec4(in_pos, 0.0, 1.0);
}
"""

_FRAGMENT_SHADER = """
#version 300 es
precision highp float;

in vec2 v_uv;
uniform sampler2D u_tex;
out vec4 out_color;

void main() {
    vec4 c = texture(u_tex, v_uv);
    // pygame surfaces are BGRA when saved as RGBA bytes on little-endian,
    // but pygame.image.tostring handles the byte order, so c is already RGBA.
    out_color = c;
}
"""


def setup_gl_display(width: int, height: int, fullscreen: bool = True):
    """Create a pygame display with an attached moderngl context.

    Must be called AFTER pygame.init() but BEFORE any other GL work.
    Returns (screen_surface, moderngl_context).

    Raises on failure (moderngl missing, GL context creation failure,
    driver incompatibility, etc.). Callers are expected to catch and
    fall back to the pygame-only path.
    """
    if not HAS_MODERNGL:
        raise RuntimeError("moderngl not installed — cannot use opengl_shared")
    if not HAS_NUMPY:
        raise RuntimeError("numpy not installed — cannot use opengl_shared")

    flags = pygame.OPENGL | pygame.DOUBLEBUF
    if fullscreen:
        flags |= pygame.FULLSCREEN | pygame.NOFRAME

    # GLES 3.0 matches svt_renderer_gl shaders (#version 300 es).
    # SDL_GL_CONTEXT_PROFILE_MASK=4 selects GL ES profile.
    pygame.display.gl_set_attribute(pygame.GL_CONTEXT_MAJOR_VERSION, 3)
    pygame.display.gl_set_attribute(pygame.GL_CONTEXT_MINOR_VERSION, 0)
    pygame.display.gl_set_attribute(pygame.GL_CONTEXT_PROFILE_MASK, 4)  # ES
    pygame.display.gl_set_attribute(pygame.GL_DOUBLEBUFFER, 1)

    screen = pygame.display.set_mode((width, height), flags)

    ctx = moderngl.create_context(require=300)
    renderer = ctx.info.get("GL_RENDERER", "?")
    version  = ctx.info.get("GL_VERSION",  "?")
    print(f"[GL-Composite] shared GL context OK: {renderer} / {version}")
    return screen, ctx


class Compositor:
    """Uploads a pygame.Surface as a GL texture and draws it as a
    fullscreen quad on top of whatever's already in the framebuffer.

    Reuses the texture object across frames (resized only if the
    surface size changes) to avoid per-frame allocation.
    """

    def __init__(self, ctx, width: int, height: int):
        self.ctx = ctx
        self.width = width
        self.height = height

        self.prog = ctx.program(
            vertex_shader=_VERTEX_SHADER,
            fragment_shader=_FRAGMENT_SHADER,
        )

        # Fullscreen quad as two triangles — NDC positions + UVs.
        # UV.y is flipped (0 at bottom, 1 at top) to match pygame's
        # top-left origin after we upload the surface as-is.
        verts = np.array([
            # x,   y,    u, v
            -1.0, -1.0,  0.0, 1.0,   # bottom-left
             1.0, -1.0,  1.0, 1.0,   # bottom-right
            -1.0,  1.0,  0.0, 0.0,   # top-left
             1.0,  1.0,  1.0, 0.0,   # top-right
        ], dtype=np.float32)
        self.vbo = ctx.buffer(verts.tobytes())
        self.vao = ctx.vertex_array(
            self.prog,
            [(self.vbo, "2f 2f", "in_pos", "in_uv")],
        )

        self.tex = ctx.texture((width, height), 4)
        self.tex.filter = (moderngl.NEAREST, moderngl.NEAREST)
        self.tex.repeat_x = False
        self.tex.repeat_y = False

    def upload_and_draw(self, pfd_surface: pygame.Surface):
        """Upload the given pygame Surface to our GL texture and draw
        it as a fullscreen quad with alpha blending.

        The surface must be width×height and have an alpha channel
        (pygame.SRCALPHA). Transparent regions let the terrain show
        through; opaque regions replace it.
        """
        if pfd_surface.get_size() != (self.width, self.height):
            raise ValueError(
                f"Compositor size ({self.width}×{self.height}) does not match "
                f"surface size {pfd_surface.get_size()}"
            )

        raw = pygame.image.tostring(pfd_surface, "RGBA", True)
        self.tex.write(raw)
        self.tex.use(location=0)
        self.prog["u_tex"].value = 0

        self.ctx.enable(moderngl.BLEND)
        self.ctx.blend_func = (moderngl.SRC_ALPHA, moderngl.ONE_MINUS_SRC_ALPHA)
        self.vao.render(mode=moderngl.TRIANGLE_STRIP)
        self.ctx.disable(moderngl.BLEND)

    def release(self):
        self.tex.release()
        self.vao.release()
        self.vbo.release()
        self.prog.release()
