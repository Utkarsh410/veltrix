"""
Files saved by an earlier release open in this one, unchanged; files from a
newer release are refused with a reason and never overwritten.

tests/fixtures/saved_release_1/ holds one file of every kind as release 1 wrote
it (tools/freeze_saved_files.py). Never regenerate an existing release's folder:
add a new one when a format changes, and keep the old one passing.
"""

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from veltrix_engine import ProjectStore, events, thresholds
from veltrix_engine.errors import VersionError
from veltrix_engine.mapping_profiles import Profile
from veltrix_engine.maths import MathSet
from veltrix_engine.model_store import read_manifest
from veltrix_engine.user_data import AnnotationStore
from veltrix_engine.versions import upgrade

RELEASE_1 = Path(__file__).parent / "fixtures" / "saved_release_1"
SHA = (RELEASE_1 / "SOURCE").read_text().split()[1]


@pytest.fixture
def saved(tmp_path):
    """A copy of release 1's files, so a test that writes cannot change the fixture."""
    return Path(shutil.copytree(RELEASE_1, tmp_path / "saved"))


def test_every_kind_of_file_from_release_1_opens_unchanged(saved):
    ms = MathSet.load(saved / "maths.json")
    assert ms.parameters["k"] == 2.0 and ms.channels["SpeedTimesK"].formula == "speed * $k"
    rules = events.RuleSet.load(saved / "event_rules.json")
    assert rules.parameters["lock_on"] == 14.0 and any(r.id == "lock_up" for r in rules.rules)
    assert Profile.load(saved / "profile.json").name == "Logger layout"
    assert thresholds.load(saved / "thresholds.json") == {"engineering.LAPS_HIGH": 6}
    notes = AnnotationStore(saved / "annotations").load(SHA)
    assert [(n.text, n.category) for n in notes] == [("kerb on exit", "driver")]
    reviews = events.ReviewStore(saved / "reviews").load(SHA)
    assert reviews[0]["verdict"] == "rejected" and reviews[0]["original"]["status"] == "candidate"
    pytest.importorskip("PySide6")
    from veltrix_app.video_sync import VideoStore
    from veltrix_app.workspace import Workspace
    links = VideoStore(saved / "videos").load(SHA)
    assert links[0].path == "onboard.mp4" and links[0].offset == 12.5
    assert Workspace.load(saved / "workspace.json").name == "saved workspace"


def test_a_release_1_project_opens_and_analyses(saved):
    store = ProjectStore(saved / "project")
    try:
        assert store.schema_found == 3
        sid = int(store.sessions()["id"].iloc[0])
        model = store.model(sid)
        assert len(model.laps) >= 3 and read_manifest(saved / "project" / "sessions" / str(sid) / "model")
        report = store.engineering(sid)
        assert [s["id"] for s in report["sections"]][0] == "lap_potential"
    finally:
        store.close()


def test_an_older_project_schema_is_brought_up_to_date(saved):
    db = sqlite3.connect(saved / "project" / "project.sqlite")
    db.execute("ALTER TABLE session DROP COLUMN format")                 # as schema 1 had it
    db.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    db.commit()
    db.close()
    store = ProjectStore(saved / "project")
    try:
        assert store.schema_found == 1
        columns = {r["name"] for r in store.db.execute("PRAGMA table_info(session)")}
        assert "format" in columns
        assert store.db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()["value"] == "3"
    finally:
        store.close()


def _bump(path: Path, key: str = "version", to: int = 99) -> str:
    data = json.loads(path.read_text())
    data[key] = to
    path.write_text(json.dumps(data))
    return path.read_text()


@pytest.mark.parametrize("name, read", [
    ("maths.json", lambda p: MathSet.load(p)),
    ("event_rules.json", lambda p: events.RuleSet.load(p)),
    ("profile.json", lambda p: Profile.load(p)),
    ("thresholds.json", lambda p: thresholds.load(p)),
])
def test_a_file_from_a_newer_release_is_refused_with_a_reason(saved, name, read):
    _bump(saved / name)
    with pytest.raises(VersionError, match="newer release"):
        read(saved / name)


