#!/usr/bin/env bash
# fetch_sedona_tiles.sh – Download the 4 SRTM3 elevation tiles covering
# the Sedona AZ area for demo mode terrain rendering.
#
# Tiles: N34W112, N34W111, N35W112, N35W111
# Size: ~2.8 MB each, ~11 MB total (unzipped .hgt files)
# Output: pi_display/data/srtm/
#
# Usage:
#   bash fetch_sedona_tiles.sh
#
# Sources tried in order:
#   1. Mapzen/Nextzen AWS public bucket (same source as in-app downloader)
#   2. USGS SRTM3 direct
#   3. ESA STEP auxiliary data mirror
#   4. NASA EarthData via ~/.netrc credentials (if configured)

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

    # Source 1: Mapzen/Nextzen AWS public bucket (.hgt.gz, no auth)
    # Same source used by the in-app terrain downloader
    local LAT_FOLDER="${TILE:0:3}"   # e.g. N34
    local URL1="https://elevation-tiles-prod.s3.amazonaws.com/skadi/${LAT_FOLDER}/${TILE}.hgt.gz"

    # Source 2: USGS SRTM3 North America (.hgt.zip, no auth)
    local URL2="https://dds.cr.usgs.gov/srtm/version2_1/SRTM3/North_America/${TILE}.hgt.zip"

    # Source 3: ESA STEP mirror (.hgt.zip, no auth)
    local URL3="https://step.esa.int/auxdata/dem/SRTM90/hgt/${TILE}.hgt.zip"

    # Source 4: NASA EarthData SRTM3 (.hgt.zip, requires ~/.netrc credentials)
    # To set up: echo "machine urs.earthdata.nasa.gov login YOUR_USER password YOUR_PASS" >> ~/.netrc
    #            chmod 600 ~/.netrc
    local URL4="https://e4ftl01.cr.usgs.gov/MEASURES/SRTMGL3.003/2000.02.11/${TILE}.SRTMGL3.hgt.zip"

    local TMPFILE
    TMPFILE="$(mktemp /tmp/srtm_XXXXXX)"

    # Try Mapzen first (.hgt.gz — gunzip directly to destination)
    if curl -fsSL --retry 3 --retry-delay 2 -o "${TMPFILE}.gz" "$URL1" 2>/dev/null; then
        if gunzip -c "${TMPFILE}.gz" > "$DEST" 2>/dev/null; then
            rm -f "${TMPFILE}.gz"
            echo "OK — Mapzen AWS ($(du -h "$DEST" | cut -f1))"
            return 0
        fi
        rm -f "${TMPFILE}.gz" "$DEST"
    fi

    # Try USGS and ESA (.hgt.zip — extract from zip)
    for URL in "$URL2" "$URL3"; do
        if curl -fsSL --retry 3 --retry-delay 2 -o "${TMPFILE}.zip" "$URL" 2>/dev/null; then
            HGT=$(unzip -Z1 "${TMPFILE}.zip" 2>/dev/null | grep -i '\.hgt$' | head -1)
            if [ -n "$HGT" ]; then
                unzip -p "${TMPFILE}.zip" "$HGT" > "$DEST"
                rm -f "${TMPFILE}.zip"
                echo "OK — USGS/ESA ($(du -h "$DEST" | cut -f1))"
                return 0
            fi
            rm -f "${TMPFILE}.zip"
        fi
    done

    # Try NASA EarthData (requires ~/.netrc)
    if [ -f ~/.netrc ] && grep -q "earthdata.nasa.gov" ~/.netrc 2>/dev/null; then
        if curl -fsSL --netrc --location --retry 3 --retry-delay 2 \
                -o "${TMPFILE}.zip" "$URL4" 2>/dev/null; then
            HGT=$(unzip -Z1 "${TMPFILE}.zip" 2>/dev/null | grep -i '\.hgt$' | head -1)
            if [ -n "$HGT" ]; then
                unzip -p "${TMPFILE}.zip" "$HGT" > "$DEST"
                rm -f "${TMPFILE}.zip"
                echo "OK — NASA EarthData ($(du -h "$DEST" | cut -f1))"
                return 0
            fi
            rm -f "${TMPFILE}.zip"
        fi
    fi

    rm -f "${TMPFILE}" "${TMPFILE}.gz" "${TMPFILE}.zip"
    echo "FAILED — all sources unavailable"
    echo ""
    echo "    Manual download options:"
    echo "    A) NASA EarthData (free account):"
    echo "       1. Add credentials to ~/.netrc:"
    echo "          echo \"machine urs.earthdata.nasa.gov login YOUR_USER password YOUR_PASS\" >> ~/.netrc"
    echo "          chmod 600 ~/.netrc"
    echo "       2. Re-run this script"
    echo ""
    echo "    B) Download manually from https://dwtkns.com/srtm30m/"
    echo "       Search for ${TILE}, download the .zip, extract the .hgt file,"
    echo "       and copy it to: $SRTM_DIR/"
    echo ""
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
    echo "$FAILED tile(s) failed."
    echo ""
    echo "To use your NASA EarthData account for future downloads:"
    echo "  echo \"machine urs.earthdata.nasa.gov login YOUR_USER password YOUR_PASS\" >> ~/.netrc"
    echo "  chmod 600 ~/.netrc"
    echo "  bash fetch_sedona_tiles.sh"
fi
