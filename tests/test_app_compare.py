"""Lap comparison in the app: laps overlaid by distance or by time, and delta time against a reference."""

import numpy as np
import pytest

from conftest import GOLDEN
from veltrix_app.compare import LapComparison
from veltrix_engine import build_session, laps as lap_engine, load_session
from veltrix_engine.importers import parse_csv_text


@pytest.fixture(scope="module")
def monza():
    return load_session(GOLDEN / "monza_ac_gt3.csv.gz")


def test_defaults_are_the_best_lap_against_the_next_best(monza):
    cmp_ = LapComparison(monza.model)
    assert cmp_.best() == 4 and cmp_.laps == [4, 1] and cmp_.reference == 4, "laps 4 and 1 are the two fastest"
    assert cmp_.previous(4) == 3 and cmp_.previous(1) is None
    assert cmp_.set_laps([2, 2, 99, 3]) == [2, 3], "unknown laps are dropped, repeats ignored"
    assert [lap.lap for lap in cmp_.usable()] == [1, 2, 3, 4], "the out-lap did not start on the line"
    with pytest.raises(ValueError, match="aligned by distance or time"):
        cmp_.set_align("rpm")


def test_delta_agrees_with_the_engine_and_ends_at_the_lap_time_difference(monza):
    cmp_ = LapComparison(monza.model, laps=[4, 2])
    d = cmp_.delta(2)
    engine = lap_engine.compare_laps(monza, 4, 2)["delta_t"]
    theirs = np.interp(d["distance_m"], engine.index.to_numpy(), engine.to_numpy())
    assert np.nanmax(np.abs(d["dt"] - theirs)[5:-5]) < 0.02, "same answer as laps.compare_laps"
    times = monza.laps.set_index("lap")["lap_time_s"]
    assert d["final_s"] == pytest.approx(times[2] - times[4]) and d["dt"][-1] == pytest.approx(d["final_s"], abs=0.05)
    assert cmp_.delta(4) is None, "the reference has no delta against itself"


def test_distance_and_time_alignment_put_the_same_samples_on_different_axes(monza):
    cmp_ = LapComparison(monza.model, laps=[4, 2], align="distance")
    by_d = cmp_.trace("speed", 2)
    cmp_.set_align("time")
    by_t = cmp_.trace("speed", 2)
    assert by_d["align"] == "distance" and by_t["align"] == "time" and "Distance" not in cmp_.x_label
    assert np.array_equal(by_d["v"], by_t["v"], equal_nan=True), "the lap's own samples either way, not resampled"
    lap = monza.lap_row(2)
    assert by_t["x"][0] >= 0 and by_t["x"][-1] <= lap.lap_time_s
    assert 0 <= by_d["x"][0] < 10 and by_d["x"][-1] == pytest.approx(monza.lap_length, rel=0.01)
    assert np.all(np.diff(by_d["x"]) >= 0)
    assert cmp_.extent() == (0.0, pytest.approx(max(monza.lap_row(4).lap_time_s, lap.lap_time_s)))
    drawn = LapComparison(monza.model, laps=[4, 2]).trace("speed", 4, width_px=300)
    assert drawn["decimated"] and drawn["points"] <= 600 and np.nanmax(drawn["v"]) == np.nanmax(cmp_.trace("speed", 4)["v"])


def test_readout_compares_the_laps_at_the_same_place(monza):
    cmp_ = LapComparison(monza.model, laps=[4, 2])
    rows = cmp_.readout(5000.0, ["speed", "gear", "nope"])
    by = {(r["channel"], r["lap"]): r for r in rows}
    assert set(by) == {("speed", 4), ("speed", 2), ("gear", 4), ("gear", 2)}
    ref, other = by[("speed", 4)], by[("speed", 2)]
    assert ref["difference"] is None and other["difference"] == pytest.approx(other["value"] - ref["value"])
    assert by[("gear", 2)]["difference"] is None and by[("gear", 2)]["how"] in ("held", "sample")
    t = cmp_.session_time(2, 5000.0)[0]
    frame = monza.channels
    near = frame.iloc[(frame["time"] - t).abs().idxmin()]
    assert near["lap"] == 2 and near["lap_distance"] == pytest.approx(5000.0, abs=1.0)
    assert other["value"] == pytest.approx(near["speed"], abs=1.0)
    assert all(r["value"] is None and r["how"] == "none" for r in cmp_.readout(1e7, ["speed"])), "past the end of the lap"


def test_a_session_without_laps_says_comparison_is_not_possible():
    t = np.arange(0, 30, 0.1)
    s = build_session(parse_csv_text("Time,Speed\n" + "\n".join(f"{a:.1f},{100 + a:.1f}" for a in t)))
    with pytest.raises(ValueError, match="no lap information"):
        LapComparison(s.model)


