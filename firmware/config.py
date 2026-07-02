# ---------------------------------------------------------------------------
# config.py  –  Hardware pin assignments and system settings
# ---------------------------------------------------------------------------
#
# Pin map (same as the AHRS PCB rev A; matches bench breakouts wired the
# same way).  Both build paths share this file — the only difference is
# whether the sensors hang off a single board or four separate breakouts.
#
#   WT901 AHRS         UART0   GP0 TX / GP1 RX        3V3 @ 36
#   GY-NEO6MV2 GPS     UART1   GP4 TX / GP5 RX        VSYS @ 39
#   BME280 baro        I2C1    GP2 SDA / GP3 SCL      addr 0x76, 3V3
#   SDP33-1500Pa        I2C1    GP2 SDA / GP3 SCL      addr 0x21, 3V3
#   (reserved AOA)     I2C1    GP2 SDA / GP3 SCL      addr 0x22 (future SDP3x)
#
# I²C1 is shared across BME280 + SDP33 + (future) AOA sensor — each device
# has a distinct address so they coexist on the same SDA/SCL pair without
# arbitration logic.
#
# NOTE: UART0 on GP0/GP1 is shared with USB-serial debug.  During normal
#       (non-debug) flight use this is fine.  If you need USB debug
#       simultaneously, move WT901 to GP12/13 (UART0 alt) and update
#       WT901_TX_PIN / WT901_RX_PIN below.
# ---------------------------------------------------------------------------

# ── WT901 AHRS ──────────────────────────────────────────────────────────────
WT901_UART_ID = 0
WT901_TX_PIN  = 0   # GP0  – Pico TX → WT901 RX  (for sending config, optional)
WT901_RX_PIN  = 1   # GP1  – Pico RX ← WT901 TX
WT901_BAUD    = 9600  # WT901 factory default; increase to 115200 after config

# Force the WT901's Return-Data-Switch back to the factory default
# (ACC + GYRO + ANGLE + MAG) at every firmware boot. Recovers from a chip
# whose config has somehow been corrupted — we hit this with PKT_ANGLE
# (0x53) silently disappearing after many bench reboots. The reconfigure
# is idempotent (writes the same value) so leaving this True forever is
# safe. Set False only if you have a deliberately customised WT901 config
# you don't want overwritten.
WT901_FORCE_DEFAULT_OUTPUT = True

# ── WT901 sensor front-end: anti-alias bandwidth + sample rate ──────────────
# The on-Pico Mahony filter fuses the WT901's RAW accel/gyro (unlike the chip's
# internal Kalman, which shipped a pre-fused angle that was fine at a slow
# output rate).  A homebrew filter needs the raw IMU *oversampled* and *anti-
# alias filtered*, or cabin/engine vibration folds into the attitude solution.
#
# Two knobs:
#   WT901_HIGH_RATE  — when True, the driver raises the WT901 to 115200 baud +
#                      100 Hz output + 20 Hz bandwidth at boot (the proper fix,
#                      REQ-AHRS-SF-003).  The baud switch is self-healing, but
#                      FLASH AND VERIFY THIS ON THE BENCH FIRST — the AHRS is
#                      the only attitude source and you don't want to be
#                      chasing a mis-baud on the sole horizon in flight.
#                      Leave WT901_BAUD at 9600 (the chip's power-on baud); the
#                      driver probes and switches up from there.
#   WT901_LOWRATE_BANDWIDTH_CODE — applied when WT901_HIGH_RATE is False.  With
#                      the link still at 9600 (~10–20 raw samples/s) the anti-
#                      alias cutoff must be low; 5 Hz (0x06) trades attitude
#                      crispness for killing the vibration aliasing.  A safe
#                      in-flight-flashable partial fix on its own.
WT901_HIGH_RATE              = True
WT901_LOWRATE_BANDWIDTH_CODE = 0x06   # WT901.BW_5HZ
# 50 Hz output matches the firmware's 50 Hz sensor loop (asyncio.sleep_ms(20)):
# every packet is consumed by exactly one filter step, so there's no wasted
# MicroPython parse load (100 Hz half-fills the buffer and drops the loop rate,
# which starves the $AHRS output down to ~6 Hz).  20 Hz bandwidth sits just
# under the 25 Hz Nyquist for clean anti-aliasing.
WT901_HIGHRATE_RATE_CODE     = 0x08   # WT901.RRATE_50HZ
# 5 Hz DLPF: field capture showed the gyro reading ~18 dps of vibration with a
# 20 Hz cutoff (true rate ~2 dps), which poisons the centripetal term and gates
# the accel out 86% of the time so the filter can't hold gravity.  Aircraft
# attitude dynamics are < 2 Hz, so a 5 Hz cutoff (well under the 25 Hz Nyquist
# at 50 Hz output) crushes the engine/prop vibration with negligible lag.
WT901_HIGHRATE_BANDWIDTH_CODE = 0x06  # WT901.BW_5HZ
WT901_TARGET_BAUD            = 115200
WT901_TARGET_BAUD_CODE       = 0x06   # WT901.BAUD_115200

