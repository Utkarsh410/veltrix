# Veltrix Data

Motorsport telemetry and vehicle-performance analysis platform.

This repository currently holds the **analysis engine** as a Python reference
implementation: it reads a telemetry file in any of the formats below,
normalises it to one channel schema, finds and times laps, computes derived
channels, and answers lap-comparison and engineering questions. The
**workspace** is a local page in front of it: drop files in, open their reports.

## Formats

| Format | Files | Read by | Laps come from |
|---|---|---|---|
| CSV | `.csv` `.tsv` `.txt` | built in | lap channel, beacon markers, lap timer, distance resets |
| MoTeC | `.ld` (+ `.ldx`) | built in | the `.ldx` beacons, or a lap channel |
| Assetto Corsa | `.ld` (+ `.ldx`), `.csv` | built in | as MoTeC: ACC writes `.ld` / `.ldx` itself, Assetto Corsa through ACTI |
| iRacing | `.ibt` | built in | the `Lap` variable |
| VBOX | `.vbo` | built in | the start/finish line stored in the file |
| AiM | `.xrk` `.xrz` `.drk`, RaceStudio `.csv` | `libxrk` (CSV: built in) | the logger's lap table; beacon markers in CSV |
| MDF / MF4 | `.mf4` `.mdf` `.dat` | `asammdf` | a lap channel |
| MAT | `.mat` | built in (v7.3: `h5py`) | a lap channel |
| CAN | `.asc` `.log` `.trc` `.csv`, `.blf`, MF4 bus logging | built in (`.blf`: `python-can`) | a lap signal in the DBC |
| DBC | `.dbc` | built in | — it decodes CAN logs |

Any of them may be gzip-compressed (`.gz`). A file is recognised by its content,
not only its name. `veltrix formats` lists the formats and says which optional
readers are installed; `pip install -e ".[all]"` installs all of them.

A CAN log holds frames, not channels, so it needs its DBC: pass `--dbc car.dbc`
(or `dbc=` in Python), or keep the `.dbc` in the same folder as the log.

## Desktop app (in progress)

Double-click **Veltrix Data.app** in this folder (built by `sh tools/build_mac_app.sh`), or from Terminal:

```bash
.venv/bin/pip install -e ".[ui]"          # PySide6 and pyqtgraph
.venv/bin/veltrix app path/to/session.csv
```

The window has:

- a "Measures and laps" panel on the left: channels as buttons with their values at the cursor, the
  session's laps to load by clicking, and a channel-details table (search by name, unit, layer, kind,
  quality flag or source column);
- stacked charts on one time axis with one cursor, and a readout of every charted channel at the
  cursor — values between samples are marked as interpolated or held;
- a y-axis per unit on each chart, and a marked range with min, max, mean and change for every
  charted channel;
- loaded laps drawn over each other by distance or by time (the mode is always shown), with delta
  time against a reference lap and each lap's value at the cursor;
- a maths channel editor (Ctrl+M): formula colouring, live validation with the error marked, unit
  checking, a preview plot before saving, a channel picker, the function reference, named parameters,
  and a dependency view;
- a track map coloured by any channel, following the cursor; a click on it moves the cursor. A map
  rebuilt from speed and lateral g says so;
- detected events (worded as candidates for review) and your own notes, kept apart; a click jumps there;
- saved workspaces that reopen, and still open on a session that lacks some of their channels;
- "Open with mapping…": every column with what it was taken to be and why, to check, change and
  confirm, and to save as a profile for the next file from the same logger;
- video beside the data: playing it moves the cursor, moving the cursor seeks it. Sync is a manual
  offset or a fit to anchors, with the residuals shown; a sync that has not been checked says so.

It has been driven by tests offscreen, including a full walk-through on a 79-minute session, but
and by hand on a real display; video only with a clip the tests encode themselves. See
[docs/implementation-roadmap.md](docs/implementation-roadmap.md).

## Workspace (browser)

```bash
.venv/bin/veltrix workspace my_project
```

Opens a page on this machine (`127.0.0.1` only). Drop in files of any format,
together with their `.dbc` or `.ldx` where they have one. Each becomes a session
in the project folder; the page shows what it was read as, the channels
recognised, the laps and every note from the import, and opens its report.
Dropped DBC files stay in the workspace and decode every CAN log added later.

It was extracted from two earlier toolkits (see [docs/AUDIT.md](docs/AUDIT.md))
and is the baseline a native macOS core will be validated against.

## Engineering report

