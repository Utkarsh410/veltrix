"""
The session library (library.py, `veltrix library`). Phase 14's "done when":
a season of sessions is found, filtered and compared without opening folders.
"""

import shutil
from pathlib import Path

import numpy as np
import pytest

from veltrix_engine.cli import main
from veltrix_engine.errors import VersionError
from veltrix_engine.library import Library, _date_of
from veltrix_engine.synth import generate_session


def write_session(path: Path, venue: str, car: str, driver: str, day: str, seed: int, pace: float = 1.0,
                  laps: int = 5) -> Path:
    """A logger CSV with a header block; pace > 1 is a slower driver (every lap time × pace)."""
    frame = generate_session(laps, seed=seed)
    frame["Time"] = frame["Time"] * pace
    frame["Speed"] = frame["Speed"] / pace
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (f'"Format","Logger CSV File"\n"Venue","{venue}"\n"Vehicle","{car}"\n"Driver","{driver}"\n'
              f'"Log Date","{day}"\n"Log Time","10:00:00"\n\n')
    path.write_text(header + frame.to_csv(index=False, float_format="%.3f"))
    return path


@pytest.fixture(scope="module")
def season(tmp_path_factory):
    """Six sessions over two events and three folders, as a season's files would be kept."""
    root = tmp_path_factory.mktemp("season")
    write_session(root / "spa" / "fp1.csv", "Circuit de Spa-Francorchamps", "GT3", "Driver A", "02/05/2026", 1, 1.010)
    write_session(root / "spa" / "fp2.csv", "Circuit de Spa-Francorchamps", "GT3", "Driver A", "03/05/2026", 2, 1.004)
    write_session(root / "spa" / "race.csv", "Spa", "GT3", "Driver B", "04/05/2026", 3, 1.000)
    write_session(root / "monza" / "test_day.csv", "Monza", "GT3", "Driver A", "20/06/2026", 4, 1.000)
    write_session(root / "monza" / "test_day_pm.csv", "Monza", "GT4", "Driver A", "20/06/2026", 5, 1.050)
    write_session(root / "2026-07-10 sim" / "short.csv", "Monza", "GT3", "Driver B", "", 6, 1.000, laps=2)
    return root


@pytest.fixture
def lib(tmp_path, season):
    library = Library(tmp_path / "library.sqlite")
    library.scan([season])
    yield library
    library.close()


def test_a_season_is_indexed_once(tmp_path, season):
    with Library(tmp_path / "lib.sqlite") as lib:
        first = lib.scan([season])
        assert first["added"] == 6 and first["failed"] == 0
        again = lib.scan([season])
        assert again["unchanged"] == 6 and again["added"] == 0
        assert len(lib.find()) == 6


def test_what_the_index_holds(lib):
    row = lib.find(text="race.csv").iloc[0]
    assert row["track"] == "Spa" and row["car"] == "GT3" and row["driver"] == "Driver B"
    assert row["date"] == "2026-05-04 10:00:00" and row["date_source"] == "file header"
    assert row["laps"] == 5 and row["flying_laps"] == 4 and row["best_lap_s"] > 0
    assert row["ideal_lap_s"] <= row["best_lap_s"] and row["consistency_s"] >= 0
    assert lib.find(text="short").iloc[0]["date"] == "2026-07-10" and lib.find(text="short").iloc[0]["date_source"] == "file name"


def test_filters(lib):
    assert len(lib.find(track="monza")) == 3
    assert len(lib.find(track="monza", car="GT3")) == 2
    assert len(lib.find(driver="driver b")) == 2
    assert list(lib.find(since="2026-05-03", until="2026-06-20")["file"]) == ["fp2.csv", "race.csv", "test_day.csv",
                                                                               "test_day_pm.csv"]
    assert len(lib.find(text="test_day")) == 2
    assert list(lib.find(sort="-best_lap_s")["file"])[0] == "test_day_pm.csv"


def test_an_alias_joins_what_files_spell_differently(lib):
    assert set(lib.find(track="spa")["track"]) == {"Circuit de Spa-Francorchamps", "Spa"}
    lib.alias("track", "Circuit de Spa-Francorchamps", "Spa")
    assert set(lib.find(track="spa")["track"]) == {"Spa"}
    assert list(lib.aliases()["name"]) == ["Spa"]
    lib.alias("track", "Circuit de Spa-Francorchamps", None)
    assert len(lib.aliases()) == 0


def test_what_the_user_sets_wins_over_the_file(lib):
    sid = int(lib.find(text="short").iloc[0]["id"])
    lib.set(sid, "driver", "Driver C")
    lib.set(sid, "date", "2026-07-11 09:30")
    lib.set(sid, "simulator", "Assetto Corsa")
    row = lib.get(sid)
    assert (row["driver"], row["date"], row["date_source"], row["simulator"]) == \
        ("Driver C", "2026-07-11 09:30:00", "you", "Assetto Corsa")
    lib.set(sid, "driver", None)
    assert lib.get(sid)["driver"] == "Driver B"
    with pytest.raises(ValueError, match="cannot be set"):
        lib.set(sid, "best_lap_s", "1")


