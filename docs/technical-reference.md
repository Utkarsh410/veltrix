# Technical reference

Where each part of the engine is specified. Experimental parts are marked.

| Topic | Document or module |
|---|---|
| Session model, layers, labels, provenance, storage | [data-model.md](data-model.md) |
| Sampling policy (native timestamps, alignment, gaps) | [data-model.md](data-model.md#sampling-policy) |
| Channel schema, units and sign conventions | [CHANNEL_SCHEMA.md](CHANNEL_SCHEMA.md) |
| Maths expression syntax, functions, filters, units | [maths-channels.md](maths-channels.md) |
| Mapping decisions and profiles | below, and `mapping_profiles.py` |
| MDF compatibility | [mdf-compatibility.md](mdf-compatibility.md) |
| Events | below, and `events.py` |
| Video sync — experimental | below, and `veltrix_app/video_sync.py` |
| UI framework | [ui-framework-decision.md](ui-framework-decision.md) |
| Performance | [performance.md](performance.md) |
| Packaging | [packaging.md](packaging.md) |
| Testing | [testing-strategy.md](testing-strategy.md) |
| Status and what is next | [implementation-roadmap.md](implementation-roadmap.md) |

## Units

Two layers handle units, on purpose:

- **Import** (`session.py`, `schema.UNIT_FACTORS`): a source column is brought
  to its schema unit. Where the unit came from is recorded per channel
  (`file`, `stated`, `plausibility`, `assumed`); the raw layer keeps `unknown`
  when the file gave none.
- **Maths** (`maths/units.py`): units are scale + dimensions; addition and
  comparison need the same dimensions; unknown stays unknown.

## Mapping

`mapping_profiles.decide(raw, profile, user)` returns one `Decision` per source
column: `channel`, `basis`, `confidence`, `confirmed`, `unit`, `unit_source`,
`problems`, `reason`. Order: what the format itself identifies → a confirmed
profile rule whose values fit → a name match whose values fit (a suggestion,
never confirmed) → nothing. An explicit user choice overrides all. Value
ranges are in `PLAUSIBLE` and are deliberately wide. A profile is JSON:

```json
{"version": 1, "name": "Team A GT3", "logger": "ADL3", "car": "GT3", "series": "", "team": "",
 "rules": [{"pattern": "vCar", "channel": "speed", "unit": "m/s", "valid_min": null, "valid_max": null,
            "confidence": 0.9, "confirmed": true}]}
```

`pattern` matches the source column name, case-insensitively, with `*` and `?`.
`ProjectStore.import_options(id)` records the stated units, sign flips,
channel selection and profile name a session was imported with, and
`reload_session` uses them.

## Events

`events.detect(model, rules)` returns `(events, report)`.

A `Rule`:

| Field | Meaning |
|---|---|
| `on` | maths formula; the event starts where it becomes true |
| `off` | optional maths formula; the event ends where it becomes true (hysteresis). Without it the event ends when `on` stops holding |
| `min_duration_s`, `merge_gap_s` | shorter events are dropped; events closer than the gap are joined |
| `severity`, `moderate`, `severe` | a formula whose peak in the event grades it |
| `evidence` | channels whose min / max / mean during the event are recorded |
| `fine_inputs` | channels that must not be recorded in coarse steps; otherwise the rule is not run and says why |
| `confidence`, `note` | how far to trust it, and what else gives the same pattern |
| `version`, `enabled` | stored on each event; a rule can be switched off |

A `RuleSet` also has `parameters` (every threshold: `$spin_on`, …) and
`helpers` (maths channels the rules build on, marked as estimates). An event
never runs through invalid samples or a gap in the timestamps. A rule whose
inputs are missing is reported as "Not looked for: …" and the others run.
Default rules (`events.default_rules()`): wheelspin, front lock-up, traction
loss, oversteer, throttle held while braking, steering correction, speed
sensor fault. **Their thresholds are illustrative.** On the reference session
the defaults give 2,354 candidates, 1,199 of them steering corrections — a
sign the thresholds need tuning to the car, not a finding.

`events.calibrate(model, rules)` fits thresholds to a session: a rule's
`calibrate` entry names the parameter, the signal it is compared with, and
either a `quiet` condition (threshold raised to that signal's median plus
`sigmas` robust standard deviations while nothing should be happening) or a
`percentile` over a `where` condition. A threshold is only ever raised.
`detect(..., fit=True)` does this first and reports the changes under
`report["_calibration"]`.

The older balance-incident detector (`analysis/handling.py`, steering against
the session's own norm) still runs beside the rules; its results become events
through `model.events_from_incidents`.

## Video sync — experimental

`telemetry_time = rate · video_time + offset`. Manual offset: `rate = 1`. With
anchors: offset only, unless at least three anchors spanning a minute or more
miss a pure offset by more than the tolerance and a rate term removes at least
half of that. With `piecewise` set and three or more anchors, the sync runs
straight from anchor to anchor (one for one beyond the first and last); its
uncertainty is the leave-one-out error, since it is exact at the anchors by
construction. `VideoLink.status()` reports the model used, the RMS residual as
the sync uncertainty, and which anchors disagree. A manual offset or one
anchor is never "validated".

## Known limitations

Collected in the [README](../README.md#known-limits). The ones that matter most:

- Only CSV and one AiM file are real data. Every other format, MDF included,
  has been read only from files the tests or a library wrote.
- The desktop app has been driven offscreen by tests, not used on a display.
- A log on a steady clock is analysed on its own timestamps. A log with no
  single rate is resampled to a uniform one first, because the filters need
  it; that is recorded on each derived channel. The model keeps the native
  samples either way.
- Units the file does not state are still assumed for analysis, and flagged.
- Thresholds of the older analysis modules are overridden through
  `veltrix_engine.thresholds` rather than passed as arguments.
- Event thresholds and the tyre operating window are illustrative.
- Recommendations have not been validated on a car.
