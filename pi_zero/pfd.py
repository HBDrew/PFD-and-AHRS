#!/usr/bin/env python3
"""
pfd.py – GI-275 inspired PFD for Pi Zero 2W (no SVT version).

This version omits the Synthetic Vision Terrain (SVT) background renderer
to maintain solid 30 fps on the Pi Zero 2W's limited GPU.  The attitude
indicator uses a plain sky/ground split.  Terrain and obstacle proximity
alerting (TAWS banners) is still fully functional via elevation lookups.

Run:  python3 pfd.py           (connects to Pico W at 192.168.4.1)
      python3 pfd.py --demo    (Sedona demo, no hardware needed)
      python3 pfd.py --sim     (windowed 640x480 for desktop testing)
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
import re
import subprocess
import urllib.request

# Add shared modules to path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'shared'))

os.environ.setdefault("SDL_VIDEODRIVER", "kmsdrm")  # overridden by --sim
os.environ["SDL_AUDIODRIVER"] = "dummy"  # suppress ALSA underrun spam

import pygame
import pygame.gfxdraw

from config import *   # noqa: F403
from sse_client import SSEClient
from terrain import (
    get_elevation_ft, get_elevation_ft_combined,
    coarse_tile_list, coarse_tile_url, coarse_tile_path, coarse_disk_stats,
    set_resolution_preference as _srtm_set_resolution_preference,
)   # elevation lookup only — no SVT renderer

# Pi Zero 2W only has 512 MB RAM and the tint samples ~2300 elevation
# points anyway — SRTM1 (25 MB / 52 MB-in-RAM per tile) is wasteful
# and contributed to OOM-reboots at wider zooms.  Tell the shared
# tile loader to treat any SRTM1 .hgt files as missing so it falls
# through to SRTM3 (5.8 MB-in-RAM per tile).
_srtm_set_resolution_preference("srtm3")
import obstacles as obs_mod
import airports as apt_mod
import airspaces as asp_mod
import water as water_mod
import settings as _settings
import screen_sync as _ssync_mod

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
AMBER      = (255, 190,  30)   # warmer than YELLOW; used for degraded sources

# ── Shared state ─────────────────────────────────────────────────────────────
_state_lock = threading.Lock()
state = {
    "roll": 0.0, "pitch": 0.0, "yaw": 0.0, "ay": 0.0,
    "lat": DEMO_LAT, "lon": DEMO_LON,
    "speed": 0.0, "track": 0.0, "fix": 0, "sats": 0,
    "alt": 0.0, "gps_alt": 0.0, "vspeed": 0.0,
    "baro_src": "gps", "baro_hpa": BARO_DEFAULT_HPA,
    "pitch_trim": 0.0, "roll_trim": 0.0, "yaw_trim": 0.0,
    "ahrs_ok": False, "gps_ok": False, "baro_ok": False,
}

# ── Display values (smoothed) ─────────────────────────────────────────────────
disp = dict(state)
disp["hdg_bug"]       = 0.0
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
disp["kbd_error"]     = ""          # error string shown on the keyboard (nav: UNKNOWN WAYPOINT)
disp["fp"] = {                      # flight-profile values
    "tail":   "N12345", "actype": "C172S",
    "vs0":    VS0,  "vs1": VS1,  "vfe": VFE,
    "vno":    VNO,  "vne": VNE,  "va":  VA,
    "vy":     VY,   "vx":  VX,
}
disp["display_mode"]  = "pfd"       # "pfd" | "mfd" — runtime view selector;
                                    # user no longer toggles this directly in
                                    # setup.  Flipped at runtime by the
                                    # 3-finger long-press gesture (see
                                    # MFD_SWAP_HOLD_MS) when mfd_enabled is
                                    # True.  mfd_enabled is the setup-screen
                                    # feature gate; display_mode is the
                                    # which-screen-am-I-on cursor.
disp["td"] = {                      # terrain download state
    "downloading": False,
    "compacting":  False,            # SRTM1 → SRTM3 in-place compactor
    "dl_region":   "",
    "dl_current":  0,
    "dl_total":    0,
    "dl_status":   "",
    "dl_cancel":   False,
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
# disp["asp"] mirrors disp["ad"]/disp["td"]/disp["od"]: status fields
# surfaced on the AIRSPACE DATA subscreen + the AIRSPACE tile on the
# system-setup screen.  Not persisted (it's recomputed on load).
disp["asp"] = {
    "records":   0,
    "downloading": False,
    "dl_status": "",
    "dl_cancel": False,
}
disp["ad"] = {                      # airport download/parse state
    "downloading": False,
    "dl_status":   "",
    "dl_cancel":   False,
    "parsing":     False,
    "records":     0,
    "used_mb":     0.0,
    "dl_date":     None,
    "expired":     False,
    "age_days":    0,
    # Per-category display filters
    "show_public":   True,
    "show_heli":     True,
    "show_seaplane": False,
    "show_other":    False,
}
disp["ds"] = {                      # display settings
    "spd_unit":  "kt",   "alt_unit":   "ft",
    "baro_unit": "inhg", "brightness": 8,  "night_mode": False,
    # MFD feature gate — when False, the runtime 3-finger swap gesture is
    # disarmed and display_mode is pinned to "pfd".  When True the user can
    # flip PFD ↔ MFD with a 3-finger 2 s hold without re-entering setup.
    "mfd_enabled":      False,
    # MFD settings — only relevant when display_mode == "mfd".
    "map_orient":       "trk",   # "trk" | "nrth"
    "map_zoom_nm":      10,      # half-extent of the inset's shorter axis
    "map_show_terrain":  True,
    "map_show_water":    True,
    "map_show_airports": True,
    "map_show_obstacles": True,
    "map_show_state_lines": True,
    "map_show_country_lines": True,
    # Airspace layer — master toggle + per-class.  Off by default for
    # the first-cut data so the screen doesn't show stale or
    # approximate polygons until the pilot has explicitly enabled
    # them.  Per-class toggles let pilots hide MOAs / restricted /
    # whatever isn't relevant to their flight.
    "map_show_airspaces":   False,
    "map_show_airspace_b":   True,
    "map_show_airspace_c":   True,
    "map_show_airspace_d":   True,
    "map_show_airspace_moa": True,
    "map_show_airspace_r":   True,
    "map_show_airspace_p":   True,
    "map_show_airspace_tfr": True,
    # MFD bottom data strip — 8 user-selectable readout slots.  Each
    # entry is a kind id from _MFD_STRIP_KIND_IDS; user reconfigures
    # via tap-strip → chooser overlay.  See draw_mfd / draw_mfd_strip_setup.
    "mfd_strip_kinds":  ["gs", "trk", "alt", "wpt", "btw", "dist", "ete", "eta"],
}
disp["ss"] = {                      # AHRS / sensor settings
    "pitch_trim":    0.0, "roll_trim": 0.0,
    # Axis alignment — input-side rotation applied on the Pico, used
    # to kill yaw → pitch/roll coupling from a slightly tilted sensor
    # mount.  Auto-captured during a level cardinal walk; can also be
    # tuned via the Pi 4's AHRS setup screen.
    "pitch_align":   0.0, "roll_align": 0.0,
    "mag_cal":       "idle", "mounting": "normal",
    "hdg_src":       "auto",  # "mag" | "trk" | "auto" — heading source preference
    "airspeed_src":  "gps",   # "gps" | "ias"  — speed source (GPS groundspeed or IAS sensor)
}
disp["wd"] = {                      # water-mask download / rasterise state
    "downloading": False,
    "dl_status":   "",
    "dl_current":  0,
    "dl_total":    0,
    "dl_cancel":   False,
}
disp["fw"] = {                      # AHRS firmware loader state
    "push_state":  "",   # ""|"pushing"|"done"|"error"
    "push_msg":    "",
    "flash_state": "",   # ""|"flashing"|"done"|"error"
    "flash_msg":   "",
}
disp["cs"] = {                      # connectivity settings
    "ahrs_url":  PICO_URL, "wifi_ssid": "AHRS-Link",
    "wifi_pass": "",        "wifi_ok":  False,
    "wifi_actual": "",      # SSID actually associated now (from iwgetid -r)
    "scan_state": "",   "scan_nets": [], "scan_scroll": 0, "scan_error": "",
    "ahrs_ok":   False,     "test_msg": "", "apply_msg": "", "inet_msg": "",
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
    "sync_publish_fpl":  False, "sync_consume_fpl":  False,
}
disp["nav"] = {                     # direct-to navigation
    "ident":   "",      # ICAO/local ID of active waypoint, "" = none
    "lat":     0.0,
    "lon":     0.0,
    "elev_ft": 0.0,
    "act_lat": 0.0,     # aircraft lat at activation (CDI course reference)
    "act_lon": 0.0,
}
disp["mfd_pan"] = {                 # MFD pan offset
    "lat": None,        # None → map follows aircraft
    "lon": None,
}
# Flight plan — ordered list of waypoints + active-leg index.  Each
# waypoint is {ident, lat, lon, elev_ft, user}.  active_idx == -1 means
# no plan is active (the FPL list still survives across restarts; you
# just aren't navigating along it).  When active_idx >= 0 the current
# leg's destination is mirrored into disp["nav"] so the existing CDI,
# moving-map course line, ETE/ETA, and D→ button all keep working.
disp["fpl"] = {
    "waypoints":  [],
    "active_idx": -1,
}
# User-waypoint library — persistent across flights.  Every waypoint
# created via +LAT/LON is auto-saved here (dedup by ident),
# so the pilot can recall it onto any later flight plan via +LIB.
# Stored as a dict containing the list so the existing
# _PERSIST_COMPLEX_SUBTREES mechanism (which works on subtree keys)
# can persist it.  Idents are unique within the library.
disp["user_wpts"] = {
    "list": [],   # [{ident, lat, lon, elev_ft}, ...]
}
# In-progress user-waypoint entry — populated by the +LAT/LON entry
# screen (which pre-fills the lat/lon fields with the current aircraft
# position so the same path also handles "mark a point HERE").
# Cleared on save or cancel.  Not persisted — transient editor state.
disp["fpl_new"] = {
    "ident":   "",
    "lat":     0.0,
    "lon":     0.0,
    "lat_str": "",         # raw keyboard buffer
    "lon_str": "",
    "source":  "",
}
disp["sim"] = {                     # flight simulator state
    "preset_idx": 0,    # index into SIM_PRESETS
    "init_alt":   5000.0,
    "init_hdg":   0.0,
    "init_spd":   90.0,
    "gps_fail":   False,
    "baro_fail":  False,
    "ahrs_fail":  False,
}

SMOOTH_K = 0.25   # IIR coefficient (higher = faster response)

# ── Module-level SSE handle (set in main, restarted by handle_event) ─────────
_sse_client  = None
_sim_state   = None   # SimFlyState instance when sim is running, else None
_link_lost_t = None   # monotonic timestamp when link first dropped (None if connected)

# ── Screen-to-screen sync ────────────────────────────────────────────────────
# Created in main() once disp["cs"] has been restored from settings.json.
_screen_sync = None
# When a remote update arrives we want to mark it as applied without
# bouncing it back out as a fresh broadcast.  Bumped by listener
# callbacks, checked by _ssync_publish_*.
_ssync_suppress_publish = 0


def _ssync_kinds_from_cs(direction):
    """Build the set of categories whose `direction` ("publish" / "consume")
    toggle is on in disp["cs"]."""
    out = set()
    cs = disp.get("cs", {})
    for k in (_ssync_mod.KIND_BUGS, _ssync_mod.KIND_BARO,
              _ssync_mod.KIND_NAV,  _ssync_mod.KIND_AHRS,
              _ssync_mod.KIND_GPS,  _ssync_mod.KIND_FPL):
        if cs.get(f"sync_{direction}_{k}", False):
            out.add(k)
    return out


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
    """Broadcast current bug values to peer screens (no-op when neither
    sync is running nor publish-bugs is enabled)."""
    if _screen_sync is None or _ssync_suppress_publish:
        return
    _screen_sync.publish(_ssync_mod.KIND_BUGS, {
        "alt_bug": float(disp.get("alt_bug", 0.0)),
        "spd_bug": float(disp.get("spd_bug", 0.0)),
        "hdg_bug": float(disp.get("hdg_bug", 0.0)),
        "vs_bug":  float(disp.get("vs_bug",  0.0)),
    })


def _ssync_apply_bugs(data):
    """Listener callback: apply incoming bug values from a peer.  Sets
    the suppression flag so the assignment doesn't echo back out."""
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
            # Mirror the numpad-commit path: keep shared state in lock-step
            # and push the new altimeter setting to the Pico if connected.
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


_ssync_last_ahrs_t = 0.0
_ssync_last_gps_t  = 0.0
_SSYNC_AHRS_MIN_DT = 0.05    # 20 Hz upper bound
_SSYNC_GPS_MIN_DT  = 0.20    # 5 Hz upper bound


def _ssync_publish_ahrs():
    """Broadcast attitude at most 20 Hz."""
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
        "pitch":    float(disp.get("pitch", 0.0)),
        "roll":     float(disp.get("roll",  0.0)),
        "yaw":      float(disp.get("yaw",   0.0)),
        # Include health so the receiving screen can mark its attitude
        # indicator as live (no red X) when a peer's Pico is sourcing it.
        "ahrs_ok":  bool(disp.get("ahrs_ok", False)),
    })


def _ssync_apply_ahrs(data):
    """Inject remote attitude into the shared state dict so the normal
    smoothing path picks it up.  Only useful when this screen has no
    local AHRS source — otherwise the local writer just bashes it back
    on the next sensor sample."""
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
    })


def _ssync_apply_gps(data):
    """Inject remote GPS + altitude + airspeed into shared state."""
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
    finally:
        _ssync_suppress_publish -= 1


def _ssync_publish_fpl():
    """Mirror the full flight plan + active leg index to the peer.
    Triggered whenever the FPL changes (add/delete/reorder/activate/
    deactivate).  Subject to _ssync_suppress_publish so applying a
    received FPL doesn't bounce back."""
    if _screen_sync is None or _ssync_suppress_publish:
        return
    fpl = disp.get("fpl", {})
    _screen_sync.publish(_ssync_mod.KIND_FPL, {
        "waypoints":  list(fpl.get("waypoints", [])),
        "active_idx": int(fpl.get("active_idx", -1)),
    })


def _ssync_apply_fpl(data):
    """Replace the local FPL with the peer's.  Also re-applies the
    active leg into disp["nav"] so CDI / D→ button / moving-map line
    follow the new active waypoint."""
    global _ssync_suppress_publish
    _ssync_suppress_publish += 1
    try:
        wps = data.get("waypoints", [])
        idx = int(data.get("active_idx", -1))
        # Defensive parse — accept dicts or skip malformed entries.
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
        disp["fpl"]["active_idx"] = idx if 0 <= idx < len(clean) else -1
        # Mirror the active leg into disp["nav"] without touching
        # _settings.mark_dirty (the FPL itself persists in this side's
        # settings.json on next mark) so CDI etc. pick up the change.
        if _fpl_is_active():
            _fpl_apply_active()
        else:
            disp["nav"]["ident"]   = ""
            disp["nav"]["lat"]     = 0.0
            disp["nav"]["lon"]     = 0.0
            disp["nav"]["elev_ft"] = 0.0
        _settings.mark_dirty()
    finally:
        _ssync_suppress_publish -= 1


def _ssync_apply_nav(data):
    """Apply a remote D2 update.  Empty ident clears D2 (matches the
    CANCEL D2 button on the keyboard)."""
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

# ── GPS-slaved heading complementary filter ───────────────────────────────────
# Propagate heading using the AHRS gyro yaw-rate (smooth, 30 Hz) and
# slowly slave the absolute reference toward the GPS ground track (1–5 Hz,
# noisy).  This mirrors how real GPS/IRS heading modes work.
_gps_hdg      = None   # current complementary-filter output (degrees, 0–360)
_prev_yaw_disp = None  # disp["yaw"] value from the previous frame

HDG_TRK_MIN_KT = 3.0   # below this speed, GPS track is unreliable


