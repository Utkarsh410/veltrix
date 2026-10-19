"""
Project store: SQLite for metadata, Parquet for telemetry.

Layout of a project folder:

  project.sqlite                     events, sessions, laps, drivers, vehicles, tracks, setups
  maths.json                         the project's maths channels: formulas and their parameters
  event_rules.json                   the project's event rules and thresholds (defaults if absent)
  thresholds.json                    the project's overrides of the analysis thresholds (see thresholds.py)
  sessions/<id>/source/<file>        the imported file, byte-for-byte, never modified
                                     (with its .ldx lap-marker file, when it has one)
  sessions/<id>/dbc/<file>           the DBC files a CAN log was decoded with
  sessions/<id>/channels.parquet     normalised channels
  sessions/<id>/derived.parquet      derived channels
  sessions/<id>/engineering.json     the engineering report, written when the session is added
  sessions/<id>/model/               the canonical model: manifest.json (metadata, provenance) and the
                                     normalised channels on their own timestamps (see model_store.py)

Imported files are immutable and identified by their SHA-256, so every result
can be traced to exactly the bytes it came from. Derived channels are stored
for speed but can always be recomputed from the source.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .analysis import engineering
from . import events as event_engine
from .model import SessionModel, events_from_incidents
from .model_store import MANIFEST, load_model, save_model
from .session import Session, load_session
from .vehicle import VehicleParams

SCHEMA_VERSION = 3          # 2: session.format   3: sessions/<id>/model/ (files only; no table change)

_DDL = """
CREATE TABLE IF NOT EXISTS meta      (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS track     (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, length_m REAL);
CREATE TABLE IF NOT EXISTS driver    (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS vehicle   (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, params TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS event     (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL,
                                      track_id INTEGER REFERENCES track(id));
CREATE TABLE IF NOT EXISTS setup     (id INTEGER PRIMARY KEY, name TEXT NOT NULL, params TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS session   (
    id INTEGER PRIMARY KEY,
    event_id   INTEGER REFERENCES event(id),
    driver_id  INTEGER REFERENCES driver(id),
    vehicle_id INTEGER REFERENCES vehicle(id),
    setup_id   INTEGER REFERENCES setup(id),
    name TEXT NOT NULL, day TEXT,
    source_file TEXT NOT NULL, source_sha256 TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    rate_hz REAL NOT NULL, lap_length_m REAL, samples INTEGER NOT NULL,
    lap_source TEXT, crossing_method TEXT,
    mapping TEXT NOT NULL, units TEXT NOT NULL, notes TEXT NOT NULL, file_info TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lap       (
    session_id INTEGER NOT NULL REFERENCES session(id) ON DELETE CASCADE,
    lap TEXT NOT NULL, t_start REAL, t_end REAL, lap_time_s REAL, dist_m REAL, d_scale REAL,
    start_crossed INTEGER, end_crossed INTEGER, status TEXT, stint INTEGER, stint_lap INTEGER,
    PRIMARY KEY (session_id, lap));
"""


def _events(session: Session, rules) -> list:
    """What the detectors find, for storing with the session. A detector that
    cannot run on a file leaves no events; it never costs the import."""
    found = []
    try:
        found += event_engine.detect(session.model, rules, fit=True)[0]
    except Exception:
        pass
    try:
        from .analysis import handling
        result = handling.analyse(session)
        m = result["model"]
        found += events_from_incidents(result["events"], session.laps, "handling 1",
                                       session.model.get("lap_distance") if session.model.has("lap_distance") else None,
                                       thresholds={"threshold_deg": round(float(m["threshold_deg"]), 1),
                                                   "robust_sigmas": handling.SIGMA_LEVEL,
                                                   "min_duration_s": handling.MIN_DURATION_S,
                                                   "opposite_lock_deg": handling.COUNTER_STEER_DEG})
    except Exception:
        pass
    recorded = event_engine.recorded_incidents(session.model)
    event_engine.confirm_with_records(found, recorded)
    return sorted(found + recorded, key=lambda e: e.t_start)


def _engineering(session: Session) -> dict:
    """The engineering report, or — so that a report that cannot be built
    never costs the import — the same nine sections saying why not."""
    try:
        return engineering.analyse(session)
    except Exception as e:
        return engineering.unavailable(f"the analysis failed on this file ({type(e).__name__}: {e})")


class ProjectStore:
    """A project folder. Create it with ProjectStore(path); it is created on first use."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        (self.path / "sessions").mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path / "project.sqlite")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(_DDL)
        if "format" not in {row["name"] for row in self.db.execute("PRAGMA table_info(session)")}:
            self.db.execute("ALTER TABLE session ADD COLUMN format TEXT")
        self.db.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # ── lookups ───────────────────────────────────────────────────────────────

    def _named(self, table: str, name: str | None, **extra) -> int | None:
        if not name:
            return None
        row = self.db.execute(f"SELECT id FROM {table} WHERE name = ?", (name,)).fetchone()
        if row:
            return row["id"]
        cols = ["name", *extra]
        cur = self.db.execute(
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            (name, *extra.values()))
        return cur.lastrowid

    # ── write ─────────────────────────────────────────────────────────────────

    def import_file(self, source: str | Path, name: str | None = None, **kwargs) -> int:
        """Load a telemetry file of any supported format and add it to the
        project. Keyword arguments: event, track, driver, vehicle_name, day,
        setup, plus anything load_session accepts (dbc, vehicle, mapping,
        rate_hz, flip, ...). The DBC files a CAN log was decoded with are
        stored with it."""
        store_keys = ("event", "track", "driver", "vehicle_name", "day", "setup", "profile")
        store_args = {k: kwargs.pop(k) for k in store_keys if k in kwargs}
        from . import thresholds as tuning
        with tuning.use(self.thresholds()):
            session = load_session(source, **kwargs)
            sid = self._store_import(session, source, name, kwargs, store_args)
        return sid

    def _store_import(self, session, source, name, kwargs, store_args) -> int:
        # what a later rebuild needs to give the same session: units that were stated rather than read,
        # signs that were flipped, which channels of a lazily read file were loaded, the profile used
        options = {"units": dict(kwargs.get("units") or {}), "flip": list(kwargs.get("flip") or ()),
                   "channels": list(kwargs["channels"]) if kwargs.get("channels") is not None else None,
                   "profile": store_args.pop("profile", "")}
        dbc_paths = []
        if session.raw.info.get("DBC"):
            given = kwargs.get("dbc")
            if isinstance(given, (str, Path)):
                dbc_paths = [given]
            elif given and not hasattr(given, "messages"):
                dbc_paths = list(given)
            else:
                dbc_paths = [Path(source).parent / n for n in session.raw.info["DBC"].split(", ")]
        sid = self.add_session(session, source_path=source, name=name, dbc_paths=dbc_paths, **store_args)
        (self._folder(sid) / "import.json").write_text(json.dumps(options, indent=1), encoding="utf-8")
        return sid

    def import_options(self, session_id: int) -> dict:
        """How a stored session was imported: stated units, flipped signs, the
        channels loaded and the mapping profile used."""
        path = self._folder(session_id) / "import.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else \
            {"units": {}, "flip": [], "channels": None, "profile": ""}

    def add_session(self, session: Session, source_path: str | Path | None = None,
                    name: str | None = None, event: str | None = None, track: str | None = None,
                    driver: str | None = None, vehicle_name: str | None = None,
                    day: str | None = None, setup: dict | None = None, dbc_paths=()) -> int:
        """Store a session. Returns its id. The same file (by SHA-256) can only
        be stored once per project."""
        sha = session.raw.sha256
        if sha:
            dup = self.db.execute("SELECT id FROM session WHERE source_sha256 = ?", (sha,)).fetchone()
            if dup:
                raise ValueError(f"This file is already in the project as session {dup['id']}.")
        track_id = self._named("track", track, length_m=session.lap_length)
        event_id = self._named("event", event, track_id=track_id)
        driver_id = self._named("driver", driver)
        vehicle_id = self._named("vehicle", vehicle_name, params=json.dumps(session.vehicle.to_dict()))
        setup_id = None
        if setup:
            setup_id = self.db.execute("INSERT INTO setup (name, params) VALUES (?, ?)",
                                       (setup.get("name", "setup"), json.dumps(setup))).lastrowid
        cur = self.db.execute(
            """INSERT INTO session (event_id, driver_id, vehicle_id, setup_id, name, day, source_file,
                   source_sha256, imported_at, rate_hz, lap_length_m, samples, lap_source,
                   crossing_method, mapping, units, notes, file_info, format)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_id, driver_id, vehicle_id, setup_id, name or session.raw.source or "session", day,
             session.raw.source, sha, datetime.now(timezone.utc).isoformat(timespec="seconds"),
             session.rate_hz, session.lap_length, len(session.channels), session.lap_source,
             session.crossing_method, json.dumps(session.mapping), json.dumps(session.units),
             json.dumps(session.notes), json.dumps({k: str(v) for k, v in session.raw.info.items()}),
             session.raw.format))
        sid = cur.lastrowid
        for lap in session.laps.itertuples():
            self.db.execute(
                "INSERT INTO lap VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (sid, str(lap.lap), lap.t_start, lap.t_end, lap.lap_time_s, lap.dist_m, lap.d_scale,
                 int(lap.start_crossed), int(lap.end_crossed), lap.status,
                 int(lap.stint), int(lap.stint_lap)))

        folder = self.path / "sessions" / str(sid)
        try:
            (folder / "source").mkdir(parents=True)
            if source_path is not None:
                target = folder / "source" / Path(source_path).name
                shutil.copyfile(source_path, target)
                target.chmod(0o444)
                ldx = next((p for p in (Path(source_path).with_suffix(".ldx"), Path(source_path).with_suffix(".LDX"))
                            if p.exists() and Path(source_path).suffix.lower() == ".ld"), None)
                if ldx:
                    shutil.copyfile(ldx, folder / "source" / (target.stem + ".ldx"))
            for p in dbc_paths:
                if Path(p).exists():
                    (folder / "dbc").mkdir(exist_ok=True)
                    shutil.copyfile(p, folder / "dbc" / Path(p).name)
            session.channels.to_parquet(folder / "channels.parquet", compression="zstd", index=False)
            session.derived.to_parquet(folder / "derived.parquet", compression="zstd", index=False)
            if session.model is not None:
                if not session.model.events:
                    session.model.events = _events(session, self.event_rules())
                save_model(session.model, folder / "model")
            (folder / "engineering.json").write_text(json.dumps(_engineering(session)), encoding="utf-8")
        except Exception:
            self.db.rollback()
            shutil.rmtree(folder, ignore_errors=True)
            raise
        self.db.commit()
        return sid

    # ── read ──────────────────────────────────────────────────────────────────

    def sessions(self) -> pd.DataFrame:
        """One row per stored session, with event, track, driver and best lap."""
        return pd.read_sql_query(
            """SELECT s.id, s.name, s.day, e.name AS event, t.name AS track, d.name AS driver,
                      v.name AS vehicle, s.source_file, s.format, s.rate_hz, s.lap_length_m, s.samples,
                      (SELECT COUNT(*) FROM lap l WHERE l.session_id = s.id) AS laps,
                      (SELECT MIN(lap_time_s) FROM lap l
                        WHERE l.session_id = s.id AND l.status = 'flying') AS best_lap_s,
                      s.imported_at
               FROM session s
               LEFT JOIN event e ON e.id = s.event_id LEFT JOIN track t ON t.id = e.track_id
               LEFT JOIN driver d ON d.id = s.driver_id LEFT JOIN vehicle v ON v.id = s.vehicle_id
               ORDER BY s.id""", self.db)

    def laps(self, session_id: int) -> pd.DataFrame:
        laps = pd.read_sql_query("SELECT * FROM lap WHERE session_id = ? ORDER BY t_start",
                                 self.db, params=(session_id,))
        laps["start_crossed"] = laps["start_crossed"].astype(bool)
        laps["end_crossed"] = laps["end_crossed"].astype(bool)
        laps["lap"] = [int(x) if x.lstrip("-").isdigit() else x for x in laps["lap"]]
        return laps.drop(columns="session_id")

    def channels(self, session_id: int, columns: list | None = None) -> pd.DataFrame:
        """Normalised and derived channels of a stored session. Pass `columns`
        to read only some channels — Parquet reads just those from disk."""
        folder = self._folder(session_id)
        parts = []
        for name in ("channels.parquet", "derived.parquet"):
            path = folder / name
            if columns is None:
                parts.append(pd.read_parquet(path))
            else:
                import pyarrow.parquet as pq
                have = [c for c in columns if c in pq.read_schema(path).names]
                if have:
                    parts.append(pd.read_parquet(path, columns=have))
        return pd.concat(parts, axis=1) if parts else pd.DataFrame()

    def source_file(self, session_id: int) -> Path | None:
        files = sorted((self._folder(session_id) / "source").iterdir())
        row = self.db.execute("SELECT source_file FROM session WHERE id = ?", (session_id,)).fetchone()
        named = [f for f in files if row and f.name == row["source_file"]]
        return (named or [f for f in files if f.suffix.lower() != ".ldx"] or [None])[0]

    def dbc_files(self, session_id: int) -> list:
        """The DBC files a stored CAN log was decoded with."""
        folder = self._folder(session_id) / "dbc"
        return sorted(folder.glob("*.dbc")) if folder.exists() else []

    def model(self, session_id: int, channels: list | None = None) -> SessionModel:
        """The session's canonical model: every channel's metadata and
        provenance, and the arrays of the channels asked for (all of them by
        default) on their own timestamps. A session stored before the model
        existed gets one here, rebuilt from its source file."""
        folder = self._folder(session_id) / "model"
        if not (folder / MANIFEST).exists():
            save_model(self.reload_session(session_id).model, folder)
        return load_model(folder, channels)

    def thresholds(self) -> dict:
        """The project's overrides of the analysis thresholds (`thresholds.json`); empty if none."""
        from . import thresholds as tuning
        path = self.path / "thresholds.json"
        return tuning.load(path) if path.exists() else {}

    def save_thresholds(self, overrides: dict) -> None:
        from . import thresholds as tuning
        tuning.save(self.path / "thresholds.json", overrides)

    def event_rules(self):
        """The project's event rules (`event_rules.json`), or the defaults."""
        path = self.path / "event_rules.json"
        return event_engine.RuleSet.load(path) if path.exists() else event_engine.default_rules()

    def save_event_rules(self, rules) -> None:
        rules.save(self.path / "event_rules.json")

    def maths(self):
        """The project's maths channels (an empty set if none were saved)."""
        from .maths import MathSet
        path = self.path / "maths.json"
        return MathSet.load(path) if path.exists() else MathSet()

    def save_maths(self, mset) -> None:
        mset.save(self.path / "maths.json")

    def engineering(self, session_id: int) -> dict:
        """The session's engineering report (see analysis/engineering.py). Every
        session gets one when it is added; a session stored before that
        existed gets it here, rebuilt from its source file."""
        path = self._folder(session_id) / "engineering.json"
        if not path.exists():
            path.write_text(json.dumps(_engineering(self.reload_session(session_id))), encoding="utf-8")
        return json.loads(path.read_text(encoding="utf-8"))

    def notes(self, session_id: int) -> list:
        """The import notes of a stored session: [(level, message)]."""
        row = self.db.execute("SELECT notes FROM session WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            raise KeyError(f"No session {session_id} in this project.")
        return [tuple(n) for n in json.loads(row["notes"])]

    def reload_session(self, session_id: int) -> Session:
        """Rebuild the full Session from the stored source file, using the
        mapping, stated units, sign flips, channel selection and vehicle
        parameters it was imported with."""
        row = self.db.execute(
            "SELECT s.mapping, s.rate_hz, v.params FROM session s "
            "LEFT JOIN vehicle v ON v.id = s.vehicle_id WHERE s.id = ?", (session_id,)).fetchone()
        if row is None:
            raise KeyError(f"No session {session_id} in this project.")
        source = self.source_file(session_id)
        if source is None:
            raise FileNotFoundError(f"Session {session_id} was stored without its source file.")
        vehicle = VehicleParams(**json.loads(row["params"])) if row["params"] else None
        how = self.import_options(session_id)
        from . import thresholds as tuning
        with tuning.use(self.thresholds()):
            return self._reload(source, row, vehicle, how, session_id)

    def _reload(self, source, row, vehicle, how, session_id) -> Session:
        return load_session(source, mapping=json.loads(row["mapping"]), rate_hz=row["rate_hz"], vehicle=vehicle,
                            dbc=self.dbc_files(session_id) or None, units=how["units"] or None,
                            flip=tuple(how["flip"]), channels=how["channels"])

    def delete_session(self, session_id: int) -> None:
        folder = self._folder(session_id)
        self.db.execute("DELETE FROM session WHERE id = ?", (session_id,))
        self.db.commit()
        source = folder / "source"
        if source.exists():
            for f in source.iterdir():
                f.chmod(0o644)
        shutil.rmtree(folder, ignore_errors=True)

    def _folder(self, session_id: int) -> Path:
        folder = self.path / "sessions" / str(session_id)
        if not folder.exists():
            raise KeyError(f"No session {session_id} in this project.")
        return folder
