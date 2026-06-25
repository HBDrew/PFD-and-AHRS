"""World Magnetic Model 2025 — magnetic declination (variation) only.

The whole EFIS is internally TRUE-referenced: GPS track is true, the
magnetometer is calibrated against true cardinals, and every stored course
(approach course, flight-plan leg bearing) is a true great-circle bearing.
Charts, plates, runway numbers and ATC clearances are MAGNETIC, so for the
pilot-facing display we convert true -> magnetic with the local variation.

This module provides that variation from the WMM2025 spherical-harmonic model
(valid 2025.0–2030.0).  It is self-contained: the degree-12 Gauss coefficients
are embedded below, so there is no data file to ship to each Pi and no network
dependency.

Convention: declination east-positive (the usual aviation sense).  In Arizona
it is ~+10–11° (east), so a true bearing of 041° reads ~030° magnetic.

    magnetic = true - declination_east

Coefficients: NOAA/NGA World Magnetic Model 2025 (public domain).
Computation vendored and trimmed to declination-only from pygeomag
(https://pypi.org/project/pygeomag/, MIT, © 2023 Justin Myers), which in turn
follows the WMM reference C implementation.
"""

import math

# (n, m, g_nm, h_nm, dg_nm, dh_nm) — WMM2025, nT and nT/yr.
_EPOCH = 2025.0
_COEFFS = [
    (1, 0, -29351.8, 0.0, 12.0, 0.0),
    (1, 1, -1410.8, 4545.4, 9.7, -21.5),
    (2, 0, -2556.6, 0.0, -11.6, 0.0),
    (2, 1, 2951.1, -3133.6, -5.2, -27.7),
    (2, 2, 1649.3, -815.1, -8.0, -12.1),
    (3, 0, 1361.0, 0.0, -1.3, 0.0),
    (3, 1, -2404.1, -56.6, -4.2, 4.0),
    (3, 2, 1243.8, 237.5, 0.4, -0.3),
    (3, 3, 453.6, -549.5, -15.6, -4.1),
    (4, 0, 895.0, 0.0, -1.6, 0.0),
    (4, 1, 799.5, 278.6, -2.4, -1.1),
    (4, 2, 55.7, -133.9, -6.0, 4.1),
    (4, 3, -281.1, 212.0, 5.6, 1.6),
    (4, 4, 12.1, -375.6, -7.0, -4.4),
    (5, 0, -233.2, 0.0, 0.6, 0.0),
    (5, 1, 368.9, 45.4, 1.4, -0.5),
    (5, 2, 187.2, 220.2, 0.0, 2.2),
    (5, 3, -138.7, -122.9, 0.6, 0.4),
    (5, 4, -142.0, 43.0, 2.2, 1.7),
    (5, 5, 20.9, 106.1, 0.9, 1.9),
    (6, 0, 64.4, 0.0, -0.2, 0.0),
    (6, 1, 63.8, -18.4, -0.4, 0.3),
    (6, 2, 76.9, 16.8, 0.9, -1.6),
    (6, 3, -115.7, 48.8, 1.2, -0.4),
    (6, 4, -40.9, -59.8, -0.9, 0.9),
    (6, 5, 14.9, 10.9, 0.3, 0.7),
    (6, 6, -60.7, 72.7, 0.9, 0.9),
    (7, 0, 79.5, 0.0, -0.0, 0.0),
    (7, 1, -77.0, -48.9, -0.1, 0.6),
    (7, 2, -8.8, -14.4, -0.1, 0.5),
    (7, 3, 59.3, -1.0, 0.5, -0.8),
    (7, 4, 15.8, 23.4, -0.1, 0.0),
    (7, 5, 2.5, -7.4, -0.8, -1.0),
    (7, 6, -11.1, -25.1, -0.8, 0.6),
    (7, 7, 14.2, -2.3, 0.8, -0.2),
    (8, 0, 23.2, 0.0, -0.1, 0.0),
    (8, 1, 10.8, 7.1, 0.2, -0.2),
    (8, 2, -17.5, -12.6, 0.0, 0.5),
    (8, 3, 2.0, 11.4, 0.5, -0.4),
    (8, 4, -21.7, -9.7, -0.1, 0.4),
    (8, 5, 16.9, 12.7, 0.3, -0.5),
    (8, 6, 15.0, 0.7, 0.2, -0.6),
    (8, 7, -16.8, -5.2, -0.0, 0.3),
    (8, 8, 0.9, 3.9, 0.2, 0.2),
    (9, 0, 4.6, 0.0, -0.0, 0.0),
    (9, 1, 7.8, -24.8, -0.1, -0.3),
    (9, 2, 3.0, 12.2, 0.1, 0.3),
    (9, 3, -0.2, 8.3, 0.3, -0.3),
    (9, 4, -2.5, -3.3, -0.3, 0.3),
    (9, 5, -13.1, -5.2, 0.0, 0.2),
    (9, 6, 2.4, 7.2, 0.3, -0.1),
    (9, 7, 8.6, -0.6, -0.1, -0.2),
    (9, 8, -8.7, 0.8, 0.1, 0.4),
    (9, 9, -12.9, 10.0, -0.1, 0.1),
    (10, 0, -1.3, 0.0, 0.1, 0.0),
    (10, 1, -6.4, 3.3, 0.0, 0.0),
    (10, 2, 0.2, 0.0, 0.1, -0.0),
    (10, 3, 2.0, 2.4, 0.1, -0.2),
    (10, 4, -1.0, 5.3, -0.0, 0.1),
    (10, 5, -0.6, -9.1, -0.3, -0.1),
    (10, 6, -0.9, 0.4, 0.0, 0.1),
    (10, 7, 1.5, -4.2, -0.1, 0.0),
    (10, 8, 0.9, -3.8, -0.1, -0.1),
    (10, 9, -2.7, 0.9, -0.0, 0.2),
    (10, 10, -3.9, -9.1, -0.0, -0.0),
    (11, 0, 2.9, 0.0, 0.0, 0.0),
    (11, 1, -1.5, 0.0, -0.0, -0.0),
    (11, 2, -2.5, 2.9, 0.0, 0.1),
    (11, 3, 2.4, -0.6, 0.0, -0.0),
    (11, 4, -0.6, 0.2, 0.0, 0.1),
    (11, 5, -0.1, 0.5, -0.1, -0.0),
    (11, 6, -0.6, -0.3, 0.0, -0.0),
    (11, 7, -0.1, -1.2, -0.0, 0.1),
    (11, 8, 1.1, -1.7, -0.1, -0.0),
    (11, 9, -1.0, -2.9, -0.1, 0.0),
    (11, 10, -0.2, -1.8, -0.1, 0.0),
    (11, 11, 2.6, -2.3, -0.1, 0.0),
    (12, 0, -2.0, 0.0, 0.0, 0.0),
    (12, 1, -0.2, -1.3, 0.0, -0.0),
    (12, 2, 0.3, 0.7, -0.0, 0.0),
    (12, 3, 1.2, 1.0, -0.0, -0.1),
    (12, 4, -1.3, -1.4, -0.0, 0.1),
    (12, 5, 0.6, -0.0, -0.0, -0.0),
    (12, 6, 0.6, 0.6, 0.1, -0.0),
    (12, 7, 0.5, -0.1, -0.0, -0.0),
    (12, 8, -0.1, 0.8, 0.0, 0.0),
    (12, 9, -0.4, 0.1, 0.0, -0.0),
    (12, 10, -0.2, -1.0, -0.1, -0.0),
    (12, 11, -1.3, 0.1, -0.0, 0.0),
    (12, 12, -0.7, 0.2, -0.1, -0.1),
]

