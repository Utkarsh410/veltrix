"""
Importers turn a file into a RawTable. One module per format, no shared state.

`read_file` is the single way in: it works out what a file is from its content
(falling back on its extension), reads it with the right module and returns a
RawTable — the same thing whatever the format was.

  CSV, TSV, TXT        csv_importer   any logger's CSV export, incl. AiM RaceStudio and MoTeC i2
  MoTeC .ld (+ .ldx)   motec          also Assetto Corsa / ACC telemetry
  iRacing .ibt         iracing
  VBOX .vbo            vbox
  AiM .xrk .xrz .drk   aim            needs libxrk
  MDF .mf4 .mdf .dat   mdf            needs asammdf; decoded channels and raw CAN bus logging
  MATLAB .mat          mat            v7.3 needs h5py
  CAN .asc .log .trc   can            decoded with a DBC; .blf needs python-can
  DBC .dbc             dbc            the database a CAN log is decoded with
"""

from __future__ import annotations

import gzip
import hashlib
import importlib.util
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import aim, can, dbc, iracing, mat, mdf, motec, vbox
from .csv_importer import parse_csv_text, read_csv
from .dbc import Dbc, read_dbc
from .table import RawTable, Signal, signals_to_table
from ..errors import FileFormatError, VeltrixError
from .mdf import ImportCancelled

log = logging.getLogger("veltrix.import")

__all__ = ["inspect_file", "FORMATS", "Dbc", "RawTable", "Signal", "detect_format", "parse_csv_text", "read_csv", "read_dbc",
           "read_file", "signals_to_table", "supported_extensions", "supported_formats"]


@dataclass(frozen=True)
class Format:
    key: str
    name: str
    extensions: tuple
    needs: str = ""           # optional package the reader depends on
    extra: str = ""           # pip extra that installs it
    remark: str = ""


FORMATS = (
    Format("csv", "CSV", (".csv", ".tsv", ".txt"), remark="any delimiter; logger header blocks, units rows and "
           "beacon markers; AiM RaceStudio, MoTeC i2, VBOX, iRacing and Assetto Corsa CSV exports"),
    Format("ld", "MoTeC", (".ld",), remark="lap beacons from the .ldx beside it"),
    Format("ld", "Assetto Corsa", (".ld",), remark="ACC writes MoTeC .ld / .ldx itself, Assetto Corsa through "
           "ACTI; CSV exports from telemetry apps are read as CSV"),
    Format("ibt", "iRacing", (".ibt",)),
    Format("vbo", "VBOX", (".vbo",), remark="laps from the start/finish line stored in the file"),
    Format("xrk", "AiM", (".xrk", ".xrz", ".drk"), "libxrk", "aim", "laps from the logger's lap table"),
    Format("mdf", "MDF / MF4", (".mf4", ".mdf", ".dat"), "asammdf", "mdf", "decoded channels, and raw CAN "
           "bus logging with a DBC"),
    Format("mat", "MAT", (".mat",), remark="per-channel structs, Simulink logging or equal-length vectors; "
           "v7.3 needs h5py"),
    Format("can", "CAN", (".asc", ".log", ".trc", ".csv"), remark="Vector ASCII, candump, PEAK trace and CSV "
           "frame logs, decoded with a DBC"),
    Format("blf", "CAN (Vector BLF)", (".blf",), "can", "can", "decoded with a DBC"),
    Format("dbc", "DBC", (".dbc",), remark="given with a CAN log, or kept beside it"),
)
_CAN_KINDS = ("asc", "candump", "trc", "can_csv", "blf")
_BY_EXTENSION = {".ld": "ld", ".ibt": "ibt", ".vbo": "vbo", ".xrk": "xrk", ".xrz": "xrk", ".drk": "xrk",
                 ".mf4": "mdf", ".mdf": "mdf", ".mat": "mat", ".asc": "asc", ".trc": "trc", ".blf": "blf",
                 ".dbc": "dbc", ".ldx": "ldx"}
_CANDUMP_LINE = re.compile(r"^\(\d+\.\d+\)\s+\S+\s+[0-9A-Fa-f]+#", re.M)


def supported_extensions() -> list:
    """Every file extension read_file accepts, for file pickers and upload filters."""
    return sorted({e for f in FORMATS for e in f.extensions} | {".gz", ".ldx"})


def supported_formats() -> list:
    """One row per accepted format: name, extensions, whether its reader is
    installed, and how to install it when it is not."""
    rows = []
    for f in FORMATS:
        ready = not f.needs or importlib.util.find_spec(f.needs) is not None
        rows.append({"format": f.name, "extensions": " ".join(f.extensions), "ready": ready,
                     "install": "" if ready else f"pip install 'veltrix-engine[{f.extra}]'", "remark": f.remark})
    return rows


