# Maths channels

Channels defined by a formula and computed on the canonical model
(`src/veltrix_engine/maths/`), and the editor for them in the desktop app
(`src/veltrix_app/maths_editor.py`). Both are built and tested; the editor has
been driven by tests offscreen, not yet used on a display.

## The editor

Open it from the app with **Maths editor…** (Ctrl+M), or **New…** / **Edit…**
in the Maths channels panel.

| Part | What it does |
|---|---|
| Channel list (left) | every maths channel; red if it does not compute on the open session, with the reason on hover. New, Duplicate, Delete |
| Name, unit, category, description | the unit is optional; if given, the formula must come out in it. "An estimate" flags the channel wherever it appears |
| Formula | coloured as you type: functions, `$parameters`, numbers, and a red wavy line under a name no channel answers to. The place an error was found is marked |
| Result line | `✓ gives g · uses acc_lat, acc_long`, or `✗` with the reason |
| Preview | the result plotted over what the main charts are showing, before anything is saved, with sample count, rate, invalid samples and min / max / mean in view |
| Channels tab | the session's channels with unit, rate and kind, searchable; double-click to insert one (names with spaces go in as `[Name]`) |
| Functions tab | the function reference; double-click to insert |
| Parameters tab | the named values formulas use as `$name`; changing one recomputes every channel that uses it |
| Dependencies tab | what the channel uses, down to session channels and parameters; what uses it; its place in the computation order; and how the saved version was calculated (inputs, conversions, filter settings, formula version) |

**Save channel** (Ctrl+S) writes the definition, computes it on the open
session and adds it to the channel browser; **Plot on selected chart** puts it
on a chart. A formula has to parse to be saved, but not to compute on the
session that happens to be open — it is then listed as not calculated, with
why. Renaming or deleting a channel is refused while another is built on it.
Definitions are kept in the app's data folder and are back at the next start.

```python
from veltrix_engine import load_session, maths

s = load_session("run.csv")
channels = maths.MathSet()
channels.parameters["slip_limit"] = 8
channels.add("RearWheelSpeed", "(wheel_speed_rl + wheel_speed_rr) / 2")
channels.add("RearSlipEstimate", "(RearWheelSpeed - speed) / speed * 100", unit="%", estimate=True)
channels.add("Wheelspin", "throttle > 70 and RearSlipEstimate > $slip_limit")
results = maths.compute(s.model, channels)      # {name: Result(ok, message, channel)}
maths.on_view(s, results)                       # make them usable in lap comparison
```

## Safety

A formula is text parsed by the engine's own parser into seven node types
(number, channel, parameter, unary, binary, call, unit conversion). It is never
passed to `eval`, `exec` or Python's `ast`. There is no attribute access, no
indexing, no strings, no assignment, and only the functions listed below can
be called. A formula is limited to 2,000 characters, 400 terms and 40 levels
of nesting.

## Syntax

| Write | Meaning |
|---|---|
| `speed`, `Ground_Speed` | a channel, by its id or by the name of its source column |
| `[Brake Pres Front]` | a channel whose name has spaces |
| `[raw:Ground Speed]` | a channel in a named layer |
| `$slip_limit` | a named parameter of the channel set (how thresholds stay configurable) |
| `+ - * / ^ ( )` | arithmetic |
| `< <= > >= == !=` | comparison, giving 1 or 0 |
| `and or not` | logic |
| `if(condition, a, b)` | choice |
| `x in m/s` | convert to a unit; the dimensions must match |
| `x as g` | state the unit of a value without converting it |

`in` and `as` bind to the value just before them: write `(a / b) as g`, not
`a / b as g`.

A name is resolved in this order: a `layer:id`; the id of a calculated,
corrected or normalised channel; the normalised channel made from a source
column of that name; the same ignoring case and punctuation; a raw column. A
name that fits more than one channel is refused, not guessed.

## Functions

