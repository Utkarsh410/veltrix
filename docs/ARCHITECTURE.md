# Architecture

The canonical data model every layer below reads is described in
[data-model.md](data-model.md).

## Layers

```
desktop app            src/veltrix_app       PySide6 windows and docks; no analysis of its own
        │                                     (Qt-free helpers: workspace, compare, trackmap, video_sync)
report / CLI           veltrix_engine.report, veltrix_engine.cli
        │
analysis               veltrix_engine.analysis   engineering report (a package, one module per section),
        │                                        advice, balance, handling, tyres, gg, plausibility
engine                 veltrix_engine            importers → mapping → session → laps → corners → derived;
        │                                        maths, events, user_data (notes, reviews, rules, data folder)
storage                model_store, store        Parquet + manifest per session; SQLite project database
```

**Rules between the layers** — checked by `tests/test_architecture.py` on every run:

- The engine never imports the desktop app. The single exception is the
  `veltrix app` command, whose job is to start it. Anything the command line
  and the app both need (the data folder, saved maths, rules, notes, reviews)
  lives in the engine (`user_data.py`).
- The analysis never imports the report or the command line. The report reads
  the analysis; the analysis does not know how it is shown.
- No module is longer than 900 lines. A module that size is several modules;
  the engineering report was split for this reason, the window's session charts
  moved to `session_charts.py`.

**Errors** (`errors.py`): `VeltrixError` with `FileFormatError` (a file that
cannot be read), `MappingError` (columns that cannot become the channels the
analysis needs) and `VersionError` (a saved file from a release this one cannot
read or must not overwrite). All are also `ValueError`, so older callers still
catch them. `read_file` turns anything an importer trips over into a
`FileFormatError` naming the file; the details go to the log.

**Saved files** (`versions.py`): every saved kind carries a format version and is
read through `versions.upgrade` — newer files are refused with a reason, older
ones are migrated one step at a time; stores call `refuse_overwrite` before
writing. `tests/fixtures/saved_release_1/` holds one file of every kind as
release 1 wrote it; `tests/test_saved_files.py` keeps them opening.

**Log and crash reports**: `logs.setup()` writes `logs/veltrix.log` in the data
folder (file names, formats, timings, errors — never telemetry). The app's
`crash.install()` writes `crash_reports/crash-*.txt` on an unexpected error and
tells the user where; nothing is sent.

**Tests**: `pytest` runs everything on every push (`.github/workflows/tests.yml`,
macOS). `pytest -m real` runs the regression check on the user's real sessions,
kept on this machine with their frozen outputs (`tests/real_sessions.py`,
`tools/freeze_real_sessions.py`).

## Import pipeline

`load_session(path)` runs these steps in order. Each is a separate function.

| Step | Module | Output |
|---|---|---|
| Read | `importers/` (`read_file`) | `RawTable`: source columns, units, header metadata, beacons, importer notes, SHA-256 |
| Map | `mapping.py` | `{channel id: source column}` |
| Convert units | `session.py` | Channels in schema units; every conversion or guess recorded |
| Check | `session.py` | Notes on gaps, backwards or repeated timestamps, bad cells |
| Find laps | `laps.py` | Lap table with line-to-line times and a status per lap |
| Resample | `session.py` | Uniform time base; `lap` and `lap_distance` rebuilt so they are exact at the line |
| Derive | `derived.py` | Derived channels whose inputs exist; the rest skipped and reported |

`read_file` recognises the format from the file's content (then its extension)
and hands it to one module per format. Every module returns the same
`RawTable`, so nothing after the read step knows what the file was:

| Module | Formats | Notes |
|---|---|---|
| `csv_importer` | CSV exports of any logger | |
| `motec` | MoTeC `.ld`, beacons from `.ldx` | multi-rate |
| `iracing` | `.ibt` | single rate; every float the SDK labels "%" is stored 0–1 and is scaled to 0–100 at import (full scale reads 1.00000012 as a 32-bit float) |
| `vbox` | `.vbo` | decodes the VBOX clock and minute-based coordinates; beacons from the stored start/finish line |
| `aim` | `.xrk` `.xrz` `.drk` via `libxrk` | multi-rate; beacons from the logger's lap table |
| `mdf` | MDF 3 / 4 via `asammdf` | multi-rate; raw CAN bus logging decoded with a DBC |
| `mat` | MATLAB v5 / v7.3 | layout is recognised, not fixed |
| `can` + `dbc` | `.asc`, candump, `.trc`, CSV frame logs, `.blf` | frames → signals with a DBC; multi-rate |

