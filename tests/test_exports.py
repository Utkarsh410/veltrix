"""Phase 8 / 15: a session exported for other tools, with its units, imports back the same."""

import numpy as np
import pytest

from conftest import GOLDEN
from veltrix_engine import load_session
from veltrix_engine.cli import main as cli_main
from veltrix_engine.exports import export_session


def _same_laps(a, b, tol=0.06):
    fa, fb = a.laps[a.laps["status"] == "flying"], b.laps[b.laps["status"] == "flying"]
    assert list(fa["lap"]) == list(fb["lap"])
    assert np.allclose(fa["lap_time_s"], fb["lap_time_s"], atol=tol)


def test_a_csv_export_names_its_units_and_imports_back_the_same(synthetic, tmp_path):
    out = export_session(synthetic, tmp_path / "s.csv")
    head = out.read_text().splitlines()[:2]
    names, units = head[0].split(","), head[1].split(",")
    assert names[:3] == ["time", "lap", "lap_distance"] and units[names.index("speed")] == "km/h"
    again = load_session(out)
    _same_laps(synthetic, again)
    assert again.units["speed"] == "km/h"


def test_a_parquet_export_keeps_units_in_its_columns(synthetic, tmp_path):
    import pyarrow.parquet as pq
    out = export_session(synthetic, tmp_path / "s.parquet", ["speed", "throttle"])
    table = pq.read_table(out)
    assert table.column_names == ["time", "lap", "lap_distance", "speed", "throttle"]
    assert table.schema.field("speed").metadata[b"unit"] == b"km/h"
    assert table.schema.metadata[b"veltrix_sha256"].decode() == synthetic.raw.sha256


def test_an_mf4_export_imports_back_the_same(synthetic, tmp_path):
    pytest.importorskip("asammdf")
    out = export_session(synthetic, tmp_path / "s.mf4", ["speed", "throttle", "brake", "steering", "acc_lat"])
    again = load_session(out)
    _same_laps(synthetic, again)


def test_exports_refuse_what_they_cannot_do(synthetic, tmp_path):
    with pytest.raises(ValueError, match="Export to"):
        export_session(synthetic, tmp_path / "s.xlsx")
    with pytest.raises(ValueError, match="Not in this session"):
        export_session(synthetic, tmp_path / "s.csv", ["warp_drive"])


def test_the_export_command(tmp_path, capsys):
    out = tmp_path / "x.csv"
    assert cli_main(["export", str(GOLDEN / "logger_layout_3lap_20hz.csv"), str(out), "--no-app-data"]) == 0
    assert out.exists() and "Written" in capsys.readouterr().out
