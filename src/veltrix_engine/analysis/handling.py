"""
Understeer and oversteer detection, located by lap, corner and corner phase.

The idea
--------
For a given speed and lateral acceleration, a car needs a certain amount of
steering. The engine learns that relationship from the session's own flying
laps:

    steering into the corner = |ay| · (a · L·g/V² + b + c·|ay|)

(the kinematic part fades with speed, the understeer part grows with lateral g).
The difference between the steering actually applied and that expectation is
the *steering residual*, in degrees at the steering wheel:

    residual > 0   more steering than this car normally needs here  → understeer
    residual < 0   less steering than normal, or opposite lock      → oversteer

Every lap has the same small residual pattern at the same place on the track —
steering leads lateral g at turn-in and unwinds early at exit. So the typical
residual at each point of the lap (the median over the flying laps) is taken
out, leaving the *excess*: what was different on this lap. An incident is a
stretch where the excess is clearly outside its normal scatter for long enough
to be more than noise. "Normal scatter" is judged point by point around the
lap: where laps naturally differ a lot from each other — the direction change
in a chicane, where a few metres of timing swing the steering by a hundred
degrees — a lap has to differ by correspondingly more to count.

Two different things come out of this:

  events     incidents on individual laps — where a lap departed from the norm
  tendency   each corner's habitual balance — the typical residual itself

Opposite lock is an oversteer event in its own right: steering against the
direction the car is turning, at a point where the driver normally steers into
the corner, followed by the steering coming back into the same corner. (Without
that last condition every chicane would qualify: through a direction change the
steering leads the car, but it never comes back.) A slide caught on the way out
of a corner, where the car simply straightens, is still found through the
excess — it is just not labelled as opposite lock.

Needs steering, speed and a usable lateral g (logged finely, or rebuilt).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .. import corners as corner_engine
from ..derived import best_lat
from ..vehicle import G

MIN_SPEED_KMH = 40.0        # below this the car is in the pits or recovering
CORNERING_G = 0.5           # the model is fitted, and incidents looked for, above this lateral g
SIGMA_LEVEL = 3.5           # an incident is this many robust standard deviations from normal
FLOOR_DEG = 6.0             # ... and at least this much steering, whatever the scatter
MIN_DURATION_S = 0.25       # shorter than this is a kerb or a twitch
MERGE_GAP_M = 12.0          # pieces of one incident separated by less than this are joined
COUNTER_STEER_DEG = 8.0     # opposite lock beyond this, while cornering, is oversteer outright
APEX_WINDOW = 0.15          # "apex" phase: within this share of the corner's length of the apex
SEVERITY = (1.6, 2.6)       # peak ÷ threshold: below → mild, between → moderate, above → severe
LOCAL_SMOOTH_M = 20.0       # the point-by-point scatter is smoothed over this distance
MIN_LAPS_LOCAL = 6          # fewer flying laps than this: one scatter figure for the whole lap
RETURN_S = 1.5              # after opposite lock, the steering must come back into the corner within this time
LOCK_SEVERITY = (25.0, 70.0)    # degrees of opposite lock: below → mild, between → moderate, above → severe
WHEELSPIN_KMH = 4.0         # rear wheels this much faster than the fronts counts as wheelspin
_RANK = {"mild": 0, "moderate": 1, "severe": 2}


def _cause(kind: str, driver: str, wheelspin: bool) -> str:
    """A short label for the situation the incident happened in."""
    if kind == "oversteer":
        if driver == "on throttle":
            return "power oversteer, rear wheels spinning" if wheelspin else "power oversteer"
        return "oversteer under braking" if driver == "braking" else "lift-off oversteer"
    if driver == "on throttle":
        return "power understeer"
    return "understeer under braking" if driver == "braking" else "mid-corner understeer"


def _model_terms(v_ms, ay, wheelbase):
    x = np.degrees(wheelbase * G / np.maximum(v_ms, 1.0) ** 2)      # kinematic steer per g, deg
    return np.column_stack([x, np.ones_like(x), np.abs(ay)])


def _robust_sigma(x) -> float:
    x = x[np.isfinite(x)]
    if len(x) < 20:
        return np.nan
    return float(1.4826 * np.median(np.abs(x - np.median(x))))


def analyse(session, corners: pd.DataFrame | None = None, laps=None) -> dict:
    """Detect understeer and oversteer incidents and each corner's balance tendency.

    Returns a dict:
      events    one row per incident: lap, corner, direction, kind
                ("understeer" / "oversteer"), phase ("entry" / "apex" / "exit"),
                driver ("braking" / "on throttle" / "coasting"), cause,
                start_m, end_m, peak_m, duration_s, peak_deg (excess steering
                at the wheel; +ve understeer), opposite_lock_deg, severity,
                counter_steer, speed, lat_g, rear_slip_kmh (rear minus front
                wheel speed, when wheel speeds are logged), lap_status, cost_s
                (flying laps only: that lap's time through the corner's zone
                minus the typical time there)
      by_corner per corner: incidents of each kind, laps affected, time cost
      tendency  per corner: the habitual steering residual at entry, apex and
                exit over the flying laps (+ve understeer), and a one-word read
      model     the fitted steering relationship and the thresholds used
      trace     {lap: DataFrame(dist, residual, excess)} for plotting
    """
    frame = session.frame()
    lat_id = best_lat(frame)
    usable = lat_id is not None and not ("acc_lat" in session.coarse and "acc_lat" not in session.rebuilt)
    if not usable or "steering" not in frame:
        raise ValueError("Understeer / oversteer detection needs steering and a usable lateral g channel.")
    if corners is None:
        corners = corner_engine.find_corners(session)
    step = corners.attrs.get("step", corner_engine.STEP_M)
    laps = corner_engine._usable_laps(session, laps)
    grid, frames = corner_engine._lap_frames(session, laps, step)
    flying = [l for l in laps if l in set(session.laps.loc[session.laps["status"] == "flying", "lap"])] or laps
    L = session.vehicle.wheelbase_m

    status_of = session.laps.set_index("lap")["status"]
    wheels = [f"wheel_speed_{c}" for c in ("fl", "fr", "rl", "rr")]
    slip_frames = None
    if all(w in frame for w in wheels):
        # rear minus front wheel speed, on the same distance grid
        slip_frames = {}
        wheel_frame = session.frame(["lap", "lap_distance", *wheels])
        for lap in laps:
            rows = wheel_frame[wheel_frame["lap"] == lap]
            slip = (rows[wheels[2]] + rows[wheels[3]] - rows[wheels[0]] - rows[wheels[1]]).to_numpy(float) / 2
            d = np.maximum.accumulate(np.nan_to_num(rows["lap_distance"].to_numpy(float)))
            keep = np.r_[True, np.diff(d) > 0]
            slip_frames[lap] = np.interp(grid, d[keep], slip[keep])

    def lap_arrays(lap):
        f = frames[lap]
        v, ay, steer = f["speed"].to_numpy() / 3.6, f[lat_id].to_numpy(), f["steering"].to_numpy()
        return f, v, ay, steer * np.sign(ay)            # steering into the corner (+), opposite lock (−)

    # ── the car's normal steering, from the flying laps ────────────────────
    X, y = [], []
    for lap in flying:
        _, v, ay, into = lap_arrays(lap)
        use = (v > MIN_SPEED_KMH / 3.6) & (np.abs(ay) > CORNERING_G) & (into > 0)
        X.append(_model_terms(v[use], ay[use], L))
        y.append(into[use] / np.abs(ay[use]))
    X, y = np.vstack(X), np.concatenate(y)
    if len(y) < 500:
        raise ValueError("Too little cornering data to learn the car's normal steering.")
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)

    def residual_of(lap):
        f, v, ay, into = lap_arrays(lap)
        expected = np.abs(ay) * (_model_terms(v, ay, L) @ coef)
        cornering = (v > MIN_SPEED_KMH / 3.6) & (np.abs(ay) > CORNERING_G)
        return np.where(cornering, into - expected, 0.0), cornering, into, f, v, ay

    per_lap = {lap: residual_of(lap) for lap in laps}
    typical = (np.median([per_lap[l][0] for l in flying], axis=0) if len(flying) >= 3
               else np.zeros(len(grid)))
    excess_all = np.concatenate([(per_lap[l][0] - typical)[per_lap[l][1]] for l in flying])
    sigma = _robust_sigma(excess_all)
    if not np.isfinite(sigma):
        raise ValueError("Too little cornering data to set incident thresholds.")
    # Scatter at each point of the lap, never below the whole-lap figure
    sigma_local = np.full(len(grid), sigma)
    if len(flying) >= MIN_LAPS_LOCAL:
        stack = np.array([per_lap[l][0] - typical for l in flying])
        mad = 1.4826 * np.median(np.abs(stack), axis=0)
        k = max(int(round(LOCAL_SMOOTH_M / step)), 1)
        # widest scatter nearby, so a lap that is early or late through a transition is still covered
        widened = pd.Series(mad).rolling(2 * k + 1, center=True, min_periods=1).max().to_numpy()
        sigma_local = np.maximum(widened, sigma)
    level = np.maximum(SIGMA_LEVEL * sigma_local, FLOOR_DEG)
    typical_into = np.median([per_lap[l][2] for l in flying], axis=0)

    # ── corner lookup ──────────────────────────────────────────────────────
    metrics = corner_engine.corner_metrics(session, corners, laps)
    apex_of = metrics.set_index(["lap", "corner"])["apex_m"]
    zone_time = metrics.set_index(["lap", "corner"])["zone_time_s"]
    typical_time = metrics[metrics["lap"].isin(flying)].groupby("corner")["zone_time_s"].median()
    zone_ends = corners["zone_end_m"].to_numpy()
    brake_level = None
    if "brake" in frame:
        top = float(np.nanmax(frame["brake"]))
        brake_level = corner_engine.BRAKE_ON_FRACTION * top if top > 0 else None

    def runs(mask):
        edges = np.flatnonzero(np.diff(np.r_[False, mask, False].astype(int)))
        out = []
        for a, b in zip(edges[::2], edges[1::2] - 1):
            if out and (a - out[-1][1]) * step <= MERGE_GAP_M:
                out[-1] = (out[-1][0], b)
            else:
                out.append((a, b))
        return out

    events, traces = [], {}
    for lap in laps:
        res, cornering, into, f, v, ay = per_lap[lap]
        excess = np.where(cornering, res - typical, 0.0)
        t = f["t"].to_numpy()
        traces[lap] = pd.DataFrame({"dist": grid, "residual": res, "excess": excess})
        # Opposite lock: against the corner, where the driver normally steers into it —
        # and afterwards the steering comes back into the SAME corner while the car is
        # still turning that way. In a direction change the steering never comes back:
        # the car follows it into the next corner, so that is not a correction.
        counter = cornering & (into < -COUNTER_STEER_DEG) & (typical_into > 0)
        steer = f["steering"].to_numpy()
        for a, b in runs(counter):
            way = np.sign(ay[a])
            end = min(int(np.searchsorted(t, t[b] + RETURN_S)), len(t) - 1)
            came_back = False
            for i in range(b + 1, end + 1):
                if ay[i] * way < 0.3:                 # the corner is over, or the car has changed direction
                    break
                if steer[i] * way > COUNTER_STEER_DEG / 2:
                    came_back = True
                    break
            if not came_back:
                counter[a:b + 1] = False
        # Different from the norm AND on the right side of the car's own steering model:
        # where most laps slide, a tidy lap is not "understeer" just because it differs.
        under = (excess > level) & (res > FLOOR_DEG)
        over = ((excess < -level) & (res < -FLOOR_DEG)) | counter
        for kind, mask in (("understeer", under), ("oversteer", over)):
            for a, b in runs(mask):
                duration = float(t[min(b + 1, len(t) - 1)] - t[a])
                opposite = bool(counter[a:b + 1].any()) if kind == "oversteer" else False
                if duration < MIN_DURATION_S and not (opposite and duration >= MIN_DURATION_S / 2):
                    continue
                seg = excess[a:b + 1] / level[a:b + 1]
                k = a + int(np.argmax(seg) if kind == "understeer" else np.argmin(seg))
                ci = int(min(np.searchsorted(zone_ends, grid[k], side="right"), len(corners) - 1))
                c = corners.iloc[ci]
                apex = float(apex_of.get((lap, c["corner"]), c["apex_m"]))
                window = APEX_WINDOW * max(c["end_m"] - c["start_m"], 40.0)
                phase = "apex" if abs(grid[k] - apex) <= window else ("entry" if grid[k] < apex else "exit")
                if brake_level is not None and f["brake"].to_numpy()[k] > brake_level:
                    driver = "braking"
                elif "throttle" in f and f["throttle"].to_numpy()[k] >= 50:
                    driver = "on throttle"
                else:
                    driver = "coasting"
                ratio = abs(excess[k]) / level[k]
                severity = "mild" if ratio < SEVERITY[0] else "moderate" if ratio < SEVERITY[1] else "severe"
                # 90th percentile, so one bad steering sample cannot set the figure
                lock = float(max(np.percentile(-into[a:b + 1], 90), 0.0)) if opposite else 0.0
                if opposite:
                    by_lock = "mild" if lock < LOCK_SEVERITY[0] else "moderate" if lock < LOCK_SEVERITY[1] else "severe"
                    severity = max(severity, by_lock, key=_RANK.get)
                slip = float(np.max(slip_frames[lap][a:b + 1])) if slip_frames else np.nan
                is_flying = status_of.get(lap) == "flying"
                events.append({
                    "lap": lap, "corner": c["corner"], "direction": c["direction"], "kind": kind,
                    "phase": phase, "driver": driver,
                    "cause": _cause(kind, driver, bool(np.isfinite(slip) and slip >= WHEELSPIN_KMH)),
                    "start_m": float(grid[a]), "end_m": float(grid[b]), "peak_m": float(grid[k]),
                    "duration_s": duration, "peak_deg": float(excess[k]),
                    "opposite_lock_deg": lock, "severity": severity,
                    "counter_steer": opposite, "speed": float(v[k] * 3.6), "lat_g": float(abs(ay[k])),
                    "rear_slip_kmh": slip, "lap_status": status_of.get(lap),
                    "cost_s": float(zone_time.get((lap, c["corner"]), np.nan)
                                    - typical_time.get(c["corner"], np.nan)) if is_flying else np.nan,
                })
    cols = ["lap", "corner", "direction", "kind", "phase", "driver", "cause", "start_m", "end_m", "peak_m",
            "duration_s", "peak_deg", "opposite_lock_deg", "severity", "counter_steer", "speed", "lat_g",
            "rear_slip_kmh", "lap_status", "cost_s"]
    events = pd.DataFrame(events, columns=cols)
    if len(events):
        order = {l: i for i, l in enumerate(laps)}
        events = events.sort_values(["lap", "peak_m"], key=lambda s: s.map(order) if s.name == "lap" else s) \
                       .reset_index(drop=True)

    # ── per corner: incidents, and the habitual balance ────────────────────
    rows_c, rows_t = [], []
    for c in corners.itertuples():
        ev = events[events["corner"] == c.corner]
        us, os_ = ev[ev["kind"] == "understeer"], ev[ev["kind"] == "oversteer"]
        common = ev["cause"].mode().iloc[0] if len(ev) else ""
        where = ev["phase"].mode().iloc[0] if len(ev) else ""
        rows_c.append({"corner": c.corner, "direction": c.direction, "kind": c.kind,
                       "understeer": int(len(us)), "oversteer": int(len(os_)),
                       "severe": int((ev["severity"] == "severe").sum()),
                       "laps_affected": int(ev["lap"].nunique()), "laps": int(len(laps)),
                       "usual_phase": where, "usual_cause": common,
                       "mean_cost_s": float(ev.groupby("lap")["cost_s"].first().mean()) if len(ev) else np.nan})
        i0, i1 = int(round(c.start_m / step)), int(round(c.end_m / step))
        ia = int(round(c.apex_m / step))
        w = max(int(round(APEX_WINDOW * max(c.end_m - c.start_m, 40.0) / step)), 1)
        phases = {"entry": (i0, max(ia - w, i0)), "apex": (max(ia - w, i0), min(ia + w, i1)), "exit": (min(ia + w, i1), i1)}
        rec = {"corner": c.corner, "direction": c.direction, "kind": c.kind}
        for name, (a, b) in phases.items():
            part = typical[a:b + 1]
            part = part[part != 0]
            rec[f"{name}_deg"] = float(np.mean(part)) if len(part) else np.nan
        whole = typical[i0:i1 + 1]
        whole = whole[whole != 0]
        rec["overall_deg"] = float(np.mean(whole)) if len(whole) else np.nan
        lim = max(sigma, FLOOR_DEG / 2)
        rec["read"] = ("—" if not np.isfinite(rec["overall_deg"]) else
                       "understeer" if rec["overall_deg"] > lim else
                       "oversteer" if rec["overall_deg"] < -lim else "neutral")
        rows_t.append(rec)

    model = {"kinematic": float(coef[0]), "base_deg_per_g": float(coef[1]), "per_g_deg_per_g": float(coef[2]),
             "sigma_deg": sigma, "threshold_deg": float(np.min(level)),
             "threshold_max_deg": float(np.max(level)), "local_scatter": len(flying) >= MIN_LAPS_LOCAL,
             "samples": int(len(y)),
             "flying_laps": len(flying), "positional_baseline": len(flying) >= 3,
             "lateral_source": "rebuilt from damper roll" if lat_id == "acc_lat_rebuilt" else "logged"}
    return {"events": events, "by_corner": pd.DataFrame(rows_c), "tendency": pd.DataFrame(rows_t),
            "model": model, "trace": traces, "corners": corners}
