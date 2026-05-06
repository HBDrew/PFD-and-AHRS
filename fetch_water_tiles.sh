#!/usr/bin/env bash
# fetch_water_tiles.sh – Download Natural Earth ocean + lakes vectors
# and rasterise them into per-tile binary water masks for the PFD's
# OpenGL SVT renderer.  Companion to fetch_sedona_tiles.sh / the
# in-app TERRAIN DATA download.
#
# One-time download: ~12 MB of Natural Earth shapefiles.
# Per-tile output:  ~180 KB .water masks (1201×1201 bit-packed).
#
# Usage:
#   bash fetch_water_tiles.sh                       # Sedona area, both versions
#   bash fetch_water_tiles.sh pi4                   # Sedona area, pi4 only
#   bash fetch_water_tiles.sh --bbox N33W113 N36W110 pi4
#   bash fetch_water_tiles.sh --tiles "N34W112 N34W111" pi4

set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
SHAPE_DIR="$REPO_DIR/data/natural_earth"
mkdir -p "$SHAPE_DIR"

# ── Parse args ────────────────────────────────────────────────────────────────
MODE="tiles"
TILES_ARG=""
BBOX_ARG=""
VERSIONS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --tiles)  MODE="tiles"; TILES_ARG="$2"; shift 2 ;;
        --bbox)   MODE="bbox";  BBOX_ARG="$2 $3"; shift 3 ;;
        pi4|pi_zero) VERSIONS+=("$1"); shift ;;
        *) echo "Unknown arg: $1"; exit 2 ;;
    esac
done
[ "${#VERSIONS[@]}" -eq 0 ] && VERSIONS=("pi_zero" "pi4")
# Default Sedona-area tiles if none specified
if [ -z "$TILES_ARG" ] && [ -z "$BBOX_ARG" ]; then
    TILES_ARG="N34W112 N34W111 N35W112 N35W111"
fi

# ── 1. Check for gdal_rasterize ────────────────────────────────────────────────
if ! command -v gdal_rasterize >/dev/null 2>&1; then
    echo "ERROR: gdal_rasterize not found."
    echo "  Install with: sudo apt-get install gdal-bin"
    exit 1
fi

# ── 2. Download Natural Earth shapefiles (one-time) ──────────────────────────
download_ne() {
    local NAME="$1"
    local ZIP="$SHAPE_DIR/${NAME}.zip"
    local SHP="$SHAPE_DIR/${NAME}.shp"
    if [ -f "$SHP" ]; then
        echo "  ${NAME}.shp — already present"
        return 0
    fi

    # Primary: official Natural Earth CDN
    local URL1="https://naciscdn.org/naturalearth/10m/physical/${NAME}.zip"
    # Mirror: GitHub release of the natural-earth-vector repo
    local URL2="https://github.com/nvkelso/natural-earth-vector/raw/master/zips/10m_physical/${NAME}.zip"

    echo -n "  Fetching ${NAME}.zip … "
    for URL in "$URL1" "$URL2"; do
        if curl -fsSL --retry 3 --retry-delay 2 -o "$ZIP" "$URL" 2>/dev/null; then
            unzip -q -o "$ZIP" -d "$SHAPE_DIR"
            rm -f "$ZIP"
            if [ -f "$SHP" ]; then
                echo "OK ($(du -h "$SHP" | cut -f1))"
                return 0
            fi
        fi
    done
    echo "FAILED — could not fetch from any source"
    return 1
}

echo "[1/2] Downloading Natural Earth physical vectors…"
download_ne "ne_10m_ocean"
download_ne "ne_10m_lakes"
echo ""

# ── 3. Rasterise into per-tile water masks ────────────────────────────────────
WATER_DIR="$REPO_DIR/${VERSIONS[0]}/data/water"
mkdir -p "$WATER_DIR"

echo "[2/2] Rasterising water masks → $WATER_DIR"
if [ "$MODE" = "bbox" ]; then
    # shellcheck disable=SC2086
    python3 "$REPO_DIR/tools/build_water_tiles.py" \
        --shapes "$SHAPE_DIR" --out "$WATER_DIR" --bbox $BBOX_ARG
else
    # shellcheck disable=SC2086
    python3 "$REPO_DIR/tools/build_water_tiles.py" \
        --shapes "$SHAPE_DIR" --out "$WATER_DIR" --tiles $TILES_ARG
fi

# ── 4. Mirror to additional version dirs ──────────────────────────────────────
for V in "${VERSIONS[@]:1}"; do
    OTHER="$REPO_DIR/$V/data/water"
    if [ "$OTHER" != "$WATER_DIR" ]; then
        mkdir -p "$OTHER"
        cp -n "$WATER_DIR"/*.water "$OTHER/" 2>/dev/null || true
        echo "Mirrored masks → $OTHER"
    fi
done

echo ""
echo "Done.  SVT will draw water bodies the next time pfd.py starts."
