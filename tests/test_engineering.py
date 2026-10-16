"""The engineering report: nine sections for every session, and every
conclusion carrying its evidence, calculation, confidence and recommendation."""

import json

import numpy as np
import pandas as pd
import pytest

from conftest import GOLDEN
from test_advice import staged_session
from veltrix_engine import ProjectStore, build_session, corners as C
from veltrix_engine.analysis import engineering
from veltrix_engine.cli import main as cli_main
from veltrix_engine.importers import parse_csv_text
from veltrix_engine.report import render_report, report_data
from veltrix_engine.workspace import Workspace

TITLES = ["Lap potential", "Driver performance", "Braking", "Cornering", "Traction", "Tyres", "Vehicle balance",
          "Incidents", "Setup hypotheses"]


def findings(report, section=None):
    return [f for s in report["sections"] if section in (None, s["id"]) for f in s["findings"]]


def section(report, sid):
    return next(s for s in report["sections"] if s["id"] == sid)


@pytest.fixture(scope="module")
def reports(sessions):
    return {key: engineering.analyse(s) for key, s in sessions.items()}


@pytest.fixture(scope="module")
def sliding():
    """Eight laps with opposite lock on full throttle out of T4 and T8 on three laps each."""
    def slide(raw, rows, bump):
        raw.loc[rows, "SteeringAngle"] -= 1.7 * raw.loc[rows, "SteeringAngle"] * bump
        raw.loc[rows, "ThrottlePos"] = 100.0
        raw.loc[rows, "BrakePress"] = 0.0
    where = [(lap, 1262, 1300) for lap in (3, 6, 8)] + [(lap, 3092, 3130) for lap in (4, 5, 7)]
    return staged_session({"where": where, "do": slide})


# ── shape ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("key", ["synthetic", "logger", "monza"])
def test_every_session_gets_all_nine_sections_in_order(reports, key):
    report = reports[key]
    assert [s["title"] for s in report["sections"]] == TITLES
    for s in report["sections"]:
        assert bool(s["findings"]) != bool(s["not_assessed"]), "a section has findings or says why it has none"
    json.dumps(report)                                     # plain values all the way down
    assert report["summary"]["findings"] == len(findings(report))
    assert report["summary"]["assessed"] + report["summary"]["not_assessed"] != [] and \
        set(report["summary"]["assessed"]) | set(report["summary"]["not_assessed"]) == set(TITLES)


@pytest.mark.parametrize("key", ["synthetic", "logger", "monza"])
def test_every_conclusion_has_evidence_calculation_confidence_and_recommendation(reports, key):
    for f in findings(reports[key]):
        assert f["conclusion"] and not f["conclusion"].endswith(".")
        assert f["evidence"] and all(isinstance(e, str) and e for e in f["evidence"])
        if f["where"] != "the file":                       # import warnings are quoted as they are
            assert any(ch.isdigit() for e in f["evidence"] for ch in e), f["conclusion"]
        assert len(f["calculation"]) > 40, "the calculation says how the numbers were worked out"
        assert f["confidence"]["level"] in ("high", "medium", "low") and f["confidence"]["basis"]
        assert f["recommendation"].endswith(".")
        assert not (f["gain_s"] and f["at_stake_s"])


def test_text_and_markdown_show_the_chain_for_every_finding(reports):
    report = reports["monza"]
    n = len(findings(report))
    text, md = engineering.to_text(report), engineering.to_markdown(report)
    for label in ("Evidence", "Calculation", "Confidence", "Recommendation"):
        assert text.count(f"     {label}") == n and md.count(f"**{label}") == n
    assert [line for line in text.splitlines() if line.isupper()] == [t.upper() for t in TITLES]
    assert "Not assessed: it needs steering and a usable lateral acceleration channel." in text


# ── confidence is a rule ──────────────────────────────────────────────────────

def test_confidence_follows_laps_effect_size_and_data_quality(synthetic, sliding):
    few, many = engineering._Ctx(synthetic, None, None), engineering._Ctx(sliding, None, None)
    assert (few.n, many.n) == (4, 7)
    assert few.conf()["level"] == "low" and "4 flying laps" in few.conf()["basis"]
    assert many.conf()["level"] == "medium"
    assert many.conf(n=9)["level"] == "high"
    assert many.conf(n=9, effect=1.0, scatter=0.8)["level"] == "medium"          # 1.25 × its scatter
    assert many.conf(n=9, effect=1.0, scatter=2.0)["level"] == "low"
    capped = many.conf(n=9, caps=[("medium", "a rule of thumb")])
    assert capped["level"] == "medium" and capped["basis"].endswith("a rule of thumb")
    many.session.coarse["speed"] = 1.0
    try:
        assert many.conf(n=9, uses=("speed",))["level"] == "low"
    finally:
        del many.session.coarse["speed"]


def test_four_flying_laps_never_give_more_than_low_confidence_in_a_comparison(reports):
    for f in findings(reports["monza"]):
        if "flying lap" in f["confidence"]["basis"]:
            assert f["confidence"]["level"] == "low"


