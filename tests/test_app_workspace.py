"""The interactive workspace: its state (no window needed), and the window itself driven offscreen."""

import json
import os

import numpy as np
import pytest

from conftest import GOLDEN
from veltrix_app.workspace import Workspace, default_layout
from veltrix_engine import load_session, maths

LOGGER = GOLDEN / "logger_layout_3lap_20hz.csv"


@pytest.fixture()
def session():
    return load_session(LOGGER)


# ── state ─────────────────────────────────────────────────────────────────────

def test_default_layout_uses_what_the_session_has(session):
    ws = default_layout(session.model)
    assert [c.channels for c in ws.charts] == [["speed"], ["throttle", "brake"], ["steering"], ["acc_lat", "acc_long"],
                                               ["rpm", "gear"]]
    assert ws.window() == ws.extent() == (0.0, pytest.approx(242.35)) and ws.cursor == 0.0


def test_charts_and_channels_can_be_added_and_removed(session):
    ws = Workspace(session.model)
    a = ws.add_chart(["speed"])
    b = ws.add_chart()
    assert ws.add_channel(b, "rpm") and not ws.add_channel(b, "rpm"), "a channel is on a chart once"
    ws.add_channel(b, "tyre_temp_fl_mid")
    ws.remove_channel(b, "rpm")
    ws.remove_chart(a)
    assert [c.channels for c in ws.charts] == [["tyre_temp_fl_mid"]]


def test_drawing_is_decimated_but_reading_is_exact(session):
    ws = default_layout(session.model)
    drawn = ws.series("speed", width_px=300)
    speed = session.model.get("speed")
    assert drawn["decimated"] and drawn["points"] <= 600 < drawn["samples"] == len(speed.values)
    assert np.nanmax(drawn["v"]) == np.nanmax(speed.values), "the peak is still on the chart"
    ws.zoom(100.0, 102.0)
    close = ws.series("speed", width_px=300)
    assert not close["decimated"] and close["unit"] == "km/h", "zoomed in, the samples themselves are drawn"
    on, between = speed.timestamps[2000], speed.timestamps[2000] + 0.02
    ws.set_cursor(on)
    r = ws.readout()[0][0]
    assert (r.channel, r.value, r.how, r.unit, r.label) == ("speed", speed.values[2000], "sample", "km/h", "measured")
    ws.set_cursor(between)
    flat = [x for chart in ws.readout() for x in chart]
    by = {x.channel: x for x in flat}
    assert by["speed"].how == "interpolated" and by["gear"].how == "held"
    assert by["speed"].value == pytest.approx(np.interp(between, speed.timestamps, speed.values))
    assert ws.lap_at().lap == 2 and ws.lap_at(0.0).lap == 1


def test_view_and_cursor_stay_inside_the_session(session):
    ws = default_layout(session.model)
    assert ws.zoom(-50, 1e6) == ws.extent()
    assert ws.zoom(200, 100) == (100.0, 200.0), "a range given backwards is put right"
    assert ws.pan(1e6) == (pytest.approx(142.35), pytest.approx(242.35)) and ws.pan(-1e6)[0] == 0.0
    assert ws.set_cursor(1e9) == pytest.approx(242.35) and ws.set_cursor(-5) == 0.0
    t0, t1 = ws.zoom(10.0, 10.0)
    assert t1 - t0 == pytest.approx(1e-3)


def test_a_maths_channel_is_charted_like_any_other(session):
    mset = maths.MathSet()
    mset.add("CombinedG", "sqrt(acc_lat^2 + acc_long^2)")
    maths.compute(session.model, mset)
    ws = default_layout(session.model)
    ws.add_chart(["CombinedG"])
    ws.set_cursor(50.0)
    r = ws.readout()[-1][0]
    assert r.label == "calculated" and r.unit == "g" and r.value > 0
    assert ws.series("CombinedG", 500)["label"] == "calculated"


def test_a_workspace_reopens_and_survives_missing_channels(tmp_path, session):
    ws = default_layout(session.model)
    ws.name = "Race engineer"
    ws.add_chart(["tyre_temp_fl_mid", "NotLogged"], title="Tyres")
    ws.zoom(80.0, 160.0)
    ws.set_cursor(120.5)
    path = ws.save(tmp_path / "ws.json")
    assert json.loads(path.read_text())["version"] == 1
    back = Workspace.load(path, session.model)
    assert back.to_dict() == ws.to_dict() and back.missing() == ["NotLogged"]
    assert back.series("NotLogged", 500) is None and back.readout()[-1][1].how == "none"
    other = load_session(GOLDEN / "monza_ac_gt3.csv.gz")           # a session without half of those channels
    moved = Workspace.load(path, other.model)
    assert len(moved.charts) == len(ws.charts), "the layout is kept"
    assert set(moved.missing()) >= {"acc_lat", "acc_long", "tyre_temp_fl_mid", "NotLogged"}
    data = json.loads(path.read_text())
    data["version"] = 99
    with pytest.raises(ValueError, match="newer release"):
        Workspace.from_dict(data)


