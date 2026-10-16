# Architecture audit (Phase 0)

Audit of this repository against [BRIEF.md](BRIEF.md), made on 2026-10-12
**before any of the brief's phases were built**. It is the baseline, not the
current state: for that, see [implementation-roadmap.md](implementation-roadmap.md).
The older [AUDIT.md](AUDIT.md) covers the two toolkits this code was extracted
from; this file covers the code as it stands.

## Facts (verified in code or by running it)

| Item | Finding |
|---|---|
| Repository | Local git repo, no remote configured. Branch `analysis-report-and-advice`, two commits, a large uncommitted working tree (importers, workspace, engineering report). The brief names `gt3-performance-toolkit` on GitHub; that toolkit is a sibling folder and one of this repo's two ancestors, not this repo. |
| Language and stack | Python 3.12, NumPy, pandas, SciPy, PyArrow. Optional: asammdf, libxrk, h5py, python-can. No GUI toolkit. |
| Entry points | `veltrix` CLI (`cli.py`): `inspect`, `report`, `engineer`, `workspace`, `formats`, `circuit`. Python API: `load_session`, `ProjectStore`. `Veltrix Report.app`: an AppleScript launcher around `veltrix report`. |
| UI | No desktop UI. Two HTML surfaces: the self-contained session report (`report/template.html`, own SVG charts, shared cursor, lap comparison, track map) and the local workspace page served on 127.0.0.1 (`workspace/`). |
| Pipeline | `importers.read_file` → `RawTable` → `session.build_session` (map → convert units → check → laps → resample → derived) → `Session` → `corners`, `analysis/*`, `report`. |
| Data model | `Session` dataclass: `raw` (RawTable), `channels` (DataFrame, uniform rate), `derived` (DataFrame), `laps` (DataFrame), notes as `(level, text)`. No Channel, Stint, Event or Annotation entities. |
| Persistence | `store.py`: SQLite metadata (schema version 2, one `ALTER TABLE` migration), Parquet for normalised and derived channels, source file kept read-only by SHA-256, `engineering.json` per session. |
| Tests | 208 tests, all passing (`.venv/bin/python -m pytest`, 27 s). Golden outputs frozen in `datasets/expected/*.json`, checked to 1e-6. |
| Sample data | Three golden sessions in the repo (synthetic 5 laps, 20 Hz logger layout, Monza 5 laps). The brief's reference session is on this machine outside the repo (see below). |
| Packaging | None beyond the AppleScript launcher. No app bundle, signing or FFmpeg. |
| Machine | Apple M5, 16 GB, macOS 27.0.1. |

### Baseline on the reference session

`nurburgring-mercedes_amg_gt3-10-2026.04.08-16.26.51.csv`, 264 MB, read from
`~/Downloads` and never copied into the repo. It matches the brief's
description exactly.

| Measure | Value |
|---|---|
| Samples / columns / mapped | 951,694 / 57 / 33 |
| Rate, duration | 200 Hz, 79.3 min |
| Laps / stints / flying laps | 31 / 8 / 19 |
| Best lap | 122.330 s (lap 28) |
| Ideal lap by corner | 120.730 s (14 corners) |
| Theoretical best, 3 equal sectors | 121.950 s |
| Import | 2.14 s |
| Corner analysis | 0.27 s |
| Engineering report | 0.82 s |
| HTML report | 1.46 s, 624 KB |
| Peak memory | 3.3 GB |

The brief's "theoretical 120.730 s" is this engine's *ideal lap by corner*; the
three-sector figure is 121.950 s. They are different calculations and the
report shows both.

## Features named in the brief, classified

