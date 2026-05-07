#!/usr/bin/env bash
# capture_pi4_previews.sh — render every doc / readme preview PNG using the
# offline renderer (pi4/render_pfd_offline.py), which uses SDL_VIDEODRIVER=
# dummy + an offscreen GL context so the captured PNGs include the full 3D
# SVT terrain.  pfd.py's --screenshots mode skips GL on purpose (it reads
# from the pygame surface, which in shared-GL mode contains only the 2D
# overlay), so the output had no terrain.
#
# Output: GL flight scenes go into pi4/previews/pfd_gl/, setup / modal PNGs
# go into pi4/previews/.  Pass an argument to override the GL output dir;
# the setup/modal output dir is then its parent.
#
# Captures the existing scenes (cruise, climb turn, approach, runway final,
# numpads, keyboard, setup screens, terrain / obstacle / airport screens)
# PLUS the new V4.5–V5.0 modal / feature scenes:
#   preview_direct_to_keyboard.png    keyboard + KSEZ placeholder + NEAREST
#   preview_unknown_waypoint.png      UNKNOWN WAYPOINT error state
#   preview_nav_confirm.png           Activate Direct to KSEZ? modal
#   preview_compass_cal.png           cardinal-walk wizard, step 2 (EAST)
#   preview_compass_cal_done.png      wizard with all four Δ values
#   preview_agl_readout.png           cruise frame with AGL box
#   preview_setup_ahrs_orient_*.png   one per ORIENTATION segment

set -e

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PI4_DIR="$REPO_DIR/pi4"
OUT_DIR="${1:-$PI4_DIR/previews/pfd_gl}"

if [ ! -f "$PI4_DIR/render_pfd_offline.py" ]; then
    echo "ERROR: $PI4_DIR/render_pfd_offline.py not found"
    exit 1
fi

# If pfd.service is running it owns the display — but the offline renderer
# uses SDL_VIDEODRIVER=dummy and an offscreen GL context, so they can
# coexist.  We don't stop the service.

mkdir -p "$OUT_DIR"
mkdir -p "$(dirname "$OUT_DIR")"
echo "Rendering previews with full SVT → $OUT_DIR"
echo "(setup screens + modals → $(dirname "$OUT_DIR"))"
cd "$PI4_DIR"
PYTHONPATH="$REPO_DIR/shared" python3 render_pfd_offline.py "$OUT_DIR"

echo ""
echo "Done.  Capture inventory:"
echo ""
echo "GL flight scenes ($OUT_DIR):"
ls -1 "$OUT_DIR" 2>/dev/null | sort
echo ""
echo "Setup / modals / numpads ($(dirname "$OUT_DIR")):"
ls -1 "$(dirname "$OUT_DIR")" 2>/dev/null | grep -E '^preview_' | sort