# ── the numbers ───────────────────────────────────────────────────────────────

def test_lap_potential_is_best_lap_against_the_sum_of_best_zones(monza, reports):
    report = reports["monza"]
    corner = C.analyse(monza)
    best = monza.laps.loc[monza.laps["status"] == "flying", "lap_time_s"].min()
    head = section(report, "lap_potential")["findings"][0]
    assert head["gain_s"] == pytest.approx(best - corner["ideal_lap_s"], abs=1e-3)
    assert report["summary"]["gain_s"] == head["gain_s"]
    assert "2:37.680" in head["evidence"][0] and "Σ" in head["calculation"]
    top = corner["summary"].sort_values("available_s", ascending=False)["corner"].tolist()[:3]
    assert [p["corner"] for p in report["summary"]["priorities"]] == top
    assert all(p["findings"] for p in report["summary"]["priorities"]), "each priority corner is explained somewhere"


def test_time_in_a_zone_is_not_counted_as_a_gain_by_every_section_that_mentions_it(reports):
    report = reports["monza"]
    phase = findings(report, "braking") + findings(report, "cornering") + findings(report, "traction")
    assert any(f["at_stake_s"] for f in phase) and not any(f["gain_s"] for f in phase)


def test_braking_finding_quotes_the_two_braking_points(monza, reports):
    f = next(f for f in findings(reports["monza"], "braking") if "braked later" in f["conclusion"])
    m = C.analyse(monza)["metrics"]
    m = m[m["lap"].isin([1, 2, 3, 4])]
    for line in f["evidence"]:
        corner = line.split(":")[0]
        part = m[m["corner"] == corner]
        best = part.loc[part["zone_time_s"].idxmin()]
        assert C.before_corner(part["brake_before_m"].median()) in line and C.before_corner(best["brake_before_m"]) in line
        assert best["brake_m"] > part["brake_m"].median(), "only a later braking point is called later"
        assert f"{corner} toward {C.before_corner(best['brake_before_m'])}" in f["recommendation"]


def test_the_braking_point_is_where_braking_starts_not_a_second_press(monza):
    # Lap 3 into T1 at Monza: on the brakes at 845 m, off, and on again at 941 m. The braking point
    # is 845 m; reading the second press made the fastest lap look as if it braked 52 m later.
    m = C.analyse(monza)["metrics"]
    t1 = m[(m["corner"] == "T1")].set_index("lap")
    assert 840 <= t1.loc[3, "brake_m"] <= 850
    # and as a driver reads it off the boards: about 90 m before turn-in, the same point for every lap
    assert t1["turn_in_m"].nunique() == 1 and 80 <= t1.loc[3, "turn_in_m"] - t1.loc[3, "brake_m"] <= 130
    assert (t1["brake_before_m"] == t1["turn_in_m"] - t1["brake_m"]).all()
    assert (t1["peak_brake_g"] < 2.6).all(), "no single-sample spike sets the peak (a GT3 car brakes at about 2 g)"


def test_a_clean_session_says_so_with_evidence(reports):
    report = reports["synthetic"]
    assert section(report, "lap_potential")["findings"][0]["conclusion"].startswith("The best lap is the ideal lap")
    assert findings(report, "incidents")[0]["conclusion"] == "No understeer or oversteer incident on any lap"
    assert findings(report, "setup_hypotheses")[0]["conclusion"] == "No setup change is indicated by this session"
    assert any("inside the operating window" in f["conclusion"] for f in findings(report, "tyres"))
    assert any("falls away by 126 ms a lap" in f["conclusion"] for f in findings(report, "tyres"))
    assert any(f["conclusion"].startswith("In steady cornering the car is close to neutral") for f in findings(report, "vehicle_balance"))


def test_repeated_power_oversteer_runs_through_incidents_driver_and_setup(sliding):
    report = engineering.analyse(sliding)
    incidents = findings(report, "incidents")[0]
    assert "balance-incident candidates" in incidents["conclusion"] and "power oversteer" in " ".join(incidents["evidence"])
    driver = [f for f in findings(report, "driver_performance") if "power oversteer" in f["conclusion"]]
    assert {f["where"] for f in driver} == {"T4", "T8"} and all("throttle" in f["recommendation"] for f in driver)
    hypothesis = next(f for f in findings(report, "setup_hypotheses") if "rear traction" in f["conclusion"])
    assert hypothesis["conclusion"].startswith("Hypothesis:")
    assert hypothesis["hypothesis"].startswith("Rear traction on corner exit is what limits the car in")
    assert hypothesis["investigation"] == hypothesis["recommendation"] and hypothesis["interpretation"] == hypothesis["conclusion"]
    assert "rules of thumb" in hypothesis["confidence"]["basis"]
    if hypothesis["confidence"]["level"] == "low":           # six incidents: a pattern, but thin
        assert "too thin to suggest a setup change" in hypothesis["recommendation"]
        assert "anti-roll bar" not in hypothesis["recommendation"], "no setup advice without adequate evidence"
        assert "fewer power-oversteer incidents" in hypothesis["recommendation"], "but it says what to look for"
    else:
        assert "rear anti-roll bar" in hypothesis["recommendation"] and "confirmed by fewer power-oversteer" in hypothesis["recommendation"]
    text = engineering.to_text(report)
    assert "     Hypothesis      Rear traction on corner exit is what limits the car" in text
    assert any("power-oversteer incidents" in f["conclusion"] for f in findings(report, "traction"))


