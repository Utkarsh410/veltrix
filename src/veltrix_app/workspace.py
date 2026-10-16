"""
The state of the interactive workspace, without any window.

What charts there are and which channels each shows, where the cursor is, what
is in view — and from those, what to draw and what to read out. The Qt window
(`veltrix_app.window`) only displays this and forwards input to it, so the
behaviour is tested without a screen and a saved workspace is just this state
as JSON.

Two rules it keeps:
  - what is drawn is decimated for the screen; what is read out is the
    channel's own full-resolution samples, looked up by the channel's policy,
    and a value that is not on a sample is marked as interpolated or held;
  - a saved workspace that names channels a session does not have is still
    opened: the charts stay, and the missing channels are listed.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from veltrix_engine.display import decimate
from veltrix_engine.model import SessionModel

WORKSPACE_VERSION = 1


@dataclass
class Chart:
    channels: list = field(default_factory=list)      # channel ids ("speed") or "layer:id"
    title: str = ""


@dataclass
class Readout:
    channel: str
    unit: str
    value: float | None        # None: no value here (outside the channel's samples, or in a gap)
    how: str                   # "sample" | "interpolated" | "held" | "none"
    label: str                 # measured | corrected/reconstructed | calculated


class Workspace:
    def __init__(self, model: SessionModel | None = None):
        self.model = model
        self.charts: list = []
        self.cursor: float | None = None
        self.view: tuple | None = None            # (t0, t1) shown on every chart; None = everything
        self.name = "Untitled"
        self.compare: dict = {}                   # lap comparison settings: {"align", "laps"} (see compare.py)
        self.selection: tuple | None = None       # (t0, t1) the user has marked for statistics; None = no range

    # ── session ───────────────────────────────────────────────────────────────

    def set_model(self, model: SessionModel) -> list:
        """Show another session with the same layout. Returns the channels the
        layout names that this session does not have."""
        self.model = model
        lo, hi = self.extent()
        self.view = None
        self.cursor = lo if self.cursor is None else float(np.clip(self.cursor, lo, hi))
        return self.missing()

    def extent(self) -> tuple[float, float]:
        """First and last timestamp of any channel in the session."""
        chans = [c for c in self.model.channels() if c.layer != "raw" and len(c.timestamps)] if self.model else []
        if not chans:
            return 0.0, 1.0
        return float(min(c.timestamps[0] for c in chans)), float(max(c.timestamps[-1] for c in chans))

    def missing(self) -> list:
        if self.model is None:
            return []
        return sorted({ch for chart in self.charts for ch in chart.channels if not self.model.has(ch)})

    # ── layout ────────────────────────────────────────────────────────────────

    def add_chart(self, channels=(), title: str = "", at: int | None = None) -> int:
        chart = Chart(list(channels), title)
        index = len(self.charts) if at is None else max(0, min(at, len(self.charts)))
        self.charts.insert(index, chart)
        return index

    def remove_chart(self, index: int) -> None:
        del self.charts[index]

    def add_channel(self, index: int, channel: str) -> bool:
        """Add a channel to a chart. False if it is already there."""
        if channel in self.charts[index].channels:
            return False
        self.charts[index].channels.append(channel)
        return True

    def remove_channel(self, index: int, channel: str) -> None:
        self.charts[index].channels.remove(channel)

    # ── view and cursor ───────────────────────────────────────────────────────

    def window(self) -> tuple[float, float]:
        return self.view if self.view else self.extent()

    def zoom(self, t0: float, t1: float) -> tuple[float, float]:
        """Show t0…t1, kept inside the session and at least a millisecond wide."""
        lo, hi = self.extent()
        t0, t1 = sorted((float(t0), float(t1)))
        t0, t1 = max(t0, lo), min(t1, hi)
        if t1 - t0 < 1e-3:
            t1 = min(t0 + 1e-3, hi)
            t0 = t1 - 1e-3
        self.view = (t0, t1)
        return self.view

    def pan(self, seconds: float) -> tuple[float, float]:
        lo, hi = self.extent()
        t0, t1 = self.window()
        shift = float(np.clip(seconds, lo - t0, hi - t1))
        self.view = (t0 + shift, t1 + shift)
        return self.view

    def reset_view(self) -> None:
        self.view = None

    def set_cursor(self, t: float) -> float:
        lo, hi = self.extent()
        self.cursor = float(np.clip(t, lo, hi))
        return self.cursor

    # ── what to draw, what to read ────────────────────────────────────────────

    def series(self, channel: str, width_px: int) -> dict | None:
        """The channel's samples in view, decimated for a chart `width_px` wide.
        None if the session does not have the channel."""
        if self.model is None or not self.model.has(channel):
            return None
        c = self.model.get(channel)
        t0, t1 = self.window()
        values = np.where(c.validity, np.asarray(c.values, float), np.nan)
        t, v, info = decimate(c.timestamps, values, t0, t1, width_px)
        return {"t": t, "v": v, "unit": c.unit, "discrete": c.discrete, "label": c.label, "name": c.name, **info}

    def readout(self, t: float | None = None) -> list:
        """The value of every charted channel at time t (default: the cursor),
        from full-resolution data, with how each value was obtained."""
        t = self.cursor if t is None else t
        out = []
        if self.model is None or t is None:
            return out
        for chart in self.charts:
            row = []
            for name in chart.channels:
                if not self.model.has(name):
                    row.append(Readout(name, "", None, "none", ""))
                    continue
                c = self.model.get(name)
                value, exact = c.at([t])
                found = bool(np.isfinite(value[0]))
                how = "none" if not found else "sample" if exact[0] else "held" if c.discrete else "interpolated"
                row.append(Readout(name, c.unit, float(value[0]) if found else None, how, c.label))
            out.append(row)
        return out

    # ── a marked range ────────────────────────────────────────────────────────

    def select(self, t0: float | None, t1: float | None = None) -> tuple | None:
        """Mark t0…t1 for statistics, kept inside the session. select(None) clears it."""
        if t0 is None or t1 is None:
            self.selection = None
            return None
        lo, hi = self.extent()
        a, b = sorted((float(np.clip(t0, lo, hi)), float(np.clip(t1, lo, hi))))
        self.selection = (a, b) if b > a else None
        return self.selection

    def units_of(self, index: int) -> list:
        """The units on one chart, in the order their channels were added: each gets its own axis."""
        seen = []
        for name in self.charts[index].channels:
            if self.model is not None and self.model.has(name):
                unit = self.model.get(name).unit
                if unit not in seen:
                    seen.append(unit)
        return seen

    def range_stats(self) -> list:
        """Every charted channel over the marked range, from its own samples —
        never from what is drawn: [{"channel", "unit", "samples", "min", "max",
        "mean", "std", "start", "end", "change"}]. `start` and `end` are the
        values at the two edges by the channel's lookup policy. A channel with
        no valid sample in the range has samples 0 and no figures."""
        if self.model is None or not self.selection:
            return []
        t0, t1 = self.selection
        rows, done = [], set()
        for chart in self.charts:
            for name in chart.channels:
                if name in done or not self.model.has(name):
                    continue
                done.add(name)
                c = self.model.get(name)
                a, b = np.searchsorted(c.timestamps, [t0, t1], side="left")
                b = int(np.searchsorted(c.timestamps, t1, side="right"))
                v = np.asarray(c.values[a:b], float)[c.validity[a:b]]
                edges = c.at([t0, t1])[0]
                first, last = (float(x) if np.isfinite(x) else None for x in edges)
                row = {"channel": c.id, "unit": c.unit, "samples": int(len(v)), "min": None, "max": None, "mean": None,
                       "std": None, "start": first, "end": last,
                       "change": None if first is None or last is None or c.discrete else last - first}
                if len(v):
                    row.update(min=float(v.min()), max=float(v.max()), mean=float(v.mean()), std=float(v.std()))
                rows.append(row)
        return rows

    def lap_at(self, t: float | None = None):
        """The lap the cursor is in, or None."""
        t = self.cursor if t is None else t
        if self.model is None or t is None:
            return None
        return next((lap for lap in self.model.laps if lap.t_start <= t < lap.t_end), None)

    # ── saving ────────────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {"version": WORKSPACE_VERSION, "name": self.name, "charts": [asdict(c) for c in self.charts],
                "cursor": self.cursor, "view": list(self.view) if self.view else None, "compare": dict(self.compare),
                "selection": list(self.selection) if self.selection else None}

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.write_text(json.dumps(self.to_dict(), indent=1), encoding="utf-8")
        return path

    @classmethod
    def from_dict(cls, data: dict, model: SessionModel | None = None) -> "Workspace":
        if data.get("version", 1) > WORKSPACE_VERSION:
            raise ValueError("This workspace was saved by a newer version of the app.")
        ws = cls(model)
        ws.name = str(data.get("name", "Untitled"))
        ws.charts = [Chart([str(c) for c in ch.get("channels", [])], str(ch.get("title", ""))) for ch in data.get("charts", [])]
        ws.cursor = data.get("cursor")
        ws.compare = dict(data.get("compare") or {})
        chosen = data.get("selection")
        ws.selection = (float(chosen[0]), float(chosen[1])) if chosen else None
        view = data.get("view")
        ws.view = (float(view[0]), float(view[1])) if view else None
        if model is not None:
            lo, hi = ws.extent()
            if ws.cursor is not None:
                ws.cursor = float(np.clip(ws.cursor, lo, hi))
            if ws.view and (ws.view[1] <= lo or ws.view[0] >= hi):
                ws.view = None                     # the saved range lies outside this session
        return ws

    @classmethod
    def load(cls, path: str | Path, model: SessionModel | None = None) -> "Workspace":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")), model)


def default_layout(model: SessionModel) -> Workspace:
    """A first layout for a session: speed, the pedals, steering, and the
    accelerations — whichever of them the session has."""
    ws = Workspace(model)
    for group in (["speed"], ["throttle", "brake"], ["steering"], ["acc_lat", "acc_long"], ["rpm", "gear"]):
        have = [c for c in group if model.has(c)]
        if have:
            ws.add_chart(have)
    if not ws.charts:
        first = [c.id for c in model.channels("normalised") if c.id != "time"][:1]
        if first:
            ws.add_chart(first)
    ws.cursor = ws.extent()[0]
    return ws
