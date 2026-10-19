"""Phase 10: wheel slip, damper speed, roll, pitch and tyre slip angles — signs, and checks against known cases."""

import numpy as np
import pytest

from real_sessions import CORPUS, find


def test_roll_pitch_and_estimated_damper_speed_have_the_right_signs(synthetic):
    d, ch = synthetic.derived, synthetic.channels
    ay = ch["acc_lat"].to_numpy()
    left, right = ay > 0.8, ay < -0.8
    assert np.nanmedian(d["roll_front_deg"][left]) > 0 > np.nanmedian(d["roll_front_deg"][right]), \
        "the right side compresses in a left-hand corner"
    braking = synthetic.derived["acc_long_calc"].to_numpy() < -0.8
    assert np.nanmedian(d["pitch_deg"][braking]) > 0, "nose down under braking"
    assert "damper_vel_fl_est" in d and "damper_vel_fl" not in ch, "estimated only when the file logs none"
    from veltrix_engine.derived import lowpass
    travel = ch["susp_travel_fl"].to_numpy()
    want = lowpass(np.gradient(travel, 1 / synthetic.rate_hz), 20.0, synthetic.rate_hz)
    assert np.allclose(d["damper_vel_fl_est"].to_numpy(), want, equal_nan=True), "d(travel)/dt, 20 Hz low-pass"


def test_wheel_slip_is_zero_when_the_wheels_turn_at_road_speed(synthetic):
    import copy
    from veltrix_engine.derived import compute_derived
    ch = synthetic.channels.copy()
    for c in ("fl", "fr", "rl", "rr"):
        ch[f"wheel_speed_{c}"] = ch["speed"]
    ch.loc[ch.index[5000:5100], "wheel_speed_rl"] = ch["speed"].iloc[5000:5100] * 1.10
    d, _, _ = compute_derived(ch, synthetic.rate_hz, synthetic.vehicle)
    fast = ch["speed"].to_numpy() > 40
    assert np.nanmax(np.abs(d["slip_ratio_fl"].to_numpy()[fast])) < 1e-9
    assert d["slip_ratio_rl"].iloc[5050] == pytest.approx(0.10, abs=1e-9), "+ is a wheel spinning faster than the car"
    assert np.isnan(d["slip_ratio_fl"].to_numpy()[ch["speed"].to_numpy() < 20]).all(), "not worked out near standstill"


@pytest.mark.real
def test_on_spa_the_simulators_damper_speed_is_used_and_slip_angles_agree_with_the_steering():
    path = find(CORPUS["spa_gr86_ibt"])
    if path is None:
        pytest.skip("the Spa .ibt is not on this machine")
    from veltrix_engine import load_session
    from veltrix_engine.analysis import balance
    s = load_session(path)
    assert s.mapping["damper_vel_fl"] == "LFshockVel" and s.units["damper_vel_fl"] == "mm/s"
    assert "damper_vel_fl_est" not in s.derived
    f = s.frame()
    fly = f["lap"].isin(s.laps.loc[s.laps["status"] == "flying", "lap"]).to_numpy()
    ay = f["acc_lat"].to_numpy()
    m = fly & (np.abs(ay) > 0.8)
    side = np.sign(ay[m])                         # both slip angles are + in a left-hand corner
    per_g = np.nanmedian((f["slip_angle_front_deg"].to_numpy()[m] - f["slip_angle_rear_deg"].to_numpy()[m]) * side
                         / np.abs(ay[m]))
    k = balance.steering_characteristic(s)["understeer_deg_per_g"]
    assert np.sign(per_g) == np.sign(k) and abs(per_g - k) <= 0.3 * abs(k), (per_g, k)
    front = np.nanmedian(f["slip_angle_front_deg"].to_numpy()[m] * np.sign(ay[m]))
    assert 1.0 < front < 8.0, "a road car's front tyres in steady cornering"
