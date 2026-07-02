# PV.40 Baseline Additions (v0.1)

**Scope:** What the chosen baseline **PMA Vizion PV.40** (`pv.40` branch,
`9757708`, 2019-05-07) adds over the ~2011-era `TT AP Sources` snapshot the v0.1
spec set was reverse-engineered from. The core engine, control laws, stepper
stage, torque scheme, and envelope logic are unchanged (see the v0.1 deep-dives);
this document captures the *deltas*.

**Source:** `pv40/PV.40/vizion2hc12/Sources/` (branch `pv.40`). Citations `File:line`.

---

## 1. Mode-model changes

- **`VERTICAL_MASK` widened `$70 → $F0`** (vertical field is now bits **4–7**) to
  fit the new vertical mode (`Vizion2Flat/Switches.asm:66`). `LATERAL_MASK` = `$0F`
  unchanged. **Update the v0.1 map/vertical docs accordingly.**
- **New lateral modes** (`Switches.asm:47-49`): `LM_DYNON=$0B`, `LM_AEP=$0C`,
  `LM_ASPEN=$0D`.
- **New vertical mode** (`Switches.asm:63-64`): `VM_DYNON=$80`, `VM_EXT_ALT=$90`.
- **EFIS constants** (`DFCMain.asm:116-117`): `ASPEN=1`, `G5=2`.

---

## 2. New flight modes

### 2.1 `LM_AEP` — Autopilot Envelope Protection (bank-angle auto-leveler) ★

**This is D2 already built.** AEP is *not* a nav mode — it's a stand-alone
bank-angle envelope-protection / wings-leveler that runs while the pilot is
hand-flying (AP off).

- **State machine** (`AEPbits`, `DFCMain.asm:251-256`): OFF (`AEPenabled=0`),
  STBY/armed (`AEPenabled=1,AEPon=0`), ACTIVE (`AEPon=1`); annunciated
  `AEP OFF/STBY/ACTIVE` (`:5290-5296`). Toggled by the TRK/MODE switch when AP is
  off (`AEPToggle`, `:3108-3123`; wired at `:5279-5282`).
- **Auto-engage** (`:729-758`): requires **AP off** (`APOnTest`), **IAS ≥
  0.9375·minAS** (airspeed floor), and **|bank| ≥ `AEPMaxBank`**. On trip it
  seizes the roll servo (`SetRollServo`), forces `LM_AEP` + `VM_NONE`.
- **Control law** (`AEPL`, `:1133-1141`): `turnCmd = (−bankDegrees) × AEPGain`
  with `AEPGain = −30` → **`turnCmd ≈ 30·bankDegrees`** (proportional roll-to-level).
  **Bypasses the normal `MaxBank` limiter** — uses its own logic.
- **Release** (`dLM_AEP`, `:5429-5442`): when `|bank| ≤ AEPMaxBank − 5` → `LM_NONE`,
  `Disengage`. **5° release hysteresis.**
- **Param:** `AEPMaxBank` EEPROM **`$3B`**, default 40 (`EEPROM.asm`; loaded
  `:10007`); `AEPGain` init −30 (`:568`).

> **Implication for the revival:** TruTrak already implemented attitude-based
> protection (D2), keyed on a **bank-angle estimate** (`bankDegrees` /
> `bankDegreesAbs`). Adopt this design directly. **Open question:** the source of
> `bankDegrees` on PV.40 (ARINC roll-angle label 325? internal rate integration?)
> — resolve, because the modern IMU makes this estimate far better and can extend
> AEP to pitch/overspeed too.

### 2.2 `LM_DYNON` — Dynon SkyView serial steering

Follows the Dynon serial stream (`D*` vars: `DHdgBug`, `DCDIDeflection`, `DCrs`,
`DHdg`, `DGS`…, `DFCMain.asm:88`). Two behaviours (`DoDynon`, `:1145-1298`):
GPS-CDI intercept (≤45° track offset from `DCrs`) or heading-bug tracking
(IAS-scheduled filter + rate term + `DynonHeadingGAINtbl`). Armed **together with
`VM_DYNON`** via the mode key when `DynonFlag` bits set (`:4189-4194`, Dynon takes
priority over ARINC EFIS). Reverts to TRK on mode-key.

### 2.3 `LM_ASPEN` — Aspen ARINC heading-error roll steering

Follows ARINC `ARINC_HeadingError` (`doASPEN2`, `:1062-1129`): GPSS-style law
`gpssGain·(HeadingError/8) + (TAS/60)·gpssRate`, each clipped ±8192, through the
shared limiter (clamped by user `MaxBank`, not `gpssMaxBank`). **Auto-reverts to
TRK** on label loss (snapshots DG→`selTrk`, `:1051-1060`). Entered via `checkAspen`
when `EfisType==ASPEN` and the label is valid (`:4231-4236`).

### 2.4 `VM_EXT_ALT` — external altitude preselect/hold (G5)