# ── the window, offscreen ─────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def qt():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_window_shows_a_session_end_to_end(qt, tmp_path, session):
    from veltrix_app.window import MainWindow
    w = MainWindow(tmp_path / "maths.json")
    w.resize(1400, 850)
    w.show()
    w.show_session(session, str(LOGGER))
    qt.processEvents()
    assert len(w._plots) == 5 and w.browser.rowCount() >= len(session.mapping) - 1
    assert "16 channels" in w.statusBar().currentMessage() or "channels of" in w.statusBar().currentMessage()

    w.search.setText("tyre fl")
    shown = [w.browser.item(r, 0).text() for r in range(w.browser.rowCount()) if not w.browser.isRowHidden(r)]
    assert shown == ["tyre_temp_fl_mid"]
    w.search.setText("")

    w.select_chart(0)
    w.add_channel("tyre_temp_fl_mid")
    assert w.ws.charts[0].channels == ["speed", "tyre_temp_fl_mid"]
    w.add_chart(["rpm"])
    assert len(w._plots) == 6 and w.active == 5

    w.set_cursor(100.02)
    qt.processEvents()
    assert all(line.value() == pytest.approx(100.02) for line in w._cursors), "one cursor on every chart"
    assert "lap 2" in w.where.text()
    cells = {w.readout.item(r, 0).text(): (w.readout.item(r, 1).text(), w.readout.item(r, 3).text())
             for r in range(w.readout.rowCount())}
    assert cells["speed"][1] == "≈ interpolated" and cells["gear"][1] == "↦ held"
    assert float(cells["speed"][0]) == pytest.approx(session.model.get("speed").at([100.02])[0][0], rel=1e-3)

    w._plots[0].setXRange(90, 110, padding=0)
    qt.processEvents()
    w._draw_curves()
    assert w.ws.window() == (pytest.approx(90), pytest.approx(110))
    x, _ = w._curves[0][0].getData()
    assert 80 < x.min() <= 90 and 110 <= x.max() < 120, "only what is in view is drawn"

    saved = tmp_path / "layout.json"
    w.save_workspace(str(saved))
    w.remove_chart()
    w.open_workspace(str(saved))
    assert len(w._plots) == 6

    shot = os.environ.get("VELTRIX_SCREENSHOT")
    if shot:
        w.reset_view()
        qt.processEvents()
        w._draw_curves()
        w.set_cursor(120.0)
        w.grab().save(shot)
    w.close()


def test_window_loads_in_the_background_and_reports_a_bad_file(qt, tmp_path, monkeypatch):
    from PySide6 import QtCore, QtWidgets
    from veltrix_app.window import MainWindow
    said = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", lambda *a: said.append(a[2]))
    w = MainWindow(tmp_path / "maths.json")

    def wait(done, seconds=30):
        loop, timer = QtCore.QEventLoop(), QtCore.QElapsedTimer()
        timer.start()
        while not done() and timer.elapsed() < seconds * 1000:
            loop.processEvents(QtCore.QEventLoop.AllEvents, 50)
        assert done()

    w.open_file(str(LOGGER))
    assert w.session is None, "the read is on another thread"
    wait(lambda: w.session is not None)
    assert len(w._plots) == 5 and "logger_layout_3lap_20hz.csv" in w.windowTitle()

    bad = tmp_path / "junk.bin"
    bad.write_bytes(bytes(range(256)) * 8)
    w.open_file(str(bad))
    wait(lambda: bool(said))
    assert "not a format this engine reads" in said[0] and len(w._plots) == 5, "the open session is untouched"
    w.close()


