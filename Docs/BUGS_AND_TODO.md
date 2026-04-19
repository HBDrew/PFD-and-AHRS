# Bugs & TODO

Tracked in git so nothing is lost across Claude Code sessions.
When opening a new session, start here for context.

Format: each item gets a short ID, a status, a one-line summary, and
notes with enough context to pick it up cold.

---

## Open

### #1  GL SVT — pygame.OPENGL shared-context composite (approach A)
Status: **IN PROGRESS — pfd.py wiring landed, hardware bring-up next**
Target: `pi4/svt_composite_gl.py` (new), `pi4/test_svt_composite.py` (new),
`pi4/svt_renderer_gl.py`, `pi4/pfd.py`, `pi4/config.py`.

Background: standalone EGL context creation breaks KMS/DRM on this Pi 4
(kernel 6.12 + mesa 25.0 V3D). Disabled in `cad8e40`. Chosen approach
is "A" (simpler) rather than "B" (full GL-native rewrite):
  - pygame owns the display via `pygame.OPENGL | pygame.DOUBLEBUF`
  - moderngl attaches via `create_context()` (NOT standalone — that's
    what broke before)
  - Terrain rendered by GL into the default framebuffer
  - 2D PFD elements drawn by the existing pygame code onto an
    offscreen SRCALPHA surface with the AI region left transparent
  - The 2D surface is uploaded as a GL texture each frame and
    composited as a fullscreen quad with alpha blending

