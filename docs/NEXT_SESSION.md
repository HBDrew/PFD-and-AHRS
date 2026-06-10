# Next-session backlog

Logged at the end of the radio/ADS-B session (branch
`claude/nooelec-radio-equipment-0fuq7s`, merged to main). Context for picking
these up cold.

## ★ BIG: Manuals are stale — full update needed (own session/branch)

The `Docs/` manuals predate this whole session's work.  **The two pilot
manuals have ZERO coverage** of: the internet + FIS-B weather suite
(METAR/TAF/AIRMET/SIGMET/NEXRAD/winds/NOTAM), the WX source toggle
(RADIO/AUTO/INET) + provenance tags + data-age stamps, the full-screen MFD
weather pages (OVLY cycle ASP→TFC→MET→WND→NEXRAD), the WND page (altitude +
forecast-time buttons), the traffic collision alert (audio "Traffic, Traffic"
+ banner), alert-only traffic on wx pages, and the in-app NOTAM key entry.
`ADSB_IN.md` still lists winds/AIRMET/SIGMET/NOTAM + the traffic alert as
*planned* though they're done.  Decision: **do EVERYTHING, in a new session**
(likely its own docs branch).  Audited plan, per file:

**`Docs/USER_MANUAL_PI4.md`** (pilot-facing, the big one) — add new sections
near the moving-map material (§16D):
- Full-screen MFD: 3-finger swap, pan/recenter/zoom, bottom data strip,
  D2/FPL buttons, the OVLY overlay cycle.
- Weather overview & sources: RADIO/AUTO/INET toggle, `[FIS-B]`/`[INET]`
  tags, data-age stamps, radio-primary/internet-bonus model.
- Weather products: MET page (category dots, tap-for-readout, the
  METAR/TAF/AIRMET/SIGMET/NOTAM picker), graphical AIRMET/SIGMET, NEXRAD,
  nearest-station listing, scrollable readouts.
- Winds aloft (WND): barb grid, ALT + forecast-time (NOW/+Nh) buttons,
  data-age stamp, offline-forecast persistence (12 h expiry).
- Traffic (ADS-B/FIS-B IN): diamonds + threat colours, TFC page + detail
  card, declutter filters, alert-only on weather pages.
Edit: §12 Connectivity (the two NOTAM CLIENT ID/SECRET fields + how to get
the free FAA key at api.faa.gov); §19 Audio Alerts (the "Traffic, Traffic"
callout + collision banner, PFD badge-strip vs MFD placement); §1/Contents
renumber.

**`Docs/USER_MANUAL_ZERO.md`** — same additions, piZ-scaled (expand the
existing "MAP LAYERS" stub), NOTAM fields in §12, note piZ traffic alerting
is visual-only (no audio module).  FIS-B 978 reception applies here too.

**`Docs/ADSB_IN.md`** — move winds/AIRMET-SIGMET/NOTAM/traffic-alert from
"planned" → "implemented"; add the internet sources (AWC TAF/airsigmet,
Open-Meteo winds, FAA NOTAM) alongside the FIS-B/radio path; document the
dump978 bridge + install scripts, the WX source toggle, the new pollers.

**`Docs/REQUIREMENTS_DISPLAY_PI4.md` / `_ZERO.md`** — new requirements for the
weather suite, traffic collision alert, in-app NOTAM entry.

**`Docs/TEST_PROCEDURE_PI4.md` / `_ZERO.md`** — test steps per new feature
(weather/traffic paths exercised by `tools/fisb_sim.py`).

**`README.md`** — refresh the feature list.  **`Docs/BUGS_AND_TODO.md`** —
fold in this session's fixes (pan/AGL judder, render-perf, border cache) +
the 3D-traffic and winds-cross-section TODOs.

## FIS-B WX UI — remaining TODO (branch `claude/open-items-followup-93awex`)

Done this session: METAR, TAF (decoded/labeled + nearest + direction),
AIRMET/SIGMET/NOTAM text (nearest-first + on-route + valid times), graphical
AIRMET/SIGMET hazard areas on MET (tap-for-bulletin), winds aloft (table +
graphical barbs on the WND overlay with an altitude selector), and a TFC-page
aircraft detail card.  All driven/tested by `tools/fisb_sim.py`.

### Internet backfill for the non-METAR products (done)

METARs already merged radio + internet; now **TAF and AIRMET/SIGMET do too**.
Two view-driven AWC pollers (`wx.AwcPoller`, free, no key) feed parsed products
into the *same* `FisbWeather` store the readouts read, so internet data surfaces
through the existing TAF/advisory/graphics paths with no read-path change:
- `wx.fetch_tafs` → AWC `/api/data/taf` (bbox) → `store.add_tafs` (radio wins per
  station; the TAF readout title now tags `[FIS-B]` vs `[INET]`).
- `wx.fetch_airsigmets` → AWC `/api/data/airsigmet` (CONUS) →
  `store.add_airsigmets`, which folds each bulletin into both the text
  advisories *and*, when it carries a ring, the MET-page graphical overlay
  (hazard codes normalised to our legend). Dedupes by content across sources.
