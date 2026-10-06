"""
Lap engine: lap boundaries, line-to-line timing, classification, distance
alignment, delta time and sector times.

Detection works on the samples as logged (before resampling), so crossing times
are as exact as the source allows. Everything downstream — distance grids,
delta, sectors — works on a Session.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

SLOW_LAP_RATIO = 1.07     # over 107% of the best flying lap -> "slow"
PARTIAL_LAP_RATIO = 0.97  # covers under 97% of a lap's distance -> "partial"


# ── boundaries from the different sources a log can offer ─────────────────────

def lap_runs(lap) -> list:
    """(lap label, first row, last row) for each unbroken run of a lap value."""
    if lap is None:
        return []
    lap = np.asarray(lap)
    pos = np.flatnonzero(~pd.isna(lap))
    if len(pos) == 0:
        return []
    vals = lap[pos]
    new = np.flatnonzero(vals[1:] != vals[:-1]) + 1
    return [(_lap_label(vals[s]), int(pos[s]), int(pos[e]))
            for s, e in zip(np.r_[0, new], np.r_[new - 1, len(pos) - 1])]


def runs_from_crossings(t, crossings):
    """Split the log at known line-crossing times (beacons, a position gate).
    Returns (runs, the crossing between each run and the next). Laps are
    numbered from 1, which is whatever comes before the first crossing."""
    c = np.sort(np.asarray(crossings, float))
    c = c[np.isfinite(c)]
    idx = np.searchsorted(c, t, side="right")
    new = np.flatnonzero(np.diff(idx) != 0) + 1
    starts, ends = np.r_[0, new], np.r_[new - 1, len(t) - 1]
    runs = [(int(idx[s]) + 1, int(s), int(e)) for s, e in zip(starts, ends)]
    return runs, c[idx[starts[1:]] - 1]


def distance_resets(d) -> bool:
    """True when a distance channel drops back towards zero — a lap-distance channel."""
    d = np.asarray(d, float)
    if len(d) < 3 or not np.nanmax(d) > 0:
        return False
    return bool((np.diff(d) < -0.5 * np.nanmax(d)).any())


def runs_from_distance_resets(d):
    """Split the log where a lap-distance channel drops back towards zero."""
    d = np.asarray(d, float)
    drops = np.flatnonzero(np.diff(d) < -0.5 * np.nanmax(d)) + 1
    starts, ends = np.r_[0, drops], np.r_[drops - 1, len(d) - 1]
    return [(i + 1, int(s), int(e)) for i, (s, e) in enumerate(zip(starts, ends))]


def unwrap_distance(d):
    """A distance channel that resets every lap turned into one that keeps
    counting. A channel that already keeps counting is returned unchanged."""
    d = np.asarray(d, float)
    drops = np.flatnonzero(np.diff(d) < -0.5 * np.nanmax(d)) + 1
    jump = np.zeros_like(d)
    jump[drops] = d[drops - 1]
    return d + np.cumsum(jump)


def time_restarts(t) -> bool:
    """True when time falls back to about zero again and again — a running lap
    timer rather than a session clock."""
    t = np.asarray(t, float)
    drops = np.flatnonzero(np.diff(t) < 0)
    return len(drops) > 0 and np.sum(t[drops + 1] < 0.1 * t[drops]) >= max(1, 0.8 * len(drops))


def stitch_lap_timer(t):
    """Turn a lap timer that restarts every lap into one session clock.
    Returns (session time, crossing times). The crossings are exact: each
    restart is where the timer read zero, i.e. the line."""
    t = np.asarray(t, float)
    restarts = np.flatnonzero(np.diff(t) < 0) + 1
    jump = np.zeros_like(t)
    jump[restarts] = t[restarts - 1]
    offset = np.cumsum(jump)
    return t + offset, offset[restarts]


def gate_crossings(t, x, y, gate_pos, half_width=25.0, min_lap_s=10.0):
    """Times the car crosses a line drawn across the track at sample `gate_pos`,
    square to the direction of travel there (x, y in metres). Each crossing is
    interpolated between the samples either side of it. Crossings against the
    direction of travel, or more than `half_width` m from the gate (another part
    of the track), don't count."""
    t, x, y = (np.asarray(a, float) for a in (t, x, y))
    j0, j1 = max(gate_pos - 5, 0), min(gate_pos + 5, len(x) - 1)
    ux, uy = x[j1] - x[j0], y[j1] - y[j0]
    norm = np.hypot(ux, uy)
    if not norm > 0:
        return np.array([])
    ux, uy = ux / norm, uy / norm
    along = (x - x[gate_pos]) * ux + (y - y[gate_pos]) * uy
    across = (y - y[gate_pos]) * ux - (x - x[gate_pos]) * uy
    i = np.flatnonzero((along[:-1] < 0) & (along[1:] >= 0))
    f = -along[i] / (along[i + 1] - along[i])
    near = np.abs(across[i] + f * (across[i + 1] - across[i])) < half_width
    tc = (t[i] + f * (t[i + 1] - t[i]))[near]
    if len(tc) > 1:  # position jitter while crawling past the gate can cross it twice
        tc = tc[np.r_[True, np.diff(tc) > min_lap_s]]
    return tc


