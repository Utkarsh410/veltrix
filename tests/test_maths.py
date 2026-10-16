"""
Maths channels: the seven fixtures of the brief, the errors a formula can
make, units, mixed rates, gaps, and that definitions and results survive a
restart.

The session used is synthetic and its properties are known exactly:
  Speed            100 + 50·sin(0.2 t) km/h, and 0 between 70 and 72 s
  Steering_Angle   30·sin(t) deg
  G_Lat            1.2·sin(0.5 t) g;  G_Long = d(Speed)/dt in g
  Throttle         100 % in 20–30 s and 50–55 s, 40 % elsewhere
  rear wheels      10 % faster than the car in 20–30 s (RL) — wheelspin on full throttle
  Oil_Temp         90 °C;  Mystery — a column with no unit
"""

import numpy as np
import pandas as pd
import pytest

from test_real_export_cases import export_layout
from veltrix_engine import ProjectStore, build_session, laps as lap_engine, maths
from veltrix_engine.importers import parse_csv_text
from veltrix_engine.maths import FormulaError, MathSet
from veltrix_engine.maths.parser import parse
from veltrix_engine.model import CALCULATED, Channel, SessionModel
from veltrix_engine.model_store import load_model, save_model

RATE, G0 = 20.0, 9.80665


def make_session():
    t = np.arange(0, 100, 1 / RATE)
    speed = 100 + 50 * np.sin(0.2 * t)
    stopped = (t >= 70) & (t < 72)
    speed[stopped] = 0.0
    spin = (t >= 20) & (t < 30)
    df = pd.DataFrame({
        "Time": t, "Lap": 1 + (t // 25).astype(int), "Speed": speed,
        "WheelSpeed_RL": speed * np.where(spin, 1.10, 1.0), "WheelSpeed_RR": speed,
        "Steering_Angle": 30 * np.sin(t), "G_Lat": 1.2 * np.sin(0.5 * t),
        "G_Long": 10 * np.cos(0.2 * t) / 3.6 / G0,
        "Throttle": np.where(spin | ((t >= 50) & (t < 55)), 100.0, 40.0),
        "Oil_Temp": 90.0, "Mystery": np.linspace(0, 1, len(t))})
    units = {"Time": "s", "Speed": "km/h", "WheelSpeed_RL": "km/h", "WheelSpeed_RR": "km/h", "Steering_Angle": "deg",
             "G_Lat": "g", "G_Long": "g", "Throttle": "%", "Oil_Temp": "C"}
    text = export_layout(df, units).replace('"..",', '"",').replace('".."', '""')
    return build_session(parse_csv_text(text)), df


@pytest.fixture()
def session():
    return make_session()[0]


@pytest.fixture(scope="module")
def frame():
    return make_session()[1]


def one(session, formula, unit="", **params):
    mset = MathSet()
    mset.parameters.update(params)
    mset.add("X", formula, unit)
    result = maths.compute(session.model, mset)["X"]
    assert result.ok, result.message
    return result.channel


def fails(session, formula, unit=""):
    mset = MathSet()
    mset.channels["X"] = maths.MathChannel("X", formula, unit)
    result = maths.compute(session.model, mset)["X"]
    assert not result.ok
    return result.message


# ── the brief's fixtures ──────────────────────────────────────────────────────

def test_brief_fixtures(session, frame):
    t = frame["Time"].to_numpy()
    mset = MathSet()
    mset.parameters.update(throttle_min=70, slip_limit=8)
    mset.add("RearWheelSpeed", "(WheelSpeed_RL + WheelSpeed_RR) / 2")
    mset.add("SteeringRate", "derivative(Steering_Angle)")
    mset.add("CombinedG", "sqrt(G_Lat^2 + G_Long^2)")
    mset.add("HighThrottle", "if(Throttle > 90, 1, 0)")
    mset.add("LongG_fromSpeed", "(derivative(Speed in m/s) / 9.80665) as g", unit="g")
    mset.add("RearSlipEstimate", "(RearWheelSpeed - Speed) / Speed * 100", unit="%", estimate=True)
    mset.add("RearWheelspinCandidate", "Throttle > $throttle_min and RearSlipEstimate > $slip_limit")
    r = maths.compute(session.model, mset)
    assert all(x.ok for x in r.values()), {k: v.message for k, v in r.items() if not v.ok}
    c = {k: v.channel for k, v in r.items()}
    moving = frame["Speed"].to_numpy() > 0
    edge = (t < 69.5) | (t > 72.5)                                   # away from the staged stop

    assert np.allclose(c["RearWheelSpeed"].values, (frame["WheelSpeed_RL"] + frame["WheelSpeed_RR"]) / 2, atol=1e-3)
    assert c["RearWheelSpeed"].unit == "km/h"
    assert np.allclose(c["SteeringRate"].values[5:-5], 30 * np.cos(t)[5:-5], atol=0.05) and c["SteeringRate"].unit == "deg/s"
    assert np.allclose(c["CombinedG"].values, np.hypot(frame["G_Lat"], frame["G_Long"]), atol=1e-3)
    assert c["CombinedG"].unit == "g"
    assert np.array_equal(c["HighThrottle"].values, (frame["Throttle"] > 90).astype(float))
    assert c["HighThrottle"].discrete and c["HighThrottle"].unit == "-"
    want = 10 * np.cos(0.2 * t) / 3.6 / G0
    assert np.allclose(c["LongG_fromSpeed"].values[edge][5:-5], want[edge][5:-5], atol=2e-3) and c["LongG_fromSpeed"].unit == "g"
    slip = c["RearSlipEstimate"]
    assert not slip.validity[~moving].any() and slip.validity[moving].all(), "no value where the car is stopped"
    assert np.allclose(slip.values[(t >= 20) & (t < 30)], 5.0, atol=0.01) and slip.unit == "%"
    assert any("estimate" in q for q in slip.quality) and any("invalid" in q for q in slip.quality)
    spin = c["RearWheelspinCandidate"]
    assert spin.values[moving].sum() == 0, "5 % slip does not pass the 8 % limit"
    mset.parameters["slip_limit"] = 4
    again = maths.compute(session.model, mset)["RearWheelspinCandidate"].channel
    assert np.array_equal(again.values[moving] == 1, ((t >= 20) & (t < 30))[moving])
    assert again.provenance.parameters["$slip_limit"] == 4 and again.dependencies == ("calculated:RearSlipEstimate", "normalised:throttle")


def test_every_result_has_values_a_mask_a_label_and_provenance(session):
    c = one(session, "derivative(Speed in m/s) in g")
    assert c.layer == "calculated" and c.label == CALCULATED and c.formula == "derivative(Speed in m/s) in g"
    assert c.valid is not None and len(c.valid) == len(c.values) == len(c.timestamps)
    assert c.dependencies == ("normalised:speed",) and c.provenance.inputs == c.dependencies
    assert any("converted from km/h to m/s" in n for n in c.provenance.notes)
    assert any("derivative on the logged timestamps" in n for n in c.provenance.notes)
    assert session.model.get("X") is c


# ── errors ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("formula, says", [
    ("Speed +", "ends where a value was expected"), ("(Speed", "Expected ')'"), ("Speed Speed", "Unexpected 'Speed'"),
    ("Speed = 3", "Use '=='"), ("1 < Speed < 3", "cannot be chained"), ("", "empty"),
    ("__import__('os')", "Unexpected character"), ("Speed.values", "Unexpected character"),
    ("Speed[0]", "Unexpected"), ("open(\"x\")", "Unexpected character"), ("lambda: 1", "Unexpected character"),
    ("Speed; Speed", "Unexpected character"), ("exec(Speed)", "Unknown function 'exec'"),
    ("eval(Speed)", "Unknown function 'eval'"), ("sqrt(Speed, 2)", "at most 1 argument"),
    ("lowpass(Speed)", "needs cutoff"), ("lowpass(Speed, cutoff=2, colour=1)", "no argument 'colour'"),
    ("Speed + $nope", "$nope is not defined"), ("Speed in furlongs", "not a unit"), ("Speed / 0", "Division by zero"),
    ("3 + 4", "single number"),
])
def test_bad_formulas_are_refused_with_a_reason(session, formula, says):
    assert says in fails(session, formula)


