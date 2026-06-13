#!/usr/bin/env python3
"""Launcher for the PFD Ground Station desktop app.

Run from source:   python3 run_ground_station.py   (or: python3 -m ground_station)
Packaged:          this is the PyInstaller entry point — see
                   ground_station/pfd_ground_station.spec.
"""

from ground_station.app import main

if __name__ == "__main__":
    main()
