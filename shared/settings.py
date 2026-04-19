"""
settings.py – User settings persistence for the PFD.

Saves a subset of the disp[] dict to a JSON file so pilot-configurable
values (V-speeds, units, brightness, AHRS trims, airport filters, etc.)
survive restarts and power cycles.

Usage:
    from settings import load_into, save_from, mark_dirty, flush

    load_into(disp, path)       # called once at startup
    ...
    mark_dirty()                # called whenever a user setting changes
                                # (debounced write, ~1.5 s after last change)

    flush(disp, path)           # called on graceful shutdown

Only a whitelisted set of nested keys is persisted — flight state (roll,
pitch, bugs, etc.) is NOT saved, so the PFD always starts clean.

Thread-safe: mark_dirty() can be called from the main loop, writes
happen on a daemon thread so the UI never blocks on disk I/O.
"""

import json
import os
import threading
import time

# ── Which top-level disp subtrees to persist ──────────────────────────────────
# Nested dicts (one level) — only their leaf values get saved.
_PERSIST_SUBTREES = [
    "fp",    # Flight profile: V-speeds, callsign, aircraft type
    "ds",    # Display settings: units, brightness, baro_unit
    "ss",    # Sensor settings: trims, mounting, heading/airspeed source
    "cs",    # Connectivity: AHRS URL, WiFi SSID  (not password — see below)
    "ad",    # Airport data: filter toggles (show_public, show_heli, etc.)
]

# Top-level scalar keys that persist as-is
_PERSIST_SCALARS = [
    "hdg_bug",      # optional — pilot may prefer to start fresh each flight
    "alt_bug",
    "display_mode",
]

# Keys within subtrees that we deliberately do NOT persist (volatile or secret)
_SKIP_KEYS = {
    "cs": {"wifi_pass", "ahrs_ok", "test_msg", "apply_msg", "wifi_ok"},
    "ad": {"downloading", "dl_status", "dl_cancel", "parsing",
           "records", "used_mb", "dl_date", "age_days", "expired"},
    "ss": {"mag_cal"},   # calibration state is runtime-only
}


# ── Internal state ────────────────────────────────────────────────────────────
_dirty_flag = threading.Event()
_writer_started = False
_writer_path = None
_writer_disp = None
_WRITE_DELAY_S = 1.5   # debounce: save 1.5 s after last change


def _extract(disp: dict) -> dict:
    """Build a shallow, JSON-safe snapshot of persistable settings."""
    out = {"subtrees": {}, "scalars": {}}
    for sub in _PERSIST_SUBTREES:
        if sub not in disp:
            continue
        skip = _SKIP_KEYS.get(sub, set())
        out["subtrees"][sub] = {
            k: v for k, v in disp[sub].items()
            if k not in skip and isinstance(v, (bool, int, float, str, type(None)))
        }
    for k in _PERSIST_SCALARS:
        if k in disp and isinstance(disp[k], (bool, int, float, str, type(None))):
            out["scalars"][k] = disp[k]
    return out


def _apply(disp: dict, data: dict) -> None:
    """Merge a loaded snapshot back into disp (in place)."""
    for sub, values in data.get("subtrees", {}).items():
        if sub not in disp or not isinstance(disp[sub], dict):
            continue
        skip = _SKIP_KEYS.get(sub, set())
        for k, v in values.items():
            if k in skip:
                continue
            disp[sub][k] = v
    for k, v in data.get("scalars", {}).items():
        if k in _PERSIST_SCALARS:
            disp[k] = v


def load_into(disp: dict, path: str) -> bool:
    """Load persisted settings from `path` into `disp` in place.
    Returns True if a file was loaded, False if none existed or load failed."""
    if not path or not os.path.exists(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        _apply(disp, data)
        return True
    except (OSError, json.JSONDecodeError, ValueError) as e:
        print(f"[settings] Failed to load {path}: {e}")
        return False


def save_from(disp: dict, path: str) -> bool:
    """Synchronously write persistable settings to `path`.  Atomic replace."""
    if not path:
        return False
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_extract(disp), f, indent=2, sort_keys=True)
        os.replace(tmp, path)
        return True
    except OSError as e:
        print(f"[settings] Failed to save {path}: {e}")
        return False


def _writer_loop():
    """Background thread: waits for dirty flag, then writes after a short
    debounce so rapid consecutive changes batch into one disk write."""
    global _dirty_flag
    while True:
        _dirty_flag.wait()
        time.sleep(_WRITE_DELAY_S)
        _dirty_flag.clear()
        if _writer_disp is not None and _writer_path is not None:
            save_from(_writer_disp, _writer_path)


def start(disp: dict, path: str) -> None:
    """Start the background debounced writer.  Call once at startup after
    load_into() has restored the previous settings."""
    global _writer_started, _writer_path, _writer_disp
    _writer_path = path
    _writer_disp = disp
    if _writer_started:
        return
    t = threading.Thread(target=_writer_loop, daemon=True, name="SettingsWriter")
    t.start()
    _writer_started = True


def mark_dirty() -> None:
    """Signal that a user-configurable setting changed; triggers a debounced
    save by the background writer thread."""
    _dirty_flag.set()


def flush(disp: dict = None, path: str = None) -> None:
    """Force a synchronous save (used at graceful shutdown).  If disp/path
    are omitted, uses the values from the last start() call."""
    if disp is None:
        disp = _writer_disp
    if path is None:
        path = _writer_path
    if disp is not None and path is not None:
        save_from(disp, path)
