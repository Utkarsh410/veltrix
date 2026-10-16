"""
CAN bus logs.

A bus log holds frames, not channels: it needs a DBC to say what the bytes
mean. The readers here turn a log into CanFrames; `read_can` decodes them with
a DBC and lays the signals on one time base.

Read natively: Vector ASCII (.asc), candump (.log), PEAK trace (.trc) and CSV
logs with an identifier and data columns (SavvyCAN and similar). Vector binary
(.blf) is read through the optional `python-can` package.
"""

from __future__ import annotations

import io
import re

import numpy as np
import pandas as pd

from . import dbc as dbc_mod
from .dbc import CanFrames
from .table import RawTable, signals_to_table

FD_LENGTHS = (0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64)


def _frames(t, ids, ext, payloads) -> CanFrames:
    if not len(t):
        raise ValueError("No CAN data frames were found in the log.")
    width = max(max(map(len, payloads)), 1)
    dlc = np.fromiter(map(len, payloads), np.int64, len(payloads))
    data = np.frombuffer(b"".join(p.ljust(width, b"\0") for p in payloads), np.uint8).reshape(-1, width)
    return CanFrames(np.asarray(t, float), np.asarray(ids, np.uint32), np.asarray(ext, bool), dlc, data)


# ── candump ───────────────────────────────────────────────────────────────────

_CANDUMP = re.compile(r"^\((\d+\.\d+)\)\s+\S+\s+([0-9A-Fa-f]+)#(#[0-9A-Fa-f])?([0-9A-Fa-f]*)")