# ── GPS (GY-NEO6MV2 / u-blox NEO-6M) ───────────────────────────────────────
GPS_UART_ID = 1
GPS_TX_PIN  = 4   # GP4  – Pico TX → GPS RX  (for UBX config, optional)
GPS_RX_PIN  = 5   # GP5  – Pico RX ← GPS TX
GPS_BAUD    = 9600  # NEO-6M factory default

# ── WiFi Access Point ────────────────────────────────────────────────────────
# Phone connects to this network – no internet required
AP_SSID     = "AHRS-PFD"
AP_PASSWORD = ""           # empty = open AP (no password); WPA2 if non-empty

# ── Web server ────────────────────────────────────────────────────────────────
HTTP_PORT = 80   # Navigate to http://192.168.4.1 on the phone

# ── BME280 Barometer (optional) ──────────────────────────────────────────────
# Set BME280_ENABLE = False if no barometer is connected; the firmware will
# fall back to GPS altitude automatically.
#
# Wiring (I2C1):
#   VCC → 3V3(OUT)   GND → GND
#   SDA → GP2  (pin 4)    SCL → GP3  (pin 5)
#   SDO → GND  → I2C address 0x76  (pull SDO high for 0x77)
BME280_ENABLE      = True
BME280_I2C_ID      = 1
BME280_SDA_PIN     = 2      # GP2  (I2C1 SDA)
BME280_SCL_PIN     = 3      # GP3  (I2C1 SCL)
BME280_I2C_ADDR    = 0x76   # 0x76 (SDO=GND) or 0x77 (SDO=VCC)
BME280_QNH_DEFAULT = 1013.25  # hPa – ICAO standard; update via /baro on the display

# ── SDP33-1500Pa Differential Pressure (optional – airspeed sensor) ──────────
# Set SDP31_ENABLE = False on bench builds that don't carry the air-data
# sensor; the firmware will fall back to GPS groundspeed automatically and
# the $AHRS packet will report airdata_ok = False.
#
# Wiring (I2C1, shared with BME280):
#   VDD → 3V3(OUT)  GND → GND
#   SDA → GP2       SCL → GP3
#   ADDR pin floating → 0x21  (default; 0x22 if pulled to VDD — useful when
#   pairing a second SDP3x for AOA on the same bus, see AOA-PROBE in
#   Docs/BUGS_AND_TODO.md)
#
# Pneumatic plumbing on the airframe:
#   "+" port  → pitot   (ram pressure)
#   "−" port  → static  (cabin/airframe static reference, tee'd into BME280)
SDP31_ENABLE   = True
SDP31_I2C_ID   = 1
SDP31_SDA_PIN  = 2          # shared with BME280 (I2C1 SDA)
SDP31_SCL_PIN  = 3          # shared with BME280 (I2C1 SCL)
SDP31_I2C_ADDR = 0x21       # 0x21 default; 0x22 when ADDR pin tied to VDD

