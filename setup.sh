#!/usr/bin/env bash
# setup.sh – One-shot install for Pi Zero 2W PFD display
# Run: sudo bash setup.sh
# Tested on: Raspberry Pi OS Lite (64-bit), Pi Zero 2W

set -e

echo "================================================================"
echo " PFD Display – Setup script"
echo " Pi Zero 2W + KLAYERS 3.5\" 640×480 DSI touch display"
echo "================================================================"
echo ""

# ── 0. Must run as root ──────────────────────────────────────────────────────
if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: Run as root: sudo bash setup.sh"; exit 1
fi

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
DISPLAY_DIR="$REPO_DIR/pi_display"
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

echo "[3/8] Installing Python packages…"
pip3 install --quiet --break-system-packages pygame numpy 2>/dev/null || \
pip3 install --quiet pygame numpy

echo "[4/8] Configuring DSI display (KLAYERS 3.5\" 640×480)…"
# Enable DSI, set framebuffer resolution
if ! grep -q "dtoverlay=vc4-kms-v3d" /boot/firmware/config.txt 2>/dev/null; then
    cat >> /boot/firmware/config.txt << 'CFG'
# PFD Display – added by setup.sh
dtoverlay=vc4-kms-v3d
dtparam=i2c_arm=on
disable_overscan=1
framebuffer_width=640
framebuffer_height=480
CFG
    echo "  → /boot/firmware/config.txt updated"
else
    echo "  → config.txt already configured"
fi

echo "[5/8] Creating data directories…"
mkdir -p "$DISPLAY_DIR/data/srtm"
mkdir -p "$DISPLAY_DIR/data/obstacles"
chown -R "$RUN_USER:" "$DISPLAY_DIR/data"

echo "[6/8] Installing systemd service…"
cat > /etc/systemd/system/pfd.service << SVCEOF
[Unit]
Description=PFD Flight Display
After=network.target

[Service]
User=$RUN_USER
WorkingDirectory=$DISPLAY_DIR
Environment="SDL_FBDEV=/dev/fb0"
Environment="SDL_VIDEODRIVER=fbcon"
Environment="DISPLAY="
ExecStart=/usr/bin/python3 $DISPLAY_DIR/pfd.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable pfd.service
echo "  → pfd.service installed and enabled"

echo "[7/8] WiFi config…"
echo "  → To switch networks use: sudo raspi-config → System → Wireless LAN"
echo "  → Home WiFi (terrain downloads): normal AP mode"
echo "  → Flight mode (Pico W AP):       connect to PFD_AP"

echo "[8/8] Creating terrain download helper…"
cat > "$DISPLAY_DIR/download_terrain.py" << 'DLEOF'
#!/usr/bin/env python3
"""
download_terrain.py – Download SRTM3 elevation tiles for a region.

Usage:
  python3 download_terrain.py               # default: western US + AZ
  python3 download_terrain.py 30 37 -117 -108  # lat_min lat_max lon_min lon_max

Files go to: pi_display/data/srtm/
Source: https://srtm.csi.cgiar.org  (or NASA EarthData – requires free account)
"""
import sys, os, urllib.request, time

SRTM_DIR = os.path.join(os.path.dirname(__file__), "data", "srtm")
os.makedirs(SRTM_DIR, exist_ok=True)

# Using OpenTopography / CGIAR mirrors (no login required)
MIRROR = "https://srtm.csi.cgiar.org/wp-content/uploads/files/srtm_5x5/TIFF/"

# Default: Arizona + surrounding area (covers Sedona, Grand Canyon, Flagstaff)
lat_min = int(sys.argv[1]) if len(sys.argv) > 1 else 31
lat_max = int(sys.argv[2]) if len(sys.argv) > 2 else 37
lon_min = int(sys.argv[3]) if len(sys.argv) > 3 else -115
lon_max = int(sys.argv[4]) if len(sys.argv) > 4 else -109

print(f"Downloading SRTM tiles for lat {lat_min}–{lat_max}, lon {lon_min}–{lon_max}")
print(f"Output: {SRTM_DIR}")

def tile_name(lat, lon):
    ns = 'N' if lat >= 0 else 'S'
    ew = 'E' if lon >= 0 else 'W'
    return f"{ns}{abs(lat):02d}{ew}{abs(lon):03d}.hgt"

# NASA SRTM via USGS (requires .netrc or earthdata login) – fallback to CGIAR
# For simplest use, try OpenTopography API (no account needed for small areas)
def download_tile(lat, lon):
    fname = tile_name(lat, lon)
    dest  = os.path.join(SRTM_DIR, fname)
    if os.path.exists(dest):
        print(f"  {fname} already present")
        return

    # Try multiple sources
    urls = [
        f"https://step.esa.int/auxdata/dem/SRTM90/hgt/{fname}.zip",
        f"https://dds.cr.usgs.gov/srtm/version2_1/SRTM3/North_America/{fname}.zip",
    ]
    for url in urls:
        try:
            print(f"  Downloading {fname}…", end=" ", flush=True)
            import zipfile, io
            with urllib.request.urlopen(url, timeout=30) as r:
                data = r.read()
            z = zipfile.ZipFile(io.BytesIO(data))
            names = [n for n in z.namelist() if n.endswith('.hgt')]
            if names:
                with open(dest, 'wb') as f:
                    f.write(z.read(names[0]))
                print(f"OK ({os.path.getsize(dest)//1024} KB)")
                return
        except Exception as e:
            print(f"failed ({e})")
    print(f"  WARNING: could not download {fname}")

for lat in range(lat_min, lat_max + 1):
    for lon in range(lon_min, lon_max + 1):
        download_tile(lat, lon)
        time.sleep(0.3)

print("Done.")
DLEOF
chmod +x "$DISPLAY_DIR/download_terrain.py"
chown "$RUN_USER:" "$DISPLAY_DIR/download_terrain.py"

echo ""
echo "================================================================"
echo " Setup complete!"
echo ""
echo " Next steps:"
echo "   1. Reboot: sudo reboot"
echo "   2. Download terrain (optional, while on home WiFi):"
echo "      python3 pi_display/download_terrain.py"
echo "   3. Test demo mode:"
echo "      python3 pi_display/pfd.py --demo --sim"
echo "   4. Connect to Pico W AP and run:"
echo "      python3 pi_display/pfd.py"
echo ""
echo " The pfd.service will auto-start on next boot."
echo "================================================================"
