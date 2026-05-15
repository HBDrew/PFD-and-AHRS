# Display Unit (Pi 4) — High-Level Requirements

| Field          | Value                                         |
|----------------|-----------------------------------------------|
| Document No.   | HLR-DISP-PI4-001                              |
| Title          | Display Unit (Pi 4) — High-Level Requirements |
| Project        | Pico-AHRS / PFD                               |
| Date           | 2026-05-15                                    |
| Version        | 0.4                                           |

---

## 1. Overview

The Pi 4 display unit is the high-performance variant of the pilot-facing PFD. It runs on a Raspberry Pi 4 (2 GB) and renders a full Primary Flight Display with true Synthetic Vision Terrain (SVT) using OpenGL ES vector graphics. The SVT renderer uses a 3D perspective projection so that terrain features — including mountain peaks and ridges above the aircraft's altitude — are visible above the horizon line. The display unit receives flight-state data from the AHRS unit over a Wi-Fi SSE stream and provides the same touch-based interface, menus, simulator, and demo mode as the Pi Zero 2W variant. The Pi 4's VideoCore VI GPU and additional RAM allow sustained 30 fps rendering of the full SVT terrain mesh, vector-drawn instruments, and anti-aliased graphics.

For the lightweight variant without SVT, see HLR-DISP-ZERO-001.

---

## 2. Hardware Platform

> **REQ-DISP-PI4-HW-001** The processor shall be a Raspberry Pi 4 with a minimum of 2 GB RAM.

> **REQ-DISP-PI4-HW-002** The nominal display shall be a ROADOM 7" IPS 1024×600 HDMI panel with USB capacitive touch. The software shall support a configurable display profile via `DISPLAY_PROFILE` in `pi4/config.py` so that alternative panels of the same or higher resolution, or a 640×480 Waveshare DPI panel as a fallback, can be substituted. The minimum supported resolution is 640×480 pixels.

> **REQ-DISP-PI4-HW-003** The touchscreen shall support at minimum 2-point simultaneous touch detection to allow two-finger gestures for menu access.

> **REQ-DISP-PI4-HW-004** The display unit shall be powered from an aircraft 5 V supply (3 A minimum for Pi 4).

> **REQ-DISP-PI4-HW-005** The GPU memory allocation shall be set to a minimum of 256 MB to support OpenGL ES terrain rendering.

---

## 3. Rendering Performance

> **REQ-DISP-PI4-REND-001** The PFD shall render at a sustained minimum of 30 frames per second under all normal operating conditions including full SVT terrain rendering.

> **REQ-DISP-PI4-REND-002** The PFD renderer shall use a hybrid architecture: pygame/SDL shall draw all PFD UI elements (tapes, pitch ladder, roll arc, drum boxes, menus, aircraft symbol) into the display surface; OpenGL ES shall render only the SVT terrain background, which is composited into the attitude indicator region of the pygame surface as a `pygame.Surface` read back from an offscreen framebuffer.

> **REQ-DISP-PI4-REND-003** The OpenGL SVT renderer shall use OpenGL ES 3.0 (accessed via `moderngl`) with a standalone EGL context so that no X11 or Wayland compositor is required. If the EGL context cannot be created, the renderer shall automatically fall back to the pygame scanline SVT implementation.

> **REQ-DISP-PI4-REND-004** The SVT rendering path shall be selectable via the `SVT_RENDERER` configuration value (`"opengl"` or `"pygame"`). Default shall be `"opengl"`.

> **REQ-DISP-PI4-REND-005** Anti-aliasing on terrain grid lines shall be applied via `fwidth()`-based screen-space derivatives so that line width is constant regardless of terrain distance.

> **REQ-DISP-PI4-REND-006** An IIR low-pass smoothing filter with coefficient α = 0.25 per frame shall be applied to the attitude, altitude, airspeed, and vertical speed values before rendering.

---

## 4. Data Stale Detection

> **REQ-DISP-PI4-STALE-001** If no valid SSE event is received from the AHRS unit within 3 seconds, the display shall treat all received data as stale.

> **REQ-DISP-PI4-STALE-002** While data is stale, the display shall show a `NO LINK` status badge and shall set `ahrs_ok = false` internally.

---

## 5. Airspeed Tape

> **REQ-DISP-PI4-SPD-001** The airspeed tape shall display a Veeder-Root style drum readout centred on the current airspeed value, with smooth continuous digit roll as speed changes.

> **REQ-DISP-PI4-SPD-002** V-speed colour arcs shall be drawn on the right edge of the airspeed tape: white (VS0–VFE), green (VS1–VNO), yellow (VNO–VNE), and a red radial line at VNE.

> **REQ-DISP-PI4-SPD-003** The drum numerals shall change colour based on airspeed: white below VNO, yellow above VNO, red above VNE.

> **REQ-DISP-PI4-SPD-004** A speed bug chevron shall be rendered at the currently set speed bug value.

> **REQ-DISP-PI4-SPD-005** The speed bug chevron and readout button shall be MAGENTA when the source is GPS groundspeed, and CYAN when the source is an IAS sensor.

> **REQ-DISP-PI4-SPD-006** The pilot shall be able to set the speed bug via numpad entry by tapping the readout button.

> **REQ-DISP-PI4-SPD-007** The airspeed source shall be selectable from the AHRS / Sensors sub-menu. The selectable options shall be `IAS SENSOR` (SDP31-500Pa with BME280 density correction, sourced from the AHRS `ias_kt` field) and `GPS GS` (GPS groundspeed, sourced from `speed`). The default selection shall be `IAS SENSOR`, with automatic fallback to `GPS GS` when `airdata_ok = False`.

