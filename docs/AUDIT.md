# Audit of the existing toolkits

Two codebases existed before this repository. This is the module-by-module
inventory the product plan asks for: what each part is, how it was classified,
and where it went.

Classes: **engine** (reusable engineering logic, extracted), **prototype**
(useful idea, rewritten), **UI/reporting** (left behind for the app phase),
**debt** (dropped).

Both toolkits stay untouched as reference baselines.

## GT3 Performance Toolkit (`gt3-performance-toolkit-main`)

| Module | What it does | Class | Now in |
|---|---|---|---|
| `telemetry_pipeline.py` — maths channels | Lateral g from the bicycle model, longitudinal g, steering rate, yaw rate, oversteer metric, load transfer from damper travel, grip utilisation | engine | `derived.py` |
| `telemetry_pipeline.py` — loader, alias table, resampling | Flat CSV and one header layout, exact-name aliases, 100 Hz resample | prototype | Replaced by `importers/`, `mapping.py`, `session.py` |
| `telemetry_pipeline.py` — `VehicleParams` | Car constants | engine | `vehicle.py` |
| `generate_sample_data.py` | Point-mass lap simulation producing a consistent synthetic session | engine | `synth.py` |
| `tyre_temperature_analysis.py` — analyser | Window classification, lap summary, anomaly detection, degradation fit | engine | `analysis/tyres.py` |
| `tyre_temperature_analysis.py` — plots | Four matplotlib figures | UI/reporting | Not ported |
| `gg_diagram.py` | G-G density, phase split, envelope per lap (all as plots) | prototype | Numbers in `analysis/gg.py`; plots not ported |
| `pacejka_magic_formula.m` | Magic Formula Fy / Fx, peaks, load sensitivity | engine | `models/pacejka.py` |
| `arb_sensitivity_sweep.m` | Understeer gradient against front bar stiffness, with load transfer and downforce | engine | `models/arb.py` |
| `phase1_analysis.py` | Four matplotlib figures | UI/reporting | Not ported |
| `session_report.py` | Two-page PDF, recommendations computed from the session | UI/reporting | Not ported; the recommendation logic is the part worth keeping when reporting is built |
| `dashboard.py` | Streamlit dashboard | UI/reporting | Not ported |
| `run_analysis.py` | Command-line entry point chaining the phases through a CSV | debt | Replaced by `load_session()` |
| `toolkit_config.py` | Palette, tyre windows, plot style | UI/reporting | Tyre windows moved to `analysis/tyres.py` |

Findings that shaped the engine:

- The MATLAB models had never been run. They are now Python, tested, and their
  outputs are frozen in `datasets/expected/models.json`.
- Channel names were the synthetic generator's own. Anything named differently
  only worked through an exact-match alias list.
- Lap time came from a per-row `LapTime` column that real exports do not have.
- There was no distance channel, so no lap alignment, delta or sectors.

## Race Engineer Toolkit (`race engineer toolkit`)

| Module | What it does | Class | Now in |
|---|---|---|---|
| `telemetry.py` — `load_csv` and helpers | Delimiter and decimal detection, metadata block, units row, beacon markers, bad-cell reporting | engine | `importers/csv_importer.py` |
| `telemetry.py` — `auto_map`, alias and exclusion tables | Whole-word channel matching, each column used once | engine | `mapping.py`, `schema.py` |
| `telemetry.py` — `canonical_channels` | Unit conversion with recorded assumptions | engine | `session.py` (`_to_canonical`) |
| `telemetry.py` — `data_checks` | Sample rate, gaps, backwards and repeated timestamps | engine | `session.py` (`_time_checks`) |
| `telemetry.py` — `detect_laps` and boundary sources | Lap column, beacons, lap-timer restarts, distance resets, GPS gate; line-to-line timing; lap classification | engine | `laps.py` |
| `telemetry.py` — `lap_trace`, `on_distance_grid`, `biggest_swing` | Distance alignment, delta, where time is lost | engine | `laps.py` |
| `telemetry.py` — `gg_envelope`, `tyre_by_lap` | G-G envelope, per-lap tyre table | engine | `analysis/gg.py`, `analysis/tyres.py` |
| `telemetry.py` — mapping profiles, `decimate` | Saved mappings per logger set-up; peak-preserving thinning for plots | UI/reporting | Not ported; both belong to the app |
| `app.py` | Streamlit interface | UI/reporting | Not ported |
| `sample_data.py` | A second synthetic session generator | debt | Superseded by `synth.py` |

## What neither toolkit had

Added in this repository: one schema for both, a `Session` with raw / normalised
/ derived layers, resampling that keeps lap distance exact at the line, sector
times, a track map, the project store, golden datasets with frozen outputs.

Still missing after this sprint:

- A real logger export among the golden datasets.
- Binary logger formats (all import is CSV).
- The maths expression engine (derived channels are fixed functions).
- Reporting and plotting.
- Lateral g derived from position, which would give G-G analysis for files
  like the Monza session that log position but no accelerometer.