# ── detection, timing and classification ──────────────────────────────────────

def detect_laps(t, runs, d=None, v_kmh=None, crossings=None, crossing_source=None):
    """Time each lap line-to-line and classify it.

    The start/finish line is crossed somewhere between the last sample of one
    lap and the first sample of the next. Unless the exact crossing times are
    given (beacons, a position gate, a restarting lap timer), the crossing time
    is estimated inside that gap. A lap runs from one crossing to the next, so
    lap times add up to the session time instead of each lap losing a slice of
    the gap.

    t: sample times [s]; runs: [(lap, first row, last row)]; d: distance [m]
    (cumulative or resetting each lap); v_kmh: speed.

    Returns (laps, info): one row per lap, plus `info` with the per-sample
    distance from the line ("distance"), the lap length and how the crossings
    were worked out."""
    t = np.asarray(t, float)
    d = None if d is None else np.asarray(d, float).copy()
    v = None if v_kmh is None else np.asarray(v_kmh, float) / 3.6
    if not runs:
        runs = [(1, 0, len(t) - 1)]
    n = len(runs)
    starts = np.array([r[1] for r in runs])
    ends = np.array([r[2] for r in runs])
    a, b = ends[:-1], starts[1:]  # last sample before / first sample after each crossing

    info = {"distance": None, "lap_length": None,
            "crossing": "estimated at the midpoint of the gap between samples"}
    # A lap-distance channel resets at the line; anything else is made to keep
    # counting (e.g. lap distance when laps are timed from a position gate).
    resets = d is not None and n > 1 and np.mean(d[b] < d[a]) >= 0.5
    if d is not None and not resets:
        d = unwrap_distance(d)
    step = float(np.median(d[a] - d[np.maximum(a - 1, 0)])) if d is not None and n > 1 else np.nan
    frac = np.full(n - 1, 0.5)
    if resets:
        # The line is at, or just past, the furthest any lap got before its
        # reset (ignoring outliers). Any offset from the true line is the same
        # at every crossing, so it cancels out of the lap times.
        typical = np.abs(d[a] - np.median(d[a])) <= 3 * abs(step)
        lap_length = float(np.max(d[a][typical]))
        info["lap_length"] = lap_length
        if crossings is None:
            to_line = np.clip(lap_length - d[a], 0, None)
            with np.errstate(divide="ignore", invalid="ignore"):
                if v is not None:
                    # Carry the lap's last sample on to the line at its speed.
                    frac = to_line / v[a] / (t[b] - t[a])
                    info["crossing"] = "extrapolated to the line from lap distance and speed"
                else:
                    frac = to_line / (to_line + d[b])
                    info["crossing"] = "interpolated from lap distance"
            frac = np.where(np.isfinite(frac), frac, 0.5)
    if crossings is not None:
        t_cross = np.asarray(crossings, float)
        with np.errstate(divide="ignore", invalid="ignore"):
            frac = (t_cross - t[a]) / (t[b] - t[a])
        frac = np.where(np.isfinite(frac), np.clip(frac, 0, 1), 0.5)
        info["crossing"] = crossing_source or "given"
    else:
        frac = np.clip(frac, 0, 1)  # the crossing can't leave the gap it happened in
        t_cross = t[a] + frac * (t[b] - t[a])

    t_start = np.r_[t[starts[0]], t_cross]
    t_end = np.r_[t_cross, t[ends[-1]]]
    lap_d = info["distance"] = _lap_distance(d, starts, ends, resets, info["lap_length"], frac, a, b)
    dist_m = lap_d[ends] - lap_d[starts] if lap_d is not None else np.full(n, np.nan)

    if resets:
        lap_ref = info["lap_length"]
    elif d is not None and n > 2:
        lap_ref = float(np.median(dist_m[1:-1]))
    else:
        lap_ref = np.nan
    if info["lap_length"] is None and np.isfinite(lap_ref):
        info["lap_length"] = lap_ref

    start_crossed = np.r_[False, np.ones(n - 1, bool)]
    end_crossed = np.r_[np.ones(n - 1, bool), False]
    lap_end = lap_d[ends] if lap_d is not None else np.full(n, np.nan)
    if n > 1 and lap_end[-1] >= lap_ref - 2 * step:
        # The log stops right at the line: finish this lap like the others.
        end_crossed[-1] = True
        t_end[-1] += np.median(t_cross - t[a])

    # Distance from wheel speed or GPS drifts a little from lap to lap. Stretch
    # each full lap to the common lap length so corners line up between laps.
    with np.errstate(divide="ignore", invalid="ignore"):
        full = start_crossed & end_crossed & (np.abs(lap_end / lap_ref - 1) < 1 - PARTIAL_LAP_RATIO)
        d_scale = np.where(full, lap_ref / lap_end, 1.0)

    laps = pd.DataFrame({
        "lap": [r[0] for r in runs],
        "start_pos": starts, "end_pos": ends,
        "t_start": t_start, "t_end": t_end, "lap_time_s": t_end - t_start,
        "d_scale": d_scale, "dist_m": dist_m,
        "start_crossed": start_crossed, "end_crossed": end_crossed,
    })
    laps["status"] = _classify_laps(laps, lap_ref) if n > 1 else "whole session"
    return laps, info


