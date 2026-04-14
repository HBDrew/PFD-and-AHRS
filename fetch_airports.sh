#!/usr/bin/env bash
# fetch_airports.sh — Download the OurAirports.com global airport CSV.
#
# Source: https://davidmegginson.github.io/ourairports-data/airports.csv
#   (mirror of https://ourairports.com/data/airports.csv, free, public)
#
# About 3 MB, ~80K airports worldwide including the USA's ~20K.
# Parsed on first load into a compact numpy cache (airports_cache.npy).
#
# Usage:
#   bash fetch_airports.sh                    # downloads to both pi_zero and pi4
#   bash fetch_airports.sh pi4                # only pi4
#   bash fetch_airports.sh pi_zero            # only pi_zero
#
# Data is shared between both display versions so you can keep a single copy
# in one directory and symlink if you prefer.

set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
URL="https://raw.githubusercontent.com/davidmegginson/ourairports-data/main/airports.csv"

VERSIONS=("pi_zero" "pi4")
if [ $# -gt 0 ]; then
  VERSIONS=("$@")
fi

for V in "${VERSIONS[@]}"; do
  DEST_DIR="$REPO_DIR/$V/data/airports"
  mkdir -p "$DEST_DIR"
  DEST="$DEST_DIR/airports.csv"

  echo "Downloading airport database → $DEST"
  if command -v curl >/dev/null 2>&1; then
    curl -L --fail --silent --show-error -o "$DEST" "$URL"
  elif command -v wget >/dev/null 2>&1; then
    wget -q -O "$DEST" "$URL"
  else
    echo "ERROR: need curl or wget."; exit 1
  fi

  size=$(stat -c%s "$DEST" 2>/dev/null || stat -f%z "$DEST")
  echo "  → $(printf '%.1f' $(echo "$size / 1024 / 1024" | bc -l)) MB"

  # Invalidate the old numpy cache so it rebuilds on next launch
  rm -f "$DEST_DIR/airports_cache.npy"
done

echo ""
echo "Done. The PFD will parse and cache on first launch."
