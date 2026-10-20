"""Batch mode: a folder of files in one run (batch.py, `veltrix batch`)."""

import csv
import shutil
from pathlib import Path

import pytest

from veltrix_engine.batch import find_files, run_batch, write_index
from veltrix_engine.cli import main

GOLDEN = Path(__file__).resolve().parents[1] / "datasets" / "golden"


@pytest.fixture
def folder(tmp_path):
    """Two good files, one in a sub-folder, a copy of one, a damaged one, and files that are not sessions."""
    root = tmp_path / "season"
    (root / "round2").mkdir(parents=True)
    shutil.copy(GOLDEN / "logger_layout_3lap_20hz.csv", root / "practice.csv")
    shutil.copy(GOLDEN / "synthetic_gt3_5lap.csv.gz", root / "round2" / "race.csv.gz")
    shutil.copy(GOLDEN / "logger_layout_3lap_20hz.csv", root / "round2" / "practice_again.csv")
    (root / "round2" / "broken.ibt").write_bytes(b"\x02\x00\x00\x00" + b"\x00" * 200)
    (root / "notes.md").write_text("not telemetry")
    (root / "car.dbc").write_text('VERSION ""\nBU_: ECU\n')
    (root / ".hidden.csv").write_text("Time,Speed\n0,1\n")
    return root


def test_find_files_takes_telemetry_and_leaves_the_rest(folder):
    assert [p.relative_to(folder).as_posix() for p in find_files(folder)] == [
        "practice.csv", "round2/broken.ibt", "round2/practice_again.csv", "round2/race.csv.gz"]
    assert [p.name for p in find_files(folder, recursive=False)] == ["practice.csv"]
    with pytest.raises(ValueError, match="not a folder"):
        find_files(folder / "practice.csv")


def test_a_batch_reads_every_file_and_a_bad_one_is_only_its_own_row(folder, tmp_path):
    seen = []
    rows = run_batch(find_files(folder), root=folder, out_dir=tmp_path / "out", outputs=("report", "engineer", ".csv"),
                     progress=lambda done, total, row: seen.append((done, total, row.file)))
    by = {r.file: r for r in rows}
    assert [r.file for r in rows] == ["practice.csv", "round2/broken.ibt", "round2/practice_again.csv", "round2/race.csv.gz"]
    assert by["practice.csv"].status == "ok" and by["practice.csv"].laps == 3 and by["practice.csv"].best_lap_s
    assert by["round2/race.csv.gz"].status == "ok" and by["round2/race.csv.gz"].laps == 5
    assert by["round2/broken.ibt"].status == "failed" and "broken.ibt" in by["round2/broken.ibt"].error
    assert by["round2/practice_again.csv"].status == "duplicate" and "practice.csv" in by["round2/practice_again.csv"].error
    assert sorted(d for d, _, _ in seen) == [1, 2, 3, 4] and all(t == 4 for _, t, _ in seen)
    out = tmp_path / "out"
    assert (out / "practice_report.html").exists() and (out / "practice_engineer.md").exists()
    assert (out / "round2" / "race_report.html").exists() and (out / "round2" / "race.csv").exists()
    assert len(by["practice.csv"].outputs) == 3 and not by["round2/broken.ibt"].outputs


def test_the_index_has_a_row_per_file(folder, tmp_path):
    rows = run_batch(find_files(folder), root=folder)
    csv_path, md_path = write_index(rows, tmp_path / "out")
    table = list(csv.DictReader(open(csv_path, encoding="utf-8")))
    assert [r["status"] for r in table] == ["ok", "failed", "duplicate", "ok"]
    assert table[0]["venue"] == "Synthetic Circuit" and table[0]["laps"] == "3" and len(table[0]["sha256"]) == 64
    assert "2 read, 1 failed, 1 duplicate(s)." in md_path.read_text()


def test_files_in_parallel_give_the_same_rows(folder):
    one = run_batch(find_files(folder), root=folder)
    two = run_batch(find_files(folder), root=folder, jobs=2)
    strip = lambda rows: [(r.file, r.status, r.laps, r.best_lap_s, r.sha256) for r in rows]
    assert strip(one) == strip(two)


def test_an_unknown_output_is_refused(folder):
    with pytest.raises(ValueError, match="unknown output"):
        run_batch(find_files(folder), outputs=("pdf",))


def test_the_command_reports_and_fails_when_a_file_fails(folder, tmp_path, capsys):
    code = main(["batch", str(folder), "-o", str(tmp_path / "out"), "--report", "--no-app-data"])
    printed = capsys.readouterr().out
    assert code == 1 and "4 file(s)" in printed and "FAILED" in printed and "3 read, 1 failed" in printed
    assert (tmp_path / "out" / "index.md").exists() and (tmp_path / "out" / "practice_report.html").exists()
    (folder / "round2" / "broken.ibt").unlink()
    assert main(["batch", str(folder), "-o", str(tmp_path / "out2"), "--no-app-data"]) == 0


def test_the_command_says_so_when_a_folder_has_no_telemetry(tmp_path, capsys):
    assert main(["batch", str(tmp_path)]) == 1
    assert "No telemetry files" in capsys.readouterr().out
