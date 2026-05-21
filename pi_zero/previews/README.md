# Pi Zero 2W — Preview Screenshots

Screenshots from the Pi Zero 2W version of the PFD (no SVT terrain
background).  The attitude indicator shows a plain sky/ground horizon
split.

## Regenerating

These PNGs are checked into git so the user manual renders correctly on
GitHub.  To regenerate them after a UI / scene change:

```bash
./tools/regen_previews.sh piz
```

That runs `pi_zero/pfd.py --screenshots pi_zero/previews/` with
`SDL_VIDEODRIVER=dummy` so it works headless — on the piZ itself, on
the pi4, or on any desktop dev machine with `python3-pygame` and
`libsdl2-dev` installed.  The piZ render path is pure pygame (no GL),
so the captured PNGs are visually identical regardless of host.
