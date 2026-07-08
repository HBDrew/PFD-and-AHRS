# Display Unit (Pi Zero 2W) — Software Architecture Document

| Field          | Value                                                    |
|----------------|----------------------------------------------------------|
| Document No.   | SAD-DISP-ZERO-001                                        |
| Title          | Display Unit (Pi Zero 2W) — Software Architecture Document|
| Project        | Pico-AHRS / PFD                                          |
| Date           | 2026-07-08                                               |
| Version        | 0.1                                                      |
| Parent (HLR)   | HLR-DISP-ZERO-001                                        |
| Child (SLR)    | SLR-DISP-ZERO-001                                        |

---

## 1. Introduction

### 1.1 Purpose

This SAD describes the software architecture of the Pi Zero 2W display unit — the
lightweight, no-SVT variant of the pilot-facing Primary Flight Display. It decomposes
the software into components, defines the render pipeline and threading model, and
records the design decisions that keep the application inside the Pi Zero 2W's memory
and GPU budget while sharing the bulk of its logic with the Pi 4 variant.

### 1.2 Scope

Covers `pi_zero/` (`pfd.py`, `moving_map.py`, `config.py`, `setup.sh`,
`setup_usb_sync.sh`) plus the `shared/` library modules the unit imports. The Pi 4
full-SVT variant is covered by SAD-DISP-PI4-001; the AHRS data producer by
SAD-AHRS-001.

### 1.3 Reference Documents

| Ref | Document |
|-----|----------|
| [HLR] | `Docs/REQUIREMENTS_DISPLAY_ZERO.md` — HLR-DISP-ZERO-001 |
| [SLR] | `Docs/SLR_DISPLAY_ZERO.md` — SLR-DISP-ZERO-001 |
| [PI4] | `Docs/SAD_DISPLAY_PI4.md` — SAD-DISP-PI4-001 (superset variant) |
| [AHRS]| `Docs/SAD_AHRS.md` — the data producer |

### 1.4 Target Platform

Raspberry Pi Zero 2W, Waveshare 3.5" 640×480 DPI LCD (parallel RGB over the 40-pin
header, I2C capacitive touch, PWM backlight on GPIO 18). Rendering is pygame writing to
the KMS/DRM framebuffer (`SDL_VIDEODRIVER=kmsdrm`) with no X11/Wayland compositor.

---

## 2. Architectural Overview

The application is a **single monolithic module**, `pi_zero/pfd.py` (~16.5k lines),
around one main render loop, supported by a set of daemon threads that ingest data and
persist settings, plus the `shared/` library for all data formats and clients, and
`moving_map.py` for the MFD view.

```
                      ┌──────────────── daemon threads ─────────────────┐
   AHRS (Pico W)  ──► SerialClient / SSEClient ─┐                       │
   ADS-B UDP/net  ──► ADSBClient / TrafficFeed ─┤                       │
   Weather/NEXRAD ──► WxClient/…/NexradClient ──┤   merge into           │
   Peers (UDP)    ──► ScreenSync ───────────────┤   state / disp / caches│
                                                 │                       │
   settings.json  ◄── SettingsWriter (debounced)─┘                       │
                      └─────────────────────────────────────────────────┘
                                        │  (read under _state_lock)
                                        ▼
   ┌──────────────────────── pfd.py main() loop @30 fps ──────────────────────┐
   │  handle_event() ──► gesture/touch state machines                          │
   │  smooth_state()  ──► IIR α=0.25 on analogue fields                        │
   │  render()        ──► setup/MFD full-screen OR the PFD instrument stack     │
   │  _update_terrain_alert() ──► TAWS banners (visual only)                    │
   │  _flip()         ──► present to /dev/fb (kmsdrm, pygame.SCALED)            │
   └──────────────────────────────────────────────────────────────────────────┘
```

State lives in two module-level dicts: `state` (live flight state from the AHRS/feeds,
guarded by `_state_lock` because worker threads write it) and `disp` (UI/pilot
settings, mutated on the main thread and read by workers). `render()` never blocks on
I/O — all network and disk work is off-thread.

---

## 3. Component Decomposition

### 3.1 `pfd.py` (~16,510 lines) — the application

Key blocks and their anchor symbols:

