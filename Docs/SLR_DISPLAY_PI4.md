# Display Unit (Pi 4) — Software Level Requirements

| Field          | Value                                             |
|----------------|---------------------------------------------------|
| Document No.   | SLR-DISP-PI4-001                                  |
| Title          | Display Unit (Pi 4) — Software Level Requirements |
| Project        | Pico-AHRS / PFD                                   |
| Date           | 2026-07-08                                        |
| Version        | 0.1                                               |
| Parent (HLR)   | HLR-DISP-PI4-001                                  |
| Architecture   | SAD-DISP-PI4-001                                  |

---

## 1. Introduction

This document defines the low-level Software Level Requirements for the Pi 4 full-SVT
display unit. Each requirement refines one or more high-level requirements from
HLR-DISP-PI4-001 and traces to the specific module and function/symbol that implements
it (per SAD-DISP-PI4-001).

```
HLR-DISP-PI4-001  ─►  SLR-DISP-PI4-001 (this document)  ─►  pi4/ + shared/ source
```

Notation: each requirement is tagged **SLR-DISP-PI4-<AREA>-<n>**, cites its **Parent**
HLR, and gives a **Trace** to `file:symbol`. Function/symbol names are the durable
anchor; line numbers are indicative. Unless a file is named, `pfd.py` means
`pi4/pfd.py`.

---

## 2. Platform and Render Loop

> **SLR-DISP-PI4-PLAT-001** — The application shall render on the KMS/DRM framebuffer
> (`SDL_VIDEODRIVER=kmsdrm`) with no X11/Wayland compositor, selecting resolution and
> layout from the configured `DISPLAY_PROFILE` (roadom_7/roadom_10 at 1024×600, or
> waveshare_35 at 640×480), with all layout constants derived from `DISPLAY_W/H`.
> *Parent:* HLR-DISP-PI4-HW-002, REND-002, HW(§14A). *Trace:* `config.py:_PROFILES`, layout block; `pfd.py` SDL setup.

> **SLR-DISP-PI4-PLAT-002** — The main loop shall sustain a 30 fps target
> (`clock.tick(TARGET_FPS)`, env-overridable) and shall wrap `render()` in an exception
> guard so a draw fault degrades the frame rather than crash-looping the service.
> *Parent:* HLR-DISP-PI4-REND-001. *Trace:* `pfd.py:main()` loop; `config.TARGET_FPS`.

> **SLR-DISP-PI4-PLAT-003** — Analogue fields (roll, pitch, ay, speed, alt, vspeed, ias,
> tas) shall be IIR-smoothed with α = 0.25 per frame, heading with 0/360° wrap; discrete
> fields copied through; `baro_hpa` shall not be smoothed.
> *Parent:* HLR-DISP-PI4-REND-006. *Trace:* `pfd.py:smooth_state()`; `SMOOTH_K`.

> **SLR-DISP-PI4-PLAT-004** — All network and disk I/O and SVT mesh CPU assembly shall run
> on background daemon threads; shared live state shall be accessed under `_state_lock`
> and UI settings (`disp`) mutated only on the main thread.
> *Parent:* HLR-DISP-PI4-REND-001. *Trace:* `pfd.py:main()` thread starts; `_state_lock`.

---

## 3. SVT Rendering

> **SLR-DISP-PI4-SVT-001** — The renderer shall use a hybrid architecture: pygame/SDL
> shall draw all 2D PFD elements onto a transparent surface, and OpenGL ES shall render
> the SVT terrain background into the attitude-indicator viewport; a compositor shall
> upload the 2D surface as a texture and blend it over the terrain each frame.
> *Parent:* HLR-DISP-PI4-REND-002. *Trace:* `svt_composite_gl.py:setup_gl_display`, `Compositor.upload_and_draw`; `svt_renderer_gl.py:render_svt_into_current_fb`.