def test_errors_point_at_the_place():
    with pytest.raises(FormulaError) as e:
        parse("Speed * (2 + )")
    assert e.value.position == 13 and str(e.value).splitlines()[-1] == "  " + " " * 13 + "^"


def test_a_formula_cannot_grow_without_limit():
    with pytest.raises(FormulaError, match="too long"):
        parse("1+" * 1500 + "1")
    with pytest.raises(FormulaError, match="too complex"):
        parse("+".join(["a"] * 450))
    with pytest.raises(FormulaError, match="nested too deeply"):
        parse("(" * 60 + "1" + ")" * 60)


def test_missing_inputs_and_cycles_are_reported_per_channel(session):
    mset = MathSet()
    mset.add("Good", "Speed * 2")
    mset.add("NeedsGhost", "Ghost + 1")
    mset.add("OnTop", "NeedsGhost * 2")
    mset.add("A", "B + 1")
    mset.add("B", "C + 1")
    mset.add("C", "A + 1")
    r = maths.compute(session.model, mset)
    assert r["Good"].ok, "one bad channel does not stop the others"
    assert r["NeedsGhost"].message == "This session has no channel named Ghost."
    assert "NeedsGhost, which could not be calculated" in r["OnTop"].message
    assert "depends on itself: A → B → C → A" in r["A"].message and not r["B"].ok and not r["C"].ok
    mset.add("speed", "Speed * 2")
    assert "already has a channel called 'speed'" in maths.compute(session.model, mset)["speed"].message


