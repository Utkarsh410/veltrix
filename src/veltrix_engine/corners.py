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

from . import circuits
from . import laps as lap_engine
from .derived import best_lat, lowpass
from .vehicle import G

STEP_M = 2.0                  # distance step for corner work
SMOOTH_M = 20.0               # smoothing of the cornering signal before detection
THRESHOLD = 0.30              # a corner is where the signal exceeds this share of its 95th percentile
MIN_LENGTH_M = 20.0           # shorter than this is a kerb strike or a correction, not a corner
MERGE_GAP_M = 25.0            # same-direction pieces closer than this are one corner
HOLD = 0.5                    # ... or any distance apart, while the signal between them stays above this share of the threshold
PEAK_MIN = 0.45               # a corner's peak must reach this share of the 95th percentile; less is a kink in a straight
SPLIT_DIP = 0.5               # two peaks with the signal falling below this share of the smaller one are two corners
SPLIT_SMOOTH_M = 60.0         # ... judged on a signal smoothed this much, so a bump or a noisy sample cannot split a corner
SLOW_KMH, FAST_KMH = 110.0, 180.0     # corner class by minimum speed
BRAKE_ON_FRACTION = 0.05      # brake counts as applied above this share of its session maximum
BRAKE_ON_G = -0.3             # ... or, with no brake channel, below this longitudinal g
BRAKE_GAP_S = 0.3             # a release shorter than this inside one braking phase (trail-braking) does not end it
TURN_IN_SHARE = 0.30          # turn-in = where lateral g starts its climb to the corner's peak (typical lap), or where
                              # it is still below this share of the peak, whichever comes first walking back