> **SLR-DISP-PI4-SVT-002** — The default live SVT path shall use OpenGL ES 3.0 via
> `moderngl` on a context shared with pygame's display (not a standalone EGL context), to
> avoid the EGL/KMS conflict on the live Pi; the SVT path shall be selectable via
> `SVT_RENDERER` (`opengl_shared` default, `opengl`, `pygame`) and shall fall back
> automatically to the pygame scanline renderer if the GL path is unavailable.
> *Parent:* HLR-DISP-PI4-REND-003/004. *Trace:* `config.SVT_RENDERER`; `svt_renderer_gl.py:is_available`; `svt_renderer.py:render_svt`.

> **SLR-DISP-PI4-SVT-003** — The terrain background shall be rendered by 3D perspective
> projection from the aircraft position along the heading vector, with a 48° vertical
> field of view matched to the pitch-ladder scale so the SVT horizon, zero-pitch line,
> and pitch ladder align at any pitch angle.
> *Parent:* HLR-DISP-PI4-AI-001, AI-014/015. *Trace:* `svt_renderer_gl.py:_perspective`, `_look_at`, `_attitude_basis`; `V_FOV_DEG`.

> **SLR-DISP-PI4-SVT-004** — The terrain mesh shall be built from SRTM elevation within a
> configurable radius (inner ~20 nm high-detail plus an outer far-LOD tier), with mesh
> CPU assembly on worker threads and GL upload/swap on the main thread, keeping the prior
> mesh visible until swap so rebuild stalls are not seen; rebuilds shall be triggered only
> on sufficient position/altitude change.
> *Parent:* HLR-DISP-PI4-AI-002/003/004. *Trace:* `svt_renderer_gl.py:_build_tier_mesh_cpu`, `_upload_tier_mesh`, `_swap_inner/_swap_outer`.

> **SLR-DISP-PI4-SVT-005** — Terrain features above aircraft altitude shall appear above
> the horizon as peaks/ridges in correct perspective; terrain shall be coloured by a
> six-band clearance palette relative to aircraft altitude (red above/near, through
> dark-brown beyond 2000 ft clearance).
> *Parent:* HLR-DISP-PI4-AI-002/005. *Trace:* `svt_renderer_gl.py` terrain fragment shader `clearance_color`.

> **SLR-DISP-PI4-SVT-006** — A sky gradient (dark at zenith, light at horizon) shall be
> rendered behind the terrain with the sky/ground boundary rotating with roll, and beyond
> the mesh radius the view shall transition to a dusty atmospheric-haze gradient with no
> visible seam.
> *Parent:* HLR-DISP-PI4-AI-007/008. *Trace:* `svt_renderer_gl.py` `SKY_VERTEX_SHADER`/`SKY_FRAGMENT_SHADER`, `_horizon_y_ndc`.

> **SLR-DISP-PI4-SVT-007** — Directional sun-angle Lambertian lighting shall be applied to
> the terrain using time-of-day solar position when a GPS fix is available (fixed defaults
> otherwise), with configurable intensity and ambient level.
> *Parent:* HLR-DISP-PI4-AI-009. *Trace:* `sun.py:solar_position`; `svt_renderer_gl.py` `compute_normal`, sun uniforms.

> **SLR-DISP-PI4-SVT-008** — A cardinal-aligned distance grid (minor every 0.5 nm, major
> every 2 nm) shall be overlaid on the terrain with anti-aliased, screen-space-constant
> line width, contrast-aware colouring over caution/warning terrain, and a fade to
> invisible at the mesh edge.
> *Parent:* HLR-DISP-PI4-AI-010/011/012. *Trace:* `svt_renderer_gl.py` `grid_line`; `GRID_SPACING_NM`.

> **SLR-DISP-PI4-SVT-009** — When SRTM tiles are absent, the attitude indicator shall show
> a plain blue/brown horizon; the sky/ground polygon shall be drawn from a roll-aware
> horizon direction vector so the ground side is correct past ±90° bank.
> *Parent:* HLR-DISP-PI4-AI-006, UA-006. *Trace:* `pfd.py:draw_simple_ai_background`.

---

## 4. Attitude Indicator Instruments

> **SLR-DISP-PI4-AI-001** — A zero-pitch reference line shall be drawn as cyan hash marks
> offset with pitch (10 px/deg) and rotated with roll, and the pitch ladder shall draw
> lines at ±5/10/15/20/30° in GI-275 style with the 0° bar coincident with the zero-pitch
> line.
> *Parent:* HLR-DISP-PI4-AI-013/015. *Trace:* `pfd.py:draw_zero_pitch_line`, `draw_pitch_ladder`.

