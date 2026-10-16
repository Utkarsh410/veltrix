"""
What every importer returns, and the one step the multi-rate formats share.

A RawTable is a telemetry file as read: source channel names, source units, one
row per sample. CSV-like files are already a table. Loggers that record each
channel at its own rate (MoTeC, AiM, MDF, CAN) are read as a list of Signals
and `signals_to_table` lays them on one time base, so the rest of the engine
sees the same thing whatever the file was.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

TIME_COLUMN = "Time"
MAX_BASE_HZ = 1000.0          # no table is built faster than this
MIN_BASE_HZ = 20.0            # ... or slowed below this to save memory
MAX_CELLS = 60_000_000        # rows × columns a table may hold (about 500 MB)
STEP_MAX_VALUES = 64          # an integer channel with this few distinct values is step-valued


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
    format: str = "CSV"                              # what the file was read as
    notes: list = field(default_factory=list)        # [(level, message)] from the importer
    hints: dict = field(default_factory=dict)        # {channel id: column} the format itself defines
    vehicle: dict = field(default_factory=dict)      # {VehicleParams field: value} the file states (a setup sheet)
    signals: dict | None = None                      # {column: Signal} on each channel's own clock, for formats
                                                     # that log every channel at its own rate; `data` is then
                                                     # those signals laid on one time base


@dataclass
class Signal:
    """One logged channel on its own clock."""
    name: str
    t: np.ndarray                 # seconds
    y: np.ndarray
    unit: str = ""
    step: bool | None = None      # held between samples, not interpolated; None = decide from the values


def _is_step(y: np.ndarray) -> bool:
    if y.dtype.kind in "biu":
        return True
    finite = y[np.isfinite(y)]
    if not len(finite) or np.any(finite != np.round(finite)):
        return False
    return len(np.unique(finite[:200_000])) <= STEP_MAX_VALUES


def _rate(t: np.ndarray) -> float:
    dt = np.diff(t)
    dt = dt[dt > 0]
    return 1.0 / float(np.median(dt)) if len(dt) else 0.0


LAP_TIMER_NAMES = {"time", "lap time", "laptime", "current lap time", "running lap time", "lap timer", "i lap time"}
LAP_TIMER_SLOPE = 0.05        # a lap timer gains one second a second, to within this share


def lap_timer_crossings(signals: list) -> tuple[np.ndarray, str] | None:
    """Line-crossing times from a lap timer channel, if the file has one.

    A lap timer is recognised by what it does, not only by its name: it must
    be named like one, count up at one second a second, and drop back to
    near zero at least twice. Each crossing is placed where the timer read
    zero — the first sample after the drop, less what the timer already shows
    there. Returns (crossing times on the signals' clock, the channel's name)."""
    for s in signals:
        words = " ".join("".join(ch if ch.isalnum() else " " for ch in s.name.lower()).split())
        if words not in LAP_TIMER_NAMES or len(s.y) < 100:
            continue
        t, y = np.asarray(s.t, float), np.asarray(s.y, float)
        dy, dt = np.diff(y), np.diff(t)
        ok = np.isfinite(dy) & (dt > 0)
        if ok.sum() < 50:
            continue
        rising = ok & (dy > 0)
        if rising.sum() < 0.8 * ok.sum() or abs(np.median(dy[rising] / dt[rising]) - 1.0) > LAP_TIMER_SLOPE:
            continue
        drops = np.flatnonzero(ok & (dy < -1.0) & (y[1:] < 1.0)) + 1          # back to under a second
        if len(drops) < 2:
            continue
        return t[drops] - np.clip(y[drops], 0.0, None), s.name
    return None


def _clean_unit(unit) -> str:
    """A unit as written, or "" for the placeholders loggers write when there is none ("..", "-")."""
    text = (unit or "").strip()
    return "" if not text or set(text) <= set(".-") else text


def signals_to_table(signals: list, fmt: str, info: dict | None = None,
                     beacons: np.ndarray | None = None, hints: dict | None = None,
                     notes: list | None = None, zero_time: bool = True) -> RawTable:
    """Lay signals logged at different rates on one uniform time base.

    The time base runs at the fastest channel's rate. Continuous channels are
    linearly interpolated and left empty outside the span they were logged
    over; step channels (gear, lap number, flags) hold their last value.
    With `zero_time` the clock starts at the first sample of the file."""
    notes = list(notes or [])
    usable, names = [], set()
    for s in signals:
        t, y = np.asarray(s.t, float), np.asarray(s.y)
        if y.dtype.kind not in "biuf" or y.ndim != 1 or len(t) != len(y) or len(y) < 2:
            continue
        ok = np.isfinite(t)
        if not ok.all():
            t, y = t[ok], y[ok]
        if len(t) < 2 or t[-1] <= t[0]:
            continue
        if np.any(np.diff(t) < 0):
            order = np.argsort(t, kind="stable")
            t, y = t[order], y[order]
        name, k = s.name or "channel", 1
        while name in names or name == TIME_COLUMN:
            k += 1
            name = f"{s.name}.{k}"
        names.add(name)
        usable.append(Signal(name, t, y, _clean_unit(s.unit), _is_step(y) if s.step is None else s.step))
    if not usable:
        raise ValueError(f"{fmt}: the file has no numeric channels with more than one sample.")

    # Work from the first sample: clocks that count from 1970 leave too few
    # digits for the sample interval otherwise.
    t0 = min(s.t[0] for s in usable)
    for s in usable:
        s.t = s.t - t0
    origin, t0 = t0, 0.0
    t1 = max(s.t[-1] for s in usable)
    rates = [_rate(s.t) for s in usable]
    base = float(f"{min(max(rates), MAX_BASE_HZ):.4g}")     # 20.0001 Hz is 20 Hz with timestamp rounding
    cap = MAX_CELLS / (max(t1 - t0, 1e-9) * len(usable))
    if base > cap:
        slower = max(cap, MIN_BASE_HZ)
        notes.append(("warning", f"{len(usable)} channels over {t1 - t0:,.0f} s is too much to hold at the "
                                 f"fastest channel's {base:g} Hz — the table was built at {slower:.3g} Hz."))
        base = slower
    n = int(np.floor((t1 - t0) * base + 1e-6)) + 1
    grid = t0 + np.arange(n) / base

    columns = {TIME_COLUMN: grid if zero_time else grid + origin}
    units = {TIME_COLUMN: "s"}
    for s in usable:
        if s.step:
            idx = np.searchsorted(s.t, grid + 1e-6, side="right") - 1      # a microsecond of timestamp rounding
            col = np.where(idx >= 0, s.y[np.clip(idx, 0, None)].astype(float), np.nan)
        else:
            y = s.y.astype(float)
            ok = np.isfinite(y)
            col = np.interp(grid, s.t[ok], y[ok], left=np.nan, right=np.nan) if ok.sum() >= 2 \
                else np.full(n, np.nan)
        columns[s.name] = col
        if s.unit:
            units[s.name] = s.unit
    distinct = sorted({float(f"{r:.3g}") for r in rates if r > 0})
    if len(distinct) > 1:
        notes.append(("info", f"Channels are logged at {distinct[0]:g} to {distinct[-1]:g} Hz; they were laid on "
                              f"one {base:g} Hz time base (linear between samples, flags and counters held)."))
    if beacons is None:                     # no lap markers given: a lap timer channel carries the same information
        timer = lap_timer_crossings(usable)
        if timer is not None:
            crossings, name = timer
            beacons = crossings + (0.0 if zero_time else origin)
            notes.append(("info", f"{len(crossings)} line crossing(s) taken from the lap timer channel '{name}' "
                                  "(it counts up at one second a second and restarts at the line)."))
    elif zero_time:
        beacons = np.asarray(beacons, float) - origin
    shift = 0.0 if zero_time else origin             # the signals' own samples, on the clock of the table
    kept = {s.name: Signal(s.name, s.t + shift, s.y, s.unit, s.step) for s in usable}
    return RawTable(pd.DataFrame(columns), units, dict(info or {}), beacons, source="", format=fmt,
                    notes=notes, hints=dict(hints or {}), signals=kept)
