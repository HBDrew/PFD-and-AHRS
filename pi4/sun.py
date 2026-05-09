"""
sun.py – Solar position from UTC + lat/lon, for SVT terrain shading.

Uses the standard NOAA / "low-precision" solar position formulas
(General Solar Position Calculations, J. Meeus simplified).  Accuracy
is well under 1° in azimuth and elevation across the next century —
more than enough to drive terrain illumination.  Returns:
    azimuth_deg    compass bearing of the sun (0=N, 90=E, 180=S, 270=W)
    elevation_deg  altitude above the local horizon (negative = below)
    intensity      0.0 (sun well below horizon) → 1.0 (sun ≥ +6°)
                   smooth ramp through civil twilight so dawn/dusk
                   doesn't step.

The intensity ramp lets the caller hand the value straight to the
shader's `u_sun_intensity` uniform without a separate night-mode
branch — Lambertian lighting fades out automatically when the sun
drops below the horizon.
"""

import math
import time as _time


def _julian_day(unix_ts: float) -> float:
    return unix_ts / 86400.0 + 2440587.5


def solar_position(lat_deg: float, lon_deg: float,
                   unix_ts: float | None = None
                   ) -> tuple[float, float, float]:
    """Compute (azimuth_deg, elevation_deg, intensity) for the given
    location and UTC time.  ``unix_ts`` defaults to the current system
    clock; pass an explicit timestamp for sim/preview replays.
    """
    if unix_ts is None:
        unix_ts = _time.time()

    # Days since J2000.0 (2000-01-01 12:00 UT)
    n = _julian_day(unix_ts) - 2451545.0

    # Mean longitude and mean anomaly of the sun (degrees)
    L = (280.460 + 0.9856474 * n) % 360.0
    g = math.radians((357.528 + 0.9856003 * n) % 360.0)

    # Ecliptic longitude (degrees → radians)
    lam = math.radians(L + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g))

    # Obliquity of the ecliptic
    eps = math.radians(23.439 - 0.0000004 * n)

    # Right ascension and declination
    ra = math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam))
    dec = math.asin(math.sin(eps) * math.sin(lam))

    # Greenwich Mean Sidereal Time (hours) → local hour angle
    jd = _julian_day(unix_ts)
    gmst_h = (18.697374558 + 24.06570982441908 * (jd - 2451545.0)) % 24.0
    lst_rad = math.radians(gmst_h * 15.0 + lon_deg)
    h = lst_rad - ra        # hour angle

    lat = math.radians(lat_deg)
    sin_el = math.sin(lat) * math.sin(dec) + math.cos(lat) * math.cos(dec) * math.cos(h)
    el = math.asin(max(-1.0, min(1.0, sin_el)))

    # Azimuth from north, clockwise (compass convention)
    sin_az = -math.cos(dec) * math.sin(h)
    cos_az = math.sin(dec) * math.cos(lat) - math.cos(dec) * math.sin(lat) * math.cos(h)
    az = (math.degrees(math.atan2(sin_az, cos_az))) % 360.0
    el_deg = math.degrees(el)

    # Civil-twilight intensity ramp:  -6° → 0,  +6° → 1
    if el_deg <= -6.0:
        intensity = 0.0
    elif el_deg >= 6.0:
        intensity = 1.0
    else:
        intensity = (el_deg + 6.0) / 12.0

    return az, el_deg, intensity