> **SLR-DISP-PI4-AI-002** — A sky-pointer roll arc with graduated ticks (10/20/30/45/60°)
> and outer doghouse marker shall rotate with the sky; a slip/skid ball shall deflect with
> lateral acceleration `ay`; and a fixed amber delta-wing aircraft symbol shall mark AI
> centre.
> *Parent:* HLR-DISP-PI4-AI-016/017/018. *Trace:* `pfd.py:draw_roll_arc`, `draw_slip_ball`, `draw_aircraft_symbol`.

> **SLR-DISP-PI4-AI-003** — A flight-path-vector marker shall be computed from GPS track
> and flight-path angle (`atan2(vspeed, groundspeed)`), projected into the AI frame,
> hidden below 5 kt groundspeed, clamped to an edge arrow when outside the viewport, and
> toggleable from DISPLAY setup (default ON, persisted).
> *Parent:* HLR-DISP-PI4-FPV-001/002/003. *Trace:* `pfd.py:draw_fpv_marker`.

> **SLR-DISP-PI4-AI-004** — An AGL readout box shall display MSL altitude minus SRTM
> ground elevation using the real (unclamped) altitude, showing dashes at/below 0 AGL and
> hiding when there is no GPS fix or the SRTM lookup is a missing-tile sentinel.
> *Parent:* HLR-DISP-PI4-AGL-001/002/003. *Trace:* `pfd.py:draw_agl_readout`.

> **SLR-DISP-PI4-AI-005** — Unusual-attitude declutter shall trigger above 30° pitch or
> 60° roll, suppressing SVT/water/overlays and drawing red recovery glyphs (pitch chevron
> stack centred on the aircraft symbol, roll-recovery arc), and shall re-fold attitudes
> outside ±90° pitch into the renderer's Euler chart so loops/inverted flight stay
> drawable.
> *Parent:* HLR-DISP-PI4-UA-001..006. *Trace:* `pfd.py:is_extreme_attitude`, `draw_unusual_attitude_arrows`, `normalize_attitude`.

---

## 5. Tapes and Heading

> **SLR-DISP-PI4-SPD-001** — The airspeed tape shall present a Veeder-Root drum readout
> with V-speed colour arcs (white VS0–VFE, green VS1–VNO, yellow VNO–VNE, red radial at
> VNE), numerals coloured by airspeed, a settable speed bug, and cyan (IAS sensor) /
> magenta (GPS GS) source colouring with automatic fallback to GPS when `airdata_ok` is
> false.
> *Parent:* HLR-DISP-PI4-SPD-001..008. *Trace:* `pfd.py:draw_speed_tape`, `_band`, `_rolling_drum`.

> **SLR-DISP-PI4-ALT-001** — The altitude tape shall present a 50 ft drum readout, a
> settable altitude bug (button or tap), a baro/Kollsman button (QNH cyan when barometric,
> `GPS ALT` magenta otherwise) with inHg/hPa numpad entry, and an inner VSI bar scaled to
> ±2000 fpm turning amber beyond ±1500 fpm.
> *Parent:* HLR-DISP-PI4-ALT-001..007. *Trace:* `pfd.py:draw_alt_tape`, `_rolling_drum_alt20`.

> **SLR-DISP-PI4-HDG-001** — The heading tape shall scroll with cardinal/intercardinal
> labels, show a 3-digit heading box with `M`/`G` subscript and magenta border in GPS-TRK
> mode, a source-coloured heading bug, and a cyan GPS-track tick in MAG mode; GPS-TRK mode
> shall slave heading to GPS ground track via a complementary filter (K = 0.05); AUTO mode
> shall use TRK above ~3 kt and MAG otherwise.
> *Parent:* HLR-DISP-PI4-HDG-001..008, SETUP-013. *Trace:* `pfd.py:draw_heading_tape`, `_resolve_hdg_source`, `_update_gps_heading`, `_hdg_ref`.