def test_laps_are_loaded_from_the_panel_on_the_left(tmp_path, monza):
    """The "Measures and laps" panel: laps are loaded by clicking them, drawn
    over each other, listed under "Tests loaded"; measures are buttons with
    the value of every loaded lap beside them."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtWidgets
    from veltrix_app.window import MainWindow
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    w = MainWindow(tmp_path / "maths.json")
    w.resize(1500, 900)
    w.show()
    panel, view = w.panel, w.compare
    assert w.browser_dock.windowTitle() == "Measures and laps" and w.dockWidgetArea(w.browser_dock) == QtCore.Qt.LeftDockWidgetArea
    assert [panel.tabs.tabText(i) for i in range(panel.tabs.count())] == ["Measures", "Laps", "Channel details"]
    assert not w.tabs.tabBar().isVisible(), "there is no Lap comparison tab to go to"
    w.show_session(monza)
    app.processEvents()

    # nothing loaded: the whole session against time, one value per drawn measure
    assert w.loaded_laps() == [] and w.tabs.currentIndex() == 0 and panel.loaded == []
    assert panel.lap_list.topLevelItemCount() == len(monza.model.laps)
    drawn = {c for chart in w.ws.charts for c in chart.channels}
    assert {n for n, b in panel.measures.items() if b.isChecked()} == drawn and "speed" in drawn
    assert panel.measures["speed"].text() == "speed   [km/h]"
    w.set_cursor(100.0)
    assert panel.values["speed"].text().count("<span") == 1
    not_drawn = next(n for n in panel.measures if n not in drawn)
    assert panel.values[not_drawn].text() == ""

    # Laps tab: a click on a lap's row loads it, a second click unloads it
    def click(lap):
        panel.lap_list.itemClicked.emit(panel.lap_row(lap), 1)
    click(4)
    assert w.loaded_laps() == [4] and w.tabs.currentIndex() == 1, "the loaded lap is what the charts show now"
    click(2)
    click(3)
    assert view.cmp.laps == [4, 2, 3] and panel.tabs.tabText(1) == "Laps (3 loaded)"
    assert panel.lap_row(4).text(0) == "Lap 4  — reference" and panel.lap_row(2).font(0).bold() and not panel.lap_row(1).font(0).bold()
    assert "aligned by distance" in view.mode.text() and "Reference: lap 4" in view.mode.text()
    assert len(view._plots) == len([c for c in w.ws.charts if c.channels]) + 1, "the session's charts, and delta time"
    assert len(view._plots[-1].listDataItems()) == 2
    assert all(p.legend is None for p in view._plots + w._plots), "no legend laid over the curves"
    assert view._plots[0].key == ["speed [km/h]"] and view._plots[-1].key[0].startswith("lap 2 +")
    assert "Δt against lap 4" in view._plots[-1].titleLabel.text

    # the charts are evenly laid out: same plotting height, same left edge, whichever carries the x-axis numbers
    app.processEvents()
    for plots in (view._plots, w._plots):
        boxes = [p.vb.sceneBoundingRect() for p in plots]
        assert max(b.height() for b in boxes) - min(b.height() for b in boxes) <= 2, [b.height() for b in boxes]
        assert max(b.left() for b in boxes) - min(b.left() for b in boxes) <= 1
        gaps = [b2.top() - b1.bottom() for b1, b2 in zip(boxes, boxes[1:])]
        assert max(gaps) - min(gaps) <= 2, gaps

    # Measures tab: tests loaded
    assert [x["lap"] for x in panel.loaded] == [4, 2, 3]
    assert panel.loaded[0]["text"].endswith("reference") and panel.loaded[1]["text"].split()[-1].startswith("+")

    # values at the cursor: one per loaded lap, and the map follows the reference lap
    view.set_cursor(5000.0)
    assert all(line.value() == 5000.0 for line in view._cursors)
    shown = panel.values["speed"].text()
    expect = [f"{r['value']:.4g}" for r in view.cmp.readout(5000.0, ["speed"])]
    assert shown.count("<br>") == 2 and all(f">{v}<" in shown for v in expect)
    lap4 = next(lap for lap in monza.model.laps if lap.lap == 4)
    assert lap4.t_start < w.ws.cursor < lap4.t_end, "the session cursor is where the reference lap was"

    # a measure's button draws the channel or stops drawing it
    before = len(w.ws.charts)
    panel.measures[not_drawn].click()
    assert any(not_drawn in c.channels for c in w.ws.charts) and len(w.ws.charts) == before + 1
    assert panel.measures[not_drawn].isChecked() and len(view._plots) == before + 2 and panel.values[not_drawn].text()
    panel.measures[not_drawn].click()
    assert len(w.ws.charts) == before and not panel.measures[not_drawn].isChecked()
    panel.channel_search.setText("spee")
    assert panel.channel_names(shown_only=True) == [n for n in panel.channel_names() if "spee" in n.lower()]
    panel.channel_search.setText("")
    panel.sort.setChecked(True)
    assert set(panel.channel_names()[:len(drawn)]) == drawn, "the drawn channels come first"
    panel.sort.setChecked(False)

    # reference, unloading, alignment
    panel.loaded[2]["reference"].click()
    assert view.cmp.laps == [3, 4, 2] and "Reference: lap 3" in view.mode.text()
    next(x for x in panel.loaded if x["lap"] == 4)["remove"].click()
    assert view.cmp.laps == [3, 2] and panel.lap_row(4).text(0) == "Lap 4"
    view.set_align("time")
    assert view.cmp.align == "time" and "aligned by time" in view.mode.text()
    view.set_align("distance")
    w.load_laps([4, 2, 3])

    saved = tmp_path / "layout.json"
    w.save_workspace(str(saved))
    w.load_laps([])
    assert w.tabs.currentIndex() == 0 and panel.loaded == [] and panel.tabs.tabText(1) == "Laps"
    w.open_workspace(str(saved))
    assert w.compare.cmp.laps == [4, 2, 3] and w.tabs.currentIndex() == 1, "the loaded laps are part of the workspace"
    assert [x["lap"] for x in panel.loaded] == [4, 2, 3]
    panel.best_two.click()
    assert w.loaded_laps() == [4, 1]
    shot = os.environ.get("VELTRIX_SCREENSHOT")
    if shot:
        w.load_laps([4, 2, 3])
        view.set_cursor(3000.0)
        app.processEvents()
        w.grab().save(shot)
        panel.tabs.setCurrentIndex(1)
        app.processEvents()
        w.grab().save(shot.replace(".png", "_laps.png"))
    w.resize(1300, 700)                                       # still even after a resize, and with fewer charts
    w.set_measure("gear", False)
    w.set_measure("rpm", False)
    for _ in range(3):
        app.processEvents()
    heights = [p.vb.sceneBoundingRect().height() for p in view._plots]
    # even; and still usable in a 700 px window with the corner table taking its share underneath
    assert max(heights) - min(heights) <= 2 and min(heights) > 45, heights
    if shot:
        w.grab().save(shot.replace(".png", "_fewer.png"))
    panel.unload.click()
    for _ in range(3):
        app.processEvents()
    heights = [p.vb.sceneBoundingRect().height() for p in w._plots]
    assert max(heights) - min(heights) <= 2 and min(heights) > 60, heights
    if shot:
        w.grab().save(shot.replace(".png", "_session.png"))
    assert w.loaded_laps() == [] and w.tabs.currentIndex() == 0
    w.close()


def test_corner_by_corner_table_under_the_overlaid_laps(tmp_path, monza):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtWidgets
    from veltrix_engine import corners as corner_engine
    from veltrix_app.window import MainWindow
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    w = MainWindow(tmp_path / "maths.json")
    w.resize(1500, 950)
    w.show()
    w.show_session(monza)
    view, table = w.compare, w.compare.corner_table
    found = corner_engine.find_corners(monza)

    # the map marks every corner's zone and labels it, inferred corners with a "?"
    assert len(w.map.labels) == len(found) and w.map.labels[0].toPlainText() == "T1 ?"
    assert len(w.map.zone_ticks.getData()[0]) == 2 * len(found)

    w.load_laps([4])
    assert table.rowCount() == 0 and "Load a second lap" in view.corner_note.text()
    w.load_laps([4, 1])
    app.processEvents()
    assert table.rowCount() == len(found) and "lap 1 against lap 4 (reference)" in view.corner_title.text()
    assert "inferred from the data" in view.corner_note.text() and "turn-in" in view.corner_note.toolTip()
    assert not view.against.isVisible(), "with two laps there is nothing to choose"
    headers = [table.horizontalHeaderItem(k).text() for k in range(table.columnCount())]
    assert headers[2].startswith("Braking point") and headers[5].startswith("Throttle on")
    t = corner_engine.pass_comparison(corner_engine.analyse(monza)["metrics"], found, 4, 1).set_index("corner")
    row = next(r for r in range(table.rowCount()) if table.item(r, 0).data(0x0100) == "T1")
    assert table.item(row, 1).text() == f"{t.loc['T1', 'd_zone_time_s']:+.2f}"
    assert table.item(row, 2).text() == f"{t.loc['T1', 'ref_brake_before_m']:.0f} → {t.loc['T1', 'lap_brake_before_m']:.0f}"
    assert table.item(row, 7).text() == t.loc["T1", "finding"]
    assert table.item(row, 0).text().endswith("· inferred")

    # a click zooms the charts to the corner, puts the cursor on its apex and lights it on the map
    table.selectRow(row)
    c = found.set_index("corner").loc["T1"]
    lo, hi = view._plots[0].viewRange()[0]
    assert lo <= c.zone_start_m + 5 and hi >= c.zone_end_m - 5 and hi - lo < 2 * (c.zone_end_m - c.zone_start_m)
    assert view.cursor_x == pytest.approx(c.apex_m)
    assert len(w.map.zone_lit.getData()[0]) > 0

    # with three laps the comparison lap can be chosen
    w.load_laps([4, 1, 2])
    assert view.against.isVisibleTo(view) and view.against.count() == 2
    view.against.setCurrentIndex(1)
    assert "lap 2 against lap 4" in view.corner_title.text()
    shot = os.environ.get("VELTRIX_SCREENSHOT")
    if shot:
        app.processEvents()
        w.grab().save(shot.replace(".png", "_corners.png"))
    w.close()