TURN_IN_SMOOTH_M = 30.0       # ... judged on the typical lateral g smoothed this much
PEAK_G_SAMPLES = 3            # peak deceleration is held for this many grid samples, so one bad sample cannot set it
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
    wanted = [c for c in ("speed", "throttle", "brake", "steering", "acc_long", "acc_long_calc", "acc_lat_calc",
                          "acc_lat_sustained",
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


def _turn_in_points(frames: dict, laps: list, corners: pd.DataFrame, grid: np.ndarray, step: float) -> dict:
    """Each corner's turn-in, one point per corner: walking back from the typical
    lap's peak lateral g between the corner's start and its apex, the point where that climb began — the last
    low point before the peak, or where lateral g was below TURN_IN_SHARE of the peak,
    whichever comes first. Never past the end of the corner before. A curved
    approach (a chicane after a long bend) then still gets its own turn-in."""
    out = {c.corner: float(c.start_m) for c in corners.itertuples()}
    if not laps:
        return out
    try:                                       # lateral g, or the path's curvature, or the steering estimate
        typical, _ = _cornering_signal({l: frames[l] for l in laps}, step)
    except ValueError:
        return out
    k_s = max(int(round(TURN_IN_SMOOTH_M / step)), 1)
    typical = pd.Series(typical).rolling(k_s, center=True, min_periods=1).mean().to_numpy()
    idx = lambda metres: int(np.clip(round(metres / step), 0, len(grid) - 1))
    floor = 0
    for c in corners.itertuples():
        c0, c1 = idx(c.start_m), idx(c.end_m)
        # turning in comes before the slowest point: in a chicane whose second part is the
        # sharper, the turn-in for the braking is still the entry to the first
        ca = max(idx(getattr(c, "apex_m", c.end_m)), c0)
        part = typical[c0:ca + 1]
        if not np.isfinite(part).any():
            floor = c1
            continue
        peak_at = c0 + int(np.nanargmax(np.abs(part)))
        peak, sign = abs(typical[peak_at]), np.sign(typical[peak_at])
        k, slack = peak_at, 0.02 * peak
        while (k > floor and np.isfinite(typical[k - 1]) and sign * typical[k - 1] >= TURN_IN_SHARE * peak
               and sign * typical[k - 1] <= sign * typical[k] + slack):      # still climbing toward the peak
            k -= 1
        out[c.corner] = float(grid[k])
        floor = c1
    return out


def before_corner(metres: float) -> str:
    """A braking point as a driver reads it off the marker boards: "120 m before the corner",
    "5 m into the corner" when the braking starts after turn-in, or "at the turn-in"."""
    if not np.isfinite(metres):
        return "—"
    if abs(metres) < 0.5:
        return "at the turn-in"
    return f"{metres:.0f} m before the corner" if metres > 0 else f"{-metres:.0f} m into the corner"


def _main_braking(braking: np.ndarray, v: np.ndarray, z0: int, apex: int, floor: int):
    """(start, end) of the braking phase that slows the car for this corner:
    of the phases that reach into the zone before the apex, the one that takes
    off the most speed. A phase may have begun before the zone's start (on the
    straight shared with the corner before) but not before `floor`, the end of
    the corner before. A late dab near the apex is then not taken for the
    braking point. None when the brakes are not used."""
    phases = []
    k = apex
    while k >= z0:
        if braking[k]:
            end = k
            while k > floor and braking[k - 1]:
                k -= 1
            phases.append((k, end))
        k -= 1
    if not phases:
        return None
    return max(phases, key=lambda p: (v[p[0]] - v[min(p[1] + 1, len(v) - 1)], -p[0]))


def _close_gaps(on: np.ndarray, t: np.ndarray, gap_s: float) -> np.ndarray:
    """Fill releases shorter than gap_s between two stretches that are on."""
    on = np.asarray(on, bool).copy()
    idx = np.flatnonzero(on)
    if len(idx) < 2:
        return on
    breaks = np.flatnonzero(np.diff(idx) > 1)
    for b in breaks:
        a, z = idx[b], idx[b + 1]                      # last on before the gap, first on after it
        if np.isfinite(t[a]) and np.isfinite(t[z]) and t[z] - t[a] <= gap_s:
            on[a:z] = True
    return on


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
        # Left and right are taken from the position plane as logged; the import
        # warns when that disagrees with the steering channel.
        return np.nanmedian(signals, axis=0), "path curvature from position"
    if "acc_lat_calc" in first:
        return np.nanmedian([f["acc_lat_calc"].to_numpy() for f in frames.values()], axis=0), "steering and speed"
    raise ValueError("Corners need lateral g, a position channel, or steering.")


def find_corners(session, laps=None, step: float = STEP_M, track="auto") -> pd.DataFrame:
    """The corners of the lap.

    track: "auto" (default) uses a circuit file from the library when one
    matches the session's venue and fits its data — that gives the circuit's
    own corner numbers and names. Pass a circuit dict to force one, or None to
    always detect corners from the data.

    laps: laps to build the picture from (default: up to eight of the fastest
    flying laps; the median over them ignores one-off mistakes).

    One row per corner: corner ("T1", ...), name, direction, start_m, apex_m
    (lowest typical speed), end_m, zone_start_m, zone_end_m, min_speed
    (typical), kind ("slow" / "medium" / "fast"). `attrs["source"]` says where
    the corners came from; `attrs["track"]` is the circuit file's name, if one
    was used, and `attrs["track_note"]` explains a file that was rejected."""
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

    # A circuit file, when one fits, decides the corners
    track_note = ""
    candidates = ([track] if isinstance(track, dict) else
                  circuits.for_venue(session.raw.info.get("Venue", "")) if track == "auto" else [])
    for t in candidates:
        ok, why = circuits.fit(t, session.lap_length, grid, smooth)
        if not ok:
            track_note = f"Circuit file '{t['name']}' was not used: {why}."
            continue
        scale = session.lap_length / float(t["lap_length_m"])
        rows = []
        for c in t["corners"]:
            a = int(np.clip(round(c["start_m"] * scale / step), 0, len(grid) - 1))
            b = int(np.clip(round(c["end_m"] * scale / step), a + 1, len(grid) - 1))
            apex = a + int(np.nanargmin(speed[a:b + 1]))
            rows.append({"corner": f"T{c['number']}", "name": c.get("name", ""), "direction": c["direction"],
                         "start_m": float(grid[a]), "apex_m": float(grid[apex]), "end_m": float(grid[b]),
                         "min_speed": float(speed[apex])})
        try:                                   # what the data alone would have made of the lap
            inferred = find_corners(session, laps, step, track=None)
            check = circuits.validate(t, session.lap_length, inferred)
        except ValueError as e:
            check = {"error": str(e)}
        out = pd.DataFrame(rows)
        out["basis"] = "circuit file"
        return _finish(out, session.lap_length,
                       dict(source=f"circuit file ({t['name']})", track=t["name"], track_note=why,
                            laps=list(laps), step=step, validation=check))

    level = THRESHOLD * np.nanpercentile(np.abs(smooth), 95)
    side = np.where(smooth > level, 1, np.where(smooth < -level, -1, 0))

    # Runs of one direction. Same-direction runs are one corner when the gap is
    # short, or when the car never stops cornering between them: lateral g
    # often sags mid-corner under heavy braking without the corner ending.
    edges = np.flatnonzero(np.diff(np.r_[0, side, 0]) != 0)
    runs = [(a, b - 1, side[a]) for a, b in zip(edges[:-1], edges[1:]) if side[a] != 0]
    merged = []
    for a, b, d in runs:
        if merged and merged[-1][2] == d:
            between = smooth[merged[-1][1] + 1:a] * d
            if (a - merged[-1][1]) * step <= MERGE_GAP_M and not np.any(side[merged[-1][1] + 1:a] == -d) \
                    or (len(between) and between.min() > HOLD * level):
                merged[-1] = (merged[-1][0], b, d)
                continue
        merged.append((a, b, d))
    p95 = np.nanpercentile(np.abs(smooth), 95)
    runs = [(a, b, d) for a, b, d in merged
            if (b - a) * step >= MIN_LENGTH_M and np.max(np.abs(smooth[a:b + 1])) >= PEAK_MIN * p95]

    # A run with two clear peaks and a real sag between them is two corners
    # taken one after the other in the same direction.
    ks = max(int(round(SPLIT_SMOOTH_M / step)), 1)
    broad = np.convolve(smooth, np.ones(ks) / ks, mode="same")
    split = []
    for a, b, d in runs:
        seg = broad[a:b + 1] * d
        cut = None
        if (b - a) * step >= 3 * MIN_LENGTH_M:
            lo = int(round(MIN_LENGTH_M / step))
            best = None
            for k in range(lo, len(seg) - lo):
                left, right = seg[:k].max(), seg[k + 1:].max()
                small = min(left, right)
                if small >= PEAK_MIN * p95 and seg[k] < SPLIT_DIP * small and (best is None or seg[k] < seg[best]):
                    best = k
            cut = best
        if cut is None:
            split.append((a, b, d))
        else:
            split += [(a, a + cut, d), (a + cut + 1, b, d)]
    runs = split
    if not runs:
        raise ValueError("No corners found — the cornering signal never rises clear of the straights.")

    rows = []
    for i, (a, b, d) in enumerate(runs):
        apex = a + int(np.nanargmin(speed[a:b + 1]))
        rows.append({"corner": f"T{i + 1}", "name": "", "direction": "L" if d > 0 else "R",
                     "start_m": float(grid[a]), "apex_m": float(grid[apex]), "end_m": float(grid[b]),
                     "min_speed": float(speed[apex])})
    out = pd.DataFrame(rows)
    out["basis"] = "inferred"
    return _finish(out, session.lap_length,
                   dict(source=source, track=None, track_note=track_note, laps=list(laps), step=step,
                        validation=None, tried=[t["name"] for t in candidates]))


def _finish(corners: pd.DataFrame, length: float, attrs: dict) -> pd.DataFrame:
    """Zones (the lap divided between the corners) and corner class."""
    mids = (corners["end_m"].to_numpy()[:-1] + corners["start_m"].to_numpy()[1:]) / 2
    corners["zone_start_m"] = np.r_[0.0, mids]
    corners["zone_end_m"] = np.r_[mids, length]
    corners["kind"] = np.where(corners["min_speed"] < SLOW_KMH, "slow",
                               np.where(corners["min_speed"] < FAST_KMH, "medium", "fast"))
    corners.attrs.update(attrs)
    return corners


# ── 2. what happened in each corner, each lap ─────────────────────────────────

def corner_metrics(session, corners: pd.DataFrame, laps=None, step: float | None = None) -> pd.DataFrame:
    """Per lap and corner:

      zone_time_s     time through the corner's zone (zones add up to the lap)
      corner_time_s   time from corner start to corner end
      entry_speed, min_speed, exit_speed   km/h at corner start, lowest, at corner end
      apex_m          where the lowest speed was
      brake_m         where the brakes went on for this corner (NaN: no braking). The braking
                      phase may begin before the zone's own start (braking for a corner often
                      starts on the straight shared with the corner before), and a release
                      shorter than BRAKE_GAP_S inside it does not end it
      turn_in_m       where the car turns in: on the typical lap (median over the laps),
                      where lateral g first builds to TURN_IN_SHARE of the corner's peak.
                      One fixed point per corner, as a marker board is; the corner's start
                      when there is no lateral g
      brake_before_m  the braking point as a driver sees it: metres before turn-in, the
                      distance a marker board counts down to. Positive is before the corner;
                      larger is earlier
      brake_release_m where that braking phase ends (the brake fully off, or trail-braking ending)
      release_after_turn_in_m   the same, metres after turn-in (negative: released before turning in)
      apex_ref_m      the corner's apex: one fixed point, the typical lap's slowest point
      throttle_after_apex_m     where throttle came back above 20%, metres after that apex
      peak_brake_g    hardest deceleration in that braking phase, sustained over PEAK_G_SAMPLES
                      grid samples; from the measured longitudinal g when the file has it,
                      otherwise from d(speed)/dt
      throttle_on_m   where throttle came back above 20% after the apex
      full_throttle_m where it reached 95%
      min_throttle    lowest throttle in the corner, %
      peak_lat_g      highest lateral g held for a quarter of a second in the corner (when
                      lateral g is available): a kerb strike's jolt does not set it
    """
    step = step or corners.attrs.get("step", STEP_M)
    laps = _usable_laps(session, laps)
    grid, frames = _lap_frames(session, laps, step)
    idx = lambda metres: int(np.clip(round(metres / step), 0, len(grid) - 1))

    brake_level = None
    if "brake" in session.channels:
        top = float(np.nanmax(session.channels["brake"]))
        brake_level = BRAKE_ON_FRACTION * top if top > 0 else None

    turn_in = _turn_in_points(frames, laps, corners, grid, step)
    rows = []
    for lap in laps:
        f = frames[lap]
        t, v = f["t"].to_numpy(), f["speed"].to_numpy()
        lap_time = float(session.lap_row(lap).lap_time_s)
        thr = f["throttle"].to_numpy() if "throttle" in f else None
        # measured longitudinal g where there is one: d(speed)/dt turns one irregular speed sample into a spike
        lon_id = "acc_long" if "acc_long" in f and f["acc_long"].notna().any() else "acc_long_calc" if "acc_long_calc" in f else None
        lon = f[lon_id].to_numpy() if lon_id else None
        lon_peak = None if lon is None else -pd.Series(lon).rolling(PEAK_G_SAMPLES, center=True, min_periods=1).max().to_numpy()
        if brake_level is not None:
            braking = f["brake"].to_numpy() > brake_level
        elif lon is not None:
            braking = lon < BRAKE_ON_G
        else:
            braking = None
        if braking is not None:
            braking = _close_gaps(braking, t, BRAKE_GAP_S)
        # the peak the tyres held, not the jolt of a kerb strike
        lat_id = "acc_lat_sustained" if "acc_lat_sustained" in f else best_lat(f)
        lat = f[lat_id].to_numpy() if lat_id else None

        previous_end = 0
        for n, c in enumerate(corners.itertuples()):
            z0, z1, c0, c1 = idx(c.zone_start_m), idx(c.zone_end_m), idx(c.start_m), idx(c.end_m)
            last = n == len(corners) - 1
            apex = c0 + int(np.argmin(v[c0:c1 + 1]))
            rec = {"lap": lap, "corner": c.corner,
                   # the lap starts and ends on the line, whatever the first and last grid points read
                   "zone_time_s": (lap_time if last else t[z1]) - (0.0 if n == 0 else t[z0]),
                   "corner_time_s": t[c1] - t[c0],
                   "entry_speed": v[c0], "min_speed": v[apex], "exit_speed": v[c1],
                   "apex_m": grid[apex], "brake_m": np.nan, "turn_in_m": turn_in[c.corner], "brake_before_m": np.nan,
                   "brake_release_m": np.nan, "release_after_turn_in_m": np.nan, "apex_ref_m": float(c.apex_m),
                   "throttle_after_apex_m": np.nan, "peak_brake_g": np.nan,
                   "throttle_on_m": np.nan, "full_throttle_m": np.nan, "min_throttle": np.nan,
                   "peak_lat_g": np.nan}
            if braking is not None:
                phase = _main_braking(braking, v, z0, apex, previous_end)
                if phase is not None:
                    start, end = phase
                    rec["brake_m"] = grid[start]
                    rec["brake_before_m"] = float(turn_in[c.corner] - grid[start])
                    rec["brake_release_m"] = float(grid[end])
                    rec["release_after_turn_in_m"] = float(grid[end] - turn_in[c.corner])
                    if lon_peak is not None:
                        rec["peak_brake_g"] = float(np.nanmax(lon_peak[start:end + 1]))
            if thr is not None:
                rec["min_throttle"] = float(np.min(thr[c0:c1 + 1]))
                after = thr[apex:z1 + 1]
                on = np.flatnonzero(after >= THROTTLE_ON)
                if len(on):
                    rec["throttle_on_m"] = grid[apex + on[0]]
                    if rec["min_throttle"] < THROTTLE_FULL:     # a corner taken flat has no throttle pick-up
                        rec["throttle_after_apex_m"] = float(grid[apex + on[0]] - c.apex_m)
                full = np.flatnonzero(after >= THROTTLE_FULL)
                if len(full):
                    rec["full_throttle_m"] = grid[apex + full[0]]
            if lat is not None:
                rec["peak_lat_g"] = float(np.nanmax(np.abs(lat[c0:c1 + 1])))
            rows.append(rec)
            previous_end = c1
    return pd.DataFrame(rows)


# ── 3. each corner across the flying laps ─────────────────────────────────────

def corner_summary(session, corners: pd.DataFrame, metrics: pd.DataFrame) -> pd.DataFrame:
    """Each corner across the flying laps.

      best_s, best_lap   fastest time through the zone and the lap that set it
      mean_s, spread_s   typical time and its standard deviation (consistency)
      available_s        mean − best: time on the table in this corner
      min_speed_best, min_speed_mean
    `attrs["ideal_lap_s"]` is the sum of the best zone times: a composite of
    several laps, each corner driven at that pace once — not a lap that was driven.

      on_best_lap_s       the session's best lap's own time through the zone
      gap_on_best_lap_s   that minus best_s: this corner's part of the gap between
                          the best lap and the ideal lap (they add up to it exactly)"""
    flying = set(session.laps.loc[session.laps["status"] == "flying", "lap"])
    m = metrics[metrics["lap"].isin(flying)] if flying else metrics
    try:
        best_lap = lap_engine.best_lap(session)
    except (ValueError, KeyError):
        best_lap = None
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
                     "on_best_lap_s": (float(part.loc[part["lap"] == best_lap, "zone_time_s"].iloc[0])
                                       if (part["lap"] == best_lap).any() else np.nan),
                     "min_speed_best": float(part["min_speed"].max()),
                     "min_speed_mean": float(part["min_speed"].mean()), "laps": int(len(part))})
    out = pd.DataFrame(rows)
    if len(out):
        out["gap_on_best_lap_s"] = out["on_best_lap_s"] - out["best_s"]
    out.attrs["ideal_lap_s"] = float(out["best_s"].sum()) if len(out) else np.nan
    out.attrs["best_lap"] = best_lap
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


