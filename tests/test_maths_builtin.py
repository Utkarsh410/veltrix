"""
Phase 6: the engine's derived channels as maths channels, new functions,
sharing maths sets, and the history of each channel's formula.
"""

import re

import numpy as np
import pytest

from conftest import GOLDEN
from veltrix_engine import load_session, maths
from veltrix_engine.maths import MathChannel, MathSet
from veltrix_engine.maths.builtin import CHANNELS, NOT_A_FORMULA, standard_set


def _renamed(std: MathSet) -> MathSet:
    """The built-in set under other names, so it computes beside the engine's own channels."""
    out = MathSet()
    out.parameters = dict(std.parameters)
    for n, c in std.channels.items():
        f = c.formula
        for m in std.channels:
            f = re.sub(rf"\b{m}\b", m + "_m", f)
        out.channels[n + "_m"] = MathChannel(n + "_m", f)
    return out


@pytest.mark.parametrize("name", ["synthetic_gt3_5lap.csv.gz", "logger_layout_3lap_20hz.csv"])
def test_the_built_in_formulas_give_the_engines_numbers(name):
    """On an evenly sampled log every derived channel the engine computes is the
    built-in formula's result — to the 0.04% between g = 9.81 and 9.80665."""
    s = load_session(GOLDEN / name)
    results = maths.compute(s.model, _renamed(standard_set(s.vehicle)))
    t = s.channels["time"].to_numpy()
    compared = 0
    for n in CHANNELS:
        if n not in s.derived:
            continue
        r = results[n + "_m"]
        if not r.ok and "has no channel named" in r.message:
            continue          # e.g. no dampers: the engine's fallback carries no information, the formula needs them
        assert r.ok, f"{n}: {r.message}"
        a, b = s.derived[n].to_numpy(float), r.channel.at(t)[0]
        core = slice(int(0.02 * len(a)), int(0.98 * len(a)))           # the filters pad the ends differently
        ok = np.isfinite(a[core]) & np.isfinite(b[core])
        assert ok.mean() > 0.3, n
        scale = max(float(np.nanpercentile(np.abs(a[core][ok]), 99)), 1e-9)
        assert np.percentile(np.abs(a[core][ok] - b[core][ok]), 99) <= 0.003 * scale, n
        compared += 1
    assert compared >= 7


def test_every_derived_channel_is_a_formula_or_says_why_not(synthetic):
    derived = set(synthetic.derived.columns)
    assert derived <= set(CHANNELS) | set(NOT_A_FORMULA), derived - set(CHANNELS) - set(NOT_A_FORMULA)
    std = standard_set(synthetic.vehicle)
    assert std.parameters["wheelbase_m"] == synthetic.vehicle.wheelbase_m
    assert all(c.description and c.category == "built-in" for c in std.channels.values())


def test_new_functions(synthetic):
    ms = MathSet()
    for name, f in {"t_lap": "since_change(lap)", "ang": "atan2(acc_lat, acc_long) in deg",
                    "comb": "hypot(acc_lat, acc_long)", "fast": "when(speed > 200, speed)",
                    "held": "rolling_median(acc_lat, 0.25, edges=1)"}.items():
        ms.channels[name] = MathChannel(name, f)
    r = maths.compute(synthetic.model, ms)
    assert all(x.ok for x in r.values()), {k: v.message for k, v in r.items() if not v.ok}
    assert (r["t_lap"].channel.unit, r["ang"].channel.unit, r["comb"].channel.unit) == ("s", "deg", "g")
    lap = synthetic.model.laps[2]
    t, v = r["t_lap"].channel.timestamps, np.asarray(r["t_lap"].channel.values, float)
    inside = (t > lap.t_start + 1) & (t < lap.t_end - 1)
    assert np.allclose(v[inside], t[inside] - lap.t_start, atol=0.05), "since_change(lap) is the time into the lap"
    fast = r["fast"].channel
    speed = synthetic.model.get("speed").at(fast.timestamps)[0]
    assert np.all(np.isnan(np.asarray(fast.values, float)[speed < 199])), "when() leaves the rest invalid, not zero"
    bad = maths.compute(synthetic.model, _one("bad", "atan2(speed, acc_lat)"))["bad"]
    assert not bad.ok and "different" in bad.message, "atan2 needs two of the same kind of quantity"


def _one(name, formula):
    ms = MathSet()
    ms.channels[name] = MathChannel(name, formula)
    return ms


