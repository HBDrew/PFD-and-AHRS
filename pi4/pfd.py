#!/usr/bin/env python3
"""
pfd.py – GI-275 inspired PFD for Raspberry Pi 4 with full OpenGL SVT.

This version targets the Pi 4's GPU (VideoCore VI) and will be migrated
to OpenGL ES vector graphics for true Synthetic Vision Technology (SVT)
including terrain rendering above the horizon line.

Currently runs the pygame-based renderer (inherited from the original
codebase).  The OpenGL migration is planned — see svt_renderer.py for
the scaffold.

Run:  python3 pfd.py           (connects to Pico W at 192.168.4.1)
      python3 pfd.py --demo    (Sedona demo, no hardware needed)
      python3 pfd.py --sim     (windowed for desktop testing)
"""

import math
import sys
import time
import threading
import argparse
import os
import io
import gzip
import socket
import subprocess
import urllib.request

# SDL/pygame audio: force ALSA before pygame imports anything else so
# SDL doesn't pre-pick PulseAudio/PipeWire (which ignore ~/.asoundrc
# and bypass the panel-speaker redirect). Must precede `import pygame`
# below — by the time pygame.init() runs SDL_Init reads this hint.
os.environ.setdefault("SDL_AUDIODRIVER", "alsa")

# Add shared modules to path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'shared'))

os.environ.setdefault("SDL_VIDEODRIVER", "kmsdrm")  # overridden by --sim
# Note: a previous version of this file hardcoded SDL_AUDIODRIVER=dummy
# here to silence ALSA underrun warnings — but that also silenced every
# voice callout. The TAWS/bank-angle audio depends on a real audio
# backend; we set SDL_AUDIODRIVER=alsa near the top instead and let the
# mixer use a large enough buffer to avoid underruns in normal operation.

import numpy as np
import pygame
import pygame.gfxdraw

from config import *   # noqa: F403
from sse_client import SSEClient
import adsb as _adsb
import fpllib as _fpllib
import adsb_feed as _afeed
import wx as _wx
import fisb as _fisb
import localtime as _localtime
import nexrad as _nexrad
import mapoverlay as _ovl
import perf as _perf_mod
from terrain import (get_elevation_ft,
                     set_tile_cache_max as _set_tile_cache_max,
                     set_srtm3_cache_dir as _set_srtm3_cache_dir)
from svt_renderer import render_svt as render_svt_pygame

# Parallel on-disk SRTM3 cache for the moving-map tint: keep full SRTM1 in
# data/srtm (the SVT 3D scene needs it) but read cheap pre-decimated ~2.8 MB
# tiles from data/srtm3 for the tint/TAWS-overlay.  Fills on demand the first
# time the tint touches a tile, so wide-zoom builds stop re-reading 26 MB
# files — critical on the 2 GB Pi 4's SD card.
_set_srtm3_cache_dir(os.path.join(os.path.dirname(SRTM_DIR), "srtm3"))

# Size the SRTM tile cache to available RAM.  Now that the moving-map tint
# cache key is stable (see _quantise_centre), the wide-zoom tint rebuilds
# only when the aircraft actually crosses a grid cell — not every frame — so
# we no longer need a huge tile cache to survive per-frame thrash.  Keep it
# modest so a 2-4 GB Pi 4 can't OOM (full SRTM1 tiles are ~26 MB each; the
# SVT 3D scene loads them at native res).  A Pi 5 with more RAM gets a larger
# warm working set.
def _ram_gb():
    try:
        with open("/proc/meminfo") as _f:
            for _l in _f:
                if _l.startswith("MemTotal"):
                    return int(_l.split()[1]) / 1024.0 / 1024.0
    except Exception:
        pass
    return 2.0
_mem_gb = _ram_gb()
_set_tile_cache_max(48 if _mem_gb >= 6.0 else (28 if _mem_gb >= 3.5 else 16))

# Try to load the OpenGL SVT renderer.  Falls back to pygame on failure.
# NOTE: GL SVT is disabled while we resolve EGL/KMS device contention on
# Pi 4 hardware — the standalone EGL context locks the V3D GPU and prevents
# pygame's KMS/DRM display from rendering.  The pygame scanline SVT works
# at ~17 fps in the meantime.  Set _FORCE_PYGAME_SVT = False to re-test.
# GL SVT availability is probed LAZILY on first render frame — not at
# import time — so pygame can grab KMS/DRM first without the EGL probe
# stealing the GPU device.  _SVT_GL_AVAILABLE starts as None (unknown)
# and gets set to True/False on the first frame.
# GL SVT disabled — EGL context creation disrupts KMS/DRM display on
# this Pi 4 kernel/mesa combination regardless of device_index or timing.
# Needs further investigation with a different EGL approach (perhaps
# piglit/gbm backend or sharing pygame's own GL context).
_SVT_GL_AVAILABLE = False
_gl_available = None
try:
    from svt_renderer_gl import render_svt_gl, render_svt_into_current_fb
except Exception:
    render_svt_gl = None
    render_svt_into_current_fb = None

# Shared-context composite path (pygame.OPENGL + moderngl).  Imported lazily
# because it's only needed when SVT_RENDERER == "opengl_shared".
try:
    from svt_composite_gl import setup_gl_display, Compositor
    HAS_SHARED_GL = True
except Exception:
    setup_gl_display = None
    Compositor = None
    HAS_SHARED_GL = False

# Populated by main() when opengl_shared setup succeeds.  render() and _flip()
# check `_shared_gl_ctx is not None` to take the GL composite path.
_shared_gl_ctx = None
_shared_gl_compositor = None

import obstacles as obs_mod
import airports as apt_mod
import airspaces as asp_mod
import navdata as nd_mod
import runways as rwy_mod
import water as water_mod
import settings as _settings
import screen_sync as _ssync_mod
import moving_map as _map_mod
import sun as _sun_mod
import hits as _hits_mod
import audio_alerts

DEG = math.pi / 180

# ── Colour palette ────────────────────────────────────────────────────────────
SKY_TOP    = ( 10,  42,  80)
SKY_HOR    = ( 58, 130, 200)
GND_HOR    = (130,  85,  45)
GND_BOT    = ( 60,  40,  20)
WHITE      = (255, 255, 255)
YELLOW     = (255, 215,   0)
CYAN       = (  0, 220, 220)
RED        = (220,  30,  30)
ORANGE     = (220, 100,   0)
GREEN_ARC  = ( 30, 200,  50)
YELLOW_ARC = (240, 200,   0)
TAPE_BG    = (  0,   8,  22, 195)
DIMGREY    = ( 80,  80,  90)
LTGREY     = (180, 180, 190)
MAGENTA    = (220,   0, 220)

# ── Shared state ─────────────────────────────────────────────────────────────
_state_lock = threading.Lock()
state = {
    "roll": 0.0, "pitch": 0.0, "yaw": 0.0, "ay": 0.0,
    "lat": DEMO_LAT, "lon": DEMO_LON,
    "speed": 0.0, "track": 0.0, "fix": 0, "sats": 0,
    "alt": 0.0, "gps_alt": 0.0, "vspeed": 0.0,
    "baro_src": "gps", "baro_hpa": BARO_DEFAULT_HPA,
    "pitch_trim": 0.0, "roll_trim": 0.0, "yaw_trim": 0.0,
    "ahrs_ok": False, "gps_ok": False, "gps_comm": False, "baro_ok": False,
    "orientation": "right", "mounting": "normal",
    # Echo-back of the Pico's input-side axis alignment (the source of
    # truth lives in disp["ss"]; this mirrors the broadcast so the UI
    # can show pending-vs-confirmed state).
    "pitch_align": 0.0, "roll_align": 0.0,
    "yaw_raw": 0.0,
}

# ── Display values (smoothed) ─────────────────────────────────────────────────
disp = dict(state)
disp["hdg_bug"]       = 0.0
disp["trk_bug"]       = 0.0  # GPS-track bug (used when heading source is "trk")
disp["alt_bug"]       = 0.0
disp["baro_hpa"]      = BARO_DEFAULT_HPA
disp["show_demo"]     = False
disp["mode"]          = "pfd"       # "pfd"|"setup"|"flight_profile"|"numpad"|"keyboard"
                                     # |"display_setup"|"ahrs_setup"|"connectivity_setup"|"system_setup"
                                     # |"sim_setup"|"sim_controls"
disp["numpad_target"] = ""          # "alt_bug"|"hdg_bug"|"spd_bug"|fp key
disp["numpad_buf"]    = ""          # digits entered so far
disp["numpad_prev"]   = "pfd"       # mode to return to on cancel/enter
disp["kbd_target"]    = ""          # field being edited in keyboard mode
disp["kbd_buf"]       = ""          # text entered so far
disp["kbd_prev"]      = "flight_profile"  # mode to return to on DONE/CANCEL
disp["fp"] = {                      # flight-profile values
    "tail":   "N12345", "actype": "C172S",
    "vs0":    VS0,  "vs1": VS1,  "vfe": VFE,
    "vno":    VNO,  "vne": VNE,  "va":  VA,
    "vy":     VY,   "vx":  VX,
}
disp["display_mode"]  = "pfd"       # "pfd" | "mfd" — runtime view selector
                                    # (3-finger hold swaps; boots to PFD)
disp["td"] = {                      # terrain download state
    "downloading": False,
    "dl_region":   "",
    "dl_current":  0,
    "dl_total":    0,
    "dl_status":   "",
    "dl_cancel":   False,
}
disp["wd"] = {                      # water-mask rasterise state (companion
    "downloading": False,           # to terrain download — produces a .water
    "dl_current":  0,               # file per existing SRTM tile from the
    "dl_total":    0,               # Natural Earth ocean+lakes shapefiles)
    "dl_status":   "",
    "dl_cancel":   False,
}
disp["fw"] = {                      # AHRS firmware update state
    "push_state": "", "push_msg": "",
    "flash_state": "", "flash_msg": "",
}
disp["od"] = {                      # obstacle download/parse state
    "downloading": False,
    "dl_status":   "",
    "dl_cancel":   False,
    "parsing":     False,
    "records":     0,       # record count after successful load
    "used_mb":     0.0,
    "dl_date":     None,    # datetime.date of last download (or None)
    "expired":     False,   # True when file is > OBSTACLE_EXPIRY_DAYS old
    "age_days":    0,
}
_obstacles = None           # loaded obstacle array (module-level)
_airports  = None           # loaded airport array (module-level)
_airspaces = None           # list of airspace records, or None until loaded
_navdata   = None           # NavData (fixes/navaids/procedures), or None
# disp["asp"] mirrors disp["ad"]/disp["td"]/disp["od"]: status fields
# surfaced on the AIRSPACE DATA subscreen.  Not persisted (recomputed
# on load).
disp["asp"] = {
    "records":     0,
    "downloading": False,
    "dl_status":   "",
    "dl_cancel":   False,
}
# disp["nd"] mirrors disp["ad"]: download/status fields for the NAV DATA
# subscreen + the NAV tile on the DATA & MAPS page.
disp["nd"] = {
    "downloading": False,
    "dl_status":   "",
    "dl_cancel":   False,
    "present":     False,
    "cycle":       "",
    "issued":      "",
    "fixes":       0,
    "navaids":     0,
    "airways":     0,
    "procedures":  0,
    "holds":       0,
    "mb":          0.0,
    "date":        None,
    "age_days":    0,
    "expired":     False,
}
_runways   = None           # loaded runway array (module-level)
disp["ad"] = {                      # airport download/parse state
    "downloading": False,
    "dl_status":   "",
    "dl_cancel":   False,
    "parsing":     False,
    "records":     0,
    "used_mb":     0.0,
    "dl_date":     None,    # datetime.date of last download
    "expired":     False,   # True when CSV is > AIRPORT_EXPIRY_DAYS old
    "age_days":    0,
    # Per-category display filters (bool).  Users toggle these on the
    # AIRPORT DATA screen to reduce clutter.
    "show_public":   True,      # S/M/L — small/medium/large airports
    "show_heli":     True,      # H — heliports
    "show_seaplane": False,     # W — seaplane bases
    "show_other":    False,     # B — balloonports + misc
    # Runway + extended-centerline rendering (Pi 4 primarily; Pi Zero
    # renders the polygons as 2D projections too).
    "show_runways":     True,
    "show_centerlines": True,
    # SVT water bodies (oceans + lakes from Natural Earth, rasterised into
    # the data/water/ companion of the SRTM tiles).  Off-by-default has no
    # effect when no .water tiles are present.
    "show_water":       True,
}
disp["ds"] = {                      # display settings
    "spd_unit":  "kt",   "alt_unit":   "ft",
    "baro_unit": "inhg", "brightness": 8,  "night_mode": False,
    # Audio callouts (TERRAIN / PULL UP / BANK ANGLE).  Volume is 0–10
    # to match the rest of the integer settings; the audio module maps
    # it to a 0.0–1.0 multiplier internally. 0 = effectively muted but
    # the master switch is the cleaner toggle for "I want silence".
    "audio_enabled": True, "audio_volume": 8,
    # Flight-path vector (velocity vector) marker on the AI.  Default ON —
    # it's a primary instrument cue and self-hides below 5 kt GS.
    "fpv_enabled":   True,
    # Highway-in-the-sky approach corridor (cyan boxes on the SVT).  Default ON.
    "hits_enabled":  True,
    # Lower-left moving-map inset
    "map_enabled":       False,
    "map_orient":        "trk",     # "trk" | "nrth"
    "map_zoom_nm":       5,         # one of 1, 2, 5, 10, 20, 40
    "map_show_terrain":  True,
    "map_show_water":    True,      # reserved — water tile overlay
    "map_show_airports": True,
    "map_show_runways":  True,
    "map_show_obstacles": True,
    "map_show_traffic":  True,      # ADS-B traffic diamonds (when fed GDL90)
    # Traffic declutter (0 = show all).  alt band is ± relative feet;
    # range is nautical miles.  Alert-class threats ignore both.
    "traffic_alt_band":  0,
    "traffic_range_nm":  0,
    "map_show_metar":    False,  # METAR station dots (off until WX selected)
    "map_show_winds":    False,  # winds-aloft barbs (off until WND selected)
    "winds_alt_ft":      9000,   # selected altitude for the winds barbs
    "winds_time_offset_h": 0,    # WND forecast time: hours ahead of now (0 = now)
    # The WND overlay keeps its OWN zoom (40/80/160 — it never needs a close-in
    # view) so it doesn't disturb the terrain-map zoom when you switch pages.
    "winds_zoom_nm":     80,
    "map_show_nexrad":   False,  # NEXRAD reflectivity raster (off until selected)
    # Full-screen MFD: gate for the 3-finger PFD↔MFD swap.  Default on for
    # the larger HDMI screens (that's where a full-screen map earns its keep).
    "mfd_enabled":       True,
    # MFD bottom data strip — 8 user-selectable readout slots.
    # Convention: a bare nav label is to the ACTIVE WAYPOINT; the "…D" variant
    # is to the FINAL DESTINATION (whole route).  eta_local flips ALL arrival
    # clocks between local time and Zulu.
    "mfd_strip_kinds":  ["gs", "trk", "alt", "wpt", "btw", "dist", "ete", "etad"],
    "eta_local":        True,    # ETA/ETAD shown in local time (False = Zulu)
    # PFD top readout ribbon — 5 slots in the band above the AI (tap it to
    # configure).  Same readout kinds as the MFD strip; defaults to fields that
    # complement the tapes rather than duplicate them.
    "pfd_top_kinds":    ["agl", "tas", "oat", "wind", "etad"],
    "map_show_state_lines": True,   # admin_1 boundaries at >= 20 nm
    "map_show_country_lines": True, # admin_0 boundaries at >= 20 nm
    "map_show_directto": True,
    # Airspace overlay — off by default until the pilot has a real
    # NASR-derived file on disk.  Per-class toggles let pilots hide
    # MOAs / restricted / etc. when not relevant.
    "map_show_airspaces":    False,
    "map_show_airspace_b":   True,
    "map_show_airspace_c":   True,
    "map_show_airspace_d":   True,
    "map_show_airspace_moa": True,
    "map_show_airspace_r":   True,
    "map_show_airspace_p":   True,
    "map_show_airspace_tfr": True,
    # Real-time SVT sun position (off → SE / mid-morning fixed lighting)
    "sun_realtime":      True,
}
disp["ss"] = {                      # AHRS / sensor settings
    "pitch_trim":    0.0, "roll_trim": 0.0,
    "mag_cal":       "idle", "mounting": "normal",
    # Axis alignment — degrees of compensation around the airframe
    # pitch and roll axes.  See firmware/_apply_axis_align.  Tunable on
    # the AHRS setup screen; pushed to the Pico via $ALIGN.
    "pitch_align":   0.0, "roll_align": 0.0,
    "mag_cal_deltas": [0.0] * 4,     # per-cardinal (N/E/S/W) heading
                                      # corrections from the compass-cal
                                      # wizard.  Piecewise-linear
                                      # interpolation between cardinals
                                      # gives each 90° quadrant its own
                                      # correction curve.

    # Heading source — matches the iPhone display:
    #   "mag"  : magnetic heading from AHRS (cyan/white "M" subscript).
    #   "trk"  : GPS ground track via complementary filter (magenta "G").
    #            Falls back to "G?" when GPS fix is missing or speed < 3 kt
    #            (track is unreliable below taxi speed).
    #   "auto" : prefer "trk" when GPS is moving, fall back to "mag" otherwise.
    "hdg_src":       "auto",
    "airspeed_src":  "gps",   # "gps" | "ias"  — speed source (GPS groundspeed or IAS sensor)
}
disp["cs"] = {                      # connectivity settings
    "ahrs_url":  PICO_URL, "wifi_ssid": "AHRS-Link",
    "wifi_pass": "",        "wifi_ok":  False,
    "wifi_actual": "",      # SSID actually associated now (from iwgetid -r)
    # FAA NMS-API NOTAM credentials (entered in Connectivity).  KEY = client_id,
    # SECRET = client_secret, from the FAA NMS onboarding sheet.  Persisted
    # (plaintext); the poller reads them live, so entering them enables NOTAMs
    # without a reboot.  notam_env selects the host (preprod | prod).
    "notam_client_id": "",  "notam_client_secret": "",
    "notam_env": "preprod",
    "scan_state": "",   "scan_nets": [], "scan_scroll": 0, "scan_error": "",
    "ahrs_ok":   False,     "test_msg": "", "apply_msg": "",
    # AHRS link diagnostics (populated by the transport client thread)
    "ahrs_transport": "",   # "usb" | "wifi" | ""
    "ahrs_port":      "",   # /dev/ttyACM0 or the SSE URL
    "ahrs_rx":        0,    # count of $AHRS, lines parsed OK
    "ahrs_err":       0,    # count of parse / IO errors
    "ahrs_last_err":  "",   # most recent error message
    # Screen-to-screen sync.  Each category is opt-in per direction so a
    # screen with its own AHRS can still publish bugs/baro/nav to a
    # second screen, etc.  All default off; toggled in the Screen Sync
    # subscreen.
    "sync_enabled":   True,     # master ON/OFF — when False, all sync stops
    "sync_transport": "auto",   # "auto" | "usb" | "net"
    "sync_publish_bugs": False, "sync_consume_bugs": False,
    "sync_publish_baro": False, "sync_consume_baro": False,
    "sync_publish_nav":  False, "sync_consume_nav":  False,
    "sync_publish_ahrs": False, "sync_consume_ahrs": False,
    "sync_publish_gps":  False, "sync_consume_gps":  False,
    "sync_fpl_enabled":  True,   # single SHARE FPL toggle (both ways, no echo)
    # ADS-B IN — listen for GDL90 traffic on UDP.  Diagnostics mirror the
    # AHRS link fields so the Connectivity screen can show an ADS-B row.
    "adsb_enabled":   True,
    "traffic_source": "auto",   # "auto" | "radio" | "internet" (built-in feed)
    "wx_source":      "auto",   # "auto" | "radio" (FIS-B) | "internet" (AWC)
    "adsb_port":      ADSB_UDP_PORT,
    "adsb_online":    False,
    "adsb_rx":        0,
    "adsb_err":       0,
    "adsb_last_err":  "",
    "adsb_uplink":    0,
    # Internet weather (METARs) — needs internet (AHRS on USB, or all on a
    # shared Starlink LAN).  Diagnostics are runtime-only.
    "wx_enabled":     True,
    "wx_online":      False,
    "wx_rx":          0,
    "wx_err":         0,
    "wx_last_err":    "",
}
# Live ADS-B traffic — refreshed each frame from the GDL90/UDP listener
# (or the demo generator).  Targets are relativised + threat-classified
# and sorted nearest-first.  Not persisted.
disp["traffic"] = {
    "targets": [], "online": False, "n": 0, "n_total": 0, "alert": False,
}
# Live weather — METAR stations near the aircraft (internet poller). Not persisted.
disp["weather"] = {
    "metars": [], "online": False, "n": 0,
    "n_rdr": 0, "n_inet": 0,        # split: FIS-B radio vs internet METARs
}
# When set (tapping a METAR dot on the MFD), holds the station for the
# decoded-METAR readout panel.  None = no panel.
disp["wx_popup"] = None
# When set, holds a coincident airport+METAR tap so the pilot picks which
# one they meant (Weather readout vs Direct-To).  None = no chooser.
disp["mfd_pick"] = None
# True while the quick MAP LAYERS panel is open on the MFD (tapping the
# layers icon under the N↑/TRK↑ toggle) — toggle layers without entering setup.
disp["mfd_layers"] = False
# MFD pan offset.  lat/lon None = follow the aircraft; set = panned map.
disp["mfd_pan"] = {"lat": None, "lon": None}
# Mirror of the peer's flight plan (received over screen sync).  The
# Pi 4 PFD doesn't edit FPLs today — it just renders the active leg
# (via disp["nav"]) and the multi-leg polyline on its inset map.  When
# the user adds an editor on this side, the existing _ssync_publish_fpl
# pattern from pi_zero can drop in here verbatim.
disp["fpl"] = {
    "waypoints":  [],
    "active_idx": -1,
}
# Saved named flight plans (persistent).  Same schema as pi_zero so a plan
# saved on either screen loads on the other.
disp["fpl_saved"] = {"plans": [],   # [{name, waypoints:[...], ts}]
                     "deleted": {}}  # tombstones {NAME_UPPER: deleted_ts}
# User-waypoint library (persistent) — +LAT/LON entries auto-save here.
disp["user_wpts"] = {"list": []}    # [{ident, lat, lon, elev_ft}, ...]
# In-progress +LAT/LON user-waypoint entry (transient; cleared on save/cancel).
disp["fpl_new"] = {"ident": "", "lat": 0.0, "lon": 0.0,
                   "lat_str": "", "lon_str": "", "source": ""}
disp["sim"] = {                     # flight simulator state
    "preset_idx": 0,    # index into SIM_PRESETS
    "init_alt":   5000.0,
    "init_hdg":   0.0,
    "init_spd":   90.0,
    "gps_fail":   False,
    "baro_fail":  False,
    "ahrs_fail":  False,
    # Autopilot source for the sim:
    #   "bugs" — follow hdg_bug + alt_bug (default — what the sim has
    #             always done).
    #   "fp"   — follow the active direct-to (bearing to waypoint) +
    #             alt_bug; if a synthetic approach is active, slide
    #             down the 3° glideslope to the threshold once the
    #             aircraft has intercepted it.
    "follow_mode": "bugs",
    # Pause flag — set from the sim_controls overlay's PAUSE button.
    # When True, _sim_state.tick() is skipped in the main loop so the
    # aircraft state holds steady while the rest of the UI stays live.
    "paused": False,
}
disp["nav"] = {                     # rudimentary direct-to-airport navigation
    "ident":   "",      # ICAO/local ID of active waypoint, "" = none
    "lat":     0.0,
    "lon":     0.0,
    "elev_ft": 0.0,
    "act_lat": 0.0,     # aircraft lat at activation (CDI course reference)
    "act_lon": 0.0,
}

# Synthetic approach (HITS).  Active when the pilot has selected a
# specific runway end via the APPR picker.  When set, the renderer
# draws cyan HITS boxes along the extended centreline at a 3°
# glideslope, and the direct-to is auto-pointed at the threshold so
# the CDI / ETE line up with the runway instead of the airport
# centroid.  Cleared on PFD restart (not persisted) — fresh approach
# each flight.
disp["approach"] = {
    "active":          False,
    "airport":         "",         # parent airport ident (e.g. "KSEZ")
    "runway":          "",         # runway-end ident (e.g. "03")
    "thresh_lat":      0.0,
    "thresh_lon":      0.0,
    "thresh_elev_ft":  0.0,
    "course_deg":      0.0,        # true course TO the threshold
}

SMOOTH_K = 0.25   # IIR coefficient (higher = faster response)

# Heading-source resolution thresholds — match the iPhone display.
# 7 kt sits comfortably above GPS GS noise floor and below typical taxi
# speeds; below it AUTO mode stays on MAG and TRK mode reports "G?" amber.
HDG_TRK_MIN_KT = 7.0   # GPS groundspeed below this → track is unreliable


def _resolve_hdg_source(hdg_src_pref, gps_ok, ahrs_ok, speed_kt):
    """Resolve the user's preference (hdg_src in {"mag","trk","auto"}) +
    runtime conditions into the active source, label, and colour shown in
    the heading box / on the tape.

    Mirrors iphone_display/index.html#_activeHdg so the two displays use
    identical UX:

        Returns (use_track: bool, label: str, color: tuple).

    label is one of "M" / "G" / "M?" / "G?" / "?" ; color is white for
    valid magnetic, MAGENTA for valid GPS track, amber for unavailable.
    """
    track_ok = gps_ok and (speed_kt or 0.0) > HDG_TRK_MIN_KT
    mag_ok   = ahrs_ok

    if hdg_src_pref == "mag":
        if mag_ok:
            return False, "M", WHITE
        return False, "M?", AMBER
    if hdg_src_pref == "trk":
        if track_ok:
            return True, "G", MAGENTA
        # Track unavailable (no GPS fix or below the speed threshold).
        # Fall back to MAG rather than keep returning use_track=True —
        # otherwise the GPS-slaved complementary filter has nothing to
        # slave against and the displayed heading drifts toward stale
        # GPS track (typically 0° on a stationary aircraft).  Amber
        # "G?" label still surfaces that the pilot's chosen source
        # isn't currently usable.
        if mag_ok:
            return False, "G?", AMBER
        return False, "?", AMBER
    # auto: prefer TRK when GPS is moving, fall back to MAG
    if track_ok:
        return True, "G", MAGENTA
    if mag_ok:
        return False, "M", WHITE
    return False, "?", AMBER

# ── Module-level SSE handle (set in main, restarted by handle_event) ─────────
_sse_client  = None
_adsb_client = None   # ADSBClient (GDL90/UDP traffic) when ADS-B enabled
_traffic_feed = None  # TrafficFeed (built-in internet feed) — paused per traffic_source
_prev_alert_ids = set()  # ICAOs already in alert state (edge-trigger the callout)

# Traffic recompute throttle.  _update_traffic relativises + threat-classifies
# every target against ownship; the PFD_CPROFILE_SEC profile showed ~290
# targets recomputed EVERY frame costing ~14 ms/frame on a Pi 4.  ADS-B only
# refreshes ~1 Hz, so doing this at frame rate is ~29× wasted work.  Run it at
# a few Hz instead — far faster than the data, invisible on screen, and still
# well ahead of any threat callout.  PFD_TRAFFIC_HZ overrides for tuning.
try:
    _TRAFFIC_UPDATE_DT = 1.0 / max(1.0, float(os.environ.get("PFD_TRAFFIC_HZ", "5") or 5))
except ValueError:
    _TRAFFIC_UPDATE_DT = 0.2

_perf = _perf_mod.PerfGrab()   # frame-timing sampler (no-op unless PFD_PERF set)


def _soc_thermals():
    """Return (temp_c, throttled_hex) for the SoC, or (None, None) off-Pi.

    Temperature is read straight from sysfs (no subprocess).  The throttle
    word needs vcgencmd, which we cache-off after the first failure so dev
    boxes / sims pay nothing.  Used by the per-frame diagnostic line so a
    perf run shows whether the Pi 4/5 is thermally or power throttling
    (throttled=0x0 means neither — the measured fps is the real ceiling).
    """
    temp_c = None
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as _f:
            temp_c = int(_f.read().strip()) / 1000.0
    except (OSError, ValueError):
        pass
    thr = None
    if getattr(_soc_thermals, "_vc_ok", True):
        try:
            import subprocess
            out = subprocess.run(["vcgencmd", "get_throttled"],
                                 capture_output=True, text=True, timeout=1.0)
            thr = out.stdout.strip().split("=", 1)[-1] or None
        except (OSError, ValueError, subprocess.SubprocessError):
            _soc_thermals._vc_ok = False
    return temp_c, thr
_wx_client   = None   # WxClient (internet METAR poller) when weather enabled
_taf_client  = None   # AwcPoller (internet TAF backfill) when weather enabled
_airsig_client = None # AwcPoller (internet AIRMET/SIGMET backfill)
_winds_client = None  # WindsUSCache (zoned national winds aloft, disk-cached)
_WINDS_DISK_PATH = os.path.join(os.path.dirname(SRTM_DIR), "winds",
                                "conus_winds.json")
_notam_client = None  # AwcPoller (internet NOTAMs, FAA API) when creds present
_taf_fed_at  = 0.0    # updated_s of last TAF snapshot folded into the store
_airsig_fed_at = 0.0  # updated_s of last AIRMET/SIGMET snapshot folded in
_winds_fed_at = 0.0   # updated_s of last winds snapshot folded in
_notam_fed_at = 0.0   # updated_s of last NOTAM snapshot folded in
_notam_pub_at = 0.0   # monotonic time of last NOTAM re-broadcast to peers
_nexrad_client = None # NexradClient (internet radar poller) when enabled
_sim_state   = None   # SimFlyState instance when sim is running, else None
# Decoded NEXRAD image cache (pygame surface); re-decoded only on new image.
_nexrad_decoded = {"seq": -1, "surf": None, "bbox": None}
_link_lost_t = None   # monotonic timestamp when link first dropped (None if connected)

# ── Screen-to-screen sync ────────────────────────────────────────────────────
# Created in main() once disp["cs"] has been restored from settings.json.
_screen_sync = None
_ssync_suppress_publish = 0
_ssync_last_ahrs_t = 0.0
_ssync_last_gps_t  = 0.0
_SSYNC_AHRS_MIN_DT = 0.05    # 20 Hz upper bound
_SSYNC_GPS_MIN_DT  = 0.20    # 5 Hz upper bound


def _ssync_kinds_from_cs(direction):
    """Build the set of categories whose `direction` ("publish" / "consume")
    toggle is on in disp["cs"]."""
    out = set()
    cs = disp.get("cs", {})
    for k in (_ssync_mod.KIND_BUGS, _ssync_mod.KIND_BARO,
              _ssync_mod.KIND_NAV,  _ssync_mod.KIND_AHRS,
              _ssync_mod.KIND_GPS):
        if cs.get(f"sync_{direction}_{k}", False):
            out.add(k)
    # Flight plans are ALWAYS bidirectional when sharing is on — no
    # master/slave.  Active plan (KIND_FPL) + saved-plan/user-wpt library
    # (KIND_FPLLIB) sync both ways.  A single SHARE FPL toggle gates both
    # directions (no separate TX/RX) to avoid echo confusion.
    if cs.get("sync_fpl_enabled", True):
        out.add(_ssync_mod.KIND_FPL)
        out.add(_ssync_mod.KIND_FPLLIB)
        out.add(_ssync_mod.KIND_APPR)        # approach rides with the plan
        out.add(_ssync_mod.KIND_NAV)         # direct-to / active fix rides too,
                                             # so a D2 on one screen drives the
                                             # other's CDI + AP (its own NAV
                                             # toggle still works independently)
    # Winds-aloft zones always sync both ways when screen-sync is on — it's pure
    # benefit (a screen with internet feeds the others so they don't each hit
    # Open-Meteo's shared-per-IP rate limit).
    out.add(_ssync_mod.KIND_WINDS)
    # NOTAMs ride the same always-on, both-ways model as winds: a screen with the
    # FAA key feeds the fetched NOTAMs to peers that have no key.  NOTAM creds
    # ride along too so the key can be entered once and pushed to every display.
    out.add(_ssync_mod.KIND_NOTAMS)
    out.add(_ssync_mod.KIND_NOTAMCREDS)
    return out


def _ssync_apply_winds(data):
    """Screen-sync KIND_WINDS callback — adopt a peer's winds zone."""
    if _winds_client is not None:
        _winds_client.ingest_packed(data)


def _winds_publish(packed):
    """publish_fn for WindsUSCache — broadcast a zone to peer screens."""
    if _screen_sync is not None:
        _screen_sync.publish(_ssync_mod.KIND_WINDS, packed)


# Cap the NOTAM list shared per packet — screen_sync sends one UDP datagram with
# no chunking, and the tight NOTAM radius already keeps the count modest.
_NOTAM_SHARE_MAX = 120


def _ssync_publish_notams(texts):
    """Broadcast our fetched NOTAM list to peer screens (one keyed display feeds
    the LAN — same model as winds).  Only the fetcher has a non-empty list."""
    if _screen_sync is not None and texts:
        _screen_sync.publish(_ssync_mod.KIND_NOTAMS,
                             {"notams": list(texts)[:_NOTAM_SHARE_MAX]})


def _ssync_apply_notams(data):
    """Screen-sync KIND_NOTAMS callback — adopt a peer's fetched NOTAMs so a
    display without its own FAA key still shows them."""
    store = getattr(_adsb_client, "fisb", None) if _adsb_client else None
    if store is not None:
        store.add_notams(data.get("notams", []))


def _ssync_push_notam_creds():
    """Broadcast the FAA NOTAM credentials to peer screens — entered once,
    stored on every display.  Called when a NOTAM cred field is committed."""
    if _screen_sync is None:
        return
    cs = disp.get("cs", {})
    _screen_sync.publish(_ssync_mod.KIND_NOTAMCREDS, {
        "client_id":     cs.get("notam_client_id", ""),
        "client_secret": cs.get("notam_client_secret", ""),
        "env":           cs.get("notam_env", "preprod"),
    })


def _ssync_apply_notamcreds(data):
    """Screen-sync KIND_NOTAMCREDS callback — adopt pushed FAA NOTAM creds and
    persist locally.  Only non-empty fields overwrite, so a partial push (key
    entered before secret) never clears a value already on a peer."""
    cs = disp.get("cs", {})
    changed = False
    for fld, src in (("notam_client_id", "client_id"),
                     ("notam_client_secret", "client_secret"),
                     ("notam_env", "env")):
        v = (data.get(src) or "").strip()
        if fld == "notam_env":
            v = v.lower()
        if v and v != cs.get(fld, ""):
            cs[fld] = v
            changed = True
    if changed:
        _settings.mark_dirty()


def _ssync_refresh_kinds():
    """Push every screen-sync setting from disp["cs"] into the live
    ScreenSync — enable flag, transport selector, and the 10 TX/RX
    category toggles.  Called whenever the user changes any of them."""
    if _screen_sync is None:
        return
    cs = disp.get("cs", {})
    _screen_sync.set_enabled(cs.get("sync_enabled", True))
    _screen_sync.set_transport(cs.get("sync_transport", "auto"))
    _screen_sync.set_publish_kinds(_ssync_kinds_from_cs("publish"))
    _screen_sync.set_consume_kinds(_ssync_kinds_from_cs("consume"))


def _ssync_publish_bugs():
    if _screen_sync is None or _ssync_suppress_publish:
        return
    _screen_sync.publish(_ssync_mod.KIND_BUGS, {
        "alt_bug": float(disp.get("alt_bug", 0.0)),
        "spd_bug": float(disp.get("spd_bug", 0.0)),
        "hdg_bug": float(disp.get("hdg_bug", 0.0)),
        "vs_bug":  float(disp.get("vs_bug",  0.0)),
    })


def _ssync_apply_bugs(data):
    global _ssync_suppress_publish
    _ssync_suppress_publish += 1
    try:
        if "alt_bug" in data:
            disp["alt_bug"] = float(data["alt_bug"])
        if "spd_bug" in data:
            disp["spd_bug"] = float(data["spd_bug"])
        if "hdg_bug" in data:
            disp["hdg_bug"] = float(data["hdg_bug"])
        if "vs_bug" in data:
            disp["vs_bug"] = float(data["vs_bug"])
    finally:
        _ssync_suppress_publish -= 1


def _ssync_publish_baro():
    if _screen_sync is None or _ssync_suppress_publish:
        return
    _screen_sync.publish(_ssync_mod.KIND_BARO, {
        "baro_hpa": float(disp.get("baro_hpa", BARO_DEFAULT_HPA)),
    })


def _ssync_apply_baro(data):
    global _ssync_suppress_publish
    _ssync_suppress_publish += 1
    try:
        if "baro_hpa" in data:
            new_hpa = float(data["baro_hpa"])
            disp["baro_hpa"] = new_hpa
            try:
                with _state_lock:
                    state["baro_hpa"] = new_hpa
                _push_baro_to_pico(new_hpa)
            except Exception:
                pass
    finally:
        _ssync_suppress_publish -= 1


def _ssync_publish_nav():
    if _screen_sync is None or _ssync_suppress_publish:
        return
    nav = disp.get("nav", {})
    _screen_sync.publish(_ssync_mod.KIND_NAV, {
        "ident":   nav.get("ident", ""),
        "lat":     float(nav.get("lat", 0.0)),
        "lon":     float(nav.get("lon", 0.0)),
        "elev_ft": float(nav.get("elev_ft", 0.0)),
        "act_lat": float(nav.get("act_lat", 0.0)),
        "act_lon": float(nav.get("act_lon", 0.0)),
    })


def _ssync_apply_nav(data):
    global _ssync_suppress_publish
    _ssync_suppress_publish += 1
    try:
        ident = str(data.get("ident", ""))
        if not ident:
            disp["nav"]["ident"]   = ""
            disp["nav"]["lat"]     = 0.0
            disp["nav"]["lon"]     = 0.0
            disp["nav"]["elev_ft"] = 0.0
            disp["nav"]["act_lat"] = 0.0
            disp["nav"]["act_lon"] = 0.0
        else:
            disp["nav"]["ident"]   = ident
            disp["nav"]["lat"]     = float(data.get("lat", 0.0))
            disp["nav"]["lon"]     = float(data.get("lon", 0.0))
            disp["nav"]["elev_ft"] = float(data.get("elev_ft", 0.0))
            disp["nav"]["act_lat"] = float(data.get("act_lat", 0.0))
            disp["nav"]["act_lon"] = float(data.get("act_lon", 0.0))
    finally:
        _ssync_suppress_publish -= 1


def _ssync_publish_ahrs():
    global _ssync_last_ahrs_t
    if _screen_sync is None or _ssync_suppress_publish:
        return
    if not _screen_sync.publish_enabled(_ssync_mod.KIND_AHRS):
        return
    now = time.monotonic()
    if now - _ssync_last_ahrs_t < _SSYNC_AHRS_MIN_DT:
        return
    _ssync_last_ahrs_t = now
    _screen_sync.publish(_ssync_mod.KIND_AHRS, {
        "pitch":   float(disp.get("pitch", 0.0)),
        "roll":    float(disp.get("roll",  0.0)),
        "yaw":     float(disp.get("yaw",   0.0)),
        # Include health so the receiving screen can mark its attitude
        # indicator as live (no red X) when a peer's Pico is sourcing it.
        "ahrs_ok": bool(disp.get("ahrs_ok", False)),
    })


def _ssync_apply_ahrs(data):
    global _ssync_suppress_publish
    _ssync_suppress_publish += 1
    try:
        with _state_lock:
            if "pitch" in data:
                state["pitch"] = float(data["pitch"])
            if "roll" in data:
                state["roll"]  = float(data["roll"])
            if "yaw" in data:
                state["yaw"]   = float(data["yaw"])
            if "ahrs_ok" in data:
                state["ahrs_ok"] = bool(data["ahrs_ok"])
    finally:
        _ssync_suppress_publish -= 1


def _ssync_publish_gps():
    """Broadcast GPS + altitude + airspeed sensor outputs at most 5 Hz.

    The "gps" category is the convenience bucket for everything the
    Pico produces that isn't attitude — position, fused altitude / VS,
    baro source flag, MS4525/SDP3x air-data.  On a setup where one
    screen has the Pico and the other shadows it, RX of this category
    is what makes the altimeter tape and airspeed source work without
    a local sensor.
    """
    global _ssync_last_gps_t
    if _screen_sync is None or _ssync_suppress_publish:
        return
    if not _screen_sync.publish_enabled(_ssync_mod.KIND_GPS):
        return
    now = time.monotonic()
    if now - _ssync_last_gps_t < _SSYNC_GPS_MIN_DT:
        return
    _ssync_last_gps_t = now
    _screen_sync.publish(_ssync_mod.KIND_GPS, {
        # Position
        "lat":         float(disp.get("lat", 0.0)),
        "lon":         float(disp.get("lon", 0.0)),
        "gps_alt":     float(disp.get("gps_alt", 0.0)),
        "speed":       float(disp.get("speed", 0.0)),
        "track":       float(disp.get("track", 0.0)),
        "gps_ok":      bool(disp.get("gps_ok", False)),
        # gps_comm = GPS hardware responding (NMEA flowing) even with no fix.
        # A shadowing screen needs this to tell "acquiring" (amber NO FIX) from
        # "dead" (red NO SIGNAL + airspeed/alt red-X): without it a peer with a
        # live GPS but no satellite lock (e.g. indoors) reads as total failure.
        "gps_comm":    bool(disp.get("gps_comm", False)),
        "fix":         int(disp.get("fix", 0)),
        "sats":        int(disp.get("sats", 0)),
        # Altitude / vertical
        "alt":         float(disp.get("alt", 0.0)),
        "vspeed":      float(disp.get("vspeed", 0.0)),
        "baro_src":    str(disp.get("baro_src", "gps")),
        "baro_ok":     bool(disp.get("baro_ok", False)),
        # Air data
        "ias_kt":      float(disp.get("ias_kt", 0.0)),
        "tas_kt":      float(disp.get("tas_kt", 0.0)),
        "airdata_ok":  bool(disp.get("airdata_ok", False)),
        # Wind solution (firmware/sim) so a shadow screen shows it on the strip.
        "wind_dir":    float(disp.get("wind_dir", 0.0)),
        "wind_kt":     float(disp.get("wind_kt", 0.0)),
    })


# ── Flight plan helpers ────────────────────────────────────────────────
# Pi 4 has the sim / GPS source and so is the side that "knows" when the
# aircraft passes a waypoint.  Without auto-advance here the sim's
# autopilot keeps chasing a waypoint that's now behind it — the
# bearing-to-waypoint flips 180° at overflight and the AP commands a
# tight orbit.  Same _FPL_ADVANCE_DIST_NM / _fpl_check_advance pattern
# as pi_zero so the two stay in lockstep.

_FPL_ADVANCE_DIST_NM = 0.5    # distance below which auto-sequence fires


def _fpl_is_active():
    fpl = disp.get("fpl", {})
    wps = fpl.get("waypoints", [])
    idx = fpl.get("active_idx", -1)
    return 0 <= idx < len(wps)


def _fpl_current():
    if not _fpl_is_active():
        return None
    return disp["fpl"]["waypoints"][disp["fpl"]["active_idx"]]


def _fpl_apply_active(reset_activation=False):
    """Mirror the active FPL waypoint into disp["nav"]."""
    wp = _fpl_current()
    if wp is None:
        return
    idx = disp["fpl"]["active_idx"]
    if idx > 0 and not reset_activation:
        prev = disp["fpl"]["waypoints"][idx - 1]
        act_lat = float(prev["lat"])
        act_lon = float(prev["lon"])
    else:
        act_lat = float(disp.get("lat", wp["lat"]))
        act_lon = float(disp.get("lon", wp["lon"]))
    disp["nav"]["ident"]   = str(wp["ident"])
    disp["nav"]["lat"]     = float(wp["lat"])
    disp["nav"]["lon"]     = float(wp["lon"])
    disp["nav"]["elev_ft"] = float(wp.get("elev_ft", 0.0))
    disp["nav"]["act_lat"] = act_lat
    disp["nav"]["act_lon"] = act_lon


def _fpl_activate(idx, reset_activation=False):
    """Set active_idx and mirror into disp["nav"].  Called only from
    the auto-advance path on this side (no editor here yet), so
    reset_activation defaults False — the leg origin is the previous
    waypoint, not the aircraft's current position."""
    wps = disp["fpl"]["waypoints"]
    if not (0 <= idx < len(wps)):
        return
    disp["fpl"]["active_idx"] = idx
    _fpl_apply_active(reset_activation=reset_activation)
    _ssync_publish_fpl()


def _fpl_deactivate():
    disp["fpl"]["active_idx"] = -1
    disp["nav"]["ident"]   = ""
    disp["nav"]["lat"]     = 0.0
    disp["nav"]["lon"]     = 0.0
    disp["nav"]["elev_ft"] = 0.0
    # Cancelling the plan drops the approach (and its fixes/HITS/sign-posts).
    disp["approach"] = {"loaded": False}
    _ssync_publish_fpl()


def _fpl_check_advance(lat, lon):
    """Called every frame.  When the aircraft is within
    _FPL_ADVANCE_DIST_NM of the active waypoint, sequence to the next
    leg.  Deactivates on reaching the final waypoint."""
    wp = _fpl_current()
    if wp is None:
        return
    dist_nm, _ = _nav_geo_dist_brg(lat, lon, wp["lat"], wp["lon"])
    if dist_nm >= _FPL_ADVANCE_DIST_NM:
        return
    fpl = disp["fpl"]
    if fpl["active_idx"] < len(fpl["waypoints"]) - 1:
        _fpl_activate(fpl["active_idx"] + 1, reset_activation=False)
    else:
        # Reached the destination.  A loaded approach is NOT auto-engaged —
        # the pilot activates it deliberately (FPL screen / CDI).  Just end
        # plan sequencing.
        _fpl_deactivate()


def _ssync_publish_fpl():
    """Broadcast the full plan to the peer.  Gated by the standard
    publish_kinds check (so Pi 4 only TXes when the pilot has FPL
    set to TX on this side).  Called from _fpl_activate /
    _fpl_deactivate so auto-advance changes propagate to the MFD
    when sync is configured that direction."""
    if _screen_sync is None or _ssync_suppress_publish:
        return
    fpl = disp.get("fpl", {})
    _screen_sync.publish(_ssync_mod.KIND_FPL, {
        "waypoints":  list(fpl.get("waypoints", [])),
        "active_idx": int(fpl.get("active_idx", -1)),
    })


def _fpl_render_remaining():
    """Return [(lat, lon, ident), ...] for the polyline on the inset
    map starting at the active waypoint, or None when there's nothing
    forward of the active leg.  Mirrors pi_zero's same-named helper."""
    fpl = disp.get("fpl", {})
    wps = fpl.get("waypoints", [])
    idx = int(fpl.get("active_idx", -1))
    if not (0 <= idx < len(wps) - 1):
        return None
    return [(float(w["lat"]), float(w["lon"]),
             str(w.get("ident", "")))
            for w in wps[idx:]]


def _approach_render_path():
    """[(lat, lon, ident), ...] of a loaded published approach's legs for the
    moving-map inset, or None (nothing loaded, or a synthetic single-point
    approach with no leg list)."""
    ap = disp.get("approach") or {}
    if not ap.get("loaded"):
        return None
    legs = ap.get("legs") or []
    if len(legs) < 2:
        return None
    return [(la, lo, ident) for (la, lo, ident, _lt, _alt, _at) in legs]


def _approach_raw_proc():
    """The raw procedure dict ({type,transitions,final,missed}) for the loaded
    approach, or None — used to read per-leg leg_type/course/turn the rendered
    6-tuples don't carry."""
    ap = disp.get("approach") or {}
    if _navdata is None or not ap.get("published"):
        return None
    return _navdata.procedure(ap.get("airport", ""), ap.get("procedure", ""))


def _approach_render_missed():
    """[(lat, lon, ident), ...] for the missed approach.  Per the plate it does
    NOT touch the runway: it begins at the climb-ahead point off the MAP (final
    course) and continues to the missed fixes.  None when nothing to draw."""
    ap = disp.get("approach") or {}
    if not ap.get("loaded"):
        return None
    missed = ap.get("missed_legs") or []
    if not missed or ap.get("thresh_lat") is None:
        return None
    thr_la, thr_lo = float(ap["thresh_lat"]), float(ap["thresh_lon"])
    crs = float(ap.get("course_deg", 0.0))
    # Climb-straight-ahead point off the MAP — so the dashed line starts ahead
    # of the runway (a gap), pointing up the final course like the plate.
    cl_la, cl_lo = _appr_project(thr_la, thr_lo, crs, 2.0)
    pts = [(cl_la, cl_lo, "")]
    pts += [(la, lo, ident) for (la, lo, ident, _lt, _alt, _at) in missed]
    return pts if len(pts) >= 2 else None


_HOLD_LEG_TYPES = ("HM", "HF", "HA")


def _approach_render_holds():
    """All holding patterns in the loaded approach → [(la, lo, course, turn,
    leg_nm), …].  Reads the raw legs (transition + final + missed) so the hold
    inbound course/turn come from the actual HM/HF/HA leg (correct racetrack
    orientation) — e.g. the HILPT at the IF and the missed hold.  Falls back to
    a published hold entry, then the arrival bearing."""
    ap = disp.get("approach") or {}
    if not ap.get("loaded"):
        return []
    # A peer sent its pre-computed holds (key present even if empty) — use them
    # verbatim so a screen without nav-data still draws the racetracks.
    if "synced_holds" in ap:
        return [tuple(h) for h in (ap.get("synced_holds") or [])]
    p = _approach_raw_proc()
    raw = []
    if p:
        # Scan EVERY transition (not just the selected one): a hold-in-lieu-of-PT
        # is often coded only on the transition that needs the course reversal
        # (e.g. KFLG R03's SEZCY HILPT lives in the FLG transition), but the
        # plate charts it regardless of how you join — so always show it.
        for tlegs in (p.get("transitions") or {}).values():
            raw += tlegs
        raw += p.get("final") or []
        raw += p.get("missed") or []
    out, seen, prev = [], set(), None
    for lg in raw:
        la, lo, ident = lg.get("lat"), lg.get("lon"), (lg.get("fix") or "")
        if la is None or lo is None or not ident:
            continue
        lt = (lg.get("leg_type") or "").upper()
        hd = _navdata.hold(ident) if _navdata is not None else None
        if (lt in _HOLD_LEG_TYPES or hd) and ident not in seen:
            crs = lg.get("course")
            turn = (lg.get("turn") or "").upper()        # from the hold leg
            leg_nm = 4.0
            if hd:
                if crs is None:
                    crs = hd.get("course")
                if turn not in ("L", "R"):
                    turn = (hd.get("turn") or "").upper()
                leg_nm = hd.get("leg_nm") or 4.0
            if turn not in ("L", "R"):
                turn = "R"
            if crs is None and prev is not None:        # last resort: arrival brg
                _d, crs = _nav_geo_dist_brg(prev[0], prev[1], la, lo)
            if crs is not None:
                out.append((float(la), float(lo), float(crs), turn, float(leg_nm)))
                seen.add(ident)
        prev = (la, lo)
    return out


def _approach_hits_final_nm():
    """Length (nm) of the synthetic-glideslope HITS corridor (fallback only,
    when there are no published leg altitudes).  Spans threshold → first fix."""
    ap = disp.get("approach") or {}
    legs = ap.get("legs") or []
    if ap.get("thresh_lat") is None or not legs:
        return _hits_mod.DEFAULT_FINAL_NM
    d, _ = _nav_geo_dist_brg(float(ap["thresh_lat"]), float(ap["thresh_lon"]),
                             float(legs[0][0]), float(legs[0][1]))
    return max(_hits_mod.DEFAULT_FINAL_NM, min(d, 20.0))


def _approach_hits_path():
    """[(lat, lon, alt_ft), …] from the first fix (IAF) to the runway threshold
    for the HITS boxes, using the PUBLISHED crossing altitude at each fix so the
    boxes follow the real vertical profile (step-downs), not a constant 3°.
    Fixes without a published altitude are linearly interpolated from their
    neighbours; the last point is pinned to the runway threshold elevation.
    None for a synthetic approach (no legs) → caller uses the 3° fallback."""
    ap = disp.get("approach") or {}
    legs = ap.get("legs") or []
    if len(legs) < 2 or ap.get("thresh_lat") is None:
        return None
    pts = [[float(la), float(lo),
            (float(alt) if alt is not None else None)]
           for (la, lo, _ident, _lt, alt, _at) in legs]
    # The last leg is the MAP/runway — pin it to the real threshold elevation.
    pts[-1][2] = float(ap.get("thresh_elev_ft") or (pts[-1][2] or 0.0))
    known = [i for i, p in enumerate(pts) if p[2] is not None]
    if not known:
        return None
    for i, p in enumerate(pts):
        if p[2] is not None:
            continue
        prev = max((k for k in known if k < i), default=None)
        nxt = min((k for k in known if k > i), default=None)
        if prev is not None and nxt is not None:
            f = (i - prev) / float(nxt - prev)
            p[2] = pts[prev][2] + f * (pts[nxt][2] - pts[prev][2])
        else:
            p[2] = pts[prev][2] if prev is not None else pts[nxt][2]
    return [(p[0], p[1], p[2]) for p in pts]


def _approach_hits_polylines():
    """HITS box polylines for the active approach (cached)."""
    _approach_hits_refresh()
    return _appr_hits_cache["polylines"]


# Cache the HITS boxes + vertical profile so they're rebuilt only when the
# loaded approach changes — NOT every frame.  Building the boxes (and
# re-deriving the path for the AP + VDI) every frame slowed the whole sim.
_appr_hits_cache = {"key": None, "polylines": [], "profile": None}
# Boxes now batch into a SINGLE draw call (svt render_polylines_..._batched), so
# count is cheap — this is just a safety bound on pathologically long corridors.
# Normal approaches stay at the dense default spacing (~1000 ft → a continuous
# tunnel from the IAF to the runway).
_HITS_MAX_BOXES = 500


def _approach_hits_refresh():
    ap = disp.get("approach") or {}
    key = (ap.get("airport"), ap.get("procedure"), ap.get("transition"),
           bool(ap.get("loaded")), len(ap.get("legs") or []),
           ap.get("thresh_lat"), ap.get("thresh_lon"))
    if _appr_hits_cache["key"] == key:
        return
    _appr_hits_cache["key"] = key
    path = _approach_hits_path()
    if path:
        # Pick a spacing that keeps the box count bounded over the whole
        # IAF→runway corridor (denser on a short final, sparser on a long one).
        total_ft = 0.0
        for (la0, lo0, _a0), (la1, lo1, _a1) in zip(path[:-1], path[1:]):
            total_ft += _nav_geo_dist_brg(la0, lo0, la1, lo1)[0] * 6076.12
        spacing = max(_hits_mod.DEFAULT_SPACING_FT, total_ft / _HITS_MAX_BOXES)
        _appr_hits_cache["polylines"] = _hits_mod.build_box_polylines_path(
            path, spacing_ft=spacing)
        tla, tlo = float(ap["thresh_lat"]), float(ap["thresh_lon"])
        _appr_hits_cache["profile"] = sorted(
            (_nav_geo_dist_brg(pla, plo, tla, tlo)[0], pa)
            for pla, plo, pa in path)
    elif ap.get("loaded") and ap.get("thresh_lat") is not None:
        _appr_hits_cache["polylines"] = _hits_mod.build_box_polylines(
            ap["thresh_lat"], ap["thresh_lon"], ap["thresh_elev_ft"],
            ap["course_deg"], final_nm=_approach_hits_final_nm())
        _appr_hits_cache["profile"] = None
    else:
        _appr_hits_cache["polylines"] = []
        _appr_hits_cache["profile"] = None


# ── Approach-fix sign-posts ─────────────────────────────────────────────────
# A "sign post" at each approach waypoint: an amber box floating at the fix's
# PUBLISHED crossing altitude with a thin vertical post dropping to the terrain
# below, so the pilot can eyeball whether they're high/low on the profile.  The
# box + post are 3D depth-tested polylines (same pipeline as the HITS boxes);
# the ident + altitude text is a 2D overlay projected per frame.  Amber keeps it
# distinct from the cyan HITS corridor and the magenta course line.
_SIGNPOST_COLOR    = (1.0, 200 / 255.0, 0.0, 1.0)   # amber
_SIGNPOST_SIDE_FT  = 400.0
_appr_signpost_cache = {"key": None, "polylines": [], "labels": []}


def _approach_signpost_refresh():
    ap = disp.get("approach") or {}
    key = (ap.get("airport"), ap.get("procedure"), ap.get("transition"),
           bool(ap.get("loaded")), bool(ap.get("active")),
           len(ap.get("legs") or []),
           ap.get("thresh_lat"), ap.get("thresh_lon"))
    if _appr_signpost_cache["key"] == key:
        return
    _appr_signpost_cache["key"] = key
    polylines = []
    labels = []
    # Only while an approach is actually loaded/active — so a new D2 or a
    # cancelled plan (both drop these flags) clears the sign-posts even though
    # the leg list may not have been wiped yet.
    if ap.get("loaded") or ap.get("active"):
        path = _approach_hits_path()       # [(la, lo, alt)…] interpolated alts
        legs = ap.get("legs") or []
        if path and len(path) == len(legs):
            course = float(ap.get("course_deg", 0.0))
            perp_rad = math.radians((course + 90.0) % 360.0)
            sin_p, cos_p = math.sin(perp_rad), math.cos(perp_rad)
            half = _SIGNPOST_SIDE_FT / 2.0
            dpf_lat = 1.0 / (60.0 * 6076.12)   # degrees latitude per foot
            for (la, lo, alt), leg in zip(path, legs):
                ident = str(leg[2] or "").strip()
                if not ident:
                    continue
                cos_lat = max(1e-6, math.cos(math.radians(la)))
                dpf_lon = dpf_lat / cos_lat
                dlat = cos_p * half * dpf_lat
                dlon = sin_p * half * dpf_lon
                # Diamond in the vertical plane perpendicular to the final
                # course, centred on the crossing altitude (top→right→bottom→
                # left→top).
                top_pt = (la, lo, alt + half)
                rt_pt  = (la - dlat, lo - dlon, alt)
                bot_pt = (la, lo, alt - half)
                lf_pt  = (la + dlat, lo + dlon, alt)
                polylines.append(([top_pt, rt_pt, bot_pt, lf_pt, top_pt],
                                  _SIGNPOST_COLOR, 2.0))
                # Vertical post from the diamond's bottom point to the terrain.
                try:
                    gelev = get_elevation_ft(SRTM_DIR, la, lo)
                    if gelev is None or gelev < -100:
                        gelev = alt - half
                except Exception:
                    gelev = alt - half
                polylines.append(([(la, lo, alt - half), (la, lo, gelev)],
                                  _SIGNPOST_COLOR, 2.0))
                labels.append((la, lo, alt, ident, int(round(alt))))
    _appr_signpost_cache["polylines"] = polylines
    _appr_signpost_cache["labels"] = labels


def _approach_signpost_polylines():
    """3D box + post polylines for each approach fix (cached)."""
    _approach_signpost_refresh()
    return _appr_signpost_cache["polylines"]


def _draw_approach_signpost_labels(surf, ai_rect, lat, lon, alt_ft,
                                   hdg, pitch, roll):
    """2D ident + crossing-altitude text at each sign-post, projected to the
    fix's screen position (amber, matching the 3D box)."""
    _approach_signpost_refresh()
    data = _appr_signpost_cache["labels"]
    if not data:
        return
    # Label ONLY the next fix being flown to (the active approach leg) — a label
    # at every fix cluttered the midpoint.
    ap = disp.get("approach") or {}
    legs = ap.get("legs") or []
    li = int(ap.get("leg_idx", 0))
    next_ident = (str(legs[li][2] or "").strip().upper()
                  if 0 <= li < len(legs) else "")
    if not next_ident:
        return
    ax, ay_r, aw, ah = ai_rect
    cx_ai = ax + aw // 2
    cy_ai = ay_r + ah // 2
    px_per_deg = ah / 48.0
    old_clip = surf.get_clip()
    surf.set_clip(pygame.Rect(ax, ay_r, aw, ah))
    half = _SIGNPOST_SIDE_FT / 2.0
    for fla, flo, falt, ident, ialt in data:
        if str(ident).strip().upper() != next_ident:
            continue
        # Project the TOP of the diamond (falt + half), not its centre, and
        # stack the label just above it — so the text always sits OUTSIDE the
        # box and rises with it as the box grows on approach, instead of
        # cluttering the middle of an already-busy space.
        pt = _project_latlon(float(fla), float(flo), lat, lon, alt_ft,
                             float(falt) + half, hdg, pitch, roll, cx_ai, cy_ai,
                             px_per_deg, max_fov_deg=None, ground_only=False)
        if pt is not None:
            sx, sy = int(pt[0]), int(pt[1])
            _text(surf, ident, 13, (255, 200, 0), bold=True, x=sx + 6, y=sy - 30)
            _text(surf, f"{ialt:,}", 12, (255, 200, 0), x=sx + 6, y=sy - 16)
        break
    surf.set_clip(old_clip)


def _approach_target_alt(lat, lon):
    """Target altitude (ft) of the PUBLISHED vertical profile at the aircraft's
    current distance from the threshold — the altitude the boxes show, what the
    AP descends to and the VDI references.  Uses the cached profile (cheap
    interpolation); None when no published profile (synthetic approach) →
    callers fall back to the 3° glideslope."""
    _approach_hits_refresh()
    prof = _appr_hits_cache["profile"]
    ap = disp.get("approach") or {}
    if not prof or ap.get("thresh_lat") is None:
        return None
    d = _nav_geo_dist_brg(float(lat), float(lon),
                          float(ap["thresh_lat"]), float(ap["thresh_lon"]))[0]
    if d <= prof[0][0]:
        return prof[0][1]
    if d >= prof[-1][0]:
        return prof[-1][1]
    for (d0, a0), (d1, a1) in zip(prof[:-1], prof[1:]):
        if d0 <= d <= d1:
            f = (d - d0) / (d1 - d0) if d1 > d0 else 0.0
            return a0 + f * (a1 - a0)
    return prof[-1][1]


# ── FPL editing (MFD flight-plan editor) ──────────────────────────────────────
_FPL_MAX_WAYPOINTS = 20     # matches pi_zero
_FPL_SAVED_MAX     = 8
_USER_WPT_MAX      = 50


def _user_wpt_save(ident, lat, lon, elev_ft=0.0):
    ident = str(ident).upper().strip()
    if not ident:
        return False
    lib = disp["user_wpts"]["list"]
    for w in lib:
        if str(w.get("ident", "")).upper() == ident:
            w["lat"], w["lon"], w["elev_ft"] = float(lat), float(lon), float(elev_ft)
            _settings.mark_dirty()
            return True
    if len(lib) >= _USER_WPT_MAX:
        return False
    lib.append({"ident": ident, "lat": float(lat), "lon": float(lon),
                "elev_ft": float(elev_ft)})
    _settings.mark_dirty()
    return True


def _user_wpt_delete(ident):
    ident = str(ident).upper()
    disp["user_wpts"]["list"] = [
        w for w in disp["user_wpts"]["list"]
        if str(w.get("ident", "")).upper() != ident]
    _settings.mark_dirty()


def _fpl_open_save_keyboard():
    disp["kbd_target"] = "fpl_save_name"
    disp["kbd_prev"]   = "fpl"
    disp["kbd_buf"]    = ""
    disp["kbd_error"]  = ""
    disp["kbd_shift"]  = False
    disp["mode"]       = "keyboard"


def _fpl_add_waypoint(ident, lat, lon, elev_ft=0.0, user=False,
                      name="", region=""):
    wps = disp["fpl"]["waypoints"]
    if len(wps) >= _FPL_MAX_WAYPOINTS:
        return False
    wps.append({"ident": str(ident), "lat": float(lat), "lon": float(lon),
                "elev_ft": float(elev_ft), "user": bool(user),
                "name": str(name), "region": str(region)})
    _settings.mark_dirty()
    _ssync_publish_fpl()
    return True


def _fpl_remove(idx):
    wps = disp["fpl"]["waypoints"]
    if not (0 <= idx < len(wps)):
        return
    cur = disp["fpl"]["active_idx"]
    del wps[idx]
    if cur == idx:
        _fpl_deactivate()
    elif cur > idx:
        disp["fpl"]["active_idx"] = cur - 1
        _fpl_apply_active()
    _settings.mark_dirty()
    _ssync_publish_fpl()


def _fpl_swap(i, j):
    wps = disp["fpl"]["waypoints"]
    if not (0 <= i < len(wps) and 0 <= j < len(wps)):
        return
    wps[i], wps[j] = wps[j], wps[i]
    cur = disp["fpl"]["active_idx"]
    if cur == i:
        disp["fpl"]["active_idx"] = j
    elif cur == j:
        disp["fpl"]["active_idx"] = i
    if _fpl_is_active():
        _fpl_apply_active()
    _settings.mark_dirty()
    _ssync_publish_fpl()


def _fpl_plan_save(name):
    name = str(name).strip()
    if not name:
        return (False, "empty name")
    wps = disp.get("fpl", {}).get("waypoints", [])
    if not wps:
        return (False, "no waypoints to save")
    snapshot = [dict(w) for w in wps]
    plans = disp["fpl_saved"]["plans"]
    now = time.time()
    # Saving clears any tombstone for this name so a re-create wins back over
    # a prior delete (its ts is newer than the tombstone).
    disp["fpl_saved"].setdefault("deleted", {}).pop(name.upper(), None)
    for p in plans:
        if str(p.get("name", "")).upper() == name.upper():
            p["name"] = name
            p["waypoints"] = snapshot
            p["ts"] = now
            _settings.mark_dirty()
            return (True, "")
    if len(plans) >= _FPL_SAVED_MAX:
        return (False, f"saved-plan limit ({_FPL_SAVED_MAX})")
    plans.append({"name": name, "waypoints": snapshot, "ts": now})
    _settings.mark_dirty()
    return (True, "")


def _fpl_plan_delete(name):
    name = str(name).upper()
    disp["fpl_saved"]["plans"] = [
        p for p in disp["fpl_saved"]["plans"]
        if str(p.get("name", "")).upper() != name]
    # Write a tombstone so the delete sticks across the panel (a peer that
    # still holds the plan won't resurrect it; see _ssync_apply_fpl_lib).
    disp["fpl_saved"].setdefault("deleted", {})[name] = time.time()
    _settings.mark_dirty()
    _ssync_publish_fpl_lib(force=True)


def _fpl_plan_load(name):
    name = str(name).upper()
    for p in disp["fpl_saved"]["plans"]:
        if str(p.get("name", "")).upper() == name:
            disp["fpl"]["waypoints"] = [dict(w) for w in p.get("waypoints", [])]
            _fpl_deactivate()
            return True
    return False


def _ssync_apply_fpl(data):
    """Receive a flight plan + active leg index from the peer (piZ).
    Replaces local disp["fpl"] verbatim and mirrors the active leg
    into disp["nav"] so the CDI / D→ / inset's magenta course line
    track the active waypoint — without this, the polyline refreshed
    on leg changes but the D2 line stayed pointing at whatever the
    first sync delivered."""
    global _ssync_suppress_publish
    _ssync_suppress_publish += 1
    try:
        wps = data.get("waypoints", [])
        idx = int(data.get("active_idx", -1))
        clean = []
        for w in wps:
            if not isinstance(w, dict):
                continue
            try:
                clean.append({
                    "ident":   str(w.get("ident", "")),
                    "lat":     float(w.get("lat",  0.0)),
                    "lon":     float(w.get("lon",  0.0)),
                    "elev_ft": float(w.get("elev_ft", 0.0)),
                    "user":    bool(w.get("user")),
                    "name":    str(w.get("name", "")),
                    "region":  str(w.get("region", "")),
                })
            except (TypeError, ValueError):
                continue
        disp["fpl"]["waypoints"]  = clean
        active_idx = idx if 0 <= idx < len(clean) else -1
        disp["fpl"]["active_idx"] = active_idx

        # Mirror the active leg into disp["nav"].  Leg origin =
        # previous waypoint (or aircraft pos for the first leg) so
        # CDI XTE is referenced to the correct course line.
        if active_idx >= 0:
            wp = clean[active_idx]
            disp["nav"]["ident"]   = wp["ident"]
            disp["nav"]["lat"]     = wp["lat"]
            disp["nav"]["lon"]     = wp["lon"]
            disp["nav"]["elev_ft"] = wp["elev_ft"]
            if active_idx > 0:
                prev = clean[active_idx - 1]
                disp["nav"]["act_lat"] = prev["lat"]
                disp["nav"]["act_lon"] = prev["lon"]
            else:
                disp["nav"]["act_lat"] = float(disp.get("lat", wp["lat"]))
                disp["nav"]["act_lon"] = float(disp.get("lon", wp["lon"]))
        else:
            disp["nav"]["ident"]   = ""
            disp["nav"]["lat"]     = 0.0
            disp["nav"]["lon"]     = 0.0
            disp["nav"]["elev_ft"] = 0.0
    finally:
        _ssync_suppress_publish -= 1


def _ssync_publish_approach():
    """Broadcast the loaded/active published approach so peer screens draw the
    same approach (legs, missed, threshold, course, holds).  Holds are sent
    pre-computed (not re-derived on the peer) so a screen without its own
    nav-data — e.g. the Pi Zero — still draws them.  Gated by the FPL share
    toggle — an approach is part of the plan.  Called whenever the approach
    state changes."""
    if _screen_sync is None or _ssync_suppress_publish:
        return
    ap = disp.get("approach") or {}
    if not ap.get("loaded"):
        _screen_sync.publish(_ssync_mod.KIND_APPR, {"loaded": False})
        return
    nv = disp.get("nav") or {}
    _screen_sync.publish(_ssync_mod.KIND_APPR, {
        "loaded":         True,
        "active":         bool(ap.get("active")),
        "missed":         bool(ap.get("missed")),
        "published":      bool(ap.get("published")),
        "airport":        str(ap.get("airport", "")),
        "procedure":      str(ap.get("procedure", "")),
        "transition":     str(ap.get("transition", "")),
        "runway":         str(ap.get("runway", "")),
        "legs":           [list(l) for l in (ap.get("legs") or [])],
        "final_idx":      int(ap.get("final_idx", 0)),
        "missed_legs":    [list(l) for l in (ap.get("missed_legs") or [])],
        "thresh_lat":     ap.get("thresh_lat"),
        "thresh_lon":     ap.get("thresh_lon"),
        "thresh_elev_ft": ap.get("thresh_elev_ft"),
        "course_deg":     ap.get("course_deg"),
        "leg_idx":        int(ap.get("leg_idx", 0)),
        # Pre-computed holds [(la, lo, course, turn, leg_nm), …] so a peer
        # without nav-data (piZ) draws the racetracks too.
        "holds":          [list(h) for h in _approach_render_holds()],
        # The EXACT active nav (course origin included) so the peer draws the
        # identical magenta course / CDI — a D2 anchors at the aircraft, not the
        # previous leg, so the consumer must not re-derive the origin.
        "nav": ({
            "ident":   str(nv.get("ident", "")),
            "lat":     float(nv.get("lat", 0.0)),
            "lon":     float(nv.get("lon", 0.0)),
            "elev_ft": float(nv.get("elev_ft", 0.0)),
            "act_lat": float(nv.get("act_lat", 0.0)),
            "act_lon": float(nv.get("act_lon", 0.0)),
        } if ap.get("active") and nv.get("ident") else None),
    })


def _ssync_apply_approach(data):
    """Adopt a peer's loaded/active approach so this screen draws it (and, when
    active, mirrors the active leg into disp['nav'] so the CDI / magenta course
    track it — same pattern as _ssync_apply_fpl)."""
    global _ssync_suppress_publish
    _ssync_suppress_publish += 1
    try:
        if not data.get("loaded"):
            disp["approach"] = {"loaded": False}
            return

        def _legs(key):
            out = []
            for l in (data.get(key) or []):
                try:
                    out.append((
                        float(l[0]), float(l[1]), str(l[2]),
                        str(l[3]) if len(l) > 3 and l[3] is not None else "",
                        l[4] if len(l) > 4 else None,
                        l[5] if len(l) > 5 else None))
                except (TypeError, ValueError, IndexError):
                    continue
            return out

        disp["approach"] = {
            "loaded":         True,
            "active":         bool(data.get("active")),
            "missed":         bool(data.get("missed")),
            "published":      bool(data.get("published")),
            "airport":        str(data.get("airport", "")),
            "procedure":      str(data.get("procedure", "")),
            "transition":     str(data.get("transition", "")),
            "runway":         str(data.get("runway", "")),
            "legs":           _legs("legs"),
            "final_idx":      int(data.get("final_idx", 0)),
            "missed_legs":    _legs("missed_legs"),
            "thresh_lat":     data.get("thresh_lat"),
            "thresh_lon":     data.get("thresh_lon"),
            "thresh_elev_ft": data.get("thresh_elev_ft"),
            "course_deg":     data.get("course_deg"),
            "leg_idx":        int(data.get("leg_idx", 0)),
        }
        # Pre-computed holds from the publisher — only set the key when the peer
        # actually sent them (an older peer that didn't → we fall back to
        # re-deriving from local nav-data in _approach_render_holds).
        if "holds" in data:
            sh = []
            for h in (data.get("holds") or []):
                try:
                    sh.append((float(h[0]), float(h[1]), float(h[2]),
                               str(h[3]), float(h[4])))
                except (TypeError, ValueError, IndexError):
                    continue
            disp["approach"]["synced_holds"] = sh
        # Mirror the active leg into disp["nav"] so the CDI / magenta course line
        # track it (the peer screen has no sim engine; this is display only).
        # Prefer the EXACT nav the publisher sent (so a D2's course origin is
        # the aircraft, matching the originating screen's XTK/magenta); fall
        # back to re-deriving it from the active leg for older peers.
        ap = disp["approach"]
        nv = data.get("nav")
        if ap.get("active") and nv and nv.get("ident"):
            disp["nav"] = {
                "ident":   str(nv.get("ident", "")),
                "lat":     float(nv.get("lat", 0.0)),
                "lon":     float(nv.get("lon", 0.0)),
                "elev_ft": float(nv.get("elev_ft", 0.0)),
                "act_lat": float(nv.get("act_lat", 0.0)),
                "act_lon": float(nv.get("act_lon", 0.0)),
            }
        elif ap.get("active") and (ap.get("legs") or []):
            if ap.get("missed"):
                _approach_apply_missed_leg()
            else:
                _approach_apply_leg()
    finally:
        _ssync_suppress_publish -= 1


_ssync_fpllib_last = 0.0


def _ssync_publish_fpl_lib(force=False):
    """Broadcast this screen's saved-plan + user-waypoint LIBRARY so any peer
    can load a plan stored here.  Rate-limited."""
    global _ssync_fpllib_last
    if _screen_sync is None:
        return
    now = time.monotonic()
    if not force and now - _ssync_fpllib_last < 5.0:
        return
    _ssync_fpllib_last = now
    _screen_sync.publish(_ssync_mod.KIND_FPLLIB, {
        "plans":     list(disp.get("fpl_saved", {}).get("plans", [])),
        "deleted":   dict(disp.get("fpl_saved", {}).get("deleted", {})),
        "user_wpts": list(disp.get("user_wpts", {}).get("list", [])),
    })


def _ssync_apply_fpl_lib(data):
    """Merge a peer's library into ours so any plan / user waypoint stored on
    any screen is loadable everywhere.  Saved plans merge with **deletion
    tombstones** (shared/fpllib) so a deleted plan doesn't resurrect from a
    peer that still holds it; user waypoints stay a simple union for now."""
    changed = False
    fs = disp.setdefault("fpl_saved", {})
    plans = fs.setdefault("plans", [])
    deleted = fs.setdefault("deleted", {})
    before = ([(str(p.get("name", "")).upper(), p.get("ts")) for p in plans],
              dict(deleted))
    merged, new_deleted = _fpllib.merge_plan_lib(
        plans, deleted,
        [p for p in data.get("plans", []) if isinstance(p, dict)],
        data.get("deleted", {}) if isinstance(data.get("deleted"), dict) else {},
        now=time.time(), max_plans=_FPL_SAVED_MAX)
    fs["plans"] = merged
    fs["deleted"] = new_deleted
    if before != ([(str(p.get("name", "")).upper(), p.get("ts")) for p in merged],
                  new_deleted):
        changed = True
    lib = disp.setdefault("user_wpts", {}).setdefault("list", [])
    have_w = {str(w.get("ident", "")).upper() for w in lib}
    for w in data.get("user_wpts", []):
        if not isinstance(w, dict):
            continue
        ident = str(w.get("ident", "")).strip().upper()
        if ident and ident not in have_w and len(lib) < _USER_WPT_MAX:
            lib.append({"ident": ident, "lat": float(w.get("lat", 0.0)),
                        "lon": float(w.get("lon", 0.0)),
                        "elev_ft": float(w.get("elev_ft", 0.0))})
            have_w.add(ident)
            changed = True
    if changed:
        _settings.mark_dirty()


def _ssync_apply_gps(data):
    global _ssync_suppress_publish
    _ssync_suppress_publish += 1
    try:
        with _state_lock:
            for k in ("lat", "lon", "gps_alt", "speed", "track",
                      "alt", "vspeed", "ias_kt", "tas_kt"):
                if k in data:
                    state[k] = float(data[k])
            if "gps_ok" in data:
                state["gps_ok"] = bool(data["gps_ok"])
            if "gps_comm" in data:
                state["gps_comm"] = bool(data["gps_comm"])
            if "fix" in data:
                state["fix"] = int(data["fix"])
            if "sats" in data:
                state["sats"] = int(data["sats"])
            if "baro_src" in data:
                state["baro_src"] = str(data["baro_src"])
            if "baro_ok" in data:
                state["baro_ok"] = bool(data["baro_ok"])
            if "airdata_ok" in data:
                state["airdata_ok"] = bool(data["airdata_ok"])
            for k in ("wind_dir", "wind_kt"):
                if k in data:
                    state[k] = float(data[k])
    finally:
        _ssync_suppress_publish -= 1

# ── GPS-slaved heading complementary filter ───────────────────────────────────
# Propagate heading using the AHRS gyro yaw-rate (smooth, 30 Hz) and
# slowly slave the absolute reference toward the GPS ground track (1–5 Hz,
# noisy).  This mirrors how real GPS/IRS heading modes work.
_gps_hdg      = None   # current complementary-filter output (degrees, 0–360)
_prev_yaw_disp = None  # disp["yaw"] value from the previous frame


# ── GPS heading complementary filter ─────────────────────────────────────────

def _update_gps_heading(yaw_now: float, track: float, gps_ok: bool) -> float:
    """
    Complementary filter for GPS-slaved heading.

    High-frequency path: AHRS yaw rate (smooth, 30 Hz) propagates _gps_hdg.
    Low-frequency path:  GPS ground track slowly slaves the absolute value.

    Returns the filtered heading in degrees [0, 360).
    """
    global _gps_hdg, _prev_yaw_disp

    if _gps_hdg is None:
        # Initialise from GPS track if available, else fall back to yaw
        _gps_hdg       = track if gps_ok else yaw_now
        _prev_yaw_disp = yaw_now
        return _gps_hdg

    # ── Gyro propagation ───────────────────────────────────────────────────────
    # Use the frame-to-frame change in the AHRS yaw (already smoothed) as a
    # proxy for the gyro turn rate.  Normalise to (−180, +180] to handle the
    # 359° → 0° wrap correctly.
    delta = ((yaw_now - _prev_yaw_disp) + 180) % 360 - 180
    _gps_hdg = (_gps_hdg + delta) % 360
    _prev_yaw_disp = yaw_now

    # ── GPS slaving ────────────────────────────────────────────────────────────
    # Pull _gps_hdg toward the GPS track at rate GPS_HDG_SLAVE_K per frame.
    # Signed error handles the 359°/0° wrap.
    if gps_ok:
        err = ((track - _gps_hdg) + 180) % 360 - 180
        _gps_hdg = (_gps_hdg + err * GPS_HDG_SLAVE_K) % 360

    return _gps_hdg


# ── Connectivity helpers ──────────────────────────────────────────────────────

def _wifi_ssid_current():
    """Return currently-associated WiFi SSID, or '' if not connected / unsupported."""
    try:
        r = subprocess.run(["iwgetid", "-r"],
                           capture_output=True, text=True, timeout=2)
        return r.stdout.strip()
    except Exception:
        return ""


def _poll_wifi_status():
    """Background thread: update disp['cs']['wifi_ok'] and the actual
    connected SSID (wifi_actual) every 5 s."""
    while True:
        ssid = _wifi_ssid_current()
        disp["cs"]["wifi_actual"] = ssid
        disp["cs"]["wifi_ok"]     = bool(ssid)
        time.sleep(5)


def _SPD_DISP_FACTOR():
    """Current speed display conversion factor (kt → display unit)."""
    return {"kt": 1.0, "mph": 1.15078, "kph": 1.852}.get(
        disp["ds"].get("spd_unit", "kt"), 1.0)


def _ALT_DISP_FACTOR():
    """Current altitude display conversion factor (ft → display unit)."""
    return {"ft": 1.0, "m": 0.3048}.get(disp["ds"].get("alt_unit", "ft"), 1.0)


def _push_baro_to_pico(qnh_hpa: float):
    """Fire-and-forget HTTP GET to the Pico W's /baro?qnh=X endpoint.

    The firmware uses this to update its internal BME280 QNH so future
    altitude samples are computed against the pilot's altimeter setting.
    Without this call, the PFD would show the pilot's entered baro
    locally but the AHRS-derived altitude would still reflect the
    firmware's default QNH — a silent miscalibration.

    Runs in a background thread because the Pico's HTTP handler can
    take 100–300 ms to respond and must not block the PFD frame loop.
    Failures are logged but don't alter disp — the local baro value
    sticks regardless of whether the Pico is reachable.
    """
    base = disp.get("cs", {}).get("ahrs_url", "http://192.168.4.1").rstrip("/")
    url  = f"{base}/baro?qnh={qnh_hpa:.2f}"

    def _worker():
        try:
            import urllib.request
            with urllib.request.urlopen(url, timeout=3) as resp:
                resp.read()
        except Exception as e:
            print(f"[PFD] /baro push failed ({url}): {e}")

    threading.Thread(target=_worker, daemon=True, name="BaroPush").start()


def _push_magcal_to_pico(table):
    """Send a 36-point deviation table to the Pico.
    Tries USB serial ($MAGDEV, command) first — works even when the Pi4
    is not on the Pico's WiFi AP.  Falls back to HTTP GET as a bonus.
    Runs in a background thread."""
    t_str = ",".join(f"{v:.3f}" for v in table)

    def _worker():
        sent_ok = False
        # ── Try USB serial (primary) ──
        client = _sse_client
        if client is not None and hasattr(client, 'write'):
            try:
                cmd = f"$MAGDEV,{t_str}\n".encode()
                client.write(cmd)
                print(f"[PFD] magcal sent via USB serial ({len(table)} pts)")
                sent_ok = True
            except Exception as e:
                print(f"[PFD] magcal serial write failed: {e}")
        # ── Try HTTP (bonus, only if serial unavailable) ──
        if not sent_ok:
            base = disp.get("cs", {}).get("ahrs_url", "http://192.168.4.1").rstrip("/")
            url = f"{base}/magcal?action=set&t={t_str}"
            try:
                import urllib.request
                with urllib.request.urlopen(url, timeout=5) as resp:
                    resp.read()
                print(f"[PFD] magcal sent via HTTP ({len(table)} pts)")
                sent_ok = True
            except Exception as e:
                print(f"[PFD] magcal HTTP push failed: {e}")
        wiz = disp.get("mag_cal_wiz") or {}
        if sent_ok:
            wiz["msg"] = "Saved locally + sent to AHRS ✓"
        else:
            wiz["msg"] = "Saved locally only (AHRS unreachable)"

    threading.Thread(target=_worker, daemon=True, name="MagCalPush").start()


def _push_magoff_tumble(action):
    """Send $MAGOFF,START or $MAGOFF,FINISH to the Pico to bracket a tumble-
    cal session. Serial primary, HTTP fallback, background thread.
    action must be 'START' or 'FINISH'."""
    payload = f"$MAGOFF,{action}\n".encode()
    http_action = "tumble_start" if action == "START" else "tumble_finish"

    def _worker():
        client = _sse_client
        if client is not None and hasattr(client, 'write'):
            try:
                client.write(payload)
                print(f"[PFD] magoff {action} sent via USB serial")
                return
            except Exception as e:
                print(f"[PFD] magoff {action} serial failed: {e}")
        base = disp.get("cs", {}).get("ahrs_url", "http://192.168.4.1").rstrip("/")
        url = f"{base}/magoff?action={http_action}"
        try:
            import urllib.request
            with urllib.request.urlopen(url, timeout=5) as resp:
                resp.read()
            print(f"[PFD] magoff {action} sent via HTTP")
        except Exception as e:
            print(f"[PFD] magoff {action} HTTP failed: {e}")

    threading.Thread(target=_worker, daemon=True,
                     name=f"MagOff{action}").start()


def _push_magoff_to_pico(offset):
    """Send hard-iron offsets (mx_off, my_off, mz_off) to the Pico. Serial
    first ($MAGOFF,...), HTTP fallback (/magoff?action=set&v=...). Background."""
    v_str = f"{offset[0]:.2f},{offset[1]:.2f},{offset[2]:.2f}"

    def _worker():
        client = _sse_client
        if client is not None and hasattr(client, 'write'):
            try:
                client.write(f"$MAGOFF,{v_str}\n".encode())
                print(f"[PFD] magoff sent via USB serial ({v_str})")
                return
            except Exception as e:
                print(f"[PFD] magoff serial write failed: {e}")
        base = disp.get("cs", {}).get("ahrs_url", "http://192.168.4.1").rstrip("/")
        url = f"{base}/magoff?action=set&v={v_str}"
        try:
            import urllib.request
            with urllib.request.urlopen(url, timeout=5) as resp:
                resp.read()
            print(f"[PFD] magoff sent via HTTP ({v_str})")
        except Exception as e:
            print(f"[PFD] magoff HTTP push failed: {e}")

    threading.Thread(target=_worker, daemon=True, name="MagOffPush").start()


def _push_magoff_clear_to_pico():
    """Clear hard-iron offsets on the Pico."""
    def _worker():
        client = _sse_client
        if client is not None and hasattr(client, 'write'):
            try:
                client.write(b"$MAGOFF,CLEAR\n")
                print("[PFD] magoff cleared via USB serial")
                return
            except Exception as e:
                print(f"[PFD] magoff serial clear failed: {e}")
        base = disp.get("cs", {}).get("ahrs_url", "http://192.168.4.1").rstrip("/")
        url = f"{base}/magoff?action=clear"
        try:
            import urllib.request
            with urllib.request.urlopen(url, timeout=5) as resp:
                resp.read()
            print("[PFD] magoff cleared via HTTP")
        except Exception as e:
            print(f"[PFD] magoff HTTP clear failed: {e}")

    threading.Thread(target=_worker, daemon=True, name="MagOffClear").start()


def _push_magcal_clear_to_pico():
    """Clear the Pico's deviation table via serial then HTTP.  Background thread."""
    def _worker():
        client = _sse_client
        if client is not None and hasattr(client, 'write'):
            try:
                client.write(b"$MAGDEV,CLEAR\n")
                print("[PFD] magcal cleared via USB serial")
                return
            except Exception as e:
                print(f"[PFD] magcal serial clear failed: {e}")
        base = disp.get("cs", {}).get("ahrs_url", "http://192.168.4.1").rstrip("/")
        url = f"{base}/magcal?action=clear"
        try:
            import urllib.request
            with urllib.request.urlopen(url, timeout=5) as resp:
                resp.read()
            print("[PFD] magcal cleared via HTTP")
        except Exception as e:
            print(f"[PFD] magcal HTTP clear failed: {e}")

    threading.Thread(target=_worker, daemon=True, name="MagCalClear").start()


def _push_align_to_pico(pitch_align, roll_align):
    """Send axis-alignment values to the Pico via $ALIGN.  Same retry-
    until-broadcast-echo pattern as _push_orient_to_pico — confirms
    receipt by watching state["pitch_align"]/state["roll_align"] for a
    matching update from the next $AHRS frame."""
    import time as _time

    def _worker():
        for attempt in range(6):
            client = _sse_client
            if client is None or not hasattr(client, 'write'):
                print("[PFD] align push: no serial client available")
                return
            try:
                cmd = f"$ALIGN,{pitch_align:.2f},{roll_align:.2f}\n".encode()
                client.write(cmd)
                print(f"[PFD] align sent (attempt {attempt + 1}) "
                      f"({pitch_align:+.2f},{roll_align:+.2f})")
            except Exception as e:
                print(f"[PFD] align serial write failed: {e}")
                return
            for _ in range(20):
                _time.sleep(0.1)
                with _state_lock:
                    pa = state.get("pitch_align")
                    ra = state.get("roll_align")
                if (pa is not None and ra is not None
                        and abs(pa - pitch_align) < 0.05
                        and abs(ra - roll_align)  < 0.05):
                    print(f"[PFD] align confirmed by Pico "
                          f"({pitch_align:+.2f},{roll_align:+.2f})")
                    return
        print(f"[PFD] align push gave up after 6 attempts "
              f"({pitch_align:+.2f},{roll_align:+.2f})")
    threading.Thread(target=_worker, daemon=True, name="AlignPush").start()


def _push_orient_to_pico(connector, mounting):
    """Send orientation + mounting to the Pico via USB serial ($ORIENT, command).
    Retries every 2 s (up to 6 attempts) until the Pico echoes back the new
    orientation in its $AHRS broadcast, confirming receipt."""
    import time as _time
    def _worker():
        for attempt in range(6):
            client = _sse_client
            if client is None or not hasattr(client, 'write'):
                print("[PFD] orient push: no serial client available")
                return
            try:
                cmd = f"$ORIENT,{connector},{mounting}\n".encode()
                client.write(cmd)
                print(f"[PFD] orient sent (attempt {attempt + 1}) ({connector},{mounting})")
            except Exception as e:
                print(f"[PFD] orient serial write failed: {e}")
                return
            # Poll up to 2 s for the Pico to echo back via the $AHRS broadcast.
            for _ in range(20):
                _time.sleep(0.1)
                with _state_lock:
                    pico_ori = state.get("orientation")
                    pico_mnt = state.get("mounting")
                if pico_ori == connector and pico_mnt == mounting:
                    print(f"[PFD] orient confirmed by Pico ({connector},{mounting})")
                    return
        print(f"[PFD] orient push gave up after 6 attempts ({connector},{mounting})")
    threading.Thread(target=_worker, daemon=True, name="OrientPush").start()


def _apply_local_magdev(yaw, table):
    """Apply a 36-slot deviation table — mirrors Pico's apply_magdev exactly."""
    if len(table) != 36:
        return yaw
    idx = (yaw % 360) / 10.0
    i0 = int(idx) % 36
    i1 = (i0 + 1) % 36
    frac = idx - int(idx)
    c0, c1 = table[i0], table[i1]
    dc = c1 - c0
    if dc > 180:  dc -= 360
    elif dc < -180: dc += 360
    return (yaw + c0 + frac * dc) % 360


def _build_magdev_table(samples):
    """Build a 36-point (10°/slot) deviation table from (expected, raw) pairs.
    Matches the circular interpolation used by the iPhone calibration UI."""
    pts = sorted(
        [{"a": r % 360.0, "c": ((e - r + 180 + 360) % 360) - 180}
         for e, r in samples],
        key=lambda p: p["a"],
    )
    return [_magdev_interp(i * 10.0, pts) for i in range(36)]


def _magdev_interp(target, pts):
    if not pts:
        return 0.0
    if len(pts) == 1:
        return pts[0]["c"]
    lo, hi = pts[-1], pts[0]
    for p in pts:
        if p["a"] <= target:
            lo = p
        else:
            hi = p
            break
    hi_a = hi["a"] + (360.0 if hi["a"] <= lo["a"] else 0.0)
    span = hi_a - lo["a"] or 360.0
    tgt  = target + 360.0 if target < lo["a"] else target
    dc   = hi["c"] - lo["c"]
    if dc > 180:   dc -= 360
    elif dc < -180: dc += 360
    return lo["c"] + (tgt - lo["a"]) / span * dc


def _poll_ahrs_diag():
    """Background thread: mirror the AHRS transport client's diagnostic
    counters (rx_count, err_count, last_err) into disp['cs'] so the
    Connectivity screen can render them."""
    while True:
        c = _sse_client
        if c is not None:
            disp["cs"]["ahrs_rx"]       = getattr(c, "rx_count",  0)
            disp["cs"]["ahrs_err"]      = getattr(c, "err_count", 0)
            disp["cs"]["ahrs_last_err"] = getattr(c, "last_err", "")
        time.sleep(1)


def _apply_wifi(ssid, password):
    """Connect wlan0 to ssid using nmcli (NetworkManager) or wpa_supplicant,
    whichever is managing the interface.  Returns (success: bool, message: str).
    For nmcli a sudoers entry for nmcli is required (see /etc/sudoers.d/pfd-nmcli).
    For wpa_supplicant the process must be root (or have a sudoers entry).
    """
    if not ssid:
        return False, "SSID required"

    # Mirror wifi_switch.sh: prefer nmcli when NetworkManager is running.
    try:
        nm = subprocess.run(["systemctl", "is-active", "--quiet", "NetworkManager"],
                            capture_output=True, timeout=5)
        use_nm = nm.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        use_nm = False

    try:
        if use_nm:
            CON = "pfd-wifi"
            subprocess.run(["sudo", "nmcli", "con", "delete", CON],
                           capture_output=True, timeout=5)
            cmd = ["sudo", "nmcli", "dev", "wifi", "connect", ssid]
            if password:
                cmd += ["password", password]
            cmd += ["name", CON]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                err = (r.stderr or r.stdout).strip()
                return False, err[:80] if err else "nmcli connect failed"
            return True, "WiFi connected"
        else:
            net_block = (
                f'network={{\n'
                f'    ssid="{ssid}"\n'
                + (f'    psk="{password}"\n    key_mgmt=WPA-PSK\n' if password
                   else '    key_mgmt=NONE\n')
                + '}\n'
            )
            conf = (
                "ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev\n"
                "update_config=1\ncountry=US\n\n"
                + net_block
            )
            with open("/etc/wpa_supplicant/wpa_supplicant.conf", "w") as f:
                f.write(conf)
            r = subprocess.run(["wpa_cli", "-i", "wlan0", "reconfigure"],
                               capture_output=True, text=True, timeout=5)
            if r.returncode != 0:
                return False, "wpa_cli failed"
            return True, "WiFi config applied — connecting…"
    except PermissionError:
        return False, "Permission denied — run with sudo"
    except FileNotFoundError as e:
        return False, f"Not found: {e.filename}"
    except subprocess.TimeoutExpired:
        return False, "Timed out connecting"
    except Exception as e:
        return False, str(e)[:60]


def _restart_sse(url):
    """Stop the current SSE client and start a new one pointing at url."""
    global _sse_client
    if _sse_client:
        _sse_client.stop()
    sse_url = url.rstrip("/") + "/events"
    _sse_client = SSEClient(sse_url, state, _state_lock)
    _sse_client.start()
    # Keep the AHRS LINK row's transport label honest — TEST AHRS / a
    # WiFi-side restart needs to overwrite whatever USB labels were set
    # at boot, otherwise the screen lies about which transport is live.
    disp["cs"]["ahrs_transport"] = "wifi"
    disp["cs"]["ahrs_port"]      = sse_url
    print(f"[PFD] SSE → {sse_url}")


def _test_ahrs_connection(url):
    """TCP connect test to the AHRS host. Returns (ok: bool, msg: str)."""
    try:
        stripped = url.replace("http://", "").replace("https://", "")
        host_port, *_ = stripped.split("/")
        host, *port_part = host_port.split(":")
        port = int(port_part[0]) if port_part else 80
        s = socket.socket()
        s.settimeout(3)
        s.connect((host, port))
        s.close()
        return True, f"Reached {host}:{port} \u2713"
    except Exception as e:
        return False, str(e)[:50]


# ── Backlight control ─────────────────────────────────────────────────────────

_BACKLIGHT_PATHS = [
    "/sys/class/backlight/rpi_backlight/brightness",
    "/sys/class/backlight/10-0045/brightness",
]
_backlight_path     = None
_backlight_max_path = None   # max_brightness sysfs node
_backlight_ddc_bus  = None   # /dev/i2c-N when DDC/CI brightness works
_backlight_lock     = threading.Lock()   # serialise ddcutil writes


def _detect_ddc_bus():
    """Return the I²C bus number of a DDC/CI display whose VCP 10
    (brightness) is gettable, or None. ddcutil's getvcp is the cheap
    "is this connected and does brightness work?" probe — much faster
    than a full `capabilities` parse."""
    try:
        r = subprocess.run(["ddcutil", "detect"],
                           capture_output=True, text=True, timeout=8)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    # First "I2C bus: /dev/i2c-N" line in the detect output is the
    # primary panel — that's the one we want for brightness control.
    bus = None
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith("I2C bus:") and "/dev/i2c-" in line:
            try:
                bus = int(line.split("/dev/i2c-")[1].strip())
                break
            except (IndexError, ValueError):
                continue
    if bus is None:
        return None
    try:
        r = subprocess.run(
            ["ddcutil", "--bus", str(bus), "getvcp", "10", "--terse"],
            capture_output=True, text=True, timeout=4,
        )
    except subprocess.TimeoutExpired:
        return None
    if r.returncode == 0 and "VCP" in r.stdout:
        return bus
    return None


def _init_backlight():
    """Find the active brightness sink — sysfs first (RPi 7" touch),
    then DDC/CI (HDMI panels that speak VESA MCCS, e.g. ROADOM Z3)."""
    global _backlight_path, _backlight_max_path, _backlight_ddc_bus
    for p in _BACKLIGHT_PATHS:
        if os.path.exists(p):
            _backlight_path     = p
            _backlight_max_path = os.path.join(os.path.dirname(p), "max_brightness")
            print(f"[BL] Using backlight: {p}")
            return
    bus = _detect_ddc_bus()
    if bus is not None:
        _backlight_ddc_bus = bus
        print(f"[BL] Using DDC/CI brightness on /dev/i2c-{bus}")
        return
    print("[BL] No backlight control available (no sysfs node, no DDC/CI)")


def _ddc_set_brightness(value_0_100: int):
    """Background-thread DDC/CI brightness write. ddcutil over I²C
    takes 200–400 ms; we don't want that on the render thread.
    Serialised with a lock so the user spamming the brightness slider
    doesn't queue a dozen concurrent ddcutil processes."""
    def _worker():
        with _backlight_lock:
            try:
                subprocess.run(
                    ["ddcutil", "--bus", str(_backlight_ddc_bus),
                     "setvcp", "10", str(value_0_100)],
                    capture_output=True, timeout=5,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
    threading.Thread(target=_worker, daemon=True, name="DDCBacklight").start()


def _set_backlight(level: int):
    """Set brightness 1–10 → sysfs / DDC. No-op if neither is wired."""
    if _backlight_path is not None:
        try:
            max_b = 255
            if _backlight_max_path and os.path.exists(_backlight_max_path):
                with open(_backlight_max_path) as f:
                    max_b = int(f.read().strip())
            raw = max(0, min(max_b, int((level - 1) / 9.0 * max_b)))
            with open(_backlight_path, "w") as f:
                f.write(str(raw))
        except OSError:
            pass
    elif _backlight_ddc_bus is not None:
        # DDC/CI VCP 10 is 0..100; map our 1-10 setting linearly.
        pct = max(0, min(100, int((level - 1) / 9.0 * 100)))
        _ddc_set_brightness(pct)


def smooth_state():
    """Copy live state → display values with IIR smoothing for analogue fields."""
    with _state_lock:
        snap = dict(state)
    for k in ("roll", "pitch", "ay", "speed", "alt", "vspeed",
              "ias_kt", "tas_kt"):
        if k in snap:
            disp[k] = disp.get(k, 0.0) * (1 - SMOOTH_K) + snap[k] * SMOOTH_K
    # Heading: handle 0/360 wraparound
    dh = ((snap["yaw"] - disp["yaw"] + 180) % 360) - 180
    disp["yaw"] = (disp["yaw"] + dh * SMOOTH_K) % 360
    # Boolean / discrete fields: copy directly.
    # NOTE: baro_hpa is NOT copied here — it's a user-set Kollsman-window value
    # owned by disp[] and pushed outward to the Pico W via _push_baro_to_pico().
    # Copying it from state every frame would clobber numpad/± button entries
    # whenever the SSE stream carried a stale QNH echo back from the firmware.
    for k in ("lat", "lon", "track", "fix", "sats",
              "gps_alt", "baro_src",
              "ahrs_ok", "gps_ok", "gps_comm", "baro_ok", "airdata_ok",
              "wind_dir", "wind_kt",
              "ahrs_aligning",
              "pitch_trim", "roll_trim", "yaw_trim",
              "orientation", "mounting", "pitch_align", "roll_align",
              "yaw_raw", "yaw_wt901",
              "mx", "my", "mz", "fw_ver"):
        if k in snap:
            disp[k] = snap[k]


# ── ADS-B traffic refresh ──────────────────────────────────────────────────
# Per-target previous range/time + smoothed closure, keyed by ICAO, for the
# tau-based RA.  Pruned of stale targets each frame inside _update_traffic.
_traffic_range_hist: dict = {}


def _update_traffic(demo_mode):
    """Refresh disp["traffic"] once per frame from the live GDL90 listener
    (or the synthetic generator in demo mode).  Each target is relativised
    against the current ownship fix and classified by threat level, then
    sorted nearest-first for the renderer."""
    own_lat = float(disp.get("lat", DEMO_LAT))
    own_lon = float(disp.get("lon", DEMO_LON))
    own_alt = float(disp.get("alt", 0.0))

    # Built-in internet feed runs unless source is "radio" (external only),
    # we're in demo, or there's no GPS fix yet.
    if _traffic_feed is not None:
        src = disp["cs"].get("traffic_source", "auto")
        _traffic_feed.paused = (src == "radio" or demo_mode
                                or not disp.get("gps_ok", False))

    # RADIO and INTERNET are kept on SEPARATE paths so they can never be
    # confused: the radio/SDR bridge arrives on GDL90/UDP via _adsb_client,
    # the internet feed has its own in-process snapshot.  Each target is
    # tagged with its source; on a duplicate ICAO the radio (real local
    # sensor) wins.  This lets the status line show split R/I counts so the
    # pilot can verify the radio is actually producing targets even in AUTO.
    online = False
    radio = []
    if _adsb_client is not None:
        radio = _adsb_client.snapshot()
        for t in radio:
            t["src"] = "radio"
        online = _adsb_client.connected
        cs = disp["cs"]
        cs["adsb_online"]   = online
        cs["adsb_rx"]       = _adsb_client.rx_count
        cs["adsb_err"]      = _adsb_client.err_count
        cs["adsb_last_err"] = _adsb_client.last_err
        cs["adsb_uplink"]   = _adsb_client.uplink_count
    inet = []
    if _traffic_feed is not None and not _traffic_feed.paused:
        inet = _traffic_feed.snapshot()      # already tagged src="internet"

    if radio or inet:
        by_icao = {}
        for t in inet:
            by_icao[t.get("icao")] = t
        for t in radio:                       # radio overrides on dup ICAO
            by_icao[t.get("icao")] = t
        raw = list(by_icao.values())
        online = online or bool(inet)
    elif demo_mode:
        raw = _adsb.demo_targets(own_lat, own_lon, own_alt, time.monotonic())
        for t in raw:
            t["src"] = "demo"
        online = True
    else:
        raw = []

    rel = []
    any_alert = False
    _now_mono = time.monotonic()
    _ra_cfg = dict(tau_s=ADSB_TAU_S, floor_nm=ADSB_ALERT_FLOOR_NM,
                   floor_ft=ADSB_ALERT_FLOOR_FT, alert_ft=ADSB_ALERT_FT,
                   proximate_nm=ADSB_PROX_NM, proximate_ft=ADSB_PROX_FT,
                   arm_s=ADSB_RA_ARM_S, hold_s=ADSB_RA_HOLD_S)
    for t in raw:
        r = _adsb.relative(t, own_lat, own_lon, own_alt)
        # Stable closure estimate (over real ADS-B update intervals) + arming/
        # latch hysteresis, so the RA doesn't chatter or re-fire the callout on
        # borderline traffic.  Per-ICAO state persists in _traffic_range_hist.
        tid = t.get("icao")
        if tid is not None:
            h = _traffic_range_hist.setdefault(tid, {})
            thr = _adsb.track_threat(h, r, _now_mono, **_ra_cfg)
        else:
            thr = _adsb.threat_level(r, proximate_nm=ADSB_PROX_NM,
                                     proximate_ft=ADSB_PROX_FT,
                                     alert_ft=ADSB_ALERT_FT, tau_s=ADSB_TAU_S,
                                     floor_nm=ADSB_ALERT_FLOOR_NM,
                                     floor_ft=ADSB_ALERT_FLOOR_FT)
        r["threat"] = thr
        if thr == "alert":
            any_alert = True
        rel.append(r)
    # Prune state for targets not seen for >12 s so the dict stays small.
    for _tid in [k for k, v in _traffic_range_hist.items()
                 if _now_mono - v.get("seen", 0.0) > 12.0]:
        _traffic_range_hist.pop(_tid, None)
    rel.sort(key=lambda d: (d.get("range_nm") is None,
                            d.get("range_nm") or 1e9))

    # Declutter filters (alert-class threats always survive).  alt_band /
    # range of 0 mean "show all".
    shown = _adsb.filter_targets(
        rel,
        alt_band_ft=int(disp["ds"].get("traffic_alt_band", 0)),
        range_nm=float(disp["ds"].get("traffic_range_nm", 0)))

    tr = disp["traffic"]
    tr["targets"] = shown
    tr["online"]  = online
    tr["n"]       = len(shown)
    tr["n_total"] = len(rel)
    tr["n_radio"] = sum(1 for t in rel if t.get("src") == "radio")
    tr["n_inet"]  = sum(1 for t in rel if t.get("src") == "internet")
    tr["alert"]   = any_alert

    # Edge-triggered traffic advisory: a "Traffic, Traffic" callout + the banner
    # fire when a NEW target enters alert state — not re-fired every frame for a
    # threat already called.  Nearest alert target drives the banner.
    alerts = sorted((t for t in rel if t.get("threat") == "alert"),
                    key=lambda d: (d.get("range_nm") is None,
                                   d.get("range_nm") or 1e9))
    tr["alert_target"] = alerts[0] if alerts else None
    global _prev_alert_ids
    cur_ids = {t.get("icao") for t in alerts}
    if cur_ids - _prev_alert_ids:           # a threat we hadn't called yet
        audio_alerts.play("traffic")
    _prev_alert_ids = cur_ids


def _traffic_to_draw():
    """Traffic list for the map.  On the dedicated TFC overlay the full
    (declutter-filtered) list is shown; on every other page traffic is clamped
    to nearby 'safety' targets — within a few miles and a few thousand feet —
    so distant aircraft don't clutter a weather / airspace picture.  Alert-
    class threats are never clamped out."""
    shown = disp.get("traffic", {}).get("targets") or []
    if _map_overlay_state(disp["ds"]) == "tfc":
        return shown
    return _adsb.filter_targets(shown, alt_band_ft=TRAFFIC_SAFETY_FT,
                                range_nm=TRAFFIC_SAFETY_NM, keep_alert=True)


def _wx_view():
    """(center_lat, center_lon, radius_nm) for the weather query.  Follows
    the full-screen MFD's pan + (resolved) zoom so METAR/NEXRAD load for
    wherever you're looking over CONUS — not just a fixed ring at the
    aircraft.  Falls back to the inset's aircraft-centred view otherwise."""
    if disp.get("display_mode") == "mfd":
        clat, clon = _mfd_effective_center()          # pan-aware
        rng = _mfd_last_range or 10                    # AUTO-resolved range
    else:
        clat = float(disp.get("lat", DEMO_LAT))
        clon = float(disp.get("lon", DEMO_LON))
        rng = int(disp["ds"].get("map_zoom_nm", 5)) or 5
    radius = max(WX_MIN_RADIUS_NM, min(WX_MAX_RADIUS_NM, rng * WX_RADIUS_ZOOM_K))
    return float(clat), float(clon), float(radius)


def _notam_view():
    """(center_lat, center_lon, radius_nm) for the NOTAM query — same center as
    _wx_view but a far tighter radius.  NOTAMs are much denser than METAR
    stations, so the 60-100 nm weather radius pulls hundreds of mostly-
    irrelevant items (one metro returns the whole region's NOTAMs).  Scope them
    to the immediate area and follow zoom (NOTAM_MIN/MAX_RADIUS_NM)."""
    if disp.get("display_mode") == "mfd":
        clat, clon = _mfd_effective_center()          # pan-aware
        rng = _mfd_last_range or 10                    # AUTO-resolved range
    else:
        clat = float(disp.get("lat", DEMO_LAT))
        clon = float(disp.get("lon", DEMO_LON))
        rng = int(disp["ds"].get("map_zoom_nm", 5)) or 5
    radius = max(NOTAM_MIN_RADIUS_NM, min(NOTAM_MAX_RADIUS_NM, rng * NOTAM_RADIUS_ZOOM_K))
    return float(clat), float(clon), float(radius)


def _notam_fetch(lat, lon, radius_nm):
    """Poller fetch shim: NOTAMs using the FAA credentials entered in
    Connectivity (cs) — falls back to the env vars, no-ops without either."""
    cs = disp.get("cs", {})
    return _wx.fetch_notams(
        lat, lon, radius_nm,
        client_id=(cs.get("notam_client_id") or "").strip() or None,
        client_secret=(cs.get("notam_client_secret") or "").strip() or None,
        env=(cs.get("notam_env") or "preprod").strip().lower())


def _fisb_locate(icao):
    """ident -> (lat, lon) for the FIS-B store's deferred geolocation.  FIS-B
    text weather carries no position, so we resolve it against the loaded
    airport DB."""
    r = _nav_lookup_ident(icao)
    return (r[1], r[2]) if r else None


def _fisb_nexrad_cells():
    """FIS-B (radio) NEXRAD intensity cells for the map, or [].  Hidden when the
    weather source is INTERNET-only (the radio picture isn't what you asked for)."""
    if disp["cs"].get("wx_source", "auto") == "internet":
        return []
    store = _fisb_store()
    return store.nexrad_cells() if store is not None else []


_fisb_rdr_cache = []
_fisb_rdr_at    = 0.0
_FISB_MERGE_INTERVAL_S = 3.0     # METARs change slowly; geolocate at most this often


def _fisb_rdr_snapshot():
    """Geolocated FIS-B (radio) METAR stations, rebuilt at most every few
    seconds.  Returns [] cheaply when no radio weather has arrived — the normal
    case until dump978 is feeding the 978 uplink — so the internet path is
    unchanged then."""
    global _fisb_rdr_cache, _fisb_rdr_at
    store = getattr(_adsb_client, "fisb", None) if _adsb_client else None
    if store is None or store.count() == 0:
        _fisb_rdr_cache = []
        return _fisb_rdr_cache
    now = time.monotonic()
    if now - _fisb_rdr_at >= _FISB_MERGE_INTERVAL_S or not _fisb_rdr_cache:
        _fisb_rdr_at = now
        _fisb_rdr_cache = store.metar_stations(_fisb_locate)
    return _fisb_rdr_cache


def _update_weather():
    """Pull the latest METAR snapshot from the background poller into
    disp["weather"] and mirror link diagnostics into cs.  METARs are cheap
    (text, ~120 s refresh) and feed the airport-tap weather picker on *every*
    map page, so the poller runs continuously — not just when the METAR
    overlay is up.  (NEXRAD, the heavy radar raster, stays gated to its own
    overlay in _update_nexrad.)

    Radio-primary / internet-bonus: FIS-B (978 UAT) METARs win per station, the
    internet poll backfills the rest — mirroring the traffic source model."""
    if _wx_client is None:
        return
    # Source mode mirrors traffic: AUTO merges (radio wins, internet backfills),
    # RADIO is FIS-B only (and pauses the internet pull — no data needed), INET
    # ignores the radio store.
    src = disp["cs"].get("wx_source", "auto")
    radio_only = (src == "radio")
    inet_only  = (src == "internet")
    _wx_client.paused = radio_only
    # Winds aloft is an INTERNET-ONLY product (there is no FIS-B winds-aloft), so
    # it always pre-loads and always shows regardless of the weather-source pill
    # — the same carve-out as the local traffic sensor: RADIO suppresses the
    # other internet weather, but winds has no radio alternative to fall back to,
    # so hiding it would just blank the layer.  The cache walks every stale zone
    # (the aircraft's first) one per tick, then idles until the ~6 h GFS cadence;
    # it's disk-cached, so a restart re-loads for free.
    if _winds_client is not None:
        _winds_client.enabled = True
    w = disp["weather"]
    inet = [] if radio_only else _wx_client.snapshot()
    rdr  = [] if inet_only  else _fisb_rdr_snapshot()
    if rdr:
        w["metars"] = _fisb.merge_metar_sources(rdr, inet)
        w["n_rdr"]  = sum(1 for m in w["metars"] if m.get("src") == "RDR")
    else:
        w["metars"] = inet
        w["n_rdr"]  = 0
    w["online"] = (_wx_client.connected and not radio_only) or bool(rdr)
    w["n"]      = len(w["metars"])
    w["n_inet"] = w["n"] - w["n_rdr"]
    # FIS-B ground stations we're hearing (radio reception cue + diagnostic),
    # and graphical hazard areas (G-AIRMET/SIGMET polygons for the MET overlay).
    _store = getattr(_adsb_client, "fisb", None) if _adsb_client else None
    global _taf_fed_at, _airsig_fed_at, _winds_fed_at, _notam_fed_at, _notam_pub_at
    # Winds always feeds the store — internet-only product, shown in every source
    # mode (see the enable carve-out above), so it isn't gated on radio_only.
    if (_store is not None and _winds_client is not None
            and _winds_client.updated_s != _winds_fed_at):
        _winds_fed_at = _winds_client.updated_s
        _store.set_winds(_winds_client.columns(), "INET")
    # Internet TAF + AIRMET/SIGMET backfill: fold each poller's snapshot into the
    # same FIS-B store the readouts already read, but only when it actually
    # refreshed (updated_s changed) and we're not radio-only.  Radio wins per
    # item inside the store, so this purely backfills.  AIRMET/SIGMET carry
    # geometry, so this also populates the MET-page graphical overlay.
    if _store is not None and not radio_only:
        if _taf_client is not None and _taf_client.updated_s != _taf_fed_at:
            _taf_fed_at = _taf_client.updated_s
            _store.add_tafs(_taf_client.snapshot())
        if _airsig_client is not None and _airsig_client.updated_s != _airsig_fed_at:
            _airsig_fed_at = _airsig_client.updated_s
            _store.add_airsigmets(_airsig_client.snapshot())
        if _notam_client is not None and _notam_client.updated_s != _notam_fed_at:
            _notam_fed_at = _notam_client.updated_s
            _store.add_notams(_notam_client.snapshot())
        # Re-broadcast our NOTAM snapshot to peer screens periodically — not only
        # on a fresh fetch (~10 min apart), so a peer that joins or restarts
        # between our fetches is fed within a broadcast interval instead of
        # waiting.  No-op without a key (empty snapshot); deduped on the store.
        if (_notam_client is not None
                and time.monotonic() - _notam_pub_at >= NOTAM_REBROADCAST_S):
            _notams = _notam_client.snapshot()
            if _notams:
                _ssync_publish_notams(_notams)
                _notam_pub_at = time.monotonic()
    w["stations"] = _store.ground_stations() if _store is not None else []
    w["graphics"] = _store.graphics() if _store is not None else []
    cs = disp["cs"]
    cs["wx_online"]   = _wx_client.connected
    cs["wx_rx"]       = _wx_client.rx_count
    cs["wx_err"]      = _wx_client.err_count
    cs["wx_last_err"] = _wx_client.last_err


def _update_nexrad():
    """Pause/resume the radar poller with the overlay; decode a new image to
    a pygame surface only when one arrives (seq change)."""
    if _nexrad_client is None:
        return
    show = bool(disp["ds"].get("map_show_nexrad"))
    _nexrad_client.paused = not show
    if not show:
        return
    png, bbox, seq = _nexrad_client.snapshot()
    if png is not None and seq != _nexrad_decoded["seq"]:
        try:
            img = pygame.image.load(io.BytesIO(png)).convert_alpha()
            _nexrad_decoded.update(seq=seq, surf=img, bbox=bbox)
        except Exception as e:                                # noqa: BLE001
            print(f"[NEXRAD] decode failed: {e}")


def _nexrad_render_arg():
    # The downloaded (internet) radar mosaic — hidden when the weather source is
    # RADIO-only, so RADIO shows just the FIS-B cells and vice-versa.
    if disp["cs"].get("wx_source", "auto") == "radio":
        return None
    if not disp["ds"].get("map_show_nexrad") or _nexrad_decoded["surf"] is None:
        return None
    return (_nexrad_decoded["surf"], _nexrad_decoded["bbox"],
            _nexrad_decoded["seq"])


# ── Font helpers ──────────────────────────────────────────────────────────────
_fonts = {}

def _get_font(size: int, bold: bool = False):
    # Scale font size proportionally when display is larger than 640×480
    _fs = getattr(sys.modules[__name__], '_font_scale', None)
    if _fs is None:
        try:
            from config import FONT_SCALE
            _fs = FONT_SCALE
        except ImportError:
            _fs = 1.0
        sys.modules[__name__]._font_scale = _fs
    size = int(size * _fs)
    key = (size, bold)
    if key not in _fonts:
        if bold:
            paths = [
                "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
            ]
        else:
            paths = [
                "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
            ]
        fnt = None
        for p in paths:
            try:
                fnt = pygame.font.Font(p, size)
                break
            except Exception:
                continue
        if fnt is None:
            fnt = pygame.font.SysFont("monospace", size, bold=bold)
        _fonts[key] = fnt
    return _fonts[key]


def _text(surf, txt, size, colour, cx=None, cy=None, x=None, y=None, bold=False):
    """Render text centred on (cx,cy) or top-left at (x,y)."""
    fnt = _get_font(size, bold)
    img = fnt.render(str(txt), True, colour)
    if cx is not None:
        rx = cx - img.get_width() // 2
    else:
        rx = x
    if cy is not None:
        ry = cy - img.get_height() // 2
    else:
        ry = y
    surf.blit(img, (rx, ry))
    return img.get_width()


def _chamfer(pts, indices, r=3):
    """Round polygon corners at given indices with smooth arcs (works for 90° corners)."""
    import math
    n = len(pts)
    out = []
    for i, p in enumerate(pts):
        if i not in indices:
            out.append(p)
            continue
        prev_p = pts[(i - 1) % n]
        next_p = pts[(i + 1) % n]
        dx1 = prev_p[0] - p[0]; dy1 = prev_p[1] - p[1]
        l1 = (dx1*dx1 + dy1*dy1) ** 0.5
        if l1: dx1 /= l1; dy1 /= l1
        dx2 = next_p[0] - p[0]; dy2 = next_p[1] - p[1]
        l2 = (dx2*dx2 + dy2*dy2) ** 0.5
        if l2: dx2 /= l2; dy2 /= l2
        sx = p[0] + dx1*r;  sy = p[1] + dy1*r
        ex = p[0] + dx2*r;  ey = p[1] + dy2*r
        acx = p[0] + dx1*r + dx2*r
        acy = p[1] + dy1*r + dy2*r
        a1 = math.atan2(sy - acy, sx - acx)
        a2 = math.atan2(ey - acy, ex - acx)
        cross = dx1 * dy2 - dy1 * dx2
        da = a2 - a1
        if cross < 0:
            while da < 0: da += 2 * math.pi
        else:
            while da > 0: da -= 2 * math.pi
        for j in range(5):
            angle = a1 + da * j / 4
            out.append((int(round(acx + r * math.cos(angle))),
                        int(round(acy + r * math.sin(angle)))))
    return out


def _rolling_drum(surf, bx, by, bw, bh, value, n_digits, color, font_sz,
                  suppress_leading=False, power_offset=0, show_adjacent=False,
                  adj_slot_h=None):
    """
    Veeder-Root rolling-drum digit readout for pygame.
    show_adjacent=True: adjacent digits are ~50% visible above/below (true drum look).
    Cascading: every digit carries smoothly when the digit below approaches 9→0.
    """
    char_w  = bw // n_digits
    f       = _get_font(font_sz, bold=True)
    val_int = round(abs(value))
    slot_h  = ((adj_slot_h if adj_slot_h is not None else bh // 2)
               if show_adjacent else bh)

    for col_i in range(n_digits):
        power = power_offset + n_digits - 1 - col_i
        if suppress_leading and power > 0 and val_int < 10 ** power:
            continue

        if power == 0:
            d_cont = float(value % 10.0)
        else:
            lower_cont = (value % (10 ** power)) / (10 ** (power - 1))
            carry_frac = max(0.0, lower_cont - 9.0)
            d_lo   = (int(value) // (10 ** power)) % 10
            d_cont = float(d_lo) + carry_frac

        d_lo   = int(d_cont)
        frac   = d_cont - d_lo
        d_hi   = (d_lo + 1) % 10
        scroll = int(frac * slot_h)
        cx     = bx + col_i * char_w

        img_lo = f.render(str(d_lo), True, color)
        img_hi = f.render(str(d_hi), True, color)
        gw     = img_lo.get_width()
        gh     = img_lo.get_height()
        tx     = max(0, (char_w - gw) // 2)
        cell   = pygame.Surface((char_w, bh), pygame.SRCALPHA)

        if show_adjacent:
            d_prev   = (d_lo - 1 + 10) % 10
            d_hi2    = (d_lo + 2) % 10
            img_prev = f.render(str(d_prev), True, color)
            img_hi2  = f.render(str(d_hi2),  True, color)
            ty_lo    = bh // 2 - gh // 2 + scroll   # reversed: lo scrolls down
            cell.blit(img_hi2,  (tx, ty_lo - 2 * slot_h))  # two steps above
            cell.blit(img_hi,   (tx, ty_lo - slot_h))       # hi  (higher) above
            cell.blit(img_lo,   (tx, ty_lo))
            cell.blit(img_prev, (tx, ty_lo + slot_h))       # prev (lower) below
        else:
            ty_lo = (bh - gh) // 2 + scroll   # reversed: lo scrolls down
            cell.blit(img_hi, (tx, ty_lo - bh))   # hi (higher) one slot above
            cell.blit(img_lo, (tx, ty_lo))

        surf.blit(cell, (cx, by))


def _drum_shade(surf, bx, by, bw, bh):
    """Overlay a top-and-bottom fade-to-dark gradient on the drum window."""
    shade = pygame.Surface((bw, bh), pygame.SRCALPHA)
    shade.fill((0, 0, 0, 0))
    fade = bh // 3
    for i in range(fade):
        a = int(210 * (fade - i) / fade)
        pygame.draw.line(shade, (0, 5, 15, a), (0, i),      (bw-1, i))
        pygame.draw.line(shade, (0, 5, 15, a), (0, bh-1-i), (bw-1, bh-1-i))
    surf.blit(shade, (bx, by))


def _rolling_drum_alt20(surf, bx, by, bw, bh, alt, color, font_sz, show_adjacent=False,
                        adj_slot_h=None):
    """
    Altimeter Veeder-Root drum: both digits scroll together in 20-foot steps.
    Labels '00','20','40','60','80' move as a unit.
    """
    _LABELS = ("00", "20", "40", "60", "80")
    f = _get_font(font_sz, bold=True)

    drum_pos = (alt % 100) / 20
    d_lo_idx = int(drum_pos) % 5
    frac     = drum_pos - int(drum_pos)
    slot_h   = ((adj_slot_h if adj_slot_h is not None else bh // 2)
                if show_adjacent else bh)
    scroll   = int(frac * slot_h)

    img_lo = f.render(_LABELS[d_lo_idx], True, color)
    gw = img_lo.get_width()
    gh = img_lo.get_height()
    tx = max(0, (bw - gw) // 2)
    cell = pygame.Surface((bw, bh), pygame.SRCALPHA)

    if show_adjacent:
        d_prev_idx = (d_lo_idx - 1 + 5) % 5
        d_hi_idx   = (d_lo_idx + 1) % 5
        d_hi2_idx  = (d_lo_idx + 2) % 5
        img_prev = f.render(_LABELS[d_prev_idx], True, color)
        img_hi   = f.render(_LABELS[d_hi_idx],   True, color)
        img_hi2  = f.render(_LABELS[d_hi2_idx],  True, color)
        ty_lo    = bh // 2 - gh // 2 + scroll   # reversed: lo scrolls down
        cell.blit(img_hi2,  (tx, ty_lo - 2 * slot_h))  # two steps above
        cell.blit(img_hi,   (tx, ty_lo - slot_h))       # hi  (higher) above
        cell.blit(img_lo,   (tx, ty_lo))
        cell.blit(img_prev, (tx, ty_lo + slot_h))       # prev (lower) below
    else:
        d_hi_idx = (d_lo_idx + 1) % 5
        img_hi = f.render(_LABELS[d_hi_idx], True, color)
        ty_lo = (bh - gh) // 2 + scroll   # reversed: lo scrolls down
        cell.blit(img_hi, (tx, ty_lo - bh))   # hi (higher) one slot above
        cell.blit(img_lo, (tx, ty_lo))

    surf.blit(cell, (bx, by))


def lerp(a, b, t):
    return a + (b - a) * max(0.0, min(1.0, t))


def lerp_col(a, b, t):
    return tuple(int(lerp(a[i], b[i], t)) for i in range(3))


# ── SVT background ────────────────────────────────────────────────────────────
_svt_cache: dict = {}
_svt_frame = 0
SVT_UPDATE_FRAMES = 3   # update terrain every N frames (~10 Hz at 30 fps)


def get_svt_surface(ai_w, ai_h, pitch, roll, hdg, alt, lat, lon, polylines=None):
    """Dispatch to OpenGL or pygame SVT renderer based on config + availability.

    With the OpenGL renderer, we render every frame (no caching) since the
    GPU update is essentially free at the Pi 4's frame rate.  The pygame
    fallback caches every SVT_UPDATE_FRAMES frames to keep up with 30 fps.

    ``polylines`` (HITS boxes, direct-to trace) are rendered into the
    standalone-EGL pass when SVT_RENDERER == "opengl"; pygame fallback
    ignores them (the live Pi 4 always uses GL, so the pygame path only
    runs on hardware without EGL).
    """
    global _svt_frame
    _svt_frame += 1

    use_gl = (SVT_RENDERER == "opengl") and _SVT_GL_AVAILABLE
    if use_gl:
        surf = render_svt_gl(SRTM_DIR, ai_w, ai_h, pitch, roll, hdg, alt,
                             lat, lon, polylines=polylines)
        if surf is not None:
            return surf
        # Fall through to pygame on render failure

    key = "svt"
    if key not in _svt_cache or _svt_frame % SVT_UPDATE_FRAMES == 0:
        surf = render_svt_pygame(
            SRTM_DIR, ai_w, ai_h, pitch, roll, hdg, alt, lat, lon
        )
        _svt_cache[key] = surf
    return _svt_cache[key]


def draw_ai_background(surf, ai_rect, pitch, roll, hdg, alt, lat, lon,
                       polylines=None):
    """Draw SVT sky/terrain background into ai_rect region of surf.

    ``polylines`` flows through to the standalone-EGL path so HITS
    boxes and the direct-to course trace render in the offline
    preview captures, not just the live shared-GL path."""
    ax, ay, aw, ah = ai_rect
    bg = get_svt_surface(aw, ah, pitch, roll, hdg, alt, lat, lon,
                         polylines=polylines)
    surf.blit(bg, (ax, ay))


# Unusual-attitude thresholds.  Past these the SVT mesh and symbol
# overlays come off (declutter) and a sky/ground split with red
# recovery chevrons replaces them — same cue Garmin / Honeywell use.
EXTREME_PITCH_DEG = 30.0
EXTREME_BANK_DEG  = 60.0


def is_extreme_attitude(pitch_deg, roll_deg):
    return (abs(pitch_deg) > EXTREME_PITCH_DEG
            or abs(roll_deg) > EXTREME_BANK_DEG)


def normalize_attitude(pitch_deg, roll_deg):
    """Remap pitch outside ±90° to its equivalent in-range Euler form.

    When the aircraft goes past vertical (over-the-top loop, split-S,
    aerobatic inverted flight) the AHRS reports pitch values >90° or
    <-90°. The rest of the AI pipeline assumes pitch ∈ [-90, +90] for
    the horizon math and ladder placement to stay visually continuous,
    so reflect the pitch back into range and shift roll by 180° — the
    physical attitude is unchanged, just expressed in the Euler chart
    the renderer can draw without going wonky.
    """
    if pitch_deg > 90.0:
        pitch_deg = 180.0 - pitch_deg
        roll_deg += 180.0
    elif pitch_deg < -90.0:
        pitch_deg = -180.0 - pitch_deg
        roll_deg += 180.0
    # Wrap roll to (-180, 180]
    roll_deg = ((roll_deg + 180.0) % 360.0) - 180.0
    return pitch_deg, roll_deg


def _draw_chevron_stack(surf, cx, cy, size, direction, color, count=3, gap=4):
    """Draw `count` V-shaped chevrons stacked in `direction`. Filled
    polygons — anti-aliasing the outline buys nothing at this size and
    costs us another smoothscale pass we don't need on the render
    thread during a recovery."""
    step = size + gap
    for i in range(count):
        d = i * step
        if direction == 'up':
            tip = (cx, cy - d)
            l   = (cx - size, cy - d + size)
            r   = (cx + size, cy - d + size)
        elif direction == 'down':
            tip = (cx, cy + d)
            l   = (cx - size, cy + d - size)
            r   = (cx + size, cy + d - size)
        elif direction == 'left':
            tip = (cx - d, cy)
            l   = (cx - d + size, cy - size)
            r   = (cx - d + size, cy + size)
        else:  # 'right'
            tip = (cx + d, cy)
            l   = (cx + d - size, cy - size)
            r   = (cx + d - size, cy + size)
        pygame.draw.polygon(surf, color, [tip, l, r])
        pygame.gfxdraw.aapolygon(surf, [tip, l, r], color)


def _draw_roll_recovery_arc(surf, cx, cy, radius, direction, color, width=5):
    """Curved arrow telling the pilot to roll the wings in `direction`.

    Arc curves over the top of (cx, cy) and the arrowhead lands at the
    leading edge of the arc's motion. For direction='left' the arc
    sweeps counter-clockwise (upper-right → over-the-top → upper-left)
    with the arrowhead pointing further along that CCW direction —
    matches the horizon's rotational motion as the pilot rolls left.
    'right' is the mirror.
    """
    def pt(a_local_deg):
        a = math.radians(-90.0 + a_local_deg)
        return (cx + radius * math.cos(a), cy + radius * math.sin(a))

    span = 120.0
    if direction == 'left':
        a_start, a_end = +span / 2, -span / 2   # CCW
    else:
        a_start, a_end = -span / 2, +span / 2   # CW

    n = 24
    pts = [pt(a_start + (a_end - a_start) * i / n) for i in range(n + 1)]
    pygame.draw.lines(surf, color, False,
                      [(int(p[0]), int(p[1])) for p in pts], width)
    pygame.draw.aalines(surf, color, False, pts)

    # Arrowhead at the end: tip along the tangent direction.
    last, prev = pts[-1], pts[-2]
    dx, dy = last[0] - prev[0], last[1] - prev[1]
    length = math.hypot(dx, dy) or 1.0
    dx /= length; dy /= length
    px_, py_ = -dy, dx   # perpendicular unit vector
    head_len = max(14, radius * 0.24)
    head_wid = max(10, radius * 0.17)
    tip = (last[0] + dx * head_len, last[1] + dy * head_len)
    bl  = (last[0] + px_ * head_wid, last[1] + py_ * head_wid)
    br  = (last[0] - px_ * head_wid, last[1] - py_ * head_wid)
    pts_arrow = [(int(tip[0]), int(tip[1])),
                 (int(bl[0]),  int(bl[1])),
                 (int(br[0]),  int(br[1]))]
    pygame.draw.polygon(surf, color, pts_arrow)
    pygame.gfxdraw.aapolygon(surf, pts_arrow, color)


def draw_unusual_attitude_arrows(surf, ai_rect, pitch_deg, roll_deg):
    """Recovery cues for unusual attitudes.

    Both glyphs are centred on the ownship so the pilot's eye doesn't
    have to leave the centre of the AI during a recovery. Pitch arrows
    live inside the roll arc — the arc is sized to enclose them — and
    both can appear simultaneously when both pitch and bank are
    extreme.

    Pitch: short linear chevron stack centred at the ownship, tips
    pointing toward the corrective input (down to push from nose-high,
    up to pull from nose-low).

    Roll: a large curved arrow sweeping over the ownship indicating the
    rotational direction of needed input. Bigger radius than the pitch
    chevron extent so it reads as a frame around them, not a glyph in
    the same visual band.
    """
    ax, ay, aw, ah = ai_rect
    cx, cy = ax + aw // 2, ay + ah // 2
    arrow = max(14, int(min(aw, ah) * 0.045))
    red   = (220, 30, 30)

    # _draw_chevron_stack anchors the first chevron at the call-site
    # (cx, cy) and stacks outward — for a 3-chevron stack this puts the
    # geometric centre half a step + half a chevron off from cy. Offset
    # the call so the stack's actual midline sits on cy.
    gap     = 4
    count   = 3
    step    = arrow + gap
    stack_offset = ((count - 1) * step - arrow) // 2

    if pitch_deg > EXTREME_PITCH_DEG:
        _draw_chevron_stack(surf, cx, cy - stack_offset, arrow, 'down', red,
                            count=count, gap=gap)
    elif pitch_deg < -EXTREME_PITCH_DEG:
        _draw_chevron_stack(surf, cx, cy + stack_offset, arrow, 'up', red,
                            count=count, gap=gap)

    if abs(roll_deg) > EXTREME_BANK_DEG:
        # Radius large enough that the arc's lowest points (the arc
        # endpoints) sit clear of the pitch chevron stack's outermost
        # tip. Pitch stack reaches ±((count-1)/2 * step + arrow) from
        # cy; pad ~20 % beyond so the arc reads as a frame, not a
        # collision.
        pitch_reach = int((count - 1) * step / 2 + arrow)
        radius = max(int(pitch_reach * 1.6),
                     int(min(aw, ah) * 0.22))
        direction = 'left' if roll_deg > 0 else 'right'
        _draw_roll_recovery_arc(surf, cx, cy, radius, direction, red)


def draw_simple_ai_background(surf, ai_rect, pitch, roll):
    """
    Fallback SVT background (no SRTM tiles loaded).
    Draws sky/ground split directly into ai_rect using polygon fill + clipping.
    No large surface rotation — runs in < 1 ms on Pi Zero 2W.
    """
    ax, ay, aw, ah = ai_rect
    GND_NEAR = ( 80, 110,  40)
    GND_MID  = (120,  85,  38)
    GND_FAR  = ( 70,  50,  25)

    px_per_deg = ah / 48.0   # scale with AI height (10.0 at 480px)
    old_clip   = surf.get_clip()
    surf.set_clip(pygame.Rect(ax, ay, aw, ah))

    cx  = ax + aw // 2
    # Anchor horizon at TAPE_MID — same reference as draw_pitch_ladder's
    # zero-pitch line.  _full_ai (the rect this is called with) has its
    # geometric centre above TAPE_MID, which produced a ~1° gap between
    # this fallback horizon and the pitch ladder when SVT couldn't paint
    # (no GPS / no SRTM tiles).
    cy  = TAPE_MID

    roll_rad = math.radians(roll)
    cos_r, sin_r = math.cos(roll_rad), math.sin(roll_rad)

    # Horizon point: the point on the horizon line closest to the camera
    # centre. At pitch=θ, roll=0 it sits at (cx, cy + θ*px_per_deg) —
    # directly below. Rolling rotates that point around (cx, cy) so at
    # roll=180° the horizon ends up *above* the centre, not below.
    #
    # Sign convention must match draw_pitch_ladder's _rv() which rotates
    # body coords (0, pitch_px) to screen (cx + pitch_px*sin_r,
    # cy + pitch_px*cos_r). An earlier fix had the lateral term as
    # -sin_r which agreed with the polygon math internally but landed
    # on the *opposite* side of centre from the pitch ladder — visible
    # as the horizon line and the sky/ground regions disagreeing with
    # the pitch ladder beyond ~60° of bank.
    pitch_offset = pitch * px_per_deg
    hcx = cx + pitch_offset * sin_r
    hcy = cy + pitch_offset * cos_r

    # Extend horizon line well beyond the rect so clipping takes care of edges.
    # Line direction in pygame Y-down is (cos_r, -sin_r); for positive roll
    # (right bank) that's "right and up" → LEFT-DOWN, RIGHT-UP, matching the
    # pitch-ladder white horizon and the SVT GL terrain horizon.
    R  = aw + ah
    h1 = (hcx - R * cos_r, hcy + R * sin_r)
    h2 = (hcx + R * cos_r, hcy - R * sin_r)

    # Classify each corner relative to the (h1, h2) line.  The implicit
    # equation of that line is sin_r*(px-hcx) + cos_r*(py-hcy) = 0; sky is
    # the side where py is "above" (smaller y in pygame), giving
    #     sky_side(p) = (hcy - py)*cos_r + (hcx - px)*sin_r > 0.
    # NB: the older form used (px - hcx) which flipped the sin term and
    # silently classified points against a different line — visible only at
    # non-zero roll, where the polygon edge and the drawn h1-h2 white line
    # disagreed.
    def _sky_side(px, py):
        return (hcy - py) * cos_r + (hcx - px) * sin_r > 0

    corners = [(ax, ay), (ax + aw, ay), (ax + aw, ay + ah), (ax, ay + ah)]

    # Build sky polygon: traverse rect corners in order, insert horizon
    # intersection points where the boundary crosses from sky to ground or vice versa.
    sky_poly = []
    for i, c in enumerate(corners):
        nc = corners[(i + 1) % 4]
        c_sky  = _sky_side(c[0],  c[1])
        nc_sky = _sky_side(nc[0], nc[1])
        if c_sky:
            sky_poly.append(c)
        if c_sky != nc_sky:
            dx, dy = nc[0] - c[0], nc[1] - c[1]
            denom  = dy * cos_r + dx * sin_r
            if abs(denom) > 1e-6:
                t  = ((hcy - c[1]) * cos_r + (hcx - c[0]) * sin_r) / denom
                sky_poly.append((c[0] + t * dx, c[1] + t * dy))

    # Fill ground first (covers whole rect), then paint sky polygon on top
    surf.fill(GND_MID, (ax, ay, aw, ah))
    if sky_poly and len(sky_poly) >= 3:
        pygame.draw.polygon(surf, SKY_HOR, sky_poly)
    elif all(_sky_side(c[0], c[1]) for c in corners):
        surf.fill(SKY_HOR, (ax, ay, aw, ah))

    # Horizon line (extended; clipped to AI rect by set_clip above)
    pygame.draw.line(surf, WHITE,
                     (int(h1[0]), int(h1[1])), (int(h2[0]), int(h2[1])), 2)

    surf.set_clip(old_clip)


# ── Pitch ladder ──────────────────────────────────────────────────────────────
def draw_zero_pitch_line(surf, ai_rect, pitch, roll):
    """Draw the zero-pitch reference line across the AI.

    The line represents the aircraft's 0° pitch reference — i.e. the
    straight-and-level horizon line in the sky frame.  It moves with pitch
    (drops below AI centre when pitched up, rises when pitched down) and
    rotates with roll, tracking where the "true" horizon would be on a
    flat-earth model.

    Rendered as a pair of hash marks with a gap for the aircraft symbol.
    Cyan to distinguish it from the white terrain horizon of the SVT.
    """
    ax, ay, aw, ah = ai_rect
    cy = ay + ah // 2
    cx = ax + aw // 2
    gap_half = int(aw * 0.20)
    end_half = int(aw * 0.42)

    # Same pitch scale as the pitch ladder so the zero-pitch hash marks
    # line up exactly with the ladder's 0° bar position.
    px_per_deg = ah / 48.0
    pitch_px = int(pitch * px_per_deg)   # + = nose up = line below centre

    # Rotate around (cx, cy) by -roll and offset vertically by pitch_px.
    # Endpoints before rotation lie on a horizontal line at y=pitch_px below cy.
    theta = math.radians(-roll)
    c = math.cos(theta)
    s = math.sin(theta)

    def rot(dx, dy):
        return (cx + int(dx * c - dy * s),
                cy + int(dx * s + dy * c))

    l1 = rot(-end_half, pitch_px); l2 = rot(-gap_half, pitch_px)
    r1 = rot( gap_half, pitch_px); r2 = rot( end_half, pitch_px)
    pygame.draw.line(surf, CYAN, l1, l2, 2)
    pygame.draw.line(surf, CYAN, r1, r2, 2)


def draw_pitch_ladder(surf, ai_rect, pitch, roll):
    """
    White pitch ladder lines drawn directly in rotated coordinates.
    No intermediate surface or transform.rotate — fast on Pi Zero 2W.
    """
    ax, ay, aw, ah = ai_rect
    cx, cy = ax + aw // 2, ay + ah // 2

    px_per_deg = ah / 48.0   # scale with AI height (10.0 at 480px)
    pitch_px   = int(pitch * px_per_deg)

    major_half = int(aw * 0.07)   # ~34 px
    minor_half = int(aw * 0.04)   # ~19 px

    # Precompute rotation basis (pygame CCW rotation in Y-down screen coords):
    #   rotated_x = x * cos_r + y * sin_r
    #   rotated_y = -x * sin_r + y * cos_r
    roll_rad = math.radians(roll)
    cos_r    = math.cos(roll_rad)
    sin_r    = math.sin(roll_rad)

    def _rv(x, y):
        """Rotate vector (x,y) and offset to surf coords."""
        return (int(cx + x * cos_r + y * sin_r),
                int(cy - x * sin_r + y * cos_r))

    # Clip to AI rect so lines don't bleed into tapes / heading tape
    old_clip = surf.get_clip()
    surf.set_clip(pygame.Rect(ax, ay, aw, ah))

    # Pitch ladder runs from ±80° so the ladder still gives the pilot a
    # readable scale during an unusual-attitude recovery.  Lines outside
    # the visible window are culled below; the loop bounds just gate
    # which pitch values are eligible to draw.
    for deg in range(-80, 85, 5):
        rel_y = pitch_px - int(deg * px_per_deg)  # y offset from AI center

        # Cull lines too far from the visible window (±185 px from centre)
        if rel_y < -185 or rel_y > 185:
            continue

        major = (deg % 10 == 0)
        half  = major_half if major else minor_half
        # At extreme attitudes shorten the lines progressively so the
        # ladder reads visually different from the cruise band — same
        # cue Garmin uses to flag "you're a long way from level".
        if abs(deg) > 60:
            half = int(half * 0.45)
        elif abs(deg) > 30:
            half = int(half * 0.70)

        if deg == 0:
            # Horizon line
            p1 = _rv(-half, rel_y)
            p2 = _rv( half, rel_y)
            pygame.draw.line(surf, (255, 255, 255, 200), p1, p2, 4)
            continue

        col = (255, 255, 255, 220)
        p1  = _rv(-half, rel_y)
        p2  = _rv( half, rel_y)

        if major:
            pygame.draw.line(surf, col, p1, p2, 4)
        else:
            pygame.draw.line(surf, col, p1, p2, 2)

        # Tick marks: 8 px inward (toward horizon = toward centre of AI)
        tick = 8 if deg > 0 else -8
        pygame.draw.aaline(surf, col, p1, _rv(-half, rel_y + tick))
        pygame.draw.aaline(surf, col, p2, _rv( half, rel_y + tick))

        # Degree labels at major lines (drawn without rotation for speed)
        if major:
            lbl = str(abs(deg))
            fnt = _get_font(16)
            img = fnt.render(lbl, True, (255, 255, 255))
            # Position label just outside each end of the line
            lx1, ly1 = _rv(-half - img.get_width() - 4, rel_y - 8)
            lx2, ly2 = _rv( half + 4,                   rel_y - 8)
            surf.blit(img, (lx1, ly1))
            surf.blit(img, (lx2, ly2))

    surf.set_clip(old_clip)


# gfxdraw.filled_polygon and gfxdraw.aapolygon both fail to write
# per-pixel alpha on SRCALPHA surfaces — fills/outlines come out invisible
# on the shared-GL composite surface. pygame.draw.polygon writes the fill
# at alpha=255 correctly. aa=True adds a pygame.draw.aalines pass for
# anti-aliased edges; on small markers (doghouses, etc.) that AA pass
# bleeds outward as a visible halo, so callers that prefer hard edges
# pass aa=False.
def _filled_polygon(surf, points, color, aa=True):
    pygame.draw.polygon(surf, color, points)
    if aa:
        pygame.draw.aalines(surf, color, True, points)


# Anti-aliased polygon outline that doesn't stair-step on oblique edges.
# pygame.draw.polygon(width=N) renders a hard-edge N-pixel stroke, and
# pygame.gfxdraw.aapolygon only smooths a 1-pixel outline centred on the
# polygon coords — so the outer pixel of the 2-px outline stays jaggy on
# the Veeder-Root pointer angles (the chamfered corners are fine, the
# pointer diagonals are the problem). Supersampling at 2× and bilinear
# downscaling gives a clean AA stroke at any angle for the cost of a
# small SRCALPHA surface and one smoothscale.
def _aa_polygon_outline(surf, points, color, width=2, pad=2):
    if not points:
        return
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x0, y0 = min(xs) - pad, min(ys) - pad
    x1, y1 = max(xs) + pad, max(ys) + pad
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0:
        return
    big = pygame.Surface((w * 2, h * 2), pygame.SRCALPHA)
    pts_2x = [((px - x0) * 2, (py - y0) * 2) for px, py in points]
    pygame.draw.polygon(big, color, pts_2x, width=width * 2)
    small = pygame.transform.smoothscale(big, (w, h))
    surf.blit(small, (x0, y0))


# ── Roll arc ──────────────────────────────────────────────────────────────────
def _doghouse_pts(cx, cy, ang_rad, r, size=11, inward=True):
    """
    Pentagon 'doghouse' pointer at radius r.
    inward=True : tip at r points toward centre (used outside the arc).
    inward=False: tip at r points away from centre (used inside the arc).
    """
    out_x  = math.cos(ang_rad);  out_y  = math.sin(ang_rad)
    perp_x = -out_y;             perp_y =  out_x
    if inward:
        tip_r  = r
        base_r = r + size * 1.3
        roof_r = r + size * 0.6
    else:
        tip_r  = r
        base_r = r - size * 1.3
        roof_r = r - size * 0.6
    half_w  = size * 0.7
    roof_hw = size * 0.35
    return [
        (int(cx + base_r * out_x - half_w  * perp_x),
         int(cy + base_r * out_y - half_w  * perp_y)),
        (int(cx + roof_r * out_x - roof_hw * perp_x),
         int(cy + roof_r * out_y - roof_hw * perp_y)),
        (int(cx + tip_r  * out_x), int(cy + tip_r  * out_y)),
        (int(cx + roof_r * out_x + roof_hw * perp_x),
         int(cy + roof_r * out_y + roof_hw * perp_y)),
        (int(cx + base_r * out_x + half_w  * perp_x),
         int(cy + base_r * out_y + half_w  * perp_y)),
    ]


def draw_roll_arc(surf, roll):
    """Draw GI-275 style roll scale: arc, tick marks, doghouse zero marker,
    and doghouse roll pointer.
    Uses pygame.draw.arc (single C call) instead of 121-iteration Python loop.
    """
    cx, cy = CX, ROLL_CY

    # ── Arc: 120° span centred at 12 o'clock, rotated by roll ────────────────
    # Drawn as a single thick polyline along the centreline radius (not a
    # band polygon): a band gets a weird double-AA halo when run through
    # _filled_polygon (aalines traces both inner and outer edges), whereas
    # a thick line is one clean stroke.
    _ARC_STEPS = 80
    _ARC_THICK = 4  # pixels of arc band thickness
    arc_pts = []
    for i in range(_ARC_STEPS + 1):
        # Sky-pointer design: arc rotates WITH the sky/horizon so the fixed
        # aircraft reference at the top of the screen reads the current bank.
        # In pygame Y-down, right bank (positive roll) rotates the sky CCW
        # visually, which means pygame angles DECREASE (hence -roll).
        ang = (-90 - roll - 60 + i * 120.0 / _ARC_STEPS) * DEG
        cos_a, sin_a = math.cos(ang), math.sin(ang)
        arc_pts.append((int(cx + ROLL_R * cos_a),
                        int(cy + ROLL_R * sin_a)))
    pygame.draw.lines(surf, WHITE, False, arc_pts, _ARC_THICK)

    # ── Tick marks — rotate with sky, solid white, 2px width ─────────────────
    for deg2, length in [(10, 9), (20, 9), (30, 13),
                         (-10, 9), (-20, 9), (-30, 13),
                         (45, 9), (-45, 9), (60, 11), (-60, 11)]:
        ang = (-90 - roll + deg2) * DEG   # rotate with the sky
        cos_a, sin_a = math.cos(ang), math.sin(ang)
        x1 = int(cx + (ROLL_R - length) * cos_a)
        y1 = int(cy + (ROLL_R - length) * sin_a)
        x2 = int(cx + (ROLL_R + _ARC_THICK) * cos_a)
        y2 = int(cy + (ROLL_R + _ARC_THICK) * sin_a)
        pygame.draw.line(surf, WHITE, (x1, y1), (x2, y2), 2)
        # Hollow triangles at ±45
        if abs(deg2) == 45:
            perp = ang + math.pi / 2
            tx2, ty2 = int(5 * math.cos(perp)), int(5 * math.sin(perp))
            mx, my = (x1 + x2) // 2, (y1 + y2) // 2
            inner_x = int(cx + (ROLL_R - 16) * cos_a)
            inner_y = int(cy + (ROLL_R - 16) * sin_a)
            tri = [(mx - tx2, my - ty2), (mx + tx2, my + ty2), (inner_x, inner_y)]
            pygame.gfxdraw.aapolygon(surf, tri, LTGREY)

    # Moving upper doghouse — OUTSIDE arc, tip at arc, rotates with the arc
    # (sky pointer).  At right bank it moves to upper-left (same direction
    # as the arc's 0° tick).
    upper_ang = (-90 - roll) * DEG
    tri0 = _doghouse_pts(cx, cy, upper_ang, ROLL_R + 2, size=10, inward=True)
    _filled_polygon(surf, tri0, WHITE, aa=False)

    # Fixed lower doghouse — INSIDE arc, tip at arc-8, fixed at 12 o'clock
    roll_ang = -math.pi / 2
    rp_pts = _doghouse_pts(cx, cy, roll_ang, ROLL_R - 8, size=10, inward=False)
    _filled_polygon(surf, rp_pts, WHITE, aa=False)


# ── Aircraft symbol ───────────────────────────────────────────────────────────
AMBER      = (255, 190,  30)   # slightly warmer than YELLOW for symbol fill
AMBER_DARK = (180, 120,   0)   # shadow/outline

def draw_aircraft_symbol(surf):
    """Swept delta wing aircraft reference with engine nacelles, 1.5× scale."""
    # Wing panels — apex at (CX, CY), trailing edge at CY+44 (1.5× original 29)
    # Outer strip = leading-edge side (lighter/top); Inner strip = trailing-edge side (darker/bottom)
    # Fills — inner/outer strips, no outline so colour-split edge stays clean
    # Inner edge moved ±69 → ±57 (50% wider base; outer edge ±93 unchanged)
    # Bisect at ±75 = midpoint of ±57..±93, giving equal-width inner/outer strips
    li = [(CX, CY), (CX - 75, CY + 44), (CX - 57, CY + 44)]   # L inner (darker)
    lo = [(CX, CY), (CX - 93, CY + 44), (CX - 75, CY + 44)]   # L outer (lighter)
    ri = [(CX, CY), (CX + 57, CY + 44), (CX + 75, CY + 44)]   # R inner (darker)
    ro = [(CX, CY), (CX + 75, CY + 44), (CX + 93, CY + 44)]   # R outer (lighter)
    _filled_polygon(surf, li, AMBER_DARK)
    pygame.gfxdraw.aapolygon(surf, li, AMBER_DARK)
    _filled_polygon(surf, lo, AMBER)
    pygame.gfxdraw.aapolygon(surf, lo, AMBER)
    _filled_polygon(surf, ri, AMBER_DARK)
    pygame.gfxdraw.aapolygon(surf, ri, AMBER_DARK)
    _filled_polygon(surf, ro, AMBER)
    pygame.gfxdraw.aapolygon(surf, ro, AMBER)

    # Engine nacelles — fills
    lu = [(CX - 93, CY), (CX - 99, CY - 6), (CX - 138, CY - 6), (CX - 138, CY)]
    ll = [(CX - 93, CY), (CX - 138, CY),    (CX - 138, CY + 6), (CX - 99, CY + 6)]
    ru = [(CX + 93, CY), (CX + 99, CY - 6), (CX + 138, CY - 6), (CX + 138, CY)]
    rl = [(CX + 93, CY), (CX + 138, CY),    (CX + 138, CY + 6), (CX + 99, CY + 6)]
    _filled_polygon(surf, lu, AMBER)
    pygame.gfxdraw.aapolygon(surf, lu, AMBER)
    _filled_polygon(surf, ll, AMBER_DARK)
    pygame.gfxdraw.aapolygon(surf, ll, AMBER_DARK)
    _filled_polygon(surf, ru, AMBER)
    pygame.gfxdraw.aapolygon(surf, ru, AMBER)
    _filled_polygon(surf, rl, AMBER_DARK)
    pygame.gfxdraw.aapolygon(surf, rl, AMBER_DARK)

    # Outer perimeter outlines — no line across the inner colour-split edge
    BLK = (0, 0, 0)
    lw = [(CX, CY), (CX - 93, CY + 44), (CX - 57, CY + 44)]
    rw = [(CX, CY), (CX + 57, CY + 44), (CX + 93, CY + 44)]
    ln = [(CX - 93, CY), (CX - 99, CY - 6), (CX - 138, CY - 6), (CX - 138, CY + 6), (CX - 99, CY + 6)]
    rn = [(CX + 93, CY), (CX + 99, CY - 6), (CX + 138, CY - 6), (CX + 138, CY + 6), (CX + 99, CY + 6)]
    pygame.gfxdraw.aapolygon(surf, lw, BLK)
    pygame.gfxdraw.aapolygon(surf, rw, BLK)
    pygame.gfxdraw.aapolygon(surf, ln, BLK)
    pygame.gfxdraw.aapolygon(surf, rn, BLK)


# ── Slip/skid indicator ───────────────────────────────────────────────────────
def draw_slip_ball(surf, ay):
    """Slip indicator: thin bar that slides under the fixed zero-bank triangle."""
    slip_y = ROLL_CY - ROLL_R + 24  # below lower doghouse base (y≈40)
    max_d  = 12
    defl   = int(max(-max_d, min(max_d, (ay / 0.2) * max_d)))
    pygame.draw.rect(surf, WHITE, (CX + defl - 8, slip_y, 16, 4))


# ── Speed tape ────────────────────────────────────────────────────────────────
PX_PER_KT  = TAPE_H / 120.0   # 120 kt visible range
PX_PER_FT  = TAPE_H / 600.0   # 600 ft visible range
PX_PER_DEG = DISPLAY_W / 120.0  # 120° visible heading range (half spacing)


def spd_y(v, speed): return int(TAPE_MID - (v - speed) * PX_PER_KT)
def alt_y(ft, alt):  return int(TAPE_MID - (ft - alt)  * PX_PER_FT)


_spd_tape_bg = None   # cached speed-tape background surface
_alt_tape_bg = None   # cached alt-tape background surface


def draw_speed_tape(surf, speed, gs_bug=None,
                    vs0=VS0, vs1=VS1, vfe=VFE, vno=VNO, vne=VNE,
                    airspeed_src="gps"):
    """Left airspeed tape with GI-275-style V-speed colour bands.
    V-speed params should already be in the same unit as *speed*."""
    # Background — cached to avoid a new SRCALPHA Surface allocation every frame
    global _spd_tape_bg
    if _spd_tape_bg is None:
        _spd_tape_bg = pygame.Surface((SPD_W, TAPE_BOT), pygame.SRCALPHA)
        _spd_tape_bg.fill(TAPE_BG)
    surf.blit(_spd_tape_bg, (SPD_X, 0))
    pygame.draw.line(surf, (255, 255, 255, 60), (SPD_X + SPD_W, 0),
                     (SPD_X + SPD_W, TAPE_BOT), 1)

    def sy(v): return spd_y(v, speed)

    # V-speed colour bands (right edge of tape)
    def _band(v_lo, v_hi, col, bar_x, bar_w=4):
        y1 = sy(v_hi)
        y2 = sy(v_lo)
        y1c = max(TAPE_TOP, min(TAPE_BOT, y1))
        y2c = max(TAPE_TOP, min(TAPE_BOT, y2))
        if y1c < y2c:
            pygame.draw.rect(surf, col, (bar_x, y1c, bar_w, y2c - y1c))

    # White arc: Vs0 – Vfe  (flap range)
    _band(vs0, vfe, WHITE, SPD_X + SPD_W - 10, 3)
    # Green arc: Vs1 – Vno  (normal ops)
    _band(vs1, vno, GREEN_ARC, SPD_X + SPD_W - 5, 4)
    # Yellow arc: Vno – Vne (caution)
    _band(vno, vne, YELLOW_ARC, SPD_X + SPD_W - 5, 4)
    # Red Vne line
    vne_y = sy(vne)
    if TAPE_TOP < vne_y < TAPE_BOT:
        pygame.draw.line(surf, RED, (SPD_X + SPD_W - 16, vne_y),
                         (SPD_X + SPD_W, vne_y), 3)

    # Tick marks and numbers
    base = int(round(speed / 20)) * 20
    for v in range(base - 100, base + 100, 10):
        if v < 0: continue
        vy = sy(v)
        if not (TAPE_TOP + 15 < vy < TAPE_BOT - 15):
            continue
        major = (v % 20 == 0)
        tl = 12 if major else 7
        pygame.draw.line(surf, LTGREY,
                         (SPD_X, vy), (SPD_X + tl, vy),
                         2 if major else 1)
        if major:
            _text(surf, str(v), 17, (230, 230, 230), bold=True,
                  x=SPD_X + tl + 2, y=vy - 9)

    # GS/IAS bug — color reflects source: magenta=GPS groundspeed, cyan=IAS sensor.
    if gs_bug is not None:
        gby = max(TAPE_TOP, min(TAPE_BOT, spd_y(gs_bug, speed)))
        gb = [(SPD_X,      gby - 17),
              (SPD_X + 14, gby - 17), (SPD_X + 14, gby - 5), (SPD_X + 7, gby),
              (SPD_X + 14, gby + 5),  (SPD_X + 14, gby + 17), (SPD_X, gby + 17)]
        spd_bug_col = MAGENTA if airspeed_src == "gps" else CYAN
        surf.set_clip((0, TAPE_TOP, DISPLAY_W, TAPE_BOT - TAPE_TOP))
        pygame.draw.polygon(surf, spd_bug_col, gb)
        surf.set_clip(None)

    # Speed readout box — stepped Veeder-Root style.
    # Scale widths with font so digits fit on 1024×600 (FONT_SCALE≈1.25).
    _sp = SPD_X
    _fs = getattr(sys.modules[__name__], '_font_scale', 1.0)
    _ptr_r  = int(18 * _fs)
    _inn_w  = int(35 * _fs)
    _drm_sw = int(18 * _fs)
    _inn_r  = _ptr_r + _inn_w
    _box_r  = _inn_r + _drm_sw
    _half_in = int(16 * _fs)
    _half_out = int(30 * _fs)
    pts_s = _chamfer([(_sp,          TAPE_MID),
                      (_sp + _ptr_r, TAPE_MID - _half_in), (_sp + _inn_r, TAPE_MID - _half_in),
                      (_sp + _inn_r, TAPE_MID - _half_out), (_sp + _box_r, TAPE_MID - _half_out),
                      (_sp + _box_r, TAPE_MID + _half_out),
                      (_sp + _inn_r, TAPE_MID + _half_out), (_sp + _inn_r, TAPE_MID + _half_in),
                      (_sp + _ptr_r, TAPE_MID + _half_in)], {2, 3, 4, 5, 6, 7}, r=3)
    _filled_polygon(surf, pts_s, (0, 10, 30))
    spd_col = RED if speed > vne else (YELLOW if speed > vno else WHITE)
    _rolling_drum(surf, _sp + _ptr_r + 1, TAPE_MID - _half_in + 1, _inn_w - 2, _half_in * 2 - 2, speed, 2, spd_col, 24,
                  power_offset=1, suppress_leading=True)
    _rolling_drum(surf, _sp + _inn_r + 1, TAPE_MID - _half_out + 1, _drm_sw - 2, _half_out * 2 - 2, speed, 1, spd_col, 24,
                  show_adjacent=True, adj_slot_h=int(23 * _fs))
    _drum_shade(surf, _sp + _inn_r + 1, TAPE_MID - _half_out + 1, _drm_sw - 2, _half_out * 2 - 2)
    # Border drawn LAST so drum shade doesn't cover the inner pixels
    _aa_polygon_outline(surf, pts_s, WHITE, width=4)

    # GS bug button — top strip of speed tape; color matches bug triangle
    gs_str = f"{round(gs_bug):3d}" if gs_bug is not None else "---"
    spd_box_col = MAGENTA if airspeed_src == "gps" else CYAN
    _cyan_box(surf, gs_str, x=SPD_X, y=0, w=SPD_W, h=TAPE_TOP, col=spd_box_col)


# ── Altitude tape ──────────────────────────────────────────────────────────────
def draw_alt_tape(surf, alt, vspeed, baro_hpa, baro_src, alt_bug=None, baro_ok=True):
    """Right altitude tape with VSI and baro setting."""
    global _alt_tape_bg
    if _alt_tape_bg is None:
        _alt_tape_bg = pygame.Surface((ALT_W, TAPE_BOT), pygame.SRCALPHA)
        _alt_tape_bg.fill(TAPE_BG)
    surf.blit(_alt_tape_bg, (ALT_X, 0))
    pygame.draw.line(surf, (255, 255, 255, 60), (ALT_X, 0),
                     (ALT_X, TAPE_BOT), 1)

    def ay2(ft): return alt_y(ft, alt)

    # Tick marks and numbers — every 50ft minor, every 100ft major with label
    base = int(round(alt / 50)) * 50
    for ft in range(base - 450, base + 450, 50):
        fy = ay2(ft)
        if not (TAPE_TOP + 8 < fy < TAPE_BOT - 8):
            continue
        major = (ft % 100 == 0)
        tl = 12 if major else 7
        pygame.draw.line(surf, LTGREY,
                         (ALT_X + ALT_W - tl, fy), (ALT_X + ALT_W, fy),
                         2 if major else 1)
        if major:
            s = str(ft)
            if ft >= 1000:
                # Thousands digit slightly larger than hundreds+
                f_l = _get_font(16, bold=True)
                f_s = _get_font(13, bold=True)
                thou, rest = s[:1], s[1:]
                tw_l = f_l.size(thou)[0]
                tw_s = f_s.size(rest)[0]
                x0 = ALT_X + ALT_W - tl - 2 - tw_l - tw_s
                _text(surf, thou, 16, (230, 230, 230), bold=True, x=x0,        y=fy - 10)
                _text(surf, rest, 13, (230, 230, 230), bold=True, x=x0 + tw_l, y=fy -  8)
            else:
                lw = _get_font(13, bold=True).size(s)[0]
                _text(surf, s, 13, (230, 230, 230), bold=True,
                      x=ALT_X + ALT_W - tl - 2 - lw, y=fy - 8)

    # ALT bug button — top strip of alt tape; color matches bug triangle
    alt_str = f"{round(alt_bug):5d}" if alt_bug is not None else "-----"
    alt_box_col = CYAN if baro_ok else MAGENTA
    _cyan_box(surf, alt_str, x=ALT_X + 1, y=0, w=ALT_W - 1, h=TAPE_TOP, col=alt_box_col)

    # VS bar — 5px wide on the outer (right) edge of the alt tape.
    # Visible whenever climbing/descending; covered by alt bug only when at bug altitude.
    # 2000 fpm ≡ 200 ft on the tape scale.
    _vs_scale = 200 * PX_PER_FT / 2000   # px per fpm
    _vs_px    = int(abs(vspeed) * _vs_scale)
    if abs(vspeed) > 30 and _vs_px > 0:
        if vspeed > 0:
            _vsy1 = max(TAPE_TOP, TAPE_MID - _vs_px)
            _vsy2 = TAPE_MID
        else:
            _vsy1 = TAPE_MID
            _vsy2 = min(TAPE_BOT, TAPE_MID + _vs_px)
        pygame.draw.rect(surf, MAGENTA, (ALT_X + ALT_W - 5, _vsy1, 5, _vsy2 - _vsy1))

    # Altitude bug — color reflects source: cyan=baro/pressure transducer, magenta=GPS alt (baro failed).
    if alt_bug is not None:
        aby = max(TAPE_TOP, min(TAPE_BOT, ay2(alt_bug)))
        bug = [(ALT_X + ALT_W,      aby - 17),
               (ALT_X + ALT_W - 14, aby - 17), (ALT_X + ALT_W - 14, aby - 5), (ALT_X + ALT_W - 7, aby),
               (ALT_X + ALT_W - 14, aby + 5),  (ALT_X + ALT_W - 14, aby + 17), (ALT_X + ALT_W, aby + 17)]
        alt_bug_col = CYAN if baro_ok else MAGENTA
        surf.set_clip((0, TAPE_TOP, DISPLAY_W, TAPE_BOT - TAPE_TOP))
        pygame.draw.polygon(surf, alt_bug_col, bug)
        surf.set_clip(None)

    # Altitude readout box — stepped Veeder-Root style, scaled with FONT_SCALE.
    _fs = getattr(sys.modules[__name__], '_font_scale', 1.0)
    R = ALT_X + ALT_W
    _ptr_w  = int(18 * _fs)
    _drm_w  = int(31 * _fs)
    _inn_w  = int(47 * _fs)
    _box_w  = _inn_w + _drm_w + _ptr_w
    _drm_l  = _ptr_w + _drm_w
    _half_in = int(16 * _fs)
    _half_out = int(30 * _fs)
    pts_a = _chamfer([(R,              TAPE_MID),
                      (R - _ptr_w,     TAPE_MID - _half_in), (R - _ptr_w, TAPE_MID - _half_out),
                      (R - _drm_l,     TAPE_MID - _half_out), (R - _drm_l, TAPE_MID - _half_in),
                      (R - _box_w,     TAPE_MID - _half_in),
                      (R - _box_w,     TAPE_MID + _half_in),
                      (R - _drm_l,     TAPE_MID + _half_in), (R - _drm_l, TAPE_MID + _half_out),
                      (R - _ptr_w,     TAPE_MID + _half_out), (R - _ptr_w, TAPE_MID + _half_in)], {2, 3, 4, 5, 6, 7, 8, 9}, r=3)
    _filled_polygon(surf, pts_a, (0, 10, 30))

    # VSI readout — extends left beyond the tape if the text needs room
    _ny   = TAPE_MID + _half_in
    _nh   = int(22 * _fs)
    if abs(vspeed) > 30:
        _varr = "▲" if vspeed > 0 else "▼"
        _vstr = f"{_varr}{abs(vspeed)/1000:.1f}"
        _vcol = (0, 220, 0) if vspeed > 0 else (255, 140, 0)
    else:
        _vstr = "—"
        _vcol = LTGREY
    _vf = _get_font(13, bold=True)
    _vtw = _vf.size(_vstr)[0] + 12
    _vsi_min_w = R - _drm_l - ALT_X
    _nw = max(_vsi_min_w, _vtw)
    _nx = R - _drm_l - _nw
    pygame.draw.rect(surf, (0, 8, 22), (_nx, _ny, _nw, _nh), border_radius=3)
    pygame.draw.rect(surf, (70, 100, 130), (_nx, _ny, _nw, _nh), width=1, border_radius=3)
    _text(surf, _vstr, 13, _vcol, bold=True, cx=_nx + _nw // 2, cy=_ny + _nh // 2)

    # Inner: cascade from drum; carry starts when drum_pos > 4 (last 20 ft before rollover)
    carry_frac = max(0.0, (alt % 100) / 20 - 4.0)
    alt_inner  = float(alt // 100) + carry_frac
    # round() (not int()) so IIR-smoothed alt of e.g. 9999.99 still
    # picks the 3-drum branch that renders the leading "1" at 10000.
    inner_int  = round(alt_inner)
    _cell = int(16 * _fs)
    _inn_right = R - _drm_l
    _inn_h = _half_in * 2 - 2
    if inner_int < 10:
        _rolling_drum(surf, _inn_right - _cell, TAPE_MID - _half_in + 1, _cell, _inn_h, alt_inner, 1, WHITE, 24)
    elif inner_int < 100:
        _rolling_drum(surf, _inn_right - _cell * 2, TAPE_MID - _half_in + 1, _cell, _inn_h, alt_inner, 1, WHITE, 24,
                      power_offset=1)
        _rolling_drum(surf, _inn_right - _cell, TAPE_MID - _half_in + 1, _cell, _inn_h, alt_inner, 1, WHITE, 22)
    else:
        _rolling_drum(surf, _inn_right - _cell * 3, TAPE_MID - _half_in + 1, _cell * 2, _inn_h, alt_inner, 2, WHITE, 22,
                      suppress_leading=True, power_offset=1)
        _rolling_drum(surf, _inn_right - _cell, TAPE_MID - _half_in + 1, _cell, _inn_h, alt_inner, 1, WHITE, 22)
    _drm_x = R - _drm_l + 1
    _drm_render_w = _drm_w - 2
    _drm_h = _half_out * 2 - 2
    _rolling_drum_alt20(surf, _drm_x, TAPE_MID - _half_out + 1, _drm_render_w, _drm_h, alt, WHITE, 18,
                        show_adjacent=True, adj_slot_h=int(18 * _fs))
    _drum_shade(surf, _drm_x, TAPE_MID - _half_out + 1, _drm_render_w, _drm_h)
    # Border drawn LAST so drum shade doesn't cover the inner pixels
    _aa_polygon_outline(surf, pts_a, WHITE, width=4)


# ── Heading tape ──────────────────────────────────────────────────────────────
_CARDINALS = {0: "N", 45: "NE", 90: "E", 135: "SE",
              180: "S", 225: "SW", 270: "W", 315: "NW"}


def draw_heading_tape(surf, hdg, hdg_bug=None, track=None, yaw=None,
                      gps_ok=False, ahrs_ok=True, use_track=False,
                      hdg_label="M", hdg_color=WHITE):
    """Bottom heading strip with bug, current-heading box, and a
    cross-source diamond showing the *other* heading source's value.

    Args:
        hdg:        the heading value to display in the box (track or yaw)
        track:      raw GPS track (deg)        — used for cross-source pointer
        yaw:        raw AHRS magnetic yaw (deg) — used for cross-source pointer
        use_track:  True if the active source is GPS track
        hdg_label:  subscript "M" / "G" / "M?" / "G?" / "?"
        hdg_color:  WHITE for valid mag, MAGENTA for valid track, AMBER for ?
    """
    hdg_surf = pygame.Surface((DISPLAY_W, HDG_H), pygame.SRCALPHA)
    hdg_surf.fill((0, 8, 22, 210))
    surf.blit(hdg_surf, (0, HDG_Y))
    pygame.draw.line(surf, (255, 255, 255, 80), (0, HDG_Y), (DISPLAY_W, HDG_Y), 1)

    # Tick marks
    for i in range(-70, 71):
        deg = int((round(hdg) + i + 3600)) % 360
        off = i - (hdg - round(hdg))
        x = int(CX + off * PX_PER_DEG)
        if not (0 < x < DISPLAY_W):
            continue
        if deg % 5 == 0:
            th = int(HDG_H * (0.35 if deg % 10 == 0 else 0.18))
            pygame.draw.line(surf, (200, 200, 200),
                             (x, HDG_Y), (x, HDG_Y + th),
                             2 if deg % 10 == 0 else 1)
        if deg % 10 == 0:
            lbl = _CARDINALS.get(deg, str(deg // 10))
            col = YELLOW if deg in _CARDINALS else (230, 230, 230)
            _text(surf, lbl, 17, col, bold=True, cx=x, cy=HDG_Y + HDG_H * 3 // 4)

    # Heading bug chevron — color matches the active source
    if hdg_bug is not None:
        off = ((hdg_bug - hdg + 180) % 360) - 180
        hbx = int(CX + off * PX_PER_DEG)
        hbx = max(SPD_W, min(ALT_X, hbx))   # clamp to inner edges of tap buttons
        bug = [(hbx - 17, HDG_Y + 14), (hbx - 17, HDG_Y),
               (hbx - 5,  HDG_Y), (hbx, HDG_Y + 7), (hbx + 5, HDG_Y),
               (hbx + 17, HDG_Y), (hbx + 17, HDG_Y + 14)]
        hdg_bug_col = MAGENTA if use_track else CYAN
        _filled_polygon(surf, bug, hdg_bug_col)
        pygame.gfxdraw.aapolygon(surf, bug, hdg_bug_col)

    # Cross-source pointer — when the OTHER source is also valid, mark its
    # value on the tape so the pilot can see the discrepancy (wind correction
    # angle, mag variation, compass drift) without having to flip modes.
    # Cyan triangle = magnetic; magenta triangle = GPS track.
    cross_val = None
    cross_col = None
    if use_track and ahrs_ok and yaw is not None:
        cross_val = yaw
        cross_col = CYAN
    elif (not use_track) and gps_ok and track is not None:
        cross_val = track
        cross_col = MAGENTA
    if cross_val is not None:
        off = ((cross_val - hdg + 180) % 360) - 180
        if abs(off) > 1.0:   # avoid clutter when sources agree
            tx = int(CX + off * PX_PER_DEG)
            if 0 < tx < DISPLAY_W:
                tri = [(tx, HDG_Y + 2),
                       (tx - 8, HDG_Y + 18), (tx + 8, HDG_Y + 18)]
                _filled_polygon(surf, tri, cross_col)
                pygame.draw.polygon(surf, WHITE, tri, 1)

    # Heading box — colour reflects active source.
    hdg_col = hdg_color
    # Measure actual rendered width of "133°" to size the box
    _hf = _get_font(17)
    _hdg_str = f"{round(hdg) % 360:03d}\u00b0"
    _hw = _hf.size(_hdg_str)[0]
    # Subscript can be 1 char ("M"/"G") or 2 chars ("M?"/"G?"); pad accordingly.
    sub_pad = 22 if len(hdg_label) > 1 else 14
    bw = max(66, _hw + sub_pad + 14)
    bh = max(28, _hf.get_height() + 8)
    bx, by2 = CX - bw // 2, HDG_Y - bh - 2
    th = bw // 3
    td = 14
    tx = CX - th // 2
    pts_h = _chamfer([(bx,      by2),
                      (bx + bw, by2),
                      (bx + bw, by2 + bh),
                      (tx + th, by2 + bh),
                      (CX,      by2 + bh + td),
                      (tx,      by2 + bh),
                      (bx,      by2 + bh)], {0, 1, 2, 6}, r=3)
    _filled_polygon(surf, pts_h, (0, 0, 0))
    # Same 2× supersample AA outline used on the speed / altitude boxes —
    # keeps the heading box's pointer diagonals visually consistent with
    # its siblings now that the Veeder-Root edges are smooth.
    _aa_polygon_outline(surf, pts_h, hdg_col, width=4)
    # Three-digit readout — centred in the box
    _text(surf, _hdg_str, 17, hdg_col, cx=CX, cy=by2 + bh // 2)
    # Source subscript ("M" / "G" / "M?" / "G?" / "?") — outboard of ° glyph
    deg_right = CX + _hw // 2 + 3
    _text(surf, hdg_label, 8, hdg_col, x=deg_right, y=by2 + bh - 12)


# ── Terrain / obstacle proximity alert ───────────────────────────────────────
# alert_level: 0 = none, 1 = caution (amber), 2 = warning (red flash)
_terrain_alert_level = 0


def _alert_radius_nm(speed_kt: float) -> float:
    """
    Compute obstacle alert radius from current airspeed.
    radius = speed × ALERT_TIME_S, clamped to [MIN, MAX].
    Gives a constant time-to-obstacle regardless of airspeed.
    """
    dyn = speed_kt * ALERT_TIME_S / 3600.0
    return max(ALERT_RADIUS_MIN_NM, min(ALERT_RADIUS_MAX_NM, dyn))


def _approach_corridor_inhibit(lat, lon, alt_ft):
    """Return True when the aircraft is inside the published-approach
    corridor and TAWS alerts should be silenced — same convention as
    Honeywell MK V/VII and Garmin G3X.  Mode 2 / Mode 7 alerts
    suppress when the aircraft is on a stabilised approach to a known
    runway: within ~5 NM of the threshold, aligned within roughly the
    CDI full-scale (±0.3 NM) of the centreline, and on a sensible
    altitude band relative to the threshold.

    The pilot deliberately descends into terrain proximity on every
    final — keeping TAWS armed there spams the cockpit during normal
    landings.  This gate only fires when an approach is loaded
    (disp["approach"]["active"]), so en-route deviations into terrain
    still trip alerts as normal.
    """
    ap = disp.get("approach") or {}
    if not ap.get("active"):
        return False
    thr_lat  = ap.get("thresh_lat")
    thr_lon  = ap.get("thresh_lon")
    thr_elev = ap.get("thresh_elev_ft")
    course   = ap.get("course_deg")
    if thr_lat is None or thr_lon is None or thr_elev is None or course is None:
        return False

    cos_lat = max(0.05, math.cos(math.radians(thr_lat)))
    # Vector from threshold to aircraft, in NM East/North.
    n_nm = (lat - thr_lat) * 60.0
    e_nm = (lon - thr_lon) * 60.0 * cos_lat
    # Rotate into runway-aligned frame: +y = along reciprocal course
    # (back toward the aircraft on final), +x = perpendicular right.
    back_rad = math.radians((course + 180.0) % 360.0)
    cos_b, sin_b = math.cos(back_rad), math.sin(back_rad)
    along_nm =  n_nm * cos_b + e_nm * sin_b   # +ve = along final, -ve = past threshold
    cross_nm = -n_nm * sin_b + e_nm * cos_b   # +ve = right of course

    if along_nm < -0.5:        # past the threshold (rollout)
        return False
    if along_nm > 5.0:          # too far out to be on final
        return False
    if abs(cross_nm) > 0.3:    # outside the ±0.3 NM CDI full-scale
        return False
    # Altitude band: threshold elevation to threshold + 2500 ft.
    # "Way too high" deviations come back out of inhibit so a botched
    # missed approach into rising terrain still trips the alert.
    if alt_ft < thr_elev - 100.0 or alt_ft > thr_elev + 2500.0:
        return False
    return True


# Manual TERRAIN INHIBIT — pilot-controlled mute on the TAWS pipeline.
# Tap the button on AHRS / SENSORS to silence terrain + obstacle +
# pull-up callouts for _INHIBIT_DURATION_S; sink-rate stays armed
# since "excessive descent rate" is the one alert that still matters
# on a stabilised approach.  Inhibit auto-clears on timeout so a
# forgotten toggle doesn't permanently mute the safety net.
_INHIBIT_DURATION_S = 120.0
_terrain_inhibit_until_ms = 0

# True for the duration of one render frame whenever the
# approach-corridor auto-inhibit (REQ-DISP-PI4-TAWS-010) is gating the
# alert pipeline.  Set in _update_terrain_alert; read by the status
# badges so the cockpit shows the safety net is off.  No timer — the
# auto-inhibit follows the aircraft position frame-to-frame.
_approach_inhibit_active = False


def is_terrain_inhibited():
    """True while the pilot's manual TERRAIN INHIBIT is in effect."""
    return pygame.time.get_ticks() < _terrain_inhibit_until_ms


def is_approach_inhibit_active():
    """True while the synthetic-approach corridor is gating TAWS
    callouts (set fresh on every _update_terrain_alert tick)."""
    return _approach_inhibit_active


def inhibit_remaining_s():
    """Seconds left on the manual TERRAIN INHIBIT (0 when not active)."""
    rem_ms = _terrain_inhibit_until_ms - pygame.time.get_ticks()
    return max(0.0, rem_ms / 1000.0)


def toggle_terrain_inhibit():
    """Tap-handler for the TERRAIN INHIBIT button.  Arms a fresh
    _INHIBIT_DURATION_S window if currently inactive; clears the
    inhibit immediately if active (so a second tap cancels)."""
    global _terrain_inhibit_until_ms
    if is_terrain_inhibited():
        _terrain_inhibit_until_ms = 0
    else:
        _terrain_inhibit_until_ms = (pygame.time.get_ticks()
                                     + int(_INHIBIT_DURATION_S * 1000))


def _update_terrain_alert(lat, lon, alt_ft, speed_kt, gps_ok,
                          track_deg=0.0, vsi_fpm=0.0, vso_kt=VS0):
    """
    Compute the current terrain/obstacle alert level and store it globally.
    Called once per render frame with current aircraft position and airspeed.
      0 — no alert
      1 — CAUTION  (clearance < TERRAIN_CAUTION_FT or obstacle < OBSTACLE_CAUTION_FT)
      2 — WARNING  (clearance < TERRAIN_WARNING_FT or obstacle < OBSTACLE_WARNING_FT)
    vso_kt is the user-set stall speed (flaps down) from the flight profile;
    alerts are inhibited below this groundspeed to silence taxi/rollout nuisance.

    Terrain check is a forward look-ahead along the GPS ground track with
    altitude projected by current VSI — mirrors how EGPWS / TAWS-B fire
    on a mountain *ahead* of the aircraft rather than waiting until it's
    directly under (or in) it.

    Two additional inhibit gates layer on top of the Vso check:
      • approach-corridor auto-inhibit (Mode 7 EGPWS convention)
      • pilot-controlled manual TERRAIN INHIBIT (120 s timer)
    Both silence terrain + obstacle + pull-up callouts.  Sink-rate
    stays armed in both cases — the relevant alert during a real
    descent is "you're going down too fast", which the pilot still
    wants to hear.
    """
    global _terrain_alert_level, _approach_inhibit_active
    # Clear the auto-inhibit flag every frame BEFORE any early returns
    # so the badge doesn't stick on a stale True when GPS drops out or
    # the aircraft taxis below Vso.
    _approach_inhibit_active = False
    if not gps_ok:
        _terrain_alert_level = 0
        return

    # Inhibit terrain/obstacle alerts below Vso (taxi, rollout, etc.)
    if speed_kt < vso_kt:
        _terrain_alert_level = 0
        return

    # Approach-corridor auto-inhibit + manual TERRAIN INHIBIT.  Both
    # mute the alert pipeline without touching the sink-rate path
    # (which fires its own audio callout below regardless).  The
    # auto-corridor state is published to a module-level flag so the
    # status-badge code can surface the `TER INH APR` cue without
    # re-computing the corridor geometry.
    _approach_inhibit_active = _approach_corridor_inhibit(lat, lon, alt_ft)
    if _approach_inhibit_active or is_terrain_inhibited():
        _terrain_alert_level = 0
        # Still run the sink-rate check below — it's the one cue that
        # remains relevant on a stabilised approach.  Skip the rest
        # of the look-ahead / obstacle work since the result is muted.
        agl_ft = None
        if _has_terrain:
            elev_under = get_elevation_ft(SRTM_DIR, lat, lon)
            agl_ft = alt_ft - elev_under
        if agl_ft is not None and 0 < agl_ft < 2500.0 and vsi_fpm < 0:
            sink_threshold_fpm = 1500.0 + agl_ft * 1.4
            if -vsi_fpm > sink_threshold_fpm:
                audio_alerts.play("sink_rate")
        return

    level = 0
    # Track each source separately so audio can distinguish them — TAWS
    # convention is "TERRAIN" vs "OBSTACLE" at caution, both rolling up
    # to "PULL UP" at warning.
    terrain_level  = 0
    obstacle_level = 0

    # ── Terrain clearance (look-ahead along ground track) ────────────────────
    # 45 s × current ground speed × current VSI gives a projection cone
    # roughly matching standard TAWS-B caution-band lookahead. Sampling at
    # 12 points along the path catches isolated peaks while keeping the
    # per-frame cost in microseconds (cache-hot SRTM tiles).
    agl_ft = None
    if _has_terrain:
        TERRAIN_LOOKAHEAD_S = 45.0
        SAMPLES = 12
        dist_nm = speed_kt * TERRAIN_LOOKAHEAD_S / 3600.0
        track_rad = math.radians(track_deg)
        cos_lat = max(0.05, math.cos(math.radians(lat)))
        # Pre-compute per-step delta in (lat, lon) so the per-sample work
        # is just two adds + one elevation lookup.
        step_lat = (dist_nm / SAMPLES) * math.cos(track_rad) / 60.0
        step_lon = (dist_nm / SAMPLES) * math.sin(track_rad) / (60.0 * cos_lat)
        step_t_s = TERRAIN_LOOKAHEAD_S / SAMPLES
        # Walk from current position (i=0) out to full lookahead (i=SAMPLES).
        # Track worst (minimum) clearance — that's what trips the alert.
        elev_under = get_elevation_ft(SRTM_DIR, lat, lon)
        agl_ft = alt_ft - elev_under
        worst_clearance = agl_ft
        s_lat, s_lon = lat, lon
        for i in range(1, SAMPLES + 1):
            s_lat += step_lat
            s_lon += step_lon
            elev = get_elevation_ft(SRTM_DIR, s_lat, s_lon)
            pred_alt = alt_ft + (vsi_fpm * step_t_s * i / 60.0)
            clearance = pred_alt - elev
            if clearance < worst_clearance:
                worst_clearance = clearance
        if worst_clearance < TERRAIN_WARNING_FT:
            terrain_level = 2
        elif worst_clearance < TERRAIN_CAUTION_FT:
            terrain_level = 1

    # ── Sink rate (GPWS Mode 1: excessive descent rate scaled by AGL) ───────
    # Threshold curve: 1500 fpm at the surface, climbing to 5000 fpm at
    # 2500 ft AGL. Above 2500 ft AGL no alert — normal cruise descents
    # routinely hit 1000–2000 fpm and we don't want to nag at altitude.
    sink_rate_active = False
    if agl_ft is not None and 0 < agl_ft < 2500.0 and vsi_fpm < 0:
        sink_threshold_fpm = 1500.0 + agl_ft * 1.4
        if -vsi_fpm > sink_threshold_fpm:
            sink_rate_active = True

    # ── Obstacle clearance (time-based lookahead radius) ─────────────────────
    # Filter the radius-query down to a forward wedge (±OBSTACLE_WEDGE_HALF_DEG
    # off ground track) so towers behind / abeam don't fire spurious alerts.
    if _obstacles is not None:
        radius = _alert_radius_nm(speed_kt)
        nearby = obs_mod.query_nearby(_obstacles, lat, lon,
                                      radius_nm=radius,
                                      alt_ft=alt_ft,
                                      below_ft=OBSTACLE_CAUTION_FT)
        if len(nearby) > 0:
            # Vectorised forward-wedge filter: bearing-to-obstacle vs
            # ground track, wrapped to (-180, +180], kept only if its
            # absolute delta is inside the wedge half-angle.
            cos_lat = max(0.05, math.cos(math.radians(lat)))
            d_north = nearby["lat"] - lat
            d_east  = (nearby["lon"] - lon) * cos_lat
            brg_deg = (np.degrees(np.arctan2(d_east, d_north)) + 360.0) % 360.0
            delta = ((brg_deg - track_deg + 540.0) % 360.0) - 180.0
            in_wedge = np.abs(delta) <= OBSTACLE_WEDGE_HALF_DEG
            ahead = nearby[in_wedge]
            if len(ahead) > 0:
                clearance = alt_ft - ahead["msl_ft"]
                if (clearance < OBSTACLE_WARNING_FT).any():
                    obstacle_level = 2
                elif (clearance < OBSTACLE_CAUTION_FT).any():
                    obstacle_level = 1

    level = max(terrain_level, obstacle_level)
    _terrain_alert_level = level
    # Voice callouts (EGPWS-style, source-identifying at every band):
    #   warning → "Terrain Terrain Pull up Pull up"  or
    #             "Obstacle Obstacle Pull up Pull up"
    #   caution → "Sink rate"  (excessive descent)
    #          or "Terrain Terrain" / "Obstacle Obstacle"
    # Priority order: pull-up warnings first (life-critical), then sink
    # rate (root cause that's eroding clearance), then proximity
    # cautions. Obstacle wins over terrain at the same band — towers /
    # antennas demand a tighter visual scan than a broad terrain band.
    # Rate-limited inside audio_alerts.play() so this is safe per-frame.
    if obstacle_level == 2:
        audio_alerts.play("obstacle_pull_up")
    elif terrain_level == 2:
        audio_alerts.play("terrain_pull_up")
    elif sink_rate_active:
        audio_alerts.play("sink_rate")
    elif obstacle_level == 1:
        audio_alerts.play("obstacle")
    elif terrain_level == 1:
        audio_alerts.play("terrain")


def draw_terrain_alert(surf):
    """
    Draw the TERRAIN / PULL UP alert banner in the centre of the badge strip
    (y = 0..22, same row as the status badges).  Level 2 flashes at 1 Hz.
    """
    level = _terrain_alert_level
    if level == 0:
        return

    # Flash at 1 Hz for WARNING (level 2): on for 500 ms, off for 500 ms
    if level == 2:
        if (pygame.time.get_ticks() // 500) % 2 == 1:
            return  # off phase — nothing drawn

    # Banner dimensions — centred in the AI strip, above pitch ladder
    bw = 140; bh = 16
    bx = CX - bw // 2
    by = 3

    if level == 2:
        bg  = (180, 0, 0)
        fg  = (255, 255, 255)
        lbl = "PULL UP"
        sub = "TERRAIN"
    else:
        bg  = (160, 110, 0)
        fg  = (255, 235, 0)
        lbl = "TERRAIN"
        sub = "CAUTION"

    # Filled rounded rectangle
    pygame.draw.rect(surf, bg, (bx, by, bw, bh), border_radius=3)
    pygame.draw.rect(surf, fg, (bx, by, bw, bh), width=1, border_radius=3)

    # Two-word label: primary left, secondary right.  Nudged up 2 px so
    # the descenders don't kiss the lower edge of the rounded rect.
    _text(surf, lbl, 11, fg, bold=True, x=bx + 6, y=by)
    _text(surf, sub, 9,  fg, bold=False, x=bx + bw - 52, y=by + 2)


# ── Status badges ─────────────────────────────────────────────────────────────
def draw_status_badges(surf, ahrs_ok, gps_ok, gps_comm, baro_ok, baro_src, sats, connected,
                       use_track=False, ahrs_aligning=False):
    """
    Badges are shown only when something requires pilot attention.
    Nominal state = clean strip.  Problem state = badge appears.

    Left  (from AI_X): AHRS FAIL, NO LINK, NO TER, NO OBS, EXP OBS, NO APT, EXP APT
    Right (to ALT_X):  GPS TRK (info), GPS ALT (only when baro absent),
                       GPS Xsat (acquiring), NO GPS (absent)
    """
    f10 = _get_font(10)

    # Badges sit just BELOW the top readout ribbon (TAPE_TOP + 2) so they
    # never overlap it — the ribbon owns the band, the badges sit on the AI.
    _by = TAPE_TOP + 2

    # ── Left badges: problems only ──────────────────────────────────────────
    bx = AI_X + 4
    def badge_l(text, bg, fg=(255, 255, 255)):
        nonlocal bx
        w = f10.size(text)[0] + 10
        pygame.draw.rect(surf, bg, (bx, _by, w, 15))
        _text(surf, text, 10, fg, x=bx + 5, y=_by + 1)
        bx += w + 2

    if not ahrs_ok:
        badge_l("AHRS FAIL", (150, 0, 0))
    elif ahrs_aligning:
        # Amber caution: filter still settling — attitude not yet trustworthy.
        # Mutually exclusive with AHRS FAIL (no data = can't be aligning).
        badge_l("AHRS ALIGN", (140, 95, 0), (255, 200, 70))
    if not connected:
        badge_l("NO LINK", (130, 0, 0))

    # Data-availability — only shown when something is missing/stale
    _AMBER = (130, 90, 0)
    if not _has_terrain:
        badge_l("NO TER", _AMBER, (220, 180, 60))

    od = disp["od"]
    if od.get("records", 0) == 0:
        badge_l("NO OBS", _AMBER, (220, 180, 60))
    elif od.get("expired", False):
        badge_l("EXP OBS", (120, 55, 0), (255, 160, 40))

    ad = disp["ad"]
    if ad.get("records", 0) == 0:
        badge_l("NO APT", _AMBER, (220, 180, 60))
    elif ad.get("expired", False):
        badge_l("EXP APT", (120, 55, 0), (255, 160, 40))

    # TERRAIN INHIBIT — amber badge whenever the TAWS safety net is
    # gated.  Two reasons that can happen, with different labels so
    # the pilot can tell why:
    #   `TER INH Xs`    pilot's manual mute is active (countdown)
    #   `TER INH APR`   approach-corridor auto-inhibit (on stabilised
    #                   final to a loaded synthetic approach)
    # Manual wins the label when both are active — the countdown is
    # the more actionable cue (it tells the pilot when alerts return).
    if is_terrain_inhibited():
        badge_l(f"TER INH {int(inhibit_remaining_s())}s",
                (120, 70, 0), (255, 180, 60))
    elif is_approach_inhibit_active():
        badge_l("TER INH APR", (120, 70, 0), (255, 180, 60))

    # ── Right badges: problems only ─────────────────────────────────────────
    rx = ALT_X - 4
    def badge_r(text, bg, fg=(255, 255, 255)):
        nonlocal rx
        w = f10.size(text)[0] + 10
        rx -= w + 2
        pygame.draw.rect(surf, bg, (rx, _by, w, 15))
        _text(surf, text, 10, fg, x=rx + 5, y=_by + 1)

    # GPS-slaved heading mode indicator — magenta badge (matches track-pointer colour)
    if use_track and gps_ok:
        badge_r("GPS TRK", (70, 0, 70), (220, 80, 220))

    # Show GPS ALT only when baro sensor is absent (pilot needs to know alt source)
    if not baro_ok:
        badge_r("GPS ALT", (80, 80, 0), (220, 220, 100))

    # GPS state:
    #   fix valid          → no badge (clean)
    #   comm but no fix    → amber NO FIX (NMEA flowing, waiting for satellites)
    #   no comm            → red NO SIGNAL (GPS hardware not responding)
    if not gps_ok:
        if gps_comm:
            badge_r("NO FIX", (120, 80, 0), (220, 180, 60))
        else:
            badge_r("NO SIGNAL", (150, 0, 0))


# ── Red-X failure overlays ────────────────────────────────────────────────────
def draw_red_x(surf, x, y, w, h, label, sub="FAIL"):
    """Semi-transparent dark overlay with red X and two-line label.
    Default sub-label is "FAIL"; pass sub="ALIGN" during AHRS settling so
    the same untrustworthy-data signal is used but the pilot understands
    the system is settling rather than broken."""
    ov = pygame.Surface((w, h), pygame.SRCALPHA)
    ov.fill((20, 0, 0, 160))
    surf.blit(ov, (x, y))
    pygame.draw.line(surf, RED, (x + 4, y + 4), (x + w - 4, y + h - 4), 3)
    pygame.draw.line(surf, RED, (x + w - 4, y + 4), (x + 4, y + h - 4), 3)
    if label:
        _text(surf, label, 14, RED, bold=True, cx=x + w // 2, cy=y + h // 2 - 8)
        _text(surf, sub,   14, RED, bold=True, cx=x + w // 2, cy=y + h // 2 + 8)


def draw_failure_overlays(surf, ahrs_ok, gps_ok, gps_comm, baro_ok,
                          ahrs_aligning=False):
    ai_h_used = TAPE_H
    ai_y = TAPE_TOP
    ai_w = ALT_X - SPD_W
    if not ahrs_ok:
        # Cover AI center + heading strip — no valid data at all.
        draw_red_x(surf, SPD_W, ai_y, ai_w, ai_h_used, "ATTITUDE")
        draw_red_x(surf, 0, HDG_Y, DISPLAY_W, HDG_H, "HDG")
    elif ahrs_aligning:
        # Filter still settling — attitude is technically being computed
        # but isn't yet trustworthy. Use the same red-X overlay as FAIL
        # (don't trust this), with "ALIGN" sub-label so the pilot knows
        # the system is settling rather than broken.
        draw_red_x(surf, SPD_W, ai_y, ai_w, ai_h_used, "AHRS",  sub="ALIGN")
        draw_red_x(surf, 0, HDG_Y, DISPLAY_W, HDG_H, "HDG",   sub="ALIGN")
    # Red X on speed tape only when GPS has no signal at all.
    # While communicating but no fix, the amber badge is sufficient warning.
    if not gps_ok and not gps_comm:
        draw_red_x(surf, SPD_X, ai_y, SPD_W, ai_h_used, "AIRSPD")
    if not baro_ok and not gps_ok and not gps_comm:
        draw_red_x(surf, ALT_X, ai_y, ALT_W, ai_h_used, "ALT")


# ── Demo animation ────────────────────────────────────────────────────────────
class DemoState:
    """Sedona AZ flight scenario animation."""
    SCENARIOS = [
        # (label, roll, pitch, hdg, alt, spd, vs, ay, hdg_bug, alt_bug, duration_s)
        ("Level cruise SE – Sedona Valley 8500 ft",
          0,  2, 133, 8500, 115,    0, 0.00, 133, 8500, 8.0),
        ("Climbing left turn – departing NW",
         -18, 4, 218, 7200, 108,  650, -0.08, 250, 9500, 8.0),
        ("Descending final – Rwy 03 KSEZ",
          0, -3,  19, 6200,  90, -500, 0.00,  19, 4900, 8.0),
        ("Short final – gear speed",
          0, -2,  19, 5200,  75, -600, 0.00,  19, 4900, 6.0),
    ]

    def __init__(self):
        self._idx  = 0
        self._t0   = time.monotonic()
        self._apply(0)

    def _apply(self, idx):
        sc = self.SCENARIOS[idx % len(self.SCENARIOS)]
        self.label   = sc[0]
        self._target = dict(zip(
            ("roll", "pitch", "yaw", "alt", "speed", "vspeed", "ay",
             "hdg_bug", "alt_bug", "_dur"),
            sc[1:]))
        self._target["lat"] = DEMO_LAT
        self._target["lon"] = DEMO_LON
        self._target["fix"] = 1
        self._target["sats"] = 8
        self._target["ahrs_ok"] = True
        self._target["gps_ok"]  = True
        self._target["baro_ok"] = False
        self._target["baro_src"] = "gps"

    def tick(self):
        elapsed = time.monotonic() - self._t0
        sc_dur  = self._target.get("_dur", 8.0)
        if elapsed > sc_dur:
            self._idx += 1
            self._t0   = time.monotonic()
            self._apply(self._idx)
        # Apply sensor values to shared state
        with _state_lock:
            for k in ("roll", "pitch", "yaw", "alt", "speed", "vspeed",
                      "ay", "lat", "lon", "fix", "sats",
                      "ahrs_ok", "gps_ok", "baro_ok", "baro_src"):
                if k in self._target:
                    state[k] = self._target[k]
            state["gps_alt"] = state["alt"]
            state["track"]   = state["yaw"]
        # Apply bug positions directly to disp (they're not in state)
        disp["hdg_bug"] = self._target.get("hdg_bug", disp["hdg_bug"])
        disp["alt_bug"] = self._target.get("alt_bug", disp["alt_bug"])


# ── Flight simulator ─────────────────────────────────────────────────────────
class SimFlyState:
    """Interactive flight simulator. Bugs (hdg_bug, alt_bug, spd_bug) are
    autopilot targets; sensor failure flags in disp['sim'] control which
    instruments are serviceable."""

    def __init__(self):
        sim = disp["sim"]
        preset = SIM_PRESETS[sim["preset_idx"]]
        self._last_t = time.monotonic()
        self._vs_filt = 0.0    # low-passed vspeed (smooths per-frame Δalt/dt jitter)
        with _state_lock:
            state["lat"]     = preset[2]
            state["lon"]     = preset[3]
            state["alt"]     = sim["init_alt"]
            state["gps_alt"] = sim["init_alt"]
            state["yaw"]     = sim["init_hdg"]
            state["track"]   = sim["init_hdg"]
            state["speed"]   = sim["init_spd"]
            state["pitch"]   = 0.0
            state["roll"]    = 0.0
            state["vspeed"]  = 0.0
            state["ay"]      = 0.0
            state["fix"]     = 1
            state["sats"]    = 8
            state["ahrs_ok"] = True
            state["gps_ok"]  = True
            state["baro_ok"] = False
            state["baro_src"] = "gps"
            # Faux air-data so the airspeed tape responds when the
            # pilot picks IAS as the speed source in sim: IAS == GS
            # (still air assumption), TAS scaled for ISA altitude.
            state["ias_kt"]     = sim["init_spd"]
            state["tas_kt"]     = sim["init_spd"]
            state["airdata_ok"] = True
        # Seed bugs so the aircraft holds its initial state.  Both bugs
        # start at the same value; whichever is active in the current
        # heading-source mode is what the autopilot follows.
        disp["hdg_bug"] = sim["init_hdg"]
        disp["trk_bug"] = sim["init_hdg"]
        disp["alt_bug"] = sim["init_alt"]
        if disp.get("spd_bug") is None:
            disp["spd_bug"] = sim["init_spd"]
        _ssync_publish_bugs()

    def tick(self):
        now = time.monotonic()
        dt  = min(now - self._last_t, 0.1)
        self._last_t = now

        sim = disp["sim"]
        gps_fail  = sim.get("gps_fail",  False)
        ahrs_fail = sim.get("ahrs_fail", False)
        baro_fail = sim.get("baro_fail", False)

        with _state_lock:
            # ── Targets from bugs ──────────────────────────────────────────────
            # Honour whichever bug the pilot is actually looking at.  TRK mode
            # closes the heading loop on GPS track instead of magnetic yaw —
            # otherwise the AP holds yaw at the bug, wind crabs the track off
            # to one side, and the displayed track never matches what the
            # pilot dialled in.  By driving bank from track error, yaw
            # naturally settles a few degrees into the wind so track lands
            # on the bug.
            _bk = _active_bug_key()
            tgt_hdg = disp.get(_bk)
            if tgt_hdg is None:
                tgt_hdg = state["yaw"]
            tgt_alt = disp["alt_bug"] if disp.get("alt_bug") is not None else state["alt"]
            tgt_spd = disp.get("spd_bug") or 90.0

            # FOLLOW FLIGHT-PLAN mode — overrides bug-based targets.
            # The autopilot flies a 45° intercept to the course (D2 line
            # for plain direct-to, final approach course when an
            # approach is loaded), ramping down to a gentle cross-track
            # correction once within ~0.3 nm of track.  Standard
            # behaviour for any modern avionics — far cleaner than
            # "always turn toward the destination" which over-shoots
            # parallel courses and never settles on track.
            if sim.get("follow_mode") == "fp":
                nv = disp.get("nav") or {}
                if nv.get("ident"):
                    cur_lat = state["lat"]
                    cur_lon = state["lon"]
                    wp_lat  = float(nv["lat"])
                    wp_lon  = float(nv["lon"])
                    dist_nm, brg = _nav_geo_dist_brg(
                        cur_lat, cur_lon, wp_lat, wp_lon)

                    ap = disp.get("approach") or {}
                    appr_cl = _approach_centerline_active()
                    lateral_gain = False     # use tight APPROACH intercept gain
                    if appr_cl:
                        # Approach: course is the published runway
                        # heading (true).  Final is short enough (5–10
                        # nm) that flat-earth XTK at the threshold is
                        # equivalent to the spherical version, and is
                        # what the CDI itself uses on the approach path.
                        course_deg = float(ap["course_deg"])
                        cl_lat = float(ap["thresh_lat"])
                        cl_lon = float(ap["thresh_lon"])
                        cos_lat = max(1e-6, math.cos(math.radians(cl_lat)))
                        de_nm = (cur_lon - cl_lon) * 60.0 * cos_lat
                        dn_nm = (cur_lat - cl_lat) * 60.0
                        course_rad = math.radians(course_deg)
                        xtk = (de_nm * math.cos(course_rad)
                               - dn_nm * math.sin(course_rad))
                    else:
                        # Feeder leg.  On an ACTIVE approach, TRACK the published
                        # leg course (centre on the charted track) with the tight
                        # APPROACH gain, so it's established on course before the
                        # final — not flying loose enroute gain.  Fall back to
                        # direct-to (pure pursuit) for a plain D2 (act≈aircraft)
                        # or when the aircraft is abeam/behind and an intercept
                        # would parallel/diverge from the fix (the old fly-away).
                        ax_lat = float(nv.get("act_lat", cur_lat))
                        ax_lon = float(nv.get("act_lon", cur_lon))
                        appr_active = bool(ap.get("active")
                                           and not ap.get("missed"))
                        leg_d, leg_course = _nav_geo_dist_brg(
                            ax_lat, ax_lon, wp_lat, wp_lon)
                        course_deg, xtk = brg, 0.0          # default: direct
                        if appr_active and leg_d > 0.3:
                            leg_xtk = _nav_xtk_nm(ax_lat, ax_lon,
                                                  wp_lat, wp_lon,
                                                  cur_lat, cur_lon)
                            cand = _sim_intercept_heading(
                                leg_course, leg_xtk, approach=True)
                            if abs(((cand - brg + 180.0) % 360.0)
                                   - 180.0) <= 80.0:        # converges to the fix
                                course_deg, xtk = leg_course, leg_xtk
                                lateral_gain = True

                    # Lateral guidance.  On an approach course (final centreline
                    # or a tracked feeder leg) use a full PID on cross-track so it
                    # captures fast AND holds the centreline:
                    #   P  — proportional to XTK (capture)
                    #   D  — cross-track RATE: lets P be aggressive yet roll out
                    #        onto the centreline without overshoot (the "rate"
                    #        term — anticipates the closure instead of waiting)
                    #   I  — kills the steady-state offset a P/PD law leaves
                    # Otherwise (enroute / plain D2) keep the simple intercept.
                    global _appr_xtk_int
                    if appr_cl or lateral_gain:
                        gs = max(20.0, float(state["speed"]))
                        trk_off = ((float(state["track"]) - course_deg + 180.0)
                                   % 360.0) - 180.0
                        xtk_rate = gs * math.sin(math.radians(trk_off)) / 3600.0
                        corr = -(_APPR_XTK_KP * xtk + _APPR_XTK_KD * xtk_rate)
                        corr = max(-_APPR_MAX_INTERCEPT,
                                   min(_APPR_MAX_INTERCEPT, corr))
                        _appr_xtk_int = max(-_APPR_XTK_INT_LIM,
                                            min(_APPR_XTK_INT_LIM,
                                                _appr_xtk_int + xtk * dt))
                        i_corr = max(-_APPR_XTK_I_AUTH,
                                     min(_APPR_XTK_I_AUTH,
                                         -_APPR_XTK_KI * _appr_xtk_int))
                        tgt_hdg = (course_deg + corr + i_corr) % 360.0
                    else:
                        _appr_xtk_int = 0.0
                        tgt_hdg = _sim_intercept_heading(
                            course_deg, xtk, approach=False)

                    # Vertical guidance — fly the PUBLISHED step-down profile
                    # (the same altitudes the HITS boxes show) whenever the
                    # approach is active, not just on the final centreline, so
                    # it descends segment-by-segment (SEZCY→YEDUV→RW) instead of
                    # diving at the end.  Capture from ABOVE only: descend to the
                    # profile, never climb to chase it.  Falls back to a 3° GS to
                    # the threshold for a synthetic approach (no published alts).
                    if ap.get("active") and not ap.get("missed"):
                        thresh_elev = float(ap["thresh_elev_ft"])
                        prof_alt = _approach_target_alt(cur_lat, cur_lon)
                        if prof_alt is None:
                            prof_alt = thresh_elev + (
                                dist_nm * 6076.12
                                * math.tan(math.radians(3.0)))
                        if state["alt"] >= prof_alt - 20:
                            tgt_alt = max(prof_alt, thresh_elev + 5)
                        else:
                            tgt_alt = state["alt"]

            # ── Heading / bank ─────────────────────────────────────────────────
            # Coordinated-turn model: BANK is the only commanded
            # quantity; yaw rate falls out of the bank via
            # ω = g·tan(φ)/V.  The previous version commanded yaw
            # directly and let bank be a separate cosmetic display,
            # which produced "flat yaw" — the AI airplane swung in
            # heading with wings level — particularly during the last
            # few degrees of a CDI capture where bank was nearly zero
            # but yaw was still being driven.
            #
            # Reference for the heading-hold error: yaw in MAG mode,
            # track in TRK mode.  state["track"] gets refreshed below
            # from the wind solution; first frame falls back to yaw to
            # avoid a transient.
            if _bk == "trk_bug":
                ref_hdg = state.get("track", state["yaw"]) or state["yaw"]
            else:
                ref_hdg = state["yaw"]
            hdg_err = ((tgt_hdg - ref_hdg + 180) % 360) - 180

            # Bank command — proportional, ±25° saturation at ~5°
            # error.  Below saturation, bank rolls out smoothly with
            # heading error so we settle on track without overshoot.
            bank = max(-25.0, min(25.0, hdg_err * 5.0))
            state["roll"] = bank if not ahrs_fail else 0.0
            state["ay"]   = -bank / 600.0   # slip ball

            # Yaw follows from actual bank — coordinated turn.
            v_fps = max(20.0, state["speed"] * 1.6878)
            yaw_rate_dps = math.degrees(
                32.174 * math.tan(math.radians(bank)) / v_fps)
            state["yaw"] = (state["yaw"] + yaw_rate_dps * dt) % 360

            # ── Altitude / VS / pitch ──────────────────────────────────────────
            # On approach, the GS itself is descending at ~530 fpm at
            # 100 kt, so a pure proportional alt-error controller can
            # only catch it asymptotically — sim flies persistently
            # above the GS.  Add the GS descent rate as feedforward and
            # bump the closed-loop gain so the diamond actually centres.
            _ap_now = disp.get("approach") or {}
            _on_appr_descent = (_ap_now.get("active")
                                and tgt_alt < state["alt"] - 5.0)
            alt     = state["alt"]
            alt_err = tgt_alt - alt
            # Compute the new altitude per the control law, then DERIVE vspeed
            # from the actual altitude change.  The old code hard-coded
            # vs_fpm=0 whenever |alt_err|<5 and snapped alt=tgt_alt — but on an
            # approach tgt_alt itself descends along the glidepath, so the
            # aircraft kept losing altitude while VS read 0.  That zero fed the
            # FPV (γ = atan2(VS, GS)) and pinned the flight-path marker to the
            # horizon instead of the runway.  Deriving VS from Δalt makes the
            # snap-to-profile case report the real descent rate; a true level
            # hold (tgt_alt constant) still yields 0.
            if abs(alt_err) < 5.0:
                new_alt = tgt_alt
            elif _on_appr_descent:
                gs_descent_fpm = -(state["speed"] * 6076.12
                                    * math.tan(math.radians(3.0)) / 60.0)
                vs_cmd  = max(-1500.0, min(1500.0,
                                          alt_err * 6.0 + gs_descent_fpm))
                new_alt = alt + vs_cmd / 60.0 * dt
            else:
                vs_cmd  = max(-1500.0, min(1500.0, alt_err * 2.0))
                new_alt = alt + vs_cmd / 60.0 * dt
            raw_vs = (new_alt - alt) / max(dt, 1e-3) * 60.0
            # Δalt reflects the PREVIOUS frame's position advance (gs·dt_prev)
            # but we divide by THIS frame's dt; with jittery frame times that
            # ratio swings, so the raw rate bounces ~200↔300 fpm on a steady
            # glidepath.  dt-aware low-pass (τ≈0.6 s) → smooth VS, and since
            # pitch and the FPV both derive from VS, a smooth descent too.
            self._vs_filt += (raw_vs - self._vs_filt) * (dt / (0.6 + dt))
            vs_fpm = self._vs_filt
            state["alt"]     = new_alt
            state["gps_alt"] = new_alt
            state["vspeed"]  = vs_fpm
            state["pitch"]   = max(-10.0, min(10.0, vs_fpm / 100.0)) if not ahrs_fail else 0.0

            # ── Speed / acceleration ───────────────────────────────────────────
            spd     = state["speed"]
            spd_err = tgt_spd - spd
            d_spd   = max(-2.0 * dt, min(2.0 * dt, spd_err * 0.5))
            state["speed"] = max(0.0, spd + d_spd)
            # Faux IAS/TAS track GS in sim (still air assumption) so the
            # airspeed tape moves when the pilot has IAS as the source.
            state["ias_kt"]     = state["speed"]
            state["tas_kt"]     = state["speed"]
            state["airdata_ok"] = True

            # ── Position ───────────────────────────────────────────────────────
            # Constant simulated wind from 270° (west) at 7 kt — yields a
            # ~3–5° crab angle at typical training-aircraft speeds.  Just
            # enough that the cross-source pointer on the heading tape
            # has something to show (track ≠ heading) without the magenta
            # direct-to course visibly leaning off the nose.
            SIM_WIND_FROM_DEG = 270.0
            SIM_WIND_KT       = 7.0
            wind_rad = math.radians(SIM_WIND_FROM_DEG + 180.0)  # blowing TO
            wind_n_kt = SIM_WIND_KT * math.cos(wind_rad)
            wind_e_kt = SIM_WIND_KT * math.sin(wind_rad)
            tas_kt    = state["speed"]   # treat sim speed as TAS
            hdg_rad   = math.radians(state["yaw"])
            ac_n_kt   = tas_kt * math.cos(hdg_rad)
            ac_e_kt   = tas_kt * math.sin(hdg_rad)
            gnd_n_kt  = ac_n_kt + wind_n_kt
            gnd_e_kt  = ac_e_kt + wind_e_kt
            gs_kt     = math.hypot(gnd_n_kt, gnd_e_kt)
            track_deg = math.degrees(math.atan2(gnd_e_kt, gnd_n_kt)) % 360.0
            # Expose the simulated wind so the WIND strip field populates in sim
            # (meteorological "from" convention, knots).
            state["wind_dir"] = SIM_WIND_FROM_DEG
            state["wind_kt"]  = SIM_WIND_KT

            nm_s           = gs_kt / 3600.0
            track_rad      = math.radians(track_deg)
            nm_per_deg_lat = 60.0
            nm_per_deg_lon = max(1.0, 60.0 * math.cos(math.radians(state["lat"])))
            state["lat"]  += nm_s * dt * math.cos(track_rad) / nm_per_deg_lat
            state["lon"]  += nm_s * dt * math.sin(track_rad) / nm_per_deg_lon
            state["track"] = track_deg

            # ── Sensor failure simulation ──────────────────────────────────────
            state["gps_ok"]  = not gps_fail
            state["fix"]     = 0 if gps_fail else 1
            state["sats"]    = 0 if gps_fail else 8
            state["ahrs_ok"] = not ahrs_fail
            state["baro_ok"] = not baro_fail
            state["baro_src"] = "baro" if (not baro_fail) else "gps"


# ── Sim setup screen ─────────────────────────────────────────────────────────

# Airport grid: 4 cols × 3 rows, 8px gap, y starts at 52 (below header)
_SIM_COLS      = 4
_SIM_ROWS_     = 3        # number of preset rows (underscore to avoid shadowing)
_SIM_BTN_W    = 148
_SIM_BTN_H    = 54
_SIM_GAP      = 8
_SIM_GRID_X0  = (DISPLAY_W - _SIM_COLS * _SIM_BTN_W - (_SIM_COLS - 1) * _SIM_GAP) // 2
_SIM_GRID_Y0  = 52

# Condition boxes row
_SIM_COND_Y   = _SIM_GRID_Y0 + _SIM_ROWS_ * _SIM_BTN_H + (_SIM_ROWS_ - 1) * _SIM_GAP + _SIM_GAP + 8
_SIM_COND_H   = 44
_SIM_COND_W   = (DISPLAY_W - 2 * 12) // 3     # ~205 px each

# Sensor-failure toggles row
_SIM_FAIL_Y   = _SIM_COND_Y + _SIM_COND_H + 8
_SIM_FAIL_H   = 40
_SIM_FAIL_BW  = 70    # ON / FAIL button width each
_SIM_FAIL_GAP = 4     # gap between ON / FAIL pair

# START / CANCEL
_SIM_ACT_Y    = _SIM_FAIL_Y + _SIM_FAIL_H + 10
_SIM_ACT_H    = 54


def _sim_preset_rect(idx):
    """Return (x, y, w, h) for a preset button by index."""
    col = idx % _SIM_COLS
    row = idx // _SIM_COLS
    x = _SIM_GRID_X0 + col * (_SIM_BTN_W + _SIM_GAP)
    y = _SIM_GRID_Y0 + row * (_SIM_BTN_H + _SIM_GAP)
    return x, y, _SIM_BTN_W, _SIM_BTN_H


def _sim_cond_rect(idx):
    """Return (x, y, w, h) for condition box (ALT=0, HDG=1, SPD=2)."""
    mx = 12
    x = mx + idx * (_SIM_COND_W + 4)
    return x, _SIM_COND_Y, _SIM_COND_W, _SIM_COND_H


def _sim_fail_x(col_idx):
    """Left x of the ON/FAIL pair for GPS=0, BARO=1, AHRS=2."""
    total_pair = _SIM_FAIL_BW * 2 + _SIM_FAIL_GAP
    section_w  = DISPLAY_W // 3
    # centre the pair inside its section
    return col_idx * section_w + (section_w - total_pair) // 2


def _sim_fail_btn_pair(surf, col_idx, label, failed, y=None):
    """Draw a sensor ON/FAIL segmented pair at col_idx (0=GPS,1=BARO,2=AHRS)."""
    bx = _sim_fail_x(col_idx)
    by = y if y is not None else _SIM_FAIL_Y
    section_w = DISPLAY_W // 3
    # section label
    _text(surf, label, 11, (120, 140, 165),
          cx=col_idx * section_w + section_w // 2, y=by - 14)
    # ON button
    on_active = not failed
    on_bg = (0, 55, 20) if on_active else (0, 10, 20)
    on_oc = (40, 200, 60) if on_active else (40, 70, 55)
    on_tc = (60, 220, 80) if on_active else (70, 110, 90)
    pygame.draw.rect(surf, on_bg, (bx, by, _SIM_FAIL_BW, _SIM_FAIL_H), border_radius=5)
    pygame.draw.rect(surf, on_oc, (bx, by, _SIM_FAIL_BW, _SIM_FAIL_H), width=2, border_radius=5)
    _text(surf, "ON", 13, on_tc, bold=on_active, cx=bx + _SIM_FAIL_BW // 2, cy=by + _SIM_FAIL_H // 2)
    # FAIL button
    fail_x = bx + _SIM_FAIL_BW + _SIM_FAIL_GAP
    fail_active = failed
    fail_bg = (50, 5, 5) if fail_active else (12, 0, 0)
    fail_oc = (200, 40, 40) if fail_active else (80, 35, 35)
    fail_tc = RED if fail_active else (120, 60, 60)
    pygame.draw.rect(surf, fail_bg, (fail_x, by, _SIM_FAIL_BW, _SIM_FAIL_H), border_radius=5)
    pygame.draw.rect(surf, fail_oc, (fail_x, by, _SIM_FAIL_BW, _SIM_FAIL_H), width=2, border_radius=5)
    _text(surf, "FAIL", 11, fail_tc, bold=fail_active, cx=fail_x + _SIM_FAIL_BW // 2, cy=by + _SIM_FAIL_H // 2)


def draw_sim_setup(surf):
    """Full-screen flight simulator setup screen."""
    _screen_header(surf, "FLIGHT SIMULATOR")
    sim = disp["sim"]
    selected = sim["preset_idx"]

    # ── Airport preset grid ───────────────────────────────────────────────────
    for idx, (icao, city, *_) in enumerate(SIM_PRESETS):
        px, py, pw, ph = _sim_preset_rect(idx)
        active = (idx == selected)
        bg = (0, 35, 55) if active else (0, 12, 32)
        oc = CYAN if active else (50, 70, 100)
        pygame.draw.rect(surf, bg, (px, py, pw, ph), border_radius=6)
        glow_h = ph // 5
        for i in range(glow_h):
            t = 1.0 - i / glow_h
            gc = ((int(t * 20), int(50 + t * 50), int(65 + t * 60)) if active
                  else (int(15 + t * 25), int(20 + t * 40), int(40 + t * 60)))
            pygame.draw.line(surf, gc, (px + 6, py + 1 + i), (px + pw - 6, py + 1 + i))
        pygame.draw.rect(surf, oc, (px, py, pw, ph), width=2 if active else 1, border_radius=6)
        _text(surf, icao, 15, WHITE if active else (180, 195, 210), bold=True,
              cx=px + pw // 2, cy=py + ph // 2 - 8)
        _text(surf, city, 9, (100, 130, 155) if not active else CYAN,
              cx=px + pw // 2, cy=py + ph // 2 + 8)

    # ── Initial conditions row ─────────────────────────────────────────────────
    cond_labels = ["ALT (ft)", "HDG (°)", "SPEED (kt)"]
    cond_keys   = ["init_alt", "init_hdg", "init_spd"]
    cond_vals   = [int(sim["init_alt"]), int(sim["init_hdg"]), int(sim["init_spd"])]

    for i, (lbl, val) in enumerate(zip(cond_labels, cond_vals)):
        cx2, cy2, cw, ch = _sim_cond_rect(i)
        pygame.draw.rect(surf, (0, 18, 38), (cx2, cy2, cw, ch), border_radius=5)
        pygame.draw.rect(surf, CYAN, (cx2, cy2, cw, ch), width=1, border_radius=5)
        _text(surf, lbl, 9, (100, 140, 170), cx=cx2 + cw // 2, y=cy2 + 4)
        _text(surf, str(val), 17, CYAN, bold=True, cx=cx2 + cw // 2, cy=cy2 + ch // 2 + 5)
        _text(surf, "tap to set", 8, (70, 100, 130), cx=cx2 + cw // 2, y=cy2 + ch - 12)

    # ── Sensor failure toggles ────────────────────────────────────────────────
    _sim_fail_btn_pair(surf, 0, "GPS",  sim.get("gps_fail",  False))
    _sim_fail_btn_pair(surf, 1, "BARO", sim.get("baro_fail", False))
    _sim_fail_btn_pair(surf, 2, "AHRS", sim.get("ahrs_fail", False))

    # ── START / CANCEL buttons ────────────────────────────────────────────────
    bx = 12; bw = DISPLAY_W - 24
    half = (bw - 10) // 2
    _action_btn(surf, bx,          _SIM_ACT_Y, half, _SIM_ACT_H, "START SIM", "ok")
    _action_btn(surf, bx + half + 10, _SIM_ACT_Y, half, _SIM_ACT_H, "CANCEL",    "danger")


def sim_setup_hit(x, y):
    """Return action string for the sim setup screen tap, or None."""
    # BACK button
    if _back_hit(x, y):
        return "back"

    # Airport preset grid
    for idx in range(len(SIM_PRESETS)):
        px, py, pw, ph = _sim_preset_rect(idx)
        if px <= x <= px + pw and py <= y <= py + ph:
            return f"preset:{idx}"

    # Initial conditions tappable boxes
    sim = disp["sim"]
    for i, key in enumerate(("init_alt", "init_hdg", "init_spd")):
        cx2, cy2, cw, ch = _sim_cond_rect(i)
        if cx2 <= x <= cx2 + cw and cy2 <= y <= cy2 + ch:
            return f"cond:{key}"

    # Sensor failure toggles
    for col_idx, sensor in enumerate(("gps", "baro", "ahrs")):
        bx = _sim_fail_x(col_idx)
        by = _SIM_FAIL_Y
        if by <= y <= by + _SIM_FAIL_H:
            if bx <= x <= bx + _SIM_FAIL_BW:
                return f"sensor_on:{sensor}"
            fail_x = bx + _SIM_FAIL_BW + _SIM_FAIL_GAP
            if fail_x <= x <= fail_x + _SIM_FAIL_BW:
                return f"sensor_fail:{sensor}"

    # START / CANCEL
    bx_btn = 12; bw_btn = DISPLAY_W - 24
    half = (bw_btn - 10) // 2
    if _SIM_ACT_Y <= y <= _SIM_ACT_Y + _SIM_ACT_H:
        if bx_btn <= x <= bx_btn + half:
            return "start"
        if bx_btn + half + 10 <= x <= bx_btn + half + 10 + half:
            return "cancel"

    return None


# ── Sim watermark / quick-exit button ────────────────────────────────────────
# Sized to read clearly from a normal cockpit viewing distance so pilots
# notice it's tappable.  Tap → opens the SIM CONTROLS overlay (which has
# the EXIT SIM action).  Distinct names from the sim-setup grid above
# (which also defines _SIM_BTN_W/H) — they're different controls and
# would otherwise shadow each other through the global namespace.
_SIM_EXIT_W = 88
_SIM_EXIT_H = 32
_SIM_EXIT_X = CX - _SIM_EXIT_W // 2
# Just under the slip/skid bar at the top of the AI (slip bar sits at
# ROLL_CY - ROLL_R + 24) — keeps the badge out of the approach corridor in the
# centre of the screen.  Tapping it still opens the (lower) sim-controls panel.
_SIM_EXIT_Y = ROLL_CY - ROLL_R + 30


# ── Sim controls overlay ─────────────────────────────────────────────────────

_SIMCTRL_W = 320
_SIMCTRL_H = 372
_SIMCTRL_X = (DISPLAY_W - _SIMCTRL_W) // 2
_SIMCTRL_Y = (DISPLAY_H - _SIMCTRL_H) // 2 - 10

_SIMCTRL_ROW_Y0  = _SIMCTRL_Y + 36   # first sensor row top
_SIMCTRL_ROW_H   = 32
_SIMCTRL_ROW_GAP = 4
_SIMCTRL_BW      = 70     # ON / FAIL button width

_SIMCTRL_FOLLOW_BW   = 110    # FOLLOW BUGS / FLT PLAN button width


def _simctrl_follow_y() -> int:
    return _SIMCTRL_ROW_Y0 + 3 * (_SIMCTRL_ROW_H + _SIMCTRL_ROW_GAP) + 8


def _simctrl_pause_y() -> int:
    return _simctrl_follow_y() + _SIMCTRL_ROW_H + 14


def _simctrl_exit_setup_y() -> int:
    return _simctrl_pause_y() + 44 + 8


def _simctrl_exit_sim_y() -> int:
    return _simctrl_exit_setup_y() + 44 + 8


def draw_sim_controls(surf):
    """Semi-transparent overlay drawn on top of the live PFD."""
    sim = disp["sim"]

    # Background panel
    panel = pygame.Surface((_SIMCTRL_W, _SIMCTRL_H), pygame.SRCALPHA)
    panel.fill((0, 10, 28, 220))
    surf.blit(panel, (_SIMCTRL_X, _SIMCTRL_Y))
    pygame.draw.rect(surf, CYAN, (_SIMCTRL_X, _SIMCTRL_Y, _SIMCTRL_W, _SIMCTRL_H),
                     width=2, border_radius=8)

    # Title
    _text(surf, "SIM CONTROLS", 14, CYAN, bold=True,
          cx=_SIMCTRL_X + _SIMCTRL_W // 2, cy=_SIMCTRL_Y + 16)

    # Sensor rows: GPS / BARO / AHRS
    sensors = [("GPS",  "gps_fail"), ("BARO", "baro_fail"), ("AHRS", "ahrs_fail")]
    for ri, (label, key) in enumerate(sensors):
        row_y = _SIMCTRL_ROW_Y0 + ri * (_SIMCTRL_ROW_H + _SIMCTRL_ROW_GAP)
        failed = sim.get(key, False)

        _text(surf, label, 12, (160, 175, 200), bold=True,
              x=_SIMCTRL_X + 14, cy=row_y + _SIMCTRL_ROW_H // 2)

        on_active = not failed
        on_bg = (0, 50, 20) if on_active else (0, 8, 16)
        on_oc = (40, 190, 60) if on_active else (35, 60, 45)
        on_tc = (60, 220, 80) if on_active else (60, 100, 75)
        ox = _SIMCTRL_X + _SIMCTRL_W - 2 * _SIMCTRL_BW - 8 - 6
        pygame.draw.rect(surf, on_bg, (ox, row_y, _SIMCTRL_BW, _SIMCTRL_ROW_H), border_radius=4)
        pygame.draw.rect(surf, on_oc, (ox, row_y, _SIMCTRL_BW, _SIMCTRL_ROW_H), width=2, border_radius=4)
        _text(surf, "ON", 12, on_tc, bold=on_active,
              cx=ox + _SIMCTRL_BW // 2, cy=row_y + _SIMCTRL_ROW_H // 2)

        fx = ox + _SIMCTRL_BW + 6
        fail_active = failed
        fail_bg = (50, 5, 5) if fail_active else (12, 0, 0)
        fail_oc = (200, 40, 40) if fail_active else (75, 30, 30)
        fail_tc = RED if fail_active else (110, 55, 55)
        pygame.draw.rect(surf, fail_bg, (fx, row_y, _SIMCTRL_BW, _SIMCTRL_ROW_H), border_radius=4)
        pygame.draw.rect(surf, fail_oc, (fx, row_y, _SIMCTRL_BW, _SIMCTRL_ROW_H), width=2, border_radius=4)
        _text(surf, "FAIL", 11, fail_tc, bold=fail_active,
              cx=fx + _SIMCTRL_BW // 2, cy=row_y + _SIMCTRL_ROW_H // 2)

    # FOLLOW row — segmented control selecting the autopilot source.
    fy = _simctrl_follow_y()
    _text(surf, "FOLLOW", 12, (160, 175, 200), bold=True,
          x=_SIMCTRL_X + 14, cy=fy + _SIMCTRL_ROW_H // 2)
    follow = sim.get("follow_mode", "bugs")
    fx_b = _SIMCTRL_X + _SIMCTRL_W - 2 * _SIMCTRL_FOLLOW_BW - 8 - 6
    for i, (val, lbl) in enumerate((("bugs", "BUGS"), ("fp", "FLT PLAN"))):
        bx = fx_b + i * (_SIMCTRL_FOLLOW_BW + 6)
        active = (follow == val)
        bg = (0, 55, 65) if active else (0, 10, 25)
        oc = CYAN       if active else (50, 68, 92)
        tc = CYAN       if active else (130, 148, 168)
        pygame.draw.rect(surf, bg, (bx, fy, _SIMCTRL_FOLLOW_BW, _SIMCTRL_ROW_H), border_radius=4)
        pygame.draw.rect(surf, oc, (bx, fy, _SIMCTRL_FOLLOW_BW, _SIMCTRL_ROW_H), width=2, border_radius=4)
        _text(surf, lbl, 12, tc, bold=active,
              cx=bx + _SIMCTRL_FOLLOW_BW // 2, cy=fy + _SIMCTRL_ROW_H // 2)

    # PAUSE / RESUME — freezes the sim's tick() while keeping the rest of
    # the UI live. Amber when running ("PAUSE → stop"), green when paused
    # ("RESUME → go") so the current state reads at a glance.
    paused = sim.get("paused", False)
    pz_y = _simctrl_pause_y()
    _action_btn(surf, _SIMCTRL_X + 14, pz_y,
                _SIMCTRL_W - 28, 44,
                "RESUME" if paused else "PAUSE",
                "ok"      if paused else "warn")

    # EXIT SETUP — closes the overlay, sim continues running.
    es_y = _simctrl_exit_setup_y()
    _action_btn(surf, _SIMCTRL_X + 14, es_y,
                _SIMCTRL_W - 28, 44, "EXIT SETUP", "normal")

    # EXIT SIM — kills the sim and returns to live AHRS.
    xs_y = _simctrl_exit_sim_y()
    _action_btn(surf, _SIMCTRL_X + 14, xs_y,
                _SIMCTRL_W - 28, 44, "EXIT SIM", "danger")


def sim_controls_hit(x, y):
    """Return action for a tap on the sim_controls overlay, or None."""
    # Outside the panel — ignore (do not propagate to PFD)
    if not (_SIMCTRL_X <= x <= _SIMCTRL_X + _SIMCTRL_W and
            _SIMCTRL_Y <= y <= _SIMCTRL_Y + _SIMCTRL_H):
        return None

    sensors = [("gps", "gps_fail"), ("baro", "baro_fail"), ("ahrs", "ahrs_fail")]
    for ri, (key_short, _key) in enumerate(sensors):
        row_y = _SIMCTRL_ROW_Y0 + ri * (_SIMCTRL_ROW_H + _SIMCTRL_ROW_GAP)
        if not (row_y <= y <= row_y + _SIMCTRL_ROW_H):
            continue
        ox = _SIMCTRL_X + _SIMCTRL_W - 2 * _SIMCTRL_BW - 8 - 6
        fx = ox + _SIMCTRL_BW + 6
        if ox <= x <= ox + _SIMCTRL_BW:
            return f"sensor_on:{key_short}"
        if fx <= x <= fx + _SIMCTRL_BW:
            return f"sensor_fail:{key_short}"

    # FOLLOW row
    fy = _simctrl_follow_y()
    if fy <= y <= fy + _SIMCTRL_ROW_H:
        fx_b = _SIMCTRL_X + _SIMCTRL_W - 2 * _SIMCTRL_FOLLOW_BW - 8 - 6
        for i, val in enumerate(("bugs", "fp")):
            bx = fx_b + i * (_SIMCTRL_FOLLOW_BW + 6)
            if bx <= x <= bx + _SIMCTRL_FOLLOW_BW:
                return f"follow:{val}"

    # PAUSE / RESUME
    pz_y = _simctrl_pause_y()
    if (pz_y <= y <= pz_y + 44 and
            _SIMCTRL_X + 14 <= x <= _SIMCTRL_X + _SIMCTRL_W - 14):
        return "toggle_pause"

    # EXIT SETUP
    es_y = _simctrl_exit_setup_y()
    if (es_y <= y <= es_y + 44 and
            _SIMCTRL_X + 14 <= x <= _SIMCTRL_X + _SIMCTRL_W - 14):
        return "exit_setup"

    # EXIT SIM
    xs_y = _simctrl_exit_sim_y()
    if (xs_y <= y <= xs_y + 44 and
            _SIMCTRL_X + 14 <= x <= _SIMCTRL_X + _SIMCTRL_W - 14):
        return "exit_sim"

    return "noop"   # tapped inside panel but not on a control — consume event


# ── Nav-confirm modal ("Activate Direct to XXXX?") ──────────────────────────
# Standard avionics convention: typing/refreshing a waypoint pops up a
# confirmation, requiring a second ENTER (or tap on ACTIVATE) before the
# active flight plan changes.  Prevents accidental flight-plan edits from
# a stray screen tap.
_NAVCNF_W   = 360
_NAVCNF_H   = 170
_NAVCNF_BTN_H = 48


def _navcnf_geom():
    bx = (DISPLAY_W - _NAVCNF_W) // 2
    by = (DISPLAY_H - _NAVCNF_H) // 2
    btn_y = by + _NAVCNF_H - _NAVCNF_BTN_H - 14
    btn_w = (_NAVCNF_W - 14 - 14 - 12) // 2
    bx_l  = bx + 14
    bx_r  = bx + _NAVCNF_W - 14 - btn_w
    return bx, by, btn_y, btn_w, bx_l, bx_r


def draw_nav_confirm(surf):
    """Centered "Activate Direct to XXXX?" modal."""
    ident = disp.get("nav_confirm_ident", "")
    bx, by, btn_y, btn_w, bx_l, bx_r = _navcnf_geom()

    _draw_veil(surf)
    panel = pygame.Surface((_NAVCNF_W, _NAVCNF_H), pygame.SRCALPHA)
    panel.fill((0, 12, 32, 235))
    surf.blit(panel, (bx, by))
    pygame.draw.rect(surf, CYAN, (bx, by, _NAVCNF_W, _NAVCNF_H),
                     width=2, border_radius=10)

    _text(surf, "DIRECT TO", 13, (160, 200, 230), bold=True,
          cx=bx + _NAVCNF_W // 2, cy=by + 22)
    _text(surf, ident or "—", 32, MAGENTA, bold=True,
          cx=bx + _NAVCNF_W // 2, cy=by + 64)
    _text(surf, "Activate?", 14, (200, 215, 235),
          cx=bx + _NAVCNF_W // 2, cy=by + 96)

    _action_btn(surf, bx_l, btn_y, btn_w, _NAVCNF_BTN_H, "CANCEL",   "danger")
    _action_btn(surf, bx_r, btn_y, btn_w, _NAVCNF_BTN_H, "ACTIVATE", "ok")


def nav_confirm_hit(x, y):
    """Return 'activate' / 'cancel' / 'noop' / None for a tap on the modal."""
    bx, by, btn_y, btn_w, bx_l, bx_r = _navcnf_geom()
    if not (bx <= x <= bx + _NAVCNF_W and by <= y <= by + _NAVCNF_H):
        return None
    if btn_y <= y <= btn_y + _NAVCNF_BTN_H:
        if bx_l <= x <= bx_l + btn_w:
            return "cancel"
        if bx_r <= x <= bx_r + btn_w:
            return "activate"
    return "noop"


def _nav_confirm_apply():
    """Activate the pending direct-to and dismiss the modal.  Any
    active approach is cleared — pilot is explicitly choosing a new
    D2, so the existing HITS corridor no longer applies."""
    ident = disp.get("nav_confirm_ident", "")
    if ident:
        _nav_set_by_ident(ident)
        # Pilot is explicitly choosing a new D2 — drop any loaded/active
        # approach ENTIRELY (legs, threshold, course), not just its flags, so
        # the HITS corridor, sign-posts and CDI label/colour all clear instead
        # of lingering on stale approach data.
        disp["approach"] = {"loaded": False}
    disp["nav_confirm_ident"] = ""
    disp["mode"] = disp.get("nav_confirm_prev", "pfd")


def _nav_confirm_cancel():
    disp["nav_confirm_ident"] = ""
    disp["mode"] = disp.get("nav_confirm_prev", "pfd")


# ── Nav picker (tap the CDI → choose Direct-To or Flight Plan) ────────────────
_NAVPICK_W      = 340
_NAVPICK_BTN_H  = 56


def _navpick_options():
    """The buttons to show, as (action, label, style).  DIRECT-TO + FLIGHT PLAN
    always; a loaded approach adds controls for its current phase."""
    opts = [("d2", "DIRECT-TO  →", "ok"),
            ("fpl", "FLIGHT PLAN  →", "normal")]
    ap = disp.get("approach") or {}
    rwy = ap.get("runway", "")
    phase = _approach_phase()
    if phase == "armed":
        opts.append(("appr_activate", f"ACTIVATE APPR {rwy}", "warn"))
    elif phase == "active":
        opts.append(("appr_missed", f"MISSED APPROACH {rwy}", "warn"))
        opts.append(("appr_cancel", f"CANCEL APPR {rwy}", "danger"))
    elif phase == "missed":
        opts.append(("appr_cancel", f"END MISSED {rwy}", "danger"))
    return opts


def _navpick_layout():
    """(bx, by, h, rects) where rects is [(action, (x,y,w,h)), ...].  Height
    grows with the option count + a loaded-approach reminder line."""
    opts = _navpick_options()
    has_reminder = bool((disp.get("approach") or {}).get("loaded"))
    top = 36
    rem_h = 22 if has_reminder else 0
    gap = 10
    h = top + rem_h + len(opts) * _NAVPICK_BTN_H + (len(opts) - 1) * gap + 30
    bx = (DISPLAY_W - _NAVPICK_W) // 2
    by = (DISPLAY_H - h) // 2
    bxl = bx + 20
    btn_w = _NAVPICK_W - 40
    y0 = by + top + rem_h
    rects = [(opt[0], (bxl, y0 + i * (_NAVPICK_BTN_H + gap), btn_w, _NAVPICK_BTN_H))
             for i, opt in enumerate(opts)]
    return bx, by, h, opts, rects, has_reminder


def draw_nav_pick(surf):
    """Modal shown when the CDI strip is tapped: Direct-To, Flight Plan, and —
    when an approach is loaded — Activate/Cancel it (with a which-approach
    reminder)."""
    bx, by, h, opts, rects, has_reminder = _navpick_layout()
    _draw_veil(surf)
    panel = pygame.Surface((_NAVPICK_W, h), pygame.SRCALPHA)
    panel.fill((0, 12, 32, 235))
    surf.blit(panel, (bx, by))
    pygame.draw.rect(surf, CYAN, (bx, by, _NAVPICK_W, h), width=2, border_radius=10)
    _text(surf, "NAVIGATE", 14, (160, 200, 230), bold=True,
          cx=bx + _NAVPICK_W // 2, cy=by + 20)
    if has_reminder:
        ap = disp.get("approach") or {}
        phase = _approach_phase()
        plabel = {"armed": "ARMED", "active": "ACTIVE",
                  "missed": "MISSED"}.get(phase, "")
        col = {"armed": (225, 185, 80), "active": (60, 220, 100),
               "missed": (240, 140, 60)}.get(phase, (200, 200, 200))
        _text(surf, f"{ap.get('airport','')}  {_approach_label()}  ·  "
                    + plabel, 12, col, bold=True,
              cx=bx + _NAVPICK_W // 2, cy=by + 40)
    for (_act, lbl, style), (_a, rect) in zip(opts, rects):
        _action_btn(surf, *rect, lbl, style)
    _text(surf, "tap outside to cancel", 11, (140, 150, 160),
          cx=bx + _NAVPICK_W // 2, cy=by + h - 12)


def nav_pick_hit(x, y):
    """Return the tapped option's action ('d2'/'fpl'/'appr_activate'/
    'appr_cancel'), 'cancel' (tap-outside), or 'noop'."""
    bx, by, h, _opts, rects, _ = _navpick_layout()
    if not (bx <= x <= bx + _NAVPICK_W and by <= y <= by + h):
        return "cancel"
    for act, (rx, ry, rw, rh) in rects:
        if rx <= x <= rx + rw and ry <= y <= ry + rh:
            return act
    return "noop"


def _nav_pick_open():
    """Tap on the CDI → choose D2 or FPL."""
    disp["mode"] = "nav_pick"


# ── Leg menu — tap a flight-plan waypoint / approach fix to choose what to fly ──
_LEGMENU_W      = 360
_LEGMENU_BTN_H  = 56


def _legmenu_open(kind, idx):
    """kind = 'fpl' | 'appr'; idx into the plan / approach legs."""
    disp["leg_menu"] = {"kind": kind, "idx": int(idx)}
    disp["mode"] = "leg_menu"


def _legmenu_title():
    lm = disp.get("leg_menu") or {}
    idx = lm.get("idx", 0)
    if lm.get("kind") == "appr":
        legs = (disp.get("approach") or {}).get("legs") or []
        return (legs[idx][2] if 0 <= idx < len(legs) else ""), "Approach leg"
    wps = disp.get("fpl", {}).get("waypoints", [])
    return ((wps[idx].get("ident", "") if 0 <= idx < len(wps) else ""),
            "Flight-plan leg")


def _legmenu_options():
    opts = [("activate", "ACTIVATE LEG", "ok"),
            ("d2",       "DIRECT-TO  →", "normal")]
    if (disp.get("leg_menu") or {}).get("kind") == "appr":
        opts.append(("vectors", "VECTORS TO FINAL", "warn"))
    return opts


def _legmenu_layout():
    opts = _legmenu_options()
    gap, top = 10, 56
    h = top + len(opts) * _LEGMENU_BTN_H + (len(opts) - 1) * gap + 28
    bx = (DISPLAY_W - _LEGMENU_W) // 2
    by = (DISPLAY_H - h) // 2
    bxl, btn_w = bx + 20, _LEGMENU_W - 40
    y0 = by + top
    rects = [(opt[0], (bxl, y0 + i * (_LEGMENU_BTN_H + gap), btn_w, _LEGMENU_BTN_H))
             for i, opt in enumerate(opts)]
    return bx, by, h, opts, rects


def draw_leg_menu(surf):
    """Modal over the FPL screen: tap a leg → ACTIVATE / DIRECT-TO / (VECTORS)."""
    draw_fpl(surf)
    bx, by, h, opts, rects = _legmenu_layout()
    _draw_veil(surf)
    panel = pygame.Surface((_LEGMENU_W, h), pygame.SRCALPHA)
    panel.fill((0, 12, 32, 235))
    surf.blit(panel, (bx, by))
    pygame.draw.rect(surf, CYAN, (bx, by, _LEGMENU_W, h), width=2, border_radius=10)
    ident, sub = _legmenu_title()
    _text(surf, ident or "—", 22, WHITE, bold=True, cx=bx + _LEGMENU_W // 2, cy=by + 22)
    _text(surf, sub, 12, (150, 180, 210), cx=bx + _LEGMENU_W // 2, cy=by + 42)
    for (_a, lbl, st), (_a2, rect) in zip(opts, rects):
        _action_btn(surf, *rect, lbl, st)
    _text(surf, "tap outside to cancel", 11, (140, 150, 160),
          cx=bx + _LEGMENU_W // 2, cy=by + h - 12)


def leg_menu_hit(x, y):
    bx, by, h, _opts, rects = _legmenu_layout()
    if not (bx <= x <= bx + _LEGMENU_W and by <= y <= by + h):
        return "cancel"
    for act, (rx, ry, rw, rh) in rects:
        if rx <= x <= rx + rw and ry <= y <= ry + rh:
            return act
    return "noop"


def _nav_open_confirm(ident: str, prev_mode: str) -> bool:
    """Switch to the nav_confirm modal for `ident`.  Returns False if
    the ident is empty (caller should fall through to its no-op
    path)."""
    if not ident:
        return False
    disp["nav_confirm_ident"] = ident
    disp["nav_confirm_prev"]  = prev_mode
    disp["mode"] = "nav_confirm"
    return True


# ── Compass calibration wizard ───────────────────────────────────────────────
# Cardinal walk-through: pilot points the aircraft at N / E / S / W in turn
# and taps CAPTURE.  We record (expected, raw) at each cardinal, then
# compute the circular mean of the (expected − raw) deltas and persist it
# as a single additive offset in ss["mag_cal_offset"].  The offset is added
# to the corrected yaw at render time so MAG-mode display reads true.  TRK
# mode is unaffected (the complementary filter sees yaw deltas, which a
# constant offset drops out of).  Same approach the iPhone display uses.
_MAG_CAL_CARDINALS = [("N",   0.0), ("NE",  45.0),
                      ("E",  90.0), ("SE", 135.0),
                      ("S", 180.0), ("SW", 225.0),
                      ("W", 270.0), ("NW", 315.0)]
_MCAL_W   = 460
_MCAL_H   = 290
_MCAL_BTN_H = 44
# Max acceptable spread (max − min) in degrees between the 8 cardinal
# captures for the alignment auto-apply to fire.  Looser values would
# auto-push a stale alignment if the pilot bumped the aircraft between
# captures; tighter values can be hard to hit even in steady cruise.
_ALIGN_MAX_SPREAD_DEG = 1.0


def _apply_mag_cal(raw_hdg, deltas):
    """Piecewise-linear mag cal correction.  `deltas` is a 4-list of
    signed (expected − raw) deltas at N / E / S / W.  Linearly
    interpolates between adjacent cardinals so each 90° quadrant gets
    its own correction curve — same convention as a real aircraft
    compass swing card.  Returns calibrated heading in [0, 360)."""
    if not deltas or len(deltas) != 4 or not any(deltas):
        return raw_hdg % 360.0
    h = raw_hdg % 360.0
    band = int(h // 90) % 4
    t = (h - band * 90.0) / 90.0
    d0 = deltas[band]
    d1 = deltas[(band + 1) % 4]
    # Short-arc interpolation in case adjacent deltas straddle ±180
    # (rare with sane mag bias but keeps the math safe).
    diff = ((d1 - d0 + 540.0) % 360.0) - 180.0
    delta = d0 + diff * t
    return (h + delta) % 360.0


def _instant_mag_heading_deg():
    """Return the Mahony's yaw output (post-remap, pre-magdev) for the cal
    wizard's RAW HDG display.

    We previously preferred state['yaw_wt901'] (the WT901's own PKT_ANGLE
    routed through the same remap) but in practice that chip has had
    PKT_ANGLE intermittently disabled — its RSW register sometimes
    refuses our reconfigure attempt and 0x53 silently goes dark. yaw_wt901
    then freezes at whatever value it last had and the cal screen reads a
    constant heading regardless of physical rotation.

    The Mahony's yaw is responsive at gyro rate (every gyro packet
    advances the quaternion). The original "lag" complaint was the
    mag-correction term opposing the gyro in a biased magnetic
    environment — that goes away after running the tumble (hard-iron)
    cal once. So for cal purposes, _yaw_uncal is the right signal:
    snappy when nothing is fighting it, and the tumble cal makes sure
    nothing is."""
    return float(disp.get("_yaw_uncal", disp.get("yaw", 0.0))) % 360.0


def _mag_cal_open(prev_mode: str):
    disp["mag_cal_wiz"] = {"step": 0, "samples": [], "msg": "",
                           "prev": prev_mode}
    disp["mode"] = "mag_cal"


def _mag_cal_capture():
    wiz = disp.get("mag_cal_wiz") or {}
    step = wiz.get("step", 0)
    if step >= len(_MAG_CAL_CARDINALS):
        return
    # Use instantaneous mag-derived heading (not the filter's gyro-integrated
    # output) so the captured value reflects what the pilot was looking at
    # the moment they pressed CAPTURE. The filter's yaw lags behind the
    # actual rotation; capturing on it would record stale headings.
    raw = _instant_mag_heading_deg()
    expected = _MAG_CAL_CARDINALS[step][1]
    wiz.setdefault("samples", []).append((expected, raw))
    # Also capture raw mag vector at this cardinal for hard-iron offset
    # computation. (mx,my,mz) come from the AHRS broadcast via smooth_state.
    mx = float(disp.get("mx", 0.0))
    my = float(disp.get("my", 0.0))
    mz = float(disp.get("mz", 0.0))
    wiz.setdefault("mag_samples", []).append((mx, my, mz))
    # Attitude sample for the level-flight alignment auto-capture.  Use
    # the displayed pitch/roll minus the Pico's trim so the value
    # reflects the FILTER OUTPUT (post-remap, pre-trim) — which is the
    # residual after any existing alignment is applied.  Averaging
    # these across the 8 cardinals gives the additional alignment
    # rotation needed; the spread across captures tells us whether the
    # aircraft was actually stable enough to trust the result.
    pitch_raw = float(disp.get("pitch", 0.0)) - float(disp.get("pitch_trim", 0.0))
    roll_raw  = float(disp.get("roll",  0.0)) - float(disp.get("roll_trim",  0.0))
    wiz.setdefault("att_samples", []).append((pitch_raw, roll_raw))
    wiz["step"] = step + 1
    wiz["msg"] = f"Captured {_MAG_CAL_CARDINALS[step][0]}."
    if wiz["step"] >= len(_MAG_CAL_CARDINALS):
        # Build 36-point deviation table (residual output correction)
        table = _build_magdev_table(wiz["samples"])
        # Compute hard-iron offsets from the captured raw mag vectors:
        # offset_axis = (max_axis + min_axis) / 2 — center of the ellipse
        # the rotating sensor traces in mag space. Subtracting this from
        # raw mag before the Mahony eats it stops the mag-correction term
        # from fighting the gyro in a biased environment.
        mag_samples = wiz.get("mag_samples", [])
        if len(mag_samples) >= 2:
            mxs = [s[0] for s in mag_samples]
            mys = [s[1] for s in mag_samples]
            mzs = [s[2] for s in mag_samples]
            offset = (0.5 * (max(mxs) + min(mxs)),
                      0.5 * (max(mys) + min(mys)),
                      0.5 * (max(mzs) + min(mzs)))
        else:
            offset = (0.0, 0.0, 0.0)
        # Store locally so Pi4 always shows calibrated heading,
        # even when the HTTP push to the Pico can't reach it.
        disp["ss"]["pi4_magdev"] = table
        disp["ss"]["pi4_mag_offset"] = list(offset)
        disp["ss"]["mag_cal_deltas"] = [0.0] * 4
        disp["ss"].pop("mag_cal_offset", None)
        disp["ss"]["mag_cal"] = "done"
        _settings.mark_dirty()
        _push_magcal_to_pico(table)   # 36-pt deviation table
        _push_magoff_to_pico(offset)  # hard-iron offsets
        # Level-flight alignment auto-capture.  Mean of the 8 residual
        # pitch/roll readings gives the additional input-side rotation
        # needed; spread tells us whether the readings were consistent
        # enough to trust.  Skipped (with reason) if either spread
        # exceeds _ALIGN_MAX_SPREAD_DEG — the aircraft was either not
        # level enough at each cardinal, or the AHRS itself wasn't
        # held still between captures.
        att = wiz.get("att_samples", [])
        align_msg = ""
        if len(att) >= 4:
            ps = [s[0] for s in att]; rs = [s[1] for s in att]
            mean_p = sum(ps) / len(ps)
            mean_r = sum(rs) / len(rs)
            sp_p = max(ps) - min(ps)
            sp_r = max(rs) - min(rs)
            if sp_p > _ALIGN_MAX_SPREAD_DEG or sp_r > _ALIGN_MAX_SPREAD_DEG:
                align_msg = (f"  alignment SKIPPED — spread "
                             f"P {sp_p:.1f}° / R {sp_r:.1f}° "
                             f"(need ≤{_ALIGN_MAX_SPREAD_DEG:.1f}°);"
                             f" re-level aircraft or AHRS mount")
            else:
                cur_p = float(disp["ss"].get("pitch_align", 0.0))
                cur_r = float(disp["ss"].get("roll_align",  0.0))
                new_p = max(-10.0, min(10.0, round(cur_p + mean_p, 1)))
                new_r = max(-10.0, min(10.0, round(cur_r + mean_r, 1)))
                disp["ss"]["pitch_align"] = new_p
                disp["ss"]["roll_align"]  = new_r
                _push_align_to_pico(new_p, new_r)
                align_msg = (f"  alignment applied: pitch {new_p:+.1f}°, "
                             f"roll {new_r:+.1f}° (spread "
                             f"P {sp_p:.1f}° / R {sp_r:.1f}°)")
        wiz["msg"] = (f"Saved locally — sending to AHRS… "
                      f"(hard-iron: {offset[0]:+.0f},{offset[1]:+.0f},{offset[2]:+.0f})"
                      + align_msg)
        wiz["step"]    = 0
        wiz["samples"] = []
        wiz["mag_samples"] = []
        wiz["att_samples"] = []


def _mag_cal_tumble_toggle():
    """First press → start tumble cal (Pico tracks mag min/max).
    Second press → finish, Pico computes (min+max)/2 per axis and stores."""
    wiz = disp.get("mag_cal_wiz") or {}
    if wiz.get("tumble_active"):
        # Stop
        _push_magoff_tumble("FINISH")
        wiz["tumble_active"] = False
        wiz["msg"] = "Tumble cal finished — offsets sent to AHRS."
        wiz["tumble_started_ms"] = None
    else:
        # Start — also reset the tumble extents we track locally for display
        _push_magoff_tumble("START")
        wiz["tumble_active"] = True
        wiz["tumble_started_ms"] = pygame.time.get_ticks()
        wiz["tumble_min"] = [None, None, None]
        wiz["tumble_max"] = [None, None, None]
        wiz["msg"] = ("Rotate AHRS slowly through ALL orientations — "
                      "pitch, roll, yaw — for ~30 s. Press STOP TUMBLE when done.")


def _mag_cal_tumble_tick():
    """Called each frame while the cal modal is open. When a tumble session
    is active, mirror the Pico's min/max tracking locally so the display
    can show the pilot how much "ground" they've covered."""
    wiz = disp.get("mag_cal_wiz") or {}
    if not wiz.get("tumble_active"):
        return
    mx = float(disp.get("mx", 0.0))
    my = float(disp.get("my", 0.0))
    mz = float(disp.get("mz", 0.0))
    mn = wiz.get("tumble_min") or [None, None, None]
    mx_arr = wiz.get("tumble_max") or [None, None, None]
    for i, v in enumerate((mx, my, mz)):
        if mn[i] is None or v < mn[i]: mn[i] = v
        if mx_arr[i] is None or v > mx_arr[i]: mx_arr[i] = v
    wiz["tumble_min"] = mn
    wiz["tumble_max"] = mx_arr


def _mag_cal_restart():
    wiz = disp.get("mag_cal_wiz") or {}
    wiz["step"] = 0
    wiz["samples"] = []
    wiz["mag_samples"] = []
    wiz["att_samples"] = []
    wiz["msg"] = "Restarted."


def _mag_cal_reset():
    """Wipe the stored cal on Pico and locally — both the deviation table
    and the hard-iron offsets."""
    _push_magcal_clear_to_pico()
    _push_magoff_clear_to_pico()
    disp["ss"].pop("pi4_magdev", None)
    disp["ss"].pop("pi4_mag_offset", None)
    disp["ss"]["mag_cal_deltas"] = [0.0, 0.0, 0.0, 0.0]
    disp["ss"].pop("mag_cal_offset", None)
    disp["ss"]["mag_cal"] = "idle"
    _settings.mark_dirty()
    wiz = disp.get("mag_cal_wiz") or {}
    wiz["step"] = 0
    wiz["samples"] = []
    wiz["mag_samples"] = []
    wiz["att_samples"] = []
    wiz["msg"] = "Calibration cleared."


def _mag_cal_close():
    wiz = disp.get("mag_cal_wiz") or {}
    disp["mode"] = wiz.get("prev", "ahrs_setup")


def _mcal_geom():
    bx = (DISPLAY_W - _MCAL_W) // 2
    by = (DISPLAY_H - _MCAL_H) // 2
    btn_y = by + _MCAL_H - _MCAL_BTN_H - 14
    btn_w = (_MCAL_W - 14 - 14 - 3 * 8) // 4
    btn_xs = [bx + 14 + i * (btn_w + 8) for i in range(4)]
    return bx, by, btn_y, btn_w, btn_xs


def draw_mag_cal(surf):
    """Compass-calibration modal — cardinal walk-through."""
    wiz = disp.get("mag_cal_wiz") or {"step": 0, "samples": [], "msg": ""}
    step = wiz.get("step", 0)
    card_name, card_exp = _MAG_CAL_CARDINALS[min(step,
                                                  len(_MAG_CAL_CARDINALS) - 1)]
    bx, by, btn_y, btn_w, btn_xs = _mcal_geom()

    _draw_veil(surf)
    panel = pygame.Surface((_MCAL_W, _MCAL_H), pygame.SRCALPHA)
    panel.fill((0, 12, 32, 235))
    surf.blit(panel, (bx, by))
    pygame.draw.rect(surf, CYAN, (bx, by, _MCAL_W, _MCAL_H),
                     width=2, border_radius=10)

    _text(surf, "COMPASS CAL", 14, (160, 200, 230), bold=True,
          cx=bx + _MCAL_W // 2, cy=by + 22)

    instr = (f"Step {step + 1} of {len(_MAG_CAL_CARDINALS)} — "
             f"point aircraft {card_name} ({int(card_exp):03d}°)")
    _text(surf, instr, 13, WHITE, cx=bx + _MCAL_W // 2, cy=by + 56)

    raw = _instant_mag_heading_deg()
    cal = float(disp.get("_yaw_cal",   disp.get("yaw", 0.0))) % 360.0

    _text(surf, "RAW HDG", 11, (200, 190, 100), bold=True,
          x=bx + 30, y=by + 88)
    _text(surf, f"{raw:6.1f}°", 20, (240, 220, 80), bold=True,
          x=bx + 30, y=by + 102)
    _text(surf, "CAL HDG", 11, (100, 200, 130), bold=True,
          x=bx + 150, y=by + 88)
    _text(surf, f"{cal:6.1f}°", 20, (80, 230, 120), bold=True,
          x=bx + 150, y=by + 102)

    # 8-point capture results — two rows of 4 (N NE E SE / S SW W NW)
    wiz_samples = wiz.get("samples", [])
    col_xs = [bx + 40 + c * (_MCAL_W - 80) // 3 for c in range(4)]
    for row in range(2):
        row_y = by + 148 + row * 26
        for col in range(4):
            i = row * 4 + col
            name, exp = _MAG_CAL_CARDINALS[i]
            cx_card = col_xs[col]
            lbl_col = (170, 185, 210) if i >= len(wiz_samples) else (100, 200, 255)
            _text(surf, name, 10, lbl_col, bold=True, cx=cx_card, cy=row_y)
            if i < len(wiz_samples):
                _exp, rawv = wiz_samples[i]
                d = ((_exp - rawv + 540) % 360) - 180
                _text(surf, f"{d:+.1f}°", 12, WHITE, bold=True,
                      cx=cx_card, cy=row_y + 14)
            else:
                _text(surf, "—", 12, (110, 120, 140), bold=True,
                      cx=cx_card, cy=row_y + 14)

    msg = wiz.get("msg", "") or ""
    if msg:
        col = (255, 180, 60) if ("WARNING" in msg or "FAILED" in msg or "failed" in msg) \
              else (60, 220, 80)
        _text(surf, msg, 12, col, cx=bx + _MCAL_W // 2, cy=by + 178)

    # When tumble cal is active, surface live progress so the pilot can tell
    # whether they've covered enough of the mag ellipse to compute a good
    # ellipse-center estimate. "Spread" is max-min per axis — a tight
    # number means they haven't covered enough orientations yet.
    if wiz.get("tumble_active"):
        _mag_cal_tumble_tick()
        mn = wiz.get("tumble_min") or [None, None, None]
        mx_arr = wiz.get("tumble_max") or [None, None, None]
        spread = [(mx_arr[i] - mn[i]) if (mx_arr[i] is not None and mn[i] is not None) else 0
                  for i in range(3)]
        elapsed_ms = pygame.time.get_ticks() - (wiz.get("tumble_started_ms") or 0)
        _text(surf, f"TUMBLE  {elapsed_ms/1000:.0f}s  "
                    f"spread X:{int(spread[0])}  Y:{int(spread[1])}  Z:{int(spread[2])}",
              11, (255, 200, 80), cx=bx + _MCAL_W // 2, cy=by + 198)

    # Left button reads CANCEL only when there's something to cancel —
    # i.e. partial captures haven't been committed yet.  Once the
    # 4-cardinal walk completes, the offset is already persisted and
    # the button just closes the modal, so EXIT is the honest label.
    in_progress = step > 0 and step < len(_MAG_CAL_CARDINALS)
    left_lbl   = "CANCEL" if in_progress else "EXIT"
    left_style = "danger" if in_progress else "ok"
    tumble_active = bool(wiz.get("tumble_active"))
    tumble_lbl = "STOP TUMBLE" if tumble_active else "TUMBLE"
    tumble_style = "danger" if tumble_active else "warn"
    _action_btn(surf, btn_xs[0], btn_y, btn_w, _MCAL_BTN_H, left_lbl, left_style)
    _action_btn(surf, btn_xs[1], btn_y, btn_w, _MCAL_BTN_H, "RESET",    "warn")
    _action_btn(surf, btn_xs[2], btn_y, btn_w, _MCAL_BTN_H, tumble_lbl, tumble_style)
    _action_btn(surf, btn_xs[3], btn_y, btn_w, _MCAL_BTN_H,
                f"⊕ CAPTURE {card_name}", "ok")


def mag_cal_hit(x, y):
    bx, by, btn_y, btn_w, btn_xs = _mcal_geom()
    if not (bx <= x <= bx + _MCAL_W and by <= y <= by + _MCAL_H):
        return None
    if btn_y <= y <= btn_y + _MCAL_BTN_H:
        for i, action in enumerate(("cancel", "reset", "tumble", "capture")):
            if btn_xs[i] <= x <= btn_xs[i] + btn_w:
                return action
    return "noop"


# ── Touch handler ─────────────────────────────────────────────────────────────
_touch_t0      = {}
_bug_dragging  = None    # "hdg" | "alt"
_active_fingers = {}     # finger_id → touch-down time (ms)
_multitouch_t0  = None   # time when 2nd finger touched down
_multitouch_max_fingers = 0  # peak finger count this gesture — 2 = setup,
                             # 3 = PFD↔MFD swap (same scheme as pi_zero)
# Set when a 2/3-finger gesture FIRES (enter setup / swap MFD); blocks taps
# until every finger of that gesture has lifted, so the finger-lift (or SDL
# re-synthesising the mouse as a finger leaves) doesn't land as a tap on
# whatever's now under the finger on the freshly-opened screen.
_gesture_tap_lockout = False

# Moving-map inset state.  _last_map_rect is updated each frame by the
# render loop so the touch handler can hit-test against it for the
# left/right tap-zoom affordance.
_last_map_rect = None


# ── Map overlay quick-cycle (Airspace / Traffic-only / METAR / NEXRAD) ────────
# Traffic stays on always (safety); this cycles the *other* heavy overlays
# one-at-a-time to keep the inset readable.  Logic in shared/mapoverlay.py
# (unit-tested); drives the same ds booleans the Display-setup pills do.
_map_overlay_state = _ovl.state
_map_overlay_label = _ovl.label
_map_overlay_cycle = _ovl.cycle


def _current_str_for_kbd(target, prev_mode):
    """String form of current keyboard-editable value for pre-population."""
    if prev_mode == "connectivity_setup":
        v = disp["cs"].get(target, "")
    else:
        v = disp["fp"].get(target, "")
    return str(v) if v not in (None, 0, "") else ""


def _active_bug_key():
    """Return "trk_bug" if the active heading source is GPS track, else
    "hdg_bug".  Used so all bug-set affordances (tap on heading box,
    arrow keys, numpad open, knob nudge) write to the bug that matches
    what the pilot is actually looking at."""
    ss = disp.get("ss", {})
    pref = ss.get("hdg_src", "auto")
    use_track, _, _ = _resolve_hdg_source(
        pref, disp.get("gps_ok", False), disp.get("ahrs_ok", False),
        disp.get("speed", 0.0))
    return "trk_bug" if use_track else "hdg_bug"


def _open_numpad(target):
    """Switch to numpad mode for the given bug target.  The buffer
    starts empty; draw_numpad falls back to showing the current value
    as a placeholder until the user types.  First digit replaces the
    placeholder naturally (because buf was empty), and backspace is a
    no-op until something is actually typed."""
    disp["numpad_target"] = target
    disp["numpad_buf"]    = ""
    disp["numpad_prev"]   = disp["mode"]
    disp["mode"]          = "numpad"


def handle_event(event, demo_mode):
    global _bug_dragging, _active_fingers, _multitouch_t0, _sim_state
    global _gesture_tap_lockout
    global _multitouch_max_fingers

    if event.type == pygame.QUIT:
        return False

    # ── Keyboard shortcuts ────────────────────────────────────────────────────
    if event.type == pygame.KEYDOWN:
        if event.key == pygame.K_ESCAPE:
            if disp["mode"] == "nav_confirm":
                _nav_confirm_cancel()
            elif disp["mode"] == "mag_cal":
                _mag_cal_close()
            elif disp["mode"] != "pfd":
                disp["mode"] = "pfd"   # ESC exits any overlay
            else:
                return False
        if event.key == pygame.K_RETURN and disp["mode"] == "nav_confirm":
            _nav_confirm_apply()
            return True
        if event.key == pygame.K_RETURN and disp["mode"] == "mag_cal":
            _mag_cal_capture()
            return True
        if event.key == pygame.K_d:
            return "toggle_demo"
        if event.key == pygame.K_F9:
            # Guided live preview capture — save the current frame to the
            # next manual filename (serviced in the render loop so the
            # saved PNG is toast-free).
            global _preview_cap_pending
            _preview_cap_pending = True
            return True
        if event.key == pygame.K_F10:
            # Reset the capture sequence back to the first target.
            global _preview_cap_idx, _preview_cap_msg, _preview_cap_msg_t
            _preview_cap_idx = 0
            _preview_cap_msg = "capture reset → next: full-screen MFD"
            _preview_cap_msg_t = time.monotonic()
            return True
        if disp["mode"] == "pfd":
            if event.key == pygame.K_UP:
                disp["alt_bug"] = round(disp["alt_bug"] / 100) * 100 + 100
                _ssync_publish_bugs()
            if event.key == pygame.K_DOWN:
                disp["alt_bug"] = round(disp["alt_bug"] / 100) * 100 - 100
                _ssync_publish_bugs()
            if event.key == pygame.K_LEFT:
                _bk = _active_bug_key()
                disp[_bk] = (round(disp[_bk]) - 10) % 360
                _ssync_publish_bugs()
            if event.key == pygame.K_RIGHT:
                _bk = _active_bug_key()
                disp[_bk] = (round(disp[_bk]) + 10) % 360
                _ssync_publish_bugs()
            if event.key in (pygame.K_PLUS, pygame.K_EQUALS):
                disp["baro_hpa"] = round(disp["baro_hpa"] * 100 + 1) / 100
                _ssync_publish_baro()
            if event.key == pygame.K_MINUS:
                disp["baro_hpa"] = round(disp["baro_hpa"] * 100 - 1) / 100
                _ssync_publish_baro()

    # ── Multi-finger tracking (FINGERDOWN / FINGERUP only) ───────────────────
    if event.type == pygame.FINGERDOWN:
        _now_ms = pygame.time.get_ticks()
        # Drop any finger that's been "down" longer than the longest intended
        # gesture (the 2 s MFD-swap hold) — it can't belong to the touch
        # starting now, so it's a ghost from a missed FINGERUP or the spurious
        # touch some panels emit at startup.  Without this, the first 2-finger
        # gesture after a restart counted a lingering boot ghost as a 3rd
        # finger and swapped to the MFD instead of opening setup.
        for _fid in [f for f, t in _active_fingers.items() if _now_ms - t > 3000]:
            _active_fingers.pop(_fid, None)
        if len(_active_fingers) < 2:        # ghost(s) cleared → resync gesture
            _multitouch_t0 = None
            _multitouch_max_fingers = 0
        if not _active_fingers:             # a genuinely fresh touch → the
            _gesture_tap_lockout = False    # gesture fingers are gone; accept taps
        _active_fingers[event.finger_id] = _now_ms
        if len(_active_fingers) >= 2 and _multitouch_t0 is None:
            _multitouch_t0 = _now_ms
        if len(_active_fingers) > _multitouch_max_fingers:
            _multitouch_max_fingers = len(_active_fingers)

    if event.type == pygame.FINGERUP:
        _active_fingers.pop(event.finger_id, None)
        if len(_active_fingers) < 2:
            _multitouch_t0 = None
            _multitouch_max_fingers = 0
        if not _active_fingers:             # all fingers lifted → end the lockout
            _gesture_tap_lockout = False

    # ── Drag-to-scroll on setup screens ──────────────────────────────────────
    # We defer the tap-fire on BUTTONDOWN inside a drag-capable setup
    # screen, watch MOTION to detect a scroll-drag, and on BUTTONUP either
    # consume the drag (no action fires) or replay the tap at the up
    # position so the underlying row-hit code runs as if nothing happened.
    global _ss_drag, _dispatch_replay, _mfd_drag, _fpl_drag, _fpl_scroll
    global _wx_drag, _prc_drag, _prc_scroll
    # ── FPL list scroll drag (defer-replay; taps still fire) ──────────────────
    if event.type in (pygame.MOUSEMOTION, pygame.FINGERMOTION) and _fpl_drag is not None:
        pos = event.pos if hasattr(event, "pos") else (
            int(event.x * DISPLAY_W), int(event.y * DISPLAY_H))
        _, y = pos
        dy = y - _fpl_drag["down_y"]
        if not _fpl_drag["is_drag"] and abs(dy) > _FPL_DRAG_THRESHOLD:
            _fpl_drag["is_drag"] = True
        if _fpl_drag["is_drag"]:
            wps = disp.get("fpl", {}).get("waypoints", [])
            max_s = _fpl_max_scroll(len(wps))
            _fpl_scroll = max(0, min(max_s, _fpl_drag["scroll_at_down"] - dy))
        _fpl_drag["pos"] = pos
        return True
    if event.type in (pygame.MOUSEBUTTONUP, pygame.FINGERUP) and _fpl_drag is not None:
        d = _fpl_drag
        _fpl_drag = None
        if not d["is_drag"]:
            _dispatch_replay = True
            try:
                handle_event(pygame.event.Event(
                    pygame.MOUSEBUTTONDOWN, {"pos": d["pos"], "button": 1}),
                    demo_mode)
            finally:
                _dispatch_replay = False
        return True

    # ── Approach procedure/transition picker scroll drag ──────────────────────
    if event.type in (pygame.MOUSEMOTION, pygame.FINGERMOTION) and _prc_drag is not None:
        pos = event.pos if hasattr(event, "pos") else (
            int(event.x * DISPLAY_W), int(event.y * DISPLAY_H))
        dy = pos[1] - _prc_drag["down_y"]
        if not _prc_drag["is_drag"] and abs(dy) > _FPL_DRAG_THRESHOLD:
            _prc_drag["is_drag"] = True
        if _prc_drag["is_drag"]:
            _prc_scroll = max(0, min(_prc_max_scroll(_prc_drag["n"]),
                                     _prc_drag["scroll_at_down"] - dy))
        _prc_drag["pos"] = pos
        return True
    if event.type in (pygame.MOUSEBUTTONUP, pygame.FINGERUP) and _prc_drag is not None:
        d = _prc_drag
        _prc_drag = None
        if not d["is_drag"]:
            _dispatch_replay = True
            try:
                handle_event(pygame.event.Event(
                    pygame.MOUSEBUTTONDOWN, {"pos": d["pos"], "button": 1}),
                    demo_mode)
            finally:
                _dispatch_replay = False
        return True

    # ── MFD pan drag ──────────────────────────────────────────────────────────
    # Drag-to-scroll the TAF / advisory readouts (tap closes; drag scrolls).
    if event.type in (pygame.MOUSEMOTION, pygame.FINGERMOTION) and _wx_drag is not None:
        pos = event.pos if hasattr(event, "pos") else (
            int(event.x * DISPLAY_W), int(event.y * DISPLAY_H))
        dy = pos[1] - _wx_drag["down_y"]
        if abs(dy) > 6:
            _wx_drag["is_drag"] = True
        if _wx_drag["is_drag"]:
            disp["wx_scroll"] = max(0, _wx_drag["scroll0"] - dy)
        return True

    if event.type in (pygame.MOUSEBUTTONUP, pygame.FINGERUP) and _wx_drag is not None:
        d = _wx_drag
        _wx_drag = None
        if not d["is_drag"]:           # a tap (not a scroll) dismisses
            disp["wx_taf"] = None
            disp["wx_text"] = None
            disp["wx_winds"] = None
            disp["wx_scroll"] = 0
        return True

    if event.type in (pygame.MOUSEMOTION, pygame.FINGERMOTION) and _mfd_drag is not None:
        pos = event.pos if hasattr(event, "pos") else (
            int(event.x * DISPLAY_W), int(event.y * DISPLAY_H))
        x, y = pos
        dx = x - _mfd_drag["down_x"]
        dy = y - _mfd_drag["down_y"]
        if (not _mfd_drag["is_drag"]
                and (abs(dx) > _MFD_DRAG_THRESHOLD or abs(dy) > _MFD_DRAG_THRESHOLD)):
            _mfd_drag["is_drag"] = True
        if _mfd_drag["is_drag"]:
            unproj = _mfd_drag["unproject"]
            la0, lo0 = unproj(_mfd_drag["down_x"], _mfd_drag["down_y"])
            la1, lo1 = unproj(x, y)
            disp["mfd_pan"]["lat"] = _mfd_drag["base_lat"] - (la1 - la0)
            disp["mfd_pan"]["lon"] = _mfd_drag["base_lon"] - (lo1 - lo0)
        _mfd_drag["pos"] = (x, y)
        return True

    if event.type in (pygame.MOUSEBUTTONUP, pygame.FINGERUP) and _mfd_drag is not None:
        d = _mfd_drag
        _mfd_drag = None
        if not d["is_drag"]:
            # A tap (not a pan).  Tapping an airport — on ANY page — offers a
            # choice of Weather or Direct-To, since on a cross-country you
            # almost always want the field's WX too.  We show the field's own
            # METAR if it has one, else the nearest reporting station (labelled
            # with distance).  With no METARs loaded at all it's a straight
            # Direct-To.  A tap on a bare METAR dot (WX overlay up, no airport
            # under the finger) opens the weather readout directly.
            tx, ty = d["pos"]
            # A loaded Direct-To destination is tappable at ANY zoom (its
            # diamond is drawn even past the airport-dot range); otherwise
            # hit-test the drawn airport dots.
            _ovl_state = _map_overlay_state(disp["ds"])
            if _ovl_state == "tfc":
                # On the traffic page the map is about aircraft — tap one for
                # its detail card; airports/METARs/hazards aren't hit-tested
                # here so the traffic tap is unambiguous.
                t = _mfd_find_traffic(tx, ty)
                if t is not None:
                    disp["tfc_popup"] = dict(t)
            elif _ovl_state == "wnd":
                # On the winds page, tap a barb for that station's full table.
                sid = _mfd_find_winds(tx, ty)
                if sid:
                    disp["wx_winds"] = {"station": sid, "dist": None,
                                        "brg": None}
            else:
                apt = _mfd_find_d2_dest(tx, ty) or _mfd_find_airport(tx, ty)
                if apt:
                    ident, alat, alon = apt
                    # Store the field + position; the picker resolves WX live so
                    # a post-pan fetch fills it in and far stale WX is dropped.
                    disp["mfd_pick"] = {"airport": ident,
                                        "lat": alat, "lon": alon}
                else:
                    met = _mfd_find_metar(tx, ty)
                    if met:
                        # Bare METAR dot → the weather product picker on that
                        # station (any page/zoom the dots are drawn).
                        disp["wx_menu"] = {
                            "airport": met.get("icao", ""),
                            "icao": met.get("icao", ""),
                            "lat": met.get("lat"), "lon": met.get("lon"),
                            "metar": dict(met)}
                    else:
                        # A tap inside a shaded hazard area opens its advisory.
                        g = _mfd_find_graphic(tx, ty)
                        if g is not None:
                            _wx_open_graphic_text(g)
        return True

    if event.type in (pygame.MOUSEMOTION, pygame.FINGERMOTION) and _ss_drag is not None:
        pos = event.pos if hasattr(event, "pos") else (
            int(event.x * DISPLAY_W), int(event.y * DISPLAY_H))
        x, y = pos
        dy = y - _ss_drag["down_y"]
        if not _ss_drag["is_drag"] and abs(dy) > _SS_DRAG_THRESHOLD:
            _ss_drag["is_drag"] = True
        if _ss_drag["is_drag"]:
            mode = _ss_drag["mode"]
            max_s = _ss_max_scroll(_SS_DRAG_MODES.get(mode, 5))
            new_scroll = _ss_drag["scroll_at_down"] - dy
            _ss_scroll[mode] = max(0, min(max_s, new_scroll))
        _ss_drag["pos"] = (x, y)
        return True

    if event.type in (pygame.MOUSEBUTTONUP, pygame.FINGERUP) and _ss_drag is not None:
        d = _ss_drag
        _ss_drag = None
        if not d["is_drag"]:
            _dispatch_replay = True
            try:
                fake = pygame.event.Event(
                    pygame.MOUSEBUTTONDOWN,
                    {"pos": d["pos"], "button": 1})
                handle_event(fake, demo_mode)
            finally:
                _dispatch_replay = False
        return True

    # ── Single-touch / mouse ──────────────────────────────────────────────────
    if event.type in (pygame.MOUSEBUTTONDOWN, pygame.FINGERDOWN):
        # Skip if this is part of a multi-touch gesture, or while a just-fired
        # gesture's fingers are still down (lockout) — so entering setup / swap
        # doesn't tap whatever's under the finger as it lifts.
        if len(_active_fingers) >= 2 or _gesture_tap_lockout:
            return True

        pos = event.pos if hasattr(event, "pos") else (
            int(event.x * DISPLAY_W), int(event.y * DISPLAY_H))
        x, y = pos

        mode = disp["mode"]

        # ── Full-screen MFD ───────────────────────────────────────────────
        if mode == "pfd" and disp.get("display_mode", "pfd") == "mfd":
            # WX readouts / menus stack on top — they take the tap first.
            # TAF / advisory readouts: start a drag-or-tap (drag scrolls, tap
            # closes — handled on MOTION/UP above).
            if disp.get("wx_taf") or disp.get("wx_text") or disp.get("wx_winds"):
                _wx_drag = {"down_y": y, "scroll0": disp.get("wx_scroll", 0),
                            "is_drag": False}
                return True
            if disp.get("wx_menu"):
                _wx_menu_hit(x, y)
                return True
            # The airport/METAR chooser is up → its buttons take the tap.
            if disp.get("mfd_pick"):
                _mfd_pick_hit(x, y)
                return True
            # A METAR readout is up → any tap dismisses it.
            if disp.get("wx_popup"):
                disp["wx_popup"] = None
                return True
            # A traffic detail card is up → any tap dismisses it.
            if disp.get("tfc_popup"):
                disp["tfc_popup"] = None
                return True
            # The quick MAP LAYERS panel is up → route the tap to it (toggle a
            # layer, or tap outside to close) and consume it.
            if disp.get("mfd_layers"):
                _mfd_layers_hit(x, y)
                return True
            r = _p4_mfd_rects()

            def _in(rc):
                return rc[0] <= x <= rc[0] + rc[2] and rc[1] <= y <= rc[1] + rc[3]
            if _in(r["ovly"]):
                _map_overlay_cycle(disp["ds"])
                _settings.mark_dirty()
            elif _in(r["orient"]):
                cur = disp["ds"].get("map_orient", "trk")
                disp["ds"]["map_orient"] = "nrth" if cur == "trk" else "trk"
                _settings.mark_dirty()
            elif _mfd_is_panned() and _in(r["center"]):
                _mfd_clear_pan()
            elif _in(r["zoom_in"]):
                if _map_overlay_state(disp["ds"]) == "wnd":
                    _winds_zoom_step(-1)
                else:
                    cur = int(disp["ds"].get("map_zoom_nm", 10))
                    disp["ds"]["map_zoom_nm"] = _map_mod.zoom_in(cur)
                    _settings.mark_dirty()
            elif _in(r["zoom_out"]):
                if _map_overlay_state(disp["ds"]) == "wnd":
                    _winds_zoom_step(+1)
                else:
                    cur = int(disp["ds"].get("map_zoom_nm", 10))
                    has_d2 = bool((disp.get("nav") or {}).get("ident"))
                    disp["ds"]["map_zoom_nm"] = _map_mod.zoom_out(
                        cur, allow_auto=has_d2)
                    _settings.mark_dirty()
            elif _in(r["layers"]):
                disp["mfd_layers"] = True        # open the quick layers panel
            elif _in(r["d2"]):
                # Direct-to entry — reuse the PFD's nav keyboard (NEAREST /
                # CANCEL FP / APPR / ENTER → nav_confirm).  kbd_prev="pfd"
                # so it returns to the MFD (display_mode stays "mfd").
                disp["kbd_target"] = "nav_ident"
                disp["kbd_prev"]   = "pfd"
                disp["kbd_buf"]    = ""
                disp["kbd_error"]  = ""
                disp["mode"]       = "keyboard"
            elif _mfd_source_status_hit(x, y):
                _mfd_cycle_traffic_source()
            elif _mfd_wx_status_hit(x, y):
                _mfd_cycle_wx_source()
            elif _map_overlay_state(disp["ds"]) == "wnd" and _in(r["winds"]):
                _mfd_cycle_winds_alt()
            elif _map_overlay_state(disp["ds"]) == "wnd" and _in(r["wtime"]):
                _mfd_cycle_winds_time()
            elif _mfd_strip_hit(x, y):
                disp["mss_which"] = "mfd"
                disp["mode"] = "mfd_strip_setup"
                disp["mss_sel"] = 0
            elif _in(r["fpl"]):
                disp["mode"] = "fpl"
            else:
                # Tap/drag on the map → start a pan.  MOTION converts to a
                # pan; UP without motion runs the METAR hit-test.
                cen_lat, cen_lon = _mfd_effective_center()
                _, unproj = _map_mod.make_projector(
                    (0, 0, DISPLAY_W, DISPLAY_H), cen_lat, cen_lon,
                    _mfd_last_orient, _mfd_last_range,
                    disp.get("yaw", 0.0), _mfd_last_track)
                _mfd_drag = {"down_x": x, "down_y": y, "pos": (x, y),
                             "is_drag": False, "base_lat": cen_lat,
                             "base_lon": cen_lon, "unproject": unproj}
            return True

        # Defer tap-fire on drag-capable setup screens — except taps inside
        # the title bar (back button), which still fire immediately.
        if (not _dispatch_replay
                and mode in _SS_DRAG_MODES
                and y >= _SS_TITLE_BAR_H):
            _ss_drag = {
                "mode":           mode,
                "down_x":         x,
                "down_y":         y,
                "pos":            (x, y),
                "scroll_at_down": _ss_scroll.get(mode, 0),
                "is_drag":        False,
            }
            return True

        # ── Setup screen taps ─────────────────────────────────────────────
        if mode == "setup":
            idx = setup_hit(x, y)
            # Indices follow _SETUP_ITEMS row-major order: 0 FLIGHT, 1 DISPLAY,
            # 2 AHRS, 3 CONNECTIVITY, 4 SCREEN SYNC, 5 SYSTEM, 6 EXIT,
            # 7 DATA & MAPS.
            if   idx == 6: disp["mode"] = "pfd"
            elif idx == 7:
                _ss_reset_scroll("downloads_setup")
                disp["mode"] = "downloads_setup"
            elif idx == 0:
                _ss_reset_scroll("flight_profile")
                disp["mode"] = "flight_profile"
            elif idx == 1:
                _ss_reset_scroll("display_setup")
                disp["dsp_tab"] = 0
                disp["mode"] = "display_setup"
            elif idx == 2:
                _ss_reset_scroll("ahrs_setup")
                disp["mode"] = "ahrs_setup"
            elif idx == 3:
                actual = disp["cs"].get("wifi_actual", "")
                if actual:
                    disp["cs"]["wifi_ssid"] = actual
                _ss_reset_scroll("connectivity_setup")
                disp["mode"] = "connectivity_setup"
            elif idx == 4:
                _ss_reset_scroll("screen_sync_setup")
                disp["mode"] = "screen_sync_setup"
            elif idx == 5:
                _ss_reset_scroll("system_setup")
                disp["mode"] = "system_setup"
            return True

        # ── Display settings taps ─────────────────────────────────────────
        if mode == "display_setup":
            action = display_setup_hit(x, y, disp["ds"])
            if action == "back":
                disp["mode"] = "setup"
            elif action and action.startswith("tab:"):
                disp["dsp_tab"] = int(action.split(":")[1])
            elif action and action.startswith("set:"):
                _, key, val_str = action.split(":", 2)
                # Coerce by token: True/False → bool, digits → int, else str.
                # Lets one action protocol carry the mix of types now in
                # disp["ds"] (units strings, map enable/layer bools, zoom int).
                if val_str in ("True", "False"):
                    val = (val_str == "True")
                elif val_str.lstrip("-").isdigit():
                    val = int(val_str)
                else:
                    val = val_str
                disp["ds"][key] = val
                if key == "audio_enabled":
                    audio_alerts.set_enabled(bool(val))
                _settings.mark_dirty()
            elif action and action.startswith("inc:"):
                # Generic 1–10 stepper for any display-setup row that
                # uses the inc/dec UI (brightness, audio_volume, ...).
                _, k, delta_str = action.split(":", 2)
                delta = int(delta_str)
                cur = int(disp["ds"].get(k, 8))
                disp["ds"][k] = max(1, min(10, cur + delta))
                if k == "brightness":
                    _set_backlight(disp["ds"][k])
                elif k == "audio_volume":
                    audio_alerts.set_volume(disp["ds"][k] / 10.0)
                _settings.mark_dirty()
            return True

        # Picker scroll controls (▲/▼ tap buttons) + list-area scroll-drag.
        if mode in ("appr_proc_select", "appr_trans_select"):
            n = (len(_appr_published((disp.get("approach") or {}).get("airport", "")))
                 if mode == "appr_proc_select"
                 else len(_appr_pending_transitions()))
            btn = _prc_scroll_btn_hit(n, x, y)
            if btn:
                _prc_scroll_by(n, btn)
                return True
            # List area defers for a possible scroll-drag; header/back taps
            # (above the list) fall through to the hit-test.
            if not _dispatch_replay and _PRC_TOP <= y <= _prc_list_bot() \
                    and x < DISPLAY_W - _PRC_SB_W:
                _prc_drag = {"down_y": y, "pos": (x, y),
                             "scroll_at_down": _prc_scroll, "is_drag": False, "n": n}
                return True

        # ── Published-approach procedure picker taps ──────────────────────
        if mode == "appr_proc_select":
            act, payload = appr_proc_select_hit(x, y)
            if act == "back":
                disp["mode"] = "pfd"
            elif act == "pick":
                airport = (disp.get("approach") or {}).get("airport", "")
                p = (_navdata.procedure(airport, payload)
                     if _navdata is not None else None)
                if p and (p.get("transitions") or {}):
                    disp["approach"]["pending_proc"] = payload
                    _prc_scroll = 0
                    disp["mode"] = "appr_trans_select"
                else:
                    from_fpl = bool(disp.get("appr_from_fpl"))
                    if _approach_load_published(airport, payload, "",
                                                activate=not from_fpl):
                        disp["mode"] = "appr_preview" if from_fpl else "pfd"
            return True

        # ── Published-approach transition picker taps ─────────────────────
        if mode == "appr_trans_select":
            act, payload = appr_trans_select_hit(x, y)
            if act == "back":
                _prc_scroll = 0
                disp["mode"] = "appr_proc_select"
            elif act == "pick":
                ap = disp.get("approach") or {}
                airport = ap.get("airport", "")
                proc = ap.get("pending_proc", "")
                from_fpl = bool(disp.get("appr_from_fpl"))
                trans = "" if payload == "VECTORS" else payload
                if _approach_load_published(airport, proc, trans,
                                            activate=not from_fpl):
                    disp["mode"] = "appr_preview" if from_fpl else "pfd"
            return True

        # ── Approach preview taps ─────────────────────────────────────────
        if mode == "appr_preview":
            action = appr_preview_hit(x, y)
            if action == "back":
                _approach_cancel()
                _prc_scroll = 0
                disp["mode"] = "appr_proc_select"
            elif action == "load":
                disp["mode"] = "pfd"
            return True

        # ── Approach selection taps ───────────────────────────────────────
        if mode == "approach_select":
            action = approach_select_hit(x, y)
            if action == "back":
                disp["mode"] = "pfd"
            elif action == "cancel":
                _approach_cancel()
                disp["mode"] = "pfd"
            elif action and action.startswith("select:"):
                idx = int(action.split(":", 1)[1])
                ident = (disp.get("approach") or {}).get("airport", "") or \
                        (disp.get("nav") or {}).get("ident", "")
                ends = _apr_runway_ends(ident)
                if 0 <= idx < len(ends):
                    # From the FPL → load armed (pilot activates later); a plain
                    # direct-to APPR loads + activates in one step.
                    from_fpl = bool(disp.get("appr_from_fpl"))
                    _approach_load(ident, ends[idx], activate=not from_fpl)
                    disp["mode"] = "pfd"
            return True

        # ── AHRS / Sensors taps ───────────────────────────────────────────
        if mode == "ahrs_setup":
            action = ahrs_setup_hit(x, y, disp["ss"])
            if action == "back":
                disp["mode"] = "setup"
            elif action and action.startswith("trim:"):
                _, key, delta_str = action.split(":")
                disp["ss"][key] = round(disp["ss"].get(key, 0.0) + float(delta_str), 1)
                _settings.mark_dirty()
            elif action and action.startswith("align:"):
                # Input-side axis alignment — clamp to ±10° to match the
                # firmware-side cap, push to the Pico via $ALIGN.
                _, key, delta_str = action.split(":")
                new = round(disp["ss"].get(key, 0.0) + float(delta_str), 1)
                if   new > 10.0: new = 10.0
                elif new < -10.0: new = -10.0
                disp["ss"][key] = new
                _settings.mark_dirty()
                _push_align_to_pico(
                    float(disp["ss"].get("pitch_align", 0.0)),
                    float(disp["ss"].get("roll_align",  0.0)))
            elif action == "mag_cal_open":
                _mag_cal_open("ahrs_setup")
            elif action == "terrain_inhibit_toggle":
                toggle_terrain_inhibit()
            elif action and action.startswith("set:"):
                _, key, val = action.split(":", 2)
                disp["ss"][key] = val
                _settings.mark_dirty()
                if key in ("orientation", "mounting"):
                    _push_orient_to_pico(
                        disp["ss"].get("orientation", "right"),
                        disp["ss"].get("mounting", "normal"))
            return True

        # ── Connectivity taps ─────────────────────────────────────────────
        if mode == "connectivity_setup":
            action = connectivity_setup_hit(x, y, disp["cs"])
            if action == "back":
                disp["mode"] = "setup"
            elif action and action.startswith("edit:"):
                key = action.split(":", 1)[1]
                disp["kbd_target"] = key
                disp["kbd_prev"]   = "connectivity_setup"
                disp["kbd_buf"]    = _current_str_for_kbd(key, "connectivity_setup")
                disp["kbd_shift"]  = False
                disp["mode"]       = "keyboard"
            elif action == "scan_wifi":
                _do_scan()
            elif action == "apply_wifi":
                disp["cs"]["apply_msg"] = "Applying…"
                def _do_apply():
                    ok, msg = _apply_wifi(disp["cs"]["wifi_ssid"],
                                          disp["cs"]["wifi_pass"])
                    disp["cs"]["apply_msg"] = msg
                threading.Thread(target=_do_apply, daemon=True).start()
            elif action == "test_ahrs":
                disp["cs"]["test_msg"] = "Testing…"
                def _do_test():
                    ok, msg = _test_ahrs_connection(disp["cs"]["ahrs_url"])
                    disp["cs"]["test_msg"] = msg
                    if ok:
                        _restart_sse(disp["cs"]["ahrs_url"])
                threading.Thread(target=_do_test, daemon=True).start()
            return True

        # ── Screen sync taps ──────────────────────────────────────────────
        if mode == "screen_sync_setup":
            action = screen_sync_setup_hit(x, y, disp["cs"])
            if action == "back":
                disp["mode"] = "setup"
            elif action == "toggle_enable":
                disp["cs"]["sync_enabled"] = not disp["cs"].get(
                    "sync_enabled", True)
                _settings.mark_dirty()
                _ssync_refresh_kinds()
            elif action and action.startswith("transport:"):
                disp["cs"]["sync_transport"] = action.split(":", 1)[1]
                _settings.mark_dirty()
                _ssync_refresh_kinds()
            elif action == "toggle_fpl_share":
                disp["cs"]["sync_fpl_enabled"] = not disp["cs"].get(
                    "sync_fpl_enabled", True)
                _settings.mark_dirty()
                _ssync_refresh_kinds()
            elif action and action.startswith("set_mode:"):
                # AHRS/GPS mutex selector: one of off / tx / rx.
                _, kind, mode = action.split(":", 2)
                disp["cs"][f"sync_publish_{kind}"] = (mode == "tx")
                disp["cs"][f"sync_consume_{kind}"] = (mode == "rx")
                _settings.mark_dirty()
                _ssync_refresh_kinds()
            elif action and action.startswith(("toggle_publish:",
                                                "toggle_consume:")):
                head, kind = action.split(":", 1)
                direction = head.split("_", 1)[1]    # "publish" or "consume"
                key = f"sync_{direction}_{kind}"
                disp["cs"][key] = not disp["cs"].get(key, False)
                _settings.mark_dirty()
                _ssync_refresh_kinds()
            return True

        # ── WiFi scan screen taps ─────────────────────────────────────────────
        if mode == "wifi_scan":
            action = wifi_scan_hit(x, y, disp["cs"])
            if action == "back":
                disp["mode"] = "connectivity_setup"
            elif action == "rescan":
                _do_scan()
            elif action and action.startswith("select:"):
                idx  = int(action.split(":", 1)[1])
                nets = disp["cs"].get("scan_nets", [])
                if 0 <= idx < len(nets):
                    net = nets[idx]
                    disp["cs"]["wifi_ssid"] = net["ssid"]
                    disp["cs"]["wifi_pass"] = ""
                    if net.get("secured"):
                        disp["kbd_target"] = "wifi_pass"
                        disp["kbd_prev"]   = "connectivity_setup"
                        disp["kbd_buf"]    = ""
                        disp["kbd_shift"]  = False
                        disp["mode"]       = "keyboard"
                    else:
                        disp["mode"] = "connectivity_setup"
            elif action == "scroll_up":
                disp["cs"]["scan_scroll"] = max(0, disp["cs"].get("scan_scroll", 0) - 1)
            elif action == "scroll_down":
                ws_btn_y = DISPLAY_H - _WS_BTN_H - 8
                list_h   = ws_btn_y - _WS_LIST_Y - 8
                visible  = list_h // _WS_ITEM_H
                max_s    = max(0, len(disp["cs"].get("scan_nets", [])) - visible)
                disp["cs"]["scan_scroll"] = min(max_s, disp["cs"].get("scan_scroll", 0) + 1)
            return True

        # ── System screen taps ────────────────────────────────────────────
        # ── MFD strip-slot chooser ────────────────────────────────────────
        if mode == "mfd_strip_setup":
            act, payload = mfd_strip_setup_hit(x, y)
            if act == "back":
                disp["mode"] = "pfd"        # MFD runs under mode == "pfd"
            elif act == "slot":
                disp["mss_sel"] = int(payload)
            elif act == "kind":
                _key, _def, _cnt, _t = _mss_cfg()
                kinds = _mss_kinds()
                sel = int(disp.get("mss_sel", 0)) % _cnt
                kinds[sel] = payload
                disp["ds"][_key] = kinds
                disp["mss_sel"] = (sel + 1) % _cnt
                _settings.mark_dirty()
            elif act == "eta_tz":
                disp["ds"]["eta_local"] = bool(payload)
                _settings.mark_dirty()
            return True

        # FPL list area defers for a possible scroll-drag; action-bar /
        # header taps (above the list) fall straight through to fpl_hit.
        if not _dispatch_replay and mode == "fpl":
            list_top, list_bot = _fpl_list_area_y()
            if list_top <= y <= list_bot:
                _fpl_drag = {"down_x": x, "down_y": y, "pos": (x, y),
                             "scroll_at_down": _fpl_scroll, "is_drag": False}
                return True

        # ── FPL editor ────────────────────────────────────────────────────
        if mode == "fpl":
            act, payload = fpl_hit(x, y)
            if act == "back":
                disp["mode"] = "pfd"
            elif act == "add_icao":
                _fpl_open_add_keyboard()
            elif act == "add_ll":
                _fpl_open_latlon_entry()
            elif act == "add_lib":
                disp["mode"] = "user_wpt_picker"
            elif act == "save":
                if disp.get("fpl", {}).get("waypoints", []):
                    _fpl_open_save_keyboard()
            elif act == "load":
                if disp.get("fpl_saved", {}).get("plans", []):
                    disp["mode"] = "fpl_plan_picker"
            elif act == "deact":
                _fpl_deactivate()
            elif act == "load_appr":
                # The approach button is a 3-state machine, mirroring a real
                # navigator: nothing loaded → open the runway picker to LOAD
                # (armed only); loaded but not active → ACTIVATE (engage
                # threshold guidance); active → CANCEL.  Loading never retargets
                # the CDI — the plan keeps flying to the airport until the pilot
                # deliberately activates.
                _phase = _approach_phase()
                if _phase in ("active", "missed"):
                    _approach_cancel()
                elif _phase == "armed":
                    _approach_engage()
                else:
                    dest = _fpl_dest_approach_ident()
                    if dest:
                        disp["approach"]["airport"] = dest
                        disp["appr_from_fpl"] = True
                        _prc_scroll = 0
                        disp["mode"] = ("appr_proc_select"
                                        if _appr_published(dest)
                                        else "approach_select")
            elif act == "legmenu":
                _legmenu_open("fpl", payload)
            elif act == "appr_legmenu":
                _legmenu_open("appr", payload)
            elif act == "up" and payload > 0:
                _fpl_swap(payload, payload - 1)
            elif act == "down":
                _fpl_swap(payload, payload + 1)
            elif act == "delete":
                _fpl_remove(payload)
            return True

        # ── +LAT/LON user-waypoint entry ──────────────────────────────────
        if mode == "fpl_latlon_entry":
            act, payload = fpl_latlon_entry_hit(x, y)
            if act in ("back", "cancel"):
                disp["fle_err_field"] = ""
                disp["fle_err_msg"] = ""
                disp["mode"] = "fpl"
            elif act == "edit":
                _fle_open_kbd(payload)
            elif act == "save":
                field, msg = _fpl_commit_latlon()
                if field:
                    disp["fle_err_field"] = field
                    disp["fle_err_msg"] = msg
                else:
                    disp["fle_err_field"] = ""
                    disp["fle_err_msg"] = ""
                    disp["mode"] = "fpl"
            return True

        # ── User-waypoint picker (+ USER) ─────────────────────────────────
        if mode == "user_wpt_picker":
            act, payload = user_wpt_picker_hit(x, y)
            if act == "back":
                disp["mode"] = "fpl"
            elif act == "add" and payload is not None:
                ident = str(payload.get("ident", ""))
                field, _msg = _fpl_validate_user_ident(ident)
                if not field:
                    _fpl_add_waypoint(ident, payload["lat"], payload["lon"],
                                      payload.get("elev_ft", 0.0), user=True)
            elif act == "delete" and payload is not None:
                _user_wpt_delete(payload.get("ident", ""))
            return True

        # ── FPL plan picker (LOAD) ────────────────────────────────────────
        if mode == "fpl_plan_picker":
            act, payload = fpl_plan_picker_hit(x, y)
            if act == "back":
                disp["mode"] = "fpl"
            elif act == "load" and payload is not None:
                _fpl_plan_load(payload.get("name", ""))
                disp["mode"] = "fpl"
            elif act == "delete" and payload is not None:
                _fpl_plan_delete(payload.get("name", ""))
            return True

        if mode == "system_setup":
            action = system_setup_hit(x, y)
            if action == "back":
                disp["mode"] = "setup"
            elif action == "mode_pfd":
                disp["display_mode"] = "pfd"
                disp["mode"] = "pfd"
                _settings.mark_dirty()
            elif action == "mode_mfd":
                disp["ds"]["mfd_enabled"] = True
                disp["display_mode"] = "mfd"
                disp["mode"] = "pfd"      # leave setup; MFD shows in pfd mode
                _settings.mark_dirty()
            elif action == "terrain_data":
                disp["mode"] = "terrain_data"
            elif action == "obstacle_data":
                disp["mode"] = "obstacle_data"
            elif action == "airport_data":
                disp["mode"] = "airport_data"
            elif action == "airspace_data":
                disp["mode"] = "airspace_data"
            elif action == "ahrs_firmware":
                disp["fw"]["push_state"]  = ""
                disp["fw"]["push_msg"]    = ""
                disp["fw"]["flash_state"] = ""
                disp["fw"]["flash_msg"]   = ""
                disp["mode"] = "ahrs_firmware"
            elif action == "simulator":
                disp["mode"] = "sim_setup"
            elif action == "reset_defaults":
                for k,v in [("vs0",VS0),("vs1",VS1),("vfe",VFE),("vno",VNO),
                             ("vne",VNE),("va",VA),("vy",VY),("vx",VX)]:
                    disp["fp"][k] = v
                disp["ds"].update(spd_unit="kt", alt_unit="ft", baro_unit="inhg",
                                   brightness=8, night_mode=False)
                disp["ss"].update(pitch_trim=0.0, roll_trim=0.0)
            elif action == "quit":
                _settings.flush()
                pygame.quit()
                sys.exit(0)
            return True

        # ── AHRS firmware screen taps ─────────────────────────────────────
        if mode == "ahrs_firmware":
            action = ahrs_firmware_hit(x, y)
            if action == "back":
                disp["mode"] = "system_setup"
            elif action == "push_scripts":
                if disp["fw"].get("push_state") != "pushing":
                    _do_push_scripts()
            elif action == "flash_uf2":
                if disp["fw"].get("flash_state") != "flashing":
                    _do_flash_uf2()
            return True

        # ── Sim setup screen taps ─────────────────────────────────────────
        if mode == "sim_setup":
            action = sim_setup_hit(x, y)
            if action == "back":
                disp["mode"] = "system_setup"
            elif action and action.startswith("preset:"):
                disp["sim"]["preset_idx"] = int(action.split(":")[1])
            elif action and action.startswith("cond:"):
                key = action.split(":")[1]
                target_map = {
                    "init_alt": "sim_init_alt",
                    "init_hdg": "sim_init_hdg",
                    "init_spd": "sim_init_spd",
                }
                _open_numpad(target_map[key])
            elif action and action.startswith("sensor_on:"):
                sensor = action.split(":")[1]
                disp["sim"][sensor + "_fail"] = False
            elif action and action.startswith("sensor_fail:"):
                sensor = action.split(":")[1]
                disp["sim"][sensor + "_fail"] = True
            elif action == "start":
                _sim_state = SimFlyState()
                # Pause live AHRS transport so its writes don't clobber the
                # sim's writes to state.  Resumed on exit_sim.
                if _sse_client is not None:
                    _sse_client.paused = True
                disp["mode"] = "pfd"
            elif action == "cancel":
                disp["mode"] = "system_setup"
            return True

        # ── Sim controls overlay taps ─────────────────────────────────────
        if mode == "sim_controls":
            action = sim_controls_hit(x, y)
            if action == "exit_sim":
                _sim_state = None
                # Resume live AHRS transport — sim no longer owns state
                if _sse_client is not None:
                    _sse_client.paused = False
                disp["mode"] = "pfd"
            elif action == "exit_setup":
                # Close the overlay; the sim keeps running.  No state
                # touched — pilot just wants to fly the sim now.
                disp["mode"] = "pfd"
            elif action and action.startswith("sensor_on:"):
                sensor = action.split(":")[1]
                disp["sim"][sensor + "_fail"] = False
            elif action and action.startswith("sensor_fail:"):
                sensor = action.split(":")[1]
                disp["sim"][sensor + "_fail"] = True
            elif action and action.startswith("follow:"):
                disp["sim"]["follow_mode"] = action.split(":", 1)[1]
            elif action == "toggle_pause":
                disp["sim"]["paused"] = not disp["sim"].get("paused", False)
            # "noop" or None: consume the event either way
            return True

        # ── Nav-confirm modal (Activate Direct to XXXX?) ──────────────────
        if mode == "nav_confirm":
            action = nav_confirm_hit(x, y)
            if action == "activate":
                _nav_confirm_apply()
            elif action == "cancel":
                _nav_confirm_cancel()
            # "noop" / None / outside-panel: consume to keep the modal up
            return True

        # ── Nav picker (CDI tap → Direct-To / Flight Plan) ────────────────
        if mode == "leg_menu":
            action = leg_menu_hit(x, y)
            lm = disp.get("leg_menu") or {}
            kind, idx = lm.get("kind"), int(lm.get("idx", 0))
            if action == "activate":
                if kind == "appr":
                    _approach_goto_leg(idx, from_present=False)
                else:
                    _fpl_activate(idx, reset_activation=False)
                disp["mode"] = "fpl"
            elif action == "d2":
                if kind == "appr":
                    _approach_goto_leg(idx, from_present=True)
                else:
                    _fpl_activate(idx, reset_activation=True)
                disp["mode"] = "fpl"
            elif action == "vectors":
                if kind == "appr":
                    fi = int((disp.get("approach") or {}).get("final_idx", 0))
                    _approach_goto_leg(fi, from_present=True)
                disp["mode"] = "fpl"
            elif action == "cancel":
                disp["mode"] = "fpl"
            return True

        if mode == "nav_pick":
            action = nav_pick_hit(x, y)
            if action == "d2":
                disp["kbd_target"] = "nav_ident"
                disp["kbd_prev"]   = "pfd"
                disp["kbd_buf"]    = ""
                disp["kbd_error"]  = ""
                disp["mode"]       = "keyboard"
            elif action == "fpl":
                disp["mode"] = "fpl"        # the existing FPL editor
            elif action == "appr_activate":
                _approach_engage()
                disp["mode"] = "pfd"
            elif action == "appr_missed":
                _approach_go_missed()
                disp["mode"] = "pfd"
            elif action == "appr_cancel":
                _approach_cancel()
                disp["mode"] = "pfd"
            elif action == "cancel":
                disp["mode"] = "pfd"
            # "noop": keep the modal up
            return True

        # ── Compass calibration wizard taps ──────────────────────────────
        if mode == "mag_cal":
            action = mag_cal_hit(x, y)
            if action == "capture":
                _mag_cal_capture()
            elif action == "tumble":
                _mag_cal_tumble_toggle()
            elif action == "reset":
                _mag_cal_reset()
            elif action == "cancel":
                _mag_cal_close()
            # "noop" / None / outside-panel: keep the modal up
            return True

        # ── DATA & MAPS page taps ─────────────────────────────────────────
        if mode == "downloads_setup":
            act, payload = downloads_setup_hit(x, y)
            if act == "back":
                disp["mode"] = "setup"
            elif act == "open":
                if payload == "navdata_data":
                    _nd_load()           # refresh stats on entry
                disp["mode"] = payload
            return True

        # ── Nav-data screen taps ──────────────────────────────────────────
        if mode == "navdata_data":
            action = navdata_data_hit(x, y, disp["nd"])
            if action == "back":
                disp["mode"] = "downloads_setup"
            elif action == "cancel":
                disp["nd"]["dl_cancel"] = True
            elif action == "download":
                if not disp["nd"]["downloading"]:
                    _nd_start_download()
            return True

        # ── Obstacle data screen taps ─────────────────────────────────────
        if mode == "obstacle_data":
            action = obstacle_data_hit(x, y, disp["od"])
            if action == "back":
                disp["mode"] = "downloads_setup"
            elif action == "cancel":
                disp["od"]["dl_cancel"] = True
            elif action == "download":
                if not disp["od"]["downloading"]:
                    _od_start_download()
            return True

        # ── Airport data screen taps ──────────────────────────────────────
        if mode == "airport_data":
            action = airport_data_hit(x, y, disp["ad"])
            if action == "back":
                disp["mode"] = "downloads_setup"
            elif action == "cancel":
                disp["ad"]["dl_cancel"] = True
            elif action == "download":
                if not disp["ad"]["downloading"]:
                    _ad_start_download()
            elif isinstance(action, str) and action.startswith("toggle:"):
                key = action.split(":", 1)[1]
                disp["ad"][key] = not disp["ad"].get(key, False)
                _settings.mark_dirty()
            elif action == "nav_direct":
                # Open with empty buf so the first keystroke replaces the
                # current ident, matching the heading/altitude/airspeed
                # numpad UX.  The active ident still shows as the
                # placeholder (resolved via disp["nav"]["ident"] in the
                # keyboard renderer).
                disp["kbd_target"] = "nav_ident"
                disp["kbd_prev"]   = "airport_data"
                disp["kbd_buf"]    = ""
                disp["kbd_error"]  = ""
                disp["mode"]       = "keyboard"
            elif action == "nav_nearest":
                # Same confirmation gate as a typed waypoint — surface
                # the resolved ident so the pilot can verify before the
                # flight plan changes.
                _nav_open_confirm(_nav_lookup_nearest(), "airport_data")
            elif action == "nav_clear":
                _nav_clear()
            return True

        # ── Airspace data screen taps ─────────────────────────────────────
        if mode == "airspace_data":
            action = airspace_data_hit(x, y)
            asp = disp["asp"]
            if action == "back":
                disp["mode"] = "downloads_setup"
            elif action == "download_static":
                if asp.get("downloading"):
                    asp["dl_cancel"] = True
                else:
                    _asp_start_download(asp_mod.DOWNLOAD_SOURCES_STATIC,
                                        "AIRSPACES")
            elif action == "download_tfr":
                if asp.get("downloading"):
                    asp["dl_cancel"] = True
                else:
                    _asp_start_download(asp_mod.DOWNLOAD_SOURCES_TFR,
                                        "TFRs")
            elif action == "classes":
                disp["mode"] = "airspace_classes"
            return True

        # ── Airspace per-class toggle screen taps ─────────────────────────
        if mode == "airspace_classes":
            action = airspace_classes_hit(x, y)
            if action == "back":
                disp["mode"] = "airspace_data"
            elif isinstance(action, str) and action.startswith("toggle:"):
                key = action.split(":", 1)[1]
                ds = disp["ds"]
                ds[key] = not ds.get(key, True)
                _settings.mark_dirty()
            return True

        # ── Terrain data screen taps ──────────────────────────────────────
        if mode == "terrain_data":
            action = terrain_data_hit(x, y, disp["td"])
            if action == "back":
                disp["mode"] = "downloads_setup"
            elif action == "cancel":
                disp["td"]["dl_cancel"] = True
                disp["wd"]["dl_cancel"] = True
            elif action == "current_area":
                if not disp["td"]["downloading"]:
                    _td_start_current_area()
            elif action == "water_masks":
                if not disp["wd"]["downloading"]:
                    _wd_start_download()
            elif action and action.startswith("region:"):
                if not disp["td"]["downloading"]:
                    idx = int(action.split(":")[1])
                    _td_start_download(_TD_REGIONS[idx])
            return True

        # ── Flight Profile screen taps ────────────────────────────────────
        if mode == "flight_profile":
            key = flight_profile_hit(x, y, disp["fp"])
            if key == "__back__":
                disp["mode"] = "setup"
            elif key is not None:
                ftype = next((f[4] for f in _FP_FIELDS if f[0]==key), "num")
                if ftype == "kbd":
                    disp["kbd_target"] = key
                    disp["kbd_prev"]   = "flight_profile"
                    disp["kbd_buf"]    = _current_str_for_kbd(key, "flight_profile")
                    disp["kbd_shift"]  = False
                    disp["mode"]       = "keyboard"
                else:
                    disp["numpad_target"] = key
                    disp["numpad_prev"]   = "flight_profile"
                    disp["numpad_buf"]    = ""
                    disp["mode"]          = "numpad"
            return True

        # ── Keyboard taps ─────────────────────────────────────────────────
        if mode == "keyboard":
            hit = keyboard_hit(x, y)
            if hit:
                lbl, sty = hit
                target  = disp["kbd_target"]
                _CS_MAX = {"wifi_ssid": 32, "wifi_pass": 63, "ahrs_url": 80,
                           "notam_client_id": 80, "notam_client_secret": 80,
                           "notam_env": 10}
                _FPL_KBD_MAX = {"fpl_ident": 7, "fpl_latlon_ident": 10,
                                "fpl_latlon_lat": 12, "fpl_latlon_lon": 12,
                                "fpl_save_name": 16, "nav_ident": 7}
                if target in _FPL_KBD_MAX:
                    max_len = _FPL_KBD_MAX[target]
                elif disp.get("kbd_prev") == "connectivity_setup":
                    max_len = _CS_MAX.get(target, 32)
                else:
                    max_len = next((f[3] for f in _FP_FIELDS if f[0]==target), 16)
                if sty == 'n':                # character / space
                    ch = ' ' if lbl == 'SPACE' else lbl
                    if len(disp["kbd_buf"]) < max_len:
                        disp["kbd_buf"] += ch
                    disp["kbd_error"] = ""    # any keystroke clears the error
                elif sty == 'del':            # backspace
                    disp["kbd_buf"] = disp["kbd_buf"][:-1]
                    disp["kbd_error"] = ""
                elif sty in ('shift', 'shift_on'):
                    disp["kbd_shift"] = not disp.get("kbd_shift", False)
                elif sty == 'x':              # CANCEL
                    disp["kbd_buf"] = ""
                    disp["kbd_error"] = ""
                    disp["mode"] = disp["kbd_prev"]
                elif sty == 'nrst':           # DIRECT TO NEAREST
                    # Resolve the nearest ident, close the keyboard,
                    # and route through the confirmation modal so the
                    # pilot can verify before the flight plan changes.
                    nearest = _nav_lookup_nearest()
                    prev = disp["kbd_prev"]
                    disp["kbd_buf"] = ""
                    disp["kbd_error"] = ""
                    if not _nav_open_confirm(nearest, prev):
                        # No nearest airport (no fix or empty DB) —
                        # just close the keyboard.
                        disp["mode"] = prev
                elif sty == 'clrfp':          # CANCEL FLIGHT PLAN
                    _nav_clear()
                    disp["kbd_buf"] = ""
                    disp["kbd_error"] = ""
                    disp["mode"] = disp["kbd_prev"]
                elif sty == 'appr':           # APPR — open runway picker
                    # Resolve the airport ident: typed buf wins over the
                    # currently-active D2 ident.  Validate against the
                    # airport DB (matches ENTER's path), and either
                    # surface "UNKNOWN WAYPOINT" or jump to the picker.
                    buf = disp["kbd_buf"].strip().upper()
                    cur_ident = disp.get("nav", {}).get("ident", "")
                    target_ident = buf or cur_ident
                    if not target_ident:
                        disp["kbd_error"] = "ENTER AIRPORT FIRST"
                    elif buf and not _nav_lookup_ident(buf):
                        disp["kbd_error"] = f"UNKNOWN WAYPOINT  {buf}"
                    elif not (_appr_published(target_ident)
                              or _ident_has_runways(target_ident)):
                        disp["kbd_error"] = f"NO APPROACHES  {target_ident}"
                    else:
                        # If the typed buf is a fresh airport, activate
                        # the D2 first so the picker sees it as the
                        # current airport.  Then jump to the picker.
                        if buf and buf != cur_ident:
                            _nav_set_by_ident(buf)
                        disp["approach"]["airport"] = target_ident
                        disp["appr_from_fpl"] = False  # plain D2
                        disp["kbd_buf"] = ""
                        disp["kbd_error"] = ""
                        _prc_scroll = 0
                        disp["mode"] = ("appr_proc_select"
                                        if _appr_published(target_ident)
                                        else "approach_select")
                elif sty == 'ok':             # ENTER
                    buf = disp["kbd_buf"].strip()
                    if target == "nav_ident":
                        # Three paths:
                        #   1. Buffer empty AND a waypoint is already
                        #      active → "keep what's there but
                        #      re-activate so the magenta line redraws
                        #      from my current position".  Confirm.
                        #   2. Buffer has text and resolves to a known
                        #      airport → confirm.
                        #   3. Buffer has text but doesn't resolve →
                        #      stay on the keyboard and surface
                        #      "UNKNOWN WAYPOINT" so the pilot can fix
                        #      the typo without retyping from scratch.
                        cur_ident = disp.get("nav", {}).get("ident", "")
                        if buf:
                            candidate = buf.upper()
                            if _nav_lookup_ident(candidate):
                                disp["nav_confirm_ident"] = candidate
                                disp["nav_confirm_prev"]  = disp["kbd_prev"]
                                disp["kbd_buf"] = ""
                                disp["kbd_error"] = ""
                                disp["mode"] = "nav_confirm"
                            else:
                                disp["kbd_error"] = f"UNKNOWN WAYPOINT  {candidate}"
                            return True
                        if cur_ident:
                            disp["nav_confirm_ident"] = cur_ident
                            disp["nav_confirm_prev"]  = disp["kbd_prev"]
                            disp["kbd_buf"] = ""
                            disp["kbd_error"] = ""
                            disp["mode"] = "nav_confirm"
                            return True
                        # Empty buf with no active waypoint → close.
                        disp["kbd_buf"] = ""
                        disp["kbd_error"] = ""
                        disp["mode"] = disp["kbd_prev"]
                        return True
                    elif target == "fpl_ident":
                        # FPL append by ICAO ident → fpl screen.
                        if not buf:
                            disp["kbd_buf"] = ""; disp["kbd_error"] = ""
                            disp["mode"] = "fpl"
                            return True
                        candidate = buf.upper()
                        hit = _nav_lookup_ident(candidate)
                        if hit is None:
                            disp["kbd_error"] = f"UNKNOWN WAYPOINT  {candidate}"
                            return True
                        ident, lat, lon, elev_ft = hit[:4]
                        name = hit[4] if len(hit) > 4 else ""
                        region = hit[5] if len(hit) > 5 else ""
                        if _fpl_add_waypoint(ident, lat, lon, elev_ft,
                                             name=name, region=region):
                            disp["kbd_buf"] = ""; disp["kbd_error"] = ""
                            disp["mode"] = "fpl"
                        else:
                            disp["kbd_error"] = f"PLAN FULL ({_FPL_MAX_WAYPOINTS} MAX)"
                        return True
                    elif target == "fpl_save_name":
                        if not buf:
                            disp["kbd_buf"] = ""; disp["kbd_error"] = ""
                            disp["mode"] = "fpl"
                            return True
                        ok, msg = _fpl_plan_save(buf)
                        if not ok:
                            disp["kbd_error"] = msg.upper()
                            return True
                        disp["kbd_buf"] = ""; disp["kbd_error"] = ""
                        disp["mode"] = "fpl"
                        return True
                    elif target == "fpl_latlon_ident":
                        disp["fpl_new"]["ident"] = buf.upper()[:10]
                        disp["kbd_buf"] = ""; disp["kbd_error"] = ""
                        disp["mode"] = "fpl_latlon_entry"
                        return True
                    elif target in ("fpl_latlon_lat", "fpl_latlon_lon"):
                        axis = "lat" if target.endswith("lat") else "lon"
                        disp["fpl_new"][f"{axis}_str"] = buf
                        v, _err = _fpl_parse_latlon(buf, axis)
                        if v is not None:
                            disp["fpl_new"][axis] = v
                        disp["kbd_buf"] = ""; disp["kbd_error"] = ""
                        disp["mode"] = "fpl_latlon_entry"
                        return True
                    elif buf:
                        if disp["kbd_prev"] == "connectivity_setup":
                            disp["cs"][target] = buf
                            # Changing AHRS URL live-restarts the SSE stream
                            if target == "ahrs_url":
                                _restart_sse(buf)
                            # Entering a NOTAM cred pushes it to peer screens so
                            # the key is stored on every display, not just here.
                            if target in ("notam_client_id",
                                          "notam_client_secret", "notam_env"):
                                _ssync_push_notam_creds()
                        else:
                            disp["fp"][target] = buf
                        _settings.mark_dirty()
                    disp["kbd_buf"] = ""
                    disp["mode"] = disp["kbd_prev"]
            return True

        # ── Numpad taps ───────────────────────────────────────────────────
        if mode == "numpad":
            hit = numpad_hit(x, y)
            if hit:
                lbl, sty = hit
                target = disp["numpad_target"]
                _NP_MAX = {"baro_hpa": 4}   # targets needing >3 digits
                max_digits = _NP_MAX.get(target, 3)
                if sty == 'n':                # digit
                    if len(disp["numpad_buf"]) < max_digits:
                        disp["numpad_buf"] += lbl
                elif sty == 'del':            # backspace
                    disp["numpad_buf"] = disp["numpad_buf"][:-1]
                elif sty == 'x':              # CANCEL
                    disp["mode"] = disp["numpad_prev"]
                    disp["numpad_buf"] = ""
                elif sty == 'ok':             # ENTER
                    buf = disp["numpad_buf"]
                    if buf:
                        val = int(buf)
                        # Unit factors: user types values in whatever unit is
                        # currently displayed, but bugs are stored canonically
                        # (kt for speed, ft for altitude).  Divide by the
                        # factor to convert display → canonical.
                        ds = disp["ds"]
                        spd_factor = {"kt": 1.0, "mph": 1.15078,
                                      "kph": 1.852}.get(ds.get("spd_unit", "kt"), 1.0)
                        alt_factor = {"ft": 1.0,
                                      "m":  0.3048}.get(ds.get("alt_unit", "ft"), 1.0)
                        if target == "alt_bug":
                            # Input is hundreds of display-unit altitude.
                            disp["alt_bug"] = float(val * 100) / alt_factor
                            _ssync_publish_bugs()
                        elif target == "hdg_bug":
                            disp["hdg_bug"] = float(val % 360)
                            _ssync_publish_bugs()
                        elif target == "trk_bug":
                            disp["trk_bug"] = float(val % 360)
                            _ssync_publish_bugs()
                        elif target == "spd_bug":
                            disp["spd_bug"] = float(val) / spd_factor
                            _ssync_publish_bugs()
                        elif target == "baro_hpa":
                            baro_unit = ds.get("baro_unit", "inhg")
                            if baro_unit == "hpa":
                                new_hpa = float(val)
                            else:   # inHg: 4 digits → insert decimal after 2
                                new_hpa = round(val / 100.0 * 33.8639, 2)
                            # baro_hpa is a user-set value.  Write it into disp
                            # (immediate UI feedback), into state (so other
                            # readers see it) and push to the Pico W so the
                            # firmware recomputes altitude against the new QNH.
                            disp["baro_hpa"] = new_hpa
                            with _state_lock:
                                state["baro_hpa"] = new_hpa
                            _push_baro_to_pico(new_hpa)
                            _ssync_publish_baro()
                        elif target == "sim_init_alt":
                            disp["sim"]["init_alt"] = float(val * 100) / alt_factor
                        elif target == "sim_init_hdg":
                            disp["sim"]["init_hdg"] = float(val % 360)
                        elif target == "sim_init_spd":
                            # sim_init_spd title is explicitly "(kt)" so no
                            # conversion here — entered value is already kt.
                            disp["sim"]["init_spd"] = float(val)
                        elif target in disp["fp"]:   # V-speed field (always kt)
                            disp["fp"][target] = val
                        _settings.mark_dirty()
                    disp["mode"] = disp["numpad_prev"]
                    disp["numpad_buf"] = ""
            return True

        # ── PFD taps ──────────────────────────────────────────────────────
        # Tap on SIM button → open sim controls overlay (which has EXIT SIM)
        if _sim_state is not None and mode == "pfd":
            if (_SIM_EXIT_X <= x <= _SIM_EXIT_X + _SIM_EXIT_W and
                    _SIM_EXIT_Y <= y <= _SIM_EXIT_Y + _SIM_EXIT_H):
                disp["mode"] = "sim_controls"
                return True

        # Tap on the CDI strip → open keyboard for waypoint entry.  Strip
        # is rendered whenever GPS is fixed (with or without an active
        # waypoint), and the hit region matches the translucent backplate.
        nv = disp.get("nav", {})
        if mode == "pfd" and disp.get("gps_ok", False):
            _cdi_bar_w = max(140, int(DISPLAY_W * 0.20))
            _cdi_bar_y = HDG_Y - 50
            _cdi_l = CX - _cdi_bar_w // 2 - 18
            _cdi_r = CX + _cdi_bar_w // 2 + 18
            _cdi_t = _cdi_bar_y - 32
            _cdi_b = _cdi_bar_y + 12
            if _cdi_l <= x <= _cdi_r and _cdi_t <= y <= _cdi_b:
                # Tap the CDI → choose Direct-To or Flight Plan.
                _nav_pick_open()
                return True

        # Tap on the moving-map inset → cycle range one step.  Right
        # half zooms IN (smaller range), left half zooms OUT.  Left-tap
        # at the largest standard step rolls into AUTO when a direct-to
        # is active so the pilot can reach AUTO without diving into the
        # display-setup screen.  The top-right corner (where the TRK↑ /
        # N↑ label sits) is split off as its own hot-zone — tapping it
        # toggles orientation without changing the range.
        if (mode == "pfd" and _last_map_rect is not None
                and disp["ds"].get("map_enabled", False)):
            mrx, mry, mrw, mrh = _last_map_rect
            if mrx <= x <= mrx + mrw and mry <= y <= mry + mrh:
                # Orient-toggle corner: top-right slab, sized to comfortably
                # cover the TRK↑ / N↑ label without stealing usable area
                # from the zoom-in half.
                corner_w = max(46, mrw // 4)
                corner_h = max(22, mrh // 5)
                if (x >= mrx + mrw - corner_w and
                        y <= mry + corner_h):
                    cur_or = disp["ds"].get("map_orient", "trk")
                    disp["ds"]["map_orient"] = "nrth" if cur_or == "trk" else "trk"
                    _settings.mark_dirty()
                    return True
                # Bottom-left corner: cycle the WX / Airspace overlay (traffic
                # stays on).  Checked before the zoom halves so it doesn't also
                # change range.  Bottom-left is free (RNG label is top-left,
                # ETE is bottom-right).
                if (x <= mrx + corner_w and y >= mry + mrh - corner_h):
                    nxt = _map_overlay_cycle(disp["ds"])
                    print(f"[inset] overlay → {nxt}")
                    _settings.mark_dirty()
                    return True
                if _map_overlay_state(disp["ds"]) == "wnd":
                    # Top-left winds-altitude tap-target (matches the readout
                    # drawn under the range label) cycles the level; the rest
                    # of the inset still controls winds zoom on the L/R halves.
                    if x <= mrx + 54 and mry + 26 <= y <= mry + 46:
                        _mfd_cycle_winds_alt()
                        return True
                    _winds_zoom_step(-1 if x >= mrx + mrw / 2 else +1)
                    return True
                cur = int(disp["ds"].get("map_zoom_nm", 5))
                _has_d2 = bool((disp.get("nav") or {}).get("ident"))
                if x >= mrx + mrw / 2:
                    disp["ds"]["map_zoom_nm"] = _map_mod.zoom_in(cur)
                else:
                    disp["ds"]["map_zoom_nm"] = _map_mod.zoom_out(
                        cur, allow_auto=_has_d2)
                _settings.mark_dirty()
                return True

        # Tap on the PFD top readout ribbon (band between the bug boxes) →
        # configure its fields (its own picker, separate from the MFD strip).
        _rx0, _rx1, _ry0, _ry1 = _pfd_top_band()
        if _rx0 <= x <= _rx1 and _ry0 <= y <= _ry1:
            disp["mss_which"] = "pfd"
            disp["mode"] = "mfd_strip_setup"
            disp["mss_sel"] = 0
            return True

        # Tap on alt bug button (top of alt tape) — hit region extends to HDG_H
        # for easy touch even though the visual button only fills TAPE_TOP
        if ALT_X <= x <= DISPLAY_W and 0 <= y <= HDG_H:
            _open_numpad("alt_bug")
            return True
        # Tap on GS bug button (top of speed tape) — same extended hit region
        if SPD_X <= x <= SPD_X + SPD_W and 0 <= y <= HDG_H:
            _open_numpad("spd_bug")
            return True
        # Tap on speed VR readout (centre of speed tape) → open speed numpad
        if SPD_X <= x <= SPD_X + SPD_W and TAPE_MID - 30 <= y <= TAPE_MID + 30:
            _open_numpad("spd_bug")
            return True
        # Tap on alt VR readout (centre of alt tape) → open alt numpad
        if ALT_X <= x <= DISPLAY_W and TAPE_MID - 30 <= y <= TAPE_MID + 30:
            _open_numpad("alt_bug")
            return True
        # Tap on hdg bug button → open numpad (track bug if in TRK mode)
        if SPD_X <= x <= SPD_X + SPD_W and HDG_Y <= y <= DISPLAY_H:
            _open_numpad(_active_bug_key())
            return True
        # Tap on heading readout box (centre of heading tape) → open hdg numpad
        if CX - 40 <= x <= CX + 40 and HDG_Y - 40 <= y <= HDG_Y:
            _open_numpad(_active_bug_key())
            return True
        # Tap on baro button → open numpad
        if ALT_X <= x <= DISPLAY_W and HDG_Y <= y <= DISPLAY_H:
            _open_numpad("baro_hpa")
            return True
        # Tap on alt tape → adjust alt bug by position
        if ALT_X <= x <= DISPLAY_W and TAPE_TOP <= y <= TAPE_BOT:
            ft = round(disp["alt"] + (TAPE_MID - y) / PX_PER_FT)
            disp["alt_bug"] = round(ft / 100) * 100
            _ssync_publish_bugs()
        # Tap on heading tape → adjust active bug by position.  In TRK mode
        # the centre reference is GPS track (matching what the box shows),
        # so the bug lands under the finger relative to the displayed value.
        if HDG_Y <= y <= DISPLAY_H:
            off = (x - CX) / PX_PER_DEG
            _bk = _active_bug_key()
            _ref = disp.get("track", disp["yaw"]) if _bk == "trk_bug" else disp["yaw"]
            disp[_bk] = round(_ref + off) % 360
            _ssync_publish_bugs()

    return True


# ── Setup / numpad screens ────────────────────────────────────────────────────

_SETUP_ITEMS = [
    (0, 0, "FLIGHT PROFILE",  "V-speeds · Aircraft · Tail #"),
    (1, 0, "DISPLAY",         "Units · Brightness · Night mode"),
    (0, 1, "AHRS / SENSORS",  "Trim · Mag cal · Mounting"),
    (1, 1, "CONNECTIVITY",    "WiFi · AHRS link"),
    (0, 2, "SCREEN SYNC",     "Share bugs · baro · nav · AHRS"),
    (1, 2, "SYSTEM",          "Version · Diagnostics · Reset"),
    (0, 3, "EXIT",            "Return to PFD"),
    (1, 3, "DATA & MAPS",     "Terrain · Obstacles · Airports · Nav"),
]
_S_MX=15; _S_MY=50; _S_GX=10; _S_GY=10
_S_BW = (DISPLAY_W - 2*_S_MX - _S_GX) // 2
_S_BH = (DISPLAY_H - _S_MY - 14 - 3*_S_GY) // 4
_S_COLS = [_S_MX, _S_MX + _S_BW + _S_GX]
_S_ROWS = [_S_MY,
           _S_MY + _S_BH + _S_GY,
           _S_MY + 2*(_S_BH + _S_GY),
           _S_MY + 3*(_S_BH + _S_GY)]


def _setup_button(surf, bx, by, bw, bh, label, subtitle="", exit_btn=False, r=8):
    bg = (28, 6, 6) if exit_btn else (0, 12, 32)
    pygame.draw.rect(surf, bg, (bx, by, bw, bh), border_radius=r)
    glow_h = bh // 5
    for i in range(glow_h):
        t = 1.0 - i / glow_h
        gc = ((int(45+t*55), int(8+t*12),  int(8+t*12)) if exit_btn
              else (int(15+t*35), int(20+t*50), int(40+t*80)))
        pygame.draw.line(surf, gc, (bx+r, by+1+i), (bx+bw-r, by+1+i))
    oc = (200, 55, 55) if exit_btn else WHITE
    pygame.draw.rect(surf, oc, (bx, by, bw, bh), width=2, border_radius=r)
    lh = 22
    total_h = lh + (16 if subtitle else 0)
    ly = by + (bh - total_h) // 2
    _text(surf, label,    19, WHITE,         bold=True, cx=bx+bw//2, cy=ly+lh//2)
    if subtitle:
        _text(surf, subtitle, 11, (155,170,190),           cx=bx+bw//2, cy=ly+lh+10)


# ── Header BACK button — sized to comfortably fit the scaled label font ───────
# On 640×480 the 72 px width is fine; on 1024×600 (FONT_SCALE≈1.25) the arrow
# + "BACK" at 19pt bold exceeds 72 px, so we scale width with the font.
try:
    from config import FONT_SCALE as _FS_FOR_BACK
except ImportError:
    _FS_FOR_BACK = 1.0
_BACK_BX = 8
_BACK_BY = 6
_BACK_BW = max(72, int(72 * _FS_FOR_BACK + 0.5))
_BACK_BH = 31

# Bug tap-button height — match the heading tape height for easy touch
_BUG_BTN_H = HDG_H


def _draw_back_button(surf):
    _setup_button(surf, _BACK_BX, _BACK_BY, _BACK_BW, _BACK_BH,
                  "\u2190 BACK", r=5)


def _back_hit(x, y):
    return (_BACK_BX <= x <= _BACK_BX + _BACK_BW
            and _BACK_BY <= y <= _BACK_BY + _BACK_BH)


def draw_setup_screen(surf):
    """Full-screen setup main menu — entered via 2-finger hold."""
    surf.fill((0, 8, 22))
    pygame.draw.rect(surf, (0, 18, 45), (0, 0, DISPLAY_W, 44))
    pygame.draw.line(surf, WHITE, (0, 43), (DISPLAY_W-1, 43), 1)
    _text(surf, "SETUP", 22, WHITE, bold=True, cx=DISPLAY_W//2, cy=22)
    _text(surf, "2-finger hold to enter  ·  EXIT to return", 10, (110,120,140),
          x=DISPLAY_W-340, y=15)
    for col, row, lbl, sub in _SETUP_ITEMS:
        exit_btn = (lbl == "EXIT")
        _setup_button(surf, _S_COLS[col], _S_ROWS[row], _S_BW, _S_BH,
                      lbl, sub, exit_btn)


def setup_hit(x, y):
    """Return index of the tapped setup button (row-major), or None."""
    for idx, (col, row, *_) in enumerate(_SETUP_ITEMS):
        bx = _S_COLS[col]; by = _S_ROWS[row]
        if bx <= x <= bx+_S_BW and by <= y <= by+_S_BH:
            return idx
    return None


# ── DATA & MAPS page — one home for every downloadable dataset ───────────────
# Previously each dataset hung off a cramped 4-tile row at the bottom of the
# SYSTEM screen.  With NAV DATA there are five; they live here as roomy tiles,
# each routing to its existing management subscreen.
_DL_MX = 15; _DL_MY = 52; _DL_GX = 10; _DL_GY = 10
_DL_BW = (DISPLAY_W - 2*_DL_MX - _DL_GX) // 2
_DL_BH = (DISPLAY_H - _DL_MY - 12 - 2*_DL_GY) // 3
_DL_COLS = [_DL_MX, _DL_MX + _DL_BW + _DL_GX]
_DL_ROWS = [_DL_MY, _DL_MY + _DL_BH + _DL_GY, _DL_MY + 2*(_DL_BH + _DL_GY)]
_DL_ITEMS = [
    (0, 0, "TERRAIN",   "terrain_data"),
    (1, 0, "OBSTACLES", "obstacle_data"),
    (0, 1, "AIRPORTS",  "airport_data"),
    (1, 1, "AIRSPACE",  "airspace_data"),
    (0, 2, "NAV DATA",  "navdata_data"),
    (1, 2, "BACK",      None),
]


def _dl_status(key):
    """(sub-line, colour) summarising a dataset's on-disk state for its tile."""
    if key == "terrain_data":
        n, mb = _td_disk_stats()
        if n:
            return (f"{n} tile{'s' if n != 1 else ''}  ·  {mb:.1f} MB", (60,210,90))
        return ("Tap to download", YELLOW)
    if key == "obstacle_data":
        od = disp["od"]; c = od.get("records", 0)
        if c:
            if od.get("expired"):
                return (f"{c:,} obstacles  ·  ⚠ EXP", (220,140,60))
            return (f"{c:,} obstacles  ·  {od.get('used_mb',0.0):.1f} MB", (60,210,90))
        return ("Tap to download", YELLOW)
    if key == "airport_data":
        ad = disp["ad"]; c = ad.get("records", 0)
        if c:
            if ad.get("expired"):
                return (f"{c:,} airports  ·  ⚠ EXP", (220,140,60))
            return (f"{c:,} airports", (60,210,90))
        return ("Tap to download", YELLOW)
    if key == "airspace_data":
        c = disp.get("asp", {}).get("records", 0)
        if c:
            return (f"{c} polygons", (60,210,90))
        return ("Tap to set up", YELLOW)
    if key == "navdata_data":
        nd = disp["nd"]
        if nd.get("present"):
            sub = (f"cycle {nd.get('cycle') or '—'}  ·  "
                   f"{nd.get('procedures',0):,} appr")
            return (sub, (220,140,60) if nd.get("expired") else (60,210,90))
        return ("Tap to download", YELLOW)
    return ("", (150,160,175))


def _dl_tile(surf, bx, by, bw, bh, label, sub, sub_col, back=False):
    for i in range(bh):
        t = 1.0 - i / bh
        c = ((int(40+t*30), int(8+t*10), int(8+t*10)) if back
             else (int(t*10), int(14+t*22), int(30+t*42)))
        pygame.draw.line(surf, c, (bx, by+i), (bx+bw, by+i))
    oc = (200, 80, 80) if back else (60, 85, 120)
    pygame.draw.rect(surf, oc, (bx, by, bw, bh), width=2, border_radius=8)
    if back:
        _text(surf, "BACK", 18, WHITE, bold=True, cx=bx+bw//2, cy=by+bh//2)
        return
    _text(surf, label, 18, WHITE, bold=True, x=bx+16, y=by+16)
    _text(surf, sub, 12, sub_col, x=bx+16, y=by+bh-28)
    _text(surf, "▶", 16, (70,95,130), x=bx+bw-26, y=by+bh//2-10)


def draw_downloads_setup(surf):
    """Full-screen DATA & MAPS menu — tiles for every downloadable dataset."""
    surf.fill((0, 8, 22))
    pygame.draw.rect(surf, (0, 18, 45), (0, 0, DISPLAY_W, 44))
    pygame.draw.line(surf, WHITE, (0, 43), (DISPLAY_W-1, 43), 1)
    _text(surf, "‹ SETUP", 14, (150,170,200), x=12, y=15)
    _text(surf, "DATA & MAPS", 20, WHITE, bold=True, cx=DISPLAY_W//2, cy=22)
    for col, row, label, mode in _DL_ITEMS:
        bx = _DL_COLS[col]; by = _DL_ROWS[row]
        if mode is None:
            _dl_tile(surf, bx, by, _DL_BW, _DL_BH, label, "", WHITE, back=True)
        else:
            sub, sc = _dl_status(mode)
            _dl_tile(surf, bx, by, _DL_BW, _DL_BH, label, sub, sc)


def downloads_setup_hit(x, y):
    """Return ('back', None) | ('open', subscreen-mode) | (None, None)."""
    if y <= 44 and x <= 110:
        return ("back", None)
    for col, row, label, mode in _DL_ITEMS:
        bx = _DL_COLS[col]; by = _DL_ROWS[row]
        if bx <= x <= bx+_DL_BW and by <= y <= by+_DL_BH:
            return ("back", None) if mode is None else ("open", mode)
    return (None, None)


# Numpad constants — shared between draw and hit-test
_NP_KEYS = [
    [('7','n'), ('8','n'), ('9','n')],
    [('4','n'), ('5','n'), ('6','n')],
    [('1','n'), ('2','n'), ('3','n')],
    # Bottom row: 4 keys share the same 384-px row width as the 3-col rows above.
    # Each tuple's optional 3rd element overrides the default _NP_PW width.
    [('CANCEL','x',87), ('0','n',87), ('\u232b','del',87), ('ENTER','ok',87)],
]
_NP_PW=120; _NP_PH=64; _NP_GX=12; _NP_GY=10
_NP_TW = 3*_NP_PW + 2*_NP_GX   # 384
_NP_X0 = (DISPLAY_W - _NP_TW) // 2  # 128
_NP_Y0 = 118


def _np_row_layout(row):
    """Return [(label, style, bx, bw), ...] for one numpad row, centered.
    Keys may be 2-tuples (label, style) or 3-tuples (label, style, width);
    2-tuples default to _NP_PW."""
    widths = [(k[2] if len(k) >= 3 else _NP_PW) for k in row]
    total = sum(widths) + _NP_GX * (len(row) - 1)
    x = (DISPLAY_W - total) // 2
    out = []
    for i, k in enumerate(row):
        out.append((k[0], k[1], x, widths[i]))
        x += widths[i] + _NP_GX
    return out


def _numpad_key(surf, bx, by, bw, label, style, r=8):
    if style == 'x':
        bg=(28,6,6);  oc=(200,55,55); tc=(220,80,80)
    elif style == 'ok':
        bg=(5,25,10); oc=(50,200,80); tc=(60,220,90)
    elif style == 'del':
        bg=(30,18,5); oc=(200,140,40);tc=(220,160,50)
    else:
        bg=(0,12,32); oc=WHITE;       tc=WHITE
    pygame.draw.rect(surf, bg, (bx, by, bw, _NP_PH), border_radius=r)
    glow_h = _NP_PH // 5
    for i in range(glow_h):
        t = 1.0 - i / glow_h
        gc = ((int(45+t*55), int(8+t*12),  int(8+t*12))  if style=='x'  else
              (int(5+t*15),  int(40+t*60), int(10+t*20)) if style=='ok' else
              (int(40+t*50), int(25+t*35), int(5+t*10))  if style=='del' else
              (int(15+t*30), int(20+t*45), int(40+t*75)))
        pygame.draw.line(surf, gc, (bx+r, by+1+i), (bx+bw-r, by+1+i))
    pygame.draw.rect(surf, oc, (bx, by, bw, _NP_PH), width=2, border_radius=r)
    fs = 18 if len(label) > 3 else 20
    _text(surf, label, fs, tc, bold=True, cx=bx+bw//2, cy=by+_NP_PH//2)


def _fmt_decimal(digits: str, decimal_after: int) -> str:
    """Insert a decimal point into a digit string.
    '2992', decimal_after=2 → '29.92'
    '29',   decimal_after=2 → '29'   (still typing)
    """
    if decimal_after and len(digits) > decimal_after:
        return digits[:decimal_after] + "." + digits[decimal_after:]
    return digits


def draw_numpad(surf, title, current_val, entered="", suffix="",
                transparent=False, decimal_after=0):
    """Full-screen numeric entry pad.
    suffix:        appended in dim cyan (e.g. '00' for alt_bug).
    transparent:   skip background fill; semi-opaque header over live PFD.
    decimal_after: auto-insert '.' after this many digits (0 = no decimal).
                   current_val should be the integer form (e.g. 2992 for 29.92).
    """
    if not transparent:
        surf.fill((0, 8, 22))
    hdr = pygame.Surface((DISPLAY_W, 44), pygame.SRCALPHA)
    hdr.fill((0, 18, 45, 220 if transparent else 255))
    surf.blit(hdr, (0, 0))
    pygame.draw.line(surf, WHITE, (0, 43), (DISPLAY_W-1, 43), 1)
    _text(surf, title, 18, WHITE, bold=True, cx=DISPLAY_W//2, cy=22)

    # Value display box
    raw_str  = entered if entered else str(current_val)
    base_str = _fmt_decimal(raw_str, decimal_after) if decimal_after else raw_str
    pygame.draw.rect(surf, (0,15,38), (80, 50, DISPLAY_W-161, 50), border_radius=6)
    pygame.draw.rect(surf, WHITE,     (80, 50, DISPLAY_W-161, 50), width=1, border_radius=6)
    if suffix:
        f32 = _get_font(32, bold=True)
        bw  = f32.size(base_str)[0]
        sw  = f32.size(suffix)[0]
        bx_str = DISPLAY_W//2 - (bw + sw)//2
        surf.blit(f32.render(base_str, True, CYAN), (bx_str, 59))
        surf.blit(f32.render(suffix,   True, (0,100,100)), (bx_str + bw, 59))
    else:
        _text(surf, base_str, 32, CYAN, bold=True, cx=DISPLAY_W//2, cy=75)

    # "Current:" hint — format with decimal too
    cur_raw = _fmt_decimal(str(current_val), decimal_after) if decimal_after else str(current_val)
    cur_display = f"{cur_raw}{suffix}" if suffix else cur_raw
    _text(surf, f"Current: {cur_display}", 10, (110,120,140), cx=DISPLAY_W//2, cy=108)
    for ri, row in enumerate(_NP_KEYS):
        by = _NP_Y0 + ri*(_NP_PH+_NP_GY)
        for lbl, sty, bx, bw in _np_row_layout(row):
            _numpad_key(surf, bx, by, bw, lbl, sty)


def numpad_hit(x, y):
    """Return (label, style) of the tapped numpad key, or None."""
    for ri, row in enumerate(_NP_KEYS):
        by = _NP_Y0 + ri*(_NP_PH+_NP_GY)
        if not (by <= y <= by+_NP_PH):
            continue
        for lbl, sty, bx, bw in _np_row_layout(row):
            if bx <= x <= bx+bw:
                return (lbl, sty)
    return None


# ── Flight Profile screen ────────────────────────────────────────────────────

_FP_FIELDS = [
    ("tail",  "TAIL NUMBER",          "",   8, "kbd"),
    ("actype","AIRCRAFT TYPE",        "",   8, "kbd"),
    ("vs0",   "VS0 \u2014 Stall flaps",  "kt", 3, "num"),
    ("vs1",   "VS1 \u2014 Stall clean",  "kt", 3, "num"),
    ("vfe",   "VFE \u2014 Max flaps",    "kt", 3, "num"),
    ("vno",   "VNO \u2014 Max cruise",   "kt", 3, "num"),
    ("vne",   "VNE \u2014 Never exceed", "kt", 3, "num"),
    ("va",    "VA  \u2014 Manoeuvre",    "kt", 3, "num"),
    ("vy",    "VY  \u2014 Best rate",    "kt", 3, "num"),
    ("vx",    "VX  \u2014 Best angle",   "kt", 3, "num"),
]

_FP_MX=12; _FP_GAP=8; _FP_H1=58; _FP_H2=48; _FP_Y0=50


def _fp_field(surf, bx, by, bw, bh, label, value, units="", r=6):
    pygame.draw.rect(surf, (0, 12, 32), (bx, by, bw, bh), border_radius=r)
    glow_h = bh // 5
    for i in range(glow_h):
        t = 1.0 - i / glow_h
        gc = (int(15+t*35), int(20+t*50), int(40+t*80))
        pygame.draw.line(surf, gc, (bx+r, by+1+i), (bx+bw-r, by+1+i))
    pygame.draw.rect(surf, WHITE, (bx, by, bw, bh), width=2, border_radius=r)
    _text(surf, label, 11, (155,170,190), x=bx+10, y=by+6)
    val_str = str(value) if value not in (None, "", 0) else "---"
    if units and val_str != "---":
        val_str = f"{val_str} {units}"
    _text(surf, val_str, 18, WHITE, bold=True,
          cx=bx+bw - _get_font(18,bold=True).size(val_str)[0]//2 - 12,
          cy=by+bh//2)


def draw_flight_profile(surf, fp_vals):
    """Full-screen Flight Profile setup screen."""
    surf.fill((0, 8, 22))
    pygame.draw.rect(surf, (0, 18, 45), (0, 0, DISPLAY_W, 44))
    pygame.draw.line(surf, WHITE, (0, 43), (DISPLAY_W-1, 43), 1)
    _draw_back_button(surf)
    _text(surf, "FLIGHT PROFILE", 20, WHITE, bold=True, cx=DISPLAY_W//2, cy=22)

    MX=_FP_MX; GAP=_FP_GAP
    FW = DISPLAY_W - 2*MX

    # Aircraft info (full-width)
    y = _FP_Y0
    for key in ("tail", "actype"):
        _, label, units, _, _ = next(f for f in _FP_FIELDS if f[0]==key)
        _fp_field(surf, MX, y, FW, _FP_H1, label, fp_vals.get(key,"---"), units)
        y += _FP_H1 + GAP

    # Section divider
    y += 2
    pygame.draw.line(surf, (40,60,90), (MX, y), (DISPLAY_W-MX, y), 1)
    y += 4
    _text(surf, "V-SPEEDS  (knots) \u2014 tap to edit", 11, (120,140,165), x=MX, y=y)
    y += 18

    # V-speed grid: 4 rows × 2 cols
    V_KEYS = [k for k,*_ in _FP_FIELDS if k not in ("tail","actype")]
    BW = (FW - GAP) // 2
    BH = (DISPLAY_H - y - GAP*3 - 4) // 4
    COLS = [MX, MX+BW+GAP]
    for i, key in enumerate(V_KEYS):
        _, label, units, _, _ = next(f for f in _FP_FIELDS if f[0]==key)
        bx = COLS[i%2]; by = y + (i//2)*(BH+GAP)
        _fp_field(surf, bx, by, BW, BH, label, fp_vals.get(key,"---"), units)


def flight_profile_hit(x, y, fp_vals):
    """Return the field key tapped, or None."""
    MX=_FP_MX; GAP=_FP_GAP; FW=DISPLAY_W-2*MX
    # BACK button
    if _back_hit(x, y):
        return "__back__"
    # Aircraft fields
    fy = _FP_Y0
    for key in ("tail","actype"):
        if MX<=x<=MX+FW and fy<=y<=fy+_FP_H1:
            return key
        fy += _FP_H1+GAP
    # V-speed grid
    fy += 26   # divider + label
    V_KEYS = [k for k,*_ in _FP_FIELDS if k not in ("tail","actype")]
    BW = (FW-GAP)//2; BH = (DISPLAY_H-fy-GAP*3-4)//4
    COLS = [MX, MX+BW+GAP]
    for i, key in enumerate(V_KEYS):
        bx=COLS[i%2]; by=fy+(i//2)*(BH+GAP)
        if bx<=x<=bx+BW and by<=y<=by+BH:
            return key
    return None


# ── Keyboard screen ────────────────────────────────────────────────────────────

# Normal layout: uppercase letters + digits.
_KB_ROWS_NORMAL = [
    [('1',60,'n'),('2',60,'n'),('3',60,'n'),('4',60,'n'),('5',60,'n'),
     ('6',60,'n'),('7',60,'n'),('8',60,'n'),('9',60,'n'),('0',60,'n')],
    [('Q',60,'n'),('W',60,'n'),('E',60,'n'),('R',60,'n'),('T',60,'n'),
     ('Y',60,'n'),('U',60,'n'),('I',60,'n'),('O',60,'n'),('P',60,'n')],
    [('A',60,'n'),('S',60,'n'),('D',60,'n'),('F',60,'n'),('G',60,'n'),
     ('H',60,'n'),('J',60,'n'),('K',60,'n'),('L',60,'n')],
    [('Z',60,'n'),('X',60,'n'),('C',60,'n'),('V',60,'n'),('B',60,'n'),
     ('N',60,'n'),('M',60,'n'),('.',60,'n'),(':',60,'n'),('\u232b',60,'del')],
    # \u21e7 (shift arrow) replaces hyphen; SPACE narrowed 20 px to compensate.
    [('CANCEL',108,'x'),('\u21e7',80,'shift'),('SPACE',212,'n'),('ENTER',108,'ok')],
]
# Shift layout: lowercase letters + symbol row instead of digits.
_KB_ROWS_SHIFT = [
    [('!',60,'n'),('@',60,'n'),('#',60,'n'),('$',60,'n'),('%',60,'n'),
     ('^',60,'n'),('&',60,'n'),('*',60,'n'),('(',60,'n'),(')',60,'n')],
    [('q',60,'n'),('w',60,'n'),('e',60,'n'),('r',60,'n'),('t',60,'n'),
     ('y',60,'n'),('u',60,'n'),('i',60,'n'),('o',60,'n'),('p',60,'n')],
    [('a',60,'n'),('s',60,'n'),('d',60,'n'),('f',60,'n'),('g',60,'n'),
     ('h',60,'n'),('j',60,'n'),('k',60,'n'),('l',60,'n')],
    [('z',60,'n'),('x',60,'n'),('c',60,'n'),('v',60,'n'),('b',60,'n'),
     ('n',60,'n'),('m',60,'n'),('-',60,'n'),('_',60,'n'),('\u232b',60,'del')],
    [('CANCEL',108,'x'),('\u21e7',80,'shift_on'),('SPACE',212,'n'),('ENTER',108,'ok')],
]

def _current_kb_rows():
    return _KB_ROWS_SHIFT if disp.get('kbd_shift') else _KB_ROWS_NORMAL

_KB_ROW_H=66; _KB_GAP_Y=6; _KB_GAP_X=4; _KB_Y0=112

# Nav-ident extras row: two big action buttons below the QWERTY when the
# keyboard is open for waypoint entry.  Only rendered/hit-tested on
# displays tall enough to fit them — short displays (480 px) keep the
# bare keyboard and the pilot uses the usual CANCEL/DONE flow.
_KB_NAV_BTN_Y = _KB_Y0 + 5 * _KB_ROW_H + 4 * _KB_GAP_Y + 12
_KB_NAV_BTN_H = 70


def _kb_nav_extras_visible():
    return (disp.get("kbd_target") == "nav_ident"
            and DISPLAY_H >= _KB_NAV_BTN_Y + _KB_NAV_BTN_H + 6)


def _kb_nav_extras_geometry():
    """(x positions, btn_w) for the three nav-ident extras buttons:
    DIRECT TO NEAREST · CANCEL FLIGHT PLAN · APPR."""
    pad = 12
    gap = 8
    btn_w = (DISPLAY_W - 2 * pad - 2 * gap) // 3
    bx_l = pad
    bx_m = pad + btn_w + gap
    bx_r = pad + 2 * (btn_w + gap)
    return bx_l, bx_m, bx_r, btn_w


def _kb_row_x0(row):
    total = sum(w for _,w,_ in row) + _KB_GAP_X*(len(row)-1)
    return (DISPLAY_W - total) // 2


def _kb_key(surf, bx, by, bw, bh, label, style, r=6):
    if style=='x':
        bg=(28,6,6);  oc=(200,55,55); tc=(220,80,80)
    elif style=='ok':
        bg=(5,25,10); oc=(50,200,80); tc=(60,220,90)
    elif style=='del':
        bg=(30,18,5); oc=(200,140,40);tc=(220,160,50)
    elif style=='shift':
        bg=(10,20,45); oc=(90,130,200); tc=(110,160,230)
    elif style=='shift_on':
        bg=(45,32,5);  oc=(220,160,40); tc=(255,200,60)
    else:
        bg=(0,12,32); oc=WHITE;       tc=WHITE
    pygame.draw.rect(surf, bg, (bx,by,bw,bh), border_radius=r)
    glow_h = bh//5
    for i in range(glow_h):
        t = 1.0-i/glow_h
        gc=((int(45+t*55),int(8+t*12),int(8+t*12))  if style=='x'  else
            (int(5+t*15),int(40+t*60),int(10+t*20)) if style=='ok' else
            (int(40+t*50),int(25+t*35),int(5+t*10)) if style=='del'else
            (int(15+t*30),int(20+t*45),int(40+t*75)))
        pygame.draw.line(surf, gc, (bx+r,by+1+i),(bx+bw-r,by+1+i))
    pygame.draw.rect(surf, oc, (bx,by,bw,bh), width=2, border_radius=r)
    fs = 13 if len(label)>2 else 18
    _text(surf, label, fs, tc, bold=True, cx=bx+bw//2, cy=by+bh//2)


def draw_keyboard(surf, title, current_val, entered="", transparent=False,
                  error="", hint=""):
    """Full-screen QWERTY keyboard for text entry.  `hint` (e.g. a resolved
    airport name) shows green under the input when there's no error."""
    if not transparent:
        surf.fill((0,8,22))
    hdr = pygame.Surface((DISPLAY_W, 44), pygame.SRCALPHA)
    hdr.fill((0, 18, 45, 220 if transparent else 255))
    surf.blit(hdr, (0, 0))
    pygame.draw.line(surf,WHITE,(0,43),(DISPLAY_W-1,43),1)
    _text(surf,title,17,WHITE,bold=True,cx=DISPLAY_W//2,cy=22)
    disp_str = (entered if entered else str(current_val)) + "\u2502"
    # Input box shrunk a touch so the live resolved label below it reads clearly.
    pygame.draw.rect(surf,(0,15,38),(10,50,DISPLAY_W-21,42),border_radius=6)
    pygame.draw.rect(surf,WHITE,(10,50,DISPLAY_W-21,42),width=1,border_radius=6)
    _text(surf,disp_str,26,CYAN,bold=True,cx=DISPLAY_W//2,cy=71)
    if error:
        # Error overrides the hint so the pilot's eye lands on the problem.
        _text(surf, error, 13, (255, 90, 90), bold=True,
              cx=DISPLAY_W//2, cy=102)
    elif hint:
        # Live resolved label (the airport's LID/ICAO + name) so the pilot can
        # confirm the field before ENTER.
        _text(surf, hint, 17, (90, 220, 130), bold=True,
              cx=DISPLAY_W//2, cy=102)
    else:
        _text(surf, f"Current: {current_val}", 10, (110, 120, 140),
              cx=DISPLAY_W//2, cy=102)
    y = _KB_Y0
    for row in _current_kb_rows():
        x = _kb_row_x0(row)
        for label,kw,style in row:
            _kb_key(surf,x,y,kw,_KB_ROW_H,label,style)
            x += kw+_KB_GAP_X
        y += _KB_ROW_H+_KB_GAP_Y

    # Nav-ident extras: jump straight to NEAREST, wipe the active flight
    # plan, or open the approach runway picker — all without typing.
    # Only on tall enough displays.
    if _kb_nav_extras_visible():
        bx_l, bx_m, bx_r, btn_w = _kb_nav_extras_geometry()
        # Resolve the nearest ident so the pilot sees what they're
        # about to activate before tapping.  Empty string falls back
        # to the generic label when there's no fix / no airports.
        nrst = _nav_lookup_nearest()
        nrst_lbl = f"DIRECT TO {nrst}" if nrst else "DIRECT TO NEAREST"
        _action_btn(surf, bx_l, _KB_NAV_BTN_Y, btn_w, _KB_NAV_BTN_H,
                    nrst_lbl, "ok")
        _action_btn(surf, bx_m, _KB_NAV_BTN_Y, btn_w, _KB_NAV_BTN_H,
                    "CANCEL FLIGHT PLAN", "danger")
        _action_btn(surf, bx_r, _KB_NAV_BTN_Y, btn_w, _KB_NAV_BTN_H,
                    "APPR", "normal")


def keyboard_hit(x, y):
    """Return (label, style) of the tapped key, or None.

    Style 'nrst' / 'clrfp' are synthetic — emitted by the nav-ident
    extras buttons (not part of _KB_ROWS).  The dispatcher in the main
    event loop handles them by activating the nearest airport or
    clearing the active waypoint, then closing the keyboard."""
    if (_kb_nav_extras_visible()
            and _KB_NAV_BTN_Y <= y <= _KB_NAV_BTN_Y + _KB_NAV_BTN_H):
        bx_l, bx_m, bx_r, btn_w = _kb_nav_extras_geometry()
        if bx_l <= x <= bx_l + btn_w:
            return ("NRST", "nrst")
        if bx_m <= x <= bx_m + btn_w:
            return ("CLRFP", "clrfp")
        if bx_r <= x <= bx_r + btn_w:
            return ("APPR", "appr")
    ky = _KB_Y0
    for row in _current_kb_rows():
        if ky <= y <= ky+_KB_ROW_H:
            kx = _kb_row_x0(row)
            for label,kw,style in row:
                if kx <= x <= kx+kw:
                    return (label, style)
                kx += kw+_KB_GAP_X
        ky += _KB_ROW_H+_KB_GAP_Y
    return None


# ── Sub-setup screens (Display · AHRS · Connectivity · System) ───────────────

_SS_MX  = 12     # side margin
_SS_Y0  = 52     # first row top (44px title bar + 8px gap)
_SS_RH  = 62     # row height
_SS_GAP = 6      # gap between rows

# Per-setup-screen scroll offsets (in pixels). Keyed by disp["mode"].
# Used when a screen's content exceeds the available vertical area below
# the title bar.
_ss_scroll = {}

_SS_TITLE_BAR_H = 44

# Drag-to-scroll state. Set on MOUSEBUTTONDOWN/FINGERDOWN inside a drag-
# capable setup screen, cleared on UP. Motion exceeding _SS_DRAG_THRESHOLD
# converts the touch from "tap" to "drag".
_ss_drag = None
_SS_DRAG_THRESHOLD = 8
_SS_DRAG_MODES = {         # mode → n_rows (used to clamp max scroll)
    "ahrs_setup":         10,
    "system_setup":       9,
    "connectivity_setup": 9,
    "flight_profile":     8,
    "ahrs_firmware":      5,
    "screen_sync_setup": 10,    # enable + transport + peer + ifaces + 6 categories
}
_dispatch_replay = False


def _ss_row_y(i):
    base = _SS_Y0 + i * (_SS_RH + _SS_GAP)
    return base - _ss_scroll.get(disp.get("mode", ""), 0)


def _ss_content_h(n_rows):
    return _SS_Y0 + n_rows * (_SS_RH + _SS_GAP) - _SS_GAP


def _ss_max_scroll(n_rows):
    visible = DISPLAY_H - _SS_TITLE_BAR_H
    return max(0, _ss_content_h(n_rows) - _SS_TITLE_BAR_H - visible)


def _ss_clip_to_content(surf):
    """Clip drawing to the area below the title bar.  Returns the previous
    clip so the caller can restore it via surf.set_clip(prev)."""
    prev = surf.get_clip()
    surf.set_clip(pygame.Rect(0, _SS_TITLE_BAR_H,
                              DISPLAY_W, DISPLAY_H - _SS_TITLE_BAR_H))
    return prev


def _ss_reset_scroll(mode):
    _ss_scroll.pop(mode, None)


def _screen_header(surf, title):
    surf.fill((0, 8, 22))
    pygame.draw.rect(surf, (0, 18, 45), (0, 0, DISPLAY_W, 44))
    pygame.draw.line(surf, WHITE, (0, 43), (DISPLAY_W-1, 43), 1)
    _draw_back_button(surf)
    _text(surf, title, 20, WHITE, bold=True, cx=DISPLAY_W//2, cy=22)


def _setting_row(surf, row_i, label, sub="", _y_override=None):
    """Draw settings row background + label. Returns (bx, by, bw, bh)."""
    bx = _SS_MX; by = _y_override if _y_override is not None else _ss_row_y(row_i)
    bw = DISPLAY_W - 2*_SS_MX; bh = _SS_RH
    pygame.draw.rect(surf, (0, 12, 32), (bx, by, bw, bh), border_radius=6)
    gh = bh // 6
    for i in range(gh):
        t = 1.0 - i/gh
        gc = (int(15+t*25), int(20+t*40), int(40+t*65))
        pygame.draw.line(surf, gc, (bx+6, by+1+i), (bx+bw-6, by+1+i))
    pygame.draw.rect(surf, (55, 75, 105), (bx, by, bw, bh), width=1, border_radius=6)
    _text(surf, label, 14, WHITE, bold=True, x=bx+14, y=by+10)
    if sub:
        _text(surf, sub, 10, (120, 135, 155), x=bx+14, y=by+32)
    return bx, by, bw, bh


def _seg_btn(surf, bx, by, bw, bh, label, active, r=5):
    """Segmented-control button — CYAN highlight when active."""
    bg = (0, 55, 65) if active else (0, 10, 25)
    oc = CYAN        if active else (50, 68, 92)
    tc = CYAN        if active else (130, 148, 168)
    pygame.draw.rect(surf, bg, (bx, by, bw, bh), border_radius=r)
    if active:
        gh = bh // 4
        for i in range(gh):
            t = 1.0 - i/gh
            gc = (int(t*20), int(60+t*40), int(70+t*50))
            pygame.draw.line(surf, gc, (bx+r, by+1+i), (bx+bw-r, by+1+i))
    pygame.draw.rect(surf, oc, (bx, by, bw, bh), width=2, border_radius=r)
    _text(surf, label, 14, tc, bold=active, cx=bx+bw//2, cy=by+bh//2)


def _step_btn(surf, bx, by, bw, bh, label):
    """+/- stepper button."""
    if label == "+":
        bg=(8,28,12); oc=(50,180,70); tc=(70,220,90)
    else:
        bg=(30,12,12); oc=(180,50,50); tc=(220,80,80)
    pygame.draw.rect(surf, bg, (bx, by, bw, bh), border_radius=5)
    pygame.draw.rect(surf, oc, (bx, by, bw, bh), width=2, border_radius=5)
    _text(surf, label, 20, tc, bold=True, cx=bx+bw//2, cy=by+bh//2)


def _action_btn(surf, bx, by, bw, bh, label, style="normal", r=6):
    """Standalone action button (normal / ok / warn / danger)."""
    if style == "danger":
        bg=(35,5,5);   oc=(200,40,40);  tc=RED
    elif style == "warn":
        bg=(30,20,5);  oc=(200,140,40); tc=YELLOW
    elif style == "ok":
        bg=(5,28,10);  oc=(40,180,60);  tc=(60,220,80)
    else:
        bg=(0,18,45);  oc=WHITE;        tc=WHITE
    pygame.draw.rect(surf, bg, (bx, by, bw, bh), border_radius=r)
    gh = bh // 5
    for i in range(gh):
        t = 1.0 - i/gh
        if   style == "danger": gc=(int(bg[0]+t*40),int(bg[1]+t*10),int(bg[2]+t*10))
        elif style == "warn":   gc=(int(bg[0]+t*35),int(bg[1]+t*25),int(bg[2]+t*5))
        elif style == "ok":     gc=(int(bg[0]+t*10),int(bg[1]+t*35),int(bg[2]+t*10))
        else:                   gc=(int(bg[0]+t*15),int(bg[1]+t*25),int(bg[2]+t*50))
        pygame.draw.line(surf, gc, (bx+r, by+1+i), (bx+bw-r, by+1+i))
    pygame.draw.rect(surf, oc, (bx, by, bw, bh), width=2, border_radius=r)
    _text(surf, label, 15, tc, bold=True, cx=bx+bw//2, cy=by+bh//2)


# ── Display settings screen ───────────────────────────────────────────────────

_DSP_BTN_H = 40    # control button height inside row
_DSP_BTN_G = 6     # gap between buttons
_DSP_SW    = 40    # stepper +/- button width
_DSP_VW    = 70    # stepper value-display box width

# Rows are grouped into three tabbed sub-pages (UNITS / DISPLAY / MAP) so
# the screen fits without scrolling.  Each tab's rows are drawn by LOCAL
# index via _dsp_row_y(); the MAP tab additionally carries the tall
# MAP LAYERS multi-toggle after its standard rows.
#   (key, label, sub, opts_vals, opts_labels, btn_w)   None -> stepper
_DSP_ROWS_UNITS = [
    ("spd_unit",   "SPEED UNITS",  "Knots · Miles · Km/h",
     ["kt","mph","kph"], ["KT","MPH","KPH"], 80),
    ("alt_unit",   "ALTITUDE",     "Feet or Metres",
     ["ft","m"],         ["FT","M"],         100),
    ("baro_unit",  "PRESSURE",     "Inches Hg or hPa",
     ["inhg","hpa"],     ["inHg","hPa"],     100),
]
_DSP_ROWS_DISPLAY = [
    ("brightness", "BRIGHTNESS",   "Screen brightness 1–10",
     None, None, None),
    ("audio_enabled","ALERT AUDIO", "Voice callouts (TERRAIN / PULL UP / BANK)",
     [False, True],      ["OFF", "ON"],       80),
    ("audio_volume","ALERT VOLUME", "Callout volume 1–10",
     None, None, None),
    ("sun_realtime","SUN POSITION", "Real-time from UTC + GPS",
     [False, True],      ["FIXED", "REAL"],   80),
    ("fpv_enabled", "FLIGHT PATH", "Velocity vector / flight-path marker on AI",
     [False, True],      ["OFF", "ON"],       80),
    ("hits_enabled", "HITS BOXES", "Highway-in-the-sky approach corridor",
     [False, True],      ["OFF", "ON"],       80),
]
_DSP_ROWS_MAP = [
    # MAP INSET row carries TWO segmented controls: enable + orientation.
    # Custom drawing/hit-test below handles the second pair.  Listed here
    # as a single key so it occupies one row in the standard loop.
    ("map_enabled", "MAP INSET",    "Lower-left 2D moving map · orient",
     [False, True],      ["OFF", "ON"],       80),
    ("map_zoom_nm", "MAP RANGE",    "Default radius (nm) · AUTO fits D2",
     [1, 2, 5, 10, 20, 40, 80, 160, 0],
     ["1","2","5","10","20","40","80","160","AUTO"], 50),
    ("winds_alt_ft", "WINDS ALT",   "Winds-aloft level for the WND overlay",
     [3000, 6000, 9000, 12000, 18000],
     ["3k", "6k", "9k", "12k", "18k"], 56),
    ("traffic_alt_band", "TFC ALT",  "Hide traffic beyond ± band",
     [0, 2000, 5000, 10000], ["ALL", "±2k", "±5k", "±10k"], 64),
    ("traffic_range_nm", "TFC RANGE", "Hide traffic beyond range (nm)",
     [0, 5, 10, 20, 40], ["ALL", "5", "10", "20", "40"], 56),
]
_DSP_TAB_LABELS = ["UNITS", "DISPLAY", "MAP"]
_DSP_TAB_ROWS   = [_DSP_ROWS_UNITS, _DSP_ROWS_DISPLAY, _DSP_ROWS_MAP]
_DSP_MAP_TAB_INDEX = 2          # MAP LAYERS appears only on this tab
# Trailing controls for the MAP INSET row (orientation), drawn to the
# left of the standard segmented control by hand.
_DSP_MAP_ORIENT_OPTS  = ["trk", "nrth"]
_DSP_MAP_ORIENT_LBLS  = ["TRK\u2191", "N\u2191"]
_DSP_MAP_ORIENT_BW    = 80
_DSP_MAP_ORIENT_GAP   = 24    # gap between orient pair and on/off pair

# Multi-toggle MAP LAYERS row \u2014 pills wrap to two sub-rows so the
# right edge doesn't clobber the row label.  Same pattern piZ uses.
# Drawn separately from the MAP tab's standard rows because the standard
# row schema is one control per row.
_DSP_MAP_LAYERS = [
    ("map_show_terrain",      "TER"),
    ("map_show_water",        "WTR"),
    ("map_show_airports",     "APT"),
    ("map_show_runways",      "RWY"),
    ("map_show_obstacles",    "OBS"),
    ("map_show_traffic",      "TFC"),
    ("map_show_metar",        "MET"),
    ("map_show_nexrad",       "NEX"),
    ("map_show_state_lines",  "STA"),
    ("map_show_country_lines","CTRY"),
    ("map_show_airspaces",    "ASP"),
]
_DSP_LAYERS_PER_SUBROW = 4
_DSP_LAYERS_ROW_LOCAL_INDEX = len(_DSP_ROWS_MAP)
_DSP_LAYERS_BTN_W      = 84
_DSP_LAYERS_BTN_G      = 8
_DSP_LAYERS_BTN_H      = 44
_DSP_LAYERS_SUB_GAP    = 8
_DSP_LAYERS_ROW_H      = (2 * _DSP_LAYERS_BTN_H
                          + _DSP_LAYERS_SUB_GAP + 16)


def _dsp_layers_subrow_count():
    n = len(_DSP_MAP_LAYERS)
    return 1 if n <= _DSP_LAYERS_PER_SUBROW else 2


def _dsp_layers_subrow_split():
    n = len(_DSP_MAP_LAYERS)
    if n <= _DSP_LAYERS_PER_SUBROW:
        return list(range(n)), []
    return (list(range(_DSP_LAYERS_PER_SUBROW)),
            list(range(_DSP_LAYERS_PER_SUBROW, n)))


def _dsp_layers_subrow_y(by, subrow_idx):
    n_sub = _dsp_layers_subrow_count()
    total_h = n_sub * _DSP_LAYERS_BTN_H + (n_sub - 1) * _DSP_LAYERS_SUB_GAP
    y0 = by + max(8, (_DSP_LAYERS_ROW_H - total_h) // 2)
    return y0 + subrow_idx * (_DSP_LAYERS_BTN_H + _DSP_LAYERS_SUB_GAP)


def _dsp_rx(row, bx, bw):
    """Left x of control group (right-aligned, 14 px margin)."""
    *_, opts_v, opts_l, bw_each = row
    if opts_v is None:
        total = _DSP_SW + _DSP_BTN_G + _DSP_VW + _DSP_BTN_G + _DSP_SW
    else:
        total = len(opts_v)*bw_each + (len(opts_v)-1)*_DSP_BTN_G
    return bx + bw - total - 14


def _dsp_layers_geom(bx, bw, subrow_idx=0):
    """Right-aligned x for the first pill of the given sub-row."""
    top, bot = _dsp_layers_subrow_split()
    n = len(top) if subrow_idx == 0 else len(bot)
    if n == 0:
        return bx + bw
    total = n * _DSP_LAYERS_BTN_W + (n - 1) * _DSP_LAYERS_BTN_G
    return bx + bw - total - 14


# -- Tabbed sub-pages (UNITS / DISPLAY / MAP) ---------------------------------
_DSP_TAB_BAR_Y  = _SS_TITLE_BAR_H + 6      # just below the header rule
_DSP_TAB_BAR_H  = 34
_DSP_TAB_GAP    = 6
_DSP_CONTENT_Y0 = _DSP_TAB_BAR_Y + _DSP_TAB_BAR_H + 8


def _dsp_row_y(local_i):
    """Top-of-row y for a row at LOCAL index within the active tab, offset
    below the tab bar.  No scroll -- every tab fits on screen."""
    return _DSP_CONTENT_Y0 + local_i * (_SS_RH + _SS_GAP)


def _dsp_tab_geom(i):
    n = len(_DSP_TAB_LABELS)
    total_w = DISPLAY_W - 2 * _SS_MX
    seg_w = (total_w - (n - 1) * _DSP_TAB_GAP) // n
    return _SS_MX + i * (seg_w + _DSP_TAB_GAP), seg_w


def _draw_dsp_tabs(surf, active):
    for i, lbl in enumerate(_DSP_TAB_LABELS):
        bx, seg_w = _dsp_tab_geom(i)
        _seg_btn(surf, bx, _DSP_TAB_BAR_Y, seg_w, _DSP_TAB_BAR_H, lbl,
                 i == active)


def _dsp_tab_hit(x, y):
    if not (_DSP_TAB_BAR_Y <= y <= _DSP_TAB_BAR_Y + _DSP_TAB_BAR_H):
        return None
    for i in range(len(_DSP_TAB_LABELS)):
        bx, seg_w = _dsp_tab_geom(i)
        if bx <= x <= bx + seg_w:
            return i
    return None


def draw_display_setup(surf, ds):
    _screen_header(surf, "DISPLAY")
    tab = disp.get("dsp_tab", 0)
    _draw_dsp_tabs(surf, tab)
    _prev_clip = _ss_clip_to_content(surf)
    rows = _DSP_TAB_ROWS[tab]
    for li, row in enumerate(rows):
        key, label, sub, opts_v, opts_l, bw_each = row
        bx, by, bw, bh = _setting_row(surf, li, label, sub,
                                      _y_override=_dsp_row_y(li))
        ry = by + (bh - _DSP_BTN_H) // 2
        rx = _dsp_rx(row, bx, bw)
        if opts_v is None:                              # 1-10 stepper row
            val = ds.get(key, 8)
            _step_btn(surf, rx, ry, _DSP_SW, _DSP_BTN_H, "−")
            vx = rx + _DSP_SW + _DSP_BTN_G
            pygame.draw.rect(surf, (0,18,38), (vx, ry, _DSP_VW, _DSP_BTN_H), border_radius=4)
            pygame.draw.rect(surf, (60,80,110), (vx, ry, _DSP_VW, _DSP_BTN_H), width=1, border_radius=4)
            _text(surf, str(val), 18, WHITE, bold=True, cx=vx+_DSP_VW//2, cy=ry+_DSP_BTN_H//2)
            _step_btn(surf, vx+_DSP_VW+_DSP_BTN_G, ry, _DSP_SW, _DSP_BTN_H, "+")
        else:                                           # segmented control
            cur = ds.get(key, opts_v[0])
            for i, (v, lbl) in enumerate(zip(opts_v, opts_l)):
                _seg_btn(surf, rx+i*(bw_each+_DSP_BTN_G), ry, bw_each, _DSP_BTN_H, lbl, v==cur)
            # MAP INSET row also carries the orient pair to its left
            if key == "map_enabled":
                cur_or = ds.get("map_orient", "trk")
                ox = rx - (_DSP_MAP_ORIENT_GAP
                           + 2 * _DSP_MAP_ORIENT_BW + _DSP_BTN_G)
                for i, (v, lbl) in enumerate(zip(_DSP_MAP_ORIENT_OPTS,
                                                 _DSP_MAP_ORIENT_LBLS)):
                    _seg_btn(surf,
                             ox + i * (_DSP_MAP_ORIENT_BW + _DSP_BTN_G),
                             ry, _DSP_MAP_ORIENT_BW, _DSP_BTN_H,
                             lbl, v == cur_or)

    # MAP LAYERS -- packed multi-toggle row, MAP tab only.  Pills wrap to
    # two sub-rows so the right edge stays clear of the row label.  Drawn
    # manually (not via _setting_row) since it needs the taller height.
    if tab == _DSP_MAP_TAB_INDEX:
        bx = _SS_MX
        bw = DISPLAY_W - 2 * _SS_MX
        by = _dsp_row_y(_DSP_LAYERS_ROW_LOCAL_INDEX)
        bh = _DSP_LAYERS_ROW_H
        pygame.draw.rect(surf, (0, 12, 32), (bx, by, bw, bh), border_radius=6)
        gh = bh // 6
        for i in range(gh):
            t = 1.0 - i / gh
            gc = (int(15 + t * 25), int(20 + t * 40), int(40 + t * 65))
            pygame.draw.line(surf, gc, (bx + 6, by + 1 + i),
                              (bx + bw - 6, by + 1 + i))
        pygame.draw.rect(surf, (55, 75, 105), (bx, by, bw, bh),
                         width=1, border_radius=6)
        _text(surf, "MAP LAYERS", 14, WHITE, bold=True, x=bx + 14, y=by + 10)
        _text(surf, "Per-layer visibility on the map inset",
              10, (120, 135, 155), x=bx + 14, y=by + 32)
        top_idx, bot_idx = _dsp_layers_subrow_split()
        for sub, indices in enumerate((top_idx, bot_idx)):
            if not indices:
                continue
            ry = _dsp_layers_subrow_y(by, sub)
            rx = _dsp_layers_geom(bx, bw, sub)
            for slot, i in enumerate(indices):
                key, lbl = _DSP_MAP_LAYERS[i]
                active = bool(ds.get(key, True))
                _seg_btn(surf,
                         rx + slot * (_DSP_LAYERS_BTN_W + _DSP_LAYERS_BTN_G),
                         ry, _DSP_LAYERS_BTN_W, _DSP_LAYERS_BTN_H, lbl, active)
    surf.set_clip(_prev_clip)


def display_setup_hit(x, y, ds):
    """Return action string or None."""
    if _back_hit(x, y):
        return "back"
    t = _dsp_tab_hit(x, y)
    if t is not None:
        return f"tab:{t}"
    tab = disp.get("dsp_tab", 0)
    rows = _DSP_TAB_ROWS[tab]
    for li, row in enumerate(rows):
        key, *_, opts_v, opts_l, bw_each = row
        by = _dsp_row_y(li)
        if not (by <= y <= by+_SS_RH):
            continue
        bx = _SS_MX; bw = DISPLAY_W - 2*_SS_MX
        ry = by + (_SS_RH - _DSP_BTN_H) // 2
        rx = _dsp_rx(row, bx, bw)
        if not (ry <= y <= ry+_DSP_BTN_H):
            continue
        if opts_v is None:
            if rx <= x <= rx+_DSP_SW:
                return f"inc:{key}:-1"
            plus_x = rx + _DSP_SW + _DSP_BTN_G + _DSP_VW + _DSP_BTN_G
            if plus_x <= x <= plus_x+_DSP_SW:
                return f"inc:{key}:1"
        else:
            for i, v in enumerate(opts_v):
                bx_b = rx + i*(bw_each+_DSP_BTN_G)
                if bx_b <= x <= bx_b+bw_each:
                    return f"set:{key}:{v}"
            # MAP INSET row carries the orient pair to its left
            if key == "map_enabled":
                ox = rx - (_DSP_MAP_ORIENT_GAP
                           + 2 * _DSP_MAP_ORIENT_BW + _DSP_BTN_G)
                for i, v in enumerate(_DSP_MAP_ORIENT_OPTS):
                    bx_b = ox + i * (_DSP_MAP_ORIENT_BW + _DSP_BTN_G)
                    if bx_b <= x <= bx_b + _DSP_MAP_ORIENT_BW:
                        return f"set:map_orient:{v}"

    if tab == _DSP_MAP_TAB_INDEX:
        by = _dsp_row_y(_DSP_LAYERS_ROW_LOCAL_INDEX)
        if by <= y <= by + _DSP_LAYERS_ROW_H:
            bx = _SS_MX; bw = DISPLAY_W - 2 * _SS_MX
            top_idx, bot_idx = _dsp_layers_subrow_split()
            for sub, indices in enumerate((top_idx, bot_idx)):
                if not indices:
                    continue
                ry = _dsp_layers_subrow_y(by, sub)
                if not (ry <= y <= ry + _DSP_LAYERS_BTN_H):
                    continue
                rx = _dsp_layers_geom(bx, bw, sub)
                for slot, i in enumerate(indices):
                    key, _lbl = _DSP_MAP_LAYERS[i]
                    bx_b = rx + slot * (_DSP_LAYERS_BTN_W + _DSP_LAYERS_BTN_G)
                    if bx_b <= x <= bx_b + _DSP_LAYERS_BTN_W:
                        new_val = not bool(ds.get(key, True))
                        return f"set:{key}:{new_val}"
    return None


# ── AHRS / Sensors screen ─────────────────────────────────────────────────────

_SS_TRIM_SW = 40   # stepper button width
_SS_TRIM_VW = 90   # trim value box width
_SS_TRIM_H  = 40   # stepper/control height
_SS_TRIM_G  = 6    # gap

_SS_MAG_LABELS = {
    "idle":    ("IDLE",       (100,110,130)),
    "running": ("RUNNING\u2026", YELLOW),
    "done":    ("DONE  \u2713",  (50,200,80)),
    "error":   ("ERROR",      RED),
}


def _trim_stepper(surf, bx, by, bw, bh, val, key):
    """Draw [-][val°][+] stepper right-aligned in a settings row."""
    total = _SS_TRIM_SW + _SS_TRIM_G + _SS_TRIM_VW + _SS_TRIM_G + _SS_TRIM_SW
    rx = bx + bw - total - 14
    ry = by + (bh - _SS_TRIM_H) // 2
    _step_btn(surf, rx, ry, _SS_TRIM_SW, _SS_TRIM_H, "\u2212")
    vx = rx + _SS_TRIM_SW + _SS_TRIM_G
    pygame.draw.rect(surf, (0,18,38), (vx, ry, _SS_TRIM_VW, _SS_TRIM_H), border_radius=4)
    pygame.draw.rect(surf, (60,80,110), (vx, ry, _SS_TRIM_VW, _SS_TRIM_H), width=1, border_radius=4)
    _text(surf, f"{val:+.1f}\u00b0", 16, WHITE, bold=True,
          cx=vx+_SS_TRIM_VW//2, cy=ry+_SS_TRIM_H//2)
    _step_btn(surf, vx+_SS_TRIM_VW+_SS_TRIM_G, ry, _SS_TRIM_SW, _SS_TRIM_H, "+")
    return rx   # leftmost x of stepper (for hit detection)


# ── Approach selection screen (HITS / synthetic approaches) ──────────────────
#
# Pilot taps APPR on the CDI strip → opens this screen.  Lists runway
# ends for the airport currently held in disp["nav"]["ident"]; a tap on
# any end activates HITS to that threshold and retargets the direct-to.

_APR_HEADER_Y = 64       # below the title bar
_APR_GRID_TOP = 110
_APR_TILE_W   = 280
_APR_TILE_H   = 84
_APR_TILE_GX  = 18
_APR_TILE_GY  = 12
_APR_COLS     = 2
_APR_CANCEL_H = 44


def _apr_runway_ends(ident: str) -> list:
    """Return [(rwy_id, lat, lon, elev_ft, hdg_deg, length_ft), ...] for the
    airport ``ident``, one entry per runway end (so a single physical
    runway yields two entries, e.g. RWY 03 and RWY 21)."""
    if _runways is None or not ident:
        return []
    if hasattr(_runways, "dtype"):
        mask = _runways["airport"] == ident
        rows = _runways[mask]
        ends = []
        for r in rows:
            ends.append((str(r["le_ident"]), float(r["le_lat"]),
                         float(r["le_lon"]), float(r["le_elev_ft"]),
                         float(r["le_hdg"]), float(r["length_ft"])))
            ends.append((str(r["he_ident"]), float(r["he_lat"]),
                         float(r["he_lon"]), float(r["he_elev_ft"]),
                         float(r["he_hdg"]), float(r["length_ft"])))
        return ends
    # Pure-Python fallback (legacy path).
    return [
        (e[0], e[1], e[2], e[3], e[4], r.length_ft)
        for r in _runways if r.airport == ident
        for e in (
            (r.le_ident, r.le_lat, r.le_lon, r.le_elev_ft, r.le_hdg),
            (r.he_ident, r.he_lat, r.he_lon, r.he_elev_ft, r.he_hdg),
        )
    ]


def _apr_grid_origin():
    """Top-left of the runway tile grid (centred horizontally)."""
    grid_w = _APR_COLS * _APR_TILE_W + (_APR_COLS - 1) * _APR_TILE_GX
    return (DISPLAY_W - grid_w) // 2, _APR_GRID_TOP


def _apr_tile_rect(i: int) -> pygame.Rect:
    """Rect for the i-th runway tile (row-major, _APR_COLS columns)."""
    gx, gy = _apr_grid_origin()
    col = i % _APR_COLS
    row = i // _APR_COLS
    x = gx + col * (_APR_TILE_W + _APR_TILE_GX)
    y = gy + row * (_APR_TILE_H + _APR_TILE_GY)
    return pygame.Rect(x, y, _APR_TILE_W, _APR_TILE_H)


def draw_approach_select(surf):
    """Approach-runway picker screen.  Draws runway tiles for the parent
    airport (read from disp["nav"]["ident"]), plus a CANCEL APPROACH
    button when an approach is currently active."""
    _screen_header(surf, "APPROACH")

    nv  = disp.get("nav") or {}
    ap  = disp.get("approach") or {}
    # Prefer the approach's own airport (set when the approach was opened) so a
    # FPL-loaded approach resolves the destination's runways, not the active
    # leg's waypoint that happens to be in disp["nav"].
    ident = ap.get("airport", "") or nv.get("ident", "")

    # Header line — the airport this picker is for.
    if ident:
        _text(surf, ident, 22, WHITE, bold=True,
              cx=DISPLAY_W // 2, cy=_APR_HEADER_Y)
    else:
        _text(surf, "Set a Direct-To airport first",
              16, (200, 180, 80),
              cx=DISPLAY_W // 2, cy=_APR_HEADER_Y)
        return

    ends = _apr_runway_ends(ident)
    if not ends:
        _text(surf, f"No runway data loaded for {ident}",
              16, (200, 180, 80),
              cx=DISPLAY_W // 2, cy=_APR_HEADER_Y + 36)
        return

    cur_runway = ap.get("runway", "") if ap.get("active") else ""
    for i, (rid, _la, _lo, elev, hdg, length) in enumerate(ends):
        rect = _apr_tile_rect(i)
        if rect.bottom > DISPLAY_H - _APR_CANCEL_H - 10:
            break    # ran out of room; pagination would go here
        active = (rid == cur_runway)
        bg = (0, 55, 65) if active else (0, 18, 38)
        oc = CYAN      if active else (60, 80, 110)
        pygame.draw.rect(surf, bg, rect, border_radius=8)
        pygame.draw.rect(surf, oc, rect, width=2, border_radius=8)
        _text(surf, f"RWY {rid}", 22, WHITE, bold=True,
              cx=rect.centerx, cy=rect.y + 24)
        _text(surf, f"{int(round(length)):,} ft   {int(round(hdg))}°",
              14, (180, 195, 220),
              cx=rect.centerx, cy=rect.y + 56)

    # CANCEL APPROACH button — only when an approach is active.
    if ap.get("active"):
        bx = DISPLAY_W // 2 - 130
        by = DISPLAY_H - _APR_CANCEL_H - 12
        _action_btn(surf, bx, by, 260, _APR_CANCEL_H,
                    "CANCEL APPROACH", style="warn")


def approach_select_hit(x, y):
    """Return action string for a tap on the approach-select screen, or
    None if nothing was hit."""
    if _back_hit(x, y):
        return "back"

    nv  = disp.get("nav") or {}
    ap  = disp.get("approach") or {}
    # Prefer the approach's own airport (set when the approach was opened) so a
    # FPL-loaded approach resolves the destination's runways, not the active
    # leg's waypoint that happens to be in disp["nav"].
    ident = ap.get("airport", "") or nv.get("ident", "")
    if not ident:
        return None

    ends = _apr_runway_ends(ident)
    for i in range(len(ends)):
        rect = _apr_tile_rect(i)
        if rect.bottom > DISPLAY_H - _APR_CANCEL_H - 10:
            break
        if rect.collidepoint(x, y):
            return f"select:{i}"

    if ap.get("active"):
        bx = DISPLAY_W // 2 - 130
        by = DISPLAY_H - _APR_CANCEL_H - 12
        if bx <= x <= bx + 260 and by <= y <= by + _APR_CANCEL_H:
            return "cancel"
    return None


def _approach_load(ident, rwy_end, activate=False):
    """Load a synthetic approach to a runway end of airport `ident`.

    Two states.  *Loaded* (armed) stores the threshold and shows the approach
    on the plan, but leaves lateral guidance alone — the CDI keeps flying the
    flight plan to the airport.  *Active* (engaged) hands the CDI / ETE / inset
    line to the threshold and turns on HITS + the vertical deviation indicator.

    A plain direct-to APPR loads-and-activates in one step (activate=True); an
    approach loaded from the flight plan stays armed until the pilot activates
    it (FPL screen / CDI)."""
    rid, la, lo, elev, hdg, _length = rwy_end
    disp["approach"] = {
        "loaded":          True,
        "active":          bool(activate),
        "airport":         ident,
        "runway":          rid,
        "thresh_lat":      float(la),
        "thresh_lon":      float(lo),
        "thresh_elev_ft":  float(elev),
        "course_deg":      float(hdg),
    }
    if activate:
        _approach_begin_guidance()


# ── Published approaches (FAA CIFP, via shared/navdata) ─────────────────────────
import re as _re_appr


def _appr_runway_from_name(name):
    """'RNAV (GPS) RWY 03' → '03'  ·  'ILS OR LOC RWY 21L' → '21L'  ·  '' if none."""
    m = _re_appr.search(r"RWY\s*(\d{1,2}[LRC]?)", name or "")
    return m.group(1) if m else ""


# An approach name carries its type/runway (RNAV (GPS) RWY 03, ILS OR LOC RWY 21,
# VOR-A, …); a SID/STAR is a fix-name + number (FLG1, SEDON2).  Filter on the
# name so a mis-typed departure can't sneak into the approach list.
_APPR_NAME_RE = _re_appr.compile(
    r"\b(?:RWY|RNAV|ILS|LOC|VOR|NDB|GPS|GLS|LDA|TACAN|RNP|SDF|MLS)\b", _re_appr.I)


def _appr_published(airport):
    """Published APPROACH procedure idents for an airport (SIDs/STARs/departures
    filtered out by type AND name), or [] when no nav data is loaded."""
    if _navdata is None or not airport:
        return []
    out = []
    for pid in _navdata.procedures_for(airport):
        p = _navdata.procedure(airport, pid)
        if not p or p.get("type") in ("SID", "STAR"):
            continue
        if _APPR_NAME_RE.search(pid):
            out.append(pid)
    return out


def _appr_leg_pts(legs):
    """Filter a leg list to those with resolved coordinates →
    [(lat, lon, ident, leg_type, alt_ft, alt_type), ...]  (map + threshold +
    crossing-altitude display).  alt_type: 'AB' above · 'BL' below · 'AT' at ·
    'WN' window."""
    out = []
    for lg in legs or []:
        la, lo = lg.get("lat"), lg.get("lon")
        if la is None or lo is None:
            continue
        out.append((float(la), float(lo), str(lg.get("fix", "")),
                    str(lg.get("leg_type", "")), lg.get("alt_ft"),
                    lg.get("alt_type")))
    return out


def _appr_alt_label(alt_ft, alt_type):
    """'9000A' (at/above) · '8500' (at) · '10000B' (at/below) · '' if none."""
    if alt_ft is None:
        return ""
    suffix = {"AB": "A", "BL": "B", "WN": "W"}.get(alt_type, "")
    return f"{int(alt_ft):,}{suffix}"


def _appr_dedupe(pts):
    """Drop consecutive same-ident legs (transition/final IF overlap, repeated
    MAP, …), keeping a crossing altitude if the surviving row lacked one."""
    out = []
    for p in pts:
        if out and out[-1][2] == p[2]:
            if out[-1][4] is None and p[4] is not None:
                out[-1] = p
            continue
        out.append(p)
    return out


def _appr_path_dedupe(pts):
    """Dedupe an approach path: drop consecutive same-ident legs AND an earlier
    occurrence of a fix that recurs later — a transition that overlaps the
    common segment lists the shared fix twice, non-adjacently (e.g. FIMAL via
    the FLG transition).  Keeps the LAST occurrence so the common-segment order
    wins; legs without an ident (synthesised points) are always kept."""
    last = {}
    for i, p in enumerate(pts):
        if p[2]:
            last[p[2]] = i
    out = []
    for i, p in enumerate(pts):
        if p[2] and last.get(p[2]) != i:
            continue                              # earlier dup of a later fix
        if out and out[-1][2] and out[-1][2] == p[2]:
            continue                              # consecutive same ident
        out.append(p)
    return out


def _approach_load_published(airport, proc_ident, transition="", activate=False):
    """Load a published approach (its transition + final legs, and the missed
    legs) into disp["approach"], armed.  Reuses the existing approach guidance:
    threshold + final-approach course come from the procedure's last final leg,
    so HITS / VDI / centreline CDI all work unchanged once activated.  Returns
    False if the procedure can't be resolved."""
    p = _navdata.procedure(airport, proc_ident) if _navdata is not None else None
    if not p:
        return False
    legs = []
    if transition and transition in (p.get("transitions") or {}):
        legs.extend(p["transitions"][transition])
    legs.extend(p.get("final") or [])
    final_pts = _appr_leg_pts(p.get("final") or [])
    if not final_pts:                       # nothing flyable — bail to synthetic
        return False
    # Split off the missed approach.  The missed begins at the first climb leg
    # (CA/FA/VA/VM/FM) or hold-to-manual (HM) — you only climb on the missed —
    # which works for LOC/VOR approaches whose MAP is a timing point (no RWxx
    # fix) as well as for ILS/RNAV.  Fall back to the RWxx fix, then to "all".
    _CLIMB = ("CA", "FA", "VA", "VM", "FM")
    missed_start = None
    for i, lg in enumerate(legs):
        lt = (lg.get("leg_type") or "").upper()
        if lt in _CLIMB or lt == "HM":
            missed_start = i
            break
    if missed_start is None:
        rw = [i for i, lg in enumerate(legs)
              if _re_appr.match(r"RW\d", lg.get("fix") or "")]
        missed_start = (rw[-1] + 1) if rw else len(legs)
    # Resolve coords + dedupe.  The approach path also drops a fix that recurs
    # later (a transition that overlaps the common segment lists the shared fix
    # twice, non-adjacently — e.g. FIMAL via the FLG transition).
    all_pts = _appr_path_dedupe(_appr_leg_pts(legs[:missed_start]))
    missed_pts = _appr_dedupe(_appr_leg_pts(legs[missed_start:] + (p.get("missed") or [])))
    if not all_pts:
        return False
    first_final = final_pts[0][2]
    final_idx = next((i for i, q in enumerate(all_pts) if q[2] == first_final), 0)
    # Connect to the runway: if the last approach fix IS the runway (RWxx) snap
    # it onto the real threshold; otherwise (LOC/VOR — the MAP is a timing
    # point) extend the final leg to the threshold so it reaches the runway bar.
    rwy = _appr_runway_from_name(proc_ident)
    thr = _appr_landing_threshold(airport, rwy)
    if thr is not None:
        if _re_appr.match(r"RW\d", all_pts[-1][2] or ""):
            la0, lo0, idn, lt0, alt0, at0 = all_pts[-1]
            all_pts[-1] = (float(thr[0]), float(thr[1]), idn, lt0, alt0, at0)
        else:
            all_pts.append((float(thr[0]), float(thr[1]), "", "TF", None, None))
    final_idx = min(final_idx, len(all_pts) - 1)
    th_lat, th_lon, _thid, _tht, th_alt, _that = all_pts[-1]
    # Threshold elevation: prefer the runway DB (authoritative field elevation);
    # the CIFP RW leg often has no altitude (→ 0), which would put the whole
    # vertical profile / HITS boxes / glidepath at sea level.
    th_elev = float(thr[2]) if (thr is not None and len(thr) > 2) else float(th_alt or 0.0)
    # Final-approach course = bearing of the leg into the MAP (matches the plate
    # far better than the raw CIFP course field).
    if len(all_pts) >= 2:
        _d, course = _nav_geo_dist_brg(all_pts[-2][0], all_pts[-2][1],
                                       th_lat, th_lon)
    else:
        course = 0.0
    disp["approach"] = {
        "loaded":          True,
        "active":          bool(activate),
        "missed":          False,
        "published":       True,
        "airport":         airport,
        "procedure":       proc_ident,
        "transition":      transition,
        "runway":          _appr_runway_from_name(proc_ident),
        "legs":            all_pts,
        "final_idx":       final_idx,          # leg index where the final begins
        "missed_legs":     missed_pts,
        "thresh_lat":      float(th_lat),
        "thresh_lon":      float(th_lon),
        "thresh_elev_ft":  th_elev,
        "course_deg":      float(course or 0.0),
        "leg_idx":         0,
    }
    if activate:
        _approach_begin_guidance()
    _ssync_publish_approach()
    return True


def _approach_begin_guidance():
    """Engage guidance for the now-active approach: leg-by-leg sequencing from
    leg 0 for a published approach, or the runway centreline for a synthetic
    one.  A published approach supersedes the flight plan, so drop the FPL's
    active leg so the two don't fight for the CDI."""
    ap = disp.get("approach") or {}
    if ap.get("published") and (ap.get("legs") or []):
        ap["leg_idx"] = 0
        if _fpl_is_active():
            disp["fpl"]["active_idx"] = -1
            _ssync_publish_fpl()
        _approach_apply_leg()
    else:
        _approach_retarget_nav()


def _approach_apply_leg(from_present=False):
    """Drive disp["nav"] from the active published-approach leg.  The final leg
    targets the airport/threshold so draw_cdi switches to the runway-centreline
    branch (HITS + VDI); earlier legs fly direct fix-to-fix.  ``from_present``
    anchors the course origin at the aircraft (a direct-to) instead of the
    previous leg."""
    ap = disp.get("approach") or {}
    legs = ap.get("legs") or []
    if not legs:
        return
    idx = max(0, min(int(ap.get("leg_idx", 0)), len(legs) - 1))
    ap["leg_idx"] = idx
    la, lo, ident, _lt, _alt, _at = legs[idx]
    prev = None if from_present else (legs[idx - 1] if idx > 0 else None)
    act_lat = float(prev[0]) if prev else float(disp.get("lat", la))
    act_lon = float(prev[1]) if prev else float(disp.get("lon", lo))
    if idx == len(legs) - 1:                 # final leg → threshold/centreline
        disp["nav"] = {
            "ident":   ap.get("airport", ""),
            "lat":     float(ap.get("thresh_lat", la)),
            "lon":     float(ap.get("thresh_lon", lo)),
            "elev_ft": float(ap.get("thresh_elev_ft", 0.0)),
            "act_lat": act_lat, "act_lon": act_lon,
        }
    else:
        disp["nav"] = {
            "ident": ident, "lat": float(la), "lon": float(lo), "elev_ft": 0.0,
            "act_lat": act_lat, "act_lon": act_lon,
        }


def _approach_centerline_active():
    """True when guidance should fly the runway CENTRELINE (the final approach
    leg, or a synthetic missed) rather than direct-to an intermediate fix.
    Mirrors draw_cdi exactly: the centreline applies only when disp['nav'] is
    targeting the airport itself — which `_approach_apply_leg` sets only on the
    final leg / threshold.  On an intermediate leg (or a published missed leg)
    nav targets the fix, so this is False and guidance flies direct-to it.

    This is the fix for "D2/activate an approach leg flies the opposite way":
    the sim autopilot + the inset's magenta line keyed off `approach.active`
    alone, forcing the final-approach course/threshold even when the active
    leg was an upstream fix behind the aircraft."""
    ap = disp.get("approach") or {}
    nv = disp.get("nav") or {}
    return bool((ap.get("active") or ap.get("missed"))
                and ap.get("airport") and nv.get("ident") == ap.get("airport"))


_APPR_FLYBY_BANK_DEG = 18.0    # nominal bank used to size the turn-anticipation


def _approach_check_advance(lat, lon):
    """Per-frame approach leg sequencing — FLY-BY by default.

    Real procedures mark only a few fixes fly-OVER (the MAP, the odd charted
    one); the rest are fly-BY: lead the turn and sequence early so the aircraft
    rolls out established on the next segment instead of overflying the fix and
    S-turning back onto course.  Our nav data carries no fly-over flag, so per
    the convention everything intermediate is fly-by.  (The MAP IS fly-over but
    it's the final leg, which this function never sequences — the centreline
    CDI/HITS/VDI fly it to the threshold.)

    Sequence when the aircraft is within the turn-anticipation lead distance of
    the fix — the standard R·tan(Δ/2), R = V²/(g·tanφ) from groundspeed at a
    nominal bank, Δ the leg-to-leg turn angle — bounded so a near-straight leg
    still just crosses the fix.  Vertical guidance is distance-to-threshold
    based (`_approach_target_alt`), NOT leg-index based, so sequencing the
    LATERAL leg early does not advance the step-down profile early."""
    ap = disp.get("approach") or {}
    if not (ap.get("active") and ap.get("published") and not ap.get("missed")):
        return
    legs = ap.get("legs") or []
    idx = int(ap.get("leg_idx", 0))
    if idx >= len(legs) - 1:
        return
    la, lo, _ident, _lt, _alt, _at = legs[idx]
    # Inbound course to this fix (from the previous fix, or the aircraft on the
    # first leg) and outbound course to the next fix → the turn angle.
    if idx > 0:
        pla, plo = float(legs[idx - 1][0]), float(legs[idx - 1][1])
    else:
        pla, plo = lat, lon
    _d, course_in = _nav_geo_dist_brg(pla, plo, la, lo)
    nla, nlo = float(legs[idx + 1][0]), float(legs[idx + 1][1])
    _dn, course_out = _nav_geo_dist_brg(la, lo, nla, nlo)
    turn = abs(((course_out - course_in + 180.0) % 360.0) - 180.0)
    # Turn-anticipation lead distance.
    gs = max(20.0, float(disp.get("speed", 0.0)))
    v_fps = gs * 1.6878
    R_ft = v_fps * v_fps / (32.174 * math.tan(math.radians(_APPR_FLYBY_BANK_DEG)))
    lead_ft = R_ft * math.tan(math.radians(min(turn, 170.0) / 2.0))
    lead_nm = max(0.10, min(2.0, lead_ft / 6076.12))
    dist_nm, brg_fix_ac = _nav_geo_dist_brg(la, lo, lat, lon)
    ahead = abs(((brg_fix_ac - course_in + 180.0) % 360.0) - 180.0) < 90.0
    if dist_nm <= lead_nm or ahead:      # within the turn lead, or crossed
        ap["leg_idx"] = idx + 1
        _approach_apply_leg()
        _ssync_publish_approach()    # propagate the sequenced leg to peers


def _approach_goto_leg(idx, from_present=False):
    """Engage the approach and jump to leg ``idx`` (used by the leg menu's
    ACTIVATE / DIRECT-TO and VECTORS).  from_present anchors the course at the
    aircraft (direct-to)."""
    ap = disp.get("approach") or {}
    legs = ap.get("legs") or []
    if not legs:
        return
    ap["active"] = True
    ap["missed"] = False
    if _fpl_is_active():
        disp["fpl"]["active_idx"] = -1
        _ssync_publish_fpl()
    ap["leg_idx"] = max(0, min(int(idx), len(legs) - 1))
    global _appr_xtk_int
    _appr_xtk_int = 0.0          # fresh leg → reset the cross-track integrator
    _approach_apply_leg(from_present=from_present)
    _ssync_publish_approach()    # broadcast the activated/D2'd leg to peers


def _approach_retarget_nav():
    """Point the direct-to / CDI at the active approach's threshold.  Keep
    nav["ident"] the plain airport ident so airport-DB lookups still resolve;
    draw_cdi appends the runway suffix itself."""
    ap = disp.get("approach") or {}
    th_lat = float(ap.get("thresh_lat", 0.0))
    th_lon = float(ap.get("thresh_lon", 0.0))
    disp["nav"] = {
        "ident":   ap.get("airport", ""),
        "lat":     th_lat,
        "lon":     th_lon,
        "elev_ft": float(ap.get("thresh_elev_ft", 0.0)),
        "act_lat": float(disp.get("lat", th_lat)),
        "act_lon": float(disp.get("lon", th_lon)),
    }


def _approach_engage():
    """Pilot deliberately activates a loaded approach — engage guidance (leg-by-
    leg for a published approach, runway centreline for a synthetic one).  No-op
    if nothing is loaded."""
    ap = disp.get("approach") or {}
    if not ap.get("loaded"):
        return
    ap["active"] = True
    ap["missed"] = False
    _approach_begin_guidance()
    _ssync_publish_approach()


def _approach_go_missed():
    """Pilot goes missed / goes around on an active approach.

    Drops descent guidance (active=False turns off the VDI / HITS / terrain
    inhibit).  A published approach with missed legs sequences the real missed
    procedure (climb to a fix, hold); otherwise it's the synthetic advisory —
    the CDI keeps the runway centreline so it reads as 'climb straight ahead on
    the runway heading'."""
    ap = disp.get("approach") or {}
    if not ap.get("loaded"):
        return
    ap["active"] = False
    ap["missed"] = True
    if ap.get("published") and (ap.get("missed_legs") or []):
        ap["missed_idx"] = 0
        _approach_apply_missed_leg()
    # else: synthetic / no missed legs — disp["nav"] stays on the threshold and
    # draw_cdi keeps the centreline reference (runway-heading climb advisory).
    _ssync_publish_approach()


def _approach_apply_missed_leg():
    """Drive disp["nav"] from the active published missed-approach leg
    (direct fix-to-fix; the last leg, usually the hold fix, is held)."""
    ap = disp.get("approach") or {}
    mlegs = ap.get("missed_legs") or []
    if not mlegs:
        return
    idx = max(0, min(int(ap.get("missed_idx", 0)), len(mlegs) - 1))
    ap["missed_idx"] = idx
    la, lo, ident, _lt, _alt, _at = mlegs[idx]
    prev = mlegs[idx - 1] if idx > 0 else None
    disp["nav"] = {
        "ident": ident, "lat": float(la), "lon": float(lo), "elev_ft": 0.0,
        "act_lat": float(prev[0]) if prev else float(disp.get("lat", la)),
        "act_lon": float(prev[1]) if prev else float(disp.get("lon", lo)),
    }


def _approach_check_missed_advance(lat, lon):
    """Per-frame: sequence through the published missed legs; hold on the last
    one (the missed-approach holding fix)."""
    ap = disp.get("approach") or {}
    if not (ap.get("missed") and ap.get("published")):
        return
    mlegs = ap.get("missed_legs") or []
    idx = int(ap.get("missed_idx", 0))
    if idx >= len(mlegs) - 1:
        return
    la, lo, _ident, _lt, _alt, _at = mlegs[idx]
    if _nav_geo_dist_brg(lat, lon, la, lo)[0] < _FPL_ADVANCE_DIST_NM:
        ap["missed_idx"] = idx + 1
        _approach_apply_missed_leg()
        _ssync_publish_approach()


def _approach_phase():
    """'none' | 'armed' | 'active' | 'missed' — the single source of truth the
    UI reads to label the approach controls."""
    ap = disp.get("approach") or {}
    if not ap.get("loaded"):
        return "none"
    if ap.get("missed"):
        return "missed"
    if ap.get("active"):
        return "active"
    return "armed"


def _approach_label():
    """Short human label for the loaded approach — the published procedure name
    ('RNAV (GPS) RWY 03'), or 'RWY <id>' for a synthetic approach."""
    ap = disp.get("approach") or {}
    if ap.get("published") and ap.get("procedure"):
        return ap["procedure"]
    rwy = ap.get("runway", "")
    return f"RWY {rwy}" if rwy else "APPR"


def _approach_cancel():
    """Clear the loaded/active/missed approach.  If a flight plan is still
    active, restore the CDI to its active leg; otherwise leave the direct-to
    where it is (pilot can re-issue a D2)."""
    ap = disp.get("approach")
    if ap:
        ap["loaded"] = False
        ap["active"] = False
        ap["missed"] = False
    _ssync_publish_approach()
    if _fpl_is_active():
        _fpl_apply_active()


# ── Published-approach procedure picker (CIFP) ─────────────────────────────────
_PRC_TOP   = 100
_PRC_MX    = 14
_PRC_ROW_H = 52
_PRC_GY    = 8
_PRC_SB_W  = 84            # reserved right column for ▲/▼ scroll buttons
_PRC_BTN_H = 76            # height of each scroll button


def _prc_list_bot():
    return DISPLAY_H - 12


def _prc_row_rect(i):
    by = _PRC_TOP + i * (_PRC_ROW_H + _PRC_GY) - _prc_scroll
    return pygame.Rect(_PRC_MX, by, DISPLAY_W - _PRC_MX - _PRC_SB_W, _PRC_ROW_H)


def _prc_max_scroll(n):
    content = n * (_PRC_ROW_H + _PRC_GY) - _PRC_GY
    return max(0, content - (_prc_list_bot() - _PRC_TOP))


def _prc_clamp_scroll(n):
    global _prc_scroll
    _prc_scroll = max(0, min(_prc_scroll, _prc_max_scroll(n)))


def _prc_page_step():
    """Scroll amount for one ▲/▼ tap: a near-full page (keeps one row of
    overlap so nothing is skipped)."""
    return max(_PRC_ROW_H + _PRC_GY,
               (_prc_list_bot() - _PRC_TOP) - (_PRC_ROW_H + _PRC_GY))


def _prc_up_btn_rect():
    bx = DISPLAY_W - _PRC_SB_W + 6
    return pygame.Rect(bx, _PRC_TOP, _PRC_SB_W - 12, _PRC_BTN_H)


def _prc_down_btn_rect():
    bx = DISPLAY_W - _PRC_SB_W + 6
    return pygame.Rect(bx, _prc_list_bot() - _PRC_BTN_H, _PRC_SB_W - 12, _PRC_BTN_H)


def _prc_scroll_btn_hit(n, x, y):
    """'up' / 'down' / None for a tap on a scroll button (only live when the
    list overflows in that direction)."""
    if _prc_max_scroll(n) <= 0:
        return None
    if _prc_scroll > 0 and _prc_up_btn_rect().collidepoint(x, y):
        return "up"
    if _prc_scroll < _prc_max_scroll(n) and _prc_down_btn_rect().collidepoint(x, y):
        return "down"
    return None


def _prc_scroll_by(n, direction):
    global _prc_scroll
    _prc_scroll += _prc_page_step() * (1 if direction == "down" else -1)
    _prc_clamp_scroll(n)


def _prc_scrollbar(surf, n):
    """Draw the right-column scroll controls: ▲/▼ tap buttons, an 'N MORE'
    badge, and a thin position bar.  Buttons dim when they can't move."""
    max_s = _prc_max_scroll(n)
    if max_s <= 0:
        return
    top, bot = _PRC_TOP, _prc_list_bot()
    # Thin position bar hugging the inner edge of the reserved column.
    bx = DISPLAY_W - _PRC_SB_W
    th = bot - top
    thumb = max(24, int(th * th / (th + max_s)))
    ty = top + int((th - thumb) * _prc_scroll / max_s)
    pygame.draw.rect(surf, (40, 50, 70), (bx, top, 4, th), border_radius=2)
    pygame.draw.rect(surf, (120, 150, 190), (bx, ty, 4, thumb), border_radius=2)

    row = _PRC_ROW_H + _PRC_GY
    above = int(round(_prc_scroll / row))
    below = int(round((max_s - _prc_scroll) / row))

    def _btn(rect, glyph, count, live):
        col_bg = (24, 40, 64) if live else (16, 20, 28)
        col_ln = (90, 130, 180) if live else (45, 55, 70)
        col_tx = (210, 230, 255) if live else (70, 80, 95)
        pygame.draw.rect(surf, col_bg, rect, border_radius=10)
        pygame.draw.rect(surf, col_ln, rect, width=2, border_radius=10)
        _text(surf, glyph, 30, col_tx, bold=True, cx=rect.centerx,
              cy=rect.centery - 9)
        _text(surf, (f"{count}" if live else "—"), 14, col_tx,
              cx=rect.centerx, cy=rect.centery + 18)

    _btn(_prc_up_btn_rect(),   "▲", above, _prc_scroll > 0)
    _btn(_prc_down_btn_rect(), "▼", below, _prc_scroll < max_s)
    if below > 0:
        _text(surf, f"{below} MORE ▼", 14, (255, 210, 90), bold=True,
              cx=DISPLAY_W - _PRC_SB_W // 2,
              cy=(_prc_up_btn_rect().bottom + _prc_down_btn_rect().top) // 2)


def _prc_draw_rows(surf, items, accent=(60, 90, 130), label_color=WHITE,
                   suffix="▶"):
    """Draw a scrollable row list; returns nothing.  items: list of (label,) or
    (label, sublabel, accent_override)."""
    _prc_clamp_scroll(len(items))
    clip = surf.get_clip()
    surf.set_clip(pygame.Rect(0, _PRC_TOP, DISPLAY_W, _prc_list_bot() - _PRC_TOP))
    for i, it in enumerate(items):
        rect = _prc_row_rect(i)
        if rect.bottom < _PRC_TOP or rect.top > _prc_list_bot():
            continue
        for j in range(rect.height):
            t = 1.0 - j / rect.height
            pygame.draw.line(surf, (int(t * 10), int(14 + t * 22), int(30 + t * 42)),
                             (rect.x, rect.y + j), (rect.right, rect.y + j))
        pygame.draw.rect(surf, accent, rect, width=1, border_radius=6)
        _text(surf, it, 19, label_color, bold=True, x=rect.x + 16, cy=rect.centery)
        if suffix:
            _text(surf, suffix, 18, (70, 100, 140), x=rect.right - 30,
                  cy=rect.centery)
    surf.set_clip(clip)
    _prc_scrollbar(surf, len(items))


def _prc_row_hit(n, x, y):
    """Index of the on-screen row hit, or None."""
    for i in range(n):
        r = _prc_row_rect(i)
        if r.top >= _PRC_TOP - 1 and r.bottom <= _prc_list_bot() + 1 \
                and r.collidepoint(x, y):
            return i
    return None


def draw_appr_proc_select(surf):
    """List the published approaches for disp["approach"]["airport"]; tap to load."""
    surf.fill((0, 0, 0))
    _screen_header(surf, "SELECT APPROACH")
    airport = (disp.get("approach") or {}).get("airport", "")
    _text(surf, airport or "—", 20, WHITE, bold=True, cx=DISPLAY_W // 2, cy=72)
    procs = _appr_published(airport)
    if not procs:
        _text(surf, f"No published approaches for {airport}", 15, YELLOW,
              cx=DISPLAY_W // 2, cy=_PRC_TOP + 30)
        return
    _prc_draw_rows(surf, procs)


def appr_proc_select_hit(x, y):
    if _back_hit(x, y):
        return ("back", None)
    procs = _appr_published((disp.get("approach") or {}).get("airport", ""))
    i = _prc_row_hit(len(procs), x, y)
    return ("pick", procs[i]) if i is not None else (None, None)


def _appr_pending_transitions():
    """['VECTORS', <iaf>, …] for the procedure picked in appr_proc_select."""
    ap = disp.get("approach") or {}
    p = (_navdata.procedure(ap.get("airport", ""), ap.get("pending_proc", ""))
         if _navdata is not None else None)
    return ["VECTORS"] + sorted((p.get("transitions") or {}).keys()) if p else ["VECTORS"]


def _appr_trans_kind(tid):
    """Short tag for a transition entry so a navaid feeder reads differently
    from a charted IAF: 'VOR'/'VORTAC'/'NDB'/… when the ident resolves to a
    navaid (e.g. the FLG VORTAC feeder), else 'IAF'."""
    if _navdata is not None:
        nv = _navdata.navaid(tid)
        if nv:
            nt = (nv[1] or "").upper().strip()
            if nt.startswith("VOR") or nt in ("DME", "TAC", "VT"):
                return "VOR"
            if nt.startswith("NDB"):
                return "NDB"
            return nt or "VOR"
    return "IAF"


def draw_appr_trans_select(surf):
    """Pick a transition (IAF) for the procedure chosen on the previous screen,
    or VECTORS to fly the final segment only."""
    surf.fill((0, 0, 0))
    _screen_header(surf, "TRANSITION")
    ap = disp.get("approach") or {}
    _text(surf, f"{ap.get('airport','')}  {ap.get('pending_proc','')}", 18, WHITE,
          bold=True, cx=DISPLAY_W // 2, cy=72)
    trans = _appr_pending_transitions()
    _prc_clamp_scroll(len(trans))
    clip = surf.get_clip()
    surf.set_clip(pygame.Rect(0, _PRC_TOP, DISPLAY_W, _prc_list_bot() - _PRC_TOP))
    for i, tid in enumerate(trans):
        rect = _prc_row_rect(i)
        if rect.bottom < _PRC_TOP or rect.top > _prc_list_bot():
            continue
        vectors = (tid == "VECTORS")
        for j in range(rect.height):
            t = 1.0 - j / rect.height
            base = ((int(t * 8), int(10 + t * 16), int(20 + t * 30)) if vectors
                    else (int(t * 10), int(14 + t * 22), int(30 + t * 42)))
            pygame.draw.line(surf, base, (rect.x, rect.y + j), (rect.right, rect.y + j))
        pygame.draw.rect(surf, (90, 110, 80) if vectors else (60, 90, 130),
                         rect, width=1, border_radius=6)
        lbl = "VECTORS (final only)" if vectors else tid
        _text(surf, lbl, 19, (200, 210, 180) if vectors else WHITE, bold=True,
              x=rect.x + 16, cy=rect.centery)
        if not vectors:
            # Tag feeders by kind so a VOR/VORTAC reads differently from an IAF
            # fix (a navaid feeder like FLG isn't a charted IAF).
            kind = _appr_trans_kind(tid)
            is_iaf = (kind == "IAF")
            kcol = (120, 140, 170) if is_iaf else (255, 200, 110)
            _text(surf, f"{kind} ▶", 14, kcol, x=rect.right - 84,
                  cy=rect.centery)
    surf.set_clip(clip)
    _prc_scrollbar(surf, len(trans))


def appr_trans_select_hit(x, y):
    if _back_hit(x, y):
        return ("back", None)
    trans = _appr_pending_transitions()
    i = _prc_row_hit(len(trans), x, y)
    return ("pick", trans[i]) if i is not None else (None, None)


# ── Approach preview (map) — shown before a loaded approach is kept ─────────────
_APRV_BTN_H = 56


def _appr_project(lat, lon, brg_deg, dist_nm):
    """Destination lat/lon from a point along a bearing (equirectangular —
    fine for the short runway/marker distances here)."""
    br = math.radians(brg_deg)
    dlat = (dist_nm / 60.0) * math.cos(br)
    dlon = (dist_nm / 60.0) * math.sin(br) / max(0.2, math.cos(math.radians(lat)))
    return lat + dlat, lon + dlon


def _rwy_ident_eq(a, b):
    """Runway-ident equality tolerant of zero-padding: '3' == '03', '3L' ==
    '03L'.  OurAirports stores single-digit runways unpadded while the CIFP
    approach name yields a padded number, so a naive '==' silently misses."""
    def norm(s):
        s = (s or "").upper().strip()
        i = 0
        while i < len(s) and s[i] == "0":
            i += 1
        return s[i:] if i < len(s) else s
    return norm(a) == norm(b) and bool(norm(a))


def _appr_landing_threshold(airport, rwy):
    """(lat, lon, elev_ft) of the landing threshold for ``rwy`` at ``airport``
    from the runway DB (the end whose ident matches the approach runway), or
    None.  The elevation is authoritative for the vertical profile — the CIFP
    RW leg often carries no altitude (→ 0), which would anchor the whole HITS /
    glidepath profile at sea level."""
    if not rwy or _runways is None or not hasattr(_runways, "dtype"):
        return None
    for r in _runways[_runways["airport"] == airport]:
        if _rwy_ident_eq(rwy, str(r["le_ident"])):
            return (float(r["le_lat"]), float(r["le_lon"]), float(r["le_elev_ft"]))
        if _rwy_ident_eq(rwy, str(r["he_ident"])):
            return (float(r["he_lat"]), float(r["he_lon"]), float(r["he_elev_ft"]))
    return None


def _appr_runway_marker(ap):
    """((le_la, le_lo), (he_la, he_lo), 'RWY 03') — the ENTIRE real runway from
    the runway DB (true endpoints, so position/orientation/length are exact),
    or a threshold + final-course fallback when the DB has no match."""
    rwy = (ap.get("runway") or "").upper()
    airport = ap.get("airport", "")
    if rwy and _runways is not None and hasattr(_runways, "dtype"):
        rows = _runways[_runways["airport"] == airport]
        for r in rows:
            le, he = str(r["le_ident"]).upper(), str(r["he_ident"]).upper()
            if _rwy_ident_eq(rwy, le) or _rwy_ident_eq(rwy, he):
                return ((float(r["le_lat"]), float(r["le_lon"])),
                        (float(r["he_lat"]), float(r["he_lon"])),
                        f"RWY {rwy}")
    if ap.get("thresh_lat") is not None:        # no DB match → short stub
        la, lo = float(ap["thresh_lat"]), float(ap["thresh_lon"])
        fla, flo = _appr_project(la, lo, float(ap.get("course_deg", 0.0)), 1.0)
        return ((la, lo), (fla, flo), (f"RWY {rwy}" if rwy else "RWY"))
    return None


def _appr_preview_extent(rect):
    """(center_lat, center_lon, range_nm) that tightly frames every approach fix
    + the threshold in ``rect``.  range_nm is the vertical half-extent; the wide
    landscape rect shows range_nm*aspect across, so fit both axes."""
    ap = disp.get("approach") or {}
    pts = [(p[0], p[1]) for p in (ap.get("legs") or [])]
    if ap.get("thresh_lat") is not None:
        pts.append((float(ap["thresh_lat"]), float(ap["thresh_lon"])))
    rwm = _appr_runway_marker(ap)
    if rwm is not None:
        pts.extend([rwm[0], rwm[1]])           # keep the runway in frame
    # Keep the missed approach + every hold racetrack in frame too.
    pts += [(p[0], p[1]) for p in (_approach_render_missed() or [])]
    for h_la, h_lo, h_crs, h_turn, h_leg in _approach_render_holds():
        pts += _map_mod._hold_racetrack_pts(
            h_la, h_lo, h_crs, h_turn, h_leg, math.cos(math.radians(h_la)))
    if not pts:
        return (float(disp.get("lat", DEMO_LAT)),
                float(disp.get("lon", DEMO_LON)), 10.0)
    lats = [p[0] for p in pts]; lons = [p[1] for p in pts]
    clat = (min(lats) + max(lats)) / 2.0
    clon = (min(lons) + max(lons)) / 2.0
    half_n = (max(lats) - min(lats)) / 2.0 * 60.0
    half_e = (max(lons) - min(lons)) / 2.0 * 60.0 * max(0.2, math.cos(math.radians(clat)))
    _x, _y, rw, rh = rect
    aspect = max(0.1, rw / float(rh))
    rng = max(half_n, half_e / aspect) * 1.35 + 1.0
    return clat, clon, max(3.0, rng)


def draw_appr_preview(surf):
    """Map preview of the loaded approach with LOAD / BACK."""
    surf.fill((0, 0, 0))
    _screen_header(surf, "APPROACH PREVIEW")
    ap = disp.get("approach") or {}
    via = (f"via {ap.get('transition')}" if ap.get("transition") else "VECTORS")
    _text(surf, f"{ap.get('airport','')}   {ap.get('procedure','')}   ·   {via}",
          15, WHITE, bold=True, cx=DISPLAY_W // 2, cy=62)
    rect = (12, 82, DISPLAY_W - 24, DISPLAY_H - 82 - (_APRV_BTN_H + 24))
    clat, clon, rng = _appr_preview_extent(rect)
    # Declutter: keep terrain/water context only.  The destination airport's
    # symbol+label is dropped (it stacks on top of the RW threshold / "RWY xx"
    # marker — unreadable); the explicit runway_marker draws the runway instead.
    ds = dict(disp.get("ds", {}))
    ds.update({"map_show_airports": False, "map_show_obstacles": False,
               "map_show_traffic": False, "map_show_metar": False,
               "map_show_winds": False, "map_show_nexrad": False,
               "map_show_airspaces": False})
    _map_mod.render(surf, rect, clat, clon, 0.0, 0.0, 0.0, "nrth", rng, ds,
                    runways_arr=_runways,
                    srtm_dir=SRTM_DIR, water_dir=WATER_DIR,
                    font=_mfd_get_apt_font(),
                    symbol_scale=1.4,
                    state_lines=_state_lines, country_lines=_country_lines,
                    approach_path=_approach_render_path(),
                    runway_marker=_appr_runway_marker(ap),
                    missed_path=_approach_render_missed(),
                    holds=_approach_render_holds(),
                    draw_corner_labels=False)
    by = DISPLAY_H - _APRV_BTN_H - 12
    half = (DISPLAY_W - 24 - 12) // 2
    _action_btn(surf, 12, by, half, _APRV_BTN_H, "‹ BACK", "warn")
    _action_btn(surf, 12 + half + 12, by, DISPLAY_W - 24 - half - 12,
                _APRV_BTN_H, "LOAD APPROACH", "ok")


def appr_preview_hit(x, y):
    if _back_hit(x, y):
        return "back"
    by = DISPLAY_H - _APRV_BTN_H - 12
    if not (by <= y <= by + _APRV_BTN_H):
        return None
    half = (DISPLAY_W - 24 - 12) // 2
    if 12 <= x <= 12 + half:
        return "back"
    if 12 + half + 12 <= x <= DISPLAY_W - 12:
        return "load"
    return None


def draw_ahrs_setup(surf, ss):
    _screen_header(surf, "AHRS / SENSORS")
    _prev_clip = _ss_clip_to_content(surf)

    # Row 0: Pitch trim
    bx, by, bw, bh = _setting_row(surf, 0, "PITCH TRIM", "Horizon offset correction")
    _trim_stepper(surf, bx, by, bw, bh, ss.get("pitch_trim", 0.0), "pitch_trim")

    # Row 1: Roll trim
    bx, by, bw, bh = _setting_row(surf, 1, "ROLL TRIM", "Wing-level correction")
    _trim_stepper(surf, bx, by, bw, bh, ss.get("roll_trim", 0.0), "roll_trim")

    # Row 2: Magnetometer calibration — cardinal walk-through wizard
    bx, by, bw, bh = _setting_row(surf, 2, "MAGNETOMETER", "Compass calibration")
    cal = ss.get("mag_cal", "idle")
    state_lbl, state_col = _SS_MAG_LABELS.get(cal, ("?", WHITE))
    _text(surf, state_lbl, 13, state_col, bold=True, x=bx+220, y=by+(bh-30)//2)
    cur_deltas = ss.get("mag_cal_deltas") or []
    if cur_deltas and any(abs(d) > 0.05 for d in cur_deltas):
        peak = max(abs(d) for d in cur_deltas)
        _text(surf, f"max |Δ| {peak:.1f}°", 11, (140, 160, 190),
              x=bx+220, y=by+(bh-30)//2 + 18)
    elif abs(ss.get("mag_cal_offset", 0.0)) > 0.05:
        # Legacy single-offset still on disk (pre-piecewise)
        _text(surf, f"offset {ss['mag_cal_offset']:+.1f}°", 11,
              (140, 160, 190), x=bx+220, y=by+(bh-30)//2 + 18)
    cbx = bx+bw-138-14; cby = by+(bh-_DSP_BTN_H)//2
    _action_btn(surf, cbx, cby, 138, _DSP_BTN_H, "CALIBRATE", "ok")

    # Row 3: AHRS orientation — highlight = what Pico is actually using (broadcast).
    # Tap sends $ORIENT, to Pico; Pi4 selection shown as pending until confirmed.
    pico_ori = disp.get("orientation", "right")
    sel_ori  = ss.get("orientation", pico_ori)
    if sel_ori != pico_ori:
        ori_sub = f"Direction connector faces  (AHRS: {pico_ori} — sending…)"
    else:
        ori_sub = f"Direction connector faces  (AHRS: {pico_ori})"
    bx, by, bw, bh = _setting_row(surf, 3, "ORIENTATION", ori_sub)
    opts_ori = [("forward", "FWD"), ("left", "LEFT"),
                ("right",   "RIGHT"), ("aft", "AFT")]
    seg_w = 88
    total_ori = 4 * seg_w + 3 * _DSP_BTN_G
    rx = bx + bw - total_ori - 14
    ry = by + (bh - _DSP_BTN_H) // 2
    for i, (v, lbl) in enumerate(opts_ori):
        _seg_btn(surf, rx + i * (seg_w + _DSP_BTN_G), ry, seg_w, _DSP_BTN_H,
                 lbl, v == pico_ori)   # highlight = Pico-confirmed value

    # Row 4: Mounting — same pattern: highlight = Pico-confirmed, tap pushes change.
    pico_mnt = disp.get("mounting", "normal")
    sel_mnt  = ss.get("mounting", pico_mnt)
    if sel_mnt != pico_mnt:
        mnt_sub = f"Right-side-up or inverted  (AHRS: {pico_mnt} — sending…)"
    else:
        mnt_sub = f"Right-side-up or inverted  (AHRS: {pico_mnt})"
    bx, by, bw, bh = _setting_row(surf, 4, "MOUNTING", mnt_sub)
    opts = [("normal","NORMAL"),("inverted","INVERTED")]
    total = 2*120 + _DSP_BTN_G
    rx = bx + bw - total - 14
    ry = by + (bh - _DSP_BTN_H) // 2
    for i, (v, lbl) in enumerate(opts):
        _seg_btn(surf, rx+i*(120+_DSP_BTN_G), ry, 120, _DSP_BTN_H, lbl, v==pico_mnt)

    # Row 5: Heading source (MAG / TRK / AUTO) — matches the iPhone display.
    # Sub-line documents what each option does so a pilot can pick without
    # reaching for the manual.
    bx, by, bw, bh = _setting_row(
        surf, 5, "HEADING SOURCE",
        "MAG=compass  TRK=GPS track  AUTO=TRK when moving, MAG otherwise")
    cur_src = ss.get("hdg_src", "auto")
    opts_src = [("mag", "MAG"), ("trk", "TRK"), ("auto", "AUTO")]
    seg_w = 96
    total_src = 3 * seg_w + 2 * _DSP_BTN_G
    rx = bx + bw - total_src - 14
    ry = by + (bh - _DSP_BTN_H) // 2
    for i, (v, lbl) in enumerate(opts_src):
        _seg_btn(surf, rx + i * (seg_w + _DSP_BTN_G), ry, seg_w, _DSP_BTN_H,
                 lbl, v == cur_src)

    # Row 6: Airspeed source (GPS groundspeed vs dedicated IAS sensor).
    # When IAS is selected but airdata_ok is False (sensor missing or stale),
    # the speed tape auto-falls-back to GPS GS so the display never goes blank.
    bx, by, bw, bh = _setting_row(surf, 6, "AIRSPEED SOURCE",
                                   "GPS groundspeed or IAS sensor (auto-falls back to GS without air data)")
    cur_as = ss.get("airspeed_src", "gps")
    opts_as = [("gps", "GPS GS"), ("ias", "IAS SENSOR")]
    total_as = 2*120 + _DSP_BTN_G
    rx = bx + bw - total_as - 14
    ry = by + (bh - _DSP_BTN_H) // 2
    for i, (v, lbl) in enumerate(opts_as):
        _seg_btn(surf, rx+i*(120+_DSP_BTN_G), ry, 120, _DSP_BTN_H, lbl, v == cur_as)

    # Row 7: Terrain alert inhibit — pilot-controlled mute on the
    # TAWS pipeline (terrain look-ahead + obstacle + pull-up callouts).
    # Auto-clears after 120 s so a forgotten toggle doesn't permanently
    # disable the safety net.  Sink-rate stays armed even while
    # inhibited — that's the alert that still matters on a descent.
    inh_rem = inhibit_remaining_s()
    if inh_rem > 0:
        inh_sub = f"TAWS callouts muted — {int(inh_rem)} s remaining (tap to clear)"
    else:
        inh_sub = "Mute TAWS callouts for 120 s (sink-rate stays armed)"
    bx, by, bw, bh = _setting_row(surf, 7, "TERRAIN INHIBIT", inh_sub)
    inh_w = 138
    inh_bx = bx + bw - inh_w - 14
    inh_by = by + (bh - _DSP_BTN_H) // 2
    if inh_rem > 0:
        _action_btn(surf, inh_bx, inh_by, inh_w, _DSP_BTN_H,
                    f"INHIBIT  {int(inh_rem)}s", "warn")
    else:
        _action_btn(surf, inh_bx, inh_by, inh_w, _DSP_BTN_H,
                    "INHIBIT", "normal")

    # Rows 8 & 9: PITCH / ROLL ALIGN — input-side axis alignment.  Unlike
    # the TRIM rows above (output-side static offsets), these rotate the
    # raw gyro/accel/mag before the Mahony filter, killing yaw-rate
    # coupling into pitch/roll from imperfect sensor mounting.  Pushed
    # to the Pico via $ALIGN; "sending…" hint shows until the Pico
    # echoes the new value back in its $AHRS broadcast.
    pico_pa = float(disp.get("pitch_align", 0.0))
    sel_pa  = float(ss.get("pitch_align", pico_pa))
    pa_sub  = "Yaw-coupling fix; tune until turns don't pitch the display"
    if abs(sel_pa - pico_pa) > 0.05:
        pa_sub = f"{pa_sub}  (AHRS: {pico_pa:+.1f}° — sending…)"
    bx, by, bw, bh = _setting_row(surf, 8, "PITCH ALIGN", pa_sub)
    _trim_stepper(surf, bx, by, bw, bh, sel_pa, "pitch_align")

    pico_ra = float(disp.get("roll_align", 0.0))
    sel_ra  = float(ss.get("roll_align", pico_ra))
    ra_sub  = "Yaw-coupling fix; tune until turns don't roll the display"
    if abs(sel_ra - pico_ra) > 0.05:
        ra_sub = f"{ra_sub}  (AHRS: {pico_ra:+.1f}° — sending…)"
    bx, by, bw, bh = _setting_row(surf, 9, "ROLL ALIGN", ra_sub)
    _trim_stepper(surf, bx, by, bw, bh, sel_ra, "roll_align")

    surf.set_clip(_prev_clip)


def ahrs_setup_hit(x, y, ss):
    if _back_hit(x, y):
        return "back"
    bw = DISPLAY_W - 2*_SS_MX
    total = _SS_TRIM_SW + _SS_TRIM_G + _SS_TRIM_VW + _SS_TRIM_G + _SS_TRIM_SW
    rx_trim = _SS_MX + bw - total - 14
    for ri in range(10):
        by = _ss_row_y(ri)
        if not (by <= y <= by+_SS_RH):
            continue
        bx = _SS_MX
        if ri in (0, 1, 8, 9):
            # Stepper rows: pitch/roll TRIM (output offset) on 0/1,
            # pitch/roll ALIGN (input rotation) on 8/9.  All use the
            # same ±0.1° step widget.
            key = {0: "pitch_trim", 1: "roll_trim",
                   8: "pitch_align", 9: "roll_align"}[ri]
            ry = by + (_SS_RH - _SS_TRIM_H) // 2
            if not (ry <= y <= ry+_SS_TRIM_H):
                continue
            action_prefix = "align" if ri in (8, 9) else "trim"
            if rx_trim <= x <= rx_trim+_SS_TRIM_SW:
                return f"{action_prefix}:{key}:-0.1"
            plus_x = rx_trim + _SS_TRIM_SW + _SS_TRIM_G + _SS_TRIM_VW + _SS_TRIM_G
            if plus_x <= x <= plus_x+_SS_TRIM_SW:
                return f"{action_prefix}:{key}:+0.1"
        elif ri == 2:
            cbx = _SS_MX + bw - 138 - 14
            cby = by + (_SS_RH - _DSP_BTN_H) // 2
            if cbx <= x <= cbx + 138 and cby <= y <= cby + _DSP_BTN_H:
                return "mag_cal_open"
        elif ri == 3:
            seg_w = 88
            total_o = 4 * seg_w + 3 * _DSP_BTN_G
            rx = bx + bw - total_o - 14
            ry = by + (_SS_RH - _DSP_BTN_H) // 2
            for i, v in enumerate(("forward", "left", "right", "aft")):
                xi = rx + i * (seg_w + _DSP_BTN_G)
                if xi <= x <= xi + seg_w and ry <= y <= ry + _DSP_BTN_H:
                    return f"set:orientation:{v}"
        elif ri == 4:
            total_m = 2*120 + _DSP_BTN_G
            rx = bx + bw - total_m - 14
            ry = by + (_SS_RH - _DSP_BTN_H) // 2
            for i, v in enumerate(("normal", "inverted")):
                if rx+i*(120+_DSP_BTN_G) <= x <= rx+i*(120+_DSP_BTN_G)+120:
                    if ry <= y <= ry + _DSP_BTN_H:
                        return f"set:mounting:{v}"
        elif ri == 5:
            seg_w = 96
            total_src = 3 * seg_w + 2 * _DSP_BTN_G
            rx = bx + bw - total_src - 14
            ry = by + (_SS_RH - _DSP_BTN_H) // 2
            for i, v in enumerate(("mag", "trk", "auto")):
                xi = rx + i * (seg_w + _DSP_BTN_G)
                if xi <= x <= xi + seg_w and ry <= y <= ry + _DSP_BTN_H:
                    return f"set:hdg_src:{v}"
        elif ri == 6:
            total_as = 2*120 + _DSP_BTN_G
            rx = bx + bw - total_as - 14
            ry = by + (_SS_RH - _DSP_BTN_H) // 2
            for i, v in enumerate(("gps", "ias")):
                xi = rx + i * (120 + _DSP_BTN_G)
                if xi <= x <= xi + 120 and ry <= y <= ry + _DSP_BTN_H:
                    return f"set:airspeed_src:{v}"
        elif ri == 7:
            inh_w = 138
            inh_bx = _SS_MX + bw - inh_w - 14
            inh_by = by + (_SS_RH - _DSP_BTN_H) // 2
            if (inh_bx <= x <= inh_bx + inh_w
                    and inh_by <= y <= inh_by + _DSP_BTN_H):
                return "terrain_inhibit_toggle"
    return None


# ── WiFi network scan ─────────────────────────────────────────────────────────

def _scan_wifi():
    """Return [{ssid, signal, secured}] sorted by signal desc, deduped by SSID."""
    import re
    try:
        # Kick off a fresh scan (returns immediately) then wait for results
        subprocess.run(["sudo", "nmcli", "dev", "wifi", "rescan"],
                       capture_output=True, timeout=10)
        import time; time.sleep(4)
        r = subprocess.run(
            ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY",
             "dev", "wifi", "list"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip()[:80] or "nmcli scan failed")
        seen = {}
        for line in r.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = re.split(r"(?<!\\):", line)
            ssid = parts[0].replace("\\:", ":").strip()
            if not ssid:
                continue
            try:
                signal = int(parts[1]) if len(parts) > 1 else 0
            except ValueError:
                continue
            secured = len(parts) > 2 and parts[2].strip() not in ("", "--")
            if ssid not in seen or signal > seen[ssid]["signal"]:
                seen[ssid] = {"ssid": ssid, "signal": signal, "secured": secured}
        return sorted(seen.values(), key=lambda n: n["signal"], reverse=True)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Scan timed out")
    except FileNotFoundError:
        raise RuntimeError("nmcli not found")


def _do_scan():
    disp["cs"]["scan_state"]  = "scanning"
    disp["cs"]["scan_nets"]   = []
    disp["cs"]["scan_scroll"] = 0
    disp["cs"]["scan_error"]  = ""
    disp["mode"] = "wifi_scan"
    def _worker():
        try:
            nets = _scan_wifi()
            disp["cs"]["scan_nets"]  = nets
            disp["cs"]["scan_state"] = "done"
        except Exception as e:
            disp["cs"]["scan_error"] = str(e)[:80]
            disp["cs"]["scan_state"] = "error"
    threading.Thread(target=_worker, daemon=True).start()


_WS_ITEM_H = 56
_WS_LIST_Y = 82
_WS_BTN_H  = 46


def _signal_bars(signal):
    if signal >= 75: return 4
    if signal >= 50: return 3
    if signal >= 25: return 2
    return 1


def draw_wifi_scan(surf, cs):
    ws_btn_y = DISPLAY_H - _WS_BTN_H - 8
    surf.fill((0, 8, 22))
    _screen_header(surf, "WIFI NETWORKS")
    state = cs.get("scan_state", "")
    nets  = cs.get("scan_nets", [])

    if state == "scanning":
        _text(surf, "Scanning\u2026", 22, CYAN, bold=True,
              cx=DISPLAY_W//2, cy=DISPLAY_H//2 - 16)
        _text(surf, "This may take a few seconds", 13, (110, 130, 160),
              cx=DISPLAY_W//2, cy=DISPLAY_H//2 + 16)
    elif state == "error":
        _text(surf, cs.get("scan_error", "Scan failed"), 15, (220, 80, 80),
              bold=True, cx=DISPLAY_W//2, cy=DISPLAY_H//2 - 10)
    elif state == "done":
        if not nets:
            _text(surf, "No networks found", 18, (180, 180, 180),
                  bold=True, cx=DISPLAY_W//2, cy=DISPLAY_H//2 - 10)
        else:
            _text(surf, f"{len(nets)} network{'s' if len(nets) != 1 else ''} \u2014 tap to select",
                  11, (100, 130, 160), cx=DISPLAY_W//2, cy=62)
            scroll  = cs.get("scan_scroll", 0)
            list_h  = ws_btn_y - _WS_LIST_Y - 8
            visible = list_h // _WS_ITEM_H
            for i, net in enumerate(nets[scroll:scroll + visible]):
                iy      = _WS_LIST_Y + i * _WS_ITEM_H
                row_col = (0, 22, 48) if i % 2 == 0 else (0, 16, 36)
                pygame.draw.rect(surf, row_col,
                                 (_SS_MX, iy, DISPLAY_W - 2*_SS_MX, _WS_ITEM_H - 2))
                bars    = _signal_bars(net["signal"])
                bar_col = ((60, 220, 80) if bars >= 3 else
                           (220, 180, 60) if bars == 2 else (200, 80, 80))
                bx0 = _SS_MX + 12
                for b in range(4):
                    bh  = 8 + b * 7
                    bby = iy + _WS_ITEM_H//2 + 16 - bh
                    col = bar_col if b < bars else (35, 48, 62)
                    pygame.draw.rect(surf, col, (bx0 + b * 10, bby, 7, bh))
                ssid = net["ssid"]
                if len(ssid) > 30:
                    ssid = ssid[:29] + "\u2026"
                _text(surf, ssid, 16, WHITE, bold=True, x=bx0 + 52, y=iy + 8)
                _text(surf, f"{net['signal']}%", 11, (100, 130, 160), x=bx0 + 52, y=iy + 30)
                lock_lbl = "WPA" if net["secured"] else "OPEN"
                lock_col = (200, 160, 60) if net["secured"] else (60, 200, 100)
                _text(surf, lock_lbl, 11, lock_col, bold=True,
                      x=DISPLAY_W - _SS_MX - 52, y=iy + _WS_ITEM_H//2 - 8)
            if scroll > 0:
                _text(surf, "\u25b2", 16, (100, 140, 180),
                      cx=DISPLAY_W//2, cy=_WS_LIST_Y - 16)
            if scroll + visible < len(nets):
                _text(surf, "\u25bc", 16, (100, 140, 180),
                      cx=DISPLAY_W//2, cy=ws_btn_y - 16)

    bw = DISPLAY_W - 2*_SS_MX
    _action_btn(surf, _SS_MX, ws_btn_y, bw, _WS_BTN_H, "RESCAN", "normal")


def wifi_scan_hit(x, y, cs):
    ws_btn_y = DISPLAY_H - _WS_BTN_H - 8
    if _back_hit(x, y):
        return "back"
    bw = DISPLAY_W - 2*_SS_MX
    if ws_btn_y <= y <= ws_btn_y + _WS_BTN_H and _SS_MX <= x <= _SS_MX + bw:
        return "rescan"
    if cs.get("scan_state") == "done":
        nets    = cs.get("scan_nets", [])
        scroll  = cs.get("scan_scroll", 0)
        list_h  = ws_btn_y - _WS_LIST_Y - 8
        visible = list_h // _WS_ITEM_H
        if _WS_LIST_Y - 20 <= y < _WS_LIST_Y and scroll > 0:
            return "scroll_up"
        if ws_btn_y - 20 <= y < ws_btn_y and scroll + visible < len(nets):
            return "scroll_down"
        if _WS_LIST_Y <= y < _WS_LIST_Y + visible * _WS_ITEM_H:
            idx = (y - _WS_LIST_Y) // _WS_ITEM_H + scroll
            if 0 <= idx < len(nets):
                return f"select:{idx}"
    return None


# ── Connectivity screen ───────────────────────────────────────────────────────

_CS_FIELDS = [
    ("ahrs_url",  "AHRS URL",        "Pico W access-point address"),
    ("wifi_ssid", "WiFi SSID",       "Network name to join"),
    ("wifi_pass", "WiFi PASSWORD",   "WPA2 passphrase"),
    ("notam_client_id",     "NOTAM KEY",    "FAA NMS-API key (client_id) — onboarding sheet"),
    ("notam_client_secret", "NOTAM SECRET", "FAA NMS-API secret (client_secret) — on device"),
    ("notam_env",           "NOTAM ENV",    "preprod (default) or prod"),
]
def _cs_btn_y():
    # Live button-row Y (scrolls with the content) so the SCAN/APPLY/TEST row is
    # reachable once the NOTAM fields push it past the screen's bottom edge.
    return _ss_row_y(len(_CS_FIELDS) + 2) + 4
_CS_BTN_H  = 50


def _cs_val_box(surf, bx, by, bw, bh, key, val):
    """Draw the right-side value box for a connectivity field."""
    masked = key in ("wifi_pass", "notam_client_secret") and val
    display = "\u25cf" * min(len(val), 16) if masked else val
    vbx = bx+210; vby = by+12; vbw = bx+bw-vbx-12; vbh = bh-24
    pygame.draw.rect(surf, (0,20,42), (vbx, vby, vbw, vbh), border_radius=4)
    pygame.draw.rect(surf, CYAN, (vbx, vby, vbw, vbh), width=1, border_radius=4)
    _text(surf, display or "\u2014", 12, CYAN, bold=bool(val),
          cx=vbx+vbw//2, cy=vby+vbh//2)
    _text(surf, "tap to edit", 9, (80,100,125), x=vbx+6, y=vby+vbh-13)


def draw_connectivity_setup(surf, cs):
    _screen_header(surf, "CONNECTIVITY")
    _prev_clip = _ss_clip_to_content(surf)
    bx = _SS_MX; bw = DISPLAY_W - 2*_SS_MX

    # Rows 0-2: editable fields (URL / SSID / password)
    for ri, (key, label, sub) in enumerate(_CS_FIELDS):
        bx2, by, _, bh = _setting_row(surf, ri, label, sub)
        _cs_val_box(surf, bx2, by, bw, bh, key, cs.get(key, ""))

    # Row 3: live status
    stat_ri = len(_CS_FIELDS)
    bx2, by, _, bh = _setting_row(surf, stat_ri, "STATUS", "Live connection state")
    for i, (ok_key, ok_y_lbl, ok_n_lbl) in enumerate([
            ("ahrs_ok", "AHRS  CONNECTED", "AHRS  NO LINK"),
            ("wifi_ok", "WiFi  CONNECTED", "WiFi  NO LINK")]):
        ok  = cs.get(ok_key, False)
        col = (60,220,80) if ok else (200,50,50)
        lbl = ok_y_lbl if ok else ok_n_lbl
        # When WiFi is up, show the actually-connected SSID (truncated)
        # instead of the generic "CONNECTED" text so the user can see
        # which network they're on.
        if ok and ok_key == "wifi_ok":
            actual = cs.get("wifi_actual", "")
            if actual:
                if len(actual) > 18:
                    actual = actual[:17] + "\u2026"
                lbl = f"WiFi: {actual}"
        cy  = by + bh//4 + i*bh//2
        pygame.draw.circle(surf, col, (bx2+238, cy), 6)
        _text(surf, lbl, 13, col, bold=True, x=bx2+252, y=cy-9)

    # AHRS transport diagnostics — shown on a separate row under STATUS.
    # Visible even when ahrs_ok=False so the user can tell WHY the link
    # isn't working (port open? lines parsing? specific error?).
    diag_ri = stat_ri + 1
    bx2, by, bw2, bh = _setting_row(
        surf, diag_ri, "AHRS LINK",
        f"{cs.get('ahrs_transport','?').upper()}  {cs.get('ahrs_port','')}")
    rx  = cs.get("ahrs_rx",  0)
    err = cs.get("ahrs_err", 0)
    lerr = cs.get("ahrs_last_err", "")
    _text(surf, f"RX:{rx}  ERR:{err}",
          12, (180,200,220), bold=True, x=bx2+14, y=by+bh-22)
    if lerr:
        shown = lerr if len(lerr) < 44 else lerr[:43] + "\u2026"
        _text(surf, shown, 10, (230,150,80), x=bx2+132, y=by+bh-20)

    # Live AHRS values on the right side of the row — lets the user
    # confirm sensor output is sane (e.g. RX growing but all zeros =
    # firmware alive but WT901 silent).
    live = (f"R {disp.get('roll',0):+5.1f}\u00b0  "
            f"P {disp.get('pitch',0):+5.1f}\u00b0  "
            f"Y {disp.get('yaw',0):5.1f}\u00b0  "
            f"ALT {int(disp.get('alt',0))}'")
    _text(surf, live, 11, (130,200,230), bold=True,
          x=bx2+bw2-14-_get_font(11, bold=True).size(live)[0],
          y=by+bh-20)

    # Status messages from last apply / test
    for msg, col, y_off in [
            (cs.get("apply_msg",""), (100,180,80), _cs_btn_y() - 20),
            (cs.get("test_msg",""),  (100,160,220), _cs_btn_y() - 8)]:
        if msg:
            _text(surf, msg, 10, col, cx=DISPLAY_W//2, y=y_off)

    # Action buttons (SCAN / APPLY / TEST)
    third = (bw - 20) // 3
    _action_btn(surf, bx,                _cs_btn_y(), third, _CS_BTN_H, "SCAN WIFI",  "normal")
    _action_btn(surf, bx+third+10,       _cs_btn_y(), third, _CS_BTN_H, "APPLY WIFI", "warn")
    _action_btn(surf, bx+2*(third+10),   _cs_btn_y(), third, _CS_BTN_H, "TEST AHRS",  "ok")
    surf.set_clip(_prev_clip)


def connectivity_setup_hit(x, y, cs):
    if _back_hit(x, y):
        return "back"
    bx = _SS_MX; bw = DISPLAY_W - 2*_SS_MX
    # Editable field rows
    for ri, (key, _, __) in enumerate(_CS_FIELDS):
        by = _ss_row_y(ri)
        if by <= y <= by+_SS_RH:
            vbx = bx+210
            if vbx <= x <= bx+bw-12:
                return f"edit:{key}"
    # Action buttons
    third = (bw - 20) // 3
    if _cs_btn_y() <= y <= _cs_btn_y()+_CS_BTN_H:
        if bx <= x <= bx+third:
            return "scan_wifi"
        if bx+third+10 <= x <= bx+2*third+10:
            return "apply_wifi"
        if bx+2*(third+10) <= x <= bx+3*third+20:
            return "test_ahrs"
    return None


# ── Screen Sync subscreen ─────────────────────────────────────────────────────
# One row per category (BUGS / BARO / NAV / AHRS / GPS).  Each row has two
# segmented pills: TX (publish to peers) and RX (consume from peers).  Tap
# either pill to toggle.  A peer-status header row shows whether anyone
# is on the wire so the user can confirm the link works before flipping
# their first toggle.

_SCS_KINDS = (
    ("bugs", "BUGS",     "alt / spd / hdg / vs"),
    ("baro", "BARO",     "altimeter setting"),
    ("nav",  "NAV (D2)", "waypoint ident + activation point"),
    ("ahrs", "AHRS",     "pitch / roll / yaw — pick one direction"),
    ("gps",  "GPS",      "lat / lon / alt / speed / track — pick one"),
    ("fpl",  "SHARE FPL", "flight plans + saved library sync both ways"),
)
# Stream sensors: exactly one of OFF/TX/RX.  If both TX and RX were
# on, the periodic publisher would rebroadcast each peer packet right
# back as if it were our own data, creating a feedback loop that
# jitters the display.  Bugs/baro/nav don't have this problem (they
# only publish on user edits, gated by _ssync_suppress_publish).
_SCS_MUTEX_KINDS = {"ahrs", "gps"}
# Flight plans use a single ON/OFF share toggle (both directions at once,
# no master/slave).  Echo is prevented by INSTANCE_ID dedup +
# _ssync_suppress_publish, so there's no need for separate TX/RX.
_SCS_TOGGLE_KINDS = {"fpl"}

_SCS_PILL_W = 86
_SCS_PILL_H = 36
_SCS_PILL_GAP = 6

# Layout: rows 0 (enable), 1 (transport), 2 (peer), 3 (ifaces),
# then 4..8 for the five category TX/RX rows.
_SCS_ROW_ENABLE    = 0
_SCS_ROW_TRANSPORT = 1
_SCS_ROW_PEER      = 2
_SCS_ROW_IFACES    = 3
_SCS_ROW_KINDS_OFS = 4

_SCS_TRANSPORTS = (
    ("auto", "AUTO"),
    ("usb",  "USB"),
    ("net",  "NET"),
)


def _scs_pill_rects(by, bh):
    """Return (tx_rect, rx_rect) for the right side of a sync row."""
    bw = DISPLAY_W - 2 * _SS_MX
    rx = _SS_MX + bw - _SS_MX - _SCS_PILL_W
    tx = rx - _SCS_PILL_GAP - _SCS_PILL_W
    py = by + (bh - _SCS_PILL_H) // 2
    return ((tx, py, _SCS_PILL_W, _SCS_PILL_H),
            (rx, py, _SCS_PILL_W, _SCS_PILL_H))


def _scs_mutex_rects(by, bh):
    """Return (off_rect, tx_rect, rx_rect) for the three-pill OFF/TX/RX
    selector used on AHRS and GPS rows (mutually-exclusive direction)."""
    bw  = DISPLAY_W - 2 * _SS_MX
    pw  = _SCS_PILL_W
    gap = _SCS_PILL_GAP
    rx_x  = _SS_MX + bw - _SS_MX - pw
    tx_x  = rx_x  - gap - pw
    off_x = tx_x  - gap - pw
    py = by + (bh - _SCS_PILL_H) // 2
    return ((off_x, py, pw, _SCS_PILL_H),
            (tx_x,  py, pw, _SCS_PILL_H),
            (rx_x,  py, pw, _SCS_PILL_H))


def _scs_mutex_mode(cs, kind):
    """Reduce the two stored toggles to a single 'off' | 'tx' | 'rx'
    mode.  If both were on (legacy state) we treat as TX so the user's
    publishing intent wins."""
    pub = cs.get(f"sync_publish_{kind}", False)
    con = cs.get(f"sync_consume_{kind}", False)
    if pub:
        return "tx"
    if con:
        return "rx"
    return "off"


def _scs_enable_rect(by, bh):
    """ON/OFF pill on the right side of the master-enable row."""
    bw = DISPLAY_W - 2 * _SS_MX
    pw = _SCS_PILL_W
    px = _SS_MX + bw - _SS_MX - pw
    py = by + (bh - _SCS_PILL_H) // 2
    return (px, py, pw, _SCS_PILL_H)


def _scs_transport_rects(by, bh):
    """Three segmented buttons (AUTO / USB / NET) on the transport row."""
    bw = DISPLAY_W - 2 * _SS_MX
    seg_w = 72
    gap   = _SCS_PILL_GAP
    total = 3 * seg_w + 2 * gap
    rx_right = _SS_MX + bw - _SS_MX
    base_x = rx_right - total
    py = by + (bh - _SCS_PILL_H) // 2
    rects = []
    for i in range(3):
        rects.append((base_x + i * (seg_w + gap), py, seg_w, _SCS_PILL_H))
    return rects


def draw_screen_sync_setup(surf, cs):
    _screen_header(surf, "SCREEN SYNC")
    _prev_clip = _ss_clip_to_content(surf)

    enabled = cs.get("sync_enabled", True)
    transport = cs.get("sync_transport", "auto")

    # Row 0: master enable
    bx, by, bw, bh = _setting_row(surf, _SCS_ROW_ENABLE, "SYNC",
                                   "Master enable for all screen sync")
    er = _scs_enable_rect(by, bh)
    _seg_btn(surf, *er, "ON" if enabled else "OFF", enabled)

    # Row 1: transport selector
    bx, by, bw, bh = _setting_row(surf, _SCS_ROW_TRANSPORT, "TRANSPORT",
                                   "AUTO sends on every link · USB or NET "
                                   "forces one")
    for rect, (val, lbl) in zip(_scs_transport_rects(by, bh),
                                 _SCS_TRANSPORTS):
        _seg_btn(surf, *rect, lbl, transport == val)

    # Row 2: peer status
    bx, by, bw, bh = _setting_row(surf, _SCS_ROW_PEER, "PEER",
                                   "Other PFD seen on this network")
    if _screen_sync is None or not enabled:
        n, age = 0, None
        peer_id = ""
    else:
        n, age = _screen_sync.peer_status()
        peer_id = _screen_sync.first_peer_id()
    if not enabled:
        col = (130, 130, 130)
        lbl = "SYNC OFF"
    elif n > 0:
        age_s = f"{age:.1f}s" if age is not None else "—"
        col   = (60, 220, 80)
        lbl   = f"PEER {peer_id}  ·  last {age_s} ago"
    else:
        col   = (180, 90, 90)
        lbl   = "NO PEER"
    pygame.draw.circle(surf, col, (bx + bw - 240, by + bh // 2), 7)
    _text(surf, lbl, 16, col, bold=True,
          x=bx + bw - 226, y=by + bh // 2 - 9)

    # Row 3: per-interface diagnostics — shows which links are actually
    # carrying packets so the user can see "USB rx 0 tx 142" and know
    # the gadget link isn't delivering anything from the peer.
    bx, by, bw, bh = _setting_row(surf, _SCS_ROW_IFACES, "LINKS",
                                   "Per-interface packet counts")
    stats = _screen_sync.iface_stats() if _screen_sync is not None else []
    if not stats:
        _text(surf, "(no interfaces enumerated)", 11, (140, 140, 140),
              x=bx + 14, y=by + bh - 22)
    else:
        # Two-column layout: line 1 holds the first iface, line 2 the
        # second.  Anything beyond 2 wraps onto the same line as a
        # compact tail.
        line_y = [by + 30, by + 44]
        for idx, s in enumerate(stats[:2]):
            cat = s["category"].upper()
            mark = "●" if s["eligible"] else "○"
            txt = (f"{mark} {s['name']:<6} [{cat:<3}]  "
                   f"rx {s['rx']:<5} tx {s['tx']:<5}  {s['baddr']}")
            col = (190, 210, 230) if s["eligible"] else (110, 120, 140)
            _text(surf, txt, 11, col, bold=True,
                  x=bx + 14, y=line_y[idx])
        if len(stats) > 2:
            tail = "  ".join(f"{s['name']}({s['rx']}/{s['tx']})"
                              for s in stats[2:])
            _text(surf, tail, 10, (140, 150, 170),
                  x=bx + 14, y=by + bh - 14)

    # Rows 4-8: per-category direction controls.  AHRS and GPS use a
    # mutex three-pill (OFF/TX/RX) because both-on would create a
    # publish→receive→republish echo loop on the streaming sensor
    # data.  Bugs/baro/nav stay as independent TX + RX pills (no
    # echo since they only publish on user edits).
    for i, (kind, label, sub) in enumerate(_SCS_KINDS,
                                            start=_SCS_ROW_KINDS_OFS):
        bx2, by2, bw2, bh2 = _setting_row(surf, i, label, sub)
        if kind in _SCS_TOGGLE_KINDS:
            _, rect = _scs_pill_rects(by2, bh2)   # single ON/OFF (rightmost)
            on = cs.get("sync_fpl_enabled", True)
            _seg_btn(surf, *rect, "ON" if on else "OFF", on)
        elif kind in _SCS_MUTEX_KINDS:
            mode = _scs_mutex_mode(cs, kind)
            off_r, tx_r, rx_r = _scs_mutex_rects(by2, bh2)
            _seg_btn(surf, *off_r, "OFF", mode == "off")
            _seg_btn(surf, *tx_r,  "TX",  mode == "tx")
            _seg_btn(surf, *rx_r,  "RX",  mode == "rx")
        else:
            tx_rect, rx_rect = _scs_pill_rects(by2, bh2)
            _seg_btn(surf, *tx_rect, "TX",
                     cs.get(f"sync_publish_{kind}", False))
            _seg_btn(surf, *rx_rect, "RX",
                     cs.get(f"sync_consume_{kind}", False))

    surf.set_clip(_prev_clip)


def screen_sync_setup_hit(x, y, cs):
    if 8 <= x <= 80 and 6 <= y <= 37:
        return "back"

    # Master enable
    by = _ss_row_y(_SCS_ROW_ENABLE)
    if by <= y <= by + _SS_RH:
        ex, ey, ew, eh = _scs_enable_rect(by, _SS_RH)
        if ex <= x <= ex + ew and ey <= y <= ey + eh:
            return "toggle_enable"

    # Transport selector
    by = _ss_row_y(_SCS_ROW_TRANSPORT)
    if by <= y <= by + _SS_RH:
        for rect, (val, _lbl) in zip(_scs_transport_rects(by, _SS_RH),
                                      _SCS_TRANSPORTS):
            tx, ty, tw, th = rect
            if tx <= x <= tx + tw and ty <= y <= ty + th:
                return f"transport:{val}"

    # Per-category direction controls.  Mutex kinds (AHRS, GPS) emit
    # set_mode:<kind>:<off|tx|rx>; the others emit toggle_publish /
    # toggle_consume as before.
    for i, (kind, _, _sub) in enumerate(_SCS_KINDS,
                                         start=_SCS_ROW_KINDS_OFS):
        by = _ss_row_y(i)
        if not (by <= y <= by + _SS_RH):
            continue
        if kind in _SCS_TOGGLE_KINDS:
            _, rect = _scs_pill_rects(by, _SS_RH)
            rx_, ry_, rw_, rh_ = rect
            if rx_ <= x <= rx_ + rw_ and ry_ <= y <= ry_ + rh_:
                return "toggle_fpl_share"
            continue
        if kind in _SCS_MUTEX_KINDS:
            off_r, tx_r, rx_r = _scs_mutex_rects(by, _SS_RH)
            for rect, m in ((off_r, "off"), (tx_r, "tx"), (rx_r, "rx")):
                rx_, ry_, rw_, rh_ = rect
                if (rx_ <= x <= rx_ + rw_
                        and ry_ <= y <= ry_ + rh_):
                    return f"set_mode:{kind}:{m}"
            continue
        tx_rect, rx_rect = _scs_pill_rects(by, _SS_RH)
        tx_x, tx_y, tx_w, tx_h = tx_rect
        rx_x, rx_y, rx_w, rx_h = rx_rect
        if tx_x <= x <= tx_x + tx_w and tx_y <= y <= tx_y + tx_h:
            return f"toggle_publish:{kind}"
        if rx_x <= x <= rx_x + rx_w and rx_y <= y <= rx_y + rx_h:
            return f"toggle_consume:{kind}"
    return None


# ── System screen ─────────────────────────────────────────────────────────────

_SYS_VERSION = "0.1.0"
_SYS_BUILD   = "2026-05-17"   # bump on each meaningful PFD release
_SYS_INFO_Y  = 56
_SYS_INFO_LH = 26


_SYS_N_LINES = 7
_SYS_IH      = _SYS_N_LINES * _SYS_INFO_LH + 16
_SYS_MODE_Y    = _SYS_INFO_Y + _SYS_IH + 8        # ENABLE MFD row top
# Data-download tiles moved to the DATA & MAPS page; action buttons follow the
# ENABLE MFD row directly now.
_SYS_BTN_Y     = _SYS_MODE_Y + _SS_RH + 8         # action buttons top
_SYS_BTN_H     = 54


def _sys_data_tile(surf, bx, by, bw, bh, label, sub, active=True):
    """Half-width tappable tile for data download rows (terrain / obstacle)."""
    # Background gradient
    for i in range(bh):
        t = 1.0 - i / bh
        if active:
            c = (int(t*8), int(12+t*18), int(28+t*35))
        else:
            c = (int(t*5), int(t*7), int(t*12))
        pygame.draw.line(surf, c, (bx, by+i), (bx+bw, by+i))
    bc = (55,75,105) if active else (28,35,48)
    pygame.draw.rect(surf, bc, (bx, by, bw, bh), width=1, border_radius=4)
    lc = WHITE if active else (55,62,72)
    sc = (100,120,145) if active else (42,48,58)
    _text(surf, label, 13, lc, bold=True, x=bx+12, y=by+10)
    _text(surf, sub,   11, sc,             x=bx+12, y=by+28)
    if active:
        _text(surf, "\u25b6", 16, (60,80,110), x=bx+bw-22, y=by+(bh-18)//2)
    else:
        _text(surf, "future", 10, (48,55,65), x=bx+bw-50, y=by+bh-18)


def draw_system_setup(surf):
    _screen_header(surf, "SYSTEM")
    _prev_clip = _ss_clip_to_content(surf)
    bx = _SS_MX; bw = DISPLAY_W - 2*_SS_MX
    _gps_ok   = disp.get("gps_ok", False)
    _gps_comm = disp.get("gps_comm", False)
    _gps_sats = int(disp.get("sats", 0) or 0)
    if _gps_ok:
        _gps_status = f"fix \u00b7 {_gps_sats} sat{'s' if _gps_sats != 1 else ''}"
    elif _gps_comm:
        _gps_status = "no fix \u00b7 acquiring"
    else:
        _gps_status = "no signal"
    _ahrs_fw = disp.get("fw_ver", "\u2014")
    if not _ahrs_fw or _ahrs_fw == "\u2014":
        _ahrs_fw = "unknown" if disp.get("ahrs_ok") else "no link"
    lines = [
        ("PFD version",       _SYS_VERSION),
        ("PFD build date",    _SYS_BUILD),
        ("AHRS firmware",     str(_ahrs_fw)),
        ("Display",           f"{DISPLAY_W}\u00d7{DISPLAY_H}  HDMI"),
        ("Hardware",          "Pi 4 + Pico 2W  (OpenGL SVT)"),
        ("GPS",               _gps_status),
        ("SRTM terrain data", "loaded" if os.path.isdir(SRTM_DIR) else "not found"),
    ]
    pygame.draw.rect(surf, (0,12,32), (bx, _SYS_INFO_Y, bw, _SYS_IH), border_radius=6)
    pygame.draw.rect(surf, (55,75,105), (bx, _SYS_INFO_Y, bw, _SYS_IH), width=1, border_radius=6)
    for i, (k, v) in enumerate(lines):
        ty = _SYS_INFO_Y + 10 + i*_SYS_INFO_LH
        _text(surf, k, 12, (120,140,165), x=bx+14, y=ty)
        _text(surf, v, 13, WHITE, bold=True, x=bx+310, y=ty)

    # DISPLAY MODE row — PFD / MFD (also switchable by the 3-finger hold).
    _setting_row(surf, 0, "DISPLAY MODE", "Primary Flight Display or Multi-Function Display  ·  3-finger hold swaps",
                 _y_override=_SYS_MODE_Y)
    cur = disp.get("display_mode", "pfd")
    btn_h_m = _DSP_BTN_H; btn_w_m = 110; gap_m = _DSP_BTN_G
    rx = bx + bw - 2*(btn_w_m+gap_m) + gap_m - 14
    ry = _SYS_MODE_Y + (_SS_RH - btn_h_m) // 2
    _seg_btn(surf, rx, ry, btn_w_m, btn_h_m, "PFD", cur == "pfd")
    _seg_btn(surf, rx+btn_w_m+gap_m, ry, btn_w_m, btn_h_m, "MFD", cur == "mfd")

    # (Data-download tiles moved to the DATA & MAPS setup page.)
    half_w = (bw - 10) // 2
    # FIRMWARE first (right after terrain row), then SIMULATOR/RESET, then QUIT
    sim_y  = _SYS_BTN_Y + _SYS_BTN_H + 10
    quit_y = sim_y       + _SYS_BTN_H + 10
    _action_btn(surf, bx,           _SYS_BTN_Y, bw,     _SYS_BTN_H, "AHRS FIRMWARE",  "normal")
    _action_btn(surf, bx,           sim_y,      half_w, _SYS_BTN_H, "SIMULATOR",       "ok")
    _action_btn(surf, bx+half_w+10, sim_y,      half_w, _SYS_BTN_H, "RESET DEFAULTS",  "danger")
    _action_btn(surf, bx,           quit_y,     bw,     _SYS_BTN_H, "QUIT PFD",        "danger")
    surf.set_clip(_prev_clip)


def _sys_mode_btn_rects():
    """(pfd_rect, mfd_rect) for the DISPLAY MODE segmented buttons — shared
    by draw + hit-test so they stay aligned."""
    bx = _SS_MX; bw = DISPLAY_W - 2*_SS_MX
    btn_h_m = _DSP_BTN_H; btn_w_m = 110; gap_m = _DSP_BTN_G
    rx = bx + bw - 2*(btn_w_m+gap_m) + gap_m - 14
    ry = _SYS_MODE_Y + (_SS_RH - btn_h_m) // 2
    return ((rx, ry, btn_w_m, btn_h_m),
            (rx+btn_w_m+gap_m, ry, btn_w_m, btn_h_m))


def system_setup_hit(x, y):
    if _back_hit(x, y):
        return "back"
    _pfd_r, _mfd_r = _sys_mode_btn_rects()

    def _in(rc):
        return rc[0] <= x <= rc[0]+rc[2] and rc[1] <= y <= rc[1]+rc[3]
    if _in(_pfd_r):
        return "mode_pfd"
    if _in(_mfd_r):
        return "mode_mfd"
    bx = _SS_MX; bw = DISPLAY_W - 2*_SS_MX
    half_w = (bw - 10) // 2
    sim_y  = _SYS_BTN_Y + _SYS_BTN_H + 10
    quit_y = sim_y       + _SYS_BTN_H + 10
    if _SYS_BTN_Y <= y <= _SYS_BTN_Y+_SYS_BTN_H and bx <= x <= bx+bw:
        return "ahrs_firmware"
    if sim_y <= y <= sim_y+_SYS_BTN_H:
        if bx <= x <= bx+half_w:
            return "simulator"
        if bx+half_w+10 <= x <= bx+half_w+10+half_w:
            return "reset_defaults"
    if quit_y <= y <= quit_y+_SYS_BTN_H and bx <= x <= bx+bw:
        return "quit"
    return None


# ── AHRS firmware update screen ───────────────────────────────────────────────

_FW_DIR     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "firmware")
_FW_SCRIPTS = ["main.py", "config.py", "web_server.py", "wt901.py",
               "bme280.py", "gps.py", "airdata.py", "sdp31.py",
               "ms4525.py", "ahrs_filter.py"]
_IPHONE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "iphone_display")
_FW_WEB     = ["index.html", "terrain.js", "sw.js", "manifest.webmanifest", "icon-192.png"]
_FW_ROW_H   = 72
_FW_Y0      = 52


_pico_serial_cache  = (0.0, None)   # (timestamp, result)
_pico_bootsel_cache = (0.0, None)
_PICO_CACHE_TTL = 2.0  # seconds between lsblk / glob rescans


def _find_pico_serial():
    """Return first /dev/ttyACM* or /dev/ttyUSB* path, or None (cached 2 s)."""
    global _pico_serial_cache
    now = time.time()
    if now - _pico_serial_cache[0] < _PICO_CACHE_TTL:
        return _pico_serial_cache[1]
    import glob
    result = None
    for pat in ["/dev/ttyACM*", "/dev/ttyUSB*"]:
        ports = sorted(glob.glob(pat))
        if ports:
            result = ports[0]
            break
    _pico_serial_cache = (now, result)
    return result


def _find_pico_bootsel():
    """Return the Pico BOOTSEL mount path, auto-mounting via udisksctl
    if needed (cached 2 s).  Handles both chip families:
        Pico W  (RP2040)  → label "RPI-RP2"
        Pico 2W (RP2350)  → label "RP2350"
    """
    global _pico_bootsel_cache
    now = time.time()
    if now - _pico_bootsel_cache[0] < _PICO_CACHE_TTL:
        return _pico_bootsel_cache[1]
    import glob
    _LABELS = ("RPI-RP2", "RP2350")
    # Check standard mount paths first (must be an actual mountpoint, not a stale dir)
    _mount_pats = [p
                   for lbl in _LABELS
                   for p in (f"/media/*/{lbl}",
                             f"/run/media/*/{lbl}",
                             f"/mnt/{lbl}")]
    for pat in _mount_pats:
        mounts = [m for m in glob.glob(pat) if os.path.ismount(m)]
        if mounts:
            _pico_bootsel_cache = (now, mounts[0])
            return mounts[0]
    # Not mounted — look for the block device by label and mount it
    try:
        r = subprocess.run(
            ["lsblk", "-o", "NAME,LABEL", "--noheadings", "--raw"],
            capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] in _LABELS:
                label   = parts[1]
                devpath = f"/dev/{parts[0]}"
                # Try udisksctl first, fall back to sudo mount
                mr = subprocess.run(["udisksctl", "mount", "-b", devpath],
                                    capture_output=True, timeout=10)
                if mr.returncode != 0:
                    subprocess.run(["sudo", "mkdir", "-p", f"/mnt/{label}"],
                                   capture_output=True, timeout=5)
                    uid = os.getuid(); gid = os.getgid()
                    subprocess.run(["sudo", "mount", "-o", f"uid={uid},gid={gid}",
                                    devpath, f"/mnt/{label}"],
                                   capture_output=True, timeout=10)
                for pat in _mount_pats:
                    mounts = [m for m in glob.glob(pat) if os.path.ismount(m)]
                    if mounts:
                        _pico_bootsel_cache = (now, mounts[0])
                        return mounts[0]
    except Exception:
        pass
    _pico_bootsel_cache = (now, None)
    return None


def _find_uf2():
    """Return first .uf2 file in firmware dir, or None."""
    import glob
    files = sorted(glob.glob(os.path.join(_FW_DIR, "*.uf2")))
    return files[0] if files else None


def _do_push_scripts():
    disp["fw"]["push_state"] = "pushing"
    disp["fw"]["push_msg"]   = "Starting…"
    def _worker():
        global _sse_client
        # If the AHRS client is holding the USB serial port open, release it
        # so mpremote can connect.  We restart a fresh client after the push.
        released_serial_port = None
        prev_client = _sse_client
        try:
            from serial_client import SerialClient as _SC
            if isinstance(prev_client, _SC):
                released_serial_port = prev_client.port
                disp["fw"]["push_msg"] = "Releasing serial port…"
                prev_client.stop()
                _sse_client = None
                global _pico_serial_cache
                _pico_serial_cache = (0.0, None)
                time.sleep(1.5)  # allow port to close
        except ImportError:
            pass

        port = _find_pico_serial()
        if not port:
            disp["fw"]["push_msg"]   = "Pico not detected — check USB cable"
            disp["fw"]["push_state"] = "error"
            return
        # Build mpremote chain: cp file1 :file1 + cp file2 :file2 + ... + reset
        cmd = ["python3", "-m", "mpremote", "connect", port]
        first = True
        all_files = (
            [(name, os.path.join(_FW_DIR, name))     for name in _FW_SCRIPTS] +
            [(name, os.path.join(_IPHONE_DIR, name)) for name in _FW_WEB]
        )
        for name, src_path in all_files:
            if not os.path.isfile(src_path):
                continue
            if not first:
                cmd.append("+")
            first = False
            cmd += ["cp", src_path, f":{name}"]
        cmd += ["+", "reset"]
        try:
            disp["fw"]["push_msg"] = "Copying files…"
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                err = (r.stderr or r.stdout).strip()
                disp["fw"]["push_msg"]   = err[:80] if err else "mpremote failed"
                disp["fw"]["push_state"] = "error"
            else:
                disp["fw"]["push_msg"]   = "All scripts pushed — Pico rebooting"
                disp["fw"]["push_state"] = "done"
        except FileNotFoundError:
            disp["fw"]["push_msg"]   = "mpremote not found — pip3 install mpremote --break-system-packages"
            disp["fw"]["push_state"] = "error"
        except subprocess.TimeoutExpired:
            disp["fw"]["push_msg"]   = "Timed out — check connection"
            disp["fw"]["push_state"] = "error"
        except Exception as e:
            disp["fw"]["push_msg"]   = str(e)[:80]
            disp["fw"]["push_state"] = "error"
        finally:
            # Restart serial client if we stopped it (Pico reboots after reset)
            if released_serial_port and _sse_client is None:
                try:
                    from serial_client import SerialClient as _SC
                    time.sleep(3)  # wait for Pico to reboot and re-enumerate
                    new_client = _SC(released_serial_port, state, _state_lock)
                    new_client.start()
                    _sse_client = new_client
                    disp["cs"]["ahrs_transport"] = "usb"
                    disp["cs"]["ahrs_port"]      = released_serial_port
                except Exception:
                    pass
    threading.Thread(target=_worker, daemon=True).start()



def _do_flash_uf2():
    disp["fw"]["flash_state"] = "flashing"
    disp["fw"]["flash_msg"]   = "Starting…"
    def _worker():
        uf2 = _find_uf2()
        if not uf2:
            disp["fw"]["flash_msg"]   = "No .uf2 file in firmware/ — add one first"
            disp["fw"]["flash_state"] = "error"
            return
        mount = _find_pico_bootsel()
        if not mount:
            disp["fw"]["flash_msg"]   = "Pico BOOTSEL not mounted — hold BOOTSEL then plug USB"
            disp["fw"]["flash_state"] = "error"
            return
        try:
            import shutil
            dest = os.path.join(mount, os.path.basename(uf2))
            disp["fw"]["flash_msg"] = f"Writing {os.path.basename(uf2)}…"
            shutil.copy2(uf2, dest)
            disp["fw"]["flash_msg"]   = "Flash complete — Pico will reboot"
            disp["fw"]["flash_state"] = "done"
            global _pico_bootsel_cache; _pico_bootsel_cache = (0.0, None)
        except Exception as e:
            disp["fw"]["flash_msg"]   = str(e)[:80]
            disp["fw"]["flash_state"] = "error"
    threading.Thread(target=_worker, daemon=True).start()


def draw_ahrs_firmware(surf):
    _screen_header(surf, "AHRS FIRMWARE")
    _prev_clip = _ss_clip_to_content(surf)
    bx = _SS_MX; bw = DISPLAY_W - 2*_SS_MX
    fw = disp["fw"]

    # ── Device status row ──────────────────────────────────────────────────────
    row_y = _FW_Y0
    pygame.draw.rect(surf, (0,12,32), (bx, row_y, bw, _FW_ROW_H), border_radius=6)
    pygame.draw.rect(surf, (40,55,80), (bx, row_y, bw, _FW_ROW_H), width=1, border_radius=6)
    _text(surf, "DEVICE STATUS", 11, (100,130,160), bold=True, x=bx+14, y=row_y+8)

    serial = _find_pico_serial()
    s_col  = (60,220,80) if serial else (90,100,115)
    s_lbl  = serial if serial else "not detected"
    pygame.draw.circle(surf, s_col, (bx+22, row_y+36), 6)
    _text(surf, f"USB Serial:  {s_lbl}", 13, s_col, bold=bool(serial), x=bx+34, y=row_y+27)

    bootsel = _find_pico_bootsel()
    b_col   = (60,220,80) if bootsel else (90,100,115)
    b_lbl   = bootsel if bootsel else "not mounted"
    pygame.draw.circle(surf, b_col, (bx + bw//2 + 8, row_y+36), 6)
    _text(surf, f"BOOTSEL:  {b_lbl}", 13, b_col, bold=bool(bootsel),
          x=bx + bw//2 + 20, y=row_y+27)

    # ── Push scripts row ───────────────────────────────────────────────────────
    row_y += _FW_ROW_H + 8
    pygame.draw.rect(surf, (0,12,32), (bx, row_y, bw, _FW_ROW_H), border_radius=6)
    pygame.draw.rect(surf, (40,55,80), (bx, row_y, bw, _FW_ROW_H), width=1, border_radius=6)
    _text(surf, "PUSH SCRIPTS", 13, WHITE, bold=True, x=bx+14, y=row_y+8)
    files_lbl = "  ".join(_FW_SCRIPTS)
    if _get_font(10).size(files_lbl)[0] > bw - 28:
        files_lbl = "main.py  config.py  web_server.py  + 3 more"
    _text(surf, files_lbl, 10, (100,130,160), x=bx+14, y=row_y+28)
    ps = fw.get("push_state", "")
    pm = fw.get("push_msg",   "")
    p_col = (60,220,80) if ps=="done" else (220,80,80) if ps=="error" else (180,180,100)
    if pm:
        _text(surf, pm, 11, p_col, bold=True, x=bx+14, y=row_y+50)

    # ── Flash .uf2 row ─────────────────────────────────────────────────────────
    row_y += _FW_ROW_H + 8
    pygame.draw.rect(surf, (0,12,32), (bx, row_y, bw, _FW_ROW_H), border_radius=6)
    pygame.draw.rect(surf, (40,55,80), (bx, row_y, bw, _FW_ROW_H), width=1, border_radius=6)
    _text(surf, "FLASH MICROPYTHON  (.uf2)", 13, WHITE, bold=True, x=bx+14, y=row_y+8)
    uf2 = _find_uf2()
    uf2_lbl = os.path.basename(uf2) if uf2 else "no .uf2 found in firmware/"
    _text(surf, uf2_lbl, 10, (100,130,160) if uf2 else (160,100,60), x=bx+14, y=row_y+28)
    fs = fw.get("flash_state", "")
    fm = fw.get("flash_msg",   "")
    f_col = (60,220,80) if fs=="done" else (220,80,80) if fs=="error" else (180,180,100)
    if fm:
        _text(surf, fm, 11, f_col, bold=True, x=bx+14, y=row_y+50)
    else:
        _text(surf, "Hold BOOTSEL + connect USB, then tap FLASH .UF2",
              10, (110,125,145), x=bx+14, y=row_y+50)

    # ── Action buttons ─────────────────────────────────────────────────────────
    btn_y  = row_y + _FW_ROW_H + 14
    half   = (bw - 10) // 2
    push_style = "normal" if fw.get("push_state") != "pushing" else "warn"
    flash_style = "normal" if fw.get("flash_state") != "flashing" else "warn"
    _action_btn(surf, bx,          btn_y, half, 54, "PUSH SCRIPTS TO PICO", push_style)
    _action_btn(surf, bx+half+10,  btn_y, half, 54, "FLASH .UF2",           flash_style)
    surf.set_clip(_prev_clip)


def ahrs_firmware_hit(x, y):
    if _back_hit(x, y):
        return "back"
    bx = _SS_MX; bw = DISPLAY_W - 2*_SS_MX
    btn_y = _FW_Y0 + 3 * (_FW_ROW_H + 8) + 14
    half  = (bw - 10) // 2
    if btn_y <= y <= btn_y + 54:
        if bx <= x <= bx + half:
            return "push_scripts"
        if bx + half + 10 <= x <= bx + half + 10 + half:
            return "flash_uf2"
    return None


# ── Terrain data screen ──────────────────────────────────────────────────────

# Download source: Mapzen/Nextzen AWS public bucket — .hgt.gz, no auth required
_SRTM_BASE = "https://elevation-tiles-prod.s3.amazonaws.com/skadi"

# Preset download regions: (label, subtitle, lat_min, lat_max, lon_min, lon_max)
_TD_REGIONS = [
    ("US Southwest", "AZ \u00b7 NM \u00b7 NV \u00b7 UT \u00b7 CO",   31, 42, -115, -103),
    ("US Pacific",   "CA \u00b7 OR \u00b7 WA",                         32, 49, -125, -114),
    ("US Southeast", "FL \u00b7 GA \u00b7 AL \u00b7 NC \u00b7 SC",    24, 37,  -92,  -74),
    ("US Northeast", "NY \u00b7 PA \u00b7 NE states",                  37, 48,  -80,  -66),
    ("US Midwest",   "OH \u00b7 MI \u00b7 IL \u00b7 MN \u00b7 WI",    37, 49, -103,  -80),
    ("All CONUS",    "Lower 48 \u2014 ~2 GB",                          24, 49, -125,  -66),
    ("Alaska",       "Southern AK corridor",                            55, 64, -165, -131),
    ("Europe West",  "UK \u00b7 FR \u00b7 DE \u00b7 ES \u00b7 IT",    36, 58,   -9,   15),
    ("All Europe",   "UK to Turkey \u2014 ~3 GB",                      35, 60,  -12,   30),
]

_TD_COLS = 2
_TD_MX   = 12
_TD_MY   = 84      # top of region grid (below title bar + status strip)
_TD_GAP  = 8


def _td_tile_name(lat, lon):
    """Return the standard HGT filename for a 1°×1° tile by its SW corner."""
    ns = "N" if lat >= 0 else "S"
    ew = "W" if lon < 0 else "E"
    return f"{ns}{abs(lat):02d}{ew}{abs(lon):03d}.hgt"


def _td_tile_url(lat, lon):
    """Return (url, local_filename) for a tile."""
    ns = "N" if lat >= 0 else "S"
    fname_gz = _td_tile_name(lat, lon) + ".gz"
    folder   = f"{ns}{abs(lat):02d}"
    return f"{_SRTM_BASE}/{folder}/{fname_gz}"


def _td_tiles_for_region(lat_min, lat_max, lon_min, lon_max):
    """Enumerate all 1°×1° tile SW-corner coords for a lat/lon bounding box."""
    tiles = []
    for lat in range(lat_min, lat_max):
        for lon in range(lon_min, lon_max):
            tiles.append((lat, lon))
    return tiles


def _td_disk_stats():
    """Return (tile_count, total_mb) of HGT files in SRTM_DIR."""
    if not os.path.isdir(SRTM_DIR):
        return 0, 0.0
    total = 0
    for f in os.listdir(SRTM_DIR):
        if f.endswith(".hgt"):
            total += os.path.getsize(os.path.join(SRTM_DIR, f))
    count = sum(1 for f in os.listdir(SRTM_DIR) if f.endswith(".hgt"))
    return count, total / 1_048_576


def _td_region_tile_count(region):
    """Return number of tiles for a preset region."""
    _, _, lat_min, lat_max, lon_min, lon_max = region
    return (lat_max - lat_min) * (lon_max - lon_min)


def _td_download_thread(tiles, region_name):
    """Background download of a list of (lat, lon) tiles."""
    td = disp["td"]
    td["downloading"] = True
    td["dl_region"]   = region_name
    td["dl_total"]    = len(tiles)
    td["dl_current"]  = 0
    td["dl_cancel"]   = False
    os.makedirs(SRTM_DIR, exist_ok=True)
    ok = skip = err = 0
    for i, (lat, lon) in enumerate(tiles):
        if td["dl_cancel"]:
            td["dl_status"] = f"Cancelled  ({ok} new, {skip} skipped)"
            td["downloading"] = False
            return
        td["dl_current"] = i
        fname = _td_tile_name(lat, lon)
        dest  = os.path.join(SRTM_DIR, fname)
        if os.path.exists(dest):
            skip += 1
            td["dl_status"] = f"Skipping {fname}"
            continue
        url = _td_tile_url(lat, lon)
        td["dl_status"] = f"Downloading {fname}\u2026"
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                gz_data = resp.read()
            with gzip.open(io.BytesIO(gz_data)) as gz_f:
                hgt_data = gz_f.read()
            with open(dest, "wb") as f:
                f.write(hgt_data)
            ok += 1
        except Exception as exc:
            td["dl_status"] = f"Error {fname}: {exc}"
            err += 1
    td["dl_current"] = len(tiles)
    td["dl_status"]  = (f"Done \u2713  {ok} downloaded"
                        + (f", {skip} skipped" if skip else "")
                        + (f", {err} errors"   if err  else ""))
    td["downloading"] = False
    global _has_terrain
    _has_terrain = _check_terrain()


def _td_start_download(region):
    """Kick off a background download for a preset region."""
    label, sub, lat_min, lat_max, lon_min, lon_max = region
    tiles = _td_tiles_for_region(lat_min, lat_max, lon_min, lon_max)
    t = threading.Thread(target=_td_download_thread,
                         args=(tiles, label), daemon=True)
    t.start()


def _td_start_current_area():
    """Download tiles around the current GPS position (±2° box)."""
    lat = int(disp.get("lat", DEMO_LAT))
    lon = int(disp.get("lon", DEMO_LON))
    tiles = _td_tiles_for_region(lat-2, lat+3, lon-2, lon+3)
    t = threading.Thread(target=_td_download_thread,
                         args=(tiles, "Current Area"), daemon=True)
    t.start()


# ── Water-mask download ───────────────────────────────────────────────────────
# Companion to the SRTM download flow: pulls the Natural Earth 10m ocean +
# lakes shapefiles once (~12 MB combined), then for each existing .hgt tile
# rasterises a 1201×1201 binary water mask in-process using pyshp + pygame's
# C-based polygon fill.  Runs in a daemon thread so the UI stays responsive;
# status reported via disp["wd"].
#
# Speed vs the old gdal_rasterize subprocess flow:
#   - shapefiles parsed ONCE per process (cached at module level), not once
#     per tile per shapefile.
#   - pygame.draw.polygon is a single C scanline fill, no subprocess startup.
#   - bbox prefilter discards inland tiles in microseconds.
# Typical 25-tile "current area": old ~3 minutes, new ~5 seconds.

_WD_NE_FILES   = ("ne_10m_ocean", "ne_10m_lakes")
_WD_NE_PRIMARY = "https://naciscdn.org/naturalearth/10m/physical"
_WD_NE_MIRROR  = ("https://github.com/nvkelso/natural-earth-vector/"
                  "raw/master/zips/10m_physical")
_WD_TILE_RES   = 1201

# Per-shapefile cache of (bbox, [ring, ...]) tuples; built lazily on first use.
_wd_shapes_cache = {}


def _wd_shapes_dir():
    """Where Natural Earth .shp files live (alongside data/srtm/)."""
    return os.path.join(os.path.dirname(SRTM_DIR), "natural_earth")


def _wd_ensure_shapefiles(wd):
    """Download + unzip the Natural Earth shapefiles if not present.
    Returns the list of shapefile names found, or None on failure."""
    import zipfile as _zipfile
    sdir = _wd_shapes_dir()
    os.makedirs(sdir, exist_ok=True)
    found = []
    for name in _WD_NE_FILES:
        shp_path = os.path.join(sdir, name + ".shp")
        if os.path.exists(shp_path):
            found.append(name)
            continue
        zip_path = os.path.join(sdir, name + ".zip")
        wd["dl_status"] = f"Downloading {name}.zip…"
        urls = (f"{_WD_NE_PRIMARY}/{name}.zip",
                f"{_WD_NE_MIRROR}/{name}.zip")
        ok = False
        for url in urls:
            try:
                with urllib.request.urlopen(url, timeout=30) as resp:
                    blob = resp.read()
                with open(zip_path, "wb") as f:
                    f.write(blob)
                ok = True
                break
            except Exception as e:
                wd["dl_status"] = f"  retry: {e}"
        if not ok:
            wd["dl_status"] = f"Failed to download {name}.zip"
            return None
        try:
            with _zipfile.ZipFile(zip_path) as z:
                z.extractall(sdir)
            os.remove(zip_path)
        except Exception as e:
            wd["dl_status"] = f"Unzip failed: {e}"
            return None
        if not os.path.exists(shp_path):
            wd["dl_status"] = f"{name}.shp missing after unzip"
            return None
        found.append(name)
    return found


def _wd_load_shapes(name, wd):
    """Return cached parsed shapefile for `name`.  Cached as a dict with
    flat numpy arrays (much smaller + faster than nested Python lists),
    persisted to disk as .npz so subsequent runs skip the slow shapefile
    parse entirely:

        {'points':       (N, 2) float32,    flat list of all (lon, lat)
         'ring_starts':  (M+1,) int32,       indices into points[] per ring
         'ring_bboxes':  (M, 4) float32}     (lon_min, lat_min, lon_max, lat_max)

    Pure numpy means the worker thread never has to touch pygame/SDL
    (which has global locks that can deadlock the render thread when a
    huge polygon is being filled in C).
    """
    import numpy as _np
    if name in _wd_shapes_cache:
        return _wd_shapes_cache[name]

    sdir = _wd_shapes_dir()
    npz_path = os.path.join(sdir, name + ".npz")

    # Fast path: previously-parsed cache on disk.
    if os.path.exists(npz_path):
        wd["dl_status"] = f"Loading cached {name}…"
        try:
            with _np.load(npz_path, allow_pickle=False) as z:
                cache = {
                    "points":      z["points"],
                    "ring_starts": z["ring_starts"],
                    "ring_bboxes": z["ring_bboxes"],
                }
            _wd_shapes_cache[name] = cache
            return cache
        except Exception:
            pass   # fall through to re-parse

    # Slow path: parse the .shp.  Big shapefiles (ocean has 600 K points)
    # can take 30 s the first time; cache to .npz makes the second run
    # instant.
    import shapefile as _shapefile   # pyshp
    shp_path = os.path.join(sdir, name + ".shp")
    wd["dl_status"] = f"Parsing {name}.shp (one-time, ~30 s)…"

    all_pts = []
    ring_starts = [0]
    ring_bboxes = []
    cum = 0
    with _shapefile.Reader(shp_path) as sf:
        for shp in sf.iterShapes():
            parts = list(shp.parts) + [len(shp.points)]
            for i in range(len(parts) - 1):
                ring = shp.points[parts[i]:parts[i + 1]]
                if len(ring) < 3:
                    continue
                arr = _np.asarray(ring, dtype=_np.float32)
                all_pts.append(arr)
                cum += len(arr)
                ring_starts.append(cum)
                ring_bboxes.append((arr[:, 0].min(), arr[:, 1].min(),
                                    arr[:, 0].max(), arr[:, 1].max()))

    if not all_pts:
        cache = {
            "points":      _np.zeros((0, 2), dtype=_np.float32),
            "ring_starts": _np.zeros((1,), dtype=_np.int32),
            "ring_bboxes": _np.zeros((0, 4), dtype=_np.float32),
        }
    else:
        cache = {
            "points":      _np.concatenate(all_pts, axis=0).astype(_np.float32),
            "ring_starts": _np.asarray(ring_starts, dtype=_np.int32),
            "ring_bboxes": _np.asarray(ring_bboxes, dtype=_np.float32),
        }

    # Persist cache for next time.
    try:
        _np.savez(npz_path,
                  points=cache["points"],
                  ring_starts=cache["ring_starts"],
                  ring_bboxes=cache["ring_bboxes"])
    except OSError:
        pass

    _wd_shapes_cache[name] = cache
    return cache


# ── Natural Earth boundary lines ──────────────────────────────────────────────
# Companion to the water-mask download.  Pulls Natural Earth's 10m cultural
# shapefiles (admin-1 states/provinces; admin-0 countries), parses each into
# a flat polyline cache the inset can scan with bbox culling, persists the
# parsed result as a small .npz alongside the water shapefiles so subsequent
# boots load in ~10 ms.

_NE_PRIMARY    = "https://naciscdn.org/naturalearth/10m/cultural"
_NE_MIRROR     = ("https://github.com/nvkelso/natural-earth-vector/"
                  "raw/master/zips/10m_cultural")

_SL_NE_NAME    = "ne_10m_admin_1_states_provinces"
_SL_NPZ_NAME   = _SL_NE_NAME + "_lines.npz"
_CL_NE_NAME    = "ne_10m_admin_0_countries"
_CL_NPZ_NAME   = _CL_NE_NAME + "_lines.npz"


def _ne_ensure_shapefile(wd, ne_name):
    """Download + unzip a Natural Earth cultural shapefile if missing.
    Status text reuses disp["wd"] so it shows in the water-mask progress."""
    import zipfile as _zipfile
    sdir = _wd_shapes_dir()
    os.makedirs(sdir, exist_ok=True)
    shp_path = os.path.join(sdir, ne_name + ".shp")
    if os.path.exists(shp_path):
        return shp_path
    zip_path = os.path.join(sdir, ne_name + ".zip")
    wd["dl_status"] = f"Downloading {ne_name}.zip…"
    urls = (f"{_NE_PRIMARY}/{ne_name}.zip",
            f"{_NE_MIRROR}/{ne_name}.zip")
    for url in urls:
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                blob = resp.read()
            with open(zip_path, "wb") as f:
                f.write(blob)
            break
        except Exception as e:
            wd["dl_status"] = f"  {ne_name} retry: {e}"
    else:
        wd["dl_status"] = f"Failed to download {ne_name}.zip"
        return None
    try:
        with _zipfile.ZipFile(zip_path) as z:
            z.extractall(sdir)
        os.remove(zip_path)
    except Exception as e:
        wd["dl_status"] = f"Unzip failed: {e}"
        return None
    return shp_path if os.path.exists(shp_path) else None


def _ne_build_cache(wd, ne_name, npz_name):
    """Parse a Natural Earth shapefile into a flat polyline cache and persist
    as .npz.  Returns the cache dict, or None on failure.

        {'points':     (N, 2) float32   flat (lon, lat) for every vertex
         'seg_starts': (M+1,) int32     indices into points[] per polyline
         'seg_bboxes': (M, 4) float32   (lon_min, lat_min, lon_max, lat_max)}

    Borders ship as polygons in the shapefile; each ring's vertices are
    copied verbatim so the renderer can stroke it as a closed polyline.
    No simplification — admin_1 is ~3000 rings / ~500 K points, admin_0
    is much smaller (~250 rings); bbox culling skips out-of-view rings
    in microseconds either way."""
    import numpy as _np
    try:
        import shapefile as _shapefile
    except ImportError:
        wd["dl_status"] = ("Install pyshp: sudo pip3 install "
                           "--break-system-packages pyshp")
        return None

    shp_path = _ne_ensure_shapefile(wd, ne_name)
    if shp_path is None:
        return None

    sdir = _wd_shapes_dir()
    npz_path = os.path.join(sdir, npz_name)

    wd["dl_status"] = f"Parsing {ne_name}.shp (one-time)…"
    all_pts, seg_starts, seg_bboxes = [], [0], []
    cum = 0
    with _shapefile.Reader(shp_path) as sf:
        for shp in sf.iterShapes():
            parts = list(shp.parts) + [len(shp.points)]
            for i in range(len(parts) - 1):
                ring = shp.points[parts[i]:parts[i + 1]]
                if len(ring) < 2:
                    continue
                arr = _np.asarray(ring, dtype=_np.float32)
                all_pts.append(arr)
                cum += len(arr)
                seg_starts.append(cum)
                seg_bboxes.append((arr[:, 0].min(), arr[:, 1].min(),
                                   arr[:, 0].max(), arr[:, 1].max()))

    if not all_pts:
        return None
    cache = {
        "points":     _np.concatenate(all_pts, axis=0).astype(_np.float32),
        "seg_starts": _np.asarray(seg_starts, dtype=_np.int32),
        "seg_bboxes": _np.asarray(seg_bboxes, dtype=_np.float32),
    }
    try:
        _np.savez(npz_path,
                  points=cache["points"],
                  seg_starts=cache["seg_starts"],
                  seg_bboxes=cache["seg_bboxes"])
    except OSError:
        pass
    return cache


def _ne_load_cache(npz_name):
    """Load a parsed Natural Earth .npz if it exists.  Returns the cache
    dict or None.  Called once at startup and again after the worker
    finishes a fresh download/parse."""
    import numpy as _np
    sdir = _wd_shapes_dir()
    npz_path = os.path.join(sdir, npz_name)
    if not os.path.exists(npz_path):
        return None
    try:
        with _np.load(npz_path, allow_pickle=False) as z:
            return {
                "points":     z["points"],
                "seg_starts": z["seg_starts"],
                "seg_bboxes": z["seg_bboxes"],
            }
    except Exception:
        return None


_state_lines   = None  # admin_1; populated on startup by _ne_load_cache;
                       # rebound after the water/boundary worker finishes.
_country_lines = None  # admin_0 — same lifecycle as _state_lines.


def _wd_fill_ring(out, ring_xy_px):
    """Burn one polygon ring (N×2 float pixel coords) into out (H, W)
    uint8 array via numpy scanline fill.  Even–odd fill rule means
    polygon-with-holes (e.g. ocean cut by continents) renders correctly
    when outer + inner rings are passed in.
    """
    import numpy as _np
    n = len(ring_xy_px)
    if n < 3:
        return
    H, W = out.shape

    x1 = ring_xy_px[:, 0]
    y1 = ring_xy_px[:, 1]
    x2 = _np.roll(x1, -1)
    y2 = _np.roll(y1, -1)

    # Edge prefilter: drop edges entirely above or below the tile in Y.
    # Do NOT prefilter in X — closed polygons whose perimeter lies far
    # east/west of the tile (e.g. the Pacific Ocean ring's antimeridian
    # and Asian-coast edges for an offshore CONUS tile) still contribute
    # scanline-crossing parity.  Their xs values land outside [0, W) and
    # are clipped during fill, but keeping them is what makes the
    # even–odd count even on every scanline.
    keep = ~(((y1 < 0) & (y2 < 0)) | ((y1 >= H) & (y2 >= H)))
    if not keep.any():
        return
    x1 = x1[keep]; y1 = y1[keep]
    x2 = x2[keep]; y2 = y2[keep]

    y_min = max(0, int(_np.floor(min(y1.min(), y2.min()))))
    y_max = min(H - 1, int(_np.ceil(max(y1.max(), y2.max()))))
    if y_min > y_max:
        return

    for y in range(y_min, y_max + 1):
        # Edges crossing scanline y (top-inclusive, bottom-exclusive)
        crosses = ((y1 <= y) & (y < y2)) | ((y2 <= y) & (y < y1))
        if not crosses.any():
            continue
        with _np.errstate(divide="ignore", invalid="ignore"):
            xs = x1[crosses] + (y - y1[crosses]) * \
                 (x2[crosses] - x1[crosses]) / (y2[crosses] - y1[crosses])
        xs = _np.sort(xs)
        # Even–odd fill between successive intersection pairs
        for k in range(0, len(xs) - 1, 2):
            x_start = max(0, int(_np.ceil(xs[k])))
            x_end   = min(W, int(xs[k + 1]) + 1)
            if x_start < x_end:
                out[y, x_start:x_end] ^= 1   # XOR for even–odd


def _wd_rasterise_tile(lat_int, lon_int, shape_names, wd):
    """Build a (res×res) uint8 0/1 water mask for the 1°×1° tile at
    (lat_int, lon_int).  Pure numpy — no pygame/SDL on the worker
    thread, so the main render loop stays unblocked even on a huge
    polygon."""
    import numpy as _np
    res = _WD_TILE_RES
    out = _np.zeros((res, res), dtype=_np.uint8)

    tile_lon0 = float(lon_int)
    tile_lon1 = float(lon_int + 1)
    tile_lat0 = float(lat_int)
    tile_lat1 = float(lat_int + 1)
    scale = float(res - 1)

    for name in shape_names:
        cache = _wd_load_shapes(name, wd)
        ring_starts = cache["ring_starts"]
        bboxes      = cache["ring_bboxes"]
        points      = cache["points"]
        if len(bboxes) == 0:
            continue

        # Vectorised per-ring bbox prefilter
        keep_rings = ~((bboxes[:, 2] < tile_lon0) | (bboxes[:, 0] > tile_lon1) |
                       (bboxes[:, 3] < tile_lat0) | (bboxes[:, 1] > tile_lat1))
        ring_idx = _np.flatnonzero(keep_rings)
        if ring_idx.size == 0:
            continue

        for ri in ring_idx:
            s = ring_starts[ri]
            e = ring_starts[ri + 1]
            ring = points[s:e]
            # Convert lon/lat → pixel coords (col 0 = west, row 0 = north).
            xy = _np.empty_like(ring)
            xy[:, 0] = (ring[:, 0] - tile_lon0) * scale
            xy[:, 1] = (tile_lat1 - ring[:, 1]) * scale
            _wd_fill_ring(out, xy)

    # XOR fill produced 0/1 pixels but inner rings (continents inside
    # ocean) flip back to 0, which is what we want — water=1 only.
    return out


def _wd_existing_srtm_tiles():
    """Enumerate (lat_int, lon_int) for every .hgt file in SRTM_DIR."""
    tiles = []
    if not os.path.isdir(SRTM_DIR):
        return tiles
    for f in os.listdir(SRTM_DIR):
        if not f.endswith(".hgt") or len(f) < 11:
            continue
        try:
            ns = f[0]
            lat_int = int(f[1:3]) * (1 if ns == "N" else -1)
            ew = f[3]
            lon_int = int(f[4:7]) * (1 if ew == "E" else -1)
        except ValueError:
            continue
        tiles.append((lat_int, lon_int))
    return sorted(tiles)


def _wd_target_tiles(buffer_deg=1):
    """SRTM tiles plus a `buffer_deg` border in every direction.

    Adding buffer tiles means the rasteriser also produces water masks
    for offshore tiles where the SRTM coverage stops (e.g. just east of
    the Florida coast, or west of California).  Those tiles have no
    .hgt file but a valid .water mask — Natural Earth's ocean polygon
    fills them entirely with water — so the SVT outer mesh paints
    Pacific/Atlantic blue instead of defaulting to flat brown."""
    have = set(_wd_existing_srtm_tiles())
    extended = set(have)
    for lat_int, lon_int in have:
        for dlat in range(-buffer_deg, buffer_deg + 1):
            for dlon in range(-buffer_deg, buffer_deg + 1):
                la = lat_int + dlat
                lo = lon_int + dlon
                if -90 <= la <= 89 and -180 <= lo <= 179:
                    extended.add((la, lo))
    return sorted(extended)


def _wd_download_thread():
    """Background worker: download Natural Earth + rasterise per-tile masks."""
    from water import save_tile, _tile_key as _water_tile_key

    wd = disp["wd"]
    wd["downloading"] = True
    wd["dl_cancel"]   = False
    wd["dl_current"]  = 0
    wd["dl_total"]    = 0

    # Pure-python path: needs pyshp.  If missing, tell the user how to
    # install it without forcing them to install gdal-bin (heavy + slow).
    try:
        import shapefile  # noqa: F401
    except ImportError:
        wd["dl_status"] = ("Install pyshp: sudo pip3 install "
                           "--break-system-packages pyshp")
        wd["downloading"] = False
        return

    wd["dl_status"] = "Loading Natural Earth shapefiles…"
    found = _wd_ensure_shapefiles(wd)
    if found is None:
        wd["downloading"] = False
        return

    tiles = _wd_target_tiles(buffer_deg=1)
    if not tiles:
        wd["dl_status"] = "No SRTM tiles on disk — download terrain first"
        wd["downloading"] = False
        return

    os.makedirs(WATER_DIR, exist_ok=True)
    wd["dl_total"] = len(tiles)
    ok = skip = err = 0
    try:
        for i, (lat_int, lon_int) in enumerate(tiles):
            if wd["dl_cancel"]:
                wd["dl_status"] = (f"Cancelled  ({ok} new, "
                                   f"{skip} skipped, {err} errors)")
                return
            wd["dl_current"] = i
            key = _water_tile_key(lat_int, lon_int)
            out_path = os.path.join(WATER_DIR, key)
            if os.path.exists(out_path):
                skip += 1
                continue
            wd["dl_status"] = f"Rasterising {key}…"
            try:
                arr = _wd_rasterise_tile(lat_int, lon_int, found, wd)
                save_tile(out_path, arr)
                ok += 1
            except Exception as e:
                wd["dl_status"] = f"{key}: {e}"
                err += 1
        wd["dl_current"] = len(tiles)
        wd["dl_status"]  = (f"Done ✓  {ok} new"
                            + (f", {skip} skipped" if skip else "")
                            + (f", {err} errors"   if err   else ""))

        # State (admin_1) + country (admin_0) boundary polylines.
        # Downloaded + parsed once alongside the water shapefiles so the inset
        # can paint regional context at the wider zoom levels.  Best-effort:
        # if pyshp / network fail the rest of the water flow still completes.
        global _state_lines, _country_lines
        if _ne_load_cache(_SL_NPZ_NAME) is None:
            wd["dl_status"] = "Fetching state boundaries…"
            built = _ne_build_cache(wd, _SL_NE_NAME, _SL_NPZ_NAME)
            if built is not None:
                _state_lines = built
                wd["dl_status"] = (f"Done ✓  {ok} new"
                                   + (f", {skip} skipped" if skip else "")
                                   + (f", {err} errors"   if err   else "")
                                   + "  · state lines ready")
        else:
            _state_lines = _ne_load_cache(_SL_NPZ_NAME)

        if _ne_load_cache(_CL_NPZ_NAME) is None:
            wd["dl_status"] = "Fetching country boundaries…"
            built = _ne_build_cache(wd, _CL_NE_NAME, _CL_NPZ_NAME)
            if built is not None:
                _country_lines = built
                wd["dl_status"] = (f"Done ✓  {ok} new"
                                   + (f", {skip} skipped" if skip else "")
                                   + (f", {err} errors"   if err   else "")
                                   + "  · country lines ready")
        else:
            _country_lines = _ne_load_cache(_CL_NPZ_NAME)
    finally:
        wd["downloading"] = False


def _wd_start_download():
    """Kick off the water-mask rasterise in a daemon thread."""
    if disp["wd"]["downloading"]:
        return
    t = threading.Thread(target=_wd_download_thread, daemon=True,
                         name="WaterMaskDL")
    t.start()


# ── Obstacle data download ─────────────────────────────────────────────────────

def _od_load_obstacles():
    """(Re-)load the obstacle cache into module-level _obstacles."""
    import datetime
    global _obstacles
    os.makedirs(OBSTACLE_DIR, exist_ok=True)
    arr = obs_mod.load(OBSTACLE_DIR)
    _obstacles = arr
    cnt, mb = obs_mod.disk_stats(OBSTACLE_DIR)
    disp["od"]["records"] = cnt
    disp["od"]["used_mb"] = mb
    dl_date = obs_mod.download_date(OBSTACLE_DIR)
    disp["od"]["dl_date"] = dl_date
    if dl_date is not None:
        age = (datetime.date.today() - dl_date).days
        disp["od"]["age_days"] = age
        disp["od"]["expired"]  = age > OBSTACLE_EXPIRY_DAYS
    else:
        disp["od"]["age_days"] = 0
        disp["od"]["expired"]  = False


def _od_download_thread():
    """Background thread: download DOF ZIP, extract DAT, parse cache."""
    import zipfile
    import tempfile

    od = disp["od"]
    od["downloading"] = True
    od["dl_cancel"]   = False
    od["dl_status"]   = "Connecting to FAA\u2026"

    os.makedirs(OBSTACLE_DIR, exist_ok=True)
    dat_path   = os.path.join(OBSTACLE_DIR, obs_mod.DOF_FILENAME)
    cache_path = os.path.join(OBSTACLE_DIR, obs_mod.CACHE_FILENAME)

    try:
        # ── Download ZIP ──────────────────────────────────────────────────────
        od["dl_status"] = "Downloading DAILY_DOF_DAT.ZIP\u2026"
        req = urllib.request.Request(
            obs_mod.DOF_ZIP_URL,
            headers={"User-Agent": "PFD-AHRS/1.0"}
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            chunk_size  = 65536
            buf = io.BytesIO()
            while True:
                if od["dl_cancel"]:
                    od["dl_status"]   = "Cancelled"
                    od["downloading"] = False
                    return
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                buf.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = int(downloaded * 100 / total)
                    od["dl_status"] = f"Downloading\u2026 {pct}%  ({downloaded//1024} / {total//1024} KB)"
                else:
                    od["dl_status"] = f"Downloading\u2026 {downloaded//1024} KB"

        # ── Extract DAT ───────────────────────────────────────────────────────
        od["dl_status"] = "Extracting\u2026"
        buf.seek(0)
        with zipfile.ZipFile(buf) as zf:
            dat_name = next((n for n in zf.namelist()
                             if n.upper().endswith(".DAT")), None)
            if dat_name is None:
                od["dl_status"]   = "Error: no DAT in ZIP"
                od["downloading"] = False
                return
            with zf.open(dat_name) as src, open(dat_path, "wb") as dst:
                dst.write(src.read())

        # ── Parse and cache ───────────────────────────────────────────────────
        od["dl_status"] = "Parsing obstacle records\u2026"
        od["parsing"]   = True
        _od_load_obstacles()
        od["parsing"]   = False

        cnt = disp["od"]["records"]
        od["dl_status"] = f"Done \u2713  {cnt:,} obstacles loaded"

    except Exception as exc:
        od["dl_status"] = f"Error: {exc}"
    finally:
        od["downloading"] = False


def _od_start_download():
    t = threading.Thread(target=_od_download_thread, daemon=True,
                         name="ObstacleDownload")
    t.start()


# ── Obstacle data screen ──────────────────────────────────────────────────────

_OD_MX = 12   # horizontal margin

def draw_obstacle_data(surf, od):
    """Full-screen obstacle data management screen."""
    surf.fill((0, 0, 0))
    _screen_header(surf, "OBSTACLE DATA")
    bx = _OD_MX; bw = DISPLAY_W - 2*_OD_MX

    cnt      = od.get("records", 0)
    used_mb  = od.get("used_mb", 0.0)

    # ── Status strip ─────────────────────────────────────────────────────────
    pygame.draw.rect(surf, (0,12,32), (bx, 52, bw, 28), border_radius=4)
    pygame.draw.rect(surf, (40,60,90), (bx, 52, bw, 28), width=1, border_radius=4)
    if cnt:
        stat_str = f"{cnt:,} obstacles  \u00b7  {used_mb:.1f} MB on disk"
        stat_col = (60, 220, 80)
    else:
        stat_str = "No obstacle data on disk"
        stat_col = YELLOW
    _text(surf, stat_str, 12, stat_col, bold=True, cx=DISPLAY_W//2, cy=66)

    downloading = od.get("downloading", False)
    parsing     = od.get("parsing", False)

    # ── Info panel ────────────────────────────────────────────────────────────
    info_y = 92
    info_h = 90
    pygame.draw.rect(surf, (0,10,26), (bx, info_y, bw, info_h), border_radius=6)
    pygame.draw.rect(surf, (40,55,80), (bx, info_y, bw, info_h), width=1, border_radius=6)
    _text(surf, "FAA Digital Obstacle File (DOF)", 13, WHITE, bold=True,
          cx=DISPLAY_W//2, cy=info_y+16)
    _text(surf, "Covers all US obstacles > 200 ft AGL (towers, antennas, wind turbines\u2026)",
          10, (140,160,185), cx=DISPLAY_W//2, cy=info_y+34)
    _text(surf, "Single file \u2248 15\u201320 MB \u00b7 Updated every 28 days by the FAA",
          10, (120,140,165), cx=DISPLAY_W//2, cy=info_y+50)
    _text(surf, "Displayed on AI as red/amber symbols within 10 nm and \u00b12000 ft",
          10, (120,140,165), cx=DISPLAY_W//2, cy=info_y+66)
    _text(surf, "WiFi (home network) required for download",
          10, (160,130,60), cx=DISPLAY_W//2, cy=info_y+80)

    # ── Download / Update button ──────────────────────────────────────────────
    btn_y = info_y + info_h + 14
    btn_h = 54
    if downloading or parsing:
        bg = (0,20,10); oc = (40,140,60)
    else:
        bg = (0,18,45); oc = WHITE
    pygame.draw.rect(surf, bg, (bx, btn_y, bw, btn_h), border_radius=6)
    gh = btn_h // 5
    if not (downloading or parsing):
        for i in range(gh):
            t2 = 1.0 - i/gh
            gc = (int(15+t2*25), int(20+t2*40), int(40+t2*65))
            pygame.draw.line(surf, gc, (bx+6, btn_y+1+i), (bx+bw-6, btn_y+1+i))
    pygame.draw.rect(surf, oc, (bx, btn_y, bw, btn_h), width=2, border_radius=6)
    btn_label = "UPDATE" if cnt else "DOWNLOAD"
    tc = (70,80,90) if (downloading or parsing) else WHITE
    _text(surf, btn_label, 15, tc, bold=True, cx=DISPLAY_W//2, cy=btn_y+btn_h//2-8)
    sub = "DAILY_DOF_DAT.ZIP  from  aeronav.faa.gov"
    _text(surf, sub, 10, (100,120,140) if not (downloading or parsing) else (60,80,70),
          cx=DISPLAY_W//2, cy=btn_y+btn_h//2+10)

    # ── Progress / status area ────────────────────────────────────────────────
    prog_y = btn_y + btn_h + 10
    prog_h = 48
    pygame.draw.rect(surf, (0,10,24), (bx, prog_y, bw, prog_h), border_radius=6)
    pygame.draw.rect(surf, (35,50,75), (bx, prog_y, bw, prog_h), width=1, border_radius=6)

    status_msg = od.get("dl_status", "")
    if downloading:
        # Parse percentage out of status message for progress bar
        pct = 0
        try:
            if "%" in status_msg:
                pct = int(status_msg.split("%")[0].split()[-1])
        except (ValueError, IndexError):
            pct = 0
        bar_w = int((bw - 20) * pct / 100)
        pygame.draw.rect(surf, (0,22,12), (bx+10, prog_y+28, bw-20, 10), border_radius=3)
        if bar_w > 0:
            pygame.draw.rect(surf, (40,180,60), (bx+10, prog_y+28, bar_w, 10), border_radius=3)
        _text(surf, status_msg, 10, (140,160,180), cx=DISPLAY_W//2, cy=prog_y+16)
        # CANCEL
        _action_btn(surf, bw-80, prog_y+6, 72, 32, "CANCEL", "danger", r=5)
    elif parsing:
        _text(surf, status_msg, 10, (140,180,140), cx=DISPLAY_W//2, cy=prog_y+24)
    else:
        col = (60,220,80) if status_msg.startswith("Done") else (160,160,170)
        _text(surf, status_msg, 10, col, cx=DISPLAY_W//2, cy=prog_y+24)

    # ── Clearance legend ──────────────────────────────────────────────────────
    leg_y = prog_y + prog_h + 12
    leg_h = 34
    pygame.draw.rect(surf, (0,8,20), (bx, leg_y, bw, leg_h), border_radius=4)
    _text(surf, "Clearance legend:", 10, (120,140,165), x=bx+10, y=leg_y+8)
    for dx, col, lbl in [(120, RED, "< 100 ft  WARNING"),
                          (270, YELLOW, "< 500 ft  CAUTION"),
                          (430, WHITE,       "> 500 ft  CLEAR")]:
        pygame.draw.rect(surf, col, (bx+dx, leg_y+8, 10, 10))
        _text(surf, lbl, 9, (160,170,180), x=bx+dx+14, y=leg_y+8)


def obstacle_data_hit(x, y, od):
    """Return action string or None."""
    if _back_hit(x, y):
        return "back"
    bx = _OD_MX; bw = DISPLAY_W - 2*_OD_MX
    btn_y = 92 + 90 + 14   # info_y + info_h + 14
    btn_h = 54
    # Cancel during download
    if od.get("downloading"):
        prog_y = btn_y + btn_h + 10
        if (bx+bw-80 <= x <= bx+bw and prog_y+6 <= y <= prog_y+38):
            return "cancel"
    # Download/Update button
    if bx <= x <= bx+bw and btn_y <= y <= btn_y+btn_h:
        return "download"
    return None


# ── Airport data download ─────────────────────────────────────────────────────

_AD_MX = 12   # horizontal margin for airport data screen

def _ad_load_airports():
    """(Re-)load the airport and runway caches into module-level arrays."""
    import airports as apt_mod
    global _airports, _runways
    os.makedirs(AIRPORT_DIR, exist_ok=True)
    _airports = apt_mod.load(AIRPORT_DIR)
    _runways  = rwy_mod.load(AIRPORT_DIR)
    cnt, mb = apt_mod.disk_stats(AIRPORT_DIR)
    # Sum runway cache size into the total disk usage reported
    rcnt, rmb = rwy_mod.disk_stats(AIRPORT_DIR)
    disp["ad"]["records"] = cnt
    disp["ad"]["used_mb"] = mb + rmb
    disp["ad"]["runway_count"] = rcnt
    dl_date = apt_mod.download_date(AIRPORT_DIR)
    disp["ad"]["dl_date"] = dl_date
    if dl_date is not None:
        import datetime as _dt
        age = (_dt.date.today() - dl_date).days
        disp["ad"]["age_days"] = age
        disp["ad"]["expired"]  = age > AIRPORT_EXPIRY_DAYS
    else:
        disp["ad"]["age_days"] = 0
        disp["ad"]["expired"]  = False


def _ad_download_thread():
    """Background thread: download OurAirports CSV, parse, cache."""
    import airports as apt_mod

    ad = disp["ad"]
    ad["downloading"] = True
    ad["dl_cancel"]   = False
    ad["dl_status"]   = "Connecting to OurAirports\u2026"

    os.makedirs(AIRPORT_DIR, exist_ok=True)

    def _download_file(url, path, label):
        """Stream-download to path.tmp, atomic rename on success, update
        status with percentage.  Honours ad['dl_cancel']."""
        ad["dl_status"] = f"Downloading {label}\u2026"
        req = urllib.request.Request(url, headers={"User-Agent": "PFD-AHRS/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            with open(path + ".tmp", "wb") as out:
                while True:
                    if ad["dl_cancel"]:
                        try: os.remove(path + ".tmp")
                        except Exception: pass
                        return False
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    out.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = int(downloaded * 100 / total)
                        ad["dl_status"] = f"{label}: {pct}%  ({downloaded//1024} / {total//1024} KB)"
                    else:
                        ad["dl_status"] = f"{label}: {downloaded//1024} KB"
        os.replace(path + ".tmp", path)
        return True

    csv_apt   = os.path.join(AIRPORT_DIR, apt_mod.CSV_FILENAME)
    csv_rwy   = os.path.join(AIRPORT_DIR, rwy_mod.CSV_FILENAME)
    cache_apt = os.path.join(AIRPORT_DIR, apt_mod.CACHE_FILENAME)
    cache_rwy = os.path.join(AIRPORT_DIR, rwy_mod.CACHE_FILENAME)

    try:
        if not _download_file(apt_mod.AIRPORTS_CSV_URL, csv_apt, "airports.csv"):
            ad["dl_status"]   = "Cancelled"
            ad["downloading"] = False
            return
        if not _download_file(rwy_mod.RUNWAYS_CSV_URL, csv_rwy, "runways.csv"):
            ad["dl_status"]   = "Cancelled"
            ad["downloading"] = False
            return

        # Invalidate both caches so parser rebuilds fresh
        for p in (cache_apt, cache_rwy):
            try: os.remove(p)
            except Exception: pass

        ad["dl_status"] = "Parsing airport + runway records\u2026"
        ad["parsing"]   = True
        _ad_load_airports()
        ad["parsing"]   = False

        cnt = ad["records"]
        rwy_cnt = ad.get("runway_count", 0)
        ad["dl_status"] = f"Done \u2713  {cnt:,} airports, {rwy_cnt:,} runways"

    except Exception as exc:
        ad["dl_status"] = f"Error: {exc}"
    finally:
        ad["downloading"] = False


def _ad_start_download():
    t = threading.Thread(target=_ad_download_thread, daemon=True,
                         name="AirportDownload")
    t.start()


# ── Nav-data (FAA NASR + CIFP) download / status ────────────────────────────
_ND_MX = 12


def _nd_load():
    """(Re-)load the nav-data cache into _navdata + refresh disp["nd"] stats."""
    global _navdata
    os.makedirs(NAVDATA_DIR, exist_ok=True)
    _navdata = nd_mod.load(NAVDATA_DIR)
    st = nd_mod.cache_stats(NAVDATA_DIR)
    disp["nd"].update({k: st[k] for k in (
        "present", "cycle", "issued", "fixes", "navaids", "airways", "procedures",
        "holds", "mb", "date", "age_days", "expired")})


def _nd_download_thread():
    nd = disp["nd"]
    nd["downloading"] = True
    nd["dl_cancel"]   = False
    os.makedirs(NAVDATA_DIR, exist_ok=True)
    if nd_mod.download_url(nd_mod.JSON_FILE) is None:
        nd["dl_status"]   = "No download source configured"
        nd["downloading"] = False
        return

    def _download_file(url, path, label):
        nd["dl_status"] = f"Downloading {label}…"
        req = urllib.request.Request(url, headers={"User-Agent": "PFD-AHRS/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            got = 0
            with open(path + ".tmp", "wb") as out:
                while True:
                    if nd["dl_cancel"]:
                        try: os.remove(path + ".tmp")
                        except Exception: pass
                        return False
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    out.write(chunk)
                    got += len(chunk)
                    if total:
                        nd["dl_status"] = (f"{label}: {got*100//total}%  "
                                           f"({got//1024} / {total//1024} KB)")
                    else:
                        nd["dl_status"] = f"{label}: {got//1024} KB"
        os.replace(path + ".tmp", path)
        return True

    try:
        for fname in nd_mod.DOWNLOAD_FILES:
            url = nd_mod.download_url(fname)
            if not _download_file(url, os.path.join(NAVDATA_DIR, fname), fname):
                nd["dl_status"]   = "Cancelled"
                nd["downloading"] = False
                return
        nd["dl_status"] = "Loading nav data…"
        _nd_load()
        nd["dl_status"] = f"Done ✓  cycle {disp['nd'].get('cycle') or '—'}"
    except Exception as exc:
        nd["dl_status"] = f"Error: {exc}"
    finally:
        nd["downloading"] = False


def _nd_start_download():
    t = threading.Thread(target=_nd_download_thread, daemon=True,
                         name="NavDataDownload")
    t.start()


def draw_navdata_data(surf, nd):
    """Full-screen IFR nav-data management screen (fixes/navaids/procedures)."""
    surf.fill((0, 0, 0))
    _screen_header(surf, "NAV DATA")
    bx = _ND_MX; bw = DISPLAY_W - 2*_ND_MX
    present = nd.get("present", False)

    pygame.draw.rect(surf, (0,12,32), (bx, 52, bw, 28), border_radius=4)
    pygame.draw.rect(surf, (40,60,90), (bx, 52, bw, 28), width=1, border_radius=4)
    if present:
        age = nd.get("age_days", 0)
        age_str = f"  ·  {age} day{'' if age == 1 else 's'} old"
        if nd.get("expired"):
            age_str += "  (expired)"
            stat_col = (220, 130, 60)
        else:
            stat_col = (60, 220, 80)
        _iss = nd.get('issued') or ''
        _iss_str = f"issued {_iss}  ·  " if _iss else ""
        stat_str = (f"cycle {nd.get('cycle') or '—'}  ·  {_iss_str}"
                    f"{nd.get('mb', 0.0):.1f} MB{age_str}")
    else:
        stat_str = "No nav data on disk"
        stat_col = YELLOW
    _text(surf, stat_str, 12, stat_col, bold=True, cx=DISPLAY_W//2, cy=66)

    info_y = 92
    info_h = 90
    pygame.draw.rect(surf, (0,10,26), (bx, info_y, bw, info_h), border_radius=6)
    pygame.draw.rect(surf, (40,55,80), (bx, info_y, bw, info_h), width=1, border_radius=6)
    _text(surf, "FAA IFR Nav Data  (US · 28-day cycle)", 13, WHITE, bold=True,
          cx=DISPLAY_W//2, cy=info_y+16)
    _text(surf, f"{nd.get('fixes',0):,} fixes  ·  {nd.get('navaids',0):,} "
                f"navaids  ·  {nd.get('airways',0):,} airways",
          10, (140,160,185), cx=DISPLAY_W//2, cy=info_y+34)
    _text(surf, f"{nd.get('procedures',0):,} procedures  ·  "
                f"{nd.get('holds',0):,} holds",
          10, (140,160,185), cx=DISPLAY_W//2, cy=info_y+50)
    _text(surf, "Built off-aircraft: tools/build_navdata_us.py (NASR + CIFP)",
          10, (120,140,165), cx=DISPLAY_W//2, cy=info_y+66)
    _text(surf, "Powers published approaches, airways & holds",
          10, (120,140,165), cx=DISPLAY_W//2, cy=info_y+80)

    have_src = nd_mod.download_url(nd_mod.JSON_FILE) is not None
    downloading = nd.get("downloading", False)
    btn_y = info_y + info_h + 14
    btn_h = 54
    if downloading:
        bg = (0,20,10); oc = (40,140,60)
    elif have_src:
        bg = (0,18,45); oc = WHITE
    else:
        bg = (10,10,14); oc = (60,70,85)
    pygame.draw.rect(surf, bg, (bx, btn_y, bw, btn_h), border_radius=6)
    gh = btn_h // 5
    if have_src and not downloading:
        for i in range(gh):
            t2 = 1.0 - i/gh
            gc = (int(15+t2*25), int(20+t2*40), int(40+t2*65))
            pygame.draw.line(surf, gc, (bx+6, btn_y+1+i), (bx+bw-6, btn_y+1+i))
    pygame.draw.rect(surf, oc, (bx, btn_y, bw, btn_h), width=2, border_radius=6)
    btn_label = "UPDATE" if present else "DOWNLOAD"
    tc = (70,80,90) if (downloading or not have_src) else WHITE
    _text(surf, btn_label, 15, tc, bold=True, cx=DISPLAY_W//2, cy=btn_y+btn_h//2-8)
    sub = ("from configured nav-data source" if have_src
           else "no source configured — copy cache to data/navdata/")
    _text(surf, sub, 10, (100,120,140) if have_src else (150,120,60),
          cx=DISPLAY_W//2, cy=btn_y+btn_h//2+10)

    prog_y = btn_y + btn_h + 10
    prog_h = 48
    pygame.draw.rect(surf, (0,10,24), (bx, prog_y, bw, prog_h), border_radius=6)
    pygame.draw.rect(surf, (35,50,75), (bx, prog_y, bw, prog_h), width=1, border_radius=6)
    status_msg = nd.get("dl_status", "")
    if downloading:
        pct = 0
        try:
            if "%" in status_msg:
                pct = int(status_msg.split("%")[0].split()[-1])
        except (ValueError, IndexError):
            pct = 0
        bar_w = int((bw - 20) * pct / 100)
        pygame.draw.rect(surf, (0,22,12), (bx+10, prog_y+28, bw-20, 10), border_radius=3)
        if bar_w > 0:
            pygame.draw.rect(surf, (40,180,60), (bx+10, prog_y+28, bar_w, 10), border_radius=3)
        _text(surf, status_msg, 10, (140,160,180), cx=DISPLAY_W//2, cy=prog_y+16)
        _action_btn(surf, bw-80, prog_y+6, 72, 32, "CANCEL", "danger", r=5)
    else:
        col = (60,220,80) if status_msg.startswith("Done") else (160,160,170)
        _text(surf, status_msg, 10, col, cx=DISPLAY_W//2, cy=prog_y+24)


def navdata_data_hit(x, y, nd):
    if 8 <= x <= 80 and 6 <= y <= 37:
        return "back"
    bx = _ND_MX; bw = DISPLAY_W - 2*_ND_MX
    info_y = 92; info_h = 90
    btn_y  = info_y + info_h + 14
    btn_h  = 54
    prog_y = btn_y + btn_h + 10
    if nd.get("downloading"):
        if bx+bw-80 <= x <= bx+bw and prog_y+6 <= y <= prog_y+38:
            return "cancel"
    if bx <= x <= bx+bw and btn_y <= y <= btn_y+btn_h:
        return "download"
    return None


def draw_airport_data(surf, ad):
    """Full-screen airport data management screen."""
    surf.fill((0, 0, 0))
    _screen_header(surf, "AIRPORT DATA")
    bx = _AD_MX; bw = DISPLAY_W - 2*_AD_MX

    cnt      = ad.get("records", 0)
    used_mb  = ad.get("used_mb", 0.0)
    expired  = ad.get("expired", False)
    age      = ad.get("age_days", 0)

    # Status strip
    pygame.draw.rect(surf, (0,12,32), (bx, 52, bw, 28), border_radius=4)
    pygame.draw.rect(surf, (40,60,90), (bx, 52, bw, 28), width=1, border_radius=4)
    if cnt:
        age_str = f"  \u00b7  {age} day{'' if age == 1 else 's'} old"
        if expired:
            age_str += "  (expired)"
            stat_col = (220, 130, 60)
        else:
            stat_col = (60, 220, 80)
        stat_str = f"{cnt:,} airports  \u00b7  {used_mb:.1f} MB on disk{age_str}"
    else:
        stat_str = "No airport data on disk"
        stat_col = YELLOW
    _text(surf, stat_str, 12, stat_col, bold=True, cx=DISPLAY_W//2, cy=66)

    downloading = ad.get("downloading", False)
    parsing     = ad.get("parsing", False)

    # Info panel
    info_y = 92
    info_h = 90
    pygame.draw.rect(surf, (0,10,26), (bx, info_y, bw, info_h), border_radius=6)
    pygame.draw.rect(surf, (40,55,80), (bx, info_y, bw, info_h), width=1, border_radius=6)
    _text(surf, "OurAirports Global Database", 13, WHITE, bold=True,
          cx=DISPLAY_W//2, cy=info_y+16)
    _text(surf, "\u2248 72,000 airports worldwide (~20K in the US)",
          10, (140,160,185), cx=DISPLAY_W//2, cy=info_y+34)
    _text(surf, "Single CSV \u2248 12 MB \u00b7 Community-maintained, updated frequently",
          10, (120,140,165), cx=DISPLAY_W//2, cy=info_y+50)
    _text(surf, "Displayed on AI as cyan rings (airports), magenta H (heliports)",
          10, (120,140,165), cx=DISPLAY_W//2, cy=info_y+66)
    _text(surf, "WiFi (home network) required for download",
          10, (160,130,60), cx=DISPLAY_W//2, cy=info_y+80)

    # Download / Update button
    btn_y = info_y + info_h + 14
    btn_h = 54
    if downloading or parsing:
        bg = (0,20,10); oc = (40,140,60)
    else:
        bg = (0,18,45); oc = WHITE
    pygame.draw.rect(surf, bg, (bx, btn_y, bw, btn_h), border_radius=6)
    gh = btn_h // 5
    if not (downloading or parsing):
        for i in range(gh):
            t2 = 1.0 - i/gh
            gc = (int(15+t2*25), int(20+t2*40), int(40+t2*65))
            pygame.draw.line(surf, gc, (bx+6, btn_y+1+i), (bx+bw-6, btn_y+1+i))
    pygame.draw.rect(surf, oc, (bx, btn_y, bw, btn_h), width=2, border_radius=6)
    btn_label = "UPDATE" if cnt else "DOWNLOAD"
    tc = (70,80,90) if (downloading or parsing) else WHITE
    _text(surf, btn_label, 15, tc, bold=True, cx=DISPLAY_W//2, cy=btn_y+btn_h//2-8)
    sub = "airports.csv  from  ourairports-data"
    _text(surf, sub, 10, (100,120,140) if not (downloading or parsing) else (60,80,70),
          cx=DISPLAY_W//2, cy=btn_y+btn_h//2+10)

    # Progress / status area
    prog_y = btn_y + btn_h + 10
    prog_h = 48
    pygame.draw.rect(surf, (0,10,24), (bx, prog_y, bw, prog_h), border_radius=6)
    pygame.draw.rect(surf, (35,50,75), (bx, prog_y, bw, prog_h), width=1, border_radius=6)

    status_msg = ad.get("dl_status", "")
    if downloading:
        pct = 0
        try:
            if "%" in status_msg:
                pct = int(status_msg.split("%")[0].split()[-1])
        except (ValueError, IndexError):
            pct = 0
        bar_w = int((bw - 20) * pct / 100)
        pygame.draw.rect(surf, (0,22,12), (bx+10, prog_y+28, bw-20, 10), border_radius=3)
        if bar_w > 0:
            pygame.draw.rect(surf, (40,180,60), (bx+10, prog_y+28, bar_w, 10), border_radius=3)
        _text(surf, status_msg, 10, (140,160,180), cx=DISPLAY_W//2, cy=prog_y+16)
        _action_btn(surf, bw-80, prog_y+6, 72, 32, "CANCEL", "danger", r=5)
    elif parsing:
        _text(surf, status_msg, 10, (140,180,140), cx=DISPLAY_W//2, cy=prog_y+24)
    else:
        col = (60,220,80) if status_msg.startswith("Done") else (160,160,170)
        _text(surf, status_msg, 10, col, cx=DISPLAY_W//2, cy=prog_y+24)

    # Symbol legend
    leg_y = prog_y + prog_h + 12
    leg_h = 34
    pygame.draw.rect(surf, (0,8,20), (bx, leg_y, bw, leg_h), border_radius=4)
    _text(surf, "Symbol legend:", 10, (120,140,165), x=bx+10, y=leg_y+8)
    # Public airport ring
    lx = bx + 120; ly = leg_y + 13
    pygame.draw.circle(surf, (120, 220, 255), (lx, ly), 5, 0)
    pygame.draw.circle(surf, (0, 10, 30), (lx, ly), 3, 0)
    _text(surf, "PUBLIC", 9, (160,170,180), x=lx+10, y=leg_y+8)
    # Heliport H
    _text(surf, "H", 11, (220, 120, 220), bold=True, cx=bx+230, cy=ly)
    _text(surf, "HELIPORT", 9, (160,170,180), x=bx+240, y=leg_y+8)
    # Seaplane base
    sx = bx + 340; sy = ly
    pygame.draw.circle(surf, (150, 200, 255), (sx, sy), 4, 1)
    pygame.draw.line(surf, (150, 200, 255), (sx - 4, sy + 5), (sx + 4, sy + 5), 1)
    _text(surf, "SEAPLANE", 9, (160,170,180), x=sx+10, y=leg_y+8)

    # ── Display filters — toggle which airport types render on the AI ────
    filt_y = leg_y + leg_h + 14
    filt_h = 40
    _text(surf, "Display filters — tap to toggle:",
          11, (140,160,185), x=bx+6, y=filt_y-14)
    btn_w = (bw - 30) // 4
    # Row 1: airport type filters
    for i, (key, lbl) in enumerate([("show_public",   "PUBLIC"),
                                     ("show_heli",     "HELIPORTS"),
                                     ("show_seaplane", "SEAPLANE"),
                                     ("show_other",    "OTHER")]):
        bxi = bx + i * (btn_w + 10)
        _seg_btn(surf, bxi, filt_y, btn_w, filt_h, lbl, ad.get(key, False), r=6)
    # Row 2: runway + centerline + water overlays (3 wide tiles)
    row2_y = filt_y + filt_h + 10
    row2_w = (bw - 20) // 3
    _seg_btn(surf, bx,                       row2_y, row2_w, filt_h,
             "RUNWAYS", ad.get("show_runways", False), r=6)
    _seg_btn(surf, bx + row2_w + 10,         row2_y, row2_w, filt_h,
             "EXT CENTERLINES", ad.get("show_centerlines", False), r=6)
    _seg_btn(surf, bx + 2 * (row2_w + 10),   row2_y, row2_w, filt_h,
             "WATER", ad.get("show_water", False), r=6)

    # Row 3: direct-to navigation controls (3 tiles).  "DIRECT TO" opens the
    # QWERTY keyboard for ident entry; "NEAREST" picks the closest public
    # airport using current GPS pos; "CLEAR" cancels the active waypoint.
    nv = disp.get("nav", {})
    nav_ident = nv.get("ident", "")
    row3_y = row2_y + filt_h + 10
    nav_w = (bw - 20) // 3
    nav_lbl = f"DIRECT  →  {nav_ident}" if nav_ident else "DIRECT  →"
    nrst_ident = _nav_lookup_nearest()
    nrst_lbl = f"NEAREST  {nrst_ident}" if nrst_ident else "NEAREST"
    _seg_btn(surf, bx,                       row3_y, nav_w, filt_h,
             nav_lbl,   bool(nav_ident), r=6)
    _seg_btn(surf, bx + nav_w + 10,          row3_y, nav_w, filt_h,
             nrst_lbl,  False,            r=6)
    _seg_btn(surf, bx + 2 * (nav_w + 10),    row3_y, nav_w, filt_h,
             "CLEAR",   False,            r=6)


def airport_data_hit(x, y, ad):
    """Return action string or None."""
    if _back_hit(x, y):
        return "back"
    bx = _AD_MX; bw = DISPLAY_W - 2*_AD_MX
    btn_y = 92 + 90 + 14
    btn_h = 54
    # Filter toggle strip (same geometry as in draw_airport_data)
    prog_y = btn_y + btn_h + 10
    prog_h = 48
    leg_y  = prog_y + prog_h + 12
    leg_h  = 34
    filt_y = leg_y + leg_h + 14
    filt_h = 40
    btn_w  = (bw - 30) // 4
    # Row 1: airport type filters
    if filt_y <= y <= filt_y + filt_h:
        for i, key in enumerate(["show_public", "show_heli",
                                 "show_seaplane", "show_other"]):
            bxi = bx + i * (btn_w + 10)
            if bxi <= x <= bxi + btn_w:
                return f"toggle:{key}"
    # Row 2: runway + centerline + water toggles (3 wide tiles)
    row2_y = filt_y + filt_h + 10
    row2_w = (bw - 20) // 3
    if row2_y <= y <= row2_y + filt_h:
        if bx <= x <= bx + row2_w:
            return "toggle:show_runways"
        if bx + row2_w + 10 <= x <= bx + 2 * row2_w + 10:
            return "toggle:show_centerlines"
        if bx + 2 * (row2_w + 10) <= x <= bx + 2 * (row2_w + 10) + row2_w:
            return "toggle:show_water"
    # Row 3: direct-to navigation (3 tiles)
    row3_y = row2_y + filt_h + 10
    nav_w = (bw - 20) // 3
    if row3_y <= y <= row3_y + filt_h:
        if bx <= x <= bx + nav_w:
            return "nav_direct"
        if bx + nav_w + 10 <= x <= bx + 2 * nav_w + 10:
            return "nav_nearest"
        if bx + 2 * (nav_w + 10) <= x <= bx + 2 * (nav_w + 10) + nav_w:
            return "nav_clear"
    if ad.get("downloading"):
        if (bx+bw-80 <= x <= bx+bw and prog_y+6 <= y <= prog_y+38):
            return "cancel"
    if bx <= x <= bx+bw and btn_y <= y <= btn_y+btn_h:
        return "download"
    return None


# ── Airspace data management (pi4) ──────────────────────────────────────
# Mirror of pi_zero's AIRSPACE DATA screen.  Pi 4 can build its own
# airspaces.json so the PFD's inset map shows the polygons, and so
# the user has a consistent build flow regardless of which Pi has
# the GeoJSON files on disk.

def draw_airspace_data(surf):
    surf.fill((0, 0, 0))
    _screen_header(surf, "AIRSPACE DATA")
    bx = _AD_MX; bw = DISPLAY_W - 2 * _AD_MX
    asp = disp["asp"]
    cnt = asp.get("records", 0)

    pygame.draw.rect(surf, (0, 12, 32), (bx, 52, bw, 28), border_radius=4)
    pygame.draw.rect(surf, (40, 60, 90), (bx, 52, bw, 28), width=1, border_radius=4)
    if cnt:
        stat_str = f"{cnt} airspace polygons on disk"
        stat_col = (60, 220, 80)
    else:
        stat_str = "No airspaces.json found — using bundled example"
        stat_col = (200, 180, 100)
    _text(surf, stat_str, 14, stat_col, bold=True,
          cx=DISPLAY_W // 2, cy=66)

    info_y = 92
    info_h = 96
    pygame.draw.rect(surf, (0, 10, 26), (bx, info_y, bw, info_h),
                     border_radius=6)
    pygame.draw.rect(surf, (40, 55, 80), (bx, info_y, bw, info_h),
                     width=1, border_radius=6)
    _text(surf, "US Airspace Polygons (B/C/D/MOA/R/P/TFR)", 15, WHITE,
          bold=True, cx=DISPLAY_W // 2, cy=info_y + 14)
    _text(surf, "Rendered on the PFD inset map by class.",
          10, (140, 160, 185), cx=DISPLAY_W // 2, cy=info_y + 32)
    _text(surf, "Master toggle: Display Settings → ASP pill.",
          10, (140, 160, 185), cx=DISPLAY_W // 2, cy=info_y + 48)
    _text(surf, "AIRSPACES: B/C/D + SUA + Prohibited  (28-day cycle)",
          9, (140, 170, 200), cx=DISPLAY_W // 2, cy=info_y + 66)
    _text(surf, "TFRs: Stadium + Defense  (refresh more often)",
          9, (140, 170, 200), cx=DISPLAY_W // 2, cy=info_y + 80)

    # Two action buttons side by side, each with its own
    # last-downloaded date.  CLASSES button below opens per-class
    # toggle screen so pilot can hide MOAs but keep TFRs, etc.
    btn_y = info_y + info_h + 12
    btn_h = 56
    half  = (bw - 10) // 2
    downloading = asp.get("downloading", False)
    dl_style    = "normal" if not downloading else "warn"

    static_mt = _asp_bucket_mtime(asp_mod.DOWNLOAD_SOURCES_STATIC.keys())
    tfr_mt    = _asp_bucket_mtime(asp_mod.DOWNLOAD_SOURCES_TFR.keys())
    _action_btn(surf, bx, btn_y, half, btn_h,
                "AIRSPACES" if not downloading else "CANCEL", dl_style)
    _text(surf, f"last: {_asp_format_date(static_mt)}",
          11, (140, 160, 185),
          cx=bx + half // 2, cy=btn_y + btn_h + 12)
    _action_btn(surf, bx + half + 10, btn_y, half, btn_h,
                "TFRs" if not downloading else "CANCEL", dl_style)
    _text(surf, f"last: {_asp_format_date(tfr_mt)}",
          11, (140, 160, 185),
          cx=bx + half + 10 + half // 2, cy=btn_y + btn_h + 12)

    # CLASSES button — opens per-class display toggle screen.
    cls_y = btn_y + btn_h + 28
    cls_h = 44
    _action_btn(surf, bx, cls_y, bw, cls_h, "CLASSES  →", "normal")

    # Status / result line
    prog_y = cls_y + cls_h + 12
    prog_h = 36
    pygame.draw.rect(surf, (0, 10, 24), (bx, prog_y, bw, prog_h),
                     border_radius=6)
    pygame.draw.rect(surf, (35, 50, 75), (bx, prog_y, bw, prog_h),
                     width=1, border_radius=6)
    status = asp.get("dl_status", "")
    if status:
        col = ((60, 220, 80) if status.startswith("Done")
               else (220, 130, 60) if (status.startswith("Error")
                                        or status.startswith("Build failed"))
               else (160, 160, 170))
        _text(surf, status, 11, col, cx=DISPLAY_W // 2, cy=prog_y + 18)


def airspace_data_hit(x, y):
    if _back_hit(x, y):
        return "back"
    bx = _AD_MX; bw = DISPLAY_W - 2 * _AD_MX
    info_y = 92; info_h = 96
    btn_y  = info_y + info_h + 12
    btn_h  = 56
    half   = (bw - 10) // 2
    if btn_y <= y <= btn_y + btn_h:
        if bx <= x <= bx + half:
            return "download_static"
        if bx + half + 10 <= x <= bx + 2 * half + 10:
            return "download_tfr"
    cls_y = btn_y + btn_h + 28
    cls_h = 44
    if cls_y <= y <= cls_y + cls_h and bx <= x <= bx + bw:
        return "classes"
    return None


# ── AIRSPACE CLASSES toggle screen ──────────────────────────────────────────
# Per-class display toggles (pilot wants to hide MOAs but keep TFRs, etc.).
# Settings keys live in disp["ds"] as map_show_airspace_<lower>.

_ASP_CLS_ROWS = [
    ("b",   "B",   "Class B  ·  major airline hub"),
    ("c",   "C",   "Class C  ·  regional / busy general aviation"),
    ("d",   "D",   "Class D  ·  control tower airports"),
    ("moa", "MOA", "Military Operations Area"),
    ("r",   "R",   "Restricted airspace"),
    ("p",   "P",   "Prohibited airspace"),
    ("tfr", "TFR", "Temporary Flight Restriction"),
]


def draw_airspace_classes(surf):
    """Per-class airspace display toggles."""
    surf.fill((0, 0, 0))
    _screen_header(surf, "AIRSPACE CLASSES")
    bx = _AD_MX; bw = DISPLAY_W - 2 * _AD_MX
    ds = disp["ds"]

    _text(surf, "Hide individual airspace classes on the moving map.",
          12, (140, 160, 185), cx=DISPLAY_W // 2, cy=60)
    _text(surf, "Master toggle: Display Settings → ASP pill.",
          10, (120, 140, 165), cx=DISPLAY_W // 2, cy=76)

    rows_y0 = 92
    row_h   = 46
    row_gap = 6
    for i, (suffix, lbl, desc) in enumerate(_ASP_CLS_ROWS):
        ry = rows_y0 + i * (row_h + row_gap)
        pygame.draw.rect(surf, (0, 10, 28), (bx, ry, bw, row_h),
                         border_radius=6)
        pygame.draw.rect(surf, (45, 65, 95), (bx, ry, bw, row_h),
                         width=1, border_radius=6)
        col, _fill = _map_mod._AIRSPACE_COLORS.get(
            lbl, _map_mod._AIRSPACE_DEFAULT)
        sw_w = 36; sw_h = row_h - 14
        sx = bx + 8; sy = ry + 7
        pygame.draw.rect(surf, (col[0]//4, col[1]//4, col[2]//4),
                         (sx, sy, sw_w, sw_h), border_radius=4)
        pygame.draw.rect(surf, col, (sx, sy, sw_w, sw_h),
                         width=2, border_radius=4)
        _text(surf, lbl, 16, col, bold=True,
              cx=sx + sw_w // 2, cy=ry + row_h // 2)
        _text(surf, desc, 14, WHITE, bold=True,
              x=sx + sw_w + 12, y=ry + 6)
        key = f"map_show_airspace_{suffix}"
        on  = bool(ds.get(key, True))
        bt_w = 60; bt_h = row_h - 14
        on_x  = bx + bw - 2 * bt_w - 12
        off_x = bx + bw - bt_w - 6
        _seg_btn(surf, on_x,  ry + 7, bt_w, bt_h, "ON",  on,  r=4)
        _seg_btn(surf, off_x, ry + 7, bt_w, bt_h, "OFF", not on, r=4)


def airspace_classes_hit(x, y):
    if _back_hit(x, y):
        return "back"
    bx = _AD_MX; bw = DISPLAY_W - 2 * _AD_MX
    rows_y0 = 92
    row_h   = 46
    row_gap = 6
    bt_w = 60; bt_h = row_h - 14
    on_x  = bx + bw - 2 * bt_w - 12
    off_x = bx + bw - bt_w - 6
    for i, (suffix, _lbl, _desc) in enumerate(_ASP_CLS_ROWS):
        ry = rows_y0 + i * (row_h + row_gap)
        if ry + 7 <= y <= ry + 7 + bt_h:
            if on_x <= x <= on_x + bt_w or off_x <= x <= off_x + bt_w:
                return f"toggle:map_show_airspace_{suffix}"
    return None


def draw_terrain_data(surf, td):
    """Full-screen terrain data management screen."""
    _screen_header(surf, "TERRAIN DATA")
    bx = _TD_MX; bw = DISPLAY_W - 2*_TD_MX
    n_tiles, used_mb = _td_disk_stats()

    # Status strip
    pygame.draw.rect(surf, (0,12,32), (bx, 52, bw, 28), border_radius=4)
    pygame.draw.rect(surf, (40,60,90), (bx, 52, bw, 28), width=1, border_radius=4)
    stat_str = (f"{n_tiles} tile{'s' if n_tiles != 1 else ''} on disk  \u00b7  {used_mb:.1f} MB used"
                if n_tiles else "No tiles on disk  \u00b7  SVT uses flat terrain")
    stat_col = (60,220,80) if n_tiles else YELLOW
    _text(surf, stat_str, 12, stat_col, bold=True, cx=DISPLAY_W//2, cy=66)

    downloading = td.get("downloading", False)
    rows = (len(_TD_REGIONS) + _TD_COLS - 1) // _TD_COLS
    available_h = DISPLAY_H - _TD_MY - _TD_GAP*(rows-1) - 8
    bh = available_h // (rows + 1)   # +1 row for the "Current Area" button

    # ── Top row: Current Area | Water Masks ──────────────────────────────────
    # Two side-by-side full-height tiles.  Left = SRTM tile download for the
    # current GPS area; right = rasterise Natural Earth water masks for the
    # SRTM tiles already on disk so SVT paints oceans + lakes blue.
    half_w = (bw - _TD_GAP) // 2
    wd = disp["wd"]
    wd_busy = wd.get("downloading", False)

    # Left: Current Area (terrain SRTM)
    cur_col = (50,50,70) if downloading else (0,18,45)
    cur_oc  = (70,70,95)  if downloading else WHITE
    pygame.draw.rect(surf, cur_col, (bx, _TD_MY, half_w, bh), border_radius=6)
    gh = bh // 5
    for i in range(gh):
        t = 1.0 - i/gh
        gc = (int(15+t*25), int(20+t*40), int(40+t*65)) if not downloading else (int(20+t*20),int(20+t*20),int(30+t*30))
        pygame.draw.line(surf, gc, (bx+6, _TD_MY+1+i), (bx+half_w-6, _TD_MY+1+i))
    pygame.draw.rect(surf, cur_oc, (bx, _TD_MY, half_w, bh), width=2, border_radius=6)
    _text(surf, "DOWNLOAD CURRENT AREA", 13, cur_oc, bold=True,
          cx=bx+half_w//2, cy=_TD_MY+bh//2-8)
    lat_i = int(disp.get("lat", DEMO_LAT)); lon_i = int(disp.get("lon", DEMO_LON))
    area_str = (f"25 tiles  {lat_i}\u00b0{'N' if lat_i>=0 else 'S'} "
                f"{abs(lon_i)}\u00b0{'W' if lon_i<0 else 'E'}  \u2248 35 MB")
    _text(surf, area_str, 10, (120,140,165),
          cx=bx+half_w//2, cy=_TD_MY+bh//2+10)

    # Right: Water Masks (rasterise Natural Earth for existing SRTM tiles)
    wd_x = bx + half_w + _TD_GAP
    wd_n_tiles, wd_used_mb = water_mod.disk_stats(WATER_DIR)
    if wd_busy:
        wd_bg = (50,50,70); wd_oc = (70,70,95)
    else:
        wd_bg = (0,15,30);  wd_oc = (90, 160, 210)   # cyan-ish for water
    pygame.draw.rect(surf, wd_bg, (wd_x, _TD_MY, half_w, bh), border_radius=6)
    for i in range(gh):
        t = 1.0 - i/gh
        gc = ((int(10+t*20), int(40+t*55), int(60+t*70)) if not wd_busy
              else (int(20+t*20),int(20+t*20),int(30+t*30)))
        pygame.draw.line(surf, gc,
                         (wd_x+6, _TD_MY+1+i),
                         (wd_x+half_w-6, _TD_MY+1+i))
    pygame.draw.rect(surf, wd_oc, (wd_x, _TD_MY, half_w, bh),
                     width=2, border_radius=6)
    _text(surf, "DOWNLOAD WATER MASKS", 13, wd_oc, bold=True,
          cx=wd_x+half_w//2, cy=_TD_MY+bh//2-8)
    if wd_n_tiles:
        sub = f"{wd_n_tiles} masks on disk  \u00b7  {wd_used_mb:.1f} MB"
    else:
        sub = "Natural Earth ocean + lakes  \u2248 12 MB once"
    _text(surf, sub, 10, (120,160,190),
          cx=wd_x+half_w//2, cy=_TD_MY+bh//2+10)

    # ── Preset region grid ────────────────────────────────────────────────────
    grid_y = _TD_MY + bh + _TD_GAP
    btn_w = (bw - _TD_GAP) // 2
    for idx, region in enumerate(_TD_REGIONS):
        col = idx % _TD_COLS; row = idx // _TD_COLS
        rx = bx + col*(btn_w+_TD_GAP)
        ry = grid_y + row*(bh+_TD_GAP)
        label, sub, *_ = region
        n = _td_region_tile_count(region)
        mb = n * 1.5
        is_active = downloading and td.get("dl_region","") == label

        if is_active:
            bg=(0,28,18); oc=(40,180,60)
        elif downloading:
            bg=(0,8,18); oc=(35,45,60)
        else:
            bg=(0,12,32); oc=(55,75,105)

        pygame.draw.rect(surf, bg, (rx, ry, btn_w, bh), border_radius=6)
        if not downloading:
            gh2 = bh // 6
            for i in range(gh2):
                t2 = 1.0-i/gh2
                gc2=(int(15+t2*20),int(20+t2*35),int(40+t2*60))
                pygame.draw.line(surf, gc2, (rx+6, ry+1+i), (rx+btn_w-6, ry+1+i))
        pygame.draw.rect(surf, oc, (rx, ry, btn_w, bh), width=2, border_radius=6)
        tc = (40,180,60) if is_active else ((70,80,90) if downloading else WHITE)
        _text(surf, label, 14, tc, bold=True, cx=rx+btn_w//2, cy=ry+bh//2-12)
        _text(surf, sub,   10, (100,120,140) if not is_active else (60,180,80),
              cx=rx+btn_w//2, cy=ry+bh//2+4)
        _text(surf, f"\u223c{n} tiles  {mb:.0f} MB", 9, (70,85,105),
              cx=rx+btn_w//2, cy=ry+bh//2+18)

    # ── Download progress overlay ─────────────────────────────────────────────
    # Either terrain or water rasterisation may be active; whichever is
    # busy gets the bottom strip + cancel button.
    active_dict = None
    if downloading:
        active_dict = td
    elif wd_busy:
        active_dict = wd
    if active_dict is not None:
        prog_y = DISPLAY_H - 58
        cur = active_dict.get("dl_current", 0)
        total = max(1, active_dict.get("dl_total", 1))
        frac = cur / total
        pygame.draw.rect(surf, (0,12,32), (bx, prog_y, bw, 50), border_radius=6)
        pygame.draw.rect(surf, (55,75,105), (bx, prog_y, bw, 50), width=1, border_radius=6)
        bar_w = int((bw - 20) * frac)
        pygame.draw.rect(surf, (0,25,12), (bx+10, prog_y+28, bw-20, 12), border_radius=3)
        if bar_w > 0:
            pygame.draw.rect(surf, (40,180,60), (bx+10, prog_y+28, bar_w, 12), border_radius=3)
        _text(surf, active_dict.get("dl_status",""), 10, (140,160,180),
              cx=DISPLAY_W//2, cy=prog_y+14)
        pct = f"{int(frac*100)}%  ({cur}/{total})"
        _text(surf, pct, 10, (60,220,80), cx=DISPLAY_W//2, cy=prog_y+43)
        # CANCEL button
        _action_btn(surf, DISPLAY_W-100-bx, prog_y+6, 92, 36, "CANCEL", "danger", r=5)
    else:
        # Show whichever last status we have (prefer water if just finished)
        last = wd.get("dl_status") or td.get("dl_status", "")
        if last:
            _text(surf, last, 11, (80,160,100), cx=DISPLAY_W//2, cy=DISPLAY_H-12)


def terrain_data_hit(x, y, td):
    """Return action string or None."""
    if _back_hit(x, y):
        return "back"
    bx = _TD_MX; bw = DISPLAY_W - 2*_TD_MX
    rows = (len(_TD_REGIONS) + _TD_COLS - 1) // _TD_COLS
    available_h = DISPLAY_H - _TD_MY - _TD_GAP*(rows-1) - 8
    bh = available_h // (rows + 1)
    half_w = (bw - _TD_GAP) // 2

    # Cancel button during download (terrain or water)
    if td.get("downloading") or disp["wd"].get("downloading"):
        prog_y = DISPLAY_H - 58
        if (DISPLAY_W-100-bx <= x <= DISPLAY_W-bx and
                prog_y+6 <= y <= prog_y+42):
            return "cancel"

    # Top row: left half = current area; right half = water masks
    if _TD_MY <= y <= _TD_MY + bh:
        if bx <= x <= bx + half_w:
            return "current_area"
        wd_x = bx + half_w + _TD_GAP
        if wd_x <= x <= wd_x + half_w:
            return "water_masks"

    # Region grid
    grid_y = _TD_MY + bh + _TD_GAP
    btn_w = (bw - _TD_GAP) // 2
    for idx, region in enumerate(_TD_REGIONS):
        col = idx % _TD_COLS; row = idx // _TD_COLS
        rx = bx + col*(btn_w+_TD_GAP)
        ry = grid_y + row*(bh+_TD_GAP)
        if rx <= x <= rx+btn_w and ry <= y <= ry+bh:
            return f"region:{idx}"
    return None


# ── Cyan tap-buttons (HDG bug, BARO, ALT bug) ────────────────────────────────
# These sit just below the heading tape and below each tape's bottom edge,
# styled as cyan-bordered dark boxes matching the GI-275 blue label style.

def _cyan_box(surf, value_str, x, y, w=74, h=22, font_sz=14, col=None):
    """Illuminated tap button: r=3 corners, 2px border, top glow, no label.
    col defaults to CYAN; pass MAGENTA for GPS-sourced values."""
    if col is None:
        col = CYAN
    cr, cg, cb = col
    # Background fill
    pygame.draw.rect(surf, (0, 20, 35), (x, y, w, h), border_radius=3)
    # Top glow — tinted to border colour
    glow_h = max(4, h // 3)
    for i in range(glow_h):
        t = 1.0 - i / glow_h
        gr = min(255, int(cr * t * 0.35))
        gg = min(255, int(20 + cg * t * 0.45))
        gb = min(255, int(35 + cb * t * 0.50))
        pygame.draw.line(surf, (gr, gg, gb), (x + 2, y + 1 + i), (x + w - 3, y + 1 + i))
    # 2px border (matching veeder-root outline width)
    pygame.draw.rect(surf, col, (x, y, w, h), width=2, border_radius=3)
    # Value text — centred H+V
    _text(surf, value_str, font_sz, col, bold=True, cx=x + w // 2, cy=y + h // 2)


def draw_tap_buttons(surf, hdg, hdg_bug, baro_hpa, baro_src, alt_bug,
                     use_track=False, baro_ok=True):
    """
    Tap buttons in the heading strip — left and right only so the centre
    heading readout remains unobstructed:
      • Left  (under speed tape) : HDG bug  — MAGENTA=GPS TRK, CYAN=MAG
      • Right (under alt tape)   : Baro setting — CYAN=baro sensor, MAGENTA=GPS ALT
    IAS and ALT bug buttons are drawn at the tops of their own tapes.
    """
    y = HDG_Y

    # HDG bug — left side of heading strip; color matches heading bug triangle
    _hdg_btn = f"{round(hdg_bug) % 360:03d}\u00b0" if hdg_bug is not None else "---\u00b0"
    hdg_box_col = MAGENTA if use_track else CYAN
    _cyan_box(surf, _hdg_btn, x=SPD_X, y=y, w=SPD_W, h=HDG_H, col=hdg_box_col)

    # Baro — right side of heading strip; CYAN when baro sensor active, MAGENTA when GPS ALT
    # Accept any non-"gps" baro_src as meaning baro sensor is active (firmware uses
    # "bme280", sim/demo/preview code uses "baro" — both mean the same thing).
    if baro_ok and baro_src != "gps":
        baro_unit = disp["ds"].get("baro_unit", "inhg")
        if baro_unit == "hpa":
            baro_lbl = f"{baro_hpa:.0f} hPa"
            baro_fsz = 12
        else:
            baro_lbl = f"{baro_hpa / 33.8639:.2f} IN"
            baro_fsz = 12   # wider string needs slightly smaller font
        baro_col = CYAN
    else:
        baro_lbl = "GPS ALT"
        baro_fsz = 14
        baro_col = MAGENTA
    _cyan_box(surf, baro_lbl,
              x=ALT_X + 1, y=y, w=ALT_W - 1, h=HDG_H, font_sz=baro_fsz, col=baro_col)


# ── Veil surface for transparent overlay modes (allocated once) ───────────────
_veil_surf = None

def _draw_veil(surf):
    """Alpha-blend a dark overlay onto surf for numpad/keyboard transparency."""
    global _veil_surf
    if _veil_surf is None:
        _veil_surf = pygame.Surface((DISPLAY_W, DISPLAY_H), pygame.SRCALPHA)
        _veil_surf.fill((0, 5, 15, 180))
    surf.blit(_veil_surf, (0, 0))


# ── Obstacle symbol renderer ──────────────────────────────────────────────────

_OBS_RADIUS_NM  = OBSTACLE_RADIUS_NM
_OBS_BELOW_FT   = OBSTACLE_BELOW_FT
_OBS_CAUTION_FT = OBSTACLE_CAUTION_FT
_OBS_MIN_AGL_FT = 25.0    # hide DOF entries shorter than this so airport-
                          # surface clutter (signs, low markers, taxiway
                          # lighting) doesn't paint phantom towers on
                          # the runway / ramp.  Anything 25 ft or taller
                          # is something a VFR pilot might want to know
                          # about.
# Airport-boundary declutter: anything shorter than _OBS_AIRPORT_FLOOR_FT
# AGL gets hidden when it sits within _OBS_AIRPORT_RADIUS_NM of any
# runway centroid.  Real airport surface infrastructure (terminal
# buildings, light poles, jet bridges) is typically 25-49 ft AGL and
# clutters the runway view; tall obstructions like the ATC tower
# (321 ft AGL at PHX) stay visible because they exceed the floor.
_OBS_AIRPORT_RADIUS_NM = 1.0
_OBS_AIRPORT_FLOOR_FT  = 50.0

# Cache rendered obstacle labels keyed on (text, colour) — pygame.font
# rendering is ~1 ms each call, and a busy metro view can show 50+ towers
# whose MSL labels repeat (round-100 buckets).  Cleared on font/style
# change, capped to keep memory bounded.
_obs_label_cache = {}
_OBS_LABEL_CACHE_MAX = 256


def _obs_label_blit(surf, text, color, cx, cy):
    """Blit a small obstacle MSL label, reusing cached pygame surfaces
    so the heavy text rendering only happens once per (text, color)."""
    key = (text, color)
    img = _obs_label_cache.get(key)
    if img is None:
        font = _get_font(8, False)
        img = font.render(text, True, color)
        if len(_obs_label_cache) >= _OBS_LABEL_CACHE_MAX:
            _obs_label_cache.clear()
        _obs_label_cache[key] = img
    rect = img.get_rect(center=(cx, cy))
    surf.blit(img, rect)
_OBS_WARNING_FT = OBSTACLE_WARNING_FT

def draw_obstacle_symbols(surf, ai_rect, lat, lon, alt_ft,
                          hdg_deg, pitch_deg, roll_deg):
    """
    Project nearby obstacles onto the AI viewport as red/amber tower symbols.

    Vectorised projection: candidate obstacles come back from
    obs_mod.query_nearby as a numpy structured array, all bearing/
    distance/vertical-angle math runs over the whole batch in numpy,
    and we only fall into a Python loop to issue the pygame draw calls
    for the obstacles whose top anchor lands inside the AI rect.
    """
    import numpy as _np
    nearby = obs_mod.query_nearby(_obstacles, lat, lon,
                                  radius_nm=_OBS_RADIUS_NM,
                                  alt_ft=alt_ft,
                                  below_ft=_OBS_BELOW_FT)
    if nearby is None or len(nearby) == 0:
        return

    ax, ay_r, aw, ah = ai_rect
    cx = ax + aw // 2
    cy = ay_r + ah // 2

    # Same px/deg as the SVT and airport overlays (ai_h / 48° vertical FOV).
    # Obstacles previously used a hardcoded 8 px/deg, which didn't match the
    # SVT camera so towers shifted relative to the terrain on turn/pitch
    # and floated above ground.
    PX_PER_DEG = ah / 48.0

    nm_per_deg_lat = 60.0
    nm_per_deg_lon = 60.0 * math.cos(math.radians(lat))

    ob_lat = nearby["lat"].astype(_np.float64)
    ob_lon = nearby["lon"].astype(_np.float64)
    ob_msl = nearby["msl_ft"].astype(_np.float64)
    ob_agl = nearby["agl_ft"].astype(_np.float64)

    dlat_nm = (ob_lat - lat) * nm_per_deg_lat
    dlon_nm = (ob_lon - lon) * nm_per_deg_lon
    dist_nm = _np.hypot(dlat_nm, dlon_nm)
    bearing = _np.degrees(_np.arctan2(dlon_nm, dlat_nm)) % 360.0
    rel_brg = (bearing - hdg_deg + 180.0) % 360.0 - 180.0

    dist_ft = dist_nm * 6076.0
    # Avoid div-zero in arctan2 for co-located obstacles (just clip distance)
    dist_ft_safe = _np.maximum(dist_ft, 1.0)
    top_diff_ft  = ob_msl - alt_ft
    base_diff_ft = (ob_msl - ob_agl) - alt_ft
    top_vert_deg  = _np.degrees(_np.arctan2(top_diff_ft,  dist_ft_safe))
    base_vert_deg = _np.degrees(_np.arctan2(base_diff_ft, dist_ft_safe))

    cos_r = math.cos(math.radians(roll_deg))
    sin_r = math.sin(math.radians(roll_deg))
    sxr = rel_brg * PX_PER_DEG
    syr_top  = (pitch_deg - top_vert_deg)  * PX_PER_DEG
    syr_base = (pitch_deg - base_vert_deg) * PX_PER_DEG

    sx_top  = (cx + sxr * cos_r - syr_top  * sin_r).astype(_np.int32)
    sy_top  = (cy + sxr * sin_r + syr_top  * cos_r).astype(_np.int32)
    sy_base = (cy + sxr * sin_r + syr_base * cos_r).astype(_np.int32)

    # Airport-boundary declutter.  For every obstacle, find the
    # nearest runway centroid; if it's within _OBS_AIRPORT_RADIUS_NM
    # AND the obstacle is shorter than _OBS_AIRPORT_FLOOR_FT AGL,
    # it's airport-surface clutter (terminal buildings, jet bridges,
    # taxiway lighting) and gets hidden.  Tall airport obstructions
    # (ATC tower, long-range antennas) clear the floor and stay
    # visible.  Outside the airport boundary the only height filter
    # is _OBS_MIN_AGL_FT.
    inside_airport = _np.zeros(len(ob_lat), dtype=bool)
    if _runways is not None and len(_runways) > 0:
        nearby_rwys = rwy_mod.query_nearby(
            _runways, lat, lon,
            radius_nm=_OBS_RADIUS_NM + _OBS_AIRPORT_RADIUS_NM)
        if nearby_rwys:
            rwy_lat = _np.fromiter(
                (rw.centre_lat for rw in nearby_rwys),
                dtype=_np.float64, count=len(nearby_rwys))
            rwy_lon = _np.fromiter(
                (rw.centre_lon for rw in nearby_rwys),
                dtype=_np.float64, count=len(nearby_rwys))
            # Min-distance per obstacle against all nearby runway
            # centroids.  Broadcast (N_obs, 1) - (1, N_rwy).
            dlat_r = (ob_lat[:, None] - rwy_lat[None, :]) * nm_per_deg_lat
            dlon_r = (ob_lon[:, None] - rwy_lon[None, :]) * nm_per_deg_lon
            min_d_nm = _np.sqrt(dlat_r * dlat_r + dlon_r * dlon_r).min(axis=1)
            inside_airport = min_d_nm <= _OBS_AIRPORT_RADIUS_NM

    # Visibility mask: not co-located + top anchor inside AI rect +
    # AGL clears the global floor + (if inside an airport boundary,
    # AGL also clears the airport-clutter floor).
    agl_ok = (ob_agl >= _OBS_MIN_AGL_FT) & (
        ~inside_airport | (ob_agl >= _OBS_AIRPORT_FLOOR_FT))
    visible = ((dist_nm >= 0.01)
               & agl_ok
               & (sx_top >= ax + 4) & (sx_top <= ax + aw - 4)
               & (sy_top >= ay_r + 4) & (sy_top <= ay_r + ah - 4))
    visible_idx = _np.flatnonzero(visible)
    if visible_idx.size == 0:
        return

    clearance = alt_ft - ob_msl
    ob_lit = nearby["lit"]

    for i in visible_idx:
        cl = clearance[i]
        if cl < _OBS_WARNING_FT:
            col = RED
        elif cl < _OBS_CAUTION_FT:
            col = YELLOW
        else:
            col = WHITE

        sx, sy = int(sx_top[i]),  int(sy_top[i])
        by     = int(sy_base[i])
        tower_h = max(6, by - sy)
        apex = (sx, sy)
        base_half = max(3, tower_h // 3)
        left_base  = (sx - base_half, sy + tower_h)
        right_base = (sx + base_half, sy + tower_h)
        pygame.draw.line(surf, col, left_base,  apex, 2)
        pygame.draw.line(surf, col, right_base, apex, 2)

        if ob_lit[i]:
            r = 4
            star_col = (255, 230, 100)
            pygame.draw.line(surf, star_col, (sx - r, sy),     (sx + r, sy),     2)
            pygame.draw.line(surf, star_col, (sx,     sy - r), (sx,     sy + r), 2)
            pygame.draw.line(surf, star_col, (sx - r, sy - r), (sx + r, sy + r), 1)
            pygame.draw.line(surf, star_col, (sx - r, sy + r), (sx + r, sy - r), 1)

        # Selective labelling — pygame text render is ~1 ms each, so we
        # only label the towers that the pilot most needs to read.  Tall
        # towers (≥ 1000 ft AGL) anywhere in range, plus everything
        # within 1 nm.  Label is the obstacle's true MSL top (was
        # bucketed to nearest 100 ft, which read like the obstacle's
        # height instead of its altitude — "1100" looked like a 1100-ft
        # tower when it was a 50-ft pole at 1185 MSL).
        if ob_agl[i] >= 1000 or dist_nm[i] < 1.0:
            lbl = f"{int(ob_msl[i])}"
            _obs_label_blit(surf, lbl, col, sx, sy - 14)


def draw_airport_symbols(surf, ai_rect, lat, lon, alt_ft,
                         hdg_deg, pitch_deg, roll_deg):
    """
    Project nearby airports onto the AI viewport as small symbols + labels.

    Symbol shape encodes airport type:
      S/M/L (small/medium/large public airport) → cyan circle
      H (heliport) → magenta "H"
      W (seaplane base) → cyan circle with wavy underscore
      B (balloonport) → small cyan triangle

    Label (ident) shown only within AIRPORT_LABEL_NM to avoid clutter.
    """
    import airports as apt_mod
    if _airports is None:
        return

    # Per-category filter — user toggles on the AIRPORT DATA screen
    ad = disp["ad"]
    show = {
        "S": ad.get("show_public",   True),
        "M": ad.get("show_public",   True),
        "L": ad.get("show_public",   True),
        "H": ad.get("show_heli",     True),
        "W": ad.get("show_seaplane", False),
        "B": ad.get("show_other",    False),
    }
    if not any(show.values()):
        return

    import numpy as _np
    nearby = apt_mod.query_nearby(_airports, lat, lon,
                                  radius_nm=AIRPORT_RADIUS_NM)
    if nearby is None or len(nearby) == 0:
        return

    ax, ay_r, aw, ah = ai_rect
    cx = ax + aw // 2
    cy = ay_r + ah // 2

    # Same pixel-per-degree scale as the pitch ladder and SVT projection
    # (ai_h / 48° vertical FOV), so airport symbols align with the 3D view.
    PX_PER_DEG = ah / 48.0
    nm_per_deg_lat = 60.0
    nm_per_deg_lon = 60.0 * math.cos(math.radians(lat))
    cos_r = math.cos(math.radians(roll_deg))
    sin_r = math.sin(math.radians(roll_deg))

    max_rel_brg = (aw // 2) / PX_PER_DEG

    APT_PUBLIC  = (120, 220, 255)   # cyan — public paved/unpaved
    APT_HELI    = (220, 120, 220)   # magenta — heliport
    APT_WATER   = (150, 200, 255)   # lighter blue — seaplane base
    APT_OTHER   = (180, 180, 200)   # grey — other

    # Vectorised type filter (still in user's show set) on the structured
    # array so we never project airports that wouldn't draw anyway.
    atype_col = nearby["atype"]
    type_mask = _np.zeros(len(nearby), dtype=bool)
    for t, on in show.items():
        if on:
            type_mask |= (atype_col == t)
    if not type_mask.any():
        return
    nearby = nearby[type_mask]

    apt_lat = nearby["lat"].astype(_np.float64)
    apt_lon = nearby["lon"].astype(_np.float64)
    apt_elev = nearby["elev_ft"].astype(_np.float64)

    dlat_nm = (apt_lat - lat) * nm_per_deg_lat
    dlon_nm = (apt_lon - lon) * nm_per_deg_lon
    dist_nm = _np.hypot(dlat_nm, dlon_nm)
    bearing = _np.degrees(_np.arctan2(dlon_nm, dlat_nm)) % 360.0
    rel_brg = (bearing - hdg_deg + 180.0) % 360.0 - 180.0

    dist_ft = dist_nm * 6076.0
    dist_ft_safe = _np.maximum(dist_ft, 1.0)
    alt_diff_ft = apt_elev - alt_ft
    vert_deg = _np.degrees(_np.arctan2(alt_diff_ft, dist_ft_safe))

    sxr = rel_brg * PX_PER_DEG
    syr = (pitch_deg - vert_deg) * PX_PER_DEG
    sx_arr = (cx + sxr * cos_r - syr * sin_r).astype(_np.int32)
    sy_arr = (cy + sxr * sin_r + syr * cos_r).astype(_np.int32)

    visible = ((dist_nm >= 0.05)
               & (_np.abs(rel_brg) <= max_rel_brg)
               & (vert_deg <= 0)
               & (sx_arr >= ax + 8) & (sx_arr <= ax + aw - 8)
               & (sy_arr >= ay_r + 8) & (sy_arr <= ay_r + ah - 8))
    visible_idx = _np.flatnonzero(visible)
    if visible_idx.size == 0:
        return

    # Render farthest first so nearer ones are drawn on top (Z-order ish).
    # nearby is already sorted by distance ascending (query_nearby), so we
    # walk visible_idx in reverse.
    apt_ident = nearby["ident"]
    apt_atype = nearby["atype"]
    for i in reversed(visible_idx.tolist()):
        atype = str(apt_atype[i])
        sx, sy = int(sx_arr[i]), int(sy_arr[i])

        if atype == "H":
            col = APT_HELI
            _text(surf, "H", 12, col, bold=True, cx=sx, cy=sy)
        elif atype == "W":
            col = APT_WATER
            pygame.draw.circle(surf, col, (sx, sy), 4, 1)
            pygame.draw.line(surf, col, (sx - 4, sy + 5), (sx + 4, sy + 5), 1)
        elif atype == "B":
            col = APT_OTHER
            pts = [(sx, sy - 4), (sx - 4, sy + 3), (sx + 4, sy + 3)]
            pygame.draw.polygon(surf, col, pts, 1)
        else:
            col = APT_PUBLIC
            pygame.draw.circle(surf, col, (sx, sy), 5, 0)
            pygame.draw.circle(surf, (0, 10, 30), (sx, sy), 3, 0)
            if atype in ("M", "L"):
                pygame.draw.circle(surf, col, (sx, sy), 7, 1)

        if dist_nm[i] <= AIRPORT_LABEL_NM:
            lbl = str(apt_ident[i])
            font_sz = 9
            f = _get_font(font_sz, bold=True)
            tw, th = f.size(lbl)
            sign_w = tw + 8
            sign_h = th + 4
            post_h = 22
            sign_x = sx - sign_w // 2
            sign_y = sy - post_h - sign_h
            if sign_y < ay_r + 2:
                sign_y = ay_r + 2
                post_h = max(4, sy - sign_y - sign_h)
            pygame.draw.line(surf, col, (sx, sy - 6), (sx, sign_y + sign_h), 1)
            pygame.draw.rect(surf, (0, 10, 26),
                             (sign_x, sign_y, sign_w, sign_h), border_radius=2)
            pygame.draw.rect(surf, col,
                             (sign_x, sign_y, sign_w, sign_h), width=1, border_radius=2)
            _text(surf, lbl, font_sz, col, bold=True,
                  cx=sx, cy=sign_y + sign_h // 2)


def draw_fpv_marker(surf, ai_rect, hdg_deg, pitch_deg, roll_deg,
                    gs_kt, track_deg, vspeed_fpm):
    """Flight-path vector (velocity-vector) marker on the AI.

    Shows where the aircraft is actually going through space, not where
    the nose is pointing.  Horizontal offset from the nose = track − heading
    (drift / crab); vertical offset below the nose = flight-path angle vs
    pitch, which equals AOA when the wind is along the flight path.  Reuses
    the same relative-bearing / vertical-angle → AI projection the airport,
    runway and obstacle overlays use (cx/cy, ah/48° px-per-deg, roll
    rotation), so the marker banks with the horizon and lines up with the
    SVT and airport symbols.

    Hidden below 5 kt GS (parked / taxi noise carries no meaningful track).
    Clamped to the AI rectangle; when the velocity vector falls outside the
    viewport (extreme attitude / big crab) a small ghost arrow is drawn at
    the edge pointing toward it — G3X convention, so the cue never just
    vanishes when it matters most.
    """
    if gs_kt < 5.0:
        return

    ax, ay_r, aw, ah = ai_rect
    cx = ax + aw // 2
    cy = ay_r + ah // 2
    PX_PER_DEG = ah / 48.0    # same vertical-FOV scale as overlays + SVT

    # Velocity vector in the world: azimuth = GPS track, elevation above the
    # horizon = flight-path angle γ = atan2(VS, GS).
    rel_brg = (track_deg - hdg_deg + 180.0) % 360.0 - 180.0
    gs_fpm  = gs_kt * 101.27     # kt → ft/min (6076.12 / 60)
    fpa_deg = math.degrees(math.atan2(vspeed_fpm, max(gs_fpm, 1.0)))

    # Diagnostic overlay (config_local: FPV_DEBUG=True) — shows the inputs so an
    # FPV-vs-horizon misalignment can be read in flight.  In level flight γ must
    # be 0 (ring centred on the horizon); if it isn't, the VS feeding the FPV is
    # non-zero.  At low GS the marker is very sensitive (≈1° per gs/57 fpm).
    if FPV_DEBUG:
        _text(surf, "pit %+.1f  VS %+d  GS %.0f  g %+.2f  dy %+.2f"
              % (pitch_deg, int(round(vspeed_fpm)), gs_kt, fpa_deg,
                 pitch_deg - fpa_deg),
              13, (40, 230, 60), bold=True, x=ax + 8, y=ay_r + 6)

    sxr = rel_brg * PX_PER_DEG
    syr = (pitch_deg - fpa_deg) * PX_PER_DEG
    cos_r = math.cos(math.radians(roll_deg))
    sin_r = math.sin(math.radians(roll_deg))
    sx = cx + sxr * cos_r - syr * sin_r
    sy = cy + sxr * sin_r + syr * cos_r

    FPV_COL = (40, 230, 60)     # green — stands out from the cyan HITS boxes /
                                # course line it would otherwise blend into
    margin  = 26

    if (ax + margin <= sx <= ax + aw - margin
            and ay_r + margin <= sy <= ay_r + ah - margin):
        ix, iy = int(sx), int(sy)
        # Open circle + two horizontal wings + a short vertical stub.
        pygame.draw.circle(surf, FPV_COL, (ix, iy), 12, 3)
        pygame.draw.line(surf, FPV_COL, (ix - 24, iy), (ix - 12, iy), 3)
        pygame.draw.line(surf, FPV_COL, (ix + 12, iy), (ix + 24, iy), 3)
        pygame.draw.line(surf, FPV_COL, (ix, iy - 24), (ix, iy - 12), 3)
    else:
        # Clamp to the AI edge, draw a small arrow pointing at the true,
        # off-screen velocity vector.
        gx = min(max(sx, ax + margin), ax + aw - margin)
        gy = min(max(sy, ay_r + margin), ay_r + ah - margin)
        dx, dy = sx - gx, sy - gy
        if dx == 0 and dy == 0:
            return
        ang = math.atan2(dy, dx)
        ca, sa = math.cos(ang), math.sin(ang)
        tip  = (gx + 18 * ca,          gy + 18 * sa)
        bl   = (gx - 8 * ca - 10 * sa, gy - 8 * sa + 10 * ca)
        br   = (gx - 8 * ca + 10 * sa, gy - 8 * sa - 10 * ca)
        pygame.draw.polygon(surf, FPV_COL,
                            [(int(tip[0]), int(tip[1])),
                             (int(bl[0]),  int(bl[1])),
                             (int(br[0]),  int(br[1]))], 2)


# ── Direct-to navigation ─────────────────────────────────────────────────────
# Rudimentary GPS nav: a single active waypoint (an airport in our DB).  The
# magenta course trace runs along the ground from the aircraft to the
# waypoint; the CDI shows perpendicular cross-track from the great-circle
# course originating at the activation point.

_CDI_FULL_SCALE_NM      = 1.0    # ±1 nm full-scale en-route / D2
_CDI_APPR_FULL_SCALE_NM = 0.3    # ±0.3 nm full-scale on approach (RNAV/LPV)
_EARTH_R_NM             = 3440.065  # Earth mean radius (km/1.852)


def _ident_has_runways(ident: str) -> bool:
    """True when the airport ``ident`` has runway records loaded — used
    to decide whether to surface the APPR button on the nav-confirm
    modal."""
    if not ident or _runways is None:
        return False
    return len(_apr_runway_ends(ident)) > 0


def _nav_geo_dist_brg(la1, lo1, la2, lo2):
    """Great-circle distance (nm) and initial bearing (deg) from 1 to 2.
    Haversine + spherical-law-of-cosines bearing — accurate at any leg
    length, unlike flat-earth approximations that drift on long legs."""
    phi1 = math.radians(la1)
    phi2 = math.radians(la2)
    dphi = math.radians(la2 - la1)
    dlam = math.radians(lo2 - lo1)
    a = (math.sin(dphi * 0.5) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlam * 0.5) ** 2)
    dist = 2.0 * _EARTH_R_NM * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    y = math.sin(dlam) * math.cos(phi2)
    x = (math.cos(phi1) * math.sin(phi2)
         - math.sin(phi1) * math.cos(phi2) * math.cos(dlam))
    brg = math.degrees(math.atan2(y, x)) % 360.0
    return dist, brg


def _nav_xtk_nm(act_lat, act_lon, wpt_lat, wpt_lon, cur_lat, cur_lon):
    """Signed great-circle cross-track distance (nm) from the course
    (act → wpt) to the current position.  Positive = right of course."""
    d13, brg13 = _nav_geo_dist_brg(act_lat, act_lon, cur_lat, cur_lon)
    _,   brg12 = _nav_geo_dist_brg(act_lat, act_lon, wpt_lat, wpt_lon)
    if d13 < 1e-6:
        return 0.0
    return _EARTH_R_NM * math.asin(
        math.sin(d13 / _EARTH_R_NM)
        * math.sin(math.radians(brg13 - brg12))
    )


def _sim_intercept_heading(course_deg, xtk_nm, max_intercept=45.0,
                           approach=False):
    """Standard avionics intercept logic for the sim's FOLLOW FLT-PLAN
    autopilot.  Returns a target heading (true degrees) that brings the
    aircraft onto the course at a 45° intercept angle when far from
    track, ramping down to a gentle XTK correction once within the
    inner band.

    ``course_deg`` is the direction the course flows (TOWARD the
    destination), in true degrees.  ``xtk_nm`` is signed cross-track
    distance, positive = aircraft right of course.  The caller picks
    the right geometry for its leg length: spherical XTK
    (``_nav_xtk_nm``) for D2 legs that can run thousands of nm, or a
    cheap flat-earth projection at the threshold for short approach
    finals where the two are equivalent.

    ``approach=True`` switches to tighter approach scaling that
    matches the ±0.3 nm CDI: gentle band shrinks from 0.3 → 0.1 nm,
    full intercept reached at 0.5 nm instead of 1.5 nm, and the gentle
    gain triples so the AP actually settles on centreline rather than
    wallowing at half-scale deflection.

    Convention: positive XTK = aircraft is right of course → returned
    heading is to the LEFT of ``course_deg``."""
    if approach:
        gentle_nm   = 0.1
        full_nm     = 0.5
        gentle_gain = 200.0   # deg per nm — steeper so it drives onto centreline
        gentle_cap  = 32.0
    else:
        gentle_nm   = 0.3
        full_nm     = 1.5
        gentle_gain = 30.0
        gentle_cap  = 20.0

    xtk_abs = abs(xtk_nm)
    if xtk_abs < gentle_nm:
        correction = max(-gentle_cap, min(gentle_cap, -xtk_nm * gentle_gain))
    elif xtk_abs >= full_nm:
        correction = -max_intercept if xtk_nm > 0 else max_intercept
    else:
        t = (xtk_abs - gentle_nm) / (full_nm - gentle_nm)
        sign = -1.0 if xtk_nm > 0 else 1.0
        gentle = -xtk_nm * gentle_gain
        correction = gentle * (1.0 - t) + max_intercept * sign * t
    return (course_deg + correction) % 360


def _nav_gc_interp(la1, lo1, la2, lo2, f):
    """Lat/lon at fraction f ∈ [0, 1] along the great circle from 1 to 2.
    Standard slerp on the unit sphere; degenerates gracefully when the
    endpoints coincide."""
    phi1 = math.radians(la1); lam1 = math.radians(lo1)
    phi2 = math.radians(la2); lam2 = math.radians(lo2)
    dphi = phi2 - phi1
    dlam = lam2 - lam1
    a = (math.sin(dphi * 0.5) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlam * 0.5) ** 2)
    d = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    if d < 1e-9:
        return la1, lo1
    sd = math.sin(d)
    A = math.sin((1.0 - f) * d) / sd
    B = math.sin(f * d) / sd
    x = A * math.cos(phi1) * math.cos(lam1) + B * math.cos(phi2) * math.cos(lam2)
    y = A * math.cos(phi1) * math.sin(lam1) + B * math.cos(phi2) * math.sin(lam2)
    z = A * math.sin(phi1) + B * math.sin(phi2)
    return (math.degrees(math.atan2(z, math.hypot(x, y))),
            math.degrees(math.atan2(y, x)))


def _airports_exact(ident: str):
    """Airport lookup matching the ICAO `ident` (KP14) OR the FAA Local ID
    (P14) — pilots type either.  → (ident, lat, lon, elev_ft, name, region)
    or None.  The returned ident is the canonical DB ident (what runway /
    approach lookups key on)."""
    if _airports is None or not ident:
        return None
    if hasattr(_airports, "dtype"):
        names = _airports.dtype.names or ()
        mask = (_airports["ident"] == ident)
        if "local" in names:
            mask = mask | (_airports["local"] == ident)
        rows = _airports[mask]
        if len(rows) == 0:
            return None
        row = rows[0]
        name   = str(row["name"])   if "name"   in names else ""
        region = str(row["region"]) if "region" in names else ""
        local  = str(row["local"])  if "local"  in names else ""
        return (str(row["ident"]), float(row["lat"]),
                float(row["lon"]), float(row["elev_ft"]), name, region, local)
    for rec in _airports:
        if rec[0] == ident or (len(rec) > 7 and rec[7] == ident):
            name   = str(rec[5]) if len(rec) > 5 else ""
            region = str(rec[6]) if len(rec) > 6 else ""
            local  = str(rec[7]) if len(rec) > 7 else ""
            return (rec[0], float(rec[2]), float(rec[3]), float(rec[4]),
                    name, region, local)
    return None


def _nav_lookup_ident(ident: str):
    """Return (ident, lat, lon, elev_ft, name, region) for the airport with
    this exact ident, or None.  Shared between the activate path, the
    confirmation modal, and the FPL append path (which stores the name/region
    for the flight-plan row).  Callers that only need position read [1]/[2].

    Exact match only — idents are looked up as the DB stores them (P14, KSEZ,
    0G6 …).  No automatic K add/strip: a VOR like FLG must never be silently
    treated as the airport KFLG, and a field whose ident has no K (P14) must
    not be forced to one."""
    if _airports is None or not ident:
        return None
    return _airports_exact(ident.strip().upper())


def _nav_set_by_ident(ident: str) -> bool:
    """Activate direct-to to the airport with this ident.  Returns True on hit."""
    hit = _nav_lookup_ident(ident)
    if hit is None:
        return False
    lat = disp.get("lat", 0.0)
    lon = disp.get("lon", 0.0)
    ai, alat, alon, aelev = hit[:4]
    disp["nav"]["ident"]   = ai
    disp["nav"]["lat"]     = alat
    disp["nav"]["lon"]     = alon
    disp["nav"]["elev_ft"] = aelev
    disp["nav"]["act_lat"] = lat
    disp["nav"]["act_lon"] = lon
    _settings.mark_dirty()
    _ssync_publish_nav()
    return True


# Cached nearest-airport ident.  query_nearby with a 100 nm radius
# costs ~1 ms — fine on demand, wasteful at 30 Hz when the keyboard or
# AIRPORTS screen is up.  Refresh when the aircraft has moved >0.6 nm
# (lat/lon rounded to 0.01°) or 2 s has elapsed.
_nav_nearest_cache = {"lat": None, "lon": None, "ident": "", "ts": 0.0}


def _nav_lookup_nearest():
    """Return the ident of the nearest public airport (S/M/L) within
    100 nm, or "" if no airports / no fix.  Result is cached for ~0.6 nm
    of motion or 2 s of wall time so repeated render-frame calls from
    the keyboard / AIRPORTS screen don't hammer the spatial query."""
    if _airports is None:
        return ""
    lat = disp.get("lat", 0.0)
    lon = disp.get("lon", 0.0)
    rlat = round(lat, 2)
    rlon = round(lon, 2)
    now = time.monotonic()
    c = _nav_nearest_cache
    if (rlat == c["lat"] and rlon == c["lon"]
            and now - c["ts"] < 2.0):
        return c["ident"]
    nearby = apt_mod.query_nearby(_airports, lat, lon, radius_nm=100.0)
    ident = ""
    if nearby is not None and len(nearby) > 0:
        if hasattr(nearby, "dtype"):
            for i in range(len(nearby)):
                if str(nearby["atype"][i]) in ("S", "M", "L"):
                    ident = str(nearby["ident"][i])
                    break
        else:
            for apt in nearby:
                if apt.atype in ("S", "M", "L"):
                    ident = apt.ident
                    break
    c["lat"] = rlat
    c["lon"] = rlon
    c["ts"]  = now
    c["ident"] = ident
    return ident


def _nav_set_nearest() -> bool:
    """Activate direct-to to the nearest public airport (S/M/L) within 100 nm."""
    ident = _nav_lookup_nearest()
    if not ident:
        return False
    return _nav_set_by_ident(ident)


def _nav_clear() -> None:
    disp["nav"]["ident"]   = ""
    disp["nav"]["lat"]     = 0.0
    disp["nav"]["lon"]     = 0.0
    disp["nav"]["elev_ft"] = 0.0
    disp["nav"]["act_lat"] = 0.0
    disp["nav"]["act_lon"] = 0.0
    _settings.mark_dirty()
    _ssync_publish_nav()


_DIRECT_TO_DRAPE_OFFSET_FT = 200.0  # ft above terrain.  Has to clear the
                                    # GPU mesh's bilinear interpolation
                                    # between its ~250 m grid samples — at
                                    # narrow ridges that interpolated
                                    # surface can sit hundreds of feet
                                    # above the DEM cell our line samples.

# Cached trace vertex array.  Key = (act_lat, act_lon, wpt_lat, wpt_lon);
# rebuilt only when the active direct-to changes.  Without this, the per-
# frame DEM sampling costs ~5 µs per step × hundreds of steps and visibly
# drops the GL frame rate.
#
# The actual SRTM sampling happens off-thread because cold-tile reads on
# a long course (up to 400 steps × several ms each) used to freeze the
# screen for 1-4 seconds after the user pressed ENTER on a new waypoint.
_direct_to_trace_cache_key = None
_direct_to_trace_cache_arr = None
_direct_to_trace_thread    = None


def _build_direct_to_trace_async(key, wpt_elev):
    """Worker: sample the great-circle course over SRTM and publish
    partial vertex arrays to the cache as we go.  The near end of the
    magenta line appears within the first second; the far end fills in
    as more SRTM tiles are read.  Exits early if the user changes the
    active direct-to mid-build."""
    global _direct_to_trace_cache_arr
    act_lat, act_lon, wpt_lat, wpt_lon = key
    course_nm, _ = _nav_geo_dist_brg(act_lat, act_lon, wpt_lat, wpt_lon)
    if course_nm < 0.05:
        return
    # 0.2 nm step: catches Sedona-grade terrain features that fit
    # between coarser samples.  Cap at 1000 so very long courses
    # don't blow the SRTM read budget on the worker thread.
    n_steps = max(8, int(course_nm / 0.2))
    n_steps = min(n_steps, 1000)

    # Publish the partial vertex array every PUBLISH_EVERY samples.
    # Sampling proceeds near→far (i=0 is the activation point near the
    # aircraft), so each publish extends the visible line outward.
    PUBLISH_EVERY = 8
    try:
        import numpy as _np
        _have_np = True
    except ImportError:
        _have_np = False

    # Track raw terrain elevations (pre-drape) so we can apply a
    # rolling max over each vertex's two neighbours before the
    # offset is added.  Without the rolling max, the line between
    # two valley samples can still dip below a peak that sits
    # between them.
    raw_elevs = []
    pts = []
    for i in range(0, n_steps + 1):
        # Bail if the user changed direct-to — the next call to
        # build_direct_to_trace_vertices() will overwrite the cache key
        # and start a fresh worker for the new course.
        if _direct_to_trace_cache_key != key:
            return
        t = i / n_steps
        s_lat, s_lon = _nav_gc_interp(act_lat, act_lon, wpt_lat, wpt_lon, t)
        try:
            terrain_elev = get_elevation_ft(SRTM_DIR, s_lat, s_lon)
            if terrain_elev is None or terrain_elev < -100:
                terrain_elev = wpt_elev
        except Exception:
            terrain_elev = wpt_elev
        raw_elevs.append(terrain_elev)

        # Rolling max over (i-1, i, i+1) becomes the publish elev for
        # vertex i.  Vertex i+1 isn't sampled yet, so we fall back to
        # max(i-1, i) for the trailing vertex; it gets corrected when
        # i+1 lands on the next iteration.
        if i == 0:
            commit_idx = 0
            commit_elev = raw_elevs[0]
        else:
            commit_idx = i - 1
            commit_elev = max(raw_elevs[commit_idx],
                              raw_elevs[commit_idx + 1])
            if commit_idx > 0:
                commit_elev = max(commit_elev, raw_elevs[commit_idx - 1])
        # Recover the lat/lon for commit_idx
        ct = commit_idx / n_steps
        c_lat, c_lon = _nav_gc_interp(act_lat, act_lon, wpt_lat, wpt_lon, ct)
        # Either append a new vertex or rewrite the previous one with
        # the now-known forward neighbour.
        if commit_idx == len(pts):
            pts.append((c_lat, c_lon, commit_elev + _DIRECT_TO_DRAPE_OFFSET_FT))
        else:
            pts[commit_idx] = (c_lat, c_lon,
                               commit_elev + _DIRECT_TO_DRAPE_OFFSET_FT)
        # On the final iteration, also commit the last vertex (which
        # only has a backward neighbour).
        if i == n_steps:
            last_elev = max(raw_elevs[i], raw_elevs[i - 1])
            l_lat, l_lon = s_lat, s_lon
            if i == len(pts):
                pts.append((l_lat, l_lon,
                            last_elev + _DIRECT_TO_DRAPE_OFFSET_FT))
            else:
                pts[i] = (l_lat, l_lon,
                          last_elev + _DIRECT_TO_DRAPE_OFFSET_FT)

        if (i + 1) % PUBLISH_EVERY == 0 or i == n_steps:
            arr = _np.array(pts, dtype=_np.float32) if _have_np else list(pts)
            # Re-check ownership immediately before publishing.  In
            # CPython attribute writes are atomic, so the render thread
            # always sees either the previous snapshot or this one.
            if _direct_to_trace_cache_key == key:
                _direct_to_trace_cache_arr = arr


# ── Full approach course trace (magenta, all legs) ──────────────────────────
# The whole approach drawn as a magenta course line on the PFD (like the FPL
# legs), in addition to the cyan HITS corridor.  Terrain-draped along every
# approach leg (IAF → threshold), bright magenta, built on a daemon thread so
# the SRTM sampling never hitches the render loop; rebuilt only when the loaded
# approach changes.
_APPR_TRACE_COLOR = (220 / 255.0, 0.0, 220 / 255.0, 1.0)   # bright magenta
_appr_trace_cache = {"key": None, "arr": None}
_appr_trace_thread = None


def _build_approach_trace_async(key):
    global _appr_trace_cache
    legs = list(key)
    out = []
    for (a_lat, a_lon), (b_lat, b_lon) in zip(legs[:-1], legs[1:]):
        if _appr_trace_cache["key"] != key:
            return                      # approach changed mid-build — drop it
        dist_nm, _ = _nav_geo_dist_brg(a_lat, a_lon, b_lat, b_lon)
        n = max(2, min(80, int(dist_nm / 0.4)))
        for i in range(n + 1):
            if i == 0 and out:
                continue                # skip the shared vertex at a leg join
            t = i / n
            s_lat, s_lon = _nav_gc_interp(a_lat, a_lon, b_lat, b_lon, t)
            try:
                e = get_elevation_ft(SRTM_DIR, s_lat, s_lon)
                if e is None or e < -100:
                    e = 0.0
            except Exception:
                e = 0.0
            out.append((s_lat, s_lon, e + _DIRECT_TO_DRAPE_OFFSET_FT))
    if len(out) < 2:
        return
    try:
        import numpy as _np
        arr = _np.array(out, dtype=_np.float32)
    except Exception:
        arr = out
    if _appr_trace_cache["key"] == key:
        _appr_trace_cache["arr"] = arr


def build_approach_trace_vertices():
    """Cached terrain-draped magenta trace along the whole loaded approach, or
    None when no approach is loaded/active.  Starts a worker on a change."""
    global _appr_trace_thread
    ap = disp.get("approach") or {}
    if not (ap.get("loaded") or ap.get("active")):
        _appr_trace_cache["key"] = None
        _appr_trace_cache["arr"] = None
        return None
    legs = ap.get("legs") or []
    if len(legs) < 2:
        _appr_trace_cache["key"] = None
        _appr_trace_cache["arr"] = None
        return None
    key = tuple((float(l[0]), float(l[1])) for l in legs)
    if key == _appr_trace_cache["key"]:
        return _appr_trace_cache["arr"]
    _appr_trace_cache["key"] = key
    _appr_trace_cache["arr"] = None
    _appr_trace_thread = threading.Thread(
        target=_build_approach_trace_async, args=(key,), daemon=True,
        name="approach-trace")
    _appr_trace_thread.start()
    return None


# ── Next-leg trace (faded magenta, matches the MFD) ─────────────────────────
# The PFD already draws the ACTIVE leg as a bright-magenta terrain-following
# trace.  This adds the NEXT leg (active waypoint → the one after it) as a
# faded-magenta trace, the same (140,0,140) the MFD uses for remaining legs, so
# the two displays agree.  Built on a daemon thread (like the direct-to trace)
# so the SRTM sampling never hitches the render loop; rebuilt only when the leg
# changes.
_NEXT_LEG_COLOR = (140 / 255.0, 0.0, 140 / 255.0, 1.0)
_next_leg_trace_cache = {"key": None, "arr": None}
_next_leg_trace_thread = None


def _build_next_leg_trace_async(key):
    global _next_leg_trace_cache
    a_lat, a_lon, b_lat, b_lon = key
    dist_nm, _ = _nav_geo_dist_brg(a_lat, a_lon, b_lat, b_lon)
    if dist_nm < 0.05:
        return
    n = max(8, min(120, int(dist_nm / 0.5)))
    pts = []
    for i in range(n + 1):
        if _next_leg_trace_cache["key"] != key:
            return                      # leg changed mid-build — drop this one
        t = i / n
        s_lat, s_lon = _nav_gc_interp(a_lat, a_lon, b_lat, b_lon, t)
        try:
            e = get_elevation_ft(SRTM_DIR, s_lat, s_lon)
            if e is None or e < -100:
                e = 0.0
        except Exception:
            e = 0.0
        pts.append((s_lat, s_lon, e + _DIRECT_TO_DRAPE_OFFSET_FT))
    try:
        import numpy as _np
        arr = _np.array(pts, dtype=_np.float32)
    except Exception:
        arr = pts
    if _next_leg_trace_cache["key"] == key:
        _next_leg_trace_cache["arr"] = arr


def build_next_leg_trace_vertices():
    """Cached terrain-following trace for the leg AFTER the active one, or None
    when there's no next leg.  Starts a fresh worker on a leg change."""
    global _next_leg_trace_thread
    rem = _fpl_render_remaining()
    if not rem or len(rem) < 2:
        _next_leg_trace_cache["key"] = None
        _next_leg_trace_cache["arr"] = None
        return None
    a_lat, a_lon, _ = rem[0]
    b_lat, b_lon, _ = rem[1]
    key = (float(a_lat), float(a_lon), float(b_lat), float(b_lon))
    if key == _next_leg_trace_cache["key"]:
        return _next_leg_trace_cache["arr"]
    _next_leg_trace_cache["key"] = key
    _next_leg_trace_cache["arr"] = None
    _next_leg_trace_thread = threading.Thread(
        target=_build_next_leg_trace_async, args=(key,), daemon=True,
        name="next-leg-trace")
    _next_leg_trace_thread.start()
    return None


def build_direct_to_trace_vertices():
    """Return the cached trace for the active direct-to.  May return a
    partial vertex array while the background sampler is still walking
    the course — the magenta line grows from the aircraft outward as
    more vertices are added.  Returns None until the worker publishes
    its first chunk (~4 samples / 2 nm)."""
    global _direct_to_trace_cache_key, _direct_to_trace_cache_arr
    global _direct_to_trace_thread

    nv = disp.get("nav", {})
    if not nv.get("ident"):
        _direct_to_trace_cache_key = None
        _direct_to_trace_cache_arr = None
        return None
    wpt_lat  = float(nv["lat"])
    wpt_lon  = float(nv["lon"])
    wpt_elev = float(nv.get("elev_ft", 0.0))
    act_lat  = float(nv.get("act_lat", 0.0))
    act_lon  = float(nv.get("act_lon", 0.0))
    if act_lat == 0.0 and act_lon == 0.0:
        return None

    key = (act_lat, act_lon, wpt_lat, wpt_lon)
    if key == _direct_to_trace_cache_key:
        # Either still building or fully done — return whatever's been
        # published so far so the renderer can show the partial line.
        return _direct_to_trace_cache_arr

    # New course.  Claim the cache key and invalidate the stale arr;
    # any in-flight worker for the previous course will see the
    # mismatched key on its next iteration and exit before stomping
    # the new cache.  We always start a fresh worker (we don't poll
    # is_alive) so back-to-back waypoint changes don't drop a build.
    _direct_to_trace_cache_key = key
    _direct_to_trace_cache_arr = None
    _direct_to_trace_thread = threading.Thread(
        target=_build_direct_to_trace_async,
        args=(key, wpt_elev),
        daemon=True,
        name="direct-to-trace",
    )
    _direct_to_trace_thread.start()
    return None


def draw_direct_to_trace(surf, ai_rect, lat, lon, alt_ft,
                         hdg_deg, pitch_deg, roll_deg):
    """2D pygame fallback: solid magenta course trace projected without
    depth occlusion.  Used only when the OpenGL SVT path is unavailable;
    in the GL path the trace renders through the depth buffer via
    svt_renderer_gl.render_polyline_latlonelev so ridges occlude it
    naturally."""
    verts = build_direct_to_trace_vertices()
    if verts is None or len(verts) < 2:
        return

    ax, ay_r, aw, ah = ai_rect
    cx_ai = ax + aw // 2
    cy_ai = ay_r + ah // 2
    px_per_deg = ah / 48.0

    old_clip = surf.get_clip()
    surf.set_clip(pygame.Rect(ax, ay_r, aw, ah))

    prev_pt = None
    for s_lat, s_lon, elev_ft in verts:
        pt = _project_latlon(float(s_lat), float(s_lon), lat, lon, alt_ft,
                             float(elev_ft),
                             hdg_deg, pitch_deg, roll_deg, cx_ai, cy_ai,
                             px_per_deg, max_fov_deg=None,
                             ground_only=False)
        if pt is None:
            prev_pt = None
            continue
        if prev_pt is not None:
            pygame.draw.line(surf, MAGENTA, prev_pt, pt, 3)
        prev_pt = pt

    surf.set_clip(old_clip)


# ── AGL readout (lower-right, just left of alt tape) ──────────────────────────
# Two-line stack: small "AGL" label on top, value below.  Grew vertically
# to give 4-digit values (e.g. 5,500) full clearance on either side of
# the inset box.
_AGL_W = 78
_AGL_H = 42


def draw_agl_readout(surf, alt_ft, ground_elev_ft, gps_ok):
    """Show "AGL\\n1234" in the lower-right corner of the AI region —
    just left of the altitude tape and just above the heading tape.
    Hidden when there's no GPS fix (no terrain lookup) or when the
    SRTM sample comes back as None / clearly invalid (out of coverage)."""
    if not gps_ok or ground_elev_ft is None:
        return
    if ground_elev_ft < -100.0:
        # Out of SRTM coverage (terrain.py returns extreme negatives
        # for missing tiles); don't display rather than misleading.
        return

    bx = ALT_X - 2 - _AGL_W
    by = HDG_Y - 2 - _AGL_H

    plate = pygame.Surface((_AGL_W, _AGL_H), pygame.SRCALPHA)
    plate.fill((0, 8, 22, 180))
    surf.blit(plate, (bx, by))
    pygame.draw.rect(surf, (140, 150, 170), (bx, by, _AGL_W, _AGL_H),
                     width=1, border_radius=4)

    # Round to the nearest 10 ft.  Both the GPS altitude and the SRTM
    # terrain sample have ~10–30 ft of real precision, so a 1-ft display
    # only shows GPS/DEM jitter in the last digit.
    agl_ft = int(round((alt_ft - ground_elev_ft) / 10.0)) * 10
    # Below the ground in the SRTM sample is sensor / DEM disagreement
    # (baro miss-set, runway elev vs SRTM, missing tile), not useful
    # info — show dashes rather than a misleading negative value.
    if agl_ft <= 0:
        val_str = "---"
        val_col = (170, 185, 210)
    else:
        val_str = f"{agl_ft:,}"
        val_col = WHITE
    _text(surf, "AGL", 11, (170, 185, 210), bold=True,
          cx=bx + _AGL_W // 2, cy=by + 11)
    _text(surf, val_str, 18, val_col, bold=True,
          cx=bx + _AGL_W // 2, cy=by + _AGL_H - 14)


def draw_cdi(surf):
    """Course Deviation Indicator strip above the heading readout box.

    Classic XTK CDI: the diamond shows where the activation→waypoint great
    circle is relative to your current position.  Right-of-course → course
    is to your left → diamond deflects LEFT → fly LEFT to intercept (fly
    TO the needle).  Full-scale at ±_CDI_FULL_SCALE_NM.

    With no active waypoint we still draw the empty bar + a "DIRECT  →"
    placeholder so the strip is always tappable and the pilot has a fixed
    entry point for the keyboard.  Caller gates the whole thing on
    gps_ok — a fix is required before the strip means anything."""
    nv = disp.get("nav", {})
    ident = nv.get("ident", "")
    have_wpt = bool(ident)

    # Bar geometry: sit just above the heading readout box.  Box height is
    # max(28, font_h+8); place the bar with a small margin above.
    bar_w = max(140, int(DISPLAY_W * 0.20))
    bar_h = 6
    bar_y = HDG_Y - 50            # leaves room for the readout box (28-32 px)
    bar_x = CX - bar_w // 2

    # Translucent backplate sized for the larger readout font
    plate = pygame.Surface((bar_w + 36, 44), pygame.SRCALPHA)
    plate.fill((0, 8, 22, 180))
    surf.blit(plate, (bar_x - 18, bar_y - 32))

    # Bar + tick marks (centre + ±50% + full-scale dots)
    pygame.draw.rect(surf, (60, 80, 110), (bar_x, bar_y, bar_w, bar_h),
                     border_radius=2)
    for frac in (-1.0, -0.5, 0.0, 0.5, 1.0):
        tx = bar_x + int((frac + 1.0) * 0.5 * bar_w)
        if frac == 0.0:
            pygame.draw.line(surf, WHITE,
                             (tx, bar_y - 4), (tx, bar_y + bar_h + 4), 2)
        else:
            pygame.draw.circle(surf, (180, 200, 220),
                               (tx, bar_y + bar_h // 2), 2)

    if have_wpt:
        lat = disp.get("lat", 0.0)
        lon = disp.get("lon", 0.0)
        wpt_lat = float(nv["lat"])
        wpt_lon = float(nv["lon"])

        dist_nm, brg = _nav_geo_dist_brg(lat, lon, wpt_lat, wpt_lon)

        ap = disp.get("approach") or {}
        appr_missed = ap.get("missed") and ap.get("airport") == ident
        # Both the active approach and a synthetic missed track the runway
        # centreline (missed = "climb straight ahead on runway heading").
        appr_active = (ap.get("active") or appr_missed) and ap.get("airport") == ident
        if appr_active:
            # Approach: CDI reference is the extended runway centreline
            # (threshold + published course) — NOT the line from the
            # pilot's activation point to the threshold, which only
            # coincides with the centreline if they tapped DIRECT while
            # already perfectly lined up.
            cl_lat = float(ap["thresh_lat"])
            cl_lon = float(ap["thresh_lon"])
            course_deg = float(ap["course_deg"])
            cos_lat = max(1e-6, math.cos(math.radians(cl_lat)))
            de_nm = (lon - cl_lon) * 60.0 * cos_lat
            dn_nm = (lat - cl_lat) * 60.0
            course_rad = math.radians(course_deg)
            xtk = (de_nm * math.cos(course_rad)
                   - dn_nm * math.sin(course_rad))
            full_scale = _CDI_APPR_FULL_SCALE_NM
        else:
            act_lat = float(nv.get("act_lat", lat))
            act_lon = float(nv.get("act_lon", lon))
            xtk = _nav_xtk_nm(act_lat, act_lon, wpt_lat, wpt_lon, lat, lon)
            full_scale = _CDI_FULL_SCALE_NM

        # Diamond shows where the course line is relative to the aircraft.
        # Right-of-course (positive xtk) → diamond LEFT (course is to your left).
        xtk_clamped = max(-1.0, min(1.0, xtk / full_scale))
        dx_diamond = -int(xtk_clamped * (bar_w / 2))
        dcx = CX + dx_diamond
        dcy = bar_y + bar_h // 2
        dpts = [(dcx, dcy - 9), (dcx + 8, dcy), (dcx, dcy + 9), (dcx - 8, dcy)]
        _filled_polygon(surf, dpts, MAGENTA)

        # Readout: ident · BRG · DIST — centred above the bar.  Font 50% larger
        # than original (11→16); positioned so the text bottom sits 3 px higher
        # than the original layout would have placed it.  When an approach is
        # active, append the runway suffix to the airport ident.
        if ap.get("missed"):
            # Missed: amber readout to the missed fix (published) or the
            # runway-heading climb advisory (synthetic, ident == airport).
            ident_lbl = f"{ident} ▲MA"
            read_col = (240, 150, 60)
        elif appr_active:
            ident_lbl = f"{ident}/{ap['runway']}"
            read_col = MAGENTA
        else:
            ident_lbl = ident
            read_col = MAGENTA
        readout = f"{ident_lbl}  {int(round(brg)) % 360:03d}°  {dist_nm:.1f}NM"
        _text(surf, readout, 16, read_col, bold=True, cx=CX, cy=bar_y - 20)
    else:
        # No active waypoint — leave the bar empty (no diamond) and show
        # the "DIRECT  →" affordance so the pilot knows tapping opens the
        # keyboard.
        _text(surf, "DIRECT  →", 16, MAGENTA, bold=True, cx=CX, cy=bar_y - 20)


# ── Vertical Deviation Indicator (glideslope) ────────────────────────────────

_VDI_FULL_SCALE_DEG = 0.7   # ±0.7° full-scale (LPV / ILS-equivalent)


def draw_vdi(surf):
    """Vertical deviation indicator — only painted when an approach is
    active.  Sits just inside the right edge of the AI (left of the
    altitude tape) so the pilot's eye can sweep CDI → AI → VDI without
    leaving the primary scan.

    Convention matches every modern PFD: the diamond shows where the
    glideslope is relative to the aircraft.  Above GS → diamond moves
    DOWN (fly down to the diamond); below GS → diamond moves UP."""
    ap = disp.get("approach") or {}
    if not ap.get("active"):
        return

    lat = float(disp.get("lat", 0.0))
    lon = float(disp.get("lon", 0.0))
    alt = float(disp.get("alt", 0.0))   # baro altitude, ft
    th_lat  = float(ap["thresh_lat"])
    th_lon  = float(ap["thresh_lon"])
    th_elev = float(ap["thresh_elev_ft"])

    dist_nm, _ = _nav_geo_dist_brg(lat, lon, th_lat, th_lon)
    dist_ft = dist_nm * 6076.12
    if dist_ft < 100.0:
        return  # over the threshold — VDI no longer meaningful

    # Deviation from the PUBLISHED vertical profile (the altitude the HITS boxes
    # show at this distance), expressed as the angle the altitude error subtends
    # at the current range so sensitivity tightens near the runway like a GS.
    # Falls back to a flat 3° glideslope for a synthetic approach.
    prof_alt = _approach_target_alt(lat, lon)
    if prof_alt is None:
        prof_alt = th_elev + dist_ft * math.tan(math.radians(3.0))
    dev_deg = math.degrees(math.atan2(alt - prof_alt, dist_ft))  # + high, - low

    # Bar geometry — vertical strip just inside the alt tape.
    bar_w = 6
    bar_h = max(160, int(AI_H * 0.55))
    bar_x = ALT_X - 24 - bar_w // 2
    bar_y = AI_Y + (AI_H - bar_h) // 2

    plate_w = 36
    plate = pygame.Surface((plate_w, bar_h + 40), pygame.SRCALPHA)
    plate.fill((0, 8, 22, 180))
    surf.blit(plate, (bar_x - (plate_w - bar_w) // 2, bar_y - 20))

    pygame.draw.rect(surf, (60, 80, 110), (bar_x, bar_y, bar_w, bar_h),
                     border_radius=2)

    # Tick marks (centre + ±50% + full-scale).  Centre line crosses the
    # bar; outer dots mark the 0.35° / 0.7° boundaries.
    cx_bar = bar_x + bar_w // 2
    for frac in (-1.0, -0.5, 0.0, 0.5, 1.0):
        ty = bar_y + int((frac + 1.0) * 0.5 * bar_h)
        if frac == 0.0:
            pygame.draw.line(surf, WHITE,
                             (bar_x - 4, ty), (bar_x + bar_w + 4, ty), 2)
        else:
            pygame.draw.circle(surf, (180, 200, 220), (cx_bar, ty), 2)

    # Diamond — clamped to ±full-scale.  Above GS → diamond moves DOWN.
    dev_clamped = max(-1.0, min(1.0, dev_deg / _VDI_FULL_SCALE_DEG))
    dy_diamond = int(dev_clamped * (bar_h / 2))
    dcx = cx_bar
    dcy = bar_y + bar_h // 2 + dy_diamond
    dpts = [(dcx, dcy - 8), (dcx + 9, dcy), (dcx, dcy + 8), (dcx - 9, dcy)]
    _filled_polygon(surf, dpts, MAGENTA)

    _text(surf, "G", 12, (180, 200, 220), bold=True,
          cx=cx_bar, cy=bar_y - 10)
    _text(surf, "S", 12, (180, 200, 220), bold=True,
          cx=cx_bar, cy=bar_y + bar_h + 10)


# ── Runway polygons + extended centerlines ───────────────────────────────────

_RUNWAY_MAX_RANGE_NM       = 8.0    # only draw runways within this range
_CENTERLINE_RANGE_NM       = 15.0   # draw extended centerlines within this range
_CENTERLINE_EXTEND_NM      = 5.0    # centerline extends this far from threshold
_CENTERLINE_DASH_NM        = 0.5    # dash length (nm)
_CENTERLINE_GLIDE_DEG      = 3.0    # rise centerline at standard glideslope

# Runway corners use the surveyed threshold elevation directly — the
# polygon stays on the ground where it belongs.  Earlier versions
# tried to "anchor" the corner elev to the aircraft's altitude when
# close, but that made the polygon track your altitude (not the
# ground), so descending through it looked like the runway rose to
# meet you and then vanished as the anchor disengaged.  Whatever
# small wobble baro/GPS noise produces near a corner is honest sensor
# behaviour, not something to mask.


def _clip_polygon_forward(corners, ref_lat, ref_lon, hdg_rad,
                          nm_per_deg_lat, nm_per_deg_lon):
    """Sutherland-Hodgman clip of a polygon against the perpendicular
    line through the aircraft (the "forward" half-plane).  Input is a
    list of (lat, lon, elev) tuples; output is the clipped polygon
    (also (lat, lon, elev)).  Empty list ⇒ polygon is entirely
    behind.

    Used by the runway draw to keep the polygon visible during a
    fly-over / takeoff roll, where one threshold is ahead and the
    other is behind — the unclipped quad would project corners to
    opposite ends of the AI and paint across the whole screen."""
    if not corners:
        return []

    cos_h = math.cos(hdg_rad)
    sin_h = math.sin(hdg_rad)

    def _fwd(p):
        # Dot product of (p - aircraft) with the heading unit vector
        # in nm.  Positive = ahead.
        dlat_nm = (p[0] - ref_lat) * nm_per_deg_lat
        dlon_nm = (p[1] - ref_lon) * nm_per_deg_lon
        return dlat_nm * cos_h + dlon_nm * sin_h

    fwds = [_fwd(p) for p in corners]
    n = len(corners)
    out = []
    for i in range(n):
        A   = corners[i]
        B   = corners[(i + 1) % n]
        fA  = fwds[i]
        fB  = fwds[(i + 1) % n]
        if fA >= 0:
            out.append(A)
        # Edge crosses the forward / behind boundary — interpolate the
        # crossing point and add it to the output polygon.
        if (fA >= 0) != (fB >= 0):
            t = fA / (fA - fB)
            out.append((
                A[0] + t * (B[0] - A[0]),
                A[1] + t * (B[1] - A[1]),
                A[2] + t * (B[2] - A[2]),
            ))
    return out


def _project_latlon(lat_deg, lon_deg, ref_lat, ref_lon, ref_alt_ft,
                    elev_ft, hdg_deg, pitch_deg, roll_deg,
                    cx, cy, px_per_deg, max_fov_deg=None,
                    ground_only=False, nm_per_deg_lon=None):
    """Project a lat/lon/elevation point onto the AI screen.
    Returns (sx, sy), or None when culled.
    max_fov_deg: cull points whose bearing is more than this off the nose.
    ground_only: cull points that aren't physically ground features in
                 front of the aircraft — i.e. above our altitude or
                 behind us.  We don't cull on screen-space position any
                 more (the previous syr<0 check rejected valid below-
                 horizon targets when pitch was more nose-down than the
                 angle to them, which made runways disappear on steep
                 descent).
    nm_per_deg_lon: optional precomputed 60·cos(ref_lat) — pass it from
                 hot loops (runway/centerline draws) so we don't redo
                 the cos every call."""
    nm_per_deg_lat = 60.0
    if nm_per_deg_lon is None:
        nm_per_deg_lon = 60.0 * math.cos(math.radians(ref_lat))
    dlat_nm = (lat_deg - ref_lat) * nm_per_deg_lat
    dlon_nm = (lon_deg - ref_lon) * nm_per_deg_lon
    dist_nm = math.hypot(dlat_nm, dlon_nm)
    if dist_nm < 0.001:
        return (cx, cy)
    bearing = math.degrees(math.atan2(dlon_nm, dlat_nm)) % 360.0
    rel_brg = (bearing - hdg_deg + 180) % 360 - 180
    if max_fov_deg is not None and abs(rel_brg) > max_fov_deg:
        return None
    if ground_only and abs(rel_brg) > 90.0:
        return None
    dist_ft = dist_nm * 6076.0
    alt_diff = elev_ft - ref_alt_ft
    vert_deg = math.degrees(math.atan2(alt_diff, dist_ft))
    if ground_only and vert_deg > 0.0:
        return None   # ground feature above our altitude — won't render
    sxr = rel_brg * px_per_deg
    syr = (pitch_deg - vert_deg) * px_per_deg
    cos_r = math.cos(math.radians(roll_deg))
    sin_r = math.sin(math.radians(roll_deg))
    return (int(cx + sxr * cos_r - syr * sin_r),
            int(cy + sxr * sin_r + syr * cos_r))


def draw_runway_symbols(surf, ai_rect, lat, lon, alt_ft,
                        hdg_deg, pitch_deg, roll_deg):
    """Project nearby runway polygons (and optional extended centerlines)
    onto the AI.  Runways anchor to their own threshold elevations so they
    sit flat on the terrain."""
    if _runways is None:
        return

    ad = disp["ad"]
    show_rwy   = ad.get("show_runways",     True)
    show_cline = ad.get("show_centerlines", True)
    if not (show_rwy or show_cline):
        return

    # When a direct-to waypoint is active, restrict extended centerlines to
    # the selected airport — keeps the approach picture uncluttered when
    # several airports are within the centerline range.
    sel_ident = (disp.get("nav") or {}).get("ident", "")

    nearby = rwy_mod.query_nearby(_runways, lat, lon,
                                  radius_nm=max(_RUNWAY_MAX_RANGE_NM,
                                                _CENTERLINE_RANGE_NM))
    if len(nearby) == 0:
        return

    ax, ay_r, aw, ah = ai_rect
    cx = ax + aw // 2
    cy = ay_r + ah // 2
    px_per_deg = ah / 48.0

    ASPHALT = (60, 60, 65)
    STRIPE  = (230, 230, 235)
    CLINE   = (220, 230, 240)
    BOX_COL = (60, 220, 80)   # green airport-environment box
    NUM_COL = (240, 240, 250) # runway-number text

    nm_per_deg_lat = 60.0
    nm_per_deg_lon = 60.0 * math.cos(math.radians(lat))
    hdg_rad        = math.radians(hdg_deg)

    def _proj(la, lo, elev):
        # ground_only=False — wrap / behind handling is done by the
        # forward-half polygon clip in the loop, not the projection.
        return _project_latlon(la, lo, lat, lon, alt_ft,
                               elev, hdg_deg, pitch_deg, roll_deg,
                               cx, cy, px_per_deg, ground_only=False,
                               nm_per_deg_lon=nm_per_deg_lon)

    def _fwd_dist_nm(la, lo):
        """Forward distance in nm — positive when ahead of the wing
        line.  Used to gate single-point projections (centerline
        midpoints) without running them through the polygon clip."""
        dlat_nm = (la - lat) * nm_per_deg_lat
        dlon_nm = (lo - lon) * nm_per_deg_lon
        return dlat_nm * math.cos(hdg_rad) + dlon_nm * math.sin(hdg_rad)

    def _in_ai(sx, sy):
        return ax <= sx <= ax + aw and ay_r <= sy <= ay_r + ah

    # Set the AI clip rect once around the whole pass — hoisted from the
    # per-runway loop so we don't burn dozens of pygame.set_clip calls
    # per frame at busy fields.
    ai_clip_rect = pygame.Rect(ax, ay_r, aw, ah)
    old_clip = surf.get_clip()
    surf.set_clip(ai_clip_rect)

    try:
        for r in nearby:
            # Distance to runway centre (for range culling)
            d_nm = math.hypot((r.centre_lat - lat) * nm_per_deg_lat,
                              (r.centre_lon - lon) * nm_per_deg_lon)

            # ── Runway polygon ────────────────────────────────────────────────
            if show_rwy and d_nm <= _RUNWAY_MAX_RANGE_NM:
                # Runway axis in lat/lon (he - le)
                ax_lat = r.he_lat - r.le_lat
                ax_lon = r.he_lon - r.le_lon
                axis_len_nm = math.hypot(ax_lat * nm_per_deg_lat,
                                         ax_lon * nm_per_deg_lon)
                if axis_len_nm < 0.01:
                    continue
                # Unit perpendicular in nm: rotate axis 90° CW
                perp_lat_nm =  (ax_lon * nm_per_deg_lon) / axis_len_nm
                perp_lon_nm = -(ax_lat * nm_per_deg_lat) / axis_len_nm
                half_w_nm = r.width_ft / 6076.0
                perp_lat = (perp_lat_nm * half_w_nm) / nm_per_deg_lat
                perp_lon = (perp_lon_nm * half_w_nm) / nm_per_deg_lon

                # Clip the runway quad against the forward half-plane
                # through the aircraft.  When you're on or past one
                # threshold (taxi, takeoff roll, low fly-over), the
                # behind half is dropped and the polygon stays
                # visible without painting across the AI.  Returns
                # 0, 3, 4, or 5 corners.
                quad = [
                    (r.le_lat + perp_lat, r.le_lon + perp_lon, r.le_elev_ft),
                    (r.he_lat + perp_lat, r.he_lon + perp_lon, r.he_elev_ft),
                    (r.he_lat - perp_lat, r.he_lon - perp_lon, r.he_elev_ft),
                    (r.le_lat - perp_lat, r.le_lon - perp_lon, r.le_elev_ft),
                ]
                quad = _clip_polygon_forward(
                    quad, lat, lon, hdg_rad,
                    nm_per_deg_lat, nm_per_deg_lon)
                if len(quad) < 3:
                    continue   # entirely behind us

                projected = [_proj(p[0], p[1], p[2]) for p in quad]
                if any(pt is None for pt in projected):
                    continue
                # Bounding-box check — close-up the polygon can be
                # bigger than the screen and all corners can sit off
                # the bottom while the polygon still crosses the AI.
                xs = [pt[0] for pt in projected]
                ys = [pt[1] for pt in projected]
                xs_min, xs_max = min(xs), max(xs)
                ys_min, ys_max = min(ys), max(ys)
                if (xs_max >= ax and xs_min <= ax + aw and
                        ys_max >= ay_r and ys_min <= ay_r + ah):
                    _filled_polygon(surf, projected, ASPHALT)
                    pygame.gfxdraw.aapolygon(surf, projected, STRIPE)

                    # Centreline: dashed segments from LE midpoint to
                    # HE midpoint.  Clip the midline against the
                    # forward half-plane too so the dashes (and the
                    # runway-number labels) don't draw behind us.
                    midline = _clip_polygon_forward(
                        [(r.le_lat, r.le_lon, r.le_elev_ft),
                         (r.he_lat, r.he_lon, r.he_elev_ft)],
                        lat, lon, hdg_rad,
                        nm_per_deg_lat, nm_per_deg_lon)
                    mid_le = mid_he = None
                    if len(midline) >= 2:
                        mid_le = _proj(midline[0][0], midline[0][1], midline[0][2])
                        mid_he = _proj(midline[1][0], midline[1][1], midline[1][2])
                    if mid_le is not None and mid_he is not None:
                        dx = mid_he[0] - mid_le[0]
                        dy = mid_he[1] - mid_le[1]
                        seg_len = math.hypot(dx, dy)
                        if seg_len > 4:
                            n_dashes = max(2, int(seg_len / 14))
                            inv_n = 1.0 / n_dashes
                            for k in range(0, n_dashes, 2):
                                t0 = k * inv_n
                                t1 = (k + 1) * inv_n
                                pygame.draw.aaline(
                                    surf, STRIPE,
                                    (mid_le[0] + dx * t0, mid_le[1] + dy * t0),
                                    (mid_le[0] + dx * t1, mid_le[1] + dy * t1))
                        # Runway numbers: offset 8 % in from each
                        # threshold.  Only draw the end whose actual
                        # threshold is still ahead of us — once you've
                        # passed it the marker would otherwise float
                        # on the runway surface where the LE / HE
                        # marker physically isn't.
                        if seg_len > 36:
                            sz = 11 if seg_len < 120 else 13
                            if _fwd_dist_nm(r.le_lat, r.le_lon) > 0:
                                le_id = str(r.le_ident).strip().lstrip("0") or "0"
                                _text(surf, le_id, sz, NUM_COL, bold=True,
                                      cx=int(mid_le[0] + dx * 0.08),
                                      cy=int(mid_le[1] + dy * 0.08))
                            if _fwd_dist_nm(r.he_lat, r.he_lon) > 0:
                                he_id = str(r.he_ident).strip().lstrip("0") or "0"
                                _text(surf, he_id, sz, NUM_COL, bold=True,
                                      cx=int(mid_he[0] - dx * 0.08),
                                      cy=int(mid_he[1] - dy * 0.08))

                    # Airport environment box: a runway-axis-aligned
                    # rectangle 2.5× the runway's width, extending
                    # past each threshold so it reads as a frame.
                    # Clipped against the forward half-plane like the
                    # runway polygon for consistent fly-over behaviour.
                    #
                    # Suppressed close-in (within 2 nm AND under 500 ft
                    # above the threshold elev) — at that range the box
                    # is mostly off-screen anyway and its corners
                    # foreshorten enough to look broken; pilot is on
                    # final / rolling out and the runway polygon itself
                    # is the primary cue.
                    field_elev = min(r.le_elev_ft, r.he_elev_ft)
                    box_close_in = (d_nm < 2.0
                                    and (alt_ft - field_elev) < 500.0)
                    BOX_W_SCALE   = 2.5
                    BOX_LEN_PAD   = 0.10
                    axis_lat_unit = ax_lat / axis_len_nm
                    axis_lon_unit = ax_lon / axis_len_nm
                    pad_nm = axis_len_nm * BOX_LEN_PAD
                    ext_lat = axis_lat_unit * pad_nm
                    ext_lon = axis_lon_unit * pad_nm
                    pl = perp_lat * BOX_W_SCALE
                    po = perp_lon * BOX_W_SCALE
                    box_quad = _clip_polygon_forward(
                        [(r.le_lat - ext_lat + pl, r.le_lon - ext_lon + po, r.le_elev_ft),
                         (r.he_lat + ext_lat + pl, r.he_lon + ext_lon + po, r.he_elev_ft),
                         (r.he_lat + ext_lat - pl, r.he_lon + ext_lon - po, r.he_elev_ft),
                         (r.le_lat - ext_lat - pl, r.le_lon - ext_lon - po, r.le_elev_ft)],
                        lat, lon, hdg_rad,
                        nm_per_deg_lat, nm_per_deg_lon)
                    if len(box_quad) >= 3 and not box_close_in:
                        box_pts = [_proj(p[0], p[1], p[2]) for p in box_quad]
                        if all(pt is not None for pt in box_pts):
                            bxs = [pt[0] for pt in box_pts]
                            bys = [pt[1] for pt in box_pts]
                            if (max(bxs) >= ax and min(bxs) <= ax + aw and
                                    max(bys) >= ay_r and min(bys) <= ay_r + ah):
                                pygame.draw.lines(surf, BOX_COL, True,
                                                  box_pts, 2)

            # ── Extended centerlines from each threshold ──────────────────
            # Only show centerlines if within a somewhat larger range —
            # navigation aid for approach planning.  When a direct-to
            # waypoint is active, render only the selected airport's
            # centerlines.
            if (show_cline and d_nm <= _CENTERLINE_RANGE_NM and
                    (not sel_ident or str(r.airport) == sel_ident)):
                _draw_extended_centerline(
                    surf, ai_rect, r, lat, lon, alt_ft,
                    hdg_deg, pitch_deg, roll_deg, cx, cy, px_per_deg,
                    CLINE, nm_per_deg_lat, nm_per_deg_lon,
                )
    finally:
        surf.set_clip(old_clip)


def _draw_extended_centerline(surf, ai_rect, r, lat, lon, alt_ft,
                              hdg_deg, pitch_deg, roll_deg,
                              cx, cy, px_per_deg, col,
                              nm_per_deg_lat, nm_per_deg_lon):
    """Dashed line extending OUTWARD from each threshold along the reciprocal
    of the runway axis, rising at the standard glideslope so the dashes
    represent the visual approach path rather than ground projection.

    Caller (draw_runway_symbols) holds the AI clip, so we don't set/restore
    it here.  nm_per_deg_lon is passed through to _project_latlon so the
    inner cos() isn't recomputed on every dash endpoint."""
    # Axis unit vector from LE → HE in degrees
    ax_dlat = r.he_lat - r.le_lat
    ax_dlon = r.he_lon - r.le_lon
    axis_len_nm = math.hypot(ax_dlat * nm_per_deg_lat,
                             ax_dlon * nm_per_deg_lon)
    if axis_len_nm < 0.01:
        return
    # Unit vector components in degrees-per-nm
    u_dlat = ax_dlat / axis_len_nm
    u_dlon = ax_dlon / axis_len_nm

    dash_nm = _CENTERLINE_DASH_NM
    gap_nm  = _CENTERLINE_DASH_NM * 0.6
    step_nm = dash_nm + gap_nm
    n_steps = int(_CENTERLINE_EXTEND_NM / step_nm)

    # Vertical rise per nm along the approach path (ft).
    rise_ft_per_nm = math.tan(math.radians(_CENTERLINE_GLIDE_DEG)) * 6076.0

    # Angular cutoff: skip segments whose endpoint is more than 60° off the
    # nose — past that the flat-earth bearing math wraps and dashes behind
    # the aircraft would streak horizontally across the AI.
    _FOV = 60.0

    # For each threshold, extend OUTWARD (opposite of the axis toward
    # the other end).  Threshold elevations are surveyed values used
    # directly so the near end of the centerline starts on the actual
    # ground, not pinned to the aircraft altitude.
    for thresh_lat, thresh_lon, thresh_elev, sign in (
        (r.le_lat, r.le_lon, r.le_elev_ft, -1),
        (r.he_lat, r.he_lon, r.he_elev_ft, +1),
    ):
        s_dlat = sign * u_dlat
        s_dlon = sign * u_dlon
        for i in range(n_steps):
            start = step_nm * i
            end   = start + dash_nm
            ps = _project_latlon(thresh_lat + s_dlat * start,
                                 thresh_lon + s_dlon * start,
                                 lat, lon, alt_ft,
                                 thresh_elev + start * rise_ft_per_nm,
                                 hdg_deg, pitch_deg, roll_deg,
                                 cx, cy, px_per_deg, max_fov_deg=_FOV,
                                 ground_only=False,
                                 nm_per_deg_lon=nm_per_deg_lon)
            if ps is None:
                continue
            pe = _project_latlon(thresh_lat + s_dlat * end,
                                 thresh_lon + s_dlon * end,
                                 lat, lon, alt_ft,
                                 thresh_elev + end * rise_ft_per_nm,
                                 hdg_deg, pitch_deg, roll_deg,
                                 cx, cy, px_per_deg, max_fov_deg=_FOV,
                                 ground_only=False,
                                 nm_per_deg_lon=nm_per_deg_lon)
            if pe is None:
                continue
            pygame.draw.line(surf, col, ps, pe, 2)


# ── Main render function ──────────────────────────────────────────────────────
# ── Full-screen MFD (larger HDMI screens) ─────────────────────────────────────
_P4_MFD_BW  = 120
_P4_MFD_BH  = 48
_P4_MFD_PAD = 8


# Bottom data strip — 8 configurable readout slots (mirrors pi_zero).
_MFD_STRIP_H   = 56
_MFD_STRIP_SLOT_COUNT = 8
# Each entry: (id, caption, needs_D2, description).  Nav convention: a bare
# label is to the ACTIVE WAYPOINT; the "…D" variant is to the FINAL
# DESTINATION (whole remaining route).
_MFD_STRIP_AVAILABLE = (
    ("gs",   "GS",   False, "Ground speed (GPS), knots"),
    ("as",   "AS",   False, "Indicated airspeed, knots"),
    ("tas",  "TAS",  False, "True airspeed, knots"),
    ("trk",  "TRK",  False, "GPS ground track, degrees"),
    ("hdg",  "HDG",  False, "Magnetic heading, degrees"),
    ("alt",  "ALT",  False, "Pressure altitude, feet"),
    ("agl",  "AGL",  False, "Height above terrain, feet"),
    ("vs",   "VS",   False, "Vertical speed, feet per minute"),
    ("oat",  "OAT",  False, "Outside air temperature (not yet wired)"),
    ("da",   "DA",   False, "Density altitude (not yet wired)"),
    ("pa",   "PA",   False, "Pressure altitude (not yet wired)"),
    ("wind", "WIND", False, "Wind direction / speed"),
    ("time", "UTC",  False, "Current Zulu (UTC) clock time"),
    ("baro", "BARO", False, "Altimeter setting"),
    ("sat",  "SAT",  False, "GPS satellites in use"),
    ("wpt",  "WPT",  True,  "Active waypoint identifier"),
    ("btw",  "BTW",  True,  "Bearing to the active waypoint, degrees"),
    ("dtk",  "DTK",  True,  "Desired track of the active leg, degrees"),
    ("xte",  "XTE",  True,  "Cross-track error from the active leg, nm"),
    ("dist", "DIST", True,  "Distance to the ACTIVE WAYPOINT, nm"),
    ("distd", "DISTD", True, "Distance to the FINAL DESTINATION (whole route), nm"),
    ("ete",  "ETE",  True,  "Time enroute to the ACTIVE WAYPOINT"),
    ("eted", "ETED", True,  "Time enroute to the FINAL DESTINATION"),
    ("eta",  "ETA",  True,  "Arrival clock at the ACTIVE WAYPOINT"),
    ("etad", "ETAD", True,  "Arrival clock at the FINAL DESTINATION"),
)
_MFD_STRIP_KIND_IDS = tuple(k[0] for k in _MFD_STRIP_AVAILABLE)
_MFD_STRIP_CAPTIONS = {k[0]: k[1] for k in _MFD_STRIP_AVAILABLE}
_MFD_STRIP_NEEDS_D2 = {k[0]: k[2] for k in _MFD_STRIP_AVAILABLE}
_MFD_STRIP_DESC     = {k[0]: k[3] for k in _MFD_STRIP_AVAILABLE}
_MFD_STRIP_DEFAULT  = ["gs", "trk", "alt", "wpt", "btw", "dist", "ete", "etad"]
# One-time migration of the pre-convention IDs to the new scheme (schema v2):
# old DIST/ETE were waypoint and DISW/ETEW were destination, but the ETA pair
# was reversed (ETA=destination, ETW=waypoint).  Remap so bare=waypoint.
_MFD_STRIP_MIGRATE  = {"disw": "distd", "etew": "eted",
                       "eta": "etad", "etw": "eta"}
_D2_DIM = (110, 90, 110)
_P4_MFD_ZOOM = 64          # zoom button square

# PFD top readout ribbon (band above the AI) — its own 5 slots, same kinds.
_PFD_TOP_SLOT_COUNT = 5
_PFD_TOP_DEFAULT    = ["agl", "tas", "oat", "wind", "etad"]


def _strip_kinds_for(key, default, count):
    """Validated/padded kind list for a strip target (MFD strip or PFD top)."""
    cur = list(disp["ds"].get(key, default))
    out = []
    for i in range(count):
        k = cur[i] if i < len(cur) else default[i % len(default)]
        if k not in _MFD_STRIP_KIND_IDS:
            k = default[i % len(default)]
        out.append(k)
    return out


def _pfd_top_kinds():
    return _strip_kinds_for("pfd_top_kinds", _PFD_TOP_DEFAULT,
                            _PFD_TOP_SLOT_COUNT)


def _pfd_top_band():
    """(x0, x1, y0, y1) of the tappable ribbon band between the bug boxes."""
    return (SPD_W, ALT_X, 0, TAPE_TOP)


def draw_pfd_top_strip(surf):
    """Compact readout ribbon in the band above the AI (between the GS and ALT
    bug boxes).  Drawn BEFORE the status badges / alert banners so any
    annunciation paints over it — alerts always win.  Tap it to choose fields
    (its own pfd_top_kinds, separate from the MFD strip)."""
    kinds = _pfd_top_kinds()
    nav = disp.get("nav") or {}
    d2 = nav if nav.get("ident") else None
    yaw = float(disp.get("yaw", 0.0))
    ctx = _mfd_strip_ctx(float(disp.get("lat", DEMO_LAT)),
                         float(disp.get("lon", DEMO_LON)),
                         float(disp.get("alt", 0.0)), yaw,
                         float(disp.get("track", yaw)),
                         float(disp.get("speed", 0.0)), d2)
    bx0, bx1, _y0, y1 = _pfd_top_band()
    # Dark backing so the readouts stay legible over sky / ground / terrain,
    # and so the top reads as one bar continuous with the GS/ALT bug boxes.
    pygame.draw.rect(surf, (6, 14, 24), (bx0, 0, bx1 - bx0, y1))
    pygame.draw.line(surf, (55, 75, 105), (bx0, y1 - 1), (bx1, y1 - 1), 1)
    x0 = bx0 + 6
    x1 = bx1 - 6
    n = max(1, len(kinds))
    slot_w = (x1 - x0) / n
    cy = y1 // 2
    cap_f = _get_font(11, bold=True)
    val_f = _get_font(15, bold=True)
    for i, kind in enumerate(kinds):
        cap, val, col = _mfd_strip_format(kind, ctx)
        val = str(val)
        cw = cap_f.size(cap)[0]
        vw = val_f.size(val)[0]
        sx = int(x0 + slot_w * (i + 0.5) - (cw + 4 + vw) / 2)
        _text(surf, cap, 11, (150, 165, 190), bold=True, x=sx, cy=cy)
        _text(surf, val, 15, col, bold=True, x=sx + cw + 4, cy=cy)


def _fpl_total_remaining_nm(lat, lon):
    """Distance from the aircraft through the active + remaining legs."""
    if not _fpl_is_active():
        return 0.0
    wps = disp["fpl"]["waypoints"]
    idx = disp["fpl"]["active_idx"]
    total, _ = _nav_geo_dist_brg(lat, lon, wps[idx]["lat"], wps[idx]["lon"])
    for i in range(idx, len(wps) - 1):
        leg, _ = _nav_geo_dist_brg(wps[i]["lat"], wps[i]["lon"],
                                   wps[i + 1]["lat"], wps[i + 1]["lon"])
        total += leg
    return total


def _mfd_strip_rect():
    return (0, DISPLAY_H - _MFD_STRIP_H, DISPLAY_W, _MFD_STRIP_H)


def _mfd_strip_hit(x, y):
    sx, sy, sw, sh = _mfd_strip_rect()
    return sx <= x <= sx + sw and sy <= y <= sy + sh


def _mfd_strip_kinds():
    ds = disp["ds"]
    # One-time remap of the pre-convention IDs (see _MFD_STRIP_MIGRATE).
    if ds.get("mfd_strip_ver", 1) < 2:
        ds["mfd_strip_kinds"] = [
            _MFD_STRIP_MIGRATE.get(k, k)
            for k in ds.get("mfd_strip_kinds", _MFD_STRIP_DEFAULT)]
        ds["mfd_strip_ver"] = 2
        _settings.mark_dirty()
    cur = list(ds.get("mfd_strip_kinds", _MFD_STRIP_DEFAULT))
    out = []
    for i in range(_MFD_STRIP_SLOT_COUNT):
        k = cur[i] if i < len(cur) else _MFD_STRIP_DEFAULT[i]
        if k not in _MFD_STRIP_KIND_IDS:
            k = _MFD_STRIP_DEFAULT[i]
        out.append(k)
    return out


def _mfd_strip_ete_str(gs_kt, dist_nm):
    if gs_kt < 3.0 or dist_nm <= 0.0:
        return "--:--"
    hours = dist_nm / gs_kt
    if hours < 1.0:
        mm, ss = divmod(int(round(hours * 3600)), 60)
        return f"{mm}:{ss:02d}"
    if hours < 99.0:
        h_, rem = divmod(int(round(hours * 3600)), 3600)
        mm, _ = divmod(rem, 60)
        return f"{h_}:{mm:02d}"
    return "--:--"


def _mfd_strip_eta_str(gs_kt, dist_nm, local=False, tz_lat=None, tz_lon=None):
    """Arrival clock time.  Zulu (``18:34Z``) by default; local (``11:34``, no
    Z) when `local` and the arrival-point position is known — using that point's
    timezone + DST at the arrival instant (see shared/localtime.py)."""
    if gs_kt < 3.0 or dist_nm <= 0.0 or dist_nm / max(1e-6, gs_kt) >= 99.0:
        return "--:--"
    eta_t = time.time() + int(round(dist_nm / gs_kt * 3600))
    if local:
        from datetime import datetime, timezone
        off = _localtime.offset_hours(
            tz_lat, tz_lon, datetime.fromtimestamp(eta_t, timezone.utc))
        if off is not None:
            return time.strftime("%H:%M", time.gmtime(eta_t + int(off * 3600)))
    return time.strftime("%H:%MZ", time.gmtime(eta_t))


def _fpl_dest_latlon():
    """(lat, lon) of the active flight plan's final waypoint, or None."""
    if not _fpl_is_active():
        return None
    wps = disp["fpl"]["waypoints"]
    return (wps[-1]["lat"], wps[-1]["lon"]) if wps else None


def _mfd_strip_ctx(lat, lon, alt, hdg, track, gs_kt, d2):
    ctx = {
        "lat": lat, "lon": lon, "alt": alt, "hdg": hdg, "track": track,
        "gs_kt": gs_kt, "ias": float(disp.get("ias_kt", 0.0)),
        "tas": float(disp.get("tas_kt", 0.0)),
        "vs": float(disp.get("vspeed", 0.0)),
        "baro_hpa": float(disp.get("baro_hpa", BARO_DEFAULT_HPA)),
        "sats": int(disp.get("sats", 0)), "d2": d2,
    }
    if _has_terrain:
        try:
            ctx["agl"] = max(0.0, alt - get_elevation_ft(SRTM_DIR, lat, lon))
        except Exception:
            ctx["agl"] = None
    if d2 is not None:
        dist_nm, brg = _nav_geo_dist_brg(lat, lon, d2["lat"], d2["lon"])
        ctx["dist_nm"] = dist_nm
        ctx["brg"] = brg
        act_lat = float(d2.get("act_lat", lat))
        act_lon = float(d2.get("act_lon", lon))
        _, dtk = _nav_geo_dist_brg(act_lat, act_lon, d2["lat"], d2["lon"])
        ctx["dtk"] = dtk
        ctx["xte_nm"] = _nav_xtk_nm(act_lat, act_lon, d2["lat"], d2["lon"],
                                    lat, lon)
    return ctx


def _strip_wind(ctx):
    """(from_deg, speed_kt) for the WIND strip field, or None when there's no
    usable wind (on the ground / no IAS / no GPS track).

    Source ladder: prefer a firmware- or sim-provided solution (disp
    wind_dir/wind_kt — best when an OAT sensor feeds density-corrected TAS);
    otherwise compute it on the display from IAS + pressure-altitude ISA-TAS
    and the GPS track/heading triangle, so it still reads without an OAT
    sensor (TAS just assumes standard temperature for the current altitude)."""
    wd = disp.get("wind_dir", 0.0) or 0.0
    wk = disp.get("wind_kt", 0.0) or 0.0
    if wk > 0.0:
        return (wd % 360.0, wk)
    # Display-side fallback — needs live IAS and a valid GPS track (in flight).
    ias = ctx.get("ias", 0.0)
    gs  = ctx.get("gs_kt", 0.0)
    if ias <= 0.0 or gs < HDG_TRK_MIN_KT:
        return None
    # ISA true airspeed from IAS + pressure altitude (no OAT needed): the
    # standard-atmosphere density ratio σ = (1 − 6.8756e-6·h_ft)^4.2559;
    # TAS = IAS / √σ.
    h = max(0.0, ctx.get("alt", 0.0))
    sigma = (1.0 - 6.8756e-6 * h) ** 4.2559
    tas = ias / math.sqrt(sigma) if sigma > 0.0 else ias
    hd = math.radians(ctx.get("hdg", 0.0))
    tk = math.radians(ctx.get("track", 0.0))
    wn = gs * math.cos(tk) - tas * math.cos(hd)
    we = gs * math.sin(tk) - tas * math.sin(hd)
    spd = math.hypot(wn, we)
    if spd < 1.0:                       # sub-knot → call it calm, show nothing
        return None
    frm = (math.degrees(math.atan2(we, wn)) + 180.0) % 360.0
    return (frm, spd)


def _mfd_strip_format(kind, ctx):
    d2 = ctx.get("d2")
    gs_kt = ctx["gs_kt"]
    if kind == "gs":
        return ("GS", f"{int(round(gs_kt)):3d}", WHITE)
    if kind == "as":
        v = ctx["ias"]
        return ("AS", f"{int(round(v)):3d}" if v > 0 else "---", WHITE)
    if kind == "tas":
        v = ctx["tas"]
        return ("TAS", f"{int(round(v)):3d}" if v > 0 else "---", WHITE)
    if kind == "trk":
        s = (f"{int(round(ctx['track'])) % 360:03d}°"
             if gs_kt >= HDG_TRK_MIN_KT else "---°")
        return ("TRK", s, WHITE)
    if kind == "hdg":
        return ("HDG", f"{int(round(ctx['hdg'])) % 360:03d}°", WHITE)
    if kind == "alt":
        return ("ALT", f"{int(round(ctx['alt'] / 20.0) * 20):5d}", WHITE)
    if kind == "agl":
        v = ctx.get("agl")
        if v is None:
            return ("AGL", "----", (140, 140, 140))
        return ("AGL", f"{int(round(v / 10.0) * 10):5d}", WHITE)
    if kind == "vs":
        return ("VS", f"{int(round(ctx['vs'])):+5d}", WHITE)
    # OAT / DA / PA — placeholders (parity with pi_zero; no sensor wiring yet).
    # Shown dim so they read as "available but no data".
    if kind == "oat":
        return ("OAT", "--", (140, 140, 140))
    if kind == "da":
        return ("DA", "----", (140, 140, 140))
    if kind == "pa":
        return ("PA", "----", (140, 140, 140))
    if kind == "wind":
        w = _strip_wind(ctx)
        if w is None:
            return ("WIND", "---/--", (140, 140, 140))
        return ("WIND", f"{int(round(w[0])) % 360:03d}/{int(round(w[1])):02d}",
                WHITE)
    if kind == "time":
        return ("UTC", time.strftime("%H:%MZ", time.gmtime()), WHITE)
    if kind == "baro":
        hpa = ctx["baro_hpa"]
        if disp["ds"].get("baro_unit", "inhg") == "inhg":
            return ("BARO", f"{hpa * 0.02953:.2f}", WHITE)
        return ("BARO", f"{int(round(hpa)):4d}", WHITE)
    if kind == "sat":
        return ("SAT", f"{ctx['sats']:2d}", WHITE)

    caption = _MFD_STRIP_CAPTIONS.get(kind, "?")
    if d2 is None:
        return (caption, "--", _D2_DIM)
    if kind == "wpt":
        return (caption, d2.get("ident", "----"), MAGENTA)
    if kind == "btw":
        return (caption, f"{int(round(ctx['brg'])) % 360:03d}°", MAGENTA)
    if kind == "dtk":
        return (caption, f"{int(round(ctx['dtk'])) % 360:03d}°", MAGENTA)
    # Nav convention: bare = to the ACTIVE WAYPOINT (ctx["dist_nm"]); the
    # "…D" variant = to the FINAL DESTINATION (whole remaining route).
    def _fmt_dist(d_nm):
        return f"{int(round(d_nm)):d}" if d_nm >= 1000 else f"{d_nm:.1f}"

    def _route_remaining():
        return (_fpl_total_remaining_nm(ctx["lat"], ctx["lon"])
                if _fpl_is_active() else ctx["dist_nm"])

    local = bool(disp["ds"].get("eta_local", True))
    if kind == "dist":
        return (caption, _fmt_dist(ctx["dist_nm"]), MAGENTA)
    if kind == "distd":
        return (caption, _fmt_dist(_route_remaining()), MAGENTA)
    if kind == "xte":
        return (caption, f"{ctx['xte_nm']:+.1f}", MAGENTA)
    if kind == "ete":
        return (caption, _mfd_strip_ete_str(gs_kt, ctx["dist_nm"]), MAGENTA)
    if kind == "eted":
        return (caption, _mfd_strip_ete_str(gs_kt, _route_remaining()), MAGENTA)
    if kind == "eta":         # arrival at the active waypoint
        return (caption, _mfd_strip_eta_str(
            gs_kt, ctx["dist_nm"], local=local,
            tz_lat=d2.get("lat"), tz_lon=d2.get("lon")), MAGENTA)
    if kind == "etad":        # arrival at the final destination
        dll = _fpl_dest_latlon() or (d2.get("lat"), d2.get("lon"))
        return (caption, _mfd_strip_eta_str(
            gs_kt, _route_remaining(), local=local,
            tz_lat=dll[0], tz_lon=dll[1]), MAGENTA)
    return (caption, "--", _D2_DIM)


# ── MFD pan ───────────────────────────────────────────────────────────────────
def _mfd_effective_center():
    pan = disp.get("mfd_pan", {})
    if pan.get("lat") is not None and pan.get("lon") is not None:
        return float(pan["lat"]), float(pan["lon"])
    return (disp.get("lat", DEMO_LAT), disp.get("lon", DEMO_LON))


def _mfd_is_panned():
    pan = disp.get("mfd_pan", {})
    return pan.get("lat") is not None and pan.get("lon") is not None


def _mfd_clear_pan():
    disp["mfd_pan"]["lat"] = None
    disp["mfd_pan"]["lon"] = None


# Pan-drag state + last-rendered map params (so tap/drag projection matches
# what's on screen, including AUTO-resolved range / forced north-up).
_mfd_drag = None
_wx_drag = None             # drag-to-scroll state for the TAF / advisory readouts
_mfd_last_range = 10
_mfd_last_orient = "trk"
_mfd_last_track = None
_inset_render_err_logged = False   # log the first PFD-inset render exception once

# Approach lateral AP — PID on cross-track so it captures fast and holds the
# centreline (P = capture, D = cross-track rate for damping/anticipation,
# I = removes steady-state offset).  All tunable.
_appr_xtk_int      = 0.0    # state: accumulated cross-track (nm·s)
_APPR_XTK_KP       = 180.0  # deg of intercept per nm of cross-track
_APPR_XTK_KD       = 500.0  # deg per (nm/s) of cross-track RATE — the rate term
_APPR_XTK_KI       = 5.0    # deg per (nm·s) of integral
_APPR_XTK_INT_LIM  = 2.0    # anti-windup clamp on the integral (nm·s)
_APPR_XTK_I_AUTH   = 10.0   # max integral authority (deg)
_APPR_MAX_INTERCEPT = 45.0  # max commanded intercept angle (deg)
_MFD_DRAG_THRESHOLD = 8

_mfd_apt_font = None


def _mfd_get_apt_font():
    global _mfd_apt_font
    if _mfd_apt_font is None:
        # 36 pt on the 7"; scaled down on the physically larger 10" panel
        # (both are 1024×600) via MFD_FONT_SCALE so airport names aren't huge.
        pt = max(12, int(round(36 * globals().get("MFD_FONT_SCALE", 1.0))))
        _mfd_apt_font = pygame.font.SysFont("DejaVu Sans", pt, bold=True)
    return _mfd_apt_font


# ── METAR readout popup (tap a station) ───────────────────────────────────────
def _wrap_text(text, font, max_w):
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = w if not cur else cur + " " + w
        if font.size(trial)[0] <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _fisb_taf_for(icao):
    """Raw FIS-B TAF text for a station, if one's been heard recently."""
    store = getattr(_adsb_client, "fisb", None) if _adsb_client else None
    return store.taf_for(icao) if (store is not None and icao) else None


def _draw_wx_popup(surf):
    m = disp.get("wx_popup")
    if not m:
        return
    pw = min(620, DISPLAY_W - 40)
    icao = m.get("icao", "----")
    cat = m.get("fltcat", "")

    wd, ws, wg = m.get("wdir"), m.get("wspd"), m.get("wgst")
    if ws is None:
        wind = "Wind —"
    elif ws == 0:
        wind = "Wind calm"
    else:
        d = "VRB" if wd in (None, "VRB") else f"{int(wd):03d}°"
        wind = f"Wind {d} {int(ws)} kt" + (f" G{int(wg)}" if wg else "")
    vis = m.get("visib_mi")
    ceil = m.get("ceiling_ft")
    alt = m.get("altim_hpa")
    t, dp = m.get("temp_c"), m.get("dewp_c")
    age = m.get("age_min")
    rows = [r for r in [
        wind,
        f"Vis {vis:g} sm" if vis is not None else "Vis —",
        f"Ceiling {int(ceil)} ft" if ceil is not None else "No ceiling",
        (f"Alt {alt * 0.0295300:.2f} inHg / {int(round(alt))} hPa"
         if alt else "Alt —"),
        (f"Temp {t:.0f}° / Dew {dp:.0f}°C"
         if t is not None and dp is not None else ""),
        f"Observed {int(age)} min ago" if age is not None else "",
    ] if r]

    # Wrap the raw METAR with the SAME font _text renders with so it fits;
    # size the panel to the content.  (TAF lives on its own screen now.)
    f16 = _get_font(16)
    raw = m.get("raw", "")
    raw_lines = _wrap_text(raw, f16, pw - 36)[:3] if raw else []

    ph = 60 + len(rows) * 28
    if raw_lines:
        ph += 26 + len(raw_lines) * 20
    ph += 30
    ph = min(ph, DISPLAY_H - 24)
    px = (DISPLAY_W - pw) // 2
    py = (DISPLAY_H - ph) // 2

    dim = pygame.Surface((DISPLAY_W, DISPLAY_H), pygame.SRCALPHA)
    dim.fill((0, 0, 0, 150))
    surf.blit(dim, (0, 0))
    pygame.draw.rect(surf, (10, 18, 34), (px, py, pw, ph), border_radius=10)
    pygame.draw.rect(surf, (80, 110, 150), (px, py, pw, ph), width=2,
                     border_radius=10)
    _text(surf, icao, 30, (235, 235, 235), bold=True, x=px + 18, y=py + 12)
    _text(surf, cat or "—", 26, _wx.cat_color(cat), bold=True,
          x=px + pw - 120, y=py + 15)
    # Source: FIS-B (radio) vs internet — so the pilot knows where it came from.
    src = (m.get("src") or "").upper()
    if src in ("RDR", "INET"):
        s_lbl = "FIS-B" if src == "RDR" else "INET"
        s_col = (90, 210, 230) if src == "RDR" else (150, 180, 220)
        _text(surf, s_lbl, 18, s_col, bold=True, x=px + pw - 120, y=py + 48)

    yy = py + 60
    for rr in rows:
        _text(surf, rr, 22, (210, 220, 230), x=px + 18, y=yy)
        yy += 28
    if raw_lines:
        yy += 6
        _text(surf, "METAR", 15, (110, 150, 185), bold=True, x=px + 18, y=yy)
        yy += 20
        for ln in raw_lines:
            _text(surf, ln, 16, (150, 200, 240), x=px + 18, y=yy)
            yy += 20
    _text(surf, "tap to close", 16, (140, 150, 160),
          cx=DISPLAY_W // 2, cy=py + ph - 14)


# ── Weather product menu + TAF / advisory readouts ──────────────────────────────
_WX_MENU_KINDS = ("METAR", "TAF", "AIRMET", "SIGMET", "NOTAM")
# Which advisory kind a graphical hazard belongs to (for tap-a-shape → text).
_HAZARD_KIND = {"Convective": "SIGMET", "Ash": "SIGMET"}   # else AIRMET


def _fisb_store():
    return getattr(_adsb_client, "fisb", None) if _adsb_client else None


def _point_in_poly(x, y, pts):
    """Ray-cast point-in-polygon test on screen-space vertices."""
    inside = False
    n = len(pts)
    j = n - 1
    for i in range(n):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if (yi > y) != (yj > y) and \
                x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-9) + xi:
            inside = not inside
        j = i
    return inside


def _poly_area(pts):
    """Shoelace area of a screen-space polygon (for picking the smallest one
    under a tap when hazard areas overlap)."""
    a = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        a += x1 * y2 - x2 * y1
    return abs(a) * 0.5


def _mfd_find_graphic(tap_x, tap_y):
    """The graphical hazard area under the tap, or None.  When areas overlap
    (e.g. a convective cell nested in a turbulence region) the *smallest*
    containing polygon wins, so the specific hazard is reachable.  MET only."""
    if not disp["ds"].get("map_show_metar"):
        return None
    gfx = disp.get("weather", {}).get("graphics") or []
    if not gfx:
        return None
    cen_lat, cen_lon = _mfd_effective_center()
    project, _ = _map_mod.make_projector(
        (0, 0, DISPLAY_W, DISPLAY_H), cen_lat, cen_lon, _mfd_last_orient,
        _mfd_last_range, disp.get("yaw", 0.0), _mfd_last_track)
    best, best_area = None, None
    for g in gfx:
        verts = g.get("vertices") or []
        if len(verts) < 3:
            continue
        pts = [project(la, lo) for la, lo in verts]
        if _point_in_poly(tap_x, tap_y, pts):
            area = _poly_area(pts)
            if best is None or area < best_area:
                best, best_area = g, area
    return best


def _wx_open_graphic_text(g):
    """Open the bulletin for a tapped hazard polygon: the area's *own* paired
    text when the graphic carries it, else fall back to the matching AIRMET/
    SIGMET list."""
    kind = _HAZARD_KIND.get(g.get("hazard"), "AIRMET")
    disp["wx_scroll"] = 0
    title = f"{g.get('hazard', '')} {kind}".strip()
    if g.get("text"):
        v = _fisb.advisory_valid(g["text"])
        bulletin = f"[valid {v}]  {g['text']}" if v else g["text"]
        disp["wx_text"] = {"title": title, "bulletins": [bulletin]}
        return
    store = _fisb_store()
    disp["wx_text"] = {"title": title,
                       "bulletins": store.advisories(kind) if store else []}


def _notam_locate(text):
    """Best-effort (lat, lon) for a NOTAM from an airport id in its text
    (e.g. '!SEZ …' → KSEZ).  Only used to rank by distance — never to hide."""
    for raw in text.replace("!", " ").split():
        tok = raw.strip(".,:;/").upper()
        if tok.isalpha() and 3 <= len(tok) <= 4:
            for cand in ((tok, "K" + tok) if len(tok) == 3 else (tok,)):
                r = _nav_lookup_ident(cand)
                if r:
                    return (r[1], r[2])
    return None


def _active_route_pts():
    """Active route as a [(lat, lon), …] polyline (FPL remaining, else the D2
    leg), or None when nothing's active — for the on-route flag."""
    fpl = _fpl_render_remaining() or []
    if len(fpl) >= 2:
        return [(la, lo) for (la, lo, _i) in fpl]
    nav = disp.get("nav") or {}
    if nav.get("ident") and nav.get("lat") is not None:
        wpt = (float(nav["lat"]), float(nav["lon"]))
        al, ao = nav.get("act_lat"), nav.get("act_lon")
        if al and ao:
            return [(float(al), float(ao)), wpt]
        if disp.get("lat") is not None:
            return [(float(disp["lat"]), float(disp["lon"])), wpt]
    return None


def _advisory_list(kind, ref_lat=None, ref_lon=None):
    """Advisory bulletins for ``kind``, ranked nearest-first with an on-route
    tag.  Merges the graphical-paired advisories (located by polygon) with the
    text-product bulletins (NOTAMs located by airport id; text-only AIRMET/
    SIGMET un-locatable → listed last).  Nothing is hidden."""
    store = _fisb_store()
    if store is None:
        return []
    items, seen = [], set()
    for g in store.graphics():
        if _HAZARD_KIND.get(g.get("hazard"), "AIRMET") != kind or not g.get("text"):
            continue
        seen.add(g["text"].strip())
        items.append({"text": g["text"], "verts": g.get("vertices")})
    for t in store.advisories(kind):
        if t.strip() in seen:
            continue
        item = {"text": t}
        if kind == "NOTAM":
            loc = _notam_locate(t)
            if loc:
                item["point"] = loc
        items.append(item)
    # Rank relative to the TAPPED station (the picker is about that field), not
    # ownship; fall back to the aircraft when there's no station context.
    if ref_lat is None or ref_lon is None:
        ref_lat, ref_lon = disp.get("lat"), disp.get("lon")
    ranked = _fisb.rank_advisories(items, ref_lat, ref_lon,
                                   route_pts=_active_route_pts())
    extra = 0
    if kind == "NOTAM" and len(ranked) > NOTAM_LIST_MAX:
        extra = len(ranked) - NOTAM_LIST_MAX   # ranked nearest-first already, so
        ranked = ranked[:NOTAM_LIST_MAX]       # the cap keeps the closest fields
    out = []
    for e in ranked:
        parts = []
        if e["on_route"]:
            parts.append("ON ROUTE")
        if e["dist"] is not None:
            parts.append(f"{e['dist']:.0f} nm")
        v = _fisb.advisory_valid(e["text"])
        if v:
            parts.append(f"valid {v}")
        out.append(f"[{' · '.join(parts) or 'area n/a'}]  {e['text']}")
    if extra:
        out.append(f"… +{extra} more — showing the nearest {NOTAM_LIST_MAX}")
    return out


def _nearest_taf(lat, lon):
    """(icao, raw, dist_nm, bearing_deg) of the nearest station with a TAF, or
    None.  Geolocates each TAF station via the airport DB (TAFs carry no
    position)."""
    store = _fisb_store()
    if store is None or lat is None or lon is None:
        return None
    best = None
    for icao in store.taf_stations():
        r = _nav_lookup_ident(icao)
        if not r:
            continue
        d, b = _nav_geo_dist_brg(lat, lon, r[1], r[2])
        if best is None or d < best[2]:
            best = (icao, store.taf_for(icao), d, b)
    return best


def _winds_station_for(icao):
    """FD winds station id for an airport ICAO — US fields drop the leading K
    (KSEZ → SEZ)."""
    icao = (icao or "").upper()
    return icao[1:] if len(icao) == 4 and icao.startswith("K") else icao


def _winds_pos(sid, w):
    """(lat, lon) for a winds column: its own coords (internet Open-Meteo grid)
    when present, else the airport DB for a radio FD station id, else None."""
    if w and w.get("lat") is not None and w.get("lon") is not None:
        return (w["lat"], w["lon"])
    r = _nav_lookup_ident("K" + sid) or _nav_lookup_ident(sid)
    return (r[1], r[2]) if r else None


def _nearest_winds(lat, lon):
    """(station_id, dist_nm, bearing_deg) of the nearest winds-aloft station, or
    None.  Internet grid columns geolocate by their own coords; radio FD ids via
    the airport DB."""
    store = _fisb_store()
    if store is None or lat is None or lon is None:
        return None
    best = None
    for sid in store.winds_stations():
        pos = _winds_pos(sid, store.winds_for(sid))
        if pos is None:
            continue
        d, b = _nav_geo_dist_brg(lat, lon, pos[0], pos[1])
        if best is None or d < best[1]:
            best = (sid, d, b)
    return best


# Altitudes the WND-overlay barbs can show (standard FD levels).
_WINDS_ALTS = [3000, 6000, 9000, 12000, 18000]   # capped at 18k (see WINDS_ALTS)
_winds_barbs_cache = []
_winds_barbs_key = None


def _winds_barbs(offset_h=None):
    """Geolocated winds-aloft barbs for the selected altitude, valid at
    ``now + offset_h`` hours.  ``offset_h=None`` uses the WND-page forecast-time
    selector; the PFD inset passes 0 so it always shows *now*, independent of the
    page selector.  Each column carries a forecast series, so the right hour is
    picked here at draw time — the picture rolls forward to now on its own
    between fetches.  Rebuilt only when the altitude, the data, the offset, or
    the target hour changes."""
    global _winds_barbs_cache, _winds_barbs_key
    store = _fisb_store()
    if store is None:
        return []
    if offset_h is None:
        offset_h = int(disp["ds"].get("winds_time_offset_h", 0))
    alt = int(disp["ds"].get("winds_alt_ft", 9000))
    target = time.time() + offset_h * 3600.0
    hour_bucket = int((target + 1800.0) // 3600.0)
    # Winds is internet-only and always shown (not filtered by the source pill).
    key = (alt, store.winds_count, offset_h, hour_bucket)
    if key == _winds_barbs_key:
        return _winds_barbs_cache
    out = []
    for sid in store.winds_stations():
        w = store.winds_for(sid)
        if not w:
            continue
        pos = _winds_pos(sid, w)        # own lat/lon (internet grid) or airport DB
        if pos is None:
            continue
        levels = _wx.winds_levels_at(w, target)
        lvl = next((lv for lv in levels if lv["alt_ft"] == alt), None)
        if lvl is None:
            continue
        out.append({"lat": pos[0], "lon": pos[1], "station": sid,
                    "dir": lvl.get("dir"), "spd": lvl.get("spd"),
                    "temp": lvl.get("temp"), "lv": lvl.get("lv", False)})
    _winds_barbs_key, _winds_barbs_cache = key, out
    return out


def _mfd_cycle_winds_alt():
    """Step the WND overlay to the next altitude that has data (or just the next
    standard level)."""
    cur = int(disp["ds"].get("winds_alt_ft", 9000))
    order = _WINDS_ALTS
    i = order.index(cur) if cur in order else 0
    disp["ds"]["winds_alt_ft"] = order[(i + 1) % len(order)]
    _settings.mark_dirty()
    return disp["ds"]["winds_alt_ft"]


_WINDS_TIME_OFFSETS = [0, 3, 6, 9, 12, 18, 24]   # forecast hours ahead


def _mfd_cycle_winds_time():
    """Step the WND overlay to the next forecast-time offset.  No re-fetch: each
    column already carries the forecast series (out to ~30 h), so the barbs
    retarget to ``now + offset`` locally — changing the time is instant and
    costs no Open-Meteo call."""
    cur = int(disp["ds"].get("winds_time_offset_h", 0))
    order = _WINDS_TIME_OFFSETS
    i = order.index(cur) if cur in order else 0
    disp["ds"]["winds_time_offset_h"] = order[(i + 1) % len(order)]
    _settings.mark_dirty()
    return disp["ds"]["winds_time_offset_h"]


def _winds_zoom():
    """The WND page's own zoom (one of WINDS_ZOOMS_NM)."""
    z = int(disp["ds"].get("winds_zoom_nm", 80))
    return z if z in WINDS_ZOOMS_NM else min(
        WINDS_ZOOMS_NM, key=lambda t: abs(t - z))


def _winds_zoom_step(delta):
    """Step the WND page's own zoom within WINDS_ZOOMS_NM (-1 = in, +1 = out)."""
    try:
        i = WINDS_ZOOMS_NM.index(_winds_zoom())
    except ValueError:
        i = len(WINDS_ZOOMS_NM) // 2
    i = max(0, min(len(WINDS_ZOOMS_NM) - 1, i + delta))
    disp["ds"]["winds_zoom_nm"] = WINDS_ZOOMS_NM[i]
    _settings.mark_dirty()


def _winds_age_str(age_s):
    """Compact age label for the winds cache (None -> '--')."""
    if age_s is None:
        return "--"
    if age_s < 90:
        return "now"
    if age_s < 3600:
        return f"{int(age_s / 60)}m"
    return f"{int(age_s / 3600)}h{int((age_s % 3600) / 60):02d}m"


def _winds_status_text():
    """e.g. 'WINDS 6/6 · 12m' all current, or 'WINDS 4/6 · 2 stale · 7h' when
    some zones have aged out.  Reports how many zones still hold CURRENT data
    (fresh) rather than how many are merely loaded — zones refresh in place and
    never drop, so a loaded-count would read 6/6 forever and hide staleness.
    A trailing '…' shows while it's still working toward a full fresh set."""
    if _winds_client is None:
        return "WINDS --"
    fresh, total, age_s, stale, expired = _winds_client.status()
    enabled = getattr(_winds_client, "enabled", False)
    if fresh == 0 and stale == 0 and expired == 0:
        return f"WINDS 0/{total} loading…" if enabled else f"WINDS 0/{total}"
    txt = f"WINDS {fresh}/{total}"
    if age_s is not None:
        txt += f" · {_winds_age_str(age_s)}"
    flags = []
    if stale:
        flags.append(f"{stale} stale")
    if expired:
        flags.append(f"{expired} expired")
    if flags:
        txt += " · " + " ".join(flags)
    if fresh < total and enabled:
        txt += " …"
    return txt


def _mfd_find_winds(tap_x, tap_y, tap_px=34):
    """Station id of the nearest winds barb under the tap, or None."""
    barbs = _winds_barbs()
    if not barbs:
        return None
    cen_lat, cen_lon = _mfd_effective_center()
    project, _ = _map_mod.make_projector(
        (0, 0, DISPLAY_W, DISPLAY_H), cen_lat, cen_lon, _mfd_last_orient,
        _mfd_last_range, disp.get("yaw", 0.0), _mfd_last_track)
    best_d2, best = (tap_px + 1) ** 2, None
    for b in barbs:
        sx, sy = project(b["lat"], b["lon"])
        dd = (sx - tap_x) ** 2 + (sy - tap_y) ** 2
        if dd < best_d2:
            best_d2, best = dd, b["station"]
    return best


def _wx_menu_items():
    """(kind, label, enabled) for each product, given what's available for the
    menu's station / area."""
    menu = disp.get("wx_menu") or {}
    icao = menu.get("icao") or menu.get("airport") or ""
    store = _fisb_store()
    # TAF: the field's own if it has one, else the nearest reporting station.
    if store and store.taf_for(icao):
        taf_lbl, taf_on = f"TAF  {icao}".rstrip(), True
    else:
        nt = _nearest_taf(menu.get("lat"), menu.get("lon"))
        if nt:
            taf_lbl = f"TAF  {nt[0]} · {nt[2]:.0f} nm {_compass8(nt[3])}".rstrip()
            taf_on = True
        else:
            taf_lbl, taf_on = "TAF", False
    # WINDS: the field's own winds aloft, else the nearest forecast point.
    ws = _winds_station_for(icao)
    if store and store.winds_for(ws):
        winds_lbl, winds_on = f"WINDS  {ws}".rstrip(), True
    else:
        nw = _nearest_winds(menu.get("lat"), menu.get("lon"))
        if nw:
            winds_lbl = f"WINDS  {nw[0]} · {nw[1]:.0f} nm {_compass8(nw[2])}".rstrip()
            winds_on = True
        else:
            winds_lbl, winds_on = "WINDS", False
    items = [("METAR", f"METAR  {icao}".rstrip(), menu.get("metar") is not None),
             ("TAF", taf_lbl, taf_on),
             ("WINDS", winds_lbl, winds_on)]
    for kind in ("AIRMET", "SIGMET", "NOTAM"):
        n = len(store.advisories(kind)) if store else 0
        # NOTAM list is capped to NOTAM_LIST_MAX in the readout, so badge it as
        # "100+" when more exist — the count then matches what the list shows.
        cnt = (f"  ({NOTAM_LIST_MAX}+)" if kind == "NOTAM" and n > NOTAM_LIST_MAX
               else (f"  ({n})" if n else ""))
        items.append((kind, f"{kind}{cnt}", n > 0))
    return items


def _wx_menu_rects():
    items = _wx_menu_items()
    pw = min(380, DISPLAY_W - 40)
    bh, gap = 46, 10
    ph = 52 + len(items) * (bh + gap) + 22
    px = (DISPLAY_W - pw) // 2
    py = (DISPLAY_H - ph) // 2
    rects = []
    by = py + 50
    for it in items:
        rects.append((it, (px + 16, by, pw - 32, bh)))
        by += bh + gap
    return (px, py, pw, ph), rects


def _draw_wx_menu(surf):
    if not disp.get("wx_menu"):
        return
    (px, py, pw, ph), rects = _wx_menu_rects()
    icao = (disp["wx_menu"].get("icao")
            or disp["wx_menu"].get("airport") or "")
    dim = pygame.Surface((DISPLAY_W, DISPLAY_H), pygame.SRCALPHA)
    dim.fill((0, 0, 0, 150))
    surf.blit(dim, (0, 0))
    pygame.draw.rect(surf, (10, 18, 34), (px, py, pw, ph), border_radius=10)
    pygame.draw.rect(surf, (80, 110, 150), (px, py, pw, ph), width=2,
                     border_radius=10)
    _text(surf, f"Weather — {icao}".rstrip(), 22, (210, 220, 230), bold=True,
          cx=DISPLAY_W // 2, cy=py + 26)
    for (kind, label, enabled), (bx, by, bw, bh) in rects:
        if enabled:
            _action_btn(surf, bx, by, bw, bh, label, "ok", r=8)
        else:
            pygame.draw.rect(surf, (28, 34, 46), (bx, by, bw, bh),
                             border_radius=8)
            _text(surf, label, 20, (90, 100, 115), x=bx + 14,
                  y=by + bh // 2 - 12)
    _text(surf, "tap outside to cancel", 15, (140, 150, 160),
          cx=DISPLAY_W // 2, cy=py + ph - 14)


def _wx_menu_hit(x, y):
    menu = disp.get("wx_menu")
    if not menu:
        return
    _, rects = _wx_menu_rects()
    store = _fisb_store()
    icao = menu.get("icao") or menu.get("airport") or ""
    for (kind, _label, enabled), (bx, by, bw, bh) in rects:
        if not (enabled and bx <= x <= bx + bw and by <= y <= by + bh):
            continue
        disp["wx_menu"] = None
        disp["wx_scroll"] = 0
        if kind == "METAR" and menu.get("metar"):
            disp["wx_popup"] = dict(menu["metar"])
        elif kind == "TAF" and store:
            if store.taf_for(icao):
                disp["wx_taf"] = {"icao": icao, "dist": None, "brg": None}
            else:                       # fall back to the nearest TAF station
                nt = _nearest_taf(menu.get("lat"), menu.get("lon"))
                if nt:
                    disp["wx_taf"] = {"icao": nt[0], "dist": nt[2],
                                      "brg": nt[3]}
        elif kind == "WINDS" and store:
            ws = _winds_station_for(icao)
            if store.winds_for(ws):
                disp["wx_winds"] = {"station": ws, "dist": None, "brg": None}
            else:
                nw = _nearest_winds(menu.get("lat"), menu.get("lon"))
                if nw:
                    disp["wx_winds"] = {"station": nw[0], "dist": nw[1],
                                        "brg": nw[2]}
        elif store:
            disp["wx_text"] = {"title": f"{kind} — nearest first",
                               "bulletins": _advisory_list(
                                   kind, menu.get("lat"), menu.get("lon"))}
        return
    disp["wx_menu"] = None       # tapped outside the buttons → cancel


def _draw_wx_scroll_panel(surf, title, items, pw):
    """Modal panel with a scrollable content region.  ``items`` is a list of
    (height, draw_fn(surf, x, y)); content taller than the panel scrolls via
    disp['wx_scroll'] (clamped here, driven by the drag handler).  A tap closes
    (handled in the dispatch)."""
    content_h = sum(h for h, _ in items)
    px = (DISPLAY_W - pw) // 2
    ph = min(48 + content_h + 30, DISPLAY_H - 24)
    py = (DISPLAY_H - ph) // 2
    ctop, cbot = py + 46, py + ph - 28
    vis_h = cbot - ctop
    max_scroll = max(0, content_h - vis_h)
    sc = max(0, min(int(disp.get("wx_scroll", 0)), max_scroll))
    disp["wx_scroll"] = sc

    dim = pygame.Surface((DISPLAY_W, DISPLAY_H), pygame.SRCALPHA)
    dim.fill((0, 0, 0, 150))
    surf.blit(dim, (0, 0))
    pygame.draw.rect(surf, (10, 18, 34), (px, py, pw, ph), border_radius=10)
    pygame.draw.rect(surf, (80, 110, 150), (px, py, pw, ph), width=2,
                     border_radius=10)
    _text(surf, title, 24, (235, 235, 235), bold=True, x=px + 18, y=py + 12)

    old_clip = surf.get_clip()
    surf.set_clip(pygame.Rect(px + 2, ctop, pw - 4, vis_h))
    y = ctop - sc
    for h, fn in items:
        if fn is not None and y + h >= ctop and y <= cbot:
            fn(surf, px + 18, y)
        y += h
    surf.set_clip(old_clip)

    if max_scroll > 0:                       # scrollbar thumb + hint
        thumb_h = max(20, int(vis_h * vis_h / content_h))
        thumb_y = ctop + int((vis_h - thumb_h) * sc / max_scroll)
        pygame.draw.rect(surf, (90, 120, 160),
                         (px + pw - 9, thumb_y, 4, thumb_h), border_radius=2)
        foot = "drag to scroll · tap to close"
    else:
        foot = "tap to close"
    _text(surf, foot, 16, (140, 150, 160), cx=DISPLAY_W // 2, cy=py + ph - 14)


def _gps_local_offset_h():
    """Local UTC offset (hours) at the aircraft from GPS.  Exact (timezone
    boundary + Daylight Saving) when `timezonefinder` is installed, else a
    longitude approximation.  None with no GPS fix.  See shared/localtime.py."""
    if not disp.get("gps_ok"):
        return None
    return _localtime.offset_hours(disp.get("lat"), disp.get("lon"))


def _draw_wx_taf(surf):
    """Full TAF readout: each forecast period broken out with labeled fields,
    like the METAR panel.  Scrolls when the forecast is long."""
    wt = disp.get("wx_taf")
    if not wt:
        return
    icao = wt.get("icao", "")
    near = wt.get("dist")
    brg = wt.get("brg")
    store = _fisb_store()
    raw = store.taf_for(icao) if store else None
    src = store.taf_source(icao) if store else None
    off = _gps_local_offset_h()
    p = _fisb.parse_taf(raw, local_offset_h=off) if raw else None
    pw = min(640, DISPLAY_W - 40)
    near_txt = (f" · nearest {near:.0f} nm {_compass8(brg)}".rstrip()
                if near else "")
    src_txt = f"  [{'FIS-B' if src == 'RDR' else src}]" if src else ""
    if p:
        vz = f"{_fisb._hhz(p['valid_from'])}–{_fisb._hhz(p['valid_to'])}"
        if off is not None:
            tzlab = _localtime.abbrev(disp.get("lat"), disp.get("lon")) or "L"
            sep = " " if tzlab != "L" else ""
            vz += (f"  ·  {_fisb._lhh(p['valid_from'], off)}–"
                   f"{_fisb._lhh(p['valid_to'], off)}{sep}{tzlab}")
        valid_txt = f"   valid {vz}"
    else:
        valid_txt = ""
    title = f"TAF  {icao}{src_txt}{near_txt}{valid_txt}"
    items = []
    if p:
        for g in p["periods"]:
            items.append((26, lambda s, x, y, t=g["label"]:
                          _text(s, t, 18, (120, 200, 230), bold=True, x=x, y=y)))
            rows = [(lbl, v) for lbl, v in (("Wind", g["wind"]),
                    ("Vis", g["vis"]), ("Wx", g["wx"]), ("Sky", g["sky"])) if v]
            for lbl, val in (rows or [("", "—")]):
                def _row(s, x, y, lbl=lbl, val=val):
                    if lbl:
                        _text(s, lbl, 16, (140, 160, 180), x=x + 18, y=y)
                    _text(s, val, 16, (210, 220, 230), x=x + 92, y=y)
                items.append((24, _row))
            items.append((8, None))          # gap between periods
    else:
        items.append((24, lambda s, x, y: _text(s, "No TAF.", 18,
                                                 (200, 215, 230), x=x, y=y)))
    _draw_wx_scroll_panel(surf, title, items, pw)


def _draw_wx_winds(surf):
    """Winds & temps aloft table (altitude / wind / temp) for a station."""
    ww = disp.get("wx_winds")
    if not ww:
        return
    station = ww.get("station", "")
    near, brg = ww.get("dist"), ww.get("brg")
    store = _fisb_store()
    w = store.winds_for(station) if store else None
    near_txt = (f" · nearest {near:.0f} nm {_compass8(brg)}".rstrip()
                if near else "")
    src = (w or {}).get("src")
    # Internet winds are a forecast grid (coordinate id); label them as a grid
    # point + source rather than showing a bare lat,lon as if it were a station.
    head = "grid" if (src == "INET" or "," in str(station)) else station
    src_txt = f"  [{'FIS-B' if src == 'RDR' else src}]" if src else ""
    # Retarget the column to the WND-page forecast time, like the barbs, so the
    # table rolls forward to now and tracks the +Nh selector (internet winds now
    # carry a forecast series rather than a single fetch-time hour).
    off = int(disp["ds"].get("winds_time_offset_h", 0)) if src == "INET" \
        else int((w or {}).get("hour_offset", 0))
    levels = (_wx.winds_levels_at(w, time.time() + off * 3600.0)
              if (w and src == "INET") else ((w or {}).get("levels") or []))
    time_txt = "" if off == 0 else f"  +{off}h"
    age_s = store.winds_age_s(station) if store else None
    age_txt = _fisb.short_age(age_s)
    age_part = f"  ·  {age_txt}" if age_txt else ""
    title = f"WINDS  {head}{src_txt}{time_txt}{age_part}{near_txt}"

    def _hdr(s, x, y):
        _text(s, "ALT", 15, (140, 160, 180), bold=True, x=x, y=y)
        _text(s, "WIND", 15, (140, 160, 180), bold=True, x=x + 115, y=y)
        _text(s, "TEMP", 15, (140, 160, 180), bold=True, x=x + 270, y=y)
    items = [(24, _hdr)]
    if levels:
        for lv in levels:
            if lv.get("lv"):
                wind = "LT & VAR"
            elif lv.get("dir") is None:
                wind = "—"
            else:
                wind = f"{lv['dir']:03d}°  {lv['spd']} kt"
            temp = "" if lv.get("temp") is None else f"{lv['temp']:+d}°C"

            def _row(s, x, y, a=lv["alt_ft"], wd=wind, tp=temp):
                _text(s, f"{a:,} ft", 17, (210, 220, 230), x=x, y=y)
                _text(s, wd, 17, (210, 220, 230), x=x + 115, y=y)
                _text(s, tp, 17, (190, 205, 225), x=x + 270, y=y)
            items.append((24, _row))
    else:
        items.append((24, lambda s, x, y: _text(s, "No winds aloft.", 18,
                                                 (200, 215, 230), x=x, y=y)))
    _draw_wx_scroll_panel(surf, title, items, min(560, DISPLAY_W - 40))


def _draw_wx_text(surf):
    """Scrolling text readout for area advisories (AIRMET/SIGMET/NOTAM)."""
    wt = disp.get("wx_text")
    if not wt:
        return
    pw = min(680, DISPLAY_W - 32)
    f16 = _get_font(16)
    items = []
    bulletins = wt.get("bulletins") or []
    for b in bulletins:
        for ln in _wrap_text(b, f16, pw - 40):
            items.append((20, lambda s, x, y, t=ln:
                          _text(s, t, 16, (200, 215, 230), x=x, y=y)))
        items.append((10, None))             # gap between bulletins
    if not bulletins:
        items.append((24, lambda s, x, y: _text(s, "None active in range.",
                                                 18, (200, 215, 230), x=x, y=y)))
    _draw_wx_scroll_panel(surf, wt.get("title", "WX"), items, pw)


def _mfd_find_metar(tap_x, tap_y, tap_px=30):
    """Nearest METAR station within tap_px of the tap, or None."""
    metars = disp.get("weather", {}).get("metars") or []
    if not metars:
        return None
    cen_lat, cen_lon = _mfd_effective_center()
    project, _ = _map_mod.make_projector(
        (0, 0, DISPLAY_W, DISPLAY_H), cen_lat, cen_lon, _mfd_last_orient,
        _mfd_last_range, disp.get("yaw", 0.0), _mfd_last_track)
    best_d2 = (tap_px + 1) ** 2
    best = None
    for m in metars:
        la, lo = m.get("lat"), m.get("lon")
        if la is None or lo is None:
            continue
        sx, sy = project(la, lo)
        dd = (sx - tap_x) ** 2 + (sy - tap_y) ** 2
        if dd < best_d2:
            best_d2 = dd
            best = m
    return best


def _mfd_find_traffic(tap_x, tap_y, tap_px=32):
    """Nearest drawn ADS-B target within tap_px of the tap, or None."""
    targets = _traffic_to_draw()
    if not targets:
        return None
    cen_lat, cen_lon = _mfd_effective_center()
    project, _ = _map_mod.make_projector(
        (0, 0, DISPLAY_W, DISPLAY_H), cen_lat, cen_lon, _mfd_last_orient,
        _mfd_last_range, disp.get("yaw", 0.0), _mfd_last_track)
    best_d2 = (tap_px + 1) ** 2
    best = None
    for t in targets:
        la, lo = t.get("lat"), t.get("lon")
        if la is None or lo is None:
            continue
        sx, sy = project(la, lo)
        dd = (sx - tap_x) ** 2 + (sy - tap_y) ** 2
        if dd < best_d2:
            best_d2 = dd
            best = t
    return best


def _traffic_clock(brg, own_hdg):
    """Clock position (1-12) of a target at absolute bearing ``brg`` relative to
    the aircraft's nose, or None."""
    if brg is None:
        return None
    rel = (brg - (own_hdg or 0.0)) % 360.0
    c = int(round(rel / 30.0)) % 12
    return 12 if c == 0 else c


def _draw_traffic_banner(surf, rect):
    """Red collision-alert banner at the top of the map when a target is in the
    alert band — clock position + relative altitude so it's actionable at a
    glance (e.g. 'TRAFFIC  2 o'clock  −200').  Flashes at 1 Hz."""
    t = disp.get("traffic", {}).get("alert_target")
    if not t:
        return
    if (pygame.time.get_ticks() // 500) % 2 == 1:     # 1 Hz flash, off phase
        return
    x, y, w, h = rect
    clk = _traffic_clock(t.get("bearing_deg"), disp.get("yaw"))
    parts = ["TRAFFIC"]
    if clk is not None:
        parts.append(f"{clk} o'clock")
    ra = t.get("rel_alt_ft")
    if ra is not None:
        hund = int(round(ra / 100.0)) * 100
        parts.append(f"{'+' if hund >= 0 else '−'}{abs(hund)}")
    rng = t.get("range_nm")
    if rng is not None:
        parts.append(f"{rng:.1f} nm")
    label = "   ".join(parts)
    f = _get_font(22, bold=True)
    tw = f.size(label)[0]
    bw, bh = tw + 36, 36
    bx, by = x + (w - bw) // 2, y + 8
    pygame.draw.rect(surf, (200, 0, 0), (bx, by, bw, bh), border_radius=6)
    pygame.draw.rect(surf, (255, 230, 230), (bx, by, bw, bh), width=2,
                     border_radius=6)
    _text(surf, label, 22, (255, 255, 255), bold=True,
          cx=bx + bw // 2, cy=by + bh // 2)


def _draw_pfd_traffic_alert(surf):
    """Compact traffic collision-alert banner in the PFD badge strip — the same
    place the TERRAIN/PULL UP banner uses, stacked just below it when terrain is
    also alerting.  Flashes at 1 Hz."""
    t = disp.get("traffic", {}).get("alert_target")
    if not t:
        return
    if (pygame.time.get_ticks() // 500) % 2 == 1:     # 1 Hz flash, off phase
        return
    clk = _traffic_clock(t.get("bearing_deg"), disp.get("yaw"))
    parts = ["TFC"]
    if clk is not None:
        parts.append(f"{clk}:00")
    ra = t.get("rel_alt_ft")
    if ra is not None:
        hund = int(round(ra / 100.0)) * 100
        parts.append(f"{'+' if hund >= 0 else '−'}{abs(hund)}")
    label = "  ".join(parts)
    bw, bh = 150, 16
    bx = CX - bw // 2
    by = 3 + (20 if _terrain_alert_level else 0)      # stack under terrain banner
    pygame.draw.rect(surf, (200, 0, 0), (bx, by, bw, bh), border_radius=3)
    pygame.draw.rect(surf, (255, 230, 230), (bx, by, bw, bh), width=1,
                     border_radius=3)
    _text(surf, label, 11, (255, 255, 255), bold=True,
          cx=bx + bw // 2, cy=by + bh // 2)


def _draw_tfc_popup(surf):
    """ADS-B target detail card (callsign, altitude, vector, range/bearing,
    source) for the aircraft tapped on the TFC page."""
    t = disp.get("tfc_popup")
    if not t:
        return
    pw = min(560, DISPLAY_W - 40)
    cs = (t.get("callsign") or "").strip() or str(t.get("icao", "----"))
    threat = t.get("threat", "other")
    hcol = {"alert": (235, 80, 80),
            "proximate": (235, 175, 60)}.get(threat, (235, 235, 235))
    alt, ra = t.get("alt_ft"), t.get("rel_alt_ft")
    gs, trk, vv = t.get("gs_kt"), t.get("track_deg"), t.get("vvel_fpm")
    rng, brg = t.get("range_nm"), t.get("bearing_deg")
    if alt is None:
        alt_row = "Altitude —"
    else:
        rel = ("" if ra is None
               else f"   ({'+' if ra >= 0 else '−'}{abs(int(round(ra))):,} ft)")
        alt_row = f"Altitude {int(round(alt)):,} ft{rel}"
    if vv is None:
        vv_row = ""
    elif abs(vv) < 100:
        vv_row = "Level"
    else:
        vv_row = f"{'Climbing' if vv > 0 else 'Descending'} {abs(int(round(vv))):,} fpm"
    rows = [r for r in [
        alt_row,
        f"Speed {int(round(gs))} kt" if gs is not None else "Speed —",
        f"Track {int(round(trk)) % 360:03d}°" if trk is not None else "Track —",
        vv_row,
        (f"Range {rng:.1f} nm {_compass8(brg)}".rstrip()
         if rng is not None else ""),
    ] if r]

    ph = 60 + len(rows) * 28 + 30
    ph = min(ph, DISPLAY_H - 24)
    px = (DISPLAY_W - pw) // 2
    py = (DISPLAY_H - ph) // 2
    dim = pygame.Surface((DISPLAY_W, DISPLAY_H), pygame.SRCALPHA)
    dim.fill((0, 0, 0, 150))
    surf.blit(dim, (0, 0))
    pygame.draw.rect(surf, (10, 18, 34), (px, py, pw, ph), border_radius=10)
    pygame.draw.rect(surf, (80, 110, 150), (px, py, pw, ph), width=2,
                     border_radius=10)
    _text(surf, cs, 30, hcol, bold=True, x=px + 18, y=py + 12)
    src = (t.get("src") or "").lower()
    s_lbl = {"radio": "RADIO", "internet": "INET"}.get(src, "")
    if s_lbl:
        _text(surf, s_lbl, 18, (150, 180, 220), bold=True,
              x=px + pw - 90, y=py + 18)
    yy = py + 60
    for rr in rows:
        _text(surf, rr, 22, (210, 220, 230), x=px + 18, y=yy)
        yy += 28
    _text(surf, "tap to close", 16, (140, 150, 160),
          cx=DISPLAY_W // 2, cy=py + ph - 14)


def _mfd_find_airport(tap_x, tap_y, tap_px=40):
    """Nearest pickable airport within tap_px of the tap — returns
    (ident, lat, lon) or None.  Bigger target than the dot itself (fiddly to
    hit on a touch panel).  Matches what the renderer DRAWS: every visible
    type out to 40 nm (the pi4 MFD shows all dots, no zoom-band declutter), so
    anything you can see you can tap."""
    if _airports is None:
        return None
    range_nm = _mfd_last_range or 10
    if range_nm > 40 or range_nm <= 0:
        return None
    cen_lat, cen_lon = _mfd_effective_center()
    project, _ = _map_mod.make_projector(
        (0, 0, DISPLAY_W, DISPLAY_H), cen_lat, cen_lon, _mfd_last_orient,
        range_nm, disp.get("yaw", 0.0), _mfd_last_track)
    apt_types = {
        "S": disp["ad"].get("show_public", True),
        "M": disp["ad"].get("show_public", True),
        "L": disp["ad"].get("show_public", True),
        "H": disp["ad"].get("show_heli", True),
        "W": disp["ad"].get("show_seaplane", False),
        "B": disp["ad"].get("show_other", False),
    }
    nearby = apt_mod.query_nearby(_airports, cen_lat, cen_lon,
                                  radius_nm=range_nm * 1.4)
    if nearby is None or len(nearby) == 0:
        return None
    best_d2 = (tap_px + 1) ** 2
    best = None      # (ident, lat, lon)
    if hasattr(nearby, "dtype"):
        for i in range(len(nearby)):
            atype = str(nearby["atype"][i])
            if not apt_types.get(atype, False):
                continue
            la, lo = float(nearby["lat"][i]), float(nearby["lon"][i])
            sx, sy = project(la, lo)
            dd = (sx - tap_x) ** 2 + (sy - tap_y) ** 2
            if dd < best_d2:
                best_d2 = dd
                best = (str(nearby["ident"][i]), la, lo)
    else:
        for r in nearby:
            atype = getattr(r, "atype", "")
            if not apt_types.get(atype, False):
                continue
            la, lo = float(r.lat), float(r.lon)
            sx, sy = project(la, lo)
            dd = (sx - tap_x) ** 2 + (sy - tap_y) ** 2
            if dd < best_d2:
                best_d2 = dd
                best = (r.ident, la, lo)
    return best


def _mfd_find_d2_dest(tap_x, tap_y, tap_px=46):
    """If a Direct-To is loaded, return its (ident, lat, lon) when the tap
    lands on the destination waypoint — at ANY zoom, since the D2 diamond is
    drawn even past the 40 nm airport-dot range.  Lets you pull up the
    destination's WX / re-confirm D2 without zooming in.  Else None."""
    nav = disp.get("nav") or {}
    ident = nav.get("ident")
    if not ident or nav.get("lat") is None or nav.get("lon") is None:
        return None
    cen_lat, cen_lon = _mfd_effective_center()
    project, _ = _map_mod.make_projector(
        (0, 0, DISPLAY_W, DISPLAY_H), cen_lat, cen_lon, _mfd_last_orient,
        _mfd_last_range or 10, disp.get("yaw", 0.0), _mfd_last_track)
    sx, sy = project(float(nav["lat"]), float(nav["lon"]))
    if (sx - tap_x) ** 2 + (sy - tap_y) ** 2 <= (tap_px + 1) ** 2:
        return (ident, float(nav["lat"]), float(nav["lon"]))
    return None


_COMPASS8 = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


def _compass8(deg):
    """Degrees → 8-point compass (general direction, not an exact radial)."""
    if deg is None:
        return ""
    return _COMPASS8[int((deg + 22.5) % 360 // 45)]


def _wx_for_airport(ident, alat, alon, max_nm=75.0):
    """Best METAR to show for an airport: the station whose ICAO matches the
    field, else the nearest station within ``max_nm``.  Returns (metar_dict,
    dist_nm, bearing_deg) — dist 0 / bearing None for an on-field match — or
    (None, None, None) if nothing suitable is loaded.

    The distance cap matters after a large pan: the view-driven poller still
    holds the OLD area's METARs for a few seconds, and the nearest of those to
    the newly-tapped field can be hundreds of miles away.  Showing none (the
    live picker fills in when the new fetch lands) beats showing far-off WX."""
    metars = disp.get("weather", {}).get("metars") or []
    if not metars or alat is None or alon is None:
        return None, None, None
    ident_u = (ident or "").upper()
    for m in metars:
        if str(m.get("icao", "")).upper() == ident_u:
            return m, 0.0, None
    best, best_d = None, 1e9
    for m in metars:
        la, lo = m.get("lat"), m.get("lon")
        if la is None or lo is None:
            continue
        dlat = (la - alat) * 60.0
        dlon = (lo - alon) * 60.0 * math.cos(math.radians((la + alat) / 2.0))
        d = math.hypot(dlat, dlon)
        if d < best_d:
            best_d, best = d, m
    if best is None or best_d > max_nm:
        return None, None, None
    # Bearing from the field out to the station (where the weather actually is).
    bla, blo = best["lat"], best["lon"]
    ndlat = bla - alat
    ndlon = (blo - alon) * math.cos(math.radians((bla + alat) / 2.0))
    brg = math.degrees(math.atan2(ndlon, ndlat)) % 360.0
    return best, best_d, brg


def _mfd_pick_rects():
    """Panel + two button rects for the coincident airport/METAR chooser."""
    pw, ph = 380, 214
    px = (DISPLAY_W - pw) // 2
    py = (DISPLAY_H - ph) // 2
    bw, bh = pw - 40, 56
    wx_r = (px + 20, py + 60, bw, bh)
    d2_r = (px + 20, py + 60 + bh + 14, bw, bh)
    return (px, py, pw, ph), wx_r, d2_r


def _draw_mfd_pick(surf):
    """Chooser shown when a tap lands on an airport (or the D2 destination):
    the pilot picks the Weather readout or Direct-To.  WX is resolved LIVE
    each frame so a post-pan fetch fills it in without re-tapping, and a far
    stale station is never shown (_wx_for_airport caps the distance)."""
    pick = disp.get("mfd_pick")
    if not pick:
        return
    (px, py, pw, ph), wx_r, d2_r = _mfd_pick_rects()
    dim = pygame.Surface((DISPLAY_W, DISPLAY_H), pygame.SRCALPHA)
    dim.fill((0, 0, 0, 150))
    surf.blit(dim, (0, 0))
    pygame.draw.rect(surf, (10, 18, 34), (px, py, pw, ph), border_radius=10)
    pygame.draw.rect(surf, (80, 110, 150), (px, py, pw, ph), width=2,
                     border_radius=10)
    _text(surf, "What here?", 22, (210, 220, 230), bold=True,
          cx=DISPLAY_W // 2, cy=py + 28)
    ident = pick.get("airport", "")
    wx, dist, brg = _wx_for_airport(ident, pick.get("lat"), pick.get("lon"))
    if wx is None:
        _action_btn(surf, *wx_r, "WX  …", "normal", r=8)   # loading / none near
    else:
        icao = wx.get("icao", "WX")
        # Flag a *nearby* station (with general direction), not the field itself.
        if dist and dist >= 2.0:
            wx_label = f"Weather  {icao} · {dist:.0f} nm {_compass8(brg)}".rstrip()
        else:
            wx_label = f"Weather  {icao}"
        _action_btn(surf, *wx_r, wx_label, "ok", r=8)
    _action_btn(surf, *d2_r, f"Direct →  {ident}", "warn", r=8)
    _text(surf, "tap outside to cancel", 15, (140, 150, 160),
          cx=DISPLAY_W // 2, cy=py + ph - 16)


def _mfd_pick_hit(x, y):
    """Handle a tap while the airport/METAR chooser is up."""
    pick = disp.get("mfd_pick")
    if not pick:
        return
    _, wx_r, d2_r = _mfd_pick_rects()

    def _in(rc):
        return rc[0] <= x <= rc[0] + rc[2] and rc[1] <= y <= rc[1] + rc[3]
    if _in(wx_r):
        # Weather → the product menu (METAR / TAF / AIRMET / SIGMET / NOTAM),
        # so each is its own readout instead of one crammed box.
        wx, _, _ = _wx_for_airport(pick.get("airport", ""),
                                   pick.get("lat"), pick.get("lon"))
        disp["mfd_pick"] = None
        disp["wx_menu"] = {
            "airport": pick.get("airport", ""),
            "icao": (wx or {}).get("icao") or pick.get("airport", ""),
            "lat": pick.get("lat"), "lon": pick.get("lon"),
            "metar": dict(wx) if wx else None,
        }
    elif _in(d2_r):
        ident = pick.get("airport", "")
        disp["mfd_pick"] = None
        if ident:
            _nav_open_confirm(ident, "pfd")
    else:
        disp["mfd_pick"] = None      # tap outside cancels


# ── Quick MAP LAYERS panel (MFD) ──────────────────────────────────────────────
# A layers icon sits just above the MFD "+" zoom button; tapping it opens this
# panel so the pilot can flip map layers on/off in flight without diving into
# the Display setup screen.  Reuses the same _DSP_MAP_LAYERS keys/labels.
_MFD_LYR_COLS = 4
_MFD_LYR_PW   = 112
_MFD_LYR_PH   = 46
_MFD_LYR_G    = 10


def _mfd_layers_rects():
    """Centred panel rect + a (key, label, pill_rect) list for each layer."""
    n    = len(_DSP_MAP_LAYERS)
    cols = _MFD_LYR_COLS
    rows = (n + cols - 1) // cols
    gw   = cols * _MFD_LYR_PW + (cols - 1) * _MFD_LYR_G
    gh   = rows * _MFD_LYR_PH + (rows - 1) * _MFD_LYR_G
    title_h, pad = 34, 18
    pw = gw + 2 * pad
    ph = gh + title_h + pad + 22          # +22 for the footer hint
    px = (DISPLAY_W - pw) // 2
    py = (DISPLAY_H - ph) // 2
    gx = px + pad
    gy = py + title_h
    cells = []
    for i, (key, lbl) in enumerate(_DSP_MAP_LAYERS):
        r_i, c_i = divmod(i, cols)
        cx = gx + c_i * (_MFD_LYR_PW + _MFD_LYR_G)
        cy = gy + r_i * (_MFD_LYR_PH + _MFD_LYR_G)
        cells.append((key, lbl, (cx, cy, _MFD_LYR_PW, _MFD_LYR_PH)))
    return (px, py, pw, ph), cells


def _draw_mfd_layers_icon(surf, rect):
    """Stacked-layers glyph in a button-weight box (matches the zoom keys)."""
    bx, by, bw, bh = rect
    pygame.draw.rect(surf, (0, 10, 25), (bx, by, bw, bh), border_radius=8)
    pygame.draw.rect(surf, (50, 68, 92), (bx, by, bw, bh), width=2,
                     border_radius=8)
    cx, cy = bx + bw // 2, by + bh // 2
    hw, hh = 16, 6
    for dy in (-12, 0, 12):
        yy = cy + dy
        pygame.draw.polygon(
            surf, CYAN,
            [(cx - hw, yy), (cx, yy - hh), (cx + hw, yy), (cx, yy + hh)], width=2)


def _draw_mfd_layers(surf):
    """Quick MAP LAYERS toggle panel, drawn on top when open."""
    if not disp.get("mfd_layers"):
        return
    ds = disp["ds"]
    (px, py, pw, ph), cells = _mfd_layers_rects()
    dim = pygame.Surface((DISPLAY_W, DISPLAY_H), pygame.SRCALPHA)
    dim.fill((0, 0, 0, 140))
    surf.blit(dim, (0, 0))
    pygame.draw.rect(surf, (10, 18, 34), (px, py, pw, ph), border_radius=10)
    pygame.draw.rect(surf, (80, 110, 150), (px, py, pw, ph), width=2,
                     border_radius=10)
    _text(surf, "MAP LAYERS", 20, (210, 220, 230), bold=True,
          cx=px + pw // 2, cy=py + 20)
    for key, lbl, rc in cells:
        _seg_btn(surf, rc[0], rc[1], rc[2], rc[3], lbl, bool(ds.get(key)))
    _text(surf, "tap a layer to toggle  ·  tap outside to close", 14,
          (140, 150, 160), cx=px + pw // 2, cy=py + ph - 15)


def _mfd_layers_hit(x, y):
    """Handle a tap while the quick MAP LAYERS panel is open."""
    (px, py, pw, ph), cells = _mfd_layers_rects()
    for key, _lbl, rc in cells:
        if rc[0] <= x <= rc[0] + rc[2] and rc[1] <= y <= rc[1] + rc[3]:
            disp["ds"][key] = not bool(disp["ds"].get(key))
            _settings.mark_dirty()
            return                              # stay open for more toggles
    if not (px <= x <= px + pw and py <= y <= py + ph):
        disp["mfd_layers"] = False              # tap outside closes


def _p4_mfd_rects():
    """Chrome button rects for the full-screen MFD (keyed by name)."""
    W, H = DISPLAY_W, DISPLAY_H
    p, bw, bh = _P4_MFD_PAD, _P4_MFD_BW, _P4_MFD_BH
    z = _P4_MFD_ZOOM
    _, sy, _, _ = _mfd_strip_rect()        # zoom buttons sit above the strip
    zy = sy - 6 - z
    # No PFD button — the 3-finger hold swaps back to the PFD (like piZ).
    return {
        "d2":       (p, p, bw, bh),                       # top-left
        "winds":    (p + bw + p, p, bw, bh),              # top row, right of D2
        "wtime":    (p + 2 * (bw + p), p, bw, bh),        # WND only, right of alt
        "fpl":      (W - bw - p, p, bw, bh),              # top-right
        "ovly":     (p, p + bh + p, bw, bh),              # under D2
        "orient":   (W - bw - p, p + bh + p, bw, bh),     # under FPL
        "center":   (W - bw - p, p + 2 * (bh + p), bw, bh),  # CTR (when panned)
        "zoom_out": (p, zy, z, z),                        # above strip, left
        "zoom_in":  (W - z - p, zy, z, z),                # above strip, right
        "layers":   (W - z - p, zy - p - z, z, z),        # just above the + button
    }


_mfd_adsb_status_rect = None   # tap zone to cycle traffic_source
_mfd_wx_status_rect   = None   # tap zone to cycle wx_source
_mfd_winds_status_rect = None  # tap zone to cycle the winds-barb altitude


def _mfd_draw_source_status(surf):
    """Source health, stacked down the LEFT side (clear of the forward view).
    Only the labels relevant to what's on screen: ADS-B always (with the
    traffic source mode; tap to cycle), WX when METAR overlay is up, NEX when
    NEXRAD overlay is up.  Green = receiving, amber = enabled but no data;
    data age shown for WX/NEX."""
    global _mfd_adsb_status_rect, _mfd_wx_status_rect, _mfd_winds_status_rect
    now = time.monotonic()
    ds = disp["ds"]
    pt = max(11, int(round(15 * globals().get("MFD_FONT_SCALE", 1.0))))

    def age(c):
        u = getattr(c, "updated_s", 0.0) or 0.0
        if u <= 0:
            return ""
        a = now - u
        return f" {int(a)}s" if a < 60 else f" {int(a / 60)}m"

    f = _get_font(pt, bold=True)
    x = _P4_MFD_PAD
    y = _P4_MFD_PAD + 2 * (_P4_MFD_BH + _P4_MFD_PAD) + 8   # below D2 + OVLY
    _mfd_adsb_status_rect = None
    _mfd_winds_status_rect = None

    if _adsb_client is not None:
        src = disp["cs"].get("traffic_source", "auto")
        mode = {"auto": "AUTO", "radio": "RADIO",
                "internet": "INET"}.get(src, "AUTO")
        tr = disp.get("traffic", {})
        nr, ni = tr.get("n_radio", 0), tr.get("n_inet", 0)
        # Split counts so the pilot can tell radio from internet: R = targets
        # from the radio/SDR, I = targets from the built-in internet feed.
        # RADIO mode pauses the feed, so I never shows there.
        parts = [f"ADS-B {mode}"]
        if src != "internet":
            parts.append(f"R{nr}")
        if src != "radio":
            parts.append(f"I{ni}")
        txt = " ".join(parts)
        live = (nr > 0 or ni > 0) or _adsb_client.connected
        col = (60, 220, 90) if live else (220, 160, 60)
        if not live:
            txt += " …"
        _text(surf, txt, pt, col, bold=True, x=x, y=y)
        _mfd_adsb_status_rect = (x - 4, y - 3, f.size(txt)[0] + 8, pt + 8)
        y += pt + 8
    _mfd_wx_status_rect = None
    # WX source line shows on every page (like ADS-B) so the FIS-B/INET counts
    # and the RADIO/AUTO/INET toggle are always reachable — not just on MET.
    if _wx_client is not None:
        w = disp.get("weather", {})
        wsrc = disp["cs"].get("wx_source", "auto")
        wmode = {"auto": "AUTO", "radio": "RADIO",
                 "internet": "INET"}.get(wsrc, "AUTO")
        n_rdr, n_inet = w.get("n_rdr", 0), w.get("n_inet", 0)
        # Same R/I split + tap-to-cycle as the ADS-B line above.
        live = (w.get("n", 0) > 0
                or (_wx_client.connected and wsrc != "radio"))
        parts = [f"WX {wmode}"]
        if wsrc != "internet":
            parts.append(f"R{n_rdr}")
        if wsrc != "radio":
            parts.append(f"I{n_inet}")
        txt = " ".join(parts) + age(_wx_client)
        col = (60, 220, 90) if live else (220, 160, 60)
        if not live:
            txt += " …"
        _text(surf, txt, pt, col, bold=True, x=x, y=y)
        _mfd_wx_status_rect = (x - 4, y - 3, f.size(txt)[0] + 8, pt + 8)
        y += pt + 8
    # Winds-cache fill + age — only on the WND page (informational).
    if ds.get("map_show_winds") and _winds_client is not None:
        txt = _winds_status_text()
        fresh, total, _age, _stale, _exp = _winds_client.status()
        col = (60, 220, 90) if fresh >= total and fresh > 0 else (220, 160, 60)
        _text(surf, txt, pt, col, bold=True, x=x, y=y)
        _mfd_winds_status_rect = (x - 4, y - 3, f.size(txt)[0] + 8, pt + 8)
        y += pt + 8
    if ds.get("map_show_nexrad"):
        if _nexrad_client is not None:
            if _nexrad_client.connected:
                txt = f"NEX{age(_nexrad_client)}"
                col = (60, 220, 90)
            else:
                txt = f"NEX …{age(_nexrad_client)}"
                col = (220, 160, 60)
            _text(surf, txt, pt, col, bold=True, x=x, y=y)
            y += pt + 8
        # FIS-B radar carries its own *valid* time, which can lag receipt by
        # minutes — badge that age (green<10 / amber<20 / red) so stale radar
        # never reads as current.
        _store = _fisb_store()
        nst = _store.nexrad_status() if _store is not None else None
        if nst is not None:
            va = nst.get("valid_age_min")
            if va is None:
                vtxt, vcol = "NEX RDR valid —", (160, 180, 200)
            else:
                vtxt = f"NEX RDR valid {va:.0f}m"
                vcol = ((60, 220, 90) if va < 10 else
                        (220, 160, 60) if va < 20 else (235, 70, 70))
            _text(surf, vtxt, pt, vcol, bold=True, x=x, y=y)


def _mfd_source_status_hit(x, y):
    r = _mfd_adsb_status_rect
    return r is not None and r[0] <= x <= r[0]+r[2] and r[1] <= y <= r[1]+r[3]


def _mfd_winds_status_hit(x, y):
    r = _mfd_winds_status_rect
    return r is not None and r[0] <= x <= r[0]+r[2] and r[1] <= y <= r[1]+r[3]


def _mfd_wx_status_hit(x, y):
    r = _mfd_wx_status_rect
    return r is not None and r[0] <= x <= r[0]+r[2] and r[1] <= y <= r[1]+r[3]


def _mfd_cycle_traffic_source():
    order = ["auto", "radio", "internet"]
    cur = disp["cs"].get("traffic_source", "auto")
    nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else "auto"
    disp["cs"]["traffic_source"] = nxt
    _settings.mark_dirty()
    return nxt


def _mfd_cycle_wx_source():
    order = ["auto", "radio", "internet"]
    cur = disp["cs"].get("wx_source", "auto")
    nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else "auto"
    disp["cs"]["wx_source"] = nxt
    _settings.mark_dirty()
    return nxt


def draw_mfd(surf, connected=True, data_stale=False):
    """Full-screen moving map for the larger screens — pans, configurable
    bottom data strip, overlay cycle, zoom, direct-to, and a PFD button."""
    surf.fill((0, 0, 0))
    rect = (0, 0, DISPLAY_W, DISPLAY_H)
    ds = disp["ds"]
    ac_lat = disp.get("lat", DEMO_LAT)
    ac_lon = disp.get("lon", DEMO_LON)
    cen_lat, cen_lon = _mfd_effective_center()
    alt = disp.get("alt", 0.0)
    hdg = disp.get("yaw", 0.0)
    track = disp.get("track", hdg)
    gs_kt = disp.get("speed", 0.0)
    map_track = track if gs_kt >= 3.0 else None

    d2 = dict(disp.get("nav") or {})
    _ap = disp.get("approach") or {}
    # Centreline (cyan) only on the final leg; intermediate D2/activated legs
    # draw the normal magenta course to the fix.
    d2["approach_active"] = _approach_centerline_active()
    if d2["approach_active"]:
        d2["approach_course_deg"] = float(_ap.get("course_deg", 0.0))
        d2["approach_final_nm"]   = _approach_hits_final_nm()

    _ad = disp.get("ad", {})
    types_vis = set()
    if _ad.get("show_public", True):
        types_vis.update({"S", "M", "L"})
    if _ad.get("show_heli", True):
        types_vis.add("H")
    if _ad.get("show_seaplane", False):
        types_vis.add("W")
    if _ad.get("show_other", False):
        types_vis.add("B")

    # The WND overlay carries its own limited zoom (40/80/160) so it neither
    # offers a useless close-in view nor disturbs the terrain-map zoom.
    zoom_pref = (_winds_zoom() if _map_overlay_state(ds) == "wnd"
                 else int(ds.get("map_zoom_nm", 10)))
    if zoom_pref == _map_mod.ZOOM_AUTO:
        if d2.get("ident"):
            # Fit range to the leg from the AIRCRAFT to the waypoint — NOT the
            # panned map centre, or panning would change the distance and
            # rescale the map every frame (the "jumpy pan" bug).
            cos_lat = max(0.05, math.cos(math.radians(ac_lat)))
            n_nm = (d2["lat"] - ac_lat) * 60.0
            e_nm = (d2["lon"] - ac_lon) * 60.0 * cos_lat
            eff_range = _map_mod.auto_fit_range(math.hypot(n_nm, e_nm) * 1.10)
        else:
            eff_range = _map_mod.ZOOM_LEVELS[-1]
        eff_label = "AUTO"
    else:
        eff_range = zoom_pref
        eff_label = None
    # Respect the TRK↑ / N↑ choice at every range (matches the piZ MFD —
    # no silent force to north-up at wide zoom).
    eff_orient = ds.get("map_orient", "trk")
    global _mfd_last_range, _mfd_last_orient, _mfd_last_track
    _mfd_last_range, _mfd_last_orient, _mfd_last_track = (
        eff_range, eff_orient, map_track)

    _map_mod.render(
        surf, rect, cen_lat, cen_lon, alt, hdg, map_track, eff_orient,
        eff_range, ds,
        airports_arr=_airports, runways_arr=_runways, obstacles_arr=_obstacles,
        srtm_dir=SRTM_DIR, water_dir=WATER_DIR,
        direct_to=d2 if d2.get("ident") else None,
        font=_mfd_get_apt_font(),
        airport_types_visible=types_vis, gs_kt=gs_kt,
        vso_kt=disp["fp"].get("vs0", VS0),
        range_label=eff_label,
        state_lines=_state_lines, country_lines=_country_lines,
        fpl_remaining=_fpl_render_remaining(),
        approach_path=_approach_render_path(),
        missed_path=_approach_render_missed(),
        holds=_approach_render_holds(),
        airspaces=_airspaces,
        traffic=_traffic_to_draw(),
        metars=disp.get("weather", {}).get("metars"),
        ground_stations=disp.get("weather", {}).get("stations"),
        wx_graphics=disp.get("weather", {}).get("graphics"),
        winds_barbs=(_winds_barbs() if (ds.get("map_show_winds")
                     and eff_range >= WINDS_MIN_RENDER_NM) else None),
        nexrad=_nexrad_render_arg(),
        nexrad_cells=(_fisb_nexrad_cells()
                      if ds.get("map_show_nexrad") else None),
        own_lat=ac_lat, own_lon=ac_lon,
        symbol_scale=2.0,            # bigger airport dots on the big MFD
        # While actively dragging a pan, skip the heavy layers (terrain
        # tint, airspace, NEXRAD, METAR, obstacles, airports) so the map
        # tracks the finger; they repaint on release.
        fast=bool(_mfd_drag is not None and _mfd_drag.get("is_drag")),
        draw_corner_labels=False)

    # ── Bottom data strip ──────────────────────────────────────────────────
    sx, sy, sw, sh = _mfd_strip_rect()
    plate = pygame.Surface((sw, sh), pygame.SRCALPHA)
    plate.fill((0, 8, 22, 190))
    surf.blit(plate, (sx, sy))
    pygame.draw.line(surf, (60, 80, 110), (sx, sy), (sx + sw - 1, sy), 1)
    # Aircraft data strip — dist/brg/AGL/XTE are all ownship-relative, so feed
    # the AIRCRAFT position, never the panned map centre (else they drift as you
    # pan, which is wrong and was part of the "jumpy pan" report).
    ctx = _mfd_strip_ctx(ac_lat, ac_lon, alt, hdg, track, gs_kt,
                         d2 if d2.get("ident") else None)
    col_w = sw // _MFD_STRIP_SLOT_COUNT
    # Data-strip text uses a gentler scale than the map labels: the airport
    # names needed to shrink a lot on the 10", but the strip caption/value
    # got too small at the same factor.  MFD_STRIP_FONT_SCALE backs off.
    _fsc = globals().get("MFD_STRIP_FONT_SCALE",
                         globals().get("MFD_FONT_SCALE", 1.0))
    _cap_pt = max(11, int(round(13 * _fsc)))
    _val_pt = max(18, int(round(26 * _fsc)))
    for i, kind in enumerate(_mfd_strip_kinds()):
        cap, val, col = _mfd_strip_format(kind, ctx)
        cx = sx + col_w // 2 + col_w * i
        _text(surf, cap, _cap_pt, (140, 170, 200), bold=True, cx=cx, y=sy + 5)
        _text(surf, val, _val_pt, col, bold=True, cx=cx, cy=sy + 36)

    # ── Chrome ─────────────────────────────────────────────────────────────
    # No PFD button — 3-finger hold swaps back to the PFD (like piZ).
    r = _p4_mfd_rects()
    d2_style = "warn" if d2.get("ident") else "normal"
    d2_label = f"D→ {d2['ident']}" if d2.get("ident") else "D→"
    _action_btn(surf, *r["d2"], d2_label, d2_style, r=6)
    _action_btn(surf, *r["fpl"], "FPL", "normal", r=6)
    ov_state = _map_overlay_state(ds)
    # Winds altitude + forecast-time selectors — only on the WND page.
    if ov_state == "wnd":
        alt_k = int(ds.get("winds_alt_ft", 9000)) // 1000
        _action_btn(surf, *r["winds"], f"{alt_k}k ft", "ok", r=6)
        off = int(ds.get("winds_time_offset_h", 0))
        _action_btn(surf, *r["wtime"], "NOW" if off == 0 else f"+{off}h",
                    "ok" if off == 0 else "warn", r=6)
    _action_btn(surf, *r["ovly"], _map_overlay_label(ds),
                "ok" if ov_state != "tfc" else "normal", r=6)
    # Orientation: a tappable CYAN label (like piZ), not a button.
    ox, oy, ow, oh = r["orient"]
    _text(surf, "TRK↑" if ds.get("map_orient", "trk") == "trk" else "N↑",
          20, CYAN, bold=True, cx=ox + ow // 2, cy=oy + oh // 2)
    if _mfd_is_panned():
        _action_btn(surf, *r["center"], "CTR", "ok", r=6)
    _action_btn(surf, *r["zoom_out"], "−", "normal", r=8)
    _action_btn(surf, *r["zoom_in"], "+", "normal", r=8)
    _draw_mfd_layers_icon(surf, r["layers"])

    # Range / scale readout — lower-left corner, just above the zoom-out button.
    zx, zy, _zw, _zh = r["zoom_out"]
    rng_lbl = (f"{eff_range:g} NM" if eff_label is None
               else f"AUTO · {eff_range:g} NM")
    _text(surf, rng_lbl, 20, CYAN, bold=True, x=zx, y=zy - 28)
    if not connected or data_stale:
        _text(surf, "NO LINK" if not connected else "DATA STALE",
              18, (240, 90, 90), bold=True, cx=DISPLAY_W // 2, cy=sy - 12)

    _mfd_draw_source_status(surf)
    _draw_traffic_banner(surf, (0, 0, DISPLAY_W, DISPLAY_H))
    _draw_wx_popup(surf)
    _draw_mfd_pick(surf)
    _draw_wx_menu(surf)
    _draw_wx_taf(surf)
    _draw_wx_winds(surf)
    _draw_wx_text(surf)
    _draw_tfc_popup(surf)
    _draw_mfd_layers(surf)


# ── MFD strip-slot chooser (tap the data strip) ───────────────────────────────
_MSS_HEADER_H  = 44
_MSS_SLOT_H    = 56
_MSS_SLOT_GAP  = 4
_MSS_GRID_GAP  = 6
_MSS_GRID_COLS = 7          # 21 kinds → 3 rows of 7 on the wide screen
# Description sentence + arrival-time toggle sit below the grid (row count
# derived from the kind list so it follows when options are added).
_MSS_GRID_ROWS = (len(_MFD_STRIP_AVAILABLE) + _MSS_GRID_COLS - 1) // _MSS_GRID_COLS
_MSS_DESC_Y    = (_MSS_HEADER_H + 10 + _MSS_SLOT_H + 20
                  + _MSS_GRID_ROWS * (64 + _MSS_GRID_GAP) + 8)
_MSS_TZ_Y      = _MSS_DESC_Y + 30


def _mss_tz_rects():
    """LOCAL / ZULU pills for the arrival-time mode (applies to all ETA/ETAD)."""
    bw_, bh_, g = 110, 42, 8
    x0 = (DISPLAY_W - (2 * bw_ + g)) // 2 + 70   # nudged right of the label
    return ((x0, _MSS_TZ_Y, bw_, bh_), (x0 + bw_ + g, _MSS_TZ_Y, bw_, bh_))


def _mss_cfg():
    """Which strip the picker is editing: the MFD bottom strip (default) or the
    PFD top ribbon.  Returns (settings_key, default_kinds, slot_count, title)."""
    if disp.get("mss_which") == "pfd":
        return ("pfd_top_kinds", _PFD_TOP_DEFAULT, _PFD_TOP_SLOT_COUNT,
                "PFD TOP ROW")
    return ("mfd_strip_kinds", _MFD_STRIP_DEFAULT, _MFD_STRIP_SLOT_COUNT,
            "MFD STRIP")


def _mss_kinds():
    """Current slot kinds for whichever strip the picker is editing (the MFD
    path runs its one-time migration; the PFD top has none)."""
    return _pfd_top_kinds() if disp.get("mss_which") == "pfd" else _mfd_strip_kinds()


def _mss_slot_rects():
    pad = 8
    n = _mss_cfg()[2]
    pw = (DISPLAY_W - 2 * pad - (n - 1) * _MSS_SLOT_GAP) // n
    y = _MSS_HEADER_H + 10
    return [(pad + i * (pw + _MSS_SLOT_GAP), y, pw, _MSS_SLOT_H)
            for i in range(n)]


def _mss_grid_rects():
    pad = 8
    n = len(_MFD_STRIP_AVAILABLE)
    cols = _MSS_GRID_COLS
    cw = (DISPLAY_W - 2 * pad - (cols - 1) * _MSS_GRID_GAP) // cols
    ch = 64
    y0 = _MSS_HEADER_H + 10 + _MSS_SLOT_H + 20
    out = []
    for i in range(n):
        r, c = divmod(i, cols)
        out.append((pad + c * (cw + _MSS_GRID_GAP),
                    y0 + r * (ch + _MSS_GRID_GAP), cw, ch))
    return out


def draw_mfd_strip_setup(surf):
    key, default, count, title = _mss_cfg()
    _screen_header(surf, title)
    sel = max(0, min(count - 1, int(disp.get("mss_sel", 0))))
    kinds = _mss_kinds()
    for i, (rect, kind) in enumerate(zip(_mss_slot_rects(), kinds)):
        bx, by, bw, bh = rect
        is_sel = (i == sel)
        pygame.draw.rect(surf, (0, 40, 60) if is_sel else (0, 12, 32), rect,
                         border_radius=5)
        pygame.draw.rect(surf, CYAN if is_sel else (60, 80, 110), rect,
                         width=2 if is_sel else 1, border_radius=5)
        _text(surf, f"{i+1}", 11, (140, 150, 170), bold=True,
              cx=bx + bw // 2, y=by + 4)
        _text(surf, _MFD_STRIP_CAPTIONS.get(kind, "?"), 20,
              CYAN if is_sel else WHITE, bold=True,
              cx=bx + bw // 2, cy=by + bh // 2 + 4)
    _text(surf, "tap a slot, then a readout below — auto-advances", 12,
          (140, 150, 170), cx=DISPLAY_W // 2,
          y=_MSS_HEADER_H + 12 + _MSS_SLOT_H)
    for (kind, cap, needs_d2, _desc), rect in zip(_MFD_STRIP_AVAILABLE,
                                                  _mss_grid_rects()):
        bx, by, bw, bh = rect
        in_use = (kinds[sel] == kind)
        pygame.draw.rect(surf, (0, 55, 65) if in_use else (0, 18, 38), rect,
                         border_radius=4)
        pygame.draw.rect(surf, CYAN if in_use else (60, 80, 110), rect,
                         width=1, border_radius=4)
        tc = CYAN if in_use else (WHITE if not needs_d2 else MAGENTA)
        _text(surf, cap, 20, tc, bold=True, cx=bx + bw // 2, cy=by + bh // 2 - 6)
        if needs_d2:
            _text(surf, "needs D2", 10, (140, 100, 130),
                  cx=bx + bw // 2, y=by + bh - 16)

    # Description of the readout in the SELECTED slot (a sentence, as requested).
    sel_kind = kinds[sel]
    local = bool(disp["ds"].get("eta_local", True))
    desc = _MFD_STRIP_DESC.get(sel_kind, "")
    if sel_kind in ("eta", "etad"):
        desc += f"  ·  showing {'LOCAL time' if local else 'ZULU (UTC)'}"
    _text(surf, f"{_MFD_STRIP_CAPTIONS.get(sel_kind, '?')}  —  {desc}", 16,
          (190, 205, 225), bold=True, cx=DISPLAY_W // 2, y=_MSS_DESC_Y)

    # Arrival-time mode toggle — flips every ETA / ETAD readout local ↔ Zulu.
    loc_r, zul_r = _mss_tz_rects()
    _text(surf, "ARRIVAL TIME", 14, (150, 165, 185), bold=True,
          x=loc_r[0] - 135, y=loc_r[1] + 13)
    _seg_btn(surf, *loc_r, "LOCAL", local)
    _seg_btn(surf, *zul_r, "ZULU", not local)


def mfd_strip_setup_hit(x, y):
    if _back_hit(x, y):
        return ("back", None)
    loc_r, zul_r = _mss_tz_rects()

    def _in(rc):
        return rc[0] <= x <= rc[0] + rc[2] and rc[1] <= y <= rc[1] + rc[3]
    if _in(loc_r):
        return ("eta_tz", True)
    if _in(zul_r):
        return ("eta_tz", False)
    for i, rect in enumerate(_mss_slot_rects()):
        bx, by, bw, bh = rect
        if bx <= x <= bx + bw and by <= y <= by + bh:
            return ("slot", i)
    for (kind, _c, _n, _d), rect in zip(_MFD_STRIP_AVAILABLE, _mss_grid_rects()):
        bx, by, bw, bh = rect
        if bx <= x <= bx + bw and by <= y <= by + bh:
            return ("kind", kind)
    return (None, None)


# ── FPL editor (tap the MFD FPL button) — ported verbatim from pi_zero ─────────
_FPL_HEADER_H   = 44
_FPL_ACTIONS_H  = 50
_FPL_ROW_H      = 80
_FPL_ROW_GAP    = 6
_FPL_ICON_W     = 56
_FPL_ICON_GAP   = 6
_FPL_ACTIONS_GAP = 6
_FPL_SAVELOAD_H  = 44
_FPL_DEACT_H     = 36
_fpl_scroll = 0
_fpl_drag = None
_FPL_DRAG_THRESHOLD = 8
_prc_scroll = 0             # approach procedure/transition picker scroll
_prc_drag = None


def _fpl_actions_rect():
    pad = 6
    return (pad, _FPL_HEADER_H + 6, DISPLAY_W - 2 * pad, _FPL_ACTIONS_H)


def _fpl_add_buttons():
    ax, ay, aw, ah = _fpl_actions_rect()
    n = 3
    gap = _FPL_ACTIONS_GAP
    bw = (aw - (n - 1) * gap) // n
    return [(ax + i * (bw + gap), ay, bw, ah) for i in range(n)]


def _fpl_saveload_btns():
    ax, ay, aw, _ = _fpl_actions_rect()
    by = ay + _FPL_ACTIONS_H + _FPL_ACTIONS_GAP
    gap = _FPL_ACTIONS_GAP
    bw = (aw - gap) // 2
    return ((ax, by, bw, _FPL_SAVELOAD_H),
            (ax + bw + gap, by, aw - bw - gap, _FPL_SAVELOAD_H))


def _fpl_deact_btn_rect():
    ax, ay, aw, _ = _fpl_actions_rect()
    by = (ay + _FPL_ACTIONS_H + _FPL_ACTIONS_GAP
          + _FPL_SAVELOAD_H + _FPL_ACTIONS_GAP)
    return (ax, by, aw, _FPL_DEACT_H)


def _fpl_dest_approach_ident():
    """Final FPL waypoint's ident if an approach can be loaded to it — either a
    published CIFP approach (preferred) or runway data for a synthetic one —
    else ''."""
    wps = disp.get("fpl", {}).get("waypoints", [])
    if not wps:
        return ""
    ident = (wps[-1].get("ident") or "").upper()
    if ident and (_appr_published(ident) or _ident_has_runways(ident)):
        return ident
    return ""


def _fpl_deact_row_rects():
    """(load_appr_rect | None, deact_rect).  When the FPL destination can take
    a synthetic approach, the bottom row splits into LOAD APPR | DEACTIVATE;
    otherwise DEACTIVATE spans the full width."""
    ax, ay, aw, _ = _fpl_actions_rect()
    by = (ay + _FPL_ACTIONS_H + _FPL_ACTIONS_GAP
          + _FPL_SAVELOAD_H + _FPL_ACTIONS_GAP)
    if _fpl_dest_approach_ident():
        gap = _FPL_ACTIONS_GAP
        bw = (aw - gap) // 2
        return ((ax, by, bw, _FPL_DEACT_H),
                (ax + bw + gap, by, aw - bw - gap, _FPL_DEACT_H))
    return (None, (ax, by, aw, _FPL_DEACT_H))


def _fpl_list_y0():
    return (_FPL_HEADER_H + 6 + _FPL_ACTIONS_H + _FPL_ACTIONS_GAP
            + _FPL_SAVELOAD_H + _FPL_ACTIONS_GAP + _FPL_DEACT_H + 8)


def _fpl_row_rect(idx):
    y0 = _fpl_list_y0() - _fpl_scroll
    pad = 6
    return (pad, y0 + idx * (_FPL_ROW_H + _FPL_ROW_GAP),
            DISPLAY_W - 2 * pad, _FPL_ROW_H)


def _appr_section_h():
    """Extra scroll height for the loaded-approach section under the plan."""
    ap = disp.get("approach") or {}
    if not ap.get("loaded"):
        return 0
    legs = ap.get("legs") or []
    return (22 + len(legs) * 31 + 8) if legs else 42


def _fpl_max_scroll(n_rows):
    visible_h = DISPLAY_H - _fpl_list_y0() - 6
    content_h = n_rows * (_FPL_ROW_H + _FPL_ROW_GAP) - _FPL_ROW_GAP
    content_h += _appr_section_h()
    return max(0, content_h - visible_h)


def _fpl_list_area_y():
    return _fpl_list_y0(), DISPLAY_H - 6


def _fpl_row_icon_rects(rect):
    bx, by, bw, bh = rect
    iy = by + (bh - _FPL_ICON_W) // 2
    ih = _FPL_ICON_W
    del_x  = bx + bw - 6 - _FPL_ICON_W
    down_x = del_x  - _FPL_ICON_GAP - _FPL_ICON_W
    up_x   = down_x - _FPL_ICON_GAP - _FPL_ICON_W
    return ((up_x, iy, _FPL_ICON_W, ih),
            (down_x, iy, _FPL_ICON_W, ih),
            (del_x, iy, _FPL_ICON_W, ih))


def _fpl_icon_btn(surf, rect, glyph, dim=False):
    bx, by, bw, bh = rect
    bg = (8, 18, 32) if not dim else (4, 8, 14)
    oc = (80, 100, 130) if not dim else (40, 50, 70)
    tc = WHITE if not dim else (90, 100, 120)
    pygame.draw.rect(surf, bg, rect, border_radius=5)
    pygame.draw.rect(surf, oc, rect, width=1, border_radius=5)
    _text(surf, glyph, 34, tc, bold=True, cx=bx + bw // 2, cy=by + bh // 2 + 1)


def draw_fpl(surf):
    _screen_header(surf, "FLIGHT PLAN")
    wps = disp.get("fpl", {}).get("waypoints", [])
    active_idx = disp.get("fpl", {}).get("active_idx", -1)
    is_active  = 0 <= active_idx < len(wps)

    full = len(wps) >= _FPL_MAX_WAYPOINTS
    add_style = "ok" if not full else "normal"
    add_rects = _fpl_add_buttons()
    labels  = ("+ ICAO", "+ LAT/LON", "+ USER")
    styles  = (add_style if not full else "normal",
               add_style if not full else "normal", "ok")
    if full:
        labels = ("FULL", "FULL", "+ USER")
    for (ax, ay, aw, ah), lbl, st in zip(add_rects, labels, styles):
        _action_btn(surf, ax, ay, aw, ah, lbl, st, r=6)

    save_r, load_r = _fpl_saveload_btns()
    n_saved = len(disp.get("fpl_saved", {}).get("plans", []))
    if wps:
        _action_btn(surf, *save_r, "SAVE", "ok", r=6)
    else:
        sx, sy, sw, sh = save_r
        pygame.draw.rect(surf, (10, 14, 22), save_r, border_radius=6)
        pygame.draw.rect(surf, (40, 48, 62), save_r, width=1, border_radius=6)
        _text(surf, "SAVE", 15, (80, 90, 110), bold=True,
              cx=sx + sw // 2, cy=sy + sh // 2)
    if n_saved:
        _action_btn(surf, *load_r, f"LOAD ({n_saved})", "normal", r=6)
    else:
        lx, ly, lw, lh = load_r
        pygame.draw.rect(surf, (10, 14, 22), load_r, border_radius=6)
        pygame.draw.rect(surf, (40, 48, 62), load_r, width=1, border_radius=6)
        _text(surf, "LOAD", 15, (80, 90, 110), bold=True,
              cx=lx + lw // 2, cy=ly + lh // 2)

    appr_r, (dx, dy, dw, dh) = _fpl_deact_row_rects()
    if appr_r is not None:
        _ap = disp.get("approach") or {}
        _rwy = _ap.get("runway", "")
        _phase = _approach_phase()
        if _phase == "active":
            _action_btn(surf, *appr_r, f"CANCEL APPR {_rwy}", "danger", r=6)
        elif _phase == "missed":
            _action_btn(surf, *appr_r, f"END MISSED {_rwy}", "danger", r=6)
        elif _phase == "armed":
            _action_btn(surf, *appr_r, f"ACTIVATE {_rwy}", "warn", r=6)
        else:
            _action_btn(surf, *appr_r, "LOAD APPR", "ok", r=6)
    if is_active:
        _action_btn(surf, dx, dy, dw, dh, "DEACTIVATE", "warn", r=6)
    else:
        pygame.draw.rect(surf, (10, 14, 22), (dx, dy, dw, dh), border_radius=6)
        pygame.draw.rect(surf, (40, 48, 62), (dx, dy, dw, dh), width=1,
                         border_radius=6)
        _text(surf, "DEACTIVATE", 14, (80, 90, 110), bold=True,
              cx=dx + dw // 2, cy=dy + dh // 2)

    if not wps:
        _text(surf, "No waypoints yet — tap + ICAO / + LAT/LON / + USER",
              14, (140, 160, 190), cx=DISPLAY_W // 2, cy=_fpl_list_y0() + 60)
        _text(surf, "Each ident becomes a leg.  Tap a row to activate it.",
              11, (110, 130, 160), cx=DISPLAY_W // 2, cy=_fpl_list_y0() + 90)
        return

    global _fpl_scroll
    list_top, list_bot = _fpl_list_area_y()
    _fpl_scroll = max(0, min(_fpl_scroll, _fpl_max_scroll(len(wps))))
    prev_clip = surf.get_clip()
    surf.set_clip(pygame.Rect(0, list_top, DISPLAY_W, list_bot - list_top))

    for i, wp in enumerate(wps):
        bx, by, bw, bh = _fpl_row_rect(i)
        if by + bh < list_top or by > list_bot:
            continue
        is_this_active = (i == active_idx)
        bg = (0, 40, 18) if is_this_active else (0, 12, 32)
        oc = (60, 200, 90) if is_this_active else (60, 80, 110)
        pygame.draw.rect(surf, bg, (bx, by, bw, bh), border_radius=5)
        pygame.draw.rect(surf, oc, (bx, by, bw, bh),
                         width=2 if is_this_active else 1, border_radius=5)
        _text(surf, f"{i+1}", 20, (160, 180, 200), bold=True,
              x=bx + 14, cy=by + bh // 2)
        ident_col = MAGENTA if is_this_active else WHITE
        _text(surf, wp.get("ident", ""), 32, ident_col, bold=True,
              x=bx + 56, y=by + 10)
        wp_name   = str(wp.get("name", "") or "")
        wp_region = str(wp.get("region", "") or "")
        wp_user   = bool(wp.get("user"))
        if wp_user:
            sub = f"USER  ·  {wp['lat']:.4f}, {wp['lon']:.4f}"
            sub_col = (200, 180, 130)
        elif wp_name:
            sub = f"{wp_name}, {wp_region}" if wp_region else wp_name
            sub_col = (150, 175, 205)
        else:
            sub = f"{wp['lat']:.4f}, {wp['lon']:.4f}"
            sub_col = (130, 150, 180)
        # Leg distance — from the previous waypoint (from the aircraft for the
        # first leg) — shown at the row's bottom-right, before the icons.
        # Leg course (true) + distance, e.g. "123°T  4.5 nm".  No magvar model
        # in the codebase, so all courses here are true — same as the stored
        # approach course_deg.
        if i == 0:
            if disp.get('gps_ok') and disp.get('lat') is not None:
                _ld, _lb = _nav_geo_dist_brg(disp['lat'], disp['lon'],
                                             wp['lat'], wp['lon'])
                leg_txt = f"{int(round(_lb)) % 360:03d}°T  {_ld:.1f} nm"
            else:
                leg_txt = ""
        else:
            _pv = wps[i - 1]
            _ld, _lb = _nav_geo_dist_brg(_pv['lat'], _pv['lon'],
                                         wp['lat'], wp['lon'])
            leg_txt = f"{int(round(_lb)) % 360:03d}°T  {_ld:.1f} nm"
        dist_right = bx + bw - 3 * _FPL_ICON_W - 2 * _FPL_ICON_GAP - 14
        dist_w = _get_font(15, bold=True).size(leg_txt)[0] if leg_txt else 0
        if leg_txt:
            _text(surf, leg_txt, 15, (90, 205, 225), bold=True,
                  x=dist_right - dist_w, y=by + bh - 26)
        max_sub_x = dist_right - dist_w - 14
        sub_font = _get_font(18, bold=False)
        while sub and sub_font.size(sub)[0] > (max_sub_x - (bx + 56)):
            sub = sub[:-1]
        _text(surf, sub, 18, sub_col, x=bx + 56, y=by + bh - 26)
        if is_this_active:
            _text(surf, "● ACTIVE", 15, (60, 220, 100), bold=True,
                  x=bx + bw - 3 * _FPL_ICON_W - 2 * _FPL_ICON_GAP - 108,
                  y=by + 12)
        up_r, dn_r, del_r = _fpl_row_icon_rects((bx, by, bw, bh))
        _fpl_icon_btn(surf, up_r, "↑", dim=(i == 0))
        _fpl_icon_btn(surf, dn_r, "↓", dim=(i == len(wps) - 1))
        _fpl_icon_btn(surf, del_r, "✕")

    # Loaded approach — an indented read-only section under the destination: one
    # row per leg (with leg distances) for a published approach, or a single
    # placeholder line for a synthetic one.
    _ap = disp.get("approach") or {}
    _phase = _approach_phase()
    _legs = _ap.get("legs") or []
    if _phase != "none" and wps:
        col = {"armed": (225, 185, 80), "active": (60, 220, 100),
               "missed": (240, 140, 60)}.get(_phase, (200, 200, 200))
        tag = {"armed": "ARMED · tap ACTIVATE", "active": "ACTIVE",
               "missed": "MISSED"}.get(_phase, "")
        lbx, lby, lbw, lbh = _fpl_row_rect(len(wps) - 1)
        hy = lby + lbh + 6
        if _legs:
            if list_top <= hy <= list_bot:
                _text(surf, f"APPROACH · {_approach_label()} · {tag}", 13, col,
                      bold=True, x=lbx + 40, y=hy)
            leg_idx = int(_ap.get("leg_idx", -1)) if _phase == "active" else -1
            prev_la, prev_lo = wps[-1]["lat"], wps[-1]["lon"]
            rh = 28
            for k, (la, lo, ident, _lt, _alt, _at) in enumerate(_legs):
                ry = hy + 22 + k * (rh + 3)
                ld = _nav_geo_dist_brg(prev_la, prev_lo, la, lo)[0]
                prev_la, prev_lo = la, lo
                if ry > list_bot:
                    _text(surf, f"+{len(_legs) - k} more", 11, col,
                          x=lbx + 56, y=ry - 2)
                    break
                if ry + rh < list_top:
                    continue
                on_leg = (_phase == "active" and k == leg_idx)
                rc = pygame.Rect(lbx + 40, ry, lbw - 40, rh)
                pygame.draw.rect(surf, (0, 36, 18) if on_leg else (0, 14, 28),
                                 rc, border_radius=4)
                pygame.draw.rect(surf, col if on_leg else (50, 70, 95), rc,
                                 width=2 if on_leg else 1, border_radius=4)
                _text(surf, f"↳ {ident}", 16, (90, 240, 130) if on_leg else WHITE,
                      bold=True, x=rc.x + 14, cy=rc.centery)
                alt_lbl = _appr_alt_label(_alt, _at)
                if alt_lbl:
                    _text(surf, alt_lbl, 15, (210, 200, 120), bold=True,
                          x=rc.right - 210, cy=rc.centery)
                _text(surf, f"{ld:.1f} nm", 14, (90, 205, 225),
                      x=rc.right - 92, cy=rc.centery)
        elif list_top <= hy <= list_bot:
            ph = 36
            pygame.draw.rect(surf, (0, 14, 28), (lbx + 40, hy, lbw - 40, ph),
                             border_radius=5)
            pygame.draw.rect(surf, col, (lbx + 40, hy, lbw - 40, ph), width=1,
                             border_radius=5)
            _text(surf, f"↳  {_approach_label()}", 17, col,
                  bold=True, x=lbx + 58, cy=hy + ph // 2)
            _text(surf, tag, 14, col, x=lbx + lbw - 200, cy=hy + ph // 2)

    surf.set_clip(prev_clip)
    max_s = _fpl_max_scroll(len(wps))
    if max_s > 0:
        bar_w = 4
        bar_x = DISPLAY_W - bar_w - 2
        track_h = list_bot - list_top
        thumb_h = max(20, int(track_h * track_h / (track_h + max_s)))
        thumb_y = list_top + int((track_h - thumb_h) * _fpl_scroll / max_s)
        pygame.draw.rect(surf, (40, 50, 70),
                         (bar_x, list_top, bar_w, track_h), border_radius=2)
        pygame.draw.rect(surf, (120, 150, 190),
                         (bar_x, thumb_y, bar_w, thumb_h), border_radius=2)


def fpl_hit(x, y):
    if _back_hit(x, y):
        return ("back", None)
    for rect, kind in zip(_fpl_add_buttons(), ("add_icao", "add_ll", "add_lib")):
        ax, ay, aw, ah = rect
        if ax <= x <= ax + aw and ay <= y <= ay + ah:
            return (kind, None)
    save_r, load_r = _fpl_saveload_btns()
    for rect, kind in ((save_r, "save"), (load_r, "load")):
        ax, ay, aw, ah = rect
        if ax <= x <= ax + aw and ay <= y <= ay + ah:
            return (kind, None)
    appr_r, deact_r = _fpl_deact_row_rects()
    if appr_r is not None:
        ax2, ay2, aw2, ah2 = appr_r
        if ax2 <= x <= ax2 + aw2 and ay2 <= y <= ay2 + ah2:
            return ("load_appr", None)
    dx, dy, dw, dh = deact_r
    if dx <= x <= dx + dw and dy <= y <= dy + dh:
        return ("deact", None)
    wps = disp.get("fpl", {}).get("waypoints", [])
    for i in range(len(wps)):
        bx, by, bw, bh = _fpl_row_rect(i)
        if not (bx <= x <= bx + bw and by <= y <= by + bh):
            continue
        up_r, dn_r, del_r = _fpl_row_icon_rects((bx, by, bw, bh))
        rx, ry, rw, rh = up_r
        if rx <= x <= rx + rw and ry <= y <= ry + rh:
            return ("up", i)
        rx, ry, rw, rh = dn_r
        if rx <= x <= rx + rw and ry <= y <= ry + rh:
            return ("down", i)
        rx, ry, rw, rh = del_r
        if rx <= x <= rx + rw and ry <= y <= ry + rh:
            return ("delete", i)
        return ("legmenu", i)
    # Approach-section leg rows (read-only display, but tappable to fly).
    ap = disp.get("approach") or {}
    legs = ap.get("legs") or []
    if wps and legs and _approach_phase() != "none":
        lbx, lby, lbw, lbh = _fpl_row_rect(len(wps) - 1)
        hy = lby + lbh + 6
        rh = 28
        for k in range(len(legs)):
            ry = hy + 22 + k * (rh + 3)
            if lbx + 40 <= x <= lbx + lbw and ry <= y <= ry + rh:
                return ("appr_legmenu", k)
    return (None, None)


def _fpl_open_add_keyboard():
    disp["kbd_target"] = "fpl_ident"
    disp["kbd_prev"]   = "fpl"
    disp["kbd_buf"]    = ""
    disp["kbd_error"]  = ""
    disp["kbd_shift"]  = False
    disp["mode"]       = "keyboard"


# ── +LAT/LON entry screen ─────────────────────────────────────────────────────
_FLE_HEADER_H  = 44
_FLE_ROW_H     = 60
_FLE_ROW_GAP   = 10
_FLE_FOOTER_H  = 56


def _fle_field_rect(i):
    pad = 6
    y0 = _FLE_HEADER_H + 12
    return (pad, y0 + i * (_FLE_ROW_H + _FLE_ROW_GAP),
            DISPLAY_W - 2 * pad, _FLE_ROW_H)


def _fle_footer_rects():
    pad = 6
    fy = DISPLAY_H - _FLE_FOOTER_H - pad
    half = (DISPLAY_W - 2 * pad - _FPL_ACTIONS_GAP) // 2
    return ((pad, fy, half, _FLE_FOOTER_H),
            (pad + half + _FPL_ACTIONS_GAP, fy, half, _FLE_FOOTER_H))


def _fle_open_kbd(target_axis):
    n = disp["fpl_new"]
    if target_axis == "ident":
        disp["kbd_target"] = "fpl_latlon_ident"; disp["kbd_buf"] = n.get("ident", "")
    elif target_axis == "lat":
        disp["kbd_target"] = "fpl_latlon_lat"; disp["kbd_buf"] = n.get("lat_str", "")
    elif target_axis == "lon":
        disp["kbd_target"] = "fpl_latlon_lon"; disp["kbd_buf"] = n.get("lon_str", "")
    else:
        return
    disp["kbd_prev"] = "fpl_latlon_entry"
    disp["kbd_error"] = ""
    disp["kbd_shift"] = False
    disp["mode"] = "keyboard"


def draw_fpl_latlon_entry(surf):
    _screen_header(surf, "ADD USER WAYPOINT")
    n = disp["fpl_new"]
    fields = [
        ("IDENT", "ident",   n.get("ident", ""),   "name (e.g. FISH, RDV1)"),
        ("LAT",   "lat_str", n.get("lat_str", ""), "decimal degrees, e.g. 34.523"),
        ("LON",   "lon_str", n.get("lon_str", ""), "decimal degrees, e.g. -111.789"),
    ]
    err_field, err_msg = disp.get("fle_err_field", ""), disp.get("fle_err_msg", "")
    for i, (label, key, val, hint) in enumerate(fields):
        bx, by, bw, bh = _fle_field_rect(i)
        is_err = (err_field and (
            (err_field == "ident" and key == "ident")
            or (err_field == "lat" and key == "lat_str")
            or (err_field == "lon" and key == "lon_str")))
        bg = (28, 14, 14) if is_err else (0, 12, 32)
        oc = (200, 80, 80) if is_err else (60, 80, 110)
        pygame.draw.rect(surf, bg, (bx, by, bw, bh), border_radius=6)
        pygame.draw.rect(surf, oc, (bx, by, bw, bh), width=1, border_radius=6)
        _text(surf, label, 12, (160, 180, 210), bold=True, x=bx + 14, y=by + 8)
        if val:
            _text(surf, val, 22, WHITE, bold=True, x=bx + 14, cy=by + bh // 2 + 8)
        else:
            _text(surf, hint, 12, (100, 110, 130), x=bx + 14, cy=by + bh // 2 + 8)
        _text(surf, "edit ›", 11, (130, 150, 180), x=bx + bw - 60, cy=by + bh // 2)
    if err_msg:
        _text(surf, err_msg, 13, (240, 120, 120), bold=True,
              cx=DISPLAY_W // 2, y=DISPLAY_H - _FLE_FOOTER_H - 28)
    (cx_, cy_, cw_, ch_), (sx_, sy_, sw_, sh_) = _fle_footer_rects()
    _action_btn(surf, cx_, cy_, cw_, ch_, "CANCEL", "normal", r=8)
    _action_btn(surf, sx_, sy_, sw_, sh_, "SAVE", "ok", r=8)


def fpl_latlon_entry_hit(x, y):
    if _back_hit(x, y):
        return ("back", None)
    for i, axis in enumerate(("ident", "lat", "lon")):
        bx, by, bw, bh = _fle_field_rect(i)
        if bx <= x <= bx + bw and by <= y <= by + bh:
            return ("edit", axis)
    (cx_, cy_, cw_, ch_), (sx_, sy_, sw_, sh_) = _fle_footer_rects()
    if cx_ <= x <= cx_ + cw_ and cy_ <= y <= cy_ + ch_:
        return ("cancel", None)
    if sx_ <= x <= sx_ + sw_ and sy_ <= y <= sy_ + sh_:
        return ("save", None)
    return (None, None)


def _fpl_validate_user_ident(ident):
    ident = (ident or "").strip().upper()
    if not ident:
        return ("ident", "ident is required")
    for i, wp in enumerate(disp["fpl"]["waypoints"]):
        if str(wp.get("ident", "")).upper() == ident:
            return ("ident", f"'{ident}' already in plan (row {i+1})")
    if _nav_lookup_ident(ident) is not None:
        return ("ident", f"'{ident}' is an airport ident — pick another")
    return ("", "")


def _fpl_open_latlon_entry():
    cur_lat = float(disp.get("lat", 0.0))
    cur_lon = float(disp.get("lon", 0.0))
    disp["fpl_new"]["ident"]   = ""
    disp["fpl_new"]["lat"]     = cur_lat
    disp["fpl_new"]["lon"]     = cur_lon
    disp["fpl_new"]["lat_str"] = f"{cur_lat:.5f}"
    disp["fpl_new"]["lon_str"] = f"{cur_lon:.5f}"
    disp["fpl_new"]["source"]  = "latlon"
    disp["mode"]               = "fpl_latlon_entry"


def _fpl_parse_latlon(s, axis):
    s = s.strip()
    if not s:
        return (None, "empty")
    try:
        v = float(s)
    except ValueError:
        return (None, f"can't parse '{s}'")
    if axis == "lat" and not (-90.0 <= v <= 90.0):
        return (None, "lat out of range")
    if axis == "lon" and not (-180.0 <= v <= 180.0):
        return (None, "lon out of range")
    return (v, "")


def _fpl_commit_latlon():
    n = disp["fpl_new"]
    ident = n["ident"].strip().upper()
    field, msg = _fpl_validate_user_ident(ident)
    if field:
        return (field, msg)
    lat, err = _fpl_parse_latlon(n["lat_str"], "lat")
    if lat is None:
        return ("lat", err)
    lon, err = _fpl_parse_latlon(n["lon_str"], "lon")
    if lon is None:
        return ("lon", err)
    if not _fpl_add_waypoint(ident, lat, lon, elev_ft=0.0, user=True):
        return ("ident", f"plan full ({_FPL_MAX_WAYPOINTS} max)")
    _user_wpt_save(ident, lat, lon)
    n["ident"] = ""; n["lat"] = 0.0; n["lon"] = 0.0
    n["lat_str"] = ""; n["lon_str"] = ""; n["source"] = ""
    return ("", "")


# ── User-waypoint picker (+ USER) ─────────────────────────────────────────────
_UWP_HEADER_H  = 44
_UWP_ROW_H     = 50
_UWP_ROW_GAP   = 4
_UWP_ICON_W    = 56


def _uwp_row_rect(idx):
    pad = 6
    y0 = _UWP_HEADER_H + 8
    return (pad, y0 + idx * (_UWP_ROW_H + _UWP_ROW_GAP),
            DISPLAY_W - 2 * pad, _UWP_ROW_H)


def _uwp_row_btn_rects(rect):
    bx, by, bw, bh = rect
    iy = by + (bh - _UWP_ICON_W) // 2 + 4
    ih = _UWP_ICON_W - 8
    del_x = bx + bw - 6 - _UWP_ICON_W
    add_x = del_x - 4 - _UWP_ICON_W
    return ((add_x, iy, _UWP_ICON_W, ih), (del_x, iy, _UWP_ICON_W, ih))


def draw_user_wpt_picker(surf):
    _screen_header(surf, "USER WAYPOINTS")
    wps = disp.get("user_wpts", {}).get("list", [])
    if not wps:
        _text(surf, "No saved user waypoints yet.", 14, (160, 180, 210),
              cx=DISPLAY_W // 2, cy=120)
        _text(surf, "Waypoints created via + LAT/LON are auto-saved here.",
              11, (130, 150, 180), cx=DISPLAY_W // 2, cy=160)
        return
    sorted_wps = sorted(wps, key=lambda w: str(w.get("ident", "")))
    fpl_idents = {str(w.get("ident", "")).upper()
                  for w in disp.get("fpl", {}).get("waypoints", [])}
    plan_full = (len(disp.get("fpl", {}).get("waypoints", [])) >= _FPL_MAX_WAYPOINTS)
    for i, wp in enumerate(sorted_wps):
        bx, by, bw, bh = _uwp_row_rect(i)
        if by + bh > DISPLAY_H - 6:
            break
        in_fpl = str(wp.get("ident", "")).upper() in fpl_idents
        bg = (0, 12, 32) if not in_fpl else (10, 26, 16)
        oc = (60, 80, 110) if not in_fpl else (90, 160, 110)
        pygame.draw.rect(surf, bg, (bx, by, bw, bh), border_radius=5)
        pygame.draw.rect(surf, oc, (bx, by, bw, bh), width=1, border_radius=5)
        _text(surf, str(wp.get("ident", "")), 22, MAGENTA, bold=True,
              x=bx + 14, y=by + 6)
        sub = f"{float(wp.get('lat', 0)):.4f}, {float(wp.get('lon', 0)):.4f}"
        _text(surf, sub, 12, (160, 180, 210), x=bx + 14, y=by + bh - 18)
        if in_fpl:
            _text(surf, "● in plan", 11, (90, 200, 130), bold=True,
                  x=bx + 200, cy=by + bh // 2)
        add_r, del_r = _uwp_row_btn_rects((bx, by, bw, bh))
        if in_fpl or plan_full:
            pygame.draw.rect(surf, (10, 14, 22), add_r, border_radius=4)
            pygame.draw.rect(surf, (40, 50, 64), add_r, width=1, border_radius=4)
            _text(surf, "ADD", 13, (80, 90, 110), bold=True,
                  cx=add_r[0] + add_r[2] // 2, cy=add_r[1] + add_r[3] // 2)
        else:
            _action_btn(surf, *add_r, "ADD", "ok", r=4)
        _action_btn(surf, *del_r, "DEL", "danger", r=4)


def user_wpt_picker_hit(x, y):
    if _back_hit(x, y):
        return ("back", None)
    wps = disp.get("user_wpts", {}).get("list", [])
    sorted_wps = sorted(wps, key=lambda w: str(w.get("ident", "")))
    for i, wp in enumerate(sorted_wps):
        bx, by, bw, bh = _uwp_row_rect(i)
        if not (bx <= x <= bx + bw and by <= y <= by + bh):
            continue
        add_r, del_r = _uwp_row_btn_rects((bx, by, bw, bh))
        ax, ay, aw, ah = add_r
        if ax <= x <= ax + aw and ay <= y <= ay + ah:
            return ("add", wp)
        dx, dy, dw, dh = del_r
        if dx <= x <= dx + dw and dy <= y <= dy + dh:
            return ("delete", wp)
    return (None, None)


def draw_fpl_plan_picker(surf):
    _screen_header(surf, "LOAD PLAN")
    plans = disp.get("fpl_saved", {}).get("plans", [])
    if not plans:
        _text(surf, "No saved plans yet.", 14, (160, 180, 210),
              cx=DISPLAY_W // 2, cy=120)
        _text(surf, "Build a plan, then tap SAVE to store it here.",
              11, (130, 150, 180), cx=DISPLAY_W // 2, cy=160)
        return
    sorted_plans = sorted(plans, key=lambda p: str(p.get("name", "")).upper())
    for i, p in enumerate(sorted_plans):
        bx, by, bw, bh = _uwp_row_rect(i)
        if by + bh > DISPLAY_H - 6:
            break
        pygame.draw.rect(surf, (0, 12, 32), (bx, by, bw, bh), border_radius=5)
        pygame.draw.rect(surf, (60, 80, 110), (bx, by, bw, bh), width=1,
                         border_radius=5)
        _text(surf, str(p.get("name", "")), 22, WHITE, bold=True,
              x=bx + 14, y=by + 6)
        wps = p.get("waypoints", [])
        nwp = len(wps)
        if nwp:
            sub = (f"{nwp} wpt{'s' if nwp != 1 else ''}  ·  "
                   f"{wps[0].get('ident','?')} → {wps[-1].get('ident','?')}")
        else:
            sub = "empty"
        _text(surf, sub, 12, (160, 180, 210), x=bx + 14, y=by + bh - 18)
        load_r, del_r = _uwp_row_btn_rects((bx, by, bw, bh))
        _action_btn(surf, *load_r, "LOAD", "ok", r=4)
        _action_btn(surf, *del_r, "DEL", "danger", r=4)


def fpl_plan_picker_hit(x, y):
    if _back_hit(x, y):
        return ("back", None)
    plans = disp.get("fpl_saved", {}).get("plans", [])
    sorted_plans = sorted(plans, key=lambda p: str(p.get("name", "")).upper())
    for i, p in enumerate(sorted_plans):
        bx, by, bw, bh = _uwp_row_rect(i)
        if not (bx <= x <= bx + bw and by <= y <= by + bh):
            continue
        load_r, del_r = _uwp_row_btn_rects((bx, by, bw, bh))
        lx, ly, lw, lh = load_r
        if lx <= x <= lx + lw and ly <= y <= ly + lh:
            return ("load", p)
        dx, dy, dw, dh = del_r
        if dx <= x <= dx + dw and dy <= y <= dy + dh:
            return ("delete", p)
    return (None, None)


def _draw_modal_overlays(surf, airspeed_src):
    """Draw the veiled data-entry / confirmation overlays (sim controls, nav
    confirm, mag-cal, numpad, keyboard) on top of whatever backdrop the caller
    already painted.  Shared by the PFD render path and the MFD-backdrop path
    so opening the D2 keyboard from the full-screen map doesn't drag the PFD's
    3D terrain render back in behind the veil."""
    mode = disp.get("mode", "pfd")
    if mode == "sim_controls":
        draw_sim_controls(surf)

    elif mode == "nav_confirm":
        draw_nav_confirm(surf)
    elif mode == "nav_pick":
        draw_nav_pick(surf)

    elif mode == "mag_cal":
        draw_mag_cal(surf)

    elif mode == "numpad":
        _draw_veil(surf)
        target  = disp.get("numpad_target", "")
        buf     = disp.get("numpad_buf", "")
        baro_unit = disp["ds"].get("baro_unit", "inhg")
        # Build baro current value in integer entry form
        if baro_unit == "hpa":
            baro_cur  = int(round(disp["baro_hpa"]))
            baro_title = "SET BARO  (hPa)"
            baro_dec   = 0
        else:
            baro_cur  = int(round(disp["baro_hpa"] / 33.8639 * 100))  # e.g. 2992
            baro_title = "SET BARO  (in Hg)"
            baro_dec   = 2
        # Bug entries are shown (and entered) in current display unit.  Storage
        # stays canonical (kt/ft) — conversion is applied at commit time.
        spd_unit_lbl = {"kt": "kt", "mph": "mph", "kph": "kph"}.get(
            disp["ds"].get("spd_unit", "kt"), "kt")
        alt_unit_lbl = {"ft": "ft", "m": "m"}.get(
            disp["ds"].get("alt_unit", "ft"), "ft")
        spd_bug_src  = "IAS" if airspeed_src == "ias" else "GS"
        spd_bug_title = f"SET {spd_bug_src} BUG  ({spd_unit_lbl})"
        titles  = {"alt_bug":   f"SET ALTITUDE BUG  (×100 {alt_unit_lbl})",
                   "hdg_bug":   "SET HEADING BUG",
                   "trk_bug":   "SET TRACK BUG",
                   "spd_bug":   spd_bug_title,
                   "baro_hpa":  baro_title,
                   "sim_init_alt": f"SET INITIAL ALTITUDE  (×100 {alt_unit_lbl})",
                   "sim_init_hdg": "SET INITIAL HEADING",
                   "sim_init_spd": "SET INITIAL SPEED  (kt)"}
        curvals = {"alt_bug":   int(round(disp.get("alt_bug", 0)
                                          * _ALT_DISP_FACTOR())) // 100,
                   "hdg_bug":   int(disp.get("hdg_bug", 0)),
                   "trk_bug":   int(disp.get("trk_bug", 0)),
                   "spd_bug":   int(round(disp.get("spd_bug", 0)
                                          * _SPD_DISP_FACTOR())),
                   "baro_hpa":  baro_cur,
                   "sim_init_alt": int(round(disp["sim"]["init_alt"]
                                             * _ALT_DISP_FACTOR())) // 100,
                   "sim_init_hdg": int(disp["sim"]["init_hdg"]),
                   "sim_init_spd": int(disp["sim"]["init_spd"])}
        for fkey, flabel, *rest in _FP_FIELDS:
            if fkey not in titles:
                titles[fkey]  = f"SET {flabel}"
                _v = disp["fp"].get(fkey, 0)
                # Only numeric fields are numpad-editable; skip string fields (tail, actype)
                if rest and len(rest) >= 3 and rest[2] == "kbd":
                    continue
                try:
                    curvals[fkey] = int(_v)
                except (ValueError, TypeError):
                    continue
        dec = baro_dec if target == "baro_hpa" else 0
        # sim_init_alt also uses ×100 suffix like alt_bug
        sim_alt_suffix = "00" if target == "sim_init_alt" else ""
        draw_numpad(surf, titles.get(target, "ENTER VALUE"),
                    curvals.get(target, 0), buf,
                    suffix=("00" if target == "alt_bug" else sim_alt_suffix),
                    transparent=True,
                    decimal_after=dec)

    elif mode == "keyboard":
        _draw_veil(surf)
        target = disp.get("kbd_target", "")
        buf    = disp.get("kbd_buf", "")
        prev   = disp.get("kbd_prev", "flight_profile")
        if target == "nav_ident":
            # Direct-to ident lives under disp["nav"], not the connectivity
            # or flight-profile dicts.  Surface it as the placeholder so
            # the user sees what's active while typing the replacement.
            cur   = disp.get("nav", {}).get("ident", "")
            title = "WAYPOINT"
        elif prev == "connectivity_setup":
            cur   = disp["cs"].get(target, "")
            title = {"ahrs_url": "AHRS URL", "wifi_ssid": "WiFi SSID",
                     "wifi_pass": "WiFi PASSWORD"}.get(target, "ENTER TEXT")
        else:
            cur   = disp["fp"].get(target, "")
            title = next((f[1] for f in _FP_FIELDS if f[0]==target), "ENTER TEXT")
        # Live airport label while typing an ident: show the FAA Local ID
        # (P14) when the field has one, else the ICAO ident, plus the name.
        hint = ""
        if disp.get("kbd_target") in ("nav_ident", "fpl_ident") and buf.strip():
            h = _nav_lookup_ident(buf)
            if h:
                disp_id = h[6] if (len(h) > 6 and h[6]) else h[0]
                hint = f"{disp_id} — {h[4]}" if h[4] else disp_id
        draw_keyboard(surf, f"ENTER {title}", cur, buf, transparent=True,
                      error=disp.get("kbd_error", ""), hint=hint)


def render(surf, demo_mode, connected, data_stale=False):
    mode = disp.get("mode", "pfd")

    # In shared-GL composite mode, surf is an SRCALPHA overlay — pre-clear
    # it each frame so stale pixels from the previous frame don't leak
    # through transparent regions.  The GL framebuffer gets a safe clear
    # below (before any terrain render) so setup screens (which return
    # early) show on top of a blank background instead of last frame's
    # terrain.
    if _shared_gl_ctx is not None:
        surf.fill((0, 0, 0, 0))
        _shared_gl_ctx.screen.use()
        _shared_gl_ctx.viewport = (0, 0, DISPLAY_W, DISPLAY_H)
        _shared_gl_ctx.clear(0.0, 0.0, 0.0, 1.0)

    # ── Full-screen replacement screens (no PFD behind them) ─────────────────
    if mode == "setup":
        draw_setup_screen(surf); return
    if mode == "downloads_setup":
        draw_downloads_setup(surf); return
    if mode == "navdata_data":
        draw_navdata_data(surf, disp["nd"]); return
    if mode == "flight_profile":
        draw_flight_profile(surf, disp["fp"]); return
    if mode == "display_setup":
        draw_display_setup(surf, disp["ds"]); return
    if mode == "appr_proc_select":
        draw_appr_proc_select(surf); return
    if mode == "appr_trans_select":
        draw_appr_trans_select(surf); return
    if mode == "appr_preview":
        draw_appr_preview(surf); return
    if mode == "approach_select":
        draw_approach_select(surf); return
    if mode == "ahrs_setup":
        draw_ahrs_setup(surf, disp["ss"]); return
    if mode == "wifi_scan":
        draw_wifi_scan(surf, disp["cs"]); return
    if mode == "connectivity_setup":
        draw_connectivity_setup(surf, disp["cs"]); return
    if mode == "screen_sync_setup":
        draw_screen_sync_setup(surf, disp["cs"]); return
    if mode == "ahrs_firmware":
        draw_ahrs_firmware(surf); return
    if mode == "system_setup":
        draw_system_setup(surf); return
    if mode == "terrain_data":
        draw_terrain_data(surf, disp["td"]); return
    if mode == "obstacle_data":
        draw_obstacle_data(surf, disp["od"]); return
    if mode == "airport_data":
        draw_airport_data(surf, disp["ad"]); return
    if mode == "airspace_data":
        draw_airspace_data(surf); return
    if mode == "airspace_classes":
        draw_airspace_classes(surf); return
    if mode == "sim_setup":
        draw_sim_setup(surf); return
    if mode == "mfd_strip_setup":
        draw_mfd_strip_setup(surf); return
    if mode == "fpl":
        draw_fpl(surf); return
    if mode == "leg_menu":
        draw_leg_menu(surf); return
    if mode == "fpl_latlon_entry":
        draw_fpl_latlon_entry(surf); return
    if mode == "user_wpt_picker":
        draw_user_wpt_picker(surf); return
    if mode == "fpl_plan_picker":
        draw_fpl_plan_picker(surf); return

    # ── Full-screen MFD (3-finger swap) ──────────────────────────────────────
    # Pure-2D map; skip the SVT/PFD render entirely (saves the terrain pass).
    # Overlays opened *from* the MFD — the D2 keyboard, the numpad, the
    # nav-confirm modal — keep the cheap MFD as their backdrop too.  Otherwise
    # `mode` flips to "keyboard"/etc and we fall through to the full PFD/SVT
    # render, relighting the 3D terrain pass behind the veil every frame —
    # that's what made D2 entry crawl on the MFD (very slow on pi4).
    if (disp.get("display_mode", "pfd") == "mfd"
            and mode in ("pfd", "keyboard", "numpad", "nav_confirm", "nav_pick")):
        draw_mfd(surf, connected=connected, data_stale=data_stale)
        if mode != "pfd":
            surf.set_clip(None)     # MFD layers may leave a clip set
            _user_src = disp["ss"].get("airspeed_src", "gps")
            _as_src = ("ias" if _user_src == "ias" and disp.get("airdata_ok")
                       else "gps")
            _draw_modal_overlays(surf, _as_src)
        return

    # ── PFD always renders for pfd / numpad / keyboard modes ─────────────────
    # Shared-GL path already cleared surf to transparent at the top of
    # render() — skip the opaque fill here so the AI region can show
    # terrain through the compositor.
    if _shared_gl_ctx is None:
        surf.fill((0, 0, 0))

    roll    = disp["roll"]
    pitch   = disp["pitch"]
    # When the aircraft goes past vertical (loop, split-S, inverted
    # cruise) the AHRS may report |pitch| > 90°. Reflect that back into
    # ±90° and pick up the extra 180° as roll — same physical attitude,
    # but the Euler chart the AI / pitch-ladder / horizon math can draw
    # without going wonky.
    pitch, roll = normalize_attitude(pitch, roll)
    alt     = disp["alt"]
    speed   = disp["speed"]
    vspeed  = disp["vspeed"]
    ay      = disp["ay"]
    lat     = disp["lat"]
    lon     = disp["lon"]
    track   = disp["track"]
    baro_hpa = disp["baro_hpa"]
    baro_src = disp["baro_src"]
    ahrs_ok  = disp["ahrs_ok"]
    gps_ok   = disp["gps_ok"]
    gps_comm = disp.get("gps_comm", False)
    baro_ok  = disp["baro_ok"]
    sats     = disp["sats"]
    hdg_bug  = disp["hdg_bug"]
    trk_bug  = disp.get("trk_bug", 0.0)
    alt_bug  = disp["alt_bug"]

    # ── Trim (Pi4-local, additive on top of Pico's own trim) ──────────────
    # The Pico firmware now handles ENU→NED conversion, connector-orientation
    # axis remapping, and mounting flip in sensor_loop before broadcasting.
    # Sim / demo generate aircraft-frame (NED) values directly.  Either way
    # roll / pitch arrive here already in the correct NED frame.
    # Pi4 local trim (disp["ss"]["pitch_trim"] / roll_trim) adds a small
    # residual correction; typically both are 0.0.
    ss = disp["ss"]
    pitch_trim = ss.get("pitch_trim", 0.0)
    roll_trim  = ss.get("roll_trim",  0.0)
    ahrs_synthetic = demo_mode or (_sim_state is not None)

    if not ahrs_synthetic:
        pitch += pitch_trim
        roll  += roll_trim

    # ── Stale-data timeout: no link for > STALE_TIMEOUT_S → treat as AHRS fail
    if data_stale:
        ahrs_ok = False

    # ── Heading source selection ──────────────────────────────────────────────
    # The user picks a preference (mag / trk / auto); _resolve_hdg_source
    # turns that into the actual source given runtime conditions and
    # produces the label + colour that the heading box / setup show.
    hdg_pref = ss.get("hdg_src", "auto")
    # GS specifically — NEVER the user-selected airspeed source. Whether GPS
    # track is usable is a ground-motion question; passing IAS here would
    # let MS4525 noise dither the heading box between MAG and TRK on the ramp.
    use_track, hdg_label, hdg_color = _resolve_hdg_source(
        hdg_pref, gps_ok, ahrs_ok, disp.get("speed", 0.0))
    # Raw NED heading pre-magdev.  Pico firmware (sensor_loop) already applied
    # ENU→NED, connector-orientation axis mapping, and mounting flip before
    # broadcasting.  Sim / demo generate NED headings directly.  In TRK mode
    # the complementary filter blends yaw with GPS ground track; the corrected
    # yaw feeds the filter so the GPS-track component is unaffected.
    yaw_raw_oriented = disp.get("yaw_raw", disp["yaw"])
    disp["_yaw_uncal"] = yaw_raw_oriented   # used by cal wizard CAPTURE

    # Apply Pi4-local deviation table when available.  It is built from
    # yaw_raw captures, so it always starts from the true sensor reading
    # regardless of what table (if any) the Pico has applied.
    _pi4_magdev = ss.get("pi4_magdev", [])
    if not ahrs_synthetic and _pi4_magdev:
        yaw_corr = _apply_local_magdev(yaw_raw_oriented, _pi4_magdev)
    else:
        # No local table — use Pico-corrected yaw (or synthetic).
        yaw_corr = disp.get("yaw", yaw_raw_oriented)
    disp["_yaw_cal"] = yaw_corr
    if use_track:
        # Complementary filter: AHRS yaw rate propagates each frame, GPS
        # track slowly slaves the absolute reference.  Smoother than raw
        # GPS track at low speeds.
        hdg = _update_gps_heading(yaw_corr, disp["track"], gps_ok)
    else:
        global _gps_hdg, _prev_yaw_disp  # reset filter when not using TRK
        _gps_hdg = _prev_yaw_disp = None
        hdg = yaw_corr

    # ── Airspeed source selection ─────────────────────────────────────────────
    # "gps" : GPS groundspeed         → bug triangle / tape source label is magenta
    # "ias" : SDP/MS4525 airspeed     → bug triangle / tape source label is cyan
    # Effective source resolves to "ias" only when the pilot has selected it AND
    # the air-data sensor is currently fresh (airdata_ok). Auto-falls-back to GPS
    # GS otherwise so a transient MS4525/SDP3x dropout doesn't blank the tape.
    _user_src = ss.get("airspeed_src", "gps")
    if _user_src == "ias" and disp.get("airdata_ok"):
        airspeed_src = "ias"
        speed = disp.get("ias_kt", speed)
        # Display-side IAS deadband. The firmware already gates ias_kt at the
        # same threshold, but the IIR smoothing in smooth_state() leaks brief
        # firmware-side noise spikes into the display as a bouncing 2–6 kt
        # readout. Re-clamping here gives a clean steady 0 on the ramp.
        if speed < 10.0:
            speed = 0.0
    else:
        airspeed_src = "gps"

    # ── Unit conversions ──────────────────────────────────────────────────────
    ds = disp["ds"]
    spd_unit = ds.get("spd_unit", "kt")
    alt_unit = ds.get("alt_unit", "ft")
    spd_factor = {"kt": 1.0, "mph": 1.15078, "kph": 1.852}.get(spd_unit, 1.0)
    alt_factor = {"ft": 1.0, "m": 0.3048}.get(alt_unit, 1.0)

    speed_d   = speed * spd_factor
    alt_d     = alt   * alt_factor
    alt_bug_d = (alt_bug * alt_factor) if alt_bug is not None else None
    gs_bug_d  = (disp.get("spd_bug") * spd_factor) if disp.get("spd_bug") is not None else None

    # V-speeds from flight profile, converted to display unit
    fp = disp["fp"]
    vs0_d = fp.get("vs0", VS0) * spd_factor
    vs1_d = fp.get("vs1", VS1) * spd_factor
    vfe_d = fp.get("vfe", VFE) * spd_factor
    vno_d = fp.get("vno", VNO) * spd_factor
    vne_d = fp.get("vne", VNE) * spd_factor

    ai_rect = (AI_X, AI_Y, AI_W, AI_H)

    # 0a. Lazy GL SVT probe — runs once on first frame, after pygame display
    # is already initialised so KMS/DRM is safely held before EGL touches GPU.
    global _SVT_GL_AVAILABLE
    if _SVT_GL_AVAILABLE is None:
        if _gl_available is not None:
            _SVT_GL_AVAILABLE = _gl_available()
            print(f"[PFD] OpenGL SVT: {'enabled' if _SVT_GL_AVAILABLE else 'unavailable (pygame fallback)'}")
        else:
            _SVT_GL_AVAILABLE = False

    # 0. Compute terrain/obstacle alert level for this frame
    _update_terrain_alert(lat, lon, alt, speed, gps_ok,
                          track_deg=track, vsi_fpm=vspeed,
                          vso_kt=fp.get("vs0", VS0))

    # Auto-sequence the active FPL leg when within the advance
    # threshold.  Pi 4 owns this on the sim side (it has the GPS
    # source); pi_zero has its own copy for the case where the GPS
    # lives on the MFD instead.  GPS-gated so a bad fix can't blip
    # us through the plan.
    if gps_ok:
        _ap_seq = disp.get("approach") or {}
        if _ap_seq.get("active") and _ap_seq.get("published") \
                and not _ap_seq.get("missed"):
            _approach_check_advance(lat, lon)        # fly the published legs
        elif _ap_seq.get("missed") and _ap_seq.get("published") \
                and _ap_seq.get("missed_legs"):
            _approach_check_missed_advance(lat, lon)  # fly the missed procedure
        elif not (_ap_seq.get("active") or _ap_seq.get("missed")) \
                and _fpl_is_active():
            _fpl_check_advance(lat, lon)             # no approach engaged → FPL

    # 1. AI background — draw full-width so tapes are transparent over sky/ground.
    # Shared-GL composite path renders sky+terrain directly into the default
    # framebuffer instead of blitting a pygame surface; the 2D overlay here
    # leaves the AI region transparent so terrain shows through.
    #
    # Camera altitude floor: clamp the alt passed to the SVT renderer so the
    # camera never punches through terrain (the displayed altimeter tape is
    # untouched — the aircraft's *real* altitude can still go below the
    # ground in degenerate sensor states or sim error).  100 ft above the
    # interpolated SRTM elevation keeps the eye-point above the *mesh* —
    # the rendered triangulation of a coarser grid can sit a few tens of
    # feet above the get_elevation_ft sample at the same lat/lon, and at
    # the discard-square boundary the camera sees through to sky if any
    # mesh polygon clips the eye plane.  100 ft also keeps the runway in
    # frame when the aircraft is rolling on it.
    _ground_elev_ft = get_elevation_ft(SRTM_DIR, lat, lon) if gps_ok else 0.0
    alt_render = max(alt, _ground_elev_ft + 100.0)
    _full_ai = (0, 0, DISPLAY_W, HDG_Y)
    # Without a GPS fix we don't know where the aircraft is, so any SVT
    # rendering would be lying about the world.  Fall back to plain
    # blue-over-brown so attitude (pitch/roll from AHRS) is still
    # readable but the synthetic terrain is suppressed.
    # Real-time sun position drives terrain shading when enabled and a
    # GPS fix is live.  Off / no-fix → renderer falls back to its
    # built-in SE / mid-morning constants (bright daylight that always
    # reads).
    _sun_az = _sun_el = _sun_int = None
    if ds.get("sun_realtime", True) and gps_ok:
        try:
            _sun_az, _sun_el, _sun_int = _sun_mod.solar_position(lat, lon)
        except Exception:
            _sun_az = _sun_el = _sun_int = None

    # Below-horizon mesh-gap colour: same 6-band palette the GLSL
    # clearance_color() uses on the terrain mesh, driven by the SRTM
    # clearance under the aircraft (the same value the AGL readout
    # reflects).  This makes any gap blend continuously with the band
    # the surrounding mesh is painting — red where mesh is red, deep
    # orange in the 200–300 ft band, amber 300–700, brown 700–1200,
    # dark brown 1200–2200, very dark > 2200.  Bands match
    # FRAGMENT_SHADER clearance_color() exactly so the gap and the
    # nearest mesh fragment never differ by more than the band edge.
    _clr = alt - _ground_elev_ft if gps_ok else 9999.0
    # Same Garmin-style ground inhibit applied in clearance_color() — when
    # below Vso, skip the red/orange/amber bands so taxi rollout doesn't
    # paint horizon gaps red.
    _alert_on = speed >= fp.get("vs0", VS0)
    if   _alert_on and _clr < 200:  _below_col = (0.86, 0.12, 0.12)
    elif _alert_on and _clr < 300:  _below_col = (0.86, 0.31, 0.0)
    elif _alert_on and _clr < 700:  _below_col = (0.78, 0.51, 0.0)
    elif _clr < 1200: _below_col = (0.55, 0.39, 0.16)
    elif _clr < 2200: _below_col = (0.39, 0.29, 0.14)
    else:             _below_col = (0.27, 0.22, 0.11)

    # Unusual-attitude declutter: at |pitch| > 30° or |roll| > 60° the
    # SVT mesh, water mask, airport / runway / obstacle overlays all
    # come off so the pilot sees nothing but solid sky/ground + the red
    # recovery chevrons + the pitch ladder.  Faster too — no SVT pass,
    # no symbol projection.
    _extreme_att = is_extreme_attitude(pitch, roll)
    # Voice cue for extreme bank — only fires when the AHRS is trusted
    # and the sim isn't paused (otherwise the sim's frozen attitude
    # would keep the callout repeating forever).
    if (ahrs_ok and abs(roll) > EXTREME_BANK_DEG
            and not disp["sim"].get("paused", False)):
        audio_alerts.play("bank")
    if _extreme_att:
        draw_simple_ai_background(surf, _full_ai, pitch, roll)
    elif _shared_gl_ctx is not None and gps_ok:
        # Render terrain into the AI region of the default framebuffer.
        # GL viewport origin is bottom-left: pygame AI row 0..HDG_Y maps to
        # GL rows HDG_H..DISPLAY_H.
        _shared_gl_ctx.viewport = (0, HDG_H, DISPLAY_W, HDG_Y)
        # Build polyline list for depth-tested rendering on top of terrain.
        # When an approach is active, the HITS boxes replace the magenta
        # D2 trace — the boxes already convey the path in 3D and the
        # extra magenta line clutters the corridor.
        _gl_polylines = []
        _ap = disp.get("approach") or {}
        # Show the magenta direct-to trace UNLESS we're flying the final
        # centreline (where the HITS boxes carry the path).  On an
        # intermediate/activated approach leg the boxes sit at the runway, so
        # without this the leg to the active fix had no magenta AND no boxes.
        if not _approach_centerline_active():
            _trace_verts = build_direct_to_trace_vertices()
            if _trace_verts is not None and len(_trace_verts) >= 2:
                _gl_polylines.append((
                    _trace_verts,
                    (220 / 255.0, 0.0, 220 / 255.0, 1.0),
                    3.0,
                ))
        # Next FPL leg (faded magenta) — independent of the final-approach
        # centreline gate, so it still shows while flying the approach.  Needs
        # an active multi-leg flight plan; absent on a plain single-point D2.
        _next_verts = build_next_leg_trace_vertices()
        if _next_verts is not None and len(_next_verts) >= 2:
            _gl_polylines.append((_next_verts, _NEXT_LEG_COLOR, 3.0))
        # HITS boxes — cyan rectangles along the extended centreline at 3°
        # glideslope whenever an approach is active (any leg), so the corridor
        # into the runway is visible ahead while you fly the feeder legs too.
        if _ap.get("active") and disp["ds"].get("hits_enabled", True):
            _gl_polylines.extend(_approach_hits_polylines())
        _gl_polylines.extend(_approach_signpost_polylines())
        _appr_verts = build_approach_trace_vertices()
        if _appr_verts is not None and len(_appr_verts) >= 2:
            _gl_polylines.append((_appr_verts, _APPR_TRACE_COLOR, 3.0))
        render_svt_into_current_fb(
            _shared_gl_ctx, SRTM_DIR,
            DISPLAY_W, HDG_Y,
            pitch, roll, hdg, alt_render, lat, lon,
            polylines=_gl_polylines,
            water_dir=WATER_DIR,
            water_enable=disp["ad"].get("show_water", True),
            airports_arr=_airports,
            sun_az_deg=_sun_az,
            sun_el_deg=_sun_el,
            sun_intensity=_sun_int,
            below_horizon_color=_below_col,
            alert_enable=(speed >= fp.get("vs0", VS0)),
        )
        _shared_gl_ctx.viewport = (0, 0, DISPLAY_W, DISPLAY_H)
    elif _has_terrain and gps_ok:
        # Build the same HITS / direct-to polyline list the shared-GL
        # path builds above so offline preview captures (which use the
        # standalone-EGL renderer) show the cyan HITS boxes and the
        # magenta D2 trace.  The shared-GL branch only runs on the
        # live Pi 4; this branch covers everything else.
        _gl_polylines = []
        _ap = disp.get("approach") or {}
        if not _approach_centerline_active():
            _trace_verts = build_direct_to_trace_vertices()
            if _trace_verts is not None and len(_trace_verts) >= 2:
                _gl_polylines.append((
                    _trace_verts,
                    (220 / 255.0, 0.0, 220 / 255.0, 1.0),
                    3.0,
                ))
        # Next FPL leg (faded magenta) — independent of the final-approach
        # centreline gate, so it still shows while flying the approach.  Needs
        # an active multi-leg flight plan; absent on a plain single-point D2.
        _next_verts = build_next_leg_trace_vertices()
        if _next_verts is not None and len(_next_verts) >= 2:
            _gl_polylines.append((_next_verts, _NEXT_LEG_COLOR, 3.0))
        if _ap.get("active") and disp["ds"].get("hits_enabled", True):
            _gl_polylines.extend(_approach_hits_polylines())
        _gl_polylines.extend(_approach_signpost_polylines())
        _appr_verts = build_approach_trace_vertices()
        if _appr_verts is not None and len(_appr_verts) >= 2:
            _gl_polylines.append((_appr_verts, _APPR_TRACE_COLOR, 3.0))
        draw_ai_background(surf, _full_ai, pitch, roll, hdg, alt_render,
                           lat, lon, polylines=_gl_polylines)
    else:
        draw_simple_ai_background(surf, _full_ai, pitch, roll)

    # 1b. Symbol overlays on the AI — runways, airports, obstacles, and the
    # direct-to course trace.  The draw functions already project with the
    # given roll_deg (cos/sin in their per-feature math), so we pass the
    # real roll and write straight onto the main surface.  An older path
    # drew everything onto a SRCALPHA overlay rotated by pygame at the end,
    # which cost ~20 ms/frame in a turn at 1024×600 — replaced with the
    # per-feature projection-roll for that win.
    if (not _extreme_att) and gps_ok and (
            _runways is not None or _airports is not None or _obstacles is not None):
        # Roll sign: the per-feature projections rotate by (cos/sin) in math
        # convention but write to screen-Y-down pixels, which flips CW/CCW.
        # The original SRCALPHA-rotate path used pygame.transform.rotate's
        # screen convention, so we negate roll here to match it — symbols
        # and terrain bank together as the pilot expects.
        _ov_roll = -roll
        if _runways is not None:
            draw_runway_symbols(surf, _full_ai, lat, lon, alt_render, hdg, pitch, _ov_roll)
        if _airports is not None:
            draw_airport_symbols(surf, _full_ai, lat, lon, alt_render, hdg, pitch, _ov_roll)
        if _obstacles is not None:
            # Obstacles project from REAL alt, not alt_render.  The
            # camera-floor clamp pushes alt_render up to ~100 ft above
            # the SRTM-derived ground when we're near the surface, and
            # using that for obstacle math made low-AGL FAA DOF entries
            # (jetway masts, terminal cornices, lighting around major
            # airports like PHX) project below the horizon — they
            # painted "phantom" tower symbols all over the runway and
            # ramp because their real top elevation reads below the
            # clamped camera position.  Real alt restores the correct
            # geometry: a 30 ft tower 1000 ft away is above the horizon
            # by 1.7°, regardless of where the SVT camera floor sits.
            draw_obstacle_symbols(surf, _full_ai, lat, lon, alt, hdg, pitch, _ov_roll)
        # Direct-to course trace — depth-tested 3D in the shared-GL path,
        # 2D pygame fallback when no GL.  Suppressed when an approach is
        # active (HITS boxes carry the path instead).
        if _shared_gl_ctx is None and not (disp.get("approach") or {}).get("active"):
            draw_direct_to_trace(surf, _full_ai, lat, lon, alt_render, hdg, pitch, _ov_roll)

    # 1b-2. Flight-path vector marker.  Projected into the same _full_ai
    # frame as the symbol overlays (so it aligns with airports + SVT), but
    # NOT gated on _extreme_att — it clamps to a ghost arrow at the AI edge
    # rather than disappearing, which is exactly when the pilot wants it.
    if ds.get("fpv_enabled", True) and gps_ok:
        draw_fpv_marker(surf, _full_ai, hdg, pitch, -roll, speed, track, vspeed)

    # 1c. Zero-pitch reference line — always horizontal across AI at
    # screen-centre, regardless of actual horizon position.  Critical with
    # 3D SVT because high terrain shifts the visible horizon away from 0°.
    _svt_3d_active = ((SVT_RENDERER == "opengl" and _SVT_GL_AVAILABLE)
                      or _shared_gl_ctx is not None)
    if _svt_3d_active:
        draw_zero_pitch_line(surf, ai_rect, pitch, roll)

    # 1c-2. Approach-fix sign-post labels (ident + crossing altitude), upright.
    # Projected with the same -roll convention the symbol overlays use so the
    # text sits on the 3D amber box at each fix.
    if _svt_3d_active and gps_ok:
        _draw_approach_signpost_labels(surf, ai_rect, lat, lon, alt_render,
                                       hdg, pitch, -roll)

    # 1d. Lower-left moving-map inset (pure-pygame; reuses the airport,
    # runway, obstacle and SRTM caches the SVT already keeps loaded).
    # Drawn after symbols so the inset frame sits on top, before the
    # pitch ladder so the ladder reads through unobstructed.
    if ds.get("map_enabled", False) and gps_ok:
        _miw = max(130, int(AI_W * 0.28))
        _mih = max(120, int(AI_H * 0.40))
        rect = (AI_X + 6,
                AI_Y + AI_H - _mih - 6,
                _miw, _mih)
        global _last_map_rect
        _last_map_rect = rect
        d2_src = disp.get("nav") or {}
        # Tag the dict the inset receives with the current approach
        # state so moving_map.render can colour the course line cyan
        # (approach) vs magenta (regular D2).  When approach is active,
        # carry the final-approach course so the inset can draw the
        # cyan line FROM the threshold OUT along the corridor (matches
        # what the pilot sees as HITS boxes on the SVT) instead of as
        # a generic D2 line from activation point to threshold.
        d2 = dict(d2_src)
        _ap = disp.get("approach") or {}
        # Centreline (cyan) only on the final leg; intermediate D2/activated
        # legs draw the normal magenta course to the fix.
        d2["approach_active"] = _approach_centerline_active()
        if d2["approach_active"]:
            d2["approach_course_deg"] = float(_ap.get("course_deg", 0.0))
            d2["approach_final_nm"]   = _approach_hits_final_nm()
        # GPS track sticks at its last value when groundspeed drops to
        # zero (stationary on the ramp), so passing it straight to the
        # inset would freeze the rotation at whatever heading we last
        # taxied in.  Below taxi-speed (3 kt — same threshold the AUTO
        # heading source uses) suppress track and let the inset fall
        # back to mag heading so yawing the nose visibly rotates the
        # map in TRK↑ mode.
        #
        # Gate on GPS groundspeed specifically (NOT the user-selected
        # airspeed source). GS reads sub-knot when stationary, while IAS
        # has the MS4525/SDP noise floor — picking IAS here would let
        # sensor noise dither the map between TRK↑ and N↑ on the ramp.
        _gs_kt = disp.get("speed", 0.0)
        _map_track = track if _gs_kt >= 3.0 else None
        # Translate the main-PFD airport-type filters (managed on the
        # AIRPORT DATA screen) into the set of atype letters the inset
        # should draw.  Sharing the flags with the main PFD means the
        # pilot sets type filters once and both displays honour them.
        _ad = disp.get("ad", {})
        _types_vis = set()
        if _ad.get("show_public", True):
            _types_vis.update({"S", "M", "L"})
        if _ad.get("show_heli", True):
            _types_vis.add("H")
        if _ad.get("show_seaplane", False):
            _types_vis.add("W")
        if _ad.get("show_other", False):
            _types_vis.add("B")
        # Resolve the effective range and orientation. In AUTO mode the
        # inset picks the smallest standard step that fits the active
        # direct-to and forces north-up so the destination doesn't spin
        # under the chevron. If no D2 is active the fallback is 80 nm —
        # the user can still pan around at the widest standard range.
        _zoom_pref  = (_winds_zoom() if _map_overlay_state(ds) == "wnd"
                       else int(ds.get("map_zoom_nm", 5)))
        _orient_pref = ds.get("map_orient", "trk")
        if _zoom_pref == _map_mod.ZOOM_AUTO:
            _d2_dst = d2 if d2.get("ident") else None
            if _d2_dst:
                _cos_lat = max(0.05, math.cos(math.radians(lat)))
                _n_nm = (_d2_dst["lat"] - lat) * 60.0
                _e_nm = (_d2_dst["lon"] - lon) * 60.0 * _cos_lat
                _eff_range = _map_mod.auto_fit_range(
                    math.hypot(_n_nm, _e_nm) * 1.10)  # 10 % framing margin
            else:
                _eff_range = _map_mod.ZOOM_LEVELS[-1]
            _eff_label = "AUTO"
        else:
            _eff_range  = _zoom_pref
            _eff_label  = None  # use the inset's standard "X NM" label
        # Honour the pilot's TRK↑ / N↑ choice at every manual inset range.
        # The old wide-zoom force-north-up was for rotated-tint smear and a
        # supposed per-heading rebuild; both are gone now (the tint registers
        # + rotates correctly and the cache key doesn't include rotation, so a
        # heading change re-rotates the cached surface rather than rebuilding).
        # Forcing N-up at >40 nm was swallowing the toggle on the WND page
        # (winds zoom ≥40) and at wide manual zooms.  AUTO still pins north-up
        # so the destination doesn't spin under the chevron during whole-leg
        # fit.
        _eff_orient = "nrth" if _eff_label == "AUTO" else _orient_pref
        # The inset is a SECONDARY display — never let an exception in it (or in
        # the approach/FPL overlays it draws) abort the rest of the frame, which
        # would drop the primary speed/alt tapes drawn below.  Log the first
        # failure (with traceback) so the cause is captured, then carry on.
        try:
            _map_mod.render(
                surf, rect, lat, lon, alt, hdg, _map_track,
                _eff_orient,
                _eff_range,
                ds,
                airports_arr=_airports,
                runways_arr=_runways,
                obstacles_arr=_obstacles,
                srtm_dir=SRTM_DIR,
                water_dir=WATER_DIR,
                direct_to=d2 if d2.get("ident") else None,
                font=_get_font(11, bold=True),
                airport_types_visible=_types_vis,
                # GS specifically — NEVER the user-selected speed source. The map
                # is a ground-motion display, so range/ETE/track decisions must
                # come from GPS groundspeed even when the speed tape shows IAS.
                gs_kt=_gs_kt,
                vso_kt=fp.get("vs0", VS0),
                range_label=_eff_label,
                state_lines=_state_lines,
                country_lines=_country_lines,
                # Multi-leg FPL polyline.  Mirrored from the MFD over
                # screen sync (KIND_FPL) — only renders when the pilot has
                # FPL sync set to RX on this side AND the MFD is TXing.
                fpl_remaining=_fpl_render_remaining(),
                approach_path=_approach_render_path(),
                missed_path=_approach_render_missed(),
                holds=_approach_render_holds(),
                # Airspace polygons (B/C/D/MOA/R).  Loaded in the
                # background at startup from AIRSPACE_DIR/airspaces.json; per-
                # class display gates live in disp["ds"]["map_show_airspace_*"].
                airspaces=_airspaces,
                # ADS-B traffic — relativised + threat-classified each frame in
                # _update_traffic.  Clamped to nearby safety traffic on the PFD
                # inset (always a non-TFC view) so it never clutters the SVT.
                traffic=_traffic_to_draw(),
                # METAR station dots — gated by ds["map_show_metar"] (MET/OVLY).
                metars=disp.get("weather", {}).get("metars"),
                ground_stations=disp.get("weather", {}).get("stations"),
                wx_graphics=disp.get("weather", {}).get("graphics"),
                winds_barbs=(_winds_barbs(0)        # inset is always NOW
                             if (disp["ds"].get("map_show_winds")
                                 and _eff_range >= WINDS_MIN_RENDER_NM) else None),
                # NEXRAD reflectivity raster — gated by ds["map_show_nexrad"].
                nexrad=_nexrad_render_arg(),
                nexrad_cells=(_fisb_nexrad_cells()
                              if disp["ds"].get("map_show_nexrad") else None),
            )
        except Exception:
            global _inset_render_err_logged
            if not _inset_render_err_logged:
                import traceback
                print("[PFD] inset render error (tapes preserved):")
                traceback.print_exc()
                _inset_render_err_logged = True
        # OVLY label — bottom-left of the inset; tap there to cycle the
        # WX / Airspace overlay (traffic stays on).  Colour hints the state.
        _ov_state = _map_overlay_state(disp["ds"])
        _ov_col = {"tfc": (150, 160, 170), "wx": (0, 200, 0),
                   "nexrad": (0, 200, 255), "asp": (40, 120, 255),
                   "multi": (220, 200, 80)}.get(_ov_state, (150, 160, 170))
        _mx, _my, _mw, _mh = rect
        _text(surf, _map_overlay_label(disp["ds"]), 11, _ov_col, bold=True,
              x=_mx + 4, y=_my + _mh - 15)
        # Winds-altitude tap-target — a small readout just under the range
        # label when the WND overlay is up, so the level can be changed on the
        # inset without the full-screen MFD (tap it to cycle 3k/6k/9k/12k/18k).
        # Hit-test mirrors this box in the inset tap handler.
        if _ov_state == "wnd":
            _wk = int(disp["ds"].get("winds_alt_ft", 9000)) // 1000
            pygame.draw.rect(surf, (0, 20, 40), (_mx + 2, _my + 27, 52, 18),
                             border_radius=4)
            pygame.draw.rect(surf, (0, 150, 200), (_mx + 2, _my + 27, 52, 18),
                             width=1, border_radius=4)
            _text(surf, f"{_wk}k ft", 11, (120, 210, 255), bold=True,
                  x=_mx + 8, y=_my + 29)

    # 2. Pitch ladder (with roll rotation)
    draw_pitch_ladder(surf, ai_rect, pitch, roll)

    # Unusual-attitude recovery chevrons (drawn over the pitch ladder so
    # they catch the eye through the ladder lines, under the aircraft
    # symbol which goes last).
    if _extreme_att:
        draw_unusual_attitude_arrows(surf, ai_rect, pitch, roll)

    # 3. Speed tape (display unit, fp V-speeds)
    draw_speed_tape(surf, speed_d, gs_bug=gs_bug_d,
                    vs0=vs0_d, vs1=vs1_d, vfe=vfe_d, vno=vno_d, vne=vne_d,
                    airspeed_src=airspeed_src)

    # 4. Alt tape (display unit)
    draw_alt_tape(surf, alt_d, vspeed, baro_hpa, baro_src, alt_bug_d,
                  baro_ok=baro_ok)

    # 4b. AGL readout — sits in the gap between the alt tape and the
    # heading tape.  Reuses the SRTM sample we already took for the
    # camera-floor clamp, so it costs ~nothing.  Uses real (sensor)
    # alt, not the clamped alt_render, so a punched-ground state still
    # reads negative as a warning.
    draw_agl_readout(surf, alt, _ground_elev_ft, gps_ok)

    # 5. Heading tape — show the bug that matches the active source: TRK
    # mode shows the track bug, MAG mode shows the heading bug.  Setting
    # one doesn't disturb the other so flipping sources preserves both.
    active_bug = trk_bug if use_track else hdg_bug
    draw_heading_tape(surf, hdg, active_bug, track=track, yaw=disp["yaw"],
                      gps_ok=gps_ok, ahrs_ok=ahrs_ok, use_track=use_track,
                      hdg_label=hdg_label, hdg_color=hdg_color)

    # 5b. CDI — direct-to course deviation indicator above the heading box.
    # No-op when no waypoint is active.
    if gps_ok:
        draw_cdi(surf)
        draw_vdi(surf)   # vertical glideslope, only paints when approach active

    # 6. Roll arc
    draw_roll_arc(surf, roll)

    # 7. Aircraft symbol
    draw_aircraft_symbol(surf)

    # 8. Slip ball
    draw_slip_ball(surf, ay)

    # 8b. PFD top readout ribbon — drawn BEFORE the badges/banners below so any
    # annunciation (status badge, TERRAIN/PULL UP, traffic) paints over it.
    draw_pfd_top_strip(surf)

    # 9. Status badges
    draw_status_badges(surf, ahrs_ok, gps_ok, gps_comm, baro_ok, baro_src, sats, connected,
                       use_track=use_track,
                       ahrs_aligning=bool(disp.get("ahrs_aligning", False)))

    # 9b. Terrain / obstacle proximity alert banner (centre of badge strip)
    draw_terrain_alert(surf)
    # 9c. Traffic collision alert — same badge-strip location, stacked just
    # below the terrain banner when both fire.
    _draw_pfd_traffic_alert(surf)

    # 10. Failure overlays
    draw_failure_overlays(surf, ahrs_ok, gps_ok, gps_comm, baro_ok,
                          ahrs_aligning=bool(disp.get("ahrs_aligning", False)))

    # 11. Tap-buttons for heading bug, baro, and alt bug (color = data source)
    draw_tap_buttons(surf, hdg, active_bug, baro_hpa, baro_src, alt_bug,
                     use_track=use_track, baro_ok=baro_ok)

    # 12. Demo / SIM watermark.  When the simulator is running, the
    # watermark doubles as a clearly-tappable button — pilots otherwise
    # don't realise the small "SIM" text is interactive and end up
    # killing the whole PFD process to leave the simulator.
    if demo_mode:
        _text(surf, "DEMO", 14, (255, 60, 60), cx=CX, cy=CY - 20)
    elif _sim_state is not None:
        _action_btn(surf, _SIM_EXIT_X, _SIM_EXIT_Y, _SIM_EXIT_W, _SIM_EXIT_H,
                    "SIM  ✕", style="danger", r=6)

    # ── Overlay modes: veil + UI drawn on top of the live PFD backdrop ───────
    _draw_modal_overlays(surf, airspeed_src)


# ── Terrain availability (computed once at import time) ───────────────────────
def _check_terrain():
    if not os.path.isdir(SRTM_DIR):
        return False
    return any(f.endswith(".hgt") for f in os.listdir(SRTM_DIR))

_has_terrain = _check_terrain()


def _startup_load_obstacles():
    """Background thread: load obstacle cache at startup without blocking."""
    _od_load_obstacles()
    cnt = disp["od"]["records"]
    if cnt:
        print(f"[PFD] Obstacles: {cnt:,} records loaded")
    else:
        print("[PFD] Obstacles: no data on disk")


def _startup_load_airports():
    """Background thread: load airport cache at startup without blocking."""
    _ad_load_airports()
    if _airports is not None:
        print(f"[PFD] Airports: {len(_airports):,} records loaded")
    else:
        print("[PFD] Airports: no data on disk")
    # Same thread does the boundary-lines loads — small npz files, but
    # the mmap-friendly numpy load is cheap and we don't want it on the
    # render thread the first frame after boot.
    global _state_lines, _country_lines
    _state_lines   = _ne_load_cache(_SL_NPZ_NAME)
    _country_lines = _ne_load_cache(_CL_NPZ_NAME)
    if _state_lines is not None:
        print(f"[PFD] State lines: "
              f"{len(_state_lines['seg_starts']) - 1:,} polylines loaded")
    if _country_lines is not None:
        print(f"[PFD] Country lines: "
              f"{len(_country_lines['seg_starts']) - 1:,} polylines loaded")


def _startup_load_airspaces():
    """Background thread: load airspace polygons.  Falls back to the
    bundled example when no airspaces.json is on disk so the render
    path is verifiable even before the pilot builds real data."""
    global _airspaces
    loaded = asp_mod.load(AIRSPACE_DIR)
    if loaded is None:
        loaded = asp_mod.load_bundled_example()
        disp["asp"]["records"] = 0
        print(f"[PFD] Airspaces: no airspaces.json at {AIRSPACE_DIR}; "
              f"using bundled {len(loaded)}-record example")
    else:
        disp["asp"]["records"] = len(loaded)
        print(f"[PFD] Airspaces: {len(loaded)} polygons loaded")
    _airspaces = loaded


def _startup_load_navdata():
    """Background thread: load the IFR nav-data cache (fixes/navaids/proc)."""
    _nd_load()
    if _navdata is not None:
        print(f"[PFD] Nav data: cycle {disp['nd'].get('cycle') or '—'}  "
              f"{disp['nd'].get('fixes',0):,} fixes  "
              f"{disp['nd'].get('procedures',0):,} procedures")
    else:
        print(f"[PFD] Nav data: no cache at {NAVDATA_DIR}")


def _asp_reload():
    """Re-read airspaces.json from disk."""
    global _airspaces
    loaded = asp_mod.load(AIRSPACE_DIR)
    if loaded is None:
        loaded = asp_mod.load_bundled_example()
        disp["asp"]["records"] = 0
    else:
        disp["asp"]["records"] = len(loaded)
    _airspaces = loaded


def _asp_install_example():
    asp = disp["asp"]
    asp["dl_status"] = "Writing example dataset…"
    try:
        asp_mod.write_example(AIRSPACE_DIR)
        _asp_reload()
        asp["dl_status"] = f"Done ✓  example dataset ({asp['records']} polygons)"
    except Exception as exc:
        asp["dl_status"] = f"Error: {exc}"


def _asp_download_thread(sources=None, bucket_label="airspaces"):
    """Fetch each URL in `sources` (dict of filename → URL) into
    AIRSPACE_DIR, then auto-build airspaces.json from every *.geojson
    in the dir.  Two-bucket version: static (28-day chart cycle) and
    TFR (more frequent refresh) get separate download buttons + their
    own "last downloaded" dates.  Mirrors pi_zero exactly."""
    asp = disp["asp"]
    asp["downloading"] = True
    asp["dl_cancel"]   = False
    if sources is None:
        sources = getattr(asp_mod, "DOWNLOAD_SOURCES", {}) or {}
    sources = {k: v for k, v in sources.items() if v}
    if not sources:
        asp["dl_status"]   = ("No download URLs configured — see "
                              "shared/airspaces.py DOWNLOAD_SOURCES; "
                              f"or drop *.geojson into {AIRSPACE_DIR}/ "
                              "and tap BUILD")
        asp["downloading"] = False
        return
    os.makedirs(AIRSPACE_DIR, exist_ok=True)
    try:
        for i, (fname, url) in enumerate(sources.items(), start=1):
            if asp["dl_cancel"]:
                asp["dl_status"]   = "Cancelled"
                asp["downloading"] = False
                return
            label = f"[{bucket_label} {i}/{len(sources)}] {fname}"
            asp["dl_status"] = f"Fetching {label}…"
            path = os.path.join(AIRSPACE_DIR, fname)
            req = urllib.request.Request(
                url, headers={"User-Agent": "PFD-AHRS/1.0"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                total = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                with open(path + ".tmp", "wb") as out:
                    while True:
                        if asp["dl_cancel"]:
                            try: os.remove(path + ".tmp")
                            except Exception: pass
                            asp["dl_status"]   = "Cancelled"
                            asp["downloading"] = False
                            return
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        out.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            pct = int(downloaded * 100 / total)
                            asp["dl_status"] = (
                                f"{label}: {pct}%  "
                                f"({downloaded//1024} / {total//1024} KB)")
                        else:
                            asp["dl_status"] = (
                                f"{label}: {downloaded//1024} KB")
            os.replace(path + ".tmp", path)
        asp["dl_status"] = f"Building airspaces.json ({bucket_label})…"
        import sys as _sys
        _tools = os.path.abspath(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
        if _tools not in _sys.path:
            _sys.path.insert(0, _tools)
        import build_airspaces_us as _bldr
        stats = _bldr.build_from_dir(
            AIRSPACE_DIR, source_note="FAA GeoJSON (auto-downloaded)")
        _asp_reload()
        if stats.get("errors"):
            asp["dl_status"] = (f"{bucket_label} downloaded; "
                                f"build had errors: {stats['errors'][0]}")
        else:
            asp["dl_status"] = (
                f"Done ✓  {stats['records']} polygons "
                f"(B:{stats['B']} C:{stats['C']} D:{stats['D']} "
                f"MOA:{stats['MOA']} R:{stats['R']} "
                f"P:{stats.get('P',0)} TFR:{stats.get('TFR',0)})")
    except Exception as exc:
        asp["dl_status"] = f"Error: {exc}"
    finally:
        asp["downloading"] = False


def _asp_start_download(sources=None, bucket_label="airspaces"):
    threading.Thread(
        target=lambda: _asp_download_thread(sources, bucket_label),
        daemon=True, name="AirspaceDownload").start()


def _asp_bucket_mtime(filenames):
    """Latest mtime across the given filenames in AIRSPACE_DIR,
    or None if none of them exist on disk."""
    latest = None
    for fname in filenames:
        path = os.path.join(AIRSPACE_DIR, fname)
        if os.path.exists(path):
            mt = os.path.getmtime(path)
            if latest is None or mt > latest:
                latest = mt
    return latest


def _asp_format_date(mtime):
    if mtime is None:
        return "—"
    import datetime as _dt
    return _dt.datetime.fromtimestamp(mtime).strftime("%b %d")


def _asp_build_from_geojson():
    """Convert any *.geojson under AIRSPACE_DIR into airspaces.json in
    the same dir.  Background-threaded so the UI stays responsive on
    large datasets."""
    asp = disp["asp"]
    asp["dl_status"] = "Building airspaces.json from *.geojson…"

    def _worker():
        try:
            import sys as _sys
            _tools = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "..", "tools")
            _tools = os.path.abspath(_tools)
            if _tools not in _sys.path:
                _sys.path.insert(0, _tools)
            import build_airspaces_us as _bldr
            stats = _bldr.build_from_dir(
                AIRSPACE_DIR,
                source_note="FAA GeoJSON (built on pi4)")
            _asp_reload()
            if stats.get("files", 0) == 0:
                asp["dl_status"] = (f"No *.geojson in {AIRSPACE_DIR} "
                                    f"— drop FAA exports there first")
            elif stats.get("errors"):
                asp["dl_status"] = f"Done with errors: {stats['errors'][0]}"
            else:
                asp["dl_status"] = (
                    f"Done ✓  {stats['records']} polygons "
                    f"(B:{stats['B']} C:{stats['C']} D:{stats['D']} "
                    f"MOA:{stats['MOA']} R:{stats['R']})")
        except Exception as exc:
            asp["dl_status"] = f"Build failed: {exc}"

    threading.Thread(target=_worker, daemon=True,
                     name="AirspaceBuild").start()


# ── Main entry point ──────────────────────────────────────────────────────────
# ── Live guided preview capture (F9) ──────────────────────────────────────────
# fbgrab can't capture this app: on kmsdrm the GL frame goes straight to the
# DRM display plane and /dev/fb0 only mirrors the (blank) text console.  But
# the full-screen MFD and every setup/list screen are drawn entirely into the
# 2D pygame `surf` (only the PFD's 3D SVT terrain lives in the GL framebuffer),
# so saving `surf` captures those pages perfectly.  F9 walks this ordered list,
# saving the live frame to the exact manual filename and announcing the next
# target on-screen.  Navigate to each page, press F9.
_PREVIEW_CAPTURE_LIST = [
    ("preview_mfd.png",          "full-screen MFD (plan loaded)"),
    ("preview_winds.png",        "OVLY → WND"),
    ("preview_metar.png",        "OVLY → MET"),
    ("preview_traffic.png",      "OVLY → TFC, traffic in view"),
    ("preview_mfd_airspace.png", "OVLY → ASP"),
    ("preview_mfd_nexrad.png",   "OVLY → NEX"),
    ("preview_mfd_trk_up.png",   "tap N↑ → TRK↑"),
    ("preview_mfd_160.png",      "zoom out to 160 NM"),
]
_PREVIEW_CAPTURE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "previews", "pfd_gl")
_preview_cap_idx = 0
_preview_cap_pending = False     # set by F9 in handle_event, serviced in main loop
_preview_cap_msg = ""
_preview_cap_msg_t = 0.0


def _do_preview_capture(surf):
    """Save the just-rendered (toast-free) frame to the next manual filename
    and advance.  Called from the render loop AFTER render() and BEFORE the
    toast is drawn, so the saved PNG never contains the overlay."""
    global _preview_cap_idx, _preview_cap_msg, _preview_cap_msg_t
    name, _hint = _PREVIEW_CAPTURE_LIST[_preview_cap_idx
                                        % len(_PREVIEW_CAPTURE_LIST)]
    try:
        os.makedirs(_PREVIEW_CAPTURE_DIR, exist_ok=True)
        out = os.path.join(_PREVIEW_CAPTURE_DIR, name)
        pygame.image.save(surf, out)
        _preview_cap_idx += 1
        nxt = _PREVIEW_CAPTURE_LIST[_preview_cap_idx
                                    % len(_PREVIEW_CAPTURE_LIST)]
        n = len(_PREVIEW_CAPTURE_LIST)
        done = _preview_cap_idx
        if done >= n:
            _preview_cap_msg = f"Saved {name}  ({n}/{n})  ✓ all done"
        else:
            _preview_cap_msg = (f"Saved {name}  ({done}/{n})   "
                                f"next: {nxt[1]} → {nxt[0]}")
        print(f"[PFD] preview capture → {out}")
    except Exception as e:
        _preview_cap_msg = f"capture FAILED: {e}"
        print(f"[PFD] preview capture failed: {e}", file=sys.stderr)
    _preview_cap_msg_t = time.monotonic()


def _draw_capture_toast(surf, msg):
    """Small banner across the top — display feedback only, never saved."""
    w = DISPLAY_W
    bar = pygame.Surface((w, 30), pygame.SRCALPHA)
    bar.fill((10, 20, 35, 230))
    surf.blit(bar, (0, 0))
    pygame.draw.line(surf, (90, 200, 130), (0, 30), (w, 30), 1)
    f = _get_font(16, bold=True)
    t = f.render(msg, True, (150, 230, 175))
    surf.blit(t, (10, 7))


def main():
    parser = argparse.ArgumentParser(description="PFD Display")
    parser.add_argument("--demo", action="store_true",
                        help="Run Sedona demo (no Pico W needed)")
    parser.add_argument("--sim",  action="store_true",
                        help="Windowed mode for desktop testing")
    # Screenshot mode: render one frame to a PNG then exit.
    # Useful for capturing SVT terrain renders with real SRTM tiles on hardware.
    parser.add_argument("--screenshot", metavar="FILE",
                        help="Render one PFD frame to FILE (.png) and exit")
    parser.add_argument("--screenshots", metavar="DIR",
                        help="Generate a full set of preview PNGs to DIR and exit")
    parser.add_argument("--ss-lat",    type=float, default=DEMO_LAT, metavar="DEG",
                        help="Screenshot latitude  (default: Sedona)")
    parser.add_argument("--ss-lon",    type=float, default=DEMO_LON, metavar="DEG",
                        help="Screenshot longitude (default: Sedona)")
    parser.add_argument("--ss-alt",    type=float, default=DEMO_ALT, metavar="FT",
                        help="Screenshot altitude ft MSL")
    parser.add_argument("--ss-hdg",    type=float, default=DEMO_HDG, metavar="DEG",
                        help="Screenshot heading degrees")
    parser.add_argument("--ss-pitch",  type=float, default=2.0,      metavar="DEG",
                        help="Screenshot pitch degrees (nose-up positive)")
    parser.add_argument("--ss-roll",   type=float, default=0.0,      metavar="DEG",
                        help="Screenshot roll degrees (right-wing-down positive)")
    parser.add_argument("--ss-speed",  type=float, default=115.0,    metavar="KT",
                        help="Screenshot groundspeed kt")
    parser.add_argument("--ss-vspeed", type=float, default=0.0,      metavar="FPM",
                        help="Screenshot vertical speed fpm")
    parser.add_argument("--ss-mode",   default="", metavar="MODE",
                        help="Screenshot a specific screen (e.g. downloads_setup)")
    parser.add_argument("--trace-mem", action="store_true",
                        help="Enable tracemalloc; log top growing allocators "
                             "every 60 s so we can identify memory leaks.")
    args = parser.parse_args()

    # Optional memory tracing for leak hunts. Captures a baseline snapshot
    # after the first 30 s (so steady-state caches are populated), then every
    # 60 s diffs against the baseline and logs the top 10 growing allocators.
    # Tracemalloc costs roughly 2x memory overhead — only enable when chasing
    # a leak.
    global _tm_baseline
    _tm_baseline = None
    if args.trace_mem:
        import tracemalloc
        tracemalloc.start()
        print("[PFD] tracemalloc enabled — baseline will snapshot at 30 s")

    if args.sim or not FULLSCREEN:
        # Desktop / windowed mode — let SDL auto-detect the display server
        # (x11 on X.Org, wayland on Wayfire/Weston, etc.) instead of forcing
        # kmsdrm which is only correct for bare-console fullscreen.
        os.environ.pop("SDL_VIDEODRIVER", None)
        os.environ.pop("SDL_FBDEV", None)

    # Restore persisted user settings (V-speeds, units, brightness, trims,
    # airport filters, etc.).  Must run BEFORE _set_backlight so the
    # restored brightness is used.  No-op on first run (no file yet).
    if _settings.load_into(disp, SETTINGS_PATH):
        print(f"[PFD] Settings restored from {SETTINGS_PATH}")
    # Migrate legacy hdg_src values: "gps" was the old name for "trk".
    if disp["ss"].get("hdg_src") == "gps":
        disp["ss"]["hdg_src"] = "trk"
    if disp["ss"].get("hdg_src") not in ("mag", "trk", "auto"):
        disp["ss"]["hdg_src"] = "auto"
    _settings.start(disp, SETTINGS_PATH)

    # Screen-to-screen sync: start the UDP listener so we hear peers from
    # boot.  Publish/consume sets come from disp["cs"] (restored above),
    # so the user's previous toggle state is honoured at startup.
    global _screen_sync
    _screen_sync = _ssync_mod.ScreenSync(
        publish_kinds=_ssync_kinds_from_cs("publish"),
        consume_kinds=_ssync_kinds_from_cs("consume"),
        enabled=disp["cs"].get("sync_enabled", True),
        transport=disp["cs"].get("sync_transport", "auto"))
    _screen_sync.on(_ssync_mod.KIND_BUGS, _ssync_apply_bugs)
    _screen_sync.on(_ssync_mod.KIND_BARO, _ssync_apply_baro)
    _screen_sync.on(_ssync_mod.KIND_NAV,  _ssync_apply_nav)
    _screen_sync.on(_ssync_mod.KIND_FPL,  _ssync_apply_fpl)
    _screen_sync.on(_ssync_mod.KIND_APPR, _ssync_apply_approach)
    _screen_sync.on(_ssync_mod.KIND_FPLLIB, _ssync_apply_fpl_lib)
    _screen_sync.on(_ssync_mod.KIND_AHRS, _ssync_apply_ahrs)
    _screen_sync.on(_ssync_mod.KIND_GPS,  _ssync_apply_gps)
    _screen_sync.on(_ssync_mod.KIND_WINDS, _ssync_apply_winds)
    _screen_sync.on(_ssync_mod.KIND_NOTAMS, _ssync_apply_notams)
    _screen_sync.on(_ssync_mod.KIND_NOTAMCREDS, _ssync_apply_notamcreds)
    _screen_sync.start()
    print(f"[PFD] Screen sync listening on UDP {_ssync_mod.DEFAULT_PORT}"
          f" (instance {_ssync_mod.INSTANCE_ID[:8]})")

    _init_backlight()
    _set_backlight(disp["ds"].get("brightness", 8))

    # Load obstacle + airport databases in background (non-blocking)
    threading.Thread(target=_startup_load_obstacles, daemon=True,
                     name="ObstacleLoad").start()
    threading.Thread(target=_startup_load_airports, daemon=True,
                     name="AirportLoad").start()
    threading.Thread(target=_startup_load_airspaces, daemon=True,
                     name="AirspaceLoad").start()
    threading.Thread(target=_startup_load_navdata, daemon=True,
                     name="NavDataLoad").start()

    # Disable vsync so display.flip() doesn't block waiting for the display's
    # vsync signal (which was taking ~82 ms at ~12 Hz on KMS/DRM, halving FPS).
    os.environ.setdefault("SDL_RENDER_VSYNC", "0")
    os.environ.setdefault("SDL_VIDEO_KMSDRM_VSYNC", "0")

    # pygame.init() returns (n_success, n_failed).  If the video subsystem
    # silently fails (typically because something else is holding the DRM
    # master after a previous crash), every later display call errors with
    # "video system not initialized".  Detect and exit clean — without this
    # systemd would tight-loop the service for hours.
    init_ok, init_failed = pygame.init()
    if init_failed > 0 or not pygame.display.get_init():
        sys.stderr.write(
            f"[PFD] pygame.init failed (success={init_ok}, failed={init_failed}).\n"
            f"[PFD] Video subsystem is not initialised — usually means another\n"
            f"[PFD] process is holding /dev/dri/card0.  Try `sudo lsof /dev/dri/card*`\n"
            f"[PFD] or reboot to clear stuck DRM state.  Exiting.\n"
        )
        sys.exit(2)
    try:
        pygame.mouse.set_visible(False)
    except pygame.error:
        # Mouse subsystem not available under some headless drivers; ignore.
        pass

    # Audio: init AFTER pygame.init() so SDL_Init has fully come up and
    # the mixer's audio callback thread can actually pump samples.
    # Doing this before pygame.init() left the mixer in a half-alive
    # state where Sound.play() returned success but no audio reached
    # the speaker.
    audio_alerts.init()
    audio_alerts.set_enabled(disp["ds"].get("audio_enabled", True))
    audio_alerts.set_volume(disp["ds"].get("audio_volume", 8) / 10.0)

    # ── Shared-context GL composite path ─────────────────────────────────────
    # When SVT_RENDERER == "opengl_shared", pygame owns the display in
    # pygame.OPENGL mode and moderngl attaches to that context.  Terrain
    # renders directly into the default framebuffer; the 2D PFD layer is
    # drawn onto a separate SRCALPHA surface and uploaded as a GL texture
    # for alpha-blended composite each frame.  Falls back to the normal
    # pygame-display path on any setup failure.  Disabled in screenshot
    # modes since those read pixels back from `surf`, which in shared-GL
    # mode contains only the 2D overlay (no terrain).
    _use_shared_gl = False
    gl_ctx = None
    gl_compositor = None
    use_shared_req = (SVT_RENDERER == "opengl_shared"
                      and HAS_SHARED_GL
                      and render_svt_into_current_fb is not None
                      and not args.screenshot
                      and not args.screenshots)
    if use_shared_req:
        try:
            screen, gl_ctx = setup_gl_display(
                DISPLAY_W, DISPLAY_H,
                fullscreen=(not args.sim) and FULLSCREEN,
            )
            gl_compositor = Compositor(
                gl_ctx, DISPLAY_W, DISPLAY_H,
                rotate_deg=DISPLAY_ROTATE,
            )
            surf = pygame.Surface((DISPLAY_W, DISPLAY_H), pygame.SRCALPHA)
            _sw = _sh = _sx = _sy = None
            _use_shared_gl = True
            global _shared_gl_ctx, _shared_gl_compositor
            _shared_gl_ctx = gl_ctx
            _shared_gl_compositor = gl_compositor
            print("[PFD] SVT renderer: opengl_shared (pygame.OPENGL composite)")
        except Exception as e:
            print(f"[PFD] opengl_shared setup failed ({e}); falling back to pygame display")
            _use_shared_gl = False
            gl_ctx = None
            gl_compositor = None

    if not _use_shared_gl:
        if (not args.sim) and FULLSCREEN:
            if DISPLAY_ROTATE:
                # Rotated display: need explicit native-res surface + manual transform.
                info = pygame.display.Info()
                _native_w = info.current_w if info.current_w > 0 else DISPLAY_W
                _native_h = info.current_h if info.current_h > 0 else DISPLAY_H
                screen = pygame.display.set_mode(
                    (_native_w, _native_h),
                    pygame.FULLSCREEN | pygame.NOFRAME
                )
                _scale = min(_native_w / DISPLAY_W, _native_h / DISPLAY_H)
                _sw = int(DISPLAY_W * _scale)
                _sh = int(DISPLAY_H * _scale)
                _sx = (_native_w - _sw) // 2
                _sy = (_native_h - _sh) // 2
                surf = pygame.Surface((DISPLAY_W, DISPLAY_H))
            else:
                # Use SDL2's built-in logical scaling (pygame.SCALED). SDL2 scales
                # the 640×480 logical surface to the physical display size in C,
                # which is ~10× faster than pygame.transform.scale() in Python
                # (~80 ms saved per frame on Pi Zero 2W scaling to 1080p).
                # SDL_RENDER_VSYNC=0 (set above) disables vsync on the SDL_Renderer
                # that SCALED creates internally, removing the vsync-wait overhead.
                screen = pygame.display.set_mode(
                    (DISPLAY_W, DISPLAY_H),
                    pygame.FULLSCREEN | pygame.SCALED
                )
                surf = screen
                _sw = _sh = _sx = _sy = None
        else:
            screen = pygame.display.set_mode((DISPLAY_W, DISPLAY_H))
            surf = screen
            _sw = _sh = _sx = _sy = None

    def _flip():
        """Present the PFD surface to the physical display.

        In the normal (non-rotated) fullscreen path, surf IS screen and SDL2
        handles the logical→physical scaling internally via pygame.SCALED, so
        this function is just a pygame.display.flip() call.

        The rotated path (DISPLAY_ROTATE != 0) still does an explicit
        transform+scale because SDL2's logical-size API doesn't handle rotation.

        In shared-GL composite mode, terrain has already been rendered into
        the default framebuffer by render(); here we just upload the 2D
        overlay surface as a texture and draw it as a fullscreen quad.
        """
        if _use_shared_gl:
            gl_compositor.upload_and_draw(surf)
            pygame.display.flip()
            return
        if surf is not screen:
            # Rotated display — manual transform + scale
            s = pygame.transform.rotate(surf, DISPLAY_ROTATE)
            screen.fill((0, 0, 0))
            screen.blit(pygame.transform.scale(s, (_sw, _sh)), (_sx, _sy))
        pygame.display.flip()

    pygame.display.set_caption("PFD")
    clock = pygame.time.Clock()

    # ── Built-in render-loop profiler ────────────────────────────────────────
    # Set PFD_CPROFILE_SEC=20 to cProfile the first N seconds of the live loop
    # and self-print the top functions by tottime — no Ctrl-C, no second
    # command, no lost .prof file.  Default 0 = off (zero overhead).  Lets a
    # field tester find the hot draw calls with one env var.  Also dumps the
    # raw stats to /tmp/pfd.prof for offline `pstats` digging.
    _cprof = None
    _cprof_until = 0.0
    try:
        _cprof_sec = float(os.environ.get("PFD_CPROFILE_SEC", "0") or 0)
    except ValueError:
        _cprof_sec = 0.0
    if _cprof_sec > 0:
        import cProfile
        _cprof = cProfile.Profile()
        _cprof.enable()
        _cprof_until = time.monotonic() + _cprof_sec
        print(f"[CPROF] profiling first {_cprof_sec:.0f}s of the render loop…")

    # Seed state directly (bypasses IIR smoothing), render one frame, save PNG.
    # Run on the Pi with SRTM tiles installed to capture real SVT renders.
    #   python3 pfd.py --screenshot ~/ss/sedona_cruise.png
    #   python3 pfd.py --screenshot ~/ss/custom.png --ss-lat 34.87 --ss-lon -111.76 \
    #                  --ss-alt 8500 --ss-hdg 133 --ss-pitch 5 --ss-roll -18
    if args.screenshot:
        snap = {
            "lat": args.ss_lat, "lon": args.ss_lon,
            "alt": args.ss_alt, "yaw": args.ss_hdg,
            "track": args.ss_hdg, "pitch": args.ss_pitch,
            "roll": args.ss_roll, "speed": args.ss_speed,
            "vspeed": args.ss_vspeed, "ay": 0.0,
            "gps_ok": True, "baro_ok": True, "ahrs_ok": True,
            "sats": 8, "gps_alt": args.ss_alt,
            "baro_hpa": BARO_DEFAULT_HPA, "baro_src": "baro",
            "fix": True, "pitch_trim": 0.0, "roll_trim": 0.0, "yaw_trim": 0.0,
        }
        with _state_lock:
            state.update(snap)
        disp.update(snap)           # bypass IIR: seed disp directly from snap
        disp["hdg_bug"] = args.ss_hdg
        disp["alt_bug"] = args.ss_alt
        if args.ss_mode:
            disp["mode"] = args.ss_mode
            if args.ss_mode == "navdata_data":
                _nd_load()          # deterministic stats for the screenshot
            if args.ss_mode == "appr_proc_select":
                _nd_load()          # load _navdata so procedures resolve
                disp["approach"] = {"airport": "KFLG", "arm_from_fpl": True}
            if args.ss_mode == "appr_trans_select":
                _nd_load()
                disp["approach"] = {"airport": "KFLG", "arm_from_fpl": True,
                                    "pending_proc": "RNAV (GPS) RWY 03"}
            if args.ss_mode == "mfd_appr":
                _nd_load()
                disp["mode"] = "pfd"
                disp["display_mode"] = "mfd"
                disp["ds"]["mfd_enabled"] = True
                disp["ds"]["map_zoom_nm"] = 20
                for _k, _v in (("lat", 35.02), ("lon", -111.80)):
                    disp[_k] = _v
                    state[_k] = _v
                _approach_load_published("KFLG", "RNAV (GPS) RWY 03",
                                         "BANYO", activate=True)
            if args.ss_mode in ("fpl", "nav_pick", "leg_menu"):
                # Demo plan + a loaded (armed) approach, for layout checks.
                disp["fpl"]["waypoints"] = [
                    {"ident": "DRK", "lat": 34.70, "lon": -111.30, "name": "DRAKE"},
                    {"ident": "SEZ", "lat": 34.85, "lon": -111.79, "name": "SEDONA"},
                    {"ident": "KFLG", "lat": 35.14, "lon": -111.67, "name": "FLAGSTAFF PULLIAM"},
                ]
                disp["fpl"]["active_idx"] = 1
                _nd_load()
                if not _approach_load_published("KFLG", "RNAV (GPS) RWY 03",
                                                "BANYO", activate=False):
                    disp["approach"] = {"loaded": True, "active": False,
                                        "airport": "KFLG", "runway": "21",
                                        "thresh_lat": 35.13, "thresh_lon": -111.66,
                                        "thresh_elev_ft": 7000.0, "course_deg": 210.0}
                if args.ss_mode == "leg_menu":
                    disp["leg_menu"] = {"kind": "appr", "idx": 0}
            if args.ss_mode == "appr_preview":
                _nd_load()
                # Seed a real KFLG 03/21 runway so the preview exercises the
                # runway-DB marker path (not just the fallback).
                global _runways
                try:
                    import numpy as _np_rw
                    _runways = _np_rw.array([(
                        "KFLG", 6999.0, 150.0, "ASP", True, "03", "21",
                        35.129, -111.677, 7014.0, 28.0,
                        35.149, -111.666, 7014.0, 208.0)],
                        dtype=[("airport", "U7"), ("length_ft", "f4"),
                               ("width_ft", "f4"), ("surface", "U6"),
                               ("lighted", "?"), ("le_ident", "U4"),
                               ("he_ident", "U4"), ("le_lat", "f4"),
                               ("le_lon", "f4"), ("le_elev_ft", "f4"),
                               ("le_hdg", "f4"), ("he_lat", "f4"),
                               ("he_lon", "f4"), ("he_elev_ft", "f4"),
                               ("he_hdg", "f4")])
                except Exception:
                    pass
                disp["approach"] = {"airport": "KFLG", "arm_from_fpl": True}
                _approach_load_published("KFLG", "RNAV (GPS) RWY 03",
                                         "BANYO", activate=False)
        smooth_state()              # now a no-op (disp already matches state)
        render(surf, demo_mode=False, connected=True, data_stale=False)
        _flip()
        outpath = os.path.abspath(args.screenshot)
        os.makedirs(os.path.dirname(outpath) or ".", exist_ok=True)
        pygame.image.save(surf, outpath)
        print(f"[PFD] Screenshot → {outpath}")
        pygame.quit()
        return

    # ── Batch screenshots mode ────────────────────────────────────────────────
    if args.screenshots:
        outdir = os.path.abspath(args.screenshots)
        os.makedirs(outdir, exist_ok=True)

        # Load airport DB synchronously so symbols appear in preview renders
        _startup_load_airports()

        def _save(fname):
            smooth_state()
            render(surf, demo_mode=False, connected=True, data_stale=False)
            _flip()
            pygame.image.save(surf, os.path.join(outdir, fname))
            print(f"  → {fname}")

        def _seed(**kwargs):
            """Seed state + disp with a scene's flight values."""
            snap = {
                "lat": kwargs.get("lat", DEMO_LAT),
                "lon": kwargs.get("lon", DEMO_LON),
                "yaw": kwargs.get("hdg", 133),
                "track": kwargs.get("track", kwargs.get("hdg", 133)),
                "roll": kwargs.get("roll", 0),
                "pitch": kwargs.get("pitch", 2),
                "speed": kwargs.get("speed", 115),
                "alt": kwargs.get("alt", 8500),
                "vspeed": kwargs.get("vspeed", 0),
                "ay": kwargs.get("ay", 0),
                "gps_ok": kwargs.get("gps_ok", True),
                "baro_ok": kwargs.get("baro_ok", True),
                "ahrs_ok": kwargs.get("ahrs_ok", True),
                "sats": kwargs.get("sats", 8),
                "gps_alt": kwargs.get("alt", 8500),
                "baro_hpa": BARO_DEFAULT_HPA,
                "baro_src": "baro" if kwargs.get("baro_ok", True) else "gps",
                "fix": kwargs.get("gps_ok", True),
                "pitch_trim": 0.0, "roll_trim": 0.0, "yaw_trim": 0.0,
            }
            with _state_lock:
                state.update(snap)
            disp.update(snap)
            disp["hdg_bug"] = kwargs.get("hdg_bug", kwargs.get("hdg", 133))
            disp["alt_bug"] = kwargs.get("alt_bug", kwargs.get("alt", 8500))
            if "spd_bug" in kwargs:
                disp["spd_bug"] = kwargs["spd_bug"]
            else:
                disp["spd_bug"] = 0
            disp["mode"]    = "pfd"
            disp["ss"]["hdg_src"] = kwargs.get("hdg_src", "mag")

        # ── Flight scenes ─────────────────────────────────────────────────────
        _seed(roll=0,   pitch=2,  hdg=133, alt=8500, speed=115, vspeed=0)
        _save("preview_sedona_level.png")

        _seed(roll=-18, pitch=6,  hdg=145, alt=7800, speed=95,  vspeed=500,  ay=0.12)
        _save("preview_sedona_climb_turn.png")

        _seed(roll=0,   pitch=-3, hdg=200, alt=5800, speed=90,  vspeed=-700)
        _save("preview_sedona_approach.png")

        # GPS TRK heading mode
        _seed(roll=0, pitch=2, hdg=133, alt=8500, speed=115, hdg_src="trk")
        _save("preview_gps_trk_mode.png")

        # Badge states
        _seed(roll=0, pitch=0, hdg=133, alt=8500, speed=115, baro_ok=False)
        _save("preview_badges_no_data.png")

        # Expired obstacles: set od state
        _seed(roll=0, pitch=0, hdg=133, alt=8500, speed=115)
        disp["od"]["expired"] = True
        disp["od"]["records"] = 76842
        _save("preview_badges_exp_obs.png")
        disp["od"]["expired"] = False

        # ── PFD hero shot (matches root pfd_preview.png) ──────────────────────
        _seed(roll=0, pitch=2, hdg=133, alt=8500, speed=115)
        _save("pfd_preview.png")

        # ── Numpad overlays (PFD underneath) ──────────────────────────────────
        _seed(roll=0, pitch=2, hdg=133, alt=8500, speed=115)
        disp["mode"] = "numpad"
        disp["numpad_target"] = "alt_bug"
        disp["numpad_buf"]    = "85"
        _save("preview_numpad_alt.png")

        disp["numpad_target"] = "hdg_bug"
        disp["numpad_buf"]    = "133"
        _save("preview_numpad_hdg.png")

        disp["ds"]["baro_unit"] = "inhg"
        disp["numpad_target"] = "baro_hpa"
        disp["numpad_buf"]    = "2992"
        _save("preview_numpad_baro_inhg.png")

        disp["ds"]["baro_unit"] = "hpa"
        disp["numpad_target"] = "baro_hpa"
        disp["numpad_buf"]    = "1013"
        _save("preview_numpad_baro_hpa.png")
        disp["ds"]["baro_unit"] = "inhg"

        # ── Keyboard overlay ──────────────────────────────────────────────────
        disp["mode"] = "keyboard"
        disp["kbd_target"] = "tail"
        disp["kbd_buf"]    = "N12345"
        disp["kbd_prev"]   = "flight_profile"
        _save("preview_keyboard.png")

        # ── Setup screens ─────────────────────────────────────────────────────
        for screen_mode, fname in [
            ("setup",               "preview_setup_main.png"),
            ("flight_profile",      "preview_setup_flight_profile.png"),
            ("display_setup",       "preview_setup_display.png"),
            ("ahrs_setup",          "preview_setup_ahrs.png"),
            ("connectivity_setup",  "preview_setup_connectivity.png"),
            ("system_setup",        "preview_setup_system.png"),
        ]:
            disp["mode"] = screen_mode
            _save(fname)

        # AHRS setup with GPS TRK selected
        disp["ss"]["hdg_src"] = "trk"
        disp["mode"] = "ahrs_setup"
        _save("preview_setup_ahrs_gpstrk.png")
        disp["ss"]["hdg_src"] = "mag"

        # ── Terrain data screen states ────────────────────────────────────────
        disp["mode"] = "terrain_data"
        disp["td"]["downloading"] = False
        disp["td"]["dl_region"]   = ""
        disp["td"]["dl_current"]  = 0
        disp["td"]["dl_total"]    = 0
        disp["td"]["dl_status"]   = ""
        _save("preview_terrain_idle.png")

        disp["td"]["downloading"] = True
        disp["td"]["dl_region"]   = "US Southwest"
        disp["td"]["dl_current"]  = 47
        disp["td"]["dl_total"]    = 132
        disp["td"]["dl_status"]   = "Downloading N35W111.hgt\u2026"
        _save("preview_terrain_downloading.png")

        disp["td"]["downloading"] = False
        disp["td"]["dl_region"]   = ""

        # ── Obstacle data screen states ───────────────────────────────────────
        disp["mode"] = "obstacle_data"
        disp["od"]["downloading"] = False
        disp["od"]["records"]     = 0
        disp["od"]["used_mb"]     = 0.0
        disp["od"]["dl_status"]   = ""
        _save("preview_obstacle_idle.png")

        disp["od"]["records"] = 76842
        disp["od"]["used_mb"] = 19.4
        disp["od"]["dl_status"] = "Done \u2713  76,842 obstacles loaded"
        _save("preview_obstacle_loaded.png")

        disp["od"]["downloading"] = True
        disp["od"]["records"]     = 0
        disp["od"]["dl_status"]   = "Downloading\u2026 38%  (7,440 / 19,584 KB)"
        _save("preview_obstacle_downloading.png")

        disp["od"]["downloading"] = False
        disp["od"]["dl_status"]   = ""

        # ── Airport data screen states ────────────────────────────────────────
        disp["mode"] = "airport_data"
        disp["ad"]["downloading"] = False
        disp["ad"]["records"]     = 0
        disp["ad"]["used_mb"]     = 0.0
        disp["ad"]["dl_status"]   = ""
        disp["ad"]["expired"]     = False
        _save("preview_airport_idle.png")

        disp["ad"]["records"] = 72007
        disp["ad"]["used_mb"] = 12.3
        disp["ad"]["age_days"] = 5
        disp["ad"]["dl_status"] = "Done \u2713  72,007 airports loaded"
        _save("preview_airport_loaded.png")

        disp["ad"]["downloading"] = True
        disp["ad"]["records"]     = 0
        disp["ad"]["dl_status"]   = "Downloading\u2026 42%  (5,280 / 12,500 KB)"
        _save("preview_airport_downloading.png")

        disp["ad"]["downloading"] = False
        disp["ad"]["dl_status"]   = ""

        # ── Direct-to keyboard with placeholder + NEAREST extras row ─────────
        # Set up an active waypoint so the placeholder shows and the
        # NEAREST extras button has something to render.
        _seed(roll=0, pitch=2, hdg=133, alt=8500, speed=115)
        disp["nav"] = {
            "ident": "KSEZ", "lat": 34.85, "lon": -111.79, "elev_ft": 4830,
            "act_lat": 34.84, "act_lon": -111.80,
        }
        disp["kbd_target"] = "nav_ident"
        disp["kbd_prev"]   = "pfd"
        disp["kbd_buf"]    = ""
        disp["kbd_error"]  = ""
        disp["mode"]       = "keyboard"
        _save("preview_direct_to_keyboard.png")

        # Same keyboard mid-error: pilot typed an unknown ident
        disp["kbd_buf"]   = "ZZZZ"
        disp["kbd_error"] = "UNKNOWN WAYPOINT  ZZZZ"
        _save("preview_unknown_waypoint.png")
        disp["kbd_buf"]   = ""
        disp["kbd_error"] = ""

        # ── Direct-to confirmation modal (Activate Direct to KSEZ?) ──────────
        _seed(roll=0, pitch=2, hdg=133, alt=8500, speed=115)
        disp["nav_confirm_ident"] = "KSEZ"
        disp["nav_confirm_prev"]  = "pfd"
        disp["mode"]              = "nav_confirm"
        _save("preview_nav_confirm.png")

        # ── Compass calibration wizard mid-walk (step 2 = EAST) ──────────────
        # Pre-load the wizard with one captured cardinal so the modal shows
        # the "Captured NORTH." status, the step-2 NORTH→EAST transition, and
        # the four cardinal Δ slots (still zeroed before the cal commits on
        # the 4th capture).  Heading aligned near EAST so RAW reads right.
        _seed(roll=0, pitch=0, hdg=88, alt=4830, speed=0,
              vspeed=0, ay=0, gps_ok=True)
        disp["yaw"]        = 88.0
        disp["_yaw_uncal"] = 88.0
        disp["mag_cal_wiz"] = {
            "step": 1, "samples": [(0.0, 358.5)],
            "msg":  "Captured NORTH.",
            "prev": "ahrs_setup",
        }
        disp["mode"] = "mag_cal"
        _save("preview_compass_cal.png")

        # Compass cal completed — show the four cardinal Δ values
        disp["ss"]["mag_cal_deltas"] = [1.2, -0.8, 0.7, -1.5]
        disp["mag_cal_wiz"] = {
            "step": 0, "samples": [],
            "msg":  "Done — N+1.2° E-0.8° S+0.7° W-1.5°",
            "prev": "ahrs_setup",
        }
        _save("preview_compass_cal_done.png")
        disp["ss"]["mag_cal_deltas"] = [0.0] * 4

        # AGL readout — close-up of lower-right corner via a normal cruise
        # frame; the 78×42 box appears above the heading tape automatically.
        _seed(roll=0, pitch=2, hdg=133, alt=6800, speed=115, vspeed=0)
        disp["mode"] = "pfd"
        _save("preview_agl_readout.png")

        # AHRS orientation row variants — capture each of the four selections
        # so the docs can show the highlight state of each segment.
        for orient, fname in (("forward", "preview_setup_ahrs_orient_fwd.png"),
                              ("left",    "preview_setup_ahrs_orient_left.png"),
                              ("right",   "preview_setup_ahrs_orient_right.png"),
                              ("aft",     "preview_setup_ahrs_orient_aft.png")):
            disp["ss"]["orientation"] = orient
            disp["mode"] = "ahrs_setup"
            _save(fname)
        disp["ss"]["orientation"] = "right"

        # ── Terrain proximity alert scenes ────────────────────────────────────
        # Force terrain alert by seeding alert state directly.  Renders over
        # normal PFD so alerts show even without SRTM tiles loaded.
        import pfd as _pfd_self  # noqa — reference this module
        # Caution: alt slightly above a simulated terrain high point
        _seed(roll=0, pitch=-2, hdg=133, alt=5500, speed=95, vspeed=-200)
        try:
            # Poke the terrain-alert module state if accessible
            globals()['_terrain_alert_level'] = 1  # caution (amber)
            globals()['_terrain_alert_alpha'] = 1.0
        except Exception:
            pass
        _save("preview_terrain_caution.png")

        _seed(roll=0, pitch=-5, hdg=133, alt=5200, speed=95, vspeed=-400)
        try:
            globals()['_terrain_alert_level'] = 2  # warning (red flashing)
            globals()['_terrain_alert_alpha'] = 1.0
        except Exception:
            pass
        _save("preview_terrain_warning.png")

        # Reset alert state
        try:
            globals()['_terrain_alert_level'] = 0
            globals()['_terrain_alert_alpha'] = 0.0
        except Exception:
            pass

        # ── VR cascade demo (alt = 9980 — shows rolling digits mid-cascade) ──
        _seed(roll=0, pitch=0, hdg=133, alt=9980, speed=115, vspeed=0)
        _save("preview_vr_cascade.png")

        disp["mode"] = "pfd"
        print(f"\n[PFD] Batch screenshots → {outdir}")
        pygame.quit()
        return

    demo_mode  = args.demo
    demo       = DemoState() if demo_mode else None
    connected  = False
    data_stale = False
    global _link_lost_t, _multitouch_t0, _active_fingers, _multitouch_max_fingers
    global _gesture_tap_lockout

    if not demo_mode:
        global _sse_client
        # Try USB serial first — direct connection, no WiFi needed
        try:
            from serial_client import SerialClient
            _usb_port = SerialClient.find_port()
        except ImportError:
            _usb_port = None
        if _usb_port:
            _sse_client = SerialClient(_usb_port, state, _state_lock)
            _sse_client.start()
            disp["cs"]["ahrs_transport"] = "usb"
            disp["cs"]["ahrs_port"]      = _usb_port
            print(f"[PFD] AHRS via USB serial: {_usb_port}")
        else:
            _sse_client = SSEClient(SSE_URL, state, _state_lock)
            _sse_client.start()
            disp["cs"]["ahrs_transport"] = "wifi"
            disp["cs"]["ahrs_port"]      = SSE_URL
            print(f"[PFD] AHRS via WiFi SSE: {SSE_URL}")
        threading.Thread(target=_poll_wifi_status, daemon=True,
                         name="WiFiPoll").start()
        threading.Thread(target=_poll_ahrs_diag,  daemon=True,
                         name="AhrsDiag").start()
    else:
        # Seed initial state for demo
        state["alt"]   = DEMO_ALT
        state["speed"] = 115.0
        state["yaw"]   = DEMO_HDG
        state["lat"]   = DEMO_LAT
        state["lon"]   = DEMO_LON
        disp["hdg_bug"] = DEMO_HDG
        disp["alt_bug"] = DEMO_ALT
        print("[PFD] Demo mode — Sedona AZ")

    # ADS-B IN: start the GDL90/UDP listener whenever it's enabled (cheap
    # idle bind; in demo we synthesise targets instead, but a real receiver
    # on the bench still feeds the listener).
    if disp["cs"].get("adsb_enabled", True):
        global _adsb_client
        _adsb_client = _adsb.ADSBClient(
            port=int(disp["cs"].get("adsb_port", ADSB_UDP_PORT)),
            stale_s=ADSB_STALE_S)
        _adsb_client.start()
        print(f"[PFD] ADS-B listening for GDL90 on UDP {_adsb_client.port}")
        global _traffic_feed
        # In-process (loopback=False): the internet feed keeps its own target
        # table read via snapshot(), entirely separate from the GDL90/UDP radio
        # listener.  The two sources never co-mingle, so radio can't be
        # mistaken for internet and switching to RADIO clears the net picture.
        _traffic_feed = _afeed.TrafficFeed(
            pos_fn=lambda: (float(disp.get("lat", DEMO_LAT)),
                            float(disp.get("lon", DEMO_LON))),
            source=TRAFFIC_FEED_SOURCE, radius_nm=TRAFFIC_FEED_RADIUS_NM,
            interval_s=TRAFFIC_FEED_INTERVAL_S,
            loopback=False, stale_s=ADSB_STALE_S)
        _traffic_feed.start()

    # Internet weather poller (METARs), centred on the live GPS fix.
    if disp["cs"].get("wx_enabled", True):
        global _wx_client
        _wx_client = _wx.WxClient(view_fn=_wx_view, interval_s=WX_INTERVAL_S)
        _wx_client.start()
        print("[PFD] Weather poller started (METAR, follows inset zoom)")
        global _taf_client, _airsig_client
        _taf_client = _wx.AwcPoller(view_fn=_wx_view, fetch_fn=_wx.fetch_tafs,
                                    interval_s=TAF_INTERVAL_S, name="TafPoller")
        _taf_client.start()
        _airsig_client = _wx.AwcPoller(view_fn=_wx_view,
                                       fetch_fn=_wx.fetch_airsigmets,
                                       interval_s=AIRSIG_INTERVAL_S,
                                       name="AirSigPoller")
        _airsig_client.start()
        global _winds_client
        # National winds: one coarse coordinate-list grid per zone, the
        # aircraft's zone first, timestamped + disk-cached, refreshed only every
        # ~6 h (GFS cadence) and only while the WND overlay is up.
        _winds_client = _wx.WindsUSCache(
            WINDS_US_BBOX, rows=2, cols=3, spacing_nm=WINDS_US_SPACING_NM,
            disk_path=_WINDS_DISK_PATH,
            locate_fn=lambda: (float(disp.get("lat", DEMO_LAT)),
                               float(disp.get("lon", DEMO_LON))),
            hour_offset_fn=lambda: int(disp["ds"].get("winds_time_offset_h", 0)),
            model=WINDS_GFS_MODEL, max_alt_ft=WINDS_MAX_ALT_FT,
            max_age_s=WINDS_DISK_MAX_AGE_S, publish_fn=_winds_publish)
        _winds_client.start()
        print("[PFD] TAF + AIRMET/SIGMET + winds pollers started (internet)")
        # NOTAMs need an FAA API key.  Always start the poller; its fetch reads
        # the credentials live from cs (entered in Connectivity) or the env, and
        # no-ops returning [] until a key is present — so typing one in enables
        # NOTAMs without a restart.
        global _notam_client
        _notam_client = _wx.AwcPoller(view_fn=_notam_view,
                                      fetch_fn=_notam_fetch,
                                      interval_s=NOTAM_INTERVAL_S,
                                      name="NotamPoller")
        _notam_client.start()
        print("[PFD] NOTAM poller started (FAA API — keyed via Connectivity)")
        global _nexrad_client
        _nexrad_client = _nexrad.NexradClient(view_fn=_wx_view,
                                              interval_s=NEXRAD_INTERVAL_S)
        _nexrad_client.start()
        print("[PFD] NEXRAD poller started (radar, follows inset zoom)")

    # Signal-driven preview capture so a headless / SSH session (panel is
    # touch-only, no keyboard) can grab each page: navigate by touch, then
    # `kill -USR1 <pid>` to save the next manual filename, `kill -USR2 <pid>`
    # to reset the sequence.  The handler only flips a flag — the save runs
    # in the render loop where the frame is fresh and toast-free.
    import signal as _signal

    def _sigusr1(_sig, _frm):
        global _preview_cap_pending
        _preview_cap_pending = True

    def _sigusr2(_sig, _frm):
        global _preview_cap_idx, _preview_cap_msg, _preview_cap_msg_t
        _preview_cap_idx = 0
        _preview_cap_msg = "capture reset → next: full-screen MFD"
        _preview_cap_msg_t = time.monotonic()
    try:
        _signal.signal(_signal.SIGUSR1, _sigusr1)
        _signal.signal(_signal.SIGUSR2, _sigusr2)
    except Exception:
        pass   # non-main-thread / unsupported platform — F9/F10 still work

    running = True
    _last_traffic_t = 0.0      # monotonic time of last traffic recompute (throttle)
    while running:
        # Update demo state
        if demo_mode and demo:
            demo.tick()

        # Update flight simulator state (mutually exclusive with demo).
        # Skip the tick while paused so the freeze-frame holds steady —
        # taps, panels and the live UI still run normally.
        if _sim_state is not None and not disp["sim"].get("paused", False):
            _sim_state.tick()

        # Smooth sensor values into display values
        smooth_state()

        # Refresh ADS-B traffic against the freshly smoothed ownship fix.
        # Throttled to ~5 Hz (see _TRAFFIC_UPDATE_DT): the recompute is the
        # single biggest non-render cost per frame and the data underneath
        # only moves at ~1 Hz, so frame-rate recompute is wasted work.
        _now_t = time.monotonic()
        if _now_t - _last_traffic_t >= _TRAFFIC_UPDATE_DT:
            _update_traffic(demo_mode)
            _last_traffic_t = _now_t
        _update_weather()
        _update_nexrad()

        # Push AHRS / GPS to peer screens (rate-limited inside the helpers).
        _ssync_publish_ahrs()
        _ssync_publish_gps()
        _ssync_publish_fpl_lib()   # shared saved-plan / user-wpt library

        # Events
        for event in pygame.event.get():
            result = handle_event(event, demo_mode)
            if result is False:
                running = False
            elif result == "toggle_demo":
                demo_mode = not demo_mode
                if demo_mode:
                    demo = DemoState()

        if _sse_client:
            connected = _sse_client.connected
            # If the local SSE/serial link is down but a peer screen is
            # actively shadowing us data over UDP, treat the link as
            # alive — the NO LINK badge is about "no data source", not
            # "no SSE socket".  Without this the badge stays red on a
            # screen whose only source is a sibling PFD's published
            # AHRS / GPS feed.
            if (not connected and _screen_sync is not None):
                _peers, _age = _screen_sync.peer_status()
                if _peers > 0 and _age is not None and _age < 3.0:
                    connected = True
            disp["cs"]["ahrs_ok"] = connected
            # Stale-data timeout: track when link first dropped
            if not connected:
                if _link_lost_t is None:
                    _link_lost_t = time.monotonic()
                data_stale = (time.monotonic() - _link_lost_t) > STALE_TIMEOUT_S
            else:
                _link_lost_t = None
                data_stale   = False

        # Sim or demo provides its own data — SSE link state is irrelevant
        if _sim_state is not None or demo_mode:
            connected  = True
            data_stale = False

        # Multi-finger holds (same scheme as pi_zero):
        #   • exactly 2 fingers held LONG_PRESS_MS (800 ms)  → enter setup
        #   • 3+ fingers held MFD_SWAP_HOLD_MS (2 s)         → swap PFD ↔ MFD
        # The 3-finger threshold is longer so a 2-finger setup hold can't
        # accidentally trigger the swap when a 3rd finger grazes the screen.
        if (_multitouch_t0 is not None
                and len(_active_fingers) >= 2
                and disp["mode"] == "pfd"):
            dt = pygame.time.get_ticks() - _multitouch_t0
            if (_multitouch_max_fingers >= 3
                    and dt >= MFD_SWAP_HOLD_MS
                    and disp["ds"].get("mfd_enabled", True)):
                disp["display_mode"] = (
                    "mfd" if disp.get("display_mode", "pfd") == "pfd"
                    else "pfd")
                _settings.mark_dirty()
                # Lock taps out until the fingers lift; keep _active_fingers so
                # the FINGERUP path clears the lockout on full release.
                _gesture_tap_lockout = True
                _multitouch_t0 = None
                _multitouch_max_fingers = 0
            elif _multitouch_max_fingers == 2 and dt >= LONG_PRESS_MS:
                disp["mode"] = "setup"
                _gesture_tap_lockout = True
                _multitouch_t0 = None
                _multitouch_max_fingers = 0

        # Render.  A draw bug must never crash-loop the service (which the
        # pilot sees as the display "rebooting") — log it and keep flying the
        # last good frame so gestures (e.g. swap back to PFD) still recover.
        _t0 = time.monotonic()
        try:
            render(surf, demo_mode, connected, data_stale=data_stale)
        except Exception:
            import traceback
            print("[PFD] render crashed:", file=sys.stderr)
            traceback.print_exc()
        _t1 = time.monotonic()
        # Guided preview capture — save the clean (toast-free) frame first,
        # then draw the feedback banner for display only.
        global _preview_cap_pending
        if _preview_cap_pending:
            _preview_cap_pending = False
            _do_preview_capture(surf)
        if _preview_cap_msg and time.monotonic() - _preview_cap_msg_t < 5.0:
            _draw_capture_toast(surf, _preview_cap_msg)
        _flip()
        _t2 = time.monotonic()
        clock.tick(TARGET_FPS)

        # Built-in profiler: once the window elapses, dump the ranked table
        # in-line (loop keeps running) and disable so there's no further cost.
        if _cprof is not None and time.monotonic() >= _cprof_until:
            _cprof.disable()
            try:
                import pstats, io as _io
                _buf = _io.StringIO()
                pstats.Stats(_cprof, stream=_buf).sort_stats("tottime").print_stats(25)
                print("[CPROF] top 25 by tottime (self time):\n" + _buf.getvalue())
                _cprof.dump_stats("/tmp/pfd.prof")
                print("[CPROF] raw stats -> /tmp/pfd.prof")
            except Exception as _e:
                print(f"[CPROF] dump failed: {_e}")
            _cprof = None

        # Field perf grab (PFD_PERF=1) — render vs flip percentiles per window,
        # tagged with the view so a slow page/zoom is obvious in the summary.
        if _perf.enabled:
            _ptag = disp.get("display_mode", "pfd")
            if _ptag == "mfd":
                _ptag = (f"mfd {_map_overlay_state(disp['ds'])} "
                         f"{int(_mfd_last_range or 0)}nm"
                         + (" pan" if (_mfd_drag and _mfd_drag.get('is_drag'))
                            else ""))
            _flip_ms = (_t2 - _t1) * 1000.0
            if _use_shared_gl and gl_compositor is not None:
                # Split the flip into its parts: CPU serialize (tostring) and
                # GPU upload (tex.write) come from the compositor; present is
                # whatever's left (quad draw + vsync swap).
                _ser = gl_compositor.t_serialize
                _upl = gl_compositor.t_upload
                _pre = max(0.0, _flip_ms - _ser - _upl)
                _perf.add((_t1 - _t0) * 1000.0, _flip_ms, tag=_ptag,
                          serialize_ms=_ser, upload_ms=_upl, present_ms=_pre)
            else:
                _perf.add((_t1 - _t0) * 1000.0, _flip_ms, tag=_ptag)

        # Print frame timing every 60 frames so we can diagnose bottlenecks
        if not hasattr(main, '_frame_n'):
            main._frame_n = 0
        main._frame_n += 1
        if main._frame_n % 60 == 0:
            render_ms = (_t1 - _t0) * 1000
            flip_ms   = (_t2 - _t1) * 1000
            fps       = clock.get_fps()
            # Track process memory each ~2 s along with fps so we can spot
            # leak rates against rendering load. /proc/self/status VmRSS is
            # cheap and doesn't require psutil.
            _mem_kb = 0
            try:
                with open("/proc/self/status", "r") as _f:
                    for _ln in _f:
                        if _ln.startswith("VmRSS:"):
                            _mem_kb = int(_ln.split()[1])
                            break
            except OSError:
                pass
            # In shared-GL mode break flip into serialize/upload/present so a
            # glance at the console shows whether the 2D handoff or the vsync
            # present dominates — decides which optimisation is worth doing.
            _flip_dbg = f"flip={flip_ms:.1f}ms"
            if _use_shared_gl and gl_compositor is not None:
                _s = gl_compositor.t_serialize
                _u = gl_compositor.t_upload
                _p = max(0.0, flip_ms - _s - _u)
                _flip_dbg = (f"flip={flip_ms:.1f}ms"
                             f"(ser={_s:.1f}/upl={_u:.1f}/pre={_p:.1f}) ")
            # SoC temp + throttle word: confirms whether thermals/PSU are
            # capping fps. "thr=0x0" = clean; anything else = throttling now
            # or since boot (a trailing "!" flags non-zero so it's obvious).
            _tc, _thr = _soc_thermals()
            _therm_dbg = ""
            if _tc is not None:
                _therm_dbg = f"temp={_tc:.1f}C "
            if _thr is not None:
                _therm_dbg += f"thr={_thr}{'!' if _thr not in ('0x0','0') else ''}  "
            print(f"[PFD] fps={fps:.1f}  render={render_ms:.1f}ms  "
                  f"{_flip_dbg}  rss={_mem_kb/1024:.1f}MB  {_therm_dbg}"
                  f"roll={disp.get('roll', 0.0):+.2f} "
                  f"pitch={disp.get('pitch', 0.0):+.2f} "
                  f"yaw={disp.get('yaw', 0.0):+.2f} "
                  f"zupt={int(bool(disp.get('ahrs_zupt', False)))}")
            # Tracemalloc diff against baseline — only when --trace-mem.
            # Skip the first ~30 s so steady-state caches don't pollute
            # the "growing" picture.
            try:
                import tracemalloc
                if tracemalloc.is_tracing():
                    # Use frame counts that work even when tracemalloc tanks
                    # the framerate from 30 fps → 5 fps. Baseline after 60
                    # frames (~12 s at 5 fps, ~2 s at 30 fps), diff every
                    # 120 frames after that.
                    if main._frame_n >= 60 and _tm_baseline is None:
                        globals()['_tm_baseline'] = tracemalloc.take_snapshot()
                        print("[PFD][mem] baseline snapshot captured")
                    elif _tm_baseline is not None and main._frame_n % 120 == 0:
                        _snap = tracemalloc.take_snapshot()
                        _diff = _snap.compare_to(_tm_baseline, 'lineno')
                        print("[PFD][mem] top 10 growing since baseline:")
                        for _stat in _diff[:10]:
                            print(f"  +{_stat.size_diff/1024:7.1f} KB "
                                  f"({_stat.count_diff:+5d} blocks)  "
                                  f"{_stat.traceback}")
            except Exception:
                pass

    if _sse_client:
        _sse_client.stop()
    if _adsb_client:
        _adsb_client.stop()
    if _traffic_feed:
        _traffic_feed.stop()
    if _wx_client:
        _wx_client.stop()
    if _taf_client:
        _taf_client.stop()
    if _airsig_client:
        _airsig_client.stop()
    if _winds_client:
        _winds_client.stop()
    if _notam_client:
        _notam_client.stop()
    if _nexrad_client:
        _nexrad_client.stop()
    # Flush any pending settings changes to disk before exiting
    _settings.flush()
    pygame.quit()


if __name__ == "__main__":
    main()
