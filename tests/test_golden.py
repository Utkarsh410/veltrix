"""
Numerical contract: the engine must reproduce the frozen outputs in
datasets/expected/ on the golden datasets.

A failure here means a calculation changed. If the change is intended, re-run
tools/freeze_expected.py and review the diff; otherwise it is a regression.
"""

import json

import numpy as np
import pytest

from conftest import DATASETS, EXPECTED, expected
from veltrix_engine import laps as lap_engine
from veltrix_engine.analysis import gg, tyres
from veltrix_engine.models import arb, pacejka

RTOL, ATOL = 1e-6, 1e-7


def close(actual, wanted):
    a = np.array([np.nan if v is None else v for v in np.atleast_1d(actual)], float)
    w = np.array([np.nan if v is None else v for v in np.atleast_1d(wanted)], float)
    np.testing.assert_allclose(a, w, rtol=RTOL, atol=ATOL, equal_nan=True)


@pytest.mark.parametrize("key", DATASETS)
def test_session_matches_frozen_outputs(sessions, key):
    s, exp = sessions[key], expected(key)
    assert s.raw.sha256 == exp["source_sha256"], "golden dataset file has changed"
    assert s.rate_hz == exp["rate_hz"]
    assert s.mapping == exp["mapping"]
    assert s.units == exp["units"]
    assert s.warnings() == exp["warnings"]
    assert s.lap_source == exp["lap_source"]
    assert s.crossing_method == exp["crossing_method"]

    assert [r.lap for r in s.laps.itertuples()] == [l["lap"] for l in exp["laps"]]
    assert list(s.laps["status"]) == [l["status"] for l in exp["laps"]]
    for field in ("t_start", "t_end", "lap_time_s", "dist_m"):
        close(s.laps[field].to_numpy(), [l[field] for l in exp["laps"]])

    frame = s.frame()
    assert sorted(c for c in frame.columns if c != "lap") == sorted(exp["channels"])
    for cid, fp in exp["channels"].items():
        x = frame[cid].to_numpy(float)
        ok = np.isfinite(x)
        assert len(x) == fp["n"] and int(ok.sum()) == fp["n_valid"], cid
        close([x[ok].mean(), x[ok].std(), x[ok].min(), x[ok].max()],
              [fp["mean"], fp["std"], fp["min"], fp["max"]])
        idx = np.linspace(0, len(x) - 1, len(fp["samples"])).round().astype(int)
        close(x[idx], fp["samples"])


@pytest.mark.parametrize("key", DATASETS)
def test_lap_analysis_matches_frozen_outputs(sessions, key):
    s, exp = sessions[key], expected(key)
    assert lap_engine.best_lap(s) == exp["best_lap"]
    sectors = lap_engine.sector_times(s)
    for row, want in zip(sectors.to_dict("records"), exp["sectors"]):
        assert row["lap"] == want["lap"]
        close([row["s1"], row["s2"], row["s3"]], [want["s1"], want["s2"], want["s3"]])
    close(lap_engine.theoretical_best(sectors), exp["theoretical_best"])

    d = exp["delta"]
    cmp_ = lap_engine.compare_laps(s, d["ref"], d["lap"], channels=("speed",))
    delta = cmp_["delta_t"].to_numpy()
    idx = np.linspace(0, len(delta) - 1, len(d["delta_t"]["samples"])).round().astype(int)
    close(delta[idx], d["delta_t"]["samples"])
    loss, gain = lap_engine.biggest_swing(delta, cmp_.index.to_numpy())
    close(loss, d["biggest_loss"])
    close(gain, d["biggest_gain"])


@pytest.mark.parametrize("key", ["synthetic", "logger"])
def test_analysis_matches_frozen_outputs(sessions, key):
    s, exp = sessions[key], expected(key)
    summary = gg.gg_summary(s)
    close([summary[k] for k in exp["gg"]], list(exp["gg"].values()))
    close(list(gg.phase_split(s).values()), list(exp["gg_phases"].values()))

    events = tyres.detect_anomalies(s)
    assert [(int(e.lap), e.corner) for e in events.itertuples()] == \
           [(e["lap"], e["corner"]) for e in exp["tyre_anomalies"]]
    deg = tyres.fit_degradation(s)
    close([deg["slope"], deg["intercept"], deg["r_squared"]],
          [exp["degradation"][k] for k in ("slope", "intercept", "r_squared")])
    assert deg["threshold_age"] == exp["degradation"]["threshold_age"]

    for row, want in zip(tyres.lap_summary(s).to_dict("records"), exp["tyre_lap_summary"]):
        nums = [k for k, v in want.items() if isinstance(v, float)]
        close([row[k] for k in nums], [want[k] for k in nums])


def test_models_match_frozen_outputs():
    exp = json.loads((EXPECTED / "models.json").read_text())
    alpha, kappa = np.linspace(-14, 14, 57), np.linspace(-0.25, 0.25, 51)
    for axle, coeffs in (("front", pacejka.FRONT), ("rear", pacejka.REAR)):
        e = exp["pacejka"][axle]
        close(pacejka.lateral_force(alpha, 3500.0, coeffs), e["Fy_3500"])
        close(pacejka.longitudinal_force(kappa, 3500.0, coeffs), e["Fx_3500"])
        for want in e["peaks"]:
            got = pacejka.peak_lateral(want["Fz"], coeffs)
            close([got[k] for k in want], list(want.values()))
    sweep = arb.sweep_front_arb(n=13)
    for col in ("K_us", "C_front", "C_rear", "lltd_front", "min_inside_Fz"):
        close(sweep[col].to_numpy(), [r[col] for r in exp["arb_sweep"]])
