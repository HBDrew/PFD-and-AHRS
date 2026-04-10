#!/usr/bin/env bash
# fetch_sedona_tiles.sh – Download the 4 SRTM3 elevation tiles covering
# the Sedona AZ area for demo mode terrain rendering.
#
# Tiles: N34W112, N34W111, N35W112, N35W111
# Size: ~2.8 MB each, ~11 MB total (unzipped .hgt files)
# Output: pi_display/data/srtm/
#
# Usage: bash fetch_sedona_tiles.sh
#
# Sources tried in order:
#   1. NASA SRTM via USGS (most reliable, no login needed for these tiles)
#   2. ESA STEP auxiliary data mirror
#   3. CGIAR-CSI mirror (SRTM 90m)

set -e

SRTM_DIR="$(cd "$(dirname "$0")" && pwd)/pi_display/data/srtm"
mkdir -p "$SRTM_DIR"
echo "Output directory: $SRTM_DIR"
echo ""

# 4 tiles that cover Sedona, Oak Creek Canyon, Jerome, Cottonwood, KSEZ
TILES=("N34W112" "N34W111" "N35W112" "N35W111")

fetch_tile() {
    local TILE="$1"
    local DEST="$SRTM_DIR/${TILE}.hgt"

    if [ -f "$DEST" ]; then
        echo "  ${TILE}.hgt — already present ($(du -h "$DEST" | cut -f1))"
        return 0
    fi

    echo -n "  Fetching ${TILE}.hgt … "

    # Source 1: USGS EarthExplorer direct (version 2.1, North America)
    local URL1="https://dds.cr.usgs.gov/srtm/version2_1/SRTM3/North_America/${TILE}.hgt.zip"
    # Source 2: ESA STEP (no auth needed for SRTM3)
    local URL2="https://step.esa.int/auxdata/dem/SRTM90/hgt/${TILE}.hgt.zip"
    # Source 3: OpenTopography via direct HGT zip
    local URL3="https://srtm.csi.cgiar.org/wp-content/uploads/files/srtm_5x5/TIFF/${TILE}.zip"

    local TMPZIP
    TMPZIP="$(mktemp /tmp/srtm_XXXXXX.zip)"

    for URL in "$URL1" "$URL2"; do
        if curl -fsSL --retry 3 --retry-delay 2 -o "$TMPZIP" "$URL" 2>/dev/null; then
            # Extract .hgt from zip
            HGT=$(unzip -Z1 "$TMPZIP" 2>/dev/null | grep -i '\.hgt$' | head -1)
            if [ -n "$HGT" ]; then
                unzip -p "$TMPZIP" "$HGT" > "$DEST"
                rm -f "$TMPZIP"
                echo "OK ($(du -h "$DEST" | cut -f1))"
                return 0
            fi
        fi
    done

    rm -f "$TMPZIP"
    echo "FAILED"
    echo "    Could not download ${TILE}.hgt from any source."
    echo "    Manual download: https://dwtkns.com/srtm30m/ (free NASA EarthData account)"
    echo "    Place the .hgt file in: $SRTM_DIR/"
    return 1
}

FAILED=0
for TILE in "${TILES[@]}"; do
    fetch_tile "$TILE" || FAILED=$((FAILED + 1))
done

echo ""
if [ "$FAILED" -eq 0 ]; then
    echo "All 4 Sedona SRTM tiles downloaded successfully."
    echo "Demo mode will now show real terrain. Run:"
    echo "  python3 pi_display/pfd.py --demo --sim"
else
    echo "$FAILED tile(s) failed. Demo will use gradient sky/ground fallback."
    echo "Manual source: https://dwtkns.com/srtm30m/"
    echo "  (free NASA EarthData account, search for N34W112 etc.)"
fi
