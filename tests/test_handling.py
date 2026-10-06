"""Understeer and oversteer detection.

The synthetic session has no incidents, so a clean run must find none. Incidents
are then written into its steering and lateral g at known places, and must be
found there and nowhere else."""

import numpy as np
import pandas as pd
import pytest

from conftest import GOLDEN
from veltrix_engine import build_session, corners as C
from veltrix_engine.analysis import handling as H
from veltrix_engine.importers import parse_csv_text, read_csv
from veltrix_engine.report import report_data

SYNTH = GOLDEN / "synthetic_gt3_5lap.csv.gz"


def with_incidents(clean, edits):
    """The synthetic file with steering / lateral g changed over given stretches.
    edits: [(lap, from_m, to_m, function(rows, bump) -> None)]"""
    raw = read_csv(SYNTH).data.copy()
    ch = clean.channels
    for lap, d0, d1, change in edits:
        t = ch.loc[(ch["lap"] == lap) & ch["lap_distance"].between(d0, d1), "time"]
        rows = raw["Time"].between(t.min(), t.max())
        change(raw, rows, np.hanning(int(rows.sum())))
    return build_session(parse_csv_text(raw.to_csv(index=False)))


def understeer(raw, rows, bump):          # more lock, and the car turns a little less
    raw.loc[rows, "SteeringAngle"] += 45 * bump * np.sign(raw.loc[rows, "SteeringAngle"].mean())
    raw.loc[rows, "LateralAcc"] *= 1 - 0.12 * bump


def opposite_lock(raw, rows, bump):       # steering swings against the corner and comes back
    raw.loc[rows, "SteeringAngle"] -= 1.6 * raw.loc[rows, "SteeringAngle"] * bump


@pytest.fixture(scope="module")
def staged(synthetic):
    # T4 is a left-hander with its apex at 1250 m; T8 a left-hander with its apex at 3080 m
    s = with_incidents(synthetic, [(3, 1215, 1275, understeer), (4, 3085, 3120, opposite_lock)])
    return s, H.analyse(s)


def test_a_clean_session_has_no_incidents(synthetic):
    r = H.analyse(synthetic)
    assert r["events"].empty
    assert (r["by_corner"][["understeer", "oversteer"]].to_numpy() == 0).all()
    assert set(r["tendency"]["read"]) == {"neutral"}
    # the fitted steering relationship recovers the built-in steering ratio
    assert r["model"]["kinematic"] == pytest.approx(14.0, rel=0.05)


def test_staged_incidents_are_found_in_the_right_lap_and_corner(staged):
    _, r = staged
    ev = r["events"]
    assert [(e.lap, e.corner, e.kind) for e in ev.itertuples()] == [(3, "T4", "understeer"), (4, "T8", "oversteer")]
    us, os_ = ev.iloc[0], ev.iloc[1]
    assert 1215 <= us.peak_m <= 1275 and us.peak_deg == pytest.approx(45, abs=8)
    assert us.phase == "apex" and not us.counter_steer and us.severity == "severe"
    assert 3085 <= os_.peak_m <= 3120 and os_.peak_deg < -30
    assert os_.counter_steer and os_.opposite_lock_deg > COUNTER_MIN and os_.direction == "L"
    assert "understeer" in us.cause and "oversteer" in os_.cause


COUNTER_MIN = H.COUNTER_STEER_DEG


def test_per_corner_counts_follow_the_incidents(staged):
    _, r = staged
    bc = r["by_corner"].set_index("corner")
    assert (bc.loc["T4", "understeer"], bc.loc["T4", "oversteer"], bc.loc["T4", "laps_affected"]) == (1, 0, 1)
    assert (bc.loc["T8", "understeer"], bc.loc["T8", "oversteer"]) == (0, 1)
    assert bc.drop(index=["T4", "T8"])[["understeer", "oversteer"]].to_numpy().sum() == 0
    # one lap out of four does not change a corner's habit
    assert set(r["tendency"]["read"]) == {"neutral"}


