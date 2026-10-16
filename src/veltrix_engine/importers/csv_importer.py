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
from pathlib import Path

import numpy as np
import pandas as pd

from .table import RawTable

SEPARATORS = [",", ";", "\t", "|"]


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def read_csv(path: str | Path) -> RawTable:
    """Read a telemetry CSV (optionally .gz) into a RawTable.

    Only the start of the file is turned into text, to work out its layout;
    the rows are then parsed straight from the file. A 260 MB export is never
    held in memory as text, which used to cost more than the table itself."""
    path = Path(path)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as f:
        head = f.read(HEAD_BYTES)
        more = bool(f.read(1))
    text = head.decode("utf-8-sig", errors="ignore")
    table = None
    if "\r" not in text.replace("\r\n", ""):            # old-Mac line endings need the whole text; nothing else does
        lines = text.replace("\r\n", "\n").split("\n")
        if more:
            lines = lines[:-1]
        if lines:
            layout = _layout(lines)
            try:
                with opener(path, "rb") as f:
                    df = pd.read_csv(f, skiprows=layout["data_row"], encoding="utf-8", encoding_errors="ignore",
                                     **_parse_options(layout))
                table = _finish(df, lines, layout, 0)
            except (pd.errors.ParserError, ValueError, UnicodeError):
                table = None                              # malformed rows: the slower path below counts and skips them
    if table is None:
        with opener(path, "rb") as f:
            table = parse_csv_text(f.read().decode("utf-8-sig", errors="ignore"))
    table.source, table.sha256 = path.name, _sha256_of(path)
    return table


HEAD_BYTES = 400_000      # the layout is worked out from the start of the file only


def _layout(lines: list) -> dict:
    """Where the table is in a file, from its first lines: delimiter, header row, first data row, decimal mark, names."""
    sep = _detect_sep(lines)
    header_row, data_row = _find_header_and_data(lines, sep) or (0, 1)
    return {"sep": sep, "header_row": header_row, "data_row": data_row,
            "decimal": _detect_decimal(lines[data_row:data_row + 200], sep),
            "names": _unique(_split_row(lines[header_row], sep))}


def _parse_options(layout: dict) -> dict:
    return dict(sep=layout["sep"], decimal=layout["decimal"], header=None, names=layout["names"], skip_blank_lines=True,
                index_col=False)


def parse_csv_text(text: str) -> RawTable:
    if "\r" in text[:HEAD_BYTES]:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text[:HEAD_BYTES].split("\n")
    if not lines:
        raise ValueError("The file is empty.")
    if len(text) > HEAD_BYTES:
        lines = lines[:-1]            # the last line of the head may be cut mid-row
    layout = _layout(lines)

    # Parse from the first data row: this drops the metadata block above the
    # header, and the units row and blank lines between the two.
    offset = _line_offset(text, layout["data_row"])
    kwargs = _parse_options(layout)
    skipped = 0
    try:
        df = pd.read_csv(io.StringIO(text[offset:]), **kwargs)
    except (pd.errors.ParserError, ValueError):
        df = pd.read_csv(io.StringIO(text[offset:]), on_bad_lines="skip", **kwargs)
        skipped = sum(1 for line in text[offset:].splitlines() if line.strip()) - len(df)
    return _finish(df, lines, layout, skipped)


def _finish(df: pd.DataFrame, lines: list, layout: dict, skipped: int) -> RawTable:
    """Units, header metadata and cell checks for a parsed table."""
    sep, decimal, names = layout["sep"], layout["decimal"], layout["names"]
    header_row, data_row = layout["header_row"], layout["data_row"]
    units_row = _units_row(lines, header_row, data_row, sep) or []
    units = {c: u for c, u in zip(names, units_row) if u and set(u) != {"."}}
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


def _line_offset(text: str, n_lines: int) -> int:
    """Character offset of the start of line `n_lines` (0-based)."""
    pos = 0
    for _ in range(n_lines):
        pos = text.index("\n", pos) + 1
    return pos


def _unique(names):
    """Column names made unique and non-empty, keeping the first of each as is."""
    seen, out = {}, []
    for i, name in enumerate(names):
        name = name or f"column_{i + 1}"
        k = seen.get(name, 0)
        seen[name] = k + 1
        out.append(name if k == 0 else f"{name}.{k}")
    return out


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