_MAXORD = 12
_SIZE = _MAXORD + 1

# --- One-time setup: Schmidt-normalized -> unnormalized Gauss coefficients ---
# Mirrors the WMM reference loader.  c/cd are [m][n], snorm is flat (n+m*SIZE).


def _build():
    c = [[0.0] * _SIZE for _ in range(_SIZE)]
    cd = [[0.0] * _SIZE for _ in range(_SIZE)]
    snorm = [0.0] * (_SIZE * _SIZE)
    fn = [0.0] * _SIZE
    fm = [0.0] * _SIZE
    k = [[0.0] * _SIZE for _ in range(_SIZE)]

    for n, m, gnm, hnm, dgnm, dhnm in _COEFFS:
        if m > _MAXORD:
            break
        c[m][n] = gnm
        cd[m][n] = dgnm
        if m != 0:
            c[n][m - 1] = hnm
            cd[n][m - 1] = dhnm

    snorm[0] = 1.0
    fm[0] = 0.0
    for n in range(1, _MAXORD + 1):
        snorm[n] = snorm[n - 1] * float(2 * n - 1) / float(n)
        j = 2
        m = 0
        D2 = n + 1
        while D2 > 0:
            k[m][n] = float(((n - 1) * (n - 1)) - (m * m)) / float(
                (2 * n - 1) * (2 * n - 3)
            )
            if m > 0:
                flnmj = float((n - m + 1) * j) / float(n + m)
                snorm[n + m * _SIZE] = snorm[n + (m - 1) * _SIZE] * math.sqrt(flnmj)
                j = 1
                c[n][m - 1] = snorm[n + m * _SIZE] * c[n][m - 1]
                cd[n][m - 1] = snorm[n + m * _SIZE] * cd[n][m - 1]
            c[m][n] = snorm[n + m * _SIZE] * c[m][n]
            cd[m][n] = snorm[n + m * _SIZE] * cd[m][n]
            D2 -= 1
            m += 1
        fn[n] = float(n + 1)
        fm[n] = float(n)
    k[1][1] = 0.0
    return c, cd, k, fn, fm, snorm


