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
    python3-serial \
    python3-pyshp \
    libsdl2-dev libsdl2-ttf-dev libsdl2-image-dev \
    fonts-dejavu-core \
    git curl

echo "[3/8] Verifying Python packages…"
# pygame and numpy are installed via apt (python3-pygame, python3-numpy) above.
# No pip install needed — avoids PEP 668 "externally managed environment" error
# on Bookworm+.
python3 -c "import pygame; import numpy; print(f'  → pygame {pygame.ver}, numpy {numpy.__version__}')"
# pyshp powers the water-mask + state-line download path on the Terrain Data
# screen.  apt's python3-pyshp doesn't exist on every RPi OS variant, so fall
# back to pip if the import still fails after the apt step above.
if ! python3 -c "import shapefile" 2>/dev/null; then
    echo "  → pyshp not present via apt — installing via pip"
    pip3 install --break-system-packages pyshp || \
        sudo -u "$RUN_USER" pip3 install --break-system-packages --user pyshp || {
            echo "  ! WARNING: pyshp install failed — water masks + state lines will not work"
            echo "    Run manually:  sudo pip3 install --break-system-packages pyshp"
        }
fi
python3 -c "import shapefile; print(f'  → pyshp {shapefile.__version__}')" 2>/dev/null || true

# timezonefinder: EXACT local time (timezone boundaries + Daylight Saving) for
# the TAF / ETA readouts.  Optional — falls back to a longitude estimate
# without it, so best-effort and never blocks setup.
pip3 install --break-system-packages timezonefinder 2>/dev/null || \
    sudo -u "$RUN_USER" pip3 install --break-system-packages --user timezonefinder 2>/dev/null || \
    echo "  → timezonefinder not installed — local time will use a longitude estimate"

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
# Hardware PWM on GPIO 18 for the panel backlight (active-low; pfd.py
# uses kernel sysfs PWM with polarity=inversed).
dtoverlay=pwm,pin=18,func=2
CFG
    echo "  → /boot/firmware/config.txt updated for Waveshare 3.5\" DPI"
    echo "  → IMPORTANT: Copy waveshare DT overlay files to /boot/overlays/"
    echo "    Download from: https://www.waveshare.com/wiki/3.5inch_DPI_LCD"
else
    echo "  → config.txt already configured (Waveshare block present)"
    # Idempotent backfill of the PWM-backlight overlay for installs that
    # were set up before backlight control existed in this repo.
    if ! grep -q "^dtoverlay=pwm,pin=18" /boot/firmware/config.txt; then
        echo "" >> /boot/firmware/config.txt
        echo "# Hardware PWM on GPIO 18 for the panel backlight (added by setup.sh)" >> /boot/firmware/config.txt
        echo "dtoverlay=pwm,pin=18,func=2" >> /boot/firmware/config.txt
        echo "  → added PWM-backlight overlay (reboot required)"
    fi
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
SupplementaryGroups=video render input dialout gpio
WorkingDirectory=$ZERO_DIR
# kmsdrm is required when the panel is driven via vc4-kms-DPI-35inch
# (the new Waveshare 3.5" DPI overlay).  fbcon was correct only for
# legacy framebuffer-console mode, which the KMS overlay disables —
# SDL would silently fail to init video and the PFD would crash on
# the first pygame call.
Environment="SDL_VIDEODRIVER=kmsdrm"
Environment="DISPLAY="
Environment="PYTHONPATH=$SHARED_DIR"
# Backlight PWM setup — runs as root (the leading '+' bypasses User=)
# before the main process starts.  Idempotent so a service restart
# doesn't fail when pwm0 is already exported.  Skips cleanly when the
# pwm overlay isn't loaded (HDMI-only test bench, e.g.) so the PFD
# still starts and just falls back to the no-control path.
ExecStartPre=+/bin/sh -c '\
    if [ -d /sys/class/pwm/pwmchip0 ]; then \
        [ -e /sys/class/pwm/pwmchip0/pwm0 ] || echo 0 > /sys/class/pwm/pwmchip0/export; \
        sleep 0.05; \
        echo 0        > /sys/class/pwm/pwmchip0/pwm0/enable     2>/dev/null || true; \
        echo 1000000  > /sys/class/pwm/pwmchip0/pwm0/period; \
        echo inversed > /sys/class/pwm/pwmchip0/pwm0/polarity   2>/dev/null || true; \
        echo 500000   > /sys/class/pwm/pwmchip0/pwm0/duty_cycle 2>/dev/null || true; \
        echo 1        > /sys/class/pwm/pwmchip0/pwm0/enable; \
        chgrp gpio /sys/class/pwm/pwmchip0/pwm0/duty_cycle; \
        chmod g+w  /sys/class/pwm/pwmchip0/pwm0/duty_cycle; \
    fi'
ExecStart=/usr/bin/python3 $ZERO_DIR/pfd.py
Restart=always
RestartSec=5
StartLimitIntervalSec=0

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable pfd.service
echo "  → pfd.service installed and enabled"

# Let the (non-root) PFD user halt / reboot the Pi from the SYSTEM screen's
# SHUTDOWN / REBOOT buttons — a graceful stop so an SD-booted Pi isn't only
# ever power-pulled.  Scoped to exactly these two commands, nothing else.
cat > /etc/sudoers.d/pfd-power << SUDOEOF
$RUN_USER ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff, /usr/bin/systemctl reboot
SUDOEOF
chmod 440 /etc/sudoers.d/pfd-power
visudo -cf /etc/sudoers.d/pfd-power >/dev/null 2>&1 \
    && echo "  → pfd-power sudoers rule installed (SHUTDOWN / REBOOT buttons)" \
    || { echo "  ! pfd-power sudoers rule invalid — removing"; rm -f /etc/sudoers.d/pfd-power; }

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
