"""
The importer interface: a format the engine does not read is added as a
module of its own, not as a change to the engine.

An importer is an `Importer`: a name, the extensions it takes, a `read`
function that turns a file into a RawTable and, when its extension is one the
engine already reads, a `sniff` function that recognises its files by their
first bytes. It is registered with `register(importer)`, or — for an importer
in its own package — through the `veltrix.importers` entry point group, which
the engine loads on first use:

    [project.entry-points."veltrix.importers"]
    mylogger = "veltrix_mylogger:IMPORTER"

The entry point names an Importer, a list of them, or a function returning
either. `read_file` then reads the format like any built-in one: it detects it,
calls `read`, checks the table against the contract below, records the file's
name and SHA-256 and the importer that read it, and turns anything the
importer raises into a FileFormatError naming the file.

The contract a returned RawTable must keep (`check_table`):
  - `data` is a DataFrame with at least one row and one column, its column
    names unique, non-empty strings, one row per sample;
  - a time column in seconds named "Time" (or one `hints` names as "time"),
    when the file has a clock;
  - `units`, `hints` and `signals` name only columns of `data`; `hints` keys
    are channel ids of the schema (CHANNEL_SCHEMA.md);
  - `beacons`, when given, is an array of lap-marker times in seconds;
  - nothing the engine fills itself: `source` and `sha256` are overwritten.
Everything else (`info`, `notes`, `vehicle`, `setup`) is optional and passed
through. `importers/testkit.py` checks an importer against all of this.

Importers are code: they are loaded from installed packages only, never from
a data folder. Setting VELTRIX_NO_PLUGINS=1 leaves every plug-in out, to tell a
plug-in's problem from the engine's.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from ..errors import FileFormatError, VersionError
from .table import RawTable

log = logging.getLogger("veltrix.import")

API_VERSION = 1                       # the interface this release offers; an importer states the one it was written for
ENTRY_POINT_GROUP = "veltrix.importers"
BUILT_IN_KEYS = {"csv", "ld", "ibt", "vbo", "xrk", "mdf", "mat", "can", "blf", "dbc", "ldx",
                 "asc", "candump", "trc", "can_csv"}
_KEY = re.compile(r"^[a-z][a-z0-9_]{1,31}$")


@dataclass(frozen=True)
class Importer:
    """One file format, read by code outside the engine."""
    key: str                                           # short unique id: lower case, digits, _ ("mylogger")
    name: str                                          # what users see ("MyLogger")
    extensions: tuple                                  # (".mlg",) — lower case, with the dot
    read: Callable[[Path], RawTable]                   # the file → a RawTable
    sniff: Callable[[str, bytes], bool] | None = None  # (file name, first 64 KB) → is this one of mine?
    version: str = ""                                  # the importer's own version, recorded with what it reads
    api: int = API_VERSION                             # the interface version it was written for
    remark: str = ""                                   # one line for `veltrix formats`


_registered: dict = {}                # key -> Importer
_failed: dict = {}                    # entry point name -> why it did not load
_loaded = False


def validate(importer: Importer, built_in_extensions=()) -> None:
    """Raise ValueError (VersionError for the interface version) if the importer cannot be registered."""
    if not isinstance(importer, Importer):
        raise ValueError(f"not an Importer: {importer!r}")
    if not isinstance(importer.api, int) or importer.api > API_VERSION or importer.api < 1:
        raise VersionError(f"importer '{importer.key}' is written for importer interface {importer.api}; "
                           f"this release offers 1 to {API_VERSION}.")
    if not _KEY.match(str(importer.key)):
        raise ValueError(f"importer key '{importer.key}': use 2 to 32 lower-case letters, digits or _, "
                         "starting with a letter.")
    if importer.key in BUILT_IN_KEYS:
        raise ValueError(f"importer key '{importer.key}' is a built-in format's.")
    if not str(importer.name).strip():
        raise ValueError(f"importer '{importer.key}' has no name.")
    if not callable(importer.read):
        raise ValueError(f"importer '{importer.key}': read is not callable.")
    if importer.sniff is not None and not callable(importer.sniff):
        raise ValueError(f"importer '{importer.key}': sniff is not callable.")
    exts = tuple(importer.extensions)
    if not exts or not all(isinstance(e, str) and re.fullmatch(r"\.[a-z0-9_]{1,12}", e) for e in exts):
        raise ValueError(f"importer '{importer.key}': extensions must be lower case with the dot, e.g. ('.mlg',).")
    clash = sorted(set(exts) & set(built_in_extensions))
    if clash and importer.sniff is None:
        raise ValueError(f"importer '{importer.key}' takes {', '.join(clash)}, which the engine already reads: "
                         "it needs a sniff function that recognises its own files.")


def register(importer: Importer, replace: bool = False) -> Importer:
    """Add an importer. replace: allow a second registration under the same key (a newer version)."""
    validate(importer, _built_in_extensions())
    if importer.key in _registered and not replace and _registered[importer.key] is not importer:
        raise ValueError(f"an importer '{importer.key}' is already registered.")
    _registered[importer.key] = importer
    log.info("importer registered: %s %s (%s)", importer.key, importer.version, ", ".join(importer.extensions))
    return importer


def unregister(key: str) -> None:
    _registered.pop(key, None)


def _built_in_extensions() -> set:
    from . import FORMATS
    return {e for f in FORMATS for e in f.extensions} | {".gz", ".ldx"}


def load_entry_points(force: bool = False) -> dict:
    """Register the importers installed packages declare. Once per process
    unless forced. A plug-in that fails to load is left out and its reason kept
    (`failed()`); it never stops the built-in formats from working."""
    global _loaded
    if _loaded and not force:
        return dict(_failed)
    _loaded = True
    if os.environ.get("VELTRIX_NO_PLUGINS"):
        return {}
    from importlib.metadata import entry_points
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        try:
            found = ep.load()
            found = found() if callable(found) and not isinstance(found, Importer) else found
            for importer in (found if isinstance(found, (list, tuple)) else [found]):
                register(importer, replace=True)
            _failed.pop(ep.name, None)
        except Exception as e:                       # a broken plug-in is reported, never fatal
            _failed[ep.name] = f"{type(e).__name__}: {e}"
            log.warning("importer plug-in %s (%s) not loaded: %s", ep.name, ep.value, e)
    return dict(_failed)


def registered() -> list:
    load_entry_points()
    return list(_registered.values())


def failed() -> dict:
    """{entry point name: why it did not load}."""
    load_entry_points()
    return dict(_failed)


def get(key: str) -> Importer:
    return _registered[key]


def claim(name: str, head: bytes) -> Importer | None:
    """The registered importer that claims this file, if any: one whose sniff
    says yes, or — without a sniff — one whose extension it is. A sniff that
    raises is taken as a no."""
    suffix = Path(name).suffix.lower()
    for importer in registered():
        if importer.sniff is not None:
            try:
                if importer.sniff(name, head):
                    return importer
            except Exception as e:
                log.warning("importer %s: sniff failed on %s: %s", importer.key, name, e)
        elif suffix in importer.extensions:
            return importer
    return None


def check_table(table, importer_name: str = "the importer") -> list:
    """Every way a table breaks the contract (module docstring). Empty: it keeps it."""
    if not isinstance(table, RawTable):
        return [f"{importer_name} returned {type(table).__name__}, not a RawTable"]
    out = []
    df = table.data
    if not isinstance(df, pd.DataFrame):
        return [f"data is {type(df).__name__}, not a DataFrame"]
    if df.shape[0] < 1 or df.shape[1] < 1:
        out.append(f"data is empty ({df.shape[0]} rows × {df.shape[1]} columns)")
    names = list(df.columns)
    if not all(isinstance(c, str) and c.strip() for c in names):
        out.append("column names must be non-empty strings")
    if len(set(names)) != len(names):
        out.append("column names are not unique: " + ", ".join(sorted({c for c in names if names.count(c) > 1})))
    if not isinstance(table.units, dict) or any(c not in df.columns for c in table.units):
        out.append("units name columns that are not in data")
    from ..schema import CHANNELS
    if not isinstance(table.hints, dict):
        out.append("hints is not a dict")
    else:
        out += [f"hints: '{cid}' is not a channel id of the schema" for cid in table.hints if cid not in CHANNELS]
        out += [f"hints: column '{col}' is not in data" for col in table.hints.values() if col not in df.columns]
    if table.signals is not None and (not isinstance(table.signals, dict)
                                      or any(c not in df.columns for c in table.signals)):
        out.append("signals name columns that are not in data")
    if table.beacons is not None:
        b = np.asarray(table.beacons)
        if b.ndim != 1 or b.dtype.kind not in "if":
            out.append("beacons must be a one-dimensional array of times in seconds")
    time_col = table.hints.get("time") if isinstance(table.hints, dict) else None
    time_col = time_col or ("Time" if "Time" in df.columns else None)
    if time_col is not None and time_col in df.columns:
        t = pd.to_numeric(df[time_col], errors="coerce").to_numpy(float)
        if len(t) and not np.isfinite(t).any():
            out.append(f"the time column '{time_col}' holds no numbers")
    for name in ("info", "vehicle", "setup"):
        if not isinstance(getattr(table, name), dict):
            out.append(f"{name} is not a dict")
    if not isinstance(table.notes, list) or any(not (isinstance(n, (tuple, list)) and len(n) == 2) for n in table.notes):
        out.append("notes must be a list of (level, message)")
    return out


def read_with(importer: Importer, path: Path) -> RawTable:
    """Run a plug-in importer and hold its table to the contract."""
    table = importer.read(path)
    problems = check_table(table, f"the {importer.name} importer")
    if problems:
        raise FileFormatError(f"{path.name}: the {importer.name} importer returned a table the engine cannot use: "
                              + "; ".join(problems) + ".")
    if table.format in ("", "CSV"):
        table.format = importer.name
    table.notes.append(("info", f"Read by the {importer.name} importer"
                                + (f" {importer.version}" if importer.version else "") + " (a plug-in)."))
    return table