_C, _CD, _K, _FN, _FM, _SNORM = _build()


def _declination_raw(glat, glon, time):
    """WMM declination in degrees (east +) at sea level for decimal-year time."""
    size = _SIZE
    tc = [[0.0] * size for _ in range(size)]
    dp = [[0.0] * size for _ in range(size)]
    sp = [0.0] * size
    cp = [0.0] * size
    p = list(_SNORM)  # working copy of associated Legendre values

    sp[0] = 0.0
    cp[0] = p[0] = 1.0
    dp[0][0] = 0.0

    a = 6378.137
    b = 6356.7523142
    re = 6371.2
    a2 = a * a
    b2 = b * b
    c2 = a2 - b2
    a4 = a2 * a2
    b4 = b2 * b2
    c4 = a4 - b4

    dt = time - _EPOCH
    alt = 0.0  # sea level — variation barely changes over the GA altitude band

    rlon = math.radians(glon)
    rlat = math.radians(glat)
    srlon = math.sin(rlon)
    srlat = math.sin(rlat)
    crlon = math.cos(rlon)
    crlat = math.cos(rlat)
    srlat2 = srlat * srlat
    crlat2 = crlat * crlat
    sp[1] = srlon
    cp[1] = crlon

    q = math.sqrt(a2 - c2 * srlat2)
    q1 = alt * q
    q2 = ((q1 + a2) / (q1 + b2)) ** 2
    ct = srlat / math.sqrt(q2 * crlat2 + srlat2)
    st = math.sqrt(1.0 - ct * ct)
    r2 = alt * alt + 2.0 * q1 + (a4 - c4 * srlat2) / (q * q)
    r = math.sqrt(r2)
    d = math.sqrt(a2 * crlat2 + b2 * srlat2)
    ca = (alt + d) / r
    sa = c2 * crlat * srlat / (r * d)

    for m in range(2, _MAXORD + 1):
        sp[m] = sp[1] * cp[m - 1] + cp[1] * sp[m - 1]
        cp[m] = cp[1] * cp[m - 1] - sp[1] * sp[m - 1]

    aor = re / r
    ar = aor * aor
    bt = bp = br = bpp = 0.0
    pp = [0.0] * size
    pp[0] = 1.0

    for n in range(1, _MAXORD + 1):
        ar = ar * aor
        m = 0
        D4 = n + 1
        while D4 > 0:
            if n == m:
                p[n + m * size] = st * p[n - 1 + (m - 1) * size]
                dp[m][n] = st * dp[m - 1][n - 1] + ct * p[n - 1 + (m - 1) * size]
            elif n == 1 and m == 0:
                p[n + m * size] = ct * p[n - 1 + m * size]
                dp[m][n] = ct * dp[m][n - 1] - st * p[n - 1 + m * size]
            elif n > 1 and n != m:
                if m > n - 2:
                    p[n - 2 + m * size] = 0.0
                    dp[m][n - 2] = 0.0
                p[n + m * size] = (
                    ct * p[n - 1 + m * size] - _K[m][n] * p[n - 2 + m * size]
                )
                dp[m][n] = (
                    ct * dp[m][n - 1]
                    - st * p[n - 1 + m * size]
                    - _K[m][n] * dp[m][n - 2]
                )

            tc[m][n] = _C[m][n] + dt * _CD[m][n]
            if m != 0:
                tc[n][m - 1] = _C[n][m - 1] + dt * _CD[n][m - 1]

            par = ar * p[n + m * size]
            if m == 0:
                temp1 = tc[m][n] * cp[m]
                temp2 = tc[m][n] * sp[m]
            else:
                temp1 = tc[m][n] * cp[m] + tc[n][m - 1] * sp[m]
                temp2 = tc[m][n] * sp[m] - tc[n][m - 1] * cp[m]
            bt = bt - ar * temp1 * dp[m][n]
            bp += _FM[m] * temp2 * par
            br += _FN[n] * temp1 * par

            if st == 0.0 and m == 1:
                if n == 1:
                    pp[n] = pp[n - 1]
                else:
                    pp[n] = ct * pp[n - 1] - _K[m][n] * pp[n - 2]
                parp = ar * pp[n]
                bpp += _FM[m] * temp2 * parp

            D4 -= 1
            m += 1

    if st == 0.0:
        bp = bpp
    else:
        bp /= st

    bx = -bt * ca - br * sa
    by = bp
    return math.degrees(math.atan2(by, bx))