Folding is throttled to each poller's `updated_s` and skipped in RADIO-only
mode. Caveat: AWC field names (`rawTAF`, `airSigmetType`, `hazard`, `coords`)
are coded from the documented schema — want a quick check against a live pull.

**WINDS aloft (internet) — DONE via Open-Meteo (no key):** rather than GRIB,
the GFS pressure-level winds come as JSON from Open-Meteo
(`api.open-meteo.com/v1/forecast`, free, no key). `wx.fetch_winds_grid` queries
a coarse grid across the view in one batched request, `parse_open_meteo_winds`
interpolates the pressure levels onto our standard FD altitudes (u/v for
direction, geopotential height for the vertical), and `store.add_winds`
folds them in carrying their own lat/lon (the barb/readout geolocate by those
via `_winds_pos`, not the airport DB). A `WindsPoller` (15 min) feeds it like
the other internet sources; readout tags `[INET]` vs `[FIS-B]`. Field names are
coded to Open-Meteo's documented schema — wants a live-pull check on-device.

### ★ NOTAM access — blocked on FAA approval (key requested by email)

The NOTAM *code* is done and dormant (in-app credential entry + poller no-op
without a key).  The blocker is purely getting a key:

- **FAA NOTAM API is mid-migration** (legacy `api.faa.gov` → new **NMS**, full
  transition targeted 2026).  There is **no self-service signup** right now —
  access is by **emailed request + FAA approval**.  Andrew has **submitted an
  email request** (the only current path); awaiting approval.
- Registration paths once live: FNS NDS registration
  (`notams.aim.faa.gov/fnsAdmin/?page=nds_registration`) and the NMS portal
  (`nms.aim.faa.gov`).  Still OAuth client_credentials (client_id/secret), so
  the in-app Connectivity field fits.
- **Caveat:** our client targets the *legacy* `external-api.faa.gov/notamapi/v1`
  endpoint.  Once a key arrives, **live-test it** — the NMS migration may have
  moved the endpoint / changed field names (GeoJSON/AIXM).  Repoint
  `wx.fetch_notams` if needed.  *No app changes otherwise — paste the key into
  Connectivity and NOTAMs light up.*
- **Optional alternative — Notamify** (paid): instant Bearer-token signup, and
  it returns **plain-English decoded** NOTAMs.  Endpoint `api.notamify.com/api/
  v2/notams?locations=ICAO` (≤5/call), `Authorization: Bearer`.  Credit-based
  (50 free to start, ~250 for testing, then paid).  If wanted, add as a *second
  selectable source* with a **narrow, on-demand, destination-scoped** query
  (D2 + a couple nearby fields, slow refresh) so credits barely move — NOT the
  continuous map-following poll FAA uses.  Needs a Bearer field + new fetch/
  parse for the decoded JSON (`icao_message`, `interpretation.affected_elements`).

**NOTAM (internet) — DONE (needs a free FAA key):** `wx.fetch_notams` queries
the FAA NOTAM API (`external-api.faa.gov/notamapi/v1/notams`) by lat/lon/radius,
`parse_notams` takes the human-readable `formattedText` (location-prefixed so
`_notam_locate` can geolocate it), and `store.add_notams` folds them into the
existing NOTAM advisory path (picker → ranked list). The `NotamPoller` only
starts when credentials are present, and `fetch_notams` no-ops without them, so
nothing is affected if no key is set.

To enable, register a free app at https://api.faa.gov (NOTAM API) and set the
two env vars on the service:

```
sudo systemctl edit pfd.service
# [Service]
# Environment="FAA_NOTAM_CLIENT_ID=xxxx"
# Environment="FAA_NOTAM_CLIENT_SECRET=yyyy"
sudo systemctl restart pfd.service
```

Field names follow the FAA NOTAM API v1 geoJson schema — wants a live-pull check
once a key is in place.

Still open:
- **NEXRAD radar (the big one):** decode the FIS-B regional/CONUS run-length
  block raster into the existing NEXRAD render path; drive with synthetic
  rasters from the sim.  Highest impact, hardest decode, wants real-frame
  validation.
- **Winds vertical profile / route cross-section (FMS-type, Option B):** a
  chart of altitude vs. position along the active route (wind/temp per level).
  Niche; needs a solid route.  Deferred — do after NEXRAD.
- **3D traffic:** render ADS-B traffic in the SVT/synthetic-vision view (the
  PFD 3D backdrop), not just the 2D map — targets placed in the 3D scene at
  their relative bearing/range/altitude, threat-coloured.  Pairs with the new
  traffic collision-alert (audio "Traffic, Traffic" + badge-strip banner).
- **Real-frame validation** of every binary decode written blind: APDU
  timestamps, the 8-byte uplink/station header, graphical geometry overlays,
  and the winds bulletin envelope (the per-code FD decode is standard; the
  `WINDS <id> <alt> <code>…` framing is a sim stand-in).

