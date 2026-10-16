"""
The event engine: rules as data, hysteresis, minimum duration, gaps, missing
inputs, and events worded as candidates.

The session is synthetic, 50 Hz, two laps of 60 s at a steady 150 km/h, with
these staged on it:
  20–22 s      rear wheels 15 % fast on full throttle                (wheelspin)
  30–33 s      rear slip swinging between 5 % and 9 %, full throttle (one event, by hysteresis)
  40.00–40.06  a 15 % blip                                           (too short to count)
  50–50.6 s    1 g of braking with the front-left wheel 30 % slow    (lock-up)
  70 s         one sample of speed reads zero                        (blanked at import, with a warning)
  79.5–80 s and 85–85.5 s  wheelspin either side of five missing seconds
"""

import json

import numpy as np
import pandas as pd
import pytest

from test_real_export_cases import export_layout
from veltrix_engine import build_session, events
from veltrix_engine.importers import parse_csv_text


def make(wheels=True, drop_gap=True):
    t = np.arange(0, 120, 0.02)
    speed = np.full(len(t), 150.0)
    braking = (t >= 50) & (t < 50.6)
    speed[t >= 50] -= np.clip(t[t >= 50] - 50, 0, 0.6) * 9.80665 * 3.6
    after = t >= 50.6                                   # back up to speed gently: 4 km/h a second
    speed[after] = np.minimum(150.0, speed[after] + (t[after] - 50.6) * 4.0)
    throttle = np.full(len(t), 60.0)
    slip = np.zeros(len(t))
    for a, b, value in ((20, 22, 15.0), (40.0, 40.06, 15.0), (79.5, 80.0, 15.0), (85.0, 85.5, 15.0)):
        slip[(t >= a) & (t < b)] = value
    hover = (t >= 30) & (t < 33)
    slip[hover] = 7.0 + 2.0 * np.sin(2 * np.pi * 2.0 * t[hover])
    throttle[slip > 0] = 100.0
    throttle[braking] = 0.0
    front = speed.copy()
    fl = np.where(braking, speed * 0.70, front)
    glitch = np.argmin(np.abs(t - 70.0))
    df = pd.DataFrame({"Time": t, "Lap": 1 + (t // 60).astype(int), "Speed": speed, "WheelSpeed_FL": fl, "WheelSpeed_FR": front,
                       "WheelSpeed_RL": speed * (1 + slip / 100), "WheelSpeed_RR": speed * (1 + slip / 100),
                       "Throttle": throttle, "Steering_Angle": 5 * np.sin(0.3 * t), "G_Lat": 0.3 * np.sin(0.3 * t)})
    df.loc[glitch, "Speed"] = 0.0
    if drop_gap:
        df = df[(df["Time"] < 80.0) | (df["Time"] >= 85.0)]
    if not wheels:
        df = df.drop(columns=[c for c in df if c.startswith("WheelSpeed")])
    units = {"Time": "s", "Speed": "km/h", "Throttle": "%", "Steering_Angle": "deg", "G_Lat": "g",
             **{c: "km/h" for c in df if c.startswith("WheelSpeed")}}
    return build_session(parse_csv_text(export_layout(df, units).replace('".."', '""')))


@pytest.fixture(scope="module")
def found():
    s = make()
    ev, report = events.detect(s.model)
    return s, ev, report


def of(ev, rule):
    return [e for e in ev if e.rule == rule]


def test_staged_events_are_found_once_each_and_worded_as_candidates(found):
    s, ev, report = found
    spin = of(ev, "wheelspin")
    assert [round(e.t_start) for e in spin] == [20, 30, 80, 85], [(e.t_start, e.t_end) for e in spin]
    first = spin[0]
    assert first.kind == "Possible wheelspin" and first.label == "detected event"
    assert first.confidence == "medium — requires review" and first.rule_version == "1"
    assert (first.t_start, first.t_end) == (pytest.approx(20.0, abs=0.03), pytest.approx(22.0, abs=0.05))
    assert first.lap == 1 and first.severity == "moderate" and first.evidence["peak"]["value"] == pytest.approx(15.0, abs=0.1)
    assert first.evidence["throttle"] == {"min": 100.0, "max": 100.0, "mean": 100.0, "unit": "%"}
    assert first.evidence["RearSlipEstimate"]["max"] == pytest.approx(15.0, abs=0.1) and "kerb" in first.evidence["could_also_be"]
    assert first.position_m == pytest.approx(s.model.get("lap_distance").at([first.evidence["peak"]["at_s"]])[0][0])
    lock = of(ev, "lock_up")
    assert len(lock) == 1 and lock[0].kind == "Possible front lock-up" and 50.0 <= lock[0].t_start <= 50.2
    assert lock[0].evidence["peak"]["value"] == pytest.approx(30.0, abs=1.0) and lock[0].severity == "moderate"
    assert of(ev, "speed_glitch") == [] and report["speed_glitch"]["ran"], "the one-sample dropout was dealt with at import"
    assert any("jump up to 150 km/h away from the samples either side" in w for w in s.warnings())
    assert not s.model.get("speed", "normalised").validity[np.argmin(np.abs(s.model.get("speed", "normalised").timestamps - 70.0))]
    assert report["wheelspin"] == {"ran": True, "events": 4, "message": "4 found"}
    assert "Possible wheelspin (moderate) on lap 1" in events.describe(first) and "requires review" in events.describe(first)


def test_hysteresis_makes_one_event_of_a_signal_hovering_at_the_limit(found):
    s, ev, _ = found
    hover = [e for e in of(ev, "wheelspin") if 29.5 < e.t_start < 33]
    assert len(hover) == 1 and hover[0].t_end - hover[0].t_start > 2.5, "it starts above 8 % and only ends below 4 %"
    rules = events.default_rules()
    next(r for r in rules.rules if r.id == "wheelspin").off = ""          # the same rule without a release threshold
    rules.rules = [r for r in rules.rules if r.id == "wheelspin"]
    rules.rules[0].merge_gap_s = rules.rules[0].min_duration_s = 0.0
    chatter, _ = events.detect(make().model, rules)
    assert len([e for e in chatter if 29.5 < e.t_start < 33]) >= 5, "without it, every swing is its own event"


def test_minimum_duration_and_gaps(found):
    s, ev, _ = found
    assert not [e for e in of(ev, "wheelspin") if 39 < e.t_start < 41], "a 60 ms blip is under the 150 ms minimum"
    before, after = [e for e in of(ev, "wheelspin") if e.t_start > 75]
    assert before.t_end < 80.01 and after.t_start >= 85.0, "an event never runs through missing data"
    assert before.evidence["duration_s"] == pytest.approx(0.48, abs=0.05)


def test_thresholds_are_parameters_and_rules_are_data(tmp_path):
    rules = events.default_rules()
    rules.parameters["spin_on"] = 20.0                                    # this car spins its wheels more than 15 %
    ev, report = events.detect(make().model, rules)
    assert of(ev, "wheelspin") == [] and report["wheelspin"]["message"] == "none found"
    back = events.RuleSet.load(rules.save(tmp_path / "rules.json"))
    assert back.to_dict() == rules.to_dict() and back.parameters["spin_on"] == 20.0
    assert json.loads((tmp_path / "rules.json").read_text())["rules"][0]["on"].startswith("throttle > $spin_throttle")

    mine = events.RuleSet([events.Rule("full_throttle", "Full throttle", "throttle > $limit", min_duration_s=1.0,
                                       evidence=("speed",), version=3)], {"limit": 95.0})
    ev, _ = events.detect(make().model, mine)
    assert [round(e.t_start) for e in ev] == [20, 30] and all(e.rule_version == "3" and e.kind == "Full throttle" and e.severity == "" for e in ev)
    mine.rules[0].enabled = False
    ev, report = events.detect(make().model, mine)
    assert ev == [] and report["full_throttle"]["message"] == "switched off"
    with pytest.raises(ValueError, match="Rule 'bad', on: Expected"):
        events.RuleSet.from_dict({"rules": [{"id": "bad", "title": "x", "on": "sqrt(speed"}]})
    with pytest.raises(ValueError, match="repeated"):
        events.RuleSet([events.Rule("a", "A", "speed > 1"), events.Rule("a", "A", "speed > 2")]).check()
    with pytest.raises(ValueError, match="Unknown function|Unexpected"):
        events.RuleSet.from_dict({"rules": [{"id": "evil", "title": "x", "on": "__import__('os').system('true')"}]})


def test_a_rule_whose_inputs_are_missing_is_skipped_and_says_why():
    s = make(wheels=False)
    ev, report = events.detect(s.model)
    assert not report["wheelspin"]["ran"] and report["wheelspin"]["message"].startswith("Not looked for:")
    assert "could not be calculated" in report["wheelspin"]["message"] or "no channel named" in report["wheelspin"]["message"]
    assert report["speed_glitch"]["ran"] and report["oversteer"]["ran"], "the other rules still run"


def test_the_engines_working_channels_do_not_stay_in_the_session(found):
    s, _, _ = found
    names = {c.id for c in s.model.channels("calculated")}
    assert not any(n.startswith("__event_") for n in names)
    assert "RearSlipEstimate" in names and any("estimate" in q for q in s.model.get("RearSlipEstimate").quality)


def test_a_rule_that_differentiates_is_not_run_on_a_coarsely_recorded_channel():
    s = make()
    s.model.get("speed", "normalised").quality.append("recorded in coarse steps of 3.6 km/h")
    ev, report = events.detect(s.model)
    assert not report["speed_glitch"]["ran"] and "speed is recorded in coarse steps of 3.6 km/h" in report["speed_glitch"]["message"]
    assert of(ev, "speed_glitch") == [] and report["wheelspin"]["ran"]


# ── thresholds fitted to the session ──────────────────────────────────────────

def noisy():
    """The staged session with rear wheel speeds that wander by a few per cent
    of slip (slowly, as a worn sensor or a tyre of another size would), and
    fifteen seconds of coasting for the noise to be measured in."""
    s = make()
    rng = np.random.default_rng(3)
    for name in ("wheel_speed_rl", "wheel_speed_rr"):
        c = s.model.get(name, "normalised")
        wander = np.convolve(rng.normal(0, 1, len(c.values)), np.ones(25) / 5, mode="same")      # about ±1, slow
        c.values = c.values + wander * 0.045 * 150
    throttle = s.model.get("throttle", "normalised")
    values = throttle.values.copy()
    values[(throttle.timestamps >= 100) & (throttle.timestamps < 115)] = 10.0
    throttle.values = values
    return s


def test_calibration_raises_a_threshold_that_sits_inside_the_sensor_noise():
    s = noisy()
    rules = events.default_rules()
    rules.parameters["spin_throttle"] = 50.0                # this car is at 60 % throttle most of the lap
    plain, _ = events.detect(s.model, rules)
    assert len(of(plain, "wheelspin")) > 4, "at the configured 8 %, sensor wander trips the rule beside the four staged events"
    fitted, changes = events.calibrate(s.model, rules)
    assert fitted.parameters["spin_on"] > 9 and fitted.parameters["spin_off"] == pytest.approx(fitted.parameters["spin_on"] / 2, rel=0.01)
    assert any(c.startswith("$spin_on raised from 8 to") and "with nothing happening" in c for c in changes)
    assert events.default_rules().parameters["spin_on"] == 8.0, "the rule set given is not altered"
    found, report = events.detect(s.model, rules, fit=True)
    spin = of(found, "wheelspin")
    assert 0 < len(spin) < len(of(plain, "wheelspin")) and all(e.evidence["peak"]["value"] > 13 for e in spin), \
        "what is left is the staged 15 % wheelspin, not the wander"
    assert report["_calibration"]["parameters"]["spin_on"] == fitted.parameters["spin_on"]
    assert "raised from 8" in report["_calibration"]["message"]


def test_calibration_never_lowers_a_threshold_and_skips_what_it_cannot_measure():
    clean = make()
    fitted, changes = events.calibrate(clean.model)
    assert fitted.parameters["spin_on"] == 8.0 and fitted.parameters["lock_on"] == 12.0, "clean sensors: the configured values stand"
    assert not any("spin_on" in c or "lock_on" in c for c in changes)
    found, report = events.detect(clean.model, fit=True)
    assert [round(e.t_start) for e in of(found, "wheelspin")] == [20, 30, 80, 85], "and the staged events are all still found"
    bare, changes = events.calibrate(make(wheels=False).model)
    assert bare.parameters["spin_on"] == 8.0, "no wheel speeds: nothing to fit, nothing changed"
    mine = events.RuleSet([events.Rule("fast", "Fast", "speed > $limit", calibrate={"parameter": "limit", "signal": "speed",
                                                                                 "percentile": 50})], {"limit": 500.0})
    assert events.calibrate(clean.model, mine)[0].parameters["limit"] == 500.0, "a fitted value below the configured one is not used"
