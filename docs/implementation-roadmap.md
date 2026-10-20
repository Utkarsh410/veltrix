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

**From 2026-10-19 the plan is [development-plan.md](development-plan.md)**
(phases 4–20, set by the user). Start with its wave A (phase 4); the items
below are folded into it. Every phase of the brief has a working, tested slice,
and the user's improvement list P0–P3 is done and pushed.

**Settled by the user (do not raise again until they do):** the desktop app
works when used by hand; MoTeC `.ld` files run smoothly; real-car data, real
video footage and a logger's MF4 are not available yet and validation against
them waits until they are. Since 2026-10-16 the work is in git
(`origin` = github.com/Utkarsh410/veltrix, `main`); commit and push only when
the user asks.

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

## Throttle scaling on iRacing files — fixed 2026-10-16

Reported by the user after an independent parse of a real `.ibt` (Toyota GR86, Spa) confirmed speed, g,
steering, pressure, wheel-speed and shock conversions but found throttle wrong. Cause: iRacing stores pedals
0–1 in 32-bit floats, so a flat pedal reads 1.00000012; the importer dropped the "%" label and the engine only
scaled to % when the maximum was ≤ 1.0, so 25 samples just above 1 kept throttle (and brake) at 0–1. Every
coasting, full-throttle and throttle pick-up figure was wrong (coasting 147 s a lap → 7.4 s; full throttle
0% → 71%). Fix: `importers/iracing.py` scales every "%" float ×100 and keeps the label; `session._is_fraction`
allows 1e-3 above 1 for any other format. Tests: `test_iracing_ibt` (real full-scale value),
`test_a_pedal_logged_as_a_float32_fraction_is_still_read_as_a_fraction`.

## Report findings checked against an independent .ibt parse — fixed 2026-10-16

The user's independent parse of the Spa GR86 `.ibt` confirmed lap timing and corner placement but rejected
seven findings. Each was traced to its cause on the real file and fixed:

