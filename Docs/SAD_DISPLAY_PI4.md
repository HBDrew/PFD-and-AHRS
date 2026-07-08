# Display Unit (Pi 4) — Software Architecture Document

| Field          | Value                                              |
|----------------|----------------------------------------------------|
| Document No.   | SAD-DISP-PI4-001                                   |
| Title          | Display Unit (Pi 4) — Software Architecture Document|
| Project        | Pico-AHRS / PFD                                    |
| Date           | 2026-07-08                                         |
| Version        | 0.1                                                |
| Parent (HLR)   | HLR-DISP-PI4-001                                   |
| Child (SLR)    | SLR-DISP-PI4-001                                   |

---

## 1. Introduction

### 1.1 Purpose

This SAD describes the software architecture of the Pi 4 display unit — the
full-Synthetic-Vision-Terrain (SVT) variant of the Primary Flight Display. It
decomposes the software into components, defines the hybrid pygame + OpenGL ES render
pipeline, the threading model, the alerting and navigation subsystems, and the design
decisions that let a Raspberry Pi 4 sustain 30 fps of 3D terrain plus vector
instruments. It is the design-phase bridge between HLR-DISP-PI4-001 and
SLR-DISP-PI4-001.

### 1.2 Scope

Covers `pi4/` (`pfd.py`, the SVT renderer stack, `moving_map.py`, `hits.py`, `sun.py`,
`audio_alerts.py`, `config.py`, `setup.sh`, and the offline/preview helpers) plus the
`shared/` library modules the unit imports. The Pi Zero 2W variant is covered by
SAD-DISP-ZERO-001; the AHRS data producer by SAD-AHRS-001.

### 1.3 Reference Documents

| Ref | Document |
|-----|----------|
| [HLR] | `Docs/REQUIREMENTS_DISPLAY_PI4.md` — HLR-DISP-PI4-001 |
| [SLR] | `Docs/SLR_DISPLAY_PI4.md` — SLR-DISP-PI4-001 |
| [ZERO]| `Docs/SAD_DISPLAY_ZERO.md` — reduced no-SVT variant |
| [AHRS]| `Docs/SAD_AHRS.md` — the data producer |

### 1.4 Target Platform

Raspberry Pi 4 (≥2 GB) or Pi 5, ROADOM 7"/10" 1024×600 HDMI (or Waveshare 3.5" 640×480
DPI). Rendering is a hybrid of pygame/SDL (2D UI) and OpenGL ES 3.0 via `moderngl`
(3D terrain), on the KMS/DRM framebuffer (`SDL_VIDEODRIVER=kmsdrm`) with no
X11/Wayland compositor.

---

## 2. Architectural Overview

Like the Pi Zero variant, the Pi 4 application is one large module (`pi4/pfd.py`,
~19.4k lines) around a single 30 fps render loop, fed by daemon threads and the
`shared/` library. The defining difference is the **hybrid render architecture**:
OpenGL ES draws the SVT terrain background into the default framebuffer, and pygame
draws every 2D instrument onto a transparent surface that a small `moderngl`
compositor uploads and blends on top each frame.

```
                     ┌───────────────── daemon threads ──────────────────┐
  AHRS (Pico W)  ──► Serial/SSE client ─┐                                 │
  ADS-B / GDL90  ──► ADSBClient/Feed ────┤                                │
  Weather/NEXRAD ──► Wx/Awc/Nexrad ──────┤  merge into state / caches      │
  Winds / NOTAM  ──► WindsUSCache/… ─────┤                                 │
  Peers (UDP)    ──► ScreenSync ─────────┤                                 │
  SVT mesh CPU   ──► inner/outer mesh ────┤ (async build, main-thread swap) │
  settings.json  ◄── SettingsWriter ─────┘                                 │
                     └───────────────────────────────────────────────────┘
                                       │ (read under _state_lock)
                                       ▼
  ┌────────────────────── pfd.py main() loop @30 fps ───────────────────────┐
  │ demo/sim tick → smooth_state (α=0.25) → _update_traffic/weather/nexrad   │
  │ → screen-sync publish → event/gesture handling → render(surf)            │
  │   render(): full-screen setup/MFD OR:                                    │
  │     SVT terrain → default framebuffer (render_svt_into_current_fb)       │
  │     2D instruments/overlays → transparent surf                           │
  │   _flip(): Compositor.upload_and_draw(surf) → display.flip()             │
  └─────────────────────────────────────────────────────────────────────────┘
```

