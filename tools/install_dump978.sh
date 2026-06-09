#!/usr/bin/env bash
# install_dump978.sh – 978 UAT (FIS-B weather) receiver for the PFD.
#
# Adds a 978 decoder ALONGSIDE the existing 1090 stack — nothing about 1090
# (readsb + adsb-gdl90.service) changes.  Different dongle (serial 978),
# different decoder, different bridge; they share only the GDL90 :4000 sink.
#
# What it does:
#   1. installs dump978-fa (FlightAware feed, or builds from source if needed)
#   2. runs it on the 978 dongle, raw output on TCP 30978   (dump978-978.service)
#   3. runs tools/dump978_gdl90_bridge.py to forward the FIS-B uplink frames as
#      GDL90 0x07 to the display's UDP :4000 listener        (dump978-gdl90.service)
#
# Run:  sudo bash tools/install_dump978.sh

set -e

if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: run as root: sudo bash tools/install_dump978.sh"; exit 1
fi

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="${SUDO_USER:-pi}"
BRIDGE="$REPO_DIR/tools/dump978_gdl90_bridge.py"
SERIAL="978"          # USB serial set via: rtl_eeprom -d 0 -s 978
RAW_PORT="30978"
OUT_HOST="255.255.255.255"
OUT_PORT="4000"

echo "================================================================"
echo " dump978 / FIS-B weather install  (alongside the 1090 stack)"
echo " repo: $REPO_DIR   user: $RUN_USER   dongle serial: $SERIAL"
echo "================================================================"

build_from_source() {
  echo "      building dump978-fa from FlightAware source…"
  apt-get install -y --no-install-recommends \
    git build-essential debhelper \
    libboost-system-dev libboost-program-options-dev \
    libboost-regex-dev libboost-filesystem-dev libsoapysdr-dev
  tmp="$(mktemp -d)"
  git clone --depth 1 https://github.com/flightaware/dump978 "$tmp/dump978"
  make -C "$tmp/dump978" dump978-fa
  install -m755 "$tmp/dump978/dump978-fa" /usr/local/bin/dump978-fa
  rm -rf "$tmp"
}

# ── 1. RTL-SDR + SoapySDR runtime ─────────────────────────────────────────────
# dump978-fa selects the dongle through SoapySDR's rtlsdr module, so that
# module must be present even though readsb talks to librtlsdr directly.
echo "[1/5] Installing rtl-sdr + SoapySDR rtlsdr module…"
apt-get update -qq
apt-get install -y --no-install-recommends \
  rtl-sdr soapysdr-module-rtlsdr python3 2>/dev/null || true

# ── 2. dump978-fa binary ──────────────────────────────────────────────────────
echo "[2/5] Installing dump978-fa…"
if command -v dump978-fa >/dev/null 2>&1; then
  echo "      already present: $(command -v dump978-fa)"
elif apt-get install -y --no-install-recommends dump978-fa 2>/dev/null; then
  echo "      installed from apt."
else
  echo "      not in apt — adding FlightAware's package feed…"
  if wget -nv -O /tmp/piaware-repo.deb \
        "https://www.flightaware.com/adsb/piaware/files/packages/pool/piaware/p/piaware-repository/piaware-repository_8.2_all.deb" \
     && dpkg -i /tmp/piaware-repo.deb && apt-get update -qq \
     && apt-get install -y --no-install-recommends dump978-fa 2>/dev/null; then
    echo "      installed from FlightAware feed."
  else
    build_from_source
  fi
fi

# The FlightAware package ships its own dump978-fa.service (and pulls in
# skyaware978).  Disable it so it doesn't grab the 978 dongle out from under
# our explicitly-configured unit below.
systemctl disable --now dump978-fa.service 2>/dev/null || true

BIN="$(command -v dump978-fa || echo /usr/local/bin/dump978-fa)"
echo "      using binary: $BIN"

# ── 3. dump978 decoder service (978 dongle, raw port) ─────────────────────────
echo "[3/5] Installing dump978-978.service (serial $SERIAL → raw :$RAW_PORT)…"
cat > /etc/systemd/system/dump978-978.service <<EOF
[Unit]
Description=dump978-fa (978 UAT / FIS-B) on dongle serial $SERIAL
After=network.target

[Service]
Type=simple
ExecStart=$BIN --sdr driver=rtlsdr,serial=$SERIAL --raw-port $RAW_PORT
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# ── 4. dump978 → GDL90 bridge service ─────────────────────────────────────────
echo "[4/5] Installing dump978-gdl90.service (raw :$RAW_PORT → GDL90 :$OUT_PORT)…"
cat > /etc/systemd/system/dump978-gdl90.service <<EOF
[Unit]
Description=dump978 raw uplink -> GDL90/UDP bridge (FIS-B weather) for the PFD
After=network.target dump978-978.service
Wants=dump978-978.service

[Service]
Type=simple
User=$RUN_USER
ExecStart=/usr/bin/python3 $BRIDGE --raw-host 127.0.0.1 --raw-port $RAW_PORT --out-host $OUT_HOST --out-port $OUT_PORT
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

# ── 5. Enable ─────────────────────────────────────────────────────────────────
echo "[5/5] Enabling services…"
systemctl daemon-reload
systemctl enable --now dump978-978.service
systemctl enable --now dump978-gdl90.service

cat <<EOF

Done.  Verify:
  systemctl status dump978-978.service dump978-gdl90.service
  nc 127.0.0.1 $RAW_PORT          # '+' lines = FIS-B weather, '-' = UAT traffic
  python3 $BRIDGE --selftest      # offline bridge round-trip

Notes:
  • 1090/readsb is untouched — this only uses the free '$SERIAL' dongle.
  • FIS-B is line-of-sight from ground stations: expect little/nothing on the
    ground until airborne or near a tower.  The internet METARs backfill.
  • On the MFD (MET overlay): the WX status line shows 'WX AUTO R… I…'; tap it
    to switch RADIO / AUTO / INET.
EOF