> **SLR-DISP-PI4-HDG-002** — Displayed headings and courses shall be converted between
> true and magnetic reference using the shared WMM2025 declination module.
> *Parent:* HLR-DISP-PI4-HDG-003. *Trace:* `pfd.py` `_magvar.declination`; `shared/magvar.py`.

---

## 6. Navigation

> **SLR-DISP-PI4-NAV-001** — A direct-to feature shall be reachable from the CDI strip or
> the AIRPORT DATA screen, validating a typed ident against the airport DB, offering a
> confirmation modal (with re-activation of an existing waypoint and a NEAREST
> quick-button), and rejecting unknown idents with an inline error.
> *Parent:* HLR-DISP-PI4-NAV-001..006. *Trace:* `pfd.py:_nav_set_by_ident`, `_nav_lookup_ident`, `_nav_set_nearest`, `draw_nav_confirm`.

> **SLR-DISP-PI4-NAV-002** — A magenta great-circle course trace shall be built
> asynchronously in a background thread, draped over SRTM terrain at a clearance offset
> with rolling-max smoothing, published progressively, and discarded on a mid-build
> waypoint change.
> *Parent:* HLR-DISP-PI4-NAV-007/008. *Trace:* `pfd.py:_build_direct_to_trace_async`, `build_direct_to_trace_vertices`.

> **SLR-DISP-PI4-NAV-003** — A CDI strip shall display ident·bearing·distance and a
> magenta diamond deflecting opposite the course, with full-scale deflection mode-dependent
> (±1.0 nm en-route, ±0.3 nm on an active synthetic approach) and the cross-track
> reference switching to the extended runway centreline on approach.
> *Parent:* HLR-DISP-PI4-NAV-009/010/011/012. *Trace:* `pfd.py:draw_cdi`, `_nav_xtk_nm`.

> **SLR-DISP-PI4-NAV-004** — A synthetic approach shall be activatable from the waypoint
> keyboard (APPR button visible only for airports with runway records), opening a
> runway-end picker; activation shall draw HITS boxes, paint the VDI, rescale the CDI,
> suppress the magenta trace, and switch the ident readout to `IDENT/RWY` form; a CANCEL
> APPROACH control shall revert to plain direct-to.
> *Parent:* HLR-DISP-PI4-APPR-001/002/003/006. *Trace:* `pfd.py:_approach_load`, `_approach_load_published`, `draw_approach_select`.

> **SLR-DISP-PI4-NAV-005** — HITS boxes shall be cyan rectangular polylines along the 3°
> glideslope (default 300×200 ft, spaced ~1000–1500 ft to 5 nm final), rendered through
> the depth-tested polyline pipeline so terrain/obstacles occlude them, and mutually
> exclusive with the magenta direct-to trace.
> *Parent:* HLR-DISP-PI4-APPR-004/005/006. *Trace:* `hits.py:build_box_polylines`, `build_box_polylines_path`; `pfd.py:_approach_hits_refresh`.

> **SLR-DISP-PI4-NAV-006** — A Vertical Deviation Indicator shall paint (only on active
> approach) as a magenta diamond on a vertical bar inside the altitude tape, deviation =
> `atan2(alt − thresh_elev, dist) − 3°`, full-scale ±0.7°, diamond down when above the
> glideslope.
> *Parent:* HLR-DISP-PI4-APPR-007/008. *Trace:* `pfd.py:draw_vdi`.

> **SLR-DISP-PI4-NAV-007** — A flight-plan subsystem shall support activation, automatic
> leg advance, add/remove/swap/reverse, saved plan library, and user waypoints, with the
> library merged across displays via the shared CRDT merge.
> *Parent:* HLR-DISP-PI4-NAV (§9B), SYNC. *Trace:* `pfd.py:_fpl_activate`, `_fpl_check_advance`, `_fpl_plan_save`; `shared/fpllib.py`.

---

## 7. TAWS and Audio Alerting

> **SLR-DISP-PI4-TAWS-001** — Terrain-clearance alerting shall use a look-ahead of at
> least 45 s along the GPS ground track (≥12 samples, altitude projected by current VSI),
> raising a TERRAIN CAUTION banner within 500 ft clearance and a PULL UP / TERRAIN banner
> (red, 1 Hz flash) within 100 ft, using the worst projected clearance.
> *Parent:* HLR-DISP-PI4-TAWS-001/002/006. *Trace:* `pfd.py:_update_terrain_alert`, `draw_terrain_alert`.

