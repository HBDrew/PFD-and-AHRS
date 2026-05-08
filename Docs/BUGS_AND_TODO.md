# Bugs & TODO

Tracked in git so nothing is lost across Claude Code sessions.
When opening a new session, start here for context.

Format: each item gets a short ID, a status, a one-line summary, and
notes with enough context to pick it up cold.

---

## Open

### IPHONE-PICO-HOSTING  iPhone display HTML too large for Pico W to serve
Status: **OPEN — blocks iPhone display use over the Pico's WiFi AP**
Target: `firmware/web_server.py` `_load_index` / `_handle_root`,
`iphone_display/index.html`, possibly the hosting strategy.
Context: today flipped the Pico's AP to open and tried to load
`http://192.168.4.1` from the iPhone. Page loaded only a stale
40 KB version of `iphone_display/index.html` that had been flashed
months ago. Tried to flash the current 106 KB version
(`mpremote cp iphone_display/index.html :index.html`) and now the
page won't load at all. Almost certainly Pico W out-of-memory:
`_load_index()` does `with open('index.html', 'r') as f: return f.read()`
— reads the entire HTML into RAM, then writes it to the socket.
Pico W has ~160 KB free MicroPython heap after firmware load; a
106 KB string + the encode buffer + JSON state + pending SSE writes
overflows.
Also no automated way to keep the Pico's `iphone_display/*` files
in sync with the repo — every change in `iphone_display/` requires
a manual `mpremote cp` per file. Drift is silent and we hit it.
Three viable fixes:
  - **A. Stream the file** — change `_handle_root` to open the file
    and `await writer.write(chunk)` in chunks (e.g. 4 KB at a time)
    so it never holds the full HTML in RAM. Smallest change. Works
    with the current bundle as-is.
  - **B. Slim build** — produce a minified iphone_display bundle
    aimed at the Pico (drop service worker, inline only the critical
    paths, defer non-essential JS). Buys headroom for future growth
    but adds a build step.
  - **C. Host on the Pi 4 instead** — the Pi 4 has plenty of RAM
    and is already running an HTTP server for other purposes.
    Pico stays as the AHRS data source (continues to broadcast
    `$AHRS,{json}` over USB to Pi 4); Pi 4 serves the iPhone HTML
    and re-emits `/events` SSE from its own SSE proxy. Most
    flexible long-term but requires plumbing on the Pi 4 side.
Plus auxiliary task — **add `tools/flash_pico.sh`** that bundles
the firmware + `iphone_display/` flash in one command (kill pfd.py,
copy all files, reset, restart pfd.py). Stops the silent-drift
problem we just hit.
Recovery to current state: nothing user-facing on the Pi 4 PFD is
affected (it gets AHRS over USB and renders locally). Only the
iPhone display via WiFi is broken. Roll back to a smaller
`index.html` on the Pico if needed in the meantime, or skip the
iPhone display until one of A/B/C lands.

### WAVESHARE-35-DPI  Pi 4 Waveshare 3.5" DPI panel won't initialise
Status: **OPEN — low priority, blocked on time, ROADOM 7" HDMI works fine**
Target: `/boot/firmware/config.txt`, possibly `pi4/config.py` profile
overlay name, possibly Waveshare's `LCD-show` package.
Context: tried switching from the ROADOM 7" HDMI to the Waveshare 3.5"
DPI hat. Set `DISPLAY_PROFILE = "waveshare_35"` and uncommented the
DPI lines in `config.txt`:
  ```
  dtoverlay=waveshare-35dpi-3b-4b
  framebuffer_width=640
  framebuffer_height=480
  ```
Pi boots and SSH works, but the Waveshare panel stays dark. Diagnosis:
the overlay file `waveshare-35dpi-3b-4b.dtbo` is **not present** in
`/boot/firmware/overlays/` on Pi OS Bookworm (kernel 6.12). Only the
DSI-Waveshare and CAN-hat overlays ship now — the older panel-specific
DPI overlays were deprecated. The dtoverlay line is silently ignored;
`/sys/class/drm/` shows only HDMI connectors, never a DPI one.
Two paths forward when picking this up:
  - **A1**: Install Waveshare's `LCD-show` package
    (`git clone https://github.com/waveshare/LCD-show && sudo
    ./LCD35-show`) — ships the missing overlay. Old script, edits
    `config.txt` itself and reboots; back up `config.txt` first.
  - **A2**: Use the modern `vc4-kms-dpi-generic` overlay with explicit
    panel timings looked up from the Waveshare datasheet. Cleaner,
    keeps the install self-managed.
Confirmed today that path **B** (override `DISPLAY_W`/`DISPLAY_H`
to 640×480 in `config_local.py` while keeping HDMI on the ROADOM)
would let us validate the small-screen *layout* without bringing
up the Waveshare panel — useful as a pre-step for any future
sessions that want to verify PFD rendering at 640×480 before doing
the hardware swap.
Recovery applied today: reverted `config.txt` to HDMI-only, restored
ROADOM 7" as the active display.