| Finding | Cause | Fix |
|---|---|---|
| T18 braking point ±45 m, "88 m later" on the fastest pass | braking phase clipped at the zone start (braking begins on the straight shared with T17), and the last brief re-press taken as the start | `corners._main_braking`: the phase that takes off the most speed, allowed to start before the zone (not before the previous corner's end); `_close_gaps` merges releases < `BRAKE_GAP_S` |
| T5 best 1.67 g | one irregular speed sample → spike in d(speed)/dt; measured `LongAccel` said 1.20 g | peak from measured `acc_long` when logged, held over `PEAK_G_SAMPLES` |
| ABS 100% | `abs_active` mapped to `dcABS`, the driver's ABS dial | iRacing hint `BrakeABSactive`; "dc"/dial/cut/pct excluded from ABS/TC; also fixed `tc_active` ← `dcTractionControl` and `tyre_temp_*` ← `Tire*_RumblePitch` / `dp*TireChange` |
| Static weight 41.5% | `VehicleParams` default quoted as the car's | `VehicleParams.source` (user / file / default); iRacing setup → mass and front weight (53.2%); defaults never quoted as fact |
| Front load-transfer 50% | share computed from default wheel rates | split withheld unless both wheel rates are stated; measured damper roll per g given instead; `--wheel-rate-front/-rear`, `--front-weight` |
| "Oversteer is the norm" hypothesis | residual against a model fitted to the same session read as balance | needs measured body slip ≥ `advice.HABIT_SLIP_DEG` at those exits (`body_slip` from `vel_x`/`vel_y`); balance-by-phase finding says so when slip contradicts |
| Off-tracks on laps 5 and 6 missed | iRacing incident channels not read | `incident_count` / `incident_flags`; "The simulator recorded N incidents" finding; incident passes never used as a corner's fastest pass |

Also: Monza golden test had enshrined the re-press bug (T1 lap 3 "52 m later"); rewritten. Tests:
`tests/test_report_checks.py`, extended `test_iracing_ibt`, `test_engineering`, `test_advice`.
Open: incident flag kinds other than 0x01/0x02 are shown as codes, not named; wheel rates still need entering
for a load-transfer split.

## Braking points measured from the corner — 2026-10-16

Asked for by the user: on track only the corner's marker boards are visible, so a braking point at "6528 m"
from the line is no use. `corner_metrics` now gives `turn_in_m` (one fixed point per corner: on the typical
lap, where the cornering signal — lateral g, else path curvature, else the steering estimate — starts its
climb to the corner's peak, searched between the corner's start and its apex) and `brake_before_m` =
turn-in − braking point. Report and driver tips quote `corners.before_corner()`: "117 m before the corner",
or "12 m into the corner" where braking starts after turn-in (trail-braking inside a complex, or a curved
approach such as Monza's Lesmo 1). `brake_m` stays for comparisons between laps. Open: the boards' own
reference point varies by circuit; a circuit file could carry board positions.

## Improvement list from the user (P0–P3) — P0 done 2026-10-16

The user supplied a prioritised list (P0 correctness and traceability, P1 driver analysis per corner, P2
lap-potential honesty, P3 event review) and chose "P0 first, report back before P1".

P0, done:
- **Original vs cleaned sample counts** — `Session.counts` (rows in file, skipped by reader, without time,
  repeated time, out of order, used, blanked per channel, analysed, own clock or resampled, derived samples
  bridged) and `Session.count_lines()`; shown in the report ("70,141 of 70,145 logged rows were used"),
  `veltrix inspect` and the HTML data tab ("Samples: as logged and as analysed").
- **Corner zones against circuit definitions** — `circuits.validate()`; when a circuit file fits, the inferred
  detection is run too and compared turn by turn (found, offsets, missing, merged, extra) →
  `corners.attrs["validation"]`; every corner row has `basis` ("circuit file" / "inferred"). Report finding
  "Corner zones are the circuit's own…" or "…inferred from the data, not checked against a circuit
  definition". Only Monza GP and Silverstone GP files exist; the Monza golden file is the 1966 full course
  (10,019 m) and is correctly refused.
- **Implausible acceleration peaks** — `analysis/plausibility.check_peaks()`: each corner pass's lateral and
  braking peak checked as held (0.25 s median), corroborated (yaw rate × speed; d(speed)/dt) and within a
  stated grip limit; verdicts kerb / bump / spike / jolt / unsupported / beyond. New `acc_lat_sustained`
  (corner `peak_lat_g` uses it) and iRacing `on_kerb` (irsdk_TrkSurf rumble 11–14). Unsupported/beyond peaks
  are removed from the peak figures. Spa: 30 of 228 peaks set aside (12 kerb strikes, 8 bumps, …).
- **Recorded vs inferred incidents** — `Event.status` (candidate / recorded / confirmed) and `Event.source`;
  `events.recorded_incidents()` and `confirm_with_records()` used by the app and the project store; report says
  "N balance-incident candidates inferred from the steering … — K confirmed by a simulator record"; HTML data
  tab has a Status column, records first.

P1, done (the user chose "table under the charts"):
- Engine: `corner_metrics` adds `brake_release_m`, `release_after_turn_in_m`, `apex_ref_m`,
  `throttle_after_apex_m` (NaN for a corner taken flat); `corners.pass_comparison()` (one lap against another,
  corner by corner, with `basis` and driver-readable words) and `corners.relative()`.
- App: `CompareView` corner table under the overlaid charts (splitter; keeps its share of the height; hidden
  with fewer than two laps; "Against" picks the comparison lap); a row click zooms the charts to the zone, puts
  the cursor on the apex and lights the zone on the map (`MapDock.set_corners` / `highlight`: zone-start ticks,
  apex labels, "T5 ?" for inferred). Corner analysis runs when a session opens (`MainWindow._corner_analysis`).
- HTML report: corner table shows the same fields lap → lap with a basis note; map draws zone ticks and "?"
  on inferred corners. Headline peaks are held values (`gg.gg_summary`: peak_*_g held 0.25 s, measured
  accelerometer for braking; *_instant_g beside) — the Spa tiles read 3.23 g / 1.74 g before.
- Key strips above charts are a fixed 18 px (`chart_layout.KEY_HEIGHT`).

P2, done:
- Lap potential is four separate findings: the laps as driven (tagged **measured**); the ideal lap by corner
  and the best sectors added up (each tagged **estimate**, "built from N different laps, not driven"); and
  the typical lap against each corner's best pass. Findings carry `nature` ("measured" / "estimate"), shown in
  text, Markdown and the HTML report.
- Which laps supplied the fastest passes: "Built from lap 1 (5 corners: …); lap 2 (…)"; per sector the lap
  that set it.
- Each corner's part of the gap: `corner_summary` adds `on_best_lap_s` and `gap_on_best_lap_s` (the best lap's
  zone time minus the fastest; they sum exactly to best lap − ideal); `laps.sector_breakdown()` does the same per
  sector. Shown in the finding, the HTML corner summary ("Best lap gives away", with share) and inspect.
- No composite is called a ceiling or a target: the estimate finding has no gain tag; the report summary's
  `gain_s` carries `gain_basis` ("estimate: …"); HTML tiles and inspect say "estimate, not driven".

P3, done:
- **Thresholds and confidence exposed** — every rule event carries `Event.thresholds` (`events.rule_limits`: the
  parameters its formulas use, directly or through helper channels; which were fitted to the session and from
  what; minimum duration, merge gap, severity bands, rule confidence); steering-model candidates carry the
  model's threshold, sigma level, duration and opposite-lock limit. Shown as "Found with" in the events panel
  and the HTML data tab (`events.limits_text`). The HTML data tab also says how confidence is decided and lists
  every analysis threshold (`thresholds.snapshot()`, now including `plausibility`).
- **Confirm / reject / note** — events panel buttons Confirm, Reject, Note…, Clear review, and a detail box
  (found with, evidence, review, status as detected). `MainWindow.review_event` / `clear_event_review`; author
  is the login name. Status `rejected` added; a person's confirmation never overrules a simulator record.
- **Original kept for audit** — `events.ReviewStore` (`<app data>/reviews/<sha256>.json`); each review keeps
  the event exactly as first detected (`original`) and earlier verdicts (`history`); `apply_reviews` restores
  the status as detected before applying, and returns reviews no event matches any more (rules changed) —
  shown in the app summary and the HTML report ("Earlier reviews of events no longer detected").
- Report.app / `veltrix report` read the reviews through `extras.apply`; the engineering report marks
  balance-incident candidates confirmed or rejected on review.
- Tests: `tests/test_event_review.py`.

All four priorities (P0–P3) of the user's improvement list are done.

## Wave A (phase 4, development plan) — what was built, 2026-10-19

- **CI**: `.github/workflows/tests.yml` — macOS 14, Python 3.12, `pip install -e ".[ui,all,dev]"`, offscreen
  Qt, full suite on every push and pull request. Checked by a fresh environment built from the declared
  dependencies only (387 passed, 5 skipped without the 264 MB file). Not yet run on GitHub.
- **Real-session regression**: `tests/real_sessions.py` (Spa `.ibt`, Hungaroring `.ld`, Nürburgring CSV),
  `tests/test_real_sessions.py` (marker `real`, `pytest -m real`, 10 s), `tools/freeze_real_sessions.py`
  (prints what changed). Files and `tests/real_expected/` ignored by git (the plan's default).
- **Module boundaries**: `tests/test_architecture.py` (engine ↛ app except `veltrix app`; analysis ↛
  report/CLI; ≤ 900 lines). `user_data.py` moved into the engine (data folder, detect_events, notes,
  reviews, saved rules, `apply`); `veltrix_app.events/extras/paths` re-export it. `analysis/engineering`
  is a package (base, potential_driver, braking_cornering, traction_tyres, vehicle_balance, incidents,
  setup, report). `thresholds.use` now reaches every loaded engine module's copy of a constant.
  `veltrix_app/session_charts.py` (SessionCharts mixin) takes the charts out of `window.py`.
- **Saved-file versions**: `versions.py` (`upgrade`, `refuse_overwrite`) in every reader and store
  (maths, rules, profiles, workspaces, thresholds, notes, reviews, video links, session manifest, project
  schema — which no longer rewrites a newer schema number). `tests/fixtures/saved_release_1/` from
  `tools/freeze_saved_files.py`; `tests/test_saved_files.py`.
- **Errors, log, crash reports**: `errors.py`; `read_file` wraps importer failures; `MappingError` for
  missing channels; `logs.py` (rotating log in the data folder, set up by the app and the CLI);
  `veltrix_app/crash.py` (local crash report, nothing sent).

## Wave B, phase 6 (maths engine) — what was built, 2026-10-19

- `maths/builtin.py`: `standard_set(vehicle)` — acc_long_calc, steering_rate, acc_lat_calc, yaw_rate_est,
  oversteer_metric, acc_lat_sustained, body_slip, llt_front/rear, lltd_front, grip_util as formulas with car
  parameters; `NOT_A_FORMULA` (acc_lat_rebuilt). Matches `derived.py` to ≤0.04% on evenly sampled logs (golden
  sessions; Spa `.ibt` exact); on multi-rate / uneven logs r ≥ 0.99 but differs in detail (Hungaroring `.ld`,
  Monza) because the engine works on the resampled view. Engine numbers stay authoritative for now.
- `maths/evaluate.py`: `atan2`, `hypot`, `when`, `rolling_median`, `since_change`; `lowpass`/`highpass`
  options `cap=1` (cutoff ≤ 0.4 × rate) and `uniform=1` (filter an uneven clock on an even grid).
- `MathSet.subset`, `MathSet.merge` (rename / replace / keep; dependants follow a rename; parameters never
  overwritten); `MathChannel.history`; format 2 with migration from 1; `MathSet.save` refuses a newer file.
- Editor: Built-in list (read-only, "Copy to my channels" with its car parameters), Import…, Export…; the
  window passes the session's car to it.
- Tests: `tests/test_maths_builtin.py`.

## Wave B, phase 5 (channel model) — what was built, 2026-10-19

- `cars.py`: `CarLibrary` (`<data>/cars/<key>.json`, versioned; match by name or alias ignoring case and
  punctuation; `apply` fills parameters not stated or read from the file, source "library").
  `VehicleParams.known/origin` treat "library" as the car's own. `build_session(..., cars=)`, notes say what
  came from the library or that the car is missing. CLI `_load` and the app's loader pass the data folder's
  library; `veltrix car list|show|set|remove`. Tests: `tests/test_cars.py`.
- Window status line: units assumed → "File ▸ Open with mapping… to confirm them; remembered for this
  logger" (the mapping dialog already saves typed units into the profile).
- Waiting on the user: circuit data (official numbers, names, boards), other simulators' files, a reference
  screenshot for an in-app car-settings screen; phase 7 views wait for references too.

## Waves C and D — what was built, 2026-10-19

- **Phase 12** `analysis/ab.py`: `compare_sessions(a, b, change)` — same circuit / car checks, setup changes from
  the files (`RawTable.setup`, iRacing `setup_sheet`; results of running such as last hot pressures are not
  settings), lap and per-corner verdicts (difference of means against the combined standard error: ≥ 2 real,
  1–2 probably, below no detectable change), corner metrics of B measured on A's corners; `to_markdown`;
  `veltrix compare A B --change`. Tests `tests/test_ab.py` (no change, +2% grip, too few laps, setup diff).
- **Phase 11** `analysis/coaching.py`: cards per corner (usual against the fastest clean pass, references from
  turn-in and apex, "Try" lines with the same limits as the driver tips, evidence, confidence), Markdown and
  an A4 page; `veltrix coach`. Tests `tests/test_coaching.py`.
- **Phase 10** `derived.py`: `slip_ratio_*`, `damper_vel_*_est` (only without the file's own `damper_vel_*`,
  which iRacing gives as `*shockVel`), `roll_front_deg`, `roll_rear_deg`, `pitch_deg`, `slip_angle_front_deg`,
  `slip_angle_rear_deg`; all also in `maths/builtin.py`. Vehicle-balance findings "In the corners the front
  tyres work harder…" (checked against the steering gradient) and "Damper speeds, roll and pitch". Tests
  `tests/test_dynamics.py` (Spa checks under `-m real`).
- **Phase 13** `report/documents.py` (debrief, driver-card, comparison; static A4; foot names the source
  files' SHA-256, no clock time), `report/pdf.py` (Qt WebEngine `printToPdf`, offscreen), specs
  (`make_spec`, `save_spec`, `load_spec`, `build`); `veltrix document`. Tests `tests/test_documents.py`.
- **Phase 8 (tooling)** `exports.py` (`export_session` → .csv / .parquet / .mf4); `veltrix export`. Tests
  `tests/test_exports.py`. No `.ld` writer (cannot be checked against MoTeC i2 here).
- Golden frozen outputs and real-session freezes updated: additions only (new channels; new balance findings).

## Phase 19 (performance) — what was built, 2026-10-20

- Import peak on the 264 MB reference CSV 2.3 → 1.2 GB, report peak 2.4 → 1.4 GB, model save +560 → +80 MB,
  model load +1.0 → +0.5 GB (load of all channels 42 → 150 ms: read column by column).
- `session._resample`: a log already on a steady clock keeps its own timestamps and gap-free arrays (no
  interpolation onto the same instants); the row filter and sort of the time base only run when needed.
- `importers/csv_importer._finish`: integer columns downcast to the smallest type holding them exactly;
  `maths._series` works such columns in floats so a formula cannot overflow.
- `Session.frame(columns)`; `corners._lap_frames`, `laps.lap_trace`, `handling`, `balance`, `gg`, `track_xy`
  and the report's G-G block pick rows from the columns they use only.
- `model_store`: Parquet written from Arrow arrays and read column by column (no pandas frame).
- `benchmarks/profile_session.py` shows the memory peak after each step; memory budget test in
  `tests/test_reference_session.py`. Golden outputs refrozen: rounding only, plus one tyre sample on the
  synthetic set that now falls inside its window. `pytest -m real` unchanged.

## Phase 15 (importers and batch) — what was built, 2026-10-20

- `importers/plugins.py`: `Importer` (key, name, extensions, read, sniff, version, api), `register`, `unregister`,
  entry points in the group `veltrix.importers` (loaded once; a broken one goes in `failed()`, never fatal),
  `VELTRIX_NO_PLUGINS`, `check_table` (the contract), `read_with`. Detection order: the formats that sign their
  files, then plug-ins, then name and text guesses. An extension shared with a built-in format needs a sniff.
  A plug-in's errors become FileFormatErrors that name the importer.
- `importers/testkit.py`: `check_importer` / `assert_importer` (registration, detection without claiming others'
  files, contract, determinism, session with laps, damaged copies refused within 30 s); `veltrix importer
  list|check`.
- `batch.py`: `find_files`, `run_batch` (report, engineer, .csv/.parquet/.mf4; duplicates by SHA-256 read once;
  `jobs` processes, which take the thresholds file and the parent's log), `write_index` (index.csv, index.md);
  `veltrix batch`.
- `examples/importer_plugin/`: the Example Logger `.elg` importer, a separate package with an entry point, using
  only `Importer` and `RawTable`. Tests: `tests/test_importer_plugins.py` (26), `tests/test_batch.py` (7).
- Docs: `docs/importers.md`; the user guide, ARCHITECTURE and README updated.

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