> **SLR-DISP-PI4-TAWS-002** — Obstacle proximity alerting shall query the FAA DOF within a
> speed-scaled radius (activating within 3 nm) gated by a ±25° forward track wedge, so
> obstacles abeam or behind the wing do not fire.
> *Parent:* HLR-DISP-PI4-TAWS-003/007. *Trace:* `pfd.py:_alert_radius_nm`, `obstacles.query_nearby`; `OBSTACLE_WEDGE_HALF_DEG`.

> **SLR-DISP-PI4-TAWS-003** — A SINK RATE caution (audio only) shall fire below 2500 ft
> AGL when descent exceeds an AGL-scaled threshold curve (≈1500 fpm at surface rising to
> ≈5000 fpm at 2500 ft AGL), implementing GPWS Mode 1.
> *Parent:* HLR-DISP-PI4-TAWS-009. *Trace:* `pfd.py:_update_terrain_alert` sink-rate block.

> **SLR-DISP-PI4-TAWS-004** — All terrain/obstacle/pull-up alerting shall be inhibited
> below the pilot VS0 ground speed; a synthetic-approach corridor shall auto-inhibit
> terrain/obstacle/pull-up (SINK RATE stays armed); a manual TERRAIN INHIBIT shall mute
> the same set for 120 s with auto-clear; the badge strip shall show `TER INH Xs` (manual)
> or `TER INH APR` (approach).
> *Parent:* HLR-DISP-PI4-TAWS-008/010/011/012/013. *Trace:* `pfd.py:_approach_corridor_inhibit`, `toggle_terrain_inhibit`, `is_terrain_inhibited`, `draw_status_badges`.

> **SLR-DISP-PI4-AUD-001** — The PFD shall play pre-rendered voice callouts (terrain,
> obstacle, sink rate, terrain/obstacle pull-up, bank angle, traffic) generated once via
> espeak and cached as WAVs, selecting only the highest-priority callout per frame
> (obstacle pull-up → terrain pull-up → sink rate → obstacle caution → terrain caution →
> bank angle) and rate-limiting per band (≥4 s warning, ≥3 s caution/bank).
> *Parent:* HLR-DISP-PI4-AUD-001/002/003/004. *Trace:* `audio_alerts.py:_CALLOUTS`, `_generate_wav`, `play`, `_MIN_INTERVAL`; `pfd.py:_update_terrain_alert` priority.

> **SLR-DISP-PI4-AUD-002** — DISPLAY setup shall expose an ALERT AUDIO master mute
> (default ON) and an ALERT VOLUME control (1–10, linear), both persisted; a startup
> self-test callout shall fire when audio is ON; audio-init failure shall degrade to a
> silent no-op without affecting the visual pipeline; and the SDL audio backend shall be
> forced to ALSA.
> *Parent:* HLR-DISP-PI4-AUD-005/006/007/008. *Trace:* `audio_alerts.py:init`, `set_enabled`, `set_volume`; `pfd.py:main()` audio wiring.

> **SLR-DISP-PI4-TFC-001** — ADS-B IN traffic (radio GDL90/UDP or built-in internet feed,
> source-selectable) shall render as threat-tiered diamonds with heading leader and
> relative-altitude tag; alert-class threats shall never be hidden by the declutter
> filters; a new alert-envelope entry shall raise a flashing TRAFFIC banner and a
> rate-limited "Traffic, Traffic" callout gated by ALERT AUDIO.
> *Parent:* HLR-DISP-PI4-TFC-001/002/003. *Trace:* `pfd.py:_update_traffic`, `_draw_pfd_traffic_alert`; `shared/adsb.py` (`threat_level`, `track_threat`), `shared/adsb_feed.py`.

---

## 8. Airports, Runways, Obstacles

