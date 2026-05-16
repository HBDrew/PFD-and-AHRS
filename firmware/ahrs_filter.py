# ---------------------------------------------------------------------------
# ahrs_filter.py  –  Mahony attitude filter with accel gating + mag aiding
# ---------------------------------------------------------------------------
# Reference: R. Mahony, T. Hamel, J.-M. Pflimlin,
# "Nonlinear Complementary Filters on the Special Orthogonal Group",
# IEEE Trans. Automatic Control, 2008.
#
# Quaternion state q = (q0, q1, q2, q3) represents the rotation from the
# sensor frame to the local "world" frame. Convention follows the WT901:
# stationary level reads accel = (0, 0, +1 g) — i.e. +Z of the world frame
# is along measured-gravity (specific-force-up). Mag-derived yaw and Euler
# extraction are consistent with this convention.
#
# The filter is sensor-frame agnostic: callers are responsible for any
# orientation remapping of the OUTPUT Euler angles (the existing main.py
# pipeline does this for the WT901's PKT_ANGLE; the same remap applies to
# our Euler output unchanged).
#
# Tuning knobs (see Mahony class __init__):
#   kp_acc   – accel proportional gain (rad/s per unit cross-product error).
#              Higher = faster correction of attitude toward gravity, but
#              also faster contamination from linear acceleration.
#   ki_acc   – accel integral gain. Estimates and removes gyro bias.
#   kp_mag   – mag proportional gain. Drives yaw error correction.
#   accel_gate_g – width of the magnitude window around 1 g inside which
#                  accel correction is at full weight. Outside the window
#                  weight scales linearly to zero, so high-g maneuvers and
#                  turbulence contribute almost nothing to attitude error.
# ---------------------------------------------------------------------------

import math