def test_maths_editor_validates_previews_saves_plots_and_reloads(qt, tmp_path, session):
    from veltrix_app.window import MainWindow
    store = tmp_path / "maths.json"
    w = MainWindow(store)
    w.resize(1400, 850)
    w.show()
    w.show_session(session, str(LOGGER))
    assert "No maths channels yet" in w.maths.status.text()
    w.maths.open_editor()
    ed = w.maths.editor
    assert ed.isVisible() and ed.channels.rowCount() >= len(session.mapping) - 1, "the session's channels are there to pick from"

    # live validation: where the error is, and that units are checked while typing
    ed.name.setText("CombinedG")
    ed.formula.setPlainText("sqrt(acc_lat^2 + acc_long^2")
    bad = ed.validate()
    assert not bad["ok"] and ed.status.text().startswith("✗ Expected ',' or ')'") and len(ed.formula.extraSelections()) == 1
    assert ed.last_preview is None and "Preview:" in ed.stats.text()
    ed.formula.setPlainText("sqrt(acc_lat^2 + speed^2)")
    assert not ed.validate()["ok"] and "Cannot add g^2 and (km/h)^2" in ed.status.text()
    assert ed.formula.extraSelections()[0].cursor.selectionStart() == 15, "the mark sits on the operator that cannot be done"
    ed.formula.setPlainText("sqrt(acc_lat^2 + Nonsense^2)")
    ed.validate()
    assert "no channel named Nonsense" in ed.status.text() and not ed._is_known("Nonsense") and ed._is_known("acc_lat")

    # a valid formula: unit, inputs, a preview drawn from it — and nothing saved yet
    ed.formula.clear()
    ed.search.setText("acc")
    shown = [ed.channels.item(r, 0).text() for r in range(ed.channels.rowCount()) if not ed.channels.isRowHidden(r)]
    assert {"acc_lat", "acc_long"} <= set(shown) and "speed" not in shown
    ed.insert("sqrt(")
    ed.insert("acc_lat")                                      # what a double-click in the channel list does
    ed.insert("^2 + acc_long^2)")
    info = ed.validate()
    assert info["ok"] and info["unit"] == "g" and ed.status.text().startswith("✓ gives g   ·   uses acc_lat, acc_long")
    assert ed.formula.extraSelections() == [] and ed.last_preview is not None and ed.last_preview.unit == "g"
    x, y = ed.curve.getData()
    assert len(x) > 100 and np.nanmax(y) == pytest.approx(np.nanmax(ed.last_preview.values))
    assert "samples at 20 Hz" in ed.stats.text() and "in view: min" in ed.stats.text()
    assert not session.model.has("CombinedG") and ed.is_modified() and "unsaved changes" in ed.windowTitle()
    w.ws.zoom(100.0, 110.0)                                   # the preview follows what the charts show
    ed.validate()
    assert 99.0 < ed.curve.getData()[0].min() and ed.curve.getData()[0].max() < 111.0
    w.ws.reset_view()

    # save: computed, listed, in the browser, with its provenance
    assert ed.save_channel() and store.exists() and not ed.is_modified()
    assert session.model.get("CombinedG").unit == "g"
    assert [w.maths.list.item(r, 0).text() for r in range(w.maths.list.rowCount())] == ["CombinedG"]
    assert w.maths.list.item(0, 1).text() == "g" and "1 maths channel(s)" in w.maths.status.text()
    assert "CombinedG" in [w.browser.item(r, 0).text() for r in range(w.browser.rowCount())]
    assert "inputs: normalised:acc_lat, normalised:acc_long" in ed.provenance.toPlainText()
    w.select_chart(0)
    ed.plot_requested.emit("CombinedG")
    assert w.ws.charts[0].channels == ["speed", "CombinedG"]

    # a second channel on the first, with a parameter; then the dependency view
    ed.new_channel()
    ed.name.setText("Hard")
    ed.formula.setPlainText("CombinedG > $limit")
    assert not ed.validate()["ok"] and "$limit is not defined" in ed.status.text()
    ed.set_parameter("limit", 1.2)
    assert ed.validate()["ok"] and ed.parameters.item(0, 0).text() == "limit" and ed.save_channel()
    assert session.model.get("Hard").discrete and session.model.get("Hard").provenance.parameters["$limit"] == 1.2

    def tree_text(item, depth=0):
        return [("  " * depth) + item.text(0) + " | " + item.text(1)] + [x for i in range(item.childCount())
                                                                         for x in tree_text(item.child(i), depth + 1)]
    tree = "\n".join(tree_text(ed.tree.topLevelItem(0)))
    assert "Hard | computed 2 of 2" in tree and "CombinedG | maths channel: sqrt(acc_lat^2 + acc_long^2)" in tree
    assert "acc_lat | normalised:acc_lat  [g]" in tree and "$limit | = 1.2" in tree
    ed.load("CombinedG")
    assert "Hard | CombinedG > $limit" in "\n".join(tree_text(ed.tree.topLevelItem(0))), "and what is built on it"

    # a parameter change reaches the saved channels
    before = float(session.model.get("Hard").values.sum())
    ed.set_parameter("limit", 0.5)
    assert float(session.model.get("Hard").values.sum()) > before

    # renaming and deleting are refused while something depends on the channel
    ed.load("CombinedG")
    ed.name.setText("TotalG")
    assert not ed.save_channel() and "is used by Hard" in ed.status.text() and "CombinedG" in ed.mset.channels
    ed.revert()
    assert ed.name.text() == "CombinedG"
    assert not ed.delete_channel("CombinedG") and "is used by Hard" in ed.status.text()

    # a formula that parses is saved even if this session lacks its input
    ed.new_channel()
    ed.name.setText("Broken")
    ed.formula.setPlainText("NotLogged * 2")
    assert ed.save_channel() and "no channel named NotLogged" in ed.results["Broken"].message
    assert "Not calculated for this session: Broken" in w.maths.status.text()
    ed.duplicate_channel()
    assert ed.name.text() == "Broken2" and ed._editing is None
    shot = os.environ.get("VELTRIX_EDITOR_SCREENSHOT")
    if shot:
        ed.load("Hard")
        ed.tabs.setCurrentIndex(3)
        qt.processEvents()
        ed.grab().save(shot)
    w.close()

    again = MainWindow(store)                              # a restart
    assert list(again.maths.mset.channels) == ["CombinedG", "Hard", "Broken"] and again.maths.mset.parameters == {"limit": 0.5}
    again.show_session(load_session(LOGGER), str(LOGGER))
    assert again.session.model.has("Hard") and "Broken" in again.statusBar().currentMessage()
    again.ws.add_chart(["Hard"])
    again.maths.list.selectRow(1)
    assert again.maths.selected() == "Hard" and again.maths.delete_channel()
    assert "Hard" not in again.maths.mset.channels and not again.session.model.has("Hard")
    assert all("Hard" not in c.channels for c in again.ws.charts), "a deleted channel leaves the charts"
    again.maths.list.selectRow(0)
    assert again.maths.delete_channel() and not again.session.model.has("CombinedG"), "free to go once nothing uses it"
    again.close()


# ── track map ─────────────────────────────────────────────────────────────────

def test_track_map_follows_the_cursor_and_says_where_it_came_from(session):
    from veltrix_app.trackmap import TrackMap
    monza = load_session(GOLDEN / "monza_ac_gt3.csv.gz")
    measured, rebuilt = TrackMap(monza), TrackMap(session)
    assert measured.measured and measured.source == "Logged position."
    assert not rebuilt.measured and "not surveyed" in rebuilt.source, "a rebuilt map is never passed off as measured"

    lap = monza.lap_row(2)
    t = lap.t_start + 40.0
    d = measured.distance_at(t)
    x, y = measured.position(t)
    assert measured.nearest(x, y) == pytest.approx(d, abs=3.0), "the marker sits at the cursor's distance round the lap"
    assert measured.time_at(2, d) == pytest.approx(t, abs=0.05), "and a click there comes back to the same moment"
    assert measured.time_at(2, 1e9) is None and measured.time_at(99, 100.0) is None
    assert measured.position(-100.0) is None

    speed = measured.colour_values("speed", 2)
    assert speed["unit"] == "km/h" and len(speed["values"]) == len(measured.x)
    k = len(measured.dist) // 3
    frame = monza.channels[monza.channels["lap"] == 2]
    near = frame.iloc[(frame["lap_distance"] - measured.dist[k]).abs().argmin()]
    assert speed["values"][k] == pytest.approx(near["speed"], abs=2.0)
    assert measured.colour_values("nope", 2) is None


