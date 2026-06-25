#!/usr/bin/env bash
# install_adsb.sh – ADS-B IN receiver stack for the PFD (Nooelec NESDR Nano 2).
#
# Installs the RTL-SDR tooling, the readsb (1090ES) decoder, and the GDL90
# bridge service that feeds the PFD's traffic listener on UDP 4000.  The
# display itself needs no changes — it already listens for GDL90 (see
# shared/adsb.py); this script just stands up a source on the same Pi (or a
# dedicated receiver Pi, e.g. a Pi 5).
#
# Run:  sudo bash tools/install_adsb.sh
#
# Hardware: Nooelec NESDR Nano 2 dual-band bundle = two RTL-SDR dongles
# (one per band) + 978/1090 MHz antennas.  Each dongle is addressed by a
# USB serial string so the 1090 and 978 decoders each grab the right one.
#
# Bands:
#   1090ES traffic .......... readsb (this script)         → SBS :30003 → bridge
#   978 UAT weather/traffic   tools/install_dump978.sh      → dump978 :30978
#                             tools/enable_978_traffic.sh   folds 978 traffic in
#
# Note: this used to install FlightAware's dump1090-fa from apt, but that
# package isn't reliably available on current Raspberry Pi OS (Debian trixie /
# Pi 5 — the pinned piaware-repository .deb 404s).  readsb (wiedehopf's
# installer) builds for the running OS and serves the same SBS feed on :30003,
# so the bridge below is unchanged.

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
# blocks rtl-sdr.  Blacklist it so the decoders can open the devices.
echo "[2/6] Blacklisting DVB-T kernel modules…"
cat > /etc/modprobe.d/blacklist-rtlsdr.conf <<'EOF'
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2830
blacklist rtl2838
EOF

# ── 3. readsb (1090ES decoder) ────────────────────────────────────────────────
echo "[3/6] Installing the readsb 1090 decoder (wiedehopf installer)…"
if command -v readsb >/dev/null 2>&1; then
  echo "      readsb already installed — skipping."
else
  # Builds readsb for the running OS, installs readsb.service + the tar1090 web
  # map, and serves the SBS-1 feed on TCP :30003 by default — what the bridge
  # consumes.  (readsb is also built with UAT support, so enable_978_traffic.sh
  # can later fold 978 traffic into the same SBS output.)
  bash -c "$(wget -nv -O - https://github.com/wiedehopf/adsb-scripts/raw/master/readsb-install.sh)"
fi

# ── 4. Pin readsb to the 1090 dongle ──────────────────────────────────────────
echo "[4/6] Pinning readsb to the 1090 dongle (serial 1090)…"
RD=/etc/default/readsb
if [ -f "$RD" ]; then
  sed -i 's|^RECEIVER_OPTIONS=.*|RECEIVER_OPTIONS="--device 1090 --device-type rtlsdr --gain auto --ppm 0"|' "$RD"
  systemctl restart readsb || true
else
  echo "      WARNING: $RD not found — set --device 1090 in readsb's options by hand."
fi
cat <<EOF
      Give each dongle a distinct USB serial once (so each decoder grabs its band):
        rtl_eeprom -d 0 -s 1090       # the dongle on the 1090 antenna
        rtl_eeprom -d 1 -s 978        # the dongle on the 978 antenna
      For the 978 band: tools/install_dump978.sh (decoder + FIS-B weather bridge),
      then tools/enable_978_traffic.sh to fold 978 *traffic* into readsb's :30003.
EOF

# ── 5. GDL90 bridge systemd service ───────────────────────────────────────────
echo "[5/6] Installing adsb-gdl90.service…"
cat > /etc/systemd/system/adsb-gdl90.service <<EOF
[Unit]
Description=ADS-B SBS-1 -> GDL90/UDP bridge for the PFD
After=network.target readsb.service
Wants=readsb.service

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
echo "  systemctl status readsb adsb-gdl90.service"
echo "  sudo ss -tlnp | grep 30003           # readsb SBS feed listening"
echo "  python3 $BRIDGE --selftest           # bridge smoke test"
echo "The PFD picks up traffic automatically (Setup → Display → MAP LAYERS → TFC)."
echo "A reboot is recommended so the DVB-T blacklist takes effect."
