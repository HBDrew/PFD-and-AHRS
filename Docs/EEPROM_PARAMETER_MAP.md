# EEPROM / Non-Volatile Parameter Map — Deep Dive (v0.1)

**Scope:** The complete persisted parameter set — addresses, meanings, units,
defaults, and which values are EEPROM-settable vs. hardwired. This is the tuning
DNA the revival must carry forward (D5), and the schema for the new box's config
store.

**Source:** `TT AP Sources/` — `EEDEFINES.h` (compile-time defaults),
`EEPROM.asm` (storage engine + `consts` layout), load/store sites in
`DFCMain.asm` / `DFC_IRQ.asm`. Citations are `File:line`.

> **⚠ Baseline moved to PV.40.** The `$00..$3F` table below is the 2011 baseline.
> The storage model (§1) is unchanged, but PV.40 renamed/moved several slots.
> **PV.40 deltas** (authoritative for the revival): `EfisType` at `$06-$07`
> (replaced `ydActivity`/`ydCentering`); `EnableSettings` at `$0C` (dual-named
> with `beepVol` — see note); `defVS` at `$2C` (replaced `mxCenter`); `RollRev`
> at `$3A`; `AEPMaxBank` at `$3B`; `dgRateGain` moved to `$3D`. Full PV.40 `consts`
> block + per-airframe defaults (RV10 / Cessna-172) are in
> [`PV40_BASELINE_ADDITIONS.md`](PV40_BASELINE_ADDITIONS.md) §3–4. Per-unit
> calibration values also changed (new sensor hardware) and do not port.

---

## 1. Storage model

- A dedicated **256-byte parameter page** in the HCS12 on-chip EEPROM is
  byte-addressable: **EE address `N` = physical `$0400 + N`** (`EEPROM.asm:172,199`,
  `ADDD #consts` where `consts` = `$0400`).
- The EEPROM programs in **4-byte sectors**: sector = `addr & $FC`, byte-in-sector
  = `addr & 3`.
- RAM working state (`EEPROM.asm:23-26`): `eeState` (b0 = dirty), `eeSector`
  (buffered sector addr), `eeBuff` (4-byte sector buffer).

### 1.1 Deferred write + recursive checksum (the v2.25 "time-dependent bug" fix)

| Routine | Behavior |
|---|---|
| `EE_INIT` (`:86`) | Set EEPROM clock divider (200 kHz), clear state. Called once at boot (`DFC_IRQ.asm:3416`). |
| `EE_LOAD` (`:168`) | Read byte at X; if dirty buffer covers it, read from `eeBuff`, else from EEPROM. `EE_LOADWORD` reads 2 bytes big-endian. |
| `EE_STORE` (`:195`) | **No-op if value unchanged** (`CMPA 0,X / BEQ`). If dirty buffer is a *different* sector, flush first; else load sector into `eeBuff`, mark dirty, patch byte. **No physical write yet.** |
| `EE_FLUSH` (`:97`) | Erase-1-sector (`$40`) + two Program-word (`$20`) ops to commit `eeBuff`. Then recompute the additive checksum over `$00..$3F`, store the correcting byte at **`$3F`**, and **recursively call itself** to flush that. |

**Deferring the erase until `EE_FLUSH`** (called at mode transitions / after setup
edits) is what eliminated the timing bug and reduced EEPROM wear. **Checksum:**
1 byte at `$3F` makes the sum of `$00..$3F` zero; verified at boot
(`DFCMain.asm:9830-9843`) → on failure sets `mode` b7, enters `EM_CHECKSUM`.

### 1.2 Version-migration flags

Bits from physical `$07EF` (EE `$3EF`) mark newly-added variables as
uninitialized per firmware version; when set, defaults are written and the bit
cleared (`EEPROM.asm:75-83`; example: 2.34 `xRoll` initializer at
`DFCMain.asm:9846-9858`). **A real, working scheme for evolving the parameter set
across firmware revisions without wiping calibration — worth preserving in spirit.**

---

## 2. Master parameter table (`$00..$3F`)

Default source: `EEPROM.asm consts:` (`:31-73`) and `EEDEFINES.h`. Active build =
`PLANENUMBER 0` / `SORCERERNUMBER 0`. Sz: B=byte, W=2-byte big-endian.