def test_a_session_that_cannot_give_a_map_gets_none():
    from veltrix_app.trackmap import TrackMap
    from veltrix_engine import build_session
    from veltrix_engine.importers import parse_csv_text
    t = np.arange(0, 30, 0.1)
    s = build_session(parse_csv_text("Time,Speed\n" + "\n".join(f"{a:.1f},{100 + a:.1f}" for a in t)))
    with pytest.raises((ValueError, KeyError)):
        TrackMap(s)


def test_map_dock_in_the_window(qt, tmp_path):
    from veltrix_app.window import MainWindow
    monza = load_session(GOLDEN / "monza_ac_gt3.csv.gz")
    w = MainWindow(tmp_path / "maths.json")
    w.resize(1400, 850)
    w.show()
    w.show_session(monza)
    lap = monza.lap_row(2)
    w.set_cursor(lap.t_start + 40.0)
    assert w.map.lap == 2 and w.map.colour.currentData() == "speed" and "speed on lap 2" in w.map.scale.text()
    mx, my = w.map.marker.getData()
    assert (mx[0], my[0]) == pytest.approx(w.map.map.position(lap.t_start + 40.0))
    target = w.map.map.time_at(2, 5000.0)
    w.map.moved.emit(target)                                # what a click on the map sends
    assert w.ws.cursor == pytest.approx(target) and all(c.value() == pytest.approx(target) for c in w._cursors)
    w.map.colour.setCurrentIndex(w.map.colour.findData("throttle"))
    assert "throttle on lap 2" in w.map.scale.text()
    shot = os.environ.get("VELTRIX_MAP_SCREENSHOT")
    if shot:
        w.map.raise_()
        qt.processEvents()
        w.grab().save(shot)
    w.close()


# ── events and annotations ────────────────────────────────────────────────────

def sliding_session():
    from test_advice import staged_session

    def slide(raw, rows, bump):
        raw.loc[rows, "SteeringAngle"] -= 1.7 * raw.loc[rows, "SteeringAngle"] * bump
        raw.loc[rows, "ThrottlePos"] = 100.0
    return staged_session({"where": [(lap, 1262, 1300) for lap in (3, 6, 8)], "do": slide})


def test_events_are_candidates_and_annotations_are_kept_apart(tmp_path):
    from veltrix_app.events import AnnotationStore, annotate, describe, detect_events
    s = sliding_session()
    events, summary = detect_events(s)
    assert "candidate event(s) for review" in summary and "possible balance incident(s)" in summary
    balance = [e for e in events if "corner" in e.evidence]             # from the steering-norm detector
    by_rule = [e for e in events if e.rule == "oversteer"]              # from the configurable rule engine
    assert len(balance) >= 3 and len(by_rule) == 3, "two detectors, each finding the three staged slides"
    assert all(any(abs(b.t_start - r.t_start) < 2 for b in balance) for r in by_rule), "and agreeing on when"
    assert "Rules run:" in summary and "Not looked for: wheelspin" in summary, "no wheel speeds in this file"
    row = describe(balance[0])
    assert row["what"] == "Possible oversteer — requires review" and row["where"] == "T4 exit" and row["label"] == "detected event"
    assert describe(by_rule[0])["detail"].startswith("peak ") and describe(by_rule[0])["rule"] == "oversteer"
    assert s.lap_row(row["lap"]).t_start < row["time"] < s.lap_row(row["lap"]).t_end

    monza = load_session(GOLDEN / "monza_ac_gt3.csv.gz")
    none, why = detect_events(monza)
    assert "Balance incidents were not looked for:" in why and not [e for e in none if "corner" in e.evidence]

    note = annotate(s.model, "  Driver reported a snap on exit ", time=row["time"], category="driver", author="RE")
    whole = annotate(s.model, "New front tyres")
    assert (note.lap, note.scope, note.text) == (row["lap"], "time", "Driver reported a snap on exit")
    assert note.position_m == pytest.approx(row["position_m"], abs=30) and whole.scope == "session" and whole.time is None
    with pytest.raises(ValueError, match="needs some text"):
        annotate(s.model, "   ")
    s.model.events = events
    assert all(a.text != e.kind for a in s.model.annotations for e in s.model.events)

    store = AnnotationStore(tmp_path / "notes")
    store.save(s.raw.sha256 or "abc123", s.model.annotations)
    back = store.load(s.raw.sha256 or "abc123")
    assert [a.text for a in back] == ["Driver reported a snap on exit", "New front tyres"] and back[0].author == "RE"
    assert store.load("neverseen") == [] and store.load("../../etc/passwd") == [], "a bad key reads nothing"
    with pytest.raises(ValueError):
        store.save("../escape", back)


def test_events_and_notes_in_the_window(qt, tmp_path):
    from veltrix_app.window import MainWindow
    s = sliding_session()
    s.raw.sha256 = "feedc0de"
    w = MainWindow(tmp_path / "maths.json")
    w.resize(1400, 900)
    w.show()
    w.show_session(s)
    assert w.events.events.rowCount() >= 3 and "requires review" in w.events.events.item(0, 1).text()
    when = w.events.events.item(0, 0).data(0x0100)
    w.events.jump.emit(when)                               # what a click on the row sends
    assert w.ws.cursor == pytest.approx(when) and w.ws.window()[1] - w.ws.window()[0] == pytest.approx(10.0)

    w.events.text.setText("Snap on exit, caught it")
    w.events.category.setText("driver")
    w.events._add()
    assert w.events.notes.rowCount() == 1 and w.events.notes.item(0, 3).text() == "Snap on exit, caught it"
    assert len(w._note_lines) == len(w._plots), "the note is marked on every chart"
    assert w.events.events.rowCount() >= 3, "a note is not an event"
    w.close()

    again = MainWindow(tmp_path / "maths.json")            # the same file opened another day
    again.show_session(s)
    assert [a.text for a in again.session.model.annotations] == ["Snap on exit, caught it"]
    again.remove_annotation(0)
    assert again.events.notes.rowCount() == 0 and again.notes.load("feedc0de") == []
    again.close()