# --- Public API with a coarse spatial/temporal cache -----------------------
# Variation changes slowly (~0.1°/30 nm), so a 0.25° grid keyed cache keeps the
# heading tape from re-running the harmonic sum every frame while staying well
# under 0.1° of error.

_cache = {}
_CACHE_MAX = 512


def _decimal_year():
    """Current UTC decimal year, clamped to the WMM2025 life span."""
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc)
    start = datetime.datetime(now.year, 1, 1, tzinfo=datetime.timezone.utc)
    end = datetime.datetime(now.year + 1, 1, 1, tzinfo=datetime.timezone.utc)
    frac = (now - start).total_seconds() / (end - start).total_seconds()
    yr = now.year + frac
    # Stay inside the model's validity so the extrapolation never blows up.
    return min(max(yr, _EPOCH), _EPOCH + 5.0 - 1e-6)


def declination(lat, lon, year=None):
    """Magnetic declination at lat/lon (deg, east positive).  Returns 0.0 on
    bad input so callers can treat 'no variation available' as true == mag."""
    if lat is None or lon is None:
        return 0.0
    try:
        yr = year if year is not None else _decimal_year()
        key = (round(lat * 4) / 4.0, round(lon * 4) / 4.0, round(yr * 4) / 4.0)
        val = _cache.get(key)
        if val is None:
            val = _declination_raw(key[0], key[1], key[2])
            if len(_cache) > _CACHE_MAX:
                _cache.clear()
            _cache[key] = val
        return val
    except Exception:
        return 0.0


def true_to_mag(true_deg, lat, lon, year=None):
    """Convert a TRUE bearing/heading to MAGNETIC at lat/lon."""
    if true_deg is None:
        return true_deg
    return (true_deg - declination(lat, lon, year)) % 360.0


def mag_to_true(mag_deg, lat, lon, year=None):
    """Convert a MAGNETIC bearing/heading to TRUE at lat/lon."""
    if mag_deg is None:
        return mag_deg
    return (mag_deg + declination(lat, lon, year)) % 360.0
