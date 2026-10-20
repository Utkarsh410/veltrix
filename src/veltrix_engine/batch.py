"""
Batch mode: every telemetry file in a folder, imported and summarised in one
run, with the same outputs a single file gets.

    rows = run_batch(find_files("season_2026"), out_dir="season_2026_out", outputs=("report", "engineer"))
    write_index(rows, "season_2026_out")

A file that fails is a row with its reason, never the end of the run. The
same file found twice (by content) is read once. Outputs mirror the folder
layout under out_dir:

  report    <name>_report.html     the HTML report
  engineer  <name>_engineer.md     the engineering report as Markdown
  .csv / .parquet / .mf4           the session exported (exports.py)

`write_index` writes index.csv and index.md: one row per file with its format,
venue, car, driver, laps and best lap, what was written and any error.

Each file is a separate import, so with jobs > 1 that many are held in memory
at once (up to about 1.2 GB each for a 260 MB CSV).
"""

from __future__ import annotations

import csv
import hashlib
import os
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

OUTPUTS = ("report", "engineer", ".csv", ".parquet", ".mf4")
COMPANIONS = {".ldx", ".dbc"}           # read with the file beside them, not on their own
INDEX_COLUMNS = ("file", "status", "format", "venue", "vehicle", "driver", "date", "samples", "rate_hz",
                 "duration_s", "laps", "flying_laps", "best_lap", "best_lap_s", "warnings", "seconds", "outputs",
                 "error", "sha256")


@dataclass
class BatchRow:
    file: str                            # relative to the folder searched
    status: str = "ok"                   # ok | failed | duplicate
    format: str = ""
    venue: str = ""
    vehicle: str = ""
    driver: str = ""
    date: str = ""
    samples: int = 0
    rate_hz: float = 0.0
    duration_s: float = 0.0
    laps: int = 0
    flying_laps: int = 0
    best_lap: int | None = None
    best_lap_s: float | None = None
    warnings: int = 0
    seconds: float = 0.0
    outputs: list = field(default_factory=list)
    error: str = ""
    sha256: str = ""


def find_files(folder, recursive: bool = True) -> list:
    """The telemetry files in a folder, in name order: every extension the
    engine (and any plug-in) reads, without hidden files and the companion
    files (.ldx, .dbc) that are read with their log."""
    from .importers import supported_extensions
    folder = Path(folder)
    if not folder.is_dir():
        raise ValueError(f"{folder} is not a folder.")
    accepted = set(supported_extensions()) - COMPANIONS
    found = []
    for p in sorted(folder.rglob("*") if recursive else folder.iterdir()):
        if not p.is_file() or any(part.startswith(".") for part in p.relative_to(folder).parts):
            continue
        suffix = p.suffix.lower()
        if suffix == ".gz":
            suffix = Path(p.stem).suffix.lower()
        if suffix in accepted:
            found.append(p)
    return found


def _output_stem(path: Path, root: Path, out_dir: Path) -> Path:
    rel = path.relative_to(root) if path.is_relative_to(root) else Path(path.name)
    name = rel.name[:-3] if rel.name.lower().endswith(".gz") else rel.name
    return out_dir / rel.parent / (Path(name).stem or name)


def _one(path: Path, root: Path, out_dir: Path | None, outputs: tuple, options: dict) -> BatchRow:
    """Import one file and write its outputs. Never raises: a failure is the row's error."""
    from . import thresholds as tuning
    _log_like_the_parent(options.get("log_dir"))
    try:
        overrides = tuning.load(options["thresholds"]) if options.get("thresholds") else None
    except Exception as e:
        return BatchRow(file=path.name, status="failed", error=f"thresholds file: {e}")
    with tuning.use(overrides):
        return _import_one(path, root, out_dir, outputs, options)


def _log_like_the_parent(folder) -> None:
    """In a worker process: the parent's log file, or nothing — never tracebacks on the terminal."""
    import logging
    if folder:
        from .logs import setup
        setup(folder)
    elif not logging.getLogger("veltrix").handlers:
        logging.getLogger("veltrix").addHandler(logging.NullHandler())


def _parent_log_dir():
    import logging
    from .logs import _HANDLER_TAG
    for h in logging.getLogger("veltrix").handlers:
        if getattr(h, _HANDLER_TAG, False):
            return str(Path(h.baseFilename).parent)
    return None


