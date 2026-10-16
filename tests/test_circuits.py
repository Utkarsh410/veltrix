"""Circuit files: official corner numbers and names, and the checks that guard them."""

import json

import numpy as np
import pytest

from conftest import GOLDEN

from veltrix_engine import circuits, corners as corner_engine
from veltrix_engine.cli import main
from veltrix_engine.report import report_data

@pytest.fixture(scope="module")
def session(synthetic):
    return synthetic


def _draft(session):
    found = corner_engine.find_corners(session, track=None)
    return found, circuits.draft_from_session(session, found, "Test circuit")


def test_draft_round_trips_through_a_file(session, tmp_path):
    found, draft = _draft(session)
    loaded = circuits.load(circuits.save(draft, tmp_path / "t.json"))
    assert loaded["name"] == "Test circuit" and len(loaded["corners"]) == len(found)
    assert [c["direction"] for c in loaded["corners"]] == list(found["direction"])


def test_circuit_file_sets_numbers_and_names(session):
    found, draft = _draft(session)
    n = len(draft["corners"])
    for i, c in enumerate(draft["corners"]):                 # the circuit's Turn 1 is not the first corner driven
        c["number"], c["name"] = (i + 3) % n + 1, f"Corner {i}"
    named = corner_engine.find_corners(session, track=draft)
    assert named.attrs["track"] == "Test circuit" and "circuit file" in named.attrs["source"]
    assert list(named["corner"]) == [f"T{(i + 3) % n + 1}" for i in range(n)]
    assert list(named["name"]) == [f"Corner {i}" for i in range(n)]
    assert np.allclose(named["start_m"], found["start_m"], atol=2.1)
    assert named["zone_start_m"].iloc[0] == 0 and named["zone_end_m"].iloc[-1] == session.lap_length


def test_one_corner_can_be_counted_as_two(session):
    found, draft = _draft(session)
    long = max(range(len(found)), key=lambda i: found["end_m"].iloc[i] - found["start_m"].iloc[i])
    c = draft["corners"][long]
    mid = (c["start_m"] + c["end_m"]) // 2
    draft["corners"][long:long + 1] = [{**c, "end_m": mid}, {**c, "number": 99, "start_m": mid + 2}]
    named = corner_engine.find_corners(session, track=draft)
    assert len(named) == len(found) + 1 and "T99" in list(named["corner"])
    metrics = corner_engine.corner_metrics(session, named)
    assert set(metrics["corner"]) == set(named["corner"])


def test_wrong_length_or_layout_is_rejected(session):
    found, draft = _draft(session)
    longer = {**draft, "lap_length_m": draft["lap_length_m"] * 1.2}
    got = corner_engine.find_corners(session, track=longer)
    assert got.attrs["track"] is None and "lap length" in got.attrs["track_note"] and len(got) == len(found)
    mirrored = {**draft, "corners": [{**c, "direction": "L" if c["direction"] == "R" else "R"} for c in draft["corners"]]}
    got = corner_engine.find_corners(session, track=mirrored)
    assert got.attrs["track"] is None and "turns the stated way" in got.attrs["track_note"]


def test_bad_files_are_refused(tmp_path):
    bad = {"name": "x", "lap_length_m": 1000, "corners": [{"number": 1, "direction": "L", "start_m": 500, "end_m": 600},
                                                          {"number": 2, "direction": "R", "start_m": 100, "end_m": 200}]}
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="order"):
        circuits.load(p)
    assert circuits.available(tmp_path) == []


def test_library_files_are_valid_and_matched_by_venue():
    names = {t["name"] for t in circuits.available()}
    assert {"Silverstone GP", "Monza GP"} <= names
    silverstone = circuits.for_venue("Silverstone")[0]
    assert [c["number"] for c in silverstone["corners"]] == list(range(1, 19))      # Turn 1 is the first corner after the line
    assert len(circuits.for_venue("monza")[0]["corners"]) == 11
    assert circuits.for_venue("") == [] and circuits.for_venue("spa") == []


def test_a_different_layout_at_the_same_venue_falls_back_to_detection():
    from veltrix_engine import load_session
    s = load_session(GOLDEN / "monza_ac_gt3.csv.gz")       # the 10 km full course, not the GP circuit
    found = corner_engine.find_corners(s, track=circuits.for_venue("monza")[0])
    assert found.attrs["track"] is None and "Monza GP" in found.attrs["track_note"]


def test_report_and_cli_carry_names(session, tmp_path, capsys):
    data = report_data(session)
    assert all("name" in c for c in data["corners"]["list"])
    out = tmp_path / "c.json"
    assert main(["circuit", str(GOLDEN / "synthetic_gt3_5lap.csv.gz"), "-o", str(out), "--name", "Synthetic"]) == 0
    assert circuits.load(out)["name"] == "Synthetic"
    assert main(["circuit", str(GOLDEN / "synthetic_gt3_5lap.csv.gz"), "-o", str(out)]) == 1     # will not overwrite