def test_stores_neither_read_nor_overwrite_a_newer_file(saved):
    notes = saved / "annotations" / f"{SHA}.json"
    reviews = saved / "reviews" / f"{SHA}.json"
    kept_notes, kept_reviews = _bump(notes), _bump(reviews)
    with pytest.raises(VersionError):
        AnnotationStore(saved / "annotations").load(SHA)
    with pytest.raises(VersionError):
        AnnotationStore(saved / "annotations").save(SHA, [])
    with pytest.raises(VersionError):
        events.ReviewStore(saved / "reviews").load(SHA)
    with pytest.raises(VersionError):
        events.ReviewStore(saved / "reviews").save(SHA, [])
    assert notes.read_text() == kept_notes and reviews.read_text() == kept_reviews, "a newer file is left as it was"
    with pytest.raises(VersionError):
        thresholds.save(_bump(saved / "thresholds.json") and saved / "thresholds.json", {})


def test_a_newer_project_is_refused_and_left_alone(saved):
    db = sqlite3.connect(saved / "project" / "project.sqlite")
    db.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    db.commit()
    db.close()
    with pytest.raises(VersionError, match="newer release"):
        ProjectStore(saved / "project")
    db = sqlite3.connect(saved / "project" / "project.sqlite")
    assert db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0] == "99"
    db.close()


def test_upgrade_walks_an_old_file_forward_one_step_at_a_time():
    steps = {1: lambda d: {**d, "b": d["a"] * 2}, 2: lambda d: {**d, "c": d["b"] + 1}}
    assert upgrade("thing", {"version": 1, "a": 3}, 3, steps) == {"version": 3, "a": 3, "b": 6, "c": 7}
    assert upgrade("thing", {"a": 3}, 1) == {"a": 3}, "no version: the first format"
    with pytest.raises(VersionError, match="no way to bring it"):
        upgrade("thing", {"version": 1}, 3, {1: lambda d: d})


# ── errors, the log and crash reports ──────────────────────────────────────────

def test_an_unreadable_file_is_one_named_error_and_the_details_go_to_the_log(tmp_path, caplog):
    import logging
    from veltrix_engine.errors import FileFormatError, MappingError, VeltrixError
    from veltrix_engine import load_session
    from veltrix_engine.importers import read_file
    damaged = tmp_path / "broken.ibt"
    damaged.write_bytes(b"\x02\x00\x00\x00" + b"\xff" * 300)          # an iRacing header that leads nowhere
    with caplog.at_level(logging.INFO, logger="veltrix"):
        with pytest.raises(FileFormatError) as caught:
            read_file(damaged)
    assert str(caught.value).startswith("broken.ibt") and isinstance(caught.value, (ValueError, VeltrixError))
    assert any("broken.ibt" in r.getMessage() for r in caplog.records)
    empty = tmp_path / "empty.csv"
    empty.write_bytes(b"")
    with pytest.raises(FileFormatError, match="empty"):
        read_file(empty)
    with pytest.raises(FileNotFoundError):
        read_file(tmp_path / "missing.csv")
    nothing = tmp_path / "nothing.csv"
    nothing.write_text("a,b\n" + "\n".join(f"{i},{i}" for i in range(50)))
    with pytest.raises(MappingError, match="required channel"):
        load_session(nothing)


def test_the_log_goes_to_the_data_folder(tmp_path):
    import logging
    from veltrix_engine import logs
    path = logs.setup(tmp_path)
    logging.getLogger("veltrix.test").info("a line for the log")
    for h in logging.getLogger("veltrix").handlers:
        h.flush()
    assert path == tmp_path / "logs" / "veltrix.log" and "a line for the log" in logs.tail(tmp_path)
    assert logs.setup(tmp_path) == path and sum(getattr(h, "_veltrix_file_handler", False)
                                                for h in logging.getLogger("veltrix").handlers) == 1


def test_an_unexpected_error_in_the_app_writes_a_crash_report_and_sends_nothing(tmp_path):
    import sys
    pytest.importorskip("PySide6")
    from veltrix_app import crash
    before = sys.excepthook
    try:
        hook = crash.install(folder=tmp_path, interactive=False)
        try:
            raise RuntimeError("something nobody expected")
        except RuntimeError:
            hook(*sys.exc_info())
        report = hook.last_report
        text = report.read_text()
        assert report.parent == tmp_path / "crash_reports"
        assert "RuntimeError: something nobody expected" in text and "Veltrix Data" in text and "Python" in text
    finally:
        sys.excepthook = before