> **REQ-DISP-PI4-SPD-008** When the active airspeed source falls back from `IAS SENSOR` to `GPS GS` (sensor failure, missing baro, or `airdata_ok` cleared), the speed tape, drum, and bug shall recolour from cyan to magenta to communicate the source change without an explicit banner. No additional alert is required: the colour convention is the same as the altitude tape's baro/GPS fallback indication.

---

## 6. Altitude Tape and VSI

> **REQ-DISP-PI4-ALT-001** The altitude tape shall display a Veeder-Root drum readout advancing in 50 ft increments.

> **REQ-DISP-PI4-ALT-002** An altitude bug chevron shall be rendered at the currently set target altitude.

> **REQ-DISP-PI4-ALT-003** The altitude bug and readout button shall be CYAN when barometric, MAGENTA when GPS-derived.

> **REQ-DISP-PI4-ALT-004** The baro setting button shall display the current QNH in CYAN when the barometric sensor is active, or `GPS ALT` in MAGENTA when absent.

> **REQ-DISP-PI4-ALT-005** The pilot shall be able to enter a QNH baro setting via numpad in either inHg or hPa.

> **REQ-DISP-PI4-ALT-006** The altitude bug shall be settable by tapping the readout button or by tapping directly on the altitude tape.

> **REQ-DISP-PI4-ALT-007** A vertical speed indicator bar shall run along the inner edge of the altitude tape, scaled to ±2000 fpm, turning amber above ±1500 fpm.

---

## 7. Attitude Indicator and Synthetic Vision

The attitude indicator on the Pi 4 variant provides full 3D Synthetic Vision Terrain rendering, including terrain features visible above the horizon line.

> **REQ-DISP-PI4-AI-001** The SVT terrain background shall be rendered using a 3D perspective projection from the aircraft's current position (latitude, longitude, altitude) looking along the current heading vector.

> **REQ-DISP-PI4-AI-002** Terrain features whose elevation exceeds the aircraft's current altitude shall be visible above the horizon line as mountain peaks and ridges, rendered in correct geometric perspective.

> **REQ-DISP-PI4-AI-003** The terrain mesh shall be constructed from SRTM elevation data within a configurable radius of the aircraft position. Default radius shall be 20 nautical miles, with configurable altitude-scaled mode available (radius scales with √altitude, clamped between 10 and 40 nautical miles).

> **REQ-DISP-PI4-AI-004** The terrain mesh shall update at a minimum rate of 10 Hz to ensure smooth visual tracking during turns and altitude changes. Mesh rebuilds shall be cached and triggered only when aircraft position moves by more than ~0.3 nm or altitude changes by more than 200 ft.

> **REQ-DISP-PI4-AI-005** Terrain shall be coloured by clearance relative to the aircraft altitude using the following palette: red for terrain above aircraft altitude, deep orange for 0–100 ft clearance, amber for 100–500 ft, brown for 500–1000 ft, dark brown for 1000–2000 ft, and very dark brown for clearance greater than 2000 ft.

> **REQ-DISP-PI4-AI-006** When SRTM terrain tiles are not present, a plain horizon shall be displayed using a solid blue upper half and solid brown lower half.

> **REQ-DISP-PI4-AI-007** A sky gradient background shall be rendered behind the terrain mesh, darker at zenith and lighter near the horizon. The sky/ground boundary in the background shader shall rotate with aircraft roll to match the terrain mesh orientation at any bank angle.

> **REQ-DISP-PI4-AI-008** Beyond the mesh radius, the rendering shall transition smoothly to a dusty atmospheric-haze gradient (lighter at horizon, darker deep) so that there is no visible seam between the mesh edge and the distant view.

> **REQ-DISP-PI4-AI-009** Directional sun-angle lighting shall be applied to the terrain mesh using a Lambertian diffuse model. Slopes facing the sun shall appear brighter; slopes in shadow shall darken toward a configurable ambient level. Sun azimuth, elevation, intensity, and ambient level shall be configurable in the renderer module.

> **REQ-DISP-PI4-AI-010** A cardinal-aligned distance grid shall be overlaid on the terrain. Minor lines shall be drawn every 0.5 nautical miles; major lines every 2 nautical miles. Grid line width shall be anti-aliased and constant in screen space regardless of distance.

> **REQ-DISP-PI4-AI-011** Distance grid line colour shall be contrast-aware: light cyan-white on safe-clearance terrain, dark blue on caution/warning terrain (red/orange zones). Line intensity shall boost automatically over red zones so the lines remain clearly visible.

> **REQ-DISP-PI4-AI-012** The distance grid shall fade to invisible at the mesh edge to avoid clutter at the haze transition.

> **REQ-DISP-PI4-AI-013** A zero-pitch reference line shall be drawn across the attitude indicator as a pair of cyan hash marks with a gap for the aircraft symbol. The line shall offset vertically with pitch (drop below centre for positive pitch, rise above centre for negative pitch) using a scale of 10 pixels per degree matching the pitch ladder, and shall rotate around the AI centre with aircraft roll.

> **REQ-DISP-PI4-AI-014** The SVT vertical field of view shall be 48° so that the pitch ladder bars, the zero-pitch reference line, and the SVT horizon all align at the same screen position for any given pitch angle.

> **REQ-DISP-PI4-AI-015** Pitch ladder lines shall be drawn at ±5°, ±10°, ±15°, ±20°, and ±30° from the horizon, consistent with GI-275 styling. The pitch ladder 0° bar shall coincide with the zero-pitch reference line.

> **REQ-DISP-PI4-AI-016** A roll arc shall be rendered implementing the sky-pointer convention. The arc, tick marks (10°, 20°, 30°, 45°, 60°), and outer doghouse marker shall rotate with the sky so that a fixed aircraft reference inside the arc reads the current bank angle on the graduated scale.

