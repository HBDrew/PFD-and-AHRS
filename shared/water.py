"""
water.py – Water-mask loader for SVT terrain rendering.

Companion dataset to terrain.py SRTM tiles.  Each 1°×1° tile is a
binary mask (1 = water, 0 = land) at SRTM3 resolution (1201×1201).
Source data: Natural Earth 10m physical vectors (ocean + lakes),
rasterised at install time by tools/build_water_tiles.py.

File format (NXXEYYY.water):
    bytes  0–3:  width  (int32 little-endian, normally 1201)
    bytes  4–7:  height (int32 little-endian, normally 1201)
    bytes  8– :  bit-packed mask, MSB-first, row-major (numpy.packbits)

Bit-packed storage so a global CONUS download is small:
    1201² bits = 180 304 bytes ≈ 180 KB / tile
    25-tile "current area" download ≈ 4.5 MB
    Whole CONUS (~600 tiles) ≈ 110 MB

The renderer samples a per-vertex water flag alongside elevation when
it builds the terrain mesh (svt_renderer_gl._build_tier_mesh) and the
fragment shader paints water with a dedicated palette.
"""

import os
import struct

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False


WATER_TILE_RES = 1201       # samples per side (matches SRTM3)
WATER_FILE_EXT = ".water"


# ── Tile cache (keyed identically to terrain._tile_key) ───────────────────────
_tile_cache: dict = {}


def _tile_key(lat_int: int, lon_int: int) -> str:
    ns = "N" if lat_int >= 0 else "S"
    ew = "E" if lon_int >= 0 else "W"
    return f"{ns}{abs(lat_int):02d}{ew}{abs(lon_int):03d}{WATER_FILE_EXT}"


def load_tile(water_dir: str, lat_int: int, lon_int: int):
    """Return (mask, n_samples) for the given tile or None if missing.

    `mask` is a numpy uint8 array (n×n, values 0/1) when numpy is
    available; falls back to a flat bytes object otherwise.
    """
    if not water_dir:
        return None
    key = _tile_key(lat_int, lon_int)
    if key in _tile_cache:
        return _tile_cache[key]

    path = os.path.join(water_dir, key)
    if not os.path.exists(path):
        _tile_cache[key] = None
        return None

    try:
        with open(path, "rb") as f:
            header = f.read(8)
            if len(header) != 8:
                raise ValueError("short header")
            w, h = struct.unpack("<ii", header)
            packed = f.read()
    except OSError:
        _tile_cache[key] = None
        return None

    if HAS_NUMPY:
        bits = np.unpackbits(np.frombuffer(packed, dtype=np.uint8))
        if bits.size < h * w:
            _tile_cache[key] = None
            return None
        mask = bits[: h * w].reshape(h, w).astype(np.uint8)
    else:
        # Pure-Python fallback: keep as raw bytes.  Sampling is only used by
        # the GL renderer (which needs numpy anyway), so this branch is
        # mostly for the shared/terrain.py CLI consumers that might import.
        mask = packed

    result = (mask, w)
    _tile_cache[key] = result
    return result


def is_water(water_dir: str, lat: float, lon: float) -> bool:
    """Single-point water lookup.  Returns False when no tile is loaded."""
    if not HAS_NUMPY:
        return False
    import math
    lat_int = int(math.floor(lat))
    lon_int = int(math.floor(lon))
    res = load_tile(water_dir, lat_int, lon_int)
    if res is None:
        return False
    mask, n = res
    step = 1.0 / (n - 1)
    row = int(round((lat_int + 1 - lat) / step))
    col = int(round((lon - lon_int) / step))
    row = max(0, min(n - 1, row))
    col = max(0, min(n - 1, col))
    return bool(mask[row, col])


# ── Writer (used by tools/build_water_tiles.py) ───────────────────────────────

def save_tile(path: str, mask) -> None:
    """Write a (h, w) uint8 0/1 array to disk in the .water format."""
    if not HAS_NUMPY:
        raise RuntimeError("save_tile requires numpy")
    arr = np.asarray(mask, dtype=np.uint8)
    if arr.ndim != 2:
        raise ValueError("mask must be 2-D")
    h, w = arr.shape
    packed = np.packbits(arr.flatten())
    with open(path, "wb") as f:
        f.write(struct.pack("<ii", w, h))
        f.write(packed.tobytes())


def disk_stats(water_dir: str):
    """Return (n_tiles, used_mb) for the water-tile directory."""
    if not water_dir or not os.path.isdir(water_dir):
        return 0, 0.0
    n = 0
    total = 0
    for name in os.listdir(water_dir):
        if name.endswith(WATER_FILE_EXT):
            n += 1
            try:
                total += os.path.getsize(os.path.join(water_dir, name))
            except OSError:
                pass
    return n, total / (1024 * 1024)
