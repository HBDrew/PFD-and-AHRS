#!/usr/bin/env python3
"""
ahrs_capture.py — quick noise/rate probe for the WT901 AHRS over USB.

Reads the Pico's "$AHRS,{json}" serial stream and reports the message rate and
the roll / pitch / yaw scatter (peak-to-peak and standard deviation) over a
capture window. Use it to measure the effect of the WT901 front-end changes
(sample rate + anti-alias bandwidth): a lower roll/pitch std with the airframe
steady means less vibration is leaking into the attitude solution.

The display service holds the serial port, so stop it first:
    sudo systemctl stop pfd
    python3 tools/ahrs_capture.py           # 30 s on the first ACM port
    python3 tools/ahrs_capture.py 20 /dev/ttyACM0
    sudo systemctl restart pfd

USB CDC ignores the baud value, so the 115200 here is nominal — it works
whatever the WT901<->Pico link is running at.
"""

import glob
import json
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


def main():
    dur  = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
    port = sys.argv[2] if len(sys.argv) > 2 else find_port()
    if not port:
        sys.exit("No /dev/ttyACM* or /dev/ttyUSB* found — is the Pico plugged in?")

    print(f"Capturing {dur:.0f}s from {port} … keep the airframe as steady as you can.")
    ser = serial.Serial(port, 115200, timeout=1)

    roll, pitch, yaw = [], [], []
    n = 0
    t0 = time.time()
    while time.time() - t0 < dur:
        line = ser.readline().decode(errors="ignore").strip()
        if not line.startswith("$AHRS,"):
            continue
        try:
            d = json.loads(line[6:])
        except Exception:
            continue
        roll.append(float(d.get("roll", 0.0)))
        pitch.append(float(d.get("pitch", 0.0)))
        yaw.append(float(d.get("yaw", 0.0)))
        n += 1

    elapsed = time.time() - t0
    if n < 2:
        sys.exit(f"Only {n} $AHRS messages in {elapsed:.0f}s — is the display still "
                 f"holding the port? (sudo systemctl stop pfd)")

    def stats(name, xs):
        return (f"{name:5s} pk-pk={max(xs) - min(xs):6.2f}°  "
                f"std={statistics.pstdev(xs):5.2f}°  mean={statistics.fmean(xs):7.2f}°")

    print(f"\n$AHRS messages: {n}  ({n / elapsed:.1f}/s over {elapsed:.0f}s)")
    print(stats("roll",  roll))
    print(stats("pitch", pitch))
    print(stats("yaw",   yaw))
    print("\nLower std with the airframe steady = less vibration in the solution.")


if __name__ == "__main__":
    main()
