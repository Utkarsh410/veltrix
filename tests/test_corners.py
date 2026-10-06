"""Corner-by-corner analysis, checked against the corners built into the
synthetic circuit and against properties that must hold on any session."""

import numpy as np
import pandas as pd
import pytest

from conftest import GOLDEN
from veltrix_engine import build_session, corners as C
from veltrix_engine.importers import parse_csv_text, read_csv
from veltrix_engine.report import report_data
from veltrix_engine.synth import CORNERS as BUILT_IN


@pytest.fixture(scope="module")
def synth(synthetic):
    return synthetic, C.analyse(synthetic)


def test_finds_the_corners_built_into_the_circuit(synth):
    _, r = synth
    found = r["corners"]
    assert len(found) == len(BUILT_IN) == 9
    for row, (apex, radius, direction) in zip(found.itertuples(), BUILT_IN):
        assert row.apex_m == pytest.approx(apex, abs=15), row.corner
        assert row.direction == ("L" if direction > 0 else "R"), row.corner
        assert row.start_m < apex < row.end_m
    assert found.attrs["source"] == "lateral g"
    # tighter radius, lower minimum speed: the hairpin (30 m) is the slowest corner
    assert found.loc[found["min_speed"].idxmin(), "corner"] == "T2"
    assert set(found["kind"]) == {"slow", "medium", "fast"}


@pytest.mark.parametrize("key", ["synthetic", "logger", "monza"])
def test_zones_tile_the_lap_and_their_times_add_up(sessions, key):
    s = sessions[key]
    r = C.analyse(s)
    c = r["corners"]
    assert c["zone_start_m"].iloc[0] == 0 and c["zone_end_m"].iloc[-1] == pytest.approx(s.lap_length)
    assert np.allclose(c["zone_end_m"].iloc[:-1], c["zone_start_m"].iloc[1:])
    assert (c["zone_start_m"] <= c["start_m"]).all() and (c["end_m"] <= c["zone_end_m"]).all()
    sums = r["metrics"].groupby("lap")["zone_time_s"].sum()
    lap_times = s.laps.set_index("lap")["lap_time_s"].reindex(sums.index)
    assert np.allclose(sums, lap_times, atol=1e-6)


def test_metrics_are_physically_sensible(synth):
    _, r = synth
    m = r["metrics"]
    assert (m["min_speed"] <= m["entry_speed"] + 1e-6).all() and (m["min_speed"] <= m["exit_speed"] + 1e-6).all()
    braked = m.dropna(subset=["brake_m"])
    assert len(braked) > 0.6 * len(m)
    assert (braked["brake_m"] < braked["apex_m"]).all()                 # brakes go on before the apex
    assert braked["peak_brake_g"].between(0.3, 2.3).all()
    on = m.dropna(subset=["throttle_on_m"])
    assert (on["throttle_on_m"] >= on["apex_m"]).all()                  # and throttle comes back after it
    assert m["peak_lat_g"].between(0.8, 2.0).all()


def test_ideal_lap_is_no_slower_than_the_best_lap(sessions):
    for s in sessions.values():
        r = C.analyse(s)
        best = s.laps.loc[s.laps["status"] == "flying", "lap_time_s"].min()
        assert r["ideal_lap_s"] <= best + 1e-9
        assert (r["summary"]["available_s"] >= -1e-12).all()


def test_comparison_adds_up_to_the_lap_delta_and_finds_the_slower_corners(synth):
    s, r = synth
    cmp_ = C.compare_corners(r["metrics"], 2, 5)
    lap = s.laps.set_index("lap")["lap_time_s"]
    assert cmp_["d_time_s"].sum() == pytest.approx(lap[5] - lap[2], abs=1e-6)
    # Lap 5 has less grip everywhere: no corner is meaningfully faster, minimum speeds are lower
    assert (cmp_["d_time_s"] > -0.01).all() and (cmp_["d_min_speed"] < 0).all()
    assert C.compare_corners(r["metrics"], 3, 3)["finding"].eq("level").all()


