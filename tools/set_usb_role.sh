#!/bin/bash
# set_usb_role.sh — flip Pi Zero's dwc2 dr_mode between host and peripheral.
#
# Usage:  sudo tools/set_usb_role.sh host          # USB → AHRS / sensors
#         sudo tools/set_usb_role.sh peripheral    # USB → screen-sync to Pi 4
#         sudo tools/set_usb_role.sh status        # print current mode
#
# Pi Zero has a single USB OTG controller; dr_mode is read at boot so a
# reboot is required after a flip.  Edits /boot/firmware/config.txt
# (preferred) or /boot/config.txt (older raspbians) as a fallback.

set -euo pipefail

CONFIG=""
for c in /boot/firmware/config.txt /boot/config.txt; do
  [[ -f "$c" ]] && { CONFIG="$c"; break; }
done
if [[ -z "$CONFIG" ]]; then
  echo "set_usb_role: cannot find config.txt" >&2
  exit 2
fi

mode="${1:-status}"

case "$mode" in
  host|peripheral)
    if grep -q '^dtoverlay=dwc2' "$CONFIG"; then
      sed -i -E "s/^(dtoverlay=dwc2[^[:space:]]*?,dr_mode=)(host|peripheral|otg)/\1${mode}/" "$CONFIG"
      # If the line was bare "dtoverlay=dwc2" with no dr_mode, append.
      if ! grep -q "dr_mode=${mode}" "$CONFIG"; then
        sed -i -E "s/^dtoverlay=dwc2$/dtoverlay=dwc2,dr_mode=${mode}/" "$CONFIG"
      fi
    else
      # Append a fresh line to the [all] section (or end of file).
      printf '\ndtoverlay=dwc2,dr_mode=%s\n' "$mode" >> "$CONFIG"
    fi
    echo "set_usb_role: dr_mode=${mode} (reboot to apply)"
    ;;
  status)
    line=$(grep -E '^dtoverlay=dwc2' "$CONFIG" || true)
    if [[ -z "$line" ]]; then
      echo "none"
    elif [[ "$line" == *dr_mode=peripheral* ]]; then
      echo "peripheral"
    elif [[ "$line" == *dr_mode=host* ]]; then
      echo "host"
    elif [[ "$line" == *dr_mode=otg* ]]; then
      echo "otg"
    else
      echo "unknown"
    fi
    ;;
  *)
    echo "Usage: $0 {host|peripheral|status}" >&2
    exit 1
    ;;
esac