State lives in `state` (live flight data, guarded by `_state_lock`) and `disp`
(UI/pilot settings, main-thread owned). The render loop is wrapped in try/except so a
draw bug degrades rather than crash-loops the service.

---

## 3. Component Decomposition

### 3.1 `pfd.py` (~19,434 lines) — the application

| Block | Anchor symbols |
|-------|----------------|
| State + lock | `state`, `disp`, `_state_lock` |
| Data ingest / diagnostics | `SerialClient`/`SSEClient` setup, `_restart_sse`, `_poll_wifi_status`, `_poll_ahrs_diag` |
| Smoothing | `smooth_state()` (`SMOOTH_K = 0.25`) |
| Main loop | `main()` while-loop, `_flip()`, `clock.tick(TARGET_FPS)` |
| Render dispatch | `render()` (full-screen setup/MFD early-return, else SVT + instrument stack) |
| Attitude / SVT bridge | `normalize_attitude`, `draw_ai_background`, `draw_simple_ai_background`, `get_svt_surface` |
| Instruments | `draw_speed_tape`, `draw_alt_tape`, `draw_pitch_ladder`, `draw_roll_arc`, `draw_aircraft_symbol`, `draw_slip_ball`, `draw_heading_tape`, `draw_zero_pitch_line`, `draw_agl_readout` |
| Nav overlays | `draw_cdi`, `draw_vdi`, `draw_fpv_marker`, `draw_direct_to_trace`, `draw_airport_symbols`, `draw_runway_symbols`, `draw_obstacle_symbols` |
| Unusual attitude | `is_extreme_attitude`, `draw_unusual_attitude_arrows` |
| TAWS | `_update_terrain_alert`, `_alert_radius_nm`, `_approach_corridor_inhibit`, `toggle_terrain_inhibit`, `draw_terrain_alert` |
| Traffic | `_update_traffic`, `_draw_pfd_traffic_alert` |
| Status | `draw_status_badges`, `draw_failure_overlays`, `draw_pfd_top_strip`, `draw_tap_buttons` |
| Setup menus | `_SETUP_ITEMS`, `draw_setup_screen`, `setup_hit`, per-screen `draw_*`/`*_hit` |
| Overlays | `_open_numpad`, `draw_numpad`, `draw_keyboard`, `_draw_modal_overlays`, `_draw_veil` |
| Navigation | `_nav_set_by_ident`, `_build_direct_to_trace_async`, `_fpl_*`, `_approach_*` |
| Simulator / demo | `SimFlyState`, `DemoState` |
| MFD | `draw_mfd` → `moving_map.render` |
| Pico push workers | `_push_*_to_pico` (baro, magcal, magoff, align, orient) |

### 3.2 SVT renderer stack

| Module | Role |
|--------|------|
| `svt_renderer_gl.py` (~1,523) | OpenGL ES SVT terrain: mesh build, perspective projection, sky/haze/terrain shaders, distance grid, depth-tested polylines. Provides both the shared-context entry `render_svt_into_current_fb` and the standalone-EGL `render_svt_gl`. |
| `svt_composite_gl.py` (~240) | pygame `OPENGL` + `moderngl` shared-context setup (`setup_gl_display`) and the `Compositor` that uploads the 2D pygame surface as a texture and blends it over the terrain. |
| `svt_renderer.py` (~293) | Legacy pygame scanline SVT fallback (`render_svt`, numpy and pure-python paths). |
| `sun.py` (~79) | NOAA solar-position model driving terrain sun lighting. |

### 3.3 Other `pi4/` modules

| Module | Role |
|--------|------|
| `moving_map.py` (~2,061) | MFD/inset moving map: async terrain tint, TAWS overlay, airspaces/symbols, NEXRAD, METAR/winds/traffic, projector and zoom. |
| `hits.py` (~164) | Highway-In-The-Sky box polyline generation (fixed-3° and published-approach paths). |
| `audio_alerts.py` (~251) | espeak→WAV callout cache played through `pygame.mixer`, with rate limiting, priority, master mute and volume. |
| `config.py` (~185) | Display profiles, dynamic layout, `SVT_RENDERER` selection, data dirs; `from config_base import *`. |
| `render_pfd_offline.py`, `render_preview_gl.py`, `build_srtm3.py` | Offline preview/screenshot and SRTM3 pre-decimation tools (not part of the live service). |

