# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for the PFD Ground Station.
# Build (from anywhere):
#     pip install pyinstaller
#     pyinstaller ground_station/pfd_ground_station.spec
# Output: dist/PFD-Ground-Station(.exe)
#
import os

# SPECPATH is injected by PyInstaller = the dir holding this spec file.
HERE = SPECPATH
REPO = os.path.dirname(HERE)

block_cipher = None

a = Analysis(
    [os.path.join(REPO, "run_ground_station.py")],
    pathex=[REPO, os.path.join(REPO, "tools"), os.path.join(REPO, "shared")],
    binaries=[],
    datas=[],
    # The build tools + runtime modules are imported by string-stable top-level
    # imports, but list them so PyInstaller's analysis never misses one.
    hiddenimports=[
        "build_navdata_us", "build_airspaces_us", "compact_srtm",
        "build_water_tiles",
        "navdata", "airports", "obstacles", "airspaces", "water",
        "numpy",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["pygame", "OpenGL"],   # the GUI doesn't need the render stack
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="PFD-Ground-Station",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    runtime_tmpdir=None,
    console=False,        # windowed GUI app (no console)
    disable_windowed_traceback=False,
)
