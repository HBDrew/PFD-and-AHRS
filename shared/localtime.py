"""
localtime.py – exact-or-approximate local time for a GPS position.

Turns a UTC instant + lat/lon into the correct *local* time.  When the optional
``timezonefinder`` package is installed it does a real ``lat/lon → IANA zone``
lookup (so it knows where the timezone *lines* are and flips zones the moment
you cross one) and applies that zone's Daylight-Saving rules via the stdlib
``zoneinfo`` — entirely offline, no internet.  When timezonefinder isn't
present it falls back to a longitude-derived whole-hour offset
(``round(lon / 15)``, no DST), so callers always get *a* local time and the
display silently upgrades to exact once the package is installed.

Used for TAF / METAR / ETA local-time display.  The timezonefinder import is
lazy and guarded so importing this module is cheap and never fails.

Enable the exact path on a device with:  pip install timezonefinder
"""

import threading
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
    _HAS_ZONEINFO = True
except ImportError:                       # pragma: no cover  (py < 3.9)
    ZoneInfo = None
    _HAS_ZONEINFO = False

_tf = None
_tf_tried = False
_tf_lock = threading.Lock()

# Cache tz-name lookups by quantised position — the zone only changes at a
# boundary, so ~0.2° (≈12 nm) cells keep the (relatively pricey) polygon query
# off the hot path as the aircraft creeps.
_tz_cache = {}
_TZ_QUANT = 0.2
_TZ_CACHE_MAX = 64


def _finder():
    """Lazily construct the TimezoneFinder, once; None if unavailable."""
    global _tf, _tf_tried
    if _tf is None and not _tf_tried:
        with _tf_lock:
            if _tf is None and not _tf_tried:
                _tf_tried = True
                try:
                    from timezonefinder import TimezoneFinder
                    _tf = TimezoneFinder()
                except Exception:
                    _tf = None
    return _tf


def available():
    """True when the exact (boundary + DST) path is usable on this device."""
    return _HAS_ZONEINFO and _finder() is not None


def tz_name_for(lat, lon):
    """IANA timezone name (e.g. ``America/Phoenix``) for a position, or None."""
    if lat is None or lon is None or not _HAS_ZONEINFO:
        return None
    key = (round(lat / _TZ_QUANT), round(lon / _TZ_QUANT))
    if key in _tz_cache:
        return _tz_cache[key]
    tf = _finder()
    name = None
    if tf is not None:
        try:
            name = tf.timezone_at(lat=float(lat), lng=float(lon))
        except Exception:
            name = None
    if len(_tz_cache) > _TZ_CACHE_MAX:
        _tz_cache.clear()
    _tz_cache[key] = name
    return name


def _zone_dt(lat, lon, when_utc):
    """The given UTC instant expressed in the position's zone, or None when the
    exact path isn't available."""
    name = tz_name_for(lat, lon)
    if not name:
        return None
    try:
        return when_utc.astimezone(ZoneInfo(name))
    except Exception:
        return None


def offset_hours(lat, lon, when_utc=None):
    """Local UTC offset in hours (float) at the position for ``when_utc`` (now
    if None).  DST/boundary-correct when timezonefinder+zoneinfo are present,
    else the longitude approximation ``round(lon / 15)``.  None if no longitude.
    """
    if lon is None:
        return None
    when = when_utc or datetime.now(timezone.utc)
    zdt = _zone_dt(lat, lon, when)
    if zdt is not None:
        off = zdt.utcoffset()
        if off is not None:
            return off.total_seconds() / 3600.0
    try:
        return float(round(float(lon) / 15.0))
    except (TypeError, ValueError):
        return None


def abbrev(lat, lon, when_utc=None):
    """Short zone label (e.g. ``MST`` / ``PDT``) when the exact path is active,
    else '' (callers then label the time plain ``L``)."""
    when = when_utc or datetime.now(timezone.utc)
    zdt = _zone_dt(lat, lon, when)
    if zdt is None:
        return ""
    try:
        return zdt.tzname() or ""
    except Exception:
        return ""
