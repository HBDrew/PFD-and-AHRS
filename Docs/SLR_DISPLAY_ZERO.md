# Display Unit (Pi Zero 2W) — Software Level Requirements

| Field          | Value                                                 |
|----------------|-------------------------------------------------------|
| Document No.   | SLR-DISP-ZERO-001                                     |
| Title          | Display Unit (Pi Zero 2W) — Software Level Requirements|
| Project        | Pico-AHRS / PFD                                       |
| Date           | 2026-07-08                                            |
| Version        | 0.1                                                   |
| Parent (HLR)   | HLR-DISP-ZERO-001                                     |
| Architecture   | SAD-DISP-ZERO-001                                     |

---

## 1. Introduction

This document defines the low-level Software Level Requirements for the Pi Zero 2W
display unit. Each requirement refines one or more high-level requirements from
HLR-DISP-ZERO-001 and traces to the specific module and function/symbol that
implements it (per SAD-DISP-ZERO-001).

```
HLR-DISP-ZERO-001  ─►  SLR-DISP-ZERO-001 (this document)  ─►  pi_zero/ + shared/ source
```

Notation follows SLR-AHRS-001 §1.2: each requirement is tagged
**SLR-DISP-ZERO-<AREA>-<n>**, cites its **Parent** HLR, and gives a **Trace** to
`file:symbol`. Function/symbol names are the durable anchor; line numbers are
indicative. Unless a file is named, `pfd.py` means `pi_zero/pfd.py`.

---

## 2. Platform and Render Loop

> **SLR-DISP-ZERO-PLAT-001** — The application shall render with pygame directly to the
> KMS/DRM framebuffer (`SDL_VIDEODRIVER=kmsdrm`) with no X11 or Wayland compositor, at a
> 640×480 native resolution.
> *Parent:* HLR-DISP-ZERO-HW-002, REND-002. *Trace:* `pfd.py` SDL env setup; `config.py:DISPLAY_W/H`.

> **SLR-DISP-ZERO-PLAT-002** — The main loop shall maintain a sustained 30 fps target
> via `clock.tick(TARGET_FPS)` and shall present each frame through the flip helper,
> using `pygame.SCALED` for SDL2 C-side logical scaling in fullscreen.
> *Parent:* HLR-DISP-ZERO-REND-001. *Trace:* `pfd.py:main()` loop; `_flip()`; `config.TARGET_FPS`.

> **SLR-DISP-ZERO-PLAT-003** — The render function shall never block on network or disk
> I/O; all such work shall run on background daemon threads that merge results into the
> shared `state` dict under `_state_lock` or into module caches.
> *Parent:* HLR-DISP-ZERO-REND-001. *Trace:* `pfd.py` thread starts in `main()`; `_state_lock`.

> **SLR-DISP-ZERO-PLAT-004** — The display backlight shall be driven via PWM on GPIO 18
> from a brightness setting adjustable in 10 steps and persisted across power cycles.
> *Parent:* HLR-DISP-ZERO-SETUP-005. *Trace:* `pfd.py:_init_backlight()`, `_set_backlight()`.

---

## 3. Data Ingest and Smoothing

> **SLR-DISP-ZERO-DATA-001** — The unit shall ingest AHRS flight state preferring the
> wired USB `$AHRS` serial link (`SerialClient`) and falling back to the Wi-Fi SSE
> stream (`SSEClient`, `http://192.168.4.1/events`), merging each JSON update into
> `state` under `_state_lock`.
> *Parent:* HLR-DISP-ZERO §1, STALE-001. *Trace:* `pfd.py:main()` client setup; `shared/serial_client.py`, `shared/sse_client.py`.

> **SLR-DISP-ZERO-DATA-002** — Analogue fields (roll, pitch, ay, speed, alt, vspeed,
> ias, tas) shall be passed through an IIR low-pass filter with coefficient α = 0.25 per
> frame before rendering; heading shall be smoothed with 0/360° wrap handling; discrete
> fields shall be copied unfiltered; and `baro_hpa` shall not be smoothed.
> *Parent:* HLR-DISP-ZERO-REND-003. *Trace:* `pfd.py:smooth_state()`; `SMOOTH_K`.