def parse_candump(text: str) -> CanFrames:
    t, ids, ext, payloads = [], [], [], []
    for line in text.splitlines():
        m = _CANDUMP.match(line)
        if not m:
            continue                                   # comments, remote frames (123#R)
        t.append(float(m.group(1)))
        ids.append(int(m.group(2), 16))
        ext.append(len(m.group(2)) > 3)
        payloads.append(bytes.fromhex(m.group(4)[:len(m.group(4)) // 2 * 2]))
    return _frames(t, ids, ext, payloads)


# ── Vector ASCII ──────────────────────────────────────────────────────────────

_ASC = re.compile(r"^\s*(\d+\.\d+)\s+\d+\s+([0-9A-Fa-f]+)(x?)\s+(?:Rx|Tx)\s+d\s+([0-9A-Fa-f]+)((?:\s+[0-9A-Fa-f]{1,3})*)")
_ASC_FD = re.compile(r"^\s*(\d+\.\d+)\s+CANFD\s+\d+\s+(?:Rx|Tx)\s+([0-9A-Fa-f]+)(x?)\s+(?:[^\d\s]\S*\s+)?"
                     r"[01]\s+[01]\s+[0-9A-Fa-f]+\s+(\d+)((?:\s+[0-9A-Fa-f]{1,3})*)")


def parse_asc(text: str) -> CanFrames:
    base = 10 if re.search(r"^base\s+dec", text[:2000], re.M | re.I) else 16
    t, ids, ext, payloads = [], [], [], []
    for line in text.splitlines():
        m = _ASC.match(line)
        if m:
            n = int(m.group(4), base)
        else:
            m = _ASC_FD.match(line)
            if not m:
                continue
            n = int(m.group(4))
        values = m.group(5).split()[:n]
        if len(values) < n:
            continue
        t.append(float(m.group(1)))
        ids.append(int(m.group(2), base))
        ext.append(bool(m.group(3)))
        payloads.append(bytes(int(v, base) for v in values))
    return _frames(t, ids, ext, payloads)


# ── PEAK trace ────────────────────────────────────────────────────────────────

_TRC_V1 = re.compile(r"^\s*\d+\)\s+([\d.]+)\s+(?:\d+\s+)?(?:Rx|Tx)\s+([0-9A-Fa-f]+)\s+(?:-\s+)?(\d+)((?:\s+[0-9A-Fa-f]{2})*)\s*$")


def parse_trc(text: str) -> CanFrames:
    """PEAK .trc, file versions 1.x and 2.x. Times in the file are milliseconds."""
    columns = None
    m = re.search(r"^;\$COLUMNS=(\S+)", text, re.M)
    if m:
        columns = m.group(1).split(",")
    t, ids, ext, payloads = [], [], [], []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith(";"):
            continue
        if columns is None:
            m = _TRC_V1.match(line)
            if not m:
                continue
            n = int(m.group(3))
            values = m.group(4).split()[:n]
            ms, id_text = float(m.group(1)), m.group(2)
        else:
            tok = line.split()
            fixed = [c for c in columns if c != "D"]
            if len(tok) < len(fixed):
                continue
            row = dict(zip(fixed, tok))
            if row.get("T", "DT") not in ("DT", "FD", "FB", "FE", "BI"):
                continue                               # status, error and remote frames
            try:
                ms, id_text = float(row["O"]), row["I"]
                n = int(row["L"]) if "L" in row else FD_LENGTHS[int(row["l"], 16) if "l" in row else 0]
            except (KeyError, ValueError, IndexError):
                continue
            values = tok[len(fixed):len(fixed) + n]
        if len(values) < n:
            continue
        t.append(ms / 1000.0)
        ids.append(int(id_text, 16))
        ext.append(len(id_text) > 4)
        payloads.append(bytes(int(v, 16) for v in values))
    return _frames(t, ids, ext, payloads)


# ── CSV logs ──────────────────────────────────────────────────────────────────

_ID_NAMES = ("id", "can id", "canid", "arbitration id", "arbitration_id", "identifier", "msg id", "message id", "arb id")
_TIME_NAMES = ("time", "timestamp", "time stamp", "t", "time s", "timestamps", "time (s)", "abs time", "rel time")
_BYTE = re.compile(r"^(?:d|data|byte|b)[ _]?(\d+)$")


def _norm(name) -> str:
    return str(name).strip().strip('"').lower()


def looks_like_can_csv(head: str) -> bool:
    """Whether the first lines of a CSV are a frame log: an identifier column
    plus the payload, as one data column or one column per byte."""
    for line in head.splitlines()[:30]:
        if line.startswith(("#", ";")):
            continue
        for sep in (",", ";", "\t"):
            names = [_norm(n) for n in line.split(sep)]
            if len(names) >= 3 and any(n in _ID_NAMES for n in names) and (
                    "data" in names or "data bytes" in names or "payload" in names
                    or sum(bool(_BYTE.match(n)) for n in names) >= 2):
                return True
    return False


def parse_can_csv(text: str) -> tuple[CanFrames, list]:
    notes = []
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines[:60]) if looks_like_can_csv(line))
    sep = max((",", ";", "\t"), key=lines[start].count)
    df = pd.read_csv(io.StringIO("\n".join(lines[start:])), sep=sep, dtype=str, index_col=False,
                     on_bad_lines="skip", skipinitialspace=True)
    df.columns = [_norm(c) for c in df.columns]
    id_col = next(c for c in df.columns if c in _ID_NAMES)
    df = df[df[id_col].notna()]
    id_text = df[id_col].str.strip()
    prefixed = id_text.str.lower().str.startswith("0x")
    widths = id_text.str.len()
    hex_ids = bool(prefixed.any() or id_text.str.contains("[A-Fa-f]").any()
                   or (widths.nunique() == 1 and widths.iloc[0] in (3, 8)))
    if not (prefixed.any() or id_text.str.contains("[A-Fa-f]").any()):
        notes.append(("warning", "The identifier column has only digits — read as "
                                 + ("hexadecimal, as its fixed width suggests." if hex_ids else "decimal.")))
    ids = id_text.map(lambda s: int(s, 16 if hex_ids else 10)).to_numpy(np.int64)

    ext_col = next((c for c in df.columns if c in ("extended", "ext", "ide", "is_extended_id", "is extended id")), None)
    if ext_col:
        ext = df[ext_col].str.strip().str.lower().isin(("1", "true", "t", "yes", "x")).to_numpy()
    else:
        ext = ids > 0x7FF
    ids = ids & dbc_mod.ID_MASK

    byte_cols = sorted((c for c in df.columns if _BYTE.match(c)), key=lambda c: int(_BYTE.match(c).group(1)))
    data_col = next((c for c in df.columns if c in ("data", "data bytes", "payload")), None)
    len_col = next((c for c in df.columns if c in ("len", "dlc", "length", "data length")), None)
    if data_col:
        cleaned = df[data_col].fillna("").str.replace(r"0x|[^0-9A-Fa-f]", "", regex=True)
        payloads = [bytes.fromhex(s[:len(s) // 2 * 2]) for s in cleaned]
    else:
        cells = df[byte_cols].fillna("").apply(lambda c: c.str.strip().str.replace("0x", "", regex=False))
        hex_bytes = hex_ids or cells.apply(lambda c: c.str.contains("[A-Fa-f]").any()).any()
        payloads = [bytes(int(v, 16 if hex_bytes else 10) for v in row if v) for row in cells.to_numpy()]
    if len_col:
        n = pd.to_numeric(df[len_col], errors="coerce").fillna(64).astype(int).to_numpy()
        payloads = [p[:k] for p, k in zip(payloads, n)]

    time_col = next((c for c in df.columns if c in _TIME_NAMES), None)
    if time_col is None:
        raise ValueError(f"The CAN log has no time column. Columns: {list(df.columns)}")
    t = pd.to_numeric(df[time_col], errors="coerce").to_numpy(float)
    if np.isnan(t).mean() > 0.5:
        stamp = pd.to_datetime(df[time_col], errors="coerce")
        t = (stamp - stamp.min()).dt.total_seconds().to_numpy(float)
    else:
        span = np.nanmax(t) - np.nanmin(t)
        whole = bool(np.all(t[np.isfinite(t)] == np.round(t[np.isfinite(t)])))
        # a log is minutes to hours long: whole-number stamps spanning millions are µs, thousands ms
        scale, unit = (1e-6, "microseconds") if whole and span > 3e7 else (1e-3, "milliseconds") \
            if whole and span > 3e4 else (1.0, "seconds")
        if scale != 1.0:
            notes.append(("warning", f"The time column holds whole numbers spanning {span:,.0f} — read as {unit}."))
        t = t * scale
    ok = np.isfinite(t)
    payloads = [p for p, k in zip(payloads, ok) if k]
    return _frames(t[ok], ids[ok], ext[ok], payloads), notes


# ── Vector binary ─────────────────────────────────────────────────────────────

def parse_blf(data: bytes) -> CanFrames:
    try:
        import can
    except ImportError:
        raise ValueError("Reading Vector .blf logs needs the python-can package: "
                         "pip install 'veltrix-engine[can]'  (or export the log as .asc).") from None
    t, ids, ext, payloads = [], [], [], []
    for msg in can.BLFReader(io.BytesIO(data)):
        if msg.is_error_frame or msg.is_remote_frame:
            continue
        t.append(msg.timestamp)
        ids.append(msg.arbitration_id)
        ext.append(msg.is_extended_id)
        payloads.append(bytes(msg.data))
    return _frames(t, ids, ext, payloads)


# ── entry point ───────────────────────────────────────────────────────────────

FORMAT_NAMES = {"asc": "Vector ASCII CAN log", "candump": "candump CAN log", "trc": "PEAK CAN trace",
                "can_csv": "CAN log (CSV)", "blf": "Vector BLF CAN log"}


def read_frames(data: bytes, kind: str) -> tuple[CanFrames, list]:
    if kind == "blf":
        return parse_blf(data), []
    text = data.decode("utf-8-sig", errors="ignore")
    if kind == "can_csv":
        return parse_can_csv(text)
    return {"asc": parse_asc, "candump": parse_candump, "trc": parse_trc}[kind](text), []


def frames_to_table(frames: CanFrames, db, fmt: str, notes=()) -> RawTable:
    signals, decode_notes = dbc_mod.decode(frames, db)
    return signals_to_table(signals, fmt, notes=[*notes, *decode_notes])


def read_can(data: bytes, kind: str, db) -> RawTable:
    """Decode a bus log with a DBC into a table of signals."""
    frames, notes = read_frames(data, kind)
    return frames_to_table(frames, db, FORMAT_NAMES[kind], notes)
