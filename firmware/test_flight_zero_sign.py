"""
test_flight_zero_sign.py – lock the sign of the display's LEVEL / flight-zero
button against the firmware's alignment + Euler-remap math.

The LEVEL button (pi4/pfd.py & pi_zero/pfd.py `_flight_zero`) re-zeros the AHRS
by folding the current attitude into the input-side axis alignment
(`pitch_align` / `roll_align`, applied to the RAW gyro/accel/mag before the
Mahony filter).  It uses:

    sign     = -1 if mounting == 'normal' else +1
    align_new = clamp(align_old + sign * body_attitude,  ±10°)

A wrong sign would DOUBLE the error instead of removing it, so this test
replicates `firmware/main.py`'s pure helpers (`_rot_about_axis`,
`_ac_axes_in_sensor`, `_apply_axis_align`, `_apply_remap`) and the filter's
steady-state accel→Euler mapping, then proves the formula drives an arbitrary
mounting tilt to level for every connector orientation × mounting.

If `_apply_remap` / `_ac_axes_in_sensor` in main.py ever change, update the
copies here AND re-confirm the sign rule in `_flight_zero`.

Run:  python3 firmware/test_flight_zero_sign.py
"""

import math

_passed = 0


def check(cond, msg):
    global _passed
    assert cond, f"FAIL: {msg}"
    _passed += 1


# ── faithful copies of firmware/main.py pure helpers ─────────────────────────
def _rot_about_axis(vec, axis, theta_rad):
    if theta_rad == 0.0:
        return vec
    s = math.sin(theta_rad)
    c = math.cos(theta_rad)
    kx, ky, kz = axis
    vx, vy, vz = vec
    cx = ky * vz - kz * vy
    cy = kz * vx - kx * vz
    cz = kx * vy - ky * vx
    d = kx * vx + ky * vy + kz * vz
    return (vx * c + cx * s + kx * d * (1.0 - c),
            vy * c + cy * s + ky * d * (1.0 - c),
            vz * c + cz * s + kz * d * (1.0 - c))


def _ac_axes_in_sensor(conn):
    if conn == 'forward':
        return ((0.0, 1.0, 0.0), (1.0, 0.0, 0.0))
    if conn == 'left':
        return ((1.0, 0.0, 0.0), (0.0, -1.0, 0.0))
    if conn == 'aft':
        return ((0.0, -1.0, 0.0), (-1.0, 0.0, 0.0))
    return ((-1.0, 0.0, 0.0), (0.0, 1.0, 0.0))   # right


def _apply_axis_align_accel(a, pitch_align_deg, roll_align_deg, conn):
    if pitch_align_deg == 0.0 and roll_align_deg == 0.0:
        return a
    roll_axis, pitch_axis = _ac_axes_in_sensor(conn)
    pa = -math.radians(pitch_align_deg)
    ra = -math.radians(roll_align_deg)
    a = _rot_about_axis(a, pitch_axis, pa)
    a = _rot_about_axis(a, roll_axis, ra)
    return a


def _apply_remap(roll, pitch, conn, mounting):
    _r = -roll
    _p = pitch
    if conn == 'forward':
        _p, _r = -_r, _p
    elif conn == 'left':
        _p, _r = -_p, -_r
    elif conn == 'aft':
        _p, _r = _r, -_p
    if mounting == 'inverted':
        _p = -_p
        _r = -_r
    return _r, _p


# ── steady-state model of the display's reported body attitude ───────────────
def _reported_body(raw_accel, pa, ra, conn, mounting):
    """Body (roll, pitch) in degrees the display would show, given the raw
    sensor gravity vector at level, the current align, connector, mounting.
    The Mahony converges so predicted gravity == aligned-accel direction;
    for level=(0,0,1) that is pitch_s = -asin(gx), roll_s = asin(gy)."""
    ax, ay, az = _apply_axis_align_accel(raw_accel, pa, ra, conn)
    n = math.sqrt(ax * ax + ay * ay + az * az)
    ax, ay, az = ax / n, ay / n, az / n
    pitch_s = math.degrees(-math.asin(max(-1.0, min(1.0, ax))))
    roll_s = math.degrees(math.asin(max(-1.0, min(1.0, ay))))
    return _apply_remap(roll_s, pitch_s, conn, mounting)


def _mounting_raw(mrx_deg, mry_deg):
    """Raw sensor gravity when the aircraft is level but the sensor is tilted
    by an arbitrary mounting error (rotate ideal (0,0,1) about sensor X, Y)."""
    a = (0.0, 0.0, 1.0)
    a = _rot_about_axis(a, (1.0, 0.0, 0.0), math.radians(mrx_deg))
    a = _rot_about_axis(a, (0.0, 1.0, 0.0), math.radians(mry_deg))
    return a


def _flight_zero_formula(cur, body, mounting):
    """The exact rule used by _flight_zero() in the display code (±15° cap)."""
    sign = -1.0 if mounting == 'normal' else 1.0
    return max(-15.0, min(15.0, round(cur + sign * body, 1)))


def test_level_converges_all_orientations():
    # Includes tilts near the ±15° cap to confirm the extra headroom is usable.
    tilts = [(2, 3), (-4, 1.5), (5, -2), (-1, -3), (0, 4), (3, 0),
             (6, -6), (-5, 5), (1.2, -0.7), (12, -11), (-13, 8)]
    for conn in ('right', 'forward', 'left', 'aft'):
        for mounting in ('normal', 'inverted'):
            for mrx, mry in tilts:
                raw = _mounting_raw(mrx, mry)
                pa = ra = 0.0
                # A couple of taps (one is ~exact at small angles).
                for _ in range(3):
                    br, bp = _reported_body(raw, pa, ra, conn, mounting)
                    pa = _flight_zero_formula(pa, bp, mounting)
                    ra = _flight_zero_formula(ra, br, mounting)
                br, bp = _reported_body(raw, pa, ra, conn, mounting)
                check(max(abs(br), abs(bp)) < 0.05,
                      f"{conn}/{mounting} tilt=({mrx},{mry}) "
                      f"residual=({br:+.3f},{bp:+.3f})")


def test_one_tap_removes_most_of_error():
    # A single tap should not make things worse and should knock the error
    # down to a small residual (pilot taps once while straight-and-level).
    raw = _mounting_raw(3.0, -2.0)
    for conn in ('right', 'forward', 'left', 'aft'):
        for mounting in ('normal', 'inverted'):
            br0, bp0 = _reported_body(raw, 0.0, 0.0, conn, mounting)
            pa = _flight_zero_formula(0.0, bp0, mounting)
            ra = _flight_zero_formula(0.0, br0, mounting)
            br1, bp1 = _reported_body(raw, pa, ra, conn, mounting)
            check(max(abs(br1), abs(bp1)) < max(abs(br0), abs(bp0)),
                  f"{conn}/{mounting}: one tap reduced error "
                  f"({br0:+.2f},{bp0:+.2f})→({br1:+.2f},{bp1:+.2f})")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"FLIGHT-ZERO SIGN TESTS PASSED ({_passed} checks, {len(tests)} cases)")


if __name__ == "__main__":
    main()
