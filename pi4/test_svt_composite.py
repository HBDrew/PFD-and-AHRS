"""
test_svt_composite.py – minimal smoke test for svt_composite_gl.

Stands up a pygame.OPENGL window, clears it to a solid color (stands in
for the GL terrain pass), draws a fake PFD 2D layer onto a transparent
pygame.Surface, and composites the two with alpha blending. Runs for
5 seconds, then exits.

What this verifies:
  - pygame.OPENGL display mode opens without stealing KMS/DRM
  - moderngl.create_context() attaches to pygame's shared GL context
  - Fullscreen-quad shader compiles on Pi 4 V3D (GLES 3.0)
  - Texture upload from pygame.Surface works
  - Alpha blending looks right (transparent AI region, opaque tapes)

What this does NOT verify:
  - Actual terrain rendering (no mesh, no shader, no SRTM)
  - Interaction with the full pfd.py render loop
  - Rotated display path (DISPLAY_ROTATE != 0)

Run on any Linux box with GL drivers:
    python3 pi4/test_svt_composite.py
Or windowed on the Pi 4 (avoid fullscreen if running over X/wayland):
    python3 pi4/test_svt_composite.py --windowed
"""

import os
import sys
import time
import argparse

import pygame

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from svt_composite_gl import setup_gl_display, Compositor  # noqa: E402


W, H = 1024, 600


def build_fake_pfd_layer(width: int, height: int) -> pygame.Surface:
    """Draw a recognisable 2D PFD stand-in: two side tapes + readouts,
    middle region left transparent so the terrain colour shows through."""
    layer = pygame.Surface((width, height), pygame.SRCALPHA)
    layer.fill((0, 0, 0, 0))

    # Left tape (airspeed)
    tape_w = 80
    pygame.draw.rect(layer, (0, 10, 30, 220), (0, 0, tape_w, height))
    # Right tape (altitude)
    pygame.draw.rect(layer, (0, 10, 30, 220), (width - tape_w, 0, tape_w, height))
    # Top strip (heading bugs)
    pygame.draw.rect(layer, (0, 10, 30, 220), (0, 0, width, 40))
    # Bottom strip (heading tape)
    pygame.draw.rect(layer, (0, 10, 30, 220), (0, height - 60, width, 60))

    # Some white lines to stand in for tick marks
    f = pygame.font.Font(None, 32)
    for y in range(50, height - 80, 50):
        pygame.draw.line(layer, (255, 255, 255), (tape_w - 10, y), (tape_w, y), 2)
        pygame.draw.line(layer, (255, 255, 255),
                         (width - tape_w, y), (width - tape_w + 10, y), 2)
        t = f.render(f"{y}", True, (255, 255, 255))
        layer.blit(t, (8, y - 14))
        layer.blit(t, (width - tape_w + 16, y - 14))

    # Centre aircraft symbol
    cx, cy = width // 2, height // 2
    pygame.draw.line(layer, (255, 255, 0), (cx - 30, cy), (cx - 10, cy), 4)
    pygame.draw.line(layer, (255, 255, 0), (cx + 10, cy), (cx + 30, cy), 4)
    pygame.draw.circle(layer, (255, 255, 0), (cx, cy), 3)

    # Label so you know the 2D layer rendered
    banner = pygame.font.Font(None, 36).render(
        "SVT COMPOSITE TEST  (2D layer)", True, (0, 255, 0))
    layer.blit(banner, (100, 50))

    return layer


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--windowed", action="store_true",
                    help="Run in a window (for desktop dev); default is fullscreen.")
    ap.add_argument("--seconds", type=float, default=5.0,
                    help="How long to run before auto-exiting.")
    ap.add_argument("--rotate", type=int, default=0, choices=(0, 90, 180, 270),
                    help="Rotate the composite output N degrees to match a "
                         "physically-rotated display. pygame.OPENGL bypasses "
                         "KMS rotation so we have to apply it at the GL layer.")
    args = ap.parse_args()

    os.environ.setdefault("SDL_RENDER_VSYNC", "0")

    pygame.init()
    pygame.mouse.set_visible(False)

    try:
        screen, ctx = setup_gl_display(W, H, fullscreen=not args.windowed)
    except Exception as e:
        print(f"[TEST] GL display setup failed: {e}")
        pygame.quit()
        sys.exit(1)

    compositor = Compositor(ctx, W, H, rotate_deg=args.rotate)
    pfd_layer = build_fake_pfd_layer(W, H)

    import moderngl

    clock = pygame.time.Clock()
    t0 = time.monotonic()
    frame = 0
    running = True

    while running and (time.monotonic() - t0) < args.seconds:
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                running = False
            elif ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE:
                running = False

        # "Terrain" pass: animate the clear color so you can see the
        # background is GL-rendered (and not just a static pygame fill).
        # Sky-ish colour that shifts through blues/greens.
        t = (time.monotonic() - t0) / args.seconds
        r = 0.10 + 0.10 * t
        g = 0.40 + 0.20 * t
        b = 0.70 - 0.20 * t
        ctx.clear(r, g, b, 1.0)

        # 2D PFD overlay
        compositor.upload_and_draw(pfd_layer)

        pygame.display.flip()
        clock.tick(60)
        frame += 1

    fps = frame / max(1e-3, time.monotonic() - t0)
    print(f"[TEST] {frame} frames in {args.seconds:.1f}s  ({fps:.1f} FPS)")

    compositor.release()
    pygame.quit()


if __name__ == "__main__":
    main()
