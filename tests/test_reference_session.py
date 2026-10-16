"""
The large real session the brief names as the performance target (79 minutes
at 200 Hz, 31 laps, 8 stints). It is not in the repository: these tests look
for it at VELTRIX_REFERENCE_SESSION, then in ~/Downloads, and are skipped when
it is not on the machine. They pin the headline numbers earlier reports were
built from, so a change to the import or lap logic that moves them is seen.
"""

import os
import time
from pathlib import Path

import numpy as np
import pytest

from veltrix_engine import corners, laps as lap_engine, load_session

NAME = "nurburgring-mercedes_amg_gt3-10-2026.04.08-16.26.51.csv"
PATH = Path(os.environ.get("VELTRIX_REFERENCE_SESSION", Path.home() / "Downloads" / NAME))
pytestmark = pytest.mark.skipif(not PATH.exists(), reason=f"reference session not on this machine ({PATH})")


@pytest.fixture(scope="module")
def loaded():
    t0 = time.perf_counter()
    s = load_session(PATH)
    return s, time.perf_counter() - t0


def test_headline_numbers(loaded):
    s, _ = loaded
    assert (len(s.channels), s.raw.data.shape[1], len(s.mapping), s.rate_hz) == (951_694, 57, 33, 200)
    assert (len(s.laps), s.laps["stint"].nunique(), int((s.laps["status"] == "flying").sum())) == (31, 8, 19)
    best = lap_engine.best_lap(s)
    assert best == 28 and s.laps.loc[s.laps["lap"] == best, "lap_time_s"].iloc[0] == pytest.approx(122.330, abs=5e-4)
    result = corners.analyse(s)
    assert len(result["corners"]) == 14 and result["ideal_lap_s"] == pytest.approx(120.730, abs=5e-4)


def test_the_model_holds_the_session_without_a_second_copy(loaded):
    s, _ = loaded
    m = s.model
    assert len(m.channels("raw")) == 57 and len(m.channels("normalised")) == 33
    assert len(m.laps) == 31 and len(m.stints) == 8
    speed = m.get("speed", "normalised")
    assert speed.sample_rate == pytest.approx(200, rel=1e-3) and len(speed.values) >= 951_694
    clocks = {id(c.timestamps) for c in m.channels("normalised")}
    assert len(clocks) == 1, "one timestamp array shared by every channel of the file"
    raw = m.get(s.mapping["rpm"], "raw")
    assert np.shares_memory(raw.values, s.raw.data[s.mapping["rpm"]].to_numpy())


def test_import_stays_interactive(loaded):
    _, seconds = loaded
    assert seconds < 20, f"import took {seconds:.1f} s (2 to 3 s when measured; this guards an order of magnitude)"


def test_maths_channels_on_the_full_session(loaded):
    from veltrix_engine import maths
    s, _ = loaded
    mset = maths.MathSet()
    mset.parameters.update(throttle_min=70, slip_limit=8)
    mset.add("RearWheelSpeed", "(wheel_speed_rl + wheel_speed_rr) / 2")
    mset.add("SteeringRate", "derivative(steering)")
    mset.add("CombinedG", "sqrt(acc_lat^2 + acc_long^2)")
    mset.add("LongG_fromSpeed", "lowpass(derivative(speed in m/s), 5) in g", unit="g")
    mset.add("RearSlipEstimate", "(RearWheelSpeed - speed) / speed * 100", unit="%", estimate=True)
    mset.add("RearWheelspinCandidate", "throttle > $throttle_min and RearSlipEstimate > $slip_limit")
    t0 = time.perf_counter()
    results = maths.compute(s.model, mset)
    seconds = time.perf_counter() - t0
    assert all(r.ok for r in results.values()), {k: r.message for k, r in results.items() if not r.ok}
    slip = results["RearSlipEstimate"].channel
    speed = s.model.get("speed", "normalised").values
    assert len(slip.values) == len(speed) and not slip.validity[speed == 0].any(), "no slip figure while stationary"
    assert seconds < 5, f"six maths channels took {seconds:.2f} s (about 0.05 s when measured)"


def test_workspace_walkthrough_on_the_full_session(loaded, tmp_path):
    """Phase 3's "done when", end to end on the large session, offscreen:
    load → browse → synced charts → overlay laps by distance and time → plot a
    maths channel → map follows the cursor → inspect an event → save and reopen."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtWidgets
    from veltrix_app.window import MainWindow
    s, _ = loaded
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    took = {}

    def timed(name, fn):
        t0 = time.perf_counter()
        out = fn()
        app.processEvents()
        took[name] = time.perf_counter() - t0
        return out

    w = MainWindow(tmp_path / "maths.json")
    w.resize(1600, 950)
    w.show()
    timed("show session", lambda: w.show_session(s))
    assert len(w._plots) >= 4 and w.browser.rowCount() >= 33

    w.search.setText("wheel speed")
    shown = {w.browser.item(r, 0).text() for r in range(w.browser.rowCount()) if not w.browser.isRowHidden(r)}
    assert shown >= {"wheel_speed_fl", "wheel_speed_fr", "wheel_speed_rl", "wheel_speed_rr"} and "rpm" not in shown
    w.search.setText("")

    lap = s.lap_row(28)
    timed("zoom to a lap", lambda: (w._plots[0].setXRange(lap.t_start, lap.t_end, padding=0), w._draw_curves()))
    timed("move cursor", lambda: w.set_cursor(lap.t_start + 60.0))
    assert all(c.value() == pytest.approx(lap.t_start + 60.0) for c in w._cursors) and w.map.lap == 28

    timed("lap comparison", lambda: w.compare.set_laps([28, 7]))
    assert w.compare.cmp.laps == [28, 7] and len(w.compare._plots) == len(w._plots) + 1
    timed("switch alignment", lambda: w.compare.set_align("time"))
    assert "aligned by time" in w.compare.mode.text()

    ed = w.maths.editor
    ed.name.setText("RearSlipEstimate")
    ed.unit.setText("%")
    ed.formula.setPlainText("((wheel_speed_rl + wheel_speed_rr) / 2 - speed) / speed * 100")
    assert ed.validate()["ok"]
    timed("save and plot a maths channel", lambda: (ed.save_channel(), ed.plot_requested.emit("RearSlipEstimate")))
    assert "RearSlipEstimate" in w.ws.charts[w.active].channels

    assert w.events.events.rowCount() == len(s.model.events) > 0
    when = w.events.events.item(0, 0).data(0x0100)
    timed("jump to an event", lambda: w.events.jump.emit(when))
    assert w.ws.cursor == pytest.approx(when)

    saved = tmp_path / "walkthrough.json"
    w.save_workspace(str(saved))
    charts = [list(c.channels) for c in w.ws.charts]
    w.close()
    again = MainWindow(tmp_path / "maths.json")
    again.open_workspace(str(saved))
    timed("reopen", lambda: again.show_session(s))
    assert [list(c.channels) for c in again.ws.charts] == charts and again.compare.cmp.laps == [28, 7]
    assert again.session.model.has("RearSlipEstimate"), "the maths channel is back after a restart"
    again.close()
    print("\nworkspace walkthrough, seconds:", {k: round(v, 3) for k, v in took.items()})
    assert max(took.values()) < 5, took
