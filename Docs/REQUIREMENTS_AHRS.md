# AHRS Unit — High-Level Requirements

| Field          | Value                               |
|----------------|-------------------------------------|
| Document No.   | HLR-AHRS-001                        |
| Title          | AHRS Unit — High-Level Requirements |
| Project        | Pico-AHRS / PFD                     |
| Date           | 2026-05-15                          |
| Version        | 0.2                                 |

---

## 1. Overview

The AHRS unit is the airborne sensor node of the Pico-AHRS / PFD system. It runs on a Raspberry Pi Pico W and is responsible for acquiring raw inertial, barometric, and GPS data; fusing that data into a real-time attitude solution; and broadcasting the resulting flight-state information over a Wi-Fi Server-Sent Events (SSE) stream to one or more display units. The AHRS unit operates autonomously once powered and requires no operator interaction during normal flight. All pilot-adjustable parameters — mounting orientation, baro reference, and Wi-Fi credentials — are set from the display unit or from a configuration file without reflashing firmware.

---

## 2. Hardware Platform

The following requirements define the minimum acceptable hardware configuration for the AHRS unit. All components must be integrated into a self-contained enclosure suitable for aircraft cockpit installation.

> **REQ-AHRS-HW-001** The processor shall be a Raspberry Pi Pico W.

> **REQ-AHRS-HW-002** The IMU shall be an ICM-42688-P 6-axis (gyroscope and accelerometer) sensor mounted on the same PCB or on a breakout board connected to the Pico W via SPI or I2C.

> **REQ-AHRS-HW-003** A BMP280 or BME280 barometric pressure sensor shall be connected to the Pico W via I2C.

> **REQ-AHRS-HW-004** A GPS module providing NMEA sentences — at minimum GGA and RMC — shall be connected to the Pico W via UART.

> **REQ-AHRS-HW-005** The unit shall be powered from an aircraft 5 V USB supply or a regulated aircraft bus.

> **REQ-AHRS-HW-006** A Sensirion SDP33-1500Pa differential-pressure sensor shall be connected to the Pico W via I²C at address 0x21, sharing the same SDA/SCL pair as the BME280. The sensor's `+` port shall be connected to the airframe pitot tube and the `−` port to the static reference (teed into the BME280's static port).

> **REQ-AHRS-HW-007** The AHRS unit shall be implemented as a single integrated printed-circuit board (rev A or later) carrying the Pico W, IMU, GPS, BME280, and SDP33. The PCB shall reserve an additional I²C device footprint at address 0x22 for a future second differential-pressure sensor dedicated to angle-of-attack measurement (see AOA-PROBE in `Docs/BUGS_AND_TODO.md`). Backwards compatibility with the bench-breakout pinout shall be preserved so that the same firmware boots on either build path. The driver implementation (`firmware/sdp31.py`) is protocol-compatible with the entire Sensirion SDP3x family — the scale factor is read from the device at startup — so a future swap to a higher-range part requires only a part-number update in the documentation.

---

## 3. Sensor Fusion

The firmware must maintain a continuous, low-latency attitude estimate by combining gyroscope and accelerometer measurements through a numerical filter. The requirements in this section govern the algorithm choice, update rate, accuracy budget, and latency budget for the attitude solution delivered to the display.

> **REQ-AHRS-SF-001** The firmware shall compute roll, pitch, and yaw (magnetic heading) by fusing gyroscope and accelerometer data using a complementary filter or a Madgwick / Mahony filter algorithm.

> **REQ-AHRS-SF-002** Yaw drift shall be corrected by magnetometer data when a magnetometer is available, or by GPS ground track when magnetometer data is unavailable or invalid.

> **REQ-AHRS-SF-003** The sensor fusion algorithm shall execute at a minimum internal update rate of 100 Hz.

> **REQ-AHRS-SF-004** Attitude data placed into the outbound SSE stream shall be no older than 20 ms at the moment of transmission.

> **REQ-AHRS-SF-005** Roll accuracy shall be within ±1° of the true roll angle under steady coordinated flight conditions.

> **REQ-AHRS-SF-006** Pitch accuracy shall be within ±1° of the true pitch angle under steady coordinated flight conditions.

---

