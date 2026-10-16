# Implementation roadmap

Project memory for the build in [BRIEF.md](BRIEF.md). **Read this first, update
it after every slice, and continue from "Resume here".** Never mark a phase
done before its "done when" holds and its tests pass.

## Status

| Phase | State | Evidence |
|---|---|---|
| 0 Audit | done 2026-10-12 | [architecture-audit.md](architecture-audit.md), [testing-strategy.md](testing-strategy.md), [data-model.md](data-model.md); baseline 208 tests passing |
| 1 Canonical session model | done 2026-10-12 | `model.py`, `model_build.py`, `model_store.py`; 226 tests passing; reference session unchanged (best 122.330 s, ideal 120.730 s) |
| 2 Maths-channel engine | done: engine 2026-10-12, full editor window 2026-10-13 (channel browser, function reference, live validation with error position, preview plot, parameters, dependency and provenance view, persistence). Maths channels in command-line reports still pending | `maths/` (parser, units, evaluate, engine), [maths-channels.md](maths-channels.md); 269 tests passing; seven fixtures on the reference session in 53 ms |
| 3 Interactive workspace | "done when" walk-through passes on the reference session, offscreen (2026-10-12); **outstanding items listed below; never used on a real display** | `veltrix_app/`, `tests/test_app_*.py`, walkthrough in `tests/test_reference_session.py`; 296 tests passing |
| 4 MF4/MDF import | "done when" met on generated files (2026-10-12): opens, maps through a profile, analyses, reopens the same from the store; damaged files fail with a reason; large-file benchmark recorded. **No file from a real logger has been tested** | [mdf-compatibility.md](mdf-compatibility.md), `importers/mdf.py`, `mapping_profiles.py`, `veltrix_app/mapping_dialog.py`; 308 tests passing |
| 5 Synchronised video | "done when" met with a synthetic video (2026-10-12): offset persists, playback drives the cursor and the cursor seeks the video, sync status and residuals shown, app works with no video or a missing file. **No real camera footage tested** | `veltrix_app/video_sync.py`, `video_dock.py`, `tests/test_video_sync.py`; 316 tests passing |
| 6 Events, reports, hardening | built 2026-10-12: configurable event engine, hypothesis-structured and evidence-gated setup advice, report data/events/limits tab, four end-to-end paths, profiling, configurable thresholds, user guide and technical reference. **Packaging is a documented route only; nothing is validated on real-car data or a real display** | `events.py`, `thresholds.py`, `tests/test_events.py`, `tests/test_end_to_end.py`, [performance.md](performance.md), [packaging.md](packaging.md), [user-guide.md](user-guide.md), [technical-reference.md](technical-reference.md); 332 tests passing |

## Resume here

Every phase of the brief has a working, tested slice. **The roadmap is not
complete.** What remains, in the order worth doing it; start at the top.

**Settled by the user on 2026-10-13 (do not raise again until they do):**
the desktop app works when used by hand; MoTeC `.ld` files run smoothly;
real-car data, real video footage and a logger's MF4 are not available yet and
validation against them waits until they are; no git commit is planned.

**Next slices (no input needed):**

1. Packaging: build, sign and notarise a real app bundle following
   [packaging.md](packaging.md). `Veltrix Data.app` and `Veltrix Report.app`
   are launchers tied to this folder.
2. Smaller leftovers: workspace file to hold lap rules and filters; the rule
   editor's "Found" column is off to the right on a narrow window; decide
   whether the older "What to work on" report tab stays; the import still
   peaks at 2.4 GB on the reference CSV (a further cut means sharing the
   analysis view with the normalised arrays when nothing is resampled).
3. Engine debt that remains: derived channels of a log with no steady clock
   are still computed on a resampled view (the calculation needs a uniform
   rate; it is recorded as resampled); units a file does not state are still
   assumed for analysis (flagged); `thresholds.use()` does not reach constants
   used as default arguments; the oversteer event rule finds nothing on the
   two real sessions once fitted, because its metric depends on a steering
   ratio and wheelbase that are configured, not known.

