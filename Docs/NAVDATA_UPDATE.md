# Updating IFR Nav Data (28-day cycle)

The nav-data cache (named fixes, navaids, Victor/Jet airways, published
approaches + holding patterns) comes from two **free, US-only** FAA products
that refresh every **28 days**. This is the step-by-step to pull a new cycle
and get it onto all three displays (Pi 4 / Pi 5 / Pi Zero).

You only need to do this when you want the latest cycle — the in-app badge on
**Setup → DATA & MAPS → NAV DATA** turns orange ("expired") once the loaded
cycle is past its useful life (~56 days from the cycle's issue date, because
FAA publishes each cycle ~28 days before it becomes effective).

There are two ways to publish a new cycle:

- **A. From an iPad / phone / any browser** — GitHub Actions does the build.
  No laptop, no command line. *Recommended.*
- **B. From a laptop** — build locally and run a one-line publish script.

Either way, once published, each Pi just taps **DOWNLOAD/UPDATE** to pull it.

---

## Step 1 — Grab the two FAA files (links)

You need the current-cycle download link for **two** products. Both are on
faa.gov. Open each page, find the **current 28-day cycle**, and copy the ZIP
link (on iPad: long-press the link → **Copy Link**).

1. **NASR Subscription (CSV)** — fixes, navaids, airways
   <https://www.faa.gov/air_traffic/flight_info/aeronav/aero_data/NASR_Subscription/>
   - Pick the current **"28 Day NASR Subscription"** entry.
   - Copy the **CSV** ZIP link (not the "Additional" or shapefile ones). The
     ZIP must contain `FIX_BASE.csv`, `NAV_BASE.csv`, `AWY_BASE.csv`,
     `AWY_SEG.csv`.

2. **CIFP (Coded Instrument Flight Procedures)** — approaches + holds
   <https://www.faa.gov/air_traffic/flight_info/aeronav/digital_products/cifp/download/>
   - Pick the **Current** cycle.
   - Copy that cycle's ZIP link. The ZIP contains a file named `FAACIFP18`.

> Cycles are dated by their **effective** date. If you're updating a few days
> before a new cycle goes live, you can grab the *next* cycle so you're current
> for the trip — both pages list upcoming cycles.

---

## Step 2A — Publish from an iPad / browser (recommended)

1. In **Safari**, open the repo: <https://github.com/HBDrew/PFD-and-AHRS>
   - Use the **desktop site** (tap **aA** → *Request Desktop Website*). The
     GitHub iOS app can't fill in workflow inputs — Safari can.
2. Go to the **Actions** tab → pick **"Publish nav data"** in the left list.
3. Tap **Run workflow** (top right of the runs list). A small form drops down:
   - **nasr_url** → paste the NASR CSV ZIP link from Step 1.
   - **cifp_url** → paste the CIFP ZIP link from Step 1.
   - **cycle** → leave blank (it's auto-detected from the CIFP header).
4. Tap the green **Run workflow** button.
5. Wait ~1–3 min. Refresh; the run shows a **green check** when done (open it
   to see the cycle it published, e.g. "Nav data (cycle 2606)").
   - If it fails, open the failed step — the error says exactly what's wrong
     (usually a wrong/expired link). Re-copy the link and run it again.

That's it — the `navdata` release now holds the new cycle. Go to Step 3.

---

## Step 2B — Publish from a laptop (alternative)

Needs Python 3.8+, `numpy`, and the [`gh` CLI](https://cli.github.com/)
authenticated to this repo (`gh auth login`).

```bash
# 1. Download + unzip both FAA files (from the Step 1 links).
#    You'll have an unzipped NASR folder and a FAACIFP18 file.

# 2. Build the cache:
python3 tools/build_navdata_us.py \
    --nasr /path/to/unzipped_NASR_dir \
    --cifp /path/to/FAACIFP18 \
    --out  pi4/data/navdata

# 3. Publish it to the "navdata" release:
tools/publish_navdata.sh pi4/data/navdata
```

---

## Step 3 — Pull it onto each display

With the release published, every Pi can self-update **as long as it has
internet** (home WiFi is fine):

On **each** of the Pi 4, Pi 5, and Pi Zero:
1. **Setup → DATA & MAPS → NAV DATA**
2. Tap **DOWNLOAD** (first time) / **UPDATE** (refresh).
3. Watch the progress bar → **"Done ✓ cycle NNNN"**.
4. Confirm the status line shows the new **cycle** and a green (not orange)
   age badge.

### Offline fallback (no internet on a device)

If a display can't reach the internet, copy the three cache files onto it from
a Pi that already has them. They live in `<device>/data/navdata/`:

- `navdata_fixes.npy`
- `navdata_navaids.npy`
- `navdata.json`

Example, pushing from the Pi 4 to the Zero over the LAN (use the Zero's IP and
your real user/path):

```bash
cd ~/PFD-and-AHRS
ssh pi@<zero-ip> 'mkdir -p ~/PFD-and-AHRS/pi_zero/data/navdata'
scp pi4/data/navdata/navdata_fixes.npy \
    pi4/data/navdata/navdata_navaids.npy \
    pi4/data/navdata/navdata.json \
    pi@<zero-ip>:~/PFD-and-AHRS/pi_zero/data/navdata/
```

Then on that device open **Setup → DATA & MAPS → NAV DATA** to load it (it also
loads automatically on next boot).

---

## Quick reference

| What | Where |
|------|-------|
| NASR CSV (fixes/navaids/airways) | faa.gov → AeroNav → NASR Subscription |
| CIFP (approaches/holds), file `FAACIFP18` | faa.gov → AeroNav → Digital Products → CIFP |
| Publish from browser/iPad | repo → Actions → **Publish nav data** → Run workflow |
| Publish from laptop | `tools/build_navdata_us.py` then `tools/publish_navdata.sh` |
| Pull onto a Pi | **Setup → DATA & MAPS → NAV DATA → DOWNLOAD/UPDATE** |
| Cache files on disk | `<pi4\|pi_zero>/data/navdata/navdata_{fixes,navaids}.npy`, `navdata.json` |
| Release the app downloads from | tag **`navdata`** (fixed; URL never changes) |