def _import_one(path: Path, root: Path, out_dir: Path | None, outputs: tuple, options: dict) -> BatchRow:
    from .session import load_session
    started = time.perf_counter()
    row = BatchRow(file=str(path.relative_to(root)) if path.is_relative_to(root) else path.name)
    try:
        cars = None
        if options.get("cars_dir"):
            from .cars import CarLibrary
            cars = CarLibrary(options["cars_dir"])
        s = load_session(path, cars=cars, dbc=options.get("dbc"))
        info = s.raw.info
        flying = s.laps[s.laps["status"] == "flying"]
        timed = flying if len(flying) else s.laps
        times = timed["lap_time_s"].to_numpy(float)
        best = int(np.nanargmin(times)) if np.isfinite(times).any() else None
        t = s.channels["time"].to_numpy()
        row.format, row.sha256 = s.raw.format, s.raw.sha256
        row.venue, row.vehicle, row.driver = (str(info.get(k, "") or "") for k in ("Venue", "Vehicle", "Driver"))
        row.date = " ".join(str(info.get(k, "") or "") for k in ("Log Date", "Log Time")).strip()
        row.samples, row.rate_hz = len(s.channels), float(s.rate_hz)
        row.duration_s = float(t[-1] - t[0]) if len(t) else 0.0
        row.laps, row.flying_laps, row.warnings = len(s.laps), len(flying), len(s.warnings())
        if best is not None:
            row.best_lap, row.best_lap_s = int(timed["lap"].iloc[best]), round(float(times[best]), 3)
        if out_dir is not None and outputs:
            stem = _output_stem(path, root, out_dir)
            stem.parent.mkdir(parents=True, exist_ok=True)
            for kind in outputs:
                if kind == "report":
                    from .report import write_report
                    row.outputs.append(str(write_report(s, f"{stem}_report.html")))
                elif kind == "engineer":
                    from .analysis import engineering
                    target = Path(f"{stem}_engineer.md")
                    target.write_text(engineering.to_markdown(engineering.analyse(s),
                                                              f"Performance engineering report — {path.name}"),
                                      encoding="utf-8")
                    row.outputs.append(str(target))
                else:
                    from .exports import export_session
                    row.outputs.append(str(export_session(s, f"{stem}{kind}")))
    except Exception as e:                    # one bad file is that file's row, not the end of the run
        row.status, row.error = "failed", f"{type(e).__name__}: {e}" if not isinstance(e, ValueError) else str(e)
    row.seconds = round(time.perf_counter() - started, 2)
    return row


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def run_batch(files, root=None, out_dir=None, outputs: tuple = (), jobs: int = 1, progress=None,
              cars_dir=None, dbc=None, thresholds=None) -> list:
    """One BatchRow per file, in the order given.

    root:      the folder paths are shown relative to (default: their common folder)
    out_dir:   where outputs go, mirroring the layout under root; None writes nothing
    outputs:   any of OUTPUTS
    jobs:      files imported side by side, each in its own process
    progress:  progress(done, total, row) after each file
    cars_dir:  a car library folder, for car parameters the files do not give
    dbc:       DBC file(s) for CAN logs (default: the .dbc beside each log)
    thresholds: a thresholds JSON file, applied in every process

    Importers registered in code are not seen by the processes of jobs > 1;
    installed ones (entry points) are."""
    files = [Path(f) for f in files]
    unknown = [o for o in outputs if o not in OUTPUTS]
    if unknown:
        raise ValueError(f"unknown output(s) {unknown}: choose from {', '.join(OUTPUTS)}")
    if root is None:
        root = Path(os.path.commonpath([f.resolve().parent for f in files])) if files else Path(".")
    root = Path(root).resolve()
    out_dir = Path(out_dir).resolve() if out_dir is not None else None
    options = {"cars_dir": str(cars_dir) if cars_dir else None, "dbc": dbc,
               "thresholds": str(thresholds) if thresholds else None, "log_dir": _parent_log_dir()}

    first_of, todo, rows = {}, [], [None] * len(files)
    for k, f in enumerate(files):             # the same content twice is read once
        try:
            sha = _sha(f)
        except OSError as e:
            rows[k] = BatchRow(file=f.name, status="failed", error=str(e))
            continue
        if sha in first_of:
            rows[k] = BatchRow(file=str(f.resolve().relative_to(root)), status="duplicate", sha256=sha,
                               error=f"same content as {files[first_of[sha]].name}")
        else:
            first_of[sha] = k
            todo.append(k)

    done = 0
    def finished(k, row):
        nonlocal done
        rows[k] = row
        done += 1
        if progress:
            progress(done, len(files), row)

    for k, row in enumerate(rows):            # duplicates and unreadable files are settled already
        if row is not None:
            finished(k, row)

    if jobs <= 1 or len(todo) <= 1:
        for k in todo:
            finished(k, _one(files[k].resolve(), root, out_dir, tuple(outputs), options))
    else:
        with ProcessPoolExecutor(max_workers=jobs) as pool:
            futures = {k: pool.submit(_one, files[k].resolve(), root, out_dir, tuple(outputs), options) for k in todo}
            for k, fut in futures.items():
                finished(k, fut.result())
    return rows


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return " ".join(Path(v).name for v in value)
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def write_index(rows, out_dir) -> list:
    """index.csv and index.md in out_dir, one row per file. Returns their paths."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    table = [{c: _cell(asdict(r)[c]) for c in INDEX_COLUMNS} for r in rows]
    with open(out_dir / "index.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=INDEX_COLUMNS)
        w.writeheader()
        w.writerows(table)
    ok = sum(r.status == "ok" for r in rows)
    shown = ("file", "status", "format", "venue", "vehicle", "driver", "laps", "best_lap_s", "warnings", "outputs", "error")
    lines = [f"# Batch import — {len(rows)} file(s)", "",
             f"{ok} read, {sum(r.status == 'failed' for r in rows)} failed, "
             f"{sum(r.status == 'duplicate' for r in rows)} duplicate(s).", "",
             "| " + " | ".join(shown) + " |", "|" + "---|" * len(shown)]
    for r in table:
        lines.append("| " + " | ".join(r[c].replace("|", "/") for c in shown) + " |")
    (out_dir / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return [out_dir / "index.csv", out_dir / "index.md"]