**Waiting on data (when the user has it):** an `.ldx` beside a real `.ld`; an
iRacing `.ibt`; a logger's MF4; onboard video with a known sync moment;
real-car data for thresholds, the tyre window and recommendations.

## Phase 1 — what was built

- `model.py`: `SessionModel`, `SourceFile`, `Channel`, `Lap`, `Stint`, `Event`,
  `Annotation`, `View`; four layers; labels; provenance; per-channel lookup
  policy (`Channel.at`, `SessionModel.align`).
- `model_build.py`: the import pipeline now builds the model; `Session` is a
  view of it (`Session.model`).
- `model_store.py` and `store.py`: `model/manifest.json` + Parquet groups per
  shared clock; `ProjectStore.model()`; older projects get a model on demand.
- Multi-rate importers keep each signal's own samples (`RawTable.signals`), so
  the model holds native rates.
- Engineering findings carry a `label` (interpretation / detected event /
  hypothesis/recommendation).
- Tests: `tests/test_model.py` (15), `tests/test_reference_session.py` (3).

Decisions:

1. The uniform analysis frame stays, as a recorded *view* of the model, rather
   than rewriting every calculation for native timestamps at once. Results are
   identical to before (golden outputs at 1e-6).
2. Raw arrays are not stored twice: the kept source file is the raw layer's
   storage.
3. Unit assumptions are not removed in this phase (that would change results);
   they are made visible per channel (`unit_source`, quality flag).

## Phase 2 — what was built

- `maths/parser.py`: own tokenizer and recursive-descent parser to seven node
  types; limits on length, terms and nesting; errors carry a position.
- `maths/units.py`: units as scale + dimensions; dimensional checks;
  conversions; unknown units stay unknown.
- `maths/evaluate.py`: every value is a series with a validity mask; 20
  functions; gap-aware time functions; mixed rates meet on the faster clock.
- `maths/__init__.py`: `MathSet` (definitions + named parameters, JSON),
  `order` (topological, cycle report), `compute` (per-channel errors, cache,
  versions, provenance), `check` (live validation), `on_view`.
- `ProjectStore.maths()` / `save_maths()`.
- Tests: `tests/test_maths.py` (43), one on the reference session.

Decisions: `in` converts and `as` states a unit, so the brief's
`derivative(Speed in m/s) / 9.80665` is written `(...) as g` (or simply
`derivative(Speed in m/s) in g`); a source-column name in a formula means the
normalised channel made from it, `[raw:Name]` the column itself; `%` is a
label, not a factor of 0.01.

## Batch of 2026-10-13 — what was built

Engine
- **Steady-clock logs are analysed on their own timestamps.** A file logged at
  a steady rate (60 Hz, say) is no longer resampled to a "standard" one; the
  view is the logger's samples and derived channels say so in provenance.
  Only a log with no single rate is resampled, and says that.
- **CSV import reads rows straight from the file** instead of holding the
  whole text: 3.2 GB → 2.4 GB peak on the reference session, results identical.
- **Event thresholds fitted to the session** (`events.calibrate`,
  `detect(fit=True)`): a threshold is raised to the session's own noise floor
  or percentile, never lowered; used by the app and the store. Reference
  session 2,354 → 541 candidates; Hungaroring 2,358 → 792.
- **Speed dropouts and spikes are blanked at import** (judged against the
  samples either side), with a warning; the raw layer keeps the file's value.
- **Mapping decisions are kept with the session** (`model.mapping`, in the
  manifest); `mapping_profiles.best_profile` picks the saved profile that fits.

App
- Notes on the track map; "From" and "Mapped by" in the channel browser;
  a channel picker for MDF files before mapping; the fitting profile applied
  automatically in the mapping dialog; analysis after mapping on the loader
  thread; an event-rule editor (Events ▸ Event rules…, with try, fit, add,
  remove, defaults); undo and redo of saves, deletes and parameter changes in
  the maths editor; File ▸ Make report….
