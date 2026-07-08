# AHRS Unit — Software Architecture Document

| Field          | Value                                        |
|----------------|----------------------------------------------|
| Document No.   | SAD-AHRS-001                                 |
| Title          | AHRS Unit — Software Architecture Document   |
| Project        | Pico-AHRS / PFD                              |
| Date           | 2026-07-08                                   |
| Version        | 0.1                                          |
| Parent (HLR)   | HLR-AHRS-001                                 |
| Child (SLR)    | SLR-AHRS-001                                 |

---

## 1. Introduction

### 1.1 Purpose

This Software Architecture Document (SAD) describes the software architecture of the
AHRS unit firmware — the airborne sensor node of the Pico-AHRS / PFD system. It
decomposes the firmware into its constituent software components, defines their
responsibilities and interfaces, describes the runtime (concurrency) model and the
data flow through the system, and records the principal design decisions and their
rationale. The SAD is the design-phase bridge between the high-level requirements
(HLR-AHRS-001) and the low-level software requirements (SLR-AHRS-001): every
architectural element named here is the anchor to which one or more low-level
requirements trace.

### 1.2 Scope

This document covers the MicroPython firmware in the `firmware/` directory that runs
on the Raspberry Pi Pico W. It does **not** cover the display units (see
SAD-DISP-PI4-001 and SAD-DISP-ZERO-001) nor the `shared/` Python library, which the
firmware does not import — the firmware is the *producer* of the data that the
displays consume.

### 1.3 Reference Documents

| Ref | Document |
|-----|----------|
| [HLR] | `Docs/REQUIREMENTS_AHRS.md` — HLR-AHRS-001 |
| [SLR] | `Docs/SLR_AHRS.md` — SLR-AHRS-001 |
| [SSE] | W3C Server-Sent Events specification |
| [WMM] | WMM2025 (handled on the display side; firmware reports true/magnetic per WT901) |
| [MAH] | Mahony, Hamel & Pflimlin, *Nonlinear Complementary Filters on SO(3)* (2008) |

### 1.4 Target Platform

- **Processor:** Raspberry Pi Pico W (RP2040 + CYW43439 Wi-Fi), MicroPython runtime.
- **Peripheral buses:** UART0 (IMU), UART1 (GPS), I2C1 (BME280 + differential-pressure
  sensor), USB CDC serial (Pi link / debug).
- **Language/runtime:** MicroPython with `uasyncio` cooperative scheduler; no CPython,
  no OS threads.

---

## 2. Architectural Overview

The firmware is a **single-process, single-thread, cooperatively-scheduled**
application. All concurrency is expressed as `uasyncio` coroutines gathered at
startup; there is no preemption, no `_thread`, and no interrupt-driven I/O in the data
path. A single module-level `state` dictionary is the shared blackboard: one coroutine
(`sensor_loop`) writes flight state into it, and the network/serial handlers read from
it and post command flags back into it. Cooperative scheduling makes this blackboard
safe without locks, because mutation only ever yields at `await` points.

The architecture divides into five layers:

```
 ┌────────────────────────────────────────────────────────────────────┐
 │  L5  Output / interface   web_server.py (HTTP+SSE), USB serial       │
 ├────────────────────────────────────────────────────────────────────┤
 │  L4  State blackboard     main.state (dict)                          │
 ├────────────────────────────────────────────────────────────────────┤
 │  L3  Fusion & air-data    ahrs_filter.Mahony, airdata.*, main.*      │
 ├────────────────────────────────────────────────────────────────────┤
 │  L2  Sensor drivers       wt901, gps, bme280, sdp31, ms4525          │
 ├────────────────────────────────────────────────────────────────────┤
 │  L1  Platform / config    config.py, machine/uasyncio, WDT, WLAN AP  │
 └────────────────────────────────────────────────────────────────────┘
```

The `main` module owns L3–L4 orchestration and the boot sequence; it is deliberately
the only place where sensor outputs, fusion, remapping/trim, persistence, and the two
output paths are wired together.

---

## 3. Component Decomposition

