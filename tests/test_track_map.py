"""Track map: drawn from position when the file has it, otherwise rebuilt from
speed and lateral g and closed into a lap. The rebuilt shape is checked against
a session whose true path is known."""

import numpy as np
import pytest

from conftest import GOLDEN
from veltrix_engine import build_session
from veltrix_engine.importers import parse_csv_text, read_csv
from veltrix_engine.report import report_data
from veltrix_engine.track import STEP_M, track_map

MONZA = GOLDEN / "monza_ac_gt3.csv.gz"


def fit_error(a, b):
    """Mean and largest distance between two paths after sliding and rotating
    one onto the other. No mirroring and no scaling: left must stay left."""
    A, B = a - a.mean(0), b - b.mean(0)
    U, _, Vt = np.linalg.svd(B.T @ A)
    if np.linalg.det(U @ Vt) < 0:
        U[:, -1] *= -1
    d = np.hypot(*(A - B @ (U @ Vt)).T)
    return float(d.mean()), float(d.max())


def without_position(scale=1.0, offset=0.0, noise=0.0):
    """The Monza session with its position replaced by the lateral g that path
    implies (v² × curvature), optionally spoiled like a real accelerometer."""
    raw = read_csv(MONZA).data.copy()
    box = np.ones(15) / 15
    x, y = np.convolve(raw["PosX"], box, mode="same"), np.convolve(raw["PosY"], box, mode="same")
    curvature = np.gradient(np.unwrap(np.arctan2(np.gradient(y), np.gradient(x))))      # one row per metre
    curvature[np.abs(curvature) > 0.05] = 0
    ay = (raw["Speed"].to_numpy() / 3.6) ** 2 * curvature / 9.81
    raw = raw.drop(columns=["PosX", "PosY", "Steering"])
    raw["LateralAcc"] = ay * scale + offset + np.random.default_rng(3).normal(0, noise, len(raw))
    return build_session(parse_csv_text(raw.to_csv(index=False)))


def test_map_from_logged_position(monza):
    m = track_map(monza)
    assert m.attrs["source"] == "position" and m.attrs["closed"]
    assert m.attrs["direction"] == "clockwise"                       # Monza is run clockwise
    assert m.attrs["gap_m"] < 10                                     # the lap really does come back to the line
    path = np.hypot(np.diff(m["x"]), np.diff(m["y"])).sum()
    assert path == pytest.approx(monza.lap_length, rel=0.01)
    assert np.ptp(m["x"]) > np.ptp(m["y"])                           # laid out wider than tall
    assert m["y"].iloc[0] <= 0                                       # start/finish in the lower half
    assert np.allclose(np.diff(m["dist"]), STEP_M)


def test_rebuilt_map_closes_and_matches_the_true_shape(monza):
    truth = track_map(monza)[["x", "y"]].to_numpy()
    for kwargs, mean_limit in (({}, 45.0), ({"scale": 0.93, "offset": 0.03, "noise": 0.05}, 80.0)):
        m = track_map(without_position(**kwargs))
        assert m.attrs["source"] == "reconstructed" and m.attrs["closed"]
        assert m.attrs["direction"] == "clockwise"
        ends = np.hypot(m["x"].iloc[-1] - m["x"].iloc[0], m["y"].iloc[-1] - m["y"].iloc[0])
        assert ends <= 2 * STEP_M                                    # closed onto itself
        n = min(len(m), len(truth))
        mean, worst = fit_error(truth[:n], m[["x", "y"]].to_numpy()[:n])
        assert mean < mean_limit, f"{kwargs}: mean {mean:.0f} m"     # on a 10 km lap, 2.6 km across
        assert worst < 4 * mean_limit


def test_closing_improves_on_plain_integration(monza):
    m = track_map(without_position(scale=0.93, offset=0.03, noise=0.05))
    assert m.attrs["gap_m"] > 500                                    # how far apart the ends were before
    assert 0.8 < m.attrs["turn_scale"] < 1.25


def test_a_path_too_far_from_one_revolution_is_drawn_open(synthetic):
    # The synthetic circuit is a list of corners, not a closed loop
    m = track_map(synthetic)
    assert m.attrs["source"] == "reconstructed" and not m.attrs["closed"]
    assert report_data(synthetic)["track"]["closed"] is False


def test_needs_position_or_usable_lateral_g():
    raw = read_csv(GOLDEN / "synthetic_gt3_5lap.csv.gz").data[["Time", "LapNumber", "Speed", "SteeringAngle"]]
    with pytest.raises(ValueError, match="position channels, or speed and a usable lateral g"):
        track_map(build_session(parse_csv_text(raw.to_csv(index=False))))


def test_report_map_shares_the_lap_trace_grid(monza):
    d = report_data(monza)
    t = d["track"]
    n = len(d["traces"]["1"]["v"])
    assert len(t["x"]) == len(t["y"]) == n                           # one map point per chart point
    assert t["source"] == "position" and t["direction"] == "clockwise" and t["closed"]
    assert all(c["apex"] <= (n - 1) * d["meta"]["grid_step"] for c in d["corners"]["list"])