## 4. Barometric Altimetry

The barometric subsystem converts raw pressure readings from the pressure sensor into a pilot-correctable pressure altitude. The requirements below govern the sampling rate, the altitude computation method, fault detection, and resolution.

> **REQ-AHRS-BARO-001** The firmware shall read the barometric pressure sensor at a minimum rate of 10 Hz.

> **REQ-AHRS-BARO-002** Altitude shall be computed from measured pressure using the International Standard Atmosphere (ISA) formula, with a pilot-adjustable barometric reference setting (QNH) applied to the calculation.

> **REQ-AHRS-BARO-003** When the pressure sensor is absent or returns readings that are outside the valid range or otherwise flagged as invalid, the firmware shall set `baro_ok = false` and report `baro_src = "gps"` in the outbound data stream, indicating that GPS altitude is being used as the fallback source.

> **REQ-AHRS-BARO-004** The computed altitude shall have a resolution better than 5 ft across the operational altitude range.

---

## 5. GPS Integration

The GPS subsystem parses standard NMEA sentences received over UART and forwards position, velocity, and navigation state data to the display unit as part of the regular SSE stream. The requirements below govern which sentences are parsed, which data fields are exposed, and how loss of fix is reported.

> **REQ-AHRS-GPS-001** The firmware shall parse NMEA GGA and RMC sentences received from the connected GPS module.

> **REQ-AHRS-GPS-002** The following GPS-derived values shall be included in the outbound data stream: latitude, longitude, ground track (true), ground speed, GPS altitude (MSL), fix status, and satellite count.

> **REQ-AHRS-GPS-003** When no valid GPS fix is available, the firmware shall set `gps_ok = false` in the outbound data stream.

> **REQ-AHRS-GPS-004** GPS data shall be forwarded to the display within one NMEA update epoch, which is typically one second at the standard 1 Hz GPS output rate.

---

## 6. Data Output — SSE Stream

The SSE stream is the sole output interface of the AHRS unit. It must carry all flight-state data needed by the display unit in a self-describing, reconnect-tolerant format. The requirements below govern the server configuration, the endpoint definition, the JSON payload, the emission rate, and the reconnect behaviour.

> **REQ-AHRS-SSE-001** The Pico W shall host an HTTP/1.1 server listening on port 80.

> **REQ-AHRS-SSE-002** The endpoint `/stream` shall deliver responses with `Content-Type: text/event-stream`, conforming to the Server-Sent Events specification.

> **REQ-AHRS-SSE-003** Each SSE event payload shall be a JSON object containing at minimum the following fields: `roll`, `pitch`, `yaw`, `speed`, `alt`, `vspeed`, `lat`, `lon`, `track`, `gps_alt`, `baro_hpa`, `baro_src`, `baro_ok`, `gps_ok`, `ahrs_ok`, `fix`, `sats`, `ay`, `ias_kt`, `tas_kt`, `dp_pa`, `oat_c`, `dens_alt_ft`, `wind_dir`, `wind_kt`, and `airdata_ok`.

> **REQ-AHRS-SSE-004** The `/stream` endpoint shall emit events at a minimum rate of 20 Hz.

> **REQ-AHRS-SSE-005** A client that disconnects and reconnects to `/stream` shall receive a valid, well-formed SSE event within one second of reconnection, without requiring any handshake beyond the initial HTTP GET request.

> **REQ-AHRS-SSE-006** The Pico W Wi-Fi access point SSID shall default to `AHRS-Link`. Both the SSID and the passphrase shall be configurable by editing a configuration file without requiring reflashing of the firmware.

---

## 7. Mounting and Calibration

Because the AHRS unit may be installed in different physical orientations depending on the aircraft and panel layout, the firmware and display software must support orientation correction and software-adjustable trim. The requirements below define the supported orientations and the available correction mechanisms.

> **REQ-AHRS-CAL-001** The unit shall support two mounting orientations: NORMAL (label side up) and INVERTED (label side down). The active orientation shall be selectable from the display unit without reflashing the AHRS firmware.

> **REQ-AHRS-CAL-002** The display unit shall apply pilot-configurable pitch and roll trim offsets, each in the range ±20°, in software to compensate for imperfect physical mounting alignment.

