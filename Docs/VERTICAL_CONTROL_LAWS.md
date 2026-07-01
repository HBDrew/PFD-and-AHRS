# Vertical Control Laws — Deep Dive (v0.1)

**Scope:** The full vertical axis — ALT hold, VS, airspeed-hold climb/descent,
VNAV, glideslope capture, and vertical GPS steering — plus the iVSI estimator,
the `pitchAdj` trim integrator, and airspeed envelope protection. Companion to
`CONTROL_LOGIC_MAP.md` §5 (which this supersedes in detail).

**Source:** `TT AP Sources/` — `DFCMain.asm` (mode dispatch + laws),
`DFC_IRQ.asm` (pitch gyro, go-around, VGS timers), `AD7714.asm` (iVSI + pitchAdj),
`p20Hz.asm` (VNAV distance integration). Citations are `File:line`.

---

## 0. Architecture

The vertical command is computed once per **20 Hz** cycle. `mode & VERTICAL_MASK`
($70) dispatches to a per-mode law (`DFCMain.asm:1889-1987`). Output is
**`pitchCmd`** (signed, **100 = 1 °/s** nose rate).

Almost every mode funnels through one pipeline:

```
commanded VS (fpm)  ──SUBD iVSI──►  vsError  ──vCalcGyro──►  rate cmd  ──limiter──►  pitchCmd
```

Two helpers convert an error into a pitch-rate command:

| Helper | Input → output | Location |
|---|---|---|
| `vCalcGyro` | VS error (fpm) → pitch-rate cmd | `DFCMain.asm:2419-2519` |
| `aCalcGyro` | commanded airspeed (kt) → pitch-rate cmd | `DFCMain.asm:2521-2554` |

**Output limiter** (all VS-derived modes), `DFCMain.asm:1930-1943`:
clamped to **−2.0 … +2.5 °/s** (constants `−200`/`+250`).
> ⚠ **Gotcha:** the comment says "−117..+183" but the *assembled constants are
> −200/+250*. Trust the code. Carry −2.0/+2.5 °/s forward.

This is still a **rate loop** (per D1): every vertical mode ultimately commands a
pitch *rate*, and the inner pitch-gyro loop closes it. Modern sensors improve the
*estimates* feeding it (iVSI, bank comp) without changing the structure.

---

## 1. iVSI — the vertical-speed estimator (`AD7714.asm:172-299`)

The single most important vertical signal. A **complementary filter**: barometric
rate blended with a pitch-gyro-derived rate.

- **Baro rate:** pressure deltas convolved with a FIR `weights` table
  (`AD7714.asm:169-215`), then `VS(fpm) = tVSI·256 / VSGAIN`, `VSGAIN = 16960`
  (≈32 ADC counts/ft). Low-passed into iVSI with a **~3.9 Hz** cutoff (3 shifts,
  `pTC3=3`, `AD7714.asm:246-279`) — the "faster iVSI cutoff to cut alt-hold
  hunting" from the changelog.
- **Gyro term (altitude-corrected, v2.13):** pitch rate × `MAX(70, TAS)/200`
  added in, so the same pitch angle yields larger fpm at altitude
  (`AD7714.asm:287-299`). TAS in kt×8.

```
iVSI(fpm) ≈ LPF₃.₉Hz(baro rate)  +  (pitchValue − pitchAdj) · MAX(70,TAS) / 200
```

**Revival note:** a modern 6-DOF IMU + baro gives a far better complementary VS
estimate (accel/attitude aided). Drop it in *here* — the consumers (ALT/VS/VNAV)
are unchanged. This is the highest-value sensor upgrade in the whole vertical axis.

---

## 2. ALT HOLD (`VM_HOLD` → `cAltErr`, `DFCMain.asm:1896-1944`)

```
altErr   = selAlt − currAlt            ; subAlt, polarity-correct, ±32767
cmdVS    = altErr × altGain            ; altGain 0..8, fpm per foot
cmdVS    = clamp(cmdVS, −512 … +800)   ; fpm (asymmetric: gentler descent)
vsError  = cmdVS − iVSI
pitchCmd = limiter( vCalcGyro(vsError) )
```

