"""
End-to-end paths (brief, Phase 6). Each one runs a whole workflow on files the
test builds, checking that every stage hands the next what it needs.

  CSV → model → maths → lap compare → events → report
  MDF → mapping → model → workspace → report
  session → video → sync → seek          (tests/test_video_sync.py, with real playback)
  workspace save → close → reopen        (also tests/test_app_workspace.py, in the window)
"""

import json
import re

import numpy as np
import pytest

from conftest import GOLDEN
from veltrix_app.compare import LapComparison
from veltrix_app.workspace import Workspace, default_layout
from veltrix_engine import ProjectStore, build_session, events, laps as lap_engine, load_session, maths
from veltrix_engine.importers import inspect_file, read_file
from veltrix_engine.mapping_profiles import Profile, Rule, decide, diagnostics, to_import_args
from veltrix_engine.report import render_report, report_data

SYNTH = GOLDEN / "synthetic_gt3_5lap.csv.gz"


def test_csv_to_model_to_maths_to_lap_compare_to_events_to_report(tmp_path):
    s = load_session(SYNTH)
    assert s.model.describe()["channels"]["normalised"] == len(s.mapping) and len(s.model.laps) == 5

    mset = maths.MathSet()
    mset.parameters["hard"] = 1.2
    mset.add("CombinedG", "sqrt(acc_lat^2 + acc_long^2)", description="total acceleration")
    mset.add("BrakePower", "if(acc_long < -0.5, abs(acc_long) * speed, 0)", estimate=True)
    results = maths.compute(s.model, mset)
    assert all(r.ok for r in results.values()) and results["CombinedG"].channel.unit == "g"
    assert maths.on_view(s, results) == ["CombinedG", "BrakePower"]

    cmp_ = lap_engine.compare_laps(s, 2, 3, channels=("speed", "CombinedG"))
    assert cmp_["delta_t"].iloc[-1] == pytest.approx(s.lap_row(3).lap_time_s - s.lap_row(2).lap_time_s, abs=0.02)
    assert cmp_["CombinedG_cmp"].notna().mean() > 0.95, "the maths channel is compared lap against lap"

    rules = events.default_rules()
    rules.rules.append(events.Rule("hard_cornering", "Sustained high combined g", "CombinedG > $hard", min_duration_s=0.5,
                                   severity="CombinedG", evidence=("CombinedG", "speed")))
    rules.parameters["hard"] = 1.2
    found, report = events.detect(s.model, rules)
    hard = [e for e in found if e.rule == "hard_cornering"]
    assert report["hard_cornering"]["ran"] and len(hard) > 5, "a rule built on a maths channel"
    assert all(e.evidence["CombinedG"]["max"] > 1.2 and e.lap in (1, 2, 3, 4, 5) for e in hard)
    assert not report["wheelspin"]["ran"], "this file has no wheel speeds, and the report says so"
    s.model.events = found

    data = report_data(s)
    assert [m["name"] for m in data["data"]["maths"]][:2] == ["CombinedG", "BrakePower"], "then the event rules' helper channels"
    assert data["data"]["maths"][1]["estimate"] and data["data"]["event_counts"]["Sustained high combined g"] == len(hard)
    assert {"channel": "speed", "column": "Speed", "unit": "km/h", "unit_source": "assumed"}.items() <= data["data"]["mapping"][
        [m["channel"] for m in data["data"]["mapping"]].index("speed")].items()
    assert any("no unit for" in t and "speed" in t for t in data["data"]["limitations"])
    assert any("candidates found by threshold rules" in t for t in data["data"]["limitations"])
    page = tmp_path / "report.html"
    page.write_text(render_report(s))
    html = page.read_text()
    assert 'data-tab="data"' in html and "sqrt(acc_lat^2 + acc_long^2)" in html and "Sustained high combined g" in html
    payload = json.loads(re.search(r"const D = (\{.*\});\n", html).group(1).replace("<\\/", "</"))
    assert payload["engineering"]["sections"][0]["title"] == "Lap potential"


