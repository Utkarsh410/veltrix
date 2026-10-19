"""Phase 12: two sessions, one change — did it help, by more than the scatter?"""

import numpy as np
import pytest

import format_writers as fw
from test_advice import generate_session
from veltrix_engine import build_session, load_session, synth
from veltrix_engine.analysis import ab
from veltrix_engine.cli import main as cli_main
from veltrix_engine.importers import parse_csv_text
from test_formats import ref  # noqa: F401  (a fixture)


def session(seed, grip=1.0, monkeypatch=None, laps=8):
    if grip != 1.0:
        solve = synth._solve_speed_profile
        monkeypatch.setattr(synth, "_solve_speed_profile", lambda mu, p: solve(mu * grip, p))
    raw = generate_session(lap_count=laps, seed=seed, lockup_lap=0)
    if grip != 1.0:
        monkeypatch.undo()
    return build_session(parse_csv_text(raw.to_csv(index=False)))


def test_no_change_is_called_no_change():
    r = ab.compare_sessions(session(7), session(11))
    assert r["comparable"] and r["lap"]["call"] == "no detectable change"
    assert not set(r["corners"]["call"]) & {"faster", "slower"}, "the scatter between two runs is not a result"


def test_more_grip_is_found_on_the_lap_and_in_the_corners(monkeypatch):
    a, b = session(7), session(11, grip=1.02, monkeypatch=monkeypatch)
    r = ab.compare_sessions(a, b, "softer rear anti-roll bar")
    assert r["lap"]["call"] == "faster" and r["lap"]["delta_s"] < -0.3 and r["lap"]["ratio"] >= ab.REAL_RATIO
    faster = r["corners"][r["corners"]["call"] == "faster"]
    assert len(faster) >= len(r["corners"]) // 2 and (r["corners"]["call"] != "slower").all()
    assert (faster["d_min_speed"] > 0).all(), "faster through the corner goes with more minimum speed"
    text = ab.to_markdown(r)
    assert "Stated: softer rear anti-roll bar" in text and "Lap: faster" in text and "| T1 |" in text


def test_too_few_laps_is_said_not_guessed():
    r = ab.compare_sessions(session(7, laps=2), session(11, laps=2))
    assert r["lap"]["call"] == "not enough laps" and r["lap"]["confidence"] == "low"


def test_the_setup_change_is_read_from_the_files(tmp_path, ref):
    n = len(ref)
    variables = [("SessionTime", "double", "s", ref["time"]), ("Speed", "float", "m/s", ref["speed"] / 3.6),
                 ("Lap", "int", "", ref["lap"]), ("LapDist", "float", "m", ref["lap_distance"]),
                 ("Throttle", "float", "%", ref["throttle"] / 100), ("Brake", "float", "%", ref["brake"] / ref["brake"].max()),
                 ("SteeringWheelAngle", "float", "rad", np.radians(ref["steering"])),
                 ("LatAccel", "float", "m/s^2", ref["acc_lat"] * 9.81), ("RPM", "float", "revs/min", ref["rpm"])]
    one = tmp_path / "before.ibt"
    one.write_bytes(fw.ibt(variables, 20))
    two = tmp_path / "after.ibt"
    yaml_after = fw.IBT_YAML.replace("   ArbSetting: 3", "   ArbSetting: 5")
    original = fw.IBT_YAML
    try:
        fw.IBT_YAML = yaml_after
        two.write_bytes(fw.ibt(variables, 20))
    finally:
        fw.IBT_YAML = original
    a, b = load_session(one), load_session(two)
    assert a.raw.setup["Chassis.Front.ArbSetting"] == "3" and b.raw.setup["Chassis.Front.ArbSetting"] == "5"
    r = ab.compare_sessions(a, b)
    assert r["changes"] == [("Chassis.Front.ArbSetting", "3", "5")]
    assert "Chassis.Front.ArbSetting: 3 → 5" in ab.to_markdown(r)


def test_results_of_running_are_not_settings():
    a = {"Tires.LeftFront.LastHotPressure": "172 kPa", "Chassis.Rear.ArbSetting": "1"}
    b = {"Tires.LeftFront.LastHotPressure": "180 kPa", "Chassis.Rear.ArbSetting": "2"}
    assert ab.setup_changes(a, b) == [("Chassis.Rear.ArbSetting", "1", "2")]


def test_the_compare_command(tmp_path, capsys):
    from conftest import GOLDEN
    f = str(GOLDEN / "synthetic_gt3_5lap.csv.gz")
    code = cli_main(["compare", f, f, "--change", "nothing", "--markdown", str(tmp_path / "c.md"), "--no-app-data"])
    out = capsys.readouterr().out
    assert code == 0 and "Lap: no detectable change" in out and (tmp_path / "c.md").exists()
