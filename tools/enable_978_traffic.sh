#!/usr/bin/env bash
# enable_978_traffic.sh – feed dump978's UAT downlinks into readsb as traffic.
#
# dump978 already receives 978 UAT *traffic* (the '-' downlink frames), but
# readsb is 1090-only and our weather bridge only forwards FIS-B uplinks, so
# that traffic goes unused.  readsb can ingest dump978's raw output directly via
# a `uat_in` net-connector and fold it into its existing SBS :30003 output —
# which the adsb-gdl90 bridge already turns into GDL90 on :4000.  So 978 traffic
# rides the *same* path as 1090, no new decode code.
#
# This only ADDS one connector to readsb; the 1090 receive path is unchanged.
# Idempotent: safe to re-run.  Run:  sudo bash tools/enable_978_traffic.sh

set -e

if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: run as root: sudo bash tools/enable_978_traffic.sh"; exit 1
fi

DEFAULT="/etc/default/readsb"
CONNECTOR="--net-connector 127.0.0.1,30978,uat_in"
MARKER="# PFD: ingest dump978 UAT (978 traffic) into readsb"

if [ ! -f "$DEFAULT" ]; then
  cat <<EOF
ERROR: $DEFAULT not found — this helper expects the wiedehopf readsb install.
       Add the connector to your readsb command manually instead:
         $CONNECTOR
       (readsb must be built with UAT support; wiedehopf's is.)
EOF
  exit 1
fi

if grep -q "30978,uat_in" "$DEFAULT"; then
  echo "Already enabled — '$DEFAULT' already has a 30978 uat_in connector."
  exit 0
fi

if ! grep -q "NET_OPTIONS" "$DEFAULT"; then
  cat <<EOF
ERROR: no NET_OPTIONS line in $DEFAULT — can't safely extend it.
       Add this connector to readsb's networking options by hand:
         $CONNECTOR
EOF
  exit 1
fi

cp -a "$DEFAULT" "$DEFAULT.bak.$(date +%Y%m%d%H%M%S)"

# Append a line that EXTENDS NET_OPTIONS (the file is sourced as shell), so we
# never have to parse/rewrite the existing value — whatever it was, we just add
# our connector to it.
cat >> "$DEFAULT" <<EOF

$MARKER
NET_OPTIONS="\$NET_OPTIONS $CONNECTOR"
EOF

echo "Added uat_in connector to $DEFAULT (backup saved alongside)."
systemctl restart readsb
echo "readsb restarted."

cat <<EOF

Verify:
  systemctl status readsb                          # active, no errors
  # 978 aircraft now appear in readsb's SBS feed and flow to the display via
  # the existing adsb-gdl90 bridge — check the MFD ADS-B 'R' count climbs when
  # UAT traffic is around (it shares the radio 'R' tally with 1090).

Revert: remove the two added lines at the bottom of $DEFAULT (or restore the
.bak file) and 'systemctl restart readsb'.
EOF
