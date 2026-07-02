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

## 5. EFIS interface handlers & ARINC-429

Two physical transports feed the (unchanged) control laws:

- **ARINC-429 receiver** — a HI-828x-class chip read over Port T; **data-ready is a
  Port H key-wakeup edge on PH7** (`ARINC_RDY=%10000000`), CS on PH5, decoded in
  `DFC_IRQ.asm` `arinc_int` (vector `0xFFCC`, `prm/banked_flash.prm:47`). The
  commit *"arinc interrupt working without xirq"* (`7f16550`) merged `arinc_int`,
  `xirq_isr`, `irq_isr` onto one handler (`DFC_IRQ.asm:2553-2556`) so reception
  works regardless of which line the module is strapped to.
- **RS-232 serial** — SCI0 (`GPSMonitor.asm`: NMEA `$GPRMC/$GPRMB` + ARNAV; also
  owns the ARINC RAM block) and SCI1 (`GPSMonitor2.asm`: Dynon SkyView). Both
  **9600 baud** (`RS232.asm:72,84`). `GPSMonitor125/225.asm` are unbuilt legacy
  variants (GRT/Garmin VNAV).

### 5.1 ARINC-429 label table (`DFC_IRQ.asm:2589-2657`)

Compares are against the **bit-reversed** label byte (ARINC is LSB-first); each
record is `[DATA:2][STATE:1]`.

| Oct label | Meaning | RAM var | Consumed by |
|-----------|---------|---------|-------------|
| 121 | Roll steering (GPSS bank cmd) | `gpssRoll` | LM_GPSS |
| 325 | Roll angle (measured bank) | `ARINC_RollAngle` | GPSS bank-error turn |
| 122 | Pitch steering | `gpssPitch` | VM_GPSS |
| 324 | Pitch attitude (measured) | `gpssAtt` | VM_GPSS attitude error |
| 104 | Commanded VS | `gpssVScmd` | VM_GPSS VS |
| 117 | Vertical deviation / GS | `gpssVDI` | VM_GPSS virtual-GS |
| 235 | Baro set | `ARINC_BARO` | baro correction |
| 102 | Selected altitude | `ARINC_SEL_Alt` | **VM_EXT_ALT (G5)** |
| 204 | Current altitude @ baroset | `ARINC_Baro_Alt` | altitude source |
| 320 | Magnetic heading | `ARINC_magHeading` | **LM_DG (G5)** |
| 101 | Selected heading (bug) | `ARINC_magSelected` | **LM_DG (G5)** |
| 100 | Selected track ⚠ | `ARINC_VORBeringSelected` | course select |
| 105 | **Aspen heading error** | `ARINC_HeadingError` | **LM_ASPEN** |
| 377 | Equipment ID (`$25`→auto GPSS) | `ARINC_EquipmentID` | auto-mode |
| 310/311 | Lat/Long (presence beacon) | *(sets `gpssState`)* | "GPSS available" |

⚠ Label 100 has a `;-- changed for testing` note (`DFC_IRQ.asm:2632`) — verify the
selected-track decode isn't sharing a test value if that label matters.

### 5.2 Validity / timeout (source-loss → graceful revert)

Each received message stamps `$F1` into the STATE byte (`aSave`,
`DFC_IRQ.asm:2658`): top 6 bits = countdown, bit 0 = valid. The 20 Hz
`chkArincTimeout` (`p20Hz.asm:1311-1328`) decrements by 4/tick → **~3 s timeout**,
then clears the valid bit. Consumers test `BRSET <var>+1,1,…` and **revert to
TRK/HOLD** on loss. Dynon has per-field flags + a stream-level `DynonTimeout` that
clears `DynonFlag` (revoking LM_DYNON/VM_DYNON).

### 5.3 Per-interface summary

| EFIS | Select | Transport / data | Modes driven |
|------|--------|------------------|--------------|
| **Dynon SkyView** | `DynonFlag` (auto, priority) | SCI1 serial `!1`/`!2` records (`DHdgBug`, `DCDIDeflection`, `DCrs`, `DGS`, `DAltBug`, `DVSBug`) | LM_DYNON + VM_DYNON |
| **Aspen** | `EfisType=1` | ARINC 105 heading-error | LM_ASPEN |
| **Garmin G5** | `EfisType=2` | ARINC 320/101 (ext DG + bug), 102 (sel alt) | LM_DG + VM_EXT_ALT |
| **Generic / G3X** | `EfisType=0` | ARINC 121 roll-steer (+325 feedback), EqID `$25` auto-arms | LM_GPSS + VM_GPSS |

Dynon takes priority over any ARINC EFIS (`DFCMain.asm:4189`); otherwise
`ENT_GPSS` arbitrates `EfisType` then GPSS validity (`:4183-4259`).

---

## 5A. Audio / alerting — external Mallory alerter (PV.40)

**A cert-driven simplification, confirmed in code and by the owner.** The PMA
build drives audio as a **single discrete on/off line to an external Mallory
alerter** — `SayMsg` just toggles `AO_PORT,AO_MASK` (`Talker.asm:159-174`), no
voice chip, no SPI. The experimental VZ.15 instead drives an **ISD / ML22Q54 voice
chip over SPI** (26 SPI refs, `Talker.asm` 445 lines vs PV.40's 191).

Rationale (owner): using a self-contained Mallory alerter avoided **interfacing to
the aircraft audio panel**, which reduced certification scrutiny for the PMA unit.

**Revival implication:** for a low-cost box, the Mallory-style discrete alerter is
the simplest, cert-friendliest path; richer voice annunciation (lift from VZ.15's
Talker) is an optional feature, not a baseline requirement. Keep audio behind a
thin abstraction so either back-end drops in.

---

## 6. Re-baseline actions (tracking)

- [x] Baseline = PV.40 `pv.40` (see `BASELINE_COMPARISON_PV40_vs_VZ15.md`).
- [x] `VERTICAL_MASK $F0` noted in `CONTROL_LOGIC_MAP.md` (§2 callout).
- [x] Parameter deltas noted in `EEPROM_PARAMETER_MAP.md` (§3–4 here authoritative).
- [x] AEP documented as the D2 attitude-protection reference (§2.1).
- [x] §5 complete — EFIS handlers, ARINC-429 label table, timeout/validity.
- [x] §5A audio/Mallory alerter documented (cert-driven).
- [ ] Resolve `bankDegrees` source for AEP (ARINC 325 roll-angle vs internal).
- [ ] Verify ARINC label-100 "changed for testing" note (`DFC_IRQ.asm:2632`).
- [ ] Audio back-end abstraction: modern voice (PCM in flash → I²S/DAC) OR discrete
      Mallory-style alerter behind one interface.

---

*v0.1 — extracted from the `pv.40` branch. All sections complete. Numeric
constants verified against source; discrepancies (servo-rev polarity, `$0C`
dual-naming, ARINC label-100 test note) flagged.*
