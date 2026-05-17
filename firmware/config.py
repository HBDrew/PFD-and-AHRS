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

# Mahony tuning. Defaults are conservative; bench-test then refine.
AHRS_KP_ACC             = 1.0     # accel proportional gain (rad/s per unit error)
AHRS_KI_ACC             = 0.001   # accel integral gain — estimates gyro bias.
                                  # Keep small: any steady residual cross-product
                                  # error (centripetal mismatch, sensor noise)
                                  # winds the integrator up. Bench tested: 0.001
                                  # gives <0.05° drift over 5 min at 5° bank.
AHRS_KP_MAG             = 0.5     # mag proportional gain (yaw correction)
AHRS_ACCEL_GATE_G       = 0.20    # accel weight = 0 outside |a|=1g ± this band

# Use the WT901 magnetometer (PKT_MAG 0x54) in the Mahony correction. If the
# WT901 isn't outputting mag packets the filter falls back to gyro-only yaw,
# corrected slowly by GPS track (see AHRS_GPS_TRACK_*).
AHRS_USE_MAG            = True

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
# a moving-on-power-up scenario. Filter typically settles in 2–3 s when
# stationary, but the conservative 10 s window matches G5/G3X conventions
# and gives the pilot time to notice the indicator.
AHRS_ALIGN_DURATION_S     = 10.0

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
