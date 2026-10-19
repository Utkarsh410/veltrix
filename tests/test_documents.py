"""Phase 13: engineering documents — static, repeatable, printable."""

import json
import os
import re

import pytest

from conftest import GOLDEN
from veltrix_engine.cli import main as cli_main
from veltrix_engine.report import documents

SYNTH = str(GOLDEN / "synthetic_gt3_5lap.csv.gz")
LOGGER = str(GOLDEN / "logger_layout_3lap_20hz.csv")


@pytest.mark.parametrize("template, files", [("debrief", [SYNTH]), ("driver-card", [SYNTH]), ("comparison", [SYNTH, SYNTH])])
def test_a_document_built_twice_from_one_spec_is_the_same_page(template, files, tmp_path):
    spec = documents.make_spec(template, files, app_data=False)
    one, two = documents.build(spec), documents.build(spec)
    assert one == two, "no clock time or anything else that changes between builds"
    assert one.startswith("<!doctype html>") and "<script" not in one and "@page{size:A4" in one
    assert re.search(r"built from synthetic_gt3_5lap\.csv\.gz \([0-9a-f]{12}\)", one), "what it was built from"
    path = documents.save_spec(spec, tmp_path / "spec.json")
    assert documents.build(documents.load_spec(path)) == one


def test_the_debrief_carries_every_section_and_the_chain_of_each_finding():
    page = documents.debrief_html(__import__("veltrix_engine").load_session(SYNTH))
    for title in ("Lap potential", "Driver performance", "Braking", "Cornering", "Traction", "Tyres", "Vehicle balance",
                  "Incidents", "Setup hypotheses"):
        assert f"<h2>{title}</h2>" in page
    assert page.count("<dt>Evidence</dt>") == page.count("<dt>Calculation</dt>") == page.count("<dt>Confidence</dt>") > 10
    assert "Estimate — built from several laps, not driven" in page and "Measured" in page


def test_a_spec_must_name_a_template_and_the_right_number_of_files():
    with pytest.raises(ValueError, match="one of"):
        documents.make_spec("brochure", [SYNTH])
    with pytest.raises(ValueError, match="2 files"):
        documents.make_spec("comparison", [SYNTH])


def test_the_document_command_writes_html_a_spec_and_a_pdf(tmp_path, capsys):
    html, spec, pdf = tmp_path / "d.html", tmp_path / "d.json", tmp_path / "d.pdf"
    args = ["document", "comparison", LOGGER, LOGGER, "--change", "rear wing +1", "-o", str(html),
            "--save-spec", str(spec), "--no-app-data"]
    from veltrix_engine.report import pdf as pdf_mod
    if pdf_mod.available():
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        args += ["--pdf", str(pdf)]
    assert cli_main(args) == 0
    page = html.read_text()
    assert "Stated: rear wing +1" in page and "Corner by corner" in page
    assert json.loads(spec.read_text())["template"] == "comparison"
    if pdf_mod.available():
        data = pdf.read_bytes()
        assert data.startswith(b"%PDF") and len(re.findall(rb"/Type\s*/Page[^s]", data)) >= 1
    again = tmp_path / "again.html"
    assert cli_main(["document", "--spec", str(spec), "-o", str(again)]) == 0
    assert again.read_text() == page, "the saved spec builds the same document"
