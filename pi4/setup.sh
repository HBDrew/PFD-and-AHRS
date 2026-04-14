#!/usr/bin/env bash
# setup.sh – One-shot install for Raspberry Pi 4 PFD display (full SVT version)
# Run: sudo bash setup.sh
# Tested on: Raspberry Pi OS Lite (64-bit), Pi 4

set -e

echo "================================================================"
echo " PFD Display (Pi 4) – Setup script"
echo " Full SVT version — OpenGL vector graphics"
echo "================================================================"
echo ""

# ── 0. Must run as root ──────────────────────────────────────────────────────
if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: Run as root: sudo bash setup.sh"; exit 1
fi

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PI4_DIR="$REPO_DIR/pi4"
SHARED_DIR="$REPO_DIR/shared"
USER_HOME=$(getent passwd "${SUDO_USER:-pi}" | cut -d: -f6)
RUN_USER="${SUDO_USER:-pi}"

echo "[1/9] Updating package lists…"
apt-get update -qq

echo "[2/9] Installing system dependencies…"
apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv \
    python3-pygame \
    python3-numpy \
    libsdl2-dev libsdl2-ttf-dev libsdl2-image-dev \
    fonts-dejavu-core \
    git curl \
    2>/dev/null

echo "[3/9] Installing OpenGL dependencies (for SVT renderer)…"
apt-get install -y --no-install-recommends \
    libgles2-mesa-dev \
    libegl1-mesa-dev \
    libgl1-mesa-dri \
    mesa-utils \
    2>/dev/null

echo "[4/9] Installing Python packages…"
pip3 install --quiet --break-system-packages pygame numpy moderngl glcontext 2>/dev/null || \
pip3 install --quiet pygame numpy moderngl glcontext

echo "[5/9] Configuring display…"
echo ""
echo "  Supported displays:"
echo "    1) ROADOM 7\" HDMI  (1024×600) — default"
echo "    2) ROADOM 10\" HDMI (1024×600)"
echo "    3) Waveshare 3.5\" DPI (640×480)"
echo ""
# Auto-configure for HDMI displays (works for both ROADOM 7" and 10")
# For Waveshare DPI: edit pi4/config.py → DISPLAY_PROFILE = "waveshare_35"
if ! grep -q "# PFD Display (Pi 4)" /boot/firmware/config.txt 2>/dev/null; then
    cat >> /boot/firmware/config.txt << 'CFG'
# PFD Display (Pi 4) – added by setup.sh
dtoverlay=vc4-kms-v3d
dtparam=i2c_arm=on
disable_overscan=1
# GPU memory allocation for OpenGL rendering
gpu_mem=256
# HDMI display (ROADOM 7"/10"): auto-detected, no extra config needed.
# For Waveshare 3.5" DPI, uncomment below and add DPI overlays:
#dtoverlay=waveshare-35dpi-3b-4b
#framebuffer_width=640
#framebuffer_height=480
CFG
    echo "  → /boot/firmware/config.txt updated"
    echo "  → Default: HDMI display (ROADOM 7\"/10\")"
    echo "  → For Waveshare 3.5\" DPI: edit config.txt and pi4/config.py"
else
    echo "  → config.txt already configured"
fi

echo "[6/9] Creating data directories…"
mkdir -p "$PI4_DIR/data/srtm"
mkdir -p "$PI4_DIR/data/obstacles"
chown -R "$RUN_USER:" "$PI4_DIR/data"

echo "[7/9] Installing systemd service…"
cat > /etc/systemd/system/pfd.service << SVCEOF
[Unit]
Description=PFD Flight Display (Pi 4 – Full SVT)
After=network.target

[Service]
User=$RUN_USER
WorkingDirectory=$PI4_DIR
Environment="SDL_FBDEV=/dev/fb0"
Environment="SDL_VIDEODRIVER=fbcon"
Environment="DISPLAY="
Environment="PYTHONPATH=$SHARED_DIR"
ExecStart=/usr/bin/python3 $PI4_DIR/pfd.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable pfd.service
echo "  → pfd.service installed and enabled"

echo "[8/9] WiFi config…"
echo "  → To switch networks use: sudo bash wifi_switch.sh flight|home"

echo "[9/9] Done."

echo ""
echo "================================================================"
echo " Setup complete! (Pi 4 – Full SVT)"
echo ""
echo " Next steps:"
echo "   1. Reboot: sudo reboot"
echo "   2. Download terrain tiles (while on home WiFi):"
echo "      bash fetch_sedona_tiles.sh"
echo "   3. Test demo mode:"
echo "      python3 pi4/pfd.py --demo --sim"
echo "   4. Connect to Pico W AP and run:"
echo "      python3 pi4/pfd.py"
echo ""
echo " The pfd.service will auto-start on next boot."
echo "================================================================"