Every session produces the same nine sections, in the same order: **lap
potential, driver performance, braking, cornering, traction, tyres, vehicle
balance, incidents, setup hypotheses**. Each conclusion in them carries its
chain:

| | |
|---|---|
| Evidence | the numbers from this session it rests on |
| Calculation | how those numbers were worked out, with the thresholds used |
| Confidence | high, medium or low, and what sets it |
| Recommendation | what to do about it |

```bash
.venv/bin/veltrix engineer path/to/session.csv
.venv/bin/veltrix engineer path/to/run.ld --markdown report.md --json report.json
```

It is also the first tab of the HTML report, it is written beside every session
the store or the workspace takes in (`sessions/<id>/engineering.json`), and
`engineering.analyse(session)` returns it in Python.

Confidence is a rule, not a judgement — the lowest of: flying laps (8 or more
high, 5 to 7 medium, fewer low); the size of the effect against its own
lap-to-lap scatter (2× high, 1× medium); data quality (a channel recorded too
coarsely, or rebuilt); and a named cap wherever a finding leans on a generic
figure such as a default tyre window or a setup rule of thumb.

A section the file cannot support is still there and says what is missing — a
session without lateral g gets "Vehicle balance: not assessed, it needs steering
and a usable lateral acceleration channel", not a guess. A session with nothing
wrong gets findings that say so, with the numbers that show it.

## Without Terminal (macOS)

```bash
sh tools/build_mac_app.sh        # once; builds "Veltrix Report.app" in this folder
```

Drag **Veltrix Report.app** to the Dock or Desktop. Double-click it to pick
telemetry files, or drop files of any supported format onto it. Each file's report is written to
`reports/` and opens in the browser. The app remembers where this folder is, so
build it again if the folder moves.

## Quick start

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev,all]"      # all = the optional format readers
.venv/bin/python -m pytest
```

Inspect any telemetry file from the command line:

```bash
.venv/bin/veltrix inspect path/to/session.csv
.venv/bin/veltrix inspect path/to/run.ld
.venv/bin/veltrix inspect path/to/bus.asc --dbc path/to/car.dbc
.venv/bin/veltrix inspect path/to/session.csv --ref 11 --compare 12 --export out/
```

Or write a self-contained HTML dashboard. Its charts sit together in one grid and
share a cursor: track map, speed, time gained or lost, throttle, brake, time by
corner, G-G, lap times and incidents by corner. Suggestions, corner tables, the
incident list, stints, sectors and import notes are in tabs underneath:

```bash
.venv/bin/veltrix report path/to/session.csv -o session.html
```

`inspect` prints what was read, every assumption the engine made, the lap table with
stints, sectors, a lap comparison and the engineering summaries the channels
allow. `--export` also writes the tables as CSV. Other options: `--flip` for
channels with opposite signs, `--unit COLUMN=UNIT` to correct a unit,
`--wheelbase`, `--steer-ratio`, `--mass`, `--grip-limit`, `--front-weight`, `--wheel-rate-front`,
`--wheel-rate-rear`. iRacing files supply the mass and static front weight from the setup they carry.
A value that is neither stated nor in the file is a placeholder, and the report says so rather than
quoting it as the car's.

The same from Python:

```python
from veltrix_engine import load_session, laps
from veltrix_engine.analysis import gg, tyres

s = load_session("datasets/golden/monza_ac_gt3.csv.gz")     # or .ld, .ibt, .vbo, .xrk, .mf4, .mat
s = load_session("bus.asc", dbc="car.dbc")                  # a CAN log with its database

s.laps                 # lap table: times line to line, status per lap
s.channels             # normalised channels on a uniform time base
s.derived              # channels computed by the engine
s.notes                # every unit conversion, assumption and data problem

best = laps.best_lap(s)
cmp = laps.compare_laps(s, ref_lap=best, lap=1, channels=("speed", "throttle"))
cmp["delta_t"]         # time lost or gained against the reference, per metre
laps.biggest_swing(cmp["delta_t"].to_numpy(), cmp.index.to_numpy())
laps.sector_times(s)
```

Storing sessions in a project:

```python
from veltrix_engine import ProjectStore