def test_explain_names_only_what_points_the_same_way_as_the_time():
    lost = {"d_time_s": 0.20, "d_entry_speed": 0.5, "d_brake_m": -12.0, "d_min_speed": -4.0,
            "d_throttle_on_m": 9.0, "d_exit_speed": 6.0}        # exit was faster: not a reason for a loss
    assert C.explain(lost) == "braked earlier by 12 m; lower minimum speed by 4 km/h; later on throttle by 9 m"
    gained = {"d_time_s": -0.15, "d_entry_speed": 5.0, "d_brake_m": 8.0, "d_min_speed": 0.5,
              "d_throttle_on_m": np.nan, "d_exit_speed": 3.0}
    assert C.explain(gained) == "arrived faster by 5 km/h; braked later by 8 m; faster exit by 3 km/h"
    assert C.explain({"d_time_s": 0.01}) == "level"
    assert C.explain({"d_time_s": 0.2, "d_min_speed": 0.1}) == "no single difference stands out"


def test_corners_from_position_do_not_depend_on_how_the_map_is_drawn(monza):
    found = C.find_corners(monza)
    assert found.attrs["source"] == "path curvature from position" and len(found) >= 6
    raw = read_csv(GOLDEN / "monza_ac_gt3.csv.gz")
    raw.data["PosY"] = -raw.data["PosY"]                         # the same lap on a mirrored map
    mirrored = C.find_corners(build_session(raw))
    assert list(mirrored["direction"]) == list(found["direction"])   # steering settles left from right
    assert np.allclose(mirrored["apex_m"], found["apex_m"], atol=6)


def test_corners_survive_a_coarse_export(synthetic):
    raw = read_csv(GOLDEN / "synthetic_gt3_5lap.csv.gz").data
    hold = lambda x: np.repeat(np.asarray(x)[::5], 5)[:len(x)]
    raw["Speed"] = np.round(hold(raw["Speed"] / 3.6)) * 3.6
    raw["LateralAcc"], raw["LongAcc"] = np.round(hold(raw["LateralAcc"])), np.round(hold(raw["LongAcc"]))
    coarse = build_session(parse_csv_text(raw.to_csv(index=False)))
    a, b = C.analyse(coarse), C.analyse(synthetic)
    assert len(a["corners"]) == 9
    assert np.allclose(a["corners"]["apex_m"], b["corners"]["apex_m"], atol=25)
    ma, mb = a["metrics"].set_index(["lap", "corner"]), b["metrics"].set_index(["lap", "corner"])
    assert np.abs(ma["min_speed"] - mb["min_speed"]).max() < 4.0          # not rounded to 3.6 km/h steps
    assert np.abs(ma["zone_time_s"] - mb["zone_time_s"]).max() < 0.08


def test_steering_alone_is_enough_to_find_corners():
    raw = read_csv(GOLDEN / "synthetic_gt3_5lap.csv.gz").data[["Time", "LapNumber", "Speed", "SteeringAngle",
                                                                "ThrottlePos", "BrakePress"]]
    s = build_session(parse_csv_text(raw.to_csv(index=False)))
    found = C.find_corners(s)
    assert found.attrs["source"] == "steering and speed" and len(found) == 9


def test_no_cornering_signal_is_a_clear_error():
    t = np.arange(0, 60, 0.05)
    raw = pd.DataFrame({"Time": t, "Lap": (t // 20).astype(int) + 1, "Speed": 150.0})
    with pytest.raises(ValueError, match="lateral g, a position channel, or steering"):
        C.find_corners(build_session(parse_csv_text(raw.to_csv(index=False))))


def test_report_carries_the_corner_analysis(synthetic):
    d = report_data(synthetic)["corners"]
    assert [c["corner"] for c in d["list"]] == [f"T{i}" for i in range(1, 10)]
    assert set(d["laps"]) == {"2", "3", "4", "5"} and len(d["laps"]["2"]) == 9
    assert len(d["laps"]["2"][0]) == len(d["fields"])
    assert d["ideal"] <= min(l["time"] for l in report_data(synthetic)["laps"] if l["status"] == "flying")