### 3.4 `shared/` library (imported)

The same set as the Pi Zero variant — `sse_client`, `serial_client`, `adsb`,
`adsb_feed`, `gdl90` (transitive), `fisb`, `wx`, `nexrad`, `wxloop`, `terrain`,
`water`, `obstacles`, `airports`, `runways`, `airspaces`, `navdata`, `magvar`,
`localtime`, `mapoverlay`, `screen_sync`, `fpllib`, `settings`, `perf`, `config_base`
— plus a heavier reliance on `terrain`/`water` for the 3D SVT mesh.

---

## 4. Render Pipeline (per frame)

`render()` executes, when not showing a full-screen setup/MFD screen:

1. **Clear** the transparent overlay surface and the default framebuffer.
2. **Resolve inputs:** `normalize_attitude`, heading source (`_resolve_hdg_source`,
   `_update_gps_heading`), airspeed source, unit conversion.
3. **Alerting/sequencing:** `_update_terrain_alert`, FPL/approach auto-sequence.
4. **SVT background** (into the default framebuffer): camera-floor clamp of altitude,
   sun position, TAWS-aware below-horizon colour, then one of — extreme-attitude simple
   background; shared-GL `render_svt_into_current_fb` (default live path); standalone-GL
   `draw_ai_background`; or the pygame simple background.
5. **3D-projected overlays** onto the surface: runways, airports, obstacles, direct-to
   trace, FPV marker, zero-pitch line, approach signposts, moving-map inset.
6. **2D instruments:** pitch ladder, unusual-attitude glyphs, speed tape, altitude tape,
   AGL box, heading tape, CDI, VDI, roll arc, aircraft symbol, slip ball.
7. **Chrome:** top strip, status badges, terrain banner, traffic alert, failure
   overlays, tap buttons, DEMO/SIM watermark, modal overlays.
8. **Present:** `_flip()` → `Compositor.upload_and_draw(surf)` → `display.flip()`.

---

## 5. SVT Rendering Architecture

### 5.1 Three selectable renderers

`config.SVT_RENDERER` selects `"opengl_shared"` (default, live Pi 4), `"opengl"`
(standalone EGL, offline/preview), or `"pygame"` (scanline fallback). Runtime failure
falls back automatically to the pygame path.

### 5.2 Shared-context path (default)

`setup_gl_display()` gives pygame ownership of the GL display (`OPENGL|DOUBLEBUF|
FULLSCREEN`, GLES 3.0, 24-bit depth) and attaches a `moderngl` context to it — this
avoids the EGL/KMS conflict that a standalone context would cause on the live Pi. Each
frame, `render_svt_into_current_fb` draws terrain into the default framebuffer's AI
viewport; the 2D PFD is drawn to a transparent `SRCALPHA` surface; and
`Compositor.upload_and_draw` uploads that surface as a texture and blends it over the
terrain with a fullscreen quad (supporting a `rotate_deg` for physically-rotated
panels).

### 5.3 Terrain mesh and projection

- **Two-tier mesh:** an inner high-detail mesh (20 nm radius, 300×300 grid) plus an
  outer far-LOD silhouette mesh (75 nm, 60×60). CPU assembly runs on worker threads
  (`_build_tier_mesh_cpu`); GL upload and swap happen on the main thread, and the old
  mesh keeps rendering until the swap so the 100–500 ms rebuild stall is hidden.
- **Elevation/water:** vertices come from SRTM (`terrain.load_tile`) with a per-vertex
  water flag from the Natural Earth mask (`water.load_tile`), an airport "land burn"
  over water, and a sub-sea clamp.
- **Projection:** perspective with a 48° vertical FOV (deliberately matched to the
  pitch-ladder `px_per_deg` so the SVT horizon, zero-pitch line, and ladder align),
  near plane 50 m, far plane 75 nm×1.5; camera basis from aircraft pitch/roll/heading;
  world frame X=East, Y=North, Z=Up.

### 5.4 Shaders

- **Sky/haze:** a fullscreen quad at maximum depth; horizon-blue→zenith gradient above
  a roll-aware horizon line; below-horizon blends into a TAWS-aware ground colour.
- **Terrain:** a six-band clearance palette (red < 200 ft clearance … very dark
  > 2200 ft) gated by the ground-speed inhibit, water colouring, flat-shaded Lambertian
  sun lighting via screen-space-derivative normals, and an anti-aliased cyan distance
  grid (minor 0.5 nm, major every 2 nm) that fades at the mesh edge.
