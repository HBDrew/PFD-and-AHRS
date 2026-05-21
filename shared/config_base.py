"""
config_base.py – Shared configuration for both Pi Zero 2W and Pi 4 display versions.

Contains V-speed defaults, barometric defaults, AHRS connection settings,
terrain/obstacle thresholds, demo presets, and flight simulator airports.

Platform-specific settings (display resolution, layout, rendering options)
are in each platform's own config.py which imports from this file.
"""

# ── Server ────────────────────────────────────────────────────────────────────
PICO_URL    = "http://192.168.4.1"        # Pico W AP address
SSE_URL     = PICO_URL + "/events"
SSE_TIMEOUT = 10                          # seconds before reconnect

# ── V-speeds (knots) – Cessna 172S defaults ───────────────────────────────────
VS0  =  48   # Stall speed, flaps down
VS1  =  55   # Stall speed, clean
VFE  =  85   # Max flap extension
VNO  = 129   # Max structural cruising
VNE  = 163   # Never exceed
VA   = 105   # Maneuvering (at gross)
VY   =  74   # Best rate of climb
VX   =  62   # Best angle of climb

# ── Barometric defaults ───────────────────────────────────────────────────────
BARO_DEFAULT_HPA = 1013.25

# ── Terrain / SVT thresholds ──────────────────────────────────────────────────
# Aligned with PALETTE_RELATIVE in shared/terrain.py — caution kicks in at the
# amber band, warning at the red band, so the central alert banner colour
# tracks the SVT terrain colour the pilot is already looking at.
TERRAIN_CAUTION_FT = 700    # terrain within 700 ft → amber alert (yellow band)
TERRAIN_WARNING_FT = 200    # terrain within 200 ft → red alert (red band)

# ── Obstacle database (FAA DOF) ───────────────────────────────────────────────
OBSTACLE_RADIUS_NM   = 5.0    # AI symbol render radius — beyond a few miles
                              # towers are too small to read and crowd the view
OBSTACLE_BELOW_FT    = 1000   # hide towers more than this far below aircraft
                              # (towers at or above us are always shown)
OBSTACLE_WINDOW_FT   = OBSTACLE_BELOW_FT  # back-compat alias — older code
                                          # used a symmetric ±window, the new
                                          # semantic is "below only" and
                                          # OBSTACLE_BELOW_FT is the source
                                          # of truth.  Kept so pi_zero keeps
                                          # importing without churn.
OBSTACLE_CAUTION_FT  = 500    # amber below this clearance
OBSTACLE_WARNING_FT  = 200    # red below this clearance — matches
                              # TERRAIN_WARNING_FT so the obstacle and
                              # terrain palettes flip to red on the same
                              # 200 ft buffer.
# Forward-look half-angle for the obstacle alert wedge. Bearings beyond
# this delta from the GPS ground track are filtered out — a tower 90°
# off the wingtip isn't a hazard and shouldn't fire the callout. ±25°
# is the middle of standard GA TAWS-B implementations (Honeywell EGPWS
# uses ±15° near-field expanding to ±30° at distance; we use a flat
# half-angle for simplicity since our look-ahead radius is already
# small relative to that ±15→30° dilation envelope).
OBSTACLE_WEDGE_HALF_DEG = 25.0
OBSTACLE_EXPIRY_DAYS = 28     # FAA DOF update cycle (days)

# ── Airport database (OurAirports CSV) ────────────────────────────────────────
AIRPORT_RADIUS_NM    = 20.0   # only show airports within this radius on AI
AIRPORT_LABEL_NM     = 15.0   # only show text label within this closer range
AIRPORT_EXPIRY_DAYS  = 60     # recommend refresh after 60 days (loose vs DOF's 28)

# ── Proximity alert lookahead ─────────────────────────────────────────────────
ALERT_TIME_S         = 60     # lookahead window in seconds
ALERT_RADIUS_MIN_NM  = 1.0    # floor — always check at least this far ahead
ALERT_RADIUS_MAX_NM  = 3.0    # ceiling — never exceed this radius

# ── Demo mode ─────────────────────────────────────────────────────────────────
DEMO_LAT = 34.8697
DEMO_LON = -111.7610
DEMO_ALT = 8500.0   # ft MSL starting altitude
DEMO_HDG = 133.0    # degrees

# ── Flight simulator presets ─────────────────────────────────────────────────
# Each entry: (icao, city_label, lat, lon, field_elev_ft)
SIM_PRESETS = [
    ("KSEZ", "Sedona AZ",        34.8486, -111.7884, 4830),
    ("KPHX", "Phoenix AZ",       33.4373, -112.0078, 1135),
    ("KDEN", "Denver CO",        39.8561, -104.6737, 5431),
    ("KLAX", "Los Angeles CA",   33.9425, -118.4081,  125),
    ("KSFO", "San Francisco CA", 37.6213, -122.3790,   13),
    ("KLAS", "Las Vegas NV",     36.0840, -115.1537, 2141),
    ("KSEA", "Seattle WA",       47.4502, -122.3088,  433),
    ("KOSH", "Oshkosh WI",       43.9844,  -88.5570,  808),
    ("KJFK", "New York NY",      40.6413,  -73.7781,   13),
    ("KORD", "Chicago IL",       41.9742,  -87.9073,  668),
    ("KDFW", "Dallas TX",        32.8998,  -97.0403,  603),
    ("KMIA", "Miami FL",         25.7959,  -80.2870,    8),
]

# ── Touch / interaction ───────────────────────────────────────────────────────
LONG_PRESS_MS    = 800  # ms for 2-finger long-press to enter setup
MFD_SWAP_HOLD_MS = 2000 # ms for 3-finger hold to swap PFD ↔ MFD on piZ
STALE_TIMEOUT_S  = 3    # seconds after link loss before AHRS is marked failed

# ── GPS heading complementary filter ─────────────────────────────────────────
GPS_HDG_SLAVE_K  = 0.05
