# AHRS Unit — High-Level Requirements

| Field          | Value                               |
|----------------|-------------------------------------|
| Document No.   | HLR-AHRS-001                        |
| Title          | AHRS Unit — High-Level Requirements |
| Project        | Pico-AHRS / PFD                     |
| Date           | 2026-04-11                          |
| Version        | 0.1                                 |

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

> **REQ-AHRS-SSE-003** Each SSE event payload shall be a JSON object containing at minimum the following fields: `roll`, `pitch`, `yaw`, `speed`, `alt`, `vspeed`, `lat`, `lon`, `track`, `gps_alt`, `baro_hpa`, `baro_src`, `baro_ok`, `gps_ok`, `ahrs_ok`, `fix`, `sats`, and `ay`.

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

## 8. Environmental

The AHRS unit will be installed in general aviation cockpits, which experience a wide range of temperatures, vibration levels, and occasional exposure to condensation. The requirements below establish the minimum environmental performance standards the enclosure and electronics must meet.

> **REQ-AHRS-ENV-001** The unit shall operate correctly across the full cockpit temperature range of −20 °C to +70 °C.

> **REQ-AHRS-ENV-002** The enclosure shall protect the internal electronics from direct moisture contact, meeting at minimum IP42 protection class or an equivalent aviation-grade enclosure standard.