- `veltrix_app/paths.py` and `extras.py` (no Qt): one data folder for maths
  channels, event rules, profiles, notes and video links.

Reports
- A report made from the app, or by `veltrix report`, includes the app's maths
  channels, the events found with the saved rules, and the notes for that file
  (`--no-app-data` leaves them out).

Video
- Every linked camera shown side by side, each on its own sync; the selected
  one drives the cursor, the others are held to it; piecewise sync through the
  anchors, with leave-one-out uncertainty.

Behaviour that changed (hard rule 8): a steady non-standard-rate log now keeps
its own rate (new algorithm; no golden output changed); speed dropouts are now
blanked at import, so the speed-glitch event rule no longer reports single
dropped samples — the import warning does (new behaviour).

## Since the phase reports — what was built

- **Real MoTeC `.ld` (Hungaroring, ACC):** the header and channel layout are
  confirmed on a real file. Laps are now taken from a lap timer channel when
  there is no `.ldx` (`importers/table.lap_timer_crossings`), and `..` unit
  placeholders are read as "no unit".
- **Launcher and layout:** `Veltrix Data.app` opens the desktop app without
  Terminal; a menu bar (File, Charts, Maths, View); side panels as tabs so the
  charts get the room. Checked in screenshots from the real display.
- **Range selection:** Charts ▸ Mark a range (⌘R) puts a draggable range on
  every chart; the "Marked range" panel gives each charted channel's min, max,
  mean, standard deviation, values at the edges and change, from every logged
  sample; zoom to it; saved in the workspace.
- **A y-axis per unit:** the first unit on the left, each further unit on its
  own axis on the right with its own scale; chart areas line up across the
  stack whatever axes each has.

## Maths editor (Phase 2's UI) — what was built

- Engine: `maths.preview` (compute a formula without keeping it, leaving the
  session untouched), `maths.dependencies` (uses / used by / missing /
  parameters / order), `maths.check` now reports the position of unit and
  evaluation errors too; powers of units are named readably (`(km/h)^2`).
- `veltrix_app/maths_editor.py`: the editor window; `maths_dock.py` is now the
  list and the way in. Rename and delete are refused while a channel is used;
  a deleted channel leaves the charts.
- Tests: `tests/test_maths.py` (+3), editor walk-through in
  `tests/test_app_workspace.py`; a screenshot was inspected once.

## Phase 3 — what was built

- [ui-framework-decision.md](ui-framework-decision.md): PySide6 + pyqtgraph,
  from a benchmark on the reference session. The web alternative could not be
  benchmarked on this machine; that is stated in the decision.
- `display.py`: min/max-per-pixel decimation; drawing only, never readouts.
- `veltrix_app/workspace.py` (no Qt): charts, channels, view, cursor,
  `series()`, `readout()` marking interpolated and held values, versioned
  save/load that keeps the layout and lists missing channels.
- `veltrix_app/compare.py` (no Qt): laps overlaid by distance or time, delta
  time against a reference (agrees with `laps.compare_laps`), per-lap readout.
- `veltrix_app/trackmap.py` (no Qt): map from logged or rebuilt position
  (labelled which), cursor position, colour by channel, click → time.
- `veltrix_app/events.py` (no Qt): detector output as model events worded as
  candidates; annotations kept apart, saved per source file by SHA-256.
- Qt: `window.py` (browser, stacked charts, shared cursor, readout, tabs,
  background loading with cancel), `compare_view.py`, `maths_dock.py` (the
  Phase 2 editor: live validation, unit, function reference, provenance,
  saved and reloaded), `map_dock.py`, `events_dock.py`.
- Walk-through on the reference session, offscreen, slowest step 1.3 s
  (showing the session); zoom 0.03 s, cursor 0.14 s, lap comparison 0.08 s.
- Bug found and fixed on the way: event times were estimated as a share of
  the lap; they are now read from the lap-distance channel.