> **REQ-DISP-PI4-AI-017** A slip/skid ball indicator shall deflect laterally in proportion to lateral acceleration (`ay`).

> **REQ-DISP-PI4-AI-018** A fixed amber delta-wing aircraft symbol shall be rendered at the centre of the attitude indicator.

---

## 8. Heading Tape and Source Modes

> **REQ-DISP-PI4-HDG-001** A heading tape shall scroll horizontally across the bottom of the display with cardinal and intercardinal labels.

> **REQ-DISP-PI4-HDG-002** A central heading box shall display the current heading as a 3-digit value with degree symbol.

> **REQ-DISP-PI4-HDG-003** A subscript `M` (magnetometer) or `G` (GPS track) shall appear in the heading box.

> **REQ-DISP-PI4-HDG-004** In GPS TRK mode, the heading box border shall be MAGENTA.

> **REQ-DISP-PI4-HDG-005** GPS TRK mode shall slave the heading to GPS ground track using a complementary filter (K = 0.05 default).

> **REQ-DISP-PI4-HDG-006** A heading bug shall be rendered on the tape, colour-coded by heading source (CYAN for MAG, MAGENTA for GPS TRK).

> **REQ-DISP-PI4-HDG-007** The heading bug shall be settable via numpad or by tapping the heading tape.

> **REQ-DISP-PI4-HDG-008** A CYAN GPS ground track tick mark shall be rendered on the heading tape when in MAG mode with valid GPS fix.

---

## 9. Terrain and Obstacle Proximity Alerting

> **REQ-DISP-PI4-TAWS-001** A TERRAIN CAUTION banner (amber, steady) shall be displayed when terrain or an obstacle is within 500 ft below aircraft altitude.

> **REQ-DISP-PI4-TAWS-002** A PULL UP / TERRAIN banner (red, 1 Hz flash) shall be displayed when terrain or an obstacle is within 100 ft below aircraft altitude.

> **REQ-DISP-PI4-TAWS-003** Obstacle proximity alerting shall activate within 3 nautical miles of the aircraft.

> **REQ-DISP-PI4-TAWS-004** SRTM terrain tiles shall be downloadable from within the PFD user interface.

> **REQ-DISP-PI4-TAWS-005** FAA Digital Obstacle File data shall be downloadable from within the PFD user interface, with a 28-day expiry indication.

> **REQ-DISP-PI4-TAWS-006** Terrain-clearance alerting shall use a look-ahead along the current GPS ground track of at least 45 seconds at the current ground speed, sampled at no fewer than 12 points and with altitude projected forward by the current vertical speed. The worst (minimum) projected clearance along the look-ahead shall determine the alert level.

> **REQ-DISP-PI4-TAWS-007** Obstacle proximity alerting shall be gated by a forward-facing wedge of ±25° around the GPS ground track. Obstacles abeam or behind the wing shall not trigger the alert, regardless of their absolute clearance.

> **REQ-DISP-PI4-TAWS-008** Both terrain and obstacle alerting (visual banners and audio callouts) shall be inhibited when ground speed is below the pilot-configured VS0 stall speed. This inhibit shall apply uniformly to all alert classes (TERRAIN, OBSTACLE, SINK RATE, PULL UP) to silence false fires during taxi, takeoff roll, and landing rollout.

> **REQ-DISP-PI4-TAWS-009** A SINK RATE caution (audio only; the banner band is owned by TERRAIN / OBSTACLE) shall fire when the aircraft is below 2 500 ft AGL and the descent rate exceeds an AGL-scaled threshold curve: 1 500 fpm at the surface, rising linearly to approximately 5 000 fpm at 2 500 ft AGL. This implements the GPWS Mode 1 (excessive descent rate) callout.

---

## 9A. Airport Display

The display unit shall show nearby airports on the attitude indicator to provide the pilot with immediate situational awareness of emergency-landing options, navigation references, and surrounding airspace structure.

> **REQ-DISP-PI4-APT-001** Airports within a configurable radius of the aircraft (default 20 nm) shall be rendered on the attitude indicator as small symbols projected into the 3D view using the same perspective-projection scale as the pitch ladder and SVT.

> **REQ-DISP-PI4-APT-002** Airport symbol style shall encode airport type:
>
> - Public airport (small / medium / large) — cyan ring with dark centre; a second outer ring shall be added for medium and large airports
> - Heliport — magenta letter "H"
> - Seaplane base — cyan circle with a wavy underscore
> - Balloonport — grey triangle

> **REQ-DISP-PI4-APT-003** The airport identifier (ICAO/local code) shall be rendered within a closer configurable range (default 15 nm) as a "road sign" — a dark-filled, coloured-bordered text box mounted on a thin vertical post anchored at the airport symbol. The post shall lift the sign clear of the airport symbol so the label remains legible against busy terrain. The sign border colour shall match the symbol colour (cyan for public airports, magenta for heliports, etc.). The sign shall be auto-sized to the rendered text width plus padding, and shall be clamped to remain within the AI rectangle when the airport is near the top of the visible area.

> **REQ-DISP-PI4-APT-004** Airports with relative bearing outside the attitude indicator's angular field of view shall be culled so that no symbol appears clipped at the edge of the AI rectangle.

> **REQ-DISP-PI4-APT-005** Airport symbols shall be drawn before obstacle symbols in the Z-order so that close-in towers and obstructions appear on top of airport symbols at the same screen position.

> **REQ-DISP-PI4-APT-006** The airport database shall be the OurAirports global CSV (field-accurate latitude, longitude, elevation, and type for approximately 72,000 airports worldwide). Closed airports and records with missing coordinates shall be filtered out at parse time.

