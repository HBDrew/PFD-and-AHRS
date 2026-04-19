#!/usr/bin/env python3
"""
render_iphone_previews.py – Generate PNG screenshots of the iPhone PFD
for the user manual.

Uses Playwright + a local HTTP server so the browser can load
terrain.js and scripts via http:// (EventSource won't open over
file://).  Renders both `preview.html` (self-simulating PFD) for
flight scenes, and `index.html` with a mocked state for the panel
overlays (terrain / baro / trim).

Output goes to iphone_display/previews/*.png.

Requires:
    pip install playwright
    playwright install chromium

Run from repo root:
    python3 tools/render_iphone_previews.py
"""
import os
import sys
import time
import threading
import http.server
import socketserver
import contextlib

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_DISPLAY_DIR = os.path.join(_ROOT, "iphone_display")
_OUT_DIR = os.path.join(_DISPLAY_DIR, "previews")

# iPhone 14 physical pixels in LANDSCAPE orientation (the natural attitude for
# a cockpit-mounted phone PFD).  The render code reads canvas dims dynamically
# so all instruments scale correctly to this viewport.
VIEWPORT_W = 844
VIEWPORT_H = 390
DEVICE_SCALE = 3

# Playwright browser path (fallback for environments where chromium is
# pre-installed under /opt/pw-browsers).
_CHROME = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"


import json

# Mutable shared state — each PANEL_SHOTS entry overwrites this before
# navigating, and the fake /events handler below streams it to index.html.
_FAKE_STATE = {
    "roll": 2, "pitch": 1.5, "yaw": 133, "lat": 34.87, "lon": -111.76,
    "alt": 8500, "speed": 115, "track": 133, "vspeed": 0, "fix": 3, "sats": 9,
    "ay": 0.02, "baro_hpa": 1013.25, "baro_src": "gps", "gps_alt": 8500,
    "pitch_trim": 0.0, "roll_trim": 0.0, "yaw_trim": 0.0,
    "ahrs_ok": True, "gps_ok": True, "baro_ok": False,
}


