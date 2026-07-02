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

    amag, accw, gmag, magw = [], [], [], []
    bpitch, broll = [], []
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
        if len(acc) == 3:
            amag.append(math.sqrt(sum(v * v for v in acc)))
        if len(gyr) == 3:
            gmag.append(math.sqrt(sum(v * v for v in gyr)))
        if d.get("acc_w"):
            accw.append(d["acc_w"][0])
        if d.get("mag_w"):
            magw.append(d["mag_w"][0])
        if len(bdy) == 3:
            broll.append(bdy[0])
            bpitch.append(bdy[1])
        n += 1

    if n < 2:
        sys.exit(f"Only {n} $AHRSDBG lines. Is AHRS_DEBUG_PRINT=True flashed, and "
                 f"is the display stopped? (sudo systemctl stop pfd)")

    print(f"\n$AHRSDBG lines: {n} over {time.time() - t0:.0f}s\n")
    if amag: print(summarize("|accel|",  amag, " g"))
    if accw:
        gated = sum(1 for w in accw if w < 0.05) / len(accw) * 100.0
        print(summarize("acc_w",    accw))
        print(f"          -> accel gated out (w<0.05) {gated:.0f}% of the time")
    if gmag: print(summarize("|gyro|",   gmag, " dps"))
    if magw: print(summarize("mag_w",    magw))
    if broll:  print(summarize("bdy roll",  broll,  "°"))
    if bpitch: print(summarize("bdy pitch", bpitch, "°"))

    print("\nExpect (steady): |accel|~1.00 g, acc_w>0 most of the time, |gyro| small.")


if __name__ == "__main__":
    main()