| Block | Anchor symbols |
|-------|----------------|
| State + settings dicts, lock | `state`, `disp`, `_state_lock` |
| Data ingest / screen-sync apply | `_ssync_apply_*`, `_ssync_publish_*`, `_poll_ahrs_diag` |
| Smoothing | `smooth_state()` (`SMOOTH_K = 0.25`) |
| Main loop | `main()` (while-loop, `clock.tick(TARGET_FPS)`, `_flip()`) |
| Event/gesture handling | `handle_event()`, multi-finger tracking, `LONG_PRESS_MS`, `MFD_SWAP_HOLD_MS` |
| Render dispatch | `render()` (full-screen setup/MFD early-return, else PFD stack) |
| Attitude indicator | `draw_simple_ai_background()`, `draw_above_horizon_terrain()`, `draw_pitch_ladder()`, `draw_roll_arc()`, `draw_slip_ball()`, `draw_aircraft_symbol()` |
| Tapes | `draw_speed_tape()`, `draw_alt_tape()`, `draw_heading_tape()`, `_rolling_drum*()` |
| Heading source | `_resolve_hdg_source()`, `_update_gps_heading()`, `_hdg_ref()` |
| Airports / obstacles | `draw_airport_symbols()`, `draw_obstacle_symbols()` |
| TAWS | `_update_terrain_alert()`, `_alert_radius_nm()`, `draw_terrain_alert()` |
| Traffic | `_update_traffic()`, `_draw_pfd_traffic_alert()` |
| Status / failures | `draw_status_badges()`, `draw_failure_overlays()`, `draw_red_x()`, `draw_pfd_top_strip()` |
| Setup menus | `draw_setup_screen()`, `setup_hit()`, per-screen `draw_*_setup` / `*_hit` (~40 screens) |
| Overlays | `draw_numpad()`, `draw_keyboard()`, `_draw_modal_overlays()`, `_draw_veil()` |
| Magnetometer cal | `_mag_cal_capture()`, `_mag_cal_tumble_toggle()`, `_mag_cal_tumble_tick()`, `_push_magoff_tumble()` |
| Simulator | `SimFlyState`, `draw_sim_setup()`, `draw_sim_controls()` |
| Demo | `DemoState` |
| MFD | `draw_mfd()` → `moving_map.render()` |
| Backlight | `_init_backlight()`, `_set_backlight()` (PWM GPIO 18) |

### 3.2 `moving_map.py` (~1,960 lines) — MFD renderer

Terrain-tint moving map with NEXRAD, METAR/winds/traffic overlays, projector and zoom
helpers. Active only in the MFD view. Uses `terrain`, `water`, and `airspaces`.

### 3.3 `config.py` (93 lines) — platform config

640×480 DPI panel geometry, layout constants (`SPD_W`, `ALT_W`, `CX`, `CY`, `ROLL_R`,
AI region), data-directory paths, `TARGET_FPS = 30`, and `from config_base import *`
for the shared thresholds and defaults. Optional `config_local` override.

### 3.4 `shared/` library (imported)

`sse_client`, `serial_client`, `adsb`, `adsb_feed`, `gdl90` (transitively), `fisb`,
`wx`, `nexrad`, `wxloop`, `terrain`, `water`, `obstacles`, `airports`, `runways`,
`airspaces`, `navdata`, `magvar`, `localtime`, `mapoverlay`, `screen_sync`, `fpllib`,
`settings`, `perf`, `config_base`. See SAD-AHRS-001 §1 note and the shared-library
inventory for their responsibilities.

### 3.5 `setup.sh` / `setup_usb_sync.sh`

`setup.sh` installs apt/pip deps, the Waveshare DPI + PWM-backlight boot config, the
`pfd.service` systemd unit (with a root `ExecStartPre` that arms the PWM backlight),
data directories, and a restricted power sudoers rule. `setup_usb_sync.sh` configures
the Zero as a USB-ethernet gadget (`dwc2`/`g_ether`, `usb0` = 10.55.0.2) so screen-sync
can run over the USB cable to a Pi 4 at 10.55.0.1.

---

## 4. Runtime / Threading Model

The main thread owns pygame, `disp`, and all rendering. All I/O is on daemon threads,
each merging results into `state` (under `_state_lock`) or into module caches:

| Thread(s) | Role |
|-----------|------|
| `SerialClient` (primary) / `SSEClient` (fallback) | AHRS ingest |
| `ADSBClient`, `TrafficFeed` | ADS-B/GDL90 + internet traffic |
| `WxClient`, TAF/AIRMET/SIGMET/NOTAM pollers, `WindsUSCache`, `NexradClient` | internet weather |
| `ScreenSync` | UDP peer sync |
| `SettingsWriter` | debounced atomic settings persistence |
| Startup DB loaders (obstacles/airports/airspace/navdata) | one-shot cache builds |
| Ad-hoc `_worker()` threads | downloads, firmware push/flash, Wi-Fi apply, baro/mag push-to-Pico |

The AHRS ingest prefers the wired USB `$AHRS` serial link and falls back to the Wi-Fi
SSE stream.

---

## 5. Rendering Architecture

- **Framebuffer, no compositor:** pygame with `SDL_VIDEODRIVER=kmsdrm`; fullscreen uses
  `pygame.SCALED` for SDL2 C-side logical scaling; target 30 fps via `clock.tick`.