`selAlt ← ARINC_SEL_Alt` (the EFIS altitude bug). Reuses the alt-hold law
(`doEXT_ALT`, `:1961-2029`): inside the capture window
`pitchCmd = vCalcGyro(4·altErr − iVSI)`; outside it flies `selVS` toward the bug
via `doSPEED`. `EXTVMode` = 2 (select/VS) or 4 (hold). Guarded by `IAS_CHK`;
**falls back to `VM_VS_SELECT`** on loss of `LM_DG` coupling or invalid bug
(`:8276-8278`). G5 coupling sets **`LM_DG` + `VM_EXT_ALT`** together (`checkG5`,
`:4218-4229`).

---

## 3. EFIS interface selector — `EfisType`

- **EEPROM `$06` (word)**, RAM `EfisType` (`DFCMain.asm:258`, loaded `:10074`).
  **Replaced** the old `ydActivity`/`ydCentering` slots.
- **Values:** `0` = none/default (plain GPSS + serial GPS), `1` = **Aspen**,
  `2` = **G5**. **Dynon is separate** (`DynonFlag`, priority over `EfisType`).
  No G3X/G900/GTN value on this branch.
- **Routing:** G5+`LM_DG` altitude-bug follow (`doHOLD`, `:2160-2171`); alt-select
  entry suppression (`goSEL1_ENTRY`, `:3934-3946`); GPSS-key arbitration
  (`ENT_GPSS`, `:4183-4229`); ARINC status annunciation (`:5121-5134`).
- **Set via the Diagnostic screen** (`EM_DIAG`, `S1_DIAG` button); `diagTbl` row
  for `EfisType` at EE `$06` (`:9101-9103`, `MSGEFISTYPE 'EFIS TYPE'`).

---

## 4. Parameter-map deltas (folded into `EEPROM_PARAMETER_MAP.md`)

| EE | PV.40 | vs. 2011 baseline |
|----|-------|-------------------|
| `$06-$07` | **`EfisType`** (word) | replaced `ydActivity`/`ydCentering` |
| `$0C` | **`EnableSettings`** (setup unlock; see below) | slot was `beepVol` — **dual-named**, see caveat |
| `$2A` | `magVertical` | (mag, unused) |
| `$2C-$2D` | **`defVS`** (default VS, hundreds fpm) | replaced `mxCenter` |
| `$2E` | `myCenter` | (mag) |
| `$3A` | **`RollRev`** (roll servo reversal) | new |
| `$3B` | **`AEPMaxBank`** (default 40) | new (AEP) |
| `$3D-$3E` | `dgRateGain` (default −32000) | **moved from `$3C`** |
| `$3F` | checksum (`$a4`) | seed changed |

**Servo-reversal polarity gotcha** (`DFC_IRQ.asm:2000-2004`, `:2127-2131`):
`RollRev`/`PitchRev` **bit0 = 1 → pass-through**, **bit0 = 0 → negate**
(two's-complement). Counter-intuitive; RV10/172 ship `RollRev = 1`. `PitchRev`
also flips trim-scroll direction (`p20Hz.asm:404-405`).

**`defVS`** (EE `$2C`, hundreds fpm): default climb/descent rate applied on a new
altitude-bug entry, VS-selection step floor, and altitude-capture VS clamp
(`DFCMain.asm:1977-1987, 3435-3488, 10531-10589`). Defaults 2/4/2 (DEFAULT/RV10/172).

**Setup-unlock (`EnableSettings`, EE `$0C`) — verified caveat:** the unlock gate
reads a RAM word `EnableSettings` (must == 10), but the boot loader stores EEPROM
`$0C` into **`beepVol`** (`DFCMain.asm:10087-10089`); no path demonstrably loads
`$0C` into `EnableSettings`. The slot is dual-named. **This reinforces decision
D6:** don't depend on the murky gate — *remove* it and expose setup via a menu.

---

## 5. EFIS interface handlers & ARINC-429 label table

*Pending — being extracted (Dynon serial sentence set, Aspen/G5 ARINC labels,
the validity/timeout mechanism, and how ARINC is received "without XIRQ"). This
section will be filled and the parameter map cross-checked before this doc is
marked complete.*

---

## 6. Re-baseline actions (tracking)

- [x] Baseline = PV.40 `pv.40` (see `BASELINE_COMPARISON_PV40_vs_VZ15.md`).
- [ ] Correct `VERTICAL_MASK` to `$F0` in `CONTROL_LOGIC_MAP.md` + `VERTICAL_CONTROL_LAWS.md`.
- [ ] Fold parameter deltas into `EEPROM_PARAMETER_MAP.md` (done in this pass).
- [ ] Document AEP as the D2 attitude-protection reference; resolve `bankDegrees` source.
- [ ] Complete §5 (interfaces/ARINC) and re-point remaining citations to `pv.40`.

---

*v0.1 — extracted from the `pv.40` branch. §5 pending. Numeric constants verified
against source; discrepancies (servo-rev polarity, `$0C` dual-naming) flagged.*
