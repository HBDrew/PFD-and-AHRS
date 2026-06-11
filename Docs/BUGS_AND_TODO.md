# Bugs & TODO

Tracked in git so nothing is lost across Claude Code sessions.
When opening a new session, start here for context.

Format: each item gets a short ID, a status, a one-line summary, and
notes with enough context to pick it up cold.

---

## Open

### WX-SOURCE-DISPLAY-FILTER  Auto/Radio/Internet should filter what's drawn
Status: **DONE for NEX (pi4 + pi_zero); MET already correct; WND + TFC + AIR/SIG
carved out** — the weather-source pill (`wx_source`: auto | radio | internet)
was only gating the *pollers*, so switching to RADIO left stale INTERNET data
on screen until it aged out.  It's now a hard render-time filter where the layer
has two real sources:
- **NEX** — `_nexrad_render_arg` returns None in RADIO (hide the downloaded
  mosaic); `_fisb_nexrad_cells` returns [] in INTERNET (hide the FIS-B cells);
  AUTO shows both.
- **MET** — already filtered at the merge (`inet=[]` in radio, `rdr=[]` in
  internet).  No change.
- **WND** — carve-out: winds aloft is **internet-only** (there is no FIS-B
  winds), so it ALWAYS pre-loads and ALWAYS shows regardless of the pill — like
  the traffic sensor below, hiding the only source just blanks the layer.
  `_winds_client.enabled = True` unconditionally, and the `set_winds` store feed
  is pulled out of the `not radio_only` gate so it feeds in every mode.  (The
  first instinct was the opposite — RADIO ⇒ barbs gone — but that assumed FIS-B
  carried winds; it doesn't, so "always visible" is the right version.)
- **TFC** — carve-out (separate `traffic_source` selector).  RADIO = radio
  only, AUTO = radio+internet merged, **INTERNET still keeps radio** on purpose:
  the local ADS-B receiver is the real see-and-avoid picture and must never be
  hidden to honour a literal "internet only" (it'd suppress real, locally-sensed
  targets — unlike weather, where source is just provenance of the same data).
- **AIR/SIG — NOT filterable as-is (deferred).** The FIS-B store *dedupes*
  AIRMET/SIGMET advisories by text and graphics by geometry and stores **no
  source tag** (`add_advisory`/`add_graphic` in `shared/fisb.py`), so a radio
  and internet copy of the same NWS bulletin collapse to one.  A hard
  source-filter would need per-item source tagging + a rework of the cross-source
  dedup.  Low value (the bulletins are identical across radio/internet — it's the
  same product, so a lingering "internet" AIRMET in RADIO mode isn't *wrong*
  data), so left for a follow-up if strict consistency is wanted.
Files: `_nexrad_render_arg`, `_fisb_nexrad_cells`, and the winds enable/feed in
`_update_weather` (always-on) in both `pi4/pfd.py` and `pi_zero/pfd.py`.

### WINDS-FORECAST-SERIES  Winds roll forward to "now"; inset is always now
Status: **DONE (pi4 + pi_zero)** — winds-aloft barbs are no longer a frozen
fetch-time snapshot.  Each national-cache column now carries the forecast
**series** (per-hour `[dir,spd,temp]` flat rows out to ~30 h, stored in
`t0`/`step_s`/`series`/`alts`).  Open-Meteo already returns a 48 h forecast per
call, so the series is free; we just stopped throwing all but one hour away.
- **Retarget at draw time.** `_winds_barbs(offset_h)` and the barb-tap table
  pick the hour for `now + offset_h` via `wx.winds_levels_at()`.  The PFD inset
  passes `offset_h=0` → it is ALWAYS *now*, independent of the WND-page
  forecast-time selector.  The page passes its `winds_time_offset_h`.  A target
  outside the held window draws blank (no wrong-time forecast).
- **Re-pull on the 6 h GFS run** (`max_age_s`), not to chase "now" — the series
  advances on its own between fetches.  Changing the page offset no longer
  forces a re-fetch (instant, zero API calls); `force_refresh()` removed from
  `_mfd_cycle_winds_time`.
- **LAN sharing stays compact.** The full series is ~30× too big for one UDP
  datagram, so a feeder broadcasts a single-hour *now*-snapshot (existing
  packet format) re-derived each tick and stamped `st=now`; adopters roll
  forward as `st` advances (`ingest_packed` adopts a newer run OR a newer
  snapshot of the same run).  A relayed/frozen snapshot carries an
  un-advancing `st`, so a dead feeder can't pin the panel — deferral lapses and
  someone re-pulls.  A screen holding its own series ignores peer snapshots
  (never downgrades).
- Memory: series kept as flat int rows (~7.5 MB national, shared by reference
  into the FIS-B store), not dicts (~90 MB).  Disk persists the series.
- Files: `shared/wx.py` (`parse_open_meteo_winds(series_h=…)`,
  `_build_winds_series`, `winds_levels_at`, `_winds_cols_snapshot`,
  `_zone_packet`, `ingest_packed`, `WindsUSCache(series_h=30)`); `_winds_barbs`
  + `_draw_wx_winds` + `_mfd_cycle_winds_time` in both `pi4/pfd.py` and
  `pi_zero/pfd.py`.  Tests: `shared/test_wx_winds.py` (12 cases).
- **DEFERRED — WINDS-SERIES-PEER-OFFSET (Phase 2):** a screen that ONLY ever
  adopts a zone (never fetches it) holds just the now-snapshot, so its WND-page
  `+Nh` selector reads *now* for that zone until it fetches.  Full `+Nh` on
  pure-adopters needs chunked/compressed series sharing in `screen_sync`
  (reassembly across datagrams) — not built; feeding rotates across the 3 Pis,
  so each holds series for most zones in practice.  The inset (now) is correct
  everywhere regardless.

### WINDS-STALE-STATUS  "6/6 loaded" was uninformative; report stale/expired
Status: **DONE** — zones refresh in place and never drop, so a loaded-count
sat at `6/6` forever.  `status()` now returns `(fresh, total, age_s, stale,
expired)`: fresh `< max_age_s` (6 h), stale `6–24 h` (drawn as a fallback),
expired `≥ expire_s` (24 h — `columns()`/`count()` stop SERVING it, drawn
blank).  Status line reads e.g. `WINDS 4/6 · 7h · 2 stale`.  `stale_zones()`
lists which.  `shared/wx.py`, status line in both `pfd.py`.

### TFC-RA-SENSITIVITY  Traffic collision alert (RA) fires too eagerly
Status: **FIXED** — closure/tau-based RA (replaces the flat ring)
`threat_level` now fires the "alert" (red / "Traffic, Traffic") tier only
when a target is **actually converging and will be close soon**: tau =
range / closure ≤ `ADSB_TAU_S` (30 s) AND within the vertical protected band
(`ADSB_ALERT_FT`) AND inside the advisory range — plus a hard floor backstop
(`ADSB_ALERT_FLOOR_NM/FT` = 1 NM / 400 ft) for anything right on top of us
regardless of closure.  Range-rate is tracked per ICAO frame-to-frame and
EMA-smoothed in `_update_traffic` (both pi4 + pi_zero) so jitter can't fake
convergence; diverging / parallel / non-closing traffic no longer trips it.
Proximate (amber) advisory unchanged at the static 6 NM / 1200 ft envelope.
Unit-tested in `shared/test_adsb.py` (`test_threat_tau`).  Original spec
retained below for reference.
Pilot report: the traffic "RA" (the `Traffic, Traffic` callout + flashing
TRAFFIC banner) feels too sensitive — it triggers on traffic that isn't
really a threat.  Today it's a pure static envelope: fires when a target
is within **ADSB_ALERT_NM = 3.0** *and* **ADSB_ALERT_FT = 600**
(`shared/config_base.py`), classified in `shared/adsb.py threat_level()`,
edge-triggered in `pi4/pfd.py _update_traffic` (~line 1807).  Real TCAS/TAS
RAs are **closure/time-based** (tau — time to closest approach), not a flat
ring, which is why a flat 3 NM/600 ft ring nuisance-trips on parallel or
diverging traffic.
**Decision (pilot): do it properly — intercept/closure-based, not a
distance ring.**  Real TAS/TCAS uses **tau** = time to closest point of
approach: alert only when the target is actually *converging* and will be
close *soon*.  Plan:
  - Track each target's **range rate** (closure) frame-to-frame (Δrange /
    Δt), and ideally vertical closure (Δrel_alt / Δt).
  - **Alert when `tau = range / closure_rate` is below a threshold**
    (~25–35 s is typical TAS) **AND** the projected miss distance / vertical
    separation is inside a small protected volume — i.e. it's both closing
    and going to be close.  Diverging or co-altitude-but-parallel traffic
    never trips it.
  - Keep a hard **floor** ring (e.g. anything inside ~1 NM/400 ft regardless
    of closure) as a backstop, and keep the **proximate** (amber) tier as the
    current static 6 NM/1200 ft advisory.
  - Smooth the range-rate (a frame or two of EMA) so GPS/ADS-B jitter
    doesn't produce phantom closure.
Touches `shared/adsb.py` (add range-rate to the relativised target +
a tau-based `threat_level`) and `pi4/pfd.py _update_traffic` (per-target
previous-range memory).  `shared/config_base.py` gains `ADSB_TAU_S` etc.

### SETUP-SCROLL-SHORT  Display setup page scroll stops before the bottom
Status: **FIXED (band-aid)** — superseded by DISPLAY-SETUP-SPLIT
`_DSP_ROWS` had grown to 12 rows (FLIGHT PATH / TFC ALT / TFC RANGE) but the
scroll clamp still assumed 11 + a 2-row approximation of the taller MAP
LAYERS row, so it stopped ~56 px short.  Fixed on pi4 by `_dsp_max_scroll()`
computing the real content height (standard rows + `_DSP_LAYERS_ROW_H` +
padding).  pi_zero was already correct (7 rows, clamp 9).  Proper long-term
fix is the page split below.

### DISPLAY-SETUP-SPLIT  Break the DISPLAY setup screen into 3 tabbed pages
Status: **OPEN** — design agreed
The DISPLAY setup screen has outgrown one page.  Split it into three tabbed
sub-pages (tab bar across the top, no scrolling on any page).  Grouping
(agreed with the pilot):
  - **UNITS** — SPEED UNITS, ALTITUDE, PRESSURE
  - **DISPLAY** — BRIGHTNESS, ALERT AUDIO, ALERT VOLUME, SUN POSITION,
    FLIGHT PATH
  - **MAP** — MAP INSET (+ TRK↑/N↑ orient pair), MAP RANGE, MAP LAYERS
    (the tall multi-toggle), TFC ALT, TFC RANGE
Navigation: a 3-segment tab bar (UNITS · DISPLAY · MAP) under the header,
tapping a tab swaps the visible rows; `disp["dsp_tab"]` holds the current
tab.  Each page fits without scrolling (MAP is the tallest: 4 rows + the
112 px MAP LAYERS row + tab bar ≈ 476 px — fits 600 px pi4 and *just* fits
480 px pi_zero, so verify the MAP tab on the Zero / consider trimming the
tab-bar height there).
Implementation notes:
  - Replace single `_DSP_ROWS` with `_DSP_ROWS_UNITS/_AV/_MAP` + a
    `_DSP_TAB_ROWS` dict; draw + hit-test iterate the *current* tab's rows
    by LOCAL index via a shared `_dsp_row_y(local_i)` (offset below the tab
    bar) and the existing `_dsp_rx()` so draw/hit can't diverge.  Pass the
    local y to `_setting_row(..., _y_override=...)` (already supported).
  - MAP LAYERS draws after the MAP tab's standard rows at
    `_dsp_row_y(len(_DSP_ROWS_MAP))`; `_DSP_LAYERS_ROW_INDEX` becomes
    `len(_DSP_ROWS_MAP)`.
  - Add a `tab:<id>` action from `display_setup_hit` handled in the
    `mode == "display_setup"` dispatcher; remove `"display_setup"` from
    `_SS_DRAG_MODES` (no scroll) and drop `_dsp_max_scroll()`.
  - Mirror to `pi_zero/pfd.py` (same structure, smaller constants), then
    regenerate the piZ `preview_setup_display.png` (now 3 captures, e.g.
    `_units` / `_av` / `_map`) and update the manual figures/§.
Best done in a focused session: it's layout-precise and the pi4 half can't
be render-verified here (no GL), so each tab should be checked on hardware
(or via the piZ headless render, which shares the layout).  Target:
`draw_display_setup` + `display_setup_hit` + the `_DSP_*` row tables in
`pi4/pfd.py` and `pi_zero/pfd.py`.

### INSET-ORIENT-STUCK  Moving-map inset stuck north-up — TRK↑ toggle dead
Status: **FIXED**
The inset forced north-up whenever `_eff_range > 40`
(`_eff_orient = "nrth" if _eff_range > 40 else _orient_pref`), which
swallowed the pilot's TRK↑ choice on the WND page (winds zoom ≥40) and at
wide manual zooms — the toggle and the setup pill both flipped `map_orient`
but the render ignored it.  The reasons for the force (rotated-tint smear,
per-heading rebuild) are gone now that tint registration is fixed and the
cache key excludes rotation, so the inset honours the pilot's choice at
every manual range; only AUTO stays north-up so the destination doesn't
spin under the chevron.  (pi_zero already honoured `map_orient` directly.)

