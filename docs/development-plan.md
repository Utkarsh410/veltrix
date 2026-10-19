# Veltrix Data — development plan, phases 4 to 20

This plan takes the seventeen phases set on 2026-10-19 and starts each one from
what the code already does, so no phase rebuilds work that exists. It sits
beside [implementation-roadmap.md](implementation-roadmap.md), which stays the
record of what was built; this file is the plan for what comes next.

## Where the product stands

Built and tested (375 tests, all passing; pushed to `main`):

- **Import** of CSV, MoTeC `.ld`/`.ldx`, iRacing `.ibt`, VBOX, AiM, MDF/MF4,
  MAT, CAN logs with DBC; mapping profiles; a canonical session model with
  raw / normalised / corrected / calculated layers and provenance.
- **Maths channels**: own parser (never `eval`), units, validity, editor window.
- **Desktop app**: stacked charts, Measures-and-laps panel, lap overlay with a
  corner-by-corner table, track map with corner zones, events and review,
  maths, video sync (experimental).
- **Analysis**: corners, braking from the turn-in, plausibility of g peaks,
  balance, tyres, the nine-section engineering report with evidence, confidence
  and recommendation for every finding; HTML report; two macOS launchers.

What is not there yet, and matters for every phase below:

- **No CI**: tests run only by hand.
- **Not validated on real cars**: real logger MF4, real onboard video, and
  real-car data. The real files tested are simulator sessions (iRacing Spa
  GR86, ACC Hungaroring and Nürburgring).
- **macOS only**: the simulators run on Windows.
- **Not distributable**: no signed app, no licence, no installer.

## How each phase maps onto the code today

| Phase | Name | Priority | Today | Size of what remains |
|---|---|---|---|---|
| 4 | Architecture hardening and regression testing | Critical | 375 tests, golden datasets, frozen outputs; no CI, one 1,500-line report module | **M** |
| 5 | Universal telemetry and channel model | Critical | Built (brief phase 1); gaps in units, simulator profiles, car and circuit libraries | **M** |
| 6 | Maths-channel engine | Critical | Built (brief phase 2); gaps in function library and in derived channels still hard-coded | **S–M** |
| 7 | Interactive telemetry workspace | Critical | Built (brief phase 3); gaps in plot types, saved layouts, multi-session overlay | **M–L** |
| 8 | MF4/MDF and professional data ingestion | High | Built on generated files (brief phase 4); never on a real logger | **M** (blocked on files) |
| 9 | Video synchronisation | High | Experimental (brief phase 5); never on real footage | **M** (blocked on footage) |
| 10 | Advanced vehicle dynamics | High | Steering model, understeer gradient, body slip, damper roll; car parameters mostly placeholders | **L** |
| 11 | Driver performance and coaching | High | Driver findings, braking from turn-in, per-corner comparison | **M** |
| 12 | Setup and vehicle-performance analysis | High | Evidence-gated setup hypotheses; iRacing setup read in part | **L** |
| 13 | Reporting and engineering deliverables | High | HTML report with data/limits tab, Markdown and text | **M** |
| 14 | Session management and workflow | Medium–High | Project store per folder; no session library | **L** |
| 15 | Extensible importers and integrations | Medium–High | Ten importers, mapping profiles; no plug-in interface, no exports | **M** |
| 16 | AI Race Engineer | Medium–High | Every finding already has evidence, calculation, confidence and recommendation | **M** |
| 17 | Live telemetry and session monitoring | Medium | Nothing | **L** |
| 18 | Collaboration and team workflows | Medium | Notes and reviews carry an author; nothing shared | **L** |
| 19 | Performance optimisation and scalability | Ongoing | Profiling and a benchmark exist; 264 MB CSV peaks at 2.4–3.1 GB | ongoing |
| 20 | Product release, licensing and distribution | Final | Launchers tied to this folder; packaging notes only | **L** |

Sizes are relative: **S** about one working session, **M** two to four, **L**
five or more. They are for ordering, not promises.

## Order of work

Phase 4 goes first: everything after it adds code to the analysis, and without
CI and module boundaries each addition risks breaking the findings that were
corrected on the Spa file. After that the order follows priority, except where
one phase needs another.