> **REQ-DISP-PI4-APT-007** The airport database shall be downloadable from within the PFD user interface via a dedicated AIRPORT DATA screen. The screen shall show record count, disk usage, age in days since last download, and shall indicate an expired dataset when older than AIRPORT_EXPIRY_DAYS (default 60 days).

> **REQ-DISP-PI4-APT-008** Downloaded airport CSV shall be parsed into a NumPy structured-array cache (.npy) on first access so that subsequent PFD launches load the database without re-parsing the text CSV.

> **REQ-DISP-PI4-APT-009** A `NO APT` status badge (amber) shall be displayed when no airport data is loaded. An `EXP APT` status badge (orange) shall be displayed when the loaded airport data is older than the configured expiry.

> **REQ-DISP-PI4-APT-010** The AIRPORT DATA screen shall provide four independently toggleable display filters controlling which airport types render on the attitude indicator:
>
> - PUBLIC — small / medium / large public-use airports (default: on)
> - HELIPORTS — heliports (default: on)
> - SEAPLANE — seaplane bases (default: off)
> - OTHER — balloonports and uncategorised types (default: off)
>
> Filter state shall persist across the session. When all four filters are off, no airport symbols shall render.

> **REQ-DISP-PI4-APT-011** Runway polygons for airports within 8 NM shall be projected onto the attitude indicator from each runway's low-end and high-end threshold lat/lon/elevation, using the same flat-earth atan2 bearing/elevation projection as airport and obstacle symbols. Polygons shall scale with runway width (derived from `runways.csv`) and translate/scale/rotate correctly with aircraft position, heading, pitch, and roll. The runway database shall be the OurAirports `runways.csv` (approximately 14,700 records globally), parsed into a NumPy structured-array cache (`runways_cache.npy`) analogous to the airport cache. Closed runways shall be filtered out at parse time. Runway polygons shall be drawn before airport symbols in the Z-order so that the cyan airport ring appears on top of the runway asphalt at the airport centre.
>
> A `RUNWAYS` toggle on the AIRPORT DATA screen shall enable or disable runway rendering (default: on). Toggle state shall persist across power cycles via the settings persistence layer.

> **REQ-DISP-PI4-APT-012** Extended dashed centerlines shall be rendered outward from each runway threshold along the runway bearing, for airports within 15 NM. Each centerline shall extend 10 NM from the threshold with 0.5 NM dash segments. Extended centerlines shall not render when the aircraft is between the two thresholds of the same runway.
>
> An `EXT CENTERLINES` toggle on the AIRPORT DATA screen shall enable or disable extended centerlines independently of the runway polygons (default: on). Toggle state shall persist across power cycles via the settings persistence layer.

> **REQ-DISP-PI4-APT-013** The runway polygon shall be clipped to the aircraft's forward half-plane via Sutherland-Hodgman against the perpendicular line through the aircraft. When the aircraft is on / past one threshold (taxi, takeoff roll, landing rollout, low fly-over), the half behind the wing line shall be discarded and the half ahead shall continue to render — the polygon shall not all-or-nothing disappear when one corner crosses the rear of the aircraft.

> **REQ-DISP-PI4-APT-014** Runway-number labels shall be gated per-end by an actual forward-distance check on the threshold lat/lon. Labels shall stop drawing the moment the corresponding threshold passes the wing line, rather than floating on the runway surface where the clip-derived midpoint landed.

> **REQ-DISP-PI4-APT-015** The airport-environment box (green frame around the runway polygon) shall be suppressed when the aircraft is within 2 NM of the runway centroid AND under 500 ft above the threshold elevation. Close-in the box's far corners foreshorten enough that the rectangle reads as broken; the runway polygon itself is the primary cue at that range.

> **REQ-DISP-PI4-APT-016** Obstacle rendering shall apply an airport-boundary clutter filter: obstacles shorter than `_OBS_AIRPORT_FLOOR_FT` (50 ft) AGL within `_OBS_AIRPORT_RADIUS_NM` (1.0 NM) of any runway centroid shall be hidden. This suppresses the dense low-AGL terminal / ramp infrastructure (signs, jetway masts, terminal cornices, taxiway lighting) that otherwise paints across the runway visual at busy fields. Tall airport obstructions like ATC towers (typically 200+ ft) clear the floor and remain visible.

> **REQ-DISP-PI4-APT-017** Obstacles shall additionally be subject to a global `_OBS_MIN_AGL_FT` (25 ft) AGL floor regardless of airport proximity, so airport-surface signs and similar trivial obstructions never render.

> **REQ-DISP-PI4-APT-018** Obstacle labels shall display the obstacle's true MSL top elevation in feet (e.g. `1185`) rather than a bucketed-to-100-ft form. The label format shall make clear the value represents an altitude (MSL) not a height (AGL).

> **REQ-DISP-PI4-APT-019** Obstacle vertical-angle projection shall use the real (sensor) altitude, NOT the camera-floor-clamped `alt_render`. Using the clamped value at low altitudes (when alt_render exceeds real alt to keep the SVT camera above terrain) produces obstacles projected below their actual top angle and made them appear stamped onto the runway visual at major fields like PHX.

---

## 9B. Direct-to Navigation

> **REQ-DISP-PI4-NAV-001** The display shall provide a direct-to navigation feature accessible by tapping the CDI strip above the heading box (live PFD) or the DIRECT TO tile on the AIRPORT DATA screen.

> **REQ-DISP-PI4-NAV-002** A keyboard for waypoint entry shall open with the existing active ident (if any) shown as a dim placeholder under an empty input buffer. The first keystroke shall replace the placeholder rather than appending to it.