- `altGain` user-settable 0..8 (EE `$22`).
- Note the **asymmetric VS clamp**: +800 fpm climb / −512 fpm descent.
- `ChkLeaveGS` is called first (handles decoupling from glideslope, §6).
- Altitude uses the odd encoding: top-4-bits = 1111 → below sea level
  (`subAlt`, `DFCMain.asm:3414`).

---

## 3. VS modes (`VM_VS_SELECT`, `VM_SPEED`)

`selVS` is in **hundreds of ft/min**, range −30..+30 (`DFC_IRQ.asm:220`).

- **`VM_SPEED`** (VS entered while in hold): fly `selVS×100` fpm directly
  (`selVS_x100 → selVS_x1`, `DFCMain.asm:1966-1971`).
- **`VM_VS_SELECT`** (climb/descend toward `selAlt` at selected fpm),
  `doVS_SELECT` `DFCMain.asm:2255-2312`:
  - If `|selVS/4| < |altErr|` → fly the selected VS.
  - Else (**roundout** near target alt) → fly `altErr × 4` fpm (4 fpm per foot).
  - `selVS=0` defaults to ±100 fpm. Auto-switches to ALT HOLD on capture/overshoot.

---

## 4. Airspeed-hold climb/descent (`VM_AS_SELECT` → `doAS_SELECT`, `DFCMain.asm:2314-2408`)

Climb/descend to `selAlt` while holding a **selected airspeed** via `aCalcGyro`
(`DFCMain.asm:2521-2554`):

```
asErr   = currIAS − selAS              ; kt×8; slow → negative → nose-down
asErr   = clamp(asErr, −9 … +12 kt)    ; (×16 scaling: −144 … +192)
cmd     = (asErr << asErrGain) + asDeriv
```

- Trades altitude for speed: below target speed → nose down.
- `asErrGain` is a settable power-of-2 gain (shift).
- **`asDeriv`** is an airspeed-*rate* (lead/damping) term = d(diff-pressure)/dt ×
  1/IAS (`DFC_IRQ.asm:961-1082`).
- The state logic blends AS-error and VS-error commands (takes the more
  aggressive), honoring roundout and the maxAS descent clamp (§7).

---

## 5. VNAV (`VM_VNAV` → `doVNAV`, `DFCMain.asm:2177-2253`)

Reach `selAlt` at `selDist`. Required VS:

```
reqVS(fpm) = gpsGndSpd · 1093/256 · altErr / (dist − 128)
```

- `−128` = subtract ½ mile so you **arrive early** (`DFCMain.asm:2202`).
- `selDist` is **integrated down from groundspeed** each 20 Hz cycle:
  `GS(kt)·$E900/65536` (`p20Hz.asm:1226-1247`); at zero → announce + force ALT HOLD.
- Overflow / sign mismatch → fall back to `altErr×4` fpm. Result clamped to
  **±3000 fpm**, stored to `selVS` for display, then `−iVSI → vCalcGyro`.

---

## 6. Glideslope capture (`VM_CAPTURE`)

### Arm / couple (radio GS, `DFC_IRQ.asm:1692-1739`)
Requires lateral NAV + `VM_HOLD` + LOC present. `gsTimer` is the state var:
- **Arm:** GS above +¼ scale → count up, reaching `$80` (~6.4 s).
- **Couple:** once armed, needle crossing center for **6 consecutive reads
  (300 ms)** → `SetVertMode VM_CAPTURE`.

### Follow (`doCAPTURE`, `DFCMain.asm:2119-2175`)
```
cmd = GS·gsGain/16  +  GSrate·gsRateGain/16
```
Plus an initial **−1 °/s nose-down** ramp for 3 s (`gsTimer=60`) on capture.