def test_mdf_to_mapping_to_model_to_workspace_to_report(tmp_path):
    asammdf = pytest.importorskip("asammdf")
    ref = load_session(SYNTH).channels
    t, slow = ref["time"].to_numpy(), ref.iloc[::10]
    mdf = asammdf.MDF(version="4.10")
    mdf.append([asammdf.Signal(ref["speed"].to_numpy() / 3.6, t, name="vCar"),
                asammdf.Signal(ref["steering"].to_numpy(), t, name="aSteer", unit="deg"),
                asammdf.Signal(ref["acc_lat"].to_numpy(), t, name="gLat", unit="g"),
                asammdf.Signal(ref["acc_long"].to_numpy(), t, name="gLong", unit="g"),
                asammdf.Signal(ref["throttle"].to_numpy(), t, name="rThrottle", unit="%"),
                asammdf.Signal(ref["lap"].to_numpy(np.int32), t, name="NLap")])
    mdf.append([asammdf.Signal(slow["tyre_temp_fl_mid"].to_numpy(), slow["time"].to_numpy(), name="TTyreFL", unit="C")])
    path = tmp_path / "car.mf4"
    mdf.save(path, overwrite=True)
    mdf.close()

    info = inspect_file(path)
    assert info["group_count"] == 2 and "vCar" in info["missing_units"] and info["rates_hz"] == [10.0, 100.0]
    profile = Profile("Team A", rules=[Rule("vCar", "speed", unit="m/s", confirmed=True), Rule("aSteer", "steering", confirmed=True),
                                       Rule("gLat", "acc_lat", confirmed=True), Rule("gLong", "acc_long", confirmed=True),
                                       Rule("rThrottle", "throttle", confirmed=True), Rule("NLap", "lap", confirmed=True),
                                       Rule("TTyreFL", "tyre_temp_fl", confirmed=True)])
    raw = read_file(path)
    decisions = decide(raw, profile)
    assert diagnostics(raw, decisions)["by_basis"]["profile"] == 7
    mapping, units = to_import_args(decisions)
    s = build_session(raw, mapping=mapping, units=units)

    m = s.model
    assert m.get("tyre_temp_fl").sample_rate == pytest.approx(10) and m.get("speed").sample_rate == pytest.approx(100)
    assert np.allclose(s.channels["speed"], ref["speed"], atol=1e-6) and len(s.laps) == 5

    ws = default_layout(m)
    ws.add_chart(["tyre_temp_fl"])
    ws.set_cursor(100.03)
    by = {r.channel: r for chart in ws.readout() for r in chart}
    assert by["tyre_temp_fl"].how == "interpolated" and by["speed"].unit == "km/h"
    assert ws.series("tyre_temp_fl", 400)["samples"] == len(slow), "drawn from its own 10 Hz samples"
    cmp_ = LapComparison(m)
    assert cmp_.delta(cmp_.laps[1])["final_s"] == pytest.approx(
        s.lap_row(cmp_.laps[1]).lap_time_s - s.lap_row(cmp_.laps[0]).lap_time_s)

    data = report_data(s)
    by_channel = {r["channel"]: r for r in data["data"]["mapping"]}
    assert (by_channel["speed"]["column"], by_channel["speed"]["unit_source"]) == ("vCar", "stated")
    assert by_channel["tyre_temp_fl"]["rate"] == 10 and len(data["engineering"]["sections"]) == 9
    assert "Read as MDF 4" in " ".join(n["text"] for n in data["notes"])


def test_workspace_and_project_reopen_after_a_restart(tmp_path):
    store = ProjectStore(tmp_path / "project")
    rules = events.default_rules()                          # the project's own rule, with its own threshold
    rules.rules.append(events.Rule("hard_cornering", "Sustained high lateral g", "abs(acc_lat) > $hard", min_duration_s=0.5,
                                   severity="abs(acc_lat)", evidence=("acc_lat", "speed")))
    rules.parameters["hard"] = 1.2
    store.save_event_rules(rules)
    sid = store.import_file(SYNTH)
    mset = maths.MathSet()
    mset.add("CombinedG", "sqrt(acc_lat^2 + acc_long^2)")
    store.save_maths(mset)
    first = store.reload_session(sid)
    maths.compute(first.model, store.maths())
    ws = default_layout(first.model)
    ws.add_chart(["CombinedG"])
    ws.zoom(100.0, 180.0)
    ws.set_cursor(140.0)
    ws.compare = {"align": "time", "laps": [2, 4]}
    ws.save(tmp_path / "ws.json")
    stored_events = len(store.model(sid).events)
    store.close()
    del first, ws

    store = ProjectStore(tmp_path / "project")              # everything from disk
    try:
        again = store.reload_session(sid)
        assert maths.compute(again.model, store.maths())["CombinedG"].ok
        ws = Workspace.load(tmp_path / "ws.json", again.model)
        assert ws.missing() == [] and ws.window() == (100.0, 180.0) and ws.cursor == 140.0
        assert ws.readout()[-1][0].channel == "CombinedG" and ws.readout()[-1][0].value > 0
        cmp_ = LapComparison(again.model, ws.compare["laps"], ws.compare["align"])
        assert (cmp_.laps, cmp_.align) == ([2, 4], "time")
        model = store.model(sid)
        assert len(model.events) == stored_events > 0, "the events found at import are stored with the session"
        assert all(e.label == "detected event" and "requires review" in e.confidence for e in model.events)
        assert {e.rule for e in model.events} >= {"hard_cornering"} and store.event_rules().parameters["hard"] == 1.2
        assert store.engineering(sid)["sections"][8]["title"] == "Setup hypotheses"
    finally:
        store.close()