> **REQ-DISP-PI4-NAV-003** Pressing ENTER on the keyboard with a non-empty buffer shall validate the typed ident against the airport database. On a match, a centered modal "Activate Direct to *XXXX*?" with CANCEL / ACTIVATE buttons shall be shown. Tapping ACTIVATE (or pressing physical ENTER) shall commit the activation; tapping CANCEL (or pressing ESC) shall dismiss.

> **REQ-DISP-PI4-NAV-004** Pressing ENTER on an empty buffer with an active waypoint loaded shall offer to re-activate the existing waypoint via the same confirmation modal. Re-activation shall reset `act_lat`/`act_lon` to the current aircraft position so the magenta course line redraws from the present position.

> **REQ-DISP-PI4-NAV-005** Pressing ENTER on an unknown ident shall keep the keyboard open with a red "UNKNOWN WAYPOINT *XXXX*" hint under the entry field. Any subsequent keystroke or backspace shall clear the error.

> **REQ-DISP-PI4-NAV-006** A NEAREST quick-button shall be available on the waypoint keyboard. The button label shall display the resolved nearest public airport's ident (S/M/L type within 100 NM of current position, refreshed approximately every 2 s). Tapping shall route through the same Activate? confirmation modal.

> **REQ-DISP-PI4-NAV-007** A magenta course-trace line shall be drawn on the attitude indicator from the activation point to the destination waypoint along the great-circle path, draped over SRTM terrain at a 200 ft offset above the maximum local elevation. Vertices shall be sampled at 0.2 NM intervals (capped at 1000 vertices per course) with rolling-max smoothing so the line never dips below terrain between sample points.

> **REQ-DISP-PI4-NAV-008** The course trace shall be built asynchronously in a background daemon thread and published progressively as samples are computed, so the UI remains responsive during long cross-country activations. If the user changes the active waypoint mid-build, the in-flight worker shall discard its result on detecting the key mismatch.

> **REQ-DISP-PI4-NAV-009** A CDI (Course Deviation Indicator) strip above the heading box shall display ident · bearing · distance and a magenta diamond at full-scale cross-track error. The diamond shall display on the side OPPOSITE the course relative to the aircraft (right-of-course → diamond LEFT, indicating "fly left to intercept"). When no waypoint is active the strip shall display a "DIRECT →" prompt as a tap target.

> **REQ-DISP-PI4-NAV-010** CDI full-scale deflection shall be mode-dependent: ±1.0 NM in en-route / direct-to mode, and ±0.3 NM (RNAV / LPV convention) when a synthetic approach (REQ-DISP-PI4-APPR-001 et seq.) is active.

> **REQ-DISP-PI4-NAV-011** When a synthetic approach is active, the CDI cross-track reference shall be the extended runway centreline (a line through the threshold along the published runway course), NOT the line from the activation point to the threshold. The activation point shall continue to define the cross-track reference in en-route / direct-to mode.

> **REQ-DISP-PI4-NAV-012** When a synthetic approach is active, the CDI ident readout shall append the runway suffix to the airport ident in `IDENT/RWY` form (e.g. `KSEZ/03`).

---

## 9D. Synthetic Approach (HITS + VDI)

The display unit shall provide a synthetic approach capability that loads a 3° glideslope to a selected runway and surfaces three coordinated cues — Highway-In-The-Sky (HITS) tunnel boxes, a vertical glideslope diamond (VDI), and tightened CDI scaling.

> **REQ-DISP-PI4-APPR-001** A synthetic approach shall be activatable from the waypoint keyboard via an **APPR** button. The APPR button shall be visible only when the entered ident matches an airport with one or more runway records loaded.

> **REQ-DISP-PI4-APPR-002** Tapping APPR shall open a runway-end picker listing each end (ident, course, length). Tapping a runway tile shall activate the approach: HITS boxes draw, the VDI paints, the CDI rescales per REQ-DISP-PI4-NAV-010, the magenta direct-to course trace is suppressed, and the airport ident readout transitions to `IDENT/RWY` form.

> **REQ-DISP-PI4-APPR-003** A **CANCEL APPROACH** button shall be present at the bottom of the runway picker when an approach is currently active. Tapping shall clear the approach and revert to plain direct-to to the airport, restoring CDI scaling and the magenta course trace.

> **REQ-DISP-PI4-APPR-004** HITS boxes shall be cyan rectangular polylines drawn along the published 3° glideslope, with the box centre on the glideslope (pilot eye-line through box centre). Default geometry: 300 ft wide × 200 ft tall, spaced 1000 ft apart, starting one spacing-step outside the threshold and continuing to 5 NM final. All values configurable in `pi4/hits.py`.

> **REQ-DISP-PI4-APPR-005** HITS boxes shall be rendered through the same depth-tested polyline pipeline used for the magenta direct-to course trace, so terrain and obstacles correctly occlude boxes that would otherwise paint through intervening ridges.

> **REQ-DISP-PI4-APPR-006** While a synthetic approach is active, the magenta direct-to course trace on the AI shall be hidden. HITS boxes are mutually exclusive with the magenta trace.

> **REQ-DISP-PI4-APPR-007** A Vertical Deviation Indicator (VDI) shall be rendered as a vertical bar with a magenta diamond on the right side of the AI, just inside the altitude tape. The VDI shall paint only when a synthetic approach is active.

> **REQ-DISP-PI4-APPR-008** VDI deviation shall be the elevation-angle error from a 3° glideslope to the threshold:
> `dev_deg = atan2(alt − thresh_elev, dist_ft) − 3°`.
> Full-scale deflection shall be ±0.7° (LPV / ILS convention). The diamond shall move DOWN when the aircraft is above the GS (glideslope below — fly down to the diamond) and UP when below the GS.

