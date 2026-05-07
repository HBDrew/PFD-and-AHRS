#!/usr/bin/env bash
# capture_pi4_previews.sh — render every doc / readme preview PNG by driving
# pfd.py through its built-in `--screenshots DIR` batch path.
#
# Run on the Pi 4 with the SDL display free (i.e. systemd service stopped or
# from a desktop session with the display attached).  Output goes to the
# pi4/previews/ directory by default; pass an argument to override.
#
# Captures the existing scenes (cruise, climb turn, approach, GPS-TRK,
# numpads, keyboard, setup screens, terrain / obstacle / airport screens,
# VR cascade, terrain alerts) PLUS the new ones added during the V4.5–V5.0
# round:
#   preview_direct_to_keyboard.png    keyboard + KSEZ placeholder + NEAREST
#   preview_unknown_waypoint.png      UNKNOWN WAYPOINT error state
#   preview_nav_confirm.png           Activate Direct to KSEZ? modal
#   preview_compass_cal.png           cardinal-walk wizard, step 2 (EAST)
#   preview_compass_cal_done.png      wizard with all four Δ values
#   preview_agl_readout.png           cruise frame with AGL box
#   preview_setup_ahrs_orient_*.png   one per ORIENTATION segment

set -e

if [ "$(id -u)" -eq 0 ]; then
    echo "WARNING: don't run as root — pygame/SDL needs the user's session"
fi

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PI4_DIR="$REPO_DIR/pi4"
OUT_DIR="${1:-$PI4_DIR/previews}"

if [ ! -f "$PI4_DIR/pfd.py" ]; then
    echo "ERROR: $PI4_DIR/pfd.py not found"
    exit 1
fi

# If pfd.service is running it owns the display — stop it first
if systemctl is-active --quiet pfd.service 2>/dev/null; then
    echo "(pfd.service is running — stopping it for the capture session)"
    sudo systemctl stop pfd.service
    RESTART_PFD=1
else
    RESTART_PFD=0
fi

mkdir -p "$OUT_DIR"
echo "Rendering previews → $OUT_DIR"
cd "$PI4_DIR"
PYTHONPATH="$REPO_DIR/shared" python3 pfd.py --screenshots "$OUT_DIR"

if [ "$RESTART_PFD" = "1" ]; then
    echo "Restarting pfd.service…"
    sudo systemctl start pfd.service
fi

echo ""
echo "Done.  Capture inventory:"
ls -1 "$OUT_DIR" | sort
