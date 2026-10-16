"""Suggestions: built from the session's own numbers, silent when there is
nothing to say, and pointing the right way on staged cases."""

import numpy as np
import pandas as pd
import pytest

from conftest import GOLDEN
from veltrix_engine import build_session, corners as C
from veltrix_engine.analysis import advice, handling
from veltrix_engine.importers import parse_csv_text, read_csv
from veltrix_engine.report import report_data
from veltrix_engine.synth import generate_session

SYNTH = GOLDEN / "synthetic_gt3_5lap.csv.gz"
FIELDS = {"area", "title", "where", "evidence", "action", "confidence", "gain_s"}


def test_a_session_with_nothing_to_fix_gets_no_suggestions(synthetic):
    r = advice.suggest(synthetic)
    assert r == {"driver": [], "setup": []}


def test_every_item_has_evidence_and_a_confidence(monza):
    r = advice.suggest(monza)
    assert r["driver"], "four flying laps of a sim session should give driver suggestions"
    for item in r["driver"] + r["setup"]:
        assert set(item) == FIELDS
        assert item["evidence"] and all(any(ch.isdigit() for ch in e) for e in item["evidence"])
        assert item["confidence"] in ("high", "medium", "low")
        assert item["action"].endswith(".")
    gains = [i["gain_s"] for i in r["driver"][1:] if i["gain_s"]]
    assert gains == sorted(gains, reverse=True)                 # most worthwhile first


def test_headline_gain_is_best_lap_minus_ideal_lap(monza):
    r = advice.suggest(monza)
    corner = C.analyse(monza)
    best = monza.laps.loc[monza.laps["status"] == "flying", "lap_time_s"].min()
    assert r["driver"][0]["where"] == "whole lap"
    assert r["driver"][0]["gain_s"] == pytest.approx(best - corner["ideal_lap_s"], abs=1e-6)


def staged_session(change, laps=8, seed=7):
    """Eight laps of the synthetic circuit with the same change on most laps."""
    raw = generate_session(lap_count=laps, seed=seed, lockup_lap=0)
    base = build_session(parse_csv_text(raw.to_csv(index=False)))
    ch = base.channels
    for lap, d0, d1 in change["where"]:
        t = ch.loc[(ch["lap"] == lap) & ch["lap_distance"].between(d0, d1), "time"]
        rows = raw["Time"].between(t.min(), t.max())
        change["do"](raw, rows, np.hanning(int(rows.sum())))
    return build_session(parse_csv_text(raw.to_csv(index=False)))


def test_repeated_power_oversteer_gives_a_driver_tip_and_a_setup_direction():
    # Opposite lock on full throttle at the exit of T4 (apex 1250 m) and T8 (apex 3080 m), most laps
    def slide(raw, rows, bump):
        raw.loc[rows, "SteeringAngle"] -= 1.7 * raw.loc[rows, "SteeringAngle"] * bump
        raw.loc[rows, "ThrottlePos"] = 100.0
        raw.loc[rows, "BrakePress"] = 0.0
    # on a minority of laps: a slide on most laps would be the car's norm, not an incident
    where = [(lap, 1262, 1300) for lap in (3, 6, 8)] + [(lap, 3092, 3130) for lap in (4, 5, 7)]
    s = staged_session({"where": where, "do": slide})
    ev = handling.analyse(s)["events"]
    assert set(ev["corner"]) == {"T4", "T8"} and ev["cause"].str.startswith("power oversteer").all()

    r = advice.suggest(s)
    driver = [i for i in r["driver"] if "power oversteer" in i["title"]]
    assert {i["where"] for i in driver} == {"T4", "T8"}
    assert all("throttle" in i["action"] for i in driver)
    setup = [i for i in r["setup"] if i["title"] == "Rear traction on corner exit"]
    assert len(setup) == 1
    assert "rear anti-roll bar" in setup[0]["action"] and "T4" in setup[0]["where"] and "T8" in setup[0]["where"]


def test_repeated_understeer_points_at_front_grip():
    def push(raw, rows, bump):
        raw.loc[rows, "SteeringAngle"] += 50 * bump * np.sign(raw.loc[rows, "SteeringAngle"].mean())
        raw.loc[rows, "LateralAcc"] *= 1 - 0.10 * bump
    where = [(lap, 1215, 1275) for lap in (3, 6, 8)] + [(lap, 3045, 3105) for lap in (4, 5, 7)]
    r = advice.suggest(staged_session({"where": where, "do": push}))
    setup = [i for i in r["setup"] if i["title"].startswith("Front grip")]
    assert len(setup) == 1 and "front anti-roll bar" in setup[0]["action"]
    assert not any("Rear traction" in i["title"] for i in r["setup"])