> **REQ-DISP-PI4-APPR-009** The moving-map inset's course trace shall switch colour: cyan while a synthetic approach is active (matching HITS / VDI / CDI), magenta otherwise. While approach is active the trace shall be drawn from the threshold along the reciprocal of the published course (the actual extended centreline), NOT from the activation point.

> **REQ-DISP-PI4-APPR-010** The moving-map inset's ETE label shall be coloured cyan in approach mode and magenta in direct-to mode, matching the active trace colour.

---

## 9E. Audio Alerts (EGPWS-style voice callouts)

The Pi 4 display shall provide aviation-style voice callouts coordinated with the visual TAWS / obstacle / attitude alerts.

> **REQ-DISP-PI4-AUD-001** The PFD shall play short, pre-rendered voice callouts through the system audio output for the following conditions, with phrasing chosen to identify the alert source rather than emit a generic tone:
>
> | Trigger | Callout | Band |
> |---------|---------|------|
> | Terrain look-ahead clearance < 500 ft | `Terrain. Terrain.` | Caution |
> | Obstacle in the ±25° forward wedge with clearance < 500 ft | `Obstacle. Obstacle.` | Caution |
> | Descent rate exceeding the AGL-scaled threshold curve (REQ-DISP-PI4-TAWS-009) | `Sink rate. Sink rate.` | Caution |
> | Terrain look-ahead clearance < 100 ft | `Terrain. Terrain. Pull up. Pull up.` | Warning |
> | Obstacle in the ±25° forward wedge with clearance < 100 ft | `Obstacle. Obstacle. Pull up. Pull up.` | Warning |
> | Absolute bank > 60° with AHRS healthy and simulator not paused | `Bank angle. Bank angle.` | Attention |

> **REQ-DISP-PI4-AUD-002** Callout phrases shall be generated once at first run using a text-to-speech engine (default: `espeak`) and cached on disk so subsequent boots can play the clips with no synthesis cost.

> **REQ-DISP-PI4-AUD-003** When multiple alert conditions are satisfied in the same frame, only the highest-priority callout shall be played. Priority order, highest first: obstacle pull-up, terrain pull-up, sink rate, obstacle caution, terrain caution, bank angle.

> **REQ-DISP-PI4-AUD-004** Each callout shall be rate-limited by a minimum repeat interval — warning-band callouts (the `Pull up` variants) at no less than 4 s, caution-band and bank-angle callouts at no less than 3 s. New triggers within the hold-off window shall be silently dropped, not queued.

> **REQ-DISP-PI4-AUD-005** The DISPLAY setup screen shall expose an `ALERT AUDIO` master mute (OFF / ON, default ON) that suppresses all callouts when OFF, and an `ALERT VOLUME` control (1–10) that applies a linear volume multiplier to every loaded callout. Both settings shall persist across power cycles via the settings persistence layer.

> **REQ-DISP-PI4-AUD-006** The PFD shall fire one self-test callout (`Terrain. Terrain.`) at startup when `ALERT AUDIO` is ON, so that speaker continuity and the audio pipeline state can be confirmed without a real alert.

> **REQ-DISP-PI4-AUD-007** Audio initialization shall not block the PFD render loop: if the mixer cannot be opened, the audio module shall log a diagnostic and silently no-op subsequent `play()` calls. The visual alert pipeline shall continue to operate unaffected.

> **REQ-DISP-PI4-AUD-008** The audio module shall force the SDL audio backend to ALSA before pygame imports so that an `~/.asoundrc` redirect to a specific device (e.g. HDMI panel speakers via `plughw:1,0`) is honoured.

---

## 9F. Unusual-Attitude Recovery Cues

When the aircraft enters an extreme attitude, the PFD shall declutter to a minimum legible set and overlay red recovery glyphs centred on the aircraft symbol.

> **REQ-DISP-PI4-UA-001** Unusual-attitude declutter shall trigger when absolute pitch exceeds 30° or absolute roll exceeds 60°. While active, the SVT terrain mesh, water mask, airport / runway / obstacle / direct-to overlays shall all be suppressed, leaving only the sky/ground polygon, the pitch ladder, the aircraft symbol, and the recovery glyphs visible.

> **REQ-DISP-PI4-UA-002** A vertical chevron stack (three filled red chevrons) shall be drawn centred on the aircraft symbol whenever |pitch| > 30°. The stack shall point downward when pitch is nose-high (push to recover) and upward when pitch is nose-low (pull to recover). The midline of the stack shall coincide exactly with the aircraft symbol so the pilot's eye does not need to leave AI centre to read the cue.

> **REQ-DISP-PI4-UA-003** A curved red arrow shall be drawn sweeping over the aircraft symbol whenever |roll| > 60°. The arc shall sweep counter-clockwise when right wing is low (roll left to recover) and clockwise when left wing is low (roll right to recover). The arc radius shall be large enough that the glyph reads as a frame around the pitch chevrons rather than colliding with them, and shall scale with the AI region's short dimension.

> **REQ-DISP-PI4-UA-004** Both glyphs shall be drawable simultaneously when both pitch and roll exceed their respective thresholds.

> **REQ-DISP-PI4-UA-005** The PFD shall correctly express attitudes outside ±90° pitch by re-folding them into the renderer's expected Euler chart (pitch ′ = 180° − pitch; roll ′ = roll + 180°), so that over-the-top loops, split-S manoeuvres, and aerobatic inverted flight remain continuously drawable.

> **REQ-DISP-PI4-UA-006** When the terrain mesh is suppressed (no SRTM tiles, or unusual-attitude declutter), the sky/ground polygon shall be drawn from a roll-aware horizon direction vector so that the brown half of the AI always corresponds to the actual ground side, including past ±90° bank.

