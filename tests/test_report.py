"""Session report: the data behind the page, and the page itself."""

import json
import re

import pytest

from conftest import GOLDEN
from veltrix_engine import build_session
from veltrix_engine.cli import main as cli_main
from veltrix_engine.importers import parse_csv_text, read_csv
from veltrix_engine.report import render_report, report_data, write_report


def page_data(html: str) -> dict:
    return json.loads(re.search(r"const D = (\{.*?\});\n", html, re.S).group(1).replace("<\\/", "</"))


def test_report_data_matches_the_session(synthetic):
    d = report_data(synthetic)
    assert [l["lap"] for l in d["laps"]] == list(synthetic.laps["lap"])
    assert d["meta"]["best_lap"] == 2
    assert d["laps"][1]["time"] == pytest.approx(synthetic.laps["lap_time_s"].iloc[1], abs=1e-3)
    assert set(d["traces"]) == {"2", "3", "4", "5"}                  # complete laps only
    n = len(d["traces"]["2"]["t"])
    assert all(len(d["traces"]["2"][k]) == n for k in ("v", "thr", "brk"))
    assert d["traces"]["2"]["t"][0] == pytest.approx(0.0, abs=0.05)
    assert len(d["sectors"]) == 4 and d["meta"]["theoretical"] <= min(s["time"] for s in d["sectors"])
    assert d["gg"]["summary"]["peak_lat_g"] == pytest.approx(1.83, abs=0.05)
    assert d["balance"]["ratio"] == pytest.approx(14.0, abs=0.7)
    assert d["balance"]["lltd"] == pytest.approx(53.0, abs=1.0)
    assert d["rebuilt"] == {}
    json.dumps(d)                                                    # nothing left that JSON cannot hold


def test_sections_without_data_are_left_out(monza, logger):
    d = report_data(monza)                    # no lateral g, no tyres, no dampers
    assert d["gg"] is None and d["balance"] == {"ratio": None, "kus": None, "lltd": None}
    assert d["stints"][0]["temps"] is None and "thr" in d["traces"]["1"]
    assert report_data(logger)["balance"]["lltd"] is None      # no dampers: the split carries no information


def test_coarse_lateral_g_without_dampers_shows_no_grip_section():
    raw = read_csv(GOLDEN / "synthetic_gt3_5lap.csv.gz").data
    raw = raw.drop(columns=[c for c in raw.columns if c.startswith("SuspTravel")])
    raw["LateralAcc"] = raw["LateralAcc"].round()
    d = report_data(build_session(parse_csv_text(raw.to_csv(index=False))))
    assert d["gg"] is None and d["balance"]["kus"] is None


def test_page_is_self_contained_and_carries_its_data(logger):
    html = render_report(logger)
    assert html.startswith("<!doctype html>") and "<title>Synthetic Circuit · GT3</title>" in html
    assert "__DATA__" not in html and "__TITLE__" not in html
    assert page_data(html)["meta"]["title"] == "Synthetic Circuit · GT3"
    scripts = re.findall(r'<script[^>]*src="([^"]+)"', html)
    assert scripts == [], "the page must not depend on external scripts"
    fragment = render_report(logger, fragment=True)
    assert not fragment.startswith("<!doctype") and fragment.lstrip().startswith("<title>")


def test_title_can_be_set_and_is_escaped(logger):
    html = render_report(logger, title="FP1 <Monza> & co")
    assert "<title>FP1 &lt;Monza> &amp; co</title>" in html
    assert page_data(html)["meta"]["title"] == "FP1 <Monza> & co"


def test_write_report_and_command(tmp_path, monza, capsys):
    out = write_report(monza, tmp_path / "a" / "monza.html")
    assert out.exists() and out.stat().st_size > 50_000
    target = tmp_path / "cli.html"
    assert cli_main(["report", str(GOLDEN / "logger_layout_3lap_20hz.csv"), "-o", str(target)]) == 0
    assert target.exists() and "Report written to" in capsys.readouterr().out


def test_note_text_cannot_break_out_of_the_script(logger):
    logger.notes.append(("warning", "odd </script><b>text</b>"))
    try:
        html = render_report(logger)
        assert html.count("</script>") == 1
        assert page_data(html)["notes"][-1]["text"] == "odd </script><b>text</b>"
    finally:
        logger.notes.pop()
