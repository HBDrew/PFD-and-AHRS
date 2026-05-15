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
#   SDP31-1500Pa        I2C1    GP2 SDA / GP3 SCL      addr 0x21, 3V3
#   (reserved AOA)     I2C1    GP2 SDA / GP3 SCL      addr 0x22 (future SDP3x)
#
# I²C1 is shared across BME280 + SDP31 + (future) AOA sensor — each device
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

# ── SDP31-1500Pa Differential Pressure (optional – airspeed sensor) ──────────
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

# ── Data broadcast rate ──────────────────────────────────────────────────────
BROADCAST_HZ = 10   # SSE events per second sent to the phone display
