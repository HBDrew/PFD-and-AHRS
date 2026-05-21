# Pi 4 — Preview Screenshots

Screenshots from the Pi 4 version of the PFD with full Synthetic Vision
Terrain (SVT) rendered via OpenGL.

## Regenerating

These PNGs are checked into git so the user manuals render correctly on
GitHub.  To regenerate them after a UI / scene change, run on the pi4:

```bash
./tools/regen_previews.sh pi4
```

That wraps `tools/capture_pi4_previews.sh`, which uses the offline GL
renderer (`pi4/render_pfd_offline.py`) with `SDL_VIDEODRIVER=dummy` +
an offscreen EGL context so the captured PNGs include the full 3D SVT
terrain.  Must be run on a pi4 (needs the V3D driver).  The script
stops `pfd.service` for the capture session and brings it back on exit.