Each component below is a source module in `firmware/`. Line counts are approximate.

### 3.1 `main.py` (~1426 lines) — Orchestrator

The application entry point and the only stateful "glue" module. Responsibilities:

- **Boot sequence** (`main()`): reset-cause logging (`machine.reset_cause()` →
  `boot_log.json`), Wi-Fi AP bring-up (`setup_ap()`), sensor instantiation, config
  load (`load_trims/load_orient/load_magdev/load_magcal`), filter instantiation,
  hardware watchdog init, and the `asyncio.gather()` of the five coroutines.
- **Sensor loop** (`sensor_loop()`): the ~50 Hz master tick that polls every sensor,
  runs the fusion step, computes air-data, evaluates health flags, services deferred
  persistence, and emits the USB serial frame.
- **Fusion integration** (`_run_filter_step()`, `_apply_remap()`,
  `_apply_axis_align()`, `_update_linear_accel()`, `_detect_stationary()`): the code
  that adapts raw IMU data into the Mahony filter and maps the filter output back into
  the aircraft NED frame with mounting/trim/declination corrections.
- **Auxiliary coroutines:** `stdin_cmd_loop()` (USB command parser), `wdt_loop()`
  (watchdog feeder), `alive_ticker()` (uptime persistence).
- **Shared state:** the module-level `state` dict (L4).

### 3.2 `ahrs_filter.py` (~295 lines) — Attitude Filter

`class Mahony`: a nonlinear complementary filter on SO(3) with quaternion state.
Public methods `update(gyro, accel, mag, dt, freeze_bias)`, `euler_deg()`,
`seed_from_euler_deg()`, `nudge_yaw_toward_deg()`. Accelerometer error drives roll/pitch
via a magnitude-gated cross-product term; magnetometer error drives yaw; a gyro-bias
integrator provides slow drift correction. Pure computation — no I/O, no global state.

### 3.3 Sensor drivers (L2)

| Module | Device | Bus | Addr/port | Output |
|--------|--------|-----|-----------|--------|
| `wt901.py` (~210) | WitMotion WT901 9-axis IMU/AHRS | UART0 (GP0/GP1) | 9600 baud | accel, gyro, mag, Euler, quaternion; per-packet freshness flags |
| `gps.py` (~159) | u-blox NMEA GPS | UART1 (GP4/GP5) | 9600 baud | lat/lon, gs, track, alt, fix, sats, vspeed |
| `bme280.py` (~147) | Bosch BME280 baro/temp | I2C1 (GP2/GP3) | 0x76 | pressure, temperature, altitude, vspeed |
| `sdp31.py` (~152) | Sensirion SDP3x diff-pressure | I2C1 (shared) | 0x21 | dp_pa, temperature (CRC-8 validated) |
| `ms4525.py` (~116) | TE MS4525DO diff-pressure | I2C1 (shared) | 0x28 | dp_pa, temperature (production transducer) |

Each driver exposes an `update()` method that drains its bus and refreshes instance
attributes; the differential-pressure drivers additionally share the `dp_pa` / `zero()`
interface so they are interchangeable. Drivers never touch `state` — `sensor_loop`
copies their attributes into the blackboard.

### 3.4 `airdata.py` (~140 lines) — Air-Data Computer

Pure functions with no I/O: `density_kgm3()`, `ias_kt()`, `tas_kt()`,
`density_alt_ft()`, `wind_solution()`. Convert differential pressure + static
pressure/temperature + GPS ground vector into IAS, TAS, density altitude, and a
wind-triangle solution.

### 3.5 `web_server.py` (~515 lines) — Network Interface

`uasyncio` HTTP/1.1 server on port 80. Provides the SSE `/events` stream (the primary
output), configuration endpoints (`/baro`, `/trim`, `/align`, `/magcal`, `/magoff`,
`/sdp_zero`, `/health`), and static file serving for the bundled phone UI. Config
endpoints validate input and set command flags in `state`, which `sensor_loop`
actions; they never mutate flight state directly.

### 3.6 `config.py` (~281 lines) — Configuration

