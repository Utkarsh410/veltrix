# Veltrix Data

Motorsport telemetry and vehicle-performance analysis platform.

This repository currently holds the **analysis engine** as a Python reference
implementation: it reads a telemetry file, normalises it to one channel schema,
finds and times laps, computes derived channels, and answers lap-comparison and
engineering questions. There is no user interface yet.

It was extracted from two earlier toolkits (see [docs/AUDIT.md](docs/AUDIT.md))
and is the baseline a native macOS core will be validated against.

## Without Terminal (macOS)

```bash
sh tools/build_mac_app.sh        # once; builds "Veltrix Report.app" in this folder
```

Drag **Veltrix Report.app** to the Dock or Desktop. Double-click it to pick
telemetry files, or drop CSVs onto it. Each file's report is written to
`reports/` and opens in the browser. The app remembers where this folder is, so
build it again if the folder moves.

## Quick start

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
```

Inspect any telemetry CSV from the command line:

```bash
.venv/bin/veltrix inspect path/to/session.csv
.venv/bin/veltrix inspect path/to/session.csv --ref 11 --compare 12 --export out/
```

Or write a self-contained HTML report (lap chart, lap comparison with a shared
cursor, corner-by-corner analysis, understeer and oversteer incidents, G-G diagram, stints, sectors and the import notes):

```bash
.venv/bin/veltrix report path/to/session.csv -o session.html
```

`inspect` prints what was read, every assumption the engine made, the lap table with
stints, sectors, a lap comparison and the engineering summaries the channels
allow. `--export` also writes the tables as CSV. Other options: `--flip` for
channels with opposite signs, `--unit COLUMN=UNIT` to correct a unit,
`--wheelbase`, `--steer-ratio`, `--mass`, `--grip-limit`.

The same from Python:

```python
from veltrix_engine import load_session, laps
from veltrix_engine.analysis import gg, tyres

s = load_session("datasets/golden/monza_ac_gt3.csv.gz")

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
| Import | CSV with any of four delimiters, decimal points or commas, logger-software header block, units row, beacon markers, gzip |
| Channels | 49 measured and 10 derived channels in one schema with fixed units and signs; whole-word name matching |
| Units | Converted from the units row; guesses are flagged as warnings; accelerations mislabelled between g and m/s² are caught against the speed channel |
| Data checks | Gaps, repeated or backwards timestamps, channels recorded too coarsely to analyse, steering / lateral g / yaw rate disagreeing in sign |
| Laps | From a lap channel, beacons, a restarting lap timer, lap-distance resets or a position gate; line-to-line timing; out-lap / flying / slow / short (likely track cut) / pit / partial / in-lap; pit stops split the session into stints |
| Comparison | Laps aligned on a distance grid, delta time, biggest loss and gain, sector times, theoretical best |
| Corners | Corners found from the data (lateral g, position or steering); per-corner braking point, entry, minimum and exit speed, throttle pickup; time gained or lost per corner between two laps with what differed; best, typical and spread per corner; ideal lap |
| Balance incidents | Understeer and oversteer detected per lap and located by corner and phase (entry, apex, exit), with cause (power, braking, lift-off), opposite lock, wheelspin evidence, severity and time cost; each corner's habitual balance |
| Suggestions | Driver improvements per corner (the typical lap against the driver's own best pass: braking point, minimum speed, throttle pick-up, consistency) and setup directions to test (rear traction, entry stability, front grip, camber, tyre and brake temperatures), each with the numbers behind it and a confidence level |
| Derived | Longitudinal and lateral g from speed and steering, steering rate, yaw rate, oversteer metric, load transfer and its front share, grip utilisation |
| Analysis | G-G envelope and utilisation, steering ratio and understeer gradient identified from the data, tyre window classification, temperature anomalies, degradation fit per stint |
| Track map | From logged position or GPS; otherwise reconstructed from speed and lateral g, and labelled as such |
| Models | Magic Formula tyre forces; understeer gradient against anti-roll-bar stiffness |
| Store | SQLite metadata, Parquet telemetry, source file kept byte-for-byte |

## Layout

```
src/veltrix_engine/
  schema.py            channel ids, units, signs, known source names
  importers/           file → RawTable (one module per format)
  mapping.py           source columns → channel ids
  session.py           the import pipeline and the Session object
  laps.py              lap detection, timing, delta, sectors
  corners.py           corner detection and corner-by-corner analysis
  derived.py           derived channels
  analysis/            gg.py, tyres.py, balance.py, handling.py (understeer / oversteer),
                       advice.py (driver and setup suggestions)
  models/              pacejka.py, arb.py
  track.py             track map
  store.py             project store
  report/              self-contained HTML session report
  cli.py               `veltrix inspect` and `veltrix report`
  synth.py             synthetic session generator
  vehicle.py           vehicle parameters
datasets/golden/       frozen reference sessions
datasets/expected/     frozen engine outputs on them
tests/                 golden-output, behaviour and store tests
tools/                 build golden data, freeze outputs, generate the schema doc
benchmarks/            engine timings
docs/                  CHANNEL_SCHEMA.md, ARCHITECTURE.md, AUDIT.md
```

## Status

Foundation phase of the product plan:

| Item | State |
|---|---|
| Audit of the existing toolkits | Done — [docs/AUDIT.md](docs/AUDIT.md) |
| Canonical channel schema, units and sign conventions | Done — [docs/CHANNEL_SCHEMA.md](docs/CHANNEL_SCHEMA.md) |
| Core calculations extracted and tested | Done — 133 tests |
| Golden datasets with expected outputs | Done for synthetic and simulator data; **no real-car dataset yet** |
| Tested on a full simulator session | Done — see below |
| Lap detection, distance alignment, delta | Done |
| Telemetry data model and session store | Done |
| Production architecture | Recommended, not yet confirmed — [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| Interactive plot and synchronised cursor prototype | Not started |

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
- Import is CSV only.
- Suggestions are directions to test, not instructions. The engine does not know the car's current setup, adjustment ranges or regulations, and the setup directions are standard rules of thumb applied to the patterns it sees. They have not been validated by changing a car and measuring the result.
- Understeer / oversteer detection compares each lap with the session's own norm, so it needs several flying laps, and it reports departures from that norm. A car that slides the same way at the same place every lap shows up as that corner's habit, not as incidents. It has been checked against staged incidents and against the raw channels of one simulator session, not against a driver's or engineer's notes.
- Corner analysis: a corner's best time can come from a lap that ran wide or cut it, since only whole-lap cuts are detected. The "what differed" text describes differences that go with a time change; it does not prove cause.
- Rebuilt lateral g assumes roll is proportional to lateral g. On the test file the scale varied by about 10% between low and high speed, so treat peaks as good to roughly that.
- The steering ratio found from data is tied to the wheelbase in `VehicleParams`; only their product is identified.
- Derived channels are fixed functions; there is no expression engine yet.
- A position gate needs the caller to say where the line is, and fails on
  layouts that pass the line twice per lap on parallel straights.