def test_tags_and_notes(lib):
    ids = list(lib.find(track="monza")["id"])
    lib.tag(ids, "baseline", "dry")
    lib.tag(ids[0], "dry", remove=True)
    assert len(lib.find(tag="baseline")) == 3 and len(lib.find(tag=["baseline", "dry"])) == 2
    lib.note(ids[0], "new tyres, wet start")
    assert list(lib.find(text="wet start")["id"]) == [ids[0]]


def test_events_group_sessions_and_are_suggested_from_track_and_date(lib):
    lib.alias("track", "Circuit de Spa-Francorchamps", "Spa")
    proposals = lib.suggest_events()
    assert [(p["name"], len(p["sessions"])) for p in proposals] == [
        ("Monza 2026-06-20", 2), ("Monza 2026-07-10", 1), ("Spa 2026-05-02 to 05-04", 3)]
    spa = proposals[2]
    lib.create_event("Spa 6h weekend", kind="race weekend", sessions=spa["sessions"])
    events = lib.events()
    assert list(events["event"]) == ["Spa 6h weekend"] and events.iloc[0]["sessions"] == 3
    assert (events.iloc[0]["first"], events.iloc[0]["last"], events.iloc[0]["kind"]) == ("2026-05-02", "2026-05-04", "race weekend")
    assert len(lib.find(event="6h")) == 3
    assert [p["name"] for p in lib.suggest_events()] == ["Monza 2026-06-20", "Monza 2026-07-10"]
    lib.leave_event(spa["sessions"][:1])
    assert len(lib.find(event="6h")) == 2
    lib.delete_event("Spa 6h weekend")
    assert lib.events().empty and len(lib.find()) == 6


def test_progress_over_a_season(lib):
    lib.alias("track", "Circuit de Spa-Francorchamps", "Spa")
    table = lib.progress(track="spa", car="GT3")
    assert list(table["file"]) == ["fp1.csv", "fp2.csv", "race.csv"]
    assert (table["change_s"].iloc[1:] < 0).all()             # each session faster than the one before
    assert table["best_so_far_s"].iloc[-1] == table["best_lap_s"].min()
    assert "warning" not in table.attrs
    assert "more than one track or car" in lib.progress(car="GT").attrs["warning"]


def test_best_per_track_and_car(lib):
    lib.alias("track", "Circuit de Spa-Francorchamps", "Spa")
    best = lib.best()
    assert [(r.track, r.car) for r in best.itertuples()] == [("Monza", "GT3"), ("Monza", "GT4"), ("Spa", "GT3")]


def test_sessions_are_compared_by_id_without_opening_folders(lib):
    lib.alias("track", "Circuit de Spa-Francorchamps", "Spa")
    spa = lib.find(track="spa", sort="date")
    result = lib.compare(int(spa.iloc[0]["id"]), int(spa.iloc[2]["id"]), change="new driver")
    assert result["comparable"] and result["stated_change"] == "new driver"     # the alias settles the names
    assert result["lap"]["call"] == "faster"                                     # 1 % quicker, 4 laps each
    assert result["library"]["a"]["file"] == "fp1.csv" and result["library"]["b"]["file"] == "race.csv"
    monza = lib.find(track="monza", sort="date")
    assert not lib.compare(int(monza.iloc[0]["id"]), int(monza.iloc[1]["id"]))["comparable"]   # GT3 against GT4


def test_a_moved_file_keeps_its_id_tags_and_event(tmp_path, season):
    root = tmp_path / "season"
    shutil.copytree(season, root)
    with Library(tmp_path / "lib.sqlite") as lib:
        lib.scan([root])
        sid = int(lib.find(text="race.csv").iloc[0]["id"])
        lib.tag(sid, "keeper")
        lib.create_event("Spa", sessions=[sid])
        (root / "archive").mkdir()
        (root / "spa" / "race.csv").rename(root / "archive" / "spa_race.csv")
        counts = lib.scan([root])
        assert counts["moved"] == 1 and counts["missing"] == 0
        row = lib.get(sid)
        assert row["file"] == "spa_race.csv" and row["tags"] == "keeper" and row["event"] == "Spa"
        (root / "archive" / "spa_race.csv").unlink()
        assert lib.scan([root])["missing"] == 1
        assert lib.get(sid)["status"] == "missing" and len(lib.find()) == 5
        with pytest.raises(FileNotFoundError, match="no longer there"):
            lib.load(sid)


def test_a_changed_file_is_read_again_and_a_copy_is_a_duplicate(tmp_path, season):
    root = tmp_path / "season"
    shutil.copytree(season, root)
    with Library(tmp_path / "lib.sqlite") as lib:
        lib.scan([root])
        shutil.copy(root / "spa" / "fp1.csv", root / "spa" / "fp1_copy.csv")
        write_session(root / "spa" / "fp2.csv", "Spa", "GT3", "Driver A", "03/05/2026", 2, 0.99)
        counts = lib.scan([root])
        assert counts["duplicate"] == 1 and counts["updated"] == 1 and counts["unchanged"] == 5
        assert lib.find(text="fp2.csv").iloc[0]["track"] == "Spa"
        dup = lib.find(text="fp1_copy", include=("duplicate",)).iloc[0]
        assert "same content as session" in dup["error"]