def _resolve_hdg_source(hdg_src_pref, gps_ok, ahrs_ok, speed_kt):
    """Resolve the user's preference (hdg_src in {"mag","trk","auto"}) +
    runtime conditions into the active source.  Mirrors pi4's
    _resolve_hdg_source so the two displays use identical UX.

    Returns (use_track: bool, label: str, color: tuple).
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
        if mag_ok:
            return False, "G?", AMBER
        return False, "?", AMBER
    # auto: prefer TRK when GPS is moving, fall back to MAG
    if track_ok:
        return True, "G", MAGENTA
    if mag_ok:
        return False, "M", WHITE
    return False, "?", AMBER


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

    Without this call, the PFD shows the pilot's entered baro locally
    but the AHRS-derived altitude still reflects the firmware's old
    QNH — a silent miscalibration.  Runs in a background thread so
    the frame loop is never blocked.
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


def _test_internet():
    """DNS + HTTPS round-trip to confirm the WiFi we just joined actually
    reaches the wider internet (vs. associated-but-captive-portal).
    Uses Google's generate_204 endpoint \u2014 small, fast, no body."""
    try:
        t0 = time.monotonic()
        req = urllib.request.Request(
            "https://www.google.com/generate_204",
            headers={"User-Agent": "pfd-internet-test/1"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            ms = int((time.monotonic() - t0) * 1000)
            if r.status in (200, 204):
                return True, f"Internet OK ({ms} ms) \u2713"
            return False, f"HTTP {r.status} from probe"
    except Exception as e:
        return False, f"Internet: {str(e)[:46]}"


# ── Backlight control ─────────────────────────────────────────────────────────
# Two transports, in priority order:
#
#   1. Hardware PWM via the kernel sysfs PWM subsystem
#      (/sys/class/pwm/pwmchip0/pwm0/duty_cycle).  Used on the Waveshare
#      3.5" DPI panel where backlight is active-low on GPIO 18.  Requires
#      `dtoverlay=pwm,pin=18,func=2` in /boot/firmware/config.txt and
#      the export + period + polarity + chmod set up by the pfd.service
#      ExecStartPre block (root-only ops done there so this process can
#      stay unprivileged).  Polarity is `inversed` in sysfs so the duty
#      cycle reads naturally: 0 % = off, 100 % = max brightness.
#
#   2. Legacy /sys/class/backlight/<panel>/brightness node — kernel
#      backlight drivers (rpi_backlight, I²C-controlled backlights).
#
# If neither is available the slider still works in the UI but won't
# change panel output.

_BL_PWM_DUTY     = "/sys/class/pwm/pwmchip0/pwm0/duty_cycle"
_BL_PWM_PERIOD_F = "/sys/class/pwm/pwmchip0/pwm0/period"

# Slider level (1-10) → PWM duty cycle in % of period.
#
# The Waveshare 3.5" DPI panel's backlight LED driver doesn't conduct
# below ~22 % duty cycle — anything dimmer is indistinguishable from off.
# So the bottom of the slider lives at the LED's minimum conduction
# point, and the curve is intentionally non-linear: small steps at the
# dim end (1-4) where the user wants fine control for a night cockpit,
# larger steps at the bright end (7-10) where each click should make
# a noticeable difference in daylight.
#
# Hand-tuned, not a gamma formula, so each level can be nudged
# independently if a future panel revision behaves differently.
_BL_DUTY_PCT = (22, 26, 30, 35, 44, 55, 67, 78, 89, 100)

_BACKLIGHT_PATHS = [
    "/sys/class/backlight/rpi_backlight/brightness",
    "/sys/class/backlight/10-0045/brightness",
]

_BL_TRANSPORT       = None      # "pwm" | "sysfs" | None
_BL_PWM_PERIOD_NS   = 1_000_000 # cached at init; service unit is the source of truth
_backlight_path     = None
_backlight_max_path = None      # max_brightness sysfs node

def _init_backlight():
    """Detect which backlight transport is available."""
    global _backlight_path, _backlight_max_path
    global _BL_TRANSPORT, _BL_PWM_PERIOD_NS

    # Path 1: hardware PWM (preferred on Waveshare 3.5" DPI).  The
    # service unit's ExecStartPre has already exported pwm0, set period
    # and polarity, enabled the channel, and chmod'd duty_cycle to be
    # group-writable.  We only need to confirm the file is writable and
    # cache the period that ExecStartPre set.
    if os.access(_BL_PWM_DUTY, os.W_OK):
        try:
            with open(_BL_PWM_PERIOD_F) as f:
                _BL_PWM_PERIOD_NS = int(f.read().strip())
        except OSError:
            pass
        _BL_TRANSPORT = "pwm"
        print(f"[BL] Using PWM backlight: {_BL_PWM_DUTY} "
              f"(period={_BL_PWM_PERIOD_NS} ns)")
        return

    # Path 2: legacy sysfs backlight driver.
    for p in _BACKLIGHT_PATHS:
        if os.path.exists(p):
            _backlight_path     = p
            _backlight_max_path = os.path.join(os.path.dirname(p), "max_brightness")
            _BL_TRANSPORT = "sysfs"
            print(f"[BL] Using sysfs backlight: {p}")
            return

    print("[BL] No backlight control available")

def _set_backlight(level: int):
    """Set brightness 1–10 across whichever transport is active.

    level=1 maps to 0 % (effectively off, matches the existing 1–10
    scale's lower bound); level=10 maps to 100 %.
    """
    if _BL_TRANSPORT == "pwm":
        try:
            lv = max(1, min(10, int(level)))
            duty = int(_BL_PWM_PERIOD_NS * _BL_DUTY_PCT[lv - 1] / 100)
            with open(_BL_PWM_DUTY, "w") as f:
                f.write(str(duty))
        except OSError:
            pass
        return

    if _BL_TRANSPORT == "sysfs":
        if _backlight_path is None:
            return
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
              "ahrs_ok", "gps_ok", "baro_ok", "airdata_ok",
              "pitch_trim", "roll_trim", "yaw_trim"):
        if k in snap:
            disp[k] = snap[k]


# ── Font helpers ──────────────────────────────────────────────────────────────
_fonts = {}

def _get_font(size: int, bold: bool = False):
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


# LRU cache of rendered text surfaces.  pi_zero calls _text dozens of
# times per frame (tape ticks, readouts, status labels) and font.render
# allocates a fresh SDL surface each call — at 12 fps that was a major
# source of memory churn, contributing to OOM-reboots during sustained
# high-FPS scenes like a steep turn.  The cache caps at 512 entries
# (~3 MB worst case for typical label sizes), evicting LRU on overflow.
import collections as _collections_text
_TEXT_CACHE_MAX = 512
_text_cache: "_collections_text.OrderedDict" = _collections_text.OrderedDict()


def _text(surf, txt, size, colour, cx=None, cy=None, x=None, y=None, bold=False):
    """Render text centred on (cx,cy) or top-left at (x,y)."""
    txt_s = str(txt)
    key = (txt_s, size, colour, bold)
    img = _text_cache.get(key)
    if img is None:
        fnt = _get_font(size, bold)
        img = fnt.render(txt_s, True, colour)
        _text_cache[key] = img
        if len(_text_cache) > _TEXT_CACHE_MAX:
            _text_cache.popitem(last=False)
    else:
        _text_cache.move_to_end(key)
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


# ── AI background (plain sky/ground — no SVT on Pi Zero 2W) ──────────────────


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

    px_per_deg = 10.0
    old_clip   = surf.get_clip()
    surf.set_clip(pygame.Rect(ax, ay, aw, ah))

    cx  = ax + aw // 2
    # Anchor the horizon at TAPE_MID — same reference centre the pitch
    # ladder uses for its zero-pitch line.  Using the rect's geometric
    # centre (ay + ah/2) put the horizon ~11 px above TAPE_MID for
    # _full_ai = (0, 0, W, HDG_Y), producing a visible ~1° gap between
    # the white horizon line drawn here and the pitch-ladder horizon.
    cy  = TAPE_MID

    roll_rad = math.radians(roll)
    cos_r, sin_r = math.cos(roll_rad), math.sin(roll_rad)

    # Horizon point: the point on the horizon line closest to the camera
    # centre. Sign convention must match draw_pitch_ladder's _rv() which
    # rotates body coords (0, pitch_px) to screen (cx + pitch_px*sin_r,
    # cy + pitch_px*cos_r) — so the sky/ground polygon, the horizon
    # line, and the pitch-ladder 0° line all draw at the same point for
    # any pitch / roll combination.
    pitch_offset = pitch * px_per_deg
    hcx = cx + pitch_offset * sin_r
    hcy = cy + pitch_offset * cos_r

    # Extend horizon line well beyond the rect so clipping takes care of edges.
    # Line direction in pygame Y-down is (cos_r, -sin_r); for positive roll
    # (right bank) that's "right and up" → LEFT-DOWN, RIGHT-UP, matching the
    # pitch-ladder white horizon line.
    R  = aw + ah
    h1 = (hcx - R * cos_r, hcy + R * sin_r)
    h2 = (hcx + R * cos_r, hcy - R * sin_r)

    # Classify each corner relative to the (h1, h2) line. The implicit
    # equation of that line is sin_r*(px-hcx) + cos_r*(py-hcy) = 0; sky is
    # the side where py is "above" (smaller y in pygame), giving
    #     sky_side(p) = (hcy - py)*cos_r + (hcx - px)*sin_r > 0.
    # The older form used (px - hcx) which flipped the sin term and
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


# ── Above-horizon terrain silhouette ──────────────────────────────────────────
# Pi Zero 2W can't afford the full SVT mesh that pi4 runs through OpenGL —
# the per-quad math + draw cost over ~200 polygons leaves no frame budget
# at 30 fps. Instead we ray-cast forward across the AI's horizontal FOV,
# look up the SRTM peak along each ray, and draw a single silhouette
# polygon for any terrain rising above the visual horizon. ~3-5 ms/frame
# on a Pi Zero 2W.
_AHT_N_RAYS         = 41
_AHT_HALF_FOV_DEG   = 33.0                    # ±33° covers the full 640 px display width at
                                              # 10 px/deg with margin for roll-induced edge growth
_AHT_DIST_NM        = (0.5, 1.0, 2.0, 4.0, 7.0, 12.0, 20.0)
_AHT_PX_PER_DEG     = 10.0                    # match draw_simple_ai_background + draw_pitch_ladder
                                              # (NOT the 8.0 obstacles use — caused a 20 px gap
                                              # between the silhouette base and the horizon line)

# Silhouette fill colour by worst-case clearance (terrain above us is red,
# borderline-above is orange, comfortably-below would be brown but the
# polygon only renders when peaks exceed the eye-level horizon).
def _aht_fill_colour(min_clearance_ft):
    if min_clearance_ft < TERRAIN_WARNING_FT:
        return (170,  35,  30)   # red — peak at or above aircraft alt
    if min_clearance_ft < TERRAIN_CAUTION_FT:
        return (180, 100,  30)   # orange — peak within 700 ft
    return (140,  85,  35)       # dark brown — peak comfortably below


def draw_above_horizon_terrain(surf, ai_rect, lat, lon, alt_ft,
                               hdg_deg, pitch_deg, roll_deg):
    """Ray-cast forward across the AI's FOV, find each ray's max SRTM
    elevation, and draw the resulting peak silhouette as a single
    polygon between the silhouette curve and the visual horizon line.

    No-op when SRTM tiles aren't loaded (TAWS banner covers that case).
    Colour reflects the worst clearance across the visible peaks.
    """
    import numpy as _np

    if not _has_terrain:
        return

    ax, ay_r, aw, ah = ai_rect
    cx = ax + aw // 2
    # Same TAPE_MID anchor as draw_simple_ai_background / draw_pitch_ladder
    # so the silhouette base sits exactly on the horizon line.
    cy = TAPE_MID

    nm_per_deg_lat = 60.0
    nm_per_deg_lon = max(1.0, 60.0 * math.cos(math.radians(lat)))

    # Ray bearings, relative to nose
    rays_rel_brg = _np.linspace(-_AHT_HALF_FOV_DEG, _AHT_HALF_FOV_DEG, _AHT_N_RAYS)
    abs_brg_rad  = _np.radians((hdg_deg + rays_rel_brg) % 360.0)

    # Per-ray peak elevation (ft, MSL) — start at "no peak"
    peak_elev = _np.full(_AHT_N_RAYS, -9999.0, dtype=_np.float64)
    peak_dist = _np.full(_AHT_N_RAYS,    0.0,  dtype=_np.float64)

    for d_nm in _AHT_DIST_NM:
        sample_lats = lat + d_nm * _np.cos(abs_brg_rad) / nm_per_deg_lat
        sample_lons = lon + d_nm * _np.sin(abs_brg_rad) / nm_per_deg_lon
        for i in range(_AHT_N_RAYS):
            elev = get_elevation_ft_combined(SRTM_DIR, COARSE_DIR,
                                             float(sample_lats[i]),
                                             float(sample_lons[i]))
            if elev > peak_elev[i]:
                peak_elev[i] = elev
                peak_dist[i] = d_nm

    # Visual elevation angle of each peak from the aircraft (positive =
    # peak rises above eye level).  Use the per-ray distance the peak
    # was sampled at — closer peaks loom larger for the same elevation.
    dist_ft  = peak_dist * 6076.0
    dist_ft  = _np.maximum(dist_ft, 1.0)
    peak_deg = _np.degrees(_np.arctan2(peak_elev - alt_ft, dist_ft))

    # Cull when no ray has terrain rising above horizon
    if not (peak_deg > 0.1).any():
        return

    # Project to screen in the same convention as draw_pitch_ladder (the
    # silhouette polygon then rotates with roll exactly like the
    # horizon line and the obstacle symbols).
    cos_r = math.cos(math.radians(roll_deg))
    sin_r = math.sin(math.radians(roll_deg))

    sxr     = rays_rel_brg * _AHT_PX_PER_DEG
    # peak_deg clamped at 0 so non-visible rays sit flush on the
    # horizon — they contribute zero polygon height at that bearing.
    syr_top = (pitch_deg - _np.maximum(peak_deg, 0.0)) * _AHT_PX_PER_DEG
    syr_hor = pitch_deg * _AHT_PX_PER_DEG

    # Rotation must match draw_pitch_ladder._rv() so the silhouette
    # rotates with the horizon line (not against it). Body coord (x, y)
    # → screen (cx + x*cos_r + y*sin_r, cy - x*sin_r + y*cos_r).
    sx_top = (cx + sxr * cos_r + syr_top * sin_r).astype(_np.int32)
    sy_top = (cy - sxr * sin_r + syr_top * cos_r).astype(_np.int32)
    sx_hor = (cx + sxr * cos_r + syr_hor * sin_r).astype(_np.int32)
    sy_hor = (cy - sxr * sin_r + syr_hor * cos_r).astype(_np.int32)

    # Polygon: silhouette curve left→right, then horizon line right→left
    polygon = list(zip(sx_top.tolist(), sy_top.tolist())) \
            + list(zip(sx_hor[::-1].tolist(), sy_hor[::-1].tolist()))

    # Colour by worst clearance among visible peaks
    visible = peak_deg > 0.1
    worst_clearance = float((alt_ft - peak_elev[visible]).min())
    fill = _aht_fill_colour(worst_clearance)

    old_clip = surf.get_clip()
    surf.set_clip(pygame.Rect(ax, ay_r, aw, ah))
    pygame.draw.polygon(surf, fill, polygon)
    # Ridge line (slightly lighter) for visual depth
    ridge_col = tuple(min(255, c + 35) for c in fill)
    ridge_pts = list(zip(sx_top.tolist(), sy_top.tolist()))
    if len(ridge_pts) >= 2:
        pygame.draw.lines(surf, ridge_col, False, ridge_pts, 1)
    surf.set_clip(old_clip)


# ── Pitch ladder ──────────────────────────────────────────────────────────────
def draw_pitch_ladder(surf, ai_rect, pitch, roll):
    """
    White pitch ladder lines drawn directly in rotated coordinates.
    No intermediate surface or transform.rotate — fast on Pi Zero 2W.
    """
    ax, ay, aw, ah = ai_rect
    cx, cy = ax + aw // 2, ay + ah // 2

    px_per_deg = 10.0
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

    for deg in range(-30, 35, 5):
        rel_y = pitch_px - int(deg * px_per_deg)  # y offset from AI center

        # Cull lines too far from the visible window (±185 px from centre)
        if rel_y < -185 or rel_y > 185:
            continue

        major = (deg % 10 == 0)
        half  = major_half if major else minor_half

        if deg == 0:
            # Horizon line
            p1 = _rv(-half, rel_y)
            p2 = _rv( half, rel_y)
            pygame.draw.line(surf, (255, 255, 255, 200), p1, p2, 2)
            continue

        col = (255, 255, 255, 220)
        p1  = _rv(-half, rel_y)
        p2  = _rv( half, rel_y)

        if major:
            pygame.draw.line(surf, col, p1, p2, 2)
        else:
            pygame.draw.aaline(surf, col, p1, p2)

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
    # Solid filled polygon band between inner and outer radius.
    _ARC_STEPS = 80
    _ARC_THICK = 2
    arc_outer = []
    arc_inner = []
    for i in range(_ARC_STEPS + 1):
        # Sky-pointer design: arc rotates WITH the sky/horizon so the fixed
        # aircraft reference at the top reads the current bank.
        ang = (-90 - roll - 60 + i * 120.0 / _ARC_STEPS) * DEG
        cos_a, sin_a = math.cos(ang), math.sin(ang)
        arc_outer.append((int(cx + (ROLL_R + _ARC_THICK) * cos_a),
                          int(cy + (ROLL_R + _ARC_THICK) * sin_a)))
        arc_inner.append((int(cx + ROLL_R * cos_a),
                          int(cy + ROLL_R * sin_a)))
    arc_band = arc_outer + list(reversed(arc_inner))
    pygame.gfxdraw.filled_polygon(surf, arc_band, WHITE)
    pygame.gfxdraw.aapolygon(surf, arc_band, WHITE)

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

    # Moving upper doghouse — OUTSIDE arc, tip at arc, moves with roll arc
    upper_ang = (-90 - roll) * DEG   # rotates with the arc (sky pointer)
    tri0 = _doghouse_pts(cx, cy, upper_ang, ROLL_R + 2, size=10, inward=True)
    pygame.gfxdraw.filled_polygon(surf, tri0, WHITE)
    pygame.gfxdraw.aapolygon(surf, tri0, WHITE)

    # Fixed lower doghouse — INSIDE arc, tip at arc-8, fixed at 12 o'clock
    roll_ang = -math.pi / 2
    rp_pts = _doghouse_pts(cx, cy, roll_ang, ROLL_R - 8, size=10, inward=False)
    pygame.gfxdraw.filled_polygon(surf, rp_pts, WHITE)
    pygame.gfxdraw.aapolygon(surf, rp_pts, WHITE)


# ── Aircraft symbol ───────────────────────────────────────────────────────────
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
    pygame.gfxdraw.filled_polygon(surf, li, AMBER_DARK)
    pygame.gfxdraw.aapolygon(surf, li, AMBER_DARK)
    pygame.gfxdraw.filled_polygon(surf, lo, AMBER)
    pygame.gfxdraw.aapolygon(surf, lo, AMBER)
    pygame.gfxdraw.filled_polygon(surf, ri, AMBER_DARK)
    pygame.gfxdraw.aapolygon(surf, ri, AMBER_DARK)
    pygame.gfxdraw.filled_polygon(surf, ro, AMBER)
    pygame.gfxdraw.aapolygon(surf, ro, AMBER)

    # Engine nacelles — fills
    lu = [(CX - 93, CY), (CX - 99, CY - 6), (CX - 138, CY - 6), (CX - 138, CY)]
    ll = [(CX - 93, CY), (CX - 138, CY),    (CX - 138, CY + 6), (CX - 99, CY + 6)]
    ru = [(CX + 93, CY), (CX + 99, CY - 6), (CX + 138, CY - 6), (CX + 138, CY)]
    rl = [(CX + 93, CY), (CX + 138, CY),    (CX + 138, CY + 6), (CX + 99, CY + 6)]
    pygame.gfxdraw.filled_polygon(surf, lu, AMBER)
    pygame.gfxdraw.aapolygon(surf, lu, AMBER)
    pygame.gfxdraw.filled_polygon(surf, ll, AMBER_DARK)
    pygame.gfxdraw.aapolygon(surf, ll, AMBER_DARK)
    pygame.gfxdraw.filled_polygon(surf, ru, AMBER)
    pygame.gfxdraw.aapolygon(surf, ru, AMBER)
    pygame.gfxdraw.filled_polygon(surf, rl, AMBER_DARK)
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
_hdg_tape_bg = None   # cached heading-tape background surface
_red_x_overlays = {}  # cached red-X overlay panels keyed by (w, h)


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
    # When the bug is above the visible tape range the chevron parks with its
    # CENTRE at the bug-readout box's bottom edge (y=2+HDG_H=46) so half of
    # the chevron hides behind the box — mirroring how the bottom park puts
    # the chevron half behind the heading-tape bug box at TAPE_BOT.
    if gs_bug is not None:
        _spd_bug_park_top = 2 + HDG_H
        gby = max(_spd_bug_park_top, min(TAPE_BOT, spd_y(gs_bug, speed)))
        gb = [(SPD_X,      gby - 17),
              (SPD_X + 14, gby - 17), (SPD_X + 14, gby - 5), (SPD_X + 7, gby),
              (SPD_X + 14, gby + 5),  (SPD_X + 14, gby + 17), (SPD_X, gby + 17)]
        spd_bug_col = MAGENTA if airspeed_src == "gps" else CYAN
        surf.set_clip((0, TAPE_TOP, DISPLAY_W, TAPE_BOT - TAPE_TOP))
        pygame.draw.polygon(surf, spd_bug_col, gb)
        surf.set_clip(None)

    # Speed readout box — Veeder-Root style.  Geometry scaled 1.5× from the
    # original SVG spec so the digits are legible at arm's length on the
    # 3.5" Waveshare panel.  Total width 99 px (was 66), height ±44 (was ±29).
    # Layout: pointer(22) → inner section(48) → drum section(29) = 99 px total.
    pts_s = _chamfer([(SPD_X,      TAPE_MID),
                      (SPD_X + 22, TAPE_MID - 22), (SPD_X + 70, TAPE_MID - 22),
                      (SPD_X + 70, TAPE_MID - 44), (SPD_X + 99, TAPE_MID - 44),
                      (SPD_X + 99, TAPE_MID + 44),
                      (SPD_X + 70, TAPE_MID + 44), (SPD_X + 70, TAPE_MID + 22),
                      (SPD_X + 22, TAPE_MID + 22)], {2, 3, 4, 5, 6, 7})
    pygame.gfxdraw.filled_polygon(surf, pts_s, (0, 10, 30))
    spd_col = RED if speed > vne else (YELLOW if speed > vno else WHITE)
    # Inner: hundreds + tens, cascade-rolling
    _rolling_drum(surf, SPD_X + 24, TAPE_MID - 21, 45, 42, speed, 2, spd_col, 36,
                  power_offset=1, suppress_leading=True)
    # Drum: units digit, adjacent digits ~50% visible
    _rolling_drum(surf, SPD_X + 72, TAPE_MID - 42, 25, 84, speed, 1, spd_col, 36,
                  show_adjacent=True, adj_slot_h=34)
    _drum_shade(surf,   SPD_X + 72, TAPE_MID - 42, 25, 84)
    # Border drawn LAST so drum shade doesn't cover the inner pixels
    pygame.draw.polygon(surf, WHITE, pts_s, width=2)
    pygame.gfxdraw.aapolygon(surf, pts_s, WHITE)

    # GS bug button — top strip of speed tape; color matches bug triangle
    gs_str = f"{round(gs_bug):3d}" if gs_bug is not None else "---"
    spd_box_col = MAGENTA if airspeed_src == "gps" else CYAN
    _cyan_box(surf, gs_str, x=SPD_X, y=2, w=SPD_W, h=HDG_H,
              font_sz=24, col=spd_box_col)


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
    # Park bound: chevron centre at the alt-bug-readout box's bottom edge so
    # half of the chevron tucks behind the box (symmetric with TAPE_BOT park).
    # MUST be drawn BEFORE the top _cyan_box so the box overlays the chevron's
    # top half when parked (matches the speed-tape draw order).
    if alt_bug is not None:
        _alt_bug_park_top = 2 + HDG_H
        aby = max(_alt_bug_park_top, min(TAPE_BOT, ay2(alt_bug)))
        bug = [(ALT_X + ALT_W,      aby - 17),
               (ALT_X + ALT_W - 14, aby - 17), (ALT_X + ALT_W - 14, aby - 5), (ALT_X + ALT_W - 7, aby),
               (ALT_X + ALT_W - 14, aby + 5),  (ALT_X + ALT_W - 14, aby + 17), (ALT_X + ALT_W, aby + 17)]
        alt_bug_col = CYAN if baro_ok else MAGENTA
        surf.set_clip((0, TAPE_TOP, DISPLAY_W, TAPE_BOT - TAPE_TOP))
        pygame.draw.polygon(surf, alt_bug_col, bug)
        surf.set_clip(None)

    # ALT bug button — top strip of alt tape; color matches bug triangle.
    # Drawn AFTER the chevron so a parked chevron's top half hides behind
    # the box.
    alt_str = f"{round(alt_bug):5d}" if alt_bug is not None else "-----"
    alt_box_col = CYAN if baro_ok else MAGENTA
    _cyan_box(surf, alt_str, x=ALT_X + 1, y=2, w=ALT_W - 1, h=HDG_H,
              font_sz=22, col=alt_box_col)

    # Altitude readout box — Veeder-Root style scaled 1.5× from the SVG spec.
    # Layout: inner section(63) → drum section(36) → pointer(22) = 121px total.
    R = ALT_X + ALT_W   # right edge = 640
    pts_a = _chamfer([(R,      TAPE_MID),
                      (R - 22, TAPE_MID - 22), (R - 22, TAPE_MID - 44),
                      (R - 58, TAPE_MID - 44), (R - 58, TAPE_MID - 22),
                      (R - 121, TAPE_MID - 22),
                      (R - 121, TAPE_MID + 22),
                      (R - 58, TAPE_MID + 22), (R - 58, TAPE_MID + 44),
                      (R - 22, TAPE_MID + 44), (R - 22, TAPE_MID + 22)],
                     {2, 3, 4, 5, 6, 7, 8, 9})
    pygame.gfxdraw.filled_polygon(surf, pts_a, (0, 10, 30))

    # VSI readout — drawn BEFORE the outline so the 2px white line frames shared edges
    _R58  = ALT_X + ALT_W - 58    # left edge of drum section
    _nx   = ALT_X                  # flush with tape left edge
    _ny   = TAPE_MID + 22          # flush with inner-box bottom path
    _nw   = _R58 - ALT_X          # flush with drum-section left path
    _nh   = 22                     # readability strip below outer box
    if abs(vspeed) > 30:
        _varr = "▲" if vspeed > 0 else "▼"
        _vstr = f"{_varr}{abs(vspeed)/1000:.1f}"
        _vcol = (0, 220, 0) if vspeed > 0 else (255, 140, 0)
    else:
        _vstr = "—"
        _vcol = LTGREY
    pygame.draw.rect(surf, (0, 8, 22), (_nx, _ny, _nw, _nh), border_radius=3)
    pygame.draw.rect(surf, (70, 100, 130), (_nx, _ny, _nw, _nh), width=1, border_radius=3)
    _text(surf, _vstr, 13, _vcol, bold=True, cx=_nx + _nw // 2, cy=_ny + _nh // 2)

    # Inner: cascade from drum; carry starts when drum_pos > 4 (last 20 ft before rollover)
    # Geometry scaled 1.5× from the SVG spec to match the new outer box.
    carry_frac = max(0.0, (alt % 100) / 20 - 4.0)
    alt_inner  = float(alt // 100) + carry_frac
    inner_int  = round(alt_inner)
    if inner_int < 10:                      # alt < 1,000 ft — hundreds only
        _rolling_drum(surf, R - 120, TAPE_MID - 21, 62, 42, alt_inner, 1, WHITE, 36)
    elif inner_int < 100:                   # 1,000–9,999 ft — thousands + hundreds
        _rolling_drum(surf, R - 99, TAPE_MID - 21, 21, 42, alt_inner, 1, WHITE, 36,
                      power_offset=1)
        _rolling_drum(surf, R - 78, TAPE_MID - 21, 18, 42, alt_inner, 1, WHITE, 33)
    else:                                   # alt ≥ 10,000 ft
        _rolling_drum(surf, R - 120, TAPE_MID - 21, 42, 42, alt_inner, 2, WHITE, 33,
                      suppress_leading=True, power_offset=1)
        _rolling_drum(surf, R - 78, TAPE_MID - 21, 18, 42, alt_inner, 1, WHITE, 33)
    # Drum: 20-ft labels scroll together, adjacent labels half-visible
    _rolling_drum_alt20(surf, R - 57, TAPE_MID - 42, 33, 84, alt, WHITE, 27,
                        show_adjacent=True, adj_slot_h=27)
    _drum_shade(surf,   R - 57, TAPE_MID - 42, 33, 84)
    # Border drawn LAST so drum shade doesn't cover the inner pixels
    pygame.draw.polygon(surf, WHITE, pts_a, width=2)
    pygame.gfxdraw.aapolygon(surf, pts_a, WHITE)


# ── Heading tape ──────────────────────────────────────────────────────────────
_CARDINALS = {0: "N", 45: "NE", 90: "E", 135: "SE",
              180: "S", 225: "SW", 270: "W", 315: "NW"}


def draw_heading_tape(surf, hdg, hdg_bug=None, track=None, gps_ok=False, hdg_src="mag"):
    """Bottom heading strip with bug and current-heading box.

    hdg_src="gps" means hdg is already the GPS track value; the magenta track
    pointer is suppressed (it would just sit at centre) and the readout box
    shows a small "TRK" sub-label instead of "MAG".
    """
    # Cache the background plate — at 12+ fps on Pi Zero 2W the repeated
    # SDL surface alloc + free here was a significant memory churn,
    # especially in a sustained turn when the tape is repainted every
    # frame.  Allocate once, fill each call.
    global _hdg_tape_bg
    if _hdg_tape_bg is None:
        _hdg_tape_bg = pygame.Surface((DISPLAY_W, HDG_H), pygame.SRCALPHA)
    hdg_surf = _hdg_tape_bg
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
            _text(surf, lbl, 17, col, bold=True, cx=x, y=HDG_Y + HDG_H - 18)

    # Heading bug — color reflects source: magenta=GPS track, cyan=magnetic.
    # hdg_bug=0 means "not set" (same convention as alt_bug and spd_bug).
    if hdg_bug is not None:
        off = ((hdg_bug - hdg + 180) % 360) - 180
        hbx = int(CX + off * PX_PER_DEG)
        hbx = max(SPD_W, min(ALT_X, hbx))   # clamp to inner edges of tap buttons
        bug = [(hbx - 17, HDG_Y + 14), (hbx - 17, HDG_Y),
               (hbx - 5,  HDG_Y), (hbx, HDG_Y + 7), (hbx + 5, HDG_Y),
               (hbx + 17, HDG_Y), (hbx + 17, HDG_Y + 14)]
        hdg_bug_col = MAGENTA if hdg_src == "trk" else CYAN
        pygame.gfxdraw.filled_polygon(surf, bug, hdg_bug_col)
        pygame.gfxdraw.aapolygon(surf, bug, hdg_bug_col)

    # GPS track pointer (magenta, when GPS OK and heading source is MAG)
    # Suppressed in GPS TRK mode — hdg is already the track value.
    # Also suppressed when track ≈ hdg (within 1°) to avoid clutter at centre.
    if gps_ok and track is not None and hdg_src != "trk":
        off = ((track - hdg + 180) % 360) - 180
        if abs(off) > 1.0:  # only show when there's visible wind/crab angle
            tx = int(CX + off * PX_PER_DEG)
            if 0 < tx < DISPLAY_W:
                pygame.draw.polygon(surf, (220, 60, 220),
                    [(tx, HDG_Y + 4), (tx - 5, HDG_Y + 14), (tx + 5, HDG_Y + 14)])

    # Heading box — 99×42 (scaled 1.5x from 66×28). Triangle pointer
    # also scaled. GPS TRK mode → magenta. MAG mode → white.
    hdg_col = MAGENTA if hdg_src == "trk" else WHITE
    bw, bh = 99, 42
    bx, by2 = CX - bw // 2, HDG_Y - bh - 2
    th = bw // 3           # triangle base width ≈ 33px
    td = 21                # triangle depth (was 14)
    tx = CX - th // 2      # triangle left base x
    pts_h = _chamfer([(bx,      by2),
                      (bx + bw, by2),
                      (bx + bw, by2 + bh),
                      (tx + th, by2 + bh),
                      (CX,      by2 + bh + td),
                      (tx,      by2 + bh),
                      (bx,      by2 + bh)], {0, 1, 2, 6})
    pygame.gfxdraw.filled_polygon(surf, pts_h, (0, 0, 0))
    pygame.draw.polygon(surf, hdg_col, pts_h, width=2)
    pygame.gfxdraw.aapolygon(surf, pts_h, hdg_col)
    # Three-digit readout — perfectly centred in the box
    num_str  = f"{round(hdg) % 360:03d}"
    full_str = num_str + "\u00b0"
    f_hdg    = _get_font(26)
    _text(surf, full_str, 26, hdg_col, cx=CX, cy=by2 + bh // 2)
    # G/M subscript — outboard of the ° glyph, lower-right area of box
    full_w   = f_hdg.size(full_str)[0]
    deg_right = CX + full_w // 2 + 2
    src_lbl  = "G" if hdg_src == "trk" else "M"
    _text(surf, src_lbl, 12, hdg_col, x=deg_right, y=by2 + bh - 15)


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


def _update_terrain_alert(lat, lon, alt_ft, speed_kt, gps_ok, vso_kt=VS0):
    """
    Compute the current terrain/obstacle alert level and store it globally.
    Called once per render frame with current aircraft position and airspeed.
      0 — no alert
      1 — CAUTION  (clearance < TERRAIN_CAUTION_FT or obstacle < OBSTACLE_CAUTION_FT)
      2 — WARNING  (clearance < TERRAIN_WARNING_FT or obstacle < OBSTACLE_WARNING_FT)
    vso_kt is the user-set stall speed (flaps down) from the flight profile;
    alerts are inhibited below this groundspeed to silence taxi/rollout nuisance.
    """
    global _terrain_alert_level
    if not gps_ok:
        _terrain_alert_level = 0
        return

    # Inhibit terrain/obstacle alerts below Vso (taxi, rollout, etc.)
    if speed_kt < vso_kt:
        _terrain_alert_level = 0
        return

    level = 0

    # ── Terrain clearance (sampled at current position) ──────────────────────
    if _has_terrain:
        elev = get_elevation_ft(SRTM_DIR, lat, lon)
        clearance = alt_ft - elev
        if clearance < TERRAIN_WARNING_FT:
            level = max(level, 2)
        elif clearance < TERRAIN_CAUTION_FT:
            level = max(level, 1)

    # ── Obstacle clearance (time-based lookahead radius) ─────────────────────
    if _obstacles is not None:
        radius = _alert_radius_nm(speed_kt)
        nearby = obs_mod.query_nearby(_obstacles, lat, lon,
                                      radius_nm=radius,
                                      alt_ft=alt_ft,
                                      window_ft=OBSTACLE_CAUTION_FT)
        for ob in nearby:
            clearance = alt_ft - ob.msl_ft
            if clearance < OBSTACLE_WARNING_FT:
                level = max(level, 2)
                break
            elif clearance < OBSTACLE_CAUTION_FT:
                level = max(level, 1)

    _terrain_alert_level = level


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
def draw_status_badges(surf, ahrs_ok, gps_ok, baro_ok, baro_src, sats, connected,
                       hdg_src="mag"):
    """
    Badges are shown only when something requires pilot attention.
    Nominal state = clean strip.  Problem state = badge appears.

    Left  (from AI_X): AHRS FAIL, NO LINK, NO TER, NO OBS, EXP OBS, NO APT, EXP APT
    Right (to ALT_X):  GPS TRK (info), GPS ALT (only when baro absent),
                       GPS Xsat (acquiring), NO GPS (absent)
    """
    f10 = _get_font(10)

    # ── Left badges: problems only ──────────────────────────────────────────
    bx = AI_X + 4
    def badge_l(text, bg, fg=(255, 255, 255)):
        nonlocal bx
        w = f10.size(text)[0] + 10
        pygame.draw.rect(surf, bg, (bx, 4, w, 15))
        _text(surf, text, 10, fg, x=bx + 5, y=5)
        bx += w + 2

    if not ahrs_ok:
        badge_l("AHRS FAIL", (150, 0, 0))
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

    # ── Right badges: problems only ─────────────────────────────────────────
    rx = ALT_X - 4
    def badge_r(text, bg, fg=(255, 255, 255)):
        nonlocal rx
        w = f10.size(text)[0] + 10
        rx -= w + 2
        pygame.draw.rect(surf, bg, (rx, 4, w, 15))
        _text(surf, text, 10, fg, x=rx + 5, y=5)

    # GPS-slaved heading mode indicator — magenta badge (matches track-pointer colour)
    if hdg_src == "trk" and gps_ok:
        badge_r("GPS TRK", (70, 0, 70), (220, 80, 220))

    # Show GPS ALT only when baro sensor is absent (pilot needs to know alt source)
    if not baro_ok:
        badge_r("GPS ALT", (80, 80, 0), (220, 220, 100))

    # GPS state:
    #   fix valid          → no badge (clean)
    #   satellites visible → amber sat-count (acquiring, no fix yet)
    #   no satellites      → red NO GPS (hardware absent / no signal)
    if not gps_ok:
        if sats > 0:
            badge_r(f"GPS {sats}sat", (120, 80, 0), (220, 180, 60))
        else:
            badge_r("NO GPS", (150, 0, 0))


# ── Red-X failure overlays ────────────────────────────────────────────────────
def draw_red_x(surf, x, y, w, h, label):
    """Semi-transparent dark overlay with red X and label."""
    # Cache by (w, h) — there are only ~4 overlay sizes (attitude / hdg /
    # speed / alt) so the dict stays small.  Repeated alloc + free of
    # SRCALPHA surfaces was a hot allocator during sustained AHRS-fail
    # overlays (e.g. NO LINK during data_stale).
    key = (int(w), int(h))
    ov = _red_x_overlays.get(key)
    if ov is None:
        ov = pygame.Surface((w, h), pygame.SRCALPHA)
        ov.fill((20, 0, 0, 160))
        _red_x_overlays[key] = ov
    surf.blit(ov, (x, y))
    pygame.draw.line(surf, RED, (x + 4, y + 4), (x + w - 4, y + h - 4), 3)
    pygame.draw.line(surf, RED, (x + w - 4, y + 4), (x + 4, y + h - 4), 3)
    if label:
        _text(surf, label, 14, RED, bold=True, cx=x + w // 2, cy=y + h // 2 - 8)
        _text(surf, "FAIL", 14, RED, bold=True, cx=x + w // 2, cy=y + h // 2 + 8)


def draw_failure_overlays(surf, ahrs_ok, gps_ok, baro_ok, sats=0):
    ai_h_used = TAPE_H
    ai_y = TAPE_TOP
    ai_w = ALT_X - SPD_W
    if not ahrs_ok:
        # Cover AI center + heading strip
        draw_red_x(surf, SPD_W, ai_y, ai_w, ai_h_used, "ATTITUDE")
        draw_red_x(surf, 0, HDG_Y, DISPLAY_W, HDG_H, "HDG")
    # Red X on speed/alt tapes only when GPS is truly absent (no satellites).
    # While acquiring (sats > 0 but no fix) the tape stays live — data may
    # still be usable and the amber badge is sufficient warning.
    if not gps_ok and sats == 0:
        draw_red_x(surf, SPD_X, ai_y, SPD_W, ai_h_used, "AIRSPD")
    if not baro_ok and not gps_ok and sats == 0:
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
        # Seed bugs so the aircraft holds its initial state
        disp["hdg_bug"] = sim["init_hdg"]
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
            tgt_hdg = disp["hdg_bug"] if disp.get("hdg_bug") is not None else state["yaw"]
            tgt_alt = disp["alt_bug"] if disp.get("alt_bug") is not None else state["alt"]
            tgt_spd = disp.get("spd_bug") or 90.0

            # ── Heading / bank ─────────────────────────────────────────────────
            hdg     = state["yaw"]
            hdg_err = ((tgt_hdg - hdg + 180) % 360) - 180
            turn_rate = 3.0  # standard rate deg/s
            d_hdg = max(-turn_rate * dt, min(turn_rate * dt, hdg_err * 0.4))
            state["yaw"]  = (hdg + d_hdg) % 360
            bank          = max(-25.0, min(25.0, hdg_err * 1.8))
            state["roll"] = bank if not ahrs_fail else 0.0
            state["ay"]   = -bank / 600.0  # slip ball

            # ── Altitude / VS / pitch ──────────────────────────────────────────
            alt     = state["alt"]
            alt_err = tgt_alt - alt
            if abs(alt_err) < 5.0:
                state["alt"] = tgt_alt
                vs_fpm = 0.0
            else:
                vs_fpm  = max(-1500.0, min(1500.0, alt_err * 2.0))
                state["alt"] = alt + vs_fpm / 60.0 * dt
            state["gps_alt"] = state["alt"]
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
            nm_s           = state["speed"] / 3600.0
            hdg_rad        = math.radians(state["yaw"])
            nm_per_deg_lat = 60.0
            nm_per_deg_lon = max(1.0, 60.0 * math.cos(math.radians(state["lat"])))
            state["lat"]  += nm_s * dt * math.cos(hdg_rad) / nm_per_deg_lat
            state["lon"]  += nm_s * dt * math.sin(hdg_rad) / nm_per_deg_lon
            state["track"] = state["yaw"]

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
    if 8 <= x <= 80 and 6 <= y <= 37:
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


# ── Sim controls overlay ─────────────────────────────────────────────────────

_SIMCTRL_W = 280
_SIMCTRL_H = 200
_SIMCTRL_X = (DISPLAY_W - _SIMCTRL_W) // 2
_SIMCTRL_Y = (DISPLAY_H - _SIMCTRL_H) // 2 - 10

_SIMCTRL_ROW_Y0  = _SIMCTRL_Y + 36   # first sensor row top
_SIMCTRL_ROW_H   = 34
_SIMCTRL_ROW_GAP = 4
_SIMCTRL_BW      = 70     # ON / FAIL button width


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

        # Row label
        _text(surf, label, 12, (160, 175, 200), bold=True,
              x=_SIMCTRL_X + 14, cy=row_y + _SIMCTRL_ROW_H // 2)

        # ON button
        on_active = not failed
        on_bg = (0, 50, 20) if on_active else (0, 8, 16)
        on_oc = (40, 190, 60) if on_active else (35, 60, 45)
        on_tc = (60, 220, 80) if on_active else (60, 100, 75)
        ox = _SIMCTRL_X + _SIMCTRL_W - 2 * _SIMCTRL_BW - 8 - 6
        pygame.draw.rect(surf, on_bg, (ox, row_y, _SIMCTRL_BW, _SIMCTRL_ROW_H), border_radius=4)
        pygame.draw.rect(surf, on_oc, (ox, row_y, _SIMCTRL_BW, _SIMCTRL_ROW_H), width=2, border_radius=4)
        _text(surf, "ON", 12, on_tc, bold=on_active,
              cx=ox + _SIMCTRL_BW // 2, cy=row_y + _SIMCTRL_ROW_H // 2)

        # FAIL button
        fx = ox + _SIMCTRL_BW + 6
        fail_active = failed
        fail_bg = (50, 5, 5) if fail_active else (12, 0, 0)
        fail_oc = (200, 40, 40) if fail_active else (75, 30, 30)
        fail_tc = RED if fail_active else (110, 55, 55)
        pygame.draw.rect(surf, fail_bg, (fx, row_y, _SIMCTRL_BW, _SIMCTRL_ROW_H), border_radius=4)
        pygame.draw.rect(surf, fail_oc, (fx, row_y, _SIMCTRL_BW, _SIMCTRL_ROW_H), width=2, border_radius=4)
        _text(surf, "FAIL", 11, fail_tc, bold=fail_active,
              cx=fx + _SIMCTRL_BW // 2, cy=row_y + _SIMCTRL_ROW_H // 2)

    # EXIT SIM button
    exit_y = _SIMCTRL_ROW_Y0 + len(sensors) * (_SIMCTRL_ROW_H + _SIMCTRL_ROW_GAP) + 6
    _action_btn(surf,
                _SIMCTRL_X + 14, exit_y,
                _SIMCTRL_W - 28, _SIMCTRL_H - (exit_y - _SIMCTRL_Y) - 10,
                "EXIT SIM", "danger")


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

    # EXIT SIM button area
    exit_y = _SIMCTRL_ROW_Y0 + len(sensors) * (_SIMCTRL_ROW_H + _SIMCTRL_ROW_GAP) + 6
    exit_h = _SIMCTRL_H - (exit_y - _SIMCTRL_Y) - 10
    if (exit_y <= y <= exit_y + exit_h and
            _SIMCTRL_X + 14 <= x <= _SIMCTRL_X + _SIMCTRL_W - 14):
        return "exit_sim"

    return "noop"   # tapped inside panel but not on a control — consume event


# ── Touch handler ─────────────────────────────────────────────────────────────
_touch_t0      = {}
_bug_dragging  = None    # "hdg" | "alt"
_active_fingers = {}     # finger_id → touch-down time (ms)
_multitouch_t0  = None   # time when 2nd finger touched down
_multitouch_max_fingers = 0   # peak finger count this gesture — disambiguates
                              # the 2-finger setup hold (max == 2) from the
                              # 3-finger PFD↔MFD swap hold (max >= 3) so the
                              # two gestures don't fight at the 800 ms mark.


def _current_str_for_kbd(target, prev_mode):
    """String form of current keyboard-editable value for pre-population."""
    if prev_mode == "connectivity_setup":
        v = disp["cs"].get(target, "")
    else:
        v = disp["fp"].get(target, "")
    return str(v) if v not in (None, 0, "") else ""


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
    global _bug_dragging, _active_fingers, _multitouch_t0
    global _multitouch_max_fingers, _sim_state

    if event.type == pygame.QUIT:
        return False

    # ── Keyboard shortcuts ────────────────────────────────────────────────────
    if event.type == pygame.KEYDOWN:
        if event.key == pygame.K_ESCAPE:
            if disp["mode"] != "pfd":
                disp["mode"] = "pfd"   # ESC exits any overlay
            else:
                return False
        if event.key == pygame.K_d:
            return "toggle_demo"
        if disp["mode"] == "pfd":
            if event.key == pygame.K_UP:
                disp["alt_bug"] = round(disp["alt_bug"] / 100) * 100 + 100
                _ssync_publish_bugs()
            if event.key == pygame.K_DOWN:
                disp["alt_bug"] = round(disp["alt_bug"] / 100) * 100 - 100
                _ssync_publish_bugs()
            if event.key == pygame.K_LEFT:
                disp["hdg_bug"] = (round(disp["hdg_bug"]) - 10) % 360
                _ssync_publish_bugs()
            if event.key == pygame.K_RIGHT:
                disp["hdg_bug"] = (round(disp["hdg_bug"]) + 10) % 360
                _ssync_publish_bugs()
            if event.key in (pygame.K_PLUS, pygame.K_EQUALS):
                disp["baro_hpa"] = round(disp["baro_hpa"] * 100 + 1) / 100
                _ssync_publish_baro()
            if event.key == pygame.K_MINUS:
                disp["baro_hpa"] = round(disp["baro_hpa"] * 100 - 1) / 100
                _ssync_publish_baro()

    # ── Multi-finger tracking (FINGERDOWN / FINGERUP only) ───────────────────
    global _mfd_drag
    if event.type == pygame.FINGERDOWN:
        _active_fingers[event.finger_id] = pygame.time.get_ticks()
        if len(_active_fingers) >= 2 and _multitouch_t0 is None:
            _multitouch_t0 = pygame.time.get_ticks()
        # Track peak finger count so the 3-finger swap gesture wins over
        # the 2-finger setup gesture once a 3rd finger has touched, even
        # if one of them lifts before the hold timer expires.
        if len(_active_fingers) > _multitouch_max_fingers:
            _multitouch_max_fingers = len(_active_fingers)
            # The first finger may have started an MFD pan / airport-tap
            # drag.  Cancel it so the eventual finger-up doesn't fire
            # _mfd_airport_tap or finish a pan from the first finger's
            # path.  Lets a two-finger hold cleanly enter setup mode.
            _mfd_drag = None

    if event.type == pygame.FINGERUP:
        _active_fingers.pop(event.finger_id, None)
        if len(_active_fingers) < 2:
            _multitouch_t0 = None
            _multitouch_max_fingers = 0

    # ── Drag-to-scroll on setup screens ──────────────────────────────────────
    # We defer the tap-fire on BUTTONDOWN inside a drag-capable setup
    # screen, watch MOTION to detect a scroll-drag, and on BUTTONUP either
    # consume the drag (no action fires) or replay the tap at the up
    # position so the underlying row-hit code runs as if nothing happened.
    global _ss_drag, _dispatch_replay, _fpl_drag, _fpl_scroll
    if event.type in (pygame.MOUSEMOTION, pygame.FINGERMOTION) and _ss_drag is not None:
        pos = event.pos if hasattr(event, "pos") else (
            int(event.x * DISPLAY_W), int(event.y * DISPLAY_H))
        x, y = pos
        dy = y - _ss_drag["down_y"]
        if not _ss_drag["is_drag"] and abs(dy) > _SS_DRAG_THRESHOLD:
            _ss_drag["is_drag"] = True
        if _ss_drag["is_drag"]:
            mode = _ss_drag["mode"]
            n_rows = _SS_DRAG_MODES.get(mode, 5)
            max_s = _ss_max_scroll(n_rows)
            new_scroll = _ss_drag["scroll_at_down"] - dy
            _ss_scroll[mode] = max(0, min(max_s, new_scroll))
        _ss_drag["pos"] = (x, y)
        return True

    if event.type in (pygame.MOUSEBUTTONUP, pygame.FINGERUP) and _ss_drag is not None:
        d = _ss_drag
        _ss_drag = None
        if not d["is_drag"]:
            # Replay the tap at the UP position as a synthetic
            # MOUSEBUTTONDOWN so the existing dispatch logic runs once.
            _dispatch_replay = True
            try:
                fake = pygame.event.Event(
                    pygame.MOUSEBUTTONDOWN,
                    {"pos": d["pos"], "button": 1})
                handle_event(fake, demo_mode)
            finally:
                _dispatch_replay = False
        return True

    # ── MFD pan / airport-tap drag handler ──────────────────────────────────
    # Same defer-replay pattern as scroll-drag: DOWN over the MFD's map
    # area is held until MOTION exceeds the threshold (→ pan) or UP fires
    # with no significant motion (→ airport hit-test).
    # ── FPL list scroll drag ────────────────────────────────────────────────
    # Defer-replay pattern: tap inside the list area starts a candidate
    # drag; motion beyond _FPL_DRAG_THRESHOLD converts to scroll; UP
    # without motion fires the original tap (so row buttons still work).
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
            _fpl_scroll = max(0, min(max_s,
                                      _fpl_drag["scroll_at_down"] - dy))
        _fpl_drag["pos"] = pos
        return True

    if event.type in (pygame.MOUSEBUTTONUP, pygame.FINGERUP) and _fpl_drag is not None:
        d = _fpl_drag
        _fpl_drag = None
        if not d["is_drag"]:
            # Tap — replay so the existing FPL-tap dispatch handles it.
            _dispatch_replay = True
            try:
                fake = pygame.event.Event(
                    pygame.MOUSEBUTTONDOWN,
                    {"pos": d["pos"], "button": 1})
                handle_event(fake, demo_mode)
            finally:
                _dispatch_replay = False
        return True

    if event.type in (pygame.MOUSEMOTION, pygame.FINGERMOTION) and _mfd_drag is not None:
        pos = event.pos if hasattr(event, "pos") else (
            int(event.x * DISPLAY_W), int(event.y * DISPLAY_H))
        x, y = pos
        dx = x - _mfd_drag["down_x"]
        dy = y - _mfd_drag["down_y"]
        if (not _mfd_drag["is_drag"]
                and (abs(dx) > _MFD_DRAG_THRESHOLD
                     or abs(dy) > _MFD_DRAG_THRESHOLD)):
            _mfd_drag["is_drag"] = True
        if _mfd_drag["is_drag"]:
            _mfd_apply_drag(_mfd_drag, dx, dy)
        _mfd_drag["pos"] = (x, y)
        return True

    if event.type in (pygame.MOUSEBUTTONUP, pygame.FINGERUP) and _mfd_drag is not None:
        d = _mfd_drag
        _mfd_drag = None
        if not d["is_drag"]:
            # Tap: try airport hit-test.  If nothing nearby, the tap is a
            # no-op (no fall-through to chrome — chrome was already filtered
            # out at DOWN time).
            _mfd_airport_tap(*d["pos"])
        return True

    # ── Single-touch / mouse ──────────────────────────────────────────────────
    if event.type in (pygame.MOUSEBUTTONDOWN, pygame.FINGERDOWN):
        # Skip if this is part of a multi-touch gesture
        if len(_active_fingers) >= 2:
            return True

        pos = event.pos if hasattr(event, "pos") else (
            int(event.x * DISPLAY_W), int(event.y * DISPLAY_H))
        x, y = pos

        mode = disp["mode"]

        # Defer this tap on drag-capable setup screens — except taps inside
        # the title bar (back button), which still fire immediately so the
        # user can always escape.
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

        # FPL list has its own scrollable area below the action bar;
        # taps in the action bar / header fire immediately, taps in
        # the list area defer for a possible drag.
        if not _dispatch_replay and mode == "fpl":
            list_top, list_bot = _fpl_list_area_y()
            if list_top <= y <= list_bot:
                _fpl_drag = {
                    "down_x":         x,
                    "down_y":         y,
                    "pos":            (x, y),
                    "scroll_at_down": _fpl_scroll,
                    "is_drag":        False,
                }
                return True

        # ── Setup screen taps ─────────────────────────────────────────────
        if mode == "setup":
            idx = setup_hit(x, y)
            # Indices follow _SETUP_ITEMS row-major order: 0 FLIGHT, 1 DISPLAY,
            # 2 AHRS, 3 CONNECTIVITY, 4 SCREEN SYNC, 5 SYSTEM, 6 EXIT.
            if   idx == 6: disp["mode"] = "pfd"
            elif idx == 0:
                _ss_reset_scroll("flight_profile")
                disp["mode"] = "flight_profile"
            elif idx == 1:
                _ss_reset_scroll("display_setup")
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
            elif action and action.startswith("set:"):
                _, key, val_str = action.split(":", 2)
                # Convert "True" / "False" strings back to bools for any
                # boolean-valued setting (night_mode + all map_show_*).
                if val_str in ("True", "False"):
                    disp["ds"][key] = (val_str == "True")
                else:
                    disp["ds"][key] = val_str
                _settings.mark_dirty()
            elif action and action.startswith("inc:brightness:"):
                delta = int(action.split(":")[-1])
                disp["ds"]["brightness"] = max(1, min(10, disp["ds"]["brightness"] + delta))
                _set_backlight(disp["ds"]["brightness"])
                _settings.mark_dirty()
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
            elif action == "mag_cal_open":
                _mag_cal_open("ahrs_setup")
            elif action and action.startswith("set:orientation:"):
                new_ori = action.split(":", 2)[2]
                disp["ss"]["orientation"] = new_ori
                _push_orient_to_pico(new_ori,
                                     disp["ss"].get("mounting",
                                                    state.get("mounting", "normal")))
                _settings.mark_dirty()
                return True
            elif action and action.startswith("set:mounting:"):
                new_mnt = action.split(":", 2)[2]
                disp["ss"]["mounting"] = new_mnt
                _push_orient_to_pico(
                    disp["ss"].get("orientation",
                                   state.get("orientation", "right")),
                    new_mnt)
                _settings.mark_dirty()
                return True
            elif action and action.startswith("set:"):
                _, key, val = action.split(":", 2)
                disp["ss"][key] = val
                _settings.mark_dirty()
            return True

        # ── Nav-confirm modal taps ────────────────────────────────────────
        if mode == "nav_confirm":
            action = nav_confirm_hit(x, y)
            if action == "activate":
                _nav_confirm_apply()
            elif action == "cancel":
                _nav_confirm_cancel()
            return True

        # ── Mag-cal modal taps ────────────────────────────────────────────
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
            elif action == "test_internet":
                disp["cs"]["inet_msg"] = "Testing internet…"
                def _do_inet():
                    _ok, msg = _test_internet()
                    disp["cs"]["inet_msg"] = msg
                threading.Thread(target=_do_inet, daemon=True).start()
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

        # ── +LAT/LON entry screen taps ────────────────────────────────────
        if mode == "fpl_latlon_entry":
            act, payload = fpl_latlon_entry_hit(x, y)
            if act in ("back", "cancel"):
                disp["fle_err_field"] = ""
                disp["fle_err_msg"]   = ""
                disp["mode"] = "fpl"
            elif act == "edit":
                _fle_open_kbd(payload)
            elif act == "save":
                field, msg = _fpl_commit_latlon()
                if field:
                    disp["fle_err_field"] = field
                    disp["fle_err_msg"]   = msg
                else:
                    disp["fle_err_field"] = ""
                    disp["fle_err_msg"]   = ""
                    disp["mode"] = "fpl"
            return True

        # ── User-waypoint picker (+ LIB) taps ─────────────────────────────
        if mode == "user_wpt_picker":
            act, payload = user_wpt_picker_hit(x, y)
            if act == "back":
                disp["mode"] = "fpl"
            elif act == "add" and payload is not None:
                # Collision check: refuse if already in plan.  Airport-
                # DB collision is impossible because user_wpt_save
                # already rejected that ident when it was added.
                ident = str(payload.get("ident", ""))
                in_plan = any(str(w.get("ident", "")).upper() == ident.upper()
                              for w in disp["fpl"]["waypoints"])
                if not in_plan:
                    _fpl_add_waypoint(ident,
                                       float(payload["lat"]),
                                       float(payload["lon"]),
                                       elev_ft=float(payload.get("elev_ft", 0.0)),
                                       user=True)
            elif act == "delete" and payload is not None:
                _user_wpt_delete(str(payload.get("ident", "")))
            return True

        # ── FPL screen taps ───────────────────────────────────────────────
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
            elif act == "deact":
                _fpl_deactivate()
            elif act == "activate":
                _fpl_activate(payload, reset_activation=True)
            elif act == "up" and payload > 0:
                _fpl_swap(payload, payload - 1)
            elif act == "down":
                _fpl_swap(payload, payload + 1)
            elif act == "delete":
                _fpl_remove(payload)
            return True

        # ── MFD strip-setup chooser taps ──────────────────────────────────
        if mode == "mfd_strip_setup":
            act, payload = mfd_strip_setup_hit(x, y)
            if act == "back":
                disp["mode"] = "pfd"   # MFD runs under mode=pfd
            elif act == "slot":
                disp["mss_sel"] = int(payload)
            elif act == "kind":
                kinds = _mfd_strip_kinds()
                sel = int(disp.get("mss_sel", 0)) % _MFD_STRIP_SLOT_COUNT
                kinds[sel] = payload
                disp["ds"]["mfd_strip_kinds"] = kinds
                # Auto-advance to the next slot for fast keyboard-style
                # configuration; wraps at the end.
                disp["mss_sel"] = (sel + 1) % _MFD_STRIP_SLOT_COUNT
                _settings.mark_dirty()
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

        # ── System screen taps ────────────────────────────────────────────────────────────────────
        # ── AHRS firmware screen taps ─────────────────────────────────────
        if mode == "ahrs_firmware":
            action = ahrs_firmware_hit(x, y)
            if action == "back":
                disp["mode"] = "system_setup"
            elif action == "push_scripts":
                _do_push_scripts()
            elif action == "flash_uf2":
                _do_flash_uf2()
            return True

        if mode == "system_setup":
            action = system_setup_hit(x, y)
            if action == "back":
                disp["mode"] = "setup"
            elif action == "terrain_data":
                disp["mode"] = "terrain_data"
            elif action == "obstacle_data":
                disp["mode"] = "obstacle_data"
            elif action == "airport_data":
                disp["mode"] = "airport_data"
            elif action == "airspace_data":
                disp["mode"] = "airspace_data"
            elif action == "ahrs_firmware":
                _ss_reset_scroll("ahrs_firmware")
                disp["mode"] = "ahrs_firmware"
            elif action and action.startswith("set:mfd_enabled:"):
                want = action.split(":")[-1] == "on"
                disp["ds"]["mfd_enabled"] = want
                if not want:
                    # Disabling the feature also forces the runtime view
                    # back to PFD — otherwise a piZ that was last left on
                    # the MFD would still render MFD on next render.
                    disp["display_mode"] = "pfd"
                _settings.mark_dirty()
                disp["mode"] = "pfd"   # exit setup so the change is visible
            elif action == "simulator":
                disp["mode"] = "sim_setup"
            elif action == "quit":
                _settings.flush()
                pygame.quit()
                sys.exit(0)
            elif action == "reset_defaults":
                for k,v in [("vs0",VS0),("vs1",VS1),("vfe",VFE),("vno",VNO),
                             ("vne",VNE),("va",VA),("vy",VY),("vx",VX)]:
                    disp["fp"][k] = v
                disp["ds"].update(spd_unit="kt", alt_unit="ft", baro_unit="inhg",
                                   brightness=8, night_mode=False)
                disp["ss"].update(pitch_trim=0.0, roll_trim=0.0)
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
            elif action and action.startswith("sensor_on:"):
                sensor = action.split(":")[1]
                disp["sim"][sensor + "_fail"] = False
            elif action and action.startswith("sensor_fail:"):
                sensor = action.split(":")[1]
                disp["sim"][sensor + "_fail"] = True
            # "noop" or None: consume the event either way
            return True

        # ── Obstacle data screen taps ─────────────────────────────────────
        if mode == "obstacle_data":
            action = obstacle_data_hit(x, y, disp["od"])
            if action == "back":
                disp["mode"] = "system_setup"
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
                disp["mode"] = "system_setup"
            elif action == "cancel":
                disp["ad"]["dl_cancel"] = True
            elif action == "download":
                if not disp["ad"]["downloading"]:
                    _ad_start_download()
            elif isinstance(action, str) and action.startswith("toggle:"):
                key = action.split(":", 1)[1]
                disp["ad"][key] = not disp["ad"].get(key, False)
                _settings.mark_dirty()
            return True

        # ── Airspace data screen taps ─────────────────────────────────────
        if mode == "airspace_data":
            action = airspace_data_hit(x, y)
            if action == "back":
                disp["mode"] = "system_setup"
            elif action == "download":
                asp = disp["asp"]
                if asp.get("downloading"):
                    asp["dl_cancel"] = True
                else:
                    _asp_start_download()
            return True

        # ── Terrain data screen taps ──────────────────────────────────────
        if mode == "terrain_data":
            action = terrain_data_hit(x, y, disp["td"])
            if action == "back":
                disp["mode"] = "system_setup"
            elif action == "cancel":
                if disp["td"].get("downloading"):
                    disp["td"]["dl_cancel"] = True
                if disp["wd"].get("downloading"):
                    disp["wd"]["dl_cancel"] = True
                if disp["td"].get("compacting"):
                    disp["td"]["dl_cancel"] = True
            elif action == "current_area":
                if not (disp["td"].get("downloading")
                        or disp["td"].get("compacting")):
                    _td_start_current_area()
            elif action == "global_coarse":
                if not (disp["td"].get("downloading")
                        or disp["td"].get("compacting")):
                    _tdc_start_download()
            elif action == "water_masks":
                if not disp["wd"].get("downloading"):
                    _wd_start_download()
            elif action == "compact":
                if not (disp["td"].get("downloading")
                        or disp["td"].get("compacting")):
                    _td_start_compact()
            elif action and action.startswith("region:"):
                if not (disp["td"].get("downloading")
                        or disp["td"].get("compacting")):
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
                _CS_MAX = {"wifi_ssid": 32, "wifi_pass": 63, "ahrs_url": 80}
                _FPL_MAX = {"fpl_ident": 6,
                            "fpl_latlon_ident": 6,
                            "fpl_latlon_lat": 12, "fpl_latlon_lon": 12,
                            "nav_ident": 6}
                if target in _FPL_MAX:
                    max_len = _FPL_MAX[target]
                elif disp.get("kbd_prev") == "connectivity_setup":
                    max_len = _CS_MAX.get(target, 32)
                else:
                    max_len = next((f[3] for f in _FP_FIELDS if f[0]==target), 16)
                if sty == 'n':                # character / space
                    ch = ' ' if lbl == 'SPACE' else lbl
                    if len(disp["kbd_buf"]) < max_len:
                        disp["kbd_buf"] += ch
                    disp["kbd_error"] = ""
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
                    nearest = _nav_lookup_nearest()
                    prev = disp["kbd_prev"]
                    disp["kbd_buf"] = ""
                    disp["kbd_error"] = ""
                    if not _nav_open_confirm(nearest, prev):
                        disp["mode"] = prev
                elif sty == 'clrfp':          # CANCEL D2
                    _nav_clear()
                    disp["kbd_buf"] = ""
                    disp["kbd_error"] = ""
                    disp["mode"] = disp["kbd_prev"]
                elif sty == 'ok':             # DONE
                    buf = disp["kbd_buf"].strip()
                    if target == "fpl_ident":
                        # FPL append — look up the ident in the
                        # airport DB, append to disp["fpl"]["waypoints"],
                        # return to the FPL screen.  Empty buf cancels.
                        if not buf:
                            disp["kbd_buf"]   = ""
                            disp["kbd_error"] = ""
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
                            disp["kbd_buf"]   = ""
                            disp["kbd_error"] = ""
                            disp["mode"] = "fpl"
                        else:
                            disp["kbd_error"] = f"PLAN FULL ({_FPL_MAX_WAYPOINTS} MAX)"
                        return True
                    if target == "fpl_latlon_ident":
                        # +LAT/LON ident field: store and return to the
                        # entry screen so the pilot can fill the rest.
                        disp["fpl_new"]["ident"] = buf.upper()[:6]
                        disp["kbd_buf"]   = ""
                        disp["kbd_error"] = ""
                        disp["mode"] = "fpl_latlon_entry"
                        return True
                    if target in ("fpl_latlon_lat", "fpl_latlon_lon"):
                        # Store raw string; validation runs on SAVE.
                        axis = "lat" if target.endswith("lat") else "lon"
                        disp["fpl_new"][f"{axis}_str"] = buf
                        # Eagerly parse so the entry screen shows a
                        # green tick / red strike-through as feedback.
                        v, _err = _fpl_parse_latlon(buf, axis)
                        if v is not None:
                            disp["fpl_new"][axis] = v
                        disp["kbd_buf"]   = ""
                        disp["kbd_error"] = ""
                        disp["mode"] = "fpl_latlon_entry"
                        return True
                    if target == "nav_ident":
                        # Direct-to entry — three paths (pi4 parity):
                        #   1. Empty buf + active waypoint → re-confirm
                        #      so the magenta line redraws from current pos.
                        #   2. Typed buf resolves to a known airport →
                        #      open the nav_confirm modal.
                        #   3. Typed buf doesn't resolve → stay on the
                        #      keyboard with an UNKNOWN WAYPOINT error.
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
                        disp["kbd_buf"]   = ""
                        disp["kbd_error"] = ""
                        disp["mode"] = disp["kbd_prev"]
                        return True
                    if buf:
                        if disp["kbd_prev"] == "connectivity_setup":
                            disp["cs"][target] = buf
                            # Changing AHRS URL live-restarts the SSE stream
                            if target == "ahrs_url":
                                _restart_sse(buf)
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
                        # User types in current display unit; storage stays
                        # canonical (kt/ft).  Divide by factor at commit.
                        ds = disp["ds"]
                        spd_factor = {"kt": 1.0, "mph": 1.15078,
                                      "kph": 1.852}.get(ds.get("spd_unit", "kt"), 1.0)
                        alt_factor = {"ft": 1.0,
                                      "m":  0.3048}.get(ds.get("alt_unit", "ft"), 1.0)
                        if target == "alt_bug":
                            disp["alt_bug"] = float(val * 100) / alt_factor
                            _ssync_publish_bugs()
                        elif target == "hdg_bug":
                            disp["hdg_bug"] = float(val % 360)
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
                            disp["sim"]["init_spd"] = float(val)
                        elif target in disp["fp"]:   # V-speed field (always kt)
                            disp["fp"][target] = val
                        _settings.mark_dirty()
                    disp["mode"] = disp["numpad_prev"]
                    disp["numpad_buf"] = ""
            return True

        # ── MFD taps (display_mode == "mfd" while mode == "pfd") ──────────
        if mode == "pfd" and disp.get("display_mode", "pfd") == "mfd":
            if _mfd_fpl_btn_hit(x, y):
                # Open the flight-plan editor (placeholder until multi-
                # waypoint plans + user waypoints land).  PFD ↔ MFD
                # swap is now the 3-finger 2-s hold gesture.
                disp["mode"] = "fpl"
                return True
            if _mfd_d2_btn_hit(x, y):
                # Open the existing keyboard for waypoint entry.
                _mfd_open_d2_keyboard()
                return True
            if _mfd_zoom_in_hit(x, y):
                cur = int(disp["ds"].get("map_zoom_nm", 10))
                disp["ds"]["map_zoom_nm"] = _mfd_map.zoom_in(cur)
                _settings.mark_dirty()
                return True
            if _mfd_zoom_out_hit(x, y):
                cur = int(disp["ds"].get("map_zoom_nm", 10))
                new = _mfd_map.zoom_out(
                    cur, allow_auto=bool(disp.get("nav", {}).get("ident")))
                # AUTO is 0 — let it through.  Otherwise clamp to the
                # cap so a saved range never exceeds what pi_zero can
                # render safely.
                if new > 0 and new > MFD_MAX_ZOOM_NM:
                    new = MFD_MAX_ZOOM_NM
                disp["ds"]["map_zoom_nm"] = new
                _settings.mark_dirty()
                return True
            if _mfd_center_btn_hit(x, y):
                _mfd_clear_pan()
                return True
            if _mfd_orient_label_hit(x, y):
                cur = disp["ds"].get("map_orient", "trk")
                disp["ds"]["map_orient"] = "nrth" if cur == "trk" else "trk"
                _settings.mark_dirty()
                return True
            if _mfd_strip_hit(x, y):
                disp["mode"] = "mfd_strip_setup"
                disp["mss_sel"] = 0   # currently-selected slot index
                return True
            # Anywhere else over the map → start a pan/tap drag.  MOTION
            # converts to pan; UP without motion runs airport hit-test.
            if not _dispatch_replay and not _mfd_chrome_hit(x, y):
                cen_lat, cen_lon = _mfd_effective_center()
                hdg = disp.get("yaw", 0.0)
                track = disp.get("track", hdg)
                orient = disp["ds"].get("map_orient", "trk")
                range_nm = int(disp["ds"].get("map_zoom_nm", 10))
                rot_deg = _mfd_map._rot_deg_for(orient, hdg, track)
                px_per_nm = min(DISPLAY_W, DISPLAY_H) / 2.0 / max(0.5, range_nm)
                _mfd_drag = {
                    "down_x":   x,
                    "down_y":   y,
                    "pos":      (x, y),
                    "is_drag":  False,
                    "base_lat": cen_lat,
                    "base_lon": cen_lon,
                    "rot_deg":  rot_deg,
                    "px_per_nm": px_per_nm,
                }
                return True

        # ── PFD taps ──────────────────────────────────────────────────────
        # Tap on the CDI strip → open the keyboard for waypoint entry.
        # Matches pi4 behaviour; the keyboard ENTER handler routes through
        # the nav_confirm modal.
        if (mode == "pfd"
                and disp.get("display_mode", "pfd") == "pfd"
                and disp.get("gps_ok", False)
                and _cdi_hit(x, y)):
            _mfd_open_d2_keyboard()
            return True

        # Tap on SIM watermark → open sim controls overlay
        if _sim_state is not None and mode == "pfd":
            if CX - 30 <= x <= CX + 30 and CY - 30 <= y <= CY - 10:
                disp["mode"] = "sim_controls"
                return True

        # Tap on alt bug button → open numpad
        if ALT_X <= x <= DISPLAY_W and 2 <= y <= 24:
            _open_numpad("alt_bug")
            return True
        # Tap on GS bug button → open numpad
        if SPD_X <= x <= SPD_X + SPD_W and 2 <= y <= 24:
            _open_numpad("spd_bug")
            return True
        # Tap on hdg bug button → open numpad
        if SPD_X <= x <= SPD_X + SPD_W and HDG_Y + 2 <= y <= HDG_Y + 24:
            _open_numpad("hdg_bug")
            return True
        # Tap on baro button → open numpad
        if ALT_X <= x <= DISPLAY_W and HDG_Y + 2 <= y <= HDG_Y + 24:
            _open_numpad("baro_hpa")
            return True
        # Tap on alt tape → adjust alt bug by position
        if ALT_X <= x <= DISPLAY_W and TAPE_TOP <= y <= TAPE_BOT:
            ft = round(disp["alt"] + (TAPE_MID - y) / PX_PER_FT)
            disp["alt_bug"] = round(ft / 100) * 100
            _ssync_publish_bugs()
        # Tap on heading tape → adjust hdg bug by position
        if HDG_Y <= y <= DISPLAY_H:
            off = (x - CX) / PX_PER_DEG
            disp["hdg_bug"] = round(disp["yaw"] + off) % 360
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
    """Return index 0–5 of the tapped setup button, or None."""
    for idx, (col, row, *_) in enumerate(_SETUP_ITEMS):
        bx = _S_COLS[col]; by = _S_ROWS[row]
        if bx <= x <= bx+_S_BW and by <= y <= by+_S_BH:
            return idx
    return None


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
    # Two-line label when the V-speed code is followed by an em-dash and
    # description (e.g. "VS0 — Stall flaps"); single-line for simple
    # labels like "TAIL NUMBER".  Keeps the long descriptive label off
    # the value field on the right.
    em_dash = " — "
    if em_dash in label:
        code, desc = label.split(em_dash, 1)
        _text(surf, code, 20, (180,195,220), bold=True, x=bx+14, y=by+6)
        _text(surf, desc, 13, (140,160,185), x=bx+14, y=by+bh-20)
    else:
        _text(surf, label, 20, (180,195,220), bold=True, x=bx+14, y=by+8)
    val_str = str(value) if value not in (None, "", 0) else "---"
    if units and val_str != "---":
        val_str = f"{val_str} {units}"
    _text(surf, val_str, 26, WHITE, bold=True,
          cx=bx+bw - _get_font(26,bold=True).size(val_str)[0]//2 - 14,
          cy=by+bh//2)


def draw_flight_profile(surf, fp_vals):
    """Full-screen Flight Profile setup screen."""
    _screen_header(surf, "FLIGHT PROFILE")
    _prev_clip = _ss_clip_to_content(surf)

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
    _text(surf, "V-SPEEDS  (knots) \u2014 tap to edit", 14, (140,160,185), x=MX, y=y)
    y += 22

    # V-speed grid: 4 rows × 2 cols
    V_KEYS = [k for k,*_ in _FP_FIELDS if k not in ("tail","actype")]
    BW = (FW - GAP) // 2
    BH = (DISPLAY_H - y - GAP*3 - 4) // 4
    COLS = [MX, MX+BW+GAP]
    for i, key in enumerate(V_KEYS):
        _, label, units, _, _ = next(f for f in _FP_FIELDS if f[0]==key)
        bx = COLS[i%2]; by = y + (i//2)*(BH+GAP)
        _fp_field(surf, bx, by, BW, BH, label, fp_vals.get(key,"---"), units)
    surf.set_clip(_prev_clip)


def flight_profile_hit(x, y, fp_vals):
    """Return the field key tapped, or None."""
    MX=_FP_MX; GAP=_FP_GAP; FW=DISPLAY_W-2*MX
    # BACK button
    if 8<=x<=80 and 6<=y<=37:
        return "__back__"
    # Aircraft fields
    fy = _FP_Y0
    for key in ("tail","actype"):
        if MX<=x<=MX+FW and fy<=y<=fy+_FP_H1:
            return key
        fy += _FP_H1+GAP
    # V-speed grid
    fy += 30   # divider + label (must match the draw-side y bump above)
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

# Nav-ident extras row: NEAREST · CANCEL D2 below the QWERTY when the
# keyboard is open for waypoint entry.  Compresses the row height so the
# two action buttons fit on the 480-px Pi Zero panel.
_KB_NAV_ROW_H  = 58
_KB_NAV_BTN_H  = 38


def _kb_nav_extras_visible():
    return disp.get("kbd_target") == "nav_ident"


def _kb_row_h():
    return _KB_NAV_ROW_H if _kb_nav_extras_visible() else _KB_ROW_H


def _kb_nav_extras_y():
    rh = _kb_row_h()
    return _KB_Y0 + 5 * rh + 4 * _KB_GAP_Y + 8


def _kb_nav_extras_geometry():
    """(bx_l, bx_r, btn_w) for the two nav-ident extras buttons:
    DIRECT TO NEAREST · CANCEL D2."""
    pad = 12
    gap = 8
    btn_w = (DISPLAY_W - 2 * pad - gap) // 2
    return pad, pad + btn_w + gap, btn_w


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
                  error=""):
    """Full-screen QWERTY keyboard for text entry."""
    if not transparent:
        surf.fill((0,8,22))
    hdr = pygame.Surface((DISPLAY_W, 44), pygame.SRCALPHA)
    hdr.fill((0, 18, 45, 220 if transparent else 255))
    surf.blit(hdr, (0, 0))
    pygame.draw.line(surf,WHITE,(0,43),(DISPLAY_W-1,43),1)
    _text(surf,title,17,WHITE,bold=True,cx=DISPLAY_W//2,cy=22)
    disp_str = (entered if entered else str(current_val)) + "\u2502"
    pygame.draw.rect(surf,(0,15,38),(10,50,DISPLAY_W-21,50),border_radius=6)
    pygame.draw.rect(surf,WHITE,(10,50,DISPLAY_W-21,50),width=1,border_radius=6)
    _text(surf,disp_str,28,CYAN,bold=True,cx=DISPLAY_W//2,cy=75)
    if error:
        _text(surf, error, 12, (255, 90, 90), bold=True,
              cx=DISPLAY_W//2, cy=104)
    else:
        _text(surf, f"Current: {current_val}", 10, (110, 120, 140),
              cx=DISPLAY_W//2, cy=104)
    rh = _kb_row_h()
    y = _KB_Y0
    for row in _current_kb_rows():
        x = _kb_row_x0(row)
        for label,kw,style in row:
            _kb_key(surf,x,y,kw,rh,label,style)
            x += kw+_KB_GAP_X
        y += rh+_KB_GAP_Y

    if _kb_nav_extras_visible():
        bx_l, bx_r, btn_w = _kb_nav_extras_geometry()
        by = _kb_nav_extras_y()
        nrst = _nav_lookup_nearest()
        nrst_lbl = f"DIRECT TO {nrst}" if nrst else "DIRECT TO NEAREST"
        _action_btn(surf, bx_l, by, btn_w, _KB_NAV_BTN_H, nrst_lbl, "ok")
        _action_btn(surf, bx_r, by, btn_w, _KB_NAV_BTN_H, "CANCEL D2", "danger")


def keyboard_hit(x, y):
    """Return (label, style) of the tapped key, or None.

    Style 'nrst' / 'clrfp' are synthetic \u2014 emitted by the nav-ident
    extras buttons (NEAREST, CANCEL D2)."""
    if _kb_nav_extras_visible():
        by = _kb_nav_extras_y()
        if by <= y <= by + _KB_NAV_BTN_H:
            bx_l, bx_r, btn_w = _kb_nav_extras_geometry()
            if bx_l <= x <= bx_l + btn_w:
                return ("NRST", "nrst")
            if bx_r <= x <= bx_r + btn_w:
                return ("CLRFP", "clrfp")
    rh = _kb_row_h()
    ky = _KB_Y0
    for row in _current_kb_rows():
        if ky <= y <= ky+rh:
            kx = _kb_row_x0(row)
            for label,kw,style in row:
                if kx <= x <= kx+kw:
                    return (label, style)
                kx += kw+_KB_GAP_X
        ky += rh+_KB_GAP_Y
    return None


# ── Sub-setup screens (Display · AHRS · Connectivity · System) ───────────────

_SS_MX  = 12     # side margin
_SS_Y0  = 52     # first row top (44px title bar + 8px gap)
_SS_RH  = 62     # row height (62 lets 6 rows fit in 480px without scroll)
_SS_GAP = 6      # gap between rows

# Per-setup-screen scroll offsets (in pixels). Keyed by disp["mode"].
# Used when a screen's content exceeds the 436 px of vertical area below
# the title bar.
_ss_scroll = {}

_SS_TITLE_BAR_H = 44     # taps in this band belong to the header, not rows

# Drag-to-scroll state. Set on MOUSEBUTTONDOWN/FINGERDOWN inside a drag-
# capable setup screen, cleared on UP. Motion exceeding _SS_DRAG_THRESHOLD
# converts the touch from "tap" to "drag" — taps still fire (replayed at
# UP), drags scroll without firing.
_ss_drag = None
_SS_DRAG_THRESHOLD = 8     # px before tap becomes drag
_SS_DRAG_MODES = {         # mode → n_rows (used to clamp max scroll)
    "ahrs_setup":         7,
    "display_setup":      7,    # 5 standard rows + tall MAP LAYERS row
                                # (counted as ≈2 row-slots tall for
                                # scroll math — see _DSP_LAYERS_ROW_H)
    "system_setup":       9,
    "connectivity_setup": 6,
    "flight_profile":     8,
    "ahrs_firmware":      5,
    "screen_sync_setup": 10,    # enable + transport + peer + ifaces + 6 categories
}
_dispatch_replay = False   # guard against infinite recursion in the
                           # deferred-tap replay path

# MFD drag/pan state — set on DOWN inside the map, cleared on UP.
# Motion exceeding _MFD_DRAG_THRESHOLD converts a tap into a pan-drag.
_mfd_drag = None
_MFD_DRAG_THRESHOLD = 8

# FPL list scroll-drag state (mode == "fpl").  Same shape as _ss_drag.
_fpl_drag = None
_FPL_DRAG_THRESHOLD = 8


def _ss_row_y(i):
    """Top-of-row pixel y for row index i, accounting for the active mode's
    scroll offset. Both the draw and hit-test paths share this helper so
    they stay in lock-step under scrolling."""
    base = _SS_Y0 + i * (_SS_RH + _SS_GAP)
    return base - _ss_scroll.get(disp.get("mode", ""), 0)


def _ss_content_h(n_rows):
    """Total pixel height of n_rows of setting rows (no trailing gap)."""
    return _SS_Y0 + n_rows * (_SS_RH + _SS_GAP) - _SS_GAP


def _ss_max_scroll(n_rows):
    """Scroll cap so the LAST row's bottom just touches the screen edge."""
    visible = DISPLAY_H - _SS_TITLE_BAR_H
    return max(0, _ss_content_h(n_rows) - _SS_TITLE_BAR_H - visible)


def _ss_clip_to_content(surf):
    """set_clip the surface to the content area (below title bar). Returns
    the previous clip so the caller can restore via set_clip(prev)."""
    prev = surf.get_clip()
    surf.set_clip(pygame.Rect(0, _SS_TITLE_BAR_H,
                              DISPLAY_W, DISPLAY_H - _SS_TITLE_BAR_H))
    return prev


def _ss_reset_scroll(mode):
    """Clear scroll for the named mode — call when entering a setup screen
    so the user always starts at the top."""
    _ss_scroll.pop(mode, None)


def _screen_header(surf, title):
    surf.fill((0, 8, 22))
    pygame.draw.rect(surf, (0, 18, 45), (0, 0, DISPLAY_W, 44))
    pygame.draw.line(surf, WHITE, (0, 43), (DISPLAY_W-1, 43), 1)
    _setup_button(surf, 8, 6, 72, 31, "\u2190 BACK", r=5)
    _text(surf, title, 24, WHITE, bold=True, cx=DISPLAY_W//2, cy=22)


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
    _text(surf, label, 20, WHITE, bold=True, x=bx+14, y=by+6)
    if sub:
        _text(surf, sub, 14, (155, 170, 195), x=bx+14, y=by+34)
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
    _text(surf, label, 18, tc, bold=active, cx=bx+bw//2, cy=by+bh//2)


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

_DSP_ROWS = [
    # (key, label, sub, opts_vals, opts_labels, btn_w)   None → stepper
    ("spd_unit",   "SPEED UNITS",  "Knots · Miles · Km/h",
     ["kt","mph","kph"], ["KT","MPH","KPH"], 80),
    ("alt_unit",   "ALTITUDE",     "Feet or Metres",
     ["ft","m"],         ["FT","M"],         100),
    ("baro_unit",  "PRESSURE",     "Inches Hg or hPa",
     ["inhg","hpa"],     ["inHg","hPa"],     100),
    ("brightness", "BRIGHTNESS",   "Screen brightness 1\u201310",
     None, None, None),
    ("night_mode", "NIGHT MODE",   "Dim red cockpit lighting",
     [False, True],      ["OFF","ON"],        100),
]

# MAP LAYERS multi-toggle row — mirrors pi4's _DSP_MAP_LAYERS so the
# Display setup screen has the same per-layer control across both
# displays.  Pills are laid out in TWO sub-rows so the right edge
# doesn't clobber the row label / subtitle on the left — single-row
# layout with 7 pills overflows the 480-px screen.  Tapping each
# toggles the matching map_show_* boolean in disp["ds"].
_DSP_MAP_LAYERS = [
    ("map_show_terrain",       "TER"),
    ("map_show_water",         "WTR"),
    ("map_show_airports",      "APT"),
    ("map_show_obstacles",     "OBS"),
    ("map_show_state_lines",   "STA"),
    ("map_show_country_lines", "CTRY"),
    ("map_show_airspaces",     "ASP"),
]
_DSP_LAYERS_PER_SUBROW = 4    # top row up to this many; rest spill to row 2
_DSP_LAYERS_ROW_INDEX  = len(_DSP_ROWS)
_DSP_LAYERS_BTN_W      = 84   # roomier than the standard 70 so labels
                              # like "CTRY" don't crowd the edge
_DSP_LAYERS_BTN_G      = 8
_DSP_LAYERS_BTN_H      = 44   # taller than _DSP_BTN_H (40) — pills had
                              # been crushed to 26 to fit one _SS_RH;
                              # row is now taller so the pills get
                              # finger-sized
_DSP_LAYERS_SUB_GAP    = 8
# Map-layers row is taller than the standard _SS_RH to fit two
# sub-rows of finger-sized pills.  The row index past it remains
# unused (MAP LAYERS is the last row of display_setup) so growing
# it doesn't shift anything; we just need _SS_DRAG_MODES to grant
# enough scroll for the extra height (handled in _ss_max_scroll
# below) — bumped display_setup's row count to compensate.
_DSP_LAYERS_ROW_H      = (2 * _DSP_LAYERS_BTN_H
                          + _DSP_LAYERS_SUB_GAP + 16)


def _dsp_layers_subrow_count():
    """How many sub-rows of pills (1 if everything fits the top row)."""
    n = len(_DSP_MAP_LAYERS)
    return 1 if n <= _DSP_LAYERS_PER_SUBROW else 2


def _dsp_layers_subrow_split():
    """Indices [first row], [second row] for the pill layout."""
    n = len(_DSP_MAP_LAYERS)
    if n <= _DSP_LAYERS_PER_SUBROW:
        return list(range(n)), []
    return list(range(_DSP_LAYERS_PER_SUBROW)), list(range(_DSP_LAYERS_PER_SUBROW, n))


def _dsp_layers_geom(bx, bw, subrow_idx=0):
    """Right-aligned x for the first pill of the given sub-row."""
    top, bot = _dsp_layers_subrow_split()
    n = len(top) if subrow_idx == 0 else len(bot)
    if n == 0:
        return bx + bw
    total = n * _DSP_LAYERS_BTN_W + (n - 1) * _DSP_LAYERS_BTN_G
    return bx + bw - total - 14


def _dsp_layers_subrow_y(by, subrow_idx):
    """Top y of the given pill sub-row inside the taller MAP LAYERS row.
    Centered vertically in _DSP_LAYERS_ROW_H, leaving room above the
    pills for the row label / subtitle drawn by _setting_row at the
    top edge."""
    n_sub = _dsp_layers_subrow_count()
    total_h = n_sub * _DSP_LAYERS_BTN_H + (n_sub - 1) * _DSP_LAYERS_SUB_GAP
    # Center within the row, but leave the top 8 px free of the label
    # area drawn by _setting_row.
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


def draw_display_setup(surf, ds):
    _screen_header(surf, "DISPLAY")
    _prev_clip = _ss_clip_to_content(surf)
    for ri, row in enumerate(_DSP_ROWS):
        key, label, sub, opts_v, opts_l, bw_each = row
        is_night = (key == "night_mode")
        bx, by, bw, bh = _setting_row(surf, ri, label, sub)
        if is_night:
            # Overlay dim veil to show greyed-out state
            veil = pygame.Surface((bw, bh), pygame.SRCALPHA)
            veil.fill((0, 0, 0, 160))
            surf.blit(veil, (bx, by))
            _text(surf, "future", 10, (90,90,100), x=bx+bw-60, y=by+bh-18)
            continue
        ry = by + (bh - _DSP_BTN_H) // 2
        rx = _dsp_rx(row, bx, bw)
        if opts_v is None:                              # brightness stepper
            val = ds.get("brightness", 8)
            _step_btn(surf, rx, ry, _DSP_SW, _DSP_BTN_H, "\u2212")
            vx = rx + _DSP_SW + _DSP_BTN_G
            pygame.draw.rect(surf, (0,18,38), (vx, ry, _DSP_VW, _DSP_BTN_H), border_radius=4)
            pygame.draw.rect(surf, (60,80,110), (vx, ry, _DSP_VW, _DSP_BTN_H), width=1, border_radius=4)
            _text(surf, str(val), 18, WHITE, bold=True, cx=vx+_DSP_VW//2, cy=ry+_DSP_BTN_H//2)
            _step_btn(surf, vx+_DSP_VW+_DSP_BTN_G, ry, _DSP_SW, _DSP_BTN_H, "+")
        else:                                           # segmented control
            cur = ds.get(key, opts_v[0])
            for i, (v, lbl) in enumerate(zip(opts_v, opts_l)):
                _seg_btn(surf, rx+i*(bw_each+_DSP_BTN_G), ry, bw_each, _DSP_BTN_H, lbl, v==cur)

    # MAP LAYERS — packed multi-toggle row drawn after the standard
    # ones.  This row is taller than _SS_RH so two sub-rows of
    # finger-sized pills fit comfortably; the rest of the display
    # setup scrolls beneath it via the existing drag-to-scroll
    # machinery.  Drawn manually (not via _setting_row) because we
    # need the custom height.
    bx = _SS_MX
    bw = DISPLAY_W - 2 * _SS_MX
    by = _ss_row_y(_DSP_LAYERS_ROW_INDEX)
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
    _text(surf, "Per-layer visibility on the MFD", 10,
          (120, 135, 155), x=bx + 14, y=by + 32)

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
    if 8 <= x <= 80 and 6 <= y <= 37:
        return "back"
    for ri, row in enumerate(_DSP_ROWS):
        key, *_, opts_v, opts_l, bw_each = row
        if key == "night_mode":
            continue   # greyed out — no interaction
        by = _ss_row_y(ri)
        if not (by <= y <= by+_SS_RH):
            continue
        bx = _SS_MX; bw = DISPLAY_W - 2*_SS_MX
        ry = by + (_SS_RH - _DSP_BTN_H) // 2
        rx = _dsp_rx(row, bx, bw)
        if not (ry <= y <= ry+_DSP_BTN_H):
            continue
        if opts_v is None:
            if rx <= x <= rx+_DSP_SW:
                return "inc:brightness:-1"
            plus_x = rx + _DSP_SW + _DSP_BTN_G + _DSP_VW + _DSP_BTN_G
            if plus_x <= x <= plus_x+_DSP_SW:
                return "inc:brightness:1"
        else:
            for i, v in enumerate(opts_v):
                bx_b = rx + i*(bw_each+_DSP_BTN_G)
                if bx_b <= x <= bx_b+bw_each:
                    return f"set:{key}:{v}"

    # MAP LAYERS multi-toggle row — two sub-rows of pills inside the
    # tall _DSP_LAYERS_ROW_H slot.
    by = _ss_row_y(_DSP_LAYERS_ROW_INDEX)
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


def draw_ahrs_setup(surf, ss):
    _screen_header(surf, "AHRS / SENSORS")
    _prev_clip = _ss_clip_to_content(surf)

    # Row 0: Pitch trim — 0.1° per step (was 0.5°)
    bx, by, bw, bh = _setting_row(surf, 0, "PITCH TRIM", "Horizon offset correction (0.1° / tap)")
    _trim_stepper(surf, bx, by, bw, bh, ss.get("pitch_trim", 0.0), "pitch_trim")

    # Row 1: Roll trim — 0.1° per step
    bx, by, bw, bh = _setting_row(surf, 1, "ROLL TRIM", "Wing-level correction (0.1° / tap)")
    _trim_stepper(surf, bx, by, bw, bh, ss.get("roll_trim", 0.0), "roll_trim")

    # Row 2: Magnetometer calibration — CALIBRATE button opens the
    # 8-point walk-through wizard.  Active button (was greyed out).
    bx, by, bw, bh = _setting_row(surf, 2, "MAGNETOMETER", "Compass calibration")
    cal = ss.get("mag_cal", "idle")
    state_lbl, state_col = _SS_MAG_LABELS.get(cal, ("?", WHITE))
    _text(surf, state_lbl, 14, state_col, bold=True, x=bx+260, y=by+(bh-18)//2)
    cur_deltas = ss.get("mag_cal_deltas") or []
    if cur_deltas and any(abs(d) > 0.05 for d in cur_deltas):
        peak = max(abs(d) for d in cur_deltas)
        _text(surf, f"max |Δ| {peak:.1f}°", 12, (140, 160, 190),
              x=bx+260, y=by+(bh-18)//2 + 22)
    cbx = bx+bw-138-14; cby = by+(bh-_DSP_BTN_H)//2
    _action_btn(surf, cbx, cby, 138, _DSP_BTN_H, "CALIBRATE", "ok")

    # Row 3: Connector orientation (FWD / LEFT / RIGHT / AFT) — defines
    # which side of the AHRS board the connector points toward when
    # viewed from the pilot's seat.  The Pico applies the correct
    # body-axis swap before broadcasting orientation, so changing this
    # remaps pitch and roll axes correctly without per-display tuning.
    pico_ori = state.get("orientation", "right")
    sel_ori  = ss.get("orientation", pico_ori)
    if sel_ori != pico_ori:
        ori_sub = f"Connector direction  (AHRS: {pico_ori} — sending…)"
    else:
        ori_sub = f"Connector direction  (AHRS: {pico_ori})"
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

    # Row 4: Mounting (right-side-up vs inverted)
    pico_mnt = state.get("mounting", "normal")
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

    # Row 5: Heading source (MAG / TRK / AUTO)
    # AUTO uses TRK in motion (groundspeed > threshold) and MAG when stationary.
    bx, by, bw, bh = _setting_row(
        surf, 5, "HEADING SOURCE",
        "MAG=compass  TRK=GPS track  AUTO=TRK in motion / MAG stationary")
    cur_src = ss.get("hdg_src", "auto")
    opts_src = [("mag", "MAG"), ("trk", "TRK"), ("auto", "AUTO")]
    seg_w = 96
    total_src = 3 * seg_w + 2 * _DSP_BTN_G
    rx = bx + bw - total_src - 14
    ry = by + (bh - _DSP_BTN_H) // 2
    for i, (v, lbl) in enumerate(opts_src):
        _seg_btn(surf, rx + i * (seg_w + _DSP_BTN_G), ry, seg_w, _DSP_BTN_H,
                 lbl, v == cur_src)

    # Row 6: Airspeed source (GPS GS vs IAS sensor).  When IAS is
    # selected but airdata_ok is False (sensor missing or stale), the
    # speed tape auto-falls back to GPS GS so the display never blanks.
    bx, by, bw, bh = _setting_row(surf, 6, "AIRSPEED SOURCE",
                                   "GPS groundspeed or IAS sensor (auto-falls back to GS without air data)")
    cur_as = ss.get("airspeed_src", "gps")
    opts_as = [("gps", "GPS GS"), ("ias", "IAS SENSOR")]
    total_as = 2*120 + _DSP_BTN_G
    rx = bx + bw - total_as - 14
    ry = by + (bh - _DSP_BTN_H) // 2
    for i, (v, lbl) in enumerate(opts_as):
        _seg_btn(surf, rx+i*(120+_DSP_BTN_G), ry, 120, _DSP_BTN_H, lbl, v == cur_as)

    surf.set_clip(_prev_clip)


def ahrs_setup_hit(x, y, ss):
    if 8 <= x <= 80 and 6 <= y <= 37:
        return "back"
    # Don't let scrolled-up rows whose y now falls inside the title bar
    # absorb taps that the user meant for the header.
    if y < _SS_TITLE_BAR_H:
        return None
    bw = DISPLAY_W - 2*_SS_MX
    total = _SS_TRIM_SW + _SS_TRIM_G + _SS_TRIM_VW + _SS_TRIM_G + _SS_TRIM_SW
    rx_trim = _SS_MX + bw - total - 14
    for ri in range(7):
        by = _ss_row_y(ri)
        if not (by <= y <= by+_SS_RH):
            continue
        bx = _SS_MX
        if ri in (0, 1):
            # Trim: ±0.1° per tap (was ±0.5° — pi4 convention)
            key = "pitch_trim" if ri == 0 else "roll_trim"
            ry = by + (_SS_RH - _SS_TRIM_H) // 2
            if not (ry <= y <= ry+_SS_TRIM_H):
                continue
            if rx_trim <= x <= rx_trim+_SS_TRIM_SW:
                return f"trim:{key}:-0.1"
            plus_x = rx_trim + _SS_TRIM_SW + _SS_TRIM_G + _SS_TRIM_VW + _SS_TRIM_G
            if plus_x <= x <= plus_x+_SS_TRIM_SW:
                return f"trim:{key}:+0.1"
        elif ri == 2:
            # CALIBRATE button — opens mag_cal wizard (stub in phase A;
            # phase B wires the full 8-point walkthrough)
            cbx = _SS_MX + bw - 138 - 14
            cby = by + (_SS_RH - _DSP_BTN_H) // 2
            if cbx <= x <= cbx + 138 and cby <= y <= cby + _DSP_BTN_H:
                return "mag_cal_open"
        elif ri == 3:
            # ORIENTATION: FWD / LEFT / RIGHT / AFT
            seg_w = 88
            total_o = 4 * seg_w + 3 * _DSP_BTN_G
            rx = bx + bw - total_o - 14
            ry = by + (_SS_RH - _DSP_BTN_H) // 2
            for i, v in enumerate(("forward", "left", "right", "aft")):
                xi = rx + i * (seg_w + _DSP_BTN_G)
                if xi <= x <= xi + seg_w and ry <= y <= ry + _DSP_BTN_H:
                    return f"set:orientation:{v}"
        elif ri == 4:
            # MOUNTING: NORMAL / INVERTED
            total_m = 2*120 + _DSP_BTN_G
            rx = bx + bw - total_m - 14
            ry = by + (_SS_RH - _DSP_BTN_H) // 2
            for i, v in enumerate(("normal","inverted")):
                if rx+i*(120+_DSP_BTN_G) <= x <= rx+i*(120+_DSP_BTN_G)+120:
                    if ry <= y <= ry+_DSP_BTN_H:
                        return f"set:mounting:{v}"
        elif ri == 5:
            # HEADING SOURCE: MAG / TRK / AUTO
            seg_w = 96
            total_src = 3 * seg_w + 2 * _DSP_BTN_G
            rx = bx + bw - total_src - 14
            ry = by + (_SS_RH - _DSP_BTN_H) // 2
            for i, v in enumerate(("mag", "trk", "auto")):
                xi = rx + i * (seg_w + _DSP_BTN_G)
                if xi <= x <= xi + seg_w and ry <= y <= ry + _DSP_BTN_H:
                    return f"set:hdg_src:{v}"
        elif ri == 6:
            # AIRSPEED SOURCE: GPS GS / IAS SENSOR — both tappable now.
            total_as = 2*120 + _DSP_BTN_G
            rx = bx + bw - total_as - 14
            ry = by + (_SS_RH - _DSP_BTN_H) // 2
            for i, v in enumerate(("gps", "ias")):
                xi = rx + i * (120 + _DSP_BTN_G)
                if xi <= x <= xi + 120 and ry <= y <= ry + _DSP_BTN_H:
                    return f"set:airspeed_src:{v}"
    return None


# ── Magnetometer calibration wizard ──────────────────────────────────────────
# 8-point walk-through (N / NE / E / SE / S / SW / W / NW) builds a 36-slot
# deviation table that we push to the Pico over the same transport the
# display is using (USB serial preferred, HTTP fallback) so every display
# reads the same calibrated heading from the AHRS broadcast.  Pi4 carries
# the canonical implementation; this is a verbatim port sized for the
# 640×480 panel.

_MAG_CAL_CARDINALS = [("N",   0.0), ("NE",  45.0),
                      ("E",  90.0), ("SE", 135.0),
                      ("S", 180.0), ("SW", 225.0),
                      ("W", 270.0), ("NW", 315.0)]

_MCAL_W     = 600         # modal width  (640 px panel, 20 px chrome each side)
_MCAL_H     = 430         # modal height (480 px panel — leaves 25 px chrome)
_MCAL_BTN_H = 56
# Max spread (max − min) in degrees across the 8 cardinal attitude
# captures for the level-flight alignment auto-apply to fire.  Same
# threshold as pi4 so the two screens converge on the same alignment
# from the same walk.
_ALIGN_MAX_SPREAD_DEG = 1.0


def _mcal_geom():
    bx = (DISPLAY_W - _MCAL_W) // 2
    by = (DISPLAY_H - _MCAL_H) // 2
    btn_y = by + _MCAL_H - _MCAL_BTN_H - 14
    btn_w = (_MCAL_W - 14 - 14 - 3 * 8) // 4
    btn_xs = [bx + 14 + i * (btn_w + 8) for i in range(4)]
    return bx, by, btn_y, btn_w, btn_xs


def _mag_cal_open(prev_mode: str):
    disp["mag_cal_wiz"] = {"step": 0, "samples": [], "msg": "",
                           "prev": prev_mode}
    disp["mode"] = "mag_cal"


def _mag_cal_capture():
    wiz = disp.get("mag_cal_wiz") or {}
    step = wiz.get("step", 0)
    if step >= len(_MAG_CAL_CARDINALS):
        return
    raw = float(disp.get("_yaw_uncal", disp.get("yaw", 0.0)))
    expected = _MAG_CAL_CARDINALS[step][1]
    wiz.setdefault("samples", []).append((expected, raw))
    # Capture raw mag vector at this cardinal too — the 8 points are
    # spread evenly around yaw, so (max+min)/2 per axis gives a decent
    # hard-iron offset even without a separate tumble pass.
    mx = float(disp.get("mx", 0.0))
    my = float(disp.get("my", 0.0))
    mz = float(disp.get("mz", 0.0))
    wiz.setdefault("mag_samples", []).append((mx, my, mz))
    # Attitude sample for the level-flight alignment auto-capture.
    # Use the residual filter output (displayed pitch/roll minus the
    # Pico's trim) so the mean reflects the additional input-side
    # rotation needed beyond what's already applied.
    pitch_raw = float(disp.get("pitch", 0.0)) - float(disp.get("pitch_trim", 0.0))
    roll_raw  = float(disp.get("roll",  0.0)) - float(disp.get("roll_trim",  0.0))
    wiz.setdefault("att_samples", []).append((pitch_raw, roll_raw))
    wiz["step"] = step + 1
    wiz["msg"] = f"Captured {_MAG_CAL_CARDINALS[step][0]}."
    if wiz["step"] >= len(_MAG_CAL_CARDINALS):
        table = _build_magdev_table(wiz["samples"])
        # Hard-iron offsets from the 8-cardinal mag vectors.  Won't
        # cover Z-tilt as thoroughly as a full tumble cal — the TUMBLE
        # button is the right tool for that — but it's a useful first
        # pass that costs the pilot nothing extra.
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
        disp["ss"]["pi_zero_magdev"] = table
        disp["ss"]["pi_zero_mag_offset"] = list(offset)
        disp["ss"]["mag_cal_deltas"] = [
            ((e - r + 540) % 360) - 180 for e, r in wiz["samples"]
        ]
        disp["ss"].pop("mag_cal_offset", None)
        disp["ss"]["mag_cal"] = "done"
        _settings.mark_dirty()
        _push_magcal_to_pico(table)
        _push_magoff_to_pico(offset)
        # Level-flight alignment auto-capture — same rule as pi4:
        # mean of the residual pitch/roll across the 8 cardinals is
        # the additional alignment rotation; spread between captures
        # tells us whether the readings were trustworthy.
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
        wiz["step"]        = 0
        wiz["samples"]     = []
        wiz["mag_samples"] = []
        wiz["att_samples"] = []


def _mag_cal_tumble_toggle():
    """First press → start tumble cal (Pico tracks raw mag min/max).
    Second press → finish, Pico computes (min+max)/2 per axis and stores."""
    wiz = disp.get("mag_cal_wiz") or {}
    if wiz.get("tumble_active"):
        _push_magoff_tumble("FINISH")
        wiz["tumble_active"] = False
        wiz["msg"] = "Tumble cal finished — offsets sent to AHRS."
        wiz["tumble_started_ms"] = None
    else:
        _push_magoff_tumble("START")
        wiz["tumble_active"] = True
        wiz["tumble_started_ms"] = pygame.time.get_ticks()
        wiz["tumble_min"] = [None, None, None]
        wiz["tumble_max"] = [None, None, None]
        wiz["msg"] = ("Rotate AHRS slowly through ALL orientations — "
                      "pitch, roll, yaw — for ~30 s. Press STOP TUMBLE when done.")


def _mag_cal_tumble_tick():
    """Mirror the Pico's min/max tracking locally while a tumble session
    is active, so the display can show progress (spread per axis)."""
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
    """Wipe both the deviation table and the hard-iron offsets, locally
    and on the Pico."""
    _push_magcal_clear_to_pico()
    _push_magoff_clear_to_pico()
    disp["ss"].pop("pi_zero_magdev", None)
    disp["ss"].pop("pi_zero_mag_offset", None)
    disp["ss"]["mag_cal_deltas"] = [0.0] * 4
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


def _build_magdev_table(samples):
    """Build a 36-point (10°/slot) deviation table from (expected, raw) pairs."""
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


def _push_magcal_to_pico(table):
    """Send the 36-point deviation table to the Pico.  USB serial preferred,
    HTTP fallback. Background thread; updates the modal status line."""
    t_str = ",".join(f"{v:.3f}" for v in table)

    def _worker():
        sent_ok = False
        client = _sse_client
        if client is not None and hasattr(client, "write"):
            try:
                client.write(f"$MAGDEV,{t_str}\n".encode())
                print(f"[PFD] magcal sent via USB serial ({len(table)} pts)")
                sent_ok = True
            except Exception as e:
                print(f"[PFD] magcal serial write failed: {e}")
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
        wiz["msg"] = ("Saved locally + sent to AHRS ✓" if sent_ok
                      else "Saved locally only (AHRS unreachable)")

    threading.Thread(target=_worker, daemon=True, name="MagCalPush").start()


def _push_magcal_clear_to_pico():
    """Clear the Pico's deviation table.  USB serial preferred, HTTP fallback."""
    def _worker():
        client = _sse_client
        if client is not None and hasattr(client, "write"):
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


def _push_magoff_tumble(action):
    """$MAGOFF,START / $MAGOFF,FINISH bracket a tumble-cal session: the
    Pico tracks per-axis mag min/max while the pilot rotates the unit
    through all orientations.  On FINISH the Pico computes the
    hard-iron offset (midpoint of min/max) and stores it locally."""
    payload = f"$MAGOFF,{action}\n".encode()
    http_action = "tumble_start" if action == "START" else "tumble_finish"

    def _worker():
        client = _sse_client
        if client is not None and hasattr(client, "write"):
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

    threading.Thread(target=_worker, daemon=True, name=f"MagOff{action}").start()


def _push_magoff_to_pico(offset):
    """Send a hard-iron offset triple to the Pico ($MAGOFF,<mx>,<my>,<mz>).
    Subtracted from raw mag before the Mahony filter sees it."""
    v_str = ",".join(f"{v:.1f}" for v in offset)

    def _worker():
        client = _sse_client
        if client is not None and hasattr(client, "write"):
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


def _push_align_to_pico(pitch_align, roll_align):
    """Send axis-alignment values to the Pico via $ALIGN.  Mirrors the
    Pi4 helper — USB serial preferred, no HTTP fallback (alignment is
    only ever pushed alongside a USB-attached cal session)."""
    def _worker():
        client = _sse_client
        if client is None or not hasattr(client, "write"):
            print("[PFD] align push: no serial client available")
            return
        try:
            cmd = f"$ALIGN,{pitch_align:.2f},{roll_align:.2f}\n".encode()
            client.write(cmd)
            print(f"[PFD] align sent ({pitch_align:+.2f},{roll_align:+.2f})")
        except Exception as e:
            print(f"[PFD] align serial write failed: {e}")

    threading.Thread(target=_worker, daemon=True, name="AlignPush").start()


def _push_magoff_clear_to_pico():
    """Clear the Pico's hard-iron offsets."""
    def _worker():
        client = _sse_client
        if client is not None and hasattr(client, "write"):
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


def _push_orient_to_pico(connector, mounting):
    """Send orientation + mounting to the Pico via USB serial.
    Retries every 2 s (up to 6 attempts) until the Pico echoes the new
    orientation back in $AHRS, confirming receipt."""
    import time as _time
    def _worker():
        for attempt in range(6):
            client = _sse_client
            if client is None or not hasattr(client, "write"):
                print("[PFD] orient push: no serial client available")
                return
            try:
                client.write(f"$ORIENT,{connector},{mounting}\n".encode())
                print(f"[PFD] orient sent (attempt {attempt + 1}) ({connector},{mounting})")
            except Exception as e:
                print(f"[PFD] orient serial write failed: {e}")
                return
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

    _text(surf, "COMPASS CAL", 18, (160, 200, 230), bold=True,
          cx=bx + _MCAL_W // 2, cy=by + 28)

    instr = (f"Step {step + 1} of {len(_MAG_CAL_CARDINALS)} — "
             f"point aircraft {card_name} ({int(card_exp):03d}°)")
    _text(surf, instr, 15, WHITE, cx=bx + _MCAL_W // 2, cy=by + 70)

    raw = float(disp.get("_yaw_uncal", disp.get("yaw", 0.0))) % 360.0
    cal = float(disp.get("_yaw_cal",   disp.get("yaw", 0.0))) % 360.0

    _text(surf, "RAW HDG", 12, (200, 190, 100), bold=True,
          x=bx + 40, y=by + 108)
    _text(surf, f"{raw:6.1f}°", 22, (240, 220, 80), bold=True,
          x=bx + 40, y=by + 124)
    _text(surf, "CAL HDG", 12, (100, 200, 130), bold=True,
          x=bx + 200, y=by + 108)
    _text(surf, f"{cal:6.1f}°", 22, (80, 230, 120), bold=True,
          x=bx + 200, y=by + 124)

    # 8-point capture results — two rows of 4 (N NE E SE / S SW W NW)
    wiz_samples = wiz.get("samples", [])
    col_xs = [bx + 60 + c * (_MCAL_W - 120) // 3 for c in range(4)]
    for row in range(2):
        row_y = by + 188 + row * 36
        for col in range(4):
            i = row * 4 + col
            name, exp = _MAG_CAL_CARDINALS[i]
            cx_card = col_xs[col]
            lbl_col = (170, 185, 210) if i >= len(wiz_samples) else (100, 200, 255)
            _text(surf, name, 12, lbl_col, bold=True, cx=cx_card, cy=row_y)
            if i < len(wiz_samples):
                _exp, rawv = wiz_samples[i]
                d = ((_exp - rawv + 540) % 360) - 180
                _text(surf, f"{d:+.1f}°", 14, WHITE, bold=True,
                      cx=cx_card, cy=row_y + 18)
            else:
                _text(surf, "—", 14, (110, 120, 140), bold=True,
                      cx=cx_card, cy=row_y + 18)

    msg = wiz.get("msg", "") or ""
    if msg:
        col = (255, 180, 60) if ("WARNING" in msg or "FAILED" in msg or "failed" in msg) \
              else (60, 220, 80)
        _text(surf, msg, 14, col, cx=bx + _MCAL_W // 2, cy=by + 270)

    # Tumble-cal live progress — visible whenever a tumble session is
    # running.  Spread = max − min per axis; the tighter the spread the
    # less of the mag ellipse the pilot has covered.  Hide above the
    # button row so it doesn't overlap CAPTURE.
    if wiz.get("tumble_active"):
        _mag_cal_tumble_tick()
        mn = wiz.get("tumble_min") or [None, None, None]
        mx_arr = wiz.get("tumble_max") or [None, None, None]
        spread = [(mx_arr[i] - mn[i])
                  if (mx_arr[i] is not None and mn[i] is not None) else 0
                  for i in range(3)]
        elapsed_ms = pygame.time.get_ticks() - (wiz.get("tumble_started_ms") or 0)
        _text(surf, f"TUMBLE  {elapsed_ms/1000:.0f}s  "
                    f"spread X:{int(spread[0])}  Y:{int(spread[1])}  Z:{int(spread[2])}",
              13, (255, 200, 80),
              cx=bx + _MCAL_W // 2, cy=by + 294)

    in_progress = step > 0 and step < len(_MAG_CAL_CARDINALS)
    left_lbl   = "CANCEL" if in_progress else "EXIT"
    left_style = "danger" if in_progress else "ok"
    tumble_active = bool(wiz.get("tumble_active"))
    tumble_lbl   = "STOP TUMBLE" if tumble_active else "TUMBLE"
    tumble_style = "danger"      if tumble_active else "warn"
    _action_btn(surf, btn_xs[0], btn_y, btn_w, _MCAL_BTN_H, left_lbl,    left_style)
    _action_btn(surf, btn_xs[1], btn_y, btn_w, _MCAL_BTN_H, "RESET",     "warn")
    _action_btn(surf, btn_xs[2], btn_y, btn_w, _MCAL_BTN_H, tumble_lbl,  tumble_style)
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


# ── CDI strip helpers (great-circle math + draw) ────────────────────────────

_CDI_FULL_SCALE_NM = 1.0      # ±1 nm full-scale en-route / D2
_EARTH_R_NM        = 3440.065 # Earth mean radius (nautical miles)


def _nav_geo_dist_brg(la1, lo1, la2, lo2):
    """Great-circle distance (nm) and initial bearing (deg) from 1 to 2."""
    phi1 = math.radians(la1); phi2 = math.radians(la2)
    dphi = math.radians(la2 - la1); dlam = math.radians(lo2 - lo1)
    a = (math.sin(dphi * 0.5) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlam * 0.5) ** 2)
    dist = 2.0 * _EARTH_R_NM * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    y = math.sin(dlam) * math.cos(phi2)
    x = (math.cos(phi1) * math.sin(phi2)
         - math.sin(phi1) * math.cos(phi2) * math.cos(dlam))
    brg = math.degrees(math.atan2(y, x)) % 360.0
    return dist, brg


def _nav_xtk_nm(act_lat, act_lon, wpt_lat, wpt_lon, cur_lat, cur_lon):
    """Signed great-circle cross-track distance (nm).  + = right of course."""
    d13, brg13 = _nav_geo_dist_brg(act_lat, act_lon, cur_lat, cur_lon)
    _,   brg12 = _nav_geo_dist_brg(act_lat, act_lon, wpt_lat, wpt_lon)
    if d13 < 1e-6:
        return 0.0
    return _EARTH_R_NM * math.asin(
        math.sin(d13 / _EARTH_R_NM) * math.sin(math.radians(brg13 - brg12))
    )


# ── Flight plan helpers ───────────────────────────────────────────────────
# disp["fpl"] is the source of truth.  When active_idx >= 0 we mirror
# the current leg's destination into disp["nav"] so the existing CDI /
# moving-map course / D→ button / ETE all keep working unchanged.

_FPL_ADVANCE_DIST_NM = 0.5    # auto-sequence when within this of the
                              # current waypoint
_FPL_MAX_WAYPOINTS   = 20     # capped so the list fits a single screen
                              # without scrolling


def _fpl_is_active():
    fpl = disp.get("fpl", {})
    wps = fpl.get("waypoints", [])
    idx = fpl.get("active_idx", -1)
    return 0 <= idx < len(wps)


def _fpl_current():
    """Return the waypoint we're flying toward, or None."""
    if not _fpl_is_active():
        return None
    return disp["fpl"]["waypoints"][disp["fpl"]["active_idx"]]


def _fpl_apply_active(reset_activation=False):
    """Mirror the active FPL waypoint into disp["nav"] so CDI / map /
    D→ button all see it as the current direct-to.  Activation point
    is the previous waypoint's lat/lon (for proper XTE across legs)
    or — on the first leg or when reset_activation=True — the current
    aircraft position."""
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


def _fpl_activate(idx, reset_activation=True):
    """Activate the leg whose destination is waypoints[idx].  Reset
    the activation point to the current aircraft pos by default so a
    fresh user-initiated activation always starts from where we are."""
    wps = disp["fpl"]["waypoints"]
    if not (0 <= idx < len(wps)):
        return
    disp["fpl"]["active_idx"] = idx
    _fpl_apply_active(reset_activation=reset_activation)
    _settings.mark_dirty()
    _ssync_publish_fpl()


def _fpl_deactivate():
    """Clear the active plan without touching the waypoint list."""
    disp["fpl"]["active_idx"] = -1
    disp["nav"]["ident"]   = ""
    disp["nav"]["lat"]     = 0.0
    disp["nav"]["lon"]     = 0.0
    disp["nav"]["elev_ft"] = 0.0
    _settings.mark_dirty()
    _ssync_publish_fpl()


def _fpl_add_waypoint(ident, lat, lon, elev_ft=0.0, user=False,
                       name="", region=""):
    """Append a waypoint; clamped to _FPL_MAX_WAYPOINTS.

    `name` and `region` are stored for the FPL row subtitle (e.g.
    "Sedona Airport · AZ") when available — empty strings for user
    waypoints or older airport caches without those fields."""
    wps = disp["fpl"]["waypoints"]
    if len(wps) >= _FPL_MAX_WAYPOINTS:
        return False
    wps.append({"ident": str(ident),
                "lat": float(lat), "lon": float(lon),
                "elev_ft": float(elev_ft),
                "user": bool(user),
                "name":   str(name),
                "region": str(region)})
    _settings.mark_dirty()
    _ssync_publish_fpl()
    return True


def _fpl_remove(idx):
    """Delete waypoints[idx].  If it was the active leg, deactivate;
    otherwise shift active_idx down by 1 if it was past the removal."""
    wps = disp["fpl"]["waypoints"]
    if not (0 <= idx < len(wps)):
        return
    cur = disp["fpl"]["active_idx"]
    del wps[idx]
    if cur == idx:
        _fpl_deactivate()
    elif cur > idx:
        disp["fpl"]["active_idx"] = cur - 1
        # nav[] still points at the right waypoint (object identity),
        # but the act_lat/act_lon may have come from a deleted prev —
        # refresh.
        _fpl_apply_active()
    _settings.mark_dirty()
    _ssync_publish_fpl()


def _fpl_swap(i, j):
    """Reorder waypoints by swapping i and j.  Adjusts active_idx so
    the active leg's destination follows."""
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


def _fpl_check_advance(lat, lon):
    """Called every frame.  Auto-sequence to the next leg when within
    _FPL_ADVANCE_DIST_NM of the current waypoint.  Deactivates at the
    end of the plan."""
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
        # Reached the final waypoint — deactivate so the magenta
        # course line clears.  The list stays for re-activation.
        _fpl_deactivate()


# ── User-waypoint library ────────────────────────────────────────────────
# Persistent storage for waypoints the pilot creates via +LAT/LON.
# Survives across flights and PFD restarts.  Dedup by ident — typing the
# same name twice updates the coordinates rather than creating duplicates.

_USER_WPT_MAX = 50   # cap so the picker stays single-screen


def _user_wpt_save(ident, lat, lon, elev_ft=0.0):
    """Add a waypoint to the library, or update its position if the
    ident already exists.  No-op if the library is at _USER_WPT_MAX
    capacity and the ident isn't already there."""
    ident = str(ident).upper().strip()
    if not ident:
        return False
    lib = disp["user_wpts"]["list"]
    for w in lib:
        if str(w.get("ident", "")).upper() == ident:
            w["lat"] = float(lat)
            w["lon"] = float(lon)
            w["elev_ft"] = float(elev_ft)
            _settings.mark_dirty()
            return True
    if len(lib) >= _USER_WPT_MAX:
        return False
    lib.append({"ident": ident, "lat": float(lat), "lon": float(lon),
                "elev_ft": float(elev_ft)})
    _settings.mark_dirty()
    return True


def _user_wpt_delete(ident):
    """Remove `ident` from the library.  Idempotent — no-op if absent."""
    ident = str(ident).upper()
    lib = disp["user_wpts"]["list"]
    disp["user_wpts"]["list"] = [
        w for w in lib if str(w.get("ident", "")).upper() != ident
    ]
    _settings.mark_dirty()


def _user_wpt_lookup(ident):
    """Return the library entry for `ident` or None."""
    ident = str(ident).upper()
    for w in disp["user_wpts"]["list"]:
        if str(w.get("ident", "")).upper() == ident:
            return w
    return None


def _fpl_render_remaining():
    """Return a list of (lat, lon, ident) tuples starting at the active
    waypoint and including every subsequent waypoint, for the moving
    map to draw as a dimmer magenta polyline with per-waypoint labels.
    Returns None when there's nothing forward of the active leg."""
    if not _fpl_is_active():
        return None
    wps = disp["fpl"]["waypoints"]
    idx = disp["fpl"]["active_idx"]
    if idx >= len(wps) - 1:
        return None
    return [(float(wp["lat"]), float(wp["lon"]),
             str(wp.get("ident", "")))
            for wp in wps[idx:]]


def _fpl_total_remaining_nm(lat, lon):
    """Distance from current aircraft position through the active leg
    and every remaining leg.  Used by ETA = clock at the FINAL waypoint."""
    if not _fpl_is_active():
        return 0.0
    wps = disp["fpl"]["waypoints"]
    idx = disp["fpl"]["active_idx"]
    total, _ = _nav_geo_dist_brg(lat, lon,
                                  wps[idx]["lat"], wps[idx]["lon"])
    for i in range(idx, len(wps) - 1):
        leg, _ = _nav_geo_dist_brg(
            wps[i]["lat"], wps[i]["lon"],
            wps[i + 1]["lat"], wps[i + 1]["lon"])
        total += leg
    return total


def draw_cdi(surf):
    """Course Deviation Indicator strip above the heading readout box.
    Always painted when GPS_OK so the strip is tappable; bare bar + a
    "DIRECT →" affordance when no waypoint is active so the pilot has
    a fixed entry point for the keyboard."""
    nv = disp.get("nav", {}) or {}
    ident = nv.get("ident", "")
    have_wpt = bool(ident)

    bar_w = max(140, int(DISPLAY_W * 0.32))
    bar_h = 6
    bar_y = HDG_Y - 56
    bar_x = CX - bar_w // 2

    plate = pygame.Surface((bar_w + 36, 48), pygame.SRCALPHA)
    plate.fill((0, 8, 22, 190))
    surf.blit(plate, (bar_x - 18, bar_y - 34))

    pygame.draw.rect(surf, (60, 80, 110), (bar_x, bar_y, bar_w, bar_h),
                     border_radius=2)
    for frac in (-1.0, -0.5, 0.0, 0.5, 1.0):
        tx = bar_x + int((frac + 1.0) * 0.5 * bar_w)
        if frac == 0.0:
            pygame.draw.line(surf, WHITE,
                             (tx, bar_y - 5), (tx, bar_y + bar_h + 5), 2)
        else:
            pygame.draw.circle(surf, (180, 200, 220),
                               (tx, bar_y + bar_h // 2), 2)

    if have_wpt:
        lat = disp.get("lat", 0.0); lon = disp.get("lon", 0.0)
        wpt_lat = float(nv["lat"]); wpt_lon = float(nv["lon"])
        dist_nm, brg = _nav_geo_dist_brg(lat, lon, wpt_lat, wpt_lon)
        act_lat = float(nv.get("act_lat", lat))
        act_lon = float(nv.get("act_lon", lon))
        xtk = _nav_xtk_nm(act_lat, act_lon, wpt_lat, wpt_lon, lat, lon)
        full_scale = _CDI_FULL_SCALE_NM
        xtk_clamped = max(-1.0, min(1.0, xtk / full_scale))
        dx = -int(xtk_clamped * (bar_w / 2))
        dcx = CX + dx
        dcy = bar_y + bar_h // 2
        dpts = [(dcx, dcy - 10), (dcx + 9, dcy), (dcx, dcy + 10), (dcx - 9, dcy)]
        pygame.draw.polygon(surf, MAGENTA, dpts)
        readout = f"{ident}  {int(round(brg)) % 360:03d}°  {dist_nm:.1f}NM"
        _text(surf, readout, 18, MAGENTA, bold=True, cx=CX, cy=bar_y - 22)
    else:
        _text(surf, "DIRECT  →", 18, MAGENTA, bold=True, cx=CX, cy=bar_y - 22)


def _cdi_hit(x, y):
    """Tap on the CDI strip opens the keyboard for waypoint entry."""
    bar_w = max(140, int(DISPLAY_W * 0.32))
    bar_y = HDG_Y - 56
    bar_x = CX - bar_w // 2
    return (bar_x - 18 <= x <= bar_x + bar_w + 18 and
            bar_y - 34 <= y <= bar_y + 14)


# ── Direct-to navigation + confirm modal ─────────────────────────────────────
# Lookup-by-ident and a small "Activate Direct to XXXX?" modal that gates
# the actual activation.  Verbatim port of pi4's flow — the plumbing the
# MFD work consumes for D2 routing.  No PFD-side entry point on pi_zero
# yet; the keyboard hands off into _nav_open_confirm once the MFD lands.

_NAVCNF_W     = 360
_NAVCNF_H     = 170
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
    """Centred "Activate Direct to XXXX?" modal."""
    ident = disp.get("nav_confirm_ident", "")
    bx, by, btn_y, btn_w, bx_l, bx_r = _navcnf_geom()

    _draw_veil(surf)
    panel = pygame.Surface((_NAVCNF_W, _NAVCNF_H), pygame.SRCALPHA)
    panel.fill((0, 12, 32, 235))
    surf.blit(panel, (bx, by))
    pygame.draw.rect(surf, CYAN, (bx, by, _NAVCNF_W, _NAVCNF_H),
                     width=2, border_radius=10)

    _text(surf, "DIRECT TO", 14, (170, 200, 230), bold=True,
          cx=bx + _NAVCNF_W // 2, cy=by + 24)
    _text(surf, ident or "—", 36, MAGENTA, bold=True,
          cx=bx + _NAVCNF_W // 2, cy=by + 68)
    _text(surf, "Activate?", 15, (210, 220, 240),
          cx=bx + _NAVCNF_W // 2, cy=by + 102)

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


_nav_nearest_cache = {"lat": None, "lon": None, "ident": "", "ts": 0.0}


def _nav_lookup_nearest():
    """Return the ident of the nearest public airport (S/M/L) within
    100 nm, or "" if no airports / no fix.  Cached so the keyboard's
    per-frame redraw doesn't hammer the spatial query."""
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
    """Activate direct-to to the nearest public airport (S/M/L)."""
    ident = _nav_lookup_nearest()
    if not ident:
        return False
    return _nav_set_by_ident(ident)


def _nav_lookup_ident(ident: str):
    """Return (ident, lat, lon, elev_ft, name, region) for the first
    matching airport, or None.  `name` and `region` are empty strings
    when the airport cache pre-dates those fields (graceful fallback
    used by older Pi installs whose cache hasn't been rebuilt yet)."""
    if _airports is None or not ident:
        return None
    if hasattr(_airports, "dtype"):
        mask = _airports["ident"] == ident
        rows = _airports[mask]
        if len(rows) == 0:
            return None
        row = rows[0]
        has_name = "name" in (_airports.dtype.names or ())
        name   = str(row["name"])   if has_name else ""
        region = str(row["region"]) if has_name else ""
        return (str(row["ident"]), float(row["lat"]),
                float(row["lon"]), float(row["elev_ft"]),
                name, region)
    for rec in _airports:
        if rec[0] == ident:
            name = rec[5] if len(rec) > 5 else ""
            reg  = rec[6] if len(rec) > 6 else ""
            return (rec[0], float(rec[2]), float(rec[3]),
                    float(rec[4]), name, reg)
    return None


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


def _nav_clear() -> None:
    disp["nav"]["ident"]   = ""
    disp["nav"]["lat"]     = 0.0
    disp["nav"]["lon"]     = 0.0
    disp["nav"]["elev_ft"] = 0.0
    disp["nav"]["act_lat"] = 0.0
    disp["nav"]["act_lon"] = 0.0
    _ssync_publish_nav()
    _settings.mark_dirty()


def _nav_open_confirm(ident: str, prev_mode: str) -> bool:
    """Switch to the nav_confirm modal for `ident`.  Returns False if
    the ident is empty (caller falls through to its no-op path)."""
    if not ident:
        return False
    disp["nav_confirm_ident"] = ident
    disp["nav_confirm_prev"]  = prev_mode
    disp["mode"] = "nav_confirm"
    return True


def _nav_confirm_apply():
    """Activate the pending direct-to and dismiss the modal."""
    ident = disp.get("nav_confirm_ident", "")
    if ident:
        _nav_set_by_ident(ident)
    disp["nav_confirm_ident"] = ""
    disp["mode"] = disp.get("nav_confirm_prev", "pfd")


def _nav_confirm_cancel():
    disp["nav_confirm_ident"] = ""
    disp["mode"] = disp.get("nav_confirm_prev", "pfd")


# ── WiFi network scan ─────────────────────────────────────────────────────────

def _scan_wifi():
    """Return [{ssid, signal, secured}] sorted by signal desc, deduped by SSID."""
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


_WS_ITEM_H = 52
_WS_LIST_Y = 78
_WS_BTN_H  = 44


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
        _text(surf, "Scanning\u2026", 20, CYAN, bold=True,
              cx=DISPLAY_W//2, cy=DISPLAY_H//2 - 14)
        _text(surf, "This may take a few seconds", 12, (110, 130, 160),
              cx=DISPLAY_W//2, cy=DISPLAY_H//2 + 14)
    elif state == "error":
        _text(surf, cs.get("scan_error", "Scan failed"), 14, (220, 80, 80),
              bold=True, cx=DISPLAY_W//2, cy=DISPLAY_H//2 - 10)
    elif state == "done":
        if not nets:
            _text(surf, "No networks found", 16, (180, 180, 180),
                  bold=True, cx=DISPLAY_W//2, cy=DISPLAY_H//2 - 10)
        else:
            _text(surf, f"{len(nets)} network{'s' if len(nets) != 1 else ''} \u2014 tap to select",
                  10, (100, 130, 160), cx=DISPLAY_W//2, cy=58)
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
                bx0 = _SS_MX + 10
                for b in range(4):
                    bh  = 7 + b * 6
                    bby = iy + _WS_ITEM_H//2 + 14 - bh
                    col = bar_col if b < bars else (35, 48, 62)
                    pygame.draw.rect(surf, col, (bx0 + b * 9, bby, 6, bh))
                ssid = net["ssid"]
                if len(ssid) > 26:
                    ssid = ssid[:25] + "\u2026"
                _text(surf, ssid, 14, WHITE, bold=True, x=bx0 + 46, y=iy + 6)
                _text(surf, f"{net['signal']}%", 10, (100, 130, 160), x=bx0 + 46, y=iy + 28)
                lock_lbl = "WPA" if net["secured"] else "OPEN"
                lock_col = (200, 160, 60) if net["secured"] else (60, 200, 100)
                _text(surf, lock_lbl, 10, lock_col, bold=True,
                      x=DISPLAY_W - _SS_MX - 46, y=iy + _WS_ITEM_H//2 - 7)
            if scroll > 0:
                _text(surf, "\u25b2", 14, (100, 140, 180),
                      cx=DISPLAY_W//2, cy=_WS_LIST_Y - 14)
            if scroll + visible < len(nets):
                _text(surf, "\u25bc", 14, (100, 140, 180),
                      cx=DISPLAY_W//2, cy=ws_btn_y - 14)

    bw = DISPLAY_W - 2*_SS_MX
    _action_btn(surf, _SS_MX, ws_btn_y, bw, _WS_BTN_H, "RESCAN", "normal")


def wifi_scan_hit(x, y, cs):
    ws_btn_y = DISPLAY_H - _WS_BTN_H - 8
    if 8 <= x <= 80 and 6 <= y <= 37:
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
]
_CS_BTN_Y  = _ss_row_y(len(_CS_FIELDS) + 2) + 4   # below fields + STATUS + AHRS LINK rows
_CS_BTN_H  = 50


def _cs_val_box(surf, bx, by, bw, bh, key, val):
    """Draw the right-side value box for a connectivity field."""
    masked = key == "wifi_pass" and val
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
        _text(surf, lbl, 16, col, bold=True, x=bx2+252, y=cy-10)

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
            (cs.get("apply_msg",""), (100,180,80),  _CS_BTN_Y - 32),
            (cs.get("test_msg",""),  (100,160,220), _CS_BTN_Y - 20),
            (cs.get("inet_msg",""),  (140,200,140), _CS_BTN_Y - 8)]:
        if msg:
            _text(surf, msg, 13, col, cx=DISPLAY_W//2, y=y_off)

    # Action buttons (SCAN / APPLY / TEST AHRS / TEST INTERNET)
    quart = (bw - 30) // 4
    _action_btn(surf, bx,                _CS_BTN_Y, quart, _CS_BTN_H, "SCAN WIFI",  "normal")
    _action_btn(surf, bx+1*(quart+10),   _CS_BTN_Y, quart, _CS_BTN_H, "APPLY WIFI", "warn")
    _action_btn(surf, bx+2*(quart+10),   _CS_BTN_Y, quart, _CS_BTN_H, "TEST AHRS",  "ok")
    _action_btn(surf, bx+3*(quart+10),   _CS_BTN_Y, quart, _CS_BTN_H, "INTERNET",   "ok")
    surf.set_clip(_prev_clip)


def connectivity_setup_hit(x, y, cs):
    if 8 <= x <= 80 and 6 <= y <= 37:
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
    quart = (bw - 30) // 4
    if _CS_BTN_Y <= y <= _CS_BTN_Y+_CS_BTN_H:
        if bx <= x <= bx+quart:
            return "scan_wifi"
        if bx+1*(quart+10) <= x <= bx+2*quart+10:
            return "apply_wifi"
        if bx+2*(quart+10) <= x <= bx+3*quart+20:
            return "test_ahrs"
        if bx+3*(quart+10) <= x <= bx+4*quart+30:
            return "test_internet"
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
    ("fpl",  "FPL",      "full flight plan + active leg — pick one direction"),
)
# Stream sensors / state mirrors: exactly one of OFF/TX/RX.  See
# pi4/pfd.py for the rationale — both-on creates a
# publish→receive→republish echo loop.  Bugs/baro/nav don't have
# this problem (they only publish on user edits, gated by
# _ssync_suppress_publish).
_SCS_MUTEX_KINDS = {"ahrs", "gps", "fpl"}

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
    # data.  Bugs/baro/nav stay as independent TX + RX pills.
    for i, (kind, label, sub) in enumerate(_SCS_KINDS,
                                            start=_SCS_ROW_KINDS_OFS):
        bx2, by2, bw2, bh2 = _setting_row(surf, i, label, sub)
        if kind in _SCS_MUTEX_KINDS:
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
    # set_mode:<kind>:<off|tx|rx>; others emit toggle_publish /
    # toggle_consume as before.
    for i, (kind, _, _sub) in enumerate(_SCS_KINDS,
                                         start=_SCS_ROW_KINDS_OFS):
        by = _ss_row_y(i)
        if not (by <= y <= by + _SS_RH):
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
_SYS_BUILD   = "2026-04-10"
_SYS_INFO_Y  = 56
_SYS_INFO_LH = 28


_SYS_N_LINES = 7
_SYS_IH      = _SYS_N_LINES * _SYS_INFO_LH + 16
_SYS_MODE_Y    = _SYS_INFO_Y + _SYS_IH + 8        # DISPLAY MODE row top
_SYS_TERRAIN_Y = _SYS_MODE_Y + _SS_RH + 8         # TERRAIN DATA row top
_SYS_BTN_Y     = _SYS_TERRAIN_Y + _SS_RH + 8      # action buttons top
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
    _ahrs_xport = disp.get("cs", {}).get("ahrs_transport", "wifi")
    _ahrs_port  = disp.get("cs", {}).get("ahrs_port", "\u2014")
    _ahrs_lbl   = (f"USB \u00b7 {_ahrs_port}" if _ahrs_xport == "usb"
                   else f"WiFi \u00b7 {_ahrs_port}")
    lines = [
        ("Firmware version",  _SYS_VERSION),
        ("Build date",        _SYS_BUILD),
        ("Display",           f"{DISPLAY_W}\u00d7{DISPLAY_H}  DPI"),
        ("Hardware",          "Pi Zero 2W + Pico W"),
        ("GPS",               _gps_status),
        ("AHRS link",         _ahrs_lbl),
        ("SRTM terrain data", "loaded" if os.path.isdir(SRTM_DIR) else "not found"),
    ]
    # SYSTEM uses absolute Y positions (not _ss_row_y), so apply the
    # drag-scroll offset manually here.  Without this, the new GPS / AHRS
    # link rows pushed QUIT PFD off the bottom of the panel with no way
    # to reach it.
    _sy = _ss_scroll.get("system_setup", 0)
    info_y    = _SYS_INFO_Y    - _sy
    mode_y    = _SYS_MODE_Y    - _sy
    terrain_y = _SYS_TERRAIN_Y - _sy
    btn_y     = _SYS_BTN_Y     - _sy
    pygame.draw.rect(surf, (0,12,32), (bx, info_y, bw, _SYS_IH), border_radius=6)
    pygame.draw.rect(surf, (55,75,105), (bx, info_y, bw, _SYS_IH), width=1, border_radius=6)
    for i, (k, v) in enumerate(lines):
        ty = info_y + 10 + i*_SYS_INFO_LH
        _text(surf, k, 15, (130,150,175), x=bx+14, y=ty)
        _text(surf, v, 16, WHITE, bold=True, x=bx+310, y=ty)

    # ENABLE MFD row — feature gate.  When ON, the 3-finger 2 s hold
    # gesture in the PFD/MFD view swaps between the two; when OFF the
    # gesture is disarmed and the view is pinned to the PFD.
    _setting_row(surf, 0, "ENABLE MFD",
                 "Adds the Multi-Function Display · 3-finger 2 s hold to swap",
                 _y_override=mode_y)
    enabled = bool(disp["ds"].get("mfd_enabled", False))
    btn_h_m = _DSP_BTN_H; btn_w_m = 110; gap_m = _DSP_BTN_G
    rx = bx + bw - 2*(btn_w_m+gap_m) + gap_m - 14
    ry = mode_y + (_SS_RH - btn_h_m) // 2
    _seg_btn(surf, rx,              ry, btn_w_m, btn_h_m, "OFF", not enabled)
    _seg_btn(surf, rx+btn_w_m+gap_m, ry, btn_w_m, btn_h_m, "ON",  enabled)

    # Data download tiles: TERRAIN | OBSTACLE | AIRPORT | AIRSPACE
    # (four columns \u2014 narrower than before but still finger-sized).
    quarter = (bw - 24) // 4
    n_tiles, used_mb = _td_disk_stats()
    _sys_data_tile(surf, bx,                       terrain_y, quarter, _SS_RH,
                   "TERRAIN",
                   f"{n_tiles} tile{'s' if n_tiles != 1 else ''}  \u00b7  {used_mb:.1f} MB",
                   active=True)
    od_cnt     = disp["od"].get("records", 0)
    od_mb      = disp["od"].get("used_mb", 0.0)
    od_expired = disp["od"].get("expired", False)
    if od_cnt:
        if od_expired:
            od_sub = f"{od_cnt:,} obs  \u00b7  \u26a0 EXP"
        else:
            od_sub = f"{od_cnt:,} obs  \u00b7  {od_mb:.1f} MB"
    else:
        od_sub = "Tap to download"
    _sys_data_tile(surf, bx + quarter + 8,         terrain_y, quarter, _SS_RH,
                   "OBSTACLE", od_sub, active=True)
    ad_cnt     = disp["ad"].get("records", 0)
    ad_expired = disp["ad"].get("expired", False)
    if ad_cnt:
        if ad_expired:
            ad_sub = f"{ad_cnt:,} apts  \u00b7  \u26a0 EXP"
        else:
            ad_sub = f"{ad_cnt:,} apts"
    else:
        ad_sub = "Tap to download"
    _sys_data_tile(surf, bx + 2 * (quarter + 8),   terrain_y, quarter, _SS_RH,
                   "AIRPORT", ad_sub, active=True)
    # AIRSPACE tile \u2014 uses disp["asp"] for status (filled by the
    # AIRSPACE DATA subscreen on load).
    asp_cnt = disp.get("asp", {}).get("records", 0)
    asp_sub = f"{asp_cnt} polygons" if asp_cnt else "Tap to set up"
    _sys_data_tile(surf, bx + 3 * (quarter + 8),   terrain_y, quarter, _SS_RH,
                   "AIRSPACE", asp_sub, active=True)

    half_w = (bw - 10) // 2
    # Layout: AHRS FIRMWARE (full-width), SIMULATOR+RESET (half-width), QUIT
    sim_y  = btn_y + _SYS_BTN_H + 10
    quit_y = sim_y + _SYS_BTN_H + 10
    _action_btn(surf, bx,           btn_y,  bw,     _SYS_BTN_H, "AHRS FIRMWARE",  "normal")
    _action_btn(surf, bx,           sim_y,  half_w, _SYS_BTN_H, "SIMULATOR",       "ok")
    _action_btn(surf, bx+half_w+10, sim_y,  half_w, _SYS_BTN_H, "RESET DEFAULTS",  "danger")
    _action_btn(surf, bx,           quit_y, bw,     _SYS_BTN_H, "QUIT PFD",        "danger")
    surf.set_clip(_prev_clip)


def system_setup_hit(x, y):
    if 8 <= x <= 80 and 6 <= y <= 37:
        return "back"
    bx = _SS_MX; bw = DISPLAY_W - 2*_SS_MX
    # Shift the incoming y into logical (unscrolled) coordinates so the
    # button hit-tests still match the constants that defined the layout.
    y += _ss_scroll.get("system_setup", 0)
    # ENABLE MFD row — OFF / ON toggle
    btn_h_m = _DSP_BTN_H; btn_w_m = 110; gap_m = _DSP_BTN_G
    rx = bx + bw - 2*(btn_w_m+gap_m) + gap_m - 14
    ry = _SYS_MODE_Y + (_SS_RH - btn_h_m) // 2
    if ry <= y <= ry + btn_h_m:
        if rx <= x <= rx + btn_w_m:
            return "set:mfd_enabled:off"
        if rx+btn_w_m+gap_m <= x <= rx+2*btn_w_m+gap_m:
            return "set:mfd_enabled:on"
    if _SYS_TERRAIN_Y <= y <= _SYS_TERRAIN_Y+_SS_RH:
        quarter = (bw - 24) // 4
        if bx <= x <= bx + quarter:
            return "terrain_data"
        if bx + quarter + 8 <= x <= bx + 2 * quarter + 8:
            return "obstacle_data"
        if bx + 2 * (quarter + 8) <= x <= bx + 3 * quarter + 16:
            return "airport_data"
        if bx + 3 * (quarter + 8) <= x <= bx + 4 * quarter + 24:
            return "airspace_data"
    half_w = (bw - 10) // 2
    sim_y  = _SYS_BTN_Y + _SYS_BTN_H + 10
    quit_y = sim_y       + _SYS_BTN_H + 10
    # AHRS FIRMWARE (full-width, top button row)
    if _SYS_BTN_Y <= y <= _SYS_BTN_Y + _SYS_BTN_H and bx <= x <= bx + bw:
        return "ahrs_firmware"
    # SIMULATOR + RESET DEFAULTS (half-width)
    if sim_y <= y <= sim_y + _SYS_BTN_H:
        if bx <= x <= bx + half_w:
            return "simulator"
        if bx + half_w + 10 <= x <= bx + half_w + 10 + half_w:
            return "reset_defaults"
    # QUIT PFD (full-width)
    if quit_y <= y <= quit_y + _SYS_BTN_H and bx <= x <= bx + bw:
        return "quit"
    return None


# ── AHRS firmware loader / flasher ───────────────────────────────────────────
# Pushes the firmware/*.py files to a running Pico over USB-CDC (via mpremote)
# and, optionally, copies a MicroPython .uf2 to a Pico booted into BOOTSEL
# (RPI-RP2 mass-storage device).  Verbatim port of the pi4 implementation.

_FW_DIR     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "firmware")
_FW_SCRIPTS = ["main.py", "config.py", "web_server.py", "wt901.py",
               "bme280.py", "gps.py", "airdata.py", "sdp31.py",
               "ms4525.py", "ahrs_filter.py"]
_IPHONE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "iphone_display")
_FW_WEB     = ["index.html", "terrain.js", "sw.js", "manifest.webmanifest", "icon-192.png"]
_FW_ROW_H   = 76
_FW_Y0      = 56

_pico_serial_cache  = (0.0, None)
_pico_bootsel_cache = (0.0, None)
_PICO_CACHE_TTL = 2.0


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
    try:
        r = subprocess.run(
            ["lsblk", "-o", "NAME,LABEL", "--noheadings", "--raw"],
            capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] in _LABELS:
                label   = parts[1]
                devpath = f"/dev/{parts[0]}"
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
    """Return first .uf2 file in firmware/ dir, or None."""
    import glob
    files = sorted(glob.glob(os.path.join(_FW_DIR, "*.uf2")))
    return files[0] if files else None


def _do_push_scripts():
    disp["fw"]["push_state"] = "pushing"
    disp["fw"]["push_msg"]   = "Starting…"
    def _worker():
        global _sse_client
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
                time.sleep(1.5)
        except ImportError:
            pass

        port = _find_pico_serial()
        if not port:
            disp["fw"]["push_msg"]   = "Pico not detected — check USB cable"
            disp["fw"]["push_state"] = "error"
            return
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
            if released_serial_port and _sse_client is None:
                try:
                    from serial_client import SerialClient as _SC
                    time.sleep(3)
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
            shutil.copy(uf2, dest)
            disp["fw"]["flash_msg"]   = "Flashed — Pico will auto-reboot"
            disp["fw"]["flash_state"] = "done"
        except Exception as e:
            disp["fw"]["flash_msg"]   = str(e)[:80]
            disp["fw"]["flash_state"] = "error"
    threading.Thread(target=_worker, daemon=True).start()


def draw_ahrs_firmware(surf):
    _screen_header(surf, "AHRS FIRMWARE")
    _prev_clip = _ss_clip_to_content(surf)
    bx = _SS_MX; bw = DISPLAY_W - 2*_SS_MX
    fw = disp["fw"]

    # ── Device status row ────────────────────────────────────────────────
    row_y = _FW_Y0
    pygame.draw.rect(surf, (0,12,32), (bx, row_y, bw, _FW_ROW_H), border_radius=6)
    pygame.draw.rect(surf, (40,55,80), (bx, row_y, bw, _FW_ROW_H), width=1, border_radius=6)
    _text(surf, "DEVICE STATUS", 13, (130,160,190), bold=True, x=bx+14, y=row_y+8)

    serial = _find_pico_serial()
    s_col  = (60,220,80) if serial else (90,100,115)
    s_lbl  = serial if serial else "not detected"
    pygame.draw.circle(surf, s_col, (bx+22, row_y+44), 7)
    _text(surf, f"USB Serial:  {s_lbl}", 15, s_col, bold=bool(serial),
          x=bx+38, y=row_y+34)

    bootsel = _find_pico_bootsel()
    b_col   = (60,220,80) if bootsel else (90,100,115)
    b_lbl   = bootsel if bootsel else "not mounted"
    pygame.draw.circle(surf, b_col, (bx + bw//2 + 8, row_y+44), 7)
    _text(surf, f"BOOTSEL:  {b_lbl}", 15, b_col, bold=bool(bootsel),
          x=bx + bw//2 + 22, y=row_y+34)

    # ── Push scripts row ─────────────────────────────────────────────────
    row_y += _FW_ROW_H + 8
    pygame.draw.rect(surf, (0,12,32), (bx, row_y, bw, _FW_ROW_H), border_radius=6)
    pygame.draw.rect(surf, (40,55,80), (bx, row_y, bw, _FW_ROW_H), width=1, border_radius=6)
    _text(surf, "PUSH SCRIPTS", 16, WHITE, bold=True, x=bx+14, y=row_y+8)
    files_lbl = "  ".join(_FW_SCRIPTS)
    if _get_font(12).size(files_lbl)[0] > bw - 28:
        files_lbl = "main.py  config.py  web_server.py  + 3 more"
    _text(surf, files_lbl, 12, (130,150,180), x=bx+14, y=row_y+30)
    ps = fw.get("push_state", "")
    pm = fw.get("push_msg",   "")
    p_col = (60,220,80) if ps=="done" else (220,80,80) if ps=="error" else (180,180,100)
    if pm:
        _text(surf, pm, 13, p_col, bold=True, x=bx+14, y=row_y+52)

    # ── Flash .uf2 row ───────────────────────────────────────────────────
    row_y += _FW_ROW_H + 8
    pygame.draw.rect(surf, (0,12,32), (bx, row_y, bw, _FW_ROW_H), border_radius=6)
    pygame.draw.rect(surf, (40,55,80), (bx, row_y, bw, _FW_ROW_H), width=1, border_radius=6)
    _text(surf, "FLASH MICROPYTHON  (.uf2)", 16, WHITE, bold=True, x=bx+14, y=row_y+8)
    uf2 = _find_uf2()
    uf2_lbl = os.path.basename(uf2) if uf2 else "no .uf2 found in firmware/"
    _text(surf, uf2_lbl, 12, (130,150,180) if uf2 else (180,120,70),
          x=bx+14, y=row_y+30)
    fs = fw.get("flash_state", "")
    fm = fw.get("flash_msg",   "")
    f_col = (60,220,80) if fs=="done" else (220,80,80) if fs=="error" else (180,180,100)
    if fm:
        _text(surf, fm, 13, f_col, bold=True, x=bx+14, y=row_y+52)
    else:
        _text(surf, "Hold BOOTSEL + connect USB, then tap FLASH .UF2",
              12, (130,145,165), x=bx+14, y=row_y+52)

    # ── Action buttons ───────────────────────────────────────────────────
    btn_y  = row_y + _FW_ROW_H + 14
    half   = (bw - 10) // 2
    push_style  = "normal" if fw.get("push_state")  != "pushing"  else "warn"
    flash_style = "normal" if fw.get("flash_state") != "flashing" else "warn"
    _action_btn(surf, bx,          btn_y, half, 56, "PUSH SCRIPTS TO PICO", push_style)
    _action_btn(surf, bx+half+10,  btn_y, half, 56, "FLASH .UF2",           flash_style)
    surf.set_clip(_prev_clip)


def ahrs_firmware_hit(x, y):
    if 8 <= x <= 80 and 6 <= y <= 37:
        return "back"
    bx = _SS_MX; bw = DISPLAY_W - 2*_SS_MX
    btn_y = _FW_Y0 + 3 * (_FW_ROW_H + 8) + 14
    half  = (bw - 10) // 2
    if btn_y <= y <= btn_y + 56:
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


# ── SRTM1 → SRTM3 compactor ────────────────────────────────────────────────
# One-shot maintenance op: walks SRTM_DIR, decimates every SRTM1 .hgt to
# SRTM3 in place.  Same atomic-rewrite logic as tools/compact_srtm.py so
# an interrupted run can't leave half-written files.  Reuses the download
# progress overlay — only one of {downloading, compacting} runs at a
# time, gated in the UI.

_SRTM1_BYTES = 3601 * 3601 * 2
_SRTM3_BYTES = 1201 * 1201 * 2


def _td_count_srtm1():
    """Return (n_srtm1, total_reclaimable_bytes) over SRTM_DIR."""
    if not os.path.isdir(SRTM_DIR):
        return 0, 0
    n = 0
    saved = 0
    for fname in os.listdir(SRTM_DIR):
        if not fname.endswith(".hgt"):
            continue
        try:
            sz = os.path.getsize(os.path.join(SRTM_DIR, fname))
        except OSError:
            continue
        if sz == _SRTM1_BYTES:
            n += 1
            saved += _SRTM1_BYTES - _SRTM3_BYTES
    return n, saved


def _td_compact_worker():
    """Background thread: decimate every SRTM1 .hgt to SRTM3 in place."""
    td = disp["td"]
    td["compacting"]   = True
    td["dl_cancel"]    = False
    td["dl_region"]    = "Compact SRTM"
    try:
        import numpy as np
    except ImportError:
        td["dl_status"]   = "numpy required"
        td["compacting"]  = False
        return
    candidates = []
    if os.path.isdir(SRTM_DIR):
        for fname in sorted(os.listdir(SRTM_DIR)):
            if fname.endswith(".hgt"):
                candidates.append(fname)
    td["dl_total"]   = len(candidates)
    td["dl_current"] = 0
    n_done = n_skip = n_err = 0
    bytes_saved = 0
    for i, fname in enumerate(candidates):
        if td.get("dl_cancel"):
            td["dl_status"] = (f"Cancelled  ({n_done} compacted, "
                               f"{bytes_saved/1e9:.2f} GB freed)")
            td["compacting"] = False
            return
        td["dl_current"] = i
        path = os.path.join(SRTM_DIR, fname)
        try:
            sz = os.path.getsize(path)
        except OSError:
            n_err += 1
            continue
        if sz != _SRTM1_BYTES:
            n_skip += 1
            continue
        td["dl_status"] = f"Compacting {fname}…"
        try:
            raw = np.fromfile(path, dtype='>i2').reshape(3601, 3601)
            small = raw[::3, ::3].copy()
            del raw
            tmp = path + ".tmp"
            small.astype('>i2').tofile(tmp)
            if os.path.getsize(tmp) != _SRTM3_BYTES:
                os.remove(tmp)
                raise RuntimeError("size mismatch on write")
            os.replace(tmp, path)
            n_done += 1
            bytes_saved += _SRTM1_BYTES - _SRTM3_BYTES
        except Exception as exc:
            td["dl_status"] = f"Error {fname}: {exc}"
            n_err += 1
    td["dl_current"]  = len(candidates)
    td["dl_status"]   = (f"Done ✓  {n_done} compacted, "
                         f"{bytes_saved/1e9:.2f} GB freed"
                         + (f", {n_skip} already SRTM3" if n_skip else "")
                         + (f", {n_err} errors" if n_err else ""))
    td["compacting"]  = False
    # Refresh the load_tile in-memory cache so the next render reads the
    # now-smaller files (otherwise the cache holds float32 arrays built
    # from the old SRTM1 read path until LRU evicts them naturally).
    try:
        from terrain import _tile_cache
        _tile_cache.clear()
    except (ImportError, AttributeError):
        pass


def _td_start_compact():
    """Kick off the SRTM1 → SRTM3 compactor in the background."""
    t = threading.Thread(target=_td_compact_worker, daemon=True,
                         name="SRTMCompact")
    t.start()


def _tdc_download_thread():
    """Background download of all Mapzen Terrarium PNG coarse tiles for
    lat -60° to +75° at zoom 5 — ~576 tiles, ~8 MB total.  Matches the
    iPhone PFD's downloadCoarse() behaviour."""
    td = disp["td"]
    td["downloading"] = True
    td["dl_region"]   = "Global Low-Res"
    tiles = coarse_tile_list()
    td["dl_total"]    = len(tiles)
    td["dl_current"]  = 0
    td["dl_cancel"]   = False
    os.makedirs(COARSE_DIR, exist_ok=True)
    ok = skip = err = 0
    for i, (z, x, y) in enumerate(tiles):
        if td["dl_cancel"]:
            td["dl_status"] = f"Cancelled  ({ok} new, {skip} skipped)"
            td["downloading"] = False
            return
        td["dl_current"] = i
        dest = coarse_tile_path(COARSE_DIR, z, x, y)
        if os.path.exists(dest):
            skip += 1
            continue
        url = coarse_tile_url(z, x, y)
        td["dl_status"] = f"Downloading {z}/{x}/{y}.png"
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                data = resp.read()
            with open(dest + ".tmp", "wb") as f:
                f.write(data)
            os.replace(dest + ".tmp", dest)
            ok += 1
        except Exception as exc:
            td["dl_status"] = f"Error {z}/{x}/{y}: {exc}"
            err += 1
    td["dl_current"] = len(tiles)
    td["dl_status"]  = (f"Done ✓  {ok} downloaded"
                        + (f", {skip} skipped" if skip else "")
                        + (f", {err} errors"   if err  else ""))
    td["downloading"] = False
    global _has_terrain
    _has_terrain = _check_terrain()


def _tdc_start_download():
    """Kick off the global-coarse download in a background thread."""
    t = threading.Thread(target=_tdc_download_thread, daemon=True)
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


_state_lines = None  # admin_1; populated on startup by _ne_load_cache(); rebound
                     # after WATER MASKS download completes, or lazy-reloaded on
                     # MFD render if the .npz appears after startup (e.g. rsync
                     # from a pi4 already holding the cache).
_state_lines_last_try = 0.0  # monotonic clock of last lazy-load attempt

_country_lines = None        # admin_0 — same lifecycle as _state_lines.
_country_lines_last_try = 0.0


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

        # State / province (admin_1) + country (admin_0) boundary polylines.
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
    _text(surf, stat_str, 15, stat_col, bold=True, cx=DISPLAY_W//2, cy=66)

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
    if 8 <= x <= 80 and 6 <= y <= 37:
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

_AD_MX = 12

def _ad_load_airports():
    """(Re-)load the airport cache into the module-level array.

    pi_zero deliberately omits the runway cache (and the runway / extended-
    centerline AI overlays) — the small panel is too cluttered to make use
    of them.  Obstacles + airport symbols / signposts remain.
    """
    global _airports
    os.makedirs(AIRPORT_DIR, exist_ok=True)
    _airports = apt_mod.load(AIRPORT_DIR)
    cnt, mb = apt_mod.disk_stats(AIRPORT_DIR)
    disp["ad"]["records"] = cnt
    disp["ad"]["used_mb"] = mb
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
    ad = disp["ad"]
    ad["downloading"] = True
    ad["dl_cancel"]   = False
    ad["dl_status"]   = "Connecting to OurAirports\u2026"
    os.makedirs(AIRPORT_DIR, exist_ok=True)

    def _download_file(url, path, label):
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
    cache_apt = os.path.join(AIRPORT_DIR, apt_mod.CACHE_FILENAME)

    try:
        if not _download_file(apt_mod.AIRPORTS_CSV_URL, csv_apt, "airports.csv"):
            ad["dl_status"]   = "Cancelled"
            ad["downloading"] = False
            return
        try: os.remove(cache_apt)
        except Exception: pass
        ad["dl_status"] = "Parsing airport records\u2026"
        ad["parsing"]   = True
        _ad_load_airports()
        ad["parsing"]   = False
        cnt = ad["records"]
        ad["dl_status"] = f"Done \u2713  {cnt:,} apts"
    except Exception as exc:
        ad["dl_status"] = f"Error: {exc}"
    finally:
        ad["downloading"] = False


def _ad_start_download():
    t = threading.Thread(target=_ad_download_thread, daemon=True,
                         name="AirportDownload")
    t.start()


def draw_airport_data(surf, ad):
    """Full-screen airport data management screen."""
    surf.fill((0, 0, 0))
    _screen_header(surf, "AIRPORT DATA")
    bx = _AD_MX; bw = DISPLAY_W - 2*_AD_MX

    cnt      = ad.get("records", 0)
    used_mb  = ad.get("used_mb", 0.0)
    expired  = ad.get("expired", False)
    age      = ad.get("age_days", 0)

    pygame.draw.rect(surf, (0,12,32), (bx, 52, bw, 28), border_radius=4)
    pygame.draw.rect(surf, (40,60,90), (bx, 52, bw, 28), width=1, border_radius=4)
    if cnt:
        age_str = f"  \u00b7  {age} day{'' if age == 1 else 's'} old"
        if expired:
            age_str += "  (expired)"
            stat_col = (220, 130, 60)
        else:
            stat_col = (60, 220, 80)
        stat_str = f"{cnt:,} airports  \u00b7  {used_mb:.1f} MB{age_str}"
    else:
        stat_str = "No airport data on disk"
        stat_col = YELLOW
    _text(surf, stat_str, 14, stat_col, bold=True, cx=DISPLAY_W//2, cy=66)

    downloading = ad.get("downloading", False)
    parsing     = ad.get("parsing", False)

    info_y = 92
    info_h = 82
    pygame.draw.rect(surf, (0,10,26), (bx, info_y, bw, info_h), border_radius=6)
    pygame.draw.rect(surf, (40,55,80), (bx, info_y, bw, info_h), width=1, border_radius=6)
    _text(surf, "OurAirports Global Database", 15, WHITE, bold=True,
          cx=DISPLAY_W//2, cy=info_y+14)
    _text(surf, "\u2248 72,000 airports worldwide",
          9, (140,160,185), cx=DISPLAY_W//2, cy=info_y+30)
    _text(surf, "CSV \u2248 12 MB  \u00b7  community-maintained",
          9, (120,140,165), cx=DISPLAY_W//2, cy=info_y+44)
    _text(surf, "Shown on AI as cyan rings (apt), magenta H (helipad)",
          9, (120,140,165), cx=DISPLAY_W//2, cy=info_y+58)
    _text(surf, "WiFi required for download",
          9, (160,130,60), cx=DISPLAY_W//2, cy=info_y+72)

    btn_y = info_y + info_h + 10
    btn_h = 48
    if downloading or parsing:
        bg = (0,20,10); oc = (40,140,60)
    else:
        bg = (0,18,45); oc = WHITE
    pygame.draw.rect(surf, bg, (bx, btn_y, bw, btn_h), border_radius=6)
    pygame.draw.rect(surf, oc, (bx, btn_y, bw, btn_h), width=2, border_radius=6)
    btn_label = "UPDATE" if cnt else "DOWNLOAD"
    tc = (70,80,90) if (downloading or parsing) else WHITE
    _text(surf, btn_label, 14, tc, bold=True, cx=DISPLAY_W//2, cy=btn_y+btn_h//2-6)
    _text(surf, "airports.csv  from  ourairports-data", 9,
          (100,120,140) if not (downloading or parsing) else (60,80,70),
          cx=DISPLAY_W//2, cy=btn_y+btn_h//2+10)

    prog_y = btn_y + btn_h + 8
    prog_h = 40
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
        pygame.draw.rect(surf, (0,22,12), (bx+10, prog_y+24, bw-20, 8), border_radius=3)
        if bar_w > 0:
            pygame.draw.rect(surf, (40,180,60), (bx+10, prog_y+24, bar_w, 8), border_radius=3)
        _text(surf, status_msg, 9, (140,160,180), cx=DISPLAY_W//2, cy=prog_y+12)
        _action_btn(surf, bw-70, prog_y+4, 62, 28, "CANCEL", "danger", r=5)
    elif parsing:
        _text(surf, status_msg, 9, (140,180,140), cx=DISPLAY_W//2, cy=prog_y+20)
    else:
        col = (60,220,80) if status_msg.startswith("Done") else (160,160,170)
        _text(surf, status_msg, 9, col, cx=DISPLAY_W//2, cy=prog_y+20)

    # ── Symbol legend — visual key for the three airport types ──────────
    leg_y = prog_y + prog_h + 12
    leg_h = 34
    pygame.draw.rect(surf, (0,8,20), (bx, leg_y, bw, leg_h), border_radius=4)
    _text(surf, "Symbol legend:", 12, (140,160,185), bold=True,
          x=bx+10, y=leg_y+9)
    # Public airport ring
    lx = bx + 130; ly = leg_y + 18
    pygame.draw.circle(surf, (120, 220, 255), (lx, ly), 5, 0)
    pygame.draw.circle(surf, (0, 10, 30), (lx, ly), 3, 0)
    _text(surf, "PUBLIC", 11, (170,180,195), x=lx+10, y=leg_y+10)
    # Heliport
    _text(surf, "H", 13, (220, 120, 220), bold=True, cx=bx+250, cy=ly)
    _text(surf, "HELIPORT", 11, (170,180,195), x=bx+260, y=leg_y+10)
    # Seaplane base
    sx = bx + 380
    pygame.draw.circle(surf, (150, 200, 255), (sx, ly), 4, 1)
    pygame.draw.line(surf, (150, 200, 255), (sx - 4, ly + 5), (sx + 4, ly + 5), 1)
    _text(surf, "SEAPLANE", 11, (170,180,195), x=sx+10, y=leg_y+10)

    # ── Display filters — toggle which airport types render on the AI ────
    # pi_zero shows airports + symbols only; runway polygons and extended
    # centerlines were dropped (too cluttered on the 640×480 panel).
    filt_y = leg_y + leg_h + 14
    filt_h = 30
    _text(surf, "Display filters:", 13, (140,160,185), x=bx+6, y=filt_y-14)
    bt_w = (bw - 30) // 4
    for i, (key, lbl) in enumerate([("show_public",   "PUBLIC"),
                                     ("show_heli",     "HELI"),
                                     ("show_seaplane", "WATER"),
                                     ("show_other",    "OTHER")]):
        bxi = bx + i * (bt_w + 10)
        _seg_btn(surf, bxi, filt_y, bt_w, filt_h, lbl, ad.get(key, False), r=5)


def airport_data_hit(x, y, ad):
    if 8 <= x <= 80 and 6 <= y <= 37:
        return "back"
    bx = _AD_MX; bw = DISPLAY_W - 2*_AD_MX
    btn_y = 92 + 82 + 10
    btn_h = 48
    # Filter toggle strip — match draw-side: symbol legend (leg_h=34 + 12+14)
    # sits between the download progress strip and the filter row.
    prog_y = btn_y + btn_h + 8
    prog_h = 40
    leg_y  = prog_y + prog_h + 12
    leg_h  = 34
    filt_y = leg_y + leg_h + 14
    filt_h = 30
    bt_w   = (bw - 30) // 4
    if filt_y <= y <= filt_y + filt_h:
        for i, key in enumerate(["show_public", "show_heli",
                                 "show_seaplane", "show_other"]):
            bxi = bx + i * (bt_w + 10)
            if bxi <= x <= bxi + bt_w:
                return f"toggle:{key}"
    if ad.get("downloading"):
        if (bx+bw-70 <= x <= bx+bw and prog_y+4 <= y <= prog_y+32):
            return "cancel"
    if bx <= x <= bx+bw and btn_y <= y <= btn_y+btn_h:
        return "download"
    return None


# ── Airspace data management ────────────────────────────────────────────
# Mirrors AIRPORT DATA: status strip + info box + action button +
# progress.  Two action paths:
#
#   DOWNLOAD       — fetch airspaces.json from asp_mod.DOWNLOAD_URL.
#                    Disabled with a hint when DOWNLOAD_URL is empty.
#   INSTALL EXAMPLE — write the bundled small dataset to disk so the
#                    pilot can verify the render path before sourcing
#                    real data.
#
# After either, the module-level _airspaces is reloaded so the MFD's
# inset picks up the new polygons without a restart.

def _asp_reload():
    """Re-read airspaces.json from disk + repopulate disp["asp"]."""
    global _airspaces
    loaded = asp_mod.load(AIRSPACE_DIR)
    if loaded is None:
        loaded = asp_mod.load_bundled_example()
        disp["asp"]["records"] = 0
    else:
        disp["asp"]["records"] = len(loaded)
    _airspaces = loaded


def _asp_install_example():
    """Write the bundled example dataset over airspaces.json."""
    asp = disp["asp"]
    asp["dl_status"] = "Writing example dataset…"
    try:
        asp_mod.write_example(AIRSPACE_DIR)
        _asp_reload()
        asp["dl_status"] = f"Done ✓  example dataset ({asp['records']} polygons)"
    except Exception as exc:
        asp["dl_status"] = f"Error: {exc}"


def _asp_build_from_geojson():
    """In-process FAA GeoJSON → airspaces.json conversion.  Reads
    every *.geojson in AIRSPACE_DIR, runs the converter from
    tools/build_airspaces_us.py, writes airspaces.json next to the
    sources, reloads.

    Pilot workflow: scp / rsync the two FAA GeoJSON files (Class
    Airspace + Special Use Airspace) into AIRSPACE_DIR, tap BUILD,
    the converted polygons land on disk and the MFD picks them up
    on the next render."""
    asp = disp["asp"]
    asp["dl_status"] = "Building airspaces.json from *.geojson…"

    def _worker():
        try:
            # Late import — the tools/ dir is not on the runtime
            # import path, so we attach it on first use.  This keeps
            # the runtime cold-start cheap when nobody hits BUILD.
            import sys as _sys
            _tools = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "..", "tools")
            _tools = os.path.abspath(_tools)
            if _tools not in _sys.path:
                _sys.path.insert(0, _tools)
            import build_airspaces_us as _bldr
            stats = _bldr.build_from_dir(
                AIRSPACE_DIR,
                source_note="FAA GeoJSON (built on pi_zero)")
            _asp_reload()
            if stats.get("files", 0) == 0:
                asp["dl_status"] = (f"No *.geojson found in {AIRSPACE_DIR} "
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


def _asp_download_thread():
    """Fetch every URL in asp_mod.DOWNLOAD_SOURCES, save each as the
    keyed filename in AIRSPACE_DIR, then auto-build airspaces.json
    from the freshly downloaded *.geojson files.  Cancel-safe via
    asp["dl_cancel"]."""
    asp = disp["asp"]
    asp["downloading"] = True
    asp["dl_cancel"]   = False
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
            label = f"[{i}/{len(sources)}] {fname}"
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
        # All sources fetched — convert in-process.
        asp["dl_status"] = "Building airspaces.json…"
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
            asp["dl_status"] = (f"Downloaded {len(sources)} files; "
                                f"build had errors: {stats['errors'][0]}")
        else:
            asp["dl_status"] = (
                f"Done ✓  {stats['records']} polygons "
                f"(B:{stats['B']} C:{stats['C']} D:{stats['D']} "
                f"MOA:{stats['MOA']} R:{stats['R']})")
    except Exception as exc:
        asp["dl_status"] = f"Error: {exc}"
    finally:
        asp["downloading"] = False


def _asp_start_download():
    threading.Thread(target=_asp_download_thread, daemon=True,
                     name="AirspaceDownload").start()


def draw_airspace_data(surf):
    """Full-screen airspace data management — same layout vocabulary
    as draw_airport_data so the two screens feel like siblings."""
    surf.fill((0, 0, 0))
    _screen_header(surf, "AIRSPACE DATA")
    bx = _AD_MX; bw = DISPLAY_W - 2 * _AD_MX
    asp = disp["asp"]
    cnt = asp.get("records", 0)

    # Status strip
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
    _text(surf, "US Airspace Polygons (B/C/D/MOA/R)", 15, WHITE,
          bold=True, cx=DISPLAY_W // 2, cy=info_y + 14)
    _text(surf, "Rendered on the MFD moving map by class.",
          10, (140, 160, 185), cx=DISPLAY_W // 2, cy=info_y + 32)
    _text(surf, "Master toggle: Display Settings → ASP pill.",
          10, (140, 160, 185), cx=DISPLAY_W // 2, cy=info_y + 48)
    src_count = len([v for v in
                      getattr(asp_mod, "DOWNLOAD_SOURCES", {}).values()
                      if v])
    if src_count:
        _text(surf, f"DOWNLOAD: fetch {src_count} FAA GeoJSON sources",
              9, (140, 170, 200), cx=DISPLAY_W // 2, cy=info_y + 66)
        _text(surf, "→ saved here, then auto-built into airspaces.json",
              9, (120, 140, 165), cx=DISPLAY_W // 2, cy=info_y + 80)
    else:
        _text(surf, "DOWNLOAD_SOURCES empty in shared/airspaces.py",
              9, (180, 140, 60), cx=DISPLAY_W // 2, cy=info_y + 66)

    # Single action button: DOWNLOAD (auto-builds after fetch).
    # CANCEL replaces it while a download is in flight.  TFR row will
    # land here as a second download with its own date stamp once
    # that data source is wired in.
    btn_y = info_y + info_h + 12
    btn_h = 48
    downloading = asp.get("downloading", False)
    dl_style    = "normal" if not downloading else "warn"
    _action_btn(surf, bx, btn_y, bw, btn_h,
                "DOWNLOAD" if not downloading else "CANCEL", dl_style)

    # Progress / status line
    prog_y = btn_y + btn_h + 16
    prog_h = 40
    pygame.draw.rect(surf, (0, 10, 24), (bx, prog_y, bw, prog_h),
                     border_radius=6)
    pygame.draw.rect(surf, (35, 50, 75), (bx, prog_y, bw, prog_h),
                     width=1, border_radius=6)
    status = asp.get("dl_status", "")
    if status:
        col = ((60, 220, 80) if status.startswith("Done")
               else (220, 130, 60) if status.startswith("Error")
               else (160, 160, 170))
        _text(surf, status, 11, col, cx=DISPLAY_W // 2, cy=prog_y + 20)


def airspace_data_hit(x, y):
    if 8 <= x <= 80 and 6 <= y <= 37:
        return "back"
    bx = _AD_MX; bw = DISPLAY_W - 2 * _AD_MX
    info_y = 92; info_h = 96
    btn_y  = info_y + info_h + 12
    btn_h  = 48
    if btn_y <= y <= btn_y + btn_h and bx <= x <= bx + bw:
        return "download"
    return None


def draw_terrain_data(surf, td):
    """Full-screen terrain data management screen."""
    _screen_header(surf, "TERRAIN DATA")
    bx = _TD_MX; bw = DISPLAY_W - 2*_TD_MX
    n_tiles, used_mb = _td_disk_stats()
    c_tiles, c_mb    = coarse_disk_stats(COARSE_DIR)
    n_srtm1, srtm1_reclaim = _td_count_srtm1()
    compacting = td.get("compacting", False)

    # Status strip — two lines: SRTM hi-res + Mapzen coarse, with a
    # COMPACT pill on the right edge when any SRTM1 tiles are on disk.
    strip_h = 46
    pygame.draw.rect(surf, (0,12,32), (bx, 52, bw, strip_h), border_radius=4)
    pygame.draw.rect(surf, (40,60,90), (bx, 52, bw, strip_h), width=1, border_radius=4)
    # COMPACT button (right side of strip) — shown when SRTM1 tiles exist
    # OR while a compact run is in progress.
    show_compact = (n_srtm1 > 0) or compacting
    cp_w = 92; cp_h = 32
    cp_x = bx + bw - cp_w - 8
    cp_y = 52 + (strip_h - cp_h) // 2
    if show_compact:
        cp_label = "COMPACTING…" if compacting else "COMPACT"
        _action_btn(surf, cp_x, cp_y, cp_w, cp_h, cp_label,
                    "warn" if compacting else "ok", r=5)
        # Text shifts left to avoid the button
        text_cx = (bx + cp_x) // 2
    else:
        text_cx = DISPLAY_W // 2

    if n_tiles:
        if n_srtm1 > 0:
            hi_str = (f"SRTM:  {n_tiles} tiles  ·  {used_mb:.0f} MB"
                      f"  ({n_srtm1} SRTM1 → save "
                      f"{srtm1_reclaim/1e9:.1f} GB)")
        else:
            hi_str = (f"SRTM:  {n_tiles} tile{'s' if n_tiles != 1 else ''}"
                      f"  ·  {used_mb:.1f} MB  ·  SRTM3")
        hi_col = (60,220,80)
    else:
        hi_str = "SRTM:  none on disk"
        hi_col = YELLOW
    if c_tiles:
        co_str = f"Mapzen global:  {c_tiles} tiles  ·  {c_mb:.1f} MB"
        co_col = (60,220,80)
    else:
        co_str = "Mapzen global:  none on disk"
        co_col = (180,160,80)
    _text(surf, hi_str, 13, hi_col, bold=True, cx=text_cx, cy=64)
    _text(surf, co_str, 13, co_col, bold=True, cx=text_cx, cy=85)

    # `downloading` here gates the region tiles + cancel button to cover
    # downloads and the compact pass — only one heavy job runs at a time.
    downloading = td.get("downloading", False) or compacting
    cur_region  = td.get("dl_region", "")
    rows = (len(_TD_REGIONS) + _TD_COLS - 1) // _TD_COLS
    top_y = 52 + strip_h + 6
    available_h = DISPLAY_H - top_y - _TD_GAP*(rows-1) - 8
    bh = available_h // (rows + 1)   # +1 row for the two top action buttons

    # ── Top row: CURRENT AREA, GLOBAL LOW-RES, WATER MASKS (1/3 each) ────
    third_w = (bw - 2 * _TD_GAP) // 3
    wd_downloading = disp.get("wd", {}).get("downloading", False)

    def _draw_action_tile(rx, ry, rw, label, sub, active):
        any_busy = downloading or wd_downloading
        col = (0,28,18) if active else ((50,50,70) if any_busy else (0,18,45))
        oc  = (40,180,60) if active else ((70,70,95) if any_busy else WHITE)
        pygame.draw.rect(surf, col, (rx, ry, rw, bh), border_radius=6)
        if not any_busy or active:
            gh = bh // 5
            for i in range(gh):
                t = 1.0 - i/gh
                gc = (int(15+t*25), int(20+t*40), int(40+t*65))
                pygame.draw.line(surf, gc, (rx+6, ry+1+i), (rx+rw-6, ry+1+i))
        pygame.draw.rect(surf, oc, (rx, ry, rw, bh), width=2, border_radius=6)
        tc = (40,180,60) if active else ((70,80,90) if any_busy else WHITE)
        _text(surf, label, 15, tc, bold=True, cx=rx+rw//2, cy=ry+bh//2-10)
        _text(surf, sub, 11,
              (110,130,150) if not active else (60,180,80),
              cx=rx+rw//2, cy=ry+bh//2+10)

    lat_i = int(disp.get("lat", DEMO_LAT)); lon_i = int(disp.get("lon", DEMO_LON))
    area_str = f"25 tiles  ·  ≈ 35 MB"
    _draw_action_tile(bx, top_y, third_w, "CURRENT AREA", area_str,
                      active=(downloading and cur_region == "Current Area"))
    n_coarse   = len(coarse_tile_list())
    coarse_str = f"~{n_coarse} tiles  ·  ≈ 8 MB"
    _draw_action_tile(bx + third_w + _TD_GAP, top_y, third_w,
                      "GLOBAL LOW-RES", coarse_str,
                      active=(downloading and cur_region == "Global Low-Res"))
    # Water tile uses the same active highlight when its dedicated worker
    # is running.  Re-runs the rasteriser for every existing SRTM tile.
    w_tiles, w_mb = water_mod.disk_stats(WATER_DIR)
    if w_tiles:
        water_sub = f"{w_tiles} tiles  ·  {w_mb:.1f} MB"
    else:
        water_sub = "needs pyshp + SRTM"
    _draw_action_tile(bx + 2*(third_w + _TD_GAP), top_y, third_w,
                      "WATER MASKS", water_sub, active=wd_downloading)

    # ── Preset region grid ────────────────────────────────────────────────────
    grid_y = top_y + bh + _TD_GAP
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
    if downloading:
        prog_y = DISPLAY_H - 58
        cur = td.get("dl_current", 0); total = max(1, td.get("dl_total", 1))
        frac = cur / total
        pygame.draw.rect(surf, (0,12,32), (bx, prog_y, bw, 50), border_radius=6)
        pygame.draw.rect(surf, (55,75,105), (bx, prog_y, bw, 50), width=1, border_radius=6)
        bar_w = int((bw - 20) * frac)
        pygame.draw.rect(surf, (0,25,12), (bx+10, prog_y+28, bw-20, 12), border_radius=3)
        if bar_w > 0:
            pygame.draw.rect(surf, (40,180,60), (bx+10, prog_y+28, bar_w, 12), border_radius=3)
        _text(surf, td.get("dl_status",""), 10, (140,160,180),
              cx=DISPLAY_W//2, cy=prog_y+14)
        pct = f"{int(frac*100)}%  ({cur}/{total})"
        _text(surf, pct, 10, (60,220,80), cx=DISPLAY_W//2, cy=prog_y+43)
        # CANCEL button
        _action_btn(surf, DISPLAY_W-100-bx, prog_y+6, 92, 36, "CANCEL", "danger", r=5)
    else:
        # Show last status message if any
        last = td.get("dl_status", "")
        if last:
            _text(surf, last, 11, (80,160,100), cx=DISPLAY_W//2, cy=DISPLAY_H-12)


def terrain_data_hit(x, y, td):
    """Return action string or None."""
    if 8 <= x <= 80 and 6 <= y <= 37:
        return "back"
    bx = _TD_MX; bw = DISPLAY_W - 2*_TD_MX
    rows = (len(_TD_REGIONS) + _TD_COLS - 1) // _TD_COLS
    # Match the draw-side geometry: now-taller status strip + new
    # half-width action-tile row.
    strip_h = 46
    top_y = 52 + strip_h + 6
    available_h = DISPLAY_H - top_y - _TD_GAP*(rows-1) - 8
    bh = available_h // (rows + 1)
    third_w = (bw - 2 * _TD_GAP) // 3

    # COMPACT button on the right side of the status strip — same rect
    # the draw routine uses.  Only tappable when there are SRTM1 tiles
    # to compact AND no job (download or compact) is currently running.
    n_srtm1, _ = _td_count_srtm1()
    compacting = td.get("compacting", False)
    any_busy   = td.get("downloading") or disp.get("wd", {}).get("downloading") or compacting
    if n_srtm1 > 0 or compacting:
        cp_w = 92; cp_h = 32
        cp_x = bx + bw - cp_w - 8
        cp_y = 52 + (strip_h - cp_h) // 2
        if (cp_x <= x <= cp_x + cp_w and cp_y <= y <= cp_y + cp_h
                and not any_busy):
            return "compact"

    # Cancel button during download or compact (covers td, wd, compact)
    if (td.get("downloading") or disp.get("wd", {}).get("downloading")
            or compacting):
        prog_y = DISPLAY_H - 58
        if (DISPLAY_W-100-bx <= x <= DISPLAY_W-bx and
                prog_y+6 <= y <= prog_y+42):
            return "cancel"

    # Top action-tile row: CURRENT AREA + GLOBAL LOW-RES + WATER MASKS
    if top_y <= y <= top_y+bh:
        if bx <= x <= bx+third_w:
            return "current_area"
        if bx+third_w+_TD_GAP <= x <= bx+2*third_w+_TD_GAP:
            return "global_coarse"
        if bx+2*(third_w+_TD_GAP) <= x <= bx+2*(third_w+_TD_GAP)+third_w:
            return "water_masks"

    # Region grid
    grid_y = top_y + bh + _TD_GAP
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
                     hdg_src="mag", baro_ok=True):
    """
    Tap buttons in the heading strip — left and right only so the centre
    heading readout remains unobstructed:
      • Left  (under speed tape) : HDG bug  — MAGENTA=GPS TRK, CYAN=MAG
      • Right (under alt tape)   : Baro setting — CYAN=baro sensor, MAGENTA=GPS ALT
    IAS and ALT bug buttons are drawn at the tops of their own tapes.
    """
    # Bottom-row boxes fill the full HDG_H height of the heading strip so
    # they read cleanly on the small 640x480 Waveshare panel. Matches the
    # top-row speed / alt bug boxes (also bumped to HDG_H tall).
    y = HDG_Y

    # HDG bug — left side of heading strip; color matches heading bug triangle
    _hdg_btn = f"{round(hdg_bug) % 360:03d}\u00b0" if hdg_bug is not None else "---\u00b0"
    hdg_box_col = MAGENTA if hdg_src == "trk" else CYAN
    _cyan_box(surf, _hdg_btn, x=SPD_X, y=y, w=SPD_W, h=HDG_H,
              font_sz=24, col=hdg_box_col)

    # Baro — right side of heading strip; CYAN when baro sensor active, MAGENTA when GPS ALT
    # Accept any non-"gps" baro_src as meaning baro sensor is active (firmware uses
    # "bme280", sim/demo/preview code uses "baro" — both mean the same thing).
    if baro_ok and baro_src != "gps":
        baro_unit = disp["ds"].get("baro_unit", "inhg")
        if baro_unit == "hpa":
            baro_lbl = f"{baro_hpa:.0f}"
            baro_fsz = 24
        else:
            baro_lbl = f"{baro_hpa / 33.8639:.2f}"
            baro_fsz = 24    # cyan box border + display-setup choice tells the pilot the unit
        baro_col = CYAN
    else:
        baro_lbl = "GPS"
        baro_fsz = 24
        baro_col = MAGENTA
    _cyan_box(surf, baro_lbl,
              x=ALT_X + 1, y=y, w=ALT_W - 1, h=HDG_H, font_sz=baro_fsz, col=baro_col)


# ── Boot splash ───────────────────────────────────────────────────────────────
_BOOT_SPLASH_DIR = os.path.join(os.path.dirname(__file__), "assets")


def _find_boot_splash():
    """First boot_splash.* in pi_zero/assets/ (jpg, png, bmp, …).
    Lets the user drop in whatever format they have without renaming."""
    if not os.path.isdir(_BOOT_SPLASH_DIR):
        return None
    for name in sorted(os.listdir(_BOOT_SPLASH_DIR)):
        if name.startswith("boot_splash."):
            return os.path.join(_BOOT_SPLASH_DIR, name)
    return None


def _show_boot_splash(surf, flip_fn, hold_s=2.5):
    """Cover the screen with the boot splash image while airports/obstacles
    finish loading in the background. No-op if the asset is missing —
    the PFD just falls through to its first real frame as before."""
    path = _find_boot_splash()
    if not path:
        return
    try:
        img = pygame.image.load(path)
    except pygame.error as e:
        print(f"[PFD] boot splash load failed: {e}", file=sys.stderr)
        return
    iw, ih = img.get_size()
    # Cover-fit: scale so the smaller dimension fills the display, crop the
    # overflow. Preserves the photo's composition better than stretching.
    scale = max(DISPLAY_W / iw, DISPLAY_H / ih)
    sw, sh = int(iw * scale), int(ih * scale)
    img = pygame.transform.smoothscale(img, (sw, sh))
    surf.fill((0, 0, 0))
    surf.blit(img, ((DISPLAY_W - sw) // 2, (DISPLAY_H - sh) // 2))
    flip_fn()
    time.sleep(hold_s)


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
_OBS_WARNING_FT = OBSTACLE_WARNING_FT
_OBS_MIN_AGL_FT = 25.0    # hide DOF entries shorter than this so airport-
                          # surface clutter (signs, low markers, taxiway
                          # lighting) doesn't paint phantom towers.
# Airport-boundary declutter: anything shorter than _OBS_AIRPORT_FLOOR_FT
# AGL gets hidden when it sits within _OBS_AIRPORT_RADIUS_NM of any
# runway centroid.
_OBS_AIRPORT_RADIUS_NM = 1.0
_OBS_AIRPORT_FLOOR_FT  = 50.0

# Cache rendered obstacle labels keyed on (text, colour). pygame.font.render
# is ~1 ms each — a busy metro view can show 50+ towers whose MSL labels
# repeat, so caching cuts ~50 ms/frame on the Pi Zero 2W.
_obs_label_cache = {}
_OBS_LABEL_CACHE_MAX = 256


def _obs_label_blit(surf, text, color, cx, cy):
    """Blit a small obstacle MSL label, reusing cached pygame surfaces."""
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


def draw_obstacle_symbols(surf, ai_rect, lat, lon, alt_ft,
                          hdg_deg, pitch_deg, roll_deg):
    """
    Project nearby obstacles onto the AI viewport as red/amber tower symbols.

    Vectorised: candidate obstacles come back from obs_mod.query_nearby as
    a numpy structured array; all bearing/distance/vertical-angle math
    runs over the whole batch in numpy, and the Python loop only fires
    for the obstacles whose top anchor lands inside the AI rect AND
    passes the AGL / airport-boundary declutter filters. On the Pi Zero
    2W this is ~30× faster than the per-obstacle Python loop in metro
    views.
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

    # 10 px/deg matches draw_simple_ai_background / draw_pitch_ladder /
    # draw_airport_symbols / above-horizon silhouette. The earlier 8.0
    # left obstacle towers floating 20% off the horizon scale.
    PX_PER_DEG = 10.0

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

    # Airport-boundary declutter: hide low obstacles near airport centres.
    # pi4 uses runway centroids here for a tighter polygon; pi_zero
    # doesn't load runways, so we fall back to airport centres (the same
    # OurAirports DB the symbol layer uses).  Coarser, but kills the same
    # phantom-tower forest on the runway / ramp.
    inside_airport = _np.zeros(len(ob_lat), dtype=bool)
    if _airports is not None and len(_airports) > 0:
        nearby_apts = apt_mod.query_nearby(
            _airports, lat, lon,
            radius_nm=_OBS_RADIUS_NM + _OBS_AIRPORT_RADIUS_NM)
        if len(nearby_apts) > 0:
            apt_lat_c = nearby_apts["lat"].astype(_np.float64)
            apt_lon_c = nearby_apts["lon"].astype(_np.float64)
            dlat_r = (ob_lat[:, None] - apt_lat_c[None, :]) * nm_per_deg_lat
            dlon_r = (ob_lon[:, None] - apt_lon_c[None, :]) * nm_per_deg_lon
            min_d_nm = _np.sqrt(dlat_r * dlat_r + dlon_r * dlon_r).min(axis=1)
            inside_airport = min_d_nm <= _OBS_AIRPORT_RADIUS_NM

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

        # Label only tall (≥1000 AGL) or close (<1 nm) towers — text render
        # is ~1 ms each in pygame, the cache absorbs duplicates.
        if ob_agl[i] >= 1000 or dist_nm[i] < 1.0:
            lbl = f"{int(ob_msl[i])}"
            _obs_label_blit(surf, lbl, col, sx, sy - 14)


def draw_airport_symbols(surf, ai_rect, lat, lon, alt_ft,
                         hdg_deg, pitch_deg, roll_deg):
    """Project nearby airports onto the AI as symbol + ident label."""
    if _airports is None:
        return

    # Per-category filter
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

    nearby = apt_mod.query_nearby(_airports, lat, lon,
                                  radius_nm=AIRPORT_RADIUS_NM)
    if len(nearby) == 0:
        return

    ax, ay_r, aw, ah = ai_rect
    cx = ax + aw // 2
    cy = ay_r + ah // 2

    # Match draw_simple_ai_background / draw_pitch_ladder / above-horizon
    # silhouette so airport symbols sit at the same per-degree scale as the
    # horizon line they reference. Previously ah/48 ≈ 9.08 px/deg disagreed
    # with the horizon's hardcoded 10 px/deg — airports drifted relative
    # to the horizon by ~10% per degree of pitch.
    PX_PER_DEG = 10.0
    nm_per_deg_lat = 60.0
    nm_per_deg_lon = 60.0 * math.cos(math.radians(lat))
    cos_r = math.cos(math.radians(roll_deg))
    sin_r = math.sin(math.radians(roll_deg))
    max_rel_brg = (aw // 2) / PX_PER_DEG

    APT_PUBLIC  = (120, 220, 255)
    APT_HELI    = (220, 120, 220)
    APT_WATER   = (150, 200, 255)
    APT_OTHER   = (180, 180, 200)

    for apt in reversed(nearby):
        if not show.get(apt.atype, False):
            continue
        dlat_nm = (apt.lat - lat) * nm_per_deg_lat
        dlon_nm = (apt.lon - lon) * nm_per_deg_lon
        dist_nm = math.hypot(dlat_nm, dlon_nm)
        if dist_nm < 0.05:
            continue
        bearing = math.degrees(math.atan2(dlon_nm, dlat_nm)) % 360.0
        rel_brg = (bearing - hdg_deg + 180) % 360 - 180
        if abs(rel_brg) > max_rel_brg:
            continue
        dist_ft = dist_nm * 6076.0
        alt_diff_ft = apt.elev_ft - alt_ft
        vert_deg = math.degrees(math.atan2(alt_diff_ft, dist_ft))

        # Skip airports above the visual horizon — vert_deg > 0 means the
        # field elevation is above the aircraft (looking up at it), which
        # for normal cruise means the airport is sitting in the "sky"
        # region of the AI and would visually drift into terrain features
        # / pitch ladder territory. The old cull (screen_y_raw < 0) was
        # off-centre instead of off-horizon, so at nose-up pitch airports
        # with even slightly-positive vert_deg painted above the horizon.
        if vert_deg > 0:
            continue

        screen_x_raw = rel_brg * PX_PER_DEG
        screen_y_raw = (pitch_deg - vert_deg) * PX_PER_DEG
        sx = cx + int(screen_x_raw * cos_r - screen_y_raw * sin_r)
        sy = cy + int(screen_x_raw * sin_r + screen_y_raw * cos_r)

        if not (ax + 8 <= sx <= ax + aw - 8 and ay_r + 8 <= sy <= ay_r + ah - 8):
            continue

        if apt.atype == "H":
            col = APT_HELI
            _text(surf, "H", 12, col, bold=True, cx=sx, cy=sy)
        elif apt.atype == "W":
            col = APT_WATER
            pygame.draw.circle(surf, col, (sx, sy), 4, 1)
            pygame.draw.line(surf, col, (sx - 4, sy + 5), (sx + 4, sy + 5), 1)
        elif apt.atype == "B":
            col = APT_OTHER
            pts = [(sx, sy - 4), (sx - 4, sy + 3), (sx + 4, sy + 3)]
            pygame.draw.polygon(surf, col, pts, 1)
        else:
            col = APT_PUBLIC
            pygame.draw.circle(surf, col, (sx, sy), 5, 0)
            pygame.draw.circle(surf, (0, 10, 30), (sx, sy), 3, 0)
            if apt.atype in ("M", "L"):
                pygame.draw.circle(surf, col, (sx, sy), 7, 1)

        # Ident label as a "road sign" on a post above the symbol.
        # Doubled in size from the original (font 9 / post 22 / line 1px)
        # so the 4-letter ICAO ident is readable on the 640x480 panel
        # without leaning in.
        if dist_nm <= AIRPORT_LABEL_NM:
            lbl = apt.ident
            font_sz = 18
            f = _get_font(font_sz, bold=True)
            tw, th = f.size(lbl)
            sign_w = tw + 16
            sign_h = th + 8
            post_h = 44
            sign_x = sx - sign_w // 2
            sign_y = sy - post_h - sign_h
            if sign_y < ay_r + 2:
                sign_y = ay_r + 2
                post_h = max(8, sy - sign_y - sign_h)
            pygame.draw.line(surf, col, (sx, sy - 6), (sx, sign_y + sign_h), 2)
            pygame.draw.rect(surf, (0, 10, 26),
                             (sign_x, sign_y, sign_w, sign_h), border_radius=3)
            pygame.draw.rect(surf, col,
                             (sign_x, sign_y, sign_w, sign_h), width=2, border_radius=3)
            _text(surf, lbl, font_sz, col, bold=True,
                  cx=sx, cy=sign_y + sign_h // 2)


# ── MFD: full-screen moving map ──────────────────────────────────────────────
# Wraps shared/moving_map.py with the pi_zero state.  Uses the same airport
# + obstacle + SRTM + water caches the PFD already keeps loaded — no extra
# memory cost.  Runways are intentionally absent (pi_zero PFD dropped them
# earlier so we don't load the runway DB).

import moving_map as _mfd_map   # noqa: E402

# Match pi4 inset zoom range — 1, 2, 5, 10, 20, 40, 80, 160 nm plus AUTO.
# Past 40 nm the moving_map gates out tint, airports, and obstacles, so
# the only layers rendering at 80/160 are state lines + D2 line + own-
# ship — feather-light, no SRTM I/O, no surface alloc.  AUTO scales to
# the active direct-to waypoint (or stays at 80 with no D2).
MFD_MAX_ZOOM_NM = 160

_MFD_ZOOM_BTN  = 56     # square zoom-in / zoom-out buttons
_MFD_FPL_BTN_W = 100    # FPL keeps its width for the 3-char label
_MFD_FPL_BTN_H = _MFD_ZOOM_BTN   # height matches zoom buttons
_MFD_D2_BTN_W  = 100    # D2 keeps its width for "→ KSEZ"-style idents
_MFD_D2_BTN_H  = _MFD_ZOOM_BTN
_MFD_STRIP_H   = 46     # height of the bottom data strip
_MFD_STRIP_PAD = 6      # gap between strip and zoom buttons above it
_MFD_STRIP_SLOT_COUNT = 8

# Catalog of readouts the user can drop into the strip.  Order here is
# the order they appear in the chooser grid.
#   (kind_id, caption_default, needs_d2)
# When needs_d2 is True and no direct-to is active, the slot draws "--"
# in a dim colour rather than going blank.
_MFD_STRIP_AVAILABLE = (
    ("gs",   "GS",   False),
    ("as",   "AS",   False),   # indicated airspeed
    ("tas",  "TAS",  False),
    ("trk",  "TRK",  False),
    ("hdg",  "HDG",  False),
    ("alt",  "ALT",  False),
    ("agl",  "AGL",  False),
    ("vs",   "VS",   False),
    ("oat",  "OAT",  False),
    ("da",   "DA",   False),
    ("pa",   "PA",   False),
    ("wind", "WIND", False),
    ("time", "UTC",  False),
    ("baro", "BARO", False),
    ("sat",  "SAT",  False),
    ("wpt",  "WPT",  True),
    ("btw",  "BTW",  True),
    ("dtk",  "DTK",  True),
    ("dist", "DIST", True),    # to active waypoint (current leg)
    ("disw", "DISW", True),    # total distance through remaining FPL legs
    ("xte",  "XTE",  True),
    ("ete",  "ETE",  True),    # to active waypoint
    ("etew", "ETEW", True),    # enroute time through entire remaining plan
    ("eta",  "ETA",  True),    # clock at final destination
    ("etw",  "ETW",  True),    # clock at next waypoint
)
_MFD_STRIP_KIND_IDS = tuple(k[0] for k in _MFD_STRIP_AVAILABLE)
_MFD_STRIP_CAPTIONS = {k[0]: k[1] for k in _MFD_STRIP_AVAILABLE}
_MFD_STRIP_NEEDS_D2 = {k[0]: k[2] for k in _MFD_STRIP_AVAILABLE}
_MFD_STRIP_DEFAULT  = ["gs", "trk", "alt", "wpt", "btw", "dist", "ete", "eta"]
_D2_DIM = (110, 90, 110)   # dim magenta for D2-required slots when no D2


def _mfd_d2_rect():
    pad = 6
    return (pad, pad, _MFD_D2_BTN_W, _MFD_D2_BTN_H)


def _mfd_strip_rect():
    """Bottom data strip — flush with the bottom and side edges of the
    display so the dark backplate reads as a real status bar rather
    than a floating card."""
    return (0, DISPLAY_H - _MFD_STRIP_H,
            DISPLAY_W, _MFD_STRIP_H)


def _mfd_zoom_in_rect():
    """Zoom-in (+) button — right corner, just above the strip."""
    pad = 6
    _, sy, _, _ = _mfd_strip_rect()
    return (DISPLAY_W - _MFD_ZOOM_BTN - pad,
            sy - _MFD_STRIP_PAD - _MFD_ZOOM_BTN,
            _MFD_ZOOM_BTN, _MFD_ZOOM_BTN)


def _mfd_zoom_out_rect():
    """Zoom-out (-) button — left corner, just above the strip."""
    pad = 6
    _, sy, _, _ = _mfd_strip_rect()
    return (pad,
            sy - _MFD_STRIP_PAD - _MFD_ZOOM_BTN,
            _MFD_ZOOM_BTN, _MFD_ZOOM_BTN)


def _mfd_strip_hit(x, y):
    sx, sy, sw, sh = _mfd_strip_rect()
    return sx <= x <= sx + sw and sy <= y <= sy + sh


def _mfd_strip_kinds():
    """Return the user's selected 8 strip kinds, padded/trimmed and
    validated against the available-kinds set."""
    cur = list(disp["ds"].get("mfd_strip_kinds", _MFD_STRIP_DEFAULT))
    out = []
    for i in range(_MFD_STRIP_SLOT_COUNT):
        k = cur[i] if i < len(cur) else _MFD_STRIP_DEFAULT[i]
        if k not in _MFD_STRIP_KIND_IDS:
            k = _MFD_STRIP_DEFAULT[i]
        out.append(k)
    return out


def _mfd_strip_ete_str(gs_kt, dist_nm):
    """ETE remaining — M:SS for <1 h, H:MM up to 99 h, else dashes."""
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


def _mfd_strip_eta_str(gs_kt, dist_nm):
    """Zulu clock time of arrival, HH:MMZ."""
    if gs_kt < 3.0 or dist_nm <= 0.0:
        return "--:--"
    hours = dist_nm / gs_kt
    if hours >= 99.0:
        return "--:--"
    eta_t = time.time() + int(round(hours * 3600))
    return time.strftime("%H:%MZ", time.gmtime(eta_t))


def _mfd_strip_ctx(lat, lon, alt, hdg, track, gs_kt, d2):
    """Bundle live values a strip slot may need.  D2-dependent fields
    (dist_nm/brg/dtk/xte_nm) are only set when d2 is non-None."""
    ctx = {
        "lat":      lat,
        "lon":      lon,
        "alt":      alt,
        "hdg":      hdg,
        "track":    track,
        "gs_kt":    gs_kt,
        "ias":      float(disp.get("ias_kt", 0.0)),
        "tas":      float(disp.get("tas_kt", 0.0)),
        "vs":       float(disp.get("vspeed", 0.0)),
        "baro_hpa": float(disp.get("baro_hpa", BARO_DEFAULT_HPA)),
        "sats":     int(disp.get("sats", 0)),
        "d2":       d2,
    }
    # AGL: alt minus terrain elevation under the aircraft.  Use the
    # combined helper so the coarse Mapzen layer fills in areas the
    # high-res SRTM cache doesn't cover.  Both helpers return 0.0
    # when no tile is loaded (sea-level convention) — same fallback
    # the terrain-alert code uses, so an unknown cell shows
    # AGL == baro alt rather than dashing the slot.  _has_terrain is
    # checked at import time so this gate skips the lookup entirely
    # on a Pi without any cached terrain.
    if _has_terrain:
        ground_ft = get_elevation_ft_combined(
            SRTM_DIR, COARSE_DIR, lat, lon)
        ctx["agl"] = max(0.0, alt - ground_ft)
    if d2 is not None:
        dist_nm, brg = _nav_geo_dist_brg(lat, lon, d2["lat"], d2["lon"])
        ctx["dist_nm"] = dist_nm
        ctx["brg"]     = brg
        act_lat = float(d2.get("act_lat", lat))
        act_lon = float(d2.get("act_lon", lon))
        _, dtk = _nav_geo_dist_brg(act_lat, act_lon, d2["lat"], d2["lon"])
        ctx["dtk"]     = dtk
        ctx["xte_nm"]  = _nav_xtk_nm(act_lat, act_lon,
                                      d2["lat"], d2["lon"], lat, lon)
    return ctx


def _mfd_strip_format(kind, ctx):
    """Returns (caption, value_str, color) for a single strip slot.

    D2-required kinds when no direct-to is active return (caption, '--',
    dim) so the slot stays visible but obviously inactive."""
    d2    = ctx.get("d2")
    gs_kt = ctx["gs_kt"]

    # ── Non-D2 ───────────────────────────────────────────────────────────
    if kind == "gs":
        return ("GS", f"{int(round(gs_kt)):3d}", WHITE)
    if kind == "as":
        v = ctx["ias"]
        s = f"{int(round(v)):3d}" if v > 0 else "---"
        return ("AS", s, WHITE)
    if kind == "tas":
        v = ctx["tas"]
        s = f"{int(round(v)):3d}" if v > 0 else "---"
        return ("TAS", s, WHITE)
    if kind == "trk":
        track = ctx["track"]
        s = (f"{int(round(track)) % 360:03d}°"
             if gs_kt >= HDG_TRK_MIN_KT else "---°")
        return ("TRK", s, WHITE)
    if kind == "hdg":
        return ("HDG", f"{int(round(ctx['hdg'])) % 360:03d}°", WHITE)
    if kind == "alt":
        alt_q = int(round(ctx["alt"] / 20.0) * 20)
        return ("ALT", f"{alt_q:5d}", WHITE)
    if kind == "agl":
        v = ctx.get("agl")
        if v is None:
            return ("AGL", "----", (140, 140, 140))
        # 10 ft resolution — matches the Pi 4 PFD's AGL readout.
        agl_q = int(round(v / 10.0) * 10)
        return ("AGL", f"{agl_q:5d}", WHITE)
    if kind == "vs":
        return ("VS", f"{int(round(ctx['vs'])):+5d}", WHITE)
    if kind == "oat":
        return ("OAT", "--", (140, 140, 140))
    if kind == "da":
        return ("DA", "----", (140, 140, 140))
    if kind == "pa":
        return ("PA", "----", (140, 140, 140))
    if kind == "wind":
        return ("WIND", "---/--", (140, 140, 140))
    if kind == "time":
        return ("UTC", time.strftime("%H:%MZ", time.gmtime()), WHITE)
    if kind == "baro":
        hpa = ctx["baro_hpa"]
        unit = disp["ds"].get("baro_unit", "inhg")
        if unit == "inhg":
            return ("BARO", f"{hpa * 0.02953:.2f}", WHITE)
        return ("BARO", f"{int(round(hpa)):4d}", WHITE)
    if kind == "sat":
        return ("SAT", f"{ctx['sats']:2d}", WHITE)

    # ── D2-required ──────────────────────────────────────────────────────
    caption = _MFD_STRIP_CAPTIONS.get(kind, "?")
    if d2 is None:
        return (caption, "--", _D2_DIM)

    if kind == "wpt":
        return (caption, d2.get("ident", "----"), MAGENTA)
    if kind == "btw":
        return (caption,
                f"{int(round(ctx['brg'])) % 360:03d}°", MAGENTA)
    if kind == "dtk":
        return (caption,
                f"{int(round(ctx['dtk'])) % 360:03d}°", MAGENTA)
    if kind == "dist":
        d_nm = ctx["dist_nm"]
        s = (f"{int(round(d_nm)):d}" if d_nm >= 1000.0
             else f"{d_nm:.1f}")
        return (caption, s, MAGENTA)
    if kind == "disw":
        # Total distance through every remaining leg of the plan —
        # the "to final destination" companion to DIST's single-leg
        # number.  Falls back to DIST when no plan is active (just the
        # current direct-to).
        if _fpl_is_active():
            d_nm = _fpl_total_remaining_nm(ctx["lat"], ctx["lon"])
        else:
            d_nm = ctx["dist_nm"]
        s = (f"{int(round(d_nm)):d}" if d_nm >= 1000.0
             else f"{d_nm:.1f}")
        return (caption, s, MAGENTA)
    if kind == "xte":
        return (caption, f"{ctx['xte_nm']:+.1f}", MAGENTA)
    if kind == "ete":
        # Remaining time to the active leg's destination — single leg.
        return (caption,
                _mfd_strip_ete_str(gs_kt, ctx["dist_nm"]), MAGENTA)
    if kind == "etew":
        # Enroute time through the WHOLE remaining plan — companion
        # to ETE.  Falls back to ETE when no plan is active.
        if _fpl_is_active():
            total_nm = _fpl_total_remaining_nm(ctx["lat"], ctx["lon"])
        else:
            total_nm = ctx["dist_nm"]
        return (caption, _mfd_strip_ete_str(gs_kt, total_nm), MAGENTA)
    if kind == "etw":
        # Clock time at the NEXT waypoint (the active leg's dest).
        # When the plan has only one waypoint, equivalent to ETA.
        return (caption,
                _mfd_strip_eta_str(gs_kt, ctx["dist_nm"]), MAGENTA)
    if kind == "eta":
        # Clock time at the FINAL waypoint of the plan — walks
        # remaining legs.  Falls back to single-leg D2 behaviour when
        # no plan is active (just the current direct-to destination).
        if _fpl_is_active():
            total_nm = _fpl_total_remaining_nm(ctx["lat"], ctx["lon"])
            return (caption,
                    _mfd_strip_eta_str(gs_kt, total_nm), MAGENTA)
        return (caption,
                _mfd_strip_eta_str(gs_kt, ctx["dist_nm"]), MAGENTA)

    return (caption, "--", _D2_DIM)


# Vertical spacing below the top-row chrome buttons.  Earlier rev had the
# labels 4 px below — too close on the panel, hot fingers were landing on
# the PFD button instead of the ORIENT tap.  18 px gives a clear gap.
_MFD_LABEL_DROP = 18
_MFD_LABEL_H    = 40   # tap-target height; text rendered centred


def _mfd_rng_label_rect():
    """RNG readout: under the D2 button (top-left).  Passive label, but
    its rect claims the chrome strip so a tap there doesn't pan the map."""
    pad = 6
    return (pad,
            pad + _MFD_D2_BTN_H + _MFD_LABEL_DROP,
            _MFD_D2_BTN_W,
            _MFD_LABEL_H)


def _mfd_orient_label_rect():
    """ORIENT readout: under the PFD button (top-right).  Tappable to
    toggle TRK↑ / N↑."""
    pad = 6
    return (DISPLAY_W - _MFD_FPL_BTN_W - pad,
            pad + _MFD_FPL_BTN_H + _MFD_LABEL_DROP,
            _MFD_FPL_BTN_W,
            _MFD_LABEL_H)


def _mfd_center_btn_rect():
    """CTR (re-center) button — sits one full button-width below the
    orient label so it stays visually distinct from the orient toggle
    when the map is panned.  Both are visible at the same time."""
    pad = 6
    return (DISPLAY_W - _MFD_FPL_BTN_W - pad,
            pad + _MFD_FPL_BTN_H + _MFD_LABEL_DROP
                + _MFD_LABEL_H + _MFD_FPL_BTN_W,   # ← +100 px (button-width gap)
            _MFD_FPL_BTN_W,
            _MFD_FPL_BTN_H)


def _mfd_center_btn_hit(x, y):
    if not _mfd_is_panned():
        return False
    bx, by, bw, bh = _mfd_center_btn_rect()
    return bx <= x <= bx + bw and by <= y <= by + bh


def _mfd_orient_label_hit(x, y):
    """Tap-test on the ORIENT readout (TRK↑ / N↑) to toggle map rotation."""
    bx, by, bw, bh = _mfd_orient_label_rect()
    return bx <= x <= bx + bw and by <= y <= by + bh


def _mfd_get_range_label():
    """Default-range label is the numeric NM value; AUTO mode reserved
    for the future flight-plan-aware fit-to-route."""
    nm = disp["ds"].get("map_zoom_nm", 10)
    return f"{nm} NM" if nm > 0 else "AUTO"


_mfd_apt_font = None


def _mfd_get_apt_font():
    """Lazy-init a bold font for airport labels on the MFD.  Sized for
    a panel-mounted 480 px display read at arm's length — much larger
    than the pi4 inset's 11 px because the labels are the only ident
    cue at altitude."""
    global _mfd_apt_font
    if _mfd_apt_font is None:
        # 3x the pi4 inset size — labels are the primary ident cue on
        # the MFD and need to read at arm's length.
        _mfd_apt_font = pygame.font.SysFont("DejaVu Sans", 33, bold=True)
    return _mfd_apt_font


def _mfd_effective_center():
    """Return (lat, lon) for the map center: pan offset if active, else
    the aircraft position."""
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


def draw_mfd(surf, connected=True, data_stale=False):
    """Full-screen moving map.  Reuses pi_zero's already-loaded airport +
    obstacle + terrain caches; pulls the active direct-to from disp["nav"]
    so the magenta course line / waypoint diamond paints when D2 is set."""
    # Lazy-retry state-lines + country-lines cache load: if either .npz
    # didn't exist at startup but lands later (rsync from pi4, or
    # background build), pick it up without requiring a restart or mode
    # flip.  Capped at one disk-stat every 5 s when the cache is still
    # None.
    global _state_lines, _state_lines_last_try
    global _country_lines, _country_lines_last_try
    _now = time.monotonic()
    if _state_lines is None and _now - _state_lines_last_try > 5.0:
        _state_lines_last_try = _now
        _state_lines = _ne_load_cache(_SL_NPZ_NAME)
    if _country_lines is None and _now - _country_lines_last_try > 5.0:
        _country_lines_last_try = _now
        _country_lines = _ne_load_cache(_CL_NPZ_NAME)
    surf.fill((0, 0, 0))
    rect = (0, 0, DISPLAY_W, DISPLAY_H)
    ac_lat = disp.get("lat", DEMO_LAT)
    ac_lon = disp.get("lon", DEMO_LON)
    cen_lat, cen_lon = _mfd_effective_center()
    alt = disp.get("alt", 0.0)
    hdg = disp.get("yaw", 0.0)
    track = disp.get("track", hdg)
    orient    = disp["ds"].get("map_orient", "trk")
    range_nm  = int(disp["ds"].get("map_zoom_nm", 10))
    nav = disp.get("nav", {})
    d2  = None
    if nav.get("ident"):
        d2 = {"lat":     float(nav.get("lat", 0.0)),
              "lon":     float(nav.get("lon", 0.0)),
              "act_lat": float(nav.get("act_lat", 0.0)),
              "act_lon": float(nav.get("act_lon", 0.0)),
              "ident":   nav.get("ident", "")}
    gs_kt = float(disp.get("speed", 0.0))
    # Build the *set* of type letters the user wants visible — moving_map
    # checks `atype not in airport_types_visible`, so a dict here (which
    # would test keys regardless of True/False) silently disables the
    # whole filter.  Mirrors pi4's _types_vis pattern.
    _ad = disp["ad"]
    apt_types = set()
    if _ad.get("show_public", True):
        apt_types.update({"S", "M", "L"})
    if _ad.get("show_heli", True):
        apt_types.add("H")
    if _ad.get("show_seaplane", False):
        apt_types.add("W")
    if _ad.get("show_other", False):
        apt_types.add("B")
    _mfd_map.render(
        surf, rect, cen_lat, cen_lon, alt, hdg, track, orient, range_nm,
        disp["ds"],
        airports_arr=_airports,
        runways_arr=None,
        obstacles_arr=_obstacles,
        srtm_dir=SRTM_DIR,
        water_dir=WATER_DIR,
        direct_to=d2,
        airport_types_visible=apt_types,
        gs_kt=gs_kt,
        # Pass Vso (stall speed dirty) so the SVT-style clearance
        # overlay activates above stall — moving_map paints red where
        # terrain is at/above the aircraft, orange < 100 ft, amber
        # < 500 ft, same palette as the PFD's terrain alerts.
        vso_kt=disp["fp"].get("vs0", VS0),
        # Font enables airport labels (≤10 nm).  Corner labels (RNG / ETE)
        # are drawn by pi_zero's own chrome below, so suppress moving_map's
        # versions to avoid overlap with the D2 / PFD buttons + data strip.
        font=_mfd_get_apt_font(),
        draw_corner_labels=False,
        # State (admin_1) + country (admin_0) lines: cheap (bbox-culled
        # polyline blits) and only drawn at >= 20 nm where they're the
        # primary navigational context past the airport / terrain
        # coverage caps.
        state_lines=_state_lines,
        country_lines=_country_lines,
        # Aircraft position so the own-ship chevron stays at the real
        # GPS fix when the user has panned the map elsewhere.
        own_lat=ac_lat,
        own_lon=ac_lon,
        # Remaining FPL legs past the active waypoint — drawn as a
        # dimmer magenta polyline so the pilot sees the rest of the
        # plan after the current direct-to.  None when no plan is
        # active or the active leg is also the final.
        fpl_remaining=_fpl_render_remaining(),
        # Airspace polygons (Class B/C/D + MOA + Restricted).  Loaded
        # in the background at startup; per-class display gates live
        # in disp["ds"]["map_show_airspace_*"].
        airspaces=_airspaces,
    )
    lat, lon = ac_lat, ac_lon

    # ── Top labels (no plate) ─────────────────────────────────────────
    # RNG sits under the D2 button (left); ORIENT sits under the PFD
    # button (right).  Both are cyan-on-terrain (no plate), with ORIENT
    # tappable to toggle TRK↑ / N↑.  When the map is panned, a CTR
    # button appears below ORIENT (one button-width down) so both the
    # orient toggle and the re-center button stay visible.
    pad = 6
    rng_lbl = _mfd_get_range_label()
    orient_lbl = "TRK↑" if orient == "trk" else "N↑"
    rx, ry, rw, rh = _mfd_rng_label_rect()
    ox, oy, ow, oh = _mfd_orient_label_rect()
    _text(surf, rng_lbl, 20, CYAN, bold=True, cx=rx + rw // 2, cy=ry + rh // 2)
    _text(surf, orient_lbl, 20, CYAN, bold=True,
          cx=ox + ow // 2, cy=oy + oh // 2)
    if _mfd_is_panned():
        cx_, cy_, cw_, ch_ = _mfd_center_btn_rect()
        _action_btn(surf, cx_, cy_, cw_, ch_, "CTR", "ok", r=5)

    # ── Bottom data strip ─────────────────────────────────────────────
    # Full-width 8-slot strip.  Spans display-edge to display-edge so
    # the dark backplate reads as a status bar rather than a floating
    # card; only the top edge gets a hairline since the bottom and
    # sides are flush with the bezel.
    sx, sy, sw, sh = _mfd_strip_rect()
    plate = pygame.Surface((sw, sh), pygame.SRCALPHA)
    plate.fill((0, 8, 22, 180))
    surf.blit(plate, (sx, sy))
    pygame.draw.line(surf, (60, 80, 110),
                     (sx, sy), (sx + sw - 1, sy), 1)

    ctx = _mfd_strip_ctx(lat, lon, alt, hdg, track, gs_kt, d2)
    n_cols = _MFD_STRIP_SLOT_COUNT
    col_w  = sw // n_cols
    for i, kind in enumerate(_mfd_strip_kinds()):
        cap, val, col = _mfd_strip_format(kind, ctx)
        cx = sx + col_w // 2 + col_w * i
        _text(surf, cap, 11, (140, 170, 200), bold=True,
              cx=cx, y=sy + 4)
        _text(surf, val, 22, col, bold=True,
              cx=cx, cy=sy + 30)

    # ── MFD chrome buttons ─────────────────────────────────────────────
    # D2 (top-left), FPL (top-right) opens the flight-plan editor.
    # PFD ↔ MFD swap is the 3-finger 2-s hold (MFD_SWAP_HOLD_MS).
    _action_btn(surf, DISPLAY_W - _MFD_FPL_BTN_W - pad, pad,
                _MFD_FPL_BTN_W, _MFD_FPL_BTN_H, "FPL", "normal", r=5)
    # Top-left D2 button — magenta-styled when D2 is active.  Label
    # is "D→" when inactive, "D→ <ident>" when a direct-to is set
    # (arrow-after-D reads as the standard "direct to" affordance).
    d2_style = "warn" if d2 is not None else "normal"
    d2_label = f"D→ {d2['ident']}" if d2 else "D→"
    _action_btn(surf, pad, pad, _MFD_D2_BTN_W, _MFD_D2_BTN_H,
                d2_label, d2_style, r=5)
    # Zoom buttons (bottom corners)
    zo_x, zo_y, zo_w, zo_h = _mfd_zoom_out_rect()
    zi_x, zi_y, zi_w, zi_h = _mfd_zoom_in_rect()
    _action_btn(surf, zo_x, zo_y, zo_w, zo_h, "−", "normal", r=8)
    _action_btn(surf, zi_x, zi_y, zi_w, zi_h, "+", "normal", r=8)

    # No-link / stale-data badges
    if not connected or data_stale:
        _text(surf, "NO LINK" if not connected else "DATA STALE",
              16, (240, 90, 90), bold=True,
              cx=DISPLAY_W // 2, cy=DISPLAY_H - 18)


def _mfd_fpl_btn_hit(x, y):
    """Top-right FPL button on the MFD — opens the flight-plan editor."""
    pad = 6
    bx = DISPLAY_W - _MFD_FPL_BTN_W - pad
    by = pad
    return (bx <= x <= bx + _MFD_FPL_BTN_W and
            by <= y <= by + _MFD_FPL_BTN_H)


def _mfd_d2_btn_hit(x, y):
    bx, by, bw, bh = _mfd_d2_rect()
    return bx <= x <= bx + bw and by <= y <= by + bh


def _mfd_zoom_in_hit(x, y):
    bx, by, bw, bh = _mfd_zoom_in_rect()
    return bx <= x <= bx + bw and by <= y <= by + bh


def _mfd_zoom_out_hit(x, y):
    bx, by, bw, bh = _mfd_zoom_out_rect()
    return bx <= x <= bx + bw and by <= y <= by + bh


# ── MFD strip-setup chooser overlay ──────────────────────────────────────
# Layout: top row of 8 slot pills showing the current kind for each
# strip column (currently-selected slot has a cyan border).  Below, a
# grid of all available kinds — tap any to assign it to the selected
# slot, which then auto-advances to the next slot for fast configuring.

_MSS_HEADER_H   = 44
_MSS_SLOT_H     = 56
_MSS_SLOT_GAP   = 4
_MSS_GRID_GAP   = 6
_MSS_GRID_COLS  = 5


def _mss_slot_rects():
    pad = 6
    n = _MFD_STRIP_SLOT_COUNT
    avail_w = DISPLAY_W - 2 * pad
    pw = (avail_w - (n - 1) * _MSS_SLOT_GAP) // n
    y = _MSS_HEADER_H + 8
    rects = []
    for i in range(n):
        rects.append((pad + i * (pw + _MSS_SLOT_GAP), y, pw, _MSS_SLOT_H))
    return rects


def _mss_grid_rects():
    pad = 6
    n = len(_MFD_STRIP_AVAILABLE)
    cols = _MSS_GRID_COLS
    rows = (n + cols - 1) // cols
    avail_w = DISPLAY_W - 2 * pad
    cell_w = (avail_w - (cols - 1) * _MSS_GRID_GAP) // cols
    cell_h = 56
    y0 = _MSS_HEADER_H + 8 + _MSS_SLOT_H + 16
    rects = []
    for i in range(n):
        r = i // cols
        c = i % cols
        rects.append((pad + c * (cell_w + _MSS_GRID_GAP),
                      y0 + r * (cell_h + _MSS_GRID_GAP),
                      cell_w, cell_h))
    return rects


def draw_mfd_strip_setup(surf):
    """Chooser overlay reached by tapping the MFD bottom strip."""
    _screen_header(surf, "MFD STRIP")

    sel = int(disp.get("mss_sel", 0))
    sel = max(0, min(_MFD_STRIP_SLOT_COUNT - 1, sel))
    kinds = _mfd_strip_kinds()

    # ── Top: 8 slot pills, current kind shown ────────────────────────────
    for i, (rect, kind) in enumerate(zip(_mss_slot_rects(), kinds)):
        bx, by, bw, bh = rect
        is_sel = (i == sel)
        bg = (0, 40, 60) if is_sel else (0, 12, 32)
        oc = CYAN        if is_sel else (60, 80, 110)
        pygame.draw.rect(surf, bg, rect, border_radius=5)
        pygame.draw.rect(surf, oc, rect, width=2 if is_sel else 1,
                         border_radius=5)
        cap = _MFD_STRIP_CAPTIONS.get(kind, "?")
        _text(surf, f"{i+1}", 10, (140, 150, 170), bold=True,
              cx=bx + bw // 2, y=by + 4)
        _text(surf, cap, 18, CYAN if is_sel else WHITE, bold=True,
              cx=bx + bw // 2, cy=by + bh // 2 + 4)

    # Hint under the slot row
    hint_y = _MSS_HEADER_H + 8 + _MSS_SLOT_H + 1
    _text(surf, "tap a slot, then tap a readout below — auto-advances",
          10, (140, 150, 170), cx=DISPLAY_W // 2, y=hint_y)

    # ── Grid of available kinds ──────────────────────────────────────────
    for (kind, cap, needs_d2), rect in zip(_MFD_STRIP_AVAILABLE,
                                            _mss_grid_rects()):
        bx, by, bw, bh = rect
        in_use_here = (kinds[sel] == kind)
        bg = (0, 55, 65) if in_use_here else (0, 18, 38)
        oc = CYAN        if in_use_here else (60, 80, 110)
        pygame.draw.rect(surf, bg, rect, border_radius=4)
        pygame.draw.rect(surf, oc, rect, width=1, border_radius=4)
        tc = CYAN if in_use_here else (WHITE if not needs_d2 else MAGENTA)
        _text(surf, cap, 18, tc, bold=True,
              cx=bx + bw // 2, cy=by + bh // 2 - 6)
        if needs_d2:
            _text(surf, "needs D2", 9, (140, 100, 130),
                  cx=bx + bw // 2, y=by + bh - 14)


def mfd_strip_setup_hit(x, y):
    if 8 <= x <= 80 and 6 <= y <= 37:
        return ("back", None)
    # Slot pills
    for i, rect in enumerate(_mss_slot_rects()):
        bx, by, bw, bh = rect
        if bx <= x <= bx + bw and by <= y <= by + bh:
            return ("slot", i)
    # Kind grid
    for (kind, _cap, _nd2), rect in zip(_MFD_STRIP_AVAILABLE,
                                         _mss_grid_rects()):
        bx, by, bw, bh = rect
        if bx <= x <= bx + bw and by <= y <= by + bh:
            return ("kind", kind)
    return (None, None)


# ── FPL (flight-plan editor) ────────────────────────────────────────────
# Top-right "FPL" button on the MFD opens this.  Ordered list of
# waypoints; tap a row to activate that leg (writes disp["nav"]).
# Reorder with ↑ / ↓; delete with ✕.  Auto-sequences to the next
# waypoint when within _FPL_ADVANCE_DIST_NM of the active one.

_FPL_HEADER_H   = 44
_FPL_ACTIONS_H  = 50
# Row and icon sizes sized for cockpit fingers — was 46/32 which read
# too small in real lighting.  At ~80 px row + 56 px icons the list
# matches the rest of the MFD chrome (zoom buttons, D→ / FPL buttons
# are all 56 px).  Three full rows fit per screen; the list scrolls
# by touch-drag for >3 waypoints.
_FPL_ROW_H      = 80
_FPL_ROW_GAP    = 6

# Per-row action icon button width, right-justified.
_FPL_ICON_W     = 56
_FPL_ICON_GAP   = 6

# Vertical-drag scroll offset for the FPL list.  Updated by the
# touch handler while a finger is dragging inside the list area.
_fpl_scroll = 0


# Two action rows: row 1 has three "+" buttons (ICAO / LAT-LON / USER),
# row 2 has DEACTIVATE.  Stacking lets each + button stay finger-sized
# on the 480-px-wide screen instead of being squashed into thirds.
_FPL_ACTIONS_GAP = 6
_FPL_DEACT_H     = 36


def _fpl_actions_rect():
    pad = 6
    return (pad, _FPL_HEADER_H + 6,
            DISPLAY_W - 2 * pad, _FPL_ACTIONS_H)


def _fpl_add_buttons():
    """Return three rects for + ICAO / + LAT/LON / + USER.  +HERE was
    consolidated into +LAT/LON, which now pre-fills current aircraft
    lat/lon so the same path also handles 'mark a point here'."""
    ax, ay, aw, ah = _fpl_actions_rect()
    n = 3
    gap = _FPL_ACTIONS_GAP
    bw = (aw - (n - 1) * gap) // n
    return [(ax + i * (bw + gap), ay, bw, ah) for i in range(n)]


def _fpl_deact_btn_rect():
    ax, ay, aw, _ = _fpl_actions_rect()
    by = ay + _FPL_ACTIONS_H + _FPL_ACTIONS_GAP
    return (ax, by, aw, _FPL_DEACT_H)


def _fpl_list_y0():
    return (_FPL_HEADER_H + 6 + _FPL_ACTIONS_H + _FPL_ACTIONS_GAP
            + _FPL_DEACT_H + 8)


def _fpl_row_rect(idx):
    y0 = _fpl_list_y0() - _fpl_scroll
    pad = 6
    return (pad, y0 + idx * (_FPL_ROW_H + _FPL_ROW_GAP),
            DISPLAY_W - 2 * pad, _FPL_ROW_H)


def _fpl_max_scroll(n_rows):
    """Maximum scroll offset given n_rows in the list.  Zero when the
    list fits entirely in the visible area."""
    visible_h = DISPLAY_H - _fpl_list_y0() - 6
    content_h = n_rows * (_FPL_ROW_H + _FPL_ROW_GAP) - _FPL_ROW_GAP
    return max(0, content_h - visible_h)


def _fpl_list_area_y():
    """(top, bottom) of the scrollable list area — used for hit-test
    and clip rect."""
    return _fpl_list_y0(), DISPLAY_H - 6


def _fpl_row_icon_rects(rect):
    """Return (up_rect, down_rect, del_rect) inside a row rect, right-
    justified.  Tappable to reorder / delete."""
    bx, by, bw, bh = rect
    iy = by + (bh - _FPL_ICON_W) // 2
    ih = _FPL_ICON_W
    del_x  = bx + bw - 6 - _FPL_ICON_W
    down_x = del_x  - _FPL_ICON_GAP - _FPL_ICON_W
    up_x   = down_x - _FPL_ICON_GAP - _FPL_ICON_W
    return ((up_x,   iy, _FPL_ICON_W, ih),
            (down_x, iy, _FPL_ICON_W, ih),
            (del_x,  iy, _FPL_ICON_W, ih))


def _fpl_icon_btn(surf, rect, glyph, dim=False):
    bx, by, bw, bh = rect
    bg = (8, 18, 32) if not dim else (4, 8, 14)
    oc = (80, 100, 130) if not dim else (40, 50, 70)
    tc = WHITE if not dim else (90, 100, 120)
    pygame.draw.rect(surf, bg, rect, border_radius=5)
    pygame.draw.rect(surf, oc, rect, width=1, border_radius=5)
    _text(surf, glyph, 34, tc, bold=True,
          cx=bx + bw // 2, cy=by + bh // 2 + 1)


def draw_fpl(surf):
    _screen_header(surf, "FLIGHT PLAN")

    wps = disp.get("fpl", {}).get("waypoints", [])
    active_idx = disp.get("fpl", {}).get("active_idx", -1)
    is_active  = 0 <= active_idx < len(wps)

    # ── Action row 1: + ICAO / + LAT/LON / + USER ───────────────────────
    full = len(wps) >= _FPL_MAX_WAYPOINTS
    add_style = "ok" if not full else "normal"
    add_rects = _fpl_add_buttons()
    # USER is always tappable even when FPL is full so the pilot can
    # still browse / delete library entries; the picker enforces the
    # cap on add.
    labels  = ("+ ICAO", "+ LAT/LON", "+ USER")
    styles  = (
        add_style if not full else "normal",
        add_style if not full else "normal",
        "ok",
    )
    if full:
        labels = ("FULL", "FULL", "+ USER")
    for (ax, ay, aw, ah), lbl, st in zip(add_rects, labels, styles):
        _action_btn(surf, ax, ay, aw, ah, lbl, st, r=6)

    # ── Action row 2: DEACTIVATE ──────────────────────────────────────
    dx, dy, dw, dh = _fpl_deact_btn_rect()
    if is_active:
        _action_btn(surf, dx, dy, dw, dh, "DEACTIVATE", "warn", r=6)
    else:
        pygame.draw.rect(surf, (10, 14, 22), (dx, dy, dw, dh),
                         border_radius=6)
        pygame.draw.rect(surf, (40, 48, 62), (dx, dy, dw, dh),
                         width=1, border_radius=6)
        _text(surf, "DEACTIVATE", 14, (80, 90, 110), bold=True,
              cx=dx + dw // 2, cy=dy + dh // 2)

    # ── Waypoint list ────────────────────────────────────────────────────
    if not wps:
        _text(surf, "No waypoints yet — tap + ADD WPT to start",
              14, (140, 160, 190), cx=DISPLAY_W // 2,
              cy=_fpl_list_y0() + 60)
        _text(surf, "Each ICAO ident becomes a leg.  Tap a row to",
              11, (110, 130, 160), cx=DISPLAY_W // 2,
              cy=_fpl_list_y0() + 90)
        _text(surf, "activate that leg as the direct-to.",
              11, (110, 130, 160), cx=DISPLAY_W // 2,
              cy=_fpl_list_y0() + 106)
        return

    # Clip row rendering to the list area so scrolled-up rows don't
    # bleed into the action buttons above.  Also clamp the scroll so
    # blank space never appears below the last row.
    global _fpl_scroll
    list_top, list_bot = _fpl_list_area_y()
    _fpl_scroll = max(0, min(_fpl_scroll, _fpl_max_scroll(len(wps))))
    prev_clip = surf.get_clip()
    surf.set_clip(pygame.Rect(0, list_top, DISPLAY_W, list_bot - list_top))

    for i, wp in enumerate(wps):
        bx, by, bw, bh = _fpl_row_rect(i)
        # Skip rows entirely above or below the visible window — at
        # 80 px row height + scroll, most of the list is off-screen
        # except a few rows.
        if by + bh < list_top or by > list_bot:
            continue
        is_this_active = (i == active_idx)
        bg = (0, 40, 18) if is_this_active else (0, 12, 32)
        oc = (60, 200, 90) if is_this_active else (60, 80, 110)
        pygame.draw.rect(surf, bg, (bx, by, bw, bh), border_radius=5)
        pygame.draw.rect(surf, oc, (bx, by, bw, bh),
                         width=2 if is_this_active else 1, border_radius=5)
        # Row number (full-row centred vertically)
        _text(surf, f"{i+1}", 20, (160, 180, 200), bold=True,
              x=bx + 14, cy=by + bh // 2)
        # Ident — top half so the subtitle can sit underneath
        ident_col = MAGENTA if is_this_active else WHITE
        _text(surf, wp.get("ident", ""), 32, ident_col, bold=True,
              x=bx + 56, y=by + 10)
        # Subtitle — airport name + region, or USER · lat/lon
        wp_name   = str(wp.get("name", "") or "")
        wp_region = str(wp.get("region", "") or "")
        wp_user   = bool(wp.get("user"))
        if wp_user:
            sub = f"USER  ·  {wp['lat']:.4f}, {wp['lon']:.4f}"
            sub_col = (200, 180, 130)
        elif wp_name:
            if wp_region:
                sub = f"{wp_name}, {wp_region}"
            else:
                sub = wp_name
            sub_col = (150, 175, 205)
        else:
            # Airport with no cached name (old DB) — show lat/lon as
            # a graceful fallback so the row stays informative.
            sub = f"{wp['lat']:.4f}, {wp['lon']:.4f}"
            sub_col = (130, 150, 180)
        # Crop subtitle to fit the available width before the icons.
        max_sub_x = bx + bw - 6 - 3 * _FPL_ICON_W - 2 * _FPL_ICON_GAP - 14
        sub_font = _get_font(18, bold=False)
        while sub and sub_font.size(sub)[0] > (max_sub_x - (bx + 56)):
            sub = sub[:-1]
        _text(surf, sub, 18, sub_col, x=bx + 56, y=by + bh - 26)
        # ACTIVE badge — top-right of the ident line
        if is_this_active:
            _text(surf, "● ACTIVE", 15, (60, 220, 100), bold=True,
                  x=bx + bw - 3 * _FPL_ICON_W - 2 * _FPL_ICON_GAP - 108,
                  y=by + 12)
        # Reorder / delete icons
        up_r, dn_r, del_r = _fpl_row_icon_rects((bx, by, bw, bh))
        _fpl_icon_btn(surf, up_r, "↑", dim=(i == 0))
        _fpl_icon_btn(surf, dn_r, "↓", dim=(i == len(wps) - 1))
        _fpl_icon_btn(surf, del_r, "✕")

    # Lift the clip and paint a scroll indicator on the right edge
    # when the list overflows.
    surf.set_clip(prev_clip)
    max_s = _fpl_max_scroll(len(wps))
    if max_s > 0:
        bar_w = 4
        bar_x = DISPLAY_W - bar_w - 2
        track_h = list_bot - list_top
        thumb_h = max(20, int(track_h * track_h
                              / (track_h + max_s)))
        thumb_y = list_top + int((track_h - thumb_h)
                                  * _fpl_scroll / max_s)
        pygame.draw.rect(surf, (40, 50, 70),
                         (bar_x, list_top, bar_w, track_h),
                         border_radius=2)
        pygame.draw.rect(surf, (120, 150, 190),
                         (bar_x, thumb_y, bar_w, thumb_h),
                         border_radius=2)


def fpl_hit(x, y):
    """Hit-test the FPL screen.  Returns one of:
        ("back",      None)
        ("add_icao",  None)
        ("add_ll",    None)
        ("add_lib",   None)
        ("deact",     None)
        ("activate",  idx)
        ("up",        idx)
        ("down",      idx)
        ("delete",    idx)
        (None,        None)
    """
    if 8 <= x <= 80 and 6 <= y <= 37:
        return ("back", None)
    for rect, kind in zip(_fpl_add_buttons(),
                           ("add_icao", "add_ll", "add_lib")):
        ax, ay, aw, ah = rect
        if ax <= x <= ax + aw and ay <= y <= ay + ah:
            return (kind, None)
    dx, dy, dw, dh = _fpl_deact_btn_rect()
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
        # Body of the row → activate
        return ("activate", i)
    return (None, None)


def _fpl_open_add_keyboard():
    """Open the existing ident keyboard, but route ENTER to FPL append
    instead of the D2 nav_confirm modal."""
    disp["kbd_target"] = "fpl_ident"
    disp["kbd_prev"]   = "fpl"
    disp["kbd_buf"]    = ""
    disp["kbd_error"]  = ""
    disp["kbd_shift"]  = False
    disp["mode"]       = "keyboard"


# ── +LAT/LON entry screen ───────────────────────────────────────────────
# Three tappable rows (IDENT / LAT / LON) collect a user waypoint by
# decimal degrees.  Each field hands off to the existing keyboard with
# a target that returns here on ENTER or CANCEL so the entry screen
# stays the focal point until SAVE.

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
    return ((pad, fy, half, _FLE_FOOTER_H),                       # CANCEL
            (pad + half + _FPL_ACTIONS_GAP, fy,
             half, _FLE_FOOTER_H))                                # SAVE


def _fle_open_kbd(target_axis):
    """Hand off to the keyboard for one of the three fields.  The
    keyboard's ENTER stores the typed value back into disp["fpl_new"]
    and returns to fpl_latlon_entry."""
    n = disp["fpl_new"]
    if target_axis == "ident":
        disp["kbd_target"] = "fpl_latlon_ident"
        disp["kbd_buf"]    = n.get("ident", "")
    elif target_axis == "lat":
        disp["kbd_target"] = "fpl_latlon_lat"
        disp["kbd_buf"]    = n.get("lat_str", "")
    elif target_axis == "lon":
        disp["kbd_target"] = "fpl_latlon_lon"
        disp["kbd_buf"]    = n.get("lon_str", "")
    else:
        return
    disp["kbd_prev"]  = "fpl_latlon_entry"
    disp["kbd_error"] = ""
    disp["kbd_shift"] = False
    disp["mode"]      = "keyboard"


def draw_fpl_latlon_entry(surf):
    _screen_header(surf, "ADD USER WAYPOINT")
    n = disp["fpl_new"]

    fields = [
        ("IDENT", "ident",   n.get("ident", ""),
         "name (e.g. FISH, RDV1)"),
        ("LAT",   "lat_str", n.get("lat_str", ""),
         "decimal degrees, e.g. 34.523 or -34.523"),
        ("LON",   "lon_str", n.get("lon_str", ""),
         "decimal degrees, e.g. -111.789"),
    ]
    err_field, err_msg = disp.get("fle_err_field", ""), disp.get("fle_err_msg", "")

    for i, (label, key, val, hint) in enumerate(fields):
        bx, by, bw, bh = _fle_field_rect(i)
        is_err = (err_field and (
            (err_field == "ident" and key == "ident")
            or (err_field == "lat" and key == "lat_str")
            or (err_field == "lon" and key == "lon_str")
        ))
        bg = (28, 14, 14) if is_err else (0, 12, 32)
        oc = (200, 80, 80) if is_err else (60, 80, 110)
        pygame.draw.rect(surf, bg, (bx, by, bw, bh), border_radius=6)
        pygame.draw.rect(surf, oc, (bx, by, bw, bh), width=1,
                         border_radius=6)
        _text(surf, label, 12, (160, 180, 210), bold=True,
              x=bx + 14, y=by + 8)
        # Value or placeholder
        if val:
            _text(surf, val, 22, WHITE, bold=True,
                  x=bx + 14, cy=by + bh // 2 + 8)
        else:
            _text(surf, hint, 12, (100, 110, 130),
                  x=bx + 14, cy=by + bh // 2 + 8)
        # Right-edge "tap to edit" affordance
        _text(surf, "edit ›", 11, (130, 150, 180),
              x=bx + bw - 60, cy=by + bh // 2)

    if err_msg:
        _text(surf, err_msg, 13, (240, 120, 120), bold=True,
              cx=DISPLAY_W // 2,
              y=DISPLAY_H - _FLE_FOOTER_H - 28)

    # CANCEL | SAVE
    (cx_, cy_, cw_, ch_), (sx_, sy_, sw_, sh_) = _fle_footer_rects()
    _action_btn(surf, cx_, cy_, cw_, ch_, "CANCEL", "normal", r=8)
    _action_btn(surf, sx_, sy_, sw_, sh_, "SAVE", "ok", r=8)


def fpl_latlon_entry_hit(x, y):
    if 8 <= x <= 80 and 6 <= y <= 37:
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
    """Return ('', '') if `ident` is safe to use as a new user waypoint
    name, otherwise ('field', 'message').  Two collision checks:

      1. Already in the current FPL — would create two rows with the
         same ident and break the polyline / D2 readouts.
      2. Already an airport ident in the database — using a real ICAO
         code as a user-waypoint name would confuse routing and a
         later + ICAO entry of the same code.

    Airport waypoints (+ ICAO path) are intentionally allowed to repeat
    in the plan (out-and-back through the same field is valid)."""
    ident = (ident or "").strip().upper()
    if not ident:
        return ("ident", "ident is required")
    for i, wp in enumerate(disp["fpl"]["waypoints"]):
        if str(wp.get("ident", "")).upper() == ident:
            return ("ident", f"'{ident}' already in plan (row {i+1})")
    if _nav_lookup_ident(ident) is not None:
        return ("ident", f"'{ident}' is an airport ident — pick another")
    return ("", "")


# ── User-waypoint picker (+ LIB) ────────────────────────────────────────
# Scrollable list of saved user waypoints with ADD / DEL actions per row.
# Reached from the FPL screen's + LIB button.  Tap ADD to drop the
# waypoint into the current flight plan (same collision rules as
# +LAT/LON entry); tap DEL to remove from the library.

_UWP_HEADER_H  = 44
_UWP_ROW_H     = 50
_UWP_ROW_GAP   = 4
_UWP_ICON_W    = 56     # ADD / DEL action buttons


def _uwp_row_rect(idx):
    pad = 6
    y0 = _UWP_HEADER_H + 8
    return (pad, y0 + idx * (_UWP_ROW_H + _UWP_ROW_GAP),
            DISPLAY_W - 2 * pad, _UWP_ROW_H)


def _uwp_row_btn_rects(rect):
    """Return (add_rect, del_rect) inside a row."""
    bx, by, bw, bh = rect
    iy = by + (bh - _UWP_ICON_W) // 2 + 4
    ih = _UWP_ICON_W - 8
    del_x = bx + bw - 6 - _UWP_ICON_W
    add_x = del_x - 4 - _UWP_ICON_W
    return ((add_x, iy, _UWP_ICON_W, ih),
            (del_x, iy, _UWP_ICON_W, ih))


def draw_user_wpt_picker(surf):
    _screen_header(surf, "USER WAYPOINTS")  # same screen as before; button is +USER
    wps = disp.get("user_wpts", {}).get("list", [])
    if not wps:
        _text(surf, "No saved user waypoints yet.", 14, (160, 180, 210),
              cx=DISPLAY_W // 2, cy=120)
        _text(surf, "Waypoints created via + LAT/LON in the",
              11, (130, 150, 180), cx=DISPLAY_W // 2, cy=160)
        _text(surf, "flight plan are auto-saved here for re-use.",
              11, (130, 150, 180), cx=DISPLAY_W // 2, cy=180)
        return

    # Sort by ident for stable browsing — order of creation isn't
    # meaningful once the library has several entries.
    sorted_wps = sorted(wps, key=lambda w: str(w.get("ident", "")))
    fpl_idents = {str(w.get("ident", "")).upper()
                  for w in disp.get("fpl", {}).get("waypoints", [])}
    plan_full = (len(disp.get("fpl", {}).get("waypoints", []))
                 >= _FPL_MAX_WAYPOINTS)

    for i, wp in enumerate(sorted_wps):
        bx, by, bw, bh = _uwp_row_rect(i)
        if by + bh > DISPLAY_H - 6:
            break
        in_fpl = str(wp.get("ident", "")).upper() in fpl_idents
        bg = (0, 12, 32) if not in_fpl else (10, 26, 16)
        oc = (60, 80, 110) if not in_fpl else (90, 160, 110)
        pygame.draw.rect(surf, bg, (bx, by, bw, bh), border_radius=5)
        pygame.draw.rect(surf, oc, (bx, by, bw, bh), width=1, border_radius=5)
        # Ident (left)
        _text(surf, str(wp.get("ident", "")), 22, MAGENTA, bold=True,
              x=bx + 14, y=by + 6)
        # Lat/lon (subtitle)
        sub = f"{float(wp.get('lat', 0)):.4f}, {float(wp.get('lon', 0)):.4f}"
        _text(surf, sub, 12, (160, 180, 210), x=bx + 14, y=by + bh - 18)
        # "IN PLAN" indicator if already in current FPL
        if in_fpl:
            _text(surf, "● in plan", 11, (90, 200, 130), bold=True,
                  x=bx + 200, cy=by + bh // 2)
        # ADD / DEL action buttons
        add_r, del_r = _uwp_row_btn_rects((bx, by, bw, bh))
        # ADD disabled (greyed) when already in FPL or plan full.
        if in_fpl or plan_full:
            pygame.draw.rect(surf, (10, 14, 22), add_r, border_radius=4)
            pygame.draw.rect(surf, (40, 50, 64), add_r, width=1,
                             border_radius=4)
            _text(surf, "ADD", 13, (80, 90, 110), bold=True,
                  cx=add_r[0] + add_r[2] // 2,
                  cy=add_r[1] + add_r[3] // 2)
        else:
            _action_btn(surf, *add_r, "ADD", "ok", r=4)
        _action_btn(surf, *del_r, "DEL", "danger", r=4)


def user_wpt_picker_hit(x, y):
    if 8 <= x <= 80 and 6 <= y <= 37:
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



def _fpl_open_latlon_entry():
    """+ LAT/LON: open the multi-field entry screen with the current
    aircraft position pre-filled so the same path also handles the
    "create a waypoint HERE" case — pilot just types an ident and
    taps SAVE without touching the lat/lon fields.  Edit lat/lon when
    you want a different position."""
    cur_lat = float(disp.get("lat", 0.0))
    cur_lon = float(disp.get("lon", 0.0))
    disp["fpl_new"]["ident"]   = ""
    disp["fpl_new"]["lat"]     = cur_lat
    disp["fpl_new"]["lon"]     = cur_lon
    # Pre-fill the string buffers so the entry-screen rows show the
    # current position right away.  5 decimals ≈ 1 m precision, which
    # is finer than GPS noise so we're not throwing away resolution.
    disp["fpl_new"]["lat_str"] = f"{cur_lat:.5f}"
    disp["fpl_new"]["lon_str"] = f"{cur_lon:.5f}"
    disp["fpl_new"]["source"]  = "latlon"
    disp["mode"]               = "fpl_latlon_entry"


def _fpl_parse_latlon(s, axis):
    """Parse a decimal-degree string into a float.  Returns
    (value, error_msg).  axis is 'lat' or 'lon' for range-checking."""
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
    """Validate and append a +LAT/LON waypoint.  Returns ('', '') on
    success, ('field', 'msg') on validation failure."""
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
    # Auto-save to library so this entry is recallable later.
    _user_wpt_save(ident, lat, lon)
    n["ident"] = ""; n["lat"] = 0.0; n["lon"] = 0.0
    n["lat_str"] = ""; n["lon_str"] = ""; n["source"] = ""
    return ("", "")


def _mfd_open_d2_keyboard():
    """Open the existing keyboard with kbd_target == 'nav_ident' so the
    pilot can type an ICAO ident.  ENTER routes through the nav_confirm
    modal."""
    disp["kbd_target"] = "nav_ident"
    disp["kbd_prev"]   = "pfd"          # MFD runs under mode == "pfd"
    disp["kbd_buf"]    = ""
    disp["kbd_error"]  = ""
    disp["kbd_shift"]  = False
    disp["mode"]       = "keyboard"


def _mfd_chrome_hit(x, y):
    """True if (x, y) is over any MFD chrome button or the data strip —
    i.e. the user can't pan / tap-airport here because some other widget
    owns the tap."""
    if _mfd_d2_btn_hit(x, y):       return True
    if _mfd_fpl_btn_hit(x, y):      return True
    if _mfd_zoom_in_hit(x, y):      return True
    if _mfd_zoom_out_hit(x, y):     return True
    if _mfd_center_btn_hit(x, y):   return True
    if _mfd_orient_label_hit(x, y): return True
    # RNG label (under D2) is a passive readout but still claims its rect
    # so a hot finger doesn't accidentally pan the map.
    rx, ry, rw, rh = _mfd_rng_label_rect()
    if rx <= x <= rx + rw and ry <= y <= ry + rh:
        return True
    if _mfd_strip_hit(x, y):
        return True
    return False


def _mfd_airport_tap(tap_x, tap_y, tap_px=22):
    """Hit-test airports against a screen tap.  Uses the same projection
    moving_map.render() uses, and matches its drawing gates so taps
    only consider airports that are actually visible on screen:
    no airports drawn past 40 nm (no taps); type-filtered by zoom band
    above 5 nm (same filter the draw loop uses).  Returns True if an
    airport was hit (and the nav_confirm modal was opened)."""
    if _airports is None:
        return False
    range_nm = int(disp["ds"].get("map_zoom_nm", 10))
    # Past 40 nm moving_map skips airports entirely — accept no tap
    # there so a finger landing on empty terrain doesn't D2 to an
    # invisible airport (most often hit while reaching for the orient
    # toggle at top-right of the MFD).
    if range_nm > 40 or range_nm <= 0:
        return False
    # Zoom-band type filter mirrors moving_map's drawing logic.
    if range_nm > 20:
        allowed_band = {"L"}
    elif range_nm > 10:
        allowed_band = {"M", "L"}
    elif range_nm > 5:
        allowed_band = {"S", "M", "L"}
    else:
        allowed_band = None    # all visible types
    rect = (0, 0, DISPLAY_W, DISPLAY_H)
    cen_lat, cen_lon = _mfd_effective_center()
    hdg = disp.get("yaw", 0.0)
    track = disp.get("track", hdg)
    orient = disp["ds"].get("map_orient", "trk")
    project, _ = _mfd_map.make_projector(
        rect, cen_lat, cen_lon, orient, range_nm, hdg, track)
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
        return False
    best_d2 = (tap_px + 1) ** 2
    best = None
    if hasattr(nearby, "dtype"):
        # Same MAX_AIRPORTS_DRAWN cap as the renderer — taps can't pick
        # an airport that was decimated out of the visible list.
        max_considered = 40
        considered = 0
        for i in range(len(nearby)):
            if considered >= max_considered:
                break
            atype = str(nearby["atype"][i])
            if allowed_band is not None and atype not in allowed_band:
                continue
            if not apt_types.get(atype, False):
                continue
            sx, sy = project(float(nearby["lat"][i]),
                             float(nearby["lon"][i]))
            d2 = (sx - tap_x) ** 2 + (sy - tap_y) ** 2
            if d2 < best_d2:
                best_d2 = d2
                best = str(nearby["ident"][i])
            considered += 1
    else:
        considered = 0
        for r in nearby:
            if considered >= 40:
                break
            atype = getattr(r, "atype", "")
            if allowed_band is not None and atype not in allowed_band:
                continue
            if not apt_types.get(atype, False):
                continue
            sx, sy = project(float(r.lat), float(r.lon))
            d2 = (sx - tap_x) ** 2 + (sy - tap_y) ** 2
            if d2 < best_d2:
                best_d2 = d2
                best = r.ident
            considered += 1
    if best is None:
        return False
    _nav_open_confirm(best, "pfd")
    return True


def _mfd_apply_drag(d, dx_px, dy_px):
    """Apply pixel drag delta to the pan center using the projection
    parameters captured on DOWN.  Inverse-rotates the screen delta so
    track-up panning feels natural."""
    px_per_nm = d["px_per_nm"]
    if px_per_nm <= 0:
        return
    e_s = dx_px / px_per_nm
    n_s = -dy_px / px_per_nm
    rot_deg = d["rot_deg"]
    if rot_deg != 0.0:
        rr = math.radians(rot_deg)
        cr, sr = math.cos(rr), math.sin(rr)
        e_nm = e_s * cr + n_s * sr
        n_nm = -e_s * sr + n_s * cr
    else:
        e_nm, n_nm = e_s, n_s
    cos_lat = max(0.05, math.cos(math.radians(d["base_lat"])))
    disp["mfd_pan"]["lat"] = d["base_lat"] - n_nm / 60.0
    disp["mfd_pan"]["lon"] = d["base_lon"] - e_nm / (60.0 * cos_lat)


# ── Main render function ──────────────────────────────────────────────────────
def render(surf, demo_mode, connected, data_stale=False):
    mode = disp.get("mode", "pfd")

    # ── Full-screen replacement screens (no PFD behind them) ─────────────────
    if mode == "setup":
        draw_setup_screen(surf); return
    if mode == "flight_profile":
        draw_flight_profile(surf, disp["fp"]); return
    if mode == "display_setup":
        draw_display_setup(surf, disp["ds"]); return
    if mode == "ahrs_setup":
        draw_ahrs_setup(surf, disp["ss"]); return
    if mode == "mag_cal":
        # Draw AHRS setup behind so the modal sits on top of the screen
        # it was launched from.
        draw_ahrs_setup(surf, disp["ss"])
        draw_mag_cal(surf); return
    if mode == "nav_confirm":
        # Modal is borderless; paint whatever screen the caller was on,
        # then the modal on top.
        prev = disp.get("nav_confirm_prev", "pfd")
        if prev == "pfd":
            disp["mode"] = "pfd"
            render(surf, demo_mode, connected, data_stale)
            disp["mode"] = "nav_confirm"
        draw_nav_confirm(surf); return
    if mode == "wifi_scan":
        draw_wifi_scan(surf, disp["cs"]); return
    if mode == "connectivity_setup":
        draw_connectivity_setup(surf, disp["cs"]); return
    if mode == "screen_sync_setup":
        draw_screen_sync_setup(surf, disp["cs"]); return
    if mode == "mfd_strip_setup":
        draw_mfd_strip_setup(surf); return
    if mode == "fpl":
        draw_fpl(surf); return
    if mode == "fpl_latlon_entry":
        draw_fpl_latlon_entry(surf); return
    if mode == "user_wpt_picker":
        draw_user_wpt_picker(surf); return
    if mode == "system_setup":
        draw_system_setup(surf); return
    if mode == "ahrs_firmware":
        draw_ahrs_firmware(surf); return
    if mode == "terrain_data":
        draw_terrain_data(surf, disp["td"]); return
    if mode == "obstacle_data":
        draw_obstacle_data(surf, disp["od"]); return
    if mode == "airport_data":
        draw_airport_data(surf, disp["ad"]); return
    if mode == "airspace_data":
        draw_airspace_data(surf); return
    if mode == "sim_setup":
        draw_sim_setup(surf); return

    # ── MFD: full-screen moving map (replaces the PFD when toggled) ──────────
    # Gated by mfd_enabled so a stale display_mode == "mfd" in settings.json
    # can't strand the user on the MFD when they've disabled the feature.
    if (disp.get("display_mode", "pfd") == "mfd"
            and disp["ds"].get("mfd_enabled", False)
            and mode == "pfd"):
        # Terrain / obstacle alerts work on the MFD too.  Use the
        # higher of GPS GS and IAS (when airdata_ok) so the alert
        # arms whichever speed source the pilot has selected — and
        # actually fires sooner when both are available.  gps_ok is
        # still required since we need position for the lookups.
        _alert_speed = disp.get("speed", 0.0)
        if disp.get("airdata_ok"):
            _alert_speed = max(_alert_speed, disp.get("ias_kt", 0.0))
        _update_terrain_alert(
            disp.get("lat", 0.0), disp.get("lon", 0.0),
            disp.get("alt", 0.0), _alert_speed,
            disp.get("gps_ok", False),
            vso_kt=disp["fp"].get("vs0", VS0))
        draw_mfd(surf, connected=connected, data_stale=data_stale)
        # Banner overlaid on top of the MFD chrome — same position the
        # PFD uses (top-centre between the D2 and PFD buttons there's
        # plenty of clear space at y=3).
        draw_terrain_alert(surf)
        return

    # ── PFD always renders for pfd / numpad / keyboard modes ─────────────────
    surf.fill((0, 0, 0))

    roll    = disp["roll"]
    pitch   = disp["pitch"]
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
    baro_ok  = disp["baro_ok"]
    sats     = disp["sats"]
    hdg_bug  = disp["hdg_bug"]
    alt_bug  = disp["alt_bug"]

    # ── AHRS trim + mounting correction ──────────────────────────────────────
    ss = disp["ss"]
    pitch_trim = ss.get("pitch_trim", 0.0)
    roll_trim  = ss.get("roll_trim",  0.0)
    if ss.get("mounting") == "inverted":
        pitch = -pitch + pitch_trim
        roll  = -roll  + roll_trim
    else:
        pitch = pitch + pitch_trim
        roll  = roll  + roll_trim

    # ── Stale-data timeout: no link for > STALE_TIMEOUT_S → treat as AHRS fail
    if data_stale:
        ahrs_ok = False

    # ── Heading source selection ──────────────────────────────────────────────
    # User picks mag / trk / auto in AHRS setup.  _resolve_hdg_source
    # turns the preference + runtime conditions (gps_ok, ahrs_ok, speed)
    # into the actual source for this frame.  In AUTO, TRK wins when
    # GPS is moving above HDG_TRK_MIN_KT, otherwise MAG.  Helpers below
    # receive a resolved string ("trk" or "mag") so their existing
    # checks stay simple.
    hdg_pref = ss.get("hdg_src", "auto")
    use_track, _hdg_label, _hdg_col = _resolve_hdg_source(
        hdg_pref, gps_ok, ahrs_ok, speed)
    hdg_src = "trk" if use_track else "mag"
    if use_track:
        hdg = _update_gps_heading(disp["yaw"], disp["track"], gps_ok)
    else:
        global _gps_hdg, _prev_yaw_disp  # reset filter when not using TRK
        _gps_hdg = _prev_yaw_disp = None
        hdg = disp["yaw"]

    # ── Airspeed source selection ─────────────────────────────────────────────
    # "gps" : GPS groundspeed         → bug triangle / tape source label is magenta
    # "ias" : MS4525/SDP3x airspeed   → bug triangle / tape source label is cyan
    # Effective source resolves to "ias" only when the pilot has selected it AND
    # the air-data sensor is currently fresh (airdata_ok). Auto-falls-back to
    # GPS GS otherwise so a transient sensor dropout doesn't blank the tape.
    _user_src = ss.get("airspeed_src", "gps")
    if _user_src == "ias" and disp.get("airdata_ok"):
        airspeed_src = "ias"
        speed = disp.get("ias_kt", speed)
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

    # 0. Compute terrain/obstacle alert level for this frame
    # Pick the better speed source for alert gating: max of GS and IAS
    # (when airdata_ok) so we don't wait for GPS to spin up before
    # alerts arm on a Pico that has both.
    _alert_speed = speed
    if disp.get("airdata_ok"):
        _alert_speed = max(_alert_speed, disp.get("ias_kt", 0.0))
    _update_terrain_alert(lat, lon, alt, _alert_speed, gps_ok,
                          vso_kt=fp.get("vs0", VS0))

    # Auto-sequence the active flight-plan leg when within the
    # advance threshold of the current waypoint.  Gated on GPS so a
    # bad fix can't blip us through the plan.
    if gps_ok and _fpl_is_active():
        _fpl_check_advance(lat, lon)

    # 1. AI background — draw full-width so tapes are transparent over sky/ground
    _full_ai = (0, 0, DISPLAY_W, HDG_Y)
    draw_simple_ai_background(surf, _full_ai, pitch, roll)

    # 1a. Above-horizon terrain silhouette (mountain peaks rising above
    # the eye-level horizon).  Only renders when SRTM tiles are loaded
    # and at least one ray's peak exceeds aircraft altitude.
    if gps_ok:
        draw_above_horizon_terrain(surf, _full_ai, lat, lon, alt,
                                   hdg, pitch, roll)

    # 1b. Symbol overlays painted in the TERRAIN coordinate frame
    # (heading + pitch only, no roll), then the entire overlay is
    # rotated by the roll angle so symbols stay locked to the terrain
    # during banked turns.
    if gps_ok and (_airports is not None or _obstacles is not None):
        _ov_w = DISPLAY_W
        _ov_h = HDG_Y
        diag = int(math.hypot(_ov_w, _ov_h)) + 4
        _rw = max(_ov_w, diag)
        _rh = max(_ov_h, diag)
        _overlay = pygame.Surface((_rw, _rh), pygame.SRCALPHA)
        _ox = (_rw - _ov_w) // 2
        _oy = (_rh - _ov_h) // 2
        _ov_rect = (_ox, _oy, _ov_w, _ov_h)
        if _airports is not None:
            draw_airport_symbols(_overlay, _ov_rect, lat, lon, alt, hdg, pitch, 0)
        if _obstacles is not None:
            draw_obstacle_symbols(_overlay, _ov_rect, lat, lon, alt, hdg, pitch, 0)
        if abs(roll) > 0.5:
            rotated = pygame.transform.rotate(_overlay, roll)
            rx, ry = rotated.get_size()
            crop_x = (rx - _ov_w) // 2
            crop_y = (ry - _ov_h) // 2
            surf.blit(rotated, (0, 0), area=pygame.Rect(crop_x, crop_y, _ov_w, _ov_h))
        else:
            surf.blit(_overlay, (0, 0), area=pygame.Rect(_ox, _oy, _ov_w, _ov_h))

    # 2. Pitch ladder (with roll rotation)
    draw_pitch_ladder(surf, ai_rect, pitch, roll)

    # 3. Speed tape (display unit, fp V-speeds)
    draw_speed_tape(surf, speed_d, gs_bug=gs_bug_d,
                    vs0=vs0_d, vs1=vs1_d, vfe=vfe_d, vno=vno_d, vne=vne_d,
                    airspeed_src=airspeed_src)

    # 4. Alt tape (display unit)
    draw_alt_tape(surf, alt_d, vspeed, baro_hpa, baro_src, alt_bug_d,
                  baro_ok=baro_ok)

    # 5. Heading tape
    draw_heading_tape(surf, hdg, hdg_bug, track, gps_ok, hdg_src=hdg_src)

    # 6. Roll arc
    draw_roll_arc(surf, roll)

    # 7. Aircraft symbol
    draw_aircraft_symbol(surf)

    # 8. Slip ball
    draw_slip_ball(surf, ay)

    # 9. Status badges
    draw_status_badges(surf, ahrs_ok, gps_ok, baro_ok, baro_src, sats, connected,
                       hdg_src=hdg_src)

    # 9b. Terrain / obstacle proximity alert banner (centre of badge strip)
    draw_terrain_alert(surf)

    # 10. Failure overlays
    draw_failure_overlays(surf, ahrs_ok, gps_ok, baro_ok, sats)

    # 11. Tap-buttons for heading bug, baro, and alt bug (color = data source)
    draw_tap_buttons(surf, hdg, hdg_bug, baro_hpa, baro_src, alt_bug,
                     hdg_src=hdg_src, baro_ok=baro_ok)

    # 12. CDI strip — only when GPS is locked; bare strip + DIRECT prompt
    # when no waypoint is active so the strip is always tappable.
    if gps_ok:
        draw_cdi(surf)

    # 12. Demo / SIM watermark
    if demo_mode:
        _text(surf, "DEMO", 14, (255, 60, 60), cx=CX, cy=CY - 20)
    elif _sim_state is not None:
        _text(surf, "SIM", 14, (255, 100, 60), cx=CX, cy=CY - 20)

    # ── Overlay modes: veil + UI drawn on top of live PFD ────────────────────
    if mode == "sim_controls":
        draw_sim_controls(surf)

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
        # Bug entries use current display unit; storage stays canonical.
        spd_unit_lbl = {"kt": "kt", "mph": "mph", "kph": "kph"}.get(
            disp["ds"].get("spd_unit", "kt"), "kt")
        alt_unit_lbl = {"ft": "ft", "m": "m"}.get(
            disp["ds"].get("alt_unit", "ft"), "ft")
        spd_bug_src  = "IAS" if airspeed_src == "ias" else "GS"
        spd_bug_title = f"SET {spd_bug_src} BUG  ({spd_unit_lbl})"
        titles  = {"alt_bug":   f"SET ALTITUDE BUG  (\u00d7100 {alt_unit_lbl})",
                   "hdg_bug":   "SET HEADING BUG",
                   "spd_bug":   spd_bug_title,
                   "baro_hpa":  baro_title,
                   "sim_init_alt": f"SET INITIAL ALTITUDE  (\u00d7100 {alt_unit_lbl})",
                   "sim_init_hdg": "SET INITIAL HEADING",
                   "sim_init_spd": "SET INITIAL SPEED  (kt)"}
        curvals = {"alt_bug":   int(round(disp.get("alt_bug", 0)
                                          * _ALT_DISP_FACTOR())) // 100,
                   "hdg_bug":   int(disp.get("hdg_bug", 0)),
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
            # Direct-to: surface the active ident as the placeholder so the
            # pilot sees what's active while typing the replacement.
            cur   = disp.get("nav", {}).get("ident", "")
            title = "WAYPOINT"
        elif target == "fpl_ident":
            cur, title = "", "ICAO IDENT"
        elif target == "fpl_latlon_ident":
            cur, title = disp["fpl_new"].get("ident", ""), "WAYPOINT NAME"
        elif target == "fpl_latlon_lat":
            cur   = disp["fpl_new"].get("lat_str", "")
            title = "LATITUDE  (decimal °)"
        elif target == "fpl_latlon_lon":
            cur   = disp["fpl_new"].get("lon_str", "")
            title = "LONGITUDE  (decimal °)"
        elif prev == "connectivity_setup":
            cur   = disp["cs"].get(target, "")
            title = {"ahrs_url": "AHRS URL", "wifi_ssid": "WiFi SSID",
                     "wifi_pass": "WiFi PASSWORD"}.get(target, "ENTER TEXT")
        else:
            cur   = disp["fp"].get(target, "")
            title = next((f[1] for f in _FP_FIELDS if f[0]==target), "ENTER TEXT")
        draw_keyboard(surf, f"ENTER {title}", cur, buf, transparent=True,
                      error=disp.get("kbd_error", ""))


# ── Terrain availability (computed once at import time) ───────────────────────
def _check_terrain():
    """True if either the high-res SRTM cache or the coarse Mapzen cache
    has at least one tile on disk — silhouette and TAWS lookups will
    happily mix the two via get_elevation_ft_combined."""
    if os.path.isdir(SRTM_DIR) and any(f.endswith(".hgt") for f in os.listdir(SRTM_DIR)):
        return True
    if os.path.isdir(COARSE_DIR) and any(f.endswith(".png") for f in os.listdir(COARSE_DIR)):
        return True
    return False

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


def _startup_load_airspaces():
    """Background thread: load airspace polygons.  Falls back to a
    small bundled example dataset when no real data is on disk so the
    render path is verifiable end-to-end before the pilot drops in a
    NASR-derived file."""
    global _airspaces
    loaded = asp_mod.load(AIRSPACE_DIR)
    if loaded is None:
        loaded = asp_mod.load_bundled_example()
        disp["asp"]["records"] = 0
        print(f"[PFD] Airspaces: no airspaces.json found at {AIRSPACE_DIR}; "
              f"using bundled {len(loaded)}-record example")
    else:
        disp["asp"]["records"] = len(loaded)
        print(f"[PFD] Airspaces: {len(loaded)} polygons loaded")
    _airspaces = loaded


# ── Main entry point ──────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="PFD Display")
    parser.add_argument("--demo", action="store_true",
                        help="Run Sedona demo (no Pico W needed)")
    parser.add_argument("--sim",  action="store_true",
                        help="Windowed mode for desktop testing")
    # Screenshot mode: render one frame to a PNG then exit.
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
    args = parser.parse_args()

    if args.sim or not FULLSCREEN:
        # Desktop / windowed mode — let SDL auto-detect the display server
        # (x11 on X.Org, wayland on Wayfire/Weston, etc.) instead of forcing
        # kmsdrm which is only correct for bare-console fullscreen.
        os.environ.pop("SDL_VIDEODRIVER", None)
        os.environ.pop("SDL_FBDEV", None)

    # Restore persisted user settings before initialising the display
    if _settings.load_into(disp, SETTINGS_PATH):
        print(f"[PFD] Settings restored from {SETTINGS_PATH}")
    # Migrate legacy hdg_src values: "gps" was the old name for "trk",
    # and the UI never offered "auto" before — default any unexpected
    # value to "auto" so the setting matches what the new selector shows.
    legacy_hdg = disp["ss"].get("hdg_src", "auto")
    if legacy_hdg == "gps":
        disp["ss"]["hdg_src"] = "trk"
    elif legacy_hdg not in ("mag", "trk", "auto"):
        disp["ss"]["hdg_src"] = "auto"
    # Clamp persisted map zoom — a saved value >20 nm would have crashed
    # the Pi on the first MFD render, so guarantee we boot at a safe range.
    saved_zoom = int(disp["ds"].get("map_zoom_nm", 10))
    # AUTO (0) is valid; only clamp negative or above-cap values.
    if saved_zoom > MFD_MAX_ZOOM_NM or saved_zoom < 0:
        disp["ds"]["map_zoom_nm"] = min(MFD_MAX_ZOOM_NM, 10)
    # Migrate users whose settings.json predates the mfd_enabled gate.
    # The legacy `display_mode` field was the user-toggled setting; if it
    # was set to "mfd" they had been using the MFD before the gate
    # existed.  Force-enable the new gate so they don't have to re-find
    # the toggle (it moved from System Setup → DISPLAY MODE to System
    # Setup → ENABLE MFD).
    if disp.get("display_mode") == "mfd":
        disp["ds"]["mfd_enabled"] = True
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
    _screen_sync.on(_ssync_mod.KIND_AHRS, _ssync_apply_ahrs)
    _screen_sync.on(_ssync_mod.KIND_GPS,  _ssync_apply_gps)
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

    # Disable vsync so display.flip() doesn't block waiting for the display's
    # vsync signal (which was taking ~82 ms at ~12 Hz on KMS/DRM, halving FPS).
    os.environ.setdefault("SDL_RENDER_VSYNC", "0")
    os.environ.setdefault("SDL_VIDEO_KMSDRM_VSYNC", "0")

    pygame.init()
    pygame.mouse.set_visible(False)

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
        """
        if surf is not screen:
            # Rotated display — manual transform + scale
            s = pygame.transform.rotate(surf, DISPLAY_ROTATE)
            screen.fill((0, 0, 0))
            screen.blit(pygame.transform.scale(s, (_sw, _sh)), (_sx, _sy))
        pygame.display.flip()

    pygame.display.set_caption("PFD")
    clock = pygame.time.Clock()

    # ── Screenshot mode ───────────────────────────────────────────────────────
    # Seed state directly (bypasses IIR smoothing), render one frame, save PNG.
    #   python3 pfd.py --screenshot ~/ss/sedona_cruise.png
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

        _seed(roll=0, pitch=2, hdg=133, alt=8500, speed=115, hdg_src="trk")
        _save("preview_gps_trk_mode.png")

        _seed(roll=0, pitch=0, hdg=133, alt=8500, speed=115, baro_ok=False)
        _save("preview_badges_no_data.png")

        _seed(roll=0, pitch=0, hdg=133, alt=8500, speed=115)
        disp["od"]["expired"] = True
        disp["od"]["records"] = 76842
        _save("preview_badges_exp_obs.png")
        disp["od"]["expired"] = False

        _seed(roll=0, pitch=2, hdg=133, alt=8500, speed=115)
        _save("pfd_preview.png")

        # ── Numpad overlays ───────────────────────────────────────────────────
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
            ("sim_setup",           "preview_sim_setup.png"),
        ]:
            disp["mode"] = screen_mode
            _save(fname)

        disp["ss"]["hdg_src"] = "trk"
        disp["mode"] = "ahrs_setup"
        _save("preview_setup_ahrs_gpstrk.png")
        disp["ss"]["hdg_src"] = "auto"

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

        # ── Terrain proximity alert scenes ────────────────────────────────────
        _seed(roll=0, pitch=-2, hdg=133, alt=5500, speed=95, vspeed=-200)
        try:
            globals()['_terrain_alert_level'] = 1
            globals()['_terrain_alert_alpha'] = 1.0
        except Exception:
            pass
        _save("preview_terrain_caution.png")

        _seed(roll=0, pitch=-5, hdg=133, alt=5200, speed=95, vspeed=-400)
        try:
            globals()['_terrain_alert_level'] = 2
            globals()['_terrain_alert_alpha'] = 1.0
        except Exception:
            pass
        _save("preview_terrain_warning.png")

        try:
            globals()['_terrain_alert_level'] = 0
            globals()['_terrain_alert_alpha'] = 0.0
        except Exception:
            pass

        # ── VR cascade demo ───────────────────────────────────────────────────
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

    if not demo_mode:
        global _sse_client
        # Try USB serial first — direct connection to the Pico W AHRS,
        # no Wi-Fi needed. Falls back to SSE over Wi-Fi if no USB device
        # is enumerated (no Pico W plugged in).
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

    _show_boot_splash(surf, _flip)

    running = True
    while running:
        # Update demo state
        if demo_mode and demo:
            demo.tick()

        # Update flight simulator state (mutually exclusive with demo)
        if _sim_state is not None:
            _sim_state.tick()

        # Smooth sensor values into display values
        smooth_state()

        # Push AHRS / GPS to peer screens (rate-limited inside the helpers).
        _ssync_publish_ahrs()
        _ssync_publish_gps()

        # Events
        for event in pygame.event.get():
            try:
                result = handle_event(event, demo_mode)
            except Exception:
                # Don't let a bad touch tear down the whole PFD — log and
                # carry on so the pilot can back out of whatever screen
                # tripped the handler.
                import traceback
                print("[PFD] handle_event crashed:", file=sys.stderr)
                traceback.print_exc()
                result = None
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

        # Multi-finger long-press gestures.  Two distinct gestures share
        # the same `_multitouch_t0` start instant; `_multitouch_max_fingers`
        # disambiguates which one fires:
        #   • exactly 2 fingers held LONG_PRESS_MS (800 ms)  → enter setup
        #   • 3 or more fingers held MFD_SWAP_HOLD_MS (2 s) → swap PFD ↔ MFD
        # The 3-finger threshold is deliberately longer so a 2-finger setup
        # hold can't accidentally trigger the swap when the pilot grazes
        # the screen with a 3rd finger after 800 ms.
        if (_multitouch_t0 is not None
                and len(_active_fingers) >= 2
                and disp["mode"] == "pfd"):
            dt = pygame.time.get_ticks() - _multitouch_t0
            if (_multitouch_max_fingers >= 3
                    and dt >= MFD_SWAP_HOLD_MS
                    and disp["ds"].get("mfd_enabled", False)):
                # PFD ↔ MFD swap.  Stays in the same `mode == "pfd"`
                # event-handler state; only `display_mode` flips.
                disp["display_mode"] = (
                    "mfd" if disp.get("display_mode", "pfd") == "pfd"
                    else "pfd")
                _settings.mark_dirty()
                _active_fingers.clear()
                _multitouch_t0 = None
                _multitouch_max_fingers = 0
            elif _multitouch_max_fingers == 2 and dt >= LONG_PRESS_MS:
                disp["mode"] = "setup"
                _active_fingers.clear()
                _multitouch_t0 = None
                _multitouch_max_fingers = 0

        # Render
        _t0 = time.monotonic()
        render(surf, demo_mode, connected, data_stale=data_stale)
        _t1 = time.monotonic()
        _flip()
        _t2 = time.monotonic()
        clock.tick(TARGET_FPS)

        # Print frame timing every 60 frames so we can diagnose bottlenecks
        if not hasattr(main, '_frame_n'):
            main._frame_n = 0
        main._frame_n += 1
        if main._frame_n % 60 == 0:
            render_ms = (_t1 - _t0) * 1000
            flip_ms   = (_t2 - _t1) * 1000
            fps       = clock.get_fps()
            print(f"[PFD] fps={fps:.1f}  render={render_ms:.1f}ms  flip={flip_ms:.1f}ms")

    if _sse_client:
        _sse_client.stop()
    _settings.flush()
    pygame.quit()


if __name__ == "__main__":
    main()