### Go-around / leaving GS (`ChkLeaveGS`, `DFCMain.asm:3758-3784`)
On decouple, set `gaTimer=60` (3 s). While `gaTimer≠0`, `newPitchValue` adds
**+100 (+1 °/s nose-up)** to `pitchCmd` (`DFC_IRQ.asm:912-915`). Setting any
lateral mode also drops `VM_CAPTURE → VM_HOLD` (see map §3).

---

## 7. VGPSS — vertical GPS steering (`VM_GPSS` → `vCalcGPSV`, `DFCMain.asm:2556-2642`)

Three sub-modes, chosen by which ARINC labels are valid:

| Sub-mode | Condition | Law |
|---|---|---|
| **flyAtt** | pitch cmd + attitude both present | `(gpssPitch − gpssAtt) · gpsvGain(0..63)/… ` → VS error |
| **flyVS** | commanded VS present | `gpssVScmd/2` fpm, clip ±3000 → VS pipeline |
| **flyDeviation** | VDI only | virtual glideslope (below) |

**flyDeviation** builds a virtual 3° path (`vgsTimer` state machine mirroring the
radio GS arm/couple, `DFC_IRQ.asm:1603-1686`):
```
targetVS = gpsGndSpd · (−1360/256) fpm     ; ≈ −5.3 fpm/kt ≈ 3° descent
cmd      = targetVS + gpssVDI·(−4)          ; position term
         (+ extra −1°/s pitchover for 3 s at couple)
```
> ⚠ **Gotcha:** the comment claims `+ gpssVDI·gpssVDIGain`, but the code
> **hardcodes ×(−4)** — the settable `gpssVDIGain` is *not applied* in
> `flyDeviation`. If the revival exposes that gain, wire it up for real.

`gpsvGain` (flyAtt) is settable, default 16, clamped 0..63 (EE `$38`).

---

## 8. Airspeed envelope protection

The safety net for a no-AoA design. `absMaxAS = 399 kt` hard cap on entries
(`DFCMain.asm:374`); `ChkAS` keeps min ≤ sel ≤ max ordered (`:3695-3741`).
`currIAS` is kt×8; `minAS`/`maxAS` are kt×1 (hence the `<<3`).

- **Descending too fast:** if commanded descent would push `IAS > maxAS`, switch
  to holding `maxAS` via `aCalcGyro` (pitch up to slow), `DFCMain.asm:2325-2335,
  2425-2433`.
- **Climbing too slow:** if it would push `IAS < minAS`, hold `minAS` (pitch down
  to keep speed), `DFCMain.asm:2435-2446`. `minAS<40` ignored.
- **MIN AS drops the yaw damper:** below `minAS`, `ydT_Off` disengages the damper
  (`DFCMain.asm:5854-5865`), and YD refuses to engage below `minAS`. MIN/MAX AS
  flash annunciation via `IAS_CHK`.
- **Low-airspeed inhibit:** airspeed < 50 kt is disbelieved (`airData` b0,
  `DFCMain.asm:2421, 7875`) — avoids ground false-trips.

**Revival note (D2):** this stays as-is, and is exactly where attitude protection
augments — an attitude/AoA-aware limiter can clamp `pitchCmd` upstream while the
airspeed floor/ceiling logic remains the primary net.

---

## 9. `pitchAdj` — closed-loop pitch-trim integrator (`AD7714.asm:332-576`)

A slow term **subtracted from the raw pitch gyro** before the loop uses it
(`pitchValue − pitchAdj`, `DFC_IRQ.asm:885`). Two summed corrections:

1. **VS trim integrator:** `dispVS − iVSI` error, weighted by a turbulence
   function of the az gyro `f(az)` (parabola below ~¾ °/s, linear above) × `pcGain`
   — drives achieved VS to target, removing steady pitch bias.
2. **Turn feed-forward:** computes `sin(bank)` from `turnFilter·TAS/531`, looks up
   `cos(bank)`, scales by `pcGain1/pcGain2` — **adds nose-up in turns**.

Summed and low-passed (~0.19 Hz, 4 shifts). Net: a slow, bank-and-turbulence-
compensated integral that removes steady-state pitch error so the outer loops see
a clean rate signal (the v2.16 "fix pumping in turns").

