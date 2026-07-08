# AHRS Unit — Software Level Requirements

| Field          | Value                                        |
|----------------|----------------------------------------------|
| Document No.   | SLR-AHRS-001                                 |
| Title          | AHRS Unit — Software Level Requirements      |
| Project        | Pico-AHRS / PFD                              |
| Date           | 2026-07-08                                   |
| Version        | 0.1                                          |
| Parent (HLR)   | HLR-AHRS-001                                 |
| Architecture   | SAD-AHRS-001                                 |

---

## 1. Introduction

### 1.1 Purpose and relationship to other documents

This document defines the **Software Level Requirements (SLR)** — the low-level
requirements — for the AHRS unit firmware. Each low-level requirement decomposes one or
more high-level requirements from HLR-AHRS-001 into an implementation-verifiable
"shall" statement, and traces *down* to the specific firmware module and
function/line that implements it (per SAD-AHRS-001). The traceability chain is:

```
HLR-AHRS-001  ─►  SLR-AHRS-001 (this document)  ─►  firmware/ source
```

### 1.2 Notation

- Each requirement is tagged **SLR-AHRS-<AREA>-<n>**.
- **Parent:** the HLR requirement(s) it refines.
- **Trace:** `file:symbol` (and line where stable) implementing the requirement.
- Line numbers are indicative of the version mapped and may drift; the function/symbol
  name is the durable anchor.

### 1.3 Scope note

The firmware imports none of the `shared/` library; it is the data producer. All traces
below are within `firmware/`. `test_flight_zero_sign.py` is a host-side verification
artefact, not flashed code.

---

## 2. Boot and Platform

> **SLR-AHRS-BOOT-001** — On startup the firmware shall record the hardware reset cause
> (`machine.reset_cause()`) and maintain a persistent boot counter and last-alive
> timestamp so that a prior-run crash can be bracketed.
> *Parent:* HLR-AHRS §1. *Trace:* `main.py:main()` boot-cause block; `boot_log.json` load/save helpers.

> **SLR-AHRS-BOOT-002** — The firmware shall bring up the Pico W Wi-Fi in
> access-point mode within a 10 s activation timeout, using WPA2 when a passphrase is
> configured and an open AP otherwise, and shall continue to a degraded mode if the AP
> fails to activate.
> *Parent:* HLR-AHRS-SSE-006. *Trace:* `main.py:setup_ap()`.

> **SLR-AHRS-BOOT-003** — The firmware shall instantiate all enabled sensor drivers and
> the attitude filter in a fixed order (IMU, GPS, baro, differential-pressure,
> filter), and shall tolerate the absence or init failure of the baro and
> differential-pressure sensors by continuing without them.
> *Parent:* HLR-AHRS §2, BARO-003, AIR-008. *Trace:* `main.py:main()` init ladder.

> **SLR-AHRS-BOOT-004** — The firmware shall load persisted trim, orientation,
> magnetic-deviation, and magnetometer-hard-iron configuration from flash at boot, so
> that pilot settings survive power cycles without reflashing.
> *Parent:* HLR-AHRS-CAL-001/002. *Trace:* `main.py:load_trims/load_orient/load_magdev/load_magcal`.

> **SLR-AHRS-BOOT-005** — The firmware shall initialise an 8 s hardware watchdog and
> feed it from a dedicated coroutine decoupled from the sensor loop, so that a hung
> data loop or a full scheduler deadlock triggers a reboot.
> *Parent:* HLR-AHRS §1 (autonomous operation). *Trace:* `main.py:main()` WDT init; `main.py:wdt_loop()`.

---

## 3. Concurrency and Loop

> **SLR-AHRS-LOOP-001** — The firmware shall run as a single cooperatively-scheduled
> `uasyncio` process with no preemptive threads or interrupt-driven data path, so that
> the shared `state` dictionary requires no locking.
> *Parent:* HLR-AHRS §1. *Trace:* `main.py:main()` `asyncio.gather()`; module-level `state` dict.

> **SLR-AHRS-LOOP-002** — The sensor loop shall execute at a nominal 50 Hz service rate
> (`sleep_ms(20)` per iteration), polling every present sensor once per iteration.
> *Parent:* HLR-AHRS-SF-003, BARO-001. *Trace:* `main.py:sensor_loop()` loop delay.

