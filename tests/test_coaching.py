"""Phase 11: coaching cards — per corner, what the fastest pass did and what to try."""

import numpy as np
import pytest

from conftest import GOLDEN
from test_advice import staged_session
from veltrix_engine.analysis import coaching
from veltrix_engine.cli import main as cli_main


def test_a_later_braking_point_on_the_fastest_pass_becomes_a_card(synthetic):
    import copy
    from veltrix_engine import corners as C
    found = C.find_corners(synthetic)
    metrics = C.corner_metrics(synthetic, found)
    t2 = metrics["corner"] == "T2"
    turn_in = float(metrics.loc[t2, "turn_in_m"].iloc[0])
    for lap, brake, time in ((2, 480.0, 9.30), (3, 482.0, 9.32), (4, 505.0, 9.00), (5, 479.0, 9.31)):
        metrics.loc[t2 & (metrics["lap"] == lap), ["brake_m", "brake_before_m", "zone_time_s"]] = [brake, turn_in - brake, time]
    summary = C.corner_summary(synthetic, found, metrics)
    r = coaching.cards(synthetic, {"corners": found, "metrics": metrics, "summary": summary,
                                   "ideal_lap_s": summary.attrs["ideal_lap_s"]})
    k = next(x for x in r["cards"] if x["corner"] == "T2")
    assert k["best_lap"] == 4 and k["gap_s"] == pytest.approx(9.305 - 9.00, abs=1e-3)
    assert k["references"]["Braking point"] == (C.before_corner(turn_in - 481), C.before_corner(turn_in - 505))
    assert any(t.startswith("Brake later") and "24 m later" in t for t in k["tries"])
    assert r["cards"] == sorted(r["cards"], key=lambda x: -x["gap_s"])


def test_cards_are_honest_about_where_the_corner_numbers_come_from(synthetic):
    r = coaching.cards(synthetic)
    assert "not the circuit's own" in r["summary"]["corners"]
    text = coaching.to_markdown(r)
    assert "no single lap did them all" in text
    page = coaching.to_html(r)
    assert page.startswith("<!doctype html>") and "@page{size:A4" in page and "<script" not in page


def test_a_corner_cut_is_never_the_pass_to_copy(synthetic):
    import copy
    from veltrix_engine import corners as C
    s = copy.copy(synthetic)
    s.channels = synthetic.channels.copy()
    m = C.corner_metrics(s, C.find_corners(s))
    best = m.sort_values("zone_time_s").groupby("corner").head(1).set_index("corner")
    corner = best["zone_time_s"].sub(m.groupby("corner")["zone_time_s"].median()).idxmin()
    lap = int(best.loc[corner, "lap"])
    c = C.find_corners(s).set_index("corner").loc[corner]
    at = s.channels.index[(s.channels["lap"] == lap) & (s.channels["lap_distance"] >= c.start_m)][0]
    s.channels["incident_count"] = 0.0
    s.channels.loc[at:, "incident_count"] = 1.0
    r = coaching.cards(s)
    assert all(k["best_lap"] != lap for k in r["cards"] if k["corner"] == corner)


def test_the_coach_command(tmp_path, capsys):
    out = tmp_path / "cards.html"
    assert cli_main(["coach", str(GOLDEN / "synthetic_gt3_5lap.csv.gz"), "--html", str(out), "--no-app-data"]) == 0
    assert "# Driver coaching" in capsys.readouterr().out and out.read_text().startswith("<!doctype html>")
