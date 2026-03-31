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

# ── Data broadcast rate ──────────────────────────────────────────────────────
BROADCAST_HZ = 10   # SSE events per second sent to the phone display