# Zero-offset capture at boot.  When True, the firmware records the dp_pa
# reading observed in the first ~2 s after power-up and subtracts it from
# subsequent readings — relies on the aircraft being stationary at boot.
# Disable if the aircraft is already moving at firmware start (in-flight
# restart).  The display also exposes a manual /sdp_zero endpoint.
SDP31_AUTO_ZERO_AT_BOOT = True

# ── MS4525DO Differential Pressure (alternative pitot transducer) ──────────
# TE Connectivity MS4525DO — drop-in alternative to the SDP3x family.
# Different protocol (4-byte register read, no CRC) but the on-Pico driver
# (firmware/ms4525.py) exposes the same dp_pa / update() / zero() interface
# so main.py treats them interchangeably.  When both are enabled the MS4525
# wins (it's first in the init ladder) — useful when the SDP33 has died on
# the bench but you still want to fly.
#
# Wiring (I²C1, shared with BME280):
#   VDD → 3V3(OUT)  GND → GND
#   SDA → GP2       SCL → GP3
#   Address: 0x28 (A-cal, factory default) or 0x36 (B-cal variant)
#
# Pneumatic plumbing on the airframe (same as SDP33):
#   "+" port  → pitot   (ram pressure)
#   "−" port  → static  (cabin/airframe static reference, tee'd into BME280)
MS4525_ENABLE            = True      # MS4525 is now the production transducer;
                                     # set False on bench builds without the part
MS4525_I2C_ID            = 1
MS4525_SDA_PIN           = 2         # shared with BME280 (I2C1 SDA)
MS4525_SCL_PIN           = 3         # shared with BME280 (I2C1 SCL)
MS4525_I2C_ADDR          = 0x28      # 0x28 = A-cal; 0x36 = B-cal
MS4525_PSI_RANGE         = 1.0       # full-scale ±psi: 1.0 for -001D, 2.0 for
                                     # -002D, 5.0 for -005D. Max IAS at sea-
                                     # level standard density (ρ₀=1.225 kg/m³):
                                     #   ±1 psi → 206 kt   (-001D)
                                     #   ±2 psi → 291 kt   (-002D)
                                     #   ±5 psi → 461 kt   (-005D)
                                     # ±1 psi covers any single piston / most twins.
MS4525_AUTO_ZERO_AT_BOOT = True      # same semantics as SDP31_AUTO_ZERO_AT_BOOT

# ── WT901 lateral-acceleration sign ──────────────────────────────────────────
# The WT901's ay axis drives the slip/skid ball.  If the ball deflects the
# wrong way after installation, flip this to -1 (sensor mounted 180° about yaw).
WT901_AY_SIGN = 1

# ── AHRS Mounting Trim ────────────────────────────────────────────────────────
# Additive degree offsets applied to raw WT901 output to compensate for
# imperfect physical mounting.  Overridden at runtime via /trim endpoint;
# persisted to trims.json on Pico flash.
AHRS_PITCH_TRIM = 0.0   # degrees; positive = nose-up correction
AHRS_ROLL_TRIM  = 0.0   # degrees; positive = right-roll correction
AHRS_YAW_TRIM   = 0.0   # degrees; positive = clockwise heading correction

# ── AHRS Axis Alignment ─────────────────────────────────────────────────────
# Small INPUT-side rotation applied to raw gyro/accel/mag before the Mahony
# filter, in airframe convention (pitch about aircraft Y, roll about aircraft
# X).  Compensates for sensor mounting misalignment that couples yaw rate
# into pitch/roll readings (e.g. left turn → display pitches up).  Unlike
# pitch/roll TRIM (output-side static offsets), ALIGN values fix the
# dynamic coupling.  Persisted to orient.json; pushed from Pi4 via $ALIGN.
AHRS_PITCH_ALIGN = 0.0   # degrees; rotation about aircraft pitch axis
AHRS_ROLL_ALIGN  = 0.0   # degrees; rotation about aircraft roll axis

