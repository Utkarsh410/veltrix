# VELTRIX DATA — Claude Code Build Brief

## 1. Role and goal
You are a senior Python/desktop engineer with motorsport data-engineering expertise. Extend the existing repo (https://github.com/Utkarsh410/gt3-performance-toolkit.git) into Veltrix Data: a macOS (Apple Silicon) desktop telemetry analysis tool in the class of MoTeC i2, WinTAX and Pi Toolbox.
This is a brownfield project. Reuse and extend existing code. Do not rewrite wholesale or switch stack without a written justification.

## 2. Hard rules (apply to every phase)
1. Never overwrite source data. Keep raw → normalised → corrected/reconstructed → calculated as separate layers, each with provenance (inputs, formula/algorithm, units, filter/resample settings, version).
2. Label every output as one of: measured, corrected/reconstructed, calculated, detected event, interpretation, or hypothesis/recommendation. Never present an estimate as a measurement.
3. Units are explicit. Unknown units stay "unknown". Never infer units or physical meaning from a channel name alone.
4. Preserve native timestamps and per-channel sample rates. Align or resample only when a calculation needs it, using a documented per-signal policy (interpolate continuous signals; previous/nearest for discrete). No blanket 200 Hz resampling. Never differentiate across gaps or resets.
5. Security: no eval/exec or arbitrary code from formulas or files. Treat imported files as untrusted; block path traversal; handle temp files safely; keep session data out of logs; cap formula complexity.
6. The analysis engine is UI-independent and reusable by the UI, reports, tests and batch jobs.
7. Degrade gracefully: missing channels, bad samples, unsupported metadata, missing video or failed calculations produce a clear message, never a silent failure, and the rest of the app keeps working. UI never freezes; long tasks run in the background with progress and cancel.
8. Protect existing behaviour: add regression tests before touching existing calculations. If a result changes, state whether it is a bug fix, new algorithm, new assumption or new input interpretation.
9. Honesty: a feature exists only when its execution path works and its tests pass. Never claim tests ran that did not, formats are supported without testing, or invent benchmarks. Never document unbuilt features as available.
10. Local processing only; no cloud or uploads without explicit approval. Before adding a dependency, check licence, macOS/Apple Silicon support and conflicts.
11. All thresholds and example numbers in this brief are illustrative. Make thresholds configurable; never hardcode example results.
12. Numerical correctness and traceability come before visual polish.

## 3. Context
Reference dataset (use as the performance target if present): ~951,694 samples, 57 source channels (33 mapped), 200 Hz nominal, ~79 min, 31 laps, 8 stints, 7 pit stops. An example report showed best lap 122.330 s vs theoretical 120.730 s.
Previous reports describe: ingestion, channel mapping, beacon lap detection, lap/stint analysis, best/theoretical lap, corner-by-corner and time-loss analysis, driver inputs, vehicle dynamics, data-quality checks (quantisation, speed glitches, mislabelled channels), reconstructed long/lat acceleration, incident detection, coaching, setup hypotheses and report generation.
Verify each in code during the audit and classify it as implemented, partial, report-only or missing. Keep existing features even if they are outside the phases below.

## 4. Phase 0 — Audit (do first)
- Inspect: structure, entry points, UI framework, pipeline (import → analysis → report), data models, persistence, dependencies, tests, sample data, git branch, macOS packaging.
- Run the test suite and record the baseline.
- Write (or fit into the existing docs layout): docs/architecture-audit.md (facts vs assumptions, reuse/refactor list, gaps, risks), docs/implementation-roadmap.md, docs/data-model.md, docs/testing-strategy.md.
- Then continue straight into Phase 1. Do not stop at a plan.

## 5. Phase 1 — Canonical session model
- One model that CSV, MF4/MDF and future formats all feed; everything else consumes only this model. Entities: Session, SourceFile, Channel, Lap, Stint, Event, Annotation.
- Channel fields: id, display name, source name, description, category (configurable), quantity, unit, dtype, timestamps, sample rate, source/derived status, formula + dependencies, validity mask, quality flags, provenance, version, user metadata.
- Storage: keep metadata separate from large numeric arrays (no per-sample DB rows); cache derived data; use schema migrations if a DB exists; do not break existing saved projects.
- Route the existing CSV pipeline through the model, with regression tests on lap detection, best/theoretical lap, stints, corners, data quality, incidents and reports.
- Done when: the existing CSV workflow and reports give the same results through the new model.

## 6. Phase 2 — Maths-channel engine
- Safe expression language parsed into a restricted AST. Allowed: + - * / ^ ( ), comparisons, and/or/not, if(cond, a, b), channel references. Reject imports, attribute access, unknown calls, I/O, state mutation and excessive complexity. Give clear error messages.
- Functions: abs, sqrt, min, max, clip, log, exp, sin, cos, tan, derivative, integrate, moving_average, lowpass, highpass (type, cutoff, order, causal vs zero-phase made explicit), rolling_min/max/std, peak_detect. Handle NaN/inf, gaps, resets, edge effects and constant signals. Never smooth silently. Follow existing repo naming if any.
- Unit-aware with dimensional checks (speed + temperature fails). Conversions: km/h↔m/s, deg↔rad, s↔ms, m/s²↔g, rad/s↔deg/s.
- Dependency graph: topological order, cycle rejection, missing-input reporting, cache invalidation, versioned formulas, reproducible recalculation.
- Every result returns values plus a validity mask.
- Editor UI: name, description, category, unit, formula, channel browser, function reference, live validation, preview plot, dependency/provenance view. Persist across restarts.
- Calculated channels must be usable in plots, lap comparison, events and reports.
- Test fixtures:
  RearWheelSpeed = (WheelSpeed_RL + WheelSpeed_RR) / 2
  SteeringRate = derivative(Steering_Angle)
  CombinedG = sqrt(G_Lat^2 + G_Long^2)
  HighThrottle = if(Throttle > 90, 1, 0)
  LongG_fromSpeed = derivative(Speed in m/s) / 9.80665 (real timestamps, optional filter)
  RearSlipEstimate = (RearWheelSpeed - VehicleSpeed) / VehicleSpeed * 100 (guard divide-by-zero; label as estimate)
  RearWheelspinCandidate = Throttle > 70 and RearSlipEstimate > 8 (configurable thresholds)
- Done when: all fixtures pass; syntax, unit, missing-input, divide-by-zero and cycle errors are clear; mixed sample rates follow the policy; saved formulas reopen; performance is benchmarked on the large session.

## 7. Phase 3 — Interactive workspace
- Choose the UI framework after the audit (prefer what exists; otherwise compare PySide6/Qt with a web frontend + desktop wrapper). Benchmark plotting libraries on the large session before committing. Existing reports stay; the workspace complements them.
- Charts: multiple stacked charts; add/remove charts and channels from what is available; zoom, pan, range select, value readout.
- Shared cursor syncing all charts, readouts, track map, event panel and video. Between samples, use the channel's lookup policy and mark interpolated values.
- Lap comparison: best, previous, reference or multiple laps; time or distance alignment with the mode always visible; overlays and delta-time.
- Track map from the best available position data, coloured by any channel; click to move the cursor. If position data is unreliable, say so; never invent coordinates.
- Channel browser: search, category, unit, source vs normalised name, calculated flag, quality warnings, sample rate, mapping details.
- Saved workspaces (versioned): layout, channels, axes, lap rules, alignment, filters, annotations. If channels are missing, keep the workspace and list what is unavailable.
- Annotations (session/lap/time/position, category, author, notes) shown on charts and map, includable in reports, kept separate from auto-detected events.
- Performance: NumPy arrays, no needless copies, extrema-preserving display downsampling with more detail on zoom; calculations and readouts always use full-resolution data.
- Done when: load → browse → synced charts → overlay laps (time/distance) → plot a maths channel → map sync → inspect events → save/reopen workspace all work and stay responsive on the large session, and existing reports still work.

## 8. Phase 4 — MF4/MDF import
- Evaluate asammdf (https://github.com/danielhrisca/asammdf): supported MDF versions, licence, Python/Apple Silicon support, memory use, dependency conflicts. Record the result in docs and justify any alternative.
- Pipeline: validate format → metadata → channel discovery → timestamps/rates → units/conversions → mapping → canonical model → existing analysis. Do not duplicate analysis logic.
- Preserve native channel groups, rates and timestamps. Handle irregular, missing or discontinuous data and corrupt files.
- Mapping UI + reusable profiles (per logger, car, series, team): source patterns → canonical concept, expected unit, conversion, validation rules, confidence, user-confirmed flag, overrides. Decision order: explicit metadata > verified profile > signal plausibility checks > user confirmation > unmapped.
- Metadata-first inspection, selective/lazy channel loading, progress, cancel, resource cleanup.
- Import diagnostics: format/version, channel and group counts, rates, duration, missing metadata, unknown units, mapping conflicts, unsupported features.
- Done when: a documented-supported file opens, maps, analyses and reopens reproducibly; corrupt or unsupported files fail with useful messages; a large-file benchmark is recorded.

## 9. Phase 5 — Synchronised video
- Choose the stack after the audit (Qt Multimedia for PySide6, HTML5 video for web, ffprobe for metadata); check licensing and packaging.
- Associate one or more videos per session. Store path, camera id, sync method, offset, anchors, drift parameters, validation status and metadata. Allow relinking moved files.
- Manual offset first: telemetry_time = video_time + offset (document and test the sign convention).
- Anchors (brake onset, gear change, line crossing, etc.): fit telemetry_time = a · video_time + b by least squares; use the simplest model the evidence supports; show residuals and warn when anchors disagree. Piecewise sync is a later feature.
- Two-way navigation: video ↔ cursor ↔ map/events, with throttled seeking.
- Show sync uncertainty or "not validated". A frame interval (e.g. 33.3 ms at 30 fps) is not the same as sync accuracy.
- Done when: the offset persists; playback drives the cursor and vice versa; sync errors are visible; the app works with no video or a missing file.

## 10. Phase 6 — Events, reports and hardening
- Event engine on recorded and maths channels: configurable rules with hysteresis, minimum duration and validity checks, using multiple channels where appropriate. Record start/end, lap, position, severity, channels, evidence, confidence and rule version. Classes: wheelspin, lock-up, traction-loss and oversteer candidates, braking anomalies, steering corrections, sensor/quality issues. Use cautious wording, e.g. "Possible traction-loss event requiring review."
- Reports: extend the existing pipeline with mapping decisions, maths channels, events, plots, map/video references and limitations. Structure each recommendation as Evidence → Interpretation → Hypothesis → Suggested investigation. No setup-change advice without adequate evidence.
- End-to-end tests: CSV → model → maths → lap compare → events → report; MDF → mapping → model → workspace → report; session → video → sync → seek; workspace save → close → reopen.
- Profile import, memory, calculation, rendering, lap comparison, report generation and video seek; record results with machine details.
- macOS: verify Apple Silicon dependencies; document the app-bundle, code-signing and FFmpeg packaging route. Do not block core work on packaging, but avoid choices that make it harder.
- Docs: a user guide (import, inspect channels, map, create a maths channel, compare laps, workspaces, events, video, reports) and technical docs (model, sampling policy, units, expression syntax, filters, mapping, MDF compatibility, sync, known limitations). Label experimental features.

Phase order may flex (e.g. an early MDF prototype to validate the model), but build working vertical slices. Do not start all features at once.

## 11. Working loop (every slice)
1. Pick the smallest useful slice. 2. List the files to change. 3. Implement. 4. Add tests (clearly labelled synthetic data with known properties if needed). 5. Run the tests and the regression suite. 6. Review the diff. 7. Update docs.
Prefer small changes; avoid premature abstraction. Check code and docs before asking; ask only when ambiguity could cause a material design mistake. If a dependency, sample file or platform feature is unavailable, say so and use a safe fallback.

## 12. Phase report (end of each phase)
Features done · files changed · key decisions · tests run with actual results · performance measured · limitations · next step. Never mark the roadmap complete early.

## 13. Working efficiently
- Read only files relevant to the current slice; search (grep/glob) before opening files.
- Treat docs/implementation-roadmap.md as project memory: update its status instead of re-exploring the repo.
- Keep summaries short; do not restate this brief.

## 14. Start now
Do Phase 0, report the baseline, then implement the first Phase 1 slice with tests.
Guiding principle: show engineers what the data says, how reliable it is, what the evidence supports, and what to investigate next.