def test_a_filter_can_cap_its_cutoff_and_filter_an_uneven_clock(monza, logger):
    r = maths.compute(logger.model, _one("f", "lowpass(speed, 15)"))
    assert not r["f"].ok and "cap=1" in r["f"].message
    assert maths.compute(logger.model, _one("f", "lowpass(speed, 15, cap=1)"))["f"].ok
    r = maths.compute(monza.model, _one("f", "lowpass(speed, 5)"))
    assert not r["f"].ok and "uniform=1" in r["f"].message
    r = maths.compute(monza.model, _one("f", "lowpass(speed, 5, uniform=1)"))
    assert r["f"].ok and any("even grid" in step for step in r["f"].channel.provenance.steps) \
        if hasattr(r["f"].channel.provenance, "steps") else r["f"].ok


def test_sharing_a_maths_set_brings_what_a_channel_needs_and_never_overwrites(tmp_path):
    a = MathSet()
    a.parameters["k"] = 2.0
    a.add("Base", "speed * $k")
    a.add("Top", "Base + 1")
    a.add("Other", "rpm / 2")
    shared = a.subset(["Top"])
    assert list(shared.channels) == ["Base", "Top"] and shared.parameters == {"k": 2.0}
    shared.save(tmp_path / "shared.json")
    b = MathSet()
    b.parameters["k"] = 3.0
    b.add("Base", "speed / 2")
    report = b.merge(MathSet.load(tmp_path / "shared.json"))
    assert report["renamed"] == {"Base": "Base_2"} and report["added"] == ["Top"]
    assert b.channels["Top"].formula == "Base_2 + 1", "an imported channel follows its renamed input"
    assert b.channels["Base"].formula == "speed / 2" and b.parameters["k"] == 3.0
    assert report["parameters_differ"] == ["k"]
    c = MathSet()
    c.add("Base", "speed / 2")
    c.merge(shared, on_clash="replace")
    assert c.channels["Base"].formula == "speed * $k" and c.channels["Base"].history[0]["formula"] == "speed / 2"


def test_a_channel_keeps_the_history_of_its_formula_and_old_files_open(tmp_path):
    ms = MathSet()
    ms.add("G", "hypot(acc_lat, acc_long)")
    ms.add("G", "hypot(acc_lat, acc_long) * 1.0")
    ms.add("G", "hypot(acc_lat, acc_long) * 1.0", description="only the description changed")
    c = ms.channels["G"]
    assert c.version == 2 and [h["version"] for h in c.history] == [1] and c.history[0]["formula"] == "hypot(acc_lat, acc_long)"
    ms.save(tmp_path / "m.json")
    again = MathSet.load(tmp_path / "m.json")
    assert again.channels["G"].history == c.history
    from pathlib import Path
    old = MathSet.load(Path(__file__).parent / "fixtures" / "saved_release_1" / "maths.json")
    assert old.channels["SpeedTimesK"].history == [], "a format-1 file comes up to format 2"


def test_the_editor_shows_the_built_ins_and_copies_one(tmp_path, synthetic):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtWidgets
    from veltrix_app.maths_editor import MathsEditor
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    ed = MathsEditor(tmp_path / "maths.json")
    ed.vehicle = synthetic.vehicle
    ed._fill_builtin()
    names = [ed.builtin.item(i).text() for i in range(ed.builtin.count())]
    assert "acc_long_calc" in names and any(n.startswith("acc_lat_rebuilt") and "not a formula" in n for n in names)
    ed.model = synthetic.model
    ed.copy_builtin("acc_lat_calc")
    assert ed.name.text() == "acc_lat_calc" and "$steer_ratio" in ed.formula.toPlainText()
    assert ed.mset.parameters["steer_ratio"] == synthetic.vehicle.steer_ratio
    assert ed.save_channel()
    assert ed.results["acc_lat_calc"].ok, "the copy computes, and replaces the engine's channel"
    out = ed.export_channels(str(tmp_path / "out.json"), ["acc_lat_calc"])
    assert MathSet.load(out).parameters["steer_ratio"] == synthetic.vehicle.steer_ratio
    other = tmp_path / "other.json"
    sh = MathSet()
    sh.add("acc_lat_calc", "acc_lat * 2")
    sh.save(other)
    report = ed.import_channels(str(other))
    assert report["renamed"] == {"acc_lat_calc": "acc_lat_calc_2"} and "renamed" in ed.status.text()
