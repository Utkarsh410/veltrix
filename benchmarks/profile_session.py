"""
Profile the whole path on one session and print a table with machine details.

  .venv/bin/python benchmarks/profile_session.py path/to/session.csv

Covers import, the model on disk, corner analysis, the engineering report,
maths channels, the event rules, lap comparison, display decimation, chart
rendering (offscreen, if PySide6 is installed) and the HTML report. Timings
are the best of a few runs except import, which is run once.
"""

import os
import platform
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np


def best(fn, n=5):
    out = 1e9
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        out = min(out, time.perf_counter() - t0)
    return out


def main(path: str) -> None:
    rows = []
    t0 = time.perf_counter()
    from veltrix_engine import corners, events, laps as lap_engine, load_session, maths
    from veltrix_engine.analysis import engineering
    from veltrix_engine.display import decimate
    from veltrix_engine.model_store import load_model, save_model
    from veltrix_engine.report import render_report
    rows.append(("Import the libraries", time.perf_counter() - t0))
    t0 = time.perf_counter()
    s = load_session(path)
    rows.append(("Import the file to an analysed session", time.perf_counter() - t0))
    rss_import = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6
    m = s.model
    folder = Path(tempfile.mkdtemp())
    rows.append(("Save the model", best(lambda: save_model(m, folder / "model"), 2)))
    rows.append(("Load the model, all channels", best(lambda: load_model(folder / "model"), 3)))
    rows.append(("Load the model, three channels", best(lambda: load_model(folder / "model", ["speed", "throttle", "brake"]))))
    size = sum(p.stat().st_size for p in (folder / "model").iterdir()) / 1e6
    result = {}
    rows.append(("Corner analysis", best(lambda: result.update(c=corners.analyse(s)), 3)))
    rows.append(("Engineering report", best(lambda: engineering.analyse(s, result["c"]), 2)))
    mset = maths.MathSet()
    mset.add("CombinedG", "sqrt(acc_lat^2 + acc_long^2)")
    mset.add("LongG", "lowpass(derivative(speed in m/s), 5) in g")

    def run_maths():
        m.maths_signatures.clear()
        maths.compute(m, mset)
    rows.append(("Two maths channels (one filtered derivative)", best(run_maths)))
    found = {}
    rows.append(("Event rules, default set", best(lambda: found.update(e=events.detect(m)), 3)))
    flying = list(s.laps.loc[s.laps["status"] == "flying", "lap"])
    if len(flying) >= 2:
        rows.append(("Lap comparison (engine, 1 m grid)", best(lambda: lap_engine.compare_laps(s, flying[0], flying[1]))))
    speed = m.get("speed", "normalised")
    rows.append(("Display decimation, one channel, whole session", best(lambda: decimate(speed.timestamps, speed.values, width_px=1600), 20)))
    rows.append(("HTML report", best(lambda: render_report(s), 2)))
    try:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6 import QtGui, QtWidgets
        from veltrix_app.window import MainWindow
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        w = MainWindow(folder / "maths.json")
        w.resize(1600, 950)
        w.show()
        t0 = time.perf_counter()
        w.show_session(s)
        app.processEvents()
        rows.append(("App: show the session (charts, map, events, comparison)", time.perf_counter() - t0))

        def frame():
            lo, hi = w.ws.window()
            span = min(600.0, (hi - lo) / 4)
            start = float(np.random.default_rng(len(rows)).uniform(lo, hi - span)) if hi - lo > span else lo
            w._plots[0].setXRange(start, start + span, padding=0)
            w._draw_curves()
            app.processEvents()
            image = QtGui.QImage(1600, 950, QtGui.QImage.Format_ARGB32)
            painter = QtGui.QPainter(image)
            w.graphs.render(painter)
            painter.end()
        rows.append(("App: redraw all charts after a pan (offscreen)", best(frame, 8)))
        rows.append(("App: move the cursor (readout, map, comparison)", best(lambda: (w.set_cursor(w.ws.extent()[1] * 0.37), app.processEvents()), 5)))
        w.close()
    except ImportError:
        rows.append(("App (PySide6 not installed)", float("nan")))

    chip = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True).stdout.strip() or platform.processor()
    mem = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout or 0) / 2 ** 30
    print(f"\nMachine: {chip}, {mem:.0f} GB, macOS {platform.mac_ver()[0]}, Python {platform.python_version()}")
    print(f"Session: {Path(path).name} — {len(s.raw.data):,} rows × {s.raw.data.shape[1]} columns, {len(s.mapping)} mapped, "
          f"{s.rate_hz:g} Hz, {len(s.laps)} laps, {len(found.get('e', ([],))[0])} candidate events")
    print(f"Peak memory after import: {rss_import:,.0f} MB; at the end: {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6:,.0f} MB; "
          f"model on disk: {size:.0f} MB\n")
    width = max(len(r[0]) for r in rows)
    for name, seconds in rows:
        print(f"  {name:<{width}}  {seconds * 1000:>9.1f} ms" if seconds < 1 else f"  {name:<{width}}  {seconds:>9.2f} s")


if __name__ == "__main__":
    main(sys.argv[1])
