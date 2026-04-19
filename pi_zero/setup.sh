#!/usr/bin/env bash
# setup.sh – One-shot install for Pi Zero 2W PFD display (no SVT version)
# Run: sudo bash setup.sh
# Tested on: Raspberry Pi OS Lite (64-bit), Pi Zero 2W

set -e

echo "================================================================"
echo " PFD Display (Pi Zero 2W) – Setup script"
echo " No SVT version — plain horizon + TAWS alerting"
echo "================================================================"
echo ""

# ── 0. Must run as root ──────────────────────────────────────────────────────
if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: Run as root: sudo bash setup.sh"; exit 1
fi

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ZERO_DIR="$REPO_DIR/pi_zero"
SHARED_DIR="$REPO_DIR/shared"
USER_HOME=$(getent passwd "${SUDO_USER:-pi}" | cut -d: -f6)
RUN_USER="${SUDO_USER:-pi}"

echo "[1/8] Updating package lists…"
apt-get update -qq

echo "[2/8] Installing system dependencies…"
apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv \
    python3-pygame \
    python3-numpy \
    libsdl2-dev libsdl2-ttf-dev libsdl2-image-dev \
    fonts-dejavu-core \
    git curl \
    2>/dev/null

echo "[3/8] Verifying Python packages…"
# pygame and numpy are installed via apt (python3-pygame, python3-numpy) above.
# No pip install needed — avoids PEP 668 "externally managed environment" error
# on Bookworm+.
python3 -c "import pygame; import numpy; print(f'  → pygame {pygame.ver}, numpy {numpy.__version__}')"

echo "[4/8] Configuring Waveshare 3.5\" DPI LCD…"
# Waveshare 3.5inch DPI LCD: 640×480, DPI parallel RGB interface, I2C touch
# Requires DT overlays copied to /boot/overlays/ (see Waveshare wiki)
if ! grep -q "waveshare-35dpi" /boot/firmware/config.txt 2>/dev/null; then
    cat >> /boot/firmware/config.txt << 'CFG'
# PFD Display (Pi Zero 2W) – Waveshare 3.5" DPI LCD
# Added by setup.sh
dtoverlay=vc4-kms-v3d
dtoverlay=waveshare-35dpi-3b-4b
dtparam=i2c_arm=on
disable_overscan=1
framebuffer_width=640
framebuffer_height=480
CFG
    echo "  → /boot/firmware/config.txt updated for Waveshare 3.5\" DPI"
    echo "  → IMPORTANT: Copy waveshare DT overlay files to /boot/overlays/"
    echo "    Download from: https://www.waveshare.com/wiki/3.5inch_DPI_LCD"
else
    echo "  → config.txt already configured"
fi

echo "[5/8] Creating data directories…"
mkdir -p "$ZERO_DIR/data/srtm"
mkdir -p "$ZERO_DIR/data/obstacles"
chown -R "$RUN_USER:" "$ZERO_DIR/data"

echo "[6/8] Installing systemd service…"
cat > /etc/systemd/system/pfd.service << SVCEOF
[Unit]
Description=PFD Flight Display (Pi Zero 2W – no SVT)
After=network.target

[Service]
User=$RUN_USER
WorkingDirectory=$ZERO_DIR
Environment="SDL_FBDEV=/dev/fb0"
Environment="SDL_VIDEODRIVER=fbcon"
Environment="DISPLAY="
Environment="PYTHONPATH=$SHARED_DIR"
ExecStart=/usr/bin/python3 $ZERO_DIR/pfd.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable pfd.service
echo "  → pfd.service installed and enabled"

echo "[7/8] WiFi config…"
echo "  → To switch networks use: sudo bash wifi_switch.sh flight|home"

echo "[8/8] Done."

echo ""
echo "================================================================"
echo " Setup complete! (Pi Zero 2W – no SVT)"
echo ""
echo " Next steps:"
echo "   1. Reboot: sudo reboot"
echo "   2. Download terrain tiles (for TAWS alerting, while on home WiFi):"
echo "      bash fetch_sedona_tiles.sh"
echo "   3. Test demo mode:"
echo "      python3 pi_zero/pfd.py --demo --sim"
echo "   4. Connect to Pico W AP and run:"
echo "      python3 pi_zero/pfd.py"
echo ""
echo " The pfd.service will auto-start on next boot."
echo "================================================================"