# ── units ─────────────────────────────────────────────────────────────────────

def test_dimensions_are_checked(session):
    assert "Cannot add km/h and °C: they measure different things" in fails(session, "Speed + Oil_Temp")
    assert "Cannot compare" in fails(session, "Speed > Oil_Temp")
    assert "log() needs a value without a unit" in fails(session, "log(Speed)")
    assert "sin() needs an angle" in fails(session, "sin(Speed)")
    assert "declared in bar: they measure different things" in fails(session, "Speed * 2", unit="bar")
    assert "Write (...) in m/s to convert" in fails(session, "Speed * 2", unit="m/s")
    assert one(session, "Speed + 5").unit == "km/h", "a bare number takes the unit beside it"
    assert one(session, "Speed / Speed").unit == "-" and one(session, "Speed * 2", unit="km/h").unit == "km/h"


@pytest.mark.parametrize("formula, unit, factor", [
    ("Speed in m/s", "m/s", 1 / 3.6), ("(Speed in m/s) in km/h", "km/h", 1.0), ("Steering_Angle in rad", "rad", np.pi / 180),
    ("Time in ms", "ms", 1000.0), ("G_Lat in m/s^2", "m/s^2", G0), ("(G_Lat in m/s^2) in g", "g", 1.0),
    ("derivative(Steering_Angle in rad) in deg/s", "deg/s", None),
])
def test_conversions(session, formula, unit, factor):
    c = one(session, formula)
    assert c.unit == unit
    if factor is not None:
        source = session.model.get(formula.strip("(").split()[0])
        assert np.allclose(c.values, source.values * factor)


def test_mixed_units_of_one_kind_are_converted_not_added_blindly(session):
    c = one(session, "(Speed in m/s) + Speed")
    assert c.unit == "m/s" and np.allclose(c.values, 2 * session.model.get("speed").values / 3.6)
    assert any("km/h converted to m/s" in n for n in c.provenance.notes)
    assert np.allclose(one(session, "sin(Steering_Angle)").values, np.sin(np.radians(session.model.get("steering").values)))


def test_an_unknown_unit_stays_unknown_until_it_is_stated(session):
    assert session.model.get("Mystery", "raw").unit == "unknown"
    assert one(session, "Mystery * 2").unit == "unknown"
    assert "the unit 'unknown' is not known. State it first" in fails(session, "Mystery + Speed")
    assert "Cannot convert to km/h" in fails(session, "Mystery in km/h")
    stated = one(session, "(Mystery as km/h) + Speed")
    assert stated.unit == "km/h" and any("unit stated as km/h by the formula" in n for n in stated.provenance.notes)
    assert "'as' only states a unit" in fails(session, "Speed as bar")


# ── sampling ──────────────────────────────────────────────────────────────────