## Phase 4 — what was built

- [mdf-compatibility.md](mdf-compatibility.md): asammdf evaluated (versions,
  licence, arm64, dependencies, memory) and kept.
- `importers/mdf.py`: path-based lazy opening; `inspect_mdf`; `parse_mdf` with
  `channels`, `progress`, `cancel`; handle always closed; damaged files fail
  with the file's name and a reason. `read_file` and `load_session` pass those
  through, and hash large MDF files by streaming instead of reading them whole.
- `importers.inspect_file`: import diagnostics for any format.
- `mapping_profiles.py`: `Profile` / `Rule` (JSON), `decide` in the brief's
  order with a value-plausibility check (a name alone never maps a column),
  conflicts, `diagnostics`, `to_import_args`, `profile_from_decisions`.
- `store.py`: `import.json` per session (stated units, sign flips, channels
  loaded, profile used) so a mapped import is rebuilt identically.
- `veltrix_app/mapping_dialog.py`: per-column decision with basis, confidence,
  unit and problems; change channel, type a unit, confirm; load and save
  profiles; "Open with mapping…" in the window; a progress bar for lazy
  formats, with cancel.
- Benchmark on a 79-minute, 56-channel, three-rate MF4 generated from the
  reference session: inspect 0.10 s, five channels 0.53 s, full import 4.5 s.

## Phase 5 — what was built

- Stack: Qt Multimedia (bundles FFmpeg 9, LGPL-2.1+ build) with `QVideoWidget`.
  No separate FFmpeg install is needed to play or, in tests, to encode.
- `veltrix_app/video_sync.py` (no Qt): `VideoLink` with the documented
  convention `telemetry_time = rate · video_time + offset`; manual offset;
  anchors with a least-squares fit that stays offset-only unless at least
  three anchors over a minute or more need a clock-rate term; residuals;
  status text that never calls a manual or single-anchor sync validated and
  keeps frame interval apart from sync uncertainty; `VideoStore` per source
  file; relink.
- `veltrix_app/video_dock.py`: player, play/pause, slider, offset field,
  "anchor: this frame is the cursor", anchor table with residuals, status,
  add / relink / remove, camera chooser. Cursor → video seeks are throttled
  (60 ms) and do not echo back; playback → cursor moves charts, map, readout.
- Tests: sync maths (6) and the dock with real playback of a video the test
  encodes itself (2).

## Phase 6 — what was built

- `events.py`: rules as data (on / off formulas for hysteresis, minimum
  duration, merge gap, severity grading, evidence channels, `fine_inputs`
  guard, confidence, version), helper maths channels, named parameters, JSON
  rule sets; events never run through gaps or invalid samples; rules with
  missing inputs are reported, not fatal. Default rules for wheelspin, front
  lock-up, traction loss, oversteer, throttle-while-braking, steering
  correction and speed-sensor faults. Stored with each session
  (`event_rules.json` per project); shown in the app beside the older
  balance-incident detector.
- Reports: every engineering finding also carries `interpretation`,
  `hypothesis` and `investigation`; setup items are hypotheses, and a setup
  change is suggested only when confidence is better than low. HTML report
  gains "Data, events and limits": mapping with unit source, maths channels,
  events, notes, limitations.
- `thresholds.py`: every analysis threshold listed, overridable for a run
  (`--thresholds`), a block of code or a project (`thresholds.json`).
- `tests/test_end_to_end.py`: CSV → model → maths → lap compare → events →
  report; MDF → mapping → model → workspace → report; project and workspace
  reopen. Session → video → sync → seek is in `tests/test_video_sync.py`.
- `benchmarks/profile_session.py` and [performance.md](performance.md).
- [packaging.md](packaging.md) (route only), [user-guide.md](user-guide.md),
  [technical-reference.md](technical-reference.md).

