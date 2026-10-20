"""
Track map: the shape of the lap in a flat plane, on the same distance grid as
the lap traces, so any point on the map is a point on every chart.

Two sources:

  position        logged position (pos_x / pos_y, or GPS projected to metres):
                  the median path over the flying laps.
  reconstructed   no position in the file: the path is integrated from speed
                  and lateral acceleration (curvature = ay·g / V²), using the
                  median over the flying laps. Integration drifts, so the lap
                  is then closed: a lap must turn through exactly one full
                  revolution and end where it began. The curvature is scaled
                  to make the revolution exact and the remaining gap is spread
                  evenly along the lap.

A reconstructed map shows the circuit's shape and the order and direction of
its corners. It is a drawing aid, not a survey: distances between far-apart
parts of the track are approximate, and it is never used for timing.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import corners as corner_engine
from .derived import best_lat
from .vehicle import G

STEP_M = 5.0
SMOOTH_M = 15.0               # curvature smoothing before integration
MAX_TURN_ERROR = 0.35         # how far from a whole revolution the raw path may be and still be closed


def _orient(x, y):
    """Rotate so the layout is wider than tall and the start/finish line is at
    the bottom. Rotation only — never mirrored, so left stays left."""
    x, y = x - x.mean(), y - y.mean()
    cov = np.cov(np.vstack([x, y]))
    vals, vecs = np.linalg.eigh(cov)
    major = vecs[:, np.argmax(vals)]
    angle = -np.arctan2(major[1], major[0])
    c, s = np.cos(angle), np.sin(angle)
    x, y = c * x - s * y, s * x + c * y
    if y[0] > 0:                              # start line in the upper half: turn the map round
        x, y = -x, -y
    return x, y


def track_map(session, step: float = STEP_M, laps=None) -> pd.DataFrame:
    """The lap's shape on a distance grid.

    Columns: dist, x, y (metres; y up; orientation is for display only).
    attrs: source ("position" / "reconstructed"), closed (the path was closed
    onto itself), gap_m (how far apart the two ends were before closing),
    turn_scale (factor applied to the curvature to make one exact revolution),
    direction ("clockwise" / "anticlockwise"), laps (the laps it was built from).
    """
    if laps is None:
        table = session.laps
        pool = table[table["status"] == "flying"].sort_values("lap_time_s")["lap"]
        laps = list(pool.iloc[:8]) or corner_engine._usable_laps(session)
    if not laps:
        raise ValueError("No complete laps to draw a track map from.")
    grid, frames = corner_engine._lap_frames(session, laps, step)
    first = next(iter(frames.values()))
    out = pd.DataFrame({"dist": grid})

    if {"pos_x", "pos_y"} <= set(first.columns):
        x = np.nanmedian([f["pos_x"].to_numpy() for f in frames.values()], axis=0)
        y = np.nanmedian([f["pos_y"].to_numpy() for f in frames.values()], axis=0)
        # Drawn as logged. If the plane is mirrored (some simulators), import with
        # flip=("pos_y",): the import warns when position and steering disagree.
        heading = np.unwrap(np.arctan2(np.gradient(y), np.gradient(x)))
        turn = float(heading[-1] - heading[0])
        gap = float(np.hypot(x[-1] - x[0], y[-1] - y[0]))
        out["x"], out["y"] = _orient(x, y)
        out.attrs.update(source="position", closed=True, gap_m=gap, turn_scale=1.0,
                         direction="anticlockwise" if turn > 0 else "clockwise", laps=list(laps))
        return out

    lat_id = best_lat(first)
    usable = lat_id is not None and not ("acc_lat" in session.coarse and "acc_lat" not in session.rebuilt)
    if not usable:
        raise ValueError("A track map needs position channels, or speed and a usable lateral g channel.")
    v = np.nanmedian([f["speed"].to_numpy() for f in frames.values()], axis=0) / 3.6
    ay = np.nanmedian([f[lat_id].to_numpy() for f in frames.values()], axis=0)
    curvature = ay * G / np.maximum(v, 5.0) ** 2
    k = max(int(round(SMOOTH_M / step)), 1)
    curvature = np.convolve(curvature, np.ones(k) / k, mode="same")

    def integrate(kappa):
        heading = np.cumsum(kappa) * step
        heading = np.r_[0.0, heading[:-1]]
        return np.cumsum(np.cos(heading)) * step, np.cumsum(np.sin(heading)) * step

    total = float(np.sum(curvature) * step)
    turns = int(np.round(total / (2 * np.pi)))
    x, y = integrate(curvature)
    raw_gap = float(np.hypot(x[-1] - x[0], y[-1] - y[0]))
    closed, scale = False, 1.0
    if turns != 0 and abs(total - 2 * np.pi * turns) <= MAX_TURN_ERROR * 2 * np.pi:
        scale = 2 * np.pi * turns / total
        x, y = integrate(curvature * scale)
        # spread what is left of the gap evenly along the lap, so the ends meet
        ramp = np.arange(len(x)) / (len(x) - 1)
        end_x = x[-1] + (x[1] - x[0])                 # where the next step would land
        end_y = y[-1] + (y[1] - y[0])
        x = x - ramp * (end_x - x[0])
        y = y - ramp * (end_y - y[0])
        closed = True
    out["x"], out["y"] = _orient(x, y)
    out.attrs.update(source="reconstructed", closed=closed, gap_m=raw_gap, turn_scale=float(scale),
                     direction="anticlockwise" if total > 0 else "clockwise", laps=list(laps))
    return out


def track_xy(session, lap=None) -> pd.DataFrame:
    """Path of one lap as logged or integrated, without closing it.
    Kept for exports; `track_map` is the one to draw."""
    frame = session.frame()
    frame = session.frame(["lap", "time", "lap_distance", "pos_x", "pos_y", "speed", best_lat(frame)])
    if lap is not None:
        frame = frame[frame["lap"] == lap]
    out = pd.DataFrame({"time": frame["time"].to_numpy(),
                        "lap_distance": frame["lap_distance"].to_numpy()})
    if {"pos_x", "pos_y"} <= set(frame.columns):
        out["x"], out["y"] = frame["pos_x"].to_numpy(), frame["pos_y"].to_numpy()
        out.attrs["source"] = "position"
        return out
    lat_id = best_lat(frame)
    if "speed" not in frame or lat_id is None:
        raise ValueError("A track map needs position channels, or speed and lateral acceleration.")
    dt = 1.0 / session.rate_hz
    v = frame["speed"].to_numpy(float) / 3.6
    ay = frame[lat_id].to_numpy(float)
    with np.errstate(divide="ignore", invalid="ignore"):
        yaw_rate = np.where(v > 5.0, ay * G / v, 0.0)
    heading = np.cumsum(yaw_rate) * dt
    out["x"] = np.cumsum(v * np.cos(heading)) * dt
    out["y"] = np.cumsum(v * np.sin(heading)) * dt
    out.attrs["source"] = "reconstructed"
    return out
