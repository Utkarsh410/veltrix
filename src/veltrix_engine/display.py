"""
Display decimation: what to draw when a channel has far more samples than the
chart has pixels.

`decimate` keeps, for every pixel column of the visible range, the lowest and
the highest sample that falls in it, at their own timestamps and in the order
they occur. A spike one sample wide is therefore still on screen at any zoom,
which plain every-Nth thinning would lose. When the visible range holds no
more samples than the chart can show, the samples themselves are returned —
zooming in always ends at the real data.

This is for drawing only. Readouts, calculations and exports use the
channel's full-resolution samples (`Channel.at`), never the decimated ones.
"""

from __future__ import annotations

import numpy as np


def decimate(t: np.ndarray, v: np.ndarray, t0: float | None = None, t1: float | None = None,
             width_px: int = 1000) -> tuple[np.ndarray, np.ndarray, dict]:
    """The part of (t, v) between t0 and t1 reduced for a chart `width_px` wide.

    Returns (t_out, v_out, info). At most two points per pixel column: the
    column's minimum and maximum. A column whose samples are all invalid (NaN)
    gives one NaN point, so the line breaks there instead of bridging the gap.
    info: {"samples": visible samples, "points": points returned,
    "decimated": whether anything was left out}."""
    t = np.asarray(t, float)
    v = np.asarray(v, float)
    if len(t) == 0:
        return t, v, {"samples": 0, "points": 0, "decimated": False}
    t0 = float(t[0]) if t0 is None else float(t0)
    t1 = float(t[-1]) if t1 is None else float(t1)
    # one sample either side of the window, so the line runs to the chart's edges
    a = max(int(np.searchsorted(t, t0, side="left")) - 1, 0)
    b = min(int(np.searchsorted(t, t1, side="right")) + 1, len(t))
    tt, vv = t[a:b], v[a:b]
    n = len(tt)
    width_px = max(int(width_px), 1)
    if n <= 2 * width_px or t1 <= t0:
        return tt, vv, {"samples": n, "points": n, "decimated": False}

    column = np.clip(((tt - t0) / (t1 - t0) * width_px).astype(np.int64), 0, width_px - 1)
    starts = np.flatnonzero(np.r_[True, column[1:] != column[:-1]])       # first sample of each occupied column
    finite = np.isfinite(vv)
    lo = np.minimum.reduceat(np.where(finite, vv, np.inf), starts)
    hi = np.maximum.reduceat(np.where(finite, vv, -np.inf), starts)
    which = np.repeat(np.arange(len(starts)), np.diff(np.r_[starts, n]))  # the column each sample belongs to
    index = np.arange(n)
    first = lambda hit: np.minimum.reduceat(np.where(hit, index, n), starts)
    i_lo, i_hi = first(finite & (vv == lo[which])), first(finite & (vv == hi[which]))
    empty = i_lo >= n                                                    # a column with no valid sample
    i_lo, i_hi = np.where(empty, starts, i_lo), np.where(empty, starts, i_hi)
    pick = np.unique(np.concatenate([np.minimum(i_lo, i_hi), np.maximum(i_lo, i_hi)]))
    return tt[pick], vv[pick], {"samples": n, "points": int(len(pick)), "decimated": True}