> **REQ-AHRS-CAL-003** A magnetometer hard-iron and soft-iron calibration procedure is planned, and the corresponding calibration data structure shall be reserved in firmware for future implementation. This requirement is marked as deferred pending magnetometer hardware integration.

---

## 7B. Air-Data Computer

The combination of the SDP33-1500Pa differential-pressure sensor and the existing BME280 static-pressure / OAT sensor forms an air-data computer co-located with the AHRS. The firmware computes the standard pitot-static set every sensor tick and broadcasts the results on the SSE / USB stream alongside the inertial state.

> **REQ-AHRS-AIR-001** The firmware shall read the SDP33-1500Pa differential-pressure sensor at a minimum effective rate of 20 Hz when present, and shall validate each 9-byte measurement frame against its three CRC-8 checksums before applying the value.

> **REQ-AHRS-AIR-002** Indicated airspeed (IAS) shall be computed from the differential-pressure transducer (SDP33 or MS4525DO) against standard sea-level density ρ₀ = 1.225 kg/m³ using `V = sqrt(2·dp / ρ₀)`. The result shall be expressed in knots in the outbound `ias_kt` field. Negative differential pressure shall read as zero airspeed, and the readout shall apply a low-end deadband of 10 kt to suppress static noise during ground operations. The deadband is sized for the MS4525DO's ~6 Pa peak noise floor (≈6 kt apparent IAS) with ~3× margin; the SDP33 is quieter so it remains comfortably under the same threshold.

> **REQ-AHRS-AIR-003** True airspeed (TAS) shall be computed by density-correcting IAS using the BME280 absolute static pressure and temperature: `TAS = IAS · sqrt(ρ₀ / ρ)` where `ρ = P / (R_spec · (T + 273.15))`. The result shall be expressed in knots in the outbound `tas_kt` field. When the BME280 is absent or unhealthy, TAS shall equal IAS and the `baro_ok` flag shall communicate the degraded state.

> **REQ-AHRS-AIR-004** Density altitude shall be computed from BME280 static pressure and temperature using the inverse ISA hypsometric formula on air density, and shall be expressed in feet in the outbound `dens_alt_ft` field. Accuracy shall be within ±200 ft up to 18 000 ft.

> **REQ-AHRS-AIR-005** A wind-triangle solution shall be computed each sensor tick when TAS, AHRS heading, GPS ground speed, and GPS track are all valid. The result shall populate `wind_dir` (meteorological convention, degrees *from*) and `wind_kt` in the outbound stream. Wind speeds below 1 kt shall be reported as calm (`0°, 0 kt`) rather than as a directionally noisy value.

> **REQ-AHRS-AIR-006** The firmware shall expose an `airdata_ok` boolean in the outbound stream, true when an SDP33 measurement has been successfully read within the last 5 seconds. The displays shall use this flag to switch the speed tape source between IAS (cyan) and GPS groundspeed (magenta).

> **REQ-AHRS-AIR-007** The firmware shall capture a zero-pressure offset automatically 2 seconds after boot (with `SDP31_AUTO_ZERO_AT_BOOT = True`, the default), assuming the aircraft is stationary with no airflow over the pitot. A `GET /sdp_zero` HTTP endpoint shall trigger a re-capture of the offset on demand for in-flight reboot or long-ground-hold scenarios.

> **REQ-AHRS-AIR-008** When the SDP33 is absent, has failed, or `SDP31_ENABLE = False`, the firmware shall set `airdata_ok = False` and continue operating without the air-data fields. The displays shall fall back to GPS groundspeed for the speed tape and shall suppress wind, density-altitude, and TAS readouts when this fallback is active.

---

## 8. Environmental

The AHRS unit will be installed in general aviation cockpits, which experience a wide range of temperatures, vibration levels, and occasional exposure to condensation. The requirements below establish the minimum environmental performance standards the enclosure and electronics must meet.

> **REQ-AHRS-ENV-001** The unit shall operate correctly across the full cockpit temperature range of −20 °C to +70 °C.

> **REQ-AHRS-ENV-002** The enclosure shall protect the internal electronics from direct moisture contact, meeting at minimum IP42 protection class or an equivalent aviation-grade enclosure standard.
