"""
Project store: SQLite for metadata, Parquet for telemetry.

Layout of a project folder:

  project.sqlite                     events, sessions, laps, drivers, vehicles, tracks, setups
  sessions/<id>/source/<file>        the imported file, byte-for-byte, never modified
  sessions/<id>/channels.parquet     normalised channels
  sessions/<id>/derived.parquet      derived channels

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

from .session import Session, load_session
from .vehicle import VehicleParams

SCHEMA_VERSION = 1

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


class ProjectStore:
    """A project folder. Create it with ProjectStore(path); it is created on first use."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        (self.path / "sessions").mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path / "project.sqlite")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(_DDL)
        self.db.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
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
        """Load a telemetry file and add it to the project. Keyword arguments:
        event, track, driver, vehicle_name, day, setup, plus anything
        build_session accepts (vehicle, mapping, rate_hz, flip, ...)."""
        store_keys = ("event", "track", "driver", "vehicle_name", "day", "setup")
        store_args = {k: kwargs.pop(k) for k in store_keys if k in kwargs}
        session = load_session(source, **kwargs)
        return self.add_session(session, source_path=source, name=name, **store_args)

    def add_session(self, session: Session, source_path: str | Path | None = None,
                    name: str | None = None, event: str | None = None, track: str | None = None,
                    driver: str | None = None, vehicle_name: str | None = None,
                    day: str | None = None, setup: dict | None = None) -> int:
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
                   crossing_method, mapping, units, notes, file_info)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_id, driver_id, vehicle_id, setup_id, name or session.raw.source or "session", day,
             session.raw.source, sha, datetime.now(timezone.utc).isoformat(timespec="seconds"),
             session.rate_hz, session.lap_length, len(session.channels), session.lap_source,
             session.crossing_method, json.dumps(session.mapping), json.dumps(session.units),
             json.dumps(session.notes), json.dumps({k: str(v) for k, v in session.raw.info.items()})))
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
            session.channels.to_parquet(folder / "channels.parquet", compression="zstd", index=False)
            session.derived.to_parquet(folder / "derived.parquet", compression="zstd", index=False)
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
                      v.name AS vehicle, s.source_file, s.rate_hz, s.lap_length_m, s.samples,
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
        files = list((self._folder(session_id) / "source").iterdir())
        return files[0] if files else None

    def reload_session(self, session_id: int) -> Session:
        """Rebuild the full Session from the stored source file, using the
        mapping and vehicle parameters it was imported with."""
        row = self.db.execute(
            "SELECT s.mapping, s.rate_hz, v.params FROM session s "
            "LEFT JOIN vehicle v ON v.id = s.vehicle_id WHERE s.id = ?", (session_id,)).fetchone()
        if row is None:
            raise KeyError(f"No session {session_id} in this project.")
        source = self.source_file(session_id)
        if source is None:
            raise FileNotFoundError(f"Session {session_id} was stored without its source file.")
        vehicle = VehicleParams(**json.loads(row["params"])) if row["params"] else None
        return load_session(source, mapping=json.loads(row["mapping"]), rate_hz=row["rate_hz"],
                            vehicle=vehicle)

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
