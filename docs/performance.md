# Performance

Measured with `benchmarks/profile_session.py` on 2026-10-12.

**Machine:** Apple M5, 16 GB, macOS 27.0.1, Python 3.12.14. The repository and
its virtual environment are on an external drive, which is why loading the
libraries takes seconds on a cold start.

**Session:** the reference file (`nurburgring-mercedes_amg_gt3-10-…csv`, 264 MB):
951,694 rows × 57 columns, 33 mapped, 200 Hz, 79 minutes, 31 laps. Chart
figures are rendered offscreen with Qt's software rasteriser.

| Step | Time |
|---|---|
| Import the file to an analysed session (parse, map, units, laps, view, derived, model) | 3.5 s (2.4 s when run alone) |
| Save the model | 0.74 s (74 MB on disk) |
| Load the model, all channels | 37 ms |
| Load the model, three channels | 9 ms |
| Corner analysis | 0.16 s |
| Engineering report | 0.76 s |
| Two maths channels, one a filtered derivative | 30 ms |
| Seven maths fixtures of the brief | 53 ms |
| Event rules, default set (2,354 candidates) | 0.30 s |
| Lap comparison, engine, 1 m grid | 4 ms |
| Display decimation, one channel, whole session, 1600 px | 4 ms |
| HTML report | 1.6 s (624 KB page) |
| App: show the session (charts, map, events, comparison) | 2.1 s |
| App: redraw all charts after a pan | 21 ms |
| App: move the cursor (readout, map, comparison) | 23 ms |

**Memory (updated 2026-10-13):** the CSV is now parsed straight from the file
instead of being held as text; the import peaks at 2.4 GB, of which reading
the file accounts for 1.0 GB. The figures below are from before that change.

**Memory, before:** 3.2 GB peak during import, 4.1 GB by the end of the whole profile
(everything above in one process). Almost all of the import peak is pandas
parsing a 264 MB CSV; the model shares the parsed arrays rather than copying
them.

**MF4** (the same session rewritten as a three-rate MF4): inspect 0.10 s, five
channels 0.53 s, full import 4.5 s. See [mdf-compatibility.md](mdf-compatibility.md).

**Video:** not profiled. The only clip available is the six-second one the
tests encode themselves; a cursor move seeks it within the 60 ms the dock
spaces seeks by, which says nothing about a long recording from a camera.

## Found by measuring

- The event engine first took 12 s on this session: each event looked up its
  lap position through a full-array validity check. Looking all peaks up
  together brought it to 0.3 s.
- pyqtgraph's own downsampling on full arrays took 415 ms for the first draw
  of six charts; feeding it the engine's min/max decimation takes 60 ms.

## Not optimised

- The CSV import's memory. A chunked or typed parse would cut the 3.2 GB peak.
- Showing a session in the app (2.1 s) is dominated by the lap-comparison tab
  and the map being built before they are looked at; they could be built on
  first use.
- The HTML report embeds every lap trace as JSON.