def relative(metres: float, point: str) -> str:
    """A distance from a fixed point of the corner, as a driver would say it:
    "12 m after the apex", "5 m before turn-in", "at the apex". point carries its
    own article: "the apex", "turn-in"."""
    if not np.isfinite(metres):
        return "—"
    if abs(metres) < 0.5:
        return f"at {point}"
    return f"{abs(metres):.0f} m {'after' if metres > 0 else 'before'} {point}"


PASS_FIELDS = (    # (column, label, unit, +ve difference means)
    ("zone_time_s", "Time in the zone", "s", "slower"),
    ("brake_before_m", "Braking point", "m before turn-in", "braked earlier"),
    ("release_after_turn_in_m", "Brake release", "m after turn-in", "released later"),
    ("min_speed", "Minimum speed", "km/h", "faster"),
    ("throttle_after_apex_m", "Throttle on", "m after the apex", "later on throttle"),
    ("exit_speed", "Exit speed", "km/h", "faster"),
)


def pass_comparison(metrics: pd.DataFrame, corners: pd.DataFrame, ref_lap, lap) -> pd.DataFrame:
    """One lap's pass through every corner set against another's, with the fixed
    corner references a driver can find on track. One row per corner:

      corner, name, basis ("circuit file" / "inferred"), kind, direction
      ref_<field>, lap_<field>, d_<field> (lap − reference) for every PASS_FIELDS column
      ref_text / lap_text   {field: words}: "72 m before the corner", "15 m after the apex", …
      finding               what in the data goes with the time difference"""
    a = metrics[metrics["lap"] == ref_lap].set_index("corner")
    b = metrics[metrics["lap"] == lap].set_index("corner")
    if a.empty or b.empty:
        raise KeyError(f"Corner metrics are missing for lap {ref_lap if a.empty else lap}.")
    info = corners.set_index("corner")
    rows = []
    for k in info.index:
        if k not in a.index or k not in b.index:
            continue
        row = {"corner": k, "name": info.loc[k, "name"] if "name" in info else "",
               "basis": info.loc[k, "basis"] if "basis" in info else "inferred",
               "kind": info.loc[k, "kind"], "direction": info.loc[k, "direction"]}
        for col, *_ in PASS_FIELDS:
            x, y = float(a.loc[k, col]), float(b.loc[k, col])
            row[f"ref_{col}"], row[f"lap_{col}"], row[f"d_{col}"] = x, y, y - x
        def words(r):
            braked = np.isfinite(r["brake_before_m"])
            flat = np.isfinite(r["min_throttle"]) and r["min_throttle"] >= THROTTLE_FULL
            return {"brake_before_m": before_corner(r["brake_before_m"]) if braked else "no braking",
                    "release_after_turn_in_m": relative(r["release_after_turn_in_m"], "turn-in") if braked else "—",
                    "throttle_after_apex_m": "flat out" if flat else relative(r["throttle_after_apex_m"], "the apex")}
        row["ref_text"], row["lap_text"] = words(a.loc[k]), words(b.loc[k])
        row["finding"] = explain({"d_time_s": row["d_zone_time_s"],
                                  "d_entry_speed": float(b.loc[k, "entry_speed"] - a.loc[k, "entry_speed"]),
                                  "d_brake_m": float(b.loc[k, "brake_m"] - a.loc[k, "brake_m"]),
                                  "d_min_speed": row["d_min_speed"],
                                  "d_throttle_on_m": float(b.loc[k, "throttle_on_m"] - a.loc[k, "throttle_on_m"]),
                                  "d_exit_speed": row["d_exit_speed"]})
        rows.append(row)
    return pd.DataFrame(rows)


def analyse(session, laps=None) -> dict:
    """Everything in one call: {"corners", "metrics", "summary", "ideal_lap_s"}."""
    corners = find_corners(session)
    metrics = corner_metrics(session, corners, laps)
    summary = corner_summary(session, corners, metrics)
    return {"corners": corners, "metrics": metrics, "summary": summary,
            "ideal_lap_s": summary.attrs["ideal_lap_s"]}