> **SLR-DISP-ZERO-DATA-003** — If no valid AHRS event has been received within the stale
> timeout (3 s), the unit shall treat all data as stale, display a `NO LINK` badge, and
> set `ahrs_ok` false internally.
> *Parent:* HLR-DISP-ZERO-STALE-001/002. *Trace:* `pfd.py:main()` link-state eval; `draw_status_badges()`; `config_base.STALE_TIMEOUT_S`.

---

## 4. Attitude Indicator (no SVT)

> **SLR-DISP-ZERO-AI-001** — The attitude-indicator background shall be a plain horizon
> (solid blue sky over solid brown ground) at all times; no synthetic-vision terrain
> background shall be rendered, regardless of whether SRTM tiles are present.
> *Parent:* HLR-DISP-ZERO-AI-001, ARCH-002/003. *Trace:* `pfd.py:draw_simple_ai_background()`.

> **SLR-DISP-ZERO-AI-002** — SRTM terrain data shall be consumed only for TAWS proximity
> alerting and (optionally) a lightweight above-horizon mountain silhouette; it shall not
> tint or texture the attitude-indicator background.
> *Parent:* HLR-DISP-ZERO-AI-004, ARCH-003. *Trace:* `pfd.py:draw_above_horizon_terrain()`; `_update_terrain_alert()`.

> **SLR-DISP-ZERO-AI-003** — The unit shall force the SRTM3 resolution preference so that
> only the smaller SRTM3 tiles are loaded, keeping terrain memory within the Pi Zero 2W
> RAM budget.
> *Parent:* HLR-DISP-ZERO-ARCH-004. *Trace:* `pfd.py` `terrain.set_resolution_preference("srtm3")`.

> **SLR-DISP-ZERO-AI-004** — The attitude indicator shall render a pitch ladder (±5/10/
> 15/20/30°), a roll arc with doghouse pointer and wings-level reference, a slip/skid
> ball driven by lateral acceleration `ay`, and a fixed amber delta-wing aircraft symbol.
> *Parent:* HLR-DISP-ZERO-AI-002/003/005/006. *Trace:* `pfd.py:draw_pitch_ladder()`, `draw_roll_arc()`, `draw_slip_ball()`, `draw_aircraft_symbol()`.

---

## 5. Tapes and Heading

> **SLR-DISP-ZERO-SPD-001** — The airspeed tape shall present a Veeder-Root rolling-drum
> readout with V-speed colour bands (white VS0–VFE, green VS1–VNO, yellow VNO–VNE, red
> radial at VNE) and numerals coloured by airspeed (white/yellow/red vs VNO/VNE).
> *Parent:* HLR-DISP-ZERO-SPD-001/002/003. *Trace:* `pfd.py:draw_speed_tape()`, `_band()`, `_rolling_drum()`.

> **SLR-DISP-ZERO-SPD-002** — A speed bug and readout button shall be settable via numpad;
> both shall be cyan when the airspeed source is an IAS sensor and magenta when the
> source is GPS groundspeed, with automatic fallback to GPS when `airdata_ok` is false.
> *Parent:* HLR-DISP-ZERO-SPD-004/005/006/007. *Trace:* `pfd.py:draw_speed_tape()`; `_open_numpad()`.

> **SLR-DISP-ZERO-ALT-001** — The altitude tape shall present a rolling-drum readout in
> 50 ft increments, an altitude bug settable by numpad or by tapping the tape, a baro
> setting button (QNH in inHg/hPa when barometric, `GPS ALT` in magenta otherwise), and
> an inner VSI bar scaled to ±2000 fpm turning amber beyond ±1500 fpm.
> *Parent:* HLR-DISP-ZERO-ALT-001..007. *Trace:* `pfd.py:draw_alt_tape()`, `_rolling_drum_alt20()`.

> **SLR-DISP-ZERO-HDG-001** — The heading tape shall scroll with cardinal/intercardinal
> labels, show a 3-digit heading box with an `M`/`G` source subscript and a magenta
> border in GPS-TRK mode, and render a heading bug and a GPS-track tick coloured by
> source.
> *Parent:* HLR-DISP-ZERO-HDG-001..008. *Trace:* `pfd.py:draw_heading_tape()`, `_resolve_hdg_source()`, `_hdg_ref()`.