> **SLR-DISP-PI4-APT-001** — Airports within a configurable radius shall be projected into
> the 3D AI view with type-encoded symbols and posted "road-sign" identifiers within a
> closer radius, clamped within the AI rectangle and culled outside the AI field of view,
> drawn before obstacles in Z-order; four type filters (PUBLIC/HELIPORTS/SEAPLANE/OTHER)
> plus RUNWAYS and EXT CENTERLINES shall persist across power cycles.
> *Parent:* HLR-DISP-PI4-APT-001..010. *Trace:* `pfd.py:draw_airport_symbols`, `_project_latlon`; `shared/airports.py`.

> **SLR-DISP-PI4-APT-002** — Runway polygons for airports within 8 nm shall be projected
> from both thresholds, scaled by runway width, clipped to the forward half-plane
> (Sutherland-Hodgman), with per-end runway-number labels gated by a forward-distance
> check, extended dashed centrelines within 15 nm, and an airport-environment box
> suppressed close-in.
> *Parent:* HLR-DISP-PI4-APT-011..015. *Trace:* `pfd.py:draw_runway_symbols`, `_draw_extended_centerline`, `_clip_polygon_forward`; `shared/runways.py`.

> **SLR-DISP-PI4-APT-003** — Obstacles shall render from FAA DOF data using real
> (unclamped) altitude for vertical-angle projection, subject to an airport-boundary
> clutter filter, a global minimum-AGL floor, and MSL-top labels.
> *Parent:* HLR-DISP-PI4-APT-016..019. *Trace:* `pfd.py:draw_obstacle_symbols`; `shared/obstacles.py:query_nearby`.

> **SLR-DISP-PI4-APT-004** — Airport, runway, obstacle, and nav databases shall load from
> their source files into NumPy `.npy` caches on first access (filtering closed/positionless
> records), and shall be downloadable from within the UI with record count, disk usage,
> age, and expiry indication.
> *Parent:* HLR-DISP-PI4-APT-006/007/008, TAWS-004/005. *Trace:* `shared/airports.py`, `runways.py`, `obstacles.py`, `navdata.py`; `pfd.py` DATA & MAPS screens.

---

## 9. Moving Map / MFD, Weather, Winds, Screen-Sync

> **SLR-DISP-PI4-MFD-001** — A three-finger ~2 s hold (gated by ENABLE MFD) shall swap
> between the PFD and a full-screen moving-map MFD (booting to the PFD), providing
> direct-to, flight-plan, overlay-cycle, orientation, range/zoom/recenter, pan-by-drag,
> and a configurable 8-slot data strip; the MFD render shall skip the SVT pass.
> *Parent:* HLR-DISP-PI4-MFD-001/002. *Trace:* `pfd.py:draw_mfd`, gesture in `main()`; `moving_map.py:render`, `make_projector`, `zoom_in/out`.

> **SLR-DISP-PI4-WX-001** — The display shall present blended internet + FIS-B weather
> (METAR/TAF/AIRMET/SIGMET/NEXRAD/NOTAM) with a per-family RADIO/AUTO/INET toggle,
> origin+age tagging, a single OVLY overlay cycle (Airspace→Traffic→METAR→Winds→NEXRAD
> with traffic always on), a MET flight-category readout picker (ICAO idents below 160 nm),
> and NEXRAD age/valid-age badging.
> *Parent:* HLR-DISP-PI4-WX-001..005. *Trace:* `pfd.py` `_update_weather`/`_update_nexrad`; `shared/wx.py`, `shared/fisb.py`, `shared/nexrad.py`, `shared/mapoverlay.py`.

> **SLR-DISP-PI4-WX-002** — NOTAMs shall require FAA NMS-API credentials entered on the
> Connectivity screen (NOTAM KEY/SECRET/ENV, preprod default), using a dedicated tight
> zoom-following radius; absent a key the fetch shall be a no-op; fetched NOTAMs and their
> credentials shall be shareable across displays over the screen-sync link with the secret
> transmitted only on entry and shown masked.
> *Parent:* HLR-DISP-PI4-WX-006/007. *Trace:* `pfd.py` NOTAM integration; `shared/wx.py`; `shared/screen_sync.py` NOTAM/creds kinds.

