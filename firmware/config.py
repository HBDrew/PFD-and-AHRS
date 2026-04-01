# ---------------------------------------------------------------------------
# config.py  –  Hardware pin assignments and system settings
# ---------------------------------------------------------------------------
#
# Wiring guide
# ============
#  WT901 AHRS  →  Pico W
#    VCC       →  3V3(OUT)   pin 36
#    GND       →  GND        pin 38
#    TXD       →  GP1        pin 2   (UART0 RX)
#    RXD       →  GP0        pin 1   (UART0 TX, only needed for config cmds)
#
#  GY-NEO6MV2  →  Pico W
#    VCC       →  VSYS       pin 39  (5 V tolerant; or 3V3 on some modules)
#    GND       →  GND        pin 38
#    TXD       →  GP5        pin 7   (UART1 RX)
#    RXD       →  GP4        pin 6   (UART1 TX, optional – only needed for UBX config)
#
# NOTE: UART0 on GP0/GP1 is shared with USB-serial debug.
#       During normal (non-debug) flight use this is fine.
#       If you need USB debug simultaneously, move WT901 to GP12/13 (UART0 alt)
#       and update WT901_TX_PIN / WT901_RX_PIN below.
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
AP_PASSWORD = "ahrs1234"   # min 8 chars for WPA2; set "" for open AP

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

# ── Data broadcast rate ──────────────────────────────────────────────────────
BROADCAST_HZ = 10   # SSE events per second sent to the phone display
