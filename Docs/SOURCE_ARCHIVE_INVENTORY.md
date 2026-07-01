# Source Archive Inventory (working)

Running triage of the TruTrak source drops provided for the revival project.
Appended as drops arrive. Goal: know exactly what we have, which build is
canonical, and which to use as the revival baseline.

**Products expected (per owner):** Gemini, Sorcerer, PMA Vizion, Vizion 380 —
"everything most recent."

---

## Drops received

| # | Product | Project | VCS | Newest version | Date | Targets | Key interfaces |
|---|---------|---------|-----|----------------|------|---------|----------------|
| 1 | Vizion 380 (mixed) | `TT AP Sources` | none (loose) | ~A.43 / 2.44 era | ~2010–11 | VZ380 Flat, Round | GPS NMEA, ARINC-429, CDI |
| 2 | **Vizion 380** (experimental) | `vizion380 VZ15` | **git** | **VZ.15** | 2015-03-17 | VZ380 Flat, Round | + SkyView, Xavion |
| 3 | **PMA Vizion** (certified) | `vizion2hc12` (PV.40) | **git** | **PV.40** | 2019-05-07 | Vizion2 Flat, Round | + **Garmin G5, G3X, Aspen** |
| 4 | Sorcerer | — | — | *pending* | — | — | — |
| 5 | Gemini | — | — | *pending* | — | — | — |

**All three received drops share the same engine** (`DFCMain/DFC_IRQ/p20Hz/
AD7714/EEPROM/...`) — one codebase, target- and era-differentiated. Everything in
`CONTROL_LOGIC_MAP.md` + the four deep-dives applies across them; deltas are
target/version specific.

---

## Drop 1 — `TT AP Sources` (baseline of the existing spec set)

Loose files, no VCS. `DFCMain.asm` HISTORY ends ~`A.43 (11/02/2010)` / `2.44`.
This is the snapshot the current v0.1 docs were reverse-engineered from. Targets:
`Vizion 380 Flat` (PRODUCT_ID 1) and `Round` (49). Oldest of the three — treat as
reference, supersede with Drop 2/3 where they differ.

## Drop 2 — `vizion380 VZ15` (experimental Vizion 380, newest exp.)

- **Git**, branches: `master` = `master-2` = **VZ.15** (`d2c6be1`, "compiled for
  production", 2015-03-17); `Production` = VZ.13.
- History: VZ.08 → VZ.09 → VZ.12 → VZ.125 → VZ.13 (2014-03-31 batch) →
  **VZ.14** (2015-01-09, *"merge from skyview-pilot DY.26 … basic SkyView
  interface … Xavion basic interface"*) → **VZ.15** (2015-03-11, *"bugfix for
  aviation data not recognised on primary serial input"*).
- Full CodeWarrior project: `Vizion380.mcp`, `prm/banked_flash.prm`, built
  `.abs/.s19` images, `.map`/`.lst`.
- New vs Drop 1: `GPSMonitor2.asm`; the SkyView/Xavion serial interfaces.
- **Canonical: `master` @ VZ.15.**

## Drop 3 — `vizion2hc12` (PMA Vizion, PV.40) — **newest & most capable**

- Project folder `PV.40/vizion2hc12`. **Git**, branches: `master`, `master2`,
  **`pv.40`**, `FAILURETEST`, `withouthdgtrkswitching`.
- Timeline: **PV.30 "approved version"** (`431bb46`, 2017-06-27) → flat/round
  rework (2017-08) → G5 interface (2018-06) → **Aspen "went to OSH"** (2018-07) →
  first compile of new line (2019-03) → G5/G3X/Aspen modes working (2019-05) →
  **PV.40** (`9757708`, "PV.40 fixed eeprom save for def vs", 2019-05-07).
- Targets renamed **`Vizion2Flat` / `Vizion2Round`** (the "Vizion2"/PMA line).
  Also carries `Sorcerer_Data/` and `Vizion380_Data/` build-output folders.
- **Certified-panel EFIS interfaces** present in source: **Aspen** (18 refs),
  **G5** (9), **G3X**, SkyView. This is the "talk to anything" line, matured.
- ⚠ **Canonical = branch `pv.40`** (`9757708`) — it is `master` + 1 (the def-VS
  EEPROM-save fix). **The extracted working tree is checked out on
  `withouthdgtrkswitching`** (`a671f07`, 2019-07-18, *"not sure, strange
  behavior"*) — an experimental WIP, **not** the release. Analyze the `pv.40`
  branch, not the loose working-tree files.

---

## Lineage / timeline

```
2010–11  Drop 1  TT AP Sources        A.43 / 2.44        (spec v0.1 baseline)
   |     (SkyPilot Cessna cert branch diverges at v2.37, 2010)
2014–15  Drop 2  Vizion 380 VZ15      VZ.08 → VZ.15      + SkyView, Xavion
2017–19  Drop 3  PMA Vizion PV.40     PV.30 → PV.40      + G5, G3X, Aspen  ← newest
```

## Recommended baseline for the revival

- **Primary reference: PV.40 (`pv.40` branch).** Newest (2019), most interfaces
  (G5/G3X/Aspen/SkyView), certified pedigree — best match for "talk to anything"
  and any future cert path.
- **Cross-check: VZ15 `master`** as the experimental Vizion 380 counterpart (the
  direct experimental drop-in target).
- Keep Drop 1 only as the historical baseline the current docs cite.
- Re-validate the v0.1 spec set against PV.40 and record deltas (mode encodings,
  gains, envelope, target pin maps, the new EFIS interfaces).

---

## Open items

- [ ] Receive **Sorcerer** and **Gemini** drops; add rows + detail.
- [ ] Decide whether to **vendor these trees into the repo** (they carry their own
      `.git` + build artifacts; IP/licensing owner-confirmed) or keep them
      external and cite by version.
- [ ] Re-point the spec set's citations to **PV.40 `pv.40`** as canonical, noting
      VZ15/Drop-1 deltas.
- [ ] Document the **G5 / G3X / Aspen / SkyView / Xavion** interface handlers
      (new since Drop 1) — directly serves the interoperability goal.
- [ ] Map PV.30 (certified/"approved") vs PV.40 delta — what changed post-approval.

---

*Working document — updated as drops arrive. Canonical branches: VZ15 `master`
(VZ.15), PMA Vizion `pv.40` (PV.40).*
