"""Project store: sessions round-trip, sources stay immutable, files are unique."""

import hashlib
import stat

import numpy as np
import pandas as pd
import pytest

from conftest import GOLDEN
from veltrix_engine import ProjectStore

SOURCE = GOLDEN / "logger_layout_3lap_20hz.csv"


@pytest.fixture()
def store(tmp_path):
    s = ProjectStore(tmp_path / "project")
    yield s
    s.close()


def test_session_round_trip(store, logger):
    sid = store.add_session(logger, source_path=SOURCE, event="Test Day", track="Synthetic Circuit",
                            driver="Test Driver", vehicle_name="GT3", day="2026-10-06",
                            setup={"name": "baseline", "arb_front": 700})
    listing = store.sessions()
    assert len(listing) == 1
    row = listing.iloc[0]
    assert (row["event"], row["track"], row["driver"], row["laps"]) == \
           ("Test Day", "Synthetic Circuit", "Test Driver", 3)
    assert row["best_lap_s"] == pytest.approx(logger.laps.loc[logger.laps["status"] == "flying",
                                                              "lap_time_s"].min())

    stored = store.channels(sid)
    pd.testing.assert_frame_equal(stored, logger.frame(), check_dtype=False)
    laps = store.laps(sid)
    assert list(laps["lap"]) == list(logger.laps["lap"])
    assert np.allclose(laps["lap_time_s"], logger.laps["lap_time_s"])


def test_reading_selected_channels_only(store, logger):
    sid = store.add_session(logger, source_path=SOURCE)
    part = store.channels(sid, columns=["time", "speed", "oversteer_metric"])
    assert list(part.columns) == ["time", "speed", "oversteer_metric"]


def test_source_file_is_kept_byte_for_byte_and_read_only(store, logger):
    sid = store.add_session(logger, source_path=SOURCE)
    kept = store.source_file(sid)
    assert hashlib.sha256(kept.read_bytes()).hexdigest() == logger.raw.sha256
    assert not kept.stat().st_mode & stat.S_IWUSR


def test_same_file_cannot_be_imported_twice(store, logger):
    store.add_session(logger, source_path=SOURCE)
    with pytest.raises(ValueError, match="already in the project"):
        store.add_session(logger, source_path=SOURCE)


def test_reload_rebuilds_the_same_session_from_the_source(store, logger):
    sid = store.import_file(SOURCE, vehicle_name="GT3")
    again = store.reload_session(sid)
    pd.testing.assert_frame_equal(again.frame(), logger.frame())


def test_delete_removes_rows_and_files(store, logger):
    sid = store.add_session(logger, source_path=SOURCE)
    store.delete_session(sid)
    assert store.sessions().empty
    assert not (store.path / "sessions" / str(sid)).exists()
    assert store.db.execute("SELECT COUNT(*) FROM lap").fetchone()[0] == 0