- **Sun:** `sun.solar_position(lat, lon)` supplies azimuth/elevation/intensity; when
  there is no GPS fix, fixed defaults are used.

### 5.5 Depth-tested polylines

Direct-to trace, next-leg trace, HITS boxes, approach trace, and airport signposts are
drawn after terrain with depth testing, so intervening ridges correctly occlude them.
They are batched by colour/width into single `GL_LINES` draw calls.

---

## 6. Instruments and Nav Overlays

All instruments are drawn in `pfd.py` on the 2D surface. Beyond the tapes/AI/heading
common to both variants, the Pi 4 adds: the CDI (±1 nm en-route / ±0.3 nm approach,
extended-centreline reference on approach), the VDI (±0.7° glideslope), HITS boxes
(`hits.py`), the flight-path vector, the AGL readout, and the unusual-attitude recovery
glyphs (pitch chevrons and roll-recovery arc, triggered above 30° pitch / 60° bank with
SVT/symbol declutter). Airports, runways (with forward-clip and extended centrelines),
and obstacles are projected into the same 3D frame as the SVT.

Navigation comprises direct-to (`_nav_*`, async course-trace builder), a full
flight-plan subsystem (`_fpl_*`, saved plans, sync), and synthetic approaches
(`_approach_*`: procedure/transition pickers, glideslope guidance, HITS/VDI/CDI
coordination, missed-approach sequencing) built on `navdata` and `fpllib`.

---

## 7. Alerting Architecture

### 7.1 TAWS (`_update_terrain_alert`)

Runs once per frame, producing levels 0/1/2:

- **Terrain look-ahead:** 45 s along the GPS ground track, 12 samples, altitude
  projected forward by current VSI; worst clearance vs 700 ft caution / 200 ft warning.
- **Sink-rate (GPWS Mode 1):** below 2500 ft AGL when descent exceeds an AGL-scaled
  threshold curve (audio only).
- **Obstacle look-ahead:** DOF query within a speed-scaled radius (1–3 nm) restricted to
  a ±25° forward wedge, vs 500 ft caution / 200 ft warning.
- **Inhibits:** below-VS0 ground inhibit; approach-corridor auto-inhibit
  (`_approach_corridor_inhibit`); 120 s manual terrain inhibit (`toggle_terrain_inhibit`).
  Both terrain/obstacle/pull-up mute while sink-rate stays armed.
- **Priority:** obstacle pull-up → terrain pull-up → sink-rate → obstacle → terrain;
  bank-angle handled separately.

### 7.2 Audio (`audio_alerts.py`)

espeak generates each callout to a cached WAV on first run; subsequent boots load the
WAVs into `pygame.mixer`. `play()` is rate-limited per-alert (3–5 s) and safe to call
every frame; master mute and 0–1 volume are wired to the DISPLAY settings; the SDL audio
backend is forced to ALSA. Audio init failure degrades to a silent no-op without
affecting the visual pipeline.

---

## 8. Threading Model

Main thread owns pygame, GL, `disp`, and rendering. Daemon threads:

| Thread(s) | Role |
|-----------|------|
| Serial/SSE client | AHRS ingest |
| `ADSBClient`, `TrafficFeed` | traffic |
| `WxClient`, 3× `AwcPoller` (TAF/AIRMET-SIGMET/NOTAM), `WindsUSCache`, `NexradClient`, `RadarLoop` | weather/winds |
| `ScreenSync` | UDP peer sync |
| `SettingsWriter` | debounced atomic persistence |
| `WiFiPoll`, `AhrsDiag` | diagnostics |
| Startup DB loaders | obstacles/airports/airspace/navdata caches |
| SVT inner/outer mesh workers | async CPU mesh assembly |
| `DDCBacklight` | serialised DDC/CI or sysfs brightness writes |
| Trace builders | direct-to / approach / next-leg course traces |
| Pico push workers | baro/magcal/magoff/align/orient over the AHRS link |
| Download threads | terrain/water/obstacle/airport/navdata/airspace |

`state` is guarded by `_state_lock`; `disp` is mutated only on the main thread.

---

## 9. Configuration and Persistence

