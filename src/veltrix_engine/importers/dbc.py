"""
DBC: the description of what the frames on a CAN bus mean.

`read_dbc` reads the messages and signals of a .dbc file; `decode` turns logged
frames into one Signal per DBC signal. Handles Intel and Motorola byte order,
signed, IEEE float and double signals, simple multiplexing, CAN FD payloads and
29-bit identifiers, with a J1939 fallback that matches on the PGN when the
source address in the log differs from the one in the DBC.

Not handled: extended multiplexing (SG_MUL_VAL_) — those signals are decoded as
if they were always present — and value tables, which are kept as numbers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .table import Signal

EXT_FLAG = 0x80000000
ID_MASK = 0x1FFFFFFF


@dataclass
class DbcSignal:
    name: str
    start: int
    length: int
    little_endian: bool
    signed: bool
    factor: float
    offset: float
    unit: str = ""
    mux: str = ""                 # "" | "M" (the multiplexor) | "m<N>" (present when the multiplexor reads N)
    float_type: int = 0           # 0 integer, 1 IEEE float, 2 IEEE double
    has_values: bool = False      # has a value table (an enumeration)


@dataclass
class DbcMessage:
    id: int                       # without the extended flag
    extended: bool
    name: str
    dlc: int
    signals: list = field(default_factory=list)


@dataclass
class CanFrames:
    """Frames from a bus log. `data` is zero-padded to the longest payload."""
    t: np.ndarray                 # seconds
    id: np.ndarray                # uint32, without flags
    extended: np.ndarray          # bool
    dlc: np.ndarray               # payload bytes actually present
    data: np.ndarray              # uint8, n × longest payload


@dataclass
class Dbc:
    messages: dict = field(default_factory=dict)      # {(id, extended): DbcMessage}
    source: str = ""

    def merge(self, other: "Dbc") -> None:
        for key, msg in other.messages.items():
            self.messages.setdefault(key, msg)
        self.source = ", ".join(s for s in (self.source, other.source) if s)

    @property
    def signal_count(self) -> int:
        return sum(len(m.signals) for m in self.messages.values())


_BO = re.compile(r"^BO_\s+(\d+)\s+(\w+)\s*:\s*(\d+)")
_SG = re.compile(r"^\s*SG_\s+(\w+)\s*(M|m\d+M?)?\s*:\s*(\d+)\|(\d+)@([01])([+-])\s*"
                 r"\(\s*([^,\s]+)\s*,\s*([^)\s]+)\s*\)\s*\[[^\]]*\]\s*\"([^\"]*)\"")
_VALTYPE = re.compile(r"^SIG_VALTYPE_\s+(\d+)\s+(\w+)\s*:\s*(\d)")
_VAL = re.compile(r"^VAL_\s+(\d+)\s+(\w+)\s")


def parse_dbc(text: str, source: str = "") -> Dbc:
    db, current, by_raw_id = Dbc(source=source), None, {}
    for line in text.splitlines():
        if line.startswith("BO_ "):
            m = _BO.match(line)
            current = None
            if m:
                raw_id = int(m.group(1))
                current = DbcMessage(raw_id & ID_MASK, bool(raw_id & EXT_FLAG), m.group(2), int(m.group(3)))
                if current.name != "VECTOR__INDEPENDENT_SIG_MSG":
                    db.messages[(current.id, current.extended)] = current
                    by_raw_id[raw_id] = current
            continue
        m = _SG.match(line) if current is not None and "SG_ " in line[:12] else None
        if m:
            name, mux, start, length, order, sign, factor, offset, unit = m.groups()
            current.signals.append(DbcSignal(name, int(start), int(length), order == "1", sign == "-",
                                             float(factor), float(offset), unit, mux or ""))
            continue
        if line.startswith("SIG_VALTYPE_"):
            m = _VALTYPE.match(line)
            msg = by_raw_id.get(int(m.group(1))) if m else None
            for sig in (msg.signals if msg else ()):
                if sig.name == m.group(2):
                    sig.float_type = int(m.group(3))
        elif line.startswith("VAL_ "):
            m = _VAL.match(line)
            msg = by_raw_id.get(int(m.group(1))) if m else None
            for sig in (msg.signals if msg else ()):
                if sig.name == m.group(2):
                    sig.has_values = True
    if not db.messages:
        raise ValueError(f"{source or 'DBC'}: no messages (BO_ lines) found — is this a DBC file?")
    return db


def read_dbc(paths) -> Dbc:
    """Read one DBC file or several; the first definition of an identifier wins."""
    if isinstance(paths, (str, Path)):
        paths = [paths]
    db = None
    for p in paths:
        p = Path(p)
        raw = p.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("cp1252", errors="replace")
        one = parse_dbc(text, p.name)
        if db is None:
            db = one
        else:
            db.merge(one)
    if db is None:
        raise ValueError("No DBC file given.")
    return db


def _j1939_key(can_id: np.ndarray | int):
    """The identifier with priority and source address removed, and the
    destination address too for point-to-point (PDU1) messages."""
    pf = (can_id >> 16) & 0xFF
    return np.where(pf < 240, can_id & 0x03FF0000, can_id & 0x03FFFF00)


def _raw_values(data: np.ndarray, sig: DbcSignal):
    """Unsigned raw value of a signal in every row, and the last byte it needs."""
    if sig.little_endian:
        b0, off = sig.start // 8, sig.start % 8
        b1 = (sig.start + sig.length - 1) // 8
        shifts = [(b, 8 * (b - b0) - off) for b in range(b0, b1 + 1)]
    else:
        p = 8 * (sig.start // 8) + (7 - sig.start % 8)        # position counted from the frame's first bit
        end = p + sig.length - 1
        b0, b1, tail = p // 8, end // 8, 7 - end % 8
        shifts = [(b, 8 * (b1 - b) - tail) for b in range(b0, b1 + 1)]
    if b1 >= data.shape[1]:
        return None, b1
    raw = np.zeros(len(data), np.uint64)
    for b, shift in shifts:
        if shift >= 64:
            continue
        byte = data[:, b].astype(np.uint64)
        raw |= (byte << np.uint64(shift)) if shift >= 0 else (byte >> np.uint64(-shift))
    if sig.length < 64:
        raw &= np.uint64((1 << sig.length) - 1)
    return raw, b1


def _physical(raw: np.ndarray, sig: DbcSignal) -> np.ndarray:
    if sig.float_type == 1 and sig.length == 32:
        x = raw.astype(np.uint32).view(np.float32).astype(float)
    elif sig.float_type == 2 and sig.length == 64:
        x = raw.view(np.float64)
    elif sig.signed:
        x = raw.astype(np.int64)
        if sig.length < 64:
            x = np.where(x >= (1 << (sig.length - 1)), x - (1 << sig.length), x)
        x = x.astype(float)
    else:
        x = raw.astype(float)
    return x * sig.factor + sig.offset


def decode(frames: CanFrames, db: Dbc) -> tuple[list, list]:
    """Decode frames with a DBC. Returns (signals, notes)."""
    notes, out = [], []
    order = np.lexsort((frames.t, frames.extended, frames.id))
    ids, ext = frames.id[order], frames.extended[order]
    cuts = np.flatnonzero(np.r_[True, (ids[1:] != ids[:-1]) | (ext[1:] != ext[:-1]), True])
    j1939 = {}
    for (mid, mext), msg in db.messages.items():
        if mext:
            j1939.setdefault(int(_j1939_key(mid)), msg)

    unknown, by_pgn, n_decoded = [], 0, 0
    for a, b in zip(cuts[:-1], cuts[1:]):
        can_id, is_ext = int(ids[a]), bool(ext[a])
        msg = db.messages.get((can_id, is_ext))
        if msg is None and is_ext:
            msg = j1939.get(int(_j1939_key(can_id)))
            by_pgn += msg is not None
        if msg is None:
            unknown.append(f"{can_id:X}{'x' if is_ext else ''}")
            continue
        rows = order[a:b]
        t, data, dlc = frames.t[rows], frames.data[rows], frames.dlc[rows]
        n_decoded += len(rows)
        mux_raw = None
        for sig in msg.signals:
            if sig.mux.endswith("M"):
                mux_raw, _ = _raw_values(data, sig)
                break
        for sig in msg.signals:
            raw, last = _raw_values(data, sig)
            if raw is None:
                continue
            keep = dlc > last
            if sig.mux.startswith("m") and mux_raw is not None:
                keep &= mux_raw == np.uint64(int(sig.mux[1:].rstrip("M")))
            if keep.sum() < 2:
                continue
            y = _physical(raw[keep], sig)
            out.append((msg.name, Signal(sig.name, t[keep], y, sig.unit, True if sig.has_values else None)))

    counts = {}
    for _, s in out:
        counts[s.name] = counts.get(s.name, 0) + 1
    for msg_name, s in out:
        if counts[s.name] > 1:
            s.name = f"{msg_name}.{s.name}"
    signals = [s for _, s in out]

    n_ids = len(cuts) - 1
    notes.append(("info", f"{len(frames.t):,} CAN frames on {n_ids} identifiers; {n_ids - len(unknown)} of them are "
                          f"in the DBC ({db.source}) and gave {len(signals)} signals."))
    if by_pgn:
        notes.append(("info", f"{by_pgn} identifier(s) were matched to the DBC by J1939 PGN, ignoring source address."))
    if unknown:
        shown = ", ".join(unknown[:12]) + (" …" if len(unknown) > 12 else "")
        notes.append(("info" if signals else "warning",
                      f"{len(unknown)} identifier(s) are not in the DBC and were not decoded: {shown}."))
    if not signals:
        raise ValueError(f"None of the {n_ids} identifiers in the log are in the DBC ({db.source}). "
                         f"Identifiers logged: {', '.join(unknown[:12])}. Is this the right DBC for this bus?")
    return signals, notes


def describe(db: Dbc) -> str:
    """A DBC as text: every message with its signals."""
    lines = [f"{len(db.messages)} messages, {db.signal_count} signals"]
    for (mid, mext), msg in sorted(db.messages.items()):
        lines.append(f"  0x{mid:X}{'x' if mext else ''}  {msg.name}  ({msg.dlc} bytes, {len(msg.signals)} signals)")
        for s in msg.signals:
            order = "Intel" if s.little_endian else "Motorola"
            mux = f" [{s.mux}]" if s.mux else ""
            lines.append(f"      {s.name:<32} {s.start}|{s.length} {order}{mux}  ×{s.factor:g} {s.offset:+g}  {s.unit}")
    return "\n".join(lines)
