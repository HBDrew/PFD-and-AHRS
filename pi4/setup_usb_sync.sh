#!/usr/bin/env bash
# setup_usb_sync.sh — configure this Pi 4 to accept the Pi Zero's
# USB-ethernet gadget and assign a stable static IP on usb0 so screen
# sync can use the wired link as a redundant transport alongside WiFi.
#
# Run this once on the Pi 4 (no reboot required on this end — usb0
# appears automatically when the Pi Zero is plugged in and enumerates).
# The matching script lives at pi_zero/setup_usb_sync.sh and DOES
# need a reboot on that end to load dwc2 + g_ether.
#
# Idempotent — safe to re-run.  Requires sudo / root.

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Run as root:  sudo bash $0"
    exit 1
fi

NM_CON="pfd-usb-sync"
USB_IP="10.55.0.1/24"

# ── NetworkManager profile bound to interface name usb0 ──────────────────
# We bind by interface NAME rather than MAC.  The Pi Zero pins its
# gadget MAC via /etc/modprobe.d/pfd-usb-sync.conf so usb0 stays
# stable across reboots on both ends.
if ! command -v nmcli >/dev/null 2>&1; then
    echo "[usb-sync] ERROR: nmcli not found.  Install NetworkManager:"
    echo "    sudo apt install network-manager"
    exit 1
fi

echo "[usb-sync] (re)creating NetworkManager profile '$NM_CON'"
nmcli connection delete "$NM_CON" >/dev/null 2>&1 || true
nmcli connection add \
    type ethernet \
    ifname usb0 \
    con-name "$NM_CON" \
    ipv4.method manual \
    ipv4.addresses "$USB_IP" \
    ipv6.method ignore \
    connection.autoconnect yes \
    >/dev/null

# If usb0 is already up (Pi Zero already plugged in and running with
# its side configured), bring the profile up immediately.  Otherwise
# NetworkManager will apply it the moment usb0 enumerates.
if ip link show usb0 >/dev/null 2>&1; then
    echo "[usb-sync] usb0 already present — activating profile now"
    nmcli connection up "$NM_CON" >/dev/null || true
fi

echo
echo "[usb-sync] DONE on this Pi 4."
echo
echo "Next: on the Pi Zero, run:"
echo "    sudo bash ~/PFD-and-AHRS/pi_zero/setup_usb_sync.sh"
echo "    sudo reboot"
echo
echo "Once the Pi Zero is rebooted and plugged in (DATA USB port), verify:"
echo "    ip -4 -o addr show usb0"
echo "Expected:  inet 10.55.0.1/24 ..."
echo "    ping -c2 10.55.0.2"