---

## 9G. Backlight Control

> **REQ-DISP-PI4-BL-001** The PFD shall provide a brightness control (1–10) on the DISPLAY setup screen, persisted across power cycles, that drives whichever backlight transport is available on the host hardware.

> **REQ-DISP-PI4-BL-002** The PFD shall attempt to control panel brightness through, in order of preference: (a) the `/sys/class/backlight/*/brightness` sysfs node, (b) DDC/CI VCP code 0x10 (luminance) over an i2c-N bus matching a DDC-capable HDMI display, (c) no-op if neither is available.

> **REQ-DISP-PI4-BL-003** DDC/CI writes shall be executed on a background thread under a serialising lock so that brightness adjustments do not block the render loop and concurrent writes do not collide on the I²C bus.

> **REQ-DISP-PI4-BL-004** Diagnostic log lines at startup shall report which backlight transport is in use, or that no transport could be selected.

---

## 9C. AGL Readout

> **REQ-DISP-PI4-AGL-001** A small AGL readout box shall be displayed in the lower-right corner of the attitude indicator, between the altitude tape (right edge) and the heading tape (top edge). Box size: 78 × 42 px on the 1024×600 display. Layout: dim "AGL" label on top line, numeric value (white) on bottom line.

> **REQ-DISP-PI4-AGL-002** AGL value = (real sensor altitude) − (SRTM ground elevation at current lat/lon). The real (unclamped) altitude shall be used, NOT the camera-floor-clamped `alt_render`, so a punched-ground state surfaces as a sanity-check warning rather than being silently clamped to ≥0.

> **REQ-DISP-PI4-AGL-003** When the computed AGL is at or below 0, the readout shall display dashes ("---") in dim grey rather than a misleading negative number. When there is no GPS fix or the SRTM lookup returns the missing-tile sentinel, the readout shall be hidden entirely.

---

## 9a. User Settings Persistence

> **REQ-DISP-PI4-PERSIST-001** User-adjusted settings shall persist across power cycles via a JSON file at `pi4/data/settings.json`. Persisted values shall include: flight profile (V-speeds, tail number, aircraft type, TAS offset), display settings (brightness, baro unit, speed unit, altitude unit, colour scheme), system settings, connectivity settings (Wi-Fi SSID excluding password), airport-data display filters (PUBLIC / HELIPORTS / SEAPLANE / OTHER / RUNWAYS / EXT CENTERLINES), heading bug, altitude bug, and last-used display mode.

> **REQ-DISP-PI4-PERSIST-002** Settings writes shall be debounced and performed on a background thread (default 1.5 s coalesce window) so that rapid consecutive toggle changes produce at most one file write. Writes shall be atomic via `os.replace` from a `.tmp` file so that a power-loss during write cannot corrupt `settings.json`.

> **REQ-DISP-PI4-PERSIST-003** The Wi-Fi password shall not be written to `settings.json`. Any other security-sensitive values explicitly listed in the persistence skip-list shall be excluded similarly.

> **REQ-DISP-PI4-PERSIST-004** On shutdown, pending settings changes shall be flushed synchronously so that no user-visible change is lost on a graceful exit.

---

## 10. Status Badges

> **REQ-DISP-PI4-BADGE-001** Status badges shall appear only when a condition requires pilot attention.

> **REQ-DISP-PI4-BADGE-002** Required badges: `AHRS FAIL` (red), `NO LINK` (red), `NO TER` (amber), `NO OBS` (amber), `EXP OBS` (orange), `NO APT` (amber), `EXP APT` (orange), `GPS TRK` (magenta), `GPS ALT` (amber), `GPS Nsat` (amber), `NO GPS` (red).

---

## 11. Colour Coding — Data Source Convention

> **REQ-DISP-PI4-COLOR-001** GPS-derived values and controls shall be MAGENTA.

> **REQ-DISP-PI4-COLOR-002** Onboard-sensor-derived values and controls shall be CYAN.

> **REQ-DISP-PI4-COLOR-003** The colour convention shall apply to: speed bug/button, altitude bug/button, baro button, heading bug/button, heading box border, and heading source subscript.

---

## 12. Setup and Configuration

> **REQ-DISP-PI4-SETUP-001** The setup menu shall be opened by a two-finger press-and-hold for at least 0.8 seconds.

> **REQ-DISP-PI4-SETUP-002** Sub-menus: Flight Profile, Display, AHRS / Sensors, Connectivity, System.

> **REQ-DISP-PI4-SETUP-003** Factory default V-speeds: VS0=48, VS1=55, VFE=85, VNO=129, VNE=163 (Cessna 172S).

> **REQ-DISP-PI4-SETUP-004** Display units independently selectable for speed (kt/mph/kph), altitude (ft/m), pressure (inHg/hPa).

> **REQ-DISP-PI4-SETUP-005** Backlight brightness adjustable in 10 steps.

> **REQ-DISP-PI4-SETUP-006** Pitch and roll trim shall be independently settable in 0.1° increments via on-screen ± steppers.

