#!/usr/bin/env python3
"""
ahrs_debug_capture.py — decode the Pico's $AHRSDBG stream to find out why the
Mahony filter isn't holding a level attitude.

Requires AHRS_DEBUG_PRINT = True in firmware/config.py (re-flash config.py).
The display holds the serial port, so stop it first:

    sudo systemctl stop pfd
    python3 tools/ahrs_debug_capture.py 20      # 20 s capture
    sudo systemctl restart pfd

$AHRSDBG line format (from firmware/main.py):
  $AHRSDBG,q=..,..,..,..,sf_rpy=..,..,..,bdy_rpy=..,..,..,acc=..,..,..,
           gyr=..,..,..,mag=..,..,..,mag_err=..,..,..,acc_w=..,mag_w=..,a_c_g=..

What the readout tells us (with the airframe steady):
  |accel|  should be ~1.00 g.  Far from 1.0 => accel range/scaling is wrong,
           so every sample falls outside the |a|=1g gate and the filter never
           corrects to gravity.
  acc_w    is the accel correction weight (0..1).  If it sits near 0, the accel
           is being gated out (bad magnitude or too much vibration) and the
           filter is coasting on the gyro — which is why pitch/roll wander.
  |gyro|   deg/s the filter sees while "steady".  Large => gyro noise / a wrong
           gyro range, which drives drift.
"""

import glob
import math
import statistics
import sys
import time

try:
    import serial  # pyserial
except ImportError:
    sys.exit("pyserial missing:  python3 -m pip install --break-system-packages pyserial")


def find_port():
    for pat in ("/dev/ttyACM*", "/dev/ttyUSB*"):
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[0]
    return None


def parse_dbg(line):
    """Return {key: [floats]} from one $AHRSDBG line, or None."""
    if not line.startswith("$AHRSDBG,"):
        return None
    out, key = {}, None
    for tok in line[len("$AHRSDBG,"):].split(","):
        tok = tok.strip()
        if not tok:
            continue
        if "=" in tok:
            key, _, val = tok.partition("=")
            key = key.strip()
            out[key] = []
            tok = val
        if key is None:
            continue
        try:
            out[key].append(float(tok))
        except ValueError:
            pass
    return out


def summarize(name, xs, unit=""):
    return (f"{name:9s} n={len(xs):4d}  mean={statistics.fmean(xs):8.3f}{unit}  "
            f"min={min(xs):8.3f}  max={max(xs):8.3f}  std={statistics.pstdev(xs):7.3f}")


def main():
    dur  = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
    port = sys.argv[2] if len(sys.argv) > 2 else find_port()
    if not port:
        sys.exit("No /dev/ttyACM* or /dev/ttyUSB* found — is the Pico plugged in?")

    print(f"Capturing $AHRSDBG for {dur:.0f}s from {port} … keep the airframe steady.")
    ser = serial.Serial(port, 115200, timeout=1)

    amag, accw, gmag, magw, acg = [], [], [], [], []
    bpitch, broll = [], []
    ax_l, ay_l, az_l = [], [], []
    sfp, sfr = [], []
    n = 0
    t0 = time.time()
    while time.time() - t0 < dur:
        line = ser.readline().decode(errors="ignore").strip()
        d = parse_dbg(line)
        if not d:
            continue
        acc = d.get("acc", [])
        gyr = d.get("gyr", [])
        bdy = d.get("bdy_rpy", [])
        sf  = d.get("sf_rpy", [])
        if len(acc) == 3:
            amag.append(math.sqrt(sum(v * v for v in acc)))
            ax_l.append(acc[0]); ay_l.append(acc[1]); az_l.append(acc[2])
        if len(sf) == 3:
            sfr.append(sf[0]); sfp.append(sf[1])
        if len(gyr) == 3:
            gmag.append(math.sqrt(sum(v * v for v in gyr)))
        if d.get("acc_w"):
            accw.append(d["acc_w"][0])
        if d.get("mag_w"):
            magw.append(d["mag_w"][0])
        if d.get("a_c_g"):
            acg.append(d["a_c_g"][0])
        if len(bdy) == 3:
            broll.append(bdy[0])
            bpitch.append(bdy[1])
        n += 1

    if n < 2:
        sys.exit(f"Only {n} $AHRSDBG lines. Is AHRS_DEBUG_PRINT=True flashed, and "
                 f"is the display stopped? (sudo systemctl stop pfd)")

    print(f"\n$AHRSDBG lines: {n} over {time.time() - t0:.0f}s\n")
    if ax_l:
        mx, my, mz = statistics.fmean(ax_l), statistics.fmean(ay_l), statistics.fmean(az_l)
        print(f"raw accel mean (g):  ax={mx:+.3f}  ay={my:+.3f}  az={mz:+.3f}")
        print(f"   -> gravity is {math.degrees(math.acos(max(-1,min(1,abs(mz)/max(1e-6,math.sqrt(mx*mx+my*my+mz*mz)))))):.1f}° "
              f"off the sensor's Z axis (0° = unit truly flat/level)")
    if sfp: print(summarize("sensor pitch", sfp, "°"))
    if sfr: print(summarize("sensor roll",  sfr, "°"))
    # Convergence trend: first third vs last third mean.  A shrinking magnitude
    # means the filter is still pulling toward level (raise gain / wait); a flat
    # non-zero value means it's stuck at a bias-held offset.
    if len(sfp) >= 9:
        k = len(sfp) // 3
        def _trend(name, xs):
            a = statistics.fmean(xs[:k]); b = statistics.fmean(xs[-k:])
            print(f"   {name} trend: first3rd={a:+.1f}°  ->  last3rd={b:+.1f}°  "
                  f"({'converging' if abs(b) < abs(a) - 1 else 'stuck' if abs(abs(b)-abs(a)) <= 1 else 'diverging'})")
        _trend("pitch", sfp); _trend("roll", sfr)
    if amag: print(summarize("|accel|",  amag, " g"))
    if accw:
        gated = sum(1 for w in accw if w < 0.05) / len(accw) * 100.0
        print(summarize("acc_w",    accw))
        print(f"          -> accel gated out (w<0.05) {gated:.0f}% of the time")
    if gmag: print(summarize("|gyro|",   gmag, " dps"))
    if acg:  print(summarize("a_c (centri)", acg, " g"))
    if magw: print(summarize("mag_w",    magw))
    if broll:  print(summarize("bdy roll",  broll,  "°"))
    if bpitch: print(summarize("bdy pitch", bpitch, "°"))

    print("\nExpect (steady): |accel|~1.00 g, acc_w>0 most of the time, |gyro| small.")


if __name__ == "__main__":
    main()