**Revival note:** with a real AHRS bank angle, the turn feed-forward (currently a
`sin/cos`-from-turn-rate reconstruction) becomes a direct, accurate
`1/cos(bank)`-style pitch-up — a clean upgrade of an existing approximation, still
feeding the same `pitchAdj`.

---

## 10. Units & scaling

| Quantity | Units | Evidence |
|---|---|---|
| `pitchCmd` | **100 = 1 °/s** nose rate | `DFCMain.asm:1932` |
| `altErr/selAlt/currAlt` | feet (1111-topnibble = below SL) | `:454, 3414` |
| `altGain` | fpm/ft, 0..8 | `:455` |
| ALT HOLD VS clamp | **+800 / −512 fpm** | `:1911,1914` |
| Output limiter | **−2.0 / +2.5 °/s** | `:1936,1939` |
| `selVS` | hundreds ft/min, −30..+30 | `DFC_IRQ.asm:220` |
| Roundout gain | **4 fpm/ft** | `:2294` |
| `iVSI` | fpm (4-byte fpm:frac) | `AD7714.asm:32` |
| `VSGAIN` | 16960 (32 ADC/ft) | `AD7714.asm:75` |
| `currIAS/currTAS` | **kt×8** | v2.16 |
| `minAS/maxAS/selAS` | **kt×1**; absMaxAS 399 | `:374,433` |
| AS-error limiter | **+12 / −9 kt** | `:2532,2535` |
| `PITCH_GAIN` | 156/256 | `DFC_IRQ.asm:111` |
| VNAV reqVS | `GS·1093/256·altErr/(dist−128)`, ±3000 fpm | `:2188-2243` |
| Virtual-GS | `GS·(−1360/256)` fpm (~3°), VDI×(−4) | `:2627-2638` |
| GS follow | `GS·gsGain/16 + GSrate·gsRateGain/16` | `:2132-2173` |
| Go-around | +1 °/s nose-up while `gaTimer≠0` | `DFC_IRQ.asm:912` |
| Timers | `gsTimer` arm ~6.4 s; couple 300 ms; `ga/vgs/gsTimer` init 3 s | `DFC_IRQ.asm:1716`, `:3780` |

---

## 11. Discrepancies to carry into the port (comment ≠ code)

1. **ALT HOLD output limiter** is **−2.0/+2.5 °/s** (code), not −117/+183 (comment).
2. **`flyDeviation`** hardcodes `gpssVDI × (−4)`; the settable **`gpssVDIGain` is
   never applied** despite the comment. Decide whether to honor the gain in the
   new design.

Both are exactly the kind of thing a "behavioral port" must reproduce from the
*code*, not the comments — and both are flagged here so they aren't silently
"fixed" into a behavior change.

---

## 12. Revival summary (vertical axis)

- **Keep the VS-rate pipeline** (cmd VS − iVSI → `vCalcGyro` → limiter → pitchCmd).
- **Upgrade iVSI** with the modern IMU+baro complementary estimate — biggest win,
  drop-in at §1.
- **Upgrade the `pitchAdj` turn feed-forward** to use real bank angle (§9) — same
  role, better source.
- **Preserve envelope protection verbatim** (§8), add attitude clamp upstream (D2).
- **Reproduce the two comment/code discrepancies** (§11) or decide deliberately.
- Watch the odd altitude encoding and the kt×8 / kt×1 mixed airspeed scaling when
  moving to float.

---

## 13. Open items

- [ ] Confirm `dispVS` source/definition used in the `pitchAdj` VS integrator.
- [ ] Map `pcGain/pcGain1/pcGain2` values (hardwired — see EEPROM map §3) into the
      turn-compensation math for exact reproduction.
- [ ] Bench-capture iVSI response vs. a reference VSI to validate the modern
      estimator against the original feel.

---

*v0.1 — extracted and cross-checked against `TT AP Sources/`. Numeric constants
verified against code; comment/code discrepancies explicitly flagged in §11.*