Multi-rate formats are read as `Signal`s (name, own clock, values, unit) and
`importers/table.py` lays them on one uniform time base at the fastest
channel's rate. Three things an importer may pass on besides the table:
`beacons` (lap-marker times), `notes` (what it decoded or left out) and `hints`
(`{channel id: column}` for channels the format itself identifies, such as
iRacing's tyre temperatures, which no name matching would find).

Formats from outside the engine are plug-ins (`importers/plugins.py`): an
`Importer` registered in code or through the `veltrix.importers` entry point.
Detection tries them after the formats that sign their files and before the
guesses from names and text. Their tables are checked against the same
contract (`check_table`) before anything else sees them, and
`importers/testkit.py` is the test every importer passes. The guide is
[importers.md](importers.md). `batch.py` runs the import (and the report,
engineering report or export) over a folder, one process per file when asked.

`library.py` is the session library: a SQLite index (`library.sqlite` in the
app's data folder) of sessions where they lie, with one summary row per file,
user overrides, aliases, tags and events. It reads files only to index them
and to compare two; the project store (`store.py`) remains the place that
keeps copies and analysed data. `cli_library.py` is its command line.

An importer decodes only what is the format's own encoding (a clock written as
HHMMSS, coordinates in minutes). Units and signs are still converted in one
place, `session.py`.

Laps are found on the samples as logged, before resampling, so crossing times
are as exact as the source allows.

Lap boundaries come from the best source the file offers, in this order: lap
channel (with beacons or lap-timer restarts as exact crossing times when their
count matches), beacon markers, lap-timer restarts, lap-distance resets, a
position gate, otherwise the whole file is one lap.

## Engineering report

`analysis/engineering.py` is the top of the analysis stack. It adds no signal
processing: it takes what `corners`, `handling`, `tyres`, `balance` and `advice`
measure and arranges it as nine fixed sections of findings, each a dict of
`conclusion`, `evidence`, `calculation`, `confidence {level, basis}`,
`recommendation`, and `gain_s` or `at_stake_s`. The result is plain JSON, so the
HTML report, the CLI, the store (`engineering.json` per session) and the
workspace all show the same thing, and a native app can render it unchanged.

A finding's confidence comes from one function (`_Ctx.conf`) and is the lowest
of laps, effect against scatter, data quality and named caps. Thresholds are
module constants and are quoted in each finding's calculation text.

## Rules the engine holds to

- **Imported files are immutable.** The store keeps the source byte-for-byte,
  read-only, identified by SHA-256. A session can always be rebuilt from it.
- **Raw, normalised and derived are separate.** Importers can be replaced
  without touching calculations.
- **Nothing is invented.** A derived channel whose inputs are missing is
  skipped and reported, not filled with zeros.
- **Every assumption is recorded.** Unit guesses, re-ordered samples and sign
  flips all appear in `Session.notes`.
- **Signs are never guessed.** A file with opposite signs is imported with an
  explicit `flip`.

## Numerical contract

`datasets/expected/*.json` holds the engine's outputs on the golden datasets:
the lap table, summary statistics and 200 evenly spaced samples of every
channel, sector times, a delta trace, G-G and tyre metrics, and the tyre and
anti-roll-bar model outputs. `tests/test_golden.py` checks the Python engine
against them to a relative tolerance of 1e-6.

A native implementation counts as numerically equal when it reproduces these
files. The JSON is plain numbers and strings so any language can read it.

At the time of extraction the derived channels matched the GT3 toolkit's
pipeline to machine precision on the synthetic session, and lap times matched
the race engineer toolkit exactly on the Monza session.

## Production stack: recommendation

The plan leaves the core language open between Swift, Rust and C++.

**Recommended: Swift for both the app and the core, as a Swift package.**

- One language and one build for a Mac-first product; the package builds and
  tests with the command-line toolchain, without Xcode.
- The engine's hot paths are array arithmetic, interpolation and IIR filtering,
  which Accelerate (vDSP) covers.
- The Python engine's benchmark is the bar: a 30-lap, 248,000-sample, 42-channel
  session imports in about 350 ms and a lap delta takes about 3 ms. Swift should
  beat both comfortably; there is no performance case for Rust or C++ yet.
- Choose Rust instead only if a Windows or Linux build becomes a requirement.

**Storage.** SQLite for metadata as now. For telemetry in the native app,
prefer one memory-mapped binary array per channel with a JSON manifest over
Parquet: Swift has no mature Parquet reader, and a plot needs random access to
a few channels, not a column-compressed file. Parquet stays as the format the
Python engine writes and as an exchange format.

**Plotting.** Interactive plots with a synchronised cursor over hundreds of
thousands of points need a custom Metal- or Core Graphics-backed view with
min/max decimation per pixel column. Swift Charts is not built for that.

These three are recommendations to confirm before the vertical slice starts.

## Open questions

- A real file of each format for the golden datasets. The binary importers are
  tested against files the test suite writes itself; see Known limits in the README.
- Whether `brake` should be split into `brake_pos` (%) and `brake_pressure`
  (bar). Today it is one channel and the session records which unit it holds.
- A standard rate is chosen per file (the lowest of 1, 2, 5, 10, 20, 25, 50, 100,
  200, 250, 500, 1000 Hz that does not undersample). Channels logged at
  different rates in one file are all brought to that rate.
