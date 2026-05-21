#!/usr/bin/env bash
# setup_usb_sync.sh — configure this Pi Zero as a USB-ethernet gadget so
# it can talk to the Pi 4 over the USB cable (in addition to / instead
# of WiFi).  Mirrors the AHRS link's WiFi+USB redundancy: when the
# cabin WiFi flakes, screen sync keeps running on the USB link.
#
# After running this once and rebooting, plug the Pi Zero's *data* USB
# port (not the PWR-only port) into one of the Pi 4's USB-A ports.  An
# interface called `usb0` will appear on both ends with addresses
# 10.55.0.2 (Zero) and 10.55.0.1 (Pi 4).
#
# Idempotent — safe to re-run.  Requires sudo / root.

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Run as root:  sudo bash $0"
    exit 1
fi

CONFIG_TXT="/boot/firmware/config.txt"
CMDLINE_TXT="/boot/firmware/cmdline.txt"
MODPROBE_D="/etc/modprobe.d/pfd-usb-sync.conf"
NM_CON="pfd-usb-sync"
USB_IP="10.55.0.2/24"

# Pinned MACs (locally-administered: first octet 02:) — keep usb0's
# name stable across reboots so NetworkManager always reapplies the
# right profile.  Pi 4 side uses a different pair (see its script).
DEV_MAC="02:00:00:00:55:02"   # gadget side (this Pi Zero)
HOST_MAC="02:00:00:00:55:01"  # host side (the Pi 4)

# Older firmware layouts use /boot directly.  Try both.
if [[ ! -f "$CONFIG_TXT" && -f "/boot/config.txt" ]]; then
    CONFIG_TXT="/boot/config.txt"
    CMDLINE_TXT="/boot/cmdline.txt"
fi

echo "[usb-sync] config.txt:  $CONFIG_TXT"
echo "[usb-sync] cmdline.txt: $CMDLINE_TXT"

# ── 1. dwc2 overlay in config.txt ────────────────────────────────────────
# dr_mode=peripheral is REQUIRED — without it, dwc2 stays in OTG
# autodetect mode and hangs at boot the moment a USB host appears on
# the other end of the cable.  Symptom: kernel never finishes init,
# console stops at "Loading initial ramdisk".
OVERLAY_LINE='dtoverlay=dwc2,dr_mode=peripheral'
if grep -qE '^[[:space:]]*dtoverlay=dwc2(,|$)' "$CONFIG_TXT"; then
    # Existing dtoverlay=dwc2 line.  Replace it so dr_mode is correct.
    if ! grep -qE '^[[:space:]]*dtoverlay=dwc2,dr_mode=peripheral[[:space:]]*$' "$CONFIG_TXT"; then
        echo "[usb-sync] upgrading dtoverlay=dwc2 line to include dr_mode=peripheral"
        sed -i -E "s|^[[:space:]]*dtoverlay=dwc2.*$|$OVERLAY_LINE|" "$CONFIG_TXT"
    else
        echo "[usb-sync] dtoverlay=dwc2,dr_mode=peripheral already present"
    fi
else
    echo "[usb-sync] adding $OVERLAY_LINE to $CONFIG_TXT"
    {
        echo ""
        echo "# pfd-usb-sync: USB-ethernet gadget on USB data port"
        echo "$OVERLAY_LINE"
    } >> "$CONFIG_TXT"
fi

# ── 2. modules-load=dwc2,g_ether in cmdline.txt ──────────────────────────
# cmdline.txt is a single line; inject after `rootwait` if not already
# in the args.  Both modules go in one comma list.
cur_cmdline="$(cat "$CMDLINE_TXT")"
if ! grep -q 'modules-load=.*g_ether' <<<"$cur_cmdline"; then
    echo "[usb-sync] adding modules-load=dwc2,g_ether to $CMDLINE_TXT"
    if grep -q 'modules-load=' <<<"$cur_cmdline"; then
        # Existing modules-load list.  Append only the modules that
        # aren't already in it, to avoid duplicates like dwc2,dwc2.
        existing="$(sed -nE 's/.*modules-load=([^[:space:]]+).*/\1/p' <<<"$cur_cmdline")"
        add=""
        grep -q '\bdwc2\b'    <<<"$existing" || add="${add:+$add,}dwc2"
        grep -q '\bg_ether\b' <<<"$existing" || add="${add:+$add,}g_ether"
        new_cmdline="$(sed -E "s/modules-load=([^[:space:]]+)/modules-load=\\1,$add/" <<<"$cur_cmdline")"
    elif grep -q 'rootwait' <<<"$cur_cmdline"; then
        new_cmdline="$(sed -E 's/rootwait/rootwait modules-load=dwc2,g_ether/' <<<"$cur_cmdline")"
    else
        new_cmdline="$cur_cmdline modules-load=dwc2,g_ether"
    fi
    # cmdline.txt must remain a single line.
    echo "$new_cmdline" > "$CMDLINE_TXT"
else
    echo "[usb-sync] g_ether already in $CMDLINE_TXT"
fi

# ── 3. Pin gadget MACs so usb0 name + ARP stay stable ────────────────────
echo "[usb-sync] writing $MODPROBE_D"
cat > "$MODPROBE_D" <<EOF
# Pin g_ether MACs so the kernel doesn't randomise them on every boot
# (which would churn the interface name on the Pi 4 host side).
options g_ether dev_addr=$DEV_MAC host_addr=$HOST_MAC
EOF

# ── 4. NetworkManager profile for usb0 ───────────────────────────────────
if ! command -v nmcli >/dev/null 2>&1; then
    echo "[usb-sync] WARNING: nmcli not found — install NetworkManager or"
    echo "[usb-sync] configure $USB_IP on usb0 by other means."
else
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
fi

echo
echo "[usb-sync] DONE.  Reboot to load dwc2 + g_ether:"
echo "    sudo reboot"
echo
echo "After reboot, plug the Pi Zero's DATA USB port into the Pi 4."
echo "Verify with:"
echo "    ip -4 -o addr show usb0"
echo "Expected:  inet 10.55.0.2/24 ..."