# ── AHRS Orientation ─────────────────────────────────────────────────────────
# AHRS_CONNECTOR: which edge of the WT901 the connector points toward, viewed
# from the pilot's seat.  Swaps pitch/roll axes and adds a heading offset so
# the display reads correctly regardless of how the sensor is physically bolted.
#   'right'   – connector points to the right of the aircraft (factory default)
#   'forward' – connector points toward the nose
#   'left'    – connector points to the left of the aircraft
#   'aft'     – connector points toward the tail
AHRS_CONNECTOR = 'right'

# AHRS_MOUNTING: 'normal' = component-side up, 'inverted' = component-side down.
# Applies an additional pitch/roll sign flip, independent of AHRS_CONNECTOR.
AHRS_MOUNTING  = 'normal'

# ── On-Pico Mahony AHRS filter ──────────────────────────────────────────────
# When True, the firmware runs a Mahony filter on raw WT901 accel + gyro
# (+mag when available) and uses its output as roll/pitch/yaw. When False,
# the WT901's internal Euler output (PKT_ANGLE 0x53) is used unchanged — the
# pre-filter behaviour.
#
# The filter accepts a velocity-aided centripetal-acceleration correction
# (the dominant source of "leans" in coordinated turns). Speed source order:
#   1. IAS (SDP33 + airdata_ok)   — physically correct (air-relative ω × V)
#   2. GS  (GPS, gps_ok)          — close in still air, off by wind component
#   3. none                       — no centripetal correction; gyro/accel only
# The active source is broadcast as state['att_aid'] = 'tas' | 'gs' | 'basic'.
AHRS_FILTER_ENABLE      = True

# Mahony tuning.  kp_acc is the steady-state accel proportional gain;
# dynamic gain scheduling (see AHRS_DYN_*) scales it DOWN during active
# maneuvers so the gyro dominates through turns / climbs / taxi bumps,
# then ramps back up when the aircraft is quasi-static.  This trades a
# slower cold-start convergence (acceptable on a piston single — gyro
# bias washes out in ~30 s of level flight) for substantially better
# behaviour during the dynamic events where a high-trust accel pulls
# attitude away from gyro-truth.
AHRS_KP_ACC             = 0.80    # accel proportional gain — quiescent.
                                  # Was 1.0, dropped to 0.30 because noisy
                                  # centripetal was projecting onto pitch in
                                  # turns — but that noise is now removed at
                                  # the source (gyro LPF, AHRS_CENTRI_GYRO_TAU_S).
                                  # With mag off the accel is the ONLY thing
                                  # pinning pitch/roll to gravity, and 0.30 was
                                  # too weak to hold it against the gyro bias
                                  # (offset scales ~1/kp_acc). Restored toward 1.0.
AHRS_BIAS_CLAMP_DPS     = 12.0    # ± bound on the gyro-bias estimate (deg/s).
                                  # Was hard-clamped at 5 dps, but cockpit
                                  # vibration rectifies into a ~7-9 dps apparent
                                  # bias the estimator couldn't fully cancel,
                                  # leaving a persistent attitude offset. Raised
                                  # so the estimator can actually null it. Safe
                                  # now that no aid feedback loop can wind it up.
AHRS_KI_ACC             = 0.06    # accel integral gain — estimates gyro bias.
                                  # Was 0.001 (~4 min to learn a bias). ZUPT never
                                  # engages with the engine running (vibration
                                  # keeps gyro > 1 dps), so the in-flight bias
                                  # estimator is the ONLY thing that removes the
                                  # ~4 dps gyro bias — it has to be fast enough to
                                  # converge in seconds, not minutes. dyn_scale
                                  # still gates the integrator down in maneuvers,
                                  # and the ±5 dps bias clamp bounds windup.
                                  # gives <0.05° drift over 5 min at 5° bank.