# ── sessions the analysis cannot do much with ─────────────────────────────────

def test_a_file_without_laps_still_gets_nine_sections_with_reasons():
    t = np.arange(0, 60, 0.1)
    raw = parse_csv_text(pd.DataFrame({"Time": t, "Speed": 100 + 40 * np.sin(t / 5)}).to_csv(index=False))
    report = engineering.analyse(build_session(raw))
    assert [s["title"] for s in report["sections"]] == TITLES
    assert all(s["not_assessed"] for s in report["sections"] if s["id"] not in ("incidents",))
    assert "no lap information" in section(report, "lap_potential")["not_assessed"]
    assert "tyre temperatures" in section(report, "tyres")["not_assessed"]
    assert report["summary"]["gain_s"] is None and report["summary"]["flying_laps"] == 0
    engineering.to_text(report), engineering.to_markdown(report)


def test_monza_without_lateral_g_names_what_is_missing(reports):
    report = reports["monza"]
    assert "lateral acceleration" in section(report, "vehicle_balance")["not_assessed"]
    assert "nothing a setup pattern can be read from" in section(report, "setup_hypotheses")["not_assessed"]
    assert "tyre temperatures" in section(report, "tyres")["note"], "degradation is assessed, temperatures are not"


# ── where the report turns up ─────────────────────────────────────────────────

def test_the_html_report_carries_it(monza, reports):
    data = report_data(monza)
    assert data["engineering"] == reports["monza"]
    page = render_report(monza)
    assert 'data-tab="engineering"' in page and page.index('data-tab="engineering"') < page.index('data-tab="advice"')
    assert "The fastest pass braked later into" in page


def test_every_stored_session_has_its_report(tmp_path, reports):
    source = GOLDEN / "logger_layout_3lap_20hz.csv"
    store = ProjectStore(tmp_path / "project")
    try:
        sid = store.import_file(source)
        saved = tmp_path / "project" / "sessions" / str(sid) / "engineering.json"
        assert json.loads(saved.read_text()) == reports["logger"] == store.engineering(sid)
        saved.unlink()                                       # a session stored before reports existed
        assert store.engineering(sid) == reports["logger"] and saved.exists()
    finally:
        store.close()
    ws = Workspace(tmp_path / "ws")
    added = ws.add_file(source.name, source.read_bytes())
    assert [s["title"] for s in ws.session(added["session"]["id"])["engineering"]["sections"]] == TITLES


def test_cli_prints_and_exports_the_report(tmp_path, capsys):
    out_json, out_md = tmp_path / "r.json", tmp_path / "r.md"
    assert cli_main(["engineer", str(GOLDEN / "monza_ac_gt3.csv.gz"), "--json", str(out_json), "--markdown", str(out_md)]) == 0
    printed = capsys.readouterr().out
    assert "LAP POTENTIAL" in printed and "SETUP HYPOTHESES" in printed and "Recommendation" in printed
    assert [s["title"] for s in json.loads(out_json.read_text())["sections"]] == TITLES
    assert out_md.read_text().startswith("# Performance engineering report — monza_ac_gt3.csv.gz")


def test_setup_changes_are_only_suggested_on_adequate_evidence():
    """Sixteen laps with the slide on a third of them in each of two corners
    (a slide on most laps would be the car's norm, not an incident): ten
    incidents, enough for directions to test."""
    def slide(raw, rows, bump):
        raw.loc[rows, "SteeringAngle"] -= 1.7 * raw.loc[rows, "SteeringAngle"] * bump
        raw.loc[rows, "ThrottlePos"] = 100.0
        raw.loc[rows, "BrakePress"] = 0.0
    where = [(lap, 1262, 1300) for lap in (2, 5, 8, 11, 14)] + [(lap, 3092, 3130) for lap in (3, 6, 9, 12, 15)]
    report = engineering.analyse(staged_session({"where": where, "do": slide}, laps=16))
    hypothesis = next(f for f in findings(report, "setup_hypotheses") if "rear traction" in f["conclusion"])
    assert hypothesis["confidence"]["level"] == "medium" and hypothesis["label"] == "hypothesis/recommendation"
    assert "rear anti-roll bar" in hypothesis["recommendation"] and "Change one thing" in hypothesis["recommendation"]
    assert all(f["hypothesis"] is None for f in findings(report, "braking")), "only the setup section states hypotheses"