### TINT-SHIMMER-PAN  Terrain tint shading shifts a bit on a pan + density bump
Status: **OPEN**
After the tint registration fix (scale + projected-quantised-centre blit),
the tint sits in the right place and no longer jumps several NM, but the
hypsometric *shading* still "pops" a little at each cell crossing while
panning.  Cause: the 48×48 sample grid (`_TINT_N`) is anchored to the
aircraft's quantised centre, so each rebuild re-anchors the grid and the
coarse shading re-samples onto shifted points (a peak sampled or not, a
water edge moved by a sample).  Position is correct; it's the *sampling
lattice* shifting per rebuild.
Fix: **world-anchor the sample lattice** — snap the grid's top-left to a
fixed multiple of the sample spacing (`dlat = span_lat/(n-1)`,
`dlon = span_lon/(n-1)`) in `_build_tint_pixels` so the SAME absolute
lat/lon points are sampled regardless of pan; consecutive (quantised)
builds then sample identical points where the windows overlap, so panning
slides a stable field with no pop.  The anchored grid centre differs from
the quantised centre by up to half a sample, so thread the true centre
(`a_lat, a_lon`) back through `_build_tint` / `_tint_async_worker` /
`_tint_get` (cache value becomes `(surface, elev, a_lat, a_lon)`) and blit
at `_project(a_lat, a_lon)` so registration stays exact.  Lat lattice is
fully world-stable; lon spacing depends on `cos(lat)` so it's world-stable
to within a hair over a few cells — fine.
Density: also bump `_TINT_N` 48 → **64** on the Pi 5 (pilot's call) for
crisper mountains/coastlines — the extra samples are cheap reads from
already-loaded tiles, just more smoothscale per build.  Gate it by platform
(keep 48 on the 2 GB Pi 4 unless confirmed it doesn't tax it; Pi 5 fine).
Apply to both `pi4/moving_map.py` and `pi_zero/moving_map.py`.  Verify on
the Pi 5 (a slow pan should show terrain sliding smoothly, no shading pop).

### WINDS-INSET-ALT-SELECT  No winds-altitude selector on the inset
Status: **FIXED (verify position on hardware)**
When the WND overlay is up on the PFD inset, a small **"9k ft"** readout
now sits just under the range label and **cycles the level** (3k/6k/9k/12k/
18k) on tap, reusing `_mfd_cycle_winds_alt` — so the inset can change winds
altitude without the full-screen MFD.  Winds zoom stays on the L/R half
taps.  The draw box + the hit-test box are kept in sync in `pi4/pfd.py`
(inset overlay draw + the `_last_map_rect` tap handler); eyeball the exact
position/size on the panel and nudge the two boxes if needed.  Forecast-time
on the inset (`_mfd_cycle_winds_time`) was left for later — not requested.

### FPLLIB-DELETE-RESURRECT  Deleted saved flight plans come back from peers
Status: **OPEN**
Deleting a saved flight plan (LOAD-plan picker → DEL) only removes it on
the local display.  The saved-plan / user-waypoint library syncs between
screens (`KIND_FPLLIB`, gated by SHARE FPL), and that sync is a pure
**additive union merge** with no concept of deletion: `_ssync_apply_fpl_lib`
adds any peer plan whose name we don't already have, and every display
re-broadcasts its full plan list every ~5 s (`_ssync_publish_fpl_lib`).
So a plan you delete on screen A is re-broadcast by screen B (which still
has it) and re-added on A within ~5 s — it "resurrects" unless you race to
delete it on every connected screen at once.  Same flaw applies to **user
waypoint** deletes.  Repro: two+ displays with SHARE FPL on, DEL a plan on
one → it reappears.
Root cause: union merge can't express "this was deleted" — there is no
tombstone or deletion propagation.
Fix options:
  - **Tombstones (preferred):** on delete, record `{name, deleted_ts}` and
    include a `deleted` set in the `KIND_FPLLIB` payload.  Peers drop any
    plan whose name has a tombstone newer than the plan's own
    creation/update time, and don't re-add a tombstoned name.  Expire
    tombstones after a window (e.g. 24 h) so the set stays small.  Needs a
    creation/update timestamp per saved plan (currently plans carry only
    `name` + `waypoints`).
  - **Explicit delete event:** broadcast a one-shot `KIND_FPLLIB_DELETE`
    `{name}` that peers apply immediately — simpler, but lost if a peer is
    offline during the delete (it'll re-resurrect when that peer rejoins,
    so tombstones are still the robust answer).
Touches: `shared/screen_sync.py` (payload shape), `_ssync_apply_fpl_lib` /
`_ssync_publish_fpl_lib` / `_fpl_plan_delete` (+ the user-waypoint delete
path) in both `pi4/pfd.py` and `pi_zero/pfd.py`.

### AHRS-SRC-SELECTOR  Runtime AHRS source picker (AUTO / USB / WIFI)
Status: **OPEN — usable workaround documented**
Today PFD picks the AHRS transport once at startup (USB if
`/dev/ttyACM*` is enumerated within the first ~2 s of `main()`,
otherwise Wi-Fi SSE).  That misses two real cases:
  - **Late USB enumeration**: on the Pi Zero with `dwc2 dr_mode=host`
    the Pico W enumerates around t+79 s, well after PFD has chosen
    Wi-Fi and entered its retry loop.  Workaround: `sudo systemctl
    restart pfd.service` once `ls /dev/ttyACM0` resolves.
  - **Hot-plug / hot-unplug**: yanking the USB cable or losing the
    Pico W's AP doesn't trigger a fallback; the pilot has to restart
    PFD by hand.
Wanted: a setting `ahrs_source` ∈ {auto, usb, wifi} surfaced on the
Connectivity Setup screen as a three-pill segmented control.  A daemon
thread polls every 2–3 s and:
  - When `auto`: prefers USB if `/dev/ttyACM*` exists, else Wi-Fi.
  - When `usb` (forced): retries `/dev/ttyACM*` until present; never
    falls back to Wi-Fi on its own.
  - When `wifi` (forced): never opens the serial port even if a Pico W
    appears.
  - Switches transports cleanly (stop old client, start new) without
    needing a full PFD restart.
  - Surfaces "AHRS DOWN — fallback in 5 s" hint on the AI in the
    interim so the pilot isn't confused by a frozen attitude.
Touches: `pi_zero/pfd.py` + `pi4/pfd.py` AHRS startup block,
`shared/serial_client.py` + `shared/sse_client.py` lifecycle (need a
clean `.stop()` + `.is_alive()` contract).  Estimated work: ~30 min
+ test.

### BOARD-REV-B  Next AHRS PCB spin — index
Status: **OPEN — sensor selection locked, layout work next**
Locked-in decisions (see linked entries for the full rationale):
  - **Airspeed transducer**: **TE MS4525DO ±1 psi** replaces the
    SDP33-1500Pa. The SDP33 saturates at ~96 kt; S-21 cruise is
    130 kt. See SDP31-AIRDATA "Higher-range swap path".
  - **AOA transducer**: a **second MS4525DO** on the same I²C bus at
    the alternate address (single BOM line covers both). See AOA-PROBE.
  - Net result: rev B carries **2× MS4525DO** — one for pitot/static,
    one for AOA — sharing the I²C bus, driver, and supply rails.
  - **AOA probe head**: AlphaSystems Eagle preferred; 2-hole DIY
    flush probe as the cheap alternative.
  - **Sensor footprint reservations**: keep the 0x22 SDP3x pad on rev
    A's footprint plan; rev B replaces it with the second MS4525DO
    footprint + a pair of silicone-hose nipples for the AOA probe lines.
Bench plan before the spin: finish air-data validation on the SDP33
(it'll read up to ~96 kt fine — enough to bench-prove the IAS/TAS/wind
math against a hand pump and against GPS GS in zero-wind taxi), then
queue the layout work.
Firmware to write when rev B lands:
  - `firmware/ms4525.py` — TE protocol (different from Sensirion), one
    driver covers both units. ~70 lines.
  - `main.py` instantiates the driver twice, address-strapped.
  - `airdata.py` is unchanged — it consumes `dp_pa` regardless of
    which transducer produced it.
  - AOA math: linear calibration curve fit on first-flight data, then
    persisted; `aoa_deg` + `aoa_src` already reserved in the `$AHRS`
    JSON by the AOA-CALC entry, so the display side picks it up free.

### WAVESHARE-35-DPI  Pi 4 Waveshare 3.5" DPI panel won't initialise
Status: **OPEN — low priority, blocked on time, ROADOM 7" HDMI works fine**
Target: `/boot/firmware/config.txt`, possibly `pi4/config.py` profile
overlay name, possibly Waveshare's `LCD-show` package.
Context: tried switching from the ROADOM 7" HDMI to the Waveshare 3.5"
DPI hat. Set `DISPLAY_PROFILE = "waveshare_35"` and uncommented the
DPI lines in `config.txt`:
  ```
  dtoverlay=waveshare-35dpi-3b-4b
  framebuffer_width=640
  framebuffer_height=480
  ```
Pi boots and SSH works, but the Waveshare panel stays dark. Diagnosis:
the overlay file `waveshare-35dpi-3b-4b.dtbo` is **not present** in
`/boot/firmware/overlays/` on Pi OS Bookworm (kernel 6.12). Only the
DSI-Waveshare and CAN-hat overlays ship now — the older panel-specific
DPI overlays were deprecated. The dtoverlay line is silently ignored;
`/sys/class/drm/` shows only HDMI connectors, never a DPI one.
Two paths forward when picking this up:
  - **A1**: Install Waveshare's `LCD-show` package
    (`git clone https://github.com/waveshare/LCD-show && sudo
    ./LCD35-show`) — ships the missing overlay. Old script, edits
    `config.txt` itself and reboots; back up `config.txt` first.
  - **A2**: Use the modern `vc4-kms-dpi-generic` overlay with explicit
    panel timings looked up from the Waveshare datasheet. Cleaner,
    keeps the install self-managed.
Confirmed today that path **B** (override `DISPLAY_W`/`DISPLAY_H`
to 640×480 in `config_local.py` while keeping HDMI on the ROADOM)
would let us validate the small-screen *layout* without bringing
up the Waveshare panel — useful as a pre-step for any future
sessions that want to verify PFD rendering at 640×480 before doing
the hardware swap.
Recovery applied today: reverted `config.txt` to HDMI-only, restored
ROADOM 7" as the active display.

### SVT-GROUND-SKIRT  Ground hidden by sky at low altitude + steep bank
Status: **OPEN**
Target: `pi4/svt_renderer_gl.py` (inner mesh near-zone, sky shader,
possibly a new fixed-elevation ground polygon).
Context: at low altitude with steep bank, the camera looks nearly
straight down through the AI window. The terrain mesh has finite
extent and the inner mesh's near zone has a small hole / discard
band near the camera. In that combination, fragments along the
"down-the-wing" sightline fall outside the mesh's footprint at the
camera's near plane, so the sky shader fills in — pilot sees a
strip of blue under the wing where ground should be.
Work items:
  - Identify whether the gap is from the inner mesh's near-zone
    discard, the camera near plane clipping mesh triangles, or the
    outer-mesh discard square not extending below the inner mesh
    when banked. A frame capture at low alt + 60° bank should make
    the geometry obvious.
  - Add a low-cost "ground skirt": a flat polygon at terrain
    elevation that extends well below and around the camera so any
    near-camera gap fills with terrain colour rather than sky.
    Cheap (single quad) and depth-tested so legitimate terrain
    overdraws it.
  - Verify under the ROADOM 7" sim: low-pass at 100 ft AGL at
    Sedona with 45–60° bank should show ground edge-to-edge
    through the AI, not a wedge of blue along the lower wing.

### SVT-MESH-OVERLAP  Visible blue gap between high-res and low-res mesh
Status: **OPEN**
Target: `pi4/svt_renderer_gl.py` — outer mesh's `grid_max_dist_m` /
mesh extents, plus the inner mesh's far edge.
Context: the SVT renders an inner high-res mesh (sharp foreground)
and an outer coarse mesh (distant ridges). They overlap by ~20 %
(`discard_inside_m = mesh_radius_m * 0.80`) so the inner owns the
foreground. In a few situations a thin wedge of sky shows through
the seam between the two meshes — typically along the horizon at
moderate banks, where the inner mesh's far edge sits just above
the outer mesh's near edge as drawn through the perspective
projection.
Work items:
  - Increase the overlap band — drop `discard_inside_m` to
    something like 0.65 of the inner radius, or extend the inner
    mesh outward by a tier so the seam lives further out where
    perspective compresses it below pixel resolution.
  - Alternative: pull the outer mesh slightly down toward the
    horizon by biasing its vertices' Z by a small constant when
    rendered, so any sliver always reads as terrain rather than
    sky. Cheap if the bias is small enough not to affect the
    silhouette.
  - Confirm the fix doesn't reintroduce the "morphing recentre"
    artefacts from the original mesh-snap work (commit chain on
    the GL bring-up). A regression-style preview that captures
    pre/post for the same scene at the same camera state would
    catch this.

### #8  Range rings — distance circles on terrain
Status: **OPEN**
Target: symbol overlay in pi4/pi_zero.
Context: draw 1 nm / 2 nm / 5 nm distance rings on the SVT so pilot
has spatial reference for nearby airports/obstacles.

### #9  Pico W firmware — debug AP not appearing
Status: **OPEN**
Target: `firmware/main.py`, `firmware/config.py`.
Context: when `AP_SSID = "AHRS-Link-DEBUG"` or similar diagnostic
values, the AP doesn't come up. Works with default SSID. Possible
channel/password-length edge case.

### SDP31-AIRDATA  SDP33-1500Pa airspeed driver + air-data computer
Status: **FIRMWARE LANDED — waiting on hardware install + in-flight calibration**
Target: new `firmware/sdp31.py`, additions to `firmware/main.py`,
new fields in the `$AHRS,{json}` packet consumed by `pi4/serial_link`
and the iPhone SSE client.
Resolution (firmware side): `firmware/sdp31.py` ships the Sensirion
continuous-mode I²C driver (CRC-validated 9-byte frames, soft reset
on init, manual + auto zero offset). `firmware/airdata.py` ships IAS
(against ρ₀), TAS (density-corrected via BME280), density altitude
(inverse ISA), and the wind-triangle solution. `firmware/main.py`
inits the SDP33 from `SDP31_ENABLE` in `config.py`, populates the
new `ias_kt` / `tas_kt` / `dp_pa` / `oat_c` / `dens_alt_ft` /
`wind_dir` / `wind_kt` / `airdata_ok` fields in `state`, and
broadcasts them on both the SSE event stream and the `$AHRS,…`
USB serial line. `web_server.py` adds a `/sdp_zero` HTTP endpoint
for in-flight zero re-capture. Documentation: REQUIREMENTS_AHRS
gains §7B Air-Data Computer (REQ-AHRS-AIR-001 … 008); USER_MANUAL_PI4
§21 covers wiring + plumbing + range; the speed-tape source switch
is documented in §2 of both pilot manuals.
Still open:
  - **Hardware install + first-flight cal.** Bench the SDP33 with
    a hand pump (0–1500 Pa) to confirm `dp_pa → ias_kt` math; then
    plumb pitot + static and validate IAS against GS at cruise in
    near-zero wind.
  - **Higher-range swap path — decided as MS4525DO × 2 in rev B.** The
    SDP33-1500Pa saturates at ~96 kt IAS sea level. With S-21 cruise
    actually at ~130 kt (= 2740 Pa dp) the part on the rev-A board is
    undersized for the real airframe — it pegs in cruise. Pilot
    decision: **finish bench-validation on the current SDP33** (it'll
    read up to 96 kt fine, plenty to prove the air-data math), then
    swap to **TE MS4525DO at ±1 psi** on the next board spin. Same
    pair gets used for AOA — see AOA-PROBE below for the rationale —
    so the rev B BOM gains one part number that covers both
    pitot/static and AOA. Driver work is a new ~70-line
    `firmware/ms4525.py` (different protocol from Sensirion); the
    rest of the air-data pipeline doesn't change.
  - **Stall-warn enunciator** below configured Vs1 — visual + voice
    callout to make the existing speed-tape colour band
    authoritative. Pairs with REQ-DISP-PI4-AUD-001.
Context: the new sensor board carries a Sensirion SDP33-1500Pa
differential-pressure sensor — pitot pressure on one port, static on
the other.  With the existing BME280 (static pressure + OAT) we get
a complete pitot-static air-data set:
  - **IAS** (knots) = `sqrt(2·dp / ρ₀)` — what the speed tape should
    show.  Currently the tape is fed by GPS groundspeed, which lies
    in any wind.
  - **TAS** (knots) = `IAS · sqrt(ρ₀ / ρ)` where ρ comes from BME280
    static + OAT.  Needed for centripetal accel in AHRS-GPS-AID and
    for the wind-triangle solution.
  - **Pressure altitude** is already computed by BME280 + QNH from
    the firmware's existing baro path; this entry doesn't change it.
  - **Wind solution**: with TAS + heading (from AHRS) + GS + track
    (from GPS), wind = ground_vec − air_vec.  Drop it into the
    `$AHRS` packet as `wind_dir` / `wind_kt` so the displays can
    show a wind ribbon.  Side benefit: validates AHRS-MAGCAL by
    cross-checking the wind solution against forecast / pilot
    observation.
  - **Stall-warning hook**: with IAS available we can light a
    `LOW SPD` enunciator below configured Vs1 in the V-speeds
    profile.  Already a pi4 colour band on the speed tape; this
    just makes the audible/visual alert authoritative.
Work items:
  - I²C driver for the SDP33 — Sensirion's reference protocol is
    short (start continuous mode, read 9-byte frames with CRC,
    handle the auto-zero offsets).  About 80 lines of MicroPython.
  - Air-data math in `firmware/main.py` (or a small `airdata.py`):
    IAS, TAS, density-altitude.  Cross-check IAS against GS at
    cruise to verify driver math before depending on it.
  - `$AHRS` JSON gains `ias_kt`, `tas_kt`, `wind_dir`, `wind_kt`.
  - Pi 4 + iPhone speed tapes: switch the primary source to IAS
    when the air-data path is live (fall back to GS with a small
    "GS" subscript when SDP33 reports unhealthy, mirroring the
    existing GPS-fallback pattern on the heading tape).
  - V-speeds page: nothing changes structurally — the existing
    Vs0 / Vs1 / Vfe / Vno / Vne entries already drive the colour
    bands.  Stall-warn enunciator is the only addition.
Pairs with AHRS-GPS-AID (TAS is the correct centripetal input).

### AOA-CALC  Computed AOA from speed + load factor + ρ (pre-probe)
Status: **OPEN — software-only, can land TODAY with current sensors**
Target: new `firmware/aoa_calc.py` (or extend `firmware/airdata.py`),
`$AHRS` packet, AOA indexer in `pi4/pfd.py` and the iPhone display.
Context: in the linear region of the lift curve, AOA can be inferred
from sensors we already have on the bench: WT901 (load factor),
BME280 (static + OAT for ρ), and GPS (groundspeed).  No new hardware
required to start.  Useful as an on-speed cue and as a bridge until
AOA-PROBE ships; not a substitute for measured AOA at the margins.
Math: from the lift equation `L = n·W = ½·ρ·V²·S·Cl`, solve for Cl
and divide by the airframe's lift-curve slope:
  `α ≈ α₀ + Cl / Cl_α`
  `Cl  = (n·W) / (½·ρ·V²·S)`
where `n` is load factor (from the WT901 accel Z, in g), `V` is
TAS (SDP33 + BME280), `ρ` is air density (BME280 static + OAT), and
`W` / `S` / `Cl_α` / `α₀` are airframe constants for the Rans S21.
Inputs (with the same fallback ladder AHRS-GPS-AID uses):
  - **Velocity** — TAS from SDP31-AIRDATA when it lands; GS from
    `gps.speed_kt` until then.  In zero wind GS == TAS; in wind the
    error scales with the wind component.  Surface the active
    source in the indexer (small `gs` / `tas` subscript) so the
    pilot doesn't trust an upwind on-speed cue.
  - **Load factor** from WT901 (use the same gravity-vector estimate
    the AHRS produces; rotate the body-frame accel into the
    wing-perpendicular axis).
  - **Density** ρ from BME280 static + OAT.
  - **Weight** as a pilot-entered field on the V-speeds / Flight
    Profile screen — accepts dry-weight + fuel and decrements by
    the fuel-flow estimate over the flight (or just a single GW
    entry to start; refine later).
  - **Cl_α, α₀, S** stored in the airframe profile alongside the
    V-speeds (already a JSON profile file).
Caveats — explicit so the indexer doesn't lie:
  - Linear-region only.  Departs from real AOA near the stall, in
    deep flap, and during accelerated stalls.  Calibrate the
    indicator's "yellow" band conservatively and treat the "red"
    band as advisory until AOA-PROBE replaces it.
  - Configuration-blind.  Flap deployment changes Cl_α and α₀; if
    we read flap position later, branch the constants.  Until
    then, calibrate against clean configuration only.
  - Weight-dependent.  Bad fuel-state estimate biases the whole
    output.  Surface the assumed weight on the profile screen.
  - Until SDP31-AIRDATA lands, "velocity" means GS — see source
    subscript above.
Work items:
  - Add airframe constants (Cl_α, α₀, S, max-gross W, flap-clean
    flag) to the V-speeds profile.  Default to Rans S21 numbers;
    pilot tunes against measured stall on first-flight cards.
  - `aoa_calc.py` runs each AHRS frame, emits `aoa_deg` and a
    `aoa_src` = `"calc"` field on the `$AHRS` packet.  When
    AOA-PROBE goes live the same field flips to `"probe"` and
    the same display consumes both.
  - Display: AOA indexer on the right side of AI when no approach
    is active (same slot AOA-PROBE will use).  Show a small `c`
    subscript on the indexer while `aoa_src == "calc"` so the
    pilot knows it's the inferred value.
Pairs with SDP31-AIRDATA (the inputs come from there) and with
AOA-PROBE (this entry retires when the probe lands, or stays as
a redundant cross-check).

### FPV  Velocity vector / flight-path marker on the AI
Status: **pi4 + iPhone LANDED — pi_zero port pending (tracked as FPV-PIZ)**
Resolution (pi4 + iPhone): `draw_fpv_marker()` in `pi4/pfd.py` projects
the velocity vector (NED azimuth = GPS track, elevation = flight-path
angle `atan2(VS, GS)`) into the same `_full_ai` frame the airport /
runway / obstacle overlays use (ah/48° px-per-deg, roll rotation), so
it banks with the SVT horizon and aligns with the airport symbols.
Open circle + two wings + vertical stub, cyan, self-hides below 5 kt
GS, clamps to the AI box with a ghost arrow when the vector falls
outside (drawn even in extreme attitudes, unlike the other overlays).
Toggle `fpv_enabled` (default ON) on the Display setup screen ("FLIGHT
PATH" row), persisted via the existing `ds` subtree. iPhone mirror:
`drawFPV()` in `iphone_display/index.html` using the same focal /
pitchOff / roll math `drawAI()` draws the horizon with, with a "✈
FLIGHT PATH" toggle in the setup menu (localStorage `fpv_enabled`).
Sign check (matches the cue we wanted): in a coordinated climb the FPV
sits below the nose by the AOA. Still open: **pi_zero port** — its AI
overlays use a rotate-the-whole-SRCALPHA-surface model rather than
per-feature roll, so the upright-symbol behaviour needs a small
adaptation; see FPV-PIZ below.
Target: `pi4/pfd.py` (new draw routine inside the AI block), iPhone
display equivalent.
Context: a velocity-vector / flight-path-vector marker shows the
pilot where the airplane is actually going through space, not where
the nose is pointing.  Standard on every modern PFD (G3X, Dynon,
Garrecht, mil HUDs).  Indispensable on approach — pilot flies the
FPV onto the runway numbers and lands there, whatever the crab
angle and AOA are doing.  Inputs we already have:
  - **GPS track** (`gps.track_deg`) — azimuthal direction the
    airplane is moving over the ground.
  - **GPS GS + VS** — flight-path angle = `atan2(VS_fps, GS_fps)`.
    GS from `gps.speed_kt`, VS from `gps.vspeed_fpm` (already
    smoothed in the firmware).
  - **AHRS attitude** (yaw/pitch/roll from WT901) — needed to
    project the FPV onto the AI viewport, since the AI is
    drawn in body-frame.
Math: the FPV's screen position is the projection of the velocity
vector into the camera frame the AI is rendered with.  The same
projection chain `draw_airport_symbols` already uses (yaw / pitch
/ roll, focal length, screen centre) takes a unit vector in the
NED frame and returns AI pixel coordinates.  Build a NED unit
vector from track + flight-path-angle, run it through the existing
projection, draw a small open circle with two horizontal "wings"
and a short vertical stub (the conventional FPV symbol).
Work items:
  - Compute FPV NED unit vector from GPS track + GS + VS each
    frame.  Skip when GS < 5 kt (parked / taxi noise) — hide the
    symbol below that gate.
  - Reuse the airport projection helper to land it on the AI.
    Clamp to the AI rectangle so it never escapes the viewport
    in extreme attitudes; show a "ghost" arrow at the edge in
    that case (G3X convention).
  - Symbol: 12 px circle, 6 px wings either side, 6 px vertical
    stub.  Cyan, no fill.  Same colour as the heading-bug bug
    set (pilot-relevant, not alert).
  - Display setup gains an FPV ON/OFF toggle (default ON when
    GPS is healthy).  Persist with the rest of the display
    settings.
  - Sanity check: on the ground rolling forward, the FPV should
    sit in front of the nose (track ≈ heading, FPA ≈ 0).  In a
    coordinated climb-out, the FPV sits below the nose by the
    AOA (a free cross-check against AOA-CALC once it lands).
Pairs with AOA-CALC (the vertical offset between aircraft symbol
and FPV is exactly AOA when the wind is along the flight path —
a free in-flight calibration target for the airframe constants).

### AOA-PROBE  Add a second differential-pressure transducer for AOA
Status: **OPEN — bundled into the rev B board spin alongside the
MS4525DO airspeed swap**
Target: hardware (rev B board), new `firmware/ms4525.py`, additions
to the `$AHRS` packet, AOA indicator on `pi4/pfd.py` and the iPhone
display.
Context: with one differential-pressure transducer doing pitot/static
(SDP31-AIRDATA), a second sensor connected to a flush-port AOA probe
gives a real AOA signal essentially free.  Standard experimental
implementation is a two-hole probe on the wing or a side-mount on
the fuselage where the upper / lower ports sit at different angles
to the local airflow — the ΔP across them is monotonic with AOA over
the normal envelope.  AOA buys things IAS-only can't:
  - **AOA-based stall warn** — fires at the actual stall margin
    regardless of weight, bank, or load factor.  IAS-based stall
    cues only work at 1 g and gross weight (the published Vs1 is
    a lie at any other condition).
  - **Optimal-AOA cues for approach and climb** — fly the
    indexer / donut, not the airspeed.
  - **Energy management on approach** — particularly relevant
    behind a Rotax that responds slowly to throttle.
Sensor selection: **TE Connectivity MS4525DO at ±1 psi (±6 895 Pa)**.
Same part as the rev B pitot/static transducer (see SDP31-AIRDATA
"Higher-range swap path"), strapped to the alternate I²C address so
both units share the bus. Rationale:
  - **Single BOM line** covers pitot/static + AOA. One driver
    (`firmware/ms4525.py`), one stock-keeping unit.
  - **Range never saturates** in the S-21 envelope. At 130 kt cruise
    pitot dp is ~2740 Pa, AOA ΔP at typical cruise AOA is ~100–300
    Pa, AOA ΔP at stall AOA approaches 1500–2200 Pa. All comfortably
    inside ±6 895 Pa.
  - **PixHawk-grade** so there's a deep community calibration data
    set, well-known temperature-compensation behaviour, and shipping
    is cheap.
Probe choice (decoupled from sensor — see USER_MANUAL_PI4 §21
hardware notes for the longer discussion):
  - **AlphaSystems Eagle AOA probe head** is the experimental
    standard. Two ports, pre-calibrated geometry, mounts under the
    wing on a riblet. Pair with the second MS4525DO. ~$150 for just
    the head (no electronics).
  - Cheap alternative: 2-hole DIY flush probe on a brass tube glued
    into the leading-edge cap. ~$40, takes more in-flight calibration.
  - Skip the Dynon AOA pitot unless heat is required — its
    electronics aren't needed and you'd ignore them.
Work items (rev B board):
  - Drop a second MS4525DO footprint on the rev B board, address
    strapped to the alternate slot (0x36 for the AOA twin if pitot
    sits at 0x28, or vice versa). Same package, same 3V3/GND rails,
    same I²C bus as the pitot transducer.
  - Two silicone-hose nipples next to the AOA sensor for the probe
    pair (upper / lower).
  - Pick + mount the AOA probe head (AlphaSystems Eagle preferred).
  - Build `firmware/ms4525.py` covering both units, instantiated
    twice in `main.py`. AOA math = calibration curve (linear over
    the cruising range, departs near the stall — fit on
    first-flight data, persist coefficients to flash).
  - `aoa_deg` and `aoa_src` fields added to `$AHRS` JSON. The
    pre-probe AOA-CALC entry already reserves the same fields, so
    the display path needs no rewrite — `aoa_src` flips from
    `"calc"` to `"probe"` when the hardware lands.
  - Display: AOA indexer on the right side of the AI when not on
    approach (mutually exclusive with the VDI from the recent work
    — VDI takes priority during approach, AOA at all other times).
    Standard cue: green/yellow/red segments with a fast-erecting
    diamond, donut at on-speed.
Pairs with SDP31-AIRDATA (same MS4525DO driver covers both) and with
AHRS-GPS-AID (AOA-based stall warn is the safety-of-flight payoff
once attitude is honest).

---

### #17  iPhone airport overlay — symbols + labels + download screen
Status: **OPEN**
Target: `iphone_display/` — new `airports.js` module, additions to
`index.html` for the AI overlay, and a new download/manage panel
plumbed into the setup menu next to TERRAIN.
Context: Pi4 has full airport support — `airports.py` parses the
OurAirports CSV into a numpy cache, `draw_airport_symbols()`
renders them as projected symbols on the AI with S/M/L filter,
`draw_airport_data()` is the dedicated data screen with download
controls, and the status badges include NO APT / EXP APT. iPhone
has none of this. The data set is ~3 MB (from `fetch_airports.sh`,
~80K airports worldwide), so it ships from the same upstream
(`davidmegginson/ourairports-data`) that pi4 already uses.
Work items:
  - **Data**: convert the airports + runways CSVs into a compact
    browser-loadable form (JSON shards by lat/lon tile, or a single
    binary blob with a header index — match what terrain.js does
    so the cache + service-worker story is consistent).
  - **airports.js module**: Terrain-style API:
    `Airports.init()`, `Airports.downloadGlobal(progressCb)`,
    `Airports.downloadRegion(lat, lon, radiusNm, progressCb)`,
    `Airports.nearby(lat, lon, radiusNm)`, `Airports.tileCount`,
    `Airports.status`. Use IndexedDB (or the same Cache API
    terrain.js uses) so it survives offline.
  - **Marker on AI**: `Airports.render(ctx, D, L)` called after
    `Terrain.render` and before the tape overlays — projects each
    nearby airport via the same focal/yaw/pitch/roll math the
    terrain mesh uses (factor that out into a shared helper if
    it isn't already). Render as a simple "signpost": small
    vertical pole with the ICAO identifier next to it. No
    paved/unpaved/heliport symbol distinction — just the post
    and the label, same shape for every airport.
  - **Type + size filter**: carry over pi4's four type toggles
    (`show_public`, `show_heli`, `show_seaplane`, `show_other` —
    public covers S/M/L by longest runway). Pi4-only `show_runways`
    and `show_centerlines` are skipped since iPhone only renders
    the signpost. The marker stays the simple post regardless of
    type/size; the filter just controls which airports appear.
    Persist all toggles to localStorage.
  - **Download/manage screen**: new "AIRPORTS" entry in the setup
    menu mirroring TERRAIN. Two buttons (Global / Regional),
    progress bar, count display, last-updated timestamp, clear
    cache button. Reuse the existing terrain panel CSS so it
    looks like a sibling, not a one-off.
  - **Status indicators**: NO APT / EXP APT badges in the same
    row as the GPS / link badges, mirroring pi4's `_AMBER`
    convention so the displays stay visually aligned.
  - Decide whether airport data should auto-download on first
    install (PWA add-to-home-screen), or be opt-in like terrain.
    Probably opt-in for the global set, auto for the
    home-airport-region.


---

### MAGCAL-PIZ-TUMBLE  Port pi4's hard-iron tumble mag-cal flow to pi_zero
Status: **OPEN — pi4 done, pi_zero pending**
The hard-iron tumble pass (PR #7 / commits a560924, 8280f68, 51705cd)
landed on pi4 + firmware in last session's main merge.  pi_zero's cal
modal still has the older 8-cardinal-only flow with no TUMBLE button.
Needed:
  - Cal modal: replace RESTART button with TUMBLE / STOP TUMBLE
  - Live elapsed + per-axis spread readout during tumble
  - Wire /magoff?action=tumble_start|tumble_finish HTTP endpoints
    (or the same over USB serial) — the Pico side already exists
  - State plumbing: _magtumble_active gate + _mag_cal_tumble_tick
Reference: pi4 implementation in pi4/pfd.py and firmware/main.py.

### DATA-USB-BUNDLER  Desktop "data update" tool + Pi-side USB ingest
Status: **OPEN — deferred until in-aircraft refresh becomes painful**
Long-term goal: mirror how certified avionics (Garmin G3X, Dynon,
Avidyne) do 28-day cycle updates.  Pilot runs a desktop app at home
with real internet, it builds a complete data bundle, writes to a
labeled USB stick.  At the hangar the pilot inserts the stick into
each Pi; a small daemon detects the mount, rsyncs the bundle to the
runtime data dir, validates the manifest, restarts pfd.service.
Both displays get the same data without needing any in-aircraft
network.

Bundle contents (all generation already exists, just needs glue):
  - airspaces.json          via tools/build_airspaces_us.py
  - SRTM .hgt tiles         via tools/compact_srtm.py
  - airports cache (.npy)   via shared/airports.py
  - obstacles cache         via shared/obstacles.py
  - water tiles             via tools/build_water_tiles.py
  - Natural Earth .npz      via shared/natural_earth.py
  - manifest.json           cycle date + per-source version stamps

Desktop app: probably Python tkinter or PyQt, cross-platform.  Could
also ship as a CLI for power users.  Reuses the existing converter
modules in tools/ + shared/ — no duplication.

Pi-side ingest: systemd path-unit watching /media/pfd-data/, rsync
into data/, atomic-replace each cache, systemctl restart pfd.service.
~100 lines.

When to build: as long as the in-Pi DOWNLOAD buttons work in the
hangar with whatever LTE/WiFi is available, the existing flow is
fine.  The moment a 28-day cycle update at the airport is a real
pain (slow LTE, no signal, can't sit and wait), build this.

### DEPLOY-RSYNC  Friend-friendly deploy story (rsync recipe)
Status: **OPEN**
Multiple friends will be setting up displays — current workflow has too
many manual steps.  Consolidate into one place:
  - SRTM3 hand-off: `tools/compact_srtm.py --output-dir` on pi4 →
    rsync to pi_zero (already implemented; document the recipe)
  - Water masks hand-off: rsync pi4's `data/water/` → pi_zero
  - State-lines cache: rsync pi4's `data/natural_earth/` → pi_zero
    (pi_zero won't need pyshp at all in this mode)
  - Single bootstrap script that takes a pi4 hostname and does all
    three rsyncs end-to-end
End-state: friend boots a fresh pi_zero, runs one command, has
working terrain + water + state lines without doing any Mapzen
downloads.

### DOCS-SWEEP  README + user manual refresh
Status: **OPEN — recurring**
Last session landed PR #8 (77 commits, screen sync, MFD parity, Pico
2W, IAS enable, mag-cal tumble, terrain backdrop, etc.).  Manuals
and README don't yet cover:
  - Screen sync setup + per-category TX/RX UX
  - MFD MAP LAYERS row + AIRPORT DATA type filters
  - Pico 2W (RP2350) BOOTSEL + UF2 flow
  - COMPACT button + tools/compact_srtm.py
  - TUMBLE mag-cal flow + when to use it
  - Updated zoom ladder (1/2/5/10/20/40/80/160/AUTO) on pi_zero MFD
  - Heading-source AUTO + 3 kt threshold
README needs the deploy story from DEPLOY-RSYNC once that's nailed.
Done this session (winds/FPV + weather/traffic): both pilot manuals now
cover the **flight-path vector**, **winds aloft (WND)**, the full
**weather suite** (RADIO/AUTO/INET sources, OVLY cycle, MET page with
the METAR/TAF/AIRMET/SIGMET/NOTAM readout picker, graphical
AIRMET/SIGMET, NEXRAD), **traffic** (diamonds/threat/TFC/detail-card/
declutter + collision alert), the **full-screen MFD** chrome, and the
**NOTAM** key fields.  `ADSB_IN.md` shipped items moved to Implemented;
`README.md` roadmap refreshed.  iPhone manual covers the FPV.
Also done (full-update pass): **Pi 4 / Pi 5** reframe + the three
**display profiles** (roadom_7 / roadom_10 / waveshare_35) in pi4 §1 and
README; **Screen Sync** setup (pi4 §12A, pi_zero §12); **TUMBLE**
hard-iron mag-cal (pi4 §11); **COMPACT** + `tools/compact_srtm.py`
(pi_zero §14, pi4 §14); **Pico 2W** BOOTSEL/UF2 flashing + the
**multi-display deploy (rsync)** recipe + winds-sharing note (README);
**flight-path-vector** preview wired into the render tool, with image
refs + a live-capture catalog for the WND/MET/TFC/MFD shots in
`pi4/previews/README.md`.
Still pending: only the internal **REQUIREMENTS_* / TEST_PROCEDURE_***
docs (architectural/QA, not pilot-facing) and **regenerating the actual
preview PNGs** on a Pi (the cloud box has no display/GL — run
`tools/regen_previews.sh` + the live `fbgrab` captures).

### PI4-ETE-LATENCY  pi4 inset ETE takes a beat to populate
Status: **OPEN — investigate**
PiZ data-strip ETE updates instantly when a D2 is activated; pi4's
inset ETE shows `--:--` for a noticeable interval before catching up.
Both code paths now use great-circle distance (per PR #8 ETE fix),
so it's not a math thing — likely a stale-cache or projector-recompute
in the inset draw path on pi4.  Check whether pi4's inset uses a
quantised render cycle that delays first-frame ETE pickup.

### SYNC-CACHES-PI4-TO-PIZ  Push water + state-line caches pi4 → piZ
Status: **OPEN — covered by DEPLOY-RSYNC but worth tracking**
On users who have both displays, pi4 already does the heavy lift
(downloading Natural Earth, pyshp water-mask rasterisation, etc.).
Pi_zero should be able to pull the cooked `data/water/` and
`data/natural_earth/` from pi4 over the network — no need to repeat
the work or even install pyshp on pi_zero.  Subset of the
DEPLOY-RSYNC story; included here as a standalone item in case it
gets implemented as an in-app "Sync from pi4" button instead of a
bare rsync.

### MAP-POLYLINE-WIDE-ZOOM  Inset/MFD still not snappy at 160 nm
Status: **OPEN — acceptable for now, revisit if it bites**
After 2bbb7fe (vectorised polyline projection in `_draw_polylines`,
both `pi4/moving_map.py` and `pi_zero/moving_map.py`), zoom levels
20–80 nm are snappy and 160 nm is "better, not amazing".  Bench
note: at 160 nm the AI was previously laggy across all three axes
because the polyline layer was dropping the render loop below 30 FPS;
that's gone now, but 160 nm still has visible cost from the surviving
admin_1 and admin_0 vertex counts inside the cull window.

Next lever (one-line change, no quality loss at extreme zoom because
inset is ~1.5 px/nm there → vertices closer than ~3 nm are sub-pixel):
add stride decimation inside the `for idx in visible_idx:` loop,
between the `ring = points[s:e]` line and the vectorised projection:
  ```python
  stride = 1 if range_nm < 80 else (2 if range_nm < 160 else 4)
  if stride > 1:
      ring = ring[::stride]
  ```
Keep the threshold conservative — at 80 nm the inset is still ~3 px/nm
so stride=1 is right there.  Only kick in at ≥80 nm.

If even that isn't enough, the *next* lever would be moving the
per-polyline draw call into a single `pygame.draw.lines` flush over
all visible rings (with `None`-terminated breaks) — but pygame doesn't
expose that primitive, so it'd mean dropping to `pygame.gfxdraw` or
keeping the per-ring call.  Don't go there until stride decimation
has been tried and proven insufficient.

LOD caches were considered and explicitly rejected — see commit
message on 2bbb7fe and the design discussion in chat: the rasterised
water-mask pipeline already covers the "huge vertex count, fill
rendering" case (lakes + ocean via `tools/build_water_tiles.py` +
`pi4/svt_renderer_gl.py`'s fragment shader sample), so the only
remaining vector-line layer (state + country borders) doesn't have
enough vertex count to justify a build-time LOD pipeline once
vectorisation removes the Python per-vertex overhead.

### WINDS-INET-REFETCH  Internet winds aloft freeze at startup, don't follow the route
Status: **FIXED — settle gate corrected + route-corridor winds added (pi4 + pi_zero)**
Resolution: (1) Both `WxClient.run` and `AwcPoller.run` replaced the
exact-equality `settled` test with a pan-DRAG debounce — a slice is
treated as a drag only when the view jumped more than
`max(move_frac·radius, 2 nm)`; ownship motion (even a ~30× time-
compressed sim, ~0.9 nm/slice) stays under that, and the periodic
`interval_s` refresh is always allowed through regardless. So
view-driven products (winds, TAF, AIRMET/SIGMET, NOTAM) now follow the
aircraft instead of freezing at the departure field. (2) New
`fetch_winds_route` / `_route_winds_points` in `shared/wx.py` build a
corridor along an active direct-to / flight-plan course — samples down
the polyline (ownship first, then remaining legs) with ±`WINDS_ROUTE_
WIDTH_NM` (=25 nm, `shared/config_base.py`) lateral offsets, deduped and
capped at 96 points so the batched Open-Meteo request stays bounded.
`_winds_fetch` (pi4 + pi_zero) uses the corridor when a course is active
and falls back to the visible-area grid otherwise. Verified: PHX→ABQ
(~285 nm) yields 60 corridor points covering both ends with lateral
spread; settle gate passes flight motion and rejects finger-drags;
`test_wx.py` + `test_fisb.py` still pass. Remaining nicety (not blocking):
`force_refresh()` on a D2/FPL change so the corridor switches instantly
rather than on the next move/periodic tick.
Field-test follow-ups (same session): (a) the corridor must *augment*,
not replace, the visible-area grid — a D2 to a near waypoint shrank the
winds page to ~3 clustered barbs. `fetch_winds()` now always builds the
full visible grid and adds the route corridor on top (dedup + cap 96),
so the page fills at any zoom. (b) The WND page now drops the
hypsometric terrain tint like the METAR/NEXRAD pages (`wx_active` in
`moving_map.py`) — on pi4 the 80 nm tint build pulls a huge SRTM-tile
set that could OOM/lock the display; with the winds poller now active
(no longer frozen) the contention tipped it over. pi_zero already caps
the tint below 80 nm so this is parity-only there.
Performance redesign (field-test round 2): the fetch-per-view model
hammered Open-Meteo (stalls of seconds–minutes on every pan/zoom) and
the draw re-rendered a `font.render()` temp tag per barb per frame
(~0.5–1 ms each — the real "barb draws are slow" cause). Reworked around
"cache wide, draw the subset":
  - **Wide cached grid.** `_winds_view` now returns a FIXED wide range
    (`WINDS_CACHE_RANGE_NM` = 110) instead of the zoom, with a constant
    `WINDS_GRID_SPACING_NM` (20 nm) barb spacing; `fetch_winds` builds
    that square grid (+ corridor beyond the cache for long routes). The
    poller only re-pulls on a big centre drift (½ the cache) or the
    periodic timer — so zoom and small pans need NO network.
  - **Decimate on draw.** `_winds_decimate` keeps at most one barb per
    screen cell (~one per `min(w,h)/3.5`), so any zoom shows a clean
    ~12–20 evenly-spread set — fixes the inset's wide-zoom pile-up and
    the short-D2 centre stack, and slashes draw cost.
  - **Glyph cache.** `_wx_glyph` memoises rendered temp/LV text, turning
    the per-frame `font.render` storm into blits.
  - Zoom-in refetch bug (strict `<0.5×`) made moot by the fixed cache
    range, but the symmetric `1.4×/0.7×` threshold is kept for the
    other view-driven pollers.
Net: winds follow the aircraft, pan/zoom is instant, draw is cheap, and
the data covers a wide area + the route. Open items: at very tight zoom
(≤20 nm) the fixed-spacing cache is naturally sparse (winds are smooth,
so acceptable); tune `WINDS_GRID_SPACING_NM` / `WINDS_CACHE_RANGE_NM` if
denser close-in barbs are wanted.
Original diagnosis below for reference.
Status (orig): **OPEN — diagnosed, fix not yet applied**
Target: `shared/wx.py` (`WxClient.run` / `AwcPoller.run` settle gate),
`pi4/pfd.py` `_winds_view` / `_winds_fetch`, `shared/fisb.py`
`set_winds` (replace-vs-accumulate).
Symptom (reported from an all-night sim run out of PHX): the INET winds
barbs loaded once for the local area around the departure field and
never updated as the aircraft flew — no winds along the route, no new
columns appearing en route.
Diagnosis — two separate problems:
  1. **Settle-debounce starves a moving aircraft of refetches.** Both
     `WxClient.run` and `AwcPoller.run` only fetch when the view has
     "settled": `settled = (cur == prev_view)` where
     `cur = (round(lat,2), round(lon,2), round(radius))`, compared
     between consecutive ~0.7 s poll slices. round(,2) is ~0.01° ≈ 0.6
     nm. A continuously-moving ownship (and especially a
     time-compressed overnight sim) changes that rounded tuple on most
     slices, so `settled` is rarely/never true — and BOTH the
     move-based AND the periodic (`interval_s`) refetch are gated
     behind it. Result: the only fetch that fired was the one on the
     ground at PHX where the view genuinely sat still. The debounce was
     meant to avoid a burst of fetches mid pan-drag, but it wrongly
     also suppresses ownship motion. Affects winds, TAF, AIRMET/SIGMET,
     NOTAM, NEXRAD — every view-driven poller shares this pattern.
  2. **Replace-not-accumulate + tiny grid = no route picture.**
     `set_winds()` deletes all prior INET columns each poll
     (`shared/fisb.py:1197`), and `_winds_view` requests only a
     `map_zoom_nm`-sized grid (default 5 nm in PFD mode) centred on the
     ownship. So even with refetch working you'd only ever hold a ~4 nm
     patch around the aircraft — never the whole route.
Fix sketch:
  - Replace the exact-equality settle test with a tolerance + dwell:
    treat the view as settled when it hasn't moved more than a small
    fraction of the radius for ~1–2 s (debounce drags) but never let
    that gate block the periodic `interval_s` refresh or a large
    ownship move. Cleanest: compute `settled` from a distance
    threshold against the last *evaluated* view, and always allow the
    periodic timer through regardless of settle.
  - For route coverage, decide between (a) widening the winds grid to a
    forward-biased corridor along the active FPL/track, or (b) keeping
    the local grid but bumping the grid extent so barbs read for the
    visible MFD range. (a) is the real fix; (b) is the quick one.
  - Add a one-line "[WX] winds refetch @lat,lon" debug print behind a
    flag so the next sim run can confirm the poller is following.
Bench/sim repro: start at PHX, fly a long leg (time-compressed if
possible), watch whether the winds barbs re-centre. Pre-fix they stay
at PHX; post-fix they should track the ownship.

### FPV-PIZ  Port the flight-path-vector marker to pi_zero
Status: **OPEN — pi4 + iPhone done (see FPV), pi_zero pending**
Target: `pi_zero/pfd.py` AI overlay block (~the
`draw_airport_symbols` / `draw_obstacle_symbols` SRCALPHA-rotate path).
Context: pi4 + iPhone shipped the FPV marker (see the FPV entry). pi_zero
draws its AI symbol overlays into a roll=0 SRCALPHA surface that is then
rotated whole by `pygame.transform.rotate(_overlay, roll)`, rather than
the per-feature roll rotation pi4 uses. Dropping the FPV into that
overlay would rotate the *symbol* with the bank too, which isn't the
convention (the FPV glyph should stay upright). Port options:
  - Compute the FPV screen position the pi4 way (rel-bearing + FPA →
    px, manual roll rotation) and blit the upright glyph directly onto
    `surf` after the rotated overlay, OR
  - Draw it into the overlay but counter-rotate the glyph by `-roll`.
Reuse the same 5 kt gate, cyan colour, circle+wings+stub geometry, and
edge-clamp ghost arrow. Add the `fpv_enabled` toggle to pi_zero's
display-settings screen for parity.

### NAVDATA-FAA  IFR nav-data foundation — fixes, airways, navaids, procedures
Status: **OPEN — data access confirmed (US, free FAA sources); foundation for the IFR FPL items below**
Target: new `tools/build_navdata_us.py` (28-day converter, mirrors
`tools/build_airspaces_us.py`), new `shared/navdata.py` parser + spatial
query (mirrors `shared/airports.py`), runtime cache under
`data/navdata/`.
Data-access answer (the open question on approach fixes + victor
airways): **yes, all of it is available for free, US-only.** Two FAA
28-day subscription products cover everything the FPL items need —
none of it is in the OurAirports CSVs we ship today:
  - **FAA NASR** (National Airspace System Resource, 28-day
    subscription, free at the FAA ADDS / NASR portal) — the `FIX`
    file gives named intersections/fixes with lat/lon; the `AWY`
    file gives Victor (low) and Jet (high) airways as ordered fix
    sequences; the `NAV` file gives VOR/NDB/DME navaids (also in
    OurAirports `navaids.csv` if we want a global-but-navaid-only
    fallback).
  - **FAA CIFP** (Coded Instrument Flight Procedures, ARINC 424 /
    "424-18" fixed-width, 28-day, free at the FAA CIFP download) —
    carries SIDs, STARs, **approaches** (with transitions + final
    approach fix), **missed-approach** legs, and **holding-pattern**
    definitions (`PI`/`HM`/`HF`/`HA` leg types) keyed to airport +
    procedure ident.
Scope decision (pilot): **US-only is accepted** for all the IFR FPL
items — no need to chase a global procedure source. Still surface it
in the UI: gate the IFR features behind a "NAVDATA loaded" badge (same
pattern as NO APT / EXP APT) so it degrades gracefully outside US
coverage rather than silently showing nothing. Note these build on
the **synthetic glideslope already drawn on an active direct-to**
(`disp["nav"]`, ~line 415–427) — FPL-APPROACHES generalises that
hand-built threshold guidance to a published procedure.
Distribution: too big + procedural to
fetch in-aircraft cleanly — strong candidate to ride the
DATA-USB-BUNDLER pipeline (add `navdata` to the bundle manifest)
rather than an in-Pi DOWNLOAD button.
Work items:
  - `tools/build_navdata_us.py`: parse NASR FIX/AWY/NAV + CIFP into a
    compact numpy/JSON cache (sorted by lat for `np.searchsorted`
    spatial query, same trick as airports/runways).
  - `shared/navdata.py`: load + spatial-query API — `nearby_fixes`,
    `airway(ident)`, `navaid(ident)`, `procedure(airport, ident)`,
    `hold(fix)`. Shared by pi4 + pi_zero + (later) iPhone.
  - Decide cache format + manifest stamp (28-day cycle date) so a
    stale-data badge can be shown.
This entry is the prerequisite for NAV-FIXES-AIRWAYS-DISPLAY,
FPL-APPROACHES, and FPL-HOLDS below.

### NAV-FIXES-AIRWAYS-DISPLAY  Approach fixes + Victor airways on map/AI
Status: **OPEN — blocked on NAVDATA-FAA**
Target: `pi4/moving_map.py` + `pi_zero/moving_map.py` (new fix/airway
layers), `pi4/pfd.py` + `pi_zero/pfd.py` MAP LAYERS row, optional
fix symbols projected onto the AI via the airport-projection helper.
Context: once `shared/navdata.py` exists, render named fixes
(intersection triangles + ident labels) and Victor airways
(thin lines along the fix sequence, with airway ident) on the
moving-map inset / MFD. Mirrors the existing polyline + symbol layers.
Work items:
  - New `_draw_fixes` + `_draw_airways` in `moving_map.py` (reuse the
    vectorised `_draw_polylines` for airways; fixes are point symbols
    like obstacles/airports).
  - Two new pills on the MAP LAYERS row (FIX / AWY), persisted with the
    rest of the layer toggles. Gate display ≥ a sensible zoom so fixes
    don't clutter wide views.
  - Fixes become selectable waypoints for the FPL editor (feeds
    FPL-APPROACHES — a fix ident resolves to lat/lon via navdata).
  - iPhone parity deferred (same pattern as the #17 airport-overlay
    port).

### FPL-APPROACHES  Approaches + missed approaches in flight plans
Status: **OPEN — blocked on NAVDATA-FAA**
Target: pi_zero FPL editor (`pi_zero/pfd.py` — it owns FPL editing
today; pi4 renders the active leg via the synced `disp["fpl"]`),
`disp["fpl"]` schema, screen-sync `KIND_FPL`, the direct-to / leg
sequencer (`_fpl_is_active`, `_fpl_check_advance`,
`_FPL_ADVANCE_DIST_NM`).
Context: today an FPL is a flat list of waypoints with a simple
distance-gated auto-sequencer. Loading a CIFP **approach** appends its
ordered leg sequence (transition → IAF → IF → FAF → MAP) to the
active plan, and the **missed approach** is held as a separate
segment that the sequencer only arms past the MAP / on a pilot
"activate missed" action. The system already auto-points the
direct-to at a runway threshold and draws a glideslope on approach
(see `disp["nav"]` notes ~line 415–427) — this generalises that to a
published procedure.
Work items:
  - Extend the FPL schema: a waypoint gains an optional `leg_type`
    (ARINC-424 path/terminator: TF/CF/DF/RF…) and a `segment` tag
    (`enroute` / `approach` / `missed`). Keep flat-list back-compat so
    existing saved plans still load (`fpl_saved` is `[{name,
    waypoints}]`).
  - FPL editor: "LOAD APPROACH" picker (airport → approach ident →
    transition) that pulls legs from `navdata.procedure(...)` and
    appends them; "ACTIVATE MISSED" control that switches the
    sequencer onto the missed segment.
  - Sequencer: honour leg types enough to fly the common cases (TF/CF
    as great-circle-to-fix is most of GA IFR); don't attempt full
    ARINC-424 leg geometry on day one — document which leg types are
    approximated.
  - Screen sync: the richer schema rides the existing `KIND_FPL` /
    `KIND_FPLLIB` channels — bump a schema version so a peer on the
    old schema degrades to the flat waypoint list instead of choking.
  - Render: pi4 + pi_zero draw the approach legs distinctly from
    enroute (e.g. dashed for missed), FAF/MAP labelled.

### FPL-HOLDS  Holding patterns
Status: **OPEN — blocked on NAVDATA-FAA**
Target: same FPL path as FPL-APPROACHES, plus a racetrack-geometry
helper in `moving_map.py` and a hold-entry cue.
Context: holds come from CIFP (`HM`/`HF`/`HA` leg types: hold-to-
manual-termination / -to-fix / -to-altitude) keyed to a fix, and a
pilot may also build an ad-hoc hold at any fix (inbound course, turn
direction, leg length/time). Render the racetrack on the moving map
and sequence the FPL through it.
Work items:
  - Hold definition: fix + inbound course + turn direction (L/R) +
    leg length (nm or time). Pull published holds from
    `navdata.hold(fix)`; allow a manual hold dialog for the ad-hoc
    case.
  - Geometry: racetrack polygon (two semicircles + two straights)
    computed from the hold params, drawn on the inset/MFD; own-ship
    chevron shows progress around it.
  - Entry guidance: classify direct / teardrop / parallel from the
    inbound heading vs hold course and show the recommended entry —
    nice-to-have, can ship the racetrack draw first.
  - Sequencer: a hold leg does NOT auto-advance on the
    `_FPL_ADVANCE_DIST_NM` gate; it loops until the pilot taps
    "EXIT HOLD" (then resume the next leg). This is the one place the
    existing distance-gated sequencer needs a real state change.

---

## Completed

### AHRS-ROLL-YAW-COUPLING  Pure bank input produces significant heading change — **FIXED**
Target: `firmware/ahrs_filter.py`, `firmware/config.py`, `firmware/main.py`.
Root cause from a bench debug-trace ($AHRSDBG): the Pico-side Mahony
filter's mag fusion was over-trusting the magnetometer during fast
rotation.  When the chip moves through a non-uniform external field
(bench iron, panel iron, alternator gradient) the mag vector swings in
both direction AND magnitude (saw a 22 % magnitude spike between level
and ±30° pitch — earth's field is constant, so that swing was purely
positional).  The mag-fusion math is correct but cannot tell "chip
rotated" from "chip moved through clutter", so it interpreted the
transient as a yaw error and applied a spurious correction.  With the
original `AHRS_KP_MAG = 0.5` this drove ~9°/s of yaw drift during a
fast roll — visible immediately as heading-on-bank coupling.
Fix is pure tuning, no math change:
  - **`AHRS_KP_MAG`: 0.5 → 0.10.**  WT901's own internal Kalman runs
    gains in this ballpark; long-term yaw drift is anchored by the
    `AHRS_GPS_TRACK_*` slaving once in flight (GS > 20 kt @ 0.02 α).
  - **Gyro-rate mag gate.**  Mag weight ramps linearly from full at
    `|gyro| ≤ 10°/s` to zero at `|gyro| ≥ 30°/s`.  Standard-rate turns
    (~3°/s) and ordinary coordinated maneuvering stay well below the
    lo gate; aggressive bench tumbling and aerobatic-grade rolls get
    gated out, so the filter rides the gyro through the transient and
    re-engages mag once motion settles.  Configurable via
    `AHRS_MAG_GYRO_GATE_LO_DPS` / `AHRS_MAG_GYRO_GATE_HI_DPS`.
  - **Diagnostic surface.**  `ahrs_filter.last_mag_weight` exposed; the
    temporary `$AHRSDBG` print includes the active gate value so the
    behaviour can be verified.  `AHRS_DEBUG_PRINT` flag added (default
    `False` — was flipped True for the bench session, restored).
Bench verification: pre-fix a single ±55° bank produced ~40° of
sensor-yaw drift.  Post-fix the same maneuver holds within ~3°, and
the gate is clearly seen firing in the trace (`mag_w` drops to ~0
when gyro Y is > 25°/s and returns to 1.0 once motion settles).  Real-
flight verification still pending — coordinated turns at standard rate
should keep mag at full weight, so flight behaviour should be
indistinguishable apart from a slightly slower long-term yaw
convergence (anchored by GPS-track slaving).

### AGL-PRECISION  AGL readout shouldn't show 1-foot precision — **FIXED**
Target: `pi4/pfd.py` `draw_agl_readout`. Fix: round the displayed
AGL value to the nearest 10 ft so the last digit doesn't flicker on
GPS-alt / SRTM-elevation noise (both inputs only have 10–30 ft real
precision). Matches the rolling-drum altitude tape's
minimum-resolved-value treatment.

### AHRS-GIMBAL-LOCK  WT901 Euler unusable near high bank — **FIXED**
Target: `firmware/wt901.py`, `firmware/main.py`, `shared/serial_client.py`,
`pi4/pfd.py`. Fix: enabled the WT901's quaternion stream
(`PKT_QUAT = 0x59`) at boot, parse `q0..q3` alongside the existing
Euler frame, serialise quaternion into the `$AHRS` JSON, and drive
the AI horizon math off the quaternion's body-up basis vector instead
of going through Euler near the ±90° singularity. The pitch-on-yaw
sensitivity at high bank is gone; AI tracks smoothly through full-roll
attitudes.

### AHRS-GPS-AID  GPS/IAS-aided AHRS for clean attitude in turns — **FIXED**
Target: new `firmware/ahrs_filter.py`, raw-mode IMU output from
`firmware/wt901.py`, `firmware/main.py` plumbing. Fix: Madgwick fusion
on the Pico 2 W (RP2350 FPU makes the per-sample loop free), centripetal
accel `V × ω_gyro` subtracted from raw accel before the level-finding
step. Velocity source ladder: **TAS** (SDP33 + BME280) → **GS**
(GPS speed_kt) → **basic** (no centripetal correction). Active source
surfaced on the `$AHRS` packet as `att_aid`. 25° banked turn at 100 kt
now reads steadily; the leans-during-coordinated-turn artefact is gone.
Pairs with AHRS-MAGCAL — both landed together.

### AHRS-MAGCAL  WT901 magnetometer calibration procedure — **FIXED**
Target: `firmware/wt901.py`, `firmware/main.py`, `firmware/web_server.py`,
pi4 / pi_zero cal modals. Fix: hard-iron tumble flow — pilot taps
TUMBLE, rotates the unit through all axes for ~30 s while the firmware
collects min/max on mag X/Y/Z and solves for the hard-iron offset
(ellipsoid centre). Persisted to flash and applied in `wt901.py` before
yaw is computed. `/magoff?action=tumble_start|tumble_finish` HTTP
endpoints drive it from the displays; pi4 modal landed in PR #7
(commits `a560924`, `8280f68`, `51705cd`). pi_zero port is tracked
separately as MAGCAL-PIZ-TUMBLE.

### IPHONE-PICO-HOSTING  iPhone HTML too large for Pico W — **FIXED**
Target: `firmware/web_server.py`, hardware swap. Fix: resolved by the
Pico 2 W (RP2350) upgrade — 520 KB SRAM (vs the Pico W's 264 KB)
gives ~400 KB free MicroPython heap, comfortably absorbing the 106 KB
`index.html` read + encode buffer + SSE state. No code change to the
synchronous `_load_index()` was needed in the end; the bigger heap
buys headroom for future growth too.

### #7  Demo smoothness — sinusoidal interpolation — **FIXED**
Target: `DemoState` in pi4/pi_zero. Fix: covered by the full sim mode
that landed in the recent merge — the sim flight model produces
realistic continuous motion (proper accel/decel, banked turns, climb /
descent dynamics) so the original linear DemoState easing concern is
moot. The standalone `DemoState` interpolation issue no longer applies.

### #12b  iPhone compass GPS-track auto-cal — **FIXED**
Target: `iphone_display/index.html` `_onOrient`, `applyPhoneSensors`,
`COMPASS_CAL`. Fix: auto-cal mode landed alongside the cardinal
walk-through (#12a). When GPS groundspeed > 15 kt and the compass is
live, the iPhone now folds `gps_track − compass` into
`COMPASS_CAL.offset` through a low-pass filter (straight-and-level
gate on |roll| < 5° to keep crab / wind from biasing the sample), and
surfaces a "CAL: AUTO" badge while it's actively learning. Pairs with
the AHRS-MAGCAL firmware tumble cal so both compasses converge on
GPS track in flight.

### #15  iPhone V-speeds editor UI — **FIXED**
Target: `iphone_display/index.html` setup menu. Fix: new V-SPEEDS
panel alongside TERRAIN / BAROMETER / TRIM / SENSORS. Eight
numpad-driven entries (Vs0, Vs1, Va, Vfe, Vno, Vne, Vy, Vx) reusing
the existing bug-edit numpad style; commits write through to
`localStorage['vspeeds']` in the same JSON shape the init reader
already understands. Ordering validation (Vs0 < Vs1 < Vfe ≤ Vno < Vne)
surfaces an inline error instead of silently storing bad values.
Header reads "V-SPEEDS (knots)" matching pi4 so the unit is explicit
even when the speed tape is on mph.

### DOCS-SWEEP  Refresh user manuals + previews infrastructure — **FIXED**
Target: `Docs/USER_MANUAL_PI4.md`, `Docs/USER_MANUAL_ZERO.md`,
`pi4/previews/README.md`, `pi_zero/previews/README.md`, new
`tools/regen_previews.sh`.

**piZ manual gaps closed** (USER_MANUAL_ZERO.md):
  - §8 Setup Menu — added the multi-finger gestures table:
    2-finger 0.8 s → setup; 3-finger 2 s → swap PFD ↔ MFD.
  - §10 Display Settings — BRIGHTNESS now documents the actual GPIO 18
    PWM transport, ExecStartPre setup, the non-linear duty-cycle table
    (level 1 = 22 % conduction floor, level 10 = 100 %), and the
    journalctl one-liner to diagnose a no-op slider.  New MAP LAYERS
    section with the six-pill table (TER / WTR / APT / OBS / STA / CTRY).
  - §13 System — new ENABLE MFD subsection covering the gate, the
    migration from legacy `display_mode == "mfd"`, and the
    forced-PFD-on-disable behaviour.  Persistence paragraph now lists
    `mfd_enabled` + MAP LAYERS in the saved-settings list.

**pi4 manual gaps closed** (USER_MANUAL_PI4.md):
  - §10 Display Settings table — added MAP LAYERS row with the six
    pills documented (matches piZ).
  - §14 Terrain Data — water-mask download now also fetches admin_0
    (country) lines, not just admin_1.  Added a country-lines bullet
    in the dataset list at line 550, distinct tan colour from the
    slate-blue admin_1 lines.
  - §16D Moving-Map Inset — layer list now includes country lines, and
    the per-layer-visibility paragraph points at the MAP LAYERS row.

**Previews infrastructure**:
  - `tools/regen_previews.sh` — new single-entry script that takes
    `pi4`, `piz`, or `all` (or auto-detects from `/proc/cpuinfo`).
    Wraps `tools/capture_pi4_previews.sh` for the GL-heavy pi4 path;
    runs `pi_zero/pfd.py --screenshots ...` for the pure-pygame piZ
    path.  Stops `pfd.service` for the duration so SDL doesn't fight
    over the framebuffer; restores it on exit (trap on RETURN).
  - Both previews/README.md files updated with the regen command.

Out of scope (intentional skip):
  - README.md V7 roadmap mention — aspirational, not stale.
  - TEST_PROCEDURE_*.md — internal QA docs, not user-facing.
  - REQUIREMENTS_*.md — architectural docs, separate concern.
  - USER_MANUAL_IPHONE.md — audited, no user-visible changes today.

### MAP-POLYLINE-VECTORISE  Vectorise polyline projection — **FIXED**
Target: `_draw_polylines` in `pi4/moving_map.py` and
`pi_zero/moving_map.py`. Fix: commit `2bbb7fe`. The hot loop was
`pts = [project_fn(float(la), float(lo)) for lo, la in ring]` —
~1 µs of Python call overhead per vertex, multiplied by hundreds of
vertices per admin_1 polyline and ~5–10 visible polylines at wide
zoom = 5–10 ms per frame on the polyline layer alone, dropping the
render loop below 30 FPS and making the AI visibly lag across all
three axes on the AHRS bench. Replaced with whole-array numpy ops:
`e_px = (ring[:, 0] - lon) * lon_scale` etc.; rotation always applied
(identity when `rot_deg = 0`); `column_stack + tolist` builds the
pygame point list in one pass. Bench result: 20–80 nm now snappy,
160 nm "better, not amazing" (see MAP-POLYLINE-WIDE-ZOOM for the
next lever — stride decimation, deferred). No quality loss, no
NPZ format change.

### STATE-LINES-COUNTRIES  Add admin_0 country lines layer — **FIXED**
Target: `pi_zero/pfd.py`, `pi4/pfd.py`, `pi_zero/moving_map.py`,
`pi4/moving_map.py`. Fix: refactored the three `_sl_*` Natural Earth
helpers into generic `_ne_ensure_shapefile`, `_ne_build_cache`,
`_ne_load_cache` (each takes the shapefile / npz name as arg), then
added parallel `_country_lines` cache + `_CL_NE_NAME =
"ne_10m_admin_0_countries"` constants on top. Download thread fetches
admin_0 after admin_1; pi_zero gets the same 5 s lazy-load throttle so
an rsync'd npz lands without a restart; pi4 loads both at startup.
moving_map's `_draw_state_lines` generalised to `_draw_polylines(...,
color)` and the render() public API gains a `country_lines=` kwarg.
New "CTRY" pill on the MAP LAYERS row (default ON), new `(200, 180,
140)` warm-tan colour distinct from the state-line slate-blue so the
two layers stack visibly where admin_1 perimeters happen to match a
country border. Same `range_nm >= 20` gating as state lines.

### MAP-INSET  2D moving-map inset in the lower-left corner — **FIXED**
Target: new `pi4/moving_map.py`; render hook in `pi4/pfd.py`; new
toggles in Display setup; persistence in `pi4/data/settings.json`.
Fix: commit `8745e03`. Pure-pygame inset (no GL context contention)
that reuses the airport, runway, obstacle and SRTM caches the SVT
already keeps loaded. Layers: hypsometric terrain tint (cached
surface keyed on quantised centre + range + orient, rebuilt only on
pan/zoom), runways, obstacles, airports, direct-to course +
waypoint diamond, own-ship chevron, range ring, frame + corner
labels. Track-up rotates the cached tint by current track; north-up
keeps north up and the chevron rotates to track. Six discrete zoom
levels (1/2/5/10/20/40 nm). Pinch-to-zoom (two-finger FINGERMOTION,
1.35× ratio per step) plus a single-tap fallback (left half = zoom
out, right half = zoom in) so the inset is usable even when the
touch driver doesn't surface FINGERMOTION events. Display setup
gains MAP INSET (OFF/ON + TRK↑/N↑ packed), MAP RANGE, SUN POSITION
and MAP LAYERS (TER/WTR/APT/RWY/OBS multi-toggle); the unused
NIGHT MODE placeholder retired to make room.

### SUN-POSITION  Real-time sun position drives terrain shading — **FIXED**
Target: new `pi4/sun.py`; `pi4/svt_renderer_gl.py`
(`render_svt_gl`, `render_svt_into_current_fb`); render hook in
`pi4/pfd.py`. Fix: commit `8745e03`. NOAA solar-position formulas
take UTC + GPS lat/lon, return azimuth, elevation, and a civil-
twilight intensity ramp (-6° → 0, +6° → 1) so dawn / dusk fades
smoothly into ambient instead of stepping. The two GL render
entrypoints accept optional `sun_az_deg` / `sun_el_deg` /
`sun_intensity` kwargs; passing `None` falls back to the current
SE / mid-morning module constants, which preserves the previous
behaviour exactly when SUN POSITION is set to FIXED on the Display
setup. Per-frame compute is cheap (sun moves ~0.25°/min, but
re-evaluating per frame is ~5 µs).

### RUNWAY-VECTORIZE  Runway symbol overlay vectorised — **FIXED**
Target: `pi4/pfd.py` `draw_runway_symbols`, `shared/runways.py`.
Fix: commit `878fe28` mirrors the airports.py optimisation — sort
the structured array by midpoint latitude at load, cache contiguous
lat/lon columns at module level, then bound candidates with
`np.searchsorted` before any per-row work. Dense-area test (~50
candidate rows in slab) drops 0.97 ms → 0.54 ms (1.8×); sparse
queries drop 13.6×. In `pi4/pfd.py`: `surf.set_clip` hoisted out of
the per-runway loop (was set/restored per runway, now once per frame
around the whole pass); duplicate set_clip dropped from
`_draw_extended_centerline`; `nm_per_deg_lon` threaded into
`_project_latlon` so `cos(radians(lat))` is computed once per draw
instead of on every projection (10+ per runway, 24+ per centerline);
list+min/max corner-bbox tests replaced with scalar comparisons.
On-disk cache format unchanged — older un-sorted .npy files re-sort
in memory at load.

### SVT-FPS-TURNS  Steep-bank FPS dip — **RESOLVED IN PRACTICE**
Target: `pi4/svt_renderer_gl.py` `FRAGMENT_SHADER`.
Context: previously flagged as 15–20 FPS dip during steep banked
turns vs. 30 FPS baseline. Field testing reports turns hold up
fine now — no longer a pressing issue. Specific shader-cost knobs
(LUT for `clearance_color()`, coarser per-fragment normal,
`MESH_GRID_N` 300→200) are still available if a future regression
brings this back, but no work is queued.

### #1  GL SVT — pygame.OPENGL shared-context composite — **FIXED**
Target: `pi4/svt_composite_gl.py` (new), `pi4/test_svt_composite.py` (new),
`pi4/svt_renderer_gl.py`, `pi4/pfd.py`, `pi4/config.py`.

Fix: full hardware bring-up of the shared-GL composite renderer on
Pi 4 + ROADOM display, plus a long sequence of correctness fixes
that surfaced once it ran live. End state: 30 FPS sustained terrain
+ overlays, rock-stable across mesh recentres, default renderer flipped
to `"opengl_shared"`. Commit chain on `claude/test-svs-display-OLAg2`:

  - `c16a793` — initial bring-up: SDL `GL_DEPTH_SIZE=24`, explicit
    depth-clear, `pygame.draw.polygon` SRCALPHA fill, on-screen FPS
    readout for hardware validation.
  - `8bb15ae` — world-grid mesh snap + camera-offset eye, thicker
    roll arc (`_ARC_THICK` 2→4), AA outlines for filled polygons,
    pitch-ladder lines doubled (major 2→4 px, minor aaline→2 px).
  - `137b298` — roll arc redrawn as `pygame.draw.lines` (was a
    band polygon producing a double-AA halo).
  - `b919733` — vertex Z = absolute elevation; `u_alt_m` per-frame
    uniform → smooth colour transitions across altitude. `u_world_offset`
    uniform → grid pattern stable across recentres.
  - `13f6e73` — drop unused `in_clearance` attribute (was crashing
    the VAO build after the shader refactor).
  - `b063eb9` — world-aligned sample grid (sample positions snap
    to multiples of `sample_step_m` from world origin). Doghouse
    `gfxdraw.aapolygon` halo removed.
  - `7952a62` — `snap_dlon` derived from snapped `mesh_lat` (was
    drifting per-frame off aircraft `cos(lat)`, causing rebuild
    every few frames; FPS hit + visible morph).
  - `92db1df` — angular sample grid + equator-equivalent grid
    pattern. Same world point always sampled at the same lat/lon;
    `v_world_pos.x = in_pos.x / cos(mesh_lat) + (mesh_lon * NM*60) mod GP`
    in the vertex shader → grid pattern in world coords, invariant
    across mesh recentres in any direction. Foreground rock-solid;
    grid stays locked to terrain features.
  - This commit — temporary FPS readout removed; default
    `SVT_RENDERER` flipped to `"opengl_shared"`.

Trade-off worth documenting: grid is now in fixed angular spacing
(lat/lon graticule) rather than fixed metric. At lat 33° each minor
cell is ~773 m east × 926 m north — slightly elongated, like a real
chart graticule. Acceptable for the world-anchoring it buys; if a
square-cell metric grid is needed later, introduce a fixed reference
latitude.

Residual: FPS dips to 15-20 during steep banked turns. Tracked
separately as SVT-FPS-TURNS (low priority).

### #12a  iPhone compass calibration — cardinal walk-through — **FIXED**
Target: `iphone_display/index.html` — new `#compass-cal-panel`,
`COMPASS_CAL` global, additions to `_onOrient`.
Fix:
  - HEADING setup panel gains a 🧲 CAL COMPASS button that opens a
    new full-width calibration panel.
  - Pilot points the aircraft at NORTH (000°), taps CAPTURE, repeats
    for EAST / SOUTH / WEST. Live RAW vs CAL readouts update at
    sensor rate while the panel is open. Restart and Clear-cal
    buttons reset the run / wipe the stored offset.
  - On the fourth capture we compute the circular mean of the four
    `(expected − raw)` deltas (sin/cos sums + atan2 — handles wrap)
    and persist it to `localStorage['compass_cal_offset']`.
  - Offset is applied in `_onOrient` immediately after reading
    `webkitCompassHeading`, so every consumer (display tape, GPS-vs-
    compass cross-source diamond, sensor readout) sees the corrected
    value. Raw `PS.compassHeading` is preserved for future
    calibration runs and for the SENSORS readout.
  - `drawHeadingTape` renders a small CAL subscript on the inboard
    side of the heading readout box whenever an offset is active and
    the displayed source is mag-derived. The SENSORS panel's compass
    row also gains a `(cal +N.N°)` annotation so the pilot can see
    what's been applied at a glance.
GPS-track auto-cal half-mode is tracked separately as #12b.

### #20a  iPhone heading AUTO mode flipped to TRK-first — **FIXED**
Target: `iphone_display/index.html` `_activeHdg()`, hdg-panel hint
copy. AUTO previously preferred MAG (compass) and fell back to TRK
(GPS track). Pilot feedback: GPS track is the more trustworthy of
the two on a moving aircraft (true-north, no mag drift, no panel-
iron bias), so AUTO now picks TRK whenever GPS speed > 3 kt and
falls back to MAG only when GPS isn't moving. Hint text updated.

### CROSS-SOURCE-DIAMOND  iPhone heading tape — show the *other* source — **FIXED**
Target: `iphone_display/index.html` `_activeHdg()` + `drawHeadingTape`.
When both compass and GPS track are live, the tape now draws a
filled diamond at the *other* source's value so the pilot can see
divergence (wind correction angle, magnetic variation, compass
drift) without having to flip modes. Diamond colour follows the
source convention: cyan = magnetic, magenta = GPS track. So in MAG
mode (white tape) you see a magenta diamond at the GPS track; in
TRK or AUTO modes (magenta tape) you see a cyan diamond at the
magnetic heading.

### #20  iPhone HDG vs TRK — enunciated, with user preference — **FIXED**
Target: `iphone_display/index.html` — new `HEADING` setup panel,
`_activeHdg()` resolver, and `drawHeadingTape` rewiring.
Fix:
  - Setup menu gains a 🧭 HEADING entry opening a panel with three
    mutually exclusive buttons: MAG, TRK, AUTO. Choice persists in
    `localStorage['hdg_mode']` (default AUTO).
  - New `_activeHdg()` returns `{ value, label, color, useTrack }`
    — the single source of truth for what to display and how to
    colour it. MAG always uses `D.yaw` (AHRS or phone compass), TRK
    uses `D.track` when GPS speed > 3 kt, AUTO prefers MAG when
    `D.ahrs_ok` else TRK when groundspeed is live. Unavailable
    preferred sources fall back with an amber `M?` / `G?` / `?`
    warning subscript.
  - `drawHeadingTape` now reads `hdg = src.value`, so the tape, the
    bug chevron offset, and the readout digits all agree with the
    subscript — no more silent mismatch between what's displayed
    and what the label claimed.
  - Enunciation via colour only (pilot preferred the quieter
    treatment): the heading readout box border and the `M`/`G`
    subscript take the source colour (white = MAG, magenta = TRK,
    amber = warning). The heading bug chevron stays cyan on MAG
    (pi4 convention for pilot-selected targets) and flips to the
    source colour on TRK / warning so a handover is visible
    without a pill, flash, or pulse.

### #19  iPhone VSI broken on GPS + VS bar too small — **FIXED**
Target: `iphone_display/index.html` — `PS` state, `_onGeolocation`,
`applyPhoneSensors`, and `drawAltTape`.
Fix:
  - GPS-derived vspeed: new `PS.altHist` rolling window keyed by
    `_onGeolocation` (pushes `{t, alt}` when the fix includes a
    3-D altitude). `_psComputeVspeed()` runs a least-squares slope
    over the last 5 s (requires ≥ 2 s span to report non-zero).
    `applyPhoneSensors` writes `PS.vspeed` → `TARGET.vspeed` when
    SSE is silent, so the VSI now moves when the pilot is only on
    phone sensors.
  - Pi4-style VS bar on the inside (left) edge of the alt tape:
    magenta needle growing from the midpoint against a ±2000 fpm
    scale (200 ft of tape ≡ 2000 fpm, matching pi4). Tick marks at
    ±500 / ±1000 / ±1500 / ±2000 fpm, faint gutter so the scale is
    visible at level flight, white zero-line reference.
  - Numeric VSI readout enlarged (`max(15, H*0.033)` bold, was
    `max(10, H*0.022)`), magenta when |vspeed| > 30 fpm so the
    readout and bar read as a set. Thousands-separated
    (`±1,250 fpm`) to stay compact at 4-digit rates.

### #18  iPhone tapes/drums too jumpy — per-field smoothing — **FIXED**
Target: `iphone_display/index.html` `smooth()` and render loop.
Fix: replaced the single-alpha lerp (`alpha = min(1, dt/60)`) with a
per-field `SMOOTH_TAU` table and `alpha = 1 - exp(-dt_ms/tau)`. Short
tau on attitude (roll/pitch 150 ms, yaw 250 ms) so motion still
tracks, long tau on the tape/drum readouts (speed 600 ms, alt 700
ms, vspeed 1200 ms, track 400 ms) to kill SSE noise and phone-sensor
jitter. Render loop now passes `dt_ms` directly (capped at 250 ms so
a long background pause doesn't force a huge single-frame jump).
Applies to both live-AHRS and phone-sensor sources since both feed
through `TARGET → smooth()`.

### #16  iPhone heading tape — show 25 % more degrees at once — **FIXED**
Target: `iphone_display/index.html` `drawHeadingTape`.
Fix: span widened from 90° to 112° via a new `HDG_SPAN_DEG` constant,
so the pilot now sees ±56° instead of ±45°. `pxPerDeg = W/HDG_SPAN_DEG`
and the tick loop bound derives from `HDG_HALF = ceil(HDG_SPAN_DEG/2)`.
Existing bug-chevron clamp (`spdX+spdW+20 … altX-20`) still holds and
the 10° label cadence reads cleanly with the bold double-size labels.

### #14  iPhone baro button shouldn't exist when in GPS-ALT — **FIXED**
Target: `iphone_display/index.html` `drawBaroButton`, `_handleSpdTap`.
Fix: added `_baroAdjustable()` helper returning `D.baro_ok !== false &&
D.baro_src !== 'gps'`. Both the rounded-rect draw and the tap branch
early-return when it's false, so the button disappears and the area
doesn't register taps in GPS-ALT mode — matches pi4 behaviour. The
column sits empty (heading tape shows through), which is the intended
visual. Also simplified `drawBaroButton` — the old magenta-when-GPS
branch is dead code now that the button never draws in that state.

### TERRAIN-FULLWIDTH  iPhone terrain mesh clipped to tape gap — **FIXED**
Target: `iphone_display/terrain.js` `render()`.
Root cause: `ctx.rect(clipX, tapeTopY, clipW, tapeH)` where `clipX =
spdX + spdW` and `clipW = altX - clipX` restricted the TAWS-coloured
terrain mesh (red = at/above aircraft, amber = within 500 ft) to
the AI gap between the tapes. The coloured horizon band stopped at
each tape's inner edge, so a pilot scanning the horizon saw the
alert only in a narrow central strip.
Fix: clip to `(0, tapeTopY, canvas.width, tapeH)` instead. The tapes
are rendered after terrain and use `rgba(0,8,25,0.80)` backgrounds,
so they tint the band instead of hiding it — the alert colour
now reads edge-to-edge along the horizon while the tape readouts
remain legible.
Closes the "extend TAWS colours to full AI area" sub-task that was
originally part of #11.

### #11  iPhone tape repositioning — outside-edge marks + safe-area — **FIXED**
Target: `iphone_display/index.html` — `computeLayout`, `drawSpdTape`,
`drawAltTape`.
Fix summary:
  - `L.tapeTopY` reduced from `safeT + 26` → `safeT` so the tapes now
    extend up to the notch / status-bar edge (dark tape background
    still sits behind the "GS KT" / "ALT FT" header label inside
    the tape area, so nothing protrudes into the notch).
  - Speed tape ticks moved to the LEFT (outside) edge: major 14 px /
    2 px stroke at every 20 kt, minor 7 px / 1 px at every 10 kt
    (minor ticks are new — pi4 parity). Labels now left-aligned
    immediately to the right of the tick, bold.
  - Altitude tape ticks moved to the RIGHT (outside) edge with the
    same major/minor treatment. Labels right-aligned immediately to
    the left of the tick, bold.
  - Added top/bottom exclusion (12 px) so ticks never collide with
    the header label row. Centre-exclusion zone retained so the
    Veeder-Root readout still hides the current-value tick.
  - TAWS full-AI colour bands deferred to a new issue (#13) — the
    existing TAWS code only draws a centre banner, so there's no
    "tape-scoped" version to extend; implementing it is net-new
    work rather than a #11 cleanup.

### IPHONE-ORIENT-LOCK  iPhone display must never rotate — **FIXED**
Target: `iphone_display/index.html` resize path and touch handlers.
Root cause: in steep bank, the accelerometer's gravity vector fools
iOS's auto-rotate into flipping the PWA to portrait mid-flight — the
PFD content then draws into a tall narrow box, with the heading tape
and tapes rotated 90°. The manifest's `"orientation": "landscape"`
hint alone isn't enough under these conditions, and
`screen.orientation.lock()` is not reliably supported on iOS Safari
or iOS PWAs.
Fix: render the canvas at landscape resolution regardless of the
viewport — when `window.innerHeight > window.innerWidth` we still set
`canvas.width = innerHeight`, `canvas.height = innerWidth`, then apply
CSS `translate(innerWidth, 0) rotate(90deg)` to the canvas element so
it visually covers the portrait viewport while the content reads as
landscape. Touch coordinates are remapped through the inverse
transform in a new `_eventToCanvas()` helper used by both the click
and touchend listeners (`vy → canvas.x`, `H - vx → canvas.y`).
`screen.orientation.lock('landscape')` is still attempted as a
best-effort belt-and-braces call.

### #10  iPhone heading readout box — refinement — **FIXED**
Target: `iphone_display/index.html` `drawHeadingTape`.
Previous implementation was a simple rounded rectangle with a
separately-drawn filled triangle below it. Replaced with a single
chamfered polygon whose outline traces the rectangle AND the triangle
pointer — matching the Pi4 `_chamfer()` path in `draw_heading_tape()`
(white/magenta stroke on black fill, pointer integrated into box
outline rather than a separate solid triangle). Triangle depth 14 px,
base width `bw/3`, corner radius 4 px. M/G subscript moved to
baseline-aligned position outboard of the ° glyph (previously was
top-aligned near the bottom of the box) and bumped to bold for
better legibility on high-DPI phone screens.

### UNIT-BUG  Bug values entered in display units not converted — **FIXED**
Numpad ENTER for `spd_bug` and `alt_bug` stored the user's raw
integer without converting from the current display unit.  Typing
"90" in mph mode stored 90 (interpreted as kt everywhere), so the
tape re-rendered it as 90×1.15 = 104 mph — looked like "the bug
didn't change to what I entered".  Also affected `sim_init_alt`.
Fix: divide entered value by the current spd/alt factor at commit;
multiply by factor for the "Current: N" placeholder and the
(×100 ft/m) / (kt/mph/kph) title suffix.  V-speeds unchanged (they're
entered in kt by design — the Flight Profile screen header reads
"V-SPEEDS (knots)"). pi4 and pi_zero both.

### BARO-ENTER  Baro numpad ENTER did nothing — **FIXED**
Root cause: two bugs compounding. `smooth_state()` copied
`state["baro_hpa"]` → `disp["baro_hpa"]` every frame; numpad ENTER
wrote only to `disp[]`, so the SSE echo from the firmware (at
~20 Hz) overwrote the pilot's entry within 50 ms.  Additionally,
the firmware had no way to know the new QNH, so even if the local
display had held the value, the AHRS-derived altitude would remain
computed against the old QNH — a silent miscalibration.
Fix:
  - Remove `baro_hpa` from `smooth_state()`'s state→disp copy list
    (it's a user-owned Kollsman-window value, not a sensor field).
  - Numpad ENTER now writes `disp["baro_hpa"]`, `state["baro_hpa"]`
    (under lock), and fires an HTTP GET to the Pico W's
    `/baro?qnh=X` endpoint in a background thread so the firmware
    recomputes altitude against the new QNH.
  - The Connectivity screen's `ahrs_url` field is used as the base
    URL so USB-connected or alternate-AP setups work.
pi4 and pi_zero both.

### #2  Speed drum leading 1 at 100 — **FIXED**
Commits: `6eab95f` (round vs int), plus this session
(`show_adjacent=True` + `adj_slot_h` on inner drum so the "1" above
the "0" is visible). pi4 and pi_zero both.

### #3  Heading bug at 360° / 000° — **FIXED**
Commits: `3d20b1e` (falsy-zero in sim flight model),
`6eab95f` (chevron rendering `!= 0` check removed).

### AIRPORTS-BANK  Airports slide across sky during banked turns — **FIXED**
Commit: `74844fd` introduced paint-in-terrain-frame-then-rotate
overlay. `f0e15a6` gated it behind GL SVT which is disabled on Pi 4
hardware, so the fix never executed. This session removes the gate —
overlay path runs on both GL and pygame render paths. pi_zero also
gets the overlay (was previously using the old independent-roll
projection).

### SPD-DRUM-SPACING  Airspeed drum gap between tens and ones digit — **FIXED**
Pi4 had `_drm_sw = int(26 * _fs)` (ones cell ~30 px) vs inner cell
~20 px. This session reduced to `_drm_sw = int(18 * _fs)` so the
ones drum matches the inner cell width. pi_zero already had matched
widths (17 vs 15).

### ONES-ROLL-ASYM  Ones drum "1 above 0" invisible approaching from below — **FIXED**
Root cause was NOT IIR smoothing (as initially assumed). It was
that `_rolling_drum`'s show_adjacent branch only rendered one digit
above (`d_hi`), while `_rolling_drum_alt20` used by the altimeter
also rendered a *second* digit two slots above (`d_hi2`). At
speed=99.8 the math gives `d_lo=9, d_hi=0, d_prev=8` — the "1"
simply wasn't computed or drawn. The altimeter equivalent at 9998 ft
gives `d_lo_idx=4("80"), d_hi=0("00"), d_hi2=1("20")` — the "20"
peeks above correctly.
Fix: added `d_hi2 = (d_lo + 2) % 10` and its blit at
`ty_lo - 2 * slot_h` in `_rolling_drum` show_adjacent branch.
Applied to pi4 and pi_zero.

### #5  Keyboard fixes (colon, period, backspace, pre-populated values) — **FIXED**
Three sub-issues diagnosed and fixed in pi4/pi_zero pfd.py:

1. **Colon and period keys missing**: `:` and `.` were not in `_KB_ROWS`
   at all. Added to row 3 (replacing the position of `-`, which moved to
   row 4). Row 3 now: `Z X C V B N M . : ⌫`, backspace width reduced
   from 88 to 60 px so 10 keys at 60 + 9 gaps fit pi_zero's 640 px
   display (636 px total). Row 4 now: `CANCEL - SPACE DONE`.
2. **Backspace on numpad missing**: `_NP_KEYS` had no `⌫` key and the
   event handler had no `del` branch. Added `⌫` as a fourth key on the
   bottom row (CANCEL, 0, ⌫, ENTER), each 87 px wide so the row still
   totals 384 px matching the digit rows. `_NP_KEYS` now supports an
   optional 3rd tuple element for per-key width; new `_np_row_layout()`
   centers rows by width. Handler gains `elif sty == 'del'` branch
   that does `numpad_buf = numpad_buf[:-1]`.
3. **Modal not pre-populated**: all four modal open-sites (numpad for
   bug targets, numpad for V-speed fields, keyboard for connectivity,
   keyboard for flight profile) set `numpad_buf = ""` / `kbd_buf = ""`.
   Added `_current_str_for_numpad(target)` and `_current_str_for_kbd
   (target, prev_mode)` helpers that return the string form of the
   current value (with inHg↔hPa conversion for baro_hpa, /100 for
   alt_bug). All open-sites now pre-populate the buffer. Latent bug
   also fixed: flight-profile keyboard path was not setting kbd_prev,
   which could leave it stale from a prior connectivity edit and cause
   text to go into the wrong dict on DONE.

### #6  WiFi SSID — show actual connected network — **FIXED**
Root cause: `_wifi_ssid_current()` already ran `iwgetid -r` and
returned the actual SSID, but `_poll_wifi_status()` discarded it —
only stored `wifi_ok = bool(...)`. The Connectivity "STATUS" row
showed a generic "WiFi CONNECTED" badge, never the network name.
Fix: `_poll_wifi_status` now also stashes the SSID into
`disp["cs"]["wifi_actual"]`; the status row renders
`"WiFi: <ssid>"` (truncated with ellipsis at 18 chars) when up,
falling back to "WiFi NO LINK" when down. Applied to pi4 and pi_zero.

### #4  USB AHRS — end-to-end working — **FIXED**
Full path Pico W → USB CDC → Pi 4 → PFD display verified live on
hardware. Commits across the work:
  - `0044215` — transport: firmware emits `$AHRS,{json}`, shared
    SerialClient reads /dev/ttyACM0, pi4 tries USB before SSE.
  - `e4a4c42` — Connectivity STATUS row diagnostics: transport,
    port, RX/ERR counters, last error.
  - `4e3a99c` — live R/P/Y/ALT readout on STATUS row.
  - `f80059b` — pause live AHRS while sim runs so sim writes win.
  - `63c2852` — wt901 driver: avoid `del bytearray` (MicroPython
    doesn't implement it). Crash surfaced only once the sensor
    was wired correctly and bytes actually started arriving.
Wiring note: WT901 pin 4 (TX) → Pico pin 2 (GP1 / UART0 RX).
WT901 pin 3 is RX; connecting pin 3 to Pico RX gives two listeners
and no talker. Easy mistake.
pi_zero does NOT have USB fallback yet — pending if needed.

### ALT-10K  Altitude drum shows "0000" at 10000 ft — **FIXED**
Root cause: IIR smoothing on `disp["alt"]` converges from below —
at indicated 10000 ft the actual smoothed value is e.g. 9999.998.
Branch selection used `int(alt_inner)` which returned 99 for any
`alt_inner < 100.0`, picking the 2-drum elif path that never draws
the leading "1" column. Only crossing integer 10000 (actually
10100 observed) promoted to the 3-drum else path.
Fix: changed branch selection to `round(alt_inner)` so the 3-drum
path activates at `alt_inner ≥ 99.5`, matching how `val_int` is
already computed inside `_rolling_drum`. Applied to pi4 and pi_zero.

---

## Conventions

- When a bug is fixed, move the entry to **Completed** with commit SHAs.
- Don't delete entries — keeps the history visible for cold-start
  sessions.
- When a new bug comes up, add it to **Open** with enough context that
  a new session can pick it up without back-reading the chat.