# Dynamic gain scheduling.  The Mahony's effective kp_acc is multiplied
# by a [0, 1] factor computed each tick from current gyro rate and the
# magnitude of the centripetal-acceleration vector that's been
# subtracted from the accel reading.  At full quiescence (sitting on
# the ramp, no motion) factor = 1.0 and kp_acc is full strength.  In
# any real maneuver the factor falls to AHRS_DYN_KP_SCALE_MIN and the
# filter coasts on the gyro.
AHRS_DYN_GYRO_HI_DPS    = 15.0    # above this gyro rate, dyn factor = MIN
AHRS_DYN_GYRO_LO_DPS    = 3.0     # below this, dyn factor = 1.0
                                  # (linear ramp between)
AHRS_DYN_AC_HI_G        = 0.20    # above this centripetal mag, dyn factor = MIN
AHRS_DYN_AC_LO_G        = 0.03    # below this, dyn factor = 1.0
AHRS_DYN_KP_SCALE_MIN   = 0.10    # floor on the scaling factor
# Time constant (s) for the low-pass on the gyro that feeds the centripetal
# correction (a_c = w x V) and the gain scheduler.  A real coordinated turn
# rate is slow and sustained (~3 dps over seconds); residual gyro vibration is
# fast and zero-mean.  Feeding raw gyro into a_c manufactured ~1 g of fake
# centripetal (field capture), which failed the accel gate 90% of the time and
# pinned the gain schedule to its floor.  The full-rate gyro still drives the
# Mahony attitude integration; only the inertial-accel + scheduling math use
# this smoothed rate.  ~1 s rejects vibration while tracking turn entry/exit.
AHRS_CENTRI_GYRO_TAU_S  = 1.0
# Linear-acceleration aid: subtract forward dV/dt so acceleration/braking isn't
# read as a pitch change.  It needs a smooth, high-rate speed source.  On GPS
# groundspeed (1 Hz, quantized) the per-call differentiator turns each 1 kt step
# into a ~1 g phantom spike that wanders pitch +/-10-15 deg in cruise (field
# capture).  Disabled here for GPS-only installs; re-enable if/when a pitot
# (SDP33 -> TAS) feeds a smooth airspeed.  The dyn-gain schedule still throttles
# accel during real sustained acceleration, so leaving this off is safe.
AHRS_LINEAR_ACCEL_AID   = False
# Centripetal correction (a_c = w x V, subtracted so a coordinated turn's
# lateral accel isn't read as bank).  Needs a trustworthy gyro and speed.  In a
# vibrating cockpit on GPS-only speed it injects a steady 0.09-0.14 g that
# biases/roughens pitch and drops the accel out of the 1 g gate — so it's off
# by default, leaving a robust accel+gyro filter.  The magnitude gate and the
# gyro-rate gain schedule still de-weight the accel through real turns.  Re-
# enable once the gyro bias is well controlled and a smooth TAS is available.
AHRS_CENTRIPETAL_AID    = False
AHRS_KP_MAG             = 0.10    # mag proportional gain (yaw correction).
                                  # Lowered from 0.5 after AHRS-ROLL-YAW-COUPLING
                                  # showed the higher gain was over-trusting mag
                                  # transients during fast rotation through
                                  # non-uniform fields (bench clutter, panel iron
                                  # gradients).  Long-term yaw drift is anchored
                                  # by AHRS_GPS_TRACK_* slaving once in flight.
AHRS_ACCEL_GATE_G       = 0.20    # accel weight = 0 outside |a|=1g ± this band

# Gyro-rate mag gate.  Mag weight ramps linearly from full at |gyro| <=
# AHRS_MAG_GYRO_GATE_LO_DPS to zero at |gyro| >= AHRS_MAG_GYRO_GATE_HI_DPS.
# Suppresses mag corrections when the chip is rotating fast enough to be
# sweeping through position-dependent field gradients (bench tumble,
# aerobatic-grade rolls).  Standard-rate turns (~3°/s) stay well below
# the lo gate, so normal coordinated maneuvering is unaffected.
AHRS_MAG_GYRO_GATE_LO_DPS = 10.0
AHRS_MAG_GYRO_GATE_HI_DPS = 30.0

