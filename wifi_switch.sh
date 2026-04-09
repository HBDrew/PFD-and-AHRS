#!/usr/bin/env bash
# wifi_switch.sh – Toggle the Pi between Home WiFi and Pico W AP mode.
#
# Usage:
#   sudo bash wifi_switch.sh home    — join your home network (for terrain downloads)
#   sudo bash wifi_switch.sh flight  — join Pico W AP (192.168.4.1) for live AHRS
#   sudo bash wifi_switch.sh status  — show current mode
#
# On first run, edit the HOME_SSID / HOME_PSK lines below.
# ─────────────────────────────────────────────────────────────────────────────

# ── Edit these for your home network ─────────────────────────────────────────
HOME_SSID="YourHomeNetwork"
HOME_PSK="YourHomePassword"

# ── Pico W AP (must match config.py on the Pico) ─────────────────────────────
PICO_SSID="PFD_AP"
PICO_PSK="picoahrs1"   # leave blank if Pico AP has no password

# ─────────────────────────────────────────────────────────────────────────────
WPA_CONF="/etc/wpa_supplicant/wpa_supplicant.conf"
NM_ACTIVE=$(systemctl is-active NetworkManager 2>/dev/null || true)

if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root: sudo bash wifi_switch.sh [home|flight|status]"
    exit 1
fi

MODE="${1:-status}"

write_wpa() {
    local SSID="$1" PSK="$2"
    cat > "$WPA_CONF" << WPAEOF
ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1
country=US

network={
    ssid="$SSID"
    $([ -n "$PSK" ] && echo "psk=\"$PSK\"" || echo "key_mgmt=NONE")
    priority=10
}
WPAEOF
    chmod 600 "$WPA_CONF"
    wpa_cli -i wlan0 reconfigure >/dev/null 2>&1 || true
    # Give it a moment
    sleep 3
    wpa_cli -i wlan0 status | grep -E "^ssid=|^ip_address=|^wpa_state=" || true
}

nm_connect() {
    local SSID="$1" PSK="$2" CON_NAME="$3"
    # Remove existing connection with same name if present
    nmcli con delete "$CON_NAME" 2>/dev/null || true
    if [ -n "$PSK" ]; then
        nmcli dev wifi connect "$SSID" password "$PSK" name "$CON_NAME"
    else
        nmcli dev wifi connect "$SSID" name "$CON_NAME"
    fi
}

case "$MODE" in
    home)
        echo "Switching to HOME WiFi: $HOME_SSID"
        if [ "$NM_ACTIVE" = "active" ]; then
            nm_connect "$HOME_SSID" "$HOME_PSK" "pfd-home"
        else
            write_wpa "$HOME_SSID" "$HOME_PSK"
        fi
        echo ""
        echo "Connected to home network."
        echo "Download Sedona terrain tiles:"
        echo "  bash fetch_sedona_tiles.sh"
        echo "Or full US coverage (~5 GB):"
        echo "  python3 pi_display/download_terrain.py"
        ;;

    flight)
        echo "Switching to FLIGHT mode: $PICO_SSID (Pico W AP)"
        if [ "$NM_ACTIVE" = "active" ]; then
            nm_connect "$PICO_SSID" "$PICO_PSK" "pfd-pico"
        else
            write_wpa "$PICO_SSID" "$PICO_PSK"
        fi
        echo ""
        echo "Connected to Pico W AP."
        echo "Start PFD display:"
        echo "  python3 pi_display/pfd.py"
        ;;

    status)
        echo "=== WiFi Status ==="
        if [ "$NM_ACTIVE" = "active" ]; then
            nmcli -t -f ACTIVE,SSID,SIGNAL dev wifi list | grep '^yes' || echo "Not connected"
            nmcli -t -f IP4.ADDRESS dev show wlan0 | head -1
        else
            wpa_cli -i wlan0 status 2>/dev/null | grep -E "^ssid=|^ip_address=|^wpa_state=" || \
                echo "wpa_supplicant not running"
        fi
        ;;

    *)
        echo "Usage: sudo bash wifi_switch.sh [home|flight|status]"
        echo "  home    — connect to home WiFi for terrain downloads"
        echo "  flight  — connect to Pico W AP for live AHRS"
        echo "  status  — show current connection"
        exit 1
        ;;
esac