| Wave | Phases | Why together |
|---|---|---|
| A | 4 | The foundation: CI, regressions, module boundaries, stored-data versions |
| B | 5, 6, 7 | Close the critical gaps in what already exists |
| C | 10, 11, 12, 13 | The engineering value, built on the session model and maths engine |
| D | 8, 9 | Ready when real logger files and footage arrive; tooling can start earlier |
| E | 14, 15, 16 | Workflow across many sessions, then extension points, then the AI layer that reads them |
| F | 17, 18 | Live data and teams, after the single-user product is solid |
| — | 19 | A budget checked in every wave, not a phase at the end |
| G | 20 | Release |

## Phase plans

Each phase lists what is already there, what it adds, and when it is done. "Done
when" is tested, in the same way the brief's phases were.

### Phase 4 — Architecture hardening and regression testing (Critical)

**Status, 2026-10-19:** built — CI workflow, real-session regression check,
architecture rules, versioned saved files with release-1 fixtures, named errors,
log and crash reports. **Open:** the workflow has not run on GitHub yet (it runs
on the first push); requiring it to pass before merging to `main` is a GitHub
setting (branch protection) only the repo owner can switch on; "green for a
month" is measured from that first run.

**There:** 375 tests; golden datasets with frozen outputs
(`tools/freeze_expected.py`); offscreen app tests with screenshots; a test per
finding corrected against the real Spa file.

**Adds:**
- **CI on GitHub Actions** (macOS runner, offscreen Qt): every push runs the full
  suite; a failing test blocks merge to `main`.
- **Regression corpus**: the real sessions (Spa `.ibt`, Hungaroring `.ld`, the
  264 MB Nürburgring CSV) checked against frozen report outputs, kept out of git
  (Git LFS or a private download step) because of size and ownership.
- **Module boundaries**: split `analysis/engineering.py` (one section per
  module); a written interface between engine, analysis, report and app; no app
  module imported by the engine.
- **Stored-data versions**: project stores, reviews, workspaces and maths files
  carry a version and a migration path; tests open files from older versions.
- **Error discipline**: one error type per layer, a log file, a crash report the
  user can choose to send.
- Bring the roadmap's "Resume here" up to date (it still says no commit is
  planned).

**Done when:** CI is green on every push for a month of work; the real-file
corpus runs in CI or in a nightly job; no module exceeds an agreed size; a
project saved by the previous release opens unchanged.

### Phase 5 — Universal telemetry and channel model (Critical)

**There:** canonical schema, mapping profiles with priority (metadata >
verified profile > plausibility > user), units with provenance, model layers.

**Adds:**
- **Simulator profiles**, verified on a real file each: iRacing (done), ACC and
  AC (MoTeC route, done), rFactor 2 / Le Mans Ultimate, Automobilista 2, F1 UDP.
- **No unit left assumed**: an import with an unknown unit asks once and saves the
  answer in the profile.