> **SLR-DISP-ZERO-HDG-002** — GPS-TRK mode shall slave the gyro heading to GPS ground
> track through a complementary filter (default K = 0.05); true↔magnetic conversion
> shall use the shared WMM2025 variation module.
> *Parent:* HLR-DISP-ZERO-HDG-005. *Trace:* `pfd.py:_update_gps_heading()`; `shared/magvar.py`.

---

## 6. Terrain and Obstacle Alerting (visual only)

> **SLR-DISP-ZERO-TAWS-001** — Terrain proximity shall be evaluated each frame from a
> single spot SRTM elevation sample at the aircraft's current position, raising a
> `TERRAIN CAUTION` banner (amber) within the caution floor and a `PULL UP / TERRAIN`
> banner (red, flashing 1 Hz) within the warning floor.
> *Parent:* HLR-DISP-ZERO-TAWS-001/002/006. *Trace:* `pfd.py:_update_terrain_alert()`, `draw_terrain_alert()`.

> **SLR-DISP-ZERO-TAWS-002** — Obstacle proximity shall be evaluated within a
> speed-scaled look-ahead radius (clamped to 1–3 nm) restricted to a ±25° forward
> track wedge, using the FAA DOF query.
> *Parent:* HLR-DISP-ZERO-TAWS-003. *Trace:* `pfd.py:_alert_radius_nm()`, `obstacles.query_nearby`; `config_base.OBSTACLE_WEDGE_HALF_DEG`.

> **SLR-DISP-ZERO-TAWS-003** — Terrain and obstacle alerting shall be inhibited when
> ground speed is below the pilot-configured VS0 stall speed and when GPS is invalid, to
> silence taxi/takeoff/rollout nuisance.
> *Parent:* HLR-DISP-ZERO-TAWS-006. *Trace:* `pfd.py:_update_terrain_alert()` inhibit conditions.

> **SLR-DISP-ZERO-TAWS-004** — The unit shall not implement an audio callout pipeline;
> all terrain, obstacle, and traffic alerting shall be visual only (banners/badges), and
> the SDL audio driver shall be forced to the dummy backend.
> *Parent:* HLR-DISP-ZERO-TFC-001. *Trace:* `pfd.py` `SDL_AUDIODRIVER=dummy`; `_draw_pfd_traffic_alert()`.

> **SLR-DISP-ZERO-TAWS-005** — SRTM terrain and FAA obstacle data shall be downloadable
> from within the user interface; obstacle data older than 28 days shall be flagged
> `EXP OBS`.
> *Parent:* HLR-DISP-ZERO-TAWS-004/005. *Trace:* `pfd.py` terrain/obstacle data screens.

---

## 7. Airports, Obstacles, Status

> **SLR-DISP-ZERO-APT-001** — Airports within a configurable radius shall be projected
> onto the attitude indicator with type-encoded symbols (public ring, heliport `H`,
> seaplane, balloonport), and identifiers within a closer radius shall render as posted
> "road-sign" labels clamped within the AI rectangle and culled outside the AI field of
> view.
> *Parent:* HLR-DISP-ZERO-APT-001..005. *Trace:* `pfd.py:draw_airport_symbols()`.

> **SLR-DISP-ZERO-APT-002** — Airport and runway databases shall load from the
> OurAirports CSVs into NumPy `.npy` caches on first access, filtering closed records and
> those missing coordinates; four independently toggleable type filters (PUBLIC/HELI/
> WATER/OTHER) plus RUNWAYS and extended-centerline toggles shall persist across power
> cycles.
> *Parent:* HLR-DISP-ZERO-APT-006..012. *Trace:* `shared/airports.py:load`, `shared/runways.py:load`; `disp["ad"]` filters.

> **SLR-DISP-ZERO-APT-003** — Obstacles shall render from FAA DOF data with MSL-top
> labels, drawn after airport symbols in Z-order.
> *Parent:* HLR-DISP-ZERO-APT-005. *Trace:* `pfd.py:draw_obstacle_symbols()`; `shared/obstacles.py`.

