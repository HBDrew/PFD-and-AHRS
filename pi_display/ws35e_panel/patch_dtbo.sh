#!/usr/bin/env bash
# patch_dtbo.sh – Change Waveshare_35DSI.dtbo compatible to "waveshare,ws35dsi-e"
# so that ws35e_panel.ko binds instead of panel-simple-dsi.
#
# Run as root on the Pi:  sudo bash patch_dtbo.sh

set -e

DTBO=/boot/firmware/overlays/Waveshare_35DSI.dtbo
ORIG=/boot/firmware/overlays/Waveshare_35DSI.dtbo.orig
DTS=/tmp/ws35_patch.dts
NEW_DTBO=/tmp/Waveshare_35DSI_patched.dtbo

if [ ! -f "$DTBO" ]; then
    echo "ERROR: $DTBO not found. Did you copy it to /boot/firmware/overlays/?"
    exit 1
fi

echo "==> Backing up original dtbo to $ORIG"
[ -f "$ORIG" ] || cp "$DTBO" "$ORIG"

echo "==> Decompiling $DTBO → $DTS"
dtc -I dtb -O dts -o "$DTS" "$DTBO" 2>/dev/null

echo "==> Patching compatible string"
# Replace "Generic,panel-dsi" with "waveshare,ws35dsi-e"
# Also removes the "panel-dsi" fallback to prevent panel-simple-dsi binding
sed -i 's/compatible = "Generic,panel-dsi"[^;]*/compatible = "waveshare,ws35dsi-e"/' "$DTS"

echo "==> Patched compatible lines:"
grep -n "compatible.*waveshare\|compatible.*Generic\|compatible.*panel-dsi" "$DTS" || true

echo "==> Recompiling → $NEW_DTBO"
dtc -I dts -O dtb -o "$NEW_DTBO" "$DTS" 2>/dev/null

echo "==> Installing patched dtbo"
cp "$NEW_DTBO" "$DTBO"

echo ""
echo "Done. Reboot for the change to take effect."
echo "Then: cd ~/ws35e_panel && make && sudo insmod ws35e_panel.ko"
echo "Check: dmesg | grep ws35e"