def test_detection_does_not_depend_on_corner_direction(staged, synthetic):
    """The same session mirrored left to right must give the same incidents."""
    raw = read_csv(SYNTH).data.copy()
    ch = synthetic.channels
    for lap, d0, d1, change in [(3, 1215, 1275, understeer), (4, 3085, 3120, opposite_lock)]:
        t = ch.loc[(ch["lap"] == lap) & ch["lap_distance"].between(d0, d1), "time"]
        rows = raw["Time"].between(t.min(), t.max())
        change(raw, rows, np.hanning(int(rows.sum())))
    raw["SteeringAngle"] *= -1
    raw["LateralAcc"] *= -1
    swap = {f"SuspTravel{a}": f"SuspTravel{b}" for a, b in (("FL", "FR"), ("FR", "FL"), ("RL", "RR"), ("RR", "RL"))}
    raw = raw.rename(columns=swap)
    mirrored = H.analyse(build_session(parse_csv_text(raw.to_csv(index=False))))["events"]
    original = staged[1]["events"]
    assert list(mirrored["kind"]) == list(original["kind"])
    assert list(mirrored["corner"]) == list(original["corner"])
    assert list(mirrored["direction"]) == ["R", "R"]
    assert np.allclose(mirrored["peak_deg"], original["peak_deg"], atol=1.0)


def test_a_brief_twitch_is_not_an_incident(synthetic):
    def twitch(raw, rows, bump):
        raw.loc[rows, "SteeringAngle"] += 60.0
    s = with_incidents(synthetic, [(3, 1248, 1250, twitch)])            # about 0.05 s
    assert H.analyse(s)["events"].empty


def test_steering_for_the_next_corner_is_not_counted_as_opposite_lock(synthetic):
    """Through a direction change the steering leads the car and never comes back.
    T2 (right) is followed by T3 (left): turning in early for T3 is not a correction."""
    def early_turn_in(raw, rows, bump):
        raw.loc[rows, "SteeringAngle"] = 25.0                           # left, while still loaded right
    s = with_incidents(synthetic, [(3, 700, 800, early_turn_in)])
    ev = H.analyse(s)["events"]
    assert not ev["counter_steer"].any()


def test_needs_steering_and_usable_lateral_g(monza):
    with pytest.raises(ValueError, match="steering and a usable lateral g"):
        H.analyse(monza)                                                # no lateral g in this file
    raw = read_csv(SYNTH).data
    raw = raw.drop(columns=[c for c in raw.columns if c.startswith("SuspTravel")])
    raw["LateralAcc"] = raw["LateralAcc"].round()                       # coarse, and no dampers to rebuild it
    with pytest.raises(ValueError):
        H.analyse(build_session(parse_csv_text(raw.to_csv(index=False))))


def test_works_on_rebuilt_lateral_g(synthetic):
    raw = read_csv(SYNTH).data.copy()
    hold = lambda x: np.repeat(np.asarray(x)[::5], 5)[:len(x)]
    raw["LateralAcc"] = np.round(hold(raw["LateralAcc"]))
    s = build_session(parse_csv_text(raw.to_csv(index=False)))
    r = H.analyse(s)
    assert r["model"]["lateral_source"] == "rebuilt from damper roll"
    assert r["events"].empty


def test_report_carries_incidents(staged, synthetic, monza):
    d = report_data(staged[0])["handling"]
    assert [(e["lap"], e["corner"], e["kind"]) for e in d["events"]] == [(3, "T4", "understeer"), (4, "T8", "oversteer")]
    assert d["events"][1]["lock"] > 0 and d["events"][0]["lock"] is None
    assert len(d["by_corner"]) == len(d["tendency"]) == 9
    assert report_data(synthetic)["handling"]["events"] == []
    assert report_data(monza)["handling"] is None