## Progress (branch `claude/open-items-followup-93awex`)

- **Map cleanup items 1–3: done.** Airport dots are now neutral white on both
  renderers; the big MFD draws METAR dots on every page (and the MET overlay
  now *hides* airport dots so it reads as a clean weather-only picture — guard
  against clutter); the doubled D2 / airport-loop label is de-duplicated.
- **FIS-B decoder Stage 1: done in `shared/fisb.py`** (+ `shared/test_fisb.py`,
  78 checks). DLAC 6-bit text decode, the information-frame walk, the APDU
  header (product id + T-opt timestamp), raw-METAR parsing → the same station
  dicts `wx.parse_metars` emits (tagged `src="RDR"`). Framing + DLAC are
  round-trip-tested; **APDU timestamps still need a sanity check against live
  978 frames.**
- **FIS-B store + app wiring: done.** `fisb.FisbWeather` (thread-safe, fed off
  the GDL90 stream by `ADSBClient`, geolocation deferred) + `merge_metar_sources`
  (RDR wins, INET backfills). `_update_weather` in both apps merges radio over
  internet (throttled ~3 s geolocation; merge skipped entirely when no radio WX,
  so the internet path is unchanged). MFD WX status shows the R/I split.

### FIS-B — what's left
1. **Hardware/reception** (the prereq below): install dump978 on `--device
   978`, get its uplink frames to the app, confirm `uplink_count` climbs. This
   is now the *only* thing between us and radio weather on screen — the whole
   software path is in and tested with synthetic frames.
2. **Validate against live frames:** confirm METARs actually populate
   (`weather.n_rdr` > 0, "WX R… I…" on the status line) and sanity-check the
   APDU timestamp decode (the one part not pinned to the wire).
3. **Stages 2–3:** winds aloft / AIRMET-SIGMET / NOTAM text (extend
   `FisbWeather`/decoder for non-METAR text products), then FIS-B NEXRAD
   (the block-based run-length raster) feeding the existing NEXRAD render path.

## Big item: FIS-B weather over 978 UAT

Goal: weather over the radio (no internet needed) so LTE/Starlink becomes a
bonus, mirroring the **radio-primary / internet-bonus** model we already built
for traffic (with source attribution: RDR vs INET counts in the status line).

State of play:
- The **978 dongle is free** (serial `978`); readsb runs on the `1090` dongle.
- `shared/gdl90.py` already decodes the GDL90 **uplink** frame (`kind="uplink"`)
  and the app counts `uplink_count` — but the FIS-B APDU **payload is not
  parsed** yet.

Prereq (hardware / on the Pi):
- Install **dump978** on `--device 978` (wiedehopf's dump978 install), and get
  its UAT **uplink** frames to the display — either via a GDL90 978 path
  (Stratux-style, message id `0x07` over UDP 4000) or by reading dump978's raw
  output directly.
- Confirm reception first: `uplink_count` climbing. **Caveat:** FIS-B is
  line-of-sight from ground transmitters and is sparse on the ground / at low
  altitude — may see little at the house until airborne near a station. This is
  exactly why internet stays as backfill.

Decoder work (`shared/fisb.py`, staged for early wins):
1. **METAR/TAF text first** (easy, high value) — decode to the same METAR dicts
   the picker + overlay already consume. Instant payoff.
2. Winds aloft + AIRMET/SIGMET/NOTAM text.
3. **FIS-B NEXRAD** last (the hard one — custom block-based run-length raster,
   regional + CONUS, not a PNG) — feeds the same NEXRAD render path.

Then extend the source-attribution UI: weather should show RDR (FIS-B) vs INET
source, same as traffic. AUTO = FIS-B preferred, internet backfills gaps.

## Map cleanup items

1. **Airport dot color.** Public airport dots are currently green (`_APT_PUB`),
   which now reads as a VFR weather station since METAR dots are flight-category
   colored (green=VFR / blue=MVFR / red=IFR / magenta=LIFR). Make airport dots
   **white** (or another neutral) so airports vs WX stations are unambiguous.
   Files: `_APT_PUB` in `pi4/moving_map.py` and `pi_zero/moving_map.py`.

2. **Always-on METAR dots on pi4.** We now poll METARs continuously (for the
   airport-tap picker). Consider drawing the METAR dots on the pi4 MFD on
   *all* pages, not only when the MET overlay is selected — pi4 has the screen
   + horsepower. (Keep piZ gated to the overlay.) Touches the METAR draw gate
   in `pi4/moving_map._draw_metars` and/or how `metars=` is passed in
   `pi4/pfd.draw_mfd`.

3. **Doubled destination label.** When an airport is the active Direct-To, the
   magenta D2 ident label is drawn **on top of** the white airport-loop label
   for the same field — overlapping/doubled text. Pick one: when an airport's
   ident == the active D2 ident, **skip the airport-loop label** for it (let the
   magenta D2 label be the only one). Both renderers: airport label loop vs the
   D2 diamond label section in `moving_map.py`.
