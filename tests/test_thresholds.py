"""Thresholds are configuration: listed, overridable for a run or a project, and put back afterwards."""

import json

import pytest

from conftest import GOLDEN
from veltrix_engine import ProjectStore, corners, load_session, thresholds
from veltrix_engine.analysis import advice, engineering, handling
from veltrix_engine.cli import main as cli_main

SYNTH = GOLDEN / "synthetic_gt3_5lap.csv.gz"


def test_every_threshold_is_listed_with_its_value():
    snap = thresholds.snapshot()
    assert snap["corners.SLOW_KMH"] == 110.0 and snap["handling.SIGMA_LEVEL"] == 3.5 and snap["advice.BRAKE_M"] == 5.0
    assert snap["handling.SEVERITY"] == [1.6, 2.6] and snap["engineering.LAPS_HIGH"] == 8
    assert len(snap) > 60 and all("." in k for k in snap)
    json.dumps(snap)


def test_an_override_changes_the_analysis_and_is_undone_afterwards(synthetic):
    kinds = lambda: corners.find_corners(synthetic)["kind"].value_counts().to_dict()
    before = kinds()
    assert before == {"medium": 4, "slow": 3, "fast": 2}
    with thresholds.use({"corners.SLOW_KMH": 60, "corners.FAST_KMH": 150.0}) as applied:
        assert applied == {"corners.SLOW_KMH": 60.0, "corners.FAST_KMH": 150.0}
        assert kinds() == {"medium": 6, "fast": 3}, "no corner is under 60 km/h; three are over 150"
    assert kinds() == before and corners.SLOW_KMH == 110.0

    with pytest.raises(RuntimeError):
        with thresholds.use({"handling.FLOOR_DEG": 2.0}):
            assert handling.FLOOR_DEG == 2.0
            raise RuntimeError("something went wrong inside")
    assert handling.FLOOR_DEG == 6.0, "restored even when the analysis fails"


def test_a_threshold_shared_by_two_modules_moves_in_both():
    assert engineering.BRAKE_M == advice.BRAKE_M == 5.0
    with thresholds.use({"advice.BRAKE_M": 12}):
        assert (advice.BRAKE_M, engineering.BRAKE_M) == (12.0, 12.0)
    assert (advice.BRAKE_M, engineering.BRAKE_M) == (5.0, 5.0)


def test_wrong_names_and_values_are_refused():
    with pytest.raises(ValueError, match="'corners.SLOW' is not a threshold"):
        with thresholds.use({"corners.SLOW": 1}):
            pass
    with pytest.raises(ValueError, match="Did you mean corners.SLOW_KMH"):
        thresholds.save("/dev/null", {"SLOW_KMH": 90})
    with pytest.raises(ValueError, match="takes a number"):
        with thresholds.use({"corners.SLOW_KMH": "fast"}):
            pass
    with pytest.raises(ValueError, match="takes 2 numbers"):
        with thresholds.use({"handling.SEVERITY": 2.0}):
            pass
    assert corners.SLOW_KMH == 110.0


def test_a_project_keeps_its_thresholds_and_uses_them(tmp_path, capsys):
    store = ProjectStore(tmp_path / "project")
    try:
        assert store.thresholds() == {}
        store.save_thresholds({"engineering.LAPS_MEDIUM": 3, "engineering.LAPS_HIGH": 4})
        sid = store.import_file(SYNTH)
        report = store.engineering(sid)
        levels = {f["confidence"]["level"] for sec in report["sections"] for f in sec["findings"]
                  if f["confidence"]["basis"].startswith("4 flying laps")}
        assert "high" in levels or "medium" in levels, "four laps count for more under this project's settings"
        assert store.thresholds() == {"engineering.LAPS_MEDIUM": 3, "engineering.LAPS_HIGH": 4}
    finally:
        store.close()
    assert engineering.LAPS_HIGH == 8, "the project's settings do not leak out of it"

    path = thresholds.save(tmp_path / "t.json", {"corners.SLOW_KMH": 60})
    assert cli_main(["thresholds"]) == 0 and "corners.SLOW_KMH" in capsys.readouterr().out
    assert cli_main(["engineer", str(SYNTH), "--thresholds", str(path)]) == 0
    line = next(x for x in capsys.readouterr().out.splitlines() if "lateral g by corner type" in x)
    assert "medium" in line and "slow" not in line and corners.SLOW_KMH == 110.0, "no corner counts as slow at 60 km/h"