> **SLR-DISP-PI4-WND-001** — Winds/temps aloft shall render as barbs at a selectable
> altitude (3,000–18,000 ft) and forecast time from the GFS `gfs025` Open-Meteo feed,
> cached as a national grid split into disk-persisted zones (aircraft zone first, re-pulled
> on ≥1 h staleness), with its own limited zoom and a grid-fill+age status line, and shared
> across displays over screen-sync.
> *Parent:* HLR-DISP-PI4-WND-001..004. *Trace:* `pfd.py` winds integration; `shared/wx.py` WindsUSCache; `shared/screen_sync.py`.

> **SLR-DISP-PI4-SYNC-001** — Displays on a shared network shall peer-sync over UDP with
> no master, with per-category TX/RX control (bugs, baro, nav, AHRS, GPS, flight-plan
> library) plus always-on winds/NOTAM sharing, an AUTO/USB/NET transport selector, and a
> live peer/links status; conflict resolution shall be last-writer-wins except for the
> multi-packet winds/NOTAM categories.
> *Parent:* HLR-DISP-PI4-SYNC-001. *Trace:* `pfd.py` `ScreenSync` setup, `_ssync_publish_*`/`_ssync_apply_*`; `shared/screen_sync.py`.

> **SLR-DISP-PI4-MAP-001** — The moving-map inset course trace and ETE label shall be cyan
> while a synthetic approach is active (drawn from the threshold along the extended
> centreline) and magenta in direct-to mode.
> *Parent:* HLR-DISP-PI4-APPR-009/010. *Trace:* `pfd.py` inset render; `moving_map.py`.

---

## 10. Setup, Calibration, Persistence, Service

> **SLR-DISP-PI4-SET-001** — The setup menu shall open on a two-finger press-and-hold of
> at least 0.8 s and provide Flight Profile, Display, AHRS/Sensors, Connectivity, Screen
> Sync, System, and Data & Maps sub-menus, with Cessna-172S default V-speeds and
> independently selectable speed/altitude/pressure units.
> *Parent:* HLR-DISP-PI4-SETUP-001/002/003/004. *Trace:* `pfd.py:_SETUP_ITEMS`, `draw_setup_screen`, `setup_hit`.

> **SLR-DISP-PI4-SET-002** — Pitch/roll trim shall be settable in 0.1° steps; AHRS mounting
> orientation (FORWARD/LEFT/RIGHT/AFT, default RIGHT) and NORMAL/INVERTED shall be
> selectable and remap the axes and heading offset; the base ENU→NED sign correction shall
> be applied before orientation/mounting/trim; and Sim/Demo shall bypass all mounting
> compensation.
> *Parent:* HLR-DISP-PI4-SETUP-006..010. *Trace:* `pfd.py:draw_ahrs_setup`, `ahrs_setup_hit`, `normalize_attitude`; `_push_orient_to_pico`.

> **SLR-DISP-PI4-SET-003** — A compass-calibration wizard shall provide a cardinal
> walk-through (four signed N/E/S/W deltas, piecewise-linear applied to MAG-mode heading
> only) and a TUMBLE hard-iron calibration collecting per-axis min/max and solving the
> ellipsoid-centre offset, persisting results and pushing them to the AHRS.
> *Parent:* HLR-DISP-PI4-SETUP-011/012, MAG-001. *Trace:* `pfd.py:draw_ahrs_setup` cal modal; `_push_magcal_to_pico`, `_push_magoff_to_pico`.

> **SLR-DISP-PI4-SET-004** — Panel brightness (1–10, persisted) shall drive the available
> backlight transport in order of preference — sysfs `brightness`, then DDC/CI VCP 0x10 —
> with DDC/CI writes on a serialised background thread and a startup diagnostic naming the
> selected transport.
> *Parent:* HLR-DISP-PI4-BL-001..004. *Trace:* `pfd.py:_ddc_set_brightness`, `DDCBacklight`, `_backlight_lock`.

> **SLR-DISP-PI4-PERSIST-001** — User settings shall persist to `pi4/data/settings.json`
> via a whitelisted, debounced (1.5 s) daemon writer, written atomically (`.tmp` + `fsync`
> + `os.replace` + directory fsync), excluding the Wi-Fi password and runtime diagnostics,
> loaded at startup and flushed synchronously on shutdown.
> *Parent:* HLR-DISP-PI4-PERSIST-001..004. *Trace:* `shared/settings.py:load_into/save_from/mark_dirty/flush`.

