#!/usr/bin/env bash
# install_autostart.sh — refresh the pfd.service systemd unit so the PFD
# launches automatically on power-up.  Idempotent: safe to re-run.
#
# Usage (on the Pi):
#   sudo bash tools/install_autostart.sh
#
# Drops in the same service definition the full setup.sh installs but
# without the apt-get / pip / config.txt steps, so it's safe to run
# whenever the unit needs to be refreshed (e.g. after pulling a fix
# to the service environment).

set -e

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: run as root: sudo bash $0"
    exit 1
fi

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PI4_DIR="$REPO_DIR/pi4"
SHARED_DIR="$REPO_DIR/shared"
RUN_USER="${SUDO_USER:-pi}"
USER_HOME=$(getent passwd "$RUN_USER" | cut -d: -f6)

if [ ! -f "$PI4_DIR/pfd.py" ]; then
    echo "ERROR: $PI4_DIR/pfd.py not found — run from the repo root"
    exit 1
fi

echo "Installing pfd.service for user $RUN_USER…"
cat > /etc/systemd/system/pfd.service << SVCEOF
[Unit]
Description=PFD Flight Display (Pi 4 – Full SVT)
After=multi-user.target

[Service]
Type=simple
User=$RUN_USER
SupplementaryGroups=video render input
WorkingDirectory=$PI4_DIR
# kmsdrm matches pfd.py's own default and is required for OpenGL
# ES via Mesa.  fbcon can't drive a GL context.
Environment="SDL_VIDEODRIVER=kmsdrm"
Environment="DISPLAY="
Environment="PYTHONPATH=$SHARED_DIR"
Environment="HOME=$USER_HOME"
ExecStart=/usr/bin/python3 $PI4_DIR/pfd.py
Restart=always
RestartSec=5
StartLimitIntervalSec=0

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable pfd.service
systemctl restart pfd.service
sleep 1
systemctl --no-pager --full status pfd.service | head -20

echo ""
echo "Done.  The PFD will launch on every boot."
echo "  status:  sudo systemctl status pfd.service"
echo "  logs:    sudo journalctl -u pfd.service -n 100 --no-pager"
echo "  stop:    sudo systemctl stop pfd.service"
echo "  start:   sudo systemctl start pfd.service"