### SVT-GROUND-SKIRT  Ground hidden by sky at low altitude + steep bank
Status: **OPEN**
Target: `pi4/svt_renderer_gl.py` (inner mesh near-zone, sky shader,
possibly a new fixed-elevation ground polygon).
Context: at low altitude with steep bank, the camera looks nearly
straight down through the AI window. The terrain mesh has finite
extent and the inner mesh's near zone has a small hole / discard
band near the camera. In that combination, fragments along the
"down-the-wing" sightline fall outside the mesh's footprint at the
camera's near plane, so the sky shader fills in — pilot sees a
strip of blue under the wing where ground should be.
Work items:
  - Identify whether the gap is from the inner mesh's near-zone
    discard, the camera near plane clipping mesh triangles, or the
    outer-mesh discard square not extending below the inner mesh
    when banked. A frame capture at low alt + 60° bank should make
    the geometry obvious.
  - Add a low-cost "ground skirt": a flat polygon at terrain
    elevation that extends well below and around the camera so any
    near-camera gap fills with terrain colour rather than sky.
    Cheap (single quad) and depth-tested so legitimate terrain
    overdraws it.
  - Verify under the ROADOM 7" sim: low-pass at 100 ft AGL at
    Sedona with 45–60° bank should show ground edge-to-edge
    through the AI, not a wedge of blue along the lower wing.

### SVT-MESH-OVERLAP  Visible blue gap between high-res and low-res mesh
Status: **OPEN**
Target: `pi4/svt_renderer_gl.py` — outer mesh's `grid_max_dist_m` /
mesh extents, plus the inner mesh's far edge.
Context: the SVT renders an inner high-res mesh (sharp foreground)
and an outer coarse mesh (distant ridges). They overlap by ~20 %
(`discard_inside_m = mesh_radius_m * 0.80`) so the inner owns the
foreground. In a few situations a thin wedge of sky shows through
the seam between the two meshes — typically along the horizon at
moderate banks, where the inner mesh's far edge sits just above
the outer mesh's near edge as drawn through the perspective
projection.
Work items:
  - Increase the overlap band — drop `discard_inside_m` to
    something like 0.65 of the inner radius, or extend the inner
    mesh outward by a tier so the seam lives further out where
    perspective compresses it below pixel resolution.
  - Alternative: pull the outer mesh slightly down toward the
    horizon by biasing its vertices' Z by a small constant when
    rendered, so any sliver always reads as terrain rather than
    sky. Cheap if the bias is small enough not to affect the
    silhouette.
  - Confirm the fix doesn't reintroduce the "morphing recentre"
    artefacts from the original mesh-snap work (commit chain on
    the GL bring-up). A regression-style preview that captures
    pre/post for the same scene at the same camera state would
    catch this.

### AGL-PRECISION  AGL readout shouldn't show 1-foot precision
Status: **OPEN**
Target: `pi4/pfd.py` `draw_agl_readout`.
Context: the AGL readout currently shows 1 ft precision, which
flickers in the last digit because both the GPS altitude and the
SRTM-derived terrain elevation only have 10–30 ft of real
precision. Round the displayed value to the nearest 10 ft so the
readout sits steady. Same minimum-resolved-value treatment that
the altitude tape already gets via the rolling-drum.

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