> **SLR-AHRS-LOOP-003** — The firmware shall force periodic garbage collection within
> the sensor loop so that MicroPython heap fragmentation cannot stall the data path.
> *Parent:* HLR-AHRS §1. *Trace:* `main.py:sensor_loop()` periodic `gc.collect()`.

> **SLR-AHRS-LOOP-004** — The firmware shall persist uptime to flash on a 60 s cadence
> from a dedicated coroutine, independent of the sensor loop.
> *Parent:* HLR-AHRS §1. *Trace:* `main.py:alive_ticker()`.

---

## 4. IMU Driver (WT901)

> **SLR-AHRS-IMU-001** — The IMU driver shall parse the WT901 11-byte binary packet
> stream over UART0 at the configured baud rate, verifying each packet's checksum
> (`sum(bytes[0..9]) & 0xFF`) and re-synchronising on the `0x55` header when a checksum
> fails.
> *Parent:* HLR-AHRS-HW-002, SF-001. *Trace:* `wt901.py:WT901.update()`, `_checksum()`.

> **SLR-AHRS-IMU-002** — The IMU driver shall decode accelerometer (0x51), gyroscope
> (0x52), Euler-angle (0x53), magnetometer (0x54), and quaternion (0x59) packet types,
> exposing per-type freshness flags and bad-checksum counters.
> *Parent:* HLR-AHRS-SF-001/002. *Trace:* `wt901.py:WT901.update()`.

> **SLR-AHRS-IMU-003** — When configured to do so, the driver shall re-assert the WT901
> output register set (accel + gyro + angle + mag) at startup to recover a device whose
> angle-packet output was previously disabled.
> *Parent:* HLR-AHRS-SF-001. *Trace:* `wt901.py:WT901.configure_default_output()`; `config.WT901_FORCE_DEFAULT_OUTPUT`.

---

## 5. Sensor Fusion

> **SLR-AHRS-SF-001** — Attitude shall be estimated by a Mahony nonlinear complementary
> filter on SO(3) with quaternion state, satisfying the HLR requirement for a
> complementary/Mahony/Madgwick algorithm.
> *Parent:* HLR-AHRS-SF-001. *Trace:* `ahrs_filter.py:Mahony.update()`.

> **SLR-AHRS-SF-002** — The filter shall be stepped once per fresh gyro packet using a
> `dt` measured from gyro-packet arrival time, clamped to the range 1–100 ms, so that
> loop jitter does not corrupt the integration step.
> *Parent:* HLR-AHRS-SF-003/004. *Trace:* `main.py:sensor_loop()` AHRS block; `main.py:_run_filter_step()`.

> **SLR-AHRS-SF-003** — Accelerometer correction of roll and pitch shall be weighted by
> a gravity-magnitude gate (full weight at |a| = 1 g, decreasing linearly to zero at
> the configured accel gate), so that non-gravitational acceleration is rejected.
> *Parent:* HLR-AHRS-SF-005/006. *Trace:* `ahrs_filter.py:Mahony.update()`; `config.AHRS_ACCEL_GATE_G`.

> **SLR-AHRS-SF-004** — Yaw shall be corrected by the magnetometer when available and
> valid (gated by gyro rate), and shall additionally be slaved toward GPS ground track
> at a bounded rate when GPS is valid and ground speed exceeds the configured minimum.
> *Parent:* HLR-AHRS-SF-002. *Trace:* `ahrs_filter.py:Mahony.update()` mag term; `main.py:sensor_loop()` GPS-track slaving; `Mahony.nudge_yaw_toward_deg()`.

> **SLR-AHRS-SF-005** — The filter shall maintain a gyro-bias integrator, clamped to a
> bounded rate, fed from the accelerometer error only, to correct slow gyro drift.
> *Parent:* HLR-AHRS-SF-005/006. *Trace:* `ahrs_filter.py:Mahony.update()` bias integrator; `config.AHRS_KI_ACC`.

> **SLR-AHRS-SF-006** — During maneuvers the accelerometer gain shall be dynamically
> reduced toward a configured minimum as a function of gyro rate and centripetal
> magnitude, so that the gyro dominates the solution in turns.
> *Parent:* HLR-AHRS-SF-005/006. *Trace:* `main.py:_run_filter_step()` dynamic-gain scheduling; `config.AHRS_DYN_*`.

> **SLR-AHRS-SF-007** — The firmware shall apply a centripetal-acceleration correction
> `a_c = ω × (V·fwd)` to the accelerometer input, selecting V from TAS, then GPS ground
> speed, then none, and shall report the selected aid source in the output stream.
> *Parent:* HLR-AHRS-SF-005/006. *Trace:* `main.py:_run_filter_step()` centripetal block; `state['att_aid']`.