Constants only: pin assignments, sensor enable flags, Wi-Fi credentials, filter gains,
default orientation/trim, and broadcast rate. No executable logic. This is the single
tuning surface for the firmware.

### 3.7 `test_flight_zero_sign.py` (~161 lines) — Host Test

A CPython (not flashed) unit test that locks the display's LEVEL/flight-zero sign
convention against faithful copies of the firmware remap/align helpers. Verification
artefact only; not part of the runtime image.

---

## 4. Runtime / Concurrency Model

### 4.1 Coroutine set

`main()` launches five coroutines via `asyncio.gather()`:

| Coroutine | Cadence | Responsibility |
|-----------|---------|----------------|
| `sensor_loop` | ~50 Hz (`sleep_ms(20)`) | poll sensors, fuse, compute air-data, emit USB frame |
| `start_server` (web_server) | event-driven | HTTP/SSE per-connection handlers; SSE push at `BROADCAST_HZ` (10 Hz) |
| `stdin_cmd_loop` | 20 ms poll | parse `$MAGDEV/$MAGOFF/$ORIENT/$ALIGN` USB commands |
| `wdt_loop` | 1 s | feed the 8 s hardware watchdog |
| `alive_ticker` | 60 s | persist uptime to flash for crash bracketing |

### 4.2 Why cooperative single-thread

The design deliberately avoids `_thread` and interrupts:

- The blackboard `state` dict needs no locks because there is no preemption — a writer
  only yields at `await`, so no reader ever observes a half-written multi-field update.
- The watchdog is decoupled from the data loop (its own coroutine) so a transient HTTP
  or GC stall cannot starve the feed, while a genuine scheduler deadlock stops *all*
  coroutines and correctly triggers the watchdog reboot.

### 4.3 Fusion timing

The Mahony filter is stepped once per fresh gyro packet, not once per loop tick; `dt`
is measured from gyro-packet arrival times and clamped to 1–100 ms. The WT901's native
stream rate therefore governs the effective filter update rate, while `sensor_loop`
provides the surrounding 50 Hz service cadence.

---

## 5. Data Flow

```
UART0  WT901 ─ wt901.update() ─► sensor_loop
  │                                 ├─ hard-iron subtract (_mag_offset)
  │                                 ├─ centripetal a_c = ω × (V·fwd)   V = TAS→GS→none
  │                                 ├─ linear-accel aid (dV/dt)
  │                                 ├─ axis align (_apply_axis_align)
  │                                 └─ Mahony.update() ─► euler_deg()
  │                                        ├─ _apply_remap (connector/mounting)
  │                                        └─ + trim + magdev ─► state[roll,pitch,yaw,ay]
UART1  GPS ── gps.update() ─► state[lat,lon,speed,track,fix,sats,gps_alt]
  │                              └─► GPS-track yaw slaving ─► filter
I2C1   BME280 ── update() ─► state[alt,vspeed,oat_c], static P/T ─┐
I2C1   SDP31/MS4525 ── update() ─► dp_pa ─┐                       │
                                          ▼                       ▼
                       airdata.ias_kt / tas_kt / density_alt_ft / wind_solution
                                          ▼
                       state[ias_kt,tas_kt,dens_alt_ft,wind_dir,wind_kt,airdata_ok]
                                          │
              ┌───────────────────────────┴──────────────────────────┐
              ▼                                                        ▼
   web_server /events SSE (10 Hz, public state keys)         USB serial "$AHRS,{json}"
   → phone/Pi over Wi-Fi AP (192.168.4.1:80)                 (10 Hz) ; ← "$…" commands
```

Two output paths carry the same fused state: the Wi-Fi SSE stream (primary, for the
phone display and any Wi-Fi display) and the USB serial `$AHRS` line (for a wired Pi
display that does not use Wi-Fi). Both are driven at `BROADCAST_HZ`.

---

## 6. External Interfaces

### 6.1 SSE stream — `GET /events`