> **SLR-DISP-ZERO-BADGE-001** — Status badges shall appear only when a condition requires
> attention (blank strip in fully normal operation) and shall implement the HLR badge set
> (`AHRS FAIL`, `NO LINK`, `NO TER`, `NO OBS`, `EXP OBS`, `NO APT`, `EXP APT`, `GPS TRK`,
> `GPS ALT`, `GPS Nsat`, `NO GPS`) with the specified colours.
> *Parent:* HLR-DISP-ZERO-BADGE-001/002. *Trace:* `pfd.py:draw_status_badges()`.

> **SLR-DISP-ZERO-COLOR-001** — The GPS = magenta / onboard-sensor = cyan data-source
> colour convention shall be applied consistently to the speed/altitude/heading bugs and
> buttons, the baro button, the heading-box border, and the heading source subscript.
> *Parent:* HLR-DISP-ZERO-COLOR-001/002/003. *Trace:* `pfd.py` tape/heading draw routines.

---

## 8. Setup, Calibration, Persistence

> **SLR-DISP-ZERO-SET-001** — The setup menu shall open on a two-finger press-and-hold of
> at least 0.8 s and shall provide the Flight Profile, Display, AHRS/Sensors,
> Connectivity, and System sub-menus, with the Cessna-172S factory-default V-speeds and
> independently selectable speed/altitude/pressure units.
> *Parent:* HLR-DISP-ZERO-SETUP-001..005. *Trace:* `pfd.py:handle_event()` gesture; `draw_setup_screen()`, `setup_hit()`; `config_base.LONG_PRESS_MS`.

> **SLR-DISP-ZERO-SET-002** — The MFD full-screen moving map shall be reachable by a
> three-finger hold (≈2 s) when enabled, delegating rendering to the moving-map module.
> *Parent:* HLR-DISP-ZERO-MFD-001. *Trace:* `pfd.py:main()` gesture; `draw_mfd()` → `moving_map.render()`; `config_base.MFD_SWAP_HOLD_MS`.

> **SLR-DISP-ZERO-SET-003** — A magnetometer calibration facility shall provide both a
> cardinal walk-through capture and a TUMBLE hard-iron calibration, pushing results to
> the AHRS via `$MAGOFF` START/FINISH commands.
> *Parent:* HLR-DISP-ZERO-MAG-001. *Trace:* `pfd.py:_mag_cal_capture()`, `_mag_cal_tumble_toggle()`, `_mag_cal_tumble_tick()`, `_push_magoff_tumble()`.

> **SLR-DISP-ZERO-SET-004** — A Pi-Zero-specific SRTM COMPACT action shall downsample
> present SRTM1 tiles to SRTM3 in place (with atomic per-tile rewrites) to reclaim
> storage/RAM.
> *Parent:* HLR-DISP-ZERO-SRTM-001. *Trace:* `pfd.py:_td_compact_worker()`, `_td_start_compact()`.

> **SLR-DISP-ZERO-PERSIST-001** — User settings shall persist to
> `pi_zero/data/settings.json` via a whitelisted, debounced (1.5 s coalesce) daemon
> writer, written atomically (`.tmp` + `fsync` + `os.replace` + directory fsync), with a
> skip-list excluding the Wi-Fi password, download state, and runtime mag-cal; pending
> changes shall be flushed synchronously on shutdown.
> *Parent:* HLR-DISP-ZERO-PERSIST-001..004. *Trace:* `shared/settings.py:load_into/save_from/mark_dirty/flush`.

---

## 9. Simulator and Demo

> **SLR-DISP-ZERO-SIM-001** — The unit shall include a built-in simulator whose autopilot
> model drives all instruments from the heading/altitude/speed bugs in real time, with
> 12 preset departure airports and independently injectable GPS/baro/AHRS failures, and a
> tappable `SIM` watermark opening the SIM CONTROLS overlay.
> *Parent:* HLR-DISP-ZERO-SIM-001..005. *Trace:* `pfd.py:SimFlyState`, `draw_sim_setup()`, `draw_sim_controls()`; `config_base.SIM_PRESETS`.