def detect_format(name: str, head: bytes) -> str:
    """What a file is, from its first bytes and, failing that, its extension."""
    suffix = Path(name).suffix.lower()
    if mdf.looks_like_mdf(head):
        return "mdf"
    if mat.looks_like_mat(head):
        return "mat"
    if head[:4] == b"LOGG":
        return "blf"
    if aim.looks_like_xrk(head):
        return "xrk"
    if suffix != ".ld" and iracing.looks_like_ibt(head):
        return "ibt"
    if suffix in _BY_EXTENSION and suffix not in (".asc", ".trc"):
        return _BY_EXTENSION[suffix]
    if suffix == "" and motec.looks_like_ld(head) and b"\0" in head[8:64]:
        return "ld"
    text = head.decode("utf-8-sig", errors="ignore")
    lower = text.lower()
    if "[data]" in lower or ("[header]" in lower and "[column names]" in lower):
        return "vbo"
    if text.lstrip().startswith(";$FILEVERSION") or (suffix == ".trc" and ";" in text[:200]):
        return "trc"
    if suffix == ".asc" or re.search(r"^\s*(date\s|base\s+(hex|dec)|Begin Triggerblock)", text[:4000], re.M | re.I):
        return "asc"
    if _CANDUMP_LINE.search(text[:4000]):
        return "candump"
    if re.search(r'^(VERSION\s+"|BO_\s+\d+\s+\w+\s*:)', text, re.M) and "BU_" in text:
        return "dbc"
    if can.looks_like_can_csv(text[:8000]):
        return "can_csv"
    if b"\0" in head[:4096]:
        raise ValueError(f"{name}: not a format this engine reads. Accepted: "
                         + ", ".join(dict.fromkeys(f.name for f in FORMATS)) + ".")
    return "csv"


def _resolve_dbc(given, path: Path):
    """The DBC to decode with: the one given, else the .dbc files beside the log."""
    if isinstance(given, Dbc):
        return given, []
    if given:
        return read_dbc(given), []
    beside = path.with_suffix("").with_suffix(".dbc") if path.suffix == ".gz" else path.with_suffix(".dbc")
    found = [beside] if beside.exists() else sorted(path.parent.glob("*.dbc"))
    if not found:
        return None, []
    return read_dbc(found), [("info", "Decoded with the DBC beside the log: " + ", ".join(p.name for p in found) + ".")]


def _label_csv(table: RawTable) -> None:
    """Name the logger a CSV export came from, and settle two AiM habits."""
    declared = str(table.info.get("Format", "")).lower()
    if "aim" in declared:
        table.format = "AiM CSV"
        for target, keys in (("Driver", ("Racer", "User")), ("Log Date", ("Date",)), ("Log Time", ("Time",))):
            for k in keys:
                if k in table.info and target not in table.info:
                    table.info[target] = table.info[k]
        # RaceStudio can put a row of channel numbers between the units and the data
        first = table.data.columns[0]
        col = table.data[first]
        if len(col) > 3 and col.dtype.kind in "if" and col.iloc[0] > col.iloc[1] <= col.iloc[2]:
            table.data = table.data.iloc[1:].reset_index(drop=True)
            table.notes.append(("info", "The first row under the units was not a sample (its time is out of order) and was dropped."))
    elif "motec" in declared:
        table.format = "MoTeC CSV"


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def inspect_file(path: str | Path) -> dict:
    """Import diagnostics for a file: what it is and what is in it. An MDF
    file is described from its structure alone, without reading samples; any
    other format is read first. Keys: format, version, channel_count,
    group_count, rates_hz, duration_s, missing_units, unsupported, channels."""
    path = Path(path)
    with open(path, "rb") as f:
        head = f.read(65536)
    if path.suffix.lower() != ".gz" and detect_format(path.name, head) == "mdf":
        return {"file": path.name, **mdf.inspect_mdf(path)}
    table = read_file(path)
    native = table.signals or {}
    time_col = next((c for c in table.data.columns if str(c).lower() in ("time", "sessiontime")), None)
    channels, rates = [], set()
    for col in table.data.columns:
        sig = native.get(col)
        t = sig.t if sig is not None else (table.data[time_col].to_numpy(float) if time_col is not None else None)
        rate = None
        if t is not None and len(t) > 1:
            dt = np.diff(t)
            dt = dt[dt > 0]
            rate = round(float(1 / np.median(dt)), 4) if len(dt) else None
        if rate:
            rates.add(rate)
        channels.append({"name": str(col), "unit": table.units.get(col, ""), "group": 0, "index": len(channels),
                         "samples": int(len(sig.y) if sig is not None else len(table.data)), "rate_hz": rate, "comment": ""})
    t_all = table.data[time_col].to_numpy(float) if time_col is not None else None
    return {"file": path.name, "format": table.format, "version": "", "groups": [], "channels": channels,
            "channel_count": len(channels), "group_count": max(len(rates), 1), "rates_hz": sorted(rates),
            "duration_s": float(np.nanmax(t_all) - np.nanmin(t_all)) if t_all is not None and len(t_all) else None,
            "start_time": None, "missing_units": [c["name"] for c in channels if not c["unit"]],
            "raw_can_frames": 0, "unsupported": [m for level, m in table.notes if level == "warning"]}