# Use the WT901 magnetometer (PKT_MAG 0x54) in the Mahony correction. If the
# WT901 isn't outputting mag packets the filter falls back to gyro-only yaw,
# corrected slowly by GPS track (see AHRS_GPS_TRACK_*).
AHRS_USE_MAG            = False   # TEMP TEST: the raw accel shows the unit is
                                  # level (gravity 6° off Z) but the filter sits
                                  # 56° off — an uncalibrated cockpit magnetometer
                                  # is fighting the accel and dragging pitch/roll.
                                  # With mag off, yaw is held by GPS-track slaving
                                  # (REQ-AHRS-SF-002). If pitch/roll now level,
                                  # mag is confirmed and needs a tumble cal.

# GPS-track yaw slaving. Once per AHRS_GPS_TRACK_INTERVAL_S, when the GPS
# fix is valid and groundspeed exceeds AHRS_GPS_TRACK_MIN_KT, nudge the
# filter yaw toward GPS track by AHRS_GPS_TRACK_ALPHA. Small values keep
# the short-term gyro response intact.
AHRS_GPS_TRACK_ENABLE     = True
AHRS_GPS_TRACK_MIN_KT     = 20.0   # GS below this → no yaw slaving
AHRS_GPS_TRACK_INTERVAL_S = 1.0    # seconds between corrections
AHRS_GPS_TRACK_ALPHA      = 0.02   # fraction of yaw error closed per call

# AHRS align banner — display shows "AHRS ALIGN" for this long after the
# filter first starts receiving gyro packets. Sized to cover the practical
# worst-case for the Mahony to converge from a poor PKT_ANGLE seed during
# a moving-on-power-up scenario. 20 s matches what we've seen in real
# bench/flight startup — filter is visibly still settling at 10 s.
AHRS_ALIGN_DURATION_S     = 20.0

# ── AHRS debug-print (TEMPORARY — investigating roll→yaw coupling) ──────────
# When True, the firmware emits a $AHRSDBG,... line over USB serial every
# AHRS_DEBUG_PRINT_DECIM sensor ticks. Each line carries the filter quaternion,
# sensor-frame Euler, body-frame Euler (post-remap), raw accel/gyro/mag, the
# mag cross-product error vector (ez_m is the smoking gun for roll-into-yaw
# coupling), the active accel weight, and the centripetal correction
# magnitude. Capture from a host with:
#     sudo systemctl stop pfd.service
#     python3 -m mpremote connect /dev/ttyACM0 | tee ahrs_debug.log
# then roll the AHRS unit ±30° about the bench-bank axis. Set False and
# re-flash once the bug is identified.
AHRS_DEBUG_PRINT          = True   # TEMP: diagnosing why the filter won't hold
                                   # gravity (grossly wrong, wandering pitch).
                                   # Capture $AHRSDBG with tools/ahrs_debug_capture.py,
                                   # then set back to False and re-flash.
AHRS_DEBUG_PRINT_DECIM    = 5      # at ~50 Hz tick rate → ~10 Hz print rate

# Firmware version date code — broadcast to the display so the pilot can
# verify which build is running on the AHRS. Bump manually on each
# meaningful release. YYYY-MM-DD format keeps it sortable and obvious.
FW_VERSION                = "2026-05-22"

# Aircraft "forward" unit vector expressed in the WT901 sensor frame.
# Used only for centripetal correction: a_c = ω_sensor × (V * fwd_sensor).
# Default assumes 'right' connector + 'normal' mounting: WT901 mounted
# label-up with the connector pointing to the right of the aircraft, so
# the sensor's +Y axis points along the aircraft's forward direction.
# If the centripetal correction makes turn behaviour WORSE rather than
# better, the sign or axis here is wrong — flip empirically (same workflow
# as WT901_AY_SIGN). Vector should be unit-magnitude.
AHRS_FWD_IN_SENSOR        = (0.0, 1.0, 0.0)

# ── Data broadcast rate ──────────────────────────────────────────────────────
BROADCAST_HZ = 10   # SSE events per second sent to the phone display