class Mahony:
    def __init__(self,
                 kp_acc=1.0,
                 ki_acc=0.01,
                 kp_mag=0.5,
                 accel_gate_g=0.20):
        self.kp_acc = kp_acc
        self.ki_acc = ki_acc
        self.kp_mag = kp_mag
        self.accel_gate_g = accel_gate_g

        # Quaternion: sensor-to-world (initially identity)
        self.q0 = 1.0
        self.q1 = 0.0
        self.q2 = 0.0
        self.q3 = 0.0

        # Estimated gyro bias (rad/s) — integral term of the accel feedback
        self.bx = 0.0
        self.by = 0.0
        self.bz = 0.0

        # Diagnostic: last-applied accel weight (0..1) and the magnitude
        # of the post-correction accel after centripetal subtraction
        self.last_accel_weight = 0.0
        self.last_mag_used     = False

    # ----------------------------------------------------------------------
    def update(self, gx, gy, gz, ax, ay, az,
               mx=None, my=None, mz=None, dt=0.02):
        """
        Single filter step.
          gx, gy, gz : gyro rate (rad/s) in sensor frame
          ax, ay, az : accel (g) in sensor frame; caller should already have
                       subtracted centripetal acceleration if velocity is
                       available
          mx, my, mz : optional magnetometer (any units) in sensor frame.
                       Pass None to skip mag correction (yaw drifts on gyro).
          dt         : integration timestep (s)
        """
        q0, q1, q2, q3 = self.q0, self.q1, self.q2, self.q3

        # ── Accel: cross product between measured & predicted gravity ──
        ex_a = ey_a = ez_a = 0.0
        a_mag = math.sqrt(ax*ax + ay*ay + az*az)
        a_w = 0.0
        if a_mag > 1e-6:
            # Magnitude gate: full weight at |a| = 1 g, linearly to zero at
            # |a| - 1 g| = accel_gate_g. Outside, accel is ignored entirely.
            err_g = abs(a_mag - 1.0)
            if err_g < self.accel_gate_g:
                a_w = 1.0 - err_g / self.accel_gate_g
            # Predicted gravity in body frame from quaternion (R^T @ z_world)
            vx = 2.0 * (q1*q3 - q0*q2)
            vy = 2.0 * (q0*q1 + q2*q3)
            vz = q0*q0 - q1*q1 - q2*q2 + q3*q3
            ax_n = ax / a_mag
            ay_n = ay / a_mag
            az_n = az / a_mag
            ex_a = (ay_n*vz - az_n*vy)
            ey_a = (az_n*vx - ax_n*vz)
            ez_a = (ax_n*vy - ay_n*vx)
        self.last_accel_weight = a_w

        # ── Mag: cross product between measured & predicted north ──
        ex_m = ey_m = ez_m = 0.0
        m_w = 0.0
        if mx is not None and my is not None and mz is not None:
            m_mag = math.sqrt(mx*mx + my*my + mz*mz)
            if m_mag > 1e-6:
                m_w = 1.0
                mx_n = mx / m_mag
                my_n = my / m_mag
                mz_n = mz / m_mag
                # Rotate measured mag into world (h = R @ m_body)
                hx = ((1.0 - 2.0*(q2*q2 + q3*q3))*mx_n
                      + 2.0*(q1*q2 - q0*q3)*my_n
                      + 2.0*(q1*q3 + q0*q2)*mz_n)
                hy = (2.0*(q1*q2 + q0*q3)*mx_n
                      + (1.0 - 2.0*(q1*q1 + q3*q3))*my_n
                      + 2.0*(q2*q3 - q0*q1)*mz_n)
                hz = (2.0*(q1*q3 - q0*q2)*mx_n
                      + 2.0*(q2*q3 + q0*q1)*my_n
                      + (1.0 - 2.0*(q1*q1 + q2*q2))*mz_n)
                # Reference: world north = (bxy, 0, bz), built from measurement
                bxy = math.sqrt(hx*hx + hy*hy)
                bz_w = hz
                # Rotate reference back to body (w = R^T @ ref)
                wx = ((1.0 - 2.0*(q2*q2 + q3*q3))*bxy
                      + 2.0*(q1*q3 - q0*q2)*bz_w)
                wy = (2.0*(q1*q2 - q0*q3)*bxy
                      + 2.0*(q2*q3 + q0*q1)*bz_w)
                wz = (2.0*(q1*q3 + q0*q2)*bxy
                      + (1.0 - 2.0*(q1*q1 + q2*q2))*bz_w)
                ex_m = (my_n*wz - mz_n*wy)
                ey_m = (mz_n*wx - mx_n*wz)
                ez_m = (mx_n*wy - my_n*wx)
        self.last_mag_used = (m_w > 0.0)

        # ── Weighted error and gyro-bias integration ──
        ex = self.kp_acc * a_w * ex_a + self.kp_mag * m_w * ex_m
        ey = self.kp_acc * a_w * ey_a + self.kp_mag * m_w * ey_m
        ez = self.kp_acc * a_w * ez_a + self.kp_mag * m_w * ez_m

        if a_w > 0.0:
            # Gyro-bias estimator: only consume accel-derived error. Mag
            # error is fed into the proportional path but kept out of the
            # integrator — a residual mag direction error (soft iron,
            # magnetic-deviation table mismatch) would otherwise be
            # interpreted as a gyro bias and wind up over time.
            self.bx += self.ki_acc * a_w * ex_a * dt
            self.by += self.ki_acc * a_w * ey_a * dt
            self.bz += self.ki_acc * a_w * ez_a * dt

        # Apply correction to gyro and subtract estimated bias
        gx_c = gx + ex - self.bx
        gy_c = gy + ey - self.by
        gz_c = gz + ez - self.bz

        # Quaternion derivative q_dot = 0.5 * q ⊗ (0, gx, gy, gz)
        qd0 = 0.5 * (-q1*gx_c - q2*gy_c - q3*gz_c)
        qd1 = 0.5 * ( q0*gx_c + q2*gz_c - q3*gy_c)
        qd2 = 0.5 * ( q0*gy_c - q1*gz_c + q3*gx_c)
        qd3 = 0.5 * ( q0*gz_c + q1*gy_c - q2*gx_c)

        q0 += qd0 * dt
        q1 += qd1 * dt
        q2 += qd2 * dt
        q3 += qd3 * dt

        n = math.sqrt(q0*q0 + q1*q1 + q2*q2 + q3*q3)
        if n > 1e-9:
            self.q0 = q0 / n
            self.q1 = q1 / n
            self.q2 = q2 / n
            self.q3 = q3 / n

    # ----------------------------------------------------------------------
    def euler_deg(self):
        """Return (roll, pitch, yaw) in degrees from the current quaternion.
        Tait-Bryan ZYX (yaw-pitch-roll, aerospace convention). Yaw is wrapped
        to [-180, 180); caller converts to compass [0, 360) if needed."""
        q0, q1, q2, q3 = self.q0, self.q1, self.q2, self.q3
        # Roll about sensor X
        sinr = 2.0 * (q0*q1 + q2*q3)
        cosr = 1.0 - 2.0 * (q1*q1 + q2*q2)
        roll = math.atan2(sinr, cosr)
        # Pitch about sensor Y (clamp arg for numeric safety near ±90°)
        sinp = 2.0 * (q0*q2 - q3*q1)
        if sinp > 1.0:
            sinp = 1.0
        elif sinp < -1.0:
            sinp = -1.0
        pitch = math.asin(sinp)
        # Yaw about sensor Z
        siny = 2.0 * (q0*q3 + q1*q2)
        cosy = 1.0 - 2.0 * (q2*q2 + q3*q3)
        yaw = math.atan2(siny, cosy)
        return (math.degrees(roll), math.degrees(pitch), math.degrees(yaw))

    # ----------------------------------------------------------------------
    def seed_from_euler_deg(self, roll_deg, pitch_deg, yaw_deg):
        """Seed the quaternion from a known initial attitude.
        Useful at start-up: feed the WT901's PKT_ANGLE Euler once so the
        filter doesn't have to coast from identity for several seconds while
        accel-correction tugs it toward gravity."""
        r = math.radians(roll_deg)  * 0.5
        p = math.radians(pitch_deg) * 0.5
        y = math.radians(yaw_deg)   * 0.5
        cr, sr = math.cos(r), math.sin(r)
        cp, sp = math.cos(p), math.sin(p)
        cy, sy = math.cos(y), math.sin(y)
        self.q0 = cr*cp*cy + sr*sp*sy
        self.q1 = sr*cp*cy - cr*sp*sy
        self.q2 = cr*sp*cy + sr*cp*sy
        self.q3 = cr*cp*sy - sr*sp*cy

    # ----------------------------------------------------------------------
    def nudge_yaw_toward_deg(self, target_yaw_deg, alpha):
        """Pull the filter's yaw toward an external reference (e.g. GPS
        track) by fractional amount alpha (0..1) per call. Roll/pitch are
        preserved. Use for slow long-term yaw slaving — call at low rate
        (e.g. once per second) with small alpha (e.g. 0.02) so the filter's
        own gyro/mag dynamics still dominate short-term."""
        roll_deg, pitch_deg, yaw_deg = self.euler_deg()
        err = ((target_yaw_deg - yaw_deg + 540.0) % 360.0) - 180.0
        new_yaw = yaw_deg + alpha * err
        self.seed_from_euler_deg(roll_deg, pitch_deg, new_yaw)