class _Handler(http.server.SimpleHTTPRequestHandler):
    """Serves iphone_display/ and mocks /events and /baro so index.html's
    live paths render without needing a real Pico W.  /events streams the
    current _FAKE_STATE every 200 ms; /baro just acks."""

    def log_message(self, *a, **kw):   # suppress noisy GET log
        pass

    def do_GET(self):
        if self.path.startswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                # Stream ~10 frames then hold — gives the page enough to
                # paint a stable frame before playwright screenshots.
                for _ in range(12):
                    payload = json.dumps(_FAKE_STATE)
                    self.wfile.write(f"data: {payload}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(0.05)
                # Keep the connection open until client disconnects
                while True:
                    time.sleep(0.5)
            except Exception:
                pass
            return
        if self.path.startswith("/baro"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")
            return
        return super().do_GET()


@contextlib.contextmanager
def _serve(directory, port=0):
    """Serve `directory` over a localhost HTTP thread.  Yields the port."""
    # Threading server so /events (long-lived) doesn't block other requests.
    class _ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
        daemon_threads = True
        allow_reuse_address = True

    handler = lambda *a, **kw: _Handler(*a, directory=directory, **kw)
    with _ThreadedTCPServer(("127.0.0.1", port), handler) as httpd:
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            yield httpd.server_address[1]
        finally:
            httpd.shutdown()


# Flight scenes rendered from preview.html.  Each tuple is
# (filename, scenario_index, progress_in_scenario).  The scenario list in
# preview.html maps to these: 0=level, 1=right turn, 2=climb left, 3=bank 45,
# 4=descent, 5=turbulence.
SCENES = [
    # (filename, scenario_idx, t01, taws_level)
    ("preview_level_cruise.png",     0, 0.50, 0),
    ("preview_right_turn.png",       1, 0.40, 0),
    ("preview_climb_left.png",       2, 0.60, 0),
    ("preview_steep_bank_45.png",    3, 0.35, 0),
    ("preview_descent_valley.png",   4, 0.70, 0),
    ("preview_turbulence_slip.png",  5, 0.45, 0),
    ("preview_taws_caution.png",     4, 0.60, 1),   # descent scenario + amber
    ("preview_taws_pullup.png",      4, 0.80, 2),   # descent scenario + red
]


def _freeze_scene(page, scenario_idx, t01):
    """Freeze preview.html on a specific scenario frame via its exposed hook."""
    page.evaluate(
        "([idx, t01]) => window._freezeScene(idx, t01)",
        [scenario_idx, t01],
    )


def _render_preview(page, port, fname, scenario_idx, t01, taws_level=0):
    page.goto(f"http://127.0.0.1:{port}/preview.html")
    page.wait_for_load_state("networkidle")
    # Let the page run a few frames so terrain.js finishes initialising.
    page.wait_for_timeout(800)
    _freeze_scene(page, scenario_idx, t01)
    if taws_level:
        page.evaluate(f"window._forceTawsLevel({taws_level})")
    page.wait_for_timeout(300)
    out = os.path.join(_OUT_DIR, fname)
    page.screenshot(path=out, full_page=False)
    print(f"  → {fname}")


# Live PFD + panel shots rendered from index.html with a mocked state.
PANEL_SHOTS = [
    # (filename, panel_id_to_show, state_override, show_link_dead)
    ("preview_live_link_ok.png",    None,             {"baro_ok": True}, False),
    ("preview_no_link.png",         None,             {}, True),
    ("preview_panel_baro_hpa.png",  "baro-panel",     {"baro_ok": True}, False),
    ("preview_panel_baro_inhg.png", "baro-panel",     {"_qnh_unit": "inHg", "baro_ok": True}, False),
    ("preview_panel_terrain.png",   "terrain-panel",  {}, False),
    ("preview_panel_trim.png",      "trim-panel",     {"pitch_trim": 0.5, "roll_trim": -0.5}, False),
]


def _render_panel(page, port, fname, panel_id, state_override, show_link_dead=False):
    """Render index.html with a mocked SSE state.  The shared `_FAKE_STATE`
    dict is updated in-place so the /events handler picks up the override
    before the page connects.  `show_link_dead=True` blocks /events to drive
    the NO-LINK state."""
    global _FAKE_STATE
    merged = dict(_FAKE_STATE)
    merged.update({k: v for k, v in state_override.items()
                   if not k.startswith("_")})
    _FAKE_STATE.clear()
    _FAKE_STATE.update(merged)

    if show_link_dead:
        # Abort /events so the page never connects → NO LINK state.
        page.route("**/events", lambda route: route.abort())
    else:
        page.unroute("**/events")

    page.goto(f"http://127.0.0.1:{port}/index.html")
    page.wait_for_load_state("domcontentloaded")
    # Let the SSE frames arrive and the render loop settle.
    page.wait_for_timeout(1200)

    if state_override.get("_qnh_unit") == "inHg":
        page.evaluate("toggleQNHUnit();")

    if panel_id:
        page.evaluate(
            f"document.getElementById('{panel_id}').style.display = 'block';"
        )
    page.wait_for_timeout(300)
    out = os.path.join(_OUT_DIR, fname)
    page.screenshot(path=out, full_page=False)
    print(f"  → {fname}")


def main():
    from playwright.sync_api import sync_playwright

    os.makedirs(_OUT_DIR, exist_ok=True)
    print(f"Rendering iPhone PFD previews to {_OUT_DIR}")

    with _serve(_DISPLAY_DIR) as port:
        print(f"  local server on http://127.0.0.1:{port}")
        launch_kwargs = {"headless": True, "args": ["--headless=new"]}
        if os.path.exists(_CHROME):
            launch_kwargs["executable_path"] = _CHROME

        with sync_playwright() as p:
            browser = p.chromium.launch(**launch_kwargs)
            ctx = browser.new_context(
                viewport={"width": VIEWPORT_W, "height": VIEWPORT_H},
                device_scale_factor=DEVICE_SCALE,
                is_mobile=True,
                has_touch=True,
                user_agent=(
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                    "Mobile/15E148 Safari/604.1"
                ),
            )
            page = ctx.new_page()

            print("\nFlight scenes (preview.html)")
            for fname, idx, t01, taws_level in SCENES:
                _render_preview(page, port, fname, idx, t01, taws_level)

            print("\nLive view + overlays (index.html)")
            for fname, panel_id, overrides, link_dead in PANEL_SHOTS:
                _render_panel(page, port, fname, panel_id, overrides,
                              show_link_dead=link_dead)

            browser.close()

    print("\nDone.")


if __name__ == "__main__":
    main()