> **SLR-AHRS-SF-008** — The firmware shall apply a linear-acceleration aid derived from
> the time derivative of the speed source, low-pass filtered, to remove false pitch
> from longitudinal acceleration/braking.
> *Parent:* HLR-AHRS-SF-006. *Trace:* `main.py:_update_linear_accel()`.

> **SLR-AHRS-SF-009** — On the first gyro packet after boot the filter shall be seeded
> once from the WT901 Euler angle, and an alignment banner state shall be asserted for
> the configured alignment duration.
> *Parent:* HLR-AHRS-SF-001. *Trace:* `main.py:sensor_loop()` align block; `Mahony.seed_from_euler_deg()`; `config.AHRS_ALIGN_DURATION_S`.

> **SLR-AHRS-SF-010** — The firmware shall detect a stationary condition (GPS ground
> speed, IAS, gyro rate, and net specific force all below thresholds, sustained for the
> configured hold time) and shall freeze the gyro bias and flag zero-velocity update
> (ZUPT) while stationary.
> *Parent:* HLR-AHRS-SF-005. *Trace:* `main.py:_detect_stationary()`; `state['ahrs_zupt']`.

---

## 6. Mounting, Alignment and Trim

> **SLR-AHRS-CAL-001** — The firmware shall map the WT901 sensor-frame Euler angles into
> the aircraft NED frame according to the configured connector orientation
> (right/forward/left/aft → 0/90/180/270° heading offset with axis swaps) and mounting
> (normal/inverted sign handling).
> *Parent:* HLR-AHRS-CAL-001. *Trace:* `main.py:_apply_remap()`.

> **SLR-AHRS-CAL-002** — The firmware shall apply an input-side axis alignment
> (Rodrigues rotation of raw gyro/accel/mag by the negative pitch/roll alignment angles)
> to cancel yaw-to-pitch/roll coupling from imperfect mounting, with alignment settable
> via the `/align` endpoint and the `$ALIGN` serial command within a ±15° range.
> *Parent:* HLR-AHRS-CAL-002. *Trace:* `main.py:_apply_axis_align()`; `web_server.py:_handle_align`.

> **SLR-AHRS-CAL-003** — The firmware shall add pilot pitch/roll/yaw trim offsets and a
> magnetic-deviation correction to the remapped attitude before publishing, with trim
> settable via `/trim` (±10° pitch/roll, ±180° yaw) and persisted to flash.
> *Parent:* HLR-AHRS-CAL-002. *Trace:* `main.py:_apply_remap()` trim/magdev application; `web_server.py:_handle_trim`.

> **SLR-AHRS-CAL-004** — The firmware shall support get/set/clear of a 36-point magnetic
> deviation table and of magnetometer hard-iron offsets, including a tumble-based
> hard-iron capture (`tumble_start`/`tumble_finish`), persisting each to flash.
> *Parent:* HLR-AHRS-CAL-003. *Trace:* `web_server.py:_handle_magcal/_handle_magoff`; `main.py:apply_magdev`, magcal load/save.

---

## 7. Barometric Altimetry

> **SLR-AHRS-BARO-001** — When a BME280 is present the firmware shall read pressure and
> temperature at the loop rate (≥10 Hz) and compute pressure altitude via the ICAO
> hypsometric formula with a pilot-adjustable QNH reference.
> *Parent:* HLR-AHRS-BARO-001/002. *Trace:* `bme280.py:BME280.update()`, `altitude_ft()`; `main.py:sensor_loop()` altitude block.

> **SLR-AHRS-BARO-002** — The firmware shall accept a QNH setting (validated to
> 800–1100 hPa) and an altitude-calibration request via the `/baro` endpoint, and shall
> back-calculate QNH from a commanded field altitude when requested.
> *Parent:* HLR-AHRS-BARO-002. *Trace:* `web_server.py:_handle_baro`; `bme280.py:calibrate_to_alt_ft()`.

> **SLR-AHRS-BARO-003** — When the BME280 is absent or its read fails, the firmware
> shall set `baro_ok = false`, report `baro_src = "gps"`, and use GPS altitude as the
> altitude source.
> *Parent:* HLR-AHRS-BARO-003. *Trace:* `main.py:sensor_loop()` altitude-source fallback.