`Content-Type: text/event-stream`. One JSON object per `data:` line at 10 Hz. The
payload is a shallow copy of `state` **excluding** keys with a leading underscore
(command/scratch keys). Public fields include attitude (`roll`, `pitch`, `yaw`, `ay`),
GPS (`lat`, `lon`, `speed`, `track`, `fix`, `sats`, `gps_alt`), baro/altitude (`alt`,
`vspeed`, `baro_src`, `baro_hpa`), air-data (`ias_kt`, `tas_kt`, `dp_pa`, `oat_c`,
`dens_alt_ft`, `wind_dir`, `wind_kt`, `airdata_ok`), health (`ahrs_ok`, `gps_ok`,
`baro_ok`), and provenance (`att_src`, `att_aid`, `ahrs_aligning`, `fw_ver`). Bounded to
two concurrent clients.

### 6.2 Configuration endpoints (HTTP GET)

`/baro`, `/trim`, `/align`, `/magcal`, `/magoff`, `/sdp_zero`, `/health`, plus static
serving for the bundled phone UI. All validate range and set a command flag in `state`.

### 6.3 USB serial

Outbound `$AHRS,{json}` at 10 Hz (a curated field subset) and optional `$AHRSDBG`.
Inbound `$MAGDEV/$MAGOFF/$ORIENT/$ALIGN` command lines, each acknowledged.

### 6.4 Wi-Fi

Soft-AP (`network.WLAN(AP_IF)`); default SSID and passphrase in `config.py`, editable
without reflashing. Typical AP address `192.168.4.1`.

---

## 7. Data Design

- **`state` (module-level dict):** the single source of truth for current flight state
  and command flags. Public keys are broadcast; `_`-prefixed keys are internal.
- **Persisted files (flash):** `trims.json`, `orient.json`, `magdev.json`,
  `magcal.json`, `boot_log.json`. Written only when a corresponding command flag is
  set, and flushed inside `sensor_loop` between ticks so writes never block a network
  handler.

---

## 8. Key Design Decisions & Rationale

| Decision | Rationale |
|----------|-----------|
| Cooperative `uasyncio` single-thread | Lock-free shared state; deterministic timing; watchdog can still catch a true deadlock. |
| Mahony over Madgwick/plain complementary | Explicit gyro-bias integrator and separate accel/mag gains give stable drift correction with tunable maneuver rejection. |
| Fusion stepped per gyro packet, not per tick | Decouples filter `dt` accuracy from loop jitter; `dt` measured from packet arrival. |
| Interchangeable diff-pressure drivers | `sdp31`/`ms4525` share `dp_pa`/`zero()`, so hardware can change with only a config edit. |
| Dual output (SSE + USB `$AHRS`) | Wi-Fi for phone/wireless displays; wired serial for a Pi display with no Wi-Fi dependency. |
| Watchdog on its own coroutine | Isolates the feed from HTTP/GC stalls while still rebooting on a genuine scheduler hang. |
| Config-file orientation/trim/Wi-Fi | Field adjustment without reflashing, per HLR-AHRS-CAL and HLR-AHRS-SSE-006. |

---

## 9. Traceability

The low-level requirements in SLR-AHRS-001 trace each behaviour of these components to
the specific module and function/line. The mapping from HLR sections to architectural
components is:

| HLR section | Components |
|-------------|-----------|
| §2 Hardware platform | L1/L2 drivers, `config.py` pin map |
| §3 Sensor fusion | `ahrs_filter.Mahony`, `main._run_filter_step/_apply_remap/_detect_stationary` |
| §4 Barometric altimetry | `bme280.py`, `main` altitude-source block |
| §5 GPS integration | `gps.py`, `main` GPS block |
| §6 SSE stream | `web_server._handle_sse`, `main.state` |
| §7 Mounting & calibration | `main._apply_remap/_apply_axis_align`, `/trim`,`/align`,`/magcal` endpoints |
| §7B Air-data computer | `airdata.py`, `sdp31/ms4525`, `main` air-data block |
| §8 Environmental | (hardware; software imposes no additional constraint) |

---

*End of SAD-AHRS-001.*
