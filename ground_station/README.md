# PFD Ground Station

A desktop companion app that **builds**, **deploys**, and **publishes** the
flight data the PFD/AHRS consumes — so the 28-day data refresh is buttons, not
a terminal.

One card per data product:

| Product | Source you supply | Auto-download |
|---|---|---|
| **Nav Data** | FAA NASR folder + CIFP (`FAACIFP18`) | — |
| **Airspace** | folder of FAA `*.geojson` | — |
| **Airports** | — | OurAirports CSV |
| **Obstacles** | — | FAA Digital Obstacle File |
| **Terrain (SRTM)** | folder of raw `.hgt` tiles | — |
| **Water Tiles** | Natural Earth water `.shp` | — |

For each: **Build** writes the cache into a workspace, **Deploy** copies it into
the connected checkout's `pi4/data/…` and `pi_zero/data/…` folders, and (nav
data only) **Publish →GitHub** uploads it to the release the on-device NAV DATA
screen downloads from.

## Why this exists (nav data in particular)

Airports / obstacles / terrain / airspace can be fetched by the device itself
from public sources. **Nav data can't** — the FAA only publishes IFR data
(fixes, airways, approaches, holds) as large raw subscription files too big to
parse on a Pi. So it must be built on a real computer once per cycle, then
either copied to the device or published for it to download. This app does that
build.

## Run from source

```bash
python3 -m ground_station          # or:  python3 run_ground_station.py
```
Needs Python 3.8+, `numpy`, and Tk (stdlib; on Linux: `apt install python3-tk`).
Run it from inside a repo checkout so **Deploy** can see the `pi4` / `pi_zero`
data dirs.

## Build a standalone app (no Python needed on the target PC)

```bash
pip install pyinstaller
pyinstaller ground_station/pfd_ground_station.spec
# → dist/PFD-Ground-Station(.exe)
```
Drop the executable inside (or next to) a repo checkout so **Deploy** can find
the device data dirs; **Build** and **Publish** work from anywhere.

## The 28-day nav-data refresh

1. Download the current **NASR Subscription** (CSV zip) and **CIFP** zip from
   faa.gov (both free). Unzip them.
2. In the app: point **Nav Data** at the NASR folder and the `FAACIFP18` file,
   click **Build**.
   - First time only: validate the CIFP column offsets — see the note in
     `tools/build_navdata_us.py` (`--dump-cifp`).
3. Click **Deploy** (copies onto the Pi data dir / SD card) **and/or**
   **Publish →GitHub** (needs a token with `repo` / Contents:write scope; the
   device then pulls it via NAV DATA → DOWNLOAD).

Settings (token, last-used paths, deploy targets) are remembered in
`~/.pfd_ground_station.json`.