> **SLR-AHRS-BARO-004** — The firmware shall compute barometric vertical speed as a
> time-smoothed derivative of altitude, clamped to a bounded range.
> *Parent:* HLR-AHRS-BARO-004. *Trace:* `bme280.py:BME280.update()` vspeed EMA.

---

## 8. GPS Integration

> **SLR-AHRS-GPS-001** — The GPS driver shall parse NMEA RMC and GGA sentences from
> UART1, verifying the NMEA XOR checksum and rejecting void-status or bad-checksum
> sentences.
> *Parent:* HLR-AHRS-GPS-001. *Trace:* `gps.py:GPS.update()`, `_parse_rmc()`, `_parse_gga()`.

> **SLR-AHRS-GPS-002** — The firmware shall publish latitude, longitude, ground track
> (true), ground speed, GPS MSL altitude, fix status, and satellite count in the output
> stream.
> *Parent:* HLR-AHRS-GPS-002. *Trace:* `main.py:sensor_loop()` GPS block; `state` GPS keys.

> **SLR-AHRS-GPS-003** — The firmware shall set `gps_ok = false` when no valid fix is
> present and shall track NMEA communication liveness within a 5 s window.
> *Parent:* HLR-AHRS-GPS-003. *Trace:* `main.py:sensor_loop()` GPS health flags.

---

## 9. Air-Data Computer

> **SLR-AHRS-AIR-001** — The differential-pressure driver shall read the SDP3x at ≥20 Hz
> effective rate, validating each of the three CRC-8 words in every 9-byte frame and
> discarding frames that fail, and shall read the scale factor live so a higher-range
> family part requires no code change.
> *Parent:* HLR-AHRS-AIR-001. *Trace:* `sdp31.py:SDP31.update()`, `_crc8()`.

> **SLR-AHRS-AIR-002** — The firmware shall support the MS4525DO transducer as a
> drop-in alternative sharing the `dp_pa`/`update()`/`zero()` interface, selecting it
> ahead of the SDP3x when both are enabled, and rejecting frames flagged fault or
> command-mode by the device status bits.
> *Parent:* HLR-AHRS-AIR-001/002. *Trace:* `ms4525.py:MS4525.update()`; `main.py:main()` transducer init ladder.

> **SLR-AHRS-AIR-003** — IAS shall be computed as `V = sqrt(2·dp/ρ₀)` against ρ₀ =
> 1.225 kg/m³, reporting zero for non-positive dp and applying a low-end deadband
> (default 10 kt) to suppress ground noise.
> *Parent:* HLR-AHRS-AIR-002. *Trace:* `airdata.py:ias_kt()`; `airdata.IAS_DEADBAND_KT`.

> **SLR-AHRS-AIR-004** — TAS shall be computed by density-correcting IAS with BME280
> static pressure and temperature (`ρ = P/(R·T)`), and shall equal IAS when the BME280
> is absent or unhealthy.
> *Parent:* HLR-AHRS-AIR-003. *Trace:* `airdata.py:tas_kt()`, `density_kgm3()`; `main.py:sensor_loop()` air-data block.

> **SLR-AHRS-AIR-005** — Density altitude shall be computed from BME280 static
> pressure/temperature via the inverse-ISA hypsometric relation on air density and
> reported in feet.
> *Parent:* HLR-AHRS-AIR-004. *Trace:* `airdata.py:density_alt_ft()`.

> **SLR-AHRS-AIR-006** — A wind-triangle solution shall be computed each tick when TAS,
> heading, GPS ground speed, and GPS track are all valid, reporting wind direction in
> meteorological ("from") convention and reporting calm (0°, 0 kt) below 1 kt.
> *Parent:* HLR-AHRS-AIR-005. *Trace:* `airdata.py:wind_solution()`; `main.py:sensor_loop()` wind computation.

> **SLR-AHRS-AIR-007** — The firmware shall set `airdata_ok` true when a
> differential-pressure measurement has been read successfully within the last 5 s, and
> false otherwise (sensor absent, failed, or disabled), continuing operation without the
> air-data fields when false.
> *Parent:* HLR-AHRS-AIR-006/008. *Trace:* `main.py:sensor_loop()` `airdata_ok` flag.

> **SLR-AHRS-AIR-008** — The firmware shall capture a zero-pressure offset automatically
> approximately 2 s after boot when auto-zero is enabled, and shall re-capture on demand
> via the `/sdp_zero` endpoint.
> *Parent:* HLR-AHRS-AIR-007. *Trace:* `main.py:sensor_loop()` auto-zero at tick 100; `web_server.py:_handle_sdp_zero`; `sdp31.py:SDP31.zero()`.

