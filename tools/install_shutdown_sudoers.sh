#!/usr/bin/env bash
# install_shutdown_sudoers.sh — grant the (non-root) PFD user permission to
# halt / reboot the Pi from the SYSTEM screen's SHUTDOWN / REBOOT buttons.
# Idempotent: safe to re-run.  Use on an already-set-up Pi so you don't have
# to re-run the full setup.sh just to pick up the graceful-shutdown feature.
#
# Usage (on the Pi):
#   sudo bash tools/install_shutdown_sudoers.sh
#
# Installs a sudoers drop-in scoped to EXACTLY two commands
# (systemctl poweroff / reboot) for the PFD user — nothing else.

set -e

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: run as root: sudo bash $0"
    exit 1
fi

# The user the pfd.service runs as — the human who invoked sudo, or 'pi'.
RUN_USER="${SUDO_USER:-pi}"

DROPIN=/etc/sudoers.d/pfd-power
cat > "$DROPIN" << SUDOEOF
$RUN_USER ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff, /usr/bin/systemctl reboot
SUDOEOF
chmod 440 "$DROPIN"

if visudo -cf "$DROPIN" >/dev/null 2>&1; then
    echo "→ installed $DROPIN for user '$RUN_USER'"
    echo "  SHUTDOWN / REBOOT buttons on the SYSTEM screen will now work."
else
    echo "! sudoers syntax check failed — removing $DROPIN"
    rm -f "$DROPIN"
    exit 1
fi
