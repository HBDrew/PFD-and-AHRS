# Baseline Comparison — PMA Vizion PV.40 vs. Vizion 380 VZ.15

**Purpose:** Decide, with data, which source tree to fork as the revival baseline.
**Method:** Compared canonical branches — **VZ.15** (`master`, 2015-03-17) vs.
**PV.40** (`pv.40` branch, 2019-05-07). Same shared engine; differences are
version/target/interface.

**Verdict: fork PV.40.** It is strictly newer, adds the interface-selector
architecture and modern EFIS couplings, and shares the proven engine + envelope.
Pull audio (Talker) richness from VZ.15 if wanted; review certified-specific
limits before adopting.

---

## 1. Flight modes

| | VZ.15 | PV.40 | Delta |
|---|---|---|---|
| Lateral (`LM_*`) | NONE,CWS,TRK,DG,GPSNAV,GPSPOST,GPSS,NAV_PRE,NAV_POST,REV_PRE,REV_POST,**DYNON** | …same… + **AEP**, **ASPEN** | **PV.40 adds AEP, ASPEN** (EFIS-coupled lateral modes) |
| Vertical (`VM_*`) | NONE,HOLD,AS_SELECT,VS_SELECT,SPEED,CAPTURE,VNAV,GPSS,**DYNON** | …same… + **EXT_ALT** | **PV.40 adds EXT_ALT** (external altitude preselect from EFIS) |

The common modes are encoded identically (same `LM_NEXT`/`VM_NEXT` order) → the
existing spec's mode map holds; PV.40 simply appends new EFIS-coupled modes.

## 2. Interface / connectivity (match-count proxy)

| Feature | VZ.15 | PV.40 |
|---|---:|---:|
| Aspen | 0 | **46** |
| SkyView / Dynon | 1 / 181 | **6 / 354** |
| ARINC-429 refs | 400 | **778** |
| CAN refs | 121 | **240** |
| Glideslope | 36 | **60** |
| NAV_PRE (intercept) | 17 | **32** |
| Garmin | 14 | 18 |

PV.40 has **materially more** interface/ARINC/CAN handling across the board, and
**Aspen is PV.40-exclusive**. This is the matured "talk to anything" line.

## 3. Parameter model (`EEDEFINES.h`)

PV.40 **restructured** the parameter defaults (VZ.15 93 lines → PV.40 68), and —
most importantly — introduced an **EFIS interface selector**:

| New in PV.40 | Meaning |
|---|---|
| **`EFISTYPE` (EE $06)** | Selects which EFIS the AP couples to (0=default, 2=RV10, …). Formalizes multi-EFIS support — the crux of interoperability. |
| `DEFVS` ($2C) | Default vertical speed setting |
| `ROLLREV` ($3A) | Roll servo reversal |
| `ENABLESETTINGS` ($0C) | Settings-enable gate |
| `PLANENUMBER 2 = 172` | **Cessna 172** target — the certified airframe |

Note `EFISTYPE` reuses the old `YDACTIVITY` slot ($06); torque/YD defaults moved
out of `EEDEFINES`. Calibration constants also changed (`ASPOFFSET/ASPGAIN/
ABSOFFSET/ABSGAIN`) → **new sensor hardware**; per-unit cal does not port across.

## 4. Envelope & core control (unchanged)

- `absMaxAS = 399 kt` in **both**.
- `servoLimit = $1FFF` (VZ.15 explicit; PV.40 retains the servo/velocity model).
- The rate inner loop, stepper output stage, torque scheme, and vertical laws
  documented in the v0.1 spec set are **common to both** — no re-derivation needed,
  only re-citation.

## 5. Where VZ.15 leads

- **Audio:** `Talker.asm` is 445 lines (VZ.15) vs 191 (PV.40) — PV.40 slimmed the
  voice annunciation set. If the revival wants richer audio, VZ.15 is the better
  source for that subsystem.
- Minor naming: `G3X`/`G900` string refs appear in VZ.15; in PV.40 EFIS coupling
  is routed through `EFISTYPE`/mode handlers rather than named strings (verify the
  G3X path is present under the new structure).

## 6. File-size deltas (context)

| File | VZ.15 | PV.40 |
|---|---:|---:|
| DFCMain.asm | 11007 | **11229** |
| DFC_IRQ.asm | 3759 | 3628 |
| Display.asm | 1892 | **2019** |
| Talker.asm | **445** | 191 |
| EEPROM.asm | 237 | 247 |

---

## 7. Recommendation

**Fork PV.40 (`pv.40` branch) as the revival baseline.** Rationale:

1. **Newest** (2019 vs 2015) — 4 more years of fixes.
2. **Interface architecture** — the `EFISTYPE` selector + AEP/ASPEN/EXT_ALT modes +
   more ARINC/CAN/Aspen/SkyView are exactly the "talk to anything / add interfaces"
   goal, already designed in.
3. **Certified pedigree** (PV.30 "approved", Cessna 172 target) — valuable if a
   cert path is ever pursued.
4. **Shared engine + envelope** — nothing in the v0.1 spec is invalidated.

**Adopt with these actions:**
- **Re-point the v0.1 spec citations to PV.40 `pv.40`**; document the new
  modes/params (`EFISTYPE`, AEP, ASPEN, EXT_ALT) as additions.
- **Review certified-specific limits/annunciations** for whether they suit an
  experimental-first product (the one real risk of a certified baseline).
- **Pull audio from VZ.15** if richer annunciation is wanted (Talker delta).
- **Do not port per-unit calibration** across — different sensor hardware.
- Keep **VZ.15 as the experimental cross-reference** for feel/behavior deltas.

## 8. Open items

- [ ] Decode `AEP` and `EFISTYPE` value map fully (which number = which EFIS).
- [ ] Confirm the G3X coupling path exists under PV.40's `EFISTYPE` structure.
- [ ] Diff PV.30 (approved) → PV.40 to see post-approval changes.
- [ ] Fold Sorcerer/gen2 (V3.66) into the picture where its logic differs.

---

*Comparison of canonical branches VZ.15 `master` and PV.40 `pv.40`. Counts are
grep-based proxies for relative coverage, not exact feature inventories.*
