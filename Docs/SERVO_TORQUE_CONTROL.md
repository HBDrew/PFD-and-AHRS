# Servo Torque / Drive-Current Control — Deep Dive (v0.1)

**Scope:** How the Vizion firmware sets stepper *torque* (coil current), and how
that is distinct from *activity* (step velocity) and *lost-motion* (backlash).
Directly informs the hardware decision: can a modern microstepping driver IC
replace the legacy drive? (Short answer: yes, and cleanly — see §6.)

**Source:** `TT AP Sources/` — `DAC.asm`, `DFCMain.asm` (`ChgTork`,
`SetRollServo`/`SetPitchServo`), `DFC_IRQ.asm` (`DoMotors`, `actTable`),
`EEDEFINES.h` / `EEPROM.asm`. Citations are `File:line`.

---

## 0. Bottom line

**Torque is NOT controlled by the phase drive.** The MC33291 SPI output is pure
full-on gray-code phase energization — it selects *which* phases are on, never
*how hard*. Torque is a **separate analog current-limit reference voltage**,
produced by an on-chip PWM channel used as a DAC, feeding an external current
regulator/chopper on the coils.

There are **three orthogonal knobs**, often confused:

| Knob | Variable | What it sets | Mechanism |
|---|---|---|---|
| **Torque** | roll/pitch torque (EE $00/$04) | Coil current magnitude — "how hard" | Analog Vref via PWM-DAC (PP0/PP1) |
| **Activity** | `latGain`/`vrtGain` (0..36) | Step velocity — "how brisk" | `actTable` scaling of commanded velocity |
| **Lost-motion** | `lostMotion`/`pLostMotion` (0..31) | Backlash takeup on reversal | Extra velocity kick, `16×sign(dir)×lm` |

---

## 1. `ChgTork` — setting torque (`DFCMain.asm:4087-4239`)

`ChgTork` is a shared parameter editor (torque, YD activity, volume, contrast,
deadzone/lostMotion, bank, backlight). The torque path takes a value **0..12**,
stores it in EEPROM, and converts it to a DAC voltage:

```asm
chgTork3: ; lat torque              (DFCMain.asm:4188)
        LDAA var2+1
        LDAB #21
        MUL                ; A x B => A:B   (0..12 × 21 = 0..252, top byte)
        TFR  B,A
        LDAB #0            ; DAC 0 (roll/lateral)
        BRA  chgTork7
chgTork4: ; pitch torque            (:4196)
        LDAA var2+1
        LDAB #21
        MUL
        TFR  B,A
        LDAB #1            ; DAC 1 (pitch)
        BRA  chgTork7

chgTork7: ; B=DAC#  A=VOLTS         (:4225)
        ... if lateral mode <= LM_CWS -> CLRA  ; servo off => 0 volts
        JSR  DACout
```

So **torque(0..12) × 21 → 0..252**, written to DAC0 (roll) or DAC1 (pitch). The
code literally labels the argument `A=VOLTS`. If the axis is disengaged (≤ CWS)
the DAC is forced to 0 V (`chgT_Zero`, `:4233`) → no torque when not flying.
Range is clamped 0..12 (`:4118`); `12×21 = 252 ≈ full-scale` ($FF).

> Note: `DFCMain.asm:4131` (near the earlier §3.6 reference) is the `pLostMotion`
> half-step-bit merge — a *lost-motion* edit, not torque.

---

## 2. How torque reaches the hardware — PWM-as-DAC current reference

`DACout` writes the 0..252 value to a **PWM duty register** (`DAC.asm:145-168`):

```asm
DACout:
        ANDB #7
        TFR  B,X
        ...
        STAA PWMDTY0,X     ; duty of PWM channel X
```

Channel map (`DAC.asm:14-15`, production unit):

```
; PWM1 = PP1 [pin 3] = DAC1 = Pitch Torque
; PWM0 = PP0 [pin 4] = DAC0 = Roll  Torque
```

These PWM channels are configured as smoothed analog DACs — period `$FF`,
E-clock, left-aligned (`DAC.asm:67-104`) — then **RC-filtered off-chip to a DC
voltage**. That voltage is the **current-limit / torque reference** for an
external stepper current regulator. Entirely separate pins from the MC33291 SPI
phase drive.

The phase driver `DoMotors` (`DFC_IRQ.asm:2193-2277`) writes **only** gray-code
phase bits over SPI — no duty field, no chopping, no timer modulating the bits:

```asm
LDAA grayTable,X     ; roll phase nibble
LDAA grayTable,X     ; pitch phase nibble
ASLA ... ORAA 1,SP+  ; byte = pitch_nibble : roll_nibble
STAB SPI0DR          ; intermediate (inrush-limit) pattern
STAA SPI0DR          ; final phase pattern
```

Each nibble is 4 phase-enable bits, each either **full-on or off**. The only
current-related trick is the ~1 µs intermediate pattern to limit inrush on 0→1
transitions (`:2254-2268`) — not regulation.

---

## 3. Per-axis, EEPROM, defaults, range