> **SLR-DISP-ZERO-DEMO-001** — A scripted demo mode, launchable with `--demo`, shall
> animate a Sedona-area flight driving all instruments without AHRS hardware or network.
> *Parent:* HLR-DISP-ZERO-DEMO-001/002. *Trace:* `pfd.py:DemoState`.

---

## 10. Weather, Winds, Traffic, Screen-Sync

> **SLR-DISP-ZERO-WX-001** — The unit shall present the internet + FIS-B weather suite
> (METAR/TAF/AIRMET/SIGMET/NEXRAD/NOTAM) with the OVLY overlay cycle, RADIO/AUTO/INET
> source toggle, MET readout picker with ICAO idents below 160 NM range, and NOTAM
> credential entry, matching the Pi 4 feature set.
> *Parent:* HLR-DISP-ZERO-WX-001/002. *Trace:* `pfd.py` wx integration; `shared/wx.py`, `shared/fisb.py`, `shared/nexrad.py`, `shared/mapoverlay.py`.

> **SLR-DISP-ZERO-WND-001** — The unit shall display winds-aloft barbs from the national
> disk-cached GFS grid with altitude/forecast-time selectors and shall share/adopt winds
> zones over the screen-sync link.
> *Parent:* HLR-DISP-ZERO-WND-001. *Trace:* `pfd.py` winds integration; `shared/wx.py` WindsUSCache; `shared/screen_sync.py`.

> **SLR-DISP-ZERO-TFC-001** — ADS-B/FIS-B traffic shall render as threat-coloured
> diamonds with declutter filters, raising a flashing visual TRAFFIC banner (no voice) on
> an alert-envelope entry, from a radio GDL90/UDP source or the built-in internet feed.
> *Parent:* HLR-DISP-ZERO-TFC-001. *Trace:* `pfd.py:_update_traffic()`, `_draw_pfd_traffic_alert()`; `shared/adsb.py`, `shared/adsb_feed.py`.

> **SLR-DISP-ZERO-SYNC-001** — Displays on a shared network shall peer-sync over UDP with
> no master, with per-category TX/RX control (bugs, baro, nav, AHRS, GPS, flight plan/
> library) plus always-on winds/NOTAM sharing and an AUTO/USB/NET transport selector; the
> USB transport shall use the USB-ethernet gadget configured by `setup_usb_sync.sh`.
> *Parent:* HLR-DISP-ZERO-MFD-001, WND-001, WX-002. *Trace:* `pfd.py` `ScreenSync` setup; `shared/screen_sync.py`, `shared/fpllib.py`; `pi_zero/setup_usb_sync.sh`.

---

## 11. Service Installation

> **SLR-DISP-ZERO-SVC-001** — `setup.sh` shall install a `pfd.service` systemd unit that
> auto-starts the PFD under `SDL_VIDEODRIVER=kmsdrm` with the required device groups, a
> root `ExecStartPre` that arms the PWM backlight, and `Restart=always`; and a restricted
> sudoers rule permitting only `systemctl poweroff|reboot` for the SYSTEM screen.
> *Parent:* HLR-DISP-ZERO §2. *Trace:* `pi_zero/setup.sh`.

---

## 12. Traceability Summary (HLR → SLR)

| HLR section | SLR |
|-------------|-----|
| HW/REND (§2–3) | PLAT-001..004, DATA-002 |
| STALE (§4) | DATA-001/003 |
| SPD/ALT (§5–6) | SPD-001/002, ALT-001 |
| AI (§7) | AI-001..004 |
| HDG (§8) | HDG-001/002 |
| TAWS (§9) | TAWS-001..005 |
| APT (§9A) | APT-001..003 |
| PERSIST (§9a) | PERSIST-001 |
| BADGE/COLOR (§10–11) | BADGE-001, COLOR-001 |
| SETUP (§12) | SET-001..004, PLAT-004 |
| SIM/DEMO (§13–14) | SIM-001, DEMO-001 |
| WX/WND/TFC/MFD/SYNC/MAG/SRTM (§14A) | WX-001, WND-001, TFC-001, SYNC-001, SET-002/003/004 |

---

*End of SLR-DISP-ZERO-001.*
