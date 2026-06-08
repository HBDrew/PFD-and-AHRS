#!/usr/bin/env bash
# install_adsb.sh – ADS-B IN receiver stack for the PFD (Nooelec NESDR Nano 2).
#
# Installs the RTL-SDR tooling, the dump1090 (1090ES) / dump978 (978 UAT)
# decoders, and the GDL90 bridge service that feeds the PFD's traffic
# listener on UDP 4000.  The display itself needs no changes — it already
# listens for GDL90 (see shared/adsb.py); this script just stands up a
# source on the same Pi.
#
# Run:  sudo bash tools/install_adsb.sh
#
# Hardware: Nooelec NESDR Nano 2 dual-band bundle = two RTL-SDR dongles
# (one per band) + 978/1090 MHz antennas.  Each dongle is addressed by a
# USB serial string so dump1090 and dump978 each grab the right one.

set -e

if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: run as root: sudo bash tools/install_adsb.sh"; exit 1
fi

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="${SUDO_USER:-pi}"
BRIDGE="$REPO_DIR/tools/adsb_gdl90_bridge.py"

echo "================================================================"
echo " ADS-B IN receiver install (Nooelec NESDR Nano 2 dual-band)"
echo " repo: $REPO_DIR"
echo "================================================================"

# ── 1. RTL-SDR tools ──────────────────────────────────────────────────────────
echo "[1/6] Installing RTL-SDR tooling…"
apt-get update -qq
apt-get install -y --no-install-recommends rtl-sdr librtlsdr0 python3 2>/dev/null

# ── 2. Free the dongles from the DVB-T kernel driver ──────────────────────────
# The stock dvb_usb_rtl28xxu driver claims the RTL2832U as a TV tuner and
# blocks rtl-sdr.  Blacklist it so dump1090/dump978 can open the devices.
echo "[2/6] Blacklisting DVB-T kernel modules…"
cat > /etc/modprobe.d/blacklist-rtlsdr.conf <<'EOF'
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2830
blacklist rtl2838
EOF

# ── 3. dump1090 (1090ES) + dump978 (978 UAT) ─────────────────────────────────
echo "[3/6] Installing dump1090-fa / dump978-fa…"
if apt-get install -y dump1090-fa dump978-fa 2>/dev/null; then
  echo "      installed from apt."
else
  cat <<'EOF'
      dump1090-fa / dump978-fa not in your apt sources.
      Add FlightAware's package feed, then re-run this script:

        wget -O /tmp/piaware.deb \
          https://www.flightaware.com/adsb/piaware/files/packages/pool/piaware/p/piaware-repository/piaware-repository_8.2_all.deb
        sudo dpkg -i /tmp/piaware.deb
        sudo apt-get update

      (Or build readsb/dump978 from source.)  The bridge service below
      will start regardless and simply wait for a SBS feed on :30003.
EOF
fi

# ── 4. Dongle serials (informational) ─────────────────────────────────────────
cat <<EOF
[4/6] Dual-dongle addressing
      The bundle ships two dongles.  Give each a distinct USB serial once:
        rtl_eeprom -d 0 -s 1090       # the dongle on the 1090 antenna
        rtl_eeprom -d 1 -s 978        # the dongle on the 978 antenna
      then point dump1090-fa at serial 1090 and dump978-fa at serial 978
      (RECEIVER_SERIAL in /etc/default/dump1090-fa and dump978-fa).
      dump1090-fa publishes the SBS-1 feed on TCP 30003 by default; fold
      978 traffic into it so one bridge covers both bands.
EOF

# ── 5. GDL90 bridge systemd service ───────────────────────────────────────────
echo "[5/6] Installing adsb-gdl90.service…"
cat > /etc/systemd/system/adsb-gdl90.service <<EOF
[Unit]
Description=ADS-B SBS-1 -> GDL90/UDP bridge for the PFD
After=network.target dump1090-fa.service
Wants=dump1090-fa.service

[Service]
Type=simple
User=$RUN_USER
ExecStart=/usr/bin/python3 $BRIDGE --sbs-host 127.0.0.1 --sbs-port 30003 --out-host 255.255.255.255 --out-port 4000
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

# ── 6. Enable ─────────────────────────────────────────────────────────────────
echo "[6/6] Enabling services…"
systemctl daemon-reload
systemctl enable --now adsb-gdl90.service || true

echo ""
echo "Done.  Verify with:"
echo "  systemctl status adsb-gdl90.service"
echo "  python3 $BRIDGE --selftest          # bridge smoke test"
echo "The PFD picks up traffic automatically (Setup → Display → MAP LAYERS → TFC)."
echo "A reboot is recommended so the DVB-T blacklist takes effect."