def _lap_distance(d, starts, ends, resets, lap_length, frac, a, b):
    """Distance from the start line for every sample of every lap (NaN outside laps)."""
    if d is None:
        return None
    lap_d = np.full(len(d), np.nan)
    if resets:
        # Already lap distance. A sample on the wrong side of the reset (lap
        # boundary and distance reset a sample apart, e.g. rounded beacon
        # times) is moved onto this lap's scale instead of spanning a lap.
        big_drop = -0.5 * np.nanmax(d)
        for s, e in zip(starts, ends):
            seg = d[s:e + 1].copy()
            drop = np.flatnonzero(np.diff(seg) < big_drop)
            if len(drop):
                k = drop[0] + 1
                if k < len(seg) / 2:
                    seg[:k] -= lap_length
                else:
                    seg[k:] += lap_length
            lap_d[s:e + 1] = seg
    else:
        # Distance keeps counting: measure each lap from where it crossed the line.
        at_line = np.r_[d[starts[0]], d[a] + frac * (d[b] - d[a])]
        for s, e, off in zip(starts, ends, at_line):
            lap_d[s:e + 1] = d[s:e + 1] - off
    return lap_d


def _classify_laps(laps, lap_ref):
    """out-lap: first lap in the log — it doesn't start at the line.
    in-lap: last lap, when it doesn't reach the line.
    partial: covers well under a lap's distance (a lap marker glitch).
    slow: over 107% of the best flying lap (traffic, cool-down, yellows).
    flying: everything else — these feed best lap and deltas by default."""
    status = pd.Series("flying", index=laps.index)
    status.loc[laps["dist_m"] < PARTIAL_LAP_RATIO * lap_ref] = "partial"
    status.loc[~laps["end_crossed"]] = "in-lap"
    status.loc[~laps["start_crossed"]] = "out-lap"
    flying = status == "flying"
    if flying.any():
        best = laps.loc[flying, "lap_time_s"].min()
        status.loc[flying & (laps["lap_time_s"] > SLOW_LAP_RATIO * best)] = "slow"
    return status


def _lap_label(x):
    try:
        return int(x) if float(x).is_integer() else x
    except (TypeError, ValueError):
        return x


# ── working with a Session ────────────────────────────────────────────────────

def best_lap(session, statuses=("flying",)):
    """Lap label of the fastest lap among the given statuses (falls back to any
    complete lap, then to the first lap)."""
    laps = session.laps
    pool = laps[laps["status"].isin(statuses)]
    if pool.empty:
        pool = laps[laps["start_crossed"] & laps["end_crossed"]]
    if pool.empty:
        pool = laps
    return pool.loc[pool["lap_time_s"].idxmin(), "lap"]


def lap_trace(session, lap, channels=None) -> pd.DataFrame:
    """One lap's samples: `t` from its start crossing, `dist` from the line,
    plus the requested channels (default: every measured and derived channel)."""
    row = session.lap_row(lap)
    frame = session.frame()
    rows = frame[frame["lap"] == lap]
    out = pd.DataFrame({"t": rows["time"].to_numpy() - row.t_start,
                        "dist": rows["lap_distance"].to_numpy()})
    skip = {"time", "lap", "lap_distance"}
    if channels is None:
        channels = [c for c in frame.columns if c not in skip]
    for c in channels:
        if c in rows:
            out[c] = rows[c].to_numpy()
    return out