> **SLR-DISP-PI4-SET-005** — Status badges shall appear only when attention is required and
> shall implement the HLR badge set with the specified colours; the GPS = magenta /
> onboard-sensor = cyan colour convention shall apply to the speed/altitude/heading bugs and
> buttons, baro button, heading-box border, and source subscript.
> *Parent:* HLR-DISP-PI4-BADGE-001/002, COLOR-001/002/003. *Trace:* `pfd.py:draw_status_badges`; tape/heading draws.

> **SLR-DISP-PI4-SVC-001** — `setup.sh` shall install a `pfd.service` systemd unit that
> auto-starts the PFD under `SDL_VIDEODRIVER=kmsdrm` with `SupplementaryGroups=video render
> input`, `PYTHONPATH` to `shared/`, `Restart=always`, and `StartLimitIntervalSec=0`; a
> `tools/install_autostart.sh` helper shall refresh the unit alone; and a restricted
> sudoers rule shall permit only `systemctl poweroff|reboot`.
> *Parent:* HLR-DISP-PI4-SETUP-014. *Trace:* `pi4/setup.sh`; `tools/install_autostart.sh`.

---

## 11. Simulator and Demo

> **SLR-DISP-PI4-SIM-001** — The simulator shall drive all instruments through an internal
> coordinated-turn autopilot model (bank-only command, `ω = g·tan φ / V`), respond to
> heading/altitude/speed bugs in real time, offer 12 preset departures and independent
> GPS/baro/AHRS failure injection, and show a tappable `SIM` watermark opening SIM CONTROLS
> (with EXIT SETUP).
> *Parent:* HLR-DISP-PI4-SIM-001..005/009/010. *Trace:* `pfd.py:SimFlyState`, `draw_sim_setup`, `draw_sim_controls`; `config_base.SIM_PRESETS`.

> **SLR-DISP-PI4-SIM-002** — SIM CONTROLS shall offer FOLLOW BUGS (default) and FOLLOW FLT
> PLAN modes; in FOLLOW FLT PLAN the AP shall fly a ≤45° intercept to the active direct-to
> line or extended runway centreline (tuning switching on approach-active state) and, on an
> active approach, capture the glideslope only from above with a descent-rate feedforward.
> *Parent:* HLR-DISP-PI4-SIM-006/007/008. *Trace:* `pfd.py:SimFlyState.tick`, `_sim_intercept_heading`.

> **SLR-DISP-PI4-DEMO-001** — A scripted demo mode, launchable with `--demo`, shall animate
> a Sedona-area flight driving all instruments without hardware or network.
> *Parent:* HLR-DISP-PI4-DEMO-001/002. *Trace:* `pfd.py:DemoState`.

---

## 12. Traceability Summary (HLR → SLR)

| HLR section | SLR |
|-------------|-----|
| HW/REND (§2–3, §14A) | PLAT-001..004, SVT-001/002 |
| STALE (§4) | PLAT-003, SET-005 |
| SPD/ALT (§5–6) | SPD-001, ALT-001 |
| AI + SVT (§7) | SVT-001..009, AI-001/002 |
| HDG (§8) | HDG-001/002 |
| TAWS (§9) | TAWS-001..004 |
| APT (§9A) | APT-001..004 |
| NAV/APPR (§9B/9D) | NAV-001..007, MAP-001 |
| AUD (§9E) | AUD-001/002, TFC-001 |
| UA (§9F) | AI-005 |
| BL (§9G) | SET-004 |
| AGL (§9C) | AI-004 |
| PERSIST (§9a) | PERSIST-001 |
| BADGE/COLOR/SETUP (§10–12) | SET-001/002/003/005 |
| SIM/DEMO (§13–14) | SIM-001/002, DEMO-001 |
| FPV (§14B) | AI-003 |
| WX/WND/TFC/MFD/SYNC/MAG (§14C–14H) | WX-001/002, WND-001, TFC-001, MFD-001, SYNC-001, SET-003 |

---

*End of SLR-DISP-PI4-001.*
