# Bugs & TODO

Tracked in git so nothing is lost across Claude Code sessions.
When opening a new session, start here for context.

Format: each item gets a short ID, a status, a one-line summary, and
notes with enough context to pick it up cold.

---

## Open

### #1  GL SVT — try `pygame.OPENGL` display mode
Status: **OPEN**
Target: `pi4/svt_renderer_gl.py`, `pi4/pfd.py`
Context: standalone EGL context creation breaks KMS/DRM on this Pi 4
(kernel 6.12 + mesa 25.0 V3D). Disabled in `cad8e40`. Next approach:
use `pygame.display.set_mode(..., pygame.OPENGL)` and call
`moderngl.create_context(standalone=False)` so we share pygame's own
SDL2 GL context instead of creating a new one. Alternative: GBM backend
on `/dev/dri/renderD128`.

### #5  Keyboard fixes (colon, period, backspace, pre-populated values)
Status: **OPEN**
Target: pi4 / pi_zero keypad/keyboard modal.
Context: colon and period keys don't register, backspace doesn't
delete, and the modal doesn't pre-populate with the current value
when opened for an existing bug/setting.

### #6  WiFi SSID — show actual connected network
Status: **OPEN**
Target: `_poll_wifi_status` in pi4 / pi_zero.
Context: status shows configured SSID, not the actually-connected one.
Need to read from `iw dev wlan0 link` or `iwgetid -r`.

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

### #4  USB AHRS — hardware bring-up
Status: **CODE DONE, HARDWARE NOT VERIFIED** (commit `0044215`)
Context: firmware emits `$AHRS,{json}` over USB CDC; `SerialClient`
reads from `/dev/ttyACM0`; pi4 tries USB first, falls back to WiFi.
To verify on Pi: (a) `ls -l /dev/ttyACM0`, (b) `groups` (need dialout),
(c) `screen /dev/ttyACM0 115200` should show `$AHRS,…` lines,
(d) `pfd.py` startup log should say "AHRS via USB serial".
Note: pi_zero does NOT have USB fallback — add if Zero needs wired AHRS.

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