- **`config.py`:** `DISPLAY_PROFILE` (`roadom_7`/`roadom_10`/`waveshare_35`) derives
  `DISPLAY_W/H` and all layout constants; `TARGET_FPS` (env-overridable, default 30);
  `SVT_RENDERER` (default `opengl_shared`); data directories. SVT-GL tuning constants
  live in `svt_renderer_gl.py`. Shared defaults (V-speeds, TAWS thresholds, demo coords,
  gesture timings, `SSE_URL`) come from `config_base.py`. Runtime feature toggles live in
  `disp["ds"]` and persist via settings.
- **Persistence (`shared/settings.py`):** whitelisted subtrees of `disp` written to
  `pi4/data/settings.json` by a debounced (1.5 s) daemon writer, atomically (`.tmp` +
  `fsync` + `os.replace` + directory fsync), with a skip-list excluding the Wi-Fi
  password and runtime diagnostics; loaded at startup and flushed on shutdown.

---

## 10. Service Installation

`pi4/setup.sh` installs `/etc/systemd/system/pfd.service`: `SDL_VIDEODRIVER=kmsdrm`
(mandatory for Mesa GL ES — fbcon cannot drive a GL context), `SupplementaryGroups=
video render input`, `PYTHONPATH` to `shared/`, `Restart=always`,
`StartLimitIntervalSec=0` (survive boot-time crash loops). A `tools/install_autostart.sh`
helper refreshes just the unit. A restricted sudoers drop-in grants only
`systemctl poweroff|reboot` for the SYSTEM-screen power controls.

---

## 11. Key Design Decisions & Rationale

| Decision | Rationale |
|----------|-----------|
| Hybrid pygame + shared-context moderngl | 3D terrain needs GL; the mature 2D instrument code stays in pygame; one compositor blends them at 30 fps. |
| Shared GL context (not standalone EGL) on live Pi | Avoids the EGL/KMS conflict that locks the GPU against the display; standalone EGL kept only for offline previews. |
| Two-tier async mesh with deferred swap | Hides the 100–500 ms mesh-rebuild stall; distant terrain stays visible via the cheap outer tier. |
| 48° vertical FOV matched to pitch scale | Keeps SVT horizon, zero-pitch line, and pitch ladder in registration at every pitch angle. |
| Cached WAV callouts | One-time espeak synthesis; zero per-frame TTS cost; frame-safe rate-limited playback. |
| All I/O + mesh CPU off-thread | Protects the 30 fps render budget; single `_state_lock` on shared state. |
| try/except around `render()` | A draw bug degrades the frame rather than crash-looping the service. |

---

## 12. Traceability

Low-level requirements in SLR-DISP-PI4-001 trace each behaviour to the anchor symbols
above. HLR section → component mapping (abbreviated; see the SLR for the full matrix):

| HLR section | Components |
|-------------|-----------|
| §2–3 hardware/rendering | `main()`, `config.py`, `svt_composite_gl`, `_flip()` |
| §4 stale detection | AHRS ingest, `smooth_state`, badges |
| §5–6 tapes/VSI | `draw_speed_tape`, `draw_alt_tape` |
| §7 AI + SVT | `svt_renderer_gl`, `sun.py`, `draw_pitch_ladder`, `draw_zero_pitch_line`, `draw_roll_arc`, `draw_slip_ball` |
| §8 heading | `draw_heading_tape`, `_resolve_hdg_source`, `magvar` |
| §9 TAWS | `_update_terrain_alert`, inhibits, `audio_alerts` |
| §9A airports/runways | `draw_airport_symbols`, `draw_runway_symbols`, `airports`/`runways` |
| §9B–9D nav/approach | `_nav_*`, `_fpl_*`, `_approach_*`, `hits.py`, `draw_cdi`, `draw_vdi` |
| §9E audio | `audio_alerts.py` |
| §9F unusual attitude | `draw_unusual_attitude_arrows`, `is_extreme_attitude` |
| §9G backlight | `DDCBacklight`, `_ddc_set_brightness` |
| §9C AGL | `draw_agl_readout` |
| §9a persistence | `shared/settings.py` |
| §10–12 badges/colour/setup | `draw_status_badges`, setup dispatch |
| §13–14 sim/demo/profiles/FPV/wx/winds/tfc/mfd/sync/mag | `SimFlyState`, `DemoState`, `draw_fpv_marker`, `shared/wx/fisb/nexrad/adsb/screen_sync`, `moving_map.py`, mag-cal |

---

*End of SAD-DISP-PI4-001.*