---

## 10. Data Output — SSE and Serial

> **SLR-AHRS-OUT-001** — The firmware shall host an HTTP/1.1 server on port 80 and serve
> an SSE endpoint at `/events` with `Content-Type: text/event-stream`.
> *Parent:* HLR-AHRS-SSE-001/002. *Trace:* `web_server.py:start_server()`, `_handle_sse()`.

> **SLR-AHRS-OUT-002** — Each SSE event shall be a single JSON object containing at
> minimum the HLR-mandated fields (`roll`, `pitch`, `yaw`, `speed`, `alt`, `vspeed`,
> `lat`, `lon`, `track`, `gps_alt`, `baro_hpa`, `baro_src`, `baro_ok`, `gps_ok`,
> `ahrs_ok`, `fix`, `sats`, `ay`, `ias_kt`, `tas_kt`, `dp_pa`, `oat_c`, `dens_alt_ft`,
> `wind_dir`, `wind_kt`, `airdata_ok`), produced by copying `state` and excluding
> underscore-prefixed internal keys.
> *Parent:* HLR-AHRS-SSE-003. *Trace:* `web_server.py:_handle_sse()` payload build.

> **SLR-AHRS-OUT-003** — The SSE stream shall emit events at the configured broadcast
> rate (default 10 Hz) with fresh data.
> *Parent:* HLR-AHRS-SSE-004. *Trace:* `web_server.py:_handle_sse()`; `config.BROADCAST_HZ`.

> **SLR-AHRS-OUT-004** — A client that reconnects to `/events` shall receive a
> well-formed event within one second without any handshake beyond the HTTP GET; the
> server shall bound the number of concurrent SSE clients and shall drop dead
> connections via a bounded drain timeout.
> *Parent:* HLR-AHRS-SSE-005. *Trace:* `web_server.py:_handle_sse()`, `_client_handler()`.

> **SLR-AHRS-OUT-005** — The firmware shall additionally emit a `$AHRS,{json}` frame
> over USB serial at the broadcast rate carrying a curated field subset, for a wired Pi
> display that does not use Wi-Fi.
> *Parent:* HLR-AHRS-SSE-003. *Trace:* `main.py:sensor_loop()` USB serial emit.

> **SLR-AHRS-OUT-006** — The firmware shall accept inbound `$MAGDEV/$MAGOFF/$ORIENT/
> $ALIGN` serial commands, each acknowledged, allowing the display to configure the
> AHRS over the wired link.
> *Parent:* HLR-AHRS-CAL-001/002. *Trace:* `main.py:stdin_cmd_loop()`, `_process_stdin_line()`.

> **SLR-AHRS-OUT-007** — The Wi-Fi AP SSID and passphrase shall be configurable from a
> configuration file without reflashing.
> *Parent:* HLR-AHRS-SSE-006. *Trace:* `config.py:AP_SSID/AP_PASSWORD`.

---

## 11. Health and Status

> **SLR-AHRS-HLTH-001** — The firmware shall set `ahrs_ok` true only while valid AHRS
> data has been produced within the last 5 s, and shall expose attitude provenance
> (`att_src`, `att_aid`) and alignment state (`ahrs_aligning`, `ahrs_zupt`) in the
> output stream.
> *Parent:* HLR-AHRS-SF-001. *Trace:* `main.py:sensor_loop()` health-flag block.

> **SLR-AHRS-HLTH-002** — The firmware shall expose a `/health` endpoint returning a
> liveness response for external monitoring.
> *Parent:* HLR-AHRS §1. *Trace:* `web_server.py:_handle_health`.

---

## 12. Traceability Summary (HLR → SLR)

| HLR | SLR |
|-----|-----|
| HW-001..007 | BOOT-003, IMU-001, BARO-001, AIR-001/002 |
| SF-001..006 | SF-001..010, IMU-001/002 |
| BARO-001..004 | BARO-001..004 |
| GPS-001..004 | GPS-001..003 |
| SSE-001..006 | OUT-001..007 |
| CAL-001..003 | CAL-001..004, OUT-006 |
| AIR-001..008 | AIR-001..008 |
| ENV-001..002 | (hardware; no software LLR) |

---

*End of SLR-AHRS-001.*