def on_distance_grid(trace: pd.DataFrame, grid) -> pd.DataFrame:
    """Resample a lap onto a shared distance grid so two laps line up corner
    for corner, whatever their speed or sample timing."""
    trace = trace.dropna(subset=["dist"])
    x = np.maximum.accumulate(trace["dist"].to_numpy())
    keep = np.r_[True, np.diff(x) > 0]  # np.interp needs distance to keep increasing
    return pd.DataFrame({c: np.interp(grid, x[keep], trace[c].to_numpy(float)[keep])
                         for c in trace.columns if c != "dist"}, index=pd.Index(grid, name="dist"))


def distance_grid(session, step_m: float = 1.0):
    """Shared distance axis from the line to the lap length."""
    length = session.lap_length
    if not length or not np.isfinite(length):
        raise ValueError("No lap length — the session has no distance or speed channel, or only one lap.")
    return np.arange(0.0, length, step_m)


def compare_laps(session, ref_lap, lap, channels=("speed",), step_m: float = 1.0) -> pd.DataFrame:
    """Two laps on a shared distance axis.

    Columns: `t_ref`, `t_cmp` (time from the line), `delta_t` (cmp − ref; +ve
    means the compared lap is behind the reference at that point of the track),
    and `<channel>_ref` / `<channel>_cmp` for each requested channel."""
    grid = distance_grid(session, step_m)
    ref = on_distance_grid(lap_trace(session, ref_lap, channels), grid)
    cmp_ = on_distance_grid(lap_trace(session, lap, channels), grid)
    out = pd.DataFrame({"t_ref": ref["t"], "t_cmp": cmp_["t"], "delta_t": cmp_["t"] - ref["t"]})
    for c in channels:
        if c in ref and c in cmp_:
            out[f"{c}_ref"], out[f"{c}_cmp"] = ref[c], cmp_[c]
    return out


def biggest_swing(delta, grid, window_m: float = 200.0):
    """Where the delta grows and shrinks the most over `window_m` metres:
    ((from, to, change) for the biggest loss, same for the biggest gain)."""
    delta, grid = np.asarray(delta, float), np.asarray(grid, float)
    k = max(1, int(round(window_m / (grid[1] - grid[0]))))
    if len(delta) <= k:
        return None
    change = delta[k:] - delta[:-k]
    i, j = int(np.nanargmax(change)), int(np.nanargmin(change))
    return ((float(grid[i]), float(grid[i + k]), float(change[i])),
            (float(grid[j]), float(grid[j + k]), float(change[j])))


def sector_times(session, boundaries=None, n_sectors: int = 3) -> pd.DataFrame:
    """Sector times for every complete lap.

    boundaries: distances from the line [m] where sectors end (the last sector
    ends at the line). Default: `n_sectors` equal lengths. Returns one row per
    lap with columns s1..sN and lap_time_s; sector times add up to the lap time."""
    length = session.lap_length
    if boundaries is None:
        boundaries = [length * k / n_sectors for k in range(1, n_sectors)]
    edges = np.r_[0.0, np.asarray(boundaries, float), length]
    rows = []
    for lap in session.laps.itertuples():
        if not (lap.start_crossed and lap.end_crossed):
            continue
        tr = lap_trace(session, lap.lap, channels=()).dropna(subset=["dist"])
        x = np.maximum.accumulate(tr["dist"].to_numpy())
        keep = np.r_[True, np.diff(x) > 0]
        # Anchor both ends on the line so the sectors sum to the lap time
        xs = np.r_[0.0, x[keep], length]
        ts = np.r_[0.0, tr["t"].to_numpy()[keep], lap.lap_time_s]
        order = np.argsort(xs, kind="stable")
        at = np.interp(edges, xs[order], ts[order])
        at[0], at[-1] = 0.0, lap.lap_time_s
        rec = {"lap": lap.lap, **{f"s{k + 1}": at[k + 1] - at[k] for k in range(len(edges) - 1)},
               "lap_time_s": lap.lap_time_s, "status": lap.status}
        rows.append(rec)
    return pd.DataFrame(rows)


def theoretical_best(sectors: pd.DataFrame, statuses=("flying",)) -> float:
    """Sum of the best sector times over laps with the given statuses."""
    pool = sectors[sectors["status"].isin(statuses)] if "status" in sectors else sectors
    if pool.empty:
        pool = sectors
    cols = [c for c in pool.columns if c.startswith("s") and c[1:].isdigit()]
    return float(pool[cols].min().sum())