def two_rate_model():
    fast, slow = np.arange(0, 10, 0.01), np.arange(0, 10, 0.2)
    m = SessionModel()
    m.add(Channel("speed", "normalised", fast, 100 + fast, unit="km/h"))
    m.add(Channel("temp", "normalised", slow, 50 + 2 * slow, unit="°C"))
    m.add(Channel("gear", "normalised", slow, np.floor(slow / 2) + 1, unit="-", discrete=True))
    return m, fast, slow


def test_mixed_rates_meet_on_the_faster_clock_by_each_channels_policy():
    m, fast, slow = two_rate_model()
    mset = MathSet()
    mset.add("Warm", "temp * 2")
    mset.add("PerGear", "speed / gear")
    mset.add("Hot", "if(temp > 60, speed, 0)")
    r = maths.compute(m, mset)
    assert len(r["Warm"].channel.values) == len(slow), "one input: its own clock is kept"
    per = r["PerGear"].channel
    inside = fast <= slow[-1]
    assert len(per.values) == len(fast) and per.sample_rate == pytest.approx(100)
    held = np.floor(slow[np.searchsorted(slow, fast[inside] + 1e-9, side="right") - 1] / 2) + 1
    assert np.allclose(per.values[inside], (100 + fast[inside]) / held), "the gear is held, not interpolated"
    assert not per.validity[~inside].any(), "nothing past the slower channel's last sample"
    assert any("5 Hz input was held onto the 100 Hz clock" in n for n in per.provenance.notes)
    mset.add("Sum", "speed * temp")
    assert any("linearly interpolated" in n for n in maths.compute(m, mset)["Sum"].channel.provenance.notes)
    assert any("held" in n for n in r["Hot"].channel.provenance.notes), "a comparison's result is discrete"


def test_nothing_is_differentiated_or_filtered_across_a_gap_or_reset():
    t = np.r_[np.arange(0, 5, 0.01), np.arange(8, 13, 0.01)]             # three seconds missing
    v = 2.0 * t
    v[200] = np.nan
    m = SessionModel()
    m.add(Channel("x", "normalised", t, v, unit="m"))
    m.add(Channel("flat", "normalised", t, np.full(len(t), 7.0), unit="m"))
    m.add(Channel("wave", "normalised", t, np.sin(3 * t), unit="-"))
    mset = MathSet()
    for name, formula in (("D", "derivative(x)"), ("I", "integrate(x)"), ("F", "lowpass(x, 5)"), ("M", "moving_average(x, 0.5)"),
                          ("DFlat", "derivative(flat)"), ("SFlat", "rolling_std(flat, 0.2)"), ("P", "peak_detect(wave, min_height=0.5)")):
        mset.add(name, formula)
    r = {k: v.channel for k, v in maths.compute(m, mset).items()}
    d = r["D"]
    assert np.allclose(d.values[d.validity], 2.0), "the slope is 2 everywhere it is defined, including beside the gap"
    assert not d.validity[200] and d.unit == "m/s" and any("restarted at each of 2 gap" in n for n in d.provenance.notes)
    i = r["I"]
    assert i.unit == "m·s" and i.values[499] == pytest.approx(i.values[500]), "nothing is added across the gap"
    assert np.isfinite(r["F"].values[[10, 190, 210, 490, 510, 990]]).all() and np.allclose(r["F"].values[100], v[100], atol=1e-6)
    assert np.isnan(r["M"].values[:24]).all() and np.isfinite(r["M"].values[30]), "no partial windows"
    assert np.allclose(r["DFlat"].values[r["DFlat"].validity], 0) and np.nanmax(r["SFlat"].values) == 0, "a constant signal is fine"
    assert r["P"].discrete and 4 <= r["P"].values[r["P"].validity].sum() <= 6, "one mark per crest of the wave"

    reset = Channel("y", "normalised", np.r_[np.arange(0, 2, 0.01), np.arange(0, 2, 0.01)], np.arange(400.0), unit="m")
    m.add(reset)
    mset.add("DR", "derivative(y)")
    dr = maths.compute(m, mset)["DR"].channel
    assert np.allclose(dr.values[dr.validity], 100.0), "a clock that restarts is not differentiated across"