- **Car library**: mass, weight distribution, wheelbase, track, steering ratio,
  wheel rates, read from the file where possible (as iRacing's setup now is),
  otherwise entered once per car — ends the placeholders behind several findings.
- **Circuit library**: corner files for the circuits used (Spa first), with
  official numbers and names, and marker-board positions so braking points count
  to the real boards.

**Done when:** every supported simulator imports a real session with no assumed
unit; car and circuit come from the library or the file; the report states no
placeholder value as the car's.

### Phase 6 — Maths-channel engine (Critical)

**There:** parser, units, validity, gap-aware functions, editor with live
validation, preview and dependency view.

**Adds:**
- Re-express the hard-coded derived channels (body slip, held lateral g, d(speed)/dt,
  load transfer) as maths channels, so an engineer can read and change them.
- **Function library**: filters (low/high pass, median), integrate, lap-relative
  and corner-relative functions, histogram and percentile, lookup tables.
- Share maths sets: import and export, a library per car, version history.
- Speed: a channel set computed once per session and cached.

**Done when:** every derived channel in the report is a maths channel shown in
the editor; a maths set made on one machine gives the same numbers on another.

### Phase 7 — Interactive telemetry workspace (Critical)

**There:** stacked charts, Measures-and-laps panel, lap overlay, corner table,
map, events, video, maths docks.

**Adds:**
- **Plot types**: XY / scatter, g-g in the app, histograms, a table view.
- **Saved layouts**: worksheets that hold charts, laps, filters and lap rules.
- **Comparing sessions**: laps from more than one session in the same
  overlay.
- **Keyboard-first use**: shortcuts for laps, zoom and the cursor.

**UI rule:** each new view follows a reference screenshot from race-data
software the user knows (AiM Race Studio, MoTeC i2), as agreed — no invented
layouts. Multi-session comparison in particular waits for that reference: an
earlier attempt was removed.

**Done when:** an engineer can do a full debrief (load, compare, find the corner,
read the numbers, note, report) without leaving the app, on a real display.

### Phase 8 — MF4/MDF and professional data ingestion (High)

**There:** lazy MF4 reader, channel picker, cancel and progress, damaged-file
errors, profiles; tested on generated files only.

**Adds:**
- Real logger files: Bosch, MoTeC M1, AiM, Cosworth — one each, with profiles.
- Bus logging: raw CAN in MF4 decoded with DBC.
- Very large files: streaming import within the memory budget (Phase 19).
- Exports: MDF and MoTeC `.ld` writers, so data can go back to the tools teams use.

**Blocked on:** a real logger's MF4. Tooling and exports can start before.

**Done when:** a real file from each logger family imports with a verified
profile, and the exported `.ld` opens in MoTeC i2.

### Phase 9 — Video synchronisation (High)

**There:** anchors, drift fit, multiple cameras, the cursor driving the video and
back; tested on synthetic video.

**Adds:**
- Real footage: onboard, phone and dash-cam frame rates and drift.
- Assisted sync: match the video's GPS or audio track, or a visible event, to the
  telemetry.
- Overlays: speed, pedals and the map drawn on the video; export a clip of a
  corner with data.

**Blocked on:** real footage with a known sync moment.

**Done when:** a real onboard video stays within one frame of the telemetry over
a full stint, and a corner clip with overlay exports in one action.

### Phase 10 — Advanced vehicle dynamics (High)

**There:** steady-state steering model and understeer gradient, body slip,
damper roll per g, plausibility of g peaks, Pacejka and ARB models.

**Adds:**
- Tyre slip angle and slip ratio per corner where wheel speeds allow.
- Load transfer by axle with real wheel rates (Phase 5 car library); roll and
  pitch; damper velocity histograms.
- Aero: ride height and downforce estimates where the channels exist.
- Yaw moment and balance through the corner phases, on measured body slip
  rather than the steering residual alone.

**Needs:** real-car data to validate; each output carries its validity and
assumptions.

**Done when:** each calculation is checked against a known case (simulator
setup values or a published test), and the report states its assumptions.

### Phase 11 — Driver performance and coaching (High)

**There:** driver findings (coasting, overlap, consistency, braking repeatability),
braking points from the turn-in, the corner-by-corner lap comparison.

**Adds:**
- **Coaching cards**: one per corner — where the best lap differed, the reference
  to use (marker board, apex), and what to try, with evidence.
- **Reference laps**: a driver against a team-mate or a target lap.
- **Progress over sessions**: consistency and corner times across a weekend
  (needs Phase 14).
- Throttle-on and other references from the corner, not the line, everywhere.

**Done when:** a driver can be handed a one-page card per session that a coach
agrees is right on a real session.

### Phase 12 — Setup and vehicle-performance analysis (High)

**There:** evidence-gated setup hypotheses; iRacing setup read for weight and
mass.

**Adds:**
- **Setup sheets**: read the full setup where the file carries it (iRacing,
  ACC), otherwise entered; stored with the session.
- **A/B comparison**: two sessions with one change, compared corner by corner with
  the scatter that decides whether the change is real.
- A setup and change history per car.

**Done when:** two sessions with one recorded change produce a verdict (better,
worse, no detectable change) with its confidence, checked on a real pair.

### Phase 13 — Reporting and engineering deliverables (High)

**There:** HTML report with evidence chains, data and limits tab, Markdown and
text; Report.app.

**Adds:**
- PDF export, and report templates (debrief, driver card, setup change).
- Repeatable: a report spec saved with the project rebuilds the same report.
- Comparison reports: session against session, driver against driver.

**Done when:** a report built twice from the same data and spec is identical,
and a PDF is ready to send.

### Phase 14 — Session management and workflow (Medium–High)

**There:** a project store per folder; workspaces; notes and reviews per file.

**Adds:**
- A **library**: sessions indexed by date, track, car, driver, simulator,
  with search and tags.
- Events (race weekends, test days) grouping sessions; runs and stints.
- Comparisons that span sessions, feeding Phases 11 and 12.

**UI rule:** reference screenshot first (as for Phase 7).

**Done when:** a season of sessions is found, filtered and compared without
opening folders.

### Phase 15 — Extensible importers and integrations (Medium–High)

**There:** ten importers with one shared table interface; mapping profiles.

**Adds:**
- A documented **importer interface**, so a new format is a module with tests,
  not a change to the engine.
- Exports: CSV, Parquet, MoTeC `.ld`, MDF.
- A command-line batch mode for folders of files.

**Done when:** an importer written against the interface by someone outside the
core passes the importer test kit unchanged.

### Phase 16 — AI Race Engineer (Medium–High)

**There:** every finding is already evidence, calculation, confidence and
recommendation — the material an explanation needs.

**Adds:**
- Plain-language explanations and questions ("why was lap 4 faster in T5?")
  answered **only** from the report's evidence and the session data, quoting the
  figures it uses.
- Guardrails: no claim without a cited finding; says "not in the data" when it is not.
- An evaluation set: questions with known answers on the golden sessions.

**Decision needed:** the brief requires local processing. A cloud model (the
Claude API) sends session data off the machine, so it would be opt-in and
explicit; a local model keeps data local at lower quality.

**Done when:** on the evaluation set, every answer cites a finding or says the
data does not show it, and no answer contradicts the report.

### Phase 17 — Live telemetry and session monitoring (Medium)

**Adds:**
- Live input from the simulator: iRacing's SDK, ACC's broadcast, AC shared memory.
- Live charts, lap-by-lap analysis as laps complete, alerts (tyre window,
  incidents).
- Recording to the same session format as files.

**Constraint:** the simulators run on Windows; live data needs either a Windows
build or a small relay on the simulator PC sending to the Mac over the network.

**Done when:** a live session's lap analysis matches the same session imported
from its file afterwards.

### Phase 18 — Collaboration and team workflows (Medium)

**There:** notes and reviews carry an author.

**Adds:**
- Project bundles: one file holding sessions, maths, notes, reviews and reports,
  to hand to someone else.
- Roles (engineer, driver), shared notes and reviews, later a shared library.

**Decision needed:** local-first exchange of files, or a shared server.

**Done when:** a driver and an engineer review the same session and see each
other's notes and verdicts.

### Phase 19 — Performance optimisation and scalability (Ongoing)

**Budgets**, checked in CI from Phase 4 on:

| Measure | Today | Budget |
|---|---|---|
| Import of the 264 MB reference CSV, peak memory | 2.4–3.1 GB | under 1.5 GB |
| Import time, same file | recorded in [performance.md](performance.md) | no regression above 10% |
| Chart redraw, zoom, cursor | recorded | under 50 ms |
| Report build, 70,000-sample session | about 14 s | under 10 s |

Work: share arrays between layers when nothing is resampled; compute derived
channels on native timestamps; cache maths results; profile each wave.

### Phase 20 — Product release, licensing and distribution (Final)

**Adds:**
- A self-contained, signed and notarised macOS app (and Windows if Phase 17
  calls for it), replacing the two launchers tied to this folder.
- Third-party licence review (PySide6/Qt and asammdf are LGPL, among others) and
  the product's own licence and terms.
- Installer, updates, crash reporting the user opts into, user documentation.
- A release checklist and versioning.

**Decision needed:** who the users are (private use, a team, commercial sale),
which sets the licence model and whether Windows is required.

**Done when:** someone who has never seen the code installs the app, opens a
file and gets a report.

## Decisions needed from you

| Decision | Affects | Default if not decided |
|---|---|---|
| Will the product run on Windows, where the simulators are? | 17, 20 | macOS only; live data through a relay |
| AI Race Engineer: cloud model (opt-in), local model, or both? | 16 | Opt-in cloud model, off by default |
| Collaboration: exchange files, or a shared server? | 18 | Files (project bundles) |
| Who is the product for, and how is it licensed? | 20 | Private use; licence decided before Phase 20 |
| May the real sessions (Spa `.ibt`, Hungaroring `.ld`) go into a private test store? | 4 | Kept on this machine, tested by hand |

## Data needed for validation

Phases 8, 9, 10 and 12 cannot be finished honestly without real data:

- A real logger's MF4 (Phase 8).
- Onboard video with a known sync moment (Phase 9).
- Real-car sessions with the car's parameters (Phases 5, 10, 12).
- Two sessions with one recorded setup change (Phase 12).

Work on these phases starts with the tooling; the "done when" waits for the data.

## Working rules carried over

The brief's rules stay in force: every conclusion is evidence → calculation →
confidence → recommendation; formulas are never `eval`ed; imported files are
untrusted; processing is local unless the user opts in; each slice ends with
tests passing and the roadmap updated; new desktop UI follows a reference from
software the user knows.
