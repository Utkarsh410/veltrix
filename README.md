# Veltrix Data

Motorsport telemetry and vehicle-performance analysis platform.

This repository currently holds the **analysis engine** as a Python reference
implementation: it reads a telemetry file, normalises it to one channel schema,
finds and times laps, computes derived channels, and answers lap-comparison and
engineering questions. There is no user interface yet.

It was extracted from two earlier toolkits (see [docs/AUDIT.md](docs/AUDIT.md))
and is the baseline a native macOS core will be validated against.

## Quick start

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
```

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
| Channels | 40 measured and 10 derived channels in one schema with fixed units and signs; whole-word name matching |
| Units | Converted from the units row; guesses are flagged as warnings |
| Laps | From a lap channel, beacons, a restarting lap timer, lap-distance resets or a position gate; line-to-line timing; out-lap / flying / slow / partial / in-lap |
| Comparison | Laps aligned on a distance grid, delta time, biggest loss and gain, sector times, theoretical best |
| Derived | Longitudinal and lateral g from speed and steering, steering rate, yaw rate, oversteer metric, load transfer and its front share, grip utilisation |
| Analysis | G-G envelope and utilisation, tyre window classification, temperature anomalies, degradation fit |
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
  derived.py           derived channels
  analysis/            gg.py, tyres.py
  models/              pacejka.py, arb.py
  track.py             track map
  store.py             project store
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
| Core calculations extracted and tested | Done — 71 tests |
| Golden datasets with expected outputs | Done for synthetic and simulator data; **no real-car dataset yet** |
| Lap detection, distance alignment, delta | Done |
| Telemetry data model and session store | Done |
| Production architecture | Recommended, not yet confirmed — [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| Interactive plot and synchronised cursor prototype | Not started |

## Known limits

- No dataset from a real car has been through the engine. The tyre and
  anti-roll-bar models use illustrative coefficients and assumed bar rates.
- Import is CSV only.
- Derived channels are fixed functions; there is no expression engine yet.
- A position gate needs the caller to say where the line is, and fails on
  layouts that pass the line twice per lap on parallel straights.
