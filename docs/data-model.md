# Data model

The canonical session model (`src/veltrix_engine/model.py`). Every file format
is brought to it, and it is what calculations, reports and the UI are to read.
Status: built in Phase 1; see [implementation-roadmap.md](implementation-roadmap.md).

## Entities

| Entity | Holds |
|---|---|
| `SessionModel` | sources, channels by layer, laps, stints, events, annotations, views, import notes |
| `SourceFile` | name, SHA-256, format, header metadata as the file gives it, column and row counts |
| `Channel` | one signal on its own timestamps, with what it is and how it came to be (below) |
| `Lap` | label, start and end time, lap time, distance, status, stint, whether each end was a real line crossing |
| `Stint` | number, its laps, start and end, flying-lap count |
| `Event` | something a detector found: kind, time span, lap, position, severity, channels, evidence, confidence, rule and rule version |
| `Annotation` | a note a person made: text, scope (session / lap / time / position), category, author |
| `View` | an alignment that was made: rate, the lookup policy per channel, the frame |

## Channel

| Field | Meaning |
|---|---|
| `id` | schema id (`speed`) in the normalised and later layers; the source column name in the raw layer |
| `layer` | `raw`, `normalised`, `corrected` or `calculated` |
| `label` | what kind of statement the channel is: `measured`, `corrected/reconstructed`, `calculated` |
| `name`, `source_name`, `description`, `category`, `quantity` | display name, the column in the file, text, browser category, physical quantity |
| `unit` | explicit. `unknown` in the raw layer when the file gives none |
| `timestamps`, `values` | the samples; arrays are shared, not copied |
| `sample_rate`, `dtype` | read from the arrays |
| `discrete` | lookup policy: hold-previous instead of interpolation |
| `valid` / `validity` | mask of usable samples (explicit mask and finite) |
| `formula`, `dependencies` | for derived channels |
| `quality` | flags such as "recorded in coarse steps of 1 g", "unit assumed" |
| `provenance` | `inputs`, `algorithm`, `parameters` (scale, offset, filter, timebase), `unit_source`, `engine_version`, `notes` |
| `version`, `user` | bumped when a derived channel is replaced; free user metadata |

## Layers

```
raw          source column, source unit, logger timestamps      never written over
   │  unit and sign brought to the schema (scale and offset recorded)
normalised   schema id and unit, still the logger's timestamps  never written over
   │  rebuilt from other channels            │  computed from other channels
corrected    e.g. lateral g from damper roll  calculated   derived and (Phase 2) maths channels
```

`SessionModel.add` refuses to overwrite a raw or normalised channel. A
corrected or calculated channel can be replaced only on request, and its
`version` goes up. `get("speed")` returns the most processed layer that has the
id; `get("raw:Ground Speed")` names a layer.

### Where a unit came from (`provenance.unit_source`)

| Value | Meaning |
|---|---|
| `file` | the file's units row or channel metadata |
| `stated` | the user gave it at import |
| `plausibility` | worked out from the values (a 0–1 throttle, a speed that matches the distance channel) and flagged |
| `assumed` | the file gives none and nothing in the values settles it; the schema unit was taken and the channel is flagged `unit assumed` |
| `schema` | a derived channel, whose unit is defined by its formula |
| `none` | the channel has no unit |

The raw layer always shows what the file says, so an assumption is visible as
`raw: unknown` beside `normalised: km/h, unit assumed`. Assumptions are a known
gap against hard rule 3; mapping profiles with user confirmation (Phase 4) are
where they get closed.

## Sampling policy

- Every channel keeps its own timestamps. A MoTeC, AiM, MDF or CAN log with
  channels at 5, 10 and 20 Hz has channels at 5, 10 and 20 Hz in the model.
- Nothing is resampled on import or on the way to disk.
- A calculation that needs a common clock asks for an alignment
  (`SessionModel.align`, `Channel.at`):
  - continuous channels: linear interpolation between valid samples;
  - discrete channels (gear, lap, flags): the previous sample is held;
  - no value outside a channel's own first and last sample;
  - no value across a gap (a step longer than five median steps);
  - `at()` also returns which values fell exactly on a sample, so a readout
    can mark the interpolated ones.
- The lap, corner and balance analysis runs on one such alignment, the view
  `analysis`: all normalised channels on one uniform clock at the lowest
  standard rate that does not undersample the file. It is recorded in
  `model.views["analysis"]` with its policy. Derived channels computed on it
  say so in `provenance.parameters` (`timebase`, `rate_hz`).

Known limit: the engine's derived channels are still computed on that view,
not on native timestamps, and multi-rate importers still build one table for
the mapping step. The model keeps the native samples either way.

## The existing engine and the model

`load_session` builds the model and returns a `Session` whose `model`
attribute is it. `Session.channels` is the frame of the `analysis` view and
`Session.derived` the calculated channels on it, so every existing calculation
runs on the model unchanged, and the golden outputs are identical to before.

## On disk

```
sessions/<id>/
  source/<file>            the imported file, read-only — the storage of the raw layer
  model/manifest.json      all entities, every channel's metadata and provenance (raw included)
  model/group_<n>.parquet  arrays: channels that share a timestamp array, stored with it once
  channels.parquet         the analysis view
  derived.parquet          the calculated channels on the view
  engineering.json         the engineering report
project.sqlite             sessions, laps, drivers, tracks (no per-sample rows)
```

`manifest.json` carries `model_version`; a newer one than the engine knows is
refused with a message. A session stored before the model existed gets its
model built from the kept source file the first time it is asked for, so older
projects open unchanged. `load_model(folder, channels=[...])` reads only the
arrays asked for.

Measured on the reference session (951,694 samples, 57 columns): the model
adds 0.3 s to import and no measurable memory; saving takes 0.9 s and 74 MB;
loading everything 0.3 s, three channels 10 ms.