- **Plain-horizon attitude indicator:** the AI background is a solid sky/ground split
  drawn by `draw_simple_ai_background()` (< 1 ms). There is deliberately **no SVT mesh**.
  A lightweight above-horizon SRTM *silhouette* (`draw_above_horizon_terrain()`) may be
  drawn as a mountain profile, but it is not a textured/coloured SVT background and does
  not tint the AI.
- **SRTM used for TAWS only:** terrain elevation is consumed solely for proximity
  alerting; `terrain.set_resolution_preference("srtm3")` forces the smaller SRTM3 tiles
  to fit the ~512 MB RAM budget.
- **Text-surface LRU cache** and other memory-conservation measures keep the frame
  budget and RAM within the Pi Zero 2W envelope.

---

## 6. TAWS Architecture (visual-only)

`_update_terrain_alert()` runs every frame. Terrain clearance is evaluated from a
**single spot elevation sample at the current aircraft position** (not the Pi 4's
forward-projected look-ahead), compared against caution/warning floors. Obstacles are
queried within a speed-scaled radius (clamped 1–3 nm) and filtered to a ±25° forward
track wedge. Alerts are inhibited below the pilot's VS0 stall speed and when GPS is
invalid. There is **no audio pipeline** on the Pi Zero (`SDL_AUDIODRIVER=dummy`); all
alerting — including traffic — is visual (banners/badges) only.

---

## 7. Settings Persistence

`shared/settings.py` persists a whitelisted subset of `disp` to
`pi_zero/data/settings.json`: debounced (`mark_dirty()` → 1.5 s coalesce on the
`SettingsWriter` daemon), atomic (`.tmp` + `fsync` + `os.replace` + directory fsync),
with a skip-list that excludes the Wi-Fi password, download state, and runtime mag-cal.
`load_into()` runs once at startup with legacy-value migration; `flush()` runs on
graceful shutdown.

---

## 8. Relationship to the Pi 4 Variant

The two displays share the `shared/` library and the same instrument/menu/sim/demo
design. The Pi Zero variant deliberately differs:

| Aspect | Pi Zero 2W | Pi 4 |
|--------|-----------|------|
| Attitude-indicator background | plain sky/ground (+ optional SRTM silhouette) | full 3D OpenGL SVT |
| SRTM usage | TAWS proximity only (SRTM3 forced) | TAWS + SVT mesh |
| TAWS terrain evaluation | spot sample at current position | forward-projected look-ahead |
| Audio callouts | none (visual-only) | EGPWS-style voice pipeline |
| Flight-path vector | not implemented | implemented |
| Renderer | pygame → framebuffer | pygame + moderngl/EGL composite |
| Magnetometer cal | cardinal walk **and** TUMBLE hard-iron | cardinal walk and TUMBLE |

(Note: the TUMBLE hard-iron calibration flow **is** present on the Pi Zero, alongside
the cardinal walk-through.) A Pi-Zero-specific SRTM **COMPACT** action
(`_td_compact_worker`) downsamples SRTM1 tiles to SRTM3 in place to reclaim storage/RAM.

---

## 9. Key Design Decisions & Rationale

| Decision | Rationale |
|----------|-----------|
| No SVT background | Holds 30 fps within the Pi Zero 2W GPU/RAM budget (HLR-DISP-ZERO-ARCH-002/004). |
| SRTM3 forced | Smaller tiles fit ~512 MB RAM without OOM reboots. |
| Framebuffer via kmsdrm, no X11 | Removes compositor overhead; deterministic full-screen. |
| Single monolithic `pfd.py` | Keeps the hot render path free of import/dispatch overhead; shared logic lives in `shared/`. |
| All I/O off-thread | The render loop never blocks; `state` guarded by one lock. |
| Visual-only alerting | Pi Zero has no audio stack; banners/badges carry all annunciation. |
| Atomic debounced settings | Power-loss safe, minimal SD wear. |

---

## 10. Traceability

Low-level requirements in SLR-DISP-ZERO-001 trace each behaviour to the anchor symbols
above. HLR section → component mapping:

| HLR section | Components |
|-------------|-----------|
| §2–3 platform/rendering | `main()`, `config.py`, `_flip()`, backlight |
| §4 stale detection | AHRS ingest, `smooth_state()`, badges |
| §5–8 tapes/AI/heading | `draw_speed_tape/alt_tape/heading_tape`, AI draws, `_resolve_hdg_source` |
| §9 TAWS | `_update_terrain_alert`, `_alert_radius_nm`, `draw_terrain_alert` |
| §9A airports | `draw_airport_symbols`, `airports`/`runways` |
| §9a persistence | `shared/settings.py` |
| §10–11 badges/colour | `draw_status_badges`, colour convention in draws |
| §12 setup | setup-screen dispatch, mag-cal, backlight |
| §13–14 sim/demo | `SimFlyState`, `DemoState` |
| §14A wx/winds/tfc/mfd/sync | `shared/wx/fisb/nexrad/adsb/screen_sync`, `moving_map.py` |

---

*End of SAD-DISP-ZERO-001.*
