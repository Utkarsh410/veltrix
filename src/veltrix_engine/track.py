"""Track map: the car's path in a flat local plane."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .vehicle import G


def track_xy(session, lap=None) -> pd.DataFrame:
    """Path of one lap (default: the whole session).

    Columns: time, lap_distance, x, y [m]. `attrs["source"]` says where the
    path came from:
      - "position": logged position (pos_x / pos_y, or projected GPS)
      - "reconstructed": integrated from speed and lateral acceleration
        (heading rate = ay·g / V). Good for a picture of the lap; it drifts,
        so it does not close exactly and must not be used for timing.
    """
    frame = session.frame()
    if lap is not None:
        frame = frame[frame["lap"] == lap]
    out = pd.DataFrame({"time": frame["time"].to_numpy(),
                        "lap_distance": frame["lap_distance"].to_numpy()})
    if {"pos_x", "pos_y"} <= set(frame.columns):
        out["x"], out["y"] = frame["pos_x"].to_numpy(), frame["pos_y"].to_numpy()
        out.attrs["source"] = "position"
        return out
    if not {"speed", "acc_lat"} <= set(frame.columns):
        raise ValueError("A track map needs position channels, or speed and lateral acceleration.")
    dt = 1.0 / session.rate_hz
    v = frame["speed"].to_numpy(float) / 3.6
    ay = frame["acc_lat"].to_numpy(float)
    with np.errstate(divide="ignore", invalid="ignore"):
        yaw_rate = np.where(v > 5.0, ay * G / v, 0.0)
    heading = np.cumsum(yaw_rate) * dt
    out["x"] = np.cumsum(v * np.cos(heading)) * dt
    out["y"] = np.cumsum(v * np.sin(heading)) * dt
    out.attrs["source"] = "reconstructed"
    return out
