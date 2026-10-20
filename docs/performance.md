# Performance

Measured with `benchmarks/profile_session.py` on 2026-10-20 (phase 19). Budgets
are in [development-plan.md](development-plan.md), phase 19.

**Machine:** Apple M5, 16 GB, macOS 27.0.1, Python 3.12.14. The repository and
its virtual environment are on an external drive, which is why loading the
libraries takes seconds on a cold start.

**Session:** the reference file (`nurburgring-mercedes_amg_gt3-10-…csv`, 264 MB):
951,694 rows × 57 columns, 33 mapped, 200 Hz, 79 minutes, 31 laps. Chart
figures are rendered offscreen with Qt's software rasteriser. "Peak so far" is
the process's memory high-water mark once the step has run, all steps in one
process.

| Step | Time | Peak so far |
|---|---|---|
| Import the file to an analysed session (parse, map, units, laps, view, derived, model) | 2.0 s | 1,272 MB |
| Save the model | 0.97 s (123 MB on disk) | 1,350 MB |
| Load the model, all channels | 149 ms | 1,816 MB |
| Load the model, three channels | 9 ms | |
| Corner analysis | 0.11 s | |
| Engineering report | 1.8 s | 2,098 MB |
| Two maths channels, one a filtered derivative | 30 ms | |
| Event rules, default set (2,371 candidates) | 0.13 s | |
| Lap comparison, engine, 1 m grid | 3 ms | |
| Display decimation, one channel, whole session, 1600 px | 4 ms | |
| HTML report | 2.9 s | 2,285 MB |
| App: show the session (charts, map, events) | 1.4 s | |
| App: redraw all charts after a pan | 21 ms | |
| App: move the cursor (readout, map) | 41 ms | |

The load step's peak counts a second full model held beside the session (the
benchmark keeps both); opening a saved session on its own peaks lower.

**Memory, each in a fresh process** (`ru_maxrss`, the test in
`tests/test_reference_session.py` checks the first two):

| | 2026-10-19 | 2026-10-20 |
|---|---|---|
| Import | 2,334 MB | 1,216 MB |
| Import, then the HTML report | 2,402 MB | 1,399 MB |

Of the import's peak, about 490 MB is the session itself (raw table 74 MB,
was 414 MB, now that its 56 integer columns are int8/16/32 rather than int64;
normalised channels 254 MB; derived 160 MB); the rest is what pandas' CSV
parser allocates and does not give back.

**Other sessions:** Spa `.ibt` (70,145 samples) imports in 0.4 s and reports in
0.3 s (1.6 s from the command line, with start-up); Hungaroring `.ld` (877,801
samples) 1.4 s and 2.7 s.

**MF4** (the same session rewritten as a three-rate MF4): inspect 0.10 s, five
channels 0.53 s, full import 4.5 s. See [mdf-compatibility.md](mdf-compatibility.md).

**Video:** not profiled. The only clip available is the six-second one the
tests encode themselves; a cursor move seeks it within the 60 ms the dock
spaces seeks by, which says nothing about a long recording from a camera.

## Found by measuring

- Phase 19 (2026-10-20): the corner analysis and the balance-incident
  detector each copied the whole session frame (all 57 columns) to read eight;
  each spiked memory by 1.2 GB. A log on a steady clock was interpolated onto
  its own timestamps, copying every channel; filtering and sorting the time
  base copied the table even with nothing to drop. The CSV's 56 integer
  columns were held as int64. Saving the model built a pandas frame of every
  channel; loading converted a whole Arrow table at once.

- The event engine first took 12 s on this session: each event looked up its
  lap position through a full-array validity check. Looking all peaks up
  together brought it to 0.3 s.
- pyqtgraph's own downsampling on full arrays took 415 ms for the first draw
  of six charts; feeding it the engine's min/max decimation takes 60 ms.

## Not optimised

- The CSV parser's own transient (about 450 MB on the reference file). A
  chunked parse and pyarrow's reader were both tried: chunked peaks 100 MB
  higher, pyarrow 400 MB higher (though 2.5× faster).
- Showing a session in the app (1.4 s): the map and events are built before
  they are looked at; they could be built on first use.
- The HTML report embeds every lap trace as JSON.