| Function | Notes |
|---|---|
| `abs(x)` `sqrt(x)` | square root of a negative value is invalid |
| `log(x)` `exp(x)` | need a value without a unit; `log` of zero or less is invalid |
| `sin(x)` `cos(x)` `tan(x)` | take deg or rad; a bare number is radians |
| `min(a, b, …)` `max(a, b, …)` `clip(x, low, high)` | sample by sample |
| `derivative(x)` | per second, central differences on the logged timestamps |
| `integrate(x)` | trapezoid on the logged timestamps |
| `moving_average(x, seconds)` | centred; samples without a full window are invalid |
| `rolling_min` `rolling_max` `rolling_std` `(x, seconds)` | same windowing |
| `lowpass(x, cutoff, order=2, zero_phase=true)` | Butterworth, cutoff in Hz |
| `highpass(x, cutoff, order=2, zero_phase=true)` | Butterworth |
| `peak_detect(x, min_height=, min_distance=, prominence=)` | 1 at local maxima; `min_distance` in seconds |

Filters are Butterworth only. `zero_phase=true` runs the filter forwards and
backwards (no delay, not realisable in real time); `zero_phase=false` is
causal and delays the signal. Type, order, cutoff, sample rate and phase are
written into the result's provenance. A filter refuses a channel whose sample
interval varies by more than 2%.

## Sampling

- A result sits on its input's own timestamps. Nothing is resampled to a
  fixed rate.
- Two inputs on different clocks meet on the faster one. The slower is looked
  up by its own policy — interpolated if continuous, held if discrete — never
  beyond its last sample, and the step is recorded in provenance.
- Derivatives, integrals, windows and filters work on gap-free stretches. A
  gap (a step longer than five median steps), a clock that restarts, or an
  invalid sample ends a stretch: nothing is differentiated or filtered across
  it, and an integral adds nothing across it.

## Validity

Every result is values plus a validity mask. Division by zero, a root or
logarithm outside its domain, a window without enough samples, and anything
computed from an invalid input are invalid samples — never `inf`, never a
silently filled number. The channel's quality flags say how many.

## Units

| Rule | Example |
|---|---|
| Adding, subtracting or comparing needs the same kind of quantity | `speed + oil_temp` → "Cannot add km/h and °C: they measure different things" |
| The same kind in different units is converted to the left one, and noted | `(speed in m/s) + speed` → m/s |
| A bare number takes the unit beside it | `throttle > 90`, `speed + 5` |
| Multiplying and dividing combine units | `acc_lat^2` → g², `sqrt(...)` → g, `speed / speed` → `-` |
| A derivative is per second, an integral times seconds | `derivative(steering)` → deg/s |
| An unknown unit stays unknown | scaling it is allowed; combining it with a known unit is refused until `x as km/h` states it |
| A channel's declared unit must be what the formula gives | declared `g`, formula gives m/s² → "Write (...) in g to convert, or (...) as g" |

Conversions built in include km/h ↔ m/s ↔ mph, deg ↔ rad, s ↔ ms, m/s² ↔ g,
rad/s ↔ deg/s, bar ↔ psi ↔ kPa, °C ↔ K ↔ °F. `%` is a label on a dimensionless
number: 50 % is the number 50.

## Dependencies, versions, cache

`maths.order(set)` gives the computation order; a channel that depends on
itself is reported with the loop (`A → B → C → A`). A missing input fails only
the channels that need it, each with its own message. Changing a formula or
unit raises that channel's version, which is stored in the result's
provenance. A channel is recomputed only when its formula, its parameters or
its inputs changed.

## Persistence

`MathSet.save` / `load` write the definitions as JSON; `ProjectStore.maths()`
and `save_maths()` keep one set per project (`maths.json`). Computed channels
are part of the model's calculated layer and are saved with it.

## Measured

On the reference session (951,694 samples at 200 Hz, Apple M5): the six
channels in `tests/test_reference_session.py` plus a threshold flag compute in
53 ms in total; a filtered derivative is the slowest at 24 ms; recomputing
when nothing has changed takes 5 ms.

## Not built yet

Maths channels in a report made from the command line (they appear in reports
of sessions that carry them), expressing the engine's own derived channels as
maths channels, string options (the filter type is fixed to Butterworth), and
an undo history in the editor.