def read_file(path: str | Path, dbc=None, channels=None, progress=None, cancel=None) -> RawTable:
    """Read a telemetry file of any supported format into a RawTable.

    dbc: for CAN logs and MDF bus logging, the DBC file (or files) to decode
         with. Left out, the .dbc files in the log's folder are used.
    channels, progress, cancel: for MDF files, which are read lazily — the
         channel names to load (default all), a progress(done, total)
         callback, and a cancel() callback that stops the import when it
         returns True. Other formats are read whole and ignore them.

    A file that cannot be read raises FileFormatError naming the file and what
    was wrong; whatever an importer tripped over on a damaged file is turned
    into that, with the details in the log. A missing file raises OSError and a
    cancelled MDF import ImportCancelled, as before."""
    started = time.perf_counter()
    try:
        table = _read_file(path, dbc, channels, progress, cancel)
    except (VeltrixError, OSError, ImportCancelled, KeyboardInterrupt):
        raise
    except ValueError as e:
        log.info("could not read %s: %s", path, e)
        text = str(e)
        raise FileFormatError(text if text.startswith(Path(path).name) else f"{Path(path).name}: {text}") from e
    except Exception as e:                    # an untrusted file can trip any parser in any way
        log.exception("reading %s failed", path)
        raise FileFormatError(f"{Path(path).name} could not be read: it looks damaged, or is a version of its "
                              f"format this release does not know ({type(e).__name__}: {e}).") from e
    log.info("read %s as %s in %.2f s", path, table.format, time.perf_counter() - started)
    return table


def _read_file(path, dbc=None, channels=None, progress=None, cancel=None) -> RawTable:
    path = Path(path)
    with open(path, "rb") as f:
        head = f.read(65536)
    if path.suffix.lower() != ".gz" and head and detect_format(path.name, head) == "mdf":
        db, extra_notes = _resolve_dbc(dbc, path)
        table = mdf.parse_mdf(path, db, channels, progress, cancel)      # samples stay on disk until asked for
        if db is not None and any("CAN frames" in m for _, m in table.notes):
            table.info.setdefault("DBC", db.source)
            table.notes[:0] = extra_notes
        table.source, table.sha256 = path.name, _sha256_of(path)
        return table
    zipped = path.suffix.lower() == ".gz"
    name = path.stem if zipped else path.name
    if zipped:
        with gzip.open(path, "rb") as f:
            head = f.read(65536)
    if not head:
        raise FileFormatError(f"{path.name} is empty.")
    kind = detect_format(name, head)
    if kind == "csv":                         # parsed straight from the file: never held in memory as text
        table = read_csv(path)
        _label_csv(table)
        return table
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    if zipped:
        raw = gzip.decompress(raw)

    if kind == "ldx":
        raise ValueError(f"{path.name} holds only the lap markers of a MoTeC log. Import the .ld file that goes "
                         "with it; the .ldx is picked up from the same folder.")
    if kind == "dbc":
        db = read_dbc(path)
        raise ValueError(f"{path.name} is a CAN database ({len(db.messages)} messages, {db.signal_count} signals), "
                         "not a recording. Import the CAN log; this file is used to decode it when it sits in "
                         "the same folder or is passed as dbc=.")

    extra_notes = []
    if kind in _CAN_KINDS or kind == "mdf":
        db, extra_notes = _resolve_dbc(dbc, path)
    if kind in _CAN_KINDS:
        if db is None:
            raise ValueError(f"{path.name} is a CAN bus log ({can.FORMAT_NAMES[kind]}): frames, not channels. It needs "
                             "a DBC to be decoded — pass dbc=..., use --dbc, or put the .dbc file in the same folder.")
        table = can.read_can(raw, kind, db)
    elif kind == "mdf":
        table = mdf.parse_mdf(raw, db)
    elif kind == "ld":
        ldx = next((p for p in (path.with_suffix(".ldx"), path.with_suffix(".LDX")) if p.exists()), None)
        table = motec.parse_ld(raw, ldx.read_bytes() if ldx else None)
    elif kind == "ibt":
        table = iracing.parse_ibt(raw)
    elif kind == "vbo":
        table = vbox.parse_vbo(raw.decode("utf-8-sig", errors="ignore"))
    elif kind == "xrk":
        table = aim.parse_xrk(raw)
    elif kind == "mat":
        table = mat.parse_mat(raw)
    else:
        table = parse_csv_text(raw.decode("utf-8-sig", errors="ignore"))
        _label_csv(table)
    if (kind in _CAN_KINDS or kind == "mdf") and db is not None and any("CAN frames" in m for _, m in table.notes):
        table.info.setdefault("DBC", db.source)
        table.notes[:0] = extra_notes
    table.source, table.sha256 = path.name, sha
    return table
