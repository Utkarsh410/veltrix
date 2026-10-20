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
| 14 | Session management and workflow | Medium–High | Engine side built 2026-10-20: session library (`veltrix library`); app view waits for a reference | **L** |
| 15 | Extensible importers and integrations | Medium–High | Built 2026-10-20: plug-in interface, test kit, batch mode; exports in wave D | **M** |
| 16 | AI Race Engineer | Medium–High | Every finding already has evidence, calculation, confidence and recommendation | **M** |
| 17 | Live telemetry and session monitoring | Medium | Nothing | **L** |
| 18 | Collaboration and team workflows | Medium | Notes and reviews carry an author; nothing shared | **L** |
| 19 | Performance optimisation and scalability | Ongoing | Budgets met 2026-10-20: 264 MB CSV imports at 1.2 GB peak, report adds 0.2 GB | ongoing |
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

**Status, 2026-10-19:** car library built (`cars.py`, `veltrix car`, used by the
app and the command line; priority stated > file > library > placeholder);
plain Open now points to Open with mapping when a unit was assumed (the answer
is remembered in the logger's profile). **Waiting on input:** official corner
numbers, names and marker-board positions for Spa (and each circuit used);
a real session from rFactor 2 / Le Mans Ultimate, Automobilista 2 and an F1 game
for their profiles; a reference screenshot for a car-settings screen in the app.

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

**Status, 2026-10-19:** built — built-in set of the engine's derived channels
(matching the engine on evenly sampled logs), five new functions, filters that
cap a cutoff and handle an uneven clock, import / export with dependencies and
clash handling, formula history (maths format 2). **Open:** making the formulas
the engine's own source on multi-rate logs, which moves numbers there — with
the native-timestamp work in phases 5 and 19.

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

**Status, 2026-10-19:** exports built (`exports.py`, `veltrix export`: CSV with a units row, Parquet with units per column, MF4) and checked to import back to the same laps. **Waiting on data:** a real logger's MF4; a MoTeC `.ld` writer is not built because whether i2 opens it cannot be checked here.

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

**Status, 2026-10-19:** waiting — assisted sync (GPS / audio / a visible event) needs real footage to build against.

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

**Status, 2026-10-19:** built — wheel slip ratio, damper speed (the file's own when it logs one, else estimated), roll and pitch at the dampers, front and rear tyre slip angles; two vehicle-balance findings. Checked on the Spa `.ibt`: slip angles' front−rear per g (+0.64°) agrees with the steering-based understeer gradient (+0.79°); the file's damper speed is used. **Open:** aero and ride height; validation on a real car.

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

**Status, 2026-10-19:** coaching cards built (`analysis/coaching.py`, `veltrix coach`, printable page). **Open:** a reference lap from another driver; progress over sessions per corner (lap-level progress is `veltrix library progress`, phase 14); a coach's check on a real session.

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

**Status, 2026-10-19:** built — iRacing setup sheet read in full; `analysis/ab.py` and `veltrix compare` give better / worse / no detectable change per lap and corner against the scatter. **Open:** ACC setup files; a setup history per car; checking a verdict on a real pair of sessions with one recorded change.

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

**Status, 2026-10-19:** built — `report/documents.py` (debrief, driver card, comparison — static, A4), PDF through Qt WebEngine (`report/pdf.py`), repeatable specs (`veltrix document … --save-spec`, `--spec`); a spec builds the same page byte for byte.

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

**Status, 2026-10-20:** the engine side is built.
- `library.py` and `veltrix library`: an index of sessions where they lie
  (nothing copied), filled by `scan`, which reads only new and changed files.
  Search and filters (track, car, driver, simulator, tag, event, session type,
  dates, text). Your own values over aliases over the file. Tags and notes.
  Events, with suggestions from track and date.
- Across sessions: `progress` (best, ideal, median lap and scatter, session by
  session), `best` per track and car, and `compare` (phase 12's comparison) by
  library id.
- Moved files keep their id. Missing, failed and duplicate files are listed as
  such.

`tests/test_library.py` builds a six-session season over two events and
checks all of this. On the three real sessions a scan takes 3.9 s with three
jobs, and gives the figures the regression tests pin.

**Open:**
- The app's library view, which waits for a reference screenshot.
- Runs inside a session beyond the stints the lap table already has.
- Per-corner progress for coaching (phase 11).

### Phase 15 — Extensible importers and integrations (Medium–High)

**There:** ten importers with one shared table interface; mapping profiles.

**Adds:**
- A documented **importer interface**, so a new format is a module with tests,
  not a change to the engine.
- Exports: CSV, Parquet, MoTeC `.ld`, MDF.
- A command-line batch mode for folders of files.

**Done when:** an importer written against the interface by someone outside the
core passes the importer test kit unchanged.

**Status, 2026-10-20:** built.
- The interface: `importers/plugins.py` (`Importer`, `register`, the
  `veltrix.importers` entry points, the table contract).
- The test kit: `importers/testkit.py`, and `veltrix importer check`.
- Batch mode: `batch.py`, `veltrix batch` (outputs mirror the folder, with an
  index; failures and duplicates are rows; `--jobs`).
- The guide: [importers.md](importers.md).

The example importer in `examples/importer_plugin/` uses only `Importer` and
`RawTable` and passes the kit unchanged, in its own tests and in the engine's;
installed as a package, it is found through its entry point. Exports to CSV,
Parquet and MF4 were built in wave D.

**Open:** a MoTeC `.ld` writer, which can't be checked against i2 here. A
plug-in from a real third party is still to come.

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

| Measure | 2026-10-19 | 2026-10-20 | Budget |
|---|---|---|---|
| Import of the 264 MB reference CSV, peak memory | 2.3–2.5 GB | 1.2 GB | under 1.5 GB |
| Import time, same file | 2.1–3.2 s | 2.0 s | no regression above 10% |
| Chart redraw after a pan / cursor move | 21 / 42 ms | 21 / 41 ms | under 50 ms |
| Report build, 70,000-sample session (Spa `.ibt`, command line) | about 14 s (old figure) | 1.6 s | under 10 s |

**Status, 2026-10-20:** budgets met; the memory one is a test
(`tests/test_reference_session.py`, runs where the reference CSV is on the
machine). Done: a log on a steady clock is analysed on its own timestamps
with no copy; integer CSV columns kept in the smallest exact type; no whole-
frame copies in the analyses (`Session.frame(columns)`); the model store
writes and reads Parquet without a pandas copy. Outputs unchanged except
floating-point rounding (one tyre sample on the synthetic set crosses its
window edge). Details in [performance.md](performance.md). **Open:** derived
channels on native timestamps for multi-rate files; caching maths results;
the HTML report embeds every lap trace.

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