| Item | Value | Citation |
|---|---|---|
| Roll/lateral torque | EEPROM addr **$00** → DAC0 | `EEPROM.asm:32`, `DFCMain.asm:5123` |
| Pitch torque | EEPROM addr **$04** → DAC1 | `EEPROM.asm:36`, `DFCMain.asm:5138` |
| YD torque (some builds) | 3rd DAC | `DAC.asm:10` |
| Default `LATTORQUE` / `PITCHTORQUE` | **12** (max) | `EEDEFINES.h:13/15` (+variants) |
| Range | **0..12** | `ChgTork` limit `:4118` |

Torque is independently settable per axis; default is **maximum** on both.

---

## 4. Engage-time initialization (the v1.02 "pitch torque not set until AP engage" fix)

Torque DACs are (re)loaded from EEPROM at engagement by `SetRollServo` /
`SetPitchServo` (`DFCMain.asm:5122-5151`):

```asm
SetRollServo:
        LDX  #0            ; EEPROM addr of lateral torque
        JSR  EE_LOAD
        ... if mode == LM_NONE -> CLRA   ; axis off => 0 torque
        LDAB #21
        MUL
        LDAB #0            ; DAC0
        JSR  DACout
```

`SetPitchServo` mirrors this for DAC1 ("turn torque on only if axis coming up",
`:5139`). Called at `Engage` (`:5181`) and AP-level engage (`:1054`). On
`Disengage` and on entering **CWS**, the torque DACs are explicitly zeroed
(`:5094`, `:1065`; history v2.31 "CWS switch removes servo torque"). This matches
the changelog bugfixes exactly.

---

## 5. Torque vs. Activity vs. Lost-motion (do not conflate)

- **Torque** = coil-current magnitude. Analog DAC / external current limit.
- **Activity** (`latGain`/`vrtGain`, 0..36 → `actTable` 0.5×..32×,
  `DFC_IRQ.asm:1850`) = how fast the gray-code table is advanced (step velocity),
  applied in `SetRollVel`/`SetPitchVel`. Same current per step, just brisker.
- **Lost-motion** (`lostMotion`/`pLostMotion`, 0..31, `ANDB #$1F` at
  `DFC_IRQ.asm:1964`) = backlash takeup: `deltaV = 16×sign(dir)×lm` on reversals;
  bit 7 enables half-steps.

The changelog's "microactivity max 31 → AND #$1F" is the *lost-motion* field, and
"authority 8:1 for alt gain" / `servoLimit=$1FFF` are *velocity/authority*, not
current. All distinct from torque.

---

## 6. Slip-clutch / stall / current sensing — NONE

No current-sense or stall-detect ADC channels, no feedback, no overtorque
handling. Torque is **pure open-loop**: set DAC, energize phases, step. Mechanical
overload is absorbed by the **servo's physical slip-clutch**, invisible to
firmware.

---

## 7. Implication for the revival (microstepping IC)

This is the clean-modernization finding:

- The legacy scheme needs **two** subsystems: (a) MC33291 octal low-side driver
  for full-on phase energization, plus (b) an external analog current regulator
  whose setpoint is the PWM-DAC torque voltage.
- A **modern microstepping/step-stick driver IC** (e.g. a chopper driver with
  current control — TMC-class, DRV-class, etc.) **collapses both into one chip**:
  it does phase sequencing *and* per-coil current regulation (chopping) natively.
- The mapping is direct:
  - Phase sequence (gray-code + half-step) → the driver's internal sequencer
    (feed it **step/dir** generated from the position integrator, or use its
    microstep engine directly). The 1 µs inrush trick becomes unnecessary —
    the chopper controls current.
  - **Torque (0..12)** → a **digital current setpoint** (Vref pin, or SPI/UART
    current register on smart drivers). Preserve the same 0..12 UI scale and the
    engage/disengage/CWS "zero current when off" behavior.
  - Activity and lost-motion stay in firmware unchanged (they're velocity-domain,
    upstream of the driver).
- **Validation gate:** confirm the legacy TT servo coil ratings (voltage,
  current, phase count/wiring — the gray table implies a **4-phase unipolar**
  drive) match the chosen driver's output stage. Unipolar TT windings may need a
  driver that supports unipolar, or a rewire/adapter to bipolar. **This is the
  key spec to pin down before selecting the IC.**

---

## 8. Open items

- [ ] Confirm legacy TT servo winding type (unipolar 4-phase vs. bipolar) and
      coil V/I ratings — decides driver selection and whether step/dir or direct
      microstep. **Blocking for driver choice.**
- [ ] Measure the actual torque-DAC voltage→coil-current transfer of the legacy
      external regulator, so the new digital current setpoint reproduces the same
      torque at each 0..12 step.
- [ ] Decide whether to keep the 0..12 torque UI scale verbatim (recommended for
      familiarity) or expose finer resolution.

---

*v0.1 — extracted and cross-checked against `TT AP Sources/`. Torque = analog
current-limit reference (PWM-DAC), fully decoupled from the phase drive.*