Done so far:
  - `pi4/svt_composite_gl.py`: setup_gl_display(), Compositor class
    (upload_and_draw, release). Fullscreen-quad shader in GLES 3.0.
  - `pi4/test_svt_composite.py`: standalone smoke test. Animates a
    solid-colour clear (stand-in for terrain) and composites a fake
    PFD 2D layer on top. Runs 5 s then exits. Use `--windowed` for
    desktop dev.
  - `pi4/config.py`: `SVT_RENDERER = "opengl_shared"` option documented;
    default still `"opengl"`.
  - `pi4/svt_renderer_gl.py`: `render_svt_into_current_fb(ctx, ...)`
    entrypoint added. Reuses the existing shader/math but keeps its own
    per-ctx state (`_SharedState` cached by id(ctx)), no FBO allocation,
    no glReadPixels. Existing standalone `render_svt_gl()` unchanged.
  - `pi4/pfd.py`:
      - Imports `setup_gl_display`/`Compositor` and
        `render_svt_into_current_fb`. Module-level `_shared_gl_ctx` /
        `_shared_gl_compositor` are populated by main() when setup
        succeeds and consulted by render()/_flip() to take the GL path.
      - `main()` display init: when `SVT_RENDERER == "opengl_shared"`
        (and not in screenshot mode) calls `setup_gl_display()`,
        creates a `Compositor`, and allocates an SRCALPHA offscreen
        `surf`. Any exception falls back to the existing pygame path.
      - `_flip()`: in shared-GL mode uploads `surf` as a texture and
        composites it on top of the already-rendered terrain, then
        `pygame.display.flip()`. Rotated + SCALED paths unchanged.
      - `render()`: pre-clears the SRCALPHA surf to transparent each
        frame; clears GL default FB to black before any draws (so
        setup screens don't see stale terrain); in PFD mode, renders
        sky+terrain via `render_svt_into_current_fb` into the AI
        viewport (pygame rows 0..HDG_Y → GL rows HDG_H..DISPLAY_H),
        then resets viewport to full screen; skips the pygame
        `draw_ai_background`/`draw_simple_ai_background` call in
        shared-GL mode. Zero-pitch-line gate now also fires in
        shared-GL mode.

Remaining hardware-required steps:
  1. Run `python3 pi4/test_svt_composite.py --windowed` on the Pi 4.
     Verify: window opens, no blank screen, shader compiles, fake PFD
     layer composites cleanly with animated background, ~60 FPS, clean
     exit. Log `GL_RENDERER` / `GL_VERSION` from stdout.
  2. Set `SVT_RENDERER = "opengl_shared"` in `pi4/config.py` (or via
     `config_local.py`) and run `pfd.py`. Validate on hardware:
       - Terrain visible with tapes/drums/text overlaid
       - Horizon stays aligned with pitch ladder at all pitches
       - Roll rotation matches terrain rotation (symbols track)
       - No KMS/DRM conflict, no blank screen
       - FPS ≥ ~30 on Pi 4 ROADOM display
       - Setup screens fully opaque (no terrain bleeding through)

Notes / open questions for hardware bring-up:
  - SDL GL attributes in `setup_gl_display` request GLES 3.0 profile
    (PROFILE_MASK=4). Confirm V3D accepts this; if not, may need
    `pygame.GL_CONTEXT_PROFILE_COMPATIBILITY` or no profile hint.
  - `pygame.SCALED` is incompatible with `pygame.OPENGL`. For
    non-native logical resolutions we'd need a GL scaling pass
    (easy — just adjust the fullscreen-quad UVs).
  - `DISPLAY_ROTATE != 0` path needs GL rotation; TBD for Pi 4 ROADOM
    which is native-oriented.
  - Texture upload via `pygame.image.tostring` + `tex.write` is
    correct but copies through Python — can swap to `buffer_protocol`
    or PBO later if frame rate is a problem. Unlikely on 1024×600.

### #7  Demo smoothness — sinusoidal interpolation
Status: **OPEN**
Target: `DemoState` in pi4/pi_zero.
Context: demo state changes are linear; should ease in/out for more
realistic motion.

### #8  Range rings — distance circles on terrain
Status: **OPEN**
Target: symbol overlay in pi4/pi_zero.
Context: draw 1 nm / 2 nm / 5 nm distance rings on the SVT so pilot
has spatial reference for nearby airports/obstacles.

### #9  Pico W firmware — debug AP not appearing
Status: **OPEN**
Target: `firmware/main.py`, `firmware/config.py`.
Context: when `AP_SSID = "AHRS-Link-DEBUG"` or similar diagnostic
values, the AP doesn't come up. Works with default SSID. Possible
channel/password-length edge case.

### PI_DISPLAY  pi_display/pfd.py missing all recent fixes
Status: **OPEN / DEFERRED** (unclear if pi_display is an active target)
Context: `pi_display/pfd.py` still has `int(abs(value))` in drum
(line 406) and the old `-(vert_deg + pitch_deg)` airport projection
bug (line 3546). Port recent pi4/pi_zero fixes if pi_display is
still used, otherwise delete the directory.

---

## Completed

### #2  Speed drum leading 1 at 100 — **FIXED**
Commits: `6eab95f` (round vs int), plus this session
(`show_adjacent=True` + `adj_slot_h` on inner drum so the "1" above
the "0" is visible). pi4 and pi_zero both.

### #3  Heading bug at 360° / 000° — **FIXED**
Commits: `3d20b1e` (falsy-zero in sim flight model),
`6eab95f` (chevron rendering `!= 0` check removed).

### AIRPORTS-BANK  Airports slide across sky during banked turns — **FIXED**
Commit: `74844fd` introduced paint-in-terrain-frame-then-rotate
overlay. `f0e15a6` gated it behind GL SVT which is disabled on Pi 4
hardware, so the fix never executed. This session removes the gate —
overlay path runs on both GL and pygame render paths. pi_zero also
gets the overlay (was previously using the old independent-roll
projection).

### SPD-DRUM-SPACING  Airspeed drum gap between tens and ones digit — **FIXED**
Pi4 had `_drm_sw = int(26 * _fs)` (ones cell ~30 px) vs inner cell
~20 px. This session reduced to `_drm_sw = int(18 * _fs)` so the
ones drum matches the inner cell width. pi_zero already had matched
widths (17 vs 15).

### ONES-ROLL-ASYM  Ones drum "1 above 0" invisible approaching from below — **FIXED**
Root cause was NOT IIR smoothing (as initially assumed). It was
that `_rolling_drum`'s show_adjacent branch only rendered one digit
above (`d_hi`), while `_rolling_drum_alt20` used by the altimeter
also rendered a *second* digit two slots above (`d_hi2`). At
speed=99.8 the math gives `d_lo=9, d_hi=0, d_prev=8` — the "1"
simply wasn't computed or drawn. The altimeter equivalent at 9998 ft
gives `d_lo_idx=4("80"), d_hi=0("00"), d_hi2=1("20")` — the "20"
peeks above correctly.
Fix: added `d_hi2 = (d_lo + 2) % 10` and its blit at
`ty_lo - 2 * slot_h` in `_rolling_drum` show_adjacent branch.
Applied to pi4 and pi_zero.

### #5  Keyboard fixes (colon, period, backspace, pre-populated values) — **FIXED**
Three sub-issues diagnosed and fixed in pi4/pi_zero pfd.py:

1. **Colon and period keys missing**: `:` and `.` were not in `_KB_ROWS`
   at all. Added to row 3 (replacing the position of `-`, which moved to
   row 4). Row 3 now: `Z X C V B N M . : ⌫`, backspace width reduced
   from 88 to 60 px so 10 keys at 60 + 9 gaps fit pi_zero's 640 px
   display (636 px total). Row 4 now: `CANCEL - SPACE DONE`.
2. **Backspace on numpad missing**: `_NP_KEYS` had no `⌫` key and the
   event handler had no `del` branch. Added `⌫` as a fourth key on the
   bottom row (CANCEL, 0, ⌫, ENTER), each 87 px wide so the row still
   totals 384 px matching the digit rows. `_NP_KEYS` now supports an
   optional 3rd tuple element for per-key width; new `_np_row_layout()`
   centers rows by width. Handler gains `elif sty == 'del'` branch
   that does `numpad_buf = numpad_buf[:-1]`.
3. **Modal not pre-populated**: all four modal open-sites (numpad for
   bug targets, numpad for V-speed fields, keyboard for connectivity,
   keyboard for flight profile) set `numpad_buf = ""` / `kbd_buf = ""`.
   Added `_current_str_for_numpad(target)` and `_current_str_for_kbd
   (target, prev_mode)` helpers that return the string form of the
   current value (with inHg↔hPa conversion for baro_hpa, /100 for
   alt_bug). All open-sites now pre-populate the buffer. Latent bug
   also fixed: flight-profile keyboard path was not setting kbd_prev,
   which could leave it stale from a prior connectivity edit and cause
   text to go into the wrong dict on DONE.

### #6  WiFi SSID — show actual connected network — **FIXED**
Root cause: `_wifi_ssid_current()` already ran `iwgetid -r` and
returned the actual SSID, but `_poll_wifi_status()` discarded it —
only stored `wifi_ok = bool(...)`. The Connectivity "STATUS" row
showed a generic "WiFi CONNECTED" badge, never the network name.
Fix: `_poll_wifi_status` now also stashes the SSID into
`disp["cs"]["wifi_actual"]`; the status row renders
`"WiFi: <ssid>"` (truncated with ellipsis at 18 chars) when up,
falling back to "WiFi NO LINK" when down. Applied to pi4 and pi_zero.

### #4  USB AHRS — end-to-end working — **FIXED**
Full path Pico W → USB CDC → Pi 4 → PFD display verified live on
hardware. Commits across the work:
  - `0044215` — transport: firmware emits `$AHRS,{json}`, shared
    SerialClient reads /dev/ttyACM0, pi4 tries USB before SSE.
  - `e4a4c42` — Connectivity STATUS row diagnostics: transport,
    port, RX/ERR counters, last error.
  - `4e3a99c` — live R/P/Y/ALT readout on STATUS row.
  - `f80059b` — pause live AHRS while sim runs so sim writes win.
  - `63c2852` — wt901 driver: avoid `del bytearray` (MicroPython
    doesn't implement it). Crash surfaced only once the sensor
    was wired correctly and bytes actually started arriving.
Wiring note: WT901 pin 4 (TX) → Pico pin 2 (GP1 / UART0 RX).
WT901 pin 3 is RX; connecting pin 3 to Pico RX gives two listeners
and no talker. Easy mistake.
pi_zero does NOT have USB fallback yet — pending if needed.

### ALT-10K  Altitude drum shows "0000" at 10000 ft — **FIXED**
Root cause: IIR smoothing on `disp["alt"]` converges from below —
at indicated 10000 ft the actual smoothed value is e.g. 9999.998.
Branch selection used `int(alt_inner)` which returned 99 for any
`alt_inner < 100.0`, picking the 2-drum elif path that never draws
the leading "1" column. Only crossing integer 10000 (actually
10100 observed) promoted to the 3-drum else path.
Fix: changed branch selection to `round(alt_inner)` so the 3-drum
path activates at `alt_inner ≥ 99.5`, matching how `val_int` is
already computed inside `_rolling_drum`. Applied to pi4 and pi_zero.

---

## Conventions

- When a bug is fixed, move the entry to **Completed** with commit SHAs.
- Don't delete entries — keeps the history visible for cold-start
  sessions.
- When a new bug comes up, add it to **Open** with enough context that
  a new session can pick it up without back-reading the chat.