Behaviour that changed, and why (hard rule 8):
- Setup recommendations on low-confidence evidence no longer list setup
  directions — new rule from the brief ("no setup-change advice without
  adequate evidence").
- Event times from the balance detector are read from the lap-distance
  channel instead of estimated from lap fraction — bug fix.
- `lap` no longer maps to columns named like "Prev Lap Diff" — bug fix found
  on a real AiM file.
No golden output changed in any phase.

## Lap library (laps across a folder) — removed

Built on 2026-10-13 at the user's request (a list of all laps in a folder, any number compared), reworked through
four layouts, then **removed completely at the user's request** the same day. Nothing of it remains in the code
(`veltrix_app/library.py`, `library_view.py`, `tests/test_app_library.py` and the toolbar/menu entry are gone).
Do not rebuild it unless asked.

## "Measures and laps" panel (lap selection on the left) — what was built

Asked for on 2026-10-13, from a reference screenshot of AiM Race Studio's "Measures and laps" panel: remove the
Lap comparison tab and build the lap selection on the left of the main window.

- `veltrix_app/measures_dock.py` — `MeasuresPanel` in the left dock (`MainWindow.browser_dock`, titled "Measures
  and laps"). Tabs: **Measures** ("Tests loaded" with X / colour box / lap / time / gap, then one button per
  channel, pressed in = drawn, with the value(s) at the cursor beside it), **Laps** (click a lap to load or
  unload it), **Channel details** (the former channel browser table, unchanged).
- `MainWindow`: the central tab bar is hidden; page 0 (session against time) shows when no lap is loaded, page 1
  (`CompareView`, loaded laps overlaid + delta) when any is. `load_laps`, `toggle_lap`, `make_reference`,
  `set_measure`; the lap cursor drives the session cursor at the reference lap's time so map/video/readout follow.
- `CompareView` lost its own lap chooser and values table; it starts with no laps and emits `lapsChanged` /
  `cursorMoved`.
- Tests: `tests/test_app_compare.py::test_laps_are_loaded_from_the_panel_on_the_left`.
- Not in it (differences from the reference): values only for drawn channels; no colour-by-value legend, no
  "'Per Lap' Color", no User Profiles tab; one session at a time.

## Open items carried forward

| Item | Rule | Planned in |
|---|---|---|
| Thresholds overridable through `thresholds.use` rather than passed as arguments; defaults-as-arguments are not reached | 11 | engine debt |
| Units assumed when the file gives none | 3 | Phase 4 (mapping profiles, user confirmation) |
| Derived channels computed on the analysis view, not native timestamps | 4 | Phase 2 (re-express them as maths channels) |
| Speed glitches are an event rule, not yet part of the import's glitch blanking | — | Phase 6 leftovers |
| MoTeC `.ld` and iRacing `.ibt` unverified on real files | 9 | when a real file is available |
| Background loading with cancel exists; progress only for MDF; analysis after mapping runs on the UI thread | 7 | Phase 3 leftovers |
| (done) events are stored with each session | — | — |
| Peak memory 3.1 GB on the reference session (CSV parse) | — | Phase 4/6 profiling |
| Nothing is committed to git; no remote | — | user's decision |

## Phase plan (from the brief, with this repo's starting point)

- **Phase 2** engine first, editor with the Phase 3 UI.
- **Phase 3** framework decision needs a plotting benchmark on the reference
  session: PySide6 + pyqtgraph against the existing web page + a canvas/WebGL
  plot in a desktop wrapper. The existing HTML report and local workspace
  server are the "what exists" to prefer if they hold up.
- **Phase 4** build on `importers/mdf.py`: metadata-first inspection, lazy
  channel loading, mapping profiles, diagnostics, large-file benchmark.
- **Phase 5** follows the Phase 3 framework.
- **Phase 6** event engine on maths channels; report sections restructured as
  Evidence → Interpretation → Hypothesis → Suggested investigation (the
  engineering report's Evidence → Calculation → Confidence → Recommendation is
  the starting point); end-to-end tests; profiling; packaging notes; user guide.