| EE | Sz | Variable | Meaning / units | Default | Notes |
|----|----|----------|-----------------|---------|-------|
| `$00` | B | latTorque | Roll servo torque (×21→DAC0) | 12 | 0..12 |
| `$01` | B | latActivity | Lateral activity → `latGain` | 3 | 0..36 |
| `$02` | B | baudRate | RS-232 baud (4⇒9600) | 4 | |
| `$03` | B | contrast | Display contrast | 7 | |
| `$04` | B | pitchTorque | Pitch servo torque (×21→DAC1) | 12 | 0..12 |
| `$05` | B | pitchActivity | Pitch activity → `vrtGain` | 3 | 0..36 |
| `$06` | B | ydActivity | Yaw-damper activity (×21→DAC5) | 5 | |
| `$07` | B | ydCentering | YD centering (−8..8→DAC6) | 0 | |
| `$08` | B | MaxBank | Bank limit lo/med/hi | 1 | 0..2 |
| `$09` | B | **config** | Config bitfield (see §4) | `$EC` | |
| `$0A`–`$0B` | W | defAS | Default climb airspeed | 125 kt | |
| `$0C` | B | beepVol | Audio volume | 16 | 0..16 |
| `$0D` | B | staticLag | Static lag / VS authority | 0 | 0..4 |
| `$0E` | B | lostMotion | Roll deadzone/backlash | 0 | 0..31 |
| `$0F` | B | pLostMotion | Pitch backlash; **b7=half-step** | 0 | b4..0 count |
| `$10`–`$11` | W | rollCenter | Roll gyro/servo center | `$8000` | **per-unit cal** |
| `$12`–`$13` | W | azCenter | Yaw center | `$8000` | **per-unit cal** |
| `$14`–`$15` | W | pitchCenter | Pitch center | `$8000` | **per-unit cal** |
| `$16`–`$17` | W | locGain | Localizer position gain | 100 | ⚠ overlaps `minBklt` byte @`$16` |
| `$18`–`$19` | W | minAS | Min safe airspeed | 0 kt | envelope |
| `$1A`–`$1B` | W | maxAS | Max airspeed | 200 kt | envelope |
| `$1C`–`$1D` | W | aspOfs | Airspeed sensor offset | `$07C3` | **per-unit cal** |
| `$1E`–`$1F` | W | aspGain | Airspeed sensor gain | `$031D` | **per-unit cal** |
| `$20`–`$21` | W | baroSet | Baro setting (×100 inHg) | 2992 | |
| `$22`–`$23` | W | altGain | Alt-hold gain: `altErr(ft)×gain→fpm` | 8 | |
| `$24`–`$25` | W | locRateGain | Localizer rate gain | 16 | |
| `$26`–`$27` | W | absOfs | Abs-pressure sensor offset | `$E728` | **per-unit cal** |
| `$28`–`$29` | W | lnGain | ln-abs-pressure gain | `$E6F5` | **per-unit cal** |
| `$2A`–`$2F` | W×3 | mag* | Magnetometer cal | — | **unused** (mag removed 2.25) |
| `$30` | B | gpssGain | GPSS lateral gain | 16 | 16..32 |
| `$31` | B | PitchRev | Pitch reversal / "NORM CLIMB" (b0) | 0 | |
| `$32`–`$33` | W | gpssRateGain | GPSS rate gain | 32 | |
| `$34`–`$35` | W | gpssMaxBank | GPSS bank limiter (~25°) | 400 | |
| `$36`–`$37` | W | xRoll | Additional roll damping | 0 | 0..15 (added 2.34) |
| `$38`–`$39` | W | gpsvGain | GPSV (vertical GPS) gain | 16 | |
| `$3A`–`$3B` | W | — | spare/reserved | 0 | |
| `$3C`–`$3D` | W | dgRateGain | Ext-DG input rate gain | 19 | |
| `$3E` | B | (roll damping) | Roll damping 0..8 | 0 | added 2.34 |
| `$3F` | B | checksum | Additive checksum of `$00..$3F` | `$4A` seed | |

**Extended (outside the `$00..$3F` page):** version-init flags at `$3EF`
(`$07EF`); **serial number** at `$3FA..$3FD` (`$07FA..`, `DFCMain.asm:1082-1110`).

