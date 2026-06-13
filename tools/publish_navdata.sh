#!/usr/bin/env bash
#
# publish_navdata.sh — upload a built nav-data cache to the GitHub release the
# in-app NAV DATA screen downloads from.
#
# The device fetches the three cache files from a FIXED release tag ("navdata")
# — see DOWNLOAD_BASE_URL in shared/navdata.py.  This script uploads (or
# re-uploads, --clobber) the assets to that tag, so the download URL never
# changes from one 28-day cycle to the next.  Run it on your laptop after
# building the cache; it needs the `gh` CLI authenticated to this repo.
#
# Usage:
#   tools/publish_navdata.sh [CACHE_DIR]
#       CACHE_DIR   dir holding the built cache  (default: pi4/data/navdata)
#
# Typical 28-day refresh:
#   python3 tools/build_navdata_us.py --nasr <NASR_dir> --cifp <FAACIFP18> \
#       --out pi4/data/navdata
#   tools/publish_navdata.sh pi4/data/navdata
#
set -euo pipefail

TAG="navdata"
REPO="HBDrew/PFD-and-AHRS"
DIR="${1:-pi4/data/navdata}"

FILES=(navdata_fixes.npy navdata_navaids.npy navdata.json)

# Verify the cache is present before touching the release.
for f in "${FILES[@]}"; do
    if [[ ! -f "$DIR/$f" ]]; then
        echo "ERROR: $DIR/$f not found — build the cache first" >&2
        echo "  python3 tools/build_navdata_us.py --nasr <dir> --cifp <FAACIFP18> --out $DIR" >&2
        exit 1
    fi
done

# Read the cycle stamp out of the cache for the release title/notes.
CYCLE="$(python3 - "$DIR/navdata.json" <<'PY'
import json, sys
try:
    print(json.load(open(sys.argv[1])).get("cycle", "") or "unknown")
except Exception:
    print("unknown")
PY
)"

ASSETS=()
for f in "${FILES[@]}"; do ASSETS+=("$DIR/$f"); done

if gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1; then
    echo "Updating release '$TAG' (cycle $CYCLE) …"
    gh release upload "$TAG" "${ASSETS[@]}" --repo "$REPO" --clobber
    gh release edit "$TAG" --repo "$REPO" \
        --title "Nav data (cycle $CYCLE)" \
        --notes "FAA NASR + CIFP cache · cycle $CYCLE · $(date -u +%Y-%m-%d)"
else
    echo "Creating release '$TAG' (cycle $CYCLE) …"
    gh release create "$TAG" "${ASSETS[@]}" --repo "$REPO" \
        --title "Nav data (cycle $CYCLE)" \
        --notes "FAA NASR + CIFP cache · cycle $CYCLE · $(date -u +%Y-%m-%d)"
fi

echo
echo "Done.  Devices will now download cycle $CYCLE from:"
echo "  https://github.com/$REPO/releases/download/$TAG/"
