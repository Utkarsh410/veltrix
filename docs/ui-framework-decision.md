# UI framework decision (Phase 3, slice 1)

**Chosen: PySide6 (Qt 6) with pyqtgraph, fed by the engine's own display
decimation.** Decided 2026-10-12 from the measurements below.

## What exists

No desktop UI. Two HTML surfaces — the self-contained session report and the
local workspace page — which stay as they are. The brief prefers what exists;
neither is an interactive editor, so the choice was open.

## Measured

Reference session: 951,694 samples at 200 Hz, 79 minutes. Apple M5, 16 GB,
macOS 27.0.1, Python 3.12, PySide6 6.12.0, pyqtgraph 0.14.0. Rendered
offscreen (software rasteriser, 1600 × 900), so on-screen figures will be at
least as good. Six stacked charts, one channel each, over the full session.

| Measure | pyqtgraph alone (its peak downsampling on full arrays) | pyqtgraph + `display.decimate` |
|---|---|---|
| First draw, six charts, whole session | 415 ms | 60 ms |
| Pan frame, 10-minute window | 10.6 ms | 10.3 ms |
| Frame zoomed to 10 s | 1.8 ms | 3.1 ms |

`display.decimate` itself (min and max per pixel column, 1600 px): 4.2 ms for
one channel over the whole session, 25 ms for six, 1.6 ms for six over one lap.

Process peak memory with the session loaded and both variants drawn: 2.5 GB,
almost all of it the CSV import, not the plots.

A pan frame of about 10 ms leaves room for 60 fps with six charts on the
largest session available.

## The alternative, and why not

A web front end (canvas or WebGL) in a desktop wrapper, served by the existing
local server.

| | PySide6 + pyqtgraph | Web + wrapper |
|---|---|---|
| Data path | NumPy arrays in process; no copy to the chart | every view change is a request and a JSON or binary transfer |
| Video (Phase 5) | Qt Multimedia, frame-accurate seek from the same event loop | HTML5 video; seek accuracy depends on the codec's keyframes |
| Existing report | shown in a `QWebEngineView` unchanged | native |
| Packaging | one app bundle; Qt frameworks are large (about 400 MB unpacked with WebEngine) | Python server plus a WebKit wrapper |
| Licence | LGPL-3.0 (PySide6), MIT (pyqtgraph) | MIT / BSD |
| Apple Silicon | arm64 wheels for both | arm64 |
| Measured here | yes | **no** — this machine has no headless browser, so a like-for-like render benchmark was not possible |

The web path was not benchmarked, and that is a limit of this decision. What
decided it is the data path and video: the engine's arrays are already in
process, the decimation cost is shared by both options, and Qt Multimedia
gives one event loop for charts, cursor and video.

## Consequences

- New optional extra `ui` (`PySide6`, `pyqtgraph`). The engine does not import
  Qt; the app lives in its own package, `veltrix_app`, with its state
  (`veltrix_app.workspace`) free of Qt so it can be tested and saved without a
  window.
- LGPL: the app must let a user replace the Qt libraries. A normal app bundle
  with the frameworks as separate dylibs satisfies that; static linking would
  not. Recorded for the packaging work in Phase 6.
- The HTML report and the local workspace page are kept.
