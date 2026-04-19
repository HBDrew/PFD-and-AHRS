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
APT_URL="https://raw.githubusercontent.com/davidmegginson/ourairports-data/main/airports.csv"
RWY_URL="https://raw.githubusercontent.com/davidmegginson/ourairports-data/main/runways.csv"

VERSIONS=("pi_zero" "pi4")
if [ $# -gt 0 ]; then
  VERSIONS=("$@")
fi

fetch_one() {
  local url="$1"
  local dest="$2"
  local label="$3"
  echo "Downloading $label → $dest"
  if command -v curl >/dev/null 2>&1; then
    curl -L --fail --silent --show-error -o "$dest" "$url"
  elif command -v wget >/dev/null 2>&1; then
    wget -q -O "$dest" "$url"
  else
    echo "ERROR: need curl or wget."; exit 1
  fi
  size=$(stat -c%s "$dest" 2>/dev/null || stat -f%z "$dest")
  echo "  → $(python3 -c "print(f'{$size/1048576:.1f}')" 2>/dev/null || echo "?") MB"
}

for V in "${VERSIONS[@]}"; do
  DEST_DIR="$REPO_DIR/$V/data/airports"
  mkdir -p "$DEST_DIR"

  fetch_one "$APT_URL" "$DEST_DIR/airports.csv" "airports"
  fetch_one "$RWY_URL" "$DEST_DIR/runways.csv"  "runways"

  # Invalidate old numpy caches so they rebuild on next launch
  rm -f "$DEST_DIR/airports_cache.npy" "$DEST_DIR/runways_cache.npy"
done

echo ""
echo "Done. The PFD will parse and cache on first launch."
