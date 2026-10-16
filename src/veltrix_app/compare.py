"""
Lap comparison, without any window: several laps of one session laid over
each other, and the time gained or lost against a reference lap.

Laps can be aligned two ways, and the mode is part of every result so a chart
can always say which it is showing:

  distance   x is metres from the start/finish line. The same x is the same
             place on the track for every lap — the mode for "where".
  time       x is seconds since the lap began. The same x is a different
             place on each lap — the mode for "when".

Each lap's samples are the channel's own, unresampled; in distance mode the
position of each sample is read from the session's lap-distance channel at
that sample's timestamp. Delta time is always worked out on distance (time
lost only means something at the same place) and, in time mode, plotted
against the reference lap's clock.
"""

from __future__ import annotations

import numpy as np

from veltrix_engine.display import decimate
from veltrix_engine.model import SessionModel

ALIGN = ("distance", "time")
DELTA_STEP_M = 2.0


class LapComparison:
    def __init__(self, model: SessionModel, laps: list | None = None, align: str = "distance"):
        if not model.has("lap_distance") or len(model.laps) < 2:
            raise ValueError("Lap comparison needs laps with a lap distance; this session has no lap information.")
        self.model = model
        self._dist = model.get("lap_distance")
        self._by_label = {lap.lap: lap for lap in model.laps}
        self.align = "distance"
        self.set_align(align)
        self.laps: list = []
        self.set_laps(laps if laps else self.default_laps())

    # ── which laps ────────────────────────────────────────────────────────────

    def usable(self) -> list:
        """Laps that ran line to line, in session order."""
        return [lap for lap in self.model.laps if lap.start_crossed and lap.end_crossed]

    def best(self):
        flying = [lap for lap in self.model.laps if lap.status == "flying"] or self.usable()
        return min(flying, key=lambda lap: lap.time_s).lap if flying else None

    def previous(self, label):
        """The complete lap before this one, or None."""
        labels = [lap.lap for lap in self.usable()]
        i = labels.index(label) if label in labels else 0
        return labels[i - 1] if i > 0 else None

    def default_laps(self) -> list:
        """The best lap as reference, against the next best flying lap."""
        best = self.best()
        others = sorted((lap for lap in self.model.laps if lap.status == "flying" and lap.lap != best),
                        key=lambda lap: lap.time_s)
        return [x for x in (best, others[0].lap if others else self.previous(best)) if x is not None]

    def set_laps(self, laps: list) -> list:
        """The laps to overlay; the first is the reference. Laps the session
        does not have, or that did not run line to line, are dropped."""
        ok = {lap.lap for lap in self.usable()}
        self.laps = [x for x in dict.fromkeys(laps) if x in ok]
        return self.laps

    def set_align(self, align: str) -> None:
        if align not in ALIGN:
            raise ValueError(f"Laps are aligned by {' or '.join(ALIGN)}, not '{align}'.")
        self.align = align

    @property
    def reference(self):
        return self.laps[0] if self.laps else None

    @property
    def x_label(self) -> str:
        return "Distance from the line (m)" if self.align == "distance" else "Time into the lap (s)"

    # ── one lap ───────────────────────────────────────────────────────────────

    def _lap_distance(self, label) -> tuple[np.ndarray, np.ndarray]:
        """(timestamps, metres from the line) of a lap; distance never runs backwards."""
        lap = self._by_label[label]
        t, d = self._dist.timestamps, np.asarray(self._dist.values, float)
        a, b = np.searchsorted(t, [lap.t_start, lap.t_end])
        ok = np.isfinite(d[a:b])
        return t[a:b][ok], np.maximum.accumulate(d[a:b][ok])

    def session_time(self, label, x) -> np.ndarray:
        """Session time at which a lap was at position x on the chart's axis."""
        lap = self._by_label[label]
        x = np.atleast_1d(np.asarray(x, float))
        if self.align == "time":
            return np.where((x >= 0) & (x <= lap.time_s), lap.t_start + x, np.nan)
        t, d = self._lap_distance(label)
        if len(d) < 2:
            return np.full(len(x), np.nan)
        return np.where((x >= d[0]) & (x <= d[-1]), np.interp(x, d, t), np.nan)

    def extent(self) -> tuple[float, float]:
        if not self.laps:
            return 0.0, 1.0
        if self.align == "time":
            return 0.0, float(max(self._by_label[x].time_s for x in self.laps))
        return 0.0, float(max(self._lap_distance(x)[1][-1] for x in self.laps))

    def trace(self, channel: str, label, width_px: int | None = None, x0: float | None = None,
              x1: float | None = None) -> dict | None:
        """A channel over one lap on the comparison axis: the channel's own
        samples, decimated for drawing when a width is given."""
        if not self.model.has(channel):
            return None
        c, lap = self.model.get(channel), self._by_label[label]
        a, b = np.searchsorted(c.timestamps, [lap.t_start, lap.t_end])
        ts = c.timestamps[a:b]
        values = np.where(c.validity[a:b], np.asarray(c.values[a:b], float), np.nan)
        if self.align == "time":
            x = ts - lap.t_start
        else:
            t, d = self._lap_distance(label)
            x = np.interp(ts, t, d) if len(d) >= 2 else np.full(len(ts), np.nan)
        info = {"samples": int(len(x)), "points": int(len(x)), "decimated": False}
        if width_px and len(x):
            x, values, info = decimate(x, values, x0, x1, width_px)
        return {"x": x, "v": values, "unit": c.unit, "discrete": c.discrete, "label": c.label, "lap": label,
                "align": self.align, **info}

    # ── against the reference ─────────────────────────────────────────────────

    def delta(self, label, step_m: float = DELTA_STEP_M) -> dict | None:
        """Time lost (+) or gained (−) by a lap against the reference, as it
        builds up round the lap. Compared place for place; x is on the
        comparison axis."""
        if self.reference is None or label == self.reference:
            return None
        (tr, dr), (tl, dl) = self._lap_distance(self.reference), self._lap_distance(label)
        if len(dr) < 2 or len(dl) < 2:
            return None
        grid = np.arange(max(dr[0], dl[0]), min(dr[-1], dl[-1]), step_m)
        ref_clock = np.interp(grid, dr, tr) - self._by_label[self.reference].t_start
        lap_clock = np.interp(grid, dl, tl) - self._by_label[label].t_start
        return {"x": grid if self.align == "distance" else ref_clock, "dt": lap_clock - ref_clock, "lap": label,
                "reference": self.reference, "align": self.align, "distance_m": grid,
                "final_s": float(self._by_label[label].time_s - self._by_label[self.reference].time_s)}

    def readout(self, x: float, channels: list) -> list:
        """Each channel's value on each lap at position x, from full-resolution
        data: [{"channel", "lap", "value", "unit", "how", "difference"}].
        `difference` is the lap's value minus the reference lap's."""
        rows = []
        for channel in channels:
            if not self.model.has(channel):
                continue
            c = self.model.get(channel)
            base = None
            for label in self.laps:
                t = self.session_time(label, x)[0]
                value, exact = c.at([t]) if np.isfinite(t) else (np.array([np.nan]), np.array([False]))
                found = bool(np.isfinite(value[0]))
                v = float(value[0]) if found else None
                if label == self.reference:
                    base = v
                rows.append({"channel": c.id, "lap": label, "value": v, "unit": c.unit,
                             "how": "none" if not found else "sample" if exact[0] else "held" if c.discrete else "interpolated",
                             "difference": None if v is None or base is None or label == self.reference or c.discrete
                             else v - base})
        return rows

    def to_dict(self) -> dict:
        return {"align": self.align, "laps": list(self.laps)}
