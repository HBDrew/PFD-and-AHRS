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

### #12  iPhone compass calibration — cardinal-point or GPS-track
Status: **OPEN**
Target: `iphone_display/index.html` sensor pipeline (`_onOrientation`,
`PS` state), and possibly a new calibration panel in the setup menu.
Context: iPhone's `webkitCompassHeading` is most accurate in portrait
and drifts in landscape — especially landscape-right (charging port on
right), which is the preferred mount orientation. Need a user-driven
calibration to compute a per-orientation offset that's stored locally
and added to the raw heading before display.
Modes:
  1. **Cardinal walk-through**: pilot taps a "CAL COMPASS" button in
     setup, then points the aircraft at known N / E / S / W headings
     and confirms each. Average the four offsets (handles device tilt
     bias). Store to `localStorage`.
  2. **GPS-track auto-cal**: when GPS groundspeed > 15 kt for ≥10 s
     and AHRS compass is live, compute `offset = gps_track - compass`
     (unwrapped, averaged over the sample window). Roll into the
     stored offset with a low-pass filter so wind/crab angle doesn't
     bias it too hard; require straight-and-level (|roll| < 5°) to
     include a sample.
Apply the stored offset in `_onOrientation` before writing `PS.yaw`.
Show a "CAL" indicator on the heading box when an offset is active.
Needs paired firmware work (see AHRS-MAGCAL below).

### AHRS-MAGCAL  WT901 magnetometer calibration procedure
Status: **OPEN**
Target: `firmware/wt901.py`, `firmware/main.py`, `firmware/web_server.py`.
Context: The WT901 has factory mag calibration but drifts with nearby
ferrous metal (panel, wiring, headset). For the AHRS to supply a
trustworthy yaw that the iPhone/Pi4 displays can trust, we need a
user-runnable calibration routine. Also needed so the iPhone #12
cardinal calibration has something authoritative to match against.
Work items:
  - Add a `/magcal/start` / `/magcal/sample?hdg=XXX` / `/magcal/finish`
    HTTP endpoint set (or serial command equivalent) on the Pico W so
    a display can drive the procedure without a special tool.
  - At each of N/E/S/W, read mag X/Y for ~2 s and average; solve for
    hard-iron offset (center of the ellipse) and soft-iron scale
    (ellipse-to-circle transform). See any WT901 hard/soft-iron cal
    reference for the math (2D form is sufficient — we only use yaw).
  - Persist the resulting 2x2 matrix + offset to flash. Apply in
    `wt901.py` before computing yaw.
  - Surface status on the `/status` JSON so the Connectivity panel
    on both display platforms can show "MAG CAL: OK / STALE / NONE".

---

### #14  iPhone baro button shouldn't exist when in GPS-ALT
Status: **OPEN**
Target: `iphone_display/index.html` `drawBaroButton`, `_handleSpdTap`.
Context: When the baro sensor is unavailable (`baro_ok === false`)
or the firmware is reporting GPS-derived altitude (`baro_src ===
"gps"`), the displayed altitude has no QNH input — adjusting QNH does
nothing useful. Today the baro button still draws (magenta) showing
"GPS ALT" and is tappable; it should be omitted entirely so the pilot
isn't invited to tweak a setting that has no effect.
Work items:
  - Skip the rounded-rect draw and tap registration when
    `!D.baro_ok || D.baro_src === "gps"`. Either return early from
    `drawBaroButton` and the tap branch in `_handleSpdTap`, or gate
    on a single `_baroAdjustable()` helper.
  - Decide what (if anything) fills the bottom of the alt-tape
    column when the button is hidden — probably nothing (let the
    heading tape show through), matching pi4 behaviour.