> ⚠ **`$16` overlap:** the boot loader reads a *byte* at `$16` into `minBklt`
> (`DFCMain.asm:9871`) while `locGain` is declared as a *word* at `$16`
> (`EEPROM.asm:51`). They share the slot (minBklt = high byte of the locGain
> word). A real artifact of the packed layout — **untangle this in the new schema.**

---

## 3. Hardwired constants (EE slot freed, forced in RAM at boot)

The changelog progressively "hardwired" stable gains to free EEPROM space. These
are **not** field-settable in this build:

| Variable | Value | Site |
|---|---|---|
| azGain | 282 | freed at 2.24 (`DFCMain.asm:95`) |
| dgGain | 896 | `MOVW #896,dgGain` `:9921` |
| pcGain / pcGain1 / pcGain2 | 6 / 14 / 12 | `:9907 / :9906 / :9880` |
| gsGain / gsRateGain | 16 / 60 | `:9922 / :9923` |

In the hidden-setup `diagTbl`, an EE address of **`$FF` means read-only/RAM-only
(not persisted)**; a real address means edits are `EE_STORE`d. Persisted hidden
params include `xRoll $36`, `altGain $22`, `dgRateGain $3C`, `locRateGain $24`,
`locGain $16`, `gpssMaxBank $34`, `gpssRateGain $32`, `gpsvGain $38`, `baroSet $20`,
`absOfs $26`.

---

## 4. Config byte (`$09`, default `$EC = %1110_1100`)

| Bit | Mask | Meaning | Citation |
|-----|------|---------|----------|
| 0 | `$01` | Yaw damper present (Y/N) | `DFCMain.asm:3486,5038,7357` |
| 1 | `$02` | YD status (on/off), dynamic | `:5041/5076` |
| 2 | `$04` | Pitch board present | `:9989,882,916` |
| 3 | `$08` | Alt selector present | `:1136,5267` |
| 4 | `$10` | EXT DG / HSI present (Y/N) | `:7292,4817,1324` |
| 7 | `$80` | Speech / VS available (SPEECH ON/OFF) | `:5192,7237` |

At boot: `ANDA #%11110001 / ORAA #%00001100` (`:9926`) — bits 2 & 3 are
re-derived (pitch board + alt sel forced available), not trusted from EEPROM.
*(Note: the SPEECH handler comment says "bit 4" but the code operates on bit 7.)*

---

## 5. Notes for the revival

- **Two classes of persisted data — treat them very differently:**
  1. **Per-unit calibration** (`rollCenter/azCenter/pitchCenter`, `aspOfs/aspGain`,
     `absOfs/lnGain`, serial number) — unique to each physical unit's sensors.
     For a *drop-in* into an existing install, either re-run cal on the bench or
     provide a migration path. **These cannot be defaulted.**
  2. **Tuning/config** (torque, activity, gains, bank, envelope, backlash) — the
     shared "feel." Port defaults verbatim, then rescale deliberately.
- **New storage medium:** an M4-class MCU has flash but not byte-erasable EEPROM;
  use an emulated-EEPROM flash library or a small external I²C **FRAM/EEPROM**
  (cheap, unlimited writes for FRAM — a good fit for the deferred-write pattern).
  Keep a **checksum + version-migration flag** scheme like §1.1/§1.2; it's proven.
- **Keep the human-facing scales** (torque 0..12, activity, bank lo/med/hi) so the
  UI feels familiar and existing setup habits transfer.
- **Redesign the packed layout** — kill the `$16` overlap and the mag dead space;
  a clean, named, versioned struct with room to grow the "add features/interfaces"
  goal.

---

## 6. Open items

- [ ] Recover exact `PLANENUMBER 0 / SORCERERNUMBER 0` calibration constants as
      *starting* defaults only — real units need their own cal.
- [ ] Confirm `absGain` high byte (source notes "leading `$E8` assumed").
- [ ] Define the new config-store schema (struct + versioning + medium) as its own
      design note once the MCU/BOM is chosen.

---

*v0.1 — extracted and cross-checked against `TT AP Sources/`. The `$00..$3F` page
is authoritative; per-unit calibration entries are flagged as non-defaultable.*
