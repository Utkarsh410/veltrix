"""
The session library: every session on the machine, found, filtered and
compared without opening folders.

    lib = Library()                                   # <data dir>/library.sqlite
    lib.scan(["~/Telemetry/2026"])                    # index new and changed files
    lib.find(track="Spa", car="GR86", since="2026-09-01")
    lib.progress(track="Spa", car="GR86")             # best and ideal lap, session by session
    lib.compare(12, 15, change="rear bar +1")         # phase 12's comparison, by library id

The library is an index, not a copy: files stay where they are and are found
again by content (SHA-256) when they move. Each file is read once at scan time
for what a list needs — venue, car, driver, date, laps, best and ideal lap,
consistency — and again only when a comparison asks for its samples. A file
that is unchanged (same size and modification time, same engine version) is
not read again.

What a session is called comes from, in order: what the user set for it
(`set`), an alias (`alias`: "Circuit de Spa-Francorchamps Grand Prix" → "Spa"),
and the file itself. Events (a race weekend, a test day) group sessions;
`suggest_events` proposes them from track and date. Tags and a note are the
user's own. Everything is local: library.sqlite in the app's data folder.

Dates are read from the file's header (day first, as MoTeC and the simulators
write them; a date in the file or folder name settles the order when the
header is ambiguous), else from the file or folder name, else the file's
modification time; the row says which (`date_source`).
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .errors import VersionError

LIBRARY_VERSION = 1
FIELDS = ("track", "car", "driver", "simulator", "date", "session_type")       # what `set` can override
ALIAS_KINDS = ("track", "car", "driver")
EVENT_GAP_DAYS = 3                      # sessions at one track closer than this are one event
CONSISTENT_WITHIN = 1.07                # laps slower than this × best are left out of the scatter
SIMULATORS = {"iRacing .ibt": "iRacing"}

_DDL = """
CREATE TABLE IF NOT EXISTS meta     (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS event    (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, kind TEXT NOT NULL DEFAULT '',
                                     note TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS session  (
    id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, root TEXT NOT NULL, sha256 TEXT NOT NULL,
    size INTEGER NOT NULL, mtime REAL NOT NULL, status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
    duplicate_of INTEGER, format TEXT, venue TEXT, vehicle TEXT, driver TEXT, session_type TEXT,
    date TEXT, date_source TEXT, duration_s REAL, samples INTEGER, rate_hz REAL, laps INTEGER,
    flying_laps INTEGER, stints INTEGER, best_lap TEXT, best_lap_s REAL, ideal_lap_s REAL, median_lap_s REAL,
    consistency_s REAL, lap_length_m REAL, warnings INTEGER, indexed_at TEXT NOT NULL, engine_version TEXT,
    event_id INTEGER REFERENCES event(id) ON DELETE SET NULL, note TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS override (session_id INTEGER NOT NULL REFERENCES session(id) ON DELETE CASCADE,
                                     field TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY (session_id, field));
CREATE TABLE IF NOT EXISTS alias    (kind TEXT NOT NULL, alias TEXT NOT NULL, name TEXT NOT NULL,
                                     PRIMARY KEY (kind, alias));
CREATE TABLE IF NOT EXISTS tag      (session_id INTEGER NOT NULL REFERENCES session(id) ON DELETE CASCADE,
                                     tag TEXT NOT NULL, PRIMARY KEY (session_id, tag));
CREATE INDEX IF NOT EXISTS session_sha ON session(sha256);
"""


def _engine_version() -> str:
    from .model_build import ENGINE_VERSION
    return ENGINE_VERSION


def default_path() -> Path:
    from .user_data import data_dir
    return data_dir() / "library.sqlite"


# ── reading one file ──────────────────────────────────────────────────────────

_ISO_IN_NAME = re.compile(r"(20\d\d)[-._](\d\d)[-._](\d\d)")


def _date_of(info: dict, name: str, mtime: float, folder: str = "") -> tuple[str, str]:
    """(ISO date and time, where it came from). A date in the file's name, or
    else its folder's, counts as "file name"."""
    found = _ISO_IN_NAME.search(name) or _ISO_IN_NAME.search(folder)
    from_name = None
    if found:
        try:
            from_name = date(int(found[1]), int(found[2]), int(found[3]))
        except ValueError:
            from_name = None
    text = str(info.get("Log Date", "") or "").strip()
    clock = str(info.get("Log Time", "") or "").strip()
    if text:
        parts = [int(p) for p in re.findall(r"\d+", text)[:3]] if re.fullmatch(r"[\d/.\- ]+", text) else []
        candidates = []
        if len(parts) == 3:
            a, b, c = parts
            if a > 31:                                   # 2026-10-02
                candidates = [(a, b, c)]
            else:
                year = c if c > 99 else 2000 + c
                candidates = [(year, b, a), (year, a, b)]  # day first, then month first
        readings = [day for day in (_ok(*c) for c in candidates) if day]
        # the file name settles a date both ways round (02/04: 2 April or 4 February); else day first
        near = [day for day in readings if from_name and abs((day - from_name).days) <= 1]
        for day in (near or readings)[:1]:
            stamp = day.isoformat() + (f" {clock[:8]}" if re.fullmatch(r"\d\d?:\d\d(:\d\d)?", clock[:8]) else "")
            return stamp, "file header"
    if from_name:
        return from_name.isoformat(), "file name"
    return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S"), "file time"


def _ok(y, m, d):
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def summarise(path, cars_dir=None) -> dict:
    """What the library keeps about one file. Never raises: a failure is status "failed" with the reason."""
    from .session import load_session
    path = Path(path)
    stat = path.stat()
    row = {"status": "ok", "error": "", "size": stat.st_size, "mtime": stat.st_mtime}
    try:
        cars = None
        if cars_dir:
            from .cars import CarLibrary
            cars = CarLibrary(cars_dir)
        s = load_session(path, cars=cars)
        info = s.raw.info
        laps = s.laps
        flying = laps[laps["status"] == "flying"]
        timed = flying if len(flying) else laps
        times = timed["lap_time_s"].to_numpy(float)
        ok = np.isfinite(times)
        best_i = int(np.nanargmin(times)) if ok.any() else None
        best = float(times[best_i]) if best_i is not None else None
        close = times[ok & (times <= best * CONSISTENT_WITHIN)] if best else np.array([])
        ideal = None
        try:
            from . import corners
            ideal = float(corners.analyse(s)["ideal_lap_s"])
            ideal = ideal if np.isfinite(ideal) else None
        except Exception:                               # too few laps for corners: no ideal lap, nothing else lost
            ideal = None
        t = s.channels["time"].to_numpy()
        when, source = _date_of(info, path.name, stat.st_mtime, path.parent.name)
        row.update(
            sha256=s.raw.sha256, format=s.raw.format, venue=str(info.get("Venue", "") or ""),
            vehicle=str(info.get("Vehicle", "") or ""), driver=str(info.get("Driver", "") or ""),
            session_type=str(info.get("Session", "") or ""), date=when, date_source=source,
            duration_s=float(t[-1] - t[0]) if len(t) else 0.0, samples=len(s.channels), rate_hz=float(s.rate_hz),
            laps=len(laps), flying_laps=len(flying), stints=int(laps["stint"].nunique()) if len(laps) else 0,
            best_lap=str(timed["lap"].iloc[best_i]) if best_i is not None else None,
            best_lap_s=round(best, 3) if best else None, ideal_lap_s=round(ideal, 3) if ideal else None,
            median_lap_s=round(float(np.median(close)), 3) if len(close) else None,
            consistency_s=round(float(np.std(close, ddof=1)), 3) if len(close) >= 3 else None,
            lap_length_m=float(s.lap_length) if s.lap_length else None, warnings=len(s.warnings()))
    except Exception as e:
        row.update(status="failed", error=str(e) if isinstance(e, ValueError) else f"{type(e).__name__}: {e}",
                   sha256=_sha(path), date_source="file time",
                   date=_date_of({}, path.name, stat.st_mtime, path.parent.name)[0])
    return row


def _summarise_in_worker(path, cars_dir, log_dir) -> dict:
    from .batch import _log_like_the_parent
    _log_like_the_parent(log_dir)
    return summarise(path, cars_dir)


# ── the library ───────────────────────────────────────────────────────────────

class Library:
    """The session index. Library() opens the user's own; Library(path) another one."""

    def __init__(self, path=None):
        self.path = Path(path) if path else default_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(_DDL)
        row = self.db.execute("SELECT value FROM meta WHERE key = 'library_version'").fetchone()
        found = int(row["value"]) if row else LIBRARY_VERSION
        if found > LIBRARY_VERSION:
            self.db.close()
            raise VersionError(f"This session library was written by a newer release of Veltrix (version {found}); "
                               f"this release reads up to {LIBRARY_VERSION}. It has not been changed.")
        self.db.execute("INSERT OR REPLACE INTO meta VALUES ('library_version', ?)", (str(LIBRARY_VERSION),))
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ── scanning ──────────────────────────────────────────────────────────────

    def scan(self, folders, recursive: bool = True, jobs: int = 1, progress=None, cars_dir=None,
             rescan: bool = False) -> dict:
        """Index the telemetry files under these folders. Returns counts:
        {"added", "updated", "unchanged", "moved", "missing", "failed", "duplicate"}.

        A file whose size and modification time are as indexed (by this engine
        version) is not read again unless rescan. A file found under a new path
        with the content of one that is gone keeps its id, tags and event. Files
        indexed under these folders that are no longer there are marked missing."""
        from .batch import find_files
        counts = dict.fromkeys(("added", "updated", "unchanged", "moved", "missing", "failed", "duplicate"), 0)
        version = _engine_version()
        todo = []
        seen = set()
        roots = [Path(f).expanduser().resolve() for f in folders]
        for root in roots:
            for p in find_files(root, recursive=recursive):
                p = p.resolve()
                seen.add(str(p))
                old = self.db.execute("SELECT * FROM session WHERE path = ?", (str(p),)).fetchone()
                stat = p.stat()
                if (old and not rescan and old["size"] == stat.st_size and abs(old["mtime"] - stat.st_mtime) < 1e-6
                        and old["engine_version"] == version and old["status"] != "missing"):
                    counts["unchanged"] += 1
                    continue
                todo.append((p, root, old))
        # files under these folders that were indexed and are gone
        for row in self.db.execute("SELECT id, path FROM session WHERE status != 'missing'").fetchall():
            p = Path(row["path"])
            if any(p.is_relative_to(r) for r in roots) and str(p) not in seen and not p.exists():
                self.db.execute("UPDATE session SET status = 'missing' WHERE id = ?", (row["id"],))
                counts["missing"] += 1
        self.db.commit()

        def store(item, summary):
            p, root, old = item
            kind = self._store(p, root, old, summary, version)
            counts[kind] += 1
            if progress:
                progress(sum(counts.values()) - counts["unchanged"] - counts["missing"], len(todo), p, kind, summary)

        if jobs > 1 and len(todo) > 1:
            from .batch import _parent_log_dir
            with ProcessPoolExecutor(max_workers=jobs) as pool:
                futures = [(item, pool.submit(_summarise_in_worker, item[0], cars_dir, _parent_log_dir()))
                           for item in todo]
                for item, fut in futures:
                    store(item, fut.result())
        else:
            for item in todo:
                store(item, summarise(item[0], cars_dir))
        # a missing file that turned up again elsewhere was handled by _store; the count above may include it
        counts["missing"] = self.db.execute("SELECT COUNT(*) FROM session WHERE status = 'missing'").fetchone()[0]
        return counts

    def _store(self, path: Path, root: Path, old, s: dict, version: str) -> str:
        columns = ("sha256", "size", "mtime", "status", "error", "format", "venue", "vehicle", "driver", "session_type",
                   "date", "date_source", "duration_s", "samples", "rate_hz", "laps", "flying_laps", "stints",
                   "best_lap", "best_lap_s", "ideal_lap_s", "median_lap_s", "consistency_s", "lap_length_m",
                   "warnings")
        values = {c: s.get(c) for c in columns}
        values.update(indexed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), engine_version=version,
                      root=str(root), duplicate_of=None)
        kind = "failed" if s["status"] == "failed" else "updated" if old else "added"
        if old is None:
            moved = self.db.execute("SELECT id, path FROM session WHERE sha256 = ? AND status = 'missing'",
                                    (s["sha256"],)).fetchone()
            twin = self.db.execute("SELECT id FROM session WHERE sha256 = ? AND status IN ('ok', 'failed')",
                                   (s["sha256"],)).fetchone()
            if moved:                                  # the same content under a new path: keep id, tags, event
                old = self.db.execute("SELECT * FROM session WHERE id = ?", (moved["id"],)).fetchone()
                kind = "moved"
            elif twin:
                values.update(status="duplicate", error=f"same content as session {twin['id']}", duplicate_of=twin["id"])
                kind = "duplicate"
        values["path"] = str(path)
        if old is None:
            cols = list(values)
            self.db.execute(f"INSERT INTO session ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                            [values[c] for c in cols])
        else:
            self.db.execute(f"UPDATE session SET {', '.join(f'{c} = ?' for c in values)} WHERE id = ?",
                            [*values.values(), old["id"]])
        self.db.commit()
        return kind

    # ── what the user says ────────────────────────────────────────────────────

    def _id(self, session_id) -> int:
        row = self.db.execute("SELECT id FROM session WHERE id = ?", (int(session_id),)).fetchone()
        if row is None:
            raise KeyError(f"session {session_id} is not in the library")
        return row["id"]

    def set(self, session_id, field: str, value: str | None) -> None:
        """Override what the file says: track, car, driver, simulator, date, session_type. None clears it."""
        if field not in FIELDS:
            raise ValueError(f"'{field}' cannot be set: choose from {', '.join(FIELDS)}")
        sid = self._id(session_id)
        if value is None or value == "":
            self.db.execute("DELETE FROM override WHERE session_id = ? AND field = ?", (sid, field))
        else:
            if field == "date":
                value = pd.Timestamp(value).isoformat(sep=" ")    # refuses what is not a date
            self.db.execute("INSERT OR REPLACE INTO override VALUES (?, ?, ?)", (sid, field, str(value)))
        self.db.commit()

    def alias(self, kind: str, alias: str, name: str | None) -> None:
        """Call every session whose file says `alias` by `name` (None removes the alias)."""
        if kind not in ALIAS_KINDS:
            raise ValueError(f"aliases are for {', '.join(ALIAS_KINDS)}")
        if name:
            self.db.execute("INSERT OR REPLACE INTO alias VALUES (?, ?, ?)", (kind, alias, name))
        else:
            self.db.execute("DELETE FROM alias WHERE kind = ? AND alias = ?", (kind, alias))
        self.db.commit()

    def aliases(self) -> pd.DataFrame:
        return pd.read_sql_query("SELECT kind, alias, name FROM alias ORDER BY kind, alias", self.db)

    def tag(self, session_ids, *tags: str, remove: bool = False) -> None:
        for sid in ([session_ids] if isinstance(session_ids, (int, str)) else session_ids):
            sid = self._id(sid)
            for t in tags:
                t = t.strip()
                if not t:
                    continue
                if remove:
                    self.db.execute("DELETE FROM tag WHERE session_id = ? AND tag = ?", (sid, t))
                else:
                    self.db.execute("INSERT OR IGNORE INTO tag VALUES (?, ?)", (sid, t))
        self.db.commit()

    def note(self, session_id, text: str) -> None:
        self.db.execute("UPDATE session SET note = ? WHERE id = ?", (text, self._id(session_id)))
        self.db.commit()

    def forget(self, session_id) -> None:
        """Take a session out of the library. The file is not touched."""
        self.db.execute("DELETE FROM session WHERE id = ?", (self._id(session_id),))
        self.db.commit()

    # ── events ────────────────────────────────────────────────────────────────

    def create_event(self, name: str, kind: str = "", sessions=(), note: str = "") -> int:
        if not name.strip():
            raise ValueError("an event needs a name")
        row = self.db.execute("SELECT id FROM event WHERE name = ?", (name,)).fetchone()
        eid = row["id"] if row else self.db.execute("INSERT INTO event (name, kind, note) VALUES (?, ?, ?)",
                                                    (name, kind, note)).lastrowid
        if row and (kind or note):
            self.db.execute("UPDATE event SET kind = COALESCE(NULLIF(?, ''), kind), note = COALESCE(NULLIF(?, ''), note) "
                            "WHERE id = ?", (kind, note, eid))
        for sid in sessions:
            self.db.execute("UPDATE session SET event_id = ? WHERE id = ?", (eid, self._id(sid)))
        self.db.commit()
        return eid

    def leave_event(self, session_ids) -> None:
        for sid in session_ids:
            self.db.execute("UPDATE session SET event_id = NULL WHERE id = ?", (self._id(sid),))
        self.db.commit()

    def delete_event(self, name: str) -> None:
        """The event goes; its sessions stay, in no event."""
        self.db.execute("DELETE FROM event WHERE name = ?", (name,))
        self.db.commit()

    def events(self) -> pd.DataFrame:
        """One row per event: kind, track(s), first and last date, sessions, best lap."""
        rows = []
        table = self.find(include=("ok", "failed", "missing", "duplicate"))
        for e in self.db.execute("SELECT * FROM event ORDER BY name").fetchall():
            part = table[table["event"] == e["name"]]
            dates = part["date"].dropna()
            rows.append({"event": e["name"], "kind": e["kind"], "tracks": ", ".join(sorted(set(part["track"]) - {""})),
                         "first": dates.min()[:10] if len(dates) else "", "last": dates.max()[:10] if len(dates) else "",
                         "sessions": len(part), "best_lap_s": part["best_lap_s"].min() if len(part) else None,
                         "note": e["note"]})
        return pd.DataFrame(rows, columns=["event", "kind", "tracks", "first", "last", "sessions", "best_lap_s", "note"])

    def suggest_events(self, gap_days: int = EVENT_GAP_DAYS) -> list:
        """Events to create from the sessions in none: one per track for each run of
        dates no more than gap_days apart. [{"name", "track", "first", "last", "sessions": [ids]}]."""
        table = self.find()
        table = table[(table["event"] == "") & (table["track"] != "")]
        out = []
        for track, part in table.groupby("track"):
            part = part.assign(day=pd.to_datetime(part["date"].str[:10], errors="coerce")).dropna(subset=["day"])
            part = part.sort_values("day")
            group, last = [], None
            for row in part.itertuples():
                if last is not None and (row.day - last).days > gap_days:
                    out.append(_proposal(track, group))
                    group = []
                group.append(row)
                last = row.day
            if group:
                out.append(_proposal(track, group))
        return out

    # ── finding ───────────────────────────────────────────────────────────────

    def find(self, text: str | None = None, track=None, car=None, driver=None, simulator=None, tag=None,
             event=None, since=None, until=None, session_type=None, include=("ok",), sort: str = "date") -> pd.DataFrame:
        """Sessions matching every filter given. Text filters match a part of the
        name, ignoring case; `text` matches any of file, track, car, driver,
        event, tags and note. since/until are dates (inclusive). include: which
        statuses to list (ok, failed, missing, duplicate)."""
        table = self._table()
        if include:
            table = table[table["status"].isin(include)]
        def has(col, value):
            return table[col].str.contains(str(value), case=False, regex=False, na=False)
        for col, value in (("track", track), ("car", car), ("driver", driver), ("simulator", simulator),
                           ("event", event), ("session_type", session_type)):
            if value:
                table = table[has(col, value)]
        if tag:
            wanted = {t.lower() for t in ([tag] if isinstance(tag, str) else tag)}
            table = table[[wanted <= {t.lower() for t in ts} for ts in table["tag_list"]]]
        if since:
            table = table[table["date"].fillna("") >= pd.Timestamp(since).strftime("%Y-%m-%d")]
        if until:
            table = table[table["date"].fillna("").str[:10] <= pd.Timestamp(until).strftime("%Y-%m-%d")]
        if text:
            blob = (table[["file", "track", "car", "driver", "event", "tags", "note", "venue", "vehicle"]]
                    .fillna("").astype(str).agg(" ".join, axis=1))
            table = table[blob.str.contains(text, case=False, regex=False)]
        if sort:
            descending = sort.startswith("-")
            key = sort.lstrip("-")
            if key not in table.columns:
                raise ValueError(f"cannot sort by '{key}'")
            table = table.sort_values([key, "id"], ascending=not descending, na_position="last", kind="stable")
        return table.drop(columns="tag_list").reset_index(drop=True)

    def get(self, session_id) -> dict:
        table = self._table()
        row = table[table["id"] == self._id(session_id)]
        return {k: (None if pd.isna(v) else v.item() if hasattr(v, "item") else v)
                for k, v in row.drop(columns="tag_list").iloc[0].items()}

    def _table(self) -> pd.DataFrame:
        table = pd.read_sql_query(
            """SELECT s.*, COALESCE(e.name, '') AS event FROM session s LEFT JOIN event e ON e.id = s.event_id
               ORDER BY s.id""", self.db)
        for col in ("format", "venue", "vehicle", "driver", "session_type", "date", "date_source", "best_lap", "error"):
            table[col] = table[col].fillna("").astype(str)        # a failed file has none of these: empty, not NaN
        for col in ("samples", "laps", "flying_laps", "stints", "warnings", "duplicate_of", "event_id"):
            table[col] = table[col].astype("Int64")
        overrides = {}
        for r in self.db.execute("SELECT * FROM override").fetchall():
            overrides.setdefault(r["session_id"], {})[r["field"]] = r["value"]
        aliases = {(r["kind"], r["alias"]): r["name"] for r in self.db.execute("SELECT * FROM alias").fetchall()}
        tags = {}
        for r in self.db.execute("SELECT * FROM tag ORDER BY tag").fetchall():
            tags.setdefault(r["session_id"], []).append(r["tag"])

        def pick(row, field, from_file, kind=None):
            mine = overrides.get(row["id"], {}).get(field)
            if mine:
                return mine
            value = row[from_file] or ""
            return aliases.get((kind, value), value) if kind else value
        records = table.to_dict("records")
        table["file"] = [Path(r["path"]).name for r in records]
        table["track"] = [pick(r, "track", "venue", "track") for r in records]
        table["car"] = [pick(r, "car", "vehicle", "car") for r in records]
        table["driver"] = [pick(r, "driver", "driver", "driver") for r in records]
        table["session_type"] = [pick(r, "session_type", "session_type") for r in records]
        table["simulator"] = [overrides.get(r["id"], {}).get("simulator") or SIMULATORS.get(r["format"] or "", "")
                              for r in records]
        table["date"] = [overrides.get(r["id"], {}).get("date") or r["date"] for r in records]
        table["date_source"] = ["you" if "date" in overrides.get(r["id"], {}) else r["date_source"] for r in records]
        table["tag_list"] = [tags.get(r["id"], []) for r in records]
        table["tags"] = [", ".join(t) for t in table["tag_list"]]
        first = ["id", "date", "track", "car", "driver", "simulator", "session_type", "event", "tags", "laps",
                 "flying_laps", "best_lap_s", "ideal_lap_s", "median_lap_s", "consistency_s", "file", "status"]
        return table[first + [c for c in table.columns if c not in first]]

    # ── across sessions ───────────────────────────────────────────────────────

    def progress(self, **filters) -> pd.DataFrame:
        """Session by session in date order: best, ideal and median lap and the
        scatter, with the change in best lap from the previous session and the
        best so far. Meant for one track and car (filter on both)."""
        table = self.find(sort="date", **filters)
        table = table[table["best_lap_s"].notna()]
        out = table[["id", "date", "track", "car", "driver", "event", "flying_laps", "best_lap_s", "ideal_lap_s",
                     "median_lap_s", "consistency_s", "file"]].copy()
        out["change_s"] = out["best_lap_s"].diff().round(3)
        out["best_so_far_s"] = out["best_lap_s"].cummin()
        if out["track"].nunique() > 1 or out["car"].nunique() > 1:
            out.attrs["warning"] = ("more than one track or car: filter on one of each for lap times that compare "
                                    f"({', '.join(sorted(set(out['track'])))} / {', '.join(sorted(set(out['car'])))})")
        return out.reset_index(drop=True)

    def best(self, by=("track", "car"), **filters) -> pd.DataFrame:
        """The fastest session for each track and car (or other grouping)."""
        table = self.find(**filters)
        table = table[table["best_lap_s"].notna()]
        if table.empty:
            return table
        idx = table.groupby(list(by))["best_lap_s"].idxmin()
        cols = [*by, "best_lap_s", "ideal_lap_s", "id", "date", "driver", "file"]
        return table.loc[idx, list(dict.fromkeys(cols))].sort_values(list(by)).reset_index(drop=True)

    def load(self, session_id, **kwargs):
        """The session itself, read from its file (which must still be there)."""
        from .session import load_session
        row = self.get(session_id)
        if row["status"] == "missing" or not Path(row["path"]).exists():
            raise FileNotFoundError(f"session {session_id}: {row['path']} is no longer there — rescan its folder "
                                    "if it was moved")
        return load_session(row["path"], **kwargs)

    def compare(self, a, b, change: str = "", **load_options) -> dict:
        """Session b against session a (analysis/ab.py), by library id."""
        from .analysis import ab
        result = ab.compare_sessions(self.load(a, **load_options), self.load(b, **load_options), change)
        ra, rb = self.get(a), self.get(b)
        result["library"] = {"a": {k: ra[k] for k in ("id", "date", "track", "car", "driver", "file")},
                             "b": {k: rb[k] for k in ("id", "date", "track", "car", "driver", "file")}}
        # the library's names for the circuit and car settle what the files spell differently
        if ra["track"] and ra["track"] == rb["track"]:
            result["problems"] = [p for p in result["problems"] if not p.startswith("different circuits")]
        if ra["car"] and ra["car"] == rb["car"]:
            result["problems"] = [p for p in result["problems"] if not p.startswith("different cars")]
        result["comparable"] = not result["problems"]
        return result


def _proposal(track: str, rows: list) -> dict:
    first, last = rows[0].day, rows[-1].day
    name = f"{track} {first:%Y-%m-%d}" + (f" to {last:%m-%d}" if last != first else "")
    return {"name": name, "track": track, "first": f"{first:%Y-%m-%d}", "last": f"{last:%Y-%m-%d}",
            "sessions": [int(r.id) for r in rows]}