def test_braking_later_on_the_best_pass_becomes_a_brake_later_tip(synthetic):
    """Same corner, four flying laps: three brake at 480 m, the fastest at 505 m."""
    found = C.find_corners(synthetic)
    metrics = C.corner_metrics(synthetic, found)
    t2 = metrics["corner"] == "T2"
    turn_in = float(metrics.loc[t2, "turn_in_m"].iloc[0])
    for lap, brake, time in ((2, 480.0, 9.30), (3, 482.0, 9.32), (4, 505.0, 9.00), (5, 479.0, 9.31)):
        metrics.loc[t2 & (metrics["lap"] == lap), ["brake_m", "brake_before_m", "zone_time_s"]] = [brake, turn_in - brake, time]
    summary = C.corner_summary(synthetic, found, metrics)
    r = advice.suggest(synthetic, {"corners": found, "metrics": metrics, "summary": summary}, None)
    tip = next(i for i in r["driver"] if i["where"].startswith("T2 "))
    assert tip["title"] == "T2: Brake about 24 m later"
    assert tip["gain_s"] == pytest.approx(0.2325, abs=1e-3)           # typical 9.2325 s against the best 9.00 s
    # quoted as a driver reads it: metres before the corner, not from the start/finish line
    line = next(e for e in tip["evidence"] if e.startswith("Braking point"))
    assert f"{turn_in - 481:.0f} m before the corner" in line and f"{turn_in - 505:.0f} m before" in line


def test_a_slide_on_most_laps_is_the_norm_not_an_incident():
    def slide(raw, rows, bump):
        raw.loc[rows, "SteeringAngle"] -= 1.7 * raw.loc[rows, "SteeringAngle"] * bump
    where = [(lap, 1262, 1300) for lap in (2, 3, 4, 5, 6, 7)] + [(lap, 3092, 3130) for lap in (2, 3, 4, 5, 6, 8)]
    s = staged_session({"where": where, "do": slide})
    h = handling.analyse(s)
    assert not (h["events"]["kind"] == "understeer").any()    # the tidy laps are not called understeer
    tend = h["tendency"].set_index("corner")["read"]
    assert tend["T4"] == "oversteer" and tend["T8"] == "oversteer"
    # the steering residual alone is not enough: it is measured against a model fitted to the same session
    assert not any("norm" in i["title"] for i in advice.suggest(s)["setup"]), "no body slip, no setup hypothesis"

    # with the car measured travelling sideways at those exits, it is a setup hypothesis
    corners = C.find_corners(s)
    d = s.frame()["lap_distance"].to_numpy()
    sliding = np.zeros(len(d), bool)
    for c in corners[corners["corner"].isin(["T4", "T8"])].itertuples():
        sliding |= (d >= c.start_m) & (d <= c.end_m)
    s.derived["body_slip"] = np.where(sliding, 4.0, 0.5)
    setup = advice.suggest(s)["setup"]
    item = next(i for i in setup if "norm" in i["title"])
    assert "T4" in item["where"] and "T8" in item["where"] and any("Body slip" in e for e in item["evidence"])

    # and with body slip measured small there, it is not
    s.derived["body_slip"] = np.full(len(d), 0.6)
    assert not any("norm" in i["title"] for i in advice.suggest(s)["setup"])


def test_track_cut_is_reported(synthetic):
    laps = synthetic.laps.copy()
    laps.loc[laps["lap"] == 4, ["status", "dist_m"]] = ["short", synthetic.lap_length - 40]
    import copy
    s = copy.copy(synthetic)
    s.laps = laps
    tips = [i for i in advice.suggest(s)["driver"] if i["title"] == "Track limits"]
    assert len(tips) == 1 and "4" in tips[0]["where"] and tips[0]["confidence"] == "high"


def test_camber_direction_follows_the_temperature_spread():
    raw = read_csv(SYNTH).data.copy()
    raw["TyreTempFL_inner"] += 12          # inner edge now ~22 °C hotter than the outer
    raw["TyreTempRR_inner"] -= 8           # and here only ~2 °C
    r = advice.suggest(build_session(parse_csv_text(raw.to_csv(index=False))))
    by_title = {i["title"]: i for i in r["setup"]}
    assert by_title["FL camber"]["action"].startswith("Reduce negative camber")
    assert by_title["RR camber"]["action"].startswith("Add negative camber")
    assert "FR camber" not in by_title


def test_report_carries_suggestions(monza, synthetic):
    d = report_data(monza)["advice"]
    assert d["driver"] and set(d["driver"][0]) == FIELDS
    assert report_data(synthetic)["advice"] == {"driver": [], "setup": []}