def test_a_file_that_fails_is_listed_with_its_reason(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "broken.ibt").write_bytes(b"\x02\x00\x00\x00" + b"\x00" * 300)
    with Library(tmp_path / "lib.sqlite") as lib:
        assert lib.scan([tmp_path / "data"])["failed"] == 1
        row = lib.find(include=("failed",)).iloc[0]
        assert row["status"] == "failed" and "broken.ibt" in row["error"] and row["track"] == ""
        assert lib.find().empty


def test_a_scan_in_parallel_gives_the_same_index(tmp_path, season):
    with Library(tmp_path / "a.sqlite") as one, Library(tmp_path / "b.sqlite") as two:
        one.scan([season])
        two.scan([season], jobs=3)
        cols = ["file", "track", "laps", "best_lap_s", "ideal_lap_s", "sha256"]
        a = one.find(sort="file")[cols].reset_index(drop=True)
        b = two.find(sort="file")[cols].reset_index(drop=True)
        assert a.equals(b)


def test_a_newer_library_is_refused_and_left_alone(tmp_path):
    path = tmp_path / "lib.sqlite"
    Library(path).close()
    import sqlite3
    db = sqlite3.connect(path)
    db.execute("UPDATE meta SET value = '99' WHERE key = 'library_version'")
    db.commit()
    db.close()
    with pytest.raises(VersionError, match="newer release"):
        Library(path)


@pytest.mark.parametrize("info, name, expected", [
    ({"Log Date": "02/04/2026", "Log Time": "15:05:29"}, "x-2026.04.02-15.05.29.ld", ("2026-04-02 15:05:29", "file header")),
    ({"Log Date": "04/02/2026"}, "x-2026.04.02.ld", ("2026-04-02", "file header")),    # the name settles the order
    ({"Log Date": "08-04-2026"}, "run.csv", ("2026-04-08", "file header")),             # day first otherwise
    ({"Log Date": "2026-10-02"}, "run.csv", ("2026-10-02", "file header")),
    ({"Log Date": "13/05/26"}, "run.csv", ("2026-05-13", "file header")),
    ({}, "monza 2026_06_20 fp1.csv", ("2026-06-20", "file name")),
])
def test_dates_from_the_header_then_the_name(info, name, expected):
    assert _date_of(info, name, 0.0) == expected


def test_a_date_in_the_folder_name_counts():
    assert _date_of({}, "fp1.csv", 0.0, "2026-07-10 Monza") == ("2026-07-10", "file name")


def test_dates_fall_back_on_the_file_time():
    stamp, source = _date_of({"Log Date": "not a date"}, "run.csv", 1_700_000_000.0)
    assert source == "file time" and stamp.startswith("2023-11-1")


# ── the command line ──────────────────────────────────────────────────────────

def test_the_command_line_finds_filters_groups_and_compares(tmp_path, season, capsys):
    lib = ["library", "--library", str(tmp_path / "lib.sqlite")]
    assert main([*lib, "scan", str(season), "--no-app-data"]) == 0
    assert "6 added" in capsys.readouterr().out
    assert main([*lib, "alias", "track", "Circuit de Spa-Francorchamps", "Spa"]) == 0
    assert main([*lib, "list", "--track", "spa", "--car", "GT3"]) == 0
    out = capsys.readouterr().out
    assert "fp1.csv" in out and "race.csv" in out and "3 session(s)" in out and "test_day" not in out
    assert main([*lib, "events", "--suggest", "--create"]) == 0
    assert "created: Spa 2026-05-02 to 05-04" in capsys.readouterr().out
    # ids follow the scan, in path order: 1 short, 2 test_day, 3 test_day_pm, 4 fp1, 5 fp2, 6 race
    assert main([*lib, "tag", "1", "2", "--add", "baseline"]) == 0
    assert main([*lib, "list", "--tag", "baseline", "--sort", "best_lap_s", "--desc"]) == 0
    assert "2 session(s)" in capsys.readouterr().out
    assert main([*lib, "progress", "--track", "spa", "--car", "GT3"]) == 0
    assert "best_so_far_s" in capsys.readouterr().out
    assert main([*lib, "compare", "4", "6", "--change", "new driver"]) == 0
    assert "Lap: faster" in capsys.readouterr().out
    assert main([*lib, "show", "6"]) == 0 and "Driver B" in capsys.readouterr().out
    assert main([*lib, "list", "--csv", str(tmp_path / "season.csv")]) == 0
    assert (tmp_path / "season.csv").read_text().count("\n") == 7
    assert main([*lib, "forget", "6"]) == 0 and (season / "spa" / "race.csv").exists()
    assert main([*lib, "list"]) == 0 and "5 session(s)" in capsys.readouterr().out


def test_the_command_line_names_an_unknown_session(tmp_path, capsys):
    assert main(["library", "--library", str(tmp_path / "lib.sqlite"), "show", "42"]) == 1
    assert "session 42 is not in the library" in capsys.readouterr().err