> **REQ-DISP-PI4-SETUP-007** AHRS mounting orientation shall be selectable from FORWARD / LEFT / RIGHT / AFT (which side of the AHRS the connector points toward, viewed from the pilot's seat). Default RIGHT. The display shall remap pitch / roll axes and apply a magnetic heading offset such that the displayed values match the aircraft's frame regardless of physical mounting.

> **REQ-DISP-PI4-SETUP-008** AHRS NORMAL / INVERTED mounting shall combine independently with orientation to support upside-down installations.

> **REQ-DISP-PI4-SETUP-009** AHRS firmware reports yaw and roll with an ENU sign convention; the display shall negate both at the base before applying orientation, mounting, and trim transforms so MAG-mode heading reads CW-positive and roll reads right-wing-down-positive.

> **REQ-DISP-PI4-SETUP-010** Sim and Demo modes shall bypass all AHRS-mounting compensations (orientation rotation, mounting flip, base ENU→NED correction, pitch/roll trim, compass cal). Synthetic data is generated in aircraft-frame NED; applying mounting transforms would double-flip values.

> **REQ-DISP-PI4-SETUP-011** A compass-calibration wizard shall be available from the AHRS / Sensors screen. Implementation: cardinal walk-through capturing the (expected − raw) heading delta at NORTH / EAST / SOUTH / WEST. Persistence: four signed deltas stored in `data/settings.json`. Render application: piecewise-linear interpolation between adjacent cardinals so each 90° quadrant has its own correction curve. RESET shall wipe the stored cal back to zero.

> **REQ-DISP-PI4-SETUP-012** The compass cal correction shall apply only to MAG-mode heading. TRK mode shall remain GPS-track-slaved (the complementary filter operates on yaw deltas, where a constant cal correction's frame-to-frame derivative is negligible).

> **REQ-DISP-PI4-SETUP-013** Heading source AUTO mode shall use TRK when ground speed is above ~3 kt, MAG otherwise.

> **REQ-DISP-PI4-SETUP-014** A systemd service (`pfd.service`) shall be installed and enabled by `setup.sh`. The service shall auto-start the PFD on every power-up using `SDL_VIDEODRIVER=kmsdrm` for OpenGL ES compatibility, with `SupplementaryGroups=video render input` for KMS/DRM device access, and `StartLimitIntervalSec=0` to keep retrying after transient crashes. A separate refresh helper (`tools/install_autostart.sh`) shall be available to update just the unit definition without re-running the full installer.

---

## 13. Flight Simulator

> **REQ-DISP-PI4-SIM-001** The display shall include a built-in flight simulator driving all PFD instruments through an internal autopilot model.

> **REQ-DISP-PI4-SIM-002** 12 preset departure airports across the US.

> **REQ-DISP-PI4-SIM-003** Failure injection: GPS, baro, and AHRS failures independently toggleable.

> **REQ-DISP-PI4-SIM-004** A `SIM` watermark shall be displayed during simulator operation; tapping it opens SIM CONTROLS.

> **REQ-DISP-PI4-SIM-005** The autopilot model shall respond to heading, altitude, and speed bug changes in real time.

> **REQ-DISP-PI4-SIM-006** The simulator shall offer two AP follow modes selectable from SIM CONTROLS: **FOLLOW BUGS** (default — pure heading / altitude / speed bug tracker) and **FOLLOW FLT PLAN** (couples the AP to the active direct-to or synthetic approach, overriding the heading bug for course intercept and the altitude bug for glideslope tracking).

> **REQ-DISP-PI4-SIM-007** In FOLLOW FLT PLAN mode the AP shall fly a 45°-max-intercept course-capture profile to the active D2 line (en-route) or the extended runway centreline (approach). Tuning shall switch on approach-active state: gentle band 0.3 → 0.1 NM, full-intercept threshold 1.5 → 0.5 NM, and inner-band gain raised so the AP settles on centreline at the ±0.3 NM CDI scale.

> **REQ-DISP-PI4-SIM-008** In FOLLOW FLT PLAN mode with a synthetic approach active, the AP shall capture the glideslope only from above. While below the GS, the AP shall hold current altitude (it shall NOT command a climb to chase the GS). Approach-mode altitude tracking shall include a feedforward of the GS descent rate (`V × tan(3°)`) and a higher closed-loop gain than en-route altitude hold so the diamond centres without persistent lag.

> **REQ-DISP-PI4-SIM-009** The simulated airplane shall be a coordinated-turn model: bank shall be the only AP-commanded quantity, and yaw rate shall be derived from bank via `ω = g·tan(φ)/V`. Yaw shall not be commanded independently of bank under any condition; zero bank shall produce zero yaw.

> **REQ-DISP-PI4-SIM-010** SIM CONTROLS shall include an EXIT SETUP button so the pilot can leave the SIM CONTROLS overlay without ending the simulator.

---

## 14. Demo Mode

> **REQ-DISP-PI4-DEMO-001** A scripted demo mode shall animate a flight over Sedona, Arizona, driving all instruments without hardware.

> **REQ-DISP-PI4-DEMO-002** Demo mode shall be launchable with the `--demo` command-line flag.

---

## 15. Future Planned Features

The following features are planned for future versions of the Pi 4 display and are not required for the initial release:

- **Texture-mapped terrain** with satellite imagery, USGS terrain textures, or elevation-shaded relief maps
- **Velocity vector / flight-path marker** on the AI — see TODO `FPV` (software-only with current sensors)
- **Computed AOA indexer** on the right side of the AI when no approach is active — see TODOs `AOA-CALC` and `AOA-PROBE`
- **Air-data integration** (IAS / TAS / wind triangle / stall warn) once the SDP31-500Pa ships on the new sensor board — see TODO `SDP31-AIRDATA`
- **GPS-aided AHRS** for centripetal-corrected attitude in coordinated turns (Pico 2 W) — see TODO `AHRS-GPS-AID`

Landed since v0.2 (now in §7, §9D, §13 above): time-of-day sun position, moving-map inset, Highway-In-The-Sky (HITS) rendering with runway picker, vertical glideslope diamond (VDI), approach-mode CDI scaling, sim FOLLOW FLT PLAN with 45° intercept, glideslope capture from above, coordinated-turn AP model.

---

*This document covers the Pi 4 variant with full SVT. For the Pi Zero 2W variant without SVT, see HLR-DISP-ZERO-001.*
