# ---------------------------------------------------------------------------
# airdata.py  –  IAS / TAS / density altitude / wind-triangle math
# ---------------------------------------------------------------------------
# Fed by:
#   - SDP31-1500Pa  → differential pressure dp_pa  (pitot − static)
#   - BME280       → static pressure_pa + temperature_c
#   - GPS          → groundspeed (kt) + track (deg true)
#   - AHRS         → heading (deg magnetic / true, see HEADING_IS_TRUE)
#
# Outputs (consumed by main.py and forwarded in the $AHRS JSON):
#   ias_kt       — calibrated/indicated airspeed (no instrument correction yet)
#   tas_kt       — true airspeed (density-corrected)
#   dens_alt_ft  — density altitude in feet
#   wind_dir     — meteorological convention (deg from which the wind blows)
#   wind_kt      — wind speed in knots
#   airdata_ok   — True when SDP31 and BME280 are both delivering fresh data
#
# Constants
# ---------
# ρ₀ = 1.225 kg/m³ at ISA sea level.  IAS is defined against ρ₀ so that the
# aircraft's stall margin reads the same regardless of altitude.
# R_specific_air = 287.05 J/(kg·K)
# Knot = 0.514444 m/s.
#
# IAS math
# --------
# Bernoulli: dp = ½·ρ₀·v²  →  v_ms = sqrt(2·|dp| / ρ₀)
# The sign of dp matters for the slip/reverse-flight cases but we just
# clamp negative dp to zero for the IAS readout — there is no useful
# negative-airspeed indication.  Below ~5 kt the math gets noise-dominated;
# return 0 there so the speed tape doesn't dither during taxi/parking.
#
# TAS math
# --------
# ρ = P / (R·T)  using BME280 static pressure (Pa) and temperature (K).
# TAS = IAS · sqrt(ρ₀ / ρ).
#
# Density altitude
# ----------------
# Inverse of the ISA hypsometric formula on ρ:
#   ρ/ρ₀ = (1 − Lh/T₀)^(g·M/(R·L) − 1)
# Solving for h gives a closed form against ρ (good ±200 ft below 18 kft).
#
# Wind triangle
# -------------
# Air vector  = TAS at heading (true)
# Ground vec  = GS at track (true)
# Wind vec    = ground − air
# wind_dir_to = atan2(east, north) of wind vector  (where wind is going)
# wind_dir    = (wind_dir_to + 180) mod 360       (where wind is from)
# Bench note: with no IAS sensor or no GPS, return None for the wind so the
# display can suppress the ribbon rather than show a noise value.
# ---------------------------------------------------------------------------

import math


RHO_ZERO_KGM3   = 1.225
R_SPEC_AIR      = 287.05      # J / (kg·K)
MS_PER_KNOT     = 0.514444
IAS_DEADBAND_KT = 5.0          # below this read 0 to suppress static noise


def density_kgm3(static_pa: float, temp_c: float) -> float:
    """Air density from ideal-gas law.  static_pa must be absolute (not QNH-
    corrected); BME280's pressure_pa is the right input."""
    t_kelvin = temp_c + 273.15
    if t_kelvin <= 0.0:
        return RHO_ZERO_KGM3
    return static_pa / (R_SPEC_AIR * t_kelvin)


def ias_kt(dp_pa: float) -> float:
    """Indicated airspeed (knots) from differential pressure (Pa).
    Negative dp (reverse flow or sensor at rest with offset drift) reads as 0."""
    if dp_pa <= 0.0:
        return 0.0
    v_ms = math.sqrt(2.0 * dp_pa / RHO_ZERO_KGM3)
    v_kt = v_ms / MS_PER_KNOT
    return 0.0 if v_kt < IAS_DEADBAND_KT else v_kt


def tas_kt(ias: float, static_pa: float, temp_c: float) -> float:
    """True airspeed from IAS and BME280 density.  Reduces to IAS at
    sea-level ISA (ρ = ρ₀)."""
    rho = density_kgm3(static_pa, temp_c)
    if rho <= 0.0:
        return ias
    return ias * math.sqrt(RHO_ZERO_KGM3 / rho)


def density_alt_ft(static_pa: float, temp_c: float) -> float:
    """Density altitude (feet) for the given measured static pressure and
    temperature.  Inverse of ρ = ρ₀·(1−Lh/T₀)^((gM/RL)−1) solved for h, in
    metres, then converted to ft.  Accurate ≤ ±200 ft up to 18 kft."""
    rho = density_kgm3(static_pa, temp_c)
    if rho <= 0.0:
        return 0.0
    ratio = rho / RHO_ZERO_KGM3
    # Exponent (g·M / (R·L)) − 1 ≈ 4.2559;  L = 0.0065 K/m;  T₀ = 288.15 K
    inner = max(1e-9, ratio) ** (1.0 / 4.2559)
    alt_m = (288.15 / 0.0065) * (1.0 - inner)
    return alt_m * 3.28084


def wind_solution(tas: float, heading_deg: float,
                  gs_kt: float, track_deg: float):
    """Return (wind_dir_from_deg, wind_kt) or (None, None) if inputs are unusable.

    Heading is whatever frame the AHRS yaw is reported in — for the WT901
    we currently feed magnetic heading.  In low-magnetic-deviation regions
    that's close enough to true; for transcontinental flight the display
    should pass true heading instead.  The math is identical either way as
    long as track and heading share the same reference frame.
    """
    if tas <= 0.0 or gs_kt < 0.0:
        return None, None
    # Convert to unit vectors with bearing = compass convention (0 = N, CW).
    hd = math.radians(heading_deg)
    tk = math.radians(track_deg)
    air_n   = tas   * math.cos(hd)
    air_e   = tas   * math.sin(hd)
    grnd_n  = gs_kt * math.cos(tk)
    grnd_e  = gs_kt * math.sin(tk)
    w_n = grnd_n - air_n
    w_e = grnd_e - air_e
    speed = math.sqrt(w_n * w_n + w_e * w_e)
    if speed < 1.0:
        # Below ~1 kt the direction is noise; return a clean "calm"
        # reading rather than a randomly rotating arrow on the display.
        return 0.0, 0.0
    dir_to = (math.degrees(math.atan2(w_e, w_n)) + 360.0) % 360.0
    dir_from = (dir_to + 180.0) % 360.0
    return dir_from, speed