def test_filters_state_what_they_are():
    t = np.arange(0, 10, 0.01)
    step = (t >= 5).astype(float)
    m = SessionModel()
    m.add(Channel("x", "normalised", t, step, unit="-"))
    m.add(Channel("jitter", "normalised", np.sort(t + np.random.default_rng(1).uniform(-0.004, 0.004, len(t))), step, unit="-"))
    mset = MathSet()
    mset.add("Zero", "lowpass(x, 2, order=2)")
    mset.add("Causal", "lowpass(x, 2, order=2, zero_phase=false)")
    mset.add("High", "highpass(x, 2)")
    mset.add("Bad", "lowpass(jitter, 2)")
    mset.add("TooHigh", "lowpass(x, 80)")
    r = maths.compute(m, mset)
    zero, causal = r["Zero"].channel, r["Causal"].channel
    assert abs(zero.values[500] - 0.5) < 0.05, "zero-phase: the step is half way at the step"
    assert causal.values[500] < 0.1 and causal.values[560] > 0.9, "causal: the output lags"
    assert "zero-phase" in zero.provenance.notes[-1] and "causal" in causal.provenance.notes[-1]
    assert "order 2, cutoff 2 Hz at 100 Hz" in zero.provenance.notes[-1]
    assert abs(r["High"].channel.values[900]) < 0.01
    assert "not uniformly sampled" in r["Bad"].message and "between 0 and 50 Hz" in r["TooHigh"].message


# ── graph, cache, persistence ─────────────────────────────────────────────────

def test_order_cache_and_versions(session):
    mset = MathSet()
    mset.add("C", "B * 2")
    mset.add("B", "A + 1")
    mset.add("A", "Speed * $k")
    mset.parameters["k"] = 2
    assert maths.order(mset)[0] == ["A", "B", "C"]
    first = maths.compute(session.model, mset)
    assert not any(r.cached for r in first.values())
    assert all(r.cached for r in maths.compute(session.model, mset).values()), "nothing changed, nothing recomputed"
    mset.parameters["k"] = 3
    third = maths.compute(session.model, mset)
    assert not any(r.cached for r in third.values()), "a parameter change reaches everything downstream"
    assert np.allclose(third["C"].channel.values, (session.model.get("speed").values * 3 + 1) * 2)
    mset.add("B", "A + 2")
    assert mset.channels["B"].version == 2 and mset.channels["A"].version == 1
    fourth = maths.compute(session.model, mset)
    assert fourth["A"].cached and not fourth["B"].cached and not fourth["C"].cached
    assert fourth["B"].channel.provenance.parameters["formula_version"] == 2
    only = maths.compute(session.model, mset, names=["B"])
    assert set(only) == {"A", "B"}


def test_definitions_and_results_survive_a_restart(tmp_path, session):
    mset = MathSet()
    mset.parameters["limit"] = 90
    mset.add("CombinedG", "sqrt(G_Lat^2 + G_Long^2)", description="total g", category="dynamics")
    mset.add("HighThrottle", "if(Throttle > $limit, 1, 0)")
    first = maths.compute(session.model, mset)
    back = MathSet.load(mset.save(tmp_path / "maths.json"))
    assert back.to_dict() == mset.to_dict() and back.channels["CombinedG"].category == "dynamics"
    model = load_model(save_model(session.model, tmp_path / "model"))
    stored = model.get("CombinedG")
    assert stored.formula == "sqrt(G_Lat^2 + G_Long^2)" and stored.unit == "g" and stored.user == {"maths": True}
    assert np.array_equal(stored.values, first["CombinedG"].channel.values, equal_nan=True)
    assert np.array_equal(stored.validity, first["CombinedG"].channel.validity)
    store = ProjectStore(tmp_path / "project")
    try:
        assert store.maths().channels == {}
        store.save_maths(mset)
        assert store.maths().to_dict() == mset.to_dict()
    finally:
        store.close()


def test_live_validation_for_an_editor(session):
    assert maths.check("sqrt(G_Lat^2 + G_Long^2)", session.model) == \
        {"ok": True, "message": "", "position": None, "inputs": ["G_Lat", "G_Long"], "parameters": [], "unit": "g"}
    bad = maths.check("Speed * (2 + )", session.model)
    assert not bad["ok"] and bad["position"] == 13
    assert "no channel named Ghost" in maths.check("Ghost + 1", session.model)["message"]
    assert not session.model.has("__check__"), "checking leaves nothing behind"
    assert set(maths.FUNCTIONS) >= {"abs", "sqrt", "min", "max", "clip", "log", "exp", "sin", "cos", "tan", "derivative",
                                    "integrate", "moving_average", "lowpass", "highpass", "rolling_min", "rolling_max",
                                    "rolling_std", "peak_detect", "if"}
    assert len(maths.REFERENCE) >= 15