| Feature | Class | Where |
|---|---|---|
| Ingestion (CSV) | implemented | `importers/csv_importer.py` |
| Ingestion (MF4, MAT, VBOX, MoTeC, iRacing, AiM, CAN + DBC) | partial | `importers/*` — tested against written or library-made files; only CSV and one AiM file are real |
| Channel mapping | implemented | `mapping.py`, `schema.py`; no saved profiles, no mapping UI |
| Beacon lap detection | implemented | `laps.py` (also lap channel, timer, distance resets, gate) |
| Lap and stint analysis | implemented | `laps.py`, `session._stints` |
| Best and theoretical lap | implemented | `laps.py`, `corners.py` |
| Corner-by-corner and time loss | implemented | `corners.py` |
| Driver inputs | partial | coasting, overlap, braking points in `analysis/engineering.py`; no dedicated module |
| Vehicle dynamics | implemented | `derived.py`, `analysis/balance.py`, `analysis/gg.py`, `models/` |
| Data quality: quantisation | implemented | `session._resolution_check` |
| Data quality: speed glitches | partial | glitch blanking covers accelerations and yaw rate, not speed |
| Data quality: mislabelled channels | implemented | `session._accel_unit_check`, sign checks |
| Reconstructed long / lat acceleration | implemented | `derived.py` |
| Incident detection | implemented | `analysis/handling.py` |
| Coaching | implemented | `analysis/advice.py`, `analysis/engineering.py` |
| Setup hypotheses | implemented | same |
| Report generation | implemented | `report/` (HTML), `engineering.to_markdown` |
| Maths-channel engine | missing | derived channels are fixed functions |
| Interactive desktop workspace | missing | the HTML report has charts and a cursor but is not an editor |
| Mapping profiles, mapping UI | missing | |
| Synchronised video | missing | |
| Configurable event engine | missing | incidents are one fixed detector |
| Annotations, saved workspaces | missing | |

## Against the hard rules

| Rule | State | Gap |
|---|---|---|
| 1 Layers and provenance | partial | raw / normalised / derived exist, but provenance is free-text notes, not per channel; no corrected layer distinct from calculated |
| 2 Output labels | missing | nothing carries measured / reconstructed / calculated / event / interpretation / hypothesis as data |
| 3 Explicit units | partial | units come from the file; where missing the engine assumes one and warns (`Speed … assumed km/h`), which is inference from a name |
| 4 Native timestamps and rates | **violated** | every channel is resampled to one "standard" rate on import; multi-rate formats are laid on one table by the importer |
| 5 Security | holds | no `eval`/`exec`; upload names are sanitised; server bound to loopback. No formula language yet |
| 6 UI-independent engine | holds | |
| 7 Graceful degradation | mostly | sections say "not assessed"; no background tasks, progress or cancel |
| 8 Regression protection | holds | golden outputs at 1e-6 |
| 9 Honesty | holds | README states what is untested |
| 10 Local only | holds | optional deps checked: asammdf (LGPL-3.0), libxrk (MIT), h5py (BSD), python-can (LGPL-3.0); all have arm64 wheels |
| 11 Configurable thresholds | **violated** | thresholds are module constants |
| 12 Correctness before polish | holds | |

## Reuse, refactor, replace

**Reuse as is:** importers, `mapping`, `schema`, `laps`, `corners`,
`analysis/*`, `models/*`, `track`, `circuits`, the HTML report, the store's
layout, the golden datasets.

**Refactor:** `session.build_session` (feed a canonical model; make resampling
an explicit, recorded alignment step rather than the model itself); `store`
(persist the model's metadata and native arrays); thresholds (move into a
config object); `derived.py` (register its outputs as calculated or
reconstructed channels with provenance).

**Replace:** nothing wholesale.

## Risks

1. **Rule 4 against "same results".** Analysis is built on a uniform frame.
   The model must hold native data and expose the uniform frame as a recorded
   view, or every calculation changes at once.
2. **Memory.** 3.3 GB peak on the reference session leaves no room for a
   second copy of the data on a 16 GB machine. The model must share arrays.
3. **No desktop UI exists.** Phase 3 starts from nothing; the framework choice
   needs the benchmark the brief asks for.
4. **Unverified importers.** MoTeC `.ld` and iRacing `.ibt` have not read a
   real file.
5. **Unit assumptions.** Tightening rule 3 changes outputs for files without
   units; it must go through the mapping profile work, not silently.

## Assumptions (not verified)

- The reference session is the same file earlier reports were made from; it
  reproduces their headline numbers.
- "Existing saved projects" means `ProjectStore` folders at schema version 1
  or 2; none were found on this machine outside test runs.