### #17  iPhone airport overlay — symbols + labels + download screen
Status: **OPEN**
Target: `iphone_display/` — new `airports.js` module, additions to
`index.html` for the AI overlay, and a new download/manage panel
plumbed into the setup menu next to TERRAIN.
Context: Pi4 has full airport support — `airports.py` parses the
OurAirports CSV into a numpy cache, `draw_airport_symbols()`
renders them as projected symbols on the AI with S/M/L filter,
`draw_airport_data()` is the dedicated data screen with download
controls, and the status badges include NO APT / EXP APT. iPhone
has none of this. The data set is ~3 MB (from `fetch_airports.sh`,
~80K airports worldwide), so it ships from the same upstream
(`davidmegginson/ourairports-data`) that pi4 already uses.
Work items:
  - **Data**: convert the airports + runways CSVs into a compact
    browser-loadable form (JSON shards by lat/lon tile, or a single
    binary blob with a header index — match what terrain.js does
    so the cache + service-worker story is consistent).
  - **airports.js module**: Terrain-style API:
    `Airports.init()`, `Airports.downloadGlobal(progressCb)`,
    `Airports.downloadRegion(lat, lon, radiusNm, progressCb)`,
    `Airports.nearby(lat, lon, radiusNm)`, `Airports.tileCount`,
    `Airports.status`. Use IndexedDB (or the same Cache API
    terrain.js uses) so it survives offline.
  - **Marker on AI**: `Airports.render(ctx, D, L)` called after
    `Terrain.render` and before the tape overlays — projects each
    nearby airport via the same focal/yaw/pitch/roll math the
    terrain mesh uses (factor that out into a shared helper if
    it isn't already). Render as a simple "signpost": small
    vertical pole with the ICAO identifier next to it. No
    paved/unpaved/heliport symbol distinction — just the post
    and the label, same shape for every airport.
  - **Type + size filter**: carry over pi4's four type toggles
    (`show_public`, `show_heli`, `show_seaplane`, `show_other` —
    public covers S/M/L by longest runway). Pi4-only `show_runways`
    and `show_centerlines` are skipped since iPhone only renders
    the signpost. The marker stays the simple post regardless of
    type/size; the filter just controls which airports appear.
    Persist all toggles to localStorage.
  - **Download/manage screen**: new "AIRPORTS" entry in the setup
    menu mirroring TERRAIN. Two buttons (Global / Regional),
    progress bar, count display, last-updated timestamp, clear
    cache button. Reuse the existing terrain panel CSS so it
    looks like a sibling, not a one-off.
  - **Status indicators**: NO APT / EXP APT badges in the same
    row as the GPS / link badges, mirroring pi4's `_AMBER`
    convention so the displays stay visually aligned.
  - Decide whether airport data should auto-download on first
    install (PWA add-to-home-screen), or be opt-in like terrain.
    Probably opt-in for the global set, auto for the
    home-airport-region.

### #16  iPhone heading tape — show 25% more degrees at once
Status: **OPEN**
Target: `iphone_display/index.html` `drawHeadingTape`.
Context: The heading tape currently spans 90° across the canvas
(`pxPerDeg = W/90`), so the pilot only sees ±45° around the current
heading. Want roughly 25% more degrees visible at a glance — shrink
the per-degree pixel width so a wider span fits, e.g.
`pxPerDeg = W/112.5` (≈ ±56°), or whatever ratio reads cleanly with
the current bold-double-size labels and 5°/10° tick cadence.
Work items:
  - Drop `pxPerDeg` from `W/90` to ~`W/112` (verify the tick + label
    spacing still has breathing room at the new density).
  - Widen the loop bound from `[-45..45]` to match the new span so
    ticks/labels actually populate the wider visible range.
  - Confirm the heading bug chevron clamp at the tape edges still
    leaves room for the speed/alt tapes and doesn't crowd the
    readout box.
  - Verify on both notched and non-notched phones in landscape.

### #15  iPhone V-speeds editor UI
Status: **OPEN**
Target: `iphone_display/index.html` setup menu — new "V-SPEEDS" panel.
Context: V-speeds (Vs0, Vs1, Va, Vfe, Vno, Vne, Vy, Vx) drive the
speed-tape colour bands and the V-speed labels. Defaults match
Cessna 172S POH and the only way to change them today is to hand-edit
`localStorage['vspeeds']` from the browser console — the comment in
`index.html:956` explicitly notes "Edits to these will eventually
come from a flight-profile UI". Pi4 already has a Flight Profile
screen; iPhone doesn't.
Work items:
  - Add a "V-SPEEDS" button to the setup menu (alongside TERRAIN /
    BAROMETER / TRIM / SENSORS).
  - Panel with eight numpad-driven entries (Vs0, Vs1, Va, Vfe, Vno,
    Vne, Vy, Vx); reuse the existing bug-edit numpad style.
  - Save to `localStorage['vspeeds']` in the same JSON shape the
    init reader already understands.
  - Validate ordering on commit (Vs0 < Vs1 < Vfe ≤ Vno < Vne, etc.)
    and surface an inline error rather than silently storing bad
    values.
  - Match pi4's "V-SPEEDS (knots)" header so the unit convention is
    explicit even when the speed tape is showing mph.

---

## Completed

### TERRAIN-FULLWIDTH  iPhone terrain mesh clipped to tape gap — **FIXED**
Target: `iphone_display/terrain.js` `render()`.
Root cause: `ctx.rect(clipX, tapeTopY, clipW, tapeH)` where `clipX =
spdX + spdW` and `clipW = altX - clipX` restricted the TAWS-coloured
terrain mesh (red = at/above aircraft, amber = within 500 ft) to
the AI gap between the tapes. The coloured horizon band stopped at
each tape's inner edge, so a pilot scanning the horizon saw the
alert only in a narrow central strip.
Fix: clip to `(0, tapeTopY, canvas.width, tapeH)` instead. The tapes
are rendered after terrain and use `rgba(0,8,25,0.80)` backgrounds,
so they tint the band instead of hiding it — the alert colour
now reads edge-to-edge along the horizon while the tape readouts
remain legible.
Closes the "extend TAWS colours to full AI area" sub-task that was
originally part of #11.

### #11  iPhone tape repositioning — outside-edge marks + safe-area — **FIXED**
Target: `iphone_display/index.html` — `computeLayout`, `drawSpdTape`,
`drawAltTape`.
Fix summary:
  - `L.tapeTopY` reduced from `safeT + 26` → `safeT` so the tapes now
    extend up to the notch / status-bar edge (dark tape background
    still sits behind the "GS KT" / "ALT FT" header label inside
    the tape area, so nothing protrudes into the notch).
  - Speed tape ticks moved to the LEFT (outside) edge: major 14 px /
    2 px stroke at every 20 kt, minor 7 px / 1 px at every 10 kt
    (minor ticks are new — pi4 parity). Labels now left-aligned
    immediately to the right of the tick, bold.
  - Altitude tape ticks moved to the RIGHT (outside) edge with the
    same major/minor treatment. Labels right-aligned immediately to
    the left of the tick, bold.
  - Added top/bottom exclusion (12 px) so ticks never collide with
    the header label row. Centre-exclusion zone retained so the
    Veeder-Root readout still hides the current-value tick.
  - TAWS full-AI colour bands deferred to a new issue (#13) — the
    existing TAWS code only draws a centre banner, so there's no
    "tape-scoped" version to extend; implementing it is net-new
    work rather than a #11 cleanup.

### IPHONE-ORIENT-LOCK  iPhone display must never rotate — **FIXED**
Target: `iphone_display/index.html` resize path and touch handlers.
Root cause: in steep bank, the accelerometer's gravity vector fools
iOS's auto-rotate into flipping the PWA to portrait mid-flight — the
PFD content then draws into a tall narrow box, with the heading tape
and tapes rotated 90°. The manifest's `"orientation": "landscape"`
hint alone isn't enough under these conditions, and
`screen.orientation.lock()` is not reliably supported on iOS Safari
or iOS PWAs.
Fix: render the canvas at landscape resolution regardless of the
viewport — when `window.innerHeight > window.innerWidth` we still set
`canvas.width = innerHeight`, `canvas.height = innerWidth`, then apply
CSS `translate(innerWidth, 0) rotate(90deg)` to the canvas element so
it visually covers the portrait viewport while the content reads as
landscape. Touch coordinates are remapped through the inverse
transform in a new `_eventToCanvas()` helper used by both the click
and touchend listeners (`vy → canvas.x`, `H - vx → canvas.y`).
`screen.orientation.lock('landscape')` is still attempted as a
best-effort belt-and-braces call.

### #10  iPhone heading readout box — refinement — **FIXED**
Target: `iphone_display/index.html` `drawHeadingTape`.
Previous implementation was a simple rounded rectangle with a
separately-drawn filled triangle below it. Replaced with a single
chamfered polygon whose outline traces the rectangle AND the triangle
pointer — matching the Pi4 `_chamfer()` path in `draw_heading_tape()`
(white/magenta stroke on black fill, pointer integrated into box
outline rather than a separate solid triangle). Triangle depth 14 px,
base width `bw/3`, corner radius 4 px. M/G subscript moved to
baseline-aligned position outboard of the ° glyph (previously was
top-aligned near the bottom of the box) and bumped to bold for
better legibility on high-DPI phone screens.

### UNIT-BUG  Bug values entered in display units not converted — **FIXED**
Numpad ENTER for `spd_bug` and `alt_bug` stored the user's raw
integer without converting from the current display unit.  Typing
"90" in mph mode stored 90 (interpreted as kt everywhere), so the
tape re-rendered it as 90×1.15 = 104 mph — looked like "the bug
didn't change to what I entered".  Also affected `sim_init_alt`.
Fix: divide entered value by the current spd/alt factor at commit;
multiply by factor for the "Current: N" placeholder and the
(×100 ft/m) / (kt/mph/kph) title suffix.  V-speeds unchanged (they're
entered in kt by design — the Flight Profile screen header reads
"V-SPEEDS (knots)"). pi4 and pi_zero both.

### BARO-ENTER  Baro numpad ENTER did nothing — **FIXED**
Root cause: two bugs compounding. `smooth_state()` copied
`state["baro_hpa"]` → `disp["baro_hpa"]` every frame; numpad ENTER
wrote only to `disp[]`, so the SSE echo from the firmware (at
~20 Hz) overwrote the pilot's entry within 50 ms.  Additionally,
the firmware had no way to know the new QNH, so even if the local
display had held the value, the AHRS-derived altitude would remain
computed against the old QNH — a silent miscalibration.
Fix:
  - Remove `baro_hpa` from `smooth_state()`'s state→disp copy list
    (it's a user-owned Kollsman-window value, not a sensor field).
  - Numpad ENTER now writes `disp["baro_hpa"]`, `state["baro_hpa"]`
    (under lock), and fires an HTTP GET to the Pico W's
    `/baro?qnh=X` endpoint in a background thread so the firmware
    recomputes altitude against the new QNH.
  - The Connectivity screen's `ahrs_url` field is used as the base
    URL so USB-connected or alternate-AP setups work.
pi4 and pi_zero both.

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