def test_a_maths_channel_can_be_compared_lap_against_lap(session):
    mset = MathSet()
    mset.add("CombinedG", "sqrt(G_Lat^2 + G_Long^2)")
    assert maths.on_view(session, maths.compute(session.model, mset)) == ["CombinedG"]
    assert "CombinedG" in session.frame()
    cmp_ = lap_engine.compare_laps(session, 2, 3, channels=("speed", "CombinedG"))
    assert {"CombinedG_ref", "CombinedG_cmp"} <= set(cmp_.columns) and cmp_["CombinedG_ref"].notna().mean() > 0.9


# ── what an editor needs ──────────────────────────────────────────────────────

def test_preview_computes_without_touching_the_session(session):
    mset = MathSet()
    mset.parameters["k"] = 2
    mset.add("Doubled", "Speed * $k")
    maths.compute(session.model, mset)
    before = {c.id: c for c in session.model.channels("calculated")}
    signatures = dict(session.model.maths_signatures)

    r = maths.preview("sqrt(G_Lat^2 + G_Long^2) * (Doubled / Doubled)", session.model, mset)
    assert r.ok and r.channel.unit == "g" and len(r.channel.values) == len(session.model.get("speed").values)
    assert {c.id: c for c in session.model.channels("calculated")} == before, "nothing is added, nothing replaced"
    assert session.model.maths_signatures == signatures and session.model.get("Doubled") is before["Doubled"]

    editing = maths.preview("Speed * 3", session.model, mset, name="Doubled")      # the channel being edited
    assert editing.ok and np.allclose(editing.channel.values, session.model.get("speed").values * 3)
    assert np.allclose(session.model.get("Doubled").values, session.model.get("speed").values * 2), "the saved one is untouched"
    assert "depends on itself" not in maths.preview("Doubled + 1", session.model, mset, name="Other").message
    assert not maths.preview("Doubled + 1", session.model, mset, name="Doubled").ok, "a channel cannot use itself"
    assert maths.preview("Speed +", session.model).message.startswith("The formula ends")
    assert "declared in bar" in maths.preview("Speed * 2", session.model, unit="bar").message


def test_check_reports_position_for_errors_found_while_evaluating(session):
    bad = maths.check("Speed + Oil_Temp", session.model)
    assert not bad["ok"] and bad["message"].startswith("Cannot add km/h and °C") and bad["position"] == 6
    assert maths.check("Speed * 2", session.model, unit="km/h")["unit"] == "km/h"


def test_dependencies_say_what_each_channel_uses_and_what_uses_it(session):
    mset = MathSet()
    mset.parameters["limit"] = 8
    mset.add("RearWheelSpeed", "(WheelSpeed_RL + WheelSpeed_RR) / 2")
    mset.add("RearSlip", "(RearWheelSpeed - Speed) / Speed * 100")
    mset.add("Spin", "Throttle > $min_throttle and RearSlip > $limit")
    mset.add("Ghostly", "Ghost * 2")
    mset.add("A", "B + 1")
    mset.add("B", "A + 1")
    d = maths.dependencies(mset, session.model)
    assert d["RearWheelSpeed"]["used_by"] == ["RearSlip"] and d["RearSlip"]["used_by"] == ["Spin"]
    assert d["RearSlip"]["uses_maths"] == ["RearWheelSpeed"]
    assert d["RearSlip"]["uses_session"] == [{"name": "Speed", "channel": "normalised:speed", "unit": "km/h"}]
    assert d["Spin"]["parameters"] == ["limit", "min_throttle"] and d["Spin"]["undefined_parameters"] == ["min_throttle"]
    assert d["RearWheelSpeed"]["order"] < d["RearSlip"]["order"] < d["Spin"]["order"]
    assert d["Ghostly"]["missing"] == ["Ghost"] and d["Ghostly"]["problem"] == ""
    assert "depends on itself" in d["A"]["problem"] and d["A"]["order"] is None
    assert maths.dependencies(mset)["RearSlip"]["missing"] == [], "without a session nothing can be called missing"