### #12b  iPhone compass GPS-track auto-cal
Status: **OPEN** (cardinal walk-through landed — see Completed §#12a)
Target: `iphone_display/index.html` `_onOrient`, `applyPhoneSensors`,
`COMPASS_CAL`.
Context: with the cardinal walk-through in place, a second auto-cal
mode would let a pilot keep the offset accurate without taxiing four
cardinals. When GPS groundspeed > 15 kt for ≥10 s and the compass is
live, compute `offset = gps_track − compass` (unwrapped, averaged over
the sample window). Roll into `COMPASS_CAL.offset` with a low-pass
filter so wind/crab doesn't bias it; require straight-and-level
(|roll| < 5°) to include a sample. Surface a "CAL: AUTO" badge so the
pilot can see when it's actively learning vs. holding the previous
offset.
Pairs with firmware item AHRS-MAGCAL below — when the firmware-side
mag cal also lands, the iPhone compass and the AHRS compass will both
converge on the GPS track and stay aligned.

### SDP31-AIRDATA  SDP31-500Pa airspeed driver + air-data computer
Status: **OPEN — board lays out the SDP31 alongside WT901, BME280, GPS**
Target: new `firmware/sdp31.py`, additions to `firmware/main.py`,
new fields in the `$AHRS,{json}` packet consumed by `pi4/serial_link`
and the iPhone SSE client.
Context: the new sensor board carries a Sensirion SDP31-500Pa
differential-pressure sensor — pitot pressure on one port, static on
the other.  With the existing BME280 (static pressure + OAT) we get
a complete pitot-static air-data set:
  - **IAS** (knots) = `sqrt(2·dp / ρ₀)` — what the speed tape should
    show.  Currently the tape is fed by GPS groundspeed, which lies
    in any wind.
  - **TAS** (knots) = `IAS · sqrt(ρ₀ / ρ)` where ρ comes from BME280
    static + OAT.  Needed for centripetal accel in AHRS-GPS-AID and
    for the wind-triangle solution.
  - **Pressure altitude** is already computed by BME280 + QNH from
    the firmware's existing baro path; this entry doesn't change it.
  - **Wind solution**: with TAS + heading (from AHRS) + GS + track
    (from GPS), wind = ground_vec − air_vec.  Drop it into the
    `$AHRS` packet as `wind_dir` / `wind_kt` so the displays can
    show a wind ribbon.  Side benefit: validates AHRS-MAGCAL by
    cross-checking the wind solution against forecast / pilot
    observation.
  - **Stall-warning hook**: with IAS available we can light a
    `LOW SPD` enunciator below configured Vs1 in the V-speeds
    profile.  Already a pi4 colour band on the speed tape; this
    just makes the audible/visual alert authoritative.
Work items:
  - I²C driver for the SDP31 — Sensirion's reference protocol is
    short (start continuous mode, read 9-byte frames with CRC,
    handle the auto-zero offsets).  About 80 lines of MicroPython.
  - Air-data math in `firmware/main.py` (or a small `airdata.py`):
    IAS, TAS, density-altitude.  Cross-check IAS against GS at
    cruise to verify driver math before depending on it.
  - `$AHRS` JSON gains `ias_kt`, `tas_kt`, `wind_dir`, `wind_kt`.
  - Pi 4 + iPhone speed tapes: switch the primary source to IAS
    when the air-data path is live (fall back to GS with a small
    "GS" subscript when SDP31 reports unhealthy, mirroring the
    existing GPS-fallback pattern on the heading tape).
  - V-speeds page: nothing changes structurally — the existing
    Vs0 / Vs1 / Vfe / Vno / Vne entries already drive the colour
    bands.  Stall-warn enunciator is the only addition.
Pairs with AHRS-GPS-AID (TAS is the correct centripetal input).

### AOA-CALC  Computed AOA from speed + load factor + ρ (pre-probe)
Status: **OPEN — software-only, can land TODAY with current sensors**
Target: new `firmware/aoa_calc.py` (or extend `firmware/airdata.py`),
`$AHRS` packet, AOA indexer in `pi4/pfd.py` and the iPhone display.
Context: in the linear region of the lift curve, AOA can be inferred
from sensors we already have on the bench: WT901 (load factor),
BME280 (static + OAT for ρ), and GPS (groundspeed).  No new hardware
required to start.  Useful as an on-speed cue and as a bridge until
AOA-PROBE ships; not a substitute for measured AOA at the margins.
Math: from the lift equation `L = n·W = ½·ρ·V²·S·Cl`, solve for Cl
and divide by the airframe's lift-curve slope:
  `α ≈ α₀ + Cl / Cl_α`
  `Cl  = (n·W) / (½·ρ·V²·S)`
where `n` is load factor (from the WT901 accel Z, in g), `V` is
TAS (SDP31 + BME280), `ρ` is air density (BME280 static + OAT), and
`W` / `S` / `Cl_α` / `α₀` are airframe constants for the Rans S21.
Inputs (with the same fallback ladder AHRS-GPS-AID uses):
  - **Velocity** — TAS from SDP31-AIRDATA when it lands; GS from
    `gps.speed_kt` until then.  In zero wind GS == TAS; in wind the
    error scales with the wind component.  Surface the active
    source in the indexer (small `gs` / `tas` subscript) so the
    pilot doesn't trust an upwind on-speed cue.
  - **Load factor** from WT901 (use the same gravity-vector estimate
    the AHRS produces; rotate the body-frame accel into the
    wing-perpendicular axis).
  - **Density** ρ from BME280 static + OAT.
  - **Weight** as a pilot-entered field on the V-speeds / Flight
    Profile screen — accepts dry-weight + fuel and decrements by
    the fuel-flow estimate over the flight (or just a single GW
    entry to start; refine later).
  - **Cl_α, α₀, S** stored in the airframe profile alongside the
    V-speeds (already a JSON profile file).
Caveats — explicit so the indexer doesn't lie:
  - Linear-region only.  Departs from real AOA near the stall, in
    deep flap, and during accelerated stalls.  Calibrate the
    indicator's "yellow" band conservatively and treat the "red"
    band as advisory until AOA-PROBE replaces it.
  - Configuration-blind.  Flap deployment changes Cl_α and α₀; if
    we read flap position later, branch the constants.  Until
    then, calibrate against clean configuration only.
  - Weight-dependent.  Bad fuel-state estimate biases the whole
    output.  Surface the assumed weight on the profile screen.
  - Until SDP31-AIRDATA lands, "velocity" means GS — see source
    subscript above.
Work items:
  - Add airframe constants (Cl_α, α₀, S, max-gross W, flap-clean
    flag) to the V-speeds profile.  Default to Rans S21 numbers;
    pilot tunes against measured stall on first-flight cards.
  - `aoa_calc.py` runs each AHRS frame, emits `aoa_deg` and a
    `aoa_src` = `"calc"` field on the `$AHRS` packet.  When
    AOA-PROBE goes live the same field flips to `"probe"` and
    the same display consumes both.
  - Display: AOA indexer on the right side of AI when no approach
    is active (same slot AOA-PROBE will use).  Show a small `c`
    subscript on the indexer while `aoa_src == "calc"` so the
    pilot knows it's the inferred value.
Pairs with SDP31-AIRDATA (the inputs come from there) and with
AOA-PROBE (this entry retires when the probe lands, or stays as
a redundant cross-check).

### FPV  Velocity vector / flight-path marker on the AI
Status: **OPEN — software-only, can land TODAY**
Target: `pi4/pfd.py` (new draw routine inside the AI block), iPhone
display equivalent.
Context: a velocity-vector / flight-path-vector marker shows the
pilot where the airplane is actually going through space, not where
the nose is pointing.  Standard on every modern PFD (G3X, Dynon,
Garrecht, mil HUDs).  Indispensable on approach — pilot flies the
FPV onto the runway numbers and lands there, whatever the crab
angle and AOA are doing.  Inputs we already have:
  - **GPS track** (`gps.track_deg`) — azimuthal direction the
    airplane is moving over the ground.
  - **GPS GS + VS** — flight-path angle = `atan2(VS_fps, GS_fps)`.
    GS from `gps.speed_kt`, VS from `gps.vspeed_fpm` (already
    smoothed in the firmware).
  - **AHRS attitude** (yaw/pitch/roll from WT901) — needed to
    project the FPV onto the AI viewport, since the AI is
    drawn in body-frame.
Math: the FPV's screen position is the projection of the velocity
vector into the camera frame the AI is rendered with.  The same
projection chain `draw_airport_symbols` already uses (yaw / pitch
/ roll, focal length, screen centre) takes a unit vector in the
NED frame and returns AI pixel coordinates.  Build a NED unit
vector from track + flight-path-angle, run it through the existing
projection, draw a small open circle with two horizontal "wings"
and a short vertical stub (the conventional FPV symbol).
Work items:
  - Compute FPV NED unit vector from GPS track + GS + VS each
    frame.  Skip when GS < 5 kt (parked / taxi noise) — hide the
    symbol below that gate.
  - Reuse the airport projection helper to land it on the AI.
    Clamp to the AI rectangle so it never escapes the viewport
    in extreme attitudes; show a "ghost" arrow at the edge in
    that case (G3X convention).
  - Symbol: 12 px circle, 6 px wings either side, 6 px vertical
    stub.  Cyan, no fill.  Same colour as the heading-bug bug
    set (pilot-relevant, not alert).
  - Display setup gains an FPV ON/OFF toggle (default ON when
    GPS is healthy).  Persist with the rest of the display
    settings.
  - Sanity check: on the ground rolling forward, the FPV should
    sit in front of the nose (track ≈ heading, FPA ≈ 0).  In a
    coordinated climb-out, the FPV sits below the nose by the
    AOA (a free cross-check against AOA-CALC once it lands).
Pairs with AOA-CALC (the vertical offset between aircraft symbol
and FPV is exactly AOA when the wind is along the flight path —
a free in-flight calibration target for the airframe constants).

### AOA-PROBE  Add a second differential-pressure transducer for AOA
Status: **OPEN — board-revision change for the next layout spin**
Target: hardware (next board rev), `firmware/sdp31.py` (or sibling
driver if a different pressure range is used), additions to the
`$AHRS` packet, AOA indicator on `pi4/pfd.py` and the iPhone
display.
Context: with one differential-pressure transducer doing pitot/static
(SDP31-AIRDATA), a second sensor connected to a flush-port AOA probe
gives a real AOA signal essentially free.  Standard experimental
implementation is a two-hole probe on the wing or a side-mount on
the fuselage where the upper / lower ports sit at different angles
to the local airflow — the ΔP across them is monotonic with AOA over
the normal envelope.  AOA buys things IAS-only can't:
  - **AOA-based stall warn** — fires at the actual stall margin
    regardless of weight, bank, or load factor.  IAS-based stall
    cues only work at 1 g and gross weight (the published Vs1 is
    a lie at any other condition).
  - **Optimal-AOA cues for approach and climb** — fly the
    indexer / donut, not the airspeed.
  - **Energy management on approach** — particularly relevant
    behind a Rotax that responds slowly to throttle.
Sensor selection: the AOA probe usually generates ΔP in the 0–2000 Pa
range over the normal envelope.  Likely candidates: SDP3x-2000Pa
(higher range than the airspeed unit) or Sensirion's specifically-
ranged variants.  Pick the part once the probe geometry is fixed.
Work items (next board rev):
  - Pick the AOA probe (build a flush-port pair, or buy a probe
    head — AlphaSystems and Dynon both sell heads that work with
    a generic differential-pressure sensor).
  - Add the second ΔP sensor to the board (I²C bus already up;
    address-select on the SDP3x line lets us run two on one bus).
  - Driver mirrors `firmware/sdp31.py`; AOA math = calibration curve
    (linear over the cruising range, departs near the stall — fit
    on first-flight data, persist coefficients to flash).
  - AOA field added to `$AHRS` JSON.
  - Display: AOA indexer on the right side of the AI when not on
    approach (mutually exclusive with the VDI from the recent work
    — VDI takes priority during approach, AOA at all other times).
    Standard cue: green/yellow/red segments with a fast-erecting
    diamond, donut at on-speed.
Pairs with SDP31-AIRDATA (same I²C bus + driver pattern) and with
AHRS-GPS-AID (AOA-based stall warn is the safety-of-flight payoff
once attitude is honest).

### AHRS-GPS-AID  GPS-aided AHRS for clean attitude in coordinated turns
Status: **OPEN — on-Pico is the recommended path; Pico 2 W makes it cheap**
Target: new `firmware/ahrs_filter.py`, raw-mode IMU output from
`firmware/wt901.py`, plumbing in `firmware/main.py`.
Context: the WT901's internal Kalman filter doesn't accept external
velocity, so feeding it groundspeed directly does nothing — it can
only see accel + gyro + mag.  The accelerometer measures
`gravity + linear_accel`, and in a coordinated turn the centripetal
component `a_c = V·ω` tilts the apparent gravity vector and biases
the bank solution.  At LOW bank the problem is worse, not better:
at 100 kt and 0.5°/s yaw rate (≈0.5° true bank) the centripetal
signal is ~0.9 m/s² while the gravity-on-Y bank signal is only
~0.085 m/s² — the IMU sees ~10× more "fake tilt" than real tilt.
This is why the leans-during-coordinated-turn artefact survives
even a perfect mag cal.
Architecture is straightforward because **all inputs already live
on the Pico**: WT901 raw accel/gyro on UART, GPS speed/track from
`firmware/gps.py`, and (on the laid-out hardware) an SDP31-500Pa
differential-pressure sensor for IAS/TAS plus a BME280 for static
pressure + OAT.  No cross-device transport needed — the Pi 4 just
consumes the fused result over USB CDC the same way it does today.
**Use TAS, not GS, for centripetal correction**: the IMU's centripetal
accel is `V_air × ω`, not `V_ground × ω`.  In any wind, GS-aiding
introduces an error proportional to the wind component — the SDP31
makes the correction physically right instead of just close.  Pico
2 W (RP2350) makes the path comfortable: hardware FPU collapses
Madgwick/Mahony to free, 520 KB SRAM gives plenty of headroom, and
the second M33 core can carry the per-sample fusion loop without
competing with the AP / web-server / SSE work.  On the original
Pico W (RP2040, soft float, 264 KB) the filter is doable but tight;
defer until the 2 W swap.
Work items:
  - Switch the WT901 driver in `firmware/wt901.py` to raw IMU output
    mode (or run a dual-stream config so both raw + fused are
    available during validation).
  - Implement Madgwick or Mahony in `firmware/ahrs_filter.py` (~50
    lines of MicroPython); reference impls available.  EKF is an
    option if drift compensation needs to be tighter, but Madgwick
    with GPS aiding is plenty for this airframe.
  - Subtract centripetal accel `V × ω_gyro` from raw accel BEFORE
    the level-finding step.  Source the velocity through a fallback
    ladder: **TAS first** (physically correct — see SDP31-AIRDATA),
    **GS second** (GPS speed_kt — close in zero wind, off by the
    wind component otherwise), **basic attitude last** (no
    centripetal correction at all — accel+gyro+mag fusion as today,
    accept the leans in coordinated turns).  ω is always from the
    gyro.  Plumb the active source into the `$AHRS` packet as
    `att_aid` (`tas` / `gs` / `basic`) so displays can surface it
    when the higher-quality source drops out.
  - Replace `main.py`'s yaw/pitch/roll output with the fused result;
    iPhone / Pi 4 displays consume it as today.
  - Validate at a known coordinated bank: 25° at 100 kt should read
    steadily 25°, no sag toward level after roll-in completes.
    Pre-fix it sags by a few degrees within the first 5–10 s.
Pairs with AHRS-MAGCAL (mag cal eliminates yaw bias; GPS aiding
eliminates bank/pitch bias).  Both together give a real AHRS.

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


---

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

### MAP-INSET  2D moving-map inset in the lower-left corner — **FIXED**
Target: new `pi4/moving_map.py`; render hook in `pi4/pfd.py`; new
toggles in Display setup; persistence in `pi4/data/settings.json`.
Fix: commit `8745e03`. Pure-pygame inset (no GL context contention)
that reuses the airport, runway, obstacle and SRTM caches the SVT
already keeps loaded. Layers: hypsometric terrain tint (cached
surface keyed on quantised centre + range + orient, rebuilt only on
pan/zoom), runways, obstacles, airports, direct-to course +
waypoint diamond, own-ship chevron, range ring, frame + corner
labels. Track-up rotates the cached tint by current track; north-up
keeps north up and the chevron rotates to track. Six discrete zoom
levels (1/2/5/10/20/40 nm). Pinch-to-zoom (two-finger FINGERMOTION,
1.35× ratio per step) plus a single-tap fallback (left half = zoom
out, right half = zoom in) so the inset is usable even when the
touch driver doesn't surface FINGERMOTION events. Display setup
gains MAP INSET (OFF/ON + TRK↑/N↑ packed), MAP RANGE, SUN POSITION
and MAP LAYERS (TER/WTR/APT/RWY/OBS multi-toggle); the unused
NIGHT MODE placeholder retired to make room.

### SUN-POSITION  Real-time sun position drives terrain shading — **FIXED**
Target: new `pi4/sun.py`; `pi4/svt_renderer_gl.py`
(`render_svt_gl`, `render_svt_into_current_fb`); render hook in
`pi4/pfd.py`. Fix: commit `8745e03`. NOAA solar-position formulas
take UTC + GPS lat/lon, return azimuth, elevation, and a civil-
twilight intensity ramp (-6° → 0, +6° → 1) so dawn / dusk fades
smoothly into ambient instead of stepping. The two GL render
entrypoints accept optional `sun_az_deg` / `sun_el_deg` /
`sun_intensity` kwargs; passing `None` falls back to the current
SE / mid-morning module constants, which preserves the previous
behaviour exactly when SUN POSITION is set to FIXED on the Display
setup. Per-frame compute is cheap (sun moves ~0.25°/min, but
re-evaluating per frame is ~5 µs).

### RUNWAY-VECTORIZE  Runway symbol overlay vectorised — **FIXED**
Target: `pi4/pfd.py` `draw_runway_symbols`, `shared/runways.py`.
Fix: commit `878fe28` mirrors the airports.py optimisation — sort
the structured array by midpoint latitude at load, cache contiguous
lat/lon columns at module level, then bound candidates with
`np.searchsorted` before any per-row work. Dense-area test (~50
candidate rows in slab) drops 0.97 ms → 0.54 ms (1.8×); sparse
queries drop 13.6×. In `pi4/pfd.py`: `surf.set_clip` hoisted out of
the per-runway loop (was set/restored per runway, now once per frame
around the whole pass); duplicate set_clip dropped from
`_draw_extended_centerline`; `nm_per_deg_lon` threaded into
`_project_latlon` so `cos(radians(lat))` is computed once per draw
instead of on every projection (10+ per runway, 24+ per centerline);
list+min/max corner-bbox tests replaced with scalar comparisons.
On-disk cache format unchanged — older un-sorted .npy files re-sort
in memory at load.

### SVT-FPS-TURNS  Steep-bank FPS dip — **RESOLVED IN PRACTICE**
Target: `pi4/svt_renderer_gl.py` `FRAGMENT_SHADER`.
Context: previously flagged as 15–20 FPS dip during steep banked
turns vs. 30 FPS baseline. Field testing reports turns hold up
fine now — no longer a pressing issue. Specific shader-cost knobs
(LUT for `clearance_color()`, coarser per-fragment normal,
`MESH_GRID_N` 300→200) are still available if a future regression
brings this back, but no work is queued.

### #1  GL SVT — pygame.OPENGL shared-context composite — **FIXED**
Target: `pi4/svt_composite_gl.py` (new), `pi4/test_svt_composite.py` (new),
`pi4/svt_renderer_gl.py`, `pi4/pfd.py`, `pi4/config.py`.

Fix: full hardware bring-up of the shared-GL composite renderer on
Pi 4 + ROADOM display, plus a long sequence of correctness fixes
that surfaced once it ran live. End state: 30 FPS sustained terrain
+ overlays, rock-stable across mesh recentres, default renderer flipped
to `"opengl_shared"`. Commit chain on `claude/test-svs-display-OLAg2`:

  - `c16a793` — initial bring-up: SDL `GL_DEPTH_SIZE=24`, explicit
    depth-clear, `pygame.draw.polygon` SRCALPHA fill, on-screen FPS
    readout for hardware validation.
  - `8bb15ae` — world-grid mesh snap + camera-offset eye, thicker
    roll arc (`_ARC_THICK` 2→4), AA outlines for filled polygons,
    pitch-ladder lines doubled (major 2→4 px, minor aaline→2 px).
  - `137b298` — roll arc redrawn as `pygame.draw.lines` (was a
    band polygon producing a double-AA halo).
  - `b919733` — vertex Z = absolute elevation; `u_alt_m` per-frame
    uniform → smooth colour transitions across altitude. `u_world_offset`
    uniform → grid pattern stable across recentres.
  - `13f6e73` — drop unused `in_clearance` attribute (was crashing
    the VAO build after the shader refactor).
  - `b063eb9` — world-aligned sample grid (sample positions snap
    to multiples of `sample_step_m` from world origin). Doghouse
    `gfxdraw.aapolygon` halo removed.
  - `7952a62` — `snap_dlon` derived from snapped `mesh_lat` (was
    drifting per-frame off aircraft `cos(lat)`, causing rebuild
    every few frames; FPS hit + visible morph).
  - `92db1df` — angular sample grid + equator-equivalent grid
    pattern. Same world point always sampled at the same lat/lon;
    `v_world_pos.x = in_pos.x / cos(mesh_lat) + (mesh_lon * NM*60) mod GP`
    in the vertex shader → grid pattern in world coords, invariant
    across mesh recentres in any direction. Foreground rock-solid;
    grid stays locked to terrain features.
  - This commit — temporary FPS readout removed; default
    `SVT_RENDERER` flipped to `"opengl_shared"`.

Trade-off worth documenting: grid is now in fixed angular spacing
(lat/lon graticule) rather than fixed metric. At lat 33° each minor
cell is ~773 m east × 926 m north — slightly elongated, like a real
chart graticule. Acceptable for the world-anchoring it buys; if a
square-cell metric grid is needed later, introduce a fixed reference
latitude.

Residual: FPS dips to 15-20 during steep banked turns. Tracked
separately as SVT-FPS-TURNS (low priority).

### #12a  iPhone compass calibration — cardinal walk-through — **FIXED**
Target: `iphone_display/index.html` — new `#compass-cal-panel`,
`COMPASS_CAL` global, additions to `_onOrient`.
Fix:
  - HEADING setup panel gains a 🧲 CAL COMPASS button that opens a
    new full-width calibration panel.
  - Pilot points the aircraft at NORTH (000°), taps CAPTURE, repeats
    for EAST / SOUTH / WEST. Live RAW vs CAL readouts update at
    sensor rate while the panel is open. Restart and Clear-cal
    buttons reset the run / wipe the stored offset.
  - On the fourth capture we compute the circular mean of the four
    `(expected − raw)` deltas (sin/cos sums + atan2 — handles wrap)
    and persist it to `localStorage['compass_cal_offset']`.
  - Offset is applied in `_onOrient` immediately after reading
    `webkitCompassHeading`, so every consumer (display tape, GPS-vs-
    compass cross-source diamond, sensor readout) sees the corrected
    value. Raw `PS.compassHeading` is preserved for future
    calibration runs and for the SENSORS readout.
  - `drawHeadingTape` renders a small CAL subscript on the inboard
    side of the heading readout box whenever an offset is active and
    the displayed source is mag-derived. The SENSORS panel's compass
    row also gains a `(cal +N.N°)` annotation so the pilot can see
    what's been applied at a glance.
GPS-track auto-cal half-mode is tracked separately as #12b.

### #20a  iPhone heading AUTO mode flipped to TRK-first — **FIXED**
Target: `iphone_display/index.html` `_activeHdg()`, hdg-panel hint
copy. AUTO previously preferred MAG (compass) and fell back to TRK
(GPS track). Pilot feedback: GPS track is the more trustworthy of
the two on a moving aircraft (true-north, no mag drift, no panel-
iron bias), so AUTO now picks TRK whenever GPS speed > 3 kt and
falls back to MAG only when GPS isn't moving. Hint text updated.

### CROSS-SOURCE-DIAMOND  iPhone heading tape — show the *other* source — **FIXED**
Target: `iphone_display/index.html` `_activeHdg()` + `drawHeadingTape`.
When both compass and GPS track are live, the tape now draws a
filled diamond at the *other* source's value so the pilot can see
divergence (wind correction angle, magnetic variation, compass
drift) without having to flip modes. Diamond colour follows the
source convention: cyan = magnetic, magenta = GPS track. So in MAG
mode (white tape) you see a magenta diamond at the GPS track; in
TRK or AUTO modes (magenta tape) you see a cyan diamond at the
magnetic heading.

### #20  iPhone HDG vs TRK — enunciated, with user preference — **FIXED**
Target: `iphone_display/index.html` — new `HEADING` setup panel,
`_activeHdg()` resolver, and `drawHeadingTape` rewiring.
Fix:
  - Setup menu gains a 🧭 HEADING entry opening a panel with three
    mutually exclusive buttons: MAG, TRK, AUTO. Choice persists in
    `localStorage['hdg_mode']` (default AUTO).
  - New `_activeHdg()` returns `{ value, label, color, useTrack }`
    — the single source of truth for what to display and how to
    colour it. MAG always uses `D.yaw` (AHRS or phone compass), TRK
    uses `D.track` when GPS speed > 3 kt, AUTO prefers MAG when
    `D.ahrs_ok` else TRK when groundspeed is live. Unavailable
    preferred sources fall back with an amber `M?` / `G?` / `?`
    warning subscript.
  - `drawHeadingTape` now reads `hdg = src.value`, so the tape, the
    bug chevron offset, and the readout digits all agree with the
    subscript — no more silent mismatch between what's displayed
    and what the label claimed.
  - Enunciation via colour only (pilot preferred the quieter
    treatment): the heading readout box border and the `M`/`G`
    subscript take the source colour (white = MAG, magenta = TRK,
    amber = warning). The heading bug chevron stays cyan on MAG
    (pi4 convention for pilot-selected targets) and flips to the
    source colour on TRK / warning so a handover is visible
    without a pill, flash, or pulse.

### #19  iPhone VSI broken on GPS + VS bar too small — **FIXED**
Target: `iphone_display/index.html` — `PS` state, `_onGeolocation`,
`applyPhoneSensors`, and `drawAltTape`.
Fix:
  - GPS-derived vspeed: new `PS.altHist` rolling window keyed by
    `_onGeolocation` (pushes `{t, alt}` when the fix includes a
    3-D altitude). `_psComputeVspeed()` runs a least-squares slope
    over the last 5 s (requires ≥ 2 s span to report non-zero).
    `applyPhoneSensors` writes `PS.vspeed` → `TARGET.vspeed` when
    SSE is silent, so the VSI now moves when the pilot is only on
    phone sensors.
  - Pi4-style VS bar on the inside (left) edge of the alt tape:
    magenta needle growing from the midpoint against a ±2000 fpm
    scale (200 ft of tape ≡ 2000 fpm, matching pi4). Tick marks at
    ±500 / ±1000 / ±1500 / ±2000 fpm, faint gutter so the scale is
    visible at level flight, white zero-line reference.
  - Numeric VSI readout enlarged (`max(15, H*0.033)` bold, was
    `max(10, H*0.022)`), magenta when |vspeed| > 30 fpm so the
    readout and bar read as a set. Thousands-separated
    (`±1,250 fpm`) to stay compact at 4-digit rates.

### #18  iPhone tapes/drums too jumpy — per-field smoothing — **FIXED**
Target: `iphone_display/index.html` `smooth()` and render loop.
Fix: replaced the single-alpha lerp (`alpha = min(1, dt/60)`) with a
per-field `SMOOTH_TAU` table and `alpha = 1 - exp(-dt_ms/tau)`. Short
tau on attitude (roll/pitch 150 ms, yaw 250 ms) so motion still
tracks, long tau on the tape/drum readouts (speed 600 ms, alt 700
ms, vspeed 1200 ms, track 400 ms) to kill SSE noise and phone-sensor
jitter. Render loop now passes `dt_ms` directly (capped at 250 ms so
a long background pause doesn't force a huge single-frame jump).
Applies to both live-AHRS and phone-sensor sources since both feed
through `TARGET → smooth()`.

### #16  iPhone heading tape — show 25 % more degrees at once — **FIXED**
Target: `iphone_display/index.html` `drawHeadingTape`.
Fix: span widened from 90° to 112° via a new `HDG_SPAN_DEG` constant,
so the pilot now sees ±56° instead of ±45°. `pxPerDeg = W/HDG_SPAN_DEG`
and the tick loop bound derives from `HDG_HALF = ceil(HDG_SPAN_DEG/2)`.
Existing bug-chevron clamp (`spdX+spdW+20 … altX-20`) still holds and
the 10° label cadence reads cleanly with the bold double-size labels.

### #14  iPhone baro button shouldn't exist when in GPS-ALT — **FIXED**
Target: `iphone_display/index.html` `drawBaroButton`, `_handleSpdTap`.
Fix: added `_baroAdjustable()` helper returning `D.baro_ok !== false &&
D.baro_src !== 'gps'`. Both the rounded-rect draw and the tap branch
early-return when it's false, so the button disappears and the area
doesn't register taps in GPS-ALT mode — matches pi4 behaviour. The
column sits empty (heading tape shows through), which is the intended
visual. Also simplified `drawBaroButton` — the old magenta-when-GPS
branch is dead code now that the button never draws in that state.

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