# ── import mapping ────────────────────────────────────────────────────────────

def test_mapping_dialog_lets_a_person_check_change_and_save_the_mapping(qt, tmp_path):
    from PySide6 import QtWidgets
    from test_mapping_profiles import table
    from veltrix_app.mapping_dialog import MappingDialog
    from veltrix_app.window import MainWindow
    from veltrix_engine.mapping_profiles import Profile
    raw = table({"vCar": lambda t: 40 + 10 * np.sin(t / 5), "nEngine": lambda t: 7000 + 500 * np.sin(t),
                 "Throttle": lambda t: 5000 + 0 * t, "Lap": lambda t: 1 + (t // 20)}, {"nEngine": "rpm", "Throttle": "%"})
    d = MappingDialog(raw, profiles_dir=tmp_path / "profiles")
    rows = {d.table.item(r, 0).text(): r for r in range(d.table.rowCount())}
    assert d.table.cellWidget(rows["vCar"], 1).currentData() == "speed" and d.table.item(rows["vCar"], 2).text() == "unknown"
    assert d.table.item(rows["vCar"], 3).text() == "name + values (suggestion)"
    assert d.table.cellWidget(rows["Throttle"], 1).currentData() is None and "Values do not fit the channel for: Throttle" in d.summary.text()
    assert "outside the -5 to 105" in d.table.item(rows["Throttle"], 6).text()
    assert "No unit in the file for: vCar" in d.summary.text() and "0 confirmed" in d.summary.text()

    d.set_unit("vCar", "m/s")
    assert d.table.item(rows["vCar"], 2).text() == "m/s" and "No unit in the file" not in d.summary.text()
    d.set_channel("nEngine", "rpm")
    assert d.table.item(rows["nEngine"], 3).text() == "you"
    d.confirm_all()
    assert "4 confirmed" in d.summary.text(), "time, speed, rpm and lap; the throttle column is not mapped"
    mapping, units = d.import_args()
    assert mapping == {"time": "Time", "speed": "vCar", "rpm": "nEngine", "lap": "Lap"} and units == {"vCar": "m/s"}

    saved = d.save_profile(tmp_path / "profiles" / "team_a.json", "Team A")
    assert {r.pattern: (r.channel, r.unit, r.confirmed) for r in saved.rules}["vCar"] == ("speed", "m/s", True)
    fresh = MappingDialog(raw)
    fresh.load_profile(tmp_path / "profiles" / "team_a.json")
    assert fresh.table.item(rows["vCar"], 3).text() == "verified profile" and fresh.table.item(rows["vCar"], 2).text() == "m/s"
    assert Profile.load(tmp_path / "profiles" / "team_a.json").name == "Team A"

    w = MainWindow(tmp_path / "maths.json")
    d.exec = lambda: QtWidgets.QDialog.Accepted            # the user pressed Import
    w.map_and_show(raw, "team.csv", dialog=d, wait=True)
    assert w.session is not None and w.session.mapping["speed"] == "vCar"
    assert w.session.channels["speed"].max() == pytest.approx(50 * 3.6, rel=1e-3) and len(w.session.laps) == 3
    assert w.session.model.get("speed", "normalised").provenance.unit_source == "stated"
    d.exec = lambda: QtWidgets.QDialog.Rejected
    before = w.session
    w.map_and_show(raw, "team.csv", dialog=d, wait=True)
    assert w.session is before and "cancelled" in w.statusBar().currentMessage()
    w.close()


# ── a marked range, and an axis per unit ──────────────────────────────────────

def test_range_statistics_come_from_every_sample_in_the_range(tmp_path, session):
    ws = default_layout(session.model)
    assert ws.range_stats() == [] and ws.selection is None
    assert ws.select(120.0, 100.0) == (100.0, 120.0), "given backwards, put right"
    rows = {r["channel"]: r for r in ws.range_stats()}
    speed = session.model.get("speed")
    inside = (speed.timestamps >= 100.0) & (speed.timestamps <= 120.0)
    v = speed.values[inside]
    assert rows["speed"]["samples"] == inside.sum() == 401
    assert (rows["speed"]["min"], rows["speed"]["max"]) == (v.min(), v.max())
    assert rows["speed"]["mean"] == pytest.approx(v.mean()) and rows["speed"]["std"] == pytest.approx(v.std())
    assert rows["speed"]["start"] == pytest.approx(speed.at([100.0])[0][0]) and rows["speed"]["unit"] == "km/h"
    assert rows["speed"]["change"] == pytest.approx(rows["speed"]["end"] - rows["speed"]["start"])
    assert rows["gear"]["change"] is None, "a gear number has no meaningful change"
    assert len(rows) == len({c for chart in ws.charts for c in chart.channels}), "each charted channel once"

    ws.add_chart(["NotLogged", "speed"])
    assert "NotLogged" not in {r["channel"] for r in ws.range_stats()}
    assert ws.select(-50, 1e9) == ws.extent() and ws.select(5.0, 5.0) is None and ws.select(None) is None
    ws.select(30.0, 45.5)
    back = Workspace.load(ws.save(tmp_path / "ws.json"), session.model)
    assert back.selection == (30.0, 45.5), "the marked range is part of the workspace"


def test_units_on_a_chart_each_get_an_axis(session):
    ws = Workspace(session.model)
    ws.add_chart(["speed", "tyre_temp_fl_mid", "tyre_temp_fr_mid", "rpm", "NotLogged"])
    ws.add_chart(["throttle"])
    assert ws.units_of(0) == ["km/h", "°C", "rpm"] and ws.units_of(1) == ["%"]


def test_axes_and_range_in_the_window(qt, tmp_path, session):
    from veltrix_app.window import MainWindow
    w = MainWindow(tmp_path / "maths.json")
    w.resize(1500, 900)
    w.show()
    w.show_session(session, str(LOGGER))
    w.select_chart(0)
    w.add_channel("tyre_temp_fl_mid")
    w.add_channel("rpm")
    qt.processEvents()
    w._draw_curves()
    assert len(w._extra_views) == 4, "two more on speed's chart; throttle/brake and rpm/gear each had a second already"
    plot = w._plots[0]
    assert plot.getAxis("left").labelText == "km/h" and plot.getAxis("right").labelText == "°C"
    speed_curve, temp_curve, rpm_curve = w._curves[0]
    assert temp_curve.getViewBox() is not plot.vb and rpm_curve.getViewBox() not in (plot.vb, temp_curve.getViewBox())
    lo, hi = temp_curve.getViewBox().viewRange()[1]
    assert 40 < lo < hi < 140, "the temperature has its own scale, not the speed's"
    assert np.nanmax(temp_curve.getData()[1]) == pytest.approx(np.nanmax(session.model.get("tyre_temp_fl_mid").values))
    assert plot.key == ["speed [km/h]", "tyre_temp_fl_mid [°C]", "rpm [rpm]"], "named above the chart"
    assert plot.legend is None, "no legend laid over the curves"
    w.set_cursor(100.0)
    assert all(c.value() == 100.0 for c in w._cursors), "the extra axes do not get in the cursor's way"

    assert "No range marked" in w.range_label.text()
    w.range_action.trigger()                               # Charts ▸ Mark a range
    lo, hi = w.ws.extent()
    assert w.ws.selection == (pytest.approx(lo + (hi - lo) / 3), pytest.approx(hi - (hi - lo) / 3))
    assert len(w._regions) == len(w._plots) and w.range_action.isChecked()
    w._regions[2].setRegion((100.0, 120.0))                # drag it on one chart
    assert w.ws.selection == (100.0, 120.0) and all(r.getRegion() == (100.0, 120.0) for r in w._regions)
    w._show_range()
    assert w.range_label.text().startswith("100.000 s to 120.000 s  ·  20.000 s  ·  lap 2")
    table = {w.range_table.item(r, 0).text(): [w.range_table.item(r, k).text() for k in range(10)]
             for r in range(w.range_table.rowCount())}
    speed = session.model.get("speed")
    v = speed.values[(speed.timestamps >= 100) & (speed.timestamps <= 120)]
    assert table["speed"][1:5] == ["km/h", f"{v.min():.4g}", f"{v.max():.4g}", f"{v.mean():.4g}"] and table["speed"][9] == "401"
    w.zoom_to_range()
    assert w.ws.window() == (pytest.approx(100.0), pytest.approx(120.0))
    saved = tmp_path / "layout.json"
    w.save_workspace(str(saved))
    shot = os.environ.get("VELTRIX_RANGE_SCREENSHOT")
    if shot:
        w.reset_view()
        w.range_dock.raise_()
        qt.processEvents()
        w._draw_curves()
        w.grab().save(shot)
    w.mark_range(False)
    assert w.ws.selection is None and w._regions == [] and not w.range_action.isChecked()
    w.open_workspace(str(saved))
    assert w.ws.selection == (100.0, 120.0) and len(w._regions) == len(w._plots), "back with the workspace"
    w.close()


def test_stacked_charts_line_up_whatever_axes_each_has(qt, tmp_path, session):
    from veltrix_app.window import MainWindow
    w = MainWindow(tmp_path / "maths.json")
    w.resize(1500, 900)
    w.show()
    w.show_session(session, str(LOGGER))
    w.select_chart(0)
    w.add_channel("tyre_temp_fl_mid")
    w.add_channel("rpm")                                    # three units on the first chart, one on the third
    qt.processEvents()
    spans = [(p.vb.sceneBoundingRect().left(), p.vb.sceneBoundingRect().right()) for p in w._plots]
    assert all(a == pytest.approx(spans[0][0], abs=1) and b == pytest.approx(spans[0][1], abs=1) for a, b in spans), spans
    w.close()


# ── mapping details, profiles chosen automatically, channel picker ────────────

def test_how_a_channel_was_mapped_is_shown_in_the_browser_and_a_fitting_profile_is_applied(qt, tmp_path):
    from PySide6 import QtWidgets
    from test_mapping_profiles import table
    from veltrix_app.mapping_dialog import MappingDialog
    from veltrix_app.window import MainWindow
    from veltrix_engine.mapping_profiles import Profile, Rule
    raw = table({"vCar": lambda t: 40 + 10 * np.sin(t / 5), "nEngine": lambda t: 7000 + 500 * np.sin(t),
                 "Throttle": lambda t: 50 + 0 * t, "Lap": lambda t: 1 + (t // 20)}, {"Throttle": "%"})
    w = MainWindow(tmp_path / "maths.json")
    w.profiles_dir.mkdir(parents=True)
    Profile("Team A", rules=[Rule("vCar", "speed", unit="m/s", confirmed=True), Rule("nEngine", "rpm", confirmed=True),
                             Rule("Lap", "lap", confirmed=True)]).save(w.profiles_dir / "team_a.json")
    d = MappingDialog(raw, profiles_dir=w.profiles_dir)          # no profile given: the one that fits is found
    assert d.profile.name == "Team A" and "chosen automatically: 3 of its 3 columns" in d.summary.text()
    rows = {d.table.item(r, 0).text(): r for r in range(d.table.rowCount())}
    assert d.table.item(rows["vCar"], 3).text() == "verified profile" and d.table.item(rows["vCar"], 2).text() == "m/s"
    d.exec = lambda: QtWidgets.QDialog.Accepted
    w.map_and_show(raw, "team.csv", dialog=d, wait=True)
    assert w.session.channels["speed"].max() == pytest.approx(50 * 3.6, rel=1e-3)

    header = [w.browser.horizontalHeaderItem(k).text() for k in range(w.browser.columnCount())]
    assert header[5:7] == ["From", "Mapped by"]
    cells = {w.browser.item(r, 0).text(): (w.browser.item(r, 5).text(), w.browser.item(r, 6).text()) for r in range(w.browser.rowCount())}
    assert cells["speed"] == ("vCar", "profile, confirmed (Team A); unit from the profile")
    assert cells["throttle"] == ("Throttle", "plausibility (Team A); unit from the file")
    assert cells["acc_long_calc"] == ("speed", "calculated")
    assert [m["profile"] for m in w.session.model.mapping if m["channel"] == "rpm"] == ["Team A"]
    w.search.setText("profile")
    shown = {w.browser.item(r, 0).text() for r in range(w.browser.rowCount()) if not w.browser.isRowHidden(r)}
    assert shown == {"speed", "rpm", "lap"}, "the browser can be searched by how a channel was mapped"
    w.close()


def test_a_large_mdf_asks_which_channels_to_load(qt, tmp_path, session):
    asammdf = pytest.importorskip("asammdf")
    from PySide6 import QtWidgets
    from veltrix_app.channel_picker import ChannelPicker
    from veltrix_app.window import MainWindow
    from veltrix_engine.importers import inspect_file
    ref = session.channels
    t = ref["time"].to_numpy()
    mdf = asammdf.MDF(version="4.10")
    mdf.append([asammdf.Signal(ref[c].to_numpy(float), t, name=n, unit=u) for c, n, u in (
        ("speed", "Ground Speed", "km/h"), ("rpm", "Engine RPM", "rpm"), ("steering", "Steered Angle", "deg"),
        ("throttle", "Throttle Pos", "%"), ("lap", "Lap Number", ""))])
    mdf.append([asammdf.Signal(np.full(len(t[::4]), 90.0), t[::4], name=f"Aux{i}", unit="V") for i in range(40)])
    path = tmp_path / "big.mf4"
    mdf.save(path, overwrite=True)
    mdf.close()

    picker = ChannelPicker(inspect_file(path))
    assert picker.table.rowCount() == 45 and "45 channels in 2 group(s), rates 5, 20 Hz" in picker.summary.text()
    assert "1 channel(s) have no unit" in picker.summary.text() and len(picker.chosen()) == 45
    picker.search.setText("aux")
    picker.set_shown(False)                                 # untick the forty spare channels in one go
    picker.search.setText("")
    assert picker.chosen() == ["Ground Speed", "Engine RPM", "Steered Angle", "Throttle Pos", "Lap Number"]
    assert "5 of 45 channels will be loaded" in picker.count.text()

    w = MainWindow(tmp_path / "maths.json")
    picker.exec = lambda: QtWidgets.QDialog.Accepted
    assert w.pick_channels(str(path), dialog=picker) == picker.chosen()
    picker.exec = lambda: QtWidgets.QDialog.Rejected
    assert w.pick_channels(str(path), dialog=picker) is False
    assert w.pick_channels(str(LOGGER)) is None, "a CSV is read whole; there is nothing to pick"
    from veltrix_engine import load_session as load
    s = load(path, channels=picker.chosen())
    assert len(s.raw.data.columns) == 6 and len(s.laps) == 3
    w.close()


# ── event rules, notes on the map, undo, and the report ───────────────────────

def test_event_rule_editor_changes_what_is_found(qt, tmp_path):
    from veltrix_app.rules_editor import RulesEditor
    from veltrix_app.window import MainWindow
    s = sliding_session()
    w = MainWindow(tmp_path / "maths.json")
    w.show_session(s)
    before = [e for e in s.model.events if e.rule == "oversteer"]
    assert len(before) == 3

    ed = RulesEditor(w.data_folder / "event_rules.json", s.model, w)
    titles = [ed.table.item(r, 1).text() for r in range(ed.table.rowCount())]
    assert "Possible oversteer" in titles and ed.parameters.rowCount() == len(ed.rules.parameters)
    report = ed.try_rules()
    row = titles.index("Possible oversteer")
    assert report["oversteer"]["events"] >= 3 and ed.table.item(row, 9).text() == str(report["oversteer"]["events"])
    assert "no channel named" in ed.table.item(titles.index("Possible wheelspin"), 9).text(), "why a rule cannot run is shown"

    ed.table.item(row, 2).setText("speed > $min_speed and oversteer_metric >")       # a formula that does not parse
    assert ed.try_rules() is None and ed.status.text().startswith("✗ Rule 'oversteer', on:") and not ed.save()
    ed.table.item(row, 2).setText("speed > $min_speed and oversteer_metric > $over_metric")
    ed.table.item(row, 0).setCheckState(qt_unchecked())
    ed.add_rule()
    last = ed.table.rowCount() - 1
    ed.table.item(last, 1).setText("Full throttle")
    ed.table.item(last, 2).setText("throttle > $spin_throttle")
    ed.table.item(last, 4).setText("2")
    w.open_rules_editor(dialog=_accepting(ed))               # saved from the dialog: the app looks again
    assert ed.save() and (w.data_folder / "event_rules.json").exists()
    kinds = {e.kind for e in s.model.events}
    assert "Full throttle" in kinds and not [e for e in s.model.events if e.rule == "oversteer"]
    assert w.events.events.rowCount() == len(s.model.events) and "candidate event(s) with the saved rules" in w.statusBar().currentMessage()
    blank = RulesEditor(tmp_path / "x.json")                 # no session open
    assert blank.fit() == [] and "Open a session first" in blank.status.text()
    assert isinstance(ed.fit(), list) and "Nothing is saved" not in ed.status.text()
    ed.use_defaults()
    assert ed.save() and len([e for e in w.session.model.events if e.rule == "oversteer"]) >= 3
    w.close()


def qt_unchecked():
    from PySide6 import QtCore
    return QtCore.Qt.Unchecked


def _accepting(dialog):
    dialog.exec = lambda: 1
    return dialog


def test_notes_are_marked_on_the_map(qt, tmp_path):
    from veltrix_app.window import MainWindow
    monza = load_session(GOLDEN / "monza_ac_gt3.csv.gz")
    w = MainWindow(tmp_path / "maths.json")
    w.show_session(monza)
    assert len(w.map.notes.data) == 0
    lap = monza.lap_row(2)
    w.set_cursor(lap.t_start + 40.0)
    w.add_annotation("Locked the fronts here", "driver")
    w.set_cursor(lap.t_start + 90.0)
    w.add_annotation("Kerb strike")
    assert len(w.map.notes.data) == 2
    x, y = w.map.notes.getData()
    assert (x[0], y[0]) == pytest.approx(w.map.map.position(lap.t_start + 40.0))
    assert w.map.notes.data["data"][0] == "lap 2: Locked the fronts here"
    w.remove_annotation(0)
    assert len(w.map.notes.data) == 1
    w.close()


def test_maths_editor_undo_and_redo(qt, tmp_path, session):
    from veltrix_app.window import MainWindow
    w = MainWindow(tmp_path / "maths.json")
    w.show_session(session, str(LOGGER))
    ed = w.maths.editor
    assert not ed.undo_button.isEnabled() and not ed.undo()
    ed.new_channel()
    ed.name.setText("CombinedG")
    ed.formula.setPlainText("sqrt(acc_lat^2 + acc_long^2)")
    assert ed.save_channel() and ed.undo_button.isEnabled()
    ed.formula.setPlainText("abs(acc_lat)")
    assert ed.save_channel() and np.allclose(session.model.get("CombinedG").values, np.abs(session.model.get("acc_lat").values))
    w.ws.add_chart(["CombinedG"])

    assert ed.undo(), "back to the first formula"
    assert ed.mset.channels["CombinedG"].formula == "sqrt(acc_lat^2 + acc_long^2)" and ed.formula.toPlainText() == "sqrt(acc_lat^2 + acc_long^2)"
    assert session.model.get("CombinedG").values.max() > np.abs(session.model.get("acc_lat").values).max() - 1e-9
    assert ed.undo() and "CombinedG" not in ed.mset.channels and not session.model.has("CombinedG"), "back to before it existed"
    assert not ed.undo_button.isEnabled() and ed.redo_button.isEnabled()
    assert ed.redo() and ed.redo() and ed.mset.channels["CombinedG"].formula == "abs(acc_lat)" and not ed.redo()

    ed.set_parameter("limit", 1.0)
    ed.set_parameter("limit", 2.0)
    assert ed.undo() and ed.mset.parameters == {"limit": 1.0} and ed.parameters.item(0, 1).text() == "1"
    assert ed.delete_channel("CombinedG") and ed.undo() and "CombinedG" in ed.mset.channels, "a delete can be taken back"
    assert __import__("json").loads((tmp_path / "maths.json").read_text())["channels"][0]["name"] == "CombinedG"
    w.close()


def test_a_report_made_from_the_app_or_the_command_line_carries_maths_events_and_notes(qt, tmp_path, monkeypatch, capsys):
    from veltrix_app.window import MainWindow
    from veltrix_engine.cli import main as cli_main
    from PySide6 import QtGui
    opened = []
    monkeypatch.setattr(QtGui.QDesktopServices, "openUrl", lambda url: opened.append(url.toLocalFile()))
    data = tmp_path / "appdata"
    w = MainWindow(data / "maths.json")
    s = load_session(LOGGER)
    w.show_session(s, str(LOGGER))
    ed = w.maths.editor
    ed.name.setText("CombinedG")
    ed.formula.setPlainText("sqrt(acc_lat^2 + acc_long^2)")
    assert ed.save_channel()
    w.set_cursor(100.0)
    w.add_annotation("Tail wind on the straight", "conditions")
    out = w.make_report(tmp_path / "from_app.html")
    html = out.read_text()
    assert opened == [str(out)] and "sqrt(acc_lat^2 + acc_long^2)" in html and "Tail wind on the straight" in html
    assert "CombinedG" in s.frame(), "and the maths channel can be compared lap against lap in it"
    w.close()

    target = tmp_path / "from_cli.html"                     # the same file, later, from Terminal
    assert cli_main(["report", str(LOGGER), "-o", str(target), "--app-data", str(data)]) == 0
    said = capsys.readouterr().out
    assert "1 maths channel(s)" in said and "1 note(s) from the app" in said
    html = target.read_text()
    assert "sqrt(acc_lat^2 + acc_long^2)" in html and "Tail wind on the straight" in html
    assert cli_main(["report", str(LOGGER), "-o", str(target), "--no-app-data"]) == 0
    assert "Tail wind" not in target.read_text() and "from the app" not in capsys.readouterr().out