store = ProjectStore("my_project")
sid = store.import_file("session.csv", event="Test day", track="Monza", driver="A. Driver")
store.sessions()                                   # one row per session
store.channels(sid, columns=["time", "speed"])     # reads only those channels
```

## What the engine does

| Area | Capability |
|---|---|
| Import | Ten formats into one table (see Formats). CSV with any of four delimiters, decimal points or commas, logger-software header block, units row, beacon markers. Loggers that record each channel at its own rate are laid on one time base: linear between samples, flags and counters held, nothing filled in before a channel starts |
| CAN | DBC decoding: Intel and Motorola byte order, signed, float and double signals, multiplexed signals, CAN FD payloads, 29-bit identifiers, J1939 matched by PGN whatever the source address; identifiers not in the DBC are reported |
| Channels | 49 measured and 10 derived channels in one schema with fixed units and signs; whole-word name matching |
| Units | Converted from the units row; guesses are flagged as warnings; accelerations mislabelled between g and m/s² are caught against the speed channel |
| Data checks | Gaps, repeated or backwards timestamps, channels recorded too coarsely to analyse, steering / lateral g / yaw rate disagreeing in sign |
| Laps | From a lap channel, beacons, a restarting lap timer, lap-distance resets or a position gate; line-to-line timing; out-lap / flying / slow / short (likely track cut) / pit / partial / in-lap; pit stops split the session into stints |
| Comparison | Laps aligned on a distance grid, delta time, biggest loss and gain, sector times, theoretical best |
| Corners | Corners found from the data (lateral g, position or steering); per-corner braking point, entry, minimum and exit speed, throttle pickup; time gained or lost per corner between two laps with what differed; best, typical and spread per corner; ideal lap |
| Circuit files | A circuit's own corner numbers and names (`src/veltrix_engine/tracks/*.json`), used automatically when the venue matches and the file fits the lap; Silverstone GP and Monza GP are included. `veltrix circuit FILE` drafts one from a session for editing |
| Balance incidents | Understeer and oversteer detected per lap and located by corner and phase (entry, apex, exit), with cause (power, braking, lift-off), opposite lock, wheelspin evidence, severity and time cost; each corner's habitual balance |
| Engineering report | Nine sections for every session (lap potential, driver, braking, cornering, traction, tyres, balance, incidents, setup hypotheses); every conclusion with its evidence, calculation, confidence and recommendation; sections the data cannot support say what is missing |
| Suggestions | Driver improvements per corner (the typical lap against the driver's own best pass: braking point, minimum speed, throttle pick-up, consistency) and setup directions to test (rear traction, entry stability, front grip, camber, tyre and brake temperatures), each with the numbers behind it and a confidence level |
| Derived | Longitudinal and lateral g from speed and steering, steering rate, yaw rate, oversteer metric, load transfer and its front share, grip utilisation |
| Analysis | G-G envelope and utilisation, steering ratio and understeer gradient identified from the data, tyre window classification, temperature anomalies, degradation fit per stint |
| Track map | From logged position or GPS; otherwise rebuilt from speed and lateral g and closed into a lap. In the report it carries every corner, shares one cursor with the charts, and can be coloured by speed, time gained or lost, or throttle and brake |
| Models | Magic Formula tyre forces; understeer gradient against anti-roll-bar stiffness |
| Canonical model | One model for every format: raw, normalised, corrected and calculated channels, each on its own timestamps with its unit, label, validity and provenance; laps, stints, events, annotations ([docs/data-model.md](docs/data-model.md)) |
| Maths channels | A safe formula language (never executed as code) with unit checking, validity masks, gap-aware derivatives and filters, dependency ordering and caching ([docs/maths-channels.md](docs/maths-channels.md)) |
| Mapping profiles | Column-to-channel decisions made in a fixed order (what the format says, a verified profile, name plus value plausibility, the user) and recorded per column with basis, confidence and unit source; reusable profiles per logger, car or series; import diagnostics ([docs/mdf-compatibility.md](docs/mdf-compatibility.md)) |
| Events | Configurable rules over recorded and maths channels (hysteresis, minimum duration, severity, evidence), every threshold a named parameter; events worded as candidates for review; rules a file cannot support are reported, not guessed ([docs/technical-reference.md](docs/technical-reference.md#events)) |
| Thresholds | Every analysis threshold listed (`veltrix thresholds`) and overridable per run or per project |
| Store | SQLite metadata, Parquet telemetry, source file kept byte-for-byte, with the `.ldx` or DBC files it was read with |
| Workspace | Local page: drop files of any format in, see what each was read as, open reports |

## Layout

```
src/veltrix_engine/
  schema.py            channel ids, units, signs, known source names
  importers/           file → RawTable (one module per format; read_file picks the module)
  workspace/           the local workspace in the browser: server and page
  display.py           min/max decimation for drawing
src/veltrix_app/       the desktop app: workspace.py (state, no Qt), window.py (PySide6 + pyqtgraph)
  mapping.py           source columns → channel ids
  maths/               maths channels: parser, units, functions, dependency graph
  events.py            the event engine: configurable rules, candidates for review
  mapping_profiles.py  column-to-channel decisions and reusable profiles
  thresholds.py        every analysis threshold, listed and overridable
  model.py             the canonical session model: channels by layer, laps, stints, events, annotations
  model_build.py       builds the model during import; model_store.py keeps it on disk
  session.py           the import pipeline and the Session object (the model's analysis view)
  laps.py              lap detection, timing, delta, sectors
  corners.py           corner detection and corner-by-corner analysis
  circuits.py          circuit files: official corner numbers and names
  derived.py           derived channels
  analysis/            gg.py, tyres.py, balance.py, handling.py (understeer / oversteer),
                       advice.py (driver and setup suggestions),
                       engineering.py (the nine-section engineering report)
  models/              pacejka.py, arb.py
  track.py             track map
  store.py             project store
  report/              self-contained HTML session report
  cli.py               `veltrix workspace`, `inspect`, `report`, `formats`, `circuit`
  synth.py             synthetic session generator
  vehicle.py           vehicle parameters
datasets/golden/       frozen reference sessions
datasets/expected/     frozen engine outputs on them
tests/                 golden-output, behaviour and store tests
tools/                 build golden data, freeze outputs, generate the schema doc
benchmarks/            engine timings
docs/                  BRIEF.md (the build plan), implementation-roadmap.md (status and what comes next),
                       architecture-audit.md, data-model.md, testing-strategy.md,
                       CHANNEL_SCHEMA.md, ARCHITECTURE.md, AUDIT.md
```

## Documentation

[User guide](docs/user-guide.md) · [technical reference](docs/technical-reference.md) ·
[data model](docs/data-model.md) · [maths channels](docs/maths-channels.md) ·
[MDF compatibility](docs/mdf-compatibility.md) · [performance](docs/performance.md) ·
[packaging route](docs/packaging.md) · [roadmap and status](docs/implementation-roadmap.md)

## Status

The build follows [docs/BRIEF.md](docs/BRIEF.md); where it stands and what comes next is in
[docs/implementation-roadmap.md](docs/implementation-roadmap.md).

Foundation phase of the product plan:

| Item | State |
|---|---|
| Audit of the existing toolkits | Done — [docs/AUDIT.md](docs/AUDIT.md) |
| Canonical channel schema, units and sign conventions | Done — [docs/CHANNEL_SCHEMA.md](docs/CHANNEL_SCHEMA.md) |
| Core calculations extracted and tested | Done — 392 tests |
| Golden datasets with expected outputs | Done for synthetic and simulator data; **no real-car dataset yet** |
| Tested on a full simulator session | Done — see below |
| Lap detection, distance alignment, delta | Done |
| Telemetry data model and session store | Done |
| Production architecture | Recommended, not yet confirmed — [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| Interactive plot and synchronised cursor | Started — desktop app, first slices (see the roadmap) |

## Tested on Assetto Corsa data

The engine was run on a logger-software CSV export of an Assetto Corsa session:
Porsche 991 GT3 R at Monza, 200 Hz, 50 minutes, 605,140 rows, 57 columns, 170 MB.
It loads in about 1.5 s and all 25 laps are timed from the beacon markers.

What that file taught the engine (each now covered by a test in
`tests/test_real_export_cases.py`):

- Simulator channel names (`G_LAT`, `STEERANGLE`, `ROTY`, `SUS_TRAVEL_LF`, ...).
- Accelerations labelled m/s² that are actually in g, caught by comparing
  longitudinal acceleration with the rate of change of speed.
- Pit stops, stints, and tyre changes showing up as temperature drops.
- A lap that cut the first chicane, 31 m shorter than the rest, which must not
  become the best lap.
- A `..` placeholder in the units row.

**That export has every channel rounded to whole numbers** (speed in whole m/s,
accelerations in whole g), and it cannot be re-exported. The engine therefore
rebuilds what a coarse file destroys, and says so in the import notes:

| Signal | Rebuilt from | Accuracy on a known-answer session degraded the same way |
|---|---|---|
| Longitudinal g | d(speed)/dt with a 2 Hz low-pass instead of 15 Hz | 0.12 g error, against 0.96 g without the change |
| Lateral g (`acc_lat_rebuilt`) | Damper roll, calibrated against the logged whole-g channel | 0.02 g error, against 0.25 g for the logged channel |

On the real file the damper-roll fit leaves 0.286 g of error against the logged
channel, where rounding alone accounts for 0.289 g: the rebuilt signal explains
everything the logged channel can show. Grip, balance and the track map then
use the rebuilt signals. Without damper travel a coarse lateral g cannot be
rebuilt, and those analyses are skipped rather than computed from bad data.

The file is not in `datasets/golden/` because of its size.

## Known limits

- No dataset from a real car has been through the engine. The tyre and
  anti-roll-bar models use illustrative coefficients and assumed bar rates.
- Only CSV and AiM `.xrz` have been through the engine as real files (the AiM one a Fuji session from
  the `libxrk` test data: eight laps timed from the logger's lap table). The other importers are tested
  against files written by the test suite from each format's documented layout
  (`tests/format_writers.py`) or by an independent library (`python-can` for `.asc`, candump, `.trc` and
  `.blf`; `asammdf` for MF4; SciPy for MAT), not against files from a logger or simulator. MoTeC `.ld`
  and iRacing `.ibt` have only the suite's own writer behind them: `.ld` follows the layout worked out by
  the open-source `ldparser` project (the format is unpublished), and `.ldx` beacon times are taken to be
  microseconds. Expect the first real file of each to need a fix or two, and add it to the golden
  datasets when it works.
- Assetto Corsa has no telemetry file of its own: it is read through what it exports (MoTeC `.ld` or CSV).
- MAT files have no fixed layout. Per-channel structs, Simulink logging, equal-length vectors and a matrix
  with a name list are recognised; MATLAB tables, timetables and cell arrays in v7.3 files are not.
- Not read: LIN, FlexRay and Ethernet bus logging in MF4; DBC extended multiplexing and value tables;
  array variables in `.ibt`; channels in `.ld` encodings the reader does not know (they are reported).
- Signs are not converted between formats. A format with the opposite convention for steering or
  lateral g is caught by the sign check and imported with `flip`.
- A multi-rate log too large to hold at its fastest channel's rate (about 60 million cells) is laid on a
  slower time base, with a warning.
- A rebuilt track map (no position in the file) shows the circuit's shape and corner order, not surveyed positions: on a 10 km test lap with realistic accelerometer error it was within about 60 m on average. With larger errors the path will not close and is drawn open. Its orientation on the page is arbitrary.
- The engineering report's recommendations have not been validated on a car. Its numbers are checked
  against the corner, handling and tyre modules they come from, and its conclusions against staged
  cases (a clean session, repeated power oversteer, a file with no laps); nobody has yet changed a car
  or a driver's technique on its advice and measured the result. Most sessions are short: under five
  flying laps every comparison is rated low confidence, by rule.
- Time "at stake" in braking, cornering and traction findings is the time available in the zones a
  finding concerns. Those sections look at the same zones from different angles, so their figures
  overlap and must not be added up; only lap potential gives a figure for the whole lap.
- A faster pass that braked later, carried more speed or took throttle earlier is evidence of what went
  with the time, not proof of what caused it. The report says so in each such finding's confidence.
- Tyre findings use a generic operating window and the usual camber rule of thumb, and no target
  pressure is known; they are capped at low or medium confidence for that reason.
- Suggestions are directions to test, not instructions. The engine does not know the car's current setup, adjustment ranges or regulations, and the setup directions are standard rules of thumb applied to the patterns it sees. They have not been validated by changing a car and measuring the result.
- Understeer / oversteer detection compares each lap with the session's own norm, so it needs several flying laps, and it reports departures from that norm. A car that slides the same way at the same place every lap shows up as that corner's habit, not as incidents. It has been checked against staged incidents and against the raw channels of one simulator session, not against a driver's or engineer's notes.
- Corner numbering: detection finds the corners a car actually takes as separate events. A circuit's official count can differ — a long corner counted as two turns (Club at Silverstone), a numbered kink taken flat — and Turn 1 depends on the start line. Only a circuit file gives official numbers and names; without one, corners are numbered in the order driven. Circuit file distances are from the start line of the session they were made from, and a file that does not fit the lap is ignored with a note.
- Corner analysis: a corner's best time can come from a lap that ran wide or cut it, since only whole-lap cuts are detected. The "what differed" text describes differences that go with a time change; it does not prove cause.
- Rebuilt lateral g assumes roll is proportional to lateral g. On the test file the scale varied by about 10% between low and high speed, so treat peaks as good to roughly that.
- The steering ratio found from data is tied to the wheelbase in `VehicleParams`; only their product is identified.
- Maths channels made in the app's editor do not yet appear in a report made from the command line.
  The engine's own derived channels are still fixed functions.
- A position gate needs the caller to say where the line is, and fails on
  layouts that pass the line twice per lap on parallel straights.
