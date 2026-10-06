"""
CSV importer.

Reads flat CSVs and the CSV exports written by logger analysis software: a
metadata block above the channel header, a units row and blank lines below it,
comma / semicolon / tab / pipe delimiters, and decimal points or decimal commas.
Gzip-compressed files (.gz) are read directly.

The importer only reads. It never renames, converts or drops channels — that is
the job of mapping and normalisation — so the raw table can be kept as the
immutable record of what the file contained.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

SEPARATORS = [",", ";", "\t", "|"]


@dataclass
class RawTable:
    """A telemetry file as read: source columns, untouched."""
    data: pd.DataFrame
    units: dict = field(default_factory=dict)        # {column: unit} from the units row
    info: dict = field(default_factory=dict)         # header metadata (venue, driver, ...)
    beacons: np.ndarray | None = None                # lap-marker times [s]
    sep: str = ","
    decimal: str = "."
    bad_cells: dict = field(default_factory=dict)    # {column: cells that weren't numbers}
    skipped_rows: int = 0
    source: str = ""
    sha256: str = ""


def read_csv(path: str | Path) -> RawTable:
    """Read a telemetry CSV (optionally .gz) into a RawTable."""
    path = Path(path)
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    if path.suffix == ".gz":
        raw = gzip.decompress(raw)
    table = parse_csv_text(raw.decode("utf-8-sig", errors="ignore"))
    table.source, table.sha256 = path.name, sha
    return table


def parse_csv_text(text: str) -> RawTable:
    lines = text.splitlines()
    if not lines:
        raise ValueError("The file is empty.")
    sep = _detect_sep(lines)
    header_row, data_row = _find_header_and_data(lines, sep) or (0, 1)
    decimal = _detect_decimal(lines[data_row:data_row + 200], sep)

    # Keep just the header and the data — this drops the metadata block above
    # the header, and the units row and blank lines between the two.
    data_lines = [line for line in lines[data_row:] if line.strip()]
    body = "\n".join([lines[header_row]] + data_lines)
    try:
        df = pd.read_csv(io.StringIO(body), sep=sep, decimal=decimal)
    except (pd.errors.ParserError, ValueError):
        df = pd.read_csv(io.StringIO(body), sep=sep, decimal=decimal, on_bad_lines="skip")
    skipped = len(data_lines) - len(df)

    units = {c: u for c, u in zip(df.columns, _units_row(lines, header_row, data_row, sep) or []) if u}
    info, beacons = _header_fields(lines[:header_row], sep, decimal)

    df = df.dropna(axis=1, how="all").dropna(axis=0, how="all").reset_index(drop=True)
    bad_cells = {}
    for c in df.columns:
        if pd.api.types.is_numeric_dtype(df[c]):
            continue
        cell_text = df[c].astype("string").str.strip()
        if decimal == ",":
            cell_text = cell_text.str.replace(",", ".", regex=False)
        num = pd.to_numeric(cell_text, errors="coerce").astype(float)
        present, parsed = int(df[c].notna().sum()), int(num.notna().sum())
        # A numeric channel with a few unreadable cells stays numeric: the bad
        # cells become NaN and get reported. Genuinely text channels are left.
        if present and parsed >= 0.95 * present:
            df[c] = num
            if parsed < present:
                bad_cells[c] = present - parsed
    return RawTable(df, units, info, beacons, sep, decimal, bad_cells, max(skipped, 0))


def _detect_sep(lines):
    """The delimiter that gives a real header-plus-data layout. Candidates are
    tried from most to least frequent, so the decimal commas in a semicolon- or
    tab-separated file can't win just by being common."""
    sample = "\n".join(lines[:200])
    ranked = sorted(SEPARATORS, key=lambda s: -sample.count(s))
    for sep in ranked:
        if sample.count(sep) and _find_header_and_data(lines, sep):
            return sep
    return ranked[0]


def _detect_decimal(lines, sep):
    if sep == ",":
        return "."
    tokens = [t for line in lines for t in _split_row(line, sep)]
    commas = sum(bool(re.fullmatch(r"-?\d+,\d+", t)) for t in tokens)
    points = sum(bool(re.fullmatch(r"-?\d+\.\d+", t)) for t in tokens)
    return "," if commas > points else "."


def _find_header_and_data(lines, sep):
    """Return (header_row, first_data_row) line indices, or None.

    The first data row is the first mostly-numeric line that has a header
    above it: a fully named, mostly-text line with the same field count.
    Requiring that match skips numeric metadata rows such as beacon markers."""
    for d, line in enumerate(lines[:200]):
        values = [t for t in _split_row(line, sep) if t]
        if len(values) < 3 or sum(map(_is_number, values)) < 0.5 * len(values):
            continue
        n_fields = len(_split_row(line, sep))
        for h in range(d):
            fields = _split_row(lines[h], sep)
            names = [t for t in fields if t]
            if (len(fields) == n_fields and len(names) >= 0.8 * n_fields
                    and sum(map(_is_number, names)) <= 0.2 * len(names)):
                return h, d
    return None


def _units_row(lines, header_row, data_row, sep):
    """The units row between the channel header and the data, if there is one."""
    n = len(_split_row(lines[header_row], sep))
    for line in lines[header_row + 1:data_row]:
        fields = _split_row(line, sep)
        filled = [f for f in fields if f]
        if len(fields) <= n and len(filled) >= 0.3 * n and not any(map(_is_number, filled)):
            return fields
    return None


def _header_fields(lines, sep, decimal):
    """Metadata above the channel header: {name: value} pairs, plus beacon
    (lap-marker) times. Some exports put every beacon time in one field
    separated by spaces; others give one time per field — both are read."""
    info, beacons = {}, None
    for line in lines:
        fields = _split_row(line, sep)
        if fields and fields[0].lower() == "beacon markers":
            values = [v for f in fields[1:] for v in f.split() if _is_number(v)]
            beacons = np.array([float(v.replace(",", ".")) if decimal == "," else float(v) for v in values])
            continue
        i = 0
        while i < len(fields) - 1:
            if fields[i] and not _is_number(fields[i]) and fields[i + 1]:
                info[fields[i]] = fields[i + 1]
                i += 2
            else:
                i += 1
    return info, beacons


def _split_row(line, sep):
    return [t.strip().strip('"').strip() for t in line.split(sep)]


def _is_number(token):
    try:
        float(token.strip().replace(",", "."))
        return True
    except (ValueError, AttributeError):
        return False
