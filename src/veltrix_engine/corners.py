"""
Corner-by-corner analysis.

1. find_corners      where the corners are, from the data itself
2. corner_metrics    what the driver and car did in each corner, on each lap
3. corner_summary    each corner across the flying laps: best, typical, spread
4. compare_corners   one lap against a reference, corner by corner, with the
                     time gained or lost and what in the data explains it

The lap is divided into zones, one per corner, with the boundary between two
corners at the middle of the straight that joins them. Zone times therefore add
up to the lap time, so the time lost in each zone adds up to the lap delta.

Distances are metres from the start/finish line on the session's common lap
length. Speeds are km/h. A corner's direction is L or R.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import laps as lap_engine
from .derived import best_lat, lowpass
from .vehicle import G

STEP_M = 2.0                  # distance step for corner work
SMOOTH_M = 20.0               # smoothing of the cornering signal before detection
THRESHOLD = 0.30              # a corner is where the signal exceeds this share of its 95th percentile
MIN_LENGTH_M = 20.0           # shorter than this is a kerb strike or a correction, not a corner
MERGE_GAP_M = 25.0            # same-direction pieces closer than this are one corner
SLOW_KMH, FAST_KMH = 110.0, 180.0     # corner class by minimum speed
BRAKE_ON_FRACTION = 0.05      # brake counts as applied above this share of its session maximum
BRAKE_ON_G = -0.3             # ... or, with no brake channel, below this longitudinal g
THROTTLE_ON, THROTTLE_FULL = 20.0, 95.0
LAP_STATUSES = ("flying", "short", "slow", "whole session")

# What counts as a difference worth naming when two laps are compared
FINDING = {"time_s": 0.03, "brake_m": 5.0, "speed_kmh": 2.0, "entry_kmh": 3.0, "throttle_m": 5.0}


# ── laps on a common distance grid ────────────────────────────────────────────

def _usable_laps(session, laps=None) -> list:
    table = session.laps
    ok = table[table["start_crossed"] & table["end_crossed"] & table["status"].isin(LAP_STATUSES)]
    if laps is not None:
        ok = ok[ok["lap"].isin(laps)]
    return list(ok["lap"])


def _speed_for_analysis(session) -> pd.Series:
    """Speed, smoothed when it is recorded in coarse steps so minimum speeds
    are not rounded to the nearest step."""
    v = session.channels["speed"]
    if "speed" in session.coarse:
        return pd.Series(lowpass(v.to_numpy(float), 2.0, session.rate_hz), index=v.index)
    return v


def _lap_frames(session, laps, step) -> tuple[np.ndarray, dict]:
    """Each lap resampled onto the same distance grid: {lap: DataFrame}."""
    grid = lap_engine.distance_grid(session, step)
    frame = session.frame().copy()
    frame["speed"] = _speed_for_analysis(session)
    wanted = [c for c in ("speed", "throttle", "brake", "steering", "acc_long_calc", "acc_lat_calc",
                          "pos_x", "pos_y", best_lat(frame)) if c and c in frame]
    out = {}
    for lap in laps:
        row = session.lap_row(lap)
        rows = frame[frame["lap"] == lap]
        trace = pd.DataFrame({"t": rows["time"].to_numpy() - row.t_start,
                              "dist": rows["lap_distance"].to_numpy()})
        for c in wanted:
            trace[c] = rows[c].to_numpy()
        out[lap] = lap_engine.on_distance_grid(trace, grid)
    return grid, out


# ── 1. where the corners are ──────────────────────────────────────────────────

def _cornering_signal(frames: dict, step: float) -> tuple[np.ndarray, str]:
    """A signed 'how hard is the car cornering' trace along the lap, as the
    median over laps, and where it came from. +ve = left-hand corner.

    Preference: lateral g, then the path's curvature from logged position,
    then the steering-based estimate. Only its shape matters — the detection
    threshold is relative — so an unknown steering unit does no harm."""
    first = next(iter(frames.values()))
    lat_id = next((c for c in ("acc_lat_rebuilt", "acc_lat") if c in first), None)
    if lat_id:
        return np.nanmedian([f[lat_id].to_numpy() for f in frames.values()], axis=0), "lateral g"
    if {"pos_x", "pos_y"} <= set(first.columns):
        k = max(int(round(10.0 / step)), 1)
        box = np.ones(2 * k + 1) / (2 * k + 1)
        signals = []
        for f in frames.values():
            x = np.convolve(f["pos_x"].to_numpy(), box, mode="same")
            y = np.convolve(f["pos_y"].to_numpy(), box, mode="same")
            heading = np.unwrap(np.arctan2(np.gradient(y), np.gradient(x)))
            curvature = np.gradient(heading, step)
            curvature[:2 * k], curvature[-2 * k:] = 0.0, 0.0        # smoothing edge effects
            signals.append((f["speed"].to_numpy() / 3.6) ** 2 * curvature / G)
        signal = np.nanmedian(signals, axis=0)
        # A position plane can be mirrored. Steering, when logged, settles left from right.
        if "steering" in first:
            steer = np.nanmedian([f["steering"].to_numpy() for f in frames.values()], axis=0)
            if np.nansum(steer * signal) < 0:
                signal = -signal
        return signal, "path curvature from position"
    if "acc_lat_calc" in first:
        return np.nanmedian([f["acc_lat_calc"].to_numpy() for f in frames.values()], axis=0), "steering and speed"
    raise ValueError("Corners need lateral g, a position channel, or steering.")


def find_corners(session, laps=None, step: float = STEP_M) -> pd.DataFrame:
    """Locate the corners of the lap from the data.

    laps: laps to build the picture from (default: up to eight of the fastest
    flying laps; the median over them ignores one-off mistakes).

    One row per corner: corner ("T1", ...), direction, start_m, apex_m (lowest
    typical speed), end_m, zone_start_m, zone_end_m, min_speed (typical),
    kind ("slow" / "medium" / "fast"). `attrs["source"]` names the signal used."""
    if laps is None:
        table = session.laps
        pool = table[table["status"] == "flying"].sort_values("lap_time_s")["lap"]
        laps = list(pool.iloc[:8]) or _usable_laps(session)
    if not laps:
        raise ValueError("No complete laps to find corners on.")
    grid, frames = _lap_frames(session, laps, step)
    signal, source = _cornering_signal(frames, step)
    speed = np.nanmedian([f["speed"].to_numpy() for f in frames.values()], axis=0)

    k = max(int(round(SMOOTH_M / step)), 1)
    smooth = np.convolve(np.nan_to_num(signal), np.ones(k) / k, mode="same")
    level = THRESHOLD * np.nanpercentile(np.abs(smooth), 95)
    side = np.where(smooth > level, 1, np.where(smooth < -level, -1, 0))

    # Runs of one direction; then join same-direction runs across short gaps
    edges = np.flatnonzero(np.diff(np.r_[0, side, 0]) != 0)
    runs = [(a, b - 1, side[a]) for a, b in zip(edges[:-1], edges[1:]) if side[a] != 0]
    merged = []
    for a, b, d in runs:
        if merged and merged[-1][2] == d and (a - merged[-1][1]) * step <= MERGE_GAP_M \
                and not np.any(side[merged[-1][1] + 1:a] == -d):
            merged[-1] = (merged[-1][0], b, d)
        else:
            merged.append((a, b, d))
    runs = [(a, b, d) for a, b, d in merged if (b - a) * step >= MIN_LENGTH_M]
    if not runs:
        raise ValueError("No corners found — the cornering signal never rises clear of the straights.")

    length = session.lap_length
    rows = []
    for i, (a, b, d) in enumerate(runs):
        apex = a + int(np.nanargmin(speed[a:b + 1]))
        rows.append({"corner": f"T{i + 1}", "direction": "L" if d > 0 else "R",
                     "start_m": float(grid[a]), "apex_m": float(grid[apex]), "end_m": float(grid[b]),
                     "min_speed": float(speed[apex])})
    corners = pd.DataFrame(rows)
    mids = (corners["end_m"].to_numpy()[:-1] + corners["start_m"].to_numpy()[1:]) / 2
    corners["zone_start_m"] = np.r_[0.0, mids]
    corners["zone_end_m"] = np.r_[mids, length]
    corners["kind"] = np.where(corners["min_speed"] < SLOW_KMH, "slow",
                               np.where(corners["min_speed"] < FAST_KMH, "medium", "fast"))
    corners.attrs.update(source=source, laps=list(laps), step=step)
    return corners


# ── 2. what happened in each corner, each lap ─────────────────────────────────

def corner_metrics(session, corners: pd.DataFrame, laps=None, step: float | None = None) -> pd.DataFrame:
    """Per lap and corner:

      zone_time_s     time through the corner's zone (zones add up to the lap)
      corner_time_s   time from corner start to corner end
      entry_speed, min_speed, exit_speed   km/h at corner start, lowest, at corner end
      apex_m          where the lowest speed was
      brake_m         where the brakes went on for this corner (NaN: no braking)
      peak_brake_g    hardest deceleration in that braking phase
      throttle_on_m   where throttle came back above 20% after the apex
      full_throttle_m where it reached 95%
      min_throttle    lowest throttle in the corner, %
      peak_lat_g      highest lateral g in the corner (when lateral g is available)
    """
    step = step or corners.attrs.get("step", STEP_M)
    laps = _usable_laps(session, laps)
    grid, frames = _lap_frames(session, laps, step)
    idx = lambda metres: int(np.clip(round(metres / step), 0, len(grid) - 1))

    brake_level = None
    if "brake" in session.channels:
        top = float(np.nanmax(session.channels["brake"]))
        brake_level = BRAKE_ON_FRACTION * top if top > 0 else None

    rows = []
    for lap in laps:
        f = frames[lap]
        t, v = f["t"].to_numpy(), f["speed"].to_numpy()
        lap_time = float(session.lap_row(lap).lap_time_s)
        thr = f["throttle"].to_numpy() if "throttle" in f else None
        lon = f["acc_long_calc"].to_numpy() if "acc_long_calc" in f else None
        if brake_level is not None:
            braking = f["brake"].to_numpy() > brake_level
        elif lon is not None:
            braking = lon < BRAKE_ON_G
        else:
            braking = None
        lat_id = best_lat(f)
        lat = f[lat_id].to_numpy() if lat_id else None

        for n, c in enumerate(corners.itertuples()):
            z0, z1, c0, c1 = idx(c.zone_start_m), idx(c.zone_end_m), idx(c.start_m), idx(c.end_m)
            last = n == len(corners) - 1
            apex = c0 + int(np.argmin(v[c0:c1 + 1]))
            rec = {"lap": lap, "corner": c.corner,
                   # the lap starts and ends on the line, whatever the first and last grid points read
                   "zone_time_s": (lap_time if last else t[z1]) - (0.0 if n == 0 else t[z0]),
                   "corner_time_s": t[c1] - t[c0],
                   "entry_speed": v[c0], "min_speed": v[apex], "exit_speed": v[c1],
                   "apex_m": grid[apex], "brake_m": np.nan, "peak_brake_g": np.nan,
                   "throttle_on_m": np.nan, "full_throttle_m": np.nan, "min_throttle": np.nan,
                   "peak_lat_g": np.nan}
            if braking is not None:
                on = np.flatnonzero(braking[z0:apex + 1])
                if len(on):
                    # The braking phase that runs up to the apex: walk back from its last sample
                    end = z0 + on[-1]
                    start = end
                    while start > z0 and braking[start - 1]:
                        start -= 1
                    rec["brake_m"] = grid[start]
                    if lon is not None:
                        rec["peak_brake_g"] = -float(np.min(lon[start:end + 1]))
            if thr is not None:
                rec["min_throttle"] = float(np.min(thr[c0:c1 + 1]))
                after = thr[apex:z1 + 1]
                on = np.flatnonzero(after >= THROTTLE_ON)
                if len(on):
                    rec["throttle_on_m"] = grid[apex + on[0]]
                full = np.flatnonzero(after >= THROTTLE_FULL)
                if len(full):
                    rec["full_throttle_m"] = grid[apex + full[0]]
            if lat is not None:
                rec["peak_lat_g"] = float(np.nanmax(np.abs(lat[c0:c1 + 1])))
            rows.append(rec)
    return pd.DataFrame(rows)


# ── 3. each corner across the flying laps ─────────────────────────────────────

def corner_summary(session, corners: pd.DataFrame, metrics: pd.DataFrame) -> pd.DataFrame:
    """Each corner across the flying laps.

      best_s, best_lap   fastest time through the zone and the lap that set it
      mean_s, spread_s   typical time and its standard deviation (consistency)
      available_s        mean − best: time on the table in this corner
      min_speed_best, min_speed_mean
    `attrs["ideal_lap_s"]` is the sum of the best zone times — the lap the
    driver has already driven, one corner at a time."""
    flying = set(session.laps.loc[session.laps["status"] == "flying", "lap"])
    m = metrics[metrics["lap"].isin(flying)] if flying else metrics
    rows = []
    for c in corners.itertuples():
        part = m[m["corner"] == c.corner]
        if part.empty:
            continue
        best = part.loc[part["zone_time_s"].idxmin()]
        rows.append({"corner": c.corner, "direction": c.direction, "kind": c.kind,
                     "best_s": float(best["zone_time_s"]), "best_lap": best["lap"],
                     "mean_s": float(part["zone_time_s"].mean()),
                     "spread_s": float(part["zone_time_s"].std(ddof=0)),
                     "available_s": float(part["zone_time_s"].mean() - best["zone_time_s"]),
                     "min_speed_best": float(part["min_speed"].max()),
                     "min_speed_mean": float(part["min_speed"].mean()), "laps": int(len(part))})
    out = pd.DataFrame(rows)
    out.attrs["ideal_lap_s"] = float(out["best_s"].sum()) if len(out) else np.nan
    return out


# ── 4. one lap against a reference ────────────────────────────────────────────

def explain(delta: dict) -> str:
    """What in the data goes with a time difference in one corner.
    delta: the d_* fields of a compare_corners row. Returns a short phrase;
    it describes what differed, in the order it happened, not a proven cause."""
    dt = delta["d_time_s"]
    if abs(dt) < FINDING["time_s"]:
        return "level"
    lost = dt > 0
    parts = []

    def note(value, limit, worse_when_negative, slower_text, faster_text, unit, digits=0):
        if value is None or not np.isfinite(value) or abs(value) < limit:
            return
        worse = value < 0 if worse_when_negative else value > 0
        if worse == lost:                       # only what points the same way as the time
            parts.append(f"{slower_text if worse else faster_text} by {abs(value):.{digits}f} {unit}")

    note(delta.get("d_entry_speed"), FINDING["entry_kmh"], True, "arrived slower", "arrived faster", "km/h")
    note(delta.get("d_brake_m"), FINDING["brake_m"], True, "braked earlier", "braked later", "m")
    note(delta.get("d_min_speed"), FINDING["speed_kmh"], True, "lower minimum speed", "higher minimum speed", "km/h")
    note(delta.get("d_throttle_on_m"), FINDING["throttle_m"], False, "later on throttle", "earlier on throttle", "m")
    note(delta.get("d_exit_speed"), FINDING["speed_kmh"], True, "slower exit", "faster exit", "km/h")
    return "; ".join(parts) if parts else "no single difference stands out"


def compare_corners(metrics: pd.DataFrame, ref_lap, lap) -> pd.DataFrame:
    """`lap` against `ref_lap`, corner by corner. d_* columns are lap − reference:

      d_time_s          +ve = time lost in this corner's zone (these add up to the lap delta)
      d_entry_speed, d_min_speed, d_exit_speed   km/h, +ve = faster
      d_brake_m         +ve = braked later (deeper)
      d_throttle_on_m   +ve = back on throttle later
      finding           what in the data goes with the time difference"""
    a = metrics[metrics["lap"] == ref_lap].set_index("corner")
    b = metrics[metrics["lap"] == lap].set_index("corner")
    if a.empty or b.empty:
        raise KeyError(f"Corner metrics are missing for lap {ref_lap if a.empty else lap}.")
    out = pd.DataFrame({
        "corner": a.index,
        "d_time_s": (b["zone_time_s"] - a["zone_time_s"]).to_numpy(),
        "d_entry_speed": (b["entry_speed"] - a["entry_speed"]).to_numpy(),
        "d_brake_m": (b["brake_m"] - a["brake_m"]).to_numpy(),
        "d_min_speed": (b["min_speed"] - a["min_speed"]).to_numpy(),
        "d_throttle_on_m": (b["throttle_on_m"] - a["throttle_on_m"]).to_numpy(),
        "d_exit_speed": (b["exit_speed"] - a["exit_speed"]).to_numpy(),
    })
    out["finding"] = [explain(r) for r in out.to_dict("records")]
    return out


def analyse(session, laps=None) -> dict:
    """Everything in one call: {"corners", "metrics", "summary", "ideal_lap_s"}."""
    corners = find_corners(session)
    metrics = corner_metrics(session, corners, laps)
    summary = corner_summary(session, corners, metrics)
    return {"corners": corners, "metrics": metrics, "summary": summary,
            "ideal_lap_s": summary.attrs["ideal_lap_s"]}
