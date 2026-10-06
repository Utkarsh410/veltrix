"""Tyre analysis: operating-window classification, per-lap summary,
temperature anomalies and the degradation fit."""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.stats import linregress

from ..schema import CORNERS, tyre_temp_channel

# Operating windows for the representative (mid / single) temperature, °C:
# {compound: {corner: (min, max)}}
COMPOUND_WINDOWS = {
    "SC":   {"fl": (80, 105), "fr": (80, 105), "rl": (78, 103), "rr": (78, 103)},
    "DC":   {"fl": (83, 108), "fr": (83, 108), "rl": (81, 106), "rr": (81, 106)},
    "DHD":  {"fl": (85, 110), "fr": (85, 110), "rl": (83, 108), "rr": (83, 108)},
    "DHD2": {"fl": (87, 113), "fr": (87, 113), "rl": (85, 110), "rr": (85, 110)},
}
DEFAULT_COMPOUND = "DHD"
LOSS_THRESHOLD_S = 0.5     # pace loss per lap vs a fresh tyre used as a marker


def classify(session, windows: dict | None = None) -> pd.DataFrame:
    """Thermal state per sample and corner: -1 cold, 0 in window, +1 hot.
    Adds `car`: hot if any corner is hot, else cold if any is cold."""
    windows = windows or COMPOUND_WINDOWS[DEFAULT_COMPOUND]
    ch = session.channels
    out = pd.DataFrame(index=ch.index)
    for c in CORNERS:
        cid = tyre_temp_channel(ch.columns, c)
        if cid is None:
            continue
        lo, hi = windows[c]
        temp = ch[cid].to_numpy(float)
        out[c] = np.where(temp < lo, -1, np.where(temp > hi, 1, 0))
    if out.shape[1]:
        states = out.to_numpy()
        out["car"] = np.where((states == 1).any(axis=1), 1,
                              np.where((states == -1).any(axis=1), -1, 0))
    return out


def lap_summary(session, windows: dict | None = None) -> pd.DataFrame:
    """One row per lap: lap time, tyre age, mean / max temperature and % of the
    lap in the window per corner, and the car-level cold / in-window / hot split.
    Tyre age counts laps from the first lap in the session."""
    states = classify(session, windows)
    ch = session.channels
    rows = []
    for age, lap in enumerate(session.laps.itertuples()):
        sel = (ch["lap"] == lap.lap).to_numpy()
        rec = {"lap": lap.lap, "lap_time_s": lap.lap_time_s, "status": lap.status, "tyre_age": age}
        for c in CORNERS:
            cid = tyre_temp_channel(ch.columns, c)
            if cid is None:
                continue
            temp = ch.loc[sel, cid]
            rec[f"temp_{c}_mean"], rec[f"temp_{c}_max"] = temp.mean(), temp.max()
            rec[f"in_window_{c}"] = float((states.loc[sel, c] == 0).mean() * 100)
        if "car" in states:
            car = states.loc[sel, "car"]
            rec["in_window_car"] = float((car == 0).mean() * 100)
            rec["cold_car"] = float((car == -1).mean() * 100)
            rec["hot_car"] = float((car == 1).mean() * 100)
        rows.append(rec)
    return pd.DataFrame(rows)


def detect_anomalies(session, delta_threshold_C: float = 8.0, window_s: float = 0.5) -> pd.DataFrame:
    """Events where a tyre temperature moves more than `delta_threshold_C`
    within `window_s` seconds (lock-up, flat spot, blister onset).

    Uses the inner zone when the log has one, otherwise the single sensor. The
    signal is smoothed over 0.1 s first so sensor noise is not flagged, and a
    change that straddles a lap boundary is ignored. One row per event."""
    ch = session.channels
    fs = session.rate_hz
    win = max(int(round(window_s * fs)), 1)
    smooth_n = max(int(round(0.1 * fs)), 1)
    lap = ch["lap"]
    events = []
    for c in CORNERS:
        cid = f"tyre_temp_{c}_inner" if f"tyre_temp_{c}_inner" in ch else tyre_temp_channel(ch.columns, c)
        if cid is None:
            continue
        smooth = ch[cid].rolling(smooth_n, center=True, min_periods=1).mean()
        delta = smooth.diff(win)
        delta[lap != lap.shift(win)] = np.nan
        hit = delta.abs() > delta_threshold_C
        if not hit.any():
            continue
        t_hit = ch.loc[hit, "time"]
        for _, idx in t_hit.groupby((t_hit.diff() > 1.0).cumsum()).groups.items():
            peak = delta.loc[idx].abs().idxmax()
            events.append({"time": float(ch.at[peak, "time"]), "lap": ch.at[peak, "lap"],
                           "corner": c, "channel": cid, "delta_C": float(delta.at[peak]),
                           "temp_C": float(ch.at[peak, cid])})
    cols = ["time", "lap", "corner", "channel", "delta_C", "temp_C"]
    return pd.DataFrame(events, columns=cols).sort_values("time").reset_index(drop=True)


def fit_degradation(session, statuses=("flying", "out-lap", "in-lap", "slow", "whole session"),
                    complete_only: bool = True) -> dict:
    """Linear fit of lap time against tyre age.

    Returns slope [s/lap], intercept, r_squared, the laps used, and
    `threshold_age`: the tyre age at which the fitted loss versus a fresh tyre
    passes 0.5 s per lap. That is a pace marker, not a pit-stop crossover —
    a crossover also needs pit-loss time, fuel effect and traffic."""
    laps = session.laps.reset_index(drop=True)
    age = np.arange(len(laps))
    use = laps["status"].isin(statuses).to_numpy()
    if complete_only:
        complete = (laps["start_crossed"] & laps["end_crossed"]).to_numpy()
        if complete.sum() >= 3:
            use = use & complete
    x, y = age[use].astype(float), laps.loc[use, "lap_time_s"].to_numpy(float)
    if len(x) < 3:
        return {"slope": np.nan, "intercept": np.nan, "r_squared": np.nan,
                "ages": x, "lap_times": y, "threshold_age": None}
    fit = linregress(x, y)
    threshold = next((a for a in range(1, 200) if fit.slope * a > LOSS_THRESHOLD_S), None)
    return {"slope": float(fit.slope), "intercept": float(fit.intercept),
            "r_squared": float(fit.rvalue ** 2), "ages": x, "lap_times": y,
            "threshold_age": threshold}
