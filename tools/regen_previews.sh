#!/usr/bin/env bash
# regen_previews.sh — regenerate every PNG referenced from the user
# manuals so the docs match the current code.
#
# Two halves:
#
#   1. pi4 previews — delegate to the existing capture_pi4_previews.sh,
#      which spins up the offline GL renderer (SDL_VIDEODRIVER=dummy +
#      offscreen EGL context) so the captured PNGs include the full 3D
#      SVT terrain.  Must be run on a pi4 (needs the V3D driver).
#
#   2. piZ previews — pi_zero/pfd.py's `--screenshots DIR` batch mode is
#      self-contained pygame 2D and works on any host with libsdl2 +
#      python3-pygame installed.  Output goes to pi_zero/previews/ in
#      the repo.  Run on a piZ for fidelity to the actual panel, but a
#      pi4 (or even a desktop dev machine) will produce visually
#      identical PNGs since the piZ render path is pure pygame.
#
# Usage:
#   ./tools/regen_previews.sh pi4    # pi4 PFD scenes (GL terrain)
#   ./tools/regen_previews.sh piz    # piZ PFD scenes (no SVT)
#   ./tools/regen_previews.sh all    # whichever can run on this host
#   ./tools/regen_previews.sh        # auto-detect from /proc/cpuinfo
#
# All paths are relative to the repo root; the script cd's there itself.

set -e

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="${1:-auto}"

# Auto-detect host model when called with no arg.  /proc/cpuinfo's
# "Model" line is the cleanest signal — "Raspberry Pi 4 ..." vs
# "Raspberry Pi Zero 2 W ...".
if [ "$TARGET" = "auto" ]; then
    model="$(grep -i '^Model' /proc/cpuinfo 2>/dev/null | head -1)"
    if echo "$model" | grep -qi 'Pi 4'; then
        TARGET=pi4
    elif echo "$model" | grep -qi 'Zero'; then
        TARGET=piz
    else
        # Not running on either — default to piZ which works on a desktop
        TARGET=piz
    fi
    echo "(auto-detected target: $TARGET — host model: ${model#Model*: })"
fi

regen_pi4() {
    echo "================================================================"
    echo " Regenerating pi4 previews (with GL SVT terrain)"
    echo "================================================================"
    # Refuse to run on a piZ — the offline GL renderer needs the V3D
    # driver, which doesn't exist on the Pi Zero 2W.  Failing here with
    # a clear message beats letting capture_pi4_previews.sh die ten
    # lines into the EGL setup.
    local model
    model="$(grep -i '^Model' /proc/cpuinfo 2>/dev/null | head -1 | sed 's/^Model.*: //')"
    if echo "$model" | grep -qi 'Zero'; then
        echo "ERROR: pi4 preview regen needs a Pi 4 (V3D / EGL).  This host is: $model"
        echo "       Run this on the pi4 instead.  The piZ-side previews are"
        echo "       independent — regen them with: ./tools/regen_previews.sh piz"
        return 1
    fi
    if [ ! -f "$REPO_DIR/tools/capture_pi4_previews.sh" ]; then
        echo "ERROR: tools/capture_pi4_previews.sh missing — can't regenerate pi4 previews."
        return 1
    fi
    bash "$REPO_DIR/tools/capture_pi4_previews.sh"
}

regen_piz() {
    echo "================================================================"
    echo " Regenerating piZ previews (pygame 2D)"
    echo "================================================================"
    # The live pfd.service on a piZ holds the framebuffer; stop it so the
    # offscreen capture doesn't fight for SDL resources.  Restored at end.
    RESTART_PIZ=0
    if systemctl is-active --quiet pfd.service 2>/dev/null; then
        echo "(stopping pfd.service for the capture session…)"
        sudo systemctl stop pfd.service
        RESTART_PIZ=1
    fi
    trap '[ "$RESTART_PIZ" = "1" ] && sudo systemctl start pfd.service' RETURN

    mkdir -p "$REPO_DIR/pi_zero/previews"
    cd "$REPO_DIR/pi_zero"
    SDL_VIDEODRIVER=dummy \
    PYTHONPATH="$REPO_DIR/shared" \
        python3 pfd.py --screenshots "$REPO_DIR/pi_zero/previews"

    echo ""
    echo "Done.  piZ preview inventory:"
    ls -1 "$REPO_DIR/pi_zero/previews/" | grep -E '^preview_' | sort
}

case "$TARGET" in
    pi4)  regen_pi4 ;;
    piz)  regen_piz ;;
    all)
        # Try both; don't fail the whole run if one half isn't possible
        # on this host (e.g. no GL context on a desktop, so pi4 will
        # fail there — piZ still proceeds).
        regen_pi4 || echo "(pi4 regen skipped or failed; continuing)"
        regen_piz || echo "(piZ regen skipped or failed)"
        ;;
    *)
        echo "Usage: $0 [pi4|piz|all]"
        exit 1
        ;;
esac
